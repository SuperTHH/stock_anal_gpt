from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from pypdf import PdfReader

from hengce.contracts.enums import QualityStatus, ReportType, StatementType
from hengce.contracts.financial import FilingDescriptor
from hengce.financials.pdf_cmaps import restore_declared_cmaps
from hengce.financials.registry_loader import CANONICAL_PILOT_FACTS

_GROUPED_INTEGER = r"(?:\d{1,3}(?:,\s*\d{3})+|\d+)"
_ACCOUNTING_NUMBER = (
    rf"(?:[-+]?{_GROUPED_INTEGER}(?:\.\d+)?|"
    rf"\(\s*{_GROUPED_INTEGER}(?:\.\d+)?\s*\))"
)
_NUMBER = re.compile(rf"^{_ACCOUNTING_NUMBER}$")
_CHINESE_NUMERAL = r"[一二三四五六七八九十]+"
_NOTE_REFERENCE = (
    rf"(?:注(?:释)?\s*\d+|{_CHINESE_NUMERAL}(?:、|[-－—])\d+"
    rf"(?:[（(]\d+[）)])?[A-Za-z]?|"
    rf"{_CHINESE_NUMERAL}、\s*[（(]{_CHINESE_NUMERAL}[）)]|"
    rf"{_CHINESE_NUMERAL}[（(]{_CHINESE_NUMERAL}[）)]\d+|"
    rf"{_CHINESE_NUMERAL}(?:[（(](?:\d+(?:\.\d+)?|[A-Za-z])[）)])+|"
    r"\d{1,3}[（(](?:\d+|[A-Za-z]+)[）)])"
)
_SUFFIXED_CODE = re.compile(r"\b[0-9]{6}\.(?:SH|SZ)\b")
_LABELED_CODE_DETAIL = re.compile(
    r"(?:[证證]券代[码碼](?:（A/H）|\(A/H\))?|股票代[码碼]|公司代[码碼])\s*[：:]?\s*"
    r"((?:[0-9]\s*){6})(?:\.([A-Z]{2,4}))?"
)
_REPORT_PERIOD = re.compile(r"报告期\s*[：:]\s*(\d{4}-\d{2}-\d{2})")
_WHITESPACE_FACT = re.compile(
    r"^(?P<label>.+?)\s+"
    rf"(?P<value>{_ACCOUNTING_NUMBER})"
    r"(?:\s+.*)?$"
)
_NOTE_COLUMN_FACT = re.compile(
    r"^(?P<label>.+?)\s+"
    rf"{_NOTE_REFERENCE}\s+"
    rf"(?P<value>{_ACCOUNTING_NUMBER})"
    r"(?:\s+.*)?$"
)
_SLASH_NOTE_COLUMN_FACT = re.compile(
    r"^(?P<label>.+?)\s+/\s+"
    rf"(?P<value>{_ACCOUNTING_NUMBER})"
    r"(?:\s+.*)?$"
)
_NUMERIC_NOTE_COLUMN_FACT = re.compile(
    r"^(?P<label>.+?)\s+\d{1,3}\s+"
    rf"(?P<value>{_ACCOUNTING_NUMBER})\s+"
    rf"{_ACCOUNTING_NUMBER}(?:\s+.*)?$"
)
_UNIT = re.compile(
    r"单位(?:均为|为|均以)?\s*[：:]?\s*(人民币元|人民币千元|人民币万元|人民币百万元|人民币亿元|元|千元|万元|百万元|亿元|千股|股)"
)
_SPLIT_NUMERIC_NOTE_COLUMN_FACT = re.compile(
    r"^(?P<label>.+?)\s+\d{1,2}\s+\d\s+"
    rf"(?P<value>{_ACCOUNTING_NUMBER})\s+"
    rf"{_ACCOUNTING_NUMBER}(?:\s+.*)?$"
)
_INLINE_CNY_UNIT = re.compile(
    r"[（(](?:人民币)?(元|千元|万元|百万元|亿元)(?:[，,]\s*特别注明除外)?[）)]"
)
_BARE_CNY_UNIT = re.compile(r"(人民币(?:元|千元|万元|百万元|亿元))")
_UNIT_DEFINITIONS = {
    "人民币元": ("CNY", Decimal(1)),
    "人民币千元": ("CNY", Decimal(1_000)),
    "人民币万元": ("CNY", Decimal(10_000)),
    "人民币百万元": ("CNY", Decimal(1_000_000)),
    "人民币亿元": ("CNY", Decimal(100_000_000)),
    "元": ("CNY", Decimal(1)),
    "千元": ("CNY", Decimal(1_000)),
    "万元": ("CNY", Decimal(10_000)),
    "百万元": ("CNY", Decimal(1_000_000)),
    "亿元": ("CNY", Decimal(100_000_000)),
    "千股": ("SHARES", Decimal(1_000)),
    "股": ("SHARES", Decimal(1)),
}
_REPORT_TYPE_MARKERS = {
    ReportType.ANNUAL: ("年度报告",),
    ReportType.Q1: ("第一季度报告", "一季度报告"),
    ReportType.HALF_YEAR: ("半年度报告",),
    ReportType.Q3: ("第三季度报告", "三季度报告"),
}
_STATEMENT_TITLES = {
    "主要财务数据": StatementType.INCOME_STATEMENT,
    "主要会计数据": StatementType.INCOME_STATEMENT,
    "主要会计数据和财务指标": StatementType.INCOME_STATEMENT,
    "主要会计数据及财务指标": StatementType.INCOME_STATEMENT,
    "合并资产负债表": StatementType.BALANCE_SHEET,
    "合并利润表": StatementType.INCOME_STATEMENT,
    "合并现金流量表": StatementType.CASH_FLOW,
    "股本信息": StatementType.BALANCE_SHEET,
    "股份变动情况表": StatementType.BALANCE_SHEET,
}
_CONSOLIDATED_STATEMENT_TITLES = frozenset({"合并资产负债表", "合并利润表", "合并现金流量表"})
_COMBINED_STATEMENT_TITLES = {
    "合并资产负债表和资产负债表": StatementType.BALANCE_SHEET,
    "合并利润表和利润表": StatementType.INCOME_STATEMENT,
    "合并现金流量表和现金流量表": StatementType.CASH_FLOW,
    "合并及公司资产负债表": StatementType.BALANCE_SHEET,
    "合并及母公司资产负债表": StatementType.BALANCE_SHEET,
    "合并及公司利润表": StatementType.INCOME_STATEMENT,
    "合并及母公司利润表": StatementType.INCOME_STATEMENT,
    "合并及公司现金流量表": StatementType.CASH_FLOW,
    "合并及母公司现金流量表": StatementType.CASH_FLOW,
    "合并及银行资产负债表": StatementType.BALANCE_SHEET,
    "合并及银行利润表": StatementType.INCOME_STATEMENT,
    "合并及银行现金流量表": StatementType.CASH_FLOW,
}
_EMBEDDED_CONSOLIDATED_TITLES = {
    "资产负债表": ("合并资产负债表", StatementType.BALANCE_SHEET),
    "利润表": ("合并利润表", StatementType.INCOME_STATEMENT),
    "现金流量表": ("合并现金流量表", StatementType.CASH_FLOW),
}
_EXTRACTION_BOUNDARIES = (
    "母公司资产负债表",
    "母公司利润表",
    "母公司现金流量表",
    "公司资产负债表",
    "公司利润表",
    "公司现金流量表",
    "银行资产负债表",
    "银行利润表",
    "银行现金流量表",
    "资产负债表",
    "利润表",
    "现金流量表",
    "合并所有者权益变动表",
    "母公司所有者权益变动表",
    "合并股东权益变动表",
    "财务报表附注",
    "主要财务指标",
    "分季度主要财务指标",
    "主要会计数据、财务指标发生变动的情况、原因",
    "主要会计数据、财务指标发生变动的情况及原因",
)
_FORMAL_STATEMENT_END_BOUNDARIES = (
    "财务报表附注",
)
_POST_CASH_FLOW_END_BOUNDARIES = (
    "合并所有者权益变动表",
    "母公司所有者权益变动表",
    "合并股东权益变动表",
    "合并及公司股东权益变动表",
)
_ALIASES = {
    "资产总计": "total_assets",
    "资产合计": "total_assets",
    "流动资产合计": "current_assets",
    "货币资金": "cash_and_equivalents",
    "负债合计": "total_liabilities",
    "流动负债合计": "current_liabilities",
    "有息负债": "interest_bearing_debt",
    "短期借款": "short_term_borrowings",
    "一年内到期的非流动负债": "current_portion_noncurrent_liabilities",
    "长期借款": "long_term_borrowings",
    "应付债券": "bonds_payable",
    "租赁负债": "lease_liabilities",
    "所有者权益合计": "equity",
    "股东权益合计": "equity",
    "所有者权益（或股东权益）合计": "equity",
    "\u6240\u6709\u8005\u6743\u76ca\uff08\u6216\u80a1\u4e1c\u6743\u76ca": "equity",
    "\u6240\u6709\u8005\u6743\u76ca\uff08\u6216\u80a1\u4e1c\u6743": "equity",
    "营业收入": "revenue",
    "营业收入合计": "revenue",
    "营业总收入": "revenue",
    "营业成本": "operating_cost",
    "营业支出": "operating_cost",
    "净利润": "net_profit",
    "扣除非经常性损益后的净利润": "adjusted_net_profit",
    "扣除非经常性损益的净利润": "adjusted_net_profit",
    "归属于上市公司股东的扣除非经常性损益的净利润": ("adjusted_net_profit"),
    "归属于上市公司普通股股东的扣除非经常性损益的净利润": ("adjusted_net_profit"),
    "归属于母公司股东的扣除非经常性损益后的净利润": "adjusted_net_profit",
    "归属于母公司股东扣除非经常性损益后的净利润": "adjusted_net_profit",
    "归属于母公司股东的扣除非经常性损益的净利润": "adjusted_net_profit",
    "归属于本公司股东的扣除非经常性损益的净利润": "adjusted_net_profit",
    "归属于母公司股东扣除非经常性损益的净利润": "adjusted_net_profit",
    "归属于本行股东的扣除非经常性损益的净利润": "adjusted_net_profit",
    "归属于上市公司股东的扣除非经常性损益的净利": "adjusted_net_profit",
    "利息费用": "interest_expense",
    "利息支出": "interest_expense",
    "经营活动产生的现金流量净额": "operating_cash_flow",
    "经营活动产生/(使用)的现金流量净额": "operating_cash_flow",
    "经营活动产生/（使用）的现金流量净额": "operating_cash_flow",
    "经营活动产生(使用)的现金流量净额": "operating_cash_flow",
    "经营活动产生（使用）的现金流量净额": "operating_cash_flow",
    "经营活动使用的现金流量净额": "operating_cash_flow",
    "经营活动所用的现金流量净额": "operating_cash_flow",
    "经营活动(使用)/产生的现金流量净额": "operating_cash_flow",
    "经营活动（使用）/产生的现金流量净额": "operating_cash_flow",
    "经营活动(所用)/产生的现金流量净额": "operating_cash_flow",
    "经营活动（所用）/产生的现金流量净额": "operating_cash_flow",
    "经营活动产生/(所用)的现金流量净额": "operating_cash_flow",
    "经营活动产生/（所用）的现金流量净额": "operating_cash_flow",
    "购建固定资产、无形资产和其他长期资产支付的现金": ("capital_expenditure"),
    "购建固定资产、无形资产和其他长期资产所支付的现金": ("capital_expenditure"),
    "购建固定资产、无形资产及其他长期资产支付的现金": ("capital_expenditure"),
    "购建固定资产、无形资产及其他长期资产所支付的现金": ("capital_expenditure"),
    "期末总股本": "total_shares",
    "实收资本（或股本）": "total_shares",
    "实收资本(或股本)": "total_shares",
    "股本": "total_shares",
    "投资活动产生的现金流量净额": "investing_cash_flow",
    "投资活动产生/(使用)的现金流量净额": "investing_cash_flow",
    "投资活动产生/（使用）的现金流量净额": "investing_cash_flow",
    "投资活动产生（使用）的现金流量净额": "investing_cash_flow",
    "投资活动产生(使用)的现金流量净额": "investing_cash_flow",
    "投资活动使用的现金流量净额": "investing_cash_flow",
    "投资活动所用的现金流量净额": "investing_cash_flow",
    "投资活动产生/(所用)的现金流量净额": "investing_cash_flow",
    "投资活动产生/（所用）的现金流量净额": "investing_cash_flow",
    "投资活动（使用）/产生的现金流量净额": "investing_cash_flow",
    "投资活动(使用)/产生的现金流量净额": "investing_cash_flow",
    "投资活动(所用)/产生的现金流量净额": "investing_cash_flow",
    "投资活动（所用）/产生的现金流量净额": "investing_cash_flow",
    "筹资活动产生的现金流量净额": "financing_cash_flow",
    "筹资活动使用的现金流量净额": "financing_cash_flow",
    "筹资活动产生/(使用)的现金流量净额": "financing_cash_flow",
    "筹资活动产生/（使用）的现金流量净额": "financing_cash_flow",
    "筹资活动产生/(所用)的现金流量净额": "financing_cash_flow",
    "筹资活动产生/（所用）的现金流量净额": "financing_cash_flow",
    "筹资活动(所用)/产生的现金流量净额": "financing_cash_flow",
    "筹资活动（所用）/产生的现金流量净额": "financing_cash_flow",
    "筹资活动(使用)/产生的现金流量净额": "financing_cash_flow",
    "筹资活动（使用）/产生的现金流量净额": "financing_cash_flow",
    "汇率变动对现金及现金等价物的影响": "cash_exchange_effect",
    "汇率变动对现金及现金等价物的影响额": "cash_exchange_effect",
    "汇率变动对现金的影响额": "cash_exchange_effect",
    "汇率变动对现金流量净额": "cash_exchange_effect",
    "现金及现金等价物净增加额": "net_cash_change",
    "现金及现金等价物净减少额": "net_cash_change",
    "现金及现金等价物净增加": "net_cash_change",
    "现金及现金等价物净减少": "net_cash_change",
    "现金及现金等价物净增加/(减少)": "net_cash_change",
    "现金及现金等价物净增加/（减少）": "net_cash_change",
    "现金及现金等价物净增加/(减少)额": "net_cash_change",
    "现金及现金等价物净增加/（减少）额": "net_cash_change",
    "现金及现金等价物净(减少)/增加": "net_cash_change",
    "现金及现金等价物净（减少）/增加": "net_cash_change",
    "现金及现金等价物净(减少)/增加额": "net_cash_change",
    "现金及现金等价物净（减少）/增加额": "net_cash_change",
    "现金及现金等价物净变动额": "net_cash_change",
    "股份总数": "total_shares",
    "扣除非经常性损益后归属于本行股东的净利润": "adjusted_net_profit",
}
_INTEREST_BEARING_DEBT_COMPONENTS = frozenset(
    {
        "short_term_borrowings",
        "current_portion_noncurrent_liabilities",
        "long_term_borrowings",
        "bonds_payable",
        "lease_liabilities",
    }
)
_INTEREST_BEARING_DEBT_DERIVATION_VERSION = "interest-bearing-debt-components-v1"
_OMITTED_DEBT_COMPONENT_DERIVATION_VERSION = (
    "omitted-zero-in-complete-reconciled-balance-sheet-v2"
)
_CASH_FLOW_RECONCILIATION = (
    "operating_cash_flow",
    "investing_cash_flow",
    "financing_cash_flow",
    "cash_exchange_effect",
    "net_cash_change",
)
_NEGATIVE_VALUE_SENTINEL = "<<NEGATIVE_VALUE>>"
_BANK_REQUIRED_FACTS = frozenset(
    {
        "revenue",
        "net_profit",
        "adjusted_net_profit",
        "operating_cash_flow",
        "total_assets",
        "total_liabilities",
        "equity",
        "total_shares",
    }
)
_Q1_REQUIRED_FACTS = CANONICAL_PILOT_FACTS - {"interest_expense"}
_FLATTENED_FACTS_BY_STATEMENT = {
    StatementType.BALANCE_SHEET: frozenset(
        {
            "total_assets", "current_assets", "cash_and_equivalents",
            "total_liabilities", "current_liabilities", "interest_bearing_debt",
            "short_term_borrowings", "current_portion_noncurrent_liabilities",
            "long_term_borrowings", "bonds_payable", "lease_liabilities",
            "equity", "total_shares",
        }
    ),
    StatementType.INCOME_STATEMENT: frozenset(
        {
            "revenue",
            "operating_cost",
            "net_profit",
            "adjusted_net_profit",
            "interest_expense",
        }
    ),
    StatementType.CASH_FLOW: frozenset(
        {
            "operating_cash_flow", "investing_cash_flow", "financing_cash_flow",
            "cash_exchange_effect", "net_cash_change", "capital_expenditure",
        }
    ),
}
_FLATTENED_BOUNDARIES = (
    "母公司资产负债表", "母公司利润表", "母公司现金流量表",
    "银行资产负债表", "银行利润表", "银行现金流量表",
    "合并所有者权益变动表", "母公司所有者权益变动表",
    "合并股东权益变动表", "财务报表附注",
)


@dataclass(frozen=True, slots=True)
class _FlattenedStatementState:
    statement_type: StatementType
    unit: tuple[str, Decimal]


class _PdfPage(Protocol):
    def extract_text(self) -> str | None: ...


class _PdfDocument(Protocol):
    pages: list[_PdfPage]


@dataclass(frozen=True, slots=True)
class PdfFactCandidate:
    canonical_fact_name: str
    value: Decimal
    unit_multiplier: Decimal
    currency: str
    page_number: int
    statement_type: StatementType
    source_text_hash: str
    source_priority: int = 0


@dataclass(frozen=True, slots=True)
class PdfExtractionResult:
    candidates: tuple[PdfFactCandidate, ...]
    facts: dict[str, Decimal]
    quality_status: QualityStatus
    issues: tuple[str, ...]
    parser_version: str
    pdf_content_hash: str
    derivations: tuple[tuple[str, tuple[str, ...]], ...] = ()


class CninfoPdfExtractor:
    def __init__(
        self,
        *,
        parser_version: str,
        reader_factory: Callable[[Path], _PdfDocument] = PdfReader,
        ocr_page_text: Callable[[_PdfPage, int], str | None] | None = None,
        ocr_cache_root: Path | None = None,
    ) -> None:
        if not parser_version.strip():
            raise ValueError("PDF_PARSER_VERSION_INVALID")
        self.parser_version = parser_version
        self.reader_factory = reader_factory
        self.ocr_page_text = ocr_page_text or _rapidocr_page_text
        self.ocr_cache_root = ocr_cache_root

    def extract(
        self,
        *,
        pdf_path: Path,
        descriptor: FilingDescriptor,
    ) -> PdfExtractionResult:
        try:
            payload = pdf_path.read_bytes()
        except OSError as error:
            raise ValueError("CNINFO_PDF_PARSE_FAILED") from error
        pdf_content_hash = hashlib.sha256(payload).hexdigest()
        if pdf_content_hash != descriptor.raw_object_hash:
            raise ValueError("PDF_CONTENT_HASH_MISMATCH")
        try:
            reader = self.reader_factory(pdf_path)
            if isinstance(reader, PdfReader):
                restore_declared_cmaps(reader)
            pages = [
                (
                    _physical_page_number(page, index),
                    page.extract_text(),
                )
                for index, page in enumerate(reader.pages, start=1)
            ]
        except Exception as error:
            raise ValueError("CNINFO_PDF_PARSE_FAILED") from error
        primary = self._parse_pages(
            pages,
            descriptor=descriptor,
            pdf_content_hash=pdf_content_hash,
        )
        if "PDF_IMAGE_ONLY" in primary.issues:
            ocr_indexes = _image_only_statement_indexes(pages)
            fully_scanned = not any(
                text and text.strip() for _page_number, text in pages
            )
            if fully_scanned:
                ocr_indexes = {
                    index for index, (_page_number, text) in enumerate(pages) if not text
                }
                ordered_ocr_indexes = _fully_scanned_ocr_order(
                    len(pages),
                    descriptor.report_type,
                )
            else:
                ordered_ocr_indexes = sorted(ocr_indexes)
            ocr_pages = list(pages)
            replacements = 0
            for index in ordered_ocr_indexes:
                if index not in ocr_indexes:
                    continue
                page_number = pages[index][0]
                ocr_text = self._cached_ocr_page_text(
                    reader.pages[index],
                    page_number=page_number,
                    pdf_content_hash=pdf_content_hash,
                )
                if ocr_text and ocr_text.strip():
                    ocr_pages[index] = (page_number, ocr_text)
                    replacements += 1
            if replacements:
                # Validate the complete selected scan, not an earlier valid prefix:
                # a later statement can introduce a conflict or a correction.
                return self._parse_pages(
                    ocr_pages,
                    descriptor=descriptor,
                    pdf_content_hash=pdf_content_hash,
                )
        if (
            primary.quality_status is QualityStatus.VALID
            or "PDF_CASH_FLOW_EQUATION_FAILED" not in primary.issues
        ):
            return primary
        cash_markers = (
            "现金流量表",
            *(
                alias
                for alias, canonical_name in _ALIASES.items()
                if canonical_name in _CASH_FLOW_RECONCILIATION
            ),
        )
        layout_indexes: set[int] = set()
        for index, (_page_number, text) in enumerate(pages):
            normalized_text = re.sub(r"\s+", "", text or "")
            if any(marker in normalized_text for marker in cash_markers):
                layout_indexes.update(
                    candidate_index
                    for candidate_index in (index - 1, index, index + 1)
                    if 0 <= candidate_index < len(pages)
                )
        if not layout_indexes:
            return primary
        try:
            layout_pages = list(pages)
            for index in sorted(layout_indexes):
                page = reader.pages[index]
                layout_pages[index] = (
                    _physical_page_number(page, index + 1),
                    page.extract_text(extraction_mode="layout"),
                )
        except (AttributeError, TypeError, ValueError):
            return primary
        if layout_pages == pages:
            return primary
        layout = self._parse_pages(
            layout_pages,
            descriptor=descriptor,
            pdf_content_hash=pdf_content_hash,
        )
        primary_score = _extraction_score(primary)
        layout_score = _extraction_score(layout)
        return layout if layout_score > primary_score else primary

    def _cached_ocr_page_text(
        self,
        page: _PdfPage,
        *,
        page_number: int,
        pdf_content_hash: str,
    ) -> str | None:
        cache_path = (
            self.ocr_cache_root
            / pdf_content_hash
            / f"rapidocr-v2-page-{page_number}.txt"
            if self.ocr_cache_root is not None
            else None
        )
        if cache_path is not None and cache_path.is_file():
            try:
                return cache_path.read_text(encoding="utf-8") or None
            except OSError:
                pass
        text = self.ocr_page_text(page, page_number)
        if not text or cache_path is None:
            return text
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{cache_path.name}.",
                dir=cache_path.parent,
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, cache_path)
            finally:
                temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        return text

    def _parse_pages(
        self,
        pages: list[tuple[int, str | None]],
        *,
        descriptor: FilingDescriptor,
        pdf_content_hash: str,
    ) -> PdfExtractionResult:
        issues: set[str] = set()
        visible_text = [text for _page_number, text in pages if text and text.strip()]
        if not visible_text:
            return self._result((), {"PDF_IMAGE_ONLY"}, pdf_content_hash)
        image_only_statement_block = _has_image_only_statement_block(pages)
        joined = "\n".join(visible_text)
        exchange_suffix = descriptor.ts_code.rpartition(".")[2]
        cover_labeled_codes = _a_share_labeled_codes(visible_text[0], exchange_suffix)
        if descriptor.ts_code in cover_labeled_codes and all(
            code == descriptor.ts_code or _is_secondary_share_class_code(code)
            for code in cover_labeled_codes
        ):
            cover_labeled_codes = {descriptor.ts_code}
        cover_codes = cover_labeled_codes or set(_SUFFIXED_CODE.findall(visible_text[0]))
        labeled_codes = _a_share_labeled_codes(joined, exchange_suffix)
        front_matter_labeled_codes = _a_share_labeled_codes(
            "\n".join(
                text for page_number, text in pages if page_number <= 20 and text and text.strip()
            ),
            exchange_suffix,
        )
        if descriptor.ts_code in front_matter_labeled_codes and all(
            code == descriptor.ts_code or _is_secondary_share_class_code(code)
            for code in front_matter_labeled_codes
        ):
            front_matter_labeled_codes = {descriptor.ts_code}
        codes = (
            cover_codes
            or _company_profile_codes(pages, exchange_suffix)
            or front_matter_labeled_codes
            or labeled_codes
            or set(_SUFFIXED_CODE.findall(joined))
        )
        front_matter_text = "\n".join(
            text for page_number, text in pages if page_number <= 20 and text and text.strip()
        )
        identity_matches = codes == {descriptor.ts_code} or (
            not codes
            and _issuer_name_matches(front_matter_text, descriptor.issuer_name)
        )
        periods = set(_REPORT_PERIOD.findall(joined))
        report_markers = _REPORT_TYPE_MARKERS[descriptor.report_type]
        if (
            not identity_matches
            or not _period_matches(joined, periods, descriptor)
            or not any(marker in joined for marker in report_markers)
        ):
            issues.add("PDF_LAYOUT_UNSUPPORTED")

        candidates: list[PdfFactCandidate] = []
        candidates.extend(_cross_page_split_fact_candidates(pages))
        if descriptor.report_type is ReportType.ANNUAL:
            candidates.extend(_finance_note_interest_expense_candidates(pages))
        fact_names_without_supported_unit: set[str] = set()
        statement_type: StatementType | None = None
        statement_title: str | None = None
        pending_statement: tuple[str, StatementType] | None = None
        pending_statement_lines = 0
        pending_statement_unit: tuple[str, Decimal] | None = None
        unit: tuple[str, Decimal] | None = None
        last_formal_unit: tuple[str, Decimal] | None = None
        pending_label = ""
        annual_summary_spillover = False
        noncurrent_liability_section_markers: list[tuple[int, str]] = []
        seen_debt_component_labels: set[str] = set()
        cash_flow_component_markers: dict[str, tuple[int, str]] = {}
        single_value_cash_component_hashes: set[str] = set()
        flattened_state: _FlattenedStatementState | None = None
        flattened_complete = False
        formal_statements_complete = False
        formal_statements_started = False
        formal_cash_flow_started = False
        for page_number, text in pages:
            if not text:
                continue
            raw_lines = text.splitlines()
            split_value_statement = next(
                (
                    title
                    for raw_line in raw_lines
                    if (title := _formal_statement_title(_normalized_heading(raw_line)))
                    is not None
                    and title[0]
                    in {*_CONSOLIDATED_STATEMENT_TITLES, *_COMBINED_STATEMENT_TITLES}
                ),
                None,
            )
            split_value_unit = _page_unit(raw_lines)
            if split_value_statement is not None and split_value_unit is not None:
                candidates.extend(
                    _split_decimal_value_candidates(
                        raw_lines,
                        page_number=page_number,
                        statement_type=split_value_statement[1],
                        unit=split_value_unit,
                    )
                )
            (
                flattened_candidates,
                flattened_state,
                flattened_noncurrent_marker,
                flattened_complete,
            ) = _flattened_page_candidates(
                raw_lines,
                page_number=page_number,
                active_state=flattened_state,
                extraction_complete=flattened_complete,
            )
            candidates.extend(flattened_candidates)
            if flattened_noncurrent_marker is not None:
                noncurrent_liability_section_markers.append(flattened_noncurrent_marker)
            if page_number <= 20 or not formal_statements_started:
                candidates.extend(
                    _flattened_summary_candidates(raw_lines, page_number=page_number)
                )
            garbled_bank_candidates: list[PdfFactCandidate] = []
            garbled_bank_statement: tuple[str, StatementType] | None = None
            garbled_cash_candidates: list[PdfFactCandidate] = []
            garbled_cash_statement: tuple[str, StatementType] | None = None
            if not formal_statements_complete:
                (
                    garbled_cash_candidates,
                    garbled_cash_statement,
                ) = _garbled_two_column_cash_flow_candidates(
                    raw_lines,
                    page_number=page_number,
                    active_statement_title=statement_title,
                )
                (
                    garbled_bank_candidates,
                    garbled_bank_statement,
                ) = _garbled_bank_statement_candidates(
                    raw_lines,
                    page_number=page_number,
                )
                candidates.extend(garbled_cash_candidates)
                candidates.extend(garbled_bank_candidates)
            embedded_statement = (
                None
                if formal_statements_complete
                else (
                    _embedded_consolidated_statement(raw_lines)
                    or garbled_cash_statement
                    or garbled_bank_statement
                )
            )
            if embedded_statement is not None:
                previous_unit = unit
                statement_title, statement_type = embedded_statement
                formal_statements_started = True
                formal_cash_flow_started = (
                    formal_cash_flow_started
                    or statement_type is StatementType.CASH_FLOW
                )
                pending_statement = None
                pending_statement_lines = 0
                pending_statement_unit = None
                page_unit = _page_unit(raw_lines)
                unit = (
                    previous_unit or last_formal_unit
                    if page_unit is None
                    else page_unit
                )
                if unit is not None:
                    last_formal_unit = unit
                pending_label = ""
                annual_summary_spillover = False
                if (
                    statement_type is StatementType.CASH_FLOW
                    and unit is not None
                    and (recovered_capex := _reordered_capital_expenditure(raw_lines)) is not None
                ):
                    currency, multiplier = unit
                    raw_value, number = recovered_capex
                    candidates.append(
                        PdfFactCandidate(
                            canonical_fact_name="capital_expenditure",
                            value=abs(number) * multiplier,
                            unit_multiplier=multiplier,
                            currency=currency,
                            page_number=page_number,
                            statement_type=statement_type,
                            source_text_hash=hashlib.sha256(raw_value.encode("utf-8")).hexdigest(),
                        )
                    )
            for raw_line in raw_lines:
                line = raw_line.strip()
                normalized_heading = _normalized_heading(line)
                stripped_heading = normalized_heading.removesuffix("（续）").removesuffix(
                    "(续)"
                )
                if (
                    any(
                        stripped_heading.endswith(boundary)
                        for boundary in _FORMAL_STATEMENT_END_BOUNDARIES
                    )
                    or (
                        formal_cash_flow_started
                        and any(
                            stripped_heading.endswith(boundary)
                            for boundary in _POST_CASH_FLOW_END_BOUNDARIES
                        )
                    )
                ) and formal_statements_started:
                    formal_statements_complete = True
                    statement_title = None
                    statement_type = None
                    pending_statement = None
                    pending_statement_lines = 0
                    pending_statement_unit = None
                    unit = None
                    pending_label = ""
                    annual_summary_spillover = False
                    continue
                if formal_statements_complete:
                    continue
                if (
                    embedded_statement is not None
                    and (
                        normalized_heading in _EMBEDDED_CONSOLIDATED_TITLES
                        or _qualified_plain_statement_heading(normalized_heading)
                        is not None
                    )
                ):
                    continue
                if (
                    statement_type is StatementType.BALANCE_SHEET
                    and normalized_heading.rstrip("：:") == "非流动负债"
                ):
                    noncurrent_liability_section_markers.append(
                        (
                            page_number,
                            hashlib.sha256(line.encode("utf-8")).hexdigest(),
                        )
                    )
                if (
                    _is_extraction_boundary(normalized_heading)
                    or _period_prefixed_parent_statement(normalized_heading)
                ):
                    statement_title = None
                    statement_type = None
                    pending_statement = None
                    pending_statement_lines = 0
                    pending_statement_unit = None
                    unit = None
                    pending_label = ""
                    annual_summary_spillover = False
                    continue
                title = _formal_statement_title(normalized_heading)
                if title is not None:
                    if statement_title == title[0] and statement_type is title[1]:
                        pending_label = ""
                        continue
                    statement_title = None
                    statement_type = None
                    unit = None
                    pending_label = ""
                    annual_summary_spillover = False
                    if title[0] in _CONSOLIDATED_STATEMENT_TITLES:
                        pending_statement = title
                        pending_statement_lines = 4
                        inline_unit_match = _UNIT.search(line)
                        pending_statement_unit = (
                            _UNIT_DEFINITIONS[inline_unit_match.group(1)]
                            if inline_unit_match is not None
                            else None
                        )
                        heading_period = normalized_heading.partition(title[0])[0]
                        if _statement_period_heading_matches(
                            heading_period, descriptor=descriptor, statement_type=title[1],
                        ):
                            statement_title, statement_type = title
                            formal_statements_started = True
                            formal_cash_flow_started = (
                                formal_cash_flow_started
                                or statement_type is StatementType.CASH_FLOW
                            )
                            pending_statement = None
                            pending_statement_lines = 0
                            unit = pending_statement_unit
                            pending_statement_unit = None
                    else:
                        statement_title = title[0]
                        statement_type = title[1]
                        if title[0] not in {
                            "主要财务数据",
                            "主要会计数据",
                            "主要会计数据和财务指标",
                            "主要会计数据及财务指标",
                        }:
                            formal_statements_started = True
                            formal_cash_flow_started = (
                                formal_cash_flow_started
                                or statement_type is StatementType.CASH_FLOW
                            )
                        pending_statement = None
                        pending_statement_lines = 0
                        pending_statement_unit = None
                    continue
                if page_number <= 20 and _annual_summary_header_matches(
                    line,
                    descriptor=descriptor,
                ):
                    statement_title = (
                        "\u4e3b\u8981\u4f1a\u8ba1\u6570\u636e\u548c\u8d22\u52a1\u6307\u6807"
                    )
                    statement_type = StatementType.INCOME_STATEMENT
                    pending_label = ""
                    annual_summary_spillover = True
                    continue
                if (
                    descriptor.report_type is ReportType.ANNUAL
                    and page_number <= 100
                    and _annual_adjusted_profit_header_matches(
                        line,
                        descriptor=descriptor,
                    )
                ):
                    statement_title = "主要财务指标"
                    statement_type = StatementType.INCOME_STATEMENT
                    unit = ("CNY", Decimal(1))
                    pending_label = ""
                    continue
                if pending_statement is not None:
                    if _statement_period_heading_matches(
                        line,
                        descriptor=descriptor,
                        statement_type=pending_statement[1],
                    ):
                        statement_title, statement_type = pending_statement
                        formal_statements_started = True
                        formal_cash_flow_started = (
                            formal_cash_flow_started
                            or statement_type is StatementType.CASH_FLOW
                        )
                        inline_unit_match = _UNIT.search(line)
                        bare_unit_match = _BARE_CNY_UNIT.search(line)
                        unit = pending_statement_unit or (
                            _UNIT_DEFINITIONS[inline_unit_match.group(1)]
                            if inline_unit_match is not None
                            else (
                                _UNIT_DEFINITIONS[bare_unit_match.group(1)]
                                if bare_unit_match is not None
                                else last_formal_unit
                            )
                        )
                        pending_statement = None
                        pending_statement_lines = 0
                        pending_statement_unit = None
                    elif (pending_unit_match := _UNIT.search(line)) is not None:
                        pending_statement_unit = _UNIT_DEFINITIONS[pending_unit_match.group(1)]
                    elif (pending_bare_unit_match := _BARE_CNY_UNIT.search(line)) is not None:
                        pending_statement_unit = _UNIT_DEFINITIONS[
                            pending_bare_unit_match.group(1)
                        ]
                    elif pending_statement_unit is not None and _statement_table_header_matches(
                        line,
                        descriptor=descriptor,
                        statement_type=pending_statement[1],
                    ):
                        statement_title, statement_type = pending_statement
                        formal_statements_started = True
                        formal_cash_flow_started = (
                            formal_cash_flow_started
                            or statement_type is StatementType.CASH_FLOW
                        )
                        if unit is not None:
                            last_formal_unit = unit
                        unit = pending_statement_unit
                        last_formal_unit = unit
                        pending_statement = None
                        pending_statement_lines = 0
                        pending_statement_unit = None
                    elif line:
                        pending_statement_lines -= 1
                        if pending_statement_lines <= 0:
                            pending_statement = None
                            pending_statement_unit = None
                    continue
                if statement_title == "主要财务数据" and re.match(
                    r"^[（(][二三四五六七八九十]+[）)]",
                    line,
                ):
                    statement_title = None
                    statement_type = None
                    unit = None
                    pending_label = ""
                    continue
                if statement_type is StatementType.INCOME_STATEMENT:
                    normalized_income_line = _normalize_interleaved_income_header(
                        line, descriptor=descriptor,
                    )
                    if normalized_income_line is None:
                        issues.add("PDF_LAYOUT_UNSUPPORTED")
                        statement_title = None
                        statement_type = None
                        pending_label = ""
                        continue
                    line = normalized_income_line
                standalone_inline_unit = _INLINE_CNY_UNIT.fullmatch(line.strip())
                if standalone_inline_unit is not None and _ALIASES.get(pending_label):
                    unit = _UNIT_DEFINITIONS[standalone_inline_unit.group(1)]
                    continue
                unit_match = _UNIT.search(line)
                bare_unit_match = (
                    _BARE_CNY_UNIT.search(line)
                    if unit_match is None and formal_statements_started
                    else (_BARE_CNY_UNIT.match(line.strip()) if unit_match is None else None)
                )
                if unit_match is not None or bare_unit_match is not None:
                    unit_name = (
                        unit_match.group(1)
                        if unit_match is not None
                        else bare_unit_match.group(1)
                    )
                    unit = _UNIT_DEFINITIONS[unit_name]
                    if formal_statements_started:
                        last_formal_unit = unit
                    if not pending_label or re.search(
                        _ACCOUNTING_NUMBER,
                        line[
                            (
                                unit_match.end()
                                if unit_match is not None
                                else bare_unit_match.end()
                            ) :
                        ],
                    ) is None:
                        pending_label = ""
                        continue

                if statement_type is StatementType.BALANCE_SHEET:
                    normalized_line = _normalize_label(line)
                    seen_debt_component_labels.update(
                        canonical_name
                        for alias, canonical_name in _ALIASES.items()
                        if canonical_name in _INTEREST_BEARING_DEBT_COMPONENTS
                        and alias in normalized_line
                    )

                if statement_type is StatementType.CASH_FLOW:
                    combined_cash_label = _normalize_label(f"{pending_label}{line}")
                    seen_cash_component = _ALIASES.get(combined_cash_label)
                    if seen_cash_component in _CASH_FLOW_RECONCILIATION:
                        cash_flow_component_markers.setdefault(
                            seen_cash_component,
                            (
                                page_number,
                                hashlib.sha256(
                                    combined_cash_label.encode("utf-8")
                                ).hexdigest(),
                            ),
                        )

                blank_debt_component = _blank_debt_component(line)
                if (
                    blank_debt_component is not None
                    and statement_type is StatementType.BALANCE_SHEET
                    and unit is not None
                ):
                    currency, multiplier = unit
                    if currency != "CNY":
                        fact_names_without_supported_unit.add(blank_debt_component)
                        continue
                    candidates.append(
                        PdfFactCandidate(
                            canonical_fact_name=blank_debt_component,
                            value=Decimal(0),
                            unit_multiplier=multiplier,
                            currency=currency,
                            page_number=page_number,
                            statement_type=statement_type,
                            source_text_hash=hashlib.sha256(
                                raw_line.strip().encode("utf-8")
                            ).hexdigest(),
                        )
                    )
                    pending_label = ""
                    continue

                blank_cash_flow_component = _blank_cash_flow_component(line)
                if (
                    blank_cash_flow_component is not None
                    and statement_type is StatementType.CASH_FLOW
                    and unit is not None
                ):
                    currency, multiplier = unit
                    if currency != "CNY":
                        fact_names_without_supported_unit.add(blank_cash_flow_component)
                        continue
                    candidates.append(
                        PdfFactCandidate(
                            canonical_fact_name=blank_cash_flow_component,
                            value=Decimal(0),
                            unit_multiplier=multiplier,
                            currency=currency,
                            page_number=page_number,
                            statement_type=statement_type,
                            source_text_hash=hashlib.sha256(
                                raw_line.strip().encode("utf-8")
                            ).hexdigest(),
                        )
                    )
                    pending_label = ""
                    continue

                negative_value_pending = pending_label.endswith(
                    _NEGATIVE_VALUE_SENTINEL
                )
                effective_pending_label = pending_label.removesuffix(
                    _NEGATIVE_VALUE_SENTINEL
                )
                fact_source_line = line
                parsed_line = (
                    _parse_combined_bank_group_fact(line)
                    if statement_title in _COMBINED_STATEMENT_TITLES
                    else None
                )
                if pending_label:
                    fact_source_line = (
                        f"{effective_pending_label} -{line.lstrip()}"
                        if negative_value_pending
                        else f"{effective_pending_label} {line}"
                    )
                    parsed_line = (
                        _parse_combined_bank_group_fact(fact_source_line)
                        if statement_title in _COMBINED_STATEMENT_TITLES
                        else _parse_fact_line(fact_source_line)
                    )
                if parsed_line is None:
                    fact_source_line = line
                    parsed_line = _parse_fact_line(line)
                if parsed_line is None and statement_type is StatementType.CASH_FLOW:
                    parsed_line = _parse_truncated_cash_exchange(line)
                    if parsed_line is None:
                        parsed_line = _parse_truncated_capital_expenditure(line)
                    if parsed_line is not None:
                        fact_source_line = line
                if parsed_line is None and statement_type is StatementType.BALANCE_SHEET:
                    parsed_line = _parse_trailing_balance_fact(line)
                    if parsed_line is not None:
                        fact_source_line = line[line.rfind(
                            "负债合计"
                            if parsed_line[0] == "total_liabilities"
                            else "股本"
                        ) :]
                if (
                    parsed_line is None
                    and descriptor.report_type is ReportType.ANNUAL
                    and page_number <= 100
                    and "股份总数" in line
                ):
                    parsed_line = _parse_share_change_total(line)
                    if parsed_line is not None:
                        candidates.append(
                            PdfFactCandidate(
                                canonical_fact_name="total_shares",
                                value=parsed_line[1],
                                unit_multiplier=Decimal(1),
                                currency="SHARES",
                                page_number=page_number,
                                statement_type=StatementType.BALANCE_SHEET,
                                source_text_hash=hashlib.sha256(
                                    raw_line.strip().encode("utf-8")
                                ).hexdigest(),
                                source_priority=30,
                            )
                        )
                        pending_label = ""
                        continue
                if parsed_line is None:
                    if (
                        pending_label
                        and not negative_value_pending
                        and line in {"-", "−", "–", "—"}
                        and _ALIASES.get(_normalize_label(pending_label))
                        in _CASH_FLOW_RECONCILIATION
                    ):
                        pending_label = f"{pending_label}{_NEGATIVE_VALUE_SENTINEL}"
                        continue
                    trailing_negative = re.fullmatch(
                        r"(?P<label>.+?)\s+[-−–—]",
                        line,
                    )
                    if (
                        trailing_negative is not None
                        and _ALIASES.get(
                            _normalize_label(trailing_negative.group("label"))
                        )
                        in _CASH_FLOW_RECONCILIATION
                    ):
                        pending_label = (
                            f"{trailing_negative.group('label')}"
                            f"{_NEGATIVE_VALUE_SENTINEL}"
                        )
                        continue
                    combined = _normalize_label(f"{pending_label}{line}")
                    combined_blank_cash = _blank_cash_flow_component(combined)
                    if (
                        pending_label
                        and combined_blank_cash is not None
                        and statement_type is StatementType.CASH_FLOW
                        and unit is not None
                        and not re.search(_ACCOUNTING_NUMBER, line)
                    ):
                        currency, multiplier = unit
                        candidates.append(
                            PdfFactCandidate(
                                canonical_fact_name=combined_blank_cash,
                                value=Decimal(0),
                                unit_multiplier=multiplier,
                                currency=currency,
                                page_number=page_number,
                                statement_type=statement_type,
                                source_text_hash=hashlib.sha256(
                                    combined.encode("utf-8")
                                ).hexdigest(),
                            )
                        )
                        pending_label = ""
                        continue
                    normalized_line = _normalize_label(line)
                    pending_label = (
                        combined
                        if _could_be_alias_prefix(combined)
                        else (
                            normalized_line
                            if _could_be_alias_prefix(normalized_line)
                            else (_trailing_capital_expenditure_prefix(line) or "")
                        )
                    )
                    continue
                pending_label = ""
                canonical_name, number = parsed_line
                if canonical_name == "capital_expenditure":
                    number = abs(number)
                if (
                    statement_title
                    in {
                        "主要财务数据",
                        "主要会计数据",
                        "主要会计数据和财务指标",
                        "主要会计数据及财务指标",
                    }
                    and canonical_name != "adjusted_net_profit"
                ):
                    continue
                if statement_type is None:
                    continue
                if unit is None:
                    if (
                        canonical_name == "adjusted_net_profit"
                        and statement_title
                        in {
                            "主要财务数据",
                            "主要会计数据",
                            "主要会计数据和财务指标",
                            "主要会计数据及财务指标",
                        }
                        and (inline_unit_match := _INLINE_CNY_UNIT.search(fact_source_line))
                    ):
                        currency, multiplier = _UNIT_DEFINITIONS[inline_unit_match.group(1)]
                    else:
                        fact_names_without_supported_unit.add(canonical_name)
                        continue
                else:
                    currency, multiplier = unit
                if canonical_name == "total_shares":
                    if statement_type is not StatementType.BALANCE_SHEET:
                        continue
                    currency, multiplier = "SHARES", multiplier
                elif currency != "CNY":
                    fact_names_without_supported_unit.add(canonical_name)
                    continue
                candidate = PdfFactCandidate(
                        canonical_fact_name=canonical_name,
                        value=number * multiplier,
                        unit_multiplier=multiplier,
                        currency=currency,
                        page_number=page_number,
                        statement_type=statement_type,
                        source_text_hash=hashlib.sha256(
                            raw_line.strip().encode("utf-8")
                        ).hexdigest(),
                        source_priority=_formal_fact_priority(
                            canonical_name,
                            fact_source_line,
                            statement_title=statement_title,
                        ),
                    )
                candidates.append(candidate)
                if (
                    canonical_name in _CASH_FLOW_RECONCILIATION
                    and canonical_name != "net_cash_change"
                    and len(re.findall(_ACCOUNTING_NUMBER, fact_source_line)) == 1
                ):
                    single_value_cash_component_hashes.add(candidate.source_text_hash)
                if annual_summary_spillover and canonical_name == "adjusted_net_profit":
                    annual_summary_spillover = False
        facts: dict[str, Decimal] = {}
        grouped: dict[str, list[PdfFactCandidate]] = {}
        for candidate in candidates:
            grouped.setdefault(candidate.canonical_fact_name, []).append(candidate)
        for name, name_candidates in grouped.items():
            highest_priority = max(candidate.source_priority for candidate in name_candidates)
            name_candidates = [
                candidate
                for candidate in name_candidates
                if candidate.source_priority == highest_priority
            ]
            if name == "adjusted_net_profit" and descriptor.report_type is ReportType.ANNUAL:
                summary_candidates = [
                    candidate for candidate in name_candidates if candidate.page_number <= 20
                ]
                if summary_candidates:
                    name_candidates = summary_candidates[:1]
            if name in {
                "interest_expense",
                "net_profit",
                "capital_expenditure",
                *_CASH_FLOW_RECONCILIATION,
            }:
                earliest_page = min(candidate.page_number for candidate in name_candidates)
                name_candidates = [
                    candidate
                    for candidate in name_candidates
                    if candidate.page_number == earliest_page
                ][:1]
            values = {candidate.value for candidate in name_candidates}
            if len(values) != 1:
                issues.add("PDF_FACT_CONFLICT")
                continue
            facts[name] = name_candidates[0].value

        derivations: list[tuple[str, tuple[str, ...]]] = []
        if "equity" not in facts and {"total_assets", "total_liabilities"}.issubset(facts):
            component_names = ("total_assets", "total_liabilities")
            component_candidates = [grouped[name][0] for name in component_names]
            combined_hash = hashlib.sha256(
                "\n".join(
                    f"{candidate.canonical_fact_name}:{candidate.source_text_hash}"
                    for candidate in component_candidates
                ).encode("utf-8")
            ).hexdigest()
            equity_candidate = PdfFactCandidate(
                canonical_fact_name="equity",
                value=facts["total_assets"] - facts["total_liabilities"],
                unit_multiplier=Decimal(1),
                currency="CNY",
                page_number=min(candidate.page_number for candidate in component_candidates),
                statement_type=StatementType.BALANCE_SHEET,
                source_text_hash=combined_hash,
            )
            candidates.append(equity_candidate)
            facts["equity"] = equity_candidate.value
            derivations.append(("equity", component_names))
        missing_debt_components = _INTEREST_BEARING_DEBT_COMPONENTS - facts.keys()
        if noncurrent_liability_section_markers and _balance_reconciles(facts):
            page_number, section_hash = noncurrent_liability_section_markers[0]
            omitted_components = (
                missing_debt_components - seen_debt_component_labels
            )
            for component_name in sorted(omitted_components):
                omitted_candidate = PdfFactCandidate(
                    canonical_fact_name=component_name,
                    value=Decimal(0),
                    unit_multiplier=Decimal(1),
                    currency="CNY",
                    page_number=page_number,
                    statement_type=StatementType.BALANCE_SHEET,
                    source_text_hash=section_hash,
                )
                candidates.append(omitted_candidate)
                grouped[component_name] = [omitted_candidate]
                facts[component_name] = Decimal(0)
                derivations.append(
                    (
                        component_name,
                        (
                            "complete_reconciled_balance_sheet",
                            "component_row_omitted",
                        ),
                    )
                )
        if "interest_bearing_debt" not in facts and _INTEREST_BEARING_DEBT_COMPONENTS.issubset(
            facts
        ):
            component_names = tuple(sorted(_INTEREST_BEARING_DEBT_COMPONENTS))
            component_candidates = [grouped[name][0] for name in component_names]
            combined_hash = hashlib.sha256(
                "\n".join(
                    f"{candidate.canonical_fact_name}:{candidate.source_text_hash}"
                    for candidate in component_candidates
                ).encode("utf-8")
            ).hexdigest()
            debt_value = sum(
                (facts[name] for name in component_names),
                Decimal(0),
            )
            derived_candidate = PdfFactCandidate(
                canonical_fact_name="interest_bearing_debt",
                value=debt_value,
                unit_multiplier=Decimal(1),
                currency="CNY",
                page_number=min(candidate.page_number for candidate in component_candidates),
                statement_type=StatementType.BALANCE_SHEET,
                source_text_hash=combined_hash,
            )
            candidates.append(derived_candidate)
            facts["interest_bearing_debt"] = debt_value
            derivations.append(("interest_bearing_debt", component_names))

        missing_cash_components = set(_CASH_FLOW_RECONCILIATION) - facts.keys()
        if (
            len(missing_cash_components) == 1
            and "net_cash_change" in facts
        ):
            missing_cash_component = missing_cash_components.pop()
            flow_components = {
                "operating_cash_flow",
                "investing_cash_flow",
                "financing_cash_flow",
                "cash_exchange_effect",
            }
            other_flow_components = flow_components - {missing_cash_component}
            if (
                missing_cash_component in flow_components
                and (
                    missing_cash_component in cash_flow_component_markers
                    or missing_cash_component == "cash_exchange_effect"
                )
                and other_flow_components.issubset(facts)
                and _cash_flow_values_reconcile(
                    sum((facts[name] for name in other_flow_components), Decimal(0)),
                    facts["net_cash_change"],
                )
            ):
                marker = cash_flow_component_markers.get(missing_cash_component)
                if marker is None:
                    net_candidate = grouped["net_cash_change"][0]
                    marker = (
                        net_candidate.page_number,
                        net_candidate.source_text_hash,
                    )
                page_number, source_hash = marker
                zero_candidate = PdfFactCandidate(
                    canonical_fact_name=missing_cash_component,
                    value=Decimal(0),
                    unit_multiplier=Decimal(1),
                    currency="CNY",
                    page_number=page_number,
                    statement_type=StatementType.CASH_FLOW,
                    source_text_hash=source_hash,
                )
                candidates.append(zero_candidate)
                grouped[missing_cash_component] = [zero_candidate]
                facts[missing_cash_component] = Decimal(0)
                derivations.append(
                    (
                        missing_cash_component,
                        (
                            (
                                "visible_blank_component_row"
                                if missing_cash_component in cash_flow_component_markers
                                else "component_row_omitted"
                            ),
                            "cash_flow_equation",
                        ),
                    )
                )

        if set(_CASH_FLOW_RECONCILIATION).issubset(facts):
            flow_components = _CASH_FLOW_RECONCILIATION[:-1]
            calculated = sum((facts[name] for name in flow_components), Decimal(0))
            comparative_only = []
            for name in flow_components:
                selected = next(
                    (
                        candidate
                        for candidate in grouped.get(name, [])
                        if candidate.value == facts[name]
                        and candidate.source_text_hash
                        in single_value_cash_component_hashes
                    ),
                    None,
                )
                if (
                    selected is not None
                    and facts[name] != 0
                    and _cash_flow_values_reconcile(
                        calculated - facts[name],
                        facts["net_cash_change"],
                    )
                ):
                    comparative_only.append((name, selected))
            if len(comparative_only) == 1:
                name, selected = comparative_only[0]
                zero_candidate = PdfFactCandidate(
                    canonical_fact_name=name,
                    value=Decimal(0),
                    unit_multiplier=selected.unit_multiplier,
                    currency="CNY",
                    page_number=selected.page_number,
                    statement_type=StatementType.CASH_FLOW,
                    source_text_hash=selected.source_text_hash,
                    source_priority=selected.source_priority + 1,
                )
                candidates.append(zero_candidate)
                grouped[name] = [zero_candidate]
                facts[name] = Decimal(0)
                derivations.append(
                    (
                        name,
                        ("comparative_only_value", "cash_flow_equation"),
                    )
                )

        if fact_names_without_supported_unit - facts.keys():
            issues.add("PDF_LAYOUT_UNSUPPORTED")
        required_facts = (
            _BANK_REQUIRED_FACTS
            if _is_financial_institution_report(joined, issuer_name=descriptor.issuer_name)
            else (
                _Q1_REQUIRED_FACTS
                if descriptor.report_type is ReportType.Q1
                else CANONICAL_PILOT_FACTS
            )
        )
        if not required_facts.issubset(facts):
            issues.add("PDF_REQUIRED_FACTS_MISSING")
        self._validate_balance(facts, issues)
        self._validate_cash_flow(facts, issues)
        if image_only_statement_block and not required_facts.issubset(facts):
            issues = {"PDF_IMAGE_ONLY"}
        return self._result(
            tuple(candidates),
            issues,
            pdf_content_hash,
            facts,
            tuple(derivations),
        )

    def build_document(
        self,
        *,
        filing_id: str,
        descriptor: FilingDescriptor,
        extracted: PdfExtractionResult,
        supersedes_id: str | None,
    ):
        from hengce.services.financial_resolution import (
            FinancialDocument,
            FinancialFactLineage,
        )

        lineage = {}
        for name, value in extracted.facts.items():
            candidate = next(
                candidate
                for candidate in extracted.candidates
                if candidate.canonical_fact_name == name and candidate.value == value
            )
            lineage[name] = FinancialFactLineage(
                page_number=candidate.page_number,
                source_text_hash=candidate.source_text_hash,
                unit_multiplier=str(candidate.unit_multiplier),
                currency=candidate.currency,
                parser_version=extracted.parser_version,
                pdf_content_hash=extracted.pdf_content_hash,
            )
        normalization_metadata = {
            "parser_version": extracted.parser_version,
            "pdf_content_hash": extracted.pdf_content_hash,
            "report_period": descriptor.report_period.isoformat(),
            "report_type": descriptor.report_type.value,
        }
        derivations = dict(extracted.derivations)
        for component_name in sorted(_INTEREST_BEARING_DEBT_COMPONENTS & derivations.keys()):
            normalization_metadata[f"{component_name}_derivation_version"] = (
                _OMITTED_DEBT_COMPONENT_DERIVATION_VERSION
            )
        if "interest_bearing_debt" in derivations:
            normalization_metadata["interest_bearing_debt_derivation_version"] = (
                _INTEREST_BEARING_DEBT_DERIVATION_VERSION
            )
            normalization_metadata["interest_bearing_debt_components"] = ",".join(
                derivations["interest_bearing_debt"]
            )
        return FinancialDocument(
            filing_id=filing_id,
            ts_code=descriptor.ts_code,
            source_id=descriptor.source_id,
            source_kind="PDF",
            source_url=descriptor.source_url,
            published_at=descriptor.published_at,
            valid_from=descriptor.collected_at,
            version=(f"pdf-{extracted.pdf_content_hash}:{extracted.parser_version}"),
            supersedes_id=supersedes_id,
            quality_status=extracted.quality_status,
            facts=extracted.facts,
            normalization_metadata=normalization_metadata,
            fact_lineage=lineage,
        )

    @staticmethod
    def _validate_balance(
        facts: dict[str, Decimal],
        issues: set[str],
    ) -> None:
        try:
            assets = facts["total_assets"]
            difference = abs(assets - facts["total_liabilities"] - facts["equity"])
        except KeyError:
            return
        if difference > max(Decimal(1), abs(assets) * Decimal("0.000001")):
            issues.add("PDF_BALANCE_EQUATION_FAILED")

    @staticmethod
    def _validate_cash_flow(
        facts: dict[str, Decimal],
        issues: set[str],
    ) -> None:
        if not set(_CASH_FLOW_RECONCILIATION).issubset(facts):
            issues.add("PDF_CASH_FLOW_EQUATION_FAILED")
            return
        calculated = sum(
            (
                facts["operating_cash_flow"],
                facts["investing_cash_flow"],
                facts["financing_cash_flow"],
                facts["cash_exchange_effect"],
            ),
            Decimal(0),
        )
        net_change = facts["net_cash_change"]
        tolerance = max(Decimal(1), abs(net_change) * Decimal("0.000001"))
        if abs(calculated - net_change) > tolerance:
            issues.add("PDF_CASH_FLOW_EQUATION_FAILED")

    def _result(
        self,
        candidates: tuple[PdfFactCandidate, ...],
        issues: set[str],
        pdf_content_hash: str,
        facts: dict[str, Decimal] | None = None,
        derivations: tuple[tuple[str, tuple[str, ...]], ...] = (),
    ) -> PdfExtractionResult:
        ordered_issues = tuple(sorted(issues))
        return PdfExtractionResult(
            candidates=tuple(
                sorted(
                    candidates,
                    key=lambda candidate: (
                        candidate.canonical_fact_name,
                        candidate.page_number,
                        candidate.source_text_hash,
                    ),
                )
            ),
            facts=dict(sorted((facts or {}).items())),
            quality_status=(
                QualityStatus.VALID if not ordered_issues else QualityStatus.UNVERIFIED
            ),
            issues=ordered_issues,
            parser_version=self.parser_version,
            pdf_content_hash=pdf_content_hash,
            derivations=derivations,
        )


