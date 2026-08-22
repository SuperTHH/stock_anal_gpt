import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI

from hengce.state.report_repository import ReportRepository

from .app import create_app


class LocalReadOnlyReportRepository(ReportRepository):
    def latest_report_id(self) -> str | None:
        if not self.path.is_file():
            return None
        try:
            return super().latest_report_id()
        except sqlite3.OperationalError as error:
            if "no such table: report_pointer" in str(error):
                return None
            raise


def create_local_app(data_dir: Path) -> FastAPI:
    """Build the loopback-only read surface without creating local state."""
    database = data_dir / "state" / "hengce.sqlite3"
    return create_app(
        LocalReadOnlyReportRepository(database),
        data_dir / "reports",
        data_dir / "normalized",
    )


app = create_local_app(
    Path(os.environ.get("HENGCE_DATA_DIR", "data")).resolve()
)

__all__ = ["LocalReadOnlyReportRepository", "app", "create_local_app"]
