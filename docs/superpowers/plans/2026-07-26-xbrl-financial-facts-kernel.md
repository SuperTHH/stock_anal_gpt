# XBRL Financial Facts Kernel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an offline, immutable, point-in-time XBRL financial-facts kernel that safely parses approved local filings, versions corrections, stores normalized facts, and never leaks unusable facts into strategy inputs.

**Architecture:** Approved local XBRL and taxonomy objects enter through the existing Policy Guard and Raw Object Store. A network-disabled Arelle adapter produces neutral raw facts; focused normalization, quality, SQLite, Parquet, and query components convert them into auditable bitemporal facts. The first slice has manual import and fixtures only—live discovery, PDF fallback, corporate actions, derived ratios, and strategy output remain blocked.

**Tech Stack:** Python 3.12, Pydantic 2, Arelle `>=2.42.1,<2.43`, SQLite, PyArrow/Parquet, DuckDB, Typer, pytest, Ruff.

**Dependency evidence (reviewed 2026-07-26):**
- [Arelle PyPI release](https://pypi.org/project/arelle-release/) for the pinned `2.42.1` line.
- [Arelle supported Python API](https://arelle.readthedocs.io/en/latest/python_api/python_api.html) for `Session`, `RuntimeOptions`, and the one-session-per-process thread-safety boundary.

## Global Constraints

- Follow `docs/superpowers/specs/2026-07-26-xbrl-financial-facts-kernel-design.md` exactly.
- Arelle must run with `internetConnectivity="offline"`; any socket connection in parser tests is a failure.
- Serialize Arelle `Session` use inside a process; do not run two sessions concurrently in threads.
- Do not add an SSE, SZSE, CNInfo, Ministry of Finance, or other live discovery connector.
- Do not call hidden endpoints, bypass access controls, or add a source to the approved whitelist.
- An XBRL instance, taxonomy object, and provenance must exist in `RawObjectStore` before parsing.
- XML DTDs, external entities, unsafe ZIP paths, symlinks, excessive sizes, and excessive compression ratios are rejected.
- Normalize only non-nil numeric facts; non-numeric facts remain only in raw storage and parser diagnostics.
- Never invent a production QName mapping. Tests inject mappings for the explicit `urn:hengce:test-gaap` fixture namespace.
- `record_id == fact_id`, `announcement_at == published_at`, and `content_hash == raw_object_hash`.
- `as_of` and `known_at` are mandatory timezone-aware parameters; query APIs have no implicit `now()`.
- Publicly known unusable corrections block affected queries; do not fall back to a superseded old value.
- SQLite stores transactional manifests; Parquet stores immutable facts; DuckDB reads only published manifest paths.
- Tests use only fictional issuers, fixture codes, fixture values, local files, injected clocks, and blocked sockets.
- Existing M1 tests must remain green after every task; no candidate list or trading instruction is generated.
- Use PowerShell commands on Windows and set `$env:PYTHONPATH='src'` for focused tests.

---

## File Structure

### Create

- `src/hengce/contracts/financial.py`: immutable public data contracts for descriptors, taxonomies, filings, facts, and conflicts.
- `src/hengce/financials/__init__.py`: focused financial-kernel package exports.
- `src/hengce/financials/package.py`: local attachment inspection, safe ZIP validation, and temporary materialization.
- `src/hengce/financials/xbrl.py`: Arelle boundary and neutral raw XBRL types.
- `src/hengce/financials/mapping.py`: injected versioned QName mapping and fact normalization.
- `src/hengce/financials/quality.py`: duplicate, conflict, completeness, and accounting-equation validation.
- `src/hengce/financials/query.py`: bitemporal manifest selection and DuckDB fact query.
- `src/hengce/state/financial_repository.py`: financial taxonomy, filing, artifact, and conflict transactions.
- `src/hengce/state/migrations/006_financial_filings.sql`: financial state schema.
- `src/hengce/warehouse/financial.py`: content-addressed financial-fact Parquet artifacts.
- `src/hengce/services/financial_ingestion.py`: end-to-end idempotent financial filing orchestration.
- `tests/fixtures/xbrl/minimal/test-gaap.xsd`: fictional numeric taxonomy.
- `tests/fixtures/xbrl/minimal/instance.xml`: fictional valid filing.
- `tests/unit/contracts/test_financial_models.py`
- `tests/unit/financials/test_package.py`
- `tests/unit/financials/test_xbrl.py`
- `tests/unit/financials/test_mapping.py`
- `tests/unit/financials/test_quality.py`
- `tests/unit/state/test_financial_repository.py`
- `tests/unit/warehouse/test_financial.py`
- `tests/unit/financials/test_query.py`
- `tests/unit/services/test_financial_ingestion.py`
- `tests/integration/test_m2_xbrl_acceptance.py`
- `docs/runbooks/xbrl-financial-facts.md`

### Modify

- `pyproject.toml`: add the upper-bounded Arelle runtime dependency.
- `src/hengce/contracts/enums.py`: add financial enums.
- `src/hengce/contracts/__init__.py`: export financial contracts.
- `src/hengce/cli.py`: compose the kernel and add two local-only commands.
- `tests/unit/test_cli.py`: verify manual taxonomy registration/import and policy refusal.
- `docs/superpowers/plans/2026-07-24-a-share-research-master-roadmap.md`: link this M2a child plan without marking full M2 complete.

---

### Task 1: Freeze Financial Contracts and Enums

**Files:**
- Create: `src/hengce/contracts/financial.py`
- Modify: `src/hengce/contracts/enums.py`
- Modify: `src/hengce/contracts/__init__.py`
- Create: `tests/unit/contracts/test_financial_models.py`

**Interfaces:**
- Produces enums: `ReportType`, `StatementType`, `MappingStatus`, `ConsolidationScope`, `DiscoveryMethod`, `ConflictResolutionStatus`.
- Produces models: `FilingDescriptor`, `TaxonomyPackageRef`, `FinancialFiling`, `FinancialFact`, `FactConflict`.
- All models use `ConfigDict(extra="forbid")`.
- `FinancialFiling` and `FinancialFact` inherit `FactBase`.

- [ ] **Step 1: Prepare the isolated Python test runner**

From the XBRL worktree:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e ".[dev]"
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest -q
```

Expected: the existing 189 tests pass before contract work begins.

- [ ] **Step 2: Write failing contract tests**

Create `tests/unit/contracts/test_financial_models.py` with fixed timezone-aware helpers and these tests:

```python
from datetime import UTC, date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from hengce.contracts.enums import (
    ConsolidationScope,
    DiscoveryMethod,
    MappingStatus,
    QualityStatus,
    ReportType,
    StatementType,
)
from hengce.contracts.financial import FilingDescriptor, FinancialFact


NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)
HASH = "a" * 64
SHANGHAI = ZoneInfo("Asia/Shanghai")


def descriptor() -> FilingDescriptor:
    return FilingDescriptor(
        source_id="sse",
        source_url="https://www.sse.com.cn/disclosure/test.xml",
        ts_code="600001.SH",
        exchange="SSE",
        report_period=date(2025, 12, 31),
        report_type=ReportType.ANNUAL,
        published_at=NOW,
        collected_at=NOW,
        attachment_name="instance.xml",
        content_type="application/xml",
        raw_object_hash=HASH,
        taxonomy_refs=("test-gaap-2025",),
        discovery_method=DiscoveryMethod.FIXTURE,
        instance_entrypoint=None,
    )


def financial_fact_payload() -> dict[str, object]:
    return {
        "record_id": "fact-1",
        "fact_id": "fact-1",
        "source_id": "sse",
        "source_url": "https://www.sse.com.cn/disclosure/test.xml",
        "published_at": NOW,
        "announcement_at": NOW,
        "effective_at": datetime(2025, 12, 31, 15, 59, 59, tzinfo=UTC),
        "collected_at": NOW,
        "version": "filing-v1:parser-v1:mapping-v1",
        "content_hash": HASH,
        "license_policy": "sse-personal-research",
        "quality_status": QualityStatus.VALID,
        "valid_from": NOW,
        "ts_code": "600001.SH",
        "report_period": date(2025, 12, 31),
        "report_type": ReportType.ANNUAL,
        "statement_type": StatementType.BALANCE_SHEET,
        "taxonomy": ("test-gaap-2025",),
        "fact_name": "Assets",
        "raw_qname": "{urn:hengce:test-gaap}Assets",
        "canonical_fact_name": "assets",
        "mapping_status": MappingStatus.MAPPED,
        "fact_value": Decimal("1000"),
        "unit": "iso4217:CNY",
        "currency": "CNY",
        "filing_id": "filing-1",
        "context_signature": "context-hash",
        "entity_identifier": "600001.SH",
        "period_start": None,
        "period_end": None,
        "instant": date(2025, 12, 31),
        "unit_signature": "unit-hash",
        "decimals": "-2",
        "consolidation_scope": ConsolidationScope.CONSOLIDATED,
        "dimensions": {},
        "fact_identity_hash": "b" * 64,
        "comparison_identity_hash": "c" * 64,
    }


def test_descriptor_requires_timezone_and_supported_exchange() -> None:
    assert descriptor().published_at.tzinfo is UTC
    with pytest.raises(ValidationError):
        FilingDescriptor.model_validate(
            {
                **descriptor().model_dump(),
                "published_at": datetime(2026, 7, 26, 12),
            }
        )
    with pytest.raises(ValidationError):
        FilingDescriptor.model_validate({**descriptor().model_dump(), "exchange": "BSE"})


def test_financial_fact_enforces_identity_and_period_shape() -> None:
    payload = financial_fact_payload()
    assert FinancialFact.model_validate(payload).record_id == "fact-1"
    with pytest.raises(ValidationError):
        FinancialFact.model_validate({**payload, "fact_id": "other"})
    with pytest.raises(ValidationError):
        FinancialFact.model_validate(
            {**payload, "period_start": date(2025, 1, 1)}
        )


def test_unmapped_fact_cannot_claim_canonical_name() -> None:
    with pytest.raises(ValidationError):
        FinancialFact.model_validate(
            {
                **financial_fact_payload(),
                "mapping_status": MappingStatus.UNMAPPED,
                "canonical_fact_name": "assets",
            }
        )


def test_financial_fact_effective_time_is_report_period_end_in_shanghai() -> None:
    fact = FinancialFact.model_validate(financial_fact_payload())
    assert fact.effective_at.astimezone(SHANGHAI) == datetime.combine(
        fact.report_period,
        time(23, 59, 59),
        SHANGHAI,
    )
    with pytest.raises(ValidationError):
        FinancialFact.model_validate(
            {
                **financial_fact_payload(),
                "effective_at": datetime(2025, 12, 31, 15, 59, 58, tzinfo=UTC),
            }
        )
```

- [ ] **Step 3: Run the contract tests and verify RED**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/contracts/test_financial_models.py -q
```

Expected: collection fails because the financial enums and models do not exist.

- [ ] **Step 4: Implement the contracts**

Add these exact enum values:

```python
class ReportType(StrEnum):
    ANNUAL = "ANNUAL"
    Q1 = "Q1"
    HALF_YEAR = "HALF_YEAR"
    Q3 = "Q3"


class StatementType(StrEnum):
    BALANCE_SHEET = "BALANCE_SHEET"
    INCOME_STATEMENT = "INCOME_STATEMENT"
    CASH_FLOW = "CASH_FLOW"
    OTHER = "OTHER"


class MappingStatus(StrEnum):
    MAPPED = "MAPPED"
    UNMAPPED = "UNMAPPED"


class ConsolidationScope(StrEnum):
    CONSOLIDATED = "CONSOLIDATED"
    PARENT = "PARENT"
    UNKNOWN = "UNKNOWN"


class DiscoveryMethod(StrEnum):
    FIXTURE = "FIXTURE"
    MANUAL_IMPORT = "MANUAL_IMPORT"


class ConflictResolutionStatus(StrEnum):
    OPEN = "OPEN"
    SUPERSEDED = "SUPERSEDED"
```

Implement timezone validation with one shared helper in
`src/hengce/contracts/financial.py`:

```python
def require_aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value
```

Use `model_validator(mode="after")` to enforce:

```python
if isinstance(self, FinancialFact) and self.record_id != self.fact_id:
    raise ValueError("record_id must equal fact_id")
if isinstance(self, FinancialFiling) and self.record_id != self.filing_id:
    raise ValueError("record_id must equal filing_id")
if self.published_at is None:
    raise ValueError("published_at is required")
if self.announcement_at != self.published_at:
    raise ValueError("announcement_at must equal published_at")
is_duration = self.period_start is not None or self.period_end is not None
if is_duration == (self.instant is not None):
    raise ValueError("fact period must be exactly duration or instant")
if is_duration and (self.period_start is None or self.period_end is None):
    raise ValueError("duration facts require both period bounds")
if self.mapping_status is MappingStatus.UNMAPPED and self.canonical_fact_name is not None:
    raise ValueError("unmapped facts cannot claim a canonical name")
expected_effective = datetime.combine(
    self.report_period,
    time(23, 59, 59),
    ZoneInfo("Asia/Shanghai"),
)
if self.effective_at is None or self.effective_at.astimezone(UTC) != expected_effective.astimezone(UTC):
    raise ValueError("effective_at must be report-period end in Asia/Shanghai")
```

Restrict `exchange` to `Literal["SSE", "SZSE"]`, hashes to lowercase SHA-256,
and tuple fields to immutable tuples. Apply `require_aware` to descriptor,
taxonomy, filing, and fact timestamps (`published_at`, `announcement_at`,
`collected_at`, `valid_from`, `effective_at`, and `detected_at` where present).
Export the new public models from `contracts/__init__.py`.

Freeze the public field sets in the same implementation:

```python
class FilingDescriptor(BaseModel):
    source_id: str
    source_url: AnyHttpUrl
    ts_code: str
    exchange: Literal["SSE", "SZSE"]
    report_period: date
    report_type: ReportType
    published_at: datetime
    collected_at: datetime
    attachment_name: str
    content_type: str
    raw_object_hash: str
    taxonomy_refs: tuple[str, ...]
    discovery_method: DiscoveryMethod
    instance_entrypoint: str | None


class TaxonomyPackageRef(BaseModel):
    taxonomy_id: str
    source_id: str
    source_url: AnyHttpUrl
    raw_object_hash: str
    package_name: str
    entrypoint: str
    content_type: str
    collected_at: datetime


class FinancialFiling(FactBase):
    filing_id: str
    ts_code: str
    exchange: Literal["SSE", "SZSE"]
    report_period: date
    report_type: ReportType
    announcement_at: datetime
    taxonomy: tuple[str, ...]
    taxonomy_hashes: tuple[str, ...]
    raw_object_hash: str
    filing_version: str
    parser_name: str
    parser_version: str
    mapping_version: str
    fact_count: int
    conflict_count: int
    is_restated: bool
    supersedes_id: str | None


class FinancialFact(FactBase):
    fact_id: str
    ts_code: str
    report_period: date
    report_type: ReportType
    announcement_at: datetime
    statement_type: StatementType
    taxonomy: tuple[str, ...]
    fact_name: str
    raw_qname: str
    canonical_fact_name: str | None
    mapping_status: MappingStatus
    fact_value: Decimal
    unit: str | None
    currency: str | None
    filing_id: str
    context_signature: str
    entity_identifier: str
    period_start: date | None
    period_end: date | None
    instant: date | None
    unit_signature: str | None
    decimals: str | None
    consolidation_scope: ConsolidationScope
    dimensions: dict[str, str]
    fact_identity_hash: str
    comparison_identity_hash: str


class FactConflict(BaseModel):
    conflict_id: str
    filing_id: str
    fact_identity_hash: str
    competing_fact_ids: tuple[str, ...]
    conflict_type: str
    resolution_status: ConflictResolutionStatus
    quality_status: QualityStatus
    detected_at: datetime
```

`FactBase` supplies the provenance, publication, effective-time, version,
content-hash, license, quality, and `valid_from` fields. Set
`FinancialFact.record_id == fact_id`; set `FinancialFiling.record_id == filing_id`.

- [ ] **Step 5: Run focused tests and verify GREEN**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/contracts/test_financial_models.py tests/unit/contracts/test_models.py -q
.venv\Scripts\ruff.exe check src/hengce/contracts tests/unit/contracts
```

Expected: all selected tests pass and Ruff reports `All checks passed!`.

- [ ] **Step 6: Commit Task 1**

```powershell
git add src/hengce/contracts tests/unit/contracts/test_financial_models.py
git commit -m "feat: add financial filing contracts"
```

---

### Task 2: Persist Taxonomies, Filing Versions, Artifacts, and Conflicts

**Files:**
- Create: `src/hengce/state/migrations/006_financial_filings.sql`
- Create: `src/hengce/state/financial_repository.py`
- Create: `tests/unit/state/test_financial_repository.py`

**Interfaces:**
- Produces `FinancialArtifactRecord`.
- Produces `FinancialFilingRepository.register_taxonomy(ref) -> None`.
- Produces `FinancialFilingRepository.get_taxonomies(ids) -> tuple[TaxonomyPackageRef, ...]`.
- Produces `stage_filing(filing, expected_path, expected_hash, expected_count) -> FinancialArtifactRecord`.
- Produces `publish_filing(filing_id, path, content_hash, fact_count) -> bool`.
- Produces `get_filing(filing_id) -> FinancialArtifactRecord | None`.
- Produces `list_filing_versions(ts_code, report_period) -> list[FinancialArtifactRecord]`.
- Produces `record_conflicts(conflicts) -> None` and `list_conflicts(filing_id) -> list[FactConflict]`.

- [ ] **Step 1: Write failing repository tests**

Create `tests/unit/state/test_financial_repository.py` with helpers that build
`TaxonomyPackageRef` and `FinancialFiling`, then add:

```python
def test_taxonomy_registration_is_idempotent_and_hash_immutable(tmp_path: Path) -> None:
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()
    repository = FinancialFilingRepository(state.path)
    reference = taxonomy_ref(raw_object_hash="1" * 64)

    repository.register_taxonomy(reference)
    repository.register_taxonomy(reference)

    assert repository.get_taxonomies((reference.taxonomy_id,)) == (reference,)
    with pytest.raises(ValueError, match="FINANCIAL_TAXONOMY_CONFLICT"):
        repository.register_taxonomy(
            reference.model_copy(update={"raw_object_hash": "2" * 64})
        )


def test_stage_then_publish_requires_exact_artifact_identity(tmp_path: Path) -> None:
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()
    repository = FinancialFilingRepository(state.path)
    filing = financial_filing()

    staged = repository.stage_filing(
        filing,
        expected_path="financial_facts/report_year=2025/filing.parquet",
        expected_hash="3" * 64,
        expected_count=4,
    )
    assert staged.artifact_status == "PENDING"
    assert not repository.publish_filing(
        filing.filing_id,
        path=staged.expected_path,
        content_hash="4" * 64,
        fact_count=4,
    )
    assert repository.publish_filing(
        filing.filing_id,
        path=staged.expected_path,
        content_hash="3" * 64,
        fact_count=4,
    )
    assert repository.get_filing(filing.filing_id).artifact_status == "PUBLISHED"


def test_filing_versions_and_conflicts_are_append_only(tmp_path: Path) -> None:
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()
    repository = FinancialFilingRepository(state.path)
    old = financial_filing(filing_id="old", raw_object_hash="5" * 64)
    new = financial_filing(
        filing_id="new", raw_object_hash="6" * 64, supersedes_id="old", is_restated=True
    )
    repository.stage_filing(old, "old.parquet", "7" * 64, 1)
    repository.stage_filing(new, "new.parquet", "8" * 64, 1)
    repository.record_conflicts([fact_conflict(filing_id="new")])

    assert [item.filing.filing_id for item in repository.list_filing_versions(
        old.ts_code, old.report_period
    )] == ["old", "new"]
    assert repository.list_conflicts("new")[0].quality_status is QualityStatus.CONFLICT


def test_repository_normalizes_timestamp_keys_to_utc(tmp_path: Path) -> None:
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()
    repository = FinancialFilingRepository(state.path)
    filing = financial_filing(
        published_at=datetime.fromisoformat("2026-04-30T20:00:00+08:00"),
        valid_from=datetime.fromisoformat("2026-04-30T12:01:00+00:00"),
    )
    repository.stage_filing(filing, "filing.parquet", "9" * 64, 1)

    with sqlite3.connect(state.path) as connection:
        row = connection.execute(
            "SELECT published_at, valid_from FROM financial_filings WHERE filing_id=?",
            (filing.filing_id,),
        ).fetchone()
    assert row is not None
    assert tuple(row) == (
        "2026-04-30T12:00:00.000000Z",
        "2026-04-30T12:01:00.000000Z",
    )
```

- [ ] **Step 2: Run repository tests and verify RED**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/state/test_financial_repository.py -q
```

Expected: collection fails because migration 006 and
`FinancialFilingRepository` do not exist.

- [ ] **Step 3: Add the financial migration**

Create these tables in `006_financial_filings.sql`:

```sql
CREATE TABLE IF NOT EXISTS taxonomy_packages (
    taxonomy_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    raw_object_hash TEXT NOT NULL,
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS financial_filings (
    filing_id TEXT PRIMARY KEY,
    ts_code TEXT NOT NULL,
    report_period TEXT NOT NULL,
    published_at TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    supersedes_id TEXT REFERENCES financial_filings(filing_id) ON DELETE RESTRICT,
    payload_json TEXT NOT NULL,
    artifact_status TEXT NOT NULL CHECK (artifact_status IN ('PENDING', 'PUBLISHED')),
    expected_path TEXT NOT NULL,
    expected_hash TEXT NOT NULL,
    expected_count INTEGER NOT NULL CHECK (expected_count >= 0),
    artifact_published_at TEXT,
    UNIQUE(ts_code, report_period, filing_id)
);

CREATE INDEX IF NOT EXISTS idx_financial_filings_lookup
ON financial_filings(ts_code, report_period, published_at, valid_from);

CREATE TABLE IF NOT EXISTS financial_fact_conflicts (
    conflict_id TEXT PRIMARY KEY,
    filing_id TEXT NOT NULL REFERENCES financial_filings(filing_id) ON DELETE RESTRICT,
    payload_json TEXT NOT NULL,
    detected_at TEXT NOT NULL
);
```

Do not store a mutable “latest” pointer.

- [ ] **Step 4: Implement transactional repository methods**

Create:

```python
@dataclass(frozen=True)
class FinancialArtifactRecord:
    filing: FinancialFiling
    artifact_status: str
    expected_path: str
    expected_hash: str
    expected_count: int
    artifact_published_at: datetime | None


class FinancialFilingRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
```

Use `connect(self.path)` and one transaction per public method. For idempotent
registration/staging, compare the complete existing payload and expected artifact
identity; mismatches raise `FINANCIAL_TAXONOMY_CONFLICT` or
`FINANCIAL_FILING_CONFLICT`. `publish_filing` performs one guarded update:

```python
def utc_key(value: datetime) -> str:
    require_aware(value, "repository timestamp")
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
```

Use `utc_key` for every SQLite timestamp used in ordering or cutoff comparison;
never compare mixed-offset ISO strings lexicographically.

```sql
UPDATE financial_filings
SET artifact_status='PUBLISHED', artifact_published_at=?
WHERE filing_id=? AND artifact_status='PENDING'
  AND expected_path=? AND expected_hash=? AND expected_count=?
```

An already published exact identity returns `True`; any mismatched identity returns
`False`. Missing taxonomy IDs raise `FINANCIAL_TAXONOMY_MISSING`.

- [ ] **Step 5: Run focused tests and verify GREEN**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/state/test_financial_repository.py tests/unit/state/test_repository.py -q
.venv\Scripts\ruff.exe check src/hengce/state tests/unit/state
```

Expected: financial repository tests and all existing state tests pass.

- [ ] **Step 6: Commit Task 2**

```powershell
git add src/hengce/state/migrations/006_financial_filings.sql src/hengce/state/financial_repository.py tests/unit/state/test_financial_repository.py
git commit -m "feat: add financial filing state repository"
```

---

### Task 3: Safely Inspect and Materialize Local Attachments

**Files:**
- Create: `src/hengce/financials/__init__.py`
- Create: `src/hengce/financials/package.py`
- Create: `tests/unit/financials/test_package.py`

**Interfaces:**
- Produces `AttachmentLimits`.
- Produces `MaterializedFiling(entrypoint_path, taxonomy_package_paths, root)`.
- Produces `LocalAttachmentInspector.validate(path, content_type, *, taxonomy) -> None`.
- Produces context manager `SafePackageMaterializer.materialize(descriptor, taxonomies)`.
- Consumes `RawObjectStore`, `FilingDescriptor`, and `TaxonomyPackageRef`.

- [ ] **Step 1: Write failing safety tests**

Add:

```python
def test_inspector_rejects_mime_extension_mismatch_and_doctype(tmp_path: Path) -> None:
    xml = tmp_path / "filing.xml"
    xml.write_bytes(b"<!DOCTYPE x [<!ENTITY x SYSTEM 'file:///secret'>]><x/>")
    inspector = LocalAttachmentInspector()
    with pytest.raises(ValueError, match="FINANCIAL_XML_UNSAFE"):
        inspector.validate(xml, "application/xml", taxonomy=False)
    with pytest.raises(ValueError, match="FINANCIAL_ATTACHMENT_TYPE_INVALID"):
        inspector.validate(xml, "application/zip", taxonomy=False)


def test_materializer_rejects_zip_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "taxonomy.zip"
    with ZipFile(archive, "w") as handle:
        handle.writestr("../escape.xsd", b"<x/>")
    store, descriptor, taxonomy = stored_fixture(tmp_path, archive)
    with pytest.raises(ValueError, match="FINANCIAL_ARCHIVE_UNSAFE_PATH"):
        with SafePackageMaterializer(store).materialize(descriptor, (taxonomy,)):
            pass


def test_materializer_cleans_temporary_tree_and_preserves_safe_names(tmp_path: Path) -> None:
    store, descriptor, taxonomy = stored_valid_fixture(tmp_path)
    with SafePackageMaterializer(store).materialize(
        descriptor, (taxonomy,)
    ) as materialized:
        root = materialized.root
        assert materialized.entrypoint_path.name == "instance.xml"
        assert materialized.entrypoint_path.is_file()
        assert all(path.is_file() for path in materialized.taxonomy_package_paths)
    assert not root.exists()
```

Parameterize archive tests for absolute paths, `..`, backslashes, duplicate output
paths, more than 5,000 files, any file over 64 MiB, total expansion over 512 MiB,
and compression ratio over 100.

- [ ] **Step 2: Run package tests and verify RED**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/financials/test_package.py -q
```

Expected: collection fails because `hengce.financials.package` does not exist.

- [ ] **Step 3: Implement explicit limits and signature checks**

Use:

```python
@dataclass(frozen=True)
class AttachmentLimits:
    max_files: int = 5_000
    max_file_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024
    max_compression_ratio: int = 100


ALLOWED_INSTANCE_TYPES = {
    ".xml": {"application/xml", "text/xml", "application/xbrl+xml"},
    ".xbrl": {"application/xml", "text/xml", "application/xbrl+xml"},
    ".zip": {"application/zip"},
}
ALLOWED_TAXONOMY_TYPES = {
    **ALLOWED_INSTANCE_TYPES,
    ".xsd": {"application/xml", "text/xml", "application/xml-schema"},
}
```

Reject `<!DOCTYPE` and `<!ENTITY` case-insensitively before XML parsing. ZIP member
paths must be relative POSIX paths with no `..`, drive, UNC, backslash, NUL, or
symlink mode. Check declared and actual totals while streaming each member.

- [ ] **Step 4: Implement hash-validated temporary materialization**

Resolve every payload with `RawObjectStore.validate_content_hash()`. Create one
`TemporaryDirectory(prefix="hengce-xbrl-")`, copy direct XML/XSD with sanitized
declared names, and extract ZIPs only after all central-directory checks pass.
For an instance ZIP, require `descriptor.instance_entrypoint`; for direct XML,
require it to be `None`. Yield:

```python
@dataclass(frozen=True)
class MaterializedFiling:
    entrypoint_path: Path
    taxonomy_package_paths: tuple[Path, ...]
    root: Path
```

No materialized path may escape `root.resolve()`.

- [ ] **Step 5: Run focused tests and verify GREEN**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/financials/test_package.py tests/unit/raw_store/test_store.py -q
.venv\Scripts\ruff.exe check src/hengce/financials tests/unit/financials
```

Expected: all safety and existing raw-store tests pass.

- [ ] **Step 6: Commit Task 3**

```powershell
git add src/hengce/financials tests/unit/financials/test_package.py
git commit -m "feat: safely materialize XBRL packages"
```

---

### Task 4: Add the Network-Disabled Arelle Adapter

**Files:**
- Modify: `pyproject.toml`
- Create: `src/hengce/financials/xbrl.py`
- Create: `tests/fixtures/xbrl/minimal/test-gaap.xsd`
- Create: `tests/fixtures/xbrl/minimal/instance.xml`
- Create: `tests/unit/financials/test_xbrl.py`

**Interfaces:**
- Produces `RawXbrlContext`, `RawXbrlUnit`, `RawXbrlFact`, `XbrlParseResult`.
- Produces protocol `XbrlProcessor.parse(materialized) -> XbrlParseResult`.
- Produces `ArelleXbrlProcessor`.
- Consumes `MaterializedFiling`.

- [ ] **Step 1: Add fictional XBRL fixtures**

Create `test-gaap.xsd`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema"
  xmlns:xbrli="http://www.xbrl.org/2003/instance"
  xmlns:t="urn:hengce:test-gaap"
  targetNamespace="urn:hengce:test-gaap"
  elementFormDefault="qualified">
  <xsd:annotation>
    <xsd:documentation>FIXTURE DATA - NOT A REAL ISSUER</xsd:documentation>
  </xsd:annotation>
  <xsd:import namespace="http://www.xbrl.org/2003/instance"
    schemaLocation="http://www.xbrl.org/2003/xbrl-instance-2003-12-31.xsd"/>
  <xsd:element name="Assets" id="t_Assets" type="xbrli:monetaryItemType"
    substitutionGroup="xbrli:item" xbrli:periodType="instant"/>
  <xsd:element name="Liabilities" id="t_Liabilities" type="xbrli:monetaryItemType"
    substitutionGroup="xbrli:item" xbrli:periodType="instant"/>
  <xsd:element name="Equity" id="t_Equity" type="xbrli:monetaryItemType"
    substitutionGroup="xbrli:item" xbrli:periodType="instant"/>
  <xsd:element name="Revenue" id="t_Revenue" type="xbrli:monetaryItemType"
    substitutionGroup="xbrli:item" xbrli:periodType="duration"/>
</xsd:schema>
```

Create `instance.xml` with entity `600001.SH`, instant `2025-12-31`, duration
`2025-01-01` through `2025-12-31`, CNY unit, and fictional values:
Assets `1000`, Liabilities `400`, Equity `600`, Revenue `2000`. Add a schemaRef
to `test-gaap.xsd`.

- [ ] **Step 2: Write the failing offline adapter test**

```python
def test_arelle_parses_numeric_facts_without_any_socket(
    monkeypatch: pytest.MonkeyPatch, materialized_fixture: MaterializedFiling
) -> None:
    def blocked_connect(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)
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
    assert assets.unit.currency == "CNY"


def test_arelle_sessions_are_serialized_across_threads(
    monkeypatch: pytest.MonkeyPatch,
    materialized_fixture: MaterializedFiling,
) -> None:
    spy = ConcurrentSessionSpy()
    monkeypatch.setattr("hengce.financials.xbrl.Session", spy.session_type)
    processor = ArelleXbrlProcessor()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(processor.parse, [materialized_fixture, materialized_fixture]))
    assert spy.maximum_active_sessions == 1
```

Also test that nil and text facts are counted in diagnostics but excluded from
`result.facts`, and a missing schema raises `FINANCIAL_TAXONOMY_MISSING`.

- [ ] **Step 3: Run the adapter test and verify RED**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/financials/test_xbrl.py -q
```

Expected: collection fails because Arelle and the adapter are absent.

- [ ] **Step 4: Add and install the upper-bounded dependency**

Add:

```toml
"arelle-release>=2.42.1,<2.43",
```

Then run:

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -c "from arelle.api.Session import Session; print(Session.__name__)"
```

Expected: the import prints `Session`. Do not loosen the upper bound.

- [ ] **Step 5: Implement the supported Arelle Session boundary**

Use the supported Python API:

```python
from threading import Lock

from arelle.RuntimeOptions import RuntimeOptions
from arelle.api.Session import Session


_ARELLE_SESSION_LOCK = Lock()

options = RuntimeOptions(
    entrypointFile=str(materialized.entrypoint_path),
    internetConnectivity="offline",
    keepOpen=True,
    packages=[str(path) for path in materialized.taxonomy_package_paths],
    validate=True,
)
with _ARELLE_SESSION_LOCK:
    with Session() as session:
        session.run(options)
        models = session.get_models()
        if len(models) != 1:
            raise ValueError("FINANCIAL_XBRL_PARSE_ERROR")
        model = models[0]
        # Convert to neutral frozen dataclasses before releasing the lock.
```

Convert `fact.xValue` through `Decimal(str(fact.xValue))`; never through float.
Canonicalize QName with Clark notation. For Arelle date contexts, convert the
exclusive `instantDatetime`/`endDatetime` boundary back to the disclosed date.
Represent unit numerator and denominator measures as sorted Clark-QName tuples.
Represent dimensions as a sorted tuple of `(dimension_qname, member_qname)`.
Copy all data out before the `Session` closes.

The fixture relies only on Arelle's packaged cache for the standard XBRL 2.1
schema. With the socket guard active, any unresolved standard schema is a pinned
`2.42.1` compatibility failure: return `FINANCIAL_XBRL_PARSE_ERROR` and do not
enable network fallback.

- [ ] **Step 6: Run focused tests and verify GREEN**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/financials/test_xbrl.py tests/unit/financials/test_package.py -q
.venv\Scripts\ruff.exe check src/hengce/financials tests/unit/financials
```

Expected: the fictional fixture parses offline, missing taxonomy is blocked, and
all selected tests pass.

- [ ] **Step 7: Commit Task 4**

```powershell
git add pyproject.toml src/hengce/financials/xbrl.py tests/fixtures/xbrl/minimal tests/unit/financials/test_xbrl.py
git commit -m "feat: parse XBRL with offline Arelle"
```

---

### Task 5: Normalize Facts with an Injected Versioned Mapping

**Files:**
- Create: `src/hengce/financials/mapping.py`
- Create: `tests/unit/financials/test_mapping.py`

**Interfaces:**
- Produces `FactMapping(raw_qname, canonical_fact_name, statement_type, expected_unit_kind)`.
- Produces `FactMappingRegistry(mapping_version, mappings)`.
- Produces `FinancialFactNormalizer.normalize(filing, raw_facts) -> list[FinancialFact]`.
- Consumes `FinancialFiling` and `RawXbrlFact`.

- [ ] **Step 1: Write failing normalization tests**

```python
def test_normalizer_maps_fixture_qname_and_builds_stable_identities() -> None:
    registry = FactMappingRegistry(
        mapping_version="fixture-v1",
        mappings={
            "{urn:hengce:test-gaap}Assets": FactMapping(
                raw_qname="{urn:hengce:test-gaap}Assets",
                canonical_fact_name="assets",
                statement_type=StatementType.BALANCE_SHEET,
                expected_unit_kind="MONETARY",
            )
        },
    )
    normalizer = FinancialFactNormalizer(registry)
    left = normalizer.normalize(financial_filing(), [raw_assets(dimensions={"b": "2", "a": "1"})])
    right = normalizer.normalize(financial_filing(), [raw_assets(dimensions={"a": "1", "b": "2"})])

    assert left == right
    assert left[0].mapping_status is MappingStatus.MAPPED
    assert left[0].canonical_fact_name == "assets"
    assert left[0].record_id == left[0].fact_id
    assert left[0].fact_identity_hash == right[0].fact_identity_hash


def test_unmapped_fact_is_preserved_but_unverified() -> None:
    facts = FinancialFactNormalizer(
        FactMappingRegistry(mapping_version="empty", mappings={})
    ).normalize(financial_filing(), [raw_assets()])
    assert facts[0].raw_qname == "{urn:hengce:test-gaap}Assets"
    assert facts[0].canonical_fact_name is None
    assert facts[0].mapping_status is MappingStatus.UNMAPPED
    assert facts[0].quality_status is QualityStatus.UNVERIFIED


def test_comparison_identity_ignores_filing_id_but_fact_identity_does_not() -> None:
    old = normalizer().normalize(financial_filing(filing_id="old"), [raw_assets()])[0]
    new = normalizer().normalize(financial_filing(filing_id="new"), [raw_assets()])[0]
    assert old.fact_identity_hash != new.fact_identity_hash
    assert old.comparison_identity_hash == new.comparison_identity_hash
```

- [ ] **Step 2: Run mapping tests and verify RED**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/financials/test_mapping.py -q
```

Expected: collection fails because mapping and normalizer types do not exist.

- [ ] **Step 3: Implement deterministic normalization**

Use canonical JSON and SHA-256:

```python
def stable_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
```

The fact identity includes filing ID, raw QName, entity, period, unit signature,
and sorted dimensions. The comparison identity excludes filing ID and adds
report period, report type, and consolidation scope. Generate:

```python
fact_id = f"fact-{fact_identity_hash}"
version = (
    f"{filing.filing_version}:"
    f"{filing.parser_version}:"
    f"{registry.mapping_version}"
)
```

Set `supersedes_id=None`; Task 9 links corrected facts after quality validation.
Do not ship a production QName mapping table in this task.

- [ ] **Step 4: Run focused tests and verify GREEN**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/financials/test_mapping.py tests/unit/contracts/test_financial_models.py -q
.venv\Scripts\ruff.exe check src/hengce/financials/mapping.py tests/unit/financials/test_mapping.py
```

Expected: deterministic mapping, unmapped preservation, and identity tests pass.

- [ ] **Step 5: Commit Task 5**

```powershell
git add src/hengce/financials/mapping.py tests/unit/financials/test_mapping.py
git commit -m "feat: normalize versioned financial facts"
```

---

### Task 6: Validate Duplicates, Conflicts, Mapping Coverage, and Accounting Equality

**Files:**
- Create: `src/hengce/financials/quality.py`
- Create: `tests/unit/financials/test_quality.py`

**Interfaces:**
- Produces `QualityIssue(code: str, fact_ids: tuple[str, ...], detail: str)`.
- Produces `FinancialQualityResult(facts, conflicts, issues, filing_quality_status)`.
- Produces `FinancialQualityValidator.validate(filing, facts) -> FinancialQualityResult`.
- Consumes `FinancialFiling` and normalized `FinancialFact` objects from Task 5.

- [ ] **Step 1: Write failing duplicate and conflict tests**

```python
def test_identical_duplicate_is_collapsed_without_conflict() -> None:
    fact = mapped_fact("assets", Decimal("1000"))
    result = FinancialQualityValidator().validate(financial_filing(), [fact, fact])

    assert result.facts == (fact,)
    assert result.conflicts == ()
    assert [issue.code for issue in result.issues] == ["FINANCIAL_FACT_DUPLICATE"]


def test_different_values_for_one_identity_create_open_conflict() -> None:
    left = mapped_fact("assets", Decimal("1000"), fact_id="left")
    right = mapped_fact("assets", Decimal("1100"), fact_id="right")
    right = right.model_copy(
        update={
            "fact_identity_hash": left.fact_identity_hash,
            "comparison_identity_hash": left.comparison_identity_hash,
        }
    )
    result = FinancialQualityValidator().validate(
        financial_filing(), [left, right]
    )

    assert result.filing_quality_status is QualityStatus.CONFLICT
    assert result.conflicts[0].competing_fact_ids == ("left", "right")
    assert all(fact.quality_status is QualityStatus.CONFLICT for fact in result.facts)
```

- [ ] **Step 2: Write failing completeness and equality tests**

```python
def test_balance_sheet_uses_decimals_derived_tolerance() -> None:
    facts = [
        mapped_fact("assets", Decimal("1000"), decimals="-1"),
        mapped_fact("liabilities", Decimal("399"), decimals="-1"),
        mapped_fact("equity", Decimal("600"), decimals="-1"),
    ]
    result = FinancialQualityValidator().validate(financial_filing(), facts)
    assert "FINANCIAL_BALANCE_EQUATION_CONFLICT" not in {
        issue.code for issue in result.issues
    }


def test_missing_balance_component_is_partial_not_fabricated() -> None:
    facts = [
        mapped_fact("assets", Decimal("1000")),
        mapped_fact("liabilities", Decimal("400")),
    ]
    result = FinancialQualityValidator().validate(financial_filing(), facts)
    assert result.filing_quality_status is QualityStatus.PARTIAL
    assert [fact.canonical_fact_name for fact in result.facts] == [
        "assets",
        "liabilities",
    ]
    assert "FINANCIAL_BALANCE_COMPONENT_MISSING" in {
        issue.code for issue in result.issues
    }
```

Add cases proving that different currency, unit, instant, dimensions, and
consolidation scope are never combined into one equation. Add an unmapped fact
case that preserves the fact as `UNVERIFIED` and makes the filing `PARTIAL`.

- [ ] **Step 3: Run quality tests and verify RED**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/financials/test_quality.py -q
```

Expected: collection fails because `FinancialQualityValidator` does not exist.

- [ ] **Step 4: Implement deterministic validation**

Use frozen result types:

```python
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
```

Group duplicates by `fact_identity_hash`. Sort facts and IDs before producing
results so input order cannot change output. For accounting equality, group by:

```python
(
    fact.instant,
    fact.unit_signature,
    fact.currency,
    fact.consolidation_scope,
    tuple(sorted(fact.dimensions.items())),
)
```

Calculate each fact's rounding tolerance with:

```python
def rounding_tolerance(decimals: str | None) -> Decimal:
    if decimals in {None, "INF"}:
        return Decimal(0)
    return Decimal("0.5") * (Decimal(10) ** (-int(decimals)))
```

For assets = liabilities + equity, accept an absolute difference no greater
than the sum of the three tolerances. Missing components yield `PARTIAL`;
excess difference creates `FINANCIAL_BALANCE_EQUATION_CONFLICT`.

- [ ] **Step 5: Run focused tests and verify GREEN**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/financials/test_quality.py tests/unit/financials/test_mapping.py -q
.venv\Scripts\ruff.exe check src/hengce/financials/quality.py tests/unit/financials/test_quality.py
```

Expected: duplicate, conflict, grouping, completeness, and tolerance tests pass.

- [ ] **Step 6: Commit Task 6**

```powershell
git add src/hengce/financials/quality.py tests/unit/financials/test_quality.py
git commit -m "feat: validate financial fact quality"
```

---

### Task 7: Publish Immutable Financial-Fact Parquet Artifacts

**Files:**
- Create: `src/hengce/warehouse/financial.py`
- Create: `tests/unit/warehouse/test_financial.py`

**Interfaces:**
- Produces `FinancialArtifact(path: Path, content_hash: str, fact_count: int)`.
- Produces `FinancialFactWarehouse.expected_artifact(filing, facts) -> FinancialArtifact`.
- Produces `FinancialFactWarehouse.write_facts(filing, facts) -> FinancialArtifact`.
- Produces `FinancialFactWarehouse.validate_artifact(artifact, filing_id) -> str`.
- Produces `FinancialFactWarehouse.read_artifact(path) -> list[dict[str, object]]`.

- [ ] **Step 1: Write failing artifact tests**

```python
def test_financial_artifact_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    warehouse = FinancialFactWarehouse(tmp_path)
    filing = financial_filing()
    facts = [mapped_fact("assets", Decimal("1000"))]

    first = warehouse.write_facts(filing, facts)
    second = warehouse.write_facts(filing, list(reversed(facts)))

    assert first == second
    assert first.fact_count == 1
    assert first.path.name == f"filing-{filing.filing_id}-{first.content_hash}.parquet"
    assert warehouse.read_artifact(first.path)[0]["fact_id"] == facts[0].fact_id


def test_artifact_rejects_mixed_filing_ids(tmp_path: Path) -> None:
    warehouse = FinancialFactWarehouse(tmp_path)
    with pytest.raises(ValueError, match="FINANCIAL_FACTS_MIXED_FILING"):
        warehouse.write_facts(
            financial_filing(filing_id="one"),
            [
                mapped_fact("assets", Decimal("1000"), filing_id="one"),
                mapped_fact("equity", Decimal("600"), filing_id="two"),
            ],
        )


def test_corrupt_existing_artifact_is_never_overwritten(tmp_path: Path) -> None:
    warehouse = FinancialFactWarehouse(tmp_path)
    filing = financial_filing()
    facts = [mapped_fact("assets", Decimal("1000"))]
    expected = warehouse.expected_artifact(filing, facts)
    expected.path.parent.mkdir(parents=True)
    expected.path.write_bytes(b"corrupt")

    with pytest.raises(ValueError, match="FINANCIAL_PARQUET_INTEGRITY_ERROR"):
        warehouse.write_facts(filing, facts)
    assert expected.path.read_bytes() == b"corrupt"
```

Also cover empty fact lists, wrong partition metadata, wrong row count, wrong
filename hash, and publication races.

- [ ] **Step 2: Run warehouse tests and verify RED**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/warehouse/test_financial.py -q
```

Expected: collection fails because `FinancialFactWarehouse` does not exist.

- [ ] **Step 3: Implement canonical artifact identity**

Canonicalize model rows with:

```python
rows = [fact.model_dump(mode="json") for fact in facts]
rows.sort(key=lambda row: (str(row["fact_id"]), json.dumps(row, sort_keys=True)))
canonical = json.dumps(
    rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
).encode("utf-8")
content_hash = hashlib.sha256(canonical).hexdigest()
```

Use this path:

```python
root / "financial_facts" / f"report_year={filing.report_period.year}" \
    / f"report_type={filing.report_type.value}" / f"exchange={filing.exchange}" \
    / f"filing-{filing.filing_id}-{content_hash}.parquet"
```

Require every fact's `filing_id` to equal the supplied filing ID.

- [ ] **Step 4: Implement no-overwrite publication and validation**

Write Zstandard Parquet to a same-directory temporary file, `fsync`, then publish
with `os.link`. If the target exists, delete the temporary file and validate the
winner. Validation must re-read all rows, rebuild the canonical JSON hash, verify
the filename, filing ID, count, and expected hash, and raise
`FINANCIAL_PARQUET_INTEGRITY_ERROR` on any mismatch.

- [ ] **Step 5: Run focused tests and verify GREEN**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/warehouse/test_financial.py tests/unit/warehouse/test_market.py -q
.venv\Scripts\ruff.exe check src/hengce/warehouse tests/unit/warehouse
```

Expected: financial and market warehouse tests pass.

- [ ] **Step 6: Commit Task 7**

```powershell
git add src/hengce/warehouse/financial.py tests/unit/warehouse/test_financial.py
git commit -m "feat: store immutable financial facts"
```

---

### Task 8: Query Financial Facts with Explicit Public and System Cutoffs

**Files:**
- Create: `src/hengce/financials/query.py`
- Create: `tests/unit/financials/test_query.py`

**Interfaces:**
- Produces `FinancialQueryResult(facts, blocked_reasons, filing_ids)`.
- Produces `AsOfFinancialQuery.query_financial_facts(...) -> FinancialQueryResult`.
- Consumes `FinancialFilingRepository`, its manifest records, and published Parquet files.

- [ ] **Step 1: Write failing bitemporal and restatement tests**

```python
def test_query_requires_both_timezone_aware_cutoffs(tmp_path: Path) -> None:
    query = prepared_query(tmp_path)
    with pytest.raises(ValueError, match="FINANCIAL_QUERY_CUTOFF_INVALID"):
        query.query_financial_facts(
            ts_code="600001.SH",
            report_period=date(2025, 12, 31),
            canonical_fact_names=frozenset({"assets"}),
            as_of=datetime(2026, 4, 30),
            known_at=datetime(2026, 5, 1, tzinfo=UTC),
        )


def test_correction_is_invisible_before_publication_and_visible_after(tmp_path: Path) -> None:
    query, old, new = prepared_old_and_corrected_query(tmp_path)
    before = query.query_financial_facts(
        ts_code="600001.SH",
        report_period=date(2025, 12, 31),
        canonical_fact_names=frozenset({"assets"}),
        as_of=new.filing.published_at - timedelta(seconds=1),
        known_at=new.filing.valid_from,
    )
    after = query.query_financial_facts(
        ts_code="600001.SH",
        report_period=date(2025, 12, 31),
        canonical_fact_names=frozenset({"assets"}),
        as_of=new.filing.published_at,
        known_at=new.filing.valid_from,
    )
    assert before.facts[0]["fact_value"] == Decimal("1000")
    assert after.facts[0]["fact_value"] == Decimal("1100")


def test_public_unusable_correction_blocks_instead_of_falling_back(tmp_path: Path) -> None:
    query, correction = prepared_unusable_correction(tmp_path)
    result = query.query_financial_facts(
        ts_code="600001.SH",
        report_period=date(2025, 12, 31),
        canonical_fact_names=frozenset({"assets"}),
        as_of=correction.filing.published_at,
        known_at=correction.filing.valid_from,
    )
    assert result.facts == ()
    assert result.blocked_reasons == ("FINANCIAL_RESTATEMENT_UNUSABLE",)
```

Add cases for unpublished manifest paths, unmapped facts, `CONFLICT` facts, and
requested canonical names not present.

- [ ] **Step 2: Run query tests and verify RED**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/financials/test_query.py -q
```

Expected: collection fails because `AsOfFinancialQuery` does not exist.

- [ ] **Step 3: Implement explicit chain selection**

Expose:

```python
@dataclass(frozen=True)
class FinancialQueryResult:
    facts: tuple[dict[str, object], ...]
    blocked_reasons: tuple[str, ...]
    filing_ids: tuple[str, ...]


def query_financial_facts(
    self,
    *,
    ts_code: str,
    report_period: date,
    canonical_fact_names: frozenset[str],
    as_of: datetime,
    known_at: datetime,
) -> FinancialQueryResult:
```

Reject naive cutoffs. Do not impose an ordering between `as_of` and `known_at`:
they are independent public-time and system-time axes, and historical replay may
legitimately use either ordering.
Filter versions by `published_at <= as_of` and `valid_from <= known_at`, then
walk `supersedes_id` links. A public latest correction with non-published artifact
or `CONFLICT`/`REJECTED`/`UNVERIFIED` filing quality returns
`FINANCIAL_RESTATEMENT_UNUSABLE`.

- [ ] **Step 4: Query only manifest-approved paths with DuckDB**

Pass only paths from `artifact_status="PUBLISHED"` records to. Build SQL
placeholders from the size of the non-empty `canonical_fact_names` set, while
binding every name as a parameter:

```python
names = sorted(canonical_fact_names)
placeholders = ", ".join("?" for _ in names)
cursor = connection.execute(
    f"""
    SELECT * FROM read_parquet(?)
    WHERE ts_code = ?
      AND report_period = ?
      AND canonical_fact_name IN ({placeholders})
      AND quality_status = 'VALID'
    ORDER BY canonical_fact_name, fact_id
    """,
    [paths, ts_code, report_period.isoformat(), *names],
)
```

Return an empty result without opening DuckDB when `canonical_fact_names` is empty.
Before DuckDB reads, resolve each path and require it to remain below the configured
financial warehouse root. Never glob the filesystem and never query an unregistered
file.

- [ ] **Step 5: Run focused tests and verify GREEN**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/financials/test_query.py tests/unit/state/test_financial_repository.py tests/unit/warehouse/test_financial.py -q
.venv\Scripts\ruff.exe check src/hengce/financials/query.py tests/unit/financials/test_query.py
```

Expected: cutoff, correction, blocked fallback, and manifest visibility tests pass.

- [ ] **Step 6: Commit Task 8**

```powershell
git add src/hengce/financials/query.py tests/unit/financials/test_query.py
git commit -m "feat: add point-in-time financial queries"
```

---

### Task 9: Orchestrate Idempotent Filing Ingestion and Crash Recovery

**Files:**
- Create: `src/hengce/services/financial_ingestion.py`
- Create: `tests/unit/services/test_financial_ingestion.py`

**Interfaces:**
- Produces `FinancialIngestionResult`.
- Produces `FinancialIngestionService.run(descriptor) -> FinancialIngestionResult`.
- Consumes `PolicyGuard`, `RawObjectStore`, `FinancialFilingRepository`,
  `SafePackageMaterializer`, `XbrlProcessor`, `FinancialFactNormalizer`,
  `FinancialQualityValidator`, `FinancialFactWarehouse`, and `StateRepository`.

- [ ] **Step 1: Write failing happy-path and idempotence tests**

```python
def test_ingestion_publishes_valid_filing_and_terminal_run(tmp_path: Path) -> None:
    service, state, repository = build_service(tmp_path)
    result = service.run(filing_descriptor())

    assert result.run_status is RunStatus.SUCCEEDED
    assert result.fact_count == 4
    assert repository.get_filing(result.filing_id).artifact_status == "PUBLISHED"
    run = state.list_runs(run_type="financial_xbrl")[0]
    assert run.run_status is RunStatus.SUCCEEDED
    assert run.finished_at is not None


def test_repeating_same_descriptor_is_idempotent(tmp_path: Path) -> None:
    service, _, repository = build_service(tmp_path)
    first = service.run(filing_descriptor())
    second = service.run(filing_descriptor())

    assert first == second
    assert len(repository.list_filing_versions("600001.SH", date(2025, 12, 31))) == 1
```

- [ ] **Step 2: Write failing block, conflict, correction, and recovery tests**

```python
def test_missing_taxonomy_blocks_without_parser_call(tmp_path: Path) -> None:
    service, state, _ = build_service(tmp_path, register_taxonomy=False)
    processor = cast(FakeProcessor, service.processor)
    result = service.run(filing_descriptor())
    assert result.run_status is RunStatus.BLOCKED
    assert result.error_code == "FINANCIAL_TAXONOMY_MISSING"
    assert processor.calls == 0
    assert state.list_runs(run_type="financial_xbrl")[0].run_status is RunStatus.BLOCKED


def test_crash_after_artifact_write_recovers_without_duplicate(tmp_path: Path) -> None:
    crash_once = CrashOnce("after_artifact_write")
    service, _, repository = build_service(tmp_path, stage_hook=crash_once)
    with pytest.raises(RuntimeError, match="injected crash"):
        service.run(filing_descriptor())

    recovered, _, _ = build_service(tmp_path)
    result = recovered.run(filing_descriptor())
    assert result.run_status is RunStatus.SUCCEEDED
    assert len(repository.list_filing_versions("600001.SH", date(2025, 12, 31))) == 1


def test_correction_links_matching_facts_and_never_overwrites_old(tmp_path: Path) -> None:
    service, _, repository = build_service(tmp_path)
    old = service.run(filing_descriptor(raw_hash="1" * 64))
    new = service.run(
        filing_descriptor(
            raw_hash="2" * 64,
            is_restated=True,
            supersedes_id=old.filing_id,
        )
    )
    versions = repository.list_filing_versions("600001.SH", date(2025, 12, 31))
    assert [item.filing.filing_id for item in versions] == [old.filing_id, new.filing_id]
    assert versions[1].filing.supersedes_id == old.filing_id
```

Add a policy-denial case that records one refusal and never calls the parser, plus
a conflicting-fact case that returns `RunStatus.PARTIAL` and records open conflicts.

- [ ] **Step 3: Run service tests and verify RED**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/services/test_financial_ingestion.py -q
```

Expected: collection fails because `FinancialIngestionService` does not exist.

- [ ] **Step 4: Implement deterministic filing construction and status mapping**

Create:

```python
@dataclass(frozen=True)
class FinancialIngestionResult:
    filing_id: str
    run_id: str
    run_status: RunStatus
    fact_count: int
    conflict_count: int
    artifact_path: str | None
    artifact_hash: str | None
    error_code: str | None


class FinancialIngestionService:
    def __init__(
        self,
        *,
        guard: PolicyGuard,
        raw_store: RawObjectStore,
        repository: FinancialFilingRepository,
        materializer: SafePackageMaterializer,
        processor: XbrlProcessor,
        normalizer: FinancialFactNormalizer,
        validator: FinancialQualityValidator,
        warehouse: FinancialFactWarehouse,
        state: StateRepository,
        clock: Callable[[], datetime] = utc_now,
        stage_hook: Callable[[str], None] = no_stage_hook,
    ) -> None:
        self.guard = guard
        self.raw_store = raw_store
        self.repository = repository
        self.materializer = materializer
        self.processor = processor
        self.normalizer = normalizer
        self.validator = validator
        self.warehouse = warehouse
        self.state = state
        self.clock = clock
        self.stage_hook = stage_hook
```

The constructor performs no I/O.

`run()` must execute:

```text
policy validate
→ taxonomy resolve
→ materialize
→ offline parse
→ construct candidate filing
→ normalize
→ quality validate
→ set final valid_from and fact quality
→ link correction facts by comparison_identity_hash
→ expected artifact
→ stage SQLite manifest
→ publish/validate Parquet
→ record conflicts
→ atomically publish SQLite manifest
→ terminalize RunRecord
```

Use deterministic filing ID from source ID, code, report period, report type, and
raw hash. Inject `clock` and `stage_hook(stage_name)`; production defaults to a
no-op hook. Call the hook after artifact write and before SQLite publication for
the recovery test.

- [ ] **Step 5: Implement error-to-status mapping and safe retry**

Map typed financial failures by stable error code:

```python
ERROR_STATUS = {
    "RAW_PAYLOAD_INTEGRITY_ERROR": RunStatus.FAILED,
    "FINANCIAL_TAXONOMY_MISSING": RunStatus.BLOCKED,
    "FINANCIAL_XBRL_PARSE_ERROR": RunStatus.FAILED,
    "FINANCIAL_PARQUET_INTEGRITY_ERROR": RunStatus.FAILED,
    "FINANCIAL_NUMERIC_FACTS_MISSING": RunStatus.PARTIAL,
    "FINANCIAL_FACT_UNMAPPED": RunStatus.PARTIAL,
    "FINANCIAL_FACT_CONFLICT": RunStatus.PARTIAL,
}
```

Catch `PolicyDenied` separately, preserve its concrete `reason_code` in
`FinancialIngestionResult.error_code`, and terminalize the run as
`RunStatus.BLOCKED`; do not collapse policy refusals into a synthetic code.
Do not catch the injected crash as a successful or partial run. A retry finds the
same staged artifact identity, validates an existing Parquet winner if present,
and completes the guarded SQLite publication without adding a filing or fact.

- [ ] **Step 6: Run focused tests and verify GREEN**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/services/test_financial_ingestion.py tests/unit/services/test_initializer.py -q
.venv\Scripts\ruff.exe check src/hengce/services tests/unit/services
```

Expected: ingestion, policy, idempotence, correction, conflict, and crash-recovery
tests pass with all existing service tests.

- [ ] **Step 7: Commit Task 9**

```powershell
git add src/hengce/services/financial_ingestion.py tests/unit/services/test_financial_ingestion.py
git commit -m "feat: orchestrate XBRL filing ingestion"
```

---

### Task 10: Add Local-Only Taxonomy Registration and Filing Import Commands

**Files:**
- Modify: `src/hengce/cli.py`
- Modify: `tests/unit/test_cli.py`

**Interfaces:**
- Produces `build_financial_ingestion(settings, policy_file=None) -> FinancialIngestionService`.
- Produces CLI `register-xbrl-taxonomy`.
- Produces CLI `import-financial-xbrl`.
- Commands read local files only and perform no HTTP request.

- [ ] **Step 1: Write failing CLI refusal and registration tests**

```python
def test_register_taxonomy_rejects_policy_before_raw_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taxonomy = tmp_path / "test-gaap.xsd"
    taxonomy.write_text("<xsd:schema/>", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "register-xbrl-taxonomy",
            "--file", str(taxonomy),
            "--taxonomy-id", "test-gaap-2025",
            "--source-id", "unknown",
            "--source-url", "https://example.com/test-gaap.xsd",
            "--entrypoint", "test-gaap.xsd",
            "--content-type", "application/xml-schema",
            "--collected-at", "2026-07-26T12:00:00+08:00",
            "--data-dir", str(tmp_path / "data"),
            "--policy-file", str(POLICY_FILE),
        ],
    )
    assert result.exit_code != 0
    assert not list((tmp_path / "data" / "raw").rglob("payload.bin"))


