import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from hengce.contracts.policy import SourcePolicy
from hengce.contracts.run import RefusalRecord, RunRecord

from .db import connect


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
