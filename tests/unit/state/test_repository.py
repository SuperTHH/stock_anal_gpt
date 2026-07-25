import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Barrier

import pytest

from hengce.contracts.enums import ReviewStatus, RunStatus
from hengce.contracts.market import SecurityMaster
from hengce.contracts.policy import SourcePolicy
from hengce.contracts.run import RefusalRecord, RunRecord
from hengce.policy.guard import PolicyDenied
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


def security_master_policy(**overrides: object) -> SourcePolicy:
    values: dict[str, object] = {
        "source_id": "sse",
        "source_name": "SSE",
        "allowed_domains": ["sse.com.cn", "www.sse.com.cn"],
        "allowed_schemes": ["https"],
        "allowed_purposes": ["security_master"],
        "fetch_frequency": "policy_defined",
        "full_text_rule": "necessary_public_attachment",
        "attachment_rule": "pdf_xbrl_only",
        "rate_limit_per_minute": 6,
        "robots_policy": "respect",
        "terms_url": "https://www.sse.com.cn/home/legal/",
        "terms_reviewed_at": datetime(2026, 7, 24, 9, 0, tzinfo=UTC),
        "review_status": ReviewStatus.APPROVED,
        "connection_status": "UNKNOWN",
        "enabled": True,
    }
    values.update(overrides)
    return SourcePolicy.model_validate(values)


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


def security(code: str, board: str) -> SecurityMaster:
    return SecurityMaster(
        ts_code=code,
        symbol=code.split(".")[0],
        name=f"Security {code}",
        exchange="SSE" if code.endswith(".SH") else "SZSE",
        board=board,
        list_date=date(2020, 1, 1),
        is_in_scope=True,
    )