def test_register_taxonomy_persists_approved_local_object(tmp_path: Path) -> None:
    result = invoke_fixture_taxonomy_registration(tmp_path)
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["taxonomy_id"] == "test-gaap-2025"
    assert len(payload["raw_object_hash"]) == 64
```

- [ ] **Step 2: Write failing filing-import tests**

```python
def test_import_financial_xbrl_uses_injected_local_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = FinancialIngestionResult(
        filing_id="filing-1",
        run_id="run-1",
        run_status=RunStatus.SUCCEEDED,
        fact_count=4,
        conflict_count=0,
        artifact_path="facts.parquet",
        artifact_hash="a" * 64,
        error_code=None,
    )
    fake = FakeFinancialIngestion(expected)
    monkeypatch.setattr("hengce.cli.build_financial_ingestion", lambda *args: fake)

    result = runner.invoke(
        app,
        financial_import_args(tmp_path),
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["filing_id"] == "filing-1"
    assert fake.descriptors[0].discovery_method is DiscoveryMethod.MANUAL_IMPORT
```

Also test strict ISO report date, timezone-aware `published-at` and
`collected-at`, SSE/`.SH` and SZSE/`.SZ` consistency, unsupported MIME, unsafe
XML, and missing taxonomy.

- [ ] **Step 3: Run CLI tests and verify RED**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/test_cli.py -q
```

Expected: failures because both commands and financial composition are absent.

- [ ] **Step 4: Implement pre-persist validation and taxonomy registration**

`register-xbrl-taxonomy` accepts:

```text
--file PATH
--taxonomy-id TEXT
--source-id sse|szse
--source-url HTTPS_URL
--entrypoint RELATIVE_PATH
--content-type MIME
--collected-at ISO_OFFSET_DATETIME
--data-dir PATH
--policy-file PATH
```

Order is mandatory:

```python
state = bootstrap_state(settings, policy_file)
PolicyGuard(state).validate(source_id, source_url, "xbrl", "cli.register_taxonomy")
LocalAttachmentInspector().validate(file, content_type, taxonomy=True)
raw_ref = RawObjectStore(settings.data_dir / "raw").put(
    source_id=source_id,
    source_url=source_url,
    collected_at=parsed_collected_at,
    content_type=content_type,
    payload=file.read_bytes(),
)
reference = TaxonomyPackageRef(
    taxonomy_id=taxonomy_id,
    source_id=source_id,
    source_url=source_url,
    raw_object_hash=raw_ref.content_hash,
    package_name=file.name,
    entrypoint=entrypoint,
    content_type=content_type,
    collected_at=parsed_collected_at,
)
FinancialFilingRepository(state.path).register_taxonomy(reference)
```

If policy or attachment validation fails, `RawObjectStore.put` is never called.

- [ ] **Step 5: Implement local filing import**

`import-financial-xbrl` accepts:

```text
--file PATH
--source-id sse|szse
--source-url HTTPS_URL
--ts-code 600001.SH|300001.SZ
--exchange SSE|SZSE
--report-period YYYY-MM-DD
--report-type ANNUAL|Q1|HALF_YEAR|Q3
--published-at ISO_OFFSET_DATETIME
--collected-at ISO_OFFSET_DATETIME
--content-type MIME
--taxonomy-id TEXT (repeatable)
--instance-entrypoint RELATIVE_PATH (only for ZIP)
--data-dir PATH
--policy-file PATH
```

Validate policy and attachment before raw persistence, put the object, construct
`FilingDescriptor(discovery_method=MANUAL_IMPORT)`, call the composed service,
and print only the dataclass result as sorted UTF-8 JSON. Do not print raw facts,
attachment content, environment variables, or parser logs.

Compose the command with an explicitly empty mapping registry:

```python
def build_financial_ingestion(
    settings: Settings,
    policy_file: Path | None = None,
) -> FinancialIngestionService:
    state = bootstrap_state(settings, policy_file)
    raw_store = RawObjectStore(settings.data_dir / "raw")
    mapping_registry = FactMappingRegistry(
        mapping_version="empty-v1",
        mappings={},
    )
    return FinancialIngestionService(
        guard=PolicyGuard(state),
        raw_store=raw_store,
        repository=FinancialFilingRepository(state.path),
        materializer=SafePackageMaterializer(raw_store),
        processor=ArelleXbrlProcessor(),
        normalizer=FinancialFactNormalizer(mapping_registry),
        validator=FinancialQualityValidator(),
        warehouse=FinancialFactWarehouse(
            settings.data_dir / "warehouse" / "financial_facts"
        ),
        state=state,
    )
```

This first slice intentionally has no production QName mapping. Therefore a real
manual import remains traceable but returns `PARTIAL`/`FINANCIAL_FACT_UNMAPPED`
until a separately reviewed mapping package is injected. Tests inject the
fictional `fixture-v1` registry and must never install it in the production
composition.

- [ ] **Step 6: Run focused tests and verify GREEN**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/test_cli.py tests/unit/policy/test_guard.py -q
.venv\Scripts\ruff.exe check src/hengce/cli.py tests/unit/test_cli.py
```

Expected: local commands, refusal-before-persist, validation, and existing CLI
tests pass.

- [ ] **Step 7: Commit Task 10**

```powershell
git add src/hengce/cli.py tests/unit/test_cli.py
git commit -m "feat: add local XBRL import commands"
```

---

### Task 11: Prove Acceptance Boundaries and Document Local Operation

**Files:**
- Create: `tests/integration/test_m2_xbrl_acceptance.py`
- Create: `docs/runbooks/xbrl-financial-facts.md`
- Modify: `docs/superpowers/plans/2026-07-24-a-share-research-master-roadmap.md`

**Interfaces:**
- Verifies AC-XF01 through AC-XF10 from the approved design.
- Documents fixture-only/manual-import operation and explicit unsupported features.
- Links this plan as M2a without marking M2 financials/actions complete.

- [ ] **Step 1: Write the failing end-to-end acceptance tests**

Create one fixture-driven composition helper and these acceptance tests:

```python
def test_xbrl_kernel_is_offline_traceable_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        socket.socket,
        "connect",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("network access is forbidden")
        ),
    )
    system = build_acceptance_system(tmp_path)
    first = system.ingest_valid_fixture()
    second = system.ingest_valid_fixture()
    result = system.query(
        canonical_names=frozenset({"assets", "liabilities", "equity", "revenue"}),
        as_of=system.published_at,
        known_at=system.valid_from,
    )

    assert first == second
    assert first.fact_count == 4
    assert len(result.facts) == 4
    assert all(len(row["content_hash"]) == 64 for row in result.facts)
    assert all(str(row["source_url"]).startswith("https://www.sse.com.cn/") for row in result.facts)


