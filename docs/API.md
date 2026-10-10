# Threadline HTTP API

Base URL: `http://localhost:8080`. All requests and responses are JSON (`Content-Type: application/json`). Bodies are limited to 1 MB.

## Errors

Every error has the same shape and a meaningful HTTP status:

```json
{ "error": { "code": "not_resumable", "message": "Conversation cannot be resumed safely.",
             "details": ["missing sequence 6; resuming would build on an incomplete context"] } }
```

| Status | Codes |
|---|---|
| 400 | `invalid_json`, `invalid_body`, `invalid_length`, `unknown_scenario`, `unknown_fault`, `invalid_faults`, `invalid_envelope`, `unknown_adapter`, `conversation_mismatch` |
| 404 | `not_found`, `conversation_not_found` |
| 405 | `method_not_allowed` (the message names the allowed verb; `HEAD` gets an empty 405) |
| 409 | `not_resumable`, `no_simulated_adapter` |
| 413 | `body_too_large` |
| 500 | `internal_error` (logged on the server console) |

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Liveness and version |
| GET | `/api/scenarios` | Scenario presets, fault catalogue, adapters and protocols |
| POST | `/api/experiments` | Run a simulated multi-adapter dialog with injected faults |
| GET | `/api/conversations` | Recent conversations (newest first) |
| GET | `/api/conversations/{id}` | Snapshot: conversation, raw log, in-flight messages, recovery history |
| POST | `/api/conversations/{id}/recover` | Replay the log and return the recovered context and decisions |
| POST | `/api/conversations/{id}/deliver` | Release messages held in transit (late arrival) |
| POST | `/api/conversations/{id}/resume` | Continue an interrupted hand-off from the recovered checkpoint |
| POST | `/api/conversations/{id}/events` | Ingest one external canonical envelope |
| GET | `/api/conversations/{id}/recoveries` | Audit trail of recovery runs |
| POST | `/api/reset` | Delete all demo data |

---

### POST /api/experiments

```json
{ "scenario": "chaos" }
{ "faults": ["duplicate", "missing"] }
{ "inject_interruption": true }
```

