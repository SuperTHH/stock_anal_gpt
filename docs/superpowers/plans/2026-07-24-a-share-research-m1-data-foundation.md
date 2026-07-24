# A 股中长期研究平台 M1 数据底座实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立可恢复、可审计、合规优先的数据底座，能够保存来源政策、不可变原始对象、证券主数据和按交易日一次获取的全市场未复权日线。

**Architecture:** Python 包负责策略守卫、采集适配、不可变对象存储和初始化编排。SQLite 保存来源政策、拒绝、运行与检查点等事务状态；Parquet 保存不可变标准化表；DuckDB 提供本地分析视图。

**Tech Stack:** Python 3.12、Pydantic 2、pydantic-settings、SQLite、DuckDB、PyArrow、HTTPX、Typer、Pytest、Ruff。

## Global Constraints

- 最高需求基线：`docs/product/2026-07-24-a-share-long-term-research-platform-prd.md`。
- 只实现 M1；不得提前实现财务、复权、策略、报告 API 或页面。
- 只覆盖沪深主板、创业板、科创板 A 股。
- Tushare `daily` 每个交易日调用一次，不得逐股票循环。
- Tushare REST API 使用官方文档指定的 `http://api.tushare.pro`；Policy Guard 只对该精确主机批准 HTTP，其他 MVP 自动来源只允许 HTTPS。
- 非白名单、未批准政策、401、403、429 和验证码在 M1 中必须停止，不得绕过。
- 原始对象按 SHA-256 不可变保存；重复内容不重复写入。
- SQLite 保存事务状态，DuckDB/Parquet 保存分析数据。
- Token 只从环境变量读取，不得进入日志、测试输出、Git 或数据库。
- 所有网络测试使用 HTTPX MockTransport；默认测试套件不得访问网络。
- 每个任务遵循 red-green-refactor，并在独立提交前运行相关测试和 `ruff check`。

---

## Task 1: Python 工程骨架与安全配置

**Files:**

- Create: `pyproject.toml`
- Create: `src/hengce/__init__.py`
- Create: `src/hengce/config.py`
- Create: `tests/unit/test_config.py`
- Create: `.env.example`
- Modify: `.gitignore`

**Interfaces:**

- Produces: `Settings.load() -> Settings`
- Produces: `Settings.ensure_local_dirs() -> None`
- Consumes: 环境变量 `HENGCE_DATA_DIR`、`TUSHARE_TOKEN`

- [ ] **Step 1: 写入失败测试**

```python
# tests/unit/test_config.py
from pathlib import Path

from hengce.config import Settings


def test_settings_keep_token_out_of_repr(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, tushare_token="secret-token")
    assert "secret-token" not in repr(settings)


def test_settings_create_only_expected_local_directories(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    settings.ensure_local_dirs()
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "normalized",
        "raw",
        "reports",
        "state",
        "warehouse",
    ]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/unit/test_config.py -q`

Expected: FAIL，错误包含 `ModuleNotFoundError: No module named 'hengce'`。

- [ ] **Step 3: 创建工程配置**

```toml
# pyproject.toml
[build-system]
requires = ["hatchling>=1.25,<2"]
build-backend = "hatchling.build"

[project]
name = "hengce-research"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "duckdb>=1.0,<2",
  "httpx>=0.27,<1",
  "pyarrow>=16,<20",
  "pydantic>=2.8,<3",
  "pydantic-settings>=2.3,<3",
  "typer>=0.12,<1",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.2,<9",
  "pytest-cov>=5,<7",
  "ruff>=0.5,<1",
]

[tool.hatch.build.targets.wheel]
packages = ["src/hengce"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra"

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP"]
```

```python
# src/hengce/__init__.py
__all__ = ["__version__"]
__version__ = "0.1.0"
```

```python
# src/hengce/config.py
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HENGCE_",
        env_file=".env",
        extra="ignore",
    )

    data_dir: Path = Path("data")
    tushare_token: SecretStr | None = Field(default=None, repr=False)
    timezone: str = "Asia/Shanghai"

    def ensure_local_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for name in ("raw", "normalized", "warehouse", "state", "reports"):
            (self.data_dir / name).mkdir(exist_ok=True)

    @classmethod
    def load(cls) -> "Settings":
        settings = cls()
        settings.ensure_local_dirs()
        return settings
```

```dotenv
# .env.example
HENGCE_DATA_DIR=data
HENGCE_TUSHARE_TOKEN=
```

Add to `.gitignore`:

```gitignore
.env
.venv/
__pycache__/
.pytest_cache/
.ruff_cache/
data/
*.duckdb
*.sqlite
*.sqlite3
```

- [ ] **Step 4: 安装并验证**

Run: `python -m pip install -e ".[dev]"`

Expected: exit 0。

Run: `python -m pytest tests/unit/test_config.py -q`

Expected: `2 passed`。

Run: `ruff check src tests`

Expected: `All checks passed!`

- [ ] **Step 5: 提交**

```powershell
git add pyproject.toml .env.example .gitignore src/hengce tests/unit/test_config.py
git commit -m "chore: scaffold local research data project"
```

## Task 2: 核心枚举与数据契约

**Files:**

- Create: `src/hengce/contracts/__init__.py`
- Create: `src/hengce/contracts/enums.py`
- Create: `src/hengce/contracts/base.py`
- Create: `src/hengce/contracts/policy.py`
- Create: `src/hengce/contracts/market.py`
- Create: `src/hengce/contracts/run.py`
- Create: `tests/unit/contracts/test_models.py`

**Interfaces:**

- Produces: `SourcePolicy`、`SecurityMaster`、`TradingStatus`、`MarketBar`
- Produces: `RunRecord`、`RefusalRecord`
- Produces: `QualityStatus`、`RunStatus`、`ReviewStatus`

- [ ] **Step 1: 写入契约测试**

```python
# tests/unit/contracts/test_models.py
from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from hengce.contracts.enums import QualityStatus, ReviewStatus
from hengce.contracts.market import MarketBar
from hengce.contracts.policy import SourcePolicy


def test_market_bar_rejects_inconsistent_price_range() -> None:
    with pytest.raises(ValidationError):
        MarketBar(
            record_id="bar-1",
            source_id="tushare",
            source_url="https://tushare.pro/",
            collected_at=datetime(2026, 7, 24, 21, 31),
            version="raw-1",
            content_hash="a" * 64,
            license_policy="tushare-daily",
            quality_status=QualityStatus.VALID,
            valid_from=datetime(2026, 7, 24, 21, 31),
            ts_code="600000.SH",
            trade_date=date(2026, 7, 24),
            open=Decimal("10"),
            high=Decimal("9"),
            low=Decimal("8"),
            close=Decimal("9"),
            pre_close=Decimal("9"),
            volume=Decimal("100"),
            amount=Decimal("900"),
        )


def test_source_policy_requires_approved_review_to_be_enabled() -> None:
    with pytest.raises(ValidationError):
        SourcePolicy(
            source_id="sse",
            source_name="上交所",
            allowed_domains=["www.sse.com.cn"],
            allowed_schemes=["https"],
            allowed_purposes=["security_master"],
            fetch_frequency="daily",
            full_text_rule="necessary_public_attachment",
            attachment_rule="pdf_xbrl_only",
            rate_limit_per_minute=6,
            robots_policy="respect",
            terms_url="https://www.sse.com.cn/home/legal/",
            review_status=ReviewStatus.REVIEW_REQUIRED,
            connection_status="UNKNOWN",
            enabled=True,
        )
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/unit/contracts/test_models.py -q`

Expected: FAIL，错误包含 `No module named 'hengce.contracts'`。

- [ ] **Step 3: 实现枚举与基础契约**

