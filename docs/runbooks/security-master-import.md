# Local security-master import

Import only a CSV that has already been obtained and approved from an official source.
The command makes no HTTP request and does not require `HENGCE_TUSHARE_TOKEN`.
Run `hengce init-state --data-dir data` first. The persisted `SourcePolicy` for
`source_id` must be approved, enabled, allow the `security_master` purpose, and allow
the URL's exact scheme and hostname. The bundled SSE and SZSE policies allow official
HTTPS URLs on `sse.com.cn`/`www.sse.com.cn` and `szse.cn`/`www.szse.cn`.

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
Policy denial is audited before any snapshot is written and does not reserve a network
rate-limit slot.
