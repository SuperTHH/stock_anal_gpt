import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import hengce.financials.query as query_module
from hengce.contracts.enums import (
    ConsolidationScope,
    MappingStatus,
    QualityStatus,
    ReportType,
    StatementType,
)
from hengce.contracts.financial import FinancialFact, FinancialFiling
from hengce.financials.query import AsOfFinancialQuery
from hengce.state.financial_repository import FinancialFilingRepository
from hengce.state.repository import StateRepository
from hengce.warehouse.financial import FinancialArtifact, FinancialFactWarehouse

REPORT_PERIOD = date(2025, 12, 31)
EFFECTIVE_AT = datetime.fromisoformat("2025-12-31T23:59:59+08:00")
OLD_PUBLISHED_AT = datetime(2026, 4, 1, 8, tzinfo=UTC)
OLD_VALID_FROM = datetime(2026, 4, 2, 8, tzinfo=UTC)
NEW_PUBLISHED_AT = datetime(2026, 4, 30, 8, tzinfo=UTC)
NEW_VALID_FROM = datetime(2026, 5, 1, 8, tzinfo=UTC)


@dataclass(frozen=True)
class PreparedQuery:
    query: AsOfFinancialQuery
    repository: FinancialFilingRepository
    warehouse: FinancialFactWarehouse


def filing(
    filing_id: str,
    *,
    published_at: datetime = OLD_PUBLISHED_AT,
    valid_from: datetime = OLD_VALID_FROM,
    quality_status: QualityStatus = QualityStatus.VALID,
    supersedes_id: str | None = None,
    ts_code: str = "699999.SH",
    report_period: date = REPORT_PERIOD,
) -> FinancialFiling:
    raw_hash = ("1" if filing_id == "old" else "2") * 64
    return FinancialFiling(
        record_id=filing_id,
        filing_id=filing_id,
        source_id="sse",
        source_url=f"https://www.sse.com.cn/disclosure/{filing_id}.xml",
        published_at=published_at,
        effective_at=published_at,
        collected_at=valid_from,
        version="filing-v1:parser-v1:mapping-v1",
        content_hash=raw_hash,
        license_policy="sse-personal-research",
        quality_status=quality_status,
        valid_from=valid_from,
        ts_code=ts_code,
        exchange="SSE",
        report_period=report_period,
        report_type=ReportType.ANNUAL,
        announcement_at=published_at,
        taxonomy=("test-gaap-2025",),
        taxonomy_hashes=(raw_hash,),
        raw_object_hash=raw_hash,
        filing_version="filing-v1",
        parser_name="fixture-parser",
        parser_version="parser-v1",
        mapping_version="mapping-v1",
        fact_count=1,
        conflict_count=0,
        is_restated=supersedes_id is not None,
        supersedes_id=supersedes_id,
    )


def fact(
    owner: FinancialFiling,
    *,
    fact_id: str,
    canonical_name: str | None,
    value: str,
    mapping_status: MappingStatus = MappingStatus.MAPPED,
    quality_status: QualityStatus = QualityStatus.VALID,
) -> FinancialFact:
    return FinancialFact(
        record_id=fact_id,
        fact_id=fact_id,
        source_id="sse",
        source_url=owner.source_url,
        published_at=owner.published_at,
        effective_at=EFFECTIVE_AT,
        collected_at=owner.collected_at,
        version=owner.version,
        content_hash="a" * 64,
        license_policy=owner.license_policy,
        quality_status=quality_status,
        valid_from=owner.valid_from,
        ts_code=owner.ts_code,
        report_period=owner.report_period,
        report_type=owner.report_type,
        announcement_at=owner.announcement_at,
        statement_type=StatementType.BALANCE_SHEET,
        taxonomy=owner.taxonomy,
        fact_name=fact_id,
        raw_qname=f"{{urn:hengce:test}}{fact_id}",
        canonical_fact_name=canonical_name,
        mapping_status=mapping_status,
        fact_value=Decimal(value),
        unit="CNY",
        currency="CNY",
        filing_id=owner.filing_id,
        context_signature=f"context-{fact_id}",
        entity_scheme="https://example.test/entity",
        entity_identifier=owner.ts_code,
        period_start=None,
        period_end=None,
        instant=owner.report_period,
        unit_signature="CNY",
        decimals="0",
        consolidation_scope=ConsolidationScope.CONSOLIDATED,
        dimensions={},
        fact_identity_hash="b" * 64,
        comparison_identity_hash="c" * 64,
    )


