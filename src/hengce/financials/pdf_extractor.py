from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from pypdf import PdfReader

from hengce.contracts.enums import QualityStatus, ReportType, StatementType
from hengce.contracts.financial import FilingDescriptor
from hengce.financials.registry_loader import CANONICAL_PILOT_FACTS

_ACCOUNTING_NUMBER = r"(?:[-+]?[\d,]+(?:\.\d+)?|\(\s*[\d,]+(?:\.\d+)?\s*\))"
_NUMBER = re.compile(rf"^{_ACCOUNTING_NUMBER}$")
_CHINESE_NUMERAL = r"[一二三四五六七八九十]+"
_NOTE_REFERENCE = (
    rf"(?:注释\s*\d+|{_CHINESE_NUMERAL}(?:、|[-－—])\d+"
    rf"(?:[（(]\d+[）)])?[A-Za-z]?|"
    rf"{_CHINESE_NUMERAL}[（(]{_CHINESE_NUMERAL}[）)]\d+|"
    rf"{_CHINESE_NUMERAL}(?:[（(](?:\d+|[A-Za-z])[）)])+)"
)
_SUFFIXED_CODE = re.compile(r"\b[0-9]{6}\.(?:SH|SZ)\b")
_LABELED_CODE_DETAIL = re.compile(
    r"(?:证券代码(?:（A/H）|\(A/H\))?|股票代码|公司代码)\s*[：:]?\s*"
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
    r"单位(?:均为|为)?\s*[：:]?\s*(人民币元|人民币千元|人民币万元|人民币百万元|人民币亿元|元|千元|万元|百万元|亿元|千股|股)"
)
_INLINE_CNY_UNIT = re.compile(r"[（(](元|千元|万元|百万元|亿元)[）)]")
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
    "合并资产负债表": StatementType.BALANCE_SHEET,
    "合并利润表": StatementType.INCOME_STATEMENT,
    "合并现金流量表": StatementType.CASH_FLOW,
    "股本信息": StatementType.BALANCE_SHEET,
    "股份变动情况表": StatementType.BALANCE_SHEET,
}
_CONSOLIDATED_STATEMENT_TITLES = frozenset({"合并资产负债表", "合并利润表", "合并现金流量表"})
_COMBINED_STATEMENT_TITLES = {
    "合并及公司资产负债表": StatementType.BALANCE_SHEET,
    "合并及母公司资产负债表": StatementType.BALANCE_SHEET,
    "合并及公司利润表": StatementType.INCOME_STATEMENT,
    "合并及母公司利润表": StatementType.INCOME_STATEMENT,
    "合并及公司现金流量表": StatementType.CASH_FLOW,
    "合并及母公司现金流量表": StatementType.CASH_FLOW,
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
    "合并及公司股东权益变动表",
    "合并及母公司股东权益变动表",
    "合并及公司所有者权益变动表",
    "合并及母公司所有者权益变动表",
    "合并股东权益变动表",
    "合并所有者权益变动表",
)
_ALIASES = {
    "资产总计": "total_assets",
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
    "归属于上市公司股东的扣除非经常性损益的净利润": ("adjusted_net_profit"),
    "归属于上市公司普通股股东的扣除非经常性损益的净利润": ("adjusted_net_profit"),
    "利息费用": "interest_expense",
    "利息支出": "interest_expense",
    "经营活动产生的现金流量净额": "operating_cash_flow",
    "经营活动产生/(使用)的现金流量净额": "operating_cash_flow",
    "经营活动产生/（使用）的现金流量净额": "operating_cash_flow",
    "经营活动产生(使用)的现金流量净额": "operating_cash_flow",
    "经营活动产生（使用）的现金流量净额": "operating_cash_flow",
    "购建固定资产、无形资产和其他长期资产支付的现金": ("capital_expenditure"),
    "购建固定资产、无形资产和其他长期资产所支付的现金": ("capital_expenditure"),
    "购建固定资产、无形资产及其他长期资产支付的现金": ("capital_expenditure"),
    "购建固定资产、无形资产及其他长期资产所支付的现金": ("capital_expenditure"),
    "期末总股本": "total_shares",
    "实收资本（或股本）": "total_shares",
    "股本": "total_shares",
    "投资活动产生的现金流量净额": "investing_cash_flow",
    "投资活动产生/(使用)的现金流量净额": "investing_cash_flow",
    "投资活动产生/（使用）的现金流量净额": "investing_cash_flow",
    "投资活动产生（使用）的现金流量净额": "investing_cash_flow",
    "投资活动产生(使用)的现金流量净额": "investing_cash_flow",
    "投资活动使用的现金流量净额": "investing_cash_flow",
    "投资活动（使用）/产生的现金流量净额": "investing_cash_flow",
    "投资活动(使用)/产生的现金流量净额": "investing_cash_flow",
    "筹资活动产生的现金流量净额": "financing_cash_flow",
    "筹资活动使用的现金流量净额": "financing_cash_flow",
    "筹资活动产生/(使用)的现金流量净额": "financing_cash_flow",
    "筹资活动产生/（使用）的现金流量净额": "financing_cash_flow",
    "筹资活动(使用)/产生的现金流量净额": "financing_cash_flow",
    "筹资活动（使用）/产生的现金流量净额": "financing_cash_flow",
    "汇率变动对现金及现金等价物的影响": "cash_exchange_effect",
    "汇率变动对现金流量净额": "cash_exchange_effect",
    "现金及现金等价物净增加额": "net_cash_change",
    "现金及现金等价物净减少额": "net_cash_change",
    "现金及现金等价物净增加/(减少)额": "net_cash_change",
    "现金及现金等价物净增加/（减少）额": "net_cash_change",
    "现金及现金等价物净(减少)/增加额": "net_cash_change",
    "现金及现金等价物净（减少）/增加额": "net_cash_change",
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
_OMITTED_BONDS_PAYABLE_DERIVATION_VERSION = "omitted-zero-in-complete-reconciled-balance-sheet-v1"
_CASH_FLOW_RECONCILIATION = (
    "operating_cash_flow",
    "investing_cash_flow",
    "financing_cash_flow",
    "cash_exchange_effect",
    "net_cash_change",
)
_BANK_REQUIRED_FACTS = frozenset(
    {
        "revenue",
        "net_profit",
        "adjusted_net_profit",
        "operating_cash_flow",
        "capital_expenditure",
        "total_assets",
        "total_liabilities",
        "equity",
        "total_shares",
    }
)
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
    ) -> None:
        if not parser_version.strip():
            raise ValueError("PDF_PARSER_VERSION_INVALID")
        self.parser_version = parser_version
        self.reader_factory = reader_factory

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
            pages = [
                (
                    _physical_page_number(page, index),
                    page.extract_text(),
                )
                for index, page in enumerate(reader.pages, start=1)
            ]
        except Exception as error:
            raise ValueError("CNINFO_PDF_PARSE_FAILED") from error
        return self._parse_pages(
            pages,
            descriptor=descriptor,
            pdf_content_hash=pdf_content_hash,
        )

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
            return self._result((), {"PDF_LAYOUT_UNSUPPORTED"}, pdf_content_hash)
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
            or front_matter_labeled_codes
            or labeled_codes
            or set(_SUFFIXED_CODE.findall(joined))
        )
        identity_matches = codes == {descriptor.ts_code} or (
            not codes
            and _issuer_name_matches(visible_text[0], descriptor.issuer_name)
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
        fact_names_without_supported_unit: set[str] = set()
        statement_type: StatementType | None = None
        statement_title: str | None = None
        pending_statement: tuple[str, StatementType] | None = None
        pending_statement_lines = 0
        pending_statement_unit: tuple[str, Decimal] | None = None
        unit: tuple[str, Decimal] | None = None
        pending_label = ""
        annual_summary_spillover = False
        noncurrent_liability_section_markers: list[tuple[int, str]] = []
        flattened_state: _FlattenedStatementState | None = None
        flattened_complete = False
        formal_statements_complete = False
        formal_statements_started = False
        for page_number, text in pages:
            if not text:
                continue
            raw_lines = text.splitlines()
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
            if page_number <= 20:
                candidates.extend(
                    _flattened_summary_candidates(raw_lines, page_number=page_number)
                )
            embedded_statement = (
                None
                if formal_statements_complete
                else _embedded_consolidated_statement(raw_lines)
            )
            if embedded_statement is not None:
                statement_title, statement_type = embedded_statement
                formal_statements_started = True
                pending_statement = None
                pending_statement_lines = 0
                pending_statement_unit = None
                unit = _page_unit(raw_lines)
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
                if any(
                    normalized_heading.removesuffix("（续）").removesuffix("(续)").endswith(
                        boundary
                    )
                    for boundary in _FORMAL_STATEMENT_END_BOUNDARIES
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
                    and normalized_heading in _EMBEDDED_CONSOLIDATED_TITLES
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
                    normalized_heading in _EXTRACTION_BOUNDARIES
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
                    else:
                        statement_title = title[0]
                        statement_type = title[1]
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
                        inline_unit_match = _UNIT.search(line)
                        bare_unit_match = _BARE_CNY_UNIT.search(line)
                        unit = pending_statement_unit or (
                            _UNIT_DEFINITIONS[inline_unit_match.group(1)]
                            if inline_unit_match is not None
                            else (
                                _UNIT_DEFINITIONS[bare_unit_match.group(1)]
                                if bare_unit_match is not None
                                else None
                            )
                        )
                        pending_statement = None
                        pending_statement_lines = 0
                        pending_statement_unit = None
                    elif (pending_unit_match := _UNIT.search(line)) is not None:
                        pending_statement_unit = _UNIT_DEFINITIONS[pending_unit_match.group(1)]
                    elif pending_statement_unit is not None and _statement_table_header_matches(
                        line,
                        descriptor=descriptor,
                        statement_type=pending_statement[1],
                    ):
                        statement_title, statement_type = pending_statement
                        formal_statements_started = True
                        unit = pending_statement_unit
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
                unit_match = _UNIT.search(line)
                if unit_match is not None:
                    unit = _UNIT_DEFINITIONS[unit_match.group(1)]
                    pending_label = ""
                    continue

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

                fact_source_line = line
                parsed_line = None
                if pending_label:
                    fact_source_line = f"{pending_label} {line}"
                    parsed_line = _parse_fact_line(fact_source_line)
                if parsed_line is None:
                    fact_source_line = line
                    parsed_line = _parse_fact_line(line)
                if parsed_line is None and statement_type is StatementType.CASH_FLOW:
                    parsed_line = _parse_truncated_cash_exchange(line)
                    if parsed_line is None:
                        parsed_line = _parse_truncated_capital_expenditure(line)
                    if parsed_line is not None:
                        fact_source_line = line
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
                                source_priority=10,
                            )
                        )
                        pending_label = ""
                        continue
                if parsed_line is None:
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
                    pending_label = (
                        combined
                        if _could_be_alias_prefix(combined)
                        else (_normalize_label(line) if _could_be_alias_prefix(line) else "")
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
                    currency, multiplier = "SHARES", multiplier
                elif currency != "CNY":
                    fact_names_without_supported_unit.add(canonical_name)
                    continue
                candidates.append(
                    PdfFactCandidate(
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
                        ),
                    )
                )
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
            if name in {"interest_expense", "net_profit"}:
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
        if (
            missing_debt_components == {"bonds_payable"}
            and noncurrent_liability_section_markers
            and _balance_reconciles(facts)
        ):
            page_number, section_hash = noncurrent_liability_section_markers[0]
            bonds_candidate = PdfFactCandidate(
                canonical_fact_name="bonds_payable",
                value=Decimal(0),
                unit_multiplier=Decimal(1),
                currency="CNY",
                page_number=page_number,
                statement_type=StatementType.BALANCE_SHEET,
                source_text_hash=section_hash,
            )
            candidates.append(bonds_candidate)
            grouped["bonds_payable"] = [bonds_candidate]
            facts["bonds_payable"] = Decimal(0)
            derivations.append(
                (
                    "bonds_payable",
                    ("noncurrent_liabilities_section", "balance_equation"),
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

        if fact_names_without_supported_unit - facts.keys():
            issues.add("PDF_LAYOUT_UNSUPPORTED")
        required_facts = (
            _BANK_REQUIRED_FACTS if _is_bank_report(joined) else CANONICAL_PILOT_FACTS
        )
        if not required_facts.issubset(facts):
            issues.add("PDF_REQUIRED_FACTS_MISSING")
        self._validate_balance(facts, issues)
        self._validate_cash_flow(facts, issues)
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
        if "bonds_payable" in derivations:
            normalization_metadata["bonds_payable_derivation_version"] = (
                _OMITTED_BONDS_PAYABLE_DERIVATION_VERSION
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


def _parse_fact_line(line: str) -> tuple[str, Decimal] | None:
    label, separator, raw_value = line.partition("|")
    if not separator:
        label, separator, raw_value = line.partition("｜")
    if not separator:
        match = _NOTE_COLUMN_FACT.fullmatch(line.strip())
        if match is None:
            match = _SLASH_NOTE_COLUMN_FACT.fullmatch(line.strip())
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
    normalized = re.sub(r"\s+", "", raw_value).replace(",", "")
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = f"-{normalized[1:-1]}"
    try:
        return canonical_name, Decimal(normalized)
    except InvalidOperation:
        return None


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
    has_summary_heading = "主要会计数据" in text or "主要财务数据" in text
    aliases = (
        "归属于上市公司普通股股东的扣除非经常性损益的净利润",
        "归属于上市公司股东的扣除非经常性损益的净利润",
        "扣除非经常性损益后归属于本行股东的净利润",
        "扣除非经常性损益后的净利润",
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
    note = rf"(?:(?:{_NOTE_REFERENCE})\s+)?"
    return re.search(
        rf"{prefix}{qualifier}{unit}{separator}{note}(?P<value>{_ACCOUNTING_NUMBER})",
        text,
    )


def _accounting_decimal(raw_value: str) -> Decimal | None:
    normalized = re.sub(r"\s+", "", raw_value).replace(",", "")
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
    cash_paid_label = (
        "\u8d2d\u5efa\u56fa\u5b9a\u8d44\u4ea7\u3001\u65e0\u5f62\u8d44\u4ea7\u548c\u5176\u4ed6"
        "\u957f\u671f\u8d44\u4ea7\u6240\u652f\u4ed8\u7684\u73b0\u91d1"
    )
    if normalized_label == cash_paid_label.removesuffix("\u73b0\u91d1"):
        parsed = _parse_fact_line(f"{cash_paid_label} | {raw_value.strip()}")
        if parsed is not None and parsed[0] == "capital_expenditure":
            return parsed
    truncated_label = (
        "\u8d2d\u5efa\u56fa\u5b9a\u8d44\u4ea7\u3001\u65e0\u5f62\u8d44\u4ea7\u548c\u5176\u4ed6\u957f"
    )
    if normalized_label != truncated_label:
        return None
    full_label = (
        "\u8d2d\u5efa\u56fa\u5b9a\u8d44\u4ea7\u3001\u65e0\u5f62\u8d44\u4ea7\u548c\u5176\u4ed6"
        "\u957f\u671f\u8d44\u4ea7\u652f\u4ed8\u7684\u73b0\u91d1"
    )
    parsed = _parse_fact_line(f"{full_label} | {raw_value.strip()}")
    if parsed is None or parsed[0] != "capital_expenditure":
        return None
    return parsed


def _normalize_label(label: str) -> str:
    normalized = re.sub(r"\s+", "", label.strip())
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


def _formal_fact_priority(canonical_name: str, source_line: str) -> int:
    normalized = _normalize_label(source_line)
    if canonical_name == "revenue" and normalized.startswith("营业总收入"):
        return 5
    if canonical_name == "interest_expense" and normalized.startswith("利息费用"):
        return 5
    return 0


def _normalized_heading(line: str) -> str:
    normalized = re.sub(r"\s+", "", line.strip())
    normalized = re.sub(r"^§\d+", "", normalized)
    return re.sub(
        r"^(?:[（(]?[一二三四五六七八九十0-9]+[）)、.．])",
        "",
        normalized,
    )


def _formal_statement_title(
    normalized_heading: str,
) -> tuple[str, StatementType] | None:
    for title, statement_type in _STATEMENT_TITLES.items():
        if _statement_heading_matches(normalized_heading, title):
            return title, statement_type
    return None


def _period_prefixed_parent_statement(normalized_heading: str) -> bool:
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
        r"\d{4}年\d{1,2}(?:—|－|-)\d{1,2}月)?$"
    )
    return (not prefix or period_prefix.fullmatch(prefix) is not None) and (
        not suffix or suffix.startswith(("（", "("))
    )


def _embedded_consolidated_statement(
    raw_lines: list[str],
) -> tuple[str, StatementType] | None:
    """Recognize issuer PDFs whose visual heading is extracted after the table."""
    normalized_lines = [_normalized_heading(line) for line in raw_lines]
    combined = next(
        (
            (title, statement_type)
            for line in normalized_lines
            for title, statement_type in _COMBINED_STATEMENT_TITLES.items()
            if line in {title, f"{title}（续）", f"{title}(续)"}
        ),
        None,
    )
    if combined is not None:
        return combined
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


def _page_unit(raw_lines: list[str]) -> tuple[str, Decimal] | None:
    for line in raw_lines:
        unit_match = _UNIT.search(line)
        if unit_match is not None:
            return _UNIT_DEFINITIONS[unit_match.group(1)]
    return None


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
                rf"{period.year}年度(?:人民币(?:元|千元|万元|亿元))?",
                normalized,
            )
            is not None
        ):
            return True
    return (
        re.fullmatch(
            rf"{period.year}年0?1(?:—|－|-|至)0?{period.month}月",
            normalized,
        )
        is not None
    )


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
                rf"(?:附注{_CHINESE_NUMERAL})?"
                r"期末余额(?:期初余额|年初余额|上年年末余额)",
                normalized,
            )
            is not None
            or re.fullmatch(
                rf"项目(?:附注{_CHINESE_NUMERAL})?"
                rf"{current_date}{comparative_date}",
                normalized,
            )
            is not None
        )
    year = descriptor.report_period.year
    if descriptor.report_type is ReportType.ANNUAL:
        return (
            re.fullmatch(
                rf"项目(?:附注{_CHINESE_NUMERAL})?{year}年度{year - 1}年度",
                normalized,
            )
            is not None
            or re.fullmatch(
                rf"项目(?:附注{_CHINESE_NUMERAL})?"
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
    normalized = _normalize_label(line)
    canonical = _ALIASES.get(normalized)
    if canonical is None:
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
    return (exchange == "SZ" and symbol.startswith("2")) or (
        exchange == "SH" and symbol.startswith("9")
    )


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
