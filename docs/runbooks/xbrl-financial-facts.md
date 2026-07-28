# 本地 XBRL 财务事实操作手册

## 适用范围

本功能只用于个人、非商业的本地研究，不构成投资建议。当前实现接受操作员已经审核并放到本机的 XBRL 实例和 taxonomy 文件；不发现、下载或更新任何远程文件。允许的来源身份仅为 `sse` 和 `szse`，来源 URL 必须通过相应 `SourcePolicy` 的协议和域名检查。

解析器以离线模式运行，网络访问被禁用。taxonomy 必须先登记，之后才能导入引用它的申报。命令不读取 Tushare Token，也不创建 HTTP 客户端。

## 初始化

在仓库根目录使用项目虚拟环境：

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\hengce.exe init-state --data-dir data
.venv\Scripts\hengce.exe register-xbrl-taxonomy --help
.venv\Scripts\hengce.exe import-financial-xbrl --help
```

`init-state` 会迁移 `data/state/hengce.sqlite3`，并种入项目内置的来源政策。自定义政策只能来自已经审核的本地政策文件；不要为了让输入通过而放宽域名、用途或来源身份。

## 输入检查

登记 taxonomy 和导入申报前，逐项确认：

- `--file` 指向已审核的本地常规文件，不是链接、重解析点或网络路径。
- `--source-id` 只能是 `sse` 或 `szse`；`--source-url` 是该文件真实、已审核的官方 HTTPS 来源 URL。
- `--collected-at` 和申报的 `--published-at` 都使用带 UTC 偏移的 ISO 8601 时间，例如 `2026-04-30T09:00:00+08:00`；无时区时间会被拒绝。
- `--report-period` 严格使用 `YYYY-MM-DD`。
- `sse` 必须搭配 `SSE` 和 `.SH` Tushare 后缀；`szse` 必须搭配 `SZSE` 和 `.SZ` 后缀。
- `--report-type` 只能是 `ANNUAL`、`Q1`、`HALF_YEAR` 或 `Q3`。
- XML/XBRL 实例的扩展名为 `.xml` 或 `.xbrl`，MIME 为 `application/xml`、`text/xml` 或 `application/xbrl+xml`。
- XSD taxonomy 可使用 `.xsd` 和 `application/xml-schema`；ZIP 使用 `.zip` 和 `application/zip`。
- 非 ZIP 文件不能指定实例 entrypoint；直接 XSD 的 taxonomy entrypoint 必须等于文件名。
- ZIP 必须显式给出 archive 内的相对 entrypoint。entrypoint 不能是绝对路径，不能含 `..`、反斜杠、盘符或 NUL。
- XML 中的 DTD/ENTITY、ZIP 路径穿越、链接成员、危险 Windows 路径、超限文件数/大小/压缩比都会被拒绝。

## 登记 taxonomy

先用帮助命令确认当前安装的参数：

```powershell
.venv\Scripts\hengce.exe register-xbrl-taxonomy --help
```

下面只展示不可误认为真实申报的占位符。替换每个 `<...>` 后再运行；不要把真实附件、URL、数据库或生成的数据提交到 Git。

```powershell
.venv\Scripts\hengce.exe register-xbrl-taxonomy `
  --file '<APPROVED_LOCAL_TAXONOMY_FILE>' `
  --taxonomy-id '<LOCAL_APPROVED_TAXONOMY_ID>' `
  --source-id '<sse_OR_szse>' `
  --source-url 'https://<APPROVED_EXCHANGE_DOMAIN>/<APPROVED_OFFICIAL_PATH>' `
  --entrypoint '<SAFE_RELATIVE_ENTRYPOINT_OR_DIRECT_FILENAME>' `
  --content-type '<APPROVED_MIME>' `
  --collected-at '<OFFSET_AWARE_COLLECTION_TIME>' `
  --data-dir data
