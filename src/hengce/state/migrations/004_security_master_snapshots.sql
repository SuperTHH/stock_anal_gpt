CREATE TABLE IF NOT EXISTS security_master_snapshots (
    snapshot_id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    version TEXT NOT NULL,
    quality_lineage_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_id, content_hash, collected_at, version)
);

CREATE TABLE IF NOT EXISTS security_master_members (
    snapshot_id INTEGER NOT NULL REFERENCES security_master_snapshots(snapshot_id) ON DELETE RESTRICT,
    ts_code TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, ts_code)
);
