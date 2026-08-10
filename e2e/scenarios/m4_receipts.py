"""M4 — delivery receipts, relayed into the originating Codex thread.

A Codex agent whose message is parked in a Claude approval prompt sees silence
and will read it as refusal. So Claude's `peer_message_status` control frames
have to reach the thread that sent the message.

The receiving session here runs in *prompting* mode — no
`--dangerously-skip-permissions` — which is what makes an inbound peer message
hold. The harness then answers the prompt the way a user would, and the thread
must observe `held` first and `delivered` after.

Receipts are appended with `thread/inject_items`, so the assertions read the
thread's own history rather than scraping a pane: a receipt must not cost a
model turn.
"""

import json
import os
import sys

from ..bridge import Daemon
from ..codex import CodexHome, CodexTui
from ..session import ClaudeSession

OUTBOUND = "CCX-M4-NEEDS-APPROVAL"


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
    daemon.wait_for_stub()

    # Prompting mode: this session asks its user before accepting peer traffic.
    session = ClaudeSession(
        scratch, "ccx-m4-claude", ctx.token, bypass=False
    ).start()

    home.client().start_turn(
        tui.thread_id,
        "Use the ccx MCP tool `peers_list`, then `peer_send` to the Claude Code "
        f"session named '{session.name}' with the message exactly: {OUTBOUND}. "
        "Report the msg_id peer_send returned.",
    )

    # The Codex thread observes the hold. Receipts arrive as queued turns, so
    # the assertion is on the thread's own rendering, not on ccx's logging.
    tui.wait_for("[ccx] receipt HELD", timeout=180)
    assert "relayed receipt held" in daemon.output(), (
        "the stub never reported relaying the held receipt"
    )

    # The user approves, and the thread learns the outcome.
    session.answer_held_message(deliver=True)
    tui.wait_for("[ccx] receipt DELIVERED", timeout=180)
    assert "relayed receipt delivered" in daemon.output(), (
        "the stub never reported relaying the delivered receipt"
    )

    # And the message itself actually reached the session once approved.
    session.wait_for_transcript(
        lambda r: OUTBOUND in json.dumps(r) and "cross-session-message" in json.dumps(r),
        timeout=120,
        what=f"{OUTBOUND} delivered after approval",
    )

    daemon.stop()
    session.stop()
    tui.stop()
    home.stop()


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
