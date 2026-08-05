from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hengce.contracts.enums import QualityStatus
from hengce.contracts.risk import OfficialRiskScreen
from hengce.state.repository import StateRepository
from hengce.state.risk_repository import OfficialRiskScreenRepository

NOW = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)


def _screen(
    record_id: str,
    *,
    published_at: datetime,
    valid_from: datetime,
    effective_at: datetime | None = None,
    supersedes_id: str | None = None,
    investigation: bool = False,
) -> OfficialRiskScreen:
    return OfficialRiskScreen(
        record_id=record_id,
        source_id="sse",
        source_url="https://www.sse.com.cn/disclosure/risk.pdf",
        published_at=published_at,
        effective_at=effective_at or published_at,
        collected_at=valid_from,
        version=f"version-{record_id}",
        content_hash="a" * 64,
        license_policy="fixture-only",
        quality_status=QualityStatus.VALID,
        supersedes_id=supersedes_id,
        valid_from=valid_from,
        ts_code="600001.SH",
        audit_opinion_standard=True,
        major_investigation_open=investigation,
        delisting_risk=False,
        st_status=None,
        is_suspended=False,
        publication_order_known=True,
    )


def _repository(tmp_path: Path) -> OfficialRiskScreenRepository:
    path = tmp_path / "state.sqlite3"
    StateRepository(path).migrate()
    return OfficialRiskScreenRepository(path)


def test_correction_is_append_only_and_visible_only_after_publication_and_review(
    tmp_path: Path,
) -> None:
    """Catches a correction overwriting the risk evidence visible in the past."""
    repository = _repository(tmp_path)
    original = _screen(
        "risk-original",
        published_at=NOW - timedelta(days=10),
        valid_from=NOW - timedelta(days=9),
    )
    correction = _screen(
        "risk-correction",
        published_at=NOW - timedelta(days=5),
        valid_from=NOW - timedelta(days=2),
        supersedes_id=original.record_id,
        investigation=True,
    )
    repository.save_version(original)
    repository.save_version(correction)

    assert (
        repository.visible_screen(
            original.ts_code,
            as_of=correction.published_at - timedelta(seconds=1),
            known_at=NOW,
        )
        == original
    )
    assert (
        repository.visible_screen(
            original.ts_code,
            as_of=NOW,
            known_at=correction.valid_from - timedelta(seconds=1),
        )
        == original
    )
    assert (
        repository.visible_screen(
            original.ts_code,
            as_of=NOW,
            known_at=NOW,
        )
        == correction
    )


def test_repository_rejects_naive_times_before_persistence(
    tmp_path: Path,
) -> None:
    """Catches timezone-free risk facts being ordered as if they were point-in-time safe."""
    repository = _repository(tmp_path)
    invalid = _screen(
        "risk-naive",
        published_at=NOW - timedelta(days=1),
        valid_from=NOW,
    ).model_copy(update={"valid_from": datetime(2026, 7, 22, 13, 30)})

    with pytest.raises(ValueError, match="^OFFICIAL_RISK_SCREEN_TIME_INVALID$"):
        repository.save_version(invalid)


def test_future_effective_risk_screen_is_not_visible_before_cutoff(
    tmp_path: Path,
) -> None:
    """Catches a published risk status leaking in before it becomes effective."""
    repository = _repository(tmp_path)
    future_effective = _screen(
        "risk-future-effective",
        published_at=NOW - timedelta(days=1),
        effective_at=NOW + timedelta(days=1),
        valid_from=NOW - timedelta(hours=1),
    )
    repository.save_version(future_effective)

    assert (
        repository.visible_screen(
            future_effective.ts_code,
            as_of=NOW,
            known_at=NOW,
        )
        is None
    )
    assert (
        repository.visible_screen(
            future_effective.ts_code,
            as_of=NOW + timedelta(days=1),
            known_at=NOW,
        )
        == future_effective
    )
