import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from hengce.bootstrap import bootstrap_state
from hengce.cli import build_pilot_runner
from hengce.config import Settings
from hengce.contracts.dividend import AnnualDividendRecord
from hengce.contracts.enums import (
    AcquisitionStatus,
    ActionStatus,
    DiscoveryMethod,
    DocumentKind,
    PoolReadinessStatus,
    QualityStatus,
    StrategyType,
)
from hengce.contracts.market import MarketBar, SecurityMaster
from hengce.contracts.official_event import OfficialEvent
from hengce.contracts.pilot import AcquisitionManifestItem
from hengce.contracts.risk import OfficialRiskScreen
from hengce.financials.metrics import MetricValue, PilotMetricResult
from hengce.services.financial_resolution import FinancialDocument
from hengce.services.pilot_acceptance import PilotAcceptanceValidator
from hengce.services.pilot_pipeline import PilotProductionStages
from hengce.services.pilot_reconstruction import PilotStageContext
from hengce.state.dividend_repository import AnnualDividendRepository
from hengce.state.event_repository import OfficialEventRepository
from hengce.state.pdf_financial_repository import PdfFinancialDocumentRepository
from hengce.state.pilot_repository import PilotRepository
from hengce.state.report_repository import ReportRepository
from hengce.state.risk_repository import OfficialRiskScreenRepository
from hengce.strategies.filters import HardFilterResult
from hengce.warehouse.market import MarketWarehouse

MARKET_DATE = date(2026, 7, 22)
SHANGHAI = ZoneInfo("Asia/Shanghai")
CUTOFF = datetime(2026, 7, 22, 21, 30, tzinfo=SHANGHAI)
KNOWN_AT = datetime(2026, 7, 30, 16, 30, tzinfo=SHANGHAI)


def _complete_metric_result(
    ts_code: str,
    source_record_id: str | None = None,
) -> PilotMetricResult:
    values = {
        "roe_2025": Decimal("0.15"),
        "roic_2025": Decimal("0.12"),
        "annual_revenue_growth": Decimal("0.10"),
        "annual_adjusted_profit_growth": Decimal("0.09"),
        "q1_revenue_growth": Decimal("0.11"),
        "q1_adjusted_profit_growth": Decimal("0.10"),
        "cash_flow_quality": Decimal("1.2"),
        "gross_margin_stability": Decimal("0.03"),
        "debt_ratio": Decimal("0.35"),
        "cash_debt_coverage": Decimal("1.1"),
        "pe": Decimal("15"),
        "pb": Decimal("2"),
        "fcf_yield": Decimal("0.05"),
        "current_asset_ratio": Decimal("0.5"),
        "consecutive_dividend_years": Decimal("5"),
        "announced_dividend_yield": Decimal("0.035"),
        "payout_ratio": Decimal("0.45"),
        "fcf_coverage": Decimal("1.5"),
        "dividend_cut_flag": Decimal("0"),
    }
    return PilotMetricResult(
        metrics={
            name: MetricValue(
                value=value,
                quality_status=QualityStatus.DERIVED,
                input_fact_ids=(source_record_id or f"{name}:{ts_code}",),
                algorithm_version="test-v1",
            )
            for name, value in values.items()
        },
        blocked_reasons=(),
        report_cutoff_at=CUTOFF,
        known_at=KNOWN_AT,
        tax_rate_proxy=Decimal("0.25"),
    )


