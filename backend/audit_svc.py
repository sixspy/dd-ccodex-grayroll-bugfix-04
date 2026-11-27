"""审计与变更历史写入。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Header

from auth import current_user
from db import connect

router = APIRouter(prefix="/api/audit", tags=["audit"])


def write_audit(conn, actor: str, action: str, entity: str, entity_id: int | None, detail: str) -> None:
    conn.execute(
        "INSERT INTO audit_log(actor, action, entity, entity_id, detail, created_at) VALUES(?,?,?,?,?,?)",
        (actor, action, entity, entity_id, detail, time.time()),
    )


def write_history(conn, actor: str, action: str, detail: str, flag_id: int | None = None, env_id: int | None = None) -> None:
    conn.execute(
        "INSERT INTO change_history(flag_id, env_id, action, detail, actor, created_at) VALUES(?,?,?,?,?,?)",
        (flag_id, env_id, action, detail, actor, time.time()),
    )


def count_audit(conn, action: str, detail: str | None = None) -> int:
    """同一事务内联查审计条数（升正 exactly-once 账实核对用）。"""
    if detail is None:
        row = conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE action=?", (action,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE action=? AND detail=?", (action, detail)
        ).fetchone()
    return int(row["c"])


def count_history(
    conn, action: str, flag_id: int | None = None, env_id: int | None = None
) -> int:
    sql = "SELECT COUNT(*) c FROM change_history WHERE action=?"
    args: list = [action]
    if flag_id is not None:
        sql += " AND flag_id=?"
        args.append(flag_id)
    if env_id is not None:
        sql += " AND env_id=?"
        args.append(env_id)
    return int(conn.execute(sql, args).fetchone()["c"])


def last_promoter(conn, shadow_id: int) -> str | None:
    """从升正审计里取该草稿口最近一次成功升正的操作人（争用痕 winner 兜底）。"""
    row = conn.execute(
        "SELECT actor FROM audit_log WHERE action LIKE '%promote' AND detail=? "
        "ORDER BY id DESC LIMIT 1",
        (f"from_shadow={shadow_id}",),
    ).fetchone()
    return row["actor"] if row else None


@router.get("")
def list_audit(authorization: str | None = Header(default=None), limit: int = 80):
    current_user(authorization)
    with connect() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"items": [dict(r) for r in rows]}
