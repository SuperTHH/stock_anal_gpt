# Dual-Source Security Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve independent SSE and SZSE lineage while deriving one validated four-board A-share universe for full-market ingestion.

**Architecture:** Import and persist each exchange as an independent `SecurityMasterSnapshot`. `StateRepository` selects the latest valid snapshot per required source and derives a deterministic `SecurityMasterUniverse`; `MarketIngestionService` consumes only that complete universe. Missing, denied, conflicting, or cross-exchange data blocks ingestion before Tushare is called.

**Tech Stack:** Python 3.12, Pydantic 2, Typer, SQLite, pytest, Ruff.

## Global Constraints

- Only `sse` and `szse` may contribute to the derived universe.
- SSE and SZSE raw/normalized files, URLs, versions, collection times, and hashes remain independent.
- The universe requires both sources; never emit a one-exchange candidate universe.
- Keep only CNY A-shares on `MAIN_SH`, `STAR`, `MAIN_SZ`, and `CHINEXT`.
- Exact source consistency is mandatory: SSE uses `.SH`/`exchange=SSE`; SZSE uses `.SZ`/`exchange=SZSE`.
- Policy Guard must revalidate both component snapshots without consuming a network rate slot.
- No Tushare token may appear in source, tests, logs, CLI output, reports, or Git diffs.
- No real network request, credential, or real sleep is permitted in implementation tests.
- Do not initialize five years until the 2026-07-22 one-day real-data smoke succeeds.

---

## File Structure

- Modify `src/hengce/collectors/security_master.py`: source-specific row validation.
- Modify `src/hengce/state/repository.py`: component selection and deterministic universe derivation.
- Modify `src/hengce/services/market_ingestion.py`: consume the complete universe and classify master failures as blocked.
- Modify `src/hengce/cli.py`: pass `source_id` into the importer and add a local universe-check command.
- Modify `tests/unit/collectors/test_security_master.py`: source mismatch and board/suffix coverage.
- Modify `tests/unit/state/test_repository.py`: per-source latest selection, policy checks, duplicate detection, and hash stability.
- Modify `tests/integration/test_market_ingestion.py`: complete-universe gate and no-fetch failures.
- Modify `tests/unit/test_cli.py`: two-source imports and local universe inspection.
- Modify `docs/runbooks/security-master-import.md`: two-file import and verification workflow.
- Modify `docs/runbooks/m1-data-foundation.md`: make both source snapshots mandatory.

---

### Task 1: Enforce Source-Specific Imports

**Files:**
- Modify: `src/hengce/collectors/security_master.py`
- Modify: `src/hengce/cli.py`
- Modify: `tests/unit/collectors/test_security_master.py`
- Modify: `tests/unit/test_cli.py`
- Create: `tests/fixtures/security_master_sse.csv`
- Create: `tests/fixtures/security_master_szse.csv`

**Interfaces:**
- Consumes: local official normalized CSV and CLI `--source-id`.
- Produces: `OfficialSecurityMasterCsvImporter.parse(path: Path, *, source_id: str) -> list[SecurityMaster]`.
- Error: `SECURITY_MASTER_SOURCE_MISMATCH` for a retained row that does not belong to the declared source.

- [ ] **Step 1: Split the source fixtures**

Create `tests/fixtures/security_master_sse.csv`:

```csv
ts_code,symbol,name,exchange,board,currency,list_date,security_type
600000.SH,600000,示例沪市主板,SSE,MAIN_SH,CNY,19991110,A_SHARE
688001.SH,688001,示例科创板,SSE,STAR,CNY,20190722,A_SHARE
900901.SH,900901,示例B股,SSE,MAIN_SH,USD,19960101,B_SHARE
```

Create `tests/fixtures/security_master_szse.csv`:

