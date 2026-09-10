from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

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


class OfficialEventSourceScan(BaseModel):
    """Auditable result of scanning one configured official event source."""

    model_config = ConfigDict(extra="forbid")

    scan_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    market_date: date
    listing_url: HttpUrl
    status: Literal["SUCCESS", "FAILED"]
    event_count: int = Field(ge=0)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scanned_at: datetime
    error_code: str | None = None
    ts_code: str | None = Field(default=None, pattern=r"^\d{6}\.(SH|SZ)$")
    scan_start_date: date | None = None
    scan_end_date: date | None = None
    pagination_complete: bool = False
    page_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def security_scan_scope_must_be_complete(self) -> "OfficialEventSourceScan":
        scoped_values = (self.scan_start_date, self.scan_end_date)
        if self.ts_code is None:
            if any(value is not None for value in scoped_values):
                raise ValueError("EVENT_SCAN_SECURITY_REQUIRED")
            return self
        if (
            any(value is None for value in scoped_values)
            or self.scan_start_date > self.scan_end_date
            or self.scan_end_date != self.market_date
            or self.scan_end_date > self.scanned_at.date()
        ):
            raise ValueError("EVENT_SCAN_DATE_SCOPE_INVALID")
        if self.status == "SUCCESS" and (
            not self.pagination_complete
            or self.page_count == 0
            or self.error_code is not None
        ):
            raise ValueError("EVENT_SCAN_INCOMPLETE")
        return self

    @field_validator("scanned_at")
    @classmethod
    def scanned_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scan time must include a timezone")
        return value

    @field_validator("error_code")
    @classmethod
    def error_code_must_be_non_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("error code must be non-blank")
        return value


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
