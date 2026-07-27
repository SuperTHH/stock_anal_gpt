from __future__ import annotations

import shutil
import socket
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from datetime import date
from decimal import Decimal
from pathlib import Path
from threading import Lock
from types import TracebackType
from typing import Any

import pytest
from arelle.api.Session import Session as RealSession

from hengce.financials.package import MaterializedFiling
from hengce.financials.xbrl import ArelleXbrlProcessor

FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "xbrl" / "minimal"


@pytest.fixture
def materialized_fixture() -> MaterializedFiling:
    return MaterializedFiling(
        entrypoint_path=FIXTURE_ROOT / "instance.xml",
        taxonomy_package_paths=(FIXTURE_ROOT / "test-gaap.xsd",),
        root=FIXTURE_ROOT,
    )


class _TrackedSession(AbstractContextManager["_TrackedSession"]):
    def __init__(self, spy: ConcurrentSessionSpy) -> None:
        self._spy = spy
        self._session = RealSession()

    def __enter__(self) -> _TrackedSession:
        self._session.__enter__()
        with self._spy.lock:
            self._spy.active_sessions += 1
            self._spy.maximum_active_sessions = max(
                self._spy.maximum_active_sessions,
                self._spy.active_sessions,
            )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        try:
            return self._session.__exit__(exc_type, exc_value, traceback)
        finally:
            with self._spy.lock:
                self._spy.active_sessions -= 1

    def run(self, options: Any) -> None:
        self._session.run(options)

    def get_models(self) -> list[Any]:
        return self._session.get_models()


class ConcurrentSessionSpy:
    def __init__(self) -> None:
        self.lock = Lock()
        self.active_sessions = 0
        self.maximum_active_sessions = 0

    def session_type(self) -> _TrackedSession:
        return _TrackedSession(self)


def test_arelle_parses_numeric_facts_without_any_socket(
    monkeypatch: pytest.MonkeyPatch,
    materialized_fixture: MaterializedFiling,
) -> None:
    def blocked_connect(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked_connect)
    result = ArelleXbrlProcessor().parse(materialized_fixture)

    assert result.parser_name == "arelle"
    assert {fact.raw_qname for fact in result.facts} == {
        "{urn:hengce:test-gaap}Assets",
        "{urn:hengce:test-gaap}Liabilities",
        "{urn:hengce:test-gaap}Equity",
        "{urn:hengce:test-gaap}Revenue",
    }
    assets = next(fact for fact in result.facts if fact.fact_name == "Assets")
    assert assets.value == Decimal("1000")
    assert assets.context.instant == date(2025, 12, 31)
    assert assets.unit is not None
    assert assets.unit.currency == "CNY"
    revenue = next(fact for fact in result.facts if fact.fact_name == "Revenue")
    assert revenue.context.period_start == date(2025, 1, 1)
    assert revenue.context.period_end == date(2025, 12, 31)


def test_arelle_copies_neutral_frozen_data_before_session_closes(
    materialized_fixture: MaterializedFiling,
) -> None:
    result = ArelleXbrlProcessor().parse(materialized_fixture)

    assert result.contexts == tuple(sorted(result.contexts, key=lambda item: item.context_id))
    assert result.units == tuple(sorted(result.units, key=lambda item: item.unit_id))
    assert result.facts == tuple(
        sorted(
            result.facts,
            key=lambda item: (item.raw_qname, item.context.context_id, item.decimals or ""),
        )
    )
    with pytest.raises(AttributeError):
        result.facts[0].value = Decimal("0")  # type: ignore[misc]


def test_arelle_counts_nil_and_text_facts_without_returning_them(
    materialized_fixture: MaterializedFiling,
) -> None:
    result = ArelleXbrlProcessor().parse(materialized_fixture)

    assert result.diagnostics.nil_fact_count == 1
    assert result.diagnostics.text_fact_count == 1
    assert len(result.facts) == 4
    assert all(fact.fact_name != "DisclosureNote" for fact in result.facts)


def test_arelle_sessions_are_serialized_across_threads(
    monkeypatch: pytest.MonkeyPatch,
    materialized_fixture: MaterializedFiling,
) -> None:
    spy = ConcurrentSessionSpy()
    monkeypatch.setattr("hengce.financials.xbrl.Session", spy.session_type)
    processor = ArelleXbrlProcessor()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(processor.parse, [materialized_fixture, materialized_fixture])
        )

    assert all(len(result.facts) == 4 for result in results)
    assert spy.maximum_active_sessions == 1


def test_arelle_maps_missing_local_schema_to_stable_error(tmp_path: Path) -> None:
    entrypoint = tmp_path / "instance.xml"
    shutil.copyfile(FIXTURE_ROOT / "instance.xml", entrypoint)
    materialized = MaterializedFiling(
        entrypoint_path=entrypoint,
        taxonomy_package_paths=(),
        root=tmp_path,
    )

    with pytest.raises(ValueError, match="^FINANCIAL_TAXONOMY_MISSING$"):
        ArelleXbrlProcessor().parse(materialized)


