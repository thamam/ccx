"""M5 — the two things that keep this maintainable and honest.

`ccx doctor` is the version-drift alarm: neither surface ccx stands on is
public, so the plan is to detect a moved contract loudly rather than to hope
nothing moves. It runs here against the scratch environment so a green suite
also means doctor agrees with reality.

Then the unreachable-peer contract. Addressing a session that is gone must
surface as an error inside the Codex thread. A silent success is the one
failure mode that makes the whole bridge untrustworthy — the agent believes it
has spoken and it has not.
"""

import os
import subprocess
import sys

from ..bridge import Daemon
from ..codex import CodexHome, CodexTui
from ..scratch import ENV_DENY
from ..session import ClaudeSession

FAILED = "PEER-SEND-FAILED-AS-EXPECTED"


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

    session = ClaudeSession(scratch, "ccx-m5-claude", ctx.token).start()
    dead_address = f"uds:{session.socket_path}"

    _doctor_agrees(scratch, home)
    _ccx_codex_attaches(home)

    # The peer goes away. Its address stays valid-looking, which is exactly the
    # case that must not silently succeed.
    session.stop()

    home.client().start_turn(
        tui.thread_id,
        f"Call the ccx MCP tool `peer_send` with to='{dead_address}' and "
        "message='knock knock'. It is expected to fail. If it fails, reply with "
        f"exactly {FAILED} and nothing else. If it succeeds, reply with "
        "SENT and nothing else.",
    )
    pane = tui.wait_for(FAILED, timeout=180)
    assert "SENT" not in pane.split(FAILED)[-1], (
        "peer_send reported success for a peer that no longer exists"
    )

    daemon.stop()
    tui.stop()
    home.stop()


def _doctor_agrees(scratch, home):
    """`ccx doctor` must pass against the same environment the suite drives."""
    env = {k: v for k, v in os.environ.items() if k not in ENV_DENY}
    env["CLAUDE_CONFIG_DIR"] = scratch.config
    env["CLAUDE_CODE_TMPDIR"] = scratch.run
    proc = subprocess.run(
        [sys.executable, "-m", "ccx", "doctor", "--codex-home", home.root],
        cwd=_repo_root(),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    report = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"ccx doctor failed:\n{report}"
    for contract in ("claude-socket", "claude-registry", "codex-rpc", "mcp-meta"):
        assert contract in report, f"doctor did not check {contract}:\n{report}"
    assert "FAIL" not in report, f"doctor reported drift:\n{report}"


def _ccx_codex_attaches(home):
    """`ccx codex` is the documented per-session requirement, so it is tested.

    A session launched any other way can send but cannot be reached, and that
    asymmetry is the single launch-time cost of the whole design.
    """
    import time

    from ..session import register_cleanup, tmux

    class _Tmux:
        def __init__(self, name):
            self.name = name

        def stop(self, quiet=False):
            subprocess.run(
                ["tmux", "kill-session", "-t", self.name], capture_output=True
            )
            return {}

    name = "ccx-m5-ccxcodex"
    before = set(home.client().list_threads())
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
    register_cleanup(_Tmux(name))
    tmux(
        # cwd is the repo so `-m ccx` resolves; CODEX_HOME points the launched
        # session at the scratch daemon rather than the user's.
        "new-session", "-d", "-s", name, "-x", "200", "-y", "50",
        "-c", _repo_root(),
        f"CODEX_HOME={home.root} {sys.executable} -m ccx codex",
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        new = set(home.client().list_threads()) - before
        if new:
            return sorted(new)[0]
        time.sleep(1)
    pane = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", name], capture_output=True, text=True
    ).stdout
    raise AssertionError(
        f"`ccx codex` did not produce a daemon-attached thread within 60s\n{pane}"
    )


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
