import { useEffect, useMemo, useState } from "react";

import { loadLatestReport } from "./api";
import { DEMO_REPORT } from "./demo";
import type { Candidate, ReportPayload, StrategyType } from "./types";
import "./styles.css";

type Page = "每日研究总览" | "策略候选池" | "个股研究" | "官方事件流" | "数据质量与来源";

const pages: Page[] = [
  "每日研究总览",
  "策略候选池",
  "个股研究",
  "官方事件流",
  "数据质量与来源",
];

const strategyNames: Record<StrategyType, string> = {
  QUALITY_GROWTH: "质量成长合理估值",
  DEEP_VALUE: "低估值价值",
  STABLE_DIVIDEND: "稳定高股息",
};

function candidateCount(report: ReportPayload, strategy: StrategyType): number {
  return report.candidate_pools[strategy]?.length ?? 0;
}

function StatusStrip({ report }: { report: ReportPayload }) {
  const domains = Object.values(report.data_domain_statuses);
  const ready = domains.every((status) => status === "VALID" || status === "DERIVED");
  return (
    <section className={`status-strip ${ready ? "" : "status-warning"}`}>
      <strong>{ready ? "报告数据完整，可用于研究" : "数据未更新或存在质量阻断"}</strong>
      <span>状态 {report.snapshot.report_status}</span>
      <span>数据域 {domains.length} 项</span>
    </section>
  );
}

function Overview({ report, goTo }: { report: ReportPayload; goTo: (page: Page) => void }) {
  return (
    <>
      <header className="page-header">
        <div>
          <p className="eyebrow">收市后研究报告</p>
          <h1>每日研究总览</h1>
        </div>
        <button className="outline-button" onClick={() => goTo("数据质量与来源")}>
          查看来源与质量
        </button>
      </header>
      <StatusStrip report={report} />
      <section className="kpi-row" aria-label="报告概况">
        <div><span>报告日期</span><strong>{report.snapshot.report_date}</strong></div>
        <div><span>数据截至</span><strong>{report.snapshot.market_cutoff_at.slice(0, 16)}</strong></div>
        <div><span>策略候选</span><strong>{Object.values(report.candidate_pools).flat().length}</strong></div>
        <div><span>官方事件</span><strong>{report.official_events?.length ?? 0}</strong></div>
      </section>
      <section className="section-block">
        <div className="section-heading">
          <div>
            <p className="eyebrow">独立排名，不跨策略比较</p>
            <h2>三个策略池</h2>
          </div>
          <button className="text-button" onClick={() => goTo("策略候选池")}>打开候选池</button>
        </div>
        <div className="strategy-grid">
          {(Object.keys(strategyNames) as StrategyType[]).map((strategy) => (
            <article className="strategy-card" key={strategy}>
              <p className="mono">{strategy}</p>
              <h3>{strategyNames[strategy]}</h3>
              <strong className="large-number">{candidateCount(report, strategy)}</strong>
              <span>只候选 / 观察标的</span>
              <div className="rule" />
              <p>仅显示本策略因子与排名，不产生统一总分或自动买入指令。</p>
            </article>
          ))}
        </div>
      </section>
    </>
  );
}