def test_arelle_maps_missing_file_uri_schema_to_stable_error(tmp_path: Path) -> None:
    missing_schema = tmp_path / "missing-taxonomy.xsd"
    entrypoint = tmp_path / "instance.xml"
    entrypoint.write_text(
        (FIXTURE_ROOT / "instance.xml")
        .read_text(encoding="utf-8")
        .replace("test-gaap.xsd", missing_schema.as_uri()),
        encoding="utf-8",
    )
    materialized = MaterializedFiling(
        entrypoint_path=entrypoint,
        taxonomy_package_paths=(),
        root=tmp_path,
    )

    with pytest.raises(ValueError, match="^FINANCIAL_TAXONOMY_MISSING$"):
        ArelleXbrlProcessor().parse(materialized)


def test_arelle_maps_other_parse_failures_to_stable_error(tmp_path: Path) -> None:
    entrypoint = tmp_path / "instance.xml"
    entrypoint.write_text("<not-xbrl/>", encoding="utf-8")
    materialized = MaterializedFiling(
        entrypoint_path=entrypoint,
        taxonomy_package_paths=(),
        root=tmp_path,
    )

    with pytest.raises(ValueError, match="^FINANCIAL_XBRL_PARSE_ERROR$"):
        ArelleXbrlProcessor().parse(materialized)


def test_arelle_rejects_model_with_unresolved_taxonomy_import(tmp_path: Path) -> None:
    entrypoint = tmp_path / "instance.xml"
    schema = tmp_path / "test-gaap.xsd"
    shutil.copyfile(FIXTURE_ROOT / "instance.xml", entrypoint)
    schema.write_text(
        (FIXTURE_ROOT / "test-gaap.xsd")
        .read_text(encoding="utf-8")
        .replace(
            "http://www.xbrl.org/2003/xbrl-instance-2003-12-31.xsd",
            "missing-xbrl-instance.xsd",
        ),
        encoding="utf-8",
    )
    materialized = MaterializedFiling(
        entrypoint_path=entrypoint,
        taxonomy_package_paths=(schema,),
        root=tmp_path,
    )

    with pytest.raises(ValueError, match="^FINANCIAL_TAXONOMY_MISSING$"):
        ArelleXbrlProcessor().parse(materialized)


@pytest.mark.parametrize(
    ("tag_name", "use_file_uri"),
    [("include", False), ("redefine", True)],
)
def test_arelle_maps_missing_recursive_schema_to_taxonomy_missing(
    tmp_path: Path,
    tag_name: str,
    use_file_uri: bool,
) -> None:
    entrypoint = tmp_path / "instance.xml"
    schema = tmp_path / "test-gaap.xsd"
    missing = tmp_path / "missing-nested.xsd"
    location = missing.as_uri() if use_file_uri else missing.name
    shutil.copyfile(FIXTURE_ROOT / "instance.xml", entrypoint)
    schema.write_text(
        (FIXTURE_ROOT / "test-gaap.xsd")
        .read_text(encoding="utf-8")
        .replace(
            '    schemaLocation="http://www.xbrl.org/2003/'
            'xbrl-instance-2003-12-31.xsd"/>',
            '    schemaLocation="http://www.xbrl.org/2003/'
            'xbrl-instance-2003-12-31.xsd"/>\n'
            f'  <xsd:{tag_name} schemaLocation="{location}"/>',
        ),
        encoding="utf-8",
    )
    materialized = MaterializedFiling(
        entrypoint_path=entrypoint,
        taxonomy_package_paths=(schema,),
        root=tmp_path,
    )

    with pytest.raises(ValueError, match="^FINANCIAL_TAXONOMY_MISSING$"):
        ArelleXbrlProcessor().parse(materialized)


def test_arelle_rejects_recursive_schema_reference_outside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "materialized"
    root.mkdir()
    entrypoint = root / "instance.xml"
    schema = root / "test-gaap.xsd"
    outside = tmp_path / "outside.xsd"
    shutil.copyfile(FIXTURE_ROOT / "instance.xml", entrypoint)
    outside.write_text(
        '<?xml version="1.0"?>\n'
        '<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema"/>\n',
        encoding="utf-8",
    )
    schema.write_text(
        (FIXTURE_ROOT / "test-gaap.xsd")
        .read_text(encoding="utf-8")
        .replace(
            '    schemaLocation="http://www.xbrl.org/2003/'
            'xbrl-instance-2003-12-31.xsd"/>',
            '    schemaLocation="http://www.xbrl.org/2003/'
            'xbrl-instance-2003-12-31.xsd"/>\n'
            '  <xsd:include schemaLocation="../outside.xsd"/>',
        ),
        encoding="utf-8",
    )
    materialized = MaterializedFiling(
        entrypoint_path=entrypoint,
        taxonomy_package_paths=(schema,),
        root=root,
    )

    with pytest.raises(ValueError, match="^FINANCIAL_XBRL_PARSE_ERROR$"):
        ArelleXbrlProcessor().parse(materialized)


