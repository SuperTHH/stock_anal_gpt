from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from hengce.contracts.enums import QualityStatus, StrategyType
from hengce.contracts.official_event import OfficialEvent, OfficialEventSourceScan
from hengce.state.event_repository import OfficialEventRepository
from hengce.state.repository import StateRepository

NOW = datetime(2026, 7, 22, 15, 59, tzinfo=UTC)


def _event(
    record_id: str,
    *,
    published_at: datetime,
    valid_from: datetime,
    supersedes_id: str | None = None,
    title: str = "官方事件",
) -> OfficialEvent:
    return OfficialEvent(
        record_id=record_id,
        source_id="csrc",
        source_url="https://www.csrc.gov.cn/csrc/c100028/content.shtml",
        published_at=published_at,
        effective_at=published_at,
        collected_at=valid_from,
        version=f"version-{record_id}",
        content_hash="a" * 64,
        license_policy="official-public-personal-research",
        quality_status=QualityStatus.VALID,
        supersedes_id=supersedes_id,
        valid_from=valid_from,
        institution="中国证监会",
        event_type="REGULATORY_POLICY",
        title=title,
        factual_summary="监管部门召开座谈会并公布政策安排。",
        system_assessment="属于中期制度建设信号，不构成个股买入结论。",
        affected_scope="A_SHARE_MARKET",
        affected_ts_codes=(),
        related_strategies=tuple(StrategyType),
        impact_horizon="6_TO_12_MONTHS",
        confidence=Decimal("0.85"),
    )


def _repository(tmp_path: Path) -> OfficialEventRepository:
    path = tmp_path / "state.sqlite3"
    StateRepository(path).migrate()
    return OfficialEventRepository(path)


def test_event_visibility_respects_event_cutoff_and_known_at(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    event = _event(
        "event-policy",
        published_at=NOW - timedelta(days=1),
        valid_from=NOW,
    )
    repository.save_version(event)

    assert repository.visible_events(
        as_of=event.published_at - timedelta(seconds=1), known_at=NOW
    ) == ()
    assert repository.visible_events(
        as_of=NOW, known_at=event.valid_from - timedelta(seconds=1)
    ) == ()
    assert repository.visible_events(as_of=NOW, known_at=NOW) == (event,)


def test_latest_source_scans_distinguish_valid_empty_from_failed_scan(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    scan_date = date(2026, 7, 22)
    old = OfficialEventSourceScan(
        scan_id="scan-sse-old", source_id="sse", market_date=scan_date,
        listing_url="https://www.sse.com.cn/events/", status="FAILED",
        event_count=0, content_hash="a" * 64, scanned_at=NOW - timedelta(hours=1),
        error_code="TIMEOUT",
    )
    current = old.model_copy(update={
        "scan_id": "scan-sse-current", "status": "SUCCESS",
        "content_hash": "b" * 64, "scanned_at": NOW, "error_code": None,
    })
    repository.save_source_scan(old)
    repository.save_source_scan(current)

    assert repository.latest_source_scans(
        market_date=scan_date, known_at=NOW - timedelta(minutes=30)
    ) == (old,)
    assert repository.latest_source_scans(
        market_date=scan_date, known_at=NOW
    ) == (current,)


def test_security_scans_require_complete_pagination_and_do_not_use_global_scan(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    scan_date = date(2026, 7, 22)
    global_scan = OfficialEventSourceScan(
        scan_id="global-sse", source_id="sse", market_date=scan_date,
        listing_url="https://www.sse.com.cn/events/", status="SUCCESS",
        event_count=0, content_hash="a" * 64, scanned_at=NOW,
    )
    scoped_scan = OfficialEventSourceScan(
        scan_id="scoped-sse", source_id="sse", market_date=scan_date,
        listing_url="https://www.sse.com.cn/events/?code=600000", status="SUCCESS",
        event_count=0, content_hash="b" * 64, scanned_at=NOW,
        ts_code="600000.SH", scan_start_date=date(2023, 7, 22),
        scan_end_date=scan_date, pagination_complete=True, page_count=3,
    )
    repository.save_source_scan(global_scan)
    repository.save_source_scan(scoped_scan)

    assert repository.latest_security_source_scans(
        ts_code="600000.SH", market_date=scan_date, known_at=NOW
    ) == (scoped_scan,)

    with pytest.raises(ValueError, match="EVENT_SCAN_INCOMPLETE"):
        OfficialEventSourceScan.model_validate(
            scoped_scan.model_copy(update={"scan_id": "partial", "pagination_complete": False})
        )


def test_event_correction_is_append_only_and_idempotent(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    original = _event(
        "event-original",
        published_at=NOW - timedelta(days=3),
        valid_from=NOW - timedelta(days=2),
    )
    correction = _event(
        "event-correction",
        published_at=NOW - timedelta(days=1),
        valid_from=NOW,
        supersedes_id=original.record_id,
        title="官方事件（更正）",
    )
    assert repository.save_version(original) == original
    assert repository.save_version(original) == original
    repository.save_version(correction)

    assert repository.visible_events(as_of=NOW, known_at=NOW) == (correction,)
    with pytest.raises(ValueError, match="^OFFICIAL_EVENT_VERSION_CONFLICT$"):
        repository.save_version(original.model_copy(update={"title": "冲突内容"}))
