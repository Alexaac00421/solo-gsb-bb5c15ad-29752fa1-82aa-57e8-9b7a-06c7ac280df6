# 政策裁决服务（Policy Adjudication Service）

基于 **Python 3.12 + FastAPI + PostgreSQL** 的政策/规则裁决服务。每次发布提交
**完整规则集**，成功后得到一个**单调递增的新版本号**；裁决在固定版本内按
**数值最高优先级**返回结论。提供可调用的 HTTP API 与调用示例，Docker Compose
一键启动服务和数据库并完成初始化。

---

## 1. 一键启动（Docker Compose）

前置要求：已安装 Docker 与 Docker Compose 插件（`docker compose` 子命令）。

```bash
docker compose up --build -d          # 构建并后台启动 API + PostgreSQL
docker compose logs -f api            # 查看服务日志
# 停止（保留数据卷）
docker compose down
# 停止并清空数据库数据
docker compose down -v
```

首次启动时：

* PostgreSQL 容器自动执行 [`db/init.sql`](db/init.sql) 建库建表（仅首次数据卷为空时执行）；
* API 容器等待数据库就绪后，在启动钩子中幂等执行同一套 DDL（`CREATE ... IF NOT EXISTS`），
  因此即使绕过 Compose 启动脚本，表结构也会自动就绪。

### 地址与端口

| 用途 | 地址 |
| --- | --- |
| API 根地址 | `http://localhost:8000` |
| Swagger UI（交互式文档） | `http://localhost:8000/docs` |
| OpenAPI Schema | `http://localhost:8000/openapi.json` |
| 存活探针 | `GET http://localhost:8000/healthz` |
| 就绪探针（查库） | `GET http://localhost:8000/readyz` |
| PostgreSQL | `localhost:5432`（宿主机映射） |

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `policy` / `policy` / `policy` | 数据库账号与库名 |
| `POSTGRES_HOST` / `POSTGRES_PORT` | `db` / `5432` | API 容器内连接数据库的地址（Compose 已设） |
| `DATABASE_URL` | 由上述变量拼装 | 显式设置后优先使用，格式 `postgresql+asyncpg://用户:密码@主机:端口/库` |
| `API_HOST_PORT` | `8000` | API 在宿主机暴露的端口 |
| `DB_HOST_PORT` | `5432` | 数据库在宿主机暴露的端口 |
| `DB_CONNECT_RETRIES` / `DB_CONNECT_INTERVAL` | `30` / `2` | 启动时等待数据库就绪的重试次数与间隔（秒） |

可复制 `.env.example` 为 `.env` 修改宿主机端口、数据库口令等，`docker compose`
会自动读取。

### 本地（非 Docker）开发运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 需自行提供一个 PostgreSQL，然后：
export DATABASE_URL='postgresql+asyncpg://policy:policy@localhost:5432/policy'
uvicorn app.main:app --reload --port 8000
```

---

## 2. 数据模型与规则语义

一条规则包含：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `business_key` | string（必填） | 业务键 |
| `region` | string（必填） | 地区代码；`"*"` 为**通配**，表示任意地区 |
| `category` | string（必填） | 对象类别；`"*"` 为**通配** |
| `valid_from` | ISO8601 时间（必填） | 有效时间段起点（含） |
| `valid_to` | ISO8601 时间（可空） | 有效时间段终点（不含）；省略/`null` 表示结束时间开放 |
| `priority` | 整数（必填） | 优先级；裁决取**数值最高**者 |
| `conclusion` | string（必填） | 命中结论 |

* **半开有效时间段**：`[valid_from, valid_to)`。`occurred_at == valid_from` 命中，
  `occurred_at == valid_to` **不**命中。必须满足 `valid_from < valid_to`，
  否则返回 `422` 并明确提示时间段非法。
* 不带时区的时间统一按 UTC 解释；内部统一存储为 UTC。
* 发布的规则集只**追加版本**，不就地修改；更正规则必须发布新的完整规则集。

---

## 3. HTTP API

### 3.1 全量发布 `POST /api/v1/publish`

请求体为完整规则集：

```json
{
  "rules": [
    {
      "business_key": "loan_approval",
      "region": "BJ",
      "category": "personal",
      "valid_from": "2026-01-01T00:00:00Z",
      "valid_to": "2027-01-01T00:00:00Z",
      "priority": 100,
      "conclusion": "approve"
    }
  ]
}
```

成功响应 `201 Created`：

```json
{ "version": 1, "rule_count": 1 }
```

**冲突导致整次发布失败**：若同一业务键、同一优先级、**不同结论**的两条规则在
地区、类别、时间上**可能同时命中**，返回 `409`，事务整体回滚（不留下部分数据，
也不会产生可读取的新版本）：

* 地区可同时命中：相等，或至少一方为通配；
* 类别同理；
* 时间段可同时命中：两个半开区间重叠（端点相接**不**算重叠；结束时间开放表示无限远）。

```json
{
  "error": {
    "code": "RULE_CONFLICT",
    "message": "发布失败：存在同业务键、同优先级、不同结论且可同时命中的规则"
  },
  "conflicts": [
    { "rule1": { "...": "完整规则对象" }, "rule2": { "...": "完整规则对象" } }
  ]
}
```

> 同业务键同优先级但**结论相同**的多条规则不算冲突（冗余允许）；
> 同业务键不同优先级也不冲突（裁决时高优先级胜出）。

### 3.2 裁决 `POST /api/v1/adjudicate`

请求体：

```json
{
  "business_key": "loan_approval",
  "region": "BJ",
  "category": "personal",
  "occurred_at": "2026-06-01T08:00:00Z",
  "version": 1
}
```

`version` 可选：

* **指定版本**：在该版本内裁决；版本不存在返回 `404 VERSION_NOT_FOUND`；
* **不指定版本**：固定为**请求开始时**最新已发布版本（以数据库事务快照为准）。
  该版本在整个请求内不再改变；裁决事务为 `REPEATABLE READ`，**并发发布提交的新
  版本不会被同一请求看到，保证一次裁决绝不混用版本**。
  若系统尚无任何已发布版本，返回 `409 NO_PUBLISHED_VERSION`。

命中响应（返回版本、最高优先级、该优先级上所有命中规则、结论）：

```json
{
  "version": 1,
  "covered": true,
  "priority": 100,
  "conclusion": "approve",
  "matched_rules": [ { "id": 1, "...": "..." } ]
}
```

无任何命中：

```json
{
  "version": 1,
  "covered": false,
  "priority": null,
  "conclusion": "未覆盖",
  "matched_rules": []
}
```

### 3.3 版本查询

* `GET /api/v1/versions`：全部已发布版本（版本号、规则数、发布时间）。
* `GET /api/v1/versions/{version}`：指定版本元数据及其**完整规则集**；不存在返回 `404`。

### 3.4 错误响应约定

| 场景 | HTTP 状态码 | `error.code` |
| --- | --- | --- |
| 字段缺失 / 类型错误 / 空字符串 / 布尔当整数 | 422 | `VALIDATION_ERROR`（明细在 `details`，含字段名与原因） |
| 时间段非法（`valid_from >= valid_to`） | 422 | `VALIDATION_ERROR`（消息含“时间段非法”） |
| 规则冲突 | 409 | `RULE_CONFLICT`（外层附 `conflicts` 规则对） |
| 指定版本不存在 | 404 | `VERSION_NOT_FOUND` |
| 未指定版本且尚无任何发布 | 409 | `NO_PUBLISHED_VERSION` |

统一错误体形如：

```json
{
  "error": {"code": "VALIDATION_ERROR", "message": "请求参数校验失败"},
  "details": [
    {"field": "rules.0.valid_to", "type": "value_error",
     "message": "Value error, 时间段非法：半开区间要求 valid_from < valid_to"}
  ]
}
```

---

## 4. 调用示例

服务启动后任选其一：

```bash
# （1）零依赖 Python 端到端示例（发布/裁决/冲突/非法时间段/字段缺失/
#     更正重放/版本不存在，内置断言，全绿退出码为 0）
python examples/demo_client.py
BASE_URL=http://127.0.0.1:8000 python examples/demo_client.py

