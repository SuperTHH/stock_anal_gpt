import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from hengce.contracts.financial import FinancialFact, FinancialFiling

_METADATA_FILING_ID = b"hengce.filing_id"
_METADATA_REPORT_YEAR = b"hengce.report_year"
_METADATA_REPORT_TYPE = b"hengce.report_type"
_METADATA_EXCHANGE = b"hengce.exchange"


@dataclass(frozen=True, slots=True)
class FinancialArtifact:
    path: Path
    content_hash: str
    fact_count: int


class FinancialFactWarehouse:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.dataset = root / "financial_facts"

    def expected_artifact(
        self, filing: FinancialFiling, facts: list[FinancialFact]
    ) -> FinancialArtifact:
        rows = self._canonical_rows(filing.filing_id, facts)
        content_hash = self._content_hash(rows)
        path = (
            self.dataset
            / f"report_year={filing.report_period.year}"
            / f"report_type={filing.report_type.value}"
            / f"exchange={filing.exchange}"
            / f"filing-{filing.filing_id}-{content_hash}.parquet"
        )
        return FinancialArtifact(path, content_hash, len(rows))

    def write_facts(self, filing: FinancialFiling, facts: list[FinancialFact]) -> FinancialArtifact:
        artifact = self.expected_artifact(filing, facts)
        rows = self._canonical_rows(filing.filing_id, facts)
        artifact.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = artifact.path.parent / f".{artifact.content_hash}-{uuid4().hex}.tmp"
        metadata = {
            _METADATA_FILING_ID: filing.filing_id.encode("utf-8"),
            _METADATA_REPORT_YEAR: str(filing.report_period.year).encode("ascii"),
            _METADATA_REPORT_TYPE: filing.report_type.value.encode("utf-8"),
            _METADATA_EXCHANGE: filing.exchange.encode("ascii"),
        }

        try:
            table = pa.Table.from_pylist(self._storage_rows(rows))
            table = table.replace_schema_metadata(metadata)
            pq.write_table(table, temporary, compression="zstd")
            self._flush(temporary)
            self._validate_contents(
                temporary,
                filing_id=filing.filing_id,
                expected_count=artifact.fact_count,
                expected_content_hash=artifact.content_hash,
                expected_report_period=filing.report_period.isoformat(),
                expected_report_year=str(filing.report_period.year),
                expected_report_type=filing.report_type.value,
                expected_exchange=filing.exchange,
            )
            try:
                os.link(temporary, artifact.path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)

        self.validate_artifact(artifact, filing.filing_id)
        return artifact

    def validate_artifact(self, artifact: FinancialArtifact, filing_id: str) -> str:
        try:
            report_year = self._partition_value(
                artifact.path.parent.parent.parent.name, "report_year="
            )
            report_type = self._partition_value(artifact.path.parent.parent.name, "report_type=")
            exchange = self._partition_value(artifact.path.parent.name, "exchange=")
        except ValueError as error:
            raise ValueError("FINANCIAL_PARQUET_INTEGRITY_ERROR") from error

        content_hash = self._validate_contents(
            artifact.path,
            filing_id=filing_id,
            expected_count=artifact.fact_count,
            expected_content_hash=artifact.content_hash,
            expected_report_period=None,
            expected_report_year=report_year,
            expected_report_type=report_type,
            expected_exchange=exchange,
        )
        if artifact.path.name != f"filing-{filing_id}-{content_hash}.parquet":
            raise ValueError("FINANCIAL_PARQUET_INTEGRITY_ERROR")
        return content_hash

    def _validate_contents(
        self,
        path: Path,
        *,
        filing_id: str,
        expected_count: int,
        expected_content_hash: str,
        expected_report_period: str | None,
        expected_report_year: str,
        expected_report_type: str,
        expected_exchange: str,
    ) -> str:
        try:
            with pq.ParquetFile(path) as parquet:
                raw_metadata = parquet.schema_arrow.metadata or {}
                metadata = {
                    key: raw_metadata[key].decode("utf-8")
                    for key in (
                        _METADATA_FILING_ID,
                        _METADATA_REPORT_YEAR,
                        _METADATA_REPORT_TYPE,
                        _METADATA_EXCHANGE,
                    )
                }
                rows = self._logical_rows(parquet.read().to_pylist())
            rows.sort(key=self._canonical_row_key)
            content_hash = self._content_hash(rows)
            report_periods = [date.fromisoformat(str(row["report_period"])) for row in rows]
        except (
            KeyError,
            OSError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            pa.ArrowException,
        ) as error:
            raise ValueError("FINANCIAL_PARQUET_INTEGRITY_ERROR") from error

        if (
            metadata[_METADATA_FILING_ID] != filing_id
            or metadata[_METADATA_REPORT_YEAR] != expected_report_year
            or metadata[_METADATA_REPORT_TYPE] != expected_report_type
            or metadata[_METADATA_EXCHANGE] != expected_exchange
            or any(row.get("filing_id") != filing_id for row in rows)
            or any(str(period.year) != expected_report_year for period in report_periods)
            or expected_report_period is not None
            and any(period.isoformat() != expected_report_period for period in report_periods)
            or any(row.get("report_type") != expected_report_type for row in rows)
            or len(rows) != expected_count
            or content_hash != expected_content_hash
        ):
            raise ValueError("FINANCIAL_PARQUET_INTEGRITY_ERROR")
        return content_hash

    @staticmethod
    def _partition_value(component: str, prefix: str) -> str:
        if not component.startswith(prefix) or component == prefix:
            raise ValueError("invalid financial artifact partition")
        return component.removeprefix(prefix)

    @staticmethod
    def read_artifact(path: Path) -> list[dict[str, object]]:
        try:
            with pq.ParquetFile(path) as parquet:
                rows = FinancialFactWarehouse._logical_rows(parquet.read().to_pylist())
            rows.sort(key=FinancialFactWarehouse._canonical_row_key)
        except (OSError, TypeError, ValueError, pa.ArrowException) as error:
            raise ValueError("FINANCIAL_PARQUET_INTEGRITY_ERROR") from error
        return rows

    @staticmethod
    def _canonical_rows(filing_id: str, facts: list[FinancialFact]) -> list[dict[str, object]]:
        if any(fact.filing_id != filing_id for fact in facts):
            raise ValueError("FINANCIAL_FACTS_MIXED_FILING")
        rows = [fact.model_dump(mode="json") for fact in facts]
        rows.sort(key=FinancialFactWarehouse._canonical_row_key)
        return rows

    @staticmethod
    def _canonical_row_key(row: dict[str, object]) -> tuple[str, str]:
        return str(row["fact_id"]), json.dumps(row, sort_keys=True)

    @staticmethod
    def _content_hash(rows: list[dict[str, object]]) -> str:
        canonical = json.dumps(
            rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _storage_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
        storage_rows: list[dict[str, object]] = []
        for row in rows:
            stored = dict(row)
            stored["dimensions"] = json.dumps(
                row["dimensions"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            storage_rows.append(stored)
        return storage_rows

    @staticmethod
    def _logical_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
        logical_rows: list[dict[str, object]] = []
        for row in rows:
            logical = dict(row)
            logical["dimensions"] = json.loads(str(row["dimensions"]))
            logical_rows.append(logical)
        return logical_rows

    @staticmethod
    def _flush(path: Path) -> None:
        with path.open("r+b") as handle:
            os.fsync(handle.fileno())
