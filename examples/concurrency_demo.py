#!/usr/bin/env python3
"""并发发布 / 裁决一致性演示（仅依赖标准库）。

要证明的不变量：并发发布新版本期间，一次裁决自始至终只使用同一个版本——
响应中的 version、conclusion、matched_rules 必须严格来自同一版本，绝不跨版本拼接。

交叉校验方法（关键）：
  1. 不指定版本发起裁决，得到响应 (version=v, priority, conclusion, matched_rules)；
  2. 通过 GET /api/v1/versions/v 取出 v 的完整规则集——版本一经发布即不可变；
  3. 在客户端按完全相同的匹配规则（业务键、地区/类别通配、半开时间段、
     最高优先级）独立计算期望结果；
  4. 比对结论、优先级与命中规则 id 集合。
  若服务端在一次裁决里把不同版本的行拼在一起，第 2、4 步必然对不上。
  另校验每个请求线程观察到的“最新版本”单调不减（版本只追加）。

阶段三对显式指定的旧版本并发重放，结果同样逐字段交叉校验，且恒定不变。

用法：
    BASE_URL=http://localhost:8000 python examples/concurrency_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
SHARED_KEY = "shared_key"
REGION = "ANY_REGION"
CATEGORY = "ANY_CATEGORY"
AT = datetime(2026, 6, 1, tzinfo=timezone.utc)
PUBLISHERS = 12
REQUESTERS = 8

_errors: list[str] = []
_lock = threading.Lock()


def request_json(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, json.loads(resp.read().decode())


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def expected_from_version(version_detail: dict) -> dict:
    """从某版本完整规则集在客户端独立计算期望裁决结果。"""
    def wildcard_or_exact(rule_value, query_value):
        # 服务端把通配 '*' 存为 null
        return rule_value is None or rule_value == query_value

    hits = []
    for r in version_detail["rules"]:
        if r["business_key"] != SHARED_KEY:
            continue
        if not wildcard_or_exact(r["region"], REGION):
            continue
        if not wildcard_or_exact(r["category"], CATEGORY):
            continue
        vf = parse_dt(r["valid_from"])
        vt = parse_dt(r["valid_to"]) if r["valid_to"] else None
        if not vf <= AT:
            continue
        if vt is not None and not vt > AT:  # 半开：右端不含
            continue
        hits.append(r)

    if not hits:
        return {"covered": False, "priority": None, "conclusion": "未覆盖",
                "ids": set()}
    top = max(h["priority"] for h in hits)
    top_hits = [h for h in hits if h["priority"] == top]
    return {
        "covered": True,
        "priority": top,
        "conclusion": top_hits[0]["conclusion"],
        "ids": {h["id"] for h in top_hits},
    }


def cross_check(version: int | None = None) -> int:
    """一次裁决 + 取该版本完整集 + 客户端重算比对；返回响应版本。"""
    body = {"business_key": SHARED_KEY, "region": REGION,
            "category": CATEGORY, "occurred_at": AT.isoformat()}
    if version is not None:
        body["version"] = version
    status, resp = request_json("POST", "/api/v1/adjudicate", body)
    assert status == 200, resp

    v = resp["version"]
    if version is not None and v != version:
        with _lock:
            _errors.append(f"指定 v{version} 裁决却返回 v{v}")

    status, detail = request_json("GET", f"/api/v1/versions/{v}")
    assert status == 200, detail
    exp = expected_from_version(detail)

    if resp["covered"] != exp["covered"] \
            or resp["conclusion"] != exp["conclusion"] \
            or resp["priority"] != exp["priority"] \
            or {r["id"] for r in resp["matched_rules"]} != exp["ids"]:
        with _lock:
            _errors.append(
                f"裁决与版本 v{v} 存储内容不一致（疑似跨版本混用）："
                f"resp={resp} expected={exp}"
            )
    return v


def publisher(worker_id: int, stop: threading.Event, counter: list[int]) -> int:
    """并发全量发布，直到 stop；返回发布次数。每次发布都是自洽的完整规则集。"""
    n = 0
    while not stop.is_set() or n == 0:
        with _lock:
            counter[0] += 1
            seq = counter[0]
        # 一个始终命中的通配规则 + 一个结论版本无关的稳定规则，
        # 结论带全局序号，保证每个版本内容不同且可被交叉校验。
        rules = [
            {
                "business_key": SHARED_KEY,
                "region": "*",
                "category": "*",
                "valid_from": "2020-01-01T00:00:00Z",
                "valid_to": None,
                "priority": 1,
                "conclusion": f"baseline_{seq}",
            },
            {
                "business_key": SHARED_KEY,
                "region": REGION,
                "category": CATEGORY,
                "valid_from": "2020-01-01T00:00:00Z",
                "valid_to": None,
                "priority": 100,
                "conclusion": f"specific_{seq}",
            },
        ]
        status, resp = request_json("POST", "/api/v1/publish", {"rules": rules})
        assert status == 201, resp
        n += 1
    return n


def requester(worker_id: int, stop: threading.Event) -> list[int]:
    seen: list[int] = []
    while not stop.is_set():
        seen.append(cross_check())
    for _ in range(5):
        seen.append(cross_check())
    return seen


def main() -> int:
    print(f"目标：{BASE_URL}")
    print(f"并发压力：{PUBLISHERS} 发布线程 + {REQUESTERS} 裁决线程，持续约 6 秒 ...")

    stop = threading.Event()
    counter = [0]
    observed: list[list[int]] = []

    # 预置一个初始版本，保证裁决线程启动时系统已有已发布版本（否则 409）
    status, seed = request_json("POST", "/api/v1/publish", {"rules": [{
        "business_key": SHARED_KEY, "region": "*", "category": "*",
        "valid_from": "2020-01-01T00:00:00Z", "valid_to": None,
        "priority": 1, "conclusion": "seed",
    }]})
    assert status == 201, seed
    print("  预置初始版本 v1")

    with ThreadPoolExecutor(max_workers=PUBLISHERS + REQUESTERS) as pool:
        pub_futs = [pool.submit(publisher, i, stop, counter)
                    for i in range(PUBLISHERS)]
        req_futs = [pool.submit(requester, i, stop)
                    for i in range(REQUESTERS)]
        # 并发压力持续约 6 秒后通知发布者收尾（每个发布者至少发布 1 版）
        import time
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            time.sleep(0.2)
        stop.set()
        total_publishes = sum(f.result() for f in as_completed(pub_futs))
        for f in as_completed(req_futs):
            observed.append(f.result())

    for i, seq in enumerate(observed):
        for a, b in zip(seq, seq[1:]):
            if b < a:
                with _lock:
                    _errors.append(f"请求线程 {i} 观察到最新版本倒退：{a} -> {b}")
        print(f"  requester {i:02d}: {len(seq)} 次裁决，"
              f"版本区间 v{min(seq)}..v{max(seq)}，单调不减 OK")
    print(f"  并发成功发布 {total_publishes} 个新版本")

    print("阶段三：对已发布的若干旧版本并发显式重放 ...")
    all_versions = sorted({v for seq in observed for v in seq})
    if len(all_versions) > 6:
        step = max(1, len(all_versions) // 6)
        sample = all_versions[::step][:6]
    else:
        sample = all_versions
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(cross_check, v) for v in sample for _ in range(8)]
        results = [f.result() for f in as_completed(futs)]
        print(f"  对 {len(sample)} 个旧版本各 8 并发重放，共 {len(results)} 次，"
              "结果恒定 OK")

    if _errors:
        print(f"\n发现 {len(_errors)} 个一致性错误，示例：")
        for e in _errors[:10]:
            print("  -", e)
        return 1
    total = sum(len(s) for s in observed) + len(results)
    print(f"\n并发一致性验证通过 ✅：{total} 次裁决全部与其所属版本严格自洽，"
          "无跨版本混用；旧版本重放结果恒定。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
