from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from hengce.bootstrap import bootstrap_state
from hengce.cli import build_pilot_runner
from hengce.config import Settings
from hengce.contracts.enums import (
    AcquisitionStatus,
    PoolReadinessStatus,
    QualityStatus,
    StrategyType,
)
from hengce.contracts.market import MarketBar, SecurityMaster
from hengce.services.pilot_acceptance import PilotAcceptanceValidator
from hengce.state.pilot_repository import PilotRepository
from hengce.state.report_repository import ReportRepository
from hengce.warehouse.market import MarketWarehouse

MARKET_DATE = date(2026, 7, 22)
SHANGHAI = ZoneInfo("Asia/Shanghai")
CUTOFF = datetime(2026, 7, 22, 21, 30, tzinfo=SHANGHAI)
KNOWN_AT = datetime(2026, 7, 30, 16, 30, tzinfo=SHANGHAI)


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
