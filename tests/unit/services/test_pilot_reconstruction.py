from datetime import UTC, date, datetime, timedelta
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


def test_independent_event_cutoff_reaches_every_stage_and_invalidates_checkpoints(
    tmp_path: Path,
) -> None:
    runner, calls, _network = built_runner(tmp_path)
    observed: list[datetime | None] = []
    original_handlers = runner.stage_handlers.copy()

    def capture(context: PilotStageContext) -> dict[str, object]:
        observed.append(context.event_cutoff_at)
        return dict(original_handlers[context.stage_name](context))

    runner.stage_handlers = {stage: capture for stage in original_handlers}
    event_cutoff = CUTOFF + timedelta(hours=2)
    first = runner.run(
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
        event_cutoff_at=event_cutoff,
        known_at=NOW,
        acquisition_mode="manual-only",
    )
    second = runner.run(
        market_date=MARKET_DATE,
        report_cutoff_at=CUTOFF,
        event_cutoff_at=CUTOFF,
        known_at=NOW,
        acquisition_mode="manual-only",
    )

    assert first.event_cutoff_at == event_cutoff
    assert observed[0] == event_cutoff
    assert second.stage_statuses["02_validate_inputs"] == "SUCCEEDED"


def test_new_manual_inbox_content_invalidates_scan_and_downstream_checkpoints(
    tmp_path: Path,
) -> None:
    runner, calls, _network = built_runner(tmp_path)
    arguments = {
        "market_date": MARKET_DATE,
        "report_cutoff_at": CUTOFF,
        "known_at": NOW,
        "acquisition_mode": "manual-only",
    }
    first = runner.run(**arguments)
    inbox = runner.data_dir / "manual_inbox"
    inbox.mkdir()
    (inbox / "new-report.pdf").write_bytes(b"%PDF-new")

    second = runner.run(**arguments)

    assert first.failed_stage is None
    assert second.failed_stage is None
    assert second.stage_statuses["05_acquire_public_documents"] == "REUSED"
    assert second.stage_statuses["06_scan_manual_inbox"] == "SUCCEEDED"
    assert calls.count("06_scan_manual_inbox") == 2
    assert calls.count("12_write_run_summary") == 2
