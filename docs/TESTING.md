# Testing

*Reconciled on 2026-09-26 by running the full suite on the Windows dev box, with the
`10.48.242.4` DB and gateway **unreachable** (VPN off).*

## 1. What the tests are, and what they are not

- Every test is a **regression lock for a past incident**. The tests cover:
  - regex routing;
  - prompt text;
  - YAML catalogues;
  - deterministic helpers;
  - flows with the LLM stubbed out.
- **No test measures NL→SQL accuracy** against the real models and DB. A green suite says nothing
  about whether answers are correct (KNOWN_ISSUES KI-017).
- Correctness is checked by **live QA passes**. Their reports are in `docs/*.xlsx` and
  `docs/*_DB_Issues.md`:
  - Focus Legacy: 28/28, plus the bulk run of 168 + 580; re-tested 2026-09-29: 28/28 on 3 of 3 runs vs DB and raw (`docs/Focus_Legacy_UseCase_Test_Report_2026-09-29.xlsx`, v2 after fixes `…_v2_after_fixes.xlsx`); all blocks / villages / ACs / PGs 32,560/32,560 (`docs/Focus_Legacy_AllBlocks_Villages_ACs_PGs_Test_Report_2026-09-29.xlsx`);
  - CM Elevate Legacy: 36/36 on 2026-09-25; re-tested 2026-09-29: **33/36** vs DB and raw (2 full runs + 3 repeats of the failures;
    `docs/CM_Elevate_Legacy_UseCase_Test_Report_2026-09-29.xlsx` + `_Evidence_2026-09-29/`). TC-13/14/34 failed in the AI layer (KI-166..168);
    **after fixes 36/36** on 2 of 2 fresh runs (`…_2026-09-29_v2_after_fixes.xlsx` + `_Evidence_2026-09-29_after_fixes/`);
    all districts / blocks / villages / constituencies **2,614/2,614** vs DB and raw (KI-169..181;
    `docs/CM_Elevate_Legacy_AllBlocks_AllVillages_Test_Report_2026-09-29.xlsx` + `_Evidence_2026-09-29/`).

## 2. How to run

Always use the repo venv, and run from the repo root (`.env` is read relative to the working
directory):

```bash
# 1) pytest-style suites (27 files). Do NOT run `pytest tests` — see KI-018.
.venv/Scripts/python.exe -m pytest -q $(grep -lE "^\s*(async )?def test_" tests/test_*.py)

# 2) plain-script suites (14 files) — each prints "ALL … PASSED" and exits 0/1
for f in tests/test_ac_full_results.py tests/test_admin_level_collision.py \
         tests/test_block_backstop.py tests/test_block_parent_district.py \
         tests/test_context_manager.py tests/test_cross_scheme_collision.py \
         tests/test_cross_scheme_money.py tests/test_edge_conversational.py \
         tests/test_focusplus_block_columns.py tests/test_history_charts.py \
         tests/test_mgnrega_split_facts.py tests/test_no_data_answer.py \
         tests/test_security.py tests/test_verifier_missing_geo.py; do
  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe "$f" || echo "FAIL $f"; done
```

- Set `PYTHONIOENCODING=utf-8` on Windows, because the scripts print non-ASCII characters.
- **LangGraph orchestrator (D-032): run the pytest suites twice.** First with the defaults
  (sequential path), then with the graph on, sequentially and never in parallel, with the
  checkpoint in a temp folder:
  ```bash
  PIPELINE_GRAPH_ENABLED=true LANGGRAPH_CHECKPOINT_DIR="$TEMP/lg_cp" PYTHONIOENCODING=utf-8     .venv/Scripts/python.exe -m pytest -q $(grep -lE "^\s*(async )?def test_" tests/test_*.py)
  ```
  **No existing test calls `answer_question`** (they call `_run_pipeline` or a stage directly, which
  bypasses the graph; checked 2026-10-02 by recording graph node logs during a flag-on run of the 8
  context and scheme files: 547 passed, 0 graph node runs). So this sweep only proves the flag
  breaks nothing. **All graph-path evidence is `tests/test_pipeline_graph.py`:** the same conversations
  on both orchestrators with identical stubs, with results, pauses, session state **and model / DB
  call counts** required to match. Live: the same
  `PIPELINE_GRAPH_ENABLED=true … tests/live_context_validation.py` (NOT run yet, KI-186).
- The script suites are slow when the DB is unreachable. Some entity-resolution paths wait for
  connect timeouts.
- `tests/smoke_restructure.py` is an end-to-end smoke test against a **running server** at
  `http://127.0.0.1:8502` (hardcoded `BASE`, not the default 8400). It was not run.

### Live context validation (needs the VPN)

```bash
.venv/Scripts/python.exe tests/live_context_validation.py            # all scenarios, 3 workers via Postgres
.venv/Scripts/python.exe tests/live_context_validation.py --legacy   # the pre-2026-09-26 rewrite, for A/B
.venv/Scripts/python.exe tests/live_context_validation.py --only A,E --no-db-sync
```

