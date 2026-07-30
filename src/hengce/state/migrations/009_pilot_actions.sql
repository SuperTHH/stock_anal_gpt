CREATE TABLE IF NOT EXISTS corporate_action_versions (
    record_id TEXT PRIMARY KEY,
    ts_code TEXT NOT NULL,
    action_type TEXT NOT NULL,
    supersedes_id TEXT,
    published_at TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    raw_object_hash TEXT NOT NULL,
    quality_status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    FOREIGN KEY(supersedes_id)
        REFERENCES corporate_action_versions(record_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_corporate_action_successor
ON corporate_action_versions(supersedes_id)
WHERE supersedes_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_corporate_action_visible
ON corporate_action_versions(
    ts_code,
    published_at,
    valid_from,
    collected_at,
    record_id
);
