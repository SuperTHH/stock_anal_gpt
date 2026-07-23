# A股中长期研究工作流四页面 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在已确认的每日总览 draft `1721cd00-0df1-404d-a6d4-7b823086f1ff` 基础上，生成一个包含每日总览、策略候选池、个股研究、官方事件流、数据质量与来源五个可切换页面的 Superdesign 原型。

**Architecture:** 从已确认 draft 创建一个新分支，保留每日总览为真实数据视图，并用同一 HTML 内的 hash 路由逐页加入四个页面。策略候选池与个股研究共用一份明确标记的虚构演示数据；每日总览、官方事件流和数据质量页只读取真实快照。最终用浏览器对五个路由做语义、交互和 1440/1280/1100 三档像素验收。

**Tech Stack:** Markdown、Superdesign CLI 0.9+、Superdesign Canvas、原生 HTML/CSS/JavaScript 交互、Codex in-app Browser。

## Global Constraints

- 研究日期固定为 `2026-07-22`；真实数据采集日期固定为 `2026-07-23`。
- 市场范围仅含沪深主板、创业板、科创板 A 股；排除北交所、B 股和港股。
- 每日总览、官方事件流、数据质量与来源只显示真实状态。
- 策略候选池和个股研究始终显示 `功能演示数据 · 虚构标的 · 非实时`。
- 演示代码与名称组合只用于界面展示，不对应证券查询或交易。
- 三个策略池各有六只虚构候选，独立排序，不存在跨策略总分。
- 官方事件流只允许三条已核验事件，不加入商业媒体新闻或虚构事件。
- 数据质量页不使用演示水印、虚构覆盖率或综合质量分。
- 页面不得出现自动买入、目标价、稳赚、强烈推荐或确定性收益表述。
- 全部事实、图表、事件和因子使用统一来源抽屉。
- 1440、1280、1100 px 无横向溢出；低于 1180 px 时侧栏与主区偏移均为 64 px。
- 中文正文至少 13px/19px；仅紧凑元数据允许 12px；交互控件至少 32px 高。
- 延续暖纸色、森林绿、方角、细分隔线；不加入渐变、阴影、玻璃效果或夸张圆角。

---

### Task 1: 固化演示数据与跨页面规则

**Files:**
- Create: `.superdesign/data-snapshots/demo-research-workflow-2026-07-22.md`
- Modify: `.superdesign/design-system.md`
- Reference: `.superdesign/data-snapshots/2026-07-22.md`
- Reference: `docs/superpowers/specs/2026-07-23-research-workflow-four-pages-design.md`

**Interfaces:**
- Consumes: 已确认的示例边界、三个策略因子、真实事件和真实质量状态。
- Produces: 四个新增页面共用的唯一演示内容源，以及 Superdesign 必须遵守的路由、水印和隔离规则。

- [ ] **Step 1: 用 `apply_patch` 创建演示数据快照**

文件必须完整写入以下内容：

