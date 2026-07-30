import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from hengce.contracts.market import SecurityMaster
from hengce.contracts.pilot import (
    PilotUniverseMember,
    PilotUniverseSnapshot,
)

PILOT_QUOTAS = {
    "MAIN_SH": 8,
    "MAIN_SZ": 8,
    "CHINEXT": 7,
    "STAR": 7,
}
ALGORITHM_VERSION = "board-liquidity-pilot-v1"
_BOARD_IDENTITY = {
    "MAIN_SH": ("SSE", ".SH"),
    "MAIN_SZ": ("SZSE", ".SZ"),
    "CHINEXT": ("SZSE", ".SZ"),
    "STAR": ("SSE", ".SH"),
}
_PRICE_FIELDS = ("open", "high", "low", "close", "pre_close")


class PilotUniverseSelector:
    def select(
        self,
        *,
        market_date: date,
        report_cutoff_at: datetime,
        bars: Sequence[Mapping[str, object]],
        securities: Sequence[SecurityMaster],
        market_content_hash: str,
        master_universe_hash: str,
        created_at: datetime,
    ) -> PilotUniverseSnapshot:
        if (
            report_cutoff_at.tzinfo is None
            or report_cutoff_at.utcoffset() is None
            or created_at.tzinfo is None
            or created_at.utcoffset() is None
        ):
            raise ValueError("PILOT_UNIVERSE_TIME_INVALID")
        security_codes = [security.ts_code for security in securities]
        if len(security_codes) != len(set(security_codes)):
            raise ValueError("PILOT_SECURITY_MASTER_DUPLICATE")

        bars_by_code: dict[str, list[Mapping[str, object]]] = defaultdict(list)
        for row in bars:
            row_date = self._date(row.get("trade_date"))
            ts_code = row.get("ts_code")
            if row_date == market_date and isinstance(ts_code, str):
                bars_by_code[ts_code].append(row)

        eligible: dict[str, list[tuple[SecurityMaster, Mapping[str, object], Decimal]]] = {
            board: [] for board in PILOT_QUOTAS
        }
        for security in securities:
            if not self._security_eligible(security, market_date):
                continue
            matched_bars = bars_by_code.get(security.ts_code, [])
            if len(matched_bars) != 1:
                continue
            bar = matched_bars[0]
            amount = self._valid_bar_amount(bar, security.ts_code)
            if amount is None:
                continue
            eligible[security.board].append((security, bar, amount))

        members: list[PilotUniverseMember] = []
        for board, quota in PILOT_QUOTAS.items():
            ranked = sorted(
                eligible[board],
                key=lambda row: (-row[2], row[0].ts_code),
            )
            if len(ranked) < quota:
                raise ValueError(f"PILOT_BOARD_QUOTA_UNMET:{board}")
            for rank, (security, bar, amount) in enumerate(ranked[:quota], start=1):
                members.append(
                    PilotUniverseMember(
                        ts_code=security.ts_code,
                        security_name=security.name,
                        board=board,
                        amount=amount,
                        rank_in_board=rank,
                        evidence_record_ids=(
                            str(bar["record_id"]),
                            f"security-master:{master_universe_hash}:{security.ts_code}",
                        ),
                    )
                )

        input_hashes = {
            "market": market_content_hash,
            "security_master": master_universe_hash,
        }
        identity_payload = {
            "market_date": market_date.isoformat(),
            "report_cutoff_at": report_cutoff_at.isoformat(),
            "algorithm_version": ALGORITHM_VERSION,
            "quotas": PILOT_QUOTAS,
            "members": [
                member.model_dump(mode="json")
                for member in members
            ],
            "input_hashes": input_hashes,
        }
        manifest_hash = hashlib.sha256(
            json.dumps(
                identity_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return PilotUniverseSnapshot(
            universe_id=f"pilot-{market_date.isoformat()}-{manifest_hash[:16]}",
            market_date=market_date,
            report_cutoff_at=report_cutoff_at,
            algorithm_version=ALGORITHM_VERSION,
            quotas=dict(PILOT_QUOTAS),
            members=tuple(members),
            input_hashes=input_hashes,
            manifest_hash=manifest_hash,
            created_at=created_at,
        )

    @staticmethod
    def _security_eligible(security: SecurityMaster, market_date: date) -> bool:
        identity = _BOARD_IDENTITY.get(security.board)
        return bool(
            security.is_in_scope
            and identity is not None
            and security.exchange == identity[0]
            and security.ts_code.endswith(identity[1])
            and security.security_type == "A_SHARE"
            and security.currency == "CNY"
            and (market_date - security.list_date).days >= 365
            and (
                security.delist_date is None
                or security.delist_date > market_date
            )
            and "ST" not in security.name.upper()
            and "退" not in security.name
        )

    @classmethod
    def _valid_bar_amount(
        cls,
        row: Mapping[str, object],
        expected_ts_code: str,
    ) -> Decimal | None:
        if row.get("ts_code") != expected_ts_code or not row.get("record_id"):
            return None
        values: dict[str, Decimal] = {}
        for field in (*_PRICE_FIELDS, "volume", "amount"):
            value = cls._decimal(row.get(field))
            if value is None:
                return None
            values[field] = value
        if (
            any(values[field] < 0 for field in (*_PRICE_FIELDS, "volume"))
            or values["amount"] <= 0
            or values["high"]
            < max(values["open"], values["close"], values["low"])
            or values["low"]
            > min(values["open"], values["close"], values["high"])
        ):
            return None
        return values["amount"]

    @staticmethod
    def _decimal(value: object) -> Decimal | None:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return parsed if parsed.is_finite() else None

    @staticmethod
    def _date(value: object) -> date | None:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if not isinstance(value, str):
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None


__all__ = ["PILOT_QUOTAS", "PilotUniverseSelector"]
