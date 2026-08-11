"""M10 — two agents that acknowledge each other must eventually stop.

Found live during a demo rehearsal, not by the suite: vega and orion traded
ACK-FROM-VEGA / ACK-FROM-CODEX five rounds and were not going to stop. The stub
told every recipient how to reply, and two agents that both obey that instruct
each other forever. Claude survives this only because it drops a message
identical to the previous one from the same sender; Codex has no such guard.

The fix is the `hop-chain` the envelope has always specified and ccx never set.
This asserts the bound, not a round count: the exchange must *terminate*, and
the refusal must be visible to the agent rather than dying quietly.

Both agents here are told to acknowledge whatever arrives, which is the polite
behaviour that caused the loop. Prompting them to be less chatty would hide the
bug rather than fix it.
"""

import os
import sys
import time

from ..bridge import Daemon
from ..codex import CodexHome, CodexTui

ACK = "ACK-M10"
SETTLE = 25.0


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

    a = CodexTui(home, name="loop-a", thread_name="loop-a").start()
    b = CodexTui(home, name="loop-b", thread_name="loop-b").start()
    daemon = Daemon(scratch, home).start()
    _wait_for_stubs(scratch, {a.thread_id, b.thread_id}, daemon)

    standing = (
        "Standing instruction for this session: whenever a peer message "
        f"arrives, immediately reply to its reply address with exactly {ACK}. "
        "Acknowledge this instruction in one word."
    )
    home.client().start_turn(a.thread_id, standing)
    home.client().start_turn(b.thread_id, standing)
    time.sleep(8)

    # Kick it off, then let it run and see whether it stops on its own.
    home.client().start_turn(
        a.thread_id,
        f"Use `peer_send` to send loop-b exactly: {ACK}",
    )

    counts = _settle(a, b)
    assert counts is not None, (
        "the acknowledgement exchange never stopped — the hop chain is not "
        "bounding it"
    )
    rounds_a, rounds_b = counts

    # It must have actually happened, or this proves nothing.
    assert rounds_a + rounds_b >= 2, (
        f"the exchange never got going ({rounds_a}, {rounds_b}); this would "
        "pass even with the bug present"
    )

    daemon.stop()
    a.stop()
    b.stop()
    home.stop()


def _settle(a, b, timeout=300):
    """Return the final counts once both panes stop changing, else None."""
    last, stable_since = None, time.time()
    deadline = time.time() + timeout
    while time.time() < deadline:
        now = (a.pane().count(ACK), b.pane().count(ACK))
        if now != last:
            last, stable_since = now, time.time()
        elif time.time() - stable_since >= SETTLE:
            return last
        time.sleep(2)
    return None


def _wait_for_stubs(scratch, thread_ids, daemon, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        have = {
            e.get("ccxThreadId")
            for e in scratch.registry().values()
            if e.get("ccxStub")
        }
        if thread_ids <= have:
            return
        time.sleep(0.5)
    raise AssertionError(
        f"stubs for {sorted(thread_ids)} never registered\n{daemon.output()}"
    )


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
