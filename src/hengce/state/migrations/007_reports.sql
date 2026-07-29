CREATE TABLE IF NOT EXISTS report_snapshots (
    report_id TEXT PRIMARY KEY,
    report_date TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    published_at TEXT NOT NULL,
    report_status TEXT NOT NULL CHECK (
        report_status IN ('PUBLISHED', 'PUBLISHED_PARTIAL')
    )
);

CREATE TABLE IF NOT EXISTS report_pointer (
    pointer_name TEXT PRIMARY KEY CHECK (pointer_name = 'latest'),
    report_id TEXT NOT NULL REFERENCES report_snapshots(report_id) ON DELETE RESTRICT
);
