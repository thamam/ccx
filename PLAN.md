# ccx — execution plan

Build a bridge that makes **Codex sessions and Claude Code sessions first-class
message peers**, without patching either harness.

Design was validated at the protocol level on 2026-08-10 — all seven links were
executed live, not inferred. **Nothing has been built yet.**

**Read `docs/protocol-notes.md` first and in full.** It contains every wire
format, socket path and RPC method you need, all verified. Do not re-derive them
from the binaries; that took hours and is already done.

Reference implementation of the trickiest piece (WebSocket-over-UDS JSON-RPC
client for the Codex daemon) is already written and verified:
`reference/wsrpc.py`.

---

## 1. What we're building

One user-level daemon, `ccx`, contributing exactly two things:

**A stub process per live Codex thread.** Owns a real pid, binds a socket in
`/tmp/cc-socks/`, and writes `~/.claude/sessions/<pid>.json`. That makes a Codex
session an ordinary peer in Claude's world — it appears in `ListAgents`, has a
name, and is addressable. Anything written to the stub socket is unwrapped and
pushed into the Codex thread via `turn/start`.

**An MCP server.** Registered once in Codex config. Gives Codex agents
`peers_list` and `peer_send`, and reads the caller's `thread_id` from
`_meta.x-codex-turn-metadata` so outbound messages carry the correct reply
address.

```
Claude session ──SendMessage──▶ stub socket ──turn/start──▶ Codex thread
Claude session ◀──one JSON line── ccx MCP ◀──peer_send(_meta)── Codex thread
```

The reply address is the same on both legs — the stub socket. So a reply is
literally "copy `from` into `to`", with no routing table.

### Non-goals

- No patching, forking or wrapping of either binary.
- No network transport. Local Unix sockets only, same user.
- Not a general agent-orchestration framework. Two verbs: list, send.
- Not multi-user or multi-host.

---

## 2. Repo layout

```
ccx/
  __init__.py
  cli.py            # ccx daemon | mcp | doctor | codex
  codexrpc.py       # from reference/wsrpc.py + thread helpers
  envelope.py       # <cross-session-message> encode/decode
  claudereg.py      # ~/.claude/sessions read + write, socket paths
  stub.py           # per-thread stub process
  bridged.py        # supervisor: poll threads, own stubs
  mcp.py            # stdio MCP server
tests/
docs/protocol-notes.md
reference/wsrpc.py
```

Python 3.11+, **stdlib only**. Every dependency added is a dependency the user
has to install before two agents can talk.

---

## 3. The E2E harness — a first-class deliverable, not an afterthought

**We are shipping two things: the bridge, and a harness that proves it works.**
The harness is not optional and not secondary. Build it alongside M1–M2, and
implement every milestone's acceptance test *inside* it, so acceptance is
repeatable rather than a one-time manual demo.

### Full-system mode only

No unit tests of the envelope encoder. No mocked sockets. No fake app-server.
There is a real Codex subscription and a real Claude Code install on this
machine — **every test drives real sessions of both harnesses end to end**:

- a real `claude` process, launched by the harness, registering itself normally;
- a real `codex` TUI attached to a real app-server daemon;
- real messages crossing real Unix sockets;
- assertions made against real transcripts.

If a test can pass while the product is broken, it is the wrong test.

### Isolation — mandatory

Tests must never pollute the user's live environment. A leaked registry file
puts a dead peer in their real `ListAgents`; a leaked socket is worse.

- `CLAUDE_CONFIG_DIR=<scratch>` — Claude's config root is honoured from this env
  var, so `<scratch>/sessions/` becomes the registry the test sessions use.
- `CLAUDE_CODE_TMPDIR=<scratch>` — moves `cc-socks/` out of `/tmp`. **Use this,
  not `XDG_RUNTIME_DIR`** (established empirically 2026-08-10: `XDG_RUNTIME_DIR`
  is read by the path builder, but pointing it at a directory whose ownership or
  mode is not `0700` makes the bind refuse, silently).
