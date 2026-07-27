from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from hengce.contracts.enums import (
    ConsolidationScope,
    MappingStatus,
    QualityStatus,
    ReportType,
    StatementType,
)
from hengce.contracts.financial import FinancialFiling
from hengce.financials.mapping import (
    FactMapping,
    FactMappingRegistry,
    FinancialFactNormalizer,
)
from hengce.financials.xbrl import RawXbrlContext, RawXbrlFact, RawXbrlUnit

NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)
HASH = "a" * 64
ASSETS_QNAME = "{urn:hengce:test-gaap}Assets"
CNY_MEASURE = "{http://www.xbrl.org/2003/iso4217}CNY"
METER_MEASURE = "{urn:hengce:test-unit}meter"
PURE_MEASURE = "{http://www.xbrl.org/2003/instance}pure"
SHARES_MEASURE = "{http://www.xbrl.org/2003/instance}shares"


def financial_filing(filing_id: str = "filing-1") -> FinancialFiling:
    return FinancialFiling(
        record_id=filing_id,
        filing_id=filing_id,
        source_id="fixture-source",
        source_url="https://example.test/filing.xml",
        published_at=NOW,
        effective_at=NOW,
        collected_at=NOW,
        version="filing-v1:parser-v1:filing-mapping-v1",
        content_hash=HASH,
        license_policy="fixture-only",
        quality_status=QualityStatus.VALID,
        valid_from=NOW,
        ts_code="600001.SH",
        exchange="SSE",
        report_period=date(2025, 12, 31),
        report_type=ReportType.ANNUAL,
        announcement_at=NOW,
        taxonomy=("test-gaap-2025",),
        taxonomy_hashes=(HASH,),
        raw_object_hash=HASH,
        filing_version="filing-v1",
        parser_name="fixture-parser",
        parser_version="parser-v1",
        mapping_version="filing-mapping-v1",
        fact_count=1,
        conflict_count=0,
        is_restated=False,
        supersedes_id=None,
    )


def raw_assets(
    dimensions: tuple[tuple[str, str], ...] = (),
    unit: RawXbrlUnit | None = None,
    entity_scheme: str = "https://example.test/entity",
    value: Decimal = Decimal("1000.25"),
) -> RawXbrlFact:
    return RawXbrlFact(
        raw_qname=ASSETS_QNAME,
        fact_name="Assets",
        value=value,
        decimals="-2",
        context=RawXbrlContext(
            context_id="context-1",
            entity_scheme=entity_scheme,
            entity_identifier="600001.SH",
            period_start=None,
            period_end=None,
            instant=date(2025, 12, 31),
            dimensions=dimensions,
        ),
        unit=unit
        or RawXbrlUnit(
            unit_id="unit-1",
            numerator_measures=(CNY_MEASURE,),
            denominator_measures=(),
            currency="CNY",
        ),
    )


def normalizer(expected_unit_kind: str = "MONETARY") -> FinancialFactNormalizer:
    return FinancialFactNormalizer(
        FactMappingRegistry(
            mapping_version="fixture-v1",
            mappings={
                ASSETS_QNAME: FactMapping(
                    raw_qname=ASSETS_QNAME,
                    canonical_fact_name="assets",
                    statement_type=StatementType.BALANCE_SHEET,
                    expected_unit_kind=expected_unit_kind,
                )
            },
        )
    )


def test_normalizer_maps_fixture_qname_and_builds_stable_identities() -> None:
    left = normalizer().normalize(
        financial_filing(),
        [raw_assets(dimensions=(("b", "2"), ("a", "1")))],
    )
    right = normalizer().normalize(
        financial_filing(),
        [raw_assets(dimensions=(("a", "1"), ("b", "2")))],
    )

    assert left == right
    assert left[0].mapping_status is MappingStatus.MAPPED
    assert left[0].canonical_fact_name == "assets"
    assert left[0].record_id == left[0].fact_id
    assert left[0].fact_identity_hash == right[0].fact_identity_hash
    assert left[0].fact_value == Decimal("1000.25")
    assert left[0].dimensions == {"a": "1", "b": "2"}


def test_normalizer_gives_distinct_observation_ids_to_different_values() -> None:
    facts = normalizer().normalize(
        financial_filing(),
        [raw_assets(value=Decimal("1000")), raw_assets(value=Decimal("1100"))],
    )

    assert facts[0].fact_identity_hash == facts[1].fact_identity_hash
    assert facts[0].fact_id != facts[1].fact_id
    assert all(fact.record_id == fact.fact_id for fact in facts)


def test_unmapped_fact_is_preserved_but_unverified() -> None:
    facts = FinancialFactNormalizer(
        FactMappingRegistry(mapping_version="empty", mappings={})
    ).normalize(financial_filing(), [raw_assets()])

    assert facts[0].raw_qname == ASSETS_QNAME
    assert facts[0].canonical_fact_name is None
    assert facts[0].mapping_status is MappingStatus.UNMAPPED
    assert facts[0].quality_status is QualityStatus.UNVERIFIED
    assert facts[0].statement_type is StatementType.OTHER


