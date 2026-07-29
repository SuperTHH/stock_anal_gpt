import hashlib
import json
from copy import deepcopy


def compute_artifact_hash(payload: dict[str, object]) -> str:
    canonical_payload = deepcopy(payload)
    snapshot = canonical_payload.get("snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("REPORT_ARTIFACT_SNAPSHOT_MISSING")
    snapshot.pop("manifest_hash", None)
    canonical = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()
