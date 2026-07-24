import json
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hengce.cli import app, load_trade_dates


def test_init_state_creates_sqlite_database_idempotently(tmp_path: Path) -> None:
    runner = CliRunner()

    first = runner.invoke(app, ["init-state", "--data-dir", str(tmp_path)])
    second = runner.invoke(app, ["init-state", "--data-dir", str(tmp_path)])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert (tmp_path / "state" / "hengce.sqlite3").exists()
    assert "state initialized" in first.stdout


def test_load_trade_dates_sorts_and_deduplicates(tmp_path: Path) -> None:
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(["2026-07-24", "2026-07-22", "2026-07-22"]), encoding="utf-8")

    assert load_trade_dates(calendar) == [date(2026, 7, 22), date(2026, 7, 24)]


@pytest.mark.parametrize(
    "payload",
    [
        {"date": "2026-07-22"},
        ["2026-07-22", 123],
        ["2026-07-22", "2026-7-22"],
        ["2026-02-30"],
    ],
)
def test_load_trade_dates_rejects_invalid_calendar(tmp_path: Path, payload: object) -> None:
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="TRADE_DATES_INVALID"):
        load_trade_dates(calendar)
