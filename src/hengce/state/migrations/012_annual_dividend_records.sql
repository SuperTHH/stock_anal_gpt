CREATE TABLE IF NOT EXISTS annual_dividend_record_versions (
    record_id TEXT PRIMARY KEY,
    ts_code TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
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
        REFERENCES annual_dividend_record_versions(record_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_annual_dividend_record_successor
ON annual_dividend_record_versions(supersedes_id)
WHERE supersedes_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_annual_dividend_record_visible
ON annual_dividend_record_versions(
    ts_code,
    fiscal_year,
    published_at,
    valid_from,
    collected_at,
    record_id
);
