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
- version: 5
- generated_at: 2026-07-23T09:51:59.450Z
- link_status: source-verified
- initial_visual_review: unavailable — the required in-app browser backend was not available, so no visual verification claim is made
- repair: one replace repair completed
- compliance_repair_2026-07-23: one additional replace completed; CLI exit code 0
- post_compliance_source_review: the native `nav-back` anchor keeps the exact strategy-pool preview URL as its no-history fallback and uses `window.history.length > 1`, `event.preventDefault()`, and `window.history.back()` when browser history exists; the generated HTML differs from version 2 only by this handler
- visual_review_after_compliance_repair: not performed; no visual verification claim is made
- responsive_repair_2026-07-23: one replace completed; CLI exit code 0; draft ID unchanged
- post_responsive_source_review: the narrow-rail source now hides the subtitle and the full rail-footer container, centers navigation icons, and keeps `衡策` in a non-wrapping flex row; the research facts, demo watermark, provenance drawer, exact strategy-pool fallback URL, and history handler remain present
- v4_responsive_source_gap: the main element still contained Tailwind `md:ml-[208px]` with no max-1179 CSS margin override, so browser review measured `mainLeft=208` and a wrapped two-line brand at 1100px
- final_review_correction_2026-07-23: one review-driven replace completed; CLI exit code 0; draft ID unchanged
- post_final_correction_source_review: max-1179 `main` now has `margin-left:64px!important`, `width:auto!important`, `min-width:0!important`, `max-width:calc(100vw - 64px)!important`, and `overflow-x:hidden!important`, which overrides the retained Tailwind `md:ml-[208px]`; the 64px brand container is a centered non-wrapping row and the direct `衡策` span has 18px type, 22px line-height, `white-space:nowrap`, `word-break:keep-all`, and zero letter spacing; subtitle and rail footer remain hidden
- final_correction_regression_check: v4 and v5 HTML are byte-identical from immediately after `</style>` through the end of the document, so business data, links, back behavior, watermark, provenance drawer, layout markup, and all non-style content are unchanged
- visual_review_after_responsive_repair: not performed; no visual verification claim is made

## 官方事件流

- requested_title: 官方事件流
- current_title: 官方事件流 - 衡策
- draft_id: 09aed568-3137-49c5-9bde-6678e9a04dc8
- canvas_url: https://superdesign.dev/teams/dd3e76e7-a417-46d9-a7de-4c1447246341/projects/72c80b98-e159-48f9-9ed9-e846aa353ae1?node=draft-variant-09aed568-3137-49c5-9bde-6678e9a04dc8
- preview_url: https://p.superdesign.dev/draft/09aed568-3137-49c5-9bde-6678e9a04dc8
- version: 5
- generated_at: 2026-07-23T09:52:00.412Z
- initial_visual_review: unavailable — the required in-app browser backend was not available, so no visual verification claim is made
- repair: one replace repair completed
- compliance_repair_2026-07-23: one additional replace completed; CLI exit code 0
- post_compliance_source_review: `effective_at`, `collected_at`, and `version` show 未记录 in both detail surfaces; `license_policy` shows 未核实; `v1.0.final` and `Official Public Disclosure / Open Data` are absent; the generated HTML differs from version 2 only by these metadata replacements
- visual_review_after_compliance_repair: not performed; no visual verification claim is made
- responsive_and_idle_state_repair_2026-07-23: one replace completed; CLI exit code 0; draft ID unchanged
- post_responsive_source_review: max-1179 source uses a 64px rail and a `64px !important` main offset, hides the subtitle and full rail-footer container, and centers the brand row and navigation icons; the three `[官方事实]` rows and their official URLs remain present
- post_idle_state_source_review: `当前显示 3 条官方事件` occurs once and `暂无符合条件的官方事件` occurs zero times; verified provenance placeholders remain `未记录` / `未核实`
- responsive_source_note: the generated source does not add explicit `min-width: 0` or `overflow-x: hidden` to `.main-content`; absence of horizontal document overflow therefore still requires rendered-browser verification
- browser_review_feedback_on_v4: at 1100px the brand measured `left=0`, `width=46.8`, and `height=32`; it was horizontal but clipped against the viewport edge
- final_narrow_brand_correction_2026-07-23: one review-driven replace completed; CLI exit code 0; draft ID unchanged
- post_final_correction_source_review: under max-width 1179px the 64px brand container and its direct inner wrapper are centered non-wrapping flex rows with static positioning and no transform; the direct brand span is `inline-block`, width auto, `white-space:nowrap`, `writing-mode:horizontal-tb!important`, 18px type, line-height 1, zero letter spacing, `word-break:keep-all`, static positioning, no transform, and `flex:none`; the `64px !important` main offset and hidden footer remain
- final_correction_regression_check: v4 and v5 HTML are byte-identical from immediately after `</style>` through the end of the document; the idle state still reads `当前显示 3 条官方事件`, the old empty-state text remains absent, and exactly three `[官方事实]` rows remain
- visual_review_after_responsive_repair: not performed; no visual verification claim is made

## 数据质量与来源

- requested_title: 数据质量与来源
- current_title: 数据质量与来源 - 响应式品牌布局修正
- draft_id: c000e47f-79c0-4343-8e34-49eec2104f22
- canvas_url: https://superdesign.dev/teams/dd3e76e7-a417-46d9-a7de-4c1447246341/projects/72c80b98-e159-48f9-9ed9-e846aa353ae1?node=draft-variant-c000e47f-79c0-4343-8e34-49eec2104f22
- preview_url: https://p.superdesign.dev/draft/c000e47f-79c0-4343-8e34-49eec2104f22
- version: 5
- generated_at: 2026-07-23T09:52:11.185Z
- initial_visual_review: unavailable — the required in-app browser backend was not available, so no visual verification claim is made
- repair: one replace repair completed
- compliance_repair_2026-07-23: one additional replace completed; CLI exit code 0
- post_compliance_source_review: the SourcePolicy table still has exactly six rows and all six 条款复核 cells show 未记录; `2026-01-15`, `2026-03-01`, and `2025-12-10` are absent; no 质量总分, 综合质量, 功能演示数据, or watermark marker is present; the generated HTML differs from version 2 only by the four dated cells becoming muted 未记录 cells
- visual_review_after_compliance_repair: not performed; no visual verification claim is made
- responsive_repair_2026-07-23: one replace completed; CLI exit code 0; draft ID unchanged
- post_responsive_source_review: max-1179 source uses a 64px rail, `margin-left:64px !important`, `min-width:0`, and `overflow-x:hidden`; it hides the subtitle and full rail-footer container and centers navigation icons; the exact seven data-domain states, six SourcePolicy rows, six `未记录` review cells, lineage, run/fallback state, refusal records, and inspector fields remain present
- browser_review_feedback_on_v4: at 1100px the brand measured `left=20.3`, `width=23.4`, and `height=64`, confirming a vertical two-line rendering
- final_narrow_brand_correction_2026-07-23: generation returned successfully from one review-driven replace with CLI exit code 0 and the same draft ID; the returned title was `数据质量与来源 - 响应式品牌布局修正`
- post_generation_get_design: unavailable because the approval-layer request ended with a stream-disconnect rejection; it was not retried
- browser_verification_pending: the main agent will verify quality v5 in the browser; no post-correction source or visual verification claim is made here
- last_verified_source_baseline: v4 retained the 64px main offset, `min-width:0`, `overflow-x:hidden`, hidden footer, seven data-domain states, six SourcePolicy rows, lineage, run/fallback state, refusal records, and inspector fields
- visual_review_after_responsive_repair: not performed; no visual verification claim is made
