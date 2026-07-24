"""Local-only command-line entry points for the research workspace."""

import hashlib
import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import httpx
import typer
from pydantic import SecretStr

from hengce.bootstrap import bootstrap_state
from hengce.collectors.security_master import OfficialSecurityMasterCsvImporter
from hengce.collectors.tushare import TushareDailyCollector
from hengce.config import Settings
from hengce.policy.guard import PolicyGuard
from hengce.raw_store.store import RawObjectStore
from hengce.services.initializer import HistoricalInitializer
from hengce.services.market_ingestion import MarketIngestionService
from hengce.state.repository import StateRepository
from hengce.warehouse.market import MarketWarehouse

DEFAULT_POLICY_FILE = Path(__file__).resolve().parents[2] / "config" / "source_policies.json"

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Hengce local research commands."""


def load_trade_dates(path: Path) -> list[date]:
    """Load a JSON calendar containing only ISO-8601 trade-date strings."""
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("TRADE_DATES_INVALID: expected a JSON array of ISO dates") from error
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("TRADE_DATES_INVALID: expected a JSON array of ISO dates")

    parsed: list[date] = []
    for value in values:
        try:
            item = date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("TRADE_DATES_INVALID: expected ISO dates") from error
        if item.isoformat() != value:
            raise ValueError("TRADE_DATES_INVALID: expected ISO dates")
        parsed.append(item)
    return sorted(set(parsed))


def parse_trade_date(value: str) -> date:
    """Parse a strict command-line ISO-8601 trade date."""
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise typer.BadParameter("trade date must use YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise typer.BadParameter("trade date must use YYYY-MM-DD")
    return parsed


def build_market_ingestion(
    settings: Settings, client: httpx.Client, policy_file: Path = DEFAULT_POLICY_FILE
) -> MarketIngestionService:
    """Compose M1 ingestion without making a request; caller owns ``client`` lifecycle."""
    state = bootstrap_state(settings, policy_file)
    if settings.tushare_token is None:
        raise typer.BadParameter("HENGCE_TUSHARE_TOKEN is required")
    collector = TushareDailyCollector(
        client=client,
        guard=PolicyGuard(state),
        token=SecretStr(settings.tushare_token.get_secret_value()),
        clock=lambda: datetime.now(ZoneInfo(settings.timezone)),
    )
    return MarketIngestionService(
        collector=collector,
        raw_store=RawObjectStore(settings.data_dir / "raw"),
        warehouse=MarketWarehouse(settings.data_dir / "normalized"),
        state=state,
    )


@app.command("init-state")
def init_state(
    data_dir: Annotated[Path, typer.Option(file_okay=False)] = Path("data"),
    policy_file: Annotated[Path, typer.Option()] = DEFAULT_POLICY_FILE,
) -> None:
    """Create idempotent local state and seed the approved source policies."""
    bootstrap_state(Settings(data_dir=data_dir), policy_file)
    typer.echo("state initialized")


@app.command("ingest-market")
def ingest_market(
    trade_date: Annotated[str, typer.Option()],
    data_dir: Annotated[Path, typer.Option(file_okay=False)] = Path("data"),
    policy_file: Annotated[Path, typer.Option()] = DEFAULT_POLICY_FILE,
) -> None:
    """Collect exactly one approved Tushare daily-market response."""
    parsed_trade_date = parse_trade_date(trade_date)
    settings = Settings(data_dir=data_dir)
    with httpx.Client() as client:
        result = build_market_ingestion(settings, client, policy_file).run(parsed_trade_date)
    typer.echo(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))


@app.command("initialize-history")
def initialize_history(
    calendar_file: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    data_dir: Annotated[Path, typer.Option(file_okay=False)] = Path("data"),
    policy_file: Annotated[Path, typer.Option()] = DEFAULT_POLICY_FILE,
) -> None:
    """Resume approved trading-day ingestion from its persisted checkpoint."""
    trade_dates = load_trade_dates(calendar_file)
    settings = Settings(data_dir=data_dir)
    with httpx.Client() as client:
        ingestion = build_market_ingestion(settings, client, policy_file)
        state = StateRepository(settings.data_dir / "state" / "hengce.sqlite3")
        result = HistoricalInitializer(ingestion=ingestion, state=state).run(trade_dates)
    typer.echo(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))


@app.command("import-security-master")
def import_security_master(
    file: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    source_id: Annotated[str, typer.Option()],
    source_url: Annotated[str, typer.Option()],
    version: Annotated[str, typer.Option()],
    collected_at: Annotated[str, typer.Option()],
    data_dir: Annotated[Path, typer.Option(file_okay=False)] = Path("data"),
    policy_file: Annotated[Path, typer.Option()] = DEFAULT_POLICY_FILE,
) -> None:
    """Import a locally supplied official security-master CSV; this command never fetches."""
    try:
        parsed_collected_at = datetime.fromisoformat(collected_at)
    except ValueError as error:
        raise typer.BadParameter("collected-at must be ISO-8601") from error
    if parsed_collected_at.tzinfo is None:
        raise typer.BadParameter("collected-at must include an offset")
    records = OfficialSecurityMasterCsvImporter().parse(file)
    content_hash = hashlib.sha256(file.read_bytes()).hexdigest()
    state = bootstrap_state(Settings(data_dir=data_dir), policy_file)
    snapshot = state.save_security_master_snapshot(
        records,
        source_id=source_id,
        source_url=source_url,
        collected_at=parsed_collected_at,
        content_hash=content_hash,
        version=version,
        quality_lineage={
            "filter": "a_share_cny_four_boards",
            "boards": ["CHINEXT", "MAIN_SH", "MAIN_SZ", "STAR"],
            "record_count": len(records),
        },
    )
    typer.echo(
        json.dumps(
            {
                "content_hash": snapshot.content_hash,
                "security_count": len(snapshot.securities),
                "source_id": snapshot.source_id,
                "version": snapshot.version,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    app()
