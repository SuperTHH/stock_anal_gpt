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
        sql = (
            files("hengce.state")
            .joinpath("migrations/001_initial.sql")
            .read_text(encoding="utf-8")
        )
        with connect(self.path) as connection:
            connection.executescript(sql)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                ("001_initial", datetime.now(UTC).isoformat()),
            )

    def upsert_policy(self, policy: SourcePolicy) -> None:
        self.upsert_policies([policy])

    def upsert_policies(self, policies: list[SourcePolicy]) -> None:
        """Persist a prevalidated policy batch in one SQLite transaction."""
        now = datetime.now(UTC).isoformat()
        with connect(self.path) as connection:
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

    def get_policy(self, source_id: str) -> SourcePolicy | None:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM source_policies WHERE source_id=?", (source_id,)
            ).fetchone()
        return SourcePolicy.model_validate_json(row["payload_json"]) if row else None

    def count_policies(self) -> int:
        with connect(self.path) as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM source_policies").fetchone()
        return int(row["count"])

    def record_run(self, record: RunRecord) -> None:
        with connect(self.path) as connection:
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
        with connect(self.path) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO refusal_records(refusal_id, payload_json, refused_at)
                VALUES (?, ?, ?)
                """,
                (record.refusal_id, record.model_dump_json(), record.refused_at.isoformat()),
            )

    def count_refusals(self) -> int:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM refusal_records"
            ).fetchone()
        return int(row["count"])

    def save_checkpoint(self, key: str, value: str) -> None:
        with connect(self.path) as connection:
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
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT checkpoint_value FROM checkpoints WHERE checkpoint_key=?", (key,)
            ).fetchone()
        return str(row["checkpoint_value"]) if row else None