def _normalize_interleaved_income_header(
    line: str, *, descriptor: FilingDescriptor,
) -> str | None:
    """Separate a total-revenue row from OCR-interleaved column headings.

    Both column years must match the filing. Never substitute the component
    '其中：营业收入' for the consolidated total.
    """
    if descriptor.report_type is not ReportType.ANNUAL:
        return line
    year = descriptor.report_period.year
    match = re.fullmatch(
        rf"营业总收入\s+项目\s+(?:国\s+)?附注{_CHINESE_NUMERAL}\s+"
        rf"(?P<current>{_ACCOUNTING_NUMBER})\s+(?P<year>\d{{4}})年度\s+"
        rf"{_ACCOUNTING_NUMBER}\s+(?P<prior_year>\d{{4}})年度", line,
    )
    if match is not None and (
        int(match.group("year")) != year or int(match.group("prior_year")) != year - 1
    ):
        return None
    return f"营业总收入 | {match.group('current')}" if match is not None else line


def _parse_fact_line(line: str) -> tuple[str, Decimal] | None:
    label, separator, raw_value = line.partition("|")
    if not separator:
        label, separator, raw_value = line.partition("｜")
    if not separator:
        match = _NOTE_COLUMN_FACT.fullmatch(line.strip())
        if match is None:
            match = _SLASH_NOTE_COLUMN_FACT.fullmatch(line.strip())
        if match is None:
            match = _SPLIT_NUMERIC_NOTE_COLUMN_FACT.fullmatch(line.strip())
        if match is None:
            match = _NUMERIC_NOTE_COLUMN_FACT.fullmatch(line.strip())
        if match is None:
            match = _WHITESPACE_FACT.fullmatch(line.strip())
        if match is None:
            return None
        label = match.group("label")
        raw_value = match.group("value")
    label = _normalize_label(label)
    raw_value = raw_value.strip()
    canonical_name = _canonical_name(label)
    if canonical_name is None or not _NUMBER.fullmatch(raw_value):
        return None
    number = _accounting_decimal(raw_value)
    return (canonical_name, number) if number is not None else None


