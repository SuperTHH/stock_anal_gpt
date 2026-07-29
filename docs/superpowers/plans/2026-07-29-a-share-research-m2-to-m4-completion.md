# M2–M4 数据、策略与本地应用实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:executing-plans` and `superpowers:test-driven-development`.

**Goal:** 在现有 M1 与 XBRL 财务事实内核上，补齐公司行动、总回报、财务派生指标、
巨潮 PDF 兜底、三个独立策略、原子报告快照和本地只读 UI 数据接线。

**Architecture:** 原始行情与财务事实保持不可变；所有历史读取必须显式传入
`as_of` 与 `known_at`。M2 只生成带来源谱系的派生输入，M3 只消费通过质量门禁的
输入并生成不可变候选与报告包，M4 只读取已发布报告包，绝不直接读取采集原始数据。

**Tech Stack:** Python 3.12、Pydantic 2、SQLite、DuckDB/Parquet、FastAPI、
React + TypeScript、Pytest。

---

## Task 1：冻结 M2 公司行动与派生财务契约

**Files**

- Modify: `src/hengce/contracts/enums.py`
- Modify: `src/hengce/contracts/market.py`
- Add: `src/hengce/contracts/derived.py`
- Test: `tests/unit/contracts/test_m2_models.py`

**TDD steps**

1. 为 `CorporateAction` 的日期、数值、版本和来源约束编写失败测试。
2. 为 `DerivedFinancialMetric` 的时点、输入事实、算法版本和质量状态编写失败测试。
3. 实现最小契约并运行单测。

## Task 2：公司行动、复权因子与总回报

**Files**

- Add: `src/hengce/actions/calculator.py`
- Add: `src/hengce/warehouse/derived_market.py`
- Test: `tests/unit/actions/test_calculator.py`
- Test: `tests/integration/test_m2_actions_acceptance.py`

**TDD steps**

1. 用现金分红、送转/拆股和配股固定样例验证除权日前后总回报连续。
2. 验证原始 OHLC 不变、重复公司行动不重复应用。
3. 验证缺失、冲突、未来才公开的行动阻断派生收益。
4. 将派生序列写入内容寻址的不可变 Parquet。

## Task 3：当时可知财务派生指标

**Files**

- Add: `src/hengce/financials/metrics.py`
- Test: `tests/unit/financials/test_metrics.py`

**TDD steps**

1. 为 ROE、ROIC、收入增长、现金流质量、负债率、FCF 和估值输入编写固定样例。
2. 验证除零、负利润、不完整字段和冲突事实不会产生误导性指标。
3. 验证更正只影响其公开且进入系统之后的指标。

## Task 4：交易所 XBRL 优先、巨潮 PDF 兜底

**Files**

- Add: `src/hengce/financials/fallback.py`
- Add: `src/hengce/services/financial_resolution.py`
- Test: `tests/unit/financials/test_fallback.py`
- Test: `tests/integration/test_m2_financial_resolution.py`

**TDD steps**

1. XBRL 可用时禁止读取 PDF。
2. XBRL 缺失时允许读取已通过策略守卫的巨潮公开定期报告。
3. PDF 提取先标记 `UNVERIFIED`，通过总计勾稽、期间、单位和完整度校验后才可用。
4. XBRL/PDF 冲突时优先 XBRL，同时返回质量阻断原因。
5. 保留公告时间、版本和 `supersedes_id` 更正链。

## Task 5：冻结 M3 策略与报告契约

**Files**

- Add: `src/hengce/contracts/strategy.py`
- Test: `tests/unit/contracts/test_strategy_models.py`

**TDD steps**

1. 定义 `StrategyCandidate`、`FactorDetail`、`ReportSnapshot` 和状态枚举。
2. 禁止跨策略总分，强制策略版本、数据截止时间和因子谱系。
3. 报告发布后不可变，重算生成新报告 ID。

## Task 6：通用硬过滤

**Files**

- Add: `src/hengce/strategies/filters.py`
- Test: `tests/unit/strategies/test_filters.py`

**TDD steps**

1. 覆盖 PRD HF-01 至 HF-12。
2. 阈值全部进入带版本配置。
3. 返回机器可读阻断码，不静默忽略缺失数据。

## Task 7：三个独立策略池

**Files**

- Add: `src/hengce/strategies/engine.py`
- Add: `src/hengce/strategies/quality_growth.py`
- Add: `src/hengce/strategies/deep_value.py`
- Add: `src/hengce/strategies/stable_dividend.py`
- Test: `tests/unit/strategies/`
- Test: `tests/integration/test_m3_strategy_acceptance.py`

**TDD steps**

1. 行业内百分位，样本少于 20 时回退全市场并留痕。
2. 1%/99% 缩尾，保留原始值。
3. 三个策略分别使用冻结权重、必要条件、风险规则和独立排名。
4. 负 PE 不按低估值加分；未公告分红不进入股息率。
5. 关键因子缺失转 `DATA_INSUFFICIENT`，不重分配权重。
6. 每池最多 30 只，不产生跨策略总分或自动买入指令。

## Task 8：报告快照与原子发布

**Files**

- Add: `src/hengce/reports/publisher.py`
- Add: `src/hengce/state/report_repository.py`
- Add: `src/hengce/state/migrations/007_reports.sql`
- Test: `tests/unit/reports/test_publisher.py`
- Test: `tests/integration/test_m3_report_acceptance.py`

**TDD steps**

1. 报告包内容寻址且发布后不可修改。
2. 所有数据域通过门禁才切换 `latest`。
3. 失败保留上一有效报告，并暴露 `STALE_PREVIOUS_REPORT` 派生状态。

## Task 9：本地只读 API 与 UI 接线

**Files**

- Add: `apps/api/`
- Add: `apps/web/`
- Test: `tests/integration/test_m4_api.py`
- Test: `apps/web/src/**/*.test.tsx`

**TDD steps**

1. API 仅绑定 `127.0.0.1`，只返回已发布报告与来源谱系。
2. 五个页面读取同一报告版本，不混用 `latest` 与历史数据。
3. 将现有虚构示例模式保留为显式演示模式；生产模式无数据时不得显示示例候选。
4. 展示报告状态、截止时间、质量阻断、来源链接和三个独立策略页签。

## Task 10：跨层验收

1. 运行 M2、M3、M4 单元与集成测试。
2. 运行全量 `pytest`、`ruff check` 和 `git diff --check`。
3. 验证示例数据永不进入生产报告，失败报告永不替换 `latest`。
4. 进行代码评审，归零 Critical 与 Important 问题。
