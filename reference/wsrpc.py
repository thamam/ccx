"""Minimal WebSocket-over-Unix-socket JSON-RPC client for the Codex app-server.

Verified working against codex-cli 0.146.1 on 2026-08-10:
  initialize / thread/loaded/list / turn/start / turn/steer / thread/start

The Codex app-server control socket does NOT accept raw newline-delimited JSON;
it requires a WebSocket upgrade on `GET /rpc`. `codex app-server proxy` was
tested and produced no output, so this client is the supported path.

Stdlib only, no dependencies. Copy into the package rather than importing from
here once the real module exists.

    r = WsRpc(os.path.expanduser(
        "~/.codex/app-server-control/app-server-control.sock"))
    r.call("initialize", {"clientInfo":
           {"name": "ccx", "title": "ccx", "version": "0.1"}})
    tid = r.call("thread/loaded/list", {})["result"]["data"][0]
    r.call("turn/start", {"threadId": tid,
                          "input": [{"type": "text", "text": "hello"}]})
"""

import base64
import json
import os
import socket
import struct
import threading
import time


class WsRpc:
    def __init__(self, path, timeout=10):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(timeout)
        self.s.connect(path)

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
                raise EOFError("server closed during handshake")
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        if b"101" not in head.split(b"\r\n")[0]:
            raise RuntimeError(f"upgrade refused: {head.split(b'\r\n')[0]!r}")

        self._buf = rest
        self.notifications = []   # server-initiated messages
        self._by_id = {}
        threading.Thread(target=self._reader, daemon=True).start()

    # -- framing ---------------------------------------------------------

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise EOFError
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _reader(self):
        try:
            while True:
                hdr = self._recv_exact(2)
                length = hdr[1] & 0x7F
                if length == 126:
                    length = struct.unpack(">H", self._recv_exact(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self._recv_exact(8))[0]
                payload = self._recv_exact(length)
                opcode = hdr[0] & 0x0F
                if opcode not in (1, 2):        # ignore ping/pong/close
                    continue
                try:
                    msg = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(msg, dict) and "id" in msg and (
                    "result" in msg or "error" in msg
                ):
                    self._by_id[msg["id"]] = msg
                else:
                    self.notifications.append(msg)
        except Exception:
            pass

    def send(self, obj):
        """Client frames must be masked (RFC 6455)."""
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            hdr = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            hdr = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            hdr = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.s.sendall(hdr + mask + masked)

    # -- rpc -------------------------------------------------------------

    def call(self, method, params=None, rid=None, timeout=25):
        rid = rid if rid is not None else method
        self.send({"id": rid, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            if rid in self._by_id:
                return self._by_id.pop(rid)
            time.sleep(0.05)
        return {"error": "timeout"}

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass
