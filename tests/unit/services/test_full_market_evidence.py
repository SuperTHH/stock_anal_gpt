from datetime import UTC, datetime

from hengce.contracts.enums import (
    EvidenceCohort,
    EvidenceTaskStatus,
    ReviewDecision,
)
from hengce.services.full_market_evidence import FullMarketEvidencePlanner
from hengce.state.evidence_repository import FullMarketEvidenceRepository
from tests.unit.services.test_full_market_research import MARKET_DATE, seed

NOW = datetime(2026, 8, 22, tzinfo=UTC)


def test_planner_freezes_three_idempotent_cohorts_with_twelve_tasks_per_member(
    tmp_path,
) -> None:
    service = seed(tmp_path)
    snapshot = service.build(MARKET_DATE, target_size=2)
    repository = FullMarketEvidenceRepository(service.state.path)
    planner = FullMarketEvidencePlanner(repository=repository, clock=lambda: NOW)

    first = planner.plan_all(snapshot)
    second = planner.plan_all(snapshot)

    assert first == second
    assert [run.cohort for run in first] == list(EvidenceCohort)
    assert sum(run.task_count for run in first) == 24
    high = next(run for run in first if run.cohort is EvidenceCohort.YIELD_GE_5)
    assert len(high.member_codes) == 1
    assert repository.status_counts(high.run_id) == {
        "PLANNED": 10,
        "SATISFIED": 2,
    }


def test_risk_review_is_optimistically_locked_and_append_only(tmp_path) -> None:
    service = seed(tmp_path)
    snapshot = service.build(MARKET_DATE, target_size=2)
    repository = FullMarketEvidenceRepository(service.state.path)
    run = FullMarketEvidencePlanner(repository=repository, clock=lambda: NOW).plan(
        snapshot, EvidenceCohort.YIELD_GE_5
    )
    _, tasks = repository.list_tasks(run_id=run.run_id, page_size=100)
    task = next(
        item
        for item in tasks
        if item.evidence_period == "current"
        and item.evidence_kind.value == "RISK_SCREEN"
    )
    for status in (
        EvidenceTaskStatus.DISCOVERED,
        EvidenceTaskStatus.DOWNLOADED,
        EvidenceTaskStatus.PARSED,
        EvidenceTaskStatus.AWAITING_REVIEW,
    ):
        task = repository.transition(
            task.task_id,
            expected_version=task.version,
            status=status,
            observed_at=NOW,
            updates=(
                {
                    "source_id": "cninfo",
                    "source_url": "https://static.cninfo.com.cn/finalpage/report.PDF",
                    "published_at": NOW,
                    "collected_at": NOW,
                    "raw_object_hash": "a" * 64,
                }
                if status is EvidenceTaskStatus.DISCOVERED
                else None
            ),
        )

    reviewed = repository.review_task(
        task.task_id,
        expected_version=task.version,
        decision=ReviewDecision.CONFIRM,
        reviewed_values={
            "audit_opinion_standard": True,
            "major_investigation_open": False,
            "delisting_risk": False,
            "st_status": None,
            "is_suspended": False,
            "publication_order_known": True,
        },
        note="confirmed",
        reviewed_at=NOW,
    )

    assert reviewed.status is EvidenceTaskStatus.SATISFIED
    assert reviewed.version == task.version + 1
    assert reviewed.source_record_ids[-1].startswith("official-risk-")
