"""Real Claude Code sessions, driven from the harness.

Every session here is a genuine interactive `claude` process hosted in its own
tmux session — same code path the user runs. Nothing is mocked.

Teardown is registered the instant a session is spawned, and runs on success,
on failure and on SIGINT/SIGTERM. Assume the harness will be killed mid-run.
"""

import atexit
import glob
import json
import os
import shlex
import signal
import socket
import subprocess
import time

from . import scratch as scratch_mod

_live = []
_hooked = False


def _reap_all():
    while _live:
        _live.pop().stop(quiet=True)


def _install_hooks():
    global _hooked
    if _hooked:
        return
    _hooked = True
    atexit.register(_reap_all)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        prev = signal.getsignal(sig)

        def handler(signum, frame, _prev=prev):
            _reap_all()
            if callable(_prev):
                _prev(signum, frame)
            else:
                raise SystemExit(128 + signum)

        try:
            signal.signal(sig, handler)
        except ValueError:
            pass  # not on the main thread


def tmux(*args, check=True):
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, check=check
    ).stdout


class ClaudeSession:
    """An interactive Claude Code session in a scratch config dir."""

    def __init__(self, scratch, name, oauth_token, cwd=None):
        self.scratch = scratch
        self.name = name
        self.tmux_name = f"ccx-{name}"
        self.cwd = cwd or scratch.wd
        self.overrides = scratch.child_overrides(oauth_token)
        self.pid = None
        self.registry_file = None
        self.socket_path = None
        self._stopped = False

    # -- lifecycle -------------------------------------------------------

    def start(self, timeout=60, need_socket=True):
        _install_hooks()
        subprocess.run(
            ["tmux", "kill-session", "-t", self.tmux_name],
            capture_output=True,
        )
        cmd = self._command()
        tmux("new-session", "-d", "-s", self.tmux_name, "-c", self.cwd, cmd)
        _live.append(self)
        self._await_registration(timeout, need_socket)
        return self

    def _command(self):
        """A single shell word-list: explicit env, then claude.

        The tmux server inherits the launching Claude session's environment, so
        the deny-list has to be applied in the command itself rather than by
        building os.environ here.
        """
        parts = ["env"]
        for key in scratch_mod.ENV_DENY:
            parts += ["-u", key]
        for key, value in sorted(self.overrides.items()):
            parts.append(f"{key}={value}")
        parts += [
            "claude",
            "--name",
            self.name,
            "--dangerously-skip-permissions",
        ]
        return " ".join(shlex.quote(p) for p in parts)

    def _await_registration(self, timeout, need_socket):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for fname, entry in self.scratch.registry().items():
                if entry.get("name") != self.name:
                    continue
                sock = entry.get("messagingSocketPath")
                if need_socket and not sock:
                    continue
                self.pid = entry.get("pid")
                self.registry_file = os.path.join(self.scratch.sessions_dir, fname)
                self.socket_path = sock
                return
            time.sleep(0.4)
        raise AssertionError(
            f"session {self.name!r} did not register"
            f"{' with a messaging socket' if need_socket else ''} within {timeout}s.\n"
            f"scratch registry: {list(self.scratch.registry())}\n"
            f"--- tmux pane ---\n{self.pane()}"
        )

    def stop(self, quiet=False, timeout=15):
        if self._stopped:
            return {}
        self._stopped = True
        if self in _live:
            _live.remove(self)
        subprocess.run(
            ["tmux", "kill-session", "-t", self.tmux_name], capture_output=True
        )
        # Claude unlinks its own registry file and socket on exit; give it a
        # moment, then clean up ourselves and report that it had to be forced.
        forced = {}
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._residue():
                return {}
            time.sleep(0.3)
        for path in self._residue():
            forced[path] = True
            try:
                os.unlink(path)
            except OSError:
                pass
        if forced and not quiet:
            print(f"  ! forced cleanup of {sorted(forced)}")
        return forced

    def _residue(self):
        out = []
        for path in (self.registry_file, self.socket_path):
            if path and os.path.exists(path):
                out.append(path)
        return out

    # -- interaction -----------------------------------------------------

    def pane(self, lines=200):
        try:
            return tmux(
                "capture-pane", "-p", "-t", self.tmux_name, "-S", f"-{lines}"
            )
        except subprocess.CalledProcessError:
            return "<tmux session gone>"

    def send_line(self, obj):
        """Write one JSON line into this session's messaging socket."""
        if not self.socket_path:
            raise AssertionError(f"{self.name} has no messaging socket")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(self.socket_path)
        s.sendall((json.dumps(obj) + "\n").encode())
        s.close()

    def transcript(self):
        """Every JSONL record this session has written, oldest first."""
        pattern = os.path.join(self.scratch.config, "projects", "**", "*.jsonl")
        records = []
        for path in glob.glob(pattern, recursive=True):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        pass
        return records

    def status(self):
        entry = self.scratch.registry().get(os.path.basename(self.registry_file or ""), {})
        return entry.get("status")
