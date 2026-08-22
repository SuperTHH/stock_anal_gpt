CREATE TABLE IF NOT EXISTS full_market_evidence_runs (
    run_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    market_date TEXT NOT NULL,
    cohort TEXT NOT NULL CHECK (cohort IN ('YIELD_GE_5', 'YIELD_3_TO_5', 'LIQUIDITY_FILL')),
    config_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(snapshot_id, cohort)
);

CREATE TABLE IF NOT EXISTS full_market_evidence_tasks (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES full_market_evidence_runs(run_id) ON DELETE RESTRICT,
    ts_code TEXT NOT NULL,
    evidence_kind TEXT NOT NULL CHECK (
        evidence_kind IN ('PERIODIC_REPORT', 'DIVIDEND_YEAR', 'RISK_SCREEN', 'CORPORATE_ACTION')
    ),
    evidence_period TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'PLANNED', 'DISCOVERED', 'DOWNLOADED', 'PARSED',
            'AWAITING_REVIEW', 'SATISFIED', 'RETRYABLE_FAILED', 'BLOCKED'
        )
    ),
    version INTEGER NOT NULL CHECK (version >= 1),
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, ts_code, evidence_kind, evidence_period)
);

CREATE INDEX IF NOT EXISTS idx_full_market_evidence_tasks_queue
ON full_market_evidence_tasks(run_id, status, evidence_kind, ts_code, evidence_period);

CREATE TABLE IF NOT EXISTS full_market_evidence_transitions (
    transition_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES full_market_evidence_tasks(task_id) ON DELETE RESTRICT,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    from_version INTEGER NOT NULL,
    to_version INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS full_market_review_decisions (
    decision_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES full_market_evidence_tasks(task_id) ON DELETE RESTRICT,
    task_version INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('CONFIRM', 'RETURN')),
    reviewed_values_json TEXT NOT NULL,
    note TEXT NOT NULL,
    reviewed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_full_market_review_task
ON full_market_review_decisions(task_id, reviewed_at, decision_id);