```markdown
# 2026-07-22 功能演示研究数据

## 使用边界

- display_watermark: 功能演示数据 · 虚构标的 · 非实时
- code_policy: 六位 A 股格式代码；代码与名称组合仅用于界面演示
- forbidden_consumers: 每日总览、官方事件统计、数据质量统计
- holding_horizon: 6个月以上

## 质量成长合理估值

| 代码 | 虚构名称 | 行业 | ROE | ROIC | 收入增长 | 扣非利润增长 | 现金流/利润 | 毛利率波动 | 负债率 | 估值分位 | 完整度 | 风险 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 688901.SH | 远澜微材 | 新材料 | 18.6% | 15.4% | 17.2% | 20.1% | 1.16 | ±1.2pct | 24.8% | 42% | 96% | 客户集中 |
| 300901.SZ | 澄岳智造 | 自动化 | 17.8% | 14.9% | 21.4% | 23.8% | 1.08 | ±1.7pct | 31.5% | 55% | 94% | 海外需求 |
| 603901.SH | 启衡医疗 | 医疗器械 | 16.9% | 13.7% | 14.6% | 18.2% | 1.22 | ±0.9pct | 19.4% | 61% | 97% | 注册审批 |
| 002901.SZ | 森屿自动化 | 工业设备 | 15.7% | 12.8% | 18.1% | 16.5% | 0.98 | ±2.1pct | 28.7% | 47% | 91% | 应收增长 |
| 688902.SH | 星屿光电 | 光学器件 | 19.3% | 16.2% | 25.6% | 28.4% | 1.04 | ±2.6pct | 22.6% | 68% | 92% | 周期波动 |
| 300902.SZ | 云岑软件 | 企业软件 | 21.1% | 19.0% | 16.8% | 19.7% | 1.34 | ±0.6pct | 8.2% | 73% | 95% | 大客户依赖 |

## 低估值价值

| 代码 | 虚构名称 | 行业 | PE | PB | FCF收益率 | 历史分位 | 行业分位 | 资产质量 | 价值陷阱风险 | 完整度 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: |
| 600931.SH | 沧源装备 | 通用机械 | 9.8 | 1.1 | 11.2% | 18% | 22% | B+ | 中 | 94% |
| 000931.SZ | 汇川建材 | 建材 | 8.4 | 0.9 | 8.7% | 15% | 20% | B | 高 | 91% |
| 601931.SH | 北澜物流 | 物流 | 10.2 | 1.0 | 9.6% | 27% | 31% | A- | 低 | 96% |
| 002931.SZ | 嘉砺家居 | 家居 | 11.5 | 1.3 | 7.9% | 32% | 28% | B+ | 中 | 92% |
| 603931.SH | 恒砺化工 | 基础化工 | 7.9 | 1.2 | 13.4% | 12% | 17% | B | 高 | 90% |
| 300931.SZ | 远岱环保 | 环保设备 | 12.6 | 1.4 | 8.1% | 36% | 39% | A- | 低 | 95% |

## 稳定高股息

| 代码 | 虚构名称 | 行业 | 股息率 | 连续分红 | 派息率 | FCF覆盖 | 负债率 | 削减风险 | 完整度 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| 600961.SH | 宁泽公用 | 公用事业 | 5.8% | 10年 | 55% | 1.8x | 36% | 低 | 97% |
| 000961.SZ | 海晟港务 | 港口 | 5.2% | 9年 | 48% | 2.1x | 29% | 中 | 95% |
| 601961.SH | 华砺能源 | 能源 | 6.1% | 12年 | 62% | 1.5x | 43% | 中 | 94% |
| 002961.SZ | 云岭包装 | 包装 | 4.7% | 8年 | 44% | 2.4x | 21% | 低 | 93% |
| 603961.SH | 衡远交运 | 交通运输 | 5.5% | 11年 | 58% | 1.7x | 39% | 中 | 96% |
| 300961.SZ | 澄海检测 | 检测服务 | 4.3% | 7年 | 39% | 2.8x | 12% | 低 | 92% |

## 选中个股：远澜微材 688901.SH

| 年度 | 营收亿元 | 扣非利润亿元 | ROE | ROIC | 经营现金流亿元 | 毛利率 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2021 | 42.8 | 4.2 | 13.8% | 11.9% | 4.8 | 34.8% |
| 2022 | 49.5 | 5.1 | 15.1% | 12.8% | 5.6 | 35.5% |
| 2023 | 57.9 | 6.0 | 16.4% | 13.7% | 6.5 | 35.2% |
| 2024 | 66.1 | 7.4 | 17.5% | 14.6% | 8.0 | 36.1% |
| 2025 | 75.4 | 8.9 | 18.6% | 15.4% | 10.3 | 36.0% |

- valuation: PE 26.4x; PB 4.2x; EV/EBITDA 18.1x; FCF收益率 3.7%; 历史PE分位 42%; 行业PE分位 38%
- balance_sheet: 负债率 24.8%; 净现金 8.6亿元; 应收周转天数 71; 存货周转天数 93
- shareholder_return: 近五年每股分红 0.32/0.38/0.45/0.56/0.68 元; 最新派息率 31%
- selection_reason: ROIC连续提升、现金流覆盖利润、估值位于自身历史中位以下
- catalysts: 新产线良率验证、两项客户认证、产品结构向高毛利材料升级
- risks: 前五大客户集中、原材料价格波动、扩产折旧压制短期利润
- observe: 连续两个季度经营现金流/扣非利润大于1；新产线良率达到90%
- invalidate: ROIC连续两期低于12%；应收增速连续两期高于收入增速10pct
```

- [ ] **Step 2: 用 `apply_patch` 向设计系统追加跨页面规则**

追加以下内容，不修改已有颜色、字体和响应式规则：

```markdown
## Research workflow four-page supplement

- Base draft: 1721cd00-0df1-404d-a6d4-7b823086f1ff
- Routes: #overview, #candidates, #stock, #events, #quality
- Demo-only routes: #candidates, #stock
- Demo watermark: 功能演示数据 · 虚构标的 · 非实时
- Real-only routes: #overview, #events, #quality
- Demo facts never change real coverage, event count, data readiness, or strategy-blocked status.
- Candidate state survives navigation to #stock and back through sessionStorage plus browser history.
- All routes use one 420–480 px provenance drawer.
```

