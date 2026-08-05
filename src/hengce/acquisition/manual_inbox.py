from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, ValidationError, field_validator

from hengce.acquisition.downloader import validate_attachment_payload
from hengce.contracts.enums import (
    AcquisitionStatus,
    DiscoveryMethod,
    DocumentKind,
    QualityStatus,
    ReportType,
)
from hengce.contracts.pilot import AcquisitionManifestItem
from hengce.policy.guard import PolicyDenied, PolicyGuard
from hengce.raw_store.store import RawObjectStore


class _ManualSidecar(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    source_url: AnyHttpUrl
    ts_code: str
    document_kind: DocumentKind
    report_type: ReportType | None
    report_period: date | None
    published_at: datetime
    downloaded_at: datetime
    attachment_name: str
    content_type: str
    evidence: dict[str, object] | None = None

    @field_validator("published_at", "downloaded_at")
    @classmethod
    def times_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("MANUAL_TIME_MUST_BE_AWARE")
        return value


@dataclass(frozen=True, slots=True)
class InboxRejection:
    attachment_name: str
    error_code: str


@dataclass(frozen=True, slots=True)
class InboxResult:
    accepted: tuple[AcquisitionManifestItem, ...]
    rejected: tuple[InboxRejection, ...]


class ManualInbox:
    def __init__(
        self,
        *,
        guard: PolicyGuard,
        raw_store: RawObjectStore,
        clock: Callable[[], datetime],
    ) -> None:
        self.guard = guard
        self.raw_store = raw_store
        self.clock = clock

    def scan(
        self,
        root: Path,
        manifest: Sequence[AcquisitionManifestItem],
    ) -> InboxResult:
        if not root.is_dir():
            return InboxResult(accepted=(), rejected=())
        manifest_by_id = {item.item_id: item for item in manifest}
        seen_hashes = {
            item.content_hash: self._identity(item)
            for item in manifest
            if item.content_hash is not None
        }
        accepted: list[AcquisitionManifestItem] = []
        rejected: list[InboxRejection] = []

        attachments = sorted(
            path
            for path in root.iterdir()
            if path.is_file() and path.suffix.casefold() != ".json"
        )
        for attachment_path in attachments:
            sidecar_path = root / f"{attachment_path.name}.json"
            if not sidecar_path.is_file():
                rejected.append(
                    InboxRejection(
                        attachment_name=attachment_path.name,
                        error_code="MANUAL_SIDECAR_REQUIRED",
                    )
                )
                continue
            sidecar = self._read_sidecar(sidecar_path)
            if sidecar is None:
                rejected.append(
                    InboxRejection(
                        attachment_name=attachment_path.name,
                        error_code="MANUAL_SIDECAR_INVALID",
                    )
                )
                continue
            item = manifest_by_id.get(sidecar.item_id)
            if item is None:
                rejected.append(
                    InboxRejection(
                        attachment_name=attachment_path.name,
                        error_code="MANUAL_ITEM_NOT_FOUND",
                    )
                )
                continue
            if item.status in {
                AcquisitionStatus.DOWNLOADED,
                AcquisitionStatus.VERIFIED,
                AcquisitionStatus.INGESTED,
            }:
                continue
            error = self._validate_identity(item, sidecar, attachment_path)
            if error is not None:
                rejected.append(
                    InboxRejection(
                        attachment_name=attachment_path.name,
                        error_code=error,
                    )
                )
                continue
            source = self._authorized_source(item, sidecar)
            if source is None:
                rejected.append(
                    InboxRejection(
                        attachment_name=attachment_path.name,
                        error_code="MANUAL_SOURCE_POLICY_DENIED",
                    )
                )
                continue
            try:
                payload = attachment_path.read_bytes()
            except OSError:
                rejected.append(
                    InboxRejection(
                        attachment_name=attachment_path.name,
                        error_code="MANUAL_ATTACHMENT_UNREADABLE",
                    )
                )
                continue
            if not validate_attachment_payload(
                attachment_path.name,
                sidecar.content_type,
                payload,
            ):
                rejected.append(
                    InboxRejection(
                        attachment_name=attachment_path.name,
                        error_code="ATTACHMENT_TYPE_INVALID",
                    )
                )
                continue
            content_hash = hashlib.sha256(payload).hexdigest()
            identity = self._identity(item)
            if content_hash in seen_hashes and seen_hashes[content_hash] != identity:
                rejected.append(
                    InboxRejection(
                        attachment_name=attachment_path.name,
                        error_code="MANUAL_CONTENT_IDENTITY_CONFLICT",
                    )
                )
                continue
            seen_hashes[content_hash] = identity
            reference = self.raw_store.put(
                source_id=source,
                source_url=str(sidecar.source_url),
                collected_at=sidecar.downloaded_at,
                content_type=sidecar.content_type,
                payload=payload,
            )
            accepted.append(
                AcquisitionManifestItem.model_validate(
                    {
                        **item.model_dump(),
                        "source_id": source,
                        "status": AcquisitionStatus.DOWNLOADED,
                        "source_url": sidecar.source_url,
                        "discovery_method": DiscoveryMethod.MANUAL_IMPORT,
                        "published_at": sidecar.published_at,
                        "collected_at": sidecar.downloaded_at,
                        "content_hash": reference.content_hash,
                        "version": (
                            f"{sidecar.published_at.isoformat()}-"
                            f"{reference.content_hash[:12]}"
                        ),
                        "raw_object_hash": reference.content_hash,
                        "quality_status": QualityStatus.UNVERIFIED,
                        "error_code": None,
                        "attempt_count": item.attempt_count + 1,
                    }
                )
            )
        return InboxResult(
            accepted=tuple(accepted),
            rejected=tuple(rejected),
        )

    @staticmethod
    def _read_sidecar(path: Path) -> _ManualSidecar | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return _ManualSidecar.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValidationError):
            return None

    def _validate_identity(
        self,
        item: AcquisitionManifestItem,
        sidecar: _ManualSidecar,
        attachment_path: Path,
    ) -> str | None:
        if (
            sidecar.attachment_name != attachment_path.name
            or Path(sidecar.attachment_name).name != sidecar.attachment_name
            or sidecar.ts_code != item.ts_code
            or sidecar.document_kind is not item.document_kind
            or sidecar.report_type is not item.report_type
            or sidecar.report_period != item.report_period
        ):
            return "MANUAL_IDENTITY_MISMATCH"
        if (
            sidecar.published_at > item.report_cutoff_at
            or sidecar.downloaded_at < sidecar.published_at
            or sidecar.downloaded_at > self.clock()
        ):
            return "MANUAL_TIMELINE_INVALID"
        return None

    def _authorized_source(
        self,
        item: AcquisitionManifestItem,
        sidecar: _ManualSidecar,
    ) -> str | None:
        for source_id in dict.fromkeys((item.source_id, "cninfo")):
            purpose = "financial_pdf" if source_id == "cninfo" else "xbrl"
            try:
                self.guard.validate(
                    source_id,
                    str(sidecar.source_url),
                    purpose,
                    "acquisition.manual_inbox",
                )
            except PolicyDenied:
                continue
            return source_id
        return None

    @staticmethod
    def _identity(item: AcquisitionManifestItem) -> tuple[object, ...]:
        return (
            item.item_id,
            item.ts_code,
            item.document_kind,
            item.report_type,
            item.report_period,
        )


__all__ = ["InboxRejection", "InboxResult", "ManualInbox"]