def prepared_query(tmp_path: Path) -> PreparedQuery:
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()
    repository = FinancialFilingRepository(state.path)
    warehouse = FinancialFactWarehouse(tmp_path / "warehouse")
    return PreparedQuery(
        query=AsOfFinancialQuery(repository=repository, warehouse_root=warehouse.root),
        repository=repository,
        warehouse=warehouse,
    )


def register_artifact(
    prepared: PreparedQuery,
    owner: FinancialFiling,
    facts: list[FinancialFact],
    *,
    publish: bool = True,
    warehouse: FinancialFactWarehouse | None = None,
    manifest_path: str | None = None,
) -> FinancialArtifact:
    target_warehouse = warehouse or prepared.warehouse
    artifact = target_warehouse.write_facts(owner, facts)
    registered_path = str(artifact.path) if manifest_path is None else manifest_path
    prepared.repository.stage_filing(
        owner,
        registered_path,
        artifact.content_hash,
        artifact.fact_count,
    )
    if publish:
        assert prepared.repository.publish_filing(
            owner.filing_id,
            registered_path,
            artifact.content_hash,
            artifact.fact_count,
        )
    return artifact


def prepared_old_and_corrected_query(
    tmp_path: Path,
    *,
    correction_quality: QualityStatus = QualityStatus.VALID,
    publish_correction: bool = True,
) -> tuple[PreparedQuery, FinancialFiling, FinancialFiling]:
    prepared = prepared_query(tmp_path)
    old = filing("old")
    new = filing(
        "new",
        published_at=NEW_PUBLISHED_AT,
        valid_from=NEW_VALID_FROM,
        quality_status=correction_quality,
        supersedes_id=old.filing_id,
    )
    register_artifact(
        prepared,
        old,
        [fact(old, fact_id="old-assets", canonical_name="assets", value="1000")],
    )
    register_artifact(
        prepared,
        new,
        [fact(new, fact_id="new-assets", canonical_name="assets", value="1100")],
        publish=publish_correction,
    )
    return prepared, old, new


def assert_graph_blocked_without_duckdb(
    prepared: PreparedQuery,
    monkeypatch: pytest.MonkeyPatch,
    *,
    as_of: datetime,
    known_at: datetime,
) -> None:
    real_connect = query_module.duckdb.connect
    connect_calls = 0

    def tracking_connect(*args: object, **kwargs: object) -> object:
        nonlocal connect_calls
        connect_calls += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(query_module.duckdb, "connect", tracking_connect)
    result = prepared.query.query_financial_facts(
        ts_code="699999.SH",
        report_period=REPORT_PERIOD,
        canonical_fact_names=frozenset({"assets"}),
        as_of=as_of,
        known_at=known_at,
    )

    assert connect_calls == 0
    assert result.facts == ()
    assert result.blocked_reasons == ("FINANCIAL_RESTATEMENT_UNUSABLE",)


@pytest.mark.parametrize(
    ("as_of", "known_at"),
    [
        (datetime(2026, 4, 30), NEW_VALID_FROM),
        (NEW_PUBLISHED_AT, datetime(2026, 5, 1)),
    ],
)
def test_query_rejects_each_naive_cutoff(
    tmp_path: Path,
    as_of: datetime,
    known_at: datetime,
) -> None:
    """Catches validating only one cutoff or silently treating a naive cutoff as local time."""
    prepared = prepared_query(tmp_path)

    with pytest.raises(ValueError, match="^FINANCIAL_QUERY_CUTOFF_INVALID$"):
        prepared.query.query_financial_facts(
            ts_code="699999.SH",
            report_period=REPORT_PERIOD,
            canonical_fact_names=frozenset({"assets"}),
            as_of=as_of,
            known_at=known_at,
        )


