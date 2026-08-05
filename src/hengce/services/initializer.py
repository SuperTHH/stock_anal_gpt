"""Resumable orchestration for historical daily-market ingestion."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol
from uuid import uuid4

from hengce.contracts.enums import RunStatus
from hengce.contracts.run import RunRecord
from hengce.state.repository import StateRepository


class DailyIngestion(Protocol):
    def run(self, trade_date: date) -> object: ...


@dataclass(frozen=True)
class InitializationResult:
    completed_dates: int
    last_trade_date: str | None


class HistoricalInitializer:
    checkpoint_key = "history:last_completed_trade_date"

    def __init__(
        self,
        *,
        ingestion: DailyIngestion,
        state: StateRepository,
        clock: Callable[[], datetime] | None = None,
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.ingestion = ingestion
        self.state = state
        self.clock = clock or (lambda: datetime.now(UTC))
        self.run_id_factory = run_id_factory or (lambda: uuid4().hex)

    def run(self, trade_dates: list[date]) -> InitializationResult:
        ordered = sorted(set(trade_dates))
        run = RunRecord(
            run_id=f"history_initialization:{self.run_id_factory()}",
            trade_date=ordered[-1] if ordered else self.clock().date(),
            run_type="history_initialization",
            started_at=self.clock(),
            run_status=RunStatus.RUNNING,
            stage_statuses={"checkpoint": "RUNNING"},
        )
        self.state.record_run(run)
        try:
            last_completed = self._load_checkpoint()
            for trade_date in ordered:
                self.ingestion.run(trade_date)
                if last_completed is None or trade_date > last_completed:
                    self.state.save_checkpoint(self.checkpoint_key, trade_date.isoformat())

            final = self._load_checkpoint()
            completed = sum(1 for item in ordered if final is not None and item <= final)
            result = InitializationResult(
                completed_dates=completed,
                last_trade_date=final.isoformat() if final is not None else None,
            )
            self._finish_run(
                run,
                RunStatus.SUCCEEDED,
                {"checkpoint": "SUCCEEDED", "ingestion": "SUCCEEDED"},
                retry_count=1 if last_completed is not None else 0,
            )
            return result
        except Exception as error:
            code = self._error_code(error)
            self._finish_run(
                run,
                RunStatus.BLOCKED if code == "MARKET_INGESTION_IN_PROGRESS" else RunStatus.FAILED,
                {"ingestion": "FAILED"},
                error_code=code,
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
                        "history initialization failed" if error_code is not None else None
                    ),
                }
            )
        )

    @staticmethod
    def _error_code(error: Exception) -> str:
        message = str(error)
        if re.fullmatch(r"[A-Z0-9_:-]+", message):
            return message.split(":", maxsplit=1)[0]
        return "HISTORY_INITIALIZATION_FAILED"

    def _load_checkpoint(self) -> date | None:
        value = self.state.get_checkpoint(self.checkpoint_key)
        if value is None:
            return None
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("HISTORY_CHECKPOINT_INVALID: expected ISO trade date") from error
        if parsed.isoformat() != value:
            raise ValueError("HISTORY_CHECKPOINT_INVALID: expected ISO trade date")
        return parsed
