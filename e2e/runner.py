"""`ccx e2e` — run the full-system acceptance suite and exit non-zero on failure.

Every scenario gets a fresh scratch environment and a before/after snapshot of
the user's real surfaces. A scenario that leaves anything behind fails, even if
its own assertions passed.
"""

import os
import time
import traceback

from ccx import codexrpc
from . import creds, scratch as scratch_mod
from .scenarios import SCENARIOS


class Context:
    """What a scenario is handed: a scratch env and a live OAuth token."""

    def __init__(self, scratch, token):
        self.scratch = scratch
        self.token = token


def run(only=None, keep=False):
    token = creds.oauth_token()
    selected = [s for s in SCENARIOS if not only or s.name in only]
    if only:
        unknown = set(only) - {s.name for s in SCENARIOS}
        if unknown:
            print(f"unknown scenario(s): {sorted(unknown)}")
            return 2

    # Residue from an earlier run is not this run's leak, and letting it fail
    # the first scenario points the reader at the wrong culprit. Cleared out
    # loud rather than silently: if something is leaking, the previous run said
    # so at its own end.
    stale = scratch_mod.surviving_roots()
    if stale and not keep:
        print(f"clearing scratch roots left by an earlier run: {stale}")
        for root in stale:
            scratch_mod.remove_settled(root)

    failures = []
    for scenario in selected:
        print(f"\n=== {scenario.name} — {scenario.summary}")
        before = scratch_mod.real_snapshot()
        before["codex"] = _codex_snapshot()
        scratch = scratch_mod.Scratch().create()
        started = time.time()
        try:
            scenario.run(Context(scratch, token))
            _assert_clean(scratch, before)
            if not keep:
                # Teardown is part of the scenario's verdict, not an epilogue:
                # "leaves nothing behind" is a guarantee the suite makes, so an
                # empty scratch root that outlives the run is a failure.
                scratch.destroy()
                surviving = scratch_mod.surviving_roots()
                if surviving:
                    raise AssertionError(f"scratch roots survived teardown: {surviving}")
            print(f"--- PASS {scenario.name} ({time.time() - started:.1f}s)")
        except Exception as exc:  # noqa: BLE001 — a scenario may fail any way
            failures.append((scenario.name, exc))
            print(f"--- FAIL {scenario.name} ({time.time() - started:.1f}s)")
            print(_indent(traceback.format_exc()))
        finally:
            if keep:
                print(f"    (kept scratch at {scratch.root})")
            else:
                scratch.destroy()

    if not keep:
        _final_sweep(failures)

    print()
    if failures:
        print(f"FAILED {len(failures)}/{len(selected)}: {[n for n, _ in failures]}")
        return 1
    print(f"PASSED {len(selected)}/{len(selected)}")
    return 0


def _final_sweep(failures, settle=25.0):
    """The guarantee is per *run*, so it is settled and checked once at the end.

    `codex plugin …` kicks off a background remote-catalog fetch that writes
    into CODEX_HOME well after the scenario has torn down — sometimes after the
    whole suite. Each scenario still asserts its own cleanliness; this makes the
    end state deterministic rather than a race against a detached writer.
    """
    surviving = scratch_mod.surviving_roots()
    if not surviving:
        return
    print(f"\n--- sweeping late scratch roots: {surviving}")
    for root in surviving:
        scratch_mod.remove_settled(root, settle=settle)
    still = scratch_mod.surviving_roots()
    if still:
        failures.append(
            ("final-sweep", AssertionError(f"scratch roots survived the run: {still}"))
        )
        print(f"--- FAIL final-sweep: {still}")


def _assert_clean(scratch, before):
    """No residue in the scratch env, nothing new in the user's real dirs."""
    left = scratch.leftovers()
    if any(left.values()):
        raise AssertionError(f"scratch artifacts survived teardown: {left}")
    added = scratch_mod.diff_snapshot(before, scratch_mod.real_snapshot())
    if added:
        raise AssertionError(f"leaked into the user's real environment: {added}")
    now = _codex_snapshot()
    if now != before["codex"]:
        raise AssertionError(
            f"the user's Codex daemon state changed: {before['codex']} -> {now}. "
            "The harness must never start or stop a daemon it does not own."
        )


def _codex_snapshot():
    """Whether the *user's* app-server daemon is up. Must not change across a run."""
    return os.path.exists(
        os.path.join(
            codexrpc.DEFAULT_CODEX_HOME, "app-server-control", "app-server-control.sock"
        )
    )


def _indent(text, prefix="    "):
    return "".join(prefix + line for line in text.splitlines(keepends=True))
