# Evaluation: random fault campaign

PS-021 asks for an *experimental* study of dialog correlation and recovery. The eight fixed scenarios (`python -m threadline.bench`) show what each fault does at a known position. This campaign measures how often recovery is right when faults land at **random** positions, and compares Threadline with two naive approaches on exactly the same logs.

```powershell
python -m threadline.campaign                          # 1000 trials, seed 2026
python -m threadline.campaign --trials 5000 --seed 7   # any size, any seed
python -m threadline.campaign --json                   # machine-readable
```

It is also in the dashboard (panel **4 · Random fault campaign**) and the API (`POST /api/campaign`), so it can be run on the [live demo](https://threadline-brown.vercel.app) without installing anything.

## Method

Each trial takes the scripted 9-step support dialog (web-chat → planner-agent → tool-agent and back) and:

1. **Crosses the wire.** Every delivery is encoded into its adapter's native protocol (WebSocket JSON, HTTP with headers, JSON-RPC 2.0) and decoded back, so the correlation fields must survive all three formats.
2. **Injects random faults** with a seeded `random.Random`:

   | Fault | Default rate |
   |---|---|
   | Redelivery (same `message_id`) | 15% per message |
   | Adapter retry of an assistant step (new `message_id`, new wording) | 12% per assistant step |
   | Network delay (exponential, mean 900 ms; messages are produced 400 ms apart) | 35% per delivery |
   | Clock skew on `sent_at` | uniform ±600 ms per adapter |
   | Message loss | 6% per message, at most 2 per trial |

3. **Recovers the arrival-ordered log three ways:**
   - **Threadline:** `recovery.replay()`, the same pure function the gateway uses.
   - **Naive (arrival order):** keep messages in arrival order, drop repeated `message_id`s. This is a typical at-least-once consumer.
   - **Naive (timestamp order):** sort by `sent_at`, drop repeated `message_id`s.

4. **Scores each result against ground truth:**

   | Metric | Meaning |
   |---|---|
   | Exact context | Every delivered dialog slot appears once, in sequence order, with a legitimate copy for that slot, and nothing else |
   | Duplicate replies | Extra copies of a slot left in the context (for example, the user would see two assistant answers) |
   | Order errors | Places where the context goes backwards in sequence |
   | Loss surfaced | Of the trials that lost a message, how many the strategy reported as not clean and complete |
   | False alarms | Trials without loss that were still reported as incomplete |

Threadline is also checked for **determinism** (replaying the same log twice gives the same `context_hash`) and **order invariance** (replaying a randomly re-shuffled copy of the log gives the same slots and gaps).

## Results (1000 trials, seed 2026)

Injected: 1384 redeliveries, 589 adapter retries, 3653 delayed deliveries, 522 lost messages (425 trials with loss).

| Strategy | Exact context | Duplicate replies | Order errors | Loss surfaced | False alarms |
|---|---|---|---|---|---|
| **Threadline** | **100.0%** | **0** | **0** | **100.0%** | **0** |
| Naive: arrival order | 10.5% | 589 | 1629 | 0.0% | 0 |
| Naive: timestamp order | 17.6% | 589 | 1733 | 0.0% | 0 |

Threadline replay was deterministic in 1000/1000 trials and order-invariant in 1000/1000. The median replay time was about 0.08 ms per 9–14 event log.

**Before vs. after, in one line:** deduplicating by `message_id` stops redeliveries but not retries, so every one of the 589 retries became a duplicate reply. Ordering by arrival or by clock put messages out of sequence 1629 and 1733 times. A consumer with no notion of a gap hid every lost message. Threadline's idempotency key, sequence ordering and gap detection remove all three failure modes.

### Stress run

With much harsher settings (50% redelivery, 50% retry, 90% delay with a 3000 ms mean, ±2000 ms skew, 20% loss; 1000 trials, seed 99), the campaign injected 3740 redeliveries, 2116 retries, 12090 delays and 1437 lost messages. Threadline was still exact in 1000/1000 trials and surfaced loss in all 873 lossy trials. Both naive strategies were exact in 0/1000 trials and leaked 2116 duplicate replies.

```python
from threadline import campaign
campaign.run(1000, 99, p_redeliver=0.5, p_retry=0.5, p_delay=0.9,
             mean_delay_ms=3000, max_skew_ms=2000, p_loss=0.2)
```

## What the campaign does not cover

These limits are stated so the 100% figures are read correctly:

- **Faults are within Threadline's model.** Each message carries correct correlation fields. Forged or corrupted envelopes, `message_id` reuse and foreign conversations are covered by unit tests (`test_recovery.py`), not by this campaign.
- **Split-brain conflicts and adapter timeouts are not randomised.** They depend on hand-off ownership and are covered by the fixed `conflict`, `interrupted_handoff` and `chaos` scenarios.
- **Loss is surfaced, not repaired.** A lost message is reported (status `incomplete` or an unfinished dialog state) and resume is blocked. Healing it needs an outbox and redelivery, which is future work.
- **Trailing loss is detected because this dialog ends with an assistant reply.** If the last messages are lost, Threadline reports the dialog as unfinished rather than as a gap. A conversation that legitimately ends early would look the same.
- **The baselines are deliberately simple.** They represent common practice (at-least-once delivery with `message_id` deduplication), not other published systems.
- **Simulated adapters, one process.** No real network, LLM or tool server is involved.

Tests: `tests/test_campaign.py` asserts these results on a 200-trial run (seed 7), that the same seed reproduces identical numbers, and that the scorer rejects dropped, swapped, duplicated and forged slots.