```csv
ts_code,symbol,name,exchange,board,currency,list_date,security_type
000001.SZ,000001,示例深市主板,SZSE,MAIN_SZ,CNY,19910403,A_SHARE
300001.SZ,300001,示例创业板,SZSE,CHINEXT,CNY,20091030,A_SHARE
```

- [ ] **Step 2: Write failing importer tests**

Add tests equivalent to:

```python
@pytest.mark.parametrize(
    ("source_id", "fixture", "codes"),
    [
        ("sse", "security_master_sse.csv", ["600000.SH", "688001.SH"]),
        ("szse", "security_master_szse.csv", ["000001.SZ", "300001.SZ"]),
    ],
)
def test_importer_enforces_declared_official_source(
    source_id: str, fixture: str, codes: list[str]
) -> None:
    records = OfficialSecurityMasterCsvImporter().parse(
        Path("tests/fixtures") / fixture,
        source_id=source_id,
    )
    assert [record.ts_code for record in records] == codes


def test_importer_rejects_cross_exchange_in_scope_row(tmp_path: Path) -> None:
    path = tmp_path / "wrong-source.csv"
    path.write_text(
        "ts_code,symbol,name,exchange,board,currency,list_date,security_type\n"
        "000001.SZ,000001,Wrong,SZSE,MAIN_SZ,CNY,19910403,A_SHARE\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="SECURITY_MASTER_SOURCE_MISMATCH"):
        OfficialSecurityMasterCsvImporter().parse(path, source_id="sse")
```

Update existing importer tests to pass an explicit source and keep duplicate/filter coverage.

- [ ] **Step 3: Run the importer tests and verify RED**

Run:

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/collectors/test_security_master.py -q
```

Expected: failures because `parse` does not accept `source_id` and cross-source rows are not rejected.

- [ ] **Step 4: Implement minimal source validation**

Implement these rules:

```python
source_rules = {
    "sse": ("SSE", ".SH", frozenset({"MAIN_SH", "STAR"})),
    "szse": ("SZSE", ".SZ", frozenset({"MAIN_SZ", "CHINEXT"})),
}

def parse(self, path: Path, *, source_id: str) -> list[SecurityMaster]:
    if source_id not in self.source_rules:
        raise ValueError("SECURITY_MASTER_SOURCE_MISMATCH")
    # Parse/filter as today, then validate every retained record.

def _validate_source(self, record: SecurityMaster, source_id: str) -> None:
    exchange, suffix, boards = self.source_rules[source_id]
    if (
        record.exchange != exchange
        or not record.ts_code.endswith(suffix)
        or record.board not in boards
    ):
        raise ValueError("SECURITY_MASTER_SOURCE_MISMATCH")
```

In `import_security_master`, call:

```python
records = OfficialSecurityMasterCsvImporter().parse(file, source_id=source_id)
```

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/collectors/test_security_master.py tests/unit/test_cli.py -q
.venv\Scripts\ruff.exe check src/hengce/collectors/security_master.py src/hengce/cli.py tests/unit/collectors tests/unit/test_cli.py
```

Expected: all selected tests pass and Ruff reports `All checks passed!`.

- [ ] **Step 6: Commit Task 1**

```powershell
git add src/hengce/collectors/security_master.py src/hengce/cli.py tests/unit/collectors/test_security_master.py tests/unit/test_cli.py tests/fixtures/security_master_sse.csv tests/fixtures/security_master_szse.csv
git commit -m "fix: enforce security master source identity"
```

---

### Task 2: Derive a Complete Auditable Universe

**Files:**
- Modify: `src/hengce/state/repository.py`
- Modify: `tests/unit/state/test_repository.py`

**Interfaces:**
- Consumes: latest persisted `SecurityMasterSnapshot` for `sse` and `szse`.
- Produces: `StateRepository.get_latest_security_master_snapshot(source_id: str) -> SecurityMasterSnapshot | None`.
- Produces: `StateRepository.get_security_master_universe() -> SecurityMasterUniverse`.
- `SecurityMasterUniverse` fields: `components`, `securities`, `as_of`, `universe_hash`, `quality_status`.

