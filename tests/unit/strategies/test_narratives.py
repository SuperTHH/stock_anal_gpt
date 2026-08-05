from datetime import UTC, date, datetime
from decimal import Decimal

from hengce.contracts.enums import (
    CandidateStatus,
    QualityStatus,
    StrategyType,
)
from hengce.contracts.strategy import FactorDetail, StrategyCandidate
from hengce.strategies.narratives import RuleNarrativeRenderer

CUTOFF = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)


def candidate() -> StrategyCandidate:
    return StrategyCandidate(
        report_date=date(2026, 7, 22),
        strategy_type=StrategyType.QUALITY_GROWTH,
        strategy_version="quality-growth-pilot-v1",
        ts_code="699999.SH",
        rank_in_strategy=1,
        strategy_score=Decimal("82.50"),
        factor_details=(
            FactorDetail(
                factor_name="capital_return",
                raw_value=Decimal("80"),
                normalized_score=Decimal("90"),
                weight=Decimal("0.20"),
                weighted_score=Decimal("18"),
                quality_status=QualityStatus.DERIVED,
                source_record_ids=("roe-1", "roic-1"),
                normalization_scope="pilot_universe",
                used_market_fallback=False,
            ),
            FactorDetail(
                factor_name="cash_flow_quality",
                raw_value=Decimal("1.5"),
                normalized_score=Decimal("80"),
                weight=Decimal("0.20"),
                weighted_score=Decimal("16"),
                quality_status=QualityStatus.DERIVED,
                source_record_ids=("ocf-1", "profit-1"),
                normalization_scope="pilot_universe",
                used_market_fallback=False,
            ),
        ),
        selection_reasons=("任意自由文本不得直接透传",),
        risk_flags=(),
        catalysts=("未经验证的扩产猜测",),
        observe_conditions=("任意观察文本",),
        invalidate_conditions=("任意失效文本",),
        data_completeness=Decimal("1"),
        confidence=Decimal("1"),
        data_cutoff_at=CUTOFF,
        known_at=CUTOFF,
        candidate_status=CandidateStatus.CANDIDATE,
    )


def test_rule_narrative_is_versioned_traceable_and_has_no_trade_instruction() -> None:
    narrative = RuleNarrativeRenderer().render(
        StrategyType.QUALITY_GROWTH,
        candidate(),
    )

    assert narrative.selection_reasons[0].text == (
        "质量成长入选依据：资本回报原始值80，现金流质量原始值1.5。"
    )
    assert narrative.catalysts[0].text == "暂无可验证官方催化剂"
    assert narrative.catalysts[0].source_record_ids == ()
    assert narrative.selection_reasons[0].trigger_factor_names == (
        "capital_return",
        "cash_flow_quality",
    )
    assert narrative.selection_reasons[0].source_record_ids == (
        "ocf-1",
        "profit-1",
        "roe-1",
        "roic-1",
    )
    statements = (
        *narrative.selection_reasons,
        *narrative.risks,
        *narrative.observe_conditions,
        *narrative.invalidate_conditions,
        *narrative.catalysts,
    )
    assert all(statement.template_id for statement in statements)
    assert all(statement.template_version == "candidate-narrative-v1" for statement in statements)
    rendered_text = " ".join(statement.text for statement in statements)
    assert "建议买入" not in rendered_text
    assert "保证收益" not in rendered_text
    assert "未经验证的扩产猜测" not in rendered_text


def test_renderer_rejects_strategy_candidate_mismatch() -> None:
    try:
        RuleNarrativeRenderer().render(
            StrategyType.DEEP_VALUE,
            candidate(),
        )
    except ValueError as error:
        assert str(error) == "NARRATIVE_STRATEGY_MISMATCH"
    else:
        raise AssertionError("strategy mismatch must be rejected")
