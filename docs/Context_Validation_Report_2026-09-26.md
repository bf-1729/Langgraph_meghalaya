# Context System Validation Report — 2026-09-26

End-to-end validation of the structured conversation-context system against the **real model gateway and megh_db**, through the application's own pipeline. **No application code or configuration was changed.** Raw capture (every hop, prompt make-up and model call): `logs/context_validation_2026-09-26.json`, local only because `logs/` is gitignored.

## 1. Summary

- **53 turns in 16 conversations**: **47 PASS, 1 PASS with a caveat, 5 FAIL**.
- The context layer itself behaved correctly in every group except two:
  - **K8** (context lost after a long conversation with a knowledge digression);
  - **H4** (a scheme switch carried the previous scheme's metric words).
- The other failures are answer-level issues the run surfaced:
  - an ambiguous place name resolved without asking (B3);
  - a beneficiary total double-counted across years (G2);
  - block/village labelling (J3).
- **What held up**, live and across 3 simulated workers sharing state through Postgres:
  - geography, year, metric and grouping changes;
  - clarification resumes;
  - result references;
  - contamination resistance;
  - a ~60k-character previous answer kept out of the prompt;
  - cross-worker state.
- **This does not show the system is error-free.** One run each: model output can vary between runs.

### Findings (new; recorded in KNOWN_ISSUES.md as KI-032 to KI-038)

| # | Severity | Finding | Seen in |
|---|---|---|---|
| F1 | High | A merge plan's CLEAR is not applied to the COMMITTED state. After a district change, `state.block` still holds the old block and is inherited later, together with the new district (DALU inside South Garo Hills). | K5 → K8, H5 |
| F2 | High | After a knowledge digression, the rewrite's previous turn is the knowledge turn (no filters). The model built the question from the **stale** turn-4 summary instead of the Known-context state line. The provenance check rejected it (correctly), leaving only the bare fragment. | K8 |
| F3 | High | For that fragment, the SQL model dropped every MANDATORY resolved entity (district, year) and the verifier returned ok. The answer was the statewide, all-years total. | K8 |
| F4 | High | "Total beneficiaries" across years was computed by summing per-year distinct counts: **199,099 vs 105,813 unique** (read-only query). The faithfulness guard passes it, because the number is in the result digest. | G2 |
| F5 | Medium (raises KI-031) | A scheme swap keeps the old metric words. The SQL counted PMAY-G houses **as `person_days`**, and the answer reports "person-days under PMAY-G". | H4 |
| F6 | Medium | "Tura" resolved to the West Garo Hills district (HQ-town alias) without the level-collision question. The data also has the block "Tura Municipal Board" (Focus Plus) and ACs North/South Tura. | B3 |
| F7 | Medium | A block-vs-village choice is not remembered, so the same pause is asked again. After "block" is chosen, the resolved entities and state still carry the village_code, and the answer calls a block figure a "village". | J2, J3 |
| F8 | Low | Provenance labels are imprecise. On a scope-pause resume, the scheme/metric from the paused question are labelled `model_inference`; a year counted back by the model is labelled `validated_database`. No functional effect today (neither is an inherited key). | G2-2, C2-3 |

Notes (not failures):
- **N1:** Focus Plus holds only FY 2022-23 and FY 2025-26. Every "FY 2024-25 / 2023-24" test in the brief therefore correctly hits the out-of-range pause, so the brief's expected FY 2024-25 states are unreachable on this data. A2/C2/D2/E2 repeat those groups with held years (C2 on MGNREGA, which has 4 consecutive years).
- **N2:** the brief was truncated at Test Group F, Q3 ("How many"). F3 was run as "How many?". Groups G–K cover the remaining items listed in the brief's objective.
- **N3:** Focus Plus block follow-ups filter `block_name_raw` only (the documented rule), so the inherited district is not applied. For Dalu FY 2025-26 that includes 51 beneficiaries whose rows map to South West Garo Hills (5,189 vs 5,138).
- **N4:** district breakdowns sometimes use `district_name_raw` rather than `lgd_district`. That gives slightly different district totals between turns (WGH 239,647,500 vs 239,442,500).
- **N5:** infrastructure: during group K the VPN link degraded. One SQL call took 23.0 s, one verifier call timed out and was skipped, and one DB call hit WinError 121. The pipeline degraded as designed (repair, then success).

## 2. Pre-test checks

| Check | Result |
|---|---|
| Database reachable | Yes: 10.48.242.4:5432; queries executed; scheme years loaded |
| Model gateway reachable | Yes: 10.48.242.4:443 |
| Classifier / rewrite model | `qwen4-deploy`, answered (94 calls) |
| SQL-generation model | `qwen-model`, answered (51 calls) |
| Verifier / composer | `qwen4-deploy` / `qwen35-9b`, answered |
| Latest code | The run executed **in-process with `.venv`** on the current working tree (`main` @ `7064ab6` plus the uncommitted context work). The uvicorn server already on 127.0.0.1:8300 (`--reload`) runs on **system Python**, which CLAUDE.md warns lacks `fastembed`. It was not used, and not changed. |
| Supported schemes | 6: MGNREGA, PMAY-G, Focus Plus, CM Elevate, Focus Legacy, CM Elevate Legacy |
| Scheme years (from megh_db) | MGNREGA: 2022-23, 2023-24, 2024-25, 2025-26; PMAY-G: 2017-18, 2018-19, 2019-20, 2020-21, 2021-22, 2022-23, 2023-24; Focus Plus: 2022-23, 2025-26; CM Elevate: (none loaded); Focus Legacy: 2021-22, 2022-23, 2024-25, 2025-26; CM Elevate Legacy: 2024-25, 2025-26 |
| Configuration | Unchanged. Shared state ON (3 simulated workers per conversation, synced through `app.conversations`); authorization skipped (in-process, no scope), as in the documented QA harness. Temporary rows under `live-ctx-val-*` were deleted afterwards. |
| Unexpected clarifications | Answered as a user would click (TESTING.md): the "all / combined" option, else the first. Shown per turn. |

## 3. Performance (live)

| Role | Model | Calls | Input tokens mean | Input total | Output total | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|---|---|
| classifier | `qwen4-deploy` | 94 | 811 | 76,188 | 1,701 | 140 | 639 | 746 |
| sql | `qwen-model` | 51 | 10,455 | 533,192 | 2,426 | 640 | 2,485 | 23,002 |
| sql_verify | `qwen4-deploy` | 50 | 3,688 | 184,408 | 2,964 | 389 | 2,372 | 3,624 |
| compose | `qwen35-9b` | 52 | 857 | 44,578 | 2,364 | 364 | 1,053 | 1,779 |

- Per request, p50 / p95: 1,760 / 5,990 ms over 68 requests (including clarification hops; excluding the DB sync).
- Rewrite prompt: 31 calls, mean 223 and max 315 approximate tokens.
- SQL prompt: mean 10,714 approximate tokens (the gateway counted 10,455), against a 15,360 budget. **0 prompts over budget.**
- Shared state: `sync_in` applied the other worker's state 37 times (16 first turns had nothing to load, 0 unavailable). `sync_out` p50 51 ms, max 433 ms.
- Time to first token: not observable (generation is not streamed); it equals the call latency.

## 4. Results by group

### A — Basic follow-up (as written: FY 2024-25)

| Turn | Question | Verdict | Why |
|---|---|---|---|
| 1 | How much was disbursed under Focus Plus in West Garo Hills in FY 2024-25? | **PASS** | FY 2024-25 is not held for Focus Plus (it holds 2022-23 and 2025-26), so the out-of-range pause is correct. The run then clicked "All available years combined". |
| 2 | What about Dalu block? | **PASS** | Only the geography changed (block := DALU). Scheme, district (in state) and money metric were kept. The year could not be FY 2024-25 (not held). |
| 3 | How many beneficiaries were there? | **PASS** | The metric became a distinct beneficiary count; scheme, block and year scope were kept. |

<details><summary>Pipeline capture</summary>

**T1. How much was disbursed under Focus Plus in West Garo Hills in FY 2024-25?** (worker 0, shared state: none)

- State before: (empty)
- Clarification `year-out-of-range` — options: FY 2022-23, FY 2025-26, All available years combined; clicked: All available years combined
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'WEST GARO HILLS'}
- SQL prompt: 11,107 tokens — catalog 4141, few_shot 322, live_schema 913, prohibited_joins 244, question 25, resolved_entities 76, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus WHERE lgd_district = 'WEST GARO HILLS' LIMIT 1`
- Result: 1 row(s); head `[{"amount_raw": "391287500.00"}]`
- Answer: West Garo Hills shows 391287500.00 disbursed under Focus Plus. This figure covers all financial years for the district.
- Model calls: classifier qwen4-deploy 1150→10 tok 168 ms; sql qwen-model 10912→34 tok 307 ms; sql_verify qwen4-deploy 3633→6 tok 97 ms; compose qwen35-9b 723→35 tok 344 ms · request 1,415 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, metric=disbursement
- Provenance: scheme=current_user, district=current_user, metric=current_user

**T2. What about Dalu block?** (worker 1, shared state: applied)

- State before: scheme=Focus Plus, district=WEST GARO HILLS, metric=disbursement
- Context resolution: kind `GEOGRAPHY_CHANGE`, actions {'block': 'REPLACE', 'village': 'CLEAR'}; rewrite layers ['filters'], guard `ok`, 208 tokens
- Rewritten question: How much was disbursed under Focus Plus in Dalu block across all financial years
- Schemes / resolved entities: ['Focus Plus'] / {'block': 'DALU', 'district': 'WEST GARO HILLS'}
- SQL prompt: 11,182 tokens — catalog 4141, few_shot 319, live_schema 913, prohibited_joins 244, question 24, resolved_entities 154, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus WHERE UPPER(block_name_raw) = 'DALU' LIMIT 1`
- Result: 1 row(s); head `[{"amount_raw": "61762500.00"}]`
- Answer: 61,762,500.00 was disbursed under Focus Plus in Dalu block across all financial years.
- Model calls: classifier qwen4-deploy 209→17 tok 209 ms; classifier qwen4-deploy 1149→8 tok 87 ms; sql qwen-model 10989→33 tok 358 ms; sql_verify qwen4-deploy 3716→6 tok 80 ms; compose qwen35-9b 724→29 tok 218 ms · request 1,567 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, block=DALU, metric=disbursement
- Provenance: metric=previous_user, scheme=previous_user, district=previous_user, block=current_user

**T3. How many beneficiaries were there?** (worker 2, shared state: applied)