def _parse_combined_bank_group_fact(line: str) -> tuple[str, Decimal] | None:
    """Take the current group value from group/bank four-column statements."""
    value_matches = list(re.finditer(_ACCOUNTING_NUMBER, line))
    if len(value_matches) < 4:
        return None
    label = _normalize_label(line[: value_matches[0].start()])
    note_prefix_stripped = False
    canonical_name = _canonical_name(label)
    if canonical_name is None:
        stripped_label = re.sub(
            rf"{_CHINESE_NUMERAL}(?:、|[-－—]|[（(])?$",
            "",
            label,
        )
        note_prefix_stripped = stripped_label != label
        label = stripped_label
        canonical_name = _canonical_name(label)
    if canonical_name is None:
        return None
    value_index = (
        1
        if len(value_matches) >= 5
        and (
            note_prefix_stripped
            or re.fullmatch(
                r"\d{1,3}(?:\.\d+)?",
                value_matches[0].group(0).strip(),
            )
            is not None
        )
        else 0
    )
    value = _accounting_decimal(value_matches[value_index].group(0))
    return (canonical_name, value) if value is not None else None


def _flattened_page_candidates(
    raw_lines: list[str],
    *,
    page_number: int,
    active_state: _FlattenedStatementState | None,
    extraction_complete: bool,
) -> tuple[
    list[PdfFactCandidate],
    _FlattenedStatementState | None,
    tuple[int, str] | None,
    bool,
]:
    """Parse PDFs whose extraction layer flattens an entire table into one line."""
    if extraction_complete:
        return [], None, None, True
    if len(raw_lines) > 10 or not raw_lines or max(map(len, raw_lines)) < 80:
        return [], active_state, None, False
    text = re.sub(r"\s+", " ", " ".join(raw_lines)).strip()
    events: list[tuple[int, int, StatementType | None]] = []
    for title in _CONSOLIDATED_STATEMENT_TITLES:
        start = text.find(title)
        if start >= 0:
            events.append((start, start + len(title), _STATEMENT_TITLES[title]))
    for title in _FLATTENED_BOUNDARIES:
        start = text.find(title)
        if start >= 0:
            events.append((start, start + len(title), None))
    events.sort(key=lambda item: (item[0], item[2] is None))

    candidates: list[PdfFactCandidate] = []
    noncurrent_marker = None
    state = active_state
    cursor = 0
    for start, end, next_type in events:
        if state is not None and start > cursor:
            segment = text[cursor:start]
            candidates.extend(
                _flattened_segment_candidates(segment, page_number=page_number, state=state)
            )
            if state.statement_type is StatementType.BALANCE_SHEET and "非流动负债" in segment:
                noncurrent_marker = (
                    page_number,
                    hashlib.sha256(segment.encode("utf-8")).hexdigest(),
                )
        if next_type is None:
            if state is not None and state.statement_type is StatementType.CASH_FLOW:
                extraction_complete = True
            state = None
        else:
            unit = _page_unit([text])
            state = (
                _FlattenedStatementState(next_type, unit)
                if unit is not None and unit[0] == "CNY"
                else None
            )
        cursor = end
    if state is not None and cursor < len(text):
        segment = text[cursor:]
        candidates.extend(
            _flattened_segment_candidates(segment, page_number=page_number, state=state)
        )
        if state.statement_type is StatementType.BALANCE_SHEET and "非流动负债" in segment:
            noncurrent_marker = (
                page_number,
                hashlib.sha256(segment.encode("utf-8")).hexdigest(),
            )
    return candidates, state, noncurrent_marker, extraction_complete


