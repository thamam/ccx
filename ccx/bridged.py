"""`ccx daemon` — one stub process per live Codex thread, and no more.

Polls `thread/loaded/list`, spawns a stub when a thread appears, reaps it when
the thread goes away, and reaps every stub on the way out. Reaping matters more
than spawning: a stub that outlives its thread leaves a dead peer in the user's
`ListAgents`, and a registry file that outlives the daemon is worse.

Stubs are separate processes because a peer needs its own pid — the registry
filename, the `pid` field and the socket name must agree.
"""

import argparse
import fcntl
import os
import signal
import subprocess
import sys
import time

from . import claudereg, codexrpc

POLL_SECONDS = 2.0
REAP_TIMEOUT = 5.0


def thread_name(client, thread_id, taken=()):
    """The peer name for a thread.

    An explicit thread name wins — that is what `ccx codex --name vega` sets on
    the thread itself. Otherwise the derived `codex-<cwd-slug>-<short-id>` is
    used, unchanged, so nobody who is not naming threads has to care.

    Names are made unique here rather than left to be disambiguated later.
    Human-chosen names collide — two threads called `lead` is an ordinary
    Tuesday — and a name that silently routes to one of two threads is worse
    than an ugly unique one. The later claimant gets the short thread id
    appended; the first keeps the clean name.
    """
    meta = client.thread_meta(thread_id)
    chosen = (meta.get("name") or "").strip()
    if chosen:
        name = claudereg.slug(chosen, limit=48)
    else:
        name = f"codex-{claudereg.slug(meta.get('cwd') or '')}-{thread_id[-6:]}"
    if name in taken:
        name = f"{name}-{thread_id[-6:]}"
    return name


class Bridge:
    def __init__(self, codex_home=None, poll=POLL_SECONDS, verbose=False):
        self.codex_home = codex_home
        self.poll = poll
        self.verbose = verbose
        self.stubs = {}  # thread_id -> Popen
        self.names = {}  # thread_id -> the peer name we assigned it
        self.started_daemon = False
        self.client = None
        self._lock = None
        self._stop = False

    # -- lifecycle -------------------------------------------------------

    def run(self):
        # Two bridges over one registry means two stubs per Codex thread, and
        # the user sees every peer twice. The lock lives here rather than in the
        # SessionStart hook because a hook cannot win a race against itself.
        self._lock = open(_lock_path(), "w")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._log("another ccx daemon already owns this registry; exiting")
            return 0

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
                self.names.pop(thread_id, None)

    def _spawn(self, thread_id):
        # Names already handed out this run count as taken even though the stub
        # may not have written its registry file yet. Two threads that appear in
        # the same poll cycle would otherwise both read an empty registry and
        # claim the same name — which is the ambiguity this is here to prevent,
        # arriving by a different door.
        taken = {
            entry.get("name")
            for entry in claudereg.read_all().values()
            if entry.get("name")
        } | set(self.names.values())
        name = thread_name(self.client, thread_id, taken=taken)
        self.names[thread_id] = name
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
        self.names.pop(thread_id, None)
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


def _lock_path():
    """Scoped to the registry this bridge serves, so a scratch run and the
    user's own daemon never contend."""
    key = claudereg.config_dir().replace(os.sep, "_").strip("_")
    return os.path.join(claudereg.runtime_dir(), f"ccx-daemon-{key}.lock")


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
