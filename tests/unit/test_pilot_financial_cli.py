import json
from pathlib import Path

from typer.testing import CliRunner

from hengce.cli import app

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "xbrl" / "pilot"


def test_inspect_financial_qnames_outputs_evidence_without_mapping() -> None:
    result = CliRunner().invoke(
        app,
        [
            "inspect-financial-qnames",
            "--instance-file",
            str(FIXTURE_ROOT / "instance.xml"),
            "--taxonomy-file",
            str(FIXTURE_ROOT / "pilot-gaap.xsd"),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload) == 15
    assert all("canonical_fact_name" not in item for item in payload)
    assert payload == sorted(
        payload,
        key=lambda item: (
            item["raw_qname"],
            item["period_type"],
            item["unit_kind"],
        ),
    )
