# Current State

**Last updated:** 2026-10-03 (early morning), by the all-schemes all-places QA + fix session (KI-188..199, D-034; code changed, uncommitted; LIVE-VERIFIED). Before that: 2026-10-02, by the LangGraph integration session (D-032; code changed, uncommitted; **offline-verified only**, VPN down). Before that: 2026-09-29 (early morning), by the conversational scheme-swap session (KI-180; code changed, uncommitted). Before that: 2026-09-29 (late night), by the CM Elevate Legacy use-case re-test session (no code changed; KI-166..168 opened). Before that: 2026-09-29 (night), by the Focus Legacy all-levels fix session (KI-020, KI-145..165, D-031; code changed, uncommitted). Before that: 2026-09-29, by the context-relevance / semantic-contract session (code changed, uncommitted; D-030, KI-030/032/034 and KI-130 to KI-135; **offline-verified only**, VPN down). Before that: 2026-09-28 (evening), by the CM Elevate all-blocks / all-villages fix session (code changed in `app/pipeline.py`, tests; uncommitted). Before that: 2026-09-28, by the PMAY-G fix session (code changed, uncommitted; D-029, KI-089 to KI-097). Before that: 2026-09-27 night, by the Focus Plus all-blocks / all-villages session (code changed in `app/pipeline.py`, tests in `tests/test_focusplus_usecase_fixes.py` and `tests/test_mgnrega_usecase_fixes.py`; **uncommitted**). Earlier the same day: the CM Elevate use-case QA **and fix** session (code changed in `app/pipeline.py` and `app/schema_context.py`, plus a new test file; **uncommitted**). Before that, the same day: the Focus Plus use-case QA **and fix** session (code changed in `app/pipeline.py` and `app/routers/query.py`, **uncommitted**, on top of the uncommitted 2026-09-26 MGNREGA and context work).
**Branch / commit:** `main` @ `f38ea1b` (2026-10-02 session start). Uncommitted at session start: `docs/HANDOFF.md`, `docs/KNOWN_ISSUES.md` (KI-182) and untracked QA files; the LangGraph session added the changes listed below on top, all uncommitted.

## Side-by-side deploy identifiers (2026-10-04)
- This build deploys to `/opt/meghalaya-langgraph` as `megh-langgraph.service` (port 8410) or compose project
  `megh-langgraph` (host 8411). The existing deploy (`/opt/meghalaya`, `megh-nlpservice.service`, port 8300,
  containers `megh-nlp`/`megh-qdrant`/`megh-redis`) is untouched and keeps running. Table: deploy/DEPLOYMENT.md.
- Fixed in passing: the Dockerfile served 8300 while compose mapped 8400. VERIFIED (`docker compose config`).

## Chat route and port (2026-10-04)
- Chat console is **`/megh-chat`** (was `/ai-query`, which now 301-redirects). Service port **8400** (was 8300). Config/routing only — the pipeline, API paths (`/api/*`) and `ai_query.html` itself are unchanged. VERIFIED offline. Details: HANDOFF.md.

## Qdrant server move (2026-10-03, afternoon)
- `QDRANT_URL` now `http://115.124.102.167:6335` (was `10.48.242.4:6333`). Config only. `megh_scheme_kb` rebuilt there (201 points, VERIFIED) and `megh_conversation_memory` created. Open security item KI-200 (no API key on a public IP). Details: HANDOFF.md.

## Project status
- **Stage (INFERRED):** internal UAT with live QA passes per scheme.
- **Production deployment status:** UNKNOWN — NEEDS VERIFICATION. Not recorded in the repo.

## Completed functionality (VERIFIED in code)
- All six schemes wired end to end: MGNREGA, PMAY-G, Focus Plus, CM Elevate, Focus Legacy
  (added 2026-09-22) and CM Elevate Legacy (added 2026-09-24).
- **NL→SQL chain:**
  - the classifier, entity resolution and clarification gates;
  - 30B SQL generation, 9 deterministic guards and the 4B verifier;
  - up to 3 repairs;
  - the composer, with faithfulness guards.
- RAG over 10 SME docs plus tagged web docs, with scheme-scoped retrieval.
- **Deterministic answers:**
  - geo abbreviations;
  - scheme listing, comparison, recommendation and pick;
  - Focus Legacy PG-name lookups;
  - cross-scheme money ranking.
- Multi-turn context: follow-up rewrite, structured state, a summary, and Qdrant memory.
  Since 2026-09-26 the rewrite gets the previous turn as structured tiers instead of
  `answer[:300]`, with a provenance check on its output (AI_PIPELINE.md §5.1, D-022).
- A typed reply to the "which scheme?" / "which Focus?" pause resumes the paused question
  (§5.2).
- A `prompt_context` log line for every model prompt: tokens per section, budget, and
  request id (§5.3).
- Voice input with no-speech and prompt-echo guards (2026-09-25).
- Auth, multi-tenancy, admin console, history (pin, archive, rename, delete), audit, caches,
  health and metrics.

## Latest: all six schemes, every district / block / village (2026-10-02 night → 10-03). Code changed, uncommitted. LIVE-VERIFIED.
- **Live bulk: 30,280 / 30,280 pass on the final code** (truth computed read-only from `megh_db` per question):
  MGNREGA 6,378 · PMAY-G 5,256 · PMAY-G officer cases 5,833 · Focus Plus 3,651 · CM Elevate 2,165 ·
  CM Elevate OFF-009/017/018 2,284 · CM Elevate Legacy 1,193 · Focus Legacy 3,520. 1,667 questions
  failed on their first run; every one was traced to a cause, fixed and re-run.
- **Fixed (all in `app/pipeline.py`, each with regression tests):** KI-188 village dropped from the SQL
  (non-MGNREGA); KI-189 village written as a CML programme; KI-190 Focus Legacy + CM Elevate Legacy join
  the village gate (D-034); KI-191 Focus Plus LGD-only blocks; KI-192 spelled-out figures → digits;
  KI-193 PMAY-G village comparisons; KI-194 CM Elevate applicants = distinct request_id; KI-195
  "Sports & Wellness"; KI-196 district/block literal beside a village in any UPPER/LOWER/TRIM form;
  KI-197 village named by truncated text → its code; KI-198 doubled bracket in the CML spelling fix.
  KI-182: full OFF-009 1,260/1,260.
