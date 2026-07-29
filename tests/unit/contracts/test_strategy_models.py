from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from hengce.contracts.enums import (
    CandidateStatus,
    QualityStatus,
    ReportStatus,
    StrategyType,
)
from hengce.contracts.strategy import (
    FactorDetail,
    ReportSnapshot,
    StrategyCandidate,
    StrategyRunEvidence,
)

NOW = datetime(2026, 7, 29, 13, tzinfo=UTC)


def factor() -> FactorDetail:
    return FactorDetail(
        factor_name="roe",
        raw_value=Decimal("0.15"),
        normalized_score=Decimal("80"),
        weight=Decimal("0.20"),
        weighted_score=Decimal("16"),
        quality_status=QualityStatus.DERIVED,
        source_record_ids=("net-profit-1", "equity-1"),
        normalization_scope="industry:electronics",
        used_market_fallback=False,
    )


def candidate_payload() -> dict[str, object]:
    return {
        "report_date": date(2026, 7, 29),
        "strategy_type": StrategyType.QUALITY_GROWTH,
        "strategy_version": "quality-growth-v1",
        "ts_code": "699999.SH",
        "rank_in_strategy": 1,
        "strategy_score": Decimal("80.00"),
        "factor_details": (factor(),),
        "selection_reasons": ("ROE 与现金流质量位于行业前列",),
        "risk_flags": (),
        "catalysts": ("新增产能通过客户验证",),
        "observe_conditions": ("连续两季经营现金流覆盖净利润",),
        "invalidate_conditions": ("ROIC 连续两期低于 8%",),
        "data_completeness": Decimal("0.95"),
        "confidence": Decimal("0.90"),
        "data_cutoff_at": NOW,
        "known_at": NOW,
        "candidate_status": CandidateStatus.CANDIDATE,
    }


def test_candidate_has_only_strategy_local_score_and_reproducible_factors() -> None:
    """Catches losing factor lineage or adding a misleading cross-strategy total score."""
    candidate = StrategyCandidate.model_validate(candidate_payload())
    assert candidate.strategy_score == Decimal("80.00")
    assert candidate.factor_details[0].source_record_ids == ("net-profit-1", "equity-1")
    with pytest.raises(ValidationError):
        StrategyCandidate.model_validate({**candidate_payload(), "overall_score": Decimal("90")})


def test_candidate_enforces_score_rank_completeness_and_aware_cutoff() -> None:
    """Catches impossible ranks or silently accepting an ambiguous historical cutoff."""
    with pytest.raises(ValidationError):
        StrategyCandidate.model_validate({**candidate_payload(), "rank_in_strategy": 0})
    with pytest.raises(ValidationError):
        StrategyCandidate.model_validate(
            {**candidate_payload(), "data_completeness": Decimal("1.01")}
        )
    with pytest.raises(ValidationError):
        StrategyCandidate.model_validate(
            {**candidate_payload(), "data_cutoff_at": datetime(2026, 7, 29, 13)}
        )


def snapshot_payload() -> dict[str, object]:
    return {
        "report_id": "report-2026-07-29-v1",
        "report_date": date(2026, 7, 29),
        "market_cutoff_at": NOW,
        "event_cutoff_at": NOW,
        "generated_at": NOW,
        "published_at": NOW,
        "report_status": ReportStatus.PUBLISHED,
        "previous_report_id": "report-2026-07-28-v1",
        "data_domain_statuses": {
            "market": QualityStatus.VALID,
            "financials": QualityStatus.VALID,
        },
        "strategy_versions": {
            StrategyType.QUALITY_GROWTH: "quality-growth-v1",
            StrategyType.DEEP_VALUE: "deep-value-v1",
            StrategyType.STABLE_DIVIDEND: "stable-dividend-v1",
        },
        "manifest_hash": "a" * 64,
    }


def test_published_snapshot_requires_all_three_independent_strategy_versions() -> None:
    """Catches publishing a report whose page tabs do not refer to a complete strategy set."""
    assert ReportSnapshot.model_validate(snapshot_payload()).report_status is ReportStatus.PUBLISHED
    with pytest.raises(ValidationError):
        ReportSnapshot.model_validate(
            {
                **snapshot_payload(),
                "strategy_versions": {
                    StrategyType.QUALITY_GROWTH: "quality-growth-v1",
                },
            }
        )


def test_completed_strategy_evidence_cannot_hide_qualified_candidates() -> None:
    """Catches claiming a complete empty pool after evaluating qualified securities."""
    with pytest.raises(ValidationError):
        StrategyRunEvidence(
            strategy_type=StrategyType.QUALITY_GROWTH,
            strategy_version="quality-growth-v1",
            input_count=10,
            excluded_count=0,
            data_insufficient_count=0,
            qualified_count=10,
            published_candidate_count=0,
            completed=True,
        )
