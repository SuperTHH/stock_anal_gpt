# Real-Data Minimal Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax (`- [ ]`).

**Goal:** 在不引入未授权数据源、不把真实附件或密钥提交到 Git 的前提下，以
2026-07-22 21:30（Asia/Shanghai）为严格报告截止点，从现有全市场行情与官方证券
主数据中固定抽取 30 只 A 股，建立交易所 XBRL 优先、巨潮 PDF 兜底的财务事实链，
计算三个独立策略池，并把真实历史重建报告接入本地只读 API 与五页 UI。

**Architecture:** 在现有 Raw Store、SQLite 状态库、Parquet 仓库、Arelle 适配器、
策略引擎和原子报告发布器之上增加一个“清单驱动的历史重建层”。该层先冻结
`PilotUniverseSnapshot`，再生成不可变 `AcquisitionManifestItem`；自动获取仅解析
公开页面中可见的附件链接，任何限制立即转人工收件箱。事实查询始终同时使用
`report_cutoff_at` 与实际 `known_at`，更正版本不覆盖历史。三个策略分别计算
`PoolReadiness`，达到 24/30 才发布该池排名；API/UI 只读已发布报告工件。

**Tech Stack:** Python 3.12、Pydantic 2、Typer、httpx、SQLite、DuckDB/Parquet、
Arelle、pypdf、FastAPI、React 18、TypeScript、Vitest、Pytest、Ruff。

**Approved specification:** `docs/superpowers/specs/2026-07-30-real-data-minimal-loop-design.md`

## Global constraints

- 报告日期固定为 `2026-07-22`，报告截止时间固定为
  `2026-07-22T21:30:00+08:00`；实际采集和生成时间写入 `known_at` 与
  `generation_started_at`，不得伪装成截止日当时已经采集。
- 仅使用 `tushare`、`sse`、`szse`、`cninfo`、`stats`、`csrc` 中已启用且通过
  `PolicyGuard` 的用途。不得调用隐藏接口、猜测参数、绕过验证码、登录墙或限流。
- CI 使用明确标注的虚构夹具，不访问真实网站，不读取 Tushare token，不携带真实
  公告、真实候选或真实数据库。
- `.env`、`data/`、SQLite、Parquet、Raw Store、下载附件、人工收件箱和真实报告
  始终留在 Git 忽略范围内；任何日志和命令输出不得包含 token。
- 所有时间必须带时区；所有金额使用 `Decimal`；身份、内容和发布工件使用稳定哈希。
- XBRL 可用且质量有效时，PDF 不得替代 XBRL。PDF 只有通过身份、期间、单位、勾稽、
  完整度和版本检查后才可变为 `VALID`。
- 三个策略不合并总分，不跨池比较。池未达到 80% 覆盖时发布阻断状态和待办，不发布
  空排名伪装成功。候选本身必须具有 100% 关键因子及来源谱系。
- 每个任务严格执行 RED → GREEN → REFACTOR；看到预期失败前不得写该任务实现。
- 每个任务结束运行目标测试、`ruff check` 和 `git diff --check`，然后创建一个小提交。

---

## Task 1: 冻结试点、采集与池就绪度契约

**Files**

- Modify: `src/hengce/contracts/enums.py`
- Add: `src/hengce/contracts/pilot.py`
- Modify: `src/hengce/contracts/strategy.py`
- Add: `tests/unit/contracts/test_pilot_models.py`
- Modify: `tests/unit/contracts/test_strategy_models.py`

**Required interfaces**

```python
class AcquisitionStatus(StrEnum):
    PLANNED = "PLANNED"
    DISCOVERED = "DISCOVERED"
    DOWNLOADED = "DOWNLOADED"
    VERIFIED = "VERIFIED"
    INGESTED = "INGESTED"
    AWAITING_MANUAL = "AWAITING_MANUAL"
    REJECTED = "REJECTED"

class DocumentKind(StrEnum):
    PERIODIC_REPORT = "PERIODIC_REPORT"
    DIVIDEND_RECORD = "DIVIDEND_RECORD"
    CAPITAL_ACTION_TIMELINE = "CAPITAL_ACTION_TIMELINE"
    RISK_SCREEN = "RISK_SCREEN"

class PoolReadinessStatus(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"

class PilotUniverseMember(BaseModel):
    ts_code: str
    security_name: str
    board: Literal["MAIN_SH", "MAIN_SZ", "CHINEXT", "STAR"]
    amount: Decimal
    rank_in_board: int
    evidence_record_ids: tuple[str, ...]

class PilotUniverseSnapshot(BaseModel):
    universe_id: str
    market_date: date
    report_cutoff_at: datetime
    algorithm_version: str
    quotas: dict[str, int]
    members: tuple[PilotUniverseMember, ...]
    input_hashes: dict[str, str]
    manifest_hash: str
    created_at: datetime

class AcquisitionManifestItem(BaseModel):
    item_id: str
    universe_id: str
    ts_code: str
    document_kind: DocumentKind
    report_type: ReportType | None
    report_period: date | None
    source_id: str
    report_cutoff_at: datetime
    status: AcquisitionStatus
    source_url: AnyHttpUrl | None
    discovery_method: DiscoveryMethod | None
    published_at: datetime | None
    effective_at: datetime | None
    collected_at: datetime | None
    content_hash: str | None
    version: str | None
    supersedes_id: str | None
    raw_object_hash: str | None
    quality_status: QualityStatus
    error_code: str | None
    attempt_count: int

class PoolReadiness(BaseModel):
    strategy_type: StrategyType
    universe_size: int
    eligible_count: int
    complete_factor_count: int
    coverage_ratio: Decimal
    required_coverage_ratio: Decimal
    status: PoolReadinessStatus
    missing_by_security: dict[str, tuple[str, ...]]
    blocking_codes: tuple[str, ...]
    strategy_version: str
    factor_version: str
```

`ReportSnapshot` 增加：

```python
universe_id: str
is_historical_reconstruction: bool
report_cutoff_at: datetime
known_at: datetime
generation_started_at: datetime
pool_readiness: dict[StrategyType, PoolReadiness]
manual_todo_count: int
```

**TDD steps**

- [x] 写 `test_pilot_models.py`：验证恰好四个板块、成员代码唯一、配额合计等于成员数、
      `manifest_hash` 为 64 位小写 SHA-256、所有时间带时区。
- [x] 写采集项失败测试：定期报告必须带 `report_type/report_period`，非定期报告不得
      冒充财报；`DOWNLOADED` 之后必须有 URL、采集时间、内容哈希和 Raw Store 哈希。
- [x] 写状态和质量组合测试：`INGESTED` 只能搭配 `VALID/DERIVED`；
      `AWAITING_MANUAL` 必须带机器可读 `error_code`；尝试次数不得为负。
- [x] 写池就绪度测试：`coverage_ratio == complete_factor_count / universe_size`；
      30 只样本只有 `complete_factor_count >= 24` 时可以是 `READY`。