- **Reports (docs/):** `All_Schemes_LangGraph_Test_Results_Summary.xlsx` and one
  `<Scheme>_LangGraph_Test_Results.xlsx` per scheme (MGNREGA, PMAY_G, Focus_Plus, CM_Elevate,
  CM_Elevate_Legacy, Focus_Legacy), plus `PMAY_G_Officer_Cases_LangGraph_Test_Results.xlsx` and
  `CM_Elevate_Officer_Sets_LangGraph_Test_Results.xlsx` (all runs on the LangGraph path).
- **Tests:** pytest **1,453 passed, 0 failed** (27 files) with the graph off AND on; scripts **14/14**; live context suite **43/43 on both paths** (follow-up 29/29).
- **Not covered:** one question shape per place (KI-199). Still open: KI-184/185 (Postgres checkpointer
  unverified live), canary G5.

## Latest: live verification + fixes (2026-10-02, evening, VPN up). Code changed, uncommitted. LIVE-VERIFIED.
- **LangGraph live gates passed:** context suite 43/43 on both paths; 36/36 live turns identical across all six schemes; all 21 nodes ran on live traffic with 0 errors. Flag still off by default (next gate: canary, G5).
- **Fixed:** KI-187 (verifier false positive: 'What about Dalu block?' went to the KB; pre-existing, reproduced on the pre-refactor code), KI-182 (CM Elevate OFF-009: a 0-applicant programme left out — live re-test 49/49), KI-183 (repaired SQL now re-authorized, D-033), 2 stale verifier tests.
- **Tests:** pytest 1,419 passed, 0 failed, flag off and on; scripts 14/14.
- **Still open:** KI-184/185 (shared Postgres checkpointer needs approval and a non-production DB); the full 1,260-question OFF-009 set was not re-run (49-question sample was).
- **Report:** https://claude.ai/artifact/Q2Mb7ynLwebbjcRebHkYmq (nodes + per-scheme results).

## Earlier the same day: LangGraph orchestrator (2026-10-02, D-032). Code changed, uncommitted. OFFLINE-VERIFIED ONLY (VPN down).
- **What:** `app/pipeline.py`'s `_run_pipeline`, `_answer_data` and `execute_with_repair` are split,
  verbatim, into stage functions. A new opt-in 21-node LangGraph graph (`app/pipeline_graph.py`)
  runs the same stages, with an explicit SQL repair loop, clarification pauses as `interrupt()`, a
  checkpoint under `thread_id = session_id` (SQLite), and shape-only per-node logs. **Off by
  default** (`PIPELINE_GRAPH_ENABLED=false`, `PIPELINE_GRAPH_CANARY_PERCENT=0`); the router and the
  API are unchanged.
- **Works (VERIFIED offline):**
  - the stage split is behaviour-neutral: 1,371 → 1,371 passed, the same 2 known failures;
  - 27 graph tests: the same conversations on both orchestrators give identical results, pauses,
    session state and model/DB call counts (answer, follow-up, scheme switch, pause → resume,
    "thanks" / new question abandon a pause, digression, repair, repair budget, DB outage,
    denial, edge);
  - pause → checkpoint → resume; correct even with the thread deleted;
  - every intermediate state serializes; no question text in graph logs; TTL sweep;
  - full pytest flag on: 1,397 passed (the only extra failure was a test reading the live flag,
    since fixed). No existing test calls `answer_question`, so this sweep adds no graph coverage;
    the graph evidence is the 27 dedicated tests. Scripts 14/14.
- **Not verified:**
  - live context suite and live use cases on the graph path (KI-186);
  - the Postgres checkpointer (KI-185);
  - cross-VM thread visibility with SQLite (KI-184, correct but untraced).
- **Cost:** +45 to +120 ms per turn of orchestration, measured offline (≈3–7% of a live turn);
  no extra model or DB calls.
- **Found:** repaired SQL is never re-authorized (KI-183, pre-existing, left unchanged on both
  paths).
- **Dependencies:** `langgraph` 1.2.12, `langgraph-checkpoint-sqlite` 3.1.1 (+ `langchain-core`,
  `langsmith`; `websockets` downgraded 17.1 → 16.1.1 by `langgraph-sdk`). Installed in `.venv`;
  `requirements.txt` / `.lock` updated.

## Latest: typed replies resume every chip pause (2026-09-29, morning, KI-181). Code changed, uncommitted. LIVE-VERIFIED.
- Before: only the area/year/entity/ranking pauses and the which-scheme pauses were remembered. Any other pause with chips
  (measure gap, year out of range, tranche, region, AC part, Sericulture, CM sub-scheme group, stated amount, …) forgot its question,
  so a typed "houses sanctioned" or "2022-23" was read against the last answered turn.
- Now the router remembers every pause with options; a typed reply picks ONE option by its words, an ordinal, or "yes"
  (`_resume_option_pause`), and an unmatched reply stays on the paused scheme (`_paused_thread_antecedent`). AI_PIPELINE §5.2.
- Tests: pytest 1,371 passed + the 2 parallel-session failures; scripts 14/14; live context 43/43.

## Latest: conversational scheme swap — "give me for pmay" (2026-09-29, early morning). Code changed, uncommitted. LIVE-VERIFIED.
- Reported: "Total MGNREGA person-days in 2023-24" then "give me for pmay" returned a PMAY-G scheme description.
- Now a request-verb swap continues the previous DATA question on the new scheme (`_SCHEME_SWAP_FOLLOWUP`, `_SUBSTITUTION_CUE`).
  When the carried measure does not exist in the new scheme (person-days in PMAY-G, houses in MGNREGA), the bot says so and
  offers the new scheme's own measures for the same place/year as chips (`_swap_measure_gap`, rule `swap-measure-unavailable`),
  instead of the old "couldn't build a working query". Questions about a scheme ("tell me about pmay") are unchanged.
