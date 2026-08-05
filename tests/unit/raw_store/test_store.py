import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

import hengce.raw_store.store as raw_store_module
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
    assert len(list((tmp_path / "provenance").glob("*.json"))) == 1


def test_identical_payloads_on_distinct_dates_and_sources_share_one_object_and_keep_provenance(
    tmp_path: Path,
) -> None:
    store = RawObjectStore(tmp_path)
    payload = b'{"ok":true}'
    first = store.put(
        source_id="tushare",
        source_url="http://api.tushare.pro/",
        collected_at=datetime(2026, 7, 24, 21, 31, tzinfo=UTC),
        content_type="application/json",
        payload=payload,
    )
    second = store.put(
        source_id="sse",
        source_url="https://www.sse.com.cn/data.json",
        collected_at=datetime(2026, 7, 25, 21, 31, tzinfo=UTC),
        content_type="application/json",
        payload=payload,
    )

    assert first.payload_path == second.payload_path
    assert len(list((tmp_path / "objects").rglob("payload.bin"))) == 1
    assert len(list((tmp_path / "provenance").glob("*.json"))) == 2
    first_metadata = Path(first.metadata_path).read_text(encoding="utf-8")
    second_metadata = Path(second.metadata_path).read_text(encoding="utf-8")
    assert first_metadata != second_metadata


def test_repeated_collection_event_is_idempotent_but_distinct_event_is_retained(
    tmp_path: Path,
) -> None:
    store = RawObjectStore(tmp_path)
    args = {
        "source_id": "tushare",
        "source_url": "http://api.tushare.pro/",
        "collected_at": datetime(2026, 7, 24, 21, 31, tzinfo=UTC),
        "content_type": "application/json",
        "payload": b'{"ok":true}',
    }
    first = store.put(**args)
    repeated = store.put(**args)
    changed = store.put(**{**args, "source_url": "http://api.tushare.pro/v2"})

    assert repeated == first
    assert changed.metadata_path != first.metadata_path
    assert len(list((tmp_path / "provenance").glob("*.json"))) == 2


def test_legacy_date_scoped_payload_is_reconciled_without_mutating_legacy_file(
    tmp_path: Path,
) -> None:
    store = RawObjectStore(tmp_path)
    payload = b'{"ok":true}'
    digest = hashlib.sha256(payload).hexdigest()
    legacy = tmp_path / "tushare" / "2026" / "07" / "24" / digest / "payload.bin"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(payload)

    reference = store.put(
        source_id="tushare",
        source_url="http://api.tushare.pro/",
        collected_at=datetime(2026, 7, 24, 21, 31, tzinfo=UTC),
        content_type="application/json",
        payload=payload,
    )

    assert legacy.read_bytes() == payload
    assert Path(reference.payload_path).read_bytes() == payload
    assert Path(reference.payload_path) != legacy


def test_corrupt_payload_that_wins_publication_race_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawObjectStore(tmp_path)
    payload = b'{"ok":true}'
    original_link = os.link

    def corrupt_winner(source: str, destination: str, *args: object, **kwargs: object) -> None:
        Path(destination).write_bytes(b"corrupt")
        original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(raw_store_module.os, "link", corrupt_winner)

    with pytest.raises(ValueError, match="RAW_PAYLOAD_INTEGRITY_ERROR"):
        store.put(
            source_id="tushare",
            source_url="http://api.tushare.pro/",
            collected_at=datetime(2026, 7, 24, 21, 31, tzinfo=UTC),
            content_type="application/json",
            payload=payload,
        )

    assert not list((tmp_path / "objects").rglob(".*payload.bin.*"))


def test_windows_temp_file_is_cleaned_when_publication_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawObjectStore(tmp_path)

    def reject_link(*args: object, **kwargs: object) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(raw_store_module.os, "link", reject_link)

    with pytest.raises(PermissionError, match="locked"):
        store.put(
            source_id="tushare",
            source_url="http://api.tushare.pro/",
            collected_at=datetime(2026, 7, 24, 21, 31, tzinfo=UTC),
            content_type="application/json",
            payload=b'{"ok":true}',
        )

    assert not list((tmp_path / "objects").rglob(".*payload.bin.*"))


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


def test_existing_payload_and_provenance_are_never_overwritten(tmp_path: Path) -> None:
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

    assert repeated.payload_path == reference.payload_path
    assert repeated.metadata_path != reference.metadata_path
    assert payload_path.read_bytes() == original_payload
    assert metadata_path.read_text(encoding="utf-8") == original_metadata


def test_corrupt_provenance_winner_during_publication_is_rejected_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawObjectStore(tmp_path)
    collected_at = datetime(2026, 7, 24, 21, 31, tzinfo=UTC)
    payload = b'{"ok":true}'
    competing_metadata = '{"writer":"other"}'
    original_link = os.link

    def collide_before_link(source: str, destination: str, *args: object, **kwargs: object) -> None:
        if str(destination).endswith(".json"):
            Path(destination).write_text(competing_metadata, encoding="utf-8")
        original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(raw_store_module.os, "link", collide_before_link)

    with pytest.raises(ValueError, match="RAW_PROVENANCE_INTEGRITY_ERROR"):
        store.put(
            source_id="tushare",
            source_url="http://api.tushare.pro/",
            collected_at=collected_at,
            content_type="application/json",
            payload=payload,
        )

    metadata_path = next((tmp_path / "provenance").glob("*.json"))
    assert metadata_path.read_text(encoding="utf-8") == competing_metadata


def test_corrupt_existing_payload_is_rejected_without_overwrite(tmp_path: Path) -> None:
    store = RawObjectStore(tmp_path)
    collected_at = datetime(2026, 7, 24, 21, 31, tzinfo=UTC)
    payload = b'{"ok":true}'
    digest = hashlib.sha256(payload).hexdigest()
    payload_path = tmp_path / "tushare" / "2026" / "07" / "24" / digest / "payload.bin"
    payload_path.parent.mkdir(parents=True)
    payload_path.write_bytes(b"corrupt")

    with pytest.raises(ValueError, match="RAW_PAYLOAD_INTEGRITY_ERROR"):
        store.put(
            source_id="tushare",
            source_url="http://api.tushare.pro/",
            collected_at=collected_at,
            content_type="application/json",
            payload=payload,
        )

    assert payload_path.read_bytes() == b"corrupt"
