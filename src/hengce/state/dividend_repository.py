from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from hengce.contracts.dividend import AnnualDividendRecord
from hengce.state.db import connect


class AnnualDividendRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def save_version(self, record: AnnualDividendRecord) -> AnnualDividendRecord:
        validated = AnnualDividendRecord.model_validate(record.model_dump())
        self._validate_times(validated)
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._save_version(connection, validated)
        return validated

    @staticmethod
    def _save_version(
        connection: sqlite3.Connection,
        validated: AnnualDividendRecord,
    ) -> None:
        existing = connection.execute(
            "SELECT payload_json FROM annual_dividend_record_versions WHERE record_id=?",
            (validated.record_id,),
        ).fetchone()
        if existing is not None:
            stored = AnnualDividendRecord.model_validate_json(str(existing["payload_json"]))
            if stored != validated:
                raise ValueError("ANNUAL_DIVIDEND_VERSION_CONFLICT")
            return
        if validated.supersedes_id is not None:
            predecessor_row = connection.execute(
                "SELECT payload_json FROM annual_dividend_record_versions WHERE record_id=?",
                (validated.supersedes_id,),
            ).fetchone()
            if predecessor_row is None:
                raise ValueError("ANNUAL_DIVIDEND_CHAIN_GAP")
            predecessor = AnnualDividendRecord.model_validate_json(
                str(predecessor_row["payload_json"])
            )
            if (
                predecessor.ts_code != validated.ts_code
                or predecessor.fiscal_year != validated.fiscal_year
                or predecessor.published_at is None
                or predecessor.published_at >= validated.published_at
                or predecessor.valid_from >= validated.valid_from
            ):
                raise ValueError("ANNUAL_DIVIDEND_CHAIN_CONFLICT")
            successor = connection.execute(
                "SELECT record_id FROM annual_dividend_record_versions WHERE supersedes_id=?",
                (validated.supersedes_id,),
            ).fetchone()
            if successor is not None:
                raise ValueError("ANNUAL_DIVIDEND_BRANCH_CONFLICT")
        assert validated.published_at is not None
        assert validated.effective_at is not None
        connection.execute(
            """
            INSERT INTO annual_dividend_record_versions(
                record_id, ts_code, fiscal_year, supersedes_id,
                published_at, effective_at, collected_at, valid_from,
                raw_object_hash, quality_status, payload_json, saved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                validated.record_id,
                validated.ts_code,
                validated.fiscal_year,
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

    def visible_records(
        self,
        ts_code: str,
        as_of: datetime,
        known_at: datetime,
    ) -> tuple[AnnualDividendRecord, ...]:
        self._require_aware(as_of)
        self._require_aware(known_at)
        with connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM annual_dividend_record_versions
                WHERE ts_code=?
                  AND published_at <= ?
                  AND collected_at <= ?
                  AND valid_from <= ?
                ORDER BY fiscal_year, published_at, valid_from, record_id
                """,
                (
                    ts_code,
                    as_of.isoformat(),
                    known_at.isoformat(),
                    known_at.isoformat(),
                ),
            ).fetchall()
        records = tuple(
            AnnualDividendRecord.model_validate_json(str(row["payload_json"]))
            for row in rows
        )
        superseded = {
            record.supersedes_id
            for record in records
            if record.supersedes_id is not None
        }
        return tuple(record for record in records if record.record_id not in superseded)

    @classmethod
    def _validate_times(cls, record: AnnualDividendRecord) -> None:
        for value in (
            record.published_at,
            record.effective_at,
            record.collected_at,
            record.valid_from,
        ):
            if value is None:
                raise ValueError("ANNUAL_DIVIDEND_TIME_INVALID")
            cls._require_aware(value)

    @staticmethod
    def _require_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ANNUAL_DIVIDEND_TIME_INVALID")


__all__ = ["AnnualDividendRepository"]
