from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from hengce.contracts.enums import ActionStatus, ActionType, QualityStatus
from hengce.contracts.market import CorporateAction, MarketBar

_BLOCKING_QUALITY = frozenset(
    {
        QualityStatus.CONFLICT,
        QualityStatus.MISSING,
        QualityStatus.REJECTED,
        QualityStatus.UNVERIFIED,
    }
)


@dataclass(frozen=True, slots=True)
class TotalReturnPoint:
    ts_code: str
    trade_date: date
    raw_close: Decimal
    total_return_index: Decimal
    algorithm_version: str
    action_record_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TotalReturnResult:
    points: tuple[TotalReturnPoint, ...]
    blocked_reasons: tuple[str, ...]


class TotalReturnCalculator:
    def __init__(self, algorithm_version: str) -> None:
        if not algorithm_version:
            raise ValueError("algorithm_version must not be empty")
        self.algorithm_version = algorithm_version

    def calculate(
        self,
        *,
        bars: list[MarketBar],
        actions: list[CorporateAction],
        as_of: datetime,
        known_at: datetime,
    ) -> TotalReturnResult:
        self._validate_cutoff(as_of)
        self._validate_cutoff(known_at)
        ordered_bars = sorted(bars, key=lambda item: item.trade_date)
        if not ordered_bars:
            return TotalReturnResult((), ())
        if len({bar.ts_code for bar in ordered_bars}) != 1:
            raise ValueError("TOTAL_RETURN_MIXED_SECURITIES")
        if len({bar.trade_date for bar in ordered_bars}) != len(ordered_bars):
            raise ValueError("TOTAL_RETURN_DUPLICATE_BAR")
        for bar in ordered_bars:
            if bar.trade_date > as_of.date() or (
                bar.effective_at is not None and bar.effective_at > as_of
            ):
                return TotalReturnResult((), ("MARKET_BAR_AFTER_CUTOFF",))
            if bar.collected_at > known_at or bar.valid_from > known_at:
                return TotalReturnResult((), ("MARKET_BAR_NOT_KNOWN_AT_CUTOFF",))
            if bar.quality_status not in {
                QualityStatus.VALID,
                QualityStatus.DERIVED,
            }:
                return TotalReturnResult((), ("MARKET_BAR_QUALITY_BLOCKED",))

        visible_actions: dict[str, CorporateAction] = {}
        for action in actions:
            if action.ts_code != ordered_bars[0].ts_code:
                raise ValueError("TOTAL_RETURN_MIXED_SECURITIES")
            if action.published_at is None or action.published_at > as_of:
                return TotalReturnResult((), ("CORPORATE_ACTION_PUBLICATION_AFTER_CUTOFF",))
            if action.valid_from > known_at:
                return TotalReturnResult((), ("CORPORATE_ACTION_NOT_KNOWN_AT_CUTOFF",))
            if action.quality_status in _BLOCKING_QUALITY:
                return TotalReturnResult((), ("CORPORATE_ACTION_QUALITY_BLOCKED",))
            visible_actions.setdefault(action.record_id, action)

        by_ex_date: dict[date, list[CorporateAction]] = {}
        for action in visible_actions.values():
            if action.action_status is not ActionStatus.CANCELLED:
                by_ex_date.setdefault(action.ex_date, []).append(action)

        index = Decimal(1)
        points = [
            TotalReturnPoint(
                ts_code=ordered_bars[0].ts_code,
                trade_date=ordered_bars[0].trade_date,
                raw_close=ordered_bars[0].close,
                total_return_index=index,
                algorithm_version=self.algorithm_version,
                action_record_ids=(),
            )
        ]
        previous_close = ordered_bars[0].close
        for current in ordered_bars[1:]:
            day_actions = sorted(
                by_ex_date.get(current.trade_date, ()),
                key=lambda item: item.record_id,
            )
            shares = Decimal(1)
            cash = Decimal(0)
            subscription_cost = Decimal(0)
            for action in day_actions:
                if action.action_type is ActionType.CASH_DIVIDEND:
                    cash += action.cash_dividend_per_share or Decimal(0)
                elif action.action_type is ActionType.STOCK_DIVIDEND:
                    shares *= Decimal(1) + (action.stock_dividend_ratio or Decimal(0))
                elif action.action_type is ActionType.SPLIT:
                    shares *= action.split_ratio or Decimal(1)
                elif action.action_type is ActionType.RIGHTS_ISSUE:
                    ratio = action.rights_ratio or Decimal(0)
                    shares += ratio
                    subscription_cost += ratio * (action.rights_price or Decimal(0))
            period_return = (current.close * shares + cash - subscription_cost) / previous_close
            index *= period_return
            points.append(
                TotalReturnPoint(
                    ts_code=current.ts_code,
                    trade_date=current.trade_date,
                    raw_close=current.close,
                    total_return_index=index,
                    algorithm_version=self.algorithm_version,
                    action_record_ids=tuple(action.record_id for action in day_actions),
                )
            )
            previous_close = current.close
        return TotalReturnResult(tuple(points), ())

    @staticmethod
    def _validate_cutoff(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("TOTAL_RETURN_CUTOFF_INVALID")
