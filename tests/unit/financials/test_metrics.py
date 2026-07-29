from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hengce.contracts.enums import QualityStatus
from hengce.financials.metrics import FinancialMetricCalculator, MarketValuationInput

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
