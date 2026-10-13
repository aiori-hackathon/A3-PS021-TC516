# CLAUDE.md: Threadline

Threadline is a dialog correlation and recovery lab for the **AIORI-3 hackathon**, problem statement **PS-021: Experimental Dialog Correlation and Recovery Across Agent Adapters**. One support conversation crosses three simulated agent adapters that use different wire protocols. Faults are injected deterministically. Every delivery goes into an append-only SQLite log, and a pure replay function rebuilds the context and explains each decision.

`README.md` is the user-facing reference (scenario tables, status vocabulary, guarantees). This file covers what you need to change the code safely.

## Team and submission

| Item | Value |
|---|---|
| Team | Team Nexora · team code **A3-PS021-TC516** |
| Repository | <https://github.com/yugrajmangate-dev/A3-PS021-TC516> (owner `yugrajmangate-dev`) |
| Live demo | <https://threadline-brown.vercel.app> (Vercel project `threadline`, connected to the GitHub repo) |
| Hackathon guidelines | <https://portal.aiori.in/aiori-3/hackathon-guideline/> |
| Team working doc | <https://docs.google.com/document/d/1sVP_knz2Lf_Z85Vum_5NgstbedUjxo1ng-CJvaTce5Y/edit?tab=t.0> |

| Member | GitHub | Role | Owns |
|---|---|---|---|
| Yugraj Mangate (repo owner) | `yugrajmangate-dev` | Backend, REST API, SQLite persistence, gateway, coordination | `server.py`, `threadline/service.py`, `threadline/store.py` |
| Neha Niphade (collaborator) | `Neha-Niphade` | Adapters, correlation model, recovery, tests, dashboard, docs | `threadline/adapters.py`, `events.py`, `recovery.py`, `simulator.py`, `bench.py`, `static/`, `tests/`, `docs/` |

- `aiori-hackathon` has write access as a collaborator. Repository ownership moves to `aiori-hackathon` only after that account is confirmed and every collaborator has accepted their invitation. Do not start a transfer.
- Submission needs three things: a YouTube orientation video under 10 minutes in which each member covers their role, PS-021 and the solution (script: `docs/DEMO_SCRIPT.md`); the presentation (outline: `docs/PRESENTATION.md`); and the repository URL on the AIORI portal.
- `*[YouTube video URL]*` in `README.md` and `docs/PRESENTATION.md` is the only remaining placeholder. Never invent names, links or results. Leave placeholders until the team provides the real value.
- Why PS-021: it covers what happens to an AI conversation at system boundaries (delays, loss, duplicates, reordering, timeouts, split-brain). The method is experimental: inject faults on purpose and record where and why recovery succeeds or fails.

## Commands

The environment is Windows with Python 3.11 (3.10+ required). If `python` is not found, use `py`.

```powershell
python server.py                                   # http://localhost:8080, log at data/threadline.db
python server.py --fresh                           # wipe demo data first (use before recording)
python server.py --port 9000 --db :memory:         # other port / throwaway log
python -m unittest discover -s tests -t . -v       # full suite (49 tests, < 1 s)
python -m unittest tests.test_recovery -v          # one module
python -m threadline.bench                         # all 8 scenarios end-to-end; add --json for machine output
vercel deploy --prod                               # redeploy the hosted demo (this folder is linked via .vercel/)
```

Environment variables: `HOST`, `PORT`, `THREADLINE_DB`, and `THREADLINE_ACCESS_LOG=1` (prints request lines, which are silent by default). The server binds `127.0.0.1` and has no authentication, so treat `--host 0.0.0.0` as an explicit opt-in. No CI is configured; run the tests locally.

## Architecture

```
web-chat (websocket-json)   planner-agent (http-agent)   tool-agent (json-rpc-2.0)
          \                          |                          /
           native payload --decode--> canonical envelope (adapters.py -> events.py)
                                     |
             Gateway: service.py  normalise · validate · classify live · append
                                     |
             Append-only log: store.py (SQLite; triggers block UPDATE/DELETE on `events`)
                                     |
             recovery.replay(): VALIDATE -> DEDUPLICATE -> RESOLVE -> ORDER -> VERIFY -> RESUME
                                     |
             server.py JSON API  ->  static/ dashboard (vanilla JS, no build step)
```