- [ ] **Step 3: 验证内容完整性**

Run:

```powershell
rg -n "远澜微材|沧源装备|宁泽公用|功能演示数据|invalidate" .superdesign/data-snapshots/demo-research-workflow-2026-07-22.md
rg -n "#overview|#candidates|#stock|#events|#quality|sessionStorage" .superdesign/design-system.md
git diff --check
```

Expected: 第一条命令命中五类关键内容；第二条命令命中五个路由和状态保留规则；`git diff --check` 无输出。

- [ ] **Step 4: 提交共享上下文**

```powershell
git add .superdesign/data-snapshots/demo-research-workflow-2026-07-22.md .superdesign/design-system.md
git commit -m "docs: add research workflow demo context"
```

Expected: 提交只包含演示快照和设计系统补充。

### Task 2: 创建多页面分支与策略候选池

**Files:**
- Read: `.superdesign/design-system.md`
- Read: `.superdesign/data-snapshots/demo-research-workflow-2026-07-22.md`
- Read: `docs/superpowers/specs/2026-07-23-research-workflow-four-pages-design.md`
- Create after generation: `.superdesign/review/research-workflow-draft-2026-07-23.md`
- External output: one Superdesign branch from `1721cd00-0df1-404d-a6d4-7b823086f1ff`

**Interfaces:**
- Consumes: Task 1 的设计系统补充和 18 只虚构候选。
- Produces: 一个保留每日总览并加入 `#candidates` 的 workflow draft；输出的 draft ID、canvas URL、preview URL 逐字记录到 review manifest。

- [ ] **Step 1: 验证 Superdesign CLI**

```powershell
$env:npm_config_cache=(Resolve-Path '.npm-cache').Path
npx.cmd --yes @superdesign/cli@latest --version
npx.cmd --yes @superdesign/cli@latest get-design --draft-id 1721cd00-0df1-404d-a6d4-7b823086f1ff --json
```

Expected: 两条命令退出码均为 0；第二条返回标题、HTML 和版本历史。

- [ ] **Step 2: 创建 workflow 分支**

运行一次 `iterate-design-draft --mode branch --count 1`。只传一个 `-p`，提示词完整使用以下内容：

```text
Preserve the approved 2026-07-22 daily overview as route #overview with no content or visual regressions. Convert this draft into a five-route research prototype using lightweight hash routing in the existing HTML/CSS/JavaScript; implement #overview and #candidates now, while navigation entries for #stock, #events, and #quality may show a restrained “页面正在加入本设计分支” state until later iterations. Keep the exact approved app shell, warm-paper palette, forest rail, square geometry, dividers, typography floors, 32px controls, provenance behavior, and 208px/64px responsive rail.

Create #candidates with active nav 策略候选池 and a persistent fixed watermark “功能演示数据 · 虚构标的 · 非实时”. Provide three clickable independent tabs: 质量成长合理估值, 低估值价值, 稳定高股息. Each tab must render exactly the six fictitious candidates and exact factor values from demo-research-workflow-2026-07-22.md. Do not create a shared total score, global rank, or cross-strategy comparison. Common filters: 行业, 市值, 流动性, 风险标记, 数据完整度, 观察状态. Use a dense table with code, fictitious name, industry, selection evidence, strategy-specific factors, risk, completeness, observation condition, invalidation condition. The selected row is 远澜微材 688901.SH and uses forest-100 plus a 2px forest left edge. A right evidence panel shows 入选理由, 6个月以上催化剂, 风险, 观察条件, 失效条件 and a 32px “进入个股研究” action.

Implement working tab switching. Before navigating to #stock, store current strategy tab, selected code, filter values, and scroll position in sessionStorage. Returning through browser history must restore those values. Add the unified right provenance drawer; demo records identify their source as 功能演示数据集 and never display a fabricated real URL. Candidate and stock demo data must never alter #overview real metrics, official event count, data quality, or blocked real strategy state. No buy instruction, target price, certainty language, radar chart, gradients, shadows, or new colors/fonts.
```

Context files:

```text
--context-file .superdesign/design-system.md
--context-file .superdesign/data-snapshots/demo-research-workflow-2026-07-22.md
--context-file .superdesign/data-snapshots/2026-07-22.md
--context-file docs/superpowers/specs/2026-07-23-research-workflow-four-pages-design.md
--user-request "确认本版并展开其余四个页面，PRD先不着急编写"
--device custom --width 1440 --height 900 --json
```

把上方完整提示词赋给 `$candidatePrompt`，然后运行：