```python
# src/hengce/contracts/enums.py
from enum import StrEnum


class QualityStatus(StrEnum):
    VALID = "VALID"
    DERIVED = "DERIVED"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    CONFLICT = "CONFLICT"
    STALE = "STALE"
    UNVERIFIED = "UNVERIFIED"
    REJECTED = "REJECTED"


class ReviewStatus(StrEnum):
    APPROVED = "APPROVED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"


class RunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
```

```python
# src/hengce/contracts/base.py
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from .enums import QualityStatus


class FactBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    source_id: str
    source_url: HttpUrl
    published_at: datetime | None = None
    effective_at: datetime | None = None
    collected_at: datetime
    version: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    license_policy: str
    quality_status: QualityStatus
    supersedes_id: str | None = None
    valid_from: datetime
```

```python
# src/hengce/contracts/policy.py
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, HttpUrl, model_validator

from .enums import ReviewStatus


class SourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    source_name: str
    allowed_domains: list[str]
    allowed_schemes: list[Literal["http", "https"]]
    allowed_purposes: list[str]
    fetch_frequency: str
    full_text_rule: str
    attachment_rule: str
    rate_limit_per_minute: int
    robots_policy: str
    terms_url: HttpUrl
    terms_reviewed_at: datetime | None = None
    review_status: ReviewStatus
    connection_status: str
    enabled: bool

    @model_validator(mode="after")
    def approved_when_enabled(self) -> "SourcePolicy":
        if self.enabled and self.review_status is not ReviewStatus.APPROVED:
            raise ValueError("enabled source policy must be approved")
        return self
```

- [ ] **Step 4: 实现市场与运行契约**

```python
# src/hengce/contracts/market.py
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, model_validator

from .base import FactBase


class SecurityMaster(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts_code: str
    symbol: str
    name: str
    exchange: str
    board: str
    currency: str = "CNY"
    list_date: date
    delist_date: date | None = None
    industry_l1: str | None = None
    security_type: str = "A_SHARE"
    is_in_scope: bool


class TradingStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts_code: str
    trade_date: date
    is_trading: bool
    is_suspended: bool
    st_status: str | None = None
    delisting_risk: bool = False
    special_treatment_reason: str | None = None


class MarketBar(FactBase):
    ts_code: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    pre_close: Decimal
    volume: Decimal
    amount: Decimal
    currency: str = "CNY"

    @model_validator(mode="after")
    def valid_range(self) -> "MarketBar":
        if min(self.open, self.high, self.low, self.close, self.pre_close) < 0:
            raise ValueError("prices must be non-negative")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high is below another price")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low is above another price")
        if self.volume < 0 or self.amount < 0:
            raise ValueError("volume and amount must be non-negative")
        return self
```

```python
# src/hengce/contracts/run.py
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from .enums import RunStatus


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    trade_date: date
    run_type: str
    started_at: datetime
    finished_at: datetime | None = None
    run_status: RunStatus
    stage_statuses: dict[str, str]
    retry_count: int = 0
    error_code: str | None = None
    error_summary: str | None = None
    published_report_id: str | None = None


class RefusalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refusal_id: str
    requested_url: str
    resolved_domain: str
    requested_purpose: str
    policy_rule: str
    refused_at: datetime
    reason_code: str
    requesting_module: str
```

```python
# src/hengce/contracts/__init__.py
from .market import MarketBar, SecurityMaster, TradingStatus
from .policy import SourcePolicy
from .run import RefusalRecord, RunRecord

__all__ = [
    "MarketBar",
    "RefusalRecord",
    "RunRecord",
    "SecurityMaster",
    "SourcePolicy",
    "TradingStatus",
]
```

- [ ] **Step 5: 运行契约测试与静态检查**

Run: `python -m pytest tests/unit/contracts/test_models.py -q`

Expected: `2 passed`。

Run: `ruff check src tests`

Expected: `All checks passed!`

- [ ] **Step 6: 提交**

```powershell
git add src/hengce/contracts tests/unit/contracts
git commit -m "feat: define M1 data contracts"
```

## Task 3: SQLite 轻量事务状态库

**Files:**

- Create: `src/hengce/state/__init__.py`
- Create: `src/hengce/state/db.py`
- Create: `src/hengce/state/migrations/001_initial.sql`
- Create: `src/hengce/state/repository.py`
- Create: `tests/unit/state/test_repository.py`

**Interfaces:**

- Consumes: `SourcePolicy`、`RunRecord`、`RefusalRecord`
- Produces: `StateRepository.upsert_policy(policy) -> None`
- Produces: `StateRepository.get_policy(source_id) -> SourcePolicy | None`
- Produces: `StateRepository.record_refusal(record) -> None`
- Produces: `StateRepository.save_checkpoint(key, value) -> None`

- [ ] **Step 1: 写入失败测试**

```python
# tests/unit/state/test_repository.py
from datetime import datetime
from pathlib import Path

from hengce.contracts.enums import ReviewStatus
from hengce.contracts.policy import SourcePolicy
from hengce.state.repository import StateRepository


def approved_policy() -> SourcePolicy:
    return SourcePolicy(
        source_id="tushare",
        source_name="Tushare",
        allowed_domains=["api.tushare.pro"],
        allowed_schemes=["http"],
        allowed_purposes=["market_daily"],
        fetch_frequency="trading_day",
        full_text_rule="structured_only",
        attachment_rule="none",
        rate_limit_per_minute=1,
        robots_policy="api_terms",
        terms_url="https://tushare.pro/document/1?doc_id=290",
        terms_reviewed_at=datetime(2026, 7, 24, 9, 0),
        review_status=ReviewStatus.APPROVED,
        connection_status="UNKNOWN",
        enabled=True,
    )


def test_repository_round_trips_policy_and_checkpoint(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    repository.upsert_policy(approved_policy())
    repository.save_checkpoint("init:last_trade_date", "2026-07-24")
    assert repository.get_policy("tushare") == approved_policy()
    assert repository.get_checkpoint("init:last_trade_date") == "2026-07-24"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/unit/state/test_repository.py -q`

Expected: FAIL，错误包含 `No module named 'hengce.state'`。

- [ ] **Step 3: 创建迁移和数据库连接**

```sql
-- src/hengce/state/migrations/001_initial.sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_policies (
    source_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_records (
    run_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refusal_records (
    refusal_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    refused_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_key TEXT PRIMARY KEY,
    checkpoint_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

```python
# src/hengce/state/db.py
import sqlite3
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection
```

- [ ] **Step 4: 实现仓储**

```python
# src/hengce/state/repository.py
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from hengce.contracts.policy import SourcePolicy
from hengce.contracts.run import RefusalRecord, RunRecord

from .db import connect


class StateRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def migrate(self) -> None:
        sql = (
            files("hengce.state")
            .joinpath("migrations/001_initial.sql")
            .read_text(encoding="utf-8")
        )
        with connect(self.path) as connection:
            connection.executescript(sql)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                ("001_initial", datetime.now(UTC).isoformat()),
            )

    def upsert_policy(self, policy: SourcePolicy) -> None:
        with connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO source_policies(source_id, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (policy.source_id, policy.model_dump_json(), datetime.now(UTC).isoformat()),
            )

    def get_policy(self, source_id: str) -> SourcePolicy | None:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM source_policies WHERE source_id=?",
                (source_id,),
            ).fetchone()
        return SourcePolicy.model_validate_json(row["payload_json"]) if row else None

    def record_run(self, record: RunRecord) -> None:
        with connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO run_records(run_id, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (record.run_id, record.model_dump_json(), datetime.now(UTC).isoformat()),
            )

    def record_refusal(self, record: RefusalRecord) -> None:
        with connect(self.path) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO refusal_records(refusal_id, payload_json, refused_at)
                VALUES (?, ?, ?)
                """,
                (record.refusal_id, record.model_dump_json(), record.refused_at.isoformat()),
            )

    def save_checkpoint(self, key: str, value: str) -> None:
        with connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO checkpoints(checkpoint_key, checkpoint_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(checkpoint_key) DO UPDATE SET
                    checkpoint_value=excluded.checkpoint_value,
                    updated_at=excluded.updated_at
                """,
                (key, value, datetime.now(UTC).isoformat()),
            )

    def get_checkpoint(self, key: str) -> str | None:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT checkpoint_value FROM checkpoints WHERE checkpoint_key=?",
                (key,),
            ).fetchone()
        return str(row["checkpoint_value"]) if row else None
