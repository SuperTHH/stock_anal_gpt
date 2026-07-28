import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from hengce.bootstrap import bootstrap_state
from hengce.config import Settings
from hengce.contracts.enums import (
    ConflictResolutionStatus,
    DiscoveryMethod,
    QualityStatus,
    ReportType,
    ReviewStatus,
    RunStatus,
    StatementType,
)
from hengce.contracts.financial import (
    FilingDescriptor,
    FinancialFact,
    TaxonomyPackageRef,
)
from hengce.contracts.policy import SourcePolicy
from hengce.financials.mapping import (
    FactMapping,
    FactMappingRegistry,
    FinancialFactNormalizer,
)
from hengce.financials.package import MaterializedFiling, SafePackageMaterializer
from hengce.financials.quality import FinancialQualityValidator
from hengce.financials.xbrl import (
    RawXbrlContext,
    RawXbrlFact,
    RawXbrlUnit,
    XbrlParseDiagnostics,
    XbrlParseResult,
)
from hengce.policy.guard import PolicyGuard
from hengce.raw_store.store import RawObjectStore
from hengce.services.financial_ingestion import (
    ERROR_STATUS,
    FinancialIngestionService,
)
from hengce.state.financial_repository import FinancialFilingRepository
from hengce.state.repository import StateRepository
from hengce.warehouse.financial import FinancialFactWarehouse

NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)
REPORT_PERIOD = date(2025, 12, 31)
SOURCE_URL = "https://www.sse.com.cn/disclosure/filing.xml"
TAXONOMY_ID = "test-gaap-2025"
TAXONOMY_BYTES = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
    b'targetNamespace="urn:hengce:test-gaap"/>'
)
CNY_MEASURE = "{http://www.xbrl.org/2003/iso4217}CNY"
QNAME_PREFIX = "{urn:hengce:test-gaap}"
INSTANCE_ONE = b'<?xml version="1.0"?><xbrl xmlns="http://www.xbrl.org/2003/instance"/>'
INSTANCE_TWO = (
    b'<?xml version="1.0"?><xbrl xmlns="http://www.xbrl.org/2003/instance" version="restated"/>'
)


class FakeProcessor:
    def __init__(
        self,
        facts: tuple[RawXbrlFact, ...],
        *,
        error: Exception | None = None,
    ) -> None:
        self.facts = facts
        self.error = error
        self.calls = 0

    def parse(self, materialized: MaterializedFiling) -> XbrlParseResult:
        self.calls += 1
        assert materialized.entrypoint_path.is_file()
        if self.error is not None:
            raise self.error
        return XbrlParseResult(
            parser_name="fixture-parser",
            parser_version="1.0",
            contexts=tuple(
                sorted(
                    {fact.context for fact in self.facts},
                    key=lambda context: context.context_id,
                )
            ),
            units=tuple(
                sorted(
                    {fact.unit for fact in self.facts if fact.unit is not None},
                    key=lambda unit: unit.unit_id,
                )
            ),
            facts=self.facts,
            diagnostics=XbrlParseDiagnostics(
                nil_fact_count=0,
                text_fact_count=0,
                error_codes=(),
            ),
        )


class CrashOnce:
    def __init__(self, target: str) -> None:
        self.target = target
        self.crashed = False

    def __call__(self, stage: str) -> None:
        if stage == self.target and not self.crashed:
            self.crashed = True
            raise RuntimeError("injected crash")


class NoIoDependency:
    def __getattribute__(self, name: str) -> object:
        if name.startswith("__"):
            return object.__getattribute__(self, name)
        raise AssertionError(f"constructor performed I/O through {name}")


@dataclass(frozen=True)
class BuiltService:
    service: FinancialIngestionService
    state: StateRepository
    repository: FinancialFilingRepository
    raw_store: RawObjectStore
    warehouse: FinancialFactWarehouse
    descriptor: FilingDescriptor
    processor: FakeProcessor


def raw_fact(
    name: str,
    value: str,
    *,
    context_id: str | None = None,
) -> RawXbrlFact:
    return RawXbrlFact(
        raw_qname=f"{QNAME_PREFIX}{name.title()}",
        fact_name=name.title(),
        value=Decimal(value),
        decimals="0",
        context=RawXbrlContext(
            context_id=context_id or f"context-{name}",
            entity_scheme="https://www.sse.com.cn/entity",
            entity_identifier="600001.SH",
            period_start=None,
            period_end=None,
            instant=REPORT_PERIOD,
            dimensions=(),
        ),
        unit=RawXbrlUnit(
            unit_id="unit-cny",
            numerator_measures=(CNY_MEASURE,),
            denominator_measures=(),
            currency="CNY",
        ),
    )


