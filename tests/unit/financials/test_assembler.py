from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from hengce.contracts.enums import QualityStatus
from hengce.financials.assembler import PointInTimeFinancialAssembler
from hengce.financials.query import FinancialQueryResult
from hengce.services.financial_resolution import FinancialDocument

CUTOFF = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
PERIODS = (
    date(2023, 12, 31),
    date(2024, 12, 31),
    date(2025, 3, 31),
    date(2025, 12, 31),
    date(2026, 3, 31),
)


def xbrl_fact(period: date, name: str, value: str) -> dict[str, object]:
    return {
        "fact_id": f"xbrl-{period}-{name}",
        "filing_id": f"xbrl-{period}",
        "canonical_fact_name": name,
        "fact_value": Decimal(value),
        "quality_status": "VALID",
        "published_at": CUTOFF - timedelta(days=30),
        "effective_at": datetime(
            period.year,
            period.month,
            period.day,
            15,
            59,
            59,
            tzinfo=UTC,
        ),
        "collected_at": CUTOFF - timedelta(days=29),
        "valid_from": CUTOFF - timedelta(days=29),
    }


class FakeQuery:
    def __init__(
        self,
        results: dict[date, FinancialQueryResult],
    ) -> None:
        self.results = results
        self.calls: list[dict[str, object]] = []

    def query_financial_facts(self, **kwargs: object) -> FinancialQueryResult:
        self.calls.append(kwargs)
        return self.results[kwargs["report_period"]]


def pdf_document(period: date) -> FinancialDocument:
    return FinancialDocument(
        filing_id=f"pdf-{period}",
        ts_code="699999.SH",
        source_id="cninfo",
        source_kind="PDF",
        source_url="https://www.cninfo.com.cn/fixture.pdf",
        published_at=CUTOFF - timedelta(days=20),
        valid_from=CUTOFF - timedelta(days=19),
        version="pdf-v1",
        supersedes_id=None,
        quality_status=QualityStatus.VALID,
        facts={"revenue": Decimal("88")},
    )


def test_assembles_five_periods_without_annualizing_q1_or_calling_pdf() -> None:
    """Catches collapsing annual/Q1 periods or doing unnecessary PDF extraction."""
    query = FakeQuery(
        {
            period: FinancialQueryResult(
                facts=(xbrl_fact(period, "revenue", str(index + 100)),),
                blocked_reasons=(),
                filing_ids=(f"xbrl-{period}",),
            )
            for index, period in enumerate(PERIODS)
        }
    )
    pdf_calls: list[date] = []

    result = PointInTimeFinancialAssembler(
        query=query,
        pdf_provider=lambda _code, period: (
            pdf_calls.append(period),
            pdf_document(period),
        )[1],
    ).assemble(
        ts_code="699999.SH",
        periods=PERIODS,
        report_cutoff_at=CUTOFF,
        known_at=CUTOFF,
    )

    assert result.blocked_reasons == ()
    assert tuple(result.periods) == PERIODS
    assert result.periods[date(2025, 3, 31)].report_kind == "Q1"
    assert result.periods[date(2025, 12, 31)].report_kind == "ANNUAL"
    assert result.periods[date(2026, 3, 31)].facts["revenue"].value == Decimal(
        "104"
    )
    assert all(period.source_kind == "XBRL" for period in result.periods.values())
    assert pdf_calls == []
    assert all(call["as_of"] == CUTOFF for call in query.calls)
    assert all(call["known_at"] == CUTOFF for call in query.calls)


def test_validated_pdf_is_used_only_when_xbrl_is_confirmed_missing() -> None:
    """Catches mixing PDF values into a usable exchange filing."""
    query = FakeQuery(
        {
            PERIODS[0]: FinancialQueryResult(
                facts=(),
                blocked_reasons=(),
                filing_ids=(),
            )
        }
    )

    result = PointInTimeFinancialAssembler(
        query=query,
        pdf_provider=lambda _code, period: pdf_document(period),
    ).assemble(
        ts_code="699999.SH",
        periods=(PERIODS[0],),
        report_cutoff_at=CUTOFF,
        known_at=CUTOFF,
    )

    period = result.periods[PERIODS[0]]
    assert result.blocked_reasons == ()
    assert period.source_kind == "PDF"
    assert period.filing_id == f"pdf-{PERIODS[0]}"
    assert period.facts["revenue"].value == Decimal("88")
    assert period.facts["revenue"].input_record_id == (
        f"pdf-{PERIODS[0]}:revenue"
    )


def test_unusable_published_correction_blocks_without_falling_back_to_old_or_pdf() -> None:
    """Catches hiding an unusable restatement behind an older value or PDF fallback."""
    query = FakeQuery(
        {
            PERIODS[0]: FinancialQueryResult(
                facts=(),
                blocked_reasons=("FINANCIAL_RESTATEMENT_UNUSABLE",),
                filing_ids=("old-filing", "correction-filing"),
            )
        }
    )
    pdf_called = False

    def pdf_provider(_code: str, period: date) -> FinancialDocument:
        nonlocal pdf_called
        pdf_called = True
        return pdf_document(period)

    result = PointInTimeFinancialAssembler(
        query=query,
        pdf_provider=pdf_provider,
    ).assemble(
        ts_code="699999.SH",
        periods=(PERIODS[0],),
        report_cutoff_at=CUTOFF,
        known_at=CUTOFF,
    )

    assert result.periods == {}
    assert result.blocked_reasons == (
        "2023-12-31:FINANCIAL_RESTATEMENT_UNUSABLE",
    )
    assert pdf_called is False


def test_future_or_unverified_pdf_is_not_assembled() -> None:
    query = FakeQuery(
        {
            PERIODS[0]: FinancialQueryResult(
                facts=(),
                blocked_reasons=(),
                filing_ids=(),
            )
        }
    )
    future = pdf_document(PERIODS[0]).model_copy(
        update={"published_at": CUTOFF + timedelta(seconds=1)}
    )

    result = PointInTimeFinancialAssembler(
        query=query,
        pdf_provider=lambda _code, _period: future,
    ).assemble(
        ts_code="699999.SH",
        periods=(PERIODS[0],),
        report_cutoff_at=CUTOFF,
        known_at=CUTOFF,
    )

    assert result.periods == {}
    assert result.blocked_reasons == ("2023-12-31:FINANCIAL_SOURCE_MISSING",)


def test_xbrl_comparative_context_does_not_conflict_with_current_period_fact() -> None:
    """Catches treating the prior-year comparison column as a duplicate current value."""
    period = date(2025, 12, 31)
    comparative = {
        **xbrl_fact(period, "revenue", "1200"),
        "fact_id": "revenue-comparative-2024",
        "period_end": "2024-12-31",
        "instant": None,
    }
    current = {
        **xbrl_fact(period, "revenue", "1500"),
        "fact_id": "revenue-current-2025",
        "period_end": "2025-12-31",
        "instant": None,
    }
    query = FakeQuery(
        {
            period: FinancialQueryResult(
                facts=(comparative, current),
                blocked_reasons=(),
                filing_ids=(f"xbrl-{period}",),
            )
        }
    )

    result = PointInTimeFinancialAssembler(
        query=query,
        pdf_provider=lambda _code, _period: (),
    ).assemble(
        ts_code="699999.SH",
        periods=(period,),
        report_cutoff_at=CUTOFF,
        known_at=CUTOFF,
    )

    assert result.blocked_reasons == ()
    assert result.periods[period].facts["revenue"].value == Decimal("1500")
    assert (
        result.periods[period].facts["revenue"].input_record_id
        == "revenue-current-2025"
    )
