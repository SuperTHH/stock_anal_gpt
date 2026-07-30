from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from hengce.contracts.enums import (
    CandidateStatus,
    PoolReadinessStatus,
    QualityStatus,
    StrategyType,
)
from hengce.contracts.official_event import ReportSource
from hengce.contracts.pilot import PoolReadiness
from hengce.contracts.strategy import (
    FactorDetail,
    StrategyCandidate,
    StrategyRunEvidence,
)
from hengce.reports.publisher import ReportPublisher
from hengce.state.report_repository import ReportRepository
from hengce.state.repository import StateRepository

NOW = datetime(2026, 7, 29, 13, tzinfo=UTC)


def candidate(strategy: StrategyType, version: str, ts_code: str) -> StrategyCandidate:
    factor = FactorDetail(
        factor_name="composite",
        raw_value=Decimal("1"),
        normalized_score=Decimal("80"),
        weight=Decimal("1"),
        weighted_score=Decimal("80"),
        quality_status=QualityStatus.DERIVED,
        source_record_ids=(f"{ts_code}-fact",),
        normalization_scope="market",
        used_market_fallback=True,
    )
    return StrategyCandidate(
        report_date=date(2026, 7, 29),
        strategy_type=strategy,
        strategy_version=version,
        ts_code=ts_code,
        rank_in_strategy=1,
        strategy_score=Decimal("80"),
        factor_details=(factor,),
        selection_reasons=("规则支持",),
        risk_flags=(),
        catalysts=("经营改善",),
        observe_conditions=("指标保持",),
        invalidate_conditions=("指标恶化",),
        data_completeness=Decimal("1"),
        confidence=Decimal("1"),
        data_cutoff_at=NOW,
        known_at=NOW,
        candidate_status=CandidateStatus.CANDIDATE,
    )


def pools() -> dict[StrategyType, list[StrategyCandidate]]:
    return {
        StrategyType.QUALITY_GROWTH: [
            candidate(StrategyType.QUALITY_GROWTH, "quality-growth-v1", "699991.SH")
        ],
        StrategyType.DEEP_VALUE: [
            candidate(StrategyType.DEEP_VALUE, "deep-value-v1", "699992.SH")
        ],
        StrategyType.STABLE_DIVIDEND: [
            candidate(StrategyType.STABLE_DIVIDEND, "stable-dividend-v1", "699993.SH")
        ],
    }


def publisher(tmp_path: Path) -> tuple[ReportPublisher, ReportRepository]:
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()
    repository = ReportRepository(state.path)
    return ReportPublisher(repository, tmp_path / "reports"), repository


def lineage(
    candidate_pools: dict[StrategyType, list[StrategyCandidate]],
) -> dict[str, object]:
    source_ids = {
        source_id
        for candidates in candidate_pools.values()
        for item in candidates
        for factor in item.factor_details
        for source_id in factor.source_record_ids
    }
    sources = tuple(
        ReportSource(
            record_id=source_id,
            domain="financials",
            source_name="交易所 XBRL",
            source_url="https://www.sse.com.cn/",
            published_at=NOW,
            effective_at=NOW,
            collected_at=NOW,
            valid_from=NOW,
            version="v1",
            license_policy="personal-research",
            quality_status=QualityStatus.VALID,
        )
        for source_id in sorted(source_ids)
    )
    evidence = {
        strategy: StrategyRunEvidence(
            strategy_type=strategy,
            strategy_version={
                StrategyType.QUALITY_GROWTH: "quality-growth-v1",
                StrategyType.DEEP_VALUE: "deep-value-v1",
                StrategyType.STABLE_DIVIDEND: "stable-dividend-v1",
            }[strategy],
            input_count=max(len(candidates), 1),
            excluded_count=1 if not candidates else 0,
            data_insufficient_count=0,
            qualified_count=len(candidates),
            published_candidate_count=len(candidates),
            completed=True,
        )
        for strategy, candidates in candidate_pools.items()
    }
    return {"source_records": sources, "strategy_evidence": evidence}