- [x] 运行
      `python -m pytest tests/unit/contracts/test_pilot_models.py tests/unit/contracts/test_strategy_models.py -q`
      并确认因模型/枚举缺失而失败。
- [x] 实现上述最小枚举和 Pydantic 契约；所有跨字段约束使用
      `model_validator(mode="after")`，不在服务层重复。
- [x] 再运行同一测试并确认通过，然后运行
      `python -m ruff check src/hengce/contracts tests/unit/contracts` 与
      `git diff --check`。
- [x] 提交：`git commit -am "feat: define real-data pilot contracts"`；新增文件先
      `git add`。

---

## Task 2: 增加可恢复的试点状态库与仓库

**Files**

- Add: `src/hengce/state/migrations/008_real_data_pilot.sql`
- Add: `src/hengce/state/pilot_repository.py`
- Modify: `src/hengce/state/repository.py`
- Add: `tests/unit/state/test_pilot_repository.py`
- Modify: `tests/unit/state/test_repository.py`

**Schema**

`008_real_data_pilot.sql` 必须创建：

- `pilot_universes(universe_id PK, market_date, report_cutoff_at, algorithm_version,
  payload_json, manifest_hash UNIQUE, created_at)`
- `acquisition_manifest_items(item_id PK, universe_id FK, ts_code, document_kind,
  report_type, report_period, source_id, status, payload_json, updated_at,
  UNIQUE(universe_id, ts_code, document_kind, report_type, report_period))`
- `acquisition_transitions(transition_id PK, item_id FK, from_status, to_status,
  error_code, observed_at, payload_hash)`
- `pipeline_checkpoints(run_key, stage_name, input_hash, status, payload_json,
  updated_at, PRIMARY KEY(run_key, stage_name))`

**Required interfaces**

```python
class PilotRepository:
    def publish_universe(self, snapshot: PilotUniverseSnapshot) -> PilotUniverseSnapshot: ...
    def get_universe(self, universe_id: str) -> PilotUniverseSnapshot | None: ...
    def get_universe_for_date(self, market_date: date) -> PilotUniverseSnapshot | None: ...
    def insert_manifest(self, items: Sequence[AcquisitionManifestItem]) -> int: ...
    def list_manifest(
        self,
        universe_id: str,
        statuses: frozenset[AcquisitionStatus] | None = None,
    ) -> tuple[AcquisitionManifestItem, ...]: ...
    def transition(
        self,
        item_id: str,
        expected_from: AcquisitionStatus,
        updated: AcquisitionManifestItem,
        observed_at: datetime,
    ) -> AcquisitionManifestItem: ...
    def save_checkpoint(self, checkpoint: PipelineCheckpoint) -> None: ...
    def get_checkpoint(self, run_key: str, stage_name: str) -> PipelineCheckpoint | None: ...
```

允许的状态转换只包括：

```text
PLANNED -> DISCOVERED | AWAITING_MANUAL | REJECTED
DISCOVERED -> DOWNLOADED | AWAITING_MANUAL | REJECTED
DOWNLOADED -> VERIFIED | AWAITING_MANUAL | REJECTED
VERIFIED -> INGESTED | AWAITING_MANUAL | REJECTED
AWAITING_MANUAL -> DISCOVERED | DOWNLOADED | REJECTED
```

`INGESTED` 和 `REJECTED` 为终态；相同身份及相同 payload 的重复操作必须幂等。

**TDD steps**

- [x] 写迁移测试：从只含 001–007 的临时库升级后，原表数量与内容不变，新表、索引和
      外键存在；重复 `migrate()` 不增加迁移或业务记录。
- [x] 写仓库失败测试：冻结样本的同一日期不能被不同哈希覆盖；相同哈希重复发布返回
      原记录。
- [x] 写清单测试：同一身份不可重复，批量插入使用单事务；非法跳转、并发
      `expected_from` 不一致和终态变更必须失败。
- [x] 写检查点测试：相同 `input_hash` 可续跑，不同 `input_hash` 必须返回
      `PIPELINE_CHECKPOINT_INPUT_CHANGED`。
- [x] 运行
      `python -m pytest tests/unit/state/test_pilot_repository.py tests/unit/state/test_repository.py -q`
      并确认 RED。
- [x] 实现 SQL 和 `PilotRepository`；写操作使用 `BEGIN IMMEDIATE`，时间以 ISO-8601
      保存，payload 以 Pydantic JSON 往返。
- [x] 实现 `StateRepository.backup_to(target: Path)`，使用 SQLite backup API，不复制
      活跃 WAL 文件；目标已存在时拒绝覆盖。
- [x] 运行目标测试、`python -m ruff check src/hengce/state tests/unit/state` 和
      `git diff --check`。
- [x] 提交：`git commit -m "feat: persist pilot universe and acquisition state"`。

---

## Task 3: TDD 实现 8/8/7/7 确定性流动性抽样

**Files**

- Add: `src/hengce/services/pilot_universe.py`
- Add: `tests/unit/services/test_pilot_universe.py`
- Add: `tests/fixtures/pilot/security_master.json`
- Add: `tests/fixtures/pilot/market_bars.json`
- Modify: `tests/integration/test_m1_acceptance.py`

**Required interface**

```python
PILOT_QUOTAS = {"MAIN_SH": 8, "MAIN_SZ": 8, "CHINEXT": 7, "STAR": 7}

class PilotUniverseSelector:
    def select(
        self,
        *,
        market_date: date,
        report_cutoff_at: datetime,
        bars: Sequence[Mapping[str, object]],
        securities: Sequence[SecurityMaster],
        market_content_hash: str,
        master_universe_hash: str,
        created_at: datetime,
    ) -> PilotUniverseSnapshot: ...
```

筛选顺序固定为：

1. `SecurityMaster.is_in_scope` 且板块属于四个目标板块；
2. 上市满 365 天且截止日尚未退市；
3. 截止日存在唯一行情行，`amount > 0`，价格、成交量、成交额为有限非负值；
4. 名称不含 `ST`、`*ST`、`退`，身份代码和交易所后缀一致；
5. 板块内按 `amount DESC, ts_code ASC` 排序；
6. 取 8/8/7/7；任何板块不足即整体失败，不跨板块补位。

算法版本固定为 `board-liquidity-pilot-v1`。`universe_id` 和 `manifest_hash` 由规范化
输入与有序成员计算，不能包含 `created_at`，从而允许之后复现同一快照。

**TDD steps**

- [x] 创建全部为虚构证券名和虚构代码组合的 40 行夹具；写测试验证配额、顺序、
      平局按代码、成员总数和哈希稳定。
- [x] 写每个硬过滤测试：范围外、上市不足一年、ST、退市、停牌/缺行、零成交额、
      重复行情、非有限数字、错误交易所后缀。
- [x] 写板块只有 7/8 或 6/7 时返回 `PILOT_BOARD_QUOTA_UNMET:<board>` 的测试，并
      证明不会从其他板块补位。
