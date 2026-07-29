import hashlib
import io
import json
import socket
import sys
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from subprocess import run
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from hengce import cli
from hengce.cli import app, build_market_ingestion, load_trade_dates
from hengce.config import Settings
from hengce.contracts.enums import DiscoveryMethod, ReportType, RunStatus
from hengce.financials.xbrl import (
    RawXbrlContext,
    RawXbrlFact,
    RawXbrlUnit,
    XbrlParseDiagnostics,
    XbrlParseResult,
)
from hengce.services.financial_ingestion import FinancialIngestionResult
from hengce.services.initializer import InitializationResult
from hengce.services.market_ingestion import MarketIngestionResult
from hengce.state.financial_repository import FinancialFilingRepository
from hengce.state.repository import StateRepository

POLICY_FILE = Path(__file__).parents[2] / "config" / "source_policies.json"
XBRL_FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "xbrl" / "minimal"
VALID_INSTANCE = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"/>'
)
VALID_TAXONOMY = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema"/>'
)


class ClosingClient:
    def __init__(self) -> None:
        self.closed = False

    def __enter__(self) -> "ClosingClient":
        return self

    def __exit__(self, *args: object) -> bool:
        self.closed = True
        return False


class FakeFinancialIngestion:
    def __init__(self, result: FinancialIngestionResult) -> None:
        self.result = result
        self.descriptors: list[object] = []

    def run(self, descriptor: object) -> FinancialIngestionResult:
        self.descriptors.append(descriptor)
        return self.result


class NoisyFinancialIngestion(FakeFinancialIngestion):
    def run(self, descriptor: object) -> FinancialIngestionResult:
        print("parser-stdout-log")
        print("parser-stderr-log", file=sys.stderr)
        return super().run(descriptor)


class FakeLocalXbrlProcessor:
    def parse(self, materialized: object) -> XbrlParseResult:
        context = RawXbrlContext(
            context_id="context-1",
            entity_scheme="https://www.sse.com.cn/entity",
            entity_identifier="699999.SH",
            period_start=None,
            period_end=None,
            instant=date(2025, 12, 31),
            dimensions=(),
        )
        unit = RawXbrlUnit(
            unit_id="unit-cny",
            numerator_measures=("{http://www.xbrl.org/2003/iso4217}CNY",),
            denominator_measures=(),
            currency="CNY",
        )
        return XbrlParseResult(
            parser_name="local-fixture-parser",
            parser_version="1.0",
            contexts=(context,),
            units=(unit,),
            facts=(
                RawXbrlFact(
                    raw_qname="{urn:hengce:unmapped}Assets",
                    fact_name="Assets",
                    value=Decimal("100"),
                    decimals="0",
                    context=context,
                    unit=unit,
                ),
            ),
            diagnostics=XbrlParseDiagnostics(
                nil_fact_count=0,
                text_fact_count=0,
                error_codes=(),
            ),
        )


def successful_financial_ingestion() -> FakeFinancialIngestion:
    return FakeFinancialIngestion(
        FinancialIngestionResult(
            filing_id="filing-1",
            run_id="run-1",
            run_status=RunStatus.SUCCEEDED,
            fact_count=4,
            conflict_count=0,
            artifact_path="facts.parquet",
            artifact_hash="a" * 64,
            error_code=None,
        )
    )


def xbrl_zip_payload() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("instance.xml", VALID_INSTANCE)
    return output.getvalue()


def invoke_fixture_taxonomy_registration(
    tmp_path: Path,
    *,
    payload: bytes = VALID_TAXONOMY,
    content_type: str = "application/xml-schema",
    file_name: str = "test-gaap.xsd",
    entrypoint: str = "test-gaap.xsd",
    collected_at: str = "2026-07-26T12:00:00+08:00",
    source_id: str = "sse",
    source_url: str = "https://www.sse.com.cn/test-gaap.xsd",
) -> object:
    taxonomy = tmp_path / file_name
    taxonomy.write_bytes(payload)
    return CliRunner().invoke(
        app,
        [
            "register-xbrl-taxonomy",
            "--file",
            str(taxonomy),
            "--taxonomy-id",
            "test-gaap-2025",
            "--source-id",
            source_id,
            "--source-url",
            source_url,
            "--entrypoint",
            entrypoint,
            "--content-type",
            content_type,
            "--collected-at",
            collected_at,
            "--data-dir",
            str(tmp_path / "data"),
            "--policy-file",
            str(POLICY_FILE),
        ],
    )


