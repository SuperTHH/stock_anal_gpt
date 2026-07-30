from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from hengce.contracts.enums import QualityStatus
from hengce.financials.query import AsOfFinancialQuery
from hengce.financials.registry_loader import CANONICAL_PILOT_FACTS
from hengce.services.financial_resolution import (
    FinancialDocument,
    PreferredFinancialResolver,
)


@dataclass(frozen=True, slots=True)
class AssembledFinancialFact:
    canonical_fact_name: str
    value: Decimal
    input_record_id: str
    filing_id: str
    source_kind: str
    published_at: datetime
    collected_at: datetime
    valid_from: datetime
    quality_status: QualityStatus


@dataclass(frozen=True, slots=True)
class FinancialPeriodSnapshot:
    report_period: date
    report_kind: str
    source_kind: str
    filing_id: str
    facts: dict[str, AssembledFinancialFact]


@dataclass(frozen=True, slots=True)
class FinancialSeriesResult:
    ts_code: str
    periods: dict[date, FinancialPeriodSnapshot]
    blocked_reasons: tuple[str, ...]
    report_cutoff_at: datetime
    known_at: datetime


PdfProvider = Callable[
    [str, date],
    FinancialDocument | Sequence[FinancialDocument],
]


class PointInTimeFinancialAssembler:
    def __init__(
        self,
        *,
        query: AsOfFinancialQuery,
        pdf_provider: PdfProvider,
        resolver: PreferredFinancialResolver | None = None,
    ) -> None:
        self._query = query
        self._pdf_provider = pdf_provider
        self._resolver = resolver or PreferredFinancialResolver()

    def assemble(
        self,
        *,
        ts_code: str,
        periods: tuple[date, ...],
        report_cutoff_at: datetime,
        known_at: datetime,
    ) -> FinancialSeriesResult:
        self._require_aware(report_cutoff_at)
        self._require_aware(known_at)
        snapshots: dict[date, FinancialPeriodSnapshot] = {}
        blocked: list[str] = []
        for period in periods:
            query_result = self._query.query_financial_facts(
                ts_code=ts_code,
                report_period=period,
                canonical_fact_names=CANONICAL_PILOT_FACTS,
                as_of=report_cutoff_at,
                known_at=known_at,
            )
            if query_result.blocked_reasons:
                blocked.extend(
                    f"{period.isoformat()}:{reason}"
                    for reason in query_result.blocked_reasons
                )
                continue
            if query_result.facts:
                snapshot, reason = self._from_xbrl(
                    period,
                    query_result.facts,
                    report_cutoff_at,
                    known_at,
                )
                if reason is not None:
                    blocked.append(f"{period.isoformat()}:{reason}")
                elif snapshot is not None:
                    snapshots[period] = snapshot
                continue

            resolution = self._resolver.resolve(
                exchange_xbrl=None,
                cninfo_pdf_provider=lambda period=period: self._pdf_provider(
                    ts_code,
                    period,
                ),
                as_of=report_cutoff_at,
                known_at=known_at,
            )
            if resolution.blocked_reasons:
                blocked.extend(
                    f"{period.isoformat()}:{reason}"
                    for reason in resolution.blocked_reasons
                )
                continue
            assert resolution.document is not None
            snapshots[period] = self._from_pdf(period, resolution.document)
        return FinancialSeriesResult(
            ts_code=ts_code,
            periods=snapshots,
            blocked_reasons=tuple(blocked),
            report_cutoff_at=report_cutoff_at,
            known_at=known_at,
        )

    @staticmethod
    def _from_xbrl(
        period: date,
        facts: tuple[dict[str, object], ...],
        report_cutoff_at: datetime,
        known_at: datetime,
    ) -> tuple[FinancialPeriodSnapshot | None, str | None]:
        assembled: dict[str, AssembledFinancialFact] = {}
        filing_ids: set[str] = set()
        for raw in facts:
            context_period = _fact_context_period(raw)
            if context_period is not None and context_period != period:
                continue
            name = str(raw["canonical_fact_name"])
            if name in assembled:
                return None, "FINANCIAL_PERIOD_FACT_DUPLICATE"
            published_at = _aware_datetime(raw["published_at"])
            collected_at = _aware_datetime(raw["collected_at"])
            valid_from = _aware_datetime(raw["valid_from"])
            if any(
                value is None
                for value in (published_at, collected_at, valid_from)
            ):
                return None, "FINANCIAL_PERIOD_TIME_INVALID"
            assert published_at is not None
            assert collected_at is not None
            assert valid_from is not None
            if published_at > report_cutoff_at:
                return None, "FINANCIAL_PERIOD_AFTER_CUTOFF"
            if collected_at > known_at or valid_from > known_at:
                return None, "FINANCIAL_PERIOD_NOT_KNOWN"
            quality = QualityStatus(str(raw["quality_status"]))
            if quality is not QualityStatus.VALID:
                return None, "FINANCIAL_PERIOD_QUALITY_BLOCKED"
            filing_id = str(raw["filing_id"])
            filing_ids.add(filing_id)
            assembled[name] = AssembledFinancialFact(
                canonical_fact_name=name,
                value=Decimal(str(raw["fact_value"])),
                input_record_id=str(raw["fact_id"]),
                filing_id=filing_id,
                source_kind="XBRL",
                published_at=published_at,
                collected_at=collected_at,
                valid_from=valid_from,
                quality_status=quality,
            )
        if not assembled:
            return None, "FINANCIAL_PERIOD_CURRENT_CONTEXT_MISSING"
        if len(filing_ids) != 1:
            return None, "FINANCIAL_PERIOD_FILING_CONFLICT"
        return (
            FinancialPeriodSnapshot(
                report_period=period,
                report_kind=_report_kind(period),
                source_kind="XBRL",
                filing_id=next(iter(filing_ids)),
                facts=assembled,
            ),
            None,
        )

    @staticmethod
    def _from_pdf(
        period: date,
        document: FinancialDocument,
    ) -> FinancialPeriodSnapshot:
        facts = {
            name: AssembledFinancialFact(
                canonical_fact_name=name,
                value=value,
                input_record_id=f"{document.filing_id}:{name}",
                filing_id=document.filing_id,
                source_kind="PDF",
                published_at=document.published_at,
                collected_at=document.valid_from,
                valid_from=document.valid_from,
                quality_status=document.quality_status,
            )
            for name, value in document.facts.items()
        }
        return FinancialPeriodSnapshot(
            report_period=period,
            report_kind=_report_kind(period),
            source_kind="PDF",
            filing_id=document.filing_id,
            facts=facts,
        )

    @staticmethod
    def _require_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("FINANCIAL_SERIES_CUTOFF_INVALID")


def _report_kind(period: date) -> str:
    if (period.month, period.day) == (12, 31):
        return "ANNUAL"
    if (period.month, period.day) == (3, 31):
        return "Q1"
    return "OTHER"


def _aware_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return (
        parsed
        if parsed.tzinfo is not None and parsed.utcoffset() is not None
        else None
    )


def _fact_context_period(raw: dict[str, object]) -> date | None:
    for key in ("instant", "period_end"):
        value = raw.get(key)
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str) and value:
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                return None
    return None


__all__ = [
    "AssembledFinancialFact",
    "FinancialPeriodSnapshot",
    "FinancialSeriesResult",
    "PointInTimeFinancialAssembler",
]
