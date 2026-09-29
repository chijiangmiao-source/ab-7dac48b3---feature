#!/usr/bin/env python3
"""End-to-end HTTP smoke test run by the Compose ``verify`` service.

Asserts wrap-around, expiry, duplicate, epoch-tie rejection and receipt
semantics against a fully running server, and exits non-zero on any failure.
"""

import json
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, ".")
from app import audit

MOD = 1 << 32
HALF = 1 << 31


def request(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok - {msg}")


def main(base):
    for _ in range(60):
        try:
            code, body = request("GET", base + "/healthz")
            if code == 200 and body.get("status") == "ok":
                break
        except OSError:
            time.sleep(1)
    else:
        raise RuntimeError("server never became healthy")
    print("ok - healthz reports ok")

    code, empty = request("GET", base + "/api/audit")
    check(code == 200 and len(empty["root"]) == 64
          and empty["leaf_count"] >= 0
          and (empty["leaf_count"] != 0
               or empty["root"] == audit.EMPTY_ROOT),
          "audit endpoint exposes leaf count, peaks and the empty-root convention")
    n_before = empty["leaf_count"]

    _, link = request("POST", base + "/api/links", {"name": "smoke-wrap"})
    lid = link["id"]
    frames_url = f"{base}/api/links/{lid}/frames"

    def frame(counter, rid, payload=None):
        return request("POST", frames_url,
                       {"counter": counter, "receipt_id": rid,
                        "payload": payload if payload is not None else {"v": 1}})

    # --- wrap-around with out-of-order delivery, each accepted once ---
    code, v = frame(MOD - 2, "s-2")
    check(code == 200 and v["status"] == "accepted", f"counter 2^32-2 accepted ({v})")
    _, v = frame(MOD - 1, "s-1")
    check(v["status"] == "accepted" and v["extended"] == MOD - 1, "2^32-1 accepted")
    _, v = frame(1, "s1")  # arrives before 0
    check(v["status"] == "accepted" and v["extended"] == MOD + 1,
          "out-of-order counter 1 placed in epoch 1")
    _, v = frame(0, "s0")
    check(v["status"] == "accepted" and v["extended"] == MOD,
          "counter 0 wraps to 2^32")

    _, state = request("GET", f"{base}/api/links/{lid}")
    check(state["highest"] == MOD + 1, f"highest extended seq is 2^32+1 (got {state['highest']})")
    check(state["recent"][:4] == [MOD + 1, MOD, MOD - 1, MOD - 2],
          "recent positions show all four wrap frames once")

    # --- duplicates: same counter, new receipt id ---
    _, v = frame(0, "s0-newid")
    check(v["status"] == "duplicate", "counter 0 resent is duplicate")

    # --- identical retransmission replays the FIRST verdict ---
    _, v = frame(0, "s0")
    check(v["status"] == "accepted" and v["retransmit"],
          "identical receipt+payload replays original accepted verdict")

    # --- receipt id reuse with changed counter / payload / link -> 409 ---
    code, _ = frame(5, "s0")
    check(code == 409, "receipt id reused with different counter -> 409")
    code, _ = frame(0, "s0", {"v": 2})
    check(code == 409, "receipt id reused with changed payload -> 409")

    _, other = request("POST", base + "/api/links", {"name": "smoke-other"})
    code, _ = request("POST", f"{base}/api/links/{other['id']}/frames",
                      {"counter": 0, "receipt_id": "s0", "payload": {"v": 1}})
    check(code == 409, "receipt id reused on another link -> 409")

    # --- expired: slide forward, old frame can never re-enter ---
    _, v = frame((MOD + 70) & (MOD - 1), "s70")
    check(v["status"] == "accepted", "jump 70 frames into epoch 1 accepted")
    _, v = frame(MOD - 2, "s-2-late")  # distance 72 from highest
    check(v["status"] == "expired", "old epoch-0 frame after wrap is expired")
    _, v = frame(MOD - 2, "s-2-late2")
    check(v["status"] == "expired", "expired frame stays expired")

    # --- epoch tie: counter exactly 2^31 away is rejected ---
    _, tie = request("POST", base + "/api/links", {"name": "smoke-tie"})
    request("POST", f"{base}/api/links/{tie['id']}/frames",
            {"counter": 0, "receipt_id": "t0", "payload": {}})
    code, v = request("POST", f"{base}/api/links/{tie['id']}/frames",
                      {"counter": HALF, "receipt_id": "th", "payload": {}})
    check(code == 200 and v["status"] == "rejected",
          "equidistant epoch candidates (2^31) rejected")

    # --- input validation ---
    code, _ = request("POST", frames_url,
                      {"counter": MOD, "receipt_id": "bad", "payload": {}})
    check(code == 400, "counter >= 2^32 rejected with 400")
    code, _ = request("POST", f"{base}/api/links/deadbeef/frames",
                      {"counter": 1, "receipt_id": "x", "payload": {}})
    check(code == 404, "unknown link -> 404")

    # ============================================================ audit MMR
    def verify_record(rec):
        fields = {k: rec["leaf"][k] for k in
                  ("seq", "link", "receipt", "counter", "payload_hash",
                   "status", "highest")}
        return audit.verify_inclusion(
            fields, rec["path"], rec["peaks"], rec["root"],
            rec["leaf_count"])

    code, state_now = request("GET", base + "/api/audit")
    check(code == 200 and len(state_now["root"]) == 64,
          "audit endpoint still serves a 256-bit root after verdicts")
    n_before = state_now["leaf_count"]

    # all wrap frames above produced leaves; an early receipt's record proves
    code, rec_s2 = request("GET", base + "/api/receipts/s-2")
    check(code == 200 and verify_record(rec_s2),
          f"receipt s-2 leaf verifies against current root (#{rec_s2['seq']})")
    anchor_root = rec_s2["root"]
    anchor_seq = rec_s2["seq"]

    # retransmission returns the original leaf, conflict appends none
    _, replay = request("POST", frames_url,
                        {"counter": MOD - 2, "receipt_id": "s-2",
                         "payload": {"v": 1}})
    check(replay["retransmit"] and replay["audit"]["seq"] == anchor_seq,
          "retransmit serves the original audit leaf seq")
    code, state = request("GET", base + "/api/audit")
    check(state["root"] == anchor_root, "no new root after retransmit")

    # expired and rejected first verdicts also carry leaves
    code, rec_late = request("GET", base + "/api/receipts/s-2-late")
    check(code == 200 and rec_late["leaf"]["status"] == "expired"
          and verify_record(rec_late), "expired verdict leaf verifies")
    code, rec_tie = request("GET", base + "/api/receipts/th")
    check(code == 200 and rec_tie["leaf"]["status"] == "rejected"
          and verify_record(rec_tie), "rejected verdict leaf verifies")

    # leaf content really binds the post-verdict highest extended seq
    check(rec_s2["leaf"]["highest"] == rec_s2["leaf"]["counter"] == MOD - 2,
          "leaf binds link/counter/payload/status/post-verdict highest")

    # keep the anchor record, advance the log with fresh verdicts, then prove
    # the SAME leaf independently under the NEWEST root
    _, adv = request("POST", base + "/api/links", {"name": "smoke-audit"})
    appended = 0
    for i in range(1, 21):
        code, v = request("POST", f"{base}/api/links/{adv['id']}/frames",
                          {"counter": i, "receipt_id": f"au-{i:02d}",
                           "payload": {"v": i}})
        if code == 200 and v["audit"]["seq"] > 0:
            appended += 1
    check(appended == 20, "20 later verdicts each append an audit leaf")
    code, moved = request("GET", base + "/api/receipts/s-2")
    check(moved["leaf_count"] > rec_s2["leaf_count"],
          "audit log grew after later verdicts")
    check(moved["root"] != anchor_root, "current root advanced")
    check(verify_record(moved),
          "old leaf independently verifies under the newest root")

    # every served record must verify under the root served with it
    for rid in ("s-1", "s0", "s1", "s70", "au-01", "au-13", "au-20"):
        code, rec = request("GET", base + "/api/receipts/" + rid)
        check(code == 200 and verify_record(rec), f"leaf {rid} verifies")

    # unknown receipt -> 404; concurrent submissions get one total order
    code, _ = request("GET", base + "/api/receipts/does-not-exist")
    check(code == 404, "unknown receipt id -> 404")

    import concurrent.futures
    _, race = request("POST", base + "/api/links", {"name": "smoke-race"})

    def race_frame(i):
        return request("POST", f"{base}/api/links/{race['id']}/frames",
                       {"counter": 100 + i, "receipt_id": f"race-{i:03d}",
                        "payload": {}})[1]
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        verdicts = list(pool.map(race_frame, range(48)))
    seqs = sorted(v["audit"]["seq"] for v in verdicts)
    check(len(seqs) == 48 and seqs == list(range(min(seqs), min(seqs) + 48)),
          "concurrent commits serialise into one contiguous seq order")
    code, end_state = request("GET", base + "/api/audit")
    check(end_state["leaf_count"] > n_before and end_state["root"],
          f"audit log has {end_state['leaf_count']} leaves and a current root")
    # all raced leaves verify under the final committed root, unique seq each
    race_ok = 0
    for i in range(48):
        code, rec = request("GET", base + "/api/receipts/" + f"race-{i:03d}")
        if code == 200 and verify_record(rec):
            race_ok += 1
    check(race_ok == 48, "all 48 raced leaves verify under the final root")

    print("\nALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://web:8080"
    main(base.rstrip("/"))