| File | Responsibility |
|---|---|
| `server.py` | Standard-library `ThreadingHTTPServer`. Route table in `make_handler()`, JSON errors, static files |
| `threadline/events.py` | Envelope fields, `validate()`, `normalise()`, `make_idempotency_key()` |
| `threadline/adapters.py` | `Adapter` subclasses with `encode`/`decode`. Registry: the `ADAPTERS` dict |
| `threadline/store.py` | `EventStore`: tables `events`, `conversations`, `in_flight`, `recoveries` |
| `threadline/recovery.py` | `replay(events, conversation_id)`: pure, returns status, context, decisions, `context_hash` |
| `threadline/simulator.py` | `SCRIPT` (9-step dialog), `FAULTS`, `SCENARIOS`, `plan()`, `continuation()` |
| `threadline/service.py` | `Threadline` gateway: `ingest`, `run_experiment`, `recover`, `deliver_pending`, `resume`, `ingest_external`. `ApiError` |
| `threadline/bench.py` | Runs every scenario through run → recover → deliver → resume → recover ×2 |
| `api/index.py`, `vercel.json`, `.vercelignore` | Vercel deployment. `/api/*` goes to one Python function wrapping `server.make_handler()`; the CDN serves `static/`. The log is in the function's temp dir (per instance, lost on cold start) |
| `tests/` | `test_recovery.py` (engine on hand-built logs), `test_components.py`, `test_scenarios.py`, `test_api.py`. `helpers.py` provides `ev()`, `linear()`, `renumber()` fixtures |
| `docs/` | `ARCHITECTURE.md`, `API.md`, `IMPLEMENTATION_NOTES.md` (decision log), `DEMO_SCRIPT.md`, `PRESENTATION.md` |

## Invariants: do not break these

1. **Standard library only.** No `pip install`, no third-party imports, no `requirements.txt`.
2. **The `events` table is append-only.** SQLite triggers abort any `UPDATE` or `DELETE` on it. Every delivery is appended, including duplicates, conflicts and late arrivals: never filter on write. The log is the evidence, and recovery decides validity. The other tables are mutable: `set_status()` updates `conversations` and `release()` deletes from `in_flight`. Wiping data is only possible through `EventStore.reset()`, which drops and recreates every table.
3. **`replay()` is pure and deterministic.** No I/O, clock, randomness, database or network access in `recovery.py`. The same log must give the same output and the same `context_hash`, whatever order the events arrived in. Timing and audit records belong in `service.recover()`.
4. **Never identify or order by timestamp or adapter name.** Identity is `message_id`. Position is `(conversation_id, sequence)`, with `log_offset` breaking ties. `sent_at` and `received_at` are stored for display only.
5. **Never invent context.** A missing sequence number makes the status `incomplete`, lists the gap, flags broken causation, and blocks resume (HTTP 409 `not_resumable`). Never skip or fill a gap.
6. **No duplicate assistant responses.** First arrival wins. `resume()` stops chaining as soon as an append is not `processed`.
7. **The simulator is deterministic by design.** Fault positions are fixed and message ids are derived. Tests and docs assert exact counts, so changing `SCRIPT` or fault positions also means updating `test_scenarios.py`, the README tables and `docs/`.
8. **Hold `store.lock` around multi-step operations.** It is an `RLock` and the server is multi-threaded. Existing service methods wrap each read-replay-append sequence in `with self.store.lock:`. Do the same for new ones.
9. **Keep the Vercel entry detectable.** `api/index.py` must keep its `class handler(...)` statement. Vercel's build skips a file whose `handler` is only assigned (`handler = ...`): the static site still deploys, but every `/api/*` route returns 404. `make_handler(app)` is shared with `server.py`, so a signature change there needs the same change in `api/index.py`.

## Correlation model

The envelope requires `conversation_id`, `message_id`, `adapter`, `role` (`user|assistant|tool|system`), `kind` (`message|handoff|signal`) and `content`. Dialog events also need `sequence ≥ 1`. A `handoff` needs a `target_adapter`. A `signal` (such as an adapter timeout) is out-of-band and must **not** carry a sequence. `causation_id` is the `message_id` this event responds to.

`idempotency_key = "idk_" + sha256(f"{conversation_id}|{sequence or '-'}|{role}|{causation_id or '-'}|{kind}")[:24]`

The key names the dialog *slot*, so a retry with a new `message_id` or different wording still collapses into the first one. Adapter and time are deliberately excluded. `validate()` rejects any key that doesn't match this derivation, which catches forged keys. All three wire protocols carry the five correlation fields.

**Live vs. replay:** `service.ingest()` classifies each new delivery as `accepted`, `duplicate` or `conflict` so that a gateway would not *process* it twice. The event is still appended either way. `replay()` later re-derives the same verdicts from the log.

**Conflict precedence** for one sequence slot: `HANDOFF_OWNERSHIP` (the previous hand-off leased the slot to this adapter), then `CAUSAL_LINK`, then `FIRST_ARRIVAL`. Losing claims stay in the log with verdict `CONFLICT_LOST`.