# （2）curl 示例
bash examples/curl_examples.sh

# （3）并发发布/裁决一致性演示（验证一次裁决不混用版本）
python examples/concurrency_demo.py
```

示例载荷文件：

* [`examples/rules_v1.json`](examples/rules_v1.json)：v1 完整规则集；
* [`examples/rules_v2.json`](examples/rules_v2.json)：v2 更正规则集
  （新增与旧规则半开端点相接的开放区间规则，演示“端点相接不冲突、旧版本重放不变”）；
* [`examples/rules_conflict.json`](examples/rules_conflict.json)：必失败的冲突规则集。

### 最小 Python 调用片段

```python
import urllib.request, json

def post(url, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return r.status, json.loads(r.read())

print(post("http://localhost:8000/api/v1/publish",
           {"rules": [{
               "business_key": "loan_approval", "region": "BJ", "category": "*",
               "valid_from": "2026-01-01T00:00:00Z", "valid_to": None,
               "priority": 10, "conclusion": "manual_review"}]}))

print(post("http://localhost:8000/api/v1/adjudicate",
           {"business_key": "loan_approval", "region": "BJ",
            "category": "personal", "occurred_at": "2026-06-01T00:00:00Z"}))
```

---

## 5. 关键设计说明

* **全量发布 + 单调版本号**：版本号由 PostgreSQL 序列生成；冲突预检在分配版本号
  之前完成，冲突发布整次失败、不会消耗版本号（成功版本连续）。规则与版本在同一
  事务内写入，SQL 自连接复核也失败时事务回滚，不会留下部分可见的数据。
* **冲突检测下推 SQL**：发布临时版本内部自连接，条件覆盖业务键、优先级、结论、
  地区/类别通配（数据库中存 `NULL`）与半开区间重叠
  （`f1 < t2 AND f2 < t1`，`NULL` 终点按 `infinity` 处理）。
* **裁决事务隔离**：`REPEATABLE READ` 下第一条查询建立事务快照并确定版本，
  随后全部读取都来自同一快照；并发发布不会让一次裁决混用版本。
* **最高优先级**：匹配规则按 `priority DESC` 取出，Python 侧切出最高优先级的
  全部命中规则；该优先级上结论必然唯一（否则发布时即冲突）。
* **时区**：数据库统一使用 `TIMESTAMPTZ`；响应中的时间均为 ISO8601（UTC，带偏移）。

## 6. 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── config.py          # 环境变量配置
│   ├── db.py              # 异步引擎、DDL 初始化、序列
│   ├── schemas.py         # Pydantic 模型与入参校验（含半开区间校验）
│   ├── services.py        # 发布/冲突检测/裁决/版本查询（SQL 与事务逻辑）
│   └── main.py            # FastAPI 应用、路由与错误处理
├── db/init.sql            # PostgreSQL 容器首启初始化脚本
├── examples/              # 调用示例（demo_client / curl / 并发演示 / 载荷）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