def test_fixed_replay_uses_old_then_corrected_value(tmp_path: Path) -> None:
    system = build_acceptance_system(tmp_path)
    old, corrected = system.ingest_old_and_correction()

    assert system.asset_value(
        as_of=corrected.filing.published_at - timedelta(seconds=1),
        known_at=corrected.filing.valid_from,
    ) == Decimal("1000")
    assert system.asset_value(
        as_of=corrected.filing.published_at,
        known_at=corrected.filing.valid_from,
    ) == Decimal("1100")
    assert old.filing.raw_object_hash != corrected.filing.raw_object_hash


def test_missing_taxonomy_conflict_and_partial_publication_never_become_queryable(
    tmp_path: Path,
) -> None:
    system = build_acceptance_system(tmp_path)
    assert system.ingest_missing_taxonomy().run_status is RunStatus.BLOCKED
    assert system.ingest_conflict().run_status is RunStatus.PARTIAL
    result = system.query(
        canonical_names=frozenset({"assets"}),
        as_of=system.conflict_descriptor.published_at,
        known_at=system.valid_from,
    )
    assert result.facts == ()


def test_repository_contains_only_marked_fictional_xbrl_fixtures() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    production_data = repository_root / "data"
    prohibited_suffixes = {".xbrl", ".xml", ".xsd", ".zip", ".parquet"}
    offenders = (
        [
            path.relative_to(repository_root)
            for path in production_data.rglob("*")
            if path.is_file() and path.suffix.lower() in prohibited_suffixes
        ]
        if production_data.exists()
        else []
    )
    assert offenders == []

    fixture_root = repository_root / "tests" / "fixtures" / "xbrl" / "minimal"
    fixture_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(fixture_root.iterdir())
        if path.suffix in {".xml", ".xsd"}
    )
    assert "urn:hengce:test-gaap" in fixture_text
    assert "600001.SH" in fixture_text
    assert "FIXTURE DATA - NOT A REAL ISSUER" in fixture_text