`scenario` is one of `happy_path`, `interrupted_handoff`, `duplicates`, `out_of_order`, `delayed`, `missing`, `conflict`, `chaos`. `faults` is any subset of `adapter_timeout`, `duplicate`, `out_of_order`, `delayed`, `missing`, `conflict` (it overrides the preset's faults). `inject_interruption` is kept for compatibility with the first prototype and equals `["adapter_timeout"]`.

Response: a conversation snapshot (see below).

### GET /api/conversations/{id}

```json
{
  "conversation": { "conversation_id": "conv_6f8845c18b", "scenario": "chaos",
                    "faults": ["adapter_timeout", "duplicate", "out_of_order", "delayed", "conflict"],
                    "status": "interrupted", "created_at": "...", "updated_at": "..." },
  "events": [ {
      "log_offset": 12, "event_id": "evt_000012", "conversation_id": "conv_6f8845c18b",
      "message_id": "msg_7210d8035a1c", "sequence": 3, "causation_id": "msg_467323d871ab",
      "idempotency_key": "idk_91e2c238580a...", "adapter": "planner-agent", "role": "assistant",
      "kind": "handoff", "target_adapter": "tool-agent", "content": "lookup_order(order_id=4821)",
      "sent_at": "2026-10-10T09:00:01.200+00:00", "received_at": "...",
      "native": { "protocol": "http-agent", "payload": { "headers": { "X-Sequence": "3", "...": "..." }, "body": { } } },
      "note": "fault: overtook seq 2" } ],
  "in_flight": [ { "id": 1, "reason": "fault: delayed in transit", "envelope": { "sequence": 6, "...": "..." } } ],
  "recoveries": [ { "id": 1, "context_hash": "8993b4037f54193e", "status": "incomplete", "duration_ms": 0.31, "log_watermark": 20, "created_at": "..." } ],
  "needs_recovery": false
}
```

`events` is the raw append-only log in arrival order, duplicates and conflicts included.

### POST /api/conversations/{id}/recover

```json
{
  "conversation_id": "conv_6f8845c18b", "scenario": "chaos",
  "status": "incomplete", "dialog_state": "interrupted_handoff",
  "duration_ms": 0.31,
  "metrics": { "total_events": 10, "accepted_events": 6, "duplicates_removed": 2, "rejected_events": 1,
               "conflicts": 1, "reordered_events": 1, "sequence_gaps": 1, "signals": 1 },
  "gaps": [6],
  "conflicts": [ { "sequence": 4, "rule": "HANDOFF_OWNERSHIP", "winner": "evt_000016", "losers": ["evt_000015"],
                   "reason": "hand-off msg_7210... (seq 3) leased seq 4 to tool-agent" } ],
  "context": [ { "sequence": 1, "message_id": "msg_...", "event_id": "evt_000011", "adapter": "web-chat",
                 "role": "user", "kind": "message", "target_adapter": null, "content": "...",
                 "causation_id": null, "reordered": false, "warnings": [] } ],
  "decisions": [ { "event_id": "evt_000014", "log_offset": 14, "message_id": "msg_...", "sequence": 2,
                   "adapter": "planner-agent", "verdict": "duplicate", "rule": "DUPLICATE_RETRY",
                   "reason": "idempotency key idk_1f3c2bfd... matches evt_000013 ...", "warnings": [],
                   "duplicate_of": "evt_000013" } ],
  "resume": { "resumable": false, "blockers": ["missing sequence 6; resuming would build on an incomplete context"],
              "next_sequence": 8, "resume_adapter": "tool-agent", "causation_id": "msg_ddfb...",
              "open_handoff": "evt_000018", "timeout_signals": ["evt_000019"] },
  "context_hash": "8993b4037f54193e",
  "log_watermark": 20,
  "idempotency_check": { "previous_runs": 0, "verdict": "first_run" }
}
```

* `status` ∈ `consistent`, `recovered`, `recovered_with_conflicts`, `incomplete`, `empty`; `dialog_state` ∈ `complete`, `interrupted_handoff`, `awaiting_handoff`, `awaiting_reply`, `ended_with_gaps`, `empty` (see the README's status vocabulary).
* `verdict` ∈ `accepted`, `duplicate`, `rejected`, `signal`. Rule names are listed in [ARCHITECTURE.md](ARCHITECTURE.md#5-recovery-algorithm-threadlinerecoverypy).
* `idempotency_check.verdict` ∈ `first_run`, `identical` (same log → same hash), `log_changed` (with `new_events`), `DIVERGED` (should never happen; would indicate a bug).

### POST /api/conversations/{id}/deliver

Releases held messages through their adapter. Response: `{ "delivered": 1, "outcomes": [ {"outcome": "accepted", "processed": true, "event": {...}} ], "conversation": <snapshot> }`.

### POST /api/conversations/{id}/resume

Re-runs the replay; if `resume.resumable`, the target adapter continues from `next_sequence` with `causation_id` = last recovered message. Chaining stops at the first non-accepted outcome, so a duplicate never triggers another reply. Returns `{ "resumed_from": <resume point>, "appended": 2, "outcomes": [...], "conversation": <snapshot> }`, or **409** `not_resumable` with the blockers.

### POST /api/conversations/{id}/events

Ingest an envelope from an external client. The conversation is created on first use (scenario `external`). `idempotency_key` may be omitted; it is derived. If supplied, it must match the derivation.

```powershell
$body = @{ message_id = "u1"; sequence = 1; adapter = "web-chat"; role = "user"; content = "hi" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:8080/api/conversations/conv_demo/events -ContentType "application/json" -Body $body
```

Response: `{ "outcome": "accepted" | "duplicate" | "conflict", "processed": true|false, "event": {...} }`. Every delivery is appended (the log is evidence); `processed` tells the caller whether to act on it.
