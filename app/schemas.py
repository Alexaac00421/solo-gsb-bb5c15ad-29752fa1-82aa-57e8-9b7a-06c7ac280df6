"""HTTP API 的 Pydantic 模型与入参校验。

时间段为半开区间 [valid_from, valid_to)：
* valid_to 可省略 / 为 null，表示结束时间开放；
* 必须满足 valid_from < valid_to（时刻相等即非法）；
* 不带时区的时间统一按 UTC 解释。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, field_validator


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class RuleIn(BaseModel):
    business_key: str = Field(..., description="业务键，非空字符串")
    region: str = Field(..., description="地区代码；'*' 为通配")
    category: str = Field(..., description="对象类别；'*' 为通配")
    valid_from: datetime
    valid_to: Optional[datetime] = None
    priority: int = Field(..., description="整数优先级，裁决取数值最高者")
    conclusion: str = Field(..., description="命中后的结论，非空字符串")

    @field_validator("business_key", "region", "category", "conclusion")
    @classmethod
    def _strip_required(cls, v: str, info) -> str:
        if not isinstance(v, str):
            raise ValueError("必须是字符串")
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v

    @field_validator("priority", mode="before")
    @classmethod
    def _reject_bool(cls, v):
        # bool 是 int 的子类，须在宽松模式强转前显式拒绝 true/false
        if isinstance(v, bool):
            raise ValueError("必须是整数，不能是布尔值")
        return v

    @field_validator("valid_from", "valid_to")
    @classmethod
    def _normalize_dt(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is None:
            return None
        return _to_utc(v)

    def model_post_init(self, __context) -> None:
        if self.valid_to is not None and self.valid_from >= self.valid_to:
            raise ValueError(
                "时间段非法：半开区间要求 valid_from < valid_to"
            )


class PublishRequest(BaseModel):
    rules: list[RuleIn] = Field(..., description="本次发布的完整规则集（全量）")


class AdjudicateRequest(BaseModel):
    business_key: str = Field(..., description="业务键，非空字符串")
    region: str = Field(..., description="地区代码")
    category: str = Field(..., description="对象类别")
    occurred_at: datetime = Field(..., description="事件发生时刻")
    version: Optional[int] = Field(None, description="可选版本；省略则取请求开始时最新版本")

    @field_validator("business_key", "region", "category")
    @classmethod
    def _strip_required(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("必须是字符串")
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v

    @field_validator("version", mode="before")
    @classmethod
    def _reject_bool_version(cls, v):
        if isinstance(v, bool):
            raise ValueError("版本必须是整数，不能是布尔值")
        return v

    @field_validator("occurred_at")
    @classmethod
    def _normalize_dt(cls, v: datetime) -> datetime:
        return _to_utc(v)
