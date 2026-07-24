from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Event

import pytest

from hengce.collectors.tushare import DailyFetchResult
from hengce.contracts.enums import QualityStatus
from hengce.contracts.market import MarketBar, SecurityMaster
from hengce.raw_store.store import RawObjectStore
from hengce.state.repository import StateRepository
from hengce.warehouse.market import MarketWarehouse

TRADE_DATE = date(2026, 7, 24)
COLLECTED_AT = datetime(2026, 7, 24, 21, 31, tzinfo=UTC)


def _bar() -> MarketBar:
    return MarketBar(
        record_id="600000.SH-20260724-fixture",
        source_id="tushare",
        source_url="http://api.tushare.pro/",
        collected_at=COLLECTED_AT,
        version="daily-20260724-fixture",
        content_hash="b" * 64,
        license_policy="tushare-daily",
        quality_status=QualityStatus.VALID,
        valid_from=COLLECTED_AT,
        ts_code="600000.SH",
        trade_date=TRADE_DATE,
        open=10,
        high=11,
        low=9,
        close=10.5,
        pre_close=10,
        volume=1000,
        amount=10500,
    )


def _security(ts_code: str = "600000.SH") -> SecurityMaster:
    return SecurityMaster(
        ts_code=ts_code,
        symbol=ts_code.split(".")[0],
        name="Fixture Security",
        exchange="SSE" if ts_code.endswith(".SH") else "SZSE",
        board="MAIN_SH" if ts_code.endswith(".SH") else "MAIN_SZ",
        list_date=date(2020, 1, 1),
        is_in_scope=True,
    )


def _seed_security_master(repository: StateRepository, *codes: str) -> None:
    repository.save_security_master_snapshot(
        [_security(code) for code in codes],
        source_id="sse",
        source_url="https://www.sse.com.cn/master.csv",
        collected_at=COLLECTED_AT,
        content_hash="c" * 64,
        version="fixture-v1",
        quality_lineage={"filter": "a_share_cny_four_boards"},
    )


class FakeCollector:
    def __init__(
        self, *, bars: list[MarketBar] | None = None, error: Exception | None = None
    ) -> None:
        self.calls = 0
        self.bars = [_bar()] if bars is None else bars
        self.error = error

    def fetch(self, trade_date: date) -> DailyFetchResult:
        self.calls += 1
        if self.error:
            raise self.error
        return DailyFetchResult(
            raw_payload=b'{"fixture":true}',
            content_type="application/json",
            collected_at=COLLECTED_AT,
            bars=self.bars,
        )


class BlockingCollector(FakeCollector):
    def __init__(self) -> None:
        super().__init__()
        self.fetch_started = Event()
        self.allow_return = Event()

    def fetch(self, trade_date: date) -> DailyFetchResult:
        self.fetch_started.set()
        if not self.allow_return.wait(timeout=5):
            raise TimeoutError("test collector was not released")
        return super().fetch(trade_date)


class CountingRawStore(RawObjectStore):
    def __init__(self, root: Path, *, error: Exception | None = None) -> None:
        super().__init__(root)
        self.calls = 0
        self.error = error

    def put(self, **kwargs: object):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.error:
            raise self.error
        return super().put(**kwargs)  # type: ignore[arg-type]


class CountingWarehouse(MarketWarehouse):
    def __init__(self, root: Path, *, error: Exception | None = None) -> None:
        super().__init__(root)
        self.calls = 0
        self.error = error

    def write_bars(self, bars: list[MarketBar]) -> Path:
        self.calls += 1
        if self.error:
            raise self.error
        return super().write_bars(bars)


def _service(
    tmp_path: Path,
    *,
    collector: FakeCollector | None = None,
    raw_store: CountingRawStore | None = None,
    warehouse: CountingWarehouse | None = None,
):
    from hengce.services.market_ingestion import MarketIngestionService

    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    _seed_security_master(repository, "600000.SH")
    return (
        MarketIngestionService(
            collector=collector or FakeCollector(),
            raw_store=raw_store or CountingRawStore(tmp_path / "raw"),
            warehouse=warehouse or CountingWarehouse(tmp_path / "normalized"),
            state=repository,
        ),
        repository,
    )