- Exit codes: 0 means all checks passed; 1 means a check failed; 3 means not run (unreachable).
- The report is written to `logs/live_context_validation.json`.
- With DB sync on, it writes temporary `app.conversations` rows under `live-ctx-*` session ids
  and deletes them at the end.

### Bulk live runs: every MGNREGA block and village (2026-09-26)

These runs are the only accuracy checks at scale; see §1 on why green unit tests do not prove
NL→SQL accuracy. The harness lived in the session scratchpad; its method is recorded here and on
the report's Summary sheet.

- **Cases:** FY 2024-25.
  - All 56 blocks × 8 questions: households, person-days, expenditure, women, 100-day %, spend
    + person-days, and two bare-name forms.
  - All 6,425 villages × 2 questions: person-days and total expenditure.
  - Village names are typed exactly as stored.
- **Expected values:** SUM by `village_code` / block from megh_db, read-only. Independently, the
  same sums from the raw CSVs in the repo root.
- **Execution:** `pipeline.answer_question` in-process with the `.venv` Python. When the bot
  pauses, the harness clicks the chip an officer would pick:
  - the option carrying the target village's exact name and block;
  - "village" or "block";
  - MGNREGA.
- **Pass rule:** the answer states the expected figure (lakh/crore display rounding allowed), or
  says there are no records without inventing a number.
- **DB connections: limit the pool.** The default pool is 10–30 connections per process, and
  `megh_db` allows `max_connections` = 100 in total and is shared. During a VPN drop, the server
  keeps the dead connections open until its TCP timeout. On 2026-09-26 that exhausted the server
  ("too many clients already"), and the pipeline turned it into "couldn't build a query" / KB
  fallbacks (KI-025).
  - Run bulk jobs with `DB_POOL_MIN_SIZE=2 DB_POOL_MAX_SIZE=10` and concurrency ≤ 10.
  - Pause on outages: check TCP to 10.48.242.4:5432 before each case, and retry a case that
    failed while the host was down.
- **Expect wall-clock time:** about 55 cases/min at concurrency 10.

## 3. Latest results

