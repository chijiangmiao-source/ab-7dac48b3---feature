#!/usr/bin/env python3
"""End-to-end HTTP smoke test run by the Compose ``verify`` service.

Asserts wrap-around, expiry, duplicate, epoch-tie rejection and receipt
semantics against a fully running server, and exits non-zero on any failure.
"""

import hashlib
import json
import sys
import time
import urllib.error
import urllib.request

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


# Independent MMR inclusion verifier: recomputes every hash from the served
# JSON instead of trusting any server-side code.
def _h(domain, *parts):
    m = hashlib.sha256()
    m.update(domain)
    for p in parts:
        m.update(p)
    return m.hexdigest()


def _node(left_hex, right_hex):
    return _h(b"DSRX1\x00NODE\x00", bytes.fromhex(left_hex),
              bytes.fromhex(right_hex))


def _bag(peaks, self_index, acc):
    left, right = peaks[:self_index], peaks[self_index + 1:]
    if right:
        bag = right[-1][0]
        for sibling, _h2 in reversed(right[:-1]):
            bag = _node(sibling, bag)
        acc = _node(acc, bag)
    for sibling, _h2 in reversed(left):
        acc = _node(sibling, acc)
    return acc


def verify_proof(audit):
    """Recompute the root from one leaf + its served path and compare."""
    acc = audit["leaf_hash"]
    for side, _height, sibling in audit["steps"]:
        acc = _node(sibling, acc) if side == "left" else _node(acc, sibling)
    peaks = [(p["hash"], p["height"]) for p in audit["peaks"]]
    matches = [i for i, (digest, _h2) in enumerate(peaks) if digest == acc]
    if len(matches) != 1:
        return False
    return _bag(peaks, matches[0], acc) == audit["root"]


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

    # --- MMR audit trail: first verdict -> canonical leaf, replay-safe ---
    _, aud_link = request("POST", base + "/api/links", {"name": "smoke-audit"})
    alid = aud_link["id"]

    def audit(rid):
        return request("GET", f"{base}/api/links/{alid}/receipts/{rid}")

    code, a1 = request("POST", f"{base}/api/links/{alid}/frames",
                       {"counter": 10, "receipt_id": "au1", "payload": {"z": 1}})
    check(a1["leaf_seq"] == a1["leaf_count"], "verdict reports its global leaf seq")
    seq1 = a1["leaf_seq"]
    root1 = a1["root"]

    code, leaf1 = audit("au1")
    check(code == 200, "audit leaf readable from the link page")
    check(leaf1["seq"] == seq1 and leaf1["counter"] == 10
          and leaf1["status"] == "accepted" and leaf1["highest"] == 10,
          "leaf binds link, receipt, counter, verdict and post-verdict highest")
    check(leaf1["leaf_count"] == a1["leaf_count"], "leaf reports range size")
    check(verify_proof(leaf1), "minimal inclusion path verifies under current root")

    # retransmission returns the original leaf, no new append
    code, replay_v = request("POST", f"{base}/api/links/{alid}/frames",
                             {"counter": 10, "receipt_id": "au1", "payload": {"z": 1}})
    check(replay_v["retransmit"] and replay_v["leaf_seq"] == seq1,
          "retransmission returns the original leaf seq")
    _, leaf1b = audit("au1")
    check(leaf1b["leaf_hash"] == leaf1["leaf_hash"], "replay leaf hash unchanged")

    # conflicting reuse must not append a leaf
    _, tip_before = request("GET", base + "/api/audit/root")
    code, _ = request("POST", f"{base}/api/links/{alid}/frames",
                      {"counter": 11, "receipt_id": "au1", "payload": {"z": 1}})
    check(code == 409, "audit: receipt reuse -> 409")
    _, tip_after = request("GET", base + "/api/audit/root")
    check(tip_after == tip_before, "conflict appends no audit leaf")

    # after newer verdicts join, the old leaf still verifies under the newest
    # root using a freshly served (current-peaks) minimal path
    for i in range(11, 24):
        code, v = request("POST", f"{base}/api/links/{alid}/frames",
                          {"counter": i, "receipt_id": f"au{i}", "payload": {}})
        check(code == 200, f"audit growth frame {i} accepted")
    _, tip = request("GET", base + "/api/audit/root")
    _, leaf1_now = audit("au1")
    check(leaf1_now["root"] == tip["root"], "leaf page shows newest root")
    check(leaf1_now["leaf_hash"] == leaf1["leaf_hash"]
          and leaf1_now["seq"] == seq1, "leaf identity immutable after growth")
    check(verify_proof(leaf1_now), "old leaf independently verifies under newest root")

    # every leaf in the range verifies independently
    for rid in ["au1"] + [f"au{i}" for i in range(11, 24)]:
        _, lf = audit(rid)
        check(verify_proof(lf), f"leaf {rid} inclusion proof verifies")

    # unknown receipt / wrong link -> 404
    code, _ = audit("does-not-exist")
    check(code == 404, "unknown receipt -> 404")
    code, _ = request("GET", f"{base}/api/links/{lid}/receipts/au1")
    check(code == 404, "receipt of another link is not readable on this link")

    print("\nALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://web:8080"
    main(base.rstrip("/"))
