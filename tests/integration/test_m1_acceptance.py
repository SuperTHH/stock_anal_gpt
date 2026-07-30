import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from hengce.config import Settings
from hengce.contracts.enums import QualityStatus
from hengce.contracts.market import MarketBar, SecurityMaster
from hengce.contracts.policy import SourcePolicy
from hengce.services.pilot_universe import PilotUniverseSelector
from hengce.state.repository import StateRepository
from hengce.warehouse.market import MarketWarehouse

POLICY_FILE = Path(__file__).parents[2] / "config" / "source_policies.json"


def test_m1_bootstrap_migrates_state_and_seeds_six_policies(tmp_path: Path) -> None:
    from hengce.bootstrap import bootstrap_state

    settings = Settings(data_dir=tmp_path)
    repository = bootstrap_state(settings, POLICY_FILE)

    assert isinstance(repository, StateRepository)
    assert repository.count_policies() == 6
    assert repository.get_policy("tushare") is not None
    assert (tmp_path / "state" / "hengce.sqlite3").exists()


def test_policy_file_has_only_approved_six_sources_and_tushare_http_exception() -> None:
    policies = json.loads(POLICY_FILE.read_text(encoding="utf-8"))

    assert [item["source_id"] for item in policies] == [
        "tushare",
        "sse",
        "szse",
        "cninfo",
        "stats",
        "csrc",
    ]
    assert policies[0]["allowed_schemes"] == ["http"]
    assert all(item["allowed_schemes"] == ["https"] for item in policies[1:])
    assert all(item["review_status"] == "APPROVED" and item["enabled"] for item in policies)


def test_invalid_policy_file_does_not_write_partial_seed(tmp_path: Path) -> None:
    from hengce.bootstrap import bootstrap_state

    policies = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    policies[1]["enabled"] = "not-a-bool"
    invalid_file = tmp_path / "invalid.json"
    invalid_file.write_text(json.dumps(policies), encoding="utf-8")

    with pytest.raises(ValueError):
        bootstrap_state(Settings(data_dir=tmp_path / "data"), invalid_file)

    repository = StateRepository(tmp_path / "data" / "state" / "hengce.sqlite3")
    repository.migrate()
    assert repository.count_policies() == 0


def test_duplicate_policy_file_does_not_write_partial_seed(tmp_path: Path) -> None:
    from hengce.bootstrap import bootstrap_state

    policies = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    policies.append(policies[0])
    invalid_file = tmp_path / "duplicate.json"
    invalid_file.write_text(json.dumps(policies), encoding="utf-8")

    with pytest.raises(ValueError, match="SOURCE_POLICIES_DUPLICATE_SOURCE_ID"):
        bootstrap_state(Settings(data_dir=tmp_path / "data"), invalid_file)

    repository = StateRepository(tmp_path / "data" / "state" / "hengce.sqlite3")
    repository.migrate()
    assert repository.count_policies() == 0


