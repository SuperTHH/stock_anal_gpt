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


def test_cninfo_periodic_report_discovery_reads_later_pages(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                request=request,
                json={"stockList": [{"code": "600004", "orgId": "gssh0600004"}]},
            )
        form = parse_qs(request.content.decode(), keep_blank_values=True)
        if form["pageNum"] == ["1"]:
            return httpx.Response(
                200,
                request=request,
                json={
                    "announcements": [],
                    "hasMore": True,
                    "totalpages": 2,
                },
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "announcements": [
                    {
                        "announcementTitle": "白云机场2026年第一季度报告",
                        "announcementTime": 1_777_478_400_000,
                        "adjunctUrl": "q1-full.PDF",
                        "announcementId": "later-page",
                    }
                ],
                "hasMore": False,
                "totalpages": 2,
            },
        )

    guard = _Guard()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        reports = CninfoPeriodicReportCollector(
            client=client,
            guard=guard,  # type: ignore[arg-type]
            raw_store=RawObjectStore(tmp_path / "raw"),
            clock=lambda: NOW,
        ).reports("600004.SH")

    assert [item.announcement_id for item in reports] == ["later-page"]
    assert len(guard.calls) == 3


def test_cninfo_exact_period_fallback_excludes_summary(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                request=request,
                json={"stockList": [{"code": "600050", "orgId": "gssh0600050"}]},
            )
        form = parse_qs(request.content.decode(), keep_blank_values=True)
        assert form["searchkey"] == ["2025年年度报告"]
        return httpx.Response(
            200,
            request=request,
            json={
                "announcements": [
                    {
                        "announcementTitle": "中国联通2025年年度报告摘要",
                        "announcementTime": 1_774_022_400_000,
                        "adjunctUrl": "summary.PDF",
                        "announcementId": "summary",
                    },
                    {
                        "announcementTitle": "中国联通2025年年度报告",
                        "announcementTime": 1_774_022_400_000,
                        "adjunctUrl": "full.PDF",
                        "announcementId": "full",
                    },
                ],
                "hasMore": False,
            },
        )

    guard = _Guard()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        reports = CninfoPeriodicReportCollector(
            client=client,
            guard=guard,  # type: ignore[arg-type]
            raw_store=RawObjectStore(tmp_path / "raw"),
            clock=lambda: NOW,
        ).reports_for_period("600050.SH", "2025-12-31")

    assert [item.announcement_id for item in reports] == ["full"]


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
        if form["searchkey"] == ["权益分派实施公告"]:
            return httpx.Response(200, request=request, json={"announcements": []})
        if form["searchkey"] == ["利润分配"]:
            return httpx.Response(
                200,
                request=request,
                json={
                    "announcements": [
                        {
                            "announcementTitle": "关于利润分配方案的公告",
                            "announcementTime": 1_651_094_400_000,
                            "adjunctUrl": "no-dividend.PDF",
                            "announcementId": "20",
                        }
                    ],
                    "hasMore": False,
                    "totalpages": 0,
                },
            )
        raise AssertionError("annual fallback should not run when a plan is found")

    guard = _Guard()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        reports = CninfoPeriodicReportCollector(
            client=client,
            guard=guard,  # type: ignore[arg-type]
            raw_store=RawObjectStore(tmp_path / "raw"),
            clock=lambda: NOW,
        ).dividend_announcements("000543.SZ", 2021)

    assert [item.announcement_id for item in reports] == ["20"]


def test_cninfo_dividend_discovery_prefers_full_a_share_report_for_no_dividend(
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
        if form["pageNum"] == ["1"]:
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
                        },
                        {
                            "announcementTitle": "赣能股份H股2021年度报告",
                            "announcementTime": 1_650_816_000_000,
                            "adjunctUrl": "annual-h-share.PDF",
                            "announcementId": "22",
                        },
                    ],
                    "hasMore": True,
                    "totalpages": 3,
                },
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "announcements": [
                    {
                        "announcementTitle": "赣能股份2021年度报告",
                        "announcementTime": 1_650_816_000_000,
                        "adjunctUrl": "annual-full.PDF",
                        "announcementId": "20",
                    },
                    {
                        "announcementTitle": "赣能股份关于利润分配方案的公告",
                        "announcementTime": 1_650_816_000_000,
                        "adjunctUrl": "distribution-plan.PDF",
                        "announcementId": "19",
                    },
                ],
                "hasMore": False,
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

    assert [item.announcement_id for item in reports] == ["20", "19"]
    assert reports[-1].title == "赣能股份关于利润分配方案的公告"
