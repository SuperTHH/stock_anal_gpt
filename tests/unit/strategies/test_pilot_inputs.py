from datetime import UTC, date, datetime
from decimal import Decimal

from hengce.contracts.enums import QualityStatus, StrategyType
from hengce.contracts.pilot import PilotUniverseMember, PilotUniverseSnapshot
from hengce.financials.metrics import (
    MetricValue,
    PilotMetricResult,
)
from hengce.strategies.filters import HardFilterResult
from hengce.strategies.pilot_inputs import PilotStrategyInputBuilder
from hengce.strategies.quality_growth import QUALITY_GROWTH_V1

CUTOFF = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)


def universe() -> PilotUniverseSnapshot:
    members = (
        ("699991.SH", "MAIN_SH"),
        ("699992.SH", "STAR"),
        ("399991.SZ", "MAIN_SZ"),
        ("399992.SZ", "CHINEXT"),
    )
    return PilotUniverseSnapshot(
        universe_id="pilot-fixture",
        market_date=date(2026, 7, 22),
        report_cutoff_at=CUTOFF,
        algorithm_version="fixture-v1",
        quotas={
            "MAIN_SH": 1,
            "STAR": 1,
            "MAIN_SZ": 1,
            "CHINEXT": 1,
        },
        members=tuple(
            PilotUniverseMember(
                ts_code=ts_code,
                security_name=f"示例{index}",
                board=board,
                amount=Decimal("100000000"),
                rank_in_board=1,
                evidence_record_ids=(f"universe-{ts_code}",),
            )
            for index, (ts_code, board) in enumerate(members)
        ),
        input_hashes={"market": "a" * 64},
        manifest_hash="b" * 64,
        created_at=CUTOFF,
    )


def metric(name: str, value: str | None, code: str) -> MetricValue:
    return MetricValue(
        value=Decimal(value) if value is not None else None,
        quality_status=(
            QualityStatus.DERIVED if value is not None else QualityStatus.MISSING
        ),
        input_fact_ids=(f"{code}:{name}",),
        algorithm_version="pilot-financial-metrics-v1",
        reason=None if value is not None else "INPUT_MISSING",
    )


def pilot_metrics(code: str, offset: int) -> PilotMetricResult:
    value = Decimal(offset)
    values = {
        "roe_2025": str(Decimal("0.10") + value / Decimal("100")),
        "roic_2025": str(Decimal("0.08") + value / Decimal("100")),
        "annual_revenue_growth": str(Decimal("0.10") + value / Decimal("100")),
        "annual_adjusted_profit_growth": str(
            Decimal("0.08") + value / Decimal("100")
        ),
        "q1_revenue_growth": str(Decimal("0.12") + value / Decimal("100")),
        "q1_adjusted_profit_growth": str(
            Decimal("0.10") + value / Decimal("100")
        ),
        "cash_flow_quality": str(Decimal("1") + value / Decimal("10")),
        "gross_margin_stability": str(Decimal("0.05") - value / Decimal("1000")),
        "debt_ratio": str(Decimal("0.60") - value / Decimal("100")),
        "cash_debt_coverage": str(Decimal("0.5") + value / Decimal("10")),
        "pe": str(Decimal("20") - value),
        "pb": str(Decimal("3") - value / Decimal("10")),
        "fcf_yield": str(Decimal("0.03") + value / Decimal("100")),
        "current_asset_ratio": str(Decimal("0.4") + value / Decimal("100")),
        "consecutive_dividend_years": str(min(5, 2 + offset)),
        "announced_dividend_yield": str(
            Decimal("0.02") + value / Decimal("100")
        ),
        "payout_ratio": "0.5",
        "fcf_coverage": str(Decimal("1") + value / Decimal("10")),
        "dividend_cut_flag": "0",
    }
    return PilotMetricResult(
        metrics={name: metric(name, raw, code) for name, raw in values.items()},
        blocked_reasons=(),
        report_cutoff_at=CUTOFF,
        known_at=CUTOFF,
        tax_rate_proxy=Decimal("0.25"),
    )


def filters(snapshot: PilotUniverseSnapshot) -> dict[str, HardFilterResult]:
    return {
        member.ts_code: HardFilterResult(
            passed=True,
            reasons=(),
            filter_version="hard-filter-v1",
            source_record_ids=(f"filter-{member.ts_code}",),
        )
        for member in snapshot.members
    }


