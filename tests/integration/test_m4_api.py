from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from hengce.api.app import create_app
from hengce.contracts.enums import (
    CandidateStatus,
    PoolReadinessStatus,
    QualityStatus,
    StrategyType,
)
from hengce.contracts.official_event import OfficialEvent, ReportSource
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


def publish_fixture(
    tmp_path: Path,
    blocked_strategy: StrategyType | None = None,
) -> tuple[ReportRepository, Path]:
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()
    repository = ReportRepository(state.path)
    report_root = tmp_path / "reports"
    pools: dict[StrategyType, list[StrategyCandidate]] = {}
    versions = {
        StrategyType.QUALITY_GROWTH: "quality-growth-v1",
        StrategyType.DEEP_VALUE: "deep-value-v1",
        StrategyType.STABLE_DIVIDEND: "stable-dividend-v1",
    }
    for index, strategy in enumerate(StrategyType, start=1):
        version = versions[strategy]
        factor = FactorDetail(
            factor_name="composite",
            raw_value=Decimal(index),
            normalized_score=Decimal("80"),
            weight=Decimal("1"),
            weighted_score=Decimal("80"),
            quality_status=QualityStatus.DERIVED,
            source_record_ids=(f"fact-{index}",),
            normalization_scope="market",
            used_market_fallback=True,
        )
        pools[strategy] = [
            StrategyCandidate(
                report_date=date(2026, 7, 29),
                strategy_type=strategy,
                strategy_version=version,
                ts_code=f"69999{index}.SH",
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
        ]
    if blocked_strategy is not None:
        pools[blocked_strategy] = []
    ReportPublisher(repository, report_root).publish(
        report_id="report-2026-07-29-v1",
        report_date=date(2026, 7, 29),
        market_cutoff_at=NOW,
        event_cutoff_at=NOW,
        generated_at=NOW,
        candidate_pools=pools,
        data_domain_statuses={
            "market": QualityStatus.VALID,
            "security_master": QualityStatus.VALID,
            "pilot_universe": QualityStatus.VALID,
            "manifest": QualityStatus.VALID,
            "financials": QualityStatus.VALID,
        },
        pool_readiness={
            strategy: PoolReadiness(
                strategy_type=strategy,
                universe_size=30,
                eligible_count=(
                    23 if strategy is blocked_strategy else 30
                ),
                complete_factor_count=(
                    23 if strategy is blocked_strategy else 30
                ),
                coverage_ratio=(
                    Decimal(23) / Decimal(30)
                    if strategy is blocked_strategy
                    else Decimal("1")
                ),
                required_coverage_ratio=Decimal("0.80"),
                status=(
                    PoolReadinessStatus.BLOCKED
                    if strategy is blocked_strategy
                    else PoolReadinessStatus.READY
                ),
                missing_by_security=(
                    {"699999.SH": ("critical:VALUE_MISSING",)}
                    if strategy is blocked_strategy
                    else {}
                ),
                blocking_codes=(
                    ("POOL_FACTOR_COVERAGE_BELOW_80_PERCENT",)
                    if strategy is blocked_strategy
                    else ()
                ),
                strategy_version=versions[strategy],
                factor_version="pilot-financial-metrics-v1",
            )
            for strategy in StrategyType
        },
        universe_id="pilot-2026-07-22",
        report_cutoff_at=NOW,
        known_at=NOW,
        generation_started_at=NOW,
        quality_summary={
            "manifest_status_distribution": {"INGESTED": 360},
            "xbrl_used_count": 140,
            "pdf_used_count": 10,
        },
        official_events=(
            OfficialEvent(
                record_id="event-1",
                source_id="sse",
                source_url="https://www.sse.com.cn/",
                published_at=NOW,
                effective_at=NOW,
                collected_at=NOW,
                version="v1",
                content_hash="a" * 64,
                license_policy="personal-research",
                quality_status=QualityStatus.VALID,
                valid_from=NOW,
                institution="上海证券交易所",
                event_type="COMPANY_ANNOUNCEMENT",
                title="经营进展说明",
                factual_summary="公司披露经营进展。",
                affected_ts_codes=("699991.SH",),
                impact_horizon="MEDIUM_TERM",
                confidence=Decimal("0.9"),
            ),
        ),
        source_records=tuple(
            ReportSource(
                record_id=f"fact-{index}",
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
            for index in range(1, 4)
        ),
        strategy_evidence={
            strategy: StrategyRunEvidence(
                strategy_type=strategy,
                strategy_version=versions[strategy],
                input_count=1,
                excluded_count=(1 if strategy is blocked_strategy else 0),
                data_insufficient_count=0,
                qualified_count=(0 if strategy is blocked_strategy else 1),
                published_candidate_count=(
                    0 if strategy is blocked_strategy else 1
                ),
                completed=True,
            )
            for strategy in StrategyType
        },
    )
    return repository, report_root


def test_latest_and_strategy_endpoints_share_one_published_report(tmp_path: Path) -> None:
    """Catches UI tabs independently resolving latest and mixing report versions."""
    repository, report_root = publish_fixture(tmp_path)
    client = TestClient(create_app(repository, report_root))

    latest = client.get("/api/reports/latest")
    quality = client.get(
        "/api/strategies/QUALITY_GROWTH",
        params={"report_id": "report-2026-07-29-v1"},
    )

    assert latest.status_code == 200
    assert quality.status_code == 200
    assert latest.json()["snapshot"]["report_id"] == "report-2026-07-29-v1"
    assert quality.json()["report_id"] == "report-2026-07-29-v1"
    assert quality.json()["candidates"][0]["ts_code"] == "699991.SH"


def test_security_research_is_derived_from_requested_report_not_raw_store(
    tmp_path: Path,
) -> None:
    """Catches bypassing publication quality gates for an individual-security page."""
    repository, report_root = publish_fixture(tmp_path)
    client = TestClient(create_app(repository, report_root))

    response = client.get(
        "/api/securities/699992.SH",
        params={"report_id": "report-2026-07-29-v1"},
    )

    assert response.status_code == 200
    assert response.json()["report_id"] == "report-2026-07-29-v1"
    assert response.json()["strategy_memberships"] == ["DEEP_VALUE"]


def test_events_and_quality_endpoints_preserve_published_source_lineage(
    tmp_path: Path,
) -> None:
    repository, report_root = publish_fixture(tmp_path)
    client = TestClient(create_app(repository, report_root))

    events = client.get(
        "/api/events",
        params={"report_id": "report-2026-07-29-v1"},
    )
    quality = client.get(
        "/api/quality",
        params={"report_id": "report-2026-07-29-v1"},
    )

    assert events.status_code == 200
    assert events.json()["events"][0]["title"] == "经营进展说明"
    assert events.json()["events"][0]["source_url"] == "https://www.sse.com.cn/"
    assert quality.status_code == 200
    assert quality.json()["source_records"][0]["license_policy"] == "personal-research"
    assert quality.json()["manifest_status_distribution"] == {"INGESTED": 360}
    assert quality.json()["xbrl_used_count"] == 140
    assert quality.json()["pdf_used_count"] == 10
    assert quality.json()["known_at"] == NOW.isoformat().replace("+00:00", "Z")


def test_historical_fields_and_ready_pool_are_returned_from_artifact(
    tmp_path: Path,
) -> None:
    repository, report_root = publish_fixture(tmp_path)
    client = TestClient(create_app(repository, report_root))

    latest = client.get("/api/reports/latest")
    strategy = client.get(
        "/api/strategies/QUALITY_GROWTH",
        params={"report_id": "report-2026-07-29-v1"},
    )

    assert latest.status_code == 200
    assert latest.json()["snapshot"]["is_historical_reconstruction"] is True
    assert latest.json()["snapshot"]["universe_id"] == "pilot-2026-07-22"
    assert latest.json()["snapshot"]["known_at"] == NOW.isoformat().replace(
        "+00:00",
        "Z",
    )
    assert strategy.status_code == 200
    assert strategy.json()["readiness"]["status"] == "READY"
    assert strategy.json()["readiness"]["complete_factor_count"] == 30


def test_blocked_strategy_returns_200_with_empty_candidates_and_readiness(
    tmp_path: Path,
) -> None:
    repository, report_root = publish_fixture(
        tmp_path,
        blocked_strategy=StrategyType.STABLE_DIVIDEND,
    )

    response = TestClient(create_app(repository, report_root)).get(
        "/api/strategies/STABLE_DIVIDEND",
        params={"report_id": "report-2026-07-29-v1"},
    )

    assert response.status_code == 200
    assert response.json()["candidates"] == []
    assert response.json()["readiness"]["status"] == "BLOCKED"
    assert response.json()["readiness"]["complete_factor_count"] == 23
    assert response.json()["readiness"]["blocking_codes"] == [
        "POOL_FACTOR_COVERAGE_BELOW_80_PERCENT"
    ]


def test_no_published_report_returns_404_and_never_demo_candidates(tmp_path: Path) -> None:
    """Catches silently falling back to prototype data in production mode."""
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()
    repository = ReportRepository(state.path)
    client = TestClient(create_app(repository, tmp_path / "reports"))

    response = client.get("/api/reports/latest")

    assert response.status_code == 404
    assert response.json()["detail"] == "NO_PUBLISHED_REPORT"


def test_api_rejects_report_artifact_path_outside_configured_root(tmp_path: Path) -> None:
    """Catches trusting a SQLite artifact path that escapes the report warehouse."""
    repository, report_root = publish_fixture(tmp_path)
    with repository.path.parent.joinpath("outside.json").open("w", encoding="utf-8") as handle:
        handle.write("{}")
    import sqlite3

    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            "UPDATE report_snapshots SET artifact_path=?",
            (str(repository.path.parent / "outside.json"),),
        )
    client = TestClient(create_app(repository, report_root))

    response = client.get("/api/reports/latest")

    assert response.status_code == 500
    assert response.json()["detail"] == "REPORT_ARTIFACT_PATH_INVALID"


def test_api_rejects_tampered_report_artifact(tmp_path: Path) -> None:
    repository, report_root = publish_fixture(tmp_path)
    stored = repository.latest_report()
    assert stored is not None
    import json

    payload = json.loads(stored.artifact_path.read_text(encoding="utf-8"))
    payload["candidate_pools"]["QUALITY_GROWTH"][0]["strategy_score"] = "1.00"
    stored.artifact_path.write_text(json.dumps(payload), encoding="utf-8")

    response = TestClient(create_app(repository, report_root)).get("/api/reports/latest")

    assert response.status_code == 500
    assert response.json()["detail"] == "REPORT_ARTIFACT_INVALID"
