# Handoff

*For the next Claude session or account. Keep this short and overwrite it at the end of every
significant session.*

**Latest (2026-10-04): chat route renamed and service port changed. Config + routing only; no pipeline change.**
- `/ai-query` → **`/megh-chat`** (`app/main.py`: new `megh_chat()` serves `web/ai_query.html`; `/ai-query` now returns a 301 to it, so bookmarks keep working). The file name `ai_query.html` is unchanged.
- Port **8300 → 8400**: `app/config.py` default, `.env`, `.env.example`, `deploy/systemd/megh-nlpservice.service`, all 9 nginx `proxy_pass` lines, `docker-compose.yml` (host 8401→container 8400), README, DEPLOYMENT, ARCHITECTURE, TESTING, CLAUDE.md §9.
- In-page links updated: `web/admin.html` (2), `web/Meghalaya_UnifiedPortal_UI.html` (3). `web/ai_query.html` `_prefixCandidates()` regex now also matches `megh-chat` — without this the UI would derive the wrong API prefix when served from a sub-path.
- Verified offline (VPN down) with `starlette.testclient`: `/megh-chat` 200 (serves the chat page), `/ai-query` 301 → `/megh-chat`, `/admin-ui` 200, `/` 200, unknown path 404, `settings.PORT == 8400`. Tests: 90 passed (`test_ac_full_results`, `test_asr_transcribe`, `test_history_charts`, `test_pipeline_graph`).
- **Deploy note:** the VMs need the new `.env` PORT, the new nginx conf and the new systemd unit, then `systemctl daemon-reload` + `nginx -t` + reload. Not done from here.

**Latest (2026-10-03, afternoon): Qdrant moved to a new server. Config only, no code change. KB INGESTED and verified.**
- `.env` `QDRANT_URL` changed `http://10.48.242.4:6333` → `http://115.124.102.167:6335` (collection names unchanged: `megh_scheme_kb`, `megh_conversation_memory`). `.env.example`, `deploy/DEPLOYMENT.md`, `CLAUDE.md` §9 updated.
- New server checked read-only: Qdrant 1.18.3 (client 1.19.0, compatible); reachable without the VPN; **no API key** (KI-200); holds six `Metadata_*` collections (1024-dim schema metadata, not ours, not used: our KB is 384-dim bge-small) — never touch them.
- **Done (2026-10-03):** `ingest_kb` built `megh_scheme_kb` on the new server: 201 points from 12 docs, 384-dim, five scheme tags (CM Elevate Legacy maps to CM Elevate by design). `megh_conversation_memory` created (384-dim, empty). Retrieval checked per scheme (top cosine 0.77–0.85). The `Metadata_*` collections are unchanged (e.g. Metadata_MGNREGA 360, Metadata_CMElevate 917 points).
- **Still open:** the user sets `QDRANT_API_KEY` (KI-200); the two VMs' `.env` need the same `QDRANT_URL`; a full KNOWLEDGE answer end to end needs the VPN (the composer model is on 10.48.242.4).
- **The old Qdrant (10.48.242.4:6333) is retired for this app:** no code, config default or doc points to it. `app/config.py` default `QDRANT_URL` is now `http://115.124.102.167:6335`.

**Latest (2026-10-03, early morning): all six schemes × every place, all failures fixed. Code changed, uncommitted. LIVE-VERIFIED.**
- Result: 30,280 / 30,280 live bulk questions pass (see SCHEMES.md "All-places QA"); reports in `docs/*_LangGraph_Test_Results*.xlsx`.
- Code: `app/pipeline.py` only — `_VILLAGE_NARROW_SCHEMES` (5 schemes), `_focus_legacy_village_names`, `_cm_legacy_village_names`,
  `_village_filter_missing`, `_pin_missing_village_code` → `_village_name_cond_to_code` / `_drop_geo_beside_pinned_village` /
  `_PINNED_GEO_COND`, `_GEO_BESIDE_VILLAGE_RE` (UPPER/LOWER/TRIM), `_cm_unasked_programme_beside_village` (non-programme literal),
  `_focusplus_lgd_only_block`, `_FPL_BLOCKS`, `_cm_elevate_applicants_distinct`, `_NUMBER_TENS`, `_cml_fix_place_spelling`,
  `_YEAR_AFTER_CHIP_TAIL_RE`, `_mgnrega_village_not_out_of_area` (BURMA; both Legacy schemes for MANIPUR), `_pmay_two_villages`.
- Tests changed with reasons (CML is now a village scheme): `test_focusplus_usecase_fixes.py`, `test_mgnrega_usecase_fixes.py`,
  `test_focus_legacy_usecase_fixes.py`; new tests in those and `test_cm_elevate_legacy.py`, `test_cmelevate_usecase_fixes.py`, `test_pmay_usecase_fixes.py`.
- Tests: pytest **1,453 passed, 0 failed** (27 files) with the graph off AND on; scripts **14/14**; live context suite **43/43 on both paths** (follow-up 29/29).
- Harness: scratchpad `7be462aa-…/scratchpad/bulk` (`bulk.py gen|run|judge [--only-failed f --out f]`, `pmay_sets.py`, `cme_sets.py`,
  `make_reports.py <docs dir>` with system Python + openpyxl). Background jobs die at 2 h: run villages in batches (resumable).
- Next: canary (G5); decide KI-184/185; broaden QA beyond one shape per place (KI-199); commit when the user asks.

