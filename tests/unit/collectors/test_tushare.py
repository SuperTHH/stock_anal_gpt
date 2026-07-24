from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from hengce.collectors.tushare import TushareDailyCollector
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
            allowed_purposes=["market_daily"],
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


def test_fetch_daily_rejects_rows_from_another_trade_date(tmp_path: Path) -> None:
    collector = _collector(
        tmp_path,
        httpx.MockTransport(
            lambda request: httpx.Response(200, json=_success_body(trade_date="20260723"))
        ),
    )

    with pytest.raises(ValueError, match="^TUSHARE_TRADE_DATE_MISMATCH$"):
        collector.fetch(date(2026, 7, 24))
