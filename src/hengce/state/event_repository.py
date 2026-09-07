from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from hengce.contracts.official_event import OfficialEvent, OfficialEventSourceScan
from hengce.state.db import connect


class OfficialEventRepository:
    """Append-only point-in-time store for reviewed official events."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def save_version(self, event: OfficialEvent) -> OfficialEvent:
        validated = OfficialEvent.model_validate(event.model_dump())
        self._validate_times(validated)
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_json FROM official_event_versions WHERE record_id=?",
                (validated.record_id,),
            ).fetchone()
            if existing is not None:
                stored = OfficialEvent.model_validate_json(str(existing["payload_json"]))
                if stored != validated:
                    raise ValueError("OFFICIAL_EVENT_VERSION_CONFLICT")
                return stored
            if validated.supersedes_id is not None:
                predecessor = connection.execute(
                    "SELECT payload_json FROM official_event_versions WHERE record_id=?",
                    (validated.supersedes_id,),
                ).fetchone()
                if predecessor is None:
                    raise ValueError("OFFICIAL_EVENT_CHAIN_GAP")
                previous = OfficialEvent.model_validate_json(
                    str(predecessor["payload_json"])
                )
                if (
                    previous.source_id != validated.source_id
                    or previous.event_type != validated.event_type
                    or previous.published_at is None
                    or validated.published_at is None
                    or previous.published_at >= validated.published_at
                    or previous.valid_from >= validated.valid_from
                ):
                    raise ValueError("OFFICIAL_EVENT_CHAIN_CONFLICT")
                successor = connection.execute(
                    "SELECT record_id FROM official_event_versions WHERE supersedes_id=?",
                    (validated.supersedes_id,),
                ).fetchone()
                if successor is not None:
                    raise ValueError("OFFICIAL_EVENT_BRANCH_CONFLICT")
            connection.execute(
                """
                INSERT INTO official_event_versions(
                    record_id, source_id, event_type, supersedes_id,
                    published_at, effective_at, collected_at, valid_from,
                    raw_object_hash, quality_status, payload_json, saved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validated.record_id,
                    validated.source_id,
                    validated.event_type,
                    validated.supersedes_id,
                    validated.published_at.isoformat(),
                    (
                        validated.effective_at.isoformat()
                        if validated.effective_at is not None
                        else None
                    ),
                    validated.collected_at.isoformat(),
                    validated.valid_from.isoformat(),
                    validated.content_hash,
                    validated.quality_status.value,
                    validated.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return validated

    def visible_events(
        self,
        *,
        as_of: datetime,
        known_at: datetime,
    ) -> tuple[OfficialEvent, ...]:
        self._require_aware(as_of)
        self._require_aware(known_at)
        with connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM official_event_versions
                WHERE published_at <= ?
                  AND (effective_at IS NULL OR effective_at <= ?)
                  AND collected_at <= ?
                  AND valid_from <= ?
                ORDER BY published_at DESC, valid_from DESC, record_id
                """,
                (
                    as_of.isoformat(),
                    as_of.isoformat(),
                    known_at.isoformat(),
                    known_at.isoformat(),
                ),
            ).fetchall()
        events = tuple(
            OfficialEvent.model_validate_json(str(row["payload_json"])) for row in rows
        )
        superseded = {
            event.supersedes_id for event in events if event.supersedes_id is not None
        }
        return tuple(event for event in events if event.record_id not in superseded)

    def count_visible_events(self, *, as_of: datetime, known_at: datetime) -> int:
        return len(self.visible_events(as_of=as_of, known_at=known_at))

    def save_source_scan(self, scan: OfficialEventSourceScan) -> OfficialEventSourceScan:
        validated = OfficialEventSourceScan.model_validate(scan.model_dump())
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_json FROM official_event_source_scans WHERE scan_id=?",
                (validated.scan_id,),
            ).fetchone()
            if existing is not None:
                stored = OfficialEventSourceScan.model_validate_json(
                    str(existing["payload_json"])
                )
                if stored != validated:
                    raise ValueError("OFFICIAL_EVENT_SCAN_CONFLICT")
                return stored
            connection.execute(
                """
                INSERT INTO official_event_source_scans(
                    scan_id, source_id, market_date, status, scanned_at,
                    content_hash, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validated.scan_id,
                    validated.source_id,
                    validated.market_date.isoformat(),
                    validated.status,
                    validated.scanned_at.isoformat(),
                    validated.content_hash,
                    validated.model_dump_json(),
                ),
            )
        return validated

    def latest_source_scans(
        self,
        *,
        market_date: date,
        known_at: datetime,
    ) -> tuple[OfficialEventSourceScan, ...]:
        self._require_aware(known_at)
        with connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM (
                    SELECT payload_json, source_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY source_id
                               ORDER BY scanned_at DESC, scan_id DESC
                           ) AS rank
                    FROM official_event_source_scans
                    WHERE market_date=? AND scanned_at <= ?
                ) WHERE rank=1
                ORDER BY source_id
                """,
                (market_date.isoformat(), known_at.isoformat()),
            ).fetchall()
        return tuple(
            OfficialEventSourceScan.model_validate_json(str(row["payload_json"]))
            for row in rows
        )

    @classmethod
    def _validate_times(cls, event: OfficialEvent) -> None:
        if event.published_at is None:
            raise ValueError("OFFICIAL_EVENT_TIME_INVALID")
        for value in (
            event.published_at,
            event.effective_at,
            event.collected_at,
            event.valid_from,
        ):
            if value is not None:
                cls._require_aware(value)

    @staticmethod
    def _require_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("OFFICIAL_EVENT_TIME_INVALID")


__all__ = ["OfficialEventRepository"]
