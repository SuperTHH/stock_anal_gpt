from datetime import UTC, date, datetime
from pathlib import Path

from hengce.acquisition.discovery import VisiblePublicListingResolver
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
from hengce.state.repository import StateRepository

FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "acquisition"
CUTOFF = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)


def guard(tmp_path: Path, source_id: str) -> PolicyGuard:
    domain = "www.sse.com.cn" if source_id == "sse" else "www.szse.cn"
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    repository.upsert_policy(
        SourcePolicy(
            source_id=source_id,
            source_name=source_id,
            allowed_domains=[domain],
            allowed_schemes=["https"],
            allowed_purposes=["xbrl"],
            fetch_frequency="policy_defined",
            full_text_rule="necessary_public_attachment",
            attachment_rule="pdf_xbrl_only",
            rate_limit_per_minute=6,
            robots_policy="respect",
            terms_url=f"https://{domain}/legal/",
            terms_reviewed_at=CUTOFF,
            review_status=ReviewStatus.APPROVED,
            connection_status="UNKNOWN",
            enabled=True,
        )
    )
    return PolicyGuard(repository)


def item(
    source_id: str = "sse",
    ts_code: str = "600001.SH",
    report_type: ReportType = ReportType.ANNUAL,
    report_period: date = date(2025, 12, 31),
) -> AcquisitionManifestItem:
    return AcquisitionManifestItem(
        item_id="item-1",
        universe_id="pilot-2026-07-22",
        ts_code=ts_code,
        document_kind=DocumentKind.PERIODIC_REPORT,
        report_type=report_type,
        report_period=report_period,
        source_id=source_id,
        report_cutoff_at=CUTOFF,
        status=AcquisitionStatus.PLANNED,
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
        error_code=None,
        attempt_count=0,
    )


def test_visible_listing_prefers_matching_xbrl_link_over_pdf(tmp_path: Path) -> None:
    """Catches selecting a PDF when a visible exchange XBRL attachment exists."""
    html = (FIXTURE_ROOT / "sse_public_listing.html").read_text(encoding="utf-8")
    resolver = VisiblePublicListingResolver(
        source_id="sse",
        page_url="https://www.sse.com.cn/disclosure/list.html",
        html=html,
        guard=guard(tmp_path, "sse"),
    )

    attachment = resolver.resolve(item())

    assert attachment is not None
    assert attachment.attachment_url == (
        "https://www.sse.com.cn/disclosure/600001-2025-annual.xbrl"
    )
    assert attachment.content_type == "application/xbrl+xml"
    assert attachment.purpose == "xbrl"


def test_listing_ignores_script_urls_cross_domain_and_identity_mismatch(
    tmp_path: Path,
) -> None:
    """Catches hidden or unrelated links being treated as an approved filing."""
    html = (FIXTURE_ROOT / "sse_public_listing.html").read_text(encoding="utf-8")
    resolver = VisiblePublicListingResolver(
        source_id="sse",
        page_url="https://www.sse.com.cn/disclosure/list.html",
        html=html,
        guard=guard(tmp_path, "sse"),
    )

    assert (
        resolver.resolve(
            item(report_period=date(2024, 12, 31))
        )
        is None
    )


def test_listing_excludes_post_cutoff_and_javascript_links(tmp_path: Path) -> None:
    """Catches a later correction leaking into the 2026-07-22 reconstruction."""
    html = (FIXTURE_ROOT / "szse_public_listing.html").read_text(encoding="utf-8")
    resolver = VisiblePublicListingResolver(
        source_id="szse",
        page_url="https://www.szse.cn/disclosure/list.html",
        html=html,
        guard=guard(tmp_path, "szse"),
    )

    attachment = resolver.resolve(
        item(
            source_id="szse",
            ts_code="000001.SZ",
            report_type=ReportType.Q1,
            report_period=date(2026, 3, 31),
        )
    )

    assert attachment is not None
    assert attachment.attachment_name == "000001-2026-q1.zip"
    assert attachment.published_at == datetime(
        2026, 4, 20, 16, tzinfo=attachment.published_at.tzinfo
    )
