import hashlib
import json
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path

from pypdf import PdfWriter

from hengce.acquisition.manual_inbox import ManualInbox
from hengce.contracts.enums import (
    AcquisitionStatus,
    DiscoveryMethod,
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
DOWNLOADED = datetime(2026, 7, 30, 8, tzinfo=UTC)
SCANNED = datetime(2026, 7, 30, 9, tzinfo=UTC)
XML = b'<?xml version="1.0" encoding="UTF-8"?><xbrl></xbrl>'


def fictional_pdf() -> bytes:
    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(stream)
    return stream.getvalue()


def manifest_item(
    *,
    item_id: str = "item-1",
    ts_code: str = "600001.SH",
) -> AcquisitionManifestItem:
    return AcquisitionManifestItem(
        item_id=item_id,
        universe_id="pilot-2026-07-22",
        ts_code=ts_code,
        document_kind=DocumentKind.PERIODIC_REPORT,
        report_type=ReportType.ANNUAL,
        report_period=date(2025, 12, 31),
        source_id="sse",
        report_cutoff_at=CUTOFF,
        status=AcquisitionStatus.AWAITING_MANUAL,
        source_url=None,
        discovery_method=None,
        published_at=None,
        effective_at=None,
        collected_at=None,
        content_hash=None,
        version=None,
        supersedes_id=None,
        raw_object_hash=None,
        quality_status=QualityStatus.MISSING,
        error_code="PUBLIC_LINK_NOT_DISCOVERABLE",
        attempt_count=1,
    )


def policy(
    *,
    source_id: str,
    domain: str,
    purposes: list[str],
) -> SourcePolicy:
    return SourcePolicy(
        source_id=source_id,
        source_name=source_id,
        allowed_domains=[domain],
        allowed_schemes=["https"],
        allowed_purposes=purposes,
        fetch_frequency="manual",
        full_text_rule="necessary_public_attachment",
        attachment_rule="pdf_xbrl_only",
        rate_limit_per_minute=10,
        robots_policy="respect",
        terms_url=f"https://{domain}/terms",
        terms_reviewed_at=SCANNED,
        review_status=ReviewStatus.APPROVED,
        connection_status="UNKNOWN",
        enabled=True,
    )


def inbox(tmp_path: Path) -> ManualInbox:
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()
    state.upsert_policies(
        [
            policy(
                source_id="sse",
                domain="www.sse.com.cn",
                purposes=["xbrl", "financial_pdf"],
            ).model_copy(
                update={
                    "allowed_domains": ["www.sse.com.cn", "static.sse.com.cn"],
                    "domain_purposes": {
                        "www.sse.com.cn": ["xbrl"],
                        "static.sse.com.cn": ["financial_pdf"],
                    },
                }
            ),
            policy(
                source_id="szse",
                domain="disc.static.szse.cn",
                purposes=["financial_pdf"],
            ),
            policy(
                source_id="cninfo",
                domain="static.cninfo.com.cn",
                purposes=["financial_pdf"],
            ),
        ]
    )
    return ManualInbox(
        guard=PolicyGuard(state, clock=lambda: SCANNED),
        raw_store=RawObjectStore(tmp_path / "raw"),
        clock=lambda: SCANNED,
    )


def write_pair(
    root: Path,
    *,
    attachment_name: str,
    payload: bytes,
    item: AcquisitionManifestItem,
    source_url: str,
    content_type: str,
    overrides: dict[str, object] | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / attachment_name).write_bytes(payload)
    sidecar: dict[str, object] = {
        "item_id": item.item_id,
        "source_url": source_url,
        "ts_code": item.ts_code,
        "document_kind": item.document_kind.value,
        "report_type": item.report_type.value if item.report_type else None,
        "report_period": item.report_period.isoformat() if item.report_period else None,
        "published_at": "2026-03-30T10:00:00+00:00",
        "downloaded_at": DOWNLOADED.isoformat(),
        "attachment_name": attachment_name,
        "content_type": content_type,
    }
    sidecar.update(overrides or {})
    (root / f"{attachment_name}.json").write_text(
        json.dumps(sidecar),
        encoding="utf-8",
    )


def test_scan_imports_valid_xbrl_and_cninfo_pdf_through_raw_store(
    tmp_path: Path,
) -> None:
    """Catches a manual path bypassing policy, validation, or immutable storage."""
    root = tmp_path / "inbox"
    first = manifest_item()
    second = manifest_item(item_id="item-2", ts_code="600002.SH")
    write_pair(
        root,
        attachment_name="opaque-one.xbrl",
        payload=XML,
        item=first,
        source_url="https://www.sse.com.cn/disclosure/opaque-one.xbrl",
        content_type="application/xbrl+xml",
    )
    write_pair(
        root,
        attachment_name="opaque-two.pdf",
        payload=fictional_pdf(),
        item=second,
        source_url="https://static.cninfo.com.cn/finalpage/opaque-two.pdf",
        content_type="application/pdf",
    )

    result = inbox(tmp_path).scan(root, [first, second])

    assert result.rejected == ()
    assert len(result.accepted) == 2
    by_id = {item.item_id: item for item in result.accepted}
    assert by_id["item-1"].discovery_method is DiscoveryMethod.MANUAL_IMPORT
    assert by_id["item-2"].source_id == "cninfo"
    assert all(item.status is AcquisitionStatus.DOWNLOADED for item in result.accepted)
    assert all(item.content_hash == item.raw_object_hash for item in result.accepted)
    assert len(list((tmp_path / "raw").rglob("payload.bin"))) == 2


def test_scan_preserves_explicit_exchange_pdf_source(tmp_path: Path) -> None:
    root = tmp_path / "inbox"
    item = manifest_item(ts_code="000001.SZ")
    write_pair(
        root,
        attachment_name="official.pdf",
        payload=fictional_pdf(),
        item=item,
        source_url="https://disc.static.szse.cn/disc/report.PDF",
        content_type="application/pdf",
        overrides={"source_id": "szse"},
    )

    result = inbox(tmp_path).scan(root, [item])

    assert result.rejected == ()
    assert [accepted.source_id for accepted in result.accepted] == ["szse"]


def test_explicit_pdf_source_must_match_url_policy(tmp_path: Path) -> None:
    root = tmp_path / "inbox"
    item = manifest_item(ts_code="000001.SZ")
    write_pair(
        root,
        attachment_name="mismatch.pdf",
        payload=fictional_pdf(),
        item=item,
        source_url="https://static.cninfo.com.cn/finalpage/report.pdf",
        content_type="application/pdf",
        overrides={"source_id": "szse"},
    )

    result = inbox(tmp_path).scan(root, [item])

    assert result.accepted == ()
    assert [rejection.error_code for rejection in result.rejected] == [
        "MANUAL_SOURCE_POLICY_DENIED"
    ]


def test_filename_is_not_used_as_document_identity(tmp_path: Path) -> None:
    """Catches inferring the issuer or period from a plausible-looking filename."""
    root = tmp_path / "inbox"
    item = manifest_item()
    write_pair(
        root,
        attachment_name="000999_2022_annual.xbrl",
        payload=XML,
        item=item,
        source_url="https://www.sse.com.cn/disclosure/opaque.xbrl",
        content_type="application/xbrl+xml",
    )

    result = inbox(tmp_path).scan(root, [item])

    assert [accepted.item_id for accepted in result.accepted] == ["item-1"]


def test_missing_sidecar_unknown_item_and_identity_mismatch_are_rejected(
    tmp_path: Path,
) -> None:
    """Catches unproven or mismatched files entering the financial fact chain."""
    root = tmp_path / "inbox"
    item = manifest_item()
    root.mkdir()
    (root / "orphan.xbrl").write_bytes(XML)
    write_pair(
        root,
        attachment_name="unknown.xbrl",
        payload=XML,
        item=item,
        source_url="https://www.sse.com.cn/disclosure/unknown.xbrl",
        content_type="application/xbrl+xml",
        overrides={"item_id": "unknown-item"},
    )
    write_pair(
        root,
        attachment_name="mismatch.xbrl",
        payload=XML + b" ",
        item=item,
        source_url="https://www.sse.com.cn/disclosure/mismatch.xbrl",
        content_type="application/xbrl+xml",
        overrides={"report_period": "2024-12-31"},
    )

    result = inbox(tmp_path).scan(root, [item])

    assert result.accepted == ()
    assert {rejection.error_code for rejection in result.rejected} == {
        "MANUAL_SIDECAR_REQUIRED",
        "MANUAL_ITEM_NOT_FOUND",
        "MANUAL_IDENTITY_MISMATCH",
    }
    assert not list((tmp_path / "raw").rglob("payload.bin"))


def test_policy_denial_and_cross_identity_duplicate_are_rejected(
    tmp_path: Path,
) -> None:
    """Catches unapproved URLs and one payload being asserted as two filings."""
    root = tmp_path / "inbox"
    first = manifest_item()
    second = manifest_item(item_id="item-2", ts_code="600002.SH")
    write_pair(
        root,
        attachment_name="a.xbrl",
        payload=XML,
        item=first,
        source_url="https://www.sse.com.cn/disclosure/a.xbrl",
        content_type="application/xbrl+xml",
    )
    write_pair(
        root,
        attachment_name="b.xbrl",
        payload=XML,
        item=second,
        source_url="https://www.sse.com.cn/disclosure/b.xbrl",
        content_type="application/xbrl+xml",
    )
    write_pair(
        root,
        attachment_name="commercial.xbrl",
        payload=XML + b" ",
        item=second,
        source_url="https://commercial.example/report.xbrl",
        content_type="application/xbrl+xml",
    )

    result = inbox(tmp_path).scan(root, [first, second])

    assert [accepted.item_id for accepted in result.accepted] == ["item-1"]
    assert {rejection.error_code for rejection in result.rejected} == {
        "MANUAL_CONTENT_IDENTITY_CONFLICT",
        "MANUAL_SOURCE_POLICY_DENIED",
    }


def test_same_official_attachment_can_support_two_kinds_for_same_issuer(
    tmp_path: Path,
) -> None:
    """One official annual report may evidence both financials and a dividend year."""
    root = tmp_path / "inbox"
    payload = fictional_pdf()
    content_hash = hashlib.sha256(payload).hexdigest()
    source_url = "https://static.sse.com.cn/disclosure/annual-report.pdf"
    existing = manifest_item().model_copy(
        update={
            "status": AcquisitionStatus.INGESTED,
            "source_url": source_url,
            "content_hash": content_hash,
            "raw_object_hash": content_hash,
            "quality_status": QualityStatus.VALID,
        }
    )
    dividend = manifest_item(item_id="dividend-2025").model_copy(
        update={
            "document_kind": DocumentKind.DIVIDEND_RECORD,
            "report_type": None,
        }
    )
    write_pair(
        root,
        attachment_name="annual-as-dividend.pdf",
        payload=payload,
        item=dividend,
        source_url=source_url,
        content_type="application/pdf",
    )

    result = inbox(tmp_path).scan(root, [existing, dividend])

    assert result.rejected == ()
    assert [item.item_id for item in result.accepted] == ["dividend-2025"]


def test_future_download_timestamp_is_rejected(tmp_path: Path) -> None:
    """Catches imported provenance claiming collection after the actual scan."""
    root = tmp_path / "inbox"
    item = manifest_item()
    write_pair(
        root,
        attachment_name="future.xbrl",
        payload=XML,
        item=item,
        source_url="https://www.sse.com.cn/disclosure/future.xbrl",
        content_type="application/xbrl+xml",
        overrides={"downloaded_at": "2026-07-31T09:00:00+00:00"},
    )

    result = inbox(tmp_path).scan(root, [item])

    assert result.accepted == ()
    assert [rejection.error_code for rejection in result.rejected] == ["MANUAL_TIMELINE_INVALID"]


def test_reviewed_evidence_block_survives_acquisition_sidecar_validation(
    tmp_path: Path,
) -> None:
    """Catches the manual scanner rejecting versioned action/risk review payloads."""
    root = tmp_path / "inbox"
    item = manifest_item()
    write_pair(
        root,
        attachment_name="reviewed.xbrl",
        payload=XML,
        item=item,
        source_url="https://www.sse.com.cn/disclosure/reviewed.xbrl",
        content_type="application/xbrl+xml",
        overrides={
            "evidence": {
                "schema_version": "official-risk-screen-v1",
                "attachment_sha256": "a" * 64,
            }
        },
    )

    result = inbox(tmp_path).scan(root, [item])

    assert [accepted.item_id for accepted in result.accepted] == [item.item_id]
    assert result.rejected == ()


def test_already_downloaded_attachment_is_not_imported_or_counted_again(
    tmp_path: Path,
) -> None:
    """Catches a stage-7 retry turning the same Raw object into a new transition."""
    root = tmp_path / "inbox"
    planned = manifest_item()
    downloaded = AcquisitionManifestItem.model_validate(
        {
            **planned.model_dump(),
            "status": AcquisitionStatus.DOWNLOADED,
            "source_url": "https://www.sse.com.cn/disclosure/reviewed.xbrl",
            "discovery_method": DiscoveryMethod.MANUAL_IMPORT,
            "published_at": "2026-03-30T10:00:00+00:00",
            "collected_at": DOWNLOADED,
            "content_hash": "a" * 64,
            "version": "v1",
            "raw_object_hash": "a" * 64,
            "quality_status": QualityStatus.UNVERIFIED,
            "error_code": None,
        }
    )
    write_pair(
        root,
        attachment_name="reviewed.xbrl",
        payload=XML,
        item=downloaded,
        source_url="https://www.sse.com.cn/disclosure/reviewed.xbrl",
        content_type="application/xbrl+xml",
    )

    result = inbox(tmp_path).scan(root, [downloaded])

    assert result.accepted == ()
    assert result.rejected == ()
