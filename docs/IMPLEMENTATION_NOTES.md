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

## Hosted demo on Vercel

| Change | Reason |
|---|---|
| `api/index.py` wraps the existing `server.make_handler()` as Vercel's `handler` class | One code path for local and hosted runs; no framework and no third-party packages needed |
| `vercel.json`: `/api/*` goes to the function, and `static/` is the output directory | The CDN serves the dashboard. Only `static/` is published as files, so the source tree is not exposed |
| `.vercelignore` excludes the local demo database, `tests/` and `docs/` | Not needed at runtime; keeps local demo data out of the upload |
| The event log is in the function's temp directory | Vercel functions have no persistent disk. The log lasts while an instance is warm and is empty after a cold start. This is documented as a limitation rather than replaced by an external database, which would break the standard-library-only rule |
| `handler` is declared with a `class` statement, not assigned | Vercel's build only recognises a Python entrypoint by `class handler` or `app = ...`. With `handler = make_handler(...)`, the first deployment shipped only the static site and every `/api/*` route returned 404 |

Verified on the live URL: every scenario's full operator workflow (run → recover → deliver → resume → recover twice) matches `python -m threadline.bench`, re-runs report `identical`, and source paths such as `/server.py` return 404.

## Fix: lost error responses for requests with an unread body

`test_errors_are_json` failed about once in ten runs with `ConnectionAbortedError` on `PUT /api/experiments`. The server answered 405 without reading the request body. Closing a socket that still has unread input sends a TCP reset, and the client can lose the response before reading it. This affected any request whose body the server never read: wrong-verb requests, a `GET` with a body, and errors raised before routing. With a 900 KB body, about 1 response in 6 was lost.

`server.py` now drains an unread body (up to `MAX_BODY`) after every response. `test_unread_body_does_not_reset_connection` sends 20 such requests. On the old code it fails about 97% of the time; on the fixed code it passes every time. The suite has 50 tests.
