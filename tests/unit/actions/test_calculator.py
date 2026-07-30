from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from hengce.actions.calculator import TotalReturnCalculator
from hengce.contracts.enums import ActionStatus, ActionType, QualityStatus
from hengce.contracts.market import CorporateAction, MarketBar

NOW = datetime(2026, 7, 29, 12, tzinfo=UTC)
HASH = "a" * 64


def bar(trade_date: date, close: str, pre_close: str) -> MarketBar:
    value = Decimal(close)
    return MarketBar(
        record_id=f"bar-{trade_date}",
        source_id="tushare",
        source_url="http://api.tushare.pro",
        published_at=None,
        effective_at=datetime.combine(trade_date, datetime.min.time(), UTC),
        collected_at=NOW,
        version="daily-v1",
        content_hash=HASH,
        license_policy="tushare-structured-only",
        quality_status=QualityStatus.VALID,
        valid_from=NOW,
        ts_code="699999.SH",
        trade_date=trade_date,
        open=value,
        high=value,
        low=value,
        close=value,
        pre_close=Decimal(pre_close),
        volume=Decimal("1000000"),
        amount=Decimal("100000000"),
    )


def action(action_type: ActionType, **updates: object) -> CorporateAction:
    payload: dict[str, object] = {
        "record_id": f"action-{action_type}",
        "source_id": "sse",
        "source_url": "https://www.sse.com.cn/disclosure/action.pdf",
        "published_at": NOW - timedelta(days=30),
        "effective_at": NOW - timedelta(days=10),
        "collected_at": NOW,
        "version": "official-v1",
        "content_hash": HASH,
        "license_policy": "sse-personal-research",
        "quality_status": QualityStatus.VALID,
        "valid_from": NOW - timedelta(days=20),
        "ts_code": "699999.SH",
        "action_type": action_type,
        "record_date": date(2026, 7, 19),
        "ex_date": date(2026, 7, 20),
        "pay_date": date(2026, 7, 25),
        "cash_dividend_per_share": Decimal("0.50")
        if action_type is ActionType.CASH_DIVIDEND
        else None,
        "stock_dividend_ratio": Decimal("0.20")
        if action_type is ActionType.STOCK_DIVIDEND
        else None,
        "split_ratio": Decimal("2") if action_type is ActionType.SPLIT else None,
        "rights_ratio": Decimal("0.30") if action_type is ActionType.RIGHTS_ISSUE else None,
        "rights_price": Decimal("8") if action_type is ActionType.RIGHTS_ISSUE else None,
        "action_status": ActionStatus.IMPLEMENTED,
    }
    payload.update(updates)
    return CorporateAction.model_validate(payload)


def test_cash_dividend_keeps_total_return_continuous_without_mutating_raw_close() -> None:
    """Catches calculating return from the ex-date price drop without adding cash received."""
    bars = [
        bar(date(2026, 7, 17), "10.00", "9.90"),
        bar(date(2026, 7, 20), "9.50", "10.00"),
    ]

    result = TotalReturnCalculator("total-return-v1").calculate(
        bars=bars,
        actions=[action(ActionType.CASH_DIVIDEND)],
        as_of=NOW,
        known_at=NOW,
    )

    assert result.blocked_reasons == ()
    assert result.points[0].total_return_index == Decimal("1")
    assert result.points[1].total_return_index == Decimal("1.00")
    assert result.points[1].raw_close == Decimal("9.50")
    assert bars[1].close == Decimal("9.50")


def test_stock_dividend_and_rights_issue_use_per_old_share_economics() -> None:
    """Catches ignoring new shares or treating paid rights shares as free distributions."""
    bars = [
        bar(date(2026, 7, 17), "10", "10"),
        bar(date(2026, 7, 20), "8", "10"),
    ]
    stock = TotalReturnCalculator("total-return-v1").calculate(
        bars=bars,
        actions=[action(ActionType.STOCK_DIVIDEND)],
        as_of=NOW,
        known_at=NOW,
    )
    rights = TotalReturnCalculator("total-return-v1").calculate(
        bars=bars,
        actions=[action(ActionType.RIGHTS_ISSUE)],
        as_of=NOW,
        known_at=NOW,
    )

    assert stock.points[-1].total_return_index == Decimal("0.96")
    assert rights.points[-1].total_return_index == Decimal("0.80")


