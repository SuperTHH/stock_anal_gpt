from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from hengce.contracts.enums import (
    AcquisitionStatus,
    DiscoveryMethod,
    DocumentKind,
    QualityStatus,
    ReportType,
)
from hengce.contracts.pilot import AcquisitionManifestItem
from hengce.financials.pdf_extractor import PdfExtractionResult
from hengce.services.financial_resolution import FinancialDocument
from hengce.services.pdf_financial_ingestion import (
    PdfFinancialIngestionService,
)

NOW = datetime(2026, 7, 22, 21, 30, tzinfo=UTC)


def item() -> AcquisitionManifestItem:
    return AcquisitionManifestItem(
        item_id="periodic-1",
        universe_id="universe-1",
        ts_code="600001.SH",
        document_kind=DocumentKind.PERIODIC_REPORT,
        report_type=ReportType.ANNUAL,
        report_period=date(2025, 12, 31),
        source_id="cninfo",
        report_cutoff_at=NOW,
        status=AcquisitionStatus.DOWNLOADED,
        source_url="https://static.cninfo.com.cn/finalpage/report.pdf",
        discovery_method=DiscoveryMethod.MANUAL_IMPORT,
        published_at=datetime(2026, 3, 30, tzinfo=UTC),
        effective_at=None,
        collected_at=datetime(2026, 4, 1, tzinfo=UTC),
        content_hash="a" * 64,
        version="v1",
        supersedes_id=None,
        raw_object_hash="a" * 64,
        quality_status=QualityStatus.UNVERIFIED,
        error_code=None,
        attempt_count=1,
    )


class PilotRepo:
    def __init__(self, current: AcquisitionManifestItem) -> None:
        self.current = current
        self.transitions: list[tuple[AcquisitionStatus, AcquisitionStatus]] = []

    def get_manifest_item(self, item_id: str) -> AcquisitionManifestItem | None:
        return self.current if item_id == self.current.item_id else None

    def transition(
        self,
        item_id: str,
        expected_from: AcquisitionStatus,
        updated: AcquisitionManifestItem,
        observed_at: datetime,
    ) -> AcquisitionManifestItem:
        assert item_id == self.current.item_id
        assert expected_from is self.current.status
        assert observed_at == NOW
        self.transitions.append((self.current.status, updated.status))
        self.current = updated
        return updated


class PdfRepo:
    def __init__(self) -> None:
        self.saved: list[tuple[date, FinancialDocument]] = []

    def save(
        self,
        report_period: date,
        document: FinancialDocument,
    ) -> FinancialDocument:
        self.saved.append((report_period, document))
        return document


class Extractor:
    def extract(self, *, pdf_path: Path, descriptor: object) -> PdfExtractionResult:
        assert pdf_path.name == "payload.bin"
        return PdfExtractionResult(
            candidates=(),
            facts={"total_assets": Decimal("100")},
            quality_status=QualityStatus.VALID,
            issues=(),
            parser_version="test-v1",
            pdf_content_hash="a" * 64,
        )

    def build_document(
        self,
        *,
        filing_id: str,
        descriptor: object,
        extracted: PdfExtractionResult,
        supersedes_id: str | None,
    ) -> FinancialDocument:
        return FinancialDocument(
            filing_id=filing_id,
            ts_code="600001.SH",
            source_id="cninfo",
            source_kind="PDF",
            source_url="https://static.cninfo.com.cn/finalpage/report.pdf",
            published_at=datetime(2026, 3, 30, tzinfo=UTC),
            valid_from=datetime(2026, 4, 1, tzinfo=UTC),
            version="pdf-test-v1",
            supersedes_id=supersedes_id,
            quality_status=extracted.quality_status,
            facts=extracted.facts,
            normalization_metadata={"report_period": "2025-12-31"},
        )


def test_valid_pdf_is_persisted_before_manifest_becomes_ingested(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"%PDF test")
    manifest = PilotRepo(item())
    documents = PdfRepo()
    service = PdfFinancialIngestionService(
        raw_store=SimpleNamespace(validate_content_hash=lambda _hash: payload),
        repository=documents,
        pilot_repository=manifest,
        extractor=Extractor(),
        clock=lambda: NOW,
    )

    result = service.run("periodic-1")

    assert result.ingested
    assert result.error_code is None
    assert len(documents.saved) == 1
    assert manifest.transitions == [
        (AcquisitionStatus.DOWNLOADED, AcquisitionStatus.VERIFIED),
        (AcquisitionStatus.VERIFIED, AcquisitionStatus.INGESTED),
    ]
    assert manifest.current.quality_status is QualityStatus.VALID


def test_unverified_pdf_fails_closed_to_manual_queue(tmp_path: Path) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"%PDF test")
    manifest = PilotRepo(item())
    documents = PdfRepo()
    extractor = Extractor()
    extractor.extract = lambda **_kwargs: PdfExtractionResult(
        candidates=(),
        facts={},
        quality_status=QualityStatus.UNVERIFIED,
        issues=("PDF_REQUIRED_FACTS_MISSING",),
        parser_version="test-v1",
        pdf_content_hash="a" * 64,
    )
    service = PdfFinancialIngestionService(
        raw_store=SimpleNamespace(validate_content_hash=lambda _hash: payload),
        repository=documents,
        pilot_repository=manifest,
        extractor=extractor,
        clock=lambda: NOW,
    )

    result = service.run("periodic-1")

    assert not result.ingested
    assert result.error_code == "PDF_REQUIRED_FACTS_MISSING"
    assert documents.saved == []
    assert manifest.transitions == [
        (AcquisitionStatus.DOWNLOADED, AcquisitionStatus.AWAITING_MANUAL)
    ]
