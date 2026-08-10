"""`ccx doctor` — the version-drift alarm.

Neither surface ccx stands on is public. Claude's `uds:` scheme, registry shape
and envelope are 2.1.226 internals; Codex's app-server is `[experimental]`. When
one of them moves, the failure mode without this command is messages that
silently vanish. So each check names the specific contract it exercises and
fails loudly with what moved, rather than reporting a general unhealthy state.

Checks are ordered cheapest-first and independent: one failure does not mask
the others.
"""

import json
import os
import socket
import sys
import time

from . import claudereg, codexrpc, envelope, mcp

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"


class Check:
    def __init__(self, name, contract):
        self.name = name
        self.contract = contract
        self.status = None
        self.detail = ""

    def record(self, status, detail=""):
        self.status, self.detail = status, detail
        return self


# ---------------------------------------------------------------------------


def check_claude_socket():
    """Contract: a process that binds a socket and drops <pid>.json is a peer.

    Exercised against a throwaway peer of our own — never against one of the
    user's live sessions.
    """
    check = Check("claude-socket", "bind + <pid>.json + one JSON line")
    pid = os.getpid()
    sock_path = claudereg.socket_path(pid)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.makedirs(os.path.dirname(sock_path), exist_ok=True)
        if os.path.exists(sock_path):
            os.unlink(sock_path)
        server.bind(sock_path)
        os.chmod(sock_path, 0o600)
        server.listen(4)
        server.settimeout(5)

        record = claudereg.entry(
            pid=pid,
            session_id="ccx-doctor",
            cwd=os.getcwd(),
            name=f"ccx-doctor-{pid}",
            sock=sock_path,
            started_at=time.time(),
            extra={"ccxStub": True, "ccxThreadId": "ccx-doctor"},
        )
        written = claudereg.write(record)
        if os.path.basename(written) != f"{pid}.json":
            return check.record(
                FAIL, f"registry filename rule broken: wrote {written}"
            )
        if not claudereg.reachable(record):
            return check.record(
                FAIL, f"bound {sock_path} but the connect probe failed"
            )

        content = envelope.encode(
            "doctor", from_=claudereg.address(sock_path), from_mode="bypass"
        )
        claudereg.send(sock_path, envelope.wire_message(content))
        # The reachability probe above left its own connection in the backlog,
        # and it carries no data. Skip past anything that sends nothing.
        line = ""
        deadline = time.time() + 5
        while not line and time.time() < deadline:
            conn, _ = server.accept()
            conn.settimeout(2)
            try:
                line = conn.recv(65536).decode().strip()
            except OSError:
                line = ""
            finally:
                conn.close()
        if not line:
            return check.record(FAIL, f"nothing arrived on {sock_path}")
        body, attrs = envelope.decode((json.loads(line)["message"])["content"])
        if body != "doctor" or attrs.get("from-mode") != "bypass":
            return check.record(
                FAIL, f"envelope did not round-trip: body={body!r} attrs={attrs}"
            )
        return check.record(PASS, f"round-tripped through {sock_path}")
    except Exception as exc:  # noqa: BLE001 — any failure here is the finding
        return check.record(FAIL, f"{type(exc).__name__}: {exc}")
    finally:
        claudereg.remove(pid)
        server.close()
        try:
            os.unlink(sock_path)
        except OSError:
            pass


def check_claude_registry():
    """Contract: peers are discovered by reading ~/.claude/sessions and probing."""
    check = Check("claude-registry", "sessions dir + connect probe")
    directory = claudereg.sessions_dir()
    if not os.path.isdir(directory):
        return check.record(
            WARN, f"{directory} does not exist — no Claude session has run here yet"
        )
    entries = claudereg.read_all()
    bad = [
        name
        for name, entry in entries.items()
        if name != f"{entry.get('pid')}.json"
    ]
    if bad:
        return check.record(
            FAIL, f"registry files whose name does not match their pid: {bad}"
        )
    live = [e for e in entries.values() if claudereg.reachable(e)]
    if not entries:
        return check.record(WARN, f"{directory} is empty — start a Claude session")
    if not live:
        return check.record(
            WARN,
            f"{len(entries)} registry entries, none reachable — every session "
            "may have exited without cleaning up",
        )
    return check.record(
        PASS, f"{len(live)} of {len(entries)} registered sessions reachable"
    )