def financial_import_args(
    tmp_path: Path,
    *,
    payload: bytes = VALID_INSTANCE,
    source_id: str = "sse",
    source_url: str = "https://www.sse.com.cn/filing.xml",
    ts_code: str = "699999.SH",
    exchange: str = "SSE",
    report_period: str = "2025-12-31",
    published_at: str = "2026-04-30T09:00:00+08:00",
    collected_at: str = "2026-07-26T12:00:00+08:00",
    content_type: str = "application/xbrl+xml",
    suffix: str = ".xml",
    taxonomy_ids: tuple[str, ...] = ("test-gaap-2025",),
    instance_entrypoint: str | None = None,
    policy_file: Path = POLICY_FILE,
) -> list[str]:
    filing = tmp_path / f"filing{suffix}"
    filing.write_bytes(payload)
    arguments = [
        "import-financial-xbrl",
        "--file",
        str(filing),
        "--source-id",
        source_id,
        "--source-url",
        source_url,
        "--ts-code",
        ts_code,
        "--exchange",
        exchange,
        "--report-period",
        report_period,
        "--report-type",
        "ANNUAL",
        "--published-at",
        published_at,
        "--collected-at",
        collected_at,
        "--content-type",
        content_type,
    ]
    for taxonomy_id in taxonomy_ids:
        arguments.extend(["--taxonomy-id", taxonomy_id])
    if instance_entrypoint is not None:
        arguments.extend(["--instance-entrypoint", instance_entrypoint])
    arguments.extend(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "--policy-file",
            str(policy_file),
        ]
    )
    return arguments


def test_init_state_creates_sqlite_database_idempotently(tmp_path: Path) -> None:
    runner = CliRunner()

    first = runner.invoke(app, ["init-state", "--data-dir", str(tmp_path)])
    second = runner.invoke(app, ["init-state", "--data-dir", str(tmp_path)])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert (tmp_path / "state" / "hengce.sqlite3").exists()
    assert "state initialized" in first.stdout
    assert StateRepository(tmp_path / "state" / "hengce.sqlite3").count_policies() == 6


