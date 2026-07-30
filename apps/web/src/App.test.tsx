import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import App from "./App";

const report = {
  snapshot: {
    report_id: "report-2026-07-29-v1",
    report_date: "2026-07-29",
    market_cutoff_at: "2026-07-29T21:30:00+08:00",
    report_status: "PUBLISHED",
    data_domain_statuses: { market: "VALID", financials: "VALID" },
    is_historical_reconstruction: true,
    universe_id: "pilot-2026-07-22",
    report_cutoff_at: "2026-07-29T13:00:00Z",
    known_at: "2026-07-29T13:00:00Z",
    generation_started_at: "2026-07-30T09:05:00+08:00",
    generated_at: "2026-07-30T09:08:00+08:00",
    manual_todo_count: 2,
  },
  candidate_pools: {
    QUALITY_GROWTH: [
      {
        ts_code: "699991.SH",
        rank_in_strategy: 1,
        strategy_score: "88.00",
        candidate_status: "CANDIDATE",
        selection_reasons: ["ROIC 与现金流质量领先"],
        risk_flags: ["客户集中"],
        data_completeness: "0.96",
        factor_details: [],
      },
    ],
    DEEP_VALUE: [
      {
        ts_code: "699992.SH",
        rank_in_strategy: 1,
        strategy_score: "82.00",
        candidate_status: "WATCH",
        selection_reasons: ["估值处于历史低位"],
        risk_flags: ["周期位置"],
        data_completeness: "0.92",
        factor_details: [],
      },
    ],
    STABLE_DIVIDEND: [],
  },
  data_domain_statuses: { market: "VALID", financials: "VALID" },
  official_events: [],
  pool_readiness: {
    QUALITY_GROWTH: {
      strategy_type: "QUALITY_GROWTH",
      universe_size: 30,
      eligible_count: 29,
      complete_factor_count: 28,
      coverage_ratio: "0.9333",
      required_coverage_ratio: "0.8",
      status: "READY",
      missing_by_security: { "699997.SH": ["gross_margin:VALUE_MISSING"] },
      blocking_codes: [],
      strategy_version: "quality-growth-pilot-v1",
      factor_version: "pilot-financial-metrics-v1",
    },
    DEEP_VALUE: {
      strategy_type: "DEEP_VALUE",
      universe_size: 30,
      eligible_count: 30,
      complete_factor_count: 30,
      coverage_ratio: "1",
      required_coverage_ratio: "0.8",
      status: "READY",
      missing_by_security: {},
      blocking_codes: [],
      strategy_version: "deep-value-pilot-v1",
      factor_version: "pilot-financial-metrics-v1",
    },
    STABLE_DIVIDEND: {
      strategy_type: "STABLE_DIVIDEND",
      universe_size: 30,
      eligible_count: 21,
      complete_factor_count: 21,
      coverage_ratio: "0.7",
      required_coverage_ratio: "0.8",
      status: "BLOCKED",
      missing_by_security: {
        "699996.SH": ["cash_dividend_total:VALUE_MISSING"],
        "699995.SH": ["free_cash_flow:VALUE_MISSING"],
      },
      blocking_codes: ["POOL_FACTOR_COVERAGE_BELOW_80_PERCENT"],
      strategy_version: "stable-dividend-pilot-v1",
      factor_version: "pilot-financial-metrics-v1",
    },
  },
  quality_summary: {
    manifest_status_distribution: { INGESTED: 360, MANUAL_TODO: 2 },
    xbrl_used_count: 140,
    pdf_used_count: 10,
  },
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.history.replaceState({}, "", "/");
});

test("all pages stay pinned to one published report while strategy tabs remain independent", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(report), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
  const user = userEvent.setup();
  render(<App />);

  expect((await screen.findAllByText("2026-07-29")).length).toBeGreaterThan(0);
  await user.click(screen.getByRole("button", { name: "策略候选池" }));
  expect(screen.getByText("699991.SH")).toBeInTheDocument();
  await user.click(screen.getByRole("tab", { name: /^低估值价值/ }));
  expect(screen.getByText("699992.SH")).toBeInTheDocument();
  expect(screen.queryByText("699991.SH")).not.toBeInTheDocument();
  expect(screen.getByText("report-2026-07-29-v1")).toBeInTheDocument();
});

