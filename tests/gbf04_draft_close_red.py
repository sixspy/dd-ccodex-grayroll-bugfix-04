"""gbf04 失败测甲：升正后草稿口必须在同一事务内关闭（置已消耗）。

红测口径（甲路径）：
- 升正成功后，影子草稿口状态必须是 promoted/已消耗，不能仍 open；
- 同一草稿口二次提交/升正必须被拒（4xx），正式规则零再增、版本不再跳、
  升正审计/历史只允许一条；
- 正式写入与草稿口关闭同事务：正式零增或联查账实不符时整体回滚，
  草稿口仍 open、正式规则与历史/审计均不得落库。

本文件在未修复代码上必须见红。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import db  # noqa: E402
from app import app  # noqa: E402


@pytest.fixture()
def client():
    tmp = Path(tempfile.mkdtemp(prefix="gbf04_close_")) / "close.db"
    old = db.DB_PATH
    db.DB_PATH = tmp
    db.init_db()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        db.DB_PATH = old


def _headers(c: TestClient, user: str = "admin", pwd: str = "admin123") -> dict:
    tok = c.post("/api/login", json={"username": user, "password": pwd}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def _raw() -> sqlite3.Connection:
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _make_gate(c: TestClient, h: dict, fid: int = 3, eid: int = 2, pct: int = 33) -> int:
    r = c.post(
        "/api/rules/shadow-bind",
        headers=h,
        json={"flag_id": fid, "env_id": eid, "kind": "percent", "percent": pct, "cohort_id": None},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_jia_promote_marks_draft_gate_consumed(client):
    h = _headers(client)
    sid = _make_gate(client, h)

    r = client.post(f"/api/rules/shadow/{sid}/promote", headers=h, json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("mode") == "official"

    # 口径一：升正响应必须明确带回草稿口已关闭/已消耗。
    assert body.get("draft_status") == "promoted" or body.get("gate_closed") is True, body

    # 口径二：跨表读，草稿口状态必须已消耗，不能仍 open（埋雷点）。
    with _raw() as conn:
        st = conn.execute("SELECT status FROM rule_shadows WHERE id=?", (sid,)).fetchone()["status"]
    assert st == "promoted", f"升正后草稿口仍为 {st!r}，未置已消耗"

    listing = client.get("/api/rules", headers=h).json()
    row = next(s for s in listing["shadows"] if s["id"] == sid)
    assert row["status"] == "promoted"


def test_jia_second_submit_forbidden_official_zero_increment(client):
    h = _headers(client)
    sid = _make_gate(client, h)

    first = client.post(f"/api/rules/shadow/{sid}/promote", headers=h, json={})
    assert first.status_code == 200, first.text
    v1 = first.json()["version"]

    with _raw() as conn:
        n0 = conn.execute(
            "SELECT COUNT(*) c FROM flag_rules WHERE flag_id=3 AND env_id=2 AND official=1"
        ).fetchone()["c"]
    assert n0 == 1

    # 另一会话/再次点击：同一草稿口二次升正必须被封死，不得再落一套正式。
    second = client.post(f"/api/rules/shadow/{sid}/promote", headers=h, json={})
    assert second.status_code >= 400, f"二次升正竟成功：{second.status_code} {second.text}"
    assert second.status_code in (400, 409), second.text

    with _raw() as conn:
        n1 = conn.execute(
            "SELECT COUNT(*) c FROM flag_rules WHERE flag_id=3 AND env_id=2 AND official=1"
        ).fetchone()["c"]
        v_now = conn.execute(
            "SELECT MAX(version) v FROM flag_rules WHERE flag_id=3 AND env_id=2 AND official=1"
        ).fetchone()["v"]
        audits = conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE action='rule_promote' AND detail=?",
            (f"from_shadow={sid}",),
        ).fetchone()["c"]
        hist = conn.execute(
            "SELECT COUNT(*) c FROM change_history WHERE action='rule_promote' AND flag_id=3 AND env_id=2"
        ).fetchone()["c"]
    assert n1 == 1, f"二次升正后正式规则数={n1}，出现第二套正式"
    assert v_now == v1, f"版本被二次跳号 {v1} -> {v_now}"
    assert audits == 1, f"同一草稿口升正审计 {audits} 条，应仅 1 条"
    assert hist == 1, f"rule_promote 历史 {hist} 条，应仅 1 条"


def test_jia_official_write_failure_rolls_back_gate_close(monkeypatch):
    """正式落库后记账抛错：同事务必须整体回滚，草稿口不得被消耗、正式零增。"""
    tmp = Path(tempfile.mkdtemp(prefix="gbf04_rb_")) / "rb.db"
    old = db.DB_PATH
    db.DB_PATH = tmp
    db.init_db()
    try:
        # raise_server_exceptions=False：把 500 作为响应拿回，而不是在测试线程重抛。
        with TestClient(app, raise_server_exceptions=False) as c:
            h = _headers(c)
            sid = _make_gate(c, h)

            import rules_svc

            def _boom(*a, **k):
                raise RuntimeError("simulated ledger failure after official write")

            monkeypatch.setattr(rules_svc, "write_history", _boom)

            r = c.post(f"/api/rules/shadow/{sid}/promote", headers=h, json={})
            assert r.status_code >= 500, r.text

            with _raw() as conn:
                st = conn.execute(
                    "SELECT status FROM rule_shadows WHERE id=?", (sid,)
                ).fetchone()["status"]
                n = conn.execute(
                    "SELECT COUNT(*) c FROM flag_rules WHERE flag_id=3 AND env_id=2 AND official=1"
                ).fetchone()["c"]
                audits = conn.execute(
                    "SELECT COUNT(*) c FROM audit_log WHERE action='rule_promote'"
                ).fetchone()["c"]
            assert st == "open", f"事务失败却关闭了草稿口：{st}"
            assert n == 0, f"回滚失败，正式规则残留 {n} 行"
            assert audits == 0, "回滚失败，审计残留"
    finally:
        db.DB_PATH = old