- [ ] **Step 1: Write failing component-selection tests**

Add these exact helpers to `tests/unit/state/test_repository.py`:

```python
def exchange_policy(source_id: str, *, enabled: bool = True) -> SourcePolicy:
    if source_id == "sse":
        return security_master_policy(enabled=enabled)
    return security_master_policy(
        source_id="szse",
        source_name="SZSE",
        allowed_domains=["szse.cn", "www.szse.cn"],
        terms_url="https://www.szse.cn/application/laws/",
        enabled=enabled,
    )


def save_exchange_snapshot(
    repository: StateRepository,
    source_id: str,
    *,
    content_hash: str,
    version: str,
    collected_hour: int,
    securities: list[SecurityMaster],
) -> SecurityMasterSnapshot:
    domain = "www.sse.com.cn" if source_id == "sse" else "www.szse.cn"
    return repository.save_security_master_snapshot(
        securities,
        source_id=source_id,
        source_url=f"https://{domain}/master.csv",
        collected_at=datetime(2026, 7, 24, collected_hour, tzinfo=UTC),
        content_hash=content_hash,
        version=version,
        quality_lineage={"filter": "a_share_cny_four_boards"},
    )


def test_universe_selects_latest_snapshot_per_required_source(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    repository.upsert_policies([exchange_policy("sse"), exchange_policy("szse")])
    old_sse = save_exchange_snapshot(
        repository, "sse", content_hash="1" * 64, version="sse-old",
        collected_hour=8, securities=[security("600000.SH", "MAIN_SH")],
    )
    new_sse = save_exchange_snapshot(
        repository, "sse", content_hash="2" * 64, version="sse-new",
        collected_hour=10,
        securities=[security("600000.SH", "MAIN_SH"), security("688001.SH", "STAR")],
    )
    szse = save_exchange_snapshot(
        repository, "szse", content_hash="3" * 64, version="szse-only",
        collected_hour=11,
        securities=[security("000001.SZ", "MAIN_SZ"), security("300001.SZ", "CHINEXT")],
    )

    universe = repository.get_security_master_universe()

    assert [item.version for item in universe.components] == ["sse-new", "szse-only"]
    assert [item.ts_code for item in universe.securities] == [
        "000001.SZ", "300001.SZ", "600000.SH", "688001.SH"
    ]
    assert universe.as_of == min(new_sse.collected_at, szse.collected_at)
    assert old_sse not in universe.components
```

- [ ] **Step 2: Write failing missing/conflict/policy tests**

For each case, create a fresh migrated repository, upsert both policies, and use
`save_exchange_snapshot` above. Add these exact assertions after arranging the named repository:

```python
with pytest.raises(ValueError, match="SECURITY_MASTER_SZSE_UNAVAILABLE"):
    only_sse.get_security_master_universe()

with pytest.raises(ValueError, match="SECURITY_MASTER_DUPLICATE_TS_CODE"):
    duplicate_components.get_security_master_universe()

with pytest.raises(ValueError, match="SECURITY_MASTER_POLICY_DENIED"):
    disabled_sse.get_security_master_universe()

assert disabled_sse.count_refusals() == 1
```

Add a deterministic-hash test:

```python
first = repository.get_security_master_universe()
second = repository.get_security_master_universe()
assert first.universe_hash == second.universe_hash
assert len(first.universe_hash) == 64
```

- [ ] **Step 3: Run repository tests and verify RED**

Run:

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/state/test_repository.py -q
```

Expected: failures because source-specific selection and `SecurityMasterUniverse` do not exist.

- [ ] **Step 4: Implement the universe type and source-specific selection**

Add:

```python
@dataclass(frozen=True)
class SecurityMasterUniverse:
    components: tuple[SecurityMasterSnapshot, SecurityMasterSnapshot]
    securities: list[SecurityMaster]
    as_of: datetime
    universe_hash: str
    quality_status: QualityStatus
