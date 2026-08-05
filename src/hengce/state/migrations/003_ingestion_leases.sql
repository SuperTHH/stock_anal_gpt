CREATE TABLE IF NOT EXISTS ingestion_leases (
    trade_date TEXT PRIMARY KEY,
    owner_id TEXT,
    lease_expires_at REAL,
    lifecycle_state TEXT NOT NULL,
    staged_result_json TEXT,
    updated_at TEXT NOT NULL
);
