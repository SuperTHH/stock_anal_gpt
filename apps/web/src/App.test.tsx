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
