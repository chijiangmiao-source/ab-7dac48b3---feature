import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error

from app.server import Server
from app import audit

MOD = 1 << 32
HALF = 1 << 31


def _request(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class TestHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(cls.tmp.name, "http.db")
        cls.server = Server(("127.0.0.1", 0), db_path, log_file=open(os.devnull, "w"))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"
        # Wait for readiness.
        for _ in range(50):
            try:
                _request("GET", cls.base + "/healthz")
                break
            except OSError:
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.db.close()
        cls.tmp.cleanup()

    def test_health(self):
        code, body = _request("GET", self.base + "/healthz")
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "ok")

    def test_index_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])
            body = resp.read()
            self.assertIn(b"\xe6\xb7\xb1\xe7\xa9\xba", body)  # 深空
            self.assertIn(b"/api/receipts/", body)
            self.assertIn(b"verifyRecord", body)

    def test_full_link_and_frame_flow(self):
        code, link = _request("POST", self.base + "/api/links", {"name": "flow"})
        self.assertEqual(code, 201)
        lid = link["id"]

        code, listing = _request("GET", self.base + "/api/links")
        self.assertEqual(code, 200)
        self.assertTrue(any(l["id"] == lid for l in listing["links"]))

        def frame(counter, rid, payload=None):
            return _request("POST", f"{self.base}/api/links/{lid}/frames",
                            {"counter": counter, "receipt_id": rid,
                             "payload": payload if payload is not None else {"v": 1}})

        code, v = frame(MOD - 1, "w1")
        self.assertEqual(code, 200)
        self.assertEqual(v["status"], "accepted")

        code, v = frame(0, "w2")                 # wraps to epoch 1
        self.assertEqual(v["status"], "accepted")
        self.assertEqual(v["extended"], MOD)

        code, v = frame(0, "w2")                 # identical replay
        self.assertEqual(v["status"], "accepted")
        self.assertTrue(v["retransmit"])

        code, v = frame(0, "w2", {"v": 9})       # payload changed
        self.assertEqual(code, 409)

        code, v = frame(0, "w2")                 # ledger unaffected by conflict
        self.assertEqual(code, 200)
        self.assertTrue(v["retransmit"])

        code, v = frame(0, "w2-on-other-link")  # sanity: other id fine here
        self.assertEqual(code, 200)
        self.assertEqual(v["status"], "duplicate")

    def test_unknown_link_404(self):
        code, _ = _request("POST",
                           f"{self.base}/api/links/nope/frames",
                           {"counter": 1, "receipt_id": "x", "payload": {}})
        self.assertEqual(code, 404)

    def test_bad_counter_400(self):
        _, link = _request("POST", self.base + "/api/links", {"name": "b"})
        lid = link["id"]
        for bad in (-1, MOD, "abc", None):
            code, body = _request(
                "POST", f"{self.base}/api/links/{lid}/frames",
                {"counter": bad, "receipt_id": "r", "payload": {}})
            self.assertEqual(code, 400, bad)

    def test_equidistant_counter_rejected(self):
        _, link = _request("POST", self.base + "/api/links", {"name": "tie"})
        lid = link["id"]
        _request("POST", f"{self.base}/api/links/{lid}/frames",
                 {"counter": 0, "receipt_id": "z0", "payload": {}})
        code, v = _request("POST", f"{self.base}/api/links/{lid}/frames",
                           {"counter": HALF, "receipt_id": "zh", "payload": {}})
        self.assertEqual(code, 200)
        self.assertEqual(v["status"], "rejected")

    # --------------------------------------------------------------- audit

    def test_audit_empty_state(self):
        code, state = _request("GET", self.base + "/api/audit")
        self.assertEqual(code, 200)
        self.assertEqual(state["leaf_count"], 0)
        self.assertEqual(state["root"], audit.EMPTY_ROOT)
        self.assertEqual(state["peaks"], [])

    def test_audit_leaf_and_inclusion_record(self):
        _, link = _request("POST", self.base + "/api/links", {"name": "aud"})
        lid = link["id"]

        def frame(counter, rid, payload=None):
            return _request("POST", f"{self.base}/api/links/{lid}/frames",
                            {"counter": counter, "receipt_id": rid,
                             "payload": payload if payload is not None else {"v": 1}})

        code, state0 = _request("GET", self.base + "/api/audit")
        self.assertEqual(code, 200)
        code, v = frame(10, "aud-1")
        self.assertEqual(code, 200)
        self.assertEqual(v["audit"]["seq"], state0["leaf_count"] + 1)

        code, rec = _request("GET", self.base + "/api/receipts/aud-1")
        self.assertEqual(code, 200)
        self.assertEqual(rec["seq"], state0["leaf_count"] + 1)
        self.assertEqual(rec["leaf"]["receipt"], "aud-1")
        self.assertEqual(rec["leaf"]["link"], lid)
        self.assertEqual(rec["leaf"]["counter"], 10)
        self.assertEqual(rec["leaf"]["highest"], 10)
        self.assertEqual(rec["leaf"]["status"], "accepted")
        self.assertTrue(rec["leaf"]["hash"])
        # independently verify the served proof
        fields = {k: rec["leaf"][k] for k in
                  ("seq", "link", "receipt", "counter", "payload_hash",
                   "status", "highest")}
        self.assertTrue(audit.verify_inclusion(
            fields, rec["path"], rec["peaks"], rec["root"],
            rec["leaf_count"]))

    def test_audit_record_stays_valid_under_later_root(self):
        _, link = _request("POST", self.base + "/api/links", {"name": "aud2"})
        lid = link["id"]
        for i in range(1, 6):
            _request("POST", f"{self.base}/api/links/{lid}/frames",
                     {"counter": i, "receipt_id": f"a2-{i}", "payload": {}})
        _, first = _request("GET", self.base + "/api/receipts/a2-1")
        count_at_first = first["leaf_count"]
        self.assertGreaterEqual(count_at_first, 5)
        fields = {k: first["leaf"][k] for k in
                  ("seq", "link", "receipt", "counter", "payload_hash",
                   "status", "highest")}
        self.assertTrue(audit.verify_inclusion(
            fields, first["path"], first["peaks"], first["root"],
            first["leaf_count"]))
        for i in range(6, 20):
            _request("POST", f"{self.base}/api/links/{lid}/frames",
                     {"counter": i, "receipt_id": f"a2-{i}", "payload": {}})
        _, later = _request("GET", self.base + "/api/receipts/a2-1")
        self.assertEqual(later["leaf_count"], count_at_first + 14)
        self.assertNotEqual(later["root"], first["root"])
        fields = {k: later["leaf"][k] for k in
                  ("seq", "link", "receipt", "counter", "payload_hash",
                   "status", "highest")}
        self.assertTrue(audit.verify_inclusion(
            fields, later["path"], later["peaks"], later["root"],
            later["leaf_count"]))

    def test_audit_retransmit_and_conflict_unchanged(self):
        _, link = _request("POST", self.base + "/api/links", {"name": "aud3"})
        lid = link["id"]
        _, v1 = _request("POST", f"{self.base}/api/links/{lid}/frames",
                         {"counter": 1, "receipt_id": "same", "payload": {}})
        _, state1 = _request("GET", self.base + "/api/audit")
        _, v2 = _request("POST", f"{self.base}/api/links/{lid}/frames",
                         {"counter": 1, "receipt_id": "same", "payload": {}})
        self.assertTrue(v2["retransmit"])
        self.assertEqual(v2["audit"]["seq"], v1["audit"]["seq"])
        _, state2 = _request("GET", self.base + "/api/audit")
        self.assertEqual(state1, state2)

        code, _ = _request("POST", f"{self.base}/api/links/{lid}/frames",
                           {"counter": 2, "receipt_id": "same", "payload": {}})
        self.assertEqual(code, 409)
        _, state3 = _request("GET", self.base + "/api/audit")
        self.assertEqual(state1, state3)

    def test_audit_unknown_receipt_404(self):
        code, body = _request("GET", self.base + "/api/receipts/nope-xyz")
        self.assertEqual(code, 404)
        self.assertIn("unknown receipt", body["error"])

    def test_audit_root_consistent_across_verdicts(self):
        _, link = _request("POST", self.base + "/api/links", {"name": "aud4"})
        lid = link["id"]
        _, v = _request("POST", f"{self.base}/api/links/{lid}/frames",
                        {"counter": 7, "receipt_id": "r7", "payload": {}})
        _, state = _request("GET", self.base + "/api/audit")
        self.assertEqual(v["audit"]["root"], state["root"])
        # URL-encoded receipt ids are decoded
        _, v2 = _request(
            "POST", f"{self.base}/api/links/{lid}/frames",
            {"counter": 8, "receipt_id": "tx id/001", "payload": {}})
        self.assertEqual(v2["status"], "accepted")
        code, rec = _request(
            "GET", self.base + "/api/receipts/tx%20id%2F001")
        self.assertEqual(code, 200)
        self.assertEqual(rec["leaf"]["receipt"], "tx id/001")


if __name__ == "__main__":
    unittest.main()
