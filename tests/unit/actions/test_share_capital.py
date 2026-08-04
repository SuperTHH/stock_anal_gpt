from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

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

CUTOFF = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
HASH = "a" * 64


def baseline() -> FinancialFact:
    return FinancialFact(
        record_id="fact-shares-baseline",
        fact_id="fact-shares-baseline",
        source_id="sse",
        source_url="https://www.sse.com.cn/disclosure/fixture.xbrl",
        published_at=CUTOFF - timedelta(days=100),
        effective_at=datetime(
            2025,
            12,
            31,
            23,
            59,
            59,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        collected_at=CUTOFF - timedelta(days=90),
        version="fixture-v1",
        content_hash=HASH,
        license_policy="fixture-only",
        quality_status=QualityStatus.VALID,
        supersedes_id=None,
        valid_from=CUTOFF - timedelta(days=90),
        ts_code="699999.SH",
        report_period=date(2025, 12, 31),
        report_type=ReportType.ANNUAL,
        announcement_at=CUTOFF - timedelta(days=100),
        statement_type=StatementType.BALANCE_SHEET,
        taxonomy=("fixture",),
        fact_name="TotalShares",
        raw_qname="{urn:fixture}TotalShares",
        canonical_fact_name="total_shares",
        mapping_status=MappingStatus.MAPPED,
        fact_value=Decimal("1000.25"),
        unit="shares",
        currency=None,
        filing_id="filing-baseline",
        context_signature="context",
        entity_scheme="urn:fixture",
        entity_identifier="699999.SH",
        period_start=None,
        period_end=None,
        instant=date(2025, 12, 31),
        unit_signature="unit",
        decimals="2",
        consolidation_scope=ConsolidationScope.CONSOLIDATED,
        dimensions={},
        fact_identity_hash="b" * 64,
        comparison_identity_hash="c" * 64,
    )


def action(
    action_type: ActionType,
    record_id: str,
    *,
    effective_at: datetime,
    published_at: datetime | None = None,
    **terms: object,
) -> CorporateAction:
    payload: dict[str, object] = {
        "record_id": record_id,
        "source_id": "sse",
        "source_url": "https://www.sse.com.cn/disclosure/fixture-action.pdf",
        "published_at": published_at or effective_at - timedelta(days=10),
        "effective_at": effective_at,
        "collected_at": effective_at - timedelta(days=5),
        "version": f"version-{record_id}",
        "content_hash": HASH,
        "license_policy": "fixture-only",
        "quality_status": QualityStatus.VALID,
        "supersedes_id": None,
        "valid_from": effective_at - timedelta(days=5),
        "ts_code": "699999.SH",
        "action_type": action_type,
        "record_date": effective_at.date() - timedelta(days=1),
        "ex_date": effective_at.date(),
        "pay_date": effective_at.date() + timedelta(days=3),
        "cash_dividend_per_share": None,
        "stock_dividend_ratio": None,
        "split_ratio": None,
        "rights_ratio": None,
        "rights_price": None,
        "share_reduction": None,
        "action_status": ActionStatus.IMPLEMENTED,
    }
    payload.update(terms)
    return CorporateAction.model_validate(payload)


def test_resolves_stock_split_rights_buyback_and_ignores_cash_dividend() -> None:
    same_day = CUTOFF - timedelta(days=2)
    actions = [
        action(
            ActionType.CASH_DIVIDEND,
            "cash",
            effective_at=same_day,
            cash_dividend_per_share=Decimal("0.5"),
        ),
        action(
            ActionType.RIGHTS_ISSUE,
            "rights",
            effective_at=same_day,
            rights_ratio=Decimal("0.1"),
            rights_price=Decimal("8"),
        ),
        action(
            ActionType.BUYBACK_CANCELLATION,
            "buyback",
            effective_at=same_day,
            share_reduction=Decimal("10.125"),
        ),
        action(
            ActionType.SPLIT,
            "split",
            effective_at=same_day,
            split_ratio=Decimal("2"),
        ),
        action(
            ActionType.STOCK_DIVIDEND,
            "stock",
            effective_at=same_day,
            stock_dividend_ratio=Decimal("0.2"),
        ),
    ]

    result = ShareCapitalResolver("share-capital-v1").resolve(
        baseline_fact=baseline(),
        actions=list(reversed(actions)),
        as_of=CUTOFF,
        known_at=CUTOFF,
    )

    assert result.blocked_reasons == ()
    assert result.total_shares == Decimal("2630.535")
    assert result.baseline_fact_id == "fact-shares-baseline"
    assert result.action_record_ids == (
        "stock",
        "split",
        "rights",
        "buyback",
        "cash",
    )
    assert result.algorithm_version == "share-capital-v1"


def test_resolves_from_assembled_point_in_time_share_value() -> None:
    effective_at = CUTOFF - timedelta(days=2)
    result = ShareCapitalResolver("share-capital-v1").resolve_value(
        ts_code="699999.SH",
        baseline_value=Decimal("1000.25"),
        baseline_date=date(2025, 12, 31),
        baseline_fact_id="assembled-share-fact",
        actions=[
            action(
                ActionType.SPLIT,
                "split",
                effective_at=effective_at,
                split_ratio=Decimal("2"),
            )
        ],
        as_of=CUTOFF,
        known_at=CUTOFF,
    )

    assert result.total_shares == Decimal("2000.50")
    assert result.baseline_fact_id == "assembled-share-fact"
    assert result.action_record_ids == ("split",)
    assert result.blocked_reasons == ()


def test_future_publication_or_effective_time_never_enters_share_capital() -> None:
    visible = action(
        ActionType.STOCK_DIVIDEND,
        "visible",
        effective_at=CUTOFF - timedelta(days=1),
        stock_dividend_ratio=Decimal("0.1"),
    )
    future_publication = action(
        ActionType.SPLIT,
        "future-publication",
        effective_at=CUTOFF - timedelta(hours=1),
        published_at=CUTOFF + timedelta(seconds=1),
        split_ratio=Decimal("2"),
    )
    future_effective = action(
        ActionType.SPLIT,
        "future-effective",
        effective_at=CUTOFF + timedelta(seconds=1),
        published_at=CUTOFF - timedelta(days=1),
        split_ratio=Decimal("2"),
    )

    result = ShareCapitalResolver("share-capital-v1").resolve(
        baseline_fact=baseline(),
        actions=[future_effective, visible, future_publication],
        as_of=CUTOFF,
        known_at=CUTOFF,
    )

    assert result.total_shares == Decimal("1100.275")
    assert result.action_record_ids == ("visible",)


def test_chain_gap_duplicate_effect_and_negative_capital_block() -> None:
    same_day = CUTOFF - timedelta(days=1)
    missing_parent = action(
        ActionType.SPLIT,
        "missing-parent",
        effective_at=same_day,
        split_ratio=Decimal("2"),
    ).model_copy(update={"supersedes_id": "not-present"})
    duplicate_one = action(
        ActionType.STOCK_DIVIDEND,
        "duplicate-one",
        effective_at=same_day,
        stock_dividend_ratio=Decimal("0.1"),
    )
    duplicate_two = action(
        ActionType.STOCK_DIVIDEND,
        "duplicate-two",
        effective_at=same_day,
        stock_dividend_ratio=Decimal("0.2"),
    )
    excessive_buyback = action(
        ActionType.BUYBACK_CANCELLATION,
        "excessive",
        effective_at=same_day,
        share_reduction=Decimal("2000"),
    )
    resolver = ShareCapitalResolver("share-capital-v1")

    chain = resolver.resolve(
        baseline_fact=baseline(),
        actions=[missing_parent],
        as_of=CUTOFF,
        known_at=CUTOFF,
    )
    duplicate = resolver.resolve(
        baseline_fact=baseline(),
        actions=[duplicate_one, duplicate_two],
        as_of=CUTOFF,
        known_at=CUTOFF,
    )
    negative = resolver.resolve(
        baseline_fact=baseline(),
        actions=[excessive_buyback],
        as_of=CUTOFF,
        known_at=CUTOFF,
    )

    assert chain.blocked_reasons == ("SHARE_CAPITAL_CHAIN_GAP",)
    assert duplicate.blocked_reasons == ("SHARE_CAPITAL_DUPLICATE_EFFECT",)
    assert negative.blocked_reasons == ("SHARE_CAPITAL_NON_POSITIVE",)
    assert all(result.total_shares is None for result in (chain, duplicate, negative))