def test_missing_security_master_blocks_before_collector_or_artifacts(tmp_path: Path) -> None:
    collector = FakeCollector()
    raw_store = CountingRawStore(tmp_path / "raw")
    warehouse = CountingWarehouse(tmp_path / "normalized")
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    from hengce.services.market_ingestion import MarketIngestionService

    service = MarketIngestionService(
        collector=collector, raw_store=raw_store, warehouse=warehouse, state=repository
    )

    with pytest.raises(ValueError, match="SECURITY_MASTER_UNAVAILABLE"):
        service.run(TRADE_DATE)

    assert collector.calls == 0
    assert raw_store.calls == 0
    assert warehouse.calls == 0
    assert repository.get_checkpoint(f"market_daily:{TRADE_DATE.isoformat()}") is None
    assert repository.list_runs(run_type="market_daily")[-1].run_status == "BLOCKED"


def test_duplicate_daily_codes_fail_before_parquet_publication(tmp_path: Path) -> None:
    duplicate = _bar().model_copy(update={"record_id": "600000.SH-duplicate"})
    collector = FakeCollector(bars=[_bar(), duplicate])
    raw_store = CountingRawStore(tmp_path / "raw")
    warehouse = CountingWarehouse(tmp_path / "normalized")
    service, repository = _service(
        tmp_path, collector=collector, raw_store=raw_store, warehouse=warehouse
    )

    with pytest.raises(ValueError, match="MARKET_DAILY_DUPLICATE_TS_CODE"):
        service.run(TRADE_DATE)

    assert raw_store.calls == 1
    assert warehouse.calls == 0
    assert repository.get_checkpoint(f"market_daily:{TRADE_DATE.isoformat()}") is None
    failed_run = repository.list_runs(run_type="market_daily")[-1]
    assert failed_run.run_status == "FAILED"
    assert failed_run.error_code == "MARKET_DAILY_DUPLICATE_TS_CODE"


def test_out_of_scope_bar_fails_before_parquet_publication(tmp_path: Path) -> None:
    collector = FakeCollector(bars=[_bar().model_copy(update={"ts_code": "000001.SZ"})])
    raw_store = CountingRawStore(tmp_path / "raw")
    warehouse = CountingWarehouse(tmp_path / "normalized")
    service, repository = _service(
        tmp_path, collector=collector, raw_store=raw_store, warehouse=warehouse
    )

    with pytest.raises(ValueError, match="MARKET_DAILY_OUT_OF_SCOPE_TS_CODE"):
        service.run(TRADE_DATE)

    assert raw_store.calls == 1
    assert warehouse.calls == 0
    assert repository.get_checkpoint(f"market_daily:{TRADE_DATE.isoformat()}") is None


def test_repeated_run_uses_checkpoint_without_second_fetch_or_writes(tmp_path: Path) -> None:
    collector = FakeCollector()
    raw_store = CountingRawStore(tmp_path / "raw")
    warehouse = CountingWarehouse(tmp_path / "normalized")
    service, _ = _service(
        tmp_path, collector=collector, raw_store=raw_store, warehouse=warehouse
    )

    first = service.run(TRADE_DATE)
    second = service.run(TRADE_DATE)

    assert first == second
    assert first.bar_count == 1
    assert collector.calls == 1
    assert raw_store.calls == 1
    assert warehouse.calls == 1


