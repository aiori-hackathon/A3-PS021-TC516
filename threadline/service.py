"""Gateway / service layer: the only place that writes to the event log.

Flow for every message:  native adapter payload -> decode -> normalise ->
validate -> idempotency check -> append to log.  Recovery, delayed delivery and
resume are operator actions built on top of the same path.
"""

import time
import uuid

from . import simulator
from .adapters import ADAPTERS, from_wire, to_wire
from .events import normalise, validate
from .recovery import replay
from .store import EventStore


class ApiError(Exception):
    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details

    def payload(self):
        err = {"code": self.code, "message": self.message}
        if self.details is not None:
            err["details"] = self.details
        return {"error": err}


class Threadline:
    def __init__(self, store=None):
        self.store = store or EventStore()

    # -- helpers ------------------------------------------------------
    def _conversation(self, cid):
        conv = self.store.get_conversation(cid)
        if conv is None:
            raise ApiError(404, "conversation_not_found", f"No conversation with id '{cid}'.")
        return conv

    def ingest(self, env, note=None):
        """Validate, classify against the live log, and append. Returns the outcome.

        The live classification is what a real gateway uses to avoid
        *processing* a message twice (e.g. triggering a second assistant
        reply). The delivery is still appended so the log stays complete;
        recovery re-derives the same verdict from the log.
        """
        env = normalise(env)
        problems = validate(env)
        if problems:
            raise ApiError(400, "invalid_envelope", "Envelope failed validation.", problems)
        existing = self.store.events(env["conversation_id"])
        same_id = [e for e in existing if e["message_id"] == env["message_id"]]
        if same_id and any(e["idempotency_key"] != env["idempotency_key"] for e in same_id):
            outcome = "conflict"  # message_id reused for a different step
        elif same_id or any(e["idempotency_key"] == env["idempotency_key"] for e in existing):
            outcome = "duplicate"
        elif env["kind"] != "signal" and any(e["sequence"] == env["sequence"] for e in existing):
            outcome = "conflict"  # a different step already claims this sequence slot
        else:
            outcome = "accepted"
        event = self.store.append(env, note=note)
        return {"outcome": outcome, "processed": outcome == "accepted", "event": event}

    def receive(self, native_env, note=None):
        """Send an envelope through its adapter's wire protocol, then ingest it."""
        if native_env["kind"] == "signal":  # raised by the gateway itself, not an adapter
            return self.ingest(native_env, note=note)
        wire = to_wire(native_env)
        return self.ingest(from_wire(native_env["adapter"], wire), note=note)

    # -- experiments --------------------------------------------------
    def run_experiment(self, scenario=None, faults=None):
        if scenario is not None and scenario not in simulator.SCENARIOS:
            raise ApiError(400, "unknown_scenario", f"Unknown scenario '{scenario}'.",
                           sorted(simulator.SCENARIOS))
        if faults is None:
            faults = simulator.SCENARIOS[scenario or "happy_path"]["faults"]
        if not isinstance(faults, list) or not all(isinstance(f, str) for f in faults):
            raise ApiError(400, "invalid_faults", "'faults' must be a list of fault names.")
        unknown = [f for f in faults if f not in simulator.FAULTS]
        if unknown:
            raise ApiError(400, "unknown_fault", f"Unknown fault(s): {', '.join(unknown)}.",
                           sorted(simulator.FAULTS))
        faults = [f for f in simulator.FAULTS if f in faults]  # canonical order, no repeats
        if scenario is None:
            scenario = next((k for k, v in simulator.SCENARIOS.items() if v["faults"] == faults), "custom")
        cid = "conv_" + uuid.uuid4().hex[:10]
        with self.store.lock:
            self.store.create_conversation(cid, scenario, faults)
            deliveries, held = simulator.plan(cid, faults)
            for env, note in deliveries:
                self.receive(env, note)
            for env, reason in held:
                self.store.hold(cid, env, reason)
            self.store.set_status(cid, "interrupted" if "adapter_timeout" in faults else "awaiting_recovery")
        return self.snapshot(cid)

    # -- operator actions ---------------------------------------------
    def recover(self, cid):
        conv = self._conversation(cid)
        with self.store.lock:
            started = time.perf_counter()
            result = replay(self.store.events(cid), cid)
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            history = self.store.recoveries(cid)
            prev = history[-1] if history else None
            if prev is None:
                check = {"previous_runs": 0, "verdict": "first_run"}
            elif prev["log_watermark"] == result["log_watermark"]:
                same = prev["context_hash"] == result["context_hash"]
                check = {"previous_runs": len(history), "previous_hash": prev["context_hash"],
                         "verdict": "identical" if same else "DIVERGED"}
            else:
                check = {"previous_runs": len(history), "previous_hash": prev["context_hash"],
                         "verdict": "log_changed",
                         "new_events": result["log_watermark"] - prev["log_watermark"]}
            self.store.record_recovery(cid, result["log_watermark"], result["context_hash"],
                                       result["status"], duration_ms, result["metrics"])
            self.store.set_status(cid, result["status"])
        return {
            "conversation_id": cid, "scenario": conv["scenario"], "duration_ms": duration_ms,
            "idempotency_check": check, **result,
        }

    def deliver_pending(self, cid):
        self._conversation(cid)
        delivered = []
        with self.store.lock:
            for item in self.store.in_flight(cid):
                delivered.append(self.receive(item["envelope"], note="late arrival: released from transit"))
                self.store.release(item["id"])
            if delivered:
                self.store.set_status(cid, "awaiting_recovery")
        return {"delivered": len(delivered), "outcomes": delivered, "conversation": self.snapshot(cid)}

    def resume(self, cid):
        conv = self._conversation(cid)
        with self.store.lock:
            result = replay(self.store.events(cid), cid)
            point = result["resume"]
            if not point["resumable"]:
                raise ApiError(409, "not_resumable", "Conversation cannot be resumed safely.", point["blockers"])
            if conv["scenario"] == "external":
                raise ApiError(409, "no_simulated_adapter",
                               "Resume drives the simulated adapters; external conversations must be resumed by their own adapter.")
            produced = simulator.continuation(cid, point["next_sequence"], point["causation_id"])
            outcomes = []
            for env in produced:
                out = self.receive(env, note=f"resumed by {env['adapter']} from recovered context")
                outcomes.append(out)
                if not out["processed"]:
                    break  # never chain a reply off a duplicate
            self.store.set_status(cid, "resumed")
        return {"resumed_from": point, "appended": len(outcomes), "outcomes": outcomes,
                "conversation": self.snapshot(cid)}

    def ingest_external(self, cid, body):
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_body", "Body must be a JSON object (a canonical envelope).")
        if body.get("conversation_id", cid) != cid:
            raise ApiError(400, "conversation_mismatch", "conversation_id in body does not match the URL.")
        if body.get("adapter") not in ADAPTERS:
            raise ApiError(400, "unknown_adapter", f"adapter must be one of {', '.join(ADAPTERS)}.")
        env = {"kind": "message", **body, "conversation_id": cid}
        problems = validate(normalise(env))
        if problems:
            raise ApiError(400, "invalid_envelope", "Envelope failed validation.", problems)
        with self.store.lock:
            if self.store.get_conversation(cid) is None:
                self.store.create_conversation(cid, "external", [])
            out = self.ingest(env, note="external ingest")
            self.store.set_status(cid, "awaiting_recovery")
        return out

    # -- read models ----------------------------------------------------
    def snapshot(self, cid):
        conv = self._conversation(cid)
        events = self.store.events(cid)
        history = self.store.recoveries(cid)
        watermark = events[-1]["log_offset"] if events else 0
        return {
            "conversation": conv,
            "events": events,
            "in_flight": self.store.in_flight(cid),
            "recoveries": [{k: r[k] for k in ("id", "context_hash", "status", "duration_ms", "log_watermark", "created_at")}
                           for r in history],
            "needs_recovery": not history or history[-1]["log_watermark"] < watermark,
        }

    def list_conversations(self):
        return self.store.list_conversations()

    @staticmethod
    def catalogue():
        return {
            "scenarios": [{"id": k, **v} for k, v in simulator.SCENARIOS.items()],
            "faults": [{"id": k, **v} for k, v in simulator.FAULTS.items()],
            "adapters": [{"id": a.name, "protocol": a.protocol} for a in ADAPTERS.values()],
        }