def _seed_real_shape_inputs(data_dir: Path) -> Settings:
    settings = Settings.model_construct(
        data_dir=data_dir,
        tushare_token=None,
        timezone="Asia/Shanghai",
    )
    state = bootstrap_state(settings)
    board_config = (
        ("MAIN_SH", "SSE", "600", "SH", 8),
        ("STAR", "SSE", "688", "SH", 7),
        ("MAIN_SZ", "SZSE", "000", "SZ", 8),
        ("CHINEXT", "SZSE", "300", "SZ", 7),
    )
    securities: list[SecurityMaster] = []
    bars: list[MarketBar] = []
    sequence = 0
    for board, exchange, prefix, suffix, count in board_config:
        for index in range(1, count + 1):
            sequence += 1
            symbol = f"{prefix}{index:03d}"
            ts_code = f"{symbol}.{suffix}"
            securities.append(
                SecurityMaster(
                    ts_code=ts_code,
                    symbol=symbol,
                    name=f"虚构试点公司{sequence:02d}",
                    exchange=exchange,
                    board=board,
                    list_date=date(2020, 1, 1),
                    is_in_scope=True,
                )
            )
            bars.append(
                MarketBar(
                    record_id=f"fixture-bar-{sequence:02d}",
                    source_id="tushare",
                    source_url="http://api.tushare.pro/",
                    collected_at=CUTOFF,
                    version="fixture-daily-20260722",
                    content_hash="9" * 64,
                    license_policy="fixture-only",
                    quality_status=QualityStatus.VALID,
                    valid_from=CUTOFF,
                    ts_code=ts_code,
                    trade_date=MARKET_DATE,
                    open=Decimal("10"),
                    high=Decimal("11"),
                    low=Decimal("9"),
                    close=Decimal("10.5"),
                    pre_close=Decimal("10"),
                    volume=Decimal("1000000"),
                    amount=Decimal("1000000000") - sequence,
                )
            )
    state.save_security_master_snapshot(
        [item for item in securities if item.exchange == "SSE"],
        source_id="sse",
        source_url="https://www.sse.com.cn/assortment/stock/list/share/",
        collected_at=CUTOFF,
        content_hash="1" * 64,
        version="fixture-sse-v1",
        quality_lineage={"fixture": True},
    )
    state.save_security_master_snapshot(
        [item for item in securities if item.exchange == "SZSE"],
        source_id="szse",
        source_url="https://www.szse.cn/market/product/stock/list/",
        collected_at=CUTOFF,
        content_hash="2" * 64,
        version="fixture-szse-v1",
        quality_lineage={"fixture": True},
    )
    MarketWarehouse(data_dir / "normalized").write_bars(bars)
    return settings


def test_manual_only_production_pipeline_publishes_blocked_quality_report(
    tmp_path: Path,
) -> None:
    """Catches shipping a CLI shell whose production stages are unconfigured."""
    settings = _seed_real_shape_inputs(tmp_path / "data")

    summary = build_pilot_runner(settings).run(
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
        known_at=KNOWN_AT,
        acquisition_mode="manual-only",
    )

    assert summary.failed_stage is None
    assert summary.stage_statuses["05_acquire_public_documents"] == "SKIPPED_MANUAL_ONLY"
    assert summary.aggregate_summary["market_bar_count"] == 30
    assert summary.aggregate_summary["manifest_total"] == 360
    assert summary.aggregate_summary["manual_todo_count"] == 360
    assert summary.aggregate_summary["manifest_status_distribution"] == {
        AcquisitionStatus.AWAITING_MANUAL.value: 360
    }
    assert summary.aggregate_summary["pool_statuses"] == {
        strategy.value: PoolReadinessStatus.BLOCKED.value for strategy in StrategyType
    }
    report_id = summary.aggregate_summary["report_id"]
    assert isinstance(report_id, str)
    assert (
        ReportRepository(settings.data_dir / "state" / "hengce.sqlite3").latest_report_id()
        == report_id
    )
    universe = PilotRepository(
        settings.data_dir / "state" / "hengce.sqlite3"
    ).get_universe_for_date(MARKET_DATE)
    assert universe is not None
    assert len(universe.members) == 30
    acceptance = PilotAcceptanceValidator(ignore_checker=lambda _path: True).validate(
        data_dir=settings.data_dir,
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
    )
    assert acceptance.passed
    assert acceptance.errors == ()
    assert acceptance.api_report_id == report_id


