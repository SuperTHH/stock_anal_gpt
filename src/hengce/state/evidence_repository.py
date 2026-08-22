from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

from hengce.contracts.enums import (
    EvidenceCohort,
    EvidenceKind,
    EvidenceTaskStatus,
    QualityStatus,
    ReviewDecision,
)
from hengce.contracts.evidence import FullMarketEvidenceRun, FullMarketEvidenceTask
from hengce.contracts.risk import OfficialRiskScreen

from .db import connect

_ALLOWED_TRANSITIONS = {
    EvidenceTaskStatus.PLANNED: {
        EvidenceTaskStatus.DISCOVERED,
        EvidenceTaskStatus.AWAITING_REVIEW,
        EvidenceTaskStatus.SATISFIED,
        EvidenceTaskStatus.RETRYABLE_FAILED,
        EvidenceTaskStatus.BLOCKED,
    },
    EvidenceTaskStatus.DISCOVERED: {
        EvidenceTaskStatus.DOWNLOADED,
        EvidenceTaskStatus.RETRYABLE_FAILED,
        EvidenceTaskStatus.BLOCKED,
    },
    EvidenceTaskStatus.DOWNLOADED: {
        EvidenceTaskStatus.PARSED,
        EvidenceTaskStatus.RETRYABLE_FAILED,
        EvidenceTaskStatus.BLOCKED,
    },
    EvidenceTaskStatus.PARSED: {
        EvidenceTaskStatus.AWAITING_REVIEW,
        EvidenceTaskStatus.SATISFIED,
        EvidenceTaskStatus.RETRYABLE_FAILED,
        EvidenceTaskStatus.BLOCKED,
    },
    EvidenceTaskStatus.AWAITING_REVIEW: {
        EvidenceTaskStatus.SATISFIED,
        EvidenceTaskStatus.BLOCKED,
    },
    EvidenceTaskStatus.RETRYABLE_FAILED: {
        EvidenceTaskStatus.PLANNED,
        EvidenceTaskStatus.DISCOVERED,
        EvidenceTaskStatus.DOWNLOADED,
    },
    EvidenceTaskStatus.BLOCKED: {
        EvidenceTaskStatus.PLANNED,
        EvidenceTaskStatus.PARSED,
        EvidenceTaskStatus.AWAITING_REVIEW,
    },
    EvidenceTaskStatus.SATISFIED: {
        EvidenceTaskStatus.SATISFIED,
        EvidenceTaskStatus.BLOCKED,
    },
}


class FullMarketEvidenceRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def create_run(
        self,
        run: FullMarketEvidenceRun,
        tasks: tuple[FullMarketEvidenceTask, ...],
    ) -> FullMarketEvidenceRun:
        validated_run = FullMarketEvidenceRun.model_validate(run.model_dump())
        validated_tasks = tuple(
            FullMarketEvidenceTask.model_validate(task.model_dump()) for task in tasks
        )
        if validated_run.task_count != len(validated_tasks):
            raise ValueError("EVIDENCE_RUN_TASK_COUNT_MISMATCH")
        if any(task.run_id != validated_run.run_id for task in validated_tasks):
            raise ValueError("EVIDENCE_TASK_RUN_MISMATCH")
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_json FROM full_market_evidence_runs "
                "WHERE snapshot_id=? AND cohort=?",
                (validated_run.snapshot_id, validated_run.cohort.value),
            ).fetchone()
            if existing is not None:
                stored = FullMarketEvidenceRun.model_validate_json(
                    str(existing["payload_json"])
                )
                if stored != validated_run:
                    raise ValueError("EVIDENCE_RUN_IMMUTABILITY_CONFLICT")
                return stored
            connection.execute(
                """
                INSERT INTO full_market_evidence_runs(
                    run_id, snapshot_id, market_date, cohort, config_hash,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validated_run.run_id,
                    validated_run.snapshot_id,
                    validated_run.market_date.isoformat(),
                    validated_run.cohort.value,
                    validated_run.config_hash,
                    validated_run.model_dump_json(),
                    validated_run.created_at.isoformat(),
                    validated_run.updated_at.isoformat(),
                ),
            )
            for task in validated_tasks:
                connection.execute(
                    """
                    INSERT INTO full_market_evidence_tasks(
                        task_id, run_id, ts_code, evidence_kind, evidence_period,
                        status, version, payload_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.task_id,
                        task.run_id,
                        task.ts_code,
                        task.evidence_kind.value,
                        task.evidence_period,
                        task.status.value,
                        task.version,
                        task.model_dump_json(),
                        task.updated_at.isoformat(),
                    ),
                )
        return validated_run

    def get_run(self, run_id: str) -> FullMarketEvidenceRun | None:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM full_market_evidence_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return (
            FullMarketEvidenceRun.model_validate_json(str(row["payload_json"]))
            if row is not None
            else None
        )

    def latest_runs(self, market_date: date | None = None) -> tuple[FullMarketEvidenceRun, ...]:
        query = "SELECT payload_json FROM full_market_evidence_runs"
        parameters: tuple[str, ...] = ()
        if market_date is not None:
            query += " WHERE market_date=?"
            parameters = (market_date.isoformat(),)
        query += " ORDER BY market_date DESC, created_at DESC, cohort"
        with connect(self.path) as connection:
            rows = connection.execute(query, parameters).fetchall()
        seen: set[EvidenceCohort] = set()
        result: list[FullMarketEvidenceRun] = []
        for row in rows:
            run = FullMarketEvidenceRun.model_validate_json(str(row["payload_json"]))
            if run.cohort in seen:
                continue
            seen.add(run.cohort)
            result.append(run)
        return tuple(result)

    def get_task(self, task_id: str) -> FullMarketEvidenceTask | None:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM full_market_evidence_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        return (
            FullMarketEvidenceTask.model_validate_json(str(row["payload_json"]))
            if row is not None
            else None
        )

    def list_tasks(
        self,
        *,
        run_id: str | None = None,
        statuses: tuple[EvidenceTaskStatus, ...] = (),
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[int, tuple[FullMarketEvidenceTask, ...]]:
        clauses: list[str] = []
        parameters: list[object] = []
        if run_id is not None:
            clauses.append("run_id=?")
            parameters.append(run_id)
        if statuses:
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            parameters.extend(status.value for status in statuses)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with connect(self.path) as connection:
            total = int(connection.execute(
                "SELECT COUNT(*) FROM full_market_evidence_tasks" + where,
                parameters,
            ).fetchone()[0])
            rows = connection.execute(
                "SELECT payload_json FROM full_market_evidence_tasks"
                + where
                + " ORDER BY status, evidence_kind, ts_code, evidence_period "
                + "LIMIT ? OFFSET ?",
                (*parameters, page_size, (page - 1) * page_size),
            ).fetchall()
        return total, tuple(
            FullMarketEvidenceTask.model_validate_json(str(row["payload_json"]))
            for row in rows
        )

    def status_counts(self, run_id: str) -> dict[str, int]:
        with connect(self.path) as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM full_market_evidence_tasks "
                "WHERE run_id=? GROUP BY status",
                (run_id,),
            ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def transition(
        self,
        task_id: str,
        *,
        expected_version: int,
        status: EvidenceTaskStatus,
        observed_at: datetime,
        updates: dict[str, object] | None = None,
    ) -> FullMarketEvidenceTask:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("EVIDENCE_TRANSITION_TIME_INVALID")
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_json FROM full_market_evidence_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise ValueError("EVIDENCE_TASK_NOT_FOUND")
            current = FullMarketEvidenceTask.model_validate_json(
                str(row["payload_json"])
            )
            if current.version != expected_version:
                raise ValueError("EVIDENCE_TASK_VERSION_CONFLICT")
            if status not in _ALLOWED_TRANSITIONS[current.status]:
                raise ValueError("EVIDENCE_TASK_TRANSITION_INVALID")
            changes = dict(updates or {})
            changes.update(
                status=status,
                version=current.version + 1,
                updated_at=observed_at,
            )
            updated = FullMarketEvidenceTask.model_validate(
                current.model_copy(update=changes).model_dump()
            )
            encoded = updated.model_dump_json()
            payload_hash = hashlib.sha256(encoded.encode()).hexdigest()
            transition_id = hashlib.sha256(
                f"{task_id}:{current.version}:{updated.version}:{payload_hash}".encode()
            ).hexdigest()
            changed = connection.execute(
                """
                UPDATE full_market_evidence_tasks
                SET status=?, version=?, payload_json=?, updated_at=?
                WHERE task_id=? AND version=?
                """,
                (
                    updated.status.value,
                    updated.version,
                    encoded,
                    observed_at.isoformat(),
                    task_id,
                    expected_version,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("EVIDENCE_TASK_VERSION_CONFLICT")
            connection.execute(
                """
                INSERT INTO full_market_evidence_transitions(
                    transition_id, task_id, from_status, to_status,
                    from_version, to_version, payload_hash, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transition_id,
                    task_id,
                    current.status.value,
                    updated.status.value,
                    current.version,
                    updated.version,
                    payload_hash,
                    observed_at.isoformat(),
                ),
            )
        return updated

    def save_review_decision(
        self,
        *,
        task_id: str,
        task_version: int,
        decision: str,
        reviewed_values: dict[str, object],
        note: str,
        reviewed_at: datetime,
    ) -> str:
        payload = json.dumps(
            reviewed_values, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        decision_id = hashlib.sha256(
            f"{task_id}:{task_version}:{decision}:{payload}:{note}".encode()
        ).hexdigest()
        with connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO full_market_review_decisions(
                    decision_id, task_id, task_version, decision,
                    reviewed_values_json, note, reviewed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    task_id,
                    task_version,
                    decision,
                    payload,
                    note,
                    reviewed_at.isoformat(),
                ),
            )
        return decision_id

    def review_task(
        self,
        task_id: str,
        *,
        expected_version: int,
        decision: ReviewDecision,
        reviewed_values: dict[str, bool | str | None],
        note: str,
        reviewed_at: datetime,
    ) -> FullMarketEvidenceTask:
        if reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None:
            raise ValueError("EVIDENCE_REVIEW_TIME_INVALID")
        required = {
            "audit_opinion_standard",
            "major_investigation_open",
            "delisting_risk",
            "st_status",
            "is_suspended",
            "publication_order_known",
        }
        if set(reviewed_values) != required:
            raise ValueError("EVIDENCE_REVIEW_FIELDS_INVALID")
        boolean_fields = required - {"st_status"}
        if any(not isinstance(reviewed_values[field], bool) for field in boolean_fields):
            raise ValueError("EVIDENCE_REVIEW_FIELDS_INVALID")
        if reviewed_values["st_status"] is not None and not isinstance(
            reviewed_values["st_status"], str
        ):
            raise ValueError("EVIDENCE_REVIEW_FIELDS_INVALID")
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_json FROM full_market_evidence_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise ValueError("EVIDENCE_TASK_NOT_FOUND")
            current = FullMarketEvidenceTask.model_validate_json(str(row["payload_json"]))
            if current.version != expected_version:
                raise ValueError("EVIDENCE_TASK_VERSION_CONFLICT")
            if (
                current.status is not EvidenceTaskStatus.AWAITING_REVIEW
                or current.evidence_kind is not EvidenceKind.RISK_SCREEN
            ):
                raise ValueError("EVIDENCE_TASK_NOT_REVIEWABLE")
            next_status = (
                EvidenceTaskStatus.SATISFIED
                if decision is ReviewDecision.CONFIRM
                else EvidenceTaskStatus.BLOCKED
            )
            source_record_ids = current.source_record_ids
            if decision is ReviewDecision.CONFIRM:
                if (
                    current.source_id is None
                    or current.source_url is None
                    or current.published_at is None
                    or current.collected_at is None
                    or current.raw_object_hash is None
                ):
                    raise ValueError("EVIDENCE_REVIEW_SOURCE_INVALID")
                identity = json.dumps(
                    {
                        "task_id": task_id,
                        "task_version": current.version,
                        "reviewed_values": reviewed_values,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                record_id = f"official-risk-{hashlib.sha256(identity).hexdigest()}"
                screen = OfficialRiskScreen(
                    record_id=record_id,
                    source_id=current.source_id,
                    source_url=current.source_url,
                    published_at=current.published_at,
                    effective_at=current.published_at,
                    collected_at=current.collected_at,
                    version=f"full-market-review-v1:{current.version}",
                    content_hash=current.raw_object_hash,
                    license_policy="official-public-attachment-personal-research",
                    quality_status=QualityStatus.VALID,
                    supersedes_id=None,
                    valid_from=reviewed_at,
                    ts_code=current.ts_code,
                    audit_opinion_standard=bool(
                        reviewed_values["audit_opinion_standard"]
                    ),
                    major_investigation_open=bool(
                        reviewed_values["major_investigation_open"]
                    ),
                    delisting_risk=bool(reviewed_values["delisting_risk"]),
                    st_status=(
                        str(reviewed_values["st_status"])
                        if reviewed_values["st_status"] is not None
                        else None
                    ),
                    is_suspended=bool(reviewed_values["is_suspended"]),
                    publication_order_known=bool(
                        reviewed_values["publication_order_known"]
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO official_risk_screen_versions(
                        record_id, ts_code, supersedes_id, published_at, effective_at,
                        collected_at, valid_from, raw_object_hash, quality_status,
                        payload_json, saved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        screen.record_id,
                        screen.ts_code,
                        screen.supersedes_id,
                        screen.published_at.isoformat(),
                        screen.effective_at.isoformat(),
                        screen.collected_at.isoformat(),
                        screen.valid_from.isoformat(),
                        screen.content_hash,
                        screen.quality_status.value,
                        screen.model_dump_json(),
                        reviewed_at.isoformat(),
                    ),
                )
                source_record_ids = tuple(
                    dict.fromkeys((*current.source_record_ids, screen.record_id))
                )
            updated = FullMarketEvidenceTask.model_validate(
                current.model_copy(
                    update={
                        "status": next_status,
                        "version": current.version + 1,
                        "prefilled_values": reviewed_values,
                        "source_record_ids": source_record_ids,
                        "error_code": (
                            None
                            if decision is ReviewDecision.CONFIRM
                            else "RETURNED_FOR_CORRECTION"
                        ),
                        "updated_at": reviewed_at,
                    }
                ).model_dump()
            )
            encoded = updated.model_dump_json()
            payload_hash = hashlib.sha256(encoded.encode()).hexdigest()
            transition_id = hashlib.sha256(
                f"{task_id}:{current.version}:{updated.version}:{payload_hash}".encode()
            ).hexdigest()
            decision_payload = json.dumps(
                reviewed_values,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            decision_id = hashlib.sha256(
                f"{task_id}:{current.version}:{decision.value}:{decision_payload}:{note}".encode()
            ).hexdigest()
            connection.execute(
                """
                UPDATE full_market_evidence_tasks
                SET status=?, version=?, payload_json=?, updated_at=?
                WHERE task_id=? AND version=?
                """,
                (
                    updated.status.value,
                    updated.version,
                    encoded,
                    reviewed_at.isoformat(),
                    task_id,
                    expected_version,
                ),
            )
            connection.execute(
                """
                INSERT INTO full_market_evidence_transitions(
                    transition_id, task_id, from_status, to_status,
                    from_version, to_version, payload_hash, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transition_id,
                    task_id,
                    current.status.value,
                    updated.status.value,
                    current.version,
                    updated.version,
                    payload_hash,
                    reviewed_at.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO full_market_review_decisions(
                    decision_id, task_id, task_version, decision,
                    reviewed_values_json, note, reviewed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    task_id,
                    current.version,
                    decision.value,
                    decision_payload,
                    note,
                    reviewed_at.isoformat(),
                ),
            )
        return updated


__all__ = ["FullMarketEvidenceRepository"]
