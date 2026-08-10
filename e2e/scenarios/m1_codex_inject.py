"""M1 — the Codex transport.

Starts an isolated app-server daemon, attaches a real `codex --remote` TUI to
it, injects a user turn over the WebSocket JSON-RPC control socket, and asserts
the answer renders in the TUI. Nothing is mocked: a real model answers.

Also asserts the unreachable-peer contract, because a silent success there is
how messages vanish.
"""

from ccx import codexrpc
from ..codex import CodexHome, CodexTui


def run(ctx):
    home = CodexHome().create().start_daemon()
    assert home.started_daemon, "the harness must own the daemon it stops"

    client = home.client()
    assert client.info, "initialize returned nothing"

    tui = CodexTui(home).start()
    assert tui.thread_id in client.list_threads(), (
        "the attached TUI's thread is missing from thread/loaded/list"
    )

    # Unreachable peers must error, never succeed silently.
    bogus = "00000000-0000-0000-0000-000000000000"
    try:
        codexrpc.require_thread(client, bogus)
        raise AssertionError("require_thread accepted a thread that is not attached")
    except codexrpc.CodexError as exc:
        assert "not attached" in str(exc), f"unhelpful error text: {exc}"

    codexrpc.require_thread(client, tui.thread_id)
    client.start_turn(tui.thread_id, "Reply with exactly one word: PONG")
    pane = tui.wait_for("PONG")

    # The injected text must render as a user turn, not just as our own echo.
    assert "Reply with exactly one word: PONG" in pane, (
        f"injected prompt never rendered in the TUI\n{pane}"
    )

    client.close()
    tui.stop()
    home.stop()
