import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from hengce.contracts.enums import (
    AcquisitionStatus,
    ActionStatus,
    ActionType,
    DiscoveryMethod,
    DocumentKind,
    QualityStatus,
)
from hengce.contracts.pilot import (
    AcquisitionManifestItem,
    PilotUniverseMember,
    PilotUniverseSnapshot,
)
from hengce.raw_store.store import RawObjectStore
from hengce.services.official_evidence_ingestion import (
    OfficialEvidenceIngestionService,
)
from hengce.state.action_repository import CorporateActionRepository
from hengce.state.dividend_repository import AnnualDividendRepository
from hengce.state.pilot_repository import PilotRepository
from hengce.state.repository import StateRepository

SHANGHAI = ZoneInfo("Asia/Shanghai")
CUTOFF = datetime(2026, 7, 22, 21, 30, tzinfo=SHANGHAI)
KNOWN_AT = datetime(2026, 8, 5, 10, 0, tzinfo=SHANGHAI)
PUBLISHED_AT = datetime(2026, 6, 1, 9, 0, tzinfo=SHANGHAI)


def _downloaded_item(
    *,
    item_id: str,
    document_kind: DocumentKind,
    raw_hash: str,
    report_period: date | None = None,
) -> AcquisitionManifestItem:
    return AcquisitionManifestItem(
        item_id=item_id,
        universe_id="pilot-2026-07-22",
        ts_code="600001.SH",
        document_kind=document_kind,
        report_type=None,
        report_period=report_period,
        source_id="sse",
        report_cutoff_at=CUTOFF,
        status=AcquisitionStatus.DOWNLOADED,
        source_url="https://www.sse.com.cn/disclosure/official-evidence.pdf",
        discovery_method=DiscoveryMethod.MANUAL_IMPORT,
        published_at=PUBLISHED_AT,
        effective_at=None,
        collected_at=KNOWN_AT,
        content_hash=raw_hash,
        version=f"{PUBLISHED_AT.isoformat()}-{raw_hash[:12]}",
        supersedes_id=None,
        raw_object_hash=raw_hash,
        quality_status=QualityStatus.UNVERIFIED,
        error_code=None,
        attempt_count=1,
    )


def _service(
    tmp_path: Path,
    item: AcquisitionManifestItem,
) -> tuple[
    OfficialEvidenceIngestionService,
    PilotRepository,
    CorporateActionRepository,
]:
    state_path = tmp_path / "state.sqlite3"
    StateRepository(state_path).migrate()
    pilot_repository = PilotRepository(state_path)
    pilot_repository.publish_universe(
        PilotUniverseSnapshot(
            universe_id=item.universe_id,
            market_date=date(2026, 7, 22),
            report_cutoff_at=CUTOFF,
            algorithm_version="fixture-v1",
            quotas={
                "MAIN_SH": 1,
                "MAIN_SZ": 1,
                "CHINEXT": 1,
                "STAR": 1,
            },
            members=(
                PilotUniverseMember(
                    ts_code="600001.SH",
                    security_name="虚构主板沪市",
                    board="MAIN_SH",
                    amount=Decimal("100"),
                    rank_in_board=1,
                    evidence_record_ids=("master-1", "bar-1"),
                ),
                PilotUniverseMember(
                    ts_code="000001.SZ",
                    security_name="虚构主板深市",
                    board="MAIN_SZ",
                    amount=Decimal("90"),
                    rank_in_board=1,
                    evidence_record_ids=("master-2", "bar-2"),
                ),
                PilotUniverseMember(
                    ts_code="300001.SZ",
                    security_name="虚构创业板",
                    board="CHINEXT",
                    amount=Decimal("80"),
                    rank_in_board=1,
                    evidence_record_ids=("master-3", "bar-3"),
                ),
                PilotUniverseMember(
                    ts_code="688001.SH",
                    security_name="虚构科创板",
                    board="STAR",
                    amount=Decimal("70"),
                    rank_in_board=1,
                    evidence_record_ids=("master-4", "bar-4"),
                ),
            ),
            input_hashes={"fixture": "a" * 64},
            manifest_hash="b" * 64,
            created_at=KNOWN_AT,
        )
    )
    pilot_repository.insert_manifest([item])
    action_repository = CorporateActionRepository(state_path)
    return (
        OfficialEvidenceIngestionService(
            raw_store=RawObjectStore(tmp_path / "raw"),
            pilot_repository=pilot_repository,
            action_repository=action_repository,
            manual_inbox=tmp_path / "manual_inbox",
            clock=lambda: KNOWN_AT,
        ),
        pilot_repository,
        action_repository,
    )


