# A-Share Long-Horizon Research Terminal — Design System

## 1. Product context

This is a desktop-first, read-only personal research workspace for mainland China A-shares. It supports an expected holding period of six months or longer and produces a daily post-close report at 21:30 Asia/Shanghai. The product does not issue automatic buy orders. It presents independent candidate pools, evidence, risks, invalidation conditions, data freshness, and source traceability.

Primary users are disciplined individual investors who need dense, auditable information rather than a consumer trading app. All prototype market values and company names are illustrative unless explicitly marked as live data.

The five product areas are:

1. Daily overview
2. Three independent strategy pools
3. Stock detail and research dossier
4. Official events stream
5. Data quality and source governance

## 2. Visual direction

Create a restrained professional research terminal with the precision of an architectural blueprint and the readability of an institutional research report.

- Desktop canvas: 1440 px wide, optimized for 1280–1600 px.
- Background: warm paper white, never pure white.
- Information density: high, but grouped with strong alignment and predictable spacing.
- Geometry: flat rectangular regions, square corners or 2 px radius only.
- Dividers: thin 1 px rules; use structure instead of card shadows.
- No gradients, glass effects, glow, drop shadows, pill-heavy UI, oversized marketing typography, decorative illustrations, or playful consumer-finance styling.
- Red and green are semantic market colors only. Amber is reserved for risk, warning, stale, or unverified states.
- Neutral charcoal and forest tones carry navigation, typography, and primary actions.

## 3. Color tokens

### Foundation

- `paper-0`: `#F7F7F5` — global canvas
- `paper-1`: `#FFFFFF` — table and focused content surface
- `paper-2`: `#EFEFEB` — secondary bands, inactive rows
- `ink-900`: `#1E2220` — primary text
- `ink-700`: `#3A3A38` — grid lines, secondary headings
- `ink-500`: `#6F746F` — metadata and helper text
- `ink-300`: `#C8CBC5` — hairline borders
- `forest-900`: `#1A3C2B` — left rail, primary buttons, active navigation
- `forest-100`: `#DCE8E1` — restrained selection background

### Semantic market states

- `market-up`: `#C6463A` — A-share convention: rise / positive price move
- `market-up-bg`: `#F5E3E0`
- `market-down`: `#138A5B` — A-share convention: fall / negative price move
- `market-down-bg`: `#DDEDE6`
- `risk-amber`: `#C58A22` — warnings, data gaps, stale status, elevated risk
- `risk-amber-bg`: `#F4E9D2`
- `info-blue`: `#356A8A` — neutral information and source links only

Never use market red or green as decoration, navigation, or generic success/error colors. Use text, icons, and labels in addition to color.

## 4. Typography

- Headings and navigation: **Space Grotesk**, 500–600 weight.
- Chinese body copy and tables: **General Sans**, with system CJK fallback `PingFang SC`, `Microsoft YaHei`, sans-serif.
- Numeric data, codes, timestamps, percentages, and provenance IDs: **JetBrains Mono**, 400–500 weight.

Desktop type scale:

- Page title: 26/34, weight 600
- Section title: 17/24, weight 600
- Card or module title: 14/20, weight 600
- Body: 13/20, weight 400
- Dense table: 12/18, weight 400
- Metadata: 11/16, weight 400
- Large KPI: 24/30, weight 500, tabular numbers

Do not use uppercase for Chinese labels. Use uppercase only for short Latin metadata such as ROE, ROIC, PE, PB, FCF, XBRL.

## 5. Grid and spacing

- App shell: 208 px fixed left navigation + flexible main workspace.
- Main workspace padding: 24 px horizontal and 20 px vertical.
- Content grid: 12 columns, 16 px gutters.
- Base spacing unit: 4 px.
- Common spacing: 4, 8, 12, 16, 20, 24, 32 px.
- Module internal padding: 16 px; dense table cells: 8 px vertical, 10–12 px horizontal.
- Section gap: 16 px; major section gap: 24 px.
- Border radius: 0 px by default, 2 px maximum.

## 6. App shell and navigation

Left navigation uses `forest-900` with warm-white text. It contains:

- Product mark: “衡策” and descriptor “A股长期研究台”
- Main items: 每日总览, 策略候选池, 个股研究, 官方事件流
- Governance item: 数据质量与来源
- Bottom metadata: “个人研究 · 非投资建议”, current report date, system version

Navigation icons are thin 1.5 px outline icons from a consistent library. Active item uses a paper-white rectangular indicator and forest text, not a rounded pill.

The top utility row in the main workspace contains report date, “数据截至” timestamp, data readiness state, search, and a compact source-trace action.

## 7. Core components

### Status strip

A full-width 36–40 px band directly below the page header. It summarizes report readiness, last successful update, coverage, and warnings. Use a thin border and restrained semantic background. Never hide stale or incomplete data.

### KPI cells

Flat cells separated by vertical rules. Each includes label, value, delta or qualifier, and timestamp/source metadata where relevant. Avoid floating cards.

### Strategy modules

The three strategies must remain visually independent and must never be merged into a single total ranking:

- 质量成长合理估值
- 低估值价值
- 稳定高股息

Each module includes its own candidate count, screening pass rate, top candidates, factor evidence, risk flags, and “查看候选池” action. Use a neutral header with a small distinct line marker, but do not assign market red/green to a strategy identity.

### Dense research table

