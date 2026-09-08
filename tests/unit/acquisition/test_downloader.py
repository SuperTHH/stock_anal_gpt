from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from pypdf.errors import PdfReadError

from hengce.acquisition.discovery import PublicAttachment
from hengce.acquisition.downloader import (
    ApprovedAttachmentDownloader,
    validate_attachment_payload,
)
from hengce.contracts.enums import (
    AcquisitionStatus,
    DocumentKind,
    QualityStatus,
    ReportType,
    ReviewStatus,
)
from hengce.contracts.pilot import AcquisitionManifestItem
from hengce.contracts.policy import SourcePolicy
from hengce.policy.guard import PolicyGuard
from hengce.raw_store.store import RawObjectStore
from hengce.state.repository import StateRepository

CUTOFF = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
COLLECTED = datetime(2026, 7, 30, 9, tzinfo=UTC)
XML = b'<?xml version="1.0" encoding="UTF-8"?><xbrl></xbrl>'


def item() -> AcquisitionManifestItem:
    return AcquisitionManifestItem(
        item_id="item-1",
        universe_id="pilot-2026-07-22",
        ts_code="600001.SH",
        document_kind=DocumentKind.PERIODIC_REPORT,
        report_type=ReportType.ANNUAL,
        report_period=date(2025, 12, 31),
        source_id="sse",
        report_cutoff_at=CUTOFF,
        status=AcquisitionStatus.DISCOVERED,
        source_url="https://www.sse.com.cn/disclosure/example.xbrl",
        discovery_method="PUBLIC_PAGE",
        published_at=datetime(2026, 3, 30, 10, tzinfo=UTC),
        effective_at=None,
        collected_at=None,
        content_hash=None,
        version=None,
        supersedes_id=None,
        raw_object_hash=None,
        quality_status=QualityStatus.MISSING,
        error_code=None,
        attempt_count=1,
    )


def attachment() -> PublicAttachment:
    return PublicAttachment(
        source_id="sse",
        page_url="https://www.sse.com.cn/disclosure/list.html",
        attachment_url="https://www.sse.com.cn/disclosure/example.xbrl",
        attachment_name="example.xbrl",
        content_type="application/xbrl+xml",
        published_at=datetime(2026, 3, 30, 10, tzinfo=UTC),
        report_period=date(2025, 12, 31),
        report_type=ReportType.ANNUAL,
        purpose="xbrl",
    )


def guard(tmp_path: Path) -> PolicyGuard:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    repository.upsert_policy(
        SourcePolicy(
            source_id="sse",
            source_name="上海证券交易所",
            allowed_domains=["www.sse.com.cn"],
            allowed_schemes=["https"],
            allowed_purposes=["xbrl"],
            fetch_frequency="policy_defined",
            full_text_rule="necessary_public_attachment",
            attachment_rule="pdf_xbrl_only",
            rate_limit_per_minute=6000,
            robots_policy="respect",
            terms_url="https://www.sse.com.cn/home/legal/",
            terms_reviewed_at=CUTOFF,
            review_status=ReviewStatus.APPROVED,
            connection_status="UNKNOWN",
            enabled=True,
        )
    )
    return PolicyGuard(
        repository,
        clock=lambda: COLLECTED,
        sleeper=lambda _: None,
    )


def downloader(
    tmp_path: Path,
    handler,
    *,
    max_bytes: int = 100 * 1024 * 1024,
) -> ApprovedAttachmentDownloader:
    return ApprovedAttachmentDownloader(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        guard=guard(tmp_path),
        raw_store=RawObjectStore(tmp_path / "raw"),
        clock=lambda: COLLECTED,
        max_bytes=max_bytes,
    )


def test_success_validates_then_writes_immutable_raw_object(tmp_path: Path) -> None:
    """Catches storing response bytes before their type and XML safety are validated."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/xbrl+xml",
                "ETag": '"fixture-v1"',
            },
            content=XML,
            request=request,
        )

    result = downloader(tmp_path, handler).fetch(item(), attachment())

    assert result.status is AcquisitionStatus.DOWNLOADED
    assert result.error_code is None
    assert result.raw_object_hash == result.content_hash
    assert result.content_length == len(XML)
    assert result.etag == '"fixture-v1"'
    assert Path(result.payload_path or "").read_bytes() == XML


def test_redirect_target_is_policy_checked_before_second_request(tmp_path: Path) -> None:
    """Catches a permitted URL redirecting the downloader to an unapproved domain."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://commercial.example/hidden.xbrl"},
            request=request,
        )

    result = downloader(tmp_path, handler).fetch(item(), attachment())

    assert result.status is AcquisitionStatus.REJECTED
    assert result.error_code == "ATTACHMENT_POLICY_DENIED"
    assert requested == ["https://www.sse.com.cn/disclosure/example.xbrl"]


