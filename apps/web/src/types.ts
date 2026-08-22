export type StrategyType = "QUALITY_GROWTH" | "DEEP_VALUE" | "STABLE_DIVIDEND";
export type PoolStatus = "READY" | "BLOCKED";

export interface PoolReadiness {
  strategy_type: StrategyType;
  universe_size: number;
  eligible_count: number;
  complete_factor_count: number;
  coverage_ratio: string;
  required_coverage_ratio: string;
  status: PoolStatus;
  missing_by_security: Record<string, string[]>;
  blocking_codes: string[];
  strategy_version: string;
  factor_version: string;
}

export interface FactorDetail {
  factor_name: string;
  raw_value: string | null;
  normalized_score: string | null;
  weight: string;
  weighted_score: string | null;
  quality_status: string;
  source_record_ids: string[];
  normalization_scope: string;
  used_market_fallback: boolean;
}

export interface Candidate {
  ts_code: string;
  security_name?: string;
  rank_in_strategy: number;
  strategy_score: string;
  candidate_status: string;
  selection_reasons: string[];
  risk_flags: string[];
  catalysts?: string[];
  observe_conditions?: string[];
  invalidate_conditions?: string[];
  data_completeness: string;
  factor_details: FactorDetail[];
}

export interface OfficialEvent {
  event_id?: string;
  record_id?: string;
  institution: string;
  event_type?: string;
  title: string;
  factual_summary: string;
  system_assessment?: string;
  affected_scope?: string;
  affected_ts_codes?: string[];
  related_strategies?: StrategyType[];
  impact_horizon?: string;
  confidence?: string;
  source_url?: string;
  published_at?: string;
}

export interface ReportSource {
  record_id: string;
  domain: string;
  source_name: string;
  source_url: string;
  published_at?: string | null;
  effective_at?: string | null;
  collected_at: string;
  valid_from: string;
  version: string;
  license_policy: string;
  quality_status: string;
}

export interface ReportPayload {
  snapshot: {
    report_id: string;
    report_date: string;
    market_cutoff_at: string;
    event_cutoff_at?: string;
    report_status: string;
    data_domain_statuses: Record<string, string>;
    is_historical_reconstruction?: boolean;
    universe_id?: string | null;
    report_cutoff_at?: string | null;
    known_at?: string | null;
    generation_started_at?: string | null;
    generated_at?: string;
    manual_todo_count?: number;
  };
  candidate_pools: Record<StrategyType, Candidate[]>;
  data_domain_statuses: Record<string, string>;
  official_events?: OfficialEvent[];
  source_records?: ReportSource[];
  pool_readiness?: Partial<Record<StrategyType, PoolReadiness>>;
  universe_id?: string | null;
  report_cutoff_at?: string | null;
  event_cutoff_at?: string | null;
  known_at?: string | null;
  generation_started_at?: string | null;
  manual_todo_count?: number;
  quality_summary?: {
    manifest_status_distribution?: Record<string, number>;
    xbrl_used_count?: number;
    pdf_used_count?: number;
      fallback_reason_counts?: Record<string, number>;
      corporate_action_count?: number;
      corporate_action_screen_count?: number;
      corporate_action_screen_target_count?: number;
      annual_dividend_record_count?: number;
    official_risk_screen_count?: number;
    official_event_count?: number;
    financial_fact_count?: number;
    derived_metric_count?: number;
    [key: string]: unknown;
  };
  strategy_research_status?: "READY" | "BLOCKED";
  data_completeness_status?: "COMPLETE" | "PARTIAL";
  display_status?: string;
}

export interface FullMarketSecurity {
  ts_code: string;
  name: string;
  exchange: string;
  board: string;
  industry_l1: string | null;
  market_data_status: "AVAILABLE" | "OFFICIAL_NO_TRADING" | "COLLECTION_FAILED";
  market_data_issue: string | null;
  trade_date: string | null;
  close: string | null;
  amount: string | null;
  trailing_12m_cash_dividend_per_share: string | null;
  dividend_yield: string | null;
  dividend_event_count: number;
  dividend_source_urls: string[];
}

export interface FullMarketPayload {
  market_date: string;
  universe_as_of: string;
  universe_hash: string;
  page: number;
  page_size: number;
  page_count: number;
  total: number;
  summary: {
    universe_count: number;
    market_bar_count: number;
    official_no_trading_count: number;
    market_collection_failed_count: number;
    dividend_security_count: number;
    dividend_coverage_ratio: string;
    industry_security_count: number;
    industry_coverage_ratio: string;
    yield_at_least_5_percent_count: number;
    dividend_window_start: string;
    dividend_window_end: string;
  };
  items: FullMarketSecurity[];
}

export interface EvidenceCoverage {
  periodic_report_count: number;
  annual_dividend_count: number;
  risk_screen_available: boolean;
  corporate_action_screen_available: boolean;
  required_item_count: number;
  completed_item_count: number;
  missing_items: string[];
  ready_for_scoring: boolean;
}

export interface FunnelSecurity {
  ts_code: string;
  name: string;
  board: string;
  amount: string;
  dividend_yield: string | null;
  entry_reasons: string[];
  evidence: EvidenceCoverage;
}

export interface DynamicPoolStatus {
  strategy_type: StrategyType;
  strategy_version: string;
  status: PoolStatus;
  universe_size: number;
  complete_factor_count: number;
  coverage_ratio: string;
  required_coverage_ratio: string;
  minimum_complete_factor_count: number;
  blocking_codes: string[];
  candidate_count: number;
}

export interface FullMarketResearchPayload {
  snapshot_id: string;
  market_date: string;
  market_universe_count: number;
  low_cost_eligible_count: number;
  funnel_count: number;
  high_dividend_funnel_count: number;
  depth_ready_count: number;
  evidence_item_count: number;
  evidence_completed_count: number;
  funnel: FunnelSecurity[];
  pools: Record<StrategyType, DynamicPoolStatus>;
  candidate_pools: Record<StrategyType, Candidate[]>;
  source_records: ReportSource[];
}

export type EvidenceTaskStatus =
  | "PLANNED" | "DISCOVERED" | "DOWNLOADED" | "PARSED"
  | "AWAITING_REVIEW" | "SATISFIED" | "RETRYABLE_FAILED" | "BLOCKED";

export interface EvidenceTask {
  task_id: string;
  run_id: string;
  market_date: string;
  cohort: "YIELD_GE_5" | "YIELD_3_TO_5" | "LIQUIDITY_FILL";
  ts_code: string;
  security_name: string;
  evidence_kind: "PERIODIC_REPORT" | "DIVIDEND_YEAR" | "RISK_SCREEN" | "CORPORATE_ACTION";
  evidence_period: string;
  status: EvidenceTaskStatus;
  version: number;
  source_url: string | null;
  source_title: string | null;
  source_page: number | null;
  excerpt: string | null;
  prefilled_values: Record<string, boolean | string | null>;
  error_code: string | null;
}

export interface EvidenceStatusPayload {
  market_date: string;
  runs: Array<{
    run_id: string;
    cohort: EvidenceTask["cohort"];
    member_codes: string[];
    task_count: number;
    satisfied_count: number;
    completion_ratio: string;
    status_counts: Record<string, number>;
  }>;
  page: number;
  page_count: number;
  total: number;
  items: EvidenceTask[];
}
