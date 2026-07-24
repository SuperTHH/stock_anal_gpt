import hashlib
import json
import re
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
        directory = (
            self.root
            / source_id
            / collected_at.strftime("%Y")
            / collected_at.strftime("%m")
            / collected_at.strftime("%d")
            / content_hash
        )
        directory.mkdir(parents=True, exist_ok=True)
        payload_path = directory / "payload.bin"
        metadata_path = directory / "metadata.json"
        reference = RawObjectRef(
            source_id=source_id,
            content_hash=content_hash,
            payload_path=str(payload_path),
            metadata_path=str(metadata_path),
        )

        if not payload_path.exists():
            payload_path.write_bytes(payload)
        if not metadata_path.exists():
            metadata = {
                **asdict(reference),
                "source_url": source_url,
                "collected_at": collected_at.isoformat(),
                "content_type": content_type,
                "size_bytes": len(payload),
            }
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )

        return reference