- State before: scheme=Focus Plus, district=WEST GARO HILLS, block=DALU, metric=disbursement
- Context resolution: kind `METRIC_CHANGE`, actions {'metric': 'REPLACE'}; rewrite layers ['filters'], guard `ok`, 216 tokens
- Rewritten question: How many beneficiaries were there under Focus Plus in Dalu block across all financial years?
- Schemes / resolved entities: ['Focus Plus'] / {'block': 'DALU', 'district': 'WEST GARO HILLS'}
- SQL prompt: 11,225 tokens — catalog 4141, few_shot 359, live_schema 913, prohibited_joins 244, question 27, resolved_entities 154, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus WHERE UPPER(block_name_raw) = 'DALU' LIMIT 1`
- Result: 1 row(s); head `[{"beneficiaries": 5189}]`
- Answer: There were 5,189 beneficiaries under Focus Plus in Dalu block. This figure covers all financial years for the scheme in that block.
- Model calls: classifier qwen4-deploy 218→18 tok 122 ms; classifier qwen4-deploy 1149→8 tok 82 ms; sql qwen-model 11022→33 tok 279 ms; sql_verify qwen4-deploy 3716→6 tok 137 ms; compose qwen35-9b 717→31 tok 305 ms · request 1,126 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, block=DALU, metric=beneficiaries
- Provenance: block=previous_user, metric=current_user, scheme=previous_user, district=previous_user

</details>

### A2 — Basic follow-up (FY 2025-26, a year Focus Plus holds)

| Turn | Question | Verdict | Why |
|---|---|---|---|
| 1 | How much was disbursed under Focus Plus in West Garo Hills in FY 2025-26? | **PASS** | WEST GARO HILLS + FY 2025-26 filters. |
| 2 | What about Dalu block? | **PASS** | Block := DALU; scheme, district (state), FY 2025-26 and the money metric were kept. Note N3: the SQL filters the block only. |
| 3 | How many beneficiaries were there? | **PASS** | Metric := beneficiaries; everything else kept. 5,189 includes 51 Dalu rows mapped to South West Garo Hills (N3). |

<details><summary>Pipeline capture</summary>

**T1. How much was disbursed under Focus Plus in West Garo Hills in FY 2025-26?** (worker 0, shared state: none)

- State before: (empty)
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'WEST GARO HILLS', 'year_key': 2025}
- SQL prompt: 11,122 tokens — catalog 4141, few_shot 335, live_schema 913, prohibited_joins 244, question 22, resolved_entities 81, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus WHERE lgd_district = 'WEST GARO HILLS' AND financial_year_short = '2025-26' LIMIT 1`
- Result: 1 row(s); head `[{"amount_raw": "239442500.00"}]`
- Answer: West Garo Hills had 239,442,500.00 disbursed under Focus Plus in FY 2025-26.
- Model calls: classifier qwen4-deploy 1156→22 tok 136 ms; sql qwen-model 10965→49 tok 467 ms; sql_verify qwen4-deploy 3665→72 tok 346 ms; compose qwen35-9b 744→37 tok 364 ms · request 1,670 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, year=2025, metric=disbursement
- Provenance: scheme=current_user, district=current_user, year=current_user, metric=current_user

**T2. What about Dalu block?** (worker 1, shared state: applied)

- State before: scheme=Focus Plus, district=WEST GARO HILLS, year=2025, metric=disbursement
- Context resolution: kind `GEOGRAPHY_CHANGE`, actions {'block': 'REPLACE', 'village': 'CLEAR'}; rewrite layers ['filters'], guard `ok`, 214 tokens
- Rewritten question: How much was disbursed under Focus Plus in Dalu block in FY 2025-26?
- Schemes / resolved entities: ['Focus Plus'] / {'block': 'DALU', 'year_key': 2025, 'district': 'WEST GARO HILLS'}
- SQL prompt: 11,200 tokens — catalog 4141, few_shot 336, live_schema 913, prohibited_joins 244, question 21, resolved_entities 159, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT COUNT(*) AS payments, SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus WHERE UPPER(block_name_raw) = 'DALU' AND financial_year_short = '2025-26' LIMIT 1`
- Result: 1 row(s); head `[{"payments": 14947, "amount_raw": "37367500.00"}]`
- Answer: Dalu block disbursed 37,367,500.00 under Focus Plus in FY 2025-26, with 14,947 payments recorded.
- Model calls: classifier qwen4-deploy 239→24 tok 140 ms; classifier qwen4-deploy 1155→20 tok 130 ms; sql qwen-model 11043→55 tok 758 ms; sql_verify qwen4-deploy 3756→137 tok 572 ms; compose qwen35-9b 755→45 tok 313 ms · request 2,528 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, block=DALU, year=2025, metric=disbursement
- Provenance: year=previous_user, metric=previous_user, scheme=previous_user, district=previous_user, block=current_user

**T3. How many beneficiaries were there?** (worker 2, shared state: applied)

- State before: scheme=Focus Plus, district=WEST GARO HILLS, block=DALU, year=2025, metric=disbursement
- Context resolution: kind `METRIC_CHANGE`, actions {'metric': 'REPLACE'}; rewrite layers ['filters'], guard `ok`, 221 tokens
- Rewritten question: How many beneficiaries were there under Focus Plus in Dalu block in FY 2025-26?
- Schemes / resolved entities: ['Focus Plus'] / {'block': 'DALU', 'year_key': 2025, 'district': 'WEST GARO HILLS'}
- SQL prompt: 11,201 tokens — catalog 4141, few_shot 334, live_schema 913, prohibited_joins 244, question 24, resolved_entities 159, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus WHERE UPPER(block_name_raw) = 'DALU' AND financial_year_short = '2025-26' LIMIT 1`
- Result: 1 row(s); head `[{"beneficiaries": 5189}]`
- Answer: There were 5,189 beneficiaries under Focus Plus in Dalu block for FY 2025-26.
- Model calls: classifier qwen4-deploy 248→24 tok 144 ms; classifier qwen4-deploy 1155→20 tok 130 ms; sql qwen-model 11038→48 tok 357 ms; sql_verify qwen4-deploy 3748→89 tok 423 ms; compose qwen35-9b 738→28 tok 302 ms · request 1,546 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, block=DALU, year=2025, metric=beneficiaries
- Provenance: year=previous_user, block=previous_user, metric=current_user, scheme=previous_user, district=previous_user

</details>

### B — Change geography

| Turn | Question | Verdict | Why |
|---|---|---|---|
| 1 | How many beneficiaries are there under Focus Plus in West Garo Hills? | **PASS** | No year given, so the year pause fired (All years chosen); district filter applied. |
| 2 | What about South Garo Hills? | **PASS** | Recognised as a DISTRICT change (district REPLACE, block/village cleared); 8,612. |
| 3 | What about Tura? | **FAIL** | "Tura" was resolved to the West Garo Hills DISTRICT (Tura is its HQ town) without asking. The project data also has the Focus Plus block "Tura Municipal Board" and MGNREGA ACs North/South Tura. The answer states 35,039 "in Tura", which is the whole district's figure (F6). This is entity resolution, not context carry. |

<details><summary>Pipeline capture</summary>

**T1. How many beneficiaries are there under Focus Plus in West Garo Hills?** (worker 0, shared state: none)

- State before: (empty)
- Clarification `year-not-specified` — options: FY 2022-23, FY 2025-26, All financial years combined; clicked: All financial years combined
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'WEST GARO HILLS'}
- SQL prompt: 11,144 tokens — catalog 4141, few_shot 356, live_schema 913, prohibited_joins 244, question 28, resolved_entities 76, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus WHERE lgd_district = 'WEST GARO HILLS';`
- Result: 1 row(s); head `[{"beneficiaries": 35039}]`
- Answer: West Garo Hills has 35,039 beneficiaries under Focus Plus across all financial years.
- Model calls: classifier qwen4-deploy 1150→10 tok 96 ms; sql qwen-model 10931→34 tok 293 ms; sql_verify qwen4-deploy 3629→6 tok 87 ms; compose qwen35-9b 716→22 tok 184 ms · request 1,496 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, metric=beneficiaries
- Provenance: scheme=clarification_response, district=clarification_response, metric=clarification_response

**T2. What about South Garo Hills?** (worker 1, shared state: applied)

- State before: scheme=Focus Plus, district=WEST GARO HILLS, metric=beneficiaries
- Context resolution: kind `GEOGRAPHY_CHANGE`, actions {'district': 'REPLACE', 'block': 'CLEAR', 'village': 'CLEAR'}; rewrite layers ['filters'], guard `ok`, 212 tokens
- Rewritten question: How many beneficiaries are there under Focus Plus in South Garo Hills across all financial years
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'SOUTH GARO HILLS'}
- SQL prompt: 11,171 tokens — catalog 4141, few_shot 382, live_schema 913, prohibited_joins 244, question 28, resolved_entities 76, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus WHERE lgd_district = 'SOUTH GARO HILLS';`
- Result: 1 row(s); head `[{"beneficiaries": 8612}]`
- Answer: There are 8,612 beneficiaries under Focus Plus in South Garo Hills across all financial years.
- Model calls: classifier qwen4-deploy 211→18 tok 118 ms; classifier qwen4-deploy 1150→10 tok 93 ms; sql qwen-model 10945→35 tok 381 ms; sql_verify qwen4-deploy 3631→6 tok 122 ms; compose qwen35-9b 715→23 tok 574 ms · request 1,760 ms
- State after: scheme=Focus Plus, district=SOUTH GARO HILLS, metric=beneficiaries
- Provenance: metric=previous_user, scheme=previous_user, district=current_user

**T3. What about Tura?** (worker 2, shared state: applied)

- State before: scheme=Focus Plus, district=SOUTH GARO HILLS, metric=beneficiaries
- Context resolution: kind `GEOGRAPHY_CHANGE`, actions {'block': 'REPLACE', 'village': 'CLEAR'}; rewrite layers ['filters'], guard `ok`, 210 tokens
- Rewritten question: How many beneficiaries are there under Focus Plus in Tura across all financial years
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'WEST GARO HILLS'}
- SQL prompt: 11,141 tokens — catalog 4141, few_shot 356, live_schema 913, prohibited_joins 244, question 25, resolved_entities 76, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus WHERE lgd_district = 'WEST GARO HILLS';`
- Result: 1 row(s); head `[{"beneficiaries": 35039}]`
- Answer: There are 35,039 beneficiaries under Focus Plus in Tura across all financial years. This figure is drawn from the single row of data provided for West Garo Hills district.
- Model calls: classifier qwen4-deploy 209→16 tok 114 ms; classifier qwen4-deploy 1148→8 tok 83 ms; sql qwen-model 10929→33 tok 340 ms; sql_verify qwen4-deploy 3627→108 tok 470 ms; compose qwen35-9b 714→40 tok 343 ms · request 1,835 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, metric=beneficiaries
- Provenance: metric=previous_user, scheme=previous_user, district=validated_database

</details>

### C — Change year (as written: Focus Plus 2024-25 -> 2023-24 -> year before)

| Turn | Question | Verdict | Why |
|---|---|---|---|
| 1 | How many beneficiaries were there under Focus Plus in West Garo Hills in FY 2024-25? | **PASS** | Honest out-of-range pause (FY 2024-25 not held). |
| 2 | What about FY 2023-24? | **PASS** | Honest out-of-range pause (FY 2023-24 not held). The requested year change cannot be demonstrated on Focus Plus data. |
| 3 | And the year before that? | **PASS** | No known year to count back from (all years was chosen), so it asked instead of guessing. |

<details><summary>Pipeline capture</summary>

**T1. How many beneficiaries were there under Focus Plus in West Garo Hills in FY 2024-25?** (worker 0, shared state: none)

- State before: (empty)
- Clarification `year-out-of-range` — options: FY 2022-23, FY 2025-26, All available years combined; clicked: All available years combined
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'WEST GARO HILLS'}
- SQL prompt: 11,144 tokens — catalog 4141, few_shot 356, live_schema 913, prohibited_joins 244, question 28, resolved_entities 76, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus WHERE lgd_district = 'WEST GARO HILLS';`
- Result: 1 row(s); head `[{"beneficiaries": 35039}]`
- Answer: West Garo Hills had 35,039 beneficiaries under Focus Plus across all financial years.
- Model calls: classifier qwen4-deploy 1150→10 tok 96 ms; sql qwen-model 10931→34 tok 341 ms; sql_verify qwen4-deploy 3629→6 tok 86 ms; compose qwen35-9b 716→22 tok 180 ms · request 1,164 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, metric=beneficiaries
- Provenance: scheme=current_user, district=current_user, metric=current_user

