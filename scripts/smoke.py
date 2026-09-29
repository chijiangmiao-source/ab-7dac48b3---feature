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

    print("\nALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://web:8080"
    main(base.rstrip("/"))
