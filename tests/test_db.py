import os
import tempfile
import unittest

from app.db import Database, ReceiptConflict
from app import window as win

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


if __name__ == "__main__":
    unittest.main()