**T2. What about FY 2023-24?** (worker 1, shared state: applied)

- State before: scheme=Focus Plus, district=WEST GARO HILLS, metric=beneficiaries
- Clarification `year-out-of-range` — options: FY 2022-23, FY 2025-26, All available years combined; clicked: All available years combined
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'WEST GARO HILLS'}
- SQL prompt: 11,144 tokens — catalog 4141, few_shot 356, live_schema 913, prohibited_joins 244, question 28, resolved_entities 76, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus WHERE lgd_district = 'WEST GARO HILLS';`
- Result: 1 row(s); head `[{"beneficiaries": 35039}]`
- Answer: West Garo Hills had 35,039 beneficiaries under Focus Plus across all financial years.
- Model calls: classifier qwen4-deploy 1150→10 tok 90 ms; sql qwen-model 10931→34 tok 279 ms; sql_verify qwen4-deploy 3629→6 tok 85 ms; compose qwen35-9b 716→22 tok 184 ms · request 1,112 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, metric=beneficiaries
- Provenance: metric=current_user, scheme=current_user, district=current_user

**T3. And the year before that?** (worker 2, shared state: applied)

- State before: scheme=Focus Plus, district=WEST GARO HILLS, metric=beneficiaries
- Clarification `year-not-specified` — options: FY 2022-23, FY 2025-26, All financial years combined; clicked: All financial years combined
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'WEST GARO HILLS'}
- SQL prompt: 11,161 tokens — catalog 4141, few_shot 359, live_schema 913, prohibited_joins 244, question 41, resolved_entities 76, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus WHERE lgd_district = 'WEST GARO HILLS';`
- Result: 1 row(s); head `[{"beneficiaries": 35039}]`
- Answer: There were 35,039 beneficiaries under Focus Plus in West Garo Hills across all financial years. This figure represents the total count for the district as requested.
- Model calls: classifier qwen4-deploy 1159→10 tok 94 ms; sql qwen-model 10948→49 tok 924 ms; sql_verify qwen4-deploy 3638→200 tok 916 ms; compose qwen35-9b 725→36 tok 268 ms · request 3,296 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, metric=beneficiaries
- Provenance: metric=current_user, scheme=current_user, district=current_user

</details>

### C2 — Change year (MGNREGA holds 2022-23..2025-26)

| Turn | Question | Verdict | Why |
|---|---|---|---|
| 1 | How many person-days were generated under MGNREGA in West Garo Hills in FY 2025-26? | **PASS** | MGNREGA, WEST GARO HILLS, FY 2025-26. |
| 2 | What about FY 2024-25? | **PASS** | Year only: 2025 -> 2024; scheme/district/metric kept. |
| 3 | And the year before that? | **PASS** | "And the year before that?" -> FY 2023-24 (year_key 2023); rest kept. Provenance labels it validated_database rather than resolved_reference (F8). |

<details><summary>Pipeline capture</summary>

**T1. How many person-days were generated under MGNREGA in West Garo Hills in FY 2025-26?** (worker 0, shared state: none)

- State before: (empty)
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['MGNREGA'] / {'district': 'WEST GARO HILLS', 'year_key': 2025}
- SQL prompt: 7,909 tokens — catalog 3281, few_shot 478, live_schema 1232, prohibited_joins 104, question 25, resolved_entities 81, schema_backbone 2697, scheme_scope 10
- SQL: `SELECT SUM(person_days) AS person_days FROM curated.v_employment WHERE year_key = 2025 AND lgd_district = 'WEST GARO HILLS' GROUP BY lgd_district LIMIT 1`
- Result: 1 row(s); head `[{"person_days": 4987265}]`
- Answer: West Garo Hills generated 4,987,265 person-days under MGNREGA in FY 2025-26.
- Model calls: classifier qwen4-deploy 1159→22 tok 158 ms; sql qwen-model 7862→49 tok 429 ms; sql_verify qwen4-deploy 3887→200 tok 871 ms; compose qwen35-9b 741→34 tok 320 ms · request 2,174 ms
- State after: scheme=MGNREGA, district=WEST GARO HILLS, year=2025, metric=person-days
- Provenance: scheme=current_user, district=current_user, year=current_user, metric=current_user

**T2. What about FY 2024-25?** (worker 1, shared state: applied)

- State before: scheme=MGNREGA, district=WEST GARO HILLS, year=2025, metric=person-days
- Context resolution: kind `TIME_CHANGE`, actions {'year': 'REPLACE'}; rewrite layers ['filters'], guard `ok`, 209 tokens
- Rewritten question: How many person-days were generated under MGNREGA in West Garo Hills in FY 2024-25?
- Schemes / resolved entities: ['MGNREGA'] / {'district': 'WEST GARO HILLS', 'year_key': 2024}
- SQL prompt: 7,812 tokens — catalog 3281, few_shot 381, live_schema 1232, prohibited_joins 104, question 25, resolved_entities 81, schema_backbone 2697, scheme_scope 10
- SQL: `SELECT SUM(person_days) AS person_days FROM curated.v_employment WHERE year_key = 2024 AND lgd_district = 'WEST GARO HILLS' LIMIT 1`
- Result: 1 row(s); head `[{"person_days": 2808417}]`
- Answer: West Garo Hills generated 2,808,417 person-days under MGNREGA in FY 2024-25.
- Model calls: classifier qwen4-deploy 245→28 tok 166 ms; classifier qwen4-deploy 1159→22 tok 146 ms; sql qwen-model 7746→43 tok 383 ms; sql_verify qwen4-deploy 3881→6 tok 92 ms; compose qwen35-9b 741→34 tok 254 ms · request 1,453 ms
- State after: scheme=MGNREGA, district=WEST GARO HILLS, year=2024, metric=person-days
- Provenance: year=current_user, metric=previous_user, scheme=previous_user, district=previous_user

**T3. And the year before that?** (worker 2, shared state: applied)

- State before: scheme=MGNREGA, district=WEST GARO HILLS, year=2024, metric=person-days
- Context resolution: kind `TIME_CHANGE`, actions {'year': 'REPLACE'}; rewrite layers ['filters'], guard `ok`, 210 tokens
- Rewritten question: How many person-days were generated under MGNREGA in West Garo Hills in FY 2023-24?
- Schemes / resolved entities: ['MGNREGA'] / {'district': 'WEST GARO HILLS', 'year_key': 2023}
- SQL prompt: 7,838 tokens — catalog 3281, few_shot 406, live_schema 1232, prohibited_joins 104, question 25, resolved_entities 81, schema_backbone 2697, scheme_scope 10
- SQL: `SELECT SUM(person_days) AS person_days FROM curated.v_employment WHERE year_key = 2023 AND lgd_district = 'WEST GARO HILLS' LIMIT 1`
- Result: 1 row(s); head `[{"person_days": 5489616}]`
- Answer: West Garo Hills generated 5,489,616 person-days under MGNREGA in FY 2023-24.
- Model calls: classifier qwen4-deploy 239→28 tok 165 ms; classifier qwen4-deploy 1159→22 tok 330 ms; sql qwen-model 7767→43 tok 342 ms; sql_verify qwen4-deploy 3881→6 tok 94 ms; compose qwen35-9b 741→34 tok 337 ms · request 1,933 ms
- State after: scheme=MGNREGA, district=WEST GARO HILLS, year=2023, metric=person-days
- Provenance: year=validated_database, metric=previous_user, scheme=previous_user, district=previous_user

</details>

### D — Change metric (as written: FY 2024-25)

| Turn | Question | Verdict | Why |
|---|---|---|---|
| 1 | How much was disbursed under Focus Plus in West Garo Hills in FY 2024-25? | **PASS** | Out-of-range pause, then all years. |
| 2 | How many beneficiaries were there? | **PASS** | Metric only (money -> beneficiary count). |
| 3 | What was the average amount per beneficiary? | **PASS** | Average = 391,287,500 / 35,039 = 11,167.20, checked by hand; scope kept. |

<details><summary>Pipeline capture</summary>

**T1. How much was disbursed under Focus Plus in West Garo Hills in FY 2024-25?** (worker 0, shared state: none)

- State before: (empty)
- Clarification `year-out-of-range` — options: FY 2022-23, FY 2025-26, All available years combined; clicked: All available years combined
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'WEST GARO HILLS'}
- SQL prompt: 11,107 tokens — catalog 4141, few_shot 322, live_schema 913, prohibited_joins 244, question 25, resolved_entities 76, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus WHERE lgd_district = 'WEST GARO HILLS' LIMIT 1`
- Result: 1 row(s); head `[{"amount_raw": "391287500.00"}]`
- Answer: West Garo Hills shows 391287500.00 disbursed under Focus Plus. This figure covers all financial years for the district.
- Model calls: classifier qwen4-deploy 1150→10 tok 106 ms; sql qwen-model 10912→34 tok 284 ms; sql_verify qwen4-deploy 3633→6 tok 94 ms; compose qwen35-9b 723→35 tok 363 ms · request 1,547 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, metric=disbursement
- Provenance: scheme=current_user, district=current_user, metric=current_user

**T2. How many beneficiaries were there?** (worker 1, shared state: applied)

- State before: scheme=Focus Plus, district=WEST GARO HILLS, metric=disbursement
- Context resolution: kind `METRIC_CHANGE`, actions {'metric': 'REPLACE'}; rewrite layers ['filters'], guard `ok`, 211 tokens
- Rewritten question: How many beneficiaries were there under Focus Plus in West Garo Hills across all financial years?
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'WEST GARO HILLS'}
- SQL prompt: 11,144 tokens — catalog 4141, few_shot 356, live_schema 913, prohibited_joins 244, question 28, resolved_entities 76, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus WHERE lgd_district = 'WEST GARO HILLS';`
- Result: 1 row(s); head `[{"beneficiaries": 35039}]`
- Answer: West Garo Hills had 35,039 beneficiaries under Focus Plus across all financial years.
- Model calls: classifier qwen4-deploy 209→19 tok 129 ms; classifier qwen4-deploy 1150→10 tok 100 ms; sql qwen-model 10931→34 tok 664 ms; sql_verify qwen4-deploy 3629→6 tok 108 ms; compose qwen35-9b 716→22 tok 191 ms · request 2,148 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, metric=beneficiaries
- Provenance: metric=current_user, scheme=previous_user, district=previous_user

**T3. What was the average amount per beneficiary?** (worker 2, shared state: applied)

