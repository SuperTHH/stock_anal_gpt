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
from hengce.contracts.pilot import AcquisitionManifestItem
from hengce.policy.guard import PolicyGuard
from hengce.raw_store.store import RawObjectStore
from hengce.reports.publisher import ReportPublisher
from hengce.services.pilot_reconstruction import (
    PilotStageContext,
    StageHandler,
)
from hengce.services.pilot_universe import PilotUniverseSelector
from hengce.state.pilot_repository import PilotRepository
from hengce.state.report_repository import ReportRepository
from hengce.state.repository import StateRepository
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
    strategy.value: "pilot-rule-narrative-v1"
    for strategy in StrategyType
}


class PilotProductionStages:
    """Production stage handlers for the private, fail-closed pilot loop."""

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
        self.pilot_repository = PilotRepository(state.path)
        self.market_warehouse = MarketWarehouse(data_dir / "normalized")
        self.report_repository = ReportRepository(state.path)

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
        partition = (
            self.market_warehouse.dataset
            / f"trade_date={context.market_date.isoformat()}"
        )
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
            (
                self.market_warehouse.dataset
                / f"trade_date={context.market_date.isoformat()}"
            ).glob("part-*.parquet")
        )
        if len(artifacts) != 1:
            raise ValueError("PILOT_MARKET_ARTIFACT_INVALID")
        market_hash = self.market_warehouse.validate_artifact(
            artifacts[0],
            context.market_date,
            len(bars),
        )
        existing = self.pilot_repository.get_universe_for_date(
            context.market_date
        )
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
            if (
                universe.report_cutoff_at != context.report_cutoff_at
                or universe.input_hashes
                != {
                    "market": market_hash,
                    "security_master": master.universe_hash,
                }
            ):
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
        existing = self.pilot_repository.list_manifest(
            universe.universe_id
        )
        if not existing:
            self.pilot_repository.insert_manifest(expected)
            existing = self.pilot_repository.list_manifest(
                universe.universe_id
            )
        if {item.item_id for item in existing} != {
            item.item_id for item in expected
        }:
            raise ValueError("ACQUISITION_MANIFEST_ID_SET_CHANGED")
        return {
            "universe_id": universe.universe_id,
            "manifest_total": len(existing),
            "periodic_report_count": sum(
                item.document_kind is DocumentKind.PERIODIC_REPORT
                for item in existing
            ),
            "manifest_status_distribution": self._status_distribution(
                existing
            ),
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
        manifest = self.pilot_repository.list_manifest(
            universe.universe_id
        )
        result = ManualInbox(
            guard=PolicyGuard(self.state),
            raw_store=RawObjectStore(self.data_dir / "raw"),
            clock=self.clock,
        ).scan(self.data_dir / "manual_inbox", manifest)
        for accepted in result.accepted:
            current = self.pilot_repository.get_manifest_item(
                accepted.item_id
            )
            if current is None:
                raise ValueError("ACQUISITION_ITEM_NOT_FOUND")
            self.pilot_repository.transition(
                accepted.item_id,
                current.status,
                accepted,
                context.known_at,
            )
        for item in self.pilot_repository.list_manifest(
            universe.universe_id
        ):
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
        manifest = self.pilot_repository.list_manifest(
            universe.universe_id
        )
        return {
            "accepted_manual_attachment_count": len(result.accepted),
            "rejected_manual_attachment_count": len(result.rejected),
            "manual_todo_count": self._manual_todo_count(manifest),
            "manifest_status_distribution": self._status_distribution(
                manifest
            ),
        }

    def ingest_documents(
        self,
        context: PilotStageContext,
    ) -> Mapping[str, object]:
        manifest = self.pilot_repository.list_manifest(
            self._universe(context).universe_id
        )
        ingested = tuple(
            item
            for item in manifest
            if item.status is AcquisitionStatus.INGESTED
        )
        xbrl_count = sum(
            item.document_kind is DocumentKind.PERIODIC_REPORT
            and item.source_id in {"sse", "szse"}
            for item in ingested
        )
        pdf_count = sum(
            item.document_kind is DocumentKind.PERIODIC_REPORT
            and item.source_id == "cninfo"
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
            "manifest_status_distribution": self._status_distribution(
                manifest
            ),
        }

    def assemble_facts(
        self,
        context: PilotStageContext,
    ) -> Mapping[str, object]:
        del context
        filing_count, fact_count = self._published_filing_counts()
        return {
            "published_filing_count": filing_count,
            "financial_fact_count": fact_count,
            "corporate_action_count": self._table_count(
                "corporate_action_versions"
            ),
            "share_capital_count": 0,
            "fallback_reason_counts": {},
        }

    def calculate_metrics(
        self,
        context: PilotStageContext,
    ) -> Mapping[str, object]:
        del context
        return {"derived_metric_count": 0}

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
                strategy.value: len(result.candidates)
                for strategy, result in results.items()
            },
        }

    def publish_report(
        self,
        context: PilotStageContext,
    ) -> Mapping[str, object]:
        universe = self._universe(context)
        manifest = self.pilot_repository.list_manifest(
            universe.universe_id
        )
        results = self._pool_results(context)
        filing_count, fact_count = self._published_filing_counts()
        statuses = self._status_distribution(manifest)
        xbrl_count = sum(
            item.status is AcquisitionStatus.INGESTED
            and item.document_kind is DocumentKind.PERIODIC_REPORT
            and item.source_id in {"sse", "szse"}
            for item in manifest
        )
        pdf_count = sum(
            item.status is AcquisitionStatus.INGESTED
            and item.document_kind is DocumentKind.PERIODIC_REPORT
            and item.source_id == "cninfo"
            for item in manifest
        )
        quality_summary: dict[str, object] = {
            "manifest_status_distribution": statuses,
            "xbrl_used_count": xbrl_count,
            "pdf_used_count": pdf_count,
            "fallback_reason_counts": {},
            "published_filing_count": filing_count,
            "financial_fact_count": fact_count,
            "corporate_action_count": self._table_count(
                "corporate_action_versions"
            ),
            "share_capital_count": 0,
            "derived_metric_count": 0,
            "narrative_template_versions": dict(
                _NARRATIVE_TEMPLATE_VERSIONS
            ),
        }
        identity = {
            "market_date": context.market_date.isoformat(),
            "known_at": context.known_at.isoformat(),
            "universe_id": universe.universe_id,
        }
        digest = hashlib.sha256(
            json.dumps(
                identity,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        report_id = (
            f"pilot-{context.market_date.isoformat()}-{digest[:16]}"
        )
        publication = ReportPublisher(
            self.report_repository,
            self.data_dir / "reports",
        ).publish(
            report_id=report_id,
            report_date=context.market_date,
            market_cutoff_at=context.report_cutoff_at,
            event_cutoff_at=context.report_cutoff_at,
            generated_at=context.known_at,
            candidate_pools={
                strategy: list(result.candidates)
                for strategy, result in results.items()
            },
            data_domain_statuses={
                "market": QualityStatus.VALID,
                "security_master": QualityStatus.VALID,
                "pilot_universe": QualityStatus.VALID,
                "manifest": QualityStatus.VALID,
                "financials": (
                    QualityStatus.VALID
                    if fact_count > 0
                    else QualityStatus.MISSING
                ),
                "actions": (
                    QualityStatus.VALID
                    if quality_summary["corporate_action_count"]
                    else QualityStatus.MISSING
                ),
                "metrics": QualityStatus.MISSING,
            },
            strategy_evidence={
                strategy: result.evidence
                for strategy, result in results.items()
                if result.evidence is not None
            },
            strategy_versions={
                strategy: definition.version
                for strategy, definition in _DEFINITIONS.items()
            },
            source_records=(),
            pool_readiness={
                strategy: result.readiness
                for strategy, result in results.items()
            },
            universe_id=universe.universe_id,
            report_cutoff_at=context.report_cutoff_at,
            known_at=context.known_at,
            generation_started_at=context.known_at,
            manual_todo_count=self._manual_todo_count(manifest),
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
            "manual_todo_count": self._manual_todo_count(manifest),
            "manifest_status_distribution": statuses,
            "xbrl_used_count": xbrl_count,
            "pdf_used_count": pdf_count,
            "fallback_reason_counts": {},
            "financial_fact_count": fact_count,
            "corporate_action_count": quality_summary[
                "corporate_action_count"
            ],
            "share_capital_count": 0,
            "derived_metric_count": 0,
            "pool_coverage": {
                strategy.value: str(result.readiness.coverage_ratio)
                for strategy, result in results.items()
            },
            "pool_statuses": {
                strategy.value: result.readiness.status.value
                for strategy, result in results.items()
            },
            "pool_candidate_counts": {
                strategy.value: len(result.candidates)
                for strategy, result in results.items()
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
        target = root / (
            f"pilot-{context.market_date.isoformat()}-{digest}.json"
        )
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
        universe = self.pilot_repository.get_universe_for_date(
            context.market_date
        )
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
        hard_filters = {
            member.ts_code: HardFilterResult(
                passed=True,
                reasons=(),
                filter_version="pilot-universe-hard-filters-v1",
                source_record_ids=member.evidence_record_ids,
            )
            for member in universe.members
        }
        inputs = PilotStrategyInputBuilder().build(
            universe,
            metrics={},
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
        except sqlite3.Error:
            return 0, 0
        return (
            (int(row[0]), int(row[1]))
            if row is not None
            else (0, 0)
        )

    def _table_count(self, table: str) -> int:
        if table not in {"corporate_action_versions"}:
            raise ValueError("PILOT_TABLE_NOT_ALLOWED")
        try:
            with sqlite3.connect(self.state.path) as connection:
                row = connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()
        except sqlite3.Error:
            return 0
        return int(row[0]) if row is not None else 0

    @staticmethod
    def _status_distribution(
        manifest: tuple[AcquisitionManifestItem, ...],
    ) -> dict[str, int]:
        return dict(
            sorted(
                Counter(item.status.value for item in manifest).items()
            )
        )

    @staticmethod
    def _manual_todo_count(
        manifest: tuple[AcquisitionManifestItem, ...],
    ) -> int:
        return sum(
            item.status not in {
                AcquisitionStatus.INGESTED,
                AcquisitionStatus.REJECTED,
            }
            for item in manifest
        )


__all__ = ["PilotProductionStages"]
