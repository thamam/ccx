"""Acceptance scenarios, one per milestone. Order is execution order."""

from collections import namedtuple

from . import (
    m0_isolation,
    m1_codex_inject,
    m2_claude_sees_codex,
    m3_round_trip,
)

Scenario = namedtuple("Scenario", "name summary run")

SCENARIOS = [
    Scenario(
        "m0-isolation",
        "an isolated Claude session registers, then vanishes without trace",
        m0_isolation.run,
    ),
    Scenario(
        "m1-codex-inject",
        "a turn injected over the app-server socket renders in a real Codex TUI",
        m1_codex_inject.run,
    ),
    Scenario(
        "m2-claude-sees-codex",
        "a Claude session lists a Codex thread as a peer and messages it",
        m2_claude_sees_codex.run,
    ),
    Scenario(
        "m3-round-trip",
        "Codex messages a Claude session and the reply lands in the same thread",
        m3_round_trip.run,
    ),
]