```powershell
$env:npm_config_cache=(Resolve-Path '.npm-cache').Path
npx.cmd --yes @superdesign/cli@latest iterate-design-draft `
  --draft-id 1721cd00-0df1-404d-a6d4-7b823086f1ff `
  -p $candidatePrompt `
  --mode branch --count 1 `
  --device custom --width 1440 --height 900 `
  --context-file .superdesign/design-system.md `
                 .superdesign/data-snapshots/demo-research-workflow-2026-07-22.md `
                 .superdesign/data-snapshots/2026-07-22.md `
                 docs/superpowers/specs/2026-07-23-research-workflow-four-pages-design.md `
  --user-request "确认本版并展开其余四个页面，PRD先不着急编写" `
  --json
```

Expected: JSON 中恰好一个 `drafts[0]`，包含非空 `draftId`、canvas URL 和 preview URL。

- [ ] **Step 3: 记录不可猜测的动态 ID**

用 `apply_patch` 创建 `.superdesign/review/research-workflow-draft-2026-07-23.md`。逐字复制 CLI 返回的 draft ID、canvas URL、preview URL，不得修改、缩写或根据 URL 推导。文件标题为 `Research workflow Superdesign delivery`，并包含以下字段：

- `base_draft_id` 固定写入 `1721cd00-0df1-404d-a6d4-7b823086f1ff`；
- `workflow_draft_id` 逐字写入本次 JSON 的 `drafts[0].draftId`；
- `canvas_url` 逐字写入本次 JSON 的 canvas URL；
- `preview_url` 逐字写入本次 JSON 的 preview URL；
- `implemented_routes` 写入 `#overview, #candidates`；
- `pending_routes` 写入 `#stock, #events, #quality`。

三个动态字段必须为非空具体值，且不得包含说明性文字。

- [ ] **Step 4: 候选池冒烟验收**

在浏览器打开 manifest 中的 preview URL，进入 `#candidates` 并断言：

```text
水印出现 1 次
策略页签出现 3 个
当前页签候选行恰好 6 行
点击三个页签后每个页签都恰好 6 行
远澜微材 688901.SH 为选中行
右侧存在 进入个股研究
页面不存在 总分、综合排名、目标价、立即买入
```

Expected: 所有断言通过；失败时不进入 Task 3，先用一次 `--mode replace` 只修复失败项并重复本步骤。

### Task 3: 加入个股研究页面

**Files:**
- Read: `.superdesign/review/research-workflow-draft-2026-07-23.md`
- Read: `.superdesign/data-snapshots/demo-research-workflow-2026-07-22.md`
- External output: replace current workflow draft

**Interfaces:**
- Consumes: Task 2 记录的 workflow draft ID 和候选状态协议。
- Produces: 可从远澜微材候选进入的 `#stock` 页面，并可恢复候选页上下文。

- [ ] **Step 1: 读取当前 workflow draft**

从 manifest 机械复制 `workflow_draft_id`，运行：

```powershell
$env:npm_config_cache=(Resolve-Path '.npm-cache').Path
npx.cmd --yes @superdesign/cli@latest get-design --draft-id $workflowDraftId --json
```

`$workflowDraftId` 在执行命令前必须设置为 manifest 中的原始值。Expected: HTML 同时包含 `#overview`、`#candidates` 和三策略页签。

- [ ] **Step 2: 原位加入个股研究**

对同一个 draft 执行一次 `iterate-design-draft --mode replace`，提示词完整使用：

```text
Preserve #overview and #candidates exactly, including all values, tabs, filters, sessionStorage state, responsive behavior, and visual rules. Replace only the temporary #stock state with a complete stock-research route for the selected fictitious company 远澜微材 688901.SH. The fixed watermark “功能演示数据 · 虚构标的 · 非实时” must remain visible at every scroll position.

Header: 远澜微材, 688901.SH, 新材料, 质量成长合理估值, 研究日期 2026-07-22. First viewport: 入选理由, 6个月以上催化剂, 主要风险, 观察条件, 失效条件. Add five-year financial quality using the exact 2021–2025 revenue, adjusted profit, ROE, ROIC, operating cash flow and gross-margin values in the demo snapshot. Add valuation with PE 26.4x, PB 4.2x, EV/EBITDA 18.1x, FCF yield 3.7%, historical PE percentile 42%, industry PE percentile 38%. Add balance sheet, shareholder return, demo event/catalyst timeline, and a right research memo with evidence and counter-evidence.

Charts must be flat line, bar, and percentile-band charts with explicit time window, cutoff, and demo status. Every chart and fact can open the shared provenance drawer; source name is 功能演示数据集 and no real URL is invented. Show only the current strategy fit, never a cross-strategy total. The back action uses browser history and restores the candidate strategy tab, selected code, filters, and scroll position. Do not add buy, target-price, certainty, recommendation, gradient, shadow, radar, or new font/color treatments.
```

