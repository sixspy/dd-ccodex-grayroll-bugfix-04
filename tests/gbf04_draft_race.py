"""gbf04 争用失败测：两路并发抢「同一草稿口升正互斥」，禁止无锁双成。

修复后必须满足：
- 同一条影子草稿口的两路并发升正，恰好一路 200，另一路 409（败方禁止标成功）；
- 正式规则仍只有一套、版本只跳一次，升正历史/审计仅一条；
- 败方只允许在 contention_marks 留一段争用痕（/api/locks/contention 可见），
  不得写任何正式/历史/审计成功记录。

用真实 uvicorn 线程 + 双 httpx 请求打出真并发（不是顺序调用冒充）。
未修复代码上本文件必须见红（当前为两路 200 无锁双成、零争用痕）。
"""
from __future__ import annotations

import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import db  # noqa: E402
from app import app  # noqa: E402


@pytest.fixture()
def live_server(tmp_path, monkeypatch):
    db_file = tmp_path / "race.db"
    monkeypatch.setattr(db, "DB_PATH", db_file)
    db.init_db()

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    port = None
    for sock_server in server.servers:
        for sock in sock_server.sockets:
            port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    # 等健康检查就绪。
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            r = httpx.get(base + "/api/health", timeout=2)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.1)

    yield base, db_file

    server.should_exit = True
    thread.join(timeout=5)


def _token(base: str, user: str = "admin", pwd: str = "admin123") -> str:
    r = httpx.post(
        base + "/api/login", json={"username": user, "password": pwd}, timeout=10
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _duel(base: str, token: str, url_path: str):
    """两路真并发 POST 同一升正端点，返回 [(status, json-ish text), ...]。"""
    barrier = threading.Barrier(2)
    headers = {"Authorization": f"Bearer {token}"}

    def _fire(_):
        barrier.wait()
        with httpx.Client(base_url=base, timeout=30) as cc:
            rr = cc.post(url_path, headers=headers, json={})
            try:
                payload = rr.json()
            except Exception:
                payload = {"raw": rr.text}
            return rr.status_code, payload

    with ThreadPoolExecutor(max_workers=2) as ex:
        return list(ex.map(_fire, [0, 1]))


def _rows(db_file: Path, sql: str, args=()):
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def test_race_rule_gate_single_winner_loser_only_mark(live_server):
    base, db_file = live_server
    tok = _token(base)
    h = {"Authorization": f"Bearer {tok}"}
    with httpx.Client(base_url=base, timeout=10) as c:
        sid = c.post(
            "/api/rules/shadow-bind",
            headers=h,
            json={"flag_id": 3, "env_id": 2, "kind": "percent", "percent": 41, "cohort_id": None},
        ).json()["id"]

    results = _duel(base, tok, f"/api/rules/shadow/{sid}/promote")
    codes = sorted(s for s, _ in results)

    winners = [(s, b) for s, b in results if s == 200]
    losers = [(s, b) for s, b in results if s != 200]
    assert len(winners) == 1, f"应有且仅有一路成功，实际：{results}"
    assert len(losers) == 1, f"应有且仅有一路败方，实际：{results}"
    assert losers[0][0] == 409, f"败方必须是 409 争用冲突且禁止标成功：{results}"

    # 账实：一套正式、版本 1、升正审计/历史各一条。
    assert _rows(
        db_file,
        "SELECT COUNT(*) c FROM flag_rules WHERE flag_id=3 AND env_id=2 AND official=1",
    )[0]["c"] == 1
    assert _rows(
        db_file,
        "SELECT MAX(version) v FROM flag_rules WHERE flag_id=3 AND env_id=2 AND official=1",
    )[0]["v"] == 1
    assert _rows(
        db_file,
        "SELECT COUNT(*) c FROM audit_log WHERE action='rule_promote' AND detail=?",
        (f"from_shadow={sid}",),
    )[0]["c"] == 1
    assert _rows(
        db_file,
        "SELECT COUNT(*) c FROM change_history WHERE action='rule_promote' AND flag_id=3 AND env_id=2",
    )[0]["c"] == 1
    shadow = _rows(db_file, "SELECT status FROM rule_shadows WHERE id=?", (sid,))[0]
    assert shadow["status"] == "promoted"

    # 败方只留一段争用痕。
    marks = _rows(
        db_file, "SELECT * FROM contention_marks WHERE lock_key=?", ("flag-rule:3:2",)
    )
    assert len(marks) == 1, f"同草稿口争用痕应为单段一条，实际 {len(marks)}：{marks}"

    with httpx.Client(base_url=base, timeout=10) as c:
        feed = c.get("/api/locks/contention", headers=h).json()["items"]
    assert any(m["lock_key"] == "flag-rule:3:2" for m in feed), "争用痕未在 /api/locks/contention 可见"


def test_race_release_gate_single_winner_loser_only_mark(live_server):
    base, db_file = live_server
    tok = _token(base, user="writer", pwd="writer123")
    h = {"Authorization": f"Bearer {tok}"}
    with httpx.Client(base_url=base, timeout=10) as c:
        sid = c.post(
            "/api/releases/publish",
            headers=h,
            json={
                "flag_id": 5,
                "env_id": 1,
                "kind": "percent",
                "percent": 18,
                "cohort_id": None,
                "summary": "开发环境一成八灰度",
                "use_shadow": True,
            },
        ).json()["id"]

    results = _duel(base, tok, f"/api/releases/shadow/{sid}/promote")
    codes = [s for s, _ in results]
    assert codes.count(200) == 1, f"发布草稿口双成：{results}"
    loser_codes = [s for s in codes if s != 200]
    assert loser_codes == [409], f"败方必须唯一且 409：{results}"

    assert _rows(
        db_file,
        "SELECT COUNT(*) c FROM flag_rules WHERE flag_id=5 AND env_id=1 AND official=1",
    )[0]["c"] == 1
    assert _rows(
        db_file,
        "SELECT COUNT(*) c FROM releases WHERE flag_id=5 AND env_id=1 AND official=1 AND status='published'",
    )[0]["c"] == 1, "败方不得再落一条正式发布"
    assert _rows(
        db_file,
        "SELECT COUNT(*) c FROM audit_log WHERE action='release_promote' AND detail=?",
        (f"from_shadow={sid}",),
    )[0]["c"] == 1
    marks = _rows(
        db_file, "SELECT * FROM contention_marks WHERE lock_key=?", ("flag-release:5:1",)
    )
    assert len(marks) == 1, f"发布草稿口争用痕应为单段一条，实际 {len(marks)}"
