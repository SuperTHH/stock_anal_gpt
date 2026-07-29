# CI Baseline and PR #2 Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an offline, dual-operating-system GitHub Actions quality gate, require it to pass for the exact PR #2 head SHA, squash-merge the PR into `codex/market-ui-refresh`, and verify the merged baseline.

**Architecture:** One workflow separates Python tests, static checks, and frontend checks so failures are attributable and rerunnable. A repository test freezes the workflow's security and coverage invariants before the YAML exists. GitHub checks are treated as evidence for one immutable head SHA; the merge is performed only after all jobs succeed and the target branch is then verified from its dedicated worktree.

**Tech Stack:** GitHub Actions, Python 3.12, Pytest, Ruff, Node.js 22, npm, Vitest, TypeScript, Vite, GitHub REST API, Git.

## Global Constraints

- CI must not receive or reference Tushare tokens, cookies, GitHub write tokens, databases, Parquet files, downloaded attachments, or real raw responses.
- Python tests run on both `ubuntu-latest` and `windows-latest`; Ruff and frontend checks run once on Ubuntu.
- CI uses Python 3.12 and Node.js 22 only.
- All CI tests remain offline and use committed fixtures or injected transports.
- Workflow permissions are limited to `contents: read`.
- Every job has a 30-minute timeout.
- A failed, cancelled, skipped, stale, or missing required job blocks the merge.
- PR #2 is merged with Squash merge only when its verified head SHA still equals the merge request head SHA.
- The source branch and both existing worktrees remain in place after merge verification.
- No implementation of real data connectors is included in this plan.

---

### Task 1: Freeze the CI workflow contract

**Files:**
- Create: `tests/unit/test_ci_workflow.py`
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `pyproject.toml`, `apps/web/package-lock.json`, existing offline Pytest and Vitest suites.
- Produces: `.github/workflows/ci.yml` with jobs `python-tests`, `static-checks`, and `frontend`; `test_ci_workflow_contract()` guards its required commands and security boundary.

- [ ] **Step 1: Write the failing workflow contract test**

```python
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_workflow_contract() -> None:
    assert WORKFLOW.is_file(), "CI workflow must exist"
    workflow = WORKFLOW.read_text(encoding="utf-8")

    required_fragments = (
        "contents: read",
        "ubuntu-latest",
        "windows-latest",
        'python-version: "3.12"',
        "python -m pip install -e \".[dev]\"",
        "python -m pytest",
        "python -m ruff check .",
        "git diff --check",
        'node-version: "22"',
        "cache-dependency-path: apps/web/package-lock.json",
        "npm ci",
        "npm test",
        "npm run build",
        "timeout-minutes: 30",
        "cancel-in-progress: true",
        "fetch-depth: 0",
    )
    for fragment in required_fragments:
        assert fragment in workflow

    forbidden_fragments = (
        "TUSHARE_TOKEN",
        "secrets.",
        "pull_request_target",
        "permissions: write-all",
    )
    for fragment in forbidden_fragments:
        assert fragment not in workflow
```

- [ ] **Step 2: Run the contract test and verify RED**

Run:

```text
python -m pytest tests/unit/test_ci_workflow.py -v
```

Expected: FAIL at `assert WORKFLOW.is_file()` because `.github/workflows/ci.yml` does not exist.

- [ ] **Step 3: Add the minimal workflow**

