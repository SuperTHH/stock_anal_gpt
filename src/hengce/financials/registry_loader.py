from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, field_validator

from hengce.contracts.enums import StatementType
from hengce.contracts.pilot import PilotUniverseSnapshot
from hengce.financials.mapping import (
    ALLOWED_UNIT_KINDS,
    EntityMappingRegistry,
    FactMapping,
    FactMappingRegistry,
)

CANONICAL_PILOT_FACTS = frozenset(
    {
        "revenue",
        "operating_cost",
        "net_profit",
        "adjusted_net_profit",
        "operating_cash_flow",
        "capital_expenditure",
        "total_assets",
        "current_assets",
        "cash_and_equivalents",
        "total_liabilities",
        "current_liabilities",
        "interest_bearing_debt",
        "equity",
        "interest_expense",
        "total_shares",
    }
)
_QNAME_PATTERN = re.compile(r"^\{[^{}]+\}[^{}]+$")
_TS_CODE_PATTERN = re.compile(r"^[0-9]{6}\.(?:SH|SZ)$")


class _MappingEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_qname: str
    canonical_fact_name: str
    statement_type: StatementType
    expected_unit_kind: str
    taxonomy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_url: HttpUrl
    reviewed_at: datetime

    @field_validator("reviewed_at")
    @classmethod
    def reviewed_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("reviewed_at must include an offset")
        return value


class _MappingDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mapping_version: str
    report_year_from: int = Field(ge=2000, le=2100)
    report_year_to: int = Field(ge=2000, le=2100)
    canonical_fact_set: list[str]
    mappings: list[_MappingEntry]


@dataclass(frozen=True, slots=True)
class EntityDeclaration:
    entity_scheme: str
    entity_identifier: str
    ts_code: str

    def __post_init__(self) -> None:
        if (
            not self.entity_scheme.strip()
            or not self.entity_identifier.strip()
            or not _TS_CODE_PATTERN.fullmatch(self.ts_code)
        ):
            raise ValueError("FINANCIAL_ENTITY_DECLARATION_INVALID")


class FinancialRegistryLoader:
    def load_fact_registry(
        self,
        path: Path,
        report_period: date,
    ) -> FactMappingRegistry:
        try:
            raw_document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("FINANCIAL_MAPPING_FILE_INVALID") from error
        if not isinstance(raw_document, dict):
            raise ValueError("FINANCIAL_MAPPING_FILE_INVALID")
        if not str(raw_document.get("mapping_version", "")).strip():
            raise ValueError("FINANCIAL_MAPPING_VERSION_INVALID")

        raw_mappings = raw_document.get("mappings")
        if not isinstance(raw_mappings, list):
            raise ValueError("FINANCIAL_MAPPING_FILE_INVALID")
        raw_qnames = [
            item.get("raw_qname")
            for item in raw_mappings
            if isinstance(item, dict)
        ]
        if len(raw_qnames) != len(set(raw_qnames)):
            raise ValueError("FINANCIAL_MAPPING_QNAME_DUPLICATE")
        if any(
            not isinstance(item, dict)
            or item.get("expected_unit_kind") not in ALLOWED_UNIT_KINDS
            for item in raw_mappings
        ):
            raise ValueError("FINANCIAL_MAPPING_UNIT_KIND_INVALID")
        try:
            document = _MappingDocument.model_validate(raw_document)
        except ValidationError as error:
            raise ValueError("FINANCIAL_MAPPING_EVIDENCE_INVALID") from error
        if (
            document.report_year_from > document.report_year_to
            or not (
                document.report_year_from
                <= report_period.year
                <= document.report_year_to
            )
        ):
            raise ValueError("FINANCIAL_MAPPING_REPORT_YEAR_NOT_APPLICABLE")
        if set(document.canonical_fact_set) != CANONICAL_PILOT_FACTS or {
            entry.canonical_fact_name for entry in document.mappings
        } != CANONICAL_PILOT_FACTS:
            raise ValueError("FINANCIAL_MAPPING_CANONICAL_SET_INCOMPLETE")
        if any(
            not _QNAME_PATTERN.fullmatch(entry.raw_qname)
            or entry.canonical_fact_name not in CANONICAL_PILOT_FACTS
            for entry in document.mappings
        ):
            raise ValueError("FINANCIAL_MAPPING_QNAME_INVALID")

        mappings = {
            entry.raw_qname: FactMapping(
                raw_qname=entry.raw_qname,
                canonical_fact_name=entry.canonical_fact_name,
                statement_type=entry.statement_type,
                expected_unit_kind=entry.expected_unit_kind,
                taxonomy_hash=entry.taxonomy_hash,
                evidence_url=str(entry.evidence_url),
                reviewed_at=entry.reviewed_at,
            )
            for entry in document.mappings
        }
        return FactMappingRegistry(
            mapping_version=document.mapping_version,
            mappings=mappings,
        )

    def load_entity_declarations(
        self,
        path: Path,
    ) -> tuple[EntityDeclaration, ...]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("FINANCIAL_ENTITY_DECLARATION_FILE_INVALID") from error
        if not isinstance(payload, list):
            raise ValueError("FINANCIAL_ENTITY_DECLARATION_FILE_INVALID")
        declarations: list[EntityDeclaration] = []
        try:
            for item in payload:
                if not isinstance(item, dict) or set(item) != {
                    "entity_scheme",
                    "entity_identifier",
                    "ts_code",
                }:
                    raise ValueError("FINANCIAL_ENTITY_DECLARATION_FILE_INVALID")
                declarations.append(
                    EntityDeclaration(
                        entity_scheme=item["entity_scheme"],
                        entity_identifier=item["entity_identifier"],
                        ts_code=item["ts_code"],
                    )
                )
        except (TypeError, ValueError) as error:
            if str(error) == "FINANCIAL_ENTITY_DECLARATION_FILE_INVALID":
                raise
            raise ValueError("FINANCIAL_ENTITY_DECLARATION_FILE_INVALID") from error
        return tuple(declarations)

    def build_entity_registry(
        self,
        universe: PilotUniverseSnapshot,
        declarations: Sequence[EntityDeclaration],
    ) -> EntityMappingRegistry:
        universe_codes = {member.ts_code for member in universe.members}
        if any(declaration.ts_code not in universe_codes for declaration in declarations):
            raise ValueError("FINANCIAL_ENTITY_DECLARATION_UNKNOWN_SECURITY")

        mappings: dict[tuple[str, str], str] = {}
        for declaration in declarations:
            key = (
                declaration.entity_scheme,
                declaration.entity_identifier,
            )
            existing = mappings.get(key)
            if existing is not None and existing != declaration.ts_code:
                raise ValueError("FINANCIAL_ENTITY_DECLARATION_AMBIGUOUS")
            mappings[key] = declaration.ts_code
        if {owner for owner in mappings.values()} != universe_codes:
            raise ValueError("FINANCIAL_ENTITY_DECLARATION_INCOMPLETE")
        return EntityMappingRegistry(mappings=mappings)


__all__ = [
    "CANONICAL_PILOT_FACTS",
    "EntityDeclaration",
    "FinancialRegistryLoader",
]
