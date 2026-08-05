from datetime import UTC, date, datetime
from decimal import Decimal

from hengce.contracts.enums import (
    PoolReadinessStatus,
    QualityStatus,
    StrategyType,
)
from hengce.contracts.pilot import PilotUniverseMember, PilotUniverseSnapshot
from hengce.strategies.deep_value import DEEP_VALUE_V1
from hengce.strategies.engine import FactorInput, SecurityStrategyInput
from hengce.strategies.quality_growth import QUALITY_GROWTH_V1
from hengce.strategies.readiness import (
    IndependentPoolRunner,
    PoolReadinessEvaluator,
)
from hengce.strategies.stable_dividend import STABLE_DIVIDEND_V1

CUTOFF = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)


def universe_30() -> PilotUniverseSnapshot:
    board_specs = (
        ("MAIN_SH", "60", "SH", 8),
        ("STAR", "68", "SH", 7),
        ("MAIN_SZ", "00", "SZ", 8),
        ("CHINEXT", "30", "SZ", 7),
    )
    members: list[PilotUniverseMember] = []
    for board, prefix, exchange, quota in board_specs:
        for rank in range(1, quota + 1):
            symbol = f"{prefix}{rank:04d}"
            members.append(
                PilotUniverseMember(
                    ts_code=f"{symbol}.{exchange}",
                    security_name=f"示例{symbol}",
                    board=board,
                    amount=Decimal("100000000"),
                    rank_in_board=rank,
                    evidence_record_ids=(f"member-{symbol}",),
                )
            )
    return PilotUniverseSnapshot(
        universe_id="pilot-30",
        market_date=date(2026, 7, 22),
        report_cutoff_at=CUTOFF,
        algorithm_version="fixture-v1",
        quotas={
            "MAIN_SH": 8,
            "STAR": 7,
            "MAIN_SZ": 8,
            "CHINEXT": 7,
        },
        members=tuple(members),
        input_hashes={"market": "a" * 64},
        manifest_hash="b" * 64,
        created_at=CUTOFF,
    )


def strategy_input(
    ts_code: str,
    *,
    complete: bool = True,
    lineage: bool = True,
    hard_filter_passed: bool = True,
) -> SecurityStrategyInput:
    factors = {
        spec.name: FactorInput(
            value=(
                Decimal("1")
                if complete or not spec.critical
                else None
            ),
            quality_status=(
                QualityStatus.DERIVED
                if complete or not spec.critical
                else QualityStatus.MISSING
            ),
            source_record_ids=(
                (f"{ts_code}:{spec.name}",)
                if lineage or not spec.critical
                else ()
            ),
            published_at=CUTOFF,
            effective_at=CUTOFF,
            collected_at=CUTOFF,
            valid_from=CUTOFF,
        )
        for spec in QUALITY_GROWTH_V1.factors
    }
    return SecurityStrategyInput(
        ts_code=ts_code,
        industry_l1=None,
        factors=factors,
        hard_filter_passed=hard_filter_passed,
        selection_reasons=(),
        risk_flags=(),
        catalysts=("暂无可验证官方催化剂",),
        observe_conditions=(),
        invalidate_conditions=(),
        cycle_position_available=True,
        announced_dividend_only=True,
    )


def inputs_with_complete_count(count: int) -> list[SecurityStrategyInput]:
    snapshot = universe_30()
    return [
        strategy_input(member.ts_code, complete=index < count)
        for index, member in enumerate(snapshot.members)
    ]


def test_readiness_boundary_is_exactly_twenty_four_of_thirty() -> None:
    evaluator = PoolReadinessEvaluator()
    snapshot = universe_30()

    blocked = evaluator.evaluate(
        QUALITY_GROWTH_V1,
        inputs_with_complete_count(23),
        snapshot,
    )
    ready = evaluator.evaluate(
        QUALITY_GROWTH_V1,
        inputs_with_complete_count(24),
        snapshot,
    )
    full = evaluator.evaluate(
        QUALITY_GROWTH_V1,
        inputs_with_complete_count(30),
        snapshot,
    )

    assert blocked.complete_factor_count == 23
    assert blocked.coverage_ratio == Decimal(23) / Decimal(30)
    assert blocked.status is PoolReadinessStatus.BLOCKED
    assert blocked.blocking_codes == ("POOL_FACTOR_COVERAGE_BELOW_80_PERCENT",)
    assert ready.complete_factor_count == 24
    assert ready.coverage_ratio == Decimal("0.8")
    assert ready.status is PoolReadinessStatus.READY
    assert full.coverage_ratio == Decimal("1")


