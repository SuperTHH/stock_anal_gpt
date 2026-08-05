import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from hengce.contracts.enums import (
    AcquisitionStatus,
    DiscoveryMethod,
    DocumentKind,
    QualityStatus,
    ReportType,
    RunStatus,
)
from hengce.contracts.pilot import (
    AcquisitionManifestItem,
    PilotUniverseMember,
    PilotUniverseSnapshot,
)
from hengce.state.pilot_repository import PilotRepository, PipelineCheckpoint
from hengce.state.repository import StateRepository

NOW = datetime(2026, 7, 30, 9, tzinfo=UTC)
CUTOFF = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
HASH = "a" * 64


def universe(*, manifest_hash: str = "c" * 64) -> PilotUniverseSnapshot:
    board_rows = (
        ("MAIN_SH", "600001.SH"),
        ("MAIN_SZ", "000001.SZ"),
        ("CHINEXT", "300001.SZ"),
        ("STAR", "688001.SH"),
    )
    return PilotUniverseSnapshot(
        universe_id="pilot-2026-07-22",
        market_date=date(2026, 7, 22),
        report_cutoff_at=CUTOFF,
        algorithm_version="board-liquidity-pilot-v1",
        quotas={board: 1 for board, _ in board_rows},
        members=tuple(
            PilotUniverseMember(
                ts_code=ts_code,
                security_name=f"示例{index}",
                board=board,
                amount=Decimal(1_000_000 - index),
                rank_in_board=1,
                evidence_record_ids=(f"bar-{index}", f"master-{index}"),
            )
            for index, (board, ts_code) in enumerate(board_rows, start=1)
        ),
        input_hashes={"market": HASH, "security_master": "b" * 64},
        manifest_hash=manifest_hash,
        created_at=NOW,
    )


