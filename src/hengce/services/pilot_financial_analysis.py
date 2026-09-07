from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal

from hengce.actions.share_capital import (
    ShareCapitalResolver,
    ShareCapitalResult,
)
from hengce.contracts.enums import ActionStatus, ActionType
from hengce.contracts.market import CorporateAction
from hengce.contracts.market_screen import ImplementedDividend
from hengce.contracts.pilot import PilotUniverseSnapshot
from hengce.financials.assembler import PointInTimeFinancialAssembler
from hengce.financials.metrics import PilotMetricCalculator, PilotMetricResult
from hengce.state.action_repository import CorporateActionRepository
from hengce.state.dividend_repository import AnnualDividendRepository
from hengce.warehouse.dividends import ImplementedDividendWarehouse
from hengce.warehouse.market import MarketWarehouse

PILOT_FINANCIAL_PERIODS = (
    date(2023, 12, 31),
    date(2024, 12, 31),
    date(2025, 3, 31),
    date(2025, 12, 31),
    date(2026, 3, 31),
)


class PilotFinancialAnalyzer:
    """Compute strategy metrics from point-in-time financial and action data."""

    def __init__(
        self,
        *,
        assembler: PointInTimeFinancialAssembler,
        action_repository: CorporateActionRepository,
        dividend_repository: AnnualDividendRepository | None = None,
        metric_calculator: PilotMetricCalculator,
        market_warehouse: MarketWarehouse,
        share_capital_resolver: ShareCapitalResolver | None = None,
        implemented_dividend_warehouse: ImplementedDividendWarehouse | None = None,
    ) -> None:
        self.assembler = assembler
        self.action_repository = action_repository
        repository_path = getattr(action_repository, "path", None)
        self.dividend_repository = dividend_repository or (
            AnnualDividendRepository(repository_path)
            if repository_path is not None
            else None
        )
        self.metric_calculator = metric_calculator
        self.market_warehouse = market_warehouse
        self.implemented_dividend_warehouse = implemented_dividend_warehouse
        self.share_capital_resolver = (
            share_capital_resolver
            or ShareCapitalResolver("pilot-share-capital-v1")
        )

    def calculate(
        self,
        *,
        universe: PilotUniverseSnapshot,
        market_date: date,
        report_cutoff_at: datetime,
        known_at: datetime,
    ) -> dict[str, PilotMetricResult]:
        bars = {
            str(bar["ts_code"] if isinstance(bar, Mapping) else bar.ts_code): bar
            for bar in self.market_warehouse.read_bars(market_date)
        }
        implemented_by_code: dict[str, list[ImplementedDividend]] = {}
        if self.implemented_dividend_warehouse is not None:
            for record in self.implemented_dividend_warehouse.read_records(market_date):
                implemented_by_code.setdefault(record.ts_code, []).append(record)
        results: dict[str, PilotMetricResult] = {}
        for member in universe.members:
            series = self.assembler.assemble(
                ts_code=member.ts_code,
                periods=PILOT_FINANCIAL_PERIODS,
                report_cutoff_at=report_cutoff_at,
                known_at=known_at,
            )
            actions = list(
                self.action_repository.visible_actions(
                    member.ts_code,
                    report_cutoff_at,
                    known_at,
                )
            )
            actions = self._merge_implemented_dividends(
                actions,
                implemented_by_code.get(member.ts_code, ()),
            )
            dividend_history = (
                list(
                    self.dividend_repository.visible_records(
                        member.ts_code,
                        report_cutoff_at,
                        known_at,
                    )
                )
                if self.dividend_repository is not None
                else []
            )
            share_capital = self._share_capital(
                series,
                member.ts_code,
                actions,
                report_cutoff_at,
                known_at,
            )
            bar = bars.get(member.ts_code)
            closing_price = (
                Decimal(
                    str(
                        bar["close"]
                        if isinstance(bar, Mapping)
                        else bar.close
                    )
                )
                if bar is not None
                else Decimal(0)
            )
            results[member.ts_code] = self.metric_calculator.calculate(
                series=series,
                closing_price=closing_price,
                share_capital=share_capital,
                dividends=actions,
                dividend_history=dividend_history,
                report_cutoff_at=report_cutoff_at,
                known_at=known_at,
            )
        return results

    @staticmethod
    def _merge_implemented_dividends(
        actions: list[CorporateAction],
        implemented: list[ImplementedDividend] | tuple[ImplementedDividend, ...],
    ) -> list[CorporateAction]:
        """Bridge exchange implementation facts without duplicating PDF actions."""
        existing_terms = {
            (action.record_date, action.ex_date, action.cash_dividend_per_share)
            for action in actions
            if action.action_type is ActionType.CASH_DIVIDEND
        }
        merged = list(actions)
        for record in implemented:
            terms = (
                record.record_date,
                record.ex_date,
                record.cash_dividend_per_share,
            )
            if terms in existing_terms:
                continue
            effective_at = record.effective_at or datetime.combine(
                record.ex_date,
                time.min,
                tzinfo=record.collected_at.tzinfo,
            )
            merged.append(
                CorporateAction(
                    record_id=record.record_id,
                    source_id=record.source_id,
                    source_url=record.source_url,
                    # The exchange implementation table does not expose an
                    # announcement timestamp.  The ex-date is a conservative
                    # point-in-time publication proxy: never earlier than the
                    # actual announcement and never later than implementation.
                    published_at=record.published_at or effective_at,
                    effective_at=effective_at,
                    collected_at=record.collected_at,
                    version=f"{record.version}:implemented-dividend-bridge-v1",
                    content_hash=record.content_hash,
                    license_policy=record.license_policy,
                    quality_status=record.quality_status,
                    supersedes_id=record.supersedes_id,
                    valid_from=record.valid_from,
                    ts_code=record.ts_code,
                    action_type=ActionType.CASH_DIVIDEND,
                    record_date=record.record_date,
                    ex_date=record.ex_date,
                    cash_dividend_per_share=record.cash_dividend_per_share,
                    cash_dividend_total=None,
                    # The current exchange implementation tables have no fiscal
                    # year field.  Annual cash distributions are conservatively
                    # mapped to the preceding fiscal year; explicit annual-report
                    # records remain authoritative when available.
                    fiscal_year=record.ex_date.year - 1,
                    action_status=ActionStatus.IMPLEMENTED,
                )
            )
            existing_terms.add(terms)
        return merged

    def _share_capital(
        self,
        series: object,
        ts_code: str,
        actions: list[object],
        report_cutoff_at: datetime,
        known_at: datetime,
    ) -> ShareCapitalResult:
        periods = getattr(series, "periods", {})
        # Prefer the latest audited annual share baseline. Quarterly balance sheets
        # can expose accounting share capital that is not the listed share count
        # (notably after reverse mergers); subsequent corporate actions then adjust
        # the audited baseline to the report cutoff.
        ordered_periods = sorted(
            periods,
            key=lambda period: (
                period.month == 12 and period.day == 31,
                period,
            ),
            reverse=True,
        )
        for period in ordered_periods:
            snapshot = periods[period]
            fact = snapshot.facts.get("total_shares")
            if fact is None:
                continue
            return self.share_capital_resolver.resolve_value(
                ts_code=ts_code,
                baseline_value=fact.value,
                baseline_date=period,
                baseline_fact_id=fact.input_record_id,
                actions=actions,
                as_of=report_cutoff_at,
                known_at=known_at,
            )
        return ShareCapitalResult(
            total_shares=None,
            baseline_fact_id=f"missing-total-shares:{ts_code}",
            action_record_ids=(),
            algorithm_version=self.share_capital_resolver.algorithm_version,
            blocked_reasons=("SHARE_CAPITAL_BASELINE_MISSING",),
        )


__all__ = ["PILOT_FINANCIAL_PERIODS", "PilotFinancialAnalyzer"]
