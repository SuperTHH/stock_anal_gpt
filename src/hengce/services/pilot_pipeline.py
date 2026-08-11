from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from hengce.acquisition.manual_inbox import ManualInbox
from hengce.acquisition.planner import AcquisitionPlanner
from hengce.contracts.enums import (
    AcquisitionStatus,
    DocumentKind,
    QualityStatus,
    StrategyType,
)
from hengce.contracts.official_event import ReportSource
from hengce.contracts.pilot import (
    AcquisitionManifestItem,
    PilotUniverseSnapshot,
)
from hengce.financials.assembler import PointInTimeFinancialAssembler
from hengce.financials.metrics import (
    PilotMetricCalculator,
    PilotMetricResult,
)
from hengce.financials.pdf_extractor import CninfoPdfExtractor
from hengce.financials.query import AsOfFinancialQuery
from hengce.financials.registry_loader import CANONICAL_PILOT_FACTS
from hengce.financials.sources import is_official_pdf_location
from hengce.policy.guard import PolicyGuard
from hengce.raw_store.store import RawObjectStore
from hengce.reports.publisher import ReportPublisher
from hengce.services.official_evidence_ingestion import (
    OfficialEvidenceIngestionService,
)
from hengce.services.pdf_financial_ingestion import (
    PdfFinancialIngestionService,
)
from hengce.services.pilot_financial_analysis import PILOT_FINANCIAL_PERIODS, PilotFinancialAnalyzer
from hengce.services.pilot_reconstruction import (
    PilotStageContext,
    StageHandler,
)
from hengce.services.pilot_universe import PilotUniverseSelector
from hengce.state.action_repository import CorporateActionRepository
from hengce.state.dividend_repository import AnnualDividendRepository
from hengce.state.event_repository import OfficialEventRepository
from hengce.state.financial_repository import FinancialFilingRepository
from hengce.state.pdf_financial_repository import (
    PdfFinancialDocumentRepository,
)
from hengce.state.pilot_repository import PilotRepository
from hengce.state.report_repository import ReportRepository
from hengce.state.repository import StateRepository
from hengce.state.risk_repository import OfficialRiskScreenRepository
from hengce.strategies.deep_value import DEEP_VALUE_V1
from hengce.strategies.filters import HardFilterResult
from hengce.strategies.pilot_inputs import PilotStrategyInputBuilder
from hengce.strategies.quality_growth import QUALITY_GROWTH_V1
from hengce.strategies.readiness import (
    IndependentPoolResult,
    IndependentPoolRunner,
)
from hengce.strategies.stable_dividend import STABLE_DIVIDEND_V1
from hengce.warehouse.market import MarketWarehouse

_DEFINITIONS = {
    StrategyType.QUALITY_GROWTH: QUALITY_GROWTH_V1,
    StrategyType.DEEP_VALUE: DEEP_VALUE_V1,
    StrategyType.STABLE_DIVIDEND: STABLE_DIVIDEND_V1,
}
_NARRATIVE_TEMPLATE_VERSIONS = {
    strategy.value: "pilot-rule-narrative-v1" for strategy in StrategyType
}


def _coverage_quality_status(*, covered: int, target: int) -> QualityStatus:
    if target <= 0 or covered <= 0:
        return QualityStatus.MISSING
    if covered >= target:
        return QualityStatus.VALID
    return QualityStatus.PARTIAL


