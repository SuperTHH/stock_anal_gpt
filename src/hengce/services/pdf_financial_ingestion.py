from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from hengce.contracts.enums import (
    AcquisitionStatus,
    DocumentKind,
    QualityStatus,
)
from hengce.contracts.financial import FilingDescriptor
from hengce.financials.pdf_extractor import CninfoPdfExtractor
from hengce.financials.sources import is_official_pdf_location
from hengce.raw_store.store import RawObjectStore
from hengce.state.pdf_financial_repository import (
    PdfFinancialDocumentRepository,
)
from hengce.state.pilot_repository import PilotRepository


@dataclass(frozen=True, slots=True)
class PdfFinancialIngestionResult:
    item_id: str
    filing_id: str | None
    ingested: bool
    fact_count: int
    error_code: str | None


class PdfFinancialIngestionService:
    """Validate and persist one manually supplied official PDF, fail closed."""

    def __init__(
        self,
        *,
        raw_store: RawObjectStore,
        repository: PdfFinancialDocumentRepository,
        pilot_repository: PilotRepository,
        extractor: CninfoPdfExtractor,
        clock: Callable[[], datetime],
    ) -> None:
        self.raw_store = raw_store
        self.repository = repository
        self.pilot_repository = pilot_repository
        self.extractor = extractor
        self.clock = clock

    def run(self, item_id: str) -> PdfFinancialIngestionResult:
        item = self.pilot_repository.get_manifest_item(item_id)
        if item is None:
            raise ValueError("ACQUISITION_ITEM_NOT_FOUND")
        if item.status is AcquisitionStatus.INGESTED:
            return PdfFinancialIngestionResult(
                item_id=item.item_id,
                filing_id=None,
                ingested=True,
                fact_count=0,
                error_code=None,
            )
        if (
            item.status is not AcquisitionStatus.DOWNLOADED
            or item.document_kind is not DocumentKind.PERIODIC_REPORT
            or not is_official_pdf_location(item.source_id, item.source_url)
            or item.report_period is None
            or item.report_type is None
            or item.source_url is None
            or item.published_at is None
            or item.collected_at is None
            or item.raw_object_hash is None
            or item.discovery_method is None
        ):
            raise ValueError("PDF_ACQUISITION_ITEM_INVALID")

        descriptor = FilingDescriptor(
            source_id=item.source_id,
            source_url=item.source_url,
            ts_code=item.ts_code,
            exchange="SSE" if item.ts_code.endswith(".SH") else "SZSE",
            report_period=item.report_period,
            report_type=item.report_type,
            published_at=item.published_at,
            collected_at=item.collected_at,
            attachment_name="payload.pdf",
            content_type="application/pdf",
            raw_object_hash=item.raw_object_hash,
            taxonomy_refs=(),
            discovery_method=item.discovery_method,
            instance_entrypoint=None,
        )
        extracted = self.extractor.extract(
            pdf_path=self.raw_store.validate_content_hash(
                item.raw_object_hash
            ),
            descriptor=descriptor,
        )
        if extracted.quality_status is not QualityStatus.VALID:
            error_code = (
                extracted.issues[0]
                if extracted.issues
                else "CNINFO_PDF_UNVERIFIED"
            )
            awaiting = item.model_copy(
                update={
                    "status": AcquisitionStatus.AWAITING_MANUAL,
                    "quality_status": QualityStatus.UNVERIFIED,
                    "error_code": error_code,
                }
            )
            self.pilot_repository.transition(
                item.item_id,
                AcquisitionStatus.DOWNLOADED,
                awaiting,
                self.clock(),
            )
            return PdfFinancialIngestionResult(
                item_id=item.item_id,
                filing_id=None,
                ingested=False,
                fact_count=len(extracted.facts),
                error_code=error_code,
            )

        filing_id = self._filing_id(descriptor, extracted.parser_version)
        document = self.extractor.build_document(
            filing_id=filing_id,
            descriptor=descriptor,
            extracted=extracted,
            supersedes_id=item.supersedes_id,
        )
        self.repository.save(item.report_period, document)
        verified = item.model_copy(
            update={
                "status": AcquisitionStatus.VERIFIED,
                "quality_status": QualityStatus.VALID,
                "error_code": None,
            }
        )
        self.pilot_repository.transition(
            item.item_id,
            AcquisitionStatus.DOWNLOADED,
            verified,
            self.clock(),
        )
        ingested = verified.model_copy(
            update={"status": AcquisitionStatus.INGESTED}
        )
        self.pilot_repository.transition(
            item.item_id,
            AcquisitionStatus.VERIFIED,
            ingested,
            self.clock(),
        )
        return PdfFinancialIngestionResult(
            item_id=item.item_id,
            filing_id=filing_id,
            ingested=True,
            fact_count=len(extracted.facts),
            error_code=None,
        )

    @staticmethod
    def _filing_id(descriptor: FilingDescriptor, parser_version: str) -> str:
        identity = json.dumps(
            {
                "source_id": descriptor.source_id,
                "ts_code": descriptor.ts_code,
                "report_period": descriptor.report_period.isoformat(),
                "report_type": descriptor.report_type.value,
                "raw_object_hash": descriptor.raw_object_hash,
                "parser_version": parser_version,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return f"pdf-filing-{hashlib.sha256(identity).hexdigest()}"


__all__ = [
    "PdfFinancialIngestionResult",
    "PdfFinancialIngestionService",
]
