"""gbf04 失败测乙：求值读到“双正式版本”时不得各取一行互相打假绿。

账实口径：同一 (flag, env) 只允许一个 canonical 正式版本。若库里因历史/异常
出现两行 official=1（双正式），求值必须：
- 确定性地取最高版本（ORDER BY version DESC, id DESC），不能裸 fetchone()
  随 rowid 取到旧版，导致两个断言各绿各的；
- 明确上报 official_count / consistent / dual_official 冲突，而不是返回一个
  看似正常的假绿结果；
- /api/evaluate/board 同样必须给 canonical 行并标出冲突。

另含端到端一条：正常升正后求值必须 consistent、official_count=1。
未修复代码上本文件必须见红。
"""
from __future__ import annotations

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
    tmp = Path(tempfile.mkdtemp(prefix="gbf04_dual_")) / "dual.db"
    old = db.DB_PATH
    db.DB_PATH = tmp
    db.init_db()
    try:
        with TestClient(app) as c:
            yield c, tmp
    finally:
        db.DB_PATH = old


def _headers(c: TestClient) -> dict:
    tok = c.post("/api/login", json={"username": "admin", "password": "admin123"}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def _raw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _drop_unique_and_seed_dual(path: Path) -> None:
    """重建无 UNIQUE 的 flag_rules，塞入 v1=0% 与 v2=100% 两个“正式”版本。"""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE flag_rules_nouq(
          id INTEGER PRIMARY KEY,
          flag_id INTEGER NOT NULL REFERENCES flags(id),
          env_id INTEGER NOT NULL REFERENCES environments(id),
          kind TEXT NOT NULL,
          percent INTEGER NOT NULL DEFAULT 0,
          cohort_id INTEGER REFERENCES cohorts(id),
          version INTEGER NOT NULL DEFAULT 1,
          official INTEGER NOT NULL DEFAULT 1,
          updated_at REAL NOT NULL
        );
        INSERT INTO flag_rules_nouq
          (id, flag_id, env_id, kind, percent, cohort_id, version, official, updated_at)
        SELECT id, flag_id, env_id, kind, percent, cohort_id, version, official, updated_at
        FROM flag_rules;
        DROP TABLE flag_rules;
        ALTER TABLE flag_rules_nouq RENAME TO flag_rules;
        """
    )
    # flag 5 = notify.batch_push(draft), env 1 = dev；该格种子无规则。
    conn.execute(
        "INSERT INTO flag_rules(flag_id,env_id,kind,percent,cohort_id,version,official,updated_at) "
        "VALUES(5,1,'percent',0,NULL,1,1,1700300000.0)"
    )
    conn.execute(
        "INSERT INTO flag_rules(flag_id,env_id,kind,percent,cohort_id,version,official,updated_at) "
        "VALUES(5,1,'percent',100,NULL,2,1,1700301000.0)"
    )
    conn.commit()
    conn.close()


def test_yi_evaluate_picks_canonical_and_flags_dual(client):
    c, path = client
    _drop_unique_and_seed_dual(path)
    h = _headers(c)

    results = []
    for subj in ("u-1", "u-2", "u-3"):
        r = c.post(
            "/api/evaluate",
            headers=h,
            json={"flag_key": "notify.batch_push", "env_code": "dev", "subject_key": subj},
        )
        assert r.status_code == 200, r.text
        results.append(r.json())

    for body in results:
        # 必须确定性读到最高 v2（100% => 全命中），而不是裸 fetchone 拿到 v1(0%)。
        assert body["version"] == 2, f"求值读到非 canonical 版本：{body}"
        assert body["enabled"] is True, f"v2=100% 应命中：{body}"
        # 必须如实上报双正式冲突，禁止假绿。
        assert body.get("official_count") == 2, body
        assert body.get("consistent") is False, body
        assert body.get("dual_official") is True or body.get("conflict"), body


def test_yi_evaluate_board_reports_dual_conflict(client):
    c, path = client
    _drop_unique_and_seed_dual(path)
    h = _headers(c)
    r = c.get("/api/evaluate/board", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    # board 不应把同一格的两个正式版本当成两条互不相干的绿行。
    dup_rows = [x for x in body["rules"] if x["flag_key"] == "notify.batch_push" and x["env_code"] == "dev"]
    assert len(dup_rows) == 1, f"board 出现双正式行：{dup_rows}"
    assert dup_rows[0]["version"] == 2
    conflicts = body.get("conflicts") or []
    assert any(
        x.get("flag_key") == "notify.batch_push" and x.get("env_code") == "dev"
        for x in conflicts
    ), f"board 未标出双正式冲突：{body}"


def test_yi_after_clean_promote_evaluate_is_consistent_single(client):
    c, _ = client
    h = _headers(c)
    sid = c.post(
        "/api/rules/shadow-bind",
        headers=h,
        json={"flag_id": 3, "env_id": 2, "kind": "percent", "percent": 100, "cohort_id": None},
    ).json()["id"]
    p = c.post(f"/api/rules/shadow/{sid}/promote", headers=h, json={})
    assert p.status_code == 200, p.text

    r = c.post(
        "/api/evaluate",
        headers=h,
        json={"flag_key": "feed.ranking_v2", "env_code": "staging", "subject_key": "u-7"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["enabled"] is True  # percent=100
    assert body.get("official_count", 1) == 1
    assert body.get("consistent", True) is True