class PilotProductionStages:
    """Production stage handlers for the private, fail-closed pilot loop."""

    def __init__(
        self,
        *,
        state: StateRepository,
        data_dir: Path,
        clock: Callable[[], datetime],
        pdf_ingestion_service: object | None = None,
        official_evidence_ingestion_service: object | None = None,
        financial_analyzer: object | None = None,
        hard_filter_provider: object | None = None,
    ) -> None:
        self.state = state
        self.data_dir = data_dir
        self.clock = clock
        self.pilot_repository = PilotRepository(state.path)
        self.risk_repository = OfficialRiskScreenRepository(state.path)
        self.action_repository = CorporateActionRepository(state.path)
        self.dividend_repository = AnnualDividendRepository(state.path)
        self.event_repository = OfficialEventRepository(state.path)
        self.market_warehouse = MarketWarehouse(data_dir / "normalized")
        self.report_repository = ReportRepository(state.path)
        self.pdf_repository = PdfFinancialDocumentRepository(state.path)
        self.financial_repository = FinancialFilingRepository(state.path)
        self.financial_query = AsOfFinancialQuery(
            self.financial_repository,
            data_dir / "warehouse",
        )
        self.pdf_ingestion_service = (
            pdf_ingestion_service
            if pdf_ingestion_service is not None
            else PdfFinancialIngestionService(
                raw_store=RawObjectStore(data_dir / "raw"),
                repository=self.pdf_repository,
                pilot_repository=self.pilot_repository,
                extractor=CninfoPdfExtractor(parser_version="cninfo-pdf-pilot-v5"),
                clock=clock,
            )
        )
        self.official_evidence_ingestion_service = (
            official_evidence_ingestion_service
            if official_evidence_ingestion_service is not None
            else OfficialEvidenceIngestionService(
                raw_store=RawObjectStore(data_dir / "raw"),
                pilot_repository=self.pilot_repository,
                action_repository=self.action_repository,
                dividend_repository=self.dividend_repository,
                risk_repository=self.risk_repository,
                manual_inbox=data_dir / "manual_inbox",
                clock=clock,
            )
        )
        self.financial_assembler = PointInTimeFinancialAssembler(
            query=self.financial_query,
            pdf_provider=lambda ts_code, period: self.pdf_repository.list_versions(
                ts_code, period
            ),
        )
        self.financial_analyzer = (
            financial_analyzer
            if financial_analyzer is not None
            else PilotFinancialAnalyzer(
                assembler=self.financial_assembler,
                action_repository=self.action_repository,
                dividend_repository=self.dividend_repository,
                metric_calculator=PilotMetricCalculator("pilot-financial-metrics-v1"),
                market_warehouse=self.market_warehouse,
            )
        )
        self._metric_cache: dict[tuple[object, ...], dict[str, PilotMetricResult]] = {}
        self.hard_filter_provider = hard_filter_provider

    def handlers(self) -> dict[str, StageHandler]:
        return {
            "02_validate_inputs": self.validate_inputs,
            "03_freeze_universe": self.freeze_universe,
            "04_plan_acquisition": self.plan_acquisition,
            "05_acquire_public_documents": self.acquire_public_documents,
            "06_scan_manual_inbox": self.scan_manual_inbox,
            "07_ingest_documents": self.ingest_documents,
            "08_assemble_point_in_time_facts": self.assemble_facts,
            "09_calculate_metrics": self.calculate_metrics,
            "10_run_filters_and_pools": self.run_pools,
            "11_publish_report": self.publish_report,
            "12_write_run_summary": self.write_run_summary,
        }

    def validate_inputs(
        self,
        context: PilotStageContext,
    ) -> Mapping[str, object]:
        bars = self.market_warehouse.read_bars(context.market_date)
        if not bars:
            raise ValueError("PILOT_MARKET_DATA_MISSING")
        partition = self.market_warehouse.dataset / f"trade_date={context.market_date.isoformat()}"
        artifacts = sorted(partition.glob("part-*.parquet"))
        if len(artifacts) != 1:
            raise ValueError("PILOT_MARKET_ARTIFACT_INVALID")
        market_hash = self.market_warehouse.validate_artifact(
            artifacts[0],
            context.market_date,
            len(bars),
        )
        master = self.state.get_security_master_universe()
        if len(master.components) != 2 or not master.securities:
            raise ValueError("PILOT_SECURITY_MASTER_INVALID")
        return {
            "market_bar_count": len(bars),
            "market_content_hash": market_hash,
            "security_master_count": len(master.securities),
            "security_master_hash": master.universe_hash,
        }

    def freeze_universe(
        self,
        context: PilotStageContext,
    ) -> Mapping[str, object]:
        bars = self.market_warehouse.read_bars(context.market_date)
        master = self.state.get_security_master_universe()
        artifacts = sorted(
            (self.market_warehouse.dataset / f"trade_date={context.market_date.isoformat()}").glob(
                "part-*.parquet"
            )
        )
        if len(artifacts) != 1:
            raise ValueError("PILOT_MARKET_ARTIFACT_INVALID")
        market_hash = self.market_warehouse.validate_artifact(
            artifacts[0],
            context.market_date,
            len(bars),
        )
        existing = self.pilot_repository.get_universe_for_date(context.market_date)
        if existing is None:
            universe = PilotUniverseSelector().select(
                market_date=context.market_date,
                report_cutoff_at=context.report_cutoff_at,
                bars=bars,
                securities=master.securities,
                market_content_hash=market_hash,
                master_universe_hash=master.universe_hash,
                created_at=context.known_at,
            )
            universe = self.pilot_repository.publish_universe(universe)
        else:
            universe = existing
            if universe.report_cutoff_at != context.report_cutoff_at or universe.input_hashes != {
                "market": market_hash,
                "security_master": master.universe_hash,
            }:
                raise ValueError("PILOT_UNIVERSE_INPUT_CHANGED")
        return {
            "universe_id": universe.universe_id,
            "universe_count": len(universe.members),
            "board_quotas": dict(universe.quotas),
            "universe_manifest_hash": universe.manifest_hash,
        }

    def plan_acquisition(
        self,
        context: PilotStageContext,
    ) -> Mapping[str, object]:
        universe = self._universe(context)
        expected = AcquisitionPlanner().build(
            universe,
            context.report_cutoff_at,
        )
        existing = self.pilot_repository.list_manifest(universe.universe_id)
        if not existing:
            self.pilot_repository.insert_manifest(expected)
            existing = self.pilot_repository.list_manifest(universe.universe_id)
        if {item.item_id for item in existing} != {item.item_id for item in expected}:
            raise ValueError("ACQUISITION_MANIFEST_ID_SET_CHANGED")
        return {
            "universe_id": universe.universe_id,
            "manifest_total": len(existing),
            "periodic_report_count": sum(
                item.document_kind is DocumentKind.PERIODIC_REPORT for item in existing
            ),
            "manifest_status_distribution": self._status_distribution(existing),
        }

    def acquire_public_documents(
        self,
        context: PilotStageContext,
    ) -> Mapping[str, object]:
        del context
        return {
            "policy_guarded": True,
            "network_calls": 0,
            "public_discovery_status": "AWAITING_MANUAL",
        }

    def scan_manual_inbox(
        self,
        context: PilotStageContext,
    ) -> Mapping[str, object]:
        universe = self._universe(context)
        manifest = self.pilot_repository.list_manifest(universe.universe_id)
        result = ManualInbox(
            guard=PolicyGuard(self.state),
            raw_store=RawObjectStore(self.data_dir / "raw"),
            clock=self.clock,
        ).scan(self.data_dir / "manual_inbox", manifest)
        for accepted in result.accepted:
            current = self.pilot_repository.get_manifest_item(accepted.item_id)
            if current is None:
                raise ValueError("ACQUISITION_ITEM_NOT_FOUND")
            if current.status is AcquisitionStatus.PLANNED:
                discovered = current.model_copy(
                    update={"status": AcquisitionStatus.DISCOVERED}
                )
                current = self.pilot_repository.transition(
                    current.item_id,
                    AcquisitionStatus.PLANNED,
                    discovered,
                    context.known_at,
                )
            self.pilot_repository.transition(
                accepted.item_id,
                current.status,
                accepted,
                context.known_at,
            )
        for item in self.pilot_repository.list_manifest(universe.universe_id):
            if item.status is not AcquisitionStatus.PLANNED:
                continue
            awaiting = item.model_copy(
                update={
                    "status": AcquisitionStatus.AWAITING_MANUAL,
                    "quality_status": QualityStatus.MISSING,
                    "error_code": "OFFICIAL_ATTACHMENT_REQUIRED",
                }
            )
            self.pilot_repository.transition(
                item.item_id,
                AcquisitionStatus.PLANNED,
                awaiting,
                context.known_at,
            )
        manifest = self.pilot_repository.list_manifest(universe.universe_id)
        return {
            "accepted_manual_attachment_count": len(result.accepted),
            "rejected_manual_attachment_count": len(result.rejected),
            "manual_todo_count": self._manual_todo_count(manifest),
            "manifest_status_distribution": self._status_distribution(manifest),
        }

    def ingest_documents(
        self,
        context: PilotStageContext,
    ) -> Mapping[str, object]:
        self._metric_cache.clear()
        manifest = self.pilot_repository.list_manifest(self._universe(context).universe_id)
        for item in manifest:
            if (
                item.status is AcquisitionStatus.DOWNLOADED
                and item.document_kind is DocumentKind.PERIODIC_REPORT
                and is_official_pdf_location(item.source_id, item.source_url)
            ):
                self.pdf_ingestion_service.run(item.item_id)  # type: ignore[attr-defined]
            elif (
                item.status is AcquisitionStatus.DOWNLOADED
                and item.document_kind is not DocumentKind.PERIODIC_REPORT
                and self.official_evidence_ingestion_service is not None
            ):
                self.official_evidence_ingestion_service.run(  # type: ignore[attr-defined]
                    item.item_id
                )
        manifest = self.pilot_repository.list_manifest(self._universe(context).universe_id)
        ingested = tuple(item for item in manifest if item.status is AcquisitionStatus.INGESTED)
        xbrl_count = sum(
            item.document_kind is DocumentKind.PERIODIC_REPORT
            and item.source_id in {"sse", "szse"}
            and not is_official_pdf_location(item.source_id, item.source_url)
            for item in ingested
        )
        pdf_count = sum(
            item.document_kind is DocumentKind.PERIODIC_REPORT
            and is_official_pdf_location(item.source_id, item.source_url)
            for item in ingested
        )
        downloaded_count = sum(
            item.status
            in {
                AcquisitionStatus.DOWNLOADED,
                AcquisitionStatus.VERIFIED,
            }
            for item in manifest
        )
        return {
            "xbrl_used_count": xbrl_count,
            "pdf_used_count": pdf_count,
            "downloaded_pending_ingestion_count": downloaded_count,
            "manual_todo_count": self._manual_todo_count(manifest),
            "manifest_status_distribution": self._status_distribution(manifest),
        }

    def assemble_facts(
        self,
        context: PilotStageContext,
    ) -> Mapping[str, object]:
        universe = self._universe(context)
        manifest = self.pilot_repository.list_manifest(universe.universe_id)
        filing_count, fact_count = self._published_filing_counts()
        action_screen_count = self._corporate_action_screen_count(manifest)
        return {
            "published_filing_count": filing_count,
            "financial_fact_count": fact_count,
            "corporate_action_count": self._table_count("corporate_action_versions"),
            "corporate_action_screen_count": action_screen_count,
            "corporate_action_screen_target_count": len(universe.members),
            "annual_dividend_record_count": self._table_count(
                "annual_dividend_record_versions"
            ),
            "share_capital_count": 0,
            "fallback_reason_counts": {},
        }

    def calculate_metrics(
        self,
        context: PilotStageContext,
    ) -> Mapping[str, object]:
        metrics = self._metric_results(context)
        return {
            "derived_metric_count": self._derived_metric_count(metrics),
            "share_capital_count": self._share_capital_count(metrics),
        }

    def run_pools(
        self,
        context: PilotStageContext,
    ) -> Mapping[str, object]:
        results = self._pool_results(context)
        return {
            "pool_coverage": {
                strategy.value: str(result.readiness.coverage_ratio)
                for strategy, result in results.items()
            },
            "pool_statuses": {
                strategy.value: result.readiness.status.value
                for strategy, result in results.items()
            },
            "pool_candidate_counts": {
                strategy.value: len(result.candidates) for strategy, result in results.items()
            },
        }

    def publish_report(
        self,
        context: PilotStageContext,
    ) -> Mapping[str, object]:
        universe = self._universe(context)
        manifest = self.pilot_repository.list_manifest(universe.universe_id)
        results = self._pool_results(context)
        metrics = self._metric_results(context)
        filing_count, fact_count = self._published_filing_counts()
        statuses = self._status_distribution(manifest)
        xbrl_count = sum(
            item.status is AcquisitionStatus.INGESTED
            and item.document_kind is DocumentKind.PERIODIC_REPORT
            and item.source_id in {"sse", "szse"}
            and not is_official_pdf_location(item.source_id, item.source_url)
            for item in manifest
        )
        pdf_count = sum(
            item.status is AcquisitionStatus.INGESTED
            and item.document_kind is DocumentKind.PERIODIC_REPORT
            and is_official_pdf_location(item.source_id, item.source_url)
            for item in manifest
        )
        event_cutoff_at = context.event_cutoff_at or context.report_cutoff_at
        official_events = self.event_repository.visible_events(
            as_of=event_cutoff_at,
            known_at=context.known_at,
        )
        manual_todo_count = self._manual_todo_count(manifest)
        action_screen_count = self._corporate_action_screen_count(manifest)
        fallback_reasons = (
            {"XBRL_NOT_INGESTED_PDF_FALLBACK": pdf_count}
            if pdf_count > 0 and xbrl_count == 0
            else {}
        )
        quality_summary: dict[str, object] = {
            "manifest_status_distribution": statuses,
            "xbrl_used_count": xbrl_count,
            "pdf_used_count": pdf_count,
            "fallback_reason_counts": fallback_reasons,
            "published_filing_count": filing_count,
            "financial_fact_count": fact_count,
            "corporate_action_count": self._table_count("corporate_action_versions"),
            "corporate_action_screen_count": action_screen_count,
            "corporate_action_screen_target_count": len(universe.members),
            "annual_dividend_record_count": self._table_count(
                "annual_dividend_record_versions"
            ),
            "share_capital_count": self._share_capital_count(metrics),
            "derived_metric_count": self._derived_metric_count(metrics),
            "official_risk_screen_count": self._table_count(
                "official_risk_screen_versions"
            ),
            "official_event_count": len(official_events),
            "narrative_template_versions": dict(_NARRATIVE_TEMPLATE_VERSIONS),
        }
        identity = {
            "market_date": context.market_date.isoformat(),
            "known_at": context.known_at.isoformat(),
            "event_cutoff_at": event_cutoff_at.isoformat(),
            "universe_id": universe.universe_id,
        }
        digest = hashlib.sha256(
            json.dumps(
                identity,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        report_id = f"pilot-{context.market_date.isoformat()}-{digest[:16]}"
        publication = ReportPublisher(
            self.report_repository,
            self.data_dir / "reports",
        ).publish(
            report_id=report_id,
            report_date=context.market_date,
            market_cutoff_at=context.report_cutoff_at,
            event_cutoff_at=event_cutoff_at,
            generated_at=context.known_at,
            candidate_pools={
                strategy: list(result.candidates) for strategy, result in results.items()
            },
            data_domain_statuses={
                "market": QualityStatus.VALID,
                "security_master": QualityStatus.VALID,
                "pilot_universe": QualityStatus.VALID,
                "manifest": (
                    QualityStatus.PARTIAL
                    if manual_todo_count
                    else QualityStatus.VALID
                ),
                "financials": (QualityStatus.VALID if fact_count > 0 else QualityStatus.MISSING),
                "corporate_actions": (
                    _coverage_quality_status(
                        covered=action_screen_count,
                        target=len(universe.members),
                    )
                ),
                "dividends": (
                    QualityStatus.VALID
                    if quality_summary["annual_dividend_record_count"]
                    else QualityStatus.MISSING
                ),
                "risk": (
                    QualityStatus.VALID
                    if quality_summary["official_risk_screen_count"]
                    else QualityStatus.MISSING
                ),
                "events": (
                    QualityStatus.VALID
                    if official_events
                    else QualityStatus.MISSING
                ),
                "metrics": (
                    QualityStatus.VALID
                    if quality_summary["derived_metric_count"]
                    else QualityStatus.MISSING
                ),
            },
            strategy_evidence={
                strategy: result.evidence
                for strategy, result in results.items()
                if result.evidence is not None
            },
            strategy_versions={
                strategy: definition.version for strategy, definition in _DEFINITIONS.items()
            },
            official_events=official_events,
            source_records=self._report_source_records(
                context,
                universe,
                manifest,
                referenced_ids=frozenset(
                    source_id
                    for result in results.values()
                    for candidate in result.candidates
                    for factor in candidate.factor_details
                    for source_id in factor.source_record_ids
                ),
            ),
            pool_readiness={strategy: result.readiness for strategy, result in results.items()},
            universe_id=universe.universe_id,
            report_cutoff_at=context.report_cutoff_at,
            known_at=context.known_at,
            generation_started_at=context.known_at,
            manual_todo_count=manual_todo_count,
            quality_summary=quality_summary,
        )
        if not publication.published or publication.snapshot is None:
            raise ValueError(
                publication.blocked_reasons[0]
                if publication.blocked_reasons
                else "PILOT_REPORT_NOT_PUBLISHED"
            )
        return {
            "report_id": publication.snapshot.report_id,
            "report_hash": publication.snapshot.manifest_hash,
            "manual_todo_count": manual_todo_count,
            "manifest_status_distribution": statuses,
            "xbrl_used_count": xbrl_count,
            "pdf_used_count": pdf_count,
            "fallback_reason_counts": fallback_reasons,
            "financial_fact_count": fact_count,
            "corporate_action_count": quality_summary["corporate_action_count"],
            "corporate_action_screen_count": action_screen_count,
            "corporate_action_screen_target_count": len(universe.members),
            "share_capital_count": quality_summary["share_capital_count"],
            "derived_metric_count": quality_summary["derived_metric_count"],
            "official_risk_screen_count": quality_summary[
                "official_risk_screen_count"
            ],
            "official_event_count": len(official_events),
            "pool_coverage": {
                strategy.value: str(result.readiness.coverage_ratio)
                for strategy, result in results.items()
            },
            "pool_statuses": {
                strategy.value: result.readiness.status.value
                for strategy, result in results.items()
            },
            "pool_candidate_counts": {
                strategy.value: len(result.candidates) for strategy, result in results.items()
            },
        }

    def write_run_summary(
        self,
        context: PilotStageContext,
    ) -> Mapping[str, object]:
        latest = self.report_repository.latest_report()
        if latest is None:
            raise ValueError("PILOT_REPORT_NOT_PUBLISHED")
        payload = {
            "market_date": context.market_date.isoformat(),
            "report_cutoff_at": context.report_cutoff_at.isoformat(),
            "known_at": context.known_at.isoformat(),
            "acquisition_mode": context.acquisition_mode,
            "report_id": latest.snapshot.report_id,
            "report_hash": latest.snapshot.manifest_hash,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        root = self.data_dir / "run_summaries"
        root.mkdir(parents=True, exist_ok=True)
        target = root / (f"pilot-{context.market_date.isoformat()}-{digest}.json")
        if target.exists() and target.read_bytes() != encoded:
            raise ValueError("PILOT_RUN_SUMMARY_IMMUTABILITY_CONFLICT")
        if not target.exists():
            target.write_bytes(encoded)
        return {
            "report_id": latest.snapshot.report_id,
            "report_hash": latest.snapshot.manifest_hash,
            "run_summary_hash": digest,
        }

    def _universe(self, context: PilotStageContext):
        universe = self.pilot_repository.get_universe_for_date(context.market_date)
        if universe is None:
            raise ValueError("PILOT_UNIVERSE_NOT_FOUND")
        if universe.report_cutoff_at != context.report_cutoff_at:
            raise ValueError("PILOT_UNIVERSE_CUTOFF_MISMATCH")
        return universe

    def _pool_results(
        self,
        context: PilotStageContext,
    ) -> dict[StrategyType, IndependentPoolResult]:
        universe = self._universe(context)
        hard_filters = (
            self.hard_filter_provider(context, universe)  # type: ignore[operator]
            if self.hard_filter_provider is not None
            else self._production_hard_filters(universe, context)
        )
        inputs = PilotStrategyInputBuilder().build(
            universe,
            metrics=self._metric_results(context),
            hard_filters=hard_filters,
            report_cutoff_at=context.report_cutoff_at,
            known_at=context.known_at,
        )
        return IndependentPoolRunner().run(
            definitions=_DEFINITIONS,
            inputs=inputs,
            universe=universe,
            report_date=context.market_date,
            report_cutoff_at=context.report_cutoff_at,
            known_at=context.known_at,
        )

    def _production_hard_filters(
        self,
        universe: object,
        context: PilotStageContext,
    ) -> dict[str, HardFilterResult]:
        manifest = self.pilot_repository.list_manifest(
            universe.universe_id  # type: ignore[attr-defined]
        )
        risk_items = {
            item.ts_code: item
            for item in manifest
            if item.document_kind is DocumentKind.RISK_SCREEN
        }
        results: dict[str, HardFilterResult] = {}
        for member in universe.members:  # type: ignore[attr-defined]
            risk = risk_items.get(member.ts_code)
            usable_manifest = bool(
                risk is not None
                and risk.status is AcquisitionStatus.INGESTED
                and risk.quality_status in {QualityStatus.VALID, QualityStatus.DERIVED}
            )
            screen = (
                self.risk_repository.visible_screen(
                    member.ts_code,
                    as_of=context.report_cutoff_at,
                    known_at=context.known_at,
                )
                if usable_manifest
                else None
            )
            reasons: list[str] = []
            if screen is None or screen.quality_status not in {
                QualityStatus.VALID,
                QualityStatus.DERIVED,
            }:
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
            results[member.ts_code] = HardFilterResult(
                passed=not reasons,
                reasons=tuple(reasons),
                filter_version="pilot-official-risk-screen-v1",
                source_record_ids=(
                    (
                        *member.evidence_record_ids,
                        risk.item_id,
                        screen.record_id,
                    )
                    if risk is not None and screen is not None
                    else member.evidence_record_ids
                ),
            )
        return results

    def _metric_results(
        self,
        context: PilotStageContext,
    ) -> dict[str, PilotMetricResult]:
        universe = self._universe(context)
        key = (
            universe.universe_id,
            context.market_date,
            context.report_cutoff_at,
            context.known_at,
        )
        if key not in self._metric_cache:
            self._metric_cache[key] = self.financial_analyzer.calculate(  # type: ignore[attr-defined]
                universe=universe,
                market_date=context.market_date,
                report_cutoff_at=context.report_cutoff_at,
                known_at=context.known_at,
            )
        return self._metric_cache[key]

    @staticmethod
    def _derived_metric_count(
        results: Mapping[str, PilotMetricResult],
    ) -> int:
        return sum(
            metric.value is not None and metric.quality_status is QualityStatus.DERIVED
            for result in results.values()
            for metric in result.metrics.values()
        )

    @staticmethod
    def _share_capital_count(
        results: Mapping[str, PilotMetricResult],
    ) -> int:
        return sum(
            result.metrics.get("market_cap") is not None
            and result.metrics["market_cap"].value is not None
            for result in results.values()
        )

    def _published_filing_counts(self) -> tuple[int, int]:
        try:
            with sqlite3.connect(self.state.path) as connection:
                row = connection.execute(
                    """
                    SELECT COUNT(*), COALESCE(SUM(expected_count), 0)
                    FROM financial_filings
                    WHERE artifact_status='PUBLISHED'
                    """
                ).fetchone()
                pdf_rows = connection.execute(
                    "SELECT payload_json FROM pdf_financial_documents"
                ).fetchall()
        except sqlite3.Error:
            return 0, 0
        xbrl = (int(row[0]), int(row[1])) if row is not None else (0, 0)
        pdf_fact_count = 0
        for (raw_payload,) in pdf_rows:
            try:
                payload = json.loads(str(raw_payload))
            except json.JSONDecodeError:
                continue
            facts = payload.get("facts")
            if isinstance(facts, dict):
                pdf_fact_count += len(facts)
        return xbrl[0] + len(pdf_rows), xbrl[1] + pdf_fact_count

    def _table_count(self, table: str) -> int:
        if table not in {
            "annual_dividend_record_versions",
            "corporate_action_versions",
            "official_risk_screen_versions",
        }:
            raise ValueError("PILOT_TABLE_NOT_ALLOWED")
        try:
            with sqlite3.connect(self.state.path) as connection:
                row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        except sqlite3.Error:
            return 0
        return int(row[0]) if row is not None else 0

    @staticmethod
    def _status_distribution(
        manifest: tuple[AcquisitionManifestItem, ...],
    ) -> dict[str, int]:
        return dict(sorted(Counter(item.status.value for item in manifest).items()))

    @staticmethod
    def _manual_todo_count(
        manifest: tuple[AcquisitionManifestItem, ...],
    ) -> int:
        return sum(
            item.status
            not in {
                AcquisitionStatus.INGESTED,
                AcquisitionStatus.REJECTED,
            }
            for item in manifest
        )

    @staticmethod
    def _corporate_action_screen_count(
        manifest: tuple[AcquisitionManifestItem, ...],
    ) -> int:
        return sum(
            item.document_kind is DocumentKind.CAPITAL_ACTION_TIMELINE
            and item.status is AcquisitionStatus.INGESTED
            and item.quality_status in {QualityStatus.VALID, QualityStatus.DERIVED}
            for item in manifest
        )

    def _report_source_records(
        self,
        context: PilotStageContext,
        universe: PilotUniverseSnapshot,
        manifest: tuple[AcquisitionManifestItem, ...],
        *,
        referenced_ids: frozenset[str] | None = None,
    ) -> tuple[ReportSource, ...]:
        source_names = {
            "cninfo": "巨潮资讯",
            "sse": "上海证券交易所",
            "szse": "深圳证券交易所",
        }
        domains = {
            DocumentKind.PERIODIC_REPORT: "financials",
            DocumentKind.DIVIDEND_RECORD: "dividends",
            DocumentKind.CAPITAL_ACTION_TIMELINE: "corporate_actions",
            DocumentKind.RISK_SCREEN: "risk",
        }
        records: dict[str, ReportSource] = {}

        def add(source: ReportSource) -> None:
            existing = records.get(source.record_id)
            if existing is not None and existing != source:
                raise ValueError("PILOT_REPORT_SOURCE_CONFLICT")
            records[source.record_id] = source

        def source_name(source_id: str) -> str:
            localized = {
                "cninfo": "巨潮资讯",
                "sse": "上海证券交易所",
                "szse": "深圳证券交易所",
                "csrc": "中国证监会",
                "stats": "国家统计局",
                "tushare": "Tushare 日线接口",
            }
            return localized.get(source_id, source_names.get(source_id, source_id))

        def add_fact_source(
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
            if referenced_ids is not None and record_id not in referenced_ids:
                return
            if quality_status not in {QualityStatus.VALID, QualityStatus.DERIVED}:
                return
            add(
                ReportSource.model_validate(
                    {
                        "record_id": record_id,
                        "domain": domain,
                        "source_name": source_name(source_id),
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
            )

        for item in manifest:
            if (
                item.status is not AcquisitionStatus.INGESTED
                or item.source_url is None
                or item.collected_at is None
                or item.version is None
                or item.quality_status
                not in {QualityStatus.VALID, QualityStatus.DERIVED}
            ):
                continue
            add(
                ReportSource(
                    record_id=item.item_id,
                    domain=domains[item.document_kind],
                    source_name=source_name(item.source_id),
                    source_url=item.source_url,
                    published_at=item.published_at,
                    effective_at=item.effective_at,
                    collected_at=item.collected_at,
                    valid_from=item.collected_at,
                    version=item.version,
                    license_policy="personal-non-commercial-official-public-attachment",
                    quality_status=item.quality_status,
                )
            )

        bars = {
            str(bar["ts_code"]): bar
            for bar in self.market_warehouse.read_bars(context.market_date)
        }
        master = self.state.get_security_master_universe()
        components = {
            security.ts_code: component
            for component in master.components
            for security in component.securities
        }
        for member in universe.members:
            bar = bars.get(member.ts_code)
            if bar is not None:
                for record_id in (
                    str(bar["record_id"]),
                    (
                        f"closing-price:{context.market_date.isoformat()}:"
                        f"{member.ts_code}"
                    ),
                ):
                    add_fact_source(
                        record_id=record_id,
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
            component = components.get(member.ts_code)
            if component is not None:
                add_fact_source(
                    record_id=(
                        f"security-master:{master.universe_hash}:{member.ts_code}"
                    ),
                    domain="security_master",
                    source_id=component.source_id,
                    source_url=component.source_url,
                    published_at=None,
                    effective_at=None,
                    collected_at=component.collected_at,
                    valid_from=component.collected_at,
                    version=component.version,
                    license_policy="official-public-structured-personal-research",
                    quality_status=master.quality_status,
                )

            for period in PILOT_FINANCIAL_PERIODS:
                xbrl = self.financial_query.query_financial_facts(
                    ts_code=member.ts_code,
                    report_period=period,
                    canonical_fact_names=CANONICAL_PILOT_FACTS,
                    as_of=context.report_cutoff_at,
                    known_at=context.known_at,
                )
                for fact in xbrl.facts:
                    add_fact_source(
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
                for document in self.pdf_repository.visible_documents(
                    member.ts_code,
                    period,
                    as_of=context.report_cutoff_at,
                    known_at=context.known_at,
                ):
                    for fact_name in document.facts:
                        add_fact_source(
                            record_id=f"{document.filing_id}:{fact_name}",
                            domain="financials",
                            source_id=document.source_id,
                            source_url=document.source_url,
                            published_at=document.published_at,
                            effective_at=None,
                            collected_at=document.valid_from,
                            valid_from=document.valid_from,
                            version=document.version,
                            license_policy=(
                                "official-public-attachment-personal-research"
                            ),
                            quality_status=document.quality_status,
                        )

            for action in self.action_repository.visible_actions(
                member.ts_code,
                context.report_cutoff_at,
                context.known_at,
            ):
                add_fact_source(
                    record_id=action.record_id,
                    domain="corporate_actions",
                    source_id=action.source_id,
                    source_url=action.source_url,
                    published_at=action.published_at,
                    effective_at=None,
                    collected_at=action.collected_at,
                    valid_from=action.valid_from,
                    version=action.version,
                    license_policy=action.license_policy,
                    quality_status=action.quality_status,
                )
            for record in self.dividend_repository.visible_records(
                member.ts_code,
                context.report_cutoff_at,
                context.known_at,
            ):
                add_fact_source(
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
            screen = self.risk_repository.visible_screen(
                member.ts_code,
                as_of=context.report_cutoff_at,
                known_at=context.known_at,
            )
            if screen is not None:
                add_fact_source(
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
        event_cutoff_at = context.event_cutoff_at or context.report_cutoff_at
        for event in self.event_repository.visible_events(
            as_of=event_cutoff_at,
            known_at=context.known_at,
        ):
            add(
                ReportSource(
                    record_id=event.record_id,
                    domain="events",
                    source_name=source_name(event.source_id),
                    source_url=event.source_url,
                    published_at=event.published_at,
                    effective_at=event.effective_at,
                    collected_at=event.collected_at,
                    valid_from=event.valid_from,
                    version=event.version,
                    license_policy=event.license_policy,
                    quality_status=event.quality_status,
                )
            )
        return tuple(records[key] for key in sorted(records))


__all__ = ["PilotProductionStages", "_coverage_quality_status"]
