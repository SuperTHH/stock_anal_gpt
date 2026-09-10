import hashlib
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from hengce.contracts.enums import DiscoveryMethod, QualityStatus, ReportType, StatementType
from hengce.contracts.financial import FilingDescriptor
from hengce.financials.pdf_extractor import (
    CninfoPdfExtractor,
    _garbled_bank_statement_candidates,
    _garbled_two_column_cash_flow_candidates,
    _is_financial_institution_report,
    _ocr_result_text,
    _parse_fact_line,
    _pdfium_layout_pages,
    _rapidocr_engine,
    _rapidocr_page_text,
    _statement_period_heading_matches,
)

FIXTURE = Path(__file__).parents[2] / "fixtures" / "pdf" / "pilot_extracted_pages.json"
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


def test_valid_coordinate_text_avoids_legacy_content_stream_parser(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    source = [(page["page_number"], page["text"]) for page in pages()]
    configured = CninfoPdfExtractor(
        parser_version="coordinate-test",
        layout_reader=lambda _path: source,
        reader_factory=lambda _path: pytest.fail("Unnecessary legacy stream parse"),
    )
    result = configured.extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is QualityStatus.VALID
    assert result.pdf_content_hash == content_hash
    assert result.facts["revenue"] == Decimal("10000000")


def test_coordinate_text_cannot_bypass_equation_validation(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    source = [(page["page_number"], page["text"].replace(
        "现金及现金等价物净增加额 | 150", "现金及现金等价物净增加额 | 999",
    )) for page in pages()]
    configured = CninfoPdfExtractor(
        parser_version="coordinate-test",
        layout_reader=lambda _path: source,
        reader_factory=lambda _path: (_ for _ in ()).throw(ValueError("legacy unavailable")),
    )
    result = configured.extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is QualityStatus.UNVERIFIED
    assert "PDF_CASH_FLOW_EQUATION_FAILED" in result.issues


def test_coordinate_reader_failure_keeps_legacy_reader_available(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    configured = extractor(pages())
    configured.layout_reader = lambda _path: (_ for _ in ()).throw(ValueError("bad PDF layout"))
    result = configured.extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is QualityStatus.VALID


def test_coordinate_reader_is_not_called_for_content_hash_mismatch(tmp_path: Path) -> None:
    path, _content_hash = write_pdf(tmp_path)
    configured = CninfoPdfExtractor(
        parser_version="coordinate-test",
        layout_reader=lambda _path: pytest.fail("Unverified bytes reached text extractor"),
    )
    with pytest.raises(ValueError, match="PDF_CONTENT_HASH_MISMATCH"):
        configured.extract(pdf_path=path, descriptor=descriptor("a" * 64))


def test_pdfium_sorts_scrambled_text_and_keeps_empty_physical_pages(tmp_path: Path) -> None:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }),
        }),
    })
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 300 700 Td (100) Tj ET\n"
        b"BT /F1 12 Tf 50 680 Td (Liabilities) Tj ET\n"
        b"BT /F1 12 Tf 50 700 Td (Assets) Tj ET\n"
        b"BT /F1 12 Tf 300 680 Td (60) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_blank_page(width=600, height=800)
    path = tmp_path / "scrambled.pdf"
    writer.write(path)
    original = path.read_bytes()
    assert _pdfium_layout_pages(path) == [(1, "Assets 100\nLiabilities 60"), (2, None)]
    assert path.read_bytes() == original


