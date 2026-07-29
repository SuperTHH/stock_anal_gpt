import io
import socket
import sqlite3
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hengce.bootstrap import bootstrap_state
from hengce.cli import app
from hengce.config import Settings
from hengce.contracts.enums import (
    DiscoveryMethod,
    MappingStatus,
    QualityStatus,
    ReportType,
    RunStatus,
    StatementType,
)
from hengce.contracts.financial import (
    FilingDescriptor,
    FinancialFact,
    TaxonomyPackageRef,
)
from hengce.financials.mapping import (
    FactMapping,
    FactMappingRegistry,
    FinancialFactNormalizer,
)
from hengce.financials.package import SafePackageMaterializer
from hengce.financials.quality import FinancialQualityValidator
from hengce.financials.query import AsOfFinancialQuery, FinancialQueryResult
from hengce.financials.xbrl import ArelleXbrlProcessor
from hengce.policy.guard import PolicyGuard
from hengce.raw_store.store import RawObjectStore
from hengce.services.financial_ingestion import (
    FinancialIngestionResult,
    FinancialIngestionService,
)
from hengce.state.financial_repository import (
    FinancialArtifactRecord,
    FinancialFilingRepository,
)
from hengce.state.repository import StateRepository
from hengce.warehouse.financial import FinancialFactWarehouse

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "xbrl" / "minimal"
FIXTURE_SOURCE_URL = "https://www.sse.com.cn/fixture/not-a-real-issuer.xml"
FIXTURE_TAXONOMY_URL = "https://www.sse.com.cn/fixture/test-gaap.xsd"
FIXTURE_TAXONOMY_ID = "test-gaap-2025"
FIXTURE_QNAME_PREFIX = "{urn:hengce:test-gaap}"
FIXTURE_REPORT_PERIOD = date(2025, 12, 31)
FIXTURE_PUBLISHED_AT = datetime(2026, 4, 30, 1, tzinfo=UTC)
FIXTURE_VALID_FROM = datetime(2026, 7, 26, 4, tzinfo=UTC)