def test_production_ingestion_stage_runs_downloaded_cninfo_pdfs(
    tmp_path: Path,
) -> None:
    settings = _seed_real_shape_inputs(tmp_path / "data")
    state = bootstrap_state(settings)
    pilot_repository = PilotRepository(state.path)

    class FakePdfIngestion:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def run(self, item_id: str) -> object:
            self.calls.append(item_id)
            current = pilot_repository.get_manifest_item(item_id)
            assert current is not None
            verified = current.model_copy(
                update={
                    "status": AcquisitionStatus.VERIFIED,
                    "quality_status": QualityStatus.VALID,
                }
            )
            pilot_repository.transition(
                item_id,
                AcquisitionStatus.DOWNLOADED,
                verified,
                KNOWN_AT,
            )
            pilot_repository.transition(
                item_id,
                AcquisitionStatus.VERIFIED,
                verified.model_copy(update={"status": AcquisitionStatus.INGESTED}),
                KNOWN_AT,
            )
            return object()

    fake = FakePdfIngestion()
    stages = PilotProductionStages(
        state=state,
        data_dir=settings.data_dir,
        clock=lambda: KNOWN_AT,
        pdf_ingestion_service=fake,
    )
    context = PilotStageContext(
        stage_name="test",
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
        known_at=KNOWN_AT,
        acquisition_mode="manual-only",
        input_hash="a" * 64,
        data_dir=settings.data_dir,
    )
    stages.freeze_universe(context)
    stages.plan_acquisition(context)
    manifest = pilot_repository.list_manifest(stages._universe(context).universe_id)
    selected = next(item for item in manifest if item.document_kind.value == "PERIODIC_REPORT")
    discovered = selected.model_copy(update={"status": AcquisitionStatus.DISCOVERED})
    pilot_repository.transition(
        selected.item_id,
        AcquisitionStatus.PLANNED,
        discovered,
        KNOWN_AT,
    )
    downloaded = AcquisitionManifestItem.model_validate(
        {
            **discovered.model_dump(),
            "status": AcquisitionStatus.DOWNLOADED,
            "source_id": "cninfo",
            "source_url": "https://static.cninfo.com.cn/report.pdf",
            "discovery_method": DiscoveryMethod.MANUAL_IMPORT,
            "published_at": datetime(2026, 3, 30, tzinfo=SHANGHAI),
            "collected_at": KNOWN_AT,
            "content_hash": "a" * 64,
            "raw_object_hash": "a" * 64,
            "version": "v1",
            "quality_status": QualityStatus.UNVERIFIED,
        }
    )
    pilot_repository.transition(
        selected.item_id,
        AcquisitionStatus.DISCOVERED,
        downloaded,
        KNOWN_AT,
    )

    output = stages.ingest_documents(context)

    assert fake.calls == [selected.item_id]
    assert output["pdf_used_count"] == 1
    assert output["downloaded_pending_ingestion_count"] == 0


def test_first_manual_scan_moves_planned_attachment_through_discovery(
    tmp_path: Path,
) -> None:
    """Catches a valid inbox file attempting the forbidden PLANNED-to-DOWNLOADED jump."""
    settings = _seed_real_shape_inputs(tmp_path / "data")
    state = bootstrap_state(settings)
    pilot_repository = PilotRepository(state.path)
    stages = PilotProductionStages(
        state=state,
        data_dir=settings.data_dir,
        clock=lambda: KNOWN_AT,
    )
    context = PilotStageContext(
        stage_name="test",
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
        known_at=KNOWN_AT,
        acquisition_mode="manual-only",
        input_hash="a" * 64,
        data_dir=settings.data_dir,
    )
    stages.freeze_universe(context)
    stages.plan_acquisition(context)
    selected = next(
        item
        for item in pilot_repository.list_manifest(
            stages._universe(context).universe_id
        )
        if item.document_kind is DocumentKind.PERIODIC_REPORT
        and item.source_id == "sse"
    )
    inbox = settings.data_dir / "manual_inbox"
    inbox.mkdir(parents=True)
    attachment_name = "planned-fixture.xbrl"
    (inbox / attachment_name).write_bytes(
        b'<?xml version="1.0" encoding="UTF-8"?><xbrl></xbrl>'
    )
    (inbox / f"{attachment_name}.json").write_text(
        json.dumps(
            {
                "item_id": selected.item_id,
                "source_url": (
                    "https://www.sse.com.cn/disclosure/planned-fixture.xbrl"
                ),
                "ts_code": selected.ts_code,
                "document_kind": selected.document_kind.value,
                "report_type": selected.report_type.value,
                "report_period": selected.report_period.isoformat(),
                "published_at": "2026-03-30T10:00:00+08:00",
                "downloaded_at": KNOWN_AT.isoformat(),
                "attachment_name": attachment_name,
                "content_type": "application/xbrl+xml",
            }
        ),
        encoding="utf-8",
    )

    stages.scan_manual_inbox(context)

    stored = pilot_repository.get_manifest_item(selected.item_id)
    assert stored is not None
    assert stored.status is AcquisitionStatus.DOWNLOADED
    assert stored.discovery_method is DiscoveryMethod.MANUAL_IMPORT

    output = stages.ingest_documents(context)

    pending = pilot_repository.get_manifest_item(selected.item_id)
    assert pending is not None
    assert pending.status is AcquisitionStatus.DOWNLOADED
    assert output["pdf_used_count"] == 0
    assert output["downloaded_pending_ingestion_count"] == 1


