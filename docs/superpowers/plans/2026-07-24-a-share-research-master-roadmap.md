# A 股中长期研究平台实施路线图

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 M1 至 M5 建成仅供个人非商业研究使用的本地 A 股中长期研究平台。

**Architecture:** Python 负责采集、政策守卫、数据处理、质量校验、策略与任务；SQLite 保存轻量事务状态，DuckDB 负责分析查询，Parquet 保存不可变数据。FastAPI 只绑定回环地址，React + TypeScript 实现五个只读研究页面，Windows Task Scheduler 每个交易日 21:30 调用任务 CLI。

**Tech Stack:** Python 3.12、Pydantic 2、SQLite、DuckDB、PyArrow/Parquet、FastAPI、React、TypeScript、Pytest、Playwright、Windows Task Scheduler。

## Global Constraints

- 最高需求基线：`docs/product/2026-07-24-a-share-long-term-research-platform-prd.md`。
- 只覆盖沪深主板、创业板、科创板 A 股；排除北交所、B 股和港股。
- 只使用已批准白名单；不得调用隐藏接口、绕过验证码、登录、付费或频率限制。
- 协议按 SourcePolicy 逐来源控制；Tushare 只对官方文档指定的 `http://api.tushare.pro` 允许 HTTP，其他 MVP 自动来源只允许 HTTPS。
- 原始响应、原始文件、未复权 OHLC 和历史财务版本不可覆盖。
- 历史查询必须显式提供 `as_of`，不得默认读取最新版本。
- 关键数据缺失、复权未验证或当时可知校验失败时禁止生成候选榜。
- 三个策略保持独立分数、独立排名和独立版本；不得生成跨策略总分。
- 系统不得生成自动买入、目标价、仓位或确定性收益。
- SQLite 保存事务型状态；DuckDB 和 Parquet 分别承担分析查询与不可变存储。
- Token、Cookie、本地数据库、数据文件和下载附件不得提交到 Git。
- 每个实现任务采用 TDD，并在提交前运行相关单元、集成和静态检查。

---

## 1. 分解原则

完整 PRD 涵盖数据治理、版本化仓库、财务与复权、策略、任务编排、API 和五页面。一次性实现会导致后续任务依赖尚未验证的接口，因此按已批准的 M1–M5 里程碑分别形成实施计划。

每个里程碑必须产生可运行、可测试、可独立评审的增量。只有前一里程碑的数据契约和测试通过后，才允许冻结下一里程碑的详细代码计划。

## 2. 里程碑与计划文件

| 里程碑 | 可交付增量 | 详细计划 |
| --- | --- | --- |
| M1 数据底座 | Policy Guard、Raw Store、SQLite 状态库、DuckDB/Parquet 仓库、证券主数据、按日全市场行情、可恢复五年初始化 | `docs/superpowers/plans/2026-07-24-a-share-research-m1-data-foundation.md` |
| M2 财务与公司行动 | XBRL/PDF 财务事实、更正版本、公司行动、复权、总回报、当时可知查询 | M1 验收后编写 `2026-07-24-a-share-research-m2-financials-actions.md` |
| M3 策略与报告 | 通用硬过滤、三个独立策略、因子版本、报告快照、原子发布、失败回退 | M2 验收后编写 `2026-07-24-a-share-research-m3-strategies-reports.md` |
| M4 本地研究应用 | FastAPI、本地只读接口、五页面、来源抽屉、研究备忘录、三档响应式 | M3 验收后编写 `2026-07-24-a-share-research-m4-local-application.md` |
| M5 稳定性验收 | 21:30 调度、连续交易日运行、故障注入、安全、审计、备份恢复、用户验收 | M4 验收后编写 `2026-07-24-a-share-research-m5-production-readiness.md` |

> **M2 状态：**
> [M2a 本地 XBRL 财务事实内核](2026-07-26-xbrl-financial-facts-kernel.md)
> 已实现。M2 其余范围仍未完成，包括 PDF 回退、公司行动、复权和总回报；
> 因此整个 M2 里程碑仍为进行中。

后续计划的文件名和交付边界已经固定；详细接口应引用前一里程碑实际通过测试的类型，不预先复制可能变化的实现细节。

## 3. 跨里程碑接口冻结点

### M1 → M2

必须冻结：

- `SourcePolicy`、`RunRecord`、`RefusalRecord`；
- `SecurityMaster`、`TradingStatus`、`MarketBar`；
- 原始对象地址和内容哈希格式；
- Parquet 分区规则；
- SQLite migration 机制；
- `as_of` 查询的基础时间字段。

### M2 → M3

必须冻结：

- `CorporateAction`、`FinancialFact`；
- `published_at`、`effective_at`、`valid_from` 和 `supersedes_id` 语义；
- 复权因子和总回报接口；
- 财务更正与冲突状态；
- 当时可知查询 API。

### M3 → M4

必须冻结：

- `StrategyCandidate`、`ReportSnapshot`；
- 三个策略版本和因子明细结构；
- 报告状态和派生显示状态；
- 本地只读 API 响应结构；
- 来源血缘查询接口。

### M4 → M5

必须冻结：

- 五页面路由和核心交互；
- 备忘录唯一写接口；
- 1440、1280、1100 响应式基线；
- 任务触发 CLI；
- 运行、告警和恢复界面。

## 4. 里程碑门禁

每个里程碑进入下一阶段前必须：

- [ ] 所有计划任务有独立提交。
- [ ] `git diff --check` 通过。
- [ ] 相关单元测试和集成测试全部通过。
- [ ] 固定测试数据不依赖网络。
- [ ] 真实网络烟雾测试默认关闭，只有用户显式配置凭证后运行。
- [ ] 无 Token、Cookie、数据库、Parquet、下载附件或原始响应进入 Git。
- [ ] 数据契约和错误码有文档。
- [ ] Critical 和 Important 代码审查问题归零。
- [ ] 里程碑验收编号有可复现证据。

## 5. 需求覆盖映射

| PRD 范围 | 负责里程碑 |
| --- | --- |
| 白名单、来源政策、拒绝记录 | M1 |
| 原始不可变存储、轻量状态、分析仓库 | M1 |
| 五年初始化、证券主数据、全市场日线 | M1 |
| 财务事实、更正、公司行动、复权 | M2 |
| 当时可知和历史回放 | M2 |
| 硬过滤、三策略、候选输出 | M3 |
| 21:30 任务状态、原子报告发布 | M3；M5 完成系统调度 |
| 本地 API、五页面、来源抽屉 | M4 |
| 响应式、可访问性、备忘录 | M4 |
| 性能、安全、连续运行、备份恢复 | M5 |

## 6. 执行起点

从 `docs/superpowers/plans/2026-07-24-a-share-research-m1-data-foundation.md` 的 Task 1 开始。不得跳过 M1 直接实现财务、策略或页面，也不得提前加入商业新闻、自动交易、多用户或云端部署。
