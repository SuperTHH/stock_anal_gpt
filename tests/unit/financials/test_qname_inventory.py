import hashlib
from pathlib import Path

from hengce.financials.qname_inventory import QNameInventory

FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "xbrl" / "pilot"


def test_inventory_enumerates_exact_qname_evidence_without_mapping() -> None:
    taxonomy = FIXTURE_ROOT / "pilot-gaap.xsd"

    items = QNameInventory().inspect(
        FIXTURE_ROOT / "instance.xml",
        [taxonomy],
    )

    assert len(items) == 15
    assert {item.raw_qname for item in items} >= {
        "{urn:hengce:pilot-gaap}Revenue",
        "{urn:hengce:pilot-gaap}TotalShares",
    }
    revenue = next(item for item in items if item.raw_qname.endswith("}Revenue"))
    shares = next(item for item in items if item.raw_qname.endswith("}TotalShares"))
    assert revenue.namespace == "urn:hengce:pilot-gaap"
    assert revenue.taxonomy_label == "Fictional revenue"
    assert revenue.unit_kind == "MONETARY"
    assert revenue.period_type == "duration"
    assert shares.unit_kind == "SHARES"
    assert shares.period_type == "instant"
    assert revenue.taxonomy_hash == hashlib.sha256(taxonomy.read_bytes()).hexdigest()
    assert not hasattr(revenue, "canonical_fact_name")


def test_inventory_keeps_unknown_instance_qname_without_guessing_label(
    tmp_path: Path,
) -> None:
    original = (FIXTURE_ROOT / "instance.xml").read_text(encoding="utf-8")
    instance = tmp_path / "instance.xml"
    instance.write_text(
        original.replace(
            "</xbrli:xbrl>",
            '<p:UnknownProfit contextRef="duration" unitRef="CNY" '
            'decimals="0">1</p:UnknownProfit></xbrli:xbrl>',
        ),
        encoding="utf-8",
    )

    items = QNameInventory().inspect(
        instance,
        [FIXTURE_ROOT / "pilot-gaap.xsd"],
    )

    unknown = next(item for item in items if item.raw_qname.endswith("}UnknownProfit"))
    assert unknown.taxonomy_label is None
    assert unknown.unit_kind == "MONETARY"
    assert unknown.period_type == "duration"
