from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hengce.contracts.enums import (
    EvidenceCohort,
    EvidenceKind,
    EvidenceTaskStatus,
    ReviewDecision,
)


class FullMarketEvidenceRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    market_date: date
    cohort: EvidenceCohort
    member_codes: tuple[str, ...]
    task_count: int = Field(ge=0)
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence run timestamps must be timezone-aware")
        return value


class FullMarketEvidenceTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    market_date: date
    cohort: EvidenceCohort
    ts_code: str = Field(pattern=r"^[0-9]{6}\.(?:SH|SZ)$")
    security_name: str = Field(min_length=1)
    evidence_kind: EvidenceKind
    evidence_period: str = Field(min_length=1)
    status: EvidenceTaskStatus
    version: int = Field(ge=1)
    attempt_count: int = Field(default=0, ge=0)
    source_record_ids: tuple[str, ...] = ()
    source_id: str | None = None
    source_url: str | None = None
    source_title: str | None = None
    published_at: datetime | None = None
    collected_at: datetime | None = None
    raw_object_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_page: int | None = Field(default=None, ge=1)
    excerpt: str | None = Field(default=None, max_length=500)
    prefilled_values: dict[str, bool | str | None] = Field(default_factory=dict)
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at", "published_at", "collected_at")
    @classmethod
    def aware_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence task timestamps must be timezone-aware")
        return value


class EvidenceReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    decision: ReviewDecision
    reviewed_values: dict[str, bool | str | None] = Field(default_factory=dict)
    note: str = Field(default="", max_length=1000)


__all__ = [
    "EvidenceReviewRequest",
    "FullMarketEvidenceRun",
    "FullMarketEvidenceTask",
]