Use the same four context files as Task 2 and the same custom 1440×900 device. Expected: the returned draft ID is unchanged because replace mode updates the workflow draft in place.

把上方完整提示词赋给 `$stockPrompt`，然后运行：

```powershell
$match = Select-String -Path '.superdesign/review/research-workflow-draft-2026-07-23.md' -Pattern '^- workflow_draft_id:\s*(.+)$'
$workflowDraftId = $match.Matches[0].Groups[1].Value.Trim()
if ([string]::IsNullOrWhiteSpace($workflowDraftId)) { throw 'workflow draft ID is empty' }
$env:npm_config_cache=(Resolve-Path '.npm-cache').Path
npx.cmd --yes @superdesign/cli@latest iterate-design-draft `
  --draft-id $workflowDraftId -p $stockPrompt --mode replace `
  --device custom --width 1440 --height 900 `
  --context-file .superdesign/design-system.md `
                 .superdesign/data-snapshots/demo-research-workflow-2026-07-22.md `
                 .superdesign/data-snapshots/2026-07-22.md `
                 docs/superpowers/specs/2026-07-23-research-workflow-four-pages-design.md `
  --user-request "确认本版并展开其余四个页面，PRD先不着急编写" `
  --json
```

- [ ] **Step 3: 个股页冒烟验收**

从 `#candidates` 点击远澜微材的“进入个股研究”，断言：

```text
URL hash = #stock
页头包含 远澜微材、688901.SH、质量成长合理估值、2026-07-22
水印可见
五年数据包含 2021 和 2025
模块包含 财务质量、估值、资产负债、股东回报、事件与催化剂、研究备忘录
打开任一图表来源后，抽屉包含 功能演示数据集
返回后恢复原策略页签和远澜微材选中状态
页面不存在 目标价、立即买入、强烈推荐、稳赚
```

Expected: 全部通过；失败时执行一次只包含失败断言的 replace 修补并重测。

### Task 4: 加入真实官方事件流

**Files:**
- Read: `.superdesign/review/research-workflow-draft-2026-07-23.md`
- Read: `.superdesign/data-snapshots/2026-07-22.md`
- External output: replace current workflow draft

**Interfaces:**
- Consumes: 真实快照中的三条事件。
- Produces: 只含真实官方事件的 `#events` 页面。

- [ ] **Step 1: 原位加入事件页面**

对 manifest 中的 workflow draft 执行一次 replace，提示词完整使用：

```text
Preserve #overview, #candidates, and #stock exactly. Replace only the temporary #events state. Create the real-data Official Events page with active nav 官方事件流, cutoff 2026-07-22 23:59, collected 2026-07-23, and no demo watermark.

Render exactly three events in descending publication date and no others: 2026-07-22 SSE ETF options exercise/settlement reminder; 2026-07-21 CSRC symposium with listed companies, institutions, and experts; 2026-07-16 NBS Q2/H1 GDP preliminary accounting. Copy their factual summaries, system assessments, horizons, dates, confidence where available, and official URLs exactly from 2026-07-22.md. Each row permanently separates 官方事实 from 系统研判. Filters: source, event type, date, horizon, confidence, affected object. Add working switches “仅看事实/显示系统研判” and “半年以上相关”.

The right detail area shows source summary, neutral transmission chain, horizon, related strategy, and observation data. It may reference fictitious candidate sectors only as explicitly labeled demo associations; it must never imply an official source named a fictitious company or generate a stock recommendation. Every event exposes published_at, effective_at, collected_at, version, horizon, confidence, and original source. When filters yield no rows, show 暂无符合条件的官方事件 and never commercial news. Preserve shared provenance drawer, shell, responsiveness, and all visual tokens.
```

Context files include design system, real snapshot and approved four-page spec. Expected: draft ID unchanged.

把上方完整提示词赋给 `$eventsPrompt`，然后运行：

```powershell
$match = Select-String -Path '.superdesign/review/research-workflow-draft-2026-07-23.md' -Pattern '^- workflow_draft_id:\s*(.+)$'
$workflowDraftId = $match.Matches[0].Groups[1].Value.Trim()
if ([string]::IsNullOrWhiteSpace($workflowDraftId)) { throw 'workflow draft ID is empty' }
$env:npm_config_cache=(Resolve-Path '.npm-cache').Path
npx.cmd --yes @superdesign/cli@latest iterate-design-draft `
  --draft-id $workflowDraftId -p $eventsPrompt --mode replace `
  --device custom --width 1440 --height 900 `
  --context-file .superdesign/design-system.md `
                 .superdesign/data-snapshots/2026-07-22.md `
                 docs/superpowers/specs/2026-07-23-research-workflow-four-pages-design.md `
  --user-request "确认本版并展开其余四个页面，PRD先不着急编写" `
  --json
