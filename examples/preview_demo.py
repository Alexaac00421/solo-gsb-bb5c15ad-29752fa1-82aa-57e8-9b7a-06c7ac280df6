#!/usr/bin/env python3
"""发布预览接口调用示例 / 冒烟测试（仅依赖 Python 标准库）。

用法：
    python examples/preview_demo.py                 # 默认 http://localhost:8000
    BASE_URL=http://127.0.0.1:8000 python examples/preview_demo.py

依次演示：无基准报错（仅空库时）-> 发布 v1 -> 指定基准版本预览 ->
省略基准版本（取请求开始时最新版本）-> 仅优先级不同不算变化 ->
候选集冲突整次拒绝（无部分样例结果）-> 基准版本不存在 ->
候选规则时间段非法 -> 样例字段缺失 -> 预览不写入/不消耗版本号。
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


def preview(body: dict):
    return call("POST", "/api/v1/preview", body)


def main() -> int:
    # 0. 健康检查
    step("健康检查 GET /healthz")
    status, resp = call("GET", "/healthz")
    check("healthz 200", status == 200, str(resp))

    # 1. 空库时：尚无可用基准 -> 409 NO_PUBLISHED_VERSION
    step("尚无已发布版本时预览（仅空库可验证）")
    status, resp = call("GET", "/api/v1/versions")
    if status == 200 and not resp.get("versions"):
        status, resp = preview(load("preview_request.json"))
        show(resp)
        check("409 NO_PUBLISHED_VERSION",
              status == 409
              and resp.get("error", {}).get("code") == "NO_PUBLISHED_VERSION")
    else:
        print("  [SKIP] 已有已发布版本，跳过该检查")

    # 2. 发布 v1 作为基准
    step("发布 v1 完整规则集 POST /api/v1/publish")
    status, resp = call("POST", "/api/v1/publish", load("rules_v1.json"))
    show(resp)
    check("发布返回 201 与 version=1", status == 201 and resp.get("version") == 1)

    # 3. 指定基准版本预览：候选集 = v2 更正规则集
    step("指定 base_version=1 预览 POST /api/v1/preview")
    status, resp = preview(load("preview_request.json"))
    show(resp)
    results = resp.get("results", [])
    check("200 且 base_version=1", status == 200 and resp.get("base_version") == 1)
    check("按输入顺序返回 4 个样例结果",
          [r.get("index") for r in results] == [0, 1, 2, 3], str(results))
    if len(results) == 4:
        # 样例 0：两侧均为 approve(p100) -> 不变
        r0 = results[0]
        check("样例0 两侧 approve 且未标记变化",
              r0["baseline"] == {"covered": True, "priority": 100,
                                 "conclusion": "approve"}
              and r0["candidate"]["conclusion"] == "approve"
              and r0["changed"] is False, str(r0))
        # 样例 1：基准未覆盖，候选 approve_new_policy(p50) -> 变化
        r1 = results[1]
        check("样例1 基准未覆盖/候选覆盖 -> 变化",
              r1["baseline"] == {"covered": False, "priority": None,
                                 "conclusion": "未覆盖"}
              and r1["candidate"] == {"covered": True, "priority": 50,
                                      "conclusion": "approve_new_policy"}
              and r1["changed"] is True, str(r1))
        # 样例 2/3：两侧一致 -> 不变
        check("样例2 两侧 manual_review 不变",
              results[2]["baseline"]["conclusion"] == "manual_review"
              and results[2]["changed"] is False, str(results[2]))
        check("样例3 两侧 reject 不变",
              results[3]["baseline"]["conclusion"] == "reject"
              and results[3]["changed"] is False, str(results[3]))

    # 4. 省略基准版本 -> 以请求开始时最新已发布版本（v1）为基准
    step("省略 base_version（应取请求开始时最新版本 v1）")
    body = load("preview_request.json")
    body.pop("base_version")
    status, resp = preview(body)
    check("base_version 解析为 1 且结果一致",
          status == 200 and resp.get("base_version") == 1
          and [r["changed"] for r in resp.get("results", [])]
          == [False, True, False, False])

    # 5. 仅优先级不同（覆盖状态与结论均不变）-> 不标记变化
    step("候选集仅调整优先级 -> 不标记变化")
    body = {
        "rules": [
            {**r, "priority": 200} if r["region"] == "BJ" else r
            for r in load("rules_v1.json")["rules"]
        ],
        "samples": [{
            "business_key": "loan_approval", "region": "BJ",
            "category": "personal", "occurred_at": "2026-06-01T08:00:00Z",
        }],
    }
    status, resp = preview(body)
    show(resp)
    r = resp.get("results", [{}])[0]
    check("两侧结论一致、优先级 100->200、changed=false",
          status == 200
          and r.get("baseline", {}).get("priority") == 100
          and r.get("candidate", {}).get("priority") == 200
          and r.get("changed") is False, str(r))

    # 6. 候选集冲突 -> 整次拒绝，不产生任何样例结果
    step("候选集冲突 -> 409 RULE_CONFLICT 且无样例结果")
    status, resp = preview({**load("rules_conflict.json"), "samples": [
        {"business_key": "loan_approval", "region": "BJ",
         "category": "personal", "occurred_at": "2026-06-01T08:00:00Z"},
    ]})
    show(resp)
    check("HTTP 409 且错误码 RULE_CONFLICT",
          status == 409 and resp.get("error", {}).get("code") == "RULE_CONFLICT")
    check("返回冲突规则对且无 results",
          len(resp.get("conflicts", [])) >= 1 and "results" not in resp)

    # 7. 基准版本不存在 -> 404 VERSION_NOT_FOUND
    step("base_version=999 -> 404 VERSION_NOT_FOUND")
    body = load("preview_request.json")
    body["base_version"] = 999
    status, resp = preview(body)
    show(resp)
    check("404 VERSION_NOT_FOUND",
          status == 404 and resp.get("error", {}).get("code") == "VERSION_NOT_FOUND")

    # 8. 候选规则时间段非法 -> 422
    step("候选规则 valid_from >= valid_to -> 422")
    status, resp = preview({"rules": [{
        "business_key": "loan_approval", "region": "BJ", "category": "personal",
        "valid_from": "2026-12-01T00:00:00Z", "valid_to": "2026-12-01T00:00:00Z",
        "priority": 1, "conclusion": "x",
    }], "samples": []})
    check("HTTP 422 VALIDATION_ERROR",
          status == 422 and resp.get("error", {}).get("code") == "VALIDATION_ERROR")

    # 9. 样例字段缺失 -> 422
    step("样例缺少 occurred_at -> 422")
    status, resp = preview({"rules": [], "samples": [
        {"business_key": "loan_approval", "region": "BJ", "category": "personal"},
    ]})
    check("HTTP 422 VALIDATION_ERROR",
          status == 422 and resp.get("error", {}).get("code") == "VALIDATION_ERROR")

    # 10. 预览不写入规则/版本、不消耗版本号：版本列表仍只有 v1
    step("预览后版本列表不变（未写入、未消耗版本号）")
    status, resp = call("GET", "/api/v1/versions")
    check("版本仍只有 v1", [v["version"] for v in resp.get("versions", [])] == [1],
          str(resp.get("versions")))
    status, resp = call("GET", "/api/v1/versions/1")
    check("v1 规则数仍为 3", status == 200 and resp.get("rule_count") == 3)

    print("\n==================================================")
    if failures:
        print(f"存在 {len(failures)} 个失败检查：{failures}")
        return 1
    print("全部检查通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
