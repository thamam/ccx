"""M8 — two Codex threads talking to each other.

Codex threads are peers through the same stubs Claude uses, so this needs no
new transport: A's stub is an address like any other. What was missing was
discovery — `peers_list` used to drop every stub, so a Codex agent could be
sent to but could never find anyone.

The round trip is the same shape as m3, with Codex on both ends:

    codex A --1--> codex B     (peers_list, then peer_send by name)
    codex B --2--> codex A     (reply to the address it received)

Three assertions here are about honesty rather than delivery: B must be told it
is talking to a *Codex* peer and not a Claude one, B must NOT receive the
Claude-specific provenance correction (there is nothing to correct, and saying
otherwise misdescribes its own harness), and A must not be able to find itself
in its own peer listing.
"""

import os
import sys

from ..bridge import Daemon
from ..codex import CodexHome, CodexTui

PING = "CCX-M8-PING-A-TO-B"
PONG = "CCX-M8-PONG-B-TO-A"


def run(ctx):
    scratch = ctx.scratch
    home = CodexHome().create()
    home.add_mcp_server(
        "ccx",
        command=sys.executable,
        args=["-m", "ccx.mcp"],
        env={
            "PYTHONPATH": _repo_root(),
            "CLAUDE_CONFIG_DIR": scratch.config,
            "CLAUDE_CODE_TMPDIR": scratch.run,
        },
    )
    home.start_daemon()

    # Two real TUIs on one daemon; the bridge gives each a stub.
    tui_a = CodexTui(home, name="a").start()
    tui_b = CodexTui(home, name="b").start()

    daemon = Daemon(scratch, home).start()
    stub_a = _wait_for_stub(scratch, tui_a.thread_id, daemon)
    stub_b = _wait_for_stub(scratch, tui_b.thread_id, daemon)
    assert stub_a["name"] != stub_b["name"], (
        f"both threads got the same peer name {stub_a['name']!r}; a model could "
        "not address one without the other"
    )

    # A discovers B — and must not discover itself.
    home.client().start_turn(
        tui_a.thread_id,
        "Call the ccx MCP tool `peers_list` and reply with its output verbatim, "
        "nothing else.",
    )
    listing = tui_a.wait_for(stub_b["name"], timeout=180)
    assert "[codex]" in listing, (
        f"peers_list did not tag the peer kind, so a model cannot tell what it "
        f"is addressing:\n{listing}"
    )
    assert stub_a["name"] not in listing, (
        f"{stub_a['name']} listed itself as a peer; a thread that can address "
        f"itself will loop a turn back into itself:\n{listing}"
    )

    # Leg 1: A sends to B by name.
    home.client().start_turn(
        tui_a.thread_id,
        f"Call `peer_send` with to='{stub_b['name']}' and message exactly: "
        f"{PING}. Then say DONE.",
    )
    inbound = tui_b.wait_for(PING, timeout=180)
    assert "from Codex peer" in inbound, (
        "B was told it is talking to a Claude peer; the sender is a Codex "
        f"thread and the label must say so:\n{inbound[-800:]}"
    )
    # The provenance block corrects Claude's "Another Claude session sent a
    # message" framing. Codex has no such framing, so sending the correction
    # here would state a falsehood about the recipient's own harness — in the
    # one place added to be honest about provenance. It shipped that way and a
    # human found it in demo footage, because the original test asserted the leg
    # we were worried about instead of every leg.
    assert "[ccx provenance]" not in inbound, (
        "the Claude-specific provenance correction was sent to a Codex thread, "
        f"which is a false statement about its own harness:\n{inbound[-800:]}"
    )

    # Leg 2: B replies to the address it received, and it lands in A.
    home.client().start_turn(
        tui_b.thread_id,
        "Reply to the peer that just messaged you: call `peer_send` with `to` "
        "set to the exact address that message told you to reply to, and "
        f"message exactly: {PONG}.",
    )
    tui_a.wait_for(PONG, timeout=180)

    daemon.stop()
    tui_a.stop()
    tui_b.stop()
    home.stop()


def _wait_for_stub(scratch, thread_id, daemon, timeout=60):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        for entry in scratch.registry().values():
            if entry.get("ccxThreadId") == thread_id:
                return entry
        time.sleep(0.5)
    raise AssertionError(
        f"no stub appeared for thread {thread_id} within {timeout}s\n"
        f"registry: {[e.get('name') for e in scratch.registry().values()]}\n"
        f"--- daemon ---\n{daemon.output()}"
    )


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
