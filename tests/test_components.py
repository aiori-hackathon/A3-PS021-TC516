"""Tests for the envelope rules, adapter protocols and the append-only store."""

import sqlite3
import unittest

from threadline.adapters import ADAPTERS, from_wire, to_wire
from threadline.events import make_idempotency_key, normalise, validate
from threadline.simulator import SCRIPT, chain
from threadline.store import EventStore


class IdempotencyKeys(unittest.TestCase):
    def test_deterministic(self):
        a = make_idempotency_key("c1", 3, "assistant", "m2")
        self.assertEqual(a, make_idempotency_key("c1", 3, "assistant", "m2"))

    def test_sensitive_to_every_slot_component(self):
        base = make_idempotency_key("c1", 3, "assistant", "m2")
        variants = [
            make_idempotency_key("c2", 3, "assistant", "m2"),
            make_idempotency_key("c1", 4, "assistant", "m2"),
            make_idempotency_key("c1", 3, "tool", "m2"),
            make_idempotency_key("c1", 3, "assistant", "m1"),
            make_idempotency_key("c1", 3, "assistant", "m2", kind="handoff"),
        ]
        self.assertNotIn(base, variants)
        self.assertEqual(len(set(variants)), len(variants))

    def test_normalise_derives_missing_key(self):
        env = normalise({"conversation_id": "c1", "message_id": "x", "sequence": 1, "adapter": "web-chat",
                         "role": "user", "kind": "message", "content": "hi"})
        self.assertEqual(env["idempotency_key"], make_idempotency_key("c1", 1, "user", None))
        self.assertEqual(validate(env), [])

    def test_validation_messages(self):
        problems = validate({"conversation_id": "c1", "kind": "handoff", "sequence": 0})
        joined = " | ".join(problems)
        for needle in ("message_id", "adapter", "sequence must be >= 1", "target_adapter"):
            self.assertIn(needle, joined)
        self.assertEqual(validate("nope"), ["envelope must be a JSON object"])


class AdapterProtocols(unittest.TestCase):
    def test_round_trip_preserves_correlation_metadata(self):
        envs = chain("conv_rt", SCRIPT)
        protocols = set()
        for env in envs:
            wire = to_wire(env)
            protocols.add(wire["protocol"])
            back = from_wire(env["adapter"], wire)
            for field in ("conversation_id", "message_id", "sequence", "causation_id", "idempotency_key",
                          "role", "kind", "content", "adapter"):
                self.assertEqual(back[field], env[field], f"{env['adapter']} lost {field}")
        self.assertEqual(protocols, {a.protocol for a in ADAPTERS.values()})
        self.assertEqual(len(protocols), 3)

    def test_wire_formats_differ(self):
        env = chain("conv_rt", SCRIPT)[0]
        payloads = {name: to_wire({**env, "adapter": name})["payload"] for name in ADAPTERS}
        self.assertIn("session", payloads["web-chat"])
        self.assertIn("headers", payloads["planner-agent"])
        self.assertEqual(payloads["tool-agent"]["jsonrpc"], "2.0")


class Store(unittest.TestCase):
    def setUp(self):
        self.store = EventStore(":memory:")
        self.store.create_conversation("c1", "test", [])
        self.env = normalise({"conversation_id": "c1", "message_id": "m1", "sequence": 1, "adapter": "web-chat",
                              "role": "user", "kind": "message", "content": "hi"})

    def test_offsets_are_monotonic_and_duplicates_kept(self):
        a = self.store.append(self.env)
        b = self.store.append(self.env)  # a duplicate delivery is still evidence
        self.assertEqual((a["log_offset"], b["log_offset"]), (1, 2))
        self.assertEqual([e["event_id"] for e in self.store.events("c1")], ["evt_000001", "evt_000002"])

    def test_log_is_append_only(self):
        self.store.append(self.env)
        with self.assertRaises(sqlite3.DatabaseError):
            with self.store.db:
                self.store.db.execute("UPDATE events SET content='tampered'")
        with self.assertRaises(sqlite3.DatabaseError):
            with self.store.db:
                self.store.db.execute("DELETE FROM events")
        self.assertEqual(self.store.events("c1")[0]["content"], "hi")

    def test_reset_clears_everything(self):
        self.store.append(self.env)
        self.store.reset()
        self.assertEqual(self.store.list_conversations(), [])
        self.assertEqual(self.store.events("c1"), [])


if __name__ == "__main__":
    unittest.main()
