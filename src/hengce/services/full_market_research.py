from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from hengce.contracts.enums import PoolReadinessStatus, QualityStatus, StrategyType
from hengce.contracts.market_research import (
    DepthExclusion,
    DynamicPoolStatus,
    EvidenceCoverage,
    FullMarketResearchSnapshot,
    FunnelConfig,
    FunnelSecurity,
)
from hengce.contracts.official_event import ReportSource
from hengce.contracts.strategy import StrategyCandidate
from hengce.financials.assembler import PointInTimeFinancialAssembler
from hengce.financials.metrics import PilotMetricCalculator
from hengce.financials.query import AsOfFinancialQuery
from hengce.financials.registry_loader import CANONICAL_PILOT_FACTS
from hengce.services.pilot_financial_analysis import PilotFinancialAnalyzer
from hengce.state.action_repository import CorporateActionRepository
from hengce.state.dividend_repository import AnnualDividendRepository
from hengce.state.financial_repository import FinancialFilingRepository
from hengce.state.pdf_financial_repository import PdfFinancialDocumentRepository
from hengce.state.repository import StateRepository
from hengce.state.risk_repository import OfficialRiskScreenRepository
from hengce.strategies.deep_value import DEEP_VALUE_V1
from hengce.strategies.engine import SecurityStrategyInput, StrategyEngine
from hengce.strategies.filters import HardFilterResult
from hengce.strategies.pilot_inputs import PilotStrategyInputBuilder
from hengce.strategies.quality_growth import QUALITY_GROWTH_V1
from hengce.strategies.stable_dividend import STABLE_DIVIDEND_FULL_MARKET_V1
from hengce.warehouse.dividends import ImplementedDividendWarehouse
from hengce.warehouse.market import MarketWarehouse
from hengce.warehouse.market_research import FullMarketResearchWarehouse

_DEFINITIONS = {
    StrategyType.QUALITY_GROWTH: replace(
        QUALITY_GROWTH_V1,
        version="quality-growth-full-market-v1",
        industry_normalization_minimum_size=20,
    ),
    StrategyType.DEEP_VALUE: replace(
        DEEP_VALUE_V1,
        version="deep-value-full-market-v1",
        industry_normalization_minimum_size=20,
    ),
    StrategyType.STABLE_DIVIDEND: STABLE_DIVIDEND_FULL_MARKET_V1,
}


def _required_period_labels(report_cutoff: datetime) -> dict[date, str]:
    year = report_cutoff.year
    periods = {
        date(annual_year, 12, 31): f"{annual_year}年年报"
        for annual_year in range(year - 3, year)
    }
    periods.update(
        {
            date(quarter_year, 3, 31): f"{quarter_year}年一季报"
            for quarter_year in (year - 1, year)
        }
    )
    return periods


def _required_dividend_years(report_cutoff: datetime) -> tuple[int, ...]:
    return tuple(range(report_cutoff.year - 5, report_cutoff.year))


@dataclass(frozen=True, slots=True)
class _DynamicMember:
    ts_code: str
    security_name: str
    industry_l1: str | None
    evidence_record_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _DynamicUniverse:
    report_cutoff_at: datetime
    members: tuple[_DynamicMember, ...]


