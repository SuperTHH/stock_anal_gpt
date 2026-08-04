from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from hengce.contracts.enums import QualityStatus
from hengce.services.financial_resolution import FinancialDocument

from .db import connect


class PdfFinancialDocumentRepository:
    """Immutable, point-in-time store for validated CNINFO PDF facts."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def save(
        self,
        report_period: date,
        document: FinancialDocument,
    ) -> FinancialDocument:
        validated = FinancialDocument.model_validate(document.model_dump())
        if (
            validated.source_id != "cninfo"
            or validated.source_kind != "PDF"
            or validated.quality_status is not QualityStatus.VALID
            or validated.normalization_metadata.get("report_period")
            != report_period.isoformat()
        ):
            raise ValueError("PDF_FINANCIAL_DOCUMENT_INVALID")
        payload = validated.model_dump_json()
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_json FROM pdf_financial_documents "
                "WHERE filing_id=?",
                (validated.filing_id,),
            ).fetchone()
            if existing is not None:
                stored = FinancialDocument.model_validate_json(
                    str(existing["payload_json"])
                )
                if stored != validated:
                    raise ValueError("PDF_FINANCIAL_VERSION_CONFLICT")
                return stored

            if validated.supersedes_id is not None:
                predecessor = connection.execute(
                    "SELECT ts_code, report_period, published_at, valid_from "
                    "FROM pdf_financial_documents WHERE filing_id=?",
                    (validated.supersedes_id,),
                ).fetchone()
                if predecessor is None:
                    raise ValueError("PDF_FINANCIAL_CHAIN_GAP")
                successor = connection.execute(
                    "SELECT filing_id FROM pdf_financial_documents "
                    "WHERE supersedes_id=?",
                    (validated.supersedes_id,),
                ).fetchone()
                if successor is not None:
                    raise ValueError("PDF_FINANCIAL_BRANCH_CONFLICT")
                if (
                    str(predecessor["ts_code"]) != validated.ts_code
                    or str(predecessor["report_period"])
                    != report_period.isoformat()
                    or datetime.fromisoformat(str(predecessor["published_at"]))
                    >= validated.published_at
                    or datetime.fromisoformat(str(predecessor["valid_from"]))
                    >= validated.valid_from
                ):
                    raise ValueError("PDF_FINANCIAL_CHAIN_CONFLICT")

            connection.execute(
                """
                INSERT INTO pdf_financial_documents(
                    filing_id, ts_code, report_period, published_at,
                    valid_from, supersedes_id, quality_status,
                    payload_json, saved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validated.filing_id,
                    validated.ts_code,
                    report_period.isoformat(),
                    validated.published_at.isoformat(),
                    validated.valid_from.isoformat(),
                    validated.supersedes_id,
                    validated.quality_status.value,
                    payload,
                    datetime.now(UTC).isoformat(),
                ),
            )
        return validated

    def list_versions(
        self,
        ts_code: str,
        report_period: date,
    ) -> tuple[FinancialDocument, ...]:
        with connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM pdf_financial_documents
                WHERE ts_code=? AND report_period=?
                ORDER BY published_at, valid_from, filing_id
                """,
                (ts_code, report_period.isoformat()),
            ).fetchall()
        return tuple(
            FinancialDocument.model_validate_json(str(row["payload_json"]))
            for row in rows
        )

    def visible_documents(
        self,
        ts_code: str,
        report_period: date,
        *,
        as_of: datetime,
        known_at: datetime,
    ) -> tuple[FinancialDocument, ...]:
        self._require_aware(as_of)
        self._require_aware(known_at)
        return tuple(
            document
            for document in self.list_versions(ts_code, report_period)
            if document.published_at <= as_of
            and document.valid_from <= known_at
        )

    @staticmethod
    def _require_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("PDF_FINANCIAL_CUTOFF_INVALID")


__all__ = ["PdfFinancialDocumentRepository"]