**Vocabulary.** The dashboard and API use these strings exactly; `static/app.js` maps them to colours.
- Recovery status: `consistent`, `recovered`, `recovered_with_conflicts`, `incomplete`, `empty`
- Dialog state: `complete`, `awaiting_handoff`, `interrupted_handoff` (a timeout signal points at the open hand-off), `awaiting_reply`, `ended_with_gaps`, `empty`
- Event verdict: `accepted` (`IN_ORDER`/`REORDERED`), `duplicate` (`DUPLICATE_DELIVERY`/`DUPLICATE_RETRY`), `rejected` (`INVALID_ENVELOPE`, `FOREIGN_CONVERSATION`, `MESSAGE_ID_REUSED`, `CONFLICT_LOST`), `signal`. Rule texts are in `recovery.RULES`.
- Conversation lifecycle (history list): `active`, `awaiting_recovery`, `interrupted`, `resumed`, or the last recovery status.

## Scenario baseline

This is the output of `python -m threadline.bench`. After any change, the counts, statuses and `idem = yes` must still match unless you changed the behaviour on purpose. Timings vary from run to run and don't matter.

| Scenario | Faults | Events | Accepted | Dupes | Gaps | Conflicts | Reordered | First status | Late | Resumed | Final status | Final dialog |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `happy_path` | none | 9 | 9 | 0 | – | 0 | 0 | consistent | 0 | 0 | consistent | complete |
| `interrupted_handoff` | timeout at seq 7 | 8 | 7 | 0 | – | 0 | 0 | recovered | 0 | 2 | recovered | complete |
| `duplicates` | redeliver seq 1, retry seq 2 | 11 | 9 | 2 | – | 0 | 0 | recovered | 0 | 0 | recovered | complete |
| `out_of_order` | 3 before 2, 9 before 8 | 9 | 9 | 0 | – | 0 | 2 | recovered | 0 | 0 | recovered | complete |
| `delayed` | seq 6 held in transit | 8 | 8 | 0 | 6 | 0 | 0 | incomplete | 1 | 0 | recovered | complete |
| `missing` | seq 5 lost | 8 | 8 | 0 | 5 | 0 | 0 | incomplete | 0 | 0 | incomplete | ended_with_gaps |
| `conflict` | planner and tool both claim seq 4 | 10 | 9 | 0 | – | 1 | 0 | recovered_with_conflicts | 0 | 0 | recovered_with_conflicts | complete |
| `chaos` | timeout, duplicate, out_of_order, delayed, conflict | 10 | 6 | 2 | 6 | 1 | 1 | incomplete | 1 | 2 | recovered_with_conflicts | complete |

## Making changes

- **After every change,** run the full test suite, which must stay all green (49 tests today). For behaviour changes, also run `python -m threadline.bench`. Add a test for any new behaviour: engine logic goes in `test_recovery.py` with `helpers.ev()` fixtures, which use no clocks or randomness.
- **New fault or scenario:** add it to `simulator.FAULTS` (dict order is the canonical fault order) and `SCENARIOS`, apply it in `plan()`, then update `test_every_scenario_matches_documented_outcome`, the README scenario table, the baseline above and the docs.
- **New adapter:** subclass `Adapter` in `adapters.py`, carry all five correlation fields in `encode`/`decode`, register it in `ADAPTERS`, and keep the unused `events.ADAPTERS` tuple in sync. Round-trip tests live in `test_components.py`.
- **New API route:** add a `(verb, pattern, fn)` entry to `routes` in `server.py`. Raise `ApiError(status, code, message, details)` for errors (never bare exceptions) so clients always get a JSON `{"error": {...}}` body. Document the route in `docs/API.md` and test it in `test_api.py`.
- **New status, dialog state or rule:** update `recovery.py`, the colour map in `static/app.js`, the README vocabulary table and `docs/ARCHITECTURE.md`.
- **Docs mirror the code.** README, `docs/API.md`, `docs/ARCHITECTURE.md` and `docs/DEMO_SCRIPT.md` quote exact statuses, counts and test names. Update them in the same change. Record design decisions in `docs/IMPLEMENTATION_NOTES.md`.
- **Ownership:** split files are owned per the team table above. When a change crosses into the other member's files, say so in the commit or PR description.

## Known limitations

These are by design for the hackathon; don't "fix" them unprompted.

The adapters are simulated in-process, so no real LLM, WebSocket or JSON-RPC endpoint is called. The hosted Vercel demo keeps its log only while a function instance is warm. `resume` replays the scripted continuation and refuses `external` conversations with 409 `no_simulated_adapter`. Sequence leasing at hand-off is an assumption, not an implementation. There is one SQLite file and one process lock. Permanent gaps stay open: there is no outbox redelivery or "acknowledge gap" action. Fault positions are fixed rather than random. Future work (outbox redelivery, snapshots, randomised campaigns, signed envelopes, PostgreSQL, distributed sequencer) is listed in the README.
