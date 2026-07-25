from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from threading import Event

import pytest

from hengce.collectors.tushare import DailyFetchResult
from hengce.contracts.enums import QualityStatus, RunStatus
from hengce.contracts.market import MarketBar, SecurityMaster
from hengce.contracts.policy import SourcePolicy
from hengce.raw_store.store import RawObjectStore
from hengce.services.initializer import HistoricalInitializer
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


def _seed_security_master_source(
    repository: StateRepository,
    *,
    source_id: str,
    codes: tuple[str, ...],
) -> None:
    source_urls = {
        "sse": "https://www.sse.com.cn/master.csv",
        "szse": "https://www.szse.cn/master.csv",
    }
    policies = json.loads(
        files("hengce").joinpath("data", "source_policies.json").read_text(encoding="utf-8")
    )
    repository.upsert_policy(
        SourcePolicy.model_validate(
            next(item for item in policies if item["source_id"] == source_id)
        )
    )
    repository.save_security_master_snapshot(
        [_security(code) for code in codes],
        source_id=source_id,
        source_url=source_urls[source_id],
        collected_at=COLLECTED_AT,
        content_hash=("c" if source_id == "sse" else "d") * 64,
        version=f"{source_id}-fixture-v1",
        quality_lineage={"filter": "a_share_cny_four_boards"},
    )


def _seed_complete_security_universe(
    repository: StateRepository,
    *,
    sse_codes: tuple[str, ...] = ("600000.SH",),
    szse_codes: tuple[str, ...] = ("000001.SZ",),
) -> None:
    _seed_security_master_source(repository, source_id="sse", codes=sse_codes)
    _seed_security_master_source(repository, source_id="szse", codes=szse_codes)


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


class HardCrash(BaseException):
    """Simulate process death without entering the service's Exception handler."""


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
    _seed_complete_security_universe(repository)
    return (
        MarketIngestionService(
            collector=collector or FakeCollector(),
            raw_store=raw_store or CountingRawStore(tmp_path / "raw"),
            warehouse=warehouse or CountingWarehouse(tmp_path / "normalized"),
            state=repository,
        ),
        repository,
    )


@pytest.mark.parametrize(
    ("missing_source", "error_code"),
    [
        ("sse", "SECURITY_MASTER_SSE_UNAVAILABLE"),
        ("szse", "SECURITY_MASTER_SZSE_UNAVAILABLE"),
    ],
)
def test_incomplete_universe_blocks_before_tushare_fetch(
    tmp_path: Path, missing_source: str, error_code: str
) -> None:
    collector = FakeCollector()
    raw_store = CountingRawStore(tmp_path / "raw")
    warehouse = CountingWarehouse(tmp_path / "normalized")
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    available_source = "szse" if missing_source == "sse" else "sse"
    available_codes = ("000001.SZ",) if available_source == "szse" else ("600000.SH",)
    _seed_security_master_source(
        repository,
        source_id=available_source,
        codes=available_codes,
    )
    from hengce.services.market_ingestion import MarketIngestionService

    service = MarketIngestionService(
        collector=collector, raw_store=raw_store, warehouse=warehouse, state=repository
    )

    with pytest.raises(ValueError, match=error_code):
        service.run(date(2026, 7, 22))

    assert collector.calls == 0
    assert raw_store.calls == 0
    assert warehouse.calls == 0
    assert repository.get_checkpoint("market_daily:2026-07-22") is None
    latest_run = repository.list_runs(run_type="market_daily")[-1]
    assert latest_run.run_status == RunStatus.BLOCKED
    assert latest_run.error_code == error_code


def test_missing_security_master_hard_crash_before_terminal_write_leaves_no_running_run(
    tmp_path: Path,
) -> None:
    from hengce.services.market_ingestion import MarketIngestionService

    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    service = MarketIngestionService(
        collector=FakeCollector(),
        raw_store=CountingRawStore(tmp_path / "raw"),
        warehouse=CountingWarehouse(tmp_path / "normalized"),
        state=repository,
        owner_id_factory=lambda: "missing-master",
        before_nonlease_terminal_recorded=lambda: (_ for _ in ()).throw(HardCrash()),
    )

    with pytest.raises(HardCrash):
        service.run(TRADE_DATE)

    assert repository.list_runs(run_type="market_daily") == []


