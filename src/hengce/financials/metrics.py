from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from hengce.contracts.enums import QualityStatus

_BLOCKING_QUALITY = frozenset(
    {
        QualityStatus.CONFLICT,
        QualityStatus.REJECTED,
        QualityStatus.UNVERIFIED,
    }
)


@dataclass(frozen=True, slots=True)
class MetricValue:
    value: Decimal | None
    quality_status: QualityStatus
    input_fact_ids: tuple[str, ...]
    algorithm_version: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class FinancialMetricResult:
    metrics: dict[str, MetricValue]
    blocked_reasons: tuple[str, ...]
    as_of: datetime
    known_at: datetime


@dataclass(frozen=True, slots=True)
class MarketValuationInput:
    record_id: str
    value: Decimal
    effective_at: datetime
    collected_at: datetime
    valid_from: datetime
    quality_status: QualityStatus


class FinancialMetricCalculator:
    def __init__(self, algorithm_version: str) -> None:
        if not algorithm_version:
            raise ValueError("algorithm_version must not be empty")
        self.algorithm_version = algorithm_version

    def calculate(
        self,
        *,
        facts: list[dict[str, object]],
        market_cap: MarketValuationInput,
        as_of: datetime,
        known_at: datetime,
    ) -> FinancialMetricResult:
        self._validate_cutoff(as_of)
        self._validate_cutoff(known_at)
        indexed: dict[str, dict[str, object]] = {}
        for fact in facts:
            try:
                published_at = fact["published_at"]
                effective_at = fact["effective_at"]
                collected_at = fact["collected_at"]
                valid_from = fact["valid_from"]
            except KeyError:
                return FinancialMetricResult(
                    {},
                    ("FINANCIAL_INPUT_TIME_LINEAGE_MISSING",),
                    as_of,
                    known_at,
                )
            if not all(
                isinstance(value, datetime)
                and value.tzinfo is not None
                and value.utcoffset() is not None
                for value in (published_at, effective_at, collected_at, valid_from)
            ):
                return FinancialMetricResult(
                    {},
                    ("FINANCIAL_INPUT_TIME_LINEAGE_INVALID",),
                    as_of,
                    known_at,
                )
            if published_at > as_of or effective_at > as_of:
                return FinancialMetricResult(
                    {},
                    ("FINANCIAL_INPUT_AFTER_CUTOFF",),
                    as_of,
                    known_at,
                )
            if collected_at > known_at or valid_from > known_at:
                return FinancialMetricResult(
                    {},
                    ("FINANCIAL_INPUT_NOT_KNOWN_AT_CUTOFF",),
                    as_of,
                    known_at,
                )
            name = str(fact["canonical_fact_name"])
            quality = QualityStatus(str(fact["quality_status"]))
            if quality in _BLOCKING_QUALITY:
                return FinancialMetricResult(
                    {},
                    ("FINANCIAL_INPUT_QUALITY_BLOCKED",),
                    as_of,
                    known_at,
                )
            if name in indexed:
                return FinancialMetricResult(
                    {},
                    ("FINANCIAL_INPUT_DUPLICATE",),
                    as_of,
                    known_at,
                )
            indexed[name] = fact

        for value in (
            market_cap.effective_at,
            market_cap.collected_at,
            market_cap.valid_from,
        ):
            self._validate_cutoff(value)
        if market_cap.effective_at > as_of:
            return FinancialMetricResult(
                {},
                ("MARKET_VALUATION_AFTER_CUTOFF",),
                as_of,
                known_at,
            )
        if market_cap.collected_at > known_at or market_cap.valid_from > known_at:
            return FinancialMetricResult(
                {},
                ("MARKET_VALUATION_NOT_KNOWN_AT_CUTOFF",),
                as_of,
                known_at,
            )
        if market_cap.quality_status not in {
            QualityStatus.VALID,
            QualityStatus.DERIVED,
        }:
            return FinancialMetricResult(
                {},
                ("MARKET_VALUATION_QUALITY_BLOCKED",),
                as_of,
                known_at,
            )

        def ids(*names: str) -> tuple[str, ...]:
            return tuple(
                sorted(str(indexed[name]["fact_id"]) for name in names if name in indexed)
            )

        def value(name: str) -> Decimal | None:
            fact = indexed.get(name)
            return Decimal(str(fact["fact_value"])) if fact is not None else None

        def missing(*names: str, reason: str = "INPUT_MISSING") -> MetricValue:
            return MetricValue(
                value=None,
                quality_status=QualityStatus.MISSING,
                input_fact_ids=ids(*names),
                algorithm_version=self.algorithm_version,
                reason=reason,
            )

        def derived(result: Decimal, *names: str) -> MetricValue:
            return MetricValue(
                value=result,
                quality_status=QualityStatus.DERIVED,
                input_fact_ids=ids(*names),
                algorithm_version=self.algorithm_version,
            )

        net_profit = value("net_profit")
        equity = value("equity")
        beginning_equity = value("beginning_equity")
        if net_profit is None or equity is None:
            roe = missing("net_profit", "equity", "beginning_equity")
        else:
            denominator = (
                (beginning_equity + equity) / Decimal(2)
                if beginning_equity is not None
                else equity
            )
            if denominator == 0:
                roe = missing(
                    "net_profit",
                    "equity",
                    "beginning_equity",
                    reason="DENOMINATOR_MISSING_OR_ZERO",
                )
            else:
                roe = derived(
                    net_profit / denominator,
                    "net_profit",
                    "equity",
                    "beginning_equity",
                )

        nopat = value("nopat")
        invested_capital = value("invested_capital")
        roic = self._ratio(
            nopat,
            invested_capital,
            ("nopat", "invested_capital"),
            ids,
        )

        revenue = value("revenue")
        prior_revenue = value("prior_revenue")
        revenue_growth = self._growth(
            revenue,
            prior_revenue,
            ("revenue", "prior_revenue"),
            ids,
        )

        operating_cash_flow = value("operating_cash_flow")
        cash_flow_quality = self._ratio(
            operating_cash_flow,
            net_profit,
            ("operating_cash_flow", "net_profit"),
            ids,
        )

        debt_ratio = self._ratio(
            value("total_liabilities"),
            value("total_assets"),
            ("total_liabilities", "total_assets"),
            ids,
        )

        capital_expenditure = value("capital_expenditure")
        if operating_cash_flow is None or capital_expenditure is None:
            free_cash_flow = missing("operating_cash_flow", "capital_expenditure")
        else:
            free_cash_flow = derived(
                operating_cash_flow - capital_expenditure,
                "operating_cash_flow",
                "capital_expenditure",
            )

        if net_profit is None:
            pe = missing("net_profit")
        elif net_profit <= 0:
            pe = missing("net_profit", reason="NON_POSITIVE_EARNINGS")
        else:
            pe = MetricValue(
                market_cap.value / net_profit,
                QualityStatus.DERIVED,
                (*ids("net_profit"), market_cap.record_id),
                self.algorithm_version,
            )
        pb = self._ratio(market_cap.value, equity, ("equity",), ids)
        if pb.value is not None:
            pb = MetricValue(
                pb.value,
                pb.quality_status,
                (*pb.input_fact_ids, market_cap.record_id),
                pb.algorithm_version,
                pb.reason,
            )
        fcf_yield = self._ratio(
            free_cash_flow.value,
            market_cap.value,
            ("operating_cash_flow", "capital_expenditure"),
            ids,
        )
        if fcf_yield.value is not None:
            fcf_yield = MetricValue(
                fcf_yield.value,
                fcf_yield.quality_status,
                (*fcf_yield.input_fact_ids, market_cap.record_id),
                fcf_yield.algorithm_version,
                fcf_yield.reason,
            )

        return FinancialMetricResult(
            {
                "roe": roe,
                "roic": roic,
                "revenue_growth": revenue_growth,
                "cash_flow_quality": cash_flow_quality,
                "debt_ratio": debt_ratio,
                "free_cash_flow": free_cash_flow,
                "pe": pe,
                "pb": pb,
                "fcf_yield": fcf_yield,
            },
            (),
            as_of,
            known_at,
        )

    def _ratio(
        self,
        numerator: Decimal | None,
        denominator: Decimal | None,
        names: tuple[str, ...],
        ids: object,
    ) -> MetricValue:
        fact_ids = ids(*names)
        if numerator is None or denominator is None or denominator == 0:
            return MetricValue(
                None,
                QualityStatus.MISSING,
                fact_ids,
                self.algorithm_version,
                "DENOMINATOR_MISSING_OR_ZERO"
                if numerator is not None
                else "INPUT_MISSING",
            )
        return MetricValue(
            numerator / denominator,
            QualityStatus.DERIVED,
            fact_ids,
            self.algorithm_version,
        )

    def _growth(
        self,
        current: Decimal | None,
        prior: Decimal | None,
        names: tuple[str, ...],
        ids: object,
    ) -> MetricValue:
        fact_ids = ids(*names)
        if current is None or prior is None or prior == 0:
            return MetricValue(
                None,
                QualityStatus.MISSING,
                fact_ids,
                self.algorithm_version,
                "DENOMINATOR_MISSING_OR_ZERO"
                if current is not None
                else "INPUT_MISSING",
            )
        return MetricValue(
            current / prior - Decimal(1),
            QualityStatus.DERIVED,
            fact_ids,
            self.algorithm_version,
        )

    @staticmethod
    def _validate_cutoff(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("FINANCIAL_METRIC_CUTOFF_INVALID")