```

Add acceptance cases for unsafe XML/ZIP refusal before persistence, crash recovery,
unregistered Parquet invisibility, unmapped QName exclusion, and absence of
production financial fixtures under `data/`.

- [ ] **Step 2: Run acceptance tests and verify RED**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/integration/test_m2_xbrl_acceptance.py -q
```

Expected: the new acceptance helper or uncovered boundary fails until all wiring
and recovery behavior is complete.

- [ ] **Step 3: Compose the acceptance system only from shipped boundaries**

Keep fixture convenience code in the integration test and wire the same public
constructors used by the CLI:

```python
@dataclass(frozen=True)
class AcceptanceSystem:
    service: FinancialIngestionService
    query_engine: AsOfFinancialQuery
    repository: FinancialFilingRepository
    raw_store: RawObjectStore
    descriptor: FilingDescriptor
    correction_descriptor: FilingDescriptor
    missing_taxonomy_descriptor: FilingDescriptor
    conflict_descriptor: FilingDescriptor
    published_at: datetime
    valid_from: datetime

    def ingest_valid_fixture(self) -> FinancialIngestionResult:
        return self.service.run(self.descriptor)

    def ingest_old_and_correction(
        self,
    ) -> tuple[FinancialArtifactRecord, FinancialArtifactRecord]:
        old_result = self.service.run(self.descriptor)
        corrected_result = self.service.run(self.correction_descriptor)
        old = self.repository.get_filing(old_result.filing_id)
        corrected = self.repository.get_filing(corrected_result.filing_id)
        assert old is not None and corrected is not None
        return old, corrected

    def ingest_missing_taxonomy(self) -> FinancialIngestionResult:
        return self.service.run(self.missing_taxonomy_descriptor)

    def ingest_conflict(self) -> FinancialIngestionResult:
        return self.service.run(self.conflict_descriptor)

    def query(
        self,
        *,
        canonical_names: frozenset[str],
        as_of: datetime,
        known_at: datetime,
    ) -> FinancialQueryResult:
        return self.query_engine.query_financial_facts(
            ts_code=self.descriptor.ts_code,
            report_period=self.descriptor.report_period,
            canonical_fact_names=canonical_names,
            as_of=as_of,
            known_at=known_at,
        )

    def asset_value(self, *, as_of: datetime, known_at: datetime) -> Decimal:
        result = self.query(
            canonical_names=frozenset({"assets"}),
            as_of=as_of,
            known_at=known_at,
        )
        return Decimal(str(result.facts[0]["fact_value"]))

def build_acceptance_system(tmp_path: Path) -> AcceptanceSystem:
    state = initialized_fixture_state(tmp_path)
    repository = FinancialFilingRepository(state.path)
    raw_store = RawObjectStore(tmp_path / "data" / "raw")
    register_fixture_taxonomy(repository, raw_store)
    mapping = fixture_mapping_registry()
    service = fixture_financial_ingestion(
        state=state,
        repository=repository,
        raw_store=raw_store,
        mapping=mapping,
    )
    query = AsOfFinancialQuery(
        repository=repository,
        warehouse_root=tmp_path / "data" / "warehouse" / "financial_facts",
    )
    return AcceptanceSystem(
        service=service,
        query_engine=query,
        repository=repository,
        raw_store=raw_store,
        descriptor=fixture_descriptor(raw_store),
        correction_descriptor=fixture_correction_descriptor(raw_store),
        missing_taxonomy_descriptor=fixture_missing_taxonomy_descriptor(raw_store),
        conflict_descriptor=fixture_conflict_descriptor(raw_store),
        published_at=FIXTURE_PUBLISHED_AT,
        valid_from=FIXTURE_VALID_FROM,
    )
```

