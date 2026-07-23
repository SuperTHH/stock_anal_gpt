# 2026-07-22 每日总览数据刷新 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把当前衡策 Superdesign 每日总览更新为截至 2026-07-22 收盘的真实官方数据快照，并在缺少 Tushare Token 时阻断三个策略候选榜。

**Architecture:** 把来源事实固定到一份只读数据快照，把展示与降级规则补充进现有设计系统，再从当前 draft `33ef2e2d-2ca0-470b-854c-2cca19eb27ea` 创建一个内容更新分支。最终通过浏览器读取实际 DOM，在 1440、1280、1100 三个视口验证日期、数字、阻断态、来源和布局。

**Tech Stack:** Markdown、Superdesign CLI 0.9+、Superdesign Canvas、Codex in-app Browser。

## Global Constraints

- 行情截止日固定为 `2026-07-22`；采集日期固定为 `2026-07-23`。
- 仅使用上交所、深交所、证监会和国家统计局公开页面；当前环境无 `TUSHARE_TOKEN`。
- 沪深主板、创业板、科创板 A 股；排除 B 股、北交所和港股。
- 三个策略池必须独立，并全部显示阻断态；不得显示示例候选、示例排名、买入指令或风险总分。
- 1440、1280、1100 视口无横向溢出；1100px 下侧栏和主区偏移均为 64px。
- 正文至少 13px/19px，关键操作高度 32px；1440×900 首屏完整显示第一条官方事件。
- 保留暖纸色、森林绿、方角、1px 分隔线、系统中文字体和“仅内部规则校验”。

---

### Task 1: 固化官方数据快照与展示规则

**Files:**
- Create: `.superdesign/data-snapshots/2026-07-22.md`
- Modify: `.superdesign/design-system.md`
- Reference: `docs/superpowers/specs/2026-07-23-daily-overview-data-refresh-design.md`

**Interfaces:**
- Consumes: 已确认规格中的来源、原始数值、合并公式和降级规则。
- Produces: Superdesign 生成命令可直接读取的数据上下文；所有字段包含值、单位、来源 URL 和质量说明。

- [ ] **Step 1: 创建快照文件**

用 `apply_patch` 创建 `.superdesign/data-snapshots/2026-07-22.md`，逐项写入：

```text
trade_date: 2026-07-22
collected_date: 2026-07-23
sse_a_turnover_100m: 12591.07
szse_a_turnover_100m: 13963.41
total_a_turnover_100m: 26554.48
a_share_listed_count: 5201
a_share_market_cap_100m: 1074126.20
sse_close: 3867.03
sse_close_quality: derived_from_next_day_previous_close
strategy_status: blocked_missing_tushare_and_financial_snapshot
```

同时写入三个官方事件的标题、发布日期、事实摘要、系统研判、影响周期和原文 URL。

- [ ] **Step 2: 补充设计系统规则**

在 `.superdesign/design-system.md` 末尾追加 `2026-07-22 data-refresh state`，明确：

```text
Header badge = 官方数据快照
Report state = 市场概况可用 · 策略候选榜未生成
Coverage = 交易所概况 2/2 · 策略池 0/3
Each strategy module = 本交易日候选榜未生成 + 查看缺失数据
Risk module = 风险评分未计算
Forbidden legacy copy = 2024-05-24, 原型示例数据, 99.4%, candidate names, 0-100 risk bars
```

- [ ] **Step 3: 验证快照和规则完整**

Run:

```powershell
rg -n "2026-07-22|26554.48|5201|1074126.20|3867.03|blocked_missing_tushare" .superdesign/data-snapshots/2026-07-22.md
rg -n "官方数据快照|策略候选榜未生成|策略池 0/3|风险评分未计算" .superdesign/design-system.md
```

Expected: 第一条命令返回全部 6 个关键字段；第二条命令返回全部 4 个展示规则。

- [ ] **Step 4: 提交本地上下文**

```powershell
git add .superdesign/data-snapshots/2026-07-22.md .superdesign/design-system.md
git commit -m "docs: add 2026-07-22 official market snapshot"
```

Expected: 新提交仅包含快照和设计系统更新。

### Task 2: 生成真实数据降级版设计稿

**Files:**
- Read: `.superdesign/data-snapshots/2026-07-22.md`
- Read: `.superdesign/design-system.md`
- Read: `docs/superpowers/specs/2026-07-23-daily-overview-data-refresh-design.md`
- External output: Superdesign draft branch from `33ef2e2d-2ca0-470b-854c-2cca19eb27ea`

**Interfaces:**
- Consumes: Task 1 的数据快照和设计规则。
- Produces: 一个新的 Superdesign draft ID、canvas URL 和 preview URL；执行者将 CLI 返回的 `drafts[0].draftId` 原样保存为后续步骤使用的 `$newDraftId`。

- [ ] **Step 1: 验证 CLI**

```powershell
$env:npm_config_cache=(Resolve-Path .npm-cache).Path
npx.cmd --yes @superdesign/cli@latest --version
```

Expected: 输出版本号且退出码为 0。

- [ ] **Step 2: 读取当前设计**

```powershell
$env:npm_config_cache=(Resolve-Path .npm-cache).Path
npx.cmd --yes @superdesign/cli@latest get-design --draft-id 33ef2e2d-2ca0-470b-854c-2cca19eb27ea --json
```

Expected: 返回当前 HTML、版本历史和 draft title。

- [ ] **Step 3: 创建单一内容更新分支**

运行 `iterate-design-draft --mode branch`，只传一个 `-p`。提示词必须要求：

