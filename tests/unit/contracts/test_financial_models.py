from datetime import UTC, date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from hengce.contracts.enums import (
    ConflictResolutionStatus,
    ConsolidationScope,
    DiscoveryMethod,
    MappingStatus,
    QualityStatus,
    ReportType,
    StatementType,
)
from hengce.contracts.financial import (
    FactConflict,
    FilingDescriptor,
    FinancialFact,
    FinancialFiling,
    TaxonomyPackageRef,
)

NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)
HASH = "a" * 64
SHANGHAI = ZoneInfo("Asia/Shanghai")


def descriptor() -> FilingDescriptor:
    return FilingDescriptor(
        source_id="sse",
        source_url="https://www.sse.com.cn/disclosure/test.xml",
        ts_code="699999.SH",
        exchange="SSE",
        report_period=date(2025, 12, 31),
        report_type=ReportType.ANNUAL,
        published_at=NOW,
        collected_at=NOW,
        attachment_name="instance.xml",
        content_type="application/xml",
        raw_object_hash=HASH,
        taxonomy_refs=("test-gaap-2025",),
        discovery_method=DiscoveryMethod.FIXTURE,
        instance_entrypoint=None,
    )


def financial_fact_payload() -> dict[str, object]:
    return {
        "record_id": "fact-1",
        "fact_id": "fact-1",
        "source_id": "sse",
        "source_url": "https://www.sse.com.cn/disclosure/test.xml",
        "published_at": NOW,
        "announcement_at": NOW,
        "effective_at": datetime(2025, 12, 31, 15, 59, 59, tzinfo=UTC),
        "collected_at": NOW,
        "version": "filing-v1:parser-v1:mapping-v1",
        "content_hash": HASH,
        "license_policy": "sse-personal-research",
        "quality_status": QualityStatus.VALID,
        "valid_from": NOW,
        "ts_code": "699999.SH",
        "report_period": date(2025, 12, 31),
        "report_type": ReportType.ANNUAL,
        "statement_type": StatementType.BALANCE_SHEET,
        "taxonomy": ("test-gaap-2025",),
        "fact_name": "Assets",
        "raw_qname": "{urn:hengce:test-gaap}Assets",
        "canonical_fact_name": "assets",
        "mapping_status": MappingStatus.MAPPED,
        "fact_value": Decimal("1000"),
        "unit": "iso4217:CNY",
        "currency": "CNY",
        "filing_id": "filing-1",
        "context_signature": "context-hash",
        "entity_scheme": "https://example.test/entity",
        "entity_identifier": "699999.SH",
        "period_start": None,
        "period_end": None,
        "instant": date(2025, 12, 31),
        "unit_signature": "unit-hash",
        "decimals": "-2",
        "consolidation_scope": ConsolidationScope.CONSOLIDATED,
        "dimensions": {},
        "fact_identity_hash": "b" * 64,
        "comparison_identity_hash": "c" * 64,
    }


def test_descriptor_requires_timezone_and_supported_exchange() -> None:
    assert descriptor().published_at.tzinfo is UTC
    with pytest.raises(ValidationError):
        FilingDescriptor.model_validate(
            {
                **descriptor().model_dump(),
                "published_at": datetime(2026, 7, 26, 12),
            }
        )
    with pytest.raises(ValidationError):
        FilingDescriptor.model_validate({**descriptor().model_dump(), "exchange": "BSE"})


def test_financial_fact_enforces_identity_and_period_shape() -> None:
    payload = financial_fact_payload()
    assert FinancialFact.model_validate(payload).record_id == "fact-1"
    with pytest.raises(ValidationError):
        FinancialFact.model_validate({**payload, "fact_id": "other"})
    with pytest.raises(ValidationError):
        FinancialFact.model_validate({**payload, "period_start": date(2025, 1, 1)})
    with pytest.raises(ValidationError):
        FinancialFact.model_validate(
            {key: value for key, value in payload.items() if key != "entity_scheme"}
        )


def test_unmapped_fact_cannot_claim_canonical_name() -> None:
    with pytest.raises(ValidationError):
        FinancialFact.model_validate(
            {
                **financial_fact_payload(),
                "mapping_status": MappingStatus.UNMAPPED,
                "canonical_fact_name": "assets",
            }
        )