- [x] 写同一输入不同顺序和不同 `created_at` 得到相同 `universe_id` 的测试；改变一条
      成交额必须改变哈希。
- [x] 运行 `python -m pytest tests/unit/services/test_pilot_universe.py -q` 并确认 RED。
- [x] 实现纯函数式选择器，不访问网络或数据库。
- [x] 在 M1 集成测试中接上 `MarketWarehouse.read_bars()` 与
      `StateRepository.get_security_master_universe()`，验证仓库记录可直接作为输入。
- [x] 运行目标测试、Ruff 和 `git diff --check`。
- [x] 提交：`git commit -m "feat: select deterministic pilot universe"`。

---

## Task 4: 生成 360 项不可变官方采集清单

**Files**

- Add: `src/hengce/acquisition/__init__.py`
- Add: `src/hengce/acquisition/planner.py`
- Add: `tests/unit/acquisition/test_planner.py`

**Manifest composition**

每只股票生成：

- 5 个 `PERIODIC_REPORT`：2023、2024、2025 年报，2025Q1、2026Q1；
- 5 个 `DIVIDEND_RECORD`：2021–2025 每个年度一个；
- 1 个 `CAPITAL_ACTION_TIMELINE`：覆盖截至截止点的总股本及送转、配股、回购注销；
- 1 个 `RISK_SCREEN`：审计意见、重大立案和退市风险的官方核验。

30 只共 360 项，其中定期报告必须恰好 150 项。上海证券映射 `source_id=sse`，
深圳/创业板映射 `source_id=szse`；PDF 兜底来源在 XBRL 发现失败之后才改为
`cninfo`，规划阶段不得预先把 PDF 当主来源。

**Required interface**

```python
class AcquisitionPlanner:
    def build(
        self,
        universe: PilotUniverseSnapshot,
        created_at: datetime,
    ) -> tuple[AcquisitionManifestItem, ...]: ...
```

`item_id` 由 `universe_id + ts_code + document_kind + report_type + report_period` 的
规范化 JSON 计算。所有项初始为 `PLANNED/MISSING`，`source_url` 和发布时间为空，
`attempt_count=0`。

**TDD steps**

- [x] 写数量测试：总数 360、财报 150、分红 150、资本行动 30、风险核验 30。
- [x] 写期间测试：年报期末为 12-31，Q1 为 03-31，且不存在 2025 半年报或 Q3。
- [x] 写来源测试：沪市仅规划 SSE，深市/创业板仅规划 SZSE，规划器从不规划商业媒体。
- [x] 写稳定身份测试：成员输入顺序不改变项目 ID；项目按
      `ts_code, document_kind, report_period` 排序。
- [x] 运行 `python -m pytest tests/unit/acquisition/test_planner.py -q` 并确认 RED。
- [x] 实现规划器并复用 Task 1 契约；不添加任何联网逻辑。
- [x] 运行目标测试、Ruff 和 `git diff --check`。
- [x] 提交：`git commit -m "feat: plan official pilot acquisitions"`。

---

## Task 5: 实现公开链接发现、低频下载与人工收件箱

**Files**

- Add: `src/hengce/acquisition/discovery.py`
- Add: `src/hengce/acquisition/downloader.py`
- Add: `src/hengce/acquisition/manual_inbox.py`
- Modify: `src/hengce/contracts/enums.py`
- Modify: `src/hengce/data/source_policies.json`
- Add: `tests/fixtures/acquisition/sse_public_listing.html`
- Add: `tests/fixtures/acquisition/szse_public_listing.html`
- Add: `tests/unit/acquisition/test_discovery.py`
- Add: `tests/unit/acquisition/test_downloader.py`
- Add: `tests/unit/acquisition/test_manual_inbox.py`
- Modify: `tests/unit/policy/test_guard.py`

**Required interfaces**

```python
@dataclass(frozen=True)
class PublicAttachment:
    source_id: str
    page_url: str
    attachment_url: str
    attachment_name: str
    content_type: str
    published_at: datetime
    report_period: date
    report_type: ReportType

class PublicListingResolver(Protocol):
    def resolve(self, item: AcquisitionManifestItem) -> PublicAttachment | None: ...

class ApprovedAttachmentDownloader:
    def fetch(
        self,
        item: AcquisitionManifestItem,
        attachment: PublicAttachment,
    ) -> DownloadResult: ...

class ManualInbox:
    def scan(self, root: Path, manifest: Sequence[AcquisitionManifestItem]) -> InboxResult: ...
```

发现器只解析测试所覆盖的公开 HTML 中可见 `<a href>`，不构造或调用 JSON/XHR 隐藏
接口。生产运行如果公开页面结构不受支持，直接返回 `None` 并把项目转为
`AWAITING_MANUAL:PUBLIC_LINK_NOT_DISCOVERABLE`。清单可允许
`DiscoveryMethod.PUBLIC_PAGE`，人工导入继续使用 `MANUAL_IMPORT`。

下载器规则：

- 每次请求前调用 `PolicyGuard.validate(source_id, url, purpose, operation)`；
- 使用状态库持久化限速；单线程；连接/读取超时分别为 10/30 秒；
- 只允许 1 次初始请求和 1 次网络瞬断重试，不重试 401/403/429；
- 401/403/429、验证码/登录页、HTML 冒充附件立即停止该来源本轮自动访问，并返回
  `AWAITING_MANUAL`；
- PDF 必须以 `%PDF-` 开头；XML/ZIP 必须通过现有安全包校验；最大 100 MiB；
- 先完整验证，再写不可变 Raw Store；保存最终 URL、MIME、长度、ETag/Last-Modified
  （如有）、采集时间和 SHA-256。

人工收件箱每个附件必须有同名 `.json` 侧车，字段固定为：
`item_id, source_url, ts_code, document_kind, report_type, report_period,
published_at, downloaded_at, attachment_name, content_type`。缺字段、身份不一致、非白名单
URL、未知项目或内容重复但身份冲突必须 `REJECTED`。

**TDD steps**

- [x] 先写公开 HTML 夹具解析测试；证明解析器只接受可见链接，忽略脚本内 URL、
      `javascript:`、跨白名单域名和没有明确期间/类型的附件。
- [x] 写 fake `httpx` 测试覆盖成功下载、重定向后域名再校验、超时一次重试、
      401/403/429、验证码 HTML、错误 MIME、超限、损坏 PDF/ZIP。
- [x] 写来源熔断测试：同一运行中首个限制响应后，同源剩余项目不再发请求，均进入
      `AWAITING_MANUAL:SOURCE_ACCESS_RESTRICTED`。
- [x] 写人工收件箱测试：合法 PDF/XBRL 与自动路径走相同 Raw Store；文件名本身不被
      当作身份；真实附件内容不得进入夹具。
- [x] 写政策测试：`static.cninfo.com.cn` 仅在确认为巨潮公开附件主机且用途为
      `financial_pdf` 时允许；其他子域仍拒绝。更新条款复核日期为实施当天。
