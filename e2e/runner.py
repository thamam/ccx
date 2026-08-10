"""`ccx e2e` — run the full-system acceptance suite and exit non-zero on failure.

Every scenario gets a fresh scratch environment and a before/after snapshot of
the user's real surfaces. A scenario that leaves anything behind fails, even if
its own assertions passed.
"""

import time
import traceback

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

    failures = []
    for scenario in selected:
        print(f"\n=== {scenario.name} — {scenario.summary}")
        before = scratch_mod.real_snapshot()
        scratch = scratch_mod.Scratch().create()
        started = time.time()
        try:
            scenario.run(Context(scratch, token))
            _assert_clean(scratch, before)
            print(f"--- PASS {scenario.name} ({time.time() - started:.1f}s)")
        except Exception as exc:  # noqa: BLE001 — a scenario may fail any way
            failures.append((scenario.name, exc))
            print(f"--- FAIL {scenario.name} ({time.time() - started:.1f}s)")
            print(_indent(traceback.format_exc()))
        finally:
            if not keep:
                scratch.destroy()
            else:
                print(f"    (kept scratch at {scratch.root})")

    print()
    if failures:
        print(f"FAILED {len(failures)}/{len(selected)}: {[n for n, _ in failures]}")
        return 1
    print(f"PASSED {len(selected)}/{len(selected)}")
    return 0


def _assert_clean(scratch, before):
    """No residue in the scratch env, nothing new in the user's real dirs."""
    left = scratch.leftovers()
    if any(left.values()):
        raise AssertionError(f"scratch artifacts survived teardown: {left}")
    added = scratch_mod.diff_snapshot(before, scratch_mod.real_snapshot())
    if added:
        raise AssertionError(f"leaked into the user's real environment: {added}")


def _indent(text, prefix="    "):
    return "".join(prefix + line for line in text.splitlines(keepends=True))
