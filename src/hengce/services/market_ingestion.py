"""Sequential daily market-data ingestion for a single scheduler."""

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Event, Thread
from typing import Protocol
from uuid import uuid4

from hengce.collectors.tushare import DailyFetchResult, TushareDailyCollector
from hengce.contracts.enums import RunStatus
from hengce.contracts.market import MarketBar
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
    parquet_content_hash: str | None = None


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
        after_parquet_published: Callable[[], None] | None = None,
        after_checkpoint_published: Callable[[], None] | None = None,
        after_ingestion_finalized: Callable[[], None] | None = None,
        after_lease_acquired: Callable[[], None] | None = None,
        before_nonlease_terminal_recorded: Callable[[], None] | None = None,
        lease_seconds: float = 300,
        lease_renewal_interval_seconds: float | None = None,
    ) -> None:
        self.collector = collector
        self.raw_store = raw_store
        self.warehouse = warehouse
        self.state = state
        self.clock = clock or (lambda: datetime.now(UTC))
        self.owner_id_factory = owner_id_factory or (lambda: uuid4().hex)
        self.after_artifact_staged = after_artifact_staged
        self.after_parquet_published = after_parquet_published
        self.after_checkpoint_published = after_checkpoint_published
        self.after_ingestion_finalized = after_ingestion_finalized
        self.after_lease_acquired = after_lease_acquired
        self.before_nonlease_terminal_recorded = before_nonlease_terminal_recorded
        if lease_seconds <= 0:
            raise ValueError("INGESTION_LEASE_INVALID")
        self.lease_seconds = lease_seconds
        self.lease_renewal_interval_seconds = (
            lease_renewal_interval_seconds or lease_seconds / 2
        )
        if self.lease_renewal_interval_seconds <= 0:
            raise ValueError("INGESTION_LEASE_RENEWAL_INTERVAL_INVALID")

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
        checkpoint_key = f"market_daily:{trade_date.isoformat()}"
        acquired = False
        terminal = False
        heartbeat_stop: Event | None = None
        heartbeat: Thread | None = None
        try:
            existing = self.state.get_checkpoint(checkpoint_key)
            if existing is not None:
                result = self._parse_checkpoint(existing, trade_date)
                result = self._validate_checkpoint_artifacts(result, trade_date)
                checkpoint_value = self._result_json(result)
                self.state.reconcile_published_ingestion(
                    trade_date,
                    checkpoint_value=checkpoint_value,
                    now=self.clock(),
                )
                if result.parquet_content_hash is not None:
                    self._publish_checkpoint(checkpoint_key, result)
                self._before_nonlease_terminal()
                self._finish_run(run, RunStatus.SUCCEEDED, {"checkpoint": "SUCCEEDED"})
                terminal = True
                return result

            try:
                security_universe = self.state.get_security_master_universe()
            except Exception as error:
                if not self._is_security_master_error(error):
                    raise
                self._before_nonlease_terminal()
                self._finish_run(
                    run,
                    RunStatus.BLOCKED,
                    {"security_master": "BLOCKED"},
                    error_code=self._error_code(error),
                )
                terminal = True
                raise
            approved_codes = {
                security.ts_code for security in security_universe.securities
            }

            lease = self.state.acquire_ingestion_lease(
                trade_date,
                owner_id=owner_id,
                running_run=run,
                now=self.clock(),
                lease_seconds=self.lease_seconds,
            )
            if not lease.acquired:
                self._before_nonlease_terminal()
                self._finish_run(
                    run,
                    RunStatus.BLOCKED,
                    {"lease": "BLOCKED"},
                    error_code="MARKET_INGESTION_IN_PROGRESS",
                )
                terminal = True
                raise IngestionInProgressError("MARKET_INGESTION_IN_PROGRESS")
            acquired = True
            if self.after_lease_acquired is not None:
                self.after_lease_acquired()
            heartbeat_stop = Event()
            heartbeat = Thread(
                target=self._renew_lease_until_stopped,
                args=(trade_date, owner_id, heartbeat_stop),
                daemon=True,
            )
            heartbeat.start()
            if lease.staged_result_json is not None:
                try:
                    staged = self._restore_staged(lease.staged_result_json, trade_date)
                    result = staged.result
                except ValueError as error:
                    is_invalid_stage = str(error) == "MARKET_STAGED_ARTIFACT_INVALID"
                    discarded = is_invalid_stage and self.state.discard_staged_ingestion_artifact(
                        trade_date, owner_id=owner_id
                    )
                    if not discarded:
                        raise
                else:
                    normalized_stage = self._staged_json(staged)
                    if not self.state.stage_ingestion_artifact(
                        trade_date,
                        owner_id=owner_id,
                        staged_result_json=normalized_stage,
                    ):
                        raise RuntimeError("MARKET_INGESTION_LEASE_LOST")
                    terminal_run = self._terminal_run(
                        run,
                        RunStatus.SUCCEEDED,
                        {"lease": "SUCCEEDED", "reconcile": "SUCCEEDED", "checkpoint": "SUCCEEDED"},
                        retry_count=1,
                    )
                    if not self.state.publish_and_finalize_ingestion(
                        checkpoint_key,
                        self._result_json(result),
                        trade_date,
                        owner_id=owner_id,
                        terminal_run=terminal_run,
                    ):
                        raise RuntimeError("MARKET_INGESTION_LEASE_LOST")
                    acquired = False
                    terminal = True
                    if self.after_checkpoint_published is not None:
                        self.after_checkpoint_published()
                    if self.after_ingestion_finalized is not None:
                        self.after_ingestion_finalized()
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
            self._validate_bars_in_scope(fetched.bars, approved_codes)
            parquet_path, parquet_content_hash = self.warehouse.expected_artifact(fetched.bars)
            result = MarketIngestionResult(
                trade_date=trade_date.isoformat(),
                bar_count=len(fetched.bars),
                raw_content_hash=raw.content_hash,
                parquet_path=str(parquet_path),
                parquet_content_hash=parquet_content_hash,
            )
            staged = StagedMarketArtifact(result=result, raw_payload_path=raw.payload_path)
            if not self.state.stage_ingestion_artifact(
                trade_date,
                owner_id=owner_id,
                staged_result_json=self._staged_json(staged),
            ):
                raise RuntimeError("MARKET_INGESTION_LEASE_LOST")
            published_path = self.warehouse.write_bars(fetched.bars)
            if published_path != parquet_path:
                raise RuntimeError("MARKET_PARQUET_IDENTITY_CHANGED")
            if self.after_parquet_published is not None:
                self.after_parquet_published()
            self.warehouse.validate_artifact(
                parquet_path, trade_date, len(fetched.bars), parquet_content_hash
            )
            if self.after_artifact_staged is not None:
                self.after_artifact_staged()
            terminal_run = self._terminal_run(
                run,
                RunStatus.SUCCEEDED,
                {"collect": "SUCCEEDED", "artifacts": "SUCCEEDED", "checkpoint": "SUCCEEDED"},
            )
            if not self.state.publish_and_finalize_ingestion(
                checkpoint_key,
                self._result_json(result),
                trade_date,
                owner_id=owner_id,
                terminal_run=terminal_run,
            ):
                raise RuntimeError("MARKET_INGESTION_LEASE_LOST")
            acquired = False
            terminal = True
            if self.after_checkpoint_published is not None:
                self.after_checkpoint_published()
            if self.after_ingestion_finalized is not None:
                self.after_ingestion_finalized()
            return result
        except Exception as error:
            owned_finalize_attempted = acquired
            if acquired:
                terminal_run = self._terminal_run(
                    run,
                    RunStatus.FAILED,
                    {"ingestion": "FAILED"},
                    error_code=self._error_code(error),
                )
                finalized = self.state.finalize_ingestion_run(
                    trade_date,
                    owner_id=owner_id,
                    lifecycle_state="FAILED",
                    terminal_run=terminal_run,
                )
                if finalized:
                    acquired = False
                    terminal = True
                    if self.after_ingestion_finalized is not None:
                        self.after_ingestion_finalized()
            if not terminal and not owned_finalize_attempted:
                self._before_nonlease_terminal()
                self._finish_run(
                    run,
                    RunStatus.FAILED,
                    {"ingestion": "FAILED"},
                    error_code=self._error_code(error),
                )
            raise
        finally:
            if heartbeat_stop is not None:
                heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.join()

    def _renew_lease_until_stopped(
        self, trade_date: date, owner_id: str, stop: Event
    ) -> None:
        while not stop.wait(self.lease_renewal_interval_seconds):
            if not self.state.renew_ingestion_lease(
                trade_date,
                owner_id=owner_id,
                now=self.clock(),
                lease_seconds=self.lease_seconds,
            ):
                return

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
            self._terminal_run(
                run,
                status,
                stages,
                retry_count=retry_count,
                error_code=error_code,
            )
        )

    def _terminal_run(
        self,
        run: RunRecord,
        status: RunStatus,
        stages: dict[str, str],
        *,
        retry_count: int = 0,
        error_code: str | None = None,
    ) -> RunRecord:
        return run.model_copy(
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

    @staticmethod
    def _error_code(error: Exception) -> str:
        message = str(error)
        if re.fullmatch(r"[A-Z0-9_:-]+", message):
            return message.split(":", maxsplit=1)[0]
        return "MARKET_INGESTION_FAILED"

    @staticmethod
    def _is_security_master_error(error: Exception) -> bool:
        return MarketIngestionService._error_code(error).startswith("SECURITY_MASTER_")

    def _publish_checkpoint(self, checkpoint_key: str, result: MarketIngestionResult) -> None:
        self.state.save_checkpoint(checkpoint_key, self._result_json(result))

    @staticmethod
    def _result_json(result: MarketIngestionResult) -> str:
        return json.dumps(
            asdict(result), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    @staticmethod
    def _staged_json(staged: StagedMarketArtifact) -> str:
        return json.dumps(
            asdict(staged), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    def _before_nonlease_terminal(self) -> None:
        if self.before_nonlease_terminal_recorded is not None:
            self.before_nonlease_terminal_recorded()

    @staticmethod
    def _validate_bars_in_scope(bars: list[MarketBar], approved_codes: set[str]) -> None:
        codes = [bar.ts_code for bar in bars]
        if len(codes) != len(set(codes)):
            raise ValueError("MARKET_DAILY_DUPLICATE_TS_CODE")
        if any(code not in approved_codes for code in codes):
            raise ValueError("MARKET_DAILY_OUT_OF_SCOPE_TS_CODE")

    def _restore_staged(self, value: str, trade_date: date) -> StagedMarketArtifact:
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
        try:
            self.raw_store.validate_content_hash(result.raw_content_hash)
            parquet_content_hash = self.warehouse.validate_artifact(
                Path(result.parquet_path),
                trade_date,
                result.bar_count,
                result.parquet_content_hash,
            )
        except ValueError as error:
            raise ValueError("MARKET_STAGED_ARTIFACT_INVALID") from error
        return StagedMarketArtifact(
            result=replace(result, parquet_content_hash=parquet_content_hash),
            raw_payload_path=payload["raw_payload_path"],
        )

    def _validate_checkpoint_artifacts(
        self, result: MarketIngestionResult, trade_date: date
    ) -> MarketIngestionResult:
        try:
            self.raw_store.validate_content_hash(result.raw_content_hash)
            parquet_content_hash = self.warehouse.validate_artifact(
                Path(result.parquet_path),
                trade_date,
                result.bar_count,
                result.parquet_content_hash,
            )
        except ValueError as error:
            raise ValueError("MARKET_CHECKPOINT_ARTIFACT_INVALID") from error
        return replace(result, parquet_content_hash=parquet_content_hash)

    @staticmethod
    def _parse_checkpoint(value: str, trade_date: date) -> MarketIngestionResult:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("MARKET_CHECKPOINT_INVALID: invalid JSON") from error
        required_fields = {"trade_date", "bar_count", "raw_content_hash", "parquet_path"}
        optional_fields = required_fields | {"parquet_content_hash"}
        if not isinstance(payload, dict) or (
            set(payload) != required_fields and set(payload) != optional_fields
        ):
            raise ValueError("MARKET_CHECKPOINT_INVALID: required fields")
        if (
            not isinstance(payload["trade_date"], str)
            or not isinstance(payload["bar_count"], int)
            or isinstance(payload["bar_count"], bool)
            or not isinstance(payload["raw_content_hash"], str)
            or not isinstance(payload["parquet_path"], str)
            or "parquet_content_hash" in payload
            and not isinstance(payload["parquet_content_hash"], str)
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
        content_hash = payload.get("parquet_content_hash")
        if content_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", content_hash):
            raise ValueError("MARKET_CHECKPOINT_INVALID: parquet_content_hash")
        return MarketIngestionResult(**payload)