def test_init_state_seeds_approved_policies(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["init-state", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "state initialized" in result.stdout


def test_default_seed_is_packaged_and_matches_operator_copy() -> None:
    from importlib.resources import files

    packaged = files("hengce").joinpath("data", "source_policies.json")
    operator_copy = Path(__file__).parents[2] / "config" / "source_policies.json"

    assert json.loads(packaged.read_text(encoding="utf-8")) == json.loads(
        operator_copy.read_text(encoding="utf-8")
    )


def test_wheel_contains_default_seed_and_installed_cli_initializes_state(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[2]
    dist = tmp_path / "dist"
    target = tmp_path / "site"
    data_dir = tmp_path / "data"
    build = run(
        [sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", str(dist)],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    wheel = next(dist.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert "hengce/data/source_policies.json" in archive.namelist()

    install = run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--ignore-installed",
            "--no-deps",
            "--prefix",
            str(target),
            str(wheel),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert install.returncode == 0, install.stderr
    wrapper = target / "Scripts" / "hengce.exe"
    assert wrapper.is_file()
    command = run(
        [str(wrapper), "init-state", "--data-dir", str(data_dir)],
        cwd=tmp_path,
        env={
            **__import__("os").environ,
            "PYTHONPATH": str(target / "Lib" / "site-packages"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert command.returncode == 0, command.stderr
    assert StateRepository(data_dir / "state" / "hengce.sqlite3").count_policies() == 6


@pytest.mark.parametrize(
    ("source_id", "source_url", "row"),
    [
        (
            "sse",
            "https://www.sse.com.cn/master.csv",
            "600000.SH,600000,Example,SSE,MAIN_SH,CNY,19991110,A_SHARE",
        ),
        (
            "szse",
            "https://www.szse.cn/master.csv",
            "000001.SZ,000001,Example,SZSE,MAIN_SZ,CNY,19910403,A_SHARE",
        ),
    ],
)
def test_import_security_master_persists_approved_official_file_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_id: str,
    source_url: str,
    row: str,
) -> None:
    runner = CliRunner()
    source = tmp_path / "security-master.csv"
    source.write_text(
        f"ts_code,symbol,name,exchange,board,currency,list_date,security_type\n{row}\n",
        encoding="utf-8",
    )
    client_factory = Mock()
    monkeypatch.setattr(cli.httpx, "Client", client_factory)

    result = runner.invoke(
        app,
        [
            "import-security-master",
            "--file",
            str(source),
            "--source-id",
            source_id,
            "--source-url",
            source_url,
            "--version",
            "2026-07-24",
            "--collected-at",
            "2026-07-24T09:00:00+00:00",
            "--data-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert output["security_count"] == 1
    assert output["source_id"] == source_id
    assert output["version"] == "2026-07-24"
    assert output["content_hash"]
    assert not client_factory.mock_calls


def test_check_security_universe_reports_composed_local_universe_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    sse = tmp_path / "sse-security-master.csv"
    sse_csv = (
        "ts_code,symbol,name,exchange,board,currency,list_date,security_type\n"
        "600000.SH,600000,Example,SSE,MAIN_SH,CNY,19991110,A_SHARE\n"
        "688001.SH,688001,Example,SSE,STAR,CNY,20190722,A_SHARE\n"
    )
    sse.write_bytes(sse_csv.encode("utf-8"))
    szse = tmp_path / "szse-security-master.csv"
    szse_csv = (
        "ts_code,symbol,name,exchange,board,currency,list_date,security_type\n"
        "000001.SZ,000001,Example,SZSE,MAIN_SZ,CNY,19910403,A_SHARE\n"
        "300001.SZ,300001,Example,SZSE,CHINEXT,CNY,20091030,A_SHARE\n"
    )
    szse.write_bytes(szse_csv.encode("utf-8"))
    client_factory = Mock()
    monkeypatch.setattr(cli.httpx, "Client", client_factory)

    for source_id, source_url, source, version in [
        ("szse", "https://www.szse.cn/master.csv", szse, "2026-07-24-szse"),
        ("sse", "https://www.sse.com.cn/master.csv", sse, "2026-07-24-sse"),
    ]:
        imported = runner.invoke(
            app,
            [
                "import-security-master",
                "--file",
                str(source),
                "--source-id",
                source_id,
                "--source-url",
                source_url,
                "--version",
                version,
                "--collected-at",
                "2026-07-24T09:00:00+00:00",
                "--data-dir",
                str(tmp_path),
            ],
        )
        assert imported.exit_code == 0

    monkeypatch.setenv("HENGCE_TUSHARE_TOKEN", "sentinel")
    bootstrap = Mock(wraps=cli.bootstrap_state)
    monkeypatch.setattr(cli, "bootstrap_state", bootstrap)
    first = runner.invoke(
        app,
        ["check-security-universe", "--data-dir", str(tmp_path)],
    )
    second = runner.invoke(
        app,
        ["check-security-universe", "--data-dir", str(tmp_path)],
    )

    assert first.exit_code == 0
    assert second.exit_code == 0
    output = json.loads(first.stdout)
    assert output == {
        "as_of": "2026-07-24T09:00:00+00:00",
        "component_count": 2,
        "components": [
            {"source_id": "sse", "version": "2026-07-24-sse"},
            {"source_id": "szse", "version": "2026-07-24-szse"},
        ],
        "security_count": 4,
        "universe_hash": "9c96ba030d69ebfd0102ff98da4c0f3995ba056518f0763de2a4d9d519409afa",
    }
    assert json.loads(second.stdout) == output
    assert bootstrap.call_count == 2
    assert all(call.args[0].tushare_token is None for call in bootstrap.call_args_list)
    assert not client_factory.mock_calls


def test_import_security_master_rejects_undeclared_source_before_persisting(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    source = tmp_path / "security-master.csv"
    source.write_text(
        "ts_code,symbol,name,exchange,board,currency,list_date,security_type\n"
        "600000.SH,600000,Example,SSE,MAIN_SH,CNY,19991110,A_SHARE\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "import-security-master",
            "--file",
            str(source),
            "--source-id",
            "unknown",
            "--source-url",
            "https://unknown.example/master.csv",
            "--version",
            "2026-07-24",
            "--collected-at",
            "2026-07-24T09:00:00+00:00",
            "--data-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code != 0
    assert result.exception is not None
    assert str(result.exception) == "SECURITY_MASTER_SOURCE_MISMATCH"
    assert not (tmp_path / "state").exists()


def test_import_security_master_audits_missing_declared_source_policy_without_persisting(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    source = tmp_path / "security-master.csv"
    source.write_text(
        "ts_code,symbol,name,exchange,board,currency,list_date,security_type\n"
        "600000.SH,600000,Example,SSE,MAIN_SH,CNY,19991110,A_SHARE\n",
        encoding="utf-8",
    )
    policies = json.loads(
        (Path(__file__).parents[2] / "config" / "source_policies.json").read_text(encoding="utf-8")
    )
    policy_file = tmp_path / "source-policies-without-sse.json"
    policy_file.write_text(
        json.dumps([policy for policy in policies if policy["source_id"] != "sse"]),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "import-security-master",
            "--file",
            str(source),
            "--source-id",
            "sse",
            "--source-url",
            "https://www.sse.com.cn/master.csv",
            "--version",
            "2026-07-24",
            "--collected-at",
            "2026-07-24T09:00:00+00:00",
            "--data-dir",
            str(tmp_path),
            "--policy-file",
            str(policy_file),
        ],
    )

    assert result.exit_code != 0
    assert result.exception is not None
    assert str(result.exception) == "SOURCE_POLICY_MISSING"
    repository = StateRepository(tmp_path / "state" / "hengce.sqlite3")
    assert repository.count_refusals() == 1
    content_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    assert repository.get_security_master_snapshot("sse", content_hash) is None


def test_import_security_master_rejects_cross_exchange_row_for_declared_source(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    source = tmp_path / "security-master.csv"
    source.write_text(
        "ts_code,symbol,name,exchange,board,currency,list_date,security_type\n"
        "000001.SZ,000001,Example,SZSE,MAIN_SZ,CNY,19910403,A_SHARE\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "import-security-master",
            "--file",
            str(source),
            "--source-id",
            "sse",
            "--source-url",
            "https://www.sse.com.cn/master.csv",
            "--version",
            "2026-07-24",
            "--collected-at",
            "2026-07-24T09:00:00+00:00",
            "--data-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)
    assert str(result.exception) == "SECURITY_MASTER_SOURCE_MISMATCH"


def test_register_taxonomy_rejects_policy_before_raw_persist(
    tmp_path: Path,
) -> None:
    result = invoke_fixture_taxonomy_registration(
        tmp_path,
        payload=(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY secret "body">]><xsd:schema/>'),
        source_id="unknown",
        source_url="https://example.com/test-gaap.xsd",
    )

    assert result.exit_code != 0
    assert result.exception is not None
    assert str(result.exception) == "SOURCE_POLICY_MISSING"
    assert not list((tmp_path / "data" / "raw").rglob("payload.bin"))


def test_register_taxonomy_persists_approved_local_object(tmp_path: Path) -> None:
    result = invoke_fixture_taxonomy_registration(tmp_path)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["taxonomy_id"] == "test-gaap-2025"
    assert len(payload["raw_object_hash"]) == 64
    assert result.stdout == f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n"
    assert "<xsd:schema" not in result.stdout
    references = FinancialFilingRepository(
        tmp_path / "data" / "state" / "hengce.sqlite3"
    ).get_taxonomies(("test-gaap-2025",))
    assert references[0].raw_object_hash == payload["raw_object_hash"]


def test_register_taxonomy_rejects_custom_approved_non_exchange_source_before_raw_persist(
    tmp_path: Path,
) -> None:
    policies = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    sse_policy = next(policy for policy in policies if policy["source_id"] == "sse")
    policy_file = tmp_path / "custom-policies.json"
    policy_file.write_text(
        json.dumps(
            [
                *policies,
                {
                    **sse_policy,
                    "source_id": "other",
                    "source_name": "Other approved source",
                    "allowed_domains": ["example.com"],
                    "terms_url": "https://example.com/terms",
                },
            ]
        ),
        encoding="utf-8",
    )
    taxonomy = tmp_path / "test-gaap.xsd"
    taxonomy.write_bytes(VALID_TAXONOMY)

    result = CliRunner().invoke(
        app,
        [
            "register-xbrl-taxonomy",
            "--file",
            str(taxonomy),
            "--taxonomy-id",
            "test-gaap-2025",
            "--source-id",
            "other",
            "--source-url",
            "https://example.com/test-gaap.xsd",
            "--entrypoint",
            "test-gaap.xsd",
            "--content-type",
            "application/xml-schema",
            "--collected-at",
            "2026-07-26T12:00:00+08:00",
            "--data-dir",
            str(tmp_path / "data"),
            "--policy-file",
            str(policy_file),
        ],
    )

    assert result.exit_code != 0
    assert result.exception is not None
    assert str(result.exception) == "FINANCIAL_SOURCE_MISMATCH"
    assert not list((tmp_path / "data" / "raw").rglob("payload.bin"))


def test_real_cli_materialization_keeps_registered_relative_taxonomy_reachable_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = invoke_fixture_taxonomy_registration(
        tmp_path,
        payload=(XBRL_FIXTURE_ROOT / "test-gaap.xsd").read_bytes(),
    )
    assert registered.exit_code == 0

    def blocked_connect(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked_connect)
    result = CliRunner().invoke(
        app,
        financial_import_args(
            tmp_path,
            payload=(XBRL_FIXTURE_ROOT / "instance.xml").read_bytes(),
        ),
    )

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert output["run_status"] == "PARTIAL"
    assert output["error_code"] == "FINANCIAL_FACT_UNMAPPED"
    assert output["fact_count"] == 4


def test_real_cli_overlay_collision_returns_terminal_sorted_json_without_leaks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered = invoke_fixture_taxonomy_registration(
        tmp_path,
        payload=VALID_TAXONOMY,
        content_type="application/xml",
        file_name="filing.xml",
        entrypoint="filing.xml",
        source_url="https://www.sse.com.cn/filing.xml",
    )
    assert registered.exit_code == 0
    secret = "private-overlay-instance-body"
    monkeypatch.setenv("HENGCE_TUSHARE_TOKEN", "private-overlay-environment")

    result = CliRunner().invoke(
        app,
        financial_import_args(
            tmp_path,
            payload=(
                b'<?xml version="1.0" encoding="UTF-8"?>'
                + f"<!--{secret}-->".encode()
                + b'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"/>'
            ),
        ),
    )

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert output["run_status"] == "FAILED"
    assert output["error_code"] == "FINANCIAL_OVERLAY_CONFLICT"
    assert result.stdout == f"{json.dumps(output, ensure_ascii=False, sort_keys=True)}\n"
    assert result.stderr == ""
    assert secret not in result.stdout
    assert "private-overlay-environment" not in result.stdout
    assert "parser" not in result.stdout.casefold()
    state = StateRepository(tmp_path / "data" / "state" / "hengce.sqlite3")
    runs = state.list_runs(run_type="financial_xbrl")
    assert len(runs) == 1
    assert runs[0].run_status is RunStatus.FAILED
    assert runs[0].error_code == "FINANCIAL_OVERLAY_CONFLICT"
    assert runs[0].finished_at is not None


def test_import_financial_xbrl_uses_injected_local_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = successful_financial_ingestion()
    monkeypatch.setattr(cli, "build_financial_ingestion", lambda *args: fake, raising=False)

    result = CliRunner().invoke(
        app,
        financial_import_args(
            tmp_path,
            taxonomy_ids=("test-gaap-2025", "exchange-common-2025"),
        ),
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["filing_id"] == "filing-1"
    descriptor = fake.descriptors[0]
    assert descriptor.discovery_method is DiscoveryMethod.MANUAL_IMPORT
    assert descriptor.taxonomy_refs == ("test-gaap-2025", "exchange-common-2025")
    assert descriptor.report_type is ReportType.ANNUAL
    output = json.loads(result.stdout)
    assert result.stdout == f"{json.dumps(output, ensure_ascii=False, sort_keys=True)}\n"
    assert "<xbrli:xbrl" not in result.stdout


def test_import_financial_xbrl_suppresses_parser_logs_around_sorted_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    successful = successful_financial_ingestion()
    noisy = NoisyFinancialIngestion(successful.result)
    monkeypatch.setattr(cli, "build_financial_ingestion", lambda *args: noisy)

    result = CliRunner().invoke(app, financial_import_args(tmp_path))

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert result.stdout == f"{json.dumps(output, ensure_ascii=False, sort_keys=True)}\n"
    assert "parser-stdout-log" not in result.stdout
    assert "parser-stderr-log" not in result.stderr


def test_register_taxonomy_rejects_unsafe_xml_before_raw_persist(tmp_path: Path) -> None:
    secret = "private-taxonomy-body"
    result = invoke_fixture_taxonomy_registration(
        tmp_path,
        payload=(
            f'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY secret "{secret}">]><xsd:schema/>'
        ).encode(),
    )

    assert result.exit_code != 0
    assert result.exception is not None
    assert str(result.exception) == "FINANCIAL_XML_UNSAFE"
    assert secret not in result.stdout
    assert secret not in result.stderr
    assert not list((tmp_path / "data" / "raw").rglob("payload.bin"))


def test_register_taxonomy_rejects_naive_collected_at_before_raw_persist(
    tmp_path: Path,
) -> None:
    result = invoke_fixture_taxonomy_registration(
        tmp_path,
        collected_at="2026-07-26T12:00:00",
    )

    assert result.exit_code != 0
    assert "collected-at must include an offset" in result.stderr
    assert not list((tmp_path / "data" / "raw").rglob("payload.bin"))


def test_register_taxonomy_rejects_non_relative_zip_entrypoint_before_raw_persist(
    tmp_path: Path,
) -> None:
    result = invoke_fixture_taxonomy_registration(
        tmp_path,
        payload=xbrl_zip_payload(),
        content_type="application/zip",
        file_name="taxonomy.zip",
        entrypoint="../test-gaap.xsd",
        source_url="https://www.sse.com.cn/taxonomy.zip",
    )

    assert result.exit_code != 0
    assert result.exception is not None
    assert str(result.exception) == "FINANCIAL_ENTRYPOINT_INVALID"
    assert not list((tmp_path / "data" / "raw").rglob("payload.bin"))


def test_register_taxonomy_rejects_mismatched_direct_entrypoint_before_raw_persist(
    tmp_path: Path,
) -> None:
    result = invoke_fixture_taxonomy_registration(
        tmp_path,
        entrypoint="other.xsd",
    )

    assert result.exit_code != 0
    assert result.exception is not None
    assert str(result.exception) == "FINANCIAL_ENTRYPOINT_INVALID"
    assert not list((tmp_path / "data" / "raw").rglob("payload.bin"))


def test_import_financial_xbrl_rejects_policy_before_inspecting_or_persisting(
    tmp_path: Path,
) -> None:
    secret = "private-filing-body"
    result = CliRunner().invoke(
        app,
        financial_import_args(
            tmp_path,
            payload=(
                f'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY secret "{secret}">]><xbrl/>'
            ).encode(),
            source_id="unknown",
            source_url="https://example.com/filing.xml",
        ),
    )

    assert result.exit_code != 0
    assert result.exception is not None
    assert str(result.exception) == "SOURCE_POLICY_MISSING"
    assert secret not in result.stdout
    assert secret not in result.stderr
    assert not list((tmp_path / "data" / "raw").rglob("payload.bin"))


def test_import_financial_xbrl_rejects_unsupported_mime_before_raw_persist(
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        app,
        financial_import_args(tmp_path, content_type="application/pdf"),
    )

    assert result.exit_code != 0
    assert result.exception is not None
    assert str(result.exception) == "FINANCIAL_ATTACHMENT_TYPE_INVALID"
    assert not list((tmp_path / "data" / "raw").rglob("payload.bin"))


def test_import_financial_xbrl_rejects_unsafe_xml_before_raw_persist(
    tmp_path: Path,
) -> None:
    secret = "private-instance-body"
    result = CliRunner().invoke(
        app,
        financial_import_args(
            tmp_path,
            payload=(
                f'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY secret "{secret}">]><xbrl/>'
            ).encode(),
        ),
    )

    assert result.exit_code != 0
    assert result.exception is not None
    assert str(result.exception) == "FINANCIAL_XML_UNSAFE"
    assert secret not in result.stdout
    assert secret not in result.stderr
    assert not list((tmp_path / "data" / "raw").rglob("payload.bin"))


def test_import_financial_xbrl_rejects_unsafe_zip_member_before_raw_persist(
    tmp_path: Path,
) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("instance.xml", VALID_INSTANCE)
        archive.writestr("../escape.xml", b"<xbrl/>")

    result = CliRunner().invoke(
        app,
        financial_import_args(
            tmp_path,
            payload=output.getvalue(),
            content_type="application/zip",
            suffix=".zip",
            instance_entrypoint="instance.xml",
        ),
    )

    assert list((tmp_path / "data" / "raw").rglob("payload.bin")) == []
    assert result.exit_code != 0
    assert result.exception is not None
    assert str(result.exception) == "FINANCIAL_ARCHIVE_UNSAFE_PATH"


def test_import_financial_xbrl_rejects_non_iso_report_period_before_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composition = Mock()
    monkeypatch.setattr(cli, "build_financial_ingestion", composition)

    result = CliRunner().invoke(
        app,
        financial_import_args(tmp_path, report_period="2025-1-1"),
    )

    assert result.exit_code != 0
    assert "report-period must use YYYY-MM-DD" in result.stderr
    assert not composition.mock_calls
    assert not list((tmp_path / "data" / "raw").rglob("payload.bin"))


def test_import_financial_xbrl_rejects_naive_published_at_before_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composition = Mock()
    monkeypatch.setattr(cli, "build_financial_ingestion", composition)

    result = CliRunner().invoke(
        app,
        financial_import_args(tmp_path, published_at="2026-04-30T09:00:00"),
    )

    assert result.exit_code != 0
    assert "published-at must include an offset" in result.stderr
    assert not composition.mock_calls
    assert not list((tmp_path / "data" / "raw").rglob("payload.bin"))


def test_import_financial_xbrl_rejects_naive_collected_at_before_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composition = Mock()
    monkeypatch.setattr(cli, "build_financial_ingestion", composition)

    result = CliRunner().invoke(
        app,
        financial_import_args(tmp_path, collected_at="2026-07-26T12:00:00"),
    )

    assert result.exit_code != 0
    assert "collected-at must include an offset" in result.stderr
    assert not composition.mock_calls
    assert not list((tmp_path / "data" / "raw").rglob("payload.bin"))


@pytest.mark.parametrize(
    ("source_id", "source_url", "ts_code", "exchange"),
    [
        ("sse", "https://www.sse.com.cn/filing.xml", "699999.SH", "SZSE"),
        ("sse", "https://www.sse.com.cn/filing.xml", "300001.SZ", "SSE"),
        ("szse", "https://www.szse.cn/filing.xml", "300001.SZ", "SSE"),
        ("szse", "https://www.szse.cn/filing.xml", "699999.SH", "SZSE"),
    ],
)
def test_import_financial_xbrl_rejects_exchange_or_ts_code_source_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_id: str,
    source_url: str,
    ts_code: str,
    exchange: str,
) -> None:
    composition = Mock()
    monkeypatch.setattr(cli, "build_financial_ingestion", composition)

    result = CliRunner().invoke(
        app,
        financial_import_args(
            tmp_path,
            source_id=source_id,
            source_url=source_url,
            ts_code=ts_code,
            exchange=exchange,
        ),
    )

    assert result.exit_code != 0
    assert result.exception is not None
    assert str(result.exception) == "FINANCIAL_SOURCE_MISMATCH"
    assert not composition.mock_calls
    assert not list((tmp_path / "data" / "raw").rglob("payload.bin"))


def test_import_financial_xbrl_rejects_direct_xml_entrypoint_before_raw_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composition = Mock()
    monkeypatch.setattr(cli, "build_financial_ingestion", composition)

    result = CliRunner().invoke(
        app,
        financial_import_args(tmp_path, instance_entrypoint="instance.xml"),
    )

    assert result.exit_code != 0
    assert result.exception is not None
    assert str(result.exception) == "FINANCIAL_ENTRYPOINT_INVALID"
    assert not composition.mock_calls
    assert not list((tmp_path / "data" / "raw").rglob("payload.bin"))


def test_import_financial_xbrl_rejects_zip_without_entrypoint_before_raw_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composition = Mock()
    monkeypatch.setattr(cli, "build_financial_ingestion", composition)

    result = CliRunner().invoke(
        app,
        financial_import_args(
            tmp_path,
            payload=xbrl_zip_payload(),
            suffix=".zip",
            content_type="application/zip",
        ),
    )

    assert result.exit_code != 0
    assert result.exception is not None
    assert str(result.exception) == "FINANCIAL_ENTRYPOINT_INVALID"
    assert not composition.mock_calls
    assert not list((tmp_path / "data" / "raw").rglob("payload.bin"))


def test_import_financial_xbrl_rejects_non_relative_zip_entrypoint_before_raw_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composition = Mock()
    monkeypatch.setattr(cli, "build_financial_ingestion", composition)

    result = CliRunner().invoke(
        app,
        financial_import_args(
            tmp_path,
            payload=xbrl_zip_payload(),
            suffix=".zip",
            content_type="application/zip",
            instance_entrypoint="../instance.xml",
        ),
    )

    assert result.exit_code != 0
    assert result.exception is not None
    assert str(result.exception) == "FINANCIAL_ENTRYPOINT_INVALID"
    assert not composition.mock_calls
    assert not list((tmp_path / "data" / "raw").rglob("payload.bin"))


def test_import_financial_xbrl_accepts_szse_code_and_relative_zip_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = successful_financial_ingestion()
    monkeypatch.setattr(cli, "build_financial_ingestion", lambda *args: fake)

    result = CliRunner().invoke(
        app,
        financial_import_args(
            tmp_path,
            payload=xbrl_zip_payload(),
            source_id="szse",
            source_url="https://www.szse.cn/filing.zip",
            ts_code="300001.SZ",
            exchange="SZSE",
            suffix=".zip",
            content_type="application/zip",
            instance_entrypoint="instance.xml",
        ),
    )

    assert result.exit_code == 0
    descriptor = fake.descriptors[0]
    assert descriptor.source_id == "szse"
    assert descriptor.ts_code == "300001.SZ"
    assert descriptor.exchange == "SZSE"
    assert descriptor.instance_entrypoint == "instance.xml"


def test_import_financial_xbrl_rejects_lowercase_report_type_before_raw_persist(
    tmp_path: Path,
) -> None:
    arguments = financial_import_args(tmp_path)
    arguments[arguments.index("ANNUAL")] = "annual"

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code != 0
    assert not list((tmp_path / "data" / "raw").rglob("payload.bin"))


def test_import_financial_xbrl_missing_taxonomy_returns_sorted_local_result(
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(app, financial_import_args(tmp_path))

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert output["run_status"] == "BLOCKED"
    assert output["error_code"] == "FINANCIAL_TAXONOMY_MISSING"
    assert result.stdout == f"{json.dumps(output, ensure_ascii=False, sort_keys=True)}\n"
    assert list((tmp_path / "data" / "raw").rglob("payload.bin"))


def test_build_financial_ingestion_uses_empty_mapping_and_single_layer_dataset(
    tmp_path: Path,
) -> None:
    service = cli.build_financial_ingestion(Settings(data_dir=tmp_path / "data"))

    assert service.normalizer._registry.mapping_version == "empty-v1"
    assert not service.normalizer._registry.mappings
    assert service.warehouse.dataset == tmp_path / "data" / "warehouse" / "financial_facts"


def test_real_default_policy_import_reaches_unmapped_quality_without_secret_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = invoke_fixture_taxonomy_registration(tmp_path)
    assert registered.exit_code == 0
    monkeypatch.setattr(
        cli,
        "ArelleXbrlProcessor",
        Mock(return_value=FakeLocalXbrlProcessor()),
    )
    monkeypatch.setenv("HENGCE_TUSHARE_TOKEN", "private-environment-token")

    result = CliRunner().invoke(app, financial_import_args(tmp_path))

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert output["run_status"] == "PARTIAL"
    assert output["error_code"] == "FINANCIAL_FACT_UNMAPPED"
    assert "private-environment-token" not in result.stdout
    assert "<xbrli:xbrl" not in result.stdout
    artifact_path = Path(output["artifact_path"])
    assert artifact_path.is_file()
    assert artifact_path.is_relative_to(tmp_path / "data" / "warehouse" / "financial_facts")
    assert "financial_facts/financial_facts" not in artifact_path.as_posix()


def test_local_xbrl_commands_never_construct_http_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client_factory = Mock()
    monkeypatch.setattr(cli.httpx, "Client", client_factory)
    registered = invoke_fixture_taxonomy_registration(tmp_path)
    fake = successful_financial_ingestion()
    monkeypatch.setattr(cli, "build_financial_ingestion", lambda *args: fake)

    imported = CliRunner().invoke(app, financial_import_args(tmp_path))

    assert registered.exit_code == 0
    assert imported.exit_code == 0
    assert not client_factory.mock_calls


def test_build_market_ingestion_rejects_missing_token_before_client_use(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, tushare_token=None)
    client = Mock()

    with pytest.raises(Exception, match="HENGCE_TUSHARE_TOKEN is required"):
        build_market_ingestion(settings, client)

    assert not client.mock_calls


def test_ingest_command_uses_injected_composition_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    service = Mock()
    service.run.return_value = MarketIngestionResult(
        trade_date="2026-07-24",
        bar_count=1,
        raw_content_hash="a" * 64,
        parquet_path=str(tmp_path / "normalized" / "part.parquet"),
    )
    composition = Mock(return_value=service)
    monkeypatch.setattr(cli, "build_market_ingestion", composition)

    result = runner.invoke(
        app,
        ["ingest-market", "--trade-date", "2026-07-24", "--data-dir", str(tmp_path)],
    )

    assert result.exit_code == 0
    service.run.assert_called_once_with(date(2026, 7, 24))
    assert "HENGCE_TUSHARE_TOKEN" not in result.stdout


def test_ingest_rejects_invalid_trade_date_before_client_or_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    client_factory = Mock()
    composition = Mock()
    monkeypatch.setattr(cli.httpx, "Client", client_factory)
    monkeypatch.setattr(cli, "build_market_ingestion", composition)

    result = runner.invoke(
        app,
        ["ingest-market", "--trade-date", "2026-7-24", "--data-dir", str(tmp_path)],
    )

    assert result.exit_code != 0
    assert "trade date must use YYYY-MM-DD" in result.stderr
    assert not client_factory.mock_calls
    assert not composition.mock_calls


def test_history_rejects_invalid_calendar_before_client_or_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    calendar = tmp_path / "invalid-calendar.json"
    calendar.write_text('{"not": "a list"}', encoding="utf-8")
    client_factory = Mock()
    composition = Mock()
    monkeypatch.setattr(cli.httpx, "Client", client_factory)
    monkeypatch.setattr(cli, "build_market_ingestion", composition)

    result = runner.invoke(
        app,
        ["initialize-history", "--calendar-file", str(calendar), "--data-dir", str(tmp_path)],
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)
    assert "TRADE_DATES_INVALID" in str(result.exception)
    assert not client_factory.mock_calls
    assert not composition.mock_calls


@pytest.mark.parametrize("command_name", ["ingest-market", "initialize-history"])
@pytest.mark.parametrize("fails", [False, True])
def test_network_commands_close_owned_client_on_success_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command_name: str,
    fails: bool,
) -> None:
    runner = CliRunner()
    client = ClosingClient()
    monkeypatch.setattr(cli.httpx, "Client", Mock(return_value=client))
    service = Mock()
    if fails:
        service.run.side_effect = RuntimeError("fixture failure")
    else:
        service.run.return_value = MarketIngestionResult(
            "2026-07-24", 1, "a" * 64, "fixture.parquet"
        )
    monkeypatch.setattr(cli, "build_market_ingestion", Mock(return_value=service))

    if command_name == "ingest-market":
        arguments = ["ingest-market", "--trade-date", "2026-07-24", "--data-dir", str(tmp_path)]
    else:
        calendar = tmp_path / "calendar.json"
        calendar.write_text('["2026-07-24"]', encoding="utf-8")
        initializer = Mock()
        if fails:
            initializer.run.side_effect = RuntimeError("fixture failure")
        else:
            initializer.run.return_value = InitializationResult(
                completed_dates=1, last_trade_date="2026-07-24"
            )
        monkeypatch.setattr(cli, "HistoricalInitializer", Mock(return_value=initializer))
        arguments = [
            "initialize-history",
            "--calendar-file",
            str(calendar),
            "--data-dir",
            str(tmp_path),
        ]

    result = runner.invoke(app, arguments)

    assert (result.exit_code != 0) is fails
    assert client.closed


def test_load_trade_dates_sorts_and_deduplicates(tmp_path: Path) -> None:
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(["2026-07-24", "2026-07-22", "2026-07-22"]), encoding="utf-8")

    assert load_trade_dates(calendar) == [date(2026, 7, 22), date(2026, 7, 24)]


@pytest.mark.parametrize(
    "payload",
    [
        {"date": "2026-07-22"},
        ["2026-07-22", 123],
        ["2026-07-22", "2026-7-22"],
        ["2026-02-30"],
    ],
)
def test_load_trade_dates_rejects_invalid_calendar(tmp_path: Path, payload: object) -> None:
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="TRADE_DATES_INVALID"):
        load_trade_dates(calendar)