```

```python
# src/hengce/state/__init__.py
from .repository import StateRepository

__all__ = ["StateRepository"]
```

- [ ] **Step 5: 运行测试**

Run: `python -m pytest tests/unit/state/test_repository.py -q`

Expected: `1 passed`。

Run: `ruff check src tests`

Expected: `All checks passed!`

- [ ] **Step 6: 提交**

```powershell
git add src/hengce/state tests/unit/state
git commit -m "feat: add SQLite state repository"
```

## Task 4: 来源政策守卫与拒绝记录

**Files:**

- Create: `src/hengce/policy/__init__.py`
- Create: `src/hengce/policy/guard.py`
- Create: `tests/unit/policy/test_guard.py`

**Interfaces:**

- Consumes: `StateRepository.get_policy(source_id)`
- Consumes: `StateRepository.record_refusal(record)`
- Produces: `PolicyGuard.authorize(source_id, url, purpose, module) -> None`
- Raises: `PolicyDenied(reason_code)`

- [ ] **Step 1: 写入失败测试**

```python
# tests/unit/policy/test_guard.py
from datetime import datetime
from pathlib import Path

import pytest

from hengce.contracts.enums import ReviewStatus
from hengce.contracts.policy import SourcePolicy
from hengce.policy.guard import PolicyDenied, PolicyGuard
from hengce.state.repository import StateRepository


def repository_with_tushare(tmp_path: Path) -> StateRepository:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    repository.upsert_policy(
        SourcePolicy(
            source_id="tushare",
            source_name="Tushare",
            allowed_domains=["api.tushare.pro"],
            allowed_schemes=["http"],
            allowed_purposes=["market_daily"],
            fetch_frequency="trading_day",
            full_text_rule="structured_only",
            attachment_rule="none",
            rate_limit_per_minute=1,
            robots_policy="api_terms",
            terms_url="https://tushare.pro/document/1?doc_id=290",
            terms_reviewed_at=datetime(2026, 7, 24, 9, 0),
            review_status=ReviewStatus.APPROVED,
            connection_status="UNKNOWN",
            enabled=True,
        )
    )
    return repository


def test_guard_allows_exact_approved_domain_and_purpose(tmp_path: Path) -> None:
    guard = PolicyGuard(repository_with_tushare(tmp_path))
    guard.authorize(
        "tushare",
        "http://api.tushare.pro/",
        "market_daily",
        "collectors.tushare",
    )


def test_guard_rejects_unapproved_domain_before_request(tmp_path: Path) -> None:
    repository = repository_with_tushare(tmp_path)
    guard = PolicyGuard(repository)
    with pytest.raises(PolicyDenied, match="DOMAIN_NOT_ALLOWED"):
        guard.authorize(
            "tushare",
            "http://example.com/data",
            "market_daily",
            "collectors.tushare",
        )
    assert repository.count_refusals() == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/unit/policy/test_guard.py -q`

Expected: FAIL，错误包含 `No module named 'hengce.policy'`。

- [ ] **Step 3: 为状态库增加拒绝计数**

Add to `StateRepository`:

```python
def count_refusals(self) -> int:
    with connect(self.path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM refusal_records"
        ).fetchone()
    return int(row["count"])
```

- [ ] **Step 4: 实现政策守卫**

```python
# src/hengce/policy/guard.py
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4

from hengce.contracts.enums import ReviewStatus
from hengce.contracts.policy import SourcePolicy
from hengce.contracts.run import RefusalRecord
from hengce.state.repository import StateRepository


class PolicyDenied(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class PolicyGuard:
    def __init__(self, repository: StateRepository) -> None:
        self.repository = repository

    def authorize(self, source_id: str, url: str, purpose: str, module: str) -> None:
        policy = self.repository.get_policy(source_id)
        reason = self._deny_reason(policy, url, purpose)
        if reason is None:
            return
        parsed = urlparse(url)
        self.repository.record_refusal(
            RefusalRecord(
                refusal_id=str(uuid4()),
                requested_url=url,
                resolved_domain=(parsed.hostname or "").lower(),
                requested_purpose=purpose,
                policy_rule=source_id,
                refused_at=datetime.now(UTC),
                reason_code=reason,
                requesting_module=module,
            )
        )
        raise PolicyDenied(reason)

    @staticmethod
    def _deny_reason(
        policy: SourcePolicy | None,
        url: str,
        purpose: str,
    ) -> str | None:
        if policy is None:
            return "SOURCE_POLICY_MISSING"
        if not policy.enabled:
            return "SOURCE_DISABLED"
        if policy.review_status is not ReviewStatus.APPROVED:
            return "SOURCE_REVIEW_REQUIRED"
        parsed = urlparse(url)
        if parsed.scheme not in policy.allowed_schemes or not parsed.hostname:
            return "SCHEME_NOT_ALLOWED"
        hostname = parsed.hostname.lower()
        allowed = any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in policy.allowed_domains
        )
        if not allowed:
            return "DOMAIN_NOT_ALLOWED"
        if purpose not in policy.allowed_purposes:
            return "PURPOSE_NOT_ALLOWED"
        return None
```

```python
# src/hengce/policy/__init__.py
from .guard import PolicyDenied, PolicyGuard

__all__ = ["PolicyDenied", "PolicyGuard"]
```

- [ ] **Step 5: 运行测试**

Run: `python -m pytest tests/unit/policy/test_guard.py -q`

Expected: `2 passed`。

Run: `ruff check src tests`

Expected: `All checks passed!`

- [ ] **Step 6: 提交**

```powershell
git add src/hengce/state/repository.py src/hengce/policy tests/unit/policy
git commit -m "feat: enforce source policy before network access"
```

## Task 5: 不可变原始对象存储

**Files:**

- Create: `src/hengce/raw_store/__init__.py`
- Create: `src/hengce/raw_store/store.py`
- Create: `tests/unit/raw_store/test_store.py`

**Interfaces:**

- Produces: `RawObjectStore.put(...) -> RawObjectRef`
- Guarantees: 同一 SHA-256 只写入一次，已有对象永不覆盖

- [ ] **Step 1: 写入失败测试**

```python
# tests/unit/raw_store/test_store.py
from datetime import UTC, datetime
from pathlib import Path

from hengce.raw_store.store import RawObjectStore


def test_same_content_is_stored_once_without_overwrite(tmp_path: Path) -> None:
    store = RawObjectStore(tmp_path)
    collected_at = datetime(2026, 7, 24, 21, 31, tzinfo=UTC)
    first = store.put(
        source_id="tushare",
        source_url="http://api.tushare.pro/",
        collected_at=collected_at,
        content_type="application/json",
        payload=b'{"ok":true}',
    )
    second = store.put(
        source_id="tushare",
        source_url="http://api.tushare.pro/",
        collected_at=collected_at,
        content_type="application/json",
        payload=b'{"ok":true}',
    )
    assert first == second
    assert len(list(tmp_path.rglob("payload.bin"))) == 1
    assert len(list(tmp_path.rglob("metadata.json"))) == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/unit/raw_store/test_store.py -q`

Expected: FAIL，错误包含 `No module named 'hengce.raw_store'`。

- [ ] **Step 3: 实现不可变存储**

```python
# src/hengce/raw_store/store.py
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class RawObjectRef:
    source_id: str
    content_hash: str
    payload_path: str
    metadata_path: str


class RawObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def put(
        self,
        *,
        source_id: str,
        source_url: str,
        collected_at: datetime,
        content_type: str,
        payload: bytes,
    ) -> RawObjectRef:
        if not re.fullmatch(r"[a-z0-9_-]+", source_id):
            raise ValueError("invalid source_id")
        digest = hashlib.sha256(payload).hexdigest()
        directory = (
            self.root
            / source_id
            / collected_at.strftime("%Y")
            / collected_at.strftime("%m")
            / collected_at.strftime("%d")
            / digest
        )
        directory.mkdir(parents=True, exist_ok=True)
        payload_path = directory / "payload.bin"
        metadata_path = directory / "metadata.json"
        reference = RawObjectRef(
            source_id=source_id,
            content_hash=digest,
            payload_path=str(payload_path),
            metadata_path=str(metadata_path),
        )
        if not payload_path.exists():
            payload_path.write_bytes(payload)
        if not metadata_path.exists():
            metadata = {
                **asdict(reference),
                "source_url": source_url,
                "collected_at": collected_at.isoformat(),
                "content_type": content_type,
                "size_bytes": len(payload),
            }
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
        return reference
```

```python
# src/hengce/raw_store/__init__.py
from .store import RawObjectRef, RawObjectStore

__all__ = ["RawObjectRef", "RawObjectStore"]
```

- [ ] **Step 4: 运行测试和不可变性检查**

Run: `python -m pytest tests/unit/raw_store/test_store.py -q`

Expected: `1 passed`。

Run: `ruff check src tests`

Expected: `All checks passed!`

- [ ] **Step 5: 提交**

```powershell
git add src/hengce/raw_store tests/unit/raw_store
git commit -m "feat: add immutable raw object store"
```

## Task 6: Parquet 行情表与 DuckDB 查询

**Files:**

- Create: `src/hengce/warehouse/__init__.py`
- Create: `src/hengce/warehouse/market.py`
- Create: `tests/unit/warehouse/test_market.py`

**Interfaces:**

- Consumes: `list[MarketBar]`
- Produces: `MarketWarehouse.write_bars(bars) -> Path`
- Produces: `MarketWarehouse.count_bars(trade_date) -> int`
- Produces: `MarketWarehouse.read_bars(trade_date) -> list[dict[str, object]]`

- [ ] **Step 1: 写入失败测试**

```python
# tests/unit/warehouse/test_market.py
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from hengce.contracts.enums import QualityStatus
from hengce.contracts.market import MarketBar
from hengce.warehouse.market import MarketWarehouse


def bar(code: str) -> MarketBar:
    return MarketBar(
        record_id=f"{code}-20260724",
        source_id="tushare",
        source_url="http://api.tushare.pro/",
        collected_at=datetime(2026, 7, 24, 21, 31, tzinfo=UTC),
        version="daily-20260724",
        content_hash="a" * 64,
        license_policy="tushare-daily",
        quality_status=QualityStatus.VALID,
        valid_from=datetime(2026, 7, 24, 21, 31, tzinfo=UTC),
        ts_code=code,
        trade_date=date(2026, 7, 24),
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10.5"),
        pre_close=Decimal("10"),
        volume=Decimal("1000"),
        amount=Decimal("10500"),
    )


def test_write_is_idempotent_and_queryable(tmp_path: Path) -> None:
    warehouse = MarketWarehouse(tmp_path)
    first = warehouse.write_bars([bar("600000.SH"), bar("000001.SZ")])
    second = warehouse.write_bars([bar("000001.SZ"), bar("600000.SH")])
    assert first == second
    assert warehouse.count_bars(date(2026, 7, 24)) == 2
    assert [row["ts_code"] for row in warehouse.read_bars(date(2026, 7, 24))] == [
        "000001.SZ",
        "600000.SH",
    ]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/unit/warehouse/test_market.py -q`

Expected: FAIL，错误包含 `No module named 'hengce.warehouse'`。

- [ ] **Step 3: 实现确定性 Parquet 写入和 DuckDB 查询**

```python
# src/hengce/warehouse/market.py
import hashlib
import json
from datetime import date
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from hengce.contracts.market import MarketBar