def test_arelle_local_schema_preflight_deduplicates_include_cycles(
    tmp_path: Path,
) -> None:
    entrypoint = tmp_path / "instance.xml"
    schema = tmp_path / "test-gaap.xsd"
    cycle = tmp_path / "cycle.xsd"
    shutil.copyfile(FIXTURE_ROOT / "instance.xml", entrypoint)
    schema.write_text(
        (FIXTURE_ROOT / "test-gaap.xsd")
        .read_text(encoding="utf-8")
        .replace(
            '    schemaLocation="http://www.xbrl.org/2003/'
            'xbrl-instance-2003-12-31.xsd"/>',
            '    schemaLocation="http://www.xbrl.org/2003/'
            'xbrl-instance-2003-12-31.xsd"/>\n'
            '  <xsd:include schemaLocation="cycle.xsd"/>',
        ),
        encoding="utf-8",
    )
    cycle.write_text(
        '<?xml version="1.0"?>\n'
        '<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema"\n'
        '  targetNamespace="urn:hengce:test-gaap">\n'
        '  <xsd:include schemaLocation="test-gaap.xsd"/>\n'
        "</xsd:schema>\n",
        encoding="utf-8",
    )
    materialized = MaterializedFiling(
        entrypoint_path=entrypoint,
        taxonomy_package_paths=(schema,),
        root=tmp_path,
    )

    assert len(ArelleXbrlProcessor().parse(materialized).facts) == 4


def test_arelle_rejects_recoverable_malformed_taxonomy(tmp_path: Path) -> None:
    entrypoint = tmp_path / "instance.xml"
    schema = tmp_path / "test-gaap.xsd"
    shutil.copyfile(FIXTURE_ROOT / "instance.xml", entrypoint)
    schema.write_text(
        (FIXTURE_ROOT / "test-gaap.xsd")
        .read_text(encoding="utf-8")
        .replace("</xsd:schema>", "</xsd:schem>"),
        encoding="utf-8",
    )
    materialized = MaterializedFiling(
        entrypoint_path=entrypoint,
        taxonomy_package_paths=(schema,),
        root=tmp_path,
    )

    with pytest.raises(ValueError, match="^FINANCIAL_XBRL_PARSE_ERROR$"):
        ArelleXbrlProcessor().parse(materialized)


def test_arelle_rejects_typed_dimensions_instead_of_silently_dropping_them(
    tmp_path: Path,
) -> None:
    entrypoint = tmp_path / "instance.xml"
    schema = tmp_path / "test-gaap.xsd"
    schema.write_text(
        (FIXTURE_ROOT / "test-gaap.xsd")
        .read_text(encoding="utf-8")
        .replace(
            'xmlns:t="urn:hengce:test-gaap"',
            'xmlns:t="urn:hengce:test-gaap"\n'
            '  xmlns:xbrldt="http://xbrl.org/2005/xbrldt"',
        )
        .replace(
            '    schemaLocation="http://www.xbrl.org/2003/'
            'xbrl-instance-2003-12-31.xsd"/>',
            '    schemaLocation="http://www.xbrl.org/2003/'
            'xbrl-instance-2003-12-31.xsd"/>\n'
            '  <xsd:import namespace="http://xbrl.org/2005/xbrldt"\n'
            '    schemaLocation="http://www.xbrl.org/2005/xbrldt-2005.xsd"/>',
        )
        .replace(
            '  <xsd:element name="FactTuple"',
            '  <xsd:element name="TypedDomain" id="t_TypedDomain" '
            'type="xsd:string"/>\n'
            '  <xsd:element name="TypedAxis" id="t_TypedAxis" '
            'type="xbrli:stringItemType"\n'
            '    substitutionGroup="xbrldt:dimensionItem" '
            'xbrli:periodType="duration"\n'
            '    abstract="true" xbrldt:typedDomainRef="#t_TypedDomain"/>\n'
            '  <xsd:element name="FactTuple"',
        ),
        encoding="utf-8",
    )
    entrypoint.write_text(
        (FIXTURE_ROOT / "instance.xml")
        .read_text(encoding="utf-8")
        .replace(
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
            '  xmlns:xbrldi="http://xbrl.org/2006/xbrldi"',
        )
        .replace(
            "      <xbrli:instant>2025-12-31</xbrli:instant>\n"
            "    </xbrli:period>",
            "      <xbrli:instant>2025-12-31</xbrli:instant>\n"
            "    </xbrli:period>\n"
            "    <xbrli:scenario>\n"
            '      <xbrldi:typedMember dimension="t:TypedAxis">\n'
            "        <t:TypedDomain>segment-a</t:TypedDomain>\n"
            "      </xbrldi:typedMember>\n"
            "    </xbrli:scenario>",
            1,
        ),
        encoding="utf-8",
    )
    materialized = MaterializedFiling(
        entrypoint_path=entrypoint,
        taxonomy_package_paths=(schema,),
        root=tmp_path,
    )

    with pytest.raises(ValueError, match="^FINANCIAL_XBRL_PARSE_ERROR$"):
        ArelleXbrlProcessor().parse(materialized)
