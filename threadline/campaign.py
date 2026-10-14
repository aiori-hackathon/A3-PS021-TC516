"""Seeded random fault campaign: Threadline vs. naive recovery on the same logs.

    python -m threadline.campaign                          # 1000 trials, seed 2026
    python -m threadline.campaign --trials 5000 --seed 7
    python -m threadline.campaign --json

The fixed scenarios in ``simulator.py`` show *what* each fault does. This
campaign measures *how often* recovery is right when faults land at random
positions. Each trial takes the scripted 9-step dialog, sends every message
through its adapter's wire protocol and back, then injects random network
behaviour:

* redelivery of the same message (same ``message_id``),
* adapter retries of an assistant step (new ``message_id``, new wording),
* random network delay, so messages and duplicates overtake each other,
* per-adapter clock skew on ``sent_at``,
* loss of up to two messages.

The resulting arrival-ordered log is recovered three ways:

* ``threadline``: ``recovery.replay()``, the engine the service uses;
* ``naive_arrival``: arrival order, deduplicated by ``message_id``
  (a typical at-least-once consumer);
* ``naive_timestamp``: sorted by ``sent_at``, deduplicated by ``message_id``.

A strategy's context is *exact* when it holds every delivered slot once, in
sequence order, and nothing else. Loss is *surfaced* when the strategy reports
something other than a clean, complete dialog. Same seed, same trials, same
numbers: the campaign uses its own ``random.Random`` and no clock except the
optional replay timing.
"""

import argparse
import json
import random
import statistics
import time

from .adapters import from_wire, to_wire
from .recovery import replay
from .simulator import SCRIPT, _ts, build_envelope, chain

STRATEGIES = ("threadline", "naive_arrival", "naive_timestamp")

DEFAULTS = {
    "p_redeliver": 0.15,   # per message: the network delivers it twice
    "p_retry": 0.12,       # per assistant step: the adapter regenerates it
    "p_delay": 0.35,       # per delivery: held in the network for a while
    "mean_delay_ms": 900,  # messages are produced 400 ms apart
    "max_skew_ms": 600,    # per-adapter clock offset on sent_at
    "p_loss": 0.06,        # per message, at most two per trial
}


def _trial(rng, index, p):
    """Build one faulty, arrival-ordered log and its ground truth."""
    cid = f"conv_campaign_{index:05d}"
    originals = chain(cid, SCRIPT)
    skew = {a: rng.uniform(-p["max_skew_ms"], p["max_skew_ms"]) for a in ("web-chat", "planner-agent", "tool-agent")}
    faults = {"redeliveries": 0, "retries": 0, "delayed": 0, "lost": 0}

    lost = set()
    for env in originals:
        if len(lost) < 2 and rng.random() < p["p_loss"]:
            lost.add(env["sequence"])
    faults["lost"] = len(lost)

    copies = []  # (produced_at_ms, envelope)
    for i, env in enumerate(originals):
        seq = env["sequence"]
        if seq in lost:
            continue
        produced = seq * 400
        copies.append((produced, env))
        if rng.random() < p["p_redeliver"]:
            faults["redeliveries"] += 1
            copies.append((produced + rng.uniform(50, 2500), dict(env)))
        if env["role"] == "assistant" and rng.random() < p["p_retry"]:
            faults["retries"] += 1
            cause = originals[i - 1]["message_id"] if i else None
            retry = build_envelope(cid, SCRIPT[i], cause, variant=f"retry{index}",
                                   content="(regenerated) " + env["content"])
            copies.append((produced + rng.uniform(100, 2000), retry))

    arrivals = []
    for produced, env in copies:
        sent = produced + skew[env["adapter"]]
        env = {**env, "sent_at": _ts(sent)}
        delay = rng.uniform(5, 40)
        if rng.random() < p["p_delay"]:
            faults["delayed"] += 1
            delay += rng.expovariate(1 / p["mean_delay_ms"])
        # Every delivery crosses its adapter's wire protocol and back.
        decoded = from_wire(env["adapter"], to_wire(env))
        decoded.pop("native", None)
        arrivals.append((produced + delay, rng.random(), decoded))
    arrivals.sort(key=lambda a: (a[0], a[1]))

    log = [{**env, "log_offset": n + 1, "event_id": f"evt_{n + 1:06d}", "received_at": None}
           for n, (_, _, env) in enumerate(arrivals)]
    delivered = sorted({e["sequence"] for e in log})
    keys = {e["sequence"]: e["idempotency_key"] for e in originals}
    return cid, log, {"delivered": delivered, "lost": sorted(lost), "keys": keys}, faults


def _naive(log, order):
    seen, out = set(), []
    events = log if order == "arrival" else sorted(log, key=lambda e: (e["sent_at"], e["log_offset"]))
    for e in events:
        if e["message_id"] not in seen:
            seen.add(e["message_id"])
            out.append(e)
    return out