- [x] 运行
      `python -m pytest tests/unit/acquisition tests/unit/policy/test_guard.py -q`
      并确认 RED。
- [x] 实现最小解析器、下载器、来源熔断和人工收件箱；生产解析器只支持已验证公开
      页面结构，结构漂移必须失败关闭。
- [x] 运行目标测试、Ruff 和 `git diff --check`。
- [x] 提交：`git commit -m "feat: add policy-guarded document acquisition"`。

---

## Task 6: 加载版本化生产 XBRL 映射并接入清单

**Files**

- Add: `src/hengce/data/financial_fact_mappings.json`
- Add: `src/hengce/financials/registry_loader.py`
- Add: `src/hengce/financials/qname_inventory.py`
- Modify: `src/hengce/financials/mapping.py`
- Modify: `src/hengce/services/financial_ingestion.py`
- Modify: `src/hengce/cli.py`
- Add: `tests/fixtures/xbrl/pilot/instance.xml`
- Add: `tests/fixtures/xbrl/pilot/pilot-gaap.xsd`
- Add: `tests/unit/financials/test_registry_loader.py`
- Modify: `tests/unit/financials/test_mapping.py`
- Add: `tests/integration/test_pilot_xbrl_ingestion.py`

**Minimum canonical fact set**

```text
revenue
operating_cost
net_profit
adjusted_net_profit
operating_cash_flow
capital_expenditure
total_assets
current_assets
cash_and_equivalents
total_liabilities
current_liabilities
interest_bearing_debt
equity
interest_expense
total_shares
```

映射文件按 `mapping_version`、精确 `raw_qname`、报表类型、单位类型和生效报告年度记录。
实现不得仅凭中文标签或模糊字符串猜测概念。未映射 QName 继续保存为
`UNMAPPED/UNVERIFIED`，不进入策略。实体映射从样本主数据生成精确的
`(entity_scheme, entity_identifier) -> ts_code`，一对多或未知实体阻断。

**Required interfaces**

```python
class FinancialRegistryLoader:
    def load_fact_registry(
        self, path: Path, report_period: date
    ) -> FactMappingRegistry: ...

    def build_entity_registry(
        self,
        universe: PilotUniverseSnapshot,
        declarations: Sequence[EntityDeclaration],
    ) -> EntityMappingRegistry: ...

class QNameInventory:
    def inspect(
        self,
        instance_path: Path,
        taxonomy_paths: Sequence[Path],
    ) -> tuple[QNameInventoryItem, ...]: ...
```

CLI 的 `build_financial_ingestion()` 不再使用 `empty-v1`，改为显式接收映射文件和
实体声明文件。`import-financial-xbrl` 增加 `--manifest-item-id`，成功后在一个受控
步骤中把项目从 `VERIFIED` 转为 `INGESTED`；导入失败不改变源文件和既有版本。
另加本地只读命令 `inspect-financial-qnames`，只列出实际实例中的精确 QName、命名
空间、taxonomy label、单位类型、期间类型和 taxonomy 内容哈希，不自动建立规范映射。
`financial_fact_mappings.json` 的每条生产映射必须记录
`taxonomy_hash/evidence_url/reviewed_at`，并由实际 taxonomy 中存在的精确 QName
校验通过；无法确认的概念保持未映射。

**TDD steps**

- [x] 写映射文件模式测试：重复 QName、未知单位、版本空值、报告年度不适用必须失败。
- [x] 写最小 XBRL 夹具，覆盖 15 个规范事实、合并口径、货币/股数单位和实体身份。
- [x] 写未知 QName、错误实体、单位冲突、重复规范事实和更正 `supersedes_id` 测试。
- [x] 写 QName inventory 测试，证明它只做枚举和证据输出，不会按标签相似度产生
      `canonical_fact_name`。
- [x] 写集成测试：`VERIFIED` 清单项导入为不可变 filing/Parquet 后才转 `INGESTED`；
      相同内容重跑幂等；不同内容同版本冲突。
- [x] 运行
      `python -m pytest tests/unit/financials/test_registry_loader.py tests/unit/financials/test_mapping.py tests/integration/test_pilot_xbrl_ingestion.py -q`
      并确认 RED。
- [x] 实现 JSON 加载器、实体声明加载和清单联动；复用 Arelle、
      `SafePackageMaterializer`、`FinancialQualityValidator` 与现有仓库。
- [x] 运行目标测试、Ruff 和 `git diff --check`。
- [x] 提交：`git commit -m "feat: ingest mapped pilot XBRL facts"`。

---

## Task 7: 实现巨潮 PDF 失败关闭兜底

**Files**

- Modify: `src/hengce/financials/fallback.py`
- Add: `src/hengce/financials/pdf_extractor.py`
- Modify: `src/hengce/services/financial_resolution.py`
- Add: `tests/fixtures/pdf/pilot_extracted_pages.json`
- Add: `tests/unit/financials/test_pdf_extractor.py`
- Modify: `tests/unit/financials/test_fallback.py`
- Modify: `tests/integration/test_m2_financial_resolution.py`

**Required interfaces**

```python
@dataclass(frozen=True)
class PdfFactCandidate:
    canonical_fact_name: str
    value: Decimal
    unit_multiplier: Decimal
    currency: str
    page_number: int
    statement_type: StatementType
    source_text_hash: str

class CninfoPdfExtractor:
    def extract(
        self,
        *,
        pdf_path: Path,
        descriptor: FilingDescriptor,
    ) -> PdfExtractionResult: ...
```

提取器使用 `pypdf.PdfReader`，先做文本层存在性、证券代码、报告类型、报告期、单位和
币种识别，再按报表标题与字段别名提取候选值。扫描版、布局不支持、单位不明或身份
多义时返回 `PDF_LAYOUT_UNSUPPORTED`，进入人工核验，不使用 OCR 猜值。

PDF 初始质量为 `UNVERIFIED`，以下全部通过才升级为 `VALID`：

- 资产约等于负债加权益，容差为 `max(1 元, 总资产绝对值 × 0.000001)`；
- 现金流量表经营/投资/筹资和净变动的勾稽项在同一单位下可验证；
- 关键字段至少覆盖该策略需要的完整集合；
- 同一期间、口径和事实名称只有一个值；
- 每个事实保留页码、原文片段哈希、单位、解析器版本和 PDF 内容哈希。

`PreferredFinancialResolver` 必须保证：有效 XBRL 不调用 PDF provider；XBRL 缺失或
不可用才调用 PDF；对比模式发现冲突时展示 XBRL 但返回质量阻断。

**TDD steps**

- [x] 用脱敏的页面文本 JSON 写身份、期间、单位、负号、千分位和页码提取测试。
- [x] 写扫描版、无单位、多代码、多期间、资产负债不平、现金流不平、重复冲突事实
      测试；这些情况不得返回 `VALID`。
- [x] 写 resolver spy 测试，证明有效 XBRL 时 PDF provider 调用次数为 0。
- [x] 写更正 PDF 测试：新版本指向旧 filing，旧事实保留；更正公开前查询旧值，公开
      后且新版本有效时查询新值。
