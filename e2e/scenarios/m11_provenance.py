"""M11 — the receiving model must know it is not talking to Claude.

The README claimed the sender "is stamped as Codex rather than left to read as
another Claude session". A rehearsal falsified it: a Claude session received a
message stamped `from-name="codex:5255bc"` and said out loud that it had
"received a cross-session message from another Claude session".

The envelope was already correct and the outcome was still wrong, which is why
this scenario asserts what the receiving model *says* rather than what the
envelope contains. Anything weaker passes while the claim is false.

Root cause, captured from a real transcript: Claude Code's own harness prefixes
inbound peer messages with "Another Claude session sent a message:" in the
content. No attribute we set can outrank a sentence in the prompt, so the
provenance has to be in the body, where the model actually reads.
"""

import json
import os
import sys

from ..bridge import Daemon
from ..codex import CodexHome, CodexTui
from ..session import ClaudeSession

QUESTION = (
    "Question about the SENDER of this message, not about you: is the sender a "
    "Claude Code session or a Codex CLI thread? Answer with exactly one word."
)


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
    tui = CodexTui(home, name="prov", thread_name="prov").start()
    daemon = Daemon(scratch, home).start()
    daemon.wait_for_stub()

    session = ClaudeSession(scratch, "ccx-m11-claude", ctx.token).start()

    home.client().start_turn(
        tui.thread_id,
        f"Call `peer_send` to '{session.name}' with this exact message: {QUESTION}",
    )

    answer = _wait_for_answer(session)
    assert "codex" in answer.lower(), (
        "the receiving model does not know it is talking to Codex. It said:\n"
        f"{answer}\n"
        "The envelope carries from-name=\"codex:…\", but Claude's own harness "
        "tells the model 'Another Claude session sent a message', and an "
        "attribute cannot outrank a sentence in the prompt."
    )

    daemon.stop()
    session.stop()
    tui.stop()
    home.stop()


def _wait_for_answer(session, timeout=180):
    """The assistant's own words, not the harness's framing.

    Scoped to assistant records on purpose: the inbound user record contains
    Claude's "Another Claude session sent a message" preamble, so a whole-
    transcript search would find whatever it went looking for.
    """
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        for record in reversed(session.transcript()):
            if record.get("type") != "assistant":
                continue
            text = _text_of(record)
            if "codex" in text.lower() or "claude" in text.lower():
                return text
        time.sleep(2)
    raise AssertionError(
        f"{session.name} never said anything about the sender within {timeout}s"
    )


def _text_of(record):
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return json.dumps(record)


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