- `CLAUDE_CODE_HARBOR_KITE=1` — **mandatory.** Cross-session messaging sits
  behind a Statsig gate defaulting to false; a cold scratch config dir has no
  cached gate value, so sessions come up with no messaging socket at all. See
  `docs/protocol-notes.md` §1.0.
- **Scrub the parent session's env.** A Claude session leaks
  `CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_PID`, `CLAUDE_CODE_EXECPATH`,
  `CLAUDE_CODE_SESSION_ID`, `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT` and friends
  into children — including through the tmux server. Also scrub
  `CLAUDE_CODE_SIMPLE` and never pass `--bare`: either disables messaging
  **silently, with no log line**.
- Investigate the equivalent for Codex (`CODEX_HOME` is the likely lever —
  `initialize` reports `codexHome`). If Codex cannot be isolated, use a
  dedicated daemon and reap only threads the harness created; **never** stop a
  daemon the harness did not start.
- Every fixture tears down on failure and on interrupt, not just on success.
  Assume the harness will be killed mid-run and still leave nothing behind.

### Capabilities the harness needs

- spawn a named Claude session and wait for it to register;
- spawn a Codex TUI attached to the daemon and wait for its thread to appear;
- send a message as either side, and read what the other side actually received —
  from `tmux capture-pane` for the Codex TUI, and from the Claude session's
  JSONL transcript under `<config-dir>/projects/**/*.jsonl`;
- assert on envelope structure, not just on text appearing somewhere;
- report a readable diff on failure;
- `ccx e2e` as a single entry point that runs the whole suite and exits non-zero
  on failure.

Keep scenarios small — these consume real tokens on both subscriptions.

---

## 4. Milestones

Each milestone ends with a concrete acceptance test **implemented in the
harness**. **Do not advance past a failing acceptance test.** Report results with
actual output — if a test fails, say so with the output rather than describing it
as working.

### M0 — scaffold
`git init`, package skeleton, `ccx --help`, `ccx doctor` stub, and the empty
`e2e/` harness with its isolation fixtures already working.
Confirm `git config user.name` / `user.email` match the user's identity before
the first commit.

**Accept:** `ccx e2e` spawns a real isolated Claude session in a scratch config
dir, sees it register, tears it down, and leaves **zero** artifacts — verified by
diffing the scratch dir and the user's real `~/.claude/sessions/` before and
after. Get this right before writing any bridge code; everything else depends on
it.

### M1 — Codex transport
Port `reference/wsrpc.py` into `ccx/codexrpc.py`. Add: daemon ensure/start,
`initialize`, `list_threads()`, `start_turn(thread_id, text)`.

**Accept:** with a TUI attached via
`codex --remote unix://~/.codex/app-server-control/app-server-control.sock`
(host it in tmux), `python -m ccx.codexrpc --inject <tid> "say PONG"` makes the
TUI print `PONG`. Capture the tmux pane as evidence.

### M2 — stub process → Claude sees Codex
`ccx/claudereg.py` (registry read/write, socket path rules) and `ccx/stub.py`.
`ccx/bridged.py` polls `thread/loaded/list` and maintains one stub per thread —
spawn on appear, reap on disappear, remove the registry file on exit **and on
crash**.

Naming: `codex-<cwd-slug>-<short-thread-id>`, distinct enough to avoid Claude's
`[ref]` disambiguation prompt.

**Accept, end to end:**
1. Start a Codex TUI attached to the daemon.
2. Start `ccx daemon`.
3. In a Claude session: `ListAgents` shows the Codex thread as a peer.
4. `SendMessage` to it → the text appears in the Codex TUI as a user turn.
5. Kill `ccx daemon` → no stray sockets, no stray `~/.claude/sessions/*.json`.

Step 5 is not optional. A leaked registry file puts a dead peer in the user's
real `ListAgents`.

### M3 — MCP server → the round trip
`ccx/mcp.py`: stdio MCP, protocol `2025-06-18`. Tools:

- `peers_list()` → live Claude sessions from `~/.claude/sessions/` (name, cwd,
  status, address). Exclude stubs we own.
