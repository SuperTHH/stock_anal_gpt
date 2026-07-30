from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from hengce.acquisition.planner import AcquisitionPlanner
from hengce.contracts.enums import (
    AcquisitionStatus,
    DocumentKind,
    QualityStatus,
    ReportType,
)
from hengce.contracts.pilot import PilotUniverseMember, PilotUniverseSnapshot

CUTOFF = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
CREATED = datetime(2026, 7, 30, 9, tzinfo=UTC)


def universe(*, reverse: bool = False) -> PilotUniverseSnapshot:
    board_config = (
        ("MAIN_SH", "600", "SH", 8),
        ("MAIN_SZ", "000", "SZ", 8),
        ("CHINEXT", "300", "SZ", 7),
        ("STAR", "688", "SH", 7),
    )
    members: list[PilotUniverseMember] = []
    sequence = 0
    for board, prefix, suffix, count in board_config:
        for rank in range(1, count + 1):
            sequence += 1
            members.append(
                PilotUniverseMember(
                    ts_code=f"{prefix}{rank:03d}.{suffix}",
                    security_name=f"虚构公司{sequence:02d}",
                    board=board,
                    amount=Decimal(1_000_000_000 - sequence),
                    rank_in_board=rank,
                    evidence_record_ids=(f"bar-{sequence}", f"master-{sequence}"),
                )
            )
    if reverse:
        members.reverse()
    return PilotUniverseSnapshot(
        universe_id="pilot-2026-07-22",
        market_date=date(2026, 7, 22),
        report_cutoff_at=CUTOFF,
        algorithm_version="board-liquidity-pilot-v1",
        quotas={"MAIN_SH": 8, "MAIN_SZ": 8, "CHINEXT": 7, "STAR": 7},
        members=tuple(members),
        input_hashes={"market": "a" * 64, "security_master": "b" * 64},
        manifest_hash="c" * 64,
        created_at=CREATED,
    )


def test_plans_exact_360_items_with_150_periodic_reports() -> None:
    """Catches omitting a required document family from the real-data pilot."""
    items = AcquisitionPlanner().build(universe(), CREATED)
    counts = Counter(item.document_kind for item in items)

    assert len(items) == 360
    assert counts == {
        DocumentKind.PERIODIC_REPORT: 150,
        DocumentKind.DIVIDEND_RECORD: 150,
        DocumentKind.CAPITAL_ACTION_TIMELINE: 30,
        DocumentKind.RISK_SCREEN: 30,
    }
    assert all(
        item.status is AcquisitionStatus.PLANNED
        and item.quality_status is QualityStatus.MISSING
        and item.source_url is None
        and item.attempt_count == 0
        for item in items
    )


def test_periodic_and_dividend_period_sets_are_frozen() -> None:
    """Catches adding an unapproved period or dropping a required annual/Q1 filing."""
    items = AcquisitionPlanner().build(universe(), CREATED)
    code_items = [item for item in items if item.ts_code == "600001.SH"]
    periodic = {
        (item.report_type, item.report_period)
        for item in code_items
        if item.document_kind is DocumentKind.PERIODIC_REPORT
    }
    dividends = {
        item.report_period
        for item in code_items
        if item.document_kind is DocumentKind.DIVIDEND_RECORD
    }

    assert periodic == {
        (ReportType.ANNUAL, date(2023, 12, 31)),
        (ReportType.ANNUAL, date(2024, 12, 31)),
        (ReportType.ANNUAL, date(2025, 12, 31)),
        (ReportType.Q1, date(2025, 3, 31)),
        (ReportType.Q1, date(2026, 3, 31)),
    }
    assert dividends == {date(year, 12, 31) for year in range(2021, 2026)}
    assert not any(
        item.report_type in {ReportType.HALF_YEAR, ReportType.Q3}
        for item in code_items
        if item.report_type is not None
    )


def test_source_mapping_uses_exchange_xbrl_before_cninfo_fallback() -> None:
    """Catches planning a commercial or PDF fallback source as the primary filing source."""
    items = AcquisitionPlanner().build(universe(), CREATED)

    assert {
        item.source_id
        for item in items
        if item.ts_code.endswith(".SH")
    } == {"sse"}
    assert {
        item.source_id
        for item in items
        if item.ts_code.endswith(".SZ")
    } == {"szse"}
    assert "cninfo" not in {item.source_id for item in items}


def test_item_identity_and_sorting_do_not_depend_on_member_input_order() -> None:
    """Catches manifest churn caused only by an input sequence difference."""
    first = AcquisitionPlanner().build(universe(), CREATED)
    reordered = AcquisitionPlanner().build(
        universe(reverse=True),
        CREATED.replace(hour=10),
    )

    assert [item.item_id for item in first] == [item.item_id for item in reordered]
    assert [
        (
            item.ts_code,
            item.document_kind.value,
            item.report_period or date.min,
        )
        for item in first
    ] == sorted(
        (
            item.ts_code,
            item.document_kind.value,
            item.report_period or date.min,
        )
        for item in first
    )
    assert len({item.item_id for item in first}) == 360


def test_planner_rejects_ambiguous_creation_time() -> None:
    """Catches a manifest run whose audit clock lacks a timezone."""
    with pytest.raises(ValueError, match="^ACQUISITION_PLAN_TIME_INVALID$"):
        AcquisitionPlanner().build(
            universe(),
            datetime(2026, 7, 30, 9),
        )


def test_planner_rejects_non_30_security_or_wrong_quota_universe() -> None:
    """Catches generating a manifest after the approved pilot boundary has changed."""
    full = universe()
    reduced = PilotUniverseSnapshot.model_validate(
        {
            **full.model_dump(),
            "quotas": {"MAIN_SH": 7, "MAIN_SZ": 8, "CHINEXT": 7, "STAR": 7},
            "members": tuple(
                member for member in full.members if member.ts_code != "600008.SH"
            ),
        }
    )

    with pytest.raises(ValueError, match="^ACQUISITION_PLAN_UNIVERSE_INVALID$"):
        AcquisitionPlanner().build(reduced, CREATED)
