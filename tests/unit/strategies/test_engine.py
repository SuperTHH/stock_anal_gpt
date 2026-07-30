from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from hengce.contracts.enums import CandidateStatus, QualityStatus
from hengce.strategies.engine import (
    FactorInput,
    SecurityStrategyInput,
    StrategyEngine,
)
from hengce.strategies.quality_growth import QUALITY_GROWTH_V1

CUTOFF = datetime(2026, 7, 29, 13, tzinfo=UTC)


def security_input(
    ts_code: str,
    values: dict[str, str | None],
    *,
    industry: str = "电子",
) -> SecurityStrategyInput:
    return SecurityStrategyInput(
        ts_code=ts_code,
        industry_l1=industry,
        factors={
            name: FactorInput(
                value=Decimal(value) if value is not None else None,
                quality_status=(
                    QualityStatus.DERIVED if value is not None else QualityStatus.MISSING
                ),
                source_record_ids=(f"{ts_code}-{name}",),
                published_at=CUTOFF - timedelta(days=1),
                effective_at=CUTOFF - timedelta(days=365),
                collected_at=CUTOFF - timedelta(hours=1),
                valid_from=CUTOFF - timedelta(hours=1),
            )
            for name, value in values.items()
        },
        hard_filter_passed=True,
        selection_reasons=("结构化因子满足策略规则",),
        risk_flags=(),
        catalysts=("半年维度经营改善",),
        observe_conditions=("下一期指标继续改善",),
        invalidate_conditions=("核心因子跌破阈值",),
        cycle_position_available=True,
        announced_dividend_only=True,
    )


def complete_values(value: str) -> dict[str, str]:
    return {spec.name: value for spec in QUALITY_GROWTH_V1.factors}


def test_pilot_normalization_never_displays_industry_or_fallback_scope() -> None:
    """Catches leaking an industry rank into the fixed thirty-security pilot sample."""
    result = StrategyEngine(QUALITY_GROWTH_V1).rank(
        [
            security_input("699998.SH", complete_values("10")),
            security_input("699999.SH", complete_values("20")),
        ],
        report_date=date(2026, 7, 29),
        data_cutoff_at=CUTOFF,
        known_at=CUTOFF,
    )

    top = result[0]
    assert top.ts_code == "699999.SH"
    assert all(not detail.used_market_fallback for detail in top.factor_details)
    assert all(
        detail.normalization_scope == "pilot_universe"
        for detail in top.factor_details
    )


def test_equal_scores_use_ts_code_ascending_tie_break() -> None:
    result = StrategyEngine(QUALITY_GROWTH_V1).rank(
        [
            security_input("699999.SH", complete_values("10")),
            security_input("699998.SH", complete_values("10")),
        ],
        report_date=date(2026, 7, 29),
        data_cutoff_at=CUTOFF,
        known_at=CUTOFF,
    )

    assert [candidate.ts_code for candidate in result] == [
        "699998.SH",
        "699999.SH",
    ]


def test_winsorization_uses_frozen_one_and_ninety_nine_percent_bounds() -> None:
    values = [Decimal(index) for index in range(100)] + [Decimal("10000")]

    winsorized = StrategyEngine._winsorize(values)

    assert min(winsorized) == StrategyEngine._quantile(values, Decimal("0.01"))
    assert max(winsorized) == StrategyEngine._quantile(values, Decimal("0.99"))
    assert winsorized[-1] < Decimal("10000")


def test_missing_noncritical_factor_does_not_redistribute_its_weight() -> None:
    """Catches silently inflating remaining factor weights when one input is missing."""
    values = complete_values("20")
    values["profitability_stability"] = None
    result = StrategyEngine(QUALITY_GROWTH_V1).rank(
        [
            security_input("699999.SH", values),
            security_input("699998.SH", complete_values("10")),
        ],
        report_date=date(2026, 7, 29),
        data_cutoff_at=CUTOFF,
        known_at=CUTOFF,
    )

    target = next(item for item in result if item.ts_code == "699999.SH")
    missing = next(
        detail
        for detail in target.factor_details
        if detail.factor_name == "profitability_stability"
    )
    assert missing.weight == Decimal("0.10")
    assert missing.weighted_score is None
    assert target.data_completeness == Decimal("0.90")


def test_missing_critical_factor_marks_data_insufficient_and_excludes_candidate() -> None:
    """Catches ranking a security despite a missing strategy-critical factor."""
    values = complete_values("20")
    values["capital_return"] = None
    evaluation = StrategyEngine(QUALITY_GROWTH_V1).evaluate(
        [security_input("699999.SH", values)],
        report_date=date(2026, 7, 29),
        data_cutoff_at=CUTOFF,
        known_at=CUTOFF,
    )

    assert evaluation.candidates == ()
    assert evaluation.evidence.data_insufficient_count == 1
    assert evaluation.evidence.input_count == 1
    assert evaluation.evidence.completed is True


def test_output_is_capped_at_thirty_and_never_contains_buy_instructions() -> None:
    """Catches expanding the research pool or leaking an execution recommendation field."""
    result = StrategyEngine(QUALITY_GROWTH_V1).rank(
        [
            security_input(f"{600000 + index:06d}.SH", complete_values(str(index)))
            for index in range(31)
        ],
        report_date=date(2026, 7, 29),
        data_cutoff_at=CUTOFF,
        known_at=CUTOFF,
    )

    assert len(result) == 30
    assert result[0].candidate_status in {CandidateStatus.CANDIDATE, CandidateStatus.WATCH}
    assert "buy_instruction" not in result[0].model_dump()


def test_future_factor_is_blocked_instead_of_receiving_a_cutoff_label() -> None:
    item = security_input("699999.SH", complete_values("20"))
    future = item.factors["capital_return"]
    item.factors["capital_return"] = FactorInput(
        value=future.value,
        quality_status=future.quality_status,
        source_record_ids=future.source_record_ids,
        published_at=CUTOFF + timedelta(seconds=1),
        effective_at=future.effective_at,
        collected_at=future.collected_at,
        valid_from=future.valid_from,
    )

    evaluation = StrategyEngine(QUALITY_GROWTH_V1).evaluate(
        [item],
        report_date=date(2026, 7, 29),
        data_cutoff_at=CUTOFF,
        known_at=CUTOFF,
    )

    assert evaluation.candidates == ()
    assert evaluation.evidence.data_insufficient_count == 1