```

- [ ] **Step 2: 事件页冒烟验收**

在 `#events` 断言：

```text
事件行恰好 3 条
日期顺序为 2026-07-22、2026-07-21、2026-07-16
官方事实标签 3 个
系统研判标签 3 个
官方原文链接 3 个
页面不存在 功能演示数据、水印、东方财富、雪球、第一财经、证券时报
切换 仅看事实 后系统研判正文隐藏
切换 半年以上相关 后 ETF 期权事件不在结果中
```

Expected: 全部通过；失败时执行一次只修复失败项的 replace 并重测。

### Task 5: 加入真实数据质量与来源页面

**Files:**
- Read: `.superdesign/review/research-workflow-draft-2026-07-23.md`
- Read: `.superdesign/data-snapshots/2026-07-22.md`
- Read: `docs/superpowers/specs/2026-07-23-research-workflow-four-pages-design.md`
- External output: replace current workflow draft

**Interfaces:**
- Consumes: 真实数据域状态、来源政策和任务降级规则。
- Produces: 不含虚构评分的 `#quality` 页面，完成五页面 workflow。

- [ ] **Step 1: 原位加入质量页面**

对 manifest 中的 workflow draft 执行一次 replace，提示词完整使用：

```text
Preserve #overview, #candidates, #stock, and #events exactly. Replace only the temporary #quality state. Create the real-state Data Quality and Sources page with active nav 数据质量与来源, report date 2026-07-22, collected 2026-07-23, overall status “部分可用 · 策略输出阻断”, and no demo watermark.

Create a data-domain status matrix with exact states: 交易所市场概览 已完成; 官方事件流 已完成; Tushare全市场日线 缺失; 财务事实/XBRL 缺失; 公司行动与复权 待验证; 当时可知校验 待验证; 三个策略候选池 因关键数据缺失暂停输出. Do not show any composite quality score, invented coverage percentage, circular gauge, or 0–100 bar.

Create SourcePolicy registry rows for 上交所, 深交所, 中国证监会, 国家统计局, 巨潮资讯, Tushare. Columns: allowed use, collection frequency, full-text storage rule, terms-review status/date, connection state, official link. If a written terms-review date is unavailable, display 未记录; never substitute the page date. Add a lineage view: 公开来源 → 原始采集 → 标准化与版本化 → 当时可知校验 → 分析页面, with 完成/缺失/阻断/待验证 states and affected pages/strategies. Add 21:30 run history, failure reason, retry state, next schedule, “本期未更新” fallback, and refusal records for non-whitelist domains and unauthorized attachments. The record inspector shows source_url, published_at, effective_at, collected_at, version, license_policy, quality_status. Preserve shared shell and provenance drawer.
```

Context files include design system, real snapshot and approved four-page spec. Expected: draft ID unchanged.

把上方完整提示词赋给 `$qualityPrompt`，然后运行：

```powershell
$match = Select-String -Path '.superdesign/review/research-workflow-draft-2026-07-23.md' -Pattern '^- workflow_draft_id:\s*(.+)$'
$workflowDraftId = $match.Matches[0].Groups[1].Value.Trim()
if ([string]::IsNullOrWhiteSpace($workflowDraftId)) { throw 'workflow draft ID is empty' }
$env:npm_config_cache=(Resolve-Path '.npm-cache').Path
npx.cmd --yes @superdesign/cli@latest iterate-design-draft `
  --draft-id $workflowDraftId -p $qualityPrompt --mode replace `
  --device custom --width 1440 --height 900 `
  --context-file .superdesign/design-system.md `
                 .superdesign/data-snapshots/2026-07-22.md `
                 docs/superpowers/specs/2026-07-23-research-workflow-four-pages-design.md `
  --user-request "确认本版并展开其余四个页面，PRD先不着急编写" `
  --json
```

- [ ] **Step 2: 更新 workflow manifest**

用 `apply_patch` 把 manifest 的路由状态改为：

```markdown
- implemented_routes: #overview, #candidates, #stock, #events, #quality
- pending_routes: none
```

同时记录每次 replace 返回的最新版本号；只复制 CLI 实际返回值。

- [ ] **Step 3: 质量页冒烟验收**

在 `#quality` 断言：

