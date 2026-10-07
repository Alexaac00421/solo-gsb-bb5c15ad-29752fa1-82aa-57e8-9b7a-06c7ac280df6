"""运行期配置，全部来自环境变量，附带与 docker-compose 一致的默认值。"""
from __future__ import annotations

import os


def _build_database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    user = os.getenv("POSTGRES_USER", "policy")
    password = os.getenv("POSTGRES_PASSWORD", "policy")
    host = os.getenv("POSTGRES_HOST", "db")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "policy")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"


DATABASE_URL: str = _build_database_url()

# 启动时等待数据库就绪的重试参数
DB_CONNECT_RETRIES: int = int(os.getenv("DB_CONNECT_RETRIES", "30"))
DB_CONNECT_INTERVAL: float = float(os.getenv("DB_CONNECT_INTERVAL", "2"))

# 通配符：业务规则中地区 / 对象类别用 "*" 表示可命中任意值
WILDCARD: str = "*"