def _flattened_segment_candidates(
    segment: str,
    *,
    page_number: int,
    state: _FlattenedStatementState,
) -> list[PdfFactCandidate]:
    candidates: list[PdfFactCandidate] = []
    allowed = _FLATTENED_FACTS_BY_STATEMENT[state.statement_type]
    currency, multiplier = state.unit
    for alias in sorted(_ALIASES, key=len, reverse=True):
        canonical_name = _ALIASES[alias]
        if canonical_name not in allowed:
            continue
        match = _flattened_alias_match(segment, alias)
        if match is None:
            continue
        number = _accounting_decimal(match.group("value"))
        if number is None:
            continue
        if canonical_name == "capital_expenditure":
            number = abs(number)
        candidate_currency = "SHARES" if canonical_name == "total_shares" else currency
        candidates.append(
            PdfFactCandidate(
                canonical_fact_name=canonical_name,
                value=number * multiplier,
                unit_multiplier=multiplier,
                currency=candidate_currency,
                page_number=page_number,
                statement_type=state.statement_type,
                source_text_hash=hashlib.sha256(match.group(0).encode("utf-8")).hexdigest(),
                source_priority=20,
            )
        )
    return candidates


def _flattened_summary_candidates(
    raw_lines: list[str],
    *,
    page_number: int,
) -> list[PdfFactCandidate]:
    if not raw_lines:
        return []
    text = re.sub(r"\s+", " ", " ".join(raw_lines)).strip()
    change_reason_boundaries = (
        "主要会计数据、财务指标发生变动的情况、原因",
        "主要会计数据、财务指标发生变动的情况及原因",
    )
    boundary_indexes = [
        index
        for boundary in change_reason_boundaries
        if (index := text.find(boundary)) >= 0
    ]
    if boundary_indexes:
        text = text[: min(boundary_indexes)]
    summary_heading_indexes = [
        index
        for heading in ("主要会计数据", "主要财务数据")
        if (index := text.find(heading)) >= 0
    ]
    has_summary_heading = bool(summary_heading_indexes)
    if summary_heading_indexes:
        text = text[min(summary_heading_indexes) :]
    aliases = tuple(
        alias
        for alias, canonical_name in _ALIASES.items()
        if canonical_name == "adjusted_net_profit"
    )
    unit = _page_unit(raw_lines)
    for alias in aliases:
        if not has_summary_heading and alias in {
            "扣除非经常性损益后归属于本行股东的净利润",
            "扣除非经常性损益后的净利润",
        }:
            continue
        match = _flattened_alias_match(text, alias, allow_inline_unit=True)
        if match is None:
            continue
        number = _accounting_decimal(match.group("value"))
        if number is None:
            continue
        inline_unit = _INLINE_CNY_UNIT.search(match.group(0))
        if inline_unit is not None:
            currency, multiplier = _UNIT_DEFINITIONS[inline_unit.group(1)]
        elif unit is not None and unit[0] == "CNY":
            currency, multiplier = unit
        else:
            continue
        return [
            PdfFactCandidate(
                canonical_fact_name="adjusted_net_profit",
                value=number * multiplier,
                unit_multiplier=multiplier,
                currency=currency,
                page_number=page_number,
                statement_type=StatementType.INCOME_STATEMENT,
                source_text_hash=hashlib.sha256(match.group(0).encode("utf-8")).hexdigest(),
                source_priority=20,
            )
        ]
    return []


def _finance_note_interest_expense_candidates(
    pages: list[tuple[int, str | None]],
) -> list[PdfFactCandidate]:
    """Recover annual interest expense from the audited finance-cost note."""
    labels = (
        "贷款及应付款项的利息支出",
        "贷款及应付款项利息支出",
    )
    active_unit: tuple[str, Decimal] | None = None
    for page_number, text in pages:
        if not text:
            continue
        raw_lines = text.splitlines()
        active_unit = _page_unit(raw_lines) or active_unit
        normalized_page = "".join(_normalized_heading(line) for line in raw_lines)
        if (
            active_unit is None
            or active_unit[0] != "CNY"
            or "财务费用" not in normalized_page
        ):
            continue
        for raw_line in raw_lines:
            normalized_line = _normalize_label(raw_line)
            label = next(
                (candidate for candidate in labels if normalized_line.startswith(candidate)),
                None,
            )
            if label is None:
                continue
            match = _flattened_alias_match(raw_line, label)
            if match is None:
                continue
            value = _accounting_decimal(match.group("value"))
            if value is None:
                continue
            currency, multiplier = active_unit
            return [
                PdfFactCandidate(
                    canonical_fact_name="interest_expense",
                    value=abs(value) * multiplier,
                    unit_multiplier=multiplier,
                    currency=currency,
                    page_number=page_number,
                    statement_type=StatementType.INCOME_STATEMENT,
                    source_text_hash=hashlib.sha256(
                        raw_line.strip().encode("utf-8")
                    ).hexdigest(),
                    source_priority=-20,
                )
            ]
    return []


def _split_decimal_value_candidates(
    raw_lines: list[str],
    *,
    page_number: int,
    statement_type: StatementType,
    unit: tuple[str, Decimal],
) -> list[PdfFactCandidate]:
    """Rejoin a current-period decimal split onto the following PDF text line."""
    currency, multiplier = unit
    if currency != "CNY":
        return []
    candidates: list[PdfFactCandidate] = []
    for index, raw_line in enumerate(raw_lines[:-1]):
        match = re.fullmatch(
            rf"(?P<prefix>.+?\s)(?P<whole>{_GROUPED_INTEGER})\.",
            raw_line.strip(),
        )
        decimal_match = re.fullmatch(r"(?P<decimal>\d{1,4})", raw_lines[index + 1].strip())
        if match is None or decimal_match is None:
            continue
        reconstructed = (
            f"{match.group('prefix')}{match.group('whole')}."
            f"{decimal_match.group('decimal')}"
        )
        parsed = _parse_fact_line(reconstructed)
        if parsed is None:
            continue
        canonical_name, value = parsed
        if canonical_name not in _FLATTENED_FACTS_BY_STATEMENT[statement_type]:
            continue
        candidates.append(
            PdfFactCandidate(
                canonical_fact_name=canonical_name,
                value=(abs(value) if canonical_name == "capital_expenditure" else value)
                * multiplier,
                unit_multiplier=multiplier,
                currency="SHARES" if canonical_name == "total_shares" else currency,
                page_number=page_number,
                statement_type=statement_type,
                source_text_hash=hashlib.sha256(
                    "\n".join(raw_lines[index : index + 2]).encode("utf-8")
                ).hexdigest(),
                source_priority=35,
            )
        )
    return candidates