Do not add a live connector, PDF parser, production mapping, corporate action,
derived metric, or strategy calculation. If an acceptance test exposes a defect,
first add a focused RED test to the owning Task 1–10 test module, implement the
minimal correction there, rerun that task's GREEN command, and then rerun the
acceptance file.

- [ ] **Step 4: Write the runbook**

Document exact PowerShell commands for:

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\hengce.exe init-state --data-dir data
.venv\Scripts\hengce.exe register-xbrl-taxonomy --help
.venv\Scripts\hengce.exe import-financial-xbrl --help
```

The runbook must state:

- only approved local files are supported;
- parser network access is disabled;
- taxonomy must be registered before filing import;
- source URL and timezone-aware publication/collection times are mandatory;
- missing taxonomy, conflict, unsafe file, and correction-block states;
- where raw objects, SQLite, and Parquet are stored;
- how to run focused and full verification;
- live discovery, PDF fallback, company actions, ratios, strategies, and trading
  instructions are unavailable.

Update the master roadmap's M2 row or adjacent note to link
`docs/superpowers/plans/2026-07-26-xbrl-financial-facts-kernel.md` as the M2a
kernel plan, while retaining the remaining M2 scope as incomplete.

- [ ] **Step 5: Run complete verification and verify GREEN**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest
.venv\Scripts\ruff.exe check src tests
git diff --check codex/market-ui-refresh...HEAD
git status --short
```

