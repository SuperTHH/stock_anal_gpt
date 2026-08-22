from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from hengce.contracts.enums import (
    EvidenceCohort,
    EvidenceKind,
    EvidenceTaskStatus,
)
from hengce.contracts.evidence import FullMarketEvidenceRun, FullMarketEvidenceTask
from hengce.contracts.market_research import FullMarketResearchSnapshot, FunnelSecurity
from hengce.state.evidence_repository import FullMarketEvidenceRepository
from hengce.warehouse.dividends import ImplementedDividendWarehouse

_COHORT_ORDER = (
    EvidenceCohort.YIELD_GE_5,
    EvidenceCohort.YIELD_3_TO_5,
    EvidenceCohort.LIQUIDITY_FILL,
)


def evidence_cohort(item: FunnelSecurity) -> EvidenceCohort:
    if item.dividend_yield is not None and item.dividend_yield >= Decimal("0.05"):
        return EvidenceCohort.YIELD_GE_5
    if item.dividend_yield is not None and item.dividend_yield >= Decimal("0.03"):
        return EvidenceCohort.YIELD_3_TO_5
    return EvidenceCohort.LIQUIDITY_FILL


class FullMarketEvidencePlanner:
    def __init__(
        self,
        *,
        repository: FullMarketEvidenceRepository,
        clock: Callable[[], datetime],
    ) -> None:
        self.repository = repository
        self.clock = clock

    def plan_all(
        self, snapshot: FullMarketResearchSnapshot
    ) -> tuple[FullMarketEvidenceRun, ...]:
        return tuple(self.plan(snapshot, cohort) for cohort in _COHORT_ORDER)

    def plan(
        self,
        snapshot: FullMarketResearchSnapshot,
        cohort: EvidenceCohort,
    ) -> FullMarketEvidenceRun:
        created_at = self.clock()
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("EVIDENCE_PLAN_TIME_INVALID")
        members = tuple(item for item in snapshot.funnel if evidence_cohort(item) is cohort)
        config_payload = {
            "snapshot_id": snapshot.snapshot_id,
            "market_date": snapshot.market_date.isoformat(),
            "cohort": cohort.value,
            "member_codes": [item.ts_code for item in members],
            "required_periods": self._required_periods(snapshot.market_date),
        }
        config_hash = self._hash(config_payload)
        run_id = (
            f"evidence-{snapshot.market_date.isoformat()}-"
            f"{cohort.value.lower()}-{config_hash[:16]}"
        )
        tasks = tuple(
            task
            for member in members
            for task in self._tasks(
                run_id=run_id,
                market_date=snapshot.market_date,
                cohort=cohort,
                member=member,
                created_at=created_at,
            )
        )
        run = FullMarketEvidenceRun(
            run_id=run_id,
            snapshot_id=snapshot.snapshot_id,
            market_date=snapshot.market_date,
            cohort=cohort,
            member_codes=tuple(item.ts_code for item in members),
            task_count=len(tasks),
            config_hash=config_hash,
            created_at=created_at,
            updated_at=created_at,
        )
        return self.repository.create_run(run, tasks)

    def _tasks(
        self,
        *,
        run_id: str,
        market_date: date,
        cohort: EvidenceCohort,
        member: FunnelSecurity,
        created_at: datetime,
    ) -> tuple[FullMarketEvidenceTask, ...]:
        identities: list[tuple[EvidenceKind, str, str]] = []
        year = market_date.year
        for annual_year in range(year - 3, year):
            identities.append(
                (EvidenceKind.PERIODIC_REPORT, f"{annual_year}-12-31", f"{annual_year}年年报")
            )
        for quarter_year in (year - 1, year):
            identities.append(
                (EvidenceKind.PERIODIC_REPORT, f"{quarter_year}-03-31", f"{quarter_year}年一季报")
            )
        for dividend_year in range(year - 5, year):
            identities.append(
                (EvidenceKind.DIVIDEND_YEAR, str(dividend_year), f"{dividend_year}年分红")
            )
        identities.extend(
            (
                (EvidenceKind.RISK_SCREEN, "current", "风险证据"),
                (EvidenceKind.CORPORATE_ACTION, "current", "公司行动证据"),
            )
        )
        missing = set(member.evidence.missing_items)
        tasks: list[FullMarketEvidenceTask] = []
        for kind, period, label in identities:
            identity = {
                "run_id": run_id,
                "ts_code": member.ts_code,
                "evidence_kind": kind.value,
                "evidence_period": period,
            }
            task_id = f"evidence-task-{self._hash(identity)}"
            tasks.append(
                FullMarketEvidenceTask(
                    task_id=task_id,
                    run_id=run_id,
                    market_date=market_date,
                    cohort=cohort,
                    ts_code=member.ts_code,
                    security_name=member.name,
                    evidence_kind=kind,
                    evidence_period=period,
                    status=(
                        EvidenceTaskStatus.PLANNED
                        if label in missing
                        else EvidenceTaskStatus.SATISFIED
                    ),
                    version=1,
                    created_at=created_at,
                    updated_at=created_at,
                )
            )
        return tuple(tasks)

    @staticmethod
    def _required_periods(market_date: date) -> list[str]:
        return [
            *(f"{year}-12-31" for year in range(market_date.year - 3, market_date.year)),
            f"{market_date.year - 1}-03-31",
            f"{market_date.year}-03-31",
            *(str(year) for year in range(market_date.year - 5, market_date.year)),
            "risk:current",
            "corporate_action:current",
        ]

    @staticmethod
    def _hash(payload: object) -> str:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