- State before: scheme=Focus Plus, district=WEST GARO HILLS, metric=beneficiaries
- Context resolution: kind `METRIC_CHANGE`, actions {'metric': 'REPLACE'}; rewrite layers ['filters'], guard `ok`, 217 tokens
- Rewritten question: What was the average amount per beneficiary under Focus Plus in West Garo Hills across all financial years?
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'WEST GARO HILLS'}
- SQL prompt: 11,198 tokens — catalog 4141, few_shot 407, live_schema 913, prohibited_joins 244, question 31, resolved_entities 76, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT SUM(amount_disbursed) AS amount_raw, COUNT(DISTINCT beneficiary_key) AS beneficiaries, ROUND( SUM(amount_disbursed)::numeric / NULLIF(COUNT(DISTINCT beneficiary_key), 0), 2 ) AS amount_per_beneficiary FROM curated.v_focus_plus WHERE lgd_district = 'WEST GARO HILLS' LIMIT 1`
- Result: 1 row(s); head `[{"amount_raw": "391287500.00", "beneficiaries": 35039, "amount_per_beneficiary": "11167.20"}]`
- Answer: The average amount per beneficiary under Focus Plus in West Garo Hills across all financial years was 11167.20. This figure is based on a total amount of 391287500.00 distributed among 35039 beneficiaries.
- Model calls: classifier qwen4-deploy 212→21 tok 143 ms; classifier qwen4-deploy 1152→10 tok 100 ms; sql qwen-model 10972→86 tok 628 ms; sql_verify qwen4-deploy 3690→6 tok 119 ms; compose qwen35-9b 754→61 tok 407 ms · request 1,940 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, metric=beneficiaries
- Provenance: metric=current_user, scheme=previous_user, district=previous_user

</details>

### D2 — Change metric (FY 2025-26)

| Turn | Question | Verdict | Why |
|---|---|---|---|
| 1 | How much was disbursed under Focus Plus in West Garo Hills in FY 2025-26? | **PASS** | FY 2025-26 filters. |
| 2 | How many beneficiaries were there? | **PASS** | Metric only; FY 2025-26 kept. |
| 3 | What was the average amount per beneficiary? | **PASS** | 239,442,500 / 35,039 = 6,833.60, checked by hand; scope kept. |

<details><summary>Pipeline capture</summary>

**T1. How much was disbursed under Focus Plus in West Garo Hills in FY 2025-26?** (worker 0, shared state: none)

- State before: (empty)
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'WEST GARO HILLS', 'year_key': 2025}
- SQL prompt: 11,122 tokens — catalog 4141, few_shot 335, live_schema 913, prohibited_joins 244, question 22, resolved_entities 81, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus WHERE lgd_district = 'WEST GARO HILLS' AND financial_year_short = '2025-26' LIMIT 1`
- Result: 1 row(s); head `[{"amount_raw": "239442500.00"}]`
- Answer: West Garo Hills had 239,442,500.00 disbursed under Focus Plus in FY 2025-26.
- Model calls: classifier qwen4-deploy 1156→22 tok 621 ms; sql qwen-model 10965→49 tok 379 ms; sql_verify qwen4-deploy 3665→72 tok 395 ms; compose qwen35-9b 744→37 tok 657 ms · request 2,395 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, year=2025, metric=disbursement
- Provenance: scheme=current_user, district=current_user, year=current_user, metric=current_user

**T2. How many beneficiaries were there?** (worker 1, shared state: applied)

- State before: scheme=Focus Plus, district=WEST GARO HILLS, year=2025, metric=disbursement
- Context resolution: kind `METRIC_CHANGE`, actions {'metric': 'REPLACE'}; rewrite layers ['filters'], guard `ok`, 217 tokens
- Rewritten question: How many beneficiaries were there under Focus Plus in West Garo Hills in FY 2025-26?
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'WEST GARO HILLS', 'year_key': 2025}
- SQL prompt: 11,161 tokens — catalog 4141, few_shot 371, live_schema 913, prohibited_joins 244, question 25, resolved_entities 81, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus WHERE lgd_district = 'WEST GARO HILLS' AND financial_year_short = '2025-26' LIMIT 1`
- Result: 1 row(s); head `[{"beneficiaries": 35039}]`
- Answer: There were 35,039 beneficiaries under Focus Plus in West Garo Hills in FY 2025-26.
- Model calls: classifier qwen4-deploy 239→25 tok 149 ms; classifier qwen4-deploy 1156→22 tok 150 ms; sql qwen-model 10980→49 tok 759 ms; sql_verify qwen4-deploy 3665→73 tok 427 ms; compose qwen35-9b 737→30 tok 343 ms · request 2,323 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, year=2025, metric=beneficiaries
- Provenance: year=previous_user, metric=current_user, scheme=previous_user, district=previous_user

**T3. What was the average amount per beneficiary?** (worker 2, shared state: applied)

- State before: scheme=Focus Plus, district=WEST GARO HILLS, year=2025, metric=beneficiaries
- Context resolution: kind `METRIC_CHANGE`, actions {'metric': 'REPLACE'}; rewrite layers ['filters'], guard `ok`, 222 tokens
- Rewritten question: What was the average amount per beneficiary under Focus Plus in West Garo Hills in FY 2025-26?
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'WEST GARO HILLS', 'year_key': 2025}
- SQL prompt: 11,123 tokens — catalog 4141, few_shot 331, live_schema 913, prohibited_joins 244, question 28, resolved_entities 81, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT SUM(amount_disbursed) AS amount_raw, COUNT(DISTINCT beneficiary_key) AS beneficiaries, ROUND( SUM(amount_disbursed)::numeric / NULLIF(COUNT(DISTINCT beneficiary_key), 0), 2 ) AS amount_per_beneficiary FROM curated.v_focus_plus WHERE lgd_district = 'WEST GARO HILLS' AND financial_year_short = '2025-26' LIMIT 1`
- Result: 1 row(s); head `[{"amount_raw": "239442500.00", "beneficiaries": 35039, "amount_per_beneficiary": "6833.60"}]`
- Answer: The average amount per beneficiary under Focus Plus in West Garo Hills for FY 2025-26 was 6833.60. This figure is based on a total amount of 239442500.00 distributed among 35039 beneficiaries.
- Model calls: classifier qwen4-deploy 242→27 tok 160 ms; classifier qwen4-deploy 1158→22 tok 143 ms; sql qwen-model 10949→101 tok 1,268 ms; sql_verify qwen4-deploy 3722→91 tok 730 ms; compose qwen35-9b 774→66 tok 440 ms · request 3,194 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, year=2025, metric=beneficiaries
- Provenance: year=previous_user, metric=current_user, scheme=previous_user, district=previous_user

</details>

### E — Aggregation (as written: FY 2024-25)

| Turn | Question | Verdict | Why |
|---|---|---|---|
| 1 | How much was disbursed under Focus Plus in FY 2024-25? | **PASS** | Out-of-range pause, then the area pause (All of Meghalaya): statewide total. |
| 2 | Show it by district. | **PASS** | GROUP BY district; no district filter leaked; scheme and metric kept. |
| 3 | Now show it by block. | **PASS** | Grouping switched to block; the district grouping did not leak. |

<details><summary>Pipeline capture</summary>

**T1. How much was disbursed under Focus Plus in FY 2024-25?** (worker 0, shared state: none)

- State before: (empty)
- Clarification `year-out-of-range` — options: FY 2022-23, FY 2025-26, All available years combined; clicked: All available years combined
- Clarification `scope-not-specified` — options: West Garo Hills, South West Garo Hills, East Khasi Hills, East Garo Hills, North Garo Hills; clicked: All of Meghalaya
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['Focus Plus'] / {}
- SQL prompt: 11,033 tokens — catalog 4141, few_shot 323, live_schema 913, prohibited_joins 244, question 26, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus LIMIT 1`
- Result: 1 row(s); head `[{"amount_raw": "1197392500.00"}]`
- Answer: A total of 1,197,392,500.00 was disbursed under Focus Plus across all financial years for all of Meghalaya.
- Model calls: classifier qwen4-deploy 1151→2 tok 74 ms; sql qwen-model 10804→22 tok 214 ms; sql_verify qwen4-deploy 3552→6 tok 92 ms; compose qwen35-9b 659→38 tok 667 ms · request 1,267 ms
- State after: scheme=Focus Plus, metric=disbursement
- Provenance: scheme=clarification_response, metric=clarification_response

**T2. Show it by district.** (worker 1, shared state: applied)

- State before: scheme=Focus Plus, metric=disbursement
- Context resolution: kind `GROUP_BY_CHANGE`, actions {'district': 'CLEAR', 'block': 'CLEAR', 'village': 'CLEAR', 'group_by': 'REPLACE'}; rewrite layers ['filters'], guard `ok`, 195 tokens
- Rewritten question: Show the disbursement under Focus Plus by district across all financial years for all of Meghalaya.
- Schemes / resolved entities: ['Focus Plus'] / {}
- SQL prompt: 11,036 tokens — catalog 4141, few_shot 323, live_schema 913, prohibited_joins 244, question 29, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT lgd_district, SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus GROUP BY lgd_district ORDER BY lgd_district LIMIT 1000`
- Result: 13 row(s); head `[{"lgd_district": "EASTERN WEST KHASI HILLS", "amount_raw": "15262500.00"}, {"lgd_district": "EAST GARO HILLS", "amount_raw": "126197500.00"}, {"lgd_district": "EAST JAINTIA HILLS", "amount_raw": "5050000.00"}]`
- Answer: Disbursements under Focus Plus total 1,197,392,500 across all districts in Meghalaya. The highest amount was 391,287,500 in WEST GARO HILLS, while the lowest recorded value was 12,500 in an unspecified district.
- Model calls: classifier qwen4-deploy 193→21 tok 132 ms; classifier qwen4-deploy 1152→2 tok 60 ms; sql qwen-model 10805→42 tok 428 ms; sql_verify qwen4-deploy 3574→6 tok 85 ms; compose qwen35-9b 1240→71 tok 476 ms · request 1,511 ms
- State after: scheme=Focus Plus, metric=disbursement
- Provenance: metric=previous_user, scheme=previous_user

**T3. Now show it by block.** (worker 2, shared state: applied)

- State before: scheme=Focus Plus, metric=disbursement
- Context resolution: kind `GROUP_BY_CHANGE`, actions {'block': 'CLEAR', 'village': 'CLEAR', 'group_by': 'REPLACE'}; rewrite layers ['filters'], guard `ok`, 198 tokens
- Rewritten question: Show the disbursement under Focus Plus by block across all financial years for all of Meghalaya.
- Schemes / resolved entities: ['Focus Plus'] / {}
- SQL prompt: 11,035 tokens — catalog 4141, few_shot 323, live_schema 913, prohibited_joins 244, question 28, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT UPPER(block_name_raw) AS block, COUNT(*) AS payments, SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus GROUP BY UPPER(block_name_raw) ORDER BY SUM(amount_disbursed) DESC LIMIT 1000`
- Result: 51 row(s); head `[{"block": "TIKRIKILLA", "payments": 30122, "amount_raw": "93120000.00"}, {"block": "SELSELLA", "payments": 29024, "amount_raw": "90337500.00"}, {"block": "ZIKZAK", "payments": 23208, "amount_raw": "71792500.00"}]`
- Answer: The disbursement under Focus Plus across all blocks in Meghalaya totals 1,197,392,500. This sum comprises 51 distinct blocks with individual payments ranging from 298 at RESUBELPARA-MUNICIPAL BOARD to 30,122 at TIKRIKILLA.
- Model calls: classifier qwen4-deploy 195→21 tok 133 ms; classifier qwen4-deploy 1152→2 tok 62 ms; sql qwen-model 10805→58 tok 466 ms; sql_verify qwen4-deploy 3592→6 tok 84 ms; compose qwen35-9b 2286→73 tok 740 ms · request 1,881 ms
- State after: scheme=Focus Plus, metric=disbursement
- Provenance: metric=previous_user, scheme=previous_user

</details>

### E2 — Aggregation (FY 2025-26)

| Turn | Question | Verdict | Why |
|---|---|---|---|
| 1 | How much was disbursed under Focus Plus in FY 2025-26? | **PASS** | FY 2025-26 statewide. |
| 2 | Show it by district. | **PASS** | By district, FY 2025-26 kept. The SQL grouped on district_name_raw, not lgd_district, so West Garo Hills shows 239,647,500 here and 239,442,500 in A2 (N4). |
| 3 | Now show it by block. | **PASS** | By block, FY 2025-26 kept; district grouping did not leak. |

<details><summary>Pipeline capture</summary>

**T1. How much was disbursed under Focus Plus in FY 2025-26?** (worker 0, shared state: none)

- State before: (empty)
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['Focus Plus'] / {'year_key': 2025}
- SQL prompt: 11,048 tokens — catalog 4141, few_shot 286, live_schema 913, prohibited_joins 244, question 18, resolved_entities 60, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT COUNT(*) AS payments, SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus WHERE financial_year_short = '2025-26' LIMIT 1`
- Result: 1 row(s); head `[{"payments": 292385, "amount_raw": "730962500.00"}]`
- Answer: Focus Plus disbursed 730962500.00 in FY 2025-26, with 292385 payments recorded.
- Model calls: classifier qwen4-deploy 1151→2 tok 64 ms; sql qwen-model 10904→43 tok 340 ms; sql_verify qwen4-deploy 3633→80 tok 443 ms; compose qwen35-9b 741→44 tok 316 ms; compose qwen35-9b 860→40 tok 292 ms · request 1,638 ms
- State after: scheme=Focus Plus, year=2025, metric=disbursement
- Provenance: scheme=current_user, year=current_user, metric=current_user

