from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from hengce.collectors.cninfo_reports import CninfoPeriodicReportCollector
from hengce.raw_store.store import RawObjectStore

NOW = datetime(2026, 8, 21, 21, 30, tzinfo=UTC)


class _Guard:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    def authorize(self, source_id: str, url: str, purpose: str, module: str) -> None:
        self.calls.append((source_id, url, purpose, module))


def test_cninfo_discovery_excludes_summary_and_keeps_original_and_correction(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                request=request,
                json={"stockList": [{"code": "000001", "orgId": "gssz0000001"}]},
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "announcements": [
                    {
                        "announcementTitle": "2025年年度报告摘要",
                        "announcementTime": 1_774_022_400_000,
                        "adjunctUrl": "summary.PDF",
                        "announcementId": "1",
                    },
                    {
                        "announcementTitle": "2025年年度报告",
                        "announcementTime": 1_774_022_400_000,
                        "adjunctUrl": "original.PDF",
                        "announcementId": "2",
                    },
                    {
                        "announcementTitle": "2025年年度报告（更正后）",
                        "announcementTime": 1_774_108_800_000,
                        "adjunctUrl": "corrected.PDF",
                        "announcementId": "3",
                    },
                    {
                        "announcementTitle": "2025 Annual Report",
                        "announcementTime": 1_774_108_800_000,
                        "adjunctUrl": "english.PDF",
                        "announcementId": "4",
                    },
                ]
            },
        )

    guard = _Guard()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        reports = CninfoPeriodicReportCollector(
            client=client,
            guard=guard,  # type: ignore[arg-type]
            raw_store=RawObjectStore(tmp_path / "raw"),
            clock=lambda: NOW,
        ).reports("000001.SZ")

    assert [item.announcement_id for item in reports] == ["2", "3"]
    assert reports[-1].attachment_url.endswith("corrected.PDF")
    assert [call[2] for call in guard.calls] == ["filing", "filing"]
    assert len(tuple((tmp_path / "raw" / "provenance").glob("*.json"))) == 2


def test_cninfo_dividend_discovery_filters_year_implementation_and_b_share(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                request=request,
                json={"stockList": [{"code": "000001", "orgId": "gssz0000001"}]},
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "announcements": [
                    {
                        "announcementTitle": "2021年年度权益分派实施公告",
                        "announcementTime": 1_655_827_200_000,
                        "adjunctUrl": "a-share.PDF",
                        "announcementId": "10",
                    },
                    {
                        "announcementTitle": "2021年年度B股权益分派实施公告",
                        "announcementTime": 1_655_827_200_000,
                        "adjunctUrl": "b-share.PDF",
                        "announcementId": "11",
                    },
                    {
                        "announcementTitle": "2020年年度权益分派实施公告",
                        "announcementTime": 1_655_827_200_000,
                        "adjunctUrl": "wrong-year.PDF",
                        "announcementId": "12",
                    },
                ]
            },
        )

    guard = _Guard()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        reports = CninfoPeriodicReportCollector(
            client=client,
            guard=guard,  # type: ignore[arg-type]
            raw_store=RawObjectStore(tmp_path / "raw"),
            clock=lambda: NOW,
        ).dividend_announcements("000001.SZ", 2021)

    assert [item.announcement_id for item in reports] == ["10"]


def test_cninfo_dividend_discovery_falls_back_to_profit_distribution_plan(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                request=request,
                json={"stockList": [{"code": "000543", "orgId": "gssz0000543"}]},
            )
        form = parse_qs(request.content.decode(), keep_blank_values=True)
        if form["searchkey"] != [""]:
            return httpx.Response(200, request=request, json={"announcements": []})
        if form["pageNum"] == ["1"]:
            return httpx.Response(
                200,
                request=request,
                json={
                    "announcements": [],
                    "hasMore": True,
                    "totalpages": 1,
                },
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "announcements": [
                    {
                        "announcementTitle": "关于2021年度利润分配预案的公告",
                        "announcementTime": 1_651_094_400_000,
                        "adjunctUrl": "no-dividend.PDF",
                        "announcementId": "20",
                    }
                ],
                "hasMore": False,
                "totalpages": 1,
            },
        )

    guard = _Guard()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        reports = CninfoPeriodicReportCollector(
            client=client,
            guard=guard,  # type: ignore[arg-type]
            raw_store=RawObjectStore(tmp_path / "raw"),
            clock=lambda: NOW,
        ).dividend_announcements("000543.SZ", 2021)

    assert [item.announcement_id for item in reports] == ["20"]


def test_cninfo_dividend_discovery_accepts_official_annual_summary_for_no_dividend(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                request=request,
                json={"stockList": [{"code": "000899", "orgId": "gssz0000899"}]},
            )
        form = parse_qs(request.content.decode(), keep_blank_values=True)
        if form["searchkey"] != [""]:
            return httpx.Response(200, request=request, json={"announcements": []})
        return httpx.Response(
            200,
            request=request,
            json={
                "announcements": [
                    {
                        "announcementTitle": "赣能股份2021年度报告摘要",
                        "announcementTime": 1_650_816_000_000,
                        "adjunctUrl": "annual-summary.PDF",
                        "announcementId": "21",
                    }
                ],
                "hasMore": True,
                "totalpages": 3,
            },
        )

    guard = _Guard()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        reports = CninfoPeriodicReportCollector(
            client=client,
            guard=guard,  # type: ignore[arg-type]
            raw_store=RawObjectStore(tmp_path / "raw"),
            clock=lambda: NOW,
        ).dividend_announcements("000899.SZ", 2021)

    assert [item.announcement_id for item in reports] == ["21"]
    assert reports[0].title == "赣能股份2021年度报告摘要"