def test_production_ingestion_routes_non_periodic_official_evidence(
    tmp_path: Path,
) -> None:
    """Catches stage 7 leaving downloaded action and risk evidence unprocessed."""
    settings = _seed_real_shape_inputs(tmp_path / "data")
    state = bootstrap_state(settings)
    pilot_repository = PilotRepository(state.path)

    class FakeOfficialEvidenceIngestion:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def run(self, item_id: str) -> object:
            self.calls.append(item_id)
            current = pilot_repository.get_manifest_item(item_id)
            assert current is not None
            verified = current.model_copy(
                update={
                    "status": AcquisitionStatus.VERIFIED,
                    "quality_status": QualityStatus.VALID,
                }
            )
            pilot_repository.transition(
                item_id,
                AcquisitionStatus.DOWNLOADED,
                verified,
                KNOWN_AT,
            )
            pilot_repository.transition(
                item_id,
                AcquisitionStatus.VERIFIED,
                verified.model_copy(update={"status": AcquisitionStatus.INGESTED}),
                KNOWN_AT,
            )
            return object()

    fake = FakeOfficialEvidenceIngestion()
    stages = PilotProductionStages(
        state=state,
        data_dir=settings.data_dir,
        clock=lambda: KNOWN_AT,
        official_evidence_ingestion_service=fake,
    )
    context = PilotStageContext(
        stage_name="test",
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
        known_at=KNOWN_AT,
        acquisition_mode="manual-only",
        input_hash="a" * 64,
        data_dir=settings.data_dir,
    )
    stages.freeze_universe(context)
    stages.plan_acquisition(context)
    manifest = pilot_repository.list_manifest(stages._universe(context).universe_id)
    selected = [
        next(item for item in manifest if item.document_kind is kind)
        for kind in (
            DocumentKind.DIVIDEND_RECORD,
            DocumentKind.CAPITAL_ACTION_TIMELINE,
            DocumentKind.RISK_SCREEN,
        )
    ]
    for index, item in enumerate(selected):
        discovered = item.model_copy(update={"status": AcquisitionStatus.DISCOVERED})
        pilot_repository.transition(
            item.item_id,
            AcquisitionStatus.PLANNED,
            discovered,
            KNOWN_AT,
        )
        downloaded = AcquisitionManifestItem.model_validate(
            {
                **discovered.model_dump(),
                "status": AcquisitionStatus.DOWNLOADED,
                "source_url": f"https://www.sse.com.cn/evidence-{index}.pdf",
                "discovery_method": DiscoveryMethod.MANUAL_IMPORT,
                "published_at": datetime(2026, 3, 30, tzinfo=SHANGHAI),
                "collected_at": KNOWN_AT,
                "content_hash": f"{index + 1}" * 64,
                "raw_object_hash": f"{index + 1}" * 64,
                "version": "v1",
                "quality_status": QualityStatus.UNVERIFIED,
            }
        )
        pilot_repository.transition(
            item.item_id,
            AcquisitionStatus.DISCOVERED,
            downloaded,
            KNOWN_AT,
        )

    output = stages.ingest_documents(context)

    assert fake.calls == sorted(item.item_id for item in selected)
    assert output["downloaded_pending_ingestion_count"] == 0


def test_production_stages_configure_official_evidence_ingestion_by_default(
    tmp_path: Path,
) -> None:
    """Catches the CLI production path depending on test-only service injection."""
    settings = _seed_real_shape_inputs(tmp_path / "data")
    state = bootstrap_state(settings)

    stages = PilotProductionStages(
        state=state,
        data_dir=settings.data_dir,
        clock=lambda: KNOWN_AT,
    )

    assert stages.official_evidence_ingestion_service is not None