def readiness(
    candidate_pools: dict[StrategyType, list[StrategyCandidate]],
) -> dict[StrategyType, PoolReadiness]:
    versions = {
        StrategyType.QUALITY_GROWTH: "quality-growth-v1",
        StrategyType.DEEP_VALUE: "deep-value-v1",
        StrategyType.STABLE_DIVIDEND: "stable-dividend-v1",
    }
    return {
        strategy: PoolReadiness(
            strategy_type=strategy,
            universe_size=30,
            eligible_count=(30 if candidates else 23),
            complete_factor_count=(30 if candidates else 23),
            coverage_ratio=(
                Decimal("1")
                if candidates
                else Decimal(23) / Decimal(30)
            ),
            required_coverage_ratio=Decimal("0.80"),
            status=(
                PoolReadinessStatus.READY
                if candidates
                else PoolReadinessStatus.BLOCKED
            ),
            missing_by_security=(
                {} if candidates else {"699999.SH": ("critical:VALUE_MISSING",)}
            ),
            blocking_codes=(
                ()
                if candidates
                else ("POOL_FACTOR_COVERAGE_BELOW_80_PERCENT",)
            ),
            strategy_version=versions[strategy],
            factor_version="pilot-financial-metrics-v1",
        )
        for strategy, candidates in candidate_pools.items()
    }


def test_complete_report_is_content_addressed_and_atomically_becomes_latest(
    tmp_path: Path,
) -> None:
    """Catches moving latest before a complete immutable report package exists."""
    service, repository = publisher(tmp_path)
    candidate_pools = pools()

    result = service.publish(
        report_id="report-2026-07-29-v1",
        report_date=date(2026, 7, 29),
        market_cutoff_at=NOW,
        event_cutoff_at=NOW,
        generated_at=NOW,
        candidate_pools=candidate_pools,
        **lineage(candidate_pools),
        data_domain_statuses={
            "market": QualityStatus.VALID,
            "financials": QualityStatus.VALID,
            "actions": QualityStatus.DERIVED,
        },
    )

    assert result.published is True
    assert result.snapshot is not None
    assert repository.latest_report_id() == "report-2026-07-29-v1"
    assert result.artifact_path is not None
    assert result.artifact_path.name.endswith(f"{result.snapshot.manifest_hash}.json")


def test_failed_report_keeps_previous_latest_and_derives_stale_display_state(
    tmp_path: Path,
) -> None:
    """Catches replacing a usable report with an incomplete 21:30 run."""
    service, repository = publisher(tmp_path)
    candidate_pools = pools()
    first = service.publish(
        report_id="report-2026-07-29-v1",
        report_date=date(2026, 7, 29),
        market_cutoff_at=NOW,
        event_cutoff_at=NOW,
        generated_at=NOW,
        candidate_pools=candidate_pools,
        **lineage(candidate_pools),
        data_domain_statuses={
            "market": QualityStatus.VALID,
            "financials": QualityStatus.VALID,
        },
    )
    failed = service.publish(
        report_id="report-2026-07-30-v1",
        report_date=date(2026, 7, 30),
        market_cutoff_at=NOW + timedelta(days=1),
        event_cutoff_at=NOW + timedelta(days=1),
        generated_at=NOW + timedelta(days=1),
        candidate_pools=candidate_pools,
        **lineage(candidate_pools),
        data_domain_statuses={
            "market": QualityStatus.VALID,
            "financials": QualityStatus.MISSING,
        },
    )

    assert first.published is True
    assert failed.published is False
    assert failed.blocked_reasons == ("REPORT_DOMAIN_NOT_READY:financials",)
    assert repository.latest_report_id() == "report-2026-07-29-v1"
    assert repository.display_status(latest_run_succeeded=False) == "STALE_PREVIOUS_REPORT"


def test_same_report_id_cannot_be_republished_with_different_content(tmp_path: Path) -> None:
    """Catches mutating a report after publication instead of creating a new report ID."""
    service, _repository = publisher(tmp_path)
    candidate_pools = pools()
    arguments = {
        "report_id": "report-2026-07-29-v1",
        "report_date": date(2026, 7, 29),
        "market_cutoff_at": NOW,
        "event_cutoff_at": NOW,
        "generated_at": NOW,
        "candidate_pools": candidate_pools,
        "data_domain_statuses": {"market": QualityStatus.VALID},
        **lineage(candidate_pools),
    }
    service.publish(**arguments)
    changed = pools()
    changed[StrategyType.QUALITY_GROWTH][0] = changed[
        StrategyType.QUALITY_GROWTH
    ][0].model_copy(update={"strategy_score": Decimal("70")})

    try:
        service.publish(**{**arguments, "candidate_pools": changed})
    except ValueError as error:
        assert str(error) == "REPORT_IMMUTABILITY_CONFLICT"
    else:
        raise AssertionError("mutated report was accepted")


