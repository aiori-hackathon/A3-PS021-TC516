# PS-021 regional restart lab

```sh
python3 -m threadline.restart_lab --scenario after_commit
python3 -m threadline.restart_lab
python3 -m threadline.restart_lab --json
```

The first command is the stage demo. The second checks five cases. The third includes
process IDs, exit codes, operation keys and replacement IDs for the evidence record.
The lab runs offline with Python's standard library and temporary SQLite files.
It does not use or modify the dashboard database.

## Experimental dialog-state schema: `threadline-experimental-v1`

A dialog groups interactions. A task is the work within that dialog. A delivery
is an attempt to pass the handoff or reply. Retrying a delivery does not create
another task. This lab has one replacement handoff per dialog.

| Field | Meaning |
|---|---|
| `schema` | Version of this experimental wrapper |
| `dialog_id` | Stable conversation identifier, also carried by each adapter payload |
| `task_id` | Stable task identifier scoped to the dialog |
| `wire` | Planner headers + body on handoff; tool JSON-RPC-shaped payload on reply |
| `operation_key` | Handoff slot's existing Threadline idempotency key; saved with the task |
| `state` | Durable task state; the result belongs to the same operation key |

The planner and tool are mock adapters in separate OS processes. The harness
relays payloads through stdin/stdout and injects process exits at known boundaries.
The same file is reopened after each exit. The tool checks the received handoff
against the stored one; the planner checks the returned result against the committed
operation before acknowledging. Reusing an ID with a different handoff is rejected.

| From | Event | To |
|---|---|---|
| No task | Planner saves handoff | `pending` |
| `pending` | Tool commits replacement row and state in one transaction | `committed` |
| `committed` | Planner validates and acknowledges the tool result | `complete` |
| Any persisted state | Recover/read | Same state |
| `committed` or `complete` | Tool receives an identical retry | Same state; return existing result |
| `complete` | Repeat acknowledgement | Same state |

SQLite rejects backward and skipped state transitions. A crash before commit
leaves the task pending and rolls back the uncommitted effect. A crash after
commit leaves the result available even if no reply ever reached the caller.

```text
on tool request:
    BEGIN IMMEDIATE
    load task by dialog_id and task_id
    reject if handoff differs from the one on disk
    if task is already committed or complete:
        return saved result
    insert replacement keyed by operation_key
    mark task committed in the same transaction
    COMMIT
    reply with replacement ID
```

## What the five cases check

| Case | Recovery check |
|---|---|
| Planner exits after saving, before delivery | Restart finds the original pending handoff |
| Tool exits after insert, before commit | SQLite rolls back; retry creates the first effect |
| Tool exits after commit, before reply | Retry returns the ID already on disk |
| Two tool calls overlap | Both return the same ID; only one creates a record |
| Planner exits before acknowledging result | Restart sees the committed result and completes the original task |

Each case also reopens the completed task, submits a late retry, and checks
that there is still exactly one replacement row. The test observes stored
effects, which is different from counting messages in the replay dashboard.

This proves one effect **inside this SQLite transaction**. If the real effect
is a shipment or payment in another system, that system must honor a stable
idempotency key or support reconciliation. The lab does not emulate a radio
network, claim complete MCP/A2A interoperability, or implement an IETF standard.
See the [IETF AgentProto charter](https://datatracker.ietf.org/wg/agentproto/about/)
for the broader problem and the [IMT-2030 framework](https://www.itu.int/rec/R-REC-M.2160-0-202311-I/_page.print)
for the future-network context.
