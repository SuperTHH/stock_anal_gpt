import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request

from hengce.contracts.enums import EvidenceTaskStatus, StrategyType
from hengce.contracts.evidence import EvidenceReviewRequest
from hengce.reports.integrity import compute_artifact_hash
from hengce.services.full_market_evidence import FullMarketEvidenceStatusService
from hengce.services.full_market_screen import FullMarketScreenService
from hengce.state.evidence_repository import FullMarketEvidenceRepository
from hengce.state.report_repository import ReportRepository, StoredReport
from hengce.state.repository import StateRepository
from hengce.warehouse.dividends import ImplementedDividendWarehouse
from hengce.warehouse.market import MarketWarehouse
from hengce.warehouse.market_research import FullMarketResearchWarehouse


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


def create_app(
    repository: ReportRepository,
    report_root: Path,
    normalized_root: Path | None = None,
) -> FastAPI:
    app = FastAPI(
        title="衡策本地研究 API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    reader = PublishedReportReader(repository, report_root)
    market_screen = (
        FullMarketScreenService(
            state=StateRepository(repository.path),
            market_warehouse=MarketWarehouse(normalized_root),
            dividend_warehouse=ImplementedDividendWarehouse(normalized_root),
        )
        if normalized_root is not None else None
    )
    research_warehouse = (
        FullMarketResearchWarehouse(normalized_root)
        if normalized_root is not None
        else None
    )
    evidence_repository = FullMarketEvidenceRepository(repository.path)
    evidence_status = FullMarketEvidenceStatusService(evidence_repository)

    @app.get("/api/market/securities")
    def full_market_securities(
        market_date: date | None = None,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
        search: str | None = Query(default=None, max_length=80),
        board: Literal["MAIN_SH", "MAIN_SZ", "CHINEXT", "STAR"] | None = None,
        minimum_dividend_yield: Decimal = Decimal(0),
        dividend_data: Literal["ALL", "AVAILABLE", "MISSING"] = "ALL",
        sort_by: Literal["DIVIDEND_YIELD", "AMOUNT", "TS_CODE"] = "DIVIDEND_YIELD",
        descending: bool = True,
    ) -> dict[str, object]:
        if minimum_dividend_yield < 0 or minimum_dividend_yield > 1:
            raise HTTPException(status_code=422, detail="DIVIDEND_YIELD_FILTER_INVALID")
        resolved_date = (
            market_date
            if market_date is not None
            else market_screen.market_warehouse.latest_trade_date()
            if market_screen is not None
            else None
        )
        if (
            market_screen is None
            or resolved_date is None
            or market_screen.market_warehouse.count_bars(resolved_date) == 0
        ):
            raise HTTPException(status_code=404, detail="MARKET_SNAPSHOT_NOT_FOUND")
        return market_screen.query(
            market_date=resolved_date,
            page=page,
            page_size=page_size,
            search=search,
            board=board,
            minimum_dividend_yield=minimum_dividend_yield,
            dividend_data=dividend_data,
            sort_by=sort_by,
            descending=descending,
        )

    @app.get("/api/reports/latest")
    def latest_report() -> dict[str, object]:
        return reader.latest()

    @app.get("/api/market/research")
    def full_market_research(market_date: date | None = None) -> dict[str, object]:
        snapshot = (
            research_warehouse.read(market_date)
            if research_warehouse is not None and market_date is not None
            else research_warehouse.latest()
            if research_warehouse is not None
            else None
        )
        if snapshot is None:
            raise HTTPException(status_code=404, detail="FULL_MARKET_RESEARCH_NOT_FOUND")
        return snapshot.model_dump(mode="json")

    @app.get("/api/market/evidence-status")
    def full_market_evidence_status(
        market_date: date | None = None,
        run_id: str | None = Query(default=None, min_length=1),
        status: Annotated[list[EvidenceTaskStatus] | None, Query()] = None,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, object]:
        try:
            payload = evidence_status.summary(
                market_date=market_date,
                run_id=run_id,
                statuses=tuple(status or ()),
                page=page,
                page_size=page_size,
            )
        except Exception as error:
            if "no such table" in str(error):
                raise HTTPException(status_code=404, detail="EVIDENCE_PLAN_NOT_FOUND") from None
            raise
        if not payload["runs"]:
            raise HTTPException(status_code=404, detail="EVIDENCE_PLAN_NOT_FOUND")
        return payload

    @app.get("/api/market/evidence-reviews/{task_id}")
    def evidence_review_detail(task_id: str) -> dict[str, object]:
        task = evidence_repository.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="EVIDENCE_TASK_NOT_FOUND")
        return task.model_dump(mode="json")

    @app.post("/api/market/evidence-reviews/{task_id}/decision")
    def evidence_review_decision(
        task_id: str,
        payload: EvidenceReviewRequest,
        request: Request,
    ) -> dict[str, object]:
        client_host = request.client.host if request.client is not None else ""
        origin = request.headers.get("origin")
        content_type = request.headers.get("content-type", "").partition(";")[0]
        if client_host not in {"127.0.0.1", "::1", "testclient"}:
            raise HTTPException(status_code=403, detail="LOCAL_REVIEW_ONLY")
        if origin not in {"http://127.0.0.1:4173", "http://localhost:4173"}:
            raise HTTPException(status_code=403, detail="REVIEW_ORIGIN_INVALID")
        if content_type != "application/json":
            raise HTTPException(status_code=415, detail="REVIEW_CONTENT_TYPE_INVALID")
        try:
            updated = evidence_repository.review_task(
                task_id,
                expected_version=payload.expected_version,
                decision=payload.decision,
                reviewed_values=payload.reviewed_values,
                note=payload.note,
                reviewed_at=datetime.now(ZoneInfo("Asia/Shanghai")),
            )
        except ValueError as error:
            code = str(error)
            if code == "EVIDENCE_TASK_NOT_FOUND":
                raise HTTPException(status_code=404, detail=code) from None
            if code == "EVIDENCE_TASK_VERSION_CONFLICT":
                raise HTTPException(status_code=409, detail=code) from None
            raise HTTPException(status_code=422, detail=code) from None
        return updated.model_dump(mode="json")

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
