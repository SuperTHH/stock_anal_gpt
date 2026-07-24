import json
from pathlib import Path

import pytest

from hengce.config import Settings
from hengce.state.repository import StateRepository

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
    repository = bootstrap_state(settings, POLICY_FILE)
    original = repository.get_policy("tushare")
    assert original is not None
    repository.upsert_policy(original.model_copy(update={"connection_status": "ORIGINAL"}))

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

    assert repository.get_policy("tushare").connection_status == "ORIGINAL"
    assert repository.count_policies() == 6
