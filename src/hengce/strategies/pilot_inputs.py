from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal

from hengce.contracts.enums import QualityStatus, StrategyType
from hengce.contracts.pilot import PilotUniverseSnapshot
from hengce.financials.metrics import MetricValue, PilotMetricResult
from hengce.strategies.engine import FactorInput, SecurityStrategyInput
from hengce.strategies.filters import HardFilterResult

_QUALITY_NAMES = (
    "capital_return",
    "growth_quality",
    "cash_flow_quality",
    "profitability_stability",
    "balance_sheet_quality",
    "valuation_attractiveness",
)
_VALUE_NAMES = (
    "absolute_valuation",
    "relative_valuation",
    "asset_quality",
    "cash_debt_quality",
    "cycle_position",
    "value_trap_safety",
)
_DIVIDEND_NAMES = (
    "dividend_yield",
    "dividend_continuity",
    "payout_sustainability",
    "cashflow_coverage",
    "balance_sheet_quality",
    "dividend_cut_safety",
)


class PilotStrategyInputBuilder:
    def build(
        self,
        universe: PilotUniverseSnapshot,
        metrics: Mapping[str, PilotMetricResult],
        hard_filters: Mapping[str, HardFilterResult],
        report_cutoff_at: datetime,
        known_at: datetime,
    ) -> dict[StrategyType, list[SecurityStrategyInput]]:
        self._require_aware(report_cutoff_at)
        self._require_aware(known_at)
        if universe.report_cutoff_at != report_cutoff_at:
            raise ValueError("PILOT_STRATEGY_UNIVERSE_CUTOFF_MISMATCH")

        codes = tuple(member.ts_code for member in universe.members)
        percentiles = {
            name: self._metric_percentiles(codes, metrics, name)
            for name in {
                "roe_2025",
                "roic_2025",
                "annual_revenue_growth",
                "annual_adjusted_profit_growth",
                "q1_revenue_growth",
                "q1_adjusted_profit_growth",
                "cash_flow_quality",
                "debt_ratio",
                "cash_debt_coverage",
                "pe",
                "pb",
                "fcf_yield",
                "current_asset_ratio",
            }
        }
        result = {strategy: [] for strategy in StrategyType}
        for member in universe.members:
            code = member.ts_code
            metric_result = metrics.get(code)
            hard_filter = hard_filters.get(
                code,
                HardFilterResult(
                    passed=False,
                    reasons=("HARD_FILTER_MISSING",),
                    filter_version="missing",
                ),
            )
            metric_map = metric_result.metrics if metric_result is not None else {}
            common_balance = self._percentile_average(
                code,
                metric_map,
                percentiles,
                (("debt_ratio", False), ("cash_debt_coverage", True)),
            )
            quality_factors = {
                "capital_return": self._percentile_average(
                    code,
                    metric_map,
                    percentiles,
                    (("roe_2025", True), ("roic_2025", True)),
                ),
                "growth_quality": self._percentile_average(
                    code,
                    metric_map,
                    percentiles,
                    (
                        ("annual_revenue_growth", True),
                        ("annual_adjusted_profit_growth", True),
                        ("q1_revenue_growth", True),
                        ("q1_adjusted_profit_growth", True),
                    ),
                ),
                "cash_flow_quality": self._direct(
                    metric_map,
                    "cash_flow_quality",
                ),
                "profitability_stability": self._direct(
                    metric_map,
                    "gross_margin_stability",
                ),
                "balance_sheet_quality": common_balance,
                "valuation_attractiveness": self._valuation_percentile_average(
                    code,
                    metric_map,
                    percentiles,
                    (("pe", False), ("pb", False)),
                ),
            }
            value_factors = {
                "absolute_valuation": self._valuation_percentile_average(
                    code,
                    metric_map,
                    percentiles,
                    (
                        ("pe", False),
                        ("pb", False),
                        ("fcf_yield", True),
                    ),
                ),
                "relative_valuation": self._valuation_percentile_average(
                    code,
                    metric_map,
                    percentiles,
                    (("pe", False), ("pb", False)),
                ),
                "asset_quality": self._percentile_average(
                    code,
                    metric_map,
                    percentiles,
                    (
                        ("current_asset_ratio", True),
                        ("cash_flow_quality", True),
                    ),
                ),
                "cash_debt_quality": common_balance,
                "cycle_position": self._cycle_position(metric_map),
                "value_trap_safety": self._value_trap_safety(
                    code,
                    metric_map,
                    percentiles,
                    hard_filter,
                ),
            }
            dividend_factors = {
                "dividend_yield": self._direct(
                    metric_map,
                    "announced_dividend_yield",
                ),
                "dividend_continuity": self._dividend_continuity(metric_map),
                "payout_sustainability": self._payout_sustainability(
                    metric_map
                ),
                "cashflow_coverage": self._direct(
                    metric_map,
                    "fcf_coverage",
                ),
                "balance_sheet_quality": common_balance,
                "dividend_cut_safety": self._dividend_cut_safety(metric_map),
            }
            payout = metric_map.get("payout_ratio")
            payout_risk = bool(
                self._usable(payout)
                and payout is not None
                and payout.value is not None
                and not Decimal(0) < payout.value <= Decimal(1)
            )
            base_risks = tuple(f"硬过滤:{reason}" for reason in hard_filter.reasons)
            if payout_risk:
                base_risks = (*base_risks, "派息率不在(0,1]区间")

            result[StrategyType.QUALITY_GROWTH].append(
                self._security_input(
                    code,
                    member.security_name,
                    getattr(member, "industry_l1", None),
                    quality_factors,
                    hard_filter,
                    report_cutoff_at,
                    known_at,
                    base_risks,
                    member.evidence_record_ids,
                    announced_dividend_only=True,
                )
            )
            result[StrategyType.DEEP_VALUE].append(
                self._security_input(
                    code,
                    member.security_name,
                    getattr(member, "industry_l1", None),
                    value_factors,
                    hard_filter,
                    report_cutoff_at,
                    known_at,
                    base_risks,
                    member.evidence_record_ids,
                    cycle_position_available=(
                        value_factors["cycle_position"].value is not None
                    ),
                    announced_dividend_only=True,
                )
            )
            result[StrategyType.STABLE_DIVIDEND].append(
                self._security_input(
                    code,
                    member.security_name,
                    getattr(member, "industry_l1", None),
                    dividend_factors,
                    hard_filter,
                    report_cutoff_at,
                    known_at,
                    base_risks,
                    member.evidence_record_ids,
                    announced_dividend_only=(
                        dividend_factors["dividend_yield"].value is not None
                    ),
                )
            )
        return result

    @staticmethod
    def _security_input(
        code: str,
        security_name: str,
        industry_l1: str | None,
        factors: dict[str, FactorInput],
        hard_filter: HardFilterResult,
        report_cutoff_at: datetime,
        known_at: datetime,
        risk_flags: tuple[str, ...],
        fallback_source_ids: tuple[str, ...],
        *,
        cycle_position_available: bool = True,
        announced_dividend_only: bool,
    ) -> SecurityStrategyInput:
        timed_factors = {
            name: FactorInput(
                value=factor.value,
                quality_status=factor.quality_status,
                source_record_ids=(
                    factor.source_record_ids or fallback_source_ids
                ),
                published_at=report_cutoff_at,
                effective_at=report_cutoff_at,
                collected_at=known_at,
                valid_from=known_at,
            )
            for name, factor in factors.items()
        }
        return SecurityStrategyInput(
            ts_code=code,
            security_name=security_name,
            industry_l1=industry_l1,
            factors=timed_factors,
            hard_filter_passed=hard_filter.passed,
            selection_reasons=("试点策略规则满足",),
            risk_flags=risk_flags,
            catalysts=("暂无可验证官方催化剂",),
            observe_conditions=("观察下一期同口径关键因子",),
            invalidate_conditions=("关键因子失效或触发硬过滤",),
            cycle_position_available=cycle_position_available,
            announced_dividend_only=announced_dividend_only,
        )

    def _percentile_average(
        self,
        code: str,
        metric_map: Mapping[str, MetricValue],
        percentiles: Mapping[str, Mapping[str, Decimal]],
        components: tuple[tuple[str, bool], ...],
    ) -> FactorInput:
        values: list[Decimal] = []
        source_ids: list[str] = []
        for name, higher_is_better in components:
            metric = metric_map.get(name)
            percentile = percentiles[name].get(code)
            if not self._usable(metric) or percentile is None:
                return self._missing(metric_map, tuple(name for name, _ in components))
            values.append(
                percentile
                if higher_is_better
                else Decimal(100) - percentile
            )
            assert metric is not None
            source_ids.extend(metric.input_fact_ids)
        return self._factor(
            sum(values, Decimal(0)) / Decimal(len(values)),
            tuple(sorted(set(source_ids))),
        )

    def _valuation_percentile_average(
        self,
        code: str,
        metric_map: Mapping[str, MetricValue],
        percentiles: Mapping[str, Mapping[str, Decimal]],
        components: tuple[tuple[str, bool], ...],
    ) -> FactorInput:
        """Score an observed loss as adverse while preserving unknowns as missing."""
        values: list[Decimal] = []
        source_ids: list[str] = []
        for name, higher_is_better in components:
            metric = metric_map.get(name)
            if (
                name == "pe"
                and metric is not None
                and metric.value is None
                and metric.reason == "NON_POSITIVE_EARNINGS"
                and metric.input_fact_ids
            ):
                values.append(Decimal(0))
                source_ids.extend(metric.input_fact_ids)
                continue
            percentile = percentiles[name].get(code)
            if not self._usable(metric) or percentile is None:
                return self._missing(metric_map, tuple(name for name, _ in components))
            values.append(
                percentile
                if higher_is_better
                else Decimal(100) - percentile
            )
            assert metric is not None
            source_ids.extend(metric.input_fact_ids)
        return self._factor(
            sum(values, Decimal(0)) / Decimal(len(values)),
            tuple(sorted(set(source_ids))),
        )

    def _direct(
        self,
        metric_map: Mapping[str, MetricValue],
        name: str,
    ) -> FactorInput:
        metric = metric_map.get(name)
        if not self._usable(metric):
            return self._missing(metric_map, (name,))
        assert metric is not None and metric.value is not None
        return self._factor(metric.value, metric.input_fact_ids)

    def _cycle_position(
        self,
        metric_map: Mapping[str, MetricValue],
    ) -> FactorInput:
        names = (
            "annual_revenue_growth",
            "annual_adjusted_profit_growth",
            "q1_revenue_growth",
            "q1_adjusted_profit_growth",
        )
        items = [metric_map.get(name) for name in names]
        if not all(self._usable(item) for item in items):
            return self._missing(metric_map, names)
        assert all(item is not None and item.value is not None for item in items)
        value = (
            items[2].value
            - items[0].value
            + items[3].value
            - items[1].value
        ) / Decimal(2)
        return self._factor(
            value,
            self._source_ids(metric_map, names),
        )

    def _value_trap_safety(
        self,
        code: str,
        metric_map: Mapping[str, MetricValue],
        percentiles: Mapping[str, Mapping[str, Decimal]],
        hard_filter: HardFilterResult,
    ) -> FactorInput:
        names = ("cash_flow_quality", "annual_adjusted_profit_growth")
        if (
            any(not self._usable(metric_map.get(name)) for name in names)
            or any(code not in percentiles[name] for name in names)
            or not hard_filter.source_record_ids
        ):
            return self._missing(metric_map, names)
        rule_score = (
            Decimal(100)
            if not {"HF-07", "HF-08"}.intersection(hard_filter.reasons)
            else Decimal(0)
        )
        value = (
            percentiles["cash_flow_quality"][code]
            + percentiles["annual_adjusted_profit_growth"][code]
            + rule_score
        ) / Decimal(3)
        return self._factor(
            value,
            tuple(
                sorted(
                    {
                        *self._source_ids(metric_map, names),
                        *hard_filter.source_record_ids,
                    }
                )
            ),
        )

    def _dividend_continuity(
        self,
        metric_map: Mapping[str, MetricValue],
    ) -> FactorInput:
        metric = metric_map.get("consecutive_dividend_years")
        if not self._usable(metric):
            return self._missing(metric_map, ("consecutive_dividend_years",))
        assert metric is not None and metric.value is not None
        return self._factor(
            min(metric.value, Decimal(5)) / Decimal(5),
            metric.input_fact_ids,
        )

    def _payout_sustainability(
        self,
        metric_map: Mapping[str, MetricValue],
    ) -> FactorInput:
        metric = metric_map.get("payout_ratio")
        if not self._usable(metric):
            return self._missing(metric_map, ("payout_ratio",))
        assert metric is not None and metric.value is not None
        value = Decimal(1) - abs(metric.value - Decimal("0.5")) / Decimal(
            "0.5"
        )
        return self._factor(value, metric.input_fact_ids)

    def _dividend_cut_safety(
        self,
        metric_map: Mapping[str, MetricValue],
    ) -> FactorInput:
        names = ("dividend_cut_flag", "consecutive_dividend_years")
        cut = metric_map.get(names[0])
        years = metric_map.get(names[1])
        if not self._usable(cut) or not self._usable(years):
            return self._missing(metric_map, names)
        assert cut is not None and cut.value is not None
        assert years is not None and years.value is not None
        value = (
            Decimal(1)
            if cut.value == 0 and years.value >= Decimal(3)
            else Decimal(0)
        )
        return self._factor(value, self._source_ids(metric_map, names))

    def _missing(
        self,
        metric_map: Mapping[str, MetricValue],
        names: tuple[str, ...],
    ) -> FactorInput:
        return self._factor(
            None,
            self._source_ids(metric_map, names),
        )

    @staticmethod
    def _source_ids(
        metric_map: Mapping[str, MetricValue],
        names: tuple[str, ...],
    ) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    record_id
                    for name in names
                    for record_id in (
                        metric_map[name].input_fact_ids
                        if name in metric_map
                        else ()
                    )
                }
            )
        )

    @staticmethod
    def _factor(
        value: Decimal | None,
        source_record_ids: tuple[str, ...],
    ) -> FactorInput:
        return FactorInput(
            value=value,
            quality_status=(
                QualityStatus.DERIVED
                if value is not None
                else QualityStatus.MISSING
            ),
            source_record_ids=source_record_ids,
            published_at=datetime.min.replace(tzinfo=UTC),
            effective_at=datetime.min.replace(tzinfo=UTC),
            collected_at=datetime.min.replace(tzinfo=UTC),
            valid_from=datetime.min.replace(tzinfo=UTC),
        )

    @classmethod
    def _metric_percentiles(
        cls,
        codes: tuple[str, ...],
        metrics: Mapping[str, PilotMetricResult],
        name: str,
    ) -> dict[str, Decimal]:
        values = {
            code: metric.value
            for code in codes
            if (result := metrics.get(code)) is not None
            and cls._usable(metric := result.metrics.get(name))
            and metric is not None
            and metric.value is not None
        }
        ordered = sorted(values.values())
        return {
            code: cls._percentile(value, ordered)
            for code, value in values.items()
        }

    @staticmethod
    def _percentile(value: Decimal, values: list[Decimal]) -> Decimal:
        if len(values) <= 1:
            return Decimal(50)
        below = sum(1 for candidate in values if candidate < value)
        equal = sum(1 for candidate in values if candidate == value)
        rank = Decimal(below) + Decimal(equal - 1) / Decimal(2)
        return rank / Decimal(len(values) - 1) * Decimal(100)

    @staticmethod
    def _usable(metric: MetricValue | None) -> bool:
        return bool(
            metric is not None
            and metric.value is not None
            and metric.quality_status
            in {QualityStatus.VALID, QualityStatus.DERIVED}
            and metric.input_fact_ids
        )

    @staticmethod
    def _require_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("PILOT_STRATEGY_CUTOFF_INVALID")

__all__ = ["PilotStrategyInputBuilder"]
