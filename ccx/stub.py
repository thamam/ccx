"""One stub process per live Codex thread.

The stub is what makes a Codex thread an ordinary peer in Claude's world: it
owns a real pid, binds a socket in the `cc-socks` directory, and writes a
matching `<pid>.json` registry entry. Claude's peer listing probes the socket
and lists it — it never checks that the pid belongs to a Claude process.

Anything written to the socket is unwrapped from its `<cross-session-message>`
envelope and pushed into the Codex thread with `turn/start`.

The registry entry must not outlive the process. A stale file puts a dead peer
in the user's real `ListAgents`, so cleanup runs from `finally`, from atexit and
from SIGINT/SIGTERM/SIGHUP.
"""

import argparse
import atexit
import json
import os
import signal
import socket
import sys
import threading
import time

from . import claudereg, codexrpc, envelope

BACKLOG = 16
MAX_LINE = 1024 * 1024  # Claude drops the connection past 1 MiB; match it.

# Wording matters here. A model that reads "held" as "refused" will give up on a
# message that is one keypress from being delivered.
RECEIPTS = {
    "held": (
        "HELD",
        "is waiting on the recipient user's approval. It has NOT been delivered "
        "yet, and it has NOT been refused. A further receipt will follow.",
    ),
    "delivered": ("DELIVERED", "was approved by the recipient user and delivered."),
    "denied": ("DENIED", "was declined by the recipient user. It was not delivered."),
    "expired": (
        "EXPIRED",
        "timed out waiting for approval and was not delivered.",
    ),
}


class Stub:
    def __init__(self, thread_id, name, cwd, codex_home=None, verbose=False):
        self.thread_id = thread_id
        self.name = name
        self.cwd = cwd
        self.codex_home = codex_home
        self.verbose = verbose
        self.pid = os.getpid()
        self.sock_path = claudereg.socket_path(self.pid)
        self.server = None
        self.codex = None
        self._cleaned = False
        self._stop = threading.Event()

    # -- lifecycle -------------------------------------------------------

    def start(self):
        os.makedirs(os.path.dirname(self.sock_path), exist_ok=True)
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.sock_path)
        # Same posture Claude uses. The bridge adds reach, not exposure.
        os.chmod(self.sock_path, 0o600)
        self.server.listen(BACKLOG)
        self.server.settimeout(0.5)

        self._install_cleanup()
        claudereg.write(
            claudereg.entry(
                pid=self.pid,
                session_id=self.thread_id,
                cwd=self.cwd,
                name=self.name,
                sock=self.sock_path,
                started_at=time.time(),
                extra={"ccxStub": True, "ccxThreadId": self.thread_id},
            )
        )
        self.codex = codexrpc.Codex(self.codex_home)
        self._log(f"stub for {self.thread_id} listening on {self.sock_path}")

    def _install_cleanup(self):
        atexit.register(self.cleanup)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, self._on_signal)

    def _on_signal(self, signum, frame):
        self._stop.set()
        self.cleanup()
        raise SystemExit(128 + signum)

    def cleanup(self):
        if self._cleaned:
            return
        self._cleaned = True
        claudereg.remove(self.pid)
        try:
            if self.server:
                self.server.close()
        except OSError:
            pass
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass
        if self.codex:
            self.codex.close()

    # -- serving ---------------------------------------------------------

    def serve_forever(self):
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = self.server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(
                    target=self._handle, args=(conn,), daemon=True
                ).start()
        finally:
            self.cleanup()

    def _handle(self, conn):
        conn.settimeout(30)
        buf = b""
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self._dispatch(line)
                if len(buf) > MAX_LINE:
                    self._log("dropping connection: line over 1 MiB")
                    break
        except (OSError, socket.timeout):
            pass
        finally:
            conn.close()

    def _dispatch(self, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            self._log(f"ignoring non-JSON line ({len(raw)} bytes)")
            return
        if msg.get("type") == "control":
            self._control(msg)
            return
        content = (msg.get("message") or {}).get("content")
        if not isinstance(content, str):
            self._log(f"ignoring message with no text content: {sorted(msg)}")
            return
        body, attrs = envelope.decode(content)
        text = self._render(body, attrs, msg)
        try:
            self.codex.start_turn(self.thread_id, text)
            self._log(f"delivered {len(text)} chars to {self.thread_id}")
        except codexrpc.CodexError as exc:
            self._log(f"delivery failed: {exc}")

    def _control(self, msg):
        """Relay a delivery receipt into the Codex thread.

        Without this, a Codex agent whose message is sitting in a Claude
        approval prompt sees silence and reads it as refusal.

        This uses `turn/start` rather than `thread/inject_items`. Injected items
        are cheaper — they cost no model turn — but they only reach the model
        when a turn happens to run afterwards, and the moment a receipt matters
        most is exactly when the agent has finished sending and gone idle. An
        injected receipt would then sit unread until the user next typed, which
        is barely better than the silence this exists to fix. `turn/start`
        queues behind a running turn rather than erroring, so it lands as soon
        as the agent is free either way.
        """
        action = msg.get("action")
        if action != "peer_message_status":
            self._log(f"ignoring control action {action!r}")
            return
        status = msg.get("status")
        receipt = RECEIPTS.get(status)
        if receipt is None:
            self._log(f"unknown peer_message_status {status!r}")
            return
        token, note = receipt
        text = f"[ccx] receipt {token}: your message {msg.get('orig_msg_id')} {note}"
        try:
            self.codex.start_turn(self.thread_id, text)
            self._log(f"relayed receipt {status}")
        except codexrpc.CodexError as exc:
            self._log(f"could not relay receipt {status}: {exc}")

    def _render(self, body, attrs, msg):
        """What the Codex agent sees.

        The reply address is carried in the text because Codex has no envelope
        of its own — the agent reads it and passes it back to `peer_send`.
        """
        sender = attrs.get("from") or msg.get("from") or "unknown"
        name = attrs.get("from-name")
        who = f"{name} ({sender})" if name else sender
        return (
            f"[message from Claude Code peer {who}]\n\n{body}\n\n"
            f"To reply, call the ccx tool `peer_send` with to={sender!r}."
        )

    def _log(self, line):
        if self.verbose:
            print(f"[stub {self.pid} {self.name}] {line}", file=sys.stderr, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m ccx.stub")
    parser.add_argument("--thread", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--codex-home", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    stub = Stub(args.thread, args.name, args.cwd, args.codex_home, args.verbose)
    try:
        stub.start()
    except Exception as exc:  # noqa: BLE001 — must not leave a half-registered peer
        stub.cleanup()
        print(f"ccx stub: {exc}", file=sys.stderr)
        return 1
    stub.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
