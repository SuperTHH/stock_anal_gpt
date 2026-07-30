from datetime import UTC, date, datetime
from pathlib import Path

from hengce.services.pilot_reconstruction import (
    HistoricalPilotRunner,
    PilotStageContext,
)
from hengce.state.repository import StateRepository

NOW = datetime(2026, 7, 30, 4, tzinfo=UTC)
MARKET_DATE = date(2026, 7, 22)
CUTOFF = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)


def built_runner(
    tmp_path: Path,
    *,
    fail_once_at: str | None = None,
) -> tuple[HistoricalPilotRunner, list[str], dict[str, int]]:
    data_dir = tmp_path / "data"
    state = StateRepository(data_dir / "state" / "hengce.sqlite3")
    state.path.parent.mkdir(parents=True)
    state.migrate()
    calls: list[str] = []
    network_calls = {"count": 0}
    failed = False

    def handler(context: PilotStageContext) -> dict[str, object]:
        nonlocal failed
        calls.append(context.stage_name)
        if context.stage_name == "05_acquire_public_documents":
            network_calls["count"] += 1
        if context.stage_name == fail_once_at and not failed:
            failed = True
            raise RuntimeError("fixture failure")
        return {
            "stage": context.stage_name,
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
    return (
        HistoricalPilotRunner(
            state=state,
            data_dir=data_dir,
            stage_handlers=handlers,
            clock=lambda: NOW,
        ),
        calls,
        network_calls,
    )


def test_runs_twelve_stages_in_order_with_chained_hashes_and_backup(
    tmp_path: Path,
) -> None:
    runner, calls, _network = built_runner(tmp_path)

    summary = runner.run(
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
        known_at=NOW,
        acquisition_mode="approved-public",
    )

    assert summary.failed_stage is None
    assert tuple(summary.stage_statuses) == HistoricalPilotRunner.STAGES
    assert all(status == "SUCCEEDED" for status in summary.stage_statuses.values())
    assert calls == list(HistoricalPilotRunner.STAGES[1:])
    assert len(summary.stage_output_hashes) == 12
    assert len(set(summary.stage_output_hashes.values())) == 12
    assert summary.backup_path is not None
    assert summary.backup_path.is_file()
    assert summary.backup_path.parent == runner.data_dir / "backups"


def test_failure_stops_then_resume_reuses_prior_checkpoints_and_universe(
    tmp_path: Path,
) -> None:
    runner, calls, _network = built_runner(
        tmp_path,
        fail_once_at="05_acquire_public_documents",
    )

    failed = runner.run(
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
        known_at=NOW,
        acquisition_mode="approved-public",
    )
    first_calls = tuple(calls)
    resumed = runner.run(
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
        known_at=NOW,
        acquisition_mode="approved-public",
    )

    assert failed.failed_stage == "05_acquire_public_documents"
    assert "06_scan_manual_inbox" not in first_calls
    assert resumed.failed_stage is None
    assert resumed.stage_statuses["02_validate_inputs"] == "REUSED"
    assert resumed.stage_statuses["03_freeze_universe"] == "REUSED"
    assert resumed.stage_statuses["04_plan_acquisition"] == "REUSED"
    assert calls.count("03_freeze_universe") == 1
    assert resumed.universe_id == "pilot-fixed-30"


def test_manual_only_never_invokes_public_acquisition_handler(
    tmp_path: Path,
) -> None:
    runner, calls, network = built_runner(tmp_path)

    summary = runner.run(
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
        known_at=NOW,
        acquisition_mode="manual-only",
    )

    assert summary.failed_stage is None
    assert "05_acquire_public_documents" not in calls
    assert network["count"] == 0
    assert summary.stage_statuses["05_acquire_public_documents"] == (
        "SKIPPED_MANUAL_ONLY"
    )
