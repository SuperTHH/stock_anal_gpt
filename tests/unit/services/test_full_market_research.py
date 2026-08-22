from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from hengce.contracts.enums import QualityStatus, StrategyType
from hengce.contracts.market import MarketBar, SecurityMaster
from hengce.contracts.market_research import FunnelConfig
from hengce.contracts.market_screen import ImplementedDividend
from hengce.services.full_market_research import (
    FullMarketResearchService,
    _required_dividend_years,
    _required_period_labels,
)
from hengce.state.repository import StateRepository
from hengce.strategies.deep_value import DEEP_VALUE_V1
from hengce.strategies.engine import FactorInput, SecurityStrategyInput
from hengce.strategies.quality_growth import QUALITY_GROWTH_V1
from hengce.strategies.stable_dividend import STABLE_DIVIDEND_FULL_MARKET_V1
from hengce.warehouse.dividends import ImplementedDividendWarehouse
from hengce.warehouse.market import MarketWarehouse
from tests.unit.state.test_repository import exchange_policy

NOW = datetime(2026, 8, 12, tzinfo=UTC)
MARKET_DATE = date(2026, 7, 22)


def security(
    code: str,
    board: str,
    exchange: str,
    *,
    listed: date = date(2020, 1, 1),
) -> SecurityMaster:
    return SecurityMaster(
        ts_code=code,
        symbol=code[:6],
        name=f"股票{code[:6]}",
        exchange=exchange,
        board=board,
        list_date=listed,
        industry_l1="银行",
        is_in_scope=True,
    )


def bar(code: str, amount: str, close: str = "10") -> MarketBar:
    return MarketBar(
        record_id=f"bar-{code}",
        source_id="tushare",
        source_url="http://api.tushare.pro/",
        collected_at=NOW,
        version="daily-v1",
        content_hash="a" * 64,
        license_policy="tushare-daily",
        quality_status="VALID",
        valid_from=NOW,
        ts_code=code,
        trade_date=MARKET_DATE,
        open=Decimal(close), high=Decimal(close), low=Decimal(close),
        close=Decimal(close), pre_close=Decimal(close),
        volume=Decimal("10"), amount=Decimal(amount),
    )


def seed(tmp_path: Path) -> FullMarketResearchService:
    database = tmp_path / "state" / "hengce.sqlite3"
    state = StateRepository(database)
    state.migrate()
    state.upsert_policies([exchange_policy("sse"), exchange_policy("szse")])
    state.save_security_master_snapshot(
        [
            security("600000.SH", "MAIN_SH", "SSE"),
            security("600001.SH", "MAIN_SH", "SSE"),
        ],
        source_id="sse", source_url="https://www.sse.com.cn/master.csv",
        collected_at=NOW, content_hash="b" * 64, version="sse-v1", quality_lineage={},
    )
    state.save_security_master_snapshot(
        [
            security("000001.SZ", "MAIN_SZ", "SZSE"),
            security("300001.SZ", "CHINEXT", "SZSE", listed=date(2026, 1, 1)),
        ],
        source_id="szse", source_url="https://www.szse.cn/master.csv",
        collected_at=NOW, content_hash="c" * 64, version="szse-v1", quality_lineage={},
    )
    normalized = tmp_path / "normalized"
    MarketWarehouse(normalized).write_bars([
        bar("600000.SH", "90000"),
        bar("600001.SH", "200000"),
        bar("000001.SZ", "100000"),
        bar("300001.SZ", "300000"),
    ])
    ImplementedDividendWarehouse(normalized).write_records(
        MARKET_DATE,
        [ImplementedDividend(
            record_id="dividend-1", source_id="sse",
            source_url="https://www.sse.com.cn/dividend", collected_at=NOW,
            valid_from=NOW, version="v1", content_hash="d" * 64,
            license_policy="personal-non-commercial-research", quality_status="VALID",
            ts_code="600000.SH", record_date=date(2026, 6, 1),
            ex_date=date(2026, 6, 2), cash_dividend_per_share=Decimal("0.6"),
        )],
    )
    return FullMarketResearchService(
        state=state,
        data_dir=tmp_path,
        clock=lambda: NOW,
    )


def test_funnel_prefers_high_dividend_then_liquidity_and_excludes_recent_listings(
    tmp_path: Path,
) -> None:
    snapshot = seed(tmp_path).build(MARKET_DATE, target_size=2)

    assert snapshot.market_universe_count == 4
    assert snapshot.low_cost_eligible_count == 3
    assert [item.ts_code for item in snapshot.funnel] == ["600000.SH", "600001.SH"]
    assert snapshot.funnel[0].entry_reasons == ("股息率达到初筛门槛",)
    assert snapshot.high_dividend_funnel_count == 1


