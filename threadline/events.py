"""Canonical event envelope shared by every adapter.

Every message that crosses an adapter boundary is normalised into this
envelope before it reaches the event log. Correlation never relies on a
timestamp or an adapter name: a message is identified by ``message_id`` and
positioned by ``conversation_id`` + ``sequence``.
"""

import hashlib

ROLES = ("user", "assistant", "tool", "system")
KINDS = ("message", "handoff", "signal")
ADAPTERS = ("web-chat", "planner-agent", "tool-agent")

# Fields an adapter must supply (the log adds event_id / offset / received_at).
REQUIRED_FIELDS = ("conversation_id", "message_id", "adapter", "role", "kind", "content")


def sha(text, length=16):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def make_idempotency_key(conversation_id, sequence, role, causation_id, kind="message"):
    """Deterministic key for one logical step of the dialog.

    The key names the *slot* a message fills ("the assistant reply at
    position 2, caused by message X"), not the delivery. A network redelivery
    or an adapter retry that re-generates the same step therefore produces
    the same key, even with a new message_id or different wording, and is
    processed only once. Adapter name and timestamps are deliberately excluded
    so the key stays stable when a conversation switches adapters.
    """
    seq = "-" if sequence is None else str(int(sequence))
    return "idk_" + sha(f"{conversation_id}|{seq}|{role}|{causation_id or '-'}|{kind}", 24)


def make_message_id(conversation_id, sequence, role, variant=""):
    """Deterministic message id used by the simulator (real clients send their own)."""
    return "msg_" + sha(f"{conversation_id}|{sequence}|{role}|{variant}", 12)


def validate(event):
    """Return a list of human-readable problems; empty means the envelope is valid."""
    problems = []
    if not isinstance(event, dict):
        return ["envelope must be a JSON object"]
    for field in REQUIRED_FIELDS:
        value = event.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            problems.append(f"missing required field '{field}'")
    if event.get("role") is not None and event.get("role") not in ROLES:
        problems.append(f"unknown role '{event.get('role')}' (expected one of {', '.join(ROLES)})")
    kind = event.get("kind")
    if kind is not None and kind not in KINDS:
        problems.append(f"unknown kind '{kind}' (expected one of {', '.join(KINDS)})")
    seq = event.get("sequence")
    if kind == "signal":
        if seq is not None:
            problems.append("signals are out-of-band and must not carry a sequence number")
    else:
        if isinstance(seq, bool) or not isinstance(seq, int):
            problems.append("sequence must be an integer for dialog events")
        elif seq < 1:
            problems.append("sequence must be >= 1")
    if kind == "handoff" and not event.get("target_adapter"):
        problems.append("handoff events must name a target_adapter")
    if not problems and event.get("idempotency_key"):
        expected = make_idempotency_key(
            event["conversation_id"], seq, event["role"], event.get("causation_id"), kind
        )
        if event["idempotency_key"] != expected:
            problems.append("idempotency_key does not match the deterministic derivation")
    return problems


def normalise(event):
    """Fill derivable fields (idempotency key) and drop unknown keys."""
    allowed = REQUIRED_FIELDS + (
        "sequence", "causation_id", "idempotency_key", "target_adapter", "sent_at", "native",
    )
    out = {k: event.get(k) for k in allowed}
    if not out.get("idempotency_key") and not validate({**out, "idempotency_key": None}):
        out["idempotency_key"] = make_idempotency_key(
            out["conversation_id"], out["sequence"], out["role"], out["causation_id"], out["kind"]
        )
    return out
