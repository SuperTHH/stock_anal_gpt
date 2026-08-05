from datetime import UTC, date, datetime
from decimal import Decimal

from hengce.contracts.enums import QualityStatus
from hengce.financials.fallback import CninfoPdfFactExtractor


def test_cninfo_text_facts_remain_unverified_before_accounting_checks() -> None:
    """Catches promoting OCR/text-extracted values directly into strategy inputs."""
    extractor = CninfoPdfFactExtractor("cninfo-pdf-v1")
    result = extractor.parse_text(
        """
        报告期：2025-12-31
        货币单位：人民币元
        资产总计 | 1,200.00
        负债合计 | 400.00
        所有者权益合计 | 800.00
        营业收入 | 1,500.00
        净利润 | 120.00
        经营活动产生的现金流量净额 | 180.00
        """,
        filing_id="cninfo-1",
    )

    assert result.quality_status is QualityStatus.UNVERIFIED
    assert result.report_period == date(2025, 12, 31)
    assert result.facts["total_assets"] == Decimal("1200.00")
    assert result.facts["net_profit"] == Decimal("120.00")


def test_balanced_complete_pdf_facts_become_valid_after_rule_checks() -> None:
    """Catches accepting an unbalanced statement or forgetting the required cash-flow fact."""
    extractor = CninfoPdfFactExtractor("cninfo-pdf-v1")
    parsed = extractor.parse_text(
        """
        报告期：2025-12-31
        货币单位：人民币元
        资产总计 | 1,200
        负债合计 | 400
        所有者权益合计 | 800
        营业收入 | 1,500
        净利润 | 120
        经营活动产生的现金流量净额 | 180
        """,
        filing_id="cninfo-1",
    )

    validated = extractor.validate(parsed)

    assert validated.quality_status is QualityStatus.VALID
    assert validated.issues == ()


def test_unbalanced_pdf_facts_stay_unverified_with_machine_readable_issue() -> None:
    """Catches passing a visually plausible extraction whose balance sheet does not balance."""
    extractor = CninfoPdfFactExtractor("cninfo-pdf-v1")
    parsed = extractor.parse_text(
        """
        报告期：2025-12-31
        货币单位：人民币元
        资产总计 | 1,200
        负债合计 | 500
        所有者权益合计 | 800
        营业收入 | 1,500
        净利润 | 120
        经营活动产生的现金流量净额 | 180
        """,
        filing_id="cninfo-1",
    )

    validated = extractor.validate(parsed)

    assert validated.quality_status is QualityStatus.UNVERIFIED
    assert validated.issues == ("PDF_BALANCE_EQUATION_FAILED",)


def test_balance_tolerance_is_one_part_per_million_not_one_per_thousand() -> None:
    """Catches materially loose validation accidentally blessing a bad PDF fallback."""
    extractor = CninfoPdfFactExtractor("cninfo-pdf-v1")
    parsed = extractor.parse_text(
        """
        报告期：2025-12-31
        货币单位：人民币元
        资产总计 | 1,000,000
        负债合计 | 400,000
        所有者权益合计 | 599,500
        营业收入 | 1,500
        净利润 | 120
        经营活动产生的现金流量净额 | 180
        """,
        filing_id="cninfo-strict-tolerance",
    )

    validated = extractor.validate(parsed)

    assert validated.quality_status is QualityStatus.UNVERIFIED
    assert validated.issues == ("PDF_BALANCE_EQUATION_FAILED",)


def test_version_metadata_requires_aware_publication_time() -> None:
    """Catches losing announcement/version ordering when creating a PDF fallback filing."""
    extractor = CninfoPdfFactExtractor("cninfo-pdf-v1")
    result = extractor.build_document(
        filing_id="cninfo-1",
        ts_code="000001.SZ",
        source_url="https://www.cninfo.com.cn/new/disclosure/detail?plate=szse",
        published_at=datetime(2026, 4, 1, 8, tzinfo=UTC),
        version="annual-v1",
        supersedes_id=None,
        parsed=extractor.parse_text(
            """
            报告期：2025-12-31
            货币单位：人民币元
            资产总计 | 100
            """,
            filing_id="cninfo-1",
        ),
    )

    assert result.published_at.tzinfo is UTC
    assert result.version == "annual-v1"
    assert result.supersedes_id is None
    assert result.normalization_metadata["unit_multiplier"] == "1"


def test_pdf_monetary_unit_is_normalized_to_cny_and_preserved_in_lineage() -> None:
    """Catches treating values reported in 万元 as if they were individual yuan."""
    result = CninfoPdfFactExtractor("cninfo-pdf-v1").parse_text(
        """
        报告期：2025-12-31
        货币单位：万元
        资产总计 | 1,200
        负债合计 | 400
        所有者权益合计 | 800
        营业收入 | 1,500
        净利润 | 120
        经营活动产生的现金流量净额 | 180
        """,
        filing_id="cninfo-unit",
    )

    assert result.currency == "CNY"
    assert result.currency_unit == "万元"
    assert result.unit_multiplier == Decimal("10000")
    assert result.facts["total_assets"] == Decimal("12000000")
    assert CninfoPdfFactExtractor("cninfo-pdf-v1").validate(
        result
    ).quality_status is QualityStatus.VALID
