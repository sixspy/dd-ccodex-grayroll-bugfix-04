"""控制台摘要。"""
from __future__ import annotations

from fastapi import APIRouter, Header

from auth import current_user
from db import connect
from models import FLAG_STATUS_LABEL

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("")
def dashboard(authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        flags = conn.execute("SELECT COUNT(*) c FROM flags").fetchone()["c"]
        active = conn.execute("SELECT COUNT(*) c FROM flags WHERE status='active'").fetchone()["c"]
        killed = conn.execute("SELECT COUNT(*) c FROM flags WHERE status='killed'").fetchone()["c"]
        releases = conn.execute("SELECT COUNT(*) c FROM releases WHERE official=1").fetchone()["c"]
        open_release_shadows = conn.execute(
            "SELECT COUNT(*) c FROM release_shadows WHERE status='open'"
        ).fetchone()["c"]
        open_rule_shadows = conn.execute(
            "SELECT COUNT(*) c FROM rule_shadows WHERE status='open'"
        ).fetchone()["c"]
        open_kill_shadows = conn.execute(
            "SELECT COUNT(*) c FROM kill_shadows WHERE status='open'"
        ).fetchone()["c"]
        # 总览“开放草稿口”必须把规则/发布/熔断三类影子口都算上；升正核销后即下降，
        # 不能只数发布影子，否则规则草稿口仍开放时总览也假绿。
        shadows = open_release_shadows + open_rule_shadows + open_kill_shadows
        contention_recent = conn.execute(
            "SELECT COUNT(*) c FROM contention_marks"
        ).fetchone()["c"]
        overrides = conn.execute("SELECT COUNT(*) c FROM overrides WHERE active=1").fetchone()["c"]
        by_status = [
            {
                "status": r["status"],
                "label": FLAG_STATUS_LABEL.get(r["status"], r["status"]),
                "c": r["c"],
            }
            for r in conn.execute("SELECT status, COUNT(*) c FROM flags GROUP BY status").fetchall()
        ]
        recent = [
            dict(r)
            for r in conn.execute(
                "SELECT id, action, detail, actor, created_at FROM change_history ORDER BY id DESC LIMIT 8"
            ).fetchall()
        ]
    return {
        "flags": flags,
        "active": active,
        "killed": killed,
        "releases": releases,
        "open_shadows": shadows,
        "open_draft_gates": shadows,
        "open_rule_shadows": open_rule_shadows,
        "open_release_shadows": open_release_shadows,
        "open_kill_shadows": open_kill_shadows,
        "contention_marks": contention_recent,
        "active_overrides": overrides,
        "by_status": by_status,
        "recent": recent,
    }