def _write_evidence(
    root: Path,
    *,
    item_id: str,
    payload: dict[str, object],
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{item_id}.pdf.json").write_text(
        json.dumps({"item_id": item_id, "evidence": payload}),
        encoding="utf-8",
    )


def test_ingests_reviewed_dividend_as_versioned_corporate_action(
    tmp_path: Path,
) -> None:
    """Catches a reviewed dividend attachment never entering real metrics."""
    raw_store = RawObjectStore(tmp_path / "raw")
    reference = raw_store.put(
        source_id="sse",
        source_url="https://www.sse.com.cn/disclosure/official-evidence.pdf",
        collected_at=KNOWN_AT,
        content_type="application/pdf",
        payload=b"private-official-pdf-fixture",
    )
    item = _downloaded_item(
        item_id="dividend-2025",
        document_kind=DocumentKind.DIVIDEND_RECORD,
        raw_hash=reference.content_hash,
        report_period=date(2025, 12, 31),
    )
    service, pilot_repository, action_repository = _service(tmp_path, item)
    _write_evidence(
        tmp_path / "manual_inbox",
        item_id=item.item_id,
        payload={
            "schema_version": "official-action-evidence-v1",
            "attachment_sha256": reference.content_hash,
            "reviewed_at": KNOWN_AT.isoformat(),
            "extraction_method": "MANUAL_REVIEW",
            "actions": [
                {
                    "action_key": "2025-cash-dividend",
                    "action_type": "CASH_DIVIDEND",
                    "record_date": "2026-06-10",
                    "ex_date": "2026-06-11",
                    "pay_date": "2026-06-12",
                    "cash_dividend_per_share": "0.50",
                    "cash_dividend_total": "500000000",
                    "fiscal_year": 2025,
                    "action_status": "IMPLEMENTED",
                    "supersedes_record_id": None,
                }
            ],
        },
    )

    result = service.run(item.item_id)

    stored_item = pilot_repository.get_manifest_item(item.item_id)
    assert stored_item is not None
    assert stored_item.status is AcquisitionStatus.INGESTED
    assert stored_item.quality_status is QualityStatus.VALID
    assert result.ingested is True
    assert result.action_count == 1
    actions = action_repository.visible_actions(
        item.ts_code,
        as_of=CUTOFF,
        known_at=KNOWN_AT,
    )
    assert len(actions) == 1
    action = actions[0]
    assert action.action_type is ActionType.CASH_DIVIDEND
    assert action.action_status is ActionStatus.IMPLEMENTED
    assert action.cash_dividend_per_share == Decimal("0.50")
    assert action.cash_dividend_total == Decimal("500000000")
    assert action.fiscal_year == 2025
    assert action.content_hash == reference.content_hash
    assert action.published_at == PUBLISHED_AT
    assert action.collected_at == KNOWN_AT
    assert action.record_id in result.source_record_ids


def test_ingests_reviewed_explicit_no_dividend_without_fabricating_action(
    tmp_path: Path,
) -> None:
    """An official no-distribution decision completes evidence without a fake dividend."""
    raw_store = RawObjectStore(tmp_path / "raw")
    reference = raw_store.put(
        source_id="sse",
        source_url="https://www.sse.com.cn/disclosure/official-evidence.pdf",
        collected_at=KNOWN_AT,
        content_type="application/pdf",
        payload=b"private-official-no-dividend-fixture",
    )
    item = _downloaded_item(
        item_id="dividend-2025-none",
        document_kind=DocumentKind.DIVIDEND_RECORD,
        raw_hash=reference.content_hash,
        report_period=date(2025, 12, 31),
    )
    service, pilot_repository, action_repository = _service(tmp_path, item)
    _write_evidence(
        tmp_path / "manual_inbox",
        item_id=item.item_id,
        payload={
            "schema_version": "official-action-evidence-v1",
            "attachment_sha256": reference.content_hash,
            "reviewed_at": KNOWN_AT.isoformat(),
            "extraction_method": "MANUAL_REVIEW",
            "actions": [],
            "no_dividend_fiscal_year": 2025,
        },
    )

    result = service.run(item.item_id)

    stored_item = pilot_repository.get_manifest_item(item.item_id)
    assert stored_item is not None
    assert stored_item.status is AcquisitionStatus.INGESTED
    assert result.ingested is True
    assert result.action_count == 0
    assert len(result.source_record_ids) == 1
    assert action_repository.visible_actions(
        item.ts_code,
        as_of=CUTOFF,
        known_at=KNOWN_AT,
    ) == ()
    annual_records = AnnualDividendRepository(action_repository.path).visible_records(
        item.ts_code,
        as_of=CUTOFF,
        known_at=KNOWN_AT,
    )
    assert len(annual_records) == 1
    assert annual_records[0].fiscal_year == 2025
    assert annual_records[0].has_cash_dividend is False


