from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from hengce.contracts.enums import (
    ConflictResolutionStatus,
    MappingStatus,
    QualityStatus,
)
from hengce.contracts.financial import FactConflict, FinancialFact, FinancialFiling


@dataclass(frozen=True)
class QualityIssue:
    code: str
    fact_ids: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class FinancialQualityResult:
    facts: tuple[FinancialFact, ...]
    conflicts: tuple[FactConflict, ...]
    issues: tuple[QualityIssue, ...]
    filing_quality_status: QualityStatus


def rounding_tolerance(decimals: str | None) -> Decimal:
    if decimals in {None, "INF"}:
        return Decimal(0)
    return Decimal("0.5") * (Decimal(10) ** (-int(decimals)))


class FinancialQualityValidator:
    def validate(
        self,
        filing: FinancialFiling,
        facts: list[FinancialFact],
    ) -> FinancialQualityResult:
        issues: list[QualityIssue] = []
        conflicts: list[FactConflict] = []
        retained_facts: list[FinancialFact] = []
        conflicting_fact_ids: set[str] = set()
        has_no_facts = not facts

        if has_no_facts:
            issues.append(
                QualityIssue(
                    code="FINANCIAL_NUMERIC_FACTS_MISSING",
                    fact_ids=(),
                    detail="filing contains no numeric facts",
                )
            )

        facts_by_identity: dict[str, list[FinancialFact]] = defaultdict(list)
        for fact in facts:
            facts_by_identity[fact.fact_identity_hash].append(fact)

        for fact_identity_hash in sorted(facts_by_identity):
            candidates = sorted(facts_by_identity[fact_identity_hash], key=_fact_sort_key)
            fact_ids = tuple(fact.fact_id for fact in candidates)
            if len({fact.fact_value for fact in candidates}) == 1:
                retained_facts.append(candidates[0])
                if len(candidates) > 1:
                    issues.append(
                        QualityIssue(
                            code="FINANCIAL_FACT_DUPLICATE",
                            fact_ids=fact_ids,
                            detail="identical facts share one fact identity",
                        )
                    )
                continue

            conflicting_fact_ids.update(fact_ids)
            retained_facts.extend(
                fact.model_copy(update={"quality_status": QualityStatus.CONFLICT})
                for fact in candidates
            )
            conflicts.append(
                FactConflict(
                    conflict_id=_conflict_id(filing.filing_id, fact_identity_hash, fact_ids),
                    filing_id=filing.filing_id,
                    fact_identity_hash=fact_identity_hash,
                    competing_fact_ids=fact_ids,
                    conflict_type="VALUE_MISMATCH",
                    resolution_status=ConflictResolutionStatus.OPEN,
                    quality_status=QualityStatus.CONFLICT,
                    detected_at=filing.collected_at,
                )
            )
            issues.append(
                QualityIssue(
                    code="FINANCIAL_FACT_CONFLICT",
                    fact_ids=fact_ids,
                    detail="facts with one identity have different values",
                )
            )

        facts_by_canonical_identity: dict[CanonicalFactKey, list[FinancialFact]] = defaultdict(list)
        for fact in retained_facts:
            if (
                fact.fact_id not in conflicting_fact_ids
                and fact.mapping_status is MappingStatus.MAPPED
                and fact.canonical_fact_name is not None
            ):
                facts_by_canonical_identity[_canonical_fact_key(fact)].append(fact)

        folded_alias_fact_ids: set[str] = set()
        for canonical_key in sorted(facts_by_canonical_identity):
            candidates = sorted(
                facts_by_canonical_identity[canonical_key],
                key=_fact_sort_key,
            )
            if len({fact.raw_qname for fact in candidates}) <= 1:
                continue
            fact_ids = tuple(fact.fact_id for fact in candidates)
            if len({fact.fact_value for fact in candidates}) == 1:
                folded_alias_fact_ids.update(fact_ids[1:])
                issues.append(
                    QualityIssue(
                        code="FINANCIAL_FACT_ALIAS_DUPLICATE",
                        fact_ids=fact_ids,
                        detail="raw QName aliases have one canonical identity and value",
                    )
                )
                continue

            canonical_identity_hash = _canonical_identity_hash(canonical_key)
            conflicting_fact_ids.update(fact_ids)
            conflicts.append(
                FactConflict(
                    conflict_id=_conflict_id(
                        filing.filing_id,
                        canonical_identity_hash,
                        fact_ids,
                    ),
                    filing_id=filing.filing_id,
                    fact_identity_hash=canonical_identity_hash,
                    competing_fact_ids=fact_ids,
                    conflict_type="CANONICAL_VALUE_MISMATCH",
                    resolution_status=ConflictResolutionStatus.OPEN,
                    quality_status=QualityStatus.CONFLICT,
                    detected_at=filing.collected_at,
                )
            )
            issues.append(
                QualityIssue(
                    code="FINANCIAL_FACT_CONFLICT",
                    fact_ids=fact_ids,
                    detail="raw QName aliases map to one canonical fact with different values",
                )
            )

        retained_facts = [
            (
                fact.model_copy(update={"quality_status": QualityStatus.CONFLICT})
                if fact.fact_id in conflicting_fact_ids
                else fact
            )
            for fact in retained_facts
            if fact.fact_id not in folded_alias_fact_ids
        ]

        unmapped_fact_ids = tuple(
            fact.fact_id
            for fact in sorted(retained_facts, key=_fact_sort_key)
            if fact.mapping_status is MappingStatus.UNMAPPED
        )
        if unmapped_fact_ids:
            issues.append(
                QualityIssue(
                    code="FINANCIAL_FACT_UNMAPPED",
                    fact_ids=unmapped_fact_ids,
                    detail="unmapped facts cannot be fully validated",
                )
            )
            retained_facts = [
                (
                    fact.model_copy(update={"quality_status": QualityStatus.UNVERIFIED})
                    if fact.fact_id not in conflicting_fact_ids
                    and fact.mapping_status is MappingStatus.UNMAPPED
                    else fact
                )
                for fact in retained_facts
            ]

        equation_facts = [
            fact
            for fact in retained_facts
            if fact.fact_id not in conflicting_fact_ids
            and fact.mapping_status is MappingStatus.MAPPED
            and fact.canonical_fact_name in {"assets", "liabilities", "equity"}
        ]
        facts_by_equation_key: dict[EquationKey, list[FinancialFact]] = defaultdict(list)
        for fact in equation_facts:
            facts_by_equation_key[_equation_key(fact)].append(fact)

        equation_conflict_fact_ids: set[str] = set()
        has_missing_component = False
        for key in sorted(facts_by_equation_key, key=_equation_key_sort_key):
            grouped_facts = facts_by_equation_key[key]
            components = {
                name: sorted(
                    (fact for fact in grouped_facts if fact.canonical_fact_name == name),
                    key=_fact_sort_key,
                )
                for name in ("assets", "liabilities", "equity")
            }
            if any(not component_facts for component_facts in components.values()):
                has_missing_component = True
                issues.append(
                    QualityIssue(
                        code="FINANCIAL_BALANCE_COMPONENT_MISSING",
                        fact_ids=tuple(sorted(fact.fact_id for fact in grouped_facts)),
                        detail="balance equation is missing a component",
                    )
                )
                continue

            if any(len(component_facts) != 1 for component_facts in components.values()):
                continue

            assets, liabilities, equity = (
                components[name][0] for name in ("assets", "liabilities", "equity")
            )
            difference = abs(assets.fact_value - liabilities.fact_value - equity.fact_value)
            tolerance = sum(
                (rounding_tolerance(fact.decimals) for fact in (assets, liabilities, equity)),
                Decimal(0),
            )
            if difference > tolerance:
                fact_ids = tuple(sorted((assets.fact_id, liabilities.fact_id, equity.fact_id)))
                equation_conflict_fact_ids.update(fact_ids)
                issues.append(
                    QualityIssue(
                        code="FINANCIAL_BALANCE_EQUATION_CONFLICT",
                        fact_ids=fact_ids,
                        detail=(
                            "assets do not equal liabilities plus equity within rounding tolerance"
                        ),
                    )
                )

        if equation_conflict_fact_ids:
            retained_facts = [
                (
                    fact.model_copy(update={"quality_status": QualityStatus.CONFLICT})
                    if fact.fact_id in equation_conflict_fact_ids
                    else fact
                )
                for fact in retained_facts
            ]

        has_conflict = bool(conflicts or equation_conflict_fact_ids)
        has_partial = bool(has_no_facts or unmapped_fact_ids or has_missing_component)
        filing_quality_status = (
            QualityStatus.CONFLICT
            if has_conflict
            else QualityStatus.PARTIAL
            if has_partial
            else QualityStatus.VALID
        )
        return FinancialQualityResult(
            facts=tuple(sorted(retained_facts, key=_fact_sort_key)),
            conflicts=tuple(sorted(conflicts, key=lambda conflict: conflict.conflict_id)),
            issues=tuple(sorted(issues, key=_issue_sort_key)),
            filing_quality_status=filing_quality_status,
        )


