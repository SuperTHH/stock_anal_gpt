from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from hengce.contracts.risk import OfficialRiskScreen
from hengce.state.db import connect


class OfficialRiskScreenRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def save_version(self, screen: OfficialRiskScreen) -> OfficialRiskScreen:
        validated = OfficialRiskScreen.model_validate(screen.model_dump())
        self._validate_times(validated)
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_json FROM official_risk_screen_versions WHERE record_id=?",
                (validated.record_id,),
            ).fetchone()
            if existing is not None:
                stored = OfficialRiskScreen.model_validate_json(str(existing["payload_json"]))
                if stored != validated:
                    raise ValueError("OFFICIAL_RISK_SCREEN_VERSION_CONFLICT")
                return stored
            if validated.supersedes_id is not None:
                predecessor = connection.execute(
                    "SELECT payload_json FROM official_risk_screen_versions WHERE record_id=?",
                    (validated.supersedes_id,),
                ).fetchone()
                if predecessor is None:
                    raise ValueError("OFFICIAL_RISK_SCREEN_CHAIN_GAP")
                previous = OfficialRiskScreen.model_validate_json(str(predecessor["payload_json"]))
                if (
                    previous.ts_code != validated.ts_code
                    or previous.published_at is None
                    or validated.published_at is None
                    or previous.published_at >= validated.published_at
                    or previous.valid_from >= validated.valid_from
                ):
                    raise ValueError("OFFICIAL_RISK_SCREEN_CHAIN_CONFLICT")
                successor = connection.execute(
                    "SELECT record_id FROM official_risk_screen_versions WHERE supersedes_id=?",
                    (validated.supersedes_id,),
                ).fetchone()
                if successor is not None:
                    raise ValueError("OFFICIAL_RISK_SCREEN_BRANCH_CONFLICT")
            if validated.published_at is None or validated.effective_at is None:
                raise ValueError("OFFICIAL_RISK_SCREEN_TIME_INVALID")
            connection.execute(
                """
                INSERT INTO official_risk_screen_versions(
                    record_id, ts_code, supersedes_id, published_at, effective_at,
                    collected_at, valid_from, raw_object_hash, quality_status,
                    payload_json, saved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validated.record_id,
                    validated.ts_code,
                    validated.supersedes_id,
                    validated.published_at.isoformat(),
                    validated.effective_at.isoformat(),
                    validated.collected_at.isoformat(),
                    validated.valid_from.isoformat(),
                    validated.content_hash,
                    validated.quality_status.value,
                    validated.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return validated

    def visible_screen(
        self,
        ts_code: str,
        *,
        as_of: datetime,
        known_at: datetime,
    ) -> OfficialRiskScreen | None:
        self._require_aware(as_of)
        self._require_aware(known_at)
        with connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM official_risk_screen_versions
                WHERE ts_code=?
                  AND published_at <= ?
                  AND effective_at <= ?
                  AND collected_at <= ?
                  AND valid_from <= ?
                ORDER BY published_at, valid_from, record_id
                """,
                (
                    ts_code,
                    as_of.isoformat(),
                    as_of.isoformat(),
                    known_at.isoformat(),
                    known_at.isoformat(),
                ),
            ).fetchall()
        screens = [OfficialRiskScreen.model_validate_json(str(row["payload_json"])) for row in rows]
        if not screens:
            return None
        superseded = {
            screen.supersedes_id for screen in screens if screen.supersedes_id is not None
        }
        terminal = [screen for screen in screens if screen.record_id not in superseded]
        if len(terminal) != 1:
            raise ValueError("OFFICIAL_RISK_SCREEN_VISIBLE_CONFLICT")
        return terminal[0]

    @classmethod
    def _validate_times(cls, screen: OfficialRiskScreen) -> None:
        for value in (
            screen.published_at,
            screen.effective_at,
            screen.collected_at,
            screen.valid_from,
        ):
            if value is None:
                raise ValueError("OFFICIAL_RISK_SCREEN_TIME_INVALID")
            cls._require_aware(value)

    @staticmethod
    def _require_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("OFFICIAL_RISK_SCREEN_TIME_INVALID")


__all__ = ["OfficialRiskScreenRepository"]
