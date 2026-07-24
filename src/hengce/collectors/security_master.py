import csv
from datetime import datetime
from pathlib import Path

from hengce.contracts.market import SecurityMaster


class OfficialSecurityMasterCsvImporter:
    allowed_boards = frozenset({"MAIN_SH", "STAR", "MAIN_SZ", "CHINEXT"})
    required_columns = frozenset(
        {"ts_code", "symbol", "name", "exchange", "board", "currency", "list_date", "security_type"}
    )

    def parse(self, path: Path) -> list[SecurityMaster]:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or not self.required_columns.issubset(reader.fieldnames):
                raise ValueError("SECURITY_MASTER_COLUMNS_INVALID")
            records = [self._to_security(row) for row in reader if self._is_in_scope(row)]
        if len({record.ts_code for record in records}) != len(records):
            raise ValueError("SECURITY_MASTER_DUPLICATE_TS_CODE")
        return sorted(records, key=lambda record: record.ts_code)

    def _is_in_scope(self, row: dict[str, str | None]) -> bool:
        return (
            row.get("security_type") == "A_SHARE"
            and row.get("board") in self.allowed_boards
            and row.get("currency") == "CNY"
        )

    @staticmethod
    def _to_security(row: dict[str, str | None]) -> SecurityMaster:
        required_values = (
            "ts_code",
            "symbol",
            "name",
            "exchange",
            "board",
            "currency",
            "list_date",
            "security_type",
        )
        if any(not row.get(column) for column in required_values):
            raise ValueError("SECURITY_MASTER_ROW_INVALID")
        return SecurityMaster(
            ts_code=row["ts_code"],
            symbol=row["symbol"],
            name=row["name"],
            exchange=row["exchange"],
            board=row["board"],
            currency=row["currency"],
            list_date=datetime.strptime(row["list_date"], "%Y%m%d").date(),
            security_type=row["security_type"],
            is_in_scope=True,
        )
