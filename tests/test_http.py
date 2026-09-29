import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error

from app.server import Server

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
            self.assertIn(b"\xe6\xb7\xb1\xe7\xa9\xba", resp.read())  # 深空

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

    def test_audit_root_tip_shape(self):
        code, tip = _request("GET", self.base + "/api/audit/root")
        self.assertEqual(code, 200)
        self.assertIsInstance(tip["leaf_count"], int)
        self.assertGreaterEqual(tip["leaf_count"], 0)
        self.assertRegex(tip["root"], r"^[0-9a-f]{64}$")

    def test_receipt_audit_endpoint_and_proof(self):
        from app import merkle
        code, link = _request("POST", self.base + "/api/links", {"name": "aud"})
        lid = link["id"]
        code, v = _request("POST", f"{self.base}/api/links/{lid}/frames",
                           {"counter": 42, "receipt_id": "aud-1",
                            "payload": {"x": 1}})
        self.assertEqual(code, 200)
        self.assertEqual(v["leaf_seq"], v["leaf_count"])

        code, a = _request("GET", f"{self.base}/api/links/{lid}/receipts/aud-1")
        self.assertEqual(code, 200)
        self.assertEqual(a["seq"], v["leaf_seq"])
        self.assertEqual(a["counter"], 42)
        self.assertEqual(a["status"], "accepted")
        self.assertEqual(a["highest"], 42)
        self.assertEqual(a["link_id"], lid)
        self.assertEqual(a["receipt_id"], "aud-1")
        self.assertEqual(a["root"], v["root"])
        self.assertTrue(merkle.verify_inclusion(
            a["leaf_hash"],
            [(s[0], s[1], s[2]) for s in a["steps"]],
            [(p["height"], p["hash"]) for p in a["peaks"]],
            a["root"]))

        # retransmission returns the same leaf; path is still valid
        _request("POST", f"{self.base}/api/links/{lid}/frames",
                 {"counter": 42, "receipt_id": "aud-1", "payload": {"x": 1}})
        code, a2 = _request("GET", f"{self.base}/api/links/{lid}/receipts/aud-1")
        self.assertEqual(code, 200)
        self.assertEqual(a2["seq"], a["seq"])
        self.assertEqual(a2["leaf_hash"], a["leaf_hash"])

    def test_older_leaf_verifies_under_newer_root_via_http(self):
        from app import merkle
        _, link = _request("POST", self.base + "/api/links", {"name": "grow"})
        lid = link["id"]
        _request("POST", f"{self.base}/api/links/{lid}/frames",
                 {"counter": 1, "receipt_id": "g1", "payload": {}})
        _, early = _request("GET", f"{self.base}/api/links/{lid}/receipts/g1")
        for i in range(2, 12):
            _request("POST", f"{self.base}/api/links/{lid}/frames",
                     {"counter": i, "receipt_id": f"g{i}", "payload": {}})
        _, tip = _request("GET", self.base + "/api/audit/root")
        _, now = _request("GET", f"{self.base}/api/links/{lid}/receipts/g1")
        self.assertEqual(now["root"], tip["root"])
        self.assertEqual(now["leaf_hash"], early["leaf_hash"])
        self.assertEqual(now["seq"], early["seq"])
        # current minimal path re-derived against the newest peaks verifies
        self.assertTrue(merkle.verify_inclusion(
            now["leaf_hash"],
            [(s[0], s[1], s[2]) for s in now["steps"]],
            [(p["height"], p["hash"]) for p in now["peaks"]],
            now["root"]))

    def test_receipt_audit_scoped_to_link_and_404(self):
        _, l1 = _request("POST", self.base + "/api/links", {"name": "a1"})
        _, l2 = _request("POST", self.base + "/api/links", {"name": "a2"})
        _request("POST", f"{self.base}/api/links/{l1['id']}/frames",
                 {"counter": 1, "receipt_id": "only-on-l1", "payload": {}})
        code, _ = _request(
            "GET", f"{self.base}/api/links/{l2['id']}/receipts/only-on-l1")
        self.assertEqual(code, 404)
        code, _ = _request(
            "GET", f"{self.base}/api/links/{l1['id']}/receipts/missing")
        self.assertEqual(code, 404)

    def test_conflict_appends_no_leaf_via_http(self):
        _, link = _request("POST", self.base + "/api/links", {"name": "cf"})
        lid = link["id"]
        _request("POST", f"{self.base}/api/links/{lid}/frames",
                 {"counter": 1, "receipt_id": "cf-1", "payload": {}})
        _, before = _request("GET", self.base + "/api/audit/root")
        code, _ = _request("POST", f"{self.base}/api/links/{lid}/frames",
                           {"counter": 2, "receipt_id": "cf-1", "payload": {}})
        self.assertEqual(code, 409)
        _, after = _request("GET", self.base + "/api/audit/root")
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
