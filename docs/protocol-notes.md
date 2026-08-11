# Verified protocol notes — Claude Code ↔ Codex

Everything here was confirmed by running it on this machine on 2026-08-10 against
**Claude Code 2.1.226** and **codex-cli 0.146.1** (macOS, arm64).

**2026-08-11, Claude Code 2.1.227:** all four `ccx doctor` contracts still pass
(socket write, registry visibility, Codex daemon RPC, MCP `_meta` shape), and
so does the m0 isolation scenario — registration, socket binding and the
`tengu_harbor_kite` gate are unchanged. The wire details below were **not**
re-derived against .227; only that they still hold.

Do **not** re-derive these by disassembling binaries. If something here stops
matching reality, that is a version-drift bug — fix `ccx doctor`, don't go
spelunking again.

> Both surfaces are **internal and undocumented**. Treat every path in this file
> as version-pinned. Codex's app-server is explicitly labelled `[experimental]`.

---

## 1. Claude Code — cross-session peer messaging

Claude Code sessions are genuine peers. Each interactive session:

- binds a Unix socket, and
- writes a registry file describing itself.

### 1.0 What gates socket binding — read this before debugging a missing socket

Decompiled from 2.1.226 `setup()`:

```js
MOe.unset("CLAUDE_CODE_MESSAGING_SOCKET");
if (!Yf() || explicitSocketPath !== undefined)
  if (!Yv())      log("[uds-messaging] Skipped: cross-session messaging gate off")
  else if (Wa())  log("[uds-messaging] Skipped: remote thin client")
  else            startUdsMessaging(explicitSocketPath ?? getDefaultUdsSocketPath(), …)

Yf() = truthy(env.CLAUDE_CODE_SIMPLE) || hasArg("--bare")
Yv() = platform === "windows" ? false
       : statsigGate("tengu_harbor_kite", default=false) || Boolean(env.CLAUDE_CODE_HARBOR_KITE)
Wa() = caps.workspace === "remote"
```

Three ways to end up with a registered session that has **no**
`messagingSocketPath`:

| cause | symptom |
|---|---|
| `CLAUDE_CODE_SIMPLE` set, or `--bare` | **silent** — no log line at all |
| `tengu_harbor_kite` gate false | logs `Skipped: cross-session messaging gate off` |
| remote thin client | logs `Skipped: remote thin client` |

**Cross-session messaging is behind a Statsig gate that defaults to false.** A
warm config dir has the value cached and resolves true; a **cold scratch
`CLAUDE_CONFIG_DIR` does not**, so messaging is silently disabled in exactly the
situation an E2E harness creates. This presents as intermittent and appears to
correlate with concurrent session starts — that is the gate cache being
populated by one session and read by the next.

**Always set `CLAUDE_CODE_HARBOR_KITE=1`** for harness-spawned sessions. It
short-circuits the gate deterministically. Pin it regardless of the rollout
state — a suite whose behaviour depends on a remote feature flag is not a suite.

Diagnosis shortcut: `claude --debug 2>&1 | grep uds-messaging`. A log line means
`Yv`/`Wa`; **total silence means `Yf`**.

### 1.1 Socket path

```
$XDG_RUNTIME_DIR || <tmpdir>  +  /cc-socks/<pid>.sock
```

Fallback when that path exceeds ~104 bytes:

```
/tmp/cc-socks-<uid>/<pid>.sock
```

Live example: `/tmp/cc-socks/33095.sock`, mode `0600`, user-owned.

The path is also exported into the session's own environment as
`CLAUDE_CODE_MESSAGING_SOCKET`, and can be overridden with
`--messaging-socket-path`.

### 1.2 Registry — this is the discovery mechanism

```
~/.claude/sessions/<pid>.json
```

Real example:

```json
{"pid":19790,"sessionId":"2150d9c0-...","cwd":"/Users/tomerhamam/...",
 "startedAt":1786359386860,"procStart":"Mon Aug 10 10:56:22 2026",
 "version":"2.1.226","peerProtocol":1,"kind":"interactive","entrypoint":"cli",
 "messagingSocketPath":"/tmp/cc-socks/19790.sock",
 "name":"code-reviw-forked-studio-vesrion-bb","nameSource":"derived",
 "status":"idle","updatedAt":1786360651395,"statusUpdatedAt":1786360651395,
 "bridgeSessionId":"session_013cNe..."}
```

