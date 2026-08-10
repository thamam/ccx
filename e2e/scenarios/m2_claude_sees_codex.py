"""M2 — a Codex thread is an ordinary peer in a Claude session.

The full five-step acceptance from PLAN.md, with real processes throughout:
a Codex TUI attached to an isolated daemon, `ccx daemon` maintaining a stub for
its thread, a real Claude session that lists that stub via ListAgents and sends
to it, the text arriving in the Codex TUI as a user turn — and then the daemon
dying without leaving a socket or a registry file behind.

Step 5 is not decoration. A leaked registry file puts a dead peer in the user's
real ListAgents.
"""

import json

from ..bridge import Daemon, stub_socket_leftovers
from ..codex import CodexHome, CodexTui
from ..session import ClaudeSession

MARKER = "CCX-M2-DELIVERED"


def run(ctx):
    scratch = ctx.scratch
    home = CodexHome().create().start_daemon()
    tui = CodexTui(home).start()

    daemon = Daemon(scratch, home).start()
    stub = daemon.wait_for_stub()

    # The stub must look like a peer, not merely exist.
    assert stub["name"].startswith("codex-"), f"stub name {stub['name']!r}"
    assert stub["name"].endswith(tui.thread_id[-6:]), (
        f"stub name {stub['name']!r} does not carry thread {tui.thread_id}"
    )
    assert stub["pid"] != 0 and stub["peerProtocol"] == 1, stub
    assert stub["messagingSocketPath"].startswith(scratch.socks), (
        f"stub socket escaped the scratch dir: {stub['messagingSocketPath']}"
    )

    session = ClaudeSession(scratch, "ccx-m2-claude", ctx.token).start()

    # Step 3 and 4, done by a real Claude session using its real tools.
    session.prompt(
        "Call ListAgents. Then use SendMessage to send exactly this text to the "
        f"agent whose name starts with 'codex-': {MARKER}. "
        "If SendMessage asks you to confirm with a [ref], re-send using the ref "
        "it gives you. Do not do anything else."
    )

    # Step 3: the stub is discoverable through Claude's own listing. The name
    # and the ListAgents call land in different transcript records, so they are
    # two separate waits rather than one conjunction.
    session.wait_for_transcript(
        lambda r: _mentions(r, "ListAgents"), what="a ListAgents call"
    )
    session.wait_for_transcript(
        lambda r: _mentions(r, stub["name"]),
        what=f"{stub['name']} in the peer listing",
    )

    # Step 4 lands where it matters: in the Codex thread.
    pane = tui.wait_for(MARKER, timeout=120)
    assert MARKER in pane

    # Step 5 — the part that protects the user's real environment.
    daemon.stop()
    assert daemon.wait_for_no_stubs(), (
        f"stub registry entries survived the daemon: {sorted(daemon.stubs())}"
    )
    stale = stub_socket_leftovers(scratch)
    assert not stale, f"dead sockets left behind: {stale}"

    session.stop()
    tui.stop()
    home.stop()


def _mentions(record, needle):
    return needle in json.dumps(record)