def test_report_can_publish_a_legitimately_empty_strategy_pool(tmp_path: Path) -> None:
    """Catches treating a valid zero-candidate result as a publication failure."""
    service, repository = publisher(tmp_path)
    empty_pools = pools()
    empty_pools[StrategyType.STABLE_DIVIDEND] = []

    result = service.publish(
        report_id="report-2026-07-29-empty-dividend",
        report_date=date(2026, 7, 29),
        market_cutoff_at=NOW,
        event_cutoff_at=NOW,
        generated_at=NOW,
        candidate_pools=empty_pools,
        **lineage(empty_pools),
        strategy_versions={
            StrategyType.QUALITY_GROWTH: "quality-growth-v1",
            StrategyType.DEEP_VALUE: "deep-value-v1",
            StrategyType.STABLE_DIVIDEND: "stable-dividend-v1",
        },
        data_domain_statuses={
            "market": QualityStatus.VALID,
            "financials": QualityStatus.VALID,
        },
    )

    assert result.published is True
    assert result.snapshot is not None
    assert (
        result.snapshot.strategy_versions[StrategyType.STABLE_DIVIDEND]
        == "stable-dividend-v1"
    )
    assert repository.latest_report_id() == "report-2026-07-29-empty-dividend"


def test_empty_pool_is_blocked_when_evaluation_was_all_data_insufficient(
    tmp_path: Path,
) -> None:
    service, repository = publisher(tmp_path)
    empty_pools = pools()
    empty_pools[StrategyType.STABLE_DIVIDEND] = []
    metadata = lineage(empty_pools)
    evidence = dict(metadata["strategy_evidence"])
    evidence[StrategyType.STABLE_DIVIDEND] = StrategyRunEvidence(
        strategy_type=StrategyType.STABLE_DIVIDEND,
        strategy_version="stable-dividend-v1",
        input_count=10,
        excluded_count=0,
        data_insufficient_count=10,
        qualified_count=0,
        published_candidate_count=0,
        completed=True,
    )

    result = service.publish(
        report_id="report-all-insufficient",
        report_date=date(2026, 7, 29),
        market_cutoff_at=NOW,
        event_cutoff_at=NOW,
        generated_at=NOW,
        candidate_pools=empty_pools,
        strategy_versions={
            StrategyType.QUALITY_GROWTH: "quality-growth-v1",
            StrategyType.DEEP_VALUE: "deep-value-v1",
            StrategyType.STABLE_DIVIDEND: "stable-dividend-v1",
        },
        strategy_evidence=evidence,
        source_records=metadata["source_records"],
        data_domain_statuses={"market": QualityStatus.VALID},
    )

    assert result.published is False
    assert result.blocked_reasons == (
        "REPORT_STRATEGY_ALL_DATA_INSUFFICIENT:STABLE_DIVIDEND",
    )
    assert repository.latest_report_id() is None


def test_unresolved_source_or_future_candidate_cannot_be_published(tmp_path: Path) -> None:
    service, _repository = publisher(tmp_path)
    candidate_pools = pools()
    metadata = lineage(candidate_pools)

    unresolved = service.publish(
        report_id="report-unresolved",
        report_date=date(2026, 7, 29),
        market_cutoff_at=NOW,
        event_cutoff_at=NOW,
        generated_at=NOW,
        candidate_pools=candidate_pools,
        strategy_evidence=metadata["strategy_evidence"],
        source_records=(),
        data_domain_statuses={"market": QualityStatus.VALID},
    )
    assert unresolved.published is False
    assert unresolved.blocked_reasons[0].startswith(
        "REPORT_SOURCE_LINEAGE_UNRESOLVED:"
    )

    future = dict(candidate_pools)
    future[StrategyType.QUALITY_GROWTH] = [
        candidate_pools[StrategyType.QUALITY_GROWTH][0].model_copy(
            update={"data_cutoff_at": NOW + timedelta(seconds=1)}
        )
    ]
    try:
        service.publish(
            report_id="report-future-candidate",
            report_date=date(2026, 7, 29),
            market_cutoff_at=NOW,
            event_cutoff_at=NOW,
            generated_at=NOW,
            candidate_pools=future,
            strategy_evidence=metadata["strategy_evidence"],
            source_records=metadata["source_records"],
            data_domain_statuses={"market": QualityStatus.VALID},
        )
    except ValueError as error:
        assert str(error) == "REPORT_CANDIDATE_CUTOFF_VIOLATION"
    else:
        raise AssertionError("future candidate was published")


