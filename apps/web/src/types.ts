export type StrategyType = "QUALITY_GROWTH" | "DEEP_VALUE" | "STABLE_DIVIDEND";

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
  affected_ts_codes?: string[];
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
    report_status: string;
    data_domain_statuses: Record<string, string>;
  };
  candidate_pools: Record<StrategyType, Candidate[]>;
  data_domain_statuses: Record<string, string>;
  official_events?: OfficialEvent[];
  source_records?: ReportSource[];
}
