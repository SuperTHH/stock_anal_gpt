from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from hengce.contracts.enums import QualityStatus
from hengce.contracts.market_screen import ImplementedDividend
from hengce.warehouse.dividends import ImplementedDividendWarehouse

NOW = datetime(2026, 8, 24, tzinfo=UTC)
MARKET_DATE = date(2026, 8, 21)


def dividend(record_id: str, ex_date: date) -> ImplementedDividend:
    return ImplementedDividend(
        record_id=record_id,
        source_id="sse",
        source_url="https://www.sse.com.cn/market/stockdata/dividends/dividend/",
        published_at=None,
        effective_at=NOW,
        collected_at=NOW,
        version="fixture",
        content_hash="a" * 64,
        license_policy="personal-non-commercial-research",
        quality_status=QualityStatus.VALID,
        supersedes_id=None,
        valid_from=NOW,
        ts_code="600000.SH",
        record_date=ex_date,
        ex_date=ex_date,
        cash_dividend_per_share=Decimal("0.30"),
    )


def test_replace_records_keeps_one_active_artifact_and_archives_the_prior_one(
    tmp_path: Path,
) -> None:
    warehouse = ImplementedDividendWarehouse(tmp_path / "normalized")
    first = warehouse.write_records(MARKET_DATE, [dividend("first", date(2026, 6, 1))])

    second = warehouse.replace_records(
        MARKET_DATE,
        [dividend("second", date(2026, 7, 1))],
    )

    assert second != first
    assert [item.record_id for item in warehouse.read_records(MARKET_DATE)] == ["second"]
    assert not first.exists()
    assert (warehouse.archive / f"market_date={MARKET_DATE}" / first.name).is_file()
