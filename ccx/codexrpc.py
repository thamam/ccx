"""JSON-RPC client for the Codex app-server, over WebSocket over a Unix socket.

The control socket does not take raw newline JSON — it requires a WebSocket
upgrade on `GET /rpc`, after which it is JSON-RPC in text frames with client
frames masked. `codex app-server proxy` produces no output and is not a usable
substitute. See docs/protocol-notes.md section 3.

Only threads attached to the daemon appear in `thread/loaded/list`; a plain
`codex` TUI is unreachable. That asymmetry is the whole launch-time cost of the
design, so `unreachable` errors here say so explicitly rather than returning
nothing.

Stdlib only.
"""

import argparse
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

CONTROL_SOCK = os.path.expanduser(
    "~/.codex/app-server-control/app-server-control.sock"
)

CLIENT_INFO = {"name": "ccx", "title": "ccx", "version": "0.1"}


class CodexError(RuntimeError):
    pass


class NoDaemon(CodexError):
    pass


# ---------------------------------------------------------------------------
# daemon lifecycle
# ---------------------------------------------------------------------------


def control_socket(codex_home=None):
    if codex_home:
        return os.path.join(
            codex_home, "app-server-control", "app-server-control.sock"
        )
    return CONTROL_SOCK


def _codex(*args, codex_home=None, timeout=30):
    env = dict(os.environ)
    if codex_home:
        env["CODEX_HOME"] = codex_home
    return subprocess.run(
        ["codex", *args], capture_output=True, text=True, env=env, timeout=timeout
    )


def daemon_running(codex_home=None):
    return _codex("app-server", "daemon", "version", codex_home=codex_home).returncode == 0


def daemon_start(codex_home=None):
    """Start the app-server daemon. Returns True if this call started it.

    Callers must remember the return value: stopping a daemon someone else
    started kills the user's live Codex sessions.
    """
    if daemon_running(codex_home):
        return False
    proc = _codex("app-server", "daemon", "start", codex_home=codex_home)
    if proc.returncode != 0:
        raise CodexError(
            f"codex app-server daemon start failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    sock = control_socket(codex_home)
    deadline = time.time() + 20
    while time.time() < deadline:
        if os.path.exists(sock) and daemon_running(codex_home):
            return True
        time.sleep(0.3)
    raise CodexError(f"daemon reported started but {sock} never became usable")


def daemon_stop(codex_home=None, timeout=30):
    return _codex(
        "app-server", "daemon", "stop", codex_home=codex_home, timeout=timeout
    ).returncode == 0


def daemon_kill(codex_home):
    """Last resort when `daemon stop` hangs. Scoped to a CODEX_HOME we own.

    Refuses to touch the default home — killing the user's daemon takes their
    live Codex sessions with it.
    """
    if not codex_home or os.path.realpath(codex_home) == os.path.realpath(
        os.path.expanduser("~/.codex")
    ):
        raise CodexError("refusing to kill the daemon for the default CODEX_HOME")
    pattern = os.path.join(codex_home, "")
    proc = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True)
    killed = []
    for line in proc.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if "app-server" in command and pattern in command:
            try:
                os.kill(int(pid), 15)
                killed.append(int(pid))
            except (OSError, ValueError):
                pass
    return killed


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------


