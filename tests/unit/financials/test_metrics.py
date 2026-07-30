from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from hengce.actions.share_capital import ShareCapitalResult
from hengce.contracts.enums import (
    ActionStatus,
    ActionType,
    QualityStatus,
)
from hengce.contracts.market import CorporateAction
from hengce.financials.assembler import (
    AssembledFinancialFact,
    FinancialPeriodSnapshot,
    FinancialSeriesResult,
)
from hengce.financials.metrics import (
    FinancialMetricCalculator,
    MarketValuationInput,
    PilotMetricCalculator,
)

AS_OF = datetime(2026, 7, 29, 13, tzinfo=UTC)


def fact(
    name: str,
    value: str,
    *,
    quality: str = "VALID",
    published_at: datetime = AS_OF - timedelta(days=1),
    valid_from: datetime = AS_OF - timedelta(hours=1),
) -> dict[str, object]:
    return {
        "fact_id": f"{name}-1",
        "canonical_fact_name": name,
        "fact_value": Decimal(value),
        "quality_status": quality,
        "published_at": published_at,
        "effective_at": AS_OF - timedelta(days=365),
        "collected_at": valid_from,
        "valid_from": valid_from,
    }


def market_cap(value: str = "2400", **updates: object) -> MarketValuationInput:
    payload: dict[str, object] = {
        "record_id": "market-cap-1",
        "value": Decimal(value),
        "effective_at": AS_OF - timedelta(hours=5),
        "collected_at": AS_OF - timedelta(hours=1),
        "valid_from": AS_OF - timedelta(hours=1),
        "quality_status": QualityStatus.DERIVED,
    }
    payload.update(updates)
    return MarketValuationInput(**payload)


def test_calculates_research_metrics_with_literal_expected_values() -> None:
    """Catches swapped denominators and treating capital expenditure as an inflow."""
    facts = [
        fact("net_profit", "120"),
        fact("equity", "800"),
        fact("beginning_equity", "700"),
        fact("nopat", "105"),
        fact("invested_capital", "750"),
        fact("revenue", "1500"),
        fact("prior_revenue", "1200"),
        fact("operating_cash_flow", "180"),
        fact("capital_expenditure", "60"),
        fact("total_liabilities", "400"),
        fact("total_assets", "1200"),
    ]

    result = FinancialMetricCalculator("financial-metrics-v1").calculate(
        facts=facts,
        market_cap=market_cap(),
        as_of=AS_OF,
        known_at=AS_OF,
    )

    assert result.blocked_reasons == ()
    assert result.metrics["roe"].value == Decimal("0.16")
    assert result.metrics["roic"].value == Decimal("0.14")
    assert result.metrics["revenue_growth"].value == Decimal("0.25")
    assert result.metrics["cash_flow_quality"].value == Decimal("1.5")
    assert result.metrics["debt_ratio"].value == Decimal("0.3333333333333333333333333333")
    assert result.metrics["free_cash_flow"].value == Decimal("120")
    assert result.metrics["pe"].value == Decimal("20")
    assert result.metrics["pb"].value == Decimal("3")
    assert result.metrics["fcf_yield"].value == Decimal("0.05")
    assert result.metrics["roe"].input_fact_ids == (
        "beginning_equity-1",
        "equity-1",
        "net_profit-1",
    )


def test_negative_profit_does_not_create_a_low_pe_score_input() -> None:
    """Catches ranking a loss-making company as cheap because its PE is negative."""
    result = FinancialMetricCalculator("financial-metrics-v1").calculate(
        facts=[
            fact("net_profit", "-10"),
            fact("equity", "100"),
            fact("operating_cash_flow", "5"),
            fact("capital_expenditure", "1"),
        ],
        market_cap=market_cap("200"),
        as_of=AS_OF,
        known_at=AS_OF,
    )

    assert result.metrics["pe"].value is None
    assert result.metrics["pe"].quality_status is QualityStatus.MISSING
    assert result.metrics["pe"].reason == "NON_POSITIVE_EARNINGS"


