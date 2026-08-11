from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from .base import FactBase
from .enums import QualityStatus, StrategyType


class OfficialEvent(FactBase):
    institution: str
    event_type: str
    title: str
    factual_summary: str
    system_assessment: str = ""
    affected_scope: str = "A_SHARE_MARKET"
    affected_ts_codes: tuple[str, ...]
    related_strategies: tuple[StrategyType, ...] = ()
    impact_horizon: str
    confidence: Decimal = Field(ge=0, le=1)


class ReportSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    domain: str
    source_name: str
    source_url: HttpUrl
    published_at: datetime | None = None
    effective_at: datetime | None = None
    collected_at: datetime
    valid_from: datetime
    version: str
    license_policy: str
    quality_status: QualityStatus

    @field_validator(
        "published_at",
        "effective_at",
        "collected_at",
        "valid_from",
    )
    @classmethod
    def times_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("report source times must include a timezone")
        return value
