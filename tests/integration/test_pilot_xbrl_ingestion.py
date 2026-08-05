import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from hengce.bootstrap import bootstrap_state
from hengce.config import Settings
from hengce.contracts.enums import (
    AcquisitionStatus,
    DiscoveryMethod,
    DocumentKind,
    QualityStatus,
    ReportType,
    RunStatus,
)
from hengce.contracts.financial import FilingDescriptor, TaxonomyPackageRef
from hengce.contracts.pilot import (
    AcquisitionManifestItem,
    PilotUniverseMember,
    PilotUniverseSnapshot,
)
from hengce.financials.mapping import FinancialFactNormalizer
from hengce.financials.package import SafePackageMaterializer
from hengce.financials.quality import FinancialQualityValidator
from hengce.financials.registry_loader import (
    CANONICAL_PILOT_FACTS,
    EntityDeclaration,
    FinancialRegistryLoader,
)
from hengce.financials.xbrl import ArelleXbrlProcessor
from hengce.policy.guard import PolicyGuard
from hengce.raw_store.store import RawObjectStore
from hengce.services.financial_ingestion import FinancialIngestionService
from hengce.state.financial_repository import FinancialFilingRepository
from hengce.state.pilot_repository import PilotRepository
from hengce.warehouse.financial import FinancialFactWarehouse

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "xbrl" / "pilot"
PUBLISHED = datetime(2026, 3, 30, 10, tzinfo=UTC)
COLLECTED = datetime(2026, 7, 30, 9, tzinfo=UTC)
CUTOFF = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)


def universe() -> PilotUniverseSnapshot:
    config = (
        ("MAIN_SH", "600", "SH", 8),
        ("MAIN_SZ", "000", "SZ", 8),
        ("CHINEXT", "300", "SZ", 7),
        ("STAR", "688", "SH", 7),
    )
    members: list[PilotUniverseMember] = []
    sequence = 0
    for board, prefix, suffix, count in config:
        for rank in range(1, count + 1):
            sequence += 1
            code = (
                "699998.SH"
                if board == "MAIN_SH" and rank == 1
                else f"{prefix}{rank:03d}.{suffix}"
            )
            members.append(
                PilotUniverseMember(
                    ts_code=code,
                    security_name=f"虚构公司{sequence:02d}",
                    board=board,
                    amount=Decimal(1_000_000 - sequence),
                    rank_in_board=rank,
                    evidence_record_ids=(f"bar-{sequence}", f"master-{sequence}"),
                )
            )
    return PilotUniverseSnapshot(
        universe_id="pilot-xbrl-fixture",
        market_date=date(2026, 7, 22),
        report_cutoff_at=CUTOFF,
        algorithm_version="fixture-v1",
        quotas={"MAIN_SH": 8, "MAIN_SZ": 8, "CHINEXT": 7, "STAR": 7},
        members=tuple(members),
        input_hashes={"market": "a" * 64, "master": "b" * 64},
        manifest_hash="c" * 64,
        created_at=COLLECTED,
    )


