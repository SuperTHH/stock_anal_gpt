from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .base import FactBase
from .enums import (
    ConflictResolutionStatus,
    ConsolidationScope,
    DiscoveryMethod,
    MappingStatus,
    QualityStatus,
    ReportType,
    StatementType,
)

SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SHANGHAI = ZoneInfo("Asia/Shanghai")


def require_aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value


class FilingDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    source_url: AnyHttpUrl
    ts_code: str
    exchange: Literal["SSE", "SZSE"]
    report_period: date
    report_type: ReportType
    published_at: datetime
    collected_at: datetime
    attachment_name: str
    content_type: str
    raw_object_hash: SHA256
    taxonomy_refs: tuple[str, ...]
    discovery_method: DiscoveryMethod
    instance_entrypoint: str | None

    @field_validator("published_at", "collected_at")
    @classmethod
    def timestamps_must_be_aware(cls, value: datetime, info: ValidationInfo) -> datetime:
        return require_aware(value, info.field_name)


class TaxonomyPackageRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    taxonomy_id: str
    source_id: str
    source_url: AnyHttpUrl
    raw_object_hash: SHA256
    package_name: str
    entrypoint: str
    content_type: str
    collected_at: datetime

    @field_validator("collected_at")
    @classmethod
    def collected_time_must_be_aware(cls, value: datetime, info: ValidationInfo) -> datetime:
        return require_aware(value, info.field_name)


class FinancialFiling(FactBase):
    model_config = ConfigDict(extra="forbid")

    filing_id: str
    ts_code: str
    exchange: Literal["SSE", "SZSE"]
    report_period: date
    report_type: ReportType
    announcement_at: datetime
    taxonomy: tuple[str, ...]
    taxonomy_hashes: tuple[SHA256, ...]
    raw_object_hash: SHA256
    filing_version: str
    parser_name: str
    parser_version: str
    mapping_version: str
    fact_count: int
    conflict_count: int
    is_restated: bool
    supersedes_id: str | None

    @field_validator(
        "published_at", "effective_at", "collected_at", "valid_from", "announcement_at"
    )
    @classmethod
    def timestamps_must_be_aware(
        cls, value: datetime | None, info: ValidationInfo
    ) -> datetime | None:
        return require_aware(value, info.field_name) if value is not None else value

    @model_validator(mode="after")
    def validate_identity_and_publication(self) -> "FinancialFiling":
        if self.record_id != self.filing_id:
            raise ValueError("record_id must equal filing_id")
        if self.published_at is None:
            raise ValueError("published_at is required")
        if self.announcement_at != self.published_at:
            raise ValueError("announcement_at must equal published_at")
        if self.content_hash != self.raw_object_hash:
            raise ValueError("content_hash must equal raw_object_hash")
        return self


class FinancialFact(FactBase):
    model_config = ConfigDict(extra="forbid")

    fact_id: str
    ts_code: str
    report_period: date
    report_type: ReportType
    announcement_at: datetime
    statement_type: StatementType
    taxonomy: tuple[str, ...]
    fact_name: str
    raw_qname: str
    canonical_fact_name: str | None
    mapping_status: MappingStatus
    fact_value: Decimal
    unit: str | None
    currency: str | None
    filing_id: str
    context_signature: str
    entity_scheme: str
    entity_identifier: str
    period_start: date | None
    period_end: date | None
    instant: date | None
    unit_signature: str | None
    decimals: str | None
    consolidation_scope: ConsolidationScope
    dimensions: dict[str, str]
    fact_identity_hash: SHA256
    comparison_identity_hash: SHA256

    @field_validator(
        "published_at", "effective_at", "collected_at", "valid_from", "announcement_at"
    )
    @classmethod
    def timestamps_must_be_aware(
        cls, value: datetime | None, info: ValidationInfo
    ) -> datetime | None:
        return require_aware(value, info.field_name) if value is not None else value

    @model_validator(mode="after")
    def validate_financial_fact(self) -> "FinancialFact":
        if self.record_id != self.fact_id:
            raise ValueError("record_id must equal fact_id")
        if self.published_at is None:
            raise ValueError("published_at is required")
        if self.announcement_at != self.published_at:
            raise ValueError("announcement_at must equal published_at")

        is_duration = self.period_start is not None or self.period_end is not None
        if is_duration == (self.instant is not None):
            raise ValueError("fact period must be exactly duration or instant")
        if is_duration and (self.period_start is None or self.period_end is None):
            raise ValueError("duration facts require both period bounds")
        if self.mapping_status is MappingStatus.UNMAPPED and self.canonical_fact_name is not None:
            raise ValueError("unmapped facts cannot claim a canonical name")

        expected_effective = datetime.combine(
            self.report_period,
            time(23, 59, 59, tzinfo=SHANGHAI),
        )
        if (
            self.effective_at is None
            or self.effective_at.astimezone(UTC) != expected_effective.astimezone(UTC)
        ):
            raise ValueError("effective_at must be report-period end in Asia/Shanghai")
        return self


class FactConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conflict_id: str
    filing_id: str
    fact_identity_hash: SHA256
    competing_fact_ids: tuple[str, ...]
    conflict_type: str
    resolution_status: ConflictResolutionStatus
    quality_status: QualityStatus
    detected_at: datetime

    @field_validator("detected_at")
    @classmethod
    def detected_time_must_be_aware(cls, value: datetime, info: ValidationInfo) -> datetime:
        return require_aware(value, info.field_name)
