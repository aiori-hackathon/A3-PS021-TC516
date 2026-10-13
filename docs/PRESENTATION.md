# Presentation outline: Threadline (PS-021 · A3-PS021-TC516)

10 slides, ~8 minutes + live demo. Speaker notes in *italics*.

**1. Title**
Threadline: dialog correlation and recovery across agent adapters. Team A3-PS021-TC516, AIORI-3, PS-021.
*One sentence: "We keep one AI conversation consistent while it moves between agents, fails, and comes back."*

**2. The problem**
- One conversation, many adapters: web chat → planner agent → tool agent, each with its own protocol.
- Failures happen mid hand-off: timeout, retry, duplicate, delay, reorder, loss, split-brain.
- Consequences: lost context, repeated replies, two agents answering, no audit trail.
*Show a simple diagram of the hand-off chain with a red X at the tool agent.*

**3. Key insight**
- The adapters must not own the conversation. **The event log does.**
- Recovery = deterministic replay of an append-only log.

**4. Correlation model**
Five fields on every message: `conversation_id`, `message_id`, `sequence`, `causation_id`, `idempotency_key = hash(conversation, sequence, role, cause, kind)`.
- Never by timestamp or adapter name.
- Identical across three wire protocols (WebSocket JSON, HTTP headers, JSON-RPC 2.0).

**5. Architecture**
Adapters → Gateway (validate, derive key, classify) → SQLite append-only log (triggers) → pure recovery engine → JSON API → operator dashboard. Standard-library Python, zero dependencies.

**6. Recovery pipeline**
Validate → Deduplicate (first arrival wins) → Resolve conflicts (hand-off ownership › causal link › first arrival) → Order by sequence → Verify gaps & causation → Resume point.
Every log entry gets a verdict + rule + reason.

**7. Live demo** (switch to browser, follow docs/DEMO_SCRIPT.md Part 3)
Chaos run → replay (gap, blocked) → deliver late → replay → resume → replay (complete) → replay again (same hash).

**8. Results** (`python -m threadline.bench`)

| Scenario | First recovery | Final |
|---|---|---|
| Happy path | consistent | complete |
| Interrupted hand-off | recovered, resumable | complete after resume |
| Duplicate storm | 2 duplicates removed | complete, 1 reply per step |
| Out of order | 2 reordered | complete |
| Delayed | incomplete (gap 6) | complete after late delivery |
| Missing | incomplete (gap 5) | **stays incomplete (ended_with_gaps): surfaced, not invented** |
| Split-brain | conflict → HANDOFF_OWNERSHIP | complete, flagged |
| Chaos | incomplete | complete after deliver + resume |

All recoveries idempotent; replay ≈ 0.2–1 ms for these logs; 50 automated tests.

**9. Limitations & future work**
Simulated adapters; scripted resume; single-node SQLite; permanent gaps need an outbox/redelivery path.
Next: real connectors (WebSocket / LLM HTTP / JSON-RPC-MCP), outbox + redelivery, snapshots, seeded random fault campaigns, signed envelopes, distributed sequencer.

**10. Team & thanks**
- Member 1 (Yugraj Mangate): Backend, API, SQLite persistence, and project coordination
- Member 2 (Neha Niphade): Agent adapters, correlation, recovery, testing, dashboard, and documentation

GitHub: https://github.com/yugrajmangate-dev/A3-PS021-TC516 · Orientation video: *[YouTube video URL]* · Q&A.
(Names and links are placeholders; copy the final values from README.md → *Submission details*.)

### Suggested speaker split
| Slides | Speaker |
|---|---|
| 1–3 (title, problem, insight) | Member 1 |
| 4–6 (correlation model, architecture, recovery pipeline) | Member 2 |
| 7 (live demo) | Neha drives, Yugraj narrates the recovery decisions |
| 8–10 (results, limitations, team) | Neha, then Yugraj closes |

### Likely evaluator questions

- *Why not order by timestamp?* Clocks differ across services and retries re-stamp messages; sequence + causation is the only order all adapters agree on.
- *What if both conflicting events look valid?* Deterministic fallback (lowest log offset), and the conversation is flagged `recovered_with_conflicts` for review.
- *Is recovery safe to run repeatedly?* Yes: pure function, no writes to the log, same hash on the same log (shown live).
- *How would this scale?* Snapshots + replay from the last checkpoint; partition the log by `conversation_id`.