| Date | Suite | Result | Notes |
|---|---|---|---|
| 2026-10-02 night → 10-03 (all six schemes, all districts / blocks / villages + officer sets; KI-188..198, D-034) | pytest 27 files flag off and on, scripts, live bulk | pytest: pytest **1,453 passed, 0 failed** (27 files) with the graph off AND on; scripts **14/14**; live context suite **43/43 on both paths** (follow-up 29/29). Live bulk **30,280 / 30,280** on the latest run of each question (MGNREGA 6,378; PMAY-G 5,256; PMAY-G officer cases 5,833; Focus Plus 3,651; CM Elevate 2,165; CM Elevate OFF-009/017/018 2,284; CM Elevate Legacy 1,193; Focus Legacy 3,520). 1,667 failed on a first run; each failure was fixed and re-run (8 re-run rounds, the last 29 → 3 → 0 on the final code). Harness: scratchpad `7be462aa-…/scratchpad/bulk` (`bulk.py gen/run/judge`, `pmay_sets.py`, `cme_sets.py`, `make_reports.py` → `docs/<Scheme>_LangGraph_Test_Results.xlsx`). Run with `DB_POOL_MIN_SIZE=2 DB_POOL_MAX_SIZE=12`, concurrency 10, resumable; background jobs stop at 2 h, so run villages in bounded batches | VPN up (drops handled by `_wait_for_vpn`) |
| 2026-10-02, evening (VPN up; KI-182, KI-183, KI-187 fixed; live gates G3/G4) | pytest 27 files flag off and flag on, scripts, live | **1,419 passed, 0 failed in BOTH modes** (the 2 long-standing verifier tests were stale after the CM Elevate Legacy guard extension and were updated); scripts **14/14**. Live: context suite **43/43 on both paths** (42/43 on both before the KI-187 fix; the failure reproduced on the pre-refactor code too); six-scheme sample **36/36 turns identical** sequential vs graph (scratchpad `g4.py`); CM Elevate OFF-009 re-test **49/49 on both paths** vs the DB (`off009.py`; first attempt 42/49 until the LIMIT 1000 case was handled); OFF-017 Piggery SGH correct on both paths (no code change); live node coverage: 21/21 nodes ran, 0 errors (`g4cov.py`). Report artifact: https://claude.ai/artifact/Q2Mb7ynLwebbjcRebHkYmq | VPN up from 19:17 IST after a drop |
| 2026-10-02 (LangGraph orchestrator, D-032) | pytest 27 files (flag off), pytest 27 files (`PIPELINE_GRAPH_ENABLED=true`), scripts | **Baseline before any change: 1,371 passed, 2 failed.** After the verbatim stage split: **1,371 passed, the same 2 failed** (behaviour-neutral). With the graph: flag off **1,398 passed, 2 failed** (+27 in the new `tests/test_pipeline_graph.py`); flag on **1,397 passed, 3 failed** (the same 2, plus `test_default_is_the_sequential_path`, which read the live flag; fixed to read the declared default, now 27/27 in both modes). Scripts **14/14**. Graph-path coverage comes from the 27 new tests only (no existing test calls `answer_question`). The 2 failures are the pre-existing verifier tests owned by the CM Elevate Legacy session (see the 2026-09-29 rows). One source-pin test moved from `_answer_data` to `_data_clarification_stage`. **OFFLINE ONLY**: live context suite and use cases NOT run on either path | VPN down (5432, 443, 6333 unreachable) |
| 2026-09-29, morning (typed reply to any chip pause, KI-181) | pytest 26 files + live | **1,371 passed, 2 failed** (+33 in `test_context_relevance_and_contract.py`, now 177; the same 2 verifier tests owned by the parallel CM Elevate Legacy session, see the row below). Scripts **14/14**; live context suite **43/43** (follow-up 29/29); live replay: measure gap -> "houses sanctioned" (106,527) / "the second one" (410,200 households) / "houses completed in west garo hills" (stays PMAY-G), Focus Plus year-out-of-range -> "2022-23", Sericulture -> "weaving", Garo Hills region -> "west garo hills" (5,489,616) | VPN up |
| 2026-09-29, early morning ("give me for pmay" scheme swap + measure gap, KI-180) | pytest 26 files + live | **1,338 passed, 2 failed** (+26 in `test_context_relevance_and_contract.py`, now 144). The 2 failures (`test_verifier_district_complaint_beside_village_code_is_discarded`, `test_verifier_complaint_on_correct_village_sql_is_discarded`) pin `_verifier_village_code_complaint_is_false` OUTSIDE CM Elevate Legacy; a parallel CM Elevate Legacy session widened that gate while this session ran — not caused by KI-180, left to that session. Scripts **14/14**; live context suite **43/43** (follow-up 29/29); live replay (scratchpad `119975e8-…/scratchpad/live_convo.py`): reported pair, "give me for pmay please", PMAY-G houses -> "give me for mgnrega", money swap (EKH ₹221.26 cr, unchanged), "tell me about pmay" still KNOWLEDGE, chip 106,527 houses | VPN up |
| 2026-09-29, night (Focus Legacy all blocks / villages / ACs / PGs, KI-020 + KI-145..165) | pytest 26 files + live | **1,265 passed**; scripts **14/14**; live context suite **43/43** (follow-up 29/29); use cases **28/28 on 3 of 3 runs** (final code, month names now checked); bulk **32,560/32,560** (56 blocks x3 + 12 district breakdowns, 55 ACs x3, 3,384 villages x2, 11,906 PGs x2, 1,635 older raw spellings) on the latest run of each question, with a final-code regression pass of 5,215 (all block/AC questions, every question that ever failed, 4,300 random) 5,214/5,215 then 5/5 after KI-165. Harness: scratchpad `2ca0226e-…/scratchpad/bulk` (`gen_bulk.py`, `bulk_run.py`, `bulk_judge.py`), `build_bulk_report.py` | VPN up |
| 2026-09-29, later (reported "what is focus" conversation, KI-136 to KI-144; VPN up) | pytest 26 files + live | **1,185 passed** (+22 in `test_context_relevance_and_contract.py`, now 118); scripts **14/14**; live context suite **43/43** (follow-up 29/29, SQL 20/20; turn p50 1.70 s / p95 3.12 s; rewrite p50 162 ms); live battery (scratchpad `live_convo.py`, `battery.sh`, `battery2.sh`): S1 substitution / unrelated / personal, S2 geography-metric-reference, S3 WHK, S4 five thousand (₹46.64 cr + not-a-loan line), S4b not-held, S5 Songsak search + block/village, S6 the reported conversation, S7 knowledge-then-place, S8 digression keeps MGNREGA — all correct, each figure read against its SQL. S8 failed once (KI-144), was fixed and re-run | VPN up |
| 2026-09-29 (context relevance + semantic contract, KI-030/032/034, KI-130 to KI-135) | pytest, 26 files | **1,163 passed** (1,067 + 96 in `test_context_relevance_and_contract.py`); scripts **14/14**. **OFFLINE ONLY**: `10.48.242.4` unreachable (5432 and 443), so `tests/live_context_validation.py` and every live check were NOT run. Reproduced before the fix with the real `_run_pipeline` (model/RAG/DATA stubbed): 4 of the reported turns misrouted | DB and gateway unreachable |
| 2026-09-28 (CM Elevate all districts / blocks / villages, KI-106 to KI-120) | live bulk | **7,364 / 7,364** on the final code (round 1 4,132/4,208); pytest **1,053**; context suite 43/43 | 7,364 questions; DB = raw; run passes one at a time, DB_POOL_MAX_SIZE ≤ 14; VPN dropped once (runner waits) |
| 2026-09-27 (calendar date read as FY, KI-078) | pytest, 24 files | **915 passed** (+18 in `test_calendar_date_filter.py`); scripts **14/14**; live context suite **43/43** (follow-up 29/29, SQL 20/20); live probes 4/4 (2017-11-28 and 28/11/2017 → 504, 2017-18 → 15,513, undated still asks scope) | VPN up |
| 2026-09-27 (MGNREGA women share, KI-077) | pytest, 23 files | **897 passed** (+6 in `test_mgnrega_usecase_fixes.py`, now 84); scripts **14/14**; live context suite **43/43**; live women checks 8/8 (EKH 77.70% FY 2024-25, 74.90% FY 2022-23..2024-25, FY 2025-26 "not recorded") | VPN up |
| 2026-09-27 (CM Elevate use-case fixes, KI-068 to KI-075) | pytest, 23 files | **891 passed** (858 + 33 in `test_cmelevate_usecase_fixes.py`); scripts **14/14**; live context suite **43/43** (follow-up 29/29, SQL 20/20); CM Elevate use cases: **55/55 questions on 2 of 2 fresh live runs (30/30 use cases)**, each figure checked against megh_db and the raw workbook; 11 reworded probe questions: 10 correct, 1 pre-existing intent flake (KI-076) | VPN up for the final runs |
| 2026-09-27 (CM Elevate use-case QA round 1, before the fixes) | live use cases | **23/30 use cases, 47/55 questions**, identical on 2 fresh live runs; each figure checked against megh_db `v_cm_elevate` and the raw `CM_Elevate_AllSchemes_20260927_full.xlsx` (DB = raw exactly). Failures KI-068 to KI-073; KI-074 needs a product decision. pytest and scripts not re-run (no code changed) | VPN flapped; runs were restarted after a drop. Harness in the session scratchpad; the method is on the report's Summary sheet |
| 2026-09-28 (PMAY-G extra information removed, KI-128) | pytest, 25 files | **1,065 passed** (+6 in `test_pmay_usecase_fixes.py`, 2 older ones updated to the trimmed lists); live: 2,129-question random sample of the full scenario set + 21 Sep inputs **2,129/2,129**, use cases **71/71**, follow-ups **18/18** | VPN up |
| 2026-09-28 (PMAY-G 21 Sep tester sheet recheck, KI-125) | pytest, 25 files | **1,055 passed** (incl. 2 new bare-year tests); live: 192/192 (21 Sep inputs: GABIL SONGGITCHAM breakdown + partial, KYNDONGTUBER, LASKEIN count + breakdowns, 12 districts x beneficiaries / financial summary / performance / 7 FYs, 63 dates) and use cases 71/71 (a first attempt lost 38 to a VPN drop and was re-run clean) | VPN flapping |
| 2026-09-28 (PMAY-G full scenario test, KI-099 to KI-105) | pytest, 25 files | **1,005 passed** (`test_pmay_usecase_fixes.py` now 65); scripts **14/14**; live context suite **43/43**; PMAY-G full scenario matrix **17,331/17,331** (first pass 17,297 — 34 failures diagnosed, fixed, re-run 34/34, then the whole matrix re-run; the 571 model-reachable questions re-run again after KI-105): every village × 3 phrasings, every block / district / state × 16 question types, every block / district × every FY, 63 dates, 99 comparisons, 110 model-path questions; follow-up conversations **18/18**; use cases **71/71**; Focus Plus 300/300 and MGNREGA 300/300 villages. Report `docs/PMAY_G_Full_Scenario_Test_Report_2026-09-28.xlsx`. Harness: scratchpad gen_all.py, bulk_run.py, bulk_judge.py, gen_mt.py, run_mt.py, build_all.py | VPN dropped twice (runner paused / 3 re-runs) |
| 2026-09-28 (PMAY-G village named beside its block, KI-098) | pytest, 25 files | **998 passed** (+5 in `test_pmay_usecase_fixes.py`, now 58); live: all 5,120 villages asked with their ", <BLOCK> block, <DISTRICT>" tail **5,120/5,120**; all 224 block questions **224/224**; 500 random bare-name villages **500/500**; 71 use-case questions **71/71**; offline: 0 of 448 block phrasings read as a village | VPN up |
| 2026-09-28 (PMAY-G use-case + all-blocks/all-villages fixes, KI-089 to KI-097) | pytest, 25 files | **993 passed** (+53 in `test_pmay_usecase_fixes.py`; 4 older tests that pinned PMAY-G outside the village gate or the out-of-area check were updated with the reason); scripts **14/14**; live context suite **43/43** (follow-up 29/29, SQL 20/20); PMAY-G use cases **28/28** (71/71 questions on 2 of 2 fresh live runs, identical answers; round 1 was 17/28); all blocks **224/224**; all villages **5,120/5,120** on the final code (round 1 4,393/5,120 → KI-096 fixed); Focus Plus regression 60/60 blocks + 300/300 villages; MGNREGA regression 60/60 blocks + 300/300 villages; 16 model-path probes correct | VPN up |
| 2026-09-28 (KI-025 database-outage handling) | pytest, 24 files | **939 passed** (+3 in `test_focusplus_usecase_fixes.py`); scripts **14/14**; live context suite **43/43**; live outage simulation (megh_db port unreachable) → `DatabaseUnavailableError`, no KB answer, no repairs; live regression 204/204 blocks + 200/200 random villages | VPN up |
| 2026-09-27 night (Focus Plus all blocks / all villages, KI-079 to KI-088) | pytest, 24 files | **936 passed**; scripts **14/14**; live context suite **43/43** (follow-up 29/29); bulk live run on the final code: **204/204 block + 7,026/7,026 village questions** against megh_db and the raw CSV (8 re-run on final code, 3/3); confirmation pass 204/204 + 1,000/1,000 random villages; MGNREGA regression 448/448 blocks + 300/300 villages | VPN flapping, runner paused and resumed |
| 2026-09-27 (Focus Plus use-case fixes, KI-060 to KI-067) | pytest, 22 files | **858 passed** (826 + 32 in `test_focusplus_usecase_fixes.py`); scripts **14/14**; live context suite **43/43** (follow-up 29/29); Focus Plus use cases: **39/39 questions on 2 of 2 fresh live runs (30/30 use cases)**, each figure checked against megh_db and the raw CSV; typed bank-pause flow checked live through three pauses | VPN up (dropped once mid-session, retried) |
| 2026-09-26 night (MGNREGA all blocks / all villages, KI-049 to KI-059) | pytest, 21 files | **826 passed** (748 + 78 in `test_mgnrega_usecase_fixes.py`); scripts **14/14**; live context suite **43/43** (one earlier run was 40/43, with scenario D failing on a DB-sync outage; D alone and the full suite re-run clean); bulk live run on the final code: **448/448 block + 12,850/12,850 village queries** against megh_db and the raw CSVs | VPN flapping, retried |
| 2026-09-26 (MGNREGA use-case fixes, KI-041 to KI-048) | pytest, 21 files | **789 passed** (748 + 41 in `test_mgnrega_usecase_fixes.py`); scripts **14/14**; live context suite **43/43** (follow-up 29/29, SQL 20/20); MGNREGA use cases: **43/43 queries on 2 of 2 fresh live runs (30/30 use cases)**, each figure checked against megh_db and the raw CSVs | VPN up |
| 2026-09-26 (scheme substitution, KI-039) | pytest, 20 files | **748 passed** (657 + 91 in `test_scheme_substitution.py`); scripts **14/14**; live context suite **43/43**; live substitution run: the reported conversation plus 6 scheme pairs and 3 negatives, all correct | VPN up |
| 2026-09-26 (context validation) | live end-to-end, 16 conversations / 53 turns (brief groups A–F, plus clarification, scheme switching, contamination, long answer, 8-turn) | **47 PASS, 1 PASS with a caveat, 5 FAIL**. Findings KI-032 to KI-038; report `docs/Context_Validation_Report_2026-09-26.md` | VPN up, no code changes |
| 2026-09-26 (after live fixes) | pytest, 19 files | **657 passed**, 0 failed (`test_context_hardening.py` now 61) | VPN up |
| 2026-09-26 (after live fixes) | plain scripts, 14 files | **14/14 exit 0** | VPN up |
| 2026-09-26 (context hardening) | pytest, 19 files | **649 passed**, 0 failed (596 + 53 new in `test_context_hardening.py`) | DB unreachable |
| 2026-09-26 (context hardening) | plain scripts, 14 files | **14/14 exit 0** | DB unreachable |
| 2026-09-26 (context hardening) | `tests/live_context_validation.py` (VPN up) | **43/43 checks**, follow-up resolution 29/29, SQL 20/20, turns across 3 workers via real Postgres; repeated twice | live gateway and DB |
| 2026-09-26 (context hardening) | `tests/live_context_validation.py --legacy` | 39/43, follow-up 25/29, SQL 18/18 | live A/B baseline |
| 2026-09-26 (context layer) | pytest, 18 files | **596 passed**, 0 failed (558 + 38 new in `test_context_semantic_state.py`) | DB unreachable |
| 2026-09-26 (context layer) | plain scripts, 14 files | **14/14** | DB unreachable |
| 2026-09-26 | pytest, 17 files | **558 passed**, 0 failed, 1 warning (Pydantic class-based `Config` deprecation) in 24.6 s | DB unreachable |
| 2026-09-26 | plain scripts, 14 files | **14/14 exit 0** | DB unreachable |
| 2026-09-26 | `pytest tests` (whole dir) | **INTERNALERROR** (`SystemExit` from a script at import), 214 s | KI-018 |
| 2026-09-24 | pytest, 16 files | 423 passed (TECHNICAL_BRIEF) | before later additions |

