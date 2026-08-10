"""Acceptance scenarios, one per milestone. Order is execution order."""

from collections import namedtuple

from . import (
    m0_isolation,
    m1_codex_inject,
    m2_claude_sees_codex,
    m3_round_trip,
    m4_receipts,
    m5_hardening,
    m6_conversation,
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
    Scenario(
        "m4-receipts",
        "a held message reports held, then delivered, into the Codex thread",
        m4_receipts.run,
    ),
    Scenario(
        "m5-hardening",
        "doctor agrees with reality and an unreachable peer errors, not succeeds",
        m5_hardening.run,
    ),
    Scenario(
        "m6-conversation",
        "the definition of done: a multi-turn conversation in both directions",
        m6_conversation.run,
    ),
]
