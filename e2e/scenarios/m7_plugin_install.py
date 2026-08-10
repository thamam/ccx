"""M7 — install ccx the way a user does, and prove the install works.

Two `plugin marketplace add` + `plugin install` runs, one per harness, into
scratch homes. Then the two things that make the install real:

- Codex: the app-server has actually negotiated `peers_list` and `peer_send`.
  That proves the plugin's `.mcp.json` was resolved, the server launched, and
  it initialized — none of which a manifest-parses check would catch.
- Claude: the SessionStart hook alone brings the bridge up. Nothing in the
  scenario starts `ccx daemon`; if a stub appears for the Codex thread, the
  hook did its job.

An install path that is not exercised is not shipped.
"""

from ..codex import CodexHome, CodexTui
from ..plugin import codex_mcp_tools, install_into_claude, install_into_codex
from ..session import ClaudeSession

EXPECTED_TOOLS = ["peer_send", "peers_list"]


def run(ctx):
    scratch = ctx.scratch
    home = CodexHome().create()

    # -- Codex side ------------------------------------------------------
    install_into_codex(home)
    home.start_daemon()
    tools = codex_mcp_tools(home)
    assert tools == EXPECTED_TOOLS, (
        f"the plugin's MCP server came up with the wrong tool surface: {tools}"
    )

    tui = CodexTui(home).start()

    # -- Claude side -----------------------------------------------------
    install_into_claude(scratch)

    # The session inherits CODEX_HOME so the hook's daemon watches the scratch
    # app-server rather than the user's. Nothing else starts the bridge.
    session = ClaudeSession(
        scratch, "ccx-m7-claude", ctx.token, extra_env={"CODEX_HOME": home.root}
    ).start()

    stub = _wait_for_stub(scratch, tui.thread_id)
    assert stub["name"].endswith(tui.thread_id[-6:]), (
        f"the hook-started bridge published the wrong stub: {stub['name']}"
    )
    assert stub["messagingSocketPath"].startswith(scratch.socks), (
        f"stub socket escaped the scratch dir: {stub['messagingSocketPath']}"
    )

    session.stop()
    _stop_hook_daemon(scratch, home)
    tui.stop()
    home.stop()


def _wait_for_stub(scratch, thread_id, timeout=90):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        for entry in scratch.registry().values():
            if entry.get("ccxThreadId") == thread_id:
                return entry
        time.sleep(1)
    raise AssertionError(
        f"the SessionStart hook never brought up a stub for {thread_id} within "
        f"{timeout}s. Registry: "
        f"{[e.get('name') for e in scratch.registry().values()]}\n"
        f"hook log: {_hook_log()}"
    )


def _hook_log():
    import os

    path = os.path.join(os.environ.get("TMPDIR", "/tmp"), "ccx-daemon.log")
    try:
        with open(path) as f:
            return f.read()[-1500:]
    except OSError:
        return f"(no log at {path})"


def _stop_hook_daemon(scratch, home):
    """Stop the hook's daemon, which is not a child of the harness.

    Identified by the lock file it holds — that path is derived from the
    scratch config dir, so it can only ever be our daemon. Matching on the
    command line instead would risk killing the user's own bridge.
    """
    import os
    import signal
    import subprocess
    import time

    key = scratch.config.replace(os.sep, "_").strip("_")
    lock = os.path.join(scratch.run, f"ccx-daemon-{key}.lock")
    pids = subprocess.run(
        ["lsof", "-t", lock], capture_output=True, text=True
    ).stdout.split()
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    time.sleep(2)
    return pids