def test_ingests_annual_report_dividend_evidence_without_fabricating_action_dates(
    tmp_path: Path,
) -> None:
    """Annual reports can prove a dividend year without pretending to be action notices."""
    raw_store = RawObjectStore(tmp_path / "raw")
    reference = raw_store.put(
        source_id="sse",
        source_url="https://www.sse.com.cn/disclosure/annual-report.pdf",
        collected_at=KNOWN_AT,
        content_type="application/pdf",
        payload=b"private-official-annual-dividend-fixture",
    )
    item = _downloaded_item(
        item_id="dividend-history-2024",
        document_kind=DocumentKind.DIVIDEND_RECORD,
        raw_hash=reference.content_hash,
        report_period=date(2024, 12, 31),
    )
    service, pilot_repository, action_repository = _service(tmp_path, item)
    _write_evidence(
        tmp_path / "manual_inbox",
        item_id=item.item_id,
        payload={
            "schema_version": "official-dividend-year-evidence-v2",
            "attachment_sha256": reference.content_hash,
            "reviewed_at": KNOWN_AT.isoformat(),
            "extraction_method": "MANUAL_REVIEW",
            "annual_record": {
                "fiscal_year": 2024,
                "has_cash_dividend": True,
                "cash_dividend_per_share": "0.34",
                "cash_dividend_total": "225575097.80",
                "implementation_status": "IMPLEMENTED",
            },
        },
    )

    result = service.run(item.item_id)

    assert result.ingested is True
    assert result.action_count == 0
    assert action_repository.visible_actions(item.ts_code, CUTOFF, KNOWN_AT) == ()
    stored = AnnualDividendRepository(action_repository.path).visible_records(
        item.ts_code,
        as_of=CUTOFF,
        known_at=KNOWN_AT,
    )
    assert len(stored) == 1
    assert stored[0].cash_dividend_per_share == Decimal("0.34")
    assert stored[0].cash_dividend_total == Decimal("225575097.80")
    assert stored[0].record_id in result.source_record_ids
    manifest_item = pilot_repository.get_manifest_item(item.item_id)
    assert manifest_item is not None
    assert manifest_item.status is AcquisitionStatus.INGESTED


def test_ingests_reviewed_risk_screen_with_explicit_filter_facts(
    tmp_path: Path,
) -> None:
    """Catches treating a risk attachment's existence as proof that all checks pass."""
    raw_store = RawObjectStore(tmp_path / "raw")
    reference = raw_store.put(
        source_id="sse",
        source_url="https://www.sse.com.cn/disclosure/official-evidence.pdf",
        collected_at=KNOWN_AT,
        content_type="application/pdf",
        payload=b"private-official-risk-pdf-fixture",
    )
    item = _downloaded_item(
        item_id="risk-screen",
        document_kind=DocumentKind.RISK_SCREEN,
        raw_hash=reference.content_hash,
    )
    service, pilot_repository, _ = _service(tmp_path, item)
    _write_evidence(
        tmp_path / "manual_inbox",
        item_id=item.item_id,
        payload={
            "schema_version": "official-risk-screen-v1",
            "attachment_sha256": reference.content_hash,
            "reviewed_at": KNOWN_AT.isoformat(),
            "extraction_method": "MANUAL_REVIEW",
            "audit_opinion_standard": True,
            "major_investigation_open": False,
            "delisting_risk": False,
            "st_status": None,
            "is_suspended": False,
            "publication_order_known": True,
        },
    )

    result = service.run(item.item_id)

    stored_item = pilot_repository.get_manifest_item(item.item_id)
    assert stored_item is not None
    assert stored_item.status is AcquisitionStatus.INGESTED
    assert result.ingested is True
    assert result.action_count == 0
    assert result.risk_record_id is not None
    assert result.source_record_ids == (result.risk_record_id,)