def test_repository_rejects_non_published_snapshot(tmp_path: Path) -> None:
    service, repository = publisher(tmp_path)
    candidate_pools = pools()
    result = service.publish(
        report_id="report-published",
        report_date=date(2026, 7, 29),
        market_cutoff_at=NOW,
        event_cutoff_at=NOW,
        generated_at=NOW,
        candidate_pools=candidate_pools,
        data_domain_statuses={"market": QualityStatus.VALID},
        **lineage(candidate_pools),
    )
    assert result.snapshot is not None
    blocked = result.snapshot.model_copy(
        update={"report_id": "report-blocked", "report_status": "BLOCKED_MISSING_DATA"}
    )

    try:
        repository.publish(blocked, tmp_path / "blocked.json")
    except ValueError as error:
        assert str(error) == "REPORT_STATUS_NOT_PUBLISHABLE"
    else:
        raise AssertionError("blocked report was published")


def test_factor_source_must_be_visible_at_candidate_cutoff(tmp_path: Path) -> None:
    service, _repository = publisher(tmp_path)
    candidate_pools = pools()
    early_candidate = candidate_pools[StrategyType.QUALITY_GROWTH][0].model_copy(
        update={"data_cutoff_at": NOW - timedelta(hours=1)}
    )
    candidate_pools[StrategyType.QUALITY_GROWTH] = [early_candidate]
    metadata = lineage(candidate_pools)

    try:
        service.publish(
            report_id="report-source-after-candidate",
            report_date=date(2026, 7, 29),
            market_cutoff_at=NOW,
            event_cutoff_at=NOW,
            generated_at=NOW,
            candidate_pools=candidate_pools,
            data_domain_statuses={"market": QualityStatus.VALID},
            **metadata,
        )
    except ValueError as error:
        assert str(error) == "REPORT_SOURCE_AFTER_CANDIDATE_CUTOFF"
    else:
        raise AssertionError("late source was attached to an earlier candidate cutoff")


def test_historical_report_publishes_ready_pools_and_keeps_blocked_pool_empty(
    tmp_path: Path,
) -> None:
    service, repository = publisher(tmp_path)
    candidate_pools = pools()
    candidate_pools[StrategyType.STABLE_DIVIDEND] = []
    metadata = lineage(candidate_pools)

    result = service.publish(
        report_id="historical-partial",
        report_date=date(2026, 7, 29),
        market_cutoff_at=NOW,
        event_cutoff_at=NOW,
        generated_at=NOW,
        candidate_pools=candidate_pools,
        strategy_evidence=metadata["strategy_evidence"],
        source_records=metadata["source_records"],
        data_domain_statuses={
            "market": QualityStatus.VALID,
            "security_master": QualityStatus.VALID,
            "pilot_universe": QualityStatus.VALID,
            "manifest": QualityStatus.VALID,
            "financials": QualityStatus.PARTIAL,
        },
        pool_readiness=readiness(candidate_pools),
        universe_id="pilot-2026-07-22",
        report_cutoff_at=NOW,
        known_at=NOW,
        generation_started_at=NOW,
    )

    assert result.published is True
    assert result.snapshot is not None
    assert result.snapshot.report_status.value == "PUBLISHED_PARTIAL"
    assert result.snapshot.is_historical_reconstruction is True
    assert result.snapshot.pool_readiness[
        StrategyType.STABLE_DIVIDEND
    ].status is PoolReadinessStatus.BLOCKED
    assert repository.latest_report_id() == "historical-partial"


def test_all_blocked_pools_publish_quality_report_without_candidates(
    tmp_path: Path,
) -> None:
    service, repository = publisher(tmp_path)
    candidate_pools = {strategy: [] for strategy in StrategyType}

    result = service.publish(
        report_id="historical-quality-only",
        report_date=date(2026, 7, 29),
        market_cutoff_at=NOW,
        event_cutoff_at=NOW,
        generated_at=NOW,
        candidate_pools=candidate_pools,
        strategy_evidence={},
        source_records=(),
        strategy_versions={
            StrategyType.QUALITY_GROWTH: "quality-growth-v1",
            StrategyType.DEEP_VALUE: "deep-value-v1",
            StrategyType.STABLE_DIVIDEND: "stable-dividend-v1",
        },
        data_domain_statuses={
            "market": QualityStatus.VALID,
            "security_master": QualityStatus.VALID,
            "pilot_universe": QualityStatus.VALID,
            "manifest": QualityStatus.VALID,
            "financials": QualityStatus.MISSING,
        },
        pool_readiness=readiness(candidate_pools),
        universe_id="pilot-2026-07-22",
        report_cutoff_at=NOW,
        known_at=NOW,
        generation_started_at=NOW,
        manual_todo_count=7,
    )

    assert result.published is True
    assert result.snapshot is not None
    assert result.snapshot.report_status.value == "PUBLISHED_PARTIAL"
    assert result.snapshot.manual_todo_count == 7
    assert repository.latest_report_id() == "historical-quality-only"
