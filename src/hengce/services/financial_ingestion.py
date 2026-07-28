"""Idempotent orchestration for one XBRL filing descriptor."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from hengce.contracts.enums import QualityStatus, RunStatus
from hengce.contracts.financial import (
    FilingDescriptor,
    FinancialFact,
    FinancialFiling,
    TaxonomyPackageRef,
)
from hengce.contracts.policy import SourcePolicy
from hengce.contracts.run import RunRecord
from hengce.financials.mapping import FinancialFactNormalizer
from hengce.financials.package import SafePackageMaterializer
from hengce.financials.quality import FinancialQualityResult, FinancialQualityValidator
from hengce.financials.xbrl import XbrlParseResult, XbrlProcessor
from hengce.policy.guard import PolicyDenied, PolicyGuard
from hengce.raw_store.store import RawObjectStore
from hengce.state.financial_repository import (
    FinancialArtifactRecord,
    FinancialFilingRepository,
)
from hengce.state.repository import StateRepository
from hengce.warehouse.financial import FinancialArtifact, FinancialFactWarehouse

ERROR_STATUS = {
    "RAW_PAYLOAD_INTEGRITY_ERROR": RunStatus.FAILED,
    "FINANCIAL_TAXONOMY_MISSING": RunStatus.BLOCKED,
    "FINANCIAL_XBRL_PARSE_ERROR": RunStatus.FAILED,
    "FINANCIAL_PARQUET_INTEGRITY_ERROR": RunStatus.FAILED,
    "FINANCIAL_NUMERIC_FACTS_MISSING": RunStatus.PARTIAL,
    "FINANCIAL_FACT_UNMAPPED": RunStatus.PARTIAL,
    "FINANCIAL_FACT_CONFLICT": RunStatus.PARTIAL,
}

_TERMINAL_STATUSES = frozenset(
    {
        RunStatus.SUCCEEDED,
        RunStatus.PARTIAL,
        RunStatus.FAILED,
        RunStatus.BLOCKED,
    }
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def no_stage_hook(_stage_name: str) -> None:
    return None


@dataclass(frozen=True)
class FinancialIngestionResult:
    filing_id: str
    run_id: str
    run_status: RunStatus
    fact_count: int
    conflict_count: int
    artifact_path: str | None
    artifact_hash: str | None
    error_code: str | None


class FinancialIngestionService:
    def __init__(
        self,
        *,
        guard: PolicyGuard,
        raw_store: RawObjectStore,
        repository: FinancialFilingRepository,
        materializer: SafePackageMaterializer,
        processor: XbrlProcessor,
        normalizer: FinancialFactNormalizer,
        validator: FinancialQualityValidator,
        warehouse: FinancialFactWarehouse,
        state: StateRepository,
        clock: Callable[[], datetime] = utc_now,
        stage_hook: Callable[[str], None] = no_stage_hook,
    ) -> None:
        self.guard = guard
        self.raw_store = raw_store
        self.repository = repository
        self.materializer = materializer
        self.processor = processor
        self.normalizer = normalizer
        self.validator = validator
        self.warehouse = warehouse
        self.state = state
        self.clock = clock
        self.stage_hook = stage_hook

    def run(self, descriptor: FilingDescriptor) -> FinancialIngestionResult:
        filing_id = self._filing_id(descriptor)
        run_id = f"financial_xbrl:{filing_id}"
        existing_run = self._find_run(run_id)
        if existing_run is not None and existing_run.run_status in _TERMINAL_STATUSES:
            return self._result_from_run(existing_run, filing_id)

        run = self._running_record(descriptor, run_id, existing_run)
        self.state.record_run(run)
        try:
            policy = self.guard.validate(
                descriptor.source_id,
                str(descriptor.source_url),
                "xbrl",
                "services.financial_ingestion",
            )
            taxonomies = self.repository.get_taxonomies(descriptor.taxonomy_refs)
            staged = self.repository.get_filing(filing_id)
            if staged is not None and Path(staged.expected_path).is_file():
                self._validate_raw_sources(descriptor, staged)
                return self._recover_staged(run, staged)

            return self._ingest(
                descriptor=descriptor,
                run=run,
                policy=policy,
                taxonomies=taxonomies,
                staged=staged,
            )
        except PolicyDenied as error:
            return self._terminal_error(
                run,
                filing_id,
                RunStatus.BLOCKED,
                error.reason_code,
            )
        except ValueError as error:
            error_code = str(error)
            status = ERROR_STATUS.get(error_code)
            if status is None:
                raise
            return self._terminal_error(run, filing_id, status, error_code)

    def _ingest(
        self,
        *,
        descriptor: FilingDescriptor,
        run: RunRecord,
        policy: SourcePolicy,
        taxonomies: tuple[TaxonomyPackageRef, ...],
        staged: FinancialArtifactRecord | None,
    ) -> FinancialIngestionResult:
        with self.materializer.materialize(descriptor, taxonomies) as materialized:
            parsed = self.processor.parse(materialized)

        filing = (
            staged.filing
            if staged is not None
            else self._candidate_filing(
                descriptor,
                parsed,
                policy,
                tuple(taxonomy.raw_object_hash for taxonomy in taxonomies),
            )
        )
        normalized = self.normalizer.normalize(filing, list(parsed.facts))
        quality = self.validator.validate(filing, normalized)
        final_valid_from = staged.filing.valid_from if staged is not None else self.clock()
        filing, facts = self._finalize_filing_and_facts(
            filing,
            quality,
            final_valid_from,
        )
        facts = self._link_correction_facts(filing, facts)
        artifact = self.warehouse.expected_artifact(filing, facts)
        self.repository.stage_filing(
            filing,
            str(artifact.path),
            artifact.content_hash,
            artifact.fact_count,
        )
        published = self.warehouse.write_facts(filing, facts)
        self.warehouse.validate_artifact(published, filing.filing_id)
        self.stage_hook("after_artifact_write")
        return self._publish_and_terminalize(run, filing, quality, published)

    def _recover_staged(
        self,
        run: RunRecord,
        staged: FinancialArtifactRecord,
    ) -> FinancialIngestionResult:
        artifact = FinancialArtifact(
            path=Path(staged.expected_path),
            content_hash=staged.expected_hash,
            fact_count=staged.expected_count,
        )
        self.warehouse.validate_artifact(artifact, staged.filing.filing_id)
        facts = [
            FinancialFact.model_validate(row) for row in self.warehouse.read_artifact(artifact.path)
        ]
        quality = self.validator.validate(staged.filing, facts)
        return self._publish_and_terminalize(run, staged.filing, quality, artifact)

    def _publish_and_terminalize(
        self,
        run: RunRecord,
        filing: FinancialFiling,
        quality: FinancialQualityResult,
        artifact: FinancialArtifact,
    ) -> FinancialIngestionResult:
        self.repository.record_conflicts(list(quality.conflicts))
        if not self.repository.publish_filing(
            filing.filing_id,
            str(artifact.path),
            artifact.content_hash,
            artifact.fact_count,
        ):
            raise RuntimeError("FINANCIAL_MANIFEST_PUBLICATION_FAILED")

        error_code = self._quality_error_code(quality)
        status = ERROR_STATUS[error_code] if error_code is not None else RunStatus.SUCCEEDED
        terminal = self._terminal_run(
            run,
            status,
            error_code=error_code,
            published_report_id=filing.filing_id,
        )
        self.state.record_run(terminal)
        return FinancialIngestionResult(
            filing_id=filing.filing_id,
            run_id=run.run_id,
            run_status=status,
            fact_count=artifact.fact_count,
            conflict_count=len(quality.conflicts),
            artifact_path=str(artifact.path),
            artifact_hash=artifact.content_hash,
            error_code=error_code,
        )

    def _candidate_filing(
        self,
        descriptor: FilingDescriptor,
        parsed: XbrlParseResult,
        policy: SourcePolicy,
        taxonomy_hashes: tuple[str, ...],
    ) -> FinancialFiling:
        filing_id = self._filing_id(descriptor)
        predecessors = [
            record
            for record in self.repository.list_filing_versions(
                descriptor.ts_code,
                descriptor.report_period,
            )
            if record.filing.source_id == descriptor.source_id
            and record.filing.ts_code == descriptor.ts_code
            and record.filing.report_period == descriptor.report_period
            and record.filing.report_type == descriptor.report_type
            and record.filing.filing_id != filing_id
            and record.artifact_status == "PUBLISHED"
        ]
        predecessor = predecessors[-1].filing if predecessors else None
        mapping_version = self.normalizer._registry.mapping_version
        filing_version = f"xbrl-{descriptor.raw_object_hash}"
        return FinancialFiling(
            record_id=filing_id,
            filing_id=filing_id,
            source_id=descriptor.source_id,
            source_url=descriptor.source_url,
            published_at=descriptor.published_at,
            effective_at=descriptor.published_at,
            collected_at=descriptor.collected_at,
            version=f"{filing_version}:{parsed.parser_version}:{mapping_version}",
            content_hash=descriptor.raw_object_hash,
            license_policy=policy.attachment_rule,
            quality_status=QualityStatus.UNVERIFIED,
            supersedes_id=predecessor.filing_id if predecessor is not None else None,
            valid_from=descriptor.collected_at,
            ts_code=descriptor.ts_code,
            exchange=descriptor.exchange,
            report_period=descriptor.report_period,
            report_type=descriptor.report_type,
            announcement_at=descriptor.published_at,
            taxonomy=descriptor.taxonomy_refs,
            taxonomy_hashes=taxonomy_hashes,
            raw_object_hash=descriptor.raw_object_hash,
            filing_version=filing_version,
            parser_name=parsed.parser_name,
            parser_version=parsed.parser_version,
            mapping_version=mapping_version,
            fact_count=len(parsed.facts),
            conflict_count=0,
            is_restated=predecessor is not None,
        )

    @staticmethod
    def _finalize_filing_and_facts(
        filing: FinancialFiling,
        quality: FinancialQualityResult,
        final_valid_from: datetime,
    ) -> tuple[FinancialFiling, list[FinancialFact]]:
        facts = [fact.model_copy(update={"valid_from": final_valid_from}) for fact in quality.facts]
        finalized = filing.model_copy(
            update={
                "quality_status": quality.filing_quality_status,
                "fact_count": len(facts),
                "conflict_count": len(quality.conflicts),
                "valid_from": final_valid_from,
            }
        )
        return finalized, facts

    def _link_correction_facts(
        self,
        filing: FinancialFiling,
        facts: list[FinancialFact],
    ) -> list[FinancialFact]:
        if filing.supersedes_id is None:
            return facts
        predecessor = self.repository.get_filing(filing.supersedes_id)
        if predecessor is None or predecessor.artifact_status != "PUBLISHED":
            raise RuntimeError("FINANCIAL_SUPERSEDED_FILING_MISSING")
        old_facts = sorted(
            (
                FinancialFact.model_validate(row)
                for row in self.warehouse.read_artifact(Path(predecessor.expected_path))
            ),
            key=lambda fact: fact.fact_id,
        )
        old_by_comparison = {fact.comparison_identity_hash: fact.fact_id for fact in old_facts}
        return [
            fact.model_copy(
                update={"supersedes_id": old_by_comparison[fact.comparison_identity_hash]}
            )
            if fact.comparison_identity_hash in old_by_comparison
            else fact
            for fact in facts
        ]

    def _validate_raw_sources(
        self,
        descriptor: FilingDescriptor,
        staged: FinancialArtifactRecord,
    ) -> None:
        self.raw_store.validate_content_hash(descriptor.raw_object_hash)
        for taxonomy_hash in staged.filing.taxonomy_hashes:
            self.raw_store.validate_content_hash(taxonomy_hash)

    def _terminal_error(
        self,
        run: RunRecord,
        filing_id: str,
        status: RunStatus,
        error_code: str,
    ) -> FinancialIngestionResult:
        terminal = self._terminal_run(run, status, error_code=error_code)
        self.state.record_run(terminal)
        filing = self.repository.get_filing(filing_id)
        return FinancialIngestionResult(
            filing_id=filing_id,
            run_id=run.run_id,
            run_status=status,
            fact_count=filing.expected_count if filing is not None else 0,
            conflict_count=filing.filing.conflict_count if filing is not None else 0,
            artifact_path=filing.expected_path if filing is not None else None,
            artifact_hash=filing.expected_hash if filing is not None else None,
            error_code=error_code,
        )

    def _terminal_run(
        self,
        run: RunRecord,
        status: RunStatus,
        *,
        error_code: str | None,
        published_report_id: str | None = None,
    ) -> RunRecord:
        return run.model_copy(
            update={
                "finished_at": self.clock(),
                "run_status": status,
                "stage_statuses": {"ingestion": status.value},
                "error_code": error_code,
                "error_summary": error_code,
                "published_report_id": published_report_id,
            }
        )

    def _running_record(
        self,
        descriptor: FilingDescriptor,
        run_id: str,
        existing: RunRecord | None,
    ) -> RunRecord:
        if existing is not None:
            return existing.model_copy(
                update={
                    "finished_at": None,
                    "run_status": RunStatus.RUNNING,
                    "stage_statuses": {"ingestion": "RUNNING"},
                    "retry_count": existing.retry_count + 1,
                    "error_code": None,
                    "error_summary": None,
                    "published_report_id": None,
                }
            )
        return RunRecord(
            run_id=run_id,
            trade_date=descriptor.report_period,
            run_type="financial_xbrl",
            started_at=self.clock(),
            run_status=RunStatus.RUNNING,
            stage_statuses={"ingestion": "RUNNING"},
        )

    def _find_run(self, run_id: str) -> RunRecord | None:
        return next(
            (
                run
                for run in self.state.list_runs(run_type="financial_xbrl")
                if run.run_id == run_id
            ),
            None,
        )

    def _result_from_run(
        self,
        run: RunRecord,
        filing_id: str,
    ) -> FinancialIngestionResult:
        filing = self.repository.get_filing(filing_id)
        return FinancialIngestionResult(
            filing_id=filing_id,
            run_id=run.run_id,
            run_status=run.run_status,
            fact_count=filing.expected_count if filing is not None else 0,
            conflict_count=filing.filing.conflict_count if filing is not None else 0,
            artifact_path=filing.expected_path if filing is not None else None,
            artifact_hash=filing.expected_hash if filing is not None else None,
            error_code=run.error_code,
        )

    @staticmethod
    def _quality_error_code(quality: FinancialQualityResult) -> str | None:
        return next(
            (issue.code for issue in quality.issues if issue.code in ERROR_STATUS),
            None,
        )

    @staticmethod
    def _filing_id(descriptor: FilingDescriptor) -> str:
        payload = json.dumps(
            {
                "source_id": descriptor.source_id,
                "ts_code": descriptor.ts_code,
                "report_period": descriptor.report_period.isoformat(),
                "report_type": descriptor.report_type.value,
                "raw_object_hash": descriptor.raw_object_hash,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"filing-{hashlib.sha256(payload).hexdigest()}"


__all__ = [
    "ERROR_STATUS",
    "FinancialIngestionResult",
    "FinancialIngestionService",
]
