"""SQLite persistence: link windows and stable receipts.

Every frame arrival is resolved inside one ``BEGIN IMMEDIATE`` transaction
against *both* the sliding-window state and the receipt ledger, so that
concurrent arrivals and process restarts always derive a consistent verdict.
"""

import hashlib
import json
import sqlite3
import threading
import uuid

from . import window as win

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS link_windows (
    link_id  TEXT PRIMARY KEY REFERENCES links(id),
    highest  INTEGER,
    bitmap   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id   TEXT PRIMARY KEY,
    link_id      TEXT NOT NULL REFERENCES links(id),
    counter      INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    status       TEXT NOT NULL,
    extended     INTEGER NOT NULL,
    created_at   REAL NOT NULL
);
"""


class ReceiptConflict(Exception):
    """A stable receipt id was reused with a different link/counter/payload."""


def canonical_hash(payload):
    """Deterministic hash of an arbitrary JSON payload."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class Database:
    def __init__(self, path):
        self.path = path
        # check_same_thread=False: http.server spawns a thread per request;
        # the lock + BEGIN IMMEDIATE serialise write transactions.
        # isolation_level=None => autocommit unless we open a transaction
        # explicitly, so all BEGIN/COMMIT boundaries below are real.
        self.conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self):
        with self._lock:
            self.conn.close()

    # ---------------------------------------------------------------- links

    def create_link(self, name):
        link_id = uuid.uuid4().hex
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "INSERT INTO links (id, name, created_at) "
                    "VALUES (?, ?, strftime('%s','now'))",
                    (link_id, name),
                )
                self.conn.execute(
                    "INSERT INTO link_windows (link_id, highest, bitmap) "
                    "VALUES (?, NULL, 0)",
                    (link_id,),
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return self.get_link(link_id)

    def get_link(self, link_id):
        row = self.conn.execute(
            "SELECT l.id, l.name, w.highest, w.bitmap FROM links l "
            "JOIN link_windows w ON w.link_id = l.id WHERE l.id = ?",
            (link_id,),
        ).fetchone()
        if row is None:
            return None
        return self._link_state(row)

    def list_links(self):
        rows = self.conn.execute(
            "SELECT l.id, l.name, w.highest, w.bitmap FROM links l "
            "JOIN link_windows w ON w.link_id = l.id ORDER BY l.created_at"
        ).fetchall()
        return [self._link_state(r) for r in rows]

    @staticmethod
    def _link_state(row):
        highest = row["highest"]
        bitmap = row["bitmap"] or 0
        return {
            "id": row["id"],
            "name": row["name"],
            "highest": highest,
            "bitmap": bitmap,
            "recent": win.recent_positions(highest, bitmap),
        }

    # ---------------------------------------------------------------- frames

    def submit_frame(self, link_id, counter, receipt_id, payload):
        """Atomically resolve a frame arrival.

        Returns the verdict dict.  Raises ``LookupError`` for an unknown link
        and ``ReceiptConflict`` when a receipt id is reused with changed
        link/counter/payload.  Window state and receipt record are committed in
        the same transaction.
        """
        payload_hash = canonical_hash(payload)
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT highest, bitmap FROM link_windows WHERE link_id = ?",
                    (link_id,),
                ).fetchone()
                if row is None:
                    raise LookupError(link_id)

                prior = self.conn.execute(
                    "SELECT link_id, counter, payload_hash, status, extended "
                    "FROM receipts WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone()

                retransmit = prior is not None
                if retransmit:
                    # Stable receipt ids are global: an identical retransmission
                    # (same link, counter and payload) replays the first verdict;
                    # reusing the id with the link, counter or payload changed is
                    # a conflict.
                    if (prior["link_id"] != link_id
                            or prior["counter"] != counter
                            or prior["payload_hash"] != payload_hash):
                        raise ReceiptConflict(receipt_id)
                    status = prior["status"]
                    extended = prior["extended"]
                    highest = row["highest"]
                    bitmap = row["bitmap"] or 0
                else:
                    status, new_highest, new_bitmap = win.decide(
                        counter, row["highest"], row["bitmap"] or 0
                    )
                    extended, _ = win.extend_counter(counter, row["highest"])
                    self.conn.execute(
                        "UPDATE link_windows SET highest = ?, bitmap = ? WHERE link_id = ?",
                        (new_highest, new_bitmap, link_id),
                    )
                    self.conn.execute(
                        "INSERT INTO receipts (link_id, receipt_id, counter, "
                        "payload_hash, status, extended, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, strftime('%s','now'))",
                        (link_id, receipt_id, counter, payload_hash,
                         status, extended),
                    )
                    highest, bitmap = new_highest, new_bitmap

                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

        return {
            "status": status,
            "retransmit": retransmit,
            "counter": counter,
            "extended": extended,
            "highest": highest,
            "recent": win.recent_positions(highest, bitmap),
        }
