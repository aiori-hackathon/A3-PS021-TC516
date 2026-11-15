# Recovery after an HTTP reply is lost

```sh
python3 -m threadline.receipt_lab --order 4821
python3 -m threadline.receipt_lab --order 4821 --show-unknown --json
python3 -m threadline.receipt_lab --order 4821 --evidence-dir jury-run
python3 -m unittest tests.test_receipt_lab -v
```

The planner and tool reuse Threadline's two mock adapter payloads. The
experimental wrapper `threadline-receipt-lab-v1` carries the dialog ID, task
ID, operation key and adapter payload. The planner writes the original handoff
to `planner.sqlite` before dispatch. The tool calls a separate loopback HTTP
order service with `Idempotency-Key`. That service owns `orders.sqlite` and
records a replacement receipt in its own transaction.

1. The order service commits the replacement and closes the HTTP connection
   before writing its reply. The tool sees an actual disconnect and exits.
2. The test reads the first receipt from the order service for evidence. It
   restarts the order service from disk and starts a fresh tool process.
3. The retry receives only the persisted handoff, including the original key.
   The protected service returns its saved receipt. The planner acknowledges
   that receipt and the test reads the order ledger again.

| Same injected fault in both runs | Receipt on retry | Replacement rows |
| --- | --- | ---: |
| Deduplication disabled | New receipt | 2 |
| Deduplication enabled | Original receipt | 1 |

With `--show-unknown`, a third run models a provider without a safe retry
contract. The planner records that recovery requires review before dispatch.
After the reply is lost and the service restarts, the planner leaves the
task in `needs_review` and sends no retry. The experiment can still read
the mock ledger to verify the count. That lookup is for the observer.

`--evidence-dir` keeps each run's two SQLite databases and the JSON results.
Point it at a new directory each time. To check the protected effect count
directly in the saved database, run:

```sh
sqlite3 jury-run/protected/orders.sqlite 'SELECT order_id, receipt_id FROM replacements;'
```

The service returns HTTP 409 if a caller reuses an operation key with a
different order ID. It checks the order ID and records an effect inside one
transaction. A concurrent HTTP test submits four copies of the same request
and checks the resulting ledger. Two distinct task IDs in one dialog get
different operation keys.

The experiment uses a mock effect on one machine, a local TCP connection and
explicitly injected faults. The result depends on the order service retaining
its ledger and honoring the operation key. If a provider supports neither
deduplication nor reliable status lookup, treat a lost reply as an unknown
outcome and reconcile before trying a new operation. The dashboard's replay
and the five process-restart cases are separate experiments.

References: [AWS on request identity and changed intent](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/),
[Stripe's request replay contract](https://docs.stripe.com/api/idempotent_requests),
[Temporal's crash-before-acknowledgment case](https://docs.temporal.io/activity-definition),
and [the IETF agentproto charter](https://datatracker.ietf.org/wg/agentproto/about/).