def test_missing_critical_lineage_is_incomplete_even_when_value_exists() -> None:
    snapshot = universe_30()
    inputs = inputs_with_complete_count(30)
    inputs[0] = strategy_input(
        snapshot.members[0].ts_code,
        complete=True,
        lineage=False,
    )

    readiness = PoolReadinessEvaluator().evaluate(
        QUALITY_GROWTH_V1,
        inputs,
        snapshot,
    )

    assert readiness.complete_factor_count == 29
    assert readiness.status is PoolReadinessStatus.READY
    assert readiness.missing_by_security[snapshot.members[0].ts_code] == (
        "capital_return:SOURCE_LINEAGE_MISSING",
        "cash_flow_quality:SOURCE_LINEAGE_MISSING",
        "growth_quality:SOURCE_LINEAGE_MISSING",
        "valuation_attractiveness:SOURCE_LINEAGE_MISSING",
    )


def test_hard_filtered_security_is_not_eligible_or_complete() -> None:
    snapshot = universe_30()
    inputs = inputs_with_complete_count(30)
    inputs[0] = strategy_input(
        snapshot.members[0].ts_code,
        hard_filter_passed=False,
    )

    readiness = PoolReadinessEvaluator().evaluate(
        QUALITY_GROWTH_V1,
        inputs,
        snapshot,
    )

    assert readiness.eligible_count == 29
    assert readiness.complete_factor_count == 29


def inputs_for_definition(
    definition: object,
    *,
    complete_count: int,
) -> list[SecurityStrategyInput]:
    snapshot = universe_30()
    return [
        SecurityStrategyInput(
            ts_code=member.ts_code,
            industry_l1=None,
            factors={
                spec.name: FactorInput(
                    value=(
                        Decimal(index + 1)
                        if index < complete_count or not spec.critical
                        else None
                    ),
                    quality_status=(
                        QualityStatus.DERIVED
                        if index < complete_count or not spec.critical
                        else QualityStatus.MISSING
                    ),
                    source_record_ids=(f"{member.ts_code}:{spec.name}",),
                    published_at=CUTOFF,
                    effective_at=CUTOFF,
                    collected_at=CUTOFF,
                    valid_from=CUTOFF,
                )
                for spec in definition.factors
            },
            hard_filter_passed=True,
            selection_reasons=(),
            risk_flags=(),
            catalysts=("暂无可验证官方催化剂",),
            observe_conditions=(),
            invalidate_conditions=(),
            cycle_position_available=True,
            announced_dividend_only=True,
        )
        for index, member in enumerate(snapshot.members)
    ]


def test_blocked_pool_stays_empty_without_changing_ready_pool_results() -> None:
    snapshot = universe_30()
    runner = IndependentPoolRunner()
    results = runner.run(
        definitions={
            StrategyType.QUALITY_GROWTH: QUALITY_GROWTH_V1,
            StrategyType.DEEP_VALUE: DEEP_VALUE_V1,
            StrategyType.STABLE_DIVIDEND: STABLE_DIVIDEND_V1,
        },
        inputs={
            StrategyType.QUALITY_GROWTH: inputs_for_definition(
                QUALITY_GROWTH_V1,
                complete_count=30,
            ),
            StrategyType.DEEP_VALUE: inputs_for_definition(
                DEEP_VALUE_V1,
                complete_count=30,
            ),
            StrategyType.STABLE_DIVIDEND: inputs_for_definition(
                STABLE_DIVIDEND_V1,
                complete_count=23,
            ),
        },
        universe=snapshot,
        report_date=date(2026, 7, 22),
        report_cutoff_at=CUTOFF,
        known_at=CUTOFF,
    )

    assert len(results[StrategyType.QUALITY_GROWTH].candidates) == 30
    assert len(results[StrategyType.DEEP_VALUE].candidates) == 30
    assert results[StrategyType.STABLE_DIVIDEND].candidates == ()
    assert (
        results[StrategyType.STABLE_DIVIDEND].readiness.status
        is PoolReadinessStatus.BLOCKED
    )