def test_security_master_snapshot_round_trips_four_boards_and_lineage(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    repository.upsert_policy(security_master_policy())
    records = [
        security("600000.SH", "MAIN_SH"), security("688001.SH", "STAR"),
        security("000001.SZ", "MAIN_SZ"), security("300001.SZ", "CHINEXT"),
    ]

    snapshot = repository.save_security_master_snapshot(
        records, source_id="sse", source_url="https://www.sse.com.cn/master.csv",
        collected_at=datetime(2026, 7, 24, 9, 0, tzinfo=UTC), content_hash="a" * 64,
        version="2026-07-24", quality_lineage={"filter": "a_share_cny_four_boards", "row_count": 4},
    )

    assert snapshot.source_id == "sse"
    assert snapshot.source_url == "https://www.sse.com.cn/master.csv"
    assert snapshot.collected_at == datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    assert snapshot.content_hash == "a" * 64
    assert snapshot.version == "2026-07-24"
    assert snapshot.quality_lineage == {"filter": "a_share_cny_four_boards", "row_count": 4}
    assert [item.ts_code for item in snapshot.securities] == [
        "000001.SZ",
        "300001.SZ",
        "600000.SH",
        "688001.SH",
    ]
    assert repository.get_security_master_snapshot("sse", "a" * 64) == snapshot


@pytest.mark.parametrize(
    ("policy_overrides", "source_url", "reason"),
    [
        (None, "https://www.sse.com.cn/master.csv", "SOURCE_POLICY_MISSING"),
        ({"enabled": False}, "https://www.sse.com.cn/master.csv", "SOURCE_DISABLED"),
        (
            {"enabled": False, "review_status": ReviewStatus.REVIEW_REQUIRED},
            "https://www.sse.com.cn/master.csv",
            "SOURCE_REVIEW_REQUIRED",
        ),
        (
            {"allowed_purposes": ["trading_status"]},
            "https://www.sse.com.cn/master.csv",
            "PURPOSE_NOT_ALLOWED",
        ),
        ({}, "http://www.sse.com.cn/master.csv", "HTTP_ENDPOINT_NOT_ALLOWED"),
        ({}, "https://download.sse.com.cn/master.csv", "DOMAIN_NOT_ALLOWED"),
    ],
)
def test_security_master_snapshot_denials_are_audited_and_never_persist(
    tmp_path: Path,
    policy_overrides: dict[str, object] | None,
    source_url: str,
    reason: str,
) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    if policy_overrides is not None:
        repository.upsert_policy(security_master_policy(**policy_overrides))

    with pytest.raises(PolicyDenied, match=f"^{reason}$"):
        repository.save_security_master_snapshot(
            [security("600000.SH", "MAIN_SH")],
            source_id="sse",
            source_url=source_url,
            collected_at=datetime(2026, 7, 24, tzinfo=UTC),
            content_hash="f" * 64,
            version="v1",
            quality_lineage={},
        )

    assert repository.get_security_master_snapshot("sse", "f" * 64) is None
    assert repository.count_refusals() == 1


def test_disabled_policy_prevents_an_existing_snapshot_from_gating_ingestion(
    tmp_path: Path,
) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    repository.upsert_policy(security_master_policy())
    repository.save_security_master_snapshot(
        [security("600000.SH", "MAIN_SH")],
        source_id="sse",
        source_url="https://www.sse.com.cn/master.csv",
        collected_at=datetime(2026, 7, 24, tzinfo=UTC),
        content_hash="f" * 64,
        version="v1",
        quality_lineage={},
    )
    repository.upsert_policy(security_master_policy(enabled=False))

    assert repository.get_latest_security_master_snapshot() is None
    assert repository.count_refusals() == 1


def test_security_master_snapshot_rejects_duplicates_without_persisting(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()

    with pytest.raises(ValueError, match="SECURITY_MASTER_DUPLICATE_TS_CODE"):
        repository.save_security_master_snapshot(
            [security("600000.SH", "MAIN_SH"), security("600000.SH", "MAIN_SH")],
            source_id="sse", source_url="https://example.test/master.csv",
            collected_at=datetime(2026, 7, 24, tzinfo=UTC), content_hash="b" * 64,
            version="v1", quality_lineage={},
        )

    assert repository.get_security_master_snapshot("sse", "b" * 64) is None


def test_latest_security_master_snapshot_uses_most_recent_collection(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    repository.upsert_policy(security_master_policy())
    repository.save_security_master_snapshot(
        [security("600000.SH", "MAIN_SH")],
        source_id="sse", source_url="https://www.sse.com.cn/master.csv",
        collected_at=datetime(2026, 7, 23, tzinfo=UTC), content_hash="d" * 64,
        version="v1", quality_lineage={},
    )
    newer = repository.save_security_master_snapshot(
        [security("688001.SH", "STAR")],
        source_id="sse", source_url="https://www.sse.com.cn/master.csv",
        collected_at=datetime(2026, 7, 24, tzinfo=UTC), content_hash="e" * 64,
        version="v2", quality_lineage={},
    )

    assert repository.get_latest_security_master_snapshot() == newer


def test_security_master_snapshot_rolls_back_metadata_when_row_insert_fails(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    repository.upsert_policy(security_master_policy())
    with sqlite3.connect(repository.path) as connection:
        connection.execute("""
            CREATE TRIGGER abort_security_row BEFORE INSERT ON security_master_members
            WHEN NEW.ts_code = '688001.SH'
            BEGIN SELECT RAISE(ABORT, 'forced failure'); END;
        """)

    with pytest.raises(sqlite3.IntegrityError, match="forced failure"):
        repository.save_security_master_snapshot(
            [security("600000.SH", "MAIN_SH"), security("688001.SH", "STAR")],
            source_id="sse", source_url="https://www.sse.com.cn/master.csv",
            collected_at=datetime(2026, 7, 24, tzinfo=UTC), content_hash="c" * 64,
            version="v1", quality_lineage={},
        )

    assert repository.get_security_master_snapshot("sse", "c" * 64) is None


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

    assert migrations == {
        "001_initial",
        "002_rate_reservations",
        "003_ingestion_leases",
        "004_security_master_snapshots",
        "005_ingestion_run_link",
    }
    assert columns == {"source_id", "next_allowed_at", "updated_at"}
    assert lease_columns == {
        "trade_date",
        "owner_id",
        "lease_expires_at",
        "lifecycle_state",
        "staged_result_json",
        "updated_at",
        "active_run_id",
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


def test_expired_lease_takeover_terminalizes_linked_running_record_before_new_run(
    tmp_path: Path,
) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    trade_date = date(2026, 7, 24)
    started = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    abandoned = RunRecord(
        run_id="market_daily:2026-07-24:first",
        trade_date=trade_date,
        run_type="market_daily",
        started_at=started,
        run_status="RUNNING",
        stage_statuses={"lease": "RUNNING"},
    )
    repository.record_run(abandoned)
    assert repository.acquire_ingestion_lease(
        trade_date,
        owner_id="first",
        run_id=abandoned.run_id,
        now=started,
        lease_seconds=60,
    ).acquired

    takeover = repository.acquire_ingestion_lease(
        trade_date,
        owner_id="second",
        run_id="market_daily:2026-07-24:second",
        now=started.replace(minute=2),
        lease_seconds=60,
    )

    assert takeover.acquired
    assert takeover.abandoned_run_id == abandoned.run_id
    persisted = {
        run.run_id: run for run in repository.list_runs(run_type="market_daily")
    }[abandoned.run_id]
    assert persisted.run_status == "FAILED"
    assert persisted.finished_at == started.replace(minute=2)
    assert persisted.error_code == "MARKET_INGESTION_LEASE_EXPIRED"
    assert persisted.stage_statuses == {"lease": "FAILED"}


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


def test_lease_renewal_prevents_expiry_takeover_until_renewed_expiry(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    trade_date = date(2026, 7, 24)
    started = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    assert repository.acquire_ingestion_lease(
        trade_date, owner_id="owner", now=started, lease_seconds=60
    ).acquired

    assert repository.renew_ingestion_lease(
        trade_date,
        owner_id="owner",
        now=started.replace(second=50),
        lease_seconds=60,
    )
    assert repository.acquire_ingestion_lease(
        trade_date,
        owner_id="other",
        now=started.replace(minute=1, second=1),
        lease_seconds=60,
    ).acquired is False
    assert repository.acquire_ingestion_lease(
        trade_date,
        owner_id="other",
        now=started.replace(minute=1, second=51),
        lease_seconds=60,
    ).acquired
