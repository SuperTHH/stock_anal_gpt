from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from hengce.acquisition.planner import AcquisitionPlanner
from hengce.api.app import PublishedReportReader
from hengce.contracts.enums import DocumentKind, StrategyType
from hengce.contracts.pilot import PilotUniverseSnapshot
from hengce.reports.integrity import compute_artifact_hash
from hengce.services.pilot_universe import PILOT_QUOTAS
from hengce.state.pilot_repository import PilotRepository
from hengce.state.report_repository import ReportRepository, StoredReport

IgnoreChecker = Callable[[Path], bool]


class PilotAcceptanceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    errors: tuple[str, ...]
    report_id: str | None = None
    api_report_id: str | None = None
    universe_id: str | None = None
    market_date: date
    report_cutoff_at: datetime
    universe_count: int = Field(ge=0)
    board_quotas: dict[str, int]
    universe_manifest_hash: str | None = None
    periodic_manifest_id_hash: str | None = None
    manifest_total: int = Field(ge=0)
    periodic_report_count: int = Field(ge=0)
    manifest_status_distribution: dict[str, int]
    xbrl_used_count: int = Field(ge=0)
    pdf_used_count: int = Field(ge=0)
    fallback_reason_counts: dict[str, int]
    financial_fact_count: int = Field(ge=0)
    corporate_action_count: int = Field(ge=0)
    share_capital_count: int = Field(ge=0)
    derived_metric_count: int = Field(ge=0)
    pool_coverage: dict[str, str]
    pool_statuses: dict[str, str]
    pool_candidate_counts: dict[str, int]
    candidate_count: int = Field(ge=0)
    resolved_candidate_source_count: int = Field(ge=0)
    report_artifact_hash: str | None = None
    ignored_runtime_path_count: int = Field(ge=0)


