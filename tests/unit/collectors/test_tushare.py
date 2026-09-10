import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from hengce.collectors.tushare import TushareDailyCollector, TushareTradeCalendarCollector
from hengce.contracts.enums import ReviewStatus
from hengce.contracts.policy import SourcePolicy
from hengce.policy.guard import PolicyDenied, PolicyGuard
from hengce.state.repository import StateRepository


def _repository(tmp_path: Path, *, enabled: bool = True) -> StateRepository:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    repository.upsert_policy(
        SourcePolicy(
            source_id="tushare",
            source_name="Tushare",
            allowed_domains=["api.tushare.pro"],
            allowed_schemes=["http"],
            allowed_purposes=["market_daily", "market_calendar"],
            fetch_frequency="trading_day",
            full_text_rule="structured_only",
            attachment_rule="none",
            rate_limit_per_minute=1,
            robots_policy="api_terms",
            terms_url="https://tushare.pro/document/1?doc_id=290",
            terms_reviewed_at=datetime(2026, 7, 24, 9, 0, tzinfo=UTC),
            review_status=ReviewStatus.APPROVED,
            connection_status="AVAILABLE",
            enabled=enabled,
        )
    )
    return repository


def _collector(
    tmp_path: Path, handler: httpx.MockTransport, *, enabled: bool = True
) -> TushareDailyCollector:
    return TushareDailyCollector(
        client=httpx.Client(transport=handler),
        guard=PolicyGuard(_repository(tmp_path, enabled=enabled)),
        token=SecretStr("test-token"),
        clock=lambda: datetime(2026, 7, 24, 21, 31, tzinfo=UTC),
    )


def _success_body(*, trade_date: str = "20260724") -> dict[str, object]:
    return {
        "code": 0,
        "msg": None,
        "data": {
            "fields": [
                "ts_code",
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "pre_close",
                "vol",
                "amount",
            ],
            "items": [["600000.SH", trade_date, 10, 11, 9, 10.5, 10, 1000, 10500]],
        },
    }


def test_fetch_daily_makes_one_full_market_request(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_success_body())

    result = _collector(tmp_path, httpx.MockTransport(handler)).fetch(date(2026, 7, 24))

    assert len(calls) == 1
    assert b'"trade_date":"20260724"' in calls[0].content
    assert b'"ts_code"' not in calls[0].content
    assert result.bars[0].ts_code == "600000.SH"
    assert result.bars[0].open.as_tuple().exponent == 0


def test_fetch_trade_calendar_returns_only_valid_open_dates(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "fields": ["is_open", "cal_date"],
                    "items": [[1, "20260722"], ["1", "20260721"]],
                },
            },
        )

    collector = TushareTradeCalendarCollector(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        guard=PolicyGuard(_repository(tmp_path)),
        token=SecretStr("test-token"),
        clock=lambda: datetime(2026, 7, 24, 21, 31, tzinfo=UTC),
    )
    result = collector.fetch(date(2026, 7, 21), date(2026, 7, 22))

    assert result.trade_dates == [date(2026, 7, 21), date(2026, 7, 22)]
    assert b'"api_name":"trade_cal"' in requests[0].content
    assert b'"is_open":"1"' in requests[0].content


def test_fetch_daily_accepts_reordered_unique_fields(tmp_path: Path) -> None:
    fields = [
        "amount",
        "vol",
        "pre_close",
        "close",
        "low",
        "high",
        "open",
        "trade_date",
        "ts_code",
    ]
    body = _success_body()
    body["data"] = {
        "fields": fields,
        "items": [[10500, 1000, 10, 10.5, 9, 11, 10, "20260724", "600000.SH"]],
    }
    collector = _collector(
        tmp_path,
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=json.dumps(body, allow_nan=True).encode(),
                headers={"content-type": "application/json"},
            )
        ),
    )

    result = collector.fetch(date(2026, 7, 24))

    assert result.bars[0].open == 10
    assert result.bars[0].amount == 10500


def test_policy_denial_makes_zero_http_calls(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_success_body())

    with pytest.raises(PolicyDenied, match="^SOURCE_DISABLED$"):
        _collector(tmp_path, httpx.MockTransport(handler), enabled=False).fetch(date(2026, 7, 24))

    assert calls == []


def test_fetch_daily_rejects_tushare_api_error(tmp_path: Path) -> None:
    collector = _collector(
        tmp_path,
        httpx.MockTransport(
            lambda request: httpx.Response(200, json={"code": 101, "msg": "bad token"})
        ),
    )

    with pytest.raises(RuntimeError, match="^TUSHARE_API_ERROR_101$"):
        collector.fetch(date(2026, 7, 24))


@pytest.mark.parametrize(
    "body",
    [
        {"code": 0, "data": {"fields": ["ts_code"], "items": []}},
        {"code": 0, "data": {"fields": ["ts_code", "trade_date"], "items": [["600000.SH"]]}},
    ],
)
def test_fetch_daily_rejects_malformed_shape(tmp_path: Path, body: dict[str, object]) -> None:
    collector = _collector(
        tmp_path, httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    )

    with pytest.raises(ValueError, match="TUSHARE_(FIELDS|ROW)_INVALID"):
        collector.fetch(date(2026, 7, 24))


@pytest.mark.parametrize(
    "fields",
    [
        [
            "ts_code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "vol",
            "vol",
        ],
        [
            "ts_code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "vol",
            "amount",
            "extra",
        ],
    ],
)
def test_fetch_daily_rejects_duplicate_or_extra_fields(tmp_path: Path, fields: list[str]) -> None:
    body = _success_body()
    body["data"] = {"fields": fields, "items": []}
    collector = _collector(
        tmp_path, httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    )

    with pytest.raises(ValueError, match="^TUSHARE_FIELDS_INVALID$"):
        collector.fetch(date(2026, 7, 24))


def test_fetch_daily_rejects_rows_from_another_trade_date(tmp_path: Path) -> None:
    collector = _collector(
        tmp_path,
        httpx.MockTransport(
            lambda request: httpx.Response(200, json=_success_body(trade_date="20260723"))
        ),
    )

    with pytest.raises(ValueError, match="^TUSHARE_TRADE_DATE_MISMATCH$"):
        collector.fetch(date(2026, 7, 24))


@pytest.mark.parametrize("invalid_open", ["not-a-number", float("nan"), float("inf")])
def test_fetch_daily_normalizes_invalid_numeric_values(
    tmp_path: Path, invalid_open: object
) -> None:
    body = _success_body()
    data = body["data"]
    assert isinstance(data, dict)
    items = data["items"]
    assert isinstance(items, list)
    items[0][2] = invalid_open
    collector = _collector(
        tmp_path,
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=json.dumps(body, allow_nan=True).encode(),
                headers={"content-type": "application/json"},
            )
        ),
    )

    with pytest.raises(ValueError, match="^TUSHARE_ROW_INVALID$"):
        collector.fetch(date(2026, 7, 24))
