from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from hengce.api.app import create_app
from hengce.contracts.enums import EvidenceCohort, EvidenceTaskStatus
from hengce.contracts.market import MarketBar, SecurityMaster
from hengce.contracts.market_screen import ImplementedDividend
from hengce.contracts.official_event import OfficialEvent, OfficialEventSourceScan
from hengce.contracts.xbrl_discovery import ExchangeXbrlDiscoveryScan
from hengce.services.full_market_evidence import FullMarketEvidencePlanner
from hengce.services.full_market_research import FullMarketResearchService
from hengce.state.event_repository import OfficialEventRepository
from hengce.state.evidence_repository import FullMarketEvidenceRepository
from hengce.state.report_repository import ReportRepository
from hengce.state.repository import StateRepository
from hengce.state.xbrl_discovery_repository import ExchangeXbrlDiscoveryRepository
from hengce.warehouse.dividends import ImplementedDividendWarehouse
from hengce.warehouse.market import MarketWarehouse
from tests.unit.state.test_repository import exchange_policy

NOW = datetime(2026, 8, 12, tzinfo=UTC)


def seed(tmp_path: Path) -> tuple[ReportRepository, Path, Path]:
    database = tmp_path / "state" / "hengce.sqlite3"
    state = StateRepository(database)
    state.migrate()
    state.upsert_policies([exchange_policy("sse"), exchange_policy("szse")])

    def security(code: str, name: str, board: str, exchange: str) -> SecurityMaster:
        return SecurityMaster(
            ts_code=code, symbol=code[:6], name=name, exchange=exchange,
            board=board, list_date=date(2000, 1, 1), is_in_scope=True,
        )

    state.save_security_master_snapshot(
        [security("600000.SH", "浦发银行", "MAIN_SH", "SSE")],
        source_id="sse", source_url="https://www.sse.com.cn/master.csv",
        collected_at=NOW, content_hash="a" * 64, version="sse-v1", quality_lineage={},
    )
    state.save_security_master_snapshot(
        [security("000001.SZ", "平安银行", "MAIN_SZ", "SZSE")],
        source_id="szse", source_url="https://www.szse.cn/master.csv",
        collected_at=NOW, content_hash="b" * 64, version="szse-v1", quality_lineage={},
    )

    def bar(code: str, close: str) -> MarketBar:
        return MarketBar(
            record_id=f"bar-{code}", source_id="tushare",
            source_url="http://api.tushare.pro/", collected_at=NOW,
            version="daily-v1", content_hash="c" * 64,
            license_policy="tushare-daily", quality_status="VALID", valid_from=NOW,
            ts_code=code, trade_date=date(2026, 7, 22), open=Decimal(close),
            high=Decimal(close), low=Decimal(close), close=Decimal(close),
            pre_close=Decimal(close), volume=Decimal("10"), amount=Decimal("100"),
        )

    normalized = tmp_path / "normalized"
    MarketWarehouse(normalized).write_bars([bar("600000.SH", "10"), bar("000001.SZ", "5")])
    ImplementedDividendWarehouse(normalized).write_records(
        date(2026, 7, 22),
        [ImplementedDividend(
            record_id="dividend-1", source_id="szse",
            source_url="https://docs.static.szse.cn/example.html", collected_at=NOW,
            valid_from=NOW, version="v1", content_hash="d" * 64,
            license_policy="personal-non-commercial-research", quality_status="VALID",
            ts_code="000001.SZ", record_date=date(2026, 6, 1),
            ex_date=date(2026, 6, 2), cash_dividend_per_share=Decimal("0.3"),
        )],
    )
    return ReportRepository(database), tmp_path / "reports", normalized