def test_invalid_evidence_returns_to_manual_queue_without_partial_facts(
    tmp_path: Path,
) -> None:
    """Catches malformed reviewed data aborting the run or partially persisting facts."""
    raw_store = RawObjectStore(tmp_path / "raw")
    reference = raw_store.put(
        source_id="sse",
        source_url="https://www.sse.com.cn/disclosure/official-evidence.pdf",
        collected_at=KNOWN_AT,
        content_type="application/pdf",
        payload=b"private-invalid-evidence-fixture",
    )
    item = _downloaded_item(
        item_id="invalid-dividend",
        document_kind=DocumentKind.DIVIDEND_RECORD,
        raw_hash=reference.content_hash,
        report_period=date(2025, 12, 31),
    )
    service, pilot_repository, action_repository = _service(tmp_path, item)
    _write_evidence(
        tmp_path / "manual_inbox",
        item_id=item.item_id,
        payload={
            "schema_version": "official-action-evidence-v1",
            "attachment_sha256": "f" * 64,
            "reviewed_at": KNOWN_AT.isoformat(),
            "extraction_method": "MANUAL_REVIEW",
            "actions": [
                {
                    "action_key": "2025-cash-dividend",
                    "action_type": "CASH_DIVIDEND",
                    "record_date": "2026-06-10",
                    "ex_date": "2026-06-11",
                    "cash_dividend_per_share": "0.50",
                    "fiscal_year": 2025,
                    "action_status": "IMPLEMENTED",
                    "supersedes_record_id": None,
                }
            ],
        },
    )

    result = service.run(item.item_id)

    stored_item = pilot_repository.get_manifest_item(item.item_id)
    assert stored_item is not None
    assert stored_item.status is AcquisitionStatus.AWAITING_MANUAL
    assert stored_item.quality_status is QualityStatus.UNVERIFIED
    assert stored_item.error_code == "OFFICIAL_EVIDENCE_HASH_MISMATCH"
    assert result.ingested is False
    assert result.error_code == "OFFICIAL_EVIDENCE_HASH_MISMATCH"
    assert (
        action_repository.visible_actions(
            item.ts_code,
            as_of=CUTOFF,
            known_at=KNOWN_AT,
        )
        == ()
    )


def test_action_batch_is_atomic_when_one_correction_chain_is_invalid(
    tmp_path: Path,
) -> None:
    """Catches persisting the first action when a later action in one review fails."""
    raw_store = RawObjectStore(tmp_path / "raw")
    reference = raw_store.put(
        source_id="sse",
        source_url="https://www.sse.com.cn/disclosure/official-evidence.pdf",
        collected_at=KNOWN_AT,
        content_type="application/pdf",
        payload=b"private-action-batch-fixture",
    )
    item = _downloaded_item(
        item_id="capital-actions",
        document_kind=DocumentKind.CAPITAL_ACTION_TIMELINE,
        raw_hash=reference.content_hash,
    )
    service, pilot_repository, action_repository = _service(tmp_path, item)
    _write_evidence(
        tmp_path / "manual_inbox",
        item_id=item.item_id,
        payload={
            "schema_version": "official-action-evidence-v1",
            "attachment_sha256": reference.content_hash,
            "reviewed_at": KNOWN_AT.isoformat(),
            "extraction_method": "MANUAL_REVIEW",
            "actions": [
                {
                    "action_key": "split",
                    "action_type": "SPLIT",
                    "record_date": "2026-06-10",
                    "ex_date": "2026-06-11",
                    "split_ratio": "2",
                    "action_status": "IMPLEMENTED",
                    "supersedes_record_id": None,
                },
                {
                    "action_key": "broken-correction",
                    "action_type": "BUYBACK_CANCELLATION",
                    "record_date": "2026-06-12",
                    "ex_date": "2026-06-13",
                    "share_reduction": "1000",
                    "action_status": "IMPLEMENTED",
                    "supersedes_record_id": "missing-action",
                },
            ],
        },
    )

    result = service.run(item.item_id)

    stored_item = pilot_repository.get_manifest_item(item.item_id)
    assert stored_item is not None
    assert stored_item.status is AcquisitionStatus.AWAITING_MANUAL
    assert stored_item.error_code == "CORPORATE_ACTION_CHAIN_GAP"
    assert result.ingested is False
    assert result.error_code == "CORPORATE_ACTION_CHAIN_GAP"
    assert (
        action_repository.visible_actions(
            item.ts_code,
            as_of=CUTOFF,
            known_at=KNOWN_AT,
        )
        == ()
    )