**Critical detail, verified:** the peer listing filters on a **socket connect
probe only** — connect succeeds (or returns `EBUSY`) ⇒ listed. It does *not*
verify the pid belongs to a Claude process. The pid is used only to decide
whether to garbage-collect a stale file.

Consequence: *any* process that binds a socket and drops a matching
`<pid>.json` appears as a first-class peer in `ListAgents`.
Confirmed live — a Python listener showed up as
`codex-probe-peer [467593] · interactive · idle`.

Filename must be `<integer>.json` and the integer must match the `pid` field,
or the file is unlinked on read.

### 1.3 Address schemes

The `to` field of `SendMessage` is parsed as:

| prefix | scheme |
|---|---|
| `uds:` | unix socket (rest is percent-decoded) |
| `bridge:` | cloud session |
| `did:` | decentralised id |
| `/` (bare absolute path) | **uds** |
| `\\.\pipe\` | uds (Windows) |
| anything else | `other` — resolved as a name |

So `SendMessage({to: "/tmp/cc-socks/48210.sock", ...})` works directly, and
`uds:<percent-encoded-path>` is the canonical form.

Name collisions require a disambiguator: the tool errors with
`Re-send with the ref to confirm you mean: <name> [467593]`. Ref is the first
8 hex of a hash.

**A distinct name is not enough to avoid this.** Verified 2026-08-10: a peer
that is not already part of the conversation gets the same confirmation demand
even when its name is unique —

```
'codex-cccdx-messaging-efac37' is not an agent in this conversation.
Re-send with the ref to confirm you mean:
  codex-cccdx-messaging-efac37 [428f2d] — Claude session, on this machine
```

The sending model recovers on its own by re-sending with the ref, so this costs
a round trip rather than a delivery. Note also the description Claude renders:
**"Claude session, on this machine"** — the receiving side has no way to tell a
stub from a real Claude peer, which is what the `from-name="codex:<thread>"`
stamp in M5 is for.

### 1.4 Wire format — inbound to a Claude session

Newline-delimited JSON, one message per line, 1 MiB cap per line before the
connection is dropped. Captured verbatim from a real `SendMessage`:

```json
{"msgV":1,
 "msg_id":"ed5bf970-cf01-4ada-a8a1-40cd78116d58",
 "type":"user",
 "message":{"role":"user","content":"<cross-session-message from=\"uds:/tmp/cc-socks/33095.sock\" from-name=\"Enable cross-provider messaging…\" from-mode=\"bypass\">\nBODY TEXT\n</cross-session-message>"},
 "priority":"next",
 "from":"uds:/tmp/cc-socks/33095.sock"}
```

Envelope rules:

- Tag name is literally `cross-session-message`.
- Attribute order is fixed: `from`, `from-session`, `hop-chain`, `from-name`,
  `from-mode`. All optional. The receiver re-serialises and compares against the
  original string — **if it doesn't round-trip byte-identically the metadata is
  discarded** and the raw text is shown instead. Emit attributes in this order.
- Body is wrapped in `\n` … `\n` inside the tag.
- `from-name` is truncated to 64 graphemes with `…`, and `"`, `<`, `>` stripped.
- `from-mode` ∈ `bypass` | `prompting`. **Omitting it is not neutral.** A
  message from a sender that does not attest its permission mode is *held* for
  the recipient user's approval — even in a session running with
  `--dangerously-skip-permissions`, which reports it as "from an unidentified
  session … The sender did not attest its permission mode and this session
  bypasses prompts." Anything unattended must attest, or set
  `crossSessionInbound: "accept"` on the receiving session.
- `hop-chain` is a comma list of 24-hex ids, max 32 — loop prevention.
- `priority`: `next` (normal) or `now` (jumps the queue, bypasses ordering).
- `msgV` is 1.

The minimal accepted message is much smaller — the harness logs this itself:

```
echo '{"type":"user","message":{"role":"user","content":"hello"}}' \
  | socat - UNIX-CONNECT:/tmp/cc-socks/<pid>.sock
```

`session_id`, if present, must match the receiver's session id or the message is
dropped. **Omit it.**

