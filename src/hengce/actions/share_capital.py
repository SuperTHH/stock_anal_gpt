from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from hengce.contracts.enums import (
    ActionStatus,
    ActionType,
    MappingStatus,
    QualityStatus,
)
from hengce.contracts.financial import FinancialFact
from hengce.contracts.market import CorporateAction

_ACTION_ORDER = {
    ActionType.STOCK_DIVIDEND: 0,
    ActionType.SPLIT: 1,
    ActionType.RIGHTS_ISSUE: 2,
    ActionType.BUYBACK_CANCELLATION: 3,
}
_SHARE_CHANGING_ACTIONS = frozenset(_ACTION_ORDER)
_USABLE_QUALITY = frozenset({QualityStatus.VALID, QualityStatus.DERIVED})


@dataclass(frozen=True, slots=True)
class ShareCapitalResult:
    total_shares: Decimal | None
    baseline_fact_id: str
    action_record_ids: tuple[str, ...]
    algorithm_version: str
    blocked_reasons: tuple[str, ...]


class ShareCapitalResolver:
    def __init__(self, algorithm_version: str) -> None:
        if not algorithm_version.strip():
            raise ValueError("SHARE_CAPITAL_ALGORITHM_VERSION_INVALID")
        self.algorithm_version = algorithm_version

    def resolve(
        self,
        *,
        baseline_fact: FinancialFact,
        actions: Sequence[CorporateAction],
        as_of: datetime,
        known_at: datetime,
    ) -> ShareCapitalResult:
        if not _aware(as_of) or not _aware(known_at):
            raise ValueError("SHARE_CAPITAL_CUTOFF_INVALID")
        baseline_error = self._baseline_error(baseline_fact, as_of, known_at)
        if baseline_error is not None:
            return self._blocked(baseline_fact.fact_id, baseline_error)

        return self.resolve_value(
            ts_code=baseline_fact.ts_code,
            baseline_value=baseline_fact.fact_value,
            baseline_date=baseline_fact.instant,
            baseline_fact_id=baseline_fact.fact_id,
            actions=actions,
            as_of=as_of,
            known_at=known_at,
        )

    def resolve_value(
        self,
        *,
        ts_code: str,
        baseline_value: Decimal,
        baseline_date: date,
        baseline_fact_id: str,
        actions: Sequence[CorporateAction],
        as_of: datetime,
        known_at: datetime,
    ) -> ShareCapitalResult:
        """Resolve from an already assembled, point-in-time share fact."""
        if not _aware(as_of) or not _aware(known_at):
            raise ValueError("SHARE_CAPITAL_CUTOFF_INVALID")
        if (
            baseline_date > as_of.date()
            or baseline_value <= 0
            or not baseline_fact_id
        ):
            return self._blocked(
                baseline_fact_id,
                "SHARE_CAPITAL_BASELINE_INVALID",
            )

        relevant = [
            action
            for action in actions
            if action.action_type in _SHARE_CHANGING_ACTIONS
            and self._is_visible_after_date(
                action,
                baseline_date,
                as_of,
                known_at,
            )
        ]
        if any(action.ts_code != ts_code for action in relevant):
            return self._blocked(
                baseline_fact_id,
                "SHARE_CAPITAL_MIXED_SECURITIES",
            )
        resolved, chain_error = self._resolve_versions(relevant)
        if chain_error is not None:
            return self._blocked(baseline_fact_id, chain_error)
        if any(action.quality_status not in _USABLE_QUALITY for action in resolved):
            return self._blocked(
                baseline_fact_id,
                "SHARE_CAPITAL_ACTION_QUALITY_BLOCKED",
            )

        implemented = tuple(
            action
            for action in resolved
            if action.action_status is ActionStatus.IMPLEMENTED
        )
        effect_keys: set[tuple[ActionType, datetime]] = set()
        for action in implemented:
            if action.effective_at is None:
                return self._blocked(
                    baseline_fact_id,
                    "SHARE_CAPITAL_ACTION_TIME_INVALID",
                )
            key = (action.action_type, action.effective_at)
            if key in effect_keys:
                return self._blocked(
                    baseline_fact_id,
                    "SHARE_CAPITAL_DUPLICATE_EFFECT",
                )
            effect_keys.add(key)

        ordered = sorted(
            implemented,
            key=lambda action: (
                action.effective_at,
                _ACTION_ORDER[action.action_type],
                action.record_id,
            ),
        )
        total_shares = baseline_value
        applied_ids: list[str] = []
        for action in ordered:
            applied_ids.append(action.record_id)
            if action.action_type is ActionType.STOCK_DIVIDEND:
                total_shares *= Decimal(1) + (
                    action.stock_dividend_ratio or Decimal(0)
                )
            elif action.action_type is ActionType.SPLIT:
                total_shares *= action.split_ratio or Decimal(1)
            elif action.action_type is ActionType.RIGHTS_ISSUE:
                total_shares *= Decimal(1) + (
                    action.rights_ratio or Decimal(0)
                )
            elif action.action_type is ActionType.BUYBACK_CANCELLATION:
                total_shares -= action.share_reduction or Decimal(0)
            if total_shares <= 0:
                return self._blocked(
                    baseline_fact_id,
                    "SHARE_CAPITAL_NON_POSITIVE",
                )
        return ShareCapitalResult(
            total_shares=total_shares,
            baseline_fact_id=baseline_fact_id,
            action_record_ids=tuple(applied_ids),
            algorithm_version=self.algorithm_version,
            blocked_reasons=(),
        )

    @staticmethod
    def _baseline_error(
        fact: FinancialFact,
        as_of: datetime,
        known_at: datetime,
    ) -> str | None:
        if (
            fact.canonical_fact_name != "total_shares"
            or fact.mapping_status is not MappingStatus.MAPPED
            or fact.quality_status not in _USABLE_QUALITY
            or fact.instant is None
            or fact.fact_value <= 0
        ):
            return "SHARE_CAPITAL_BASELINE_INVALID"
        if (
            fact.published_at is None
            or fact.published_at > as_of
            or fact.collected_at > known_at
            or fact.valid_from > known_at
        ):
            return "SHARE_CAPITAL_BASELINE_NOT_VISIBLE"
        return None

    @staticmethod
    def _is_visible_after_date(
        action: CorporateAction,
        baseline_date: date,
        as_of: datetime,
        known_at: datetime,
    ) -> bool:
        return bool(
            action.published_at is not None
            and action.published_at <= as_of
            and action.effective_at is not None
            and action.effective_at <= as_of
            and action.effective_at.date() > baseline_date
            and action.collected_at <= known_at
            and action.valid_from <= known_at
        )

    @staticmethod
    def _resolve_versions(
        actions: Sequence[CorporateAction],
    ) -> tuple[list[CorporateAction], str | None]:
        by_id = {action.record_id: action for action in actions}
        if len(by_id) != len(actions):
            actions = tuple(by_id.values())
        children: dict[str, list[CorporateAction]] = {}
        for action in actions:
            if action.supersedes_id is None:
                continue
            if action.supersedes_id not in by_id:
                return [], "SHARE_CAPITAL_CHAIN_GAP"
            children.setdefault(action.supersedes_id, []).append(action)
        if any(len(items) != 1 for items in children.values()):
            return [], "SHARE_CAPITAL_BRANCH_CONFLICT"
        superseded_ids = set(children)
        return (
            [
                action
                for action in actions
                if action.record_id not in superseded_ids
            ],
            None,
        )

    def _blocked(
        self,
        baseline_fact_id: str,
        reason: str,
    ) -> ShareCapitalResult:
        return ShareCapitalResult(
            total_shares=None,
            baseline_fact_id=baseline_fact_id,
            action_record_ids=(),
            algorithm_version=self.algorithm_version,
            blocked_reasons=(reason,),
        )


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


__all__ = ["ShareCapitalResolver", "ShareCapitalResult"]
