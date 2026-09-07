CREATE TABLE IF NOT EXISTS official_event_source_scans (
    scan_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    market_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('SUCCESS', 'FAILED')),
    scanned_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_official_event_source_scan_latest
ON official_event_source_scans(market_date, source_id, scanned_at DESC, scan_id DESC);
