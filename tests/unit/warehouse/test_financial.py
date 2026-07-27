import os
import shutil
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest

import hengce.warehouse.financial as financial_module
from hengce.contracts.enums import (
    ConsolidationScope,
    MappingStatus,
    QualityStatus,
    ReportType,
    StatementType,
)
from hengce.contracts.financial import FinancialFact, FinancialFiling
from hengce.warehouse.financial import FinancialArtifact, FinancialFactWarehouse

NOW = datetime(2026, 4, 30, 12, tzinfo=UTC)
EFFECTIVE_AT = datetime.fromisoformat("2025-12-31T23:59:59+08:00")
HASH = "a" * 64


def financial_filing(
    *,
    filing_id: str = "filing-1",
    exchange: str = "SSE",
) -> FinancialFiling:
    return FinancialFiling(
        record_id=filing_id,
        filing_id=filing_id,
        source_id="fixture-source",
        source_url="https://example.test/filing.xml",
        published_at=NOW,
        effective_at=NOW,
        collected_at=NOW,
        version="filing-v1:parser-v1:mapping-v1",
        content_hash=HASH,
        license_policy="fixture-only",
        quality_status=QualityStatus.VALID,
        valid_from=NOW,
        ts_code="600001.SH",
        exchange=exchange,
        report_period=date(2025, 12, 31),
        report_type=ReportType.ANNUAL,
        announcement_at=NOW,
        taxonomy=("test-gaap-2025",),
        taxonomy_hashes=(HASH,),
        raw_object_hash=HASH,
        filing_version="filing-v1",
        parser_name="fixture-parser",
        parser_version="parser-v1",
        mapping_version="mapping-v1",
        fact_count=1,
        conflict_count=0,
        is_restated=False,
        supersedes_id=None,
    )


def mapped_fact(
    canonical_fact_name: str,
    value: Decimal,
    *,
    filing_id: str = "filing-1",
    dimensions: dict[str, str] | None = None,
) -> FinancialFact:
    fact_id = f"{canonical_fact_name}-{value}"
    return FinancialFact(
        record_id=fact_id,
        fact_id=fact_id,
        source_id="fixture-source",
        source_url="https://example.test/filing.xml",
        published_at=NOW,
        effective_at=EFFECTIVE_AT,
        collected_at=NOW,
        version="filing-v1:parser-v1:mapping-v1",
        content_hash=HASH,
        license_policy="fixture-only",
        quality_status=QualityStatus.VALID,
        valid_from=NOW,
        ts_code="600001.SH",
        report_period=date(2025, 12, 31),
        report_type=ReportType.ANNUAL,
        announcement_at=NOW,
        statement_type=StatementType.BALANCE_SHEET,
        taxonomy=("test-gaap-2025",),
        fact_name=canonical_fact_name.title(),
        raw_qname=f"{{urn:hengce:test-gaap}}{canonical_fact_name.title()}",
        canonical_fact_name=canonical_fact_name,
        mapping_status=MappingStatus.MAPPED,
        fact_value=value,
        unit="CNY",
        currency="CNY",
        filing_id=filing_id,
        context_signature=f"context-{canonical_fact_name}",
        entity_scheme="https://example.test/entity",
        entity_identifier="600001.SH",
        period_start=None,
        period_end=None,
        instant=date(2025, 12, 31),
        unit_signature="CNY",
        decimals="0",
        consolidation_scope=ConsolidationScope.CONSOLIDATED,
        dimensions={} if dimensions is None else dimensions,
        fact_identity_hash="b" * 64,
        comparison_identity_hash="c" * 64,
    )


def test_financial_artifact_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    """Catches order-sensitive hashing, mutable filenames, and incomplete row storage."""
    warehouse = FinancialFactWarehouse(tmp_path)
    filing = financial_filing()
    facts = [
        mapped_fact("equity", Decimal("600"), dimensions={"segment": "domestic"}),
        mapped_fact("assets", Decimal("1000")),
    ]

    first = warehouse.write_facts(filing, facts)
    second = warehouse.write_facts(filing, list(reversed(facts)))

    assert first == second
    assert first.fact_count == 2
    assert first.path == (
        tmp_path
        / "financial_facts"
        / "report_year=2025"
        / "report_type=ANNUAL"
        / "exchange=SSE"
        / f"filing-{filing.filing_id}-{first.content_hash}.parquet"
    )
    assert warehouse.read_artifact(first.path) == sorted(
        [fact.model_dump(mode="json") for fact in facts],
        key=lambda row: row["fact_id"],
    )


