from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from importlib.metadata import version
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from arelle.api.Session import Session
from arelle.RuntimeOptions import RuntimeOptions

from .package import MaterializedFiling, _hold_materialized_tree_for_parser

_ARELLE_SESSION_LOCK = Lock()
_LINK_NAMESPACE = "http://www.xbrl.org/2003/linkbase"
_XLINK_NAMESPACE = "http://www.w3.org/1999/xlink"
_ISO_4217_NAMESPACE = "http://www.xbrl.org/2003/iso4217"
_FATAL_DTS_ERROR_CODES = frozenset(
    {
        "IOerror",
        "xbrl:schemaDefinitionMissing",
        "xbrl:schemaImportMissing",
    }
)


@dataclass(frozen=True)
class RawXbrlContext:
    context_id: str
    entity_scheme: str
    entity_identifier: str
    period_start: date | None
    period_end: date | None
    instant: date | None
    dimensions: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class RawXbrlUnit:
    unit_id: str
    numerator_measures: tuple[str, ...]
    denominator_measures: tuple[str, ...]
    currency: str | None


@dataclass(frozen=True)
class RawXbrlFact:
    raw_qname: str
    fact_name: str
    value: Decimal
    decimals: str | None
    context: RawXbrlContext
    unit: RawXbrlUnit | None


@dataclass(frozen=True)
class XbrlParseDiagnostics:
    nil_fact_count: int
    text_fact_count: int
    error_codes: tuple[str, ...]


@dataclass(frozen=True)
class XbrlParseResult:
    parser_name: str
    parser_version: str
    contexts: tuple[RawXbrlContext, ...]
    units: tuple[RawXbrlUnit, ...]
    facts: tuple[RawXbrlFact, ...]
    diagnostics: XbrlParseDiagnostics


class XbrlProcessor(Protocol):
    def parse(self, materialized: MaterializedFiling) -> XbrlParseResult: ...


class ArelleXbrlProcessor:
    def parse(self, materialized: MaterializedFiling) -> XbrlParseResult:
        try:
            with _ARELLE_SESSION_LOCK, _hold_materialized_tree_for_parser(materialized):
                _require_local_schema_refs(materialized)
                options = RuntimeOptions(
                    entrypointFile=str(materialized.entrypoint_path),
                    internetConnectivity="offline",
                    keepOpen=True,
                    packages=[
                        str(path) for path in materialized.taxonomy_package_paths
                    ],
                    validate=True,
                    disablePersistentConfig=True,
                )
                with Session() as session:
                    session.run(options)
                    models = session.get_models()
                    if len(models) != 1:
                        raise ValueError("FINANCIAL_XBRL_PARSE_ERROR")
                    _require_complete_dts(models[0])
                    return _copy_model(models[0])
        except AssertionError:
            raise
        except ValueError as exc:
            if str(exc) == "FINANCIAL_TAXONOMY_MISSING":
                raise
            raise ValueError("FINANCIAL_XBRL_PARSE_ERROR") from None
        except Exception:
            raise ValueError("FINANCIAL_XBRL_PARSE_ERROR") from None


def _require_local_schema_refs(materialized: MaterializedFiling) -> None:
    try:
        document = ElementTree.parse(materialized.entrypoint_path)
    except (ElementTree.ParseError, OSError):
        raise ValueError("FINANCIAL_XBRL_PARSE_ERROR") from None

    schema_refs = document.findall(f".//{{{_LINK_NAMESPACE}}}schemaRef")
    if not schema_refs:
        raise ValueError("FINANCIAL_XBRL_PARSE_ERROR")
    root = materialized.root.resolve()
    for schema_ref in schema_refs:
        href = schema_ref.get(f"{{{_XLINK_NAMESPACE}}}href")
        if not href:
            raise ValueError("FINANCIAL_TAXONOMY_MISSING")
        parsed = urlsplit(href)
        if parsed.scheme not in {"", "file"}:
            continue
        if parsed.scheme == "file":
            if parsed.netloc not in {"", "localhost"}:
                raise ValueError("FINANCIAL_XBRL_PARSE_ERROR")
            file_path = unquote(parsed.path)
            if os.name == "nt" and len(file_path) >= 3 and file_path[0] == "/":
                file_path = file_path[1:]
            referenced = Path(file_path).resolve()
        else:
            referenced = (
                materialized.entrypoint_path.parent / unquote(parsed.path)
            ).resolve()
        if referenced != root and root not in referenced.parents:
            raise ValueError("FINANCIAL_XBRL_PARSE_ERROR")
        if not referenced.is_file():
            raise ValueError("FINANCIAL_TAXONOMY_MISSING")


