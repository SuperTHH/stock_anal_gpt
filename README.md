# 衡策 A 股长期研究台

衡策是一个本地、只读、面向个人非商业研究的 A 股数据与分析工具。系统按“当时可知”
原则保存来源、发布时间、采集时间、版本与质量状态，并分别生成质量成长、低估值价值和
稳定高股息三个独立候选池。它不生成自动买入指令，也不构成证券投资建议。

当前真实数据最小闭环使用 2026-07-22 市场截面和固定 30 只分板块流动性试点样本。
真实附件、SQLite、Parquet、Raw、报告和 Token 只保留在本机，禁止提交到 Git。

## 快速入口

- [真实数据最小闭环操作手册](docs/runbooks/real-data-minimal-loop.md)
- [产品需求文档](docs/product/2026-07-24-a-share-long-term-research-platform-prd.md)
- [真实数据闭环设计](docs/superpowers/specs/2026-07-30-real-data-minimal-loop-design.md)
- [本地 XBRL 操作手册](docs/runbooks/xbrl-financial-facts.md)

## 常用验证

在仓库根目录执行：

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
git diff --check
```

前端验证：

```powershell
Set-Location apps/web
npm.cmd test -- --run
npm.cmd run build
```

真实闭环的初始化、人工收件箱、断点续跑、本地页面和聚合验收命令以操作手册为准。