def test_disabled_security_master_policy_blocks_existing_snapshot_before_fetch(
    tmp_path: Path,
) -> None:
    collector = FakeCollector()
    raw_store = CountingRawStore(tmp_path / "raw")
    warehouse = CountingWarehouse(tmp_path / "normalized")
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    _seed_complete_security_universe(repository)
    policy = repository.get_policy("sse")
    assert policy is not None
    repository.upsert_policy(policy.model_copy(update={"enabled": False}))
    from hengce.services.market_ingestion import MarketIngestionService

    service = MarketIngestionService(
        collector=collector,
        raw_store=raw_store,
        warehouse=warehouse,
        state=repository,
    )

    with pytest.raises(ValueError, match="SECURITY_MASTER_POLICY_DENIED"):
        service.run(TRADE_DATE)

    assert collector.calls == 0
    assert repository.count_refusals() == 1
    blocked_run = repository.list_runs(run_type="market_daily")[-1]
    assert blocked_run.run_status == RunStatus.BLOCKED
    assert blocked_run.error_code == "SECURITY_MASTER_POLICY_DENIED"


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


@pytest.mark.parametrize("ts_code", ["000002.SZ", "430047.BJ", "900901.SH", "00700.HK"])
def test_out_of_scope_bar_fails_before_parquet_publication(
    tmp_path: Path, ts_code: str
) -> None:
    collector = FakeCollector(bars=[_bar().model_copy(update={"ts_code": ts_code})])
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


def test_complete_universe_persists_sh_and_sz_bars(tmp_path: Path) -> None:
    sz_bar = _bar().model_copy(
        update={
            "record_id": "000001.SZ-20260724-fixture",
            "ts_code": "000001.SZ",
        }
    )
    collector = FakeCollector(bars=[_bar(), sz_bar])
    warehouse = CountingWarehouse(tmp_path / "normalized")
    service, _ = _service(tmp_path, collector=collector, warehouse=warehouse)

    result = service.run(TRADE_DATE)

    assert collector.calls == 1
    assert result.bar_count == 2
    assert [row["ts_code"] for row in warehouse.read_bars(TRADE_DATE)] == [
        "000001.SZ",
        "600000.SH",
    ]


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


def test_history_revalidates_completed_day_artifacts_without_refetch(
    tmp_path: Path,
) -> None:
    collector = FakeCollector()
    service, repository = _service(tmp_path, collector=collector)
    published = service.run(TRADE_DATE)
    repository.save_checkpoint(HistoricalInitializer.checkpoint_key, TRADE_DATE.isoformat())
    initializer = HistoricalInitializer(ingestion=service, state=repository)

    result = initializer.run([TRADE_DATE])

    assert result.completed_dates == 1
    assert collector.calls == 1

    Path(published.parquet_path).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="MARKET_CHECKPOINT_ARTIFACT_INVALID"):
        initializer.run([TRADE_DATE])
    assert collector.calls == 1


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


def test_lease_contention_hard_crash_before_terminal_write_leaves_no_blocked_running_run(
    tmp_path: Path,
) -> None:
    from hengce.contracts.run import RunRecord
    from hengce.services.market_ingestion import MarketIngestionService

    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    _seed_complete_security_universe(repository)
    started = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    active = RunRecord(
        run_id=f"market_daily:{TRADE_DATE.isoformat()}:active",
        trade_date=TRADE_DATE,
        run_type="market_daily",
        started_at=started,
        run_status="RUNNING",
        stage_statuses={"checkpoint": "RUNNING"},
    )
    repository.record_run(active)
    repository.acquire_ingestion_lease(
        TRADE_DATE,
        owner_id="active",
        run_id=active.run_id,
        now=started,
        lease_seconds=60,
    )
    contender = MarketIngestionService(
        collector=FakeCollector(),
        raw_store=CountingRawStore(tmp_path / "raw"),
        warehouse=CountingWarehouse(tmp_path / "normalized"),
        state=repository,
        clock=lambda: started,
        owner_id_factory=lambda: "contender",
        before_nonlease_terminal_recorded=lambda: (_ for _ in ()).throw(HardCrash()),
    )

    with pytest.raises(HardCrash):
        contender.run(TRADE_DATE)

    runs = {run.run_id: run for run in repository.list_runs(run_type="market_daily")}
    assert f"market_daily:{TRADE_DATE.isoformat()}:contender" not in runs
    assert runs[active.run_id].run_status == "RUNNING"


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