### Live A/B (2026-09-26)

| Metric (live, 2026-09-26, same 8 scenarios, one run each) | Legacy (pre-context work) | New |
|---|---|---|
| Checks passed | 39/43 | **43/43** |
| Follow-up resolution checks | 25/29 | **29/29** |
| SQL executed without the fallback | 18/18 | 20/20 (two more turns reached SQL) |
| Rewrite prompt, approximate tokens, mean / max | 236 / 279 | 217 / 240 |
| Classifier input tokens, gateway mean | 925 | 870 |
| SQL-generation input tokens, gateway mean | 10,196 | 10,280 |
| Rewrite stage p50 / p95 | 141 / 898 ms | 154 / 638 ms |
| SQL generation p50 / p95 | 715 / 1,010 ms | 728 / 998 ms |
| Whole turn p50 / p95 (excluding the DB sync) | 2,155 / 3,297 ms | 2,335 / 3,886 ms |
| DB `sync_in` p50 / p95 | 52 / 105 ms | 52 / 359 ms |
| DB `sync_out` p50 / p95 | 55 / 128 ms | 50 / 153 ms |

- The new run was repeated: two runs gave 43/43 both times.
- Legacy fails scenario A, T3: "How many beneficiaries were there?" was not treated as a
  follow-up, so it got a year pause and lost the Dalu / FY scope. It also fails one structural
  check in D.
