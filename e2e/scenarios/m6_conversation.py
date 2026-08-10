"""The definition of done: a multi-turn conversation, in both directions.

Three messages, each addressed by the address the sender received rather than
by anything configured:

    Claude  --1-->  Codex      (SendMessage to the stub)
    Codex   --2-->  Claude     (peer_send, reply address = the same stub)
    Claude  --3-->  Codex      (copy `from` into `to`)

Turn 3 is the one that matters. It proves the reply address survives a full
round trip — Claude is replying to an address it learned from a Codex message
that was itself a reply, with no routing table anywhere in the system.
"""

import json
import os
import sys

from ..bridge import Daemon
from ..codex import CodexHome, CodexTui
from ..session import ClaudeSession

ONE = "CCX-CONV-1-CLAUDE-TO-CODEX"
TWO = "CCX-CONV-2-CODEX-TO-CLAUDE"
THREE = "CCX-CONV-3-CLAUDE-TO-CODEX"


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
    tui = CodexTui(home).start()
    daemon = Daemon(scratch, home).start()
    stub = daemon.wait_for_stub()

    session = ClaudeSession(scratch, "ccx-conv-claude", ctx.token).start()

    session.prompt(
        "You are in a two-way conversation with a Codex agent.\n"
        f"1. Send this to the agent named '{stub['name']}' with SendMessage, "
        f"exactly: {ONE} — reply to me with peer_send and the body {TWO}. "
        "If SendMessage asks for a [ref], re-send with the ref it gives you.\n"
        f"2. When its reply arrives, send one more message back to the exact "
        f"`from` address on that reply, with the body exactly: {THREE}.\n"
        "Do nothing else."
    )

    # Turn 1 — Claude reaches the Codex thread.
    tui.wait_for(ONE, timeout=180)

    # Turn 2 — Codex replies through the MCP server, stamped as Codex.
    # The marker also appears in the harness's own instruction to this session,
    # so the record has to carry the Codex stamp as well to count as the reply.
    session.wait_for_transcript(
        lambda r: TWO in json.dumps(r) and 'from-name=\\"codex:' in json.dumps(r),
        timeout=240,
        what=f"{TWO} back from the Codex agent, stamped from-name=codex:",
    )

    # Turn 3 — Claude answers the reply, addressing it by what it received.
    tui.wait_for(THREE, timeout=240)

    daemon.stop()
    session.stop()
    tui.stop()
    home.stop()


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