def test_seed_write_failure_rolls_back_all_prevalidated_policy_changes(tmp_path: Path) -> None:
    from hengce.bootstrap import bootstrap_state

    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_local_dirs()
    repository = StateRepository(settings.data_dir / "state" / "hengce.sqlite3")
    repository.migrate()
    tushare = SourcePolicy.model_validate(json.loads(POLICY_FILE.read_text(encoding="utf-8"))[0])
    original = tushare.model_copy(update={"connection_status": "ORIGINAL"})
    repository.upsert_policy(original)

    import sqlite3

    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            """
            CREATE TRIGGER abort_szse_seed
            BEFORE INSERT ON source_policies
            WHEN NEW.source_id = 'szse'
            BEGIN SELECT RAISE(ABORT, 'seed failure'); END;
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="seed failure"):
        bootstrap_state(settings, POLICY_FILE)

    assert repository.get_policy("tushare") == original
    assert repository.count_policies() == 1


def test_bootstrap_only_inserts_missing_policies_and_preserves_operational_state(
    tmp_path: Path,
) -> None:
    from hengce.bootstrap import bootstrap_state

    settings = Settings(data_dir=tmp_path / "data")
    repository = bootstrap_state(settings, POLICY_FILE)
    existing = repository.get_policy("tushare")
    assert existing is not None
    changed = existing.model_copy(
        update={"connection_status": "UNAVAILABLE", "enabled": False}
    )
    repository.upsert_policy(changed)

    bootstrap_state(settings, POLICY_FILE)

    assert repository.get_policy("tushare") == changed


def test_pilot_selector_consumes_persisted_market_and_dual_master_shapes(
    tmp_path: Path,
) -> None:
    """Catches SQLite/Parquet serialization changing the selector's ranking inputs."""
    from hengce.bootstrap import bootstrap_state

    settings = Settings(data_dir=tmp_path / "data")
    repository = bootstrap_state(settings, POLICY_FILE)
    market_date = date(2026, 7, 22)
    cutoff = datetime(2026, 7, 22, 21, 30, tzinfo=UTC)
    created_at = datetime(2026, 7, 30, 9, tzinfo=UTC)
    board_config = (
        ("MAIN_SH", "SSE", "600", "SH", 8),
        ("STAR", "SSE", "688", "SH", 7),
        ("MAIN_SZ", "SZSE", "000", "SZ", 8),
        ("CHINEXT", "SZSE", "300", "SZ", 7),
    )
    securities: list[SecurityMaster] = []
    bars: list[MarketBar] = []
    sequence = 0
    for board, exchange, prefix, suffix, count in board_config:
        for index in range(1, count + 1):
            sequence += 1
            symbol = f"{prefix}{index:03d}"
            ts_code = f"{symbol}.{suffix}"
            securities.append(
                SecurityMaster(
                    ts_code=ts_code,
                    symbol=symbol,
                    name=f"虚构公司{sequence:02d}",
                    exchange=exchange,
                    board=board,
                    list_date=date(2020, 1, 1),
                    is_in_scope=True,
                )
            )
            bars.append(
                MarketBar(
                    record_id=f"bar-{ts_code}",
                    source_id="tushare",
                    source_url="http://api.tushare.pro/",
                    collected_at=cutoff,
                    version="daily-20260722",
                    content_hash="9" * 64,
                    license_policy="tushare-daily",
                    quality_status=QualityStatus.VALID,
                    valid_from=cutoff,
                    ts_code=ts_code,
                    trade_date=market_date,
                    open=Decimal("10"),
                    high=Decimal("11"),
                    low=Decimal("9"),
                    close=Decimal("10.5"),
                    pre_close=Decimal("10"),
                    volume=Decimal("1000000"),
                    amount=Decimal("1000000000") - sequence,
                )
            )

    repository.save_security_master_snapshot(
        [item for item in securities if item.exchange == "SSE"],
        source_id="sse",
        source_url="https://www.sse.com.cn/assortment/stock/list/share/",
        collected_at=cutoff,
        content_hash="1" * 64,
        version="sse-pilot-fixture-v1",
        quality_lineage={"fixture": True},
    )
    repository.save_security_master_snapshot(
        [item for item in securities if item.exchange == "SZSE"],
        source_id="szse",
        source_url="https://www.szse.cn/market/product/stock/list/",
        collected_at=cutoff,
        content_hash="2" * 64,
        version="szse-pilot-fixture-v1",
        quality_lineage={"fixture": True},
    )
    warehouse = MarketWarehouse(settings.data_dir / "normalized")
    artifact = warehouse.write_bars(bars)
    market_hash = warehouse.validate_artifact(artifact, market_date, 30)
    master = repository.get_security_master_universe()

    snapshot = PilotUniverseSelector().select(
        market_date=market_date,
        report_cutoff_at=cutoff,
        bars=warehouse.read_bars(market_date),
        securities=master.securities,
        market_content_hash=market_hash,
        master_universe_hash=master.universe_hash,
        created_at=created_at,
    )

    assert len(snapshot.members) == 30
    assert snapshot.input_hashes == {
        "market": market_hash,
        "security_master": master.universe_hash,
    }