Expected:

- all M1 and XBRL tests pass with zero failures;
- Ruff prints `All checks passed!`;
- `git diff --check` has no output;
- only intentional Task 11 files are uncommitted.

- [ ] **Step 6: Verify package installation and CLI help in a clean temporary venv**

```powershell
python -m venv .tmp-package-check
.tmp-package-check\Scripts\python.exe -m pip install .
.tmp-package-check\Scripts\hengce.exe register-xbrl-taxonomy --help
.tmp-package-check\Scripts\hengce.exe import-financial-xbrl --help
```

Expected: wheel installation succeeds and both help commands exit zero without
reading a Token or opening a network connection. After recording the result,
remove only the verified workspace-local environment:

```powershell
$candidate = (Resolve-Path .tmp-package-check).Path
$workspace = (Resolve-Path .).Path
$prefix = $workspace + [IO.Path]::DirectorySeparatorChar
if (-not $candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "refusing cleanup outside workspace"
}
Remove-Item -LiteralPath $candidate -Recurse -Force
```

- [ ] **Step 7: Commit Task 11**

```powershell
git add tests/integration/test_m2_xbrl_acceptance.py docs/runbooks/xbrl-financial-facts.md docs/superpowers/plans/2026-07-24-a-share-research-master-roadmap.md
git commit -m "test: verify XBRL financial facts kernel"
```

