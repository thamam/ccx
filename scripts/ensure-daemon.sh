#!/bin/sh
# SessionStart: make sure the ccx bridge daemon is alive.
#
# Claude needs no ccx code of its own — Codex threads become peers because the
# daemon publishes a stub for each one. So the plugin's whole job on this side
# is keeping that daemon running.
#
# Three rules this script must never break, in order of importance:
#   1. It must not hang. A hook that stalls session start is worse than no hook,
#      so everything here is a fast check and a detached spawn, and every path
#      exits 0.
#   2. It must not start a second daemon FOR THE SAME REGISTRY. Two bridges over
#      one registry means every Codex peer appears twice. The guard is an flock
#      inside the daemon, scoped to the registry it serves.
#
#      There used to be a `pgrep -f ccx.bridged` here to skip the spawn when a
#      bridge was already running. It was wrong: a bridge is per CLAUDE_CONFIG_DIR,
#      and pgrep cannot see which registry a process serves — the config dir
#      arrives by environment, not on the command line. So a session with its own
#      config dir would find someone else's bridge, skip its own, and never get
#      any peers. Found when a demo bridge silently suppressed the one an e2e
#      scenario needed. Always spawn; let the flock decide.
#   3. A missing python3 is logged, not surfaced. The user's session opens
#      normally and the log says why messaging is not available.
set -u

root="${CLAUDE_PLUGIN_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
log="${TMPDIR:-/tmp}/ccx-daemon.log"
stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "?")

if ! command -v python3 >/dev/null 2>&1; then
  echo "$stamp ccx: python3 not found; cross-harness messaging is off" >> "$log" 2>/dev/null
  exit 0
fi

cd "$root" 2>/dev/null || exit 0
# Detached, output to the log, never inheriting the session's stdio.
nohup python3 -m ccx.bridged >> "$log" 2>&1 < /dev/null &
echo "$stamp ccx: started bridge daemon (pid $!) from $root" >> "$log" 2>/dev/null
exit 0