```yaml
name: CI

on:
  pull_request:
  push:
    branches:
      - main
      - codex/market-ui-refresh
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  python-tests:
    name: Python 3.12 / ${{ matrix.os }}
    runs-on: ${{ matrix.os }}
    timeout-minutes: 30
    strategy:
      fail-fast: false
      matrix:
        os:
          - ubuntu-latest
          - windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
      - run: python -m pip install --upgrade pip
      - run: python -m pip install -e ".[dev]"
      - run: python -m pytest

  static-checks:
    name: Static checks
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
      - run: python -m pip install --upgrade pip
      - run: python -m pip install -e ".[dev]"
      - run: python -m ruff check .
      - name: Check changed lines
        shell: bash
        run: |
          if [ "${{ github.event_name }}" = "pull_request" ]; then
            base="$(git merge-base HEAD "origin/${{ github.base_ref }}")"
            git diff --check "$base"...HEAD
          else
            git diff --check HEAD^ HEAD
          fi

  frontend:
    name: Frontend
    runs-on: ubuntu-latest
    timeout-minutes: 30
    defaults:
      run:
        working-directory: apps/web
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "22"
          cache: npm
          cache-dependency-path: apps/web/package-lock.json
      - run: npm ci
      - run: npm test
      - run: npm run build
```

- [ ] **Step 4: Run the contract test and verify GREEN**

Run:

```text
python -m pytest tests/unit/test_ci_workflow.py -v
```

Expected: `1 passed`.

- [ ] **Step 5: Run focused static validation**

Run:

```text
python -m ruff check tests/unit/test_ci_workflow.py
git diff --check
```

Expected: both commands exit 0 with no violations.

- [ ] **Step 6: Commit the workflow contract and implementation**

```text
git add .github/workflows/ci.yml tests/unit/test_ci_workflow.py
git commit -m "ci: add dual-platform quality gate"
```

Expected: one commit containing only the workflow and its contract test.

### Task 2: Verify locally and publish the exact PR head

**Files:**
- Verify: entire repository
- Modify only if a test exposes a defect, following a new RED-GREEN cycle.

**Interfaces:**
- Consumes: Task 1 workflow and existing test suites.
- Produces: one pushed PR head SHA with complete local verification evidence.

- [ ] **Step 1: Run the full Python suite**

Run:

```text
python -m pytest
```

Expected: all tests pass; the Windows symlink test may be the single documented platform skip.

- [ ] **Step 2: Run repository lint and whitespace checks**

Run:

```text
python -m ruff check .
git diff --check
```

Expected: both commands exit 0.

- [ ] **Step 3: Run the frontend suite**

From `apps/web` run:

```text
npm test
npm run build
```

Expected: all Vitest tests pass and Vite production build exits 0.

- [ ] **Step 4: Confirm repository hygiene**

Run:

```text
git status --short
git ls-files | rg '(^|/)(\.env|data/)|\.(sqlite3?|parquet|zip|xbrl)$'
```

Expected: no uncommitted implementation changes and no tracked secret/data artifacts. Documentation commits may be ahead of the remote until Step 5.

- [ ] **Step 5: Push the current feature branch**

Run:

```text
git push origin codex/xbrl-financial-facts
```

Expected: remote branch advances to the exact local `HEAD`.

- [ ] **Step 6: Verify SHA equality**

Run:

```text
git rev-parse HEAD
git rev-parse origin/codex/xbrl-financial-facts
```

Expected: identical full SHA values.

### Task 3: Require GitHub Actions success for the immutable head

**Files:**
- No repository files unless a failing check requires a TDD fix.

**Interfaces:**
- Consumes: GitHub PR #2 and the exact Task 2 head SHA.
- Produces: recorded check-run names, conclusions, URLs, and confirmation that the PR remains clean for the same SHA.

- [ ] **Step 1: Read the PR head and check runs**

Call:

```text
GET /repos/SuperTHH/stock_anal_gpt/pulls/2
GET /repos/SuperTHH/stock_anal_gpt/commits/{head_sha}/check-runs
```

Expected: PR head equals the locally recorded SHA and check runs appear for both Python matrix entries, static checks, and frontend.

- [ ] **Step 2: Wait with bounded polling**

Poll the check-runs endpoint at a moderate interval until all jobs for the recorded SHA are completed or a 30-minute deadline is reached. Do not treat missing, queued, in-progress, cancelled, skipped, stale, or neutral checks as success.

Expected successful conclusions:

```text
Python 3.12 / ubuntu-latest: success
Python 3.12 / windows-latest: success
Static checks: success
Frontend: success
```

