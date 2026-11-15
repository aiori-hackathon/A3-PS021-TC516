# PS-021 experimental dialog-state schema

The live order workflow uses `threadline-order-workflow-v1`. It is an
experimental message contract for the two mock adapters.

| Field | Location | Meaning |
| --- | --- | --- |
| `schema` | Task wrapper | Must equal `threadline-order-workflow-v1`. |
| `task_id` | Task wrapper | Nonempty string identifying the same task across attempts. |
| `operation_key` | Task wrapper | Stable `idk_` key with 24 lowercase hex characters identifying the order effect. |
| `wire.protocol` | Adapter frame | `http-agent` for planner handoffs, `json-rpc-2.0` for tool results. |
| Dialog ID | Adapter payload | Planner `X-Conversation-Id` or tool `params.conversation`. Both decode to `conversation_id`. |
| Message ID and cause | Adapter payload | Each message has its own ID. The tool result names the planner handoff it answers. |
| Order ID or receipt | Adapter payload | Planner `body.output` holds the mock order ID. Tool `params.result` holds the receipt. |

The task ID and operation key travel in the same wrapper in both directions. The
receiver HTTP request carries the operation key and order ID. The order ledger is
separate from the planner. The receipt response is converted through the tool
adapter before the planner saves completion.

| Saved state | Allowed next states |
| --- | --- |
| `queued` | `sending` |
| `sending` | `sending` (new attempt after claim expiry), `retry_wait`, `needs_review`, `complete` |
| `retry_wait` | `sending` |
| `needs_review` | `complete` after the separate order ledger verifies the receipt |
| `complete` | Terminal. A repeated reconciliation with the same receipt returns the completed task. |

The database trigger rejects illegal state changes. The worker checks the
schema and IDs before sending an order. An unknown saved version moves the
task to review without contacting the order service. A late worker checks
both saved state and attempt number within the same SQLite write transaction
used to update the task and append its audit entry.

The standalone five-case process restart lab uses
`threadline-experimental-v1` with `pending → committed → complete`.
It is a separate, smaller experiment with its own database and is described in
[the restart-lab schema](RESTART_LAB.md).

```sh
python3 -m unittest tests.test_order_workflow -v
python3 -m tests.workflow_evidence
```

The focused suite checks both adapter formats, unknown versions, illegal
transitions, late completion and timeout writes, and a ledger-matched receipt.
The five-case command exercises this same schema and workflow with a separate
order-service process and database. It uses controlled fault points and an
injected clock to avoid waiting for claim expiry.
