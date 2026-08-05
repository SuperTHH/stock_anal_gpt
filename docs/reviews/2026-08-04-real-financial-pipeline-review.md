# Real financial pipeline code review

- Review updated: 2026-08-05
- Branch: `codex/real-data-minimal-loop`
- Baseline reviewed commit: `0beeff0fbd715157d06403ff211feb8054561374`
- Pull request: <https://github.com/SuperTHH/stock_anal_gpt/pull/3>
- Scope: official evidence import, PDF fallback facts, point-in-time filters,
  independent strategy pools, report lineage, and private-data isolation

## Outcome

**Code changes approved after fixes; real candidate publication remains data-blocked.**
The production path is fail-closed and now has importers for reviewed dividend, capital-action,
and risk evidence. A real CNINFO quarterly report is persisted with 24 facts, including a
versioned interest-bearing-debt component derivation. The current private pilot still has only
1 of 150 periodic reports and no reviewed action or risk evidence, so all three pools correctly
remain `BLOCKED`; no candidate is fabricated.

This review was performed against the working-tree implementation and regression tests in the
current session. The prior GitHub Copilot reviewer request returned no review or line comments,
so this document does not claim an independent bot approval.

## Important findings fixed during review

### Non-periodic official evidence stopped at DOWNLOADED

Stage 7 previously handled only CNINFO periodic-report PDFs. Reviewed dividend, capital-action,
and risk files could enter the private inbox but never become versioned facts.

Resolution:

- added hash-bound, manually reviewed `official-action-evidence-v1` and
  `official-risk-screen-v1` sidecars;
- added atomic corporate-action batch persistence and an append-only risk repository;
- route all three non-periodic document kinds through the production ingestion stage;
- invalid schemas, hashes, times, chains, and kinds return to `AWAITING_MANUAL`.

### The real CNINFO sample lacked a direct debt total

The real 603986.SH 2026 Q1 PDF did not print one `interest_bearing_debt` row, although it
explicitly disclosed all five reviewed components.

Resolution:

- derive the total only when short-term borrowings, current portions of non-current
  liabilities, long-term borrowings, bonds payable, and lease liabilities are all visibly
  present;
- treat an explicitly blank visible component row as zero, while an absent row still blocks;
- retain component IDs and `interest-bearing-debt-components-v1` in normalization metadata;
- correct pypdf's zero-based physical page number before storing fact lineage.

### Risk screens could leak before `effective_at`

Risk queries constrained publication, collection, and review time but omitted the effective
cutoff. A future-effective risk status could therefore be visible too early.

Resolution: `visible_screen()` now requires `effective_at <= as_of`; the regression test proves
the record is invisible before and visible at the effective cutoff.

### READY candidates had attachment-level but not fact-level lineage

Production reports listed acquisition-manifest IDs, while candidate factors reference exact
XBRL/PDF fact IDs, market inputs, corporate actions, and risk records. Once a pool became READY,
the publisher would reject those unresolved IDs.

Resolution:

- report publication now resolves official manifest records plus exact market, security-master,
  XBRL, PDF fact, corporate-action, and risk source records;
- blocked reports retain a concise source list, while READY reports include only referenced
  fact-level records;
- a READY three-pool integration test proves the report publishes when every factor ID resolves.

### Closing-price IDs were shared across all 30 securities

The prior ID contained only the market date. Different securities therefore referenced the
same apparent input identity.

Resolution: the identity is now `closing-price:<date>:<ts_code>` in both derived metrics and
report sources, with a regression assertion on the exact ID.

## Remaining external-data blocker

Real coverage remains below the 24/30 publication threshold:

- universe: 30, with board quotas 8/8/7/7;
- manifest: 360, including 150 periodic reports;
- ingested: 1; manual todo: 359;
- verified financial facts: 24; XBRL used: 0; PDF used: 1;
- corporate actions: 0; risk screens: 0; derived metrics: 0;
- all three pools: `BLOCKED`, coverage 0, candidates 0.

This is an input-completion blocker, not a reason to lower the threshold or publish sample
candidates. Each security needs the required five point-in-time filings and official risk
evidence; the stable-dividend pool additionally needs reviewed 2021-2025 dividend records.

## Verification evidence

- targeted acquisition, evidence, PDF, repository, and production-pipeline tests passed;
- all tests except the Windows wheel-install smoke test: 663 passed, 1 platform capability test
  skipped, 1 deselected;
- the undeselected root run reached 86% without failures but timed out after 10 minutes in the
  existing wheel-install subprocess, so it is not reported as a complete full-suite pass;
- Ruff passed; `git diff --check` passed;
- frontend: 8 tests passed; production build passed;
- the private 12-stage rebuild and acceptance validator passed with zero acceptance errors;
- five real-mode UI pages were reviewed at 1440x900 and 1280x720 with no white screen,
  horizontal overflow, or Chinese mojibake; the quality page exposes the official CNINFO link;
- real attachments, `.env`, SQLite, Parquet, reports, and run summaries remain Git-ignored.
