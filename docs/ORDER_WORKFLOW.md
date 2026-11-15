# Order recovery workflow

## Launch and verify

```sh
python3 server.py --orders data/order-workflow --demo-faults
```

Open http://127.0.0.1:8080. Enter any mock order ID, enable **Drop the first
reply after commit**, then choose **Create replacement**. Check that the
task completes after two attempts. **Check order ledger** queries the
effect service and shows the actual replacement receipt and count.

For a task that requires human review after an uncertain result, select
**Hold uncertain outcomes for review** before submitting. The same fault
leaves the task in review after one attempt. Obtain the receipt using
**Check order ledger**, paste it into the review form and verify. An
incorrect receipt is rejected. Reconciliation performs a read of the order
service and does not send another replacement request.

Restart with the same directory to recover queued, retrying or unresolved
work. To run without fault injection, omit `--demo-faults`. If port 8080
is occupied use `--port 8090` and open the matching address.

## Request flow

1. The browser submits a client request ID, order ID and recovery policy.
   The planner writes the task, versioned handoff, stable operation key
   and audit entry in one transaction. A repeated request ID for the same
   intent returns the existing task. Changed intent returns HTTP 409.
   The browser keeps an unacknowledged request ID through a same-tab reload.
2. A background worker claims a due task in a SQLite transaction, decodes
   the planner adapter envelope and calls the order service using the saved
   operation key. The order service listens on a separate loopback port,
   requires a per-launch bearer token and owns its own SQLite file.
3. The order service serializes writes and saves one receipt per key. It
   returns the same receipt on repeat requests for the same order, and
   rejects reuse of a key for a different order. The worker converts the
   receipt through the tool adapter and writes completion and its audit
   entry in one planner transaction.
4. An ambiguous transport loss schedules protected retries after one and
   then two seconds, with a maximum of three attempts. A process that
   disappears during dispatch leaves an expiring claim. A replacement
   worker can reclaim a protected task with the same key after eight
   seconds. A stale claimant cannot overwrite the current attempt.
5. Review policy or an exhausted retry budget leaves the task in review.
   Reconciliation completes it only when the order ledger confirms exactly
   one matching receipt for that operation key.

| Task state | Next possible states |
| --- | --- |
| Queued | Sending |
| Sending | Complete, retry wait, needs review, or a new sending attempt after claim expiry |
| Retry wait | Sending |
| Needs review | Complete with a verified receipt |
| Complete | No further effect for this task |

The task and audit are in `planner.sqlite`. The order ledger is in
`orders.sqlite`. The original replay dashboard uses its own event database.
Keep both workflow files together. The server refuses to start when only
one is present, and locks the data directory so two local servers cannot
dispatch from it simultaneously. For a local backup, stop the workflow
cleanly and copy the whole directory. Keep the provider ledger and keys
for at least the full retry and reconciliation window in a real deployment.

## Verification and scope

```sh
python3 -m unittest discover -s tests -t . -q
python3 -m threadline.restart_lab
python3 -m threadline.receipt_lab --order 4821 --show-unknown
```

This build runs as a single-host operator workflow with a mock order service.
The PS-021 lab uses two mock adapter formats and a versioned experimental
schema. The browser workflow exercises a real HTTP connection and two
separate stores. Production exposure would require an authenticated
operator API with TLS, backup and restore drills, monitoring, load and
failure testing, and a real provider contract. The guarantee in this
environment is one effect per retained operation key at the receiver.

The recovery design follows established practices for
[idempotent requests](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/)
and the [transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html).