def test_content_hash_changes_when_existing_fact_value_changes(tmp_path: Path) -> None:
    """Catches hashes based only on filing/fact identity instead of full model JSON."""
    warehouse = FinancialFactWarehouse(tmp_path)
    filing = financial_filing()

    original = warehouse.expected_artifact(filing, [mapped_fact("assets", Decimal("1000"))])
    changed = warehouse.expected_artifact(filing, [mapped_fact("assets", Decimal("1001"))])

    assert original.content_hash != changed.content_hash
    assert original.path != changed.path


def test_empty_fact_list_has_canonical_empty_json_hash_and_round_trips(tmp_path: Path) -> None:
    """Catches rejection of valid empty filings and non-canonical empty hashing."""
    warehouse = FinancialFactWarehouse(tmp_path)
    filing = financial_filing()

    artifact = warehouse.write_facts(filing, [])

    assert artifact.content_hash == (
        "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    )
    assert artifact.fact_count == 0
    assert warehouse.read_artifact(artifact.path) == []
    assert warehouse.validate_artifact(artifact, filing.filing_id) == artifact.content_hash


def test_artifact_rejects_mixed_filing_ids(tmp_path: Path) -> None:
    """Catches publication that admits facts owned by another filing."""
    warehouse = FinancialFactWarehouse(tmp_path)

    with pytest.raises(ValueError, match="^FINANCIAL_FACTS_MIXED_FILING$"):
        warehouse.write_facts(
            financial_filing(filing_id="one"),
            [
                mapped_fact("assets", Decimal("1000"), filing_id="one"),
                mapped_fact("equity", Decimal("600"), filing_id="two"),
            ],
        )


def test_parquet_uses_zstandard_and_records_partition_metadata(tmp_path: Path) -> None:
    """Catches wrong compression and files whose embedded partition identity is absent."""
    warehouse = FinancialFactWarehouse(tmp_path)
    filing = financial_filing()

    artifact = warehouse.write_facts(filing, [mapped_fact("assets", Decimal("1000"))])
    with pq.ParquetFile(artifact.path) as parquet:
        metadata = parquet.schema_arrow.metadata or {}
        compression = parquet.metadata.row_group(0).column(0).compression

    assert compression == "ZSTD"
    assert {
        value.decode("utf-8")
        for value in metadata.values()
        if value.decode("utf-8") in {"2025", "ANNUAL", "SSE", filing.filing_id}
    } == {"2025", "ANNUAL", "SSE", filing.filing_id}


def test_validation_rejects_wrong_partition_path(tmp_path: Path) -> None:
    """Catches validation that trusts directory partition metadata without cross-checking."""
    warehouse = FinancialFactWarehouse(tmp_path)
    filing = financial_filing()
    artifact = warehouse.write_facts(filing, [mapped_fact("assets", Decimal("1000"))])
    wrong_path = (
        tmp_path
        / "financial_facts"
        / "report_year=2024"
        / "report_type=Q1"
        / "exchange=SZSE"
        / artifact.path.name
    )
    wrong_path.parent.mkdir(parents=True)
    shutil.copyfile(artifact.path, wrong_path)
    moved = FinancialArtifact(wrong_path, artifact.content_hash, artifact.fact_count)

    with pytest.raises(ValueError, match="^FINANCIAL_PARQUET_INTEGRITY_ERROR$"):
        warehouse.validate_artifact(moved, filing.filing_id)


def test_validation_rejects_wrong_row_count(tmp_path: Path) -> None:
    """Catches validation that does not compare the manifest count to physical rows."""
    warehouse = FinancialFactWarehouse(tmp_path)
    filing = financial_filing()
    artifact = warehouse.write_facts(filing, [mapped_fact("assets", Decimal("1000"))])
    wrong_count = FinancialArtifact(artifact.path, artifact.content_hash, artifact.fact_count + 1)

    with pytest.raises(ValueError, match="^FINANCIAL_PARQUET_INTEGRITY_ERROR$"):
        warehouse.validate_artifact(wrong_count, filing.filing_id)


def test_validation_rejects_wrong_filename_hash(tmp_path: Path) -> None:
    """Catches validation that checks bytes but not the content-addressed filename."""
    warehouse = FinancialFactWarehouse(tmp_path)
    filing = financial_filing()
    artifact = warehouse.write_facts(filing, [mapped_fact("assets", Decimal("1000"))])
    wrong_path = artifact.path.with_name(f"filing-{filing.filing_id}-{'d' * 64}.parquet")
    shutil.copyfile(artifact.path, wrong_path)
    wrong_name = FinancialArtifact(wrong_path, artifact.content_hash, artifact.fact_count)

    with pytest.raises(ValueError, match="^FINANCIAL_PARQUET_INTEGRITY_ERROR$"):
        warehouse.validate_artifact(wrong_name, filing.filing_id)


def test_validation_rejects_wrong_expected_hash_or_filing_id(tmp_path: Path) -> None:
    """Catches validation that ignores manifest hash or the caller's filing identity."""
    warehouse = FinancialFactWarehouse(tmp_path)
    filing = financial_filing()
    artifact = warehouse.write_facts(filing, [mapped_fact("assets", Decimal("1000"))])
    wrong_hash = FinancialArtifact(artifact.path, "e" * 64, artifact.fact_count)

    with pytest.raises(ValueError, match="^FINANCIAL_PARQUET_INTEGRITY_ERROR$"):
        warehouse.validate_artifact(wrong_hash, filing.filing_id)
    with pytest.raises(ValueError, match="^FINANCIAL_PARQUET_INTEGRITY_ERROR$"):
        warehouse.validate_artifact(artifact, "another-filing")


def test_corrupt_existing_artifact_is_never_overwritten(tmp_path: Path) -> None:
    """Catches replace/rename publication that overwrites an existing corrupt target."""
    warehouse = FinancialFactWarehouse(tmp_path)
    filing = financial_filing()
    facts = [mapped_fact("assets", Decimal("1000"))]
    expected = warehouse.expected_artifact(filing, facts)
    expected.path.parent.mkdir(parents=True)
    expected.path.write_bytes(b"corrupt")

    with pytest.raises(ValueError, match="^FINANCIAL_PARQUET_INTEGRITY_ERROR$"):
        warehouse.write_facts(filing, facts)
    assert expected.path.read_bytes() == b"corrupt"


def test_publication_race_validates_winner_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a FileExists race treated as success without validating its winner."""
    warehouse = FinancialFactWarehouse(tmp_path)
    filing = financial_filing()
    real_link = os.link

    def publish_winner_then_lose(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *args: object,
        **kwargs: object,
    ) -> None:
        real_link(source, destination, *args, **kwargs)
        raise FileExistsError

    monkeypatch.setattr(financial_module.os, "link", publish_winner_then_lose)

    artifact = warehouse.write_facts(filing, [mapped_fact("assets", Decimal("1000"))])

    assert warehouse.validate_artifact(artifact, filing.filing_id) == artifact.content_hash
    assert not list(artifact.path.parent.glob(".*.tmp"))


def test_corrupt_publication_race_winner_is_rejected_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a losing publisher that overwrites or accepts a corrupt race winner."""
    warehouse = FinancialFactWarehouse(tmp_path)
    filing = financial_filing()

    def publish_corrupt_winner_then_lose(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *args: object,
        **kwargs: object,
    ) -> None:
        del source, args, kwargs
        Path(destination).write_bytes(b"race-winner-corrupt")
        raise FileExistsError

    monkeypatch.setattr(financial_module.os, "link", publish_corrupt_winner_then_lose)
    expected = warehouse.expected_artifact(filing, [mapped_fact("assets", Decimal("1000"))])

    with pytest.raises(ValueError, match="^FINANCIAL_PARQUET_INTEGRITY_ERROR$"):
        warehouse.write_facts(filing, [mapped_fact("assets", Decimal("1000"))])

    assert expected.path.read_bytes() == b"race-winner-corrupt"
    assert not list(expected.path.parent.glob(".*.tmp"))


def test_write_fsyncs_closed_same_directory_temp_before_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches skipped fsync, cross-directory temp files, and linking an open handle."""
    warehouse = FinancialFactWarehouse(tmp_path)
    filing = financial_filing()
    real_fsync = os.fsync
    real_link = os.link
    fsync_calls = 0
    link_observation: tuple[Path, Path, bytes] | None = None

    def record_fsync(file_descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        real_fsync(file_descriptor)

    def observe_link(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal link_observation
        source_path = Path(source)
        destination_path = Path(destination)
        with source_path.open("rb") as readable:
            prefix = readable.read(4)
        link_observation = (source_path.parent, destination_path.parent, prefix)
        real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(financial_module.os, "fsync", record_fsync)
    monkeypatch.setattr(financial_module.os, "link", observe_link)

    artifact = warehouse.write_facts(filing, [mapped_fact("assets", Decimal("1000"))])

    assert fsync_calls == 1
    assert link_observation == (artifact.path.parent, artifact.path.parent, b"PAR1")
    assert not list(artifact.path.parent.glob(".*.tmp"))
