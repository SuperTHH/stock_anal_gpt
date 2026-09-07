from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from hengce.contracts.enums import (
    ActionStatus,
    ActionType,
    QualityStatus,
)
from hengce.contracts.market import CorporateAction
from hengce.contracts.market_screen import ImplementedDividend
from hengce.financials.assembler import (
    AssembledFinancialFact,
    FinancialPeriodSnapshot,
    FinancialSeriesResult,
)
from hengce.financials.metrics import PilotMetricResult
from hengce.services.pilot_financial_analysis import (
    PilotFinancialAnalyzer,
)

CUTOFF = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)


def test_analyzer_prefers_annual_share_baseline_and_applies_later_actions() -> None:
    share_fact = AssembledFinancialFact(
        canonical_fact_name="total_shares",
        value=Decimal("100"),
        input_record_id="share-fact-q1",
        filing_id="filing-q1",
        source_kind="PDF",
        published_at=CUTOFF - timedelta(days=80),
        collected_at=CUTOFF - timedelta(days=70),
        valid_from=CUTOFF - timedelta(days=70),
        quality_status=QualityStatus.VALID,
    )
    series = FinancialSeriesResult(
        ts_code="600001.SH",
        periods={
            date(2025, 12, 31): FinancialPeriodSnapshot(
                report_period=date(2025, 12, 31),
                report_kind="ANNUAL",
                source_kind="PDF",
                filing_id="filing-annual",
                facts={
                    "total_shares": replace(
                        share_fact,
                        value=Decimal("100"),
                        input_record_id="share-fact-annual",
                        filing_id="filing-annual",
                    )
                },
            ),
            date(2026, 3, 31): FinancialPeriodSnapshot(
                report_period=date(2026, 3, 31),
                report_kind="Q1",
                source_kind="PDF",
                filing_id="filing-q1",
                facts={
                    "total_shares": replace(share_fact, value=Decimal("50"))
                },
            )
        },
        blocked_reasons=(),
        report_cutoff_at=CUTOFF,
        known_at=CUTOFF,
    )

    class Assembler:
        requested: tuple[date, ...] = ()

        def assemble(self, **kwargs: object) -> FinancialSeriesResult:
            self.requested = kwargs["periods"]  # type: ignore[assignment]
            return series

    split = CorporateAction(
        record_id="split-1",
        source_id="sse",
        source_url="https://www.sse.com.cn/split.pdf",
        published_at=CUTOFF - timedelta(days=20),
        effective_at=CUTOFF - timedelta(days=10),
        collected_at=CUTOFF - timedelta(days=19),
        version="v1",
        content_hash="a" * 64,
        license_policy="official-public",
        quality_status=QualityStatus.VALID,
        valid_from=CUTOFF - timedelta(days=19),
        ts_code="600001.SH",
        action_type=ActionType.SPLIT,
        record_date=(CUTOFF - timedelta(days=11)).date(),
        ex_date=(CUTOFF - timedelta(days=10)).date(),
        split_ratio=Decimal("2"),
        action_status=ActionStatus.IMPLEMENTED,
    )

    class Calculator:
        total_shares: Decimal | None = None

        def calculate(self, **kwargs: object) -> PilotMetricResult:
            self.total_shares = kwargs["share_capital"].total_shares  # type: ignore[union-attr]
            return PilotMetricResult(
                metrics={},
                blocked_reasons=(),
                report_cutoff_at=CUTOFF,
                known_at=CUTOFF,
                tax_rate_proxy=Decimal("0.25"),
            )

    assembler = Assembler()
    calculator = Calculator()
    analyzer = PilotFinancialAnalyzer(
        assembler=assembler,
        action_repository=SimpleNamespace(
            visible_actions=lambda *_args, **_kwargs: (split,)
        ),
        metric_calculator=calculator,
        market_warehouse=SimpleNamespace(
            read_bars=lambda _date: [
                SimpleNamespace(ts_code="600001.SH", close=Decimal("10"))
            ]
        ),
    )

    results = analyzer.calculate(
        universe=SimpleNamespace(
            members=[SimpleNamespace(ts_code="600001.SH")]
        ),
        market_date=date(2026, 7, 22),
        report_cutoff_at=CUTOFF,
        known_at=CUTOFF,
    )

    assert set(results) == {"600001.SH"}
    assert assembler.requested == (
        date(2023, 12, 31),
        date(2024, 12, 31),
        date(2025, 3, 31),
        date(2025, 12, 31),
        date(2026, 3, 31),
    )
    assert calculator.total_shares == Decimal("200")


def test_analyzer_bridges_exchange_implemented_dividends_into_strategy_actions() -> None:
    series = FinancialSeriesResult(
        ts_code="600001.SH",
        periods={},
        blocked_reasons=(),
        report_cutoff_at=CUTOFF,
        known_at=CUTOFF,
    )

    class Calculator:
        dividends: list[CorporateAction] = []

        def calculate(self, **kwargs: object) -> PilotMetricResult:
            self.dividends = kwargs["dividends"]  # type: ignore[assignment]
            return PilotMetricResult(
                metrics={},
                blocked_reasons=(),
                report_cutoff_at=CUTOFF,
                known_at=CUTOFF,
                tax_rate_proxy=Decimal("0.25"),
            )

    implemented = ImplementedDividend(
        record_id="implemented-dividend-1",
        source_id="sse",
        source_url="https://www.sse.com.cn/dividend",
        published_at=None,
        effective_at=CUTOFF - timedelta(days=30),
        collected_at=CUTOFF - timedelta(days=20),
        version="sse-implemented-v1",
        content_hash="d" * 64,
        license_policy="personal-non-commercial-research",
        quality_status=QualityStatus.VALID,
        valid_from=CUTOFF - timedelta(days=20),
        ts_code="600001.SH",
        record_date=(CUTOFF - timedelta(days=31)).date(),
        ex_date=(CUTOFF - timedelta(days=30)).date(),
        cash_dividend_per_share=Decimal("0.80"),
    )
    calculator = Calculator()
    analyzer = PilotFinancialAnalyzer(
        assembler=SimpleNamespace(assemble=lambda **_kwargs: series),
        action_repository=SimpleNamespace(visible_actions=lambda *_args: ()),
        implemented_dividend_warehouse=SimpleNamespace(
            read_records=lambda _date: [implemented]
        ),
        metric_calculator=calculator,
        market_warehouse=SimpleNamespace(
            read_bars=lambda _date: [
                SimpleNamespace(ts_code="600001.SH", close=Decimal("10"))
            ]
        ),
    )

    analyzer.calculate(
        universe=SimpleNamespace(members=[SimpleNamespace(ts_code="600001.SH")]),
        market_date=date(2026, 7, 22),
        report_cutoff_at=CUTOFF,
        known_at=CUTOFF,
    )

    assert len(calculator.dividends) == 1
    bridged = calculator.dividends[0]
    assert bridged.record_id == implemented.record_id
    assert bridged.cash_dividend_per_share == Decimal("0.80")
    assert bridged.cash_dividend_total is None
    assert bridged.fiscal_year == implemented.ex_date.year - 1
    assert bridged.published_at == implemented.effective_at