@dataclass
class FixtureClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@dataclass(frozen=True)
class AcceptanceSystem:
    service: FinancialIngestionService
    query_engine: AsOfFinancialQuery
    repository: FinancialFilingRepository
    raw_store: RawObjectStore
    warehouse: FinancialFactWarehouse
    state: StateRepository
    mapping: FactMappingRegistry
    clock: FixtureClock
    descriptor: FilingDescriptor
    correction_descriptor: FilingDescriptor
    missing_taxonomy_descriptor: FilingDescriptor
    conflict_descriptor: FilingDescriptor
    unmapped_descriptor: FilingDescriptor
    published_at: datetime
    valid_from: datetime

    def ingest_valid_fixture(self) -> FinancialIngestionResult:
        self.clock.value = self.descriptor.collected_at
        return self.service.run(self.descriptor)

    def ingest_old_and_correction(
        self,
    ) -> tuple[FinancialArtifactRecord, FinancialArtifactRecord]:
        old_result = self.ingest_valid_fixture()
        self.clock.value = self.correction_descriptor.collected_at
        corrected_result = self.service.run(self.correction_descriptor)
        old = self.repository.get_filing(old_result.filing_id)
        corrected = self.repository.get_filing(corrected_result.filing_id)
        assert old is not None and corrected is not None
        return old, corrected

    def ingest_missing_taxonomy(self) -> FinancialIngestionResult:
        self.clock.value = self.missing_taxonomy_descriptor.collected_at
        return self.service.run(self.missing_taxonomy_descriptor)

    def ingest_conflict(self) -> FinancialIngestionResult:
        self.clock.value = self.conflict_descriptor.collected_at
        return self.service.run(self.conflict_descriptor)

    def ingest_unmapped(self) -> FinancialIngestionResult:
        self.clock.value = self.unmapped_descriptor.collected_at
        return self.service.run(self.unmapped_descriptor)

    def query(
        self,
        *,
        canonical_names: frozenset[str],
        as_of: datetime,
        known_at: datetime,
    ) -> FinancialQueryResult:
        return self.query_engine.query_financial_facts(
            ts_code=self.descriptor.ts_code,
            report_period=self.descriptor.report_period,
            canonical_fact_names=canonical_names,
            as_of=as_of,
            known_at=known_at,
        )

    def asset_value(self, *, as_of: datetime, known_at: datetime) -> Decimal:
        result = self.query(
            canonical_names=frozenset({"assets"}),
            as_of=as_of,
            known_at=known_at,
        )
        return Decimal(str(result.facts[0]["fact_value"]))

    def artifact_facts(self, filing_id: str) -> tuple[FinancialFact, ...]:
        record = self.repository.get_filing(filing_id)
        assert record is not None
        return tuple(
            FinancialFact.model_validate(row)
            for row in self.warehouse.read_artifact(Path(record.expected_path))
        )

    def crash_after_artifact_write(self) -> FinancialArtifactRecord:
        crashed = False

        def crash_once(stage: str) -> None:
            nonlocal crashed
            if stage == "after_artifact_write" and not crashed:
                crashed = True
                raise RuntimeError("injected acceptance crash")

        self.clock.value = self.descriptor.collected_at
        with pytest.raises(RuntimeError, match="injected acceptance crash"):
            self._service(stage_hook=crash_once).run(self.descriptor)

        pending = self.repository.list_filing_versions(
            self.descriptor.ts_code,
            self.descriptor.report_period,
        )
        assert len(pending) == 1
        assert pending[0].artifact_status == "PENDING"
        assert Path(pending[0].expected_path).is_file()
        return pending[0]

    def recover_crashed_filing(self) -> FinancialIngestionResult:
        self.clock.value = self.descriptor.collected_at
        return self._service().run(self.descriptor)

    def write_unregistered_parquet(self, filing_id: str) -> Path:
        registered = self.repository.get_filing(filing_id)
        assert registered is not None
        source_fact = self.artifact_facts(filing_id)[0]
        rogue_filing_id = f"{filing_id}-unregistered"
        rogue_filing = registered.filing.model_copy(
            update={
                "record_id": rogue_filing_id,
                "filing_id": rogue_filing_id,
                "supersedes_id": None,
            }
        )
        rogue_fact_id = f"{source_fact.fact_id}-unregistered"
        rogue_fact = source_fact.model_copy(
            update={
                "record_id": rogue_fact_id,
                "fact_id": rogue_fact_id,
                "filing_id": rogue_filing_id,
                "canonical_fact_name": "rogue_metric",
                "mapping_status": MappingStatus.MAPPED,
                "quality_status": QualityStatus.VALID,
            }
        )
        return self.warehouse.write_facts(rogue_filing, [rogue_fact]).path

    def _service(
        self,
        stage_hook: Callable[[str], None] | None = None,
    ) -> FinancialIngestionService:
        return _financial_ingestion_service(
            state=self.state,
            repository=self.repository,
            raw_store=self.raw_store,
            warehouse=self.warehouse,
            mapping=self.mapping,
            clock=self.clock,
            stage_hook=stage_hook,
        )


