import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from hengce.contracts.enums import StatementType
from hengce.financials.registry_loader import (
    CANONICAL_PILOT_FACTS,
    EntityDeclaration,
    FinancialRegistryLoader,
)

TAXONOMY_HASH = "a" * 64


def mapping_payload() -> dict[str, object]:
    statement_by_name = {
        "revenue": StatementType.INCOME_STATEMENT.value,
        "operating_cost": StatementType.INCOME_STATEMENT.value,
        "net_profit": StatementType.INCOME_STATEMENT.value,
        "adjusted_net_profit": StatementType.INCOME_STATEMENT.value,
        "operating_cash_flow": StatementType.CASH_FLOW.value,
        "capital_expenditure": StatementType.CASH_FLOW.value,
        "total_assets": StatementType.BALANCE_SHEET.value,
        "current_assets": StatementType.BALANCE_SHEET.value,
        "cash_and_equivalents": StatementType.BALANCE_SHEET.value,
        "total_liabilities": StatementType.BALANCE_SHEET.value,
        "current_liabilities": StatementType.BALANCE_SHEET.value,
        "interest_bearing_debt": StatementType.BALANCE_SHEET.value,
        "equity": StatementType.BALANCE_SHEET.value,
        "interest_expense": StatementType.INCOME_STATEMENT.value,
        "total_shares": StatementType.BALANCE_SHEET.value,
    }
    return {
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
                "taxonomy_hash": TAXONOMY_HASH,
                "evidence_url": "https://www.sse.com.cn/fixture/pilot-gaap.xsd",
                "reviewed_at": "2026-07-30T09:00:00+08:00",
            }
            for name in sorted(CANONICAL_PILOT_FACTS)
        ],
    }


def write_mapping(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_loader_builds_exact_versioned_registry_for_report_year(
    tmp_path: Path,
) -> None:
    payload = mapping_payload()

    registry = FinancialRegistryLoader().load_fact_registry(
        write_mapping(tmp_path, payload),
        date(2025, 12, 31),
    )

    assert registry.mapping_version == "pilot-fixture-2025-v1"
    assert {
        mapping.canonical_fact_name for mapping in registry.mappings.values()
    } == CANONICAL_PILOT_FACTS
    total_shares = next(
        mapping
        for mapping in registry.mappings.values()
        if mapping.canonical_fact_name == "total_shares"
    )
    assert total_shares.expected_unit_kind == "SHARES"
    assert total_shares.taxonomy_hash == TAXONOMY_HASH
    assert str(total_shares.evidence_url).startswith("https://www.sse.com.cn/")


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        ("duplicate_qname", "FINANCIAL_MAPPING_QNAME_DUPLICATE"),
        ("unknown_unit", "FINANCIAL_MAPPING_UNIT_KIND_INVALID"),
        ("empty_version", "FINANCIAL_MAPPING_VERSION_INVALID"),
        ("wrong_year", "FINANCIAL_MAPPING_REPORT_YEAR_NOT_APPLICABLE"),
        ("missing_canonical", "FINANCIAL_MAPPING_CANONICAL_SET_INCOMPLETE"),
        ("missing_taxonomy_evidence", "FINANCIAL_MAPPING_EVIDENCE_INVALID"),
    ],
)
def test_loader_rejects_ambiguous_or_unaudited_mapping_file(
    tmp_path: Path,
    mutation: str,
    error_code: str,
) -> None:
    payload = mapping_payload()
    mappings = payload["mappings"]
    assert isinstance(mappings, list)
    if mutation == "duplicate_qname":
        mappings.append(dict(mappings[0]))
    elif mutation == "unknown_unit":
        mappings[0]["expected_unit_kind"] = "CNY"
    elif mutation == "empty_version":
        payload["mapping_version"] = ""
    elif mutation == "wrong_year":
        payload["report_year_from"] = 2026
        payload["report_year_to"] = 2026
    elif mutation == "missing_canonical":
        mappings.pop()
    elif mutation == "missing_taxonomy_evidence":
        mappings[0]["taxonomy_hash"] = ""

    with pytest.raises(ValueError, match=f"^{error_code}$"):
        FinancialRegistryLoader().load_fact_registry(
            write_mapping(tmp_path, payload),
            date(2025, 12, 31),
        )


def test_entity_registry_requires_one_exact_declared_owner_per_universe_member() -> None:
    universe = SimpleNamespace(
        members=(
            SimpleNamespace(ts_code="600001.SH"),
            SimpleNamespace(ts_code="000001.SZ"),
        )
    )
    declarations = [
        EntityDeclaration(
            entity_scheme="http://www.sse.com.cn/entity",
            entity_identifier="600001",
            ts_code="600001.SH",
        ),
        EntityDeclaration(
            entity_scheme="http://www.szse.cn/entity",
            entity_identifier="000001",
            ts_code="000001.SZ",
        ),
    ]

    registry = FinancialRegistryLoader().build_entity_registry(
        universe,
        declarations,
    )

    assert registry.owners_for(
        "http://www.sse.com.cn/entity",
        "600001",
    ) == ("600001.SH",)


@pytest.mark.parametrize(
    ("declarations", "error_code"),
    [
        (
            [
                EntityDeclaration("scheme", "same", "600001.SH"),
                EntityDeclaration("scheme", "same", "000001.SZ"),
            ],
            "FINANCIAL_ENTITY_DECLARATION_AMBIGUOUS",
        ),
        (
            [EntityDeclaration("scheme", "unknown", "688999.SH")],
            "FINANCIAL_ENTITY_DECLARATION_UNKNOWN_SECURITY",
        ),
        (
            [EntityDeclaration("scheme", "one", "600001.SH")],
            "FINANCIAL_ENTITY_DECLARATION_INCOMPLETE",
        ),
    ],
)
def test_entity_registry_blocks_ambiguous_unknown_or_incomplete_declarations(
    declarations: list[EntityDeclaration],
    error_code: str,
) -> None:
    universe = SimpleNamespace(
        members=(
            SimpleNamespace(ts_code="600001.SH"),
            SimpleNamespace(ts_code="000001.SZ"),
        )
    )

    with pytest.raises(ValueError, match=f"^{error_code}$"):
        FinancialRegistryLoader().build_entity_registry(universe, declarations)


def test_entity_declaration_rejects_blank_identity() -> None:
    with pytest.raises(ValueError, match="^FINANCIAL_ENTITY_DECLARATION_INVALID$"):
        EntityDeclaration("", "600001", "600001.SH")


def test_entity_declaration_file_is_exact_and_order_preserving(
    tmp_path: Path,
) -> None:
    path = tmp_path / "entities.json"
    path.write_text(
        json.dumps(
            [
                {
                    "entity_scheme": "scheme-b",
                    "entity_identifier": "000001",
                    "ts_code": "000001.SZ",
                },
                {
                    "entity_scheme": "scheme-a",
                    "entity_identifier": "600001",
                    "ts_code": "600001.SH",
                },
            ]
        ),
        encoding="utf-8",
    )

    declarations = FinancialRegistryLoader().load_entity_declarations(path)

    assert [item.ts_code for item in declarations] == ["000001.SZ", "600001.SH"]


def test_entity_declaration_file_rejects_extra_unreviewed_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / "entities.json"
    path.write_text(
        json.dumps(
            [
                {
                    "entity_scheme": "scheme",
                    "entity_identifier": "600001",
                    "ts_code": "600001.SH",
                    "company_name": "must-not-drive-identity",
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="^FINANCIAL_ENTITY_DECLARATION_FILE_INVALID$",
    ):
        FinancialRegistryLoader().load_entity_declarations(path)