def test_future_or_conflicting_action_blocks_instead_of_leaking_into_history() -> None:
    """Catches silently using a future correction or computing through an unresolved action."""
    bars = [
        bar(date(2026, 7, 17), "10", "10"),
        bar(date(2026, 7, 20), "9.5", "10"),
    ]
    future = action(ActionType.CASH_DIVIDEND, published_at=NOW + timedelta(seconds=1))
    conflict = action(
        ActionType.CASH_DIVIDEND,
        record_id="action-conflict",
        quality_status=QualityStatus.CONFLICT,
    )

    future_result = TotalReturnCalculator("total-return-v1").calculate(
        bars=bars,
        actions=[future],
        as_of=NOW,
        known_at=NOW,
    )
    conflict_result = TotalReturnCalculator("total-return-v1").calculate(
        bars=bars,
        actions=[conflict],
        as_of=NOW,
        known_at=NOW,
    )

    assert future_result.blocked_reasons == ("CORPORATE_ACTION_PUBLICATION_AFTER_CUTOFF",)
    assert future_result.points == ()
    assert conflict_result.blocked_reasons == ("CORPORATE_ACTION_QUALITY_BLOCKED",)
    assert conflict_result.points == ()


def test_duplicate_action_identity_is_applied_only_once() -> None:
    """Catches a retried ingestion doubling the cash distribution."""
    bars = [
        bar(date(2026, 7, 17), "10", "10"),
        bar(date(2026, 7, 20), "9.5", "10"),
    ]
    dividend = action(ActionType.CASH_DIVIDEND)

    result = TotalReturnCalculator("total-return-v1").calculate(
        bars=bars,
        actions=[dividend, dividend],
        as_of=NOW,
        known_at=NOW,
    )

    assert result.points[-1].total_return_index == Decimal("1.00")


def test_visible_correction_replaces_old_action_in_total_return() -> None:
    """Catches applying both an original dividend and its append-only correction."""
    bars = [
        bar(date(2026, 7, 17), "10", "10"),
        bar(date(2026, 7, 20), "9.4", "10"),
    ]
    original = action(ActionType.CASH_DIVIDEND, record_id="dividend-original")
    correction = action(
        ActionType.CASH_DIVIDEND,
        record_id="dividend-correction",
        cash_dividend_per_share=Decimal("0.6"),
        published_at=NOW - timedelta(days=5),
        valid_from=NOW - timedelta(days=4),
        supersedes_id=original.record_id,
    )

    result = TotalReturnCalculator("total-return-v1").calculate(
        bars=bars,
        actions=[original, correction],
        as_of=NOW,
        known_at=NOW,
    )

    assert result.blocked_reasons == ()
    assert result.points[-1].total_return_index == Decimal("1.00")
    assert result.points[-1].action_record_ids == ("dividend-correction",)


def test_buyback_cancellation_does_not_mutate_raw_price_or_holder_return() -> None:
    """Catches treating an issuer share cancellation as a holder distribution."""
    bars = [
        bar(date(2026, 7, 17), "10", "10"),
        bar(date(2026, 7, 20), "10", "10"),
    ]
    buyback = action(
        ActionType.BUYBACK_CANCELLATION,
        record_id="buyback-cancellation",
        cash_dividend_per_share=None,
        share_reduction=Decimal("1000000"),
    )

    result = TotalReturnCalculator("total-return-v1").calculate(
        bars=bars,
        actions=[buyback],
        as_of=NOW,
        known_at=NOW,
    )

    assert result.blocked_reasons == ()
    assert result.points[-1].total_return_index == Decimal("1")
    assert result.points[-1].raw_close == Decimal("10")
    assert bars[-1].close == Decimal("10")


def test_future_or_late_known_market_bar_blocks_total_return() -> None:
    future_trade = bar(date(2026, 7, 30), "10", "10")
    late_known = bar(date(2026, 7, 20), "10", "10").model_copy(
        update={"valid_from": NOW + timedelta(seconds=1)}
    )

    future_result = TotalReturnCalculator("total-return-v1").calculate(
        bars=[future_trade],
        actions=[],
        as_of=NOW,
        known_at=NOW,
    )
    late_result = TotalReturnCalculator("total-return-v1").calculate(
        bars=[late_known],
        actions=[],
        as_of=NOW,
        known_at=NOW,
    )

    assert future_result.blocked_reasons == ("MARKET_BAR_AFTER_CUTOFF",)
    assert late_result.blocked_reasons == ("MARKET_BAR_NOT_KNOWN_AT_CUTOFF",)