```text
Update the current 衡策 daily overview with the verified 2026-07-22 official data snapshot. Preserve the existing approved layout, responsive rules, typography, palette, geometry, traceability, and source actions. Replace all 2024 dates and prototype values. Header: 2026-07-22 and 官方数据快照. Report state: 市场概况可用 · 策略候选榜未生成; collected 2026-07-23; 交易所概况 2/2; 策略池 0/3; warning 未配置 Tushare Token，缺少个股日线与财务事实快照. Five market cells: 上证指数收盘 3,867.03 with 由次日昨收推导; 沪市A股成交额 1.2591万亿元; 深市A股成交额 1.3963万亿元; 沪深A股成交额 2.6554万亿元; A股挂牌数 5,201只 / 总市值107.41万亿元. Remove all sample stocks, rankings, risk tags, candidate detail actions, unverified market breadth, sample coverage, and 0-100 risk bars. Keep three equal-height independent strategy modules; each shows 本交易日候选榜未生成, the four missing data categories, and a 32px 查看缺失数据 action. Risk area shows 风险评分未计算. Data quality distinguishes complete exchange overview and official events from missing Tushare/financial snapshots and states that strategy output is blocked. Official events in descending date order: 2026-07-22 SSE ETF option settlement reminder (low long-horizon relevance), 2026-07-21 CSRC market-development meetings (6-12 months, confidence medium), 2026-07-16 NBS GDP initial calculation (Q2 4.3%, H1 4.7%, manufacturing 5.5%, financial industry 6.7%; 3-6 months). Clearly separate 官方事实 from 系统研判 and preserve original-source links. Footer: market cutoff 2026-07-22 15:00, event cutoff 2026-07-22 23:59, collected 2026-07-23, 仅内部规则校验, 不构成投资建议. At 1440x900 one full event must be visible. No horizontal overflow at 1440, 1280, or 1100; at 1100 rail and main offset are 64px. Body text minimum 13px/19px and key actions exactly 32px. Use ONLY the fonts, colors, spacing, and component styles defined in the design system. Do not introduce any fonts, colors, or visual styles not in the design system.
```

Context files:

```text
--context-file .superdesign/design-system.md
--context-file .superdesign/data-snapshots/2026-07-22.md
--context-file docs/superpowers/specs/2026-07-23-daily-overview-data-refresh-design.md
--user-request "爬取最新的数据并更新到当前的UI设计稿"
```

Expected: 返回一个新 draft ID、canvas URL 和 preview URL。

### Task 3: 浏览器内容与像素验收

**Files:**
- Create: `.superdesign/review/daily-overview-2026-07-22-1440x900.png`
- Create: `.superdesign/review/daily-overview-2026-07-22-1100x900.png`
- External input: Task 2 preview URL。

**Interfaces:**
- Consumes: Task 2 的 preview URL。
- Produces: 量化验收记录和两张最终截图。

- [ ] **Step 1: 在 1440×900 读取实际 DOM**

验证以下结果：

```text
document width = 1440
header contains 2026-07-22 and 官方数据快照
market values contain 3867.03, 1.2591, 1.3963, 2.6554, 5,201, 107.41
strategy blocked-state count = 3
candidate detail action count = 0
official original-link count = 3
first event bottom <= 900
body text >= 13px/19px
key action heights = 32px
```

保存 `.superdesign/review/daily-overview-2026-07-22-1440x900.png`。

- [ ] **Step 2: 在 1280×900 验证宽度**

Expected:

```text
document width = 1280
aside width = 208
main left = 208
all header actions right <= 1280
```

- [ ] **Step 3: 在 1100×900 验证响应式**

Expected:

```text
document width = 1100
aside width = 64
main left = 64
main right = 1100
visible navigation labels = 0
all header actions right <= 1100
```

保存 `.superdesign/review/daily-overview-2026-07-22-1100x900.png`。

- [ ] **Step 4: 检查禁止内容**

在实际 DOM 中断言以下字符串不存在：

```text
2024-05-24
原型示例数据
99.4%
贵州茅台
建设银行
中国神华
Internal Rules Only
```

- [ ] **Step 5: 只在发现具体缺陷时原位修补一次**

若 Task 3 的任一断言失败，先读取新 draft 的当前版本，然后执行一次 `iterate-design-draft --mode replace`，提示词只列失败断言并要求保留其他通过项。修补后重新执行 Steps 1—4；不得进行第二次自动修补。

### Task 4: 完成前验证与交付

**Files:**
- Verify: `.superdesign/data-snapshots/2026-07-22.md`
- Verify: `.superdesign/review/daily-overview-2026-07-22-1440x900.png`
- Verify: `.superdesign/review/daily-overview-2026-07-22-1100x900.png`

**Interfaces:**
- Consumes: Task 1—3 的所有产物。
- Produces: 设计画布链接、预览链接、数据来源说明和验收结果。

- [ ] **Step 1: 运行最终文件检查**

```powershell
Get-Item .superdesign/data-snapshots/2026-07-22.md, .superdesign/review/daily-overview-2026-07-22-1440x900.png, .superdesign/review/daily-overview-2026-07-22-1100x900.png | Select-Object FullName,Length
```

Expected: 三个文件存在且长度大于 0。

- [ ] **Step 2: 再次读取 Superdesign draft**

```powershell
$env:npm_config_cache=(Resolve-Path .npm-cache).Path
npx.cmd --yes @superdesign/cli@latest get-design --draft-id $newDraftId --json
```

Expected: 当前版本包含 Task 2 的数据刷新提示词；`$newDraftId` 必须是 Task 2 从 `drafts[0].draftId` 机械复制的原值，不得猜测。

- [ ] **Step 3: 整理交付**

交付内容包括 canvas URL、preview URL、两张截图、五项市场指标、三个数据源类别、策略阻断原因和“不构成投资建议”说明。浏览器预览标签页标记为 deliverable，其余研究页关闭。