@pytest.mark.parametrize("failure", ["collector", "raw", "warehouse"])
def test_failure_never_creates_checkpoint_and_retry_can_complete(
    tmp_path: Path, failure: str
) -> None:
    collector = FakeCollector(
        error=RuntimeError("collector failed") if failure == "collector" else None
    )
    raw_store = CountingRawStore(
        tmp_path / "raw", error=RuntimeError("raw failed") if failure == "raw" else None
    )
    warehouse = CountingWarehouse(
        tmp_path / "normalized",
        error=RuntimeError("warehouse failed") if failure == "warehouse" else None,
    )
    service, repository = _service(
        tmp_path, collector=collector, raw_store=raw_store, warehouse=warehouse
    )

    with pytest.raises(RuntimeError, match=f"{failure} failed"):
        service.run(TRADE_DATE)

    key = f"market_daily:{TRADE_DATE.isoformat()}"
    assert repository.get_checkpoint(key) is None
    if failure == "warehouse":
        assert list((tmp_path / "raw").rglob("payload.bin"))

    collector.error = None
    raw_store.error = None
    warehouse.error = None
    result = service.run(TRADE_DATE)

    assert result.bar_count == 1
    assert repository.get_checkpoint(key) is not None


def test_empty_bars_are_rejected_without_output_or_checkpoint(tmp_path: Path) -> None:
    collector = FakeCollector(bars=[])
    raw_store = CountingRawStore(tmp_path / "raw")
    warehouse = CountingWarehouse(tmp_path / "normalized")
    service, repository = _service(
        tmp_path, collector=collector, raw_store=raw_store, warehouse=warehouse
    )

    with pytest.raises(ValueError, match="MARKET_DAILY_EMPTY"):
        service.run(TRADE_DATE)

    assert raw_store.calls == 0
    assert warehouse.calls == 0
    assert repository.get_checkpoint(f"market_daily:{TRADE_DATE.isoformat()}") is None


@pytest.mark.parametrize(
    "checkpoint",
    [
        "not json",
        '{"bar_count": 1}',
        ('{"bar_count":"1","trade_date":"2026-07-24",'
         '"raw_content_hash":"a","parquet_path":"x"}'),
    ],
)
def test_malformed_checkpoint_is_rejected_without_refetch(tmp_path: Path, checkpoint: str) -> None:
    collector = FakeCollector()
    service, repository = _service(tmp_path, collector=collector)
    repository.save_checkpoint(f"market_daily:{TRADE_DATE.isoformat()}", checkpoint)

    with pytest.raises(ValueError, match="MARKET_CHECKPOINT_INVALID"):
        service.run(TRADE_DATE)

    assert collector.calls == 0


def test_stale_checkpoint_is_rejected_without_refetch(tmp_path: Path) -> None:
    collector = FakeCollector()
    service, repository = _service(tmp_path, collector=collector)
    repository.save_checkpoint(
        f"market_daily:{TRADE_DATE.isoformat()}",
        (
            '{"bar_count":1,"parquet_path":"normalized/part.parquet",'
            f'"raw_content_hash":"{"a" * 64}","trade_date":"2026-07-23"}}'
        ),
    )

    with pytest.raises(ValueError, match="MARKET_CHECKPOINT_INVALID: stale trade_date"):
        service.run(TRADE_DATE)

    assert collector.calls == 0


