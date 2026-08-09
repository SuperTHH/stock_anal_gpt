from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal

from hengce.actions.share_capital import (
    ShareCapitalResolver,
    ShareCapitalResult,
)
from hengce.contracts.pilot import PilotUniverseSnapshot
from hengce.financials.assembler import PointInTimeFinancialAssembler
from hengce.financials.metrics import PilotMetricCalculator, PilotMetricResult
from hengce.state.action_repository import CorporateActionRepository
from hengce.state.dividend_repository import AnnualDividendRepository
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

    def _share_capital(
        self,
        series: object,
        ts_code: str,
        actions: list[object],
        report_cutoff_at: datetime,
        known_at: datetime,
    ) -> ShareCapitalResult:
        periods = getattr(series, "periods", {})
        for period in sorted(periods, reverse=True):
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
