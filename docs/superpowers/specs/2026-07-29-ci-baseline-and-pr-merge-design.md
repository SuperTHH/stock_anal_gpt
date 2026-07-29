# CI 基线与 PR #2 合并设计

## 1. 目标

为当前 A 股中长期研究平台建立可重复、无真实网络依赖的 GitHub Actions
质量门禁，并在门禁通过后将 PR #2 以 Squash merge 方式合并到
`codex/market-ui-refresh`，形成真实数据最小闭环开发的稳定基线。

## 2. 范围

本次只包含：

- Python 3.12 在 Windows 和 Ubuntu 上的安装与全量测试；
- Ubuntu 上的 Ruff 静态检查；
- Node.js 22 上的前端依赖安装、测试和生产构建；
- PR #2 的检查核验、Squash merge 和合并后验证；
- 保留当前功能分支和工作树，直至合并后验证完成。

本次不包含：

- 真实 Tushare、交易所或巨潮网络烟雾测试；
- Token、Cookie、财报附件、数据库或 Parquet 上传；
- 自动部署、云端服务或公共 API；
- 真实数据连接器实现；
- GitHub 分支保护规则的管理。

## 3. 方案选择

采用双系统 CI：

- Windows 是本产品的目标运行环境，必须执行全量 Python 测试；
- Ubuntu 执行相同的 Python 测试，以覆盖 Windows 环境无法创建测试
  symlink 而跳过的安全用例；
- Ruff 和前端任务只在 Ubuntu 执行一次，减少重复耗时；
- 不增加 Python 3.11、3.13 或更多 Node.js 版本矩阵，因为项目契约已经固定
  Python 3.12，前端只需要一个受支持的 Node.js LTS 基线。

## 4. 工作流设计

新增 `.github/workflows/ci.yml`。

触发规则：

- 所有 Pull Request；
- 推送到 `main`；
- 推送到 `codex/market-ui-refresh`；
- 支持手动 `workflow_dispatch`。

最小权限：

```yaml
permissions:
  contents: read
```

并发规则以工作流名称和 Git ref 分组，新提交取消同一分支尚未完成的旧任务。

### 4.1 Python 测试任务

使用矩阵：

```yaml
os: [ubuntu-latest, windows-latest]
python-version: ["3.12"]
```

每个系统执行：

1. `actions/checkout`；
2. `actions/setup-python`，启用 pip 缓存；
3. `python -m pip install --upgrade pip`；
4. `python -m pip install -e ".[dev]"`；
5. `python -m pytest`。

测试不得配置 Tushare Token，不得访问真实网络。所有需要来源响应的用例必须使用
仓库内固定夹具或注入的本地传输。

### 4.2 静态检查任务

Ubuntu + Python 3.12，安装 `.[dev]` 后执行：

```text
python -m ruff check .
git diff --check
```

`git diff --check` 用于阻止尾随空格和冲突标记进入基线。

### 4.3 前端任务

Ubuntu + Node.js 22，工作目录为 `apps/web`，执行：

```text
npm ci
npm test
npm run build
```

`npm ci` 必须严格使用已提交的 `package-lock.json`。构建产物不得提交到 Git。

### 4.4 超时和缓存

- 每个任务设置 `timeout-minutes: 30`；
- Python 使用 `actions/setup-python` 的 pip 缓存；
- 前端使用 `actions/setup-node` 的 npm 缓存，并将
  `cache-dependency-path` 指向 `apps/web/package-lock.json`；
- CI 不缓存运行数据库、下载附件、Raw Store 或报告工件。

## 5. 合并门槛

PR #2 只有同时满足以下条件才允许合并：

1. Windows Python 全量测试成功；
2. Ubuntu Python 全量测试成功，且 symlink 安全测试未被平台原因跳过；
3. Ruff 和 `git diff --check` 成功；
4. 前端测试和生产构建成功；
5. PR head SHA 与本地、远端功能分支 SHA 一致；
6. PR 处于 `mergeable_state=clean`；
7. 没有未解决的评审意见；
8. 没有 Token、Cookie、数据库、Parquet、下载附件或真实原始响应进入 Git。

若任一门槛失败，不合并；先在当前功能分支通过 TDD 修复，再重新运行全部检查。

## 6. 合并方式

采用 Squash merge：

- 目标分支：`codex/market-ui-refresh`；
- 来源分支：`codex/xbrl-financial-facts`；
- Squash 提交标题使用完整产品范围，而不是只描述早期 XBRL 内核；
- Squash 提交正文保留财务事实、公司行动、三策略池、报告发布、本地 UI、
  测试结果和合规边界摘要。

合并后暂不删除来源分支和当前工作树。先拉取目标分支并重新验证：

- 合并提交存在；
- 全量 Python 测试成功；
- 前端测试和生产构建成功；
- Ruff 与 `git diff --check` 成功。

确认基线稳定后，再从更新后的目标分支创建真实数据最小闭环的独立功能分支。

## 7. 故障处理

- GitHub Actions 安装依赖失败：保留失败日志，不切换不明镜像，不放宽依赖上限；
- Windows 与 Ubuntu 结果不一致：视为真实兼容性问题，阻断合并；
- 测试意外访问网络：修改测试或依赖注入边界，不在 CI 中加入真实凭据；
- PR head 在检查期间变化：重新读取 head SHA，并重新等待该 SHA 的检查；
- 合并 API 或网页失败：保持 PR 开放，不修改目标分支，核对权限和网络后重试；
- 合并后验证失败：不删除来源分支或工作树，记录失败并从合并提交修复。

## 8. 验收证据

最终交付必须记录：

- 工作流文件路径和提交 SHA；
- 每个 GitHub Actions job 的状态与链接；
- PR #2 的最终 head SHA；
- Squash merge 提交 SHA；
- 合并后本地全量验证命令和通过数量；
- 目标分支与远端一致、工作树干净的 Git 证据。
