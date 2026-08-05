import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
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


def test_extracts_visible_cninfo_whitespace_tables_across_pages(
    tmp_path: Path,
) -> None:
    """Regression for the text layout emitted by real CNINFO quarterly PDFs."""
    path, content_hash = write_pdf(tmp_path)
    filing = descriptor(content_hash).model_copy(
        update={
            "ts_code": "603986.SH",
            "report_period": date(2026, 3, 31),
            "report_type": ReportType.Q1,
        }
    )
    page_payload = [
        {
            "page_number": 1,
            "text": (
                "证券代码：603986 证券简称：示例\n"
                "示例股份有限公司 2026 年第一季度报告\n"
                "一、主要财务数据\n"
                "单位：元 币种：人民币\n"
                "归属于上市公司股东的扣除\n"
                "非经常性损益的净利润 170 160\n"
            ),
        },
        {
            "page_number": 5,
            "text": (
                "合并资产负债表\n"
                "2026 年 3 月 31 日\n"
                "单位：元 币种:人民币\n"
                "货币资金 300 280\n"
                "流动资产合计 1,200 1,100\n"
                "资产总计 2,000 1,900\n"
                "流动负债合计 500 480\n"
            ),
        },
        {
            "page_number": 6,
            "text": (
                "负债合计 800 760\n"
                "有息负债 250 240\n"
                "实收资本（或股本） 100,000,000 100,000,000\n"
                "所有者权益（或股东权益）\n"
                "合计 1,200 1,140\n"
            ),
        },
        {
            "page_number": 8,
            "text": (
                "合并利润表\n"
                "2026 年 1—3 月\n"
                "单位：元 币种:人民币\n"
                "营业收入 1,000 900\n"
                "营业成本 600 550\n"
                "其中：利息费用 20 18\n"
                "五、净利润（净亏损以“-”号\n"
                "填列） 180 170\n"
            ),
        },
        {
            "page_number": 10,
            "text": (
                "合并现金流量表\n"
                "2026 年 1—3 月\n"
                "单位：元 币种：人民币\n"
                "经营活动产生的现金流量\n"
                "净额 220 210\n"
                "购建固定资产、无形资产和其\n"
                "他长期资产支付的现金 50 45\n"
            ),
        },
        {
            "page_number": 12,
            "text": (
                "投资活动产生的现金流量\n"
                "净额 -50 -40\n"
                "筹资活动产生的现金流量\n"
                "净额 -20 -15\n"
                "四、汇率变动对现金及现金等价\n"
                "物的影响 0 0\n"
                "五、现金及现金等价物净增加额 150 155\n"
            ),
        },
    ]

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=filing,
    )

    assert result.quality_status is QualityStatus.VALID
    assert result.issues == ()
    assert result.facts["total_assets"] == Decimal("2000")
    assert result.facts["adjusted_net_profit"] == Decimal("170")
    assert result.facts["total_shares"] == Decimal("100000000")
    assert result.facts["operating_cash_flow"] == Decimal("220")


def test_ignores_parent_company_statements_after_consolidated_statements(
    tmp_path: Path,
) -> None:
    """Catches parent-only rows turning valid consolidated facts into conflicts."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload.extend(
        [
            {
                "page_number": 46,
                "text": (
                    "母公司 资产负债表\n"
                    "单位：人民币万元\n"
                    "资产总计 | 1,900\n"
                    "负债合计 | 750\n"
                    "所有者权益合计 | 1,150\n"
                ),
            },
            {
                "page_number": 47,
                "text": (
                    "母公司 利润表\n"
                    "单位：人民币万元\n"
                    "营业收入 | 900\n"
                    "净利润 | 160\n"
                ),
            },
            {
                "page_number": 48,
                "text": (
                    "母公司 现金流量表\n"
                    "单位：人民币万元\n"
                    "经营活动产生的现金流量净额 | 200\n"
                    "投资活动产生的现金流量净额 | (40)\n"
                    "筹资活动产生的现金流量净额 | (10)\n"
                    "汇率变动对现金及现金等价物的影响 | 0\n"
                    "现金及现金等价物净增加额 | 150\n"
                ),
            },
            {
                "page_number": 49,
                "text": (
                    "相关现金流量已经适当地包括在合并利润表和合并现金流量表中。\n"
                    "单位：人民币万元\n"
                    "货币资金 | 999\n"
                ),
            },
        ]
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID


def test_standalone_narrative_statement_title_does_not_reactivate_extraction(
    tmp_path: Path,
) -> None:
    """A line-wrapped title mention needs a matching statement period."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload.append(
        {
            "page_number": 50,
            "text": (
                "母公司利润表\n"
                "本说明涉及下列项目\n"
                "合并利润表\n"
                "单位：人民币万元\n"
                "营业收入 | 999\n"
                "营业成本 | 888\n"
                "净利润 | 777\n"
            ),
        }
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID
    assert result.issues == ()
    assert result.facts["revenue"] == Decimal("10000000")


