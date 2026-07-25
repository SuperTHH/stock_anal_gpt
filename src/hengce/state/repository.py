import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib.resources import files
from pathlib import Path

from hengce.contracts.enums import QualityStatus, RunStatus
from hengce.contracts.market import SecurityMaster
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
    active_run_id: str | None = None
    abandoned_run_id: str | None = None


@dataclass(frozen=True)
class SecurityMasterSnapshot:
    source_id: str
    source_url: str
    collected_at: datetime
    content_hash: str
    version: str
    quality_lineage: dict[str, object]
    securities: list[SecurityMaster]


@dataclass(frozen=True)
class SecurityMasterUniverse:
    components: tuple[SecurityMasterSnapshot, SecurityMasterSnapshot]
    securities: list[SecurityMaster]
    as_of: datetime
    universe_hash: str
    quality_status: QualityStatus


class StateRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def migrate(self) -> None:
        with self._connection() as connection:
            # Numbered SQL files remain the migration ledger. Schema changes that SQLite
            # cannot express idempotently in SQL use a comment-only marker (currently 005)
            # plus a guarded Python alteration immediately after the migration loop.
            migration_root = files("hengce.state").joinpath("migrations")
            for migration in sorted(
                item for item in migration_root.iterdir() if item.name.endswith(".sql")
            ):
                connection.executescript(migration.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (migration.name.removesuffix(".sql"), datetime.now(UTC).isoformat()),
                )
            lease_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(ingestion_leases)")
            }
            if "active_run_id" not in lease_columns:
                connection.execute("ALTER TABLE ingestion_leases ADD COLUMN active_run_id TEXT")

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

    def save_security_master_snapshot(
        self,
        securities: list[SecurityMaster],
        *,
        source_id: str,
        source_url: str,
        collected_at: datetime,
        content_hash: str,
        version: str,
        quality_lineage: dict[str, object],
    ) -> SecurityMasterSnapshot:
        """Atomically persist one auditable, filtered official-master snapshot."""
        self._validate_security_master_snapshot(
            securities, source_id, source_url, collected_at, content_hash, version
        )
        from hengce.policy.guard import PolicyGuard

        PolicyGuard(self).validate(
            source_id,
            source_url,
            "security_master",
            "state.security_master_import",
        )
        collected_at_value = collected_at.isoformat()
        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT snapshot_id FROM security_master_snapshots
                WHERE source_id=? AND content_hash=? AND collected_at=? AND version=?
                """,
                (source_id, content_hash, collected_at_value, version),
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO security_master_snapshots(
                        source_id, source_url, collected_at, content_hash, version,
                        quality_lineage_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        source_url,
                        collected_at_value,
                        content_hash,
                        version,
                        json.dumps(quality_lineage, ensure_ascii=False, sort_keys=True),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                snapshot_id = int(cursor.lastrowid)
                connection.executemany(
                    """
                    INSERT INTO security_master_members(snapshot_id, ts_code, payload_json)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (snapshot_id, security.ts_code, security.model_dump_json())
                        for security in securities
                    ],
                )
            else:
                snapshot_id = int(existing["snapshot_id"])
            row = connection.execute(
                "SELECT * FROM security_master_snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            members = connection.execute(
                """
                SELECT payload_json FROM security_master_members
                WHERE snapshot_id=? ORDER BY ts_code
                """,
                (snapshot_id,),
            ).fetchall()
        return self._snapshot_from_rows(row, members)

    def get_security_master_snapshot(
        self, source_id: str, content_hash: str
    ) -> SecurityMasterSnapshot | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM security_master_snapshots
                WHERE source_id=? AND content_hash=?
                ORDER BY snapshot_id DESC LIMIT 1
                """,
                (source_id, content_hash),
            ).fetchone()
            if row is None:
                return None
            members = connection.execute(
                """
                SELECT payload_json FROM security_master_members
                WHERE snapshot_id=? ORDER BY ts_code
                """,
                (row["snapshot_id"],),
            ).fetchall()
        return self._snapshot_from_rows(row, members)

    def get_latest_security_master_snapshot(
        self, source_id: str
    ) -> SecurityMasterSnapshot | None:
        """Return the newest persisted approved in-scope security-master snapshot."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM security_master_snapshots
                WHERE source_id=?
                ORDER BY collected_at DESC, snapshot_id DESC LIMIT 1
                """,
                (source_id,),
            ).fetchone()
            if row is None:
                return None
            members = connection.execute(
                """
                SELECT payload_json FROM security_master_members
                WHERE snapshot_id=? ORDER BY ts_code
                """,
                (row["snapshot_id"],),
            ).fetchall()
        snapshot = self._snapshot_from_rows(row, members)
        self._validate_security_master_snapshot(
            snapshot.securities,
            snapshot.source_id,
            snapshot.source_url,
            snapshot.collected_at,
            snapshot.content_hash,
            snapshot.version,
        )
        from hengce.policy.guard import PolicyDenied, PolicyGuard

        try:
            PolicyGuard(self).validate(
                snapshot.source_id,
                snapshot.source_url,
                "security_master",
                "services.market_ingestion",
            )
        except PolicyDenied as error:
            raise ValueError("SECURITY_MASTER_POLICY_DENIED") from error
        return snapshot

    def get_security_master_universe(self) -> SecurityMasterUniverse:
        """Derive one complete, deterministic universe from both required exchanges."""
        required_sources = ("sse", "szse")
        missing_errors = {
            "sse": "SECURITY_MASTER_SSE_UNAVAILABLE",
            "szse": "SECURITY_MASTER_SZSE_UNAVAILABLE",
        }
        resolved_components: list[SecurityMasterSnapshot] = []
        for source_id in required_sources:
            component = self.get_latest_security_master_snapshot(source_id)
            if component is None:
                raise ValueError(missing_errors[source_id])
            resolved_components.append(component)

        components = (resolved_components[0], resolved_components[1])
        securities = sorted(
            (
                security
                for component in components
                for security in component.securities
            ),
            key=lambda security: security.ts_code,
        )
        if len({security.ts_code for security in securities}) != len(securities):
            raise ValueError("SECURITY_MASTER_DUPLICATE_TS_CODE")

        identity = [
            {
                "source_id": component.source_id,
                "version": component.version,
                "content_hash": component.content_hash,
                "collected_at": component.collected_at.isoformat(),
            }
            for component in components
        ]
        universe_hash = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return SecurityMasterUniverse(
            components=components,
            securities=securities,
            as_of=min(component.collected_at for component in components),
            universe_hash=universe_hash,
            quality_status=QualityStatus.VALID,
        )

    @staticmethod
    def _snapshot_from_rows(
        row: sqlite3.Row, members: list[sqlite3.Row]
    ) -> SecurityMasterSnapshot:
        return SecurityMasterSnapshot(
            source_id=str(row["source_id"]),
            source_url=str(row["source_url"]),
            collected_at=datetime.fromisoformat(str(row["collected_at"])),
            content_hash=str(row["content_hash"]),
            version=str(row["version"]),
            quality_lineage=json.loads(str(row["quality_lineage_json"])),
            securities=[
                SecurityMaster.model_validate_json(member["payload_json"]) for member in members
            ],
        )

    @staticmethod
    def _validate_security_master_snapshot(
        securities: list[SecurityMaster],
        source_id: str,
        source_url: str,
        collected_at: datetime,
        content_hash: str,
        version: str,
    ) -> None:
        if not securities or not source_id or not source_url or not version:
            raise ValueError("SECURITY_MASTER_SNAPSHOT_INVALID")
        if collected_at.tzinfo is None or len(content_hash) != 64:
            raise ValueError("SECURITY_MASTER_SNAPSHOT_INVALID")
        if len({security.ts_code for security in securities}) != len(securities):
            raise ValueError("SECURITY_MASTER_DUPLICATE_TS_CODE")
        allowed_boards = {"MAIN_SH", "STAR", "MAIN_SZ", "CHINEXT"}
        if any(
            not security.is_in_scope
            or security.security_type != "A_SHARE"
            or security.currency != "CNY"
            or security.board not in allowed_boards
            for security in securities
        ):
            raise ValueError("SECURITY_MASTER_SCOPE_INVALID")

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
        run_id: str | None = None,
        running_run: RunRecord | None = None,
        now: datetime,
        lease_seconds: float,
    ) -> IngestionLease:
        """Atomically acquire the single active daily-ingestion lease."""
        if not owner_id:
            raise ValueError("INGESTION_OWNER_INVALID")
        if lease_seconds <= 0:
            raise ValueError("INGESTION_LEASE_INVALID")
        if running_run is not None:
            if (
                running_run.run_status != RunStatus.RUNNING
                or running_run.trade_date != trade_date
                or run_id is not None
                and run_id != running_run.run_id
            ):
                raise ValueError("INGESTION_RUNNING_RUN_INVALID")
            run_id = running_run.run_id
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
                        updated_at, active_run_id
                    ) VALUES (?, ?, ?, 'RUNNING', NULL, ?, ?)
                    """,
                    (
                        trade_date_value,
                        owner_id,
                        now_timestamp + lease_seconds,
                        datetime.now(UTC).isoformat(),
                        run_id,
                    ),
                )
                if running_run is not None:
                    self._upsert_run(connection, running_run, now)
                return IngestionLease(
                    trade_date=trade_date_value,
                    owner_id=owner_id,
                    acquired=True,
                    lifecycle_state="RUNNING",
                    active_run_id=run_id,
                )
            expires_at = row["lease_expires_at"]
            can_acquire = row["owner_id"] is None or (
                expires_at is not None and float(expires_at) <= now_timestamp
            )
            if can_acquire:
                abandoned_run_id = (
                    str(row["active_run_id"])
                    if row["owner_id"] is not None and row["active_run_id"] is not None
                    else None
                )
                if abandoned_run_id is not None:
                    abandoned_row = connection.execute(
                        "SELECT payload_json FROM run_records WHERE run_id=?",
                        (abandoned_run_id,),
                    ).fetchone()
                    if abandoned_row is not None:
                        abandoned = RunRecord.model_validate_json(abandoned_row["payload_json"])
                        if abandoned.run_status == "RUNNING":
                            terminal = abandoned.model_copy(
                                update={
                                    "finished_at": now,
                                    "run_status": RunStatus.FAILED,
                                    "stage_statuses": {"lease": "FAILED"},
                                    "error_code": "MARKET_INGESTION_LEASE_EXPIRED",
                                    "error_summary": "market daily ingestion lease expired",
                                }
                            )
                            connection.execute(
                                """
                                UPDATE run_records
                                SET payload_json=?, updated_at=?
                                WHERE run_id=?
                                """,
                                (terminal.model_dump_json(), now.isoformat(), abandoned_run_id),
                            )
                connection.execute(
                    """
                    UPDATE ingestion_leases
                    SET owner_id=?, lease_expires_at=?, lifecycle_state='RUNNING', updated_at=?,
                        active_run_id=?
                    WHERE trade_date=?
                    """,
                    (
                        owner_id,
                        now_timestamp + lease_seconds,
                        datetime.now(UTC).isoformat(),
                        run_id,
                        trade_date_value,
                    ),
                )
                if running_run is not None:
                    self._upsert_run(connection, running_run, now)
                return IngestionLease(
                    trade_date=trade_date_value,
                    owner_id=owner_id,
                    acquired=True,
                    lifecycle_state="RUNNING",
                    staged_result_json=row["staged_result_json"],
                    active_run_id=run_id,
                    abandoned_run_id=abandoned_run_id,
                )
            return IngestionLease(
                trade_date=trade_date_value,
                owner_id=str(row["owner_id"]),
                acquired=False,
                lifecycle_state=str(row["lifecycle_state"]),
                staged_result_json=row["staged_result_json"],
                active_run_id=(
                    str(row["active_run_id"]) if row["active_run_id"] is not None else None
                ),
            )

    def release_ingestion_lease(self, trade_date: date, *, owner_id: str) -> bool:
        """Release only the caller's lease; stale owners cannot release a takeover."""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE ingestion_leases
                SET owner_id=NULL, lease_expires_at=NULL, active_run_id=NULL, updated_at=?
                WHERE trade_date=? AND owner_id=?
                """,
                (datetime.now(UTC).isoformat(), trade_date.isoformat(), owner_id),
            )
        return cursor.rowcount == 1

    def renew_ingestion_lease(
        self,
        trade_date: date,
        *,
        owner_id: str,
        now: datetime,
        lease_seconds: float,
    ) -> bool:
        """Extend an owned lease before expiry without changing its staged state."""
        if lease_seconds <= 0:
            raise ValueError("INGESTION_LEASE_INVALID")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE ingestion_leases
                SET lease_expires_at=?, updated_at=?
                WHERE trade_date=? AND owner_id=?
                """,
                (
                    now.timestamp() + lease_seconds,
                    datetime.now(UTC).isoformat(),
                    trade_date.isoformat(),
                    owner_id,
                ),
            )
        return cursor.rowcount == 1

    def stage_ingestion_artifact(
        self, trade_date: date, *, owner_id: str, staged_result_json: str
    ) -> bool:
        """Durably save expected artifact identity as a pre-publication recovery intent."""
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

    def finalize_ingestion_run(
        self,
        trade_date: date,
        *,
        owner_id: str,
        lifecycle_state: str,
        terminal_run: RunRecord,
    ) -> bool:
        """Atomically terminalize an owned lease and its exactly linked run record."""
        expected_status = {
            "SUCCEEDED": RunStatus.SUCCEEDED,
            "FAILED": RunStatus.FAILED,
            "BLOCKED": RunStatus.BLOCKED,
        }
        if lifecycle_state not in expected_status:
            raise ValueError("INGESTION_LIFECYCLE_INVALID")
        if (
            terminal_run.run_status != expected_status[lifecycle_state]
            or terminal_run.finished_at is None
        ):
            raise ValueError("INGESTION_TERMINAL_RUN_INVALID")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE ingestion_leases
                SET owner_id=NULL, lease_expires_at=NULL, lifecycle_state=?, active_run_id=NULL,
                    updated_at=?
                WHERE trade_date=? AND owner_id=? AND active_run_id=?
                """,
                (
                    lifecycle_state,
                    datetime.now(UTC).isoformat(),
                    trade_date.isoformat(),
                    owner_id,
                    terminal_run.run_id,
                ),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """
                INSERT INTO run_records(run_id, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    terminal_run.run_id,
                    terminal_run.model_dump_json(),
                    terminal_run.finished_at.isoformat(),
                ),
            )
        return True

    def publish_and_finalize_ingestion(
        self,
        checkpoint_key: str,
        checkpoint_value: str,
        trade_date: date,
        *,
        owner_id: str,
        terminal_run: RunRecord,
    ) -> bool:
        """Publish a matching staged identity and terminalize its owner in one transaction."""
        if (
            terminal_run.run_status != RunStatus.SUCCEEDED
            or terminal_run.finished_at is None
        ):
            raise ValueError("INGESTION_TERMINAL_RUN_INVALID")
        checkpoint_identity = self._ingestion_result_identity(checkpoint_value, staged=False)
        if checkpoint_identity is None:
            raise ValueError("INGESTION_CHECKPOINT_IDENTITY_INVALID")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                """
                SELECT * FROM ingestion_leases
                WHERE trade_date=? AND owner_id=? AND active_run_id=?
                """,
                (trade_date.isoformat(), owner_id, terminal_run.run_id),
            ).fetchone()
            if lease is None or self._ingestion_result_identity(
                lease["staged_result_json"], staged=True
            ) != checkpoint_identity:
                return False
            connection.execute(
                """
                INSERT INTO checkpoints(checkpoint_key, checkpoint_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(checkpoint_key) DO UPDATE SET
                    checkpoint_value=excluded.checkpoint_value,
                    updated_at=excluded.updated_at
                """,
                (checkpoint_key, checkpoint_value, terminal_run.finished_at.isoformat()),
            )
            cursor = connection.execute(
                """
                UPDATE ingestion_leases
                SET owner_id=NULL, lease_expires_at=NULL, lifecycle_state='SUCCEEDED',
                    active_run_id=NULL, updated_at=?
                WHERE trade_date=? AND owner_id=? AND active_run_id=?
                """,
                (
                    terminal_run.finished_at.isoformat(),
                    trade_date.isoformat(),
                    owner_id,
                    terminal_run.run_id,
                ),
            )
            if cursor.rowcount != 1:
                return False
            self._upsert_run(connection, terminal_run, terminal_run.finished_at)
        return True

    def reconcile_published_ingestion(
        self,
        trade_date: date,
        *,
        checkpoint_value: str,
        now: datetime,
    ) -> str | None:
        """Atomically close a linked run after a validated checkpoint proves publication."""
        checkpoint_identity = self._ingestion_result_identity(checkpoint_value, staged=False)
        if checkpoint_identity is None:
            return None
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                "SELECT * FROM ingestion_leases WHERE trade_date=?",
                (trade_date.isoformat(),),
            ).fetchone()
            if (
                lease is None
                or lease["owner_id"] is None
                or lease["active_run_id"] is None
                or self._ingestion_result_identity(
                    lease["staged_result_json"], staged=True
                )
                != checkpoint_identity
            ):
                return None
            run_id = str(lease["active_run_id"])
            row = connection.execute(
                "SELECT payload_json FROM run_records WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            linked_run = RunRecord.model_validate_json(row["payload_json"])
            if linked_run.run_status == RunStatus.RUNNING:
                linked_run = linked_run.model_copy(
                    update={
                        "finished_at": now,
                        "run_status": RunStatus.SUCCEEDED,
                        "stage_statuses": {
                            "checkpoint": "SUCCEEDED",
                            "reconcile": "SUCCEEDED",
                        },
                        "error_code": None,
                        "error_summary": None,
                    }
                )
                connection.execute(
                    """
                    UPDATE run_records
                    SET payload_json=?, updated_at=?
                    WHERE run_id=?
                    """,
                    (linked_run.model_dump_json(), now.isoformat(), run_id),
                )
            connection.execute(
                """
                UPDATE ingestion_leases
                SET owner_id=NULL, lease_expires_at=NULL, lifecycle_state='SUCCEEDED',
                    active_run_id=NULL, updated_at=?
                WHERE trade_date=? AND owner_id=? AND active_run_id=?
                """,
                (
                    now.isoformat(),
                    trade_date.isoformat(),
                    str(lease["owner_id"]),
                    run_id,
                ),
            )
        return run_id

    @staticmethod
    def _ingestion_result_identity(
        value: str | None, *, staged: bool
    ) -> tuple[str, int, str, str, str] | None:
        if value is None:
            return None
        try:
            payload = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
        if staged:
            if not isinstance(payload, dict):
                return None
            payload = payload.get("result")
        if not isinstance(payload, dict):
            return None
        fields = (
            "trade_date",
            "bar_count",
            "raw_content_hash",
            "parquet_path",
            "parquet_content_hash",
        )
        values = tuple(payload.get(field) for field in fields)
        if (
            not isinstance(values[0], str)
            or not isinstance(values[1], int)
            or isinstance(values[1], bool)
            or not all(isinstance(item, str) for item in values[2:])
        ):
            return None
        return values  # type: ignore[return-value]

    @staticmethod
    def _upsert_run(
        connection: sqlite3.Connection, run: RunRecord, updated_at: datetime
    ) -> None:
        connection.execute(
            """
            INSERT INTO run_records(run_id, payload_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (run.run_id, run.model_dump_json(), updated_at.isoformat()),
        )

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
            active_run_id=(
                str(row["active_run_id"]) if row["active_run_id"] is not None else None
            ),
        )

    def discard_staged_ingestion_artifact(self, trade_date: date, *, owner_id: str) -> bool:
        """Discard an invalid staged artifact while retaining the caller's lease for refetch."""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE ingestion_leases
                SET staged_result_json=NULL, lifecycle_state='RUNNING', updated_at=?
                WHERE trade_date=? AND owner_id=?
                """,
                (datetime.now(UTC).isoformat(), trade_date.isoformat(), owner_id),
            )
        return cursor.rowcount == 1

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
                SET owner_id=NULL, lease_expires_at=NULL, lifecycle_state=?, active_run_id=NULL,
                    updated_at=?
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