def check_codex_rpc(codex_home=None):
    """Contract: WebSocket JSON-RPC on the app-server control socket."""
    check = Check("codex-rpc", "initialize + thread/loaded/list over WS")
    sock = codexrpc.control_socket(codex_home)
    if not os.path.exists(sock):
        return check.record(
            WARN,
            f"no daemon at {sock}. Start one with `codex app-server daemon "
            "start`, or launch sessions with `ccx codex`.",
        )
    try:
        client = codexrpc.Codex(codex_home)
    except codexrpc.CodexError as exc:
        return check.record(FAIL, f"cannot speak to the daemon: {exc}")
    try:
        if not client.info:
            return check.record(FAIL, "initialize returned no result")
        threads = client.list_threads()
        return check.record(
            PASS, f"{len(threads)} thread(s) attached to {sock}"
        )
    except codexrpc.CodexError as exc:
        return check.record(FAIL, f"{exc}")
    finally:
        client.close()


def check_mcp_meta():
    """Contract: every tools/call carries the caller's thread id in _meta.

    Only a live Codex call proves Codex still sends it; what is checked here is
    that ccx still reads the documented shape, and that the tool surface Codex
    negotiates against is intact. `ccx e2e` is the live proof.
    """
    check = Check("mcp-meta", "_meta['x-codex-turn-metadata'].thread_id")
    shape = {
        "_meta": {
            "x-codex-turn-metadata": {
                "thread_id": "019feb7a-c699-7e31-911f-1d65f7043e1d",
                "sandbox": "none",
            }
        },
        "threadId": "019feb7a-c699-7e31-911f-1d65f7043e1d",
        "name": "peers_list",
        "arguments": {},
    }
    try:
        thread_id, meta = mcp.caller_thread(shape)
    except mcp.McpError as exc:
        return check.record(FAIL, f"cannot read the documented _meta shape: {exc}")
    if thread_id != shape["threadId"]:
        return check.record(FAIL, f"read the wrong thread id: {thread_id}")

    init = mcp.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    version = init["result"]["protocolVersion"]
    if version != mcp.PROTOCOL_VERSION:
        return check.record(FAIL, f"protocol version drifted to {version}")
    listed = mcp.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )
    names = sorted(t["name"] for t in listed["result"]["tools"])
    if names != ["peer_send", "peers_list"]:
        return check.record(FAIL, f"tool surface changed: {names}")
    return check.record(
        PASS, f"MCP {version}, tools {names}, sandbox={meta['sandbox']} understood"
    )


CHECKS = (check_claude_socket, check_claude_registry, check_codex_rpc, check_mcp_meta)


def run(codex_home=None):
    results = []
    for func in CHECKS:
        if func is check_codex_rpc:
            results.append(func(codex_home))
        else:
            results.append(func())

    width = max(len(c.name) for c in results)
    for check in results:
        print(f"{check.status:<4} {check.name:<{width}}  {check.contract}")
        if check.detail:
            print(f"     {' ' * width}  {check.detail}")

    failed = [c for c in results if c.status == FAIL]
    warned = [c for c in results if c.status == WARN]
    print()
    if failed:
        print(
            f"{len(failed)} contract(s) moved: {', '.join(c.name for c in failed)}.\n"
            "This is version drift, not a transient error — ccx will drop "
            "messages until it is fixed. See docs/protocol-notes.md."
        )
        return 1
    if warned:
        print(f"{len(warned)} check(s) inconclusive; nothing is broken.")
        return 0
    print("All four contracts hold.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
