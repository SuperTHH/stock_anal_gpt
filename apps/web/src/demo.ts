import type { Candidate, ReportPayload, StrategyType } from "./types";

const strategySecurities: Record<StrategyType, Array<[string, string]>> = {
  QUALITY_GROWTH: [
    ["688901.SH", "远澜微材（虚构）"],
    ["300901.SZ", "云衡智造（虚构）"],
    ["603901.SH", "澄岳电气（虚构）"],
  ],
  DEEP_VALUE: [
    ["600931.SH", "安澜实业（虚构）"],
    ["000931.SZ", "正衡资源（虚构）"],
    ["601931.SH", "海岱制造（虚构）"],
  ],
  STABLE_DIVIDEND: [
    ["600961.SH", "恒泽公用（虚构）"],
    ["000961.SZ", "宁川能源（虚构）"],
    ["601961.SH", "融泰港务（虚构）"],
  ],
};

function candidates(strategy: StrategyType): Candidate[] {
  return strategySecurities[strategy].map(([tsCode, securityName], index) => ({
    ts_code: tsCode,
    security_name: securityName,
    rank_in_strategy: index + 1,
    strategy_score: String(88 - index * 4),
    candidate_status: index === 2 ? "WATCH" : "CANDIDATE",
    selection_reasons: [
      strategy === "QUALITY_GROWTH"
        ? "资本回报与现金流质量位于演示样本前列"
        : strategy === "DEEP_VALUE"
          ? "估值与资产质量组合满足演示规则"
          : "已公告分红与自由现金流覆盖满足演示规则",
    ],
    risk_flags: [index === 0 ? "客户集中" : "周期波动"],
    catalysts: ["半年以上经营改善检查点"],
    observe_conditions: ["连续两期核心指标保持"],
    invalidate_conditions: ["核心因子连续两期跌破阈值"],
    data_completeness: String(0.96 - index * 0.02),
    factor_details: [
      ["ROIC", 18.6 - index, 92 - index * 5, 0.3],
      ["现金流质量", 1.28 - index * 0.08, 89 - index * 5, 0.25],
      ["收入增长", 14.2 - index, 84 - index * 4, 0.2],
      ["估值分位", 31 + index * 6, 78 - index * 4, 0.25],
    ].map(([factorName, rawValue, score, weight]) => ({
        factor_name: String(factorName),
        raw_value: String(rawValue),
        normalized_score: String(score),
        weight: String(weight),
        weighted_score: String(Number(score) * Number(weight)),
        quality_status: "DERIVED",
        source_record_ids: ["demo-market", "demo-financials"],
        normalization_scope: "demo-market",
        used_market_fallback: true,
      })),
  }));
}

export const DEMO_REPORT: ReportPayload = {
  snapshot: {
    report_id: "demo-report-2026-07-22",
    report_date: "2026-07-22",
    market_cutoff_at: "2026-07-22T21:30:00+08:00",
    report_status: "PUBLISHED",
    data_domain_statuses: {
      demo_market: "DERIVED",
      demo_financials: "DERIVED",
    },
  },
  candidate_pools: {
    QUALITY_GROWTH: candidates("QUALITY_GROWTH"),
    DEEP_VALUE: candidates("DEEP_VALUE"),
    STABLE_DIVIDEND: candidates("STABLE_DIVIDEND"),
  },
  data_domain_statuses: {
    demo_market: "DERIVED",
    demo_financials: "DERIVED",
  },
  official_events: [
    {
      event_id: "demo-event-1",
      institution: "上海证券交易所（演示）",
      event_type: "COMPANY_ANNOUNCEMENT",
      title: "示例公告：远澜微材经营进展说明",
      factual_summary:
        "虚构公司披露产能利用率改善；系统将其标记为中期经营观察点，不构成买入指令。",
      affected_ts_codes: ["688901.SH"],
      impact_horizon: "中期（6—12个月）",
      confidence: "0.86",
      source_url: "https://www.sse.com.cn/",
      published_at: "2026-07-22T18:10:00+08:00",
    },
    {
      event_id: "demo-event-2",
      institution: "中国证监会（演示）",
      event_type: "REGULATORY_POLICY",
      title: "示例政策：上市公司现金分红监管提示",
      factual_summary:
        "演示事件用于说明政策事实摘要、影响对象、影响周期与置信度的呈现方式。",
      affected_ts_codes: ["600961.SH", "000961.SZ", "601961.SH"],
      impact_horizon: "长期（12个月以上）",
      confidence: "0.78",
      source_url: "https://www.csrc.gov.cn/",
      published_at: "2026-07-22T16:30:00+08:00",
    },
  ],
  source_records: [
    {
      record_id: "demo-market",
      domain: "行情",
      source_name: "Tushare 日线接口",
      source_url: "https://tushare.pro/document/2?doc_id=27",
      published_at: "2026-07-22T15:05:00+08:00",
      effective_at: "2026-07-22T15:00:00+08:00",
      collected_at: "2026-07-22T21:31:00+08:00",
      valid_from: "2026-07-22T21:31:00+08:00",
      version: "demo-2026-07-22",
      license_policy: "个人非商业研究（演示）",
      quality_status: "DERIVED",
    },
    {
      record_id: "demo-financials",
      domain: "财务事实",
      source_name: "交易所 XBRL / 巨潮定期报告",
      source_url: "https://www.cninfo.com.cn/",
      published_at: "2026-04-30T18:00:00+08:00",
      effective_at: "2025-12-31T23:59:59+08:00",
      collected_at: "2026-07-22T21:31:00+08:00",
      valid_from: "2026-07-22T21:31:00+08:00",
      version: "demo-fy2025-v1",
      license_policy: "个人非商业研究（演示）",
      quality_status: "DERIVED",
    },
  ],
};
