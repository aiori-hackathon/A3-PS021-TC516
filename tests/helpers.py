"""Deterministic fixtures: hand-built logs with fixed ids, no clocks, no randomness."""

from threadline.events import make_idempotency_key

CID = "conv_fixture"


def ev(offset, seq, adapter="planner-agent", role="assistant", cause=None, msg=None,
       kind="message", content=None, target=None, cid=CID, key=None):
    msg = msg or f"m{seq}"
    return {
        "log_offset": offset,
        "event_id": f"evt_{offset:06d}",
        "conversation_id": cid,
        "message_id": msg,
        "sequence": seq,
        "causation_id": cause,
        "idempotency_key": key or make_idempotency_key(cid, seq, role, cause, kind),
        "adapter": adapter,
        "role": role,
        "kind": kind,
        "target_adapter": target,
        "content": content or f"content of {msg}",
        "sent_at": None,
        "received_at": "2026-10-10T00:00:00.000+00:00",
    }


def linear(n=5):
    """A clean dialog: seq 1..n, each caused by the previous message."""
    adapters = ["web-chat", "planner-agent", "tool-agent"]
    roles = {"web-chat": "user", "planner-agent": "assistant", "tool-agent": "tool"}
    out = []
    for i in range(1, n + 1):
        a = adapters[(i - 1) % 3]
        out.append(ev(i, i, adapter=a, role=roles[a], cause=f"m{i - 1}" if i > 1 else None))
    return out


def renumber(events):
    """Re-assign log offsets so the list order becomes the arrival order."""
    return [{**e, "log_offset": i + 1, "event_id": f"evt_{i + 1:06d}"} for i, e in enumerate(events)]
