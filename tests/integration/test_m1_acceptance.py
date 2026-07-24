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