def test_published_report_includes_ingested_official_source_lineage(
    tmp_path: Path,
) -> None:
    """Catches quality reports hiding the official source behind stored facts."""
    settings = _seed_real_shape_inputs(tmp_path / "data")
    state = bootstrap_state(settings)
    pilot_repository = PilotRepository(state.path)
    stages = PilotProductionStages(
        state=state,
        data_dir=settings.data_dir,
        clock=lambda: KNOWN_AT,
    )
    context = PilotStageContext(
        stage_name="test",
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
        known_at=KNOWN_AT,
        acquisition_mode="manual-only",
        input_hash="a" * 64,
        data_dir=settings.data_dir,
    )
    stages.freeze_universe(context)
    stages.plan_acquisition(context)
    selected = next(
        item
        for item in pilot_repository.list_manifest(
            stages._universe(context).universe_id
        )
        if item.document_kind is DocumentKind.PERIODIC_REPORT
        and item.source_id == "sse"
    )
    discovered = selected.model_copy(update={"status": AcquisitionStatus.DISCOVERED})
    pilot_repository.transition(
        selected.item_id,
        AcquisitionStatus.PLANNED,
        discovered,
        KNOWN_AT,
    )
    source_url = "https://www.sse.com.cn/disclosure/official-report.pdf"
    downloaded = AcquisitionManifestItem.model_validate(
        {
            **discovered.model_dump(),
            "status": AcquisitionStatus.DOWNLOADED,
            "source_url": source_url,
            "discovery_method": DiscoveryMethod.MANUAL_IMPORT,
            "published_at": datetime(2026, 3, 30, tzinfo=SHANGHAI),
            "collected_at": KNOWN_AT,
            "content_hash": "3" * 64,
            "raw_object_hash": "3" * 64,
            "version": "v1",
            "quality_status": QualityStatus.UNVERIFIED,
        }
    )
    pilot_repository.transition(
        selected.item_id,
        AcquisitionStatus.DISCOVERED,
        downloaded,
        KNOWN_AT,
    )
    verified = downloaded.model_copy(
        update={
            "status": AcquisitionStatus.VERIFIED,
            "quality_status": QualityStatus.VALID,
        }
    )
    pilot_repository.transition(
        selected.item_id,
        AcquisitionStatus.DOWNLOADED,
        verified,
        KNOWN_AT,
    )
    pilot_repository.transition(
        selected.item_id,
        AcquisitionStatus.VERIFIED,
        verified.model_copy(update={"status": AcquisitionStatus.INGESTED}),
        KNOWN_AT,
    )

    stages.publish_report(context)

    stored = ReportRepository(state.path).latest_report()
    assert stored is not None
    sources = json.loads(stored.artifact_path.read_text(encoding="utf-8"))[
        "source_records"
    ]
    assert len(sources) == 1
    assert sources[0]["record_id"] == selected.item_id
    assert sources[0]["source_url"] == source_url
    assert sources[0]["source_name"] == "上海证券交易所"
    assert sources[0]["quality_status"] == QualityStatus.VALID.value


def test_report_source_lineage_resolves_market_master_and_pdf_fact_ids(
    tmp_path: Path,
) -> None:
    """Catches READY candidates referring to facts absent from report lineage."""
    settings = _seed_real_shape_inputs(tmp_path / "data")
    state = bootstrap_state(settings)
    stages = PilotProductionStages(
        state=state,
        data_dir=settings.data_dir,
        clock=lambda: KNOWN_AT,
    )
    context = PilotStageContext(
        stage_name="test",
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
        known_at=KNOWN_AT,
        acquisition_mode="manual-only",
        input_hash="a" * 64,
        data_dir=settings.data_dir,
    )
    stages.freeze_universe(context)
    stages.plan_acquisition(context)
    universe = stages._universe(context)
    member = universe.members[0]
    report_period = date(2026, 3, 31)
    PdfFinancialDocumentRepository(state.path).save(
        report_period,
        FinancialDocument(
            filing_id="pdf-lineage-fixture",
            ts_code=member.ts_code,
            source_id="cninfo",
            source_kind="PDF",
            source_url="https://static.cninfo.com.cn/finalpage/lineage.pdf",
            published_at=datetime(2026, 4, 30, tzinfo=SHANGHAI),
            valid_from=KNOWN_AT,
            version="pdf-v1",
            supersedes_id=None,
            quality_status=QualityStatus.VALID,
            facts={"total_shares": Decimal("100000000")},
            normalization_metadata={"report_period": report_period.isoformat()},
        ),
    )
    AnnualDividendRepository(state.path).save_version(
        AnnualDividendRecord(
            record_id="annual-dividend-lineage-fixture",
            source_id="cninfo",
            source_url="https://static.cninfo.com.cn/finalpage/dividend-lineage.pdf",
            published_at=datetime(2026, 3, 30, tzinfo=SHANGHAI),
            effective_at=datetime(2026, 3, 30, tzinfo=SHANGHAI),
            collected_at=KNOWN_AT,
            version="annual-dividend-v2",
            content_hash="d" * 64,
            license_policy="official-public-attachment-personal-research",
            quality_status=QualityStatus.VALID,
            supersedes_id=None,
            valid_from=KNOWN_AT,
            ts_code=member.ts_code,
            fiscal_year=2025,
            has_cash_dividend=False,
            implementation_status=ActionStatus.ANNOUNCED,
        )
    )
    OfficialEventRepository(state.path).save_version(
        OfficialEvent(
            record_id="official-event-fixture",
            source_id="csrc",
            source_url="https://www.csrc.gov.cn/csrc/c100028/c7646684/content.shtml",
            published_at=datetime(2026, 7, 22, 22, 0, tzinfo=SHANGHAI),
            effective_at=datetime(2026, 7, 22, 22, 0, tzinfo=SHANGHAI),
            collected_at=KNOWN_AT,
            version="2026-07-22",
            content_hash="f" * 64,
            license_policy="official-facts-summary-link-personal-research",
            quality_status=QualityStatus.VALID,
            valid_from=KNOWN_AT,
            institution="中国证监会",
            event_type="REGULATORY_POLICY",
            title="资本市场政策座谈会",
            factual_summary="监管部门公布市场制度建设安排。",
            system_assessment="中期政策信号，不构成自动买入结论。",
            affected_scope="A_SHARE_MARKET",
            affected_ts_codes=(),
            related_strategies=tuple(StrategyType),
            impact_horizon="6_TO_12_MONTHS",
            confidence=Decimal("0.85"),
        )
    )
    context = PilotStageContext(
        **{
            field: getattr(context, field)
            for field in (
                "stage_name",
                "market_date",
                "report_cutoff_at",
                "known_at",
                "acquisition_mode",
                "input_hash",
                "data_dir",
            )
        },
        event_cutoff_at=datetime(2026, 7, 22, 23, 59, tzinfo=SHANGHAI),
    )
    manifest = PilotRepository(state.path).list_manifest(universe.universe_id)

    sources = stages._report_source_records(context, universe, manifest)
    source_ids = {source.record_id for source in sources}

    assert "pdf-lineage-fixture:total_shares" in source_ids
    assert "annual-dividend-lineage-fixture" in source_ids
    assert (
        f"closing-price:{MARKET_DATE.isoformat()}:{member.ts_code}"
        in source_ids
    )
    assert set(member.evidence_record_ids).issubset(source_ids)


