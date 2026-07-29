from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256

import pytest

from hengce.contracts.enums import (
    ConsolidationScope,
    MappingStatus,
    QualityStatus,
    ReportType,
    StatementType,
)
from hengce.contracts.financial import FinancialFact, FinancialFiling
from hengce.financials.mapping import (
    EntityMappingRegistry,
    FactMapping,
    FactMappingRegistry,
    FinancialFactNormalizer,
)
from hengce.financials.quality import FinancialQualityValidator
from hengce.financials.xbrl import RawXbrlContext, RawXbrlFact, RawXbrlUnit

NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)
HASH = "a" * 64
CNY_MEASURE = "{http://www.xbrl.org/2003/iso4217}CNY"


def financial_filing() -> FinancialFiling:
    return FinancialFiling(
        record_id="filing-1",
        filing_id="filing-1",
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
        ts_code="699999.SH",
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


def mapped_fact(
    canonical_fact_name: str,
    value: Decimal,
    *,
    decimals: str | None = "0",
    fact_id: str | None = None,
) -> FinancialFact:
    raw_qname = "{urn:hengce:test-gaap}Assets"
    fact = FinancialFactNormalizer(
        FactMappingRegistry(
            mapping_version="fixture-v1",
            mappings={
                raw_qname: FactMapping(
                    raw_qname=raw_qname,
                    canonical_fact_name="assets",
                    statement_type=StatementType.BALANCE_SHEET,
                    expected_unit_kind="MONETARY",
                )
            },
        ),
        EntityMappingRegistry(
            mappings={
                ("https://example.test/entity", "699999.SH"): "699999.SH",
            }
        ),
    ).normalize(
        financial_filing(),
        [
            RawXbrlFact(
                raw_qname=raw_qname,
                fact_name="Assets",
                value=value,
                decimals=decimals,
                context=RawXbrlContext(
                    context_id="context-1",
                    entity_scheme="https://example.test/entity",
                    entity_identifier="699999.SH",
                    period_start=None,
                    period_end=None,
                    instant=date(2025, 12, 31),
                    dimensions=(),
                ),
                unit=RawXbrlUnit(
                    unit_id="unit-1",
                    numerator_measures=(CNY_MEASURE,),
                    denominator_measures=(),
                    currency="CNY",
                ),
            )
        ],
    )[0]
    resolved_id = fact_id or canonical_fact_name
    identity = sha256(f"identity:{resolved_id}".encode()).hexdigest()
    comparison = sha256(f"comparison:{resolved_id}".encode()).hexdigest()
    return fact.model_copy(
        update={
            "record_id": resolved_id,
            "fact_id": resolved_id,
            "canonical_fact_name": canonical_fact_name,
            "fact_identity_hash": identity,
            "comparison_identity_hash": comparison,
        }
    )


def normalized_assets(*values: Decimal) -> list[FinancialFact]:
    raw_qname = "{urn:hengce:test-gaap}Assets"
    normalizer = FinancialFactNormalizer(
        FactMappingRegistry(
            mapping_version="fixture-v1",
            mappings={
                raw_qname: FactMapping(
                    raw_qname=raw_qname,
                    canonical_fact_name="assets",
                    statement_type=StatementType.BALANCE_SHEET,
                    expected_unit_kind="MONETARY",
                )
            },
        ),
        EntityMappingRegistry(
            mappings={
                ("https://example.test/entity", "699999.SH"): "699999.SH",
            }
        ),
    )
    return normalizer.normalize(
        financial_filing(),
        [
            RawXbrlFact(
                raw_qname=raw_qname,
                fact_name="Assets",
                value=value,
                decimals="0",
                context=RawXbrlContext(
                    context_id="context-1",
                    entity_scheme="https://example.test/entity",
                    entity_identifier="699999.SH",
                    period_start=None,
                    period_end=None,
                    instant=date(2025, 12, 31),
                    dimensions=(),
                ),
                unit=RawXbrlUnit(
                    unit_id="unit-1",
                    numerator_measures=(CNY_MEASURE,),
                    denominator_measures=(),
                    currency="CNY",
                ),
            )
            for value in values
        ],
    )


def normalized_asset_aliases(
    left_value: Decimal,
    right_value: Decimal,
    *,
    right_instant: date = date(2025, 12, 31),
    right_dimensions: tuple[tuple[str, str], ...] = (),
    right_unit: RawXbrlUnit | None = None,
) -> list[FinancialFact]:
    left_qname = "{urn:hengce:test-gaap}Assets"
    right_qname = "{urn:hengce:test-gaap-extension}TotalAssets"
    normalizer = FinancialFactNormalizer(
        FactMappingRegistry(
            mapping_version="fixture-v1",
            mappings={
                raw_qname: FactMapping(
                    raw_qname=raw_qname,
                    canonical_fact_name="assets",
                    statement_type=StatementType.BALANCE_SHEET,
                    expected_unit_kind="MONETARY",
                )
                for raw_qname in (left_qname, right_qname)
            },
        ),
        EntityMappingRegistry(
            mappings={
                ("https://example.test/entity", "699999.SH"): "699999.SH",
            }
        ),
    )
    cny_unit = RawXbrlUnit(
        unit_id="unit-cny",
        numerator_measures=(CNY_MEASURE,),
        denominator_measures=(),
        currency="CNY",
    )
    return normalizer.normalize(
        financial_filing(),
        [
            RawXbrlFact(
                raw_qname=left_qname,
                fact_name="Assets",
                value=left_value,
                decimals="0",
                context=RawXbrlContext(
                    context_id="context-left",
                    entity_scheme="https://example.test/entity",
                    entity_identifier="699999.SH",
                    period_start=None,
                    period_end=None,
                    instant=date(2025, 12, 31),
                    dimensions=(),
                ),
                unit=cny_unit,
            ),
            RawXbrlFact(
                raw_qname=right_qname,
                fact_name="TotalAssets",
                value=right_value,
                decimals="0",
                context=RawXbrlContext(
                    context_id="context-right",
                    entity_scheme="https://example.test/entity",
                    entity_identifier="699999.SH",
                    period_start=None,
                    period_end=None,
                    instant=right_instant,
                    dimensions=right_dimensions,
                ),
                unit=right_unit or cny_unit,
            ),
        ],
    )


def test_identical_duplicate_is_collapsed_without_conflict() -> None:
    fact = mapped_fact("assets", Decimal("1000"))

    result = FinancialQualityValidator().validate(financial_filing(), [fact, fact])

    assert result.facts == (fact,)
    assert result.conflicts == ()
    assert "FINANCIAL_FACT_DUPLICATE" in {issue.code for issue in result.issues}


def test_different_values_for_one_identity_create_open_conflict() -> None:
    left, right = normalized_assets(Decimal("1000"), Decimal("1100"))

    result = FinancialQualityValidator().validate(financial_filing(), [right, left])

    assert result.filing_quality_status is QualityStatus.CONFLICT
    assert result.conflicts[0].competing_fact_ids == tuple(sorted((left.fact_id, right.fact_id)))
    assert left.fact_id != right.fact_id
    assert [fact.fact_id for fact in result.facts] == sorted((left.fact_id, right.fact_id))
    assert all(fact.quality_status is QualityStatus.CONFLICT for fact in result.facts)
    assert "FINANCIAL_FACT_CONFLICT" in {issue.code for issue in result.issues}


def test_different_raw_qnames_for_one_canonical_fact_create_one_open_conflict() -> None:
    left, right = normalized_asset_aliases(Decimal("1000"), Decimal("1100"))
    facts = [
        right,
        mapped_fact("liabilities", Decimal("400"), fact_id="liabilities"),
        left,
        mapped_fact("equity", Decimal("600"), fact_id="equity"),
    ]

    result = FinancialQualityValidator().validate(financial_filing(), facts)

    alias_ids = tuple(sorted((left.fact_id, right.fact_id)))
    assert left.raw_qname != right.raw_qname
    assert left.fact_identity_hash != right.fact_identity_hash
    assert result.filing_quality_status is QualityStatus.CONFLICT
    assert len(result.conflicts) == 1
    assert result.conflicts[0].competing_fact_ids == alias_ids
    assert result.conflicts[0].conflict_type == "CANONICAL_VALUE_MISMATCH"
    assert [
        issue.fact_ids for issue in result.issues if issue.code == "FINANCIAL_FACT_CONFLICT"
    ] == [alias_ids]
    assert all(
        fact.quality_status is QualityStatus.CONFLICT
        for fact in result.facts
        if fact.fact_id in alias_ids
    )


def test_same_value_canonical_aliases_fold_deterministically_with_alias_issue() -> None:
    left, right = normalized_asset_aliases(Decimal("1000"), Decimal("1000"))

    result = FinancialQualityValidator().validate(financial_filing(), [right, left])

    expected = min((left, right), key=lambda fact: fact.fact_id)
    assert result.facts == (expected,)
    assert result.conflicts == ()
    assert [
        issue.fact_ids for issue in result.issues if issue.code == "FINANCIAL_FACT_ALIAS_DUPLICATE"
    ] == [tuple(sorted((left.fact_id, right.fact_id)))]


@pytest.mark.parametrize("separation", ["unit", "instant", "dimensions"])
def test_canonical_aliases_with_different_units_or_contexts_remain_independent(
    separation: str,
) -> None:
    usd_unit = RawXbrlUnit(
        unit_id="unit-usd",
        numerator_measures=("{http://www.xbrl.org/2003/iso4217}USD",),
        denominator_measures=(),
        currency="USD",
    )
    aliases = normalized_asset_aliases(
        Decimal("1000"),
        Decimal("1100"),
        right_unit=usd_unit if separation == "unit" else None,
        right_instant=(date(2024, 12, 31) if separation == "instant" else date(2025, 12, 31)),
        right_dimensions=(("segment", "domestic"),) if separation == "dimensions" else (),
    )

    result = FinancialQualityValidator().validate(financial_filing(), aliases)

    assert len(result.facts) == 2
    assert result.conflicts == ()
    assert "FINANCIAL_FACT_CONFLICT" not in {issue.code for issue in result.issues}


def test_balance_sheet_uses_decimals_derived_tolerance() -> None:
    facts = [
        mapped_fact("assets", Decimal("1000"), decimals="-1"),
        mapped_fact("liabilities", Decimal("399"), decimals="-1"),
        mapped_fact("equity", Decimal("600"), decimals="-1"),
    ]

    result = FinancialQualityValidator().validate(financial_filing(), facts)

    assert "FINANCIAL_BALANCE_EQUATION_CONFLICT" not in {issue.code for issue in result.issues}


def test_excess_balance_difference_creates_equation_conflict() -> None:
    facts = [
        mapped_fact("assets", Decimal("1000")),
        mapped_fact("liabilities", Decimal("400")),
        mapped_fact("equity", Decimal("590")),
    ]

    result = FinancialQualityValidator().validate(financial_filing(), facts)

    assert result.filing_quality_status is QualityStatus.CONFLICT
    assert [issue.code for issue in result.issues] == ["FINANCIAL_BALANCE_EQUATION_CONFLICT"]
    assert all(fact.quality_status is QualityStatus.CONFLICT for fact in result.facts)


def test_missing_balance_component_is_partial_not_fabricated() -> None:
    facts = [
        mapped_fact("assets", Decimal("1000")),
        mapped_fact("liabilities", Decimal("400")),
    ]

    result = FinancialQualityValidator().validate(financial_filing(), facts)

    assert result.filing_quality_status is QualityStatus.PARTIAL
    assert [fact.canonical_fact_name for fact in result.facts] == [
        "assets",
        "liabilities",
    ]
    assert "FINANCIAL_BALANCE_COMPONENT_MISSING" in {issue.code for issue in result.issues}


def test_empty_facts_are_partial_with_numeric_facts_missing_issue() -> None:
    result = FinancialQualityValidator().validate(financial_filing(), [])

    assert result.facts == ()
    assert result.filing_quality_status is QualityStatus.PARTIAL
    assert [issue.code for issue in result.issues] == ["FINANCIAL_NUMERIC_FACTS_MISSING"]


def test_single_assets_component_is_partial_without_fabricating_facts() -> None:
    fact = mapped_fact("assets", Decimal("1000"))

    result = FinancialQualityValidator().validate(financial_filing(), [fact])

    assert result.facts == (fact,)
    assert result.filing_quality_status is QualityStatus.PARTIAL
    assert "FINANCIAL_BALANCE_COMPONENT_MISSING" in {issue.code for issue in result.issues}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("currency", "USD"),
        ("unit_signature", "different-unit"),
        ("instant", date(2024, 12, 31)),
        ("dimensions", {"segment": "domestic"}),
        ("consolidation_scope", ConsolidationScope.CONSOLIDATED),
    ],
)
def test_balance_equation_never_combines_different_group_keys(field: str, value: object) -> None:
    assets = mapped_fact("assets", Decimal("999"), fact_id="assets")
    assets = assets.model_copy(update={field: value})
    facts = [
        assets,
        mapped_fact("liabilities", Decimal("400"), fact_id="liabilities"),
        mapped_fact("equity", Decimal("600"), fact_id="equity"),
    ]

    result = FinancialQualityValidator().validate(financial_filing(), facts)

    assert result.filing_quality_status is QualityStatus.PARTIAL
    assert "FINANCIAL_BALANCE_EQUATION_CONFLICT" not in {issue.code for issue in result.issues}
    assert "FINANCIAL_BALANCE_COMPONENT_MISSING" in {issue.code for issue in result.issues}


def test_unmapped_fact_remains_unverified_and_makes_filing_partial() -> None:
    fact = mapped_fact("assets", Decimal("1000")).model_copy(
        update={
            "canonical_fact_name": None,
            "mapping_status": MappingStatus.UNMAPPED,
            "quality_status": QualityStatus.UNVERIFIED,
        }
    )

    result = FinancialQualityValidator().validate(financial_filing(), [fact])

    assert result.facts == (fact,)
    assert result.filing_quality_status is QualityStatus.PARTIAL
    assert [issue.code for issue in result.issues] == ["FINANCIAL_FACT_UNMAPPED"]
