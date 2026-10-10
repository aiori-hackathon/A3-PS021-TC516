"""Deterministic multi-adapter dialog with composable fault injection.

The scripted dialog is a support conversation that crosses three adapters:

    web-chat (user) -> planner-agent -> tool-agent -> planner-agent -> web-chat ...

Faults are applied to the *delivery plan* (what reaches the gateway, and in
which order). Positions are fixed rather than random so every run of a
scenario produces the same log, the same metrics and the same context hash:
evaluators see exactly what the docs describe, and the tests can assert it.
"""

from datetime import datetime, timedelta, timezone

from .events import make_idempotency_key, make_message_id

# (sequence, adapter, role, kind, target_adapter, content)
SCRIPT = [
    (1, "web-chat", "user", "message", None,
     "My order #4821 arrived damaged. Can I get a replacement?"),
    (2, "planner-agent", "assistant", "message", None,
     "Sorry about that! Let me check order #4821 against the replacement policy."),
    (3, "planner-agent", "assistant", "handoff", "tool-agent",
     "lookup_order(order_id=4821)"),
    (4, "tool-agent", "tool", "message", None,
     "Order 4821: delivered 2026-10-02; damage claim eligible (30-day window)."),
    (5, "planner-agent", "assistant", "message", None,
     "Good news: order #4821 is eligible. Shall I ship the replacement to the same address?"),
    (6, "web-chat", "user", "message", None,
     "Yes, same address please."),
    (7, "planner-agent", "assistant", "handoff", "tool-agent",
     "create_replacement(order_id=4821, address=on_file)"),
    (8, "tool-agent", "tool", "message", None,
     "Replacement RPL-2207 created; ships within 2 business days."),
    (9, "planner-agent", "assistant", "message", None,
     "Done! Replacement RPL-2207 ships within 2 business days. Anything else I can help with?"),
]

FAULTS = {
    "adapter_timeout": {
        "label": "Adapter timeout / interrupted hand-off",
        "description": "tool-agent never answers the second hand-off (seq 7). seq 8-9 are never "
                       "produced and the gateway logs an adapter.timeout signal.",
    },
    "duplicate": {
        "label": "Duplicate messages",
        "description": "web-chat redelivers seq 1 (same message_id) and planner-agent retries seq 2 "
                       "with a new message_id and different wording (same idempotency key).",
    },
    "out_of_order": {
        "label": "Out-of-order delivery",
        "description": "seq 3 arrives before seq 2, and seq 9 before seq 8.",
    },
    "delayed": {
        "label": "Delayed message",
        "description": "seq 6 (user confirmation) is held in flight until the operator clicks "
                       "'Deliver delayed'; it then arrives late.",
    },
    "missing": {
        "label": "Missing sequence number",
        "description": "seq 5 (planner reply) is lost in transit and never arrives.",
    },
    "conflict": {
        "label": "Conflicting events (split-brain)",
        "description": "planner-agent assumes tool-agent hung and answers seq 4 itself; the real "
                       "tool-agent result also claims seq 4.",
    },
}

SCENARIOS = {
    "happy_path": {"label": "Happy path", "faults": [],
                   "description": "Clean dialog across all three adapters. Baseline."},
    "interrupted_handoff": {"label": "Interrupted hand-off", "faults": ["adapter_timeout"],
                            "description": "tool-agent times out mid hand-off; recover and resume."},
    "duplicates": {"label": "Duplicate storm", "faults": ["duplicate"],
                   "description": "Redelivery and adapter retry; no duplicate replies survive."},
    "out_of_order": {"label": "Out of order", "faults": ["out_of_order"],
                     "description": "Events arrive shuffled; recovery reorders by sequence."},
    "delayed": {"label": "Delayed message", "faults": ["delayed"],
                "description": "A gap appears, then closes when the late message lands."},
    "missing": {"label": "Missing message", "faults": ["missing"],
                "description": "A permanent gap is surfaced and resume is blocked."},
    "conflict": {"label": "Split-brain conflict", "faults": ["conflict"],
                 "description": "Two adapters claim one slot; hand-off ownership decides."},
    "chaos": {"label": "Chaos (combined)",
              "faults": ["adapter_timeout", "duplicate", "out_of_order", "delayed", "conflict"],
              "description": "Everything at once: deliver delayed, recover, resume, recover."},
}

