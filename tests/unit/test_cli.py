import json
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from hengce import cli
from hengce.cli import app, build_market_ingestion, load_trade_dates
from hengce.config import Settings
from hengce.services.market_ingestion import MarketIngestionResult
from hengce.state.repository import StateRepository


def test_init_state_creates_sqlite_database_idempotently(tmp_path: Path) -> None:
    runner = CliRunner()

    first = runner.invoke(app, ["init-state", "--data-dir", str(tmp_path)])
    second = runner.invoke(app, ["init-state", "--data-dir", str(tmp_path)])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert (tmp_path / "state" / "hengce.sqlite3").exists()
    assert "state initialized" in first.stdout
    assert StateRepository(tmp_path / "state" / "hengce.sqlite3").count_policies() == 6


def test_init_state_seeds_approved_policies(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["init-state", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "state initialized" in result.stdout


def test_build_market_ingestion_rejects_missing_token_before_client_use(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, tushare_token=None)
    client = Mock()

    with pytest.raises(Exception, match="HENGCE_TUSHARE_TOKEN is required"):
        build_market_ingestion(settings, client)

    assert not client.mock_calls


def test_ingest_command_uses_injected_composition_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    service = Mock()
    service.run.return_value = MarketIngestionResult(
        trade_date="2026-07-24",
        bar_count=1,
        raw_content_hash="a" * 64,
        parquet_path=str(tmp_path / "normalized" / "part.parquet"),
    )
    composition = Mock(return_value=service)
    monkeypatch.setattr(cli, "build_market_ingestion", composition)

    result = runner.invoke(
        app,
        ["ingest-market", "--trade-date", "2026-07-24", "--data-dir", str(tmp_path)],
    )

    assert result.exit_code == 0
    service.run.assert_called_once_with(date(2026, 7, 24))
    assert "HENGCE_TUSHARE_TOKEN" not in result.stdout


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
