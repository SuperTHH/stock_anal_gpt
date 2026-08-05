from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .enums import (
    AcquisitionStatus,
    DiscoveryMethod,
    DocumentKind,
    PoolReadinessStatus,
    QualityStatus,
    ReportType,
    StrategyType,
)

Board = Literal["MAIN_SH", "MAIN_SZ", "CHINEXT", "STAR"]
BOARD_NAMES = frozenset({"MAIN_SH", "MAIN_SZ", "CHINEXT", "STAR"})
USABLE_QUALITY = frozenset({QualityStatus.VALID, QualityStatus.DERIVED})
DOWNLOAD_STATUSES = frozenset(
    {
        AcquisitionStatus.DOWNLOADED,
        AcquisitionStatus.VERIFIED,
        AcquisitionStatus.INGESTED,
    }
)


class PilotUniverseMember(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts_code: str = Field(pattern=r"^[0-9]{6}\.(?:SH|SZ)$")
    security_name: str = Field(min_length=1)
    board: Board
    amount: Decimal = Field(gt=0)
    rank_in_board: int = Field(gt=0)
    evidence_record_ids: tuple[str, ...]

    @model_validator(mode="after")
    def validate_member_lineage(self) -> "PilotUniverseMember":
        if not self.evidence_record_ids or any(not item for item in self.evidence_record_ids):
            raise ValueError("PILOT_MEMBER_LINEAGE_REQUIRED")
        if len(self.evidence_record_ids) != len(set(self.evidence_record_ids)):
            raise ValueError("PILOT_MEMBER_LINEAGE_DUPLICATE")
        if self.board in {"MAIN_SH", "STAR"} and not self.ts_code.endswith(".SH"):
            raise ValueError("PILOT_MEMBER_EXCHANGE_MISMATCH")
        if self.board in {"MAIN_SZ", "CHINEXT"} and not self.ts_code.endswith(".SZ"):
            raise ValueError("PILOT_MEMBER_EXCHANGE_MISMATCH")
        return self


class PilotUniverseSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    universe_id: str = Field(min_length=1)
    market_date: date
    report_cutoff_at: datetime
    algorithm_version: str = Field(min_length=1)
    quotas: dict[Board, int]
    members: tuple[PilotUniverseMember, ...]
    input_hashes: dict[str, str]
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @field_validator("report_cutoff_at", "created_at")
    @classmethod
    def times_must_be_aware(cls, value: datetime, info: ValidationInfo) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> "PilotUniverseSnapshot":
        if set(self.quotas) != BOARD_NAMES or any(value <= 0 for value in self.quotas.values()):
            raise ValueError("PILOT_BOARD_QUOTAS_INVALID")
        member_codes = [member.ts_code for member in self.members]
        if len(member_codes) != len(set(member_codes)):
            raise ValueError("PILOT_MEMBER_DUPLICATE")
        if sum(self.quotas.values()) != len(self.members):
            raise ValueError("PILOT_BOARD_QUOTA_MISMATCH")
        for board, quota in self.quotas.items():
            board_members = [member for member in self.members if member.board == board]
            if len(board_members) != quota:
                raise ValueError("PILOT_BOARD_QUOTA_MISMATCH")
            ranks = [member.rank_in_board for member in board_members]
            if len(ranks) != len(set(ranks)):
                raise ValueError("PILOT_BOARD_RANK_DUPLICATE")
        if not self.input_hashes or any(
            not key
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for key, value in self.input_hashes.items()
        ):
            raise ValueError("PILOT_INPUT_HASH_INVALID")
        return self


class AcquisitionManifestItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    universe_id: str = Field(min_length=1)
    ts_code: str = Field(pattern=r"^[0-9]{6}\.(?:SH|SZ)$")
    document_kind: DocumentKind
    report_type: ReportType | None
    report_period: date | None
    source_id: str = Field(min_length=1)
    report_cutoff_at: datetime
    status: AcquisitionStatus
    source_url: AnyHttpUrl | None
    discovery_method: DiscoveryMethod | None
    published_at: datetime | None
    effective_at: datetime | None
    collected_at: datetime | None
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    version: str | None
    supersedes_id: str | None
    raw_object_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    quality_status: QualityStatus
    error_code: str | None
    attempt_count: int = Field(ge=0)

    @field_validator(
        "report_cutoff_at",
        "published_at",
        "effective_at",
        "collected_at",
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
    def validate_identity_and_state(self) -> "AcquisitionManifestItem":
        if self.document_kind is DocumentKind.PERIODIC_REPORT:
            if self.report_type is None or self.report_period is None:
                raise ValueError("ACQUISITION_PERIODIC_IDENTITY_REQUIRED")
        elif self.report_type is not None:
            raise ValueError("ACQUISITION_REPORT_TYPE_NOT_ALLOWED")

        if self.status in DOWNLOAD_STATUSES and any(
            value is None
            for value in (
                self.source_url,
                self.discovery_method,
                self.published_at,
                self.collected_at,
                self.content_hash,
                self.raw_object_hash,
                self.version,
            )
        ):
            raise ValueError("ACQUISITION_DOWNLOAD_LINEAGE_REQUIRED")
        if self.status is AcquisitionStatus.AWAITING_MANUAL and not self.error_code:
            raise ValueError("ACQUISITION_MANUAL_ERROR_REQUIRED")
        if (
            self.status is AcquisitionStatus.INGESTED
            and self.quality_status not in USABLE_QUALITY
        ):
            raise ValueError("ACQUISITION_INGESTED_QUALITY_INVALID")
        if (
            self.status in {AcquisitionStatus.VERIFIED, AcquisitionStatus.INGESTED}
            and self.published_at is not None
            and self.published_at > self.report_cutoff_at
        ):
            raise ValueError("ACQUISITION_PUBLISHED_AFTER_CUTOFF")
        return self


class PoolReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_type: StrategyType
    universe_size: int = Field(gt=0)
    eligible_count: int = Field(ge=0)
    complete_factor_count: int = Field(ge=0)
    coverage_ratio: Decimal = Field(ge=0, le=1)
    required_coverage_ratio: Decimal = Field(ge=0, le=1)
    status: PoolReadinessStatus
    missing_by_security: dict[str, tuple[str, ...]]
    blocking_codes: tuple[str, ...]
    strategy_version: str = Field(min_length=1)
    factor_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_readiness(self) -> "PoolReadiness":
        if self.eligible_count > self.universe_size:
            raise ValueError("POOL_ELIGIBLE_COUNT_INVALID")
        if self.complete_factor_count > self.eligible_count:
            raise ValueError("POOL_COMPLETE_COUNT_INVALID")
        expected_ratio = Decimal(self.complete_factor_count) / Decimal(self.universe_size)
        if self.coverage_ratio != expected_ratio:
            raise ValueError("POOL_COVERAGE_RATIO_MISMATCH")
        if self.required_coverage_ratio != Decimal("0.80"):
            raise ValueError("POOL_REQUIRED_COVERAGE_INVALID")
        expected_status = (
            PoolReadinessStatus.READY
            if self.coverage_ratio >= self.required_coverage_ratio
            else PoolReadinessStatus.BLOCKED
        )
        if self.status is not expected_status:
            raise ValueError("POOL_READINESS_STATUS_MISMATCH")
        if self.status is PoolReadinessStatus.READY and self.blocking_codes:
            raise ValueError("POOL_READY_CANNOT_BE_BLOCKED")
        if self.status is PoolReadinessStatus.BLOCKED and not self.blocking_codes:
            raise ValueError("POOL_BLOCKING_CODE_REQUIRED")
        return self