function CandidateTable({
  candidates,
  onSelect,
}: {
  candidates: Candidate[];
  onSelect: (candidate: Candidate) => void;
}) {
  if (!candidates.length) {
    return <div className="empty-panel">本报告在该策略下没有候选标的。</div>;
  }
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr><th>策略内名次</th><th>标的</th><th>代码</th><th>状态</th><th>入选理由</th><th>风险</th><th>完整度</th><th>策略分</th></tr>
        </thead>
        <tbody>
          {candidates.map((candidate) => (
            <tr key={candidate.ts_code} onClick={() => onSelect(candidate)}>
              <td className="numeric mono">{candidate.rank_in_strategy}</td>
              <td>{candidate.security_name ?? "名称数据不足"}</td>
              <td className="mono">{candidate.ts_code}</td>
              <td>{candidate.candidate_status === "CANDIDATE" ? "候选" : "观察"}</td>
              <td>{candidate.selection_reasons.join("；")}</td>
              <td>{candidate.risk_flags.join("；") || "暂无结构化风险标记"}</td>
              <td className="numeric mono">{Math.round(Number(candidate.data_completeness) * 100)}%</td>
              <td className="numeric mono">{candidate.strategy_score}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StrategyPools({
  report,
  openSecurity,
}: {
  report: ReportPayload;
  openSecurity: (candidate: Candidate) => void;
}) {
  const [strategy, setStrategy] = useState<StrategyType>("QUALITY_GROWTH");
  return (
    <>
      <header className="page-header">
        <div><p className="eyebrow">策略内独立排序</p><h1>策略候选池</h1></div>
      </header>
      <StatusStrip report={report} />
      <div className="tabs" role="tablist" aria-label="策略选择">
        {(Object.keys(strategyNames) as StrategyType[]).map((item) => (
          <button
            role="tab"
            aria-selected={strategy === item}
            className={strategy === item ? "active" : ""}
            key={item}
            onClick={() => setStrategy(item)}
          >
            {strategyNames[item]} <span className="mono">{candidateCount(report, item)}</span>
          </button>
        ))}
      </div>
      <CandidateTable candidates={report.candidate_pools[strategy] ?? []} onSelect={openSecurity} />
    </>
  );
}

function SecurityResearch({ report, selected }: { report: ReportPayload; selected: Candidate | null }) {
  const fallback = Object.values(report.candidate_pools).flat()[0] ?? null;
  const candidate = selected ?? fallback;
  return (
    <>
      <header className="page-header">
        <div><p className="eyebrow">已发布报告内的结构化研究</p><h1>个股研究</h1></div>
      </header>
      {!candidate ? <div className="empty-panel">当前报告没有可展示的个股候选。</div> : (
        <div className="research-grid">
          <section className="section-block">
            <h2>{candidate.security_name ?? "名称数据不足"}</h2>
            <p className="mono">{candidate.ts_code}</p>
            <h3>核心研究证据</h3>
            <dl className="evidence-list">
              <div><dt>入选理由</dt><dd>{candidate.selection_reasons.join("；")}</dd></div>
              <div><dt>半年以上催化剂</dt><dd>{candidate.catalysts?.join("；") || "数据不足"}</dd></div>
              <div><dt>主要风险</dt><dd>{candidate.risk_flags.join("；") || "暂无结构化风险标记"}</dd></div>
              <div><dt>观察条件</dt><dd>{candidate.observe_conditions?.join("；") || "数据不足"}</dd></div>
              <div><dt>失效条件</dt><dd>{candidate.invalidate_conditions?.join("；") || "数据不足"}</dd></div>
            </dl>
          </section>
          <aside className="section-block">
            <h2>因子明细</h2>
            {candidate.factor_details.length ? candidate.factor_details.map((factor) => (
              <div className="factor-row" key={factor.factor_name}>
                <span>{factor.factor_name}</span>
                <strong className="mono">{factor.normalized_score ?? "数据不足"}</strong>
              </div>
            )) : <p>该报告未附加可展示的因子明细。</p>}
          </aside>
        </div>
      )}
    </>
  );
}

function Events({ report }: { report: ReportPayload }) {
  const events = report.official_events ?? [];
  return (
    <>
      <header className="page-header"><div><p className="eyebrow">仅官方来源</p><h1>官方事件流</h1></div></header>
      {events.length ? events.map((event, index) => (
        <article className="event-row" key={event.event_id ?? index}>
          <p className="eyebrow">{event.institution ?? "官方机构"}</p>
          <h2>{event.title ?? "未命名事件"}</h2>
          <p>{event.factual_summary ?? "暂无事实摘要"}</p>
          <div className="event-meta">
            <span>影响对象 {event.affected_ts_codes?.join("、") || "市场整体"}</span>
            <span>影响周期 {event.impact_horizon ?? "待评估"}</span>
            <span>置信度 {event.confidence ?? "待评估"}</span>
            <span>发布于 {event.published_at?.replace("T", " ").slice(0, 16) ?? "未知"}</span>
          </div>
          {event.source_url && (
            <a href={event.source_url} target="_blank" rel="noreferrer">
              打开{event.institution}来源站点
            </a>
          )}
        </article>
      )) : <div className="empty-panel">本报告截止时间前暂无新增官方事件。</div>}
    </>
  );
}

function Quality({ report }: { report: ReportPayload }) {
  return (
    <>
      <header className="page-header"><div><p className="eyebrow">可追溯与可复现</p><h1>数据质量与来源</h1></div></header>
      <section className="section-block">
        <h2>数据域状态</h2>
        <div className="quality-list">
          {Object.entries(report.data_domain_statuses).map(([domain, status]) => (
            <div key={domain}><span className="mono">{domain}</span><strong>{status}</strong></div>
          ))}
        </div>
      </section>
      <section className="section-block">
        <h2>来源与采集谱系</h2>
        {report.source_records?.length ? (
          <div className="source-grid">
            {report.source_records.map((source) => (
              <article className="source-card" key={source.record_id}>
                <div className="source-card-heading">
                  <div>
                    <p className="eyebrow">{source.domain}</p>
                    <h3>{source.source_name}</h3>
                  </div>
                  <strong>{source.quality_status}</strong>
                </div>
                <dl>
                  <div><dt>版本</dt><dd className="mono">{source.version}</dd></div>
                  <div><dt>发布时间</dt><dd>{source.published_at?.replace("T", " ").slice(0, 16) ?? "不适用"}</dd></div>
                  <div><dt>采集时间</dt><dd>{source.collected_at.replace("T", " ").slice(0, 16)}</dd></div>
                  <div><dt>许可策略</dt><dd>{source.license_policy}</dd></div>
                </dl>
                <a href={source.source_url} target="_blank" rel="noreferrer">打开来源</a>
              </article>
            ))}
          </div>
        ) : (
          <div className="empty-panel">本报告未附加可展示的来源级谱系。</div>
        )}
      </section>
    </>
  );
}

export default function App() {
  const demoMode = new URLSearchParams(window.location.search).get("demo") === "1";
  const [report, setReport] = useState<ReportPayload | null>(demoMode ? DEMO_REPORT : null);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState<Page>("每日研究总览");
  const [selected, setSelected] = useState<Candidate | null>(null);

  useEffect(() => {
    if (demoMode) return;
    loadLatestReport().then(setReport).catch((reason: Error) => setError(reason.message));
  }, [demoMode]);

  const content = useMemo(() => {
    if (!report) return null;
    if (page === "策略候选池") {
      return <StrategyPools report={report} openSecurity={(candidate) => { setSelected(candidate); setPage("个股研究"); }} />;
    }
    if (page === "个股研究") return <SecurityResearch report={report} selected={selected} />;
    if (page === "官方事件流") return <Events report={report} />;
    if (page === "数据质量与来源") return <Quality report={report} />;
    return <Overview report={report} goTo={setPage} />;
  }, [page, report, selected]);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><strong>衡策</strong><span>A股长期研究台</span></div>
        <nav aria-label="主导航">
          {pages.map((item) => (
            <button className={page === item ? "active" : ""} key={item} onClick={() => setPage(item)}>
              <span className="nav-mark" aria-hidden="true">{item.slice(0, 1)}</span><span className="nav-label">{item}</span>
            </button>
          ))}
        </nav>
        <p className="sidebar-meta">个人研究 · 非投资建议</p>
      </aside>
      <main>
        {demoMode && (
          <div className="demo-watermark">功能演示数据 · 虚构标的 · 非实时</div>
        )}
        {report && (
          <div className="utility-row">
            <span>{report.snapshot.report_date}</span>
            <span className="mono">{report.snapshot.report_id}</span>
            <span>数据截至 {report.snapshot.market_cutoff_at.slice(0, 16)}</span>
          </div>
        )}
        {!report && !error && <div className="empty-panel">正在读取已发布报告…</div>}
        {error && (
          <div className="empty-panel error-state">
            <h1>暂无已发布报告</h1>
            <p>系统不会用示例候选替代真实报告。请等待下一次完整任务成功发布。</p>
          </div>
        )}
        {content}
        {report && <footer>数据截止 {report.snapshot.market_cutoff_at} · 仅供个人研究，不构成投资建议</footer>}
      </main>
    </div>
  );
}