class WsRpc:
    """WebSocket-framed JSON-RPC over an already-bound Unix socket."""

    def __init__(self, path, timeout=10):
        self.path = path
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(timeout)
        try:
            self.s.connect(path)
        except OSError as exc:
            raise NoDaemon(
                f"no Codex app-server daemon at {path} ({exc}).\n"
                "Start one with `codex app-server daemon start`."
            ) from exc

        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(
            (
                "GET /rpc HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Connection: Upgrade\r\n"
                "Upgrade: websocket\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                f"Sec-WebSocket-Key: {key}\r\n\r\n"
            ).encode()
        )

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.s.recv(4096)
            if not chunk:
                raise CodexError("server closed during WebSocket handshake")
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n")[0]
        if b"101" not in status:
            raise CodexError(f"WebSocket upgrade refused: {status!r}")

        self._buf = rest
        self._by_id = {}
        self._lock = threading.Lock()
        self._next_id = 0
        self.notifications = []
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # -- framing ---------------------------------------------------------

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise EOFError
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_loop(self):
        try:
            while True:
                hdr = self._recv_exact(2)
                length = hdr[1] & 0x7F
                if length == 126:
                    length = struct.unpack(">H", self._recv_exact(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self._recv_exact(8))[0]
                payload = self._recv_exact(length)
                if hdr[0] & 0x0F not in (1, 2):  # ignore ping/pong/close
                    continue
                try:
                    msg = json.loads(payload)
                except ValueError:
                    continue
                with self._lock:
                    if isinstance(msg, dict) and "id" in msg and (
                        "result" in msg or "error" in msg
                    ):
                        self._by_id[msg["id"]] = msg
                    else:
                        self.notifications.append(msg)
        except Exception:  # noqa: BLE001 — closed socket ends the loop
            self._closed = True

    def _send(self, obj):
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            hdr = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            hdr = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            hdr = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    # -- rpc -------------------------------------------------------------

    def call(self, method, params=None, timeout=25):
        with self._lock:
            self._next_id += 1
            rid = self._next_id
        self._send({"id": rid, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if rid in self._by_id:
                    msg = self._by_id.pop(rid)
                    break
            if self._closed:
                raise CodexError(f"connection closed while waiting for {method}")
            time.sleep(0.02)
        else:
            raise CodexError(f"{method} timed out after {timeout}s")
        if "error" in msg:
            raise CodexError(f"{method} failed: {msg['error']}")
        return msg.get("result")

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# the bits ccx actually uses
# ---------------------------------------------------------------------------


class Codex:
    """A connected, initialized app-server session."""

    def __init__(self, codex_home=None, timeout=10):
        self.codex_home = codex_home
        self.rpc = WsRpc(control_socket(codex_home), timeout=timeout)
        self.info = self.rpc.call("initialize", {"clientInfo": CLIENT_INFO})

    @classmethod
    def connect(cls, codex_home=None, start=False):
        """Connect, optionally starting the daemon first.

        Returns (client, started_by_us) so a caller that started the daemon can
        stop it again — and one that did not, cannot.
        """
        started = daemon_start(codex_home) if start else False
        return cls(codex_home), started

    def list_threads(self):
        result = self.rpc.call("thread/loaded/list", {})
        return list(result.get("data") or [])

    def start_turn(self, thread_id, text):
        """Inject a user turn. Queues if a turn is already running."""
        return self.rpc.call(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": text}]},
        )

    def inject_items(self, thread_id, items):
        """Append items to history without starting a turn."""
        return self.rpc.call(
            "thread/inject_items", {"threadId": thread_id, "items": items}
        )

    def read_thread(self, thread_id):
        return self.rpc.call("thread/read", {"threadId": thread_id})

    def close(self):
        self.rpc.close()


def require_thread(client, thread_id):
    """Fail loudly for a thread that is not daemon-attached.

    Sending works from any Codex session, but receiving requires the
    `--remote` attachment. A silent success here would let messages vanish.
    """
    live = client.list_threads()
    if thread_id not in live:
        raise CodexError(
            f"thread {thread_id} is not attached to the app-server daemon, so it "
            f"cannot receive messages.\nAttached threads: {live or '(none)'}\n"
            "Launch the session with `ccx codex` (or "
            "`codex --remote unix://<control.sock>`)."
        )


# ---------------------------------------------------------------------------
# CLI — the M1 acceptance path
# ---------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m ccx.codexrpc")
    parser.add_argument("--codex-home", default=None)
    parser.add_argument("--start", action="store_true", help="start the daemon if down")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="list attached thread ids")
    group.add_argument(
        "--inject", nargs=2, metavar=("THREAD_ID", "TEXT"), help="inject a user turn"
    )
    args = parser.parse_args(argv)

    try:
        client, _ = Codex.connect(args.codex_home, start=args.start)
    except CodexError as exc:
        print(f"ccx: {exc}", file=sys.stderr)
        return 1

    try:
        if args.list:
            threads = client.list_threads()
            print("\n".join(threads) if threads else "(no attached threads)")
            return 0
        thread_id, text = args.inject
        require_thread(client, thread_id)
        client.start_turn(thread_id, text)
        print(f"injected into {thread_id}")
        return 0
    except CodexError as exc:
        print(f"ccx: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