def test_conflicting_input_blocks_all_metrics_instead_of_selecting_one_value() -> None:
    """Catches deriving a clean-looking ratio from an unresolved source conflict."""
    result = FinancialMetricCalculator("financial-metrics-v1").calculate(
        facts=[
            fact("net_profit", "10"),
            fact("equity", "100", quality="CONFLICT"),
        ],
        market_cap=market_cap("200"),
        as_of=AS_OF,
        known_at=AS_OF,
    )

    assert result.metrics == {}
    assert result.blocked_reasons == ("FINANCIAL_INPUT_QUALITY_BLOCKED",)


def test_missing_or_zero_denominator_is_explicit_not_zero_filled() -> None:
    """Catches fabricating a zero ratio when the denominator is absent or zero."""
    result = FinancialMetricCalculator("financial-metrics-v1").calculate(
        facts=[
            fact("net_profit", "10"),
            fact("equity", "0"),
        ],
        market_cap=market_cap("200"),
        as_of=AS_OF,
        known_at=AS_OF,
    )

    assert result.metrics["roe"].value is None
    assert result.metrics["roe"].quality_status is QualityStatus.MISSING
    assert result.metrics["roe"].reason == "DENOMINATOR_MISSING_OR_ZERO"


def test_future_fact_or_market_valuation_is_blocked_before_metric_calculation() -> None:
    future_fact = FinancialMetricCalculator("financial-metrics-v1").calculate(
        facts=[fact("net_profit", "10", published_at=AS_OF + timedelta(seconds=1))],
        market_cap=market_cap("200"),
        as_of=AS_OF,
        known_at=AS_OF,
    )
    future_market = FinancialMetricCalculator("financial-metrics-v1").calculate(
        facts=[fact("net_profit", "10"), fact("equity", "100")],
        market_cap=market_cap(
            "200",
            valid_from=AS_OF + timedelta(seconds=1),
        ),
        as_of=AS_OF,
        known_at=AS_OF,
    )

    assert future_fact.blocked_reasons == ("FINANCIAL_INPUT_AFTER_CUTOFF",)
    assert future_market.blocked_reasons == ("MARKET_VALUATION_NOT_KNOWN_AT_CUTOFF",)


def assembled_fact(period: date, name: str, value: str) -> AssembledFinancialFact:
    return AssembledFinancialFact(
        canonical_fact_name=name,
        value=Decimal(value),
        input_record_id=f"{period}:{name}",
        filing_id=f"filing-{period}",
        source_kind="XBRL",
        published_at=AS_OF - timedelta(days=30),
        collected_at=AS_OF - timedelta(days=29),
        valid_from=AS_OF - timedelta(days=29),
        quality_status=QualityStatus.VALID,
    )


def financial_series(
    *,
    updates: dict[date, dict[str, str | None]] | None = None,
    blocked_reasons: tuple[str, ...] = (),
) -> FinancialSeriesResult:
    values: dict[date, dict[str, str]] = {
        date(2023, 12, 31): {
            "revenue": "1000",
            "operating_cost": "600",
            "adjusted_net_profit": "80",
        },
        date(2024, 12, 31): {
            "revenue": "1200",
            "operating_cost": "720",
            "adjusted_net_profit": "100",
            "equity": "600",
            "interest_bearing_debt": "300",
            "cash_and_equivalents": "100",
        },
        date(2025, 3, 31): {
            "revenue": "300",
            "adjusted_net_profit": "20",
        },
        date(2025, 12, 31): {
            "revenue": "1500",
            "operating_cost": "870",
            "net_profit": "120",
            "adjusted_net_profit": "130",
            "operating_cash_flow": "180",
            "capital_expenditure": "60",
            "total_assets": "1200",
            "total_liabilities": "400",
            "interest_bearing_debt": "240",
            "cash_and_equivalents": "120",
            "equity": "800",
            "interest_expense": "20",
        },
        date(2026, 3, 31): {
            "revenue": "390",
            "adjusted_net_profit": "30",
        },
    }
    for period, changes in (updates or {}).items():
        for name, value in changes.items():
            if value is None:
                values[period].pop(name, None)
            else:
                values[period][name] = value
    snapshots = {
        period: FinancialPeriodSnapshot(
            report_period=period,
            report_kind=(
                "ANNUAL" if (period.month, period.day) == (12, 31) else "Q1"
            ),
            source_kind="XBRL",
            filing_id=f"filing-{period}",
            facts={
                name: assembled_fact(period, name, value)
                for name, value in period_values.items()
            },
        )
        for period, period_values in values.items()
    }
    return FinancialSeriesResult(
        ts_code="699999.SH",
        periods=snapshots,
        blocked_reasons=blocked_reasons,
        report_cutoff_at=AS_OF,
        known_at=AS_OF,
    )