def test_full_market_endpoint_is_paginated_and_exposes_quality_denominators(tmp_path: Path) -> None:
    repository, report_root, normalized = seed(tmp_path)
    response = TestClient(create_app(repository, report_root, normalized)).get(
        "/api/market/securities",
        params={
            "market_date": "2026-07-22", "page": 1, "page_size": 20,
            "minimum_dividend_yield": "0.05", "dividend_data": "AVAILABLE",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["universe_count"] == 2
    assert payload["summary"]["market_bar_count"] == 2
    assert payload["summary"]["dividend_security_count"] == 1
    assert payload["items"][0]["ts_code"] == "000001.SZ"
    assert payload["items"][0]["dividend_yield"] == "0.06"


def test_full_market_endpoint_rejects_unknown_dates_instead_of_using_stale_data(
    tmp_path: Path,
) -> None:
    repository, report_root, normalized = seed(tmp_path)
    response = TestClient(create_app(repository, report_root, normalized)).get(
        "/api/market/securities", params={"market_date": "2026-07-21"}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "MARKET_SNAPSHOT_NOT_FOUND"


def test_full_market_endpoints_default_to_latest_persisted_snapshots(tmp_path: Path) -> None:
    repository, report_root, normalized = seed(tmp_path)
    FullMarketResearchService(
        state=StateRepository(repository.path),
        data_dir=tmp_path,
        clock=lambda: NOW,
    ).build(date(2026, 7, 22), target_size=2, minimum_amount=Decimal("1"))
    client = TestClient(create_app(repository, report_root, normalized))

    market = client.get("/api/market/securities")
    research = client.get("/api/market/research")

    assert market.status_code == 200
    assert market.json()["market_date"] == "2026-07-22"
    assert research.status_code == 200
    assert research.json()["market_date"] == "2026-07-22"


def test_full_market_events_use_the_live_repository_and_frozen_market_cutoff(
    tmp_path: Path,
) -> None:
    repository, report_root, normalized = seed(tmp_path)
    OfficialEventRepository(repository.path).save_version(
        OfficialEvent(
            record_id="event-stats-20260721", source_id="stats",
            source_url="https://www.stats.gov.cn/event.html",
            published_at=datetime(2026, 7, 21, 9, 30, tzinfo=UTC),
            effective_at=datetime(2026, 7, 21, 9, 30, tzinfo=UTC),
            collected_at=NOW, valid_from=NOW, version="v1",
            content_hash="f" * 64,
            license_policy="official-facts-summary-link-personal-research",
            quality_status="VALID", institution="国家统计局",
            event_type="MACRO_DATA", title="宏观数据公告",
            factual_summary="官方发布宏观数据。",
            affected_ts_codes=(), impact_horizon="3_TO_6_MONTHS",
            confidence=Decimal("0.9"),
        )
    )
    event_repository = OfficialEventRepository(repository.path)
    for source_id in ("csrc", "sse", "stats", "szse"):
        event_repository.save_source_scan(
            OfficialEventSourceScan(
                scan_id=f"scan-{source_id}", source_id=source_id,
                market_date=date(2026, 7, 22),
                listing_url=f"https://www.{source_id}.gov.cn/events/"
                if source_id in {"csrc", "stats"}
                else f"https://www.{source_id}.com.cn/events/",
                status="SUCCESS", event_count=1 if source_id == "stats" else 0,
                content_hash="e" * 64, scanned_at=NOW,
            )
        )

    response = TestClient(create_app(repository, report_root, normalized)).get(
        "/api/market/events", params={"market_date": "2026-07-22"}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["event_cutoff_at"] == "2026-07-22T23:59:59+08:00"
    assert [item["record_id"] for item in payload["events"]] == [
        "event-stats-20260721"
    ]
    assert payload["source_coverage_status"] == "COMPLETE"
    assert payload["successful_scan_source_ids"] == ["csrc", "sse", "stats", "szse"]


def test_xbrl_status_reports_valid_pdf_fallback_only_after_both_exchange_scans(
    tmp_path: Path,
) -> None:
    repository, report_root, normalized = seed(tmp_path)
    FullMarketResearchService(
        state=StateRepository(repository.path), data_dir=tmp_path, clock=lambda: NOW
    ).build(date(2026, 7, 22), target_size=2, minimum_amount=Decimal("1"))
    scans = ExchangeXbrlDiscoveryRepository(repository.path)
    for source_id in ("sse", "szse"):
        scans.save(
            ExchangeXbrlDiscoveryScan(
                scan_id=f"xbrl-{source_id}", source_id=source_id,
                market_date=date(2026, 7, 22),
                listing_url=f"https://www.{source_id}.com.cn/disclosure/regular/",
                status="UNAVAILABLE", instance_count=0, content_hash="f" * 64,
                scanned_at=NOW, reason_code="PUBLIC_INSTANCE_NOT_EXPOSED",
            )
        )

    payload = TestClient(create_app(repository, report_root, normalized)).get(
        "/api/market/xbrl-status"
    ).json()

    assert payload["availability_status"] == "PDF_FALLBACK"
    assert [item["source_id"] for item in payload["scans"]] == ["sse", "szse"]


def test_full_market_research_endpoint_exposes_funnel_and_depth_quality(tmp_path: Path) -> None:
    repository, report_root, normalized = seed(tmp_path)
    FullMarketResearchService(
        state=StateRepository(repository.path),
        data_dir=tmp_path,
        clock=lambda: NOW,
    ).build(date(2026, 7, 22), target_size=2, minimum_amount=Decimal("1"))

    response = TestClient(create_app(repository, report_root, normalized)).get(
        "/api/market/research", params={"market_date": "2026-07-22"}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["market_universe_count"] == 2
    assert payload["funnel_count"] == 2
    assert payload["evidence_item_count"] == 24
    assert payload["pools"]["STABLE_DIVIDEND"]["status"] == "BLOCKED"
    assert payload["funnel"][0]["evidence"]["ready_for_scoring"] is False


def test_full_market_research_endpoint_does_not_build_missing_snapshot(tmp_path: Path) -> None:
    repository, report_root, normalized = seed(tmp_path)

    response = TestClient(create_app(repository, report_root, normalized)).get(
        "/api/market/research", params={"market_date": "2026-07-22"}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "FULL_MARKET_RESEARCH_NOT_FOUND"


def test_evidence_status_and_local_optimistic_review_api(tmp_path: Path) -> None:
    repository, report_root, normalized = seed(tmp_path)
    snapshot = FullMarketResearchService(
        state=StateRepository(repository.path),
        data_dir=tmp_path,
        clock=lambda: NOW,
    ).build(date(2026, 7, 22), target_size=2, minimum_amount=Decimal("1"))
    evidence = FullMarketEvidenceRepository(repository.path)
    run = FullMarketEvidencePlanner(repository=evidence, clock=lambda: NOW).plan(
        snapshot, EvidenceCohort.LIQUIDITY_FILL
    )
    _, tasks = evidence.list_tasks(run_id=run.run_id, page_size=100)
    task = next(item for item in tasks if item.evidence_kind.value == "RISK_SCREEN")
    for status in (
        EvidenceTaskStatus.DISCOVERED,
        EvidenceTaskStatus.DOWNLOADED,
        EvidenceTaskStatus.PARSED,
        EvidenceTaskStatus.AWAITING_REVIEW,
    ):
        task = evidence.transition(
            task.task_id,
            expected_version=task.version,
            status=status,
            observed_at=NOW,
            updates=(
                {
                    "source_id": "cninfo",
                    "source_url": "https://static.cninfo.com.cn/finalpage/report.PDF",
                    "published_at": NOW,
                    "collected_at": NOW,
                    "raw_object_hash": "a" * 64,
                }
                if status is EvidenceTaskStatus.DISCOVERED
                else None
            ),
        )
    client = TestClient(create_app(repository, report_root, normalized))

    status_response = client.get(
        "/api/market/evidence-status", params={"status": "AWAITING_REVIEW"}
    )
    denied = client.post(
        f"/api/market/evidence-reviews/{task.task_id}/decision",
        json={
            "expected_version": task.version,
            "decision": "CONFIRM",
            "reviewed_values": {},
            "note": "",
        },
    )
    confirmed = client.post(
        f"/api/market/evidence-reviews/{task.task_id}/decision",
        headers={"Origin": "http://127.0.0.1:4173"},
        json={
            "expected_version": task.version,
            "decision": "CONFIRM",
            "reviewed_values": {
                "audit_opinion_standard": True,
                "major_investigation_open": False,
                "delisting_risk": False,
                "st_status": None,
                "is_suspended": False,
                "publication_order_known": True,
            },
            "note": "confirmed",
        },
    )

    assert status_response.status_code == 200
    assert status_response.json()["total"] == 1
    assert denied.status_code == 403
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "SATISFIED"
