import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query

from hengce.contracts.enums import StrategyType
from hengce.reports.integrity import compute_artifact_hash
from hengce.state.report_repository import ReportRepository, StoredReport


class PublishedReportReader:
    def __init__(self, repository: ReportRepository, report_root: Path) -> None:
        self.repository = repository
        self.report_root = report_root.resolve()

    def latest(self) -> dict[str, object]:
        stored = self.repository.latest_report()
        if stored is None:
            raise HTTPException(status_code=404, detail="NO_PUBLISHED_REPORT")
        return self._load(stored)

    def by_id(self, report_id: str) -> dict[str, object]:
        stored = self.repository.get_report(report_id)
        if stored is None:
            raise HTTPException(status_code=404, detail="REPORT_NOT_FOUND")
        return self._load(stored)

    def _load(self, stored: StoredReport) -> dict[str, object]:
        path = stored.artifact_path.resolve()
        if path == self.report_root or self.report_root not in path.parents:
            raise HTTPException(status_code=500, detail="REPORT_ARTIFACT_PATH_INVALID")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise HTTPException(status_code=500, detail="REPORT_ARTIFACT_INVALID") from None
        snapshot = payload.get("snapshot")
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("report_id") != stored.snapshot.report_id
            or snapshot.get("manifest_hash") != stored.snapshot.manifest_hash
            or stored.snapshot.manifest_hash not in path.name
            or compute_artifact_hash(payload) != stored.snapshot.manifest_hash
        ):
            raise HTTPException(status_code=500, detail="REPORT_ARTIFACT_INVALID")
        return payload


def create_app(repository: ReportRepository, report_root: Path) -> FastAPI:
    app = FastAPI(
        title="衡策本地研究 API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    reader = PublishedReportReader(repository, report_root)

    @app.get("/api/reports/latest")
    def latest_report() -> dict[str, object]:
        return reader.latest()

    @app.get("/api/reports/{report_id}")
    def report_by_id(report_id: str) -> dict[str, object]:
        return reader.by_id(report_id)

    @app.get("/api/strategies/{strategy_type}")
    def strategy_candidates(
        strategy_type: StrategyType,
        report_id: str = Query(min_length=1),
    ) -> dict[str, object]:
        report = reader.by_id(report_id)
        pools = report.get("candidate_pools", {})
        candidates = pools.get(strategy_type.value, []) if isinstance(pools, dict) else []
        return {
            "report_id": report_id,
            "strategy_type": strategy_type.value,
            "strategy_version": report["strategy_versions"][strategy_type.value],
            "candidates": candidates,
            "readiness": (
                report.get("pool_readiness", {}).get(strategy_type.value)
                if isinstance(report.get("pool_readiness"), dict)
                else None
            ),
        }

    @app.get("/api/securities/{ts_code}")
    def security_research(
        ts_code: str,
        report_id: str = Query(min_length=1),
    ) -> dict[str, object]:
        report = reader.by_id(report_id)
        memberships: list[str] = []
        candidates: list[dict[str, object]] = []
        pools = report.get("candidate_pools", {})
        if isinstance(pools, dict):
            for strategy in StrategyType:
                values = pools.get(strategy.value, [])
                if not isinstance(values, list):
                    continue
                matches = [
                    value
                    for value in values
                    if isinstance(value, dict) and value.get("ts_code") == ts_code
                ]
                if matches:
                    memberships.append(strategy.value)
                    candidates.extend(matches)
        if not candidates:
            raise HTTPException(status_code=404, detail="SECURITY_NOT_IN_REPORT")
        return {
            "report_id": report_id,
            "ts_code": ts_code,
            "strategy_memberships": memberships,
            "candidate_analyses": candidates,
        }

    @app.get("/api/quality")
    def quality(report_id: str = Query(min_length=1)) -> dict[str, object]:
        report = reader.by_id(report_id)
        snapshot = report.get("snapshot", {})
        summary = report.get("quality_summary", {})
        return {
            "report_id": report_id,
            "data_domain_statuses": report.get("data_domain_statuses", {}),
            "source_records": report.get("source_records", []),
            "known_at": (
                snapshot.get("known_at")
                if isinstance(snapshot, dict)
                else None
            ),
            "manifest_status_distribution": (
                summary.get("manifest_status_distribution", {})
                if isinstance(summary, dict)
                else {}
            ),
            "xbrl_used_count": (
                summary.get("xbrl_used_count", 0)
                if isinstance(summary, dict)
                else 0
            ),
            "pdf_used_count": (
                summary.get("pdf_used_count", 0)
                if isinstance(summary, dict)
                else 0
            ),
            "pool_readiness": report.get("pool_readiness", {}),
        }

    @app.get("/api/events")
    def official_events(report_id: str = Query(min_length=1)) -> dict[str, object]:
        report = reader.by_id(report_id)
        return {
            "report_id": report_id,
            "events": report.get("official_events", []),
        }

    return app