def test_comparison_identity_ignores_filing_id_but_fact_identity_does_not() -> None:
    old = normalizer().normalize(financial_filing(filing_id="old"), [raw_assets()])[0]
    new = normalizer().normalize(financial_filing(filing_id="new"), [raw_assets()])[0]

    assert old.fact_identity_hash != new.fact_identity_hash
    assert old.comparison_identity_hash == new.comparison_identity_hash


def test_entity_scheme_participates_in_all_context_derived_identities() -> None:
    left = normalizer().normalize(
        financial_filing(),
        [raw_assets(entity_scheme="https://example.test/entity-a")],
    )[0]
    right = normalizer().normalize(
        financial_filing(),
        [raw_assets(entity_scheme="https://example.test/entity-b")],
    )[0]

    assert left.entity_scheme == "https://example.test/entity-a"
    assert left.context_signature != right.context_signature
    assert left.fact_identity_hash != right.fact_identity_hash
    assert left.comparison_identity_hash != right.comparison_identity_hash
    assert left.fact_id != right.fact_id


def test_normalizer_rejects_mapped_fact_with_wrong_unit_kind() -> None:
    shares = RawXbrlUnit(
        unit_id="unit-shares",
        numerator_measures=("{http://www.xbrl.org/2003/instance}shares",),
        denominator_measures=(),
        currency=None,
    )

    with pytest.raises(ValueError, match="^FINANCIAL_UNIT_KIND_MISMATCH$"):
        normalizer().normalize(financial_filing(), [raw_assets(unit=shares)])


@pytest.mark.parametrize(
    ("expected_unit_kind", "unit"),
    [
        (
            "SHARES",
            RawXbrlUnit(
                unit_id="shares-per-meter",
                numerator_measures=(SHARES_MEASURE,),
                denominator_measures=(METER_MEASURE,),
                currency=None,
            ),
        ),
        (
            "PURE",
            RawXbrlUnit(
                unit_id="pure-per-meter",
                numerator_measures=(PURE_MEASURE,),
                denominator_measures=(METER_MEASURE,),
                currency=None,
            ),
        ),
        (
            "MONETARY",
            RawXbrlUnit(
                unit_id="currency-times-meter",
                numerator_measures=(CNY_MEASURE, METER_MEASURE),
                denominator_measures=(),
                currency=None,
            ),
        ),
        (
            "PER_SHARE",
            RawXbrlUnit(
                unit_id="pure-per-share",
                numerator_measures=(PURE_MEASURE,),
                denominator_measures=(SHARES_MEASURE,),
                currency=None,
            ),
        ),
        (
            "PER_SHARE",
            RawXbrlUnit(
                unit_id="currency-per-shares-meter",
                numerator_measures=(CNY_MEASURE,),
                denominator_measures=(SHARES_MEASURE, METER_MEASURE),
                currency=None,
            ),
        ),
    ],
)
def test_normalizer_rejects_non_exact_expected_unit_shapes(
    expected_unit_kind: str,
    unit: RawXbrlUnit,
) -> None:
    with pytest.raises(ValueError, match="^FINANCIAL_UNIT_KIND_MISMATCH$"):
        normalizer(expected_unit_kind).normalize(financial_filing(), [raw_assets(unit=unit)])


def test_other_expected_unit_kind_cannot_admit_a_composite_unit() -> None:
    composite = RawXbrlUnit(
        unit_id="currency-times-meter",
        numerator_measures=(CNY_MEASURE, METER_MEASURE),
        denominator_measures=(),
        currency=None,
    )

    with pytest.raises(ValueError, match="^FINANCIAL_MAPPING_UNIT_KIND_INVALID$"):
        normalizer("OTHER").normalize(
            financial_filing(),
            [raw_assets(unit=composite)],
        )


@pytest.mark.parametrize("expected_unit_kind", ["NONE", "PERCENT", "", "monetary"])
def test_fact_mapping_rejects_other_unsupported_expected_unit_kinds(
    expected_unit_kind: str,
) -> None:
    with pytest.raises(ValueError, match="^FINANCIAL_MAPPING_UNIT_KIND_INVALID$"):
        FactMapping(
            raw_qname=ASSETS_QNAME,
            canonical_fact_name="assets",
            statement_type=StatementType.BALANCE_SHEET,
            expected_unit_kind=expected_unit_kind,
        )


def test_normalizer_preserves_filing_provenance_and_uses_registry_version() -> None:
    fact = normalizer().normalize(financial_filing(), [raw_assets()])[0]

    assert fact.source_id == "fixture-source"
    assert str(fact.source_url) == "https://example.test/filing.xml"
    assert fact.published_at == NOW
    assert fact.valid_from == NOW
    assert fact.content_hash == HASH
    assert fact.version == "filing-v1:parser-v1:fixture-v1"
    assert fact.supersedes_id is None
    assert fact.consolidation_scope is ConsolidationScope.UNKNOWN