**T2. Show it by district.** (worker 1, shared state: applied)

- State before: scheme=Focus Plus, year=2025, metric=disbursement
- Context resolution: kind `GROUP_BY_CHANGE`, actions {'district': 'CLEAR', 'block': 'CLEAR', 'village': 'CLEAR', 'group_by': 'REPLACE'}; rewrite layers ['filters'], guard `ok`, 195 tokens
- Rewritten question: Show the disbursement under Focus Plus in FY 2025-26 by district.
- Schemes / resolved entities: ['Focus Plus'] / {'year_key': 2025}
- SQL prompt: 11,051 tokens — catalog 4141, few_shot 286, live_schema 913, prohibited_joins 244, question 20, resolved_entities 60, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT district_name_raw AS district, COUNT(*) AS payments, SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus WHERE financial_year_short = '2025-26' GROUP BY district_name_raw ORDER BY SUM(amount_disbursed) DESC LIMIT 100`
- Result: 13 row(s); head `[{"district": "West Garo Hills", "payments": 95859, "amount_raw": "239647500.00"}, {"district": "South West Garo Hills", "payments": 40918, "amount_raw": "102295000.00"}, {"district": "East Khasi Hills", "payments": 3803`
- Answer: Disbursements under Focus Plus in FY 2025-26 total 292,385 payments across 12 districts plus one row with a null district name. West Garo Hills recorded the highest disbursement with 95,859 payments, while the minimum was 3 payments in the row with a null district.
- Model calls: classifier qwen4-deploy 217→21 tok 127 ms; classifier qwen4-deploy 1152→2 tok 451 ms; sql qwen-model 10905→69 tok 904 ms; sql_verify qwen4-deploy 3661→80 tok 410 ms; compose qwen35-9b 1412→75 tok 510 ms · request 2,668 ms
- State after: scheme=Focus Plus, year=2025, metric=disbursement
- Provenance: year=previous_user, metric=previous_user, scheme=previous_user

**T3. Now show it by block.** (worker 2, shared state: applied)

- State before: scheme=Focus Plus, year=2025, metric=disbursement
- Context resolution: kind `GROUP_BY_CHANGE`, actions {'block': 'CLEAR', 'village': 'CLEAR', 'group_by': 'REPLACE'}; rewrite layers ['filters'], guard `ok`, 198 tokens
- Rewritten question: Show the disbursement under Focus Plus in FY 2025-26 by block.
- Schemes / resolved entities: ['Focus Plus'] / {'year_key': 2025}
- SQL prompt: 11,050 tokens — catalog 4141, few_shot 286, live_schema 913, prohibited_joins 244, question 20, resolved_entities 60, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT UPPER(block_name_raw) AS block, COUNT(*) AS payments, SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus WHERE financial_year_short = '2025-26' GROUP BY block_name_raw ORDER BY SUM(amount_disbursed) DESC LIMIT 1000`
- Result: 51 row(s); head `[{"block": "TIKRIKILLA", "payments": 22996, "amount_raw": "57490000.00"}, {"block": "SELSELLA", "payments": 21913, "amount_raw": "54782500.00"}, {"block": "ZIKZAK", "payments": 17699, "amount_raw": "44247500.00"}]`
- Answer: Disbursements under Focus Plus in FY 2025-26 total 730,962,500 across 51 blocks, with TIKRIKILLA receiving the highest amount of 57,490,000 and NAN the lowest of 7,500. The 51 distinct blocks range from TIKRIKILLA at 57,490,000 down to BAGHMARA-MUNICIPAL BOARD at 422,500.
- Model calls: classifier qwen4-deploy 219→21 tok 162 ms; classifier qwen4-deploy 1152→2 tok 71 ms; sql qwen-model 10905→71 tok 1,778 ms; sql_verify qwen4-deploy 3663→80 tok 444 ms; compose qwen35-9b 2344→115 tok 1,139 ms · request 3,809 ms
- State after: scheme=Focus Plus, year=2025, metric=disbursement
- Provenance: year=previous_user, metric=previous_user, scheme=previous_user

</details>

### F — Result references

| Turn | Question | Verdict | Why |
|---|---|---|---|
| 1 | Show the districts with the highest beneficiary counts under Focus Plus. | **PASS** | Ranking-count and year pauses (All of them / all years); ordered district list. |
| 2 | Which one had the highest? | **PASS** | Not treated as a result reference (a new ranking over the same scope); ORDER BY ... DESC LIMIT 1 -> WEST GARO HILLS, 35,039. |
| 3 | How many? | **PASS** | "How many?" (the brief is truncated here) gave 35,039, correct. It was solved with a subquery for the top district rather than from the previous result rows. |

<details><summary>Pipeline capture</summary>

**T1. Show the districts with the highest beneficiary counts under Focus Plus.** (worker 0, shared state: none)

- State before: (empty)
- Clarification `ranking-count-not-specified` — options: Top 3, Top 5, Top 10, All of them; clicked: All of them
- Clarification `year-not-specified` — options: FY 2022-23, FY 2025-26, All financial years combined; clicked: All financial years combined
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['Focus Plus'] / {}
- SQL prompt: 11,103 tokens — catalog 4141, few_shot 383, live_schema 913, prohibited_joins 244, question 37, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT lgd_district, COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus GROUP BY lgd_district ORDER BY beneficiaries DESC`
- Result: 13 row(s); head `[{"lgd_district": "WEST GARO HILLS", "beneficiaries": 35039}, {"lgd_district": "SOUTH WEST GARO HILLS", "beneficiaries": 15294}, {"lgd_district": "EAST KHASI HILLS", "beneficiaries": 13608}]`
- Answer: WEST GARO HILLS has the highest beneficiary count with 35,039. The remaining districts range from 15,294 in SOUTH WEST GARO HILLS down to 1 in the row with a null district name.
- Model calls: classifier qwen4-deploy 1155→2 tok 64 ms; sql qwen-model 10875→34 tok 340 ms; sql_verify qwen4-deploy 3569→6 tok 83 ms; compose qwen35-9b 1141→52 tok 480 ms · request 1,642 ms
- State after: scheme=Focus Plus, metric=beneficiaries
- Provenance: scheme=clarification_response, metric=clarification_response

**T2. Which one had the highest?** (worker 1, shared state: applied)

- State before: scheme=Focus Plus, metric=beneficiaries
- Context resolution: kind `FILTER_CHANGE`, actions {}; rewrite layers ['filters'], guard `ok`, 208 tokens
- Rewritten question: Which district had the highest beneficiary counts under Focus Plus across all financial years?
- Schemes / resolved entities: ['Focus Plus'] / {}
- SQL prompt: 11,034 tokens — catalog 4141, few_shot 323, live_schema 913, prohibited_joins 244, question 28, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT lgd_district, COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus GROUP BY lgd_district ORDER BY beneficiaries DESC LIMIT 1`
- Result: 1 row(s); head `[{"lgd_district": "WEST GARO HILLS", "beneficiaries": 35039}]`
- Answer: WEST GARO HILLS had 35,039 beneficiaries under Focus Plus. This is the highest count across all financial years.
- Model calls: classifier qwen4-deploy 199→16 tok 112 ms; classifier qwen4-deploy 1147→2 tok 62 ms; sql qwen-model 10800→37 tok 640 ms; sql_verify qwen4-deploy 3563→6 tok 83 ms; compose qwen35-9b 659→29 tok 237 ms · request 2,202 ms
- State after: scheme=Focus Plus, metric=beneficiaries
- Provenance: metric=previous_user, scheme=previous_user

**T3. How many?** (worker 2, shared state: applied)

- State before: scheme=Focus Plus, metric=beneficiaries
- Context resolution: kind `METRIC_CHANGE`, actions {'metric': 'REPLACE'}; rewrite layers ['filters'], guard `ok`, 194 tokens
- Rewritten question: How many beneficiaries were there under Focus Plus across all financial years in the district with the highest beneficiary counts?
- Schemes / resolved entities: ['Focus Plus'] / {}
- SQL prompt: 11,043 tokens — catalog 4141, few_shot 323, live_schema 913, prohibited_joins 244, question 37, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus WHERE lgd_district = ( SELECT lgd_district FROM curated.v_focus_plus GROUP BY lgd_district ORDER BY COUNT(DISTINCT beneficiary_key) DESC LIMIT 1 ) LIMIT 1`
- Result: 1 row(s); head `[{"beneficiaries": 35039}]`
- Answer: The district with the highest beneficiary counts under Focus Plus across all financial years had 35,039 beneficiaries.
- Model calls: classifier qwen4-deploy 188→22 tok 510 ms; classifier qwen4-deploy 1153→2 tok 321 ms; sql qwen-model 10806→66 tok 932 ms; sql_verify qwen4-deploy 3600→81 tok 378 ms; compose qwen35-9b 653→25 tok 440 ms · request 4,549 ms
- State after: scheme=Focus Plus, metric=beneficiaries
- Provenance: metric=previous_user, scheme=previous_user

</details>

### G — Clarification: scheme pause, typed reply

| Turn | Question | Verdict | Why |
|---|---|---|---|
| 1 | Show beneficiaries. | **PASS** | Asked which scheme. |
| 2 | Focus Plus | **FAIL (answer)** | The context part PASSES: the typed "Focus Plus" resumed "Show beneficiaries for Focus Plus", and the reply was not cacheable. But the answer says "199,099 total beneficiaries". That is 93,286 + 105,813 summed across years; the unique count is 105,813 (checked with a read-only query). F4. |

<details><summary>Pipeline capture</summary>

**T1. Show beneficiaries.** (worker 0, shared state: none)

- State before: (empty)
- Clarification `scheme-not-specified` — options: MGNREGA (rural employment), PMAY-G (rural housing), Focus Plus (farmer cash benefit), CM Elevate (livelihood / enterprise schemes), Focus Legacy (producer group payments); clicked: –
- State after: (empty)
- Provenance: (none)

**T2. Focus Plus** (worker 1, shared state: applied)

- State before: (empty)
- Clarification `year-not-specified` — options: FY 2022-23, FY 2025-26, All financial years combined; clicked: All financial years combined
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['Focus Plus'] / {}
- SQL prompt: 11,032 tokens — catalog 4141, few_shot 329, live_schema 913, prohibited_joins 244, question 19, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT financial_year_short, COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus GROUP BY financial_year_short ORDER BY financial_year_short LIMIT 100`
- Result: 2 row(s); head `[{"financial_year_short": "2022-23", "beneficiaries": 93286}, {"financial_year_short": "2025-26", "beneficiaries": 105813}]`
- Answer: Focus Plus recorded 199,099 total beneficiaries across the 2022-23 and 2025-26 financial years. The count was 93,286 in 2022-23 and 105,813 in 2025-26.
- Model calls: classifier qwen4-deploy 1142→2 tok 63 ms; sql qwen-model 10793→41 tok 395 ms; sql_verify qwen4-deploy 3563→6 tok 85 ms; compose qwen35-9b 819→74 tok 901 ms · request 3,091 ms
- State after: scheme=Focus Plus, metric=beneficiaries
- Provenance: scheme=current_user, metric=current_user

</details>

### G2 — Clarification: area/year pause, typed reply

| Turn | Question | Verdict | Why |
|---|---|---|---|
| 1 | How much was disbursed under Focus Plus? | **PASS** | Asked for area/year. |
| 2 | West Garo Hills, FY 2025-26 | **PASS** | Typed "West Garo Hills, FY 2025-26" merged into the paused question; correct filters. Provenance mislabels scheme/metric as model_inference (F8). |

<details><summary>Pipeline capture</summary>

**T1. How much was disbursed under Focus Plus?** (worker 0, shared state: none)

- State before: (empty)
- Clarification `scope-not-specified` — options: West Garo Hills, South West Garo Hills, East Khasi Hills, East Garo Hills, North Garo Hills; clicked: –
- State after: (empty)
- Provenance: (none)

**T2. West Garo Hills, FY 2025-26** (worker 1, shared state: applied)

- State before: (empty)
- Context resolution: kind `–`, actions {}
- Rewritten question: How much was disbursed under Focus Plus, West Garo Hills, FY 2025-26
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'WEST GARO HILLS', 'year_key': 2025}
- SQL prompt: 11,121 tokens — catalog 4141, few_shot 335, live_schema 913, prohibited_joins 244, question 21, resolved_entities 81, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus WHERE lgd_district = 'WEST GARO HILLS' AND financial_year_short = '2025-26' LIMIT 1`
- Result: 1 row(s); head `[{"amount_raw": "239442500.00"}]`
- Answer: 239,442,500.00 was disbursed under Focus Plus in West Garo Hills for FY 2025-26.
- Model calls: classifier qwen4-deploy 1156→22 tok 520 ms; sql qwen-model 10965→49 tok 2,576 ms; sql_verify qwen4-deploy 3665→73 tok 389 ms; compose qwen35-9b 744→37 tok 630 ms · request 5,846 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, year=2025, metric=disbursement
- Provenance: scheme=model_inference, district=clarification_response, year=clarification_response, metric=model_inference

</details>

### H — Scheme switching

| Turn | Question | Verdict | Why |
|---|---|---|---|
| 1 | How much was disbursed under Focus Plus in West Garo Hills in FY 2025-26? | **PASS** | Focus Plus, WGH, FY 2025-26. |
| 2 | How many person-days were generated under MGNREGA in West Garo Hills in FY 2024-25? | **PASS** | Explicit switch to MGNREGA; no Focus Plus in the SQL. |
| 3 | What about Dalu block? | **PASS** | Continued MGNREGA (v_employment) for Dalu, FY 2024-25. |
| 4 | What about PMAY-G? | **FAIL** | "What about PMAY-G?" kept MGNREGA's metric words. The SQL counted PMAY-G sanctioned houses AS person_days, and the answer says "3,724 person-days under PMAY-G" (F5, raises KI-031). No MGNREGA data was used, but the measure label leaked across schemes. |
| 5 | How many applications were received under CM Elevate? | **PASS** | Explicit CM Elevate question, area pause, 8,627 applications statewide. The state still holds WGH/Dalu/2024 from earlier turns (F1). |

<details><summary>Pipeline capture</summary>

**T1. How much was disbursed under Focus Plus in West Garo Hills in FY 2025-26?** (worker 0, shared state: none)

- State before: (empty)
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'WEST GARO HILLS', 'year_key': 2025}
- SQL prompt: 11,122 tokens — catalog 4141, few_shot 335, live_schema 913, prohibited_joins 244, question 22, resolved_entities 81, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus WHERE lgd_district = 'WEST GARO HILLS' AND financial_year_short = '2025-26' LIMIT 1`
- Result: 1 row(s); head `[{"amount_raw": "239442500.00"}]`
- Answer: West Garo Hills had 239,442,500.00 disbursed under Focus Plus in FY 2025-26.
- Model calls: classifier qwen4-deploy 1156→22 tok 703 ms; sql qwen-model 10965→49 tok 2,107 ms; sql_verify qwen4-deploy 3665→72 tok 1,265 ms; compose qwen35-9b 744→37 tok 603 ms · request 5,620 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, year=2025, metric=disbursement
- Provenance: scheme=current_user, district=current_user, year=current_user, metric=current_user

**T2. How many person-days were generated under MGNREGA in West Garo Hills in FY 2024-25?** (worker 1, shared state: applied)

- State before: scheme=Focus Plus, district=WEST GARO HILLS, year=2025, metric=disbursement
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['MGNREGA'] / {'district': 'WEST GARO HILLS', 'year_key': 2024}
- SQL prompt: 7,812 tokens — catalog 3281, few_shot 381, live_schema 1232, prohibited_joins 104, question 25, resolved_entities 81, schema_backbone 2697, scheme_scope 10
- SQL: `SELECT SUM(person_days) AS person_days FROM curated.v_employment WHERE year_key = 2024 AND lgd_district = 'WEST GARO HILLS' LIMIT 1`
- Result: 1 row(s); head `[{"person_days": 2808417}]`
- Answer: West Garo Hills generated 2,808,417 person-days under MGNREGA in FY 2024-25.
- Model calls: classifier qwen4-deploy 1159→22 tok 245 ms; sql qwen-model 7746→43 tok 771 ms; sql_verify qwen4-deploy 3881→6 tok 136 ms; compose qwen35-9b 741→34 tok 757 ms · request 2,910 ms
- State after: scheme=MGNREGA, district=WEST GARO HILLS, year=2024, metric=person-days
- Provenance: year=current_user, metric=current_user, scheme=current_user, district=current_user

**T3. What about Dalu block?** (worker 2, shared state: applied)

- State before: scheme=MGNREGA, district=WEST GARO HILLS, year=2024, metric=person-days
- Context resolution: kind `GEOGRAPHY_CHANGE`, actions {'block': 'REPLACE', 'village': 'CLEAR'}; rewrite layers ['filters'], guard `ok`, 214 tokens
- Rewritten question: How many person-days were generated under MGNREGA in Dalu block in FY 2024-25?
- Schemes / resolved entities: ['MGNREGA'] / {'block': 'DALU', 'year_key': 2024, 'district': 'WEST GARO HILLS'}
- SQL prompt: 7,925 tokens — catalog 3281, few_shot 478, live_schema 1232, prohibited_joins 104, question 24, resolved_entities 98, schema_backbone 2697, scheme_scope 10
- SQL: `SELECT SUM(person_days) AS person_days FROM curated.v_employment WHERE lgd_block = 'DALU' AND year_key = 2024 LIMIT 1`
- Result: 1 row(s); head `[{"person_days": 390921}]`
- Answer: 390,921 person-days were generated under MGNREGA in Dalu block in FY 2024-25.
- Model calls: classifier qwen4-deploy 244→27 tok 602 ms; classifier qwen4-deploy 1158→20 tok 710 ms; sql qwen-model 7879→40 tok 785 ms; sql_verify qwen4-deploy 3895→6 tok 480 ms; compose qwen35-9b 742→32 tok 1,053 ms · request 3,791 ms
- State after: scheme=MGNREGA, district=WEST GARO HILLS, block=DALU, year=2024, metric=person-days
- Provenance: year=previous_user, metric=previous_user, scheme=previous_user, district=previous_user, block=current_user

**T4. What about PMAY-G?** (worker 0, shared state: applied)

- State before: scheme=MGNREGA, district=WEST GARO HILLS, block=DALU, year=2024, metric=person-days
- Clarification `year-out-of-range` — options: FY 2017-18, FY 2018-19, FY 2019-20, FY 2020-21, FY 2021-22; clicked: All available years combined
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['PMAY-G'] / {'block': 'DALU'}
- SQL prompt: 7,111 tokens — catalog 2987, few_shot 444, live_schema 987, prohibited_joins 187, question 26, resolved_entities 73, schema_backbone 2394, scheme_scope 9
- SQL: `SELECT COUNT(*) AS person_days FROM curated.v_pmay WHERE NOT is_placeholder AND lgd_block = 'DALU' AND sanction_date IS NOT NULL LIMIT 1`
- Result: 1 row(s); head `[{"person_days": 3724}]`
- Answer: Dalu block generated 3,724 person-days under PMAY-G across all financial years.
- Model calls: classifier qwen4-deploy 1151→8 tok 486 ms; sql qwen-model 6863→41 tok 2,078 ms; sql_verify qwen4-deploy 3686→137 tok 2,785 ms; compose qwen35-9b 718→22 tok 593 ms; classifier qwen4-deploy 191→80 tok 729 ms · request 6,838 ms
- State after: scheme=PMAY-G, district=WEST GARO HILLS, block=DALU, year=2024, metric=person-days
- Provenance: year=previous_user, block=current_user, metric=current_user, scheme=current_user, district=previous_user

**T5. How many applications were received under CM Elevate?** (worker 1, shared state: applied)

- State before: scheme=PMAY-G, district=WEST GARO HILLS, block=DALU, year=2024, metric=person-days
- Clarification `scope-not-specified` — options: Ri Bhoi, West Garo Hills, East Khasi Hills, East Garo Hills, West Khasi Hills; clicked: All of Meghalaya
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['CM Elevate'] / {}
- SQL prompt: 10,647 tokens — catalog 3446, few_shot 340, live_schema 927, prohibited_joins 398, question 22, schema_backbone 5500, scheme_scope 10
- SQL: `SELECT COUNT(*) AS applications FROM curated.v_cm_elevate LIMIT 1`
- Result: 1 row(s); head `[{"applications": 8627}]`
- Answer: 8,627 applications were received under CM Elevate for all of Meghalaya.
- Model calls: classifier qwen4-deploy 1148→2 tok 82 ms; sql qwen-model 10175→19 tok 874 ms; sql_verify qwen4-deploy 3738→200 tok 3,624 ms; compose qwen35-9b 645→20 tok 634 ms · request 5,401 ms
- State after: scheme=CM Elevate, district=WEST GARO HILLS, block=DALU, year=2024, metric=applications
- Provenance: year=previous_user, block=previous_user, metric=clarification_response, scheme=clarification_response, district=previous_user

</details>

### I — Contamination (previous answer seeded with unrelated entities)

| Turn | Question | Verdict | Why |
|---|---|---|---|
| 1 | How much was disbursed under Focus Plus in West Garo Hills in FY 2025-26? | **PASS** | Baseline turn. |
| 2 | What about Dalu block? | **PASS** | The previous answer was seeded with Rongram, Selsella, villages, FY 2019-20, women, MGNREGA and PMAY-G; none appeared in the rewrite or SQL. |
| 3 | How many beneficiaries were there? | **PASS** | Same: clean rewrite and SQL; metric change only. |

<details><summary>Pipeline capture</summary>

**T1. How much was disbursed under Focus Plus in West Garo Hills in FY 2025-26?** (worker 0, shared state: none)

- State before: (empty)
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'WEST GARO HILLS', 'year_key': 2025}
- SQL prompt: 11,122 tokens — catalog 4141, few_shot 335, live_schema 913, prohibited_joins 244, question 22, resolved_entities 81, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus WHERE lgd_district = 'WEST GARO HILLS' AND financial_year_short = '2025-26' LIMIT 1`
- Result: 1 row(s); head `[{"amount_raw": "239442500.00"}]`
- Answer: West Garo Hills had 239,442,500.00 disbursed under Focus Plus in FY 2025-26.
- Model calls: classifier qwen4-deploy 1156→22 tok 577 ms; sql qwen-model 10965→49 tok 2,026 ms; sql_verify qwen4-deploy 3665→72 tok 921 ms; compose qwen35-9b 744→37 tok 277 ms · request 4,543 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, year=2025, metric=disbursement
- Provenance: scheme=current_user, district=current_user, year=current_user, metric=current_user

**T2. What about Dalu block?** (worker 1, shared state: applied)

- State before: scheme=Focus Plus, district=WEST GARO HILLS, year=2025, metric=disbursement
- Context resolution: kind `GEOGRAPHY_CHANGE`, actions {'block': 'REPLACE', 'village': 'CLEAR'}; rewrite layers ['filters'], guard `ok`, 214 tokens
- Rewritten question: How much was disbursed under Focus Plus in Dalu block in FY 2025-26?
- Schemes / resolved entities: ['Focus Plus'] / {'block': 'DALU', 'year_key': 2025, 'district': 'WEST GARO HILLS'}
- SQL prompt: 11,200 tokens — catalog 4141, few_shot 336, live_schema 913, prohibited_joins 244, question 21, resolved_entities 159, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT COUNT(*) AS payments, SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus WHERE UPPER(block_name_raw) = 'DALU' AND financial_year_short = '2025-26' LIMIT 1`
- Result: 1 row(s); head `[{"payments": 14947, "amount_raw": "37367500.00"}]`
- Answer: Dalu block disbursed 37,367,500.00 under Focus Plus in FY 2025-26, with 14,947 payments recorded.
- Model calls: classifier qwen4-deploy 239→24 tok 140 ms; classifier qwen4-deploy 1155→20 tok 478 ms; sql qwen-model 11043→55 tok 1,764 ms; sql_verify qwen4-deploy 3756→78 tok 1,707 ms; compose qwen35-9b 755→45 tok 1,016 ms · request 5,990 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, block=DALU, year=2025, metric=disbursement
- Provenance: year=previous_user, metric=previous_user, scheme=previous_user, district=previous_user, block=current_user

