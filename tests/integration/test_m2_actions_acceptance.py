from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from hengce.actions.calculator import TotalReturnPoint
from hengce.actions.share_capital import ShareCapitalResolver
from hengce.contracts.enums import (
    ActionStatus,
    ActionType,
    ConsolidationScope,
    MappingStatus,
    QualityStatus,
    ReportType,
    StatementType,
)
from hengce.contracts.financial import FinancialFact
from hengce.contracts.market import CorporateAction
from hengce.state.action_repository import CorporateActionRepository
from hengce.state.repository import StateRepository
from hengce.warehouse.derived_market import DerivedMarketWarehouse


def test_total_return_artifact_is_content_addressed_and_replayable(tmp_path: Path) -> None:
    """Catches overwriting a derived history partition or publishing unverifiable rows."""
    warehouse = DerivedMarketWarehouse(tmp_path)
    points = [
        TotalReturnPoint(
            ts_code="699999.SH",
            trade_date=date(2026, 7, 17),
            raw_close=Decimal("10"),
            total_return_index=Decimal("1"),
            algorithm_version="total-return-v1",
            action_record_ids=(),
        ),
        TotalReturnPoint(
            ts_code="699999.SH",
            trade_date=date(2026, 7, 20),
            raw_close=Decimal("9.5"),
            total_return_index=Decimal("1.00"),
            algorithm_version="total-return-v1",
            action_record_ids=("cash-1",),
        ),
    ]

    first = warehouse.write_total_return(points)
    second = warehouse.write_total_return(points)

    assert first == second
    assert first.name.startswith("part-")
    assert warehouse.read_total_return("699999.SH", "total-return-v1") == [
        {
            "action_record_ids": [],
            "algorithm_version": "total-return-v1",
            "raw_close": Decimal("10.0"),
            "total_return_index": Decimal("1.00"),
            "trade_date": date(2026, 7, 17),
            "ts_code": "699999.SH",
        },
        {
            "action_record_ids": ["cash-1"],
            "algorithm_version": "total-return-v1",
            "raw_close": Decimal("9.5"),
            "total_return_index": Decimal("1.00"),
            "trade_date": date(2026, 7, 20),
            "ts_code": "699999.SH",
        },
    ]


def test_action_versions_replay_point_in_time_share_capital(tmp_path: Path) -> None:
    """Catches persistence and resolver disagreeing about correction visibility."""
    cutoff = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
    state_path = tmp_path / "state.sqlite3"
    StateRepository(state_path).migrate()
    repository = CorporateActionRepository(state_path)
    original = CorporateAction(
        record_id="stock-original",
        source_id="sse",
        source_url="https://www.sse.com.cn/disclosure/fixture-action.pdf",
        published_at=cutoff - timedelta(days=10),
        effective_at=cutoff - timedelta(days=2),
        collected_at=cutoff - timedelta(days=9),
        version="v1",
        content_hash="a" * 64,
        license_policy="fixture-only",
        quality_status=QualityStatus.VALID,
        supersedes_id=None,
        valid_from=cutoff - timedelta(days=9),
        ts_code="699999.SH",
        action_type=ActionType.STOCK_DIVIDEND,
        record_date=date(2026, 7, 19),
        ex_date=date(2026, 7, 20),
        pay_date=date(2026, 7, 25),
        stock_dividend_ratio=Decimal("0.1"),
        action_status=ActionStatus.IMPLEMENTED,
    )
    correction = original.model_copy(
        update={
            "record_id": "stock-correction",
            "published_at": cutoff - timedelta(days=5),
            "collected_at": cutoff - timedelta(days=4),
            "valid_from": cutoff - timedelta(days=4),
            "supersedes_id": original.record_id,
            "stock_dividend_ratio": Decimal("0.2"),
            "version": "v2",
        }
    )
    repository.save_version(original)
    repository.save_version(correction)
    baseline = FinancialFact(
        record_id="shares-2025",
        fact_id="shares-2025",
        source_id="sse",
        source_url="https://www.sse.com.cn/disclosure/fixture.xbrl",
        published_at=cutoff - timedelta(days=100),
        effective_at=datetime(2025, 12, 31, 15, 59, 59, tzinfo=UTC),
        collected_at=cutoff - timedelta(days=90),
        version="fixture-v1",
        content_hash="b" * 64,
        license_policy="fixture-only",
        quality_status=QualityStatus.VALID,
        supersedes_id=None,
        valid_from=cutoff - timedelta(days=90),
        ts_code="699999.SH",
        report_period=date(2025, 12, 31),
        report_type=ReportType.ANNUAL,
        announcement_at=cutoff - timedelta(days=100),
        statement_type=StatementType.BALANCE_SHEET,
        taxonomy=("fixture",),
        fact_name="TotalShares",
        raw_qname="{urn:fixture}TotalShares",
        canonical_fact_name="total_shares",
        mapping_status=MappingStatus.MAPPED,
        fact_value=Decimal("1000"),
        unit="shares",
        currency=None,
        filing_id="filing-2025",
        context_signature="context",
        entity_scheme="urn:fixture",
        entity_identifier="699999.SH",
        period_start=None,
        period_end=None,
        instant=date(2025, 12, 31),
        unit_signature="shares",
        decimals="0",
        consolidation_scope=ConsolidationScope.CONSOLIDATED,
        dimensions={},
        fact_identity_hash="c" * 64,
        comparison_identity_hash="d" * 64,
    )

    result = ShareCapitalResolver("share-capital-v1").resolve(
        baseline_fact=baseline,
        actions=repository.visible_actions("699999.SH", cutoff, cutoff),
        as_of=cutoff,
        known_at=cutoff,
    )

    assert result.blocked_reasons == ()
    assert result.total_shares == Decimal("1200")
    assert result.action_record_ids == ("stock-correction",)
