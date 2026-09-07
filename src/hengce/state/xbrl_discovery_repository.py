from datetime import date, datetime
from pathlib import Path

from hengce.contracts.financial import require_aware
from hengce.contracts.xbrl_discovery import ExchangeXbrlDiscoveryScan
from hengce.state.db import connect


class ExchangeXbrlDiscoveryRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, scan: ExchangeXbrlDiscoveryScan) -> ExchangeXbrlDiscoveryScan:
        validated = ExchangeXbrlDiscoveryScan.model_validate(scan.model_dump())
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_json FROM exchange_xbrl_discovery_scans WHERE scan_id=?",
                (validated.scan_id,),
            ).fetchone()
            if existing is not None:
                stored = ExchangeXbrlDiscoveryScan.model_validate_json(
                    str(existing["payload_json"])
                )
                if stored != validated:
                    raise ValueError("EXCHANGE_XBRL_SCAN_CONFLICT")
                return stored
            connection.execute(
                """
                INSERT INTO exchange_xbrl_discovery_scans(
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

    def latest(
        self,
        *,
        market_date: date,
        known_at: datetime,
    ) -> tuple[ExchangeXbrlDiscoveryScan, ...]:
        require_aware(known_at, "known_at")
        with connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM (
                    SELECT payload_json, source_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY source_id
                               ORDER BY scanned_at DESC, scan_id DESC
                           ) AS rank
                    FROM exchange_xbrl_discovery_scans
                    WHERE market_date=? AND scanned_at <= ?
                ) WHERE rank=1
                ORDER BY source_id
                """,
                (market_date.isoformat(), known_at.isoformat()),
            ).fetchall()
        return tuple(
            ExchangeXbrlDiscoveryScan.model_validate_json(str(row["payload_json"]))
            for row in rows
        )


__all__ = ["ExchangeXbrlDiscoveryRepository"]