test("historical pilot report shows its fixed boundary, cutoff, generation time, and pool readiness", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(report), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
  render(<App />);

  expect(await screen.findByText("真实数据 · 30只试点样本 · 历史重建")).toBeInTheDocument();
  expect(screen.getByText("排名只在 30 只试点样本内有效")).toBeInTheDocument();
  expect(screen.getByText(/报告截止.*2026-07-29 13:00/)).toBeInTheDocument();
  expect(screen.getByText(/实际生成.*2026-07-30 09:05/)).toBeInTheDocument();
  expect(screen.getAllByText("READY")).toHaveLength(2);
  expect(screen.getByText("BLOCKED")).toBeInTheDocument();
  expect(screen.getByText("28 / 30")).toBeInTheDocument();
  expect(screen.getByText("93.33%")).toBeInTheDocument();
});

test("blocked strategy explains missing securities, factors, and machine-readable codes", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(report), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
  const user = userEvent.setup();
  render(<App />);

  await user.click(await screen.findByRole("button", { name: "策略候选池" }));
  await user.click(screen.getByRole("tab", { name: /^稳定高股息/ }));
  expect(screen.getByRole("heading", { name: "该策略池暂不发布候选" })).toBeInTheDocument();
  expect(screen.getByText("POOL_FACTOR_COVERAGE_BELOW_80_PERCENT")).toBeInTheDocument();
  expect(screen.getByText("699996.SH")).toBeInTheDocument();
  expect(screen.getByText("cash_dividend_total:VALUE_MISSING")).toBeInTheDocument();
  expect(screen.queryByText("本报告在该策略下没有候选标的。")).not.toBeInTheDocument();
});

test("stale previous report is visibly marked instead of presented as current", async () => {
  const staleReport = {
    ...report,
    display_status: "STALE_PREVIOUS_REPORT",
  };
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(staleReport), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
  render(<App />);

  expect(await screen.findByText("上一版报告 · 本次更新未完成")).toBeInTheDocument();
});

test("production empty state never falls back to fictional prototype candidates", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify({ detail: "NO_PUBLISHED_REPORT" }), {
      status: 404,
      headers: { "Content-Type": "application/json" },
    }),
  );
  render(<App />);

  await waitFor(() => expect(screen.getByText("暂无已发布报告")).toBeInTheDocument());
  expect(screen.queryByText("功能演示数据 · 虚构标的 · 非实时")).not.toBeInTheDocument();
  expect(screen.queryByText("远澜微材")).not.toBeInTheDocument();
});

test("five navigation destinations render from the loaded report without another latest fetch", async () => {
  const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(report), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "每日研究总览" });

  for (const destination of ["官方事件流", "数据质量与来源", "个股研究"]) {
    await user.click(screen.getByRole("button", { name: destination }));
    expect(screen.getByRole("heading", { name: destination })).toBeInTheDocument();
  }
  expect(fetchSpy).toHaveBeenCalledTimes(1);
});

test("demo mode is explicit, watermarked, and does not call the production report API", async () => {
  window.history.replaceState({}, "", "/?demo=1");
  const fetchSpy = vi.spyOn(globalThis, "fetch");
  render(<App />);

  expect(
    await screen.findByText("功能演示数据 · 虚构标的 · 非实时"),
  ).toBeInTheDocument();
  expect(fetchSpy).not.toHaveBeenCalled();
});

test("demo mode demonstrates fictional names, official events, and source lineage", async () => {
  window.history.replaceState({}, "", "/?demo=1");
  const user = userEvent.setup();
  render(<App />);

  await user.click(screen.getByRole("button", { name: "策略候选池" }));
  expect(screen.getByText("远澜微材（虚构）")).toBeInTheDocument();
  expect(screen.getByText("688901.SH")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "个股研究" }));
  expect(
    screen.getByRole("heading", { name: "远澜微材（虚构）" }),
  ).toBeInTheDocument();
  expect(screen.getByText("ROIC")).toBeInTheDocument();
  expect(screen.getByText("现金流质量")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "官方事件流" }));
  expect(
    screen.getByRole("heading", { name: "示例公告：远澜微材经营进展说明" }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("link", { name: "打开上海证券交易所（演示）来源站点" }),
  ).toHaveAttribute("href", "https://www.sse.com.cn/");

  await user.click(screen.getByRole("button", { name: "数据质量与来源" }));
  expect(screen.getByText("Tushare 日线接口")).toBeInTheDocument();
  expect(screen.getAllByText("个人非商业研究（演示）")).toHaveLength(2);
  expect(screen.getAllByText("2026-07-22 21:31")).toHaveLength(2);
});
