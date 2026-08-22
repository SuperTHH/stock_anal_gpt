from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hengce.contracts.enums import PoolReadinessStatus, StrategyType
from hengce.contracts.official_event import ReportSource
from hengce.contracts.strategy import StrategyCandidate


class FunnelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = "full-market-funnel-v1"
    target_size: int = Field(default=300, ge=1, le=500)
    minimum_listing_days: int = Field(default=365, ge=0)
    minimum_amount: Decimal = Field(default=Decimal("50000"), gt=0)
    high_dividend_yield: Decimal = Field(default=Decimal("0.03"), ge=0, le=1)
    stable_dividend_candidate_yield: Decimal = Field(
        default=Decimal("0.05"), ge=0, le=1
    )
    required_strategy_coverage: Decimal = Field(
        default=Decimal("0"), ge=0, le=1,
        description="Legacy display field; dynamic publication is count-based.",
    )


class EvidenceCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    periodic_report_count: int = Field(ge=0, le=5)
    annual_dividend_count: int = Field(ge=0, le=5)
    risk_screen_available: bool
    corporate_action_screen_available: bool
    required_item_count: int = 12
    completed_item_count: int = Field(ge=0, le=12)
    missing_items: tuple[str, ...]
    ready_for_scoring: bool

    @model_validator(mode="after")
    def validate_counts(self) -> EvidenceCoverage:
        expected = (
            self.periodic_report_count
            + self.annual_dividend_count
            + int(self.risk_screen_available)
            + int(self.corporate_action_screen_available)
        )
        if self.completed_item_count != expected:
            raise ValueError("evidence counts do not reconcile")
        if self.ready_for_scoring != (expected == self.required_item_count):
            raise ValueError("evidence readiness does not reconcile")
        return self


class FunnelSecurity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts_code: str
    name: str
    exchange: str
    board: str
    industry_l1: str | None
    list_date: date
    close: Decimal
    amount: Decimal
    dividend_yield: Decimal | None
    entry_reasons: tuple[str, ...]
    evidence: EvidenceCoverage


class DynamicPoolStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_type: StrategyType
    strategy_version: str
    status: PoolReadinessStatus
    universe_size: int = Field(gt=0)
    complete_factor_count: int = Field(ge=0)
    coverage_ratio: Decimal = Field(ge=0, le=1)
    required_coverage_ratio: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    minimum_complete_factor_count: int = Field(default=1, ge=1)
    blocking_codes: tuple[str, ...]
    candidate_count: int = Field(ge=0, le=30)

    @model_validator(mode="after")
    def validate_status(self) -> DynamicPoolStatus:
        if self.complete_factor_count > self.universe_size:
            raise ValueError("dynamic pool count is invalid")
        expected = Decimal(self.complete_factor_count) / Decimal(self.universe_size)
        if self.coverage_ratio != expected:
            raise ValueError("dynamic pool coverage does not reconcile")
        ready = self.complete_factor_count >= self.minimum_complete_factor_count
        if ready != (self.status is PoolReadinessStatus.READY):
            raise ValueError("dynamic pool readiness does not reconcile")
        if ready == bool(self.blocking_codes):
            raise ValueError("dynamic pool blocking codes do not reconcile")
        return self


class FullMarketResearchSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    market_date: date
    generated_at: datetime
    config: FunnelConfig
    market_universe_count: int = Field(ge=0)
    low_cost_eligible_count: int = Field(ge=0)
    funnel_count: int = Field(ge=0)
    high_dividend_funnel_count: int = Field(ge=0)
    depth_ready_count: int = Field(ge=0)
    evidence_item_count: int = Field(ge=0)
    evidence_completed_count: int = Field(ge=0)
    funnel: tuple[FunnelSecurity, ...]
    pools: dict[StrategyType, DynamicPoolStatus]
    candidate_pools: dict[StrategyType, tuple[StrategyCandidate, ...]]
    source_records: tuple[ReportSource, ...] = ()
    input_hashes: dict[str, str]
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("generated_at")
    @classmethod
    def generated_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must include timezone")
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> FullMarketResearchSnapshot:
        if self.funnel_count != len(self.funnel):
            raise ValueError("funnel count does not reconcile")
        if set(self.pools) != set(StrategyType) or set(self.candidate_pools) != set(
            StrategyType
        ):
            raise ValueError("dynamic strategy set is incomplete")
        if self.evidence_item_count != self.funnel_count * 12:
            raise ValueError("evidence plan count does not reconcile")
        if self.evidence_completed_count != sum(
            item.evidence.completed_item_count for item in self.funnel
        ):
            raise ValueError("evidence completion does not reconcile")
        if any(
            self.pools[strategy].candidate_count
            != len(self.candidate_pools[strategy])
            for strategy in StrategyType
        ):
            raise ValueError("dynamic candidate counts do not reconcile")
        return self


__all__ = [
    "DynamicPoolStatus",
    "EvidenceCoverage",
    "FullMarketResearchSnapshot",
    "FunnelConfig",
    "FunnelSecurity",
]
