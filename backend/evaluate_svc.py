"""在线求值：给定主体键，返回开关在某环境是否命中。"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from auth import current_user
from db import connect
from policies import combine_rule, eval_percent
from validators import require_str

router = APIRouter(prefix="/api/evaluate", tags=["evaluate"])


class EvalIn(BaseModel):
    flag_key: str
    env_code: str
    subject_key: str


@router.post("")
def evaluate(body: EvalIn, authorization: str | None = Header(default=None)):
    current_user(authorization)
    flag_key = require_str(body.flag_key, "开关键", 2)
    env_code = require_str(body.env_code, "环境", 2)
    subject = require_str(body.subject_key, "主体键", 1)
    with connect() as conn:
        fl = conn.execute("SELECT * FROM flags WHERE key=?", (flag_key,)).fetchone()
        if not fl:
            raise HTTPException(404, detail="开关不存在")
        env = conn.execute("SELECT * FROM environments WHERE code=?", (env_code,)).fetchone()
        if not env:
            raise HTTPException(404, detail="环境不存在")
        if fl["status"] == "killed":
            return {
                "enabled": False,
                "reason": "flag_killed",
                "flag_key": flag_key,
                "env_code": env_code,
                "subject_key": subject,
            }
        if fl["status"] == "archived":
            return {
                "enabled": False,
                "reason": "flag_archived",
                "flag_key": flag_key,
                "env_code": env_code,
                "subject_key": subject,
            }
        ov = conn.execute(
            "SELECT * FROM overrides WHERE flag_id=? AND env_id=? AND subject_key=? AND active=1",
            (fl["id"], env["id"], subject),
        ).fetchone()
        if ov:
            enabled = ov["effect"] == "force_on"
            return {
                "enabled": enabled,
                "reason": f"override:{ov['effect']}",
                "flag_key": flag_key,
                "env_code": env_code,
                "subject_key": subject,
                "version": None,
            }
        # 不能裸 fetchone()：一旦账上异常存在双正式版本，无 ORDER BY 会随 rowid
        # 取到旧版，两个互相矛盾的断言会各绿各的（gbf04 假绿）。这里全量取出，
        # 确定性选最高版本（同版本取最大 id）为 canonical，并如实上报冲突。
        rules = conn.execute(
            "SELECT * FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1 "
            "ORDER BY version DESC, id DESC",
            (fl["id"], env["id"]),
        ).fetchall()
        official_count = len(rules)
        dual_official = official_count > 1
        consistency = {
            "official_count": official_count,
            "consistent": official_count <= 1,
            "dual_official": dual_official,
            "conflict": "multiple_official_versions" if dual_official else None,
            "official_versions": [int(r["version"]) for r in rules],
        }
        rule = rules[0] if rules else None
        if not rule:
            return {
                "enabled": False,
                "reason": "no_official_rule",
                "flag_key": flag_key,
                "env_code": env_code,
                "subject_key": subject,
                **consistency,
            }
        percent_hit = eval_percent(subject, int(rule["percent"]), flag_key)
        cohort_hit = False
        if rule["cohort_id"]:
            cohort_hit = bool(
                conn.execute(
                    "SELECT 1 FROM cohort_members WHERE cohort_id=? AND subject_key=?",
                    (rule["cohort_id"], subject),
                ).fetchone()
            )
        enabled = combine_rule(rule["kind"], percent_hit, cohort_hit)
        return {
            "enabled": enabled,
            "reason": "rule",
            "kind": rule["kind"],
            "percent": rule["percent"],
            "percent_hit": percent_hit,
            "cohort_hit": cohort_hit,
            "version": rule["version"],
            "flag_key": flag_key,
            "env_code": env_code,
            "subject_key": subject,
            **consistency,
        }


@router.get("/board")
def eval_board(authorization: str | None = Header(default=None)):
    """总览：各环境正式规则摘要。"""
    current_user(authorization)
    with connect() as conn:
        raw_rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT f.id AS flag_id, e.id AS env_id,
                       f.key AS flag_key, f.status, e.code AS env_code,
                       r.kind, r.percent, r.version, r.cohort_id
                FROM flag_rules r
                JOIN flags f ON f.id=r.flag_id
                JOIN environments e ON e.id=r.env_id
                WHERE r.official=1
                ORDER BY f.key, e.risk_level, r.version DESC, r.id DESC
                """
            ).fetchall()
        ]
        by_status = [
            dict(x)
            for x in conn.execute("SELECT status, COUNT(*) c FROM flags GROUP BY status").fetchall()
        ]

    # 同一 (flag, env) 若有多行 official=1，只暴露 canonical（最高版本），
    # 同时把冲突单列出来，禁止 board 用两行互相打架的“绿”掩盖账实不符。
    grouped: dict[tuple, dict] = {}
    order: list[tuple] = []
    for row in raw_rows:
        k = (row["flag_key"], row["env_code"])
        if k not in grouped:
            grouped[k] = {"canonical": row, "versions": []}
            order.append(k)
        grouped[k]["versions"].append(int(row["version"]))

    rules = []
    conflicts = []
    for k in order:
        g = grouped[k]
        canonical = dict(g["canonical"])
        versions = g["versions"]
        count = len(versions)
        canonical["official_count"] = count
        canonical["consistent"] = count <= 1
        rules.append(canonical)
        if count > 1:
            conflicts.append(
                {
                    "flag_key": k[0],
                    "env_code": k[1],
                    "official_count": count,
                    "versions": versions,
                    "canonical_version": canonical["version"],
                    "conflict": "multiple_official_versions",
                }
            )
    return {
        "rules": rules,
        "by_status": by_status,
        "conflicts": conflicts,
        "official_conflict_count": len(conflicts),
    }
