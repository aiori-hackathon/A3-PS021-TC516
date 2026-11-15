# PS-021 mentor demo and pseudocode

Start the dashboard with `python3 server.py --orders data/order-workflow --demo-faults`
and open http://127.0.0.1:8080. Use a new mock order ID and select **Drop the first
reply after commit**. After completion, compare the two attempts with the one receipt
under **Check order ledger**.

The receiver handles the ambiguous retry inside one transaction in its own
SQLite database:

```text
replace(order_id, operation_key):
    begin write transaction
    existing = lookup replacement by operation_key
    if existing:
        if existing.order_id differs from order_id:
            return conflict
        return existing receipt
    receipt = create replacement row(order_id, operation_key)
    commit transaction
    if the demo disconnect is enabled: close connection
    return receipt
```

The planner saves the operation key before dispatch. If the first response
disappears, its protected retry sends the same key. The mock receiver stores
the replacement and its receipt in one row, with a unique key. A real provider
needs to honor that contract or provide a reliable query for reconciliation.

## Evidence to reproduce

```sh
python3 -m unittest discover -s tests -t . -v
python3 -m tests.workflow_evidence
python3 -m threadline.receipt_lab --order 4821 --show-unknown
```

The second command runs five failures against the same workflow used by the
dashboard. It checks task identity and reads the effect service's SQLite ledger
directly. The last command runs an actual HTTP
disconnect with protection off and on, then a case that holds the task for review.
