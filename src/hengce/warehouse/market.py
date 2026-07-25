import hashlib
import json
import os
from datetime import date
from pathlib import Path
from uuid import uuid4

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from hengce.contracts.market import MarketBar


class MarketWarehouse:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.dataset = root / "market_bars"

    def write_bars(self, bars: list[MarketBar]) -> Path:
        target, _ = self.expected_artifact(bars)
        rows = [bar.model_dump(mode="json") for bar in bars]
        rows.sort(key=self._canonical_row)
        digest = target.stem.removeprefix("part-")
        temporary = target.parent / f".{digest}-{uuid4().hex}.tmp"
        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            pq.write_table(pa.Table.from_pylist(rows), temporary, compression="zstd")
            self._flush(temporary)
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        self.validate_artifact(target, bars[0].trade_date, len(bars), digest)
        return target

    def expected_artifact(self, bars: list[MarketBar]) -> tuple[Path, str]:
        """Return the deterministic target and canonical hash without publishing it."""
        if not bars:
            raise ValueError("bars must not be empty")
        trade_dates = {bar.trade_date for bar in bars}
        if len(trade_dates) != 1:
            raise ValueError("one write must contain exactly one trade_date")

        rows = [bar.model_dump(mode="json") for bar in bars]
        rows.sort(key=self._canonical_row)
        canonical = json.dumps(
            rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        trade_date = next(iter(trade_dates)).isoformat()
        partition = self.dataset / f"trade_date={trade_date}"
        target = partition / f"part-{digest}.parquet"
        return target, digest

    @staticmethod
    def validate_artifact(
        path: Path,
        trade_date: date,
        expected_count: int,
        expected_content_hash: str | None = None,
    ) -> str:
        """Validate exact canonical content, date/count, and content-addressed filename."""
        try:
            rows = pq.ParquetFile(path).read().to_pylist()
        except (OSError, pa.ArrowException, ValueError) as error:
            raise ValueError("MARKET_PARQUET_INTEGRITY_ERROR") from error
        rows.sort(key=MarketWarehouse._canonical_row)
        canonical = json.dumps(
            rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        content_hash = hashlib.sha256(canonical).hexdigest()
        if (
            path.name != f"part-{content_hash}.parquet"
            or expected_content_hash is not None and content_hash != expected_content_hash
            or len(rows) != expected_count
            or any(row.get("trade_date") != trade_date.isoformat() for row in rows)
        ):
            raise ValueError("MARKET_PARQUET_INTEGRITY_ERROR")
        return content_hash

    @staticmethod
    def _canonical_row(row: dict[str, object]) -> str:
        return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _flush(path: Path) -> None:
        with path.open("r+b") as handle:
            os.fsync(handle.fileno())

    def _files(self, trade_date: date) -> list[str]:
        partition = self.dataset / f"trade_date={trade_date.isoformat()}"
        return [str(path).replace("\\", "/") for path in partition.glob("part-*.parquet")]

    def count_bars(self, trade_date: date) -> int:
        files = self._files(trade_date)
        if not files:
            return 0
        with duckdb.connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM read_parquet(?)", [files]).fetchone()
        return int(row[0])

    def read_bars(self, trade_date: date) -> list[dict[str, object]]:
        files = self._files(trade_date)
        if not files:
            return []
        with duckdb.connect() as connection:
            cursor = connection.execute("SELECT * FROM read_parquet(?) ORDER BY ts_code", [files])
            columns = [item[0] for item in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
