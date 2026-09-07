from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal

from hengce.state.repository import StateRepository
from hengce.warehouse.dividends import ImplementedDividendWarehouse
from hengce.warehouse.market import MarketWarehouse


class FullMarketScreenService:
    def __init__(
        self,
        *,
        state: StateRepository,
        market_warehouse: MarketWarehouse,
        dividend_warehouse: ImplementedDividendWarehouse,
    ) -> None:
        self.state = state
        self.market_warehouse = market_warehouse
        self.dividend_warehouse = dividend_warehouse

    def query(
        self,
        *,
        market_date: date,
        page: int,
        page_size: int,
        search: str | None,
        board: str | None,
        minimum_dividend_yield: Decimal,
        dividend_data: str,
        sort_by: str,
        descending: bool,
    ) -> dict[str, object]:
        universe = self.state.get_security_master_universe()
        securities = {item.ts_code: item for item in universe.securities}
        bars = self.market_warehouse.read_bars(market_date)
        bar_by_code = {str(item["ts_code"]): item for item in bars}
        trading_status_by_code = {
            str(item["ts_code"]): item
            for item in self.market_warehouse.read_trading_statuses(market_date)
        }
        window_start = market_date - timedelta(days=365)
        dividends = [
            item for item in self.dividend_warehouse.read_records(market_date)
            if window_start < item.ex_date <= market_date
        ]
        by_code: dict[str, list[object]] = {}
        for item in dividends:
            by_code.setdefault(item.ts_code, []).append(item)

        rows: list[dict[str, object]] = []
        dividend_security_count = 0
        industry_security_count = 0
        yield_at_least_5_percent_count = 0
        for ts_code, security in securities.items():
            if security.industry_l1:
                industry_security_count += 1
            bar = bar_by_code.get(ts_code)
            trading_status = trading_status_by_code.get(ts_code)
            official_no_trading = (
                bar is None
                and trading_status is not None
                and trading_status.get("quality_status") == "VALID"
                and trading_status.get("is_trading") is False
                and trading_status.get("is_suspended") is True
            )
            events = by_code.get(ts_code, [])
            dps = sum(
                (item.cash_dividend_per_share for item in events),
                start=Decimal(0),
            ) if events else None
            close = Decimal(str(bar["close"])) if bar is not None else None
            dividend_yield = (
                dps / close if dps is not None and close is not None and close > 0 else None
            )
            if dividend_yield is not None:
                dividend_security_count += 1
                if dividend_yield >= Decimal("0.05"):
                    yield_at_least_5_percent_count += 1
            source_urls = sorted({str(item.source_url) for item in events})
            rows.append(
                {
                    "ts_code": ts_code,
                    "name": security.name,
                    "exchange": security.exchange,
                    "board": security.board,
                    "industry_l1": security.industry_l1,
                    "market_data_status": (
                        "AVAILABLE" if bar is not None else
                        "OFFICIAL_NO_TRADING" if official_no_trading else
                        "COLLECTION_FAILED"
                    ),
                    "market_data_issue": (
                        None if bar is not None else
                        "OFFICIAL_SUSPENSION" if official_no_trading else
                        "DAILY_BAR_NOT_RETURNED"
                    ),
                    "trading_status_source_url": (
                        trading_status.get("source_url") if official_no_trading else None
                    ),
                    "trade_date": market_date.isoformat() if bar is not None else None,
                    "close": str(close) if close is not None else None,
                    "amount": str(bar["amount"]) if bar is not None else None,
                    "trailing_12m_cash_dividend_per_share": str(dps) if dps is not None else None,
                    "dividend_yield": str(dividend_yield) if dividend_yield is not None else None,
                    "dividend_event_count": len(events),
                    "dividend_source_urls": source_urls,
                }
            )

        normalized_search = search.strip().casefold() if search else ""
        filtered = [
            row for row in rows
            if (not normalized_search or normalized_search in str(row["ts_code"]).casefold()
                or normalized_search in str(row["name"]).casefold())
            and (board is None or row["board"] == board)
            and (dividend_data == "ALL"
                 or dividend_data == "AVAILABLE" and row["dividend_yield"] is not None
                 or dividend_data == "MISSING" and row["dividend_yield"] is None)
            and (row["dividend_yield"] is not None
                 and Decimal(str(row["dividend_yield"])) >= minimum_dividend_yield
                 or minimum_dividend_yield == 0)
        ]
        key_functions = {
            "DIVIDEND_YIELD": lambda row: Decimal(str(row["dividend_yield"]))
                if row["dividend_yield"] is not None else Decimal("-1"),
            "AMOUNT": lambda row: Decimal(str(row["amount"]))
                if row["amount"] is not None else Decimal("-1"),
            "TS_CODE": lambda row: str(row["ts_code"]),
        }
        filtered.sort(key=lambda row: str(row["ts_code"]))
        filtered.sort(key=key_functions[sort_by], reverse=descending)
        total = len(filtered)
        offset = (page - 1) * page_size
        return {
            "market_date": market_date.isoformat(),
            "universe_as_of": universe.as_of.isoformat(),
            "universe_hash": universe.universe_hash,
            "page": page,
            "page_size": page_size,
            "page_count": max(1, math.ceil(total / page_size)),
            "total": total,
            "summary": {
                "universe_count": len(securities),
                "market_bar_count": len(bar_by_code),
                "official_no_trading_count": sum(
                    row["market_data_status"] == "OFFICIAL_NO_TRADING" for row in rows
                ),
                "market_collection_failed_count": sum(
                    row["market_data_status"] == "COLLECTION_FAILED" for row in rows
                ),
                "dividend_security_count": dividend_security_count,
                "industry_security_count": industry_security_count,
                "industry_coverage_ratio": str(
                    Decimal(industry_security_count) / Decimal(len(securities))
                    if securities else Decimal(0)
                ),
                "dividend_coverage_ratio": str(
                    Decimal(dividend_security_count) / Decimal(len(securities))
                    if securities else Decimal(0)
                ),
                "yield_at_least_5_percent_count": yield_at_least_5_percent_count,
                "dividend_window_start": window_start.isoformat(),
                "dividend_window_end": market_date.isoformat(),
            },
            "items": filtered[offset:offset + page_size],
        }


__all__ = ["FullMarketScreenService"]
