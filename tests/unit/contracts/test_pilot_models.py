from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from hengce.contracts.enums import (
    AcquisitionStatus,
    DiscoveryMethod,
    DocumentKind,
    PoolReadinessStatus,
    QualityStatus,
    ReportType,
    StrategyType,
)
from hengce.contracts.pilot import (
    AcquisitionManifestItem,
    PilotUniverseMember,
    PilotUniverseSnapshot,
    PoolReadiness,
)

NOW = datetime(2026, 7, 30, 9, tzinfo=UTC)
CUTOFF = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
HASH = "a" * 64
BOARDS = ("MAIN_SH", "MAIN_SZ", "CHINEXT", "STAR")


def member(board: str, rank: int) -> PilotUniverseMember:
    suffix = "SH" if board in {"MAIN_SH", "STAR"} else "SZ"
    prefix = {
        "MAIN_SH": "600",
        "MAIN_SZ": "000",
        "CHINEXT": "300",
        "STAR": "688",
    }[board]
    return PilotUniverseMember(
        ts_code=f"{prefix}{rank:03d}.{suffix}",
        security_name=f"示例{board}{rank}",
        board=board,
        amount=Decimal(1_000_000 - rank),
        rank_in_board=rank,
        evidence_record_ids=(f"bar-{board}-{rank}", f"master-{board}-{rank}"),
    )


def universe_payload() -> dict[str, object]:
    quotas = {"MAIN_SH": 2, "MAIN_SZ": 2, "CHINEXT": 1, "STAR": 1}
    members = tuple(
        member(board, rank)
        for board in BOARDS
        for rank in range(1, quotas[board] + 1)
    )
    return {
        "universe_id": "pilot-2026-07-22",
        "market_date": date(2026, 7, 22),
        "report_cutoff_at": CUTOFF,
        "algorithm_version": "board-liquidity-pilot-v1",
        "quotas": quotas,
        "members": members,
        "input_hashes": {"market": HASH, "security_master": "b" * 64},
        "manifest_hash": "c" * 64,
        "created_at": NOW,
    }


def test_universe_rejects_missing_board_or_quota_member_mismatch() -> None:
    """Catches publishing a sample whose declared board coverage is not its real coverage."""
    assert len(PilotUniverseSnapshot.model_validate(universe_payload()).members) == 6

    without_star = {
        **universe_payload(),
        "quotas": {"MAIN_SH": 2, "MAIN_SZ": 2, "CHINEXT": 1},
    }
    with pytest.raises(ValidationError, match="PILOT_BOARD_QUOTAS_INVALID"):
        PilotUniverseSnapshot.model_validate(without_star)

    mismatched = {
        **universe_payload(),
        "members": tuple(universe_payload()["members"])[:-1],
    }
    with pytest.raises(ValidationError, match="PILOT_BOARD_QUOTA_MISMATCH"):
        PilotUniverseSnapshot.model_validate(mismatched)


def test_universe_requires_unique_members_hashes_and_aware_times() -> None:
    """Catches non-reproducible samples caused by duplicate identities or weak lineage."""
    payload = universe_payload()
    duplicated = tuple(payload["members"]) + (tuple(payload["members"])[0],)
    with pytest.raises(ValidationError, match="PILOT_MEMBER_DUPLICATE"):
        PilotUniverseSnapshot.model_validate({**payload, "members": duplicated})
    with pytest.raises(ValidationError):
        PilotUniverseSnapshot.model_validate({**payload, "manifest_hash": "short"})
    with pytest.raises(ValidationError, match="report_cutoff_at must include a timezone"):
        PilotUniverseSnapshot.model_validate(
            {**payload, "report_cutoff_at": datetime(2026, 7, 22, 21, 30)}
        )


def manifest_payload() -> dict[str, object]:
    return {
        "item_id": "item-1",
        "universe_id": "pilot-2026-07-22",
        "ts_code": "600001.SH",
        "document_kind": DocumentKind.PERIODIC_REPORT,
        "report_type": ReportType.ANNUAL,
        "report_period": date(2025, 12, 31),
        "source_id": "sse",
        "report_cutoff_at": CUTOFF,
        "status": AcquisitionStatus.PLANNED,
        "source_url": None,
        "discovery_method": None,
        "published_at": None,
        "effective_at": None,
        "collected_at": None,
        "content_hash": None,
        "version": None,
        "supersedes_id": None,
        "raw_object_hash": None,
        "quality_status": QualityStatus.MISSING,
        "error_code": None,
        "attempt_count": 0,
    }