def test_statement_period_embedded_in_narrative_does_not_activate_extraction(
    tmp_path: Path,
) -> None:
    """A correct date inside prose is not a structural statement heading."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload.append(
        {
            "page_number": 51,
            "text": (
                "母公司资产负债表\n"
                "合并资产负债表\n"
                "截至2025 年 12 月 31 日，本说明不构成财务报表\n"
                "单位：人民币万元\n"
                "资产总计 | 9,999\n"
                "负债合计 | 8,888\n"
                "所有者权益合计 | 1,111\n"
            ),
        }
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID
    assert result.issues == ()
    assert result.facts["total_assets"] == Decimal("20000000")


def test_labeled_issuer_code_ignores_unlabeled_peer_codes(
    tmp_path: Path,
) -> None:
    """Catches peer tickers in a valid annual report breaking issuer identity."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] = page_payload[0]["text"].replace(
        "699998.SH",
        "证券代码：699998",
    )
    page_payload[0]["text"] += "\n可比公司证券为 688228.SH 和 688432.SH"

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID
    assert result.issues == ()
    assert result.facts["total_assets"] == Decimal("20000000")
    assert result.facts["net_profit"] == Decimal("1800000")
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_cover_issuer_code_ignores_later_labeled_peer_code(
    tmp_path: Path,
) -> None:
    """A peer's labeled ticker in the body cannot override the cover issuer."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] = page_payload[0]["text"].replace(
        "699998.SH",
        "证券代码：699998",
    )
    page_payload[-1]["text"] += "\n上交所科创板证券代码：688981"

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID
    assert result.issues == ()


def test_suffixed_cover_issuer_code_outranks_later_labeled_peer_code(
    tmp_path: Path,
) -> None:
    """A labeled peer in the body cannot replace a suffixed cover issuer."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[-1]["text"] += "\n上交所科创板证券代码：688981"

    correct = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )
    wrong = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash).model_copy(
            update={"ts_code": "688981.SH"}
        ),
    )

    assert correct.quality_status is QualityStatus.VALID
    assert correct.issues == ()
    assert wrong.quality_status is QualityStatus.UNVERIFIED
    assert "PDF_LAYOUT_UNSUPPORTED" in wrong.issues