class MarketWarehouse:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.dataset = root / "market_bars"

    def write_bars(self, bars: list[MarketBar]) -> Path:
        if not bars:
            raise ValueError("bars must not be empty")
        trade_dates = {bar.trade_date for bar in bars}
        if len(trade_dates) != 1:
            raise ValueError("one write must contain exactly one trade_date")
        ordered = sorted(bars, key=lambda item: (item.ts_code, item.version))
        rows = [item.model_dump(mode="json") for item in ordered]
        canonical = json.dumps(
            rows,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        trade_date = ordered[0].trade_date.isoformat()
        partition = self.dataset / f"trade_date={trade_date}"
        partition.mkdir(parents=True, exist_ok=True)
        path = partition / f"part-{digest}.parquet"
        if not path.exists():
            pq.write_table(pa.Table.from_pylist(rows), path, compression="zstd")
        return path

    def _glob(self) -> str:
        return str(self.dataset / "trade_date=*" / "part-*.parquet").replace("\\", "/")

    def count_bars(self, trade_date: date) -> int:
        if not self.dataset.exists():
            return 0
        with duckdb.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM read_parquet(?, hive_partitioning=true)
                WHERE trade_date = ?
                """,
                [self._glob(), trade_date.isoformat()],
            ).fetchone()
        return int(row[0])

    def read_bars(self, trade_date: date) -> list[dict[str, object]]:
        with duckdb.connect() as connection:
            cursor = connection.execute(
                """
                SELECT * EXCLUDE (trade_date)
                FROM read_parquet(?, hive_partitioning=true)
                WHERE trade_date = ?
                ORDER BY ts_code
                """,
                [self._glob(), trade_date.isoformat()],
            )
            columns = [item[0] for item in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
```

```python
# src/hengce/warehouse/__init__.py
from .market import MarketWarehouse

__all__ = ["MarketWarehouse"]
```

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/unit/warehouse/test_market.py -q`

Expected: `1 passed`。

Run: `ruff check src tests`

Expected: `All checks passed!`

- [ ] **Step 5: 提交**

```powershell
git add src/hengce/warehouse tests/unit/warehouse
git commit -m "feat: add local market data warehouse"
```

## Task 7: Tushare 单日全市场采集与证券主数据导入

**Files:**

- Create: `src/hengce/collectors/__init__.py`
- Create: `src/hengce/collectors/tushare.py`
- Create: `src/hengce/collectors/security_master.py`
- Create: `tests/unit/collectors/test_tushare.py`
- Create: `tests/unit/collectors/test_security_master.py`
- Create: `tests/fixtures/security_master.csv`

**Interfaces:**

- Consumes: `PolicyGuard.authorize(...)`
- Produces: `TushareDailyCollector.fetch(trade_date) -> DailyFetchResult`
- Produces: `OfficialSecurityMasterCsvImporter.parse(path) -> list[SecurityMaster]`
- Guarantees: 一个交易日一次 HTTP 请求；不接受逐股票参数

- [ ] **Step 1: 写入 Tushare 失败测试**

```python
# tests/unit/collectors/test_tushare.py
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
from pydantic import SecretStr

from hengce.collectors.tushare import TushareDailyCollector
from hengce.contracts.enums import ReviewStatus
from hengce.contracts.policy import SourcePolicy
from hengce.policy.guard import PolicyGuard
from hengce.state.repository import StateRepository


def test_fetch_daily_makes_one_full_market_request(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": None,
                "data": {
                    "fields": [
                        "ts_code",
                        "trade_date",
                        "open",
                        "high",
                        "low",
                        "close",
                        "pre_close",
                        "vol",
                        "amount",
                    ],
                    "items": [
                        [
                            "600000.SH",
                            "20260724",
                            10,
                            11,
                            9,
                            10.5,
                            10,
                            1000,
                            10500,
                        ]
                    ],
                },
            },
        )

    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    repository.upsert_policy(
        SourcePolicy(
            source_id="tushare",
            source_name="Tushare",
            allowed_domains=["api.tushare.pro"],
            allowed_schemes=["http"],
            allowed_purposes=["market_daily"],
            fetch_frequency="trading_day",
            full_text_rule="structured_only",
            attachment_rule="none",
            rate_limit_per_minute=1,
            robots_policy="api_terms",
            terms_url="https://tushare.pro/document/1?doc_id=290",
            terms_reviewed_at=datetime(2026, 7, 24, 9, 0, tzinfo=UTC),
            review_status=ReviewStatus.APPROVED,
            connection_status="AVAILABLE",
            enabled=True,
        )
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    collector = TushareDailyCollector(
        client=client,
        guard=PolicyGuard(repository),
        token=SecretStr("test-token"),
        clock=lambda: datetime(2026, 7, 24, 21, 31, tzinfo=UTC),
    )
    result = collector.fetch(date(2026, 7, 24))
    assert len(calls) == 1
    assert b'"trade_date":"20260724"' in calls[0].content
    assert b'"ts_code"' not in calls[0].content
    assert result.bars[0].ts_code == "600000.SH"
```

- [ ] **Step 2: 写入证券主数据失败测试和固定文件**

```csv
ts_code,symbol,name,exchange,board,currency,list_date,security_type
600000.SH,600000,浦发银行,SSE,MAIN_SH,CNY,19991110,A_SHARE
688001.SH,688001,华兴源创,SSE,STAR,CNY,20190722,A_SHARE
000001.SZ,000001,平安银行,SZSE,MAIN_SZ,CNY,19910403,A_SHARE
300001.SZ,300001,特锐德,SZSE,CHINEXT,CNY,20091030,A_SHARE
920001.BJ,920001,示例证券,BSE,BSE,CNY,20200101,A_SHARE
900901.SH,900901,示例B股,SSE,MAIN_SH,USD,19960101,B_SHARE
```

```python
# tests/unit/collectors/test_security_master.py
from pathlib import Path

from hengce.collectors.security_master import OfficialSecurityMasterCsvImporter


def test_importer_keeps_only_in_scope_a_shares() -> None:
    path = Path("tests/fixtures/security_master.csv")
    records = OfficialSecurityMasterCsvImporter().parse(path)
    assert [record.ts_code for record in records] == [
        "000001.SZ",
        "300001.SZ",
        "600000.SH",
        "688001.SH",
    ]
    assert all(record.is_in_scope for record in records)
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/unit/collectors -q`

Expected: FAIL，错误包含 `No module named 'hengce.collectors'`。

- [ ] **Step 4: 实现 Tushare 采集器**

```python
# src/hengce/collectors/tushare.py
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import httpx
from pydantic import SecretStr

from hengce.contracts.enums import QualityStatus
from hengce.contracts.market import MarketBar
from hengce.policy.guard import PolicyGuard


@dataclass(frozen=True)
class DailyFetchResult:
    raw_payload: bytes
    content_type: str
    collected_at: datetime
    bars: list[MarketBar]


class TushareDailyCollector:
    endpoint = "http://api.tushare.pro/"

    def __init__(
        self,
        *,
        client: httpx.Client,
        guard: PolicyGuard,
        token: SecretStr,
        clock: Callable[[], datetime],
    ) -> None:
        self.client = client
        self.guard = guard
        self._token = token
        self.clock = clock

    def fetch(self, trade_date: date) -> DailyFetchResult:
        self.guard.authorize(
            "tushare",
            self.endpoint,
            "market_daily",
            "collectors.tushare",
        )
        request = {
            "api_name": "daily",
            "token": self._token.get_secret_value(),
            "params": {"trade_date": trade_date.strftime("%Y%m%d")},
            "fields": (
                "ts_code,trade_date,open,high,low,close,pre_close,vol,amount"
            ),
        }
        response = self.client.post(self.endpoint, json=request, timeout=30)
        response.raise_for_status()
        raw_payload = response.content
        body = response.json()
        if body.get("code") != 0:
            raise RuntimeError(f"TUSHARE_API_ERROR_{body.get('code')}")
        data = body.get("data") or {}
        fields = data.get("fields") or []
        items = data.get("items") or []
        content_hash = hashlib.sha256(raw_payload).hexdigest()
        collected_at = self.clock()
        bars = [
            self._to_bar(
                dict(zip(fields, row, strict=True)),
                content_hash,
                collected_at,
            )
            for row in items
        ]
        return DailyFetchResult(
            raw_payload=raw_payload,
            content_type=response.headers.get("content-type", "application/json"),
            collected_at=collected_at,
            bars=bars,
        )

    @staticmethod
    def _to_bar(
        row: dict[str, object],
        content_hash: str,
        collected_at: datetime,
    ) -> MarketBar:
        trade_date = datetime.strptime(str(row["trade_date"]), "%Y%m%d").date()
        code = str(row["ts_code"])
        version = f"daily-{trade_date:%Y%m%d}-{content_hash[:12]}"
        return MarketBar(
            record_id=f"{code}-{trade_date:%Y%m%d}-{content_hash[:12]}",
            source_id="tushare",
            source_url="http://api.tushare.pro/",
            collected_at=collected_at,
            version=version,
            content_hash=content_hash,
            license_policy="tushare-daily",
            quality_status=QualityStatus.VALID,
            valid_from=collected_at,
            ts_code=code,
            trade_date=trade_date,
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["close"])),
            pre_close=Decimal(str(row["pre_close"])),
            volume=Decimal(str(row["vol"])),
            amount=Decimal(str(row["amount"])),
        )
```

- [ ] **Step 5: 实现官方证券主数据 CSV 导入**

```python
# src/hengce/collectors/security_master.py
import csv
from datetime import datetime
from pathlib import Path

from hengce.contracts.market import SecurityMaster


class OfficialSecurityMasterCsvImporter:
    allowed_boards = {"MAIN_SH", "STAR", "MAIN_SZ", "CHINEXT"}

    def parse(self, path: Path) -> list[SecurityMaster]:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        records = []
        for row in rows:
            in_scope = (
                row["security_type"] == "A_SHARE"
                and row["board"] in self.allowed_boards
                and row["currency"] == "CNY"
            )
            if not in_scope:
                continue
            records.append(
                SecurityMaster(
                    ts_code=row["ts_code"],
                    symbol=row["symbol"],
                    name=row["name"],
                    exchange=row["exchange"],
                    board=row["board"],
                    currency=row["currency"],
                    list_date=datetime.strptime(row["list_date"], "%Y%m%d").date(),
                    security_type=row["security_type"],
                    is_in_scope=True,
                )
            )
        return sorted(records, key=lambda record: record.ts_code)
```

```python
# src/hengce/collectors/__init__.py
from .security_master import OfficialSecurityMasterCsvImporter
from .tushare import DailyFetchResult, TushareDailyCollector

__all__ = [
    "DailyFetchResult",
    "OfficialSecurityMasterCsvImporter",
    "TushareDailyCollector",
]
```

- [ ] **Step 6: 运行测试**

Run: `python -m pytest tests/unit/collectors -q`

Expected: `2 passed`。

Run: `ruff check src tests`

Expected: `All checks passed!`

- [ ] **Step 7: 提交**

```powershell
git add src/hengce/collectors tests/unit/collectors tests/fixtures/security_master.csv
git commit -m "feat: add compliant market data collectors"
```

## Task 8: 单日行情摄取服务与幂等检查点

**Files:**

- Create: `src/hengce/services/__init__.py`
- Create: `src/hengce/services/market_ingestion.py`
- Create: `tests/integration/test_market_ingestion.py`

**Interfaces:**

- Consumes: `TushareDailyCollector`、`RawObjectStore`、`MarketWarehouse`
- Consumes: `StateRepository`
- Produces: `MarketIngestionService.run(trade_date) -> MarketIngestionResult`
- Checkpoint: `market_daily:{trade_date}` 保存 Parquet 路径

- [ ] **Step 1: 写入失败集成测试**

```python
# tests/integration/test_market_ingestion.py
from datetime import UTC, date, datetime
from pathlib import Path

from hengce.collectors.tushare import DailyFetchResult
from hengce.contracts.enums import QualityStatus
from hengce.contracts.market import MarketBar
from hengce.raw_store.store import RawObjectStore
from hengce.services.market_ingestion import MarketIngestionService
from hengce.state.repository import StateRepository
from hengce.warehouse.market import MarketWarehouse


class FakeCollector:
    calls = 0

    def fetch(self, trade_date: date) -> DailyFetchResult:
        self.calls += 1
        collected_at = datetime(2026, 7, 24, 21, 31, tzinfo=UTC)
        bar = MarketBar(
            record_id="600000.SH-20260724-fixture",
            source_id="tushare",
            source_url="http://api.tushare.pro/",
            collected_at=collected_at,
            version="daily-20260724-fixture",
            content_hash="b" * 64,
            license_policy="tushare-daily",
            quality_status=QualityStatus.VALID,
            valid_from=collected_at,
            ts_code="600000.SH",
            trade_date=trade_date,
            open=10,
            high=11,
            low=9,
            close=10.5,
            pre_close=10,
            volume=1000,
            amount=10500,
        )
        return DailyFetchResult(
            raw_payload=b'{"fixture":true}',
            content_type="application/json",
            collected_at=collected_at,
            bars=[bar],
        )


def test_repeated_run_uses_checkpoint_without_second_fetch(tmp_path: Path) -> None:
    repository = StateRepository(tmp_path / "state.sqlite3")
    repository.migrate()
    collector = FakeCollector()
    service = MarketIngestionService(
        collector=collector,
        raw_store=RawObjectStore(tmp_path / "raw"),
        warehouse=MarketWarehouse(tmp_path / "normalized"),
        state=repository,
    )
    first = service.run(date(2026, 7, 24))
    second = service.run(date(2026, 7, 24))
    assert collector.calls == 1
    assert first == second
    assert first.bar_count == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/integration/test_market_ingestion.py -q`

Expected: FAIL，错误包含 `No module named 'hengce.services'`。

- [ ] **Step 3: 实现摄取服务**

```python
# src/hengce/services/market_ingestion.py
import json
from dataclasses import asdict, dataclass
from datetime import date

from hengce.collectors.tushare import TushareDailyCollector
from hengce.raw_store.store import RawObjectStore
from hengce.state.repository import StateRepository
from hengce.warehouse.market import MarketWarehouse


@dataclass(frozen=True)
class MarketIngestionResult:
    trade_date: str
    bar_count: int
    raw_content_hash: str
    parquet_path: str


class MarketIngestionService:
    def __init__(
        self,
        *,
        collector: TushareDailyCollector,
        raw_store: RawObjectStore,
        warehouse: MarketWarehouse,
        state: StateRepository,
    ) -> None:
        self.collector = collector
        self.raw_store = raw_store
        self.warehouse = warehouse
        self.state = state

    def run(self, trade_date: date) -> MarketIngestionResult:
        checkpoint_key = f"market_daily:{trade_date.isoformat()}"
        existing = self.state.get_checkpoint(checkpoint_key)
        if existing is not None:
            return MarketIngestionResult(**json.loads(existing))
        fetched = self.collector.fetch(trade_date)
        raw = self.raw_store.put(
            source_id="tushare",
            source_url="http://api.tushare.pro/",
            collected_at=fetched.collected_at,
            content_type=fetched.content_type,
            payload=fetched.raw_payload,
        )
        parquet_path = self.warehouse.write_bars(fetched.bars)
        result = MarketIngestionResult(
            trade_date=trade_date.isoformat(),
            bar_count=len(fetched.bars),
            raw_content_hash=raw.content_hash,
            parquet_path=str(parquet_path),
        )
        self.state.save_checkpoint(
            checkpoint_key,
            json.dumps(asdict(result), ensure_ascii=False, sort_keys=True),
        )
        return result
```

```python
# src/hengce/services/__init__.py
from .market_ingestion import MarketIngestionResult, MarketIngestionService

__all__ = ["MarketIngestionResult", "MarketIngestionService"]
```

- [ ] **Step 4: 运行集成测试**

Run: `python -m pytest tests/integration/test_market_ingestion.py -q`

Expected: `1 passed`。

Run: `ruff check src tests`

Expected: `All checks passed!`

- [ ] **Step 5: 提交**

```powershell
git add src/hengce/services tests/integration/test_market_ingestion.py
git commit -m "feat: add idempotent daily market ingestion"
```

## Task 9: 可恢复历史初始化与本地 CLI

**Files:**

- Create: `src/hengce/services/initializer.py`
- Create: `src/hengce/cli.py`
- Create: `tests/unit/services/test_initializer.py`
- Create: `tests/unit/test_cli.py`
- Create: `tests/fixtures/trade_dates.json`
- Modify: `pyproject.toml`

**Interfaces:**

- Consumes: `MarketIngestionService.run(trade_date)`
- Produces: `HistoricalInitializer.run(trade_dates) -> InitializationResult`
- Produces CLI: `hengce init-state`
- Produces CLI: `hengce initialize-history --calendar-file PATH`

- [ ] **Step 1: 写入可恢复初始化失败测试**

```python
# tests/unit/services/test_initializer.py
from datetime import date
from pathlib import Path

import pytest

from hengce.services.initializer import HistoricalInitializer
from hengce.services.market_ingestion import MarketIngestionResult
from hengce.state.repository import StateRepository


class FailingIngestion:
    def __init__(self) -> None:
        self.calls: list[date] = []
        self.fail_once = True

    def run(self, trade_date: date) -> MarketIngestionResult:
        self.calls.append(trade_date)
        if trade_date == date(2026, 7, 23) and self.fail_once:
            self.fail_once = False
            raise RuntimeError("temporary failure")
        return MarketIngestionResult(
            trade_date=trade_date.isoformat(),
            bar_count=1,
            raw_content_hash="c" * 64,
            parquet_path=f"market_bars/trade_date={trade_date.isoformat()}/part.parquet",
        )


def test_initializer_resumes_after_last_completed_date(tmp_path: Path) -> None:
    state = StateRepository(tmp_path / "state.sqlite3")
    state.migrate()
    ingestion = FailingIngestion()
    initializer = HistoricalInitializer(ingestion=ingestion, state=state)
    dates = [date(2026, 7, 22), date(2026, 7, 23), date(2026, 7, 24)]
    with pytest.raises(RuntimeError, match="temporary failure"):
        initializer.run(dates)
    result = initializer.run(dates)
    assert ingestion.calls == [
        date(2026, 7, 22),
        date(2026, 7, 23),
        date(2026, 7, 23),
        date(2026, 7, 24),
    ]
    assert result.completed_dates == 3
    assert result.last_trade_date == "2026-07-24"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/unit/services/test_initializer.py -q`

Expected: FAIL，错误包含 `No module named 'hengce.services.initializer'`。

- [ ] **Step 3: 实现初始化器**

```python
# src/hengce/services/initializer.py
from dataclasses import dataclass
from datetime import date

from hengce.state.repository import StateRepository

from .market_ingestion import MarketIngestionService


@dataclass(frozen=True)
class InitializationResult:
    completed_dates: int
    last_trade_date: str | None


class HistoricalInitializer:
    checkpoint_key = "history:last_completed_trade_date"

    def __init__(
        self,
        *,
        ingestion: MarketIngestionService,
        state: StateRepository,
    ) -> None:
        self.ingestion = ingestion
        self.state = state

    def run(self, trade_dates: list[date]) -> InitializationResult:
        ordered = sorted(set(trade_dates))
        last_completed = self.state.get_checkpoint(self.checkpoint_key)
        pending = [
            item
            for item in ordered
            if last_completed is None or item.isoformat() > last_completed
        ]
        for trade_date in pending:
            self.ingestion.run(trade_date)
            self.state.save_checkpoint(self.checkpoint_key, trade_date.isoformat())
        final = self.state.get_checkpoint(self.checkpoint_key)
        completed = sum(1 for item in ordered if final and item.isoformat() <= final)
        return InitializationResult(
            completed_dates=completed,
            last_trade_date=final,
        )
```

- [ ] **Step 4: 添加 CLI 初始化测试**

```python
# tests/unit/test_cli.py
from pathlib import Path

from typer.testing import CliRunner

from hengce.cli import app


def test_init_state_creates_sqlite_database(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["init-state", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert (tmp_path / "state" / "hengce.sqlite3").exists()
    assert "state initialized" in result.stdout
```

Run: `python -m pytest tests/unit/test_cli.py -q`

Expected: FAIL，错误包含 `No module named 'hengce.cli'`。

- [ ] **Step 5: 实现本地 CLI 基础入口**

```python
# src/hengce/cli.py
import json
from datetime import date, datetime
from pathlib import Path

import typer

from hengce.config import Settings
from hengce.state.repository import StateRepository

app = typer.Typer(no_args_is_help=True)


@app.command("init-state")
def init_state(
    data_dir: Path = typer.Option(Path("data"), file_okay=False),
) -> None:
    settings = Settings(data_dir=data_dir)
    settings.ensure_local_dirs()
    repository = StateRepository(data_dir / "state" / "hengce.sqlite3")
    repository.migrate()
    typer.echo("state initialized")


def load_trade_dates(path: Path) -> list[date]:
    values = json.loads(path.read_text(encoding="utf-8"))
    return [datetime.strptime(value, "%Y-%m-%d").date() for value in values]


if __name__ == "__main__":
    app()
```

Add to `pyproject.toml`:

```toml
[project.scripts]
hengce = "hengce.cli:app"
```

Create `tests/fixtures/trade_dates.json`:

```json
["2026-07-22", "2026-07-23", "2026-07-24"]
```

注：`initialize-history` 命令在 Task 10 的组合根中接线。Task 9 只冻结日期文件格式并验证检查点算法。

- [ ] **Step 6: 运行测试**

Run: `python -m pytest tests/unit/services/test_initializer.py tests/unit/test_cli.py -q`

Expected: `2 passed`。

Run: `ruff check src tests`

Expected: `All checks passed!`

- [ ] **Step 7: 提交**

```powershell
git add pyproject.toml src/hengce/services/initializer.py src/hengce/cli.py tests/unit tests/fixtures/trade_dates.json
git commit -m "feat: add resumable history initialization"
```

## Task 10: 组合根、策略种子和 M1 验收门禁

**Files:**

- Create: `src/hengce/bootstrap.py`
- Create: `config/source_policies.json`
- Create: `tests/integration/test_m1_acceptance.py`
- Create: `docs/runbooks/m1-data-foundation.md`
- Modify: `src/hengce/cli.py`

**Interfaces:**

- Produces: `build_market_ingestion(settings, client) -> MarketIngestionService`
- Produces CLI: `hengce ingest-market --trade-date YYYY-MM-DD`
- Produces CLI: `hengce initialize-history --calendar-file PATH`
- Satisfies: PRD AC-C01、AC-C05、AC-D01、AC-D03、AC-R05

- [ ] **Step 1: 添加已批准来源策略种子**

```json
[
  {
    "source_id": "tushare",
    "source_name": "Tushare",
    "allowed_domains": ["api.tushare.pro"],
    "allowed_schemes": ["http"],
    "allowed_purposes": ["market_daily"],
    "fetch_frequency": "trading_day",
    "full_text_rule": "structured_only",
    "attachment_rule": "none",
    "rate_limit_per_minute": 1,
    "robots_policy": "api_terms",
    "terms_url": "https://tushare.pro/document/1?doc_id=290",
    "terms_reviewed_at": "2026-07-24T00:00:00+08:00",
    "review_status": "APPROVED",
    "connection_status": "UNKNOWN",
    "enabled": true
  },
  {
    "source_id": "sse",
    "source_name": "上海证券交易所",
    "allowed_domains": ["sse.com.cn", "www.sse.com.cn"],
    "allowed_schemes": ["https"],
    "allowed_purposes": ["security_master", "trading_status", "corporate_action", "xbrl", "official_event"],
    "fetch_frequency": "policy_defined",
    "full_text_rule": "necessary_public_attachment",
    "attachment_rule": "pdf_xbrl_only",
    "rate_limit_per_minute": 6,
    "robots_policy": "respect",
    "terms_url": "https://www.sse.com.cn/home/legal/",
    "terms_reviewed_at": "2026-07-24T00:00:00+08:00",
    "review_status": "APPROVED",
    "connection_status": "UNKNOWN",
    "enabled": true
  },
  {
    "source_id": "szse",
    "source_name": "深圳证券交易所",
    "allowed_domains": ["szse.cn", "www.szse.cn"],
    "allowed_schemes": ["https"],
    "allowed_purposes": ["security_master", "trading_status", "corporate_action", "xbrl", "official_event"],
    "fetch_frequency": "policy_defined",
    "full_text_rule": "necessary_public_attachment",
    "attachment_rule": "pdf_xbrl_only",
    "rate_limit_per_minute": 6,
    "robots_policy": "respect",
    "terms_url": "https://www.szse.cn/application/laws/",
    "terms_reviewed_at": "2026-07-24T00:00:00+08:00",
    "review_status": "APPROVED",
    "connection_status": "UNKNOWN",
    "enabled": true
  },
  {
    "source_id": "cninfo",
    "source_name": "巨潮资讯",
    "allowed_domains": ["cninfo.com.cn", "www.cninfo.com.cn"],
    "allowed_schemes": ["https"],
    "allowed_purposes": ["filing", "financial_pdf", "official_event"],
    "fetch_frequency": "policy_defined",
    "full_text_rule": "personal_research_attachment",
    "attachment_rule": "public_filing_only",
    "rate_limit_per_minute": 3,
    "robots_policy": "respect",
    "terms_url": "https://www.cninfo.com.cn/",
    "terms_reviewed_at": "2026-07-24T00:00:00+08:00",
    "review_status": "APPROVED",
    "connection_status": "UNKNOWN",
    "enabled": true
  },
  {
    "source_id": "stats",
    "source_name": "国家统计局",
    "allowed_domains": ["stats.gov.cn", "www.stats.gov.cn"],
    "allowed_schemes": ["https"],
    "allowed_purposes": ["macro_event"],
    "fetch_frequency": "daily",
    "full_text_rule": "facts_summary_link",
    "attachment_rule": "none",
    "rate_limit_per_minute": 6,
    "robots_policy": "respect",
    "terms_url": "https://www.stats.gov.cn/wzgl/202302/t20230217_1912857.html",
    "terms_reviewed_at": "2026-07-24T00:00:00+08:00",
    "review_status": "APPROVED",
    "connection_status": "UNKNOWN",
    "enabled": true
  },
  {
    "source_id": "csrc",
    "source_name": "中国证券监督管理委员会",
    "allowed_domains": ["csrc.gov.cn", "www.csrc.gov.cn"],
    "allowed_schemes": ["https"],
    "allowed_purposes": ["regulatory_event", "investigation", "penalty"],
    "fetch_frequency": "daily",
    "full_text_rule": "facts_summary_link",
    "attachment_rule": "public_attachment_only",
    "rate_limit_per_minute": 6,
    "robots_policy": "respect",
    "terms_url": "https://www.csrc.gov.cn/csrc/c100227/c1362477/content.shtml",
    "terms_reviewed_at": "2026-07-24T00:00:00+08:00",
    "review_status": "APPROVED",
    "connection_status": "UNKNOWN",
    "enabled": true
  }
]
```

- [ ] **Step 2: 写入 M1 组合验收测试**

```python
# tests/integration/test_m1_acceptance.py
from pathlib import Path

from hengce.config import Settings
from hengce.state.repository import StateRepository


def test_m1_bootstrap_migrates_state_and_seeds_six_policies(tmp_path: Path) -> None:
    from hengce.bootstrap import bootstrap_state

    settings = Settings(data_dir=tmp_path)
    repository = bootstrap_state(
        settings,
        Path("config/source_policies.json"),
    )
    assert isinstance(repository, StateRepository)
    assert repository.count_policies() == 6
    assert repository.get_policy("tushare") is not None
```

- [ ] **Step 3: 运行验收测试确认失败**

Run: `python -m pytest tests/integration/test_m1_acceptance.py -q`

Expected: FAIL，错误包含 `No module named 'hengce.bootstrap'`。

- [ ] **Step 4: 增加策略计数和组合根**

Add to `StateRepository`:

```python
def count_policies(self) -> int:
    with connect(self.path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM source_policies"
        ).fetchone()
    return int(row["count"])
```

```python
# src/hengce/bootstrap.py
import json
from pathlib import Path

from hengce.config import Settings
from hengce.contracts.policy import SourcePolicy
from hengce.state.repository import StateRepository


def bootstrap_state(
    settings: Settings,
    policy_file: Path,
) -> StateRepository:
    settings.ensure_local_dirs()
    repository = StateRepository(settings.data_dir / "state" / "hengce.sqlite3")
    repository.migrate()
    payload = json.loads(policy_file.read_text(encoding="utf-8"))
    for item in payload:
        repository.upsert_policy(SourcePolicy.model_validate(item))
    return repository
```

- [ ] **Step 5: 完成 CLI 接线**

Replace `src/hengce/cli.py` with the complete M1 command surface:

```python
import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import typer
from pydantic import SecretStr

from hengce.bootstrap import bootstrap_state
from hengce.collectors.tushare import TushareDailyCollector
from hengce.config import Settings
from hengce.policy.guard import PolicyGuard
from hengce.raw_store.store import RawObjectStore
from hengce.services.initializer import HistoricalInitializer
from hengce.services.market_ingestion import MarketIngestionService
from hengce.state.repository import StateRepository
from hengce.warehouse.market import MarketWarehouse

app = typer.Typer(no_args_is_help=True)


def load_trade_dates(path: Path) -> list[date]:
    values = json.loads(path.read_text(encoding="utf-8"))
    return [datetime.strptime(value, "%Y-%m-%d").date() for value in values]


def build_ingestion(settings: Settings, policy_file: Path) -> MarketIngestionService:
    state = bootstrap_state(settings, policy_file)
    if settings.tushare_token is None:
        raise typer.BadParameter("HENGCE_TUSHARE_TOKEN is required")
    collector = TushareDailyCollector(
        client=httpx.Client(),
        guard=PolicyGuard(state),
        token=SecretStr(settings.tushare_token.get_secret_value()),
        clock=lambda: datetime.now(ZoneInfo(settings.timezone)),
    )
    return MarketIngestionService(
        collector=collector,
        raw_store=RawObjectStore(settings.data_dir / "raw"),
        warehouse=MarketWarehouse(settings.data_dir / "normalized"),
        state=state,
    )


@app.command("init-state")
def init_state(
    data_dir: Path = typer.Option(Path("data"), file_okay=False),
    policy_file: Path = typer.Option(Path("config/source_policies.json")),
) -> None:
    bootstrap_state(Settings(data_dir=data_dir), policy_file)
    typer.echo("state initialized")


@app.command("ingest-market")
def ingest_market(
    trade_date: date = typer.Option(...),
    data_dir: Path = typer.Option(Path("data"), file_okay=False),
    policy_file: Path = typer.Option(Path("config/source_policies.json")),
) -> None:
    settings = Settings(data_dir=data_dir)
    result = build_ingestion(settings, policy_file).run(trade_date)
    typer.echo(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))


@app.command("initialize-history")
def initialize_history(
    calendar_file: Path = typer.Option(..., exists=True, dir_okay=False),
    data_dir: Path = typer.Option(Path("data"), file_okay=False),
    policy_file: Path = typer.Option(Path("config/source_policies.json")),
) -> None:
    settings = Settings(data_dir=data_dir)
    ingestion = build_ingestion(settings, policy_file)
    state = StateRepository(data_dir / "state" / "hengce.sqlite3")
    result = HistoricalInitializer(ingestion=ingestion, state=state).run(
        load_trade_dates(calendar_file)
    )
    typer.echo(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    app()
```

- [ ] **Step 6: 编写运行手册**

```markdown
# M1 数据底座运行手册

## 初始化

1. 创建 Python 3.12 虚拟环境。
2. 运行 `python -m pip install -e ".[dev]"`。
3. 复制 `.env.example` 为 `.env`，在本地填写 `HENGCE_TUSHARE_TOKEN`。
4. 运行 `hengce init-state --data-dir data`。

## 单日摄取

运行：

`hengce ingest-market --trade-date 2026-07-24 --data-dir data`

成功标准：

- `data/raw/tushare/` 出现一个按 SHA-256 定位的原始对象；
- `data/normalized/market_bars/trade_date=2026-07-24/` 出现 Parquet；
- SQLite 存在 `market_daily:2026-07-24` 检查点；
- 再次执行不发出第二次请求。

## 历史初始化

准备经官方来源确认的交易日 JSON 数组，然后运行：

`hengce initialize-history --calendar-file approved-trade-dates.json --data-dir data`

任务中断后重复相同命令，从最后成功交易日继续。

## 安全

不要提交 `.env`、`data/`、SQLite、DuckDB、Parquet、原始响应或附件。遇到 401、403、429、验证码或政策拒绝时停止，不更换来源或规避限制。
```

- [ ] **Step 7: 运行完整 M1 验收**

Run: `python -m pytest tests/unit tests/integration -q`

Expected: 所有测试通过，0 failures。

Run: `ruff check src tests`

Expected: `All checks passed!`

Run: `git status --short`

Expected: 只包含 Task 10 计划内文件。

- [ ] **Step 8: 提交**

```powershell
git add config/source_policies.json src/hengce/bootstrap.py src/hengce/cli.py src/hengce/state/repository.py tests/integration/test_m1_acceptance.py docs/runbooks/m1-data-foundation.md
git commit -m "feat: complete M1 data foundation"
```

## M1 完成门禁

在进入 M2 前必须运行：

```powershell
python -m pytest tests/unit tests/integration -q
ruff check src tests
git diff --check
```

必须确认：

- 非白名单域名在网络前被拒绝；
- Tushare 单日全市场只有一个请求；
- Token 未进入 Git、日志、SQLite 和测试输出；
- 相同原始内容只保存一次；
- 相同交易日重跑不重复请求或重复 Parquet；
- 证券主数据只保留四类范围内 A 股；
- 历史初始化中断后从最后成功交易日恢复；
- SQLite、Parquet 和原始对象路径均位于配置的数据目录；
- `git status --short` 为空；
- Critical 和 Important 代码审查问题为 0。