def _cross_page_split_fact_candidates(
    pages: list[tuple[int, str | None]],
) -> list[PdfFactCandidate]:
    """Recover a row whose value precedes a label suffix on the next page."""
    visible_pages = [
        (page_number, (text or "").splitlines())
        for page_number, text in pages
        if text
    ]
    quarterly_data_pages = {
        page_number
        for page_number, lines in visible_pages
        if any(
            "分季度主要财务数据" in _normalized_heading(line)
            or "分季度主要财务指标" in _normalized_heading(line)
            for line in lines
        )
    }
    flattened_lines = [
        (page_number, line)
        for page_number, lines in visible_pages
        for line in lines
    ]
    units: dict[int, tuple[str, Decimal] | None] = {}
    active_unit: tuple[str, Decimal] | None = None
    for page_number, lines in visible_pages:
        active_unit = _page_unit(lines) or active_unit
        units[page_number] = active_unit
    candidates: list[PdfFactCandidate] = []
    for index, (page_number, raw_line) in enumerate(flattened_lines):
        if page_number in quarterly_data_pages:
            continue
        match = _NOTE_COLUMN_FACT.fullmatch(raw_line.strip())
        if match is None:
            match = _WHITESPACE_FACT.fullmatch(raw_line.strip())
        if match is None:
            continue
        prefix = _normalize_label(match.group("label"))
        alias_and_fact = next(
            (
                (alias, canonical_name)
                for alias, canonical_name in _ALIASES.items()
                if alias != prefix and alias.startswith(prefix) and len(prefix) >= 4
            ),
            None,
        )
        if alias_and_fact is None:
            continue
        alias, canonical_name = alias_and_fact
        remainder = alias.removeprefix(prefix)
        matched_suffix = ""
        source_lines = [raw_line.strip()]
        for _next_page, next_line in flattened_lines[index + 1 : index + 9]:
            normalized = _normalize_label(next_line)
            if not normalized or not remainder.startswith(matched_suffix + normalized):
                continue
            matched_suffix += normalized
            source_lines.append(next_line.strip())
            if matched_suffix == remainder:
                break
        if matched_suffix != remainder:
            continue
        number = _accounting_decimal(match.group("value"))
        unit = units.get(page_number)
        if number is None or unit is None or unit[0] != "CNY":
            continue
        currency, multiplier = unit
        statement_type = next(
            statement
            for statement, allowed_facts in _FLATTENED_FACTS_BY_STATEMENT.items()
            if canonical_name in allowed_facts
        )
        if canonical_name == "capital_expenditure":
            number = abs(number)
        if canonical_name == "total_shares":
            currency = "SHARES"
        source_text = "\n".join(source_lines)
        candidates.append(
            PdfFactCandidate(
                canonical_fact_name=canonical_name,
                value=number * multiplier,
                unit_multiplier=multiplier,
                currency=currency,
                page_number=page_number,
                statement_type=statement_type,
                source_text_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                source_priority=(
                    20
                    if canonical_name == "adjusted_net_profit" and page_number <= 20
                    else -10
                ),
            )
        )
    return candidates


def _flattened_alias_match(
    text: str,
    alias: str,
    *,
    allow_inline_unit: bool = False,
) -> re.Match[str] | None:
    alias_pattern = r"\s*".join(re.escape(character) for character in alias)
    prefix = rf"(?<![\u4e00-\u9fffA-Za-z]){alias_pattern}"
    qualifier = r"(?:[（(][^（）()]{0,80}[）)])?"
    unit = (
        r"(?:[（(](?:人民币)?(?:元|千元|万元|百万元|亿元)[）)])?"
        if allow_inline_unit
        else ""
    )
    separator = r"\s*(?:[|｜]\s*)?"
    # A bare 'note' marker is distinguishable from a numbered note when the
    # following value is grouped in thousands; never consume that value as an ID.
    note = rf"(?:(?:{_NOTE_REFERENCE})\s+|注\s+(?=\d{{1,3}},\d{{3}}))?"
    return re.search(
        rf"{prefix}{qualifier}{unit}{separator}{note}(?P<value>{_ACCOUNTING_NUMBER})",
        text,
    )


def _accounting_decimal(raw_value: str) -> Decimal | None:
    normalized = re.sub(r"\s+", "", raw_value).replace(",", "")
    if "," in raw_value and re.match(r"^[(-]?0\d", normalized) is not None:
        # A grouped integer cannot start with a zero-filled leading group.
        # OCR commonly leaves this suffix after dropping the first digits.
        return None
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = f"-{normalized[1:-1]}"
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def _balance_reconciles(facts: dict[str, Decimal]) -> bool:
    try:
        assets = facts["total_assets"]
        difference = abs(assets - facts["total_liabilities"] - facts["equity"])
    except KeyError:
        return False
    return difference <= max(Decimal(1), abs(assets) * Decimal("0.000001"))


def _is_bank_report(text: str) -> bool:
    return "银行资产负债表" in text or (
        "吸收存款" in text and "发放贷款和垫款" in text
    )


def _is_financial_institution_report(text: str, *, issuer_name: str | None = None) -> bool:
    normalized_name = (issuer_name or "").replace("銀", "银")
    named_bank = (
        re.fullmatch(r".+银行(?:股份有限公司)?", normalized_name) is not None
        and _issuer_name_matches(text[:1000].replace("銀", "银"), normalized_name)
        and "客户存款" in text
        and "贷款和垫款" in text
    )
    return (
        named_bank or _is_bank_report(text)
        or "保险（集团）" in text or "保险(集团)" in text
        or (
            "保险合同负债" in text
            and ("保险服务收入" in text or "保险业务收入" in text)
        )
    )


def _parse_truncated_cash_exchange(line: str) -> tuple[str, Decimal] | None:
    match = _WHITESPACE_FACT.fullmatch(line.strip())
    if match is None:
        return None
    expected_label = "汇率变动对现金及现金等价物的影响"
    if _normalize_label(match.group("label")) != expected_label.removesuffix("影响"):
        return None
    parsed = _parse_fact_line(f"{expected_label} {match.group('value')}")
    if parsed is None or parsed[0] != "cash_exchange_effect":
        return None
    return parsed


def _parse_truncated_capital_expenditure(
    line: str,
) -> tuple[str, Decimal] | None:
    label, separator, raw_value = line.partition("|")
    if not separator:
        match = _WHITESPACE_FACT.fullmatch(line.strip())
        if match is None:
            return None
        label = match.group("label")
        raw_value = match.group("value")
    normalized_label = _normalize_label(label)
    full_label = _matching_capital_expenditure_label(normalized_label)
    if full_label is None:
        return None
    parsed = _parse_fact_line(f"{full_label} | {raw_value.strip()}")
    if parsed is None or parsed[0] != "capital_expenditure":
        return None
    return parsed


def _parse_trailing_balance_fact(line: str) -> tuple[str, Decimal] | None:
    """Parse the right-hand half of an assets/liabilities side-by-side table."""
    for alias in ("负债合计", "股本"):
        index = line.rfind(alias)
        if index <= 0:
            continue
        prefix = line[:index]
        if re.search(_ACCOUNTING_NUMBER, prefix) is None and not (
            alias == "股本" and "商誉" in prefix
        ):
            continue
        parsed = _parse_fact_line(line[index:])
        if parsed is not None:
            return parsed
    return None


def _matching_capital_expenditure_label(prefix: str) -> str | None:
    minimum = _normalize_label("购建固定资产、无形资产")
    if not prefix.startswith(minimum):
        return None
    labels = tuple(
        alias
        for alias, canonical_name in _ALIASES.items()
        if canonical_name == "capital_expenditure"
    )
    return next((label for label in labels if label.startswith(prefix)), None)


def _trailing_capital_expenditure_prefix(line: str) -> str | None:
    normalized = _normalize_label(line)
    marker = _normalize_label("购建固定资产、无形资产")
    start = normalized.rfind(marker)
    if start < 0:
        return None
    prefix = normalized[start:]
    return prefix if _matching_capital_expenditure_label(prefix) is not None else None


def _normalize_label(label: str) -> str:
    normalized = re.sub(r"\s+", "", label.strip())
    normalized = normalized.replace("╱", "/")
    normalized = re.sub(
        r"^(?:[一二三四五六七八九十]+、|[（(][一二三四五六七八九十]+[）)])",
        "",
        normalized,
    )
    if normalized.startswith("其中："):
        normalized = normalized.removeprefix("其中：")
    if normalized.startswith(("减：", "减:")):
        normalized = normalized[2:]
    return normalized


def _has_image_only_statement_block(
    pages: list[tuple[int, str | None]],
) -> bool:
    """Detect image-only statements between a text audit report and its notes."""
    return bool(_image_only_statement_indexes(pages))


def _image_only_statement_indexes(
    pages: list[tuple[int, str | None]],
) -> set[int]:
    """Return blank-page indexes for scanned statements bounded by audit/notes text."""
    indexes: set[int] = set()
    run_start: int | None = None
    for index in range(len(pages) + 1):
        text = pages[index][1] if index < len(pages) else "end"
        if index < len(pages) and not (text or "").strip():
            if run_start is None:
                run_start = index
            continue
        if run_start is None:
            continue
        run_length = index - run_start
        before = "\n".join(
            page_text or "" for _page_number, page_text in pages[max(0, run_start - 3) : run_start]
        )
        after = "\n".join(
            page_text or "" for _page_number, page_text in pages[index : index + 3]
        )
        if (
            run_length >= 4
            and "审计报告" in before
            and "财务报表附注" in re.sub(r"\s+", "", after)
        ):
            indexes.update(range(run_start, index))
        run_start = None
    return indexes


def _fully_scanned_ocr_order(
    page_count: int,
    report_type: ReportType,
) -> tuple[int, ...]:
    """Probe cover pages, then the likely financial-report tail, then the middle."""
    front_end = min(12, page_count)
    tail_start = (
        max(front_end, int(page_count * 0.4))
        if report_type is ReportType.ANNUAL
        else front_end
    )
    return tuple(
        [
            *range(front_end),
            *range(tail_start, page_count),
            *range(front_end, tail_start),
        ]
    )


@lru_cache(maxsize=1)
def _rapidocr_engine() -> object:
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)


def _rapidocr_page_text(page: _PdfPage, _page_number: int) -> str | None:
    """OCR the largest embedded scan and rebuild reading-order table rows."""
    try:
        rotation = int(page.get("/Rotate", 0)) % 360  # type: ignore[attr-defined]
        images = list(page.images)  # type: ignore[attr-defined]
        image = max(
            (item.image for item in images),
            key=lambda candidate: candidate.width * candidate.height,
        )
        if rotation:
            image = image.rotate(-rotation, expand=True)
        # Red seals can split a black amount into spurious note/value cells.
        # The red channel attenuates the seal while retaining the printed text;
        # only an in-memory OCR input is changed, never the source image/PDF.
        image = image.convert("RGB").getchannel("R").convert("RGB")
        result, _elapsed = _rapidocr_engine()(image)  # type: ignore[operator]
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        return None
    if not result:
        return None
    return _ocr_result_text(result)


def _ocr_result_text(result: object) -> str | None:
    """Normalize RapidOCR cells into deterministic top-to-bottom table rows."""
    cells: list[tuple[float, float, float, str]] = []
    for item in result:  # type: ignore[union-attr]
        try:
            box, text, confidence = item
            if not str(text).strip() or float(confidence) < 0.5:
                continue
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
        except (IndexError, TypeError, ValueError):
            continue
        cells.append(
            (
                min(xs),
                (min(ys) + max(ys)) / 2,
                max(1.0, max(ys) - min(ys)),
                str(text).strip(),
            )
        )
    rows: list[list[tuple[float, float, float, str]]] = []
    for candidate in sorted(cells, key=lambda entry: (entry[1], entry[0])):
        _x, center_y, height, _text = candidate
        if rows:
            row_center = sum(entry[1] for entry in rows[-1]) / len(rows[-1])
            row_height = max(entry[2] for entry in rows[-1])
            if abs(center_y - row_center) <= max(height, row_height) * 0.55:
                rows[-1].append(candidate)
                continue
        rows.append([candidate])
    lines = [
        " ".join(entry[3] for entry in sorted(row, key=lambda entry: entry[0]))
        for row in rows
    ]
    text = "\n".join(line for line in lines if line)
    return text or None


def _extraction_score(result: PdfExtractionResult) -> tuple[bool, bool, int, int]:
    return (
        result.quality_status is QualityStatus.VALID,
        "PDF_IMAGE_ONLY" not in result.issues,
        len(result.facts),
        -len(result.issues),
    )


def _formal_fact_priority(
    canonical_name: str,
    source_line: str,
    *,
    statement_title: str | None = None,
) -> int:
    priority = (
        20
        if statement_title in _CONSOLIDATED_STATEMENT_TITLES
        or statement_title in _COMBINED_STATEMENT_TITLES
        else 0
    )
    normalized = _normalize_label(source_line)
    if canonical_name == "revenue" and normalized.startswith("营业总收入"):
        return priority + 5
    if canonical_name == "interest_expense" and normalized.startswith("利息费用"):
        return priority + 5
    return priority


def _normalized_heading(line: str) -> str:
    normalized = re.sub(r"\s+", "", line.strip())
    normalized = re.sub(r"^§\d+", "", normalized)
    normalized = re.sub(r"^\d+(?:\.\d+)+", "", normalized)
    return re.sub(
        r"^(?:[（(]?[一二三四五六七八九十0-9]+[）)、.．])",
        "",
        normalized,
    )


def _formal_statement_title(
    normalized_heading: str,
) -> tuple[str, StatementType] | None:
    qualified_heading = normalized_heading
    for qualifier in ("未经审计", "经审计", "已审计"):
        qualified_heading = qualified_heading.removeprefix(qualifier)
    for title, statement_type in (
        *_STATEMENT_TITLES.items(),
        *_COMBINED_STATEMENT_TITLES.items(),
    ):
        if _statement_heading_matches(qualified_heading, title):
            return title, statement_type
    return None


def _is_extraction_boundary(normalized_heading: str) -> bool:
    heading = normalized_heading
    for qualifier in ("未经审计", "经审计", "已审计"):
        heading = heading.removeprefix(qualifier)
    heading = heading.removesuffix("（续）").removesuffix("(续)")
    return heading in _EXTRACTION_BOUNDARIES


