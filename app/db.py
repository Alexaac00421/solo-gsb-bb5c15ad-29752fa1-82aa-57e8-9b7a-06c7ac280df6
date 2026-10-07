"""异步数据库引擎、DDL 初始化与版本号序列。"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy import text

from .config import DB_CONNECT_INTERVAL, DB_CONNECT_RETRIES, DATABASE_URL

logger = logging.getLogger("policy_service")

engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)

# 版本号序列：每个成功发布的完整规则集得到一个单调递增的新版本号
CREATE_SEQUENCE = text("CREATE SEQUENCE IF NOT EXISTS rule_version_id_seq")

CREATE_TABLES = [
    text(
        """
        CREATE TABLE IF NOT EXISTS rule_versions (
            version     BIGINT PRIMARY KEY
                          DEFAULT nextval('rule_version_id_seq'),
            rule_count  INTEGER NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    ),
    text(
        """
        CREATE TABLE IF NOT EXISTS rules (
            id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            version      BIGINT NOT NULL
                           REFERENCES rule_versions(version),
            business_key TEXT NOT NULL,
            region       TEXT,           -- NULL 表示通配（任意地区）
            category     TEXT,           -- NULL 表示通配（任意对象类别）
            valid_from   TIMESTAMPTZ NOT NULL,
            valid_to     TIMESTAMPTZ,    -- NULL 表示结束时间开放（无限期）
            priority     INTEGER NOT NULL,
            conclusion   TEXT NOT NULL
        )
        """
    ),
    text(
        """
        CREATE INDEX IF NOT EXISTS idx_rules_lookup
            ON rules (version, business_key)
        """
    ),
    text(
        """
        CREATE INDEX IF NOT EXISTS idx_rules_version
            ON rules (version)
        """
    ),
]


async def init_db() -> None:
    """建表/建序列（幂等）。compose 中的 db init 脚本也会执行同一套 DDL。"""
    last_error: Exception | None = None
    for attempt in range(1, DB_CONNECT_RETRIES + 1):
        try:
            async with engine.begin() as conn:
                await conn.execute(CREATE_SEQUENCE)
                for stmt in CREATE_TABLES:
                    await conn.execute(stmt)
            if attempt > 1:
                logger.info("数据库连接第 %d 次尝试成功", attempt)
            return
        except Exception as exc:  # 数据库容器可能尚未就绪
            last_error = exc
            logger.warning(
                "等待数据库就绪（%d/%d）：%s", attempt, DB_CONNECT_RETRIES, exc
            )
            await asyncio.sleep(DB_CONNECT_INTERVAL)
    raise RuntimeError(f"数据库无法连接：{last_error}") from last_error