- [x] 运行
      `python -m pytest tests/unit/financials/test_pdf_extractor.py tests/unit/financials/test_fallback.py tests/integration/test_m2_financial_resolution.py -q`
      并确认 RED。
- [x] 实现失败关闭提取、质量校验和 PDF filing 规范化；不得加入 OCR 或模糊 AI 提取。
- [x] 运行目标测试、Ruff 和 `git diff --check`。
- [x] 提交：`git commit -m "feat: add validated CNINFO PDF fallback"`。

---

## Task 8: 持久化分红、公司行动与截止日总股本

**Files**

- Add: `src/hengce/state/migrations/009_pilot_actions.sql`
- Add: `src/hengce/state/action_repository.py`
- Add: `src/hengce/actions/share_capital.py`
- Modify: `src/hengce/actions/calculator.py`
- Add: `tests/unit/state/test_action_repository.py`
- Add: `tests/unit/actions/test_share_capital.py`
- Modify: `tests/unit/actions/test_calculator.py`
- Modify: `tests/integration/test_m2_actions_acceptance.py`

**Required interfaces**

```python
class CorporateActionRepository:
    def save_version(self, action: CorporateAction) -> CorporateAction: ...
    def visible_actions(
        self, ts_code: str, as_of: datetime, known_at: datetime
    ) -> tuple[CorporateAction, ...]: ...

class ShareCapitalResolver:
    def resolve(
        self,
        *,
        baseline_fact: FinancialFact,
        actions: Sequence[CorporateAction],
        as_of: datetime,
        known_at: datetime,
    ) -> ShareCapitalResult: ...
```

`009_pilot_actions.sql` 保存行动版本、`supersedes_id`、公告/生效/采集时间、Raw Store
哈希和质量状态。总股本从最近一个截止日前已发布且实际已采集的 `total_shares` 基准
开始，只应用截止日前已生效的送转、拆股、配股和回购注销；现金分红不改变股本。
链缺口、重复生效、冲突或负股本必须返回阻断，不估算市值。

**TDD steps**

- [x] 写仓库测试：更正不覆盖旧行动；`as_of/known_at` 前后返回正确版本；错误分支链
      返回冲突。
- [x] 写股本测试：送转、配股、回购注销、现金分红、同日多行动的确定顺序和小数精度。
- [x] 写未来泄漏测试：公告或生效在 2026-07-22 21:30 之后的行动均不进入股本。
- [x] 写总回报回归测试：原始 OHLC 不变，分红送转前后总回报连续。
- [x] 运行
      `python -m pytest tests/unit/state/test_action_repository.py tests/unit/actions tests/integration/test_m2_actions_acceptance.py -q`
      并确认 RED。
- [ ] 实现仓库和解析器，所有派生结果记录输入 fact/action ID 与算法版本。
- [x] 运行目标测试、Ruff 和 `git diff --check`。
- [x] 提交：`git commit -m "feat: resolve point-in-time share capital"`。

---

## Task 9: 装配当时可知事实并补齐策略所需派生指标

**Files**

- Add: `src/hengce/financials/assembler.py`
- Modify: `src/hengce/financials/metrics.py`
- Modify: `src/hengce/contracts/derived.py`
- Add: `tests/unit/financials/test_assembler.py`
- Modify: `tests/unit/financials/test_metrics.py`
- Add: `tests/integration/test_pilot_metrics.py`

**Required interfaces**

```python
class PointInTimeFinancialAssembler:
    def assemble(
        self,
        *,
        ts_code: str,
        periods: tuple[date, ...],
        report_cutoff_at: datetime,
        known_at: datetime,
    ) -> FinancialSeriesResult: ...

class PilotMetricCalculator:
    def calculate(
        self,
        *,
        series: FinancialSeriesResult,
        closing_price: Decimal,
        share_capital: ShareCapitalResult,
        dividends: Sequence[CorporateAction],
        report_cutoff_at: datetime,
        known_at: datetime,
    ) -> PilotMetricResult: ...
```

装配器对每个期间调用 `AsOfFinancialQuery`，优先采用有效 XBRL filing，只有 resolver
确认缺失时使用有效 PDF filing。2023–2025 年报与 2025Q1/2026Q1 必须分别保留，不把
Q1 直接年化为全年。更正公开但不可用时阻断该事实，不回退到已被替代旧值。

派生指标与版本 `pilot-financial-metrics-v1`：

- 2025 ROE = 2025 归母净利润 /
  `((2024 年末归母权益 + 2025 年末归母权益) / 2)`；
- 2025 ROIC 使用显式试点代理：
  `NOPAT = 2025 归母净利润 + 2025 利息费用 × (1 - 0.25)`，
  `投入资本 = 归母权益 + 有息负债 - 现金及现金等价物`，
  分母为 2024 与 2025 年末投入资本的平均值；页面标注
  `tax_rate_proxy=25%`，不得描述为公司实际税率；
- 年报和 Q1 的收入、扣非净利润同比；
- 经营现金流 / 归母净利润；
- 毛利率 = `(营业收入 - 营业成本) / 营业收入`，稳定性使用 2023–2025 三个年度毛利率
  的总体标准差；
- 资产负债率 = 总负债 / 总资产，有息负债率 = 有息负债 / 总资产，
  现金债务覆盖 = 现金及现金等价物 / 有息负债，均使用 2025 年末值；
- 自由现金流 = 经营现金流 - 资本开支；规范化 `capital_expenditure` 必须是正的
  现金流出绝对值；
- 市值 = 2026-07-22 收盘价 × 截止日可验证总股本；
- PE = 市值 / 2025 归母净利润，PB = 市值 / 2025 归母权益，
  FCF 收益率 = 2025 自由现金流 / 市值；
- 连续分红年限从 2025 向前统计 `IMPLEMENTED` 且每股现金分红大于零的连续年度；
- 已公告股息率 = 截止日前已公告且未取消的 2025 年度每股现金分红 /
  2026-07-22 收盘价；
- 派息率 = 2025 年度现金分红总额 / 2025 归母净利润；
- FCF 覆盖 = 2025 自由现金流 / 2025 年度现金分红总额；
- 分红削减标记在 2025 每股现金分红低于 2024 时为真；缺任一年度时为数据不足，
  不预测分红。

分母为零、负值不适用、单位冲突、事实缺失和股本阻断返回 `MISSING` 加明确原因，不
生成无穷值或误导数字。

**TDD steps**

- [x] 写装配测试覆盖五个期间、XBRL 优先、PDF 兜底、更正链和
      `published_at <= report_cutoff_at`、`collected_at <= known_at`。
- [x] 写每个公式的固定 Decimal 示例及输入记录 ID 断言；检查公式版本和质量状态。
- [x] 写 PE 负利润、零权益、零债务、缺资本开支、缺总股本、未来分红和分红取消测试。
- [x] 写集成测试：真实仓库形状的虚构 filings/actions/bars 产生完整指标集，任何
      时间线违规阻断。
