import tomllib
from pathlib import Path

from hengce.config import Settings


def test_windows_runtime_declares_iana_timezone_database() -> None:
    pyproject = Path(__file__).parents[2] / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]

    assert 'tzdata>=2025.2; platform_system == "Windows"' in project["dependencies"]


def test_gitignore_protects_generated_data_and_capture_artifacts() -> None:
    gitignore = Path(__file__).parents[2] / ".gitignore"
    patterns = set(gitignore.read_text(encoding="utf-8").splitlines())

    assert {
        "*.db",
        "*.parquet",
        "cookie*.json",
        "cookie*.txt",
        "cookies*.json",
        "cookies*.txt",
        "**/cookies/",
        "*.har",
        "*.cookie",
        "*.cookies",
        "session*.json",
        "session*.txt",
        "**/raw/",
        "**/attachments/",
        "**/data-stage/",
    } <= patterns


def test_settings_keep_token_out_of_repr(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, tushare_token="secret-token")
    assert "secret-token" not in repr(settings)


def test_settings_create_only_expected_local_directories(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    settings.ensure_local_dirs()
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "normalized",
        "raw",
        "reports",
        "state",
        "warehouse",
    ]
