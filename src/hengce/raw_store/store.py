import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class RawObjectRef:
    source_id: str
    content_hash: str
    payload_path: str
    metadata_path: str


class RawObjectStore:
    """Immutable content-addressed payloads plus append-only collection provenance."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def put(
        self,
        *,
        source_id: str,
        source_url: str,
        collected_at: datetime,
        content_type: str,
        payload: bytes,
    ) -> RawObjectRef:
        if not re.fullmatch(r"[a-z0-9_-]+", source_id):
            raise ValueError("invalid source_id")

        content_hash = hashlib.sha256(payload).hexdigest()
        payload_path = self.payload_path_for_hash(content_hash)
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        if not payload_path.exists():
            legacy = self._legacy_payload_path(source_id, collected_at, content_hash)
            candidate = legacy.read_bytes() if legacy.is_file() else payload
            self._publish_if_absent(payload_path, candidate)
        self._validate_payload(payload_path, content_hash)

        event = {
            "source_id": source_id,
            "content_hash": content_hash,
            "source_url": source_url,
            "collected_at": collected_at.isoformat(),
            "content_type": content_type,
            "size_bytes": len(payload),
        }
        event_hash = hashlib.sha256(
            json.dumps(
                event, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        metadata_path = self.root / "provenance" / f"{event_hash}.json"
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        reference = RawObjectRef(
            source_id=source_id,
            content_hash=content_hash,
            payload_path=str(payload_path),
            metadata_path=str(metadata_path),
        )
        metadata = {**asdict(reference), **event}
        encoded_metadata = json.dumps(
            metadata, ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8")
        self._publish_if_absent(metadata_path, encoded_metadata)
        self._validate_metadata(metadata_path, encoded_metadata)
        return reference

    def payload_path_for_hash(self, content_hash: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
            raise ValueError("RAW_PAYLOAD_INTEGRITY_ERROR")
        return self.root / "objects" / content_hash / "payload.bin"

    def validate_content_hash(self, content_hash: str) -> Path:
        payload_path = self.payload_path_for_hash(content_hash)
        self._validate_payload(payload_path, content_hash)
        return payload_path

    def _legacy_payload_path(
        self, source_id: str, collected_at: datetime, content_hash: str
    ) -> Path:
        return (
            self.root
            / source_id
            / collected_at.strftime("%Y")
            / collected_at.strftime("%m")
            / collected_at.strftime("%d")
            / content_hash
            / "payload.bin"
        )

    @staticmethod
    def _validate_payload(path: Path, content_hash: str) -> None:
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise ValueError("RAW_PAYLOAD_INTEGRITY_ERROR") from error
        if actual != content_hash:
            raise ValueError("RAW_PAYLOAD_INTEGRITY_ERROR")

    @staticmethod
    def _validate_metadata(path: Path, expected: bytes) -> None:
        try:
            actual = path.read_bytes()
        except OSError as error:
            raise ValueError("RAW_PROVENANCE_INTEGRITY_ERROR") from error
        if actual != expected:
            raise ValueError("RAW_PROVENANCE_INTEGRITY_ERROR")

    @staticmethod
    def _publish_if_absent(target: Path, content: bytes) -> bool:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", dir=target.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "wb") as temporary_file:
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            try:
                os.link(temporary_path, target)
            except FileExistsError:
                return False
            return True
        finally:
            temporary_path.unlink(missing_ok=True)
