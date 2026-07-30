from datetime import UTC, date, datetime
from pathlib import Path

from hengce.services.pilot_reconstruction import (
    HistoricalPilotRunner,
    PilotStageContext,
)
from hengce.state.repository import StateRepository


def test_new_runner_instance_resumes_persisted_stage_chain(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    state = StateRepository(data_dir / "state" / "hengce.sqlite3")
    state.path.parent.mkdir(parents=True)
    state.migrate()
    calls: list[str] = []

    def handler(context: PilotStageContext) -> dict[str, object]:
        calls.append(context.stage_name)
        return {
            "universe_id": "pilot-fixed-30",
            "policy_guarded": (
                context.stage_name == "05_acquire_public_documents"
            ),
        }

    handlers = {
        stage: handler
        for stage in HistoricalPilotRunner.STAGES
        if stage != "01_backup_and_migrate"
    }
    arguments = {
        "market_date": date(2026, 7, 22),
        "report_cutoff_at": datetime(
            2026,
            7,
            22,
            13,
            30,
            tzinfo=UTC,
        ),
        "known_at": datetime(2026, 7, 30, tzinfo=UTC),
        "acquisition_mode": "approved-public",
    }
    first = HistoricalPilotRunner(
        state=state,
        data_dir=data_dir,
        stage_handlers=handlers,
        clock=lambda: datetime(2026, 7, 30, tzinfo=UTC),
    ).run(**arguments)
    calls_after_first = tuple(calls)
    second = HistoricalPilotRunner(
        state=StateRepository(state.path),
        data_dir=data_dir,
        stage_handlers=handlers,
        clock=lambda: datetime(2026, 7, 30, tzinfo=UTC),
    ).run(**arguments)

    assert first.failed_stage is None
    assert second.failed_stage is None
    assert all(status == "REUSED" for status in second.stage_statuses.values())
    assert tuple(calls) == calls_after_first
    assert second.universe_id == first.universe_id == "pilot-fixed-30"
