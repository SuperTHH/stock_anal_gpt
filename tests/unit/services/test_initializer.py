from datetime import date
from pathlib import Path

import pytest

from hengce.services.initializer import HistoricalInitializer
from hengce.services.market_ingestion import MarketIngestionResult
from hengce.state.repository import StateRepository


class RecordingIngestion:
    def __init__(self, fail_on: date | None = None) -> None:
        self.calls: list[date] = []
        self.fail_on = fail_on
        self.fail_once = True

    def run(self, trade_date: date) -> MarketIngestionResult:
        self.calls.append(trade_date)
        if trade_date == self.fail_on and self.fail_once:
            self.fail_once = False
            raise RuntimeError("temporary failure")
        return MarketIngestionResult(
            trade_date=trade_date.isoformat(),
            bar_count=1,
            raw_content_hash="c" * 64,
            parquet_path=f"market_bars/trade_date={trade_date.isoformat()}/part.parquet",
        )


def make_state(tmp_path: Path) -> StateRepository:
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()
    return state


def test_initializer_sorts_and_deduplicates_dates(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    ingestion = RecordingIngestion()
    initializer = HistoricalInitializer(ingestion=ingestion, state=state)

    result = initializer.run([date(2026, 7, 24), date(2026, 7, 22), date(2026, 7, 22)])

    assert ingestion.calls == [date(2026, 7, 22), date(2026, 7, 24)]
    assert result.completed_dates == 2
    assert result.last_trade_date == "2026-07-24"


def test_initializer_handles_empty_input(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    initializer = HistoricalInitializer(ingestion=RecordingIngestion(), state=state)

    result = initializer.run([])

    assert result.completed_dates == 0
    assert result.last_trade_date is None


def test_initializer_skips_dates_at_or_before_existing_checkpoint(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.save_checkpoint(HistoricalInitializer.checkpoint_key, "2026-07-23")
    ingestion = RecordingIngestion()
    initializer = HistoricalInitializer(ingestion=ingestion, state=state)

    result = initializer.run([date(2026, 7, 22), date(2026, 7, 23), date(2026, 7, 24)])

    assert ingestion.calls == [date(2026, 7, 24)]
    assert result.completed_dates == 3
    assert result.last_trade_date == "2026-07-24"


def test_initializer_does_not_advance_checkpoint_after_failure(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    ingestion = RecordingIngestion(fail_on=date(2026, 7, 23))
    initializer = HistoricalInitializer(ingestion=ingestion, state=state)

    with pytest.raises(RuntimeError, match="temporary failure"):
        initializer.run([date(2026, 7, 22), date(2026, 7, 23), date(2026, 7, 24)])

    assert state.get_checkpoint(HistoricalInitializer.checkpoint_key) == "2026-07-22"
    assert ingestion.calls == [date(2026, 7, 22), date(2026, 7, 23)]


def test_initializer_resumes_after_last_completed_date(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    ingestion = RecordingIngestion(fail_on=date(2026, 7, 23))
    initializer = HistoricalInitializer(ingestion=ingestion, state=state)
    dates = [date(2026, 7, 22), date(2026, 7, 23), date(2026, 7, 24)]

    with pytest.raises(RuntimeError, match="temporary failure"):
        initializer.run(dates)
    result = initializer.run(dates)

    assert ingestion.calls == [
        date(2026, 7, 22),
        date(2026, 7, 23),
        date(2026, 7, 23),
        date(2026, 7, 24),
    ]
    assert result.completed_dates == 3
    assert result.last_trade_date == "2026-07-24"


@pytest.mark.parametrize("checkpoint", ["2026-7-22", "not-a-date", "2026-07-22T00:00:00"])
def test_initializer_rejects_malformed_checkpoint(tmp_path: Path, checkpoint: str) -> None:
    state = make_state(tmp_path)
    state.save_checkpoint(HistoricalInitializer.checkpoint_key, checkpoint)
    initializer = HistoricalInitializer(ingestion=RecordingIngestion(), state=state)

    with pytest.raises(ValueError, match="HISTORY_CHECKPOINT_INVALID"):
        initializer.run([date(2026, 7, 23)])