- Sticky header, subtle row rules, paper-white surface.
- Left-align names and narrative; right-align all numerics.
- Stock code, date, and ratios use JetBrains Mono.
- Include explicit column sort indicators.
- Risk flags are compact rectangular labels with icon + text.
- Row hover uses `paper-2`; selected row uses `forest-100` and a 2 px forest left edge.
- Never communicate state with color alone.

### Charts

- Flat line, bar, heat strip, or percentile band charts.
- Minimal axes and labels; no 3D, gradient fills, thick decorative lines, or excessive legends.
- Market rise is red and fall is green, following mainland convention.
- Historical valuation bands use neutral ink and forest tints.
- Every chart shows time window, data cutoff, and source on hover or in metadata.

### Official event rows

Each event shows event type, concise factual summary, affected companies or sectors, possible impact horizon, confidence, published time, and original source link. Summaries must distinguish facts from system inference. Commercial media full text is never displayed.

### Provenance drawer

A right-side inspection drawer, 420–480 px wide, displaying source name, source URL, published time, effective time, collected time, version, license policy, and quality status. It is available from tables, charts, events, and stock detail facts.

## 8. Daily overview page layout

The initial prototype page should use this hierarchy:

1. Header: “每日研究总览”, trading date, report timestamp, compact search.
2. Data readiness strip: show whether the 21:30 report is complete, coverage percentage, warning count, and previous valid report fallback state.
3. Market context row: 4–5 compact KPIs for broad indices, turnover, breadth, and macro/regulatory state. All values are illustrative in the prototype.
4. Main left area (8 columns): three independent strategy modules, each with three representative candidates and factor/risk columns.
5. Main right area (4 columns): risk radar and data-quality snapshot.
6. Bottom left (7 columns): official events timeline with source links.
7. Bottom right (5 columns): watch conditions and upcoming checkpoints for six-month-plus research horizons.
8. Footer audit line: data cutoff, source policy review date, disclaimer.

## 9. Content and decision language

Use concise professional Chinese. Prefer evidence-first labels:

- “入选理由”
- “观察条件”
- “失效条件”
- “风险标记”
- “数据完整度”
- “数据截至”
- “查看原始来源”

Never use “立即买入”, “稳赚”, “强烈推荐”, or certainty language. Candidate states are “候选”, “观察”, “暂不满足”, or “数据不足”. Every candidate displays applicable strategy only and is not compared across strategies by a universal score.

## 10. Interaction and motion

- Transitions: 120–180 ms, ease-out.
- Hover: border or background change only; no lift or shadow.
- Loading: skeleton rows aligned to final table geometry.
- Empty state: explain missing data and next scheduled retry.
- Error/stale state: preserve the previous valid report and prominently show “数据未更新”; never render a partial candidate ranking.
- Keyboard focus: 2 px `info-blue` outline with 2 px offset.

## 11. Accessibility and responsive behavior

- Minimum body contrast meets WCAG AA.
- Minimum interactive target height: 32 px for dense desktop controls, preferably 36 px.
- All semantic colors have text or icon labels.
- At 1280 px, keep the left rail and collapse secondary metadata before reducing table readability.
- Below 1024 px, provide a read-only stacked layout; mobile trading interactions are out of scope.

## 12. Prototype sample-data policy

The prototype may use realistic Chinese stock codes, metrics, and dates to demonstrate density, but must display “原型示例数据” near the top-level date/status and in any detailed drawer. It must not imply that the figures are current or actionable.

## 13. Pixel-review corrections (supersedes conflicting layout guidance)

These requirements are mandatory for the revised daily overview.

1. Responsive shell at 1100 px: at viewport widths below 1180 px, the left rail is exactly 64 px wide and the main workspace margin-left is exactly 64 px. Center navigation icons, hide labels and rail metadata, keep the header/search fluid, and wrap utilities when necessary. The rendered document width must never exceed the viewport width. At 1180 px and above, retain the 208 px rail.
2. Typography floor: Chinese strategy descriptions and candidate evidence use at least 13 px type with at least 19 px line height. Only ranking labels, compact metadata, timestamps, and provenance IDs may use 12 px. Use local system CJK fonts first: `PingFang SC`, `Microsoft YaHei`, `Noto Sans CJK SC`, sans-serif. Do not load external font services.
3. First-viewport density: at 1440 x 900, show at least one complete official-event row, including its action, inside the first viewport. Gain 50-80 px by tightening KPI, strategy-header, and candidate-row vertical spacing while preserving the 13 px body floor and 32 px interactive targets.
4. Content-sized event region: the official-event panel and adjacent right-hand stack align to their own content start and must not stretch to equal heights. Avoid a large empty tail below the final event. Keep observation/checkpoint content below as an independent full-width region where needed.
5. Chinese-only audit footer: replace `(Internal Rules Only)` with `仅内部规则校验`; no residual English appears in this footer phrase.

Acceptance checks: no horizontal overflow at 1440, 1280, or 1100 px; all key action targets remain 32 px high; the three strategy pools stay equal-height and independent; the official fact/system inference distinction, traceability, risk scale, prototype-data label, flat square geometry, warm-paper palette, and market-color semantics remain unchanged.

## 2026-07-22 data-refresh state

- Header badge = 官方数据快照
- Report state = 市场概况可用 · 策略候选池未生成
- Coverage = 交易所概况 2/2 · 策略池 0/3
- Each strategy module = 本交易日候选池未生成 + 查看缺失数据
- Risk module = 风险评分未计算
- Forbidden legacy copy = 2024-05-24, 原型示例数据, 99.4%, candidate names, 0-100 risk bars
