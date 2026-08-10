"""M0 — the isolation fixture itself.

Spawns a real Claude Code session in a scratch config dir, proves it registered
as a genuine addressable peer, tears it down, and proves nothing survived. Every
later milestone stands on this, so it asserts the details rather than just
"a file appeared".
"""

import os

from ..session import ClaudeSession


def run(ctx):
    scratch = ctx.scratch
    session = ClaudeSession(scratch, "ccx-m0-probe", ctx.token).start()

    entry = scratch.registry()[os.path.basename(session.registry_file)]

    # Registered in the scratch dir, not the user's.
    assert session.registry_file.startswith(scratch.config), (
        f"registry file escaped the scratch config dir: {session.registry_file}"
    )
    assert os.path.basename(session.registry_file) == f"{entry['pid']}.json", (
        f"registry filename must be <pid>.json, got "
        f"{os.path.basename(session.registry_file)} for pid {entry['pid']}"
    )

    # A real, addressable peer: interactive, protocol 1, socket inside scratch.
    assert entry.get("kind") == "interactive", f"kind={entry.get('kind')!r}"
    assert entry.get("peerProtocol") == 1, f"peerProtocol={entry.get('peerProtocol')!r}"
    assert entry.get("name") == "ccx-m0-probe", f"name={entry.get('name')!r}"

    sock = session.socket_path
    assert sock and sock.startswith(scratch.socks), (
        f"messaging socket escaped the scratch dir: {sock!r}\n"
        "(if this is None, something set XDG_RUNTIME_DIR — that silently "
        "disables socket binding)"
    )
    assert os.path.exists(sock), f"registry advertises {sock} but it is not there"

    # The socket is live and 0600 — same posture Claude uses in production.
    mode = os.stat(sock).st_mode & 0o777
    assert mode == 0o600, f"socket mode {oct(mode)}, expected 0o600"

    forced = session.stop()
    assert not forced, f"session did not clean up after itself: {sorted(forced)}"

    # Belt and braces: the runner also checks this, but a clear failure here
    # points at the session rather than at the scenario.
    assert not os.path.exists(session.registry_file), "registry file survived exit"
    assert not os.path.exists(sock), "socket survived exit"
