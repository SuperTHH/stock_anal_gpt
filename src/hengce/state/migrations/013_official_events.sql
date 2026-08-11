CREATE TABLE IF NOT EXISTS official_event_versions (
    record_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    supersedes_id TEXT,
    published_at TEXT NOT NULL,
    effective_at TEXT,
    collected_at TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    raw_object_hash TEXT NOT NULL,
    quality_status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    FOREIGN KEY(supersedes_id)
        REFERENCES official_event_versions(record_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_official_event_successor
ON official_event_versions(supersedes_id)
WHERE supersedes_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_official_event_visible
ON official_event_versions(
    published_at,
    effective_at,
    valid_from,
    collected_at,
    record_id
);
