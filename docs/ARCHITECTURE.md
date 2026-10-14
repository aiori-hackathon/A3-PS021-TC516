# Threadline architecture

## 1. The problem in one paragraph

A single user conversation is handled by several agent adapters: a web chat front end, a planner agent, a tool agent. Each speaks its own protocol, and messages between them can time out, be retried, arrive twice, arrive late, arrive out of order, or never arrive. If each adapter keeps its own idea of "the conversation", a failure during a hand-off leaves the dialog inconsistent: lost context, repeated answers, or two agents answering the same question. Threadline makes the **event log, not any adapter, the owner of the conversation**, and rebuilds the context deterministically from that log.

## 2. Components

```
 ┌───────────┐   ┌───────────────┐   ┌────────────┐
 │ web-chat  │   │ planner-agent │   │ tool-agent │     simulated adapters
 │ websocket │   │  HTTP+headers │   │ JSON-RPC 2 │     (threadline/adapters.py)
 └─────┬─────┘   └──────┬────────┘   └─────┬──────┘
       └──── native payload ──── decode ───┘
                        │  canonical envelope
                ┌───────▼────────┐
                │    Gateway     │  validate, derive idempotency key,
                │ (service.py)   │  classify live (accepted/duplicate/conflict)
                └───────┬────────┘
                ┌───────▼────────┐
                │  Event log     │  SQLite, append-only (triggers),
                │  (store.py)    │  every delivery kept as evidence
                └───────┬────────┘
                ┌───────▼────────┐
                │ Recovery engine│  pure replay() → context, decisions,
                │ (recovery.py)  │  gaps, conflicts, resume point, hash
                └───────┬────────┘
          JSON API (server.py) → Operator dashboard (static/)
```

The random fault campaign (`campaign.py`) calls the same `replay()` directly on in-memory logs. It skips the gateway and the SQLite log, so thousands of trials run in about a second. See [EVALUATION.md](EVALUATION.md).

Supporting tables: `in_flight` (messages the simulated network is holding back), `recoveries` (audit trail of every recovery run, with its context hash, so idempotency can be checked).

## 3. The canonical envelope

| Field | Meaning | Who sets it |
|---|---|---|
| `conversation_id` | Stable id of the dialog; never changes across adapters | origin adapter |
| `message_id` | Unique id of one logical message | producing adapter |
| `sequence` | Position in the dialog (1, 2, 3…); leased by the gateway at hand-off | gateway / adapter |
| `causation_id` | `message_id` of the message this one responds to | producing adapter |
| `idempotency_key` | `sha256(conversation_id │ sequence │ role │ causation_id │ kind)` | derived (verified on replay) |
| `adapter`, `role`, `kind` | Producer, speaker role (`user/assistant/tool/system`), `message/handoff/signal` | adapter |
| `target_adapter` | For hand-offs: which adapter owns the next slot | planner |
| `event_id`, `log_offset`, `received_at` | Identity of the *delivery* in the log | event store |

Two identities on purpose: `message_id` identifies the **message**; `event_id`/`log_offset` identify the **delivery**. A message delivered twice is one message and two log entries.

The idempotency key names the **dialog slot**, not the delivery or the wording, and it deliberately excludes the adapter name and any timestamp. So:

* a network redelivery (same `message_id`) and
* an adapter retry (new `message_id`, possibly different wording, same slot)

both collapse to one accepted event, and no step can produce two assistant replies.

`signal` events (e.g. `adapter.timeout`) are raised by the gateway itself. They carry no sequence number, are never part of the context, but explain why a hand-off is open.

## 4. Each adapter speaks a different protocol

| Adapter | Protocol | Where the correlation fields live |
|---|---|---|
| web-chat | `websocket-json` | `session`, `idem`, `msg.id`, `msg.seq`, `msg.reply_to` |
| planner-agent | `http-agent` | `X-Conversation-Id`, `X-Message-Id`, `X-Sequence`, `X-Causation-Id`, `Idempotency-Key` headers |
| tool-agent | `json-rpc-2.0` | `id`, `params.conversation`, `params.seq`, `params.cause`, `params.idempotency_key` |

