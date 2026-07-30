CREATE TABLE IF NOT EXISTS pilot_universes (
    universe_id TEXT PRIMARY KEY,
    market_date TEXT NOT NULL UNIQUE,
    report_cutoff_at TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS acquisition_manifest_items (
    item_id TEXT PRIMARY KEY,
    universe_id TEXT NOT NULL
        REFERENCES pilot_universes(universe_id) ON DELETE RESTRICT,
    ts_code TEXT NOT NULL,
    document_kind TEXT NOT NULL CHECK (
        document_kind IN (
            'PERIODIC_REPORT',
            'DIVIDEND_RECORD',
            'CAPITAL_ACTION_TIMELINE',
            'RISK_SCREEN'
        )
    ),
    report_type TEXT,
    report_period TEXT,
    source_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'PLANNED',
            'DISCOVERED',
            'DOWNLOADED',
            'VERIFIED',
            'INGESTED',
            'AWAITING_MANUAL',
            'REJECTED'
        )
    ),
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_acquisition_manifest_identity
ON acquisition_manifest_items(
    universe_id,
    ts_code,
    document_kind,
    COALESCE(report_type, ''),
    COALESCE(report_period, '')
);

CREATE INDEX IF NOT EXISTS idx_acquisition_manifest_status
ON acquisition_manifest_items(universe_id, status, item_id);

CREATE TABLE IF NOT EXISTS acquisition_transitions (
    transition_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL
        REFERENCES acquisition_manifest_items(item_id) ON DELETE RESTRICT,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    error_code TEXT,
    observed_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_acquisition_transitions_item
ON acquisition_transitions(item_id, observed_at, transition_id);

CREATE TABLE IF NOT EXISTS pipeline_checkpoints (
    run_key TEXT NOT NULL,
    stage_name TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(run_key, stage_name)
);