def test_financial_fact_effective_time_is_report_period_end_in_shanghai() -> None:
    fact = FinancialFact.model_validate(financial_fact_payload())
    assert fact.effective_at.astimezone(SHANGHAI) == datetime.combine(
        fact.report_period,
        time(23, 59, 59),
        SHANGHAI,
    )
    with pytest.raises(ValidationError):
        FinancialFact.model_validate(
            {
                **financial_fact_payload(),
                "effective_at": datetime(2025, 12, 31, 15, 59, 58, tzinfo=UTC),
            }
        )


def test_financial_enum_values_are_frozen() -> None:
    assert [member.value for member in ReportType] == ["ANNUAL", "Q1", "HALF_YEAR", "Q3"]
    assert [member.value for member in StatementType] == [
        "BALANCE_SHEET",
        "INCOME_STATEMENT",
        "CASH_FLOW",
        "OTHER",
    ]
    assert [member.value for member in MappingStatus] == ["MAPPED", "UNMAPPED"]
    assert [member.value for member in ConsolidationScope] == ["CONSOLIDATED", "PARENT", "UNKNOWN"]
    assert [member.value for member in DiscoveryMethod] == ["FIXTURE", "MANUAL_IMPORT"]
    assert [member.value for member in ConflictResolutionStatus] == ["OPEN", "SUPERSEDED"]


def test_financial_filing_requires_matching_identity_and_publication_time() -> None:
    payload = {
        "record_id": "filing-1",
        "filing_id": "filing-1",
        "source_id": "sse",
        "source_url": "https://www.sse.com.cn/disclosure/test.xml",
        "published_at": NOW,
        "effective_at": NOW,
        "collected_at": NOW,
        "version": "filing-v1:parser-v1:mapping-v1",
        "content_hash": HASH,
        "license_policy": "sse-personal-research",
        "quality_status": QualityStatus.VALID,
        "valid_from": NOW,
        "ts_code": "699999.SH",
        "exchange": "SSE",
        "report_period": date(2025, 12, 31),
        "report_type": ReportType.ANNUAL,
        "announcement_at": NOW,
        "taxonomy": ("test-gaap-2025",),
        "taxonomy_hashes": (HASH,),
        "raw_object_hash": HASH,
        "filing_version": "filing-v1",
        "parser_name": "fixture-parser",
        "parser_version": "parser-v1",
        "mapping_version": "mapping-v1",
        "fact_count": 1,
        "conflict_count": 0,
        "is_restated": False,
        "supersedes_id": None,
    }
    filing = FinancialFiling.model_validate(payload)
    assert filing.announcement_at == filing.published_at
    with pytest.raises(ValidationError):
        FinancialFiling.model_validate({**payload, "record_id": "other"})
    with pytest.raises(ValidationError):
        FinancialFiling.model_validate(
            {**payload, "announcement_at": datetime(2026, 7, 26, 13, tzinfo=UTC)}
        )
    with pytest.raises(ValidationError):
        FinancialFiling.model_validate({**payload, "published_at": datetime(2026, 7, 26, 12)})
    with pytest.raises(ValidationError):
        FinancialFiling.model_validate({**payload, "content_hash": "b" * 64})


def test_taxonomy_and_conflict_require_aware_times_and_lowercase_hashes() -> None:
    taxonomy_payload = {
        "taxonomy_id": "test-gaap-2025",
        "source_id": "sse",
        "source_url": "https://www.sse.com.cn/taxonomy.zip",
        "raw_object_hash": HASH,
        "package_name": "test-gaap",
        "entrypoint": "entry.xsd",
        "content_type": "application/zip",
        "collected_at": NOW,
    }
    assert TaxonomyPackageRef.model_validate(taxonomy_payload).collected_at == NOW
    with pytest.raises(ValidationError):
        TaxonomyPackageRef.model_validate({**taxonomy_payload, "raw_object_hash": "A" * 64})
    with pytest.raises(ValidationError):
        TaxonomyPackageRef.model_validate(
            {**taxonomy_payload, "collected_at": datetime(2026, 7, 26, 12)}
        )

    conflict_payload = {
        "conflict_id": "conflict-1",
        "filing_id": "filing-1",
        "fact_identity_hash": "b" * 64,
        "competing_fact_ids": ("fact-1", "fact-2"),
        "conflict_type": "duplicate-context",
        "resolution_status": ConflictResolutionStatus.OPEN,
        "quality_status": QualityStatus.CONFLICT,
        "detected_at": NOW,
    }
    assert FactConflict.model_validate(conflict_payload).detected_at == NOW
    with pytest.raises(ValidationError):
        FactConflict.model_validate({**conflict_payload, "detected_at": datetime(2026, 7, 26, 12)})
