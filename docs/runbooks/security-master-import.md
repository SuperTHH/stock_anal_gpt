# Local security-master import

Import only a CSV that has already been obtained and approved from an official source.
The command makes no HTTP request and does not require `HENGCE_TUSHARE_TOKEN`.

```powershell
hengce import-security-master `
  --file .\approved-security-master.csv `
  --source-id sse `
  --source-url https://www.sse.com.cn/assortment/stock/list/share/ `
  --version 2026-07-24 `
  --collected-at 2026-07-24T09:00:00+00:00 `
  --data-dir data
```

The input must include `ts_code`, `symbol`, `name`, `exchange`, `board`, `currency`,
`list_date`, and `security_type`. Only CNY A-shares on MAIN_SH, STAR, MAIN_SZ, and
CHINEXT are retained. Duplicate retained `ts_code` values fail the import before any
snapshot is written.

On success, stdout is deterministic JSON containing `content_hash`, `security_count`,
`source_id`, and `version`. SQLite persists the complete filtered snapshot plus source
URL, collection time, SHA-256 content hash, version, and filter-quality lineage. Repeating
the same command is idempotent and preserves the existing auditable snapshot.
