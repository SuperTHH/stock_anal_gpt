import { useEffect, useMemo, useState } from "react";

import { loadLatestReport } from "./api";
import type {
  Candidate,
  FactorDetail,
  PoolReadiness,
  ReportPayload,
  ReportSource,
  StrategyType,
} from "./types";
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

const strategyOrder = Object.keys(strategyNames) as StrategyType[];

const factorNames: Record<string, string> = {
  capital_return: "资本回报",
  growth_quality: "成长质量",
  cash_flow_quality: "现金流质量",
  profitability_stability: "盈利稳定性",
  balance_sheet_quality: "资产负债表质量",
  valuation_attractiveness: "估值吸引力",
  absolute_valuation: "绝对估值",
  relative_valuation: "相对估值",
  asset_quality: "资产质量",
  cash_debt_quality: "现金与债务质量",
  cycle_position: "周期位置",
  value_trap_safety: "价值陷阱安全性",
  dividend_yield: "股息率",
  dividend_continuity: "分红连续性",
  payout_sustainability: "派息可持续性",
  cashflow_coverage: "现金流覆盖",
  dividend_cut_safety: "削减分红安全性",
};

const percentageFactorValues = new Set([
  "cycle_position",
  "dividend_yield",
  "dividend_continuity",
  "payout_sustainability",
  "dividend_cut_safety",
]);

const fourDecimalFactorValues = new Set([
  "cash_flow_quality",
  "profitability_stability",
  "cashflow_coverage",
]);

type SecuritySelection = {
  tsCode: string;
  strategy: StrategyType;
};

function fixedNumber(value: string | null | undefined, digits = 2): string {
  if (value === null || value === undefined || value === "") return "数据不足";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : "数据不足";
}