The gateway decodes each into the canonical envelope. The raw native payload is stored with the event and shown in the Event inspector, so evaluators can see the same correlation surviving three different wire formats.

## 5. Recovery algorithm (`threadline/recovery.py`)

`replay(events)` takes the log in arrival order and returns the recovered context. It is a **pure function**: no I/O, no clock, no randomness, no mutation of its input.

| Step | What it does | Rules (shown in the Decision log) |
|---|---|---|
| 1. Validate | Required fields, known role/kind, sequence ≥ 1, hand-offs name a target, key matches derivation, conversation matches | `INVALID_ENVELOPE`, `FOREIGN_CONVERSATION` |
| 2. Deduplicate | First arrival wins. Same `message_id` → redelivery; same key with new id → retry; same id for a different step → rejected | `DUPLICATE_DELIVERY`, `DUPLICATE_RETRY`, `MESSAGE_ID_REUSED` |
| 3. Resolve conflicts | Several *different* steps claim one sequence slot. Winner, in order: the adapter the previous hand-off leased the slot to → the claim causally linked to the previous message → the lowest log offset | `HANDOFF_OWNERSHIP`, `CAUSAL_LINK`, `FIRST_ARRIVAL`, `CONFLICT_LOST` |
| 4. Order | Sort by `sequence`. Events that arrived after a higher sequence are marked reordered | `IN_ORDER`, `REORDERED` |
| 5. Verify | Missing sequence numbers become gaps. Each event's `causation_id` must point to an earlier accepted message; otherwise a warning | warnings on events |
| 6. Resume point | If the last accepted event is a hand-off with no answer, the dialog is `interrupted_handoff` (or `awaiting_handoff`). Resume is allowed only when there are no gaps | `resume.resumable`, `resume.blockers` |

Outputs: `status` (`consistent`, `recovered`, `recovered_with_conflicts`, `incomplete`, `empty`), `dialog_state` (`complete`, `interrupted_handoff`, `awaiting_handoff`, `awaiting_reply`, `ended_with_gaps`, `empty`), `metrics`, `gaps`, `conflicts`, `context`, `decisions` (one per log entry), `resume`, and `context_hash` (sha256 over accepted `sequence:message_id` pairs and gaps).

### Why gaps block resume

If seq 6 is missing and an agent resumes at seq 8, it would answer without knowing what the user said at seq 6. That is exactly the silent context loss PS-021 is about. Threadline surfaces the gap (`incomplete`), flags the events whose cause is missing, and refuses to resume until the gap closes (e.g. a delayed message arrives).

### Idempotency of recovery

* Recovery never writes to the event log; it only appends an audit row to `recoveries`.
* Re-running recovery on an unchanged log yields the same `context_hash`; the API reports `idempotency_check.verdict = "identical"`.
* If the log grew (late delivery, resume), the verdict is `log_changed`, and the new hash reflects the new evidence.

## 6. Operator workflow

```
Run experiment ─► Replay & recover ─┬─ gap? ─► Deliver delayed ─► Replay & recover
                                    └─ open hand-off? ─► Resume from checkpoint ─► Replay & recover (complete)
```

The gateway also exposes `POST /api/conversations/{id}/events`, so an external client can push its own envelopes (see [API.md](API.md)) and use the same recovery.

## 7. Concurrency and persistence

* One SQLite connection guarded by a re-entrant lock; every multi-step operation (run, deliver, resume, recover) holds it, so a replay never sees a half-written experiment.
* `BEFORE UPDATE` / `BEFORE DELETE` triggers make the `events` table physically append-only. *Reset demo data* drops and recreates tables; it is an explicit operator action.
