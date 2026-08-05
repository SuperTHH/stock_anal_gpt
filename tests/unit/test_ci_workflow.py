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
        'python -m pip install -e ".[dev]"',
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
