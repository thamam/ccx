"""Acceptance scenarios, one per milestone. Order is execution order."""

from collections import namedtuple

from . import m0_isolation, m1_codex_inject

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
]
