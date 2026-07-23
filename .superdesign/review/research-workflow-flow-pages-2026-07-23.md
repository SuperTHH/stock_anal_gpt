# Research Workflow Flow Pages — 2026-07-23

## Base draft

- title: 衡策 A股长期研究台 - 市场指标溯源增强
- draft_id: 1721cd00-0df1-404d-a6d4-7b823086f1ff
- canvas_url: https://superdesign.dev/teams/dd3e76e7-a417-46d9-a7de-4c1447246341/projects/72c80b98-e159-48f9-9ed9-e846aa353ae1?node=draft-variant-1721cd00-0df1-404d-a6d4-7b823086f1ff
- preview_url: https://p.superdesign.dev/draft/1721cd00-0df1-404d-a6d4-7b823086f1ff
- version_after_flow_generation: 4

## 策略候选池

- requested_title: 策略候选池
- current_title: 策略候选池 - 衡策 A股长期研究台
- draft_id: f1a6f4ce-0bf7-40cd-a30e-030200612058
- canvas_url: https://superdesign.dev/teams/dd3e76e7-a417-46d9-a7de-4c1447246341/projects/72c80b98-e159-48f9-9ed9-e846aa353ae1?node=draft-variant-f1a6f4ce-0bf7-40cd-a30e-030200612058
- preview_url: https://p.superdesign.dev/draft/f1a6f4ce-0bf7-40cd-a30e-030200612058
- version: 2
- generated_at: 2026-07-23T09:52:01.177Z
- link_status: pending
- initial_visual_review: unavailable — the required in-app browser backend was not available, so no visual verification claim is made
- repair: one replace repair completed
- post_repair_source_review: all 18 required fictitious names are present; real-company leakage is absent; the fixed demo watermark, 功能演示数据集 provenance, sessionStorage state, and 32px stock-research action are present

## 个股研究

- requested_title: 个股研究
- current_title: 个股研究 - 远澜微材 (688901.SH)
- draft_id: ab9fa125-1d7d-46b4-89ea-2d8a01d8e3b1
- canvas_url: https://superdesign.dev/teams/dd3e76e7-a417-46d9-a7de-4c1447246341/projects/72c80b98-e159-48f9-9ed9-e846aa353ae1?node=draft-variant-ab9fa125-1d7d-46b4-89ea-2d8a01d8e3b1
- preview_url: https://p.superdesign.dev/draft/ab9fa125-1d7d-46b4-89ea-2d8a01d8e3b1
- version: 3
- generated_at: 2026-07-23T09:51:59.450Z
- link_status: source-verified
- initial_visual_review: unavailable — the required in-app browser backend was not available, so no visual verification claim is made
- repair: one replace repair completed
- compliance_repair_2026-07-23: one additional replace completed; CLI exit code 0
- post_compliance_source_review: the native `nav-back` anchor keeps the exact strategy-pool preview URL as its no-history fallback and uses `window.history.length > 1`, `event.preventDefault()`, and `window.history.back()` when browser history exists; the generated HTML differs from version 2 only by this handler
- visual_review_after_compliance_repair: not performed; no visual verification claim is made

## 官方事件流

- requested_title: 官方事件流
- current_title: 官方事件流 - 衡策
- draft_id: 09aed568-3137-49c5-9bde-6678e9a04dc8
- canvas_url: https://superdesign.dev/teams/dd3e76e7-a417-46d9-a7de-4c1447246341/projects/72c80b98-e159-48f9-9ed9-e846aa353ae1?node=draft-variant-09aed568-3137-49c5-9bde-6678e9a04dc8
- preview_url: https://p.superdesign.dev/draft/09aed568-3137-49c5-9bde-6678e9a04dc8
- version: 3
- generated_at: 2026-07-23T09:52:00.412Z
- initial_visual_review: unavailable — the required in-app browser backend was not available, so no visual verification claim is made
- repair: one replace repair completed
- compliance_repair_2026-07-23: one additional replace completed; CLI exit code 0
- post_compliance_source_review: `effective_at`, `collected_at`, and `version` show 未记录 in both detail surfaces; `license_policy` shows 未核实; `v1.0.final` and `Official Public Disclosure / Open Data` are absent; the generated HTML differs from version 2 only by these metadata replacements
- visual_review_after_compliance_repair: not performed; no visual verification claim is made

## 数据质量与来源

- requested_title: 数据质量与来源
- current_title: 数据质量与来源 - 条款复核修正
- draft_id: c000e47f-79c0-4343-8e34-49eec2104f22
- canvas_url: https://superdesign.dev/teams/dd3e76e7-a417-46d9-a7de-4c1447246341/projects/72c80b98-e159-48f9-9ed9-e846aa353ae1?node=draft-variant-c000e47f-79c0-4343-8e34-49eec2104f22
- preview_url: https://p.superdesign.dev/draft/c000e47f-79c0-4343-8e34-49eec2104f22
- version: 3
- generated_at: 2026-07-23T09:52:11.185Z
- initial_visual_review: unavailable — the required in-app browser backend was not available, so no visual verification claim is made
- repair: one replace repair completed
- compliance_repair_2026-07-23: one additional replace completed; CLI exit code 0
- post_compliance_source_review: the SourcePolicy table still has exactly six rows and all six 条款复核 cells show 未记录; `2026-01-15`, `2026-03-01`, and `2025-12-10` are absent; no 质量总分, 综合质量, 功能演示数据, or watermark marker is present; the generated HTML differs from version 2 only by the four dated cells becoming muted 未记录 cells
- visual_review_after_compliance_repair: not performed; no visual verification claim is made
