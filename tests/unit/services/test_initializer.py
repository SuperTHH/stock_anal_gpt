from datetime import UTC, date, datetime
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


def test_initializer_revalidates_dates_at_or_before_existing_checkpoint(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.save_checkpoint(HistoricalInitializer.checkpoint_key, "2026-07-23")
    ingestion = RecordingIngestion()
    initializer = HistoricalInitializer(ingestion=ingestion, state=state)

    result = initializer.run([date(2026, 7, 22), date(2026, 7, 23), date(2026, 7, 24)])

    assert ingestion.calls == [date(2026, 7, 22), date(2026, 7, 23), date(2026, 7, 24)]
    assert result.completed_dates == 3
    assert result.last_trade_date == "2026-07-24"


def test_initializer_fails_when_completed_date_artifact_revalidation_fails(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path)
    state.save_checkpoint(HistoricalInitializer.checkpoint_key, "2026-07-23")
    ingestion = RecordingIngestion(fail_on=date(2026, 7, 22))
    initializer = HistoricalInitializer(ingestion=ingestion, state=state)

    with pytest.raises(RuntimeError, match="temporary failure"):
        initializer.run([date(2026, 7, 22), date(2026, 7, 23), date(2026, 7, 24)])

    assert ingestion.calls == [date(2026, 7, 22)]
    assert state.get_checkpoint(HistoricalInitializer.checkpoint_key) == "2026-07-23"


def test_initializer_does_not_advance_checkpoint_after_failure(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    ingestion = RecordingIngestion(fail_on=date(2026, 7, 23))
    initializer = HistoricalInitializer(ingestion=ingestion, state=state)

    with pytest.raises(RuntimeError, match="temporary failure"):
        initializer.run([date(2026, 7, 22), date(2026, 7, 23), date(2026, 7, 24)])

    assert state.get_checkpoint(HistoricalInitializer.checkpoint_key) == "2026-07-22"
    assert ingestion.calls == [date(2026, 7, 22), date(2026, 7, 23)]


def test_initializer_revalidates_completed_dates_before_resuming(tmp_path: Path) -> None:
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
        date(2026, 7, 22),
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


def test_initializer_records_terminal_lifecycle_for_success_and_failure(tmp_path: Path) -> None:
    state = make_state(tmp_path)

    def clock() -> datetime:
        return datetime(2026, 7, 24, 21, 30, tzinfo=UTC)

    successful = HistoricalInitializer(
        ingestion=RecordingIngestion(), state=state, clock=clock, run_id_factory=lambda: "success"
    )
    failing = HistoricalInitializer(
        ingestion=RecordingIngestion(fail_on=date(2026, 7, 24)),
        state=state,
        clock=clock,
        run_id_factory=lambda: "failure",
    )

    successful.run([date(2026, 7, 23)])
    with pytest.raises(RuntimeError, match="temporary failure"):
        failing.run([date(2026, 7, 24)])

    runs = {record.run_id: record for record in state.list_runs(run_type="history_initialization")}
    assert runs["history_initialization:success"].run_status == "SUCCEEDED"
    assert runs["history_initialization:success"].finished_at == clock()
    assert runs["history_initialization:failure"].run_status == "FAILED"
    assert runs["history_initialization:failure"].error_code == "HISTORY_INITIALIZATION_FAILED"
    assert runs["history_initialization:failure"].error_summary == "history initialization failed"


def test_initializer_marks_lease_contention_as_blocked_without_error_details(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path)

    class BlockedIngestion:
        def run(self, trade_date: date) -> object:
            raise RuntimeError("MARKET_INGESTION_IN_PROGRESS")

    initializer = HistoricalInitializer(
        ingestion=BlockedIngestion(), state=state, run_id_factory=lambda: "blocked"
    )

    with pytest.raises(RuntimeError, match="MARKET_INGESTION_IN_PROGRESS"):
        initializer.run([date(2026, 7, 24)])

    record = state.list_runs(run_type="history_initialization")[0]
    assert record.run_status == "BLOCKED"
    assert record.error_code == "MARKET_INGESTION_IN_PROGRESS"
    assert record.error_summary == "history initialization failed"