**T3. How many beneficiaries were there?** (worker 2, shared state: applied)

- State before: scheme=Focus Plus, district=WEST GARO HILLS, block=DALU, year=2025, metric=disbursement
- Context resolution: kind `METRIC_CHANGE`, actions {'metric': 'REPLACE'}; rewrite layers ['filters'], guard `ok`, 221 tokens
- Rewritten question: How many beneficiaries were there under Focus Plus in Dalu block in FY 2025-26?
- Schemes / resolved entities: ['Focus Plus'] / {'block': 'DALU', 'year_key': 2025, 'district': 'WEST GARO HILLS'}
- SQL prompt: 11,201 tokens — catalog 4141, few_shot 334, live_schema 913, prohibited_joins 244, question 24, resolved_entities 159, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus WHERE UPPER(block_name_raw) = 'DALU' AND financial_year_short = '2025-26' LIMIT 1`
- Result: 1 row(s); head `[{"beneficiaries": 5189}]`
- Answer: There were 5,189 beneficiaries under Focus Plus in Dalu block for FY 2025-26.
- Model calls: classifier qwen4-deploy 248→24 tok 553 ms; classifier qwen4-deploy 1155→20 tok 639 ms; sql qwen-model 11038→48 tok 2,084 ms; sql_verify qwen4-deploy 3748→87 tok 1,014 ms; compose qwen35-9b 738→28 tok 509 ms · request 4,996 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, block=DALU, year=2025, metric=beneficiaries
- Provenance: year=previous_user, block=previous_user, metric=current_user, scheme=previous_user, district=previous_user

</details>

### J — Long previous answer (~60k chars) and a result reference

| Turn | Question | Verdict | Why |
|---|---|---|---|
| 1 | List the blocks with beneficiaries under Focus Plus in West Garo Hills in FY 2025-26. | **PASS** | 11-block list for WGH FY 2025-26 (TIKRIKILLA top, 8,744). |
| 2 | What about the top one? | **PASS (with caveat)** | "What about the top one?" was a RESULT_REFERENCE. The rewrite got the result rows and resolved TIKRIKILLA; the block/village ambiguity pause is legitimate; 8,744 is correct. But after the user picked "block", the resolved entities and state still carry village_code 273249 (F7). |
| 3 | How much was disbursed there? | **FAIL (labelling)** | The number is correct (57,490,000 = the TIKRIKILLA block total). But the same block-or-village pause was asked again (the choice was not remembered), and the answer calls it "Tikrikilla village" (F7). The ~60k-char previous answer did NOT enter the prompt: the rewrite prompt was 233 tokens. |

<details><summary>Pipeline capture</summary>

**T1. List the blocks with beneficiaries under Focus Plus in West Garo Hills in FY 2025-26.** (worker 0, shared state: none)

- State before: (empty)
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'WEST GARO HILLS', 'year_key': 2025}
- SQL prompt: 11,124 tokens — catalog 4141, few_shot 334, live_schema 913, prohibited_joins 244, question 25, resolved_entities 81, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT UPPER(block_name_raw) AS block, COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus WHERE lgd_district = 'WEST GARO HILLS' AND financial_year_short = '2025-26' GROUP BY block_name_raw ORDER BY beneficiaries DESC LIMIT 100`
- Result: 11 row(s); head `[{"block": "TIKRIKILLA", "beneficiaries": 8744}, {"block": "SELSELLA", "beneficiaries": 7691}, {"block": "RONGRAM", "beneficiaries": 5331}]`
- Answer: All 11 blocks in West Garo Hills for FY 2025-26 recorded beneficiaries, totaling 35,039. The blocks are TIKRIKILLA (8,744), SELSELLA (7,691), RONGRAM (5,331), DALU (5,138), DADENGGIRI (4,755), GAMBEGRE (1,835), DEMDEMA (1,304), TURA MUNICIPAL BOARD-MUNICIPAL BOARD (210), ZIKZAK (25), DAMALGRE (5), a
- Model calls: classifier qwen4-deploy 1156→22 tok 238 ms; sql qwen-model 10955→70 tok 2,485 ms; sql_verify qwen4-deploy 3686→66 tok 832 ms; compose qwen35-9b 1101→146 tok 1,779 ms · request 7,351 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, year=2025, metric=beneficiaries
- Provenance: scheme=current_user, district=current_user, year=current_user, metric=current_user