```

Change the existing newest-snapshot query to require a source:

```sql
SELECT * FROM security_master_snapshots
WHERE source_id=?
ORDER BY collected_at DESC, snapshot_id DESC
LIMIT 1
```

Do not silently skip an invalid newest snapshot. Validate its members and Policy Guard; translate a `PolicyDenied` into `SECURITY_MASTER_POLICY_DENIED`.

- [ ] **Step 5: Implement deterministic universe derivation**

Use fixed required-source order `("sse", "szse")`. Raise:

```python
missing_errors = {
    "sse": "SECURITY_MASTER_SSE_UNAVAILABLE",
    "szse": "SECURITY_MASTER_SZSE_UNAVAILABLE",
}
```

Build the hash from canonical JSON:

```python
identity = [
    {
        "source_id": component.source_id,
        "version": component.version,
        "content_hash": component.content_hash,
        "collected_at": component.collected_at.isoformat(),
    }
    for component in components
]
universe_hash = hashlib.sha256(
    json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
```

Before returning, sort by `ts_code`, reject duplicate codes, and set `QualityStatus.VALID`.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run:

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/state/test_repository.py tests/unit/policy/test_guard.py -q
.venv\Scripts\ruff.exe check src/hengce/state/repository.py tests/unit/state/test_repository.py
```

Expected: selected tests pass and Ruff is clean.

- [ ] **Step 7: Commit Task 2**

```powershell
git add src/hengce/state/repository.py tests/unit/state/test_repository.py
git commit -m "feat: derive dual-source security universe"
```

---

### Task 3: Gate Market Ingestion on the Complete Universe

**Files:**
- Modify: `src/hengce/services/market_ingestion.py`
- Modify: `tests/integration/test_market_ingestion.py`

**Interfaces:**
- Consumes: `StateRepository.get_security_master_universe()`.
- Produces: the existing `MarketIngestionService.run(date) -> MarketIngestionResult`.
- Failure status: all `SECURITY_MASTER_*` universe errors create one terminal `BLOCKED` RunRecord before re-raising.

- [ ] **Step 1: Replace the single-source test seed helper**

Create separate helpers:

```python
def _seed_complete_security_universe(
    repository: StateRepository,
    *,
    sse_codes: tuple[str, ...] = ("600000.SH",),
    szse_codes: tuple[str, ...] = ("000001.SZ",),
) -> None:
    # Persist approved SSE and SZSE policies and one snapshot per source.
```

Use the complete helper in every test that is meant to reach the collector.

- [ ] **Step 2: Write failing integration tests**

Add:

```python
@pytest.mark.parametrize(
    ("missing_source", "error_code"),
    [
        ("sse", "SECURITY_MASTER_SSE_UNAVAILABLE"),
        ("szse", "SECURITY_MASTER_SZSE_UNAVAILABLE"),
    ],
)
def test_incomplete_universe_blocks_before_tushare_fetch(
    tmp_path: Path, missing_source: str, error_code: str
) -> None:
    # Seed only the opposite source.
    with pytest.raises(ValueError, match=error_code):
        service.run(date(2026, 7, 22))
    assert collector.calls == 0
    assert latest_run.run_status == RunStatus.BLOCKED
    assert latest_run.error_code == error_code
```

Add one success test containing one `.SH` and one `.SZ` bar and assert the collector is called once and both rows are persisted.

- [ ] **Step 3: Run integration tests and verify RED**

Run:

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/integration/test_market_ingestion.py -q
```

Expected: failures because ingestion still uses one latest snapshot and only recognizes the legacy unavailable code.

- [ ] **Step 4: Integrate the universe and blocked classification**

Replace:

```python
security_master = self.state.get_latest_security_master_snapshot()
```

with:

```python
security_universe = self.state.get_security_master_universe()
approved_codes = {security.ts_code for security in security_universe.securities}
```

Add:

```python
@staticmethod
def _is_security_master_error(error: Exception) -> bool:
    return MarketIngestionService._error_code(error).startswith("SECURITY_MASTER_")
```

Use this predicate in the no-lease exception path so missing, denied, duplicate, and invalid universes are `BLOCKED`, not `FAILED`.

- [ ] **Step 5: Run integration and lifecycle regressions**

Run:

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/integration/test_market_ingestion.py tests/unit/state/test_repository.py -q
.venv\Scripts\ruff.exe check src/hengce/services/market_ingestion.py tests/integration/test_market_ingestion.py
```

Expected: all selected tests pass, including prior lease/crash/concurrency tests.

- [ ] **Step 6: Commit Task 3**

```powershell
git add src/hengce/services/market_ingestion.py tests/integration/test_market_ingestion.py
git commit -m "feat: require complete security universe"
```

---

### Task 4: Add a Local Universe Readiness Command

**Files:**
- Modify: `src/hengce/cli.py`
- Modify: `tests/unit/test_cli.py`

**Interfaces:**
- Produces: `hengce check-security-universe --data-dir <path>`.
- Output: deterministic JSON with `as_of`, `component_count`, `components`, `security_count`, and `universe_hash`.
- Makes no HTTP request and does not require `HENGCE_TUSHARE_TOKEN`.

- [ ] **Step 1: Write the failing CLI test**

Add a test that imports one SSE and one SZSE fixture through the existing CLI, then runs:

```python
result = runner.invoke(
    app,
    ["check-security-universe", "--data-dir", str(tmp_path)],
)
assert result.exit_code == 0
output = json.loads(result.stdout)
assert len(output.pop("universe_hash")) == 64
assert output == {
    "as_of": "2026-07-24T09:00:00+00:00",
    "component_count": 2,
    "components": [
        {"source_id": "sse", "version": "2026-07-24-sse"},
        {"source_id": "szse", "version": "2026-07-24-szse"},
    ],
    "security_count": 4,
}
assert not client_factory.mock_calls
```

- [ ] **Step 2: Run the CLI test and verify RED**

Run:

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/test_cli.py -q
```

Expected: failure because `check-security-universe` is not registered.

- [ ] **Step 3: Implement the command**

Add a Typer command that calls `bootstrap_state`, derives the universe, and emits sorted JSON. Do not instantiate `httpx.Client`; do not load or print the token.

- [ ] **Step 4: Run CLI and wheel regressions**

Run:

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit/test_cli.py -q
.venv\Scripts\ruff.exe check src/hengce/cli.py tests/unit/test_cli.py
```

Expected: the new command, generated `hengce.exe` wheel smoke, and existing commands all pass.

- [ ] **Step 5: Commit Task 4**

```powershell
git add src/hengce/cli.py tests/unit/test_cli.py
git commit -m "feat: add security universe readiness check"
```

---

### Task 5: Update the Operator Runbooks

**Files:**
- Modify: `docs/runbooks/security-master-import.md`
- Modify: `docs/runbooks/m1-data-foundation.md`

**Interfaces:**
- Consumes: two normalized local CSVs and their exact approved official source URLs.
- Produces: a reproducible no-secret preparation and import sequence.

- [ ] **Step 1: Update the import runbook**

Document these commands with real operator placeholders expressed as shell variables, not invented URLs:

```powershell
hengce import-security-master --file $sseCsv --source-id sse --source-url $sseSourceUrl --version $sseVersion --collected-at $sseCollectedAt --data-dir data
hengce import-security-master --file $szseCsv --source-id szse --source-url $szseSourceUrl --version $szseVersion --collected-at $szseCollectedAt --data-dir data
hengce check-security-universe --data-dir data
```

State explicitly that a combined hand-edited CSV is prohibited and either missing source blocks ingestion.

- [ ] **Step 2: Document secure token setup**

Add:

```text
Copy `.env.example` to `.env`, enter `HENGCE_TUSHARE_TOKEN` locally, and never paste
the token into chat, command arguments, logs, screenshots, or committed files.
```

Document a presence-only verification command that does not print the value:

```powershell
$line = Get-Content .env | Where-Object { $_ -match '^HENGCE_TUSHARE_TOKEN=.+' }
if ($line) { 'token=present' } else { 'token=missing' }
```

- [ ] **Step 3: Run documentation checks**

Run:

```powershell
rg -n "combined|两份|check-security-universe|HENGCE_TUSHARE_TOKEN" docs/runbooks
git diff --check
```

Expected: both runbooks require two source files, contain the local readiness command, and have no whitespace errors.

- [ ] **Step 4: Commit Task 5**

```powershell
git add docs/runbooks/security-master-import.md docs/runbooks/m1-data-foundation.md
git commit -m "docs: require both exchange master snapshots"
```

---

### Task 6: Full Verification and Real-Smoke Readiness

**Files:**
- Verify only; do not modify production files unless a failing test identifies a scoped defect.
- Write ignored evidence: `.superpowers/sdd/dual-source-security-master-report.md`.

**Interfaces:**
- Produces: a clean reviewed commit ready for local official files and the user-entered token.

- [ ] **Step 1: Run the complete offline suite**

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/unit tests/integration -q
.venv\Scripts\ruff.exe check src tests
git diff --check ae7ff86..HEAD
git status --short
```

Expected: all tests pass, Ruff is clean, diff check is clean, and the worktree has no uncommitted tracked changes.

- [ ] **Step 2: Scan the diff for credentials without printing values**

```powershell
$diffText = git diff ae7ff86..HEAD -- . ':!tests'
$patterns = @(
  '(?i)(token|secret|password|api[_-]?key)\s*[=:]\s*["''][^"'']{8,}["'']',
  '(?i)bearer\s+[A-Za-z0-9._-]{12,}'
)
$hitCount = 0
foreach ($pattern in $patterns) {
  $hitCount += ([regex]::Matches(($diffText -join "`n"), $pattern)).Count
}
"credential_pattern_hits=$hitCount"
```

Expected: `credential_pattern_hits=0`.

- [ ] **Step 3: Request an independent scoped review**

The reviewer must verify:

- both components preserve independent lineage;
- latest selection is per source;
- source mismatch is rejected before persistence;
- Policy Guard validates both components without rate reservation;
- one missing source blocks before Tushare;
- duplicate codes never overwrite silently;
- `universe_hash` is deterministic;
- no token or official full text is committed.

Fix every Critical or Important finding using a new RED→GREEN test loop before proceeding.

- [ ] **Step 4: Prepare—but do not fake—the real inputs**

After review approval:

1. Create ignored directories `data/incoming/sse` and `data/incoming/szse`.
2. Locate only official public SSE/SZSE download pages.
3. If either site requires CAPTCHA, login, or manual confirmation, stop and ask the user to download the file.
4. Preserve each original file, source-page URL, retrieval time, SHA-256, and conversion record.
5. Normalize each source independently to the eight required CSV columns.
6. Create `.env` from `.env.example`, open it locally, and let the user enter the token without exposing it to chat or terminal output.
7. Run `check-security-universe`.

- [ ] **Step 5: Execute the one-day smoke only when readiness passes**

Run:

```powershell
hengce ingest-market --trade-date 2026-07-22 --data-dir data
```

Accept only when:

- exactly one Tushare `daily` request is made for `20260722`;
- both `.SH` and `.SZ` response codes are within the derived universe;
- Raw, Parquet, checkpoint, run record, and both source lineages are present;
- no multiple-artifact, partial-universe, or incomplete-candidate condition exists.

Do not start five-year initialization until this smoke passes.
