import hashlib
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from hengce.contracts.enums import AcquisitionStatus, RunStatus
from hengce.contracts.pilot import (
    AcquisitionManifestItem,
    PilotUniverseSnapshot,
)

from .db import connect

_TERMINAL_STATUSES = frozenset(
    {AcquisitionStatus.INGESTED, AcquisitionStatus.REJECTED}
)
_ALLOWED_TRANSITIONS = {
    AcquisitionStatus.PLANNED: frozenset(
        {
            AcquisitionStatus.DISCOVERED,
            AcquisitionStatus.AWAITING_MANUAL,
            AcquisitionStatus.REJECTED,
        }
    ),
    AcquisitionStatus.DISCOVERED: frozenset(
        {
            AcquisitionStatus.DOWNLOADED,
            AcquisitionStatus.AWAITING_MANUAL,
            AcquisitionStatus.REJECTED,
        }
    ),
    AcquisitionStatus.DOWNLOADED: frozenset(
        {
            AcquisitionStatus.VERIFIED,
            AcquisitionStatus.AWAITING_MANUAL,
            AcquisitionStatus.REJECTED,
        }
    ),
    AcquisitionStatus.VERIFIED: frozenset(
        {
            AcquisitionStatus.INGESTED,
            AcquisitionStatus.AWAITING_MANUAL,
            AcquisitionStatus.REJECTED,
        }
    ),
    AcquisitionStatus.AWAITING_MANUAL: frozenset(
        {
            AcquisitionStatus.DISCOVERED,
            AcquisitionStatus.DOWNLOADED,
            AcquisitionStatus.REJECTED,
        }
    ),
}


class PipelineCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_key: str = Field(min_length=1)
    stage_name: str = Field(min_length=1)
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: RunStatus
    payload: dict[str, object]
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def updated_at_must_be_aware(
        cls,
        value: datetime,
        info: ValidationInfo,
    ) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{info.field_name} must include a timezone")
        return value


class PilotRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def publish_universe(
        self,
        snapshot: PilotUniverseSnapshot,
    ) -> PilotUniverseSnapshot:
        validated = PilotUniverseSnapshot.model_validate(snapshot.model_dump())
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT payload_json FROM pilot_universes
                WHERE market_date=? OR universe_id=?
                """,
                (validated.market_date.isoformat(), validated.universe_id),
            ).fetchone()
            if existing is not None:
                stored = PilotUniverseSnapshot.model_validate_json(
                    str(existing["payload_json"])
                )
                if stored != validated:
                    raise ValueError("PILOT_UNIVERSE_IMMUTABILITY_CONFLICT")
                return stored
            connection.execute(
                """
                INSERT INTO pilot_universes(
                    universe_id, market_date, report_cutoff_at, algorithm_version,
                    payload_json, manifest_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validated.universe_id,
                    validated.market_date.isoformat(),
                    validated.report_cutoff_at.isoformat(),
                    validated.algorithm_version,
                    validated.model_dump_json(),
                    validated.manifest_hash,
                    validated.created_at.isoformat(),
                ),
            )
        return validated

    def get_universe(self, universe_id: str) -> PilotUniverseSnapshot | None:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM pilot_universes WHERE universe_id=?",
                (universe_id,),
            ).fetchone()
        return (
            PilotUniverseSnapshot.model_validate_json(str(row["payload_json"]))
            if row is not None
            else None
        )

    def get_universe_for_date(
        self,
        market_date: date,
    ) -> PilotUniverseSnapshot | None:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM pilot_universes WHERE market_date=?",
                (market_date.isoformat(),),
            ).fetchone()
        return (
            PilotUniverseSnapshot.model_validate_json(str(row["payload_json"]))
            if row is not None
            else None
        )

    def insert_manifest(
        self,
        items: Sequence[AcquisitionManifestItem],
    ) -> int:
        validated_items = tuple(
            AcquisitionManifestItem.model_validate(item.model_dump()) for item in items
        )
        if not validated_items:
            return 0
        inserted = 0
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in validated_items:
                existing = connection.execute(
                    """
                    SELECT payload_json FROM acquisition_manifest_items
                    WHERE item_id=?
                       OR (
                           universe_id=?
                           AND ts_code=?
                           AND document_kind=?
                           AND COALESCE(report_type, '')=COALESCE(?, '')
                           AND COALESCE(report_period, '')=COALESCE(?, '')
                       )
                    """,
                    (
                        item.item_id,
                        item.universe_id,
                        item.ts_code,
                        item.document_kind.value,
                        item.report_type.value if item.report_type is not None else None,
                        item.report_period.isoformat()
                        if item.report_period is not None
                        else None,
                    ),
                ).fetchone()
                if existing is not None:
                    stored = AcquisitionManifestItem.model_validate_json(
                        str(existing["payload_json"])
                    )
                    if stored != item:
                        raise ValueError(
                            "ACQUISITION_MANIFEST_IMMUTABILITY_CONFLICT"
                        )
                    continue
                connection.execute(
                    """
                    INSERT INTO acquisition_manifest_items(
                        item_id, universe_id, ts_code, document_kind, report_type,
                        report_period, source_id, status, payload_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.item_id,
                        item.universe_id,
                        item.ts_code,
                        item.document_kind.value,
                        item.report_type.value if item.report_type is not None else None,
                        item.report_period.isoformat()
                        if item.report_period is not None
                        else None,
                        item.source_id,
                        item.status.value,
                        item.model_dump_json(),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                inserted += 1
        return inserted

    def list_manifest(
        self,
        universe_id: str,
        statuses: frozenset[AcquisitionStatus] | None = None,
    ) -> tuple[AcquisitionManifestItem, ...]:
        query = (
            "SELECT payload_json FROM acquisition_manifest_items "
            "WHERE universe_id=?"
        )
        parameters: list[str] = [universe_id]
        if statuses is not None:
            if not statuses:
                return ()
            placeholders = ", ".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            parameters.extend(status.value for status in sorted(statuses, key=str))
        query += " ORDER BY item_id"
        with connect(self.path) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(
            AcquisitionManifestItem.model_validate_json(str(row["payload_json"]))
            for row in rows
        )

    def get_manifest_item(
        self,
        item_id: str,
    ) -> AcquisitionManifestItem | None:
        with connect(self.path) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM acquisition_manifest_items
                WHERE item_id=?
                """,
                (item_id,),
            ).fetchone()
        return (
            AcquisitionManifestItem.model_validate_json(str(row["payload_json"]))
            if row is not None
            else None
        )

    def transition(
        self,
        item_id: str,
        expected_from: AcquisitionStatus,
        updated: AcquisitionManifestItem,
        observed_at: datetime,
    ) -> AcquisitionManifestItem:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("ACQUISITION_TRANSITION_TIME_INVALID")
        validated = AcquisitionManifestItem.model_validate(updated.model_dump())
        if validated.item_id != item_id:
            raise ValueError("ACQUISITION_TRANSITION_IDENTITY_CHANGED")
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_json FROM acquisition_manifest_items WHERE item_id=?",
                (item_id,),
            ).fetchone()
            if row is None:
                raise ValueError("ACQUISITION_ITEM_NOT_FOUND")
            current = AcquisitionManifestItem.model_validate_json(
                str(row["payload_json"])
            )
            if current == validated:
                return current
            if current.status is not expected_from:
                raise ValueError("ACQUISITION_STATE_CHANGED")
            if current.status in _TERMINAL_STATUSES:
                raise ValueError("ACQUISITION_TERMINAL_STATE")
            if validated.status not in _ALLOWED_TRANSITIONS[current.status]:
                raise ValueError("ACQUISITION_TRANSITION_INVALID")
            if self._identity(current) != self._identity(validated):
                raise ValueError("ACQUISITION_TRANSITION_IDENTITY_CHANGED")

            payload = validated.model_dump_json()
            payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            transition_id = hashlib.sha256(
                (
                    f"{item_id}:{current.status.value}:{validated.status.value}:"
                    f"{payload_hash}"
                ).encode()
            ).hexdigest()
            connection.execute(
                """
                UPDATE acquisition_manifest_items
                SET source_id=?, status=?, payload_json=?, updated_at=?
                WHERE item_id=? AND status=?
                """,
                (
                    validated.source_id,
                    validated.status.value,
                    payload,
                    observed_at.isoformat(),
                    item_id,
                    current.status.value,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO acquisition_transitions(
                    transition_id, item_id, from_status, to_status, error_code,
                    observed_at, payload_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transition_id,
                    item_id,
                    current.status.value,
                    validated.status.value,
                    validated.error_code,
                    observed_at.isoformat(),
                    payload_hash,
                ),
            )
        return validated

    def save_checkpoint(self, checkpoint: PipelineCheckpoint) -> None:
        validated = PipelineCheckpoint.model_validate(checkpoint.model_dump())
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT input_hash FROM pipeline_checkpoints
                WHERE run_key=? AND stage_name=?
                """,
                (validated.run_key, validated.stage_name),
            ).fetchone()
            if row is not None and str(row["input_hash"]) != validated.input_hash:
                raise ValueError("PIPELINE_CHECKPOINT_INPUT_CHANGED")
            connection.execute(
                """
                INSERT INTO pipeline_checkpoints(
                    run_key, stage_name, input_hash, status, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_key, stage_name) DO UPDATE SET
                    status=excluded.status,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    validated.run_key,
                    validated.stage_name,
                    validated.input_hash,
                    validated.status.value,
                    validated.model_dump_json(),
                    validated.updated_at.isoformat(),
                ),
            )

    def get_checkpoint(
        self,
        run_key: str,
        stage_name: str,
    ) -> PipelineCheckpoint | None:
        with connect(self.path) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM pipeline_checkpoints
                WHERE run_key=? AND stage_name=?
                """,
                (run_key, stage_name),
            ).fetchone()
        return (
            PipelineCheckpoint.model_validate_json(str(row["payload_json"]))
            if row is not None
            else None
        )

    @staticmethod
    def _identity(
        item: AcquisitionManifestItem,
    ) -> tuple[object, ...]:
        return (
            item.item_id,
            item.universe_id,
            item.ts_code,
            item.document_kind,
            item.report_type,
            item.report_period,
            item.report_cutoff_at,
        )