def test_concurrent_same_date_runs_allow_only_one_collector_fetch(tmp_path: Path) -> None:
    collector = BlockingCollector()
    raw_store = CountingRawStore(tmp_path / "raw")
    warehouse = CountingWarehouse(tmp_path / "normalized")
    first, _ = _service(
        tmp_path, collector=collector, raw_store=raw_store, warehouse=warehouse
    )
    second, second_state = _service(
        tmp_path, collector=collector, raw_store=raw_store, warehouse=warehouse
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_run = executor.submit(first.run, TRADE_DATE)
        assert collector.fetch_started.wait(timeout=5)
        with pytest.raises(RuntimeError, match="MARKET_INGESTION_IN_PROGRESS"):
            second.run(TRADE_DATE)
        collector.allow_return.set()
        assert first_run.result(timeout=5).bar_count == 1

    assert collector.calls == 1
    blocked = second_state.list_runs(run_type="market_daily")
    assert any(run.run_status == "BLOCKED" for run in blocked)


def test_retry_recovers_staged_parquet_after_checkpoint_crash_without_refetch(
    tmp_path: Path,
) -> None:
    collector = FakeCollector()
    raw_store = CountingRawStore(tmp_path / "raw")
    warehouse = CountingWarehouse(tmp_path / "normalized")
    service, repository = _service(
        tmp_path, collector=collector, raw_store=raw_store, warehouse=warehouse
    )
    service.after_artifact_staged = lambda: (_ for _ in ()).throw(RuntimeError("crash"))

    with pytest.raises(RuntimeError, match="crash"):
        service.run(TRADE_DATE)

    assert repository.get_checkpoint(f"market_daily:{TRADE_DATE.isoformat()}") is None
    assert collector.calls == 1
    assert warehouse.calls == 1

    recovered = service.run(TRADE_DATE)

    assert recovered.bar_count == 1
    assert collector.calls == 1
    assert warehouse.calls == 1
    assert repository.get_checkpoint(f"market_daily:{TRADE_DATE.isoformat()}") is not None


@pytest.mark.parametrize(
    "artifact", ["raw_missing", "raw_corrupt", "parquet_missing", "parquet_corrupt"]
)
def test_checkpoint_with_invalid_artifact_is_rejected_without_refetch(
    tmp_path: Path, artifact: str
) -> None:
    collector = FakeCollector()
    raw_store = CountingRawStore(tmp_path / "raw")
    warehouse = CountingWarehouse(tmp_path / "normalized")
    service, repository = _service(
        tmp_path, collector=collector, raw_store=raw_store, warehouse=warehouse
    )
    result = service.run(TRADE_DATE)
    raw_path = raw_store.validate_content_hash(result.raw_content_hash)
    parquet_path = Path(result.parquet_path)
    if artifact == "raw_missing":
        raw_path.unlink()
    elif artifact == "raw_corrupt":
        raw_path.write_bytes(b"corrupt")
    elif artifact == "parquet_missing":
        parquet_path.unlink()
    else:
        parquet_path.write_bytes(b"corrupt")

    with pytest.raises(ValueError, match="MARKET_CHECKPOINT_ARTIFACT_INVALID"):
        service.run(TRADE_DATE)

    assert collector.calls == 1
    assert repository.list_runs(run_type="market_daily")[-1].run_status == "FAILED"


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda result: {**result, "bar_count": 2}, "MARKET_CHECKPOINT_ARTIFACT_INVALID"),
        (lambda result: {**result, "trade_date": "2026-07-23"}, "MARKET_CHECKPOINT_INVALID"),
    ],
)
def test_checkpoint_requires_exact_expected_date_and_count(
    tmp_path: Path, mutate: object, error: str
) -> None:
    collector = FakeCollector()
    service, repository = _service(tmp_path, collector=collector)
    service.run(TRADE_DATE)
    checkpoint = repository.get_checkpoint(f"market_daily:{TRADE_DATE.isoformat()}")
    payload = json.loads(checkpoint or "{}")
    repository.save_checkpoint(
        f"market_daily:{TRADE_DATE.isoformat()}",
        json.dumps(mutate(payload)),  # type: ignore[operator]
    )

    with pytest.raises(ValueError, match=error):
        service.run(TRADE_DATE)

    assert collector.calls == 1


def test_retry_discards_missing_staged_artifact_then_refetches(tmp_path: Path) -> None:
    collector = FakeCollector()
    raw_store = CountingRawStore(tmp_path / "raw")
    warehouse = CountingWarehouse(tmp_path / "normalized")
    service, _ = _service(
        tmp_path, collector=collector, raw_store=raw_store, warehouse=warehouse
    )
    service.after_artifact_staged = lambda: (_ for _ in ()).throw(RuntimeError("crash"))

    with pytest.raises(RuntimeError, match="crash"):
        service.run(TRADE_DATE)
    parquet_file = next((tmp_path / "normalized").rglob("*.parquet"))
    parquet_file.unlink()
    service.after_artifact_staged = None

    result = service.run(TRADE_DATE)

    assert result.bar_count == 1
    assert collector.calls == 2
    assert warehouse.calls == 2
