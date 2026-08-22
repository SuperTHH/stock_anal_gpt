from datetime import UTC, datetime
from pathlib import Path

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