def test_expired_takeover_recovers_prepublication_intent_after_hard_crash(
    tmp_path: Path,
) -> None:
    from hengce.services.market_ingestion import MarketIngestionService

    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    _seed_complete_security_universe(repository)
    raw_store = CountingRawStore(tmp_path / "raw")
    warehouse = CountingWarehouse(tmp_path / "normalized")
    first_collector = FakeCollector()
    second_collector = FakeCollector(
        bars=[_bar().model_copy(update={"close": Decimal("10.75")})]
    )
    started = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    first = MarketIngestionService(
        collector=first_collector,
        raw_store=raw_store,
        warehouse=warehouse,
        state=repository,
        clock=lambda: started,
        owner_id_factory=lambda: "first-owner",
        lease_seconds=60,
        after_parquet_published=lambda: (_ for _ in ()).throw(HardCrash()),
    )

    with pytest.raises(HardCrash):
        first.run(TRADE_DATE)

    abandoned = repository.list_runs(run_type="market_daily")
    assert len(abandoned) == 1
    assert abandoned[0].run_status == "RUNNING"
    assert repository.get_checkpoint(f"market_daily:{TRADE_DATE.isoformat()}") is None

    second = MarketIngestionService(
        collector=second_collector,
        raw_store=raw_store,
        warehouse=warehouse,
        state=repository,
        clock=lambda: started.replace(minute=2),
        owner_id_factory=lambda: "second-owner",
        lease_seconds=60,
    )
    recovered = second.run(TRADE_DATE)

    assert recovered.bar_count == 1
    assert first_collector.calls == 1
    assert second_collector.calls == 0
    assert warehouse.calls == 1
    assert len(list((tmp_path / "normalized").rglob("part-*.parquet"))) == 1
    runs = {run.run_id: run for run in repository.list_runs(run_type="market_daily")}
    assert runs[f"market_daily:{TRADE_DATE.isoformat()}:first-owner"].run_status == "FAILED"
    assert (
        runs[f"market_daily:{TRADE_DATE.isoformat()}:first-owner"].error_code
        == "MARKET_INGESTION_LEASE_EXPIRED"
    )
    assert runs[f"market_daily:{TRADE_DATE.isoformat()}:second-owner"].run_status == "SUCCEEDED"
    assert all(run.run_status != "RUNNING" for run in runs.values())


def test_stale_owner_cannot_publish_checkpoint_after_replacement_stages_new_identity(
    tmp_path: Path,
) -> None:
    from hengce.contracts.run import RunRecord
    from hengce.services.market_ingestion import MarketIngestionService

    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    _seed_complete_security_universe(repository)
    started = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    replacement = RunRecord(
        run_id=f"market_daily:{TRADE_DATE.isoformat()}:replacement",
        trade_date=TRADE_DATE,
        run_type="market_daily",
        started_at=started.replace(minute=2),
        run_status="RUNNING",
        stage_statuses={"checkpoint": "RUNNING"},
    )

    def replace_owner_after_old_parquet_publication() -> None:
        repository.record_run(replacement)
        assert repository.acquire_ingestion_lease(
            TRADE_DATE,
            owner_id="replacement",
            run_id=replacement.run_id,
            now=started.replace(minute=2),
            lease_seconds=60,
        ).acquired
        staged_b = {
            "raw_payload_path": "raw-b",
            "result": {
                "trade_date": TRADE_DATE.isoformat(),
                "bar_count": 1,
                "raw_content_hash": "d" * 64,
                "parquet_path": "part-b.parquet",
                "parquet_content_hash": "e" * 64,
            },
        }
        assert repository.stage_ingestion_artifact(
            TRADE_DATE,
            owner_id="replacement",
            staged_result_json=json.dumps(staged_b, sort_keys=True),
        )

    service = MarketIngestionService(
        collector=FakeCollector(),
        raw_store=CountingRawStore(tmp_path / "raw"),
        warehouse=CountingWarehouse(tmp_path / "normalized"),
        state=repository,
        clock=lambda: started,
        owner_id_factory=lambda: "stale",
        lease_seconds=60,
        after_artifact_staged=replace_owner_after_old_parquet_publication,
    )

    with pytest.raises(RuntimeError, match="MARKET_INGESTION_LEASE_LOST"):
        service.run(TRADE_DATE)

    assert repository.get_checkpoint(f"market_daily:{TRADE_DATE.isoformat()}") is None
    lease = repository.get_ingestion_state(TRADE_DATE)
    runs = {run.run_id: run for run in repository.list_runs(run_type="market_daily")}
    assert lease is not None
    assert lease.owner_id == "replacement"
    assert lease.active_run_id == replacement.run_id
    assert runs[replacement.run_id].run_status == "RUNNING"


