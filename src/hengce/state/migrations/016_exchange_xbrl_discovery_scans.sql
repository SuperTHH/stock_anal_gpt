CREATE TABLE IF NOT EXISTS exchange_xbrl_discovery_scans (
    scan_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL CHECK(source_id IN ('sse', 'szse')),
    market_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('AVAILABLE', 'UNAVAILABLE', 'FAILED')),
    scanned_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exchange_xbrl_scan_latest
ON exchange_xbrl_discovery_scans(market_date, source_id, scanned_at DESC, scan_id DESC);
