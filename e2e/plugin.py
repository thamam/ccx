"""Installing ccx the way a user does — through both plugin marketplaces.

The two harnesses take the same repo and do different things with it, which is
worth knowing before reading a confusing test failure:

- Codex **git-clones** the marketplace source, so it installs committed HEAD.
  Uncommitted manifest changes are invisible to it.
- Claude **copies** the working tree, so it installs what is on disk.

Every install here is scoped to a scratch CODEX_HOME / CLAUDE_CONFIG_DIR, so
neither the user's plugin config nor their caches are touched.
"""

import json
import os
import subprocess

from . import scratch as scratch_mod

MARKETPLACE = "ccx"
PLUGIN = "ccx@ccx"


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(cmd, env, what):
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)
    if proc.returncode != 0:
        raise AssertionError(
            f"{what} failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
        )
    return proc.stdout


def install_into_codex(home):
    """`codex plugin marketplace add` + `codex plugin add`, scoped to a home."""
    env = dict(os.environ)
    env["CODEX_HOME"] = home.root
    _run(
        ["codex", "plugin", "marketplace", "add", repo_root()],
        env,
        "codex plugin marketplace add",
    )
    out = _run(["codex", "plugin", "add", PLUGIN], env, "codex plugin add")
    root = _installed_root(out)
    # Codex clones committed HEAD. If the manifests are not committed, the
    # install silently lacks them and the MCP server never appears — so say so
    # here rather than letting the tool-surface assertion fail mysteriously.
    for required in (".codex-plugin/plugin.json", ".mcp.json"):
        assert os.path.exists(os.path.join(root, required)), (
            f"{required} is missing from the installed plugin at {root}. "
            "Codex installs committed git HEAD — commit the plugin manifests "
            "before running this."
        )
    return root


def install_into_claude(scratch):
    """`claude plugin marketplace add` + `claude plugin install`, scoped to a
    scratch CLAUDE_CONFIG_DIR."""
    env = {k: v for k, v in os.environ.items() if k not in scratch_mod.ENV_DENY}
    env["CLAUDE_CONFIG_DIR"] = scratch.config
    _run(
        ["claude", "plugin", "marketplace", "add", repo_root()],
        env,
        "claude plugin marketplace add",
    )
    _run(["claude", "plugin", "install", PLUGIN], env, "claude plugin install")
    installed = os.path.join(scratch.config, "plugins", "installed_plugins.json")
    assert os.path.exists(installed), f"{installed} was never written"
    with open(installed) as f:
        assert "ccx" in json.dumps(json.load(f)), "ccx is not in installed_plugins.json"
    return _claude_plugin_root(scratch)


def _claude_plugin_root(scratch):
    base = os.path.join(scratch.config, "plugins", "cache", "ccx", "ccx")
    versions = sorted(os.listdir(base)) if os.path.isdir(base) else []
    assert versions, f"nothing cached under {base}"
    return os.path.join(base, versions[-1])


def _installed_root(stdout):
    for line in stdout.splitlines():
        if "Installed plugin root:" in line:
            return line.split("Installed plugin root:", 1)[1].strip()
    raise AssertionError(f"could not find the installed plugin root in:\n{stdout}")


def codex_mcp_tools(home, server="ccx", timeout=45):
    """Tool names the app-server has actually negotiated for a server.

    This is the assertion that matters: it proves the plugin's MCP entry was
    resolved, launched and initialized — not merely that a manifest parses.
    """
    import time

    deadline = time.time() + timeout
    seen = []
    while time.time() < deadline:
        data = (home.client().call("mcpServerStatus/list", {}, timeout=30) or {}).get(
            "data"
        ) or []
        seen = [s["name"] for s in data]
        for status in data:
            if status["name"] == server:
                return sorted(status.get("tools") or {})
        time.sleep(2)
    raise AssertionError(
        f"MCP server {server!r} never came up. Servers seen: {seen}"
    )