def test_main_financial_data_stops_before_change_reason_percentages(
    tmp_path: Path,
) -> None:
    """Catches a percentage in the change-reasons table conflicting with profit."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] += (
        "\n主要财务数据\n"
        "单位：人民币万元\n"
        "扣除非经常性损益后的净利润 | 170\n"
        "主要会计数据、财务指标发生变动的情况、原因\n"
        "扣除非经常性损益后的净利润 | 21.83\n"
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID
    assert result.issues == ()
    assert result.facts["adjusted_net_profit"] == Decimal("1700000")


def test_extracts_current_value_after_annual_report_note_column(
    tmp_path: Path,
) -> None:
    """Catches treating an annual-report note number as the reported value."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[1]["text"] = page_payload[1]["text"].replace(
        "货币资金 | 300",
        "货币资金 七、1 300 280",
    )
    page_payload[2]["text"] = page_payload[2]["text"].replace(
        "营业收入 | 1,000",
        "营业收入 七、61 1,000 900",
    )
    page_payload[4]["text"] = page_payload[4]["text"].replace(
        "期末总股本 | 100,000,000",
        "期末总股本 七、53 100,000,000 90,000,000",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID
    assert result.issues == ()
    assert result.facts["cash_and_equivalents"] == Decimal("3000000")
    assert result.facts["revenue"] == Decimal("10000000")
    assert result.facts["total_shares"] == Decimal("100000000")


def test_extracts_adjusted_profit_from_annual_main_accounting_data(
    tmp_path: Path,
) -> None:
    """Catches annual adjusted profit being lost outside the formal statements."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] += (
        "\n（一）主要会计数据\n"
        "单位：人民币万元\n"
        "净利润 | 160\n"
        "扣除非经常性损益后的净利润 | 170\n"
        "（二）主要财务指标\n"
        "扣除非经常性损益后的净利润 | 21.83\n"
    )
    page_payload[2]["text"] = page_payload[2]["text"].replace(
        "扣除非经常性损益后的净利润 | 170\n",
        "",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID
    assert result.issues == ()
    assert result.facts["adjusted_net_profit"] == Decimal("1700000")
    assert result.facts["net_profit"] == Decimal("1800000")


def test_derives_interest_bearing_debt_only_from_complete_visible_components(
    tmp_path: Path,
) -> None:
    """Catches rejecting a filing whose debt total is reproducible from explicit rows."""
    path, content_hash = write_pdf(tmp_path)
    filing = descriptor(content_hash).model_copy(
        update={
            "ts_code": "603986.SH",
            "report_period": date(2026, 3, 31),
            "report_type": ReportType.Q1,
        }
    )
    page_payload = [
        {
            "page_number": 1,
            "text": (
                "证券代码：603986 证券简称：示例\n"
                "示例股份有限公司 2026 年第一季度报告\n"
                "一、主要财务数据\n"
                "单位：元 币种：人民币\n"
                "归属于上市公司股东的扣除非经常性损益的净利润 170\n"
            ),
        },
        {
            "page_number": 5,
            "text": (
                "合并资产负债表\n"
                "2026 年 3 月 31 日\n"
                "单位：元 币种:人民币\n"
                "货币资金 300\n"
                "流动资产合计 1,200\n"
                "资产总计 2,000\n"
                "短期借款 100\n"
                "一年内到期的非流动负债 50\n"
                "长期借款\n"
                "应付债券\n"
                "租赁负债 25\n"
                "流动负债合计 500\n"
                "负债合计 800\n"
                "实收资本（或股本） 100,000,000\n"
                "所有者权益（或股东权益）合计 1,200\n"
            ),
        },
        {
            "page_number": 8,
            "text": (
                "合并利润表\n"
                "2026 年 1—3 月\n"
                "单位：元 币种:人民币\n"
                "营业收入 1,000\n"
                "营业成本 600\n"
                "其中：利息费用 20\n"
                "净利润 180\n"
            ),
        },
        {
            "page_number": 10,
            "text": (
                "合并现金流量表\n"
                "2026 年 1—3 月\n"
                "单位：元 币种：人民币\n"
                "经营活动产生的现金流量净额 220\n"
                "购建固定资产、无形资产和其他长期资产支付的现金 50\n"
                "投资活动产生的现金流量净额 -50\n"
                "筹资活动产生的现金流量净额 -20\n"
                "汇率变动对现金及现金等价物的影响 0\n"
                "现金及现金等价物净增加额 150\n"
            ),
        },
    ]

    configured = extractor(page_payload)
    result = configured.extract(pdf_path=path, descriptor=filing)
    document = configured.build_document(
        filing_id="component-debt-fixture",
        descriptor=filing,
        extracted=result,
        supersedes_id=None,
    )

    assert result.quality_status is QualityStatus.VALID
    assert result.facts["interest_bearing_debt"] == Decimal("175")
    assert document.normalization_metadata[
        "interest_bearing_debt_derivation_version"
    ] == "interest-bearing-debt-components-v1"
    assert document.normalization_metadata[
        "interest_bearing_debt_components"
    ] == (
        "bonds_payable,current_portion_noncurrent_liabilities,"
        "lease_liabilities,long_term_borrowings,short_term_borrowings"
    )


def test_incomplete_debt_components_never_create_an_estimated_total(
    tmp_path: Path,
) -> None:
    """Catches treating an absent debt row as zero when the row is not visible."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[1]["text"] = page_payload[1]["text"].replace(
        "有息负债 | 250",
        (
            "短期借款 | 100\n"
            "一年内到期的非流动负债 | 50\n"
            "长期借款 | 0\n"
            "应付债券 | 0"
        ),
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.UNVERIFIED
    assert "PDF_REQUIRED_FACTS_MISSING" in result.issues
    assert "interest_bearing_debt" not in result.facts


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


def test_zero_based_reader_page_numbers_become_one_based_lineage(
    tmp_path: Path,
) -> None:
    """Catches pypdf's zero-based page index violating the lineage contract."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    for index, page in enumerate(page_payload):
        page["page_number"] = index
    configured = extractor(page_payload)
    filing_descriptor = descriptor(content_hash)

    extracted = configured.extract(
        pdf_path=path,
        descriptor=filing_descriptor,
    )
    document = configured.build_document(
        filing_id="zero-based-pages",
        descriptor=filing_descriptor,
        extracted=extracted,
        supersedes_id=None,
    )

    assert document.fact_lineage["total_assets"].page_number == 2
    assert document.fact_lineage["adjusted_net_profit"].page_number == 3
    assert all(
        lineage.page_number > 0
        for lineage in document.fact_lineage.values()
    )