**T2. What about the top one?** (worker 1, shared state: applied)

- State before: scheme=Focus Plus, district=WEST GARO HILLS, year=2025, metric=beneficiaries
- Clarification `entity-ambiguous` — options: The TIKRIKILLA block, The TIKRIKILLA village; clicked: The TIKRIKILLA block
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'WEST GARO HILLS', 'year_key': 2025, 'village_code': 273249}
- SQL prompt: 11,132 tokens — catalog 4141, few_shot 334, live_schema 913, prohibited_joins 244, question 36, resolved_entities 78, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus WHERE UPPER(block_name_raw) = 'TIKRIKILLA' AND lgd_district = 'WEST GARO HILLS' AND financial_year_short = '2025-26' LIMIT 1`
- Result: 1 row(s); head `[{"beneficiaries": 8744}]`
- Answer: 8,744 beneficiaries.
- Model calls: classifier qwen4-deploy 1170→32 tok 746 ms; sql qwen-model 10972→64 tok 2,027 ms; sql_verify qwen4-deploy 3698→80 tok 1,626 ms; compose qwen35-9b 758→51 tok 878 ms · request 5,526 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, village=273249, year=2025, metric=beneficiaries
- Provenance: year=current_user, metric=current_user, scheme=current_user, district=current_user, village=validated_database

**T3. How much was disbursed there?** (worker 2, shared state: applied)

- State before: scheme=Focus Plus, district=WEST GARO HILLS, village=273249, year=2025, metric=beneficiaries
- Clarification `entity-ambiguous` — options: The TIKRIKILLA block, The TIKRIKILLA village; clicked: The TIKRIKILLA block
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'WEST GARO HILLS', 'year_key': 2025, 'village_code': 273249}
- SQL prompt: 11,132 tokens — catalog 4141, few_shot 336, live_schema 913, prohibited_joins 244, question 34, resolved_entities 78, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus WHERE UPPER(block_name_raw) = 'TIKRIKILLA' AND lgd_district = 'WEST GARO HILLS' AND financial_year_short = '2025-26' LIMIT 1`
- Result: 1 row(s); head `[{"amount_raw": "57490000.00"}]`
- Answer: The amount disbursed under Focus Plus in Tikrikilla village, West Garo Hills district for FY 2025-26 is 57490000.00.
- Model calls: classifier qwen4-deploy 179→7 tok 86 ms; classifier qwen4-deploy 1170→32 tok 648 ms; sql qwen-model 10977→64 tok 1,245 ms; sql_verify qwen4-deploy 3698→157 tok 2,372 ms; compose qwen35-9b 765→43 tok 327 ms · request 5,167 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, village=273249, year=2025, metric=disbursement
- Provenance: year=current_user, metric=current_user, scheme=current_user, village=previous_user, district=current_user

</details>

### K — Long multi-turn conversation (8 turns)

| Turn | Question | Verdict | Why |
|---|---|---|---|
| 1 | How much was disbursed under Focus Plus in West Garo Hills in FY 2025-26? | **PASS** | Baseline. |
| 2 | What about Dalu block? | **PASS** | Block change. |
| 3 | How many beneficiaries were there? | **PASS** | Metric change. |
| 4 | And in FY 2022-23? | **PASS** | Year change to FY 2022-23 (a held year): 4,879. |
| 5 | What about South Garo Hills? | **PASS** | District change to South Garo Hills: 8,108. BUT the committed state still holds block=DALU after the plan CLEARed it (F1). |
| 6 | Show it by block. | **PASS** | By block within SGH FY 2022-23; 4 blocks sum to 8,108, consistent. |
| 7 | Who is eligible for Focus Plus? | **PASS** | A knowledge digression answered from the KB; the data state was left untouched. |
| 8 | And the total amount disbursed? | **FAIL** | "And the total amount disbursed?" should be SGH, FY 2022-23. Instead it answered 1,197,392,500, the statewide all-years total. Chain: (1) the rewrite model used the stale turn-4 summary ("West Garo Hills, Dalu block, FY 2025-26") and the provenance check correctly rejected it; (2) the fallback carried inherited filters, including the stale block=DALU (F1); (3) the SQL model emitted no WHERE clause and the verifier passed it (F2, F3). |

<details><summary>Pipeline capture</summary>

**T1. How much was disbursed under Focus Plus in West Garo Hills in FY 2025-26?** (worker 0, shared state: none)