def share_capital(
    total_shares: str | None = "100",
    blocked_reasons: tuple[str, ...] = (),
) -> ShareCapitalResult:
    return ShareCapitalResult(
        total_shares=Decimal(total_shares) if total_shares is not None else None,
        baseline_fact_id="shares-baseline",
        action_record_ids=("share-action-1",),
        algorithm_version="share-capital-v1",
        blocked_reasons=blocked_reasons,
    )


def dividend(
    fiscal_year: int,
    dps: str,
    total: str,
    *,
    status: ActionStatus = ActionStatus.IMPLEMENTED,
    published_at: datetime = AS_OF - timedelta(days=10),
    record_id: str | None = None,
) -> CorporateAction:
    ex_date = date(fiscal_year + 1, 6, 1)
    return CorporateAction(
        record_id=record_id or f"dividend-{fiscal_year}",
        source_id="sse",
        source_url="https://www.sse.com.cn/disclosure/fixture-dividend.pdf",
        published_at=published_at,
        effective_at=datetime(fiscal_year + 1, 6, 1, tzinfo=UTC),
        collected_at=min(published_at + timedelta(hours=1), AS_OF),
        version="fixture-v1",
        content_hash="a" * 64,
        license_policy="fixture-only",
        quality_status=QualityStatus.VALID,
        supersedes_id=None,
        valid_from=min(published_at + timedelta(hours=1), AS_OF),
        ts_code="699999.SH",
        action_type=ActionType.CASH_DIVIDEND,
        record_date=ex_date - timedelta(days=1),
        ex_date=ex_date,
        pay_date=ex_date + timedelta(days=5),
        cash_dividend_per_share=Decimal(dps),
        cash_dividend_total=Decimal(total),
        fiscal_year=fiscal_year,
        action_status=status,
    )


def test_pilot_metrics_follow_approved_formulas_and_preserve_lineage() -> None:
    """Catches formula drift, Q1 annualization, float rounding, and lost evidence IDs."""
    result = PilotMetricCalculator("pilot-financial-metrics-v1").calculate(
        series=financial_series(),
        closing_price=Decimal("24"),
        share_capital=share_capital(),
        dividends=[
            dividend(2023, "0.8", "70"),
            dividend(2024, "1.0", "90"),
            dividend(2025, "0.8", "80"),
        ],
        report_cutoff_at=AS_OF,
        known_at=AS_OF,
    )

    assert result.blocked_reasons == ()
    assert result.tax_rate_proxy == Decimal("0.25")
    assert result.metrics["roe_2025"].value == Decimal("120") / Decimal("700")
    assert result.metrics["roic_2025"].value == Decimal("135") / Decimal("860")
    assert result.metrics["annual_revenue_growth"].value == Decimal("0.25")
    assert result.metrics["q1_revenue_growth"].value == Decimal("0.3")
    assert result.metrics["annual_adjusted_profit_growth"].value == Decimal("0.3")
    assert result.metrics["q1_adjusted_profit_growth"].value == Decimal("0.5")
    assert result.metrics["cash_flow_quality"].value == Decimal("1.5")
    assert result.metrics["gross_margin_stability"].value.quantize(
        Decimal("0.000001")
    ) == Decimal("0.009428")
    assert result.metrics["debt_ratio"].value == Decimal("1") / Decimal("3")
    assert result.metrics["interest_bearing_debt_ratio"].value == Decimal("0.2")
    assert result.metrics["cash_debt_coverage"].value == Decimal("0.5")
    assert result.metrics["free_cash_flow"].value == Decimal("120")
    assert result.metrics["market_cap"].value == Decimal("2400")
    assert result.metrics["pe"].value == Decimal("20")
    assert result.metrics["pb"].value == Decimal("3")
    assert result.metrics["fcf_yield"].value == Decimal("0.05")
    assert result.metrics["consecutive_dividend_years"].value == Decimal("3")
    assert result.metrics["announced_dividend_yield"].value == Decimal("1") / Decimal(
        "30"
    )
    assert result.metrics["payout_ratio"].value == Decimal("2") / Decimal("3")
    assert result.metrics["fcf_coverage"].value == Decimal("1.5")
    assert result.metrics["dividend_cut_flag"].value == Decimal("1")
    assert result.metrics["roic_2025"].algorithm_version == (
        "pilot-financial-metrics-v1"
    )
    assert set(result.metrics["roic_2025"].input_fact_ids) == {
        "2024-12-31:cash_and_equivalents",
        "2024-12-31:equity",
        "2024-12-31:interest_bearing_debt",
        "2025-12-31:cash_and_equivalents",
        "2025-12-31:equity",
        "2025-12-31:interest_bearing_debt",
        "2025-12-31:interest_expense",
        "2025-12-31:net_profit",
    }