class FullMarketEvidenceStatusService:
    def __init__(self, repository: FullMarketEvidenceRepository) -> None:
        self.repository = repository

    def summary(
        self,
        *,
        market_date: date | None,
        run_id: str | None,
        statuses: tuple[EvidenceTaskStatus, ...],
        page: int,
        page_size: int,
    ) -> dict[str, object]:
        runs = (
            tuple(filter(None, (self.repository.get_run(run_id),)))
            if run_id is not None
            else self.repository.latest_runs(market_date)
        )
        selected_run_ids = {run.run_id for run in runs}
        all_items: list[FullMarketEvidenceTask] = []
        run_summaries: list[dict[str, object]] = []
        for run in runs:
            total, items = self.repository.list_tasks(
                run_id=run.run_id,
                statuses=statuses,
                page=1,
                page_size=max(run.task_count, 1),
            )
            all_items.extend(items)
            counts = self.repository.status_counts(run.run_id)
            satisfied = counts.get(EvidenceTaskStatus.SATISFIED.value, 0)
            run_summaries.append(
                {
                    **run.model_dump(mode="json"),
                    "status_counts": counts,
                    "satisfied_count": satisfied,
                    "completion_ratio": (
                        str(Decimal(satisfied) / Decimal(run.task_count))
                        if run.task_count
                        else "0"
                    ),
                }
            )
        all_items.sort(
            key=lambda item: (
                _COHORT_ORDER.index(item.cohort),
                item.status.value,
                item.evidence_kind.value,
                item.ts_code,
                item.evidence_period,
            )
        )
        total = len(all_items)
        offset = (page - 1) * page_size
        return {
            "market_date": (
                max(run.market_date for run in runs).isoformat() if runs else None
            ),
            "run_ids": sorted(selected_run_ids),
            "runs": run_summaries,
            "page": page,
            "page_size": page_size,
            "page_count": max(1, math.ceil(total / page_size)),
            "total": total,
            "items": [
                item.model_dump(mode="json") for item in all_items[offset:offset + page_size]
            ],
        }


