from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from hengce.bootstrap import bootstrap_state
from hengce.cli import build_pilot_runner
from hengce.config import Settings
from hengce.contracts.enums import (
    AcquisitionStatus,
    DiscoveryMethod,
    PoolReadinessStatus,
    QualityStatus,
    StrategyType,
)
from hengce.contracts.market import MarketBar, SecurityMaster
from hengce.contracts.pilot import AcquisitionManifestItem
from hengce.financials.metrics import MetricValue, PilotMetricResult
from hengce.services.pilot_acceptance import PilotAcceptanceValidator
from hengce.services.pilot_pipeline import PilotProductionStages
from hengce.services.pilot_reconstruction import PilotStageContext
from hengce.state.pilot_repository import PilotRepository
from hengce.state.report_repository import ReportRepository
from hengce.strategies.filters import HardFilterResult
from hengce.warehouse.market import MarketWarehouse

MARKET_DATE = date(2026, 7, 22)
SHANGHAI = ZoneInfo("Asia/Shanghai")
CUTOFF = datetime(2026, 7, 22, 21, 30, tzinfo=SHANGHAI)
KNOWN_AT = datetime(2026, 7, 30, 16, 30, tzinfo=SHANGHAI)


def _complete_metric_result(ts_code: str) -> PilotMetricResult:
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
                input_fact_ids=(f"{name}:{ts_code}",),
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
    assert summary.stage_statuses[
        "05_acquire_public_documents"
    ] == "SKIPPED_MANUAL_ONLY"
    assert summary.aggregate_summary["market_bar_count"] == 30
    assert summary.aggregate_summary["manifest_total"] == 360
    assert summary.aggregate_summary["manual_todo_count"] == 360
    assert summary.aggregate_summary["manifest_status_distribution"] == {
        AcquisitionStatus.AWAITING_MANUAL.value: 360
    }
    assert summary.aggregate_summary["pool_statuses"] == {
        strategy.value: PoolReadinessStatus.BLOCKED.value
        for strategy in StrategyType
    }
    report_id = summary.aggregate_summary["report_id"]
    assert isinstance(report_id, str)
    assert ReportRepository(
        settings.data_dir / "state" / "hengce.sqlite3"
    ).latest_report_id() == report_id
    universe = PilotRepository(
        settings.data_dir / "state" / "hengce.sqlite3"
    ).get_universe_for_date(MARKET_DATE)
    assert universe is not None
    assert len(universe.members) == 30
    acceptance = PilotAcceptanceValidator(
        ignore_checker=lambda _path: True
    ).validate(
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
                verified.model_copy(
                    update={"status": AcquisitionStatus.INGESTED}
                ),
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
    manifest = pilot_repository.list_manifest(
        stages._universe(context).universe_id
    )
    selected = next(
        item
        for item in manifest
        if item.document_kind.value == "PERIODIC_REPORT"
    )
    discovered = selected.model_copy(
        update={"status": AcquisitionStatus.DISCOVERED}
    )
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
        strategy.value: PoolReadinessStatus.READY.value
        for strategy in StrategyType
    }
    assert all(
        count > 0
        for count in pool_output["pool_candidate_counts"].values()
    )


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
        strategy.value: PoolReadinessStatus.BLOCKED.value
        for strategy in StrategyType
    }
    assert output["pool_candidate_counts"] == {
        strategy.value: 0 for strategy in StrategyType
    }
