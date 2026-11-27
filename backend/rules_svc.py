"""灰度规则只读与影子绑定（乙路径草案；升正按甲口径同事务关闭草稿口）。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from audit_svc import (
    count_audit,
    count_history,
    last_promoter,
    write_audit,
    write_history,
)
from auth import current_user, require_writer
from db import connect
from locks import acquire, add_contention_mark, release
from policies import assert_flag_publishable, assert_not_killed_or_allow_kill, describe_path
from validators import require_int, require_percent, require_rule_kind

router = APIRouter(prefix="/api/rules", tags=["rules"])


class ShadowBindIn(BaseModel):
    flag_id: int
    env_id: int
    kind: str
    percent: int = 0
    cohort_id: int | None = None


@router.get("")
def list_rules(authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        official = [
            dict(r)
            for r in conn.execute(
                """
                SELECT r.*, f.key AS flag_key, e.code AS env_code
                FROM flag_rules r
                JOIN flags f ON f.id=r.flag_id
                JOIN environments e ON e.id=r.env_id
                WHERE r.official=1
                ORDER BY r.id
                """
            ).fetchall()
        ]
        shadows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM rule_shadows ORDER BY id DESC"
            ).fetchall()
        ]
    return {"official": official, "shadows": shadows}


@router.post("/shadow-bind")
def shadow_bind(body: ShadowBindIn, authorization: str | None = Header(default=None)):
    """乙路径：只写影子人群/比例绑定，正式规则零改。"""
    user = current_user(authorization)
    require_writer(user)
    fid = require_int(body.flag_id, "开关", 1)
    eid = require_int(body.env_id, "环境", 1)
    kind = require_rule_kind(body.kind)
    pct = require_percent(body.percent)
    key = f"flag-rule:{fid}:{eid}"
    with connect() as conn:
        token = acquire(conn, key, user["username"])
        try:
            fl = conn.execute("SELECT * FROM flags WHERE id=?", (fid,)).fetchone()
            if not fl:
                raise HTTPException(404, detail="开关不存在")
            assert_flag_publishable(fl["status"])
            assert_not_killed_or_allow_kill(fl["status"])
            if not conn.execute("SELECT 1 FROM environments WHERE id=?", (eid,)).fetchone():
                raise HTTPException(404, detail="环境不存在")
            if body.cohort_id and not conn.execute(
                "SELECT 1 FROM cohorts WHERE id=?", (body.cohort_id,)
            ).fetchone():
                raise HTTPException(404, detail="人群不存在")
            now = time.time()
            cur = conn.execute(
                "INSERT INTO rule_shadows(flag_id, env_id, kind, percent, cohort_id, status, created_at, created_by) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (fid, eid, kind, pct, body.cohort_id, "open", now, user["username"]),
            )
            sid = cur.lastrowid
            write_audit(conn, user["username"], "rule_shadow", "rule_shadow", sid, describe_path("cohort_bind", True))
            write_history(conn, user["username"], "rule_shadow", f"影子绑定 flag={fid} env={eid}", flag_id=fid, env_id=eid)
            conn.commit()
            return {"mode": "shadow", "id": sid, "path": describe_path("cohort_bind", True)}
        finally:
            release(conn, key, token)
            conn.commit()


def _cross_check_rule_promotion(conn, shadow_id: int, fid: int, eid: int, ver: int, sh) -> None:
    """同事务联查跨表账实：正式区恰好一套、草稿口已 promoted、两边字段/版本一致、审计 exactly-once。"""
    n_official = conn.execute(
        "SELECT COUNT(*) c FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1",
        (fid, eid),
    ).fetchone()["c"]
    if n_official != 1:
        raise RuntimeError(f"升正账实不符：正式规则数={n_official}，应为 1")

    row = conn.execute(
        """
        SELECT r.version AS rv, r.kind AS rk, r.percent AS rp, r.cohort_id AS rc,
               s.kind AS sk, s.percent AS sp, s.cohort_id AS sc, s.status AS ss
        FROM flag_rules r
        JOIN rule_shadows s ON s.id=?
        WHERE r.flag_id=? AND r.env_id=? AND r.official=1
        """,
        (shadow_id, fid, eid),
    ).fetchone()
    if row is None:
        raise RuntimeError("升正账实不符：联查不到正式规则↔草稿口记录")
    if row["ss"] != "promoted" or row["rv"] != ver:
        raise RuntimeError("升正账实不符：草稿口状态或版本不一致")
    if row["rk"] != sh["kind"] or int(row["rp"]) != int(sh["percent"]):
        raise RuntimeError("升正账实不符：正式规则类型/比例与草稿不一致")
    if (row["rc"] if row["rc"] is not None else None) != (
        sh["cohort_id"] if sh["cohort_id"] is not None else None
    ):
        raise RuntimeError("升正账实不符：正式规则人群与草稿不一致")
    if count_audit(conn, "rule_promote", f"from_shadow={shadow_id}") != 1:
        raise RuntimeError("升正审计不是 exactly-once")
    if count_history(conn, "rule_promote", fid, eid) < 1:
        raise RuntimeError("升正变更历史未在同事务落账")


@router.post("/shadow/{shadow_id}/promote")
def promote_rule_shadow(shadow_id: int, authorization: str | None = Header(default=None)):
    """甲口径：升正写正式 与 同事务关闭草稿口 必须同时成立，否则整体回滚。

    并发同草稿口：原子条件 UPDATE（open->promoted, rowcount=1）作为核销点，
    败方拿 409 且只留一段争用痕，绝不二次落正式。
    """
    user = current_user(authorization)
    require_writer(user)
    conn = connect()
    key: str | None = None
    token: str | None = None
    try:
        sh = conn.execute("SELECT * FROM rule_shadows WHERE id=?", (shadow_id,)).fetchone()
        if not sh:
            raise HTTPException(404, detail="影子绑定不存在")
        fid, eid = sh["flag_id"], sh["env_id"]
        key = f"flag-rule:{fid}:{eid}"

        # 同草稿口 / 同格升正互斥锁；抢锁失败直接 409 + 争用痕。
        token = acquire(conn, key, user["username"])

        # 权威核销：只有仍 open 且条件更新命中 1 行的一路是赢家。
        claim = conn.execute(
            "UPDATE rule_shadows SET status='promoted' WHERE id=? AND status='open'",
            (shadow_id,),
        )
        if claim.rowcount != 1:
            winner = last_promoter(conn, shadow_id) or "committed-winner"
            add_contention_mark(conn, key, user["username"], winner)
            conn.commit()
            raise HTTPException(
                status_code=409,
                detail="草稿口已被消耗：该影子规则已升正，禁止二次落正式",
            )

        fl = conn.execute("SELECT * FROM flags WHERE id=?", (fid,)).fetchone()
        if not fl:
            raise HTTPException(404, detail="开关不存在")
        if not conn.execute("SELECT 1 FROM environments WHERE id=?", (eid,)).fetchone():
            raise HTTPException(404, detail="环境不存在")

        now = time.time()
        old = conn.execute(
            "SELECT * FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1 "
            "ORDER BY version DESC, id DESC LIMIT 1",
            (fid, eid),
        ).fetchone()
        ver = (old["version"] + 1) if old else 1
        # 健康库仅一行；按格清空旧正式，避免双正式撞唯一键 / 残留。
        conn.execute(
            "DELETE FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1",
            (fid, eid),
        )
        cur = conn.execute(
            "INSERT INTO flag_rules(flag_id, env_id, kind, percent, cohort_id, version, official, updated_at) "
            "VALUES(?,?,?,?,?,?,1,?)",
            (fid, eid, sh["kind"], sh["percent"], sh["cohort_id"], ver, now),
        )
        rid = cur.lastrowid
        if cur.rowcount != 1 or not rid:
            raise RuntimeError("正式规则零增：回滚升正与草稿口关闭")

        if fl["status"] == "draft":
            conn.execute("UPDATE flags SET status='active' WHERE id=?", (fid,))

        write_history(conn, user["username"], "rule_promote", f"影子规则升正 v{ver}", flag_id=fid, env_id=eid)
        write_audit(conn, user["username"], "rule_promote", "flag_rule", rid, f"from_shadow={shadow_id}")

        # 同事务联查跨表账实；不过则抛错回滚，草稿口不会被消耗。
        _cross_check_rule_promotion(conn, shadow_id, fid, eid, ver, sh)

        conn.commit()
        return {
            "id": rid,
            "version": ver,
            "mode": "official",
            "draft_status": "promoted",
            "gate_closed": True,
            "consumed": True,
            "official_count": 1,
            "cross_check": "ok",
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        if key is not None and token is not None:
            try:
                release(conn, key, token)
                conn.commit()
            except Exception:
                pass
        conn.close()