- Tests: pytest 1,338 passed + 2 failing tests owned by a parallel CM Elevate Legacy session (see TESTING.md); scripts 14/14; live context 43/43.

## Latest: CM Elevate Legacy fixes + all districts / blocks / villages (2026-09-29, late night). Code changed, uncommitted. LIVE-VERIFIED.
- **Use cases 36/36** on 2 of 2 fresh runs (were 33/36: TC-13 LIMIT 10, TC-14 COUNT(*), TC-34 derived count).
- **All levels 2,614/2,614** vs DB AND raw (12 districts, 59 blocks, 1,051 villages, 55 ACs; 7 question shapes).
  Round 1 found KI-169..181 (twin-village chip loop / ranking, exact small amounts, spelling, ward literal,
  unnamed breakdown figures, Tura MB refused, invented subsidy split, garbled village lists, Garo village,
  "village" kept in the name, verifier false positive after a chip, unselected GROUP BY) — all fixed.
- Design: deterministic SQL guards in `execute_with_repair` + `_cm_legacy_answer_guarantees` after the composer
  (AI_PIPELINE §2.9a). KI-179 (level word dropped from an extracted village name) touches ALL schemes, guarded by
  an exact-name check.
- Tests: pytest 1,309+ (26 files; `test_cm_elevate_legacy.py` 131), scripts 14/14, live context 41/43 then the
  2 failing checks (scenario G, a transient app.conversations save-failed) 5/5 on 2 re-runs.

## Earlier the same night: CM Elevate Legacy use-case re-test — 33/36. No code changed.
- All 36 use cases (`Use_Cases_-_CM_Elevate_legacy.csv`) run live through `pipeline.answer_question`, 2 full runs + 3 repeats of the
  failures; every figure checked against `curated.v_cm_elevate_disbursement` AND the raw file `Cm Elevate legacy to share to BLH (1).csv`.
- **DB = raw** row for row (2,823 = 2,823 on source id; all money, scheme, FY, district, block, AC, lender, desanction fields identical).
- **Fails (AI layer, all reproducible):** TC-14 sanctioned 2,823 vs 2,820 (KI-166, SQL `COUNT(*)`); TC-34 "20 villages with 1 record" vs 24
  (KI-167, composer); TC-13 `LIMIT 10` → "down to 33" vs Motorcaravan 1 (KI-168). TC-01..10 knowledge answers all correct.
- Report `docs/CM_Elevate_Legacy_UseCase_Test_Report_2026-09-29.xlsx` (+ `docs/CM_Elevate_Legacy_UseCase_Evidence_2026-09-29/`, 37 PNGs).

## Latest: reported "what is focus" conversation (2026-09-29, later), uncommitted, LIVE-VERIFIED
- **Report:** after a CM Elevate Legacy turn, "what is focus" was answered with CM Elevate material;
  "give me beneficiaries" paused generically; the user had to retype the whole question.
- **Root causes (KI-136 to KI-144):** a bare "Focus" went to the model rewrite, which read it as a noun;
  the rewrite carried a data turn's year / place into how-it-works questions; the rewrite could swap
  the user's metric; the old scheme's filters fed follow-ups after a knowledge answer on another
  scheme; intent was read from the rewrite, not the typed words; a paused follow-up was remembered as
  a fragment; "all of them combined" kept the last year; a place-only change after knowledge answers
  lost the data thread.
- **Tests:** pytest **1,185**; scripts **14/14**; live context suite **43/43**; live battery S1–S8 all
  correct (it also live-verified the first pass, KI-130 to KI-135).

## Earlier the same day: context relevance + semantic contract (2026-09-29), uncommitted (offline at the time; live-verified later that day)
- **Reported conversation** (after "beneficiaries in focus+ across all financial years"): "who is
  harshit" / "he is my collik remember" answered as Focus Plus; "now give me five thousand loan…"
  became a Focus Plus DATA pause; "…Focus Plus … for a loan of five thousand…" ran SQL without the
  amount; WHK dropped from a comparison. Reproduced offline with the real `_run_pipeline`.
- **Fixed (D-030):** continuation gate `context_policy.continuation_signals` (KI-130); edge
  `personal_request` (KI-131); Focus Plus stated-amount filter + guard + pause (KI-132); WHK
  near-miss chips (KI-133); bare "which one?" asks (KI-134); village name search (KI-135); generic
  `_resolved_scope_missing` SQL guard (KI-034); CLEAR applied to committed state (KI-032);
  all-years carried (KI-030); `pipeline_decision` log lines.
- **Tests:** pytest **1,163** (26 files; +96 in `test_context_relevance_and_contract.py`);
  scripts **14/14**. **Not run:** `tests/live_context_validation.py` and every live check — the
  DB and gateway (`10.48.242.4`) were unreachable. Run them first when the VPN is up.
- "give me same for mgnrega" already worked (KI-039); it is re-tested and unchanged.

## Latest: CM Elevate all blocks / all villages (2026-09-28)
- **CM Elevate all districts / blocks / villages (2026-09-28): 7,364 / 7,364 on the final code** (round 1: 4,132 / 4,208). Every district (12), block (66) and village (2,087 x 3 phrasings) x the use-case question types, each figure checked against megh_db and the raw workbook. Report `docs/CM_Elevate_AllBlocks_AllVillages_Test_Report_2026-09-28.xlsx` (+ `_Evidence_2026-09-28/`).
  - Fixed: KI-074 decided (pending = On Hold only), KI-076 (intent cue), KI-106 to KI-120 (CM Elevate joined the village guards; urban bodies; literal / sector / programme-filter SQL guards; verifier false positives; twin-village chip; number words). All CM Elevate-gated except the literal- and FILTER-aware village WHERE rebuild (KI-108, also Focus Plus). pytest 1,053; context suite 43/43.

## Latest: PMAY-G plain year = calendar year (2026-09-29, KI-125 revised), uncommitted
- "during 2017" → calendar 2017 in the answer, table and SQL; FY reading as a one-line note. Explicit FY unchanged.

