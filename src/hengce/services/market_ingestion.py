"""Sequential daily market-data ingestion for a single scheduler."""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from hengce.collectors.tushare import DailyFetchResult, TushareDailyCollector
from hengce.contracts.enums import RunStatus
from hengce.contracts.run import RunRecord
from hengce.raw_store.store import RawObjectStore
from hengce.state.repository import StateRepository
from hengce.warehouse.market import MarketWarehouse


class DailyCollector(Protocol):
    def fetch(self, trade_date: date) -> DailyFetchResult: ...


@dataclass(frozen=True)
class MarketIngestionResult:
    trade_date: str
    bar_count: int
    raw_content_hash: str
    parquet_path: str


@dataclass(frozen=True)
class StagedMarketArtifact:
    result: MarketIngestionResult
    raw_payload_path: str


class IngestionInProgressError(RuntimeError):
    pass


class MarketIngestionService:
    """Durably coordinates one collector and checkpoint publication per trade date."""

    def __init__(
        self,
        *,
        collector: TushareDailyCollector | DailyCollector,
        raw_store: RawObjectStore,
        warehouse: MarketWarehouse,
        state: StateRepository,
        clock: Callable[[], datetime] | None = None,
        owner_id_factory: Callable[[], str] | None = None,
        after_artifact_staged: Callable[[], None] | None = None,
    ) -> None:
        self.collector = collector
        self.raw_store = raw_store
        self.warehouse = warehouse
        self.state = state
        self.clock = clock or (lambda: datetime.now(UTC))
        self.owner_id_factory = owner_id_factory or (lambda: uuid4().hex)
        self.after_artifact_staged = after_artifact_staged

    def run(self, trade_date: date) -> MarketIngestionResult:
        started_at = self.clock()
        owner_id = self.owner_id_factory()
        run = RunRecord(
            run_id=f"market_daily:{trade_date.isoformat()}:{owner_id}",
            trade_date=trade_date,
            run_type="market_daily",
            started_at=started_at,
            run_status=RunStatus.RUNNING,
            stage_statuses={"checkpoint": "RUNNING"},
        )
        self.state.record_run(run)
        checkpoint_key = f"market_daily:{trade_date.isoformat()}"
        acquired = False
        terminal = False
        try:
            existing = self.state.get_checkpoint(checkpoint_key)
            if existing is not None:
                result = self._parse_checkpoint(existing, trade_date)
                self._finish_run(run, RunStatus.SUCCEEDED, {"checkpoint": "SUCCEEDED"})
                terminal = True
                return result

            lease = self.state.acquire_ingestion_lease(
                trade_date, owner_id=owner_id, now=self.clock(), lease_seconds=300
            )
            if not lease.acquired:
                self._finish_run(
                    run,
                    RunStatus.BLOCKED,
                    {"lease": "BLOCKED"},
                    error_code="MARKET_INGESTION_IN_PROGRESS",
                )
                terminal = True
                raise IngestionInProgressError("MARKET_INGESTION_IN_PROGRESS")
            acquired = True
            if lease.staged_result_json is not None:
                result = self._restore_staged(lease.staged_result_json, trade_date)
                self._publish_checkpoint(checkpoint_key, result)
                self.state.finish_ingestion_lease(
                    trade_date, owner_id=owner_id, lifecycle_state="SUCCEEDED"
                )
                acquired = False
                self._finish_run(
                    run,
                    RunStatus.SUCCEEDED,
                    {"lease": "SUCCEEDED", "reconcile": "SUCCEEDED", "checkpoint": "SUCCEEDED"},
                    retry_count=1,
                )
                terminal = True
                return result

            fetched = self.collector.fetch(trade_date)
            if not fetched.bars:
                raise ValueError("MARKET_DAILY_EMPTY")
            raw = self.raw_store.put(
                source_id="tushare",
                source_url="http://api.tushare.pro/",
                collected_at=fetched.collected_at,
                content_type=fetched.content_type,
                payload=fetched.raw_payload,
            )
            parquet_path = self.warehouse.write_bars(fetched.bars)
            result = MarketIngestionResult(
                trade_date=trade_date.isoformat(),
                bar_count=len(fetched.bars),
                raw_content_hash=raw.content_hash,
                parquet_path=str(parquet_path),
            )
            staged = StagedMarketArtifact(result=result, raw_payload_path=raw.payload_path)
            if not self.state.stage_ingestion_artifact(
                trade_date,
                owner_id=owner_id,
                staged_result_json=json.dumps(
                    asdict(staged), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            ):
                raise RuntimeError("MARKET_INGESTION_LEASE_LOST")
            if self.after_artifact_staged is not None:
                self.after_artifact_staged()
            self._publish_checkpoint(checkpoint_key, result)
            self.state.finish_ingestion_lease(
                trade_date, owner_id=owner_id, lifecycle_state="SUCCEEDED"
            )
            acquired = False
            self._finish_run(
                run,
                RunStatus.SUCCEEDED,
                {"collect": "SUCCEEDED", "artifacts": "SUCCEEDED", "checkpoint": "SUCCEEDED"},
            )
            terminal = True
            return result
        except Exception as error:
            if acquired:
                self.state.finish_ingestion_lease(
                    trade_date, owner_id=owner_id, lifecycle_state="FAILED"
                )
            if not terminal:
                self._finish_run(
                    run,
                    RunStatus.FAILED,
                    {"ingestion": "FAILED"},
                    error_code=self._error_code(error),
                )
            raise

    def _finish_run(
        self,
        run: RunRecord,
        status: RunStatus,
        stages: dict[str, str],
        *,
        retry_count: int = 0,
        error_code: str | None = None,
    ) -> None:
        self.state.record_run(
            run.model_copy(
                update={
                    "finished_at": self.clock(),
                    "run_status": status,
                    "stage_statuses": stages,
                    "retry_count": retry_count,
                    "error_code": error_code,
                    "error_summary": (
                        "market daily ingestion failed" if error_code is not None else None
                    ),
                }
            )
        )

    @staticmethod
    def _error_code(error: Exception) -> str:
        message = str(error)
        if re.fullmatch(r"[A-Z0-9_:-]+", message):
            return message.split(":", maxsplit=1)[0]
        return "MARKET_INGESTION_FAILED"

    def _publish_checkpoint(self, checkpoint_key: str, result: MarketIngestionResult) -> None:
        self.state.save_checkpoint(
            checkpoint_key,
            json.dumps(asdict(result), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )

    def _restore_staged(self, value: str, trade_date: date) -> MarketIngestionResult:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("MARKET_STAGED_ARTIFACT_INVALID") from error
        if (
            not isinstance(payload, dict)
            or set(payload) != {"result", "raw_payload_path"}
            or not isinstance(payload["result"], dict)
            or not isinstance(payload["raw_payload_path"], str)
        ):
            raise ValueError("MARKET_STAGED_ARTIFACT_INVALID")
        result = self._parse_checkpoint(
            json.dumps(payload["result"], sort_keys=True, separators=(",", ":")), trade_date
        )
        raw_path = Path(payload["raw_payload_path"])
        parquet_path = Path(result.parquet_path)
        if (
            not raw_path.is_file()
            or hashlib.sha256(raw_path.read_bytes()).hexdigest() != result.raw_content_hash
            or not parquet_path.is_file()
            or self.warehouse.count_bars(trade_date) < result.bar_count
        ):
            raise ValueError("MARKET_STAGED_ARTIFACT_INVALID")
        return result

    @staticmethod
    def _parse_checkpoint(value: str, trade_date: date) -> MarketIngestionResult:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("MARKET_CHECKPOINT_INVALID: invalid JSON") from error
        if not isinstance(payload, dict) or set(payload) != {
            "trade_date",
            "bar_count",
            "raw_content_hash",
            "parquet_path",
        }:
            raise ValueError("MARKET_CHECKPOINT_INVALID: required fields")
        if (
            not isinstance(payload["trade_date"], str)
            or not isinstance(payload["bar_count"], int)
            or isinstance(payload["bar_count"], bool)
            or not isinstance(payload["raw_content_hash"], str)
            or not isinstance(payload["parquet_path"], str)
        ):
            raise ValueError("MARKET_CHECKPOINT_INVALID: field types")
        if payload["trade_date"] != trade_date.isoformat():
            raise ValueError("MARKET_CHECKPOINT_INVALID: stale trade_date")
        if payload["bar_count"] <= 0:
            raise ValueError("MARKET_CHECKPOINT_INVALID: bar_count")
        if not re.fullmatch(r"[0-9a-f]{64}", payload["raw_content_hash"]):
            raise ValueError("MARKET_CHECKPOINT_INVALID: raw_content_hash")
        if not payload["parquet_path"]:
            raise ValueError("MARKET_CHECKPOINT_INVALID: parquet_path")
        return MarketIngestionResult(**payload)