BASE_TIME = datetime(2026, 10, 10, 9, 0, 0, tzinfo=timezone.utc)


def _ts(step_ms):
    return (BASE_TIME + timedelta(milliseconds=step_ms)).isoformat(timespec="milliseconds")


def build_envelope(conversation_id, step, causation_id, variant="", content=None):
    seq, adapter, role, kind, target, text = step
    return {
        "conversation_id": conversation_id,
        "message_id": make_message_id(conversation_id, seq, role + kind, variant),
        "sequence": seq,
        "causation_id": causation_id,
        "idempotency_key": make_idempotency_key(conversation_id, seq, role, causation_id, kind),
        "adapter": adapter,
        "role": role,
        "kind": kind,
        "target_adapter": target,
        "content": content or text,
        "sent_at": _ts(seq * 400 + (37 if variant else 0)),
    }


def chain(conversation_id, steps, causation_id=None, variant=""):
    """Envelopes for consecutive steps, each caused by the previous message."""
    out = []
    for step in steps:
        env = build_envelope(conversation_id, step, causation_id, variant)
        out.append(env)
        causation_id = env["message_id"]
    return out


def timeout_signal(conversation_id, handoff):
    return {
        "conversation_id": conversation_id,
        "message_id": make_message_id(conversation_id, "signal", "timeout", handoff["message_id"]),
        "sequence": None,
        "causation_id": handoff["message_id"],
        "idempotency_key": make_idempotency_key(conversation_id, None, "system", handoff["message_id"], "signal"),
        "adapter": handoff["target_adapter"],
        "role": "system",
        "kind": "signal",
        "target_adapter": None,
        "content": f"adapter.timeout: no response from {handoff['target_adapter']} within 5000 ms "
                   f"for hand-off seq {handoff['sequence']}",
        "sent_at": _ts(handoff["sequence"] * 400 + 5000),
    }


def plan(conversation_id, faults):
    """Return (deliveries, held). Each delivery is (envelope, note) in arrival order."""
    faults = set(faults)
    envs = chain(conversation_id, SCRIPT)
    by_seq = {e["sequence"]: e for e in envs}
    deliveries = [(e, None) for e in envs]

    def index_of(seq):
        return next(i for i, (e, note) in enumerate(deliveries)
                    if e["sequence"] == seq and note is None)

    if "adapter_timeout" in faults:
        deliveries = [(e, n) for e, n in deliveries if e["sequence"] <= 7]
        deliveries.append((timeout_signal(conversation_id, by_seq[7]), "gateway: adapter timeout"))

    if "conflict" in faults:
        rogue = build_envelope(
            conversation_id, (4, "planner-agent", "assistant", "message", None, ""),
            by_seq[3]["message_id"], variant="split-brain",
            content="I couldn't reach the order system, so please try again later.",
        )
        deliveries.insert(index_of(4), (rogue, "fault: split-brain claim on seq 4"))

    if "duplicate" in faults:
        retry = build_envelope(conversation_id, SCRIPT[1], by_seq[1]["message_id"], variant="retry",
                               content="Sorry to hear that. I'll look up order #4821 and our replacement policy.")
        deliveries.insert(index_of(2) + 1, (retry, "fault: planner retry (new message_id)"))
        deliveries.append((dict(by_seq[1]), "fault: web-chat redelivery"))

    held = []
    if "missing" in faults:
        deliveries.pop(index_of(5))
    if "delayed" in faults:
        held.append((deliveries.pop(index_of(6))[0], "fault: delayed in transit"))

    if "out_of_order" in faults:
        for a, b in ((2, 3), (8, 9)):
            if any(e["sequence"] == b and n is None for e, n in deliveries):
                i, j = index_of(a), index_of(b)
                deliveries[i], deliveries[j] = deliveries[j], deliveries[i]
                deliveries[i] = (deliveries[i][0], deliveries[i][1] or f"fault: overtook seq {a}")
    return deliveries, held


def continuation(conversation_id, next_sequence, causation_id):
    """Steps a resumed adapter produces after recovery (from the scripted dialog)."""
    steps = [s for s in SCRIPT if s[0] >= next_sequence]
    return chain(conversation_id, steps, causation_id, variant="resumed")
