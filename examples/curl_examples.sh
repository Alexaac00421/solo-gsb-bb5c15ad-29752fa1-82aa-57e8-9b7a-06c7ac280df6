#!/usr/bin/env bash
# 政策裁决服务 curl 调用示例。
# 用法：bash examples/curl_examples.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
echo "服务地址：$BASE_URL"

echo
echo "1) 健康检查"
curl -fsS "$BASE_URL/healthz"; echo

echo
echo "2) 全量发布 v1 规则集（返回新版本号）"
curl -fsS -X POST "$BASE_URL/api/v1/publish" \
  -H 'Content-Type: application/json' \
  -d @"$(dirname "$0")/rules_v1.json"; echo

echo
echo "3) 裁决：BJ / personal / 2026-06-01（具体规则优先，expect approve）"
curl -fsS -X POST "$BASE_URL/api/v1/adjudicate" \
  -H 'Content-Type: application/json' \
  -d '{
        "business_key": "loan_approval",
        "region": "BJ",
        "category": "personal",
        "occurred_at": "2026-06-01T08:00:00Z"
      }'; echo

echo
echo "4) 指定旧版本裁决"
curl -fsS -X POST "$BASE_URL/api/v1/adjudicate" \
  -H 'Content-Type: application/json' \
  -d '{
        "business_key": "loan_approval",
        "region": "BJ",
        "category": "personal",
        "occurred_at": "2026-06-01T08:00:00Z",
        "version": 1
      }'; echo

echo
echo "5) 冲突发布（HTTP 409，返回冲突规则对）"
curl -sS -o /tmp/conflict.json -w 'HTTP %{http_code}\n' -X POST "$BASE_URL/api/v1/publish" \
  -H 'Content-Type: application/json' \
  -d @"$(dirname "$0")/rules_conflict.json"
cat /tmp/conflict.json; echo

echo
echo "6) 版本不存在（HTTP 404）"
curl -sS -o /tmp/notfound.json -w 'HTTP %{http_code}\n' -X POST "$BASE_URL/api/v1/adjudicate" \
  -H 'Content-Type: application/json' \
  -d '{
        "business_key": "loan_approval",
        "region": "BJ",
        "category": "personal",
        "occurred_at": "2026-06-01T08:00:00Z",
        "version": 999
      }'
cat /tmp/notfound.json; echo

echo
echo "7) 发布预览：候选集不写入，对比基准（最新版 v1）与候选在每条样例上的裁决"
curl -fsS -X POST "$BASE_URL/api/v1/preview" \
  -H 'Content-Type: application/json' \
  -d @"$(dirname "$0")/preview_request.json"; echo

echo
echo "7a) 预览冲突候选集（HTTP 409，整次拒绝、不返回样例结果，不产生版本）"
curl -sS -o /tmp/preview_conflict.json -w 'HTTP %{http_code}\n' -X POST "$BASE_URL/api/v1/preview" \
  -H 'Content-Type: application/json' \
  -d @"$(dirname "$0")/preview_conflict.json"
cat /tmp/preview_conflict.json; echo

echo
echo "7b) 预览指定不存在的基准版本（HTTP 404）"
curl -sS -o /tmp/preview_notfound.json -w 'HTTP %{http_code}\n' -X POST "$BASE_URL/api/v1/preview" \
  -H 'Content-Type: application/json' \
  -d @"$(dirname "$0")/preview_bad_version.json"
cat /tmp/preview_notfound.json; echo

echo
echo "7c) 预览后版本列表不变（未写入、未消耗版本号）"
curl -fsS "$BASE_URL/api/v1/versions"; echo

echo
echo "8) 版本列表 / 指定版本规则集"
curl -fsS "$BASE_URL/api/v1/versions"; echo
curl -fsS "$BASE_URL/api/v1/versions/1" | head -c 600; echo