## Latest: PMAY-G result table and Sources (2026-09-29, KI-129), uncommitted
- Result table = place + asked figures; Sources no longer shows "TRUE". pytest 1,067; follow-ups 18/18; use cases 71/71.

## Latest: PMAY-G extra information removed (2026-09-28, KI-128), uncommitted
- Summaries = exactly the use-case figures; no side details on single figures; follow-ups answer only what was typed.
- Live 2,129/2,129 sample + use cases 71/71 + follow-ups 18/18; pytest 1,065.

## Latest: PMAY-G 21 Sep tester sheet recheck (2026-09-28), uncommitted
- All tester-named inputs correct vs DB and raw; 192/192 live checks + use cases 71/71.
- Fixed KI-125: a bare year was dropped by the facts path (answered all years). Now FY reading + calendar-year line.
- Open product decisions: KI-126 (one combined village + year question), KI-127 (count zero-sanction placeholder
  records or not — the tester's 14 Sep and 21 Sep remarks conflict).

## Latest: PMAY-G full scenario test (2026-09-28): 17,331 / 17,331, uncommitted
- **Why:** the KI-098 miss showed the earlier village test used one phrasing only. This run covers every place type x 16
  question types, 3 village phrasings, every FY, dates, comparisons, model-path questions and follow-ups.
- **Found and fixed (KI-099 to KI-105):** 'Garo'/'Khasi' inside a village name triggered the hill-range pause; 'Old …'
  villages read as the Old House stage; ', X block' mid-sentence; a village named like its block; two-village comparisons;
  FY printed as a calendar year; model-path amounts in 2-decimal crore.
- **Result:** 17,331/17,331; follow-ups 18/18; use cases 71/71; context 43/43; pytest 1,005; scripts 14/14.
  Report `docs/PMAY_G_Full_Scenario_Test_Report_2026-09-28.xlsx`.

## Village named beside its block (2026-09-28, KI-098, user report), uncommitted
- **Report:** "…still to be released in NONGSOHRAM across all financial years, RI MULIANG block, WEST
  KHASI HILLS" answered with the whole block (₹33,84,000).
- **Fix (PMAY-G only):** `_pmay_village_beside_block`, called at the end of `resolve_entities`.
- **Live:** all 5,120 villages asked with their block + district tail **5,120/5,120** (before the fix, 216 of
  the first 927 such questions answered for the block); block questions unchanged (see TESTING).

## PMAY-G fixes (2026-09-28): 28 / 28 use cases, all blocks and all villages, uncommitted
- **Result:** use cases **28/28** (71/71 questions on 2 of 2 fresh live runs, identical answers; round 1 was 17/28); all blocks **224/224**; all villages **5,120/5,120** on the final code (round 1 4,393/5,120 → KI-096 fixed). Every figure checked against megh_db and the raw CSV (identical row for row).
- **Changed:** `app/pipeline.py` (PMAY-G facts path, model-path guards, village gate, date / typo /
  plural-blocks fixes), `app/entity_resolver.py`, `app/premise_check.py`, `app/schema_context.py`,
  `data/pmay/pmay_few_shot.yaml`, `data/pmay/pmay_entity_resolver.yaml`, tests. D-029; KI-089 to KI-097.
- **Regression:** pytest 993; scripts 14/14; live context 43/43; Focus Plus 60/60 + 300/300;
  MGNREGA 60/60 + 300/300.
- **Reports:** `docs/PMAY_G_UseCase_Test_Report_2026-09-28_v2_after_fixes.xlsx`,
  `docs/PMAY_G_AllBlocks_AllVillages_Test_Report_2026-09-28.xlsx`.

## PMAY-G use-case QA, round 1 (2026-09-28): 17 / 28, before fixes
- **What:** the 28 PMAY-G use cases (`PMAY-G.csv`, PMAY-OFF-001 … 028) as 71 real questions, each
  run twice live, with every figure checked against megh_db and the raw
  `PMAY_FullyMapped_with_dates.csv`. The user said "Focus legacy", but named PMAY-G files, so PMAY-G was tested.
- **Result:** **17 / 28 test cases pass** (49 / 71 questions). The failures are 007, 009, 012,
  016, 018, 022, 023, 024, 025, 027 and 028.
- **DB vs raw:** identical row for row (171,107 rows, 13 fields, 0 mismatches).
- **Root causes (KNOWN_ISSUES KI-089 to KI-095, all fixed the same day — see above):**
  - per-year GROUP BY on summaries and comparisons (row dumps, mislabelled or invented figures);
  - required parts missing (remaining amount, difference, sanctioned amount);
  - a false "House Sanctioned stage" qualifier from the entity resolver;
  - utilisation as an average of ratios;
  - LIMIT 1 on "which has more";
  - a written-date premise misread;
  - village money rounded to 0.0x crore.
- **Report:** `docs/PMAY_G_UseCase_Test_Report_2026-09-28.xlsx` (Summary, Test Results, Question
  Detail, DB vs Raw, Evidence); PNGs in `docs/PMAY_G_UseCase_Evidence_2026-09-28/`.
- **Fixed the same day** — see the section above.

## Latest: database-outage handling and data package (2026-09-28), uncommitted
- **KI-025 fixed (connection loss):** a megh_db outage mid-question now returns 503 "Couldn't
  reach the data service… retry". It no longer answers "not in the reference material" or spends
  SQL repairs. Changed: `app/db.py`, `app/pipeline.py`, `app/routers/query.py`. Slow-query
  timeouts are unchanged (504).
- **KI-065 / KI-084 (data):** not changeable by the chatbot (the DB belongs to the ingestion team,
  CLAUDE.md §6). A row-level package is handed over:
  `docs/Focus_Plus_Data_Corrections_for_Ingestion_2026-09-28.xlsx`. Awaiting their decision.
- **Tests:** pytest 939; scripts 14/14; live context 43/43; outage simulation passed; live
  204/204 blocks + 200/200 villages.

## Latest: Focus Plus all blocks and all villages (2026-09-27 night): 100% on the final code, uncommitted
- **Scope:** every block (51 × 4 questions) and every village or ward in `v_focus_plus`
  (3,513 × 2). Each figure was checked against megh_db and the raw CSV.
- **Result:** blocks **204/204**; villages **7,026/7,026**.
  - Before fixes: blocks 201/204, and villages 62/327 on the first sample.
  - Confirmation pass on the final code: 204/204 and 1,000/1,000.
  - MGNREGA regression: 448/448 and 300/300.
- **Report:** `docs/Focus_Plus_AllBlocks_AllVillages_Test_Report_2026-09-27.xlsx`, with evidence
  images.
- **Fixed (KI-079 to KI-088):**
  - the village guards now cover Focus Plus (`_village_scheme`, D-028);
  - `_focusplus_narrow_village`;
  - `_focusplus_pin_village_where` (place conditions, place literals, `geography_key` subquery);
  - twin-village "(LGD code)" chips;
  - the district-alias collision (BAGHMARA);
  - the "Nan" block;
  - BURMA;
  - the Focus Plus one-figure misquote rule;
  - roman numerals in names.
- **Data side:** DB village totals often exceed the raw file's code-only totals (KI-084, see
  `docs/Focus_Plus_DB_Issues.md`).
