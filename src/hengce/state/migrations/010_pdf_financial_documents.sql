CREATE TABLE IF NOT EXISTS pdf_financial_documents (
    filing_id TEXT PRIMARY KEY,
    ts_code TEXT NOT NULL,
    report_period TEXT NOT NULL,
    published_at TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    supersedes_id TEXT,
    quality_status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    FOREIGN KEY(supersedes_id)
        REFERENCES pdf_financial_documents(filing_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pdf_financial_successor
ON pdf_financial_documents(supersedes_id)
WHERE supersedes_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_pdf_financial_visible
ON pdf_financial_documents(
    ts_code,
    report_period,
    published_at,
    valid_from,
    filing_id
);