def test_transport_failure_retries_once_then_succeeds(tmp_path: Path) -> None:
    """Catches either skipping the one allowed retry or retrying without a bound."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadError("temporary", request=request)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/xbrl+xml"},
            content=XML,
            request=request,
        )

    result = downloader(tmp_path, handler).fetch(item(), attachment())

    assert result.status is AcquisitionStatus.DOWNLOADED
    assert attempts == 2


def test_official_pdf_with_recoverable_xref_is_accepted_in_tolerant_mode(
    monkeypatch,
) -> None:
    class _RecoverablePdf:
        def __init__(self, _payload, *, strict: bool) -> None:
            if strict:
                raise PdfReadError("Broken xref table")
            self.pages = [object()]
            self.trailer = {"/Root": object()}

    monkeypatch.setattr(
        "hengce.acquisition.downloader.PdfReader",
        _RecoverablePdf,
    )

    assert validate_attachment_payload(
        "official.pdf",
        "application/pdf",
        b"%PDF-1.7 recoverable official payload",
    )


@pytest.mark.parametrize("status_code", [401, 403, 429])
def test_restricted_response_trips_source_breaker(
    tmp_path: Path,
    status_code: int,
) -> None:
    """Catches continuing automatic access after an explicit restriction response."""
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(status_code, request=request)

    instance = downloader(tmp_path, handler)
    first = instance.fetch(item(), attachment())
    second = instance.fetch(item(), attachment())

    assert first.status is AcquisitionStatus.AWAITING_MANUAL
    assert first.error_code == "SOURCE_ACCESS_RESTRICTED"
    assert second.error_code == "SOURCE_ACCESS_RESTRICTED"
    assert requests == 1


@pytest.mark.parametrize(
    ("headers", "content", "max_bytes", "error_code", "status"),
    [
        (
            {"Content-Type": "text/html"},
            b"<html>captcha verification</html>",
            1024,
            "SOURCE_ACCESS_RESTRICTED",
            AcquisitionStatus.AWAITING_MANUAL,
        ),
        (
            {"Content-Type": "text/html"},
            b"<html>ordinary attachment landing page</html>",
            1024,
            "SOURCE_ACCESS_RESTRICTED",
            AcquisitionStatus.AWAITING_MANUAL,
        ),
        (
            {"Content-Type": "application/pdf"},
            XML,
            1024,
            "ATTACHMENT_TYPE_INVALID",
            AcquisitionStatus.REJECTED,
        ),
        (
            {"Content-Type": "application/xbrl+xml", "Content-Length": "2048"},
            XML,
            1024,
            "ATTACHMENT_SIZE_EXCEEDED",
            AcquisitionStatus.REJECTED,
        ),
        (
            {"Content-Type": "application/xbrl+xml"},
            b"not xml",
            1024,
            "ATTACHMENT_TYPE_INVALID",
            AcquisitionStatus.REJECTED,
        ),
    ],
)
def test_invalid_or_restricted_payload_never_reaches_raw_store(
    tmp_path: Path,
    headers: dict[str, str],
    content: bytes,
    max_bytes: int,
    error_code: str,
    status: AcquisitionStatus,
) -> None:
    """Catches unsafe, oversized, or deceptive payloads becoming immutable facts."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=headers,
            content=content,
            request=request,
        )

    result = downloader(tmp_path, handler, max_bytes=max_bytes).fetch(
        item(),
        attachment(),
    )

    assert result.status is status
    assert result.error_code == error_code
    assert not list((tmp_path / "raw").rglob("payload.bin"))


@pytest.mark.parametrize(
    ("attachment_name", "content_type", "content"),
    [
        ("broken.pdf", "application/pdf", b"%PDF-1.4\nbroken"),
        ("broken.zip", "application/zip", b"PK broken"),
    ],
)
def test_structurally_damaged_declared_attachment_is_rejected(
    tmp_path: Path,
    attachment_name: str,
    content_type: str,
    content: bytes,
) -> None:
    """Catches trusting only a file signature after MIME identity already matched."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": content_type},
            content=content,
            request=request,
        )

    declared = replace(
        attachment(),
        attachment_name=attachment_name,
        content_type=content_type,
    )
    result = downloader(tmp_path, handler).fetch(item(), declared)

    assert result.status is AcquisitionStatus.REJECTED
    assert result.error_code == "ATTACHMENT_TYPE_INVALID"
    assert not list((tmp_path / "raw").rglob("payload.bin"))
