"""`ccx mcp` — the stdio MCP server Codex agents use to reach their peers.

Registered once (`codex mcp add ccx -- ccx mcp`) and shared by every thread.
That works because every `tools/call` carries the caller's identity in
`_meta.x-codex-turn-metadata.thread_id`, so one server can tell which thread is
asking and stamp the right reply address without per-thread config.

Two verbs, deliberately: `peers_list` and `peer_send`.

The reply address is the calling thread's own stub socket — the same address
anyone else would use to reach that thread. So a recipient replies by copying
`from` into `to`, and there is no routing table anywhere in the system.

Peers are Claude sessions *and* other Codex threads: a Codex thread is reachable
through its stub like anything else, so codex-to-codex needs no extra transport,
only discovery.
"""

import json
import os
import sys
import uuid

from . import claudereg, envelope

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "ccx", "version": "0.1.0"}

TOOLS = [
    {
        "name": "peers_list",
        "description": (
            "List the agent sessions on this machine you can message: Claude "
            "Code sessions and other Codex threads. Each peer is tagged with "
            "its kind (claude or codex) alongside its working directory, "
            "status and address. You are never listed as your own peer. Pass a "
            "name or an address to peer_send."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "peer_send",
        "description": (
            "Send a message to another agent session — a Claude Code session "
            "or another Codex thread. `to` is a name or address from "
            "peers_list, or the `from` address of a message you received. The "
            "reply comes back into this Codex thread."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": (
                        "Peer name from peers_list, or an address "
                        "(uds:/path/to.sock)"
                    ),
                },
                "message": {"type": "string", "description": "Message body"},
                "summary": {
                    "type": "string",
                    "description": "Short one-line preview shown to the recipient",
                },
            },
            "required": ["to", "message"],
            "additionalProperties": False,
        },
    },
]


class McpError(Exception):
    """Reported back to the model as an isError tool result."""


# ---------------------------------------------------------------------------
# identity and addressing
# ---------------------------------------------------------------------------


def caller_thread(params):
    """The Codex thread that made this call. Never guessed.

    A wrong thread id sends a Claude session's reply into someone else's
    conversation, so an absent id is a hard error rather than a default.
    """
    meta = (params.get("_meta") or {}).get("x-codex-turn-metadata") or {}
    thread_id = meta.get("thread_id") or params.get("threadId")
    if not thread_id:
        raise McpError(
            "this call carried no Codex thread id — expected "
            "_meta['x-codex-turn-metadata'].thread_id or params.threadId. "
            "ccx cannot pick a reply address without it. This usually means the "
            "app-server contract moved; run `ccx doctor`."
        )
    return thread_id, meta


def stub_entries():
    """Registry entries this bridge owns, keyed by Codex thread id."""
    return {
        e["ccxThreadId"]: e
        for e in claudereg.read_all().values()
        if e.get("ccxStub") and e.get("ccxThreadId")
    }


def reply_address(thread_id):
    stub = stub_entries().get(thread_id)
    if not stub:
        raise McpError(
            f"no ccx stub is running for thread {thread_id}, so a reply would "
            "have nowhere to land. Start `ccx daemon`, and launch this session "
            "with `ccx codex` so the thread is attached to the app-server."
        )
    return claudereg.address(stub["messagingSocketPath"])


def peers(exclude_thread=None):
    """Every reachable peer: Claude sessions and other Codex threads alike.

    Codex threads are peers through their stubs, so listing stubs is what makes
    codex-to-codex discoverable. Sending across always worked — `resolve` takes
    a raw address and the stub injects whatever arrives — but without discovery
    it was not a feature anyone could use.

    `exclude_thread` drops the caller's own stub, and only that one. A thread
    that can address itself will eventually loop a turn back into itself.
    """
    out = []
    for entry in claudereg.read_all().values():
        if exclude_thread and entry.get("ccxThreadId") == exclude_thread:
            continue
        if not claudereg.reachable(entry):
            continue
        out.append(
            {
                "name": entry.get("name"),
                "kind": "codex" if entry.get("ccxStub") else "claude",
                "cwd": entry.get("cwd"),
                "status": entry.get("status"),
                "address": claudereg.address(entry["messagingSocketPath"]),
            }
        )
    return sorted(out, key=lambda p: p["name"] or "")