class PilotAcceptanceValidator:
    _RUNTIME_PATHS = (
        "manual_inbox",
        "reports",
        "backups",
        "state/hengce.sqlite3",
        "raw",
        "warehouse",
        "normalized",
        "run_summaries",
    )

    def __init__(self, ignore_checker: IgnoreChecker | None = None) -> None:
        self._ignore_checker = ignore_checker

    def validate(
        self,
        *,
        data_dir: Path,
        market_date: date,
        report_cutoff_at: datetime,
    ) -> PilotAcceptanceSummary:
        if (
            report_cutoff_at.tzinfo is None
            or report_cutoff_at.utcoffset() is None
        ):
            raise ValueError("PILOT_ACCEPTANCE_CUTOFF_INVALID")
        data_dir = data_dir.resolve()
        database = data_dir / "state" / "hengce.sqlite3"
        errors: set[str] = set()

        universe = self._load_universe(database, market_date, errors)
        universe_count = len(universe.members) if universe is not None else 0
        board_quotas = (
            {str(key): value for key, value in universe.quotas.items()}
            if universe is not None
            else {}
        )
        universe_hash: str | None = None
        if universe is not None:
            universe_hash = self._universe_hash(universe)
            if universe.market_date != market_date:
                errors.add("PILOT_MARKET_DATE_MISMATCH")
            if universe.report_cutoff_at != report_cutoff_at:
                errors.add("PILOT_REPORT_CUTOFF_MISMATCH")
            if (
                universe_count != 30
                or board_quotas != PILOT_QUOTAS
            ):
                errors.add("PILOT_QUOTA_INVALID")
            if universe_hash != universe.manifest_hash:
                errors.add("PILOT_UNIVERSE_HASH_MISMATCH")

        manifest_total = 0
        periodic_count = 0
        periodic_hash: str | None = None
        status_distribution: dict[str, int] = {}
        if universe is not None:
            repository = PilotRepository(database)
            try:
                items = repository.list_manifest(universe.universe_id)
            except Exception:
                errors.add("ACQUISITION_MANIFEST_INVALID")
                items = ()
            manifest_total = len(items)
            periodic_ids = sorted(
                item.item_id
                for item in items
                if item.document_kind is DocumentKind.PERIODIC_REPORT
            )
            periodic_count = len(periodic_ids)
            periodic_hash = self._id_hash(periodic_ids)
            status_distribution = dict(
                sorted(Counter(item.status.value for item in items).items())
            )
            if manifest_total != 360:
                errors.add("ACQUISITION_MANIFEST_COUNT_INVALID")
            if periodic_count != 150:
                errors.add("PERIODIC_MANIFEST_COUNT_INVALID")
            if (
                universe_count == 30
                and board_quotas == PILOT_QUOTAS
            ):
                expected = AcquisitionPlanner().build(
                    universe,
                    report_cutoff_at,
                )
                expected_ids = {item.item_id for item in expected}
                if {item.item_id for item in items} != expected_ids:
                    errors.add("ACQUISITION_MANIFEST_ID_HASH_MISMATCH")
                expected_periodic_ids = sorted(
                    item.item_id
                    for item in expected
                    if item.document_kind is DocumentKind.PERIODIC_REPORT
                )
                if periodic_hash != self._id_hash(expected_periodic_ids):
                    errors.add("PERIODIC_MANIFEST_ID_HASH_MISMATCH")

        report_repository = ReportRepository(database)
        stored = self._load_latest(report_repository, errors)
        artifact = self._load_artifact(
            stored,
            data_dir / "reports",
            errors,
        )
        snapshot = (
            artifact.get("snapshot")
            if isinstance(artifact, dict)
            else None
        )
        if not isinstance(snapshot, dict):
            snapshot = {}
        report_id = self._optional_string(snapshot.get("report_id"))
        if stored is not None and report_id != stored.snapshot.report_id:
            errors.add("REPORT_LATEST_POINTER_INVALID")
        if snapshot.get("universe_id") != (
            universe.universe_id if universe is not None else None
        ):
            errors.add("REPORT_UNIVERSE_MISMATCH")
        if not self._datetime_equals(
            snapshot.get("report_cutoff_at"),
            report_cutoff_at,
        ):
            errors.add("REPORT_CUTOFF_MISMATCH")
        report_known_at = self._parse_aware_datetime(
            snapshot.get("known_at")
        )
        if report_known_at is None:
            errors.add("REPORT_KNOWN_AT_INVALID")

        quality = artifact.get("quality_summary", {}) if artifact else {}
        if not isinstance(quality, dict):
            quality = {}
            errors.add("REPORT_QUALITY_SUMMARY_INVALID")
        reported_distribution = quality.get(
            "manifest_status_distribution",
            {},
        )
        if reported_distribution != status_distribution:
            errors.add("MANIFEST_STATUS_SUMMARY_MISMATCH")

        xbrl_count = self._count(quality, "xbrl_used_count", errors)
        pdf_count = self._count(quality, "pdf_used_count", errors)
        financial_fact_count = self._count(
            quality,
            "financial_fact_count",
            errors,
        )
        corporate_action_count = self._count(
            quality,
            "corporate_action_count",
            errors,
        )
        share_capital_count = self._count(
            quality,
            "share_capital_count",
            errors,
        )
        derived_metric_count = self._count(
            quality,
            "derived_metric_count",
            errors,
        )
        fallback_reasons = self._count_mapping(
            quality.get("fallback_reason_counts"),
            "FALLBACK_REASON_COUNTS_INVALID",
            errors,
        )

        (
            pool_coverage,
            pool_statuses,
            pool_candidate_counts,
            candidate_count,
            source_count,
        ) = self._validate_candidates(
            artifact or {},
            report_cutoff_at,
            report_known_at or report_cutoff_at,
            errors,
        )

        api_report_id: str | None = None
        if stored is not None and not {
            "REPORT_ARTIFACT_INVALID",
            "REPORT_ARTIFACT_HASH_MISMATCH",
        }.intersection(errors):
            try:
                api_payload = PublishedReportReader(
                    report_repository,
                    data_dir / "reports",
                ).latest()
                api_report_id = self._optional_string(
                    api_payload.get("snapshot", {}).get("report_id")
                    if isinstance(api_payload.get("snapshot"), dict)
                    else None
                )
            except Exception:
                errors.add("API_REPORT_READ_INVALID")
            if api_report_id != report_id:
                errors.add("API_REPORT_ID_MISMATCH")

        ignored_count = self._validate_ignored_paths(data_dir, errors)
        return PilotAcceptanceSummary(
            passed=not errors,
            errors=tuple(sorted(errors)),
            report_id=report_id,
            api_report_id=api_report_id,
            universe_id=universe.universe_id if universe is not None else None,
            market_date=market_date,
            report_cutoff_at=report_cutoff_at,
            universe_count=universe_count,
            board_quotas=board_quotas,
            universe_manifest_hash=universe_hash,
            periodic_manifest_id_hash=periodic_hash,
            manifest_total=manifest_total,
            periodic_report_count=periodic_count,
            manifest_status_distribution=status_distribution,
            xbrl_used_count=xbrl_count,
            pdf_used_count=pdf_count,
            fallback_reason_counts=fallback_reasons,
            financial_fact_count=financial_fact_count,
            corporate_action_count=corporate_action_count,
            share_capital_count=share_capital_count,
            derived_metric_count=derived_metric_count,
            pool_coverage=pool_coverage,
            pool_statuses=pool_statuses,
            pool_candidate_counts=pool_candidate_counts,
            candidate_count=candidate_count,
            resolved_candidate_source_count=source_count,
            report_artifact_hash=(
                stored.snapshot.manifest_hash if stored is not None else None
            ),
            ignored_runtime_path_count=ignored_count,
        )

    @staticmethod
    def _load_universe(
        database: Path,
        market_date: date,
        errors: set[str],
    ) -> PilotUniverseSnapshot | None:
        try:
            universe = PilotRepository(database).get_universe_for_date(
                market_date
            )
        except Exception:
            errors.add("PILOT_UNIVERSE_INVALID")
            return None
        if universe is None:
            errors.add("PILOT_UNIVERSE_MISSING")
        return universe

    @staticmethod
    def _load_latest(
        repository: ReportRepository,
        errors: set[str],
    ) -> StoredReport | None:
        try:
            report_id = repository.latest_report_id()
            stored = repository.latest_report()
        except Exception:
            errors.add("REPORT_LATEST_POINTER_INVALID")
            return None
        if report_id is None or stored is None:
            errors.add("REPORT_LATEST_POINTER_INVALID")
            return None
        if stored.snapshot.report_id != report_id:
            errors.add("REPORT_LATEST_POINTER_INVALID")
        return stored

    @staticmethod
    def _load_artifact(
        stored: StoredReport | None,
        report_root: Path,
        errors: set[str],
    ) -> dict[str, object] | None:
        if stored is None:
            return None
        path = stored.artifact_path.resolve()
        root = report_root.resolve()
        if path == root or root not in path.parents:
            errors.add("REPORT_ARTIFACT_INVALID")
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            errors.add("REPORT_ARTIFACT_INVALID")
            return None
        if not isinstance(payload, dict):
            errors.add("REPORT_ARTIFACT_INVALID")
            return None
        snapshot = payload.get("snapshot")
        if not isinstance(snapshot, dict):
            errors.add("REPORT_ARTIFACT_INVALID")
            return payload
        expected = stored.snapshot.manifest_hash
        if (
            snapshot.get("manifest_hash") != expected
            or expected not in path.name
        ):
            errors.add("REPORT_ARTIFACT_INVALID")
        try:
            actual = compute_artifact_hash(payload)
        except ValueError:
            errors.add("REPORT_ARTIFACT_INVALID")
        else:
            if actual != expected:
                errors.add("REPORT_ARTIFACT_HASH_MISMATCH")
        return payload

    @classmethod
    def _validate_candidates(
        cls,
        artifact: Mapping[str, object],
        report_cutoff_at: datetime,
        report_known_at: datetime,
        errors: set[str],
    ) -> tuple[
        dict[str, str],
        dict[str, str],
        dict[str, int],
        int,
        int,
    ]:
        readiness = artifact.get("pool_readiness")
        pools = artifact.get("candidate_pools")
        versions = artifact.get("strategy_versions")
        sources = artifact.get("source_records")
        quality = artifact.get("quality_summary")
        if not isinstance(readiness, dict):
            readiness = {}
        if not isinstance(pools, dict):
            pools = {}
        if not isinstance(versions, dict):
            versions = {}
        if not isinstance(sources, list):
            sources = []
        template_versions = (
            quality.get("narrative_template_versions", {})
            if isinstance(quality, dict)
            else {}
        )
        if not isinstance(template_versions, dict):
            template_versions = {}
        source_by_id = {
            str(item["record_id"]): item
            for item in sources
            if isinstance(item, dict)
            and isinstance(item.get("record_id"), str)
        }
        resolved_ids: set[str] = set()
        pool_coverage: dict[str, str] = {}
        pool_statuses: dict[str, str] = {}
        candidate_counts: dict[str, int] = {}
        total_candidates = 0

        for strategy in StrategyType:
            strategy_name = strategy.value
            item = readiness.get(strategy_name)
            candidates = pools.get(strategy_name)
            if not isinstance(item, dict) or not isinstance(candidates, list):
                errors.add("POOL_ACCEPTANCE_PAYLOAD_INVALID")
                candidates = []
                item = {}
            coverage = item.get("coverage_ratio")
            status = item.get("status")
            pool_coverage[strategy_name] = str(coverage)
            pool_statuses[strategy_name] = str(status)
            candidate_counts[strategy_name] = len(candidates)
            total_candidates += len(candidates)
            if status == "BLOCKED" and candidates:
                errors.add("BLOCKED_POOL_HAS_CANDIDATES")
            template_version = template_versions.get(strategy_name)
            if not isinstance(template_version, str) or not template_version:
                errors.add("CANDIDATE_TEMPLATE_VERSION_INVALID")
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    errors.add("CANDIDATE_PAYLOAD_INVALID")
                    continue
                if candidate.get("strategy_version") != versions.get(
                    strategy_name
                ):
                    errors.add("CANDIDATE_STRATEGY_VERSION_INVALID")
                if not cls._aware_at_or_before(
                    candidate.get("data_cutoff_at"),
                    report_cutoff_at,
                ) or not cls._aware_at_or_before(
                    candidate.get("known_at"),
                    report_known_at,
                ):
                    errors.add("CANDIDATE_TIME_LINEAGE_INVALID")
                details = candidate.get("factor_details")
                if not isinstance(details, list) or not details:
                    errors.add("CANDIDATE_SOURCE_LINEAGE_INVALID")
                    continue
                for detail in details:
                    detail_ids = (
                        detail.get("source_record_ids")
                        if isinstance(detail, dict)
                        else None
                    )
                    if (
                        not isinstance(detail_ids, list)
                        or not detail_ids
                        or any(
                            not isinstance(source_id, str)
                            or source_id not in source_by_id
                            for source_id in detail_ids
                        )
                    ):
                        errors.add("CANDIDATE_SOURCE_LINEAGE_INVALID")
                        continue
                    if any(
                        not cls._source_visible(
                            source_by_id[source_id],
                            report_cutoff_at,
                            report_known_at,
                        )
                        for source_id in detail_ids
                    ):
                        errors.add("CANDIDATE_TIME_LINEAGE_INVALID")
                    resolved_ids.update(detail_ids)
        return (
            dict(sorted(pool_coverage.items())),
            dict(sorted(pool_statuses.items())),
            dict(sorted(candidate_counts.items())),
            total_candidates,
            len(resolved_ids),
        )

    def _validate_ignored_paths(
        self,
        data_dir: Path,
        errors: set[str],
    ) -> int:
        checker = self._ignore_checker or self._git_ignored
        paths = tuple(
            data_dir / relative
            for relative in self._RUNTIME_PATHS
        ) + (data_dir.parent / ".env",)
        ignored = 0
        for path in paths:
            try:
                is_ignored = checker(path)
            except Exception:
                is_ignored = False
            if is_ignored:
                ignored += 1
            else:
                errors.add("SENSITIVE_RUNTIME_PATH_NOT_IGNORED")
        return ignored

    @staticmethod
    def _git_ignored(path: Path) -> bool:
        repository_root = path.parent
        while repository_root.parent != repository_root:
            if (repository_root / ".git").exists():
                break
            repository_root = repository_root.parent
        if not (repository_root / ".git").exists():
            return False
        try:
            relative = path.resolve().relative_to(repository_root.resolve())
        except ValueError:
            return False
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repository_root),
                "check-ignore",
                "--quiet",
                "--",
                relative.as_posix(),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    @staticmethod
    def _universe_hash(universe: PilotUniverseSnapshot) -> str:
        identity = {
            "market_date": universe.market_date.isoformat(),
            "report_cutoff_at": universe.report_cutoff_at.isoformat(),
            "algorithm_version": universe.algorithm_version,
            "quotas": universe.quotas,
            "members": [
                member.model_dump(mode="json")
                for member in universe.members
            ],
            "input_hashes": universe.input_hashes,
        }
        return hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    @staticmethod
    def _id_hash(values: list[str]) -> str:
        return hashlib.sha256(
            json.dumps(values, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _count(
        payload: Mapping[str, object],
        key: str,
        errors: set[str],
    ) -> int:
        value = payload.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.add(f"{key.upper()}_INVALID")
            return 0
        return value

    @staticmethod
    def _count_mapping(
        value: object,
        error_code: str,
        errors: set[str],
    ) -> dict[str, int]:
        if not isinstance(value, dict) or any(
            not isinstance(key, str)
            or not key
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
            for key, count in value.items()
        ):
            errors.add(error_code)
            return {}
        return dict(sorted(value.items()))

    @staticmethod
    def _aware_datetime(value: object) -> bool:
        return PilotAcceptanceValidator._parse_aware_datetime(value) is not None

    @staticmethod
    def _parse_aware_datetime(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed

    @classmethod
    def _aware_at_or_before(
        cls,
        value: object,
        cutoff: datetime,
    ) -> bool:
        if not cls._aware_datetime(value):
            return False
        return datetime.fromisoformat(str(value)) <= cutoff

    @classmethod
    def _source_visible(
        cls,
        source: Mapping[str, object],
        cutoff: datetime,
        known_at: datetime,
    ) -> bool:
        return (
            cls._aware_at_or_before(source.get("published_at"), cutoff)
            and cls._aware_at_or_before(source.get("effective_at"), cutoff)
            and cls._aware_at_or_before(source.get("collected_at"), known_at)
            and cls._aware_at_or_before(source.get("valid_from"), known_at)
        )

    @classmethod
    def _datetime_equals(
        cls,
        value: object,
        expected: datetime,
    ) -> bool:
        if not cls._aware_datetime(value):
            return False
        return datetime.fromisoformat(str(value)) == expected

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return value if isinstance(value, str) and value else None


__all__ = [
    "PilotAcceptanceSummary",
    "PilotAcceptanceValidator",
]
