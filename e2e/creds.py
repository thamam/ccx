"""Locate a usable Claude Code OAuth access token for harness-launched sessions.

A session started with a scratch CLAUDE_CONFIG_DIR does not see the credentials
of the user's normal config dir: on macOS, Claude Code scopes its Keychain item
by config dir (`Claude Code-credentials-<8hex>`), so a fresh config dir means a
fresh, empty item and the session comes up "Not logged in".

Passing CLAUDE_CODE_OAUTH_TOKEN in the child environment sidesteps that without
writing credentials to disk or minting new Keychain items. This module finds the
newest Keychain item that still holds an unexpired `claudeAiOauth` token.

The token is never logged. Callers get the raw string and put it straight into
the child environment.
"""

import json
import re
import subprocess
import time

_SVCE = re.compile(r'"svce"<blob>="(Claude Code-credentials[^"]*)"')
_MDAT = re.compile(r'"mdat".*"(\d{14})Z')

# How many recently-touched items to probe before giving up. The Keychain
# accumulates one item per config dir ever used, so a full scan is not viable.
_MAX_CANDIDATES = 25


class NoCredentials(RuntimeError):
    pass


def _candidates():
    """Keychain service names holding Claude credentials, newest first."""
    out = subprocess.run(
        ["security", "dump-keychain"], capture_output=True, text=True
    ).stdout
    found, cur = [], {}
    for line in out.splitlines():
        m = _MDAT.search(line)
        if m:
            cur["mdat"] = m.group(1)
        m = _SVCE.search(line)
        if m:
            cur["svce"] = m.group(1)
        if line.startswith("keychain:"):
            if "svce" in cur:
                found.append((cur.get("mdat", ""), cur["svce"]))
            cur = {}
    if "svce" in cur:
        found.append((cur.get("mdat", ""), cur["svce"]))
    found.sort(reverse=True)
    return [s for _, s in found]


def oauth_token():
    """Return an unexpired Claude Code OAuth access token.

    Raises NoCredentials with an actionable message if none is available.
    """
    now_ms = time.time() * 1000
    for svce in _candidates()[:_MAX_CANDIDATES]:
        blob = subprocess.run(
            ["security", "find-generic-password", "-s", svce, "-w"],
            capture_output=True,
            text=True,
        ).stdout
        try:
            oauth = json.loads(blob).get("claudeAiOauth") or {}
        except ValueError:
            continue
        token, expires = oauth.get("accessToken"), oauth.get("expiresAt") or 0
        if token and expires > now_ms:
            return token
    raise NoCredentials(
        "no unexpired Claude Code OAuth token found in the Keychain.\n"
        "Run `claude` and `/login` in your normal config dir, then retry."
    )
