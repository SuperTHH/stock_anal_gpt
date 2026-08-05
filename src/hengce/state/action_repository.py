from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from hengce.contracts.market import CorporateAction
from hengce.state.db import connect


class CorporateActionRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def save_version(self, action: CorporateAction) -> CorporateAction:
        return self.save_versions((action,))[0]

    def save_versions(
        self,
        actions: tuple[CorporateAction, ...],
    ) -> tuple[CorporateAction, ...]:
        validated_actions = tuple(
            CorporateAction.model_validate(action.model_dump()) for action in actions
        )
        for action in validated_actions:
            self._validate_times(action)
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for action in validated_actions:
                self._save_version(connection, action)
        return validated_actions

    @staticmethod
    def _save_version(
        connection: sqlite3.Connection,
        validated: CorporateAction,
    ) -> None:
        payload_json = validated.model_dump_json()
        existing = connection.execute(
            """
                SELECT payload_json FROM corporate_action_versions
                WHERE record_id=?
                """,
            (validated.record_id,),
        ).fetchone()
        if existing is not None:
            stored = CorporateAction.model_validate_json(str(existing["payload_json"]))
            if stored != validated:
                raise ValueError("CORPORATE_ACTION_VERSION_CONFLICT")
            return

        if validated.supersedes_id is not None:
            predecessor_row = connection.execute(
                """
                    SELECT payload_json FROM corporate_action_versions
                    WHERE record_id=?
                    """,
                (validated.supersedes_id,),
            ).fetchone()
            if predecessor_row is None:
                raise ValueError("CORPORATE_ACTION_CHAIN_GAP")
            predecessor = CorporateAction.model_validate_json(str(predecessor_row["payload_json"]))
            if (
                predecessor.ts_code != validated.ts_code
                or predecessor.action_type is not validated.action_type
                or predecessor.published_at is None
                or predecessor.published_at >= validated.published_at
                or predecessor.valid_from >= validated.valid_from
            ):
                raise ValueError("CORPORATE_ACTION_CHAIN_CONFLICT")
            successor = connection.execute(
                """
                    SELECT record_id FROM corporate_action_versions
                    WHERE supersedes_id=?
                    """,
                (validated.supersedes_id,),
            ).fetchone()
            if successor is not None:
                raise ValueError("CORPORATE_ACTION_BRANCH_CONFLICT")

        assert validated.published_at is not None
        assert validated.effective_at is not None
        connection.execute(
            """
                INSERT INTO corporate_action_versions(
                    record_id, ts_code, action_type, supersedes_id,
                    published_at, effective_at, collected_at, valid_from,
                    raw_object_hash, quality_status, payload_json, saved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
            (
                validated.record_id,
                validated.ts_code,
                validated.action_type.value,
                validated.supersedes_id,
                validated.published_at.isoformat(),
                validated.effective_at.isoformat(),
                validated.collected_at.isoformat(),
                validated.valid_from.isoformat(),
                validated.content_hash,
                validated.quality_status.value,
                payload_json,
                datetime.now(UTC).isoformat(),
            ),
        )

    def get_version(self, record_id: str) -> CorporateAction | None:
        with connect(self.path) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM corporate_action_versions
                WHERE record_id=?
                """,
                (record_id,),
            ).fetchone()
        return (
            CorporateAction.model_validate_json(str(row["payload_json"]))
            if row is not None
            else None
        )

    def visible_actions(
        self,
        ts_code: str,
        as_of: datetime,
        known_at: datetime,
    ) -> tuple[CorporateAction, ...]:
        self._require_aware(as_of)
        self._require_aware(known_at)
        with connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM corporate_action_versions
                WHERE ts_code=?
                  AND published_at <= ?
                  AND collected_at <= ?
                  AND valid_from <= ?
                ORDER BY published_at, valid_from, record_id
                """,
                (
                    ts_code,
                    as_of.isoformat(),
                    known_at.isoformat(),
                    known_at.isoformat(),
                ),
            ).fetchall()
        return tuple(CorporateAction.model_validate_json(str(row["payload_json"])) for row in rows)

    @classmethod
    def _validate_times(cls, action: CorporateAction) -> None:
        for value in (
            action.published_at,
            action.effective_at,
            action.collected_at,
            action.valid_from,
        ):
            if value is None:
                raise ValueError("CORPORATE_ACTION_TIME_INVALID")
            cls._require_aware(value)

    @staticmethod
    def _require_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("CORPORATE_ACTION_TIME_INVALID")


__all__ = ["CorporateActionRepository"]
