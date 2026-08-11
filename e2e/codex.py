"""Real Codex sessions, isolated from the user's own.

`CODEX_HOME` gives full isolation — the daemon puts its control socket under
`$CODEX_HOME/app-server-control/`, so the harness never touches the user's
daemon or their threads. Two things have to come across into the scratch home:

- `packages/` — the daemon refuses to start without the standalone install at
  `$CODEX_HOME/packages/standalone/current/codex`. A symlink is enough.
- `auth.json` — otherwise the session is unauthenticated.

MCP server blocks are stripped from the copied config so a test session starts
with a known-empty tool surface; scenarios add back exactly what they need.
"""

import os
import re
import shutil
import signal
import subprocess
import time

from . import scratch as scratch_mod, session as session_mod
from ccx import codexrpc

REAL_CODEX_HOME = os.path.expanduser("~/.codex")


class CodexHome:
    """A scratch CODEX_HOME with its own app-server daemon."""

    def __init__(self, root="/private/tmp/ccx-e2e-codex"):
        parent = os.path.realpath(os.path.dirname(root))
        self.root = os.path.join(parent, os.path.basename(root))
        self.started_daemon = False
        self._client = None
        self._stopped = False

    @property
    def control_sock(self):
        return codexrpc.control_socket(self.root)

    def create(self):
        shutil.rmtree(self.root, ignore_errors=True)
        os.makedirs(self.root, exist_ok=True)
        packages = os.path.join(REAL_CODEX_HOME, "packages")
        if not os.path.isdir(packages):
            raise RuntimeError(
                f"{packages} is missing — the app-server daemon only starts from "
                "the standalone install managed by the Codex installer."
            )
        os.symlink(packages, os.path.join(self.root, "packages"))
        auth = os.path.join(REAL_CODEX_HOME, "auth.json")
        if not os.path.exists(auth):
            raise RuntimeError(f"{auth} is missing — run `codex login` first.")
        shutil.copyfile(auth, os.path.join(self.root, "auth.json"))
        os.chmod(os.path.join(self.root, "auth.json"), 0o600)
        self._write_config()
        return self

    def _write_config(self):
        src = os.path.join(REAL_CODEX_HOME, "config.toml")
        body = ""
        if os.path.exists(src):
            with open(src) as f:
                body = _strip_mcp_servers(f.read())
        with open(os.path.join(self.root, "config.toml"), "w") as f:
            f.write("suppress_unstable_features_warning = true\n")
            f.write(body)

    def add_mcp_server(self, name, command, args=(), env=None):
        """Append an `[mcp_servers.<name>]` block to the scratch config."""
        lines = [f'\n[mcp_servers.{name}]', f'command = "{command}"']
        if args:
            lines.append("args = [" + ", ".join(f'"{a}"' for a in args) + "]")
        if env:
            lines.append(f"\n[mcp_servers.{name}.env]")
            lines += [f'{k} = "{v}"' for k, v in env.items()]
        with open(os.path.join(self.root, "config.toml"), "a") as f:
            f.write("\n".join(lines) + "\n")

    # -- daemon ----------------------------------------------------------

    def start_daemon(self):
        session_mod.register_cleanup(self)
        self.started_daemon = codexrpc.daemon_start(self.root)
        if not self.started_daemon:
            # A daemon we did not start is running in our own scratch home;
            # that can only be a leftover, but stopping it is still our call
            # to make explicitly rather than by accident.
            raise RuntimeError(
                f"a daemon is already running for {self.root}; stop it with "
                f"`CODEX_HOME={self.root} codex app-server daemon stop`"
            )
        return self

    def client(self):
        """One shared connection. `daemon stop` blocks while clients are open,
        so polling loops must not each mint their own."""
        if self._client is None:
            self._client = codexrpc.Codex(self.root)
        return self._client

    def stop(self, quiet=False):
        if self._stopped:
            return {}
        self._stopped = True
        if self._client is not None:
            self._client.close()
            self._client = None
        if self.started_daemon:
            try:
                codexrpc.daemon_stop(self.root, timeout=10)
            except subprocess.TimeoutExpired:
                if not quiet:
                    print("  ! `codex app-server daemon stop` hung; killing the pid")
                # A SIGTERMed app-server keeps flushing its cache under
                # CODEX_HOME on the way out, and those writes land after any
                # fixed settle window — so wait for the pids to actually die
                # before deleting the tree. Waiting on pids is exact; matching
                # command lines is not.
                _await_pids(codexrpc.daemon_kill(self.root), quiet=quiet)
        scratch_mod.remove_settled(self.root, quiet=quiet)
        return {}


def _strip_mcp_servers(text):
    """Drop every [mcp_servers…] table so the scratch session starts clean."""
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        header = re.match(r"\s*\[\[?([^\]]+)\]\]?", line)
        if header:
            skipping = header.group(1).startswith("mcp_servers")
        if not skipping:
            out.append(line)
    return "".join(out)


def _await_pids(pids, quiet=False, timeout=15.0):
    """Block until every pid is gone, escalating to SIGKILL near the deadline."""
    deadline = time.time() + timeout
    escalated = False
    while time.time() < deadline:
        alive = [p for p in pids if _alive(p)]
        if not alive:
            return True
        if not escalated and time.time() > deadline - 5:
            escalated = True
            for pid in alive:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        time.sleep(0.25)
    if not quiet:
        print(f"  ! pids {[p for p in pids if _alive(p)]} would not exit")
    return False


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class CodexTui:
    """A `codex --remote` TUI in tmux, attached to a scratch daemon."""

    def __init__(self, home, name="tui", cwd=None, thread_name=None):
        self.home = home
        self.tmux_name = f"ccx-codex-{name}"
        self.cwd = cwd or home.root
        # When set, the thread is created and named through the app-server
        # before the TUI attaches — the same path `ccx codex --name` takes.
        self.thread_name = thread_name
        self.thread_id = None
        self._stopped = False

    def start(self, timeout=45):
        session_mod.register_cleanup(self)
        before = set(self.home.client().list_threads())
        subprocess.run(
            ["tmux", "kill-session", "-t", self.tmux_name], capture_output=True
        )
        cmd = (
            f"CODEX_HOME={self.home.root} codex --remote unix://{self.home.control_sock}"
        )
        if self.thread_name:
            named = self.home.client().start_thread(cwd=self.cwd, name=self.thread_name)
            cmd += f" resume {named}"
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", self.tmux_name, "-x", "200", "-y", "50",
             "-c", self.cwd, cmd],
            check=True,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            new = set(self.home.client().list_threads()) - before
            if new:
                self.thread_id = sorted(new)[0]
                return self
            time.sleep(0.5)
        raise AssertionError(
            f"codex TUI thread never appeared in thread/loaded/list within {timeout}s\n"
            f"--- tmux pane ---\n{self.pane()}"
        )

    def pane(self, lines=200):
        proc = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", self.tmux_name, "-S", f"-{lines}"],
            capture_output=True,
            text=True,
        )
        return proc.stdout if proc.returncode == 0 else "<tmux session gone>"

    def wait_for(self, needle, timeout=90):
        """Block until `needle` shows up in the pane. Returns the pane text."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            pane = self.pane()
            if needle in pane:
                return pane
            time.sleep(1)
        raise AssertionError(
            f"{needle!r} never appeared in the Codex TUI within {timeout}s\n"
            f"--- tmux pane ---\n{self.pane()}"
        )

    def stop(self, quiet=False):
        if self._stopped:
            return {}
        self._stopped = True
        subprocess.run(
            ["tmux", "kill-session", "-t", self.tmux_name], capture_output=True
        )
        return {}
