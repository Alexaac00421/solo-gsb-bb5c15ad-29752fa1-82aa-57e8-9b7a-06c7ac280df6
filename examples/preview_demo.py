#!/usr/bin/env python3
"""发布预览（POST /api/v1/preview）调用示例 / 冒烟测试（仅依赖标准库）。

用法：
    python examples/preview_demo.py                 # 默认 http://localhost:8000
    BASE_URL=http://127.0.0.1:8000 python examples/preview_demo.py

依次演示：发布 v1 基准 -> 预览候选集（双侧覆盖状态/结论/优先级与变化标记，
含“仅优先级变化不算变化”）-> 候选集冲突整次拒绝（409）-> 指定基准版本 ->
基准版本不存在（404）-> 预览不消耗版本号 -> 空样例列表（422）。

建议在干净环境运行（脚本会自行发布 v1；若库中已有更新版本，
未指定基准的预览将以最新版本为准，相关断言相应放宽）。
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


def main() -> int:
    # 0. 健康检查
    step("健康检查 GET /healthz")
    status, resp = call("GET", "/healthz")
    check("healthz 200", status == 200, str(resp))

    # 1. 发布 v1 作为基准
    step("发布 v1 基准规则集")
    status, resp = call("POST", "/api/v1/publish", load("rules_v1.json"))
    show(resp)
    check("发布返回 201", status == 201, str(resp))
    baseline_version = resp.get("version")

    # 2. 预览候选集（默认基准 = 请求开始时最新版本）
    step("预览候选集 POST /api/v1/preview（不写入）")
    preview_body = load("preview_request.json")
    status, resp = call("POST", "/api/v1/preview", preview_body)
    show(resp)
    check("200", status == 200, str(resp))
    check("基准版本为刚发布的版本",
          resp.get("baseline_version") == baseline_version,
          str(resp.get("baseline_version")))
    check("候选规则数=4", resp.get("candidate_rule_count") == 4)

    samples = resp.get("samples", [])
    check("样例数量与顺序与输入一致（5 条，index 0..4）",
          [s.get("index") for s in samples] == [0, 1, 2, 3, 4])

    def side(s, name):
        return s[name]["covered"], s[name]["conclusion"], s[name]["priority"]

    # 样例 0：BJ/personal/2026-06，两侧都是 approve(p100) -> 无变化
    c0, p0 = side(samples[0], "baseline"), side(samples[0], "candidate")
    check("样例0 两侧 approve/100", c0 == p0 == (True, "approve", 100), f"{c0} {p0}")
    check("样例0 changed=false", samples[0]["changed"] is False)

    # 样例 1：2027-06，基准未覆盖，候选 approve_new_policy(p50) -> 变化
    c1, p1 = side(samples[1], "baseline"), side(samples[1], "candidate")
    check("样例1 基准未覆盖", c1 == (False, "未覆盖", None), str(c1))
    check("样例1 候选 approve_new_policy/50",
          p1 == (True, "approve_new_policy", 50), str(p1))
    check("样例1 changed=true（覆盖状态与结论都变）", samples[1]["changed"] is True)

    # 样例 2：GZ/personal，结论同为 manual_review，仅优先级 10 -> 20 -> 不算变化
    c2, p2 = side(samples[2], "baseline"), side(samples[2], "candidate")
    check("样例2 结论不变、优先级 10->20",
          c2 == (True, "manual_review", 10)
          and p2 == (True, "manual_review", 20), f"{c2} {p2}")
    check("样例2 changed=false（仅覆盖状态或结论不同才标记）",
          samples[2]["changed"] is False)

    # 样例 3：SH/personal 不变
    check("样例3 两侧 reject/100 且 changed=false",
          side(samples[3], "baseline") == side(samples[3], "candidate")
          == (True, "reject", 100) and samples[3]["changed"] is False)

    # 样例 4：2028-06，基准未覆盖，候选开放区间新政策覆盖 -> 变化
    check("样例4 基准未覆盖/候选 approve_new_policy，changed=true",
          side(samples[4], "baseline") == (False, "未覆盖", None)
          and side(samples[4], "candidate") == (True, "approve_new_policy", 50)
          and samples[4]["changed"] is True)

    # 3. 显式指定基准版本，结果应与默认一致
    step("显式指定基准版本预览")
    body = {**preview_body, "version": baseline_version}
    status, resp = call("POST", "/api/v1/preview", body)
    check("200 且 baseline_version 为指定值",
          status == 200 and resp.get("baseline_version") == baseline_version)
    check("样例1 结论与默认基准一致",
          resp["samples"][1]["candidate"]["conclusion"] == "approve_new_policy")

    # 4. 候选集冲突：整次拒绝，不返回任何样例结果
    step("预览冲突候选集（整次拒绝）")
    status, resp = call("POST", "/api/v1/preview", load("preview_conflict.json"))
    show(resp)
    check("HTTP 409", status == 409)
    check("错误码 RULE_CONFLICT", resp.get("error", {}).get("code") == "RULE_CONFLICT")
    check("返回冲突规则对", len(resp.get("conflicts", [])) >= 1
          and {"rule1", "rule2"} <= set(resp["conflicts"][0]))
    check("无任何样例结果（不产生部分结果）", "samples" not in resp)

    # 5. 基准版本不存在
    step("预览指定不存在的基准版本 999 -> 404")
    status, resp = call("POST", "/api/v1/preview", load("preview_bad_version.json"))
    show(resp)
    check("404 VERSION_NOT_FOUND",
          status == 404 and resp.get("error", {}).get("code") == "VERSION_NOT_FOUND")

    # 6. 预览不写入、不消耗版本号：再发布一次 v1，新版本号应当只 +1
    step("预览不消耗版本号：再次发布检查版本连续")
    status, resp = call("POST", "/api/v1/publish", load("rules_v1.json"))
    show(resp)
    check("新版本号仅 +1（中间预览未取号）",
          status == 201 and resp.get("version") == baseline_version + 1,
          str(resp.get("version")))

    # 7. 字段非法：空样例列表 / 缺字段 / 候选规则时间段非法
    step("非法入参 -> 422")
    status, resp = call("POST", "/api/v1/preview",
                        {"rules": [], "samples": []})
    check("空 samples 列表 422", status == 422
          and resp.get("error", {}).get("code") == "VALIDATION_ERROR")

    status, _ = call("POST", "/api/v1/preview", {"rules": []})
    check("缺少 samples 字段 422", status == 422)

    bad_time = {
        "rules": [{
            "business_key": "k", "region": "*", "category": "*",
            "valid_from": "2026-12-01T00:00:00Z",
            "valid_to": "2026-12-01T00:00:00Z",
            "priority": 1, "conclusion": "x",
        }],
        "samples": [{
            "business_key": "k", "region": "BJ", "category": "c",
            "occurred_at": "2026-06-01T00:00:00Z",
        }],
    }
    status, _ = call("POST", "/api/v1/preview", bad_time)
    check("候选规则时间段非法 422", status == 422)

    print("\n==================================================")
    if failures:
        print(f"存在 {len(failures)} 个失败检查：{failures}")
        return 1
    print("全部检查通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
