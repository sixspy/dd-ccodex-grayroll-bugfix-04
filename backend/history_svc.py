"""变更历史。"""
from __future__ import annotations

from fastapi import APIRouter, Header

from auth import current_user
from db import connect

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("")
def list_history(
    authorization: str | None = Header(default=None),
    limit: int = 100,
    flag_id: int | None = None,
    env_id: int | None = None,
    action: str | None = None,
):
    current_user(authorization)
    where = []
    args: list = []
    if flag_id is not None:
        where.append("h.flag_id=?")
        args.append(flag_id)
    if env_id is not None:
        where.append("h.env_id=?")
        args.append(env_id)
    if action is not None:
        where.append("h.action=?")
        args.append(action)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    args.append(limit)
    with connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT h.*, f.key AS flag_key, e.code AS env_code
                FROM change_history h
                LEFT JOIN flags f ON f.id=h.flag_id
                LEFT JOIN environments e ON e.id=h.env_id
                {clause}
                ORDER BY h.id DESC
                LIMIT ?
                """,
                args,
            ).fetchall()
        ]
    return {"items": rows}
