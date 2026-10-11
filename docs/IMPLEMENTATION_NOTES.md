# Implementation notes

This file records what changed relative to the first prototype and why, so team members can explain every decision.

## Baseline observed before changes

The original `server.py` (single file, in-memory dict) ran correctly, but:

1. An interrupted run recovered as `recovered` with no gaps: the open hand-off was silently ignored.
2. The simulator never produced duplicates, reordering or gaps, so dedup/gap code was never exercised.
3. The idempotency key was `conversation:sequence:adapter`: it depended on the adapter name, so the same step retried by another adapter got a different key.
4. `causation_id` pointed to the previous *event id* (a delivery), not the message it responds to.
5. Tie-breaking used timestamps; malformed JSON closed the connection without an error response; data was lost on restart.

## Structural changes (and why)

| Change | Reason |
|---|---|
| Split `server.py` into the `threadline/` package (events, adapters, store, recovery, simulator, service) | Each concept can be explained, owned and tested by a different team member. `server.py` is now only HTTP. |
| SQLite event log with append-only triggers | Persistence was requested if needed; recovery from an event log that vanishes on restart would not be credible. SQLite is in the standard library. |
| Pure `replay()` function | Makes idempotency and determinism provable in tests and easy to explain. |
| Separate delivery identity (`event_id`, `log_offset`) from message identity (`message_id`) | Required to represent "the same message delivered twice" honestly. |
| Idempotency key = hash(conversation, sequence, role, causation, kind) | Names the dialog slot, so retries with new ids/wording collapse; excludes adapter and time per the domain rules. Verified on replay to detect forged keys. |
| Adapters with three different wire protocols | PS-021 is about crossing adapters, services *and protocols*; showing the same correlation surviving three formats makes that concrete. |
| Failure signals as out-of-band events (no sequence) | A timeout is evidence, not dialog. Logging it keeps "why is this hand-off open?" answerable from the log. |
| Gaps block resume | Requirement: missing events must be surfaced, not silently ignored. Resuming past a gap would silently drop context. |
| Fixed fault positions instead of randomness | Deterministic fixtures were requested; evaluators see exactly what the docs describe. Randomised campaigns are listed as future work. |
| Server binds `127.0.0.1` by default | No authentication; `--host 0.0.0.0` is an explicit opt-in. |

## Assumptions (not invented requirements, stated explicitly)

* The gateway leases sequence numbers at hand-off time, so a retry reuses the slot's sequence number.
* Conflict precedence (hand-off ownership › causal link › first arrival) is our design choice; it is deterministic and documented, and conflicts remain flagged.
* First arrival wins among duplicates (the earliest log offset is the copy that may already have been processed).
* "Resume" in the demo replays the scripted continuation; a production system would call the adapter with the recovered context.

## Compatibility

* `POST /api/experiments` still accepts `{"inject_interruption": true}`.
* `POST /api/conversations/{id}/recover` and `GET /api/conversations/{id}` still exist; their response shapes are richer (documented in API.md).
* `python server.py` still starts on port 8080.

## Second pass: hardening and verification

| Change | Reason |
|---|---|
| Interrupting an animated replay (loading another experiment or resetting) now finishes the replay cleanly | Previously the pending animation promise was never resolved |
| History list layout: status pill no longer wraps or overflows the sidebar | Seen in headless-browser screenshots |
| `dialog_state` `ended_with_gaps` replaces `complete` when earlier messages are missing | The tile read "incomplete" / "complete" at the same time, which was confusing |
| `PATCH`/`OPTIONS` return a JSON 405, `HEAD` an empty 405; 405 messages name the allowed verb | Previously fell through to Python's default HTML 501 page |
| `python server.py --fresh` | Reproducible clean demo database for recordings |
| Inspector rows are keyboard-selectable (Tab + Enter) | Accessibility |
| New tests: end-to-end correlation fields across adapters, HEAD/PUT/PATCH/DELETE handling | Covers the verification checklist explicitly |

## Third pass: recording and submission preparation

| Change | Reason |
|---|---|
| Startup messages in `server.py` are flushed immediately | Messages were invisible when output was redirected (Python buffers non-terminal stdout) |
| Team role labels unified: *Backend, API, and SQLite persistence* · *Agent adapters, correlation, and recovery* · *Testing, experiments, dashboard, and documentation* | Consistent wording in README, DEMO_SCRIPT and PRESENTATION |
| README *Submission details* section with explicit placeholders for member names, GitHub URL and YouTube URL | Real names and links are not known yet and must not be invented |
| Orientation script re-timed (≈ 8:15 planned, hard limit under 10 minutes) with a timing table; consistent *[Member N name]* placeholders; corrected "five kinds of failure" wording | Recording must stay under 10 minutes and every member must cover role, PS-021, solution and demo |
| Recording checklist made complete and accurate | Covers names/roles, green tests, fresh data, visibility/audio, length, later YouTube link, README completion, stopping the server |

Verified in this pass: 49 tests pass; `python -m threadline.bench` reports all 8 scenarios idempotent; `python server.py --fresh` starts with 0 conversations after the database was seeded.

## Final readiness check

* The final team is two members: Yugraj Mangate and Neha Niphade. Their names and roles are filled in across README.md, docs/DEMO_SCRIPT.md and docs/PRESENTATION.md. The YouTube URL remains the only submission placeholder until upload.
* Overlapping recording-checklist items (both covered the YouTube link) separated into "names + GitHub URL" and "YouTube URL after upload".

## Publication

* Git repository initialised on branch `main`; `.gitignore` excludes Python caches, the generated demo database (`data/threadline.db`), temporary headless-browser files, editor/OS files and `.env`.
* Public GitHub repository: https://github.com/yugrajmangate-dev/A3-PS021-TC516. The `[GitHub repository URL]` placeholder was replaced with this URL in README.md, docs/DEMO_SCRIPT.md and docs/PRESENTATION.md. `[YouTube video URL]` and the member-name placeholders remain until the team supplies them.