def test_ready_pool_report_publishes_when_factor_source_ids_resolve(
    tmp_path: Path,
) -> None:
    """Catches a READY pool being blocked by attachment-only report lineage."""
    settings = _seed_real_shape_inputs(tmp_path / "data")
    state = bootstrap_state(settings)

    class Analyzer:
        def calculate(self, *, universe: object, **_kwargs: object) -> dict:
            return {
                member.ts_code: _complete_metric_result(
                    member.ts_code,
                    member.evidence_record_ids[0],
                )
                for member in universe.members
            }

    stages = PilotProductionStages(
        state=state,
        data_dir=settings.data_dir,
        clock=lambda: KNOWN_AT,
        financial_analyzer=Analyzer(),
        hard_filter_provider=lambda _context, universe: {
            member.ts_code: HardFilterResult(
                passed=True,
                reasons=(),
                filter_version="test-reviewed-risk-v1",
                source_record_ids=(member.evidence_record_ids[0],),
            )
            for member in universe.members
        },
    )
    context = PilotStageContext(
        stage_name="test",
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
        known_at=KNOWN_AT,
        acquisition_mode="manual-only",
        input_hash="a" * 64,
        data_dir=settings.data_dir,
    )
    stages.freeze_universe(context)
    stages.plan_acquisition(context)
    member = stages._universe(context).members[0]
    AnnualDividendRepository(state.path).save_version(
        AnnualDividendRecord(
            record_id="annual-dividend-domain-fixture",
            source_id="cninfo",
            source_url="https://static.cninfo.com.cn/finalpage/dividend-domain.pdf",
            published_at=datetime(2026, 3, 30, tzinfo=SHANGHAI),
            effective_at=datetime(2026, 3, 30, tzinfo=SHANGHAI),
            collected_at=KNOWN_AT,
            version="annual-dividend-v2",
            content_hash="e" * 64,
            license_policy="official-public-attachment-personal-research",
            quality_status=QualityStatus.VALID,
            supersedes_id=None,
            valid_from=KNOWN_AT,
            ts_code=member.ts_code,
            fiscal_year=2025,
            has_cash_dividend=False,
            implementation_status=ActionStatus.ANNOUNCED,
        )
    )

    OfficialEventRepository(state.path).save_version(
        OfficialEvent(
            record_id="official-event-domain-fixture",
            source_id="csrc",
            source_url="https://www.csrc.gov.cn/csrc/c100028/c7646684/content.shtml",
            published_at=datetime(2026, 7, 22, 22, 0, tzinfo=SHANGHAI),
            effective_at=datetime(2026, 7, 22, 22, 0, tzinfo=SHANGHAI),
            collected_at=KNOWN_AT,
            version="2026-07-22",
            content_hash="f" * 64,
            license_policy="official-facts-summary-link-personal-research",
            quality_status=QualityStatus.VALID,
            valid_from=KNOWN_AT,
            institution="中国证监会",
            event_type="REGULATORY_POLICY",
            title="资本市场政策座谈会",
            factual_summary="监管部门公布市场制度建设安排。",
            system_assessment="中期政策信号，不构成自动买入结论。",
            affected_scope="A_SHARE_MARKET",
            affected_ts_codes=(),
            related_strategies=tuple(StrategyType),
            impact_horizon="6_TO_12_MONTHS",
            confidence=Decimal("0.85"),
        )
    )
    context = context.__class__(
        stage_name=context.stage_name,
        market_date=context.market_date,
        report_cutoff_at=context.report_cutoff_at,
        known_at=context.known_at,
        acquisition_mode=context.acquisition_mode,
        input_hash=context.input_hash,
        data_dir=context.data_dir,
        event_cutoff_at=datetime(2026, 7, 22, 23, 59, tzinfo=SHANGHAI),
    )

    output = stages.publish_report(context)

    assert output["pool_statuses"] == {
        strategy.value: PoolReadinessStatus.READY.value
        for strategy in StrategyType
    }
    assert all(count > 0 for count in output["pool_candidate_counts"].values())
    stored = ReportRepository(state.path).latest_report()
    assert stored is not None
    artifact = json.loads(stored.artifact_path.read_text(encoding="utf-8"))
    assert artifact["data_domain_statuses"]["corporate_actions"] == QualityStatus.MISSING.value
    assert artifact["data_domain_statuses"]["dividends"] == QualityStatus.VALID.value
    assert artifact["data_domain_statuses"]["events"] == QualityStatus.VALID.value
    assert artifact["event_cutoff_at"] == "2026-07-22T23:59:00+08:00"
    assert artifact["official_events"][0]["record_id"] == "official-event-domain-fixture"
    assert any(source["domain"] == "events" for source in artifact["source_records"])
    assert artifact["quality_summary"]["annual_dividend_record_count"] == 1