- State before: (empty)
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'WEST GARO HILLS', 'year_key': 2025}
- SQL prompt: 11,122 tokens — catalog 4141, few_shot 335, live_schema 913, prohibited_joins 244, question 22, resolved_entities 81, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus WHERE lgd_district = 'WEST GARO HILLS' AND financial_year_short = '2025-26' LIMIT 1`
- Result: 1 row(s); head `[{"amount_raw": "239442500.00"}]`
- Answer: West Garo Hills had 239,442,500.00 disbursed under Focus Plus in FY 2025-26.
- Model calls: classifier qwen4-deploy 1156→22 tok 589 ms; sql qwen-model 10965→49 tok 23,002 ms; sql qwen-model 6582→49 tok 434 ms; sql_verify qwen4-deploy 3665→72 tok 328 ms; compose qwen35-9b 744→37 tok 264 ms · request 61,430 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, year=2025, metric=disbursement
- Provenance: scheme=current_user, district=current_user, year=current_user, metric=current_user

**T2. What about Dalu block?** (worker 1, shared state: applied)

- State before: scheme=Focus Plus, district=WEST GARO HILLS, year=2025, metric=disbursement
- Context resolution: kind `GEOGRAPHY_CHANGE`, actions {'block': 'REPLACE', 'village': 'CLEAR'}; rewrite layers ['filters'], guard `ok`, 214 tokens
- Rewritten question: How much was disbursed under Focus Plus in Dalu block in FY 2025-26?
- Schemes / resolved entities: ['Focus Plus'] / {'block': 'DALU', 'year_key': 2025, 'district': 'WEST GARO HILLS'}
- SQL prompt: 11,200 tokens — catalog 4141, few_shot 336, live_schema 913, prohibited_joins 244, question 21, resolved_entities 159, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT COUNT(*) AS payments, SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus WHERE UPPER(block_name_raw) = 'DALU' AND financial_year_short = '2025-26' LIMIT 1`
- Result: 1 row(s); head `[{"payments": 14947, "amount_raw": "37367500.00"}]`
- Answer: Dalu block disbursed 37,367,500.00 under Focus Plus in FY 2025-26, with 14,947 payments recorded.
- Model calls: classifier qwen4-deploy 239→24 tok 141 ms; classifier qwen4-deploy 1155→20 tok 123 ms; sql qwen-model 11043→55 tok 718 ms; sql_verify qwen4-deploy 3756→137 tok 573 ms; compose qwen35-9b 755→45 tok 337 ms · request 2,186 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, block=DALU, year=2025, metric=disbursement
- Provenance: year=previous_user, metric=previous_user, scheme=previous_user, district=previous_user, block=current_user

**T3. How many beneficiaries were there?** (worker 2, shared state: applied)

- State before: scheme=Focus Plus, district=WEST GARO HILLS, block=DALU, year=2025, metric=disbursement
- Context resolution: kind `METRIC_CHANGE`, actions {'metric': 'REPLACE'}; rewrite layers ['filters'], guard `ok`, 221 tokens
- Rewritten question: How many beneficiaries were there under Focus Plus in Dalu block in FY 2025-26?
- Schemes / resolved entities: ['Focus Plus'] / {'block': 'DALU', 'year_key': 2025, 'district': 'WEST GARO HILLS'}
- SQL prompt: 11,201 tokens — catalog 4141, few_shot 334, live_schema 913, prohibited_joins 244, question 24, resolved_entities 159, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus WHERE UPPER(block_name_raw) = 'DALU' AND financial_year_short = '2025-26' LIMIT 1`
- Result: 1 row(s); head `[{"beneficiaries": 5189}]`
- Answer: There were 5,189 beneficiaries under Focus Plus in Dalu block for FY 2025-26.
- Model calls: classifier qwen4-deploy 248→24 tok 139 ms; classifier qwen4-deploy 1155→20 tok 130 ms; sql qwen-model 11038→48 tok 782 ms; sql_verify qwen4-deploy 3748→89 tok 412 ms; compose qwen35-9b 738→28 tok 208 ms · request 1,877 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, block=DALU, year=2025, metric=beneficiaries
- Provenance: year=previous_user, block=previous_user, metric=current_user, scheme=previous_user, district=previous_user

**T4. And in FY 2022-23?** (worker 0, shared state: applied)

- State before: scheme=Focus Plus, district=WEST GARO HILLS, block=DALU, year=2025, metric=beneficiaries
- Context resolution: kind `TIME_CHANGE`, actions {'year': 'REPLACE'}; rewrite layers ['filters'], guard `ok`, 220 tokens
- Rewritten question: How many beneficiaries were there under Focus Plus in Dalu block in FY 2022-23?
- Schemes / resolved entities: ['Focus Plus'] / {'block': 'DALU', 'year_key': 2022, 'district': 'WEST GARO HILLS'}
- SQL prompt: 11,226 tokens — catalog 4141, few_shot 359, live_schema 913, prohibited_joins 244, question 24, resolved_entities 159, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus WHERE UPPER(block_name_raw) = 'DALU' AND financial_year_short = '2022-23' LIMIT 1`
- Result: 1 row(s); head `[{"beneficiaries": 4879}]`
- Answer: There were 4,879 beneficiaries under Focus Plus in Dalu block for FY 2022-23.
- Model calls: classifier qwen4-deploy 255→24 tok 150 ms; classifier qwen4-deploy 1155→20 tok 168 ms; sql qwen-model 11038→48 tok 832 ms; sql_verify qwen4-deploy 3748→70 tok 626 ms; compose qwen35-9b 738→28 tok 585 ms; classifier qwen4-deploy 184→65 tok 303 ms · request 2,838 ms
- State after: scheme=Focus Plus, district=WEST GARO HILLS, block=DALU, year=2022, metric=beneficiaries
- Provenance: year=current_user, block=previous_user, metric=previous_user, scheme=previous_user, district=previous_user

**T5. What about South Garo Hills?** (worker 1, shared state: applied)

- State before: scheme=Focus Plus, district=WEST GARO HILLS, block=DALU, year=2022, metric=beneficiaries
- Context resolution: kind `GEOGRAPHY_CHANGE`, actions {'district': 'REPLACE', 'block': 'CLEAR', 'village': 'CLEAR'}; rewrite layers ['filters'], guard `ok`, 315 tokens
- Rewritten question: How many beneficiaries were there under Focus Plus in South Garo Hills district in FY 2022-23?
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'SOUTH GARO HILLS', 'year_key': 2022}
- SQL prompt: 11,163 tokens — catalog 4141, few_shot 371, live_schema 913, prohibited_joins 244, question 28, resolved_entities 81, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus WHERE lgd_district = 'SOUTH GARO HILLS' AND year_key = 2022 LIMIT 1`
- Result: 1 row(s); head `[{"beneficiaries": 8108}]`
- Answer: There were 8,108 beneficiaries under Focus Plus in South Garo Hills district in FY 2022-23.
- Model calls: classifier qwen4-deploy 337→26 tok 593 ms; classifier qwen4-deploy 1157→22 tok 203 ms; sql qwen-model 10972→46 tok 399 ms; sql_verify qwen4-deploy 3663→6 tok 122 ms; compose qwen35-9b 737→30 tok 290 ms · request 1,748 ms
- State after: scheme=Focus Plus, district=SOUTH GARO HILLS, block=DALU, year=2022, metric=beneficiaries
- Provenance: year=previous_user, block=previous_user, metric=previous_user, scheme=previous_user, district=current_user

**T6. Show it by block.** (worker 2, shared state: applied)

- State before: scheme=Focus Plus, district=SOUTH GARO HILLS, block=DALU, year=2022, metric=beneficiaries
- Context resolution: kind `GROUP_BY_CHANGE`, actions {'block': 'CLEAR', 'village': 'CLEAR', 'group_by': 'REPLACE'}; rewrite layers ['filters'], guard `ok`, 314 tokens
- Rewritten question: Show the number of beneficiaries under Focus Plus in South Garo Hills district by block for FY 2022-23.
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'SOUTH GARO HILLS', 'year_key': 2022}
- SQL prompt: 11,163 tokens — catalog 4141, few_shot 368, live_schema 913, prohibited_joins 244, question 30, resolved_entities 81, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT UPPER(block_name_raw) AS block, COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus WHERE lgd_district = 'SOUTH GARO HILLS' AND financial_year_short = '2022-23' GROUP BY UPPER(block_name_raw) ORDER BY beneficiaries DESC LIMIT 100`
- Result: 4 row(s); head `[{"block": "CHOKPOT", "beneficiaries": 2667}, {"block": "BAGHMARA", "beneficiaries": 2230}, {"block": "GASUAPARA", "beneficiaries": 1859}]`
- Answer: South Garo Hills district recorded 8,108 total beneficiaries across all blocks in FY 2022-23. The distribution by block is 2,667 at CHOKPOT, 2,230 at BAGHMARA, 1,859 at GASUAPARA, and 1,352 at RONGARA.
- Model calls: classifier qwen4-deploy 332→28 tok 169 ms; classifier qwen4-deploy 1159→22 tok 350 ms; sql qwen-model 10966→73 tok 2,168 ms; sql_verify qwen4-deploy 3695→79 tok 509 ms; compose qwen35-9b 929→81 tok 520 ms · request 3,830 ms
- State after: scheme=Focus Plus, district=SOUTH GARO HILLS, block=DALU, year=2022, metric=beneficiaries
- Provenance: year=previous_user, block=previous_user, metric=previous_user, scheme=previous_user, district=previous_user

**T7. Who is eligible for Focus Plus?** (worker 0, shared state: applied)

- State before: scheme=Focus Plus, district=SOUTH GARO HILLS, block=DALU, year=2022, metric=beneficiaries
- Context resolution: kind `–`, actions {}
- Rewritten question: –
- Schemes / resolved entities: ['Focus Plus'] / {}
- SQL: ``
- Result: 0 row(s); head `[]`
- Answer: To be eligible for Focus Plus, a household must meet the following conditions:  - **Meghalaya residency** — The household must be a resident of Meghalaya in a village or block covered by the scheme rollout. - **Producer Group membership** — The household must be a registered member of a Producer Gro
- Model calls: compose qwen35-9b 1893→189 tok 1,217 ms · request 1,329 ms
- State after: scheme=Focus Plus, district=SOUTH GARO HILLS, block=DALU, year=2022, metric=beneficiaries
- Provenance: year=previous_user, block=previous_user, metric=previous_user, scheme=previous_user, district=previous_user

**T8. And the total amount disbursed?** (worker 1, shared state: applied)

- State before: scheme=Focus Plus, district=SOUTH GARO HILLS, block=DALU, year=2022, metric=beneficiaries
- Context resolution: kind `METRIC_CHANGE`, actions {'metric': 'REPLACE'}; rewrite layers ['filters'], guard `rejected:district`, 275 tokens
- Rewritten question: And the total amount disbursed under Focus Plus?
- Schemes / resolved entities: ['Focus Plus'] / {'district': 'SOUTH GARO HILLS', 'block': 'DALU', 'year_key': 2022}
- SQL prompt: 11,168 tokens — catalog 4141, few_shot 309, live_schema 913, prohibited_joins 244, question 16, resolved_entities 159, schema_backbone 5373, scheme_scope 10
- SQL: `SELECT COUNT(*) AS payments, SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus`
- Result: 1 row(s); head `[{"payments": 385671, "amount_raw": "1197392500.00"}]`
- Answer: The total amount disbursed under Focus Plus is 1197392500.00, with 385671 payments.
- Model calls: classifier qwen4-deploy 279→35 tok 184 ms; classifier qwen4-deploy 1142→2 tok 65 ms; sql qwen-model 10975→25 tok 232 ms; sql_verify qwen4-deploy 3712→6 tok 127 ms; compose qwen35-9b 661→35 tok 253 ms · request 1,006 ms
- State after: scheme=Focus Plus, district=SOUTH GARO HILLS, block=DALU, year=2022, metric=disbursement
- Provenance: year=previous_user, block=previous_user, metric=current_user, scheme=previous_user, district=previous_user

</details>

## 5. Reproduce

```bash
# in-process, VPN required. The runner is a scratch script around the app's own pipeline + router helpers;
# the maintained equivalent for scenarios A–G is:
.venv/Scripts/python.exe tests/live_context_validation.py
```