def test_checkpoint_fast_path_does_not_reconcile_mismatched_replacement_intent(
    tmp_path: Path,
) -> None:
    from hengce.contracts.run import RunRecord

    service, repository = _service(tmp_path)
    service.run(TRADE_DATE)
    replacement = RunRecord(
        run_id=f"market_daily:{TRADE_DATE.isoformat()}:replacement",
        trade_date=TRADE_DATE,
        run_type="market_daily",
        started_at=COLLECTED_AT,
        run_status="RUNNING",
        stage_statuses={"checkpoint": "RUNNING"},
    )
    repository.record_run(replacement)
    assert repository.acquire_ingestion_lease(
        TRADE_DATE,
        owner_id="replacement",
        run_id=replacement.run_id,
        now=COLLECTED_AT,
        lease_seconds=60,
    ).acquired
    staged_b = {
        "raw_payload_path": "raw-b",
        "result": {
            "trade_date": TRADE_DATE.isoformat(),
            "bar_count": 1,
            "raw_content_hash": "d" * 64,
            "parquet_path": "part-b.parquet",
            "parquet_content_hash": "e" * 64,
        },
    }
    assert repository.stage_ingestion_artifact(
        TRADE_DATE,
        owner_id="replacement",
        staged_result_json=json.dumps(staged_b, sort_keys=True),
    )

    service.run(TRADE_DATE)

    lease = repository.get_ingestion_state(TRADE_DATE)
    runs = {run.run_id: run for run in repository.list_runs(run_type="market_daily")}
    assert lease is not None
    assert lease.owner_id == "replacement"
    assert lease.active_run_id == replacement.run_id
    assert runs[replacement.run_id].run_status == "RUNNING"


def test_checkpoint_fast_path_hard_crash_before_terminal_write_leaves_no_running_run(
    tmp_path: Path,
) -> None:
    from hengce.services.market_ingestion import MarketIngestionService

    service, repository = _service(tmp_path)
    service.run(TRADE_DATE)
    fast = MarketIngestionService(
        collector=FakeCollector(),
        raw_store=CountingRawStore(tmp_path / "raw"),
        warehouse=CountingWarehouse(tmp_path / "normalized"),
        state=repository,
        owner_id_factory=lambda: "fast-crash",
        before_nonlease_terminal_recorded=lambda: (_ for _ in ()).throw(HardCrash()),
    )

    with pytest.raises(HardCrash):
        fast.run(TRADE_DATE)

    run_ids = {run.run_id for run in repository.list_runs(run_type="market_daily")}
    assert f"market_daily:{TRADE_DATE.isoformat()}:fast-crash" not in run_ids


def test_acquired_running_run_exists_before_post_acquire_hard_crash_and_takeover(
    tmp_path: Path,
) -> None:
    from hengce.services.market_ingestion import MarketIngestionService

    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    _seed_complete_security_universe(repository)
    started = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    first = MarketIngestionService(
        collector=FakeCollector(),
        raw_store=CountingRawStore(tmp_path / "raw"),
        warehouse=CountingWarehouse(tmp_path / "normalized"),
        state=repository,
        clock=lambda: started,
        owner_id_factory=lambda: "first",
        lease_seconds=60,
        after_lease_acquired=lambda: (_ for _ in ()).throw(HardCrash()),
    )

    with pytest.raises(HardCrash):
        first.run(TRADE_DATE)

    first_run_id = f"market_daily:{TRADE_DATE.isoformat()}:first"
    lease = repository.get_ingestion_state(TRADE_DATE)
    runs = {run.run_id: run for run in repository.list_runs(run_type="market_daily")}
    assert lease is not None
    assert lease.active_run_id == first_run_id
    assert runs[first_run_id].run_status == "RUNNING"

    second = MarketIngestionService(
        collector=FakeCollector(),
        raw_store=CountingRawStore(tmp_path / "raw"),
        warehouse=CountingWarehouse(tmp_path / "normalized"),
        state=repository,
        clock=lambda: started.replace(minute=2),
        owner_id_factory=lambda: "second",
        lease_seconds=60,
    )
    second.run(TRADE_DATE)

    runs = {run.run_id: run for run in repository.list_runs(run_type="market_daily")}
    assert runs[first_run_id].run_status == "FAILED"
    assert runs[f"market_daily:{TRADE_DATE.isoformat()}:second"].run_status == "SUCCEEDED"


