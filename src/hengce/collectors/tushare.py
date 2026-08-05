import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError

from hengce.contracts.enums import QualityStatus
from hengce.contracts.market import MarketBar
from hengce.policy.guard import PolicyGuard


@dataclass(frozen=True)
class DailyFetchResult:
    raw_payload: bytes
    content_type: str
    collected_at: datetime
    bars: list[MarketBar]


class TushareDailyCollector:
    endpoint = "http://api.tushare.pro/"
    fields = (
        "ts_code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "vol",
        "amount",
    )

    def __init__(
        self,
        *,
        client: httpx.Client,
        guard: PolicyGuard,
        token: SecretStr,
        clock: Callable[[], datetime],
    ) -> None:
        self.client = client
        self.guard = guard
        self._token = token
        self.clock = clock

    def fetch(self, trade_date: date) -> DailyFetchResult:
        self.guard.authorize("tushare", self.endpoint, "market_daily", "collectors.tushare")
        request = {
            "api_name": "daily",
            "token": self._token.get_secret_value(),
            "params": {"trade_date": trade_date.strftime("%Y%m%d")},
            "fields": ",".join(self.fields),
        }
        response = self.client.post(self.endpoint, json=request, timeout=30)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise RuntimeError(f"TUSHARE_HTTP_ERROR_{error.response.status_code}") from error

        raw_payload = response.content
        body = self._parse_body(response)
        code = body.get("code")
        if code != 0:
            raise RuntimeError(f"TUSHARE_API_ERROR_{code}")
        fields, items = self._validate_data(body.get("data"))
        collected_at = self.clock()
        if collected_at.tzinfo is None or collected_at.utcoffset() is None:
            raise ValueError("TUSHARE_COLLECTED_AT_NOT_TIMEZONE_AWARE")
        content_hash = hashlib.sha256(raw_payload).hexdigest()
        bars = []
        for row in items:
            bar = self._to_bar(
                dict(zip(fields, row, strict=True)), content_hash, collected_at, trade_date
            )
            bars.append(bar)
        return DailyFetchResult(
            raw_payload=raw_payload,
            content_type=response.headers.get("content-type", "application/json"),
            collected_at=collected_at,
            bars=bars,
        )

    @staticmethod
    def _parse_body(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as error:
            raise ValueError("TUSHARE_RESPONSE_INVALID") from error
        if not isinstance(body, dict):
            raise ValueError("TUSHARE_RESPONSE_INVALID")
        return body

    def _validate_data(self, data: object) -> tuple[list[str], list[list[object]]]:
        if not isinstance(data, dict):
            raise ValueError("TUSHARE_FIELDS_INVALID")
        fields = data.get("fields")
        items = data.get("items")
        if (
            not isinstance(fields, list)
            or not all(isinstance(field, str) for field in fields)
            or len(fields) != len(self.fields)
            or set(fields) != set(self.fields)
        ):
            raise ValueError("TUSHARE_FIELDS_INVALID")
        if not isinstance(items, list) or any(
            not isinstance(row, list) or len(row) != len(fields) for row in items
        ):
            raise ValueError("TUSHARE_ROW_INVALID")
        return fields, items

    @staticmethod
    def _to_bar(
        row: dict[str, object], content_hash: str, collected_at: datetime, requested_date: date
    ) -> MarketBar:
        try:
            returned_date = datetime.strptime(str(row["trade_date"]), "%Y%m%d").date()
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("TUSHARE_TRADE_DATE_INVALID") from error
        if returned_date != requested_date:
            raise ValueError("TUSHARE_TRADE_DATE_MISMATCH")
        try:
            code = str(row["ts_code"])
            prices = {name: Decimal(str(row[name])) for name in TushareDailyCollector.fields[2:]}
        except (InvalidOperation, KeyError, ValueError) as error:
            raise ValueError("TUSHARE_ROW_INVALID") from error
        if any(not price.is_finite() for price in prices.values()):
            raise ValueError("TUSHARE_ROW_INVALID")
        version = f"daily-{returned_date:%Y%m%d}-{content_hash[:12]}"
        try:
            return MarketBar(
                record_id=f"{code}-{returned_date:%Y%m%d}-{content_hash[:12]}",
                source_id="tushare",
                source_url=TushareDailyCollector.endpoint,
                collected_at=collected_at,
                version=version,
                content_hash=content_hash,
                license_policy="tushare-daily",
                quality_status=QualityStatus.VALID,
                valid_from=collected_at,
                ts_code=code,
                trade_date=returned_date,
                open=prices["open"],
                high=prices["high"],
                low=prices["low"],
                close=prices["close"],
                pre_close=prices["pre_close"],
                volume=prices["vol"],
                amount=prices["amount"],
            )
        except ValidationError as error:
            raise ValueError("TUSHARE_ROW_INVALID") from error
