from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
        self.guard.authorize("cninfo", _QUERY_URL, "filing", "cninfo.report_query")
        exchange = "szse" if ts_code.endswith(".SZ") else "sse"
        response = self.client.post(
            _QUERY_URL,
            data={
                "pageNum": "1",
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
        reports: list[CninfoReport] = []
        for item in payload.get("announcements") or ():
            title = str(item.get("announcementTitle") or "")
            if not title or "摘要" in title or "英文" in title:
                continue
            if "年度报告" not in title and "季度报告" not in title:
                continue
            timestamp = int(item["announcementTime"]) / 1000
            reports.append(
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
        return tuple(sorted(reports, key=lambda item: (item.published_at, item.announcement_id)))

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
