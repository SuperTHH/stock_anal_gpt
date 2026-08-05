from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from hengce.contracts.enums import (
    ActionStatus,
    ActionType,
    QualityStatus,
)
from hengce.contracts.market import CorporateAction
from hengce.state.action_repository import CorporateActionRepository
from hengce.state.repository import StateRepository

NOW = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
HASH = "a" * 64


def action(
    record_id: str,
    *,
    published_at: datetime,
    valid_from: datetime,
    supersedes_id: str | None = None,
    cash: str = "0.5",
) -> CorporateAction:
    return CorporateAction(
        record_id=record_id,
        source_id="sse",
        source_url="https://www.sse.com.cn/disclosure/fixture-action.pdf",
        published_at=published_at,
        effective_at=datetime(2026, 7, 20, tzinfo=UTC),
        collected_at=valid_from,
        version=f"version-{record_id}",
        content_hash=HASH,
        license_policy="fixture-only",
        quality_status=QualityStatus.VALID,
        supersedes_id=supersedes_id,
        valid_from=valid_from,
        ts_code="699999.SH",
        action_type=ActionType.CASH_DIVIDEND,
        record_date=date(2026, 7, 19),
        ex_date=date(2026, 7, 20),
        pay_date=date(2026, 7, 25),
        cash_dividend_per_share=Decimal(cash),
        stock_dividend_ratio=None,
        split_ratio=None,
        rights_ratio=None,
        rights_price=None,
        share_reduction=None,
        action_status=ActionStatus.IMPLEMENTED,
    )


def repository(tmp_path: Path) -> CorporateActionRepository:
    path = tmp_path / "state.sqlite3"
    StateRepository(path).migrate()
    return CorporateActionRepository(path)


def test_correction_is_append_only_and_point_in_time_visibility_switches(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    old = action(
        "action-old",
        published_at=NOW - timedelta(days=10),
        valid_from=NOW - timedelta(days=9),
    )
    correction = action(
        "action-correction",
        published_at=NOW - timedelta(days=5),
        valid_from=NOW - timedelta(days=2),
        supersedes_id=old.record_id,
        cash="0.6",
    )

    assert repo.save_version(old) == old
    assert repo.save_version(correction) == correction
    assert repo.save_version(correction) == correction

    before_publication = repo.visible_actions(
        old.ts_code,
        as_of=correction.published_at - timedelta(seconds=1),
        known_at=NOW,
    )
    before_collection = repo.visible_actions(
        old.ts_code,
        as_of=NOW,
        known_at=correction.valid_from - timedelta(seconds=1),
    )
    after_both = repo.visible_actions(old.ts_code, as_of=NOW, known_at=NOW)

    assert before_publication == (old,)
    assert before_collection == (old,)
    assert after_both == (old, correction)
    assert repo.get_version(old.record_id) == old


def test_repository_rejects_missing_predecessor_and_forked_correction(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    old = action(
        "action-old",
        published_at=NOW - timedelta(days=10),
        valid_from=NOW - timedelta(days=9),
    )
    repo.save_version(old)
    first = action(
        "correction-one",
        published_at=NOW - timedelta(days=5),
        valid_from=NOW - timedelta(days=4),
        supersedes_id=old.record_id,
    )
    repo.save_version(first)
    fork = action(
        "correction-two",
        published_at=NOW - timedelta(days=3),
        valid_from=NOW - timedelta(days=2),
        supersedes_id=old.record_id,
    )
    missing = action(
        "correction-missing",
        published_at=NOW - timedelta(days=1),
        valid_from=NOW,
        supersedes_id="does-not-exist",
    )

    with pytest.raises(ValueError, match="^CORPORATE_ACTION_BRANCH_CONFLICT$"):
        repo.save_version(fork)
    with pytest.raises(ValueError, match="^CORPORATE_ACTION_CHAIN_GAP$"):
        repo.save_version(missing)
