"""Deterministic replay of the event log into a recovered dialog context.

``replay(events)`` is a pure function: the same log always yields the same
context, the same decisions and the same ``context_hash``. That is what makes
recovery idempotent and every decision explainable.

Pipeline (each step records a decision with a rule name and a reason):

1. VALIDATE    reject envelopes that are malformed or carry a forged key.
2. DEDUPLICATE first arrival wins; later copies with the same message_id
               (redelivery) or the same idempotency key (adapter retry) are
               dropped, so a retry never yields a second assistant response.
3. RESOLVE     several *different* steps claiming one sequence slot are a
               conflict. Winner = the adapter the previous hand-off leased the
               slot to, else the claim causally linked to the previous message,
               else the earliest arrival. Losers are kept in the log, flagged.
4. ORDER       accepted events are sorted by sequence (never by timestamp or
               adapter); events that arrived after a higher sequence are
               counted as reordered.
5. VERIFY      sequence gaps and broken causation links are surfaced, not
               papered over. A gap makes the context ``incomplete``.
6. RESUME      if the dialog ends in an unanswered hand-off, report where and
               with which adapter it can safely resume.
"""

from .events import sha, validate

RULES = {
    "IN_ORDER": "Accepted: arrived in sequence order.",
    "REORDERED": "Accepted: arrived after a higher sequence; placed by sequence number.",
    "INVALID_ENVELOPE": "Rejected: envelope failed validation.",
    "FOREIGN_CONVERSATION": "Rejected: envelope belongs to a different conversation.",
    "DUPLICATE_DELIVERY": "Removed: same message_id already in the log (network redelivery).",
    "DUPLICATE_RETRY": "Removed: same idempotency key as an earlier event (adapter retry).",
    "MESSAGE_ID_REUSED": "Rejected: message_id reused for a different dialog step.",
    "HANDOFF_OWNERSHIP": "Conflict resolved: previous hand-off leased this slot to the winning adapter.",
    "CAUSAL_LINK": "Conflict resolved: only the winner is caused by the previous message.",
    "FIRST_ARRIVAL": "Conflict resolved: no ownership or causal evidence; earliest log offset wins.",
    "CONFLICT_LOST": "Rejected: lost a sequence-slot conflict.",
    "SIGNAL": "Recorded: out-of-band failure signal; not part of the dialog context.",
}


def _short(text, n=60):
    text = text or ""
    return text if len(text) <= n else text[: n - 3] + "..."