**Latest (2026-10-02, evening): live verification + fixes. Code changed, uncommitted. LIVE-VERIFIED.**
- Live: context suite 43/43 both paths; G4 36/36 identical; OFF-009 re-test 49/49 both paths; 21/21 nodes ran live.
- Fixed: KI-187 (`_verifier_unlisted_geo_is_false`), KI-182 (`_cme_asked_programmes`, `_cme_all_zero_answer`, `_cme_multi_scheme_total`), KI-183 (`_repaired_sql_denial`, `RepairedSQLDenied`, `execute_with_repair(scope=)`, graph `node_execute_attempt`; D-033); stale tests in `test_focus_legacy_usecase_fixes.py` / `test_mgnrega_usecase_fixes.py`.
- Tests: pytest 1,419 passed, 0 failed, flag off and on; scripts 14/14.
- Harnesses (session scratchpad `7be462aa-…/scratchpad`): `live_q.py` (ad-hoc live questions), `g4.py` (six-scheme old-vs-graph), `off009.py [graph]` (OFF-009 vs DB), `g4cov.py` (live node coverage), `live_q_orig.py` (runs the pre-refactor `pipeline_orig.py` for A/B). Set `DB_POOL_MIN_SIZE=2 DB_POOL_MAX_SIZE=6`.
- GOTCHA: writing Python through a bash heredoc turned `\\b` into a literal backspace (0x08) in regexes — silently matching nothing. Use the Edit tool or a script file; check with `chr(8) in source`.
- Next: canary (G5); decide KI-184/185; optionally re-run the full OFF-009 1,260 set; commit when the user asks.

**Earlier (2026-10-02): LangGraph orchestrator (D-032). Code changed, uncommitted. OFFLINE-VERIFIED ONLY (VPN down all session).**
- Read first: `docs/LANGGRAPH_ARCHITECTURE.md`, `docs/LANGGRAPH_PAUSE_RESUME.md`, `docs/LANGGRAPH_MIGRATION.md` (rollout gates G1–G8).
- Files:
  - `app/pipeline.py`: the verbatim stage split (`_turn_*_stage`, `_data_*_stage`, `_scheme_sql_rewrites`,
    `_execute_one_attempt`, `_repair_sql`, `_data_path_error_result`, `_update_context`, `MAX_SQL_REPAIRS`,
    `_pipeline_graph_selected` in `answer_question`);
  - new `app/pipeline_graph.py`;
  - `app/config.py` (8 `PIPELINE_GRAPH_*` / `LANGGRAPH_*` flags);
  - `app/main.py` (pause sweep + checkpointer close + `/health` component, only when the graph is in use);
  - `requirements.txt`, `requirements.lock`;
  - new `tests/test_pipeline_graph.py` (27);
  - `tests/test_mgnrega_usecase_fixes.py` (one source pin moved to `_data_clarification_stage`).
- The split was done with a splice script (scratchpad `7be462aa-…/scratchpad/splice.py`, from `pipeline_orig.py`),
  so every moved body is byte-for-byte the original. Live-in variables per boundary were checked with `ast` (`live.py`, `undef.py`).
- Tests:
  - baseline 1,371 passed / 2 failed; after the split, the same;
  - flag off 1,398 / 2; flag on 1,397 / 3 (one test fixed since). NOTE: no existing test calls
    `answer_question`, so only `test_pipeline_graph.py` exercises the graph;
  - `test_pipeline_graph.py` 27/27 in both modes; scripts 14/14.
  The 2 failures are the pre-existing verifier tests.
- NEXT (in order):
  1. With the VPN up: `PIPELINE_GRAPH_ENABLED=true .venv/Scripts/python.exe tests/live_context_validation.py` (expect 43/43),
     plus a per-scheme use-case sample flag on vs off (G3, G4).
  2. Decide KI-183 (re-authorize repaired SQL).
  3. Canary 10% on one VM (G5).
  4. Commit when the user asks.
- Gotchas:
  - LangGraph reads a node's *unwrapped* signature: never set `__wrapped__` on `_observed` nodes (it stops
    passing `runtime`). The wrapped function is exposed as `.stage_fn`.
  - `from tests.test_pipeline_graph import *` skips the `_loaded` fixture (underscore), and the resolver
    catalogue is then empty: import it explicitly.

**Latest (2026-10-01): CM Elevate OFF-009 all pairs — TEST ONLY, no code changed.** 1,260 questions (12 districts x 105 programme pairs): PASS 552, PASS-with-remark 141 (both 0 → 'no matching records'), FAIL 567 (one programme 0 → omitted, KI-182). Bot SQL data correct 1,260/1,260; raw = DB. Report docs/CM_Elevate_OFF009_AllPairs_2026-09-30.xlsx. On 2026-09-30 the gateway was down (timeouts) and its TLS cert failed verification; both fine on 2026-10-01.

**Latest (2026-09-29, morning): typed replies resume every chip pause (KI-181). Code changed, uncommitted. LIVE-VERIFIED.**
- Files: `app/routers/query.py` (`remember_pause` generic branch, uses `SCOPE_MERGE_RULES`), `app/pipeline.py` (`SCOPE_MERGE_RULES`,
  `_resume_option_pause`, `_option_tokens`, `_paused_thread_antecedent`, step 0a'' in `_run_pipeline`, `_paused_state` thread override,
  measure-gap pause remembered as its first offer), `tests/test_context_relevance_and_contract.py` (+33, now 177).
- Tests: pytest 1,371 passed, 2 failed (same parallel-session verifier tests as below); scripts 14/14; live context 43/43.
- Open: an unmatched reply with its own measure + place but no year keeps the paused scheme, not the paused year (FY pause follows).
- Next: restart :8300; commit when the user asks.

**Latest (2026-09-29, early morning): "give me for pmay" — conversational scheme swap (KI-180). Code changed, uncommitted. LIVE-VERIFIED.**
- Files: `app/pipeline.py` (`_SCHEME_SWAP_FOLLOWUP` request-verb opening, `_SUBSTITUTION_CUE`, new `_SCHEME_OWN_MEASURES`,
  `_SCHEME_HEADLINE_OFFERS`, `_swap_scope_phrase`, `_swap_measure_gap`, one call after the follow-up rewrite in `_run_pipeline`),
  `tests/test_context_relevance_and_contract.py` (+26, now 144).
- Tests: pytest 1,338 passed, 2 failed — both pin `_verifier_village_code_complaint_is_false` and were broken by a PARALLEL
  CM Elevate Legacy session editing `pipeline.py` at the same time (it added `["CM Elevate Legacy"]` to that gate); not KI-180.
  Scripts 14/14; live context 43/43; live replay via scratchpad `119975e8-…/scratchpad/live_convo.py`.