- Turn latency is not an apples-to-apples comparison: the new run answered 2 more data turns,
  whose full SQL chains the legacy run never reached.
- The DB sync ran in both modes. The A/B isolates the context logic, and the old per-worker
  behaviour is covered offline.
- TTFT is not observable, because generation is not streamed.
- The SQL prompt measured by the gateway is about 10.2k tokens, including LIVE SCHEMA and the
  catalogue. The offline estimate of 3.0k–6.9k excludes those blocks.

## 4. Test → component map

**The numbers in brackets below are collected pytest cases** (`pytest --collect-only`,
2026-09-26). Many tests are parametrised, so the 21 files hold 382 `def test_` functions but 826
cases. Per file:
- `ac_flow_focus_legacy` 16, `ac_focus_legacy` 18, `asr_transcribe` 54, `cm_elevate_legacy` 83;
- `context_semantic_state` 38, `context_hardening` 61, `scheme_substitution` 91;
- `fewshot_ranking` 20, `focus_legacy_routing` 44, `focus_legacy_usecase_fixes` 42, `mgnrega_usecase_fixes` 84;
- `followup_scheme_scope` 8, `lookup_intent` 19, `pg_abbreviation` 22, `pg_name_lookup` 15;
- `rag_general_scoping` 38, `scheme_pick` 46, `scheme_recommendation` 70;
- `stale_clarification` 31, `year_gap_comparison` 17, `year_gap_tolerance` 15.


