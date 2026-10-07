#!/usr/bin/env python3
"""政策裁决服务调用示例 / 冒烟测试（仅依赖 Python 标准库）。

用法：
    python examples/demo_client.py                 # 默认 http://localhost:8000
    BASE_URL=http://127.0.0.1:8000 python examples/demo_client.py

依次演示：发布 -> 裁决（优先级/通配/半开区间边界/未覆盖）->
冲突发布失败 -> 非法时间段报错 -> 字段缺失报错 ->
发布更正版本 -> 最新版本裁决 -> 指定旧版本重放 -> 版本不存在报错。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
HERE = Path(__file__).resolve().parent

failures: list[str] = []


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def step(title: str) -> None:
    print(f"\n=== {title}")


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f" :: {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def load(fname: str) -> dict:
    return json.loads((HERE / fname).read_text(encoding="utf-8"))


def show(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def adjudicate(region: str, category: str, at: str, key: str = "loan_approval",
               version: int | None = None):
    body: dict = {
        "business_key": key,
        "region": region,
        "category": category,
        "occurred_at": at,
    }
    if version is not None:
        body["version"] = version
    status, resp = call("POST", "/api/v1/adjudicate", body)
    return status, resp


def main() -> int:
    # 0. 健康检查
    step("健康检查 GET /healthz")
    status, resp = call("GET", "/healthz")
    check("healthz 200", status == 200, str(resp))

    # 1. 发布 v1
    step("发布 v1 完整规则集 POST /api/v1/publish")
    status, resp = call("POST", "/api/v1/publish", load("rules_v1.json"))
    show(resp)
    check("发布返回 201 与 version=1", status == 201 and resp.get("version") == 1)
    check("rule_count=3", resp.get("rule_count") == 3)

    # 2. 裁决：具体地区规则（p100）压过通配规则（p10）
    step("裁决 BJ / personal / 2026-06-01（具体规则优先）")
    status, resp = adjudicate("BJ", "personal", "2026-06-01T08:00:00Z")
    show(resp)
    check("200 且 covered", status == 200 and resp.get("covered") is True)
    check("结论 approve，优先级 100", resp.get("conclusion") == "approve"
          and resp.get("priority") == 100)
    check("命中 1 条规则且属于版本 1",
          len(resp.get("matched_rules", [])) == 1 and resp.get("version") == 1)

    # 3. 不同地区结论不同（互斥，不构成冲突）
    step("裁决 SH / personal / 2026-06-01")
    status, resp = adjudicate("SH", "personal", "2026-06-01T08:00:00Z")
    show(resp)
    check("结论 reject", status == 200 and resp.get("conclusion") == "reject")

    # 4. 通配：未列地区 -> 通配规则
    step("裁决 GZ / personal（仅通配命中）")
    status, resp = adjudicate("GZ", "personal", "2026-06-01T08:00:00Z")
    show(resp)
    check("结论 manual_review，优先级 10",
          status == 200 and resp.get("conclusion") == "manual_review"
          and resp.get("priority") == 10)

    # 5. 通配：类别未列 -> 通配规则
    step("裁决 BJ / enterprise（类别通配命中）")
    status, resp = adjudicate("BJ", "enterprise", "2026-06-01T08:00:00Z")
    show(resp)
    check("结论 manual_review", status == 200
          and resp.get("conclusion") == "manual_review")

    # 6. 半开区间边界：左闭
    step("半开区间边界 2026-01-01T00:00:00Z（恰好等于 valid_from，命中）")
    status, resp = adjudicate("BJ", "personal", "2026-01-01T00:00:00Z")
    show(resp)
    check("左边界命中 approve", status == 200 and resp.get("conclusion") == "approve")

    # 7. 半开区间边界：右开
    step("半开区间边界 2027-01-01T00:00:00Z（恰好等于 valid_to，不命中）")
    status, resp = adjudicate("BJ", "personal", "2027-01-01T00:00:00Z", version=1)
    show(resp)
    check("右边界未覆盖", status == 200 and resp.get("covered") is False
          and resp.get("conclusion") == "未覆盖")

    # 8. 未覆盖：未知业务键
    step("未知业务键 -> 未覆盖")
    status, resp = adjudicate("BJ", "personal", "2026-06-01T00:00:00Z",
                              key="unknown_key")
    show(resp)
    check("covered=false", status == 200 and resp.get("covered") is False)

    # 9. 冲突发布：同业务键/同优先级/不同结论，地区类别时间均可同时命中
    step("发布冲突规则集（必须整次失败，返回冲突规则）")
    status, resp = call("POST", "/api/v1/publish", load("rules_conflict.json"))
    show(resp)
    check("HTTP 409", status == 409)
    check("错误码 RULE_CONFLICT", resp.get("error", {}).get("code") == "RULE_CONFLICT")
    check("返回冲突规则对", len(resp.get("conflicts", [])) >= 1
          and {"rule1", "rule2"} <= set(resp["conflicts"][0]))

    # 冲突发布失败后不应产生新版本
    status, resp = call("GET", "/api/v1/versions")
    check("冲突发布未留下新版本", [v["version"] for v in resp["versions"]] == [1],
          str(resp["versions"]))

    # 10. 非法时间段（valid_from >= valid_to）
    step("非法时间段 valid_from >= valid_to -> 422")
    bad = {"rules": [{
        "business_key": "loan_approval", "region": "BJ", "category": "personal",
        "valid_from": "2026-12-01T00:00:00Z",
        "valid_to": "2026-12-01T00:00:00Z",
        "priority": 1, "conclusion": "x",
    }]}
    status, resp = call("POST", "/api/v1/publish", bad)
    show(resp)
    check("HTTP 422", status == 422)

    # 11. 字段缺失
    step("字段缺失（缺少 region）-> 422")
    status, resp = call("POST", "/api/v1/adjudicate", {
        "business_key": "loan_approval",
        "category": "personal",
        "occurred_at": "2026-06-01T00:00:00Z",
    })
    show(resp)
    check("HTTP 422", status == 422)

    step("发布缺少 rules 字段 -> 422")
    status, resp = call("POST", "/api/v1/publish", {"rule_set": []})
    check("HTTP 422", status == 422)

    # 12. 发布 v2（更正：BJ personal 自 2027-01-01 起新政策，与旧规则端点相接不冲突）
    step("发布 v2 更正后的完整规则集")
    status, resp = call("POST", "/api/v1/publish", load("rules_v2.json"))
    show(resp)
    check("发布返回 version=2", status == 201 and resp.get("version") == 2)

    # 13. 未指定版本 -> 固定为最新（v2）
    step("不指定版本裁决（应使用请求时最新版本 v2）")
    status, resp = adjudicate("BJ", "personal", "2027-06-01T00:00:00Z")
    show(resp)
    check("使用 v2 且结论 approve_new_policy(p50)",
          status == 200 and resp.get("version") == 2
          and resp.get("conclusion") == "approve_new_policy"
          and resp.get("priority") == 50)

    # 14. 指定旧版本重放：同样输入在 v1 上结果不同且保持不变
    step("指定 version=1 重放同一输入（旧版本结果不变）")
    status, resp = adjudicate("BJ", "personal", "2027-06-01T00:00:00Z", version=1)
    show(resp)
    check("v1 重放仍为未覆盖",
          status == 200 and resp.get("version") == 1
          and resp.get("covered") is False)

    # 15. 版本不存在
    step("裁决指定不存在版本 999 -> 404")
    status, resp = adjudicate("BJ", "personal", "2026-06-01T00:00:00Z", version=999)
    show(resp)
    check("404 VERSION_NOT_FOUND",
          status == 404 and resp.get("error", {}).get("code") == "VERSION_NOT_FOUND")

    status, resp = call("GET", "/api/v1/versions/999")
    check("GET 不存在版本同样 404", status == 404)

    print("\n==================================================")
    if failures:
        print(f"存在 {len(failures)} 个失败检查：{failures}")
        return 1
    print("全部检查通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
