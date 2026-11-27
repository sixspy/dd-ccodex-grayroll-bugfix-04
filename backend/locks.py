"""行锁与争用痕（嵌进升正 / 发布 / 熔断主写路径）。

gbf04 抢修：同草稿口升正互斥。
- acquire 用单条原子 UPSERT 抢锁，靠 owner_token 区分同账号的两个并发会话，
  修掉原先“先 SELECT 再 UPSERT + 无条件 ON CONFLICT 覆盖 holder”的 check-then-act
  破窗（两路同 admin 会互相让锁 / 抢锁，最终无锁双成）。
- 抢锁成功立即 commit，让锁行对其它连接立即可见，锁是真互斥量，不再随业务事务
  直到方法末尾才提交。
- 败方只写一段 contention_marks 争用痕并抛 409，绝不继续写正式 / 历史 / 审计成功账。
"""
from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Header, HTTPException

from auth import current_user
from db import connect

router = APIRouter(prefix="/api/locks", tags=["locks"])
TTL = 20.0


def add_contention_mark(conn, key: str, loser: str, winner: str) -> None:
    """落一段争用痕（只 INSERT，不自行 commit；由调用方决定事务边界）。"""
    conn.execute(
        "INSERT INTO contention_marks(lock_key, loser, winner, created_at) VALUES(?,?,?,?)",
        (key, loser, winner or "unknown", time.time()),
    )


def acquire(conn, key: str, holder: str, ttl: float = TTL) -> str:
    """原子抢锁，返回本次占用的唯一 owner_token。

    同一 holder 名（如两个 admin 会话）的两次并发调用拿到不同 token，因此仍互斥。
    锁行立即提交可见。抢锁失败：写且仅写一段争用痕并抛 409。
    """
    token = uuid.uuid4().hex
    now = time.time()
    conn.execute(
        """
        INSERT INTO row_locks(lock_key, holder, owner_token, expires_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(lock_key) DO UPDATE SET
            holder = excluded.holder,
            owner_token = excluded.owner_token,
            expires_at = excluded.expires_at
        WHERE row_locks.expires_at <= ?
        """,
        (key, holder, token, now + ttl, now),
    )
    # 立即发布锁，使并发连接的原子 UPSERT 能据此判定冲突。
    conn.commit()

    row = conn.execute(
        "SELECT holder, owner_token, expires_at FROM row_locks WHERE lock_key=?",
        (key,),
    ).fetchone()
    if row is not None and row["owner_token"] == token:
        return token

    # 败方：仍有未过期的异主锁。只留争用痕。
    winner = row["holder"] if row is not None else "unknown"
    try:
        conn.rollback()
    except Exception:
        pass
    add_contention_mark(conn, key, holder, winner)
    conn.commit()
    raise HTTPException(
        status_code=409,
        detail="同草稿口升正互斥：另一路正在升正，本路判败，仅记争用痕",
    )


def release(conn, key: str, owner: str) -> None:
    """释放本路锁。owner 传 acquire 返回的 token；兼容旧调用传 holder 名。"""
    conn.execute(
        "DELETE FROM row_locks WHERE lock_key=? AND (owner_token=? OR holder=?)",
        (key, owner, owner),
    )


@router.get("/contention")
def list_contention(authorization: str | None = Header(default=None), limit: int = 40):
    current_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_marks ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return {"items": [dict(r) for r in rows]}
