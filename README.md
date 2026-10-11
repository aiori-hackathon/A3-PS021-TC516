# Threadline

**Dialog correlation and recovery across agent adapters**: AIORI-3 hackathon, team **A3-PS021-TC516**, problem statement **PS-021: Experimental Dialog Correlation and Recovery Across Agent Adapters**.

Repository: <https://github.com/yugrajmangate-dev/A3-PS021-TC516> · Orientation video: *[YouTube video URL]*

Threadline keeps one AI conversation consistent while it moves between agent adapters that speak different protocols (a web chat widget, an LLM planner agent, a tool-execution agent). Every message is correlated with a stable `conversation_id`, a unique `message_id`, a `sequence` number, a `causation_id` and a deterministic `idempotency_key`, and every delivery is written to an append-only SQLite event log. When things go wrong (adapter timeouts, interrupted hand-offs, duplicates, delays, lost or out-of-order messages, split-brain conflicts), Threadline **replays the log** to rebuild the context, shows exactly why each event was accepted or rejected, and resumes the dialog from a safe checkpoint.

---

## Quick start

Requires **Python 3.10+**. No third-party packages.

```powershell
cd C:\AIORI
python server.py
```

Open **http://localhost:8080** (or http://127.0.0.1:8080). Stop the server with **Ctrl+C**.

| Option | Effect |
|---|---|
| `python server.py --fresh` | Wipe the demo event log before starting (use before recording a demo) |
| `python server.py --port 9000` | Use another port (also `PORT` environment variable) |
| `python server.py --db :memory:` | Throwaway in-memory log; nothing is written to disk |
| `python server.py --host 0.0.0.0` | Expose on the local network (no authentication; use with care) |

The default event log is `data/threadline.db` (created automatically, ignored by git). The **Reset demo data** link in the dashboard also clears it.

**Troubleshooting:** if `python` is not found on Windows, use `py` instead. If port 8080 is busy, run `python server.py --port 9000`. If `localhost` does not load, try `http://127.0.0.1:8080`.

### Run the tests

```powershell
python -m unittest discover -s tests -t . -v
```

49 tests cover the recovery engine, the envelope and idempotency rules, adapter protocol round-trips, the append-only store, every scenario end-to-end, and the HTTP API.

### Run the experiment table (no browser needed)

```powershell
python -m threadline.bench
```

This runs every scenario through the full workflow (run → recover → deliver late → resume → recover twice) and prints the results table used in the presentation.

---

## Demo in 60 seconds

1. Pick the **Chaos (combined)** preset and click **Run experiment**. The timeline shows the *raw arrival order*: duplicates, a conflicting claim, a timeout signal, and one message still in transit.
2. Click **2 · Replay & recover** (keep *Animate replay* on). Watch every log entry get a verdict and the context rebuild in sequence order. Result: `incomplete`, because seq 6 is missing. Resume is **blocked**, not silently skipped.
3. Click **Deliver delayed (1)**, then **Replay & recover** again. The gap closes and the status becomes `recovered with conflicts`. An open hand-off to `tool-agent` is detected.
4. Click **3 · Resume from checkpoint**. `tool-agent` continues from the recovered context (seq 8 and 9).
5. Click **Replay & recover** twice. The dialog is `complete`, and the second run reports *idempotent: same hash*.
6. Click any row in the **Event inspector** to see its correlation metadata, its native wire payload, and the rule that decided its fate.

The full narrated script is in [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md).

## Scenarios

| Preset | Faults | First recovery | How it ends |
|---|---|---|---|
| Happy path | none | `consistent` | complete |
| Interrupted hand-off | adapter timeout at seq 7 | `recovered`, resumable via tool-agent | resume → complete |
| Duplicate storm | redelivery of seq 1 + planner retry of seq 2 | 2 duplicates removed | complete, one reply per step |
| Out of order | seq 3 before 2, 9 before 8 | 2 reordered | complete |
| Delayed message | seq 6 held in transit | `incomplete`, gap [6] | deliver → `recovered` |
| Missing message | seq 5 lost | `incomplete`, gap [5] | stays `incomplete` (dialog: `ended_with_gaps`); surfaced, never invented |
| Split-brain conflict | planner and tool both claim seq 4 | `recovered_with_conflicts` (HANDOFF_OWNERSHIP) | complete |
| Chaos | timeout + duplicate + out-of-order + delayed + conflict | `incomplete` | deliver → recover → resume → complete |