def _copy_model(model: Any) -> XbrlParseResult:
    contexts_by_id = {
        str(context_id): _copy_context(context)
        for context_id, context in model.contexts.items()
    }
    units_by_id = {
        str(unit_id): _copy_unit(unit)
        for unit_id, unit in model.units.items()
    }
    facts: list[RawXbrlFact] = []
    nil_fact_count = 0
    text_fact_count = 0
    for fact in model.factsInInstance:
        if bool(fact.isTuple):
            continue
        if bool(fact.isNil):
            nil_fact_count += 1
            continue
        if not bool(fact.isNumeric):
            text_fact_count += 1
            continue
        try:
            value = Decimal(str(fact.xValue))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("FINANCIAL_XBRL_PARSE_ERROR") from None
        context = contexts_by_id.get(str(fact.contextID))
        if context is None:
            raise ValueError("FINANCIAL_XBRL_PARSE_ERROR")
        unit = units_by_id.get(str(fact.unitID)) if fact.unitID is not None else None
        if fact.unitID is not None and unit is None:
            raise ValueError("FINANCIAL_XBRL_PARSE_ERROR")
        fact_qname = _clark_qname(fact.qname)
        facts.append(
            RawXbrlFact(
                raw_qname=fact_qname,
                fact_name=str(fact.qname.localName),
                value=value,
                decimals=str(fact.decimals) if fact.decimals is not None else None,
                context=context,
                unit=unit,
            )
        )

    contexts = tuple(sorted(contexts_by_id.values(), key=lambda item: item.context_id))
    units = tuple(sorted(units_by_id.values(), key=lambda item: item.unit_id))
    sorted_facts = tuple(
        sorted(
            facts,
            key=lambda item: (
                item.raw_qname,
                item.context.context_id,
                item.decimals or "",
            ),
        )
    )
    return XbrlParseResult(
        parser_name="arelle",
        parser_version=version("arelle-release"),
        contexts=contexts,
        units=units,
        facts=sorted_facts,
        diagnostics=XbrlParseDiagnostics(
            nil_fact_count=nil_fact_count,
            text_fact_count=text_fact_count,
            error_codes=tuple(sorted(str(error) for error in model.errors)),
        ),
    )


def _require_complete_dts(model: Any) -> None:
    if _FATAL_DTS_ERROR_CODES.intersection(str(error) for error in model.errors):
        raise ValueError("FINANCIAL_XBRL_PARSE_ERROR")


def _copy_context(context: Any) -> RawXbrlContext:
    entity_scheme, entity_identifier = context.entityIdentifier
    if any(
        dimension.memberQname is None for dimension in context.qnameDims.values()
    ):
        raise ValueError("FINANCIAL_XBRL_PARSE_ERROR")
    dimensions = tuple(
        sorted(
            (
                _clark_qname(dimension.dimensionQname),
                _clark_qname(dimension.memberQname),
            )
            for dimension in context.qnameDims.values()
        )
    )
    if context.isInstantPeriod:
        instant = _exclusive_boundary_date(context.instantDatetime)
        period_start = None
        period_end = None
    elif context.isStartEndPeriod:
        instant = None
        period_start = context.startDatetime.date()
        period_end = _exclusive_boundary_date(context.endDatetime)
    else:
        raise ValueError("FINANCIAL_XBRL_PARSE_ERROR")
    return RawXbrlContext(
        context_id=str(context.id),
        entity_scheme=str(entity_scheme),
        entity_identifier=str(entity_identifier),
        period_start=period_start,
        period_end=period_end,
        instant=instant,
        dimensions=dimensions,
    )


def _copy_unit(unit: Any) -> RawXbrlUnit:
    numerator, denominator = unit.measures
    numerator_measures = tuple(sorted(_clark_qname(measure) for measure in numerator))
    denominator_measures = tuple(
        sorted(_clark_qname(measure) for measure in denominator)
    )
    currency = None
    if len(numerator) == 1 and not denominator:
        measure = numerator[0]
        if measure.namespaceURI == _ISO_4217_NAMESPACE:
            currency = str(measure.localName)
    return RawXbrlUnit(
        unit_id=str(unit.id),
        numerator_measures=numerator_measures,
        denominator_measures=denominator_measures,
        currency=currency,
    )


def _clark_qname(qname: Any) -> str:
    namespace = qname.namespaceURI
    local_name = str(qname.localName)
    return f"{{{namespace}}}{local_name}" if namespace else local_name


def _exclusive_boundary_date(value: Any) -> date:
    return (value - timedelta(days=1)).date()


__all__ = [
    "ArelleXbrlProcessor",
    "RawXbrlContext",
    "RawXbrlFact",
    "RawXbrlUnit",
    "XbrlParseDiagnostics",
    "XbrlParseResult",
    "XbrlProcessor",
]
