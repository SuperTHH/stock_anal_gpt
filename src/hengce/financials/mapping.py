from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from types import MappingProxyType
from typing import Literal
from zoneinfo import ZoneInfo

from hengce.contracts.enums import (
    ConsolidationScope,
    MappingStatus,
    QualityStatus,
    StatementType,
)
from hengce.contracts.financial import FinancialFact, FinancialFiling

from .xbrl import RawXbrlContext, RawXbrlFact, RawXbrlUnit

SHANGHAI = ZoneInfo("Asia/Shanghai")
ISO_4217_NAMESPACE = "http://www.xbrl.org/2003/iso4217"
XBRLI_NAMESPACE = "http://www.xbrl.org/2003/instance"
SHARES_MEASURE = f"{{{XBRLI_NAMESPACE}}}shares"
PURE_MEASURE = f"{{{XBRLI_NAMESPACE}}}pure"
ExpectedUnitKind = Literal["MONETARY", "SHARES", "PURE", "PER_SHARE"]
ALLOWED_UNIT_KINDS = frozenset({"MONETARY", "SHARES", "PURE", "PER_SHARE"})
EntityKey = tuple[str, str]
EntityOwners = str | tuple[str, ...]


@dataclass(frozen=True)
class EntityMappingRegistry:
    mappings: Mapping[EntityKey, EntityOwners]

    def __post_init__(self) -> None:
        normalized = {
            key: tuple(sorted({owners} if isinstance(owners, str) else set(owners)))
            for key, owners in self.mappings.items()
        }
        object.__setattr__(self, "mappings", MappingProxyType(normalized))

    def owners_for(self, entity_scheme: str, entity_identifier: str) -> tuple[str, ...]:
        owners = self.mappings.get((entity_scheme, entity_identifier))
        return owners if isinstance(owners, tuple) else ()


@dataclass(frozen=True)
class FactMapping:
    raw_qname: str
    canonical_fact_name: str
    statement_type: StatementType
    expected_unit_kind: ExpectedUnitKind

    def __post_init__(self) -> None:
        if self.expected_unit_kind not in ALLOWED_UNIT_KINDS:
            raise ValueError("FINANCIAL_MAPPING_UNIT_KIND_INVALID")


@dataclass(frozen=True)
class FactMappingRegistry:
    mapping_version: str
    mappings: Mapping[str, FactMapping]

    def __post_init__(self) -> None:
        mappings = dict(self.mappings)
        if any(raw_qname != mapping.raw_qname for raw_qname, mapping in mappings.items()):
            raise ValueError("mapping keys must match their raw QName")
        object.__setattr__(self, "mappings", MappingProxyType(mappings))


