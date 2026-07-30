from pathlib import Path

from fastapi.testclient import TestClient

from hengce.api.local import create_local_app


def test_local_api_factory_reads_only_the_selected_data_directory(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"

    response = TestClient(create_local_app(data_dir)).get(
        "/api/reports/latest"
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "NO_PUBLISHED_REPORT"}
    assert not data_dir.exists()


def test_local_api_treats_unmigrated_existing_database_as_no_report(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "state" / "hengce.sqlite3"
    database.parent.mkdir(parents=True)
    database.touch()

    response = TestClient(create_local_app(data_dir)).get(
        "/api/reports/latest"
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "NO_PUBLISHED_REPORT"}
