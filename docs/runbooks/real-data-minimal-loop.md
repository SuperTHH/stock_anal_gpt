# 真实数据最小闭环操作手册

## 1. 范围与安全边界

本手册只适用于个人、非商业的本地研究。固定市场日期为 `2026-07-22`，报告截止时间为
`2026-07-22T21:30:00+08:00`，试点样本必须恰好为 30 只且板块配额为
`8/8/7/7`。三个策略池独立验收；任何池覆盖率低于 80% 时，该池必须显示
`BLOCKED` 且不得发布候选排名。

禁止把以下内容提交到 Git：

- `.env` 或任何 Token；
- `data/manual_inbox/` 中的真实附件和侧车；
- `data/state/` 中的 SQLite；
- `data/raw/`、`data/warehouse/`、`data/normalized/` 中的 Raw 或 Parquet；
- `data/reports/`、`data/backups/`、`data/run_summaries/` 中的报告、备份和运行摘要。

所有命令都从仓库根目录运行。示例中的尖括号内容是占位符，不是可直接使用的真实值。

## 2. 安全检查 `.env` 与 Tushare Token

下面的检查只输出“是否存在”，不打印 Token 值：

```powershell
if (-not (Test-Path -LiteralPath '.env')) {
  throw '缺少 .env；请先在本机创建，不要提交到 Git'
}
$tokenConfigured = [bool](
  Get-Content -LiteralPath '.env' |
    Where-Object { $_ -match '^HENGCE_TUSHARE_TOKEN=.+$' }
)
if (-not $tokenConfigured) {
  throw 'HENGCE_TUSHARE_TOKEN 未配置'
}
'Tushare Token configuration present'
```

不要运行会回显 `.env` 全文、环境变量值或命令历史中 Token 的命令。

## 3. 验证行情与双交易所主数据

确认本地状态库已迁移：

```powershell
.venv\Scripts\hengce.exe init-state --data-dir data
```

主数据必须同时包含上交所和深交所快照：

```powershell
.venv\Scripts\hengce.exe check-security-universe --data-dir data
```

输出只应包含聚合数量、两份来源版本和 `universe_hash`。然后确认
`2026-07-22` 行情分区存在；此命令只列文件元数据，不读取或打印行级行情：

```powershell
$marketPartition = Get-ChildItem -LiteralPath `
  'data/normalized/market_bars/trade_date=2026-07-22' `
  -Filter '*.parquet' -File -ErrorAction Stop
if ($marketPartition.Count -eq 0) {
  throw '缺少 2026-07-22 行情分区'
}
"2026-07-22 market partitions: $($marketPartition.Count)"
```

## 4. 首次运行：`manual-only`

第一次必须使用 `manual-only`。该模式不调用公开文件网络采集阶段，只验证输入、冻结
固定样本并生成 360 项不可变采集清单：

```powershell
.venv\Scripts\hengce.exe rebuild-pilot-report `
  --market-date 2026-07-22 `
  --report-cutoff-at '2026-07-22T21:30:00+08:00' `
  --acquisition-mode manual-only `
  --data-dir data
```

输出只有聚合 JSON：阶段状态、清单状态分布、XBRL/PDF 数量、策略池覆盖率、报告 ID、
报告哈希和人工待办数量。不会输出 Token、附件内容、候选名称或候选代码。

退出码含义：

- `0`：闭环完成并发布报告；
- `1`：某一阶段失败，查看聚合 `failed_stage` 与 `error_code`；
- `2`：存在人工待办且尚未发布报告，这是首次 `manual-only` 的预期失败关闭状态。

样本生成后再次运行相同命令必须复用同一 `universe_id`、清单 ID 和已成功检查点。

## 5. 何时允许 `approved-public`

只有用户针对本次运行明确允许访问白名单公开入口时，才能把参数改为
`approved-public`：

```powershell
.venv\Scripts\hengce.exe rebuild-pilot-report `
  --market-date 2026-07-22 `
  --report-cutoff-at '2026-07-22T21:30:00+08:00' `
  --acquisition-mode approved-public `
  --data-dir data
```

该模式仍只能使用已批准来源和公开入口；不得调用隐藏接口、绕过验证码、扩大域名或自动
切换到条款不明的数据源。没有明确许可时继续使用 `manual-only`。

