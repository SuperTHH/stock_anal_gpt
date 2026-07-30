from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from hengce.state.repository import StateRepository

AcquisitionMode = Literal["manual-only", "approved-public"]


@dataclass(frozen=True, slots=True)
class PilotStageContext:
    stage_name: str
    market_date: date
    report_cutoff_at: datetime
    known_at: datetime
    acquisition_mode: AcquisitionMode
    input_hash: str
    data_dir: Path


@dataclass(frozen=True, slots=True)
class PilotRunSummary:
    market_date: date
    report_cutoff_at: datetime
    known_at: datetime
    acquisition_mode: AcquisitionMode
    stage_statuses: dict[str, str]
    stage_output_hashes: dict[str, str]
    failed_stage: str | None
    error_code: str | None
    universe_id: str | None
    backup_path: Path | None
    aggregate_summary: dict[str, object]


StageHandler = Callable[[PilotStageContext], Mapping[str, object]]


class HistoricalPilotRunner:
    STAGES = (
        "01_backup_and_migrate",
        "02_validate_inputs",
        "03_freeze_universe",
        "04_plan_acquisition",
        "05_acquire_public_documents",
        "06_scan_manual_inbox",
        "07_ingest_documents",
        "08_assemble_point_in_time_facts",
        "09_calculate_metrics",
        "10_run_filters_and_pools",
        "11_publish_report",
        "12_write_run_summary",
    )

    def __init__(
        self,
        *,
        state: StateRepository,
        data_dir: Path,
        stage_handlers: Mapping[str, StageHandler],
        clock: Callable[[], datetime],
    ) -> None:
        expected = set(self.STAGES[1:])
        if set(stage_handlers) != expected:
            raise ValueError("PILOT_STAGE_HANDLER_SET_INVALID")
        self.state = state
        self.data_dir = data_dir
        self.stage_handlers = dict(stage_handlers)
        self.clock = clock

    def run(
        self,
        *,
        market_date: date,
        report_cutoff_at: datetime,
        known_at: datetime,
        acquisition_mode: AcquisitionMode,
    ) -> PilotRunSummary:
        self._validate_inputs(
            market_date,
            report_cutoff_at,
            known_at,
            acquisition_mode,
        )
        root_payload = {
            "market_date": market_date.isoformat(),
            "report_cutoff_at": report_cutoff_at.isoformat(),
            "known_at": known_at.isoformat(),
            "acquisition_mode": acquisition_mode,
        }
        input_hash = _stable_hash(root_payload)
        statuses: dict[str, str] = {}
        hashes: dict[str, str] = {}
        universe_id: str | None = None
        backup_path: Path | None = None
        aggregate: dict[str, object] = {}

        for stage in self.STAGES:
            checkpoint = self._load_checkpoint(market_date, stage)
            if (
                checkpoint is not None
                and checkpoint.get("input_hash") == input_hash
                and checkpoint.get("status")
                in {"SUCCEEDED", "SKIPPED_MANUAL_ONLY"}
            ):
                output_hash = str(checkpoint["output_hash"])
                statuses[stage] = "REUSED"
                hashes[stage] = output_hash
                output = checkpoint.get("output")
                if isinstance(output, dict):
                    universe_id = str(
                        output.get("universe_id") or universe_id or ""
                    ) or None
                    if output.get("backup_path"):
                        backup_path = Path(str(output["backup_path"]))
                    aggregate.update(output)
                input_hash = output_hash
                continue

            try:
                if stage == "01_backup_and_migrate":
                    output = self._backup_and_migrate()
                    status = "SUCCEEDED"
                elif (
                    stage == "05_acquire_public_documents"
                    and acquisition_mode == "manual-only"
                ):
                    output = {
                        "acquisition_mode": acquisition_mode,
                        "network_calls": 0,
                        "universe_id": universe_id,
                    }
                    status = "SKIPPED_MANUAL_ONLY"
                else:
                    context = PilotStageContext(
                        stage_name=stage,
                        market_date=market_date,
                        report_cutoff_at=report_cutoff_at,
                        known_at=known_at,
                        acquisition_mode=acquisition_mode,
                        input_hash=input_hash,
                        data_dir=self.data_dir,
                    )
                    output = dict(self.stage_handlers[stage](context))
                    if (
                        stage == "05_acquire_public_documents"
                        and acquisition_mode == "approved-public"
                        and output.get("policy_guarded") is not True
                    ):
                        raise ValueError(
                            "PILOT_ACQUISITION_POLICY_GUARD_REQUIRED"
                        )
                    status = "SUCCEEDED"
            except Exception as error:
                statuses[stage] = "FAILED"
                return PilotRunSummary(
                    market_date=market_date,
                    report_cutoff_at=report_cutoff_at,
                    known_at=known_at,
                    acquisition_mode=acquisition_mode,
                    stage_statuses=statuses,
                    stage_output_hashes=hashes,
                    failed_stage=stage,
                    error_code=str(error) or error.__class__.__name__,
                    universe_id=universe_id,
                    backup_path=backup_path,
                    aggregate_summary=aggregate,
                )

            output_hash = _stable_hash(
                {
                    "stage": stage,
                    "input_hash": input_hash,
                    "output": output,
                }
            )
            self._save_checkpoint(
                market_date,
                stage,
                input_hash,
                output_hash,
                status,
                output,
            )
            statuses[stage] = status
            hashes[stage] = output_hash
            universe_id = str(
                output.get("universe_id") or universe_id or ""
            ) or None
            if output.get("backup_path"):
                backup_path = Path(str(output["backup_path"]))
            aggregate.update(output)
            input_hash = output_hash

        return PilotRunSummary(
            market_date=market_date,
            report_cutoff_at=report_cutoff_at,
            known_at=known_at,
            acquisition_mode=acquisition_mode,
            stage_statuses=statuses,
            stage_output_hashes=hashes,
            failed_stage=None,
            error_code=None,
            universe_id=universe_id,
            backup_path=backup_path,
            aggregate_summary=aggregate,
        )

    def _backup_and_migrate(self) -> dict[str, object]:
        self._integrity_check(self.state.path)
        backups = self.data_dir / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        timestamp = self.clock().astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        temporary = backups / f".hengce-{timestamp}.sqlite3.tmp"
        self.state.backup_to(temporary)
        self._integrity_check(temporary)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        target = backups / f"hengce-{timestamp}-{digest}.sqlite3"
        temporary.replace(target)
        self.state.migrate()
        self._integrity_check(self.state.path)
        return {
            "backup_path": str(target),
            "backup_sha256": digest,
        }

    def _checkpoint_key(self, market_date: date, stage: str) -> str:
        return f"pilot_reconstruction:{market_date.isoformat()}:{stage}"

    def _load_checkpoint(
        self,
        market_date: date,
        stage: str,
    ) -> dict[str, object] | None:
        raw = self.state.get_checkpoint(
            self._checkpoint_key(market_date, stage)
        )
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError("PILOT_CHECKPOINT_INVALID") from error
        if not isinstance(parsed, dict):
            raise ValueError("PILOT_CHECKPOINT_INVALID")
        return parsed

    def _save_checkpoint(
        self,
        market_date: date,
        stage: str,
        input_hash: str,
        output_hash: str,
        status: str,
        output: Mapping[str, object],
    ) -> None:
        self.state.save_checkpoint(
            self._checkpoint_key(market_date, stage),
            json.dumps(
                {
                    "input_hash": input_hash,
                    "output_hash": output_hash,
                    "status": status,
                    "output": dict(output),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    def _validate_inputs(
        self,
        market_date: date,
        report_cutoff_at: datetime,
        known_at: datetime,
        acquisition_mode: str,
    ) -> None:
        for value in (report_cutoff_at, known_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("PILOT_RUN_CUTOFF_INVALID")
        if report_cutoff_at.date() != market_date:
            raise ValueError("PILOT_RUN_MARKET_DATE_MISMATCH")
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("PILOT_RUN_CLOCK_INVALID")
        if known_at > now:
            raise ValueError("PILOT_RUN_KNOWN_AT_FUTURE")
        if acquisition_mode not in {"manual-only", "approved-public"}:
            raise ValueError("PILOT_ACQUISITION_MODE_INVALID")

    @staticmethod
    def _integrity_check(path: Path) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(path)
            row = connection.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error as error:
            raise ValueError("STATE_INTEGRITY_CHECK_FAILED") from error
        finally:
            if connection is not None:
                connection.close()
        if row is None or str(row[0]).lower() != "ok":
            raise ValueError("STATE_INTEGRITY_CHECK_FAILED")


def _stable_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "HistoricalPilotRunner",
    "PilotRunSummary",
    "PilotStageContext",
]
