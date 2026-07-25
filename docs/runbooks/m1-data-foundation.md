# M1 数据底座运行手册

## 初始化

1. 创建 Python 3.12 虚拟环境，并运行 `python -m pip install -e ".[dev]"`。
2. 复制 `.env.example` 为 `.env`，仅在本地设置 `HENGCE_TUSHARE_TOKEN`。
3. 运行 `hengce init-state --data-dir data`。默认来源策略已内置于 `hengce` wheel，因而在任何工作目录都可用。如需显式使用操作员策略文件，传入 `--policy-file config/source_policies.json`。
4. **在任何行情摄取前必须导入证券主数据。** 按
   [证券主数据导入手册](security-master-import.md) 准备已批准的上交所或深交所 CSV，
   再运行 `hengce import-security-master ... --data-dir data`。没有有效证券主数据快照时，
   单日摄取和历史初始化都会以 `SECURITY_MASTER_UNAVAILABLE` 阻断。

导入时的 `--source-id` 与 `--source-url` 必须匹配 SQLite 中已批准且启用的
`SourcePolicy`：用途必须包含 `security_master`，URL 必须使用策略允许的 scheme，
hostname 必须与 `allowed_domains` 中的一个条目精确相同。M1 的官方证券主数据来源为
`sse` 或 `szse`，均要求 HTTPS。未知、禁用、待复核、用途不符或 URL 不符的来源会在
写入快照前被拒绝并记录审计；本地校验不会消耗网络请求速率配额。

## 单日摄取

运行 `hengce ingest-market --trade-date 2026-07-24 --data-dir data`。

成功后，原始响应以 SHA-256 全局内容寻址方式保存于
`data/raw/objects/<sha256>/payload.bin`；每次采集的不可变来源、时间和内容类型记录位于
`data/raw/provenance/<event-sha256>.json`。行情 Parquet 位于
`data/normalized/market_bars/trade_date=2026-07-24/`，SQLite 会保存
`market_daily:2026-07-24` 检查点。重复相同日期不会再次请求或重复写入。

## 历史初始化

准备已由官方来源确认的 ISO 日期 JSON 数组，再运行：

`hengce initialize-history --calendar-file approved-trade-dates.json --data-dir data`

中断后重复同一命令。初始化器会让行情摄取服务重新校验请求范围内每个已完成日期的
Raw 与 Parquet 工件；有效日期只读检查点，不会再次请求。缺失或损坏的旧工件会按
行情摄取恢复语义重建或阻断，然后才继续未完成日期。日历必须是严格的 JSON 字符串
数组，日期采用 `YYYY-MM-DD`，会去重并排序。

## 安全与故障处理

不要提交 `.env`、`data/`、SQLite、DuckDB、Parquet、原始响应或附件。遇到
401、403、429、验证码或策略拒绝时停止；不要替换来源或规避限制。Tushare 是唯一
允许 HTTP 的自动来源，且仅限 `api.tushare.pro`；其他白名单来源均要求 HTTPS。

命令内创建的 HTTP 客户端在命令结束时关闭；通过 `build_market_ingestion` 嵌入时，
调用方负责关闭其传入的客户端。
