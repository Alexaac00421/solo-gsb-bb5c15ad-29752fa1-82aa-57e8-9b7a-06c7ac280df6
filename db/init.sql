-- 政策裁决服务数据库初始化（容器首次启动时自动执行；幂等，可重复执行）。
-- 应用启动时也会执行同一套 DDL，两处保持一致。

CREATE SEQUENCE IF NOT EXISTS rule_version_id_seq;

CREATE TABLE IF NOT EXISTS rule_versions (
    version     BIGINT PRIMARY KEY
                  DEFAULT nextval('rule_version_id_seq'),
    rule_count  INTEGER NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
);

CREATE INDEX IF NOT EXISTS idx_rules_lookup
    ON rules (version, business_key);

CREATE INDEX IF NOT EXISTS idx_rules_version
    ON rules (version);
