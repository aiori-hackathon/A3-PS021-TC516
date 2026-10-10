"""SQLite-backed append-only event log.

Every delivery that reaches the gateway is appended, including duplicates,
late arrivals and conflicting claims. Nothing is filtered on write: the log is
the evidence, and recovery decides what is valid by replaying it. SQLite
triggers make the ``events`` table physically append-only.
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    scenario        TEXT NOT NULL,
    faults          TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    log_offset      INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL,
    message_id      TEXT,
    sequence        INTEGER,
    causation_id    TEXT,
    idempotency_key TEXT,
    adapter         TEXT,
    role            TEXT,
    kind            TEXT,
    target_adapter  TEXT,
    content         TEXT,
    sent_at         TEXT,
    received_at     TEXT NOT NULL,
    native          TEXT,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_conv ON events(conversation_id, log_offset);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'event log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'event log is append-only'); END;
CREATE TABLE IF NOT EXISTS in_flight (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    envelope        TEXT NOT NULL,
    reason          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recoveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    log_watermark   INTEGER NOT NULL,
    context_hash    TEXT NOT NULL,
    status          TEXT NOT NULL,
    duration_ms     REAL NOT NULL,
    summary         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
"""

EVENT_COLUMNS = (
    "log_offset", "event_id", "conversation_id", "message_id", "sequence", "causation_id",
    "idempotency_key", "adapter", "role", "kind", "target_adapter", "content", "sent_at",
    "received_at", "native", "note",
)


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class EventStore:
    def __init__(self, path=":memory:"):
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # -- conversations -------------------------------------------------
    def create_conversation(self, conversation_id, scenario, faults):
        ts = utcnow()
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO conversations VALUES (?,?,?,?,?,?)",
                (conversation_id, scenario, json.dumps(list(faults)), "active", ts, ts),
            )

    def get_conversation(self, conversation_id):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)
            ).fetchone()
        return self._conv(row) if row else None

    def list_conversations(self, limit=50):
        with self.lock:
            rows = self.db.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM events e WHERE e.conversation_id=c.conversation_id) AS n "
                "FROM conversations c ORDER BY c.created_at DESC, c.rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{**self._conv(r), "event_count": r["n"]} for r in rows]

    def set_status(self, conversation_id, status):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE conversations SET status=?, updated_at=? WHERE conversation_id=?",
                (status, utcnow(), conversation_id),
            )

    @staticmethod
    def _conv(row):
        d = dict(row)
        d["faults"] = json.loads(d["faults"])
        d.pop("n", None)
        return d

    # -- event log -----------------------------------------------------
    def append(self, env, received_at=None, note=None):
        """Append one delivery; returns the stored event with its log offset."""
        with self.lock, self.db:
            cur = self.db.execute("SELECT COALESCE(MAX(log_offset), 0) + 1 FROM events")
            offset = cur.fetchone()[0]
            event_id = f"evt_{offset:06d}"
            native = env.get("native")
            self.db.execute(
                f"INSERT INTO events ({', '.join(EVENT_COLUMNS)}) VALUES ({', '.join('?' * len(EVENT_COLUMNS))})",
                (
                    offset, event_id, env.get("conversation_id"), env.get("message_id"),
                    env.get("sequence"), env.get("causation_id"), env.get("idempotency_key"),
                    env.get("adapter"), env.get("role"), env.get("kind"), env.get("target_adapter"),
                    env.get("content"), env.get("sent_at"), received_at or utcnow(),
                    json.dumps(native) if native is not None else None, note,
                ),
            )
            self.db.execute(
                "UPDATE conversations SET updated_at=? WHERE conversation_id=?",
                (utcnow(), env.get("conversation_id")),
            )
        return self.events(env.get("conversation_id"), after=offset - 1)[0]

    def events(self, conversation_id, after=0):
        """All deliveries for a conversation in arrival (log) order."""
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM events WHERE conversation_id=? AND log_offset>? ORDER BY log_offset",
                (conversation_id, after),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["native"] = json.loads(d["native"]) if d["native"] else None
            out.append(d)
        return out

    # -- simulated network: messages held in flight --------------------
    def hold(self, conversation_id, env, reason):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO in_flight (conversation_id, envelope, reason) VALUES (?,?,?)",
                (conversation_id, json.dumps(env), reason),
            )

    def in_flight(self, conversation_id):
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM in_flight WHERE conversation_id=? ORDER BY id", (conversation_id,)
            ).fetchall()
        return [{"id": r["id"], "reason": r["reason"], "envelope": json.loads(r["envelope"])} for r in rows]

    def release(self, flight_id):
        with self.lock, self.db:
            self.db.execute("DELETE FROM in_flight WHERE id=?", (flight_id,))

    # -- recovery audit trail ------------------------------------------
    def record_recovery(self, conversation_id, watermark, context_hash, status, duration_ms, summary):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO recoveries (conversation_id, log_watermark, context_hash, status, duration_ms, summary, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (conversation_id, watermark, context_hash, status, duration_ms, json.dumps(summary), utcnow()),
            )

    def recoveries(self, conversation_id):
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM recoveries WHERE conversation_id=? ORDER BY id", (conversation_id,)
            ).fetchall()
        return [{**dict(r), "summary": json.loads(r["summary"])} for r in rows]

    def reset(self):
        """Wipe all demo data (drops tables, since the log itself refuses DELETE)."""
        with self.lock:
            self.db.executescript(
                "DROP TABLE IF EXISTS events; DROP TABLE IF EXISTS conversations;"
                "DROP TABLE IF EXISTS in_flight; DROP TABLE IF EXISTS recoveries;"
            )
            self.db.executescript(SCHEMA)
