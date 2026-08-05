from dataclasses import dataclass
from pathlib import Path

from hengce.contracts.enums import ReportStatus
from hengce.contracts.strategy import ReportSnapshot

from .db import connect


@dataclass(frozen=True, slots=True)
class StoredReport:
    snapshot: ReportSnapshot
    artifact_path: Path


class ReportRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def publish(self, snapshot: ReportSnapshot, artifact_path: Path) -> StoredReport:
        if snapshot.report_status not in {
            ReportStatus.PUBLISHED,
            ReportStatus.PUBLISHED_PARTIAL,
        }:
            raise ValueError("REPORT_STATUS_NOT_PUBLISHABLE")
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM report_snapshots WHERE report_id=?",
                (snapshot.report_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["manifest_hash"]) != snapshot.manifest_hash:
                    raise ValueError("REPORT_IMMUTABILITY_CONFLICT")
                return StoredReport(
                    ReportSnapshot.model_validate_json(str(existing["payload_json"])),
                    Path(str(existing["artifact_path"])),
                )
            connection.execute(
                """
                INSERT INTO report_snapshots(
                    report_id, report_date, manifest_hash, artifact_path,
                    payload_json, published_at, report_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.report_id,
                    snapshot.report_date.isoformat(),
                    snapshot.manifest_hash,
                    str(artifact_path),
                    snapshot.model_dump_json(),
                    snapshot.published_at.isoformat() if snapshot.published_at else "",
                    snapshot.report_status.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO report_pointer(pointer_name, report_id)
                VALUES ('latest', ?)
                ON CONFLICT(pointer_name) DO UPDATE SET report_id=excluded.report_id
                """,
                (snapshot.report_id,),
            )
        return StoredReport(snapshot, artifact_path)

    def get_report(self, report_id: str) -> StoredReport | None:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json, artifact_path FROM report_snapshots WHERE report_id=?",
                (report_id,),
            ).fetchone()
        if row is None:
            return None
        snapshot = ReportSnapshot.model_validate_json(str(row["payload_json"]))
        if snapshot.report_status not in {
            ReportStatus.PUBLISHED,
            ReportStatus.PUBLISHED_PARTIAL,
        }:
            return None
        return StoredReport(snapshot, Path(str(row["artifact_path"])))

    def latest_report_id(self) -> str | None:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT report_id FROM report_pointer WHERE pointer_name='latest'"
            ).fetchone()
        return str(row["report_id"]) if row is not None else None

    def latest_report(self) -> StoredReport | None:
        report_id = self.latest_report_id()
        return self.get_report(report_id) if report_id is not None else None

    def display_status(self, *, latest_run_succeeded: bool) -> str:
        if not latest_run_succeeded and self.latest_report_id() is not None:
            return "STALE_PREVIOUS_REPORT"
        return "CURRENT" if self.latest_report_id() is not None else "NO_REPORT"
