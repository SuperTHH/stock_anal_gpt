from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from hengce.actions.share_capital import ShareCapitalResult
from hengce.contracts.enums import (
    ActionStatus,
    ActionType,
    ConsolidationScope,
    MappingStatus,
    QualityStatus,
    ReportType,
    StatementType,
)
from hengce.contracts.financial import FinancialFact, FinancialFiling
from hengce.contracts.market import CorporateAction
from hengce.financials.assembler import PointInTimeFinancialAssembler
from hengce.financials.metrics import PilotMetricCalculator
from hengce.financials.query import AsOfFinancialQuery
from hengce.state.financial_repository import FinancialFilingRepository
from hengce.state.repository import StateRepository
from hengce.warehouse.financial import FinancialFactWarehouse

CUTOFF = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
TS_CODE = "699999.SH"
PERIOD_VALUES: dict[date, dict[str, str]] = {
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
        "current_assets": "500",
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


def filing(period: date, fact_count: int, index: int) -> FinancialFiling:
    filing_id = f"filing-{period}"
    published_at = CUTOFF - timedelta(days=60 - index)
    collected_at = published_at + timedelta(hours=1)
    raw_hash = format(index + 1, "x") * 64
    report_type = (
        ReportType.ANNUAL
        if (period.month, period.day) == (12, 31)
        else ReportType.Q1
    )
    return FinancialFiling(
        record_id=filing_id,
        filing_id=filing_id,
        source_id="sse",
        source_url=f"https://www.sse.com.cn/disclosure/{filing_id}.xml",
        published_at=published_at,
        effective_at=datetime(
            period.year,
            period.month,
            period.day,
            15,
            59,
            59,
            tzinfo=UTC,
        ),
        collected_at=collected_at,
        version="fixture-v1:parser-v1:mapping-v1",
        content_hash=raw_hash,
        license_policy="fixture-only",
        quality_status=QualityStatus.VALID,
        supersedes_id=None,
        valid_from=collected_at,
        ts_code=TS_CODE,
        exchange="SSE",
        report_period=period,
        report_type=report_type,
        announcement_at=published_at,
        taxonomy=("fixture-taxonomy",),
        taxonomy_hashes=(raw_hash,),
        raw_object_hash=raw_hash,
        filing_version="fixture-v1",
        parser_name="fixture-parser",
        parser_version="parser-v1",
        mapping_version="mapping-v1",
        fact_count=fact_count,
        conflict_count=0,
        is_restated=False,
    )


def facts(owner: FinancialFiling, values: dict[str, str]) -> list[FinancialFact]:
    return [
        FinancialFact(
            record_id=f"{owner.report_period}:{name}",
            fact_id=f"{owner.report_period}:{name}",
            source_id="sse",
            source_url=owner.source_url,
            published_at=owner.published_at,
            effective_at=owner.effective_at,
            collected_at=owner.collected_at,
            version=owner.version,
            content_hash="a" * 64,
            license_policy="fixture-only",
            quality_status=QualityStatus.VALID,
            supersedes_id=None,
            valid_from=owner.valid_from,
            ts_code=TS_CODE,
            report_period=owner.report_period,
            report_type=owner.report_type,
            announcement_at=owner.announcement_at,
            statement_type=StatementType.OTHER,
            taxonomy=owner.taxonomy,
            fact_name=name,
            raw_qname=f"{{urn:fixture}}{name}",
            canonical_fact_name=name,
            mapping_status=MappingStatus.MAPPED,
            fact_value=Decimal(value),
            unit="CNY",
            currency="CNY",
            filing_id=owner.filing_id,
            context_signature=f"context-{owner.report_period}-{name}",
            entity_scheme="urn:fixture",
            entity_identifier=TS_CODE,
            period_start=date(owner.report_period.year, 1, 1),
            period_end=owner.report_period,
            instant=None,
            unit_signature="CNY",
            decimals="0",
            consolidation_scope=ConsolidationScope.CONSOLIDATED,
            dimensions={},
            fact_identity_hash="b" * 64,
            comparison_identity_hash="c" * 64,
        )
        for name, value in values.items()
    ]


def dividend(year: int, dps: str, total: str) -> CorporateAction:
    ex_date = date(year + 1, 6, 1)
    return CorporateAction(
        record_id=f"dividend-{year}",
        source_id="sse",
        source_url="https://www.sse.com.cn/disclosure/dividend.pdf",
        published_at=CUTOFF - timedelta(days=30),
        effective_at=datetime(year + 1, 6, 1, tzinfo=UTC),
        collected_at=CUTOFF - timedelta(days=29),
        version="fixture-v1",
        content_hash="d" * 64,
        license_policy="fixture-only",
        quality_status=QualityStatus.VALID,
        supersedes_id=None,
        valid_from=CUTOFF - timedelta(days=29),
        ts_code=TS_CODE,
        action_type=ActionType.CASH_DIVIDEND,
        record_date=ex_date - timedelta(days=1),
        ex_date=ex_date,
        pay_date=ex_date + timedelta(days=5),
        cash_dividend_per_share=Decimal(dps),
        cash_dividend_total=Decimal(total),
        fiscal_year=year,
        action_status=ActionStatus.IMPLEMENTED,
    )


def test_repository_warehouse_series_and_actions_produce_complete_pilot_metrics(
    tmp_path: Path,
) -> None:
    """Catches a unit-only implementation that cannot consume persisted fact artifacts."""
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()
    repository = FinancialFilingRepository(state.path)
    warehouse = FinancialFactWarehouse(tmp_path / "warehouse")
    for index, (period, values) in enumerate(PERIOD_VALUES.items()):
        owner = filing(period, len(values), index)
        artifact = warehouse.write_facts(owner, facts(owner, values))
        repository.stage_filing(
            owner,
            str(artifact.path),
            artifact.content_hash,
            artifact.fact_count,
        )
        assert repository.publish_filing(
            owner.filing_id,
            str(artifact.path),
            artifact.content_hash,
            artifact.fact_count,
        )

    series = PointInTimeFinancialAssembler(
        query=AsOfFinancialQuery(repository, warehouse.root),
        pdf_provider=lambda _code, _period: (),
    ).assemble(
        ts_code=TS_CODE,
        periods=tuple(PERIOD_VALUES),
        report_cutoff_at=CUTOFF,
        known_at=CUTOFF,
    )
    result = PilotMetricCalculator("pilot-financial-metrics-v1").calculate(
        series=series,
        closing_price=Decimal("24"),
        share_capital=ShareCapitalResult(
            total_shares=Decimal("100"),
            baseline_fact_id="shares-baseline",
            action_record_ids=(),
            algorithm_version="share-capital-v1",
            blocked_reasons=(),
        ),
        dividends=[
            dividend(2023, "0.8", "70"),
            dividend(2024, "1.0", "90"),
            dividend(2025, "0.8", "80"),
        ],
        report_cutoff_at=CUTOFF,
        known_at=CUTOFF,
    )

    assert series.blocked_reasons == ()
    assert len(series.periods) == 5
    assert result.blocked_reasons == ()
    assert all(
        metric.algorithm_version == "pilot-financial-metrics-v1"
        for metric in result.metrics.values()
    )
    assert result.metrics["market_cap"].value == Decimal("2400")
    assert result.metrics["pe"].value == Decimal("20")
    assert result.metrics["q1_revenue_growth"].value == Decimal("0.3")
    assert result.metrics["consecutive_dividend_years"].value == Decimal("3")
