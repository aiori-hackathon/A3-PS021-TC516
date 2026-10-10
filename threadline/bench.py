"""Run every scenario through the full operator workflow and print a results table.

    python -m threadline.bench            # table
    python -m threadline.bench --json     # machine-readable

Workflow per scenario: run -> recover -> (deliver delayed -> recover)
-> (resume -> recover) -> recover again to prove idempotency.
"""

import argparse
import json
import statistics

from .service import Threadline
from .simulator import SCENARIOS


def run_scenario(app, name, repeats=20):
    cid = app.run_experiment(name)["conversation"]["conversation_id"]
    first = app.recover(cid)
    delivered = app.deliver_pending(cid)["delivered"]
    if delivered:
        app.recover(cid)
    current = app.recover(cid)
    resumed = 0
    if current["resume"]["resumable"]:
        resumed = app.resume(cid)["appended"]
    final = app.recover(cid)
    again = app.recover(cid)
    timings = [app.recover(cid)["duration_ms"] for _ in range(repeats)]
    return {
        "scenario": name,
        "first_pass": {"status": first["status"], **first["metrics"], "gaps": first["gaps"]},
        "late_delivered": delivered,
        "resumed_events": resumed,
        "final_status": final["status"],
        "final_dialog_state": final["dialog_state"],
        "final_gaps": final["gaps"],
        "idempotent": again["context_hash"] == final["context_hash"],
        "median_recovery_ms": round(statistics.median(timings), 3),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    app = Threadline()
    rows = [run_scenario(app, name) for name in SCENARIOS]
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    head = ("scenario", "events", "accepted", "dupes", "gaps", "conflicts", "reordered",
            "first status", "late", "resumed", "final status", "dialog", "idem", "ms")
    table = [head]
    for r in rows:
        f = r["first_pass"]
        table.append((r["scenario"], f["total_events"], f["accepted_events"], f["duplicates_removed"],
                      ",".join(map(str, f["gaps"])) or "-", f["conflicts"], f["reordered_events"], f["status"],
                      r["late_delivered"], r["resumed_events"], r["final_status"], r["final_dialog_state"],
                      "yes" if r["idempotent"] else "NO", r["median_recovery_ms"]))
    widths = [max(len(str(row[i])) for row in table) for i in range(len(head))]
    for i, row in enumerate(table):
        print("  ".join(str(v).ljust(w) for v, w in zip(row, widths)))
        if i == 0:
            print("  ".join("-" * w for w in widths))


if __name__ == "__main__":
    main()
