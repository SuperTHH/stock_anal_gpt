# M1 数据底座运行手册

## 初始化

1. 创建 Python 3.12 虚拟环境，并运行 `python -m pip install -e ".[dev]"`。
2. 复制 `.env.example` 为 `.env`，仅在本地设置 `HENGCE_TUSHARE_TOKEN`。
3. 运行 `hengce init-state --data-dir data`。

## 单日摄取

运行 `hengce ingest-market --trade-date 2026-07-24 --data-dir data`。

成功后，原始响应位于 `data/raw/tushare/`，行情 Parquet 位于
`data/normalized/market_bars/trade_date=2026-07-24/`，SQLite 会保存
`market_daily:2026-07-24` 检查点。重复相同日期不会再次请求或重复写入。

## 历史初始化

准备已由官方来源确认的 ISO 日期 JSON 数组，再运行：

`hengce initialize-history --calendar-file approved-trade-dates.json --data-dir data`

中断后重复同一命令，任务从最后一个成功交易日继续。日历必须是严格的 JSON
字符串数组，日期采用 `YYYY-MM-DD`，会去重并排序。

## 安全与故障处理

不要提交 `.env`、`data/`、SQLite、DuckDB、Parquet、原始响应或附件。遇到
401、403、429、验证码或策略拒绝时停止；不要替换来源或规避限制。Tushare 是唯一
允许 HTTP 的自动来源，且仅限 `api.tushare.pro`；其他白名单来源均要求 HTTPS。

命令内创建的 HTTP 客户端在命令结束时关闭；通过 `build_market_ingestion` 嵌入时，
调用方负责关闭其传入的客户端。