function formatFactorValue(factor: FactorDetail): string {
  if (factor.raw_value === null) return "数据不足";
  if (percentageFactorValues.has(factor.factor_name)) {
    const parsed = Number(factor.raw_value);
    return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(2)}%` : "数据不足";
  }
  return fixedNumber(
    factor.raw_value,
    fourDecimalFactorValues.has(factor.factor_name) ? 4 : 2,
  );
}

function normalizationScopeName(scope: string): string {
  if (scope === "pilot_universe") return "30只试点样本";
  if (scope === "demo-market") return "演示样本";
  if (scope === "unavailable") return "不可用";
  return scope;
}

function uniqueSecurities(report: ReportPayload): Candidate[] {
  const securities = new Map<string, Candidate>();
  strategyOrder.forEach((strategy) => {
    (report.candidate_pools[strategy] ?? []).forEach((candidate) => {
      if (!securities.has(candidate.ts_code)) securities.set(candidate.ts_code, candidate);
    });
  });
  return [...securities.values()];
}

function strategyCandidate(
  report: ReportPayload,
  tsCode: string,
  strategy: StrategyType,
): Candidate | null {
  return report.candidate_pools[strategy]?.find((candidate) => candidate.ts_code === tsCode) ?? null;
}

function candidateStrategies(report: ReportPayload, tsCode: string): StrategyType[] {
  return strategyOrder.filter((strategy) => strategyCandidate(report, tsCode, strategy));
}

function deduplicatedSources(
  factor: FactorDetail,
  sourceMap: Map<string, ReportSource>,
): Array<{ source: ReportSource; recordCount: number }> {
  const grouped = new Map<string, { source: ReportSource; recordCount: number }>();
  factor.source_record_ids.forEach((recordId) => {
    const source = sourceMap.get(recordId);
    if (!source) return;
    const existing = grouped.get(source.source_url);
    if (existing) existing.recordCount += 1;
    else grouped.set(source.source_url, { source, recordCount: 1 });
  });
  return [...grouped.values()];
}

function candidateCount(report: ReportPayload, strategy: StrategyType): number {
  return report.candidate_pools[strategy]?.length ?? 0;
}

function formatDateTime(value?: string | null): string {
  return value ? value.replace("T", " ").replace("Z", "").slice(0, 16) : "未记录";
}

function coveragePercent(value: string): string {
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function PilotContextBanner({ report }: { report: ReportPayload }) {
  if (!report.snapshot.is_historical_reconstruction) return null;
  const cutoff = report.report_cutoff_at ?? report.snapshot.report_cutoff_at;
  const generated =
    report.generation_started_at ??
    report.snapshot.generation_started_at ??
    report.snapshot.generated_at;
  return (
    <section className="pilot-banner" aria-label="历史重建范围">
      <div>
        <strong>真实数据 · 30只试点样本 · 历史重建</strong>
        <span>排名只在 30 只试点样本内有效</span>
      </div>
      <div className="pilot-times">
        <span>报告截止 {formatDateTime(cutoff)}</span>
        <span>实际生成 {formatDateTime(generated)}</span>
      </div>
    </section>
  );
}

function ReadinessBadge({ readiness }: { readiness?: PoolReadiness }) {
  if (!readiness) return null;
  return (
    <div className="readiness-summary">
      <strong className={`readiness-badge ${readiness.status.toLowerCase()}`}>
        {readiness.status}
      </strong>
      <span className="mono">{readiness.complete_factor_count} / {readiness.universe_size}</span>
      <span className="mono">{coveragePercent(readiness.coverage_ratio)}</span>
    </div>
  );
}

function StatusStrip({ report }: { report: ReportPayload }) {
  const domains = Object.values(report.data_domain_statuses);
  const pools = Object.values(report.pool_readiness ?? {});
  const strategyReady =
    report.strategy_research_status === "READY" ||
    (report.strategy_research_status === undefined &&
      pools.length > 0 && pools.every((pool) => pool?.status === "READY"));
  const dataComplete =
    report.data_completeness_status === "COMPLETE" ||
    (report.data_completeness_status === undefined &&
      domains.every((status) => status === "VALID" || status === "DERIVED"));
  return (
    <section className={`status-strip ${strategyReady && dataComplete ? "" : "status-warning"}`}>
      <strong>{strategyReady ? "策略研究可用" : "策略研究暂不可用"}</strong>
      <span>{dataComplete ? "全部数据域完整" : "部分数据域待完善"}</span>
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
              <ReadinessBadge readiness={report.pool_readiness?.[strategy]} />
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
  readiness,
}: {
  candidates: Candidate[];
  onSelect: (candidate: Candidate) => void;
  readiness?: PoolReadiness;
}) {
  if (readiness?.status === "BLOCKED") {
    const missing = Object.entries(readiness.missing_by_security);
    return (
      <section className="blocked-panel">
        <p className="eyebrow">策略池状态 BLOCKED</p>
        <h2>该策略池暂不发布候选</h2>
        <p>
          完整因子 {readiness.complete_factor_count} / {readiness.universe_size}，
          覆盖率 {coveragePercent(readiness.coverage_ratio)}，低于要求
          {" "}{coveragePercent(readiness.required_coverage_ratio)}。
        </p>
        <div className="blocked-grid">
          <div>
            <h3>阻断代码</h3>
            <ul className="code-list">
              {readiness.blocking_codes.map((code) => <li className="mono" key={code}>{code}</li>)}
            </ul>
          </div>
          <div>
            <h3>缺失证券与因子</h3>
            <ul className="missing-list">
              {missing.map(([tsCode, factors]) => (
                <li key={tsCode}>
                  <strong className="mono">{tsCode}</strong>
                  {factors.map((factor) => <span className="mono" key={factor}>{factor}</span>)}
                </li>
              ))}
            </ul>
          </div>
        </div>
      </section>
    );
  }
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
  openSecurity: (candidate: Candidate, strategy: StrategyType) => void;
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
            {report.pool_readiness?.[item] && (
              <span className={`tab-status ${report.pool_readiness[item]?.status.toLowerCase()}`}>
                {report.pool_readiness[item]?.status}
              </span>
            )}
          </button>
        ))}
      </div>
      <CandidateTable
        candidates={report.candidate_pools[strategy] ?? []}
        onSelect={(candidate) => openSecurity(candidate, strategy)}
        readiness={report.pool_readiness?.[strategy]}
      />
    </>
  );
}

function SecurityResearch({
  report,
  selected,
  onSelect,
}: {
  report: ReportPayload;
  selected: SecuritySelection | null;
  onSelect: (selection: SecuritySelection) => void;
}) {
  const securities = uniqueSecurities(report);
  const fallback = securities[0] ?? null;
  const tsCode = selected && securities.some((item) => item.ts_code === selected.tsCode)
    ? selected.tsCode
    : fallback?.ts_code ?? "";
  const availableStrategies = candidateStrategies(report, tsCode);
  const strategy = selected && availableStrategies.includes(selected.strategy)
    ? selected.strategy
    : availableStrategies[0] ?? strategyOrder[0];
  const candidate = strategyCandidate(report, tsCode, strategy);
  const sources = new Map<string, ReportSource>(
    (report.source_records ?? []).map((source) => [source.record_id, source]),
  );

  function selectSecurity(nextTsCode: string) {
    const nextStrategies = candidateStrategies(report, nextTsCode);
    onSelect({
      tsCode: nextTsCode,
      strategy: nextStrategies.includes(strategy) ? strategy : nextStrategies[0] ?? strategy,
    });
  }

  return (
    <>
      <header className="page-header">
        <div><p className="eyebrow">已发布报告内的结构化研究</p><h1>个股研究</h1></div>
        {securities.length > 0 && (
          <label className="security-picker">
            <span>股票选择（{securities.length}只）</span>
            <select value={tsCode} onChange={(event) => selectSecurity(event.target.value)}>
              {securities.map((security) => (
                <option key={security.ts_code} value={security.ts_code}>
                  {security.security_name ?? "名称数据不足"} · {security.ts_code}
                </option>
              ))}
            </select>
          </label>
        )}
      </header>
      {!candidate ? <div className="empty-panel">当前报告没有可展示的个股候选。</div> : (
        <>
          <div className="tabs research-tabs" role="tablist" aria-label="个股策略选择">
            {strategyOrder.map((item) => {
              const strategyItem = strategyCandidate(report, tsCode, item);
              return (
                <button
                  role="tab"
                  aria-label={strategyNames[item]}
                  aria-selected={strategy === item}
                  className={strategy === item ? "active" : ""}
                  disabled={!strategyItem}
                  key={item}
                  onClick={() => onSelect({ tsCode, strategy: item })}
                >
                  {strategyNames[item]}
                  <span className="strategy-membership" aria-hidden="true">
                    {strategyItem ? `第 ${strategyItem.rank_in_strategy} 名` : "未入选"}
                  </span>
                </button>
              );
            })}
          </div>
          <section className="research-summary" aria-label="当前策略评分">
            <strong>{strategyNames[strategy]}</strong>
            <span>策略内排名 {candidate.rank_in_strategy} / {report.candidate_pools[strategy]?.length ?? 0}</span>
            <span>策略得分 {fixedNumber(candidate.strategy_score)}</span>
            <span>状态 {candidate.candidate_status === "CANDIDATE" ? "候选" : "观察"}</span>
            <span>数据完整度 {fixedNumber(String(Number(candidate.data_completeness) * 100))}%</span>
          </section>
          <div className="research-grid">
            <section className="section-block research-evidence">
              <div className="security-heading">
                <div>
                  <h2>{candidate.security_name ?? "名称数据不足"}</h2>
                  <p className="mono">{candidate.ts_code}</p>
                </div>
                <span className="candidate-badge">
                  {candidate.candidate_status === "CANDIDATE" ? "候选" : "观察"}
                </span>
              </div>
            <h3>核心研究证据</h3>
            <dl className="evidence-list">
              <div><dt>入选理由</dt><dd>{candidate.selection_reasons.join("；")}</dd></div>
              <div><dt>半年以上催化剂</dt><dd>{candidate.catalysts?.join("；") || "数据不足"}</dd></div>
              <div><dt>主要风险</dt><dd>{candidate.risk_flags.join("；") || "暂无结构化风险标记"}</dd></div>
              <div><dt>观察条件</dt><dd>{candidate.observe_conditions?.join("；") || "数据不足"}</dd></div>
              <div><dt>失效条件</dt><dd>{candidate.invalidate_conditions?.join("；") || "数据不足"}</dd></div>
            </dl>
            </section>
            <section className="section-block factor-panel">
              <div className="section-heading factor-section-heading">
                <div>
                  <p className="eyebrow">{strategyNames[strategy]}</p>
                  <h2>因子明细</h2>
                </div>
                <span>{candidate.factor_details.length} 项</span>
              </div>
              {candidate.factor_details.length ? candidate.factor_details.map((factor) => {
                const factorSources = deduplicatedSources(factor, sources);
                return (
                  <article className="factor-row" key={factor.factor_name}>
                    <div className="factor-heading">
                      <strong>{factorNames[factor.factor_name] ?? factor.factor_name}</strong>
                      {factorNames[factor.factor_name] && (
                        <span className="mono factor-code">{factor.factor_name}</span>
                      )}
                    </div>
                    <div className="factor-metrics">
                      <span>因子值 {formatFactorValue(factor)}</span>
                      <span>标准化得分 {fixedNumber(factor.normalized_score)}</span>
                      <span>权重 {fixedNumber(String(Number(factor.weight) * 100))}%</span>
                      <span>加权贡献 {fixedNumber(factor.weighted_score)}</span>
                      <span>数据质量 {factor.quality_status}</span>
                      <span>标准化范围 {normalizationScopeName(factor.normalization_scope)}</span>
                      <span>市场数据回退 {factor.used_market_fallback ? "是" : "否"}</span>
                    </div>
                    <div className="factor-sources">
                      <span>来源记录 {factor.source_record_ids.length} 条</span>
                      {factorSources.map(({ source, recordCount }) => (
                        <a
                          key={source.source_url}
                          href={source.source_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {source.source_name}（{recordCount}条记录）
                        </a>
                      ))}
                    </div>
                  </article>
                );
              }) : <p>该报告未附加可展示的因子明细。</p>}
            </section>
          </div>
        </>
      )}
    </>
  );
}

function LegacyEvents({ report }: { report: ReportPayload }) {
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

function Events({ report }: { report: ReportPayload }) {
  const events = report.official_events ?? [];
  const eventDomainStatus = report.data_domain_statuses.events;
  return (
    <>
      <header className="page-header">
        <div><p className="eyebrow">仅官方来源</p><h1>官方事件流</h1></div>
        <span>事件截止 {formatDateTime(report.snapshot.event_cutoff_at ?? report.event_cutoff_at)}</span>
      </header>
      {events.length ? events.map((event, index) => (
        <article className="event-row" key={event.record_id ?? event.event_id ?? index}>
          <p className="eyebrow">{event.institution ?? "官方机构"}</p>
          <h2>{event.title ?? "未命名事件"}</h2>
          <div className="event-narrative">
            <div><strong>官方事实</strong><p>{event.factual_summary ?? "暂无事实摘要"}</p></div>
            <div>
              <strong>系统评估</strong>
              <p>{event.system_assessment || "尚未形成独立系统评估，不构成个股买入结论。"}</p>
            </div>
          </div>
          <div className="event-meta">
            <span>影响范围 {event.affected_scope ?? (event.affected_ts_codes?.join("、") || "市场整体")}</span>
            <span>影响周期 {event.impact_horizon ?? "待评估"}</span>
            <span>置信度 {event.confidence ?? "待评估"}</span>
            <span>发布于 {formatDateTime(event.published_at)}</span>
          </div>
          {event.related_strategies?.length ? (
            <p className="event-strategies">
              相关策略 {event.related_strategies.map((strategy) => strategyNames[strategy]).join("、")}
            </p>
          ) : null}
          {event.source_url && (
            <a href={event.source_url} target="_blank" rel="noreferrer">
              打开{event.institution}来源站点
            </a>
          )}
        </article>
      )) : (
        <div className="empty-panel">
          {eventDomainStatus === "MISSING"
            ? "官方事件数据域尚未入库或未随本报告发布，不能解释为截止时间前没有事件。"
            : "本报告事件截止时间前暂无新增官方事件。"}
        </div>
      )}
    </>
  );
}

function LegacyQuality({ report }: { report: ReportPayload }) {
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
      {report.pool_readiness && (
        <section className="section-block">
          <h2>试点策略池完整度</h2>
          <div className="quality-pools">
            {(Object.keys(strategyNames) as StrategyType[]).map((strategy) => {
              const readiness = report.pool_readiness?.[strategy];
              return readiness ? (
                <div key={strategy}>
                  <span>{strategyNames[strategy]}</span>
                  <ReadinessBadge readiness={readiness} />
                </div>
              ) : null;
            })}
          </div>
          <p className="quality-note">
            XBRL 使用 {report.quality_summary?.xbrl_used_count ?? 0} 份 ·
            PDF 补充 {report.quality_summary?.pdf_used_count ?? 0} 份 ·
            人工待办 {report.manual_todo_count ?? report.snapshot.manual_todo_count ?? 0} 项
          </p>
        </section>
      )}
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

const domainNames: Record<string, string> = {
  market: "行情",
  security_master: "证券名单",
  pilot_universe: "试点样本",
  manifest: "采集清单",
  financials: "财务事实",
  corporate_actions: "公司行动",
  dividends: "年度分红",
  risk: "风险证据",
  metrics: "衍生指标",
  events: "官方事件",
};

const qualityStatusNames: Record<string, string> = {
  VALID: "有效",
  DERIVED: "衍生",
  PARTIAL: "部分",
  MISSING: "缺失",
  CONFLICT: "冲突",
  STALE: "过期",
  UNVERIFIED: "未验证",
  REJECTED: "已拒绝",
};

type AggregatedSource = {
  key: string;
  domain: string;
  sourceName: string;
  sourceUrl: string;
  recordCount: number;
  versions: string[];
  publishedAt?: string | null;
  collectedAt: string;
  licensePolicy: string;
  qualityStatus: string;
};

function aggregateReportSources(sources: ReportSource[]): AggregatedSource[] {
  const grouped = new Map<string, AggregatedSource>();
  sources.forEach((source) => {
    const key = `${source.domain}\u0000${source.source_url}`;
    const existing = grouped.get(key);
    if (existing) {
      existing.recordCount += 1;
      if (!existing.versions.includes(source.version)) existing.versions.push(source.version);
      if (source.collected_at > existing.collectedAt) existing.collectedAt = source.collected_at;
      if ((source.published_at ?? "") > (existing.publishedAt ?? "")) {
        existing.publishedAt = source.published_at;
      }
      return;
    }
    grouped.set(key, {
      key,
      domain: source.domain,
      sourceName: source.source_name,
      sourceUrl: source.source_url,
      recordCount: 1,
      versions: [source.version],
      publishedAt: source.published_at,
      collectedAt: source.collected_at,
      licensePolicy: source.license_policy,
      qualityStatus: source.quality_status,
    });
  });
  return [...grouped.values()].sort((left, right) =>
    left.domain.localeCompare(right.domain) || left.sourceUrl.localeCompare(right.sourceUrl),
  );
}

const SOURCE_PAGE_SIZE = 12;

function Quality({ report }: { report: ReportPayload }) {
  const [domainFilter, setDomainFilter] = useState("ALL");
  const [sourceSearch, setSourceSearch] = useState("");
  const [sourcePage, setSourcePage] = useState(1);
  const sources = report.source_records ?? [];
  const groupedSources = useMemo(() => aggregateReportSources(sources), [sources]);
  const domains = [...new Set(groupedSources.map((source) => source.domain))].sort();
  const search = sourceSearch.trim().toLocaleLowerCase();
  const filteredSources = groupedSources.filter((source) =>
    (domainFilter === "ALL" || source.domain === domainFilter) &&
    (!search || `${source.sourceName} ${source.sourceUrl} ${source.versions.join(" ")}`
      .toLocaleLowerCase().includes(search)),
  );
  const pageCount = Math.max(1, Math.ceil(filteredSources.length / SOURCE_PAGE_SIZE));
  const currentPage = Math.min(sourcePage, pageCount);
  const pageSources = filteredSources.slice(
    (currentPage - 1) * SOURCE_PAGE_SIZE,
    currentPage * SOURCE_PAGE_SIZE,
  );
  const fallbackCount = report.quality_summary?.fallback_reason_counts
    ?.XBRL_UNAVAILABLE_OR_NOT_INGESTED ?? 0;

  function resetPage() {
    setSourcePage(1);
  }

  return (
    <>
      <header className="page-header">
        <div><p className="eyebrow">可追溯与可复现</p><h1>数据质量与来源</h1></div>
      </header>
      <section className="section-block">
        <h2>数据域状态</h2>
        <div className="quality-list quality-domain-list">
          {Object.entries(report.data_domain_statuses).map(([domain, status]) => (
            <div key={domain}>
              <span>{domainNames[domain] ?? domain}</span>
              <strong title={status}>{qualityStatusNames[status] ?? status}</strong>
            </div>
          ))}
        </div>
        <div className="domain-evidence-grid">
          <span>公司行动 {report.quality_summary?.corporate_action_count ?? 0} 条</span>
          <span>年度分红 {report.quality_summary?.annual_dividend_record_count ?? 0} 条</span>
          <span>风险证据 {report.quality_summary?.official_risk_screen_count ?? 0} 条</span>
          <span>官方事件 {report.quality_summary?.official_event_count ?? report.official_events?.length ?? 0} 条</span>
        </div>
      </section>
      {report.pool_readiness && (
        <section className="section-block">
          <h2>试点策略池完整度</h2>
          <div className="quality-pools">
            {strategyOrder.map((strategy) => {
              const readiness = report.pool_readiness?.[strategy];
              return readiness ? (
                <div key={strategy}>
                  <span>{strategyNames[strategy]}</span>
                  <ReadinessBadge readiness={readiness} />
                </div>
              ) : null;
            })}
          </div>
          <p className="quality-note">
            XBRL 使用 {report.quality_summary?.xbrl_used_count ?? 0} 份 ·
            PDF 回退 {report.quality_summary?.pdf_used_count ?? 0} 份 ·
            人工待办 {report.manual_todo_count ?? report.snapshot.manual_todo_count ?? 0} 项
          </p>
          {fallbackCount > 0 && (
            <p className="quality-callout">
              XBRL 未获取或未入库时使用交易所/巨潮官方 PDF 回退；本报告涉及 {fallbackCount} 份，不代表来源为非官方。
            </p>
          )}
        </section>
      )}
      <section className="section-block">
        <div className="section-heading">
          <div>
            <h2>来源与采集谱系</h2>
            <p>{sources.length} 条事实记录 · {groupedSources.length} 个来源文档/批次</p>
          </div>
        </div>
        <div className="source-toolbar">
          <label>
            <span>数据域</span>
            <select
              aria-label="筛选数据域"
              value={domainFilter}
              onChange={(event) => { setDomainFilter(event.target.value); resetPage(); }}
            >
              <option value="ALL">全部数据域</option>
              {domains.map((domain) => (
                <option value={domain} key={domain}>{domainNames[domain] ?? domain}</option>
              ))}
            </select>
          </label>
          <label>
            <span>搜索</span>
            <input
              type="search"
              aria-label="搜索来源"
              placeholder="来源名称、链接或版本"
              value={sourceSearch}
              onChange={(event) => { setSourceSearch(event.target.value); resetPage(); }}
            />
          </label>
        </div>
        {pageSources.length ? (
          <>
            <div className="source-grid">
              {pageSources.map((source) => (
                <article className="source-card" key={source.key}>
                  <div className="source-card-heading">
                    <div>
                      <p className="eyebrow">{domainNames[source.domain] ?? source.domain}</p>
                      <h3>{source.sourceName}</h3>
                    </div>
                    <strong title={source.qualityStatus}>
                      {qualityStatusNames[source.qualityStatus] ?? source.qualityStatus}
                    </strong>
                  </div>
                  <dl>
                    <div><dt>聚合记录</dt><dd>{source.recordCount} 条事实</dd></div>
                    <div><dt>版本</dt><dd>{source.versions.length} 个版本</dd></div>
                    <div><dt>发布时间</dt><dd>{formatDateTime(source.publishedAt)}</dd></div>
                    <div><dt>最近采集</dt><dd>{formatDateTime(source.collectedAt)}</dd></div>
                    <div><dt>许可策略</dt><dd>{source.licensePolicy}</dd></div>
                  </dl>
                  <a href={source.sourceUrl} target="_blank" rel="noreferrer">打开来源</a>
                </article>
              ))}
            </div>
            <div className="source-pagination" aria-label="来源分页">
              <button
                className="outline-button"
                disabled={currentPage === 1}
                onClick={() => setSourcePage((page) => Math.max(1, page - 1))}
              >上一页</button>
              <span>第 {currentPage} / {pageCount} 页</span>
              <button
                className="outline-button"
                disabled={currentPage === pageCount}
                onClick={() => setSourcePage((page) => Math.min(pageCount, page + 1))}
              >下一页</button>
            </div>
          </>
        ) : (
          <div className="empty-panel">没有符合当前筛选条件的来源。</div>
        )}
      </section>
    </>
  );
}

export default function App() {
  const demoMode = new URLSearchParams(window.location.search).get("demo") === "1";
  const [report, setReport] = useState<ReportPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState<Page>("每日研究总览");
  const [selected, setSelected] = useState<SecuritySelection | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    if (demoMode) {
      import("./demo").then(({ DEMO_REPORT }) => {
        if (!cancelled) setReport(DEMO_REPORT);
      });
    } else {
      loadLatestReport()
        .then((payload) => {
          if (!cancelled) setReport(payload);
        })
        .catch((reason: Error) => {
          if (!cancelled) setError(reason.message);
        });
    }
    return () => {
      cancelled = true;
    };
  }, [demoMode]);

  const content = useMemo(() => {
    if (!report) return null;
    if (page === "策略候选池") {
      return (
        <StrategyPools
          report={report}
          openSecurity={(candidate, strategy) => {
            setSelected({ tsCode: candidate.ts_code, strategy });
            setPage("个股研究");
          }}
        />
      );
    }
    if (page === "个股研究") {
      return <SecurityResearch report={report} selected={selected} onSelect={setSelected} />;
    }
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
          <>
            {report.display_status === "STALE_PREVIOUS_REPORT" && (
              <div className="stale-banner">上一版报告 · 本次更新未完成</div>
            )}
            <div className="utility-row">
              <span>{report.snapshot.report_date}</span>
              <span className="mono">{report.snapshot.report_id}</span>
              <span>数据截至 {formatDateTime(report.snapshot.market_cutoff_at)}</span>
            </div>
            {!demoMode && <PilotContextBanner report={report} />}
          </>
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
