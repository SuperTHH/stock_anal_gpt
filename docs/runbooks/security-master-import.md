# Local security-master import

Prepare two independent normalized CSV files: one from SSE and one from SZSE. For each
file, retain its exact approved official source URL. Do not invent, shorten, substitute,
or infer a URL. A combined or hand-edited CSV is prohibited because each snapshot must
retain one exchange's provenance. If either exchange file or its approved source URL is
missing, stop: ingestion is blocked rather than continuing with a partial market.

The import commands make no HTTP request and do not require
`HENGCE_TUSHARE_TOKEN`. Run `hengce init-state --data-dir data` first. The persisted
`SourcePolicy` for each `source_id` must be approved, enabled, allow the
`security_master` purpose, and allow the exact scheme and hostname of the corresponding
official source URL.

Set the following variables locally to the two normalized files and their real,
approved metadata. The URL variables must contain the exact approved official source
URLs; the placeholders below intentionally do not supply or invent URL values.

```powershell
$sseCsv = '<path-to-normalized-sse-csv>'
$sseSourceUrl = '<exact-approved-official-sse-source-url>'
$sseVersion = '<sse-version>'
$sseCollectedAt = '<sse-collected-at-ISO-8601>'

$szseCsv = '<path-to-normalized-szse-csv>'
$szseSourceUrl = '<exact-approved-official-szse-source-url>'
$szseVersion = '<szse-version>'
$szseCollectedAt = '<szse-collected-at-ISO-8601>'

hengce import-security-master --file $sseCsv --source-id sse --source-url $sseSourceUrl --version $sseVersion --collected-at $sseCollectedAt --data-dir data
hengce import-security-master --file $szseCsv --source-id szse --source-url $szseSourceUrl --version $szseVersion --collected-at $szseCollectedAt --data-dir data
hengce check-security-universe --data-dir data
```

Do not begin market ingestion unless both imports succeed and
`check-security-universe` reports a valid combined universe. A missing, empty, rejected,
or source-mismatched SSE or SZSE snapshot blocks ingestion.

The input must include `ts_code`, `symbol`, `name`, `exchange`, `board`, `currency`,
`list_date`, and `security_type`. Only CNY A-shares on MAIN_SH, STAR, MAIN_SZ, and
CHINEXT are retained. Duplicate retained `ts_code` values fail the import before any
snapshot is written.

On each successful import, stdout is deterministic JSON containing `content_hash`,
`security_count`, `source_id`, and `version`. SQLite persists the complete filtered
snapshot plus source URL, collection time, SHA-256 content hash, version, and
filter-quality lineage. Repeating the same command is idempotent and preserves the
existing auditable snapshot. Policy denial is audited before any snapshot is written
and does not reserve a network rate-limit slot.
