from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from hengce.contracts.xbrl_discovery import ExchangeXbrlDiscoveryScan
from hengce.state.repository import StateRepository
from hengce.state.xbrl_discovery_repository import ExchangeXbrlDiscoveryRepository

NOW = datetime(2026, 8, 22, tzinfo=UTC)


def scan(scan_id: str, *, source_id: str, status: str, at: datetime):
    return ExchangeXbrlDiscoveryScan(
        scan_id=scan_id,
        source_id=source_id,
        market_date=date(2026, 8, 21),
        listing_url=f"https://www.{source_id}.com.cn/disclosure/regular/",
        status=status,
        instance_count=1 if status == "AVAILABLE" else 0,
        content_hash="a" * 64,
        scanned_at=at,
        reason_code=None if status == "AVAILABLE" else "PUBLIC_INSTANCE_NOT_EXPOSED",
    )


def test_latest_xbrl_scan_is_point_in_time_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    StateRepository(path).migrate()
    repository = ExchangeXbrlDiscoveryRepository(path)
    old = scan("old", source_id="sse", status="FAILED", at=NOW - timedelta(hours=1))
    current = scan("current", source_id="sse", status="UNAVAILABLE", at=NOW)
    repository.save(old)
    repository.save(current)
    repository.save(current)

    assert repository.latest(
        market_date=date(2026, 8, 21), known_at=NOW - timedelta(minutes=30)
    ) == (old,)
    assert repository.latest(
        market_date=date(2026, 8, 21), known_at=NOW
    ) == (current,)
