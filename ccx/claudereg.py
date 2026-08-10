"""Claude Code's session registry: where it lives, what a peer entry looks like.

The peer listing filters on a socket connect probe only — it does not check that
the pid belongs to a Claude process. Any process that binds a socket and drops a
matching `<pid>.json` is a first-class peer. That is what makes the stub work.

Two hard rules, both verified: the filename must be `<pid>.json` with the
integer matching the `pid` field or the file is unlinked on read, and the socket
named by `messagingSocketPath` must actually accept a connection or the entry is
filtered out of the listing.

See docs/protocol-notes.md sections 1.1-1.3.
"""

import json
import os
import socket
import subprocess

PEER_PROTOCOL = 1
# Reported to peers as our Claude-compatibility level, not as a Claude version
# we are pretending to be. `from-name` in M5 is what tells a peer we are Codex.
CLAUDE_VERSION = "2.1.226"

# Beyond this the kernel truncates sun_path, so Claude falls back to a shorter
# per-uid directory. Same rule here.
SUN_PATH_LIMIT = 104


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def sessions_dir():
    return os.path.join(config_dir(), "sessions")


def socket_dir():
    """Where Claude puts `cc-socks/`.

    CLAUDE_CODE_TMPDIR wins, then XDG_RUNTIME_DIR, then /tmp — note that this is
    literally /tmp and not $TMPDIR, confirmed against live sessions whose TMPDIR
    pointed at /var/folders/… while their sockets sat in /tmp/cc-socks.
    """
    base = os.environ.get("CLAUDE_CODE_TMPDIR") or os.environ.get("XDG_RUNTIME_DIR")
    return os.path.join(base or "/tmp", "cc-socks")


def socket_path(pid):
    path = os.path.join(socket_dir(), f"{pid}.sock")
    if len(path) > SUN_PATH_LIMIT:
        path = os.path.join(f"/tmp/cc-socks-{os.getuid()}", f"{pid}.sock")
    return path


def proc_start(pid):
    """The `procStart` field, in the format Claude writes: `ps -o lstart`."""
    proc = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True
    )
    return proc.stdout.strip()


def entry(pid, session_id, cwd, name, sock, started_at, status="idle", extra=None):
    now = int(started_at * 1000)
    record = {
        "pid": pid,
        "sessionId": session_id,
        "cwd": cwd,
        "startedAt": now,
        "procStart": proc_start(pid),
        "version": CLAUDE_VERSION,
        "peerProtocol": PEER_PROTOCOL,
        "kind": "interactive",
        "entrypoint": "cli",
        "messagingSocketPath": sock,
        "name": name,
        "nameSource": "explicit",
        "status": status,
        "updatedAt": now,
        "statusUpdatedAt": now,
    }
    record.update(extra or {})
    return record


def write(record):
    """Publish a registry entry. Written to a temp file and renamed, so a peer
    listing never sees a half-written file."""
    os.makedirs(sessions_dir(), exist_ok=True)
    path = os.path.join(sessions_dir(), f"{record['pid']}.json")
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(record, f)
    os.replace(tmp, path)
    return path


def update(pid, **fields):
    path = os.path.join(sessions_dir(), f"{pid}.json")
    try:
        with open(path) as f:
            record = json.load(f)
    except (OSError, ValueError):
        return None
    record.update(fields)
    return write(record)


def remove(pid):
    for path in (
        os.path.join(sessions_dir(), f"{pid}.json"),
        os.path.join(sessions_dir(), f"{pid}.json.tmp"),
    ):
        try:
            os.unlink(path)
        except OSError:
            pass


def read_all():
    out = {}
    try:
        names = os.listdir(sessions_dir())
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(sessions_dir(), name)) as f:
                out[name] = json.load(f)
        except (OSError, ValueError):
            pass
    return out


def reachable(record):
    """Claude's own liveness test: can we connect to the advertised socket."""
    sock = record.get("messagingSocketPath")
    if not sock:
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(sock)
        return True
    except OSError as exc:
        # EBUSY means someone is there but not accepting right now — still live.
        return getattr(exc, "errno", None) == 16
    finally:
        s.close()


def send(sock_path, message):
    """Write one JSON line into a Claude session's socket."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    try:
        s.connect(sock_path)
        s.sendall((json.dumps(message) + "\n").encode())
    finally:
        s.close()


def address(sock_path):
    """The canonical `uds:` form used as a reply address."""
    return f"uds:{sock_path}"


def slug(path, limit=24):
    """`/Users/x/personal/projects/foo` -> `foo`, safe for a peer name."""
    base = os.path.basename(os.path.normpath(path or "")) or "root"
    base = "".join(c if c.isalnum() or c in "-_" else "-" for c in base)
    return base[:limit].strip("-") or "root"
