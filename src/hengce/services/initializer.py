"""Resumable orchestration for historical daily-market ingestion."""

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from hengce.state.repository import StateRepository


class DailyIngestion(Protocol):
    def run(self, trade_date: date) -> object: ...


@dataclass(frozen=True)
class InitializationResult:
    completed_dates: int
    last_trade_date: str | None


class HistoricalInitializer:
    checkpoint_key = "history:last_completed_trade_date"

    def __init__(self, *, ingestion: DailyIngestion, state: StateRepository) -> None:
        self.ingestion = ingestion
        self.state = state

    def run(self, trade_dates: list[date]) -> InitializationResult:
        ordered = sorted(set(trade_dates))
        last_completed = self._load_checkpoint()
        pending = [item for item in ordered if last_completed is None or item > last_completed]
        for trade_date in pending:
            self.ingestion.run(trade_date)
            self.state.save_checkpoint(self.checkpoint_key, trade_date.isoformat())

        final = self._load_checkpoint()
        completed = sum(1 for item in ordered if final is not None and item <= final)
        return InitializationResult(
            completed_dates=completed,
            last_trade_date=final.isoformat() if final is not None else None,
        )

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