```text
总体状态包含 部分可用 和 策略输出阻断
已完成 2 项
缺失 2 项
待验证 2 项
策略候选池暂停输出 1 项
来源登记表包含 6 个白名单来源
血缘包含 5 个节点
字段包含 source_url、published_at、effective_at、collected_at、version、license_policy、quality_status
页面不存在 功能演示数据、综合质量分、0–100、虚构覆盖率
```

Expected: 全部通过；失败时执行一次只修复失败项的 replace 并重测。

- [ ] **Step 4: 提交 workflow manifest**

```powershell
git add .superdesign/review/research-workflow-draft-2026-07-23.md
git commit -m "docs: record research workflow Superdesign draft"
```

Expected: 提交只包含已生成 draft 的可追溯 ID、URL、路由和版本记录。

### Task 6: 五页面语义与交互验收

**Files:**
- Read: `.superdesign/review/research-workflow-draft-2026-07-23.md`
- Verify: Superdesign preview DOM

**Interfaces:**
- Consumes: Task 5 的完整五页面 preview。
- Produces: 一份全部通过或明确列出失败断言的语义验收结果。

- [ ] **Step 1: 验证路由与导航**

在 1440×900 浏览器视口依次访问并点击：

```text
#overview → #candidates → #stock → 浏览器返回 → #events → #quality
```

Expected:

```text
五个路由均在同一 preview URL 内加载
每个路由只有一个激活导航项
#overview 的 2026-07-22 真实总览内容未改变
#candidates 返回后恢复策略、筛选、选中行和滚动位置
```

- [ ] **Step 2: 验证真实与演示数据隔离**

读取五个路由的可见文本并断言：

```text
#candidates 和 #stock 均含 功能演示数据 · 虚构标的 · 非实时
#overview、#events、#quality 均不含该水印
#overview 仍显示 策略池 0/3 或等价阻断状态
#events 事件数始终为 3
#quality 仍显示 Tushare全市场日线 缺失 和 财务事实/XBRL 缺失
```

- [ ] **Step 3: 验证统一来源抽屉**

分别从候选因子、个股图表、官方事件和质量记录打开来源抽屉。Expected:

```text
抽屉宽度在 420–480px
四类入口共用相同字段顺序
演示入口显示 功能演示数据集 且没有伪造 URL
真实入口显示官方 URL
关闭抽屉后焦点回到触发元素
```

- [ ] **Step 4: 全局禁用文案扫描**

在五个路由的 DOM 文本中搜索：

```text
立即买入
强烈推荐
稳赚
目标价
跨策略总分
综合排名
东方财富
雪球
第一财经
证券时报
```

Expected: 零命中。若“综合排名”仅出现在解释其不存在的提示中，也应改写，避免视觉误读。

### Task 7: 三档像素复核、截图与一次最终修补

**Files:**
- Create: `.superdesign/review/research-workflow-overview-1440x900.png`
- Create: `.superdesign/review/research-workflow-candidates-1440x900.png`
- Create: `.superdesign/review/research-workflow-stock-1440x900.png`
- Create: `.superdesign/review/research-workflow-events-1440x900.png`
- Create: `.superdesign/review/research-workflow-quality-1440x900.png`
- Create: `.superdesign/review/research-workflow-overview-1100x900.png`
- Create: `.superdesign/review/research-workflow-candidates-1100x900.png`
- Create: `.superdesign/review/research-workflow-stock-1100x900.png`
- Create: `.superdesign/review/research-workflow-events-1100x900.png`
- Create: `.superdesign/review/research-workflow-quality-1100x900.png`
- Modify: `.superdesign/review/research-workflow-draft-2026-07-23.md`

**Interfaces:**
- Consumes: Task 6 语义验收通过的 preview。
- Produces: 十张最终截图、三档布局测量和最多一次综合修补后的稳定 draft。

- [ ] **Step 1: 1440×900 像素复核**

逐页测量：

```text
document.scrollWidth = 1440
aside width = 208
main left = 208
正文 computed font-size >= 13px
正文 computed line-height >= 19px
关键按钮 height >= 32px
水印或真实状态在首屏可见
来源入口在首屏或模块标题区可见
无阴影、渐变、玻璃效果和大圆角
```

人工检查表格列对齐、行高、标签密度、折线可读性、抽屉层级和长中文断行。每个路由保存对应的 1440×900 PNG。

- [ ] **Step 2: 1280×900 响应式复核**

逐页测量：

```text
document.scrollWidth = 1280
aside width = 208
main left = 208
最右侧控件 right <= 1280
表格不裁切关键代码、名称、风险和操作列
```

