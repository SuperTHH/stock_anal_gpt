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


def test_bank_report_does_not_require_industrial_balance_fields(tmp_path: Path) -> None:
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[0]["text"] += "\n银行资产负债表"
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


def test_incomplete_debt_components_never_create_an_estimated_total(
    tmp_path: Path,
) -> None:
    """Catches treating an absent debt row as zero when the row is not visible."""
    path, content_hash = write_pdf(tmp_path)
    page_payload = pages()
    page_payload[1]["text"] = page_payload[1]["text"].replace(
        "有息负债 | 250",
        ("短期借款 | 100\n一年内到期的非流动负债 | 50\n长期借款 | 0\n应付债券 | 0"),
    )

    result = extractor(page_payload).extract(
        pdf_path=path,
        descriptor=descriptor(content_hash),
    )

    assert result.quality_status is QualityStatus.UNVERIFIED
    assert "PDF_REQUIRED_FACTS_MISSING" in result.issues
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


def test_q1_title_and_generic_statement_headers_identify_implicit_period(
    tmp_path: Path,
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
        "\u5408\u5e76\u8d44\u4ea7\u8d1f\u503a\u8868\n"
        "\u7f16\u5236\u5355\u4f4d\uff1a\u865a\u6784\u516c\u53f8\n"
        "\u5355\u4f4d\uff1a\u4eba\u6c11\u5e01\u4e07\u5143\n"
        "\u9879\u76ee \u671f\u672b\u4f59\u989d \u671f\u521d\u4f59\u989d\n"
        + "\n".join(page_payload[1]["text"].splitlines()[3:])
    )
    for index in (2, 3):
        lines = page_payload[index]["text"].splitlines()
        page_payload[index]["text"] = "\n".join(
            [
                lines[0],
                "\u5355\u4f4d\uff1a\u4eba\u6c11\u5e01\u4e07\u5143",
                (
                    "\u9879\u76ee \u672c\u671f\u53d1\u751f\u989d "
                    "\u4e0a\u5e74\u540c\u671f\u53d1\u751f\u989d"
                ),
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
