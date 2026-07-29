from datetime import date, datetime
from decimal import Decimal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .enums import (
    CandidateStatus,
    QualityStatus,
    ReportStatus,
    StrategyType,
)


class FactorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    factor_name: str
    raw_value: Decimal | None
    normalized_score: Decimal | None = Field(default=None, ge=0, le=100)
    weight: Decimal = Field(ge=0, le=1)
    weighted_score: Decimal | None = Field(default=None, ge=0, le=100)
    quality_status: QualityStatus
    source_record_ids: tuple[str, ...]
    normalization_scope: str
    used_market_fallback: bool

    @model_validator(mode="after")
    def validate_lineage_and_score(self) -> "FactorDetail":
        if not self.source_record_ids:
            raise ValueError("factor source lineage is required")
        if self.normalized_score is None and self.weighted_score is not None:
            raise ValueError("weighted score requires normalized score")
        if (
            self.normalized_score is not None
            and self.weighted_score != self.normalized_score * self.weight
        ):
            raise ValueError("weighted score does not match score and weight")
        return self


class StrategyCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_date: date
    strategy_type: StrategyType
    strategy_version: str
    ts_code: str
    rank_in_strategy: int = Field(gt=0)
    strategy_score: Decimal = Field(ge=0, le=100, decimal_places=2)
    factor_details: tuple[FactorDetail, ...]
    selection_reasons: tuple[str, ...]
    risk_flags: tuple[str, ...]
    catalysts: tuple[str, ...]
    observe_conditions: tuple[str, ...]
    invalidate_conditions: tuple[str, ...]
    data_completeness: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    data_cutoff_at: datetime
    known_at: datetime
    candidate_status: CandidateStatus

    @field_validator("data_cutoff_at", "known_at")
    @classmethod
    def cutoff_must_be_aware(cls, value: datetime, info: ValidationInfo) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must include a timezone")
        return value


class StrategyRunEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_type: StrategyType
    strategy_version: str
    input_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    data_insufficient_count: int = Field(ge=0)
    qualified_count: int = Field(ge=0)
    published_candidate_count: int = Field(ge=0)
    completed: bool

    @model_validator(mode="after")
    def counts_must_reconcile(self) -> "StrategyRunEvidence":
        if (
            self.excluded_count
            + self.data_insufficient_count
            + self.qualified_count
            != self.input_count
        ):
            raise ValueError("strategy evaluation counts do not reconcile")
        if self.published_candidate_count > min(self.qualified_count, 30):
            raise ValueError("published candidate count exceeds qualified population")
        if (
            self.completed
            and self.published_candidate_count != min(self.qualified_count, 30)
        ):
            raise ValueError("completed strategy evidence must publish every ranked candidate")
        return self


class ReportSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: str
    report_date: date
    market_cutoff_at: datetime
    event_cutoff_at: datetime
    generated_at: datetime
    published_at: datetime | None
    report_status: ReportStatus
    previous_report_id: str | None
    data_domain_statuses: dict[str, QualityStatus]
    strategy_versions: dict[StrategyType, str]
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator(
        "market_cutoff_at",
        "event_cutoff_at",
        "generated_at",
        "published_at",
    )
    @classmethod
    def times_must_be_aware(
        cls,
        value: datetime | None,
        info: ValidationInfo,
    ) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError(f"{info.field_name} must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_publication(self) -> "ReportSnapshot":
        published = {
            ReportStatus.PUBLISHED,
            ReportStatus.PUBLISHED_PARTIAL,
        }
        if self.report_status in published:
            if self.published_at is None or self.published_at < self.generated_at:
                raise ValueError("published report requires a valid publication time")
            if set(self.strategy_versions) != set(StrategyType):
                raise ValueError("published report requires all strategy versions")
        return self
