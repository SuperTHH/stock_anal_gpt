import type { ReportPayload } from "./types";

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