- `peer_send(to, message, summary)` → wrap in `<cross-session-message>` with
  `from` = the calling thread's stub socket, write one line to the target.

Identity comes from `_meta.x-codex-turn-metadata.thread_id` (or
`params.threadId`). If absent, **fail loudly** — do not guess a thread.

**Accept, the whole point of the project:** a Codex agent calls `peer_send` to a
Claude session; that session receives it as a `<cross-session-message>`; it
replies by copying `from` into `to`; the reply lands in the **same** Codex
thread. Both transcripts captured.

### M4 — receipts
Relay Claude's `peer_message_status` control frames (`held` / `denied` /
`expired` / `delivered`) into the originating Codex thread via
`thread/inject_items` (no new turn) or a short `turn/start`.

Without this, a Codex agent whose message is sitting in a Claude approval prompt
sees silence and will assume refusal.

**Accept:** send from Codex to a Claude session running in `prompting` mode;
the Codex thread observes `held`, then `delivered` or `denied` after the user
answers.

### M5 — hardening
- `ccx doctor` — exercise all four contracts (Claude socket write, registry
  visibility, Codex daemon RPC, MCP `_meta` shape) and **fail loudly** with the
  specific contract that moved. This is the version-drift alarm; it is why the
  design is maintainable.
- `ccx codex [args…]` — ensure daemon, then exec
  `codex --remote unix://<control.sock> "$@"`.
- Unreachable-peer errors: addressing a Codex session that is not daemon-attached
  must return an **error**, never a silent success.
- Stamp `from-name="codex:<thread>"` so the receiving model can tell the sender
  is not a Claude session.
- `README.md`: install, the one-time `codex mcp add ccx -- ccx mcp`, and the
  per-session `ccx codex` requirement.

---

## 4. Hazards

**Version drift is the real cost.** Neither surface is public. Claude's `uds:`
scheme, registry shape and envelope are 2.1.226 internals; Codex's app-server is
`[experimental]` and 0.147.0 is already offered. The mitigation is `ccx doctor`,
not optimism.

**Test hygiene.** Tests create sockets, registry files and daemons in the user's
real environment. Clean up every artifact. Never write into another live Claude
session's socket while testing — use a purpose-built listener or the test
session's own socket. Do not stop a Codex daemon you did not start.

**Permission laundering, now cross-provider.** A Codex peer sits entirely outside
Claude's permission model. Never build anything that lets a peer escalate; the
`from-name` stamp in M5 exists so the receiving model can see what it is talking
to.

**Reach, not exposure.** Sockets stay `0600` and user-owned — the same posture
Claude already uses. The bridge must not widen it. What it does add is reach: a
Codex session can now type into a Claude session. That is the feature.

**The launch asymmetry.** Sending works from any Codex session; **receiving
requires the `--remote` attachment**. Make this loud in the README and in error
messages rather than letting messages vanish.

---

## 6. Definition of done

A Claude Code session and a Codex session, started in two terminals, hold a
multi-turn conversation in both directions — each addressing the other by name,
each reply landing in the right place — with no manual plumbing beyond
`codex mcp add ccx` once and `ccx codex` to launch.

And: **`ccx e2e` reproduces that conversation unattended, against real sessions
of both harnesses, and exits clean.** The demo is not the deliverable; the
harness that repeats the demo is.

---

## 7. Working agreement

You are running unattended in a detached tmux session under
`--dangerously-skip-permissions`. Another Claude session is supervising and will
message you roughly every 30 minutes.

- Work milestone by milestone. Commit at each green acceptance test.
- When the supervisor messages you, reply via `SendMessage` to the `from`
  address with: current milestone, what is green, what is red, and any blocker.
- **Report failures as failures**, with the actual output. A milestone described
  as working when its test did not pass is worse than no progress.
- If you hit something genuinely undecidable — a design fork the plan does not
  cover, or a blocker needing the user's judgement — stop and say so in your
  next reply rather than guessing. The supervisor will escalate.
- You have real subscriptions on both sides. Use them. Do not simulate.
