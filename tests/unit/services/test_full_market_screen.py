from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from hengce.contracts.market import MarketBar, OfficialTradingStatus, SecurityMaster
from hengce.contracts.market_screen import ImplementedDividend
from hengce.services.full_market_screen import FullMarketScreenService
from hengce.state.repository import StateRepository
from hengce.warehouse.dividends import ImplementedDividendWarehouse
from hengce.warehouse.market import MarketWarehouse

NOW = datetime(2026, 8, 12, tzinfo=UTC)


def security(code: str, name: str, board: str, exchange: str) -> SecurityMaster:
    return SecurityMaster(
        ts_code=code,
        symbol=code[:6],
        name=name,
        exchange=exchange,
        board=board,
        list_date=date(2000, 1, 1),
        industry_l1="银行" if code != "300001.SZ" else None,
        is_in_scope=True,
    )


def bar(code: str, close: str, amount: str = "100") -> MarketBar:
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
        trade_date=date(2026, 7, 22),
        open=Decimal(close), high=Decimal(close), low=Decimal(close),
        close=Decimal(close), pre_close=Decimal(close),
        volume=Decimal("10"), amount=Decimal(amount),
    )


def dividend(code: str, dps: str, ex_date: date, source: str) -> ImplementedDividend:
    return ImplementedDividend(
        record_id=f"dividend-{code}-{ex_date}-{dps}",
        source_id=source,
        source_url=(
            "https://www.sse.com.cn/market/stockdata/dividends/dividend/index_his.shtml"
            if source == "sse" else
            "https://docs.static.szse.cn/www/market/periodical/month/example.html"
        ),
        collected_at=NOW,
        valid_from=NOW,
        version="implemented-v1",
        content_hash="b" * 64,
        license_policy="personal-non-commercial-research",
        quality_status="VALID",
        ts_code=code,
        record_date=ex_date,
        ex_date=ex_date,
        cash_dividend_per_share=Decimal(dps),
    )


def service(
    tmp_path: Path,
    *,
    missing_bar_codes: frozenset[str] = frozenset(),
    official_suspension_codes: frozenset[str] = frozenset(),
) -> FullMarketScreenService:
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()
    # Reader consumes the deterministic merged universe; source policy behavior is
    # covered by repository and collector tests, so seed snapshots directly here.
    from tests.unit.state.test_repository import exchange_policy
    state.upsert_policies([exchange_policy("sse"), exchange_policy("szse")])
    state.save_security_master_snapshot(
        [security("600000.SH", "浦发银行", "MAIN_SH", "SSE")],
        source_id="sse", source_url="https://www.sse.com.cn/master.csv",
        collected_at=NOW, content_hash="c" * 64, version="sse-v1", quality_lineage={},
    )
    state.save_security_master_snapshot(
        [
            security("000001.SZ", "平安银行", "MAIN_SZ", "SZSE"),
            security("300001.SZ", "特锐德", "CHINEXT", "SZSE"),
        ],
        source_id="szse", source_url="https://www.szse.cn/master.csv",
        collected_at=NOW, content_hash="d" * 64, version="szse-v1", quality_lineage={},
    )
    market = MarketWarehouse(tmp_path / "normalized")
    market.write_bars(
        [
            item
            for item in (
                bar("600000.SH", "10"),
                bar("000001.SZ", "5"),
                bar("300001.SZ", "20"),
            )
            if item.ts_code not in missing_bar_codes
        ]
    )
    if official_suspension_codes:
        market.write_trading_statuses(
            [
                OfficialTradingStatus(
                    record_id=f"status-{code}", source_id="szse",
                    source_url="https://www.szse.cn/disclosure/suspension.html",
                    collected_at=NOW, published_at=NOW, effective_at=NOW,
                    version="status-v1", content_hash="e" * 64,
                    license_policy="official-public-disclosure",
                    quality_status="VALID", valid_from=NOW,
                    ts_code=code, trade_date=date(2026, 7, 22),
                    is_trading=False, is_suspended=True,
                    reason="重大事项停牌", evidence_title="停牌公告",
                )
                for code in official_suspension_codes
            ]
        )
    ImplementedDividendWarehouse(tmp_path / "normalized").write_records(
        date(2026, 7, 22),
        [
            dividend("000001.SZ", "0.20", date(2025, 9, 1), "szse"),
            dividend("000001.SZ", "0.10", date(2026, 6, 1), "szse"),
            dividend("600000.SH", "0.40", date(2025, 6, 1), "sse"),
        ],
    )
    return FullMarketScreenService(
        state=state,
        market_warehouse=MarketWarehouse(tmp_path / "normalized"),
        dividend_warehouse=ImplementedDividendWarehouse(tmp_path / "normalized"),
    )