## 6. 查看人工待办与准备收件箱

重新运行 `manual-only` 会复用检查点并输出最新的
`manifest_status_distribution` 与 `manual_todo_count`。这就是允许对外展示的
`AWAITING_MANUAL` 聚合清单；不要把股票代码、公告正文或本地路径复制到工单或 Git。

每个已审核文件放入独立目录：

```text
data/manual_inbox/<MANIFEST_ITEM_ID>/
  attachment.<APPROVED_EXTENSION>
  sidecar.json
```

`sidecar.json` 使用以下占位结构：

```json
{
  "manifest_item_id": "<MANIFEST_ITEM_ID>",
  "source_id": "<sse_OR_szse_OR_cninfo>",
  "source_url": "https://<APPROVED_OFFICIAL_DOMAIN>/<OFFICIAL_PATH>",
  "published_at": "<OFFSET_AWARE_ISO_TIME>",
  "collected_at": "<OFFSET_AWARE_ISO_TIME>",
  "version": "<SOURCE_VERSION>",
  "content_type": "<APPROVED_MIME>",
  "attachment_name": "attachment.<APPROVED_EXTENSION>",
  "content_sha256": "<64_LOWERCASE_HEX>"
}
```

侧车中的哈希必须与附件完全一致，时间必须含 UTC 偏移，来源必须通过 `SourcePolicy`。
不要编辑数据库状态来跳过校验。准备完成后用完全相同的市场日期、截止时间和采集模式
重新运行；系统从稳定检查点继续。

## 7. 启动本地 API 与 UI

API 只允许绑定回环地址。先在终端 A 启动只读 API：

```powershell
$env:HENGCE_DATA_DIR = (Resolve-Path -LiteralPath 'data').Path
.venv\Scripts\python.exe -m uvicorn hengce.api.local:app `
  --host 127.0.0.1 `
  --port 8000
```

再在终端 B 启动前端：

```powershell
Set-Location apps/web
npm.cmd run dev -- --host 127.0.0.1 --port 4173
```

打开 `http://127.0.0.1:4173/`。真实模式不得出现演示水印或虚构候选；无已发布报告时
必须显示明确错误。演示能力只能通过 `http://127.0.0.1:4173/?demo=1` 单独访问。

## 8. 聚合验收、备份恢复与故障续跑

只读验收器：

```powershell
.venv\Scripts\hengce.exe validate-pilot-report `
  --market-date 2026-07-22 `
  --report-cutoff-at '2026-07-22T21:30:00+08:00' `
  --data-dir data
```

通过时退出码为 `0`；任何配额、清单、哈希、latest 指针、API 报告 ID、候选谱系或
忽略规则错误都会返回聚合错误码并以 `1` 退出。输出不含候选明细。

每次重建会先把 SQLite 一致性备份写入 `data/backups/`。恢复前先停止 API 和重建任务，
保留当前损坏库的副本，再把一个已验证备份复制到显式目标：

```powershell
Copy-Item -LiteralPath '<VERIFIED_BACKUP_SQLITE_PATH>' `
  -Destination 'data/state/hengce.sqlite3.recovery' `
  -ErrorAction Stop
```

先对 `.recovery` 执行 SQLite 完整性核验，经人工确认后再安排替换；不要直接覆盖当前库。
普通中断无需恢复备份：使用完全相同的三个运行参数重试，系统会复用成功检查点和不可变
工件。

## 9. 提交前私有数据隔离检查

```powershell
git status --short
git status --ignored --short -- data .env
$sensitivePaths = @(
  'data/manual_inbox',
  'data/reports',
  'data/backups',
  'data/run_summaries',
  'data/state/hengce.sqlite3',
  'data/raw',
  'data/warehouse',
  'data/normalized',
  '.env'
)
$notIgnored = @(
  $sensitivePaths | Where-Object {
    git check-ignore --quiet -- $_
    $LASTEXITCODE -ne 0
  }
)
if ($notIgnored.Count -ne 0) {
  throw '至少一个敏感运行路径未被 Git 忽略'
}
```

第一条命令不得出现真实附件、库、Parquet、Raw、报告或 `.env`。第二、三条命令用于证明
这些路径确实被忽略；如果任何路径被跟踪或未忽略，停止提交并先修复隔离规则。