def build_acceptance_system(tmp_path: Path) -> AcceptanceSystem:
    data_dir = tmp_path / "data"
    state = bootstrap_state(Settings(data_dir=data_dir))
    repository = FinancialFilingRepository(state.path)
    raw_store = RawObjectStore(data_dir / "raw")
    warehouse = FinancialFactWarehouse(data_dir / "warehouse")
    clock = FixtureClock(FIXTURE_VALID_FROM)

    taxonomy_payload = _fixture_taxonomy_payload()
    taxonomy_raw = raw_store.put(
        source_id="sse",
        source_url=FIXTURE_TAXONOMY_URL,
        collected_at=FIXTURE_VALID_FROM,
        content_type="application/xml-schema",
        payload=taxonomy_payload,
    )
    repository.register_taxonomy(
        TaxonomyPackageRef(
            taxonomy_id=FIXTURE_TAXONOMY_ID,
            source_id="sse",
            source_url=FIXTURE_TAXONOMY_URL,
            raw_object_hash=taxonomy_raw.content_hash,
            package_name="test-gaap.xsd",
            entrypoint="test-gaap.xsd",
            content_type="application/xml-schema",
            collected_at=FIXTURE_VALID_FROM,
        )
    )

    mapping = _fixture_mapping_registry()
    descriptor = _fixture_descriptor(
        raw_store,
        _fixture_instance_payload(),
        published_at=FIXTURE_PUBLISHED_AT,
        collected_at=FIXTURE_VALID_FROM,
    )
    correction_descriptor = _fixture_descriptor(
        raw_store,
        _correction_instance_payload(),
        published_at=FIXTURE_PUBLISHED_AT + timedelta(days=1),
        collected_at=FIXTURE_VALID_FROM + timedelta(days=1),
    )
    conflict_descriptor = _fixture_descriptor(
        raw_store,
        _conflict_instance_payload(),
        published_at=FIXTURE_PUBLISHED_AT + timedelta(days=2),
        collected_at=FIXTURE_VALID_FROM + timedelta(days=2),
    )
    unmapped_descriptor = _fixture_descriptor(
        raw_store,
        _unmapped_instance_payload(),
        published_at=FIXTURE_PUBLISHED_AT + timedelta(days=3),
        collected_at=FIXTURE_VALID_FROM + timedelta(days=3),
    )
    missing_taxonomy_descriptor = descriptor.model_copy(
        update={"taxonomy_refs": ("missing-test-taxonomy",)}
    )
    service = _financial_ingestion_service(
        state=state,
        repository=repository,
        raw_store=raw_store,
        warehouse=warehouse,
        mapping=mapping,
        clock=clock,
    )
    return AcceptanceSystem(
        service=service,
        query_engine=AsOfFinancialQuery(
            repository=repository,
            warehouse_root=data_dir / "warehouse" / "financial_facts",
        ),
        repository=repository,
        raw_store=raw_store,
        warehouse=warehouse,
        state=state,
        mapping=mapping,
        clock=clock,
        descriptor=descriptor,
        correction_descriptor=correction_descriptor,
        missing_taxonomy_descriptor=missing_taxonomy_descriptor,
        conflict_descriptor=conflict_descriptor,
        unmapped_descriptor=unmapped_descriptor,
        published_at=FIXTURE_PUBLISHED_AT,
        valid_from=FIXTURE_VALID_FROM,
    )


def _financial_ingestion_service(
    *,
    state: StateRepository,
    repository: FinancialFilingRepository,
    raw_store: RawObjectStore,
    warehouse: FinancialFactWarehouse,
    mapping: FactMappingRegistry,
    clock: FixtureClock,
    stage_hook: Callable[[str], None] | None = None,
) -> FinancialIngestionService:
    return FinancialIngestionService(
        guard=PolicyGuard(state, clock=clock),
        raw_store=raw_store,
        repository=repository,
        materializer=SafePackageMaterializer(raw_store),
        processor=ArelleXbrlProcessor(),
        normalizer=FinancialFactNormalizer(mapping),
        validator=FinancialQualityValidator(),
        warehouse=warehouse,
        state=state,
        clock=clock,
        stage_hook=stage_hook or (lambda _stage: None),
    )


def _fixture_mapping_registry() -> FactMappingRegistry:
    statement_types = {
        "Assets": StatementType.BALANCE_SHEET,
        "Liabilities": StatementType.BALANCE_SHEET,
        "Equity": StatementType.BALANCE_SHEET,
        "Revenue": StatementType.INCOME_STATEMENT,
    }
    return FactMappingRegistry(
        mapping_version="fixture-mapping-v1",
        mappings={
            f"{FIXTURE_QNAME_PREFIX}{fact_name}": FactMapping(
                raw_qname=f"{FIXTURE_QNAME_PREFIX}{fact_name}",
                canonical_fact_name=fact_name.casefold(),
                statement_type=statement_type,
                expected_unit_kind="MONETARY",
            )
            for fact_name, statement_type in statement_types.items()
        },
    )


