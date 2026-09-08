from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from hengce.policy.guard import PolicyGuard
from hengce.raw_store.store import RawObjectStore

_LIST_URL = "https://www.cninfo.com.cn/new/data/szse_stock.json"
_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class CninfoReport:
    ts_code: str
    title: str
    published_at: datetime
    attachment_url: str
    announcement_id: str


class CninfoPeriodicReportCollector:
    def __init__(
        self,
        *,
        client: httpx.Client,
        guard: PolicyGuard,
        raw_store: RawObjectStore,
        clock,
    ) -> None:
        self.client = client
        self.guard = guard
        self.raw_store = raw_store
        self.clock = clock
        self._organizations: dict[str, str] | None = None

    def reports(self, ts_code: str) -> tuple[CninfoReport, ...]:
        organizations = self._organization_map()
        symbol = ts_code[:6]
        org_id = organizations.get(symbol)
        if org_id is None:
            raise ValueError("CNINFO_ORGANIZATION_NOT_FOUND")
        exchange = "szse" if ts_code.endswith(".SZ") else "sse"
        reports: list[CninfoReport] = []
        page_num = 1
        while page_num <= 20:
            self.guard.authorize("cninfo", _QUERY_URL, "filing", "cninfo.report_query")
            response = self.client.post(
                _QUERY_URL,
                data={
                    "pageNum": str(page_num),
                    "pageSize": "30",
                    "column": exchange,
                    "tabName": "fulltext",
                    "plate": "sz" if exchange == "szse" else "sh",
                    "stock": f"{symbol},{org_id}",
                    "category": "category_ndbg_szsh;category_yjdbg_szsh",
                    "seDate": "2023-01-01~2026-08-21",
                },
                headers={"Referer": "https://www.cninfo.com.cn/"},
                timeout=httpx.Timeout(30, connect=10),
            )
            response.raise_for_status()
            payload = response.json()
            self.raw_store.put(
                source_id="cninfo",
                source_url=_QUERY_URL,
                collected_at=self.clock(),
                content_type="application/json",
                payload=response.content,
            )
            for item in payload.get("announcements") or ():
                title = str(item.get("announcementTitle") or "")
                if not title or "摘要" in title or "英文" in title:
                    continue
                if not any(marker in title for marker in ("年度报告", "季度报告", "年报")):
                    continue
                timestamp = int(item["announcementTime"]) / 1000
                reports.append(
                    CninfoReport(
                        ts_code=ts_code,
                        title=title,
                        published_at=datetime.fromtimestamp(timestamp, tz=_SHANGHAI),
                        attachment_url=(
                            "https://static.cninfo.com.cn/"
                            + str(item["adjunctUrl"]).lstrip("/")
                        ),
                        announcement_id=str(item["announcementId"]),
                    )
                )
            if not payload.get("hasMore"):
                break
            page_num += 1
        return tuple(sorted(reports, key=lambda item: (item.published_at, item.announcement_id)))

    def reports_for_period(
        self,
        ts_code: str,
        evidence_period: str,
    ) -> tuple[CninfoReport, ...]:
        """Use CNINFO's exact title search when the category query omits a full report."""
        period = date.fromisoformat(evidence_period)
        organizations = self._organization_map()
        symbol = ts_code[:6]
        org_id = organizations.get(symbol)
        if org_id is None:
            raise ValueError("CNINFO_ORGANIZATION_NOT_FOUND")
        exchange = "szse" if ts_code.endswith(".SZ") else "sse"
        searchkey = (
            f"{period.year}年年度报告"
            if period.month == 12
            else f"{period.year}年一季度报告"
        )
        end_year = period.year + 1 if period.month == 12 else period.year
        reports: list[CninfoReport] = []
        page_num = 1
        while page_num <= 20:
            self.guard.authorize("cninfo", _QUERY_URL, "filing", "cninfo.report_query")
            response = self.client.post(
                _QUERY_URL,
                data={
                    "pageNum": str(page_num),
                    "pageSize": "30",
                    "column": exchange,
                    "tabName": "fulltext",
                    "plate": "sz" if exchange == "szse" else "sh",
                    "stock": f"{symbol},{org_id}",
                    "category": "",
                    "seDate": f"{end_year}-01-01~{end_year}-12-31",
                    "searchkey": searchkey,
                },
                headers={"Referer": "https://www.cninfo.com.cn/"},
                timeout=httpx.Timeout(30, connect=10),
            )
            response.raise_for_status()
            payload = response.json()
            self.raw_store.put(
                source_id="cninfo",
                source_url=_QUERY_URL,
                collected_at=self.clock(),
                content_type="application/json",
                payload=response.content,
            )
            for item in payload.get("announcements") or ():
                title = str(item.get("announcementTitle") or "")
                if not title or "摘要" in title or "英文" in title:
                    continue
                timestamp = int(item["announcementTime"]) / 1000
                reports.append(
                    CninfoReport(
                        ts_code=ts_code,
                        title=title,
                        published_at=datetime.fromtimestamp(timestamp, tz=_SHANGHAI),
                        attachment_url=(
                            "https://static.cninfo.com.cn/"
                            + str(item["adjunctUrl"]).lstrip("/")
                        ),
                        announcement_id=str(item["announcementId"]),
                    )
                )
            if not payload.get("hasMore"):
                break
            page_num += 1
        return tuple(sorted(reports, key=lambda item: (item.published_at, item.announcement_id)))

    def dividend_announcements(
        self,
        ts_code: str,
        fiscal_year: int,
    ) -> tuple[CninfoReport, ...]:
        """Discover official implementation notices for one dividend fiscal year."""
        annual_report_markers = (
            f"{fiscal_year}年年度报告",
            f"{fiscal_year}年度报告",
        )
        organizations = self._organization_map()
        symbol = ts_code[:6]
        org_id = organizations.get(symbol)
        if org_id is None:
            raise ValueError("CNINFO_ORGANIZATION_NOT_FOUND")
        exchange = "szse" if ts_code.endswith(".SZ") else "sse"

        def query(
            searchkey: str,
            *,
            se_date: str | None = None,
        ) -> tuple[dict[str, Any], ...]:
            announcements: list[dict[str, Any]] = []
            page_num = 1
            last_page = 20
            while page_num <= last_page:
                self.guard.authorize("cninfo", _QUERY_URL, "filing", "cninfo.dividend_query")
                response = self.client.post(
                    _QUERY_URL,
                    data={
                        "pageNum": str(page_num),
                        "pageSize": "30",
                        "column": exchange,
                        "tabName": "fulltext",
                        "plate": "sz" if exchange == "szse" else "sh",
                        "stock": f"{symbol},{org_id}",
                        "category": "",
                        "seDate": se_date or f"{fiscal_year + 1}-01-01~{fiscal_year + 2}-12-31",
                        "searchkey": searchkey,
                    },
                    headers={"Referer": "https://www.cninfo.com.cn/"},
                    timeout=httpx.Timeout(30, connect=10),
                )
                response.raise_for_status()
                self.raw_store.put(
                    source_id="cninfo",
                    source_url=_QUERY_URL,
                    collected_at=self.clock(),
                    content_type="application/json",
                    payload=response.content,
                )
                payload = response.json()
                page_announcements = payload.get("announcements") or ()
                announcements.extend(page_announcements)
                if not searchkey and any(
                    (
                        "摘要" not in str(item.get("announcementTitle") or "")
                        and "H股" not in str(item.get("announcementTitle") or "")
                        and (
                            "利润分配预案" in str(item.get("announcementTitle") or "")
                            or "利润分配方案" in str(item.get("announcementTitle") or "")
                            or any(
                                marker in str(item.get("announcementTitle") or "")
                                for marker in annual_report_markers
                            )
                        )
                    )
                    for item in page_announcements
                ):
                    break
                reported_last_index = int(payload.get("totalpages") or 0)
                if reported_last_index:
                    last_page = min(last_page, reported_last_index + 1)
                if not payload.get("hasMore"):
                    break
                page_num += 1
            return tuple(announcements)

        results: list[CninfoReport] = []
        year_marker = f"{fiscal_year}年"
        for item in query("权益分派实施公告"):
            title = str(item.get("announcementTitle") or "")
            if (
                year_marker not in title
                or "实施公告" not in title
                or not any(marker in title for marker in ("权益分派", "利润分配", "分红派息"))
                or ("B股" in title and "A股" not in title)
            ):
                continue
            timestamp = int(item["announcementTime"]) / 1000
            results.append(
                CninfoReport(
                    ts_code=ts_code,
                    title=title,
                    published_at=datetime.fromtimestamp(timestamp, tz=_SHANGHAI),
                    attachment_url=(
                        "https://static.cninfo.com.cn/" + str(item["adjunctUrl"]).lstrip("/")
                    ),
                    announcement_id=str(item["announcementId"]),
                )
            )
        if not results:
            for item in query(
                "利润分配",
                se_date=f"{fiscal_year + 1}-01-01~{fiscal_year + 1}-05-31",
            ):
                title = str(item.get("announcementTitle") or "")
                if (
                    not any(marker in title for marker in ("利润分配预案", "利润分配方案"))
                    or "取消" in title
                ):
                    continue
                timestamp = int(item["announcementTime"]) / 1000
                results.append(
                    CninfoReport(
                        ts_code=ts_code,
                        title=title,
                        published_at=datetime.fromtimestamp(timestamp, tz=_SHANGHAI),
                        attachment_url=(
                            "https://static.cninfo.com.cn/" + str(item["adjunctUrl"]).lstrip("/")
                        ),
                        announcement_id=str(item["announcementId"]),
                    )
                )
        if not results:
            # CNINFO's title search can omit older proposals even for exact terms.
            # The bounded annual-report window is small enough to filter locally.
            for item in query(
                "",
                se_date=f"{fiscal_year + 1}-01-01~{fiscal_year + 1}-05-31",
            ):
                title = str(item.get("announcementTitle") or "")
                is_distribution_plan = any(
                    marker in title for marker in ("利润分配预案", "利润分配方案")
                )
                if (
                    (year_marker not in title and not is_distribution_plan)
                    or "摘要" in title
                    or "H股" in title
                    or (
                        not is_distribution_plan
                        and not any(marker in title for marker in annual_report_markers)
                    )
                    or "取消" in title
                ):
                    continue
                timestamp = int(item["announcementTime"]) / 1000
                results.append(
                    CninfoReport(
                        ts_code=ts_code,
                        title=title,
                        published_at=datetime.fromtimestamp(timestamp, tz=_SHANGHAI),
                        attachment_url=(
                            "https://static.cninfo.com.cn/" + str(item["adjunctUrl"]).lstrip("/")
                        ),
                        announcement_id=str(item["announcementId"]),
                    )
                )
        return tuple(
            sorted(
                results,
                key=lambda item: (
                    any(marker in item.title for marker in ("利润分配预案", "利润分配方案")),
                    item.published_at,
                    item.announcement_id,
                ),
            )
        )

    def _organization_map(self) -> dict[str, str]:
        if self._organizations is not None:
            return self._organizations
        self.guard.authorize("cninfo", _LIST_URL, "filing", "cninfo.security_list")
        response = self.client.get(_LIST_URL, timeout=httpx.Timeout(30, connect=10))
        response.raise_for_status()
        self.raw_store.put(
            source_id="cninfo",
            source_url=_LIST_URL,
            collected_at=self.clock(),
            content_type="application/json",
            payload=response.content,
        )
        payload: dict[str, Any] = response.json()
        self._organizations = {
            str(item["code"]): str(item["orgId"])
            for item in payload.get("stockList", ())
            if item.get("code") and item.get("orgId")
        }
        return self._organizations


__all__ = ["CninfoPeriodicReportCollector", "CninfoReport"]
