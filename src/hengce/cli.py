"""Local-only command-line entry points for the research workspace."""

import json
from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from hengce.config import Settings
from hengce.state.repository import StateRepository

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Hengce local research commands."""


@app.command("init-state")
def init_state(
    data_dir: Annotated[Path, typer.Option(file_okay=False)] = Path("data"),
) -> None:
    """Create idempotent local directories and the SQLite state database."""
    settings = Settings(data_dir=data_dir)
    settings.ensure_local_dirs()
    repository = StateRepository(data_dir / "state" / "hengce.sqlite3")
    repository.migrate()
    typer.echo("state initialized")


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


if __name__ == "__main__":
    app()