- [x] 运行
      `python -m pytest tests/unit/financials/test_assembler.py tests/unit/financials/test_metrics.py tests/integration/test_pilot_metrics.py -q`
      并确认 RED。
- [x] 实现装配器和指标扩展；复用现有 `MetricValue/DerivedFinancialMetric`，不把
      派生值写回原始事实表。
- [x] 运行目标测试、Ruff 和 `git diff --check`。
- [x] 提交：`git commit -m "feat: calculate pilot financial metrics"`。

---

## Task 10: 构建三个独立策略输入、覆盖门槛与规则文案

**Files**

- Add: `src/hengce/strategies/pilot_inputs.py`
- Add: `src/hengce/strategies/readiness.py`
- Add: `src/hengce/strategies/narratives.py`
- Modify: `src/hengce/strategies/quality_growth.py`
- Modify: `src/hengce/strategies/deep_value.py`
- Modify: `src/hengce/strategies/stable_dividend.py`
- Modify: `src/hengce/strategies/engine.py`
- Add: `tests/unit/strategies/test_pilot_inputs.py`
- Add: `tests/unit/strategies/test_readiness.py`
- Add: `tests/unit/strategies/test_narratives.py`
- Modify: `tests/integration/test_m3_strategy_acceptance.py`

**Required interfaces**

```python
class PilotStrategyInputBuilder:
    def build(
        self,
        universe: PilotUniverseSnapshot,
        metrics: Mapping[str, PilotMetricResult],
        hard_filters: Mapping[str, HardFilterResult],
        report_cutoff_at: datetime,
        known_at: datetime,
    ) -> dict[StrategyType, list[SecurityStrategyInput]]: ...

class PoolReadinessEvaluator:
    def evaluate(
        self,
        definition: StrategyDefinition,
        inputs: Sequence[SecurityStrategyInput],
        universe: PilotUniverseSnapshot,
    ) -> PoolReadiness: ...

class RuleNarrativeRenderer:
    def render(
        self,
        strategy_type: StrategyType,
        candidate: StrategyCandidate,
    ) -> CandidateNarrative: ...
```

策略版本固定为：

- `quality-growth-pilot-v1`
- `deep-value-pilot-v1`
- `stable-dividend-pilot-v1`

横截面归一化范围固定为 `pilot_universe`，禁止显示行业排名。沿用 1%/99% 缩尾、
`score DESC, ts_code ASC` 平局规则和不重分配缺失权重。`PoolReadiness` 的
`complete_factor_count` 统计关键因子全部有效且谱系完整的证券；至少 24 才调用
`StrategyEngine.evaluate()`。未达标时候选列表必须为空。

规则文案只允许版本化模板，输出入选理由、风险、观察条件、失效条件和催化剂。没有
截止日前已验证官方事件时，催化剂固定为“暂无可验证官方催化剂”，不得推测。
每条文案保存 `template_id/template_version/trigger_factor_names/source_record_ids`。

**Factor composition**

| Strategy factor | Frozen pilot-v1 input |
|---|---|
| `capital_return` | ROE 与 ROIC 百分位的算术平均 |
| `growth_quality` | 年度收入增长、年度扣非利润增长、Q1 收入增长、Q1 扣非利润增长的可用项算术平均；任一关键项缺失则证券不完整 |
| `cash_flow_quality` | 经营现金流 / 归母净利润 |
| `profitability_stability` | 毛利率三年总体标准差，低者优先 |
| `balance_sheet_quality` | 资产负债率低者优先与现金债务覆盖高者优先的算术平均 |
| `valuation_attractiveness` | PE、PB 百分位反向后的算术平均；PE 不适用时证券不完整 |
| `absolute_valuation` | PE/PB 反向百分位与 FCF 收益率正向百分位的算术平均 |
| `relative_valuation` | PE、PB 相对 30 只试点样本的反向百分位平均 |
| `asset_quality` | 流动资产 / 总资产与经营现金流 / 归母净利润的百分位平均 |
| `cash_debt_quality` | 现金债务覆盖正向与资产负债率反向百分位平均 |
| `cycle_position` | 2026Q1 收入同比和扣非利润同比相对 2025 年度同比的加速度平均；四个输入齐全才可用 |
| `value_trap_safety` | 经营现金流质量、扣非利润年度增长及无重大立案/非标审计三个规则分的平均 |
| `dividend_yield` | 截止点前已公告 2025 股息率 |
| `dividend_continuity` | 2021–2025 连续正现金分红年限 / 5 |
| `payout_sustainability` | 派息率在 `(0, 1]` 内按距 50% 的绝对偏差反向评分；区间外记风险但不截断原值 |
| `cashflow_coverage` | FCF / 现金分红总额 |
| `dividend_cut_safety` | 2025 未较 2024 削减且连续分红年限不少于 3 年时为 1，否则为 0 |

**TDD steps**

- [x] 写三个策略输入映射测试，逐项断言每个 factor 来自 Task 9 指标且方向正确；
      负 PE 不作为低估值加分，未公告股息不进入稳定股息池。
- [x] 写 23/30、24/30、30/30 覆盖测试；一个池阻断不改变其他池结果；阻断池候选
      必须为空。
- [x] 写候选 100% 谱系测试：任一关键 factor 无 source ID 时该证券不计完整且不能
      排名。
- [x] 写平局、缩尾、`normalization_scope=pilot_universe` 和三个池不产生总分测试。
- [x] 写文案黄金测试，所有事实数字来自 factor，所有事件来自 source ID；输出中不得
      出现“建议买入”“保证收益”等指令。
- [x] 运行
      `python -m pytest tests/unit/strategies tests/integration/test_m3_strategy_acceptance.py -q`
      并确认 RED。
- [x] 实现输入构建器、就绪度、版本化定义和规则模板；必要时将引擎归一化范围注入为
      显式配置，不保留隐含行业回退。
- [x] 运行目标测试、Ruff 和 `git diff --check`。
- [x] 提交：`git commit -m "feat: rank independently ready pilot pools"`。

---

## Task 11: 历史重建编排、检查点与部分池原子发布

**Files**

- Add: `src/hengce/services/pilot_reconstruction.py`
- Modify: `src/hengce/reports/publisher.py`
- Modify: `src/hengce/state/report_repository.py`
- Modify: `src/hengce/cli.py`
- Add: `tests/unit/services/test_pilot_reconstruction.py`
- Modify: `tests/unit/reports/test_publisher.py`
- Add: `tests/integration/test_pilot_reconstruction.py`
- Add: `tests/integration/test_pilot_report_acceptance.py`
- Modify: `tests/unit/test_cli.py`

**Required interfaces**

```python
class HistoricalPilotRunner:
    def run(
        self,
        *,
        market_date: date,
        report_cutoff_at: datetime,
        known_at: datetime,
        acquisition_mode: Literal["manual-only", "approved-public"],
    ) -> PilotRunSummary: ...
```

固定阶段：