---

## Acceptance Traceability

| Acceptance criterion | Primary proving test | Owning tasks |
| --- | --- | --- |
| AC-XF01: parser makes no network request | `test_arelle_parses_numeric_facts_without_any_socket`; `test_xbrl_kernel_is_offline_traceable_and_idempotent` | 4, 11 |
| AC-XF02: facts trace to raw hash and official URL | `test_xbrl_kernel_is_offline_traceable_and_idempotent` | 1, 3, 7, 11 |
| AC-XF03: repeat import is idempotent | `test_repeating_same_descriptor_is_idempotent` plus acceptance replay | 2, 7, 9, 11 |
| AC-XF04: correction switches only at cutoffs or blocks | `test_correction_is_invisible_before_publication_and_visible_after`; `test_public_unusable_correction_blocks_instead_of_falling_back` | 8, 9, 11 |
| AC-XF05: conflicts never enter canonical query | `test_different_values_for_one_identity_create_open_conflict`; acceptance conflict case | 6, 8, 11 |
| AC-XF06: missing taxonomy emits no facts | `test_missing_taxonomy_blocks_without_parser_call` | 2, 4, 9, 11 |
| AC-XF07: publish failure has no partial visibility | `test_crash_after_artifact_write_recovers_without_duplicate`; unregistered-artifact acceptance case | 2, 7, 8, 9, 11 |
| AC-XF08: unmapped QName is retained but not queryable | `test_unmapped_fact_is_preserved_but_unverified`; unmapped acceptance case | 5, 6, 8, 11 |
| AC-XF09: existing M1 suite remains green | every focused regression command and Task 11 full `pytest` | 1–11 |
| AC-XF10: no real issuer fact enters Git/data | fictional fixture assertions and production-data tree scan in acceptance file | 4, 5, 10, 11 |

The Task 11 acceptance file must name each criterion in test docstrings or
parameter IDs so a failed criterion is visible directly in pytest output.

---

## Final Completion Gate

The XBRL kernel is complete only when:

- every task's RED command failed for the intended missing behavior before implementation;
- every task's GREEN command passed after the minimal implementation;
- AC-XF01 through AC-XF10 pass in the acceptance file;
- parser tests prove zero socket access;
- raw objects and prior filing versions remain immutable;
- bitemporal correction replay passes;
- no production QName mapping or real issuer data appears in Git;
- full pytest, Ruff, package installation, CLI help, and `git diff --check` pass;
- the worktree is clean after the final commit.
