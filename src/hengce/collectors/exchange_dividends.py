from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin

import httpx

from hengce.contracts.enums import QualityStatus
from hengce.contracts.market_screen import ImplementedDividend
from hengce.policy.guard import PolicyGuard
from hengce.raw_store.store import RawObjectStore

_SSE_QUERY_URL = "https://query.sse.com.cn/commonQuery.do"
_SSE_SOURCE_PAGE = (
    "https://www.sse.com.cn/market/stockdata/dividends/dividend/index_his.shtml"
)
_SZSE_INDEX_URL = "https://www.szse.cn/market/periodical/month/index.html"
_TAG = re.compile(r"<[^>]+>")


def _parse_date(value: object) -> date:
    text = str(value).strip().replace("/", "-")
    return date.fromisoformat(text)


def _decode(payload: bytes) -> str:
    for encoding in ("utf-8", "gb18030"):
        try:
            decoded = payload.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "分红" in decoded or "DIVIDEND" in decoded or "periodical" in decoded:
            return decoded
    return payload.decode("gb18030", errors="replace")


def _text(cell: str) -> str:
    return html.unescape(_TAG.sub("", cell)).strip()


class SseImplementedDividendCollector:
    def __init__(
        self,
        *,
        client: httpx.Client,
        guard: PolicyGuard,
        raw_store: RawObjectStore,
        clock: Callable[[], datetime],
        page_size: int = 2000,
    ) -> None:
        self.client = client
        self.guard = guard
        self.raw_store = raw_store
        self.clock = clock
        self.page_size = page_size

    def fetch(self, market_date: date) -> list[ImplementedDividend]:
        page = 1
        page_count = 1
        records: dict[tuple[str, date, date, Decimal], ImplementedDividend] = {}
        while page <= page_count:
            self.guard.authorize(
                "sse", _SSE_QUERY_URL, "corporate_action", "collectors.exchange_dividends"
            )
            response = self.client.get(
                _SSE_QUERY_URL,
                params={
                    "isPagination": "true",
                    "sqlId": "COMMON_SSE_GP_SJTJ_FHSG_AGFH_L_NEW",
                    "pageHelp.pageSize": self.page_size,
                    "pageHelp.pageNo": page,
                    "pageHelp.beginPage": page,
                    "pageHelp.endPage": page + 1,
                    "pageHelp.cacheSize": 1,
                    "record_date_a": "",
                    "security_code_a": "",
                },
                headers={"Referer": _SSE_SOURCE_PAGE},
                timeout=30,
            )
            response.raise_for_status()
            collected_at = self.clock()
            raw_ref = self.raw_store.put(
                source_id="sse",
                source_url=str(response.request.url),
                collected_at=collected_at,
                content_type=response.headers.get("content-type", "application/json"),
                payload=response.content,
            )
            try:
                payload = response.json()
                page_help = payload["pageHelp"]
                rows = payload.get("result") or page_help.get("data") or []
                page_count = max(1, int(page_help.get("pageCount") or 1))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("SSE_DIVIDEND_RESPONSE_INVALID") from error
            if not isinstance(rows, list):
                raise ValueError("SSE_DIVIDEND_RESPONSE_INVALID")
            for row in rows:
                try:
                    code = str(row["SECURITY_CODE_A"]).strip()
                    dps = Decimal(str(row["DIVIDEND_PER_SHARE2_A"]).strip())
                    record_date = _parse_date(row["RECORD_DATE_A"])
                    ex_date = _parse_date(row["EX_DIVIDEND_DATE_A"])
                except (KeyError, TypeError, ValueError, InvalidOperation):
                    continue
                if (
                    not re.fullmatch(r"(?:6\d{5})", code)
                    or dps <= 0
                    or ex_date > market_date
                    or record_date > ex_date
                ):
                    continue
                identity = (code, record_date, ex_date, dps)
                record_id = hashlib.sha256(
                    f"sse|{code}|{record_date}|{ex_date}|{dps}".encode()
                ).hexdigest()
                records[identity] = ImplementedDividend(
                    record_id=f"implemented-dividend-{record_id}",
                    source_id="sse",
                    source_url=_SSE_SOURCE_PAGE,
                    published_at=None,
                    effective_at=datetime.combine(
                        ex_date, datetime.min.time(), tzinfo=collected_at.tzinfo
                    ),
                    collected_at=collected_at,
                    valid_from=collected_at,
                    version=f"sse-implemented-{market_date.isoformat()}",
                    content_hash=raw_ref.content_hash,
                    license_policy="personal-non-commercial-research",
                    quality_status=QualityStatus.VALID,
                    ts_code=f"{code}.SH",
                    record_date=record_date,
                    ex_date=ex_date,
                    cash_dividend_per_share=dps,
                )
            page += 1
        return sorted(
            records.values(), key=lambda item: (item.ts_code, item.ex_date, item.record_id)
        )