def test_funnel_requires_listing_history_for_the_oldest_dividend_year(
    tmp_path: Path,
) -> None:
    service = seed(tmp_path)
    universe = service.state.get_security_master_universe()

    assert service._low_cost_eligible(
        security("000002.SZ", "MAIN_SZ", "SZSE", listed=date(2022, 1, 1)),
        bar("000002.SZ", "100000").model_dump(mode="python"),
        MARKET_DATE,
        FunnelConfig(),
    ) is False
    assert universe.securities


def test_depth_requirements_roll_forward_with_report_year() -> None:
    cutoff = datetime(2027, 8, 21, 21, 30, tzinfo=UTC)

    assert tuple(_required_period_labels(cutoff)) == (
        date(2024, 12, 31),
        date(2025, 12, 31),
        date(2026, 12, 31),
        date(2026, 3, 31),
        date(2027, 3, 31),
    )
    assert _required_dividend_years(cutoff) == (2022, 2023, 2024, 2025, 2026)


def test_depth_evidence_is_fail_closed_and_dynamic_pools_stay_blocked(tmp_path: Path) -> None:
    snapshot = seed(tmp_path).build(MARKET_DATE, target_size=2)

    assert snapshot.evidence_item_count == 24
    assert snapshot.evidence_completed_count == 2
    assert snapshot.depth_ready_count == 0
    assert snapshot.funnel[0].evidence.missing_items == (
        "2023年年报", "2024年年报", "2025年年报", "2025年一季报", "2026年一季报",
        "2021年分红", "2022年分红", "2023年分红", "2024年分红",
        "风险证据",
    )
    assert all(pool.status.value == "BLOCKED" for pool in snapshot.pools.values())
    assert all(not candidates for candidates in snapshot.candidate_pools.values())


def test_research_snapshot_round_trips_as_immutable_local_artifact(tmp_path: Path) -> None:
    service = seed(tmp_path)
    built = service.build(MARKET_DATE, target_size=2)
    loaded = service.read(MARKET_DATE)

    assert loaded == built
    assert loaded is not None
    assert loaded.manifest_hash == built.manifest_hash


def test_dynamic_pools_publish_complete_subset_without_a_fixed_coverage_gate() -> None:
    definitions = {
        StrategyType.QUALITY_GROWTH: QUALITY_GROWTH_V1,
        StrategyType.DEEP_VALUE: DEEP_VALUE_V1,
        StrategyType.STABLE_DIVIDEND: STABLE_DIVIDEND_FULL_MARKET_V1,
    }

    def strategy_input(strategy: StrategyType) -> SecurityStrategyInput:
        factors = {
            spec.name: FactorInput(
                value=(
                    Decimal("0.06")
                    if spec.name == "dividend_yield"
                    else Decimal("1")
                ),
                quality_status=QualityStatus.DERIVED,
                source_record_ids=(f"source-{spec.name}",),
                published_at=NOW,
                effective_at=NOW,
                collected_at=NOW,
                valid_from=NOW,
            )
            for spec in definitions[strategy].factors
        }
        return SecurityStrategyInput(
            ts_code="600000.SH",
            security_name="测试银行",
            industry_l1="银行",
            factors=factors,
            hard_filter_passed=True,
            selection_reasons=("动态证据与因子门槛通过",),
            risk_flags=(),
            catalysts=("暂无结构化催化剂",),
            observe_conditions=("观察下一期同口径因子",),
            invalidate_conditions=("关键因子失效",),
            cycle_position_available=True,
            announced_dividend_only=True,
        )

    pools, candidates = FullMarketResearchService.evaluate_dynamic_pools(
        inputs={strategy: (strategy_input(strategy),) for strategy in StrategyType},
        universe_size=2,
        evidence_complete_count=1,
        report_date=MARKET_DATE,
        report_cutoff=NOW,
        known_at=NOW,
    )

    assert all(pool.status.value == "READY" for pool in pools.values())
    assert all(pool.coverage_ratio == Decimal("0.5") for pool in pools.values())
    assert all(pool.minimum_complete_factor_count == 1 for pool in pools.values())
    assert all(pool.strategy_version.endswith("full-market-v1") for pool in pools.values())
    assert all(len(items) == 1 for items in candidates.values())
    assert candidates[StrategyType.STABLE_DIVIDEND][0].candidate_status.value == "WATCH"
