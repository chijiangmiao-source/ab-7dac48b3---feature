import os
import tempfile
import unittest

from app.db import Database, ReceiptConflict
from app import window as win
from app import merkle

MOD = 1 << 32
HALF = 1 << 31


class TempDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "test.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def make_link(self, name="L"):
        return self.db.create_link(name)["id"]

    def submit(self, link, counter, receipt, payload=None):
        return self.db.submit_frame(link, counter, receipt,
                                    payload if payload is not None else {"v": 1})


class TestPersistence(TempDB):
    def test_create_and_get_link(self):
        lid = self.make_link("mars")
        link = self.db.get_link(lid)
        self.assertEqual(link["name"], "mars")
        self.assertIsNone(link["highest"])
        self.assertEqual(link["recent"], [])

    def test_submit_basic_accepted_updates_window(self):
        lid = self.make_link()
        v = self.submit(lid, 5, "r1")
        self.assertEqual(v["status"], "accepted")
        self.assertEqual(v["highest"], 5)
        link = self.db.get_link(lid)
        self.assertEqual(link["highest"], 5)
        self.assertEqual(link["bitmap"], 1)

    def test_unknown_link(self):
        with self.assertRaises(LookupError):
            self.db.submit_frame("nope", 1, "r", {})

    # ------------------------------------------------------------ receipts

    def test_identical_retransmit_replays_first_verdict(self):
        lid = self.make_link()
        first = self.submit(lid, 9, "rx-1")
        self.assertEqual(first["status"], "accepted")
        second = self.submit(lid, 9, "rx-1")
        self.assertTrue(second["retransmit"])
        self.assertEqual(second["status"], "accepted")
        self.assertEqual(second["extended"], first["extended"])

    def test_duplicate_verdict_is_replayed(self):
        lid = self.make_link()
        self.submit(lid, 10, "a")
        self.submit(lid, 11, "b")
        dup = self.submit(lid, 10, "c")
        self.assertEqual(dup["status"], "duplicate")
        replay = self.submit(lid, 10, "c")
        self.assertEqual(replay["status"], "duplicate")
        self.assertTrue(replay["retransmit"])

    def test_expired_verdict_is_replayed(self):
        lid = self.make_link()
        self.submit(lid, 100, "a")
        old = self.submit(lid, 0, "old")
        self.assertEqual(old["status"], "expired")
        replay = self.submit(lid, 0, "old")
        self.assertEqual(replay["status"], "expired")

    def test_receipt_reuse_changed_counter_rejected(self):
        lid = self.make_link()
        self.submit(lid, 1, "stable")
        with self.assertRaises(ReceiptConflict):
            self.submit(lid, 2, "stable")

    def test_receipt_reuse_changed_payload_rejected(self):
        lid = self.make_link()
        self.submit(lid, 1, "stable", {"v": 1})
        with self.assertRaises(ReceiptConflict):
            self.submit(lid, 1, "stable", {"v": 2})

    def test_receipt_reuse_on_other_link_rejected(self):
        l1 = self.make_link("one")
        l2 = self.make_link("two")
        self.submit(l1, 1, "global-id")
        with self.assertRaises(ReceiptConflict):
            self.submit(l2, 1, "global-id")

    def test_conflict_does_not_mutate_window(self):
        lid = self.make_link()
        self.submit(lid, 10, "s1")
        with self.assertRaises(ReceiptConflict):
            self.submit(lid, 11, "s1")
        link = self.db.get_link(lid)
        self.assertEqual(link["highest"], 10)

    # --------------------------------------------------------------- restart

    def test_state_survives_reopen(self):
        lid = self.make_link()
        for c, r in zip((MOD - 1, 0, 1), ("a", "b", "c")):
            self.submit(lid, c, r)
        self.db.close()

        db2 = Database(self.db.path)
        try:
            link = db2.get_link(lid)
            self.assertEqual(link["highest"], MOD + 1)
            # identical retransmission replays the FIRST verdict ("accepted")
            v = db2.submit_frame(lid, 0, "b", {"v": 1})
            self.assertEqual(v["status"], "accepted")
            self.assertTrue(v["retransmit"])
            # a genuinely new receipt for an already-set bit -> duplicate,
            # derived from the window state restored after restart
            v = db2.submit_frame(lid, 0, "b2", {"v": 1})
            self.assertEqual(v["status"], "duplicate")
            self.assertFalse(v["retransmit"])
            # a new frame keeps sliding forward after restart
            v = db2.submit_frame(lid, 2, "d", {"v": 1})
            self.assertEqual(v["status"], "accepted")
        finally:
            db2.close()

    # ------------------------------------------------------------- concurrency

    def test_concurrent_unique_counters_each_accepted_once(self):
        import threading

        lid = self.make_link()
        results = {}

        def worker(counter):
            rid = f"c-{counter}"
            results[counter] = self.db.submit_frame(lid, counter, rid, {"v": 1})

        threads = [threading.Thread(target=worker, args=(c,))
                   for c in range(1, 40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 39)
        self.assertTrue(all(v["status"] == "accepted" for v in results.values()))
        link = self.db.get_link(lid)
        self.assertEqual(link["highest"], 39)
        # exactly one row per receipt, no double application
        rows = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM receipts"
        ).fetchone()
        self.assertEqual(rows["n"], 39)

    def test_concurrent_duplicates_of_same_counter(self):
        import threading

        lid = self.make_link()
        outcomes = []

        def worker(rid):
            outcomes.append(self.db.submit_frame(lid, 5, rid, {"v": 1})["status"])

        threads = [threading.Thread(target=worker, args=(f"dup-{i}",))
                   for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(outcomes.count("accepted"), 1)
        self.assertEqual(outcomes.count("duplicate"), 19)

    # ----------------------------------------------------------- audit leaves

    def test_first_verdict_appends_leaf_with_global_seq_and_root(self):
        from app.db import canonical_hash
        lid = self.make_link()
        v = self.submit(lid, 5, "r1")
        self.assertEqual(v["leaf_seq"], 1)
        self.assertEqual(v["leaf_count"], 1)
        audit = self.db.get_receipt_audit("r1")
        self.assertEqual(audit["seq"], 1)
        self.assertEqual(audit["link_id"], lid)
        self.assertEqual(audit["counter"], 5)
        self.assertEqual(audit["status"], "accepted")
        self.assertEqual(audit["highest"], 5)
        self.assertEqual(audit["payload_hash"], canonical_hash({"v": 1}))
        self.assertEqual(v["root"], audit["root"])

    def test_leaf_payload_hash_matches_payload(self):
        lid = self.make_link()
        self.submit(lid, 7, "r7", {"sensor": "temp", "value": 2.7})
        audit = self.db.get_receipt_audit("r7")
        from app.db import canonical_hash
        self.assertEqual(audit["payload_hash"], canonical_hash({"sensor": "temp", "value": 2.7}))

    def test_leaf_binds_post_verdict_highest_for_all_statuses(self):
        lid = self.make_link()
        self.submit(lid, 100, "a")
        old = self.submit(lid, 0, "old")            # expired, highest 100
        tie = self.submit(lid, 100 + HALF, "tie")   # rejected, highest 100
        self.assertEqual(old["status"], "expired")
        self.assertEqual(tie["status"], "rejected")
        a_old = self.db.get_receipt_audit("old")
        a_tie = self.db.get_receipt_audit("tie")
        self.assertEqual(a_old["highest"], 100)
        self.assertEqual(a_tie["highest"], 100)

        # duplicate verdict gets its own first-time leaf
        dup = self.submit(lid, 100, "d")
        self.assertEqual(dup["status"], "duplicate")
        a_dup = self.db.get_receipt_audit("d")
        self.assertEqual(a_dup["status"], "duplicate")
        self.assertEqual(a_dup["highest"], 100)

    def test_retransmit_returns_same_leaf(self):
        lid = self.make_link()
        first = self.submit(lid, 9, "rx-1")
        again = self.submit(lid, 9, "rx-1")
        self.assertEqual(again["leaf_seq"], first["leaf_seq"])
        self.assertEqual(again["leaf_count"], first["leaf_count"])
        self.assertEqual(again["root"], first["root"])
        rows = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM audit_leaves WHERE receipt_id = 'rx-1'"
        ).fetchone()
        self.assertEqual(rows["n"], 1)

    def test_conflict_appends_no_leaf(self):
        lid = self.make_link()
        self.submit(lid, 1, "stable")
        before = self.db.audit_tip()
        with self.assertRaises(ReceiptConflict):
            self.submit(lid, 2, "stable")
        after = self.db.audit_tip()
        self.assertEqual(after, before)

    def test_global_seqs_are_dense_across_links_and_statuses(self):
        l1, l2 = self.make_link("one"), self.make_link("two")
        seqs = []
        seqs.append(self.submit(l1, 1, "a")["leaf_seq"])
        seqs.append(self.submit(l2, 1, "b")["leaf_seq"])
        seqs.append(self.submit(l1, 1, "a-dup")["leaf_seq"])   # duplicate
        seqs.append(self.submit(l2, 999, "c")["leaf_seq"])
        seqs.append(self.submit(l2, 0, "d-old")["leaf_seq"])   # expired
        self.assertEqual(seqs, [1, 2, 3, 4, 5])
        tip = self.db.audit_tip()
        self.assertEqual(tip["leaf_count"], 5)

    def test_inclusion_proof_verifies_and_stays_valid_after_growth(self):
        lid = self.make_link()
        v = self.submit(lid, 3, "first")
        early = self.db.get_receipt_audit("first")
        self.assertTrue(merkle.verify_inclusion(
            early["leaf_hash"], early["steps"],
            [(p["height"], p["hash"]) for p in early["peaks"]],
            early["root"]))
        self.assertEqual(early["root"], v["root"])
        for i in range(4, 25):
            self.submit(lid, i, f"r{i}")
        now = self.db.get_receipt_audit("first")
        # same immutable leaf content, but the path and root are re-derived
        # against the newest tip and still verify
        self.assertEqual(now["seq"], 1)
        self.assertEqual(now["leaf_hash"], early["leaf_hash"])
        self.assertNotEqual(now["root"], early["root"])
        self.assertEqual(now["root"], self.db.audit_tip()["root"])
        self.assertTrue(merkle.verify_inclusion(
            now["leaf_hash"], now["steps"],
            [(p["height"], p["hash"]) for p in now["peaks"]],
            now["root"]))

    def test_stored_mmr_matches_pure_replay(self):
        lid = self.make_link()
        for i in range(1, 14):
            self.submit(lid, i, f"q{i}")
        rows = self.db.conn.execute(
            "SELECT leaf_hash FROM audit_leaves ORDER BY seq"
        ).fetchall()
        mmr = merkle.replay([r["leaf_hash"] for r in rows])
        self.assertEqual(self.db.audit_tip()["root"], mmr.root())
        peaks = self.db.conn.execute(
            "SELECT p.height, n.hash FROM mmr_peaks p "
            "JOIN mmr_nodes n ON n.node_id = p.node_id ORDER BY p.height DESC"
        ).fetchall()
        self.assertEqual(
            [(r["height"], r["hash"]) for r in peaks], mmr.peaks())

    # --------------------------------------------------------------- restart

    def test_audit_survives_restart_and_self_checks(self):
        lid = self.make_link()
        for i in range(1, 12):
            self.submit(lid, i, f"k{i}")
        before = self.db.get_receipt_audit("k3")
        root_before = self.db.audit_tip()
        self.db.close()

        db2 = Database(self.db.path)
        try:
            self.assertEqual(db2.audit_tip(), root_before)
            after = db2.get_receipt_audit("k3")
            self.assertEqual(after["leaf_hash"], before["leaf_hash"])
            self.assertEqual(after["seq"], before["seq"])
            self.assertTrue(merkle.verify_inclusion(
                after["leaf_hash"], after["steps"],
                [(p["height"], p["hash"]) for p in after["peaks"]],
                after["root"]))
            # append continues the same global sequence after restart
            v = db2.submit_frame(lid, 12, "k12", {"v": 1})
            self.assertEqual(v["leaf_seq"], 12)
        finally:
            db2.close()

    # ------------------------------------------------------------- concurrency

    def test_concurrent_first_verdicts_get_unique_dense_seqs_and_one_commit_order(self):
        import threading

        l1, l2 = self.make_link("c1"), self.make_link("c2")
        verdict_lock = threading.Lock()
        results = []

        def worker(link, counter):
            rid = f"cc-{link[:4]}-{counter}"
            v = self.db.submit_frame(link, counter, rid, {"v": counter})
            with verdict_lock:
                results.append(v)

        threads = []
        for c in range(1, 30):
            threads.append(threading.Thread(
                target=worker, args=(l1, c)))
            threads.append(threading.Thread(
                target=worker, args=(l2, c + 1000)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        seqs = sorted(v["leaf_seq"] for v in results)
        self.assertEqual(seqs, list(range(1, 59)))
        # roots advance monotonically with the single commit order
        roots = [v["root"] for v in results]
        self.assertEqual(len(set(roots)), len(results))
        # every stored proof verifies under its returned (current) root
        rows = self.db.conn.execute(
            "SELECT receipt_id FROM audit_leaves ORDER BY seq"
        ).fetchall()
        for row in rows:
            a = self.db.get_receipt_audit(row["receipt_id"])
            self.assertTrue(merkle.verify_inclusion(
                a["leaf_hash"], a["steps"],
                [(p["height"], p["hash"]) for p in a["peaks"]],
                a["root"]))

    def test_concurrent_same_receipt_id_identical_everywhere(self):
        import threading

        lid = self.make_link()
        outcomes = []

        def worker():
            outcomes.append(self.db.submit_frame(
                lid, 5, "same-rid", {"v": 1}))

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # exactly one first verdict -> one leaf; the rest replay it
        self.assertEqual(sum(1 for o in outcomes if not o["retransmit"]), 1)
        seqs = {o["leaf_seq"] for o in outcomes}
        self.assertEqual(seqs, {1})
        rows = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM audit_leaves WHERE receipt_id = 'same-rid'"
        ).fetchone()
        self.assertEqual(rows["n"], 1)
        rows = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM receipts WHERE receipt_id = 'same-rid'"
        ).fetchone()
        self.assertEqual(rows["n"], 1)

    def test_concurrent_conflicting_reuses_get_no_second_leaf(self):
        import threading

        lid = self.make_link()
        self.submit(lid, 1, "rid-x")
        errors = []

        def worker(counter):
            try:
                self.db.submit_frame(lid, counter, "rid-x", {"v": 1})
            except ReceiptConflict:
                errors.append(counter)

        threads = [threading.Thread(target=worker, args=(c,))
                   for c in (2, 3, 4, 5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(errors), [2, 3, 4, 5])
        tip = self.db.audit_tip()
        self.assertEqual(tip["leaf_count"], 1)

    def test_fault_after_window_update_rolls_back_everything(self):
        from unittest import mock
        lid = self.make_link()
        self.submit(lid, 1, "ok-1")
        before_link = self.db.get_link(lid)
        before_tip = self.db.audit_tip()

        # Fail *after* the window UPDATE has executed but before COMMIT: the
        # whole transaction (window row, receipt, leaf, MMR nodes, meta) must
        # roll back together.
        real = self.db._append_audit_leaf

        def boom(*a, **kw):
            real(*a, **kw)
            raise RuntimeError("simulated crash inside transaction")

        with mock.patch.object(self.db, "_append_audit_leaf", boom):
            with self.assertRaises(RuntimeError):
                self.submit(lid, 2, "doomed")
        self.assertEqual(self.db.get_link(lid), before_link)
        self.assertEqual(self.db.audit_tip(), before_tip)
        self.assertIsNone(self.db.get_receipt_audit("doomed"))
        rows = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM receipts WHERE receipt_id = 'doomed'"
        ).fetchone()
        self.assertEqual(rows["n"], 0)
        # and the database still accepts traffic normally afterwards
        v = self.submit(lid, 2, "ok-2")
        self.assertEqual(v["status"], "accepted")
        self.assertEqual(v["leaf_seq"], 2)

    def test_full_64bit_window_persists_and_reopens(self):
        # A completely full window is 2**64-1, outside SQLite's signed 64-bit
        # INTEGER range; it must persist (as a BLOB) and come back intact.
        lid = self.make_link()
        for c in range(64):
            self.submit(lid, c, f"full-{c}")
        link = self.db.get_link(lid)
        self.assertEqual(link["highest"], 63)
        self.assertEqual(link["bitmap"], (1 << 64) - 1)
        self.assertEqual(len(link["recent"]), 64)
        self.db.close()
        db2 = Database(self.db.path)
        try:
            link = db2.get_link(lid)
            self.assertEqual(link["bitmap"], (1 << 64) - 1)
            v = db2.submit_frame(lid, 64, "full-next", {"v": 1})
            self.assertEqual(v["status"], "accepted")
        finally:
            db2.close()

    def test_legacy_integer_bitmap_rows_still_read(self):
        # Older databases stored the bitmap as a signed INTEGER; such rows
        # must keep decoding after the BLOB storage change.
        lid = self.make_link()
        self.submit(lid, 1, "legacy-1")
        self.db.conn.execute(
            "UPDATE link_windows SET bitmap = ? WHERE link_id = ?",
            (0b11, lid),
        )
        link = self.db.get_link(lid)
        self.assertEqual(link["bitmap"], 0b11)
        self.assertEqual(link["recent"], [1, 0])


    def test_migration_from_pre_audit_schema_backfills_leaves(self):
        import sqlite3
        path = os.path.join(self.tmp.name, "legacy.db")
        con = sqlite3.connect(path)
        con.executescript("""
        CREATE TABLE links (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE link_windows (link_id TEXT PRIMARY KEY, highest INTEGER, bitmap INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE receipts (
            receipt_id TEXT PRIMARY KEY, link_id TEXT NOT NULL, counter INTEGER NOT NULL,
            payload_hash TEXT NOT NULL, status TEXT NOT NULL, extended INTEGER NOT NULL,
            created_at REAL NOT NULL);
        """)
        from app.db import canonical_hash
        con.execute("INSERT INTO links VALUES ('L1','old',0)")
        con.execute("INSERT INTO link_windows VALUES ('L1', 10, ?)", (0b101,))
        con.execute(
            "INSERT INTO receipts VALUES ('old-1','L1',10,?,'accepted',10,0)",
            (canonical_hash({"v": 1}),))
        con.execute(
            "INSERT INTO receipts VALUES ('old-2','L1',8,?,'accepted',8,0)",
            (canonical_hash({"v": 2}),))
        con.commit(); con.close()

        db = Database(path)
        try:
            tip = db.audit_tip()
            self.assertEqual(tip["leaf_count"], 2)
            for i, rid in enumerate(("old-1", "old-2"), start=1):
                a = db.get_receipt_audit(rid)
                self.assertEqual(a["seq"], i)
                self.assertTrue(merkle.verify_inclusion(
                    a["leaf_hash"], a["steps"],
                    [(p["height"], p["hash"]) for p in a["peaks"]],
                    a["root"]))
            # legacy INTEGER bitmap decodes, and new verdicts keep appending
            self.assertEqual(db.get_link("L1")["bitmap"], 0b101)
            v = db.submit_frame("L1", 11, "new-1", {"v": 3})
            self.assertEqual(v["leaf_seq"], 3)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
