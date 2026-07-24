"""Sequential daily market-data ingestion for a single scheduler."""

import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import Protocol

from hengce.collectors.tushare import DailyFetchResult, TushareDailyCollector
from hengce.raw_store.store import RawObjectStore
from hengce.state.repository import StateRepository
from hengce.warehouse.market import MarketWarehouse


class DailyCollector(Protocol):
    def fetch(self, trade_date: date) -> DailyFetchResult: ...


@dataclass(frozen=True)
class MarketIngestionResult:
    trade_date: str
    bar_count: int
    raw_content_hash: str
    parquet_path: str


class MarketIngestionService:
    """Runs sequentially; StateRepository provides no multi-process locking."""

    def __init__(
        self,
        *,
        collector: TushareDailyCollector | DailyCollector,
        raw_store: RawObjectStore,
        warehouse: MarketWarehouse,
        state: StateRepository,
    ) -> None:
        self.collector = collector
        self.raw_store = raw_store
        self.warehouse = warehouse
        self.state = state

    def run(self, trade_date: date) -> MarketIngestionResult:
        checkpoint_key = f"market_daily:{trade_date.isoformat()}"
        existing = self.state.get_checkpoint(checkpoint_key)
        if existing is not None:
            return self._parse_checkpoint(existing, trade_date)

        fetched = self.collector.fetch(trade_date)
        if not fetched.bars:
            raise ValueError("MARKET_DAILY_EMPTY")

        raw = self.raw_store.put(
            source_id="tushare",
            source_url="http://api.tushare.pro/",
            collected_at=fetched.collected_at,
            content_type=fetched.content_type,
            payload=fetched.raw_payload,
        )
        parquet_path = self.warehouse.write_bars(fetched.bars)
        result = MarketIngestionResult(
            trade_date=trade_date.isoformat(),
            bar_count=len(fetched.bars),
            raw_content_hash=raw.content_hash,
            parquet_path=str(parquet_path),
        )
        self.state.save_checkpoint(
            checkpoint_key,
            json.dumps(asdict(result), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
        return result

    @staticmethod
    def _parse_checkpoint(value: str, trade_date: date) -> MarketIngestionResult:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("MARKET_CHECKPOINT_INVALID: invalid JSON") from error
        if not isinstance(payload, dict) or set(payload) != {
            "trade_date",
            "bar_count",
            "raw_content_hash",
            "parquet_path",
        }:
            raise ValueError("MARKET_CHECKPOINT_INVALID: required fields")
        if (
            not isinstance(payload["trade_date"], str)
            or not isinstance(payload["bar_count"], int)
            or isinstance(payload["bar_count"], bool)
            or not isinstance(payload["raw_content_hash"], str)
            or not isinstance(payload["parquet_path"], str)
        ):
            raise ValueError("MARKET_CHECKPOINT_INVALID: field types")
        if payload["trade_date"] != trade_date.isoformat():
            raise ValueError("MARKET_CHECKPOINT_INVALID: stale trade_date")
        if payload["bar_count"] <= 0:
            raise ValueError("MARKET_CHECKPOINT_INVALID: bar_count")
        if not re.fullmatch(r"[0-9a-f]{64}", payload["raw_content_hash"]):
            raise ValueError("MARKET_CHECKPOINT_INVALID: raw_content_hash")
        if not payload["parquet_path"]:
            raise ValueError("MARKET_CHECKPOINT_INVALID: parquet_path")
        return MarketIngestionResult(**payload)