def mapping_file(tmp_path: Path, taxonomy_hash: str) -> Path:
    statement_by_name = {
        name: (
            "CASH_FLOW"
            if name in {"operating_cash_flow", "capital_expenditure"}
            else "INCOME_STATEMENT"
            if name
            in {
                "revenue",
                "operating_cost",
                "net_profit",
                "adjusted_net_profit",
                "interest_expense",
            }
            else "BALANCE_SHEET"
        )
        for name in CANONICAL_PILOT_FACTS
    }
    payload = {
        "mapping_version": "pilot-fixture-2025-v1",
        "report_year_from": 2025,
        "report_year_to": 2025,
        "canonical_fact_set": sorted(CANONICAL_PILOT_FACTS),
        "mappings": [
            {
                "raw_qname": f"{{urn:hengce:pilot-gaap}}{name.title().replace('_', '')}",
                "canonical_fact_name": name,
                "statement_type": statement_by_name[name],
                "expected_unit_kind": (
                    "SHARES" if name == "total_shares" else "MONETARY"
                ),
                "taxonomy_hash": taxonomy_hash,
                "evidence_url": "https://www.sse.com.cn/fixture/pilot-gaap.xsd",
                "reviewed_at": "2026-07-30T09:00:00+08:00",
            }
            for name in sorted(CANONICAL_PILOT_FACTS)
        ],
    }
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def build_system(
    tmp_path: Path,
) -> tuple[
    FinancialIngestionService,
    FilingDescriptor,
    PilotRepository,
    FinancialFilingRepository,
]:
    data_dir = tmp_path / "data"
    state = bootstrap_state(Settings(data_dir=data_dir))
    pilot_repository = PilotRepository(state.path)
    snapshot = universe()
    pilot_repository.publish_universe(snapshot)
    raw_store = RawObjectStore(data_dir / "raw")
    taxonomy_raw = raw_store.put(
        source_id="sse",
        source_url="https://www.sse.com.cn/fixture/pilot-gaap.xsd",
        collected_at=COLLECTED,
        content_type="application/xml-schema",
        payload=(FIXTURE_ROOT / "pilot-gaap.xsd").read_bytes(),
    )
    instance_raw = raw_store.put(
        source_id="sse",
        source_url="https://www.sse.com.cn/fixture/instance.xml",
        collected_at=COLLECTED,
        content_type="application/xbrl+xml",
        payload=(FIXTURE_ROOT / "instance.xml").read_bytes(),
    )
    item = AcquisitionManifestItem(
        item_id="pilot-filing-item",
        universe_id=snapshot.universe_id,
        ts_code="699998.SH",
        document_kind=DocumentKind.PERIODIC_REPORT,
        report_type=ReportType.ANNUAL,
        report_period=date(2025, 12, 31),
        source_id="sse",
        report_cutoff_at=CUTOFF,
        status=AcquisitionStatus.VERIFIED,
        source_url="https://www.sse.com.cn/fixture/instance.xml",
        discovery_method=DiscoveryMethod.FIXTURE,
        published_at=PUBLISHED,
        effective_at=PUBLISHED,
        collected_at=COLLECTED,
        content_hash=instance_raw.content_hash,
        version="fixture-filing-v1",
        supersedes_id=None,
        raw_object_hash=instance_raw.content_hash,
        quality_status=QualityStatus.VALID,
        error_code=None,
        attempt_count=1,
    )
    pilot_repository.insert_manifest([item])
    filing_repository = FinancialFilingRepository(state.path)
    filing_repository.register_taxonomy(
        TaxonomyPackageRef(
            taxonomy_id="pilot-gaap-2025",
            source_id="sse",
            source_url="https://www.sse.com.cn/fixture/pilot-gaap.xsd",
            raw_object_hash=taxonomy_raw.content_hash,
            package_name="pilot-gaap.xsd",
            entrypoint="pilot-gaap.xsd",
            content_type="application/xml-schema",
            collected_at=COLLECTED,
        )
    )
    loader = FinancialRegistryLoader()
    registry = loader.load_fact_registry(
        mapping_file(tmp_path, taxonomy_raw.content_hash),
        date(2025, 12, 31),
    )
    declarations = [
        EntityDeclaration(
            entity_scheme=(
                "urn:hengce:pilot-fixture"
                if member.ts_code == "699998.SH"
                else f"urn:hengce:unused:{member.ts_code}"
            ),
            entity_identifier=member.ts_code,
            ts_code=member.ts_code,
        )
        for member in snapshot.members
    ]
    service = FinancialIngestionService(
        guard=PolicyGuard(state),
        raw_store=raw_store,
        repository=filing_repository,
        materializer=SafePackageMaterializer(raw_store),
        processor=ArelleXbrlProcessor(),
        normalizer=FinancialFactNormalizer(
            registry,
            loader.build_entity_registry(snapshot, declarations),
        ),
        validator=FinancialQualityValidator(),
        warehouse=FinancialFactWarehouse(data_dir / "warehouse"),
        state=state,
        pilot_repository=pilot_repository,
        clock=lambda: COLLECTED,
    )
    descriptor = FilingDescriptor(
        source_id="sse",
        source_url="https://www.sse.com.cn/fixture/instance.xml",
        ts_code="699998.SH",
        exchange="SSE",
        report_period=date(2025, 12, 31),
        report_type=ReportType.ANNUAL,
        published_at=PUBLISHED,
        collected_at=COLLECTED,
        attachment_name="instance.xml",
        content_type="application/xbrl+xml",
        raw_object_hash=instance_raw.content_hash,
        taxonomy_refs=("pilot-gaap-2025",),
        discovery_method=DiscoveryMethod.FIXTURE,
        instance_entrypoint=None,
    )
    return service, descriptor, pilot_repository, filing_repository


def test_verified_manifest_transitions_only_after_immutable_filing_publish(
    tmp_path: Path,
) -> None:
    service, descriptor, pilot_repository, filing_repository = build_system(tmp_path)

    first = service.run(descriptor, manifest_item_id="pilot-filing-item")
    second = service.run(descriptor, manifest_item_id="pilot-filing-item")

    item = pilot_repository.get_manifest_item("pilot-filing-item")
    filing = filing_repository.get_filing(first.filing_id)
    assert first == second
    assert first.run_status is RunStatus.SUCCEEDED
    assert first.fact_count == 15
    assert item is not None and item.status is AcquisitionStatus.INGESTED
    assert filing is not None and filing.artifact_status == "PUBLISHED"
    assert Path(filing.expected_path).is_file()


def test_manifest_descriptor_content_mismatch_does_not_change_verified_item(
    tmp_path: Path,
) -> None:
    service, descriptor, pilot_repository, _filing_repository = build_system(tmp_path)
    mismatched = descriptor.model_copy(update={"raw_object_hash": "d" * 64})

    with pytest.raises(
        ValueError,
        match="^ACQUISITION_DESCRIPTOR_MISMATCH$",
    ):
        service.run(mismatched, manifest_item_id="pilot-filing-item")

    item = pilot_repository.get_manifest_item("pilot-filing-item")
    assert item is not None and item.status is AcquisitionStatus.VERIFIED
