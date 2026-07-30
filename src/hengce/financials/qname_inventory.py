from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

_XBRLI = "http://www.xbrl.org/2003/instance"
_XSD = "http://www.w3.org/2001/XMLSchema"
_ISO_4217 = "http://www.xbrl.org/2003/iso4217"
_SHARES = f"{{{_XBRLI}}}shares"
_PURE = f"{{{_XBRLI}}}pure"


@dataclass(frozen=True, slots=True)
class QNameInventoryItem:
    raw_qname: str
    namespace: str
    taxonomy_label: str | None
    unit_kind: str
    period_type: str
    taxonomy_hash: str | None


@dataclass(frozen=True, slots=True)
class _TaxonomyConcept:
    label: str | None
    period_type: str | None
    taxonomy_hash: str


class QNameInventory:
    def inspect(
        self,
        instance_path: Path,
        taxonomy_paths: list[Path] | tuple[Path, ...],
    ) -> tuple[QNameInventoryItem, ...]:
        concepts = self._taxonomy_concepts(taxonomy_paths)
        namespaces, root = _safe_parse(instance_path)
        contexts = _context_period_types(root)
        units = _unit_kinds(root, namespaces)
        items: set[QNameInventoryItem] = set()
        for element in root.iter():
            context_id = element.get("contextRef")
            if context_id is None or not element.tag.startswith("{"):
                continue
            namespace, _, _local_name = element.tag[1:].partition("}")
            concept = concepts.get(element.tag)
            period_type = contexts.get(context_id)
            if period_type is None:
                raise ValueError("FINANCIAL_INVENTORY_CONTEXT_INVALID")
            unit_ref = element.get("unitRef")
            unit_kind = units.get(unit_ref, "NONE") if unit_ref else "NONE"
            items.add(
                QNameInventoryItem(
                    raw_qname=element.tag,
                    namespace=namespace,
                    taxonomy_label=concept.label if concept is not None else None,
                    unit_kind=unit_kind,
                    period_type=period_type,
                    taxonomy_hash=(
                        concept.taxonomy_hash if concept is not None else None
                    ),
                )
            )
        return tuple(
            sorted(
                items,
                key=lambda item: (
                    item.raw_qname,
                    item.period_type,
                    item.unit_kind,
                ),
            )
        )

    @staticmethod
    def _taxonomy_concepts(
        paths: list[Path] | tuple[Path, ...],
    ) -> dict[str, _TaxonomyConcept]:
        concepts: dict[str, _TaxonomyConcept] = {}
        for path in paths:
            _namespaces, root = _safe_parse(path)
            target_namespace = root.get("targetNamespace")
            if not target_namespace:
                raise ValueError("FINANCIAL_INVENTORY_TAXONOMY_INVALID")
            taxonomy_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            for element in root.findall(f"./{{{_XSD}}}element"):
                name = element.get("name")
                if not name:
                    continue
                qname = f"{{{target_namespace}}}{name}"
                documentation = element.find(
                    f"./{{{_XSD}}}annotation/{{{_XSD}}}documentation"
                )
                label = (
                    " ".join("".join(documentation.itertext()).split())
                    if documentation is not None
                    else None
                )
                concept = _TaxonomyConcept(
                    label=label or None,
                    period_type=element.get(f"{{{_XBRLI}}}periodType"),
                    taxonomy_hash=taxonomy_hash,
                )
                if qname in concepts and concepts[qname] != concept:
                    raise ValueError("FINANCIAL_INVENTORY_TAXONOMY_CONFLICT")
                concepts[qname] = concept
        return concepts


def _safe_parse(path: Path) -> tuple[dict[str, str], ElementTree.Element]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ValueError("FINANCIAL_INVENTORY_FILE_INVALID") from error
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError("FINANCIAL_INVENTORY_XML_UNSAFE")
    namespaces: dict[str, str] = {}
    try:
        for _event, namespace in ElementTree.iterparse(
            path,
            events=("start-ns",),
        ):
            prefix, uri = namespace
            namespaces[prefix] = uri
        root = ElementTree.fromstring(payload)
    except (ElementTree.ParseError, OSError) as error:
        raise ValueError("FINANCIAL_INVENTORY_FILE_INVALID") from error
    return namespaces, root


def _context_period_types(root: ElementTree.Element) -> dict[str, str]:
    period_types: dict[str, str] = {}
    for context in root.findall(f".//{{{_XBRLI}}}context"):
        context_id = context.get("id")
        period = context.find(f"./{{{_XBRLI}}}period")
        if context_id is None or period is None:
            continue
        if period.find(f"./{{{_XBRLI}}}instant") is not None:
            period_types[context_id] = "instant"
        elif (
            period.find(f"./{{{_XBRLI}}}startDate") is not None
            and period.find(f"./{{{_XBRLI}}}endDate") is not None
        ):
            period_types[context_id] = "duration"
    return period_types


def _unit_kinds(
    root: ElementTree.Element,
    namespaces: dict[str, str],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for unit in root.findall(f".//{{{_XBRLI}}}unit"):
        unit_id = unit.get("id")
        if unit_id is None:
            continue
        numerator: list[str] = []
        denominator: list[str] = []
        divide = unit.find(f"./{{{_XBRLI}}}divide")
        if divide is None:
            numerator.extend(
                _measure_qname(measure.text, namespaces)
                for measure in unit.findall(f"./{{{_XBRLI}}}measure")
            )
        else:
            numerator.extend(
                _measure_qname(measure.text, namespaces)
                for measure in divide.findall(
                    f"./{{{_XBRLI}}}unitNumerator/{{{_XBRLI}}}measure"
                )
            )
            denominator.extend(
                _measure_qname(measure.text, namespaces)
                for measure in divide.findall(
                    f"./{{{_XBRLI}}}unitDenominator/{{{_XBRLI}}}measure"
                )
            )
        result[unit_id] = _classify_unit(tuple(numerator), tuple(denominator))
    return result


def _measure_qname(value: str | None, namespaces: dict[str, str]) -> str:
    if value is None:
        return ""
    prefix, separator, local_name = value.strip().partition(":")
    if not separator or prefix not in namespaces:
        return value.strip()
    return f"{{{namespaces[prefix]}}}{local_name}"


def _classify_unit(
    numerator: tuple[str, ...],
    denominator: tuple[str, ...],
) -> str:
    if (
        len(numerator) == 1
        and not denominator
        and numerator[0].startswith(f"{{{_ISO_4217}}}")
    ):
        return "MONETARY"
    if numerator == (_SHARES,) and not denominator:
        return "SHARES"
    if numerator == (_PURE,) and not denominator:
        return "PURE"
    if (
        len(numerator) == 1
        and numerator[0].startswith(f"{{{_ISO_4217}}}")
        and denominator == (_SHARES,)
    ):
        return "PER_SHARE"
    return "OTHER"


__all__ = ["QNameInventory", "QNameInventoryItem"]