```text
01_backup_and_migrate
02_validate_inputs
03_freeze_universe
04_plan_acquisition
05_acquire_public_documents
06_scan_manual_inbox
07_ingest_documents
08_assemble_point_in_time_facts
09_calculate_metrics
10_run_filters_and_pools
11_publish_report
12_write_run_summary
```

每阶段的检查点使用上阶段输出哈希；中断重跑必须复用相同样本、清单和已验证 Raw
对象。`approved-public` 才允许自动访问；`manual-only` 从不联网。运行前把状态库备份
到 `data/backups/hengce-<UTC timestamp>-<sha256>.sqlite3`，备份与原库均做
`PRAGMA integrity_check`。

CLI：

```text
hengce rebuild-pilot-report \
  --market-date 2026-07-22 \
  --report-cutoff-at 2026-07-22T21:30:00+08:00 \
  --acquisition-mode manual-only \
  --data-dir data
```

`known_at` 默认命令开始的 Asia/Shanghai 时间，不允许由用户设置为未来时间。命令输出
只含聚合 JSON：universe ID、12 阶段状态、360 项状态分布、XBRL/PDF 使用数、三池
覆盖率、报告 ID/哈希和人工待办数，不输出 token、附件正文或候选完整事实。

`ReportPublisher.publish()` 改为接收三个 `PoolReadiness`：

- 核心域 `market/security_master/pilot_universe/manifest` 任一失败时不移动 latest；
- `READY` 池必须有完整 evidence 和候选；`BLOCKED` 池必须为空且有阻断原因；
- 一个或两个池就绪时发布 `PUBLISHED_PARTIAL`；
- 三池全阻断时仍发布质量报告和待办，但不含候选榜；
- 写工件或 SQLite 指针失败时旧 latest 保持不变；
- 同 `report_id` 同哈希幂等，不同哈希报不可变冲突。

**TDD steps**

- [x] 写编排器 fake-stage 测试，证明阶段顺序、输入哈希、成功续跑、失败停点和清单
      不重抽样。
- [x] 写 `manual-only` 零网络调用和 `approved-public` 仍经过 PolicyGuard 的测试。
- [x] 写报告发布测试：三池 READY、单池 BLOCKED、三池 BLOCKED、核心域失败、源谱系
      缺失、候选时间线违规、工件写入失败。
- [x] 写历史字段测试：`report_cutoff_at` 固定 2026-07-22 21:30，
      `known_at/generation_started_at` 为实际运行时间，晚采集但早发布的事实可以使用，
      晚发布事实禁止使用。
- [x] 写 CLI 测试：参数严格解析、未来 known_at 拒绝、聚合摘要不泄密、退出码
      `0=已发布/部分发布`、`2=等待人工`、`1=失败`。
- [x] 运行
      `python -m pytest tests/unit/services/test_pilot_reconstruction.py tests/unit/reports/test_publisher.py tests/integration/test_pilot_reconstruction.py tests/integration/test_pilot_report_acceptance.py tests/unit/test_cli.py -q`
      并确认 RED。
- [x] 实现编排器、报告发布调整和 CLI 组装；不要在 CLI 函数中堆业务逻辑。
- [x] 运行目标测试、Ruff 和 `git diff --check`。
- [x] 提交：`git commit -m "feat: rebuild and publish historical pilot report"`。

---

## Task 12: 将真实历史重建状态接入 API 与五页 UI

**Files**

- Modify: `src/hengce/api/app.py`
- Modify: `tests/integration/test_m4_api.py`
- Modify: `apps/web/src/types.ts`
- Modify: `apps/web/src/api.ts`
- Modify: `apps/web/src/App.tsx`
- Modify: `apps/web/src/styles.css`
- Modify: `apps/web/src/App.test.tsx`

**API behavior**

- `/api/reports/latest` 和 `/api/reports/{id}` 返回历史重建字段、样本摘要、三个
  `PoolReadiness` 和人工待办摘要；
- `/api/strategies/{strategy}` 对阻断池返回 HTTP 200、空 `candidates` 和完整
  `readiness`，不返回 404；
- `/api/quality` 增加清单状态分布、XBRL/PDF 数量、池缺失项和 `known_at`；
- API 仍只读取已发布且哈希校验通过的工件，不读 Raw/Parquet/未完成清单。

**UI behavior**

- 顶部固定显示“真实数据 · 30只试点样本 · 历史重建”；
- 同时显示报告截止 `2026-07-22 21:30 +08:00` 与实际生成时间；
- 显示“排名只在 30 只试点样本内有效”，禁止“全市场/行业排名”字样；
- 每个策略卡显示 `READY/BLOCKED`、`complete_factor_count/30` 和覆盖率；
- 阻断池显示缺失证券、缺失因子和阻断码，不显示空表伪装为“暂无候选”；
- 个股页显示真实名称、A 股代码、因子原值、来源链接、模板文案和失效条件；
- 非 `?demo=1` 路径绝不导入或读取 `DEMO_REPORT`；无真实报告时显示明确错误；
- 失败更新时展示上一个已发布报告并标记 stale，不混入本次未发布数据。

**TDD steps**

- [x] 先扩展 API 集成测试，覆盖 READY/BLOCKED 池、历史时间字段、质量摘要和工件哈希
      失败；运行 `python -m pytest tests/integration/test_m4_api.py -q` 确认 RED。
- [x] 修改 API 到 GREEN，并运行 Ruff。
- [x] 扩展 TypeScript 类型，禁止 `any`；写 UI 测试覆盖真实横幅、双时间、试点边界、
      单池阻断、全池阻断、stale 和真实模式无 demo 回退。
- [x] 运行 `npm test -- --run`（工作目录 `apps/web`）并确认 RED。
- [x] 实现最小组件和样式；把 demo 数据改为仅在 `demoMode` 分支动态导入，生产 bundle
      初始化路径不得执行 demo 模块。
- [x] 运行 `npm test -- --run` 与 `npm run build`（工作目录 `apps/web`）并确认通过。
- [x] 运行 `python -m pytest tests/integration/test_m4_api.py -q`、
      `python -m ruff check src/hengce/api tests/integration/test_m4_api.py` 和
      `git diff --check`。
- [x] 使用本地 API 启动页面，在 1440×900 与 1280×720 复核五页：无白屏、无横向
      溢出、中文无乱码、阻断信息可读、来源链接可点击。
- [x] 提交：`git commit -m "feat: show real pilot readiness in local UI"`。

---

## Task 13: 增加本地真实数据验收器与操作手册

**Files**

- Add: `src/hengce/services/pilot_acceptance.py`
- Add: `tests/unit/services/test_pilot_acceptance.py`
- Add: `docs/runbooks/real-data-minimal-loop.md`
- Modify: `.gitignore`
- Modify: `README.md`

**Required interface**

```python
class PilotAcceptanceValidator:
    def validate(
        self,
        *,
        data_dir: Path,
        market_date: date,
        report_cutoff_at: datetime,
    ) -> PilotAcceptanceSummary: ...
```

