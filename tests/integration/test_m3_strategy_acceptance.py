from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from hengce.contracts.enums import QualityStatus, StrategyType
from hengce.strategies.deep_value import DEEP_VALUE_V1
from hengce.strategies.engine import FactorInput, SecurityStrategyInput, StrategyEngine
from hengce.strategies.quality_growth import QUALITY_GROWTH_V1
from hengce.strategies.stable_dividend import STABLE_DIVIDEND_V1

CUTOFF = datetime(2026, 7, 29, 13, tzinfo=UTC)


def candidate_input(ts_code: str, preferred: StrategyType) -> SecurityStrategyInput:
    all_names = {
        spec.name
        for definition in (QUALITY_GROWTH_V1, DEEP_VALUE_V1, STABLE_DIVIDEND_V1)
        for spec in definition.factors
    }
    preferred_names = {
        spec.name
        for definition in (QUALITY_GROWTH_V1, DEEP_VALUE_V1, STABLE_DIVIDEND_V1)
        if definition.strategy_type is preferred
        for spec in definition.factors
    }
    return SecurityStrategyInput(
        ts_code=ts_code,
        industry_l1="电子",
        factors={
            name: FactorInput(
                value=Decimal("90" if name in preferred_names else "10"),
                quality_status=QualityStatus.DERIVED,
                source_record_ids=(f"{ts_code}-{name}",),
                published_at=CUTOFF - timedelta(days=1),
                effective_at=CUTOFF - timedelta(days=365),
                collected_at=CUTOFF - timedelta(hours=1),
                valid_from=CUTOFF - timedelta(hours=1),
            )
            for name in all_names
        },
        hard_filter_passed=True,
        selection_reasons=("策略内因子支持",),
        risk_flags=(),
        catalysts=("经营改善",),
        observe_conditions=("下一期保持",),
        invalidate_conditions=("核心指标恶化",),
        cycle_position_available=True,
        announced_dividend_only=True,
    )


def test_three_pools_have_independent_scores_versions_and_rankings() -> None:
    """Catches combining strategies into one score or reusing one pool's ranking."""
    inputs = [
        candidate_input("699991.SH", StrategyType.QUALITY_GROWTH),
        candidate_input("699992.SH", StrategyType.DEEP_VALUE),
        candidate_input("699993.SH", StrategyType.STABLE_DIVIDEND),
    ]

    quality = StrategyEngine(QUALITY_GROWTH_V1).rank(
        inputs,
        report_date=date(2026, 7, 29),
        data_cutoff_at=CUTOFF,
        known_at=CUTOFF,
    )
    value = StrategyEngine(DEEP_VALUE_V1).rank(
        inputs,
        report_date=date(2026, 7, 29),
        data_cutoff_at=CUTOFF,
        known_at=CUTOFF,
    )
    dividend = StrategyEngine(STABLE_DIVIDEND_V1).rank(
        inputs,
        report_date=date(2026, 7, 29),
        data_cutoff_at=CUTOFF,
        known_at=CUTOFF,
    )

    assert quality[0].ts_code == "699991.SH"
    assert value[0].ts_code == "699992.SH"
    assert dividend[0].ts_code == "699993.SH"
    assert {
        quality[0].strategy_version,
        value[0].strategy_version,
        dividend[0].strategy_version,
    } == {"quality-growth-v1", "deep-value-v1", "stable-dividend-v1"}