def test_builds_three_strategy_local_factor_sets_with_frozen_composition() -> None:
    """Catches cross-pool factor reuse, reversed directions, and lost metric lineage."""
    snapshot = universe()
    results = {
        member.ts_code: pilot_metrics(member.ts_code, index)
        for index, member in enumerate(snapshot.members, start=1)
    }

    built = PilotStrategyInputBuilder().build(
        snapshot,
        results,
        filters(snapshot),
        CUTOFF,
        CUTOFF,
    )

    assert set(built) == set(StrategyType)
    assert all(len(items) == 4 for items in built.values())
    assert built[StrategyType.QUALITY_GROWTH][0].security_name == (
        snapshot.members[0].security_name
    )
    quality = built[StrategyType.QUALITY_GROWTH]
    value = built[StrategyType.DEEP_VALUE]
    dividend = built[StrategyType.STABLE_DIVIDEND]
    assert set(quality[0].factors) == {
        "capital_return",
        "growth_quality",
        "cash_flow_quality",
        "profitability_stability",
        "balance_sheet_quality",
        "valuation_attractiveness",
    }
    assert set(value[0].factors) == {
        "absolute_valuation",
        "relative_valuation",
        "asset_quality",
        "cash_debt_quality",
        "cycle_position",
        "value_trap_safety",
    }
    assert set(dividend[0].factors) == {
        "dividend_yield",
        "dividend_continuity",
        "payout_sustainability",
        "cashflow_coverage",
        "balance_sheet_quality",
        "dividend_cut_safety",
    }
    assert quality[-1].factors["capital_return"].value > quality[0].factors[
        "capital_return"
    ].value
    assert value[-1].factors["absolute_valuation"].value > value[0].factors[
        "absolute_valuation"
    ].value
    assert dividend[-1].factors["dividend_continuity"].value == Decimal("1")
    assert quality[-1].factors["profitability_stability"].value < quality[
        0
    ].factors["profitability_stability"].value
    assert all(
        factor.source_record_ids
        for items in built.values()
        for item in items
        for factor in item.factors.values()
        if factor.value is not None
    )
    stability_spec = next(
        spec
        for spec in QUALITY_GROWTH_V1.factors
        if spec.name == "profitability_stability"
    )
    assert stability_spec.higher_is_better is False


def test_negative_pe_and_missing_announced_dividend_are_not_rewarded() -> None:
    snapshot = universe()
    results = {
        member.ts_code: pilot_metrics(member.ts_code, index)
        for index, member in enumerate(snapshot.members, start=1)
    }
    target = snapshot.members[0].ts_code
    target_result = results[target]
    target_result.metrics["pe"] = metric("pe", None, target)
    target_result.metrics["announced_dividend_yield"] = metric(
        "announced_dividend_yield",
        None,
        target,
    )

    built = PilotStrategyInputBuilder().build(
        snapshot,
        results,
        filters(snapshot),
        CUTOFF,
        CUTOFF,
    )

    quality = built[StrategyType.QUALITY_GROWTH][0]
    value = built[StrategyType.DEEP_VALUE][0]
    dividend = built[StrategyType.STABLE_DIVIDEND][0]
    assert quality.factors["valuation_attractiveness"].value is None
    assert value.factors["absolute_valuation"].value is None
    assert value.factors["relative_valuation"].value is None
    assert dividend.announced_dividend_only is False


def test_non_positive_earnings_receives_a_lineaged_valuation_penalty() -> None:
    """An observed loss is adverse evidence, not an unknown valuation input."""
    snapshot = universe()
    results = {
        member.ts_code: pilot_metrics(member.ts_code, index)
        for index, member in enumerate(snapshot.members, start=1)
    }
    target = snapshot.members[0].ts_code
    results[target].metrics["pe"] = MetricValue(
        value=None,
        quality_status=QualityStatus.MISSING,
        input_fact_ids=(f"{target}:market-cap", f"{target}:net-profit"),
        algorithm_version="pilot-financial-metrics-v1",
        reason="NON_POSITIVE_EARNINGS",
    )

    built = PilotStrategyInputBuilder().build(
        snapshot,
        results,
        filters(snapshot),
        CUTOFF,
        CUTOFF,
    )

    quality = built[StrategyType.QUALITY_GROWTH][0]
    value = built[StrategyType.DEEP_VALUE][0]
    assert quality.factors["valuation_attractiveness"].value is not None
    assert value.factors["absolute_valuation"].value is not None
    assert value.factors["relative_valuation"].value is not None
    assert quality.factors["valuation_attractiveness"].value < built[
        StrategyType.QUALITY_GROWTH
    ][1].factors["valuation_attractiveness"].value
    assert f"{target}:net-profit" in quality.factors[
        "valuation_attractiveness"
    ].source_record_ids
