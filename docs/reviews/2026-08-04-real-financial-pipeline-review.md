# Real financial pipeline code review

- Review date: 2026-08-04
- Branch: `codex/real-data-minimal-loop`
- Reviewed commit: `d469a13a74e4556371d0813c341e7f7135349e55`
- Pull request: <https://github.com/SuperTHH/stock_anal_gpt/pull/3>
- Scope: real financial ingestion, point-in-time metrics, independent strategy pools,
  checkpoint resumption, and private-data isolation

## Outcome

**Changes requested before merge.** The implementation path is now wired and fail-closed,
and CI is green, but the private pilot still has no verified financial filing, corporate-action
timeline, or official risk screen. Consequently all three pools correctly remain `BLOCKED`.
The remaining open parser/import work is material because production cannot reach `READY`
without it.

GitHub Copilot review was requested through the documented REST reviewer identity
`copilot-pull-request-reviewer[bot]`. GitHub returned no review and no line comments, so this
document does not claim an independent Copilot approval.

## Findings fixed during review

### Important — complete metrics could bypass missing official risk evidence

Previously `_pool_results()` marked every sampled security as hard-filter-passed. A complete
metric payload could therefore publish candidates without an ingested official risk screen.

Resolution:

- production hard filters now require an `INGESTED` and usable `RISK_SCREEN` manifest item;
- missing evidence produces `HF-RISK-SCREEN-MISSING` and blocks every pool;
- an injected provider remains available only for isolated integration tests;
- regression test:
  `test_complete_metrics_cannot_bypass_missing_official_risk_screen`.

### Important — resumed ingestion could reuse stale in-memory metrics

The metric cache key contained only universe and cutoff identities. If the same runner object
processed a newly added manual attachment, stage 9 could reuse results calculated before the
attachment was ingested.

Resolution:

- stage 7 clears the metric cache before processing downloaded documents;
- stage 6 checkpoint input now includes deterministic manual-inbox content hashes and the
  reviewed CNINFO policy, so new files or a policy change invalidate downstream checkpoints;
- regression test:
  `test_new_manual_inbox_content_invalidates_scan_and_downstream_checkpoints`.

### Important — a second PDF correction leaked a SQLite integrity exception

The immutable PDF store had a unique successor index, but a branched correction chain surfaced
as a raw `sqlite3.IntegrityError` instead of a stable domain error.

Resolution:

- the repository now detects an existing successor before insert and raises
  `PDF_FINANCIAL_BRANCH_CONFLICT`;
- regression test:
  `test_pdf_repository_rejects_conflicts_and_broken_correction_chain`.

## Open material findings

### Important — non-periodic official documents have no production parser/import path

`DIVIDEND_RECORD`, `CAPITAL_ACTION_TIMELINE`, and `RISK_SCREEN` are planned and can enter the
manual inbox, but stage 7 currently ingests only CNINFO periodic-report PDFs. The action
repository and share-capital calculator exist, yet no production adapter converts these three
official document kinds into versioned actions and risk evidence.

Impact:

- stable-dividend factors cannot become complete from private production data;
- official risk hard filters intentionally keep every pool blocked;
- the Task 8 implementation checkbox remains open in the execution plan.

Required follow-up:

- define reviewed, versioned schemas for dividend/action/risk extraction;
- implement fail-closed importers with official attachment hashes and publication times;
- add correction-chain and point-in-time integration tests before allowing `INGESTED`.

### Important — current real CNINFO sample lacks a required canonical fact

The private `603986.SH` 2026 Q1 PDF yields 18 deterministic candidates and passes statement
equations, but it does not directly disclose the required `interest_bearing_debt` canonical
fact. The system correctly returns `PDF_REQUIRED_FACTS_MISSING` and does not persist the filing.

Required follow-up:

- prefer an official exchange XBRL instance with reviewed mappings when available; or
- add a separately reviewed, versioned debt-component derivation specification. Do not infer
  the value from loosely matched PDF labels.

### Important — real coverage remains below the publication threshold

No strategy has the required 24/30 complete, eligible securities. Current private acceptance:

- manifest: 360; manual todo: 360;
- verified financial facts: 0;
- pool coverage: 0 for all three pools;
- candidates: 0;
- acceptance errors: 0.

This is an external-data completion blocker, not a reason to lower the 80% threshold or publish
sample candidates.

## Verification evidence

- GitHub Actions: Windows Python, Ubuntu Python, static checks, and frontend all passed for
  `d469a13`.
- Local collection: 649 tests.
- Local bounded shards: 647 passed; one existing Windows capability test skipped.
- One wheel-install smoke built successfully and pip printed `Successfully installed`, but the
  pip process did not exit inside the current Windows sandbox; it is not counted as locally
  complete. The corresponding GitHub Windows job passed.
- Ruff: passed.
- `git diff --check`: passed.
- Frontend: 8 tests passed; production build passed.
- Real attachments, `.env`, SQLite, Parquet, reports, and run summaries remain Git-ignored.
