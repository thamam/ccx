"""`ccx daemon` — one stub process per live Codex thread, and no more.

Polls `thread/loaded/list`, spawns a stub when a thread appears, reaps it when
the thread goes away, and reaps every stub on the way out. Reaping matters more
than spawning: a stub that outlives its thread leaves a dead peer in the user's
`ListAgents`, and a registry file that outlives the daemon is worse.

Stubs are separate processes because a peer needs its own pid — the registry
filename, the `pid` field and the socket name must agree.
"""

import argparse
import os
import signal
import subprocess
import sys
import time

from . import claudereg, codexrpc

POLL_SECONDS = 2.0
REAP_TIMEOUT = 5.0


def thread_name(client, thread_id):
    """`codex-<cwd-slug>-<short-thread-id>` — distinct enough to avoid Claude's
    `[ref]` disambiguation prompt."""
    cwd = ""
    try:
        thread = (client.read_thread(thread_id) or {}).get("thread") or {}
        cwd = thread.get("cwd") or ""
    except codexrpc.CodexError:
        pass
    return f"codex-{claudereg.slug(cwd)}-{thread_id[-6:]}"


class Bridge:
    def __init__(self, codex_home=None, poll=POLL_SECONDS, verbose=False):
        self.codex_home = codex_home
        self.poll = poll
        self.verbose = verbose
        self.stubs = {}  # thread_id -> Popen
        self.started_daemon = False
        self.client = None
        self._stop = False

    # -- lifecycle -------------------------------------------------------

    def run(self):
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, self._on_signal)
        self.started_daemon = codexrpc.daemon_start(self.codex_home)
        self.client = codexrpc.Codex(self.codex_home)
        self._log(
            f"watching {codexrpc.control_socket(self.codex_home)}"
            f"{' (daemon started by us)' if self.started_daemon else ''}"
        )
        try:
            while not self._stop:
                self._sync()
                time.sleep(self.poll)
        finally:
            self.shutdown()
        return 0

    def _on_signal(self, signum, frame):
        self._stop = True

    def shutdown(self):
        for thread_id in list(self.stubs):
            self._reap(thread_id)
        if self.client:
            self.client.close()
            self.client = None
        # Only ever stop a daemon this process started.
        if self.started_daemon:
            codexrpc.daemon_stop(self.codex_home)
            self.started_daemon = False

    # -- the loop --------------------------------------------------------

    def _sync(self):
        try:
            live = set(self.client.list_threads())
        except codexrpc.CodexError as exc:
            self._log(f"app-server unreachable: {exc}")
            return

        for thread_id in live - set(self.stubs):
            self._spawn(thread_id)
        for thread_id in set(self.stubs) - live:
            self._reap(thread_id)
        # A stub that died on its own must not be silently forgotten, or its
        # registry file survives with nothing behind the socket.
        for thread_id, proc in list(self.stubs.items()):
            if proc.poll() is not None:
                self._log(f"stub for {thread_id} exited ({proc.returncode})")
                claudereg.remove(proc.pid)
                del self.stubs[thread_id]

    def _spawn(self, thread_id):
        name = thread_name(self.client, thread_id)
        cmd = [
            sys.executable,
            "-m",
            "ccx.stub",
            "--thread",
            thread_id,
            "--name",
            name,
        ]
        if self.codex_home:
            cmd += ["--codex-home", self.codex_home]
        if self.verbose:
            cmd.append("--verbose")
        proc = subprocess.Popen(cmd, cwd=_package_root())
        self.stubs[thread_id] = proc
        self._log(f"stub {proc.pid} -> {name} ({thread_id})")

    def _reap(self, thread_id):
        proc = self.stubs.pop(thread_id, None)
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=REAP_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=REAP_TIMEOUT)
        # The stub removes its own entry; do it again in case it was killed
        # before it could.
        claudereg.remove(proc.pid)
        self._log(f"reaped stub {proc.pid} for {thread_id}")

    def _log(self, line):
        if self.verbose:
            print(f"[ccx daemon] {line}", file=sys.stderr, flush=True)


def _package_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ccx daemon")
    parser.add_argument("--codex-home", default=None)
    parser.add_argument("--poll", type=float, default=POLL_SECONDS)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    try:
        return Bridge(args.codex_home, args.poll, args.verbose).run()
    except codexrpc.CodexError as exc:
        print(f"ccx daemon: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
