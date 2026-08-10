"""Acceptance scenarios, one per milestone. Order is execution order."""

from collections import namedtuple

from . import m0_isolation

Scenario = namedtuple("Scenario", "name summary run")

SCENARIOS = [
    Scenario(
        "m0-isolation",
        "an isolated Claude session registers, then vanishes without trace",
        m0_isolation.run,
    ),
]
