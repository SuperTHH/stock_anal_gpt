from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from hengce.contracts.enums import QualityStatus
from hengce.contracts.market import SecurityMaster, TradingStatus


@dataclass(frozen=True, slots=True)
class HardFilterConfig:
    filter_version: str
    minimum_listing_days: int
    minimum_liquidity_days: int
    minimum_average_amount: Decimal
    minimum_financial_completeness: Decimal
    maximum_financial_age_days: int

    def __post_init__(self) -> None:
        if (
            not self.filter_version
            or self.minimum_listing_days < 0
            or self.minimum_liquidity_days <= 0
            or self.minimum_average_amount < 0
            or not Decimal(0) <= self.minimum_financial_completeness <= Decimal(1)
            or self.maximum_financial_age_days < 0
        ):
            raise ValueError("HARD_FILTER_CONFIG_INVALID")


@dataclass(frozen=True, slots=True)
class SecurityResearchInput:
    security: SecurityMaster
    trading_status: TradingStatus
    report_date: date
    recent_amounts: tuple[Decimal, ...]
    audit_opinion_standard: bool
    major_investigation_open: bool
    financial_completeness: Decimal
    total_return_quality: QualityStatus
    publication_order_known: bool
    financial_age_days: int
    evidence_record_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HardFilterResult:
    passed: bool
    reasons: tuple[str, ...]
    filter_version: str
    source_record_ids: tuple[str, ...] = ()


class HardFilterEngine:
    def __init__(self, config: HardFilterConfig) -> None:
        self.config = config

    def evaluate(self, item: SecurityResearchInput) -> HardFilterResult:
        reasons: list[str] = []
        security = item.security
        status = item.trading_status
        if not security.is_in_scope or security.board not in {
            "MAIN_SH",
            "STAR",
            "MAIN_SZ",
            "CHINEXT",
        }:
            reasons.append("HF-01")
        if status.delisting_risk:
            reasons.append("HF-02")
        if status.st_status is not None:
            reasons.append("HF-03")
        if (
            status.trade_date != item.report_date
            or status.is_suspended
            or not status.is_trading
        ):
            reasons.append("HF-04")
        if (item.report_date - security.list_date).days < self.config.minimum_listing_days:
            reasons.append("HF-05")
        if (
            len(item.recent_amounts) < self.config.minimum_liquidity_days
            or sum(item.recent_amounts, Decimal(0)) / Decimal(len(item.recent_amounts))
            < self.config.minimum_average_amount
        ):
            reasons.append("HF-06")
        if not item.audit_opinion_standard:
            reasons.append("HF-07")
        if item.major_investigation_open:
            reasons.append("HF-08")
        if item.financial_completeness < self.config.minimum_financial_completeness:
            reasons.append("HF-09")
        if item.total_return_quality is not QualityStatus.DERIVED:
            reasons.append("HF-10")
        if not item.publication_order_known:
            reasons.append("HF-11")
        if item.financial_age_days > self.config.maximum_financial_age_days:
            reasons.append("HF-12")
        return HardFilterResult(
            passed=not reasons,
            reasons=tuple(reasons),
            filter_version=self.config.filter_version,
            source_record_ids=item.evidence_record_ids,
        )
