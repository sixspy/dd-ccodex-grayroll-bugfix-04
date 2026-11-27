#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""duel_draft_gate_close —— 并发双请求决斗「同一草稿口升正互斥」。

同一时刻放出两路 POST 打同一条影子草稿口的升正端点，断言 grayroll-bugfix-04
合入门槛：
  1) 恰好一路 200 成功，另一路 409 判败（败方禁止标成功）；
  2) 升正后草稿口 status=promoted（关闭 / 已消耗）；
  3) 败方只在 /api/locks/contention 留一段争用痕，没有第二套正式。

仅用标准库发请求，可对任意已启动服务运行：
  python3 scripts/duel_draft_gate_close.py \
      --base-url http://127.0.0.1:8020 --gate rule --rounds 3

不带 --base-url 而加 --selftest，则在临时库上自起 uvicorn 真并发决斗。
退出码 0=草稿口封死；1=仍可双成 / 门槛失守。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def http_json(method: str, url: str, token: str | None = None, payload=None, timeout: float = 30.0):
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            body = {}
        return e.code, body


def login(base: str, user: str, pwd: str) -> str:
    st, body = http_json("POST", base + "/api/login", payload={"username": user, "password": pwd})
    if st != 200:
        raise SystemExit(f"登录失败 {st}: {body}")
    return body["token"]


def make_gate(base: str, token: str, gate: str, fid: int, eid: int, pct: int) -> int:
    if gate == "rule":
        st, body = http_json(
            "POST", base + "/api/rules/shadow-bind", token,
            {"flag_id": fid, "env_id": eid, "kind": "percent", "percent": pct, "cohort_id": None},
        )
    else:
        st, body = http_json(
            "POST", base + "/api/releases/publish", token,
            {
                "flag_id": fid, "env_id": eid, "kind": "percent", "percent": pct,
                "cohort_id": None, "summary": f"决斗草稿口 {int(time.time())}", "use_shadow": True,
            },
        )
    if st != 200:
        raise SystemExit(f"造草稿口失败 {st}: {body}")
    return body["id"]


def duel_once(base: str, token: str, gate: str, sid: int):
    path = f"/api/rules/shadow/{sid}/promote" if gate == "rule" else f"/api/releases/shadow/{sid}/promote"
    url = base + path
    barrier = threading.Barrier(2)

    def fire(_):
        barrier.wait()
        return http_json("POST", url, token, {})

    with ThreadPoolExecutor(max_workers=2) as ex:
        return list(ex.map(fire, [0, 1]))


def gate_status(base: str, token: str, gate: str, sid: int):
    endpoint = "/api/rules" if gate == "rule" else "/api/releases"
    st, body = http_json("GET", base + endpoint, token)
    if st != 200:
        return None
    for s in body.get("shadows", []):
        if s["id"] == sid:
            return s["status"]
    return None


def contention_count(base: str, token: str, lock_key: str) -> int:
    st, body = http_json("GET", base + "/api/locks/contention?limit=200", token)
    if st != 200:
        return -1
    return sum(1 for m in body.get("items", []) if m.get("lock_key") == lock_key)


def run_rounds(base: str, gate: str, rounds: int, user: str, pwd: str,
               fid: int, eid: int, pct: int) -> bool:
    token = login(base, user, pwd)
    lock_key = f"flag-rule:{fid}:{eid}" if gate == "rule" else f"flag-release:{fid}:{eid}"
    all_ok = True
    for i in range(1, rounds + 1):
        sid = make_gate(base, token, gate, fid, eid, pct)
        marks_before = contention_count(base, token, lock_key)
        results = duel_once(base, token, gate, sid)
        codes = sorted(st for st, _ in results)
        n_win = sum(1 for st, _ in results if st == 200)
        n_409 = sum(1 for st, _ in results if st == 409)
        time.sleep(0.15)  # 等败方争用痕提交可见
        status = gate_status(base, token, gate, sid)
        marks_after = contention_count(base, token, lock_key)
        mark_delta = marks_after - marks_before
        mark = mark_delta == 1  # 争用核销单段：本决斗恰好新增一条

        ok = (
            n_win == 1 and n_409 == 1 and codes == [200, 409]
            and status == "promoted" and mark
        )
        all_ok = all_ok and ok
        print(f"[round {i}] gate={gate} sid={sid} codes={codes} "
              f"shadow_status={status} contention_delta={mark_delta} -> "
              f"{'HOLD 草稿口封死' if ok else 'FAIL 门槛失守'}")
        for st, body in results:
            print(f"    {st}: mode={body.get('mode')} version={body.get('version')} detail={body.get('detail')}")
    print("DUEL VERDICT:", "GATE_CLOSED ✅" if all_ok else "DOUBLE_LANDING ❌")
    return all_ok


def selftest(gate: str, rounds: int) -> bool:
    import tempfile
    tmp_db = Path(tempfile.mkdtemp(prefix="duel_gate_")) / "duel.db"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    import db
    db.DB_PATH = tmp_db
    db.init_db()
    import uvicorn
    from app import app

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    port = None
    for ss in server.servers:
        for sock in ss.sockets:
            port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if http_json("GET", base + "/api/health")[0] == 200:
                    break
            except Exception:
                time.sleep(0.1)
        return run_rounds(base, gate, rounds, "admin", "admin123", 3, 2, 33)
    finally:
        server.should_exit = True
        th.join(timeout=5)


def main():
    ap = argparse.ArgumentParser(description="并发双请求决斗同草稿口升正互斥")
    ap.add_argument("--base-url", default=None, help="例如 http://127.0.0.1:8020")
    ap.add_argument("--selftest", action="store_true", help="临时库自起服务决斗")
    ap.add_argument("--gate", choices=["rule", "release"], default="rule")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="admin123")
    ap.add_argument("--flag-id", type=int, default=3)
    ap.add_argument("--env-id", type=int, default=2)
    ap.add_argument("--percent", type=int, default=33)
    args = ap.parse_args()

    if args.selftest or not args.base_url:
        ok = selftest(args.gate, args.rounds)
    else:
        ok = run_rounds(
            args.base_url.rstrip("/"), args.gate, args.rounds,
            args.user, args.password, args.flag_id, args.env_id, args.percent,
        )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
