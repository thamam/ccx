"""The `<cross-session-message>` envelope Claude Code speaks.

The receiver re-serialises the envelope it parsed and compares it against the
original string; if it does not round-trip byte-identically the metadata is
discarded and the raw text is shown instead. So attribute order is not a style
choice — it is `from`, `from-session`, `hop-chain`, `from-name`, `from-mode`,
always, and only the attributes that are present.

See docs/protocol-notes.md section 1.4.
"""

import hashlib
import re

TAG = "cross-session-message"
# `hop-chain` is loop prevention: a comma list of 24-hex ids, max 32.
HOP_LIMIT = 32
ATTR_ORDER = ("from", "from-session", "hop-chain", "from-name", "from-mode")
NAME_LIMIT = 64
MODES = ("bypass", "prompting")

_ENVELOPE = re.compile(
    rf"^<{TAG}((?:\s+[a-z-]+=\"[^\"]*\")*)\s*>\n(.*)\n</{TAG}>$", re.DOTALL
)
_ATTR = re.compile(r'([a-z-]+)="([^"]*)"')


def hop_id(seed):
    """A stable 24-hex id for one participant.

    Per-participant rather than per-message on purpose: a chain that already
    contains your own id means the message has been through you, which detects
    a cycle on its second hop instead of waiting out the 32-hop cap. Two agents
    that politely acknowledge each other would otherwise trade 32 turns before
    anything stopped them.
    """
    return hashlib.sha256(seed.encode()).hexdigest()[:24]


def parse_hops(value):
    return [hop for hop in (value or "").split(",") if hop]


def render_hops(hops):
    return ",".join(hops)


def clean_name(name):
    """`from-name` is stripped of quoting characters and truncated with an ellipsis."""
    if name is None:
        return None
    name = re.sub(r'["<>]', "", name)
    if len(name) > NAME_LIMIT:
        name = name[: NAME_LIMIT - 1] + "…"
    return name


def encode(body, **attrs):
    """Build an envelope. Keys use underscores: from_, from_session, …"""
    values = {
        "from": attrs.get("from_"),
        "from-session": attrs.get("from_session"),
        "hop-chain": attrs.get("hop_chain"),
        "from-name": clean_name(attrs.get("from_name")),
        "from-mode": attrs.get("from_mode"),
    }
    mode = values["from-mode"]
    if mode is not None and mode not in MODES:
        raise ValueError(f"from-mode must be one of {MODES}, got {mode!r}")
    rendered = "".join(
        f' {key}="{values[key]}"' for key in ATTR_ORDER if values[key] is not None
    )
    return f"<{TAG}{rendered}>\n{body}\n</{TAG}>"


def decode(content):
    """Split an envelope into (body, attrs). Returns (content, {}) if unwrapped.

    Anything that is not an envelope is a plain message and is passed through —
    a Claude session can be addressed with bare text.
    """
    match = _ENVELOPE.match(content.strip())
    if not match:
        return content, {}
    attrs = dict(_ATTR.findall(match.group(1)))
    return match.group(2), attrs


def wire_message(content, from_address=None, priority="next"):
    """One line of the newline-delimited protocol a Claude socket accepts.

    `session_id` is deliberately absent: when present it must match the
    receiver's session id or the message is dropped.
    """
    msg = {
        "msgV": 1,
        "type": "user",
        "message": {"role": "user", "content": content},
        "priority": priority,
    }
    if from_address:
        msg["from"] = from_address
    return msg