You can also tick any combination of faults (preset becomes `custom`).

## Status vocabulary

The dashboard and API use exactly these terms.

| Term | Values | Meaning |
|---|---|---|
| Recovery **status** | `consistent` | Log was clean: nothing had to be fixed |
| | `recovered` | Duplicates, reordering, invalid events or failure signals were handled; context is complete |
| | `recovered_with_conflicts` | As above, but a sequence-slot conflict was resolved by rule and stays flagged for review |
| | `incomplete` | At least one sequence number is missing; resume is blocked |
| **Dialog state** | `complete` | Ends with an assistant reply and has no gaps |
| | `interrupted_handoff` / `awaiting_handoff` | Ends with an unanswered hand-off (with / without a timeout signal); resumable if there are no gaps |
| | `awaiting_reply` | Ends with a user or tool message |
| | `ended_with_gaps` | Ends with an assistant reply but earlier messages are missing |
| Event **verdict** | `accepted`, `duplicate`, `rejected`, `signal` | Per log entry, with a rule name and reason (rules listed in [ARCHITECTURE.md](docs/ARCHITECTURE.md)) |
| Conversation status (history list) | `awaiting_recovery`, `interrupted`, `resumed`, or the last recovery status | Lifecycle of an experiment between operator actions |

## Architecture (short version)

```
 web-chat (websocket-json)  planner-agent (http-agent)  tool-agent (json-rpc-2.0)
          \                         |                          /
           +----------- native payload -> decode -> canonical envelope
                                    |
                     Gateway (threadline/service.py)
              validate · derive idempotency key · classify live
                                    |
                 Append-only event log (SQLite, triggers block UPDATE/DELETE)
                                    |
             Recovery engine (threadline/recovery.py, pure function)
   validate → deduplicate → resolve conflicts → order → verify gaps → resume point
                                    |
            JSON API (server.py)  →  Operator dashboard (static/)
```

| Path | Responsibility |
|---|---|
| `server.py` | Standard-library HTTP server, routing, JSON errors, static files |
| `threadline/events.py` | Canonical envelope, validation, deterministic idempotency key |
| `threadline/adapters.py` | Three adapters with different wire protocols + codecs |
| `threadline/store.py` | SQLite append-only log, in-flight queue, recovery audit trail |
| `threadline/recovery.py` | The replay/recovery engine (pure, deterministic, explainable) |
| `threadline/simulator.py` | Scripted multi-adapter dialog + composable fault injection |
| `threadline/service.py` | Gateway: ingest, run experiment, recover, deliver, resume |
| `threadline/bench.py` | CLI experiment runner / results table |
| `static/` | Operator dashboard (vanilla JS, no build step) |
| `tests/` | `unittest` suite with deterministic fixtures |

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · API reference: [docs/API.md](docs/API.md) · Design decisions: [docs/IMPLEMENTATION_NOTES.md](docs/IMPLEMENTATION_NOTES.md) · Slides: [docs/PRESENTATION.md](docs/PRESENTATION.md)

## Domain guarantees (and where they are enforced)