def test_production_metric_stage_feeds_real_results_to_pool_builder(
    tmp_path: Path,
) -> None:
    settings = _seed_real_shape_inputs(tmp_path / "data")
    state = bootstrap_state(settings)

    class Analyzer:
        calls = 0

        def calculate(self, *, universe: object, **_kwargs: object) -> dict:
            self.calls += 1
            return {
                member.ts_code: _complete_metric_result(member.ts_code)
                for member in universe.members
            }

    analyzer = Analyzer()
    stages = PilotProductionStages(
        state=state,
        data_dir=settings.data_dir,
        clock=lambda: KNOWN_AT,
        financial_analyzer=analyzer,
        hard_filter_provider=lambda _context, universe: {
            member.ts_code: HardFilterResult(
                passed=True,
                reasons=(),
                filter_version="test-reviewed-risk-v1",
                source_record_ids=(f"risk:{member.ts_code}",),
            )
            for member in universe.members
        },
    )
    context = PilotStageContext(
        stage_name="test",
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
        known_at=KNOWN_AT,
        acquisition_mode="manual-only",
        input_hash="a" * 64,
        data_dir=settings.data_dir,
    )
    stages.freeze_universe(context)

    metric_output = stages.calculate_metrics(context)
    pool_output = stages.run_pools(context)

    assert metric_output["derived_metric_count"] == 30 * 19
    assert analyzer.calls == 1
    assert pool_output["pool_statuses"] == {
        strategy.value: PoolReadinessStatus.READY.value for strategy in StrategyType
    }
    assert all(count > 0 for count in pool_output["pool_candidate_counts"].values())


def test_complete_metrics_cannot_bypass_missing_official_risk_screen(
    tmp_path: Path,
) -> None:
    settings = _seed_real_shape_inputs(tmp_path / "data")
    state = bootstrap_state(settings)

    class Analyzer:
        def calculate(self, *, universe: object, **_kwargs: object) -> dict:
            return {
                member.ts_code: _complete_metric_result(member.ts_code)
                for member in universe.members
            }

    stages = PilotProductionStages(
        state=state,
        data_dir=settings.data_dir,
        clock=lambda: KNOWN_AT,
        financial_analyzer=Analyzer(),
    )
    context = PilotStageContext(
        stage_name="test",
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
        known_at=KNOWN_AT,
        acquisition_mode="manual-only",
        input_hash="a" * 64,
        data_dir=settings.data_dir,
    )
    stages.freeze_universe(context)
    stages.plan_acquisition(context)

    output = stages.run_pools(context)

    assert output["pool_statuses"] == {
        strategy.value: PoolReadinessStatus.BLOCKED.value for strategy in StrategyType
    }
    assert output["pool_candidate_counts"] == {strategy.value: 0 for strategy in StrategyType}