验收器只输出聚合信息并验证：

- 样本恰好 30 且 8/8/7/7；
- 样本与 150 个财报清单 ID 哈希稳定；
- 360 项状态合计无遗漏；
- XBRL/PDF 使用数与每个 fallback 原因；
- 财务事实、行动、股本和派生指标数量；
- 三池各自覆盖率、状态和候选数；
- 候选的所有来源 ID、时间与模板版本可解析；
- 报告工件哈希与 SQLite latest 指针一致；
- API 返回与报告工件同一 report ID；
- `git status --ignored` 证明真实附件、库、Parquet、Raw、收件箱和报告均被忽略。

运行手册必须给出：

1. 检查 `.env` 和 Tushare token（不打印值）；
2. 验证 2026-07-22 行情与双交易所主数据；
3. 先以 `manual-only` 生成样本和 360 项清单；
4. 只在用户明确允许时使用 `approved-public`；
5. 如何查看 `AWAITING_MANUAL` 聚合清单；
6. 如何准备附件和 `.json` 侧车并重新运行；
7. 如何启动本地 API/UI；
8. 如何运行验收器、备份恢复和故障续跑；
9. 明确禁止提交 `data/`、`.env`、真实报告和附件。

**TDD steps**

- [x] 写全通过、配额错误、清单缺项、哈希错、latest 错、候选谱系断裂和敏感文件未被
      忽略的测试。
- [x] 运行 `python -m pytest tests/unit/services/test_pilot_acceptance.py -q`
      并确认 RED。
- [x] 实现只读验收器和 CLI 子命令 `validate-pilot-report`；不修改任何业务状态。
- [x] 更新 `.gitignore`，明确包含
      `data/manual_inbox/`、`data/reports/`、`data/backups/` 和运行摘要。
- [x] 编写手册并逐条执行命令拼写检查；所有示例使用占位路径，不含 token 或真实
      候选内容。
- [x] 运行目标测试、Ruff 和 `git diff --check`。
- [x] 提交：`git commit -m "docs: add real-data pilot acceptance runbook"`。

---

## Task 14: 全量验证、真实私有冒烟与评审

**Files**

- Modify only if verification exposes a defect; every fix must first add a regression test.

**Automated verification**

- [ ] 从仓库根目录运行：

  ```powershell
  python -m pytest -q
  python -m ruff check .
  git diff --check
  ```

- [x] 从 `apps/web` 运行：

  ```powershell
  npm test -- --run
  npm run build
  ```

- [x] 确认 CI 测试中没有真实网络访问：搜索 `sse.com.cn`、`szse.cn`、
      `cninfo.com.cn` 的调用只能出现在政策/解析夹具断言中，不能由测试 client 发出。
- [x] 扫描占位和泄密风险：

  ```powershell
  rg -n "T[O]DO|T[B]D|F[I]XME|HENGCE_TUSHARE_TOKEN=|token['\"]?\s*[:=]" src tests apps docs
  git status --short --ignored
  ```

- [x] 确认 Git 跟踪文件不含 `.env`、SQLite、Parquet、PDF、ZIP、XBRL 真实附件、
      `data/reports` 或真实运行摘要。

**Private real-data smoke test**

- [x] 在本机私有 `data/` 上先运行 `validate-pilot-report` 的输入预检，验证现有
      2026-07-22 行情 5198 行和双交易所主数据完整，且不打印 token。
- [x] 运行 `rebuild-pilot-report --acquisition-mode manual-only`，核对样本为
      30 只、配额 8/8/7/7、清单 360 项、财报项 150；记录聚合摘要但不提交。
- [x] 如本轮已获得公开访问授权，运行一次 `approved-public`；遇到任何 401/403/429、
      验证码或登录墙时确认自动停止并生成 `AWAITING_MANUAL`，不尝试绕过。
- [ ] 将允许的人工附件及侧车放入私有收件箱后续跑；核对已验证文件不重复下载，样本
      和清单哈希不变化。
- [x] 当某池达到 24/30 时验证该池排名和来源；未达到的池只显示覆盖阻断。即使所有池
      阻断，也必须得到质量报告而不是虚构候选。
- [ ] 启动本地 API/UI，逐页复核 report ID、截止时间、生成时间、样本边界、池状态、
      个股来源与质量页一致。
- [x] 运行 `PilotAcceptanceValidator`，保存私有聚合结果在被忽略的 `data/` 下。

**Review and completion**

- [x] 使用 `superpowers:requesting-code-review` 进行规格符合性和代码质量评审。
- [ ] 对每个 Critical/Important 意见先复现或增加测试，再修复；重新运行全量验证。
- [ ] 使用 `superpowers:verification-before-completion` 检查最终测试输出、工作树和
      私有数据隔离证据。
- [ ] 更新 PR 描述，列明真实数据仍在本机、哪些池 READY/BLOCKED、人工待办数量和
      后续全市场扩展不在本 PR 范围内。
- [ ] 最终实现提交：`git commit -m "test: verify real-data minimal loop"`；若没有
      代码或文档变化，不创建空提交。

---

## Requirement-to-task traceability

| Approved requirement | Implemented by |
|---|---|
| 2026-07-22 21:30 严格截止、实际 known_at | Tasks 1, 9, 11 |
| 30 只、8/8/7/7 流动性抽样 | Tasks 3, 14 |
| 150 份定期报告、2021–2025 分红 | Tasks 4, 14 |
| 公开白名单、低频、限制即停、人工收件箱 | Task 5 |
| XBRL 优先、PDF 兜底、冲突阻断 | Tasks 6, 7, 9 |
| 更正不覆盖、当时可知查询 | Tasks 6–9 |
| 截止日总股本与估值依赖链 | Tasks 8, 9 |
| 三策略独立、24/30 门槛、100% 候选谱系 | Task 10 |
| 规则文案、不生成买入指令 | Task 10 |
| 可续跑历史重建和原子发布 | Task 11 |
| 真实 UI、阻断池、双时间、无 demo 回退 | Task 12 |
| 私有数据隔离、操作手册与真实验收 | Tasks 13, 14 |

## Official public discovery surfaces

实现期间只可从以下已批准的公开入口人工确认页面结构和可见附件链接，不得由链接推导
隐藏 API：

- 上交所定期报告公开页：<https://www.sse.com.cn/disclosure/listedinfo/regular/>
- 上交所上市公司信息/XBRL 展示：<https://www.sse.com.cn/disclosure/listedinfo/listedcompanies/>
- 上交所法律声明：<https://www.sse.com.cn/home/legal/>
- 深交所信息披露入口：<https://www.szse.cn/disclosure/index/>
- 深交所个股公开信息页：<https://www.szse.cn/certificate/individual/index.html>
- 巨潮资讯公开入口：<https://www.cninfo.com.cn/>

页面结构变化、下载地址不再可见、条款日期过期或附件主机不在明确白名单时，必须转
`AWAITING_MANUAL`，不得扩展自动采集权限。