def valid_raw_facts() -> tuple[RawXbrlFact, ...]:
    return (
        raw_fact("assets", "1000"),
        raw_fact("liabilities", "400"),
        raw_fact("equity", "600"),
        raw_fact("revenue", "250"),
    )


def conflicting_raw_facts() -> tuple[RawXbrlFact, ...]:
    return (
        raw_fact("assets", "1000", context_id="same-assets-context"),
        raw_fact("assets", "1100", context_id="same-assets-context"),
        raw_fact("liabilities", "400"),
        raw_fact("equity", "600"),
    )


def normalizer() -> FinancialFactNormalizer:
    statement_types = {
        "assets": StatementType.BALANCE_SHEET,
        "liabilities": StatementType.BALANCE_SHEET,
        "equity": StatementType.BALANCE_SHEET,
        "revenue": StatementType.INCOME_STATEMENT,
    }
    return FinancialFactNormalizer(
        FactMappingRegistry(
            mapping_version="fixture-mapping-v1",
            mappings={
                f"{QNAME_PREFIX}{name.title()}": FactMapping(
                    raw_qname=f"{QNAME_PREFIX}{name.title()}",
                    canonical_fact_name=name,
                    statement_type=statement_type,
                    expected_unit_kind="MONETARY",
                )
                for name, statement_type in statement_types.items()
            },
        )
    )


def approved_policy() -> SourcePolicy:
    return SourcePolicy(
        source_id="sse",
        source_name="Shanghai Stock Exchange",
        allowed_domains=["www.sse.com.cn"],
        allowed_schemes=["https"],
        allowed_purposes=["xbrl"],
        fetch_frequency="manual",
        full_text_rule="metadata-only",
        attachment_rule="fixture-research-only",
        rate_limit_per_minute=60,
        robots_policy="respect",
        terms_url="https://www.sse.com.cn/terms",
        terms_reviewed_at=NOW,
        review_status=ReviewStatus.APPROVED,
        connection_status="fixture",
        enabled=True,
    )


def add_descriptor(
    raw_store: RawObjectStore,
    payload: bytes,
    *,
    collected_at: datetime = NOW,
    source_id: str = "sse",
    report_type: ReportType = ReportType.ANNUAL,
) -> FilingDescriptor:
    raw = raw_store.put(
        source_id=source_id,
        source_url=SOURCE_URL,
        collected_at=collected_at,
        content_type="application/xml",
        payload=payload,
    )
    return FilingDescriptor(
        source_id=source_id,
        source_url=SOURCE_URL,
        ts_code="600001.SH",
        exchange="SSE",
        report_period=REPORT_PERIOD,
        report_type=report_type,
        published_at=NOW,
        collected_at=collected_at,
        attachment_name="filing.xml",
        content_type="application/xml",
        raw_object_hash=raw.content_hash,
        taxonomy_refs=(TAXONOMY_ID,),
        discovery_method=DiscoveryMethod.FIXTURE,
        instance_entrypoint=None,
    )


def build_service(
    tmp_path: Path,
    *,
    facts: tuple[RawXbrlFact, ...] | None = None,
    processor: FakeProcessor | None = None,
    register_taxonomy: bool = True,
    register_policy: bool = True,
    stage_hook: object | None = None,
    clock: object | None = None,
) -> BuiltService:
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()
    repository = FinancialFilingRepository(state.path)
    raw_store = RawObjectStore(tmp_path / "raw")
    warehouse = FinancialFactWarehouse(tmp_path / "warehouse")
    descriptor = add_descriptor(raw_store, INSTANCE_ONE)
    taxonomy_raw = raw_store.put(
        source_id="sse",
        source_url="https://www.sse.com.cn/taxonomy.xsd",
        collected_at=NOW,
        content_type="application/xml",
        payload=TAXONOMY_BYTES,
    )
    if register_taxonomy:
        repository.register_taxonomy(
            TaxonomyPackageRef(
                taxonomy_id=TAXONOMY_ID,
                source_id="sse",
                source_url="https://www.sse.com.cn/taxonomy.xsd",
                raw_object_hash=taxonomy_raw.content_hash,
                package_name="taxonomy.xsd",
                entrypoint="taxonomy.xsd",
                content_type="application/xml",
                collected_at=NOW,
            )
        )
    if register_policy:
        state.upsert_policy(approved_policy())
    resolved_processor = processor or FakeProcessor(facts or valid_raw_facts())
    service = FinancialIngestionService(
        guard=PolicyGuard(state, clock=cast(object, clock) if clock else lambda: NOW),
        raw_store=raw_store,
        repository=repository,
        materializer=SafePackageMaterializer(raw_store),
        processor=resolved_processor,
        normalizer=normalizer(),
        validator=FinancialQualityValidator(),
        warehouse=warehouse,
        state=state,
        clock=cast(object, clock) if clock else lambda: NOW,
        stage_hook=cast(object, stage_hook) if stage_hook else lambda _stage: None,
    )
    return BuiltService(
        service=service,
        state=state,
        repository=repository,
        raw_store=raw_store,
        warehouse=warehouse,
        descriptor=descriptor,
        processor=resolved_processor,
    )