def test_summary_and_audit_note_before_formal_statements_do_not_end_extraction(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload.insert(
        1,
        {
            "page_number": 20,
            "text": (
                "主要会计数据和财务指标\n"
                "单位：人民币万元\n"
                "扣除非经常性损益后的净利润 | 170\n"
                "财务报表附注"
            ),
        },
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["total_assets"] == Decimal("20000000")
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_layout_text_is_used_when_plain_text_fails_validation(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    fake_pages = []
    for item in pages():
        layout_text = item["text"]
        plain_text = (
            layout_text.replace("合并现金流量表", "母公司现金流量表")
            if item["page_number"] == 44
            else layout_text
        )

        def extract_text(
            extraction_mode: str | None = None,
            *,
            plain_text: str = plain_text,
            layout_text: str = layout_text,
        ) -> str:
            return layout_text if extraction_mode == "layout" else plain_text

        fake_pages.append(
            SimpleNamespace(
                page_number=item["page_number"],
                extract_text=extract_text,
            )
        )
    layout_extractor = CninfoPdfExtractor(
        parser_version="cninfo-pdf-layout-fallback-v1",
        reader_factory=lambda _path: SimpleNamespace(pages=fake_pages),
    )

    result = layout_extractor.extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_layout_fallback_preserves_plain_cash_flow_heading(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    fake_pages = []
    for item in pages():
        plain_text = item["text"]
        layout_text = item["text"]
        if item["page_number"] == 44:
            plain_text = plain_text.replace("| 220", "| 221", 1)
            layout_text = layout_text.removeprefix("合并现金流量表\n")

        def extract_text(
            extraction_mode: str | None = None,
            *,
            plain_text: str = plain_text,
            layout_text: str = layout_text,
        ) -> str:
            return layout_text if extraction_mode == "layout" else plain_text

        fake_pages.append(
            SimpleNamespace(
                page_number=item["page_number"],
                extract_text=extract_text,
            )
        )
    layout_extractor = CninfoPdfExtractor(
        parser_version="cninfo-pdf-layout-heading-v1",
        reader_factory=lambda _path: SimpleNamespace(pages=fake_pages),
    )

    result = layout_extractor.extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_annual_statement_period_accepts_en_dash(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    for page in page_payload:
        page["text"] = page["text"].replace("—", "–")

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_annual_statement_accepts_bare_note_column_header_after_title(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    income_lines = page_payload[2]["text"].splitlines()
    page_payload[2]["text"] = "\n".join(
        (
            income_lines[1],
            "编制单位：示例股份有限公司",
            income_lines[0],
            income_lines[2],
            "项目 附注 2025年度 2024年度",
            *income_lines[3:],
        )
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["revenue"] == Decimal("10000000")
    assert result.facts["net_profit"] == Decimal("1800000")


def test_extracts_financial_tables_flattened_to_single_lines(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    for page in page_payload[1:4]:
        page["text"] = " ".join(page["text"].splitlines())

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["total_assets"] == Decimal("20000000")
    assert result.facts["adjusted_net_profit"] == Decimal("1700000")
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


@pytest.mark.parametrize("bank_marker", [
    "银行资产负债表", "虚构银行股份有限公司\n客户存款\n贷款和垫款",
])
def test_bank_report_does_not_require_industrial_balance_fields(
    tmp_path: Path, bank_marker: str,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] += f"\n{bank_marker}"
    for unsupported in (
        "流动资产合计 | 1,200\n",
        "货币资金 | 300\n",
        "流动负债合计 | 500\n",
        "有息负债 | 250\n",
        "营业成本 | 600\n",
        "利息费用 | (20)",
        "购建固定资产、无形资产和其他长期资产支付的现金 | 50",
    ):
        for page in page_payload:
            page["text"] = page["text"].replace(unsupported, "")

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash).model_copy(update={"issuer_name": "虚构银行"}),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert "current_assets" not in result.facts
    assert "interest_bearing_debt" not in result.facts
    assert "capital_expenditure" not in result.facts


def test_bank_counterparty_name_does_not_change_nonbank_required_fields():
    text = "虚构公司\n合作方：虚构银行股份有限公司\n客户存款\n贷款和垫款"
    assert not _is_financial_institution_report(text, issuer_name="虚构公司")


def test_blank_financial_template_rows_and_insurance_investment_are_not_issuer_type():
    text = (
        "虚构时尚股份有限公司\n2025年度报告\n"
        "吸收存款\n发放贷款和垫款\n保险合同负债\n保险服务收入\n"
        "长期股权投资：中国平安保险（集团）股份有限公司\n"
    )
    assert not _is_financial_institution_report(text, issuer_name="虚构时尚")


@pytest.mark.parametrize("year, valid", [(2025, True), (2024, False)])
def test_balance_period_header_with_inline_unit_and_currency(
    tmp_path: Path, year: int, valid: bool,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    payload = pages()
    payload[1]["text"] = payload[1]["text"].replace(
        "2025 年 12 月 31 日\n单位：人民币万元",
        f"{year}年12月31日 单位：万元 币种：人民币",
    )
    result = extractor(payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    if valid:
        assert result.quality_status is QualityStatus.VALID, result.issues
        assert result.facts["total_assets"] == Decimal("20000000")
    else:
        assert result.quality_status is QualityStatus.UNVERIFIED
        assert "total_assets" not in result.facts


def test_q1_report_does_not_require_undisclosed_interest_expense(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] = page_payload[0]["text"].replace(
        "2025年年度报告",
        "2025年第一季度报告",
    ).replace(
        "报告期：2025-12-31",
        "报告期：2025-03-31",
    )
    page_payload[1]["text"] = page_payload[1]["text"].replace(
        "2025 年 12 月 31 日",
        "2025 年 3 月 31 日",
    )
    for page in page_payload[2:4]:
        page["text"] = page["text"].replace("2025 年 1—12 月", "2025 年 1—3 月")
    page_payload[2]["text"] = page_payload[2]["text"].replace(
        "利息费用 | (20)",
        "",
    )
    filing = descriptor(content_hash).model_copy(
        update={"report_period": date(2025, 3, 31), "report_type": ReportType.Q1}
    )

    result = extractor(page_payload).extract(pdf_path=path, descriptor=filing)

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert "interest_expense" not in result.facts


def test_annual_interest_expense_can_come_from_audited_finance_cost_note(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[2]["text"] = page_payload[2]["text"].replace(
        "利息费用 | (20)",
        "",
    )
    page_payload.append(
        {
            "page_number": 140,
            "text": (
                "财务费用\n"
                "单位：人民币万元\n"
                "项目 本年发生额 上年发生额\n"
                "贷款及应付款项的利息支出 33 22"
            ),
        }
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["interest_expense"] == Decimal("330000")


def test_current_statement_decimal_split_onto_next_line_is_rejoined(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[2]["text"] = page_payload[2]["text"].replace(
        "营业收入 | 1,000",
        "营业收入 1,000.\n50\n900.\n00",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["revenue"] == Decimal("10005000")


@pytest.mark.parametrize(
    "split_label",
    (
        "归属于本行股东的扣除非经常性\n损益的净利润",
        "归属于母公司股东扣除非经常\n性损益的净利润",
        "归属于上市公司股东的扣除非经\n常性损益的净利",
    ),
)
def test_adjusted_profit_summary_accepts_official_split_label_variants(
    tmp_path: Path,
    split_label: str,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] += (
        "\n主要会计数据\n"
        "单位：人民币万元\n"
        f"{split_label}\n170 160 6.25"
    )
    page_payload[0]["text"] = page_payload[0]["text"].replace(
        "扣除非经常性损益后的净利润 | 170\n",
        "",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["adjusted_net_profit"] == Decimal("1700000")


def test_insurer_report_does_not_require_industrial_balance_fields(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    # The issuer's own title, not two generic template rows, establishes its type.
    page_payload[0]["text"] += "\n虚构人寿保险股份有限公司\n保险合同负债\n保险服务收入"
    for unsupported in (
        "流动资产合计 | 1,200\n",
        "货币资金 | 300\n",
        "流动负债合计 | 500\n",
        "有息负债 | 250\n",
        "营业成本 | 600\n",
        "利息费用 | (20)\n",
    ):
        for page in page_payload:
            page["text"] = page["text"].replace(unsupported, "")

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert "current_assets" not in result.facts
    assert "interest_bearing_debt" not in result.facts


def test_combined_company_statement_uses_first_consolidated_columns(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    for page, old, new in (
        (page_payload[1], "合并资产负债表", "合并及公司资产负债表"),
        (page_payload[2], "合并利润表", "合并及公司利润表"),
        (page_payload[3], "合并现金流量表", "合并及公司现金流量表"),
    ):
        page["text"] = page["text"].replace(old, new)
        page["text"] = page["text"].replace(" | ", " ")

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["total_assets"] == Decimal("20000000")


def test_consolidated_and_company_paired_statement_titles_are_supported(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    for page, old, new in (
        (page_payload[1], "合并资产负债表", "合并资产负债表和资产负债表"),
        (page_payload[2], "合并利润表", "合并利润表和利润表"),
        (page_payload[3], "合并现金流量表", "合并现金流量表和现金流量表"),
    ):
        page["text"] = page["text"].replace(old, new)

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_paired_statements_accept_all_amounts_bare_currency_unit(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    for page, old, new in (
        (page_payload[1], "合并资产负债表", "2025年度合并及银行资产负债表"),
        (page_payload[2], "合并利润表", "2025年度合并及银行利润表"),
        (page_payload[3], "合并现金流量表", "2025年度合并及银行现金流量表"),
    ):
        page["text"] = page["text"].replace(old, new).replace(
            "单位：人民币万元",
            "（除另有标明外，所有金额均以人民币千元列示）",
        )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("220000")


def test_combined_bank_statement_uses_first_group_columns(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    for page, old, new in (
        (
            page_payload[1],
            "合并资产负债表",
            "测试银行股份有限公司合并及银行资产负债表",
        ),
        (
            page_payload[2],
            "合并利润表",
            "测试银行股份有限公司合并及银行利润表",
        ),
        (
            page_payload[3],
            "合并现金流量表",
            "测试银行股份有限公司合并及银行现金流量表",
        ),
    ):
        page["text"] = page["text"].replace(old, new)
        page["text"] = page["text"].replace(" | ", " ")
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "汇率变动对现金及现金等价物的影响",
        "汇率变动对现金及现金等价物的影响额",
    ).replace(
        "现金及现金等价物净增加额",
        "现金及现金等价物净变动额",
    )
    for label, values in (
        ("经营活动产生的现金流量净额", "220 210 200 190"),
        ("投资活动产生的现金流量净额", "(50) (40) (30) (20)"),
        ("筹资活动产生的现金流量净额", "(20) (10) (5) (4)"),
        ("汇率变动对现金及现金等价物的影响额", "0 1 0 1"),
        ("现金及现金等价物净变动额", "150 161 165 167"),
    ):
        page_payload[3]["text"] = re.sub(
            rf"{label} (?:[-+\d,()]+)",
            f"{label} {values}",
            page_payload[3]["text"],
        )
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "汇率变动对现金及现金等价物的影响额 0 1 0 1",
        "汇率变动对现金及现金等价物\n的影响额 0 1 0 1",
    ).replace(
        "经营活动产生的现金流量净额 220 210 200 190",
        "经营活动产生的现金流量\n净额 五(43.1) 220 210 200 190",
    ).replace(
        "现金及现金等价物净变动额 150 161 165 167",
        "现金及现金等价物净变动额 五、45 150 161 165 167",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_garbled_bank_statement_titles_are_inferred_from_table_structure(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    for page, old_title in (
        (page_payload[1], "合并资产负债表"),
        (page_payload[2], "合并利润表"),
        (page_payload[3], "合并现金流量表"),
    ):
        page["text"] = page["text"].replace(old_title, "锟斤拷锟斤拷锟斤拷").replace(
            "单位：人民币万元",
            "（除特别注明外，金额单位为人民币万元）\n本集团 本行",
        )
        page["text"] += "\n后附财务报表附注为本财务报表的组成部分。"
        page["text"] = page["text"].replace(" | ", " ")
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "现金及现金等价物净增加额",
        "现金及现金等价物净(减少)/增加",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["total_assets"] == Decimal("20000000")
    assert result.facts["revenue"] == Decimal("10000000")
    assert result.facts["net_cash_change"] == Decimal("1500000")


def test_garbled_bank_total_rows_are_recovered_with_equation_checks() -> None:
    footer = "后附财务报表附注为本财务报表的组成部分。"
    prefix = ["2024年12月31日", "（除特别注明外，金额单位为人民币百万元）"]
    pages_and_expected = (
        (
            [
                *prefix,
                "现金及存放中央银行款项 1 285 280 275 270",
                "发放贷款和垫款 6 400 390 380 370",
                "乱码 1,000 900 800 700",
                footer,
            ],
            {"total_assets": Decimal("1000000000")},
        ),
        (
            [
                *prefix,
                "向中央银行借款 1 100 90 80 70",
                "吸收存款 2 500 490 480 470",
                "乱码 700 650 600 550",
                footer,
            ],
            {"total_liabilities": Decimal("700000000")},
        ),
        (
            [
                *prefix,
                "股本 28 40 40 40 40",
                "少数股东权益 10 9 -- --",
                "乱码 300 250 200 150",
                "乱码 1,000 900 800 700",
                footer,
            ],
            {"equity": Decimal("300000000")},
        ),
        (
            [
                "2024年度",
                prefix[1],
                "附注八 2024 2023 2024 2023",
                "乱码 136 140 126 131",
                "利息收入 251 267 242 258",
                "乱码 32 35 30 35",
                "归属于本行股东的净利润 31 34 29 34",
                footer,
            ],
            {"revenue": Decimal("136000000"), "net_profit": Decimal("32000000")},
        ),
        (
            [
                "2024年度",
                prefix[1],
                "支付利息、手续费及佣金的现金 (125) (140) (119) (134)",
                "支付的各项税费 (14) (19) (13) (16)",
                "乱码 49 (231) 73 (222) 118",
                footer,
            ],
            {"operating_cash_flow": Decimal("-231000000")},
        ),
        (
            [
                "2024年度",
                prefix[1],
                "收回投资收到的现金 1,600 1,370 1,252 1,356",
                "投资支付的现金 (1,703) (1,389) (1,363) (1,413)",
                "购建固定资产、无形资产和其他长期资产",
                "支付的现金 (9) (8) (6) (4)",
                "乱码 42) 41 (53) 3",
                footer,
            ],
            {
                "investing_cash_flow": Decimal("-42000000"),
                "capital_expenditure": Decimal("9000000"),
            },
        ),
        (
            [
                "2024年度",
                prefix[1],
                "发行债券收到的现金 1,383 1,021 1,381 1,016",
                "乱码 1,423 1,021 1,421 1,016",
                "偿还债务支付的现金 (1,127) (992) (1,127) (992)",
                "乱码 (1,201) (1,028) (1,201) (1,028)",
                "乱码 221 (7) 219 (12)",
                "乱码 0 0 0 0",
                "乱码 49 (52) 109 (55) 110",
                "加：年初现金及现金等价物余额 237 128 230 119",
                footer,
            ],
            {
                "financing_cash_flow": Decimal("221000000"),
                "cash_exchange_effect": Decimal("0"),
                "net_cash_change": Decimal("-52000000"),
            },
        ),
    )

    recovered: dict[str, Decimal] = {}
    for page_number, (raw_lines, expected) in enumerate(pages_and_expected, start=1):
        candidates, statement = _garbled_bank_statement_candidates(
            raw_lines,
            page_number=page_number,
        )
        assert statement is not None
        actual = {item.canonical_fact_name: item.value for item in candidates}
        assert actual == expected
        recovered.update(actual)

    assert (
        recovered["operating_cash_flow"]
        + recovered["investing_cash_flow"]
        + recovered["financing_cash_flow"]
        + recovered["cash_exchange_effect"]
        == recovered["net_cash_change"]
    )


def test_garbled_two_column_cash_flow_is_recovered_across_pages() -> None:
    title_page = [
        "2024年度",
        "\u0a40\u0a08\u0456",
        "\u0586\u0eca \u011f \u0ca6\u0af6\u043b\u03e4\u0ea3\u10ed",
        "2024年 2023年",
        "\u1042a \u0a40\u0a08",
        "乱码 1,586 1,339",
        "乱码 1,139) (982)",
        "乱码59(a) 447,023 357,753",
        "a \u0a40\u0a08",
        "乱码 2,169 2,057",
        "乱码 2,462) (2,312)",
        "乱码 292,859) (255,107)",
        "\u0673",
    ]
    continuation_page = [
        "2024年度",
        "2024年 2023年",
        "\u0cd8a \u0a40\u0a08",
        "乱码 281,970 207,613",
        "乱码 279,821) (280,602)",
        "乱码 2,149 (72,989)",
        "乱码 1,195 2,164",
        "乱码59(c) 157,508 31,821",
        "乱码 599,019 567,198",
        "乱码59(b) 756,527 599,019",
        "\u0673",
    ]

    first, statement = _garbled_two_column_cash_flow_candidates(
        title_page,
        page_number=142,
        active_statement_title=None,
    )
    assert statement is not None
    second, continuation = _garbled_two_column_cash_flow_candidates(
        continuation_page,
        page_number=143,
        active_statement_title=statement[0],
    )
    assert continuation is None
    recovered = {
        item.canonical_fact_name: item.value for item in (*first, *second)
    }

    assert recovered == {
        "operating_cash_flow": Decimal("447023000000"),
        "investing_cash_flow": Decimal("-292859000000"),
        "financing_cash_flow": Decimal("2149000000"),
        "cash_exchange_effect": Decimal("1195000000"),
        "net_cash_change": Decimal("157508000000"),
    }
    assert (
        recovered["operating_cash_flow"]
        + recovered["investing_cash_flow"]
        + recovered["financing_cash_flow"]
        + recovered["cash_exchange_effect"]
        == recovered["net_cash_change"]
    )


def test_combined_bank_q1_multirow_group_header_activates_statement(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] = page_payload[0]["text"].replace(
        "2025年年度报告",
        "2025年第一季度报告",
    ).replace(
        "报告期：2025-12-31",
        "报告期：2025-03-31",
    )
    page_payload[1]["text"] = page_payload[1]["text"].replace(
        "合并资产负债表",
        "合并及银行资产负债表（未经审计）",
    ).replace(
        "2025 年 12 月 31 日",
        "2025 年 3 月 31 日",
    )
    for page, old_title, new_title in (
        (page_payload[2], "合并利润表", "合并及银行利润表（未经审计）"),
        (page_payload[3], "合并现金流量表", "合并及银行现金流量表（未经审计）"),
    ):
        page["text"] = page["text"].replace(old_title, new_title).replace(
            "2025 年 1—12 月\n单位：人民币万元",
            (
                "单位：人民币万元\n"
                "项目\n"
                "本集团 本银行\n"
                "2025 年 1-3 月 2024 年 1-3 月 "
                "2025 年 1-3 月 2024 年 1-3 月"
            ),
        )
    filing = descriptor(content_hash).model_copy(
        update={"report_period": date(2025, 3, 31), "report_type": ReportType.Q1}
    )

    result = extractor(page_payload).extract(pdf_path=path, descriptor=filing)

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_audit_qualified_statement_title_and_uniform_currency_unit(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    for page, title in (
        (page_payload[1], "合并资产负债表"),
        (page_payload[2], "合并利润表"),
        (page_payload[3], "合并现金流量表"),
    ):
        page["text"] = page["text"].replace(title, f"未经审计{title}").replace(
            "单位：人民币万元",
            "货币单位均以人民币万元列示",
        )
    page_payload.insert(
        4,
        {
            "page_number": 45,
            "text": (
                "未经审计银行现金流量表\n"
                "货币单位均以人民币万元列示\n"
                "经营活动产生的现金流量净额 999"
            ),
        },
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_plain_qualified_bank_cash_flow_is_treated_as_group_bank_table(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "合并现金流量表",
        "中国光大银行股份有限公司\n未经审计现金流量表",
    ).replace(" | ", " ")
    for label, values in (
        ("经营活动产生的现金流量净额", "220 210 200 190"),
        ("投资活动产生的现金流量净额", "(50) (40) (30) (20)"),
        ("筹资活动产生的现金流量净额", "(20) (10) (5) (4)"),
        ("汇率变动对现金及现金等价物的影响", "0 1 0 1"),
        ("现金及现金等价物净增加额", "150 161 165 167"),
    ):
        page_payload[3]["text"] = re.sub(
            rf"{label} (?:[-+\d,()]+)",
            f"{label} {values}",
            page_payload[3]["text"],
        )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


@pytest.mark.parametrize("title, row", [
    ("资产负债表", "资产总计 9999 8888"),
    ("利润表", "营业收入合计 9999 8888"),
    ("现金流量表", "经营活动产生的现金流量净额 9999 8888"),
])
def test_two_column_parent_bank_table_dates_do_not_imply_group_columns(
    tmp_path: Path, title: str, row: str,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload.insert(4, {
        "page_number": 45,
        "text": "\n".join([
            "测试银行股份有限公司", f"未经审计{title}", "单位：人民币万元",
            "项目 2025年12月31日 2024年12月31日", row,
        ]),
    })

    result = extractor(page_payload).extract(
        pdf_path=path, descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["total_assets"] == Decimal("20000000")
    assert result.facts["revenue"] == Decimal("10000000")
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_side_by_side_balance_table_extracts_trailing_liabilities_and_shares(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[1]["text"] = (
        page_payload[1]["text"]
        .replace("负债合计 | 800", "")
        .replace("实收资本（或股本） | 10,000", "")
        + "\n固定资产 10 负债合计 800 760"
        + "\n商誉 股本 30 10,000 9,000"
    )
    page_payload[4]["text"] = page_payload[4]["text"].replace(
        "期末总股本 | 100,000,000",
        "",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["total_liabilities"] == Decimal("8000000")
    assert result.facts["total_shares"] == Decimal("100000000")
    assert result.facts["revenue"] == Decimal("10000000")


def test_split_blank_cash_exchange_row_is_explicit_zero(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "汇率变动对现金及现金等价物的影响 | 0",
        "汇率变动对现金及现金等价物的\n影响",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["cash_exchange_effect"] == 0


def test_wrapped_cash_exchange_row_waits_for_values_on_following_line(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "汇率变动对现金及现金等价物的影响 | 0",
        "汇率变动对现金及现金等价物的\n影响 10 9",
    ).replace(
        "现金及现金等价物净增加额 | 150",
        "现金及现金等价物净增加额 | 160",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["cash_exchange_effect"] == Decimal("100000")


def test_single_comparative_cash_exchange_value_is_not_current_period(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "汇率变动对现金及现金等价物的影响 | 0",
        "汇率变动对现金及现金等价物的影响 | (10)",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["cash_exchange_effect"] == Decimal(0)


def test_blank_current_cash_flow_with_only_comparative_value_is_zero(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "筹资活动产生的现金流量净额 | (20)",
        "筹资活动产生的现金流量净额  (999)",
    ).replace(
        "现金及现金等价物净增加额 | 150",
        "现金及现金等价物净增加额 | 170",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["financing_cash_flow"] == 0


def test_omitted_cash_exchange_row_is_zero_when_formal_cash_flow_reconciles(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "汇率变动对现金及现金等价物的影响 | 0\n",
        "",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["cash_exchange_effect"] == Decimal(0)


def test_repeated_interest_expense_uses_primary_statement_row(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[2]["text"] += "\n利息费用 | (1)"

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["interest_expense"] == Decimal("-200000")


def test_finance_interest_expense_and_total_revenue_take_priority(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[2]["text"] = page_payload[2]["text"].replace(
        "营业收入 | 1,000",
        "营业总收入 | 1,100\n营业收入 | 1,000",
    ).replace(
        "利息费用 | (20)",
        "利息支出 | 1\n利息费用 | (20)",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["revenue"] == Decimal("11000000")
    assert result.facts["interest_expense"] == Decimal("-200000")


def test_complex_letter_note_reference_precedes_cash_value(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "经营活动产生的现金流量净额 | 220",
        "经营活动产生的现金流量净额 四(65)(h) 220 210",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


@pytest.mark.parametrize(
    "reference",
    [
        "有关进一步详情，请参阅财务报表附注",
        "有关进一步详情，请参阅合并股东权益变动表",
    ],
)
def test_prose_reference_does_not_end_formal_statements(
    tmp_path: Path, reference: str,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[1]["text"] += "\n" + reference
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "投资活动产生", reference + "\n投资活动产生", 1,
    )

    result = extractor(page_payload).extract(
        pdf_path=path, descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["net_profit"] == Decimal("1800000")
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_equity_statement_boundary_prevents_note_fact_conflicts(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload.append(
        {
            "page_number": 5,
            "text": "\n".join(
                [
                    "2025年度合并及公司股东权益变动表",
                    "单位：人民币万元",
                    "合并资产负债表",
                    "2025年12月31日",
                    "短期借款 | 999",
                    "净利润 | 999",
                ]
            ),
        }
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["total_assets"] == Decimal("20000000")
    assert result.facts["net_profit"] == Decimal("1800000")


def test_equity_statement_before_cash_flow_does_not_end_formal_tables(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload.insert(
        3,
        {
            "page_number": 44,
            "text": "\n".join(
                [
                    "2025年度合并及公司股东权益变动表",
                    "单位：人民币万元",
                ]
            ),
        },
    )
    page_payload[4]["text"] = page_payload[4]["text"].replace(
        "单位：人民币万元\n",
        "",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_long_adjusted_profit_label_without_same_page_heading(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] = page_payload[0]["text"].replace(
        "主要会计数据和财务指标\n",
        "",
    )
    page_payload[0]["text"] += (
        "\n归属于上市公司股东的扣除非经常性损益的\n"
        "净利润（万元） 90 80"
    )
    page_payload[2]["text"] = page_payload[2]["text"].replace(
        "扣除非经常性损益后的净利润 | 170\n",
        "",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["adjusted_net_profit"] == Decimal("900000")


def test_statement_headers_accept_named_current_and_prior_columns(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[1]["text"] = page_payload[1]["text"].replace(
        "2025 年 12 月 31 日\n单位：人民币万元",
        (
            "2025年12月31日 编制单位：虚构公司 单位：人民币万元\n"
            "资产 附注五 期末余额 年初余额"
        ),
    )
    for page_index in (2, 3):
        page_payload[page_index]["text"] = page_payload[page_index]["text"].replace(
            "2025 年 1—12 月\n单位：人民币万元",
            (
                "2025年度 编制单位：虚构公司 单位：人民币万元\n"
                "项目 附注五 本期金额 上期金额"
            ),
        )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["total_assets"] == Decimal("20000000")
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_statement_headers_accept_annotated_dated_columns(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[1]["text"] = page_payload[1]["text"].replace(
        "2025 年 12 月 31 日\n单位：人民币万元",
        (
            "2025年12月31日 编制单位：虚构公司 单位：人民币万元\n"
            "项目 附注五 2025年12月31日 2025年1月1日"
        ),
    ).replace(
        "货币资金 | 300",
        "货币资金 注释1 300 250",
    )
    for page_index in (2, 3):
        page_payload[page_index]["text"] = page_payload[page_index]["text"].replace(
            "2025 年 1—12 月\n单位：人民币万元",
            (
                "2025年度 编制单位：虚构公司 单位：人民币万元\n"
                "项目 附注五 2025年度 2024年度"
            ),
        )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["cash_and_equivalents"] == Decimal("3000000")
    assert result.facts["revenue"] == Decimal("10000000")


def test_extracts_consolidated_table_when_pdf_places_visual_title_last(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    for page in page_payload[1:]:
        lines = page["text"].splitlines()
        title = lines.pop(0).removeprefix("合并")
        unit = next(line for line in lines if "单位：" in line)
        lines.remove(unit)
        page["text"] = "\n".join(["项目 合并数 公司数 合并数 公司数", *lines, title, unit])

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["total_assets"] == Decimal("20000000")
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_recovers_q1_summary_and_reordered_capex_row(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    filing = descriptor(content_hash).model_copy(
        update={
            "report_period": date(2025, 3, 31),
            "report_type": ReportType.Q1,
        }
    )
    page_payload = pages()
    page_payload[2]["text"] = (
        "§2 主要财务数据\n"
        "归属于上市公司股东的扣除非经常性损益的净利润（千元）\n"
        "170 160\n"
        "项目 合并数 公司数 合并数 公司数\n"
        "经营活动产生的现金流量净额 220 200\n"
        "1,178 1,000\n"
        "投资支付的现金 500 400\n"
        "投资活动产生的现金流量净额 -50 -40\n"
        "筹资活动产生的现金流量净额 -20 -10\n"
        "汇率变动对现金及现金等价物的影响 0 0\n"
        "现金及现金等价物净增加额 150 150\n"
        "购建固定资产、无形资产和其他长期资产支付的现金\n"
        "现金流量表\n单位：人民币千元\n"
    )

    result = extractor(page_payload).extract(pdf_path=path, descriptor=filing)

    assert result.facts["adjusted_net_profit"] == Decimal("170000")
    assert result.facts["capital_expenditure"] == Decimal("1178000")


def test_accepts_dual_listed_a_h_security_code_label(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    filing = descriptor(content_hash).model_copy(
        update={"ts_code": "000063.SZ", "exchange": "SZSE"}
    )
    page_payload = pages()
    page_payload[0]["text"] = (
        "FIXTURE DATA - NOT A REAL ISSUER\n"
        "证券代码（A/H）：000063/00763\n"
        "2025年年度报告\n报告期：2025-12-31"
    )

    result = extractor(page_payload).extract(pdf_path=path, descriptor=filing)

    assert "PDF_LAYOUT_UNSUPPORTED" not in result.issues


def test_annual_bond_indicator_table_supplies_adjusted_profit(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[2]["text"] = page_payload[2]["text"].replace(
        "扣除非经常性损益后的净利润 | 170", "未支持摘要字段 | 170"
    )
    page_payload.append(
        {
            "page_number": 95,
            "text": (
                "主要指标 2025 年（元） 2024 年（元） 本期比上年同期增减（%）\n"
                "归属于上市公司股东的扣除非经常性损益的净利润 "
                "21,616,538,793 19,531,070,917 10.68"
            ),
        }
    )

    result = extractor(page_payload).extract(pdf_path=path, descriptor=descriptor(content_hash))

    assert result.facts["adjusted_net_profit"] == Decimal("21616538793")


def test_derives_equity_when_a_scanned_continuation_page_is_unreadable(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[1]["text"] = "\n".join(
        line for line in page_payload[1]["text"].splitlines() if "所有者权益合计" not in line
    )

    result = extractor(page_payload).extract(pdf_path=path, descriptor=descriptor(content_hash))

    assert result.facts["equity"] == Decimal("12000000")
    assert ("equity", ("total_assets", "total_liabilities")) in result.derivations


def test_extracts_current_total_from_annual_share_change_table(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[-1]["text"] = (
        "股份变动情况表\n单位：股\n"
        "三、股份总数 26,329,312,240 100 0 0 0 -2,741,000 "
        "-2,741,000 26,326,571,240 100"
    )

    result = extractor(page_payload).extract(pdf_path=path, descriptor=descriptor(content_hash))

    assert result.facts["total_shares"] == Decimal("26326571240")


def test_annual_summary_accepts_cny_millions_and_year_over_year_header(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[2]["text"] = page_payload[2]["text"].replace(
        "\u6263\u9664\u975e\u7ecf\u5e38\u6027\u635f\u76ca\u540e\u7684\u51c0\u5229\u6da6 | 170",
        "\u672a\u652f\u6301\u7684\u6458\u8981\u5b57\u6bb5 | 170",
    )
    page_payload.append(
        {
            "page_number": 10,
            "text": (
                "\u9879\u76ee 2025\u5e74 2024\u5e74 \u540c\u6bd4\u589e\u51cf 2023\u5e74\n"
                "\u5355\u4f4d\uff1a\u4eba\u6c11\u5e01\u767e\u4e07\u5143\n"
                "\u5f52\u5c5e\u4e8e\u4e0a\u5e02\u516c\u53f8\u80a1\u4e1c\u7684\u6263\u9664\u975e\u7ecf\u5e38\u6027"
                "\u635f\u76ca\u7684\u51c0\u5229\u6da6 | 170"
            ),
        }
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["adjusted_net_profit"] == Decimal("170000000")


def test_annual_summary_accepts_ordinary_shareholder_adjusted_profit_label(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    original_label = (
        "\u5f52\u5c5e\u4e8e\u4e0a\u5e02\u516c\u53f8\u80a1\u4e1c\u7684"
        "\u6263\u9664\u975e\u7ecf\u5e38\u6027\u635f\u76ca\u7684\u51c0\u5229\u6da6 | 170"
    )
    ordinary_shareholder_label = (
        "\u5f52\u5c5e\u4e8e\u4e0a\u5e02\u516c\u53f8\u666e\u901a\u80a1\u80a1\u4e1c"
        "\u7684\u6263\u9664\u975e\u7ecf\u5e38\u6027\u635f\u76ca\u7684\u51c0\u5229\u6da6 | 170"
    )
    page_payload[2]["text"] = page_payload[2]["text"].replace(
        original_label,
        ordinary_shareholder_label,
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["adjusted_net_profit"] == Decimal("1700000")


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

    assert result.quality_status is QualityStatus.VALID, result.issues


def test_front_matter_a_share_listing_row_establishes_identity(
    tmp_path: Path,
) -> None:
    """SSE annual reports may put the code only in a listing-information row."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] = page_payload[0]["text"].replace(
        "699998.SH",
        "",
    )
    page_payload[1]["text"] = (
        "\u80a1\u7968\u79cd\u7c7b \u80a1\u7968\u4e0a\u5e02\u4ea4\u6613\u6240 "
        "\u80a1\u7968\u7b80\u79f0 \u80a1\u7968\u4ee3\u7801\n"
        "A\u80a1 \u4e0a\u6d77\u8bc1\u5238\u4ea4\u6613\u6240 \u793a\u4f8b "
        "699998 \u65e0\n"
        f"{page_payload[1]['text']}"
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID
    assert result.issues == ()


def test_front_matter_issuer_name_establishes_identity_when_cover_has_no_code(
    tmp_path: Path,
) -> None:
    """An image-style cover may put the issuer name on a later front-matter page."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] = (
        "2025年年度报告\n"
        "报告期：2025-12-31"
    )
    page_payload.insert(
        1,
        {
            "page_number": 3,
            "text": "江苏苏州农村商业银行股份有限公司\n年度报告释义",
        },
    )
    filing = descriptor(content_hash).model_copy(
        update={"issuer_name": "江苏苏州农村商业银行股份有限公司"}
    )

    result = extractor(page_payload).extract(pdf_path=path, descriptor=filing)

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.issues == ()


def test_ascii_parenthesized_paid_in_capital_maps_to_total_shares(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[4]["text"] = page_payload[4]["text"].replace(
        "期末总股本 | 100,000,000",
        "实收资本(或股本) | 100,000,000",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["total_shares"] == Decimal("100000000")


def test_annual_quarterly_data_table_does_not_conflict_with_annual_cash_flow(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload.insert(
        1,
        {
            "page_number": 9,
            "text": (
                "八、2025年分季度主要财务数据\n"
                "单位：人民币万元\n"
                "经营活动产生的现金流量 7,638 443 -2,861 674\n"
                "净额"
            ),
        },
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_cross_page_recovery_has_lower_priority_than_formal_statement(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload.extend(
        (
            {
                "page_number": 200,
                "text": "经营活动产生的现金流量 999",
            },
            {
                "page_number": 201,
                "text": "净额",
            },
        )
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_later_cash_flow_repeat_does_not_override_formal_statement(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload.insert(
        4,
        {
            "page_number": 45,
            "text": "经营活动产生的现金流量净额 | 999",
        },
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


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


@pytest.mark.parametrize(
    ("report_type", "report_period", "cover_marker", "statement_header"),
    [
        (
            ReportType.ANNUAL,
            date(2025, 12, 31),
            "2025年年度报告",
            "项目 2025年度 2024年度",
        ),
        (
            ReportType.Q1,
            date(2025, 3, 31),
            "2025年第一季度报告",
            "项目 本期发生额 上期发生额",
        ),
    ],
)
def test_income_and_cash_flow_accept_structural_table_header(
    tmp_path: Path,
    report_type: ReportType,
    report_period: date,
    cover_marker: str,
    statement_header: str,
) -> None:
    """SZSE tables may identify the period in columns instead of a title line."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] = (
        "FIXTURE DATA - NOT A REAL ISSUER\n"
        "虚构公司 699998.SH\n"
        f"{cover_marker}\n报告期：{report_period.isoformat()}"
    )
    page_payload[1]["text"] = page_payload[1]["text"].replace(
        "2025 年 12 月 31 日",
        f"{report_period.year} 年 {report_period.month} 月 {report_period.day} 日",
    )
    for index in (2, 3):
        lines = page_payload[index]["text"].splitlines()
        page_payload[index]["text"] = "\n".join([lines[0], lines[2], statement_header, *lines[3:]])

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash).model_copy(
            update={"report_type": report_type, "report_period": report_period}
        ),
    )

    assert result.quality_status is QualityStatus.VALID
    assert result.issues == ()
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_blank_financing_cash_flow_row_is_explicit_zero(
    tmp_path: Path,
) -> None:
    """A visible blank total row is zero, not an absent disclosure."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = (
        page_payload[3]["text"]
        .replace(
            "筹资活动产生的现金流量净额 | (20)",
            "筹资活动产生的现金流量净额",
        )
        .replace(
            "现金及现金等价物净增加额 | 150",
            "现金及现金等价物净增加额 | 170",
        )
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID
    assert result.issues == ()
    assert result.facts["financing_cash_flow"] == Decimal(0)


def test_blank_truncated_cash_exchange_with_prior_value_is_zero(
    tmp_path: Path,
) -> None:
    """A dash before the comparative value is an explicit current-period zero."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "汇率变动对现金及现金等价物的影响 | 0",
        "四、汇率变动对现金及现金等价物的 - 10",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["cash_exchange_effect"] == Decimal(0)


def test_cny_thousand_unit_scales_statement_facts(
    tmp_path: Path,
) -> None:
    """SZSE reports commonly disclose all statement values in CNY thousands."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    for item in page_payload[1:4]:
        item["text"] = item["text"].replace(
            "\u5355\u4f4d\uff1a\u4eba\u6c11\u5e01\u4e07\u5143",
            "\u5355\u4f4d\uff1a\u5343\u5143",
        )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID
    assert result.issues == ()
    assert result.facts["total_assets"] == Decimal("2000000")
    assert result.facts["operating_cash_flow"] == Decimal("220000")


def test_total_shares_keeps_statement_unit_multiplier(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    share_lines = page_payload[4]["text"].splitlines()
    page_payload[4]["text"] = "\n".join(
        [
            share_lines[0],
            "\u5355\u4f4d\uff1a\u5343\u80a1",
            f"{share_lines[2].partition('|')[0]}| 100,000",
        ]
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["total_shares"] == Decimal("100000000")
    candidate = next(
        item for item in result.candidates if item.canonical_fact_name == "total_shares"
    )
    assert candidate.currency == "SHARES"
    assert candidate.unit_multiplier == Decimal("1000")


def test_annual_period_heading_with_inline_cny_thousand_activates_statement(
    tmp_path: Path,
) -> None:
    """Audited annual statements can put `CNY thousands` on the year heading."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    cash_flow_lines = page_payload[3]["text"].splitlines()
    page_payload[3]["text"] = "\n".join(
        [
            cash_flow_lines[0],
            "2025\u5e74\u5ea6 \u4eba\u6c11\u5e01\u5343\u5143",
            *cash_flow_lines[3:],
        ]
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("220000")


def test_balance_date_heading_with_inline_cny_thousand_activates_statement(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    balance_lines = page_payload[1]["text"].splitlines()
    page_payload[1]["text"] = "\n".join(
        [
            balance_lines[0],
            "2025\u5e7412\u670831\u65e5 \u4eba\u6c11\u5e01\u5343\u5143",
            *balance_lines[3:],
        ]
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["total_assets"] == Decimal("2000000")


def test_slash_note_placeholder_cash_flow_rows_are_parsed(
    tmp_path: Path,
) -> None:
    """SSE tables often use `/` as the empty note column before current value."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    cash_flow_lines = page_payload[3]["text"].splitlines()
    for index in range(3, 8):
        label, separator, value = cash_flow_lines[index].partition("|")
        assert separator
        cash_flow_lines[index] = f"{label} / {value.strip()} {value.strip()}"
    page_payload[3]["text"] = "\n".join(cash_flow_lines)

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("2200000")
    assert result.facts["investing_cash_flow"] == Decimal("-500000")


def test_zero_padded_visible_q1_period_identifies_report(
    tmp_path: Path,
) -> None:
    """A visible 03/31 date must identify a Q1 filing without a sidecar period line."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] = (
        "FIXTURE DATA - NOT A REAL ISSUER\n"
        "\u865a\u6784\u516c\u53f8 699998.SH\n"
        "2025 \u5e74\u7b2c\u4e00\u5b63\u5ea6\u62a5\u544a"
    )
    page_payload[1]["text"] = page_payload[1]["text"].replace(
        "2025 \u5e74 12 \u6708 31 \u65e5",
        "2025 \u5e74 03 \u6708 31 \u65e5",
    )
    for index in (2, 3):
        lines = page_payload[index]["text"].splitlines()
        page_payload[index]["text"] = "\n".join(
            [
                lines[0],
                lines[2],
                "\u9879\u76ee \u672c\u671f\u53d1\u751f\u989d \u4e0a\u671f\u53d1\u751f\u989d",
                *lines[3:],
            ]
        )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash).model_copy(
            update={
                "report_type": ReportType.Q1,
                "report_period": date(2025, 3, 31),
            }
        ),
    )

    assert result.quality_status is QualityStatus.VALID
    assert result.issues == ()


def test_balance_sheet_accepts_reporting_date_inside_table_header(
    tmp_path: Path,
) -> None:
    """Some annual balance sheets put the reporting date in the column header."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    lines = page_payload[1]["text"].splitlines()
    page_payload[1]["text"] = "\n".join(
        [
            lines[0],
            lines[2],
            "\u9879\u76ee 2025 \u5e74 12 \u6708 31 \u65e5 2025 \u5e74 1 \u6708 1 \u65e5",
            *lines[3:],
        ]
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID
    assert result.issues == ()
    assert result.facts["total_assets"] == Decimal("20000000")


def test_annual_balance_sheet_accepts_period_end_opening_balance_header(
    tmp_path: Path,
) -> None:
    """Annual reports may label balance columns without repeating the date."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    lines = page_payload[1]["text"].splitlines()
    page_payload[1]["text"] = "\n".join([lines[0], lines[2], "项目 期末余额 期初余额", *lines[3:]])

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.issues == ()
    assert result.facts["total_assets"] == Decimal("20000000")


def test_cash_exchange_effect_accepts_value_before_cross_page_label_suffix(
    tmp_path: Path,
) -> None:
    """A page break may place the final label word after the row's values."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "\u6c47\u7387\u53d8\u52a8\u5bf9\u73b0\u91d1\u53ca"
        "\u73b0\u91d1\u7b49\u4ef7\u7269\u7684\u5f71\u54cd | 0",
        "\u56db\u3001\u6c47\u7387\u53d8\u52a8\u5bf9\u73b0\u91d1"
        "\u53ca\u73b0\u91d1\u7b49\u4ef7\u7269\u7684 0 0",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID
    assert result.issues == ()
    assert result.facts["cash_exchange_effect"] == Decimal(0)


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
                "text": ("母公司 利润表\n单位：人民币万元\n营业收入 | 900\n净利润 | 160\n"),
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


def test_early_labeled_issuer_code_ignores_later_labeled_peer_codes(
    tmp_path: Path,
) -> None:
    """A cover without a ticker may identify the issuer in early front matter."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] = page_payload[0]["text"].replace(
        "699998.SH",
        "\u865a\u6784\u516c\u53f8",
    )
    page_payload.insert(
        1,
        {
            "page_number": 10,
            "text": "A\u80a1\u80a1\u7968\u4ee3\u7801 699998",
        },
    )
    page_payload.append(
        {
            "page_number": 48,
            "text": "\u540c\u884c\u516c\u53f8\u80a1\u7968\u4ee3\u7801\uff1a688981",
        }
    )

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
        descriptor=descriptor(content_hash).model_copy(update={"ts_code": "688981.SH"}),
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


def test_q1_flattened_summary_stops_before_later_change_reason_percentage(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] = page_payload[0]["text"].replace(
        "2025年年度报告",
        "2025年第一季度报告",
    ).replace(
        "报告期：2025-12-31",
        "报告期：2025-03-31",
    )
    page_payload[0]["text"] += (
        "\n主要会计数据\n"
        "单位：人民币万元\n"
        "归属于上市公司股东的扣除非经常性损益的净利润 170 160 6.25"
    )
    page_payload.insert(
        1,
        {
            "page_number": 2,
            "text": (
                "单位：人民币万元\n"
                "主要会计数据、财务指标发生变动的情况、原因\n"
                "归属于上市公司股东的扣除非经常性损益的净利润 31.57"
            ),
        },
    )
    page_payload[2]["text"] = page_payload[2]["text"].replace(
        "2025 年 12 月 31 日",
        "2025 年 3 月 31 日",
    )
    for page in page_payload[3:5]:
        page["text"] = page["text"].replace("2025 年 1—12 月", "2025 年 1—3 月")
    filing = descriptor(content_hash).model_copy(
        update={"report_period": date(2025, 3, 31), "report_type": ReportType.Q1}
    )

    result = extractor(page_payload).extract(pdf_path=path, descriptor=filing)

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["adjusted_net_profit"] == Decimal("1700000")


def test_q1_flattened_summary_ignores_change_reason_continuation_before_heading(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] = page_payload[0]["text"].replace(
        "2025年年度报告",
        "2025年第一季度报告",
    ).replace(
        "报告期：2025-12-31",
        "报告期：2025-03-31",
    )
    page_payload[0]["text"] += (
        "\n主要会计数据\n"
        "单位：人民币万元\n"
        "归属于上市公司股东的扣除非经常性损益的净利润 170 160 6.25"
    )
    page_payload.insert(
        1,
        {
            "page_number": 2,
            "text": (
                "项目名称 变动比例（%） 主要原因\n"
                "归属于上市公司股东的扣除非经常性损益的净利润 31.57\n"
                "（四）本年第一季度主要会计数据环比变动情况\n"
                "单位：人民币万元\n"
                "归属于上市公司股东的扣除非经常性损益的净利润 "
                "170 165 3.03"
            ),
        },
    )
    page_payload[2]["text"] = page_payload[2]["text"].replace(
        "2025 年 12 月 31 日",
        "2025 年 3 月 31 日",
    )
    for page in page_payload[3:5]:
        page["text"] = page["text"].replace("2025 年 1—12 月", "2025 年 1—3 月")
    filing = descriptor(content_hash).model_copy(
        update={"report_period": date(2025, 3, 31), "report_type": ReportType.Q1}
    )

    result = extractor(page_payload).extract(pdf_path=path, descriptor=filing)

    assert result.quality_status is QualityStatus.VALID, result.issues
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


def test_extracts_split_inline_yuan_adjusted_profit_from_szse_main_data(
    tmp_path: Path,
) -> None:
    """SZSE main-data labels may put the inline yuan unit on its own line."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] += (
        "\n五、主要会计数据和财务指标\n"
        "归属于上市公司股东\n"
        "的扣除非经常性损益\n"
        "的净利润（元）\n"
        "1,700,000 1,600,000 6.25%\n"
        "六、主要财务指标\n"
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


def test_split_adjusted_profit_prefers_pending_full_label(
    tmp_path: Path,
) -> None:
    """A final split line reading 'net profit' must retain its label prefix."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] += (
        "\n五、主要会计数据和财务指标\n"
        "归属于上市公司股东的扣除非经常性损益的\n"
        "净利润（元） 1,700,000 1,600,000 6.25%\n"
        "六、主要财务指标\n"
    )
    page_payload[2]["text"] = page_payload[2]["text"].replace(
        "扣除非经常性损益后的净利润 | 170\n",
        "",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.issues == ()
    assert result.facts["adjusted_net_profit"] == Decimal("1700000")


def test_annual_adjusted_profit_label_can_continue_on_next_page(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] += (
        "\n（一）主要会计数据\n"
        "单位：元\n"
        "归属于上市公司股东的扣 1,700,000 1,600,000 6.25 1,500,000\n"
    )
    page_payload.insert(
        1,
        {
            "page_number": 2,
            "text": (
                "某某股份有限公司2025年年度报告\n"
                "7 / 236\n"
                "除非经常性损益的净利润\n"
            ),
        },
    )
    for page in page_payload:
        page["text"] = page["text"].replace(
            "扣除非经常性损益后的净利润 | 170\n",
            "",
        )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["adjusted_net_profit"] == Decimal("1700000")


def test_note_column_fact_label_can_continue_on_next_page(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload.insert(
        2,
        {
            "page_number": 43,
            "text": "公司年度报告\n一年内到期的非流动负 七、43 47 39\n",
        },
    )
    page_payload[3]["text"] = f"债\n{page_payload[3]['text']}"

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["current_portion_noncurrent_liabilities"] == Decimal("470000")


@pytest.mark.parametrize(
    ("inline_unit", "expected"),
    [
        ("\u5343\u5143", Decimal("1700000")),
        ("\u4e07\u5143", Decimal("17000000")),
    ],
)
def test_scales_split_inline_cny_unit_adjusted_profit(
    tmp_path: Path,
    inline_unit: str,
    expected: Decimal,
) -> None:
    """Main-data facts retain the multiplier embedded in a split label."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] += (
        "\n\u4e94\u3001\u4e3b\u8981\u4f1a\u8ba1\u6570\u636e\u548c\u8d22\u52a1\u6307\u6807\n"
        "\u5f52\u5c5e\u4e8e\u4e0a\u5e02\u516c\u53f8\u80a1\u4e1c\n"
        "\u7684\u6263\u9664\u975e\u7ecf\u5e38\u6027\u635f\u76ca\n"
        f"\u7684\u51c0\u5229\u6da6\uff08{inline_unit}\uff09\n"
        "1,700 1,600 6.25%\n"
        "\u516d\u3001\u4e3b\u8981\u8d22\u52a1\u6307\u6807\n"
    )
    page_payload[2]["text"] = page_payload[2]["text"].replace(
        "\u6263\u9664\u975e\u7ecf\u5e38\u6027\u635f\u76ca\u540e\u7684\u51c0\u5229\u6da6 | 170\n",
        "",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID
    assert result.issues == ()
    assert result.facts["adjusted_net_profit"] == expected


def test_split_adjusted_profit_value_can_share_line_with_unit(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] += (
        "\n2.1 主要会计数据及财务指标\n"
        "归属于母公司股东扣除非经常性损益后的净利润\n"
        "（人民币百万元） 30,259 36,692 (17.5)\n"
    )
    page_payload[2]["text"] = page_payload[2]["text"].replace(
        "扣除非经常性损益后的净利润 | 170\n",
        "",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["adjusted_net_profit"] == Decimal("30259000000")


def test_quarterly_breakdown_does_not_conflict_with_annual_main_data(
    tmp_path: Path,
) -> None:
    """Annual reports often follow annual facts with a quarterly breakdown."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] += (
        "\n五、主要会计数据\n"
        "单位：人民币万元\n"
        "扣除非经常性损益后的净利润 | 170\n"
        "六、分季度主要财务指标\n"
        "单位：人民币万元\n"
        "扣除非经常性损益后的净利润 | 20\n"
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


def test_annual_summary_table_can_follow_an_out_of_order_quarterly_heading(
    tmp_path: Path,
) -> None:
    """A PDF reading-order quirk must not hide the annual adjusted profit."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] += (
        "\n\u516d\u3001\u5206\u5b63\u5ea6\u4e3b\u8981\u8d22\u52a1\u6307\u6807\n"
        "\u5355\u4f4d\uff1a\u5143\n"
        "2025\u5e74 2024\u5e74 \u672c\u5e74\u6bd4\u4e0a\u5e74\u589e\u51cf 2023\u5e74\n"
        "\u5f52\u5c5e\u4e8e\u4e0a\u5e02\u516c\u53f8\u80a1\u4e1c\n"
        "\u7684\u6263\u9664\u975e\u7ecf\u5e38\u6027\u635f\u76ca\n"
        "\u7684\u51c0\u5229\u6da6\uff08\u5143\uff09\n"
        "1,700,000 1,600,000 6.25% 1,500,000\n"
        "\u7b2c\u4e00\u5b63\u5ea6 \u7b2c\u4e8c\u5b63\u5ea6 "
        "\u7b2c\u4e09\u5b63\u5ea6 \u7b2c\u56db\u5b63\u5ea6\n"
        "\u5f52\u5c5e\u4e8e\u4e0a\u5e02\u516c\u53f8\u80a1\u4e1c\u7684\u6263\u9664"
        "\u975e\u7ecf\u5e38\u6027\u635f\u76ca\u7684\u51c0\u5229\u6da6 "
        "200,000 300,000 400,000 800,000\n"
    )
    page_payload[2]["text"] = page_payload[2]["text"].replace(
        "\u6263\u9664\u975e\u7ecf\u5e38\u6027\u635f\u76ca\u540e\u7684\u51c0\u5229\u6da6 | 170\n",
        "",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.issues == ()
    assert result.facts["adjusted_net_profit"] == Decimal("1700000")


def test_plain_share_capital_label_is_total_shares_inside_balance_sheet(
    tmp_path: Path,
) -> None:
    """SZSE balance sheets label issued share capital as plain `股本`."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[4]["text"] = page_payload[4]["text"].replace(
        "期末总股本 | 100,000,000",
        "股本 100,000,000 90,000,000",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID
    assert result.issues == ()
    assert result.facts["total_shares"] == Decimal("100000000")


def test_explicit_share_change_total_overrides_accounting_share_capital(
    tmp_path: Path,
) -> None:
    """A reverse-merger issuer's accounting 股本 need not equal listed shares."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[4]["text"] = page_payload[4]["text"].replace(
        "期末总股本 | 100,000,000",
        "股本 328,300,769.56 328,300,769.56",
    )
    page_payload[3]["text"] += (
        "\n三、股份总数 14,442,199,726 100.00% 0 0 0 0 0 "
        "14,442,199,726 100.00%\n"
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID
    assert result.facts["total_shares"] == Decimal("14442199726")


def test_exact_share_count_overrides_rounded_capital_not_rollforward_opening_value(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    payload = pages()
    payload.pop()
    payload[1]["text"] += "\n股本 | 10,000"
    payload[0]["text"] += (
        "\n三、股份总数 105,000,000 100.00% 0 0 0 -4,999,999 "
        "100,000,001 100.00%\n"
    )
    payload[3]["text"] += "\n股本 | 10,500"
    result = extractor(payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["total_shares"] == Decimal("100000001")
    assert not any(
        c.canonical_fact_name == "total_shares" and c.statement_type is StatementType.CASH_FLOW
        for c in result.candidates
    )


def test_later_parent_capex_candidate_does_not_conflict_with_consolidated_value(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload.insert(
        4,
        {
            "page_number": 45,
            "text": (
                "单位：人民币万元\n"
                "购建固定资产、无形资产和其他长期资产支付的现金 | 999"
            ),
        },
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["capital_expenditure"] == Decimal("500000")


def test_split_equity_row_with_value_before_continuation_is_supported(
    tmp_path: Path,
) -> None:
    """A page break may place the equity row continuation after its values."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[1]["text"] = page_payload[1]["text"].replace(
        "\u6240\u6709\u8005\u6743\u76ca\u5408\u8ba1 | 1,200",
        "\u6240\u6709\u8005\u6743\u76ca\uff08\u6216\u80a1\u4e1c\u6743 1,200 1,100",
    )
    page_payload[2]["text"] = "\u76ca\uff09\u5408\u8ba1\n" + page_payload[2]["text"]

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID
    assert result.facts["equity"] == Decimal("12000000")


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
                "短期借款 七、32 - 100\n"
                "一年内到期的非流动负债 50\n"
                "长期借款\n"
                "\u5e94\u4ed8\u503a\u5238 \u4e03\u300146 - -\n"
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

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["interest_bearing_debt"] == Decimal("75")
    assert (
        document.normalization_metadata["interest_bearing_debt_derivation_version"]
        == "interest-bearing-debt-components-v1"
    )
    assert document.normalization_metadata["interest_bearing_debt_components"] == (
        "bonds_payable,current_portion_noncurrent_liabilities,"
        "lease_liabilities,long_term_borrowings,short_term_borrowings"
    )


def test_omitted_debt_component_is_zero_in_complete_reconciled_statement(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[1]["text"] = page_payload[1]["text"].replace(
        "有息负债 | 250",
        (
            "短期借款 | 100\n"
            "一年内到期的非流动负债 | 50\n"
            "非流动负债：\n"
            "长期借款 | 0\n"
            "应付债券 | 0"
        ),
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["lease_liabilities"] == Decimal(0)
    assert result.facts["interest_bearing_debt"] == Decimal("1500000")


def test_visible_but_unparsed_debt_component_is_not_inferred_as_zero(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[1]["text"] = page_payload[1]["text"].replace(
        "有息负债 | 250",
        (
            "短期借款 | 100\n"
            "一年内到期的非流动负债 | 50\n"
            "非流动负债：\n"
            "长期借款 | 0\n"
            "应付债券 | 0\n"
            "租赁负债 | 待核对"
        ),
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.UNVERIFIED
    assert "PDF_REQUIRED_FACTS_MISSING" in result.issues
    assert "lease_liabilities" not in result.facts
    assert "interest_bearing_debt" not in result.facts


def test_slash_blank_debt_rows_are_explicit_zero_components(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    balance_lines = page_payload[1]["text"].splitlines()
    debt_index = next(index for index, line in enumerate(balance_lines) if "| 250" in line)
    balance_lines[debt_index : debt_index + 1] = [
        "短期借款 / - -",
        "一年内到期的非流动负债 | 50",
        "长期借款 / - -",
        "应付债券 / - -",
        "租赁负债 | 25",
    ]
    page_payload[1]["text"] = "\n".join(balance_lines)

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["interest_bearing_debt"] == Decimal("750000")


def test_omitted_bonds_row_is_zero_only_in_complete_reconciled_liability_section(
    tmp_path: Path,
) -> None:
    """Audited statements commonly suppress a zero-valued bonds-payable row."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[1]["text"] = page_payload[1]["text"].replace(
        "有息负债 | 250",
        (
            "短期借款 | 100\n"
            "一年内到期的非流动负债 | 50\n"
            "非流动负债：\n"
            "长期借款 | 75\n"
            "租赁负债 | 25\n"
            "非流动负债合计 | 300"
        ),
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["bonds_payable"] == Decimal(0)
    assert result.facts["interest_bearing_debt"] == Decimal("2500000")


def test_duplicate_fact_without_unit_does_not_reject_complete_statement(
    tmp_path: Path,
) -> None:
    """A unit-less contents excerpt is harmless when the audited table is complete."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    duplicate_income_page = dict(page_payload[2])
    duplicate_income_page["page_number"] = 99
    page_payload.append(duplicate_income_page)
    page_payload[2]["text"] = page_payload[2]["text"].replace(
        "\u5355\u4f4d\uff1a\u4eba\u6c11\u5e01\u4e07\u5143",
        "",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID


def test_repeated_statement_title_preserves_active_table_context(
    tmp_path: Path,
) -> None:
    """Audited reports may repeat a page title before the actual table header."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[1]["text"] = page_payload[1]["text"].replace(
        "合并资产负债表\n2025 年 12 月 31 日\n单位：人民币万元\n",
        (
            "合并资产负债表\n"
            "2025 年 12 月 31 日 人民币万元\n"
            "示例股份有限公司 2025 年年度报告\n"
            "合并资产负债表\n"
            "资产 附注 2025 年 12 月 31 日 2024 年 12 月 31 日\n"
        ),
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["total_assets"] == Decimal("20000000")


def test_period_prefixed_statement_title_carries_inline_unit(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    replacements = (
        (
            1,
            "合并资产负债表\n2025 年 12 月 31 日\n单位：人民币万元\n",
            (
                "2025年12月31日合并资产负债表"
                "（除特别注明外，金额单位均为人民币万元）\n"
                "项目 2025年12月31日 2024年12月31日\n"
            ),
        ),
        (
            2,
            "合并利润表\n2025 年 1—12 月\n单位：人民币万元\n",
            (
                "2025年度合并利润表"
                "（除特别注明外，金额单位均为人民币万元）\n"
                "项目 本期金额 上期金额\n"
            ),
        ),
        (
            3,
            "合并现金流量表\n2025 年 1—12 月\n单位：人民币万元\n",
            (
                "2025年度合并现金流量表"
                "（除特别注明外，金额单位均为人民币万元）\n"
                "项目 本期金额 上期金额\n"
            ),
        ),
    )
    for index, original, replacement in replacements:
        page_payload[index]["text"] = page_payload[index]["text"].replace(
            original,
            replacement,
        )
    page_payload[2]["text"] += (
        "\n2025年度母公司利润表"
        "（除特别注明外，金额单位均为人民币万元）\n"
        "项目 本期金额 上期金额\n"
        "营业收入 999 888\n"
        "净利润 777 666"
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_table_of_contents_end_heading_does_not_disable_later_statements(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] += "\n目录\n合并所有者权益变动表"

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_parenthesized_negative_allows_inner_pdf_spacing(
    tmp_path: Path,
) -> None:
    """Word-authored PDFs often extract a gap immediately before `)`."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "投资活动产生的现金流量净额 | (50)",
        "投资活动产生的现金流量净额 | (50 )",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["investing_cash_flow"] == Decimal("-500000")


def test_parenthesized_capital_expenditure_is_normalized_as_cash_paid_magnitude(
    tmp_path: Path,
) -> None:
    """Some audited cash-flow layouts parenthesize every cash-outflow row."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "购建固定资产、无形资产和其他长期资产支付的现金 | 50",
        "购建固定资产、无形资产和其他长期资产支付的现金 | (50)",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["capital_expenditure"] == Decimal("500000")


def test_capital_expenditure_accepts_cash_paid_wording(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "购建固定资产、无形资产和其他长期资产支付的现金 | 50",
        "购建固定资产、无形资产和其他长期资产所支付的现金 | 50",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["capital_expenditure"] == Decimal("500000")


def test_cash_exchange_effect_accepts_cash_flow_net_wording(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "汇率变动对现金及现金等价物的影响 | 0",
        "汇率变动对现金流量净额 | 0",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["cash_exchange_effect"] == Decimal(0)


def test_chinese_parenthesized_note_references_and_wrapped_labels(
    tmp_path: Path,
) -> None:
    """SSE annual statements use note references such as `七(79)(1)`."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[1]["text"] = page_payload[1]["text"].replace(
        "货币资金 | 300",
        "货币资金 七(1) 300 200",
    )
    page_payload[2]["text"] = page_payload[2]["text"].replace(
        "营业成本 | 600",
        "营业成本 七(61) 600 500",
    )
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "经营活动产生的现金流量净额 | 220",
        "经营活动产生的现金流\n量净额 七(79)(1) 220 200",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["cash_and_equivalents"] == Decimal("3000000")
    assert result.facts["operating_cost"] == Decimal("6000000")
    assert result.facts["operating_cash_flow"] == Decimal("2200000")
    assert "PDF_LAYOUT_UNSUPPORTED" not in result.issues
    assert result.facts["revenue"] == Decimal("10000000")


@pytest.mark.parametrize(
    ("mutation", "expected_issue"),
    [
        ("scanned", "PDF_IMAGE_ONLY"),
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


def test_image_only_statement_block_between_audit_report_and_notes_is_explicit(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    cover = pages()[0]
    page_payload: list[dict[str, object]] = [
        cover,
        {"page_number": 140, "text": "审计报告（续）\n注册会计师签字"},
        *(
            {"page_number": page_number, "text": None}
            for page_number in range(141, 146)
        ),
        {
            "page_number": 146,
            "text": "财务报表附注\n2025年度 人民币万元\n一、基本情况",
        },
    ]

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.UNVERIFIED
    assert result.issues == ("PDF_IMAGE_ONLY",)


def test_image_only_statement_pages_use_ocr_text_without_bypassing_validation(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    source_pages = pages()
    page_payload: list[dict[str, object]] = [
        source_pages[0],
        {"page_number": 140, "text": "审计报告（续）\n注册会计师签字"},
        *(
            {"page_number": page_number, "text": None}
            for page_number in range(141, 145)
        ),
        {
            "page_number": 145,
            "text": "财务报表附注\n2025年度 人民币万元\n一、基本情况",
        },
    ]
    ocr_text = {
        page_number: source_pages[index]["text"]
        for index, page_number in enumerate(range(141, 145), start=1)
    }
    fake_pages = [
        SimpleNamespace(
            page_number=item["page_number"],
            extract_text=lambda text=item["text"]: text,
        )
        for item in page_payload
    ]
    extractor_with_ocr = CninfoPdfExtractor(
        parser_version="cninfo-pdf-pilot-v1",
        reader_factory=lambda _path: SimpleNamespace(pages=fake_pages),
        ocr_page_text=lambda _page, page_number: ocr_text.get(page_number),
        ocr_cache_root=tmp_path / "ocr-cache",
    )

    result = extractor_with_ocr.extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["total_assets"] == Decimal("20000000")
    assert result.facts["net_cash_change"] == Decimal("1500000")

    cached = CninfoPdfExtractor(
        parser_version="cninfo-pdf-pilot-v1",
        reader_factory=lambda _path: SimpleNamespace(pages=fake_pages),
        ocr_page_text=lambda _page, _page_number: pytest.fail("OCR cache was ignored"),
        ocr_cache_root=tmp_path / "ocr-cache",
    ).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert cached.quality_status is QualityStatus.VALID


def test_ocr_rows_are_sorted_top_to_bottom_and_left_to_right() -> None:
    result = [
        ([[100, 20], [150, 20], [150, 30], [100, 30]], "200", 0.99),
        ([[0, 20], [80, 20], [80, 30], [0, 30]], "资产总计", 0.99),
        ([[0, 0], [80, 0], [80, 10], [0, 10]], "合并资产负债表", 0.99),
    ]

    assert _ocr_result_text(result) == "合并资产负债表\n资产总计 200"


def test_scan_ocr_suppresses_red_seal_without_mutating_source(monkeypatch) -> None:
    from PIL import Image

    scan = Image.new("RGB", (2, 1))
    scan.putdata([(240, 80, 90), (40, 40, 40)])
    original = scan.tobytes()
    received = []

    def engine(image):
        received.append(image)
        return [([[0, 0], [20, 0], [20, 10], [0, 10]], "98,079,980", 0.99)], 0

    monkeypatch.setattr("hengce.financials.pdf_extractor._rapidocr_engine", lambda: engine)
    page = SimpleNamespace(images=[SimpleNamespace(image=scan)], get=lambda *_args: 0)
    assert _rapidocr_page_text(page, 1) == "98,079,980"
    assert received[0].convert("RGB").getpixel((0, 0)) == (240, 240, 240)
    assert received[0].convert("RGB").getpixel((1, 0)) == (40, 40, 40)
    assert scan.tobytes() == original


def test_ocr_engine_bounds_worker_threads(monkeypatch) -> None:
    received = []
    monkeypatch.setattr(
        "rapidocr_onnxruntime.RapidOCR", lambda **kwargs: received.append(kwargs) or object(),
    )
    _rapidocr_engine.cache_clear()
    try:
        assert _rapidocr_engine() is _rapidocr_engine()
        assert received == [{"intra_op_num_threads": 1, "inter_op_num_threads": 1}]
    finally:
        _rapidocr_engine.cache_clear()


def test_ocr_renders_page_content_outside_embedded_images(monkeypatch) -> None:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=400)
    stream = DecodedStreamObject()
    stream.set_data(b"0 0 0 rg 20 350 180 20 re f 20 50 240 250 re f")
    page[NameObject("/Contents")] = writer._add_object(stream)
    received = []

    def engine(image):
        received.append(image)
        return [[[ [0, 0], [20, 0], [20, 10], [0, 10] ], "合并现金流量表", 0.99]], 0

    monkeypatch.setattr("hengce.financials.pdf_extractor._rapidocr_engine", lambda: engine)
    assert _rapidocr_page_text(page, 1) == "合并现金流量表"
    assert received[0].getpixel((60, 80)) == (0, 0, 0)
    assert received[0].getpixel((60, 400)) == (0, 0, 0)
    assert received[0].getpixel((0, 0)) == (255, 255, 255)


@pytest.mark.parametrize("inflow, outflow, valid", [
    ("30", "80", True), ("30", "81", False), ("80", "30", False),
])
def test_damaged_cash_parenthesis_requires_independent_subtotal_agreement(
    tmp_path: Path, inflow: str, outflow: str, valid: bool,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "投资活动产生的现金流量净额 | (50)",
        f"投资活动现金流入小计 {inflow} 40\n"
        f"投资活动现金流出小计 {outflow} 60\n"
        "投资活动使用的现金流量净额 50) 20)",
    )
    result = extractor(page_payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert (result.quality_status is QualityStatus.VALID) is valid
    if valid:
        assert result.facts["investing_cash_flow"] == Decimal("-500000")
        assert "investing_cash_flow" in dict(result.derivations)
        document = extractor(page_payload).build_document(
            filing_id="subtotal-fixture", descriptor=descriptor(content_hash),
            extracted=result, supersedes_id=None,
        )
        assert document.normalization_metadata["investing_cash_flow_derivation_version"] == (
            "cash-subtotals-v1"
        )
    else:
        assert "PDF_CASH_FLOW_EQUATION_FAILED" in result.issues
        assert "investing_cash_flow" not in result.facts


@pytest.mark.parametrize("interruption", [
    "单位：人民币元", "母公司现金流量表", "合并现金流量表",
    "投资活动现金流入小计 31 40",
])
def test_cash_subtotal_recovery_rejects_changed_scope_or_ambiguous_inputs(
    tmp_path: Path, interruption: str,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "投资活动产生的现金流量净额 | (50)",
        "投资活动现金流入小计 30 40\n投资活动现金流出小计 80 60\n"
        + interruption + "\n投资活动使用的现金流量净额 50) 20)",
    )
    result = extractor(page_payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is not QualityStatus.VALID
    assert "investing_cash_flow" not in result.facts


@pytest.mark.parametrize("year, share_unit, expected", [
    (2025, "股", "100000004"), (2024, "股", "100000000"),
    (2025, "万元", "100000000"),
])
def test_late_share_change_table_requires_current_report_and_share_unit(
    tmp_path: Path, year: int, share_unit: str, expected: str,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload.append({"page_number": 127, "text": "\n".join([
        f"{year}年年度报告", "股份变动情况表", "截至报告期末，公司股本结构发生如下变化：",
        f"单位：{share_unit}", "本次变动前 本次变动后", "数量 比例(%) 数量 比例(%)",
        "三、ﾠ股份总数 110,000,004 100 -10,000,000 -10,000,000 100,000,004 100",
    ])})
    result = extractor(page_payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["total_shares"] == Decimal(expected)


def test_ocr_truncated_grouped_amount_is_not_an_accepted_fact() -> None:
    assert _parse_fact_line("短期借款 98 079,980 100,674,419") is None
    assert _parse_fact_line("短期借款 28 98,079,980 100,674,419") == (
        "short_term_borrowings", Decimal("98079980"),
    )
    assert _parse_fact_line("利息费用 | 0.01") == ("interest_expense", Decimal("0.01"))


def test_empty_layout_mode_never_reclassifies_garbled_text_as_image_only(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    source_pages = pages()
    source_pages[2]["text"] = source_pages[2]["text"].replace(
        "利息费用 | (20)",
        "",
    )
    source_pages[3]["text"] = source_pages[3]["text"].replace(
        "现金及现金等价物净增加额 | 150",
        "现金及现金等价物净增加额 | 151",
    )
    page_payload = [
        source_pages[0],
        {"page_number": 140, "text": "审计报告（续）\n注册会计师签字"},
        *(
            {
                "page_number": 141 + index,
                "text": f"现金流量表\n{item['text']}",
            }
            for index, item in enumerate(source_pages[1:])
        ),
        {"page_number": 144, "text": "现金流量表（续）"},
        {"page_number": 145, "text": "财务报表附注\n一、基本情况"},
    ]

    class _ModePage:
        def __init__(self, item: dict[str, object]) -> None:
            self.page_number = item["page_number"]
            self.text = item["text"]

        def extract_text(self, extraction_mode: str | None = None) -> str | None:
            if extraction_mode == "layout" and 141 <= int(self.page_number) <= 144:
                return None
            return str(self.text) if self.text is not None else None

    reader = SimpleNamespace(pages=[_ModePage(item) for item in page_payload])
    result = CninfoPdfExtractor(
        parser_version="cninfo-pdf-pilot-v1",
        reader_factory=lambda _path: reader,
    ).extract(pdf_path=path, descriptor=descriptor(content_hash))

    assert "PDF_IMAGE_ONLY" not in result.issues
    assert "PDF_REQUIRED_FACTS_MISSING" in result.issues
    assert "PDF_CASH_FLOW_EQUATION_FAILED" in result.issues


def test_fully_scanned_annual_report_probes_front_then_financial_tail(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    source_pages = pages()
    fake_pages = [
        SimpleNamespace(page_number=number, extract_text=lambda: None)
        for number in range(1, 101)
    ]
    ocr_text = {
        1: source_pages[0]["text"],
        41: source_pages[1]["text"],
        42: source_pages[2]["text"],
        43: source_pages[3]["text"],
        44: source_pages[4]["text"],
    }
    calls: list[int] = []

    def ocr_page(_page: object, page_number: int) -> str | None:
        calls.append(page_number)
        return ocr_text.get(page_number)

    result = CninfoPdfExtractor(
        parser_version="cninfo-pdf-pilot-v1",
        reader_factory=lambda _path: SimpleNamespace(pages=fake_pages),
        ocr_page_text=ocr_page,
    ).extract(pdf_path=path, descriptor=descriptor(content_hash))

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert 41 in calls
    assert calls.index(41) < calls.index(13)


def test_ocr_does_not_hide_conflict_on_later_page(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    source = [item["text"] for item in pages()]
    source.append(source[1].replace("资产总计 | 2,000", "资产总计 | 2,100"))
    fake_pages = [SimpleNamespace(extract_text=lambda: None) for _ in source]
    result = CninfoPdfExtractor(
        parser_version="ocr-complete-check",
        reader_factory=lambda _path: SimpleNamespace(pages=fake_pages),
        ocr_page_text=lambda _page, number: source[number - 1],
    ).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is not QualityStatus.VALID
    assert "PDF_FACT_CONFLICT" in result.issues


@pytest.mark.parametrize(
    "statement_header",
    (
        "项目 本期发生额 上年同期发生额",
        "项目 本期发生额 上期发生额（调整后）",
        "项目 本期金额 上期金额",
    ),
)
def test_q1_title_and_generic_statement_headers_identify_implicit_period(
    tmp_path: Path,
    statement_header: str,
) -> None:
    """Some official Q1 statements omit the date but retain structural headers."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] = (
        "FIXTURE DATA - NOT A REAL ISSUER\n"
        "\u8bc1\u5238\u4ee3\u7801\uff1a699998\n"
        "2025\u5e74\u7b2c\u4e00\u5b63\u5ea6\u62a5\u544a"
    )
    page_payload[1]["text"] = (
        "\u5408\u5e76\u8d44\u4ea7\u8d1f\u503a\u8868\uff08\u672a\u7ecf\u5ba1\u8ba1\uff09\n"
        "\u7f16\u5236\u5355\u4f4d\uff1a\u865a\u6784\u516c\u53f8\n"
        "\u5355\u4f4d\uff1a\u4eba\u6c11\u5e01\u4e07\u5143\n"
        "\u9879\u76ee \u671f\u672b\u4f59\u989d \u671f\u521d\u4f59\u989d\n"
        + "\n".join(page_payload[1]["text"].splitlines()[3:])
    )
    for index in (2, 3):
        lines = page_payload[index]["text"].splitlines()
        page_payload[index]["text"] = "\n".join(
            [
                f"{lines[0]}\uff08\u672a\u7ecf\u5ba1\u8ba1\uff09",
                "\u5355\u4f4d\uff1a\u4eba\u6c11\u5e01\u4e07\u5143",
                statement_header,
                *lines[3:],
            ]
        )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash).model_copy(
            update={
                "report_type": ReportType.Q1,
                "report_period": date(2025, 3, 31),
            }
        ),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.issues == ()
    assert result.facts["total_assets"] == Decimal("20000000")


def test_cover_accepts_issuer_alongside_second_share_class_code(
    tmp_path: Path,
) -> None:
    """An issuer's B-share code must not invalidate its A-share filing."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] = page_payload[0]["text"].replace(
        "\u865a\u6784\u516c\u53f8 699998.SH",
        ("\u8bc1\u5238\u4ee3\u7801\uff1a699998\n\u8bc1\u5238\u4ee3\u7801\uff1a999998"),
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.issues == ()


def test_front_matter_accepts_issuer_alongside_second_share_class_code(
    tmp_path: Path,
) -> None:
    """Dual share-class codes may appear after a code-free report cover."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] = page_payload[0]["text"].replace(
        "\u865a\u6784\u516c\u53f8 699998.SH",
        "\u865a\u6784\u516c\u53f8",
    )
    page_payload.insert(
        1,
        {
            "page_number": 2,
            "text": ("\u8bc1\u5238\u4ee3\u7801\uff1a699998\n\u8bc1\u5238\u4ee3\u7801\uff1a999998"),
        },
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.issues == ()


def test_front_matter_accepts_preferred_share_and_convertible_bond_codes(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] = page_payload[0]["text"].replace(
        "虚构公司 699998.SH",
        "虚构公司",
    )
    page_payload.insert(
        1,
        {
            "page_number": 12,
            "text": (
                "普通股股票代码 699998\n"
                "优先股股票代码 360018\n"
                "可转换公司债券证券代码 113052"
            ),
        },
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.issues == ()


def test_foreign_suffixed_code_does_not_override_front_matter_issuer(
    tmp_path: Path,
) -> None:
    """A supplier's explicitly foreign ticker is not an A-share issuer code."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] = page_payload[0]["text"].replace(
        "\u865a\u6784\u516c\u53f8 699998.SH",
        "\u865a\u6784\u516c\u53f8",
    )
    page_payload.insert(
        1,
        {
            "page_number": 8,
            "text": "\u4e3b\u8981\u4f9b\u5e94\u5546\u80a1\u7968\u4ee3\u7801 005930.KS",
        },
    )
    page_payload.insert(
        2,
        {
            "page_number": 11,
            "text": "\u80a1\u7968\u7b80\u79f0 \u865a\u6784 \u80a1\u7968\u4ee3\u7801 699998",
        },
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.issues == ()


def test_annual_heading_with_inline_unit_activates_statements(
    tmp_path: Path,
) -> None:
    """Audited reports may combine the annual period and CNY unit on one line."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    for index in (2, 3):
        lines = page_payload[index]["text"].splitlines()
        page_payload[index]["text"] = "\n".join(
            [lines[0], "2025\u5e74\u5ea6 \u4eba\u6c11\u5e01\u5143", *lines[3:]]
        )
    page_payload[2]["text"] = page_payload[2]["text"].replace(
        "\u8425\u4e1a\u6210\u672c | 600",
        "\u51cf\uff1a\u8425\u4e1a\u6210\u672c | 600",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.issues == ()
    assert result.facts["operating_cost"] == Decimal("600")


def test_bare_cny_unit_after_title_activates_statement_from_table_header(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    for index in (2, 3):
        lines = page_payload[index]["text"].splitlines()
        page_payload[index]["text"] = "\n".join(
            [
                "2025年度",
                lines[0],
                "人民币万元",
                "项目 2025年度 2024年度",
                *lines[3:],
            ]
        )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_balance_date_with_inline_unit_activates_statement(
    tmp_path: Path,
) -> None:
    """Audited balance sheets may combine their date and CNY unit."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    lines = page_payload[1]["text"].splitlines()
    page_payload[1]["text"] = "\n".join(
        [lines[0], "2025\u5e7412\u670831\u65e5 \u4eba\u6c11\u5e01\u5143", *lines[3:]]
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.issues == ()


def test_net_profit_with_loss_qualifier_maps_to_canonical_fact(
    tmp_path: Path,
) -> None:
    """Audited statements may label net profit as net profit / (loss)."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[2]["text"] = page_payload[2]["text"].replace(
        "\u51c0\u5229\u6da6 | 180",
        "\u51c0\u5229\u6da6 / (\u4e8f\u635f) | 180",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.issues == ()
    assert result.facts["net_profit"] == Decimal("1800000")


def test_standalone_company_statement_stops_consolidated_extraction(
    tmp_path: Path,
) -> None:
    """Parent-company statements must not conflict with consolidated facts."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload.append(
        {
            "page_number": 46,
            "text": (
                "\u8d44\u4ea7\u8d1f\u503a\u8868\n"
                "2025\u5e7412\u670831\u65e5 \u4eba\u6c11\u5e01\u5143\n"
                "\u8d44\u4ea7\u603b\u8ba1 | 9,999\n"
                "\u8d1f\u503a\u5408\u8ba1 | 8,888\n"
                "\u6240\u6709\u8005\u6743\u76ca\u5408\u8ba1 | 1,111"
            ),
        }
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.issues == ()
    assert result.facts["total_assets"] == Decimal("20000000")


def test_numeric_note_column_uses_current_statement_value(
    tmp_path: Path,
) -> None:
    """Pure numeric note references must not be mistaken for fact values."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[1]["text"] = page_payload[1]["text"].replace(
        "\u8d27\u5e01\u8d44\u91d1 | 300",
        "\u8d27\u5e01\u8d44\u91d1 1 300 250",
    )
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "\u7ecf\u8425\u6d3b\u52a8\u4ea7\u751f\u7684\u73b0\u91d1\u6d41\u91cf\u51c0\u989d | 220",
        "\u7ecf\u8425\u6d3b\u52a8\u4ea7\u751f\u7684\u73b0\u91d1\u6d41\u91cf\u51c0\u989d 64 220 210",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.issues == ()
    assert result.facts["cash_and_equivalents"] == Decimal("3000000")
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_cash_flow_aliases_accept_used_investing_and_net_change_without_amount(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = (
        page_payload[3]["text"]
        .replace(
            "投资活动产生的现金流量净额",
            "投资活动所用的现金流量净额",
        )
        .replace(
            "现金及现金等价物净增加额",
            "现金及现金等价物净增加",
        )
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["investing_cash_flow"] == Decimal("-500000")
    assert result.facts["net_cash_change"] == Decimal("1500000")


def test_single_comparative_cash_component_is_zeroed_when_equation_proves_blank(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "现金及现金等价物净增加额 | 150",
        "现金及现金等价物净增加额 | 170",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["financing_cash_flow"] == Decimal("0")
    assert (
        "financing_cash_flow",
        ("comparative_only_value", "cash_flow_equation"),
    ) in result.derivations


def test_cash_component_value_split_after_standalone_negative_sign(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "经营活动产生的现金流量净额 | 220",
        "经营活动产生的现金流量净额 | (80)",
    ).replace(
        "筹资活动产生的现金流量净额 | (20)",
        "筹资活动产生的现金流量净额\n-\n20 10",
    ).replace(
        "现金及现金等价物净增加额 | 150",
        "现金及现金等价物净增加额 -\n150 140",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["financing_cash_flow"] == Decimal("-200000")


def test_split_numeric_note_column_uses_current_statement_value(
    tmp_path: Path,
) -> None:
    """PDF glyph positioning can split note 50 into the tokens `5 0`."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "经营活动产生的现金流量净额 | 220",
        "经营活动产生的现金流量净额 5 0 220 210",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_chinese_hyphen_note_reference_precedes_statement_value(
    tmp_path: Path,
) -> None:
    """SSE statements commonly format note references as `七-1`."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[1]["text"] = page_payload[1]["text"].replace(
        "货币资金 | 300",
        "货币资金 七-1 300 250",
    )
    page_payload[2]["text"] = page_payload[2]["text"].replace(
        "营业成本 | 600",
        "营业成本 七-46 600 500",
    )
    page_payload[1]["text"] = page_payload[1]["text"].replace(
        "实收资本（或股本） | 10,000",
        "实收资本（或股本） 七-40 10,000 9,000",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["cash_and_equivalents"] == Decimal("3000000")
    assert result.facts["operating_cost"] == Decimal("6000000")
    assert result.facts["total_shares"] == Decimal("100000000")


@pytest.mark.parametrize("note_reference", ["注释 1", "八（七）1", "五、 （一）"])
def test_additional_cninfo_note_reference_formats_precede_statement_value(
    tmp_path: Path,
    note_reference: str,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[1]["text"] = page_payload[1]["text"].replace(
        "货币资金 | 300",
        f"货币资金 {note_reference} 300 250",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["cash_and_equivalents"] == Decimal("3000000")


def test_cashflow_use_and_net_change_aliases_reconcile(
    tmp_path: Path,
) -> None:
    """Audited cash-flow wording can use 'used' and explicit decrease labels."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = (
        page_payload[3]["text"]
        .replace(
            "\u6295\u8d44\u6d3b\u52a8\u4ea7\u751f\u7684\u73b0\u91d1\u6d41\u91cf\u51c0\u989d | (50)",
            "\u6295\u8d44\u6d3b\u52a8\u4f7f\u7528\u7684\u73b0\u91d1\u6d41\u91cf\u51c0\u989d | (50)",
        )
        .replace(
            "\u7b79\u8d44\u6d3b\u52a8\u4ea7\u751f\u7684\u73b0\u91d1\u6d41\u91cf\u51c0\u989d | (20)",
            "\u7b79\u8d44\u6d3b\u52a8\u4f7f\u7528\u7684\u73b0\u91d1\u6d41\u91cf\u51c0\u989d | (20)",
        )
        .replace(
            "\u73b0\u91d1\u53ca\u73b0\u91d1\u7b49\u4ef7\u7269\u51c0\u589e\u52a0\u989d | 150",
            "\u73b0\u91d1\u53ca\u73b0\u91d1\u7b49\u4ef7\u7269\u51c0\u51cf\u5c11\u989d | 150",
        )
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.issues == ()


def test_note_reference_with_letter_suffix_precedes_cash_value(
    tmp_path: Path,
) -> None:
    """Audit note references such as 62(1)b are not statement values."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "\u73b0\u91d1\u53ca\u73b0\u91d1\u7b49\u4ef7\u7269\u51c0\u589e\u52a0\u989d | 150",
        (
            "\u73b0\u91d1\u53ca\u73b0\u91d1\u7b49\u4ef7\u7269\u51c0\u51cf\u5c11\u989d "
            "\u4e94\u300162(1)b 150 155"
        ),
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.issues == ()
    assert result.facts["net_cash_change"] == Decimal("1500000")


def test_truncated_capital_expenditure_label_with_value_is_supported(
    tmp_path: Path,
) -> None:
    """CNINFO text extraction can drop the tail of a long capex row label."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        (
            "\u8d2d\u5efa\u56fa\u5b9a\u8d44\u4ea7\u3001\u65e0\u5f62\u8d44\u4ea7\u548c\u5176\u4ed6"
            "\u957f\u671f\u8d44\u4ea7\u652f\u4ed8\u7684\u73b0\u91d1 | 50"
        ),
        (
            "\u8d2d\u5efa\u56fa\u5b9a\u8d44\u4ea7\u3001\u65e0\u5f62\u8d44\u4ea7\u548c\u5176\u4ed6"
            "\u957f | 50"
        ),
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.issues == ()
    assert result.facts["capital_expenditure"] == Decimal("500000")


def test_capital_expenditure_value_before_cross_page_cash_suffix_is_supported(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "购建固定资产、无形资产和其他长期资产支付的现金 | 50",
        "购建固定资产、无形资产和其他长期资产所支付的 | 50\n现金",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["capital_expenditure"] == Decimal("500000")


@pytest.mark.parametrize(
    "truncated_label",
    [
        "购建固定资产、无形资产和其",
        "购建固定资产、无形资产和其他长期资产",
    ],
)
def test_capital_expenditure_accepts_arbitrary_long_label_truncation(
    tmp_path: Path,
    truncated_label: str,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "购建固定资产、无形资产和其他长期资产支付的现金 | 50",
        f"{truncated_label} | 50",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["capital_expenditure"] == Decimal("500000")


def test_capital_expenditure_continuation_accepts_parenthesized_note_reference(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "购建固定资产、无形资产和其他长期资产支付的现金 | 50",
        "购建固定资产、无形资产和其\n他长期资产支付的现金 78(2) 50 40",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["capital_expenditure"] == Decimal("500000")


def test_capital_expenditure_prefix_after_prior_row_continues_on_next_line(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[3]["text"] = page_payload[3]["text"].replace(
        "购建固定资产、无形资产和其他长期资产支付的现金 | 50",
        (
            "投资活动现金流入小计 100 90 "
            "购建固定资产、无形资产和其他长期资产支付的\n"
            "现金 50 40"
        ),
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["capital_expenditure"] == Decimal("500000")


def test_financial_statement_unit_accepts_amount_unit_wording(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    for page in page_payload:
        if isinstance(page["text"], str):
            page["text"] = page["text"].replace(
                "单位：人民币万元",
                "除特别注明外，金额单位均为人民币万元",
            )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["total_assets"] == Decimal("20000000")


def test_company_cash_flow_statement_stops_consolidated_extraction(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload.append(
        {
            "page_number": 46,
            "text": (
                "公司现金流量表\n"
                "2025年度 人民币万元\n"
                "经营活动产生的现金流量净额 | 999\n"
                "投资活动产生的现金流量净额 | 888\n"
                "筹资活动产生的现金流量净额 | 777\n"
                "汇率变动对现金及现金等价物的影响 | 0\n"
                "现金及现金等价物净增加额 | 2,664"
            ),
        }
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


def test_labeled_a_share_code_accepts_pdf_inserted_digit_spacing(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] = page_payload[0]["text"].replace(
        "699998.SH",
        "证券代码： 6 9 9 9 9 8",
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.VALID, result.issues


def test_code_free_cover_requires_matching_expected_issuer_name(
    tmp_path: Path,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] = page_payload[0]["text"].replace("699998.SH", "")

    without_name = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )
    with_name = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash).model_copy(
            update={"issuer_name": "虚构公司"}
        ),
    )

    assert "PDF_LAYOUT_UNSUPPORTED" in without_name.issues
    assert with_name.quality_status is QualityStatus.VALID, with_name.issues


@pytest.mark.parametrize("label", ["股票代碼", "證券代碼", "公司代碼"])
def test_traditional_chinese_issuer_code_labels(tmp_path: Path, label: str) -> None:
    path, content_hash = write_pdf(tmp_path)
    payload = pages()
    payload[0]["text"] = payload[0]["text"].replace("699998.SH", f"{label}：699998")
    result = extractor(payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is QualityStatus.VALID, result.issues


@pytest.mark.parametrize("profile_code,valid", [("699998", True), ("600518", False)])
def test_company_profile_code_takes_precedence_over_subsidiary_definition(
    tmp_path: Path, profile_code: str, valid: bool,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    payload = pages()
    payload[0]["text"] = payload[0]["text"].replace("699998.SH", "")
    payload.insert(1, {"page_number": 3, "text": "释义\n子公司：证券代码600518"})
    payload.insert(2, {"page_number": 8, "text": (
        "第二节 公司简介和主要财务指标\n一、公司简介\n"
        "股票上市交易所名称及代码 A 股：上海证券交易所\n"
        f"代码：{profile_code} A 股简称：虚构公司\n"
        "H 股：香港联合交易所\n代码：00874 H 股简称：虚构公司"
    )})
    result = extractor(payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert ("PDF_LAYOUT_UNSUPPORTED" not in result.issues) is valid


@pytest.mark.parametrize("profile_page", [19, 21])
def test_explicit_a_share_profile_label_precedes_counterparty_tickers(
    tmp_path: Path, profile_page: int,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    payload = pages()
    payload[0]["text"] = payload[0]["text"].replace("699998.SH", "")
    payload.insert(1, {"page_number": profile_page, "text": (
        "法定中文名称：虚构公司\nA 股上市交易所：上海证券交易所\n"
        "A 股简称：虚构\nA 股代码：699998\nH 股代号：02601\n公司简介"
    )})
    payload.insert(2, {"page_number": 20, "text": "合作方：证券代码600518"})
    result = extractor(payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is QualityStatus.VALID, result.issues


def test_adjusted_profit_for_company_shareholders_with_split_inline_unit(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    payload = pages()
    for page in payload:
        if isinstance(page["text"], str):
            page["text"] = page["text"].replace(
                "扣除非经常性损益后的净利润 | 170",
                "归属于本公司股东的扣除\n非经常性损益的净利润\n（人民币千元）\n1,700 1,500",
            )
    result = extractor(payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["adjusted_net_profit"] == Decimal("1700000")


@pytest.mark.parametrize("heading", ["2025 年12月31日止年度", "截至2025年12月31日止年度"])
def test_annual_statement_year_end_heading(tmp_path: Path, heading: str) -> None:
    path, content_hash = write_pdf(tmp_path)
    payload = pages()
    for page in payload:
        if isinstance(page["text"], str):
            for title in ("合并利润表", "合并现金流量表"):
                page["text"] = page["text"].replace(title, f"{title}\n{heading}")
    result = extractor(payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is QualityStatus.VALID, result.issues
    assert _statement_period_heading_matches(
        heading, descriptor=descriptor(content_hash), statement_type=StatementType.INCOME_STATEMENT,
    )


def test_annual_statement_year_end_heading_rejects_wrong_date():
    assert not _statement_period_heading_matches(
        "2024年12月31日止年度", descriptor=descriptor("a" * 64),
        statement_type=StatementType.CASH_FLOW,
    )


def test_cash_flow_note_with_parenthesized_letter(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    payload = pages()
    payload[3]["text"] = payload[3]["text"].replace(
        "经营活动产生的现金流量净额 | 220",
        "经营活动产生的现金流量净额 59(a) 220 210",
    )
    result = extractor(payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


@pytest.mark.parametrize("unit_heading", [
    "（人民币百万元，特别注明除外）", "人民币百万元，百分比除外",
])
def test_summary_year_header_keeps_explicit_million_yuan_unit(
    tmp_path: Path, unit_heading: str,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    payload = pages()
    for page in payload:
        if isinstance(page["text"], str):
            page["text"] = page["text"].replace("扣除非经常性损益后的净利润 | 170", "")
    payload.insert(1, {"page_number": 15, "text": (
        "本集团主要会计数据和财务指标\n"
        f"{unit_heading}2025 年 2024 年 2023 年\n"
        "扣除非经常性损益后归属于本行股东的净利润 1.7 1.5 1.4\n"
        "每股计（人民币元）\n基本每股收益 0.2 0.1 0.1"
    )})
    result = extractor(payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["adjusted_net_profit"] == Decimal("1700000")


def test_cash_exchange_effect_label_without_equivalents(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    payload = pages()
    payload[3]["text"] = payload[3]["text"].replace(
        "汇率变动对现金及现金等价物的影响 | 0", "四、汇率变动对现金的影响额 10 9",
    ).replace("现金及现金等价物净增加额 | 150", "现金及现金等价物净增加额 | 160")
    result = extractor(payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["cash_exchange_effect"] == Decimal("100000")


def test_summary_after_long_front_matter_with_note_number(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    payload = pages()
    for page in payload:
        if isinstance(page["text"], str):
            page["text"] = page["text"].replace("扣除非经常性损益后的净利润 | 170", "")
    payload.insert(1, {"page_number": 33, "text": (
        "单位：人民币百万元\n主要会计数据 2025 年\n2024 年 本年比上年\n"
        "增减（%）2023 年\n调整前 调整后\n"
        "扣除非经常性损\n益的净利润\n注 1 1.7 1.5 1.6 6.25 1.4"
    )})
    result = extractor(payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["adjusted_net_profit"] == Decimal("1700000")


@pytest.mark.parametrize("suffix, valid", [
    ("常性损益的净利润", True), ("其他说明\n常性损益的净利润", False),
])
def test_summary_amount_interleaved_inside_wrapped_adjusted_profit_label(
    tmp_path: Path, suffix: str, valid: bool,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    payload = pages()
    payload[2]["text"] = payload[2]["text"].replace("扣除非经常性损益后的净利润 | 170", "")
    payload.insert(1, {"page_number": 12, "text": (
        "主要会计数据\n单位：人民币万元\n2025年 2024年 2023年\n"
        "归属于上市公司\n股东的扣除非经 170 150 149 13.3 140 139\n"
        f"{suffix}\n经营活动产生的现金流量净额 220 200 180"
    )})
    result = extractor(payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    if valid:
        assert result.quality_status is QualityStatus.VALID, result.issues
        assert result.facts["adjusted_net_profit"] == Decimal("1700000")
    else:
        assert "adjusted_net_profit" not in result.facts
        assert result.quality_status is QualityStatus.UNVERIFIED


def test_summary_bare_note_does_not_consume_current_thousands_amount(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    payload = pages()
    for page in payload:
        if isinstance(page["text"], str):
            page["text"] = page["text"].replace("扣除非经常性损益后的净利润 | 170", "")
    payload.insert(1, {"page_number": 33, "text": (
        "单位：人民币千元\n主要会计数据 2025 年 2024 年\n"
        "扣除非经常性损\n益的净利润 注 1,700 1,500 13.3 1,400"
    )})
    result = extractor(payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["adjusted_net_profit"] == Decimal("1700000")


def test_summary_value_on_separate_line_inside_wrapped_label(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    payload = pages()
    payload[2]["text"] = payload[2]["text"].replace("扣除非经常性损益后的净利润 | 170", "")
    payload.insert(1, {"page_number": 10, "text": (
        "主要会计数据\n单位：人民币万元\n2025年 2024年 2023年\n"
        "归属于上市公司股东的扣除非经常性损益的净\n"
        "170 150 149 13.3 140 139\n利润\n"
    )})
    result = extractor(payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["adjusted_net_profit"] == Decimal("1700000")


@pytest.mark.parametrize("gap, first, expected", [
    (0.7, "1,245,899,80", "1,245,899,805"),
    (15, "1,245,899,80", "1,245,899,80 5"),
    (0.7, "1,245,899,800", "1,245,899,800 5"),
])
def test_native_coordinate_join_only_adjacent_incomplete_thousands_group(
    gap: float, first: str, expected: str,
) -> None:
    cells = [
        ([(0, 0), (50, 0), (50, 10), (0, 10)], first, 1),
        ([(50 + gap, 0), (55 + gap, 0), (55 + gap, 10), (50 + gap, 10)], "5", 1),
    ]
    assert _ocr_result_text(cells, join_numeric_fragments=True) == expected
    assert _ocr_result_text(cells) == first + " 5"


def test_split_note_before_parenthesized_operating_cash_outflow() -> None:
    assert _parse_fact_line(
        "经营活动 ( 使用 ) 产生 的现金流量净额 6 5(1) (31,423,832) 20,412,048"
    ) == ("operating_cash_flow", Decimal("-31423832"))


@pytest.mark.parametrize("line, expected", [
    ("资产总计 11 , 583 , 417 11 , 009 , 940", ("total_assets", "11583417")),
    ("资产总计 11 , 583 , 417 11 , 009 , 940 9 , 994 , 079",
     ("total_assets", "11583417")),
    ("货币资金 1 577 , 212 613 , 737", ("cash_and_equivalents", "577212")),
    ("经营活动产生的现金流量净额 59 ( 1 ) 360 , 403 476 , 776",
     ("operating_cash_flow", "360403")),
    ("投资活动使用的现金流量净额 ( 104 , 001 ) ( 215 , 760 )",
     ("investing_cash_flow", "-104001")),
])
def test_grouped_amounts_with_spaces_around_commas_and_notes(
    line: str, expected: tuple[str, str],
) -> None:
    assert _parse_fact_line(line) == (expected[0], Decimal(expected[1]))


@pytest.mark.parametrize("definition, expected", [
    ("平安、中国平安、公司、 指 中国平安保险（集团）股份有限公司", True),
    ("平安、中国平安 指 中国平安保险（集团）股份有限公司", False),
    ("平安寿险、公司 指 中国平安人寿保险股份有限公司", False),
    ("平安、中国平安、公司 指 中国平安保险（集团）股份有限公司，是本公司的子公司", False),
])
def test_insurance_issuer_identified_by_exact_self_definition(
    definition: str, expected: bool,
) -> None:
    assert _is_financial_institution_report(
        "释义\n" + definition + "\n保险合同负债 123\n保险服务收入 456",
        issuer_name="中国平安",
    ) is expected


def test_split_note_cannot_become_operating_cost_with_broken_comparative() -> None:
    assert _parse_fact_line(
        "减：营业成本 4 8 957,601,788 1,019,749,05 1"
    ) == ("operating_cost", Decimal("957601788"))


def test_wrapped_summary_allows_spaced_comparative_minus(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    payload = pages()
    payload[2]["text"] = payload[2]["text"].replace("扣除非经常性损益后的净利润 | 170", "")
    payload.insert(1, {"page_number": 9, "text": (
        "主要会计数据\n单位：人民币万元\n2025年 2024年 2023年\n"
        "归属于上市公司股\n东的扣除非经常性 170 190 - 13.2 4 180 179\n"
        "损益的净利润\n"
    )})
    result = extractor(payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["adjusted_net_profit"] == Decimal("1700000")


@pytest.mark.parametrize("year, expected", [(2025, "100000123"), (2024, "100000000")])
def test_front_matter_exact_dated_total_shares_overrides_rounded_capital(
    tmp_path: Path, year: int, expected: str,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    payload = pages()
    payload[-1]["text"] = ""
    payload[1]["text"] += "\n股本 | 10,000"
    payload.insert(1, {"page_number": 3, "text": (
        f"利润分配预案：以 {year} 年 12 月 31 日公司总股本 100,000,123 股为基数，"
        "每10股派送现金红利3.50元。"
    )})
    result = extractor(payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["total_shares"] == Decimal(expected)


def test_stock_profile_table_not_counterparty_code_identifies_issuer(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    payload = pages()
    payload[0]["text"] = payload[0]["text"].replace("699998.SH", "")
    payload.insert(1, {"page_number": 8, "text": (
        "公司股票简况\n股票种类 股票上市交易所 股票简称 股票代码 变更前股票简称\n"
        "A 股 上海证券交易所 虚构公司 699998 -\nH 股 香港联合交易所 虚构公司 00390 -\n"
        "其他相关资料\n合作方证券代码600518"
    )})
    result = extractor(payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is QualityStatus.VALID, result.issues


def test_balance_sheet_table_header_carries_currency_after_dates(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    payload = pages()
    payload[1]["text"] = payload[1]["text"].replace(
        "合并资产负债表\n2025 年 12 月 31 日\n单位：人民币万元",
        "2025年12月31日合并资产负债表\n"
        "资产 附五 2025年12月31日 2024年12月31日 人民币万元\n流动资产",
    )
    result = extractor(payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["cash_and_equivalents"] == Decimal("3000000")


@pytest.mark.parametrize("current_year, valid", [(2025, True), (2024, False)])
def test_income_table_ocr_interleaves_header_and_total_revenue(
    tmp_path: Path, current_year: int, valid: bool,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    payload = pages()
    payload[2]["text"] = payload[2]["text"].replace(
        "合并利润表\n2025 年 1—12 月\n单位：人民币万元\n营业收入 | 1,000",
        "2025年度合并利润表\n人民币万元\n"
        f"营业总收入 项目 国 附注五 1,000 {current_year}年度 900 {current_year - 1}年度\n"
        "其中：营业收入 54 990 890",
    )
    result = extractor(payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    if valid:
        assert result.quality_status is QualityStatus.VALID, result.issues
        assert result.facts["revenue"] == Decimal("10000000")
    else:
        assert result.quality_status is QualityStatus.UNVERIFIED
        assert "revenue" not in result.facts


@pytest.mark.parametrize("company_heading", [
    "2025年12月31日公司资产负债表 流动资产 资产 附注 2025年12月31日",
    "2025年度公司利润表",
    "2025年度公司现金流量表",
])
def test_company_only_statements_end_consolidated_extraction(
    tmp_path: Path, company_heading: str,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    payload = pages()
    payload.insert(-1, {"page_number": 45, "text": (
        f"{company_heading}\n人民币万元\n资产总计 | 900\n营业收入 | 500\n"
        "经营活动产生的现金流量净额 | 330"
    )})
    result = extractor(payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    assert result.quality_status is QualityStatus.VALID, result.issues
    assert result.facts["total_assets"] == Decimal("20000000")
    assert result.facts["revenue"] == Decimal("10000000")
    assert result.facts["operating_cash_flow"] == Decimal("2200000")


@pytest.mark.parametrize("year, valid", [(2025, True), (2024, False)])
def test_exact_period_immediately_before_consolidated_title(
    tmp_path: Path, year: int, valid: bool,
) -> None:
    path, content_hash = write_pdf(tmp_path)
    payload = pages()
    payload[1]["text"] = payload[1]["text"].replace(
        "合并资产负债表\n2025 年 12 月 31 日",
        f"虚构公司股份有限公司\n{year} 年 12 月 31 日\n合并资产负债表",
    )
    payload[2]["text"] = payload[2]["text"].replace(
        "合并利润表\n2025 年 1—12 月",
        f"虚构公司股份有限公司\n{year} 年度\n合并利润表",
    )
    result = extractor(payload).extract(pdf_path=path, descriptor=descriptor(content_hash))
    if valid:
        assert result.quality_status is QualityStatus.VALID, result.issues
        assert result.facts["total_assets"] == Decimal("20000000")
        assert result.facts["revenue"] == Decimal("10000000")
    else:
        assert result.quality_status is QualityStatus.UNVERIFIED
        assert "total_assets" not in result.facts
        assert "revenue" not in result.facts


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


def test_build_document_preserves_exchange_pdf_source(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    filing_descriptor = descriptor(content_hash).model_copy(
        update={
            "source_id": "szse",
            "source_url": "https://disc.static.szse.cn/disc/fixture.PDF",
        }
    )
    configured = extractor(pages())
    extracted = configured.extract(
        pdf_path=path,
        descriptor=filing_descriptor,
    )

    document = configured.build_document(
        filing_id="pdf-szse-fixture-v1",
        descriptor=filing_descriptor,
        extracted=extracted,
        supersedes_id=None,
    )

    assert document.source_id == "szse"
    assert str(document.source_url) == str(filing_descriptor.source_url)


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
    assert all(lineage.page_number > 0 for lineage in document.fact_lineage.values())
