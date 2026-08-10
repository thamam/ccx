"""The `ccx daemon` under test, pointed at a scratch Claude registry."""

import os
import subprocess
import sys
import time

from ccx import claudereg
from . import scratch as scratch_mod, session as session_mod


class Daemon:
    """`ccx daemon`, writing its stubs into a scratch config dir."""

    def __init__(self, scratch, codex_home, poll=1.0):
        self.scratch = scratch
        self.codex_home = codex_home
        self.poll = poll
        self.proc = None
        self._log = ""
        self._stopped = False

    def start(self):
        env = {k: v for k, v in os.environ.items() if k not in scratch_mod.ENV_DENY}
        env["CLAUDE_CONFIG_DIR"] = self.scratch.config
        env["CLAUDE_CODE_TMPDIR"] = self.scratch.run
        session_mod.register_cleanup(self)
        self.proc = subprocess.Popen(
            [
                sys.executable, "-m", "ccx.bridged",
                "--codex-home", self.codex_home.root,
                "--poll", str(self.poll),
                "--verbose",
            ],
            cwd=_repo_root(),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return self

    def stubs(self):
        """Scratch registry entries the bridge owns, by name."""
        return {
            e["name"]: e
            for e in self.scratch.registry().values()
            if str(e.get("name", "")).startswith("codex-")
        }

    def wait_for_stub(self, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = self.stubs()
            if found:
                return next(iter(found.values()))
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"ccx daemon exited early ({self.proc.returncode}):\n{self.output()}"
                )
            time.sleep(0.5)
        raise AssertionError(
            f"no stub registered within {timeout}s\n"
            f"registry: {[e.get('name') for e in self.scratch.registry().values()]}\n"
            f"--- daemon output ---\n{self.output()}"
        )

    def wait_for_no_stubs(self, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.stubs():
                return True
            time.sleep(0.5)
        return False

    def output(self):
        """Everything the daemon has written so far.

        Draining the pipe is destructive, so what is read is accumulated —
        callers assert against the whole log, not against whatever happened to
        be buffered at that moment.
        """
        if not self.proc or not self.proc.stdout:
            return self._log
        os.set_blocking(self.proc.stdout.fileno(), False)
        try:
            self._log += self.proc.stdout.read() or ""
        except (OSError, ValueError):
            pass
        finally:
            try:
                os.set_blocking(self.proc.stdout.fileno(), True)
            except (OSError, ValueError):
                pass
        return self._log

    def stop(self, quiet=False, timeout=15):
        if self._stopped:
            return {}
        self._stopped = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        return {}


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def stub_socket_leftovers(scratch):
    """Sockets in the scratch cc-socks dir with no live listener behind them."""
    stale = []
    for name in sorted(os.listdir(scratch.socks) if os.path.isdir(scratch.socks) else []):
        if not name.endswith(".sock"):
            continue
        path = os.path.join(scratch.socks, name)
        if not claudereg.reachable({"messagingSocketPath": path}):
            stale.append(path)
    return stale