### 1.5 Control channel — same socket

```json
{"type":"control","action":"rename","name":"new-name"}
{"type":"control","action":"peer_message_status",
 "status":"held|denied|expired|delivered","orig_msg_id":"<msg_id>"}
```

Receipt semantics (these strings are shown to the sending model):

- `held` — waiting on the recipient user's approval (permission-mode parity)
- `denied` — recipient user declined; not delivered
- `expired` — held message timed out
- `delivered` — a previously-held message was approved and released

A receipt is dropped unless it matches an outstanding send. The sender only
accepts receipts from a reply address **inside its own socket namespace**.

### 1.6 Launching a session that can actually be messaged

Verified 2026-08-10 while building the E2E harness. §1.0 covers the feature
gate; these are the other three ways a scripted launch ends up unreachable.

**Parent-session env leaks.** A Claude session exports
`CLAUDE_CODE_MESSAGING_SOCKET` (its own socket path), `CLAUDE_PID`,
`CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_BRIDGE_SESSION_ID`,
`CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_CODE_EXECPATH` and more. Children inherit
them — including through a tmux server the session started — and the inherited
socket path collides, so the child binds nothing. Scrub them explicitly in the
command (`env -u …`); relying on the launcher's own environment is not enough
when tmux is in the middle.

**Socket relocation.** `CLAUDE_CODE_TMPDIR=<dir>` moves the socket to
`<dir>/cc-socks/<pid>.sock`. **Do not use `XDG_RUNTIME_DIR`** — it is read when
computing the default path, but pointing it at an ordinary scratch dir makes the
bind fail and the session ends up with no socket at all, silently.
`--messaging-socket-path <path>` also works and is exact.

Two further requirements for an unattended scratch config dir
(`CLAUDE_CONFIG_DIR=<scratch>`):

- Seed `<scratch>/.claude.json` with `hasCompletedOnboarding`, `theme`,
  `lastOnboardingVersion`, and a `projects.<cwd>.hasTrustDialogAccepted` entry,
  and `<scratch>/settings.json` with `skipDangerousModePermissionPrompt`.
  Otherwise startup blocks on theme / trust / bypass prompts. The `<cwd>` key
  must be the **realpath** — `/tmp/x` is canonicalised to `/private/tmp/x`
  before the trust check, so seeding the un-resolved path re-triggers the dialog.
- Credentials do not carry over. On macOS the Keychain item is scoped per config
  dir (`Claude Code-credentials-<8hex>`), so a new config dir starts logged out.
  Passing `CLAUDE_CODE_OAUTH_TOKEN=<accessToken>` avoids minting Keychain items
  or writing credentials to disk. A valid token can be read out of the newest
  `Claude Code-credentials*` item that has an unexpired `claudeAiOauth`.

Registration is fast — under a second from launch. If a socket has not appeared
by then, waiting longer will not help; check the gate.

### 1.7 Peer credentials

The receiver reads the connecting peer's pid via `SO_PEERCRED` and stamps
`verifiedPeerPid`. If that pid is an ancestor of the receiver it marks the
message `selfSent`. Neither blocks delivery — informational only.

---

## 2. Codex — what it actually has

**Codex has no cross-session messaging.** The `multi_agent` tool family
(`spawn_agent`, `send_message`, `followup_task`, `wait_agent`,
`interrupt_agent`, `list_agents`) drives an **in-process agent tree** inside one
thread, addressed by task path (`/root`, `/root/reviewer`). Messages arrive in
the analysis channel as:

```
Message Type: NEW_TASK | MESSAGE | FINAL_ANSWER
Task name: <recipient>
Sender: <author>
Payload:
<payload text>
```

Two Codex terminals cannot reach each other. Feature flags: `multi_agent`
stable/on, `multi_agent_v2` stable/off.

There is no socket, no registry, no external surface for a plain `codex` TUI.
**A plain `codex` session cannot be reached. Verified.**

---

## 3. Codex app-server — the injection path

### 3.1 Daemon

```bash
codex app-server daemon start     # {"status":"started","pid":...,"socketPath":...}
codex app-server daemon version   # status probe
codex app-server daemon stop
```

Socket: `~/.codex/app-server-control/app-server-control.sock`

### 3.2 Transport — this is the gotcha

