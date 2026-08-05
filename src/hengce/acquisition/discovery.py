from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Protocol
from urllib.parse import unquote, urljoin, urlparse

from hengce.contracts.enums import DocumentKind, ReportType
from hengce.contracts.pilot import AcquisitionManifestItem
from hengce.policy.guard import PolicyDenied, PolicyGuard

_XBRL_CONTENT_TYPES = {
    "application/xbrl+xml",
    "application/xml",
    "text/xml",
    "application/zip",
}
_ALLOWED_CONTENT_TYPES = _XBRL_CONTENT_TYPES | {"application/pdf"}


@dataclass(frozen=True, slots=True)
class PublicAttachment:
    source_id: str
    page_url: str
    attachment_url: str
    attachment_name: str
    content_type: str
    published_at: datetime
    report_period: date
    report_type: ReportType
    purpose: str


class PublicListingResolver(Protocol):
    def resolve(
        self,
        item: AcquisitionManifestItem,
    ) -> PublicAttachment | None: ...


@dataclass(frozen=True, slots=True)
class _VisibleLink:
    attributes: dict[str, str]
    text: str


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[_VisibleLink] = []
        self._attributes: dict[str, str] | None = None
        self._text: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() == "a":
            self._attributes = {
                key.casefold(): value
                for key, value in attrs
                if value is not None
            }
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._attributes is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._attributes is not None:
            self.links.append(
                _VisibleLink(
                    attributes=self._attributes,
                    text=" ".join(self._text).strip(),
                )
            )
            self._attributes = None
            self._text = []


class VisiblePublicListingResolver:
    def __init__(
        self,
        *,
        source_id: str,
        page_url: str,
        html: str,
        guard: PolicyGuard,
    ) -> None:
        self.source_id = source_id
        self.page_url = page_url
        self.html = html
        self.guard = guard

    def resolve(
        self,
        item: AcquisitionManifestItem,
    ) -> PublicAttachment | None:
        if (
            item.source_id != self.source_id
            or item.document_kind is not DocumentKind.PERIODIC_REPORT
            or item.report_type is None
            or item.report_period is None
        ):
            return None
        try:
            self.guard.validate(
                self.source_id,
                self.page_url,
                "xbrl",
                "acquisition.public_listing",
            )
        except PolicyDenied:
            return None

        parser = _AnchorParser()
        parser.feed(self.html)
        candidates: list[PublicAttachment] = []
        for link in parser.links:
            attachment = self._attachment(link, item)
            if attachment is not None:
                candidates.append(attachment)
        if not candidates:
            return None
        candidates.sort(
            key=lambda attachment: (
                attachment.content_type not in _XBRL_CONTENT_TYPES,
                attachment.attachment_url,
            )
        )
        return candidates[0]

    def _attachment(
        self,
        link: _VisibleLink,
        item: AcquisitionManifestItem,
    ) -> PublicAttachment | None:
        attributes = link.attributes
        href = attributes.get("href")
        if (
            not href
            or not link.text
            or attributes.get("data-ts-code") != item.ts_code
            or attributes.get("data-report-type") != item.report_type.value
            or attributes.get("data-report-period") != item.report_period.isoformat()
        ):
            return None
        attachment_url = urljoin(self.page_url, href)
        if urlparse(attachment_url).scheme != "https":
            return None
        content_type = attributes.get("type", "").partition(";")[0].strip().lower()
        if content_type not in _ALLOWED_CONTENT_TYPES:
            return None
        try:
            published_at = datetime.fromisoformat(
                attributes["data-published-at"]
            )
        except (KeyError, ValueError):
            return None
        if (
            published_at.tzinfo is None
            or published_at.utcoffset() is None
            or published_at > item.report_cutoff_at
        ):
            return None
        try:
            self.guard.validate(
                self.source_id,
                attachment_url,
                "xbrl",
                "acquisition.public_attachment",
            )
        except PolicyDenied:
            return None
        attachment_name = PurePosixPath(
            unquote(urlparse(attachment_url).path)
        ).name
        if not attachment_name:
            return None
        return PublicAttachment(
            source_id=self.source_id,
            page_url=self.page_url,
            attachment_url=attachment_url,
            attachment_name=attachment_name,
            content_type=content_type,
            published_at=published_at,
            report_period=item.report_period,
            report_type=item.report_type,
            purpose="xbrl",
        )


__all__ = [
    "PublicAttachment",
    "PublicListingResolver",
    "VisiblePublicListingResolver",
]
