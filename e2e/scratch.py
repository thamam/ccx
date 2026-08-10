"""Isolation fixtures: a scratch Claude environment that leaves nothing behind.

Verified against Claude Code 2.1.226 on 2026-08-10. Three findings drive the
shape of this module:

1. `CLAUDE_CODE_TMPDIR` relocates the messaging socket dir — the session binds
   `<CLAUDE_CODE_TMPDIR>/cc-socks/<pid>.sock`.
2. `XDG_RUNTIME_DIR` does **not**. Setting it makes the session bind no
   messaging socket at all, silently: it still registers, but with no
   `messagingSocketPath`, so it is invisible to peer messaging. PLAN.md names
   XDG_RUNTIME_DIR as the lever; that is wrong and actively harmful. Never set it.
3. A parent Claude session leaks `CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_PID`
   and friends into children (directly, or through a tmux server it started).
   The inherited socket path collides and the child binds nothing. Strip them.

The scratch root must be a real path, not a symlink: cwd is canonicalised
before the trust check, so seeding `/tmp/x` while Claude looks up
`/private/tmp/x` re-triggers the trust dialog.
"""

import json
import os
import shutil

# Real config dir surfaces the harness must never touch.
REAL_SESSIONS = os.path.expanduser("~/.claude/sessions")
REAL_SOCKS = "/tmp/cc-socks"

# Inherited from a parent Claude session; poisons the child if left in place.
ENV_DENY = (
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_PID",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "CLAUDE_EFFORT",
    "AI_AGENT",
    "XDG_RUNTIME_DIR",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_TMPDIR",
    # CLAUDE_CODE_SIMPLE skips the whole messaging setup block *without logging*,
    # so it fails exactly like a gate-off but leaves no trace in --debug output.
    "CLAUDE_CODE_SIMPLE",
)

CLAUDE_VERSION = "2.1.226"


class Scratch:
    """A throwaway Claude config root plus its socket dir."""

    def __init__(self, root="/private/tmp/ccx-e2e"):
        # Canonicalise so the trust-dialog seed matches the path Claude resolves.
        parent = os.path.realpath(os.path.dirname(root))
        self.root = os.path.join(parent, os.path.basename(root))
        self.config = os.path.join(self.root, "config")
        self.run = os.path.join(self.root, "run")
        self.socks = os.path.join(self.run, "cc-socks")
        self.wd = os.path.join(self.root, "wd")

    # -- lifecycle -------------------------------------------------------

    def create(self):
        shutil.rmtree(self.root, ignore_errors=True)
        for d in (self.config, self.run, self.wd):
            os.makedirs(d, exist_ok=True)
        self._seed()
        return self

    def destroy(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _seed(self):
        """Pre-answer onboarding, theme and trust so startup is non-interactive."""
        with open(os.path.join(self.config, ".claude.json"), "w") as f:
            json.dump(
                {
                    "theme": "dark-ansi",
                    "hasCompletedOnboarding": True,
                    "lastOnboardingVersion": CLAUDE_VERSION,
                    "installMethod": "native",
                    "autoUpdates": False,
                    "numStartups": 5,
                    "projects": {
                        self.wd: {
                            "hasTrustDialogAccepted": True,
                            "hasCompletedProjectOnboarding": True,
                            "projectOnboardingSeenCount": 1,
                            "allowedTools": [],
                            "mcpServers": {},
                            "enabledMcpjsonServers": [],
                            "disabledMcpjsonServers": [],
                            "ignorePatterns": [],
                            "exampleFiles": [],
                        }
                    },
                },
                f,
                indent=1,
            )
        with open(os.path.join(self.config, "settings.json"), "w") as f:
            json.dump(
                {
                    # Without this the bypass-permissions banner blocks startup.
                    "skipDangerousModePermissionPrompt": True,
                    "theme": "dark-ansi",
                    # Keep test sessions off the user's cloud surfaces.
                    "remoteControlAtStartup": False,
                    "enabledPlugins": {},
                    "env": {},
                },
                f,
                indent=1,
            )

    # -- child environment ----------------------------------------------

    def child_overrides(self, oauth_token):
        """Variables the child must have set, on top of a scrubbed environment."""
        return {
            "CLAUDE_CONFIG_DIR": self.config,
            "CLAUDE_CODE_TMPDIR": self.run,
            "CLAUDE_CODE_OAUTH_TOKEN": oauth_token,
            # Cross-session messaging sits behind the `tengu_harbor_kite` Statsig
            # gate, which defaults to false and is only true once a config dir
            # has a warm gate cache. A scratch dir is always cold, so without
            # this the session registers but binds no socket and logs
            # "[uds-messaging] Skipped: cross-session messaging gate off".
            # Pinning it also keeps the suite independent of flag rollout.
            "CLAUDE_CODE_HARBOR_KITE": "1",
        }

    def child_env(self, oauth_token):
        env = {k: v for k, v in os.environ.items() if k not in ENV_DENY}
        env.update(self.child_overrides(oauth_token))
        return env

    # -- inspection ------------------------------------------------------

    @property
    def sessions_dir(self):
        return os.path.join(self.config, "sessions")

    def registry(self):
        """Every session registry file currently in the scratch config dir."""
        out = {}
        for name in _listdir(self.sessions_dir):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.sessions_dir, name)) as f:
                    out[name] = json.load(f)
            except (OSError, ValueError):
                pass
        return out

    def leftovers(self):
        """Artifacts that must not survive teardown."""
        socks = [f for f in _listdir(self.socks) if f.endswith(".sock")]
        return {
            "scratch_registry": sorted(self.registry()),
            "scratch_sockets": sorted(socks),
        }


def _listdir(path):
    try:
        return os.listdir(path)
    except OSError:
        return []


def real_snapshot():
    """Filenames the user can actually see. Compared before/after every run."""
    return {
        "real_registry": sorted(f for f in _listdir(REAL_SESSIONS) if f.endswith(".json")),
        "real_sockets": sorted(f for f in _listdir(REAL_SOCKS) if f.endswith(".sock")),
    }


def diff_snapshot(before, after):
    """Additions only — the user's own sessions come and go independently."""
    added = {}
    for key, was in before.items():
        if not isinstance(was, list):
            continue  # non-listing entries are compared by their own checks
        new = sorted(set(after.get(key, [])) - set(was))
        if new:
            added[key] = new
    return added