class FinancialFactNormalizer:
    def __init__(
        self,
        registry: FactMappingRegistry,
        entity_registry: EntityMappingRegistry,
    ) -> None:
        self._registry = registry
        self._entity_registry = entity_registry

    @property
    def mapping_version(self) -> str:
        return self._registry.mapping_version

    def normalize(
        self,
        filing: FinancialFiling,
        raw_facts: list[RawXbrlFact],
    ) -> list[FinancialFact]:
        return [self._normalize_fact(filing, raw_fact) for raw_fact in raw_facts]

    def _normalize_fact(
        self,
        filing: FinancialFiling,
        raw_fact: RawXbrlFact,
    ) -> FinancialFact:
        mapping = self._registry.mappings.get(raw_fact.raw_qname)
        if mapping is None:
            mapping_status = MappingStatus.UNMAPPED
            canonical_fact_name = None
            statement_type = StatementType.OTHER
            quality_status = QualityStatus.UNVERIFIED
        else:
            owners = self._entity_registry.owners_for(
                raw_fact.context.entity_scheme,
                raw_fact.context.entity_identifier,
            )
            if owners != (filing.ts_code,):
                raise ValueError("FINANCIAL_ENTITY_MISMATCH")
            if mapping.expected_unit_kind != _unit_kind(raw_fact.unit):
                raise ValueError("FINANCIAL_UNIT_KIND_MISMATCH")
            mapping_status = MappingStatus.MAPPED
            canonical_fact_name = mapping.canonical_fact_name
            statement_type = mapping.statement_type
            quality_status = QualityStatus.VALID

        context_payload = _context_payload(raw_fact.context)
        unit_signature = _unit_signature(raw_fact.unit)
        fact_identity_hash = stable_hash(
            {
                "filing_id": filing.filing_id,
                "raw_qname": raw_fact.raw_qname,
                "entity_scheme": raw_fact.context.entity_scheme,
                "entity_identifier": raw_fact.context.entity_identifier,
                "period": context_payload["period"],
                "unit_signature": unit_signature,
                "dimensions": context_payload["dimensions"],
            }
        )
        consolidation_scope = ConsolidationScope.UNKNOWN
        comparison_identity_hash = stable_hash(
            {
                "raw_qname": raw_fact.raw_qname,
                "entity_scheme": raw_fact.context.entity_scheme,
                "entity_identifier": raw_fact.context.entity_identifier,
                "period": context_payload["period"],
                "unit_signature": unit_signature,
                "dimensions": context_payload["dimensions"],
                "report_period": filing.report_period.isoformat(),
                "report_type": filing.report_type.value,
                "consolidation_scope": consolidation_scope.value,
            }
        )
        observation_hash = stable_hash(
            {
                "fact_identity_hash": fact_identity_hash,
                "fact_value": str(raw_fact.value),
                "decimals": raw_fact.decimals,
            }
        )
        fact_id = f"fact-{observation_hash}"

        return FinancialFact(
            record_id=fact_id,
            fact_id=fact_id,
            source_id=filing.source_id,
            source_url=filing.source_url,
            published_at=filing.published_at,
            effective_at=datetime.combine(
                filing.report_period,
                time(23, 59, 59, tzinfo=SHANGHAI),
            ),
            collected_at=filing.collected_at,
            version=(
                f"{filing.filing_version}:{filing.parser_version}:{self._registry.mapping_version}"
            ),
            content_hash=filing.content_hash,
            license_policy=filing.license_policy,
            quality_status=quality_status,
            supersedes_id=None,
            valid_from=filing.valid_from,
            ts_code=filing.ts_code,
            report_period=filing.report_period,
            report_type=filing.report_type,
            announcement_at=filing.announcement_at,
            statement_type=statement_type,
            taxonomy=filing.taxonomy,
            fact_name=raw_fact.fact_name,
            raw_qname=raw_fact.raw_qname,
            canonical_fact_name=canonical_fact_name,
            mapping_status=mapping_status,
            fact_value=raw_fact.value,
            unit=_unit_text(raw_fact.unit),
            currency=raw_fact.unit.currency if raw_fact.unit is not None else None,
            filing_id=filing.filing_id,
            context_signature=stable_hash(context_payload),
            entity_scheme=raw_fact.context.entity_scheme,
            entity_identifier=raw_fact.context.entity_identifier,
            period_start=raw_fact.context.period_start,
            period_end=raw_fact.context.period_end,
            instant=raw_fact.context.instant,
            unit_signature=unit_signature,
            decimals=raw_fact.decimals,
            consolidation_scope=consolidation_scope,
            dimensions=dict(context_payload["dimensions"]),
            fact_identity_hash=fact_identity_hash,
            comparison_identity_hash=comparison_identity_hash,
        )


def stable_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _context_payload(context: RawXbrlContext) -> dict[str, object]:
    return {
        "entity_scheme": context.entity_scheme,
        "entity_identifier": context.entity_identifier,
        "period": {
            "period_start": _date_value(context.period_start),
            "period_end": _date_value(context.period_end),
            "instant": _date_value(context.instant),
        },
        "dimensions": sorted(context.dimensions),
    }


def _date_value(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _unit_signature(unit: RawXbrlUnit | None) -> str | None:
    if unit is None:
        return None
    return stable_hash(
        {
            "numerator_measures": sorted(unit.numerator_measures),
            "denominator_measures": sorted(unit.denominator_measures),
            "currency": unit.currency,
        }
    )


def _unit_text(unit: RawXbrlUnit | None) -> str | None:
    if unit is None:
        return None
    numerator = "*".join(sorted(unit.numerator_measures))
    denominator = "*".join(sorted(unit.denominator_measures))
    return f"{numerator}/{denominator}" if denominator else numerator


def _unit_kind(unit: RawXbrlUnit | None) -> str:
    if unit is None:
        return "NONE"
    if (
        len(unit.numerator_measures) == 1
        and not unit.denominator_measures
        and _is_iso_4217_measure(unit.numerator_measures[0])
    ):
        return "MONETARY"
    if (
        len(unit.numerator_measures) == 1
        and len(unit.denominator_measures) == 1
        and _is_iso_4217_measure(unit.numerator_measures[0])
        and unit.denominator_measures[0] == SHARES_MEASURE
    ):
        return "PER_SHARE"
    if unit.numerator_measures == (SHARES_MEASURE,) and not unit.denominator_measures:
        return "SHARES"
    if unit.numerator_measures == (PURE_MEASURE,) and not unit.denominator_measures:
        return "PURE"
    return "OTHER"


def _is_iso_4217_measure(measure: str) -> bool:
    namespace_prefix = f"{{{ISO_4217_NAMESPACE}}}"
    return measure.startswith(namespace_prefix) and len(measure) > len(namespace_prefix)


__all__ = [
    "EntityMappingRegistry",
    "FactMapping",
    "FactMappingRegistry",
    "FinancialFactNormalizer",
    "stable_hash",
]
