from datetime import date
from decimal import Decimal
from pathlib import Path

from hengce.actions.calculator import TotalReturnPoint
from hengce.warehouse.derived_market import DerivedMarketWarehouse


def test_total_return_artifact_is_content_addressed_and_replayable(tmp_path: Path) -> None:
    """Catches overwriting a derived history partition or publishing unverifiable rows."""
    warehouse = DerivedMarketWarehouse(tmp_path)
    points = [
        TotalReturnPoint(
            ts_code="699999.SH",
            trade_date=date(2026, 7, 17),
            raw_close=Decimal("10"),
            total_return_index=Decimal("1"),
            algorithm_version="total-return-v1",
            action_record_ids=(),
        ),
        TotalReturnPoint(
            ts_code="699999.SH",
            trade_date=date(2026, 7, 20),
            raw_close=Decimal("9.5"),
            total_return_index=Decimal("1.00"),
            algorithm_version="total-return-v1",
            action_record_ids=("cash-1",),
        ),
    ]

    first = warehouse.write_total_return(points)
    second = warehouse.write_total_return(points)

    assert first == second
    assert first.name.startswith("part-")
    assert warehouse.read_total_return("699999.SH", "total-return-v1") == [
        {
            "action_record_ids": [],
            "algorithm_version": "total-return-v1",
            "raw_close": Decimal("10.0"),
            "total_return_index": Decimal("1.00"),
            "trade_date": date(2026, 7, 17),
            "ts_code": "699999.SH",
        },
        {
            "action_record_ids": ["cash-1"],
            "algorithm_version": "total-return-v1",
            "raw_close": Decimal("9.5"),
            "total_return_index": Decimal("1.00"),
            "trade_date": date(2026, 7, 20),
            "ts_code": "699999.SH",
        },
    ]
