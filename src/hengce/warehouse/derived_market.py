import hashlib
import json
import os
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from hengce.actions.calculator import TotalReturnPoint


class DerivedMarketWarehouse:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.dataset = root / "total_return"

    def write_total_return(self, points: list[TotalReturnPoint]) -> Path:
        if not points:
            raise ValueError("TOTAL_RETURN_POINTS_EMPTY")
        ts_codes = {point.ts_code for point in points}
        versions = {point.algorithm_version for point in points}
        if len(ts_codes) != 1 or len(versions) != 1:
            raise ValueError("TOTAL_RETURN_PARTITION_MIXED")
        rows = [asdict(point) for point in points]
        rows.sort(key=lambda row: str(row["trade_date"]))
        canonical = self._canonical(rows)
        digest = hashlib.sha256(canonical).hexdigest()
        ts_code = next(iter(ts_codes))
        version = next(iter(versions))
        target = (
            self.dataset
            / f"algorithm_version={version}"
            / f"ts_code={ts_code}"
            / f"part-{digest}.parquet"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{digest}-{uuid4().hex}.tmp"
        storage_rows = [
            {
                **row,
                "action_record_ids": list(row["action_record_ids"]),
            }
            for row in rows
        ]
        try:
            pq.write_table(pa.Table.from_pylist(storage_rows), temporary, compression="zstd")
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        self._validate(target, digest)
        return target

    def read_total_return(
        self,
        ts_code: str,
        algorithm_version: str,
    ) -> list[dict[str, object]]:
        partition = (
            self.dataset
            / f"algorithm_version={algorithm_version}"
            / f"ts_code={ts_code}"
        )
        files = sorted(partition.glob("part-*.parquet"))
        if not files:
            return []
        if len(files) > 1:
            raise ValueError("TOTAL_RETURN_MULTIPLE_ARTIFACTS")
        rows = pq.ParquetFile(files[0]).read().to_pylist()
        rows.sort(key=lambda row: row["trade_date"])
        return rows

    @staticmethod
    def _validate(path: Path, digest: str) -> None:
        try:
            rows = pq.ParquetFile(path).read().to_pylist()
        except (OSError, pa.ArrowException) as error:
            raise ValueError("TOTAL_RETURN_PARQUET_INTEGRITY_ERROR") from error
        canonical = DerivedMarketWarehouse._canonical(rows)
        if path.name != f"part-{digest}.parquet" or hashlib.sha256(canonical).hexdigest() != digest:
            raise ValueError("TOTAL_RETURN_PARQUET_INTEGRITY_ERROR")

    @staticmethod
    def _canonical(rows: list[dict[str, object]]) -> bytes:
        def normalize(value: object) -> object:
            if isinstance(value, Decimal):
                return str(value.normalize())
            if isinstance(value, date):
                return value.isoformat()
            if isinstance(value, (list, tuple)):
                return [normalize(item) for item in value]
            if isinstance(value, dict):
                return {str(key): normalize(item) for key, item in value.items()}
            return value

        return json.dumps(
            normalize(rows),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