def artifact_facts(
    built: BuiltService,
    filing_id: str,
) -> list[FinancialFact]:
    record = built.repository.get_filing(filing_id)
    assert record is not None
    return [
        FinancialFact.model_validate(row)
        for row in built.warehouse.read_artifact(Path(record.expected_path))
    ]


def test_constructor_does_not_touch_dependencies_or_filesystem() -> None:
    dependency = NoIoDependency()

    service = FinancialIngestionService(
        guard=cast(object, dependency),
        raw_store=cast(object, dependency),
        repository=cast(object, dependency),
        materializer=cast(object, dependency),
        processor=cast(object, dependency),
        normalizer=cast(object, dependency),
        validator=cast(object, dependency),
        warehouse=cast(object, dependency),
        state=cast(object, dependency),
    )

    assert service.guard is dependency


def test_ingestion_publishes_valid_filing_and_terminal_run(tmp_path: Path) -> None:
    built = build_service(tmp_path)

    result = built.service.run(built.descriptor)

    assert result.run_status is RunStatus.SUCCEEDED
    assert result.fact_count == 4
    assert result.conflict_count == 0
    filing = built.repository.get_filing(result.filing_id)
    assert filing is not None
    assert filing.artifact_status == "PUBLISHED"
    assert filing.filing.valid_from == NOW
    assert all(fact.valid_from == NOW for fact in artifact_facts(built, result.filing_id))
    run = built.state.list_runs(run_type="financial_xbrl")[0]
    assert run.run_id == result.run_id
    assert run.run_status is RunStatus.SUCCEEDED
    assert run.finished_at == NOW
    assert run.published_report_id == result.filing_id


@pytest.mark.parametrize(
    ("source_id", "source_url", "ts_code", "exchange"),
    [
        ("sse", "https://www.sse.com.cn/disclosure/filing.xml", "600001.SH", "SSE"),
        ("szse", "https://www.szse.cn/disclosure/filing.xml", "300001.SZ", "SZSE"),
    ],
)
def test_default_exchange_xbrl_policy_allows_ingestion_without_purpose_denial(
    tmp_path: Path,
    source_id: str,
    source_url: str,
    ts_code: str,
    exchange: str,
) -> None:
    defaults = bootstrap_state(Settings(data_dir=tmp_path / "defaults"))
    policy = defaults.get_policy(source_id)
    assert policy is not None
    assert "xbrl" in policy.allowed_purposes
    assert "financial_xbrl" not in policy.allowed_purposes
    built = build_service(tmp_path / "service")
    built.state.upsert_policy(policy)
    descriptor = built.descriptor.model_copy(
        update={
            "source_id": source_id,
            "source_url": source_url,
            "ts_code": ts_code,
            "exchange": exchange,
        }
    )

    result = built.service.run(descriptor)

    assert result.run_status is RunStatus.SUCCEEDED
    assert result.error_code is None
    assert built.state.count_refusals() == 0
    assert built.state.list_runs(run_type="financial_xbrl")[0].run_status is RunStatus.SUCCEEDED


def test_repeating_same_descriptor_reuses_terminal_result_without_new_parse(
    tmp_path: Path,
) -> None:
    built = build_service(tmp_path)

    first = built.service.run(built.descriptor)
    second = built.service.run(built.descriptor)

    assert first == second
    assert built.processor.calls == 1
    assert len(built.repository.list_filing_versions("600001.SH", REPORT_PERIOD)) == 1
    assert len(built.state.list_runs(run_type="financial_xbrl")) == 1
    with sqlite3.connect(built.state.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM financial_filings").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM financial_fact_conflicts").fetchone()[0] == 0
        )


