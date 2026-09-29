import os
import tempfile
import threading
import unittest
from unittest import mock

from app import audit
from app.db import Database, ReceiptConflict, canonical_hash


class AuditDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "audit.db"))
        self.link = self.db.create_link("L")["id"]

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def submit(self, counter, rid, payload=None, link=None):
        return self.db.submit_frame(
            link or self.link, counter, rid,
            payload if payload is not None else {"v": 1})

    def verify(self, rid, db=None):
        db = db or self.db
        rec = db.get_receipt_audit(rid)
        fields = {k: rec["leaf"][k] for k in
                  ("seq", "link", "receipt", "counter", "payload_hash",
                   "status", "highest")}
        return audit.verify_inclusion(
            fields, rec["path"], rec["peaks"], rec["root"],
            rec["leaf_count"]), rec

    # --------------------------------------------------------- leaf contents

    def test_first_verdict_appends_one_leaf_for_every_status(self):
        half = 1 << 31
        cases = (
            ("accepted", 1, "a"),       # first frame
            ("duplicate", 1, "b"),      # same counter, fresh receipt id
        )
        for status, counter, rid in cases:
            v = self.submit(counter, rid)
            self.assertEqual(v["status"], status, rid)
            self.assertFalse(v["retransmit"])

        # jump the window forward so old counters expire
        v = self.submit(100, "j")
        self.assertEqual(v["status"], "accepted")
        v = self.submit(0, "c")
        self.assertEqual(v["status"], "expired")
        # a counter exactly 2^31 away is an epoch tie -> rejected
        v = self.submit((100 + half) & ((1 << 32) - 1), "d")
        self.assertEqual(v["status"], "rejected")

        state = self.db.audit_state()
        self.assertEqual(state["leaf_count"], 5)
        for rid, status in (("a", "accepted"), ("b", "duplicate"),
                            ("j", "accepted"), ("c", "expired"),
                            ("d", "rejected")):
            ok, rec = self.verify(rid)
            self.assertTrue(ok, rid)
            self.assertEqual(rec["leaf"]["receipt"], rid)
            self.assertEqual(rec["leaf"]["link"], self.link)
            self.assertEqual(rec["leaf"]["status"], status)

    def test_leaf_binds_post_verdict_highest_and_payload(self):
        self.submit(10, "r1", {"sensor": "temp", "value": 2.7})
        ok, rec = self.verify("r1")
        self.assertTrue(ok)
        leaf = rec["leaf"]
        self.assertEqual(leaf["highest"], 10)
        self.assertEqual(leaf["counter"], 10)
        self.assertEqual(leaf["payload_hash"],
                         canonical_hash({"sensor": "temp", "value": 2.7}))
        # status after the fact is immutable: a duplicate of the counter gets
        # its own new leaf with its own status, the first leaf stays accepted
        self.submit(10, "r2")
        ok, rec = self.verify("r1")
        self.assertTrue(ok)
        self.assertEqual(rec["leaf"]["status"], "accepted")
        ok, rec = self.verify("r2")
        self.assertEqual(rec["leaf"]["status"], "duplicate")

    def test_retransmit_returns_original_leaf_and_appends_nothing(self):
        v1 = self.submit(7, "stable")
        self.assertEqual(v1["audit"]["seq"], 1)
        v2 = self.submit(7, "stable")
        v3 = self.submit(7, "stable")
        self.assertTrue(v2["retransmit"] and v3["retransmit"])
        self.assertEqual(v2["audit"]["seq"], 1)
        self.assertEqual(v2["audit"]["root"], v1["audit"]["root"])
        self.assertEqual(self.db.audit_state()["leaf_count"], 1)
        ok, rec = self.verify("stable")
        self.assertTrue(ok)

    def test_conflict_appends_no_leaf(self):
        self.submit(1, "stable", {"v": 1})
        with self.assertRaises(ReceiptConflict):
            self.submit(2, "stable", {"v": 1})
        with self.assertRaises(ReceiptConflict):
            self.submit(1, "stable", {"v": 2})
        self.assertEqual(self.db.audit_state()["leaf_count"], 1)
        # the stored leaf is still the original verdict
        ok, rec = self.verify("stable")
        self.assertTrue(ok)
        self.assertEqual(rec["leaf"]["counter"], 1)
        self.assertIsNone(self.db.get_receipt_audit("never-used"))

    def test_global_log_spans_links(self):
        other = self.db.create_link("other")["id"]
        v = self.submit(1, "l1-a")
        v2 = self.db.submit_frame(other, 1, "l2-a", {"v": 1})
        self.assertEqual((v["audit"]["seq"], v2["audit"]["seq"]), (1, 2))
        self.assertTrue(self.verify("l1-a")[0])
        self.assertTrue(self.verify("l2-a")[0])
        rec = self.db.get_receipt_audit("l2-a")
        self.assertEqual(rec["leaf"]["link"], other)

    # --------------------------------------------------------- later roots

    def test_leaf_still_verifies_after_later_verdicts(self):
        self.submit(1, "anchor")
        early_root = self.db.audit_state()["root"]
        for i in range(2, 40):
            self.submit(i, f"r{i}")
        # the anchor's record is now served against the latest root
        ok, rec = self.verify("anchor")
        self.assertTrue(ok)
        self.assertEqual(rec["seq"], 1)
        self.assertEqual(rec["leaf_count"], 39)
        self.assertNotEqual(rec["root"], early_root)
        # every leaf proves under the current root
        for rid in ("anchor", *[f"r{i}" for i in range(2, 40)]):
            self.assertTrue(self.verify(rid)[0], rid)

    def test_response_root_matches_audit_endpoint(self):
        v = self.submit(3, "x")
        self.assertEqual(v["audit"]["root"], self.db.audit_state()["root"])

    # ------------------------------------------------------------ restart

    def test_audit_state_survives_restart(self):
        for i in range(1, 18):
            self.submit(i, f"r{i}")
        expected = self.db.audit_state()
        path = self.db.path
        self.db.close()

        db2 = Database(path)
        try:
            self.assertEqual(db2.audit_state(), expected)
            for rid in ("r1", "r7", "r13", "r17"):
                ok, _ = self.verify(rid, db=db2)
                self.assertTrue(ok, rid)
            # appends continue contiguously after recovery
            v = db2.submit_frame(self.link, 100, "after", {"v": 1})
            self.assertEqual(v["audit"]["seq"], 18)
            self.assertTrue(self.verify("after", db=db2)[0])
        finally:
            db2.close()

    def test_recovery_rebuilds_stale_derived_state(self):
        for i in range(1, 9):
            self.submit(i, f"r{i}")
        good = self.db.audit_state()
        path = self.db.path
        self.db.close()
        import sqlite3
        raw = sqlite3.connect(path)
        raw.execute("UPDATE audit_meta SET leaf_count = 0, root = ? WHERE id=1",
                    ("stale",))
        raw.execute("DELETE FROM audit_nodes")
        raw.commit()
        raw.close()

        db2 = Database(path)
        try:
            self.assertEqual(db2.audit_state(), good)
            self.assertTrue(self.verify("r5", db=db2)[0])
        finally:
            db2.close()

    def test_recovery_refuses_verdicts_without_leaf(self):
        self.submit(1, "r1")
        self.submit(2, "r2")
        path = self.db.path
        self.db.close()
        import sqlite3
        raw = sqlite3.connect(path)
        raw.execute("DELETE FROM audit_leaves WHERE receipt_id='r2'")
        raw.commit()
        raw.close()
        with self.assertRaises(RuntimeError):
            Database(path)

    # ------------------------------------------------------ transactionality

    def test_leaf_append_failure_rolls_back_everything(self):
        # Force a failure inside the submit transaction, *after* the window
        # update and receipt insert would have run: nothing may persist.
        with mock.patch("app.db.audit.leaf_digest",
                        side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.submit(42, "will-rollback")
        link = self.db.get_link(self.link)
        self.assertIsNone(link["highest"])
        self.assertEqual(self.db.audit_state()["leaf_count"], 0)
        self.assertIsNone(self.db.get_receipt_audit("will-rollback"))
        # a subsequent submit works and starts at seq 1
        v = self.submit(42, "after-failure")
        self.assertEqual(v["audit"]["seq"], 1)
        self.assertTrue(self.verify("after-failure")[0])

    def test_database_invariants_via_sql(self):
        for i in range(1, 25):
            self.submit(i, f"r{i}")
        conn = self.db.conn
        # one leaf per verdict, contiguous seq, exactly one per receipt
        n_verdicts = conn.execute(
            "SELECT COUNT(*) AS n FROM receipts").fetchone()["n"]
        n_leaves = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_leaves").fetchone()["n"]
        self.assertEqual(n_verdicts, n_leaves)
        seqs = [r["seq"] for r in conn.execute(
            "SELECT seq FROM audit_leaves ORDER BY seq")]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        dupes = conn.execute(
            "SELECT COUNT(*) AS n FROM "
            "(SELECT receipt_id, COUNT(*) c FROM audit_leaves "
            "GROUP BY receipt_id HAVING c > 1)").fetchone()["n"]
        self.assertEqual(dupes, 0)

    # ---------------------------------------------------------- concurrency

    def test_concurrent_commits_have_one_total_order(self):
        errors = []

        def worker(i):
            try:
                self.submit(1000 + i, f"c-{i:03d}")
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(60)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

        state = self.db.audit_state()
        self.assertEqual(state["leaf_count"], 60)
        # seqs are a permutation of 1..60: exactly one commit order
        rows = self.db.conn.execute(
            "SELECT seq, receipt_id, highest FROM audit_leaves ORDER BY seq"
        ).fetchall()
        self.assertEqual([r["seq"] for r in rows], list(range(1, 61)))
        self.assertEqual(sorted(r["receipt_id"] for r in rows),
                         sorted(f"c-{i:03d}" for i in range(60)))
        # the stored highest on each leaf is the link highest after that
        # verdict, non-decreasing along the unique commit order
        highests = [r["highest"] for r in rows]
        self.assertEqual(highests, sorted(highests))
        self.assertEqual(highests[-1], 1059)
        # root independently recomputed from leaves equals persisted root
        leaves = [r["leaf_hash"] for r in self.db.conn.execute(
            "SELECT leaf_hash FROM audit_leaves ORDER BY seq")]
        self.assertEqual(audit.build_mmr(leaves).root(), state["root"])
        # every leaf proves against the committed root
        for i in range(60):
            self.assertTrue(self.verify(f"c-{i:03d}")[0])

    def test_concurrent_same_receipt_serialises_to_one_leaf(self):
        outcomes = []

        def worker():
            outcomes.append(self.submit(5, "hot-id")["audit"]["seq"])

        threads = [threading.Thread(target=worker) for _ in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # every call returns the SAME leaf seq; only one leaf exists
        self.assertEqual(set(outcomes), {1})
        self.assertEqual(self.db.audit_state()["leaf_count"], 1)

    # --------------------------------------------- unsigned bitmap regression

    def test_full_64_bit_window_bitmap_persists(self):
        # 64 in-order accepted frames set every bit (2**64-1), which does not
        # fit in a signed 64-bit SQLite INTEGER; it is stored as text.
        for i in range(64):
            self.submit(i, f"b{i}")
        link = self.db.get_link(self.link)
        self.assertEqual(link["highest"], 63)
        self.assertEqual(link["bitmap"], (1 << 64) - 1)
        # one more frame slides the window without an overflow error
        v = self.submit(64, "b64")
        self.assertEqual(v["status"], "accepted")
        self.assertEqual(v["highest"], 64)
        kind = self.db.conn.execute(
            "SELECT LOWER(type) FROM pragma_table_info('link_windows') "
            "WHERE name = 'bitmap'"
        ).fetchone()[0]
        self.assertEqual(kind, "text")

    def test_legacy_integer_bitmap_column_migrates(self):
        import sqlite3
        self.db.close()
        path = self.db.path
        raw = sqlite3.connect(path)
        raw.executescript(
            """
            ALTER TABLE link_windows RENAME TO lw_old;
            CREATE TABLE link_windows (link_id TEXT PRIMARY KEY,
                highest INTEGER, bitmap INTEGER NOT NULL DEFAULT 0);
            INSERT INTO link_windows SELECT link_id, highest,
                   CAST(bitmap AS INTEGER) FROM lw_old;
            DROP TABLE lw_old;
            """)
        raw.commit()
        raw.close()

        db2 = Database(path)
        try:
            self.assertEqual(db2.get_link(self.link)["bitmap"], 0)
            v = db2.submit_frame(self.link, 1, "post-migrate", {"v": 1})
            self.assertEqual(v["status"], "accepted")
            self.assertEqual(db2.get_link(self.link)["bitmap"], 1)
        finally:
            db2.close()
        # re-open the Database for tearDown's close
        self.db = Database(path)


class TestLegacyBackfill(unittest.TestCase):
    def test_pre_audit_database_gets_canonical_leaves(self):
        import sqlite3
        from app import window as winmod

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "legacy.db")
        raw = sqlite3.connect(path)
        raw.executescript(
            """
            CREATE TABLE links (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                                created_at REAL NOT NULL);
            CREATE TABLE link_windows (link_id TEXT PRIMARY KEY
                REFERENCES links(id), highest INTEGER,
                bitmap INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE receipts (receipt_id TEXT PRIMARY KEY,
                link_id TEXT NOT NULL REFERENCES links(id),
                counter INTEGER NOT NULL, payload_hash TEXT NOT NULL,
                status TEXT NOT NULL, extended INTEGER NOT NULL,
                created_at REAL NOT NULL);
            """)
        raw.execute("INSERT INTO links VALUES ('L','n',0)")
        raw.execute("INSERT INTO link_windows VALUES ('L', NULL, 0)")
        h, b = None, 0
        for counter, rid in ((5, "a"), (6, "b"), (5, "dup"), (300, "j")):
            status, h, b = winmod.decide(counter, h, b)
            raw.execute(
                "INSERT INTO receipts VALUES (?,?,?,?,?,?,0)",
                (rid, "L", counter, f"ph-{rid}", status, counter))
        raw.execute("UPDATE link_windows SET highest=?, bitmap=? WHERE link_id='L'",
                    (h, b))
        raw.commit()
        raw.close()

        db = Database(path)
        try:
            self.assertEqual(db.audit_state()["leaf_count"], 4)
            for rid, status in (("a", "accepted"), ("b", "accepted"),
                                ("dup", "duplicate"), ("j", "accepted")):
                rec = db.get_receipt_audit(rid)
                self.assertEqual(rec["leaf"]["status"], status)
                fields = {k: rec["leaf"][k] for k in
                          ("seq", "link", "receipt", "counter",
                           "payload_hash", "status", "highest")}
                self.assertTrue(audit.verify_inclusion(
                    fields, rec["path"], rec["peaks"], rec["root"],
                    rec["leaf_count"]))
            # backfilled log continues to accept append-only verdicts
            v = db.submit_frame("L", 301, "after", {"v": 1})
            self.assertEqual(v["audit"]["seq"], 5)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
