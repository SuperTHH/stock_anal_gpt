import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from hengce.contracts.enums import ReviewStatus, RunStatus
from hengce.contracts.policy import SourcePolicy
from hengce.contracts.run import RefusalRecord, RunRecord
from hengce.state.repository import StateRepository


def approved_policy() -> SourcePolicy:
    return SourcePolicy(
        source_id="tushare",
        source_name="Tushare",
        allowed_domains=["api.tushare.pro"],
        allowed_schemes=["http"],
        allowed_purposes=["market_daily"],
        fetch_frequency="trading_day",
        full_text_rule="structured_only",
        attachment_rule="none",
        rate_limit_per_minute=1,
        robots_policy="api_terms",
        terms_url="https://tushare.pro/document/1?doc_id=290",
        terms_reviewed_at=datetime(2026, 7, 24, 9, 0, tzinfo=UTC),
        review_status=ReviewStatus.APPROVED,
        connection_status="UNKNOWN",
        enabled=True,
    )


def completed_run() -> RunRecord:
    return RunRecord(
        run_id="daily-2026-07-24",
        trade_date=date(2026, 7, 24),
        run_type="market_daily",
        started_at=datetime(2026, 7, 24, 15, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 24, 15, 5, tzinfo=UTC),
        run_status=RunStatus.SUCCEEDED,
        stage_statuses={"collect": "SUCCEEDED"},
    )


def refusal() -> RefusalRecord:
    return RefusalRecord(
        refusal_id="refusal-1",
        requested_url="https://example.com/private",
        resolved_domain="example.com",
        requested_purpose="market_daily",
        policy_rule="domain_not_allowed",
        refused_at=datetime(2026, 7, 24, 15, 1, tzinfo=UTC),
        reason_code="DOMAIN_DENIED",
        requesting_module="collector",
    )


def test_repository_round_trips_policy_and_checkpoint(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    repository.migrate()
    repository.upsert_policy(approved_policy())
    repository.save_checkpoint("init:last_trade_date", "2026-07-24")
    repository.save_checkpoint("init:last_trade_date", "2026-07-25")

    assert repository.get_policy("tushare") == approved_policy()
    assert repository.get_policy("missing") is None
    assert repository.get_checkpoint("init:last_trade_date") == "2026-07-25"
    assert repository.get_checkpoint("missing") is None


def test_repository_records_runs_as_upserts(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    record = completed_run()

    repository.record_run(record)
    repository.record_run(record.model_copy(update={"retry_count": 1}))

    with sqlite3.connect(repository.path) as connection:
        row = connection.execute(
            "SELECT payload_json FROM run_records WHERE run_id = ?", (record.run_id,)
        ).fetchone()
    assert row is not None
    persisted = RunRecord.model_validate_json(row[0])
    assert persisted == record.model_copy(update={"retry_count": 1})


def test_repository_records_refusals_idempotently(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    record = refusal()

    repository.record_refusal(record)
    repository.record_refusal(record)

    with sqlite3.connect(repository.path) as connection:
        rows = connection.execute(
            "SELECT payload_json FROM refusal_records WHERE refusal_id = ?", (record.refusal_id,)
        ).fetchall()
    assert len(rows) == 1
    assert RefusalRecord.model_validate_json(rows[0][0]) == record