def test_periodic_manifest_identity_requires_type_and_period() -> None:
    """Catches a filing item that cannot be matched to one statutory reporting period."""
    assert (
        AcquisitionManifestItem.model_validate(manifest_payload()).status
        is AcquisitionStatus.PLANNED
    )
    with pytest.raises(ValidationError, match="ACQUISITION_PERIODIC_IDENTITY_REQUIRED"):
        AcquisitionManifestItem.model_validate(
            {**manifest_payload(), "report_type": None}
        )
    with pytest.raises(ValidationError, match="ACQUISITION_PERIODIC_IDENTITY_REQUIRED"):
        AcquisitionManifestItem.model_validate(
            {**manifest_payload(), "report_period": None}
        )
    with pytest.raises(ValidationError, match="ACQUISITION_REPORT_TYPE_NOT_ALLOWED"):
        AcquisitionManifestItem.model_validate(
            {
                **manifest_payload(),
                "document_kind": DocumentKind.DIVIDEND_RECORD,
            }
        )


def test_downloaded_manifest_requires_complete_raw_lineage() -> None:
    """Catches marking a document downloaded before its immutable bytes are traceable."""
    downloaded = {
        **manifest_payload(),
        "status": AcquisitionStatus.DOWNLOADED,
        "source_url": "https://www.sse.com.cn/disclosure/example.xml",
        "discovery_method": DiscoveryMethod.MANUAL_IMPORT,
        "published_at": CUTOFF,
        "collected_at": NOW,
        "content_hash": HASH,
        "raw_object_hash": "b" * 64,
        "version": "official-v1",
        "attempt_count": 1,
    }
    assert (
        AcquisitionManifestItem.model_validate(downloaded).raw_object_hash
        == "b" * 64
    )
    with pytest.raises(ValidationError, match="ACQUISITION_DOWNLOAD_LINEAGE_REQUIRED"):
        AcquisitionManifestItem.model_validate({**downloaded, "raw_object_hash": None})
    with pytest.raises(ValidationError, match="ACQUISITION_MANUAL_ERROR_REQUIRED"):
        AcquisitionManifestItem.model_validate(
            {
                **manifest_payload(),
                "status": AcquisitionStatus.AWAITING_MANUAL,
            }
        )


def test_ingested_manifest_requires_usable_quality() -> None:
    """Catches an unverified attachment masquerading as an ingested ranking fact."""
    payload = {
        **manifest_payload(),
        "status": AcquisitionStatus.INGESTED,
        "source_url": "https://www.sse.com.cn/disclosure/example.xml",
        "discovery_method": DiscoveryMethod.MANUAL_IMPORT,
        "published_at": CUTOFF,
        "collected_at": NOW,
        "content_hash": HASH,
        "raw_object_hash": "b" * 64,
        "version": "official-v1",
        "attempt_count": 1,
        "quality_status": QualityStatus.UNVERIFIED,
    }
    with pytest.raises(ValidationError, match="ACQUISITION_INGESTED_QUALITY_INVALID"):
        AcquisitionManifestItem.model_validate(payload)


def readiness_payload(complete: int, status: PoolReadinessStatus) -> dict[str, object]:
    return {
        "strategy_type": StrategyType.QUALITY_GROWTH,
        "universe_size": 30,
        "eligible_count": 27,
        "complete_factor_count": complete,
        "coverage_ratio": Decimal(complete) / Decimal(30),
        "required_coverage_ratio": Decimal("0.80"),
        "status": status,
        "missing_by_security": (
            {} if complete == 30 else {"600001.SH": ("capital_return",)}
        ),
        "blocking_codes": (
            () if status is PoolReadinessStatus.READY else ("POOL_COVERAGE_BELOW_MINIMUM",)
        ),
        "strategy_version": "quality-growth-pilot-v1",
        "factor_version": "pilot-financial-metrics-v1",
    }


def test_pool_readiness_uses_exact_24_of_30_boundary() -> None:
    """Catches publishing rankings below the approved 80 percent coverage threshold."""
    assert (
        PoolReadiness.model_validate(
            readiness_payload(24, PoolReadinessStatus.READY)
        ).status
        is PoolReadinessStatus.READY
    )
    assert (
        PoolReadiness.model_validate(
            readiness_payload(23, PoolReadinessStatus.BLOCKED)
        ).status
        is PoolReadinessStatus.BLOCKED
    )
    with pytest.raises(ValidationError, match="POOL_READINESS_STATUS_MISMATCH"):
        PoolReadiness.model_validate(
            readiness_payload(23, PoolReadinessStatus.READY)
        )


def test_pool_readiness_reconciles_counts_and_ratio() -> None:
    """Catches a quality page showing coverage that differs from the ranked population."""
    with pytest.raises(ValidationError, match="POOL_COVERAGE_RATIO_MISMATCH"):
        PoolReadiness.model_validate(
            {
                **readiness_payload(24, PoolReadinessStatus.READY),
                "coverage_ratio": Decimal("0.81"),
            }
        )
    with pytest.raises(ValidationError, match="POOL_COMPLETE_COUNT_INVALID"):
        PoolReadiness.model_validate(
            {
                **readiness_payload(24, PoolReadinessStatus.READY),
                "eligible_count": 23,
            }
        )