EquationKey = tuple[
    date | None,
    str | None,
    str | None,
    object,
    tuple[tuple[str, str], ...],
]


CanonicalFactKey = tuple[
    str,
    str,
    str,
    str,
    str,
    str,
    str,
    str,
    tuple[tuple[str, str], ...],
    str,
    str,
    str,
]


def _canonical_fact_key(fact: FinancialFact) -> CanonicalFactKey:
    if fact.canonical_fact_name is None:
        raise ValueError("mapped canonical fact requires a canonical name")
    return (
        fact.canonical_fact_name,
        fact.entity_scheme,
        fact.entity_identifier,
        fact.period_start.isoformat() if fact.period_start is not None else "",
        fact.period_end.isoformat() if fact.period_end is not None else "",
        fact.instant.isoformat() if fact.instant is not None else "",
        fact.unit_signature or "",
        fact.currency or "",
        tuple(sorted(fact.dimensions.items())),
        fact.consolidation_scope.value,
        fact.report_period.isoformat(),
        fact.report_type.value,
    )


def _canonical_identity_hash(key: CanonicalFactKey) -> str:
    payload = json.dumps(
        key,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _equation_key(fact: FinancialFact) -> EquationKey:
    return (
        fact.instant,
        fact.unit_signature,
        fact.currency,
        fact.consolidation_scope,
        tuple(sorted(fact.dimensions.items())),
    )


def _equation_key_sort_key(key: EquationKey) -> tuple[object, ...]:
    instant, unit_signature, currency, consolidation_scope, dimensions = key
    return (
        instant.isoformat() if instant is not None else "",
        unit_signature or "",
        currency or "",
        str(consolidation_scope),
        dimensions,
    )


def _fact_sort_key(fact: FinancialFact) -> tuple[str, ...]:
    return (
        fact.fact_id,
        fact.fact_identity_hash,
        str(fact.fact_value),
        fact.decimals or "",
        fact.canonical_fact_name or "",
    )


def _issue_sort_key(issue: QualityIssue) -> tuple[str, tuple[str, ...], str]:
    return issue.code, issue.fact_ids, issue.detail


def _conflict_id(filing_id: str, fact_identity_hash: str, fact_ids: tuple[str, ...]) -> str:
    payload = json.dumps(
        {
            "filing_id": filing_id,
            "fact_identity_hash": fact_identity_hash,
            "fact_ids": fact_ids,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "FinancialQualityResult",
    "FinancialQualityValidator",
    "QualityIssue",
    "rounding_tolerance",
]