def test_pilot_metrics_return_missing_for_invalid_denominators_and_inputs() -> None:
    """Catches infinity, negative-value PE/PB, zero-debt ratios, and estimated FCF."""
    result = PilotMetricCalculator("pilot-financial-metrics-v1").calculate(
        series=financial_series(
            updates={
                date(2025, 12, 31): {
                    "net_profit": "-1",
                    "equity": "0",
                    "interest_bearing_debt": "0",
                    "capital_expenditure": None,
                }
            }
        ),
        closing_price=Decimal("24"),
        share_capital=share_capital(),
        dividends=[],
        report_cutoff_at=AS_OF,
        known_at=AS_OF,
    )

    assert result.metrics["pe"].value is None
    assert result.metrics["pe"].reason == "NON_POSITIVE_EARNINGS"
    assert result.metrics["pb"].value is None
    assert result.metrics["pb"].reason == "NON_POSITIVE_EQUITY"
    assert result.metrics["cash_debt_coverage"].value is None
    assert result.metrics["cash_debt_coverage"].reason == (
        "DENOMINATOR_MISSING_OR_ZERO"
    )
    assert result.metrics["free_cash_flow"].value is None
    assert result.metrics["free_cash_flow"].reason == "INPUT_MISSING"
    assert result.metrics["fcf_yield"].value is None


def test_blocked_share_capital_and_future_or_cancelled_dividends_do_not_leak() -> None:
    future = dividend(
        2025,
        "9",
        "900",
        published_at=AS_OF + timedelta(seconds=1),
        record_id="future-dividend",
    )
    cancelled = dividend(
        2025,
        "8",
        "800",
        status=ActionStatus.CANCELLED,
        record_id="cancelled-dividend",
    )
    result = PilotMetricCalculator("pilot-financial-metrics-v1").calculate(
        series=financial_series(),
        closing_price=Decimal("24"),
        share_capital=share_capital(
            None,
            ("SHARE_CAPITAL_CHAIN_GAP",),
        ),
        dividends=[
            dividend(2024, "1", "90"),
            dividend(2025, "0.8", "80"),
            future,
            cancelled,
        ],
        report_cutoff_at=AS_OF,
        known_at=AS_OF,
    )

    assert result.metrics["market_cap"].value is None
    assert result.metrics["market_cap"].reason == "SHARE_CAPITAL_BLOCKED"
    assert result.metrics["pe"].value is None
    assert result.metrics["announced_dividend_yield"].value == Decimal("1") / Decimal(
        "30"
    )
    assert result.metrics["dividend_cut_flag"].value == Decimal("1")
    assert "future-dividend" not in result.metrics[
        "announced_dividend_yield"
    ].input_fact_ids
    assert "cancelled-dividend" not in result.metrics[
        "announced_dividend_yield"
    ].input_fact_ids


def test_blocked_financial_series_stops_all_pilot_metric_derivation() -> None:
    result = PilotMetricCalculator("pilot-financial-metrics-v1").calculate(
        series=financial_series(
            blocked_reasons=("2025-12-31:FINANCIAL_RESTATEMENT_UNUSABLE",)
        ),
        closing_price=Decimal("24"),
        share_capital=share_capital(),
        dividends=[],
        report_cutoff_at=AS_OF,
        known_at=AS_OF,
    )

    assert result.metrics == {}
    assert result.blocked_reasons == (
        "2025-12-31:FINANCIAL_RESTATEMENT_UNUSABLE",
    )
