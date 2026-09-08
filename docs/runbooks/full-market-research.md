# 全市场研究漏斗运行手册

本流程是当前生产研究路径。历史 30 只试点报告只作为审计存档，不参与全市场候选池的
范围、覆盖率或发布判断。

## 数据边界

- 市场范围仅包含沪深主板、创业板、科创板 A 股。
- 行情来自 Tushare `daily`，一次请求获取一个交易日的全市场数据。
- 近 12 个月股息率使用交易所已实施现金分红合计除以报告日收盘价；深交所采集窗口
  同时保留五年实施记录，用于连续分红证据核验。
- 深度证据共 12 项：三期年报、两期一季报、五年分红、风险筛查和公司行动筛查。
- 缺失证据保持缺失，不按零值参与评分。

## 运行顺序

```powershell
.venv\Scripts\python.exe -m hengce.cli ingest-market --trade-date YYYY-MM-DD --data-dir data
.venv\Scripts\python.exe -m hengce.cli ingest-exchange-dividends --market-date YYYY-MM-DD --data-dir data
.venv\Scripts\python.exe -m hengce.cli build-full-market-research --market-date YYYY-MM-DD --data-dir data
```

默认漏斗规则：上市满 365 天、且上市日期足以覆盖最早所需分红年度、当日成交额不低于 50,000（Tushare `amount` 原始单位），
股息率达到 3% 的股票优先，其余按成交额补充，总数最多 300 只。

本地快照写入：

```text
data/normalized/full_market_research/market_date=YYYY-MM-DD/
```

该目录属于本地私有数据，必须保持 Git 忽略。

## 策略发布门槛

- 三个策略对当日动态漏斗独立计算、独立排序、各自最多发布 Top 30；不足时显示实际数量，
  不用试点股票补齐。
- 不再使用固定 24/30、360 项任务或 80% 覆盖率门槛。只要至少一只股票的 12 项深度证据
  和该策略关键因子完整，该策略即可发布真实候选；覆盖率继续展示为数据质量指标。
- 证据不完整的股票保留在研究漏斗并逐项显示缺口，不进入评分集合。
- 一级行业有效样本不少于 20 时使用行业内分位；否则回退全市场并留痕。
- 稳定高股息正式发布列表（包括 `CANDIDATE` 和 `WATCH`）必须满足评分股息率不低于 5%；
  不足 30 只时保留实际数量，不降低门槛。
- 不生成买入、仓位、目标价或确定性收益指令。

读取接口：

```text
GET /api/market/research
GET /api/market/research?market_date=YYYY-MM-DD
```

前端“全市场行情与分红”页面展示市场范围、低成本过滤、深度证据计划、可评分数量、
三策略状态和逐股证据缺口。

## 2026-08-21 冻结批次证据补齐

任务规划是幂等的；三个批次可以同时冻结成员，但采集命令带有强制顺序门禁，第一批未
达到全部 `SATISFIED` 时不能执行第二批，第二批未完成时不能执行第三批。

```powershell
.venv\Scripts\python.exe -m hengce.cli plan-full-market-evidence --market-date 2026-08-21 --data-dir data
.venv\Scripts\python.exe -m hengce.cli run-full-market-evidence --run-id RUN_ID --stage discover --max-items 10 --data-dir data
.venv\Scripts\python.exe -m hengce.cli run-full-market-evidence --run-id RUN_ID --stage download --max-items 10 --data-dir data
.venv\Scripts\python.exe -m hengce.cli run-full-market-evidence --run-id RUN_ID --stage parse --max-items 10 --data-dir data
.venv\Scripts\python.exe -m hengce.cli run-full-market-evidence --run-id RUN_ID --stage prefill-risk --max-items 10 --data-dir data
```

- `discover` 的数量单位是证券，其余阶段的数量单位是任务。
- 巨潮发现与 PDF 下载都经过来源策略守卫，当前上限为每分钟 3 次。
- 失败任务保留尝试次数和错误码；用 `--stage retry` 依据已保留的来源和原始哈希回到正确
  断点，不重新下载已有对象。
- PDF 解析失败仍会留下 `PARSED` 状态迁移、解析器版本、问题集合和页码，随后进入
  `BLOCKED`，不会进入评分。
- 风险机器预填只使用已下载的最新年报；无法由来源确定的调查、停牌等字段保持空值，
  一律进入 `AWAITING_REVIEW`。

本地审核接口只接受回环客户端、同源 JSON 请求，并使用任务版本号进行乐观锁定：

```text
GET  /api/market/evidence-status
GET  /api/market/evidence-reviews/{task_id}
POST /api/market/evidence-reviews/{task_id}/decision
```

确认风险证据时，审核决定、任务迁移与 `official_risk_screen_versions` 有效记录在同一个
SQLite 事务内写入。退回只进入 `BLOCKED`，不会生成有效风险记录。

## 定向修复与 PDF 解析

使用股票代码和错误码限制重跑范围，避免反复处理未涉及本次修复的证据：

```powershell
.venv\Scripts\python.exe -m hengce.cli run-full-market-evidence --run-id RUN_ID --stage reparse-blocked --ts-code 600332.SH --error-code PDF_LAYOUT_UNSUPPORTED --max-items 1 --data-dir data
```

- v6 解析器首先按 PDF 明确声明的 Adobe 字体集合恢复缺失的 Unicode 映射，仅在内存中操作。
  保留已有映射；未知字体不猜测。映射涵盖页面和嵌套表单中实际使用的字符。
  仅在发现可修复字体时扫描内容流，不对已有完整映射的报告执行额外字符扫描。
- 图片型财务报表使用本地 OCR，识别文本按内容哈希和物理页码缓存到
  `data/normalized/ocr/`，重复解析复用缓存，不重复下载原始 PDF。
- OCR 必须完成选定的全部扫描页后统一校验，不能因前缀页面已经提齐字段就提前通过。
- OCR v2 使用红色通道减弱印章干扰，保留原图与 v1 缓存；线程数固定为 1，避免多模型
  争抢本地计算资源。可疑的前导零分组金额不作为有效事实接受。
- 识别带报告期前缀的合并表标题及“公司报表”边界，避免混合合并口径与母公司口径。
  OCR 交错的收入表头必须同时匹配当期与比较期，不用收入子项替代营业总收入。
- 原始 PDF 仍在 `data/raw/objects/<content_hash>/payload.bin` 唯一保存；OCR 与字体修复
  不替代来源、不修改原始文件，不跳过主体、报告期、关键事实和财务方程校验。
- 错误从图片型转为字段缺失或方程冲突只代表定位深入，不代表证据完成；只有正式流水线
  解析校验通过并写入 `SATISFIED` 才计入完成数。