- [ ] **Step 3: Handle failures without weakening the gate**

If a check fails:

1. read only that check's annotations/log link;
2. reproduce locally where possible;
3. write a failing regression test or workflow contract assertion;
4. verify RED;
5. implement the minimal fix;
6. verify GREEN and the full local suite;
7. commit, push, record the new head SHA, and restart Task 3.

Do not add secrets, network credentials, `continue-on-error`, platform exclusions, or broad test skips.

- [ ] **Step 4: Re-read merge prerequisites**

Call:

```text
GET /repos/SuperTHH/stock_anal_gpt/pulls/2
GET /repos/SuperTHH/stock_anal_gpt/pulls/2/reviews
GET /repos/SuperTHH/stock_anal_gpt/pulls/2/comments
```

Expected: state `open`, mergeable state `clean`, unchanged head SHA, no unresolved review comments, and all required check runs successful.

### Task 4: Squash-merge PR #2

**Files:**
- No repository files.

**Interfaces:**
- Consumes: the immutable, fully successful PR head from Task 3.
- Produces: one squash commit on `codex/market-ui-refresh` and a merged PR #2.

- [ ] **Step 1: Submit the squash merge**

Call:

```text
PUT /repos/SuperTHH/stock_anal_gpt/pulls/2/merge
```

Body:

```powershell
$verifiedHeadSha = $pr.head.sha
$mergePayload = @{
    merge_method = "squash"
    sha = $verifiedHeadSha
    commit_title = "feat: complete A-share research data, strategies, and local UI (#2)"
    commit_message = "Complete the offline financial facts kernel, corporate-action and total-return derivation, point-in-time financial metrics, three independent strategy pools, immutable report publishing, local read-only API, five-page React UI, explicit fictional demo mode, and dual-platform CI quality gate."
}
```

Expected: response has `merged=true` and returns `sha`.

- [ ] **Step 2: Verify the PR and target branch**

Call:

```text
GET /repos/SuperTHH/stock_anal_gpt/pulls/2
GET /repos/SuperTHH/stock_anal_gpt/branches/codex/market-ui-refresh
```

Expected: PR state `closed`, `merged=true`, and target branch head equals the returned squash SHA.

### Task 5: Verify the merged baseline from the target worktree

**Files:**
- Worktree: `.worktrees/market-ui-refresh`
- No intended source edits.

**Interfaces:**
- Consumes: the remote squash SHA from Task 4.
- Produces: local target branch at the same SHA with fresh backend, frontend, lint, build, and hygiene evidence.

- [ ] **Step 1: Confirm the target worktree is safe to update**

Run:

```text
git -C .worktrees/market-ui-refresh status --short --branch
```

Expected: no local modifications. If dirty, stop without moving or overwriting user files.

- [ ] **Step 2: Fetch and fast-forward the target branch**

Run:

```text
git fetch origin codex/market-ui-refresh
git -C .worktrees/market-ui-refresh merge --ff-only origin/codex/market-ui-refresh
```

Expected: local target branch advances to the squash SHA without a merge commit.

- [ ] **Step 3: Run merged Python verification**

Run from `.worktrees/market-ui-refresh`:

```text
python -m pytest
python -m ruff check .
git diff --check
```

Expected: full tests pass, Ruff exits 0, and diff check reports no errors.

- [ ] **Step 4: Run merged frontend verification**

Run from `.worktrees/market-ui-refresh/apps/web`:

```text
npm ci
npm test
npm run build
```

Expected: dependency lock is honored, all tests pass, and production build exits 0.

- [ ] **Step 5: Record final repository state**

Run:

```text
git -C .worktrees/market-ui-refresh rev-parse HEAD
git -C .worktrees/market-ui-refresh rev-parse origin/codex/market-ui-refresh
git -C .worktrees/market-ui-refresh status --short
```

Expected: local and remote SHAs equal the squash SHA and the worktree is clean.