def resolve(to, exclude_thread=None):
    """Accept an address or a peer name; return a socket path."""
    if to.startswith("uds:"):
        return to[4:]
    if to.startswith("/"):
        return to
    known = peers(exclude_thread=exclude_thread)
    matches = [p for p in known if p["name"] == to]
    if not matches:
        raise McpError(
            f"no peer named {to!r}. Known peers: "
            f"{[p['name'] for p in known] or '(none)'}"
        )
    if len(matches) > 1:
        raise McpError(f"{to!r} is ambiguous — use the address instead of the name")
    return matches[0]["address"][4:]


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


def tool_peers_list(params):
    # Lenient about identity, unlike peer_send: without a thread id we simply
    # cannot leave the caller out of its own listing. That is worth a caveat,
    # not a refusal — only peer_send genuinely cannot proceed without an id.
    try:
        caller, _ = caller_thread(params)
    except McpError:
        caller = None

    found = peers(exclude_thread=caller)
    if not found:
        return "No peers are currently reachable."
    lines = [
        f"{p['name']} [{p['kind']}] — {p['status']} — {p['cwd']}\n"
        f"  address: {p['address']}"
        for p in found
    ]
    if caller is None:
        lines.append(
            "\n(No caller thread id was supplied, so this list may include you.)"
        )
    return "\n".join(lines)


def tool_peer_send(params):
    thread_id, meta = caller_thread(params)
    args = params.get("arguments") or {}
    to = args.get("to")
    body = args.get("message")
    if not to or not body:
        raise McpError("peer_send needs both `to` and `message`")

    target = resolve(to, exclude_thread=thread_id)
    stub = stub_entries().get(thread_id) or {}
    from_address = reply_address(thread_id)
    msg_id = str(uuid.uuid4())

    # Carry the chain of whatever we are replying to and add ourselves. Codex
    # has no envelope, so the inbound chain is whatever our stub last recorded
    # for this thread.
    hops = envelope.parse_hops(stub.get("ccxInboundHops")) + [
        envelope.hop_id(thread_id)
    ]
    if len(hops) > envelope.HOP_LIMIT:
        raise McpError(
            f"refusing to send: this conversation has already passed through "
            f"{len(hops) - 1} sessions, at the hop limit of "
            f"{envelope.HOP_LIMIT}. Something is relaying in a circle."
        )

    content = envelope.encode(
        body,
        from_=from_address,
        hop_chain=envelope.render_hops(hops),
        # The receiving model must be able to tell this is not a Claude session.
        from_name=f"codex:{thread_id[-6:]}",
        from_mode=_mode(meta),
    )
    message = envelope.wire_message(content, from_address=from_address)
    message["msg_id"] = msg_id
    if args.get("summary"):
        message["summary"] = args["summary"]

    try:
        claudereg.send(target, message)
    except OSError as exc:
        raise McpError(
            f"could not deliver to {to}: {exc}. The session may have exited."
        ) from exc
    return (
        f"Delivered to {to} (msg_id {msg_id}). Replies come back into this "
        f"thread; your address is {from_address}."
    )


def _mode(meta):
    """Attest the sender's permission posture honestly.

    Omitting `from-mode` is not neutral — the recipient holds the message for
    user approval. Codex reports its sandbox in the turn metadata, so an
    unsandboxed thread attests `bypass` and a sandboxed one attests `prompting`.
    """
    return "bypass" if meta.get("sandbox") in (None, "none") else "prompting"


HANDLERS = {"peers_list": tool_peers_list, "peer_send": tool_peer_send}


# ---------------------------------------------------------------------------
# stdio JSON-RPC loop
# ---------------------------------------------------------------------------


def handle(request):
    """Return a response dict, or None for notifications."""
    method = request.get("method")
    params = request.get("params") or {}
    rid = request.get("id")

    if method == "initialize":
        return _ok(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _ok(rid, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            return _ok(rid, _text(f"unknown tool {name!r}", is_error=True))
        try:
            return _ok(rid, _text(handler(params)))
        except McpError as exc:
            return _ok(rid, _text(f"ccx: {exc}", is_error=True))
        except Exception as exc:  # noqa: BLE001 — never kill the server on one call
            return _ok(rid, _text(f"ccx: unexpected failure: {exc!r}", is_error=True))
    if rid is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def _ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _text(text, is_error=False):
    result = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def main(argv=None):
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        response = handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
