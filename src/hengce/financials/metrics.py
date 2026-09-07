from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from hengce.actions.share_capital import ShareCapitalResult
from hengce.contracts.dividend import AnnualDividendRecord
from hengce.contracts.enums import (
    ActionStatus,
    ActionType,
    QualityStatus,
)
from hengce.contracts.market import CorporateAction
from hengce.financials.assembler import (
    AssembledFinancialFact,
    FinancialSeriesResult,
)

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
                operating_cash_flow - abs(capital_expenditure),
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


@dataclass(frozen=True, slots=True)
class PilotMetricResult:
    metrics: dict[str, MetricValue]
    blocked_reasons: tuple[str, ...]
    report_cutoff_at: datetime
    known_at: datetime
    tax_rate_proxy: Decimal


class PilotMetricCalculator:
    TAX_RATE_PROXY = Decimal("0.25")

    def __init__(self, algorithm_version: str) -> None:
        if not algorithm_version.strip():
            raise ValueError("PILOT_METRIC_ALGORITHM_VERSION_INVALID")
        self.algorithm_version = algorithm_version

    def calculate(
        self,
        *,
        series: FinancialSeriesResult,
        closing_price: Decimal,
        share_capital: ShareCapitalResult,
        dividends: list[CorporateAction],
        report_cutoff_at: datetime,
        known_at: datetime,
        dividend_history: list[AnnualDividendRecord] | None = None,
    ) -> PilotMetricResult:
        self._require_aware(report_cutoff_at)
        self._require_aware(known_at)
        if series.blocked_reasons:
            return PilotMetricResult(
                metrics={},
                blocked_reasons=series.blocked_reasons,
                report_cutoff_at=report_cutoff_at,
                known_at=known_at,
                tax_rate_proxy=self.TAX_RATE_PROXY,
            )
        if (
            series.report_cutoff_at != report_cutoff_at
            or series.known_at != known_at
        ):
            return PilotMetricResult(
                metrics={},
                blocked_reasons=("FINANCIAL_SERIES_CUTOFF_MISMATCH",),
                report_cutoff_at=report_cutoff_at,
                known_at=known_at,
                tax_rate_proxy=self.TAX_RATE_PROXY,
            )

        annual_2023 = series.periods.get(date(2023, 12, 31))
        annual_2024 = series.periods.get(date(2024, 12, 31))
        q1_2025 = series.periods.get(date(2025, 3, 31))
        annual_2025 = series.periods.get(date(2025, 12, 31))
        q1_2026 = series.periods.get(date(2026, 3, 31))

        def fact(
            snapshot: object,
            name: str,
        ) -> AssembledFinancialFact | None:
            return (
                snapshot.facts.get(name)
                if snapshot is not None
                else None
            )

        def ids(*items: AssembledFinancialFact | None) -> tuple[str, ...]:
            return tuple(
                sorted(
                    item.input_record_id
                    for item in items
                    if item is not None
                )
            )

        def derived(
            value: Decimal,
            input_ids: tuple[str, ...],
        ) -> MetricValue:
            return MetricValue(
                value=value,
                quality_status=QualityStatus.DERIVED,
                input_fact_ids=input_ids,
                algorithm_version=self.algorithm_version,
            )

        def missing(
            input_ids: tuple[str, ...],
            reason: str,
        ) -> MetricValue:
            return MetricValue(
                value=None,
                quality_status=QualityStatus.MISSING,
                input_fact_ids=input_ids,
                algorithm_version=self.algorithm_version,
                reason=reason,
            )

        def ratio(
            numerator: AssembledFinancialFact | None,
            denominator: AssembledFinancialFact | None,
            *,
            non_positive_reason: str = "DENOMINATOR_MISSING_OR_ZERO",
        ) -> MetricValue:
            input_ids = ids(numerator, denominator)
            if numerator is None or denominator is None:
                return missing(input_ids, "INPUT_MISSING")
            if denominator.value <= 0:
                return missing(input_ids, non_positive_reason)
            return derived(numerator.value / denominator.value, input_ids)

        def growth(
            current: AssembledFinancialFact | None,
            prior: AssembledFinancialFact | None,
        ) -> MetricValue:
            input_ids = ids(current, prior)
            if current is None or prior is None:
                return missing(input_ids, "INPUT_MISSING")
            if prior.value == 0:
                return missing(input_ids, "DENOMINATOR_MISSING_OR_ZERO")
            return derived(
                (current.value - prior.value) / abs(prior.value),
                input_ids,
            )

        net_profit_2025 = fact(annual_2025, "net_profit")
        equity_2024 = fact(annual_2024, "equity")
        equity_2025 = fact(annual_2025, "equity")
        roe_ids = ids(net_profit_2025, equity_2024, equity_2025)
        if (
            net_profit_2025 is None
            or equity_2024 is None
            or equity_2025 is None
        ):
            roe = missing(roe_ids, "INPUT_MISSING")
        else:
            average_equity = (equity_2024.value + equity_2025.value) / Decimal(2)
            roe = (
                derived(net_profit_2025.value / average_equity, roe_ids)
                if average_equity > 0
                else missing(roe_ids, "DENOMINATOR_MISSING_OR_ZERO")
            )

        interest_2025 = fact(annual_2025, "interest_expense")
        debt_2024 = fact(annual_2024, "interest_bearing_debt")
        debt_2025 = fact(annual_2025, "interest_bearing_debt")
        cash_2024 = fact(annual_2024, "cash_and_equivalents")
        cash_2025 = fact(annual_2025, "cash_and_equivalents")
        roic_inputs = (
            net_profit_2025,
            interest_2025,
            equity_2024,
            debt_2024,
            cash_2024,
            equity_2025,
            debt_2025,
            cash_2025,
        )
        roic_ids = ids(*roic_inputs)
        if any(item is None for item in roic_inputs):
            roic = missing(roic_ids, "INPUT_MISSING")
        else:
            assert all(item is not None for item in roic_inputs)
            invested_2024 = (
                equity_2024.value + debt_2024.value - cash_2024.value
            )
            invested_2025 = (
                equity_2025.value + debt_2025.value - cash_2025.value
            )
            average_invested = (invested_2024 + invested_2025) / Decimal(2)
            nopat = net_profit_2025.value + interest_2025.value * (
                Decimal(1) - self.TAX_RATE_PROXY
            )
            roic = (
                derived(nopat / average_invested, roic_ids)
                if average_invested > 0
                else missing(roic_ids, "DENOMINATOR_MISSING_OR_ZERO")
            )

        metrics: dict[str, MetricValue] = {
            "roe_2025": roe,
            "roic_2025": roic,
            "annual_revenue_growth": growth(
                fact(annual_2025, "revenue"),
                fact(annual_2024, "revenue"),
            ),
            "q1_revenue_growth": growth(
                fact(q1_2026, "revenue"),
                fact(q1_2025, "revenue"),
            ),
            "annual_adjusted_profit_growth": growth(
                fact(annual_2025, "adjusted_net_profit"),
                fact(annual_2024, "adjusted_net_profit"),
            ),
            "q1_adjusted_profit_growth": growth(
                fact(q1_2026, "adjusted_net_profit"),
                fact(q1_2025, "adjusted_net_profit"),
            ),
            "cash_flow_quality": (
                derived(
                    Decimal(0),
                    ids(
                        fact(annual_2025, "operating_cash_flow"),
                        net_profit_2025,
                    ),
                )
                if net_profit_2025 is not None and net_profit_2025.value <= 0
                else ratio(
                    fact(annual_2025, "operating_cash_flow"),
                    net_profit_2025,
                )
            ),
            "debt_ratio": ratio(
                fact(annual_2025, "total_liabilities"),
                fact(annual_2025, "total_assets"),
            ),
            "interest_bearing_debt_ratio": ratio(
                debt_2025,
                fact(annual_2025, "total_assets"),
            ),
            "current_asset_ratio": ratio(
                fact(annual_2025, "current_assets"),
                fact(annual_2025, "total_assets"),
            ),
            "cash_debt_coverage": ratio(cash_2025, debt_2025),
        }

        gross_margins: list[Decimal] = []
        gross_margin_ids: list[str] = []
        for snapshot in (annual_2023, annual_2024, annual_2025):
            revenue = fact(snapshot, "revenue")
            cost = fact(snapshot, "operating_cost")
            gross_margin_ids.extend(ids(revenue, cost))
            if revenue is None or cost is None or revenue.value <= 0:
                gross_margins = []
                break
            gross_margins.append((revenue.value - cost.value) / revenue.value)
        if len(gross_margins) == 3:
            mean = sum(gross_margins, Decimal(0)) / Decimal(3)
            variance = sum(
                ((value - mean) ** 2 for value in gross_margins),
                Decimal(0),
            ) / Decimal(3)
            metrics["gross_margin_stability"] = derived(
                variance.sqrt(),
                tuple(sorted(gross_margin_ids)),
            )
        else:
            metrics["gross_margin_stability"] = missing(
                tuple(sorted(gross_margin_ids)),
                "INPUT_MISSING",
            )

        operating_cash_flow = fact(annual_2025, "operating_cash_flow")
        capital_expenditure = fact(annual_2025, "capital_expenditure")
        fcf_ids = ids(operating_cash_flow, capital_expenditure)
        if operating_cash_flow is None or capital_expenditure is None:
            free_cash_flow = missing(fcf_ids, "INPUT_MISSING")
        else:
            free_cash_flow = derived(
                operating_cash_flow.value - abs(capital_expenditure.value),
                fcf_ids,
            )
        metrics["free_cash_flow"] = free_cash_flow

        market_ids = (
            (
                f"closing-price:{report_cutoff_at.date().isoformat()}:"
                f"{series.ts_code}"
            ),
            share_capital.baseline_fact_id,
            *share_capital.action_record_ids,
        )
        if (
            share_capital.blocked_reasons
            or share_capital.total_shares is None
        ):
            market_cap = missing(market_ids, "SHARE_CAPITAL_BLOCKED")
        elif closing_price <= 0 or share_capital.total_shares <= 0:
            market_cap = missing(market_ids, "MARKET_INPUT_NON_POSITIVE")
        else:
            market_cap = derived(
                closing_price * share_capital.total_shares,
                market_ids,
            )
        metrics["market_cap"] = market_cap

        if market_cap.value is None:
            metrics["pe"] = missing(
                (*market_cap.input_fact_ids, *ids(net_profit_2025)),
                market_cap.reason or "INPUT_MISSING",
            )
            metrics["pb"] = missing(
                (*market_cap.input_fact_ids, *ids(equity_2025)),
                market_cap.reason or "INPUT_MISSING",
            )
        elif net_profit_2025 is None:
            metrics["pe"] = missing(
                (*market_cap.input_fact_ids, *ids(net_profit_2025)),
                "INPUT_MISSING",
            )
        elif net_profit_2025.value <= 0:
            metrics["pe"] = missing(
                (*market_cap.input_fact_ids, *ids(net_profit_2025)),
                "NON_POSITIVE_EARNINGS",
            )
        else:
            metrics["pe"] = derived(
                market_cap.value / net_profit_2025.value,
                (*market_cap.input_fact_ids, *ids(net_profit_2025)),
            )
        if market_cap.value is not None:
            if equity_2025 is None:
                metrics["pb"] = missing(
                    (*market_cap.input_fact_ids, *ids(equity_2025)),
                    "INPUT_MISSING",
                )
            elif equity_2025.value <= 0:
                metrics["pb"] = missing(
                    (*market_cap.input_fact_ids, *ids(equity_2025)),
                    "NON_POSITIVE_EQUITY",
                )
            else:
                metrics["pb"] = derived(
                    market_cap.value / equity_2025.value,
                    (*market_cap.input_fact_ids, *ids(equity_2025)),
                )
        if free_cash_flow.value is None or market_cap.value is None:
            metrics["fcf_yield"] = missing(
                (*free_cash_flow.input_fact_ids, *market_cap.input_fact_ids),
                free_cash_flow.reason or market_cap.reason or "INPUT_MISSING",
            )
        else:
            metrics["fcf_yield"] = derived(
                free_cash_flow.value / market_cap.value,
                (*free_cash_flow.input_fact_ids, *market_cap.input_fact_ids),
            )

        visible_dividends = self._visible_dividends(
            series.ts_code,
            dividends,
            report_cutoff_at,
            known_at,
        )
        ttm_start = report_cutoff_at.date() - timedelta(days=365)
        implemented_ttm = tuple(
            action
            for action in visible_dividends
            if action.action_status is ActionStatus.IMPLEMENTED
            and ttm_start < action.ex_date <= report_cutoff_at.date()
            and action.cash_dividend_per_share is not None
            and action.cash_dividend_per_share > 0
        )
        implemented_ttm_ids = tuple(
            sorted(action.record_id for action in implemented_ttm)
        )
        if not implemented_ttm or closing_price <= 0:
            metrics["implemented_dividend_yield_ttm"] = missing(
                implemented_ttm_ids,
                "INPUT_MISSING",
            )
        else:
            metrics["implemented_dividend_yield_ttm"] = derived(
                sum(
                    (
                        action.cash_dividend_per_share or Decimal(0)
                        for action in implemented_ttm
                    ),
                    Decimal(0),
                )
                / closing_price,
                implemented_ttm_ids,
            )
        by_year: dict[int, list[CorporateAction]] = {}
        for action in visible_dividends:
            if action.fiscal_year is not None:
                by_year.setdefault(action.fiscal_year, []).append(action)

        visible_history = self._visible_dividend_history(
            series.ts_code,
            dividend_history or [],
            report_cutoff_at,
            known_at,
        )
        history_by_year: dict[int, list[AnnualDividendRecord]] = {}
        for record in visible_history:
            history_by_year.setdefault(record.fiscal_year, []).append(record)

        latest_fiscal_year = report_cutoff_at.year - 1
        consecutive = 0
        continuity_ids: list[str] = []
        year = latest_fiscal_year
        while True:
            annual_year_evidence = list(history_by_year.get(year, ()))
            annual_records = [
                record
                for record in annual_year_evidence
                if record.has_cash_dividend
                and record.implementation_status is not ActionStatus.CANCELLED
            ]
            implemented_actions = [
                action
                for action in by_year.get(year, ())
                if action.action_status is ActionStatus.IMPLEMENTED
                and action.cash_dividend_per_share is not None
                and action.cash_dividend_per_share > 0
            ]
            evidence = annual_records or implemented_actions
            if not evidence:
                continuity_ids.extend(
                    record.record_id for record in annual_year_evidence
                )
                break
            consecutive += 1
            continuity_ids.extend(record.record_id for record in evidence)
            year -= 1
        metrics["consecutive_dividend_years"] = derived(
            Decimal(consecutive),
            tuple(sorted(continuity_ids)),
        )

        current_history = [
            record
            for record in history_by_year.get(latest_fiscal_year, ())
            if record.implementation_status is not ActionStatus.CANCELLED
        ]
        prior_history = [
            record
            for record in history_by_year.get(latest_fiscal_year - 1, ())
            if record.implementation_status is not ActionStatus.CANCELLED
        ]
        current = current_history or [
            action
            for action in by_year.get(latest_fiscal_year, ())
            if action.action_status is not ActionStatus.CANCELLED
        ]
        prior = prior_history or [
            action
            for action in by_year.get(latest_fiscal_year - 1, ())
            if action.action_status is not ActionStatus.CANCELLED
        ]
        current_ids = tuple(sorted(record.record_id for record in current))
        prior_ids = tuple(sorted(record.record_id for record in prior))
        current_dps = (
            sum(
                (
                    action.cash_dividend_per_share or Decimal(0)
                    for action in current
                ),
                Decimal(0),
            )
            if current
            else None
        )
        prior_dps = (
            sum(
                (
                    action.cash_dividend_per_share or Decimal(0)
                    for action in prior
                ),
                Decimal(0),
            )
            if prior
            else None
        )
        if current_dps is None or closing_price <= 0:
            metrics["announced_dividend_yield"] = missing(
                current_ids,
                "INPUT_MISSING",
            )
        else:
            metrics["announced_dividend_yield"] = derived(
                current_dps / closing_price,
                current_ids,
            )

        def annual_cash_total(action: object) -> Decimal | None:
            if (
                isinstance(action, AnnualDividendRecord)
                and not action.has_cash_dividend
            ):
                return Decimal(0)
            if action.cash_dividend_total is not None:
                return action.cash_dividend_total
            if (
                action.cash_dividend_per_share is not None
                and share_capital.total_shares is not None
                and not share_capital.blocked_reasons
            ):
                return action.cash_dividend_per_share * share_capital.total_shares
            return None

        totals_available = bool(current) and all(
            annual_cash_total(action) is not None for action in current
        )
        total_dividend = (
            sum(
                (
                    annual_cash_total(action) or Decimal(0)
                    for action in current
                ),
                Decimal(0),
            )
            if totals_available
            else None
        )
        total_uses_share_capital = bool(current) and any(
            action.cash_dividend_total is None
            and action.cash_dividend_per_share is not None
            for action in current
        )
        share_capital_ids = (
            (
                share_capital.baseline_fact_id,
                *share_capital.action_record_ids,
            )
            if total_uses_share_capital
            and share_capital.total_shares is not None
            and not share_capital.blocked_reasons
            else ()
        )
        payout_ids = tuple(
            sorted({*current_ids, *ids(net_profit_2025), *share_capital_ids})
        )
        if total_dividend is None or net_profit_2025 is None:
            metrics["payout_ratio"] = missing(payout_ids, "INPUT_MISSING")
        elif total_dividend == 0:
            metrics["payout_ratio"] = derived(
                Decimal(0),
                payout_ids,
            )
        elif net_profit_2025.value <= 0:
            metrics["payout_ratio"] = missing(
                payout_ids,
                "NON_POSITIVE_EARNINGS",
            )
        else:
            metrics["payout_ratio"] = derived(
                total_dividend / net_profit_2025.value,
                payout_ids,
            )
        coverage_ids = tuple(
            sorted({*current_ids, *free_cash_flow.input_fact_ids, *share_capital_ids})
        )
        if total_dividend == 0 and free_cash_flow.value is not None:
            metrics["fcf_coverage"] = derived(
                Decimal(0),
                coverage_ids,
            )
        elif (
            total_dividend is None
            or total_dividend < 0
            or free_cash_flow.value is None
        ):
            metrics["fcf_coverage"] = missing(
                coverage_ids,
                "DENOMINATOR_MISSING_OR_ZERO"
                if total_dividend is not None
                else "INPUT_MISSING",
            )
        else:
            metrics["fcf_coverage"] = derived(
                free_cash_flow.value / total_dividend,
                coverage_ids,
            )
        if current_dps is None or prior_dps is None:
            metrics["dividend_cut_flag"] = missing(
                (*current_ids, *prior_ids),
                "DIVIDEND_HISTORY_MISSING",
            )
        else:
            metrics["dividend_cut_flag"] = derived(
                Decimal(1) if current_dps < prior_dps else Decimal(0),
                (*current_ids, *prior_ids),
            )

        return PilotMetricResult(
            metrics=metrics,
            blocked_reasons=(),
            report_cutoff_at=report_cutoff_at,
            known_at=known_at,
            tax_rate_proxy=self.TAX_RATE_PROXY,
        )

    @staticmethod
    def _visible_dividend_history(
        ts_code: str,
        records: list[AnnualDividendRecord],
        report_cutoff_at: datetime,
        known_at: datetime,
    ) -> tuple[AnnualDividendRecord, ...]:
        candidates = {
            record.record_id: record
            for record in records
            if record.ts_code == ts_code
            and record.published_at is not None
            and record.published_at <= report_cutoff_at
            and record.collected_at <= known_at
            and record.valid_from <= known_at
            and record.quality_status
            in {QualityStatus.VALID, QualityStatus.DERIVED}
        }
        superseded_ids = {
            record.supersedes_id
            for record in candidates.values()
            if record.supersedes_id in candidates
        }
        return tuple(
            record
            for record in candidates.values()
            if record.record_id not in superseded_ids
            and record.implementation_status is not ActionStatus.CANCELLED
        )

    @staticmethod
    def _visible_dividends(
        ts_code: str,
        dividends: list[CorporateAction],
        report_cutoff_at: datetime,
        known_at: datetime,
    ) -> tuple[CorporateAction, ...]:
        candidates = {
            action.record_id: action
            for action in dividends
            if action.ts_code == ts_code
            and action.action_type is ActionType.CASH_DIVIDEND
            and action.published_at is not None
            and action.published_at <= report_cutoff_at
            and action.collected_at <= known_at
            and action.valid_from <= known_at
            and action.quality_status
            in {QualityStatus.VALID, QualityStatus.DERIVED}
        }
        superseded_ids = {
            action.supersedes_id
            for action in candidates.values()
            if action.supersedes_id in candidates
        }
        return tuple(
            action
            for action in candidates.values()
            if action.record_id not in superseded_ids
            and action.action_status is not ActionStatus.CANCELLED
        )

    @staticmethod
    def _require_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("PILOT_METRIC_CUTOFF_INVALID")
