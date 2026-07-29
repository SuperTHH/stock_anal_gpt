import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

from hengce.contracts.enums import QualityStatus, ReportStatus, StrategyType
from hengce.contracts.official_event import OfficialEvent, ReportSource
from hengce.contracts.strategy import (
    ReportSnapshot,
    StrategyCandidate,
    StrategyRunEvidence,
)
from hengce.state.report_repository import ReportRepository

from .integrity import compute_artifact_hash


@dataclass(frozen=True, slots=True)
class PublicationResult:
    published: bool
    snapshot: ReportSnapshot | None
    artifact_path: Path | None
    blocked_reasons: tuple[str, ...]


class ReportPublisher:
    def __init__(self, repository: ReportRepository, report_root: Path) -> None:
        self.repository = repository
        self.report_root = report_root

    def publish(
        self,
        *,
        report_id: str,
        report_date: date,
        market_cutoff_at: datetime,
        event_cutoff_at: datetime,
        generated_at: datetime,
        candidate_pools: dict[StrategyType, list[StrategyCandidate]],
        data_domain_statuses: dict[str, QualityStatus],
        strategy_evidence: dict[StrategyType, StrategyRunEvidence],
        strategy_versions: dict[StrategyType, str] | None = None,
        official_events: tuple[OfficialEvent, ...] = (),
        source_records: tuple[ReportSource, ...] = (),
    ) -> PublicationResult:
        blocked = tuple(
            f"REPORT_DOMAIN_NOT_READY:{name}"
            for name, status in sorted(data_domain_statuses.items())
            if status not in {QualityStatus.VALID, QualityStatus.DERIVED}
        )
        if blocked:
            return PublicationResult(False, None, None, blocked)
        if set(candidate_pools) != set(StrategyType):
            return PublicationResult(
                False,
                None,
                None,
                ("REPORT_STRATEGY_POOL_INCOMPLETE",),
            )
        for strategy, candidates in candidate_pools.items():
            if any(candidate.strategy_type is not strategy for candidate in candidates):
                raise ValueError("REPORT_STRATEGY_POOL_MIXED")
            if any(
                candidate.report_date != report_date
                for candidate in candidates
            ):
                raise ValueError("REPORT_CANDIDATE_DATE_MISMATCH")
            if any(
                candidate.data_cutoff_at > market_cutoff_at
                or candidate.known_at > generated_at
                for candidate in candidates
            ):
                raise ValueError("REPORT_CANDIDATE_CUTOFF_VIOLATION")

        strategy_versions = self._resolve_versions(candidate_pools, strategy_versions)
        evidence_blocked = self._validate_evidence(
            candidate_pools,
            strategy_versions,
            strategy_evidence,
        )
        if evidence_blocked:
            return PublicationResult(False, None, None, evidence_blocked)
        if any(
            event.published_at is None
            or event.published_at > event_cutoff_at
            or (event.effective_at is not None and event.effective_at > event_cutoff_at)
            or event.collected_at > generated_at
            or event.valid_from > generated_at
            for event in official_events
        ):
            raise ValueError("REPORT_EVENT_CUTOFF_VIOLATION")
        if any(
            event.quality_status
            not in {QualityStatus.VALID, QualityStatus.DERIVED}
            for event in official_events
        ):
            raise ValueError("REPORT_EVENT_QUALITY_BLOCKED")
        source_ids = [source.record_id for source in source_records]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("REPORT_SOURCE_RECORD_DUPLICATE")
        source_by_id = {source.record_id: source for source in source_records}
        referenced = {
            source_id
            for candidates in candidate_pools.values()
            for candidate in candidates
            for factor in candidate.factor_details
            for source_id in factor.source_record_ids
        }
        unresolved = sorted(referenced - set(source_by_id))
        if unresolved:
            return PublicationResult(
                False,
                None,
                None,
                tuple(
                    f"REPORT_SOURCE_LINEAGE_UNRESOLVED:{source_id}"
                    for source_id in unresolved
                ),
            )
        if not source_records:
            return PublicationResult(
                False,
                None,
                None,
                ("REPORT_SOURCE_LINEAGE_MISSING",),
            )
        for source in source_records:
            if (
                (source.published_at is not None and source.published_at > market_cutoff_at)
                or (
                    source.effective_at is not None
                    and source.effective_at > market_cutoff_at
                )
                or source.collected_at > generated_at
                or source.valid_from > generated_at
            ):
                raise ValueError("REPORT_SOURCE_CUTOFF_VIOLATION")
            if source.quality_status not in {
                QualityStatus.VALID,
                QualityStatus.DERIVED,
            }:
                raise ValueError("REPORT_SOURCE_QUALITY_BLOCKED")
        for candidates in candidate_pools.values():
            for candidate in candidates:
                for factor in candidate.factor_details:
                    if any(
                        (
                            source_by_id[source_id].published_at is not None
                            and source_by_id[source_id].published_at
                            > candidate.data_cutoff_at
                        )
                        or (
                            source_by_id[source_id].effective_at is not None
                            and source_by_id[source_id].effective_at
                            > candidate.data_cutoff_at
                        )
                        for source_id in factor.source_record_ids
                    ):
                        raise ValueError("REPORT_SOURCE_AFTER_CANDIDATE_CUTOFF")
                    if any(
                        source_by_id[source_id].collected_at > candidate.known_at
                        or source_by_id[source_id].valid_from > candidate.known_at
                        for source_id in factor.source_record_ids
                    ):
                        raise ValueError("REPORT_SOURCE_NOT_KNOWN_TO_CANDIDATE")

        manifest_payload = {
            "report_id": report_id,
            "report_date": report_date.isoformat(),
            "market_cutoff_at": market_cutoff_at.isoformat(),
            "event_cutoff_at": event_cutoff_at.isoformat(),
            "generated_at": generated_at.isoformat(),
            "candidate_pools": {
                strategy.value: [
                    candidate.model_dump(mode="json") for candidate in candidates
                ]
                for strategy, candidates in sorted(
                    candidate_pools.items(),
                    key=lambda item: item[0].value,
                )
            },
            "data_domain_statuses": {
                name: status.value for name, status in sorted(data_domain_statuses.items())
            },
            "strategy_versions": {
                strategy.value: version
                for strategy, version in sorted(
                    strategy_versions.items(),
                    key=lambda item: item[0].value,
                )
            },
            "official_events": [
                event.model_dump(mode="json") for event in official_events
            ],
            "source_records": [
                source.model_dump(mode="json") for source in source_records
            ],
            "strategy_evidence": {
                strategy.value: evidence.model_dump(mode="json")
                for strategy, evidence in sorted(
                    strategy_evidence.items(),
                    key=lambda item: item[0].value,
                )
            },
        }
        existing = self.repository.get_report(report_id)
        snapshot = ReportSnapshot(
            report_id=report_id,
            report_date=report_date,
            market_cutoff_at=market_cutoff_at,
            event_cutoff_at=event_cutoff_at,
            generated_at=generated_at,
            published_at=generated_at,
            report_status=ReportStatus.PUBLISHED,
            previous_report_id=(
                existing.snapshot.previous_report_id
                if existing is not None
                else self.repository.latest_report_id()
            ),
            data_domain_statuses=data_domain_statuses,
            strategy_versions=strategy_versions,
            manifest_hash="0" * 64,
        )
        artifact_payload = {
            "snapshot": snapshot.model_dump(mode="json"),
            **manifest_payload,
        }
        manifest_hash = compute_artifact_hash(artifact_payload)
        snapshot = snapshot.model_copy(update={"manifest_hash": manifest_hash})
        artifact_payload["snapshot"] = snapshot.model_dump(mode="json")
        if existing is not None:
            if existing.snapshot.manifest_hash != manifest_hash:
                raise ValueError("REPORT_IMMUTABILITY_CONFLICT")
            return PublicationResult(True, existing.snapshot, existing.artifact_path, ())
        target = (
            self.report_root
            / f"report_date={report_date.isoformat()}"
            / f"{report_id}-{manifest_hash}.json"
        )
        self._write_immutable_json(target, artifact_payload)
        stored = self.repository.publish(snapshot, target)
        return PublicationResult(True, stored.snapshot, stored.artifact_path, ())

    @staticmethod
    def _validate_evidence(
        candidate_pools: dict[StrategyType, list[StrategyCandidate]],
        strategy_versions: dict[StrategyType, str],
        evidence: dict[StrategyType, StrategyRunEvidence],
    ) -> tuple[str, ...]:
        if set(evidence) != set(StrategyType):
            return ("REPORT_STRATEGY_EVIDENCE_INCOMPLETE",)
        blocked: list[str] = []
        for strategy in StrategyType:
            item = evidence[strategy]
            if (
                item.strategy_type is not strategy
                or item.strategy_version != strategy_versions[strategy]
                or item.published_candidate_count != len(candidate_pools[strategy])
            ):
                raise ValueError(f"REPORT_STRATEGY_EVIDENCE_MISMATCH:{strategy.value}")
            if not item.completed:
                blocked.append(f"REPORT_STRATEGY_EVALUATION_INCOMPLETE:{strategy.value}")
            elif item.input_count == 0:
                blocked.append(f"REPORT_STRATEGY_EVALUATION_EMPTY:{strategy.value}")
            elif (
                not candidate_pools[strategy]
                and item.data_insufficient_count == item.input_count
            ):
                blocked.append(
                    f"REPORT_STRATEGY_ALL_DATA_INSUFFICIENT:{strategy.value}"
                )
        return tuple(blocked)

    @staticmethod
    def _resolve_versions(
        candidate_pools: dict[StrategyType, list[StrategyCandidate]],
        supplied: dict[StrategyType, str] | None,
    ) -> dict[StrategyType, str]:
        if supplied is not None and set(supplied) != set(StrategyType):
            raise ValueError("REPORT_STRATEGY_VERSION_SET_INCOMPLETE")
        resolved: dict[StrategyType, str] = {}
        for strategy, candidates in candidate_pools.items():
            candidate_versions = {candidate.strategy_version for candidate in candidates}
            if len(candidate_versions) > 1:
                raise ValueError(f"REPORT_STRATEGY_VERSION_INVALID:{strategy.value}")
            if supplied is None:
                if not candidate_versions:
                    raise ValueError(
                        f"REPORT_STRATEGY_VERSION_REQUIRED:{strategy.value}"
                    )
                resolved[strategy] = next(iter(candidate_versions))
                continue
            expected = supplied[strategy]
            if candidate_versions and candidate_versions != {expected}:
                raise ValueError(f"REPORT_STRATEGY_VERSION_INVALID:{strategy.value}")
            resolved[strategy] = expected
        return resolved

    @staticmethod
    def _write_immutable_json(target: Path, payload: dict[str, object]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        temporary = target.parent / f".{target.name}-{uuid4().hex}.tmp"
        try:
            temporary.write_bytes(encoded)
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.read_bytes() != encoded:
                    raise ValueError("REPORT_IMMUTABILITY_CONFLICT") from None
        finally:
            temporary.unlink(missing_ok=True)
