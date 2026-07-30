from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hengce.contracts.enums import QualityStatus
from hengce.services.financial_resolution import (
    FinancialDocument,
    PreferredFinancialResolver,
)

NOW = datetime(2026, 7, 29, 12, tzinfo=UTC)


def document(source_kind: str, quality: QualityStatus) -> FinancialDocument:
    return FinancialDocument(
        filing_id=f"{source_kind}-1",
        ts_code="000001.SZ",
        source_id="szse" if source_kind == "XBRL" else "cninfo",
        source_kind=source_kind,
        source_url="https://www.szse.cn/xbrl.xml"
        if source_kind == "XBRL"
        else "https://www.cninfo.com.cn/report.pdf",
        published_at=NOW,
        valid_from=NOW,
        version="v1",
        supersedes_id=None,
        quality_status=quality,
        facts={"total_assets": Decimal("100")},
    )


def test_valid_exchange_xbrl_prevents_pdf_provider_from_being_called() -> None:
    """Catches doing unnecessary CNINFO extraction or letting fallback override XBRL."""

    def forbidden_pdf_provider() -> FinancialDocument:
        raise AssertionError("PDF fallback must not run when XBRL is usable")

    result = PreferredFinancialResolver().resolve(
        exchange_xbrl=document("XBRL", QualityStatus.VALID),
        cninfo_pdf_provider=forbidden_pdf_provider,
        as_of=NOW,
        known_at=NOW,
    )

    assert result.document is not None
    assert result.document.source_kind == "XBRL"
    assert result.blocked_reasons == ()


def test_missing_xbrl_uses_only_validated_cninfo_pdf() -> None:
    """Catches treating the fallback as usable before its rule validation succeeds."""
    valid = PreferredFinancialResolver().resolve(
        exchange_xbrl=None,
        cninfo_pdf_provider=lambda: document("PDF", QualityStatus.VALID),
        as_of=NOW,
        known_at=NOW,
    )
    unverified = PreferredFinancialResolver().resolve(
        exchange_xbrl=None,
        cninfo_pdf_provider=lambda: document("PDF", QualityStatus.UNVERIFIED),
        as_of=NOW,
        known_at=NOW,
    )

    assert valid.document is not None
    assert valid.document.source_kind == "PDF"
    assert unverified.document is None
    assert unverified.blocked_reasons == ("CNINFO_PDF_UNVERIFIED",)


def test_xbrl_pdf_conflict_returns_xbrl_and_quality_block() -> None:
    """Catches silently hiding a material cross-source value conflict."""
    pdf = document("PDF", QualityStatus.VALID)
    pdf = pdf.model_copy(update={"facts": {"total_assets": Decimal("120")}})

    result = PreferredFinancialResolver().resolve(
        exchange_xbrl=document("XBRL", QualityStatus.VALID),
        cninfo_pdf_provider=lambda: pdf,
        as_of=NOW,
        known_at=NOW,
        compare_fallback=True,
    )

    assert result.document is not None
    assert result.document.source_kind == "XBRL"
    assert result.blocked_reasons == ("XBRL_PDF_FACT_CONFLICT",)


def test_pdf_correction_chain_switches_only_when_published_and_known() -> None:
    """Catches replacing an old PDF value before the correction was public and collected."""
    old = document("PDF", QualityStatus.VALID).model_copy(
        update={
            "filing_id": "pdf-old",
            "published_at": NOW,
            "valid_from": NOW + timedelta(days=10),
            "facts": {"total_assets": Decimal("100")},
        }
    )
    correction = old.model_copy(
        update={
            "filing_id": "pdf-correction",
            "published_at": NOW + timedelta(days=5),
            "valid_from": NOW + timedelta(days=12),
            "version": "v2",
            "supersedes_id": "pdf-old",
            "facts": {"total_assets": Decimal("110")},
        }
    )
    def provider() -> tuple[FinancialDocument, FinancialDocument]:
        return old, correction

    resolver = PreferredFinancialResolver()

    before_publication = resolver.resolve(
        exchange_xbrl=None,
        cninfo_pdf_provider=provider,
        as_of=NOW + timedelta(days=4),
        known_at=NOW + timedelta(days=20),
    )
    before_collection = resolver.resolve(
        exchange_xbrl=None,
        cninfo_pdf_provider=provider,
        as_of=NOW + timedelta(days=6),
        known_at=NOW + timedelta(days=11),
    )
    after_both = resolver.resolve(
        exchange_xbrl=None,
        cninfo_pdf_provider=provider,
        as_of=NOW + timedelta(days=6),
        known_at=NOW + timedelta(days=12),
    )

    assert before_publication.document == old
    assert before_collection.document == old
    assert after_both.document == correction
    assert old.facts["total_assets"] == Decimal("100")
    assert correction.supersedes_id == old.filing_id