The control socket does **not** take raw newline JSON. It speaks a **WebSocket
upgrade over the Unix socket**:

```
GET /rpc HTTP/1.1
Host: localhost
Connection: Upgrade
Upgrade: websocket
Sec-WebSocket-Version: 13
Sec-WebSocket-Key: <base64-16-bytes>
```

Server replies `HTTP/1.1 101 Switching Protocols`, then it is JSON-RPC in
WebSocket text frames (client frames must be masked). Note the id field is a
plain string/number and `jsonrpc` is not echoed back.

Raw connect-and-write gets a silent EOF. `codex app-server proxy` produced no
output in testing — **do not rely on it**; use the WS client in
`reference/wsrpc.py` (already written and verified).

`codex app-server` on stdio (no daemon) *does* speak plain newline JSON — useful
for schema work, not for reaching live sessions.

### 3.3 Handshake

```json
{"id":1,"method":"initialize","params":{"clientInfo":{"name":"ccx","title":"ccx","version":"0.1"}}}
```

### 3.4 Methods that matter

| method | use |
|---|---|
| `thread/loaded/list` | live thread ids — `{"data":["019feb74-..."],"nextCursor":null}` |
| `turn/start` | `{threadId, input:[{type:"text",text:"..."}]}` → injects a user turn |
| `turn/steer` | `{threadId, expectedTurnId, input}` → into a running turn |
| `thread/inject_items` | append raw items to history **without** triggering a turn |
| `thread/read` | history |
| `thread/start` | `{cwd, config:{...}}` — `config` is free-form, accepts `mcp_servers` overrides |
| `thread/resume` | `{threadId}` |

Full schema: `codex app-server generate-json-schema --out <dir>` (39 files,
90 client methods). Regenerate rather than guess.

**Verified behaviour under load:** `turn/start` while a turn is already running
**queues** — it does not error. Three concurrent injections (two `turn/start`,
one stale-turn-id `turn/steer`) all delivered and were answered in order. No
busy-state handling is required.

### 3.5 Attaching a TUI to the daemon

```bash
codex --remote unix:///Users/<you>/.codex/app-server-control/app-server-control.sock
```

Also works with `resume <threadId>` to attach to a thread the bridge created —
verified: the TUI rendered the pre-existing history.

Only daemon-attached threads appear in `thread/loaded/list`. This is the single
launch-time cost of the whole design.

---

## 4. Codex MCP — caller identity comes free

Register once:

```bash
codex mcp add ccx -- ccx mcp
```

Codex speaks MCP `2025-06-18` over stdio, client name `codex-mcp-client`.
Sequence observed: `initialize` → `notifications/initialized` → `tools/list`
→ `tools/call`.

**Every `tools/call` carries the caller's identity.** Captured verbatim:

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{
  "_meta":{
    "x-codex-turn-metadata":{
      "session_id":"019feb7a-c699-7e31-911f-1d65f7043e1d",
      "thread_id":"019feb7a-c699-7e31-911f-1d65f7043e1d",
      "turn_id":"019feb7a-c70c-7ea0-a386-3f69b3eef0bf",
      "sandbox":"none","turn_started_at_unix_ms":1786362119949,
      "model":"gpt-5.6-luna","reasoning_effort":"medium"},
    "progressToken":1},
  "threadId":"019feb7a-c699-7e31-911f-1d65f7043e1d",
  "name":"whoami","arguments":{}}}
```

`params.threadId` is also present at the top level. **This is why one globally
registered MCP server is sufficient** — no per-thread config, no env injection,
no wrapper on the send path.

(Per-thread `mcp_servers` config via `thread/start` also works and was verified —
env reached the spawned server process. It is not needed; documented only as a
fallback if `_meta` ever disappears.)

---

## 5. Test hygiene — learned the hard way

Anything that touches these surfaces leaves user-visible residue. Every test
must clean up after itself:

- `~/.claude/sessions/<pid>.json` — a stale file makes a dead peer show up in
  the user's real `ListAgents`
- `/tmp/cc-socks/*.sock`
- tmux sessions used to host TUIs
- `codex app-server daemon stop` if the test started it (check first — do not
  stop a daemon the user started)

Never write into another live Claude session's socket during testing. Use a
purpose-built listener, or the test session's own socket.
