CREATE TABLE IF NOT EXISTS taxonomy_packages (
    taxonomy_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    raw_object_hash TEXT NOT NULL,
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS financial_filings (
    filing_id TEXT PRIMARY KEY,
    ts_code TEXT NOT NULL,
    report_period TEXT NOT NULL,
    published_at TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    supersedes_id TEXT REFERENCES financial_filings(filing_id) ON DELETE RESTRICT,
    payload_json TEXT NOT NULL,
    artifact_status TEXT NOT NULL CHECK (artifact_status IN ('PENDING', 'PUBLISHED')),
    expected_path TEXT NOT NULL,
    expected_hash TEXT NOT NULL,
    expected_count INTEGER NOT NULL CHECK (expected_count >= 0),
    artifact_published_at TEXT,
    UNIQUE(ts_code, report_period, filing_id)
);

CREATE INDEX IF NOT EXISTS idx_financial_filings_lookup
ON financial_filings(ts_code, report_period, published_at, valid_from);

CREATE TABLE IF NOT EXISTS financial_fact_conflicts (
    conflict_id TEXT PRIMARY KEY,
    filing_id TEXT NOT NULL REFERENCES financial_filings(filing_id) ON DELETE RESTRICT,
    payload_json TEXT NOT NULL,
    detected_at TEXT NOT NULL
);