- Open: a bare "for focus" -> which-Focus chip -> Focus Plus with an MGNREGA-only measure still reaches the generator (not gated).
  (The pause is now remembered — KI-181, entry above.)
- Next: restart :8300; commit when the user asks.

**Latest (2026-09-29, late night): CM Elevate Legacy KI-166..181 fixed — use cases 36/36, all levels 2,614/2,614. Code changed, uncommitted. LIVE-VERIFIED.**
- Files: `app/pipeline.py` (CM Elevate Legacy SQL guards `_cm_legacy_sanctioned_count`, `_cm_legacy_unrequested_limit`,
  `_cm_legacy_unselected_group_by`; `_cm_legacy_answer_guarantees` and its `_cml_*` helpers; chip pin / twin ranking /
  urban blocks / verifier filter / Garo gate extended to CM Elevate Legacy; KI-179 level word in the explicit-village
  step, all schemes), `tests/test_cm_elevate_legacy.py` (+48 tests, 131).
- Reports: `docs/CM_Elevate_Legacy_UseCase_Test_Report_2026-09-29_v2_after_fixes.xlsx`,
  `docs/CM_Elevate_Legacy_AllBlocks_AllVillages_Test_Report_2026-09-29.xlsx` (+ evidence folders).
- Harness: scratchpad `d746a809-…/scratchpad/bulk` — `gen_bulk.py` (truth from DB AND raw, system python),
  `bulk_run.py` (.venv; set BOTH `DB_POOL_MIN_SIZE=2 DB_POOL_MAX_SIZE=6` — a max below the default min 10 kills the
  pool), `bulk_judge.py`, `build_bulk.py`, `issues.json`; `trace_sql.py` logs failing SQL.
- Tests: pytest 1,309 (26 files), scripts 14/14, live context 41/43 + scenario G 5/5 twice (transient save-failed).
- Next: RESTART :8300; commit when the user asks; KI-003 (failing SQL not logged) made the KI-181 hunt slow.

**Earlier (2026-09-29, late night): CM Elevate Legacy use-case re-test — 33/36. No code changed.**
- The user said "Focus legacy" but named the CM Elevate Legacy files, so CM Elevate Legacy was tested (36 cases, 2 full runs + 3 repeats).
- DB = raw row for row (2,823). Failures, all AI layer and reproducible: KI-166 (TC-14 sanctioned = COUNT(*) → 2,823 vs 2,820, 5/5),
  KI-167 (TC-34 composer "20 villages with 1 record" vs 24, 5/5), KI-168 (TC-13 LIMIT 10 → "down to 33", 4/5).
- Report `docs/CM_Elevate_Legacy_UseCase_Test_Report_2026-09-29.xlsx` (+ `_Evidence_2026-09-29/`).
- Harness: scratchpad `d746a809-…/scratchpad` — `run_bot.py OUT [TC…]` (.venv), `ground_truth.py` and `recon.py` (system python; the raw
  file is aggregated only — reading its rows is blocked as PII), `judge.py`, `verdicts.py`, `fails.py`, `build.py`.
- Next: fix KI-166..168 with deterministic guards + regression tests (user has not asked yet), then re-run the 36.

**Latest (2026-09-29, night): Focus Legacy all blocks / villages / ACs / PGs — 32,560/32,560. Code changed, uncommitted. LIVE-VERIFIED.**
- Files: `app/pipeline.py`, `app/entity_resolver.py` (`constituency_contents(ac, scheme)`), `app/edge.py`
  (`_mask_group_name`), `tests/test_focus_legacy_usecase_fixes.py` (122), 3 older tests updated with reasons
  (`test_admin_level_collision.py` fake signature, `test_mgnrega_usecase_fixes.py` verifier scope,
  `test_ac_flow_focus_legacy.py` docstring).
- Tests: pytest 1,265; scripts 14/14; live context 43/43; use cases 28/28 x3 (final code).
- Reports: `docs/Focus_Legacy_AllBlocks_Villages_ACs_PGs_Test_Report_2026-09-29.xlsx`,
  `docs/Focus_Legacy_UseCase_Test_Report_2026-09-29_v2_after_fixes.xlsx` (+ evidence folders).
- Harness: scratchpad `2ca0226e-…/scratchpad/bulk` — `gen_bulk.py` (truth from DB AND raw), `bulk_run.py`
  (concurrent, resumable, clicks chips), `bulk_judge.py`; run shards in parallel with `DB_POOL_MAX_SIZE=5`.
- Next: CM Elevate Legacy duplicate-village chips (KI-156) and PMAY-G DATE_TRUNC (KI-164) — both NOT VERIFIED;
  KI-151 under load; RESTART the :8300 server to pick the changes up; commit when the user asks.

**Latest (2026-09-29, evening): Focus Legacy use-case re-test — 28/28 on 3 of 3 runs. No code changed.**
- Checked against the live DB (`curated.v_focus_legacy`, never the privacy table) and the raw file;
  DB = raw row for row. Report `docs/Focus_Legacy_UseCase_Test_Report_2026-09-29.xlsx` (+ `_Evidence_2026-09-29/`).
- New cosmetic issues KI-145..147 (open). Harness: scratchpad `2ca0226e-…/scratchpad`
  (`bot_run.py`, `judge.py`, `expected.py`, `recon.py`, `verdicts.py`, `build.py`; Excel COM for recalc).

**Latest (2026-09-29, later): the reported "what is focus" conversation — KI-136 to KI-144. Code changed, uncommitted. LIVE-VERIFIED (VPN up).**
- Fixes: `_names_bare_focus_scheme` (no model rewrite for a bare scheme "Focus"); `_drop_inherited_time` /
  `_drop_inherited_place` (KNOWLEDGE path); `rewrite_violation` keeps the follow-up's own metric;
  `_followup_thread_state` + `_data_thread_antecedent` (which thread a follow-up continues — never a
  state wipe, see D-030 amendment); `_measure_after_knowledge` + `_typed_intent`;
  `turn_context["standalone_question"]` read by `routers.query.pause_question`;
  `ConversationState.last_dimension` + "all of them" CLEAR + `_all_years_rewrite`; Focus Plus
  amount answer guarantee; `build_followup_context(state=...)`.