Expected: 五页全部通过；此档只记录测量，不要求截图。

- [ ] **Step 3: 1100×900 响应式复核**

逐页测量：

```text
document.scrollWidth = 1100
aside width = 64
main left = 64
main right <= 1100
可见侧栏文字标签 = 0
导航图标仍可识别并有 aria-label 或 title
表格使用受控列收缩、换行或内部滚动，不造成文档横向溢出
```

每个路由保存对应的 1100×900 PNG。

- [ ] **Step 4: 汇总失败项并最多修补一次**

若 Tasks 6–7 存在任何失败，先形成一个按路由分组的精确列表，再对同一 workflow draft 执行一次 `iterate-design-draft --mode replace`。提示词必须：

```text
只列实际失败的 DOM 或像素断言；
要求保留所有通过项和全部真实/演示数据边界；
禁止重新设计配色、字体、信息架构和数据；
禁止新增页面、事件、候选或评分。
```

修补后重新执行 Task 6 全部步骤和 Task 7 Steps 1–3。不得进行第二次自动修补；若仍失败，保留证据并报告用户决定。

- [ ] **Step 5: 记录最终验收状态**

用 `apply_patch` 在 workflow manifest 追加：

```markdown
## Final QA

- semantic_routes: 5/5
- candidate_tabs: 3/3, 6 rows each
- official_events: 3/3
- responsive_widths: 1440, 1280, 1100
- horizontal_overflow: none
- screenshots: 10
- repair_passes: 记录实际 0 或 1
```

只记录实际结果；任何失败不得写成通过。

- [ ] **Step 6: 提交验收产物**

```powershell
git add .superdesign/review/research-workflow-draft-2026-07-23.md .superdesign/review/research-workflow-*.png
git commit -m "test: record research workflow visual review"
```

Expected: 提交包含 workflow manifest 和十张最终截图。

### Task 8: 完成前验证与交付

**Files:**
- Verify: `.superdesign/data-snapshots/demo-research-workflow-2026-07-22.md`
- Verify: `.superdesign/review/research-workflow-draft-2026-07-23.md`
- Verify: `.superdesign/review/research-workflow-*.png`
- Verify: `docs/superpowers/specs/2026-07-23-research-workflow-four-pages-design.md`

**Interfaces:**
- Consumes: Tasks 1–7 的全部本地和外部产物。
- Produces: 可点击的 canvas/preview 链接、截图、验收结论和未完成项说明。

- [ ] **Step 1: 文件级验证**

```powershell
Get-Item `
  .superdesign/data-snapshots/demo-research-workflow-2026-07-22.md, `
  .superdesign/review/research-workflow-draft-2026-07-23.md, `
  .superdesign/review/research-workflow-*.png |
  Select-Object FullName,Length
```

Expected: 一份演示快照、一份 manifest、十张 PNG 均存在且长度大于 0。

- [ ] **Step 2: 文本边界验证**

```powershell
rg -n "功能演示数据 · 虚构标的 · 非实时|三个策略候选池|Tushare全市场日线|官方事件流" `
  .superdesign/data-snapshots/demo-research-workflow-2026-07-22.md `
  .superdesign/review/research-workflow-draft-2026-07-23.md `
  docs/superpowers/specs/2026-07-23-research-workflow-four-pages-design.md
$unfinishedPatterns = @(('T'+'BD'),('T'+'ODO'),('待'+'定'),('说明性'+'文字'))
Select-String `
  -Path .superdesign/data-snapshots/demo-research-workflow-2026-07-22.md,`
        .superdesign/review/research-workflow-draft-2026-07-23.md `
  -Pattern $unfinishedPatterns
```

Expected: 第一条命中水印和真实状态要求；第二条零命中。

- [ ] **Step 3: 再次读取最终 Superdesign draft**

从 manifest 读取原始 workflow draft ID，运行 `get-design --json`。Expected: HTML 包含五个路由、三个策略页签、远澜微材、三条真实事件和真实质量矩阵。

- [ ] **Step 4: Git 验证**

```powershell
git status --short
git log -5 --oneline
git diff --check HEAD~2..HEAD
```

Expected: 工作区无未提交改动；最近提交包含共享上下文、draft manifest 和视觉复核；`git diff --check` 无输出。

- [ ] **Step 5: 交付**

最终回复必须包含：

- workflow canvas URL；
- workflow preview URL；
- 十张截图的本地可点击链接；
- 五个路由及其真实/演示属性；
- 语义、交互、1440/1280/1100 验收结果；
- 实际修补次数；
- “仅供个人研究，不构成投资建议”；
- 若仍有失败，明确列出，不得声称完成。