class FullMarketResearchService:
    """Build the auditable two-level funnel without claiming missing depth evidence."""

    def __init__(
        self,
        *,
        state: StateRepository,
        data_dir: Path,
        clock: Callable[[], datetime],
    ) -> None:
        self.state = state
        self.data_dir = data_dir
        self.clock = clock
        self.market = MarketWarehouse(data_dir / "normalized")
        self.dividends = ImplementedDividendWarehouse(data_dir / "normalized")
        self.snapshots = FullMarketResearchWarehouse(data_dir / "normalized")

    def read(self, market_date: date) -> FullMarketResearchSnapshot | None:
        return self.snapshots.read(market_date)

    def build(
        self,
        market_date: date,
        *,
        target_size: int = 300,
        minimum_amount: Decimal = Decimal("50000"),
        high_dividend_yield: Decimal = Decimal("0.03"),
        stable_dividend_candidate_yield: Decimal = Decimal("0.05"),
    ) -> FullMarketResearchSnapshot:
        generated_at = self.clock()
        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            raise ValueError("FULL_MARKET_RESEARCH_TIME_INVALID")
        config = FunnelConfig(
            target_size=target_size,
            minimum_amount=minimum_amount,
            high_dividend_yield=high_dividend_yield,
            stable_dividend_candidate_yield=stable_dividend_candidate_yield,
        )
        universe = self.state.get_security_master_universe()
        bars = self.market.read_bars(market_date)
        if not bars:
            raise ValueError("FULL_MARKET_RESEARCH_MARKET_MISSING")
        bar_by_code = {str(row["ts_code"]): row for row in bars}
        yields = self._dividend_yields(market_date, bar_by_code)
        low_cost_eligible: list[tuple[object, dict[str, object], Decimal | None]] = []
        for security in universe.securities:
            bar = bar_by_code.get(security.ts_code)
            if not self._low_cost_eligible(security, bar, market_date, config):
                continue
            assert bar is not None
            low_cost_eligible.append((security, bar, yields.get(security.ts_code)))

        depth_exclusion_reasons = self._depth_exclusion_reasons(market_date)
        eligible = [
            row for row in low_cost_eligible if row[0].ts_code not in depth_exclusion_reasons
        ]
        depth_exclusions = tuple(
            DepthExclusion(ts_code=code, reasons=reasons)
            for code, reasons in sorted(depth_exclusion_reasons.items())
            if any(row[0].ts_code == code for row in low_cost_eligible)
        )

        high_dividend = sorted(
            (row for row in eligible if row[2] is not None and row[2] >= high_dividend_yield),
            key=lambda row: (-row[2], -Decimal(str(row[1]["amount"])), row[0].ts_code),
        )
        high_codes = {row[0].ts_code for row in high_dividend}
        liquid = sorted(
            (row for row in eligible if row[0].ts_code not in high_codes),
            key=lambda row: (-Decimal(str(row[1]["amount"])), row[0].ts_code),
        )
        selected = (high_dividend + liquid)[:target_size]
        report_cutoff = datetime.combine(
            market_date, time(21, 30), tzinfo=ZoneInfo("Asia/Shanghai")
        )
        evidence_by_code = self._evidence(
            [row[0].ts_code for row in selected], report_cutoff, generated_at
        )
        funnel = tuple(
            FunnelSecurity(
                ts_code=security.ts_code,
                name=security.name,
                exchange=security.exchange,
                board=security.board,
                industry_l1=security.industry_l1,
                list_date=security.list_date,
                close=Decimal(str(bar["close"])),
                amount=Decimal(str(bar["amount"])),
                dividend_yield=dividend_yield,
                entry_reasons=(
                    ("股息率达到初筛门槛",)
                    if dividend_yield is not None and dividend_yield >= high_dividend_yield
                    else ("成交额进入流动性补充范围",)
                ),
                evidence=evidence_by_code[security.ts_code],
            )
            for security, bar, dividend_yield in selected
        )
        complete = sum(item.evidence.ready_for_scoring for item in funnel)
        strategy_inputs = (
            self._strategy_inputs(
                tuple(item for item in funnel if item.evidence.ready_for_scoring),
                market_date,
                report_cutoff,
                generated_at,
                universe.universe_hash,
                bar_by_code,
            )
            if complete
            else {strategy: () for strategy in StrategyType}
        )
        pools, candidates = self.evaluate_dynamic_pools(
            inputs=strategy_inputs,
            universe_size=len(funnel),
            evidence_complete_count=complete,
            report_date=market_date,
            report_cutoff=report_cutoff,
            known_at=generated_at,
        )
        source_records = self._source_records(
            ready=tuple(item for item in funnel if item.evidence.ready_for_scoring),
            candidates=candidates,
            market_date=market_date,
            report_cutoff=report_cutoff,
            known_at=generated_at,
            universe=universe,
            bars=bar_by_code,
        )
        input_hashes = self._input_hashes(market_date, universe.universe_hash)
        identity = {
            "market_date": market_date.isoformat(),
            "generated_at": generated_at.isoformat(),
            "config": config.model_dump(mode="json"),
            "market_universe_count": len(universe.securities),
            "low_cost_eligible_count": len(low_cost_eligible),
            "depth_exclusions": [
                item.model_dump(mode="json") for item in depth_exclusions
            ],
            "funnel": [item.model_dump(mode="json") for item in funnel],
            "pools": {
                strategy.value: pool.model_dump(mode="json")
                for strategy, pool in pools.items()
            },
            "candidate_pools": {
                strategy.value: [item.model_dump(mode="json") for item in pool]
                for strategy, pool in candidates.items()
            },
            "source_records": [item.model_dump(mode="json") for item in source_records],
            "input_hashes": input_hashes,
        }
        manifest_hash = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        snapshot = FullMarketResearchSnapshot(
            snapshot_id=f"full-market-{market_date.isoformat()}-{manifest_hash[:16]}",
            market_date=market_date,
            generated_at=generated_at,
            config=config,
            market_universe_count=len(universe.securities),
            low_cost_eligible_count=len(low_cost_eligible),
            funnel_count=len(funnel),
            high_dividend_funnel_count=sum(
                item.dividend_yield is not None
                and item.dividend_yield >= high_dividend_yield
                for item in funnel
            ),
            depth_excluded_count=len(depth_exclusions),
            depth_exclusions=depth_exclusions,
            depth_ready_count=complete,
            evidence_item_count=len(funnel) * 12,
            evidence_completed_count=sum(
                item.evidence.completed_item_count for item in funnel
            ),
            funnel=funnel,
            pools=pools,
            candidate_pools=candidates,
            source_records=source_records,
            input_hashes=input_hashes,
            manifest_hash=manifest_hash,
        )
        self.snapshots.write(snapshot)
        return snapshot

    def _depth_exclusion_reasons(self, market_date: date) -> dict[str, tuple[str, ...]]:
        """Return unresolved terminal failures for required depth evidence."""
        if not self.state.path.is_file():
            return {}
        with sqlite3.connect(self.state.path) as connection:
            rows = connection.execute(
                """
                SELECT task.ts_code, task.evidence_kind, task.evidence_period,
                       COALESCE(json_extract(task.payload_json, '$.error_code'),
                                'OFFICIAL_EVIDENCE_FAILED')
                FROM full_market_evidence_tasks AS task
                JOIN full_market_evidence_runs AS run ON run.run_id=task.run_id
                WHERE run.market_date=?
                  AND task.evidence_kind IN (
                      'PERIODIC_REPORT', 'DIVIDEND_YEAR',
                      'RISK_SCREEN', 'CORPORATE_ACTION'
                  )
                  AND task.status IN ('BLOCKED', 'RETRYABLE_FAILED')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM full_market_evidence_tasks AS satisfied
                      JOIN full_market_evidence_runs AS satisfied_run
                        ON satisfied_run.run_id=satisfied.run_id
                      WHERE satisfied_run.market_date=run.market_date
                        AND satisfied.ts_code=task.ts_code
                        AND satisfied.evidence_kind=task.evidence_kind
                        AND satisfied.evidence_period=task.evidence_period
                        AND satisfied.status='SATISFIED'
                  )
                ORDER BY task.ts_code, task.evidence_kind, task.evidence_period
                """,
                (market_date.isoformat(),),
            ).fetchall()
        grouped: dict[str, list[str]] = {}
        for code, evidence_kind, period, error_code in rows:
            grouped.setdefault(str(code), []).append(
                f"{evidence_kind}:{period}:{error_code}"
            )
        return {code: tuple(reasons) for code, reasons in grouped.items()}

    def _source_records(
        self,
        *,
        ready: tuple[FunnelSecurity, ...],
        candidates: dict[StrategyType, tuple[StrategyCandidate, ...]],
        market_date: date,
        report_cutoff: datetime,
        known_at: datetime,
        universe: Any,
        bars: dict[str, dict[str, object]],
    ) -> tuple[ReportSource, ...]:
        referenced = {
            record_id
            for pool in candidates.values()
            for candidate in pool
            for factor in candidate.factor_details
            for record_id in factor.source_record_ids
        }
        if not referenced:
            return ()
        source_names = {
            "cninfo": "巨潮资讯",
            "sse": "上海证券交易所",
            "szse": "深圳证券交易所",
            "tushare": "Tushare 日线接口",
        }
        records: dict[str, ReportSource] = {}

        def add(
            *,
            record_id: str,
            domain: str,
            source_id: str,
            source_url: object,
            published_at: datetime | None,
            effective_at: datetime | None,
            collected_at: datetime,
            valid_from: datetime,
            version: str,
            license_policy: str,
            quality_status: QualityStatus,
        ) -> None:
            if record_id not in referenced or quality_status not in {
                QualityStatus.VALID,
                QualityStatus.DERIVED,
            }:
                return
            source = ReportSource.model_validate(
                {
                    "record_id": record_id,
                    "domain": domain,
                    "source_name": source_names.get(source_id, source_id),
                    "source_url": source_url,
                    "published_at": published_at,
                    "effective_at": effective_at,
                    "collected_at": collected_at,
                    "valid_from": valid_from,
                    "version": version,
                    "license_policy": license_policy,
                    "quality_status": quality_status,
                }
            )
            existing = records.get(record_id)
            if existing is not None and existing != source:
                raise ValueError("FULL_MARKET_SOURCE_CONFLICT")
            records[record_id] = source

        codes = {item.ts_code for item in ready}
        components = {
            security.ts_code: component
            for component in universe.components
            for security in component.securities
        }
        pdf_repository = PdfFinancialDocumentRepository(self.state.path)
        financial_query = AsOfFinancialQuery(
            FinancialFilingRepository(self.state.path),
            self.data_dir / "warehouse",
        )
        action_repository = CorporateActionRepository(self.state.path)
        dividend_repository = AnnualDividendRepository(self.state.path)
        risk_repository = OfficialRiskScreenRepository(self.state.path)
        for code in sorted(codes):
            bar = bars[code]
            for market_record_id in (
                str(bar["record_id"]),
                f"closing-price:{market_date.isoformat()}:{code}",
            ):
                add(
                    record_id=market_record_id,
                    domain="market",
                    source_id=str(bar["source_id"]),
                    source_url=bar["source_url"],
                    published_at=bar.get("published_at"),
                    effective_at=bar.get("effective_at"),
                    collected_at=bar["collected_at"],
                    valid_from=bar["valid_from"],
                    version=str(bar["version"]),
                    license_policy=str(bar["license_policy"]),
                    quality_status=QualityStatus(str(bar["quality_status"])),
                )
            component = components.get(code)
            if component is not None:
                add(
                    record_id=f"security-master:{universe.universe_hash}:{code}",
                    domain="security_master",
                    source_id=component.source_id,
                    source_url=component.source_url,
                    published_at=None,
                    effective_at=None,
                    collected_at=component.collected_at,
                    valid_from=component.collected_at,
                    version=component.version,
                    license_policy="official-public-structured-personal-research",
                    quality_status=universe.quality_status,
                )
            for period in _required_period_labels(report_cutoff):
                query = financial_query.query_financial_facts(
                    ts_code=code,
                    report_period=period,
                    canonical_fact_names=CANONICAL_PILOT_FACTS,
                    as_of=report_cutoff,
                    known_at=known_at,
                )
                for fact in query.facts:
                    add(
                        record_id=str(fact["fact_id"]),
                        domain="financials",
                        source_id=str(fact["source_id"]),
                        source_url=fact["source_url"],
                        published_at=fact["published_at"],
                        effective_at=fact["effective_at"],
                        collected_at=fact["collected_at"],
                        valid_from=fact["valid_from"],
                        version=str(fact["version"]),
                        license_policy=str(fact["license_policy"]),
                        quality_status=QualityStatus(str(fact["quality_status"])),
                    )
                for document in pdf_repository.visible_documents(
                    code,
                    period,
                    as_of=report_cutoff,
                    known_at=known_at,
                ):
                    for fact_name in document.facts:
                        add(
                            record_id=f"{document.filing_id}:{fact_name}",
                            domain="financials",
                            source_id=document.source_id,
                            source_url=document.source_url,
                            published_at=document.published_at,
                            effective_at=None,
                            collected_at=document.valid_from,
                            valid_from=document.valid_from,
                            version=document.version,
                            license_policy="official-public-attachment-personal-research",
                            quality_status=document.quality_status,
                        )
            for action in action_repository.visible_actions(code, report_cutoff, known_at):
                add(
                    record_id=action.record_id,
                    domain="corporate_actions",
                    source_id=action.source_id,
                    source_url=action.source_url,
                    published_at=action.published_at,
                    effective_at=action.effective_at,
                    collected_at=action.collected_at,
                    valid_from=action.valid_from,
                    version=action.version,
                    license_policy=action.license_policy,
                    quality_status=action.quality_status,
                )
            for record in dividend_repository.visible_records(code, report_cutoff, known_at):
                add(
                    record_id=record.record_id,
                    domain="dividends",
                    source_id=record.source_id,
                    source_url=record.source_url,
                    published_at=record.published_at,
                    effective_at=record.effective_at,
                    collected_at=record.collected_at,
                    valid_from=record.valid_from,
                    version=record.version,
                    license_policy=record.license_policy,
                    quality_status=record.quality_status,
                )
            screen = risk_repository.visible_screen(
                code,
                as_of=report_cutoff,
                known_at=known_at,
            )
            if screen is not None:
                add(
                    record_id=screen.record_id,
                    domain="risk",
                    source_id=screen.source_id,
                    source_url=screen.source_url,
                    published_at=screen.published_at,
                    effective_at=screen.effective_at,
                    collected_at=screen.collected_at,
                    valid_from=screen.valid_from,
                    version=screen.version,
                    license_policy=screen.license_policy,
                    quality_status=screen.quality_status,
                )
        return tuple(records[key] for key in sorted(records))

    def _strategy_inputs(
        self,
        ready: tuple[FunnelSecurity, ...],
        market_date: date,
        report_cutoff: datetime,
        known_at: datetime,
        universe_hash: str,
        bars: dict[str, dict[str, object]],
    ) -> dict[StrategyType, tuple[SecurityStrategyInput, ...]]:
        members = tuple(
            _DynamicMember(
                ts_code=item.ts_code,
                security_name=item.name,
                industry_l1=item.industry_l1,
                evidence_record_ids=(
                    str(bars[item.ts_code]["record_id"]),
                    f"security-master:{universe_hash}:{item.ts_code}",
                ),
            )
            for item in ready
        )
        dynamic_universe = _DynamicUniverse(report_cutoff, members)
        financial_repository = FinancialFilingRepository(self.state.path)
        pdf_repository = PdfFinancialDocumentRepository(self.state.path)
        analyzer = PilotFinancialAnalyzer(
            assembler=PointInTimeFinancialAssembler(
                query=AsOfFinancialQuery(
                    financial_repository,
                    self.data_dir / "warehouse",
                ),
                pdf_provider=lambda code, period: pdf_repository.list_versions(code, period),
            ),
            action_repository=CorporateActionRepository(self.state.path),
            dividend_repository=AnnualDividendRepository(self.state.path),
            metric_calculator=PilotMetricCalculator("full-market-financial-metrics-v1"),
            market_warehouse=self.market,
            implemented_dividend_warehouse=self.dividends,
        )
        metrics = analyzer.calculate(
            universe=dynamic_universe,  # type: ignore[arg-type]
            market_date=market_date,
            report_cutoff_at=report_cutoff,
            known_at=known_at,
        )
        risk_repository = OfficialRiskScreenRepository(self.state.path)
        hard_filters: dict[str, HardFilterResult] = {}
        for member in members:
            screen = risk_repository.visible_screen(
                member.ts_code,
                as_of=report_cutoff,
                known_at=known_at,
            )
            reasons: list[str] = []
            if screen is None:
                reasons.append("HF-RISK-SCREEN-MISSING")
            else:
                if screen.delisting_risk:
                    reasons.append("HF-02")
                if screen.st_status is not None:
                    reasons.append("HF-03")
                if screen.is_suspended:
                    reasons.append("HF-04")
                if not screen.audit_opinion_standard:
                    reasons.append("HF-07")
                if screen.major_investigation_open:
                    reasons.append("HF-08")
                if not screen.publication_order_known:
                    reasons.append("HF-11")
            hard_filters[member.ts_code] = HardFilterResult(
                passed=not reasons,
                reasons=tuple(reasons),
                filter_version="full-market-official-evidence-v1",
                source_record_ids=(
                    (*member.evidence_record_ids, screen.record_id)
                    if screen is not None
                    else member.evidence_record_ids
                ),
            )
        built = PilotStrategyInputBuilder().build(
            dynamic_universe,  # type: ignore[arg-type]
            metrics=metrics,
            hard_filters=hard_filters,
            report_cutoff_at=report_cutoff,
            known_at=known_at,
        )
        return {strategy: tuple(items) for strategy, items in built.items()}

    def _dividend_yields(
        self, market_date: date, bars: dict[str, dict[str, object]]
    ) -> dict[str, Decimal]:
        start = market_date - timedelta(days=365)
        totals: dict[str, Decimal] = {}
        for item in self.dividends.read_records(market_date):
            if start < item.ex_date <= market_date:
                totals[item.ts_code] = totals.get(item.ts_code, Decimal(0)) + (
                    item.cash_dividend_per_share
                )
        return {
            code: total / close
            for code, total in totals.items()
            if (row := bars.get(code)) is not None
            and (close := Decimal(str(row["close"]))) > 0
        }

    @staticmethod
    def _low_cost_eligible(
        security: object,
        bar: dict[str, object] | None,
        market_date: date,
        config: FunnelConfig,
    ) -> bool:
        oldest_required_dividend_year = market_date.year - 5
        history_cutoff = date(oldest_required_dividend_year, 12, 31)
        return bool(
            security.is_in_scope
            and security.board in {"MAIN_SH", "MAIN_SZ", "CHINEXT", "STAR"}
            and security.security_type == "A_SHARE"
            and security.currency == "CNY"
            and (market_date - security.list_date).days >= config.minimum_listing_days
            and security.list_date <= history_cutoff
            and (security.delist_date is None or security.delist_date > market_date)
            and "ST" not in security.name.upper()
            and "退" not in security.name
            and bar is not None
            and Decimal(str(bar["close"])) > 0
            and Decimal(str(bar["amount"])) >= config.minimum_amount
        )

    def _evidence(
        self,
        codes: list[str],
        report_cutoff: datetime,
        known_at: datetime,
    ) -> dict[str, EvidenceCoverage]:
        result: dict[str, EvidenceCoverage] = {}
        period_labels = _required_period_labels(report_cutoff)
        dividend_years = _required_dividend_years(report_cutoff)
        implemented_dividends: dict[str, set[int]] = {}
        for item in self.dividends.read_records(report_cutoff.date()):
            if item.ex_date > report_cutoff.date():
                continue
            # An annual dividend is normally implemented in the calendar year after
            # its fiscal year. This conservative mapping closes only a year for which
            # an exchange-confirmed implementation exists; annual-report evidence,
            # when present, remains authoritative and is unioned below.
            fiscal_year = item.ex_date.year - 1
            implemented_dividends.setdefault(item.ts_code, set()).add(fiscal_year)
        implemented_action_codes = set(implemented_dividends)
        with sqlite3.connect(self.state.path) as connection:
            connection.row_factory = sqlite3.Row
            for code in codes:
                periods = self._visible_periods(
                    connection,
                    code,
                    report_cutoff,
                    known_at,
                    frozenset(period_labels),
                )
                years = self._visible_dividend_years(
                    connection,
                    code,
                    report_cutoff,
                    known_at,
                    frozenset(dividend_years),
                )
                years.update(implemented_dividends.get(code, set()))
                years.intersection_update(dividend_years)
                risk = self._visible_count(
                    connection,
                    "official_risk_screen_versions",
                    code,
                    report_cutoff,
                    known_at,
                ) > 0
                manifest_action_screen = connection.execute(
                    """
                    SELECT COUNT(*) FROM acquisition_manifest_items
                    WHERE ts_code=? AND document_kind='CAPITAL_ACTION_TIMELINE'
                      AND status='INGESTED' AND json_extract(payload_json, '$.quality_status')
                          IN ('VALID', 'DERIVED')
                    """,
                    (code,),
                ).fetchone()[0] > 0
                action_screen = (
                    manifest_action_screen
                    or code in implemented_action_codes
                    or years == set(dividend_years)
                )
                missing = [
                    label
                    for period, label in period_labels.items()
                    if period not in periods
                ]
                missing.extend(
                    f"{year}年分红" for year in dividend_years if year not in years
                )
                if not risk:
                    missing.append("风险证据")
                if not action_screen:
                    missing.append("公司行动证据")
                completed = len(periods) + len(years) + int(risk) + int(action_screen)
                result[code] = EvidenceCoverage(
                    periodic_report_count=len(periods),
                    annual_dividend_count=len(years),
                    risk_screen_available=risk,
                    corporate_action_screen_available=action_screen,
                    completed_item_count=completed,
                    missing_items=tuple(missing),
                    ready_for_scoring=completed == 12,
                )
        return result

    @staticmethod
    def _visible_periods(
        connection: sqlite3.Connection,
        code: str,
        report_cutoff: datetime,
        known_at: datetime,
        required_periods: frozenset[date],
    ) -> set[date]:
        values: set[date] = set()
        for table in ("financial_filings", "pdf_financial_documents"):
            status = " AND artifact_status='PUBLISHED'" if table == "financial_filings" else ""
            rows = connection.execute(
                f"SELECT report_period FROM {table} WHERE ts_code=? "
                f"AND published_at<=? AND valid_from<=?{status}",
                (code, report_cutoff.isoformat(), known_at.isoformat()),
            ).fetchall()
            values.update(date.fromisoformat(str(row[0])) for row in rows)
        return values.intersection(required_periods)

    @staticmethod
    def _visible_dividend_years(
        connection: sqlite3.Connection,
        code: str,
        report_cutoff: datetime,
        known_at: datetime,
        required_years: frozenset[int],
    ) -> set[int]:
        rows = connection.execute(
            """
            SELECT DISTINCT fiscal_year FROM annual_dividend_record_versions
            WHERE ts_code=? AND published_at<=? AND collected_at<=? AND valid_from<=?
            """,
            (code, report_cutoff.isoformat(), known_at.isoformat(), known_at.isoformat()),
        ).fetchall()
        return {int(row[0]) for row in rows}.intersection(required_years)

    @staticmethod
    def _visible_count(
        connection: sqlite3.Connection,
        table: str,
        code: str,
        report_cutoff: datetime,
        known_at: datetime,
    ) -> int:
        row = connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE ts_code=? AND published_at<=? "
            "AND effective_at<=? AND collected_at<=? AND valid_from<=? "
            "AND quality_status IN ('VALID', 'DERIVED')",
            (
                code,
                report_cutoff.isoformat(),
                report_cutoff.isoformat(),
                known_at.isoformat(),
                known_at.isoformat(),
            ),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def evaluate_dynamic_pools(
        *,
        inputs: dict[StrategyType, tuple[SecurityStrategyInput, ...]],
        universe_size: int,
        evidence_complete_count: int,
        report_date: date,
        report_cutoff: datetime,
        known_at: datetime,
    ) -> tuple[
        dict[StrategyType, DynamicPoolStatus],
        dict[StrategyType, tuple[StrategyCandidate, ...]],
    ]:
        if universe_size <= 0:
            raise ValueError("FULL_MARKET_RESEARCH_FUNNEL_EMPTY")
        pools: dict[StrategyType, DynamicPoolStatus] = {}
        candidates: dict[StrategyType, tuple[StrategyCandidate, ...]] = {}
        for strategy, definition in _DEFINITIONS.items():
            dynamic_definition = definition
            strategy_inputs = inputs.get(strategy, ())
            if evidence_complete_count == 0 or len(strategy_inputs) != evidence_complete_count:
                blocking = (
                    "NO_DEPTH_READY_SECURITIES"
                    if evidence_complete_count == 0
                    else "DYNAMIC_STRATEGY_INPUT_SET_INCOMPLETE"
                )
                complete = evidence_complete_count
                published: tuple[StrategyCandidate, ...] = ()
            else:
                evaluation = StrategyEngine(dynamic_definition).evaluate(
                    list(strategy_inputs),
                    report_date=report_date,
                    data_cutoff_at=report_cutoff,
                    known_at=known_at,
                )
                complete = evaluation.factor_complete_count
                blocking = (
                    "NO_COMPLETE_STRATEGY_FACTORS"
                    if complete == 0
                    else ""
                )
                published = evaluation.candidates if not blocking else ()
            ratio = Decimal(complete) / Decimal(universe_size)
            pools[strategy] = DynamicPoolStatus(
                strategy_type=strategy,
                strategy_version=dynamic_definition.version,
                status=(
                    PoolReadinessStatus.READY
                    if complete >= 1
                    else PoolReadinessStatus.BLOCKED
                ),
                universe_size=universe_size,
                complete_factor_count=complete,
                coverage_ratio=ratio,
                required_coverage_ratio=Decimal(0),
                minimum_complete_factor_count=1,
                blocking_codes=(() if complete >= 1 else (blocking,)),
                candidate_count=len(published),
            )
            candidates[strategy] = published
        return pools, candidates

    def _input_hashes(self, market_date: date, universe_hash: str) -> dict[str, str]:
        market_files = sorted(
            (self.market.dataset / f"trade_date={market_date.isoformat()}").glob("part-*.parquet")
        )
        dividend_files = sorted(
            (
                self.dividends.dataset
                / f"market_date={market_date.isoformat()}"
            ).glob("part-*.parquet")
        )
        market_hash = (
            market_files[0].stem.removeprefix("part-")
            if len(market_files) == 1
            else "0" * 64
        )
        dividend_hash = (
            dividend_files[0].stem.removeprefix("part-")
            if len(dividend_files) == 1
            else "0" * 64
        )
        return {
            "security_master": universe_hash,
            "market": market_hash,
            "implemented_dividends": dividend_hash,
        }


__all__ = ["FullMarketResearchService"]
