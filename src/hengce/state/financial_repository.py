from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from hengce.contracts.financial import (
    FactConflict,
    FinancialFiling,
    TaxonomyPackageRef,
    require_aware,
)

from .db import connect


def utc_key(value: datetime) -> str:
    require_aware(value, "repository timestamp")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class FinancialArtifactRecord:
    filing: FinancialFiling
    artifact_status: str
    expected_path: str
    expected_hash: str
    expected_count: int
    artifact_published_at: datetime | None


class FinancialFilingRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def register_taxonomy(self, ref: TaxonomyPackageRef) -> None:
        payload_json = ref.model_dump_json()
        with connect(self.path) as connection:
            existing = connection.execute(
                "SELECT payload_json FROM taxonomy_packages WHERE taxonomy_id=?", (ref.taxonomy_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO taxonomy_packages(
                        taxonomy_id, payload_json, raw_object_hash, registered_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        ref.taxonomy_id,
                        payload_json,
                        ref.raw_object_hash,
                        utc_key(datetime.now(UTC)),
                    ),
                )
            elif str(existing["payload_json"]) != payload_json:
                raise ValueError("FINANCIAL_TAXONOMY_CONFLICT")

    def get_taxonomies(self, ids: tuple[str, ...]) -> tuple[TaxonomyPackageRef, ...]:
        with connect(self.path) as connection:
            if not ids:
                return ()
            placeholders = ", ".join("?" for _ in ids)
            rows = connection.execute(
                "SELECT taxonomy_id, payload_json FROM taxonomy_packages "
                f"WHERE taxonomy_id IN ({placeholders})",
                ids,
            ).fetchall()
        found = {str(row["taxonomy_id"]): str(row["payload_json"]) for row in rows}
        if any(taxonomy_id not in found for taxonomy_id in ids):
            raise ValueError("FINANCIAL_TAXONOMY_MISSING")
        return tuple(
            TaxonomyPackageRef.model_validate_json(found[taxonomy_id]) for taxonomy_id in ids
        )

    def stage_filing(
        self,
        filing: FinancialFiling,
        expected_path: str,
        expected_hash: str,
        expected_count: int,
    ) -> FinancialArtifactRecord:
        payload_json = filing.model_dump_json()
        with connect(self.path) as connection:
            existing = connection.execute(
                "SELECT * FROM financial_filings WHERE filing_id=?", (filing.filing_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO financial_filings(
                        filing_id, ts_code, report_period, published_at, valid_from, supersedes_id,
                        payload_json, artifact_status, expected_path, expected_hash, expected_count,
                        artifact_published_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, NULL)
                    """,
                    (
                        filing.filing_id,
                        filing.ts_code,
                        filing.report_period.isoformat(),
                        utc_key(filing.published_at),
                        utc_key(filing.valid_from),
                        filing.supersedes_id,
                        payload_json,
                        expected_path,
                        expected_hash,
                        expected_count,
                    ),
                )
                existing = connection.execute(
                    "SELECT * FROM financial_filings WHERE filing_id=?", (filing.filing_id,)
                ).fetchone()
            elif (
                str(existing["payload_json"]) != payload_json
                or str(existing["expected_path"]) != expected_path
                or str(existing["expected_hash"]) != expected_hash
                or int(existing["expected_count"]) != expected_count
            ):
                raise ValueError("FINANCIAL_FILING_CONFLICT")
        assert existing is not None
        return self._artifact_record(existing)

    def publish_filing(
        self, filing_id: str, path: str, content_hash: str, fact_count: int
    ) -> bool:
        published_at = utc_key(datetime.now(UTC))
        with connect(self.path) as connection:
            cursor = connection.execute(
                """
                UPDATE financial_filings
                SET artifact_status='PUBLISHED', artifact_published_at=?
                WHERE filing_id=? AND artifact_status='PENDING'
                  AND expected_path=? AND expected_hash=? AND expected_count=?
                """,
                (published_at, filing_id, path, content_hash, fact_count),
            )
            if cursor.rowcount == 1:
                return True
            existing = connection.execute(
                "SELECT artifact_status, expected_path, expected_hash, expected_count "
                "FROM financial_filings WHERE filing_id=?",
                (filing_id,),
            ).fetchone()
        return bool(
            existing is not None
            and str(existing["artifact_status"]) == "PUBLISHED"
            and str(existing["expected_path"]) == path
            and str(existing["expected_hash"]) == content_hash
            and int(existing["expected_count"]) == fact_count
        )

    def get_filing(self, filing_id: str) -> FinancialArtifactRecord | None:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT * FROM financial_filings WHERE filing_id=?", (filing_id,)
            ).fetchone()
        return self._artifact_record(row) if row is not None else None

    def list_filing_versions(
        self, ts_code: str, report_period: date
    ) -> list[FinancialArtifactRecord]:
        with connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM financial_filings
                WHERE ts_code=? AND report_period=?
                ORDER BY published_at, valid_from, rowid
                """,
                (ts_code, report_period.isoformat()),
            ).fetchall()
        return [self._artifact_record(row) for row in rows]

    def record_conflicts(self, conflicts: list[FactConflict]) -> None:
        with connect(self.path) as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO financial_fact_conflicts(
                    conflict_id, filing_id, payload_json, detected_at
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        conflict.conflict_id,
                        conflict.filing_id,
                        conflict.model_dump_json(),
                        utc_key(conflict.detected_at),
                    )
                    for conflict in conflicts
                ],
            )

    def list_conflicts(self, filing_id: str) -> list[FactConflict]:
        with connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM financial_fact_conflicts
                WHERE filing_id=? ORDER BY detected_at, conflict_id
                """,
                (filing_id,),
            ).fetchall()
        return [FactConflict.model_validate_json(row["payload_json"]) for row in rows]

    @staticmethod
    def _artifact_record(row: object) -> FinancialArtifactRecord:
        artifact_published_at = row["artifact_published_at"]
        return FinancialArtifactRecord(
            filing=FinancialFiling.model_validate_json(row["payload_json"]),
            artifact_status=str(row["artifact_status"]),
            expected_path=str(row["expected_path"]),
            expected_hash=str(row["expected_hash"]),
            expected_count=int(row["expected_count"]),
            artifact_published_at=(
                datetime.fromisoformat(str(artifact_published_at))
                if artifact_published_at is not None
                else None
            ),
        )