def _period_prefixed_parent_statement(normalized_heading: str) -> bool:
    if re.match(
        r"^(?:\d{4}年度|\d{4}年\d{1,2}月\d{1,2}日)?公司"
        r"(?:资产负债表|利润表|现金流量表)", normalized_heading,
    ) is not None:
        return True
    return any(
        _statement_heading_matches(normalized_heading, title)
        for title in ("母公司资产负债表", "母公司利润表", "母公司现金流量表")
    )


def _statement_heading_matches(normalized_heading: str, title: str) -> bool:
    index = normalized_heading.find(title)
    if index < 0:
        return False
    prefix = normalized_heading[:index]
    suffix = normalized_heading[index + len(title):]
    period_prefix = re.compile(
        r"^(?:\d{4}年度|\d{4}年\d{1,2}月\d{1,2}日|"
        r"\d{4}年\d{1,2}(?:—|–|－|-)\d{1,2}月)?$"
    )
    return (not prefix or period_prefix.fullmatch(prefix) is not None) and (
        not suffix or suffix.startswith(("（", "("))
    )


def _embedded_consolidated_statement(
    raw_lines: list[str],
) -> tuple[str, StatementType] | None:
    """Recognize issuer PDFs whose visual heading is extracted after the table."""
    normalized_lines = [_normalized_heading(line) for line in raw_lines]
    share_unit = _page_unit(raw_lines)
    if "股本信息" in normalized_lines and share_unit is not None and share_unit[0] == "SHARES":
        return "股本信息", StatementType.BALANCE_SHEET
    combined = next(
        (
            (title, statement_type)
            for line in normalized_lines
            for title, statement_type in _COMBINED_STATEMENT_TITLES.items()
            if _embedded_combined_heading_matches(line, title)
        ),
        None,
    )
    if combined is not None:
        return combined
    normalized_page = "".join(normalized_lines)
    qualified_plain = next(
        (
            title
            for line in normalized_lines
            if (title := _qualified_plain_statement_heading(line)) is not None
        ),
        None,
    )
    if (
        qualified_plain is not None
        and "银行股份有限公司" in normalized_page
        and any(len(re.findall(_ACCOUNTING_NUMBER, line)) >= 4 for line in raw_lines)
    ):
        plain_title, statement_type = _EMBEDDED_CONSOLIDATED_TITLES[qualified_plain]
        return plain_title.replace("合并", "合并及银行"), statement_type
    if (
        _page_unit(raw_lines) is not None
        and "后附财务报表附注为本财务报表的组成部分" in normalized_page
        and "本集团" in normalized_page
        and ("本行" in normalized_page or "本银行" in normalized_page)
    ):
        structural_matches = [
            (
                "合并及银行现金流量表",
                StatementType.CASH_FLOW,
                sum(
                    marker in normalized_page
                    for marker in (
                        "经营活动产生的现金流量",
                        "投资活动产生的现金流量",
                        "筹资活动产生的现金流量",
                    )
                )
                >= 2,
            ),
            (
                "合并及银行利润表",
                StatementType.INCOME_STATEMENT,
                "营业收入" in normalized_page and "净利润" in normalized_page,
            ),
            (
                "合并及银行资产负债表",
                StatementType.BALANCE_SHEET,
                (
                    ("资产总计" in normalized_page or "资产合计" in normalized_page)
                    and "负债合计" in normalized_page
                ),
            ),
        ]
        inferred = [
            (title, statement_type)
            for title, statement_type, matched in structural_matches
            if matched
        ]
        if len(inferred) == 1:
            return inferred[0]
    if not any("合并数" in line for line in normalized_lines):
        return None
    matches = {
        title
        for line in normalized_lines
        for title in _EMBEDDED_CONSOLIDATED_TITLES
        if line == title or line == f"{title}（续）" or line == f"{title}(续)"
    }
    if len(matches) != 1:
        return None
    return _EMBEDDED_CONSOLIDATED_TITLES[matches.pop()]


def _qualified_plain_statement_heading(normalized_heading: str) -> str | None:
    heading = normalized_heading
    qualified = False
    for qualifier in ("未经审计", "经审计", "已审计"):
        if heading.startswith(qualifier):
            heading = heading.removeprefix(qualifier)
            qualified = True
            break
    heading = heading.removesuffix("（续）").removesuffix("(续)")
    return heading if qualified and heading in _EMBEDDED_CONSOLIDATED_TITLES else None


def _embedded_combined_heading_matches(normalized_heading: str, title: str) -> bool:
    heading = normalized_heading.removesuffix("（续）").removesuffix("(续)")
    if heading == title:
        return True
    prefix = heading.removesuffix(title)
    return heading.endswith(title) and prefix.endswith("股份有限公司")


def _page_unit(raw_lines: list[str]) -> tuple[str, Decimal] | None:
    for line in raw_lines:
        unit_match = _UNIT.search(line)
        if unit_match is not None:
            return _UNIT_DEFINITIONS[unit_match.group(1)]
        inline_unit_match = _INLINE_CNY_UNIT.search(line)
        if inline_unit_match is not None:
            return _UNIT_DEFINITIONS[inline_unit_match.group(1)]
        bare_unit_match = _BARE_CNY_UNIT.match(line.strip())
        if bare_unit_match is not None:
            return _UNIT_DEFINITIONS[bare_unit_match.group(1)]
    return None


def _garbled_two_column_cash_flow_candidates(
    raw_lines: list[str],
    *,
    page_number: int,
    active_statement_title: str | None,
) -> tuple[list[PdfFactCandidate], tuple[str, StatementType] | None]:
    """Recover a two-column cash-flow table with a broken embedded font map."""
    garbled_title = "\u0a40\u0a08\u0456"
    garbled_section = "\u0a40\u0a08"
    garbled_unit = "\u0ca6\u0af6\u043b\u03e4\u0ea3\u10ed"
    normalized_lines = [_normalized_heading(line) for line in raw_lines]
    is_title_page = garbled_title in normalized_lines and any(
        garbled_unit in line for line in normalized_lines
    )
    is_continuation = (
        active_statement_title == "合并现金流量表"
        and any(line.endswith(garbled_section) for line in normalized_lines)
        and "\u0673" in normalized_lines
    )
    if not is_title_page and not is_continuation:
        return [], None

    def candidate(canonical_name: str, line: str) -> PdfFactCandidate | None:
        value = _two_column_current_value(line)
        if value is None:
            return None
        return PdfFactCandidate(
            canonical_fact_name=canonical_name,
            value=value * Decimal(1_000_000),
            unit_multiplier=Decimal(1_000_000),
            currency="CNY",
            page_number=page_number,
            statement_type=StatementType.CASH_FLOW,
            source_text_hash=hashlib.sha256(line.strip().encode("utf-8")).hexdigest(),
            source_priority=30,
        )

    numeric_rows = [
        (index, line)
        for index, line in enumerate(raw_lines)
        if _two_column_current_value(line) is not None
    ]
    if is_title_page:
        section_indices = [
            index
            for index, line in enumerate(normalized_lines)
            if line.endswith(garbled_section)
        ]
        if len(section_indices) < 2:
            return [], None
        second_section = section_indices[1]
        operating_row = next(
            (line for index, line in reversed(numeric_rows) if index < second_section),
            None,
        )
        investing_row = numeric_rows[-1][1] if numeric_rows else None
        recovered = [
            item
            for item in (
                candidate("operating_cash_flow", operating_row)
                if operating_row is not None
                else None,
                candidate("investing_cash_flow", investing_row)
                if investing_row is not None
                else None,
            )
            if item is not None
        ]
        return recovered, ("合并现金流量表", StatementType.CASH_FLOW)

    if len(numeric_rows) < 5:
        return [], None
    tail_rows = [line for _, line in numeric_rows[-5:]]
    recovered = [
        item
        for item in (
            candidate("financing_cash_flow", tail_rows[0]),
            candidate("cash_exchange_effect", tail_rows[1]),
            candidate("net_cash_change", tail_rows[2]),
        )
        if item is not None
    ]
    return recovered, None


def _two_column_current_value(line: str) -> Decimal | None:
    matches = list(re.finditer(_ACCOUNTING_NUMBER, line))
    if len(matches) < 2:
        return None
    value_index = 0
    if len(matches) >= 3 and re.fullmatch(
        r"\d{1,3}(?:\.\d+)?", matches[0].group(0).strip()
    ):
        value_index = 1
    selected = matches[value_index]
    value = _accounting_decimal(selected.group(0))
    if (
        value is not None
        and not selected.group(0).lstrip().startswith("(")
        and line[selected.end() :].lstrip().startswith(")")
    ):
        return -abs(value)
    return value


def _garbled_bank_statement_candidates(
    raw_lines: list[str],
    *,
    page_number: int,
) -> tuple[list[PdfFactCandidate], tuple[str, StatementType] | None]:
    """Recover official bank tables whose embedded font corrupts Chinese totals."""
    unit = _page_unit(raw_lines)
    normalized_page = "".join(_normalized_heading(line) for line in raw_lines)
    if (
        unit is None
        or not raw_lines
        or re.fullmatch(r"\d{4}年(?:度|12月31日)", _normalized_heading(raw_lines[0]))
        is None
        or "后附财务报表附注为本财务报表的组成部分" not in normalized_page
    ):
        return [], None
    currency, multiplier = unit
    if currency != "CNY":
        return [], None

    def candidate(
        canonical_name: str,
        line: str,
        statement_type: StatementType,
        *,
        absolute: bool = False,
    ) -> PdfFactCandidate | None:
        value = _bank_group_current_value(line)
        if value is None:
            return None
        return PdfFactCandidate(
            canonical_fact_name=canonical_name,
            value=(abs(value) if absolute else value) * multiplier,
            unit_multiplier=multiplier,
            currency=currency,
            page_number=page_number,
            statement_type=statement_type,
            source_text_hash=hashlib.sha256(line.strip().encode("utf-8")).hexdigest(),
            source_priority=30,
        )

    footer_index = next(
        index
        for index, line in enumerate(raw_lines)
        if "后附财务报表附注为本财务报表的组成部分"
        in _normalized_heading(line)
    )
    group_rows = [
        (index, line)
        for index, line in enumerate(raw_lines[:footer_index])
        if _bank_group_current_value(line) is not None
    ]
    if not group_rows:
        return [], None

    def compact(*items: PdfFactCandidate | None) -> list[PdfFactCandidate]:
        return [item for item in items if item is not None]

    if (
        "现金及存放中央银行款项" in normalized_page
        and "发放贷款和垫款" in normalized_page
    ):
        return (
            compact(candidate("total_assets", group_rows[-1][1], StatementType.BALANCE_SHEET)),
            ("合并及银行资产负债表", StatementType.BALANCE_SHEET),
        )
    if "向中央银行借款" in normalized_page and "吸收存款" in normalized_page:
        return (
            compact(
                candidate(
                    "total_liabilities",
                    group_rows[-1][1],
                    StatementType.BALANCE_SHEET,
                )
            ),
            ("合并及银行资产负债表", StatementType.BALANCE_SHEET),
        )
    if "股本" in normalized_page and "少数股东权益" in normalized_page:
        minority_index = next(
            (
                index
                for index, line in enumerate(raw_lines)
                if "少数股东" in _normalize_label(line)
            ),
            None,
        )
        if minority_index is None:
            return [], None
        equity_row = next(
            (
                line
                for index, line in group_rows
                if index > minority_index
            ),
            None,
        )
        return (
            compact(candidate("equity", equity_row, StatementType.BALANCE_SHEET))
            if equity_row is not None
            else [],
            ("合并及银行资产负债表", StatementType.BALANCE_SHEET),
        )
    if "利息收入" in normalized_page and "归属于本行股东的净利润" in normalized_page:
        header_index = next(
            (
                index
                for index, line in enumerate(raw_lines)
                if "附注" in line and len(re.findall(_ACCOUNTING_NUMBER, line)) >= 2
            ),
            -1,
        )
        revenue_row = next(
            (line for index, line in group_rows if index > header_index),
            None,
        )
        attributable_index = next(
            (
                index
                for index, line in enumerate(raw_lines)
                if "归属于本行股东" in _normalize_label(line)
            ),
            None,
        )
        if attributable_index is None:
            return [], None
        net_profit_row = next(
            (
                line
                for index, line in reversed(group_rows)
                if index < attributable_index
            ),
            None,
        )
        return (
            compact(
                candidate("revenue", revenue_row, StatementType.INCOME_STATEMENT)
                if revenue_row is not None
                else None,
                candidate("net_profit", net_profit_row, StatementType.INCOME_STATEMENT)
                if net_profit_row is not None
                else None,
            ),
            ("合并及银行利润表", StatementType.INCOME_STATEMENT),
        )
    if "支付利息、手续费及佣金的现金" in normalized_page and "支付的各项税费" in normalized_page:
        return (
            compact(
                candidate(
                    "operating_cash_flow",
                    group_rows[-1][1],
                    StatementType.CASH_FLOW,
                )
            ),
            ("合并及银行现金流量表", StatementType.CASH_FLOW),
        )
    if "收回投资收到的现金" in normalized_page and "投资支付的现金" in normalized_page:
        capex_row = next(
            (
                line
                for index, line in enumerate(raw_lines)
                if index > 0
                and "购建固定资产" in _normalize_label(raw_lines[index - 1])
                and "支付的现金" in _normalize_label(line)
            ),
            None,
        )
        return (
            compact(
                candidate(
                    "investing_cash_flow",
                    group_rows[-1][1],
                    StatementType.CASH_FLOW,
                ),
                candidate(
                    "capital_expenditure",
                    capex_row,
                    StatementType.CASH_FLOW,
                    absolute=True,
                )
                if capex_row is not None
                else None,
            ),
            ("合并及银行现金流量表", StatementType.CASH_FLOW),
        )
    if "发行债券收到的现金" in normalized_page and "偿还债务支付的现金" in normalized_page:
        opening_index = next(
            (
                index
                for index, line in enumerate(raw_lines)
                if "年初现金及现金等价物余额" in _normalize_label(line)
            ),
            footer_index,
        )
        tail_rows = [line for index, line in group_rows if index < opening_index]
        if len(tail_rows) < 3:
            return [], ("合并及银行现金流量表", StatementType.CASH_FLOW)
        return (
            compact(
                candidate("financing_cash_flow", tail_rows[-3], StatementType.CASH_FLOW),
                candidate("cash_exchange_effect", tail_rows[-2], StatementType.CASH_FLOW),
                candidate("net_cash_change", tail_rows[-1], StatementType.CASH_FLOW),
            ),
            ("合并及银行现金流量表", StatementType.CASH_FLOW),
        )
    return [], None