def replay(events, conversation_id=None):
    log = sorted(events, key=lambda e: e["log_offset"])
    decisions = {}

    def decide(e, verdict, rule, reason, **extra):
        decisions[e["event_id"]] = {
            "event_id": e["event_id"],
            "log_offset": e["log_offset"],
            "message_id": e.get("message_id"),
            "sequence": e.get("sequence"),
            "adapter": e.get("adapter"),
            "verdict": verdict,
            "rule": rule,
            "reason": reason,
            "warnings": [],
            **extra,
        }

    # 1. VALIDATE ---------------------------------------------------------
    valid = []
    for e in log:
        if conversation_id and e.get("conversation_id") != conversation_id:
            decide(e, "rejected", "FOREIGN_CONVERSATION",
                   f"conversation_id {e.get('conversation_id')} != {conversation_id}")
            continue
        problems = validate(e)
        if problems:
            decide(e, "rejected", "INVALID_ENVELOPE", "; ".join(problems))
        else:
            valid.append(e)

    # 2. DEDUPLICATE (first arrival wins) --------------------------------
    by_msg, by_key, alias, unique = {}, {}, {}, []
    duplicates = 0
    for e in valid:
        first = by_msg.get(e["message_id"])
        if first is not None:
            if first["idempotency_key"] == e["idempotency_key"]:
                duplicates += 1
                decide(e, "duplicate", "DUPLICATE_DELIVERY",
                       f"message {e['message_id']} was already logged as {first['event_id']} "
                       f"(offset {first['log_offset']}); redelivery ignored.", duplicate_of=first["event_id"])
            else:
                decide(e, "rejected", "MESSAGE_ID_REUSED",
                       f"message_id {e['message_id']} already names a different step "
                       f"({first['event_id']}); refusing to overwrite it.")
            continue
        first = by_key.get(e["idempotency_key"])
        if first is not None:
            duplicates += 1
            alias[e["message_id"]] = first["message_id"]
            by_msg[e["message_id"]] = first
            differs = " Retry wording differed and was discarded." if e["content"] != first["content"] else ""
            decide(e, "duplicate", "DUPLICATE_RETRY",
                   f"idempotency key {e['idempotency_key'][:12]}... matches {first['event_id']} "
                   f"(same conversation, sequence, role and cause). Kept the first arrival so no "
                   f"duplicate {e['role']} response is produced.{differs}", duplicate_of=first["event_id"])
            continue
        by_msg[e["message_id"]] = e
        by_key[e["idempotency_key"]] = e
        unique.append(e)

    def canonical(message_id):
        return alias.get(message_id, message_id)

    signals = [e for e in unique if e["kind"] == "signal"]
    dialog = [e for e in unique if e["kind"] != "signal"]

    # 3. RESOLVE sequence-slot conflicts (in sequence order) -------------
    slots = {}
    for e in dialog:
        slots.setdefault(e["sequence"], []).append(e)
    accepted_by_seq, conflicts, won_conflict = {}, [], {}
    for seq in sorted(slots):
        cands = slots[seq]
        winner, rule, why = cands[0], None, ""
        if len(cands) > 1:
            prev = accepted_by_seq.get(seq - 1)
            if prev is not None and prev["kind"] == "handoff":
                owned = [c for c in cands if c["adapter"] == prev["target_adapter"]]
                if len(owned) == 1:
                    winner, rule = owned[0], "HANDOFF_OWNERSHIP"
                    why = (f"hand-off {prev['message_id']} (seq {prev['sequence']}) leased seq {seq} "
                           f"to {prev['target_adapter']}")
            if rule is None and prev is not None:
                linked = [c for c in cands if canonical(c["causation_id"]) == prev["message_id"]]
                if len(linked) == 1:
                    winner, rule = linked[0], "CAUSAL_LINK"
                    why = f"only {linked[0]['event_id']} is caused by seq {seq - 1} ({prev['message_id']})"
            if rule is None:
                winner, rule = cands[0], "FIRST_ARRIVAL"
                why = f"no ownership or causal evidence; {cands[0]['event_id']} has the lowest log offset"
            losers = [c for c in cands if c is not winner]
            for c in losers:
                decide(c, "rejected", "CONFLICT_LOST",
                       f"seq {seq} was also claimed by {winner['adapter']} ({winner['event_id']}), "
                       f"which won via {rule}: {why}.", conflict_with=winner["event_id"])
            conflicts.append({
                "sequence": seq, "rule": rule, "reason": why,
                "winner": winner["event_id"], "losers": [c["event_id"] for c in losers],
            })
        accepted_by_seq[seq] = winner
        if rule:
            won_conflict[winner["event_id"]] = (rule, why)

    # 4. ORDER + detect reordering ---------------------------------------
    accepted = [accepted_by_seq[s] for s in sorted(accepted_by_seq)]
    reordered = set()
    highest = 0
    for e in sorted(accepted, key=lambda x: x["log_offset"]):
        if e["sequence"] < highest:
            reordered.add(e["event_id"])
        highest = max(highest, e["sequence"])

    # 5. VERIFY gaps and causation ---------------------------------------
    max_seq = accepted[-1]["sequence"] if accepted else 0
    gaps = [n for n in range(1, max_seq + 1) if n not in accepted_by_seq]
    seq_of = {e["message_id"]: e["sequence"] for e in accepted}
    context = []
    for e in accepted:
        rule = "REORDERED" if e["event_id"] in reordered else "IN_ORDER"
        reason = RULES[rule]
        if e["event_id"] in reordered:
            reason = f"arrived at offset {e['log_offset']}, after a higher sequence; placed at seq {e['sequence']}."
        conflict = won_conflict.get(e["event_id"])
        if conflict:
            reason += f" Won seq {e['sequence']} conflict via {conflict[0]}: {conflict[1]}."
        warnings = []
        cause = canonical(e["causation_id"]) if e["causation_id"] else None
        if e["sequence"] > 1:
            if cause is None:
                warnings.append("no causation_id: cannot prove which message this answers")
            elif cause not in seq_of:
                warnings.append(f"causation_id {cause} is not in the recovered context (cause lost in a gap?)")
            elif seq_of[cause] >= e["sequence"]:
                warnings.append(f"causation_id points forward to seq {seq_of[cause]}")
        if e["sequence"] - 1 in gaps:
            warnings.append(f"follows missing seq {e['sequence'] - 1}: context before this point is incomplete")
        decide(e, "accepted", rule, reason)
        decisions[e["event_id"]]["warnings"] = warnings
        context.append({
            "sequence": e["sequence"], "message_id": e["message_id"], "event_id": e["event_id"],
            "adapter": e["adapter"], "role": e["role"], "kind": e["kind"],
            "target_adapter": e.get("target_adapter"), "content": e["content"],
            "causation_id": cause, "reordered": e["event_id"] in reordered, "warnings": warnings,
        })

    for s in signals:
        decide(s, "signal", "SIGNAL",
               f"{s['adapter']}: {_short(s['content'], 90)} (refers to {s.get('causation_id') or 'n/a'})")

    # 6. RESUME point ------------------------------------------------------
    last = accepted[-1] if accepted else None
    open_handoff = last if last is not None and last["kind"] == "handoff" else None
    timeouts = [s["event_id"] for s in signals
                if open_handoff is not None and canonical(s.get("causation_id")) == open_handoff["message_id"]]
    if last is None:
        dialog_state = "empty"
    elif open_handoff is not None:
        dialog_state = "interrupted_handoff" if timeouts else "awaiting_handoff"
    elif last["role"] == "assistant":
        dialog_state = "ended_with_gaps" if gaps else "complete"
    else:
        dialog_state = "awaiting_reply"

    rejected = sum(1 for d in decisions.values() if d["verdict"] == "rejected")
    if not log:
        status = "empty"
    elif gaps:
        status = "incomplete"
    elif conflicts:
        status = "recovered_with_conflicts"
    elif duplicates or reordered or rejected or signals:
        status = "recovered"
    else:
        status = "consistent"

    blockers = []
    if gaps:
        blockers.append(f"missing sequence {', '.join(map(str, gaps))}; resuming would build on an incomplete context")
    if open_handoff is None:
        blockers.append(f"no open hand-off to resume (dialog state: {dialog_state})")
    resume = {
        "resumable": not blockers,
        "blockers": blockers,
        "next_sequence": max_seq + 1,
        "resume_adapter": open_handoff["target_adapter"] if open_handoff else None,
        "causation_id": last["message_id"] if last else None,
        "open_handoff": open_handoff["event_id"] if open_handoff else None,
        "timeout_signals": timeouts,
    }

    fingerprint = "|".join(f"{c['sequence']}:{c['message_id']}" for c in context) + "#gaps:" + ",".join(map(str, gaps))
    return {
        "status": status,
        "dialog_state": dialog_state,
        "metrics": {
            "total_events": len(log),
            "accepted_events": len(accepted),
            "duplicates_removed": duplicates,
            "rejected_events": rejected,
            "conflicts": len(conflicts),
            "reordered_events": len(reordered),
            "sequence_gaps": len(gaps),
            "signals": len(signals),
        },
        "gaps": gaps,
        "conflicts": conflicts,
        "context": context,
        "context_hash": sha(fingerprint, 16),
        "resume": resume,
        "decisions": [decisions[e["event_id"]] for e in log],
        "log_watermark": log[-1]["log_offset"] if log else 0,
    }