def test_missing_taxonomy_blocks_without_parser_call(tmp_path: Path) -> None:
    built = build_service(tmp_path, register_taxonomy=False)

    result = built.service.run(built.descriptor)

    assert result.run_status is RunStatus.BLOCKED
    assert result.error_code == "FINANCIAL_TAXONOMY_MISSING"
    assert built.processor.calls == 0
    run = built.state.list_runs(run_type="financial_xbrl")[0]
    assert run.run_status is RunStatus.BLOCKED
    assert run.finished_at == NOW


def test_policy_denial_records_one_refusal_and_never_calls_parser(
    tmp_path: Path,
) -> None:
    built = build_service(tmp_path, register_policy=False)

    first = built.service.run(built.descriptor)
    second = built.service.run(built.descriptor)

    assert first == second
    assert first.run_status is RunStatus.BLOCKED
    assert first.error_code == "SOURCE_POLICY_MISSING"
    assert built.state.count_refusals() == 1
    assert built.processor.calls == 0
    run = built.state.list_runs(run_type="financial_xbrl")[0]
    assert run.run_status is RunStatus.BLOCKED
    assert run.error_code == "SOURCE_POLICY_MISSING"


def test_conflicting_facts_publish_partial_and_record_open_conflict(
    tmp_path: Path,
) -> None:
    built = build_service(tmp_path, facts=conflicting_raw_facts())

    result = built.service.run(built.descriptor)

    assert result.run_status is RunStatus.PARTIAL
    assert result.error_code == "FINANCIAL_FACT_CONFLICT"
    assert result.conflict_count == 1
    conflicts = built.repository.list_conflicts(result.filing_id)
    assert len(conflicts) == 1
    assert conflicts[0].resolution_status is ConflictResolutionStatus.OPEN
    assert conflicts[0].quality_status is QualityStatus.CONFLICT
    filing = built.repository.get_filing(result.filing_id)
    assert filing is not None
    assert filing.artifact_status == "PUBLISHED"


@pytest.mark.parametrize(
    ("facts", "expected_code"),
    [
        ((), "FINANCIAL_NUMERIC_FACTS_MISSING"),
        ((raw_fact("unknown", "1"),), "FINANCIAL_FACT_UNMAPPED"),
    ],
)
def test_quality_issue_codes_map_to_partial_publication(
    tmp_path: Path,
    facts: tuple[RawXbrlFact, ...],
    expected_code: str,
) -> None:
    built = build_service(tmp_path, processor=FakeProcessor(facts))

    result = built.service.run(built.descriptor)

    assert result.run_status is RunStatus.PARTIAL
    assert result.error_code == expected_code
    filing = built.repository.get_filing(result.filing_id)
    assert filing is not None
    assert filing.artifact_status == "PUBLISHED"


def test_correction_links_matching_fact_observations_without_overwriting_old(
    tmp_path: Path,
) -> None:
    built = build_service(tmp_path)
    old = built.service.run(built.descriptor)
    old_record = built.repository.get_filing(old.filing_id)
    assert old_record is not None
    old_bytes = Path(old_record.expected_path).read_bytes()
    old_facts = artifact_facts(built, old.filing_id)
    restated_descriptor = add_descriptor(
        built.raw_store,
        INSTANCE_TWO,
        collected_at=NOW.replace(minute=1),
    )

    new = built.service.run(restated_descriptor)

    versions = built.repository.list_filing_versions("600001.SH", REPORT_PERIOD)
    assert [item.filing.filing_id for item in versions] == [old.filing_id, new.filing_id]
    assert versions[1].filing.is_restated
    assert versions[1].filing.supersedes_id == old.filing_id
    new_facts = artifact_facts(built, new.filing_id)
    old_by_comparison = {fact.comparison_identity_hash: fact for fact in old_facts}
    assert all(
        fact.supersedes_id == old_by_comparison[fact.comparison_identity_hash].fact_id
        for fact in new_facts
    )
    assert all(fact.fact_id != fact.supersedes_id for fact in new_facts)
    assert Path(old_record.expected_path).read_bytes() == old_bytes