def _bank_group_current_value(line: str) -> Decimal | None:
    matches = list(re.finditer(_ACCOUNTING_NUMBER, line))
    if len(matches) < 4:
        return None
    value_index = 0
    if len(matches) >= 5 and re.fullmatch(
        r"\d{1,3}(?:\.\d+)?", matches[0].group(0).strip()
    ):
        value_index = 1
    selected = matches[value_index]
    value = _accounting_decimal(selected.group(0))
    if (
        value is not None
        and not selected.group(0).lstrip().startswith("(")
        and line[selected.end() :].lstrip().startswith(")")
    ):
        return -abs(value)
    return value


def _reordered_capital_expenditure(
    raw_lines: list[str],
) -> tuple[str, Decimal] | None:
    """Recover a row when a PDF extracts its label at the bottom of the page."""
    normalized = [_normalize_label(line) for line in raw_lines]
    capex_labels = {
        _normalize_label("购建固定资产、无形资产和其他长期资产支付的现金"),
        _normalize_label("购建固定资产、无形资产和其他长期资产所支付的现金"),
    }
    if not capex_labels.intersection(normalized):
        return None
    for index, line in enumerate(normalized):
        if not line.startswith("投资支付的现金") or index == 0:
            continue
        raw_value = raw_lines[index - 1].strip()
        match = re.match(rf"^(?P<value>{_ACCOUNTING_NUMBER})(?:\s+.*)?$", raw_value)
        if match is not None:
            normalized_value = re.sub(r"\s+", "", match.group("value")).replace(",", "")
            if normalized_value.startswith("(") and normalized_value.endswith(")"):
                normalized_value = f"-{normalized_value[1:-1]}"
            return raw_value, Decimal(normalized_value)
    return None


def _parse_share_change_total(line: str) -> tuple[str, Decimal] | None:
    normalized = _normalize_label(line)
    if re.match(rf"^(?:{_CHINESE_NUMERAL}、)?股份总数", normalized) is None:
        return None
    raw_values = re.findall(_ACCOUNTING_NUMBER, line)
    if len(raw_values) < 3:
        return None
    current_total = raw_values[-2].replace(",", "")
    try:
        return "total_shares", Decimal(current_total)
    except InvalidOperation:
        return None


def _statement_period_heading_matches(
    line: str,
    *,
    descriptor: FilingDescriptor,
    statement_type: StatementType,
) -> bool:
    normalized = re.sub(r"\s+", "", line.strip())
    period = descriptor.report_period
    if statement_type is StatementType.BALANCE_SHEET:
        date_pattern = rf"{period.year}年0?{period.month}月0?{period.day}日"
        return re.fullmatch(
            rf"{date_pattern}(?:人民币(?:元|千元|万元|亿元))?",
            normalized,
        ) is not None or (
            normalized.startswith("项目") and re.search(date_pattern, normalized) is not None
        )
    if descriptor.report_type is ReportType.ANNUAL:
        if (
            re.fullmatch(
                rf"(?:{period.year}年度|(?:截至)?{period.year}年12月31日止年度)"
                r"(?:人民币(?:元|千元|万元|百万元|亿元))?",
                normalized,
            )
            is not None
        ):
            return True
    q1_pattern = rf"{period.year}年0?1(?:—|–|－|-|至)0?{period.month}月"
    if descriptor.report_type is ReportType.Q1 and re.search(
        q1_pattern,
        normalized,
    ) is not None:
        return True
    return re.fullmatch(q1_pattern, normalized) is not None


def _statement_table_header_matches(
    line: str,
    *,
    descriptor: FilingDescriptor,
    statement_type: StatementType,
) -> bool:
    normalized = re.sub(r"\s+", "", line.strip())
    normalized = normalized.replace("年年度", "年度")
    normalized = normalized.removesuffix("（调整后）").removesuffix("(调整后)")
    if statement_type is StatementType.BALANCE_SHEET:
        period = descriptor.report_period
        current_date = rf"{period.year}年0?{period.month}月0?{period.day}日"
        comparative_date = (
            rf"(?:{period.year}年0?1月0?1日|"
            rf"{period.year - 1}年0?12月0?31日)"
        )
        return (
            re.fullmatch(
                r"(?:项目|资产|负债和所有者权益)"
                rf"(?:附注(?:{_CHINESE_NUMERAL})?)?"
                r"期末余额(?:期初余额|年初余额|上年年末余额)",
                normalized,
            )
            is not None
            or re.fullmatch(
                rf"项目(?:附注(?:{_CHINESE_NUMERAL})?)?"
                rf"{current_date}{comparative_date}",
                normalized,
            )
            is not None
        )
    year = descriptor.report_period.year
    if descriptor.report_type is ReportType.ANNUAL:
        return (
            re.fullmatch(
                rf"项目(?:附注(?:{_CHINESE_NUMERAL})?)?{year}年度{year - 1}年度",
                normalized,
            )
            is not None
            or re.fullmatch(
                rf"项目(?:附注(?:{_CHINESE_NUMERAL})?)?"
                r"(?:本期金额|本期发生额)(?:上期金额|上期发生额)",
                normalized,
            )
            is not None
        )
    if descriptor.report_type is ReportType.Q1:
        return normalized in {
            "项目本期发生额上期发生额",
            "项目本期发生额上年同期发生额",
            "项目本期金额上期金额",
            f"项目{year}年第一季度{year - 1}年第一季度",
        }
    return False


def _annual_summary_header_matches(
    line: str,
    *,
    descriptor: FilingDescriptor,
) -> bool:
    if descriptor.report_type is not ReportType.ANNUAL:
        return False
    normalized = re.sub(r"\s+", "", line.strip())
    year = descriptor.report_period.year
    return (
        f"{year}\u5e74" in normalized
        and f"{year - 1}\u5e74" in normalized
        and f"{year - 2}\u5e74" in normalized
        and (
            "\u672c\u5e74\u6bd4\u4e0a\u5e74" in normalized
            or "\u540c\u6bd4\u589e\u51cf" in normalized
        )
    )


def _annual_adjusted_profit_header_matches(
    line: str,
    *,
    descriptor: FilingDescriptor,
) -> bool:
    normalized = re.sub(r"\s+", "", line.strip())
    year = descriptor.report_period.year
    return "主要指标" in normalized and f"{year}年" in normalized and f"{year - 1}年" in normalized


def _canonical_name(label: str) -> str | None:
    direct = _ALIASES.get(label)
    if direct is not None:
        return direct
    for alias in sorted(_ALIASES, key=len, reverse=True):
        if (
            label.startswith(f"{alias}（")
            or label.startswith(f"{alias}(")
            or label.startswith(f"{alias}/(")
        ):
            return _ALIASES[alias]
    return None


def _could_be_alias_prefix(label: str) -> bool:
    normalized = _normalize_label(label)
    return bool(normalized) and any(
        alias.startswith(normalized)
        or normalized.startswith(f"{alias}（")
        or normalized.startswith(f"{alias}(")
        for alias in _ALIASES
    )


def _blank_debt_component(line: str) -> str | None:
    raw_normalized = _normalize_label(line)
    normalized = re.sub(
        r"[-\u2014]+$",
        "",
        raw_normalized,
    )
    canonical = _ALIASES.get(normalized)
    if canonical is None and raw_normalized.endswith("/--"):
        canonical = _ALIASES.get(raw_normalized.removesuffix("/--"))
    if canonical is None:
        current_dash_with_prior = re.fullmatch(
            rf"(?P<label>.+?){_NOTE_REFERENCE}"
            r"[-\u2014]+[-+]?\(?[\d,]+(?:\.\d+)?\)?",
            normalized,
        )
        if current_dash_with_prior is not None:
            canonical = _ALIASES.get(current_dash_with_prior.group("label"))
    if canonical is None:
        note_only = re.fullmatch(
            rf"(?P<label>.+?){_NOTE_REFERENCE}",
            normalized,
        )
        if note_only is not None:
            canonical = _ALIASES.get(note_only.group("label"))
    if canonical in _INTEREST_BEARING_DEBT_COMPONENTS:
        return canonical
    return None


def _blank_cash_flow_component(line: str) -> str | None:
    canonical = None
    current_blank_with_prior = re.fullmatch(
        rf"(?P<label>.+?)\s{{2,}}{_ACCOUNTING_NUMBER}",
        line.strip(),
    )
    if current_blank_with_prior is not None:
        canonical = _ALIASES.get(
            _normalize_label(current_blank_with_prior.group("label"))
        )
    if canonical is None:
        current_dash_with_prior = re.fullmatch(
            rf"(?P<label>.+?)\s+[-\u2014]\s+{_ACCOUNTING_NUMBER}(?:\s+.*)?",
            line.strip(),
        )
        if current_dash_with_prior is not None:
            label = _normalize_label(current_dash_with_prior.group("label"))
            if label == "汇率变动对现金及现金等价物的":
                canonical = "cash_exchange_effect"
            else:
                canonical = _ALIASES.get(label)
    if canonical in _CASH_FLOW_RECONCILIATION:
        return canonical
    return None


def _cash_flow_values_reconcile(calculated: Decimal, net_change: Decimal) -> bool:
    tolerance = max(Decimal(1), abs(net_change) * Decimal("0.000001"))
    return abs(calculated - net_change) <= tolerance


def _period_matches(
    text: str,
    explicit_periods: set[str],
    descriptor: FilingDescriptor,
) -> bool:
    expected = descriptor.report_period.isoformat()
    if explicit_periods:
        return explicit_periods == {expected}
    period = descriptor.report_period
    if descriptor.report_type is ReportType.Q1:
        q1_title = re.compile(rf"{period.year}\s*年\s*第一季度报告")
        if q1_title.search(text) is not None:
            return True
    visible_date = re.compile(rf"{period.year}\s*年\s*0?{period.month}\s*月\s*0?{period.day}\s*日")
    return visible_date.search(text) is not None


def _company_profile_codes(
    pages: list[tuple[int, str | None]], default_suffix: str,
) -> set[str]:
    front_pages: list[str] = []
    for _number, text in pages:
        if not text:
            continue
        lines = text.splitlines()
        if _page_unit(lines) is not None and any(
            _normalized_heading(line) in _CONSOLIDATED_STATEMENT_TITLES for line in lines
        ):
            break
        front_pages.append(text)
    front = "\n".join(front_pages)
    explicit_a_codes = {
        f"{symbol}.{default_suffix}"
        for symbol in re.findall(r"(?m)^\s*A\s*股代[码碼]\s*[：:]\s*(\d{6})\b", front)
    }
    if explicit_a_codes:
        return explicit_a_codes
    stock_table_codes: set[str] = set()
    for stock_table in re.finditer(
        r"(?m)^[^\S\n]*股票种类[^\n]*股票上市交易所[^\n]*股票简称[^\n]*股票代码[^\n]*\n"
        r"(?P<rows>(?:[^\S\n]*[AH]\s*股[^\n]*\n?)+)", front,
    ):
        stock_table_codes.update(
            _a_share_labeled_codes(stock_table.group("rows"), default_suffix)
        )
    if stock_table_codes:
        return stock_table_codes
    # Match a real section heading, not a table-of-contents entry or a subsidiary
    # definition. Only the explicit A-share abbreviation row supplies the code.
    heading = re.search(
        r"(?m)^\s*(?:第[一二三四五六七八九十]+节\s*)?"
        r"公司简介(?:和主要财务指标|及主要财务指标)?\s*$", front,
    )
    if heading is None:
        return set()
    profile = front[heading.end():heading.end() + 5000]
    return {
        f"{symbol}.{default_suffix}"
        for symbol in re.findall(
            r"代[码碼]\s*[：:]\s*(\d{6})\s+A\s*股简[称稱]", profile,
        )
    }


def _a_share_labeled_codes(text: str, default_suffix: str) -> set[str]:
    codes: set[str] = set()
    for raw_symbol, explicit_suffix in _LABELED_CODE_DETAIL.findall(text):
        if explicit_suffix and explicit_suffix not in {"SH", "SZ"}:
            continue
        symbol = re.sub(r"\s+", "", raw_symbol)
        codes.add(f"{symbol}.{explicit_suffix or default_suffix}")
    exchange_name = {
        "SH": "\u4e0a\u6d77\u8bc1\u5238\u4ea4\u6613\u6240",
        "SZ": "\u6df1\u5733\u8bc1\u5238\u4ea4\u6613\u6240",
    }.get(default_suffix)
    if exchange_name is not None:
        for line in text.splitlines():
            normalized_line = re.sub(r"\s+", " ", line.strip())
            if (
                exchange_name in normalized_line
                and re.search(r"(?:^|\s)A\s*\u80a1(?:\s|$)", normalized_line) is not None
            ):
                codes.update(
                    f"{symbol}.{default_suffix}"
                    for symbol in re.findall(r"(?<!\d)\d{6}(?!\d)", normalized_line)
                )
    return codes


def _issuer_name_matches(cover_text: str, issuer_name: str | None) -> bool:
    if issuer_name is None:
        return False
    normalized_name = re.sub(r"\s+", "", issuer_name)
    if len(normalized_name) < 4:
        return False
    normalized_cover = re.sub(r"\s+", "", cover_text)
    return normalized_name in normalized_cover


def _is_secondary_share_class_code(ts_code: str) -> bool:
    symbol, _, exchange = ts_code.partition(".")
    common_a_share_prefixes = {
        "SH": ("600", "601", "603", "605", "688", "689"),
        "SZ": ("000", "001", "002", "003", "300", "301"),
    }
    prefixes = common_a_share_prefixes.get(exchange)
    return prefixes is not None and not symbol.startswith(prefixes)


def _physical_page_number(page: object, enumeration_index: int) -> int:
    declared = getattr(page, "page_number", None)
    if declared is None:
        return enumeration_index
    page_number = int(declared)
    return page_number + 1 if page_number == enumeration_index - 1 else page_number


__all__ = [
    "CninfoPdfExtractor",
    "PdfExtractionResult",
    "PdfFactCandidate",
]
