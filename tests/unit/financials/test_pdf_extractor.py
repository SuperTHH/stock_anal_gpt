import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from hengce.contracts.enums import DiscoveryMethod, QualityStatus, ReportType
from hengce.contracts.financial import FilingDescriptor
from hengce.financials.pdf_extractor import CninfoPdfExtractor

FIXTURE = (
    Path(__file__).parents[2] / "fixtures" / "pdf" / "pilot_extracted_pages.json"
)
PDF_BYTES = b"%PDF-1.7\nFIXTURE DATA - NOT A REAL ISSUER\n%%EOF"


def pages() -> list[dict[str, object]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def descriptor(content_hash: str) -> FilingDescriptor:
    return FilingDescriptor(
        source_id="cninfo",
        source_url="https://static.cninfo.com.cn/finalpage/fixture.pdf",
        ts_code="699998.SH",
        exchange="SSE",
        report_period=date(2025, 12, 31),
        report_type=ReportType.ANNUAL,
        published_at=datetime(2026, 3, 30, 10, tzinfo=UTC),
        collected_at=datetime(2026, 7, 30, 9, tzinfo=UTC),
        attachment_name="fixture.pdf",
        content_type="application/pdf",
        raw_object_hash=content_hash,
        taxonomy_refs=(),
        discovery_method=DiscoveryMethod.FIXTURE,
        instance_entrypoint=None,
    )


def extractor(page_payload: list[dict[str, object]]) -> CninfoPdfExtractor:
    fake_pages = [
        SimpleNamespace(
            page_number=item["page_number"],
            extract_text=lambda text=item["text"]: text,
        )
        for item in page_payload
    ]
    return CninfoPdfExtractor(
        parser_version="cninfo-pdf-pilot-v1",
        reader_factory=lambda _path: SimpleNamespace(pages=fake_pages),
    )


def write_pdf(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "fixture.pdf"
    path.write_bytes(PDF_BYTES)
    return path, hashlib.sha256(PDF_BYTES).hexdigest()


def test_extracts_identity_units_negative_values_pages_and_lineage(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)

    result = extractor(pages()).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID
    assert result.issues == ()
    facts = {candidate.canonical_fact_name: candidate for candidate in result.candidates}
    assert facts["total_assets"].value == 20_000_000
    assert facts["interest_expense"].value == -200_000
    assert facts["total_shares"].value == 100_000_000
    assert facts["total_assets"].page_number == 42
    assert facts["total_assets"].unit_multiplier == 10_000
    assert facts["total_assets"].currency == "CNY"
    assert facts["total_shares"].currency == "SHARES"
    assert all(len(candidate.source_text_hash) == 64 for candidate in result.candidates)
    assert result.pdf_content_hash == content_hash
    assert result.parser_version == "cninfo-pdf-pilot-v1"


def test_labeled_six_digit_a_share_code_matches_descriptor_suffix(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] = page_payload[0]["text"].replace(
        "699998.SH",
        "证券代码：699998",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID


@pytest.mark.parametrize(
    ("mutation", "expected_issue"),
    [
        ("scanned", "PDF_LAYOUT_UNSUPPORTED"),
        ("missing_unit", "PDF_LAYOUT_UNSUPPORTED"),
        ("multiple_codes", "PDF_LAYOUT_UNSUPPORTED"),
        ("multiple_periods", "PDF_LAYOUT_UNSUPPORTED"),
        ("balance_failed", "PDF_BALANCE_EQUATION_FAILED"),
        ("cashflow_failed", "PDF_CASH_FLOW_EQUATION_FAILED"),
        ("duplicate_conflict", "PDF_FACT_CONFLICT"),
    ],
)
def test_unsupported_or_ambiguous_pdf_never_becomes_valid(
    tmp_path: Path,
    mutation: str,
    expected_issue: str,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    if mutation == "scanned":
        for page in page_payload:
            page["text"] = None
    elif mutation == "missing_unit":
        for page in page_payload:
            if isinstance(page["text"], str):
                page["text"] = page["text"].replace("单位：人民币万元", "")
    elif mutation == "multiple_codes":
        page_payload[0]["text"] += "\n另一证券代码 000001.SZ"
    elif mutation == "multiple_periods":
        page_payload[0]["text"] += "\n报告期：2024-12-31"
    elif mutation == "balance_failed":
        page_payload[1]["text"] = page_payload[1]["text"].replace(
            "负债合计 | 800",
            "负债合计 | 900",
        )
    elif mutation == "cashflow_failed":
        page_payload[3]["text"] = page_payload[3]["text"].replace(
            "现金及现金等价物净增加额 | 150",
            "现金及现金等价物净增加额 | 151",
        )
    elif mutation == "duplicate_conflict":
        page_payload[1]["text"] += "\n资产总计 | 2,100"

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.UNVERIFIED
    assert expected_issue in result.issues


def test_content_hash_mismatch_is_rejected_before_text_extraction(
    tmp_path: Path,
) -> None:
    path, _content_hash = write_pdf(tmp_path)

    with pytest.raises(ValueError, match="^PDF_CONTENT_HASH_MISMATCH$"):
        extractor(pages()).extract(
            pdf_path=path,
            descriptor=descriptor("a" * 64),
        )


def test_build_document_preserves_per_fact_pdf_lineage_and_collection_time(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    filing_descriptor = descriptor(content_hash)
    configured = extractor(pages())
    extracted = configured.extract(
        pdf_path=path,
        descriptor=filing_descriptor,
    )

    document = configured.build_document(
        filing_id="pdf-fixture-v1",
        descriptor=filing_descriptor,
        extracted=extracted,
        supersedes_id=None,
    )

    assert document.valid_from == filing_descriptor.collected_at
    assert document.version == f"pdf-{content_hash}:cninfo-pdf-pilot-v1"
    assert document.facts["total_assets"] == 20_000_000
    lineage = document.fact_lineage["total_assets"]
    assert lineage.page_number == 42
    assert lineage.unit_multiplier == "10000"
    assert lineage.currency == "CNY"
    assert lineage.parser_version == "cninfo-pdf-pilot-v1"
    assert lineage.pdf_content_hash == content_hash