- Tests: pytest 1,185; scripts 14/14; live context suite 43/43 (final code); live battery S1–S8 correct.
  Harness: scratchpad `5a4188f1-…/scratchpad/live_convo.py` (real lifespan, router pause helpers;
  "@chip:<label>" clicks a chip), `battery.sh`, `battery2.sh`.
- Open: KI-040 (MGNREGA "beneficiaries" = SUM(persons_employed) across years, person-years — needs an
  SME definition); after 2+ edge turns a bare "how many beneficiaries?" asks "which scheme?" (safe, by
  choice); bulk false-positive rate of `_resolved_scope_missing` not measured.
- Next: commit when the user asks; a Focus Plus / MGNREGA block-village bulk sample to measure the guard.

**Earlier (2026-09-29): context relevance + semantic contract (D-030). Offline at the time; live-verified by the later session.**
- The VPN was down all session (`10.48.242.4` answered neither 5432 nor 443), so **nothing was
  live-verified**. FIRST NEXT STEP when it is up: `tests/live_context_validation.py` (43 checks),
  then the reported conversation live (see KNOWN_ISSUES KI-130 to KI-135), then a Focus Plus and a
  MGNREGA block/village regression sample to measure `_resolved_scope_missing` false positives.
- Changed: `app/context_policy.py` (`continuation_signals`, `is_bare_reference`),
  `app/pipeline.py` (step 0-c gate, `reference-ambiguous`, step 1g `_village_name_search_answer`,
  `_acronym_near_miss_clarification`, Focus Plus `_focusplus_stated_amount*`,
  `_resolved_scope_missing` guard, `inject_year_scope` call, decision logs), `app/edge.py`
  (`personal_request`, `has_domain_vocabulary`), `app/entity_resolver.py`
  (`acronym_near_misses`), `app/premise_check.py` (`stated_amount_filters`, number words),
  `app/context_manager.py` (`inject_year_scope`, KI-032 clears, `year_all`),
  `app/session_store.py` (`year_all`), `app/context_budget.py` (`log_decision`); new
  `tests/test_context_relevance_and_contract.py` (96).
- Tests: pytest 1,163 (26 files); scripts 14/14. Offline harness:
  scratchpad `5a4188f1-…/scratchpad/repro_routing.py` (real `_run_pipeline`, model/RAG/DATA stubbed).
- Not fixed, still open: KI-035, KI-040 (MGNREGA "beneficiaries" is undefined, which is what
  "same for mgnrega" will answer with), KI-036/037 (Songsak-style block/village level memory; tests
  5–7 of the request rely on the existing level gates and were not live-checked).

**Latest (2026-09-29, later): PMAY-G plain year = calendar year.** "during 2017" now answers calendar 2017 (EKH 1,419 houses; table + SQL agree) with FY 2017-18 (1,548) as a one-line note; explicit FY unchanged. pytest 1,266; use cases 71/71; follow-ups 18/18. Restart :8300.

**Latest (2026-09-29): KI-129 — PMAY-G result table / Sources.** The table under an answer now holds only the place and the asked figures (`_pmay_display_rows`); "Sources: TRUE" gone (SQL + UI). Live: follow-ups 18/18, use cases 71/71; pytest 1,067. Restart :8300.

**Latest (2026-09-28, night): PMAY-G extra information removed (KI-128).** Summaries list only the use-case figures; single figures carry no side details; rewritten follow-ups answer only the figure the user typed (`_PMAY_TYPED_TURN`). Live: 2,129-question sample 2,129/2,129, use cases 71/71, follow-ups 18/18; pytest 1,065. Restart :8300.

