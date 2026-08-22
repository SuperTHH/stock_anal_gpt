from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from hengce.contracts.market_screen import ImplementedDividend


class ImplementedDividendWarehouse:
    """Immutable exchange dividend snapshots keyed by their market cutoff date."""

    def __init__(self, root: Path) -> None:
        self.dataset = root / "implemented_dividends"

    def write_records(
        self,
        market_date: date,
        records: list[ImplementedDividend],
    ) -> Path:
        rows = [record.model_dump(mode="json") for record in records]
        rows.sort(key=self._canonical_row)
        canonical = json.dumps(
            rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        partition = self.dataset / f"market_date={market_date.isoformat()}"
        target = partition / f"part-{digest}.parquet"
        partition.mkdir(parents=True, exist_ok=True)
        temporary = partition / f".{digest}-{uuid4().hex}.tmp"
        try:
            pq.write_table(pa.Table.from_pylist(rows), temporary, compression="zstd")
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def read_records(self, market_date: date) -> list[ImplementedDividend]:
        partition = self.dataset / f"market_date={market_date.isoformat()}"
        files = sorted(partition.glob("part-*.parquet"))
        if len(files) > 1:
            raise ValueError("DIVIDEND_PARQUET_MULTIPLE_ARTIFACTS")
        if not files:
            return []
        try:
            rows = pq.ParquetFile(files[0]).read().to_pylist()
        except (OSError, pa.ArrowException, ValueError) as error:
            raise ValueError("DIVIDEND_PARQUET_INTEGRITY_ERROR") from error
        return [ImplementedDividend.model_validate(row) for row in rows]

    @staticmethod
    def _canonical_row(row: dict[str, object]) -> str:
        return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = ["ImplementedDividendWarehouse"]
