import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from hengce.contracts.enums import ConflictResolutionStatus, QualityStatus, ReportType
from hengce.contracts.financial import FactConflict, FinancialFiling, TaxonomyPackageRef
from hengce.state.financial_repository import FinancialFilingRepository
from hengce.state.repository import StateRepository


def taxonomy_ref(*, raw_object_hash: str = "a" * 64) -> TaxonomyPackageRef:
    return TaxonomyPackageRef(
        taxonomy_id="test-gaap-2025",
        source_id="sse",
        source_url="https://www.sse.com.cn/taxonomy.zip",
        raw_object_hash=raw_object_hash,
        package_name="test-gaap",
        entrypoint="entry.xsd",
        content_type="application/zip",
        collected_at=datetime(2026, 4, 30, 12, tzinfo=UTC),
    )


def financial_filing(
    *,
    filing_id: str = "filing",
    raw_object_hash: str = "a" * 64,
    supersedes_id: str | None = None,
    is_restated: bool = False,
    published_at: datetime = datetime(2026, 4, 30, 12, tzinfo=UTC),
    valid_from: datetime = datetime(2026, 4, 30, 12, tzinfo=UTC),
) -> FinancialFiling:
    return FinancialFiling(
        record_id=filing_id,
        filing_id=filing_id,
        source_id="sse",
        source_url="https://www.sse.com.cn/disclosure/test.xml",
        published_at=published_at,
        effective_at=published_at,
        collected_at=published_at,
        version="filing-v1:parser-v1:mapping-v1",
        content_hash=raw_object_hash,
        license_policy="sse-personal-research",
        quality_status=QualityStatus.VALID,
        valid_from=valid_from,
        ts_code="699999.SH",
        exchange="SSE",
        report_period=date(2025, 12, 31),
        report_type=ReportType.ANNUAL,
        announcement_at=published_at,
        taxonomy=("test-gaap-2025",),
        taxonomy_hashes=(raw_object_hash,),
        raw_object_hash=raw_object_hash,
        filing_version="filing-v1",
        parser_name="fixture-parser",
        parser_version="parser-v1",
        mapping_version="mapping-v1",
        fact_count=1,
        conflict_count=0,
        is_restated=is_restated,
        supersedes_id=supersedes_id,
    )


def fact_conflict(*, filing_id: str) -> FactConflict:
    return FactConflict(
        conflict_id=f"conflict-{filing_id}",
        filing_id=filing_id,
        fact_identity_hash="b" * 64,
        competing_fact_ids=("fact-1", "fact-2"),
        conflict_type="duplicate-context",
        resolution_status=ConflictResolutionStatus.OPEN,
        quality_status=QualityStatus.CONFLICT,
        detected_at=datetime(2026, 4, 30, 12, tzinfo=UTC),
    )


def test_taxonomy_registration_is_idempotent_and_hash_immutable(tmp_path: Path) -> None:
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()
    repository = FinancialFilingRepository(state.path)
    reference = taxonomy_ref(raw_object_hash="1" * 64)

    repository.register_taxonomy(reference)
    repository.register_taxonomy(reference)

    assert repository.get_taxonomies((reference.taxonomy_id,)) == (reference,)
    with pytest.raises(ValueError, match="FINANCIAL_TAXONOMY_CONFLICT"):
        repository.register_taxonomy(reference.model_copy(update={"raw_object_hash": "2" * 64}))


def test_get_taxonomies_rejects_missing_ids(tmp_path: Path) -> None:
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()

    with pytest.raises(ValueError, match="FINANCIAL_TAXONOMY_MISSING"):
        FinancialFilingRepository(state.path).get_taxonomies(("missing-taxonomy",))


def test_get_empty_taxonomies_does_not_bypass_state_connection(tmp_path: Path) -> None:
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("not a directory", encoding="utf-8")

    with pytest.raises(FileExistsError):
        FinancialFilingRepository(blocked_parent / "state.sqlite3").get_taxonomies(())


def test_stage_then_publish_requires_exact_artifact_identity(tmp_path: Path) -> None:
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()
    repository = FinancialFilingRepository(state.path)
    filing = financial_filing()

    staged = repository.stage_filing(
        filing,
        expected_path="financial_facts/report_year=2025/filing.parquet",
        expected_hash="3" * 64,
        expected_count=4,
    )
    assert staged.artifact_status == "PENDING"
    assert not repository.publish_filing(
        filing.filing_id,
        path=staged.expected_path,
        content_hash="4" * 64,
        fact_count=4,
    )
    assert repository.publish_filing(
        filing.filing_id,
        path=staged.expected_path,
        content_hash="3" * 64,
        fact_count=4,
    )
    assert repository.get_filing(filing.filing_id).artifact_status == "PUBLISHED"


def test_stage_filing_is_idempotent_but_rejects_changed_artifact_identity(tmp_path: Path) -> None:
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()
    repository = FinancialFilingRepository(state.path)
    filing = financial_filing()

    first = repository.stage_filing(filing, "filing.parquet", "3" * 64, 4)
    assert repository.stage_filing(filing, "filing.parquet", "3" * 64, 4) == first
    with pytest.raises(ValueError, match="FINANCIAL_FILING_CONFLICT"):
        repository.stage_filing(filing, "changed.parquet", "3" * 64, 4)


def test_filing_versions_and_conflicts_are_append_only(tmp_path: Path) -> None:
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()
    repository = FinancialFilingRepository(state.path)
    old = financial_filing(filing_id="old", raw_object_hash="5" * 64)
    new = financial_filing(
        filing_id="new", raw_object_hash="6" * 64, supersedes_id="old", is_restated=True
    )
    repository.stage_filing(old, "old.parquet", "7" * 64, 1)
    repository.stage_filing(new, "new.parquet", "8" * 64, 1)
    repository.record_conflicts([fact_conflict(filing_id="new")])

    assert [item.filing.filing_id for item in repository.list_filing_versions(
        old.ts_code, old.report_period
    )] == ["old", "new"]
    assert repository.list_conflicts("new")[0].quality_status is QualityStatus.CONFLICT


def test_repository_normalizes_timestamp_keys_to_utc(tmp_path: Path) -> None:
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()
    repository = FinancialFilingRepository(state.path)
    filing = financial_filing(
        published_at=datetime.fromisoformat("2026-04-30T20:00:00+08:00"),
        valid_from=datetime.fromisoformat("2026-04-30T12:01:00+00:00"),
    )
    repository.stage_filing(filing, "filing.parquet", "9" * 64, 1)

    with sqlite3.connect(state.path) as connection:
        row = connection.execute(
            "SELECT published_at, valid_from FROM financial_filings WHERE filing_id=?",
            (filing.filing_id,),
        ).fetchone()
    assert row is not None
    assert tuple(row) == ("2026-04-30T12:00:00.000000Z", "2026-04-30T12:01:00.000000Z")