- **Tests:** pytest 936 (24 files); scripts 14/14; live context 43/43.

## Latest: CM Elevate use-case fixes (2026-09-27): 30 / 30 after fixes, uncommitted
- **Result:** 55 / 55 questions on 2 of 2 fresh live runs. Each figure was checked against
  megh_db `v_cm_elevate` and the raw `CM_Elevate_AllSchemes_20260927_full.xlsx` (DB = raw
  exactly).
  - Round 1, before the fixes: 23 / 30.
  - Report: `docs/CM_Elevate_UseCase_Test_Report_2026-09-27_v2_after_fixes.xlsx`.
- **Fixed, all CM Elevate-gated (D-027):**
  - KI-068: Unresolved rows dropped from programme totals;
  - KI-069: no per-programme split;
  - KI-070: pending at level 2 gave 0;
  - KI-071: no comparison difference;
  - KI-072: zero sectors vanished;
  - KI-073: garbled two-label prose;
  - KI-075: PRIME SEED paused on "which scheme?".
  - Found while re-testing: a plain "pending" became a level filter; approved/rejected went to
    the wrong column; a verifier false positive on `scheme_specific`.
- **KI-074 interim:** "pending" is still On Hold. A single-figure pending answer also states the
  file-status Pending count. **Product decision still needed.**
- **Tests:** pytest 891 passed (858 + 33 new); scripts 14/14; live context suite 43/43.
- **Open:** KI-076, an intent-classifier flake for a question with no counting word (Low,
  pre-existing).
- The user said "Focus legacy" but named the CM Elevate files, so CM Elevate was tested.

## Latest: Focus Plus use-case fixes (2026-09-27): 30 / 30 after fixes, uncommitted
- **Result:** 39 / 39 questions on 2 of 2 fresh live runs, each figure checked against megh_db
  and the raw CSV. Report: `docs/Focus_Plus_UseCase_Test_Report_2026-09-27_v2_after_fixes.xlsx`.
- **Fixed:**
  - KI-060: shares;
  - KI-061: comparison difference;
  - KI-062: "1,0263", fixed for all schemes;
  - KI-063: bank-pause chips and typed resume;
  - KI-064: rupee format, the "amount raw" fallback (the SQL-literal fix applies to all schemes),
    and no "which area?" for a batch or tranche;
  - KI-066: a typed reply that pauses again kept only the bare reply, all schemes;
  - KI-067: the Focus Legacy bank `KeyError`.
- **Where:** `_focusplus_answer_guarantees` and helpers, `_fix_digit_grouping`,
  `_sql_literal_numbers`, `_bank_clarification`, `_needs_scope_clarification`,
  turn_context `resumed_question`, and router `pause_question`. Rationale is D-026.
- **Tests:** pytest 858 passed (826 + 32 new); scripts 14/14; live context suite 43/43.
- **Still open:** KI-065, data-side (175 Dalu ↔ SWGH rows), with the ingestion team.

## Focus Plus use cases, round 1 (2026-09-27): 19 / 30, before fixes
- **Inputs:** `Focus +_Use_Cases.csv` (FOCUS-001..030) and the raw `Focus Plus Master.csv`.
  - The user wrote "Focus legacy", but both files are Focus Plus, so Focus Plus was tested.
- **Method:** 39 concrete questions, placeholders filled with real values. Each went through the
  live `pipeline.answer_question` twice, clicking the chip a tester would pick. Every figure was
  checked against both megh_db (`v_focus_plus`) and the raw CSV.
- **Result:** **19 PASS / 11 FAIL** test cases (27 / 39 questions). Both runs gave the same
  verdicts.
  - **Failed:** FOCUS-008, 011, 012, 013, 014, 016 (percentages missing, KI-060); 026, 027, 028
    (no difference / higher district, KI-061); 021 (malformed "1,0263", KI-062); 029 (bank
    question lost after the no-chip clarification, KI-063).
  - In 10 of the 11 failures the numbers themselves are correct. The exception is 029, which gave
    no number at all.
- **Report:** `docs/Focus_Plus_UseCase_Test_Report_2026-09-27.xlsx`, with evidence PNGs in
  `docs/Focus_Plus_UseCase_Evidence_2026-09-27/`.
- **DB vs raw:** identical except 175 rows (Dalu ↔ SWGH) in a different district. See KI-065 and
  `docs/Focus_Plus_DB_Issues.md`.
- **Fixed since the testers' 14 Sep comments:**
  - no stray verification column (018, 019);
  - no stray FY column (010);
  - "female during 2025-26" answers (021a);
  - the Focus+ overall summary (030) is complete and correct.

## Calendar date read as FY fix (2026-09-27, KI-078; uncommitted)
- **Report:** "How many PMAY houses were sanctioned on 2017-11-28?" answered 504 (correct) but
  added "not the 11 or 28 figures assumed" and "applies to FY 2017-18".
