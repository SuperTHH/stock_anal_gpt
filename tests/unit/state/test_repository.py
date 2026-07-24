import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Barrier

import pytest

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
    replacement_policy = approved_policy().model_copy(update={"connection_status": "AVAILABLE"})
    repository.upsert_policy(replacement_policy)
    repository.save_checkpoint("init:last_trade_date", "2026-07-24")
    repository.save_checkpoint("init:last_trade_date", "2026-07-25")

    assert repository.get_policy("tushare") == replacement_policy
    assert repository.get_policy("missing") is None
    assert repository.get_checkpoint("init:last_trade_date") == "2026-07-25"
    assert repository.get_checkpoint("missing") is None


def test_repository_counts_policies(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()

    assert repository.count_policies() == 0
    repository.upsert_policy(approved_policy())
    assert repository.count_policies() == 1


def test_migrate_applies_state_migrations_idempotently(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")

    repository.migrate()
    repository.migrate()

    with sqlite3.connect(repository.path) as connection:
        migrations = {
            row[0] for row in connection.execute("SELECT version FROM schema_migrations")
        }
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(rate_reservations)")
        }
        lease_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(ingestion_leases)")
        }

    assert migrations == {"001_initial", "002_rate_reservations", "003_ingestion_leases"}
    assert columns == {"source_id", "next_allowed_at", "updated_at"}
    assert lease_columns == {
        "trade_date",
        "owner_id",
        "lease_expires_at",
        "lifecycle_state",
        "staged_result_json",
        "updated_at",
    }


def test_bulk_upsert_policies_is_atomic_when_later_write_fails(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    original = approved_policy().model_copy(update={"connection_status": "ORIGINAL"})
    repository.upsert_policy(original)
    replacement = approved_policy().model_copy(update={"connection_status": "REPLACED"})
    later = approved_policy().model_copy(update={"source_id": "later-source"})

    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            """
            CREATE TRIGGER abort_later_policy
            BEFORE INSERT ON source_policies
            WHEN NEW.source_id = 'later-source'
            BEGIN SELECT RAISE(ABORT, 'forced failure'); END;
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced failure"):
        repository.upsert_policies([replacement, later])

    assert repository.get_policy("tushare") == original
    assert repository.get_policy("later-source") is None


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


def test_rate_reservations_are_atomic_across_instances_and_release_connections(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    repository = StateRepository(path)
    repository.migrate()
    repository.upsert_policy(approved_policy())
    requested_at = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    barrier = Barrier(2)

    def reserve_slot() -> float:
        barrier.wait()
        return StateRepository(path).reserve_rate_slot(
            "tushare",
            requested_at=requested_at,
            rate_limit_per_minute=1,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        delays = sorted(executor.map(lambda _: reserve_slot(), range(2)))

    assert delays == [0.0, 60.0]
    repository.save_checkpoint("rate-test", "complete")
    path.unlink()


def test_ingestion_lease_is_atomic_across_repository_instances(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    StateRepository(path).migrate()
    now = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    barrier = Barrier(2)

    def acquire(owner_id: str):  # type: ignore[no-untyped-def]
        barrier.wait()
        return StateRepository(path).acquire_ingestion_lease(
            date(2026, 7, 24),
            owner_id=owner_id,
            now=now,
            lease_seconds=60,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        leases = list(executor.map(acquire, ["owner-one", "owner-two"]))

    assert sum(lease.acquired for lease in leases) == 1
    winner = next(lease for lease in leases if lease.acquired)
    assert winner.owner_id in {"owner-one", "owner-two"}


def test_expired_ingestion_lease_can_be_taken_over_and_terminal_release_is_safe(
    tmp_path: Path,
) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    now = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)

    assert repository.acquire_ingestion_lease(
        date(2026, 7, 24), owner_id="first", now=now, lease_seconds=60
    ).acquired
    assert repository.acquire_ingestion_lease(
        date(2026, 7, 24), owner_id="second", now=now, lease_seconds=60
    ).acquired is False
    assert repository.acquire_ingestion_lease(
        date(2026, 7, 24), owner_id="second", now=now.replace(minute=2), lease_seconds=60
    ).acquired

    assert repository.release_ingestion_lease(date(2026, 7, 24), owner_id="first") is False
    assert repository.release_ingestion_lease(date(2026, 7, 24), owner_id="second") is True
    assert repository.release_ingestion_lease(date(2026, 7, 24), owner_id="second") is False


def test_only_current_lease_owner_can_stage_or_read_artifact(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    trade_date = date(2026, 7, 24)
    now = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    repository.acquire_ingestion_lease(
        trade_date, owner_id="owner", now=now, lease_seconds=60
    )

    assert repository.stage_ingestion_artifact(
        trade_date, owner_id="other", staged_result_json="{}"
    ) is False
    assert repository.stage_ingestion_artifact(
        trade_date, owner_id="owner", staged_result_json='{"ready":true}'
    ) is True
    stored = repository.get_ingestion_state(trade_date)

    assert stored is not None
    assert stored.staged_result_json == '{"ready":true}'
    assert stored.lifecycle_state == "STAGED"