def test_screen_joins_all_securities_and_sums_only_trailing_twelve_months(tmp_path: Path) -> None:
    result = service(tmp_path).query(
        market_date=date(2026, 7, 22), page=1, page_size=20,
        search=None, board=None, minimum_dividend_yield=Decimal("0"),
        dividend_data="ALL", sort_by="DIVIDEND_YIELD", descending=True,
    )

    assert result["summary"]["universe_count"] == 3
    assert result["summary"]["market_bar_count"] == 3
    assert result["summary"]["dividend_security_count"] == 1
    assert result["summary"]["industry_security_count"] == 2
    assert result["summary"]["industry_coverage_ratio"] == str(Decimal(2) / Decimal(3))
    assert result["summary"]["yield_at_least_5_percent_count"] == 1
    assert result["items"][0]["ts_code"] == "000001.SZ"
    assert result["items"][0]["trailing_12m_cash_dividend_per_share"] == "0.30"
    assert result["items"][0]["dividend_yield"] == "0.06"
    assert result["items"][0]["industry_l1"] == "银行"
    assert result["items"][1]["dividend_yield"] is None


def test_missing_daily_bar_is_collection_failure_not_zero_or_valid_empty(
    tmp_path: Path,
) -> None:
    result = service(
        tmp_path,
        missing_bar_codes=frozenset({"300001.SZ"}),
    ).query(
        market_date=date(2026, 7, 22), page=1, page_size=20,
        search="300001", board=None, minimum_dividend_yield=Decimal("0"),
        dividend_data="ALL", sort_by="TS_CODE", descending=False,
    )

    assert result["summary"]["official_no_trading_count"] == 0
    assert result["summary"]["market_collection_failed_count"] == 1
    assert result["items"][0]["close"] is None
    assert result["items"][0]["market_data_status"] == "COLLECTION_FAILED"


def test_missing_daily_bar_with_official_suspension_is_valid_empty(
    tmp_path: Path,
) -> None:
    result = service(
        tmp_path,
        missing_bar_codes=frozenset({"300001.SZ"}),
        official_suspension_codes=frozenset({"300001.SZ"}),
    ).query(
        market_date=date(2026, 7, 22), page=1, page_size=20,
        search="300001", board=None, minimum_dividend_yield=Decimal("0"),
        dividend_data="ALL", sort_by="TS_CODE", descending=False,
    )

    assert result["summary"]["official_no_trading_count"] == 1
    assert result["summary"]["market_collection_failed_count"] == 0
    assert result["items"][0]["market_data_status"] == "OFFICIAL_NO_TRADING"
    assert result["items"][0]["market_data_issue"] == "OFFICIAL_SUSPENSION"
    assert result["items"][0]["trading_status_source_url"].startswith("https://")


def test_screen_filters_search_board_and_yield_then_paginates(tmp_path: Path) -> None:
    result = service(tmp_path).query(
        market_date=date(2026, 7, 22), page=1, page_size=1,
        search="平安", board="MAIN_SZ", minimum_dividend_yield=Decimal("0.05"),
        dividend_data="AVAILABLE", sort_by="TS_CODE", descending=False,
    )

    assert result["total"] == 1
    assert result["page_count"] == 1
    assert result["items"][0]["name"] == "平安银行"