def test_empty_names_validate_cutoffs_before_skipping_duckdb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an empty-name early return before public input validation or a needless DB open."""
    prepared = prepared_query(tmp_path)

    with pytest.raises(ValueError, match="^FINANCIAL_QUERY_CUTOFF_INVALID$"):
        prepared.query.query_financial_facts(
            ts_code="699999.SH",
            report_period=REPORT_PERIOD,
            canonical_fact_names=frozenset(),
            as_of=datetime(2026, 4, 30),
            known_at=NEW_VALID_FROM,
        )

    def fail_connect(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("DuckDB must not be opened for an empty name set")

    monkeypatch.setattr(query_module.duckdb, "connect", fail_connect)
    result = prepared.query.query_financial_facts(
        ts_code="699999.SH",
        report_period=REPORT_PERIOD,
        canonical_fact_names=frozenset(),
        as_of=NEW_PUBLISHED_AT,
        known_at=NEW_VALID_FROM,
    )

    assert result.facts == ()
    assert result.blocked_reasons == ()
    assert result.filing_ids == ()


def test_public_and_system_cutoffs_have_no_required_relative_order(tmp_path: Path) -> None:
    """Catches imposing either as_of <= known_at or known_at <= as_of."""
    prepared = prepared_query(tmp_path)
    owner = filing("old", published_at=OLD_PUBLISHED_AT, valid_from=OLD_PUBLISHED_AT)
    register_artifact(
        prepared,
        owner,
        [fact(owner, fact_id="assets", canonical_name="assets", value="1000")],
    )

    public_later = prepared.query.query_financial_facts(
        ts_code=owner.ts_code,
        report_period=owner.report_period,
        canonical_fact_names=frozenset({"assets"}),
        as_of=datetime(2026, 5, 2, tzinfo=UTC),
        known_at=datetime(2026, 4, 2, tzinfo=UTC),
    )
    system_later = prepared.query.query_financial_facts(
        ts_code=owner.ts_code,
        report_period=owner.report_period,
        canonical_fact_names=frozenset({"assets"}),
        as_of=datetime(2026, 4, 2, tzinfo=UTC),
        known_at=datetime(2026, 5, 2, tzinfo=UTC),
    )

    assert public_later.facts[0]["fact_value"] == Decimal("1000")
    assert system_later.facts[0]["fact_value"] == Decimal("1000")


def test_correction_is_invisible_before_publication_and_visible_after(tmp_path: Path) -> None:
    """Catches choosing by system time alone and exposing a correction before publication."""
    prepared, _old, new = prepared_old_and_corrected_query(tmp_path)

    before = prepared.query.query_financial_facts(
        ts_code=new.ts_code,
        report_period=new.report_period,
        canonical_fact_names=frozenset({"assets"}),
        as_of=new.published_at - timedelta(seconds=1),
        known_at=new.valid_from,
    )
    after = prepared.query.query_financial_facts(
        ts_code=new.ts_code,
        report_period=new.report_period,
        canonical_fact_names=frozenset({"assets"}),
        as_of=new.published_at,
        known_at=new.valid_from,
    )

    assert before.facts[0]["fact_value"] == Decimal("1000")
    assert after.facts[0]["fact_value"] == Decimal("1100")
    assert before.filing_ids == ("old",)
    assert after.filing_ids == ("new",)


def test_public_correction_is_invisible_until_known_by_the_system(tmp_path: Path) -> None:
    """Catches choosing by publication alone and ignoring the valid_from system axis."""
    prepared, _old, new = prepared_old_and_corrected_query(tmp_path)

    before = prepared.query.query_financial_facts(
        ts_code=new.ts_code,
        report_period=new.report_period,
        canonical_fact_names=frozenset({"assets"}),
        as_of=new.published_at,
        known_at=new.valid_from - timedelta(seconds=1),
    )
    after = prepared.query.query_financial_facts(
        ts_code=new.ts_code,
        report_period=new.report_period,
        canonical_fact_names=frozenset({"assets"}),
        as_of=new.published_at,
        known_at=new.valid_from,
    )

    assert before.facts[0]["fact_value"] == Decimal("1000")
    assert after.facts[0]["fact_value"] == Decimal("1100")


def test_public_unpublished_correction_blocks_instead_of_falling_back(tmp_path: Path) -> None:
    """Catches filtering PENDING manifests before chain selection and returning the old value."""
    prepared, _old, correction = prepared_old_and_corrected_query(
        tmp_path,
        publish_correction=False,
    )

    result = prepared.query.query_financial_facts(
        ts_code=correction.ts_code,
        report_period=correction.report_period,
        canonical_fact_names=frozenset({"assets"}),
        as_of=correction.published_at,
        known_at=correction.valid_from,
    )

    assert result.facts == ()
    assert result.blocked_reasons == ("FINANCIAL_RESTATEMENT_UNUSABLE",)


@pytest.mark.parametrize(
    "quality_status",
    [QualityStatus.CONFLICT, QualityStatus.REJECTED, QualityStatus.UNVERIFIED],
)
def test_public_bad_quality_correction_blocks_instead_of_falling_back(
    tmp_path: Path,
    quality_status: QualityStatus,
) -> None:
    """Catches treating a published but unusable latest correction as skippable."""
    prepared, _old, correction = prepared_old_and_corrected_query(
        tmp_path,
        correction_quality=quality_status,
    )

    result = prepared.query.query_financial_facts(
        ts_code=correction.ts_code,
        report_period=correction.report_period,
        canonical_fact_names=frozenset({"assets"}),
        as_of=correction.published_at,
        known_at=correction.valid_from,
    )

    assert result.facts == ()
    assert result.blocked_reasons == ("FINANCIAL_RESTATEMENT_UNUSABLE",)


def test_self_referencing_visible_filing_graph_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches treating a self-loop as an empty selected set and silently returning no data."""
    prepared = prepared_query(tmp_path)
    owner = filing("self-loop", supersedes_id="self-loop")
    register_artifact(
        prepared,
        owner,
        [fact(owner, fact_id="assets", canonical_name="assets", value="1000")],
    )

    assert_graph_blocked_without_duckdb(
        prepared,
        monkeypatch,
        as_of=owner.published_at,
        known_at=owner.valid_from,
    )