| Requirement | How |
|---|---|
| Never identify a message by timestamp or adapter name | Identity = `message_id`; position = `conversation_id` + `sequence`; tie-break = log offset. Test: `test_ordering_never_uses_timestamps` |
| Correlation stable across adapter switches | All three protocols carry the same five fields; the idempotency key excludes the adapter. Tests: `test_round_trip_preserves_correlation_metadata`, `test_correlation_stable_across_adapter_switches` |
| Recovery is idempotent | `replay()` is a pure function; each run stores a `context_hash`; the UI and API report `identical` on re-run. Tests: `test_replay_is_idempotent_and_pure`, `test_recovery_is_idempotent` |
| No duplicate assistant responses | Key = hash(conversation, sequence, role, causation, kind), so a retry with a new id and new wording is still a duplicate. Resume stops chaining on a duplicate. Test: `test_retry_with_new_message_id_yields_single_assistant_response` |
| Out-of-order events reordered safely | Sorted by sequence; any arrival permutation yields the same hash. Test: `test_any_arrival_order_yields_same_context` |
| Missing events surfaced | Gaps listed, status `incomplete`, broken causation flagged, resume blocked. Tests: `test_missing_sequence_is_surfaced`, `test_missing_message_blocks_resume` |
| Every decision explainable from the log | Each log entry gets `verdict` + `rule` + human `reason` (+ warnings). Test: `test_every_event_gets_an_explained_decision` |

## Limitations

- **Simulated adapters.** The three adapters and their protocols are realistic in shape but in-process. No real LLM, WebSocket or JSON-RPC server is called.
- **Scripted continuation.** *Resume* replays the remaining scripted steps from the recovered checkpoint; a real system would re-invoke the agent with the recovered context.
- **Sequence assignment is assumed.** We assume the gateway leases sequence numbers at hand-off time (so retries reuse the slot). Distributed sequencing is not implemented.
- **Single node, single SQLite file.** One process-wide lock; fine for a demo, not for high throughput.
- **Permanent gaps stay open.** There is no "request redelivery from the adapter outbox" or operator "acknowledge gap" action yet.
- **No authentication / tenancy.** The server binds to `127.0.0.1` by default; do not expose it publicly.
- **Fault positions are fixed** (by design, for reproducible results), not randomised.
- Recovery duration is wall-clock for a tiny log (~0.2–1 ms) and is not a performance benchmark.

## Future work

1. Real adapter connectors (WebSocket chat, HTTP LLM agent, JSON-RPC/MCP tool server) behind the same envelope.
2. Outbox pattern + redelivery requests to heal gaps automatically; operator "acknowledge gap" with audit.
3. Snapshots/checkpoints for long conversations so replay is O(new events).
4. Randomised fault injection with seeds and statistical evaluation (context-loss rate, false-correlation rate, recovery latency at scale).
5. Signed envelopes and tenant-scoped authorization; PostgreSQL backend.
6. Distributed sequencer (per-conversation leases) for multi-gateway deployments.

## Team

Team code **A3-PS021-TC516** · Problem statement **PS-021**.

This is a two-member team. The role labels describe the current division of work.

| Member | Name (edit) | Role | Owns in the code |
|---|---|---|---|
| Yugraj Mangate | Yugraj Mangate | Backend, API, SQLite persistence, and project coordination | `server.py`, `threadline/service.py`, `threadline/store.py` |
| Neha Niphade | Neha Niphade | Agent adapters, correlation, recovery, testing, dashboard, and documentation | `threadline/`, `tests/`, `static/`, `docs/` |

## Submission details (fill in before submitting)

| Field | Value |
|---|---|
| Member 1 name | Yugraj Mangate |
| Member 2 name | Neha Niphade |
| GitHub repository URL | <https://github.com/yugrajmangate-dev/A3-PS021-TC516> |
| Orientation video (YouTube) URL | *[YouTube video URL]* (fill in after upload) |
| Presentation | [docs/PRESENTATION.md](docs/PRESENTATION.md) (outline; slides to be built from it) |
| Orientation video script | [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) (under 10 minutes, both members) |

| Neha GitHub username | `Neha-Niphade` (collaborator) |

The YouTube URL remains the only submission placeholder until the orientation video is uploaded.

### Repository access

* `aiori-hackathon` is invited as a collaborator (write access).
* Neha Niphade is a repository collaborator; Yugraj Mangate owns the repository.
* Per the AIORI guideline, ownership is to be transferred to `aiori-hackathon` later, only after the destination account is confirmed and all collaborators have accepted their invitations.