class FullMarketEvidenceReconciliationService:
    """Attach canonical source ids and revoke unsupported inherited completion."""

    def __init__(
        self,
        *,
        repository: FullMarketEvidenceRepository,
        data_dir: Path,
        clock: Callable[[], datetime],
    ) -> None:
        self.repository = repository
        self.dividends = ImplementedDividendWarehouse(data_dir / "normalized")
        self.clock = clock

    def reconcile(self, run_id: str, *, max_tasks: int) -> dict[str, int]:
        run = self.repository.get_run(run_id)
        if run is None:
            raise ValueError("EVIDENCE_RUN_NOT_FOUND")
        implemented: dict[tuple[str, int], list[str]] = {}
        for record in self.dividends.read_records(run.market_date):
            implemented.setdefault(
                (record.ts_code, record.ex_date.year - 1), []
            ).append(record.record_id)
        _, tasks = self.repository.list_tasks(
            run_id=run_id,
            statuses=(EvidenceTaskStatus.SATISFIED,),
            page_size=10000,
        )
        candidates = [task for task in tasks if not task.source_record_ids]
        enriched = revoked = 0
        with sqlite3.connect(self.repository.path) as connection:
            for task in candidates[:max_tasks]:
                source_ids = self._source_ids(connection, task, implemented)
                if source_ids:
                    self.repository.transition(
                        task.task_id,
                        expected_version=task.version,
                        status=EvidenceTaskStatus.SATISFIED,
                        observed_at=self.clock(),
                        updates={"source_record_ids": source_ids},
                    )
                    enriched += 1
                else:
                    self.repository.transition(
                        task.task_id,
                        expected_version=task.version,
                        status=EvidenceTaskStatus.BLOCKED,
                        observed_at=self.clock(),
                        updates={"error_code": "SATISFIED_SOURCE_MISSING"},
                    )
                    revoked += 1
        return {"enriched": enriched, "revoked": revoked}

    @staticmethod
    def _source_ids(
        connection: sqlite3.Connection,
        task: FullMarketEvidenceTask,
        implemented: dict[tuple[str, int], list[str]],
    ) -> tuple[str, ...]:
        if task.evidence_kind is EvidenceKind.PERIODIC_REPORT:
            rows = connection.execute(
                "SELECT filing_id FROM financial_filings WHERE ts_code=? "
                "AND report_period=? AND artifact_status='PUBLISHED' "
                "UNION SELECT filing_id FROM pdf_financial_documents "
                "WHERE ts_code=? AND report_period=?",
                (task.ts_code, task.evidence_period, task.ts_code, task.evidence_period),
            ).fetchall()
            return tuple(sorted(str(row[0]) for row in rows))
        if task.evidence_kind is EvidenceKind.DIVIDEND_YEAR:
            fiscal_year = int(task.evidence_period)
            rows = connection.execute(
                "SELECT record_id FROM annual_dividend_record_versions "
                "WHERE ts_code=? AND fiscal_year=?",
                (task.ts_code, fiscal_year),
            ).fetchall()
            return tuple(
                sorted(
                    {
                        *(str(row[0]) for row in rows),
                        *implemented.get((task.ts_code, fiscal_year), ()),
                    }
                )
            )
        if task.evidence_kind is EvidenceKind.RISK_SCREEN:
            rows = connection.execute(
                "SELECT record_id FROM official_risk_screen_versions WHERE ts_code=?",
                (task.ts_code,),
            ).fetchall()
            return tuple(sorted(str(row[0]) for row in rows))
        action_rows = connection.execute(
            "SELECT record_id FROM corporate_action_versions WHERE ts_code=?",
            (task.ts_code,),
        ).fetchall()
        action_ids = {str(row[0]) for row in action_rows}
        action_ids.update(
            record_id
            for (code, _year), record_ids in implemented.items()
            if code == task.ts_code
            for record_id in record_ids
        )
        if not action_ids:
            manifest_rows = connection.execute(
                "SELECT item_id FROM acquisition_manifest_items WHERE ts_code=? "
                "AND document_kind='CAPITAL_ACTION_TIMELINE' AND status='INGESTED'",
                (task.ts_code,),
            ).fetchall()
            action_ids.update(str(row[0]) for row in manifest_rows)
        return tuple(sorted(action_ids))


__all__ = [
    "FullMarketEvidencePlanner",
    "FullMarketEvidenceReconciliationService",
    "FullMarketEvidenceStatusService",
    "evidence_cohort",
]