def test_different_source_does_not_create_correction_chain(tmp_path: Path) -> None:
    built = build_service(tmp_path)
    built.service.run(built.descriptor)
    built.state.upsert_policy(
        approved_policy().model_copy(
            update={
                "source_id": "alternate-sse",
                "source_name": "Alternate SSE",
            }
        )
    )
    other_source = add_descriptor(
        built.raw_store,
        INSTANCE_TWO,
        collected_at=NOW.replace(minute=1),
        source_id="alternate-sse",
    )

    result = built.service.run(other_source)

    filing = built.repository.get_filing(result.filing_id)
    assert filing is not None
    assert not filing.filing.is_restated
    assert filing.filing.supersedes_id is None
    assert all(fact.supersedes_id is None for fact in artifact_facts(built, result.filing_id))


def test_different_report_type_does_not_create_correction_chain(tmp_path: Path) -> None:
    built = build_service(tmp_path)
    built.service.run(built.descriptor)
    other_report_type = add_descriptor(
        built.raw_store,
        INSTANCE_TWO,
        collected_at=NOW.replace(minute=1),
        report_type=ReportType.Q1,
    )

    result = built.service.run(other_report_type)

    filing = built.repository.get_filing(result.filing_id)
    assert filing is not None
    assert not filing.filing.is_restated
    assert filing.filing.supersedes_id is None
    assert all(fact.supersedes_id is None for fact in artifact_facts(built, result.filing_id))


def test_crash_after_artifact_write_recovers_without_duplicate_publication(
    tmp_path: Path,
) -> None:
    crash_once = CrashOnce("after_artifact_write")
    built = build_service(tmp_path, stage_hook=crash_once)

    with pytest.raises(RuntimeError, match="injected crash"):
        built.service.run(built.descriptor)

    pending = built.repository.list_filing_versions("600001.SH", REPORT_PERIOD)
    assert len(pending) == 1
    assert pending[0].artifact_status == "PENDING"
    assert Path(pending[0].expected_path).is_file()
    crashed_run = built.state.list_runs(run_type="financial_xbrl")[0]
    assert crashed_run.run_status is RunStatus.RUNNING
    assert crashed_run.finished_at is None

    recovered = build_service(tmp_path)
    result = recovered.service.run(recovered.descriptor)

    assert result.run_status is RunStatus.SUCCEEDED
    assert len(recovered.repository.list_filing_versions("600001.SH", REPORT_PERIOD)) == 1
    terminal_run = recovered.state.list_runs(run_type="financial_xbrl")[0]
    assert terminal_run.run_status is RunStatus.SUCCEEDED
    assert terminal_run.retry_count == 1
    with sqlite3.connect(recovered.state.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM financial_filings").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM financial_fact_conflicts").fetchone()[0] == 0
        )


def test_unknown_processor_exception_is_not_disguised_as_financial_status(
    tmp_path: Path,
) -> None:
    processor = FakeProcessor(valid_raw_facts(), error=RuntimeError("unexpected parser bug"))
    built = build_service(tmp_path, processor=processor)

    with pytest.raises(RuntimeError, match="unexpected parser bug"):
        built.service.run(built.descriptor)

    run = built.state.list_runs(run_type="financial_xbrl")[0]
    assert run.run_status is RunStatus.RUNNING
    assert run.finished_at is None
    assert run.error_code is None


def test_typed_parser_failure_terminalizes_with_declared_failed_mapping(
    tmp_path: Path,
) -> None:
    processor = FakeProcessor(
        valid_raw_facts(),
        error=ValueError("FINANCIAL_XBRL_PARSE_ERROR"),
    )
    built = build_service(tmp_path, processor=processor)

    result = built.service.run(built.descriptor)

    assert result.run_status is RunStatus.FAILED
    assert result.error_code == "FINANCIAL_XBRL_PARSE_ERROR"
    run = built.state.list_runs(run_type="financial_xbrl")[0]
    assert run.run_status is RunStatus.FAILED
    assert run.finished_at == NOW


def test_error_status_is_explicit_and_does_not_guess_unknown_exceptions() -> None:
    assert ERROR_STATUS == {
        "RAW_PAYLOAD_INTEGRITY_ERROR": RunStatus.FAILED,
        "FINANCIAL_TAXONOMY_MISSING": RunStatus.BLOCKED,
        "FINANCIAL_XBRL_PARSE_ERROR": RunStatus.FAILED,
        "FINANCIAL_PARQUET_INTEGRITY_ERROR": RunStatus.FAILED,
        "FINANCIAL_NUMERIC_FACTS_MISSING": RunStatus.PARTIAL,
        "FINANCIAL_FACT_UNMAPPED": RunStatus.PARTIAL,
        "FINANCIAL_FACT_CONFLICT": RunStatus.PARTIAL,
    }
