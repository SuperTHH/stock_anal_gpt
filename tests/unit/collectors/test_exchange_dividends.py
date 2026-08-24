from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from hengce.collectors.exchange_dividends import (
    SseImplementedDividendCollector,
    SzseImplementedDividendCollector,
)
from hengce.contracts.policy import SourcePolicy
from hengce.policy.guard import PolicyGuard
from hengce.raw_store.store import RawObjectStore
from hengce.state.repository import StateRepository

NOW = datetime(2026, 8, 12, 9, tzinfo=UTC)


def policy(source_id: str, domains: list[str]) -> SourcePolicy:
    return SourcePolicy.model_validate(
        {
            "source_id": source_id,
            "source_name": source_id,
            "allowed_domains": domains,
            "allowed_schemes": ["https"],
            "allowed_purposes": ["corporate_action"],
            "fetch_frequency": "policy_defined",
            "full_text_rule": "structured_facts_only",
            "attachment_rule": "none",
            "rate_limit_per_minute": 60,
            "robots_policy": "respect",
            "terms_url": "https://www.sse.com.cn/"
            if source_id == "sse"
            else "https://www.szse.cn/",
            "terms_reviewed_at": NOW,
            "review_status": "APPROVED",
            "connection_status": "UNKNOWN",
            "enabled": True,
        }
    )


def guard(tmp_path: Path, source_id: str, domains: list[str]) -> PolicyGuard:
    state = StateRepository(tmp_path / f"{source_id}.sqlite3")
    state.migrate()
    state.upsert_policy(policy(source_id, domains))
    return PolicyGuard(state, clock=lambda: NOW, sleeper=lambda _: None)


def test_sse_collector_queries_current_year_api_paginates_and_deduplicates(
    tmp_path: Path,
) -> None:
    payloads = {
        (2026, 1): {
            "pageHelp": {"pageCount": 2, "pageNo": 1},
            "result": [
                {
                    "A_STOCK_CODE": "600000",
                    "A_BEFR_TAX_DIV": "0.30",
                    "A_REG_DATE": "20260629",
                    "A_DIV_DATE": "20260630",
                },
                {
                    "A_STOCK_CODE": "600001",
                    "A_BEFR_TAX_DIV": "0.20",
                    "A_REG_DATE": "20260730",
                    "A_DIV_DATE": "20260731",
                },
                {
                    "A_STOCK_CODE": "600002",
                    "A_BEFR_TAX_DIV": "0.10",
                    "A_REG_DATE": "20260630",
                    "A_DIV_DATE": "20260629",
                },
            ],
        },
        (2026, 2): {
            "pageHelp": {"pageCount": 2, "pageNo": 2},
            "result": [
                {
                    "A_STOCK_CODE": "600000",
                    "A_BEFR_TAX_DIV": "0.30",
                    "A_REG_DATE": "20260629",
                    "A_DIV_DATE": "20260630",
                }
            ],
        },
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.params["sqlId"] == "COMMON_SSE_SJ_GPSJ_FHSG_SSGSFHQK_L"
        assert request.url.params["CONDITION_AG"] == "1"
        year = int(request.url.params["A_REG_DATE"])
        page = int(request.url.params["pageHelp.pageNo"])
        payload = payloads.get(
            (year, page),
            {"pageHelp": {"pageCount": 1, "pageNo": 1}, "result": []},
        )
        return httpx.Response(200, json=payload, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        records = SseImplementedDividendCollector(
            client=client,
            guard=guard(tmp_path, "sse", ["query.sse.com.cn"]),
            raw_store=RawObjectStore(tmp_path / "raw"),
            clock=lambda: NOW,
            page_size=2,
        ).fetch(date(2026, 7, 22))

    assert len(records) == 1
    assert records[0].ts_code == "600000.SH"
    assert records[0].cash_dividend_per_share == Decimal("0.30")
    assert records[0].ex_date == date(2026, 6, 30)
    assert {int(request.url.params["A_REG_DATE"]) for request in requests} == set(range(2021, 2027))


def test_sse_collector_rejects_an_official_dataset_that_is_stale(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        year = int(request.url.params["A_REG_DATE"])
        rows = []
        if year == 2021:
            rows = [
                {
                    "A_STOCK_CODE": "600000",
                    "A_BEFR_TAX_DIV": "0.30",
                    "A_REG_DATE": "20211229",
                    "A_DIV_DATE": "20211230",
                }
            ]
        return httpx.Response(
            200,
            json={"pageHelp": {"pageCount": 1, "pageNo": 1}, "result": rows},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        collector = SseImplementedDividendCollector(
            client=client,
            guard=guard(tmp_path, "sse", ["query.sse.com.cn"]),
            raw_store=RawObjectStore(tmp_path / "raw"),
            clock=lambda: NOW,
        )
        with pytest.raises(ValueError, match="SSE_DIVIDEND_DATA_STALE"):
            collector.fetch(date(2026, 7, 22))


def test_szse_collector_discovers_monthly_official_table_and_parses_dps(
    tmp_path: Path,
) -> None:
    index_url = "https://www.szse.cn/market/periodical/month/index.html"
    report_url = "https://www.szse.cn/market/periodical/month/t20260806_622028.html"
    table_url = (
        "https://docs.static.szse.cn/www/market/periodical/month/W020260806123456789012.html"
    )
    index = """
        value:'./t20260806_622028.html', text:'2026-07'
    """
    report = f'<a href="{table_url}">分红派息配股</a>'
    table = """
      <table><tr><th>Code</th></tr>
      <tr><td>000001</td><td>平安银行</td><td>0</td><td>0.000</td>
      <td>100000</td><td>0.250</td><td></td><td></td><td></td><td></td>
      <td>2026/07/20</td><td>2026/07/17</td><td>10.00</td><td>10.25</td></tr>
      <tr><td>300001</td><td>特锐德</td><td>0</td><td>0.000</td>
      <td>100000</td><td>0.100</td><td></td><td></td><td></td><td></td>
      <td>2026/07/30</td><td>2026/07/29</td><td>20.00</td><td>20.10</td></tr>
      </table>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        bodies = {index_url: index, report_url: report, table_url: table}
        return httpx.Response(200, text=bodies[str(request.url)], request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        records = SzseImplementedDividendCollector(
            client=client,
            guard=guard(
                tmp_path,
                "szse",
                ["www.szse.cn", "docs.static.szse.cn"],
            ),
            raw_store=RawObjectStore(tmp_path / "raw"),
            clock=lambda: NOW,
        ).fetch(date(2026, 7, 22))

    assert len(records) == 1
    assert records[0].ts_code == "000001.SZ"
    assert records[0].cash_dividend_per_share == Decimal("0.250")
    assert str(records[0].source_url) == table_url


def test_szse_month_report_window_covers_five_complete_dividend_years() -> None:
    index = """
        value:'./t20210806_1.html', text:'2021-07'
        value:'./t20210906_2.html', text:'2021-08'
        value:'./t20260806_3.html', text:'2026-07'
        value:'./t20260906_4.html', text:'2026-08'
    """

    reports = SzseImplementedDividendCollector._month_reports(
        index,
        date(2026, 8, 21),
    )

    assert reports == [
        "https://www.szse.cn/market/periodical/month/t20210906_2.html",
        "https://www.szse.cn/market/periodical/month/t20260806_3.html",
        "https://www.szse.cn/market/periodical/month/t20260906_4.html",
    ]
