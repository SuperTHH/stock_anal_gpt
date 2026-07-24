import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib.resources import files
from pathlib import Path

from hengce.contracts.policy import SourcePolicy
from hengce.contracts.run import RefusalRecord, RunRecord

from .db import connect


@dataclass(frozen=True)
class IngestionLease:
    trade_date: str
    owner_id: str | None
    acquired: bool
    lifecycle_state: str
    staged_result_json: str | None = None


class StateRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def migrate(self) -> None:
        with self._connection() as connection:
            migration_root = files("hengce.state").joinpath("migrations")
            for migration in sorted(
                item for item in migration_root.iterdir() if item.name.endswith(".sql")
            ):
                connection.executescript(migration.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (migration.name.removesuffix(".sql"), datetime.now(UTC).isoformat()),
                )

    def upsert_policy(self, policy: SourcePolicy) -> None:
        self.upsert_policies([policy])

    def upsert_policies(self, policies: list[SourcePolicy]) -> None:
        """Persist a prevalidated policy batch in one SQLite transaction."""
        now = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            connection.executemany(
                """
                INSERT INTO source_policies(source_id, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                [(policy.source_id, policy.model_dump_json(), now) for policy in policies],
            )

    def insert_missing_policies(self, policies: list[SourcePolicy]) -> None:
        """Atomically seed absent policies without changing reviewed runtime state."""
        now = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            existing = {
                str(row["source_id"])
                for row in connection.execute("SELECT source_id FROM source_policies")
            }
            connection.executemany(
                """
                INSERT OR IGNORE INTO source_policies(source_id, payload_json, updated_at)
                VALUES (?, ?, ?)
                """,
                [
                    (policy.source_id, policy.model_dump_json(), now)
                    for policy in policies
                    if policy.source_id not in existing
                ],
            )

    def reserve_rate_slot(
        self, source_id: str, *, requested_at: datetime, rate_limit_per_minute: int
    ) -> float:
        """Reserve a persisted request slot and return required delay in seconds."""
        if rate_limit_per_minute <= 0:
            raise ValueError("RATE_LIMIT_INVALID")
        requested_timestamp = requested_at.timestamp()
        interval = 60.0 / rate_limit_per_minute
        now = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT next_allowed_at FROM rate_reservations WHERE source_id=?",
                (source_id,),
            ).fetchone()
            reserved_at = max(
                requested_timestamp,
                float(row["next_allowed_at"]) if row is not None else requested_timestamp,
            )
            connection.execute(
                """
                INSERT INTO rate_reservations(source_id, next_allowed_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    next_allowed_at=excluded.next_allowed_at,
                    updated_at=excluded.updated_at
                """,
                (source_id, reserved_at + interval, now),
            )
        return max(0.0, reserved_at - requested_timestamp)

    def get_policy(self, source_id: str) -> SourcePolicy | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM source_policies WHERE source_id=?", (source_id,)
            ).fetchone()
        return SourcePolicy.model_validate_json(row["payload_json"]) if row else None

    def count_policies(self) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM source_policies").fetchone()
        return int(row["count"])

    def record_run(self, record: RunRecord) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO run_records(run_id, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (record.run_id, record.model_dump_json(), datetime.now(UTC).isoformat()),
            )

    def list_runs(self, *, run_type: str | None = None) -> list[RunRecord]:
        query = "SELECT payload_json FROM run_records"
        parameters: tuple[str, ...] = ()
        if run_type is not None:
            query += " WHERE json_extract(payload_json, '$.run_type')=?"
            parameters = (run_type,)
        query += " ORDER BY updated_at, run_id"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [RunRecord.model_validate_json(row["payload_json"]) for row in rows]

    def record_refusal(self, record: RefusalRecord) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO refusal_records(refusal_id, payload_json, refused_at)
                VALUES (?, ?, ?)
                """,
                (record.refusal_id, record.model_dump_json(), record.refused_at.isoformat()),
            )

    def count_refusals(self) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM refusal_records"
            ).fetchone()
        return int(row["count"])

    def save_checkpoint(self, key: str, value: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO checkpoints(checkpoint_key, checkpoint_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(checkpoint_key) DO UPDATE SET
                    checkpoint_value=excluded.checkpoint_value,
                    updated_at=excluded.updated_at
                """,
                (key, value, datetime.now(UTC).isoformat()),
            )

    def get_checkpoint(self, key: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT checkpoint_value FROM checkpoints WHERE checkpoint_key=?", (key,)
            ).fetchone()
        return str(row["checkpoint_value"]) if row else None

    def acquire_ingestion_lease(
        self,
        trade_date: date,
        *,
        owner_id: str,
        now: datetime,
        lease_seconds: float,
    ) -> IngestionLease:
        """Atomically acquire the single active daily-ingestion lease."""
        if not owner_id:
            raise ValueError("INGESTION_OWNER_INVALID")
        if lease_seconds <= 0:
            raise ValueError("INGESTION_LEASE_INVALID")
        trade_date_value = trade_date.isoformat()
        now_timestamp = now.timestamp()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM ingestion_leases WHERE trade_date=?", (trade_date_value,)
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO ingestion_leases(
                        trade_date, owner_id, lease_expires_at, lifecycle_state, staged_result_json,
                        updated_at
                    ) VALUES (?, ?, ?, 'RUNNING', NULL, ?)
                    """,
                    (
                        trade_date_value,
                        owner_id,
                        now_timestamp + lease_seconds,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                return IngestionLease(trade_date_value, owner_id, True, "RUNNING")
            expires_at = row["lease_expires_at"]
            can_acquire = row["owner_id"] is None or (
                expires_at is not None and float(expires_at) <= now_timestamp
            )
            if can_acquire:
                connection.execute(
                    """
                    UPDATE ingestion_leases
                    SET owner_id=?, lease_expires_at=?, lifecycle_state='RUNNING', updated_at=?
                    WHERE trade_date=?
                    """,
                    (
                        owner_id,
                        now_timestamp + lease_seconds,
                        datetime.now(UTC).isoformat(),
                        trade_date_value,
                    ),
                )
                return IngestionLease(
                    trade_date_value,
                    owner_id,
                    True,
                    "RUNNING",
                    row["staged_result_json"],
                )
            return IngestionLease(
                trade_date_value,
                str(row["owner_id"]),
                False,
                str(row["lifecycle_state"]),
                row["staged_result_json"],
            )

    def release_ingestion_lease(self, trade_date: date, *, owner_id: str) -> bool:
        """Release only the caller's lease; stale owners cannot release a takeover."""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE ingestion_leases
                SET owner_id=NULL, lease_expires_at=NULL, updated_at=?
                WHERE trade_date=? AND owner_id=?
                """,
                (datetime.now(UTC).isoformat(), trade_date.isoformat(), owner_id),
            )
        return cursor.rowcount == 1

    def stage_ingestion_artifact(
        self, trade_date: date, *, owner_id: str, staged_result_json: str
    ) -> bool:
        """Durably save a published artifact before its checkpoint is made visible."""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE ingestion_leases
                SET staged_result_json=?, lifecycle_state='STAGED', updated_at=?
                WHERE trade_date=? AND owner_id=?
                """,
                (
                    staged_result_json,
                    datetime.now(UTC).isoformat(),
                    trade_date.isoformat(),
                    owner_id,
                ),
            )
        return cursor.rowcount == 1

    def get_ingestion_state(self, trade_date: date) -> IngestionLease | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM ingestion_leases WHERE trade_date=?", (trade_date.isoformat(),)
            ).fetchone()
        if row is None:
            return None
        return IngestionLease(
            trade_date=str(row["trade_date"]),
            owner_id=str(row["owner_id"]) if row["owner_id"] is not None else None,
            acquired=False,
            lifecycle_state=str(row["lifecycle_state"]),
            staged_result_json=row["staged_result_json"],
        )

    def finish_ingestion_lease(
        self, trade_date: date, *, owner_id: str, lifecycle_state: str
    ) -> bool:
        """Transition an owned lease to a terminal state and make it available for retry."""
        if lifecycle_state not in {"SUCCEEDED", "FAILED", "BLOCKED"}:
            raise ValueError("INGESTION_LIFECYCLE_INVALID")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE ingestion_leases
                SET owner_id=NULL, lease_expires_at=NULL, lifecycle_state=?, updated_at=?
                WHERE trade_date=? AND owner_id=?
                """,
                (
                    lifecycle_state,
                    datetime.now(UTC).isoformat(),
                    trade_date.isoformat(),
                    owner_id,
                ),
            )
        return cursor.rowcount == 1

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = connect(self.path)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