class SzseImplementedDividendCollector:
    def __init__(
        self,
        *,
        client: httpx.Client,
        guard: PolicyGuard,
        raw_store: RawObjectStore,
        clock: Callable[[], datetime],
    ) -> None:
        self.client = client
        self.guard = guard
        self.raw_store = raw_store
        self.clock = clock

    def fetch(self, market_date: date) -> list[ImplementedDividend]:
        index = self._get(_SZSE_INDEX_URL)
        month_reports = self._month_reports(_decode(index.content), market_date)
        records: dict[tuple[str, date, date, Decimal], ImplementedDividend] = {}
        for report_url in month_reports:
            report = self._get(report_url)
            report_text = _decode(report.content)
            matches = re.findall(
                r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>\s*分红派息配股\s*</a>',
                report_text,
                re.IGNORECASE,
            )
            if not matches:
                continue
            table_url = urljoin(report_url, matches[0]).replace("http://", "https://")
            table = self._get(table_url)
            collected_at = self.clock()
            raw_ref = self.raw_store.put(
                source_id="szse",
                source_url=table_url,
                collected_at=collected_at,
                content_type=table.headers.get("content-type", "text/html"),
                payload=table.content,
            )
            for cells in self._rows(_decode(table.content)):
                try:
                    code = cells[0]
                    dps = Decimal(cells[5].replace(",", ""))
                    ex_date = _parse_date(cells[10])
                    record_date = _parse_date(cells[11])
                except (IndexError, InvalidOperation, ValueError):
                    continue
                if (
                    not re.fullmatch(r"(?:00|30)\d{4}", code)
                    or dps <= 0
                    or ex_date > market_date
                    or record_date > ex_date
                ):
                    continue
                identity = (code, record_date, ex_date, dps)
                record_id = hashlib.sha256(
                    f"szse|{code}|{record_date}|{ex_date}|{dps}".encode()
                ).hexdigest()
                records[identity] = ImplementedDividend(
                    record_id=f"implemented-dividend-{record_id}",
                    source_id="szse",
                    source_url=table_url,
                    published_at=None,
                    effective_at=datetime.combine(
                        ex_date, datetime.min.time(), tzinfo=collected_at.tzinfo
                    ),
                    collected_at=collected_at,
                    valid_from=collected_at,
                    version=f"szse-implemented-{market_date.isoformat()}",
                    content_hash=raw_ref.content_hash,
                    license_policy="personal-non-commercial-research",
                    quality_status=QualityStatus.VALID,
                    ts_code=f"{code}.SZ",
                    record_date=record_date,
                    ex_date=ex_date,
                    cash_dividend_per_share=dps,
                )
        return sorted(
            records.values(), key=lambda item: (item.ts_code, item.ex_date, item.record_id)
        )

    def _get(self, url: str) -> httpx.Response:
        self.guard.authorize(
            "szse", url, "corporate_action", "collectors.exchange_dividends"
        )
        response = self.client.get(
            url, headers={"User-Agent": "Hengce personal research"}, timeout=30
        )
        response.raise_for_status()
        return response

    @staticmethod
    def _month_reports(text: str, market_date: date) -> list[str]:
        # Five completed fiscal years can be implemented into the following calendar
        # year, so retain five years plus one month at the lower boundary.
        lower = market_date - timedelta(days=5 * 366)
        reports: list[tuple[date, str]] = []
        pattern = re.compile(
            r"value\s*:\s*['\"](?P<url>\./t\d+_\d+\.html)['\"]\s*,\s*"
            r"text\s*:\s*['\"](?P<month>\d{4}-\d{2})['\"]"
        )
        for match in pattern.finditer(text):
            month = date.fromisoformat(f"{match.group('month')}-01")
            latest_month = date(market_date.year, market_date.month, 1)
            if date(lower.year, lower.month, 1) <= month <= latest_month:
                reports.append((month, urljoin(_SZSE_INDEX_URL, match.group("url"))))
        return [url for _, url in sorted(reports)]

    @staticmethod
    def _rows(text: str) -> list[list[str]]:
        parsed: list[list[str]] = []
        for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", text, re.IGNORECASE | re.DOTALL):
            cells = [_text(cell) for cell in re.findall(
                r"<td\b[^>]*>(.*?)</td>", row, re.IGNORECASE | re.DOTALL
            )]
            if cells:
                parsed.append(cells)
        return parsed


__all__ = ["SseImplementedDividendCollector", "SzseImplementedDividendCollector"]