def _fixture_descriptor(
    raw_store: RawObjectStore,
    payload: bytes,
    *,
    published_at: datetime,
    collected_at: datetime,
) -> FilingDescriptor:
    raw = raw_store.put(
        source_id="sse",
        source_url=FIXTURE_SOURCE_URL,
        collected_at=collected_at,
        content_type="application/xbrl+xml",
        payload=payload,
    )
    return FilingDescriptor(
        source_id="sse",
        source_url=FIXTURE_SOURCE_URL,
        ts_code="699999.SH",
        exchange="SSE",
        report_period=FIXTURE_REPORT_PERIOD,
        report_type=ReportType.ANNUAL,
        published_at=published_at,
        collected_at=collected_at,
        attachment_name="instance.xml",
        content_type="application/xbrl+xml",
        raw_object_hash=raw.content_hash,
        taxonomy_refs=(FIXTURE_TAXONOMY_ID,),
        discovery_method=DiscoveryMethod.FIXTURE,
        instance_entrypoint=None,
    )


def _fixture_instance_payload() -> bytes:
    return (FIXTURE_ROOT / "instance.xml").read_bytes()


def _fixture_taxonomy_payload() -> bytes:
    return (
        (FIXTURE_ROOT / "test-gaap.xsd")
        .read_bytes()
        .replace(
            b"</xsd:schema>",
            (
                b'<xsd:element name="UnmappedMetric" id="t_UnmappedMetric" '
                b'type="xbrli:monetaryItemType" substitutionGroup="xbrli:item" '
                b'xbrli:periodType="instant"/>\n'
                b"</xsd:schema>"
            ),
        )
    )


def _correction_instance_payload() -> bytes:
    assets_corrected = _fixture_instance_payload().replace(
        b'<t:Assets contextRef="instant" unitRef="CNY" decimals="0">1000</t:Assets>',
        b'<t:Assets contextRef="instant" unitRef="CNY" decimals="0">1100</t:Assets>',
        1,
    )
    return assets_corrected.replace(
        b'<t:Equity contextRef="instant" unitRef="CNY" decimals="0">600</t:Equity>',
        b'<t:Equity contextRef="instant" unitRef="CNY" decimals="0">700</t:Equity>',
        1,
    )


def _conflict_instance_payload() -> bytes:
    original = b'<t:Assets contextRef="instant" unitRef="CNY" decimals="0">1000</t:Assets>'
    conflicting = (
        original
        + b"\n  "
        + b'<t:Assets contextRef="instant" unitRef="CNY" decimals="0">1100</t:Assets>'
    )
    return _fixture_instance_payload().replace(original, conflicting, 1)


def _unmapped_instance_payload() -> bytes:
    marker = b'<t:Assets contextRef="instant" unitRef="CNY" xsi:nil="true"/>'
    unmapped = (
        b'<t:UnmappedMetric contextRef="instant" unitRef="CNY" '
        b'decimals="0">77</t:UnmappedMetric>\n  '
    )
    return _fixture_instance_payload().replace(marker, unmapped + marker, 1)