```

成功时输出一行排序 JSON，其中包含 taxonomy ID 和原始对象哈希。相同 taxonomy ID 只能对应完全相同的登记内容；冲突登记会被拒绝。

## 导入申报

先用帮助命令确认当前安装的参数：

```powershell
.venv\Scripts\hengce.exe import-financial-xbrl --help
```

占位符示例：

```powershell
.venv\Scripts\hengce.exe import-financial-xbrl `
  --file '<APPROVED_LOCAL_XBRL_FILE>' `
  --source-id '<sse_OR_szse>' `
  --source-url 'https://<APPROVED_EXCHANGE_DOMAIN>/<APPROVED_OFFICIAL_PATH>' `
  --ts-code '<FICTIONAL_OR_APPROVED_6_DIGITS.SH_OR_SZ>' `
  --exchange '<SSE_OR_SZSE>' `
  --report-period '<YYYY-MM-DD>' `
  --report-type '<ANNUAL_OR_Q1_OR_HALF_YEAR_OR_Q3>' `
  --published-at '<OFFSET_AWARE_PUBLICATION_TIME>' `
  --collected-at '<OFFSET_AWARE_COLLECTION_TIME>' `
  --content-type '<APPROVED_MIME>' `
  --taxonomy-id '<REGISTERED_TAXONOMY_ID>' `
  --data-dir data
```

ZIP 实例还要追加：

```powershell
  --instance-entrypoint '<SAFE_RELATIVE_XBRL_ENTRYPOINT>'
```

命令只导入这个本地文件，不会根据 `--source-url` 发起请求。重复导入同一描述符会复用已终结结果，不增加申报版本或事实。

## 状态与处理方式

| 情况 | 结果 | 操作含义 |
| --- | --- | --- |
| taxonomy 未登记 | `BLOCKED / FINANCIAL_TAXONOMY_MISSING` | 不调用解析器，不生成财务事实；先登记完整 taxonomy 后重新导入。 |
| parser overlay 路径冲突 | `FAILED / FINANCIAL_OVERLAY_CONFLICT` | 实例与 taxonomy 在解析空间发生精确、大小写或前缀冲突；修正本地包布局，不要覆盖文件。 |
| XML、MIME、entrypoint 或 ZIP 不安全 | 命令拒绝或稳定 `FINANCIAL_*` 错误 | 输入不会被当作可查询申报发布；检查本地文件和声明，不要绕过校验。 |
| 数值事实冲突 | `PARTIAL / FINANCIAL_FACT_CONFLICT` | 冲突事实保留审计记录，但不会进入标准指标查询。 |
| QName 未映射 | `PARTIAL / FINANCIAL_FACT_UNMAPPED` | 原始 QName 和值保留用于审计，但不能作为标准指标查询结果。生产环境当前没有 QName mapping。 |
| 更正已公开但不可用 | 查询返回 `FINANCIAL_RESTATEMENT_UNUSABLE` | 不回退到已经被公开更正替代的旧值；先修复或人工审查更正链。 |
| 工件写入后进程中断 | SQLite 中申报保持 `PENDING`，运行保持 `RUNNING` | 用完全相同的描述符重试。系统校验既有 Parquet 后完成发布，不重复生成可见版本。 |

查询只读取 SQLite 中已经 `PUBLISHED` 的显式 manifest 路径。仅把 Parquet 放进 warehouse 目录不会使它可见。

## 本地存储

- 原始内容：`data/raw/objects/<sha256>/payload.bin`
- 采集来源与时间：`data/raw/provenance/<event-sha256>.json`
- SQLite 状态、taxonomy 登记、申报 manifest 和冲突：`data/state/hengce.sqlite3`
- 财务事实 Parquet：`data/warehouse/financial_facts/report_year=<YYYY>/report_type=<TYPE>/exchange=<SSE_OR_SZSE>/`

warehouse 只有一层财务事实数据集根：`data/warehouse/financial_facts`。不要配置成 `financial_facts/financial_facts`。原始对象、SQLite 和已发布 Parquet 都是本地运行数据，不得提交到 Git。

## 验证

聚焦验收：

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests/integration/test_m2_xbrl_acceptance.py -q
.venv\Scripts\ruff.exe check src tests
.venv\Scripts\ruff.exe format --check tests/integration/test_m2_xbrl_acceptance.py
```

完整回归：

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest
.venv\Scripts\ruff.exe check src tests
git diff --check
git status --short
```

验收夹具只能使用 `tests/fixtures/xbrl/minimal` 下标明 `FIXTURE DATA - NOT A REAL ISSUER` 的虚构数据。`data/` 下不得出现 `.xbrl`、`.xml`、`.xsd`、`.zip` 或 `.parquet` 财务夹具。

## 当前不支持

当前没有实时申报发现或下载、PDF 回退解析、生产 QName 映射、公司行动、复权因子、总回报、派生财务比率、策略计算、推荐、目标价、仓位、交易指令或自动交易。M2a XBRL 财务事实内核完成不代表整个 M2 里程碑完成。
