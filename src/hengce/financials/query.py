from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb

from hengce.contracts.enums import QualityStatus
from hengce.state.financial_repository import (
    FinancialArtifactRecord,
    FinancialFilingRepository,
)

_UNUSABLE_QUALITY_STATUSES = frozenset(
    {
        QualityStatus.CONFLICT,
        QualityStatus.REJECTED,
        QualityStatus.UNVERIFIED,
    }
)


@dataclass(frozen=True)
class FinancialQueryResult:
    facts: tuple[dict[str, object], ...]
    blocked_reasons: tuple[str, ...]
    filing_ids: tuple[str, ...]


class AsOfFinancialQuery:
    def __init__(
        self,
        repository: FinancialFilingRepository,
        warehouse_root: Path,
    ) -> None:
        self._repository = repository
        self._warehouse_root = warehouse_root

    def query_financial_facts(
        self,
        *,
        ts_code: str,
        report_period: date,
        canonical_fact_names: frozenset[str],
        as_of: datetime,
        known_at: datetime,
    ) -> FinancialQueryResult:
        self._validate_cutoffs(as_of, known_at)
        if not canonical_fact_names:
            return FinancialQueryResult((), (), ())

        eligible = [
            record
            for record in self._repository.list_filing_versions(ts_code, report_period)
            if record.filing.published_at is not None
            and record.filing.published_at <= as_of
            and record.filing.valid_from <= known_at
        ]
        selected = self._latest_chain_records(eligible)
        filing_ids = tuple(record.filing.filing_id for record in selected)

        if any(
            record.filing.supersedes_id is not None
            and (
                record.artifact_status != "PUBLISHED"
                or record.filing.quality_status in _UNUSABLE_QUALITY_STATUSES
            )
            for record in selected
        ):
            return FinancialQueryResult(
                (),
                ("FINANCIAL_RESTATEMENT_UNUSABLE",),
                filing_ids,
            )

        usable = [
            record
            for record in selected
            if record.artifact_status == "PUBLISHED"
            and record.filing.quality_status not in _UNUSABLE_QUALITY_STATUSES
        ]
        if not usable:
            return FinancialQueryResult((), (), ())

        paths = [str(self._approved_path(record)) for record in usable]
        names = sorted(canonical_fact_names)
        placeholders = ", ".join("?" for _ in names)
        connection = duckdb.connect()
        try:
            cursor = connection.execute(
                f"""
                SELECT * FROM read_parquet(?)
                WHERE ts_code = ?
                  AND report_period = ?
                  AND canonical_fact_name IN ({placeholders})
                  AND quality_status = 'VALID'
                ORDER BY canonical_fact_name, fact_id
                """,
                [paths, ts_code, report_period.isoformat(), *names],
            )
            columns = [str(column[0]) for column in cursor.description]
            fact_rows: list[dict[str, object]] = []
            for row in cursor.fetchall():
                fact = dict(zip(columns, row, strict=True))
                value = fact["fact_value"]
                if not isinstance(value, Decimal):
                    fact["fact_value"] = Decimal(str(value))
                fact_rows.append(fact)
            facts = tuple(fact_rows)
        finally:
            connection.close()
        return FinancialQueryResult(
            facts,
            (),
            tuple(record.filing.filing_id for record in usable),
        )

    @staticmethod
    def _validate_cutoffs(as_of: datetime, known_at: datetime) -> None:
        if (
            as_of.tzinfo is None
            or as_of.utcoffset() is None
            or known_at.tzinfo is None
            or known_at.utcoffset() is None
        ):
            raise ValueError("FINANCIAL_QUERY_CUTOFF_INVALID")

    @staticmethod
    def _latest_chain_records(
        records: list[FinancialArtifactRecord],
    ) -> list[FinancialArtifactRecord]:
        superseded_ids = {
            record.filing.supersedes_id
            for record in records
            if record.filing.supersedes_id is not None
        }
        return sorted(
            (record for record in records if record.filing.filing_id not in superseded_ids),
            key=lambda record: (
                record.filing.published_at,
                record.filing.valid_from,
                record.filing.filing_id,
            ),
        )

    def _approved_path(self, record: FinancialArtifactRecord) -> Path:
        root = self._warehouse_root.resolve()
        path = Path(record.expected_path)
        resolved = (path if path.is_absolute() else root / path).resolve()
        if resolved == root or root not in resolved.parents:
            raise ValueError("FINANCIAL_QUERY_PATH_INVALID")
        return resolved