def test_two_node_visible_cycle_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a rootless two-node cycle collapsing to an apparently valid empty result."""
    prepared = prepared_query(tmp_path)
    first = filing("cycle-a")
    second = filing(
        "cycle-b",
        published_at=NEW_PUBLISHED_AT,
        valid_from=NEW_VALID_FROM,
        supersedes_id=first.filing_id,
    )
    register_artifact(
        prepared,
        first,
        [fact(first, fact_id="cycle-a-assets", canonical_name="assets", value="1000")],
    )
    register_artifact(
        prepared,
        second,
        [fact(second, fact_id="cycle-b-assets", canonical_name="assets", value="1100")],
    )
    cyclic_first = first.model_copy(update={"supersedes_id": second.filing_id, "is_restated": True})
    with sqlite3.connect(prepared.repository.path) as connection:
        connection.execute(
            """
            UPDATE financial_filings
            SET supersedes_id=?, payload_json=?
            WHERE filing_id=?
            """,
            (
                second.filing_id,
                cyclic_first.model_dump_json(),
                first.filing_id,
            ),
        )

    assert_graph_blocked_without_duckdb(
        prepared,
        monkeypatch,
        as_of=second.published_at,
        known_at=second.valid_from,
    )


@pytest.mark.parametrize("same_leaf_time", [False, True], ids=["different-time", "same-time"])
def test_visible_forked_corrections_block_without_opening_duckdb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    same_leaf_time: bool,
) -> None:
    """Catches sorting or combining two correction leaves instead of rejecting the fork."""
    prepared = prepared_query(tmp_path)
    parent = filing("fork-parent")
    left = filing(
        "fork-left",
        published_at=NEW_PUBLISHED_AT,
        valid_from=NEW_VALID_FROM,
        supersedes_id=parent.filing_id,
    )
    right = filing(
        "fork-right",
        published_at=(NEW_PUBLISHED_AT if same_leaf_time else NEW_PUBLISHED_AT + timedelta(days=1)),
        valid_from=(NEW_VALID_FROM if same_leaf_time else NEW_VALID_FROM + timedelta(days=1)),
        supersedes_id=parent.filing_id,
    )
    for owner, value in ((parent, "1000"), (left, "1100"), (right, "1200")):
        register_artifact(
            prepared,
            owner,
            [
                fact(
                    owner,
                    fact_id=f"{owner.filing_id}-assets",
                    canonical_name="assets",
                    value=value,
                )
            ],
        )

    assert_graph_blocked_without_duckdb(
        prepared,
        monkeypatch,
        as_of=max(left.published_at, right.published_at),
        known_at=max(left.valid_from, right.valid_from),
    )


def test_multiple_visible_root_filings_block_without_opening_duckdb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches mixing independent roots merely because neither is superseded."""
    prepared = prepared_query(tmp_path)
    for filing_id, value in (("root-a", "1000"), ("root-b", "2000")):
        owner = filing(filing_id)
        register_artifact(
            prepared,
            owner,
            [
                fact(
                    owner,
                    fact_id=f"{filing_id}-assets",
                    canonical_name="assets",
                    value=value,
                )
            ],
        )

    assert_graph_blocked_without_duckdb(
        prepared,
        monkeypatch,
        as_of=OLD_PUBLISHED_AT,
        known_at=OLD_VALID_FROM,
    )


