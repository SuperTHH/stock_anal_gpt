from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from hengce.contracts.enums import QualityStatus
from hengce.contracts.market import MarketBar
from hengce.warehouse.market import MarketWarehouse


def bar(code: str, trade_date: date = date(2026, 7, 24)) -> MarketBar:
    return MarketBar(
        record_id=f"{code}-{trade_date:%Y%m%d}",
        source_id="tushare",
        source_url="http://api.tushare.pro/",
        collected_at=datetime(2026, 7, 24, 21, 31, tzinfo=UTC),
        version=f"daily-{trade_date:%Y%m%d}",
        content_hash="a" * 64,
        license_policy="tushare-daily",
        quality_status=QualityStatus.VALID,
        valid_from=datetime(2026, 7, 24, 21, 31, tzinfo=UTC),
        ts_code=code,
        trade_date=trade_date,
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10.5"),
        pre_close=Decimal("10"),
        volume=Decimal("1000"),
        amount=Decimal("10500"),
    )


def test_write_is_idempotent_and_queryable(tmp_path: Path) -> None:
    warehouse = MarketWarehouse(tmp_path)
    first = warehouse.write_bars([bar("600000.SH"), bar("000001.SZ")])
    second = warehouse.write_bars([bar("000001.SZ"), bar("600000.SH")])

    assert first == second
    assert warehouse.count_bars(date(2026, 7, 24)) == 2
    assert [row["ts_code"] for row in warehouse.read_bars(date(2026, 7, 24))] == [
        "000001.SZ",
        "600000.SH",
    ]


def test_empty_write_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="bars must not be empty"):
        MarketWarehouse(tmp_path).write_bars([])


def test_mixed_trade_dates_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="one trade_date"):
        MarketWarehouse(tmp_path).write_bars(
            [bar("000001.SZ"), bar("600000.SH", date(2026, 7, 23))]
        )


def test_absent_dataset_or_date_is_empty(tmp_path: Path) -> None:
    warehouse = MarketWarehouse(tmp_path)
    assert warehouse.count_bars(date(2026, 7, 24)) == 0
    assert warehouse.read_bars(date(2026, 7, 24)) == []

    warehouse.write_bars([bar("000001.SZ")])
    assert warehouse.count_bars(date(2026, 7, 23)) == 0
    assert warehouse.read_bars(date(2026, 7, 23)) == []
