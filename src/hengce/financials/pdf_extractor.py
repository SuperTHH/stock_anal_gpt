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

_NUMBER = re.compile(r"^[-+]?\(?[\d,]+(?:\.\d+)?\)?$")
_SUFFIXED_CODE = re.compile(r"\b[0-9]{6}\.(?:SH|SZ)\b")
_LABELED_CODE = re.compile(
    r"(?:证券代码|股票代码|公司代码)\s*[：:]?\s*([0-9]{6})"
)
_LABELED_CODE_DETAIL = re.compile(
    r"(?:证券代码|股票代码|公司代码)\s*[：:]?\s*"
    r"([0-9]{6})(?:\.([A-Z]{2,4}))?"
)
_REPORT_PERIOD = re.compile(r"报告期\s*[：:]\s*(\d{4}-\d{2}-\d{2})")
_WHITESPACE_FACT = re.compile(
    r"^(?P<label>.+?)\s+"
    r"(?P<value>[-+]?\(?[\d,]+(?:\.\d+)?\)?)"
    r"(?:\s+.*)?$"
)
_NOTE_COLUMN_FACT = re.compile(
    r"^(?P<label>.+?)\s+"
    r"[一二三四五六七八九十]+、\d+(?:[（(]\d+[）)])?[A-Za-z]?\s+"
    r"(?P<value>[-+]?\(?[\d,]+(?:\.\d+)?\)?)"
    r"(?:\s+.*)?$"
)
_NUMERIC_NOTE_COLUMN_FACT = re.compile(
    r"^(?P<label>.+?)\s+\d{1,3}\s+"
    r"(?P<value>[-+]?\(?[\d,]+(?:\.\d+)?\)?)\s+"
    r"[-+]?\(?[\d,]+(?:\.\d+)?\)?(?:\s+.*)?$"
)
_UNIT = re.compile(
    r"单位\s*[：:]\s*(人民币元|人民币万元|人民币亿元|元|千元|万元|亿元|股)"
)
_INLINE_CNY_UNIT = re.compile(r"[（(](元|千元|万元|亿元)[）)]")
_BARE_CNY_UNIT = re.compile(r"(人民币(?:元|万元|亿元))")
_UNIT_DEFINITIONS = {
    "人民币元": ("CNY", Decimal(1)),
    "人民币万元": ("CNY", Decimal(10_000)),
    "人民币亿元": ("CNY", Decimal(100_000_000)),
    "元": ("CNY", Decimal(1)),
    "千元": ("CNY", Decimal(1_000)),
    "万元": ("CNY", Decimal(10_000)),
    "亿元": ("CNY", Decimal(100_000_000)),
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
}
_CONSOLIDATED_STATEMENT_TITLES = frozenset(
    {"合并资产负债表", "合并利润表", "合并现金流量表"}
)
_EXTRACTION_BOUNDARIES = (
    "母公司资产负债表",
    "母公司利润表",
    "母公司现金流量表",
    "资产负债表",
    "利润表",
    "现金流量表",
    "合并所有者权益变动表",
    "母公司所有者权益变动表",
    "主要财务指标",
    "分季度主要财务指标",
    "主要会计数据、财务指标发生变动的情况、原因",
    "主要会计数据、财务指标发生变动的情况及原因",
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
    "营业收入": "revenue",
    "营业成本": "operating_cost",
    "净利润": "net_profit",
    "扣除非经常性损益后的净利润": "adjusted_net_profit",
    "归属于上市公司股东的扣除非经常性损益的净利润": (
        "adjusted_net_profit"
    ),
    "利息费用": "interest_expense",
    "经营活动产生的现金流量净额": "operating_cash_flow",
    "购建固定资产、无形资产和其他长期资产支付的现金": (
        "capital_expenditure"
    ),
    "期末总股本": "total_shares",
    "实收资本（或股本）": "total_shares",
    "股本": "total_shares",
    "投资活动产生的现金流量净额": "investing_cash_flow",
    "投资活动使用的现金流量净额": "investing_cash_flow",
    "筹资活动产生的现金流量净额": "financing_cash_flow",
    "筹资活动使用的现金流量净额": "financing_cash_flow",
    "汇率变动对现金及现金等价物的影响": "cash_exchange_effect",
    "现金及现金等价物净增加额": "net_cash_change",
    "现金及现金等价物净减少额": "net_cash_change",
    "现金及现金等价物净增加/(减少)额": "net_cash_change",
    "现金及现金等价物净增加/（减少）额": "net_cash_change",
    "现金及现金等价物净(减少)/增加额": "net_cash_change",
    "现金及现金等价物净（减少）/增加额": "net_cash_change",
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
_INTEREST_BEARING_DEBT_DERIVATION_VERSION = (
    "interest-bearing-debt-components-v1"
)
_CASH_FLOW_RECONCILIATION = (
    "operating_cash_flow",
    "investing_cash_flow",
    "financing_cash_flow",
    "cash_exchange_effect",
    "net_cash_change",
)


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
        cover_labeled_codes = _a_share_labeled_codes(
            visible_text[0], exchange_suffix
        )
        if (
            descriptor.ts_code in cover_labeled_codes
            and all(
                code == descriptor.ts_code
                or _is_secondary_share_class_code(code)
                for code in cover_labeled_codes
            )
        ):
            cover_labeled_codes = {descriptor.ts_code}
        cover_codes = cover_labeled_codes or set(
            _SUFFIXED_CODE.findall(visible_text[0])
        )
        labeled_codes = _a_share_labeled_codes(joined, exchange_suffix)
        front_matter_labeled_codes = _a_share_labeled_codes(
            "\n".join(
                text
                for page_number, text in pages
                if page_number <= 20 and text and text.strip()
            ),
            exchange_suffix,
        )
        if (
            descriptor.ts_code in front_matter_labeled_codes
            and all(
                code == descriptor.ts_code
                or _is_secondary_share_class_code(code)
                for code in front_matter_labeled_codes
            )
        ):
            front_matter_labeled_codes = {descriptor.ts_code}
        codes = (
            cover_codes
            or front_matter_labeled_codes
            or labeled_codes
            or set(_SUFFIXED_CODE.findall(joined))
        )
        periods = set(_REPORT_PERIOD.findall(joined))
        report_markers = _REPORT_TYPE_MARKERS[descriptor.report_type]
        if (
            codes != {descriptor.ts_code}
            or not _period_matches(joined, periods, descriptor)
            or not any(marker in joined for marker in report_markers)
        ):
            issues.add("PDF_LAYOUT_UNSUPPORTED")

        candidates: list[PdfFactCandidate] = []
        recognized_fact_line_without_unit = False
        statement_type: StatementType | None = None
        statement_title: str | None = None
        pending_statement: tuple[str, StatementType] | None = None
        pending_statement_lines = 0
        pending_statement_unit: tuple[str, Decimal] | None = None
        unit: tuple[str, Decimal] | None = None
        pending_label = ""
        for page_number, text in pages:
            if not text:
                continue
            raw_lines = text.splitlines()
            for raw_line in raw_lines:
                line = raw_line.strip()
                normalized_heading = _normalized_heading(line)
                if normalized_heading in _EXTRACTION_BOUNDARIES:
                    statement_title = None
                    statement_type = None
                    pending_statement = None
                    pending_statement_lines = 0
                    pending_statement_unit = None
                    unit = None
                    pending_label = ""
                    continue
                title = next(
                    (
                        (candidate, kind)
                        for candidate, kind in _STATEMENT_TITLES.items()
                        if candidate == normalized_heading
                    ),
                    None,
                )
                if title is not None:
                    statement_title = None
                    statement_type = None
                    unit = None
                    pending_label = ""
                    if title[0] in _CONSOLIDATED_STATEMENT_TITLES:
                        pending_statement = title
                        pending_statement_lines = 4
                        pending_statement_unit = None
                    else:
                        statement_title = title[0]
                        statement_type = title[1]
                        pending_statement = None
                        pending_statement_lines = 0
                        pending_statement_unit = None
                    continue
                if pending_statement is not None:
                    if _statement_period_heading_matches(
                        line,
                        descriptor=descriptor,
                        statement_type=pending_statement[1],
                    ):
                        statement_title, statement_type = pending_statement
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
                        pending_statement_unit = _UNIT_DEFINITIONS[
                            pending_unit_match.group(1)
                        ]
                    elif (
                        pending_statement_unit is not None
                        and _statement_table_header_matches(
                            line,
                            descriptor=descriptor,
                            statement_type=pending_statement[1],
                        )
                    ):
                        statement_title, statement_type = pending_statement
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
                        recognized_fact_line_without_unit = True
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
                        recognized_fact_line_without_unit = True
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
                parsed_line = _parse_fact_line(line)
                if (
                    parsed_line is None
                    and statement_type is StatementType.CASH_FLOW
                ):
                    parsed_line = _parse_truncated_cash_exchange(line)
                    if parsed_line is None:
                        parsed_line = _parse_truncated_capital_expenditure(line)
                    if parsed_line is not None:
                        fact_source_line = line
                if parsed_line is None and pending_label:
                    fact_source_line = f"{pending_label} {line}"
                    parsed_line = _parse_fact_line(fact_source_line)
                if parsed_line is None:
                    combined = _normalize_label(f"{pending_label}{line}")
                    pending_label = (
                        combined
                        if _could_be_alias_prefix(combined)
                        else (
                            _normalize_label(line)
                            if _could_be_alias_prefix(line)
                            else ""
                        )
                    )
                    continue
                pending_label = ""
                canonical_name, number = parsed_line
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
                        and (
                            inline_unit_match := _INLINE_CNY_UNIT.search(
                                fact_source_line
                            )
                        )
                    ):
                        currency, multiplier = _UNIT_DEFINITIONS[
                            inline_unit_match.group(1)
                        ]
                    else:
                        recognized_fact_line_without_unit = True
                        continue
                else:
                    currency, multiplier = unit
                if canonical_name == "total_shares":
                    currency, multiplier = "SHARES", Decimal(1)
                elif currency != "CNY":
                    recognized_fact_line_without_unit = True
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
                    )
                )
        if recognized_fact_line_without_unit:
            issues.add("PDF_LAYOUT_UNSUPPORTED")

        facts: dict[str, Decimal] = {}
        grouped: dict[str, list[PdfFactCandidate]] = {}
        for candidate in candidates:
            grouped.setdefault(candidate.canonical_fact_name, []).append(candidate)
        for name, name_candidates in grouped.items():
            values = {candidate.value for candidate in name_candidates}
            if len(values) != 1:
                issues.add("PDF_FACT_CONFLICT")
                continue
            facts[name] = name_candidates[0].value

        derivations: tuple[tuple[str, tuple[str, ...]], ...] = ()
        if (
            "interest_bearing_debt" not in facts
            and _INTEREST_BEARING_DEBT_COMPONENTS.issubset(facts)
        ):
            component_names = tuple(
                sorted(_INTEREST_BEARING_DEBT_COMPONENTS)
            )
            component_candidates = [
                grouped[name][0] for name in component_names
            ]
            combined_hash = hashlib.sha256(
                "\n".join(
                    f"{candidate.canonical_fact_name}:"
                    f"{candidate.source_text_hash}"
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
                page_number=min(
                    candidate.page_number
                    for candidate in component_candidates
                ),
                statement_type=StatementType.BALANCE_SHEET,
                source_text_hash=combined_hash,
            )
            candidates.append(derived_candidate)
            facts["interest_bearing_debt"] = debt_value
            derivations = (("interest_bearing_debt", component_names),)

        if not CANONICAL_PILOT_FACTS.issubset(facts):
            issues.add("PDF_REQUIRED_FACTS_MISSING")
        self._validate_balance(facts, issues)
        self._validate_cash_flow(facts, issues)
        return self._result(
            tuple(candidates),
            issues,
            pdf_content_hash,
            facts,
            derivations,
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
                if candidate.canonical_fact_name == name
                and candidate.value == value
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
        if "interest_bearing_debt" in derivations:
            normalization_metadata[
                "interest_bearing_debt_derivation_version"
            ] = _INTEREST_BEARING_DEBT_DERIVATION_VERSION
            normalization_metadata["interest_bearing_debt_components"] = (
                ",".join(derivations["interest_bearing_debt"])
            )
        return FinancialDocument(
            filing_id=filing_id,
            ts_code=descriptor.ts_code,
            source_id="cninfo",
            source_kind="PDF",
            source_url=descriptor.source_url,
            published_at=descriptor.published_at,
            valid_from=descriptor.collected_at,
            version=(
                f"pdf-{extracted.pdf_content_hash}:{extracted.parser_version}"
            ),
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
            difference = abs(
                assets - facts["total_liabilities"] - facts["equity"]
            )
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
                QualityStatus.VALID
                if not ordered_issues
                else QualityStatus.UNVERIFIED
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
    normalized = raw_value.replace(",", "")
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = f"-{normalized[1:-1]}"
    try:
        return canonical_name, Decimal(normalized)
    except InvalidOperation:
        return None


def _parse_truncated_cash_exchange(line: str) -> tuple[str, Decimal] | None:
    match = _WHITESPACE_FACT.fullmatch(line.strip())
    if match is None:
        return None
    expected_label = "汇率变动对现金及现金等价物的影响"
    if _normalize_label(match.group("label")) != expected_label.removesuffix(
        "影响"
    ):
        return None
    parsed = _parse_fact_line(
        f"{expected_label} {match.group('value')}"
    )
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
    truncated_label = (
        "\u8d2d\u5efa\u56fa\u5b9a\u8d44\u4ea7\u3001\u65e0\u5f62\u8d44\u4ea7\u548c\u5176\u4ed6\u957f"
    )
    if _normalize_label(label) != truncated_label:
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


def _normalized_heading(line: str) -> str:
    normalized = re.sub(r"\s+", "", line.strip())
    return re.sub(
        r"^(?:[（(]?[一二三四五六七八九十0-9]+[）)、.．])",
        "",
        normalized,
    )


def _statement_period_heading_matches(
    line: str,
    *,
    descriptor: FilingDescriptor,
    statement_type: StatementType,
) -> bool:
    normalized = re.sub(r"\s+", "", line.strip())
    period = descriptor.report_period
    if statement_type is StatementType.BALANCE_SHEET:
        date_pattern = (
            rf"{period.year}年0?{period.month}月0?{period.day}日"
        )
        return (
            re.fullmatch(
                rf"{date_pattern}(?:人民币(?:元|万元|亿元))?",
                normalized,
            )
            is not None
            or (
                normalized.startswith("项目")
                and re.search(date_pattern, normalized) is not None
            )
        )
    if descriptor.report_type is ReportType.ANNUAL:
        if re.fullmatch(
            rf"{period.year}年度(?:人民币(?:元|万元|亿元))?",
            normalized,
        ) is not None:
            return True
    return re.fullmatch(
        rf"{period.year}年0?1(?:—|－|-|至)0?{period.month}月",
        normalized,
    ) is not None


def _statement_table_header_matches(
    line: str,
    *,
    descriptor: FilingDescriptor,
    statement_type: StatementType,
) -> bool:
    if statement_type is StatementType.BALANCE_SHEET:
        return (
            descriptor.report_type is ReportType.Q1
            and re.sub(r"\s+", "", line.strip()) == "项目期末余额期初余额"
        )
    normalized = re.sub(r"\s+", "", line.strip())
    year = descriptor.report_period.year
    if descriptor.report_type is ReportType.ANNUAL:
        return re.fullmatch(
            rf"项目(?:附注)?{year}年度{year - 1}年度",
            normalized,
        ) is not None
    if descriptor.report_type is ReportType.Q1:
        return normalized in {
            "项目本期发生额上期发生额",
            f"项目{year}年第一季度{year - 1}年第一季度",
        }
    return False


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
    normalized = _normalize_label(line)
    canonical = _ALIASES.get(normalized)
    if canonical in _INTEREST_BEARING_DEBT_COMPONENTS:
        return canonical
    return None


def _blank_cash_flow_component(line: str) -> str | None:
    normalized = _normalize_label(line)
    canonical = _ALIASES.get(normalized)
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
        q1_title = re.compile(
            rf"{period.year}\s*年\s*第一季度报告"
        )
        if q1_title.search(text) is not None:
            return True
    visible_date = re.compile(
        rf"{period.year}\s*年\s*0?{period.month}\s*月\s*0?{period.day}\s*日"
    )
    return visible_date.search(text) is not None


def _a_share_labeled_codes(text: str, default_suffix: str) -> set[str]:
    codes: set[str] = set()
    for symbol, explicit_suffix in _LABELED_CODE_DETAIL.findall(text):
        if explicit_suffix and explicit_suffix not in {"SH", "SZ"}:
            continue
        codes.add(f"{symbol}.{explicit_suffix or default_suffix}")
    return codes


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
    return (
        page_number + 1
        if page_number == enumeration_index - 1
        else page_number
    )


__all__ = [
    "CninfoPdfExtractor",
    "PdfExtractionResult",
    "PdfFactCandidate",
]