def test_visible_correction_with_time_filtered_parent_blocks_without_duckdb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches accepting a child whose parent is absent from the bitemporal visible graph."""
    prepared = prepared_query(tmp_path)
    parent = filing(
        "future-known-parent",
        published_at=OLD_PUBLISHED_AT,
        valid_from=NEW_VALID_FROM + timedelta(days=10),
    )
    child = filing(
        "visible-child",
        published_at=NEW_PUBLISHED_AT,
        valid_from=NEW_VALID_FROM,
        supersedes_id=parent.filing_id,
    )
    register_artifact(
        prepared,
        parent,
        [fact(parent, fact_id="parent-assets", canonical_name="assets", value="1000")],
    )
    register_artifact(
        prepared,
        child,
        [fact(child, fact_id="child-assets", canonical_name="assets", value="1100")],
    )

    assert_graph_blocked_without_duckdb(
        prepared,
        monkeypatch,
        as_of=child.published_at,
        known_at=child.valid_from,
    )


@pytest.mark.parametrize(
    "parent_identity",
    [
        {"ts_code": "000001.SZ"},
        {"report_period": date(2024, 12, 31)},
    ],
    ids=["cross-ts-code", "cross-report-period"],
)
def test_visible_correction_with_cross_identity_parent_blocks_without_duckdb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_identity: dict[str, object],
) -> None:
    """Catches accepting a parent reference outside the requested filing identity."""
    prepared = prepared_query(tmp_path)
    parent = filing("foreign-parent", **parent_identity)
    prepared.repository.stage_filing(parent, "unused.parquet", "d" * 64, 0)
    child = filing(
        "visible-child",
        published_at=NEW_PUBLISHED_AT,
        valid_from=NEW_VALID_FROM,
        supersedes_id=parent.filing_id,
    )
    register_artifact(
        prepared,
        child,
        [fact(child, fact_id="child-assets", canonical_name="assets", value="1100")],
    )

    assert_graph_blocked_without_duckdb(
        prepared,
        monkeypatch,
        as_of=child.published_at,
        known_at=child.valid_from,
    )


def test_unpublished_original_manifest_is_not_read(tmp_path: Path) -> None:
    """Catches reading expected paths without requiring manifest publication."""
    prepared = prepared_query(tmp_path)
    owner = filing("old")
    register_artifact(
        prepared,
        owner,
        [fact(owner, fact_id="assets", canonical_name="assets", value="1000")],
        publish=False,
    )

    result = prepared.query.query_financial_facts(
        ts_code=owner.ts_code,
        report_period=owner.report_period,
        canonical_fact_names=frozenset({"assets"}),
        as_of=owner.published_at,
        known_at=owner.valid_from,
    )

    assert result.facts == ()
    assert result.blocked_reasons == ()
    assert result.filing_ids == ()


def test_only_mapped_valid_facts_are_returned(tmp_path: Path) -> None:
    """Catches leaking UNMAPPED or CONFLICT rows from a valid filing artifact."""
    prepared = prepared_query(tmp_path)
    owner = filing("old")
    register_artifact(
        prepared,
        owner,
        [
            fact(owner, fact_id="assets", canonical_name="assets", value="1000"),
            fact(
                owner,
                fact_id="raw-unknown",
                canonical_name=None,
                value="12",
                mapping_status=MappingStatus.UNMAPPED,
            ),
            fact(
                owner,
                fact_id="equity-conflict",
                canonical_name="equity",
                value="600",
                quality_status=QualityStatus.CONFLICT,
            ),
        ],
    )

    result = prepared.query.query_financial_facts(
        ts_code=owner.ts_code,
        report_period=owner.report_period,
        canonical_fact_names=frozenset({"assets", "equity", "raw-unknown"}),
        as_of=owner.published_at,
        known_at=owner.valid_from,
    )

    assert [row["fact_id"] for row in result.facts] == ["assets"]


def test_missing_and_sql_shaped_canonical_names_return_no_rows(tmp_path: Path) -> None:
    """Catches interpolating a requested canonical name into SQL instead of binding it."""
    prepared = prepared_query(tmp_path)
    owner = filing("old")
    register_artifact(
        prepared,
        owner,
        [fact(owner, fact_id="assets", canonical_name="assets", value="1000")],
    )

    result = prepared.query.query_financial_facts(
        ts_code=owner.ts_code,
        report_period=owner.report_period,
        canonical_fact_names=frozenset({"liabilities') OR TRUE --"}),
        as_of=owner.published_at,
        known_at=owner.valid_from,
    )

    assert result.facts == ()
    assert result.blocked_reasons == ()
    assert result.filing_ids == ("old",)


def test_unregistered_parquet_below_warehouse_root_is_never_discovered(tmp_path: Path) -> None:
    """Catches globbing the warehouse instead of passing explicit manifest paths."""
    prepared = prepared_query(tmp_path)
    registered = filing("old")
    register_artifact(
        prepared,
        registered,
        [fact(registered, fact_id="assets", canonical_name="assets", value="1000")],
    )
    rogue = filing("rogue")
    prepared.warehouse.write_facts(
        rogue,
        [fact(rogue, fact_id="rogue-equity", canonical_name="equity", value="999")],
    )

    result = prepared.query.query_financial_facts(
        ts_code=registered.ts_code,
        report_period=registered.report_period,
        canonical_fact_names=frozenset({"equity"}),
        as_of=registered.published_at,
        known_at=registered.valid_from,
    )

    assert result.facts == ()
    assert result.filing_ids == ("old",)


def test_cwd_relative_root_prefixed_manifest_path_is_resolved_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches prepending warehouse_root to a producer path that already includes that root."""
    monkeypatch.chdir(tmp_path)
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()
    repository = FinancialFilingRepository(state.path)
    warehouse = FinancialFactWarehouse(Path("data") / "warehouse")
    prepared = PreparedQuery(
        query=AsOfFinancialQuery(repository=repository, warehouse_root=warehouse.root),
        repository=repository,
        warehouse=warehouse,
    )
    owner = filing("old")
    artifact = register_artifact(
        prepared,
        owner,
        [fact(owner, fact_id="assets", canonical_name="assets", value="1000")],
    )

    assert not artifact.path.is_absolute()
    result = prepared.query.query_financial_facts(
        ts_code=owner.ts_code,
        report_period=owner.report_period,
        canonical_fact_names=frozenset({"assets"}),
        as_of=owner.published_at,
        known_at=owner.valid_from,
    )

    assert result.facts[0]["fact_value"] == Decimal("1000")