def test_post_atomic_checkpoint_hook_crash_leaves_publisher_terminal(
    tmp_path: Path,
) -> None:
    from hengce.services.market_ingestion import MarketIngestionService

    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    _seed_complete_security_universe(repository)
    collector = FakeCollector()
    raw_store = CountingRawStore(tmp_path / "raw")
    warehouse = CountingWarehouse(tmp_path / "normalized")
    first = MarketIngestionService(
        collector=collector,
        raw_store=raw_store,
        warehouse=warehouse,
        state=repository,
        owner_id_factory=lambda: "first-owner",
        after_checkpoint_published=lambda: (_ for _ in ()).throw(HardCrash()),
    )

    with pytest.raises(HardCrash):
        first.run(TRADE_DATE)

    assert repository.get_checkpoint(f"market_daily:{TRADE_DATE.isoformat()}") is not None
    assert repository.list_runs(run_type="market_daily")[0].run_status == "SUCCEEDED"

    second = MarketIngestionService(
        collector=collector,
        raw_store=raw_store,
        warehouse=warehouse,
        state=repository,
        owner_id_factory=lambda: "second-owner",
    )
    second.run(TRADE_DATE)

    runs = {run.run_id: run for run in repository.list_runs(run_type="market_daily")}
    assert runs[f"market_daily:{TRADE_DATE.isoformat()}:first-owner"].run_status == "SUCCEEDED"
    assert runs[f"market_daily:{TRADE_DATE.isoformat()}:second-owner"].run_status == "SUCCEEDED"
    assert all(run.run_status != "RUNNING" for run in runs.values())
    lease = repository.get_ingestion_state(TRADE_DATE)
    assert lease is not None
    assert lease.owner_id is None
    assert lease.active_run_id is None
    assert collector.calls == 1


def test_hard_crash_after_atomic_finalize_cannot_leave_running_record(
    tmp_path: Path,
) -> None:
    from hengce.services.market_ingestion import MarketIngestionService

    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    _seed_complete_security_universe(repository)
    service = MarketIngestionService(
        collector=FakeCollector(),
        raw_store=CountingRawStore(tmp_path / "raw"),
        warehouse=CountingWarehouse(tmp_path / "normalized"),
        state=repository,
        owner_id_factory=lambda: "owner",
        after_ingestion_finalized=lambda: (_ for _ in ()).throw(HardCrash()),
    )

    with pytest.raises(HardCrash):
        service.run(TRADE_DATE)

    run = repository.list_runs(run_type="market_daily")[0]
    lease = repository.get_ingestion_state(TRADE_DATE)
    assert run.run_status == "SUCCEEDED"
    assert run.finished_at is not None
    assert lease is not None
    assert lease.lifecycle_state == "SUCCEEDED"
    assert lease.owner_id is None
    assert lease.active_run_id is None


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_lifecycle"),
    [
        (RuntimeError("collector failed"), "FAILED", "FAILED"),
        (RuntimeError("SECURITY_MASTER_UNAVAILABLE"), "BLOCKED", "BLOCKED"),
    ],
)
def test_failure_or_block_is_atomic_before_post_finalize_hard_crash(
    tmp_path: Path,
    error: Exception,
    expected_status: str,
    expected_lifecycle: str,
) -> None:
    from hengce.services.market_ingestion import MarketIngestionService

    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    _seed_complete_security_universe(repository)
    service = MarketIngestionService(
        collector=FakeCollector(error=error),
        raw_store=CountingRawStore(tmp_path / "raw"),
        warehouse=CountingWarehouse(tmp_path / "normalized"),
        state=repository,
        owner_id_factory=lambda: "owner",
        after_ingestion_finalized=lambda: (_ for _ in ()).throw(HardCrash()),
    )

    with pytest.raises(HardCrash):
        service.run(TRADE_DATE)

    run = repository.list_runs(run_type="market_daily")[0]
    lease = repository.get_ingestion_state(TRADE_DATE)
    assert run.run_status == expected_status
    assert run.finished_at is not None
    assert lease is not None
    assert lease.lifecycle_state == expected_lifecycle
    assert lease.owner_id is None
    assert lease.active_run_id is None


