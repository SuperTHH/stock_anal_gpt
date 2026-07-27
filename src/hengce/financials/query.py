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
        if not eligible:
            return FinancialQueryResult((), (), ())
        selected = self._latest_chain_record(eligible)
        if selected is None:
            return FinancialQueryResult(
                (),
                ("FINANCIAL_RESTATEMENT_UNUSABLE",),
                tuple(sorted(record.filing.filing_id for record in eligible)),
            )

        filing_ids = (selected.filing.filing_id,)
        if (
            selected.artifact_status != "PUBLISHED"
            or selected.filing.quality_status in _UNUSABLE_QUALITY_STATUSES
        ):
            if selected.filing.supersedes_id is not None:
                return FinancialQueryResult(
                    (),
                    ("FINANCIAL_RESTATEMENT_UNUSABLE",),
                    filing_ids,
                )
            return FinancialQueryResult((), (), ())

        paths = [str(self._approved_path(selected))]
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
            filing_ids,
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
    def _latest_chain_record(
        records: list[FinancialArtifactRecord],
    ) -> FinancialArtifactRecord | None:
        records_by_id = {record.filing.filing_id: record for record in records}
        if len(records_by_id) != len(records):
            return None

        roots: list[str] = []
        children = {filing_id: [] for filing_id in records_by_id}
        for filing_id, record in records_by_id.items():
            parent_id = record.filing.supersedes_id
            if parent_id is None:
                roots.append(filing_id)
            elif parent_id not in records_by_id:
                return None
            else:
                children[parent_id].append(filing_id)

        if len(roots) != 1 or any(len(child_ids) > 1 for child_ids in children.values()):
            return None

        seen: set[str] = set()
        current_id = roots[0]
        while True:
            if current_id in seen:
                return None
            seen.add(current_id)
            child_ids = children[current_id]
            if not child_ids:
                break
            current_id = child_ids[0]

        if len(seen) != len(records_by_id):
            return None
        return records_by_id[current_id]

    def _approved_path(self, record: FinancialArtifactRecord) -> Path:
        root = self._warehouse_root.resolve()
        path = Path(record.expected_path)
        candidates = (path,) if path.is_absolute() else (path, root / path)
        approved = [
            resolved
            for candidate in candidates
            if (resolved := candidate.resolve()) != root and root in resolved.parents
        ]
        if not approved:
            raise ValueError("FINANCIAL_QUERY_PATH_INVALID")
        return next((candidate for candidate in approved if candidate.is_file()), approved[0])