def test_production_hard_filters_use_explicit_point_in_time_risk_facts(
    tmp_path: Path,
) -> None:
    """Catches marking every imported risk attachment as an automatic pass."""
    settings = _seed_real_shape_inputs(tmp_path / "data")
    state = bootstrap_state(settings)
    pilot_repository = PilotRepository(state.path)
    stages = PilotProductionStages(
        state=state,
        data_dir=settings.data_dir,
        clock=lambda: KNOWN_AT,
    )
    context = PilotStageContext(
        stage_name="test",
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
        known_at=KNOWN_AT,
        acquisition_mode="manual-only",
        input_hash="a" * 64,
        data_dir=settings.data_dir,
    )
    stages.freeze_universe(context)
    stages.plan_acquisition(context)
    universe = stages._universe(context)
    risk_items = {
        item.ts_code: item
        for item in pilot_repository.list_manifest(universe.universe_id)
        if item.document_kind is DocumentKind.RISK_SCREEN
    }
    risk_repository = OfficialRiskScreenRepository(state.path)
    for index, member in enumerate(universe.members[:2]):
        item = risk_items[member.ts_code]
        discovered = item.model_copy(update={"status": AcquisitionStatus.DISCOVERED})
        pilot_repository.transition(
            item.item_id,
            AcquisitionStatus.PLANNED,
            discovered,
            KNOWN_AT,
        )
        downloaded = AcquisitionManifestItem.model_validate(
            {
                **discovered.model_dump(),
                "status": AcquisitionStatus.DOWNLOADED,
                "source_url": f"https://www.sse.com.cn/risk-{index}.pdf",
                "discovery_method": DiscoveryMethod.MANUAL_IMPORT,
                "published_at": datetime(2026, 3, 30, tzinfo=SHANGHAI),
                "collected_at": KNOWN_AT,
                "content_hash": f"{index + 3}" * 64,
                "raw_object_hash": f"{index + 3}" * 64,
                "version": "v1",
                "quality_status": QualityStatus.UNVERIFIED,
            }
        )
        pilot_repository.transition(
            item.item_id,
            AcquisitionStatus.DISCOVERED,
            downloaded,
            KNOWN_AT,
        )
        verified = downloaded.model_copy(
            update={
                "status": AcquisitionStatus.VERIFIED,
                "quality_status": QualityStatus.VALID,
            }
        )
        pilot_repository.transition(
            item.item_id,
            AcquisitionStatus.DOWNLOADED,
            verified,
            KNOWN_AT,
        )
        pilot_repository.transition(
            item.item_id,
            AcquisitionStatus.VERIFIED,
            verified.model_copy(update={"status": AcquisitionStatus.INGESTED}),
            KNOWN_AT,
        )
        risk_repository.save_version(
            OfficialRiskScreen(
                record_id=f"risk-record-{index}",
                source_id="sse",
                source_url=f"https://www.sse.com.cn/risk-{index}.pdf",
                published_at=datetime(2026, 3, 30, tzinfo=SHANGHAI),
                effective_at=datetime(2026, 3, 30, tzinfo=SHANGHAI),
                collected_at=KNOWN_AT,
                version="v1",
                content_hash=f"{index + 3}" * 64,
                license_policy="fixture-only",
                quality_status=QualityStatus.VALID,
                supersedes_id=None,
                valid_from=KNOWN_AT,
                ts_code=member.ts_code,
                audit_opinion_standard=True,
                major_investigation_open=index == 1,
                delisting_risk=False,
                st_status=None,
                is_suspended=False,
                publication_order_known=True,
            )
        )

    filters = stages._production_hard_filters(universe, context)

    assert filters[universe.members[0].ts_code].passed is True
    assert filters[universe.members[1].ts_code].passed is False
    assert filters[universe.members[1].ts_code].reasons == ("HF-08",)
    assert filters[universe.members[2].ts_code].reasons == ("HF-RISK-SCREEN-MISSING",)
    assert filters[universe.members[0].ts_code].source_record_ids[-1] == ("risk-record-0")
