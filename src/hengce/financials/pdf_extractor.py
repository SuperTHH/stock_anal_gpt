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
_REPORT_PERIOD = re.compile(r"报告期\s*[：:]\s*(\d{4}-\d{2}-\d{2})")
_WHITESPACE_FACT = re.compile(
    r"^(?P<label>.+?)\s+"
    r"(?P<value>[-+]?\(?[\d,]+(?:\.\d+)?\)?)"
    r"(?:\s+.*)?$"
)
_UNIT = re.compile(
    r"单位\s*[：:]\s*(人民币元|人民币万元|人民币亿元|元|万元|亿元|股)"
)
_UNIT_DEFINITIONS = {
    "人民币元": ("CNY", Decimal(1)),
    "人民币万元": ("CNY", Decimal(10_000)),
    "人民币亿元": ("CNY", Decimal(100_000_000)),
    "元": ("CNY", Decimal(1)),
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
    "合并资产负债表": StatementType.BALANCE_SHEET,
    "合并利润表": StatementType.INCOME_STATEMENT,
    "合并现金流量表": StatementType.CASH_FLOW,
    "股本信息": StatementType.BALANCE_SHEET,
}
_ALIASES = {
    "资产总计": "total_assets",
    "流动资产合计": "current_assets",
    "货币资金": "cash_and_equivalents",
    "负债合计": "total_liabilities",
    "流动负债合计": "current_liabilities",
    "有息负债": "interest_bearing_debt",
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
    "投资活动产生的现金流量净额": "investing_cash_flow",
    "筹资活动产生的现金流量净额": "financing_cash_flow",
    "汇率变动对现金及现金等价物的影响": "cash_exchange_effect",
    "现金及现金等价物净增加额": "net_cash_change",
}
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
                    int(getattr(page, "page_number", index)),
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
        codes = set(_SUFFIXED_CODE.findall(joined))
        codes.update(
            f"{symbol}.{descriptor.ts_code.rpartition('.')[2]}"
            for symbol in _LABELED_CODE.findall(joined)
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
        unit: tuple[str, Decimal] | None = None
        pending_label = ""
        for page_number, text in pages:
            if not text:
                continue
            for raw_line in text.splitlines():
                line = raw_line.strip()
                title = next(
                    (
                        (candidate, kind)
                        for candidate, kind in _STATEMENT_TITLES.items()
                        if candidate in line
                    ),
                    None,
                )
                if title is not None:
                    statement_title = title[0]
                    statement_type = title[1]
                    unit = None
                    pending_label = ""
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

                parsed_line = _parse_fact_line(line)
                if parsed_line is None and pending_label:
                    parsed_line = _parse_fact_line(f"{pending_label} {line}")
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
                    statement_title == "主要财务数据"
                    and canonical_name != "adjusted_net_profit"
                ):
                    continue
                if statement_type is None:
                    continue
                if unit is None:
                    recognized_fact_line_without_unit = True
                    continue
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

        if not CANONICAL_PILOT_FACTS.issubset(facts):
            issues.add("PDF_REQUIRED_FACTS_MISSING")
        self._validate_balance(facts, issues)
        self._validate_cash_flow(facts, issues)
        return self._result(tuple(candidates), issues, pdf_content_hash, facts)

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
            normalization_metadata={
                "parser_version": extracted.parser_version,
                "pdf_content_hash": extracted.pdf_content_hash,
                "report_period": descriptor.report_period.isoformat(),
                "report_type": descriptor.report_type.value,
            },
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
        )


def _parse_fact_line(line: str) -> tuple[str, Decimal] | None:
    label, separator, raw_value = line.partition("|")
    if not separator:
        label, separator, raw_value = line.partition("｜")
    if not separator:
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


def _normalize_label(label: str) -> str:
    normalized = re.sub(r"\s+", "", label.strip())
    normalized = re.sub(
        r"^(?:[一二三四五六七八九十]+、|[（(][一二三四五六七八九十]+[）)])",
        "",
        normalized,
    )
    if normalized.startswith("其中："):
        normalized = normalized.removeprefix("其中：")
    return normalized


def _canonical_name(label: str) -> str | None:
    direct = _ALIASES.get(label)
    if direct is not None:
        return direct
    for alias in sorted(_ALIASES, key=len, reverse=True):
        if label.startswith(f"{alias}（") or label.startswith(f'{alias}('):
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


def _period_matches(
    text: str,
    explicit_periods: set[str],
    descriptor: FilingDescriptor,
) -> bool:
    expected = descriptor.report_period.isoformat()
    if explicit_periods:
        return explicit_periods == {expected}
    period = descriptor.report_period
    visible_date = re.compile(
        rf"{period.year}\s*年\s*{period.month}\s*月\s*{period.day}\s*日"
    )
    return visible_date.search(text) is not None


__all__ = [
    "CninfoPdfExtractor",
    "PdfExtractionResult",
    "PdfFactCandidate",
]
