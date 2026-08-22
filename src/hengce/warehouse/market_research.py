from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from uuid import uuid4

from hengce.contracts.market_research import FullMarketResearchSnapshot


class FullMarketResearchWarehouse:
    def __init__(self, root: Path) -> None:
        self.dataset = root / "full_market_research"

    def write(self, snapshot: FullMarketResearchSnapshot) -> Path:
        encoded = snapshot.model_dump_json(indent=2).encode("utf-8")
        target = self._partition(snapshot.market_date) / f"snapshot-{snapshot.manifest_hash}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != encoded:
                raise ValueError("FULL_MARKET_RESEARCH_IMMUTABILITY_CONFLICT")
            return target
        temporary = target.parent / f".{uuid4().hex}.tmp"
        try:
            temporary.write_bytes(encoded)
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.link(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def read(self, market_date: date) -> FullMarketResearchSnapshot | None:
        files = sorted(self._partition(market_date).glob("snapshot-*.json"))
        if not files:
            return None
        try:
            snapshots = [
                FullMarketResearchSnapshot.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                for path in files
            ]
        except (OSError, ValueError) as error:
            raise ValueError("FULL_MARKET_RESEARCH_ARTIFACT_INVALID") from error
        if any(
            snapshot.market_date != market_date
            or path.name != f"snapshot-{snapshot.manifest_hash}.json"
            for path, snapshot in zip(files, snapshots, strict=True)
        ):
            raise ValueError("FULL_MARKET_RESEARCH_ARTIFACT_INVALID")
        return max(snapshots, key=lambda item: (item.generated_at, item.manifest_hash))

    def latest(self) -> FullMarketResearchSnapshot | None:
        dates: list[date] = []
        for path in self.dataset.glob("market_date=*"):
            if not path.is_dir() or not list(path.glob("snapshot-*.json")):
                continue
            try:
                dates.append(date.fromisoformat(path.name.removeprefix("market_date=")))
            except ValueError:
                continue
        return self.read(max(dates)) if dates else None

    def _partition(self, market_date: date) -> Path:
        return self.dataset / f"market_date={market_date.isoformat()}"


__all__ = ["FullMarketResearchWarehouse"]
