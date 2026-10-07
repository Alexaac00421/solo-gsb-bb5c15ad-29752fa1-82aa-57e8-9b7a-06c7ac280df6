"""业务逻辑：全量发布（含冲突检测）、裁决、版本查询。

冲突定义（整次发布失败）：
两条规则满足全部条件即构成冲突——
  * 同一业务键；
  * 同一优先级；
  * 结论不同；
  * 地区可同时命中：相等或至少一方为通配；
  * 对象类别可同时命中：相等或至少一方为通配；
  * 半开有效时间段重叠（端点相接不算重叠）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from .config import WILDCARD
from .schemas import RuleIn

# ---------------------------------------------------------------- 异常

class RuleConflict(Exception):
    def __init__(self, pairs: list[dict[str, Any]]):
        super().__init__("规则冲突，发布失败")
        self.pairs = pairs


class VersionNotFound(Exception):
    def __init__(self, version: int):
        super().__init__(f"版本 {version} 不存在")
        self.version = version


class NoPublishedVersion(Exception):
    pass


# ---------------------------------------------------------------- SQL

_INSERT_VERSION = text(
    "INSERT INTO rule_versions (version, rule_count) VALUES (:version, :rule_count)"
)

_INSERT_RULE = text(
    """
    INSERT INTO rules
        (version, business_key, region, category,
         valid_from, valid_to, priority, conclusion)
    VALUES
        (:version, :business_key, :region, :category,
         :valid_from, :valid_to, :priority, :conclusion)
    RETURNING id
    """
)

# 仅在“本次新版本”的行内两两比较；a.id < b.id 保证每对只出现一次。
# 半开区间 [from, to) 重叠 <=> max(f1,f2) < min(t1,t2)，
# 即 f1 < t2 AND f2 < t1；valid_to 为 NULL 表示开放到无限远。
_CONFLICT_SQL = text(
    """
    SELECT
        a.id AS a_id, a.business_key AS a_business_key,
        a.region AS a_region, a.category AS a_category,
        a.valid_from AS a_valid_from, a.valid_to AS a_valid_to,
        a.priority AS a_priority, a.conclusion AS a_conclusion,
        b.id AS b_id, b.business_key AS b_business_key,
        b.region AS b_region, b.category AS b_category,
        b.valid_from AS b_valid_from, b.valid_to AS b_valid_to,
        b.priority AS b_priority, b.conclusion AS b_conclusion
    FROM rules a
    JOIN rules b
      ON  a.id < b.id
      AND a.business_key = b.business_key
      AND a.priority = b.priority
      AND a.conclusion IS DISTINCT FROM b.conclusion
      AND (a.region   IS NULL OR b.region   IS NULL OR a.region   = b.region)
      AND (a.category IS NULL OR b.category IS NULL OR a.category = b.category)
      AND a.valid_from < COALESCE(b.valid_to, TIMESTAMPTZ 'infinity')
      AND b.valid_from < COALESCE(a.valid_to, TIMESTAMPTZ 'infinity')
    WHERE a.version = :version AND b.version = :version
    ORDER BY a.id, b.id
    LIMIT 50
    """
)

_RESOLVE_VERSION_SQL = text(
    """
    SELECT version
    FROM rule_versions
    WHERE (CAST(:version AS BIGINT) IS NULL OR version = :version)
    ORDER BY version DESC
    LIMIT 1
    """
)

_MATCH_SQL = text(
    """
    SELECT id, business_key, region, category,
           valid_from, valid_to, priority, conclusion
    FROM rules
    WHERE version = :version
      AND business_key = :business_key
      AND (region   IS NULL OR region   = :region)
      AND (category IS NULL OR category = :category)
      AND valid_from <= :occurred_at
      AND (valid_to IS NULL OR valid_to > :occurred_at)
    ORDER BY priority DESC, id
    """
)

_LIST_VERSIONS_SQL = text(
    "SELECT version, rule_count, created_at FROM rule_versions ORDER BY version DESC"
)

_VERSION_DETAIL_SQL = text(
    "SELECT version, rule_count, created_at FROM rule_versions WHERE version = :version"
)

_VERSION_RULES_SQL = text(
    """
    SELECT id, business_key, region, category,
           valid_from, valid_to, priority, conclusion
    FROM rules
    WHERE version = :version
    ORDER BY priority DESC, id
    """
)


# ---------------------------------------------------------------- 工具

def _rule_dict(mapping: Any, prefix: str = "") -> dict[str, Any]:
    """把带前缀（a_ / b_）或不带前缀的行映射整理为规则字典。"""
    return {
        "id": mapping[f"{prefix}id"],
        "business_key": mapping[f"{prefix}business_key"],
        "region": mapping[f"{prefix}region"],
        "category": mapping[f"{prefix}category"],
        "valid_from": mapping[f"{prefix}valid_from"],
        "valid_to": mapping[f"{prefix}valid_to"],
        "priority": mapping[f"{prefix}priority"],
        "conclusion": mapping[f"{prefix}conclusion"],
    }


def _present_region(value: str) -> Optional[str]:
    """通配 '*' 在数据库中存为 NULL（表示不限地区）。"""
    return None if value == WILDCARD else value


def _present_category(value: str) -> Optional[str]:
    return None if value == WILDCARD else value


def _dimensions_compatible(a: Optional[str], b: Optional[str]) -> bool:
    """地区/类别维度可同时命中：相等或至少一方为通配（None）。"""
    return a is None or b is None or a == b


def _time_ranges_overlap(
    fa: datetime, ta: Optional[datetime], fb: datetime, tb: Optional[datetime]
) -> bool:
    """半开区间 [fa, ta) 与 [fb, tb) 是否重叠。

    重叠 <=> max(fa, fb) < min(ta, tb)，即 fa < tb 且 fb < ta；
    端点相接（ta == fb）不算重叠；None 终点视为 +∞。
    """
    if tb is not None and not fa < tb:
        return False
    if ta is not None and not fb < ta:
        return False
    return True


def find_conflicts(rules: list[RuleIn]) -> list[dict[str, Any]]:
    """在发布写入前做内存冲突预检，避免冲突发布消耗版本号。

    与 SQL 兜底检测的语义完全一致：同业务键、同优先级、不同结论，
    且地区、类别、半开时间段均可同时命中。
    """
    pairs: list[dict[str, Any]] = []
    for i in range(len(rules)):
        a = rules[i]
        ra, ca = _present_region(a.region), _present_category(a.category)
        for j in range(i + 1, len(rules)):
            b = rules[j]
            if a.business_key != b.business_key or a.priority != b.priority:
                continue
            if a.conclusion == b.conclusion:
                continue
            rb, cb = _present_region(b.region), _present_category(b.category)
            if not _dimensions_compatible(ra, rb):
                continue
            if not _dimensions_compatible(ca, cb):
                continue
            if not _time_ranges_overlap(
                a.valid_from, a.valid_to, b.valid_from, b.valid_to
            ):
                continue
            pairs.append({"rule1": _conflict_rule_dump(a),
                          "rule2": _conflict_rule_dump(b)})
            if len(pairs) >= 50:
                return pairs
    return pairs


def _conflict_rule_dump(rule: RuleIn) -> dict[str, Any]:
    """冲突规则的对外表示：通配以 null 表示（与库中、与裁决返回一致）。"""
    data = rule.model_dump(mode="json")
    data["region"] = _present_region(rule.region)
    data["category"] = _present_category(rule.category)
    data.pop("id", None)  # 预检阶段规则尚未写入，没有 id
    return data


# ---------------------------------------------------------------- 发布

async def publish_rules(
    conn: AsyncConnection, rules: list[RuleIn]
) -> tuple[int, int]:
    """在调用方给定的事务中全量写入规则集并做冲突检测。

    先做内存预检：冲突时直接抛出 RuleConflict，不消耗版本号；
    随后在同一事务内写入并以 SQL 自连接兜底复核（防御性），
    冲突时整个事务回滚，不会留下任何部分可见的数据。
    """
    # 1) 写入前预检：不触碰序列，失败的发布不产生版本空号
    pre_conflicts = find_conflicts(rules)
    if pre_conflicts:
        raise RuleConflict(pre_conflicts)

    # 2) 分配版本号并写入
    version_row = await conn.execute(text("SELECT nextval('rule_version_id_seq')"))
    version = version_row.scalar_one()

    await conn.execute(
        _INSERT_VERSION, {"version": version, "rule_count": len(rules)}
    )

    for rule in rules:
        await conn.execute(
            _INSERT_RULE,
            {
                "version": version,
                "business_key": rule.business_key,
                "region": _present_region(rule.region),
                "category": _present_category(rule.category),
                "valid_from": rule.valid_from,
                "valid_to": rule.valid_to,
                "priority": rule.priority,
                "conclusion": rule.conclusion,
            },
        )

    conflict_rows = (await conn.execute(_CONFLICT_SQL, {"version": version})).mappings().all()
    if conflict_rows:
        pairs = [
            {"rule1": _rule_dict(row, "a_"), "rule2": _rule_dict(row, "b_")}
            for row in conflict_rows
        ]
        raise RuleConflict(pairs)

    return version, len(rules)


# ---------------------------------------------------------------- 裁决

async def resolve_version(conn: AsyncConnection, version: Optional[int]) -> int:
    """version 为 None 时取当前事务快照内可见的最新已发布版本。

    调用方使用 REPEATABLE READ：快照在第一条语句时固定，
    因此并发提交的新版本不会被本次裁决随后看到——裁决全程只使用一个版本。
    """
    row = (
        await conn.execute(_RESOLVE_VERSION_SQL, {"version": version})
    ).mappings().first()
    if row is None:
        if version is None:
            raise NoPublishedVersion()
        raise VersionNotFound(version)
    return int(row["version"])


async def match_rules(
    conn: AsyncConnection,
    version: int,
    business_key: str,
    region: str,
    category: str,
    occurred_at: datetime,
) -> list[dict[str, Any]]:
    params = {
        "version": version,
        "business_key": business_key,
        "region": region,
        "category": category,
        "occurred_at": occurred_at,
    }
    rows = (await conn.execute(_MATCH_SQL, params)).mappings().all()
    return [_rule_dict(row) for row in rows]


async def adjudicate(
    conn: AsyncConnection,
    business_key: str,
    region: str,
    category: str,
    occurred_at: datetime,
    version: Optional[int],
) -> dict[str, Any]:
    used_version = await resolve_version(conn, version)
    hits = await match_rules(
        conn, used_version, business_key, region, category, occurred_at
    )
    if not hits:
        return {
            "version": used_version,
            "covered": False,
            "priority": None,
            "conclusion": "未覆盖",
            "matched_rules": [],
        }
    top_priority = hits[0]["priority"]  # SQL 已按 priority DESC 排序
    top_hits = [h for h in hits if h["priority"] == top_priority]
    conclusions = sorted({h["conclusion"] for h in top_hits})
    # 同一最高优先级多条命中：发布时已保证同优先级不同结论不可能同时命中，
    # 因此这里结论必然唯一；多条规则给同一结论时返回该结论及全部命中规则。
    return {
        "version": used_version,
        "covered": True,
        "priority": top_priority,
        "conclusion": conclusions[0],
        "matched_rules": top_hits,
    }


# ---------------------------------------------------------------- 版本查询

async def list_versions(conn: AsyncConnection) -> list[dict[str, Any]]:
    rows = (await conn.execute(_LIST_VERSIONS_SQL)).mappings().all()
    return [dict(row) for row in rows]


async def get_version(conn: AsyncConnection, version: int) -> dict[str, Any]:
    row = (
        await conn.execute(_VERSION_DETAIL_SQL, {"version": version})
    ).mappings().first()
    if row is None:
        raise VersionNotFound(version)
    rules = (
        await conn.execute(_VERSION_RULES_SQL, {"version": version})
    ).mappings().all()
    result = dict(row)
    result["rules"] = [_rule_dict(r) for r in rules]
    return result