def test_manifest_path_relative_to_warehouse_root_is_supported(tmp_path: Path) -> None:
    """Catches resolving every relative manifest path only against the process CWD."""
    prepared = prepared_query(tmp_path)
    owner = filing("old")
    artifact = prepared.warehouse.write_facts(
        owner,
        [fact(owner, fact_id="assets", canonical_name="assets", value="1000")],
    )
    relative_path = artifact.path.relative_to(prepared.warehouse.root)
    prepared.repository.stage_filing(
        owner,
        str(relative_path),
        artifact.content_hash,
        artifact.fact_count,
    )
    assert prepared.repository.publish_filing(
        owner.filing_id,
        str(relative_path),
        artifact.content_hash,
        artifact.fact_count,
    )

    result = prepared.query.query_financial_facts(
        ts_code=owner.ts_code,
        report_period=owner.report_period,
        canonical_fact_names=frozenset({"assets"}),
        as_of=owner.published_at,
        known_at=owner.valid_from,
    )

    assert result.facts[0]["fact_value"] == Decimal("1000")


def test_published_manifest_path_must_resolve_below_warehouse_root(tmp_path: Path) -> None:
    """Catches trusting a registered absolute path that escapes the configured root."""
    prepared = prepared_query(tmp_path)
    owner = filing("old")
    outside_warehouse = FinancialFactWarehouse(tmp_path / "outside")
    register_artifact(
        prepared,
        owner,
        [fact(owner, fact_id="assets", canonical_name="assets", value="1000")],
        warehouse=outside_warehouse,
    )

    with pytest.raises(ValueError, match="^FINANCIAL_QUERY_PATH_INVALID$"):
        prepared.query.query_financial_facts(
            ts_code=owner.ts_code,
            report_period=owner.report_period,
            canonical_fact_names=frozenset({"assets"}),
            as_of=owner.published_at,
            known_at=owner.valid_from,
        )
