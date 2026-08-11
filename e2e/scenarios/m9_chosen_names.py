"""M9 — Codex threads can be given names a human chose.

`codex-cccdx-messaging-9c15ca` is a correct name and a useless one: nobody
addresses a peer they cannot say out loud. `ccx codex --name vega` sets the
name on the thread itself through the app-server's `thread/name/set`, so every
client sees the same value and nothing has to correlate environment variables
to threads.

The interesting case is the collision. Human-chosen names repeat, and a name
that silently routes to one of two threads is worse than an ugly unique one, so
the bridge makes them unique on the way in: the later claimant of a taken name
gets the short thread id appended, and the first keeps the clean name.
"""

import os
import sys

from ..bridge import Daemon
from ..codex import CodexHome, CodexTui

PING = "CCX-M9-BY-NAME"


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

    vega = CodexTui(home, name="vega", thread_name="vega").start()
    orion = CodexTui(home, name="orion", thread_name="orion").start()

    # Both threads exist before the bridge does, so the bridge sees them in a
    # single poll cycle. That is the case that matters: a stub registers
    # asynchronously, so two threads claiming the same name in one pass would
    # each read an empty registry and both take it. Found live, on screen.
    twin = CodexTui(home, name="twin", thread_name="vega").start()

    daemon = Daemon(scratch, home).start()
    names = _wait_for_names(
        scratch, {vega.thread_id, orion.thread_id, twin.thread_id}, daemon
    )
    assert len(set(names.values())) == 3, (
        f"two threads were given the same peer name in one poll cycle: {names}"
    )
    assert sorted(names[t] for t in (vega.thread_id, twin.thread_id))[0] == "vega", (
        f"neither same-cycle claimant kept the clean name: {names}"
    )

    assert names[orion.thread_id] == "orion", (
        f"chosen name was not honoured: {names[orion.thread_id]!r}"
    )

    # Address whichever thread actually holds the clean name: within one poll
    # cycle either claimant may win, and the test must not assume which.
    by_thread = {vega.thread_id: vega, twin.thread_id: twin}
    holder = by_thread[next(t for t in by_thread if names[t] == "vega")]

    home.client().start_turn(
        orion.thread_id,
        f"Call the ccx MCP tool `peer_send` with to='vega' and message exactly: "
        f"{PING}. Then say DONE.",
    )
    holder.wait_for(PING, timeout=180)

    # The other collision shape: a thread claiming a name already registered,
    # arriving in a later poll cycle than the incumbent.
    clash = CodexTui(home, name="clash", thread_name="orion").start()
    all_names = _wait_for_names(
        scratch,
        {vega.thread_id, orion.thread_id, twin.thread_id, clash.thread_id},
        daemon,
    )
    clash_name = all_names[clash.thread_id]
    assert clash_name != "orion", (
        "a second thread took the name `orion`; addressing it would route to "
        "one of two threads at random"
    )
    assert clash_name.startswith("orion-") and clash_name.endswith(
        clash.thread_id[-6:]
    ), f"expected the later claimant to be suffixed, got {clash_name!r}"
    assert all_names[orion.thread_id] == "orion", (
        "the first claimant lost its name to the second"
    )
    assert len(set(all_names.values())) == 4, f"peer names are not unique: {all_names}"

    daemon.stop()
    vega.stop()
    orion.stop()
    twin.stop()
    clash.stop()
    home.stop()


def _wait_for_names(scratch, thread_ids, daemon, timeout=60):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        found = {
            entry["ccxThreadId"]: entry["name"]
            for entry in scratch.registry().values()
            if entry.get("ccxThreadId") in thread_ids
        }
        if len(found) == len(thread_ids):
            return found
        time.sleep(0.5)
    raise AssertionError(
        f"stubs for {sorted(thread_ids)} did not all register within {timeout}s\n"
        f"registry: {[e.get('name') for e in scratch.registry().values()]}\n"
        f"--- daemon ---\n{daemon.output()}"
    )


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
