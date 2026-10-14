# Threadline

[![tests](https://github.com/aiori-hackathon/A3-PS021-TC516/actions/workflows/tests.yml/badge.svg)](https://github.com/aiori-hackathon/A3-PS021-TC516/actions/workflows/tests.yml)

**Dialog correlation and recovery across agent adapters**: AIORI-3 hackathon, team **A3-PS021-TC516**, problem statement **PS-021: Experimental Dialog Correlation and Recovery Across Agent Adapters**.

Repository: <https://github.com/aiori-hackathon/A3-PS021-TC516> · Live demo: <https://threadline-brown.vercel.app> · Orientation video: <https://youtu.be/4IXSzTUrEOA> · Presentation: [PDF](docs/Threadline-AIORI3-PS021.pdf)

## For evaluators (2 minutes)

| | |
|---|---|
| **Try it** | Open the [live demo](https://threadline-brown.vercel.app), pick **Chaos (combined)**, click **Run experiment**, then **2 · Replay & recover**. Scroll to **4 · Random fault campaign** and click **Run campaign**. |
| **Verify locally** | `python -m unittest discover -s tests -t .` (56 tests) · `python -m threadline.bench` (8 scenarios) · `python -m threadline.campaign` (1000 random trials). Python 3.10+, no packages to install. |
| **Main result** | Over 1000 seeded random fault trials, Threadline rebuilt the exact context in **100%** of trials with **0** duplicate replies and surfaced **every** lost message. Naive recovery on the same logs was exact in 10–18% of trials, leaked 589 duplicate replies and hid every loss. [Details](docs/EVALUATION.md) |
| **Watch / read** | [Orientation video](https://youtu.be/4IXSzTUrEOA) · [Presentation (PDF)](docs/Threadline-AIORI3-PS021.pdf) · [Architecture](docs/ARCHITECTURE.md) · [Evaluation](docs/EVALUATION.md) |

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

56 tests cover the recovery engine, the envelope and idempotency rules, adapter protocol round-trips, the append-only store, every scenario end-to-end, the random fault campaign, and the HTTP API.

### Run the experiment table (no browser needed)

```powershell
python -m threadline.bench
```

This runs every scenario through the full workflow (run → recover → deliver late → resume → recover twice) and prints the results table used in the presentation.

### Run the random fault campaign

```powershell
python -m threadline.campaign                          # 1000 trials, seed 2026
python -m threadline.campaign --trials 5000 --seed 7
```

Injects random redeliveries, adapter retries, network delays, clock skew and message loss, then compares Threadline with two naive recovery strategies on the same logs. Method, results and limits: [docs/EVALUATION.md](docs/EVALUATION.md).

### Deploy to Vercel

The hosted demo runs at **<https://threadline-brown.vercel.app>** (Vercel project `threadline`).

```powershell
vercel deploy --prod
```

`vercel.json` sends every `/api/*` request to one Python function, `api/index.py`, which wraps the same `server.make_handler()` used locally. The CDN serves the dashboard straight from `static/`. Nothing else in the repository is published.

Vercel functions have no persistent disk, so the hosted event log lives in the function's temp directory. It survives while an instance stays warm and is empty again after a cold start (for example, after a period with no traffic). Run locally for a durable log.

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

## Evaluation: random fault campaign

The fixed scenarios show what each fault does. The campaign measures how often recovery is right when faults land at random. 1000 trials, seed 2026: 1384 redeliveries, 589 adapter retries, 3653 delayed deliveries and 522 lost messages, each delivery crossing its adapter's wire protocol.

| Strategy | Exact context | Duplicate replies | Order errors | Loss surfaced |
|---|---|---|---|---|
| **Threadline** (`recovery.replay`) | **100.0%** | **0** | **0** | **100.0%** |
| Naive: arrival order, dedupe by `message_id` | 10.5% | 589 | 1629 | 0.0% |
| Naive: timestamp order, dedupe by `message_id` | 17.6% | 589 | 1733 | 0.0% |

Threadline's replay was deterministic and order-invariant in 1000/1000 trials (median 0.08 ms per log). A stress run with 50% retries and 20% loss gives the same Threadline result. Method, stress run and what the campaign does *not* cover: [docs/EVALUATION.md](docs/EVALUATION.md).

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
| `threadline/campaign.py` | Seeded random fault campaign: Threadline vs. naive recovery |
| `static/` | Operator dashboard (vanilla JS, no build step) |
| `tests/` | `unittest` suite with deterministic fixtures |

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · API reference: [docs/API.md](docs/API.md) · Evaluation: [docs/EVALUATION.md](docs/EVALUATION.md) · Design decisions: [docs/IMPLEMENTATION_NOTES.md](docs/IMPLEMENTATION_NOTES.md) · Slides: [PDF](docs/Threadline-AIORI3-PS021.pdf) · [outline](docs/PRESENTATION.md) · [Google Drive](https://drive.google.com/file/d/19Ynhj6n3OldZyBsFA8Yh84paMBQFU-A6/view?usp=sharing)

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
- **Scenario fault positions are fixed** (for reproducible demos). Random positions are measured separately by the seeded fault campaign, which does not yet randomise split-brain conflicts or timeouts.
- **Hosted demo state is temporary.** On Vercel the event log is per function instance and is lost on a cold start; it is not shared if Vercel runs several instances at once.
- Recovery duration is wall-clock for a tiny log (~0.2–1 ms) and is not a performance benchmark.

## Future work

1. Real adapter connectors (WebSocket chat, HTTP LLM agent, JSON-RPC/MCP tool server) behind the same envelope.
2. Outbox pattern + redelivery requests to heal gaps automatically; operator "acknowledge gap" with audit.
3. Snapshots/checkpoints for long conversations so replay is O(new events).
4. Extend the random fault campaign to split-brain conflicts, timeouts and forged envelopes, and to longer, branching dialogs.
5. Signed envelopes and tenant-scoped authorization; PostgreSQL backend.
6. Distributed sequencer (per-conversation leases) for multi-gateway deployments.

## Team

Team code **A3-PS021-TC516** · Problem statement **PS-021**.

This is a two-member team. The role labels describe the current division of work.

| Member | Name (edit) | Role | Owns in the code |
|---|---|---|---|
| Yugraj Mangate | Yugraj Mangate | Backend, API, SQLite persistence, and project coordination | `server.py`, `threadline/service.py`, `threadline/store.py` |
| Neha Niphade | Neha Niphade | Agent adapters, correlation, recovery, testing, dashboard, and documentation | `threadline/`, `tests/`, `static/`, `docs/` |

## Submission details

| Field | Value |
|---|---|
| Member 1 name | Yugraj Mangate |
| Member 2 name | Neha Niphade |
| GitHub repository URL | <https://github.com/aiori-hackathon/A3-PS021-TC516> |
| Live demo URL | <https://threadline-brown.vercel.app> (temporary demo data) |
| Orientation video (YouTube) URL | <https://youtu.be/4IXSzTUrEOA> |
| Presentation (PDF) | [docs/Threadline-AIORI3-PS021.pdf](docs/Threadline-AIORI3-PS021.pdf) (14 slides, AIORI proposed structure; editable [.pptx](docs/Threadline-AIORI3-PS021.pptx), outline in [docs/PRESENTATION.md](docs/PRESENTATION.md); also on [Google Drive](https://drive.google.com/file/d/19Ynhj6n3OldZyBsFA8Yh84paMBQFU-A6/view?usp=sharing)) |
| Orientation video script | [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) (under 10 minutes, both members) |
| Neha GitHub username | `Neha-Niphade` (collaborator) |

### Repository access

* Ownership has been transferred to `aiori-hackathon`, as the AIORI guideline requires, after every collaborator accepted their invitation.
* Yugraj Mangate (`yugrajmangate-dev`) and Neha Niphade (`Neha-Niphade`) keep write access as collaborators.
