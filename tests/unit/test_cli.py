import hashlib
import json
import sys
import zipfile
from datetime import date
from pathlib import Path
from subprocess import run
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from hengce import cli
from hengce.cli import app, build_market_ingestion, load_trade_dates
from hengce.config import Settings
from hengce.services.initializer import InitializationResult
from hengce.services.market_ingestion import MarketIngestionResult
from hengce.state.repository import StateRepository


class ClosingClient:
    def __init__(self) -> None:
        self.closed = False

    def __enter__(self) -> "ClosingClient":
        return self

    def __exit__(self, *args: object) -> bool:
        self.closed = True
        return False


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


def test_default_seed_is_packaged_and_matches_operator_copy() -> None:
    from importlib.resources import files

    packaged = files("hengce").joinpath("data", "source_policies.json")
    operator_copy = Path(__file__).parents[2] / "config" / "source_policies.json"

    assert json.loads(packaged.read_text(encoding="utf-8")) == json.loads(
        operator_copy.read_text(encoding="utf-8")
    )


def test_wheel_contains_default_seed_and_installed_cli_initializes_state(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[2]
    dist = tmp_path / "dist"
    target = tmp_path / "site"
    data_dir = tmp_path / "data"
    build = run(
        [sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", str(dist)],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    wheel = next(dist.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert "hengce/data/source_policies.json" in archive.namelist()

    install = run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--ignore-installed",
            "--no-deps",
            "--prefix",
            str(target),
            str(wheel),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert install.returncode == 0, install.stderr
    wrapper = target / "Scripts" / "hengce.exe"
    assert wrapper.is_file()
    command = run(
        [str(wrapper), "init-state", "--data-dir", str(data_dir)],
        cwd=tmp_path,
        env={
            **__import__("os").environ,
            "PYTHONPATH": str(target / "Lib" / "site-packages"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert command.returncode == 0, command.stderr
    assert StateRepository(data_dir / "state" / "hengce.sqlite3").count_policies() == 6


@pytest.mark.parametrize(
    ("source_id", "source_url", "row"),
    [
        (
            "sse",
            "https://www.sse.com.cn/master.csv",
            "600000.SH,600000,Example,SSE,MAIN_SH,CNY,19991110,A_SHARE",
        ),
        (
            "szse",
            "https://www.szse.cn/master.csv",
            "000001.SZ,000001,Example,SZSE,MAIN_SZ,CNY,19910403,A_SHARE",
        ),
    ],
)
def test_import_security_master_persists_approved_official_file_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_id: str,
    source_url: str,
    row: str,
) -> None:
    runner = CliRunner()
    source = tmp_path / "security-master.csv"
    source.write_text(
        "ts_code,symbol,name,exchange,board,currency,list_date,security_type\n"
        f"{row}\n",
        encoding="utf-8",
    )
    client_factory = Mock()
    monkeypatch.setattr(cli.httpx, "Client", client_factory)

    result = runner.invoke(app, [
        "import-security-master", "--file", str(source), "--source-id", source_id,
        "--source-url", source_url, "--version", "2026-07-24",
        "--collected-at", "2026-07-24T09:00:00+00:00", "--data-dir", str(tmp_path),
    ])

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert output["security_count"] == 1
    assert output["source_id"] == source_id
    assert output["version"] == "2026-07-24"
    assert output["content_hash"]
    assert not client_factory.mock_calls


def test_import_security_master_audits_policy_denial_without_persisting(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    source = tmp_path / "security-master.csv"
    source.write_text(
        "ts_code,symbol,name,exchange,board,currency,list_date,security_type\n"
        "600000.SH,600000,Example,SSE,MAIN_SH,CNY,19991110,A_SHARE\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "import-security-master",
            "--file",
            str(source),
            "--source-id",
            "unknown",
            "--source-url",
            "https://unknown.example/master.csv",
            "--version",
            "2026-07-24",
            "--collected-at",
            "2026-07-24T09:00:00+00:00",
            "--data-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code != 0
    assert result.exception is not None
    assert str(result.exception) == "SOURCE_POLICY_MISSING"
    repository = StateRepository(tmp_path / "state" / "hengce.sqlite3")
    assert repository.count_refusals() == 1
    content_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    assert repository.get_security_master_snapshot("unknown", content_hash) is None


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


def test_ingest_rejects_invalid_trade_date_before_client_or_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    client_factory = Mock()
    composition = Mock()
    monkeypatch.setattr(cli.httpx, "Client", client_factory)
    monkeypatch.setattr(cli, "build_market_ingestion", composition)

    result = runner.invoke(
        app,
        ["ingest-market", "--trade-date", "2026-7-24", "--data-dir", str(tmp_path)],
    )

    assert result.exit_code != 0
    assert "trade date must use YYYY-MM-DD" in result.stderr
    assert not client_factory.mock_calls
    assert not composition.mock_calls


def test_history_rejects_invalid_calendar_before_client_or_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    calendar = tmp_path / "invalid-calendar.json"
    calendar.write_text('{"not": "a list"}', encoding="utf-8")
    client_factory = Mock()
    composition = Mock()
    monkeypatch.setattr(cli.httpx, "Client", client_factory)
    monkeypatch.setattr(cli, "build_market_ingestion", composition)

    result = runner.invoke(
        app,
        ["initialize-history", "--calendar-file", str(calendar), "--data-dir", str(tmp_path)],
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)
    assert "TRADE_DATES_INVALID" in str(result.exception)
    assert not client_factory.mock_calls
    assert not composition.mock_calls


@pytest.mark.parametrize("command_name", ["ingest-market", "initialize-history"])
@pytest.mark.parametrize("fails", [False, True])
def test_network_commands_close_owned_client_on_success_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command_name: str,
    fails: bool,
) -> None:
    runner = CliRunner()
    client = ClosingClient()
    monkeypatch.setattr(cli.httpx, "Client", Mock(return_value=client))
    service = Mock()
    if fails:
        service.run.side_effect = RuntimeError("fixture failure")
    else:
        service.run.return_value = MarketIngestionResult(
            "2026-07-24", 1, "a" * 64, "fixture.parquet"
        )
    monkeypatch.setattr(cli, "build_market_ingestion", Mock(return_value=service))

    if command_name == "ingest-market":
        arguments = ["ingest-market", "--trade-date", "2026-07-24", "--data-dir", str(tmp_path)]
    else:
        calendar = tmp_path / "calendar.json"
        calendar.write_text('["2026-07-24"]', encoding="utf-8")
        initializer = Mock()
        if fails:
            initializer.run.side_effect = RuntimeError("fixture failure")
        else:
            initializer.run.return_value = InitializationResult(
                completed_dates=1, last_trade_date="2026-07-24"
            )
        monkeypatch.setattr(cli, "HistoricalInitializer", Mock(return_value=initializer))
        arguments = [
            "initialize-history",
            "--calendar-file",
            str(calendar),
            "--data-dir",
            str(tmp_path),
        ]

    result = runner.invoke(app, arguments)

    assert (result.exit_code != 0) is fails
    assert client.closed


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
