CREATE TABLE IF NOT EXISTS rate_reservations (
    source_id TEXT PRIMARY KEY,
    next_allowed_at REAL NOT NULL,
    updated_at TEXT NOT NULL
);
