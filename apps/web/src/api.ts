import type {
  EvidenceStatusPayload,
  EvidenceTask,
  ExchangeXbrlStatusPayload,
  FullMarketPayload,
  FullMarketResearchPayload,
  LiveOfficialEventsPayload,
  ReportPayload,
} from "./types";

export async function loadLatestReport(): Promise<ReportPayload> {
  const response = await fetch("/api/reports/latest", {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail ?? "REPORT_LOAD_FAILED");
  }
  return (await response.json()) as ReportPayload;
}

export async function loadFullMarket(
  filters: {
    page: number;
    search: string;
    board: string;
    dividendData: string;
    minimumDividendYield: string;
    sortBy: string;
    descending: boolean;
  },
): Promise<FullMarketPayload> {
  const params = new URLSearchParams({
    page: String(filters.page),
    page_size: "50",
    dividend_data: filters.dividendData,
    minimum_dividend_yield: filters.minimumDividendYield,
    sort_by: filters.sortBy,
    descending: String(filters.descending),
  });
  if (filters.search.trim()) params.set("search", filters.search.trim());
  if (filters.board) params.set("board", filters.board);
  const response = await fetch(`/api/market/securities?${params}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail ?? "FULL_MARKET_LOAD_FAILED");
  }
  return (await response.json()) as FullMarketPayload;
}

export async function loadFullMarketResearch(): Promise<FullMarketResearchPayload> {
  const response = await fetch("/api/market/research", {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail ?? "FULL_MARKET_RESEARCH_LOAD_FAILED");
  }
  return (await response.json()) as FullMarketResearchPayload;
}

export async function loadLiveOfficialEvents(): Promise<LiveOfficialEventsPayload> {
  const response = await fetch("/api/market/events", {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error("OFFICIAL_EVENTS_LOAD_FAILED");
  const payload = (await response.json()) as LiveOfficialEventsPayload;
  if (!Array.isArray(payload.events)) throw new Error("OFFICIAL_EVENTS_LOAD_FAILED");
  return payload;
}

export async function loadExchangeXbrlStatus(): Promise<ExchangeXbrlStatusPayload> {
  const response = await fetch("/api/market/xbrl-status", {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error("EXCHANGE_XBRL_STATUS_LOAD_FAILED");
  const payload = (await response.json()) as ExchangeXbrlStatusPayload;
  if (!Array.isArray(payload.scans)) throw new Error("EXCHANGE_XBRL_STATUS_LOAD_FAILED");
  return payload;
}

export async function loadEvidenceStatus(status = ""): Promise<EvidenceStatusPayload> {
  const params = new URLSearchParams({ page: "1", page_size: "200" });
  if (status) params.append("status", status);
  const response = await fetch(`/api/market/evidence-status?${params}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error("EVIDENCE_STATUS_LOAD_FAILED");
  return (await response.json()) as EvidenceStatusPayload;
}

export async function decideEvidenceReview(
  task: EvidenceTask,
  decision: "CONFIRM" | "RETURN",
  reviewedValues: Record<string, boolean | string | null>,
  note: string,
): Promise<EvidenceTask> {
  const response = await fetch(`/api/market/evidence-reviews/${task.task_id}/decision`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({
      expected_version: task.version,
      decision,
      reviewed_values: reviewedValues,
      note,
    }),
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail ?? "EVIDENCE_REVIEW_FAILED");
  }
  return (await response.json()) as EvidenceTask;
}
