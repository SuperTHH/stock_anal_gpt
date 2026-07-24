from pathlib import Path

from hengce.config import Settings


def test_gitignore_protects_generated_data_and_capture_artifacts() -> None:
    gitignore = Path(__file__).parents[2] / ".gitignore"
    patterns = set(gitignore.read_text(encoding="utf-8").splitlines())

    assert {
        "*.db",
        "*.parquet",
        "cookies*.json",
        "cookies*.txt",
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