def _score(context, truth, surfaced):
    seqs = [c["sequence"] for c in context]
    dup = len(seqs) - len(set(seqs))
    inversions = sum(1 for a, b in zip(seqs, seqs[1:]) if b < a)
    legit = all(c["idempotency_key"] == truth["keys"][c["sequence"]] for c in context)
    exact = seqs == truth["delivered"] and legit
    lost = bool(truth["lost"])
    return {
        "exact": exact,
        "duplicate_replies": dup,
        "order_errors": inversions,
        "loss_surfaced": lost and surfaced,
        "silent_loss": lost and not surfaced,
        "false_alarm": (not lost) and surfaced,
    }


def run(trials=1000, seed=2026, timing=True, **overrides):
    p = {**DEFAULTS, **overrides}
    rng = random.Random(seed)
    totals = {s: {"exact": 0, "duplicate_replies": 0, "order_errors": 0, "loss_surfaced": 0,
                  "silent_loss": 0, "false_alarm": 0} for s in STRATEGIES}
    faults = {"redeliveries": 0, "retries": 0, "delayed": 0, "lost": 0}
    trials_with_loss = deterministic = order_invariant = 0
    timings = []

    for i in range(trials):
        cid, log, truth, f = _trial(rng, i, p)
        for k in faults:
            faults[k] += f[k]
        trials_with_loss += bool(truth["lost"])

        started = time.perf_counter() if timing else 0
        result = replay(log, cid)
        if timing:
            timings.append((time.perf_counter() - started) * 1000)
        by_id = {e["event_id"]: e for e in log}
        context = [{**c, "idempotency_key": by_id[c["event_id"]]["idempotency_key"]} for c in result["context"]]
        surfaced = result["status"] == "incomplete" or result["dialog_state"] != "complete"
        scored = {"threadline": _score(context, truth, surfaced)}
        for name, order in (("naive_arrival", "arrival"), ("naive_timestamp", "timestamp")):
            # A naive consumer has no notion of a gap, so it never surfaces loss.
            scored[name] = _score(_naive(log, order), truth, False)
        for s in STRATEGIES:
            for k, v in scored[s].items():
                totals[s][k] += int(v)

        # Same log -> same hash; any other arrival order -> same slots and gaps.
        deterministic += replay(log, cid)["context_hash"] == result["context_hash"]
        shuffled = log[:]
        rng.shuffle(shuffled)
        shuffled = [{**e, "log_offset": n + 1, "event_id": f"evt_{n + 1:06d}"} for n, e in enumerate(shuffled)]
        again = replay(shuffled, cid)
        slots = [(c["sequence"], by_id[c["event_id"]]["idempotency_key"]) for c in result["context"]]
        by_new = {e["event_id"]: e for e in shuffled}
        slots_again = [(c["sequence"], by_new[c["event_id"]]["idempotency_key"]) for c in again["context"]]
        order_invariant += slots == slots_again and result["gaps"] == again["gaps"]

    return {
        "trials": trials,
        "seed": seed,
        "parameters": p,
        "faults_injected": faults,
        "trials_with_loss": trials_with_loss,
        "strategies": totals,
        "threadline_deterministic": deterministic,
        "threadline_order_invariant": order_invariant,
        "median_replay_ms": round(statistics.median(timings), 3) if timings else None,
    }


def _pct(n, d):
    return f"{100 * n / d:.1f}%" if d else "n/a"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--trials", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    r = run(args.trials, args.seed)
    if args.json:
        print(json.dumps(r, indent=2))
        return
    t, lossy = r["trials"], r["trials_with_loss"]
    f = r["faults_injected"]
    print(f"Fault campaign: {t} trials, seed {r['seed']}")
    print(f"Injected: {f['redeliveries']} redeliveries, {f['retries']} adapter retries, "
          f"{f['delayed']} delayed deliveries, {f['lost']} lost messages ({lossy} trials with loss)\n")
    head = ("strategy", "exact context", "duplicate replies", "order errors", "loss surfaced", "silent loss",
            "false alarms")
    table = [head]
    for s in STRATEGIES:
        v = r["strategies"][s]
        table.append((s, _pct(v["exact"], t), v["duplicate_replies"], v["order_errors"],
                      _pct(v["loss_surfaced"], lossy), v["silent_loss"], v["false_alarm"]))
    widths = [max(len(str(row[i])) for row in table) for i in range(len(head))]
    for i, row in enumerate(table):
        print("  ".join(str(x).ljust(w) for x, w in zip(row, widths)))
        if i == 0:
            print("  ".join("-" * w for w in widths))
    print(f"\nThreadline replay deterministic: {r['threadline_deterministic']}/{t}; "
          f"same slots and gaps under a random re-ordering: {r['threadline_order_invariant']}/{t}; "
          f"median replay {r['median_replay_ms']} ms")


if __name__ == "__main__":
    main()