| Test file | Style (cases) | Protects | Component(s) |
|---|---|---|---|
| `test_security.py` | script | headers, body/field limits, endpoint gating, login throttle, error hygiene, secret guard | middleware, routers, main |
| `test_pmay_usecase_fixes.py` | pytest (53) | PMAY-G use-case + all-villages fixes (KI-089 to KI-097): facts-path shape detection and answers (exact ₹, differences, stages), model-path guards (year GROUP BY, AVG rates, LIMIT 1), house-status 'sanctioned' phrase, written dates, typo / plural blocks, village narrowing, stated zeros | pipeline `_pmay_*`, entity_resolver `resolve_house_status`, premise_check |
| `test_asr_transcribe.py` | pytest (54) | voice input format/lang/no-speech/prompt-echo | routers/query, llm.call_asr, asr_guard, web |
| `test_context_manager.py` | script | reference substitution, state carry, summary, memory | context_manager, conversation_memory |
| `test_context_relevance_and_contract.py` | pytest (177) | the 2026-09-29 conversation: request-verb scheme swaps ("give me for pmay") and the swap measure-gap pause (KI-180); typed replies to every chip pause (`_resume_option_pause`, `_paused_thread_antecedent`, router `remember_pause`, KI-181); unrelated messages ("who is harshit") and personal money requests do not inherit the scheme; real follow-ups keep a continuation signal; substitution for every registry scheme; bare "which one?" asks; WHK near-miss chips; Focus Plus stated amount (parse, guard, not-held pause, not-a-loan note); `_resolved_scope_missing` rejects K8-shaped SQL and does not over-validate (all years, CM Elevate, village, region, LIKE, apostrophe, date range); village name search (DB stubbed); KI-032 state clears; KI-030 all-years carried and persisted; the three-turn West Garo Hills → Dalu → beneficiaries plan; `pipeline_decision` log has labels, not text | context_policy, edge, entity_resolver, premise_check, pipeline, context_manager, session_store, context_budget |
| `test_scheme_substitution.py` | pytest (91) | "give me same / do the same / now for <scheme>" keeps the previous operation for every registry scheme; the rewrite swaps only the scheme (metric, geography, year, grouped, result-reference shapes); negatives (grouping/time/geography changes, informational, comparison) are untouched; never cached; 7th-scheme agnostic | pipeline `_scheme_substitution` / `is_scheme_substitution` / `_scheme_swap_rewrite`, context_policy, router |
| `test_context_hardening.py` | pytest (61) | cross-worker state A→B→C (KI-028/KI-001) incl. stale-write refusal, DB-down/slow-DB degradation, pending pause across workers; merge actions KEEP/REPLACE/CLEAR/REQUIRE_CLARIFICATION and what they drop from `prior_resolved`; follow-up kinds → layers; provenance sources; field-specific rewrite checks (year, metric, category, village names); 7th-scheme agnosticism; priority-aware SQL budget; per-call usage record | session_sync, session_store, context_policy, context_manager, pipeline, prompt_builder, context_budget, llm, router helpers |
| `live_context_validation.py` | **live script, not pytest** | scenarios A–G against the real gateway and DB, turns rotated across 3 workers through real Postgres; per-turn rewrite/state/SQL/answer/tiers; per-role tokens and p50/p95 latency; `--legacy` A/B | whole pipeline |
| `test_context_semantic_state.py` | pytest (38) | follow-up rewrite evidence tiers (no `answer[:300]`), provenance guard, scheme isolation for all 6 schemes, failed-turn state safety, typed scheme-pause resume, pause replies never cached, `prompt_context` logging, backward compatibility | context_manager, pipeline `rewrite_followup` / `_resume_scheme_pause`, prompt_builder, context_budget, router |
| `test_history_charts.py` | script | reopened chats redraw charts (`response` JSONB) | conversation_store, web |
| `test_stale_clarification.py` | pytest (31) | a new question is not merged into a stale pause; PG names are not geography | `_reply_abandons_scope_pause`, `_drop_producer_group_names` |
| `test_edge_conversational.py` | script | meta/conversational handling | edge |
| `test_lookup_intent.py` | pytest (19) | "is there any X named Y" → DATA | classify_intent, `_DATA_HINTS` |
| `test_followup_scheme_scope.py` | pytest (8) | follow-ups that switch scheme | looks_like_followup, rewrite |
| `test_scheme_pick.py`, `test_scheme_recommendation.py` | pytest (46, 70) | pick/recommend/why/fit, harmful-first ordering | pipeline step f |
| `test_rag_general_scoping.py` | pytest (38) | KB scheme scoping; no lost clarification pause; stale collection | `_run_pipeline` KNOWLEDGE, rag, kb_ingest |
| `test_fewshot_ranking.py` | pytest (20) | breakdown questions retrieve right-shaped exemplars | annotations IDF ranking |
| `test_verifier_missing_geo.py` | script | verifier false "missing geography" suppressed | `_verify_sql` filters |
| `test_cmelevate_usecase_fixes.py` | pytest (33) | CM Elevate use-case fixes KI-068 to KI-075: the Unresolved filter off programme totals; complete programme lists; the multi-programme split and combined total; pending at a level vs a plain pending; zero-count sectors kept; comparison differences; garbled two-label answers rebuilt; the file_status reading of pending; approved/rejected on file_status; the verifier scheme_specific false complaint; PRIME SEED routing | _cm_legacy_keep_unresolved_off_village, _cm_elevate_* SQL guards, _cme_* guarantees, _verifier_scheme_specific_complaint_is_false, _infer_scheme_from_terms |
| `test_focusplus_usecase_fixes.py` | pytest (56) | Focus Plus use-case fixes KI-060 to KI-067: shares (breakdown and recount-verified single count), comparison difference / higher side, complete named lists, rupee formatting, mis-grouped digits, SQL-literal numbers, bank-pause chips + typed resume, Focus Legacy bank KeyError, batch/tranche scope, resumed question remembered on a second pause; all-villages fixes: village narrowing, WHERE pin (place literals, subquery), twin-village LGD chips, district-alias collision, Nan block, BURMA, one-figure misquote, roman numerals | compose_response, _focusplus_answer_guarantees, _bank_clarification, _resume_scheme_pause, _needs_scope_clarification, _run_pipeline, router pause_question |
| `test_mgnrega_usecase_fixes.py` | pytest (84) | MGNREGA use-case fixes KI-041 to KI-048: block chip, SOUTH TURA, scheme vocabulary, integer division, village lists, comparisons, deterministic spend + person-days and admin queries, 100-days faithfulness, top-1 wording, lakh units | resolve_entities, _infer_scheme_from_terms, execute_with_repair, _answer_data, compose_response |
| `test_calendar_date_filter.py` | pytest (18) | KI-078: a calendar date (2017-11-28, 28/11/2017) is not back-filled as an FY, yields no premise figures, and satisfies the scope gate; real FY ranges, premises and the undated scope pause unchanged | _backfill_explicit_year, premise_check.extract_premises, _needs_scope_clarification |
| `test_mgnrega_split_facts.py` | script | employment and expenditure never joined; split-fact hint | execute_with_repair guards |
| `test_no_data_answer.py` | script | empty result → one line; typos don't cause it | compose_response, `_no_data_answer` |
| `test_cross_scheme_money.py` | script | "which scheme paid most" is deterministic across schemes | `_cross_scheme_money_answer` |
| `test_cross_scheme_collision.py`, `test_admin_level_collision.py` | script | block/village/AC level-collision chips per scheme | entity_resolver, gates |
| `test_block_backstop.py`, `test_block_parent_district.py` | script | named blocks survive extraction; a block is scoped to its own district | resolve_entities |
| `test_ac_full_results.py`, `test_ac_focus_legacy.py`, `test_ac_flow_focus_legacy.py` | script / pytest (18, 16) | AC questions return all rows; AC flow end to end for Focus Legacy | entity_resolver, prompt_builder, `_AC_CAPABLE_SCHEMES` |
| `test_focusplus_block_columns.py` | script | Focus Plus blocks use `block_name_raw` | prompt_builder entities block |
| `test_focus_legacy_routing.py` | pytest (44) | Focus Legacy wiring and scoping | scheme registries |
| `test_focus_legacy_usecase_fixes.py` | pytest (122) | the 2026-09-25 QA fixes (group size, list totals, verifier guards, overview retrieval); 2026-09-29 all-levels fixes KI-020, KI-145..165 (breakdowns from rows, month / rupee format, AC scheme routing and drill-down, PG-name parsing / older spellings / edge masking, village phrase, twins, chip pin, DATE_TRUNC cast) | pipeline, edge, entity_resolver (Focus Legacy) |
| `test_pg_name_lookup.py`, `test_pg_abbreviation.py` | pytest (15, 22) | PG name matching and suffix stripping; NULL-row answers; "PG" recognised | schema_context rule 5a, compose_response, edge |
| `test_year_gap_tolerance.py`, `test_year_gap_comparison.py` | pytest (15, 17) | absent year + valid year answered; comparisons substitute | `_apply_year_gap` |
| `test_cm_elevate_legacy.py` | pytest (83) | CM Elevate Legacy wiring, pinning, not-held, exact totals | pipeline (CM Legacy), schema_context |

