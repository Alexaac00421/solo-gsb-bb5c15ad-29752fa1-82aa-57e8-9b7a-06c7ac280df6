"""FastAPI 入口：政策裁决服务 HTTP API。

接口：
  POST /api/v1/publish      全量发布规则集（成功返回新版本号）
  POST /api/v1/adjudicate   政策裁决
  GET  /api/v1/versions     已发布版本列表
  GET  /api/v1/versions/{v} 指定版本的完整规则集
  GET  /healthz             存活探针（不查库）
  GET  /readyz              就绪探针（查库）
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .db import engine, init_db
from .schemas import AdjudicateRequest, PublishRequest
from .services import (
    NoPublishedVersion,
    RuleConflict,
    VersionNotFound,
    adjudicate,
    get_version,
    list_versions,
    publish_rules,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("policy_service")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("政策裁决服务已就绪")
    yield
    await engine.dispose()


app = FastAPI(
    title="政策裁决服务",
    version="1.0.0",
    description=(
        "全量发布规则集（同业务键同优先级不同结论存在同时命中即发布失败），"
        "按业务键/地区/类别/时刻在固定版本内裁决。"
    ),
    lifespan=lifespan,
)


def _error(code: str, message: str, status: int, **extra) -> JSONResponse:
    # 错误元信息放在 error 对象内；附加数据（如 conflicts）放在响应体外层
    body: dict = {"error": {"code": code, "message": message}}
    body.update(extra)
    # jsonable_encoder 负责把 datetime/Decimal 等对象转为可 JSON 序列化的值
    return JSONResponse(status_code=status, content=jsonable_encoder(body))


@app.exception_handler(RuleConflict)
async def _rule_conflict_handler(request: Request, exc: RuleConflict):
    return _error(
        "RULE_CONFLICT",
        "发布失败：存在同业务键、同优先级、不同结论且可同时命中的规则",
        409,
        conflicts=exc.pairs,
    )


@app.exception_handler(VersionNotFound)
async def _version_not_found_handler(request: Request, exc: VersionNotFound):
    return _error(
        "VERSION_NOT_FOUND", f"版本 {exc.version} 不存在", 404, version=exc.version
    )


@app.exception_handler(NoPublishedVersion)
async def _no_published_handler(request: Request, exc: NoPublishedVersion):
    return _error(
        "NO_PUBLISHED_VERSION", "尚无任何已发布的规则集，无法裁决", 409
    )


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    # 字段缺失 / 类型错误 / 时间段非法等，统一为与其他错误一致的结构
    details = [
        {
            "field": ".".join(str(p) for p in err.get("loc", []) if p != "body"),
            "type": err.get("type"),
            "message": err.get("msg"),
        }
        for err in exc.errors()
    ]
    return _error("VALIDATION_ERROR", "请求参数校验失败", 422, details=details)


@app.post("/api/v1/publish", status_code=201)
async def publish(payload: PublishRequest):
    """全量发布：成功后规则集整体作为一个新版本可见，返回新版本号。"""
    async with engine.begin() as conn:
        version, count = await publish_rules(conn, payload.rules)
    logger.info("发布成功：version=%s, rules=%s", version, count)
    return {"version": version, "rule_count": count}


@app.post("/api/v1/adjudicate")
async def do_adjudicate(payload: AdjudicateRequest):
    """裁决。

    未指定 version 时，在请求开始时刻（事务快照建立时）固定为最新已发布版本；
    REPEATABLE READ 保证整个请求只看到该版本，即使期间有并发发布提交。
    """
    # 进入上下文后、第一条 SQL 前设置隔离级别（SQLAlchemy 异步官方用法）。
    # REPEATABLE READ 的事务快照在第一条读语句建立后固定，
    # 整次裁决随后读取均不会看到并发提交的新版本
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="REPEATABLE READ")
        result = await adjudicate(
            conn,
            business_key=payload.business_key,
            region=payload.region,
            category=payload.category,
            occurred_at=payload.occurred_at,
            version=payload.version,
        )
    return result


@app.get("/api/v1/versions")
async def get_versions():
    async with engine.connect() as conn:
        versions = await list_versions(conn)
    return {"versions": versions}


@app.get("/api/v1/versions/{version}")
async def get_one_version(version: int):
    async with engine.connect() as conn:
        return await get_version(conn, version)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    async with engine.connect() as conn:
        await conn.exec_driver_sql("SELECT 1")
    return {"status": "ready"}