**Latest (2026-09-28, late): PMAY-G tester sheet (Test Case Results - 21st Sep 26 - PMAY-G) re-tested.** Every input the tester named is now correct vs DB/raw (GABIL SONGGITCHAM partial 83; no KYNDONGTUBER mix-up; one row per status; LASKEIN 5,232). Live: 192/192 (21 Sep inputs, all districts x ben/summary/performance/each FY, 63 dates) + use cases 71/71. Fixed KI-125 (bare year "sanctioned in 2023" was dropped → now FY 2023-24 + calendar-2023 line). Open decisions: KI-126 (two taps for a shared village name) and KI-127 (placeholder rows in beneficiary counts: tester's 5,251 vs 5,232).

**Latest (2026-09-28, later): CM Elevate status = current_file_status (KI-123, user decision).** Prompt rule 13 + vocabulary, 2 few-shots, guard `_cm_elevate_status_is_file_status`. Pending / verification / on hold unchanged. Tester questions 36/36; pytest 1,062.

**Previous (2026-09-28, late): CM Elevate tester sheet (Test Case Results - 21st Sep 26) re-tested.** 36 tester-phrased questions all correct vs DB/raw after two fixes (KI-121 verifier grain false positive, KI-122 sector wording); pytest 1,055. Open decisions: KI-123 (testers want current_file_status for status/pending) and KI-124 (applicants = distinct request_id vs raw row count).

**Latest (2026-09-28, evening): CM Elevate — remaining issues + all districts / blocks / villages. Code changed, uncommitted.**
- Final pass **7,364 / 7,364** (7,364 questions = 12 districts, 66 blocks, 2,087 villages x 3 phrasings x the use-case types); round 1 4,132/4,208. Report `docs/CM_Elevate_AllBlocks_AllVillages_Test_Report_2026-09-28.xlsx`.
- KI-074 DECIDED by the product owner: pending = verification On Hold only (interim line removed). KI-076 fixed (`_APPLICANT_PLACE_CUE`).
- KI-106..120: CM Elevate in `_village_scheme` / `_VILLAGE_NARROW_SCHEMES` (village catalogue `_cm_elevate_village_names`); urban bodies mapped to stored `lgd_block` (`_cm_elevate_blocks_to_data`, `_cm_elevate_block_from_mention`, urban step at the top of `resolve_entities`); SQL guards `_cm_elevate_fix_literals`, `_cm_elevate_sector_all_programmes`, `_cm_elevate_unasked_programme_filter`; `_focusplus_pin_village_where` literal/FILTER-aware (`_top_level_where_span`); chip pin keeps the scheme's twin; verifier filters; number words → digits before the checks.
- Three older tests that pinned CM Elevate OUTSIDE the village gate were updated (reason in each).
- Tests: pytest 1,053; context suite 43/43. Harness: scratchpad `63ff2b45-…/scratchpad/bulk` (gen_bulk, bulk_run, bulk_judge, make_extra, build_bulk). RESTART the :8300 server.

**Latest (2026-09-28, late night): PMAY-G full scenario test — 17,331/17,331. Code changed, uncommitted.**
- 34 first-pass failures → KI-099 to KI-104 fixed; the final pass found KI-105 (model-path crore rounding), fixed and the
  571 model-reachable questions re-run 571/571. Follow-ups 18/18, use cases 71/71, context 43/43, pytest 1,005.
- Report `docs/PMAY_G_Full_Scenario_Test_Report_2026-09-28.xlsx` (+ `_Evidence_2026-09-28/`). RESTART the :8300 server.

**Previous (2026-09-28, night): KI-098 — PMAY-G village named beside its block. Code changed, uncommitted.**
- User report: "…in NONGSOHRAM across all financial years, RI MULIANG block, WEST KHASI HILLS" answered for
  the whole block. Fix: `_pmay_village_beside_block` at the end of `resolve_entities` (PMAY-G only; reads
  an explicit ", X block, DISTRICT" tail from the text; one mention written "X block" stays the block).
- Live: 5,120/5,120 villages with the tail, 224/224 blocks, 500/500 bare villages, 71/71 use cases;
  pytest 998. The running server on :8300 must be RESTARTED to pick the change up.

**Previous (2026-09-28, evening): PMAY-G fixes + all blocks / all villages. Code changed, uncommitted.**
- **Result:** use cases **28/28** (71/71 questions on 2 of 2 fresh live runs, identical answers; round 1 was 17/28); all blocks **224/224**; all villages **5,120/5,120** on the final code (round 1 4,393/5,120 → KI-096 fixed).
- **Design (D-029):** `_pmay_facts_query` / `_pmay_facts_answer` answer the fixed PMAY-G shapes
  deterministically; the model path keeps the rest with `_pmay_sql_issue`,
  `_pmay_comparison_limit`, `_pmay_rupee_format`. PMAY-G is now in `_village_scheme`
  (`_pmay_village_names`). Resolver: "sanctioned" stage phrases match in order only.
- **Tests:** `tests/test_pmay_usecase_fixes.py` (53); pytest 993; scripts 14/14; live context 43/43;
  Focus Plus regression 60/60 + 300/300; MGNREGA 60/60 + 300/300. Four older tests that pinned
  PMAY-G outside the village gate / out-of-area check were updated, with the reason in each.
- **Reports:** `docs/PMAY_G_UseCase_Test_Report_2026-09-28_v2_after_fixes.xlsx` (+ `_Evidence_…_after_fixes/`),
  `docs/PMAY_G_AllBlocks_AllVillages_Test_Report_2026-09-28.xlsx` (+ `_Evidence_2026-09-28/`).
- **Next step:** commit when the user asks. Keep the FY pause (product decision). The harness is in
  scratchpad `9d9b9d7b-…` (gen_bulk, bulk_run, bulk_judge, build_bulk, run_bot, judge, verdicts_after,
  build_xlsx ROUND=2).

**Previous session (2026-09-28, later): PMAY-G use-case QA. No code changed.**
- **Result:** **17 / 28 test cases** (49 / 71 questions; two live runs; a question passes only if
  both pass). The user said "Focus legacy" but named PMAY-G files (`PMAY-G.csv` = use cases,
  `PMAY_FullyMapped_with_dates.csv` = raw), so PMAY-G was tested.
- **DB = raw** row for row (171,107 rows).
- **Report:** `docs/PMAY_G_UseCase_Test_Report_2026-09-28.xlsx`, evidence
  `docs/PMAY_G_UseCase_Evidence_2026-09-28/` (73 PNGs; 016a and 022b also have `_runB`).
- **Issues found:** KI-089 to KI-095 (all fixed later the same day — see the session above). The biggest was KI-089
  (per-year GROUP BY for "financial summary" and comparisons), then KI-091 (false "House
  Sanctioned stage").
- **Not a failure:** the FY pause on no-year PMAY-G questions is a 2026-09-09 product decision.
- **Next step:** fix KI-089 to KI-095 (PMAY-G only, prompt + few-shot + deterministic guard), add
  `tests/test_pmay_usecase_fixes.py`, re-run the 71 questions.
- **Harness:** session scratchpad `9d9b9d7b-…/scratchpad` holds `cases.py`, `frames.py`,
  `recon.py`, `expected.py`, `run_bot.py`, `judge.py` (money within stated rounding and 1%),
  `verdicts.py`, `render.py` and `build_xlsx.py`. The runner and renderer use the `.venv` python;
  pandas and openpyxl steps use the system python. Raw names are dropped at load.

**Previous session (2026-09-28): KI-025 fixed; data package for KI-065 / KI-084. Code changed, uncommitted.**
- **KI-025:**
  - `db.DatabaseUnavailableError` and `db.is_connection_error()` (connection-class errors; not
    TimeoutError);
  - `execute_with_repair` raises it before any repair;
  - `_run_pipeline` re-raises it, with no KB fallback;
  - the router returns 503.
  - Tests: 3 in `tests/test_focusplus_usecase_fixes.py`. Live-verified with a real unreachable
    DB port.
- **KI-065 / KI-084:** data-side, so the chatbot must not change megh_db. The ingestion team has
  the row-level package `docs/Focus_Plus_Data_Corrections_for_Ingestion_2026-09-28.xlsx`
  (README, rows, per-village summary, read-only verification SQL). Next: their decision. No
  chatbot change is needed after they fix it.
- **Tests:** pytest 939; scripts 14/14; live context 43/43.

**Previous session (2026-09-27 night): Focus Plus all blocks and all villages. Code changed, uncommitted.**
- **Result on the final code:** blocks **204/204**; villages **7,026/7,026**.
  - Confirmation pass: 204/204 and 1,000/1,000 random villages.
  - MGNREGA regression: 448/448 and 300/300.
  - Report: `docs/Focus_Plus_AllBlocks_AllVillages_Test_Report_2026-09-27.xlsx`.
- **Fixes (all in `app/pipeline.py`; KNOWN_ISSUES KI-079 to KI-088; D-028):**
  - The MGNREGA village guards (KI-049 to KI-059) are gated on `_village_scheme(schemes)`, which
    returns MGNREGA exactly as before, or Focus Plus when it is the only scheme.
  - Focus Plus adds `_focusplus_village_names`, `_focusplus_narrow_village`,
    `_focusplus_pin_village_where` and `_focusplus_alias_district_collision`.
  - The twin-village chip tag is `_village_code_tag`.
  - Also: the "Nan" block, BURMA, and the one-figure misquote rule in `compose_response`.
- **Tests:** `tests/test_focusplus_usecase_fixes.py` (53). Two MGNREGA tests that pinned
  MGNREGA-only gates were updated, with the reason in the test.
- **Another session edited the same files in parallel** (CM Elevate, calendar dates: KI-068 to
  KI-078, D-027). Numbering was coordinated by reading the docs first.
- **Next step:** commit when the user asks. Remaining data-side items for the ingestion team are
  KI-065 and KI-084. The bulk harness (`gen_fp_bulk.py`, `fp_bulk_run.py`, `fp_bulk_judge.py`,
  `build_fp_bulk.py`) is in the session scratchpad; the method is on the report's Summary sheet.

**Earlier (2026-09-27): calendar date read as a financial year (KI-078). Code changed, uncommitted.**
- **The user's screenshot:** "How many PMAY houses were sanctioned on 2017-11-28?" gave 504
  (correct, checked in megh_db), but the answer said "not the 11 or 28 figures assumed" and
  "applies to FY 2017-18".
- **Fixed (only this; nothing else changed):**
  - `app/pipeline.py`: `_EXPLICIT_FY_RANGE_RE` no longer matches the head of a date;
    `_needs_scope_clarification` accepts a full date as the time pin (`_CALENDAR_DATE_RE`).
  - `app/premise_check.py`: `extract_premises` blanks dates (`_DATE_RE`).
- **Tests:** new `tests/test_calendar_date_filter.py` (18); pytest 915 (24 files); scripts 14/14;
  live context suite **43/43** (follow-up 29/29, SQL 20/20); live probes 4/4.
- **Next:** commit when the user asks.

**Earlier session (2026-09-27): MGNREGA women-share fix (KI-077). Code changed, uncommitted.**
- **The user's screenshot:** "What percentage of employment persons were women in ekh?" came back
  as 0.00%, with no year named.
- **Fixed in `app/pipeline.py`, MGNREGA only:**
  - the year gate now covers percentage, share and women questions, and women chips offer only
    the years that carry women data;
  - a deterministic women query uses the recorded years and says which;
  - FY 2025-26 is answered "not recorded";
  - generator SQL over all years is restricted to the recorded years.
- **Details:** KNOWN_ISSUES KI-077 and AI_PIPELINE.
- **Tests:** 897 pytest, 14/14 scripts, live context 43/43, 8/8 live women checks.
- **Next:** commit when the user asks.

**Earlier session (2026-09-27, later): CM Elevate use-case QA, then fixes. Code changed, uncommitted.**
- **Result:**
  - Round 1: 23 / 30 test cases (47 / 55 questions).
  - After fixes: **30 / 30** (55 / 55) on 2 of 2 fresh live runs, with identical answers.
  - Every figure was checked against megh_db and the raw workbook, which are identical (8,627
    rows).
  - The user said "Focus legacy" but named CM Elevate files, so CM Elevate was tested.
- **Reports:**
  - round 1: `docs/CM_Elevate_UseCase_Test_Report_2026-09-27.xlsx`;
  - after fixes: `docs/CM_Elevate_UseCase_Test_Report_2026-09-27_v2_after_fixes.xlsx`, with
    evidence in `..._Evidence_2026-09-27_after_fixes/`. It keeps the round-1 status as a column.
- **Files:**
  - `app/pipeline.py`:
    - `_UNRESOLVED_PLACEHOLDER_SCHEMES` now includes CM Elevate;
    - `_CMELEVATE_ONLY_TERMS` now includes PRIME SEED;
    - five `_cm_elevate_*` SQL rewrites run in `execute_with_repair`;
    - the `_cme_*` guarantees run through `_cm_elevate_answer_guarantees` after the composer;
    - new verifier filter `_verifier_scheme_specific_complaint_is_false`.
  - `app/schema_context.py`: the CM Elevate vocabulary now covers pending at a level, status by
    sector, approved/rejected on `file_status`, and the `file_status` key.
  - `tests/test_cmelevate_usecase_fixes.py` (33 tests).
  - Docs: AI_PIPELINE §2.8 and §2.9, D-027, KNOWN_ISSUES KI-068 to KI-076, TESTING, SCHEMES,
    CURRENT_STATE, and the CLAUDE.md baseline.
- **Tests:** pytest 891 passed; scripts 14/14; live context suite 43/43. 11 reworded probes gave
  10 correct and 1 intent flake (KI-076, pre-existing).
- **Scope:** every change is gated on `schemes == ["CM Elevate"]`. The only shared-code
  touches are the Unresolved-scheme tuple and the CM Elevate term regex. No YAML was edited.
- **Next step:**
  - Commit when the user asks.
  - **Ask the product owner which "pending" is wanted (KI-074)**: verification On Hold (953)
    or file status Pending (8,472). If they choose file status, change the vocabulary line and
    drop `_cme_pending_file_status`.
  - Consider KI-076.
- **Harness:**
  - the scratchpad `63ff2b45-…/scratchpad` holds cases, expected, judge, run_bot, render,
    build_xlsx (`ROUND=2`), verdicts and `verdicts_after`, plus the `probe/` folder;
  - run passes sequentially with `DB_POOL_MIN_SIZE=2 DB_POOL_MAX_SIZE=6`;
  - read every answer, because the judge misses grouped lists and top-3 answers.

**Latest session (2026-09-27, continued): Focus Plus fixes. Code changed, uncommitted.**
- **Result: 30 / 30** (39 / 39 questions) on 2 of 2 fresh live runs after fixing KI-060 to
  KI-064, plus two bugs found along the way:
  - KI-066: in all schemes, a typed reply that resumed a pause and then paused again was
    remembered as the bare reply;
  - KI-067: a Focus Legacy bank question raised `KeyError`.
- **Report:** `docs/Focus_Plus_UseCase_Test_Report_2026-09-27_v2_after_fixes.xlsx`, with
  evidence in `docs/Focus_Plus_UseCase_Evidence_2026-09-27_after_fixes/`. It keeps the round-1
  status as a column.
- **Files:**
  - `app/pipeline.py`: `_focusplus_answer_guarantees` and its `_fp_*` helpers,
    `_fix_digit_grouping`, `_sql_literal_numbers`, `_bank_clarification` (chips and a Focus
    Legacy entry), `SCHEME_PAUSE_RULES`, `_needs_scope_clarification`, and turn_context
    `resumed_question`;
  - `app/routers/query.py`: `pause_question`;
  - `tests/test_focusplus_usecase_fixes.py` (32 tests);
  - docs: AI_PIPELINE §2.9 / §5.2, D-026, KNOWN_ISSUES, TESTING, SCHEMES, CURRENT_STATE and
    CLAUDE.md baselines.
- **Tests:** pytest 858 passed; scripts 14/14; live context suite 43/43.
- **Scope:** the answer guarantees are gated on Focus Plus. The digit-grouping fix, the
  SQL-literal numbers and `pause_question` apply to every scheme (D-026 explains why).
- **Next step:** commit when the user asks. Consider extending `_fp_comparison` and the
  shares to other schemes if their QA shows the same gaps. KI-065 is waiting on the data team.

**Earlier the same day: Focus Plus use-case QA, round 1. No code changed.**
- **Result: 19 / 30 test cases pass** (27 / 39 questions). Two live runs agreed on every verdict.
  Every figure was checked against megh_db and the raw `Focus Plus Master.csv`.
  - The user said "Focus legacy" but named the Focus Plus files, so Focus Plus was tested.
- **Report:** `docs/Focus_Plus_UseCase_Test_Report_2026-09-27.xlsx`:
  - sheets Summary, Test Results, Question Detail, DB vs Raw and Evidence;
  - the Evidence sheet embeds `docs/Focus_Plus_UseCase_Evidence_2026-09-27/*.png`.
- **New open issues:**
  - KI-060: no percentages;
  - KI-061: no difference or "which is higher";
  - KI-062: the composer printed "1,0263";
  - KI-063: the no-chip "which bank" clarification loses the question;
  - KI-064: wording;
  - KI-065: a data-side district difference. The data-team note is `docs/Focus_Plus_DB_Issues.md`.
- **Next step (done later the same day):** fix KI-060 to KI-064 and re-run the 39 questions. The harness scripts (cases, judge, run_bot, render, build_xlsx) are
  in the session scratchpad, not the repo, and the method is on the report's Summary sheet.
- **Harness lessons:**
  - A question that pauses for "which area?" needs the "All of Meghalaya" chip, not the first
    district.
  - Set `PYTHONIOENCODING=utf-8`, or the "₹" in an answer crashes the runner's print.
  - Fetching the whole `v_focus_plus` view needs `timeout=500`.

**Previous session (2026-09-26, into the night): MGNREGA all-blocks and all-villages test, then fixes.
Code changed, uncommitted.**
- **Result on the final code: 13,298 / 13,298.**
  - All 56 blocks × 8 questions and all 6,425 villages × 2 questions (FY 2024-25);
  - each figure matched against megh_db and the raw CSVs.
  - Report: `docs/MGNREGA_AllBlocks_AllVillages_Test_Report_2026-09-26.xlsx`, with before/after
    evidence images.
- **Fixed:** KI-049 to KI-059 (the table and causes are in KNOWN_ISSUES), all in `app/pipeline.py`
  and all gated on MGNREGA. The ones to know:
  - the village-chip resume is pinned from the chip text (`_mgnrega_village_chip_pin`);
  - `_mgnrega_longest_village_in` does a whole-name lookup over every MGNREGA village;
  - SQL rewrites around a resolved `village_code`: `_mgnrega_pin_village_code` and
    `_mgnrega_drop_geo_beside_village`;
  - `_mgnrega_empty_answer` handles no-record and zero-base answers.
- **Tests:** `tests/test_mgnrega_usecase_fixes.py` now has 78 tests. Final suite numbers are in
  TESTING.md.
- **Bulk-run lesson (read before running one):** cap the DB pool (`DB_POOL_MAX_SIZE=10`).
  Tonight's VPN drops plus the default pool of 30 exhausted `megh_db` ("too many clients").
  The harness pauses on outages and retries. The method is in TESTING.md, "Bulk live runs".
- **Open:**
  - 16 village-to-block mapping differences between the DB and raw (the data team's; in
    `MGNREGA_DB_Issues.md`);
  - the KI-041 chip bug still exists in the other five schemes (awaiting the user's go-ahead);
  - generic "total expenditure" questions still ask "which scheme?", by design.
- **Next step:** commit when the user asks. Consider running the same all-villages harness for
  other years, and for the other schemes if the user wants the gated fixes extended.

**Earlier the same day: MGNREGA use-case QA, then fixes (30 / 30).**
- **Round 1:** 16 / 30. Report: `docs/MGNREGA_UseCase_Test_Report_2026-09-26.xlsx`.
- **Fixes:** KI-041 to KI-048, plus wording and unit fixes, all gated on MGNREGA (the user asked
  that other schemes not change). Rationale is in D-025, and the fix map is in KNOWN_ISSUES under
  "KI-041 to KI-048".
- **Round 2:** 30 / 30. Evidence: 43 of 43 queries on two fresh full live runs, checked against
  megh_db and the raw CSVs. Report: `docs/MGNREGA_UseCase_Test_Report_2026-09-26_v2_after_fixes.xlsx`.
- **Files changed:**
  - `app/pipeline.py`;
  - `app/schema_context.py` (MGNREGA rules 8–10);
  - new `tests/test_mgnrega_usecase_fixes.py` (41 tests);
  - docs: AI_PIPELINE, KNOWN_ISSUES, DECISIONS (D-025), CURRENT_STATE, TESTING and
    `MGNREGA_DB_Issues.md`.
- **Tests:** pytest 789 passed (baseline 748). Scripts 14/14. Live context suite 43/43.
- **Open items:**
  - GENAPARA's geography differs between the DB and raw (the data team's, in
    `docs/MGNREGA_DB_Issues.md`);
  - the KI-041 chip bug in the other schemes (awaiting the user's go-ahead);
  - generic "total expenditure" questions still ask "which scheme?". That is by design, since
    PMAY-G and Focus Plus also hold money.
- **Next step:** commit when the user asks. If other schemes are to get the KI-041 fix, remove
  the `"MGNREGA" in schemes` condition in `_explicit_level_in`, then re-run their QA sets.
- The harness scripts lived in the session scratchpad and are **not** in the repo. The method is
  on each report's Summary sheet.

**Previous session:** 2026-09-26. The context-hardening session, then a live validation with the
VPN up. Code changed, **uncommitted** (the user asked for no commit). Also uncommitted from
earlier the same day: context pass 1 (D-022) and the "All of Meghalaya" scope chip.

**LIVE-VERIFIED 2026-09-26** (`tests/live_context_validation.py`, real gateway and DB, turns
rotated across 3 worker stores through real Postgres):
- **43/43 checks**, follow-up resolution 29/29, SQL 20/20, repeated twice.
- Legacy A/B (`--legacy`): 39/43. Figures are in TESTING.md, "Live A/B".
- DB sync cost: about 50 ms p50 each way; `sync_in` p95 359 ms.

**The first live run found, and this session fixed:**
1. **Scenario design:** Focus Plus holds only FY 2022-23 and FY 2025-26, so the scenarios now use
   FY 2025-26. Scenario B2 (MGNREGA) checks the SQL-level year change. Scenario F step 5 no
   longer carries an FY, which D-009 pins to CM Elevate Legacy.
2. **Real bug:** "Show it by district." was rewritten "…in West Garo Hills". The fix is a
   `REMOVED by the follow-up` prompt line plus the `cleared:<field>` check
   (`_cleared_filters`, `context_policy.rewrite_violation`).
3. **Real bug:** "How many beneficiaries were there?" was not treated as a follow-up. The fix is
   `pipeline.is_scopeless_followup`, used in `_run_pipeline` and in the router's `is_cacheable`.

**New open issues (Low):**
- KI-030: an "all years" choice is not carried into the next follow-up.
- KI-031: the scheme swap keeps the old metric words.

**TESTS:** pytest, 19 files: **657 passed**. Scripts: **14/14**. Live: 43/43.

**LATEST: scheme substitution fixed (KI-039), uncommitted.** "give me same for <scheme>"
used to be answered from the reference docs. It is now a follow-up that swaps only the scheme:
`pipeline._scheme_substitution` / `is_scheme_substitution`, plan kind `SCHEME_SUBSTITUTION`,
AI_PIPELINE.md §5.6.
- Tests: `tests/test_scheme_substitution.py` (91).
- Live results: `logs/live_scheme_substitution_2026-09-26.json`.
- New Medium issue KI-040: MGNREGA "beneficiaries" is undefined.

**Before that (same day): live end-to-end validation, no code changed.** The report is
`docs/Context_Validation_Report_2026-09-26.md`: 53 turns, 47 PASS, 1 caveat, 5 FAIL. New issues KI-032 to KI-038.

**NEXT EXACT STEP (supersedes the list below):**
1. KI-032: apply the plan's CLEAR to `session.state` in `update_state`.
2. KI-034: a deterministic "resolved entity missing from SQL" guard.
3. KI-033: use the last DATA turn as the antecedent after a digression.
4. KI-035: never sum per-year distinct counts as a total.
5. Then re-run `tests/live_context_validation.py` and the validation groups.

**Previous next steps:**
1. KI-030: add a `year_all_combined` flag, mirroring `tranche_all_combined`.
2. KI-031.
3. Then the KI-022 / KI-023 user decision, which is still pending.

Re-run `tests/live_context_validation.py` (VPN) after any routing, rewrite or state change.

**DO NOT CHANGE (without reading the incident comment and the tests):**
- the `execute_with_repair` guards and `_verify_sql` filters;
- the `compose_response` faithfulness checks;
- `db._assert_safe` / `run_readonly`;
- the scheme-collision handling;
- the pending-pause resume (steps 0a / 0a');
- `data/<scheme>/*.yaml`;
- `megh_db.curated`.

Do not reintroduce raw previous-answer text into the rewrite prompt (D-022). Do not move
conversation state back into process memory (D-023).

**Never print or copy rows from `Focus Legacy to share to BLH.csv`.**
