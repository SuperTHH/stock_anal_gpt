from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urljoin

import httpx
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from hengce.acquisition.discovery import PublicAttachment
from hengce.contracts.enums import AcquisitionStatus
from hengce.contracts.pilot import AcquisitionManifestItem
from hengce.financials.package import LocalAttachmentInspector
from hengce.policy.guard import PolicyDenied, PolicyGuard
from hengce.raw_store.store import RawObjectStore

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_RESTRICTED_STATUSES = frozenset({401, 403, 429})
_XML_CONTENT_TYPES = frozenset(
    {"application/xbrl+xml", "application/xml", "text/xml"}
)
_ALLOWED_CONTENT_TYPES = _XML_CONTENT_TYPES | frozenset(
    {"application/zip", "application/pdf"}
)


@dataclass(frozen=True, slots=True)
class DownloadResult:
    status: AcquisitionStatus
    error_code: str | None = None
    final_url: str | None = None
    content_type: str | None = None
    content_length: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    collected_at: datetime | None = None
    content_hash: str | None = None
    raw_object_hash: str | None = None
    payload_path: str | None = None


class ApprovedAttachmentDownloader:
    def __init__(
        self,
        *,
        client: httpx.Client,
        guard: PolicyGuard,
        raw_store: RawObjectStore,
        clock: Callable[[], datetime],
        max_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        self.client = client
        self.guard = guard
        self.raw_store = raw_store
        self.clock = clock
        self.max_bytes = max_bytes
        self._restricted_sources: set[str] = set()

    def fetch(
        self,
        item: AcquisitionManifestItem,
        attachment: PublicAttachment,
    ) -> DownloadResult:
        if (
            attachment.source_id != item.source_id
            or str(item.source_url) != attachment.attachment_url
        ):
            return self._rejected("ATTACHMENT_IDENTITY_MISMATCH")
        if attachment.source_id in self._restricted_sources:
            return self._manual()

        response: httpx.Response | None = None
        final_url: str | None = None
        for attempt in range(2):
            try:
                response, final_url = self._request_with_redirects(attachment)
                break
            except PolicyDenied:
                return self._rejected("ATTACHMENT_POLICY_DENIED")
            except httpx.TransportError:
                if attempt == 1:
                    return self._rejected("ATTACHMENT_DOWNLOAD_FAILED")

        assert response is not None
        assert final_url is not None
        if response.status_code in _RESTRICTED_STATUSES:
            return self._trip_breaker(attachment.source_id)
        if response.status_code < 200 or response.status_code >= 300:
            return self._rejected("ATTACHMENT_HTTP_ERROR", final_url=final_url)

        declared_type = _normalize_content_type(attachment.content_type)
        response_type = _normalize_content_type(
            response.headers.get("Content-Type", "")
        )
        payload = response.content
        if _looks_restricted_html(response_type, payload):
            return self._trip_breaker(attachment.source_id)
        if declared_type not in _ALLOWED_CONTENT_TYPES or response_type != declared_type:
            return self._rejected(
                "ATTACHMENT_TYPE_INVALID",
                final_url=final_url,
                content_type=response_type,
            )
        if self._declared_too_large(response) or len(payload) > self.max_bytes:
            return self._rejected(
                "ATTACHMENT_SIZE_EXCEEDED",
                final_url=final_url,
                content_type=response_type,
            )
        if not validate_attachment_payload(
            attachment.attachment_name,
            response_type,
            payload,
        ):
            return self._rejected(
                "ATTACHMENT_TYPE_INVALID",
                final_url=final_url,
                content_type=response_type,
            )

        collected_at = self.clock()
        reference = self.raw_store.put(
            source_id=attachment.source_id,
            source_url=final_url,
            collected_at=collected_at,
            content_type=response_type,
            payload=payload,
        )
        return DownloadResult(
            status=AcquisitionStatus.DOWNLOADED,
            final_url=final_url,
            content_type=response_type,
            content_length=len(payload),
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
            collected_at=collected_at,
            content_hash=reference.content_hash,
            raw_object_hash=reference.content_hash,
            payload_path=reference.payload_path,
        )

    def _request_with_redirects(
        self,
        attachment: PublicAttachment,
    ) -> tuple[httpx.Response, str]:
        url = attachment.attachment_url
        for redirect_count in range(6):
            self.guard.authorize(
                attachment.source_id,
                url,
                attachment.purpose,
                "acquisition.downloader",
            )
            response = self.client.get(
                url,
                follow_redirects=False,
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
            if response.status_code not in _REDIRECT_STATUSES:
                return response, url
            location = response.headers.get("Location")
            if not location or redirect_count == 5:
                return response, url
            url = urljoin(url, location)
        raise AssertionError("redirect loop bound is unreachable")

    def _declared_too_large(self, response: httpx.Response) -> bool:
        raw_length = response.headers.get("Content-Length")
        if raw_length is None:
            return False
        try:
            return int(raw_length) > self.max_bytes
        except ValueError:
            return True

    def _trip_breaker(self, source_id: str) -> DownloadResult:
        self._restricted_sources.add(source_id)
        return self._manual()

    @staticmethod
    def _manual() -> DownloadResult:
        return DownloadResult(
            status=AcquisitionStatus.AWAITING_MANUAL,
            error_code="SOURCE_ACCESS_RESTRICTED",
        )

    @staticmethod
    def _rejected(
        error_code: str,
        *,
        final_url: str | None = None,
        content_type: str | None = None,
    ) -> DownloadResult:
        return DownloadResult(
            status=AcquisitionStatus.REJECTED,
            error_code=error_code,
            final_url=final_url,
            content_type=content_type,
        )


def _normalize_content_type(content_type: str) -> str:
    return content_type.partition(";")[0].strip().lower()


def _looks_restricted_html(content_type: str, payload: bytes) -> bool:
    return content_type in {"text/html", "application/xhtml+xml"}


def validate_attachment_payload(
    attachment_name: str,
    content_type: str,
    payload: bytes,
) -> bool:
    normalized_type = _normalize_content_type(content_type)
    if normalized_type == "application/pdf":
        if not payload.startswith(b"%PDF-"):
            return False
        try:
            reader = PdfReader(BytesIO(payload), strict=True)
            len(reader.pages)
        except (OSError, PdfReadError, ValueError):
            return False
        return reader.trailer.get("/Root") is not None
    suffix = Path(attachment_name).suffix.lower()
    if normalized_type in _XML_CONTENT_TYPES and suffix not in {".xml", ".xbrl"}:
        suffix = ".xbrl"
    elif normalized_type == "application/zip" and suffix != ".zip":
        suffix = ".zip"
    try:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / f"attachment{suffix}"
            path.write_bytes(payload)
            LocalAttachmentInspector().validate(
                path,
                normalized_type,
                taxonomy=False,
            )
    except (OSError, ValueError):
        return False
    return True


__all__ = [
    "ApprovedAttachmentDownloader",
    "DownloadResult",
    "validate_attachment_payload",
]
