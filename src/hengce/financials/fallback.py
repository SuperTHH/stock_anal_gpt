import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from hengce.contracts.enums import QualityStatus
from hengce.services.financial_resolution import FinancialDocument

_LABELS = {
    "资产总计": "total_assets",
    "负债合计": "total_liabilities",
    "所有者权益合计": "equity",
    "股东权益合计": "equity",
    "营业收入": "revenue",
    "净利润": "net_profit",
    "经营活动产生的现金流量净额": "operating_cash_flow",
}
_REQUIRED = frozenset(
    {
        "total_assets",
        "total_liabilities",
        "equity",
        "revenue",
        "net_profit",
        "operating_cash_flow",
    }
)


@dataclass(frozen=True, slots=True)
class PdfFactExtraction:
    filing_id: str
    report_period: date | None
    currency: str | None
    currency_unit: str | None
    unit_multiplier: Decimal | None
    facts: dict[str, Decimal]
    quality_status: QualityStatus
    issues: tuple[str, ...]
    parser_version: str


class CninfoPdfFactExtractor:
    def __init__(self, parser_version: str) -> None:
        if not parser_version:
            raise ValueError("parser_version must not be empty")
        self.parser_version = parser_version

    def extract(self, path: Path, *, filing_id: str) -> PdfFactExtraction:
        try:
            from pypdf import PdfReader
        except ImportError as error:
            raise RuntimeError("PDF_EXTRACTOR_UNAVAILABLE") from error
        try:
            reader = PdfReader(path)
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as error:
            raise ValueError("CNINFO_PDF_PARSE_FAILED") from error
        return self.parse_text(text, filing_id=filing_id)

    def parse_text(self, text: str, *, filing_id: str) -> PdfFactExtraction:
        report_match = re.search(r"报告期\s*[：:]\s*(\d{4}-\d{2}-\d{2})", text)
        currency_match = re.search(r"货币单位\s*[：:]\s*(人民币元|元|万元|亿元)", text)
        currency_unit = currency_match.group(1) if currency_match is not None else None
        unit_multiplier = {
            "人民币元": Decimal(1),
            "元": Decimal(1),
            "万元": Decimal(10_000),
            "亿元": Decimal(100_000_000),
        }.get(currency_unit)
        facts: dict[str, Decimal] = {}
        for label, canonical_name in _LABELS.items():
            match = re.search(
                rf"{re.escape(label)}\s*[|｜]\s*([-+]?\(?[\d,]+(?:\.\d+)?\)?)",
                text,
            )
            if match is None:
                continue
            raw = match.group(1).replace(",", "")
            if raw.startswith("(") and raw.endswith(")"):
                raw = f"-{raw[1:-1]}"
            try:
                facts[canonical_name] = Decimal(raw) * (
                    unit_multiplier if unit_multiplier is not None else Decimal(1)
                )
            except InvalidOperation:
                continue
        return PdfFactExtraction(
            filing_id=filing_id,
            report_period=(
                date.fromisoformat(report_match.group(1)) if report_match is not None else None
            ),
            currency="CNY" if currency_match is not None else None,
            currency_unit=currency_unit,
            unit_multiplier=unit_multiplier,
            facts=facts,
            quality_status=QualityStatus.UNVERIFIED,
            issues=(),
            parser_version=self.parser_version,
        )

    def validate(self, parsed: PdfFactExtraction) -> PdfFactExtraction:
        issues: list[str] = []
        if parsed.report_period is None:
            issues.append("PDF_REPORT_PERIOD_MISSING")
        if parsed.currency is None:
            issues.append("PDF_CURRENCY_MISSING")
        if parsed.unit_multiplier is None:
            issues.append("PDF_UNIT_MULTIPLIER_MISSING")
        if not _REQUIRED.issubset(parsed.facts):
            issues.append("PDF_REQUIRED_FACTS_MISSING")
        assets = parsed.facts.get("total_assets")
        liabilities = parsed.facts.get("total_liabilities")
        equity = parsed.facts.get("equity")
        if (
            assets is not None
            and liabilities is not None
            and equity is not None
            and abs(assets - liabilities - equity)
            > max(abs(assets) * Decimal("0.001"), Decimal("1"))
        ):
            issues.append("PDF_BALANCE_EQUATION_FAILED")
        return replace(
            parsed,
            quality_status=QualityStatus.VALID if not issues else QualityStatus.UNVERIFIED,
            issues=tuple(issues),
        )

    @staticmethod
    def build_document(
        *,
        filing_id: str,
        ts_code: str,
        source_url: str,
        published_at: datetime,
        version: str,
        supersedes_id: str | None,
        parsed: PdfFactExtraction,
    ) -> FinancialDocument:
        return FinancialDocument(
            filing_id=filing_id,
            ts_code=ts_code,
            source_id="cninfo",
            source_kind="PDF",
            source_url=source_url,
            published_at=published_at,
            valid_from=published_at,
            version=version,
            supersedes_id=supersedes_id,
            quality_status=parsed.quality_status,
            facts=parsed.facts,
            normalization_metadata={
                "currency": parsed.currency or "UNKNOWN",
                "currency_unit": parsed.currency_unit or "UNKNOWN",
                "unit_multiplier": (
                    str(parsed.unit_multiplier)
                    if parsed.unit_multiplier is not None
                    else "UNKNOWN"
                ),
                "normalized_unit": "CNY",
            },
        )
