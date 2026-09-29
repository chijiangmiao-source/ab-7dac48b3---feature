"""SQLite persistence: link windows, stable receipts and the audit MMR.

Every frame arrival is resolved inside one ``BEGIN IMMEDIATE`` transaction
against *both* the sliding-window state and the receipt ledger, so that
concurrent arrivals and process restarts always derive a consistent verdict.

Every *first-time* verdict additionally appends one leaf to the global
append-only audit log (a Merkle mountain range) in that same transaction:
the window update, the receipt record, the leaf, its new mountain nodes and
the root/meta row commit together or not at all.  Identical retransmissions
return the existing leaf; receipt-id conflicts append nothing.
"""

import hashlib
import json
import sqlite3
import threading
import uuid

from . import audit
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
    -- The window bitmap is an unsigned 64-bit mask (up to 2**64-1), which
    -- does not fit in SQLite's signed INTEGER; persist it as decimal text.
    bitmap   TEXT NOT NULL DEFAULT '0'
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
-- One canonical leaf per first-time verdict, global append order by seq.
-- UNIQUE(receipt_id) makes "two leaves for one receipt" impossible, and the
-- foreign keys make leaves pointing at a missing verdict/link impossible.
CREATE TABLE IF NOT EXISTS audit_leaves (
    seq          INTEGER PRIMARY KEY,
    receipt_id   TEXT NOT NULL UNIQUE REFERENCES receipts(receipt_id),
    link_id      TEXT NOT NULL REFERENCES links(id),
    counter      INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    status       TEXT NOT NULL,
    highest      INTEGER NOT NULL,
    leaf_hash    TEXT NOT NULL
);
-- Persisted MMR nodes: height 0 rows mirror the leaves; taller rows are the
-- interior hashes.  (height, position) is immutable once written.
CREATE TABLE IF NOT EXISTS audit_nodes (
    height   INTEGER NOT NULL,
    position INTEGER NOT NULL,
    hash     TEXT NOT NULL,
    PRIMARY KEY (height, position)
) WITHOUT ROWID;
-- Exactly one row: recoverable leaf count and current mountain root.
CREATE TABLE IF NOT EXISTS audit_meta (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    leaf_count INTEGER NOT NULL,
    root       TEXT NOT NULL
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
        # Detect a database written by the pre-audit release before its
        # missing audit tables get created below.
        legacy = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='receipts'"
        ).fetchone() is not None and self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='audit_leaves'"
        ).fetchone() is None
        self.conn.executescript(SCHEMA)
        self._migrate_schema()
        self.conn.execute(
            "INSERT OR IGNORE INTO audit_meta (id, leaf_count, root) "
            "VALUES (1, 0, ?)",
            (audit.EMPTY_ROOT,),
        )
        if legacy:
            self._backfill_legacy_audit()
        self._lock = threading.Lock()
        self._recover_audit()

    def _migrate_schema(self):
        """Upgrade databases created before the bitmap became TEXT.

        The unsigned 64-bit window bitmap (up to 2**64-1) cannot live in a
        signed SQLite INTEGER, so it is stored as decimal text.  Migrate any
        legacy INTEGER column in place.
        """
        kind = self.conn.execute(
            "SELECT LOWER(type) FROM pragma_table_info('link_windows') "
            "WHERE name = 'bitmap'"
        ).fetchone()
        if kind is None or kind[0] == "text":
            return
        self.conn.executescript(
            """
            ALTER TABLE link_windows RENAME TO link_windows_old;
            CREATE TABLE link_windows (
                link_id  TEXT PRIMARY KEY REFERENCES links(id),
                highest  INTEGER,
                bitmap   TEXT NOT NULL DEFAULT '0'
            );
            INSERT INTO link_windows (link_id, highest, bitmap)
            SELECT link_id, highest, CAST(bitmap AS TEXT)
            FROM link_windows_old;
            DROP TABLE link_windows_old;
            """
        )

    def _backfill_legacy_audit(self):
        """Backfill audit leaves for a pre-audit release database.

        Historical receipts already share one commit order per link (their
        insertion order); replaying every stored verdict through the same
        window function reproduces each verdict's post-verdict highest, and
        interleaving the links in global insertion (rowid) order recovers the
        single global leaf sequence.  The replayed status is cross-checked
        against the stored verdict.
        """
        verdicts = self.conn.execute(
            "SELECT r.rowid AS rowid, r.receipt_id AS receipt_id, "
            "r.link_id AS link_id, r.counter AS counter, "
            "r.payload_hash AS payload_hash, r.status AS status "
            "FROM receipts r ORDER BY r.rowid"
        ).fetchall()
        if not verdicts:
            return

        states = {}   # link_id -> [highest, bitmap]
        mmr = audit.MMR()
        leaves = []
        nodes = []
        for seq, v in enumerate(verdicts, start=1):
            highest, bitmap = states.get(v["link_id"], (None, 0))
            status, new_highest, new_bitmap = win.decide(
                v["counter"], highest, bitmap)
            if status != v["status"]:
                raise RuntimeError(
                    "audit backfill mismatch on receipt "
                    f"{v['receipt_id']!r}: stored {v['status']!r}, "
                    f"replayed {status!r}")
            states[v["link_id"]] = [new_highest, new_bitmap]
            leaf_hash = audit.leaf_digest(
                seq, v["link_id"], v["receipt_id"], v["counter"],
                v["payload_hash"], status, new_highest)
            leaves.append((seq, v["receipt_id"], v["link_id"], v["counter"],
                           v["payload_hash"], status, new_highest, leaf_hash))
            for height, position, node_hash in mmr.add(seq, leaf_hash):
                nodes.append((height, position, node_hash))

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.executemany(
                "INSERT INTO audit_leaves (seq, receipt_id, link_id, counter, "
                "payload_hash, status, highest, leaf_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                leaves,
            )
            self.conn.executemany(
                "INSERT INTO audit_nodes (height, position, hash) "
                "VALUES (?, ?, ?)",
                nodes,
            )
            self.conn.execute(
                "UPDATE audit_meta SET leaf_count = ?, root = ? WHERE id = 1",
                (len(leaves), mmr.root()),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def close(self):
        with self._lock:
            self.conn.close()

    # -------------------------------------------------------------- recovery

    def _recover_audit(self):
        """Rebuild the peak set / root / node table from the durable leaves.

        Leaves and verdicts share one commit, so they are the source of
        truth; any derived rows left stale by an interrupted update are
        recomputed here.  Structural mismatch (a committed verdict without a
        leaf, an orphan leaf, a gap in seq) refuses to start rather than
        silently rewriting history.
        """
        with self._lock:
            leaves = self.conn.execute(
                "SELECT seq, receipt_id, link_id, counter, payload_hash, "
                "status, highest, leaf_hash FROM audit_leaves ORDER BY seq"
            ).fetchall()
            verdict_ids = {
                r["receipt_id"]
                for r in self.conn.execute(
                    "SELECT receipt_id FROM receipts").fetchall()
            }
            leaf_ids = {r["receipt_id"] for r in leaves}
            if leaf_ids != verdict_ids:
                missing = sorted(verdict_ids - leaf_ids)
                orphan = sorted(leaf_ids - verdict_ids)
                raise RuntimeError(
                    "audit storage corrupt: verdicts without leaves "
                    f"{missing[:3]} or orphan leaves {orphan[:3]}")
            for expected, row in enumerate(leaves, start=1):
                if row["seq"] != expected:
                    raise RuntimeError(
                        f"audit storage corrupt: leaf seq gap at {expected}")
                canonical = audit.leaf_digest(
                    row["seq"], row["link_id"], row["receipt_id"],
                    row["counter"], row["payload_hash"], row["status"],
                    row["highest"],
                )
                if canonical != row["leaf_hash"]:
                    raise RuntimeError(
                        "audit storage corrupt: leaf "
                        f"seq {expected} content/hash mismatch")

            mmr = audit.MMR()
            nodes = []
            for row in leaves:
                for height, position, node_hash in mmr.add(row["seq"],
                                                            row["leaf_hash"]):
                    nodes.append((height, position, node_hash))
            leaf_count = len(leaves)
            root = mmr.root()

            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("DELETE FROM audit_nodes")
                self.conn.executemany(
                    "INSERT INTO audit_nodes (height, position, hash) "
                    "VALUES (?, ?, ?)",
                    nodes,
                )
                self.conn.execute(
                    "UPDATE audit_meta SET leaf_count = ?, root = ? WHERE id = 1",
                    (leaf_count, root),
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

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
        bitmap = int(row["bitmap"] or 0)
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
        link/counter/payload.  Window state, receipt record and audit leaf
        (for a first-time verdict) are committed in the same transaction.
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
                    # a conflict.  Neither path appends an audit leaf.
                    if (prior["link_id"] != link_id
                            or prior["counter"] != counter
                            or prior["payload_hash"] != payload_hash):
                        raise ReceiptConflict(receipt_id)
                    status = prior["status"]
                    extended = prior["extended"]
                    highest = row["highest"]
                    bitmap = int(row["bitmap"] or 0)
                    leaf_row = self.conn.execute(
                        "SELECT seq FROM audit_leaves WHERE receipt_id = ?",
                        (receipt_id,),
                    ).fetchone()
                    if leaf_row is None:
                        # Recovery guarantees verdict<->leaf 1:1; a miss is
                        # storage corruption, never a silent missing leaf.
                        raise RuntimeError(
                            "audit storage corrupt: receipt "
                            f"{receipt_id!r} has no leaf")
                    leaf_seq = leaf_row["seq"]
                else:
                    status, new_highest, new_bitmap = win.decide(
                        counter, row["highest"], int(row["bitmap"] or 0)
                    )
                    extended, _ = win.extend_counter(counter, row["highest"])
                    self.conn.execute(
                        "UPDATE link_windows SET highest = ?, bitmap = ? WHERE link_id = ?",
                        (new_highest, str(new_bitmap), link_id),
                    )
                    self.conn.execute(
                        "INSERT INTO receipts (link_id, receipt_id, counter, "
                        "payload_hash, status, extended, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, strftime('%s','now'))",
                        (link_id, receipt_id, counter, payload_hash,
                         status, extended),
                    )

                    meta = self.conn.execute(
                        "SELECT leaf_count FROM audit_meta WHERE id = 1"
                    ).fetchone()
                    leaf_seq = meta["leaf_count"] + 1
                    leaf_hash = audit.leaf_digest(
                        leaf_seq, link_id, receipt_id, counter, payload_hash,
                        status, new_highest,
                    )
                    self.conn.execute(
                        "INSERT INTO audit_leaves (seq, receipt_id, link_id, "
                        "counter, payload_hash, status, highest, leaf_hash) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (leaf_seq, receipt_id, link_id, counter, payload_hash,
                         status, new_highest, leaf_hash),
                    )
                    # Extend the mountain range against the persisted peaks,
                    # then write every node this append created plus the new
                    # root.  Peaks alone are enough to perform the merges.
                    leaf_count = leaf_seq - 1
                    mmr = audit.MMR(self._load_peaks(leaf_count))
                    created = mmr.add(leaf_seq, leaf_hash)
                    self.conn.executemany(
                        "INSERT INTO audit_nodes (height, position, hash) "
                        "VALUES (?, ?, ?)",
                        created,
                    )
                    self.conn.execute(
                        "UPDATE audit_meta SET leaf_count = ?, root = ? WHERE id = 1",
                        (leaf_seq, mmr.root()),
                    )
                    highest, bitmap = new_highest, new_bitmap

                root = self.conn.execute(
                    "SELECT root FROM audit_meta WHERE id = 1"
                ).fetchone()["root"]
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
            "audit": {
                "seq": leaf_seq,
                "root": root,
            },
        }

    def _load_peaks(self, leaf_count):
        """Load ``{height: (position, hash)}`` for the current peak set."""
        peaks = {}
        for height, position in audit.peak_layout(leaf_count):
            row = self.conn.execute(
                "SELECT hash FROM audit_nodes WHERE height = ? AND position = ?",
                (height, position),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    f"audit storage corrupt: missing peak {height}@{position}")
            peaks[height] = (position, row["hash"])
        return peaks

    # ----------------------------------------------------------------- audit

    def audit_state(self):
        """Current leaf count, root and ordered peaks (highest peak first)."""
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                meta = self.conn.execute(
                    "SELECT leaf_count, root FROM audit_meta WHERE id = 1"
                ).fetchone()
                leaf_count, root = meta["leaf_count"], meta["root"]
                peaks = []
                for height, position in audit.peak_layout(leaf_count):
                    row = self.conn.execute(
                        "SELECT hash FROM audit_nodes "
                        "WHERE height = ? AND position = ?",
                        (height, position),
                    ).fetchone()
                    peaks.append({"height": height, "position": position,
                                  "hash": row["hash"] if row else None})
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return {"leaf_count": leaf_count, "root": root, "peaks": peaks}

    def get_receipt_audit(self, receipt_id):
        """Full inclusion record for a receipt's leaf against the current root.

        Returns the leaf fields, its global seq, the current root/leaf count,
        the ordered peak set and the minimal sibling path (leaf -> peak).
        ``None`` for an unknown receipt id.  All reads share one WAL snapshot
        so the record can never mix two append generations.
        """
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                leaf = self.conn.execute(
                    "SELECT seq, receipt_id, link_id, counter, payload_hash, "
                    "status, highest, leaf_hash FROM audit_leaves "
                    "WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone()
                if leaf is None:
                    self.conn.execute("COMMIT")
                    return None
                meta = self.conn.execute(
                    "SELECT leaf_count, root FROM audit_meta WHERE id = 1"
                ).fetchone()
                leaf_count, root = meta["leaf_count"], meta["root"]

                peak_height, peak_position = audit.containing_peak(
                    leaf["seq"], leaf_count)
                path = []
                for height, position, side in audit.sibling_steps(
                        leaf["seq"], peak_height, peak_position):
                    row = self.conn.execute(
                        "SELECT hash FROM audit_nodes "
                        "WHERE height = ? AND position = ?",
                        (height, position),
                    ).fetchone()
                    if row is None:
                        raise RuntimeError(
                            "audit storage corrupt: missing proof node "
                            f"{height}@{position}")
                    path.append({"side": side, "hash": row["hash"]})

                peaks = []
                for height, position in audit.peak_layout(leaf_count):
                    row = self.conn.execute(
                        "SELECT hash FROM audit_nodes "
                        "WHERE height = ? AND position = ?",
                        (height, position),
                    ).fetchone()
                    peaks.append({"height": height, "position": position,
                                  "hash": row["hash"] if row else None})
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

        return {
            "leaf": {
                "seq": leaf["seq"],
                "link": leaf["link_id"],
                "receipt": leaf["receipt_id"],
                "counter": leaf["counter"],
                "payload_hash": leaf["payload_hash"],
                "status": leaf["status"],
                "highest": leaf["highest"],
                "hash": leaf["leaf_hash"],
            },
            "seq": leaf["seq"],
            "leaf_count": leaf_count,
            "root": root,
            "peak_height": peak_height,
            "peaks": peaks,
            "path": path,
        }