## 5. Coverage by requested category

| Category | Status |
|---|---|
| Unit | Yes, extensively. Most tests call single functions |
| Integration (stubbed LLM, in-process pipeline) | Partial. Several suites drive `answer_question` with stubbed model calls |
| End-to-end (live server) | Only `smoke_restructure.py`, which needs a running server; not part of the routine run |
| SQL correctness vs DB | **None automated.** Guards are tested on SQL strings only |
| Scheme tests | Focus Legacy, CM Elevate Legacy and Focus Plus blocks have suites. PMAY-G has `test_pmay_usecase_fixes.py` (2026-09-28) plus the live use-case and all-blocks / all-villages reports |
| Clarification | Yes (stale pause, collisions, year gap, follow-ups) |
| Composer | Partial. `compose_response` / `_deterministic_answer` are exercised in `test_cm_elevate_legacy.py` and `test_pg_name_lookup.py`, and no-data in `test_no_data_answer.py`. No dedicated faithfulness-guard suite |
| Security | `test_security.py`. **No test that generated SQL cannot read `app.*`** (KI-004), and no prompt-injection test |
| Regression | Every suite is regression-oriented |
| Performance / load | **None.** `docs/INFERENCE_REQUIREMENTS.md` describes a gateway load test (`hey`), run by AIOps |

## 6. Rules for new tests

- Test the **real function**. A test that re-implemented the logic under test once stayed green
  after the production branch was disabled.
- Some tests pin exact source strings (e.g. call sites in `pipeline.py`). Keep those call sites
  verbatim, or update the test deliberately.
- `_SCHEME_DATA_YEARS` is overwritten at startup by `refresh_scheme_years`. Assert against the
  values the module actually holds.
- **Live QA harness:**
  - call `pipeline.answer_question` in-process with the `.venv` Python, from the repo root;
  - click the clarification chip a user would pick;
  - the DB and gateway need the VPN.

  System Python lacks `fastembed`, so KB answers silently degrade under it.
- Test name lookups **after** a place-scoped turn. Single-turn harnesses miss follow-up rewrite
  bugs.
