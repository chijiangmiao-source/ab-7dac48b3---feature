"""SQLite persistence: link windows, stable receipts and the MMR audit trail.

Every frame arrival is resolved inside one ``BEGIN IMMEDIATE`` transaction
against *both* the sliding-window state and the receipt ledger, so that
concurrent arrivals and process restarts always derive a consistent verdict.

Each first-time verdict additionally appends a canonical audit leaf to a
Merkle mountain range, in that same transaction.  Retransmissions only return
the stored leaf; receipt-id conflicts append nothing.  The mountain nodes,
the range peaks and the bagged root are all committed together with the
window update, so after a crash or restart there is no verdict without its
leaf, no leaf pointing at a missing verdict and never two leaves for one
receipt.
"""

import hashlib
import json
import sqlite3
import threading
import uuid

from . import window as win
from . import merkle

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
    leaf_seq     INTEGER UNIQUE REFERENCES audit_leaves(seq),
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_leaves (
    seq          INTEGER PRIMARY KEY,
    link_id      TEXT NOT NULL REFERENCES links(id),
    receipt_id   TEXT NOT NULL UNIQUE,
    counter      INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    status       TEXT NOT NULL,
    highest      INTEGER NOT NULL,
    leaf_hash    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mmr_nodes (
    node_id INTEGER PRIMARY KEY,
    height  INTEGER NOT NULL,
    hash    TEXT NOT NULL,
    left_id INTEGER REFERENCES mmr_nodes(node_id),
    right_id INTEGER REFERENCES mmr_nodes(node_id)
);
CREATE TABLE IF NOT EXISTS mmr_peaks (
    node_id INTEGER PRIMARY KEY REFERENCES mmr_nodes(node_id),
    height  INTEGER NOT NULL
);
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


def _decode_bitmap(value):
    """Bitmap persisted as an 8-byte little-endian BLOB.

    Older rows stored it as an INTEGER; both forms are accepted.  BLOB is
    needed because a completely full 64-bit window is 2**64-1, which does not
    fit SQLite's signed-64-bit INTEGER range.
    """
    if value is None:
        return 0
    if isinstance(value, (bytes, bytearray, memoryview)):
        return int.from_bytes(bytes(value), "little")
    return int(value)


def _encode_bitmap(bitmap):
    return (bitmap & ((1 << 64) - 1)).to_bytes(8, "little")


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
        self._migrate()
        self._verify_mmr_integrity()
        self._lock = threading.Lock()

    def _migrate(self):
        """Bring a database created by an older build up to the audit schema.

        Pre-audit receipts are backfilled with leaves (in their original
        insertion order) so the invariant "every saved verdict owns exactly
        one leaf" holds immediately after upgrade.  The bound post-verdict
        highest for such historical rows is their stored resolved extended
        sequence (the best per-verdict position the old schema retained).
        """
        cols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(receipts)")}
        if "leaf_seq" in cols:
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # SQLite cannot ADD COLUMN with a UNIQUE constraint; add the
            # plain column and enforce uniqueness with an index instead.
            self.conn.execute(
                "ALTER TABLE receipts ADD COLUMN leaf_seq INTEGER"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_leaf_seq "
                "ON receipts(leaf_seq)"
            )
            rows = self.conn.execute(
                "SELECT link_id, receipt_id, counter, payload_hash, status, "
                "extended FROM receipts ORDER BY rowid"
            ).fetchall()
            for row in rows:
                seq = self._append_audit_leaf(
                    row["link_id"], row["receipt_id"], row["counter"],
                    row["payload_hash"], row["status"], row["extended"],
                )
                self.conn.execute(
                    "UPDATE receipts SET leaf_seq = ? WHERE receipt_id = ?",
                    (seq, row["receipt_id"]),
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def close(self):
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------------- integrity

    def _verify_mmr_integrity(self):
        """Validate the on-disk mountain once at startup (recovery check).

        Replays leaves and nodes from storage and fails fast if a committed
        verdict is missing its leaf, a leaf points at a missing verdict, a
        receipt owns two leaves, or the peaks/root do not match the leaves.
        """
        leaf_rows = self.conn.execute(
            "SELECT seq, link_id, receipt_id, counter, payload_hash, status, "
            "highest, leaf_hash FROM audit_leaves ORDER BY seq"
        ).fetchall()
        leaf_count = len(leaf_rows)

        meta = self.conn.execute(
            "SELECT leaf_count, root FROM audit_meta WHERE id = 1"
        ).fetchone()
        if leaf_count == 0 and meta is None:
            return

        if meta is None:
            raise RuntimeError("audit leaves exist but audit_meta is missing")
        if meta["leaf_count"] != leaf_count:
            raise RuntimeError(
                f"audit_meta leaf_count {meta['leaf_count']} != "
                f"{leaf_count} stored leaves"
            )

        # Every receipt points at exactly one existing leaf and vice versa.
        orphan_receipts = self.conn.execute(
            "SELECT r.receipt_id FROM receipts r "
            "LEFT JOIN audit_leaves a ON a.seq = r.leaf_seq "
            "WHERE r.leaf_seq IS NULL OR a.seq IS NULL"
        ).fetchall()
        if orphan_receipts:
            raise RuntimeError(
                "saved verdict without its audit leaf: "
                + ", ".join(r["receipt_id"] for r in orphan_receipts)
            )
        dangling = self.conn.execute(
            "SELECT a.seq FROM audit_leaves a "
            "LEFT JOIN receipts r ON r.leaf_seq = a.seq "
            "WHERE r.receipt_id IS NULL"
        ).fetchall()
        if dangling:
            raise RuntimeError(
                "audit leaf pointing at no verdict: "
                + ",".join(str(r["seq"]) for r in dangling)
            )
        doubles = self.conn.execute(
            "SELECT receipt_id, COUNT(*) AS n FROM audit_leaves "
            "GROUP BY receipt_id HAVING n > 1"
        ).fetchall()
        if doubles:
            raise RuntimeError("receipt with two audit leaves")

        # Rebuild the mountain from stored leaves and compare node hashes,
        # peak set and root with what was persisted.
        mmr = merkle.replay(r["leaf_hash"] for r in leaf_rows)
        if mmr.leaf_count != leaf_count:
            raise RuntimeError("rebuilt leaf count mismatch")
        stored_nodes = self.conn.execute(
            "SELECT node_id, height, hash FROM mmr_nodes ORDER BY node_id"
        ).fetchall()
        rebuilt = {n.node_id: (n.height, n.hash) for n in mmr.nodes}
        if len(stored_nodes) != len(rebuilt):
            raise RuntimeError("mmr_nodes count does not match leaves")
        for row in stored_nodes:
            want = rebuilt.get(row["node_id"])
            if want != (row["height"], row["hash"]):
                raise RuntimeError(
                    f"mmr_nodes row {row['node_id']} does not match replay"
                )
        stored_peaks = [
            (r["height"], self.conn.execute(
                "SELECT hash FROM mmr_nodes WHERE node_id = ?",
                (r["node_id"],)).fetchone()["hash"])
            for r in self.conn.execute(
                "SELECT node_id, height FROM mmr_peaks ORDER BY height DESC")
        ]
        if stored_peaks != mmr.peaks():
            raise RuntimeError("persisted MMR peaks do not match replay")
        if mmr.root() != meta["root"]:
            raise RuntimeError("persisted MMR root does not match replay")

        for row in leaf_rows:
            want = merkle.leaf_hash_hex(
                row["seq"], row["link_id"], row["receipt_id"],
                row["counter"], row["payload_hash"], row["status"],
                row["highest"],
            )
            if want != row["leaf_hash"]:
                raise RuntimeError(
                    f"audit leaf {row['seq']} content/hash mismatch"
                )

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
        bitmap = _decode_bitmap(row["bitmap"])
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
        link/counter/payload.  Window state, receipt record and the audit leaf
        (with its new MMR nodes, peaks and root) are committed together.
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
                current_bitmap = _decode_bitmap(row["bitmap"])

                prior = self.conn.execute(
                    "SELECT link_id, counter, payload_hash, status, extended, "
                    "leaf_seq FROM receipts WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone()

                retransmit = prior is not None
                if retransmit:
                    # Stable receipt ids are global: an identical retransmission
                    # (same link, counter and payload) replays the first verdict;
                    # reusing the id with the link, counter or payload changed is
                    # a conflict.  In either case no new leaf is appended.
                    if (prior["link_id"] != link_id
                            or prior["counter"] != counter
                            or prior["payload_hash"] != payload_hash):
                        raise ReceiptConflict(receipt_id)
                    status = prior["status"]
                    extended = prior["extended"]
                    leaf_seq = prior["leaf_seq"]
                    highest = row["highest"]
                    bitmap = current_bitmap
                else:
                    status, new_highest, new_bitmap = win.decide(
                        counter, row["highest"], current_bitmap
                    )
                    extended, _ = win.extend_counter(counter, row["highest"])
                    self.conn.execute(
                        "UPDATE link_windows SET highest = ?, bitmap = ? WHERE link_id = ?",
                        (new_highest, _encode_bitmap(new_bitmap), link_id),
                    )
                    leaf_seq = self._append_audit_leaf(
                        link_id, receipt_id, counter, payload_hash,
                        status, new_highest,
                    )
                    self.conn.execute(
                        "INSERT INTO receipts (link_id, receipt_id, counter, "
                        "payload_hash, status, extended, leaf_seq, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))",
                        (link_id, receipt_id, counter, payload_hash,
                         status, extended, leaf_seq),
                    )
                    highest, bitmap = new_highest, new_bitmap

                meta = self.conn.execute(
                    "SELECT leaf_count, root FROM audit_meta WHERE id = 1"
                ).fetchone()
                leaf_count = meta["leaf_count"] if meta else 0
                root = meta["root"] if meta else merkle.EMPTY_ROOT_HEX

                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

        result = {
            "status": status,
            "retransmit": retransmit,
            "counter": counter,
            "extended": extended,
            "highest": highest,
            "recent": win.recent_positions(highest, bitmap),
            "leaf_seq": leaf_seq,
            "root": root,
            "leaf_count": leaf_count,
        }
        return result

    # ------------------------------------------------------------ audit MMR

    def _append_audit_leaf(self, link_id, receipt_id, counter,
                           payload_hash, status, highest):
        """Append one leaf + merged nodes inside the open transaction.

        Returns the new 1-based global leaf sequence.  Peak rows and the root
        in audit_meta are rewritten in the same transaction, so commit order
        fixes leaf sequence, predecessor peaks and root atomically.
        """
        meta = self.conn.execute(
            "SELECT leaf_count FROM audit_meta WHERE id = 1"
        ).fetchone()
        seq = (meta["leaf_count"] if meta else 0) + 1
        digest = merkle.leaf_hash_hex(
            seq, link_id, receipt_id, counter,
            payload_hash, status, highest,
        )

        max_id_row = self.conn.execute(
            "SELECT COALESCE(MAX(node_id), 0) AS m FROM mmr_nodes"
        ).fetchone()
        next_id = max_id_row["m"] + 1

        self.conn.execute(
            "INSERT INTO audit_leaves (seq, link_id, receipt_id, counter, "
            "payload_hash, status, highest, leaf_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (seq, link_id, receipt_id, counter, payload_hash,
             status, highest, digest),
        )
        self.conn.execute(
            "INSERT INTO mmr_nodes (node_id, height, hash, left_id, right_id) "
            "VALUES (?, 0, ?, NULL, NULL)",
            (next_id, digest),
        )

        # Current peaks, highest first, so the lowest peak is last and pop()
        # removes it (matching the in-memory MMR merge order).
        ordered = [dict(r) for r in self.conn.execute(
            "SELECT n.node_id AS node_id, n.height AS height, n.hash AS hash "
            "FROM mmr_peaks p JOIN mmr_nodes n ON n.node_id = p.node_id "
            "ORDER BY p.height DESC"
        )]

        node_id = next_id
        node_hash = digest
        height = 0
        while ordered and ordered[-1]["height"] == height:
            sibling = ordered.pop()
            parent_id = next_id + 1
            next_id = parent_id
            parent_hash = merkle.node_hash_hex(sibling["hash"], node_hash)
            self.conn.execute(
                "INSERT INTO mmr_nodes (node_id, height, hash, left_id, right_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (parent_id, height + 1, parent_hash,
                 sibling["node_id"], node_id),
            )
            node_id, node_hash, height = (
                parent_id, parent_hash, height + 1
            )
        ordered.append({"node_id": node_id, "height": height, "hash": node_hash})

        self.conn.execute("DELETE FROM mmr_peaks")
        self.conn.executemany(
            "INSERT INTO mmr_peaks (node_id, height) VALUES (?, ?)",
            [(p["node_id"], p["height"]) for p in ordered]
        )

        new_peaks = [
            (p["height"], p["hash"]) for p in ordered
        ]
        root = merkle.root_from_peaks(new_peaks)
        self.conn.execute(
            "INSERT INTO audit_meta (id, leaf_count, root) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET leaf_count = excluded.leaf_count, "
            "root = excluded.root",
            (seq, root),
        )
        return seq

    def _peaks(self):
        return [(r["height"], r["hash"]) for r in self.conn.execute(
            "SELECT p.height AS height, n.hash AS hash "
            "FROM mmr_peaks p JOIN mmr_nodes n ON n.node_id = p.node_id "
            "ORDER BY p.height DESC"
        )]

    def _root(self):
        meta = self.conn.execute(
            "SELECT root FROM audit_meta WHERE id = 1"
        ).fetchone()
        return meta["root"] if meta else merkle.EMPTY_ROOT_HEX

    def get_receipt_audit(self, receipt_id):
        """Return leaf content, global seq, current root and inclusion path.

        The proof is always computed against the newest root, so a leaf
        fetched early stays verifiable after later verdicts join the range.
        Returns ``None`` for an unknown receipt id.
        """
        leaf = self.conn.execute(
            "SELECT seq, link_id, receipt_id, counter, payload_hash, status, "
            "highest, leaf_hash FROM audit_leaves WHERE receipt_id = ?",
            (receipt_id,),
        ).fetchone()
        if leaf is None:
            return None

        # All reads below share one read transaction: in WAL that pins a
        # consistent snapshot, so a concurrent append cannot leave the proof
        # half on old peaks and half on new ones.
        self.conn.execute("BEGIN")
        try:
            audit = self._read_receipt_audit(leaf)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return audit

    def _read_receipt_audit(self, leaf):
        # Walk parent pointers from the leaf node up to the node that is
        # currently a peak, collecting sibling nodes.  Proof shape is a pure
        # function of the leaf's seq and the total leaf count.
        leaf_node = self.conn.execute(
            "SELECT node_id FROM mmr_nodes WHERE height = 0 "
            "ORDER BY node_id LIMIT 1 OFFSET ?",
            (leaf["seq"] - 1,),
        ).fetchone()
        node_id = leaf_node["node_id"]
        steps = []
        while True:
            is_peak = self.conn.execute(
                "SELECT 1 FROM mmr_peaks WHERE node_id = ?", (node_id,)
            ).fetchone()
            if is_peak:
                break
            parent = self.conn.execute(
                "SELECT node_id, height, left_id, right_id FROM mmr_nodes "
                "WHERE left_id = ? OR right_id = ?",
                (node_id, node_id),
            ).fetchone()
            if parent is None:
                raise RuntimeError(
                    f"audit node {node_id} is neither a peak nor connected"
                )
            if parent["left_id"] == node_id:
                sib = self.conn.execute(
                    "SELECT height, hash FROM mmr_nodes WHERE node_id = ?",
                    (parent["right_id"],),
                ).fetchone()
                steps.append(["right", sib["height"], sib["hash"]])
            else:
                sib = self.conn.execute(
                    "SELECT height, hash FROM mmr_nodes WHERE node_id = ?",
                    (parent["left_id"],),
                ).fetchone()
                steps.append(["left", sib["height"], sib["hash"]])
            node_id = parent["node_id"]

        peaks = self._peaks()
        root = self._root()
        leaf_hash = leaf["leaf_hash"]
        if not merkle.verify_inclusion(leaf_hash, steps, peaks, root):
            raise RuntimeError(
                f"inclusion proof for receipt {receipt_id} failed self-check"
            )

        meta = self.conn.execute(
            "SELECT leaf_count FROM audit_meta WHERE id = 1"
        ).fetchone()
        return {
            "seq": leaf["seq"],
            "leaf_count": meta["leaf_count"],
            "receipt_id": leaf["receipt_id"],
            "link_id": leaf["link_id"],
            "counter": leaf["counter"],
            "payload_hash": leaf["payload_hash"],
            "status": leaf["status"],
            "highest": leaf["highest"],
            "leaf_hash": leaf_hash,
            "root": root,
            "steps": steps,
            "peaks": [{"height": h, "hash": d} for h, d in peaks],
        }

    def audit_tip(self):
        """Current global leaf count and root."""
        meta = self.conn.execute(
            "SELECT leaf_count, root FROM audit_meta WHERE id = 1"
        ).fetchone()
        return {
            "leaf_count": meta["leaf_count"] if meta else 0,
            "root": meta["root"] if meta else merkle.EMPTY_ROOT_HEX,
        }