def test_xbrl_kernel_is_offline_traceable_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-XF01/02/03: real offline parsing stays traceable and idempotent."""

    def blocked_connect(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked_connect)
    system = build_acceptance_system(tmp_path)

    first = system.ingest_valid_fixture()
    second = system.ingest_valid_fixture()
    result = system.query(
        canonical_names=frozenset({"assets", "liabilities", "equity", "revenue"}),
        as_of=system.published_at,
        known_at=system.valid_from,
    )

    assert first == second
    assert first.run_status is RunStatus.SUCCEEDED
    assert first.fact_count == 4
    assert len(result.facts) == 4
    assert all(len(str(row["content_hash"])) == 64 for row in result.facts)
    assert all(str(row["source_url"]).startswith("https://www.sse.com.cn/") for row in result.facts)
    assert len(system.repository.list_filing_versions("699999.SH", FIXTURE_REPORT_PERIOD)) == 1
    with sqlite3.connect(system.state.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM financial_filings").fetchone()[0] == 1


def test_fixed_replay_uses_old_then_corrected_value_and_blocks_bad_correction(
    tmp_path: Path,
) -> None:
    """AC-XF04: replay switches at publication and blocks an unusable correction."""
    system = build_acceptance_system(tmp_path)
    old, corrected = system.ingest_old_and_correction()

    assert system.asset_value(
        as_of=corrected.filing.published_at - timedelta(seconds=1),
        known_at=corrected.filing.valid_from,
    ) == Decimal("1000")
    assert system.asset_value(
        as_of=corrected.filing.published_at,
        known_at=corrected.filing.valid_from - timedelta(seconds=1),
    ) == Decimal("1000")
    assert system.asset_value(
        as_of=corrected.filing.published_at,
        known_at=corrected.filing.valid_from,
    ) == Decimal("1100")
    assert old.filing.raw_object_hash != corrected.filing.raw_object_hash

    conflict = system.ingest_conflict()
    blocked = system.query(
        canonical_names=frozenset({"assets"}),
        as_of=system.conflict_descriptor.published_at,
        known_at=system.conflict_descriptor.collected_at,
    )
    assert conflict.run_status is RunStatus.PARTIAL
    assert blocked.facts == ()
    assert blocked.blocked_reasons == ("FINANCIAL_RESTATEMENT_UNUSABLE",)


def test_missing_taxonomy_and_conflict_never_become_queryable(tmp_path: Path) -> None:
    """AC-XF05/06: missing taxonomy blocks and conflicting facts stay noncanonical."""
    system = build_acceptance_system(tmp_path)

    missing = system.ingest_missing_taxonomy()
    assert missing.run_status is RunStatus.BLOCKED
    assert missing.fact_count == 0
    assert system.repository.get_filing(missing.filing_id) is None

    conflict = system.ingest_conflict()
    result = system.query(
        canonical_names=frozenset({"assets"}),
        as_of=system.conflict_descriptor.published_at,
        known_at=system.conflict_descriptor.collected_at,
    )
    assert conflict.run_status is RunStatus.PARTIAL
    assert conflict.error_code == "FINANCIAL_FACT_CONFLICT"
    assert result.facts == ()


@pytest.mark.parametrize(
    ("case", "payload", "suffix", "content_type", "entrypoint", "expected_code"),
    [
        pytest.param(
            "xml",
            b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY secret "body">]><xbrl/>',
            ".xml",
            "application/xbrl+xml",
            None,
            "FINANCIAL_XML_UNSAFE",
            id="unsafe-xml-before-raw-persistence",
        ),
        pytest.param(
            "zip",
            _fixture_instance_payload(),
            ".zip",
            "application/zip",
            "instance.xml",
            "FINANCIAL_ARCHIVE_UNSAFE_PATH",
            id="unsafe-zip-before-raw-persistence",
        ),
    ],
)
def test_unsafe_local_input_is_refused_before_raw_persistence(
    tmp_path: Path,
    case: str,
    payload: bytes,
    suffix: str,
    content_type: str,
    entrypoint: str | None,
    expected_code: str,
) -> None:
    """Unsafe XML and ZIP members fail closed before immutable raw storage."""
    attachment = tmp_path / f"unsafe-{case}{suffix}"
    if suffix == ".zip":
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("instance.xml", payload)
            handle.writestr("../escape.xml", b"<xbrl/>")
        attachment.write_bytes(archive.getvalue())
    else:
        attachment.write_bytes(payload)

    arguments = [
        "import-financial-xbrl",
        "--file",
        str(attachment),
        "--source-id",
        "sse",
        "--source-url",
        FIXTURE_SOURCE_URL,
        "--ts-code",
        "699999.SH",
        "--exchange",
        "SSE",
        "--report-period",
        "2025-12-31",
        "--report-type",
        "ANNUAL",
        "--published-at",
        "2026-04-30T09:00:00+08:00",
        "--collected-at",
        "2026-07-26T12:00:00+08:00",
        "--content-type",
        content_type,
        "--taxonomy-id",
        FIXTURE_TAXONOMY_ID,
        "--data-dir",
        str(tmp_path / "data"),
    ]
    if entrypoint is not None:
        arguments.extend(["--instance-entrypoint", entrypoint])

    result = CliRunner().invoke(app, arguments)

    assert list((tmp_path / "data" / "raw").rglob("payload.bin")) == []
    assert result.exit_code != 0
    assert result.exception is not None
    assert str(result.exception) == expected_code


def test_crash_is_invisible_until_idempotent_recovery(tmp_path: Path) -> None:
    """AC-XF07: a crashed artifact is invisible until one idempotent recovery."""
    system = build_acceptance_system(tmp_path)

    pending = system.crash_after_artifact_write()
    before_recovery = system.query(
        canonical_names=frozenset({"assets"}),
        as_of=system.descriptor.published_at,
        known_at=system.descriptor.collected_at,
    )

    assert pending.artifact_status == "PENDING"
    assert before_recovery.facts == ()
    assert before_recovery.filing_ids == ()

    recovered = system.recover_crashed_filing()
    after_recovery = system.query(
        canonical_names=frozenset({"assets"}),
        as_of=system.descriptor.published_at,
        known_at=system.descriptor.collected_at,
    )

    assert recovered.run_status is RunStatus.SUCCEEDED
    versions = system.repository.list_filing_versions(
        system.descriptor.ts_code,
        system.descriptor.report_period,
    )
    assert len(versions) == 1
    assert versions[0].artifact_status == "PUBLISHED"
    run = system.state.list_runs(run_type="financial_xbrl")[0]
    assert run.run_status is RunStatus.SUCCEEDED
    assert run.retry_count == 1
    assert len(after_recovery.facts) == 1
    assert after_recovery.filing_ids == (recovered.filing_id,)


def test_unregistered_parquet_is_never_visible(tmp_path: Path) -> None:
    """AC-XF07: a valid Parquet without a published manifest stays invisible."""
    system = build_acceptance_system(tmp_path)
    registered = system.ingest_valid_fixture()
    rogue_path = system.write_unregistered_parquet(registered.filing_id)
    result = system.query(
        canonical_names=frozenset({"rogue_metric"}),
        as_of=system.descriptor.published_at,
        known_at=system.descriptor.collected_at,
    )
    assert rogue_path.is_file()
    assert result.facts == ()
    assert result.filing_ids == (registered.filing_id,)


def test_unmapped_qname_is_auditable_but_not_canonical(tmp_path: Path) -> None:
    """AC-XF08: an unmapped QName is retained but excluded from canonical queries."""
    system = build_acceptance_system(tmp_path)

    result = system.ingest_unmapped()
    facts = system.artifact_facts(result.filing_id)
    unmapped = [fact for fact in facts if fact.raw_qname == f"{FIXTURE_QNAME_PREFIX}UnmappedMetric"]
    query = system.query(
        canonical_names=frozenset({"unmapped_metric"}),
        as_of=system.unmapped_descriptor.published_at,
        known_at=system.unmapped_descriptor.collected_at,
    )

    assert result.run_status is RunStatus.PARTIAL
    assert result.error_code == "FINANCIAL_FACT_UNMAPPED"
    assert len(unmapped) == 1
    assert unmapped[0].mapping_status is MappingStatus.UNMAPPED
    assert unmapped[0].canonical_fact_name is None
    assert query.facts == ()


def test_repository_contains_only_marked_fictional_xbrl_fixtures() -> None:
    """AC-XF09/10: the full-suite gate uses only marked fictional XBRL fixtures."""
    prohibited_suffixes = {".xbrl", ".xml", ".xsd", ".zip", ".parquet"}
    local_only_directories = {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
    }
    fixture_files = sorted(
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path in REPOSITORY_ROOT.rglob("*")
        if path.is_file()
        and path.suffix.lower() in prohibited_suffixes
        and not local_only_directories.intersection(path.relative_to(REPOSITORY_ROOT).parts)
    )
    assert fixture_files == [
        "tests/fixtures/xbrl/minimal/instance.xml",
        "tests/fixtures/xbrl/minimal/test-gaap.xsd",
    ]

    instance_text = (FIXTURE_ROOT / "instance.xml").read_text(encoding="utf-8")
    taxonomy_text = (FIXTURE_ROOT / "test-gaap.xsd").read_text(encoding="utf-8")
    assert "FIXTURE DATA - NOT A REAL ISSUER" in instance_text
    assert "urn:hengce:test-gaap" in instance_text
    assert "699999.SH" in instance_text
    assert "FIXTURE DATA - NOT A REAL ISSUER" in taxonomy_text
    assert "urn:hengce:test-gaap" in taxonomy_text
    assert "600001.SH" not in "\n".join((instance_text, taxonomy_text))
