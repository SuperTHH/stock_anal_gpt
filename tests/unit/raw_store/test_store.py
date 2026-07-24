from datetime import UTC, datetime
from pathlib import Path

import pytest

from hengce.raw_store.store import RawObjectStore


def test_same_content_is_stored_once_without_overwrite(tmp_path: Path) -> None:
    store = RawObjectStore(tmp_path)
    collected_at = datetime(2026, 7, 24, 21, 31, tzinfo=UTC)
    first = store.put(
        source_id="tushare",
        source_url="http://api.tushare.pro/",
        collected_at=collected_at,
        content_type="application/json",
        payload=b'{"ok":true}',
    )
    second = store.put(
        source_id="tushare",
        source_url="http://api.tushare.pro/",
        collected_at=collected_at,
        content_type="application/json",
        payload=b'{"ok":true}',
    )

    assert first == second
    assert len(list(tmp_path.rglob("payload.bin"))) == 1
    assert len(list(tmp_path.rglob("metadata.json"))) == 1


@pytest.mark.parametrize("source_id", ["../tushare", "tushare/other", "..", "Tushare"])
def test_put_rejects_unsafe_source_id(tmp_path: Path, source_id: str) -> None:
    store = RawObjectStore(tmp_path)

    with pytest.raises(ValueError, match="invalid source_id"):
        store.put(
            source_id=source_id,
            source_url="http://api.tushare.pro/",
            collected_at=datetime(2026, 7, 24, 21, 31, tzinfo=UTC),
            content_type="application/json",
            payload=b"unsafe",
        )


def test_existing_payload_and_metadata_are_never_overwritten(tmp_path: Path) -> None:
    store = RawObjectStore(tmp_path)
    collected_at = datetime(2026, 7, 24, 21, 31, tzinfo=UTC)
    reference = store.put(
        source_id="tushare",
        source_url="http://api.tushare.pro/original",
        collected_at=collected_at,
        content_type="application/json",
        payload=b'{"ok":true}',
    )
    payload_path = Path(reference.payload_path)
    metadata_path = Path(reference.metadata_path)
    original_payload = payload_path.read_bytes()
    original_metadata = metadata_path.read_text(encoding="utf-8")

    repeated = store.put(
        source_id="tushare",
        source_url="http://api.tushare.pro/changed",
        collected_at=collected_at,
        content_type="text/plain",
        payload=b'{"ok":true}',
    )

    assert repeated == reference
    assert payload_path.read_bytes() == original_payload
    assert metadata_path.read_text(encoding="utf-8") == original_metadata