- **Now:** "504 PMAY houses were sanctioned on 2017-11-28." A date is no longer back-filled as
  an FY, never yields premise figures, and satisfies the scope gate.
- **Checks:** live 4/4 probes; pytest 915; scripts 14/14.

## MGNREGA women share fix (2026-09-27, KI-077; uncommitted)
- **Report:** "What percentage of employment persons were women in ekh?" answered "0.00% … a
  genuine zero".
- **Cause:** the question named no year and the year gate ignored percentage questions, so the
  SQL model took FY 2025-26, whose women column is unrecorded at source.
- **Now:**
  - the bot asks the FY, offering only the years that carry women data;
  - the answer states the years it covers: EKH 77.70% in FY 2024-25, or 74.90% for FY 2022-23
    to 2024-25;
  - FY 2025-26 is answered "not recorded", never 0.
- **Checks:** live 8/8. pytest 897, scripts 14/14, live context 43/43.

## Latest QA: MGNREGA all blocks and all villages (2026-09-26 night): 100% on the final code, uncommitted
- **Scope:** FY 2024-25.
  - All 56 blocks × 8 questions = 448.
  - All 6,425 villages × 2 questions (person-days, total expenditure) = 12,850.
  - Every expected figure comes from megh_db and, independently, from the raw CSVs.
- **Result on the final code: 13,298 / 13,298.** VERIFIED as follows:
  - the full village pass gave 12,846 / 12,850;
  - the 4 misses were 3 short-name villages (fixed afterwards) and one gateway 502;
  - those, plus every case that went through a "village" chip, were re-run on the final code:
    590 / 590 (2 retried after a VPN drop);
  - blocks re-run on the final code: 448 / 448.
- **Report:** `docs/MGNREGA_AllBlocks_AllVillages_Test_Report_2026-09-26.xlsx`, with before/after
  evidence in `docs/MGNREGA_AllBlocks_AllVillages_Evidence_2026-09-26/`.
- **Defects found and fixed:** KI-049 to KI-059, all MGNREGA-only. The worst was a village chosen
  from the bot's own list answered with its whole BLOCK's total (KI-049). Other defects:
  - village-chip loops;
  - bare block names;
  - names with "&", "INCL", "BLOCK", "India" or "Dairy" in them;
  - a stray district filter and an extra village code in the SQL;
  - "null" wording.