def manifest_item(
    *,
    item_id: str = "item-1",
    kind: DocumentKind = DocumentKind.PERIODIC_REPORT,
) -> AcquisitionManifestItem:
    return AcquisitionManifestItem(
        item_id=item_id,
        universe_id="pilot-2026-07-22",
        ts_code="600001.SH",
        document_kind=kind,
        report_type=ReportType.ANNUAL if kind is DocumentKind.PERIODIC_REPORT else None,
        report_period=(
            date(2025, 12, 31)
            if kind
            in {DocumentKind.PERIODIC_REPORT, DocumentKind.DIVIDEND_RECORD}
            else None
        ),
        source_id="sse",
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


def downloaded(item: AcquisitionManifestItem) -> AcquisitionManifestItem:
    return AcquisitionManifestItem.model_validate(
        {
            **item.model_dump(),
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
    )


def test_migration_upgrades_legacy_database_without_changing_existing_rows(
    tmp_path: Path,
) -> None:
    """Catches a pilot migration that damages the already-populated M1-M4 state."""
    path = tmp_path / "state.sqlite3"
    migration_root = (
        Path(__file__).parents[3] / "src" / "hengce" / "state" / "migrations"
    )
    with sqlite3.connect(path) as connection:
        for migration in sorted(migration_root.glob("00[1-7]_*.sql")):
            connection.executescript(migration.read_text(encoding="utf-8"))
        connection.execute(
            "INSERT INTO source_policies(source_id, payload_json, updated_at) VALUES (?, ?, ?)",
            ("preserved", '{"value":1}', NOW.isoformat()),
        )

    StateRepository(path).migrate()
    StateRepository(path).migrate()

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        preserved = connection.execute(
            "SELECT payload_json FROM source_policies WHERE source_id='preserved'"
        ).fetchone()
        migration_count = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version='008_real_data_pilot'"
        ).fetchone()[0]
    assert {
        "pilot_universes",
        "acquisition_manifest_items",
        "acquisition_transitions",
        "pipeline_checkpoints",
    } <= tables
    assert preserved == ('{"value":1}',)
    assert migration_count == 1


def test_universe_publication_is_idempotent_but_rejects_changed_date_snapshot(
    tmp_path: Path,
) -> None:
    """Catches silently reselecting a different pilot population for the same market date."""
    path = tmp_path / "state.sqlite3"
    StateRepository(path).migrate()
    repository = PilotRepository(path)
    first = universe()

    assert repository.publish_universe(first) == first
    assert repository.publish_universe(first) == first
    assert repository.get_universe(first.universe_id) == first
    assert repository.get_universe_for_date(first.market_date) == first

    changed = first.model_copy(
        update={
            "universe_id": "pilot-2026-07-22-changed",
            "manifest_hash": "d" * 64,
        }
    )
    with pytest.raises(ValueError, match="PILOT_UNIVERSE_IMMUTABILITY_CONFLICT"):
        repository.publish_universe(changed)


def test_manifest_batch_is_idempotent_atomic_and_null_safe(tmp_path: Path) -> None:
    """Catches duplicate non-periodic identities or half-written acquisition batches."""
    path = tmp_path / "state.sqlite3"
    StateRepository(path).migrate()
    repository = PilotRepository(path)
    repository.publish_universe(universe())
    periodic = manifest_item()
    capital = manifest_item(
        item_id="capital-1",
        kind=DocumentKind.CAPITAL_ACTION_TIMELINE,
    )

    written_after = datetime.now(UTC)
    assert repository.insert_manifest((periodic, capital)) == 2
    written_before = datetime.now(UTC)
    with sqlite3.connect(path) as connection:
        updated_at = datetime.fromisoformat(
            connection.execute(
                "SELECT updated_at FROM acquisition_manifest_items WHERE item_id=?",
                (periodic.item_id,),
            ).fetchone()[0]
        )
    assert written_after <= updated_at <= written_before
    assert repository.insert_manifest((periodic, capital)) == 0

    duplicate_identity = capital.model_copy(update={"item_id": "capital-other"})
    with pytest.raises(ValueError, match="ACQUISITION_MANIFEST_IMMUTABILITY_CONFLICT"):
        repository.insert_manifest(
            (
                manifest_item(item_id="would-rollback").model_copy(
                    update={"ts_code": "600002.SH"}
                ),
                duplicate_identity,
            )
        )
    assert [item.item_id for item in repository.list_manifest(periodic.universe_id)] == [
        "capital-1",
        "item-1",
    ]


def test_transition_enforces_state_machine_compare_and_swap_and_terminal_states(
    tmp_path: Path,
) -> None:
    """Catches stale workers overwriting a newer acquisition state."""
    path = tmp_path / "state.sqlite3"
    StateRepository(path).migrate()
    repository = PilotRepository(path)
    repository.publish_universe(universe())
    item = manifest_item()
    repository.insert_manifest((item,))
    discovered = AcquisitionManifestItem.model_validate(
        {
            **item.model_dump(),
            "status": AcquisitionStatus.DISCOVERED,
            "source_url": "https://www.sse.com.cn/disclosure/example.xml",
            "discovery_method": DiscoveryMethod.MANUAL_IMPORT,
            "published_at": CUTOFF,
            "attempt_count": 1,
        }
    )

    assert (
        repository.transition(
            item.item_id,
            AcquisitionStatus.PLANNED,
            discovered,
            NOW,
        ).status
        is AcquisitionStatus.DISCOVERED
    )
    assert repository.transition(
        item.item_id,
        AcquisitionStatus.PLANNED,
        discovered,
        NOW,
    ) == discovered
    with pytest.raises(ValueError, match="ACQUISITION_STATE_CHANGED"):
        repository.transition(
            item.item_id,
            AcquisitionStatus.PLANNED,
            discovered.model_copy(update={"attempt_count": 2}),
            NOW,
        )
    illegal_ingested = downloaded(item).model_copy(
        update={
            "status": AcquisitionStatus.INGESTED,
            "quality_status": QualityStatus.VALID,
        }
    )
    with pytest.raises(ValueError, match="ACQUISITION_TRANSITION_INVALID"):
        repository.transition(
            item.item_id,
            AcquisitionStatus.DISCOVERED,
            illegal_ingested,
            NOW,
        )

    stored = repository.transition(
        item.item_id,
        AcquisitionStatus.DISCOVERED,
        downloaded(item),
        NOW,
    )
    verified = stored.model_copy(
        update={
            "status": AcquisitionStatus.VERIFIED,
            "quality_status": QualityStatus.VALID,
        }
    )
    repository.transition(
        item.item_id,
        AcquisitionStatus.DOWNLOADED,
        verified,
        NOW,
    )
    ingested = verified.model_copy(update={"status": AcquisitionStatus.INGESTED})
    repository.transition(
        item.item_id,
        AcquisitionStatus.VERIFIED,
        ingested,
        NOW,
    )
    with pytest.raises(ValueError, match="ACQUISITION_TERMINAL_STATE"):
        repository.transition(
            item.item_id,
            AcquisitionStatus.INGESTED,
            ingested.model_copy(update={"status": AcquisitionStatus.REJECTED}),
            NOW,
        )


def test_checkpoint_reuses_same_input_hash_and_rejects_changed_inputs(
    tmp_path: Path,
) -> None:
    """Catches resuming a stage with inputs different from the original run."""
    path = tmp_path / "state.sqlite3"
    StateRepository(path).migrate()
    repository = PilotRepository(path)
    checkpoint = PipelineCheckpoint(
        run_key="pilot:2026-07-22",
        stage_name="03_freeze_universe",
        input_hash=HASH,
        status="SUCCEEDED",
        payload={"universe_id": "pilot-2026-07-22"},
        updated_at=NOW,
    )

    repository.save_checkpoint(checkpoint)
    repository.save_checkpoint(
        checkpoint.model_copy(update={"status": RunStatus.RUNNING, "updated_at": NOW})
    )
    assert repository.get_checkpoint(checkpoint.run_key, checkpoint.stage_name) == (
        checkpoint.model_copy(update={"status": RunStatus.RUNNING, "updated_at": NOW})
    )
    with pytest.raises(ValueError, match="PIPELINE_CHECKPOINT_INPUT_CHANGED"):
        repository.save_checkpoint(
            checkpoint.model_copy(update={"input_hash": "b" * 64})
        )


def test_state_backup_preserves_content_and_never_overwrites(tmp_path: Path) -> None:
    """Catches an unsafe backup operation replacing the only recoverable state copy."""
    path = tmp_path / "state.sqlite3"
    repository = StateRepository(path)
    repository.migrate()
    repository.save_checkpoint("pilot:test", "preserved")
    backup = tmp_path / "backups" / "state.sqlite3"

    repository.backup_to(backup)

    assert StateRepository(backup).get_checkpoint("pilot:test") == "preserved"
    with pytest.raises(ValueError, match="STATE_BACKUP_TARGET_EXISTS"):
        repository.backup_to(backup)
