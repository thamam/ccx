"""M3 — the round trip, which is the whole point of the project.

A Codex agent calls `peer_send` to a Claude session; that session receives a
`<cross-session-message>`; it replies by copying `from` into `to`; the reply
lands in the *same* Codex thread. Both transcripts are asserted on.

The reply address is never routed or looked up — it is the stub socket on both
legs, so "copy `from` into `to`" is the entire reply protocol.
"""

import json
import os
import re
import sys

from ..bridge import Daemon
from ..codex import CodexHome, CodexTui
from ..session import ClaudeSession

OUTBOUND = "CCX-M3-PING-FROM-CODEX"
REPLY = "CCX-M3-PONG-FROM-CLAUDE"


def run(ctx):
    scratch = ctx.scratch
    home = CodexHome().create()
    home.add_mcp_server(
        "ccx",
        command=sys.executable,
        args=["-m", "ccx.mcp"],
        env={
            "PYTHONPATH": _repo_root(),
            # The MCP server reads and writes the same scratch registry the
            # test's Claude session and stubs use.
            "CLAUDE_CONFIG_DIR": scratch.config,
            "CLAUDE_CODE_TMPDIR": scratch.run,
        },
    )
    home.start_daemon()
    tui = CodexTui(home).start()

    daemon = Daemon(scratch, home).start()
    stub = daemon.wait_for_stub()

    session = ClaudeSession(scratch, "ccx-m3-claude", ctx.token).start()

    # Tell the Claude session how to reply before anything arrives, so the reply
    # is a deliberate act rather than a lucky improvisation.
    session.prompt(
        "You will receive a <cross-session-message> from a Codex peer. When it "
        f"arrives, reply by calling SendMessage with `to` set to the exact value "
        f"of the message's `from` attribute, and the body exactly: {REPLY}. "
        "Acknowledge this instruction in one short line and then wait."
    )
    session.wait_for_transcript(
        lambda r: _mentions(r, "wait") or _mentions(r, "Acknowledged"),
        timeout=120,
        what="an acknowledgement of the reply instruction",
    )

    # Codex initiates, through the MCP server, addressing the Claude session by
    # the name it discovers via peers_list.
    home.client().start_turn(
        tui.thread_id,
        "Use the ccx MCP tool `peers_list` to find the Claude Code session named "
        f"'{session.name}', then call `peer_send` to it with the message "
        f"exactly: {OUTBOUND}. Report what peer_send returned.",
    )

    # Leg 1: it arrives at the Claude session as a real envelope, stamped as
    # Codex rather than as another Claude session.
    inbound = session.wait_for_transcript(
        lambda r: _mentions(r, OUTBOUND) and _mentions(r, "cross-session-message"),
        timeout=180,
        what=f"{OUTBOUND} as a cross-session-message",
    )
    blob = json.dumps(inbound)
    sent_from = _from_attr(blob)
    # Compare against the registry as it stands now: the bridge may have
    # respawned the stub, and what matters is that the address it stamped
    # belongs to a live stub for *this* thread.
    current = {
        e["messagingSocketPath"]: e
        for e in scratch.registry().values()
        if e.get("ccxThreadId") == tui.thread_id
    }
    assert sent_from, f"no `from` attribute in the envelope:\n{blob[:1200]}"
    assert sent_from.removeprefix("uds:") in current, (
        f"reply address {sent_from} is not a live stub for thread "
        f"{tui.thread_id}; stubs are {sorted(current)}"
    )
    assert re.search(r'from-name=\\"codex:', blob), (
        "the message is not stamped as coming from Codex; the receiving model "
        f"cannot tell it apart from a Claude peer.\n{blob[:1200]}"
    )

    # Leg 2: the reply lands back in the same Codex thread.
    pane = tui.wait_for(REPLY, timeout=180)
    assert OUTBOUND in pane, "the Codex thread lost its own outbound turn"

    daemon.stop()
    session.stop()
    tui.stop()
    home.stop()


def _from_attr(blob):
    match = re.search(r'from=\\"([^\\"]+)\\"', blob)
    return match.group(1) if match else None


def _mentions(record, needle):
    return needle in json.dumps(record)


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