def test_legacy_checkpoint_recovers_global_raw_object_without_refetch(tmp_path: Path) -> None:
    collector = FakeCollector()
    raw_store = CountingRawStore(tmp_path / "raw")
    service, repository = _service(tmp_path, collector=collector, raw_store=raw_store)
    result = service.run(TRADE_DATE)
    global_payload = raw_store.validate_content_hash(result.raw_content_hash)
    legacy = (
        tmp_path
        / "raw"
        / "tushare"
        / "2026"
        / "07"
        / "24"
        / result.raw_content_hash
        / "payload.bin"
    )
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(global_payload.read_bytes())
    global_payload.unlink()
    checkpoint = repository.get_checkpoint(f"market_daily:{TRADE_DATE.isoformat()}")
    checkpoint_payload = json.loads(checkpoint or "{}")
    checkpoint_payload.pop("parquet_content_hash")
    repository.save_checkpoint(
        f"market_daily:{TRADE_DATE.isoformat()}", json.dumps(checkpoint_payload)
    )

    recovered = service.run(TRADE_DATE)

    assert recovered.raw_content_hash == result.raw_content_hash
    assert raw_store.validate_content_hash(result.raw_content_hash).is_file()
    rewritten = json.loads(
        repository.get_checkpoint(f"market_daily:{TRADE_DATE.isoformat()}") or "{}"
    )
    assert rewritten["parquet_content_hash"] == recovered.parquet_content_hash
    assert collector.calls == 1


def test_staged_legacy_raw_object_recovers_without_refetch(tmp_path: Path) -> None:
    collector = FakeCollector()
    raw_store = CountingRawStore(tmp_path / "raw")
    service, repository = _service(tmp_path, collector=collector, raw_store=raw_store)

    def stage_legacy_result_then_crash() -> None:
        lease = repository.get_ingestion_state(TRADE_DATE)
        assert lease is not None
        staged = json.loads(lease.staged_result_json or "{}")
        staged["result"].pop("parquet_content_hash")
        assert repository.stage_ingestion_artifact(
            TRADE_DATE,
            owner_id=lease.owner_id or "",
            staged_result_json=json.dumps(staged),
        )
        persisted = repository.get_ingestion_state(TRADE_DATE)
        assert persisted is not None
        persisted_result = json.loads(persisted.staged_result_json or "{}")["result"]
        assert "parquet_content_hash" not in persisted_result
        raise RuntimeError("crash")

    service.after_artifact_staged = stage_legacy_result_then_crash

    with pytest.raises(RuntimeError, match="crash"):
        service.run(TRADE_DATE)
    content_hash = next((tmp_path / "raw" / "objects").iterdir()).name
    global_payload = raw_store.validate_content_hash(content_hash)
    legacy = tmp_path / "raw" / "tushare" / "2026" / "07" / "24" / content_hash / "payload.bin"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(global_payload.read_bytes())
    global_payload.unlink()
    service.after_artifact_staged = None

    recovered = service.run(TRADE_DATE)

    assert recovered.raw_content_hash == content_hash
    assert raw_store.validate_content_hash(content_hash).is_file()
    assert collector.calls == 1
    rewritten = json.loads(
        repository.get_checkpoint(f"market_daily:{TRADE_DATE.isoformat()}") or "{}"
    )
    assert rewritten["parquet_content_hash"] == recovered.parquet_content_hash


def test_checkpoint_rejects_parseable_parquet_with_changed_content(tmp_path: Path) -> None:
    collector = FakeCollector()
    warehouse = CountingWarehouse(tmp_path / "normalized")
    service, repository = _service(tmp_path, collector=collector, warehouse=warehouse)
    service.run(TRADE_DATE)
    changed = warehouse.write_bars([_bar().model_copy(update={"close": Decimal("11")})])
    checkpoint = repository.get_checkpoint(f"market_daily:{TRADE_DATE.isoformat()}")
    payload = json.loads(checkpoint or "{}")
    payload["parquet_path"] = str(changed)
    repository.save_checkpoint(f"market_daily:{TRADE_DATE.isoformat()}", json.dumps(payload))

    with pytest.raises(ValueError, match="MARKET_CHECKPOINT_ARTIFACT_INVALID"):
        service.run(TRADE_DATE)

    assert collector.calls == 1


def test_checkpoint_rejects_tampered_parquet_content_hash(tmp_path: Path) -> None:
    collector = FakeCollector()
    service, repository = _service(tmp_path, collector=collector)
    service.run(TRADE_DATE)
    checkpoint = repository.get_checkpoint(f"market_daily:{TRADE_DATE.isoformat()}")
    payload = json.loads(checkpoint or "{}")
    payload["parquet_content_hash"] = "0" * 64
    repository.save_checkpoint(f"market_daily:{TRADE_DATE.isoformat()}", json.dumps(payload))

    with pytest.raises(ValueError, match="MARKET_CHECKPOINT_ARTIFACT_INVALID"):
        service.run(TRADE_DATE)

    assert collector.calls == 1


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