- **Data side (the data team's):** 16 villages are in a different block in the DB than in the raw
  file, which affects 68 of the 448 block figures. Village figures are identical. See
  `docs/MGNREGA_DB_Issues.md`.
- **Operational lesson:** bulk runs must cap the DB pool. On 2026-09-26 VPN drops plus a 30-connection
  pool exhausted `megh_db` (max 100, shared), and the result was "too many clients already". See
  TESTING.md, "Bulk live runs".

## MGNREGA use cases (2026-09-26): 30/30 after fixes
- **Round 1 (no code changed):** 16 PASS / 14 FAIL of the 30 use cases in
  `Test_Case_Results_21st_Sep_26_MGNREGA (1).csv`. The testers had 17 / 12 / 1 On-Hold on
  21 Sep.
  - Report: `docs/MGNREGA_UseCase_Test_Report_2026-09-26.xlsx`, with evidence in
    `docs/MGNREGA_UseCase_Evidence_2026-09-26/`.
- **Round 2 (after the fixes, same day): 30/30 PASS.** All 43 concrete queries were correct on
  two fresh full live runs, with every figure checked against megh_db and the raw CSVs. VERIFIED.
  - Report: `docs/MGNREGA_UseCase_Test_Report_2026-09-26_v2_after_fixes.xlsx`, with evidence in
    `docs/MGNREGA_UseCase_Evidence_2026-09-26_after_fixes/`.
- **Fixes:** KI-041 to KI-048, plus three answer-wording and unit fixes. All are in
  `app/pipeline.py` and `app/schema_context.py` (MGNREGA rules 8–10), and **every change is
  gated on MGNREGA**. Design choices are in D-025.
- **Tests:** `tests/test_mgnrega_usecase_fixes.py` (41 at that point; 78 after the all-villages fixes). pytest 789 passed at that point. Scripts 14/14. Live
  context suite 43/43.
- **DB vs raw:** rows and all statewide totals are identical. The one mapping difference is village
  GENAPARA (`docs/MGNREGA_DB_Issues.md`, owned by the data team). No failure was caused by the DB.
- **Known limitation, left on purpose:** the negated-level chip bug (KI-041) still affects the
  other five schemes. The user asked that they not be changed.

## Focus Legacy all blocks / villages / ACs / PGs (2026-09-29, night) — VERIFIED, code changed, uncommitted
- **32,560/32,560** questions correct vs the live DB and the raw file (latest run of each): 56 blocks x3,
  12 district breakdowns, 55 ACs x3, 3,384 villages x2 (list + amount), 11,906 PGs x2 (exists + members),
  1,635 older raw spellings. Final-code regression pass: 5,215 questions (all block/AC, every question that
  ever failed, 4,300 random) 5,214/5,215, the last fixed (KI-165) and re-run.
- Use cases **28/28 on 3 of 3 runs** on the final code; live context suite **43/43**; pytest **1,265**;
  scripts **14/14**.
- Fixed: KI-020 (older PG spellings via `v_focus_legacy_pg_search`), KI-145..165 — breakdowns written from rows
  (D-031), month/rupee formatting, AC-alone no longer pins MGNREGA, AC drill-down per scheme, PG-name parsing
  (punctuation, place words, "_", non-ASCII, "Focus" in a name), edge refusing group/village names holding a
  state or country word, village phrase read from the text, twin villages and LGD chips, village chip pin +
  year chip, verifier false complaint, misspelt block literal, DATE_TRUNC timezone shift.
- Reports: `docs/Focus_Legacy_AllBlocks_Villages_ACs_PGs_Test_Report_2026-09-29.xlsx` (+ `_Evidence_…/`, 70 PNGs,
  before/after per fix), `docs/Focus_Legacy_UseCase_Test_Report_2026-09-29_v2_after_fixes.xlsx` (+ `_Evidence_…_after_fixes/`).
- **Open:** KI-151 (one intermittent "couldn't build a query" under heavy load); KI-155 data team (6 mis-encoded
  names); KI-164 NOT VERIFIED for other schemes (PMAY-G `date_trunc('month', sanction_date)`); the duplicate-village
  chip loop (KI-156) NOT VERIFIED for CM Elevate Legacy; TC-10/16 "beneficiaries" definition needs an SME.

## Focus Legacy use-case re-test (2026-09-29) — VERIFIED, no code changed
- **28/28 PASS** on 3 of 3 full runs (supplementary TC-14b, TC-28blk: 2/2); data answers were
  word-for-word identical across the runs. Each figure was checked against the live DB view
  `curated.v_focus_legacy` AND the raw file `Focus Legacy to share to BLH.csv`, computed independently.
- **DB vs raw:** 14,569 rows joined 1:1 (raw `id` = `source_row_id`); pg_id, members, amount, date, FY,
  district, block, village and constituency identical on every row. PG-name text differs on 1,636
  rows (the DB keeps one current name per pg_id — by design). 88 rows have no block in both sources.
- Remarks, not failures: KI-145 (TC-26 omits 5 no-block PGs), KI-146 (MGNREGA wording in the AC
  chip), KI-147 (unformatted money, "month 4"); TC-10/16 "beneficiaries" = members summed over payments
  (102,021; 91,446 if each PG is counted once — needs an SME definition); one PG records 190 members.
- Report: `docs/Focus_Legacy_UseCase_Test_Report_2026-09-29.xlsx`; proofs:
  `docs/Focus_Legacy_UseCase_Evidence_2026-09-29/` (one PNG per case).

## Earlier QA (from the `docs/` records, 2026-09-25)
- **Focus Legacy:**
  - 28/28 use cases in round 3, VERIFIED from the report's summary sheet;
  - bulk run of 168/168 block and 580/580 PG questions, per session notes. The report computes
    these with formulas that have no cached values, so they are not machine-verified.
- **CM Elevate Legacy:** 36/36 against both the DB and the raw file, VERIFIED from the "FINAL
  RESULT" in report v3. Earlier rounds: 22/36 and 33/36.
- **Ingestion-team fixes:**
  - block NULLs for rows with no village code, in both schemes;
  - CM Elevate Legacy row id 2392 loaded.

  8 of 9 Focus Legacy re-verification checks pass. TC-F1 has an open question (KI-019).

## Partially complete / open
- **PG alternate-spelling lookup:** the DB view `curated.v_focus_legacy_pg_search` exists, but
  the bot does not use it yet (KI-020, PLANNED).
- **No accuracy benchmark** (KI-017).
- **Security hardening gaps:**
  - generated SQL can reach `app.*` and the privacy tables (KI-004);
  - authorization blind spots (KI-007);
  - TLS verification is off unless a CA bundle is present (KI-015).
- ~~Clarification resume is per worker (KI-001)~~ — stale line, corrected 2026-09-29: KI-001 is
  fixed and live-verified (KNOWN_ISSUES; D-023), see the next bullet.
- **KI-028 / KI-001 are fixed and live-verified** (2026-09-26): shared state goes through
  Postgres (D-023). New low-severity findings from the live run: KI-030 (an "all years" choice
  is not carried forward) and KI-031 (the scheme swap keeps old metric words).
- Full list: [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

## Documentation audit (2026-09-26)

**Result:**
- All 11 maintained docs were re-checked against the code.
- Every code identifier they cite exists (script check).
- All relative links resolve.
- 24 documentation issues were found. 23 were corrected in the maintained docs. 1 (a stale
  claim in the SME file `data/pmay/README.md`) is recorded in DATA_MODEL.md but the file itself
  was not edited.
- The critical risk KI-022 is documented; it needs a user decision.

**New verified risks added to KNOWN_ISSUES:**
- KI-022: raw PII file pushed. **Critical.**
- KI-023: plaintext seed passwords.
- KI-024: voice recordings saved on the dev box.
- KI-025: LLM repairs spent on infrastructure errors.
- KI-026: no pool acquire timeout.
- KI-027: startup-only year and schema snapshots.
- KI-004 was extended with the prompt-injection path to `app.users`.

## Current development focus
- **INFERRED from the last commits:** per-scheme use-case QA and fixes for the two newest
  schemes.
- **This session:** follow-up context from structured state (D-022), the scheme-pause resume, and
  prompt-context observability.

## Current known failures
- No failing automated tests (see below).
- `pytest tests` over the whole directory fails with INTERNALERROR (KI-018). This is a test
  harness issue, not a product failure.

## Blockers
- None recorded in the repo.
- Live verification needs the office VPN to reach `10.48.242.4`. It was unreachable from this
  machine on 2026-09-26.

## Recent changes (git)

| Commit | Date | Summary |
|---|---|---|
| `7064ab6` | 2026-09-25 | Added Focus Legacy + CM Elevate Legacy data/tests/docs, QA reports, TECHNICAL_BRIEF, ASR fixes, UI |
| `815c37a` | 2026-09-18 | Routing, entity resolution, QA test coverage |
| `5c5a100` | 2026-09-13 | `verify=False` fallback for the gateway; app port bound to localhost |
| `036eafa` | 2026-09-13 | UAT fixes: district acronyms, Focus+ summary, live column notes |
| `7ad882f` | 2026-09-11 | SQL semantic verifier (qwen3-4b) |

Uncommitted code change (2026-09-26): the `scope-not-specified` statewide chip is now
"All of Meghalaya" (area only), not "All of Meghalaya, all years". See HANDOFF.md.

## Context-layer session (2026-09-26, code)

**Changed (uncommitted):**
- `app/context_manager.py`, `app/context_budget.py` (new), `app/pipeline.py`
- `app/prompt_builder.py`, `app/rag.py`, `app/entity_resolver.py`
- `app/session_store.py`, `app/routers/query.py`, `app/middleware/security_headers.py`,
  `app/config.py`
- `tests/test_context_semantic_state.py` (new)

**Behaviour:** see HANDOFF.md and AI_PIPELINE.md §5.1–5.5. The SQL, verifier and composer prompt
texts are byte-identical under budget. No `curated` DB, API-contract, scheme or business-rule
changes.

**Second pass, same day:** `app/session_sync.py`, `app/context_policy.py`,
`app/conversation_store.py`, `app/llm.py`, `tests/test_context_hardening.py` and
`tests/live_context_validation.py`, plus further edits to the pass-1 files. The only DB change is
the existing `app.conversations.context_state` JSONB, which now holds a versioned snapshot. No
DDL.

## Files changed by the documentation sessions (documentation only)

**Created:**
- `CLAUDE.md`
- `docs/PROJECT_CONTEXT.md`
- `docs/AI_PIPELINE.md`
- `docs/SCHEMES.md`
- `docs/CURRENT_STATE.md`
- `docs/KNOWN_ISSUES.md`
- `docs/DECISIONS.md`
- `docs/TESTING.md`
- `docs/HANDOFF.md`

**Rewritten** (the previous versions were stale two-scheme docs):
- `docs/ARCHITECTURE.md`
- `docs/DATA_MODEL.md`

**Amended by the audit:**
- `CLAUDE.md`: PII-file and seed-password guardrails.
- `docs/{ARCHITECTURE,AI_PIPELINE,DATA_MODEL,SCHEMES,TESTING,DECISIONS,KNOWN_ISSUES,PROJECT_CONTEXT,SECURITY}.md`.
- `deploy/DEPLOYMENT.md`: the verify commands now match the auth on `/health`, `/metrics` and
  `/api/rag/status`.

**Amended (initialisation):**
- `README.md`: scheme list, model table, docs pointer.
- `docs/SECURITY.md`: corrected the DB-role claim.
- `docs/TECHNICAL_BRIEF.md`: marked as a historical snapshot.
- `docs/INFERENCE_REQUIREMENTS.md`: added a status note.

## Scheme substitution (2026-09-26)
- KI-039 is **fixed**: "give me same for <scheme>" is a context-preserving follow-up
  (AI_PIPELINE.md §5.6).
- It is live-verified on the reported conversation and 6 more scheme pairs; the negative cases
  are unchanged.
- New Medium issue KI-040: MGNREGA "beneficiaries" is undefined.

## Live context validation (2026-09-26, no code changes)
- 53 turns, 47 PASS, 1 caveat, 5 FAIL: `docs/Context_Validation_Report_2026-09-26.md`.
- **Open High issues from it:**
  - KI-032: stale block in the committed state;
  - KI-033: stale summary after a knowledge digression;
  - KI-034: SQL ignored mandatory entities and the verifier passed it;
  - KI-035: summing distinct beneficiaries across years.
- **Medium:** KI-031 (raised), KI-036, KI-037.

## Test status (2026-09-27, after the calendar-date fix KI-078, VPN up)
- pytest, 24 pytest-style files: **915 passed**, 0 failed. Plain scripts: **14/14**.

## Test status (2026-09-27, after the Focus Plus fixes, VPN up)
- pytest, 23 pytest-style files: **891 passed**, 0 failed (2026-09-27, after the CM Elevate fixes).
- Plain scripts: **14/14**. Live context suite: **43/43** (follow-up 29/29).
- Focus Plus use cases: 39/39 questions on 2 of 2 fresh live runs (30/30).

## Test status (2026-09-26 night, after the MGNREGA all-villages fixes, VPN up)
- pytest, 21 pytest-style files: **826 passed**, 0 failed.
- Bulk live: 448/448 block and 12,850/12,850 village queries on the final code.
- Plain scripts: **14/14**.
- Live context validation: **43/43 checks** (repeated twice). Legacy A/B: 39/43. Details are in TESTING.md.
- `smoke_restructure.py`: not run (needs a live server on :8502).

## Deployment status
- Deploy artefacts exist:
  - systemd;
  - nginx + ModSecurity;
  - Docker and docker-compose;
  - SQL role and retention scripts.
- Local dev `.env`: DB user `postgres`, `AUTH_ENABLED=true`, `EMBEDDING_PROVIDER=local`, CA
  bundle path set, `REDIS_URL` blank.
- Which environment is live, and with which DB role: UNKNOWN — NEEDS VERIFICATION.

## Next tasks (suggested, in priority order; none started)
0. **Decide what to do about KI-022**, the pushed raw file with unmasked bank accounts, and
   KI-023, the plaintext seed passwords. This is the user's or data owner's call: remove the
   file, possibly rewrite history, review repo access.
1. Wire the PG alias view into `_focus_legacy_pg_name_answer` (KI-020). This is the promised
   follow-up to the ingestion team.
2. ~~Persist the clarification-resume state (KI-001) and the last turn (KI-028)~~ — done
   2026-09-26 (D-023). New item: re-run the live suite on the 2026-09-29 changes (D-030).
3. Least-privilege DB role and table allowlist for generated SQL (KI-004).
4. Request-id propagation and logging of each attempt's SQL (KI-003).
5. A golden-set accuracy benchmark per scheme (KI-017).
6. Fix test collection so `pytest tests` works (KI-018).

## Components that should not be changed casually
- `pipeline.execute_with_repair` guards and `_verify_sql` filters. Each one encodes a live
  incident.
- `compose_response` faithfulness and hedge checks.
- `db.run_readonly` / `_assert_safe`.
- `_SCHEME_NAME_PATTERN`, `_is_ambiguous_focus` and `_pin_cm_elevate_dataset`: the scheme-name
  collision handling.
- `_reply_abandons_scope_pause` and the `pending_scope_q` resume logic.
- `data/<scheme>/*.yaml`: the SME contract. Change it only with an SME-backed reason, and keep
  `schema_context.py` in step.
- `app.*` DDL in `appdb.py`: additive `IF NOT EXISTS` only, because an older build must tolerate
  a newer schema.
