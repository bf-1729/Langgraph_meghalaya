# Known Issues

*Only verified or clearly evidenced issues. Created 2026-09-26 from:*
- *the code (commit `7064ab6`);*
- *`docs/TECHNICAL_BRIEF.md` (a 2026-09-24 analysis of `logs/`; those log counts were not
  re-measured here);*
- *the 2026-09-25 QA records.*

*Update statuses as work lands. Severity: High = can produce a wrong or unsafe answer, or a
security exposure. Medium = bad UX or operability. Low = hygiene.*

| ID | Title | Status | Severity | Component |
|---|---|---|---|---|
| KI-001 | Clarification resume state is per-worker, in-memory | Fixed 2026-09-26 (offline + live-verified) | High | session_store / router / session_sync |
| KI-002 | SQL semantic verifier (4B) false positives drive repairs and hard failures | Open (mitigated by filters) | High | pipeline `_verify_sql` |
| KI-003 | Repair loop: no convergence check, no request id, failing SQL not logged | Open | Medium | pipeline, logging |
| KI-004 | Generated SQL can read `app.*` and the privacy tables; no table allowlist | Open | High (security) | db, deploy role |
| KI-005 | Auto-`LIMIT` skipped when "limit" appears anywhere in the SQL | Open | Low | db.run_readonly |
| KI-006 | Write-keyword check also matches inside string literals | Open | Low | db._assert_safe |
| KI-007 | Authorization geography/granularity regex ignores `block_name_raw` and AC columns | Open | Medium (security) | auth.authorize |
| KI-008 | `SQL_GENERATION_MAX_RETRIES` is dead config | Open | Low | config / pipeline |
| KI-009 | Broad `except Exception` turns programming bugs into "couldn't answer" | Open | Medium | pipeline `_run_pipeline` |
| KI-010 | SME YAMLs (classification/default/response/partitions) not loaded; hand-copied prose can drift | Open | Medium | annotations / schema_context |
| KI-011 | Prompt size grows linearly with schemes; classifier fallback selects all six | Open | Medium | pipeline / prompt_builder |
| KI-012 | No retry on transient model or DB errors | Open | Low | llm, db |
| KI-013 | Worst-case chain can exceed the 60 s ceiling → 504 | Open | Medium | pipeline / router |
| KI-014 | Per-IP, per-worker rate limit and caches | Open | Medium | middleware, cache |
| KI-015 | Gateway TLS verification falls back to `verify=False` | Open (by decision D-010) | Medium (security) | llm |
| KI-016 | JWT has no revocation | Open (accepted) | Low | security |
| KI-017 | No end-to-end NL→SQL accuracy benchmark | Open | High (process) | tests |
| KI-018 | `pytest tests` crashes on the plain-script suites | Open | Low | tests |
| KI-019 | Focus Legacy residual NULL blocks (source-blank) and TC-F1 expectation | Waiting on the ingestion team | Low | data (external) |
| KI-020 | Chatbot does not use the new PG-name alias view | **Fixed 2026-09-29, live-verified**: `_focus_legacy_pg_name_answer` searches current names AND earlier spellings (`v_focus_legacy_pg_search`, `NOT is_current`) together; an older spelling that equals another group's current name lists both; the answer says the typed name is an earlier spelling | Medium | pipeline `_focus_legacy_pg_name_answer` |
| KI-021 | Repo hygiene: stale `.claude/settings.json` entries, committed Excel lock files | Open | Low | repo |
| KI-022 | **Raw Focus Legacy file with unmasked bank accounts and names is committed and pushed** | Open | **Critical** (data protection) | repo root |
| KI-023 | Plaintext seed passwords for admin accounts committed in `app/users.yaml` | Open | High (security) | app/users.yaml, appdb seed |
| KI-024 | Local dev `.env` saves every voice upload (`ASR_DEBUG_DIR` set) | Open | Medium (privacy, dev box) | routers/query `_save_asr_sample` |
| KI-025 | Repair loop spent LLM repairs on infrastructure errors; a DB outage was answered from the reference documents ("not in the reference material") | **Fixed 2026-09-28** for connection loss (live-verified with a real outage); slow-query timeouts keep their 504 path | Medium | db `is_connection_error` / `DatabaseUnavailableError`, execute_with_repair, _run_pipeline, router 503 |
| KI-026 | DB pool `acquire()` has no timeout | Open | Medium | db |
| KI-027 | Scheme years and the live schema catalogue are loaded only at startup | Open | Medium | main lifespan, pipeline, schema_introspect |
| KI-028 | Follow-up context (the turn list) is per-worker; after a restart or on another worker a follow-up has no antecedent | Fixed 2026-09-26 (offline + live-verified) | High | session_store / router / session_sync |
| KI-030 | An "all years" choice is not carried into the next follow-up, so the year pause is asked again | **Fixed 2026-09-29, live-verified**: `ConversationState.year_all` + `context_manager.inject_year_scope` | Low | context_manager state / year gate |
| KI-031 | The deterministic scheme swap keeps the previous scheme's metric words ("person-days … under Focus Legacy") | Open — **raised to Medium** 2026-09-26: PMAY-G houses reported as "person-days" | Medium | pipeline `_scheme_swap_rewrite` |
| KI-032 | A merge plan's CLEAR is not applied to the committed state: an old block survives a district change and is inherited later | **Fixed 2026-09-29** (offline): `update_state` drops fields the plan CLEARs / REPLACEs and the result does not name; a question with no plan resets geography and year; live-verified (S2: Dalu dropped on South Garo Hills) | High | context_manager `update_state` |
| KI-033 | After a knowledge digression the rewrite built the question from a stale summary; the provenance check rejected it and the bare fragment lost the scope | Open | High | context_manager summary / rewrite evidence |
| KI-034 | SQL dropped all MANDATORY resolved entities on a fallback fragment and the verifier passed it → statewide all-years figure | **Fixed 2026-09-29** (offline): deterministic repair guard `_resolved_scope_missing` in `execute_with_repair`, before the verifier; live: fired correctly in S2 (inherited district dropped by the generator) and S8 (year); live suite 43/43; no false positive seen in the battery — a bulk block/village rate is still UNKNOWN — NEEDS VERIFICATION | High | pipeline `execute_with_repair` |
| KI-035 | "Total beneficiaries" across years = sum of per-year distinct counts (199,099 vs 105,813 unique) | Open | High | SQL generation / compose_response |
| KI-036 | "Tura" resolved to the West Garo Hills district (HQ-town alias) without asking; Focus Plus block "Tura Municipal Board" and ACs North/South Tura exist | Open | Medium | entity_resolver / level-collision gate |
| KI-037 | Block-vs-village choice not remembered (pause repeats); village_code kept after "block" chosen; block figure labelled "village" | Open | Medium | resolve_entities / pause resume / composer |
| KI-038 | Provenance labels imprecise on pause resumes and model-derived relative years | Open | Low | context_policy `provenance_for` |
| KI-039 | "give me same for <scheme>" answered as general scheme information (RAG) instead of the same query on that scheme | **Fixed 2026-09-26** (offline and live-verified) | High | pipeline `looks_like_followup` / `_scheme_substitution` |
| KI-040 | MGNREGA has no defined "beneficiary" measure: "beneficiaries" maps to `households_employed` or `persons_employed` depending on the SQL model, and summing them across years counts household-years | Open | Medium | schema_context MGNREGA rules / few-shots |
| KI-049 | A resumed village-disambiguation chip (", DEMDEMA block, WEST GARO HILLS") was read as a BLOCK question: the village was cleared and the block total was reported as the village's figure (BERUPARA 41.33 crore vs 50.34 lakh) | **Fixed 2026-09-26** (MGNREGA only; live-verified in the all-blocks / all-villages run) | High | resolve_entities level detection |
| KI-050 | Village-chip resume looped (the same "which village?" question returned): the extractor tagged the village as a block, or a ", the village" chip cleared the block scope | **Fixed 2026-09-26** (MGNREGA only; live-verified in the all-blocks / all-villages run) | High | resolve_entities village branch |
| KI-051 | Only 5 same-name villages were offered; ASIMGRE (7), BOLCHUGRE and UMSAW (6) had villages that could never be chosen | **Fixed 2026-09-26** (MGNREGA only; live-verified in the all-blocks / all-villages run) | Medium | resolve_entities chip lists |
| KI-052 | A bare block / constituency name the extractor tagged as a village (MAIRANG, KHATARSHNONG LAITKROH) was fuzzy-matched to look-alike villages, or failed into the KB fallback | **Fixed 2026-09-26** (MGNREGA only; live-verified in the all-blocks / all-villages run) | High | resolve_entities / collision gate (HQ-town district alias ends the check) |
| KI-053 | A qualified village name was truncated to a DIFFERENT village ("JONGCHIPARA (NOKAT)" answered as JONGCHIPARA: 8,848 vs 0 person-days) | **Fixed 2026-09-26** (MGNREGA only; live-verified in the all-blocks / all-villages run) | High | extract_entity_mentions / resolve_entities |
| KI-054 | SQL dropped the resolved village (block or statewide total reported as the village's; UMTYRNGA 106,669.85 lakh), or wrote a different village_code; the verifier also rejected correct single-village SQL | **Fixed 2026-09-26** (MGNREGA only; live-verified in the all-blocks / all-villages run) | High | generate_sql / execute_with_repair / _verify_sql |
| KI-055 | A village whose name contains "Garo" (THORIKAKONA GARO) triggered the "which Garo Hills district?" range prompt and lost the village | **Fixed 2026-09-26** (MGNREGA only; live-verified in the all-blocks / all-villages run) | Medium | _answer_data region handler |
| KI-056 | An area with no records, or with genuine zeros, was described as "null" / "the data doesn't cover" instead of "no records" / 0 (DEMDEMA, KHATARSHNONG LAITKROH) | **Fixed 2026-09-26** (MGNREGA only; live-verified in the all-blocks / all-villages run) | Medium | compose_response |
| KI-057 | Village names containing special words or punctuation broke resolution: a level word (BLOCK CAMPUS, BLOCK HQ.), an out-of-state place word (COAL INDIA COLONY, MANIPUR, BURMA), another scheme's word (UMRAN DAIRY → answered with CM Elevate money), "&" / "INCL" / a qualifier (MAWKOHMIT & MAWKYNSAH, LAWRIAT INCL DOMMUSUR, LOWER NALBARI - I), or a village the extractor put in the block slot (RONGRA) | **Fixed 2026-09-26** (MGNREGA only; live-verified) | High | resolve_entities / edge / scheme detection |
| KI-058 | Village lookup data quirks: an alias maps an exact village name to a different village too (WEST RANGASORA), and "Unresolved / Not Yet Mapped" placeholder rows (0 in MGNREGA's facts) appeared as village choices | **Fixed 2026-09-26** (MGNREGA only; live-verified) | Medium | resolve_village candidates (dim_geography_alias) |
| KI-059 | SQL around a resolved village: the generator added a WRONG district beside the correct village_code (MAWLIEH → no figure), or added a neighbour's code in an IN-list (MAWKOHMIT & MAWKYNSAH → 5,387 instead of 0) | **Fixed 2026-09-26** (MGNREGA only; live-verified) | High | generate_sql / execute_with_repair |
| KI-060 | Focus Plus: "counts **and percentages**" questions get counts only (FOCUS-008, 011–014, 016) | **Fixed 2026-09-27** (Focus Plus; live-verified 2 of 2 runs) | Medium | `_fp_breakdown_shares` / `_fp_single_count_share` |
| KI-061 | Focus Plus: a comparison gives the combined total instead of the difference / the higher one (FOCUS-026, 027, 028) | **Fixed 2026-09-27** (Focus Plus; live-verified) | Medium | `_fp_comparison` |
| KI-062 | Composer printed a malformed number "1,0263" for 10,263 (FOCUS-021b, both runs) | **Fixed 2026-09-27** (all schemes; live-verified) | High | `_fix_digit_grouping` in compose_response |
| KI-063 | "Which bank…?" asks Focus Plus vs Focus Legacy with no chips; the typed reply "Focus Plus" loses the question and gets a KB answer (FOCUS-029) | **Fixed 2026-09-27** (live-verified, chip and typed) | High | `_bank_clarification` / `SCHEME_PAUSE_RULES` |
| KI-064 | Focus Plus answer wording: money without Rs or separators, "31,317,500 amount raw.", no 12.5K-cohort caveat on gender/occupation/status | **Fixed 2026-09-27** (Focus Plus; SQL-literal part all schemes) | Low | `_fp_format_numbers` / `_sql_literal_numbers` / scope gate |
| KI-065 | Focus Plus: 175 rows (Dalu ↔ SWGH) sit in a different district in the DB than in raw, with no `has_geo_conflict` flag | Data-side — **row-level correction package handed over** (`docs/Focus_Plus_Data_Corrections_for_Ingestion_2026-09-28.xlsx`); awaiting the ingestion team's decision | Medium | data (external) |
| KI-066 | A typed reply that resumes a pause and then pauses AGAIN was remembered as the bare reply ("Focus Plus"), so the next typed reply ran as "Focus Plus, All of Meghalaya" — all schemes | **Fixed 2026-09-27** (live-verified) | High | router `pause_question` / turn_context `resumed_question` |
| KI-067 | A bank question naming Focus Legacy raised `KeyError` (no `_BANK_NOT_HELD_TEXT` entry) | **Fixed 2026-09-27** (live-verified: Meghalaya Rural Bank ₹292,875,000 = DB) | Medium | `_bank_clarification` |
| KI-068 | CM Elevate: "applicants under each program" adds `entity_type <> 'Unresolved'` to programme totals, dropping 51 applications (SEED 3,617 vs 3,633; 8 of 15 programmes low) (CM-ELEVATE-OFF-005) | **Fixed 2026-09-27** (CM Elevate; live-verified 2 of 2 runs) | High | `_UNRESOLVED_PLACEHOLDER_SCHEMES` + `_cme_complete_list` |
| KI-069 | CM Elevate: two named programmes + a district/status filter give only the combined total, no per-programme split (CM-ELEVATE-OFF-009b SEED + Tourism Vehicle; 029b SEED + Dairy); Piggery + Poultry variants do split | **Fixed 2026-09-27** (live-verified) | Medium | `_cm_elevate_split_scheme_in_list` + `_cme_multi_scheme_total` |
| KI-070 | CM Elevate: "pending at level 2" ANDs level 2 with On Hold, gets 0, and says the data "doesn't cover" it; 165 applications are at level 2, all file_status Pending (CM-ELEVATE-OFF-018a) | **Fixed 2026-09-27** (live-verified: 165) | High | `_cm_elevate_level_pending` / `_cm_elevate_pending_without_level` / zero backstop |
| KI-071 | CM Elevate: "compare … across districts" gives the four counts but no difference / higher side (CM-ELEVATE-OFF-027). The KI-061 fix is gated on Focus Plus | **Fixed 2026-09-27** (live-verified) | Medium | `_cme_comparison` |
| KI-072 | CM Elevate: "pending in each sector in <district>" filters On Hold before grouping, so sectors with 0 pending vanish and the answer says "No other sectors are listed" (CM-ELEVATE-OFF-028b West Khasi Hills) | **Fixed 2026-09-27** (live-verified) | Low | `_cm_elevate_sector_zero_groups` |
| KI-073 | CM Elevate: a scheme × sector × pendency answer is unreadable prose ("9 and 3 for Poultry and Piggery respectively… pending 1, 0, 14, 5, 0, and 0 for each corresponding row"); rows are correct (CM-ELEVATE-OFF-025a) | **Fixed 2026-09-27** (live-verified) | Medium | `_cme_grouped_rows` |
| KI-074 | CM Elevate "pending" = `data_verified = 'On Hold'` (953) by SME rule, but DB and raw both hold `scheme_specific ->> 'file_status'` = Pending 8,472 / Send Back 90 / Rejected 64 / Approved 1; schema_context's "no approval field exists" is out of date | **Decided 2026-09-28** (product owner): pending = verification On Hold only; the interim file-status line removed. Approved / rejected answer from `file_status` | Medium | schema_context vocabulary / `_cm_elevate_decision_status_column` |
| KI-075 | CM Elevate: a question naming PRIME SEED still asks "which scheme?" although SEED exists only in CM Elevate (006b, 007b, 008b, 016b, 021b, 023b, 030b); the answer after the chip is correct | **Fixed 2026-09-27** (live-verified) | Low | `_CMELEVATE_ONLY_TERMS` |
| KI-076 | Intent: a data question with no counting word ("Applicants under Goat Farming and Warehouse in West Khasi Hills?") routed to the KB as an eligibility question in 2 of 3 runs | **Fixed 2026-09-28** (`_APPLICANT_PLACE_CUE`; a knowledge word still wins) | Low | `classify_intent` / `_APPLICANT_PLACE_CUE` |
| KI-077 | MGNREGA women share with no year: the year gate skipped percentage questions, so the SQL model silently took FY 2025-26, whose women column is unrecorded, and the bot said "0.00% … a genuine zero" (East Khasi Hills is 77.70% in FY 2024-25) | **Fixed 2026-09-27** (MGNREGA only; live-verified) | High | year gate / SQL generation / composer notes |
| KI-078 | A calendar date read as a financial year: "How many PMAY houses were sanctioned on 2017-11-28?" ran the right SQL (504) but answered "not the 11 or 28 figures assumed … applies to FY 2017-18" | **Fixed 2026-09-27** (live-verified) | Medium | `_backfill_explicit_year` / `premise_check` / scope gate |
| KI-079 | Focus Plus villages / wards were not resolved: the single-village guards (whole-name lookup, chip pin, placeholder drop, SQL pin) were MGNREGA-only, so a village was filtered as a block (`block_name_raw = 'WARD NO. 4'`), a district, a non-existent `district_name_raw`, or answered with the whole block (Bhangarpar → Demdema's 1,304) | **Fixed 2026-09-27** (live: all-villages run) | High | resolve_entities village gates (`_village_scheme`), `_focusplus_pin_village_where` |
| KI-080 | Focus Plus village lookup picked a code with no Focus Plus data (Salpara - Ward No.9 → 277769, MASKA) — the generic lookup reads the statewide dim_geography | **Fixed 2026-09-27** | High | `_focusplus_narrow_village` |
| KI-081 | Two villages with the same name in the same block had identical chips ("BOLDAMGRE — SELSELLA block, …" ×2); the second was unreachable (10 twin groups, 20 villages) | **Fixed 2026-09-27** (MGNREGA + Focus Plus) | Medium | `_village_chip_label` / `_village_code_tag` / chip pin |
| KI-082 | A district HQ-town alias that is also a village ("BAGHMARA") settled as the district: the village question got South Garo Hills' 8,612 | **Fixed 2026-09-27** (Focus Plus) | High | `_focusplus_alias_district_collision` |
| KI-083 | Focus Plus "Nan" block (the source's blank-block placeholder, 1 beneficiary) refused as "not a C&RD block in this data" | **Fixed 2026-09-27** | Low | resolve_entities block branch, `_focusplus_answer_guarantees` |
| KI-084 | DB village totals exceed the raw file's code-only totals: the DB gave a village_code to all 102,923 rows the source left blank (57,643 to 'Unresolved' placeholders) and changed 21 coded rows; 445 villages | Data-side — **row-level package handed over** (same workbook); awaiting confirmation of the mapping rule | Info | data (external) |
| KI-085 | Beside the right village_code, the 30B put the block name in another column (`tranche_label ILIKE '%GASUAPARA%'`) → NULL for RONGBOKGRE | **Fixed 2026-09-27** | High | `_fp_place_literal` in `_focusplus_pin_village_where` |
| KI-086 | Village named through a `geography_key IN (SELECT … dim_geography …)` subquery (mixed-case name → NULL, NONGRIM HILLS) | **Fixed 2026-09-27** | High | `_focusplus_pin_village_where` |
| KI-087 | Composer wrote "1 beneficiary" for a result of 11 — the misquote check skips whole numbers under 100 (NONGLANG) | **Fixed 2026-09-27** (Focus Plus one-figure rule) | High | compose_response |
| KI-088 | Focus Plus village BURMA refused as "outside Meghalaya" (the village-or-outside check was MGNREGA-only) | **Fixed 2026-09-27** | Medium | `_mgnrega_village_not_out_of_area` |
| KI-089 | PMAY-G "financial summary" / some breakdowns and comparisons: the 30B groups by financial year, so the answer is a row dump (018b, 023b), a mislabelled sum of rounded yearly values ("sanctioned credit released 41.79 crore", 012) or invented statistics ("38,899 for both … mean 2,778.50 per district-year", 024b) | **Fixed 2026-09-28** (deterministic PMAY-G facts path + `_pmay_sql_issue` guard; live-verified) | High | SQL generation / compose_response |
| KI-090 | PMAY-G answers omit parts the use case requires: remaining amount (012, 016), which block is higher and by how much (023a), the district difference (024a), the sanctioned amount (025) | **Fixed 2026-09-28** (`_pmay_facts_answer` states every part; live-verified) | Medium | compose_response (no PMAY-G answer guarantees; compare `_focusplus_answer_guarantees`) |
| KI-091 | `resolve_entities` reads "received the full / part of the sanctioned amount … house" as house_status = 'House Sanctioned'; the SQL ignores it, but the composer says the count "applies to the House Sanctioned stage" (false; 009, 028) | **Fixed 2026-09-28** (`resolve_house_status` phrase match; live-verified) | High | resolve_entities / pmay_entity_resolver.yaml / composer |
| KI-092 | PMAY-G utilisation computed as AVG(released)/AVG(sanctioned) or as the mean of per-house ratios (98.82% / 98.46% vs the true released ÷ sanctioned 98.76% / 98.44%); intermittent (016) | **Fixed 2026-09-28** (ratio of totals in the facts path; `_pmay_sql_issue` repairs AVG) | High | SQL generation (PMAY vocabulary says released/sanctioned) |
| KI-093 | "Which has more X: A or B?" sometimes gets LIMIT 1, so only the winner is named ("ROHONPARA: 23 completed houses."; 022b run B) | **Fixed 2026-09-28** (one row per named area; `_pmay_comparison_limit`) | Medium | SQL generation / composer comparison rule |
| KI-094 | A written date "15 December 2020": the premise check read "15" as a figure the user assumed ("not the 15 the question assumes") and the answer said no date filter was applied; it also paused for an area (027b). ISO and dd/mm/yyyy dates are fine | **Fixed 2026-09-28** (written dates in `_CALENDAR_DATE_RE` and premise `_DATE_RE`) | Medium | premise_check `_DATE_RE` / scope gate `_CALENDAR_DATE_RE` |
| KI-095 | Village-level PMAY-G money is printed as crore to 2 decimals: "0.09 crore" for ₹9,40,000 (4.3% off), "0.04 crore" for ₹3,95,500 (007) | **Fixed 2026-09-28** (exact ₹ + lakh/crore in the facts path; `_pmay_rupee_format` on the model path) | Medium | SQL generation (ROUND(…/1e7, 2)) / composer |
| KI-096 | PMAY-G village resolution (all-villages run): an exact PMAY-G name paused against a fuzzy look-alike and the chip answered for the whole BLOCK (Bhangarpar → Demdema's 5,274); parenthesised names resolved to a code with no PMAY-G rows ("Bholarbhita (w)" → 272775, "Shyamding ( Garo )" → 904642 + a Garo Hills expansion); villages MANIPUR / BURMA refused as outside Meghalaya. 727 of 5,120 village questions | **Fixed 2026-09-28** (PMAY-G joins `_village_scheme`; `_pmay_village_names`; narrowing; out-of-area check) | High | resolve_entities / `_resolve_village_for` / `_mgnrega_village_not_out_of_area` |
| KI-097 | PMAY-G extra taps and wording: the sheet's typo "sactioned amount" asked "which scheme?"; "compare X and Y blocks" asked "block or village?"; a placeholder-only village (Rtiang Sanphew) got "the data doesn't cover … shows 0"; model-path money printed bare ("795,860,000.00") | **Fixed 2026-09-28** | Low | `_PMAY_ONLY_TERMS`, `_has_block_word`, `_pmay_no_houses_answer`, `_pmay_rupee_format` |
| KI-098 | PMAY-G: a village named beside its own block / district ("…in NONGSOHRAM across all financial years, RI MULIANG block, WEST KHASI HILLS") was answered with the whole BLOCK (₹33,84,000) — the word "block" set the level and every village branch was skipped; the block / district also arrived in varying slots (one-element block_list / district_list, or lost) | **Fixed 2026-09-28** (user report; PMAY-G only) | High | resolve_entities → `_pmay_village_beside_block` |
| KI-099 | PMAY-G: 'shyamding ( garo ), Demdema block' asked 'which Garo Hills district?' and answered for the whole block (18 questions). Cause: the hill-range check ran although a village was resolved — MGNREGA had the guard, PMAY-G did not. | **Fixed 2026-09-28** (full scenario run) | High | data path region check `_mg_place_settled` |
| KI-100 | PMAY-G: 'Old Bhaitbari … their house is still not completed' matched the alias 'old house' → filtered to the Existing site(Old House) stage → 0, and the zero answer printed the code '272820 village ( block, )'. | **Fixed 2026-09-28** (full scenario run) | High | entity_resolver `_HS_SANCTION_TOKENS`; `_pmay_entity_name` |
| KI-101 | PMAY-G: 'baghmara, Rerapara block across all financial years' / 'mawhati, Ranikor block' (villages named like OTHER blocks) answered for Baghmara / Mawhati block. | **Fixed 2026-09-28** (full scenario run) | High | resolve_entities tail parse → `_pmay_village_beside_block` |
| KI-102 | PMAY-G: 'model village, Umling block' answered for the village UMLING. | **Fixed 2026-09-28** (full scenario run) | High | resolve_entities → `_pmay_village_beside_block` |
| KI-103 | PMAY-G: 'Which has more completed houses: Nongtalang Mission or Sohkha Mission?' put one name in the block slot and the model compared a village with lgd_block = 'SOHKHA MISSION' (0). | **Fixed 2026-09-28** (full scenario run) | High | `_pmay_two_villages` |
| KI-104 | PMAY-G: '2,390 houses sanctioned in 2017' — year_key printed as a calendar year; it is FY 2017-18. | **Fixed 2026-09-28** (full scenario run) | Medium | `_pmay_fy_labels` |
| KI-105 | PMAY-G model path: 'average sanctioned amount per house in East Khasi Hills' answered '0.01 crore' (₹1,30,000) — ROUND(x / 10000000.0, 2) in the model's SQL | **Fixed 2026-09-28** (full scenario run) | High | `_pmay_crore_to_rupees` in execute_with_repair |
| KI-106 | CM Elevate villages were outside the shared village guards: a block total reported for the village, the village name in the district slot, a misspelled district beside the code (0), a neighbour's code (LENMAWTAP vs LENMAWTAP A), a chip that re-asked | **Fixed 2026-09-28** (all-villages run) | High | `_village_scheme` / `_VILLAGE_NARROW_SCHEMES` / `_cm_elevate_village_names` / `_focusplus_pin_village_where` / village-beside-block step |
| KI-107 | CM Elevate block catalogue older than its data: 21 of 66 stored blocks missing (all 9 urban bodies), 8 names spelled differently or absent. "Baghmara Municipal Board" → rural BAGHMARA (56 vs 18); "Jowai Municipal Board" → JOWAI (0 rows); RI-MULIANG never matched "RI MULIANG" | **Fixed 2026-09-28** (all-blocks run) | High | `_cm_elevate_blocks_to_data` (live `lgd_block` spellings) |
| KI-108 | The single-village WHERE rebuild split on AND inside string literals ("…Empowerment and Development (SEED)" → "…AND…", 0 rows) and could rebuild from a WHERE inside `COUNT(*) FILTER (WHERE …)` — Focus Plus and CM Elevate | **Fixed 2026-09-28** | High | `_focusplus_pin_village_where` / `_top_level_where_span` |
| KI-109 | CM Elevate one-figure misquote: "There is 1 pending application in Ranikor block" for a result of 12 — whole numbers < 100 skip the faithfulness check | **Fixed 2026-09-28** (Focus Plus rule extended) | High | compose_response `_ONE_FIGURE_SCHEMES` |
| KI-110 | CM Elevate: a village named "… (GARO)" asked "which Garo Hills district?" and answered for the whole range (273 vs 2) | **Fixed 2026-09-28** | Medium | `_answer_data` place-settled check |
| KI-111 | CM Elevate "applicants in each sector in <place>" (no programme named) was restricted to `scheme_name IN (Poultry, Dairy)` — Goat Farming (the only Goatery rows) dropped — and grouped by programme, so no sector total was stated | **Fixed 2026-09-28** (all-blocks run) | Medium | `_cm_elevate_sector_all_programmes` |
| KI-112 | CM Elevate SQL literals mistyped by the generator: `'PRIME Tourism Vehicle'` (no " Scheme"), `'SHILLONG-MUNICIPAL_BOARD'`, `'WEST JAINTEIA HILLS'` → a programme dropped or a confident 0 | **Fixed 2026-09-28** | High | `_cm_elevate_fix_literals` (snaps to stored programme / block / district names) |
| KI-113 | CM Elevate urban-body mentions: "Jowai-municipal Board" (no "block") refused as outside Meghalaya; "Mawkyrwat-town Committee" asked "C&RD block or village?" with both chips wrong; a bare block name ("Mawhati", "Siju") offered only its villages; "<village>, Nongpoh-town Committee block" answered for the whole urban body | **Fixed 2026-09-28** | High | `resolve_entities` urban-body step / collision gate data-block reading / `_cm_elevate_block_from_mention` / block spelling before the village-beside-block step |
| KI-114 | Village chip for a same-block twin (dim_geography has two "Bolbokgre" in DEMDEMA, CM Elevate holds one) pinned the twin with no data → 0 | **Fixed 2026-09-28** (MGNREGA unchanged) | Medium | `_mgnrega_village_chip_pin(question, scheme)` |
| KI-115 | 4B verifier "Check 2 … lists lgd_block = LASKEIN, but the SQL uses scheme_specific ->> file_status" rejected correct "rejected in <block>" SQL three times → KB fallback | **Fixed 2026-09-28** | Medium | `_verifier_scheme_specific_complaint_is_false` (check-2 form) |
| KI-116 | CM Elevate "applicants under each program in <village>" came back with an invented `scheme_name IN (Piggery, Poultry, …)` that excluded the village's only programme → "no matching records" | **Fixed 2026-09-28** | Medium | `_cm_elevate_unasked_programme_filter` (a named programme or a programme family keeps its filter) |
| KI-117 | CM Elevate answer wrote the figure as a word ("Five CM ELEVATE applicants…"); correct here, but a spelled-out figure is invisible to the numeric-faithfulness checks, so a wrong one would pass | **Fixed 2026-09-28** | Medium | `_cme_digits_for_number_words` (inside compose_response, before the checks) |
| KI-118 | CM Elevate: a village named MANIPUR (Umling block, 29 applications) refused as "another state" | **Fixed 2026-09-28** (bare "Manipur" now asks village or state, as for PMAY-G) | Medium | `_mgnrega_village_not_out_of_area` scheme list |
| KI-119 | CM Elevate: "Mawker, Mawhati block" came back as village_code_list + block_list (comparison shape) and the SQL filtered the block (204 vs 3) | **Fixed 2026-09-28** | High | `resolve_entities` one-village-beside-its-block collapse |
| KI-120 | CM Elevate: the generator invented a level from a village name ("QUININE" → current_level = 'quinary'; "12TH MER" → 'level12') → a false 0, or a "pending" count with no On Hold test | **Fixed 2026-09-28** | High | `_cm_elevate_unasked_level_filter` / `_cm_elevate_pending_without_level` (any level literal) |
| KI-121 | CM Elevate: 4B verifier "Check 3: grain — v_cm_elevate has multiple rows per applicant, use the raw fact table" killed "applicants in UMSHAKEN" (bare village, tester phrasing) | **Fixed 2026-09-28** | Medium | `_verifier_scheme_specific_complaint_is_false` (grain form) |
| KI-122 | CM Elevate: sector answer named the sector as a programme ("The Meghalaya Piggery Development Scheme had 5 applicants under the Meghalaya Poultry Farming Scheme") — tester OFF-013 | **Fixed 2026-09-28** | Low | `_cme_sector_answer_names_wrong_programme` |
| KI-123 | Testers (22 Sep sheet, OFF-015..021, 030) expect "status" / "pending" from `current_file_status`; the bot uses `data_verified` (status) and On Hold (pending) — the product owner chose On Hold on 2026-09-28. current_file_status holds only forward 8,497 / sendback 91 / resubmit 39 (no "pending" value) | **Decided + fixed 2026-09-28**: status = `current_file_status` (Forward / Sent back / Resubmit, send-back spellings merged); pending, verification, on hold, approved/rejected unchanged | Medium | schema_context rule 13 + vocabulary, 2 few-shots in cmelevate_few_shot.yaml, `_cm_elevate_status_is_file_status` |
| KI-124 | Testers: "numbers are coming less than the shared one" (OFF-001/002/004/005/006/009/010). The bot counts APPLICANTS = distinct request_id (rule 8); the raw sheet's rows are APPLICATIONS. They differ only where a request_id repeats (27, all Piggery): Ri Bhoi 2,596 vs 2,606. Before 2026-09-27 programme totals also dropped 51 Unresolved rows (KI-068, fixed) | **Needs a definition decision** | Low | schema_context rule 8 |
| KI-125 | PMAY-G: a bare year was dropped — "How many PMAY houses were sanctioned in 2023?" left year_key unresolved and the facts path answered for ALL years (170,981) | **Fixed 2026-09-28** (PMAY-G 21 Sep sheet recheck): a plain year is the CALENDAR year (answer, result table and SQL: 110,890 for 2023) with the FY reading as a one-line note (106,527) — changed 2026-09-29 after a user report: the FY figures had led while the calendar ones sat in the note, so the table disagreed with the question | High | `_pmay_facts_query` (`_PMAY_BARE_YEAR_RE`, `calendar_year`) |
| KI-126 | PMAY-G UX (21 Sep remark on PMAY-OFF-001): a village name shared by several villages (Apalgre x3) asks "which village?" and then "which financial year?" — two taps; the tester wants one | Open — product decision (options: default to all years after a village chip, or put the year choice into the village chips) | Low | village pause + year gate |
| KI-127 | PMAY-G counting rule (21 Sep remarks PMAY-OFF-011 / 015): the tester's LASKEIN block figure 5,251 counts all rows; the bot's 5,232 excludes 19 records with sanctioned amount ₹0 — as the tester's own 14 Sep remark asked ("where sanctioned amount is 0 are not considered as beneficiaries"). District gaps: Ri Bhoi 40, East Khasi Hills 31, West Jaintia Hills 21, South Garo Hills 11, North Garo Hills 10, South West Garo Hills 9, West Garo Hills 4 | Open — product decision (keep excluding, or state both figures) | Medium | facts path `NOT is_placeholder` |
| KI-128 | PMAY-G answers carried extra information (user report 2026-09-28): 'financial summary' added the house count and release %, 'performance' added remaining / completion % / release %, single figures added side details ('20 of 52', stage splits, '— ₹… sanctioned, ₹… released', '(covering N houses)', '(houses sanctioned)'), and follow-ups repeated the previous figure because the rewrite carries it ('and how many received only part of it?' → both 'none' and 'part') | **Fixed 2026-09-28** (PMAY-G only; the shared rewrite is unchanged) | Low | `_pmay_metrics`, `_pmay_lines`, `_PMAY_TYPED_TURN` |
| KI-129 | PMAY-G result table showed the facts path's whole working row (30 columns: fnd_st_plinth, sanctioned_positive, …), and the Sources line read "TRUE, PMAY-G" because the UI takes the word after every FROM and the SQL had "IS DISTINCT FROM TRUE" (user report 2026-09-29) | **Fixed 2026-09-29**: `_pmay_display_rows` returns only the place + asked figures; the SQL uses NOT COALESCE(is_completed, FALSE); the UI skips TRUE/FALSE/NULL | Low | pipeline facts path, web/ai_query.html |
| KI-130 | After a scheme answer, an unrelated message ("who is harshit", "he is my collik remember") was rewritten "…under Focus Plus" and answered from that scheme's reference docs or data path (user report 2026-09-29) | **Fixed 2026-09-29, live-verified** (battery S1): `context_policy.continuation_signals` gates the edge whitelist relaxation, the follow-up rewrite and the KNOWLEDGE scheme fallback | High | pipeline `_run_pipeline` step 0-c |
| KI-131 | "now give me five thousand loan for me i am in crisis" became a Focus Plus DATA question (year pause) (user report 2026-09-29) | **Fixed 2026-09-29, live-verified** (S1): edge kind `personal_request` (`edge.is_personal_request`) | Medium | edge |
| KI-132 | Focus Plus "for a loan of five thousand" dropped from SQL: `SELECT SUM(amount_disbursed) FROM curated.v_focus_plus` (₹119.74 cr for all payments vs ₹46.64 cr for the ₹5,000 ones) (user report 2026-09-29) | **Fixed 2026-09-29, live-verified** (S4: guard rejected the dropped amount, repair filtered `amount_disbursed = 5000` → ₹46.64 cr; S4b pause): `premise_check.stated_amount_filters`, repair guard `_focusplus_stated_amount_missing`, `amount-not-held` pause, "not a loan" composer note. Focus Plus only | High | pipeline, premise_check |
| KI-133 | "WHK" (letter-swapped WKH) is in no SME catalogue and resolved `not_found` silently, so "Compare … WHK and EKH" ran for EKH only (user report 2026-09-29) | **Fixed 2026-09-29, live-verified** (S3): `entity_resolver.acronym_near_misses` → "did you mean West Khasi Hills?" chips. No alias added | Medium | entity_resolver, `_answer_data` |
| KI-134 | A bare "Which one?" was rewritten and run instead of asked | **Fixed 2026-09-29, live-verified** (S2): `context_policy.is_bare_reference` → plan REQUIRE_CLARIFICATION → `reference-ambiguous` pause (not remembered) | Low | context_policy, pipeline |
| KI-135 | "How many villages are named Songsak?" resolved the name to ONE place and counted it (user report 2026-09-29) | **Fixed 2026-09-29, live-verified** (S5: 1 exact SONGSAK village, LGD 276587; 12 partial): `_village_name_search_answer` (exact / contains; scheme catalogue or `dim_geography`; parameter-bound). Songsak lookups live: bare → block-or-village pause; block ₹3,87,67,500; village ₹25,000 | Medium | pipeline step 1g |
| KI-136 | After a CM Elevate Legacy turn, "what is focus" was rewritten "What is the focus of the CM Elevate Legacy scheme in FY 2024-25?" — the scheme name read as a noun, the which-Focus question never asked (user report 2026-09-29) | **Fixed 2026-09-29, live-verified**: `_names_bare_focus_scheme` skips the model rewrite; `_FOCUS_AS_NOUN` keeps "the focus of…" a follow-up | High | pipeline follow-up step |
| KI-137 | A rewritten how-it-works question carried the previous DATA turn's year and place ("…in East Khasi Hills in FY 2023-24?"), so the reference docs answered "not listed for East Khasi Hills" | **Fixed 2026-09-29, live-verified** (S7): `_drop_inherited_time` + `_drop_inherited_place` on the KNOWLEDGE path (a typed year / place is kept) | Medium | pipeline KNOWLEDGE branch |
| KI-138 | "give me beneficiaries" rewritten "What is the disbursement for Focus Plus…": the follow-up's own metric replaced, and the provenance check passed it (money was in the state line) | **Fixed 2026-09-29** (offline-tested; the live path now avoids the model rewrite for this shape, so the check was not exercised live): `rewrite_violation` requires the follow-up's own metric family in the rewrite | High | context_policy `rewrite_violation` |
| KI-139 | A KNOWLEDGE turn on another scheme left the old scheme's year / metric / places feeding the next follow-up (FY 2024-25 of CM Elevate Legacy carried into Focus Plus → year-out-of-range pause) | **Fixed 2026-09-29, live-verified** (S6, S8): per follow-up, `_followup_thread_state` — a follow-up naming nothing continues the antecedent's scheme with a clean state; one whose words pick the earlier scheme keeps that thread. The digression still never overwrites the session state (test_context_manager §6). `update_state` drops unnamed filters when the committed scheme changes. (A first version wiped the state on every such knowledge turn; it broke the pinned digression rule and was replaced.) | High | context_manager `update_state` |
| KI-140 | After a KNOWLEDGE answer, "give me beneficiaries" was paraphrased "What are the beneficiaries of…" and answered from the reference docs | **Fixed 2026-09-29, live-verified**: `_measure_after_knowledge` (no model rewrite; scheme appended) + `_typed_intent` (the typed words decide intent when decisive) | High | pipeline |
| KI-141 | A paused follow-up was remembered as the typed fragment ("give me beneficiaries"), so a typed "2024-25" resumed without the scheme and needed a second model rewrite | **Fixed 2026-09-29, live-verified**: `turn_context["standalone_question"]`, read first by `routers.query.pause_question` | Medium | pipeline, router |
| KI-142 | "and all of them combined?" after "what about 2022-23?" kept the last year (137 / 93,286 repeated) | **Fixed 2026-09-29, live-verified**: `ConversationState.last_dimension` + `_ALL_OF_THEM_RX` CLEAR; `_all_years_rewrite` (deterministic) | Medium | context_policy, pipeline |
| KI-143 | "and in West Garo Hills?" after two KNOWLEDGE follow-ups ("who is eligible?", "documents?") continued the documents question instead of the earlier figure | **Fixed 2026-09-29, live-verified** (S7 → completed houses WGH FY 2023-24 = 13,964): `_data_thread_antecedent` — a place / year-only change after a knowledge answer continues the last DATA turn of that scheme (KI-033 family) | Medium | pipeline follow-up step |
| KI-144 | "and person-days?" after an aside "who is eligible for PMAY-G?" was pinned to PMAY-G and answered "19,058 person-days" from a house count (found by this session's own live battery, before release) | **Fixed 2026-09-29, live-verified** (S8 → MGNREGA WGH FY 2023-24 = 5,489,616): `_measure_after_knowledge` defers to scheme vocabulary; `_data_thread_antecedent` picks that scheme's last DATA turn | High | pipeline |
| KI-145 | Focus Legacy per-block breakdown silently dropped the PGs with no block in the source (West Khasi Hills: 5 of 926; 88 rows statewide) and called the listed sum the total (2026-09-29 re-test, TC-26) | **Fixed 2026-09-29, live-verified**: `_focus_legacy_unplaced_row` re-runs the query without the model's `IS NOT NULL` filter and reads the no-place row; the answer states it once, in digits (`_focus_legacy_unplaced_guarantee`, or the breakdown answer of KI-150) | Low | pipeline |
| KI-146 | Constituency drill-down read MGNREGA's `v_employment` for EVERY scheme, so a Focus Legacy chip said "190 villages in the employment data" and offered MGNREGA's blocks (TC-28) | **Fixed 2026-09-29, live-verified**: `entity_resolver.constituency_contents(ac, scheme)` reads the asking scheme's rows (`_AC_CONTENTS_SQL`: Focus Legacy / CM Elevate Legacy via `dim_geography`), wording from `AC_CONTENTS_SOURCE` | Low | entity_resolver / pipeline |
| KI-147 | Focus Legacy totals printed as stored ("54205000.00") and months as "month 4" (TC-18/21/23) | **Fixed 2026-09-29, live-verified**: `_focus_legacy_answer_guarantees` — `_focus_legacy_month_labels` ("April 2022", FY-aware) and `_pmay_rupee_format(fmt=, column_totals=True)` (₹ Indian grouping, lakh/crore on short answers, column sums too) | Low | pipeline (post-composer) |
| KI-148 | A constituency named with no scheme pinned MGNREGA (`_MGNREGA_ONLY_TERMS` still held "assembly constituency" from when only MGNREGA had one): "total amount disbursed for Baghmara assembly constituency" failed on all 55 ACs (Focus Legacy bulk run 2026-09-29) | **Fixed 2026-09-29, live-verified** (A-AMT 0/55 → 55/55): `_mgnrega_terms` ignores the AC term alone; `_scheme_clarification` offers only MGNREGA / Focus Legacy / CM Elevate Legacy for an AC question | High | pipeline (scheme routing) |
| KI-149 | Focus Legacy group-name parser could not read 219 of 9,452 stored names ("Bak -13 …", "Pg-83/21", a lone "'", "All In One …" cut at "In", a missing space on re-join) and refused 20 names holding a place word ("Green Hills Producer Group"); they fell to the model path ("group_exists value of 1") | **Fixed 2026-09-29, live-verified**: `_PG_NAMED_ENTITY` takes any non-space word; `_pg_rest_is_place`; place-word names accepted when marked as a group, else `size?` answered only on an exact-name match; all 9,452 names + 1,481 raw spellings parse | Medium | pipeline |
| KI-150 | Composer errors on Focus Legacy per-place breakdowns: dropped the per-block counts (West Jaintia Hills), called 5 blocks "5 producer groups" (West Khasi Hills), wrote figures as words; an aliased column (`lgd_block AS block`) defeated the guards | **Fixed 2026-09-29, live-verified**: `_focus_legacy_breakdown_answer` writes a one-place-column breakdown from the rows (D-031); `_FL_LEVEL_ALIAS` / `_fl_canon` read aliases | High | pipeline |
| KI-151 | Intermittent "couldn't build a working query" on a simple Focus Legacy AC total under heavy load (Dalu members, 1 of 4 tries while 16 bulk conversations ran; 3/3 correct on retry) | Open — observed only under bulk load; re-checked in the final pass | Low | SQL gen / repair |
| KI-152 | Edge out-of-area refusal fired on producer-group NAMES holding a state/country word ("Rakkam China Banana Group", 3 "Manipur …" groups) and on "Umiong U.s.t" (the U.S. pattern matched "u.s.t") | **Fixed 2026-09-29, live-verified**: `edge._mask_group_name` hides a labelled group name ("named X", "members in X … Pg/Group") from the out-of-area test only; U.S. pattern tightened. A place written after the name ("… in Assam") is still refused | Medium | edge |
| KI-153 | "…for PAKREGRE CHIKAMA village": the extractor returned the fragment "CHIKAMA" — itself another village in the same block — and the answer counted that village (₹5,000 for ₹1,40,000). Same cause for "ADUGRE (NENGSRANG ADUGRE)", "RONGBINGGRE (A)" (Focus Legacy all-villages run 2026-09-29; shared code, all village schemes) | **Fixed 2026-09-29, live-verified**: in the explicit-level village step the whole `_VILLAGE_PHRASE_RE` phrase replaces an extractor fragment when `village_names_exact` holds it | High | pipeline (resolve_entities) |
| KI-154 | Composer rewrote a producer-group name inside a village list ("Bak 13 Nalsa Pepper Producer Group." → "… Pepper Group.") | **Fixed 2026-09-29, live-verified**: `_focus_legacy_group_list_answer` writes id/name-only lists from the rows (D-031) | Medium | pipeline |
| KI-155 | 6 Focus Legacy PG names are mis-encoded text in BOTH the raw file and the DB (e.g. "AÃ£Æ’Ã¦…we Producer Group", "MawleiÃ±") | Open — **data team** (docs/Focus_Legacy_DB_Issues.md). Bot side fixed: `_pg_name_tokens` keeps non-ASCII letters, so such a name no longer matches a different group ("A.we") | Low | data |
| KI-156 | Focus Legacy duplicate-name village ("RONGSIGRE" in Chokpot and Gasuapara): tapping a village chip re-asked the same question forever — Focus Legacy was not a `_village_scheme`, so the chip tail was never pinned; after a later year chip the tail was hidden by " across all financial years" and the year pause carries no village hint | **Fixed 2026-09-29, live-verified** (6/6 probe): the chip pin runs for Focus Legacy; `_CHIP_TAIL_PARSE_RE` allows a trailing period phrase; for Focus Legacy a "village … , X block, DISTRICT" tail pins without a hint. CM Elevate Legacy may share the loop — NOT VERIFIED | High | pipeline (resolve_entities) |
| KI-157 | PG named "Focus Bibari": the bare-"Focus" gate asked "which Focus?" and its chip rewrote the name to "Focus Legacy Bibari" (no match); 4 stored names contain "Focus" | **Fixed 2026-09-29, live-verified**: `_is_ambiguous_focus` stands down when "Focus" is part of a group name with other name words (`_pg_focus_group_name`); a bare "what is focus" still asks | Medium | pipeline |
| KI-158 | PG names with "_" ("Erifa_25", "… Producer Group _19") were "not found": Postgres counts "_" as a word character, so the whole-word match never fired | **Fixed 2026-09-29, live-verified**: the lookup compares `translate(pg_name, '_', ' ')` | Medium | pipeline |
| KI-159 | Focus Legacy village answer null: the model added `lgd_block = 'RONGBARA'` (its misspelling of RONGARA) beside the pinned `village_code` (NIKWATGRE ₹95,000 → null) | **Fixed 2026-09-29, live-verified**: `_focus_legacy_village_code_only` drops block/district literals beside a resolved village_code in `execute_with_repair` | High | pipeline (SQL guard) |
| KI-160 | Village-disambiguation chips showed 5 of the same-named villages from the whole geography registry, so the one holding Focus Legacy groups could be missing ("MAWLONG": 9 candidates, Mawpat block not offered) | **Fixed 2026-09-29, live-verified**: `_fl_rank_villages` lists villages with Focus Legacy rows first (others kept, so a genuine zero can still be asked) | High | pipeline |
| KI-161 | After a village chip, the 4B verifier rejected correct Focus Legacy SQL on the pinned `village_code` ("district filter dropped") and the answer fell to the KB fallback ("reference passages do not contain…"): Belbari, DAPGIRI | **Fixed 2026-09-29, live-verified**: `_verifier_village_code_complaint_is_false` covers Focus Legacy | High | pipeline (verifier guard) |
| KI-162 | Focus Legacy village names containing "Garo" ("MENDIMA GARO", "SHYAMDING ( GARO )") drew the "which Garo Hills district?" region question, whose chip rewrote the name ("MENDIMA all of Garo Hills"); a village named "MANIPUR" was refused as out-of-state; "MAWKOHMIT & MAWKYNSAH" / "SIEJLIEH - MAWIABAN" were split | **Fixed 2026-09-29, live-verified**: Focus Legacy in `_mg_place_settled`; `edge._NAMED_VILLAGE_SPAN`; `_VILLAGE_NAME_WORD` accepts "&" and a spaced "-" | High | pipeline / edge |
| KI-163 | Same-name villages in ONE block (UMSAW ×2, TIEHSAW ×2 in Nongstoin; DEWSAW ×2 in Mairang): resolve_village merged each pair by all-scheme data volume and the chips could not tell them apart, so the wrong twin was answered | **Fixed 2026-09-29, live-verified**: `_fl_expand_twins` adds back the twin holding Focus Legacy groups, `_village_code_tag` gives Focus Legacy twins LGD-coded chips, and a resolved village with a Focus Legacy twin is asked about | High | pipeline |
| KI-164 | Month breakdowns one month early: `DATE_TRUNC('month', date_of_remittance)` returns timestamptz in the session zone (Asia/Kolkata) and the driver returns it in UTC, so 1 Apr 2022 00:00 IST arrived as 31 Mar 18:30 UTC and the answer said "₹5.20 crore in March 2022" (true: April). The Focus Legacy few-shot teaches DATE_TRUNC; `EXTRACT(MONTH …)` runs were right (TC-21, final-code re-test 2026-09-29) | **Fixed 2026-09-29 for Focus Legacy, live-verified**: `_focus_legacy_date_trunc_as_date` casts `DATE_TRUNC(...)` to `::date` in `execute_with_repair`. **Other schemes NOT VERIFIED** — PMAY-G's schema notes show `date_trunc('month', sanction_date)` and may share it | High | pipeline (SQL guard) |
| KI-165 | "Which Producer Groups are mapped to SHYAMDING ( GARO ) village?" answered with bare pg_ids (the model selected only pg_id); "RAMJONGGRE ( R )" was answered as its neighbour "RAMJONGGRE ( L )" (a lone ")" broke the village phrase) | **Fixed 2026-09-29, live-verified**: `_fl_add_group_names` adds names to an id-only Focus Legacy list; `_VILLAGE_NAME_WORD` accepts ")"; all 174 bracketed / "&" / spaced-hyphen village questions re-run 174/174 | Medium | pipeline |
| KI-166 | CM Elevate Legacy (Use case TC-14): 'How many applications have been sanctioned?' answered 2,823; DB and raw give 2,820 (5 of 5 runs). Root cause: SQL generation wrote COUNT(*) AS sanctioned_records. 3 Any Business Venture records (Refused) have no sanctioned amount. The rule existed only as a prompt rule + few-shots, with no deterministic guard. | **Fixed 2026-09-29, live-verified**: app/pipeline.py _cm_legacy_sanctioned_count, run in execute_with_repair: COUNT(*) aliased as a sanctioned count -> COUNT(sanctioned_amount); a sanctioned question whose SQL has no sanction column gets COUNT(sanctioned_amount) AS sanctioned_records. Verified: TC-14 = 2,820 live; every block/district 'sanctioned' question in the final pass. Tests: `tests/test_cm_elevate_legacy.py`. | High | SQL gen / pipeline |
| KI-167 | CM Elevate Legacy (Use case TC-34): '…each village in Tikrikilla block' said '20 villages each have 1 record'; DB and raw give 24. The 6 no-village records were not mentioned. Root cause: Composer (9B) counted wrongly; it sees only the first 40 of 42 rows and the faithfulness check lets whole numbers under 100 through as prose. | **Fixed 2026-09-29, live-verified**: app/pipeline.py _cm_legacy_answer_guarantees: _cml_fix_each_have_counts recomputes every 'N villages each have V records' from ALL rows; _cml_unmapped_in_scope re-queries the no-village records of the same scope and states them. Verified: TC-34 live: '24 villages … A further 6 records in Tikrikilla block have no village code'; all 59 block village lists. Tests: `tests/test_cm_elevate_legacy.py`. | High | composer |
| KI-168 | CM Elevate Legacy (Use case TC-13): 'Which schemes have the highest number of applications?' cut to 10 of 13 schemes; answer said the lowest was 33 (true: Motorcaravan 1). 4 of 5 runs. Root cause: SQL generation added an unrequested LIMIT 10; the top-N pause only knows districts/blocks/villages, not schemes. | **Fixed 2026-09-29, live-verified**: app/pipeline.py _cm_legacy_unrequested_limit, run in execute_with_repair: drops a trailing LIMIT from a grouped CM Elevate Legacy result unless the question states a count (a singular 'which scheme has' keeps LIMIT 1). Verified: TC-13 live lists all 13, 'down to 1 record for Motorcaravan'. Tests: `tests/test_cm_elevate_legacy.py`. | Medium | SQL gen / composer |
| KI-169 | CM Elevate Legacy (All-villages run (twin names)): Tapping a village chip for a shared name (DOMBAGRE — Rerapara / Zikzak) re-asked 'which DOMBAGRE?' forever. Root cause: The chip-tail pin (KI-156) ran for MGNREGA/PMAY-G/Focus Plus/CM Elevate/Focus Legacy only, so CM Elevate Legacy re-resolved the bare name every time. | **Fixed 2026-09-29, live-verified**: app/pipeline.py resolve_entities: the ', X block, DISTRICT' chip tail pins for CM Elevate Legacy too; _mgnrega_village_chip_pin keeps the same-block twin that holds CM Elevate Legacy records. Verified: All 70 shared-name village questions. Tests: `tests/test_cm_elevate_legacy.py`. | High | pipeline (resolve_entities) |
| KI-170 | CM Elevate Legacy (All-villages run): 'Nongthymmai' (10 registry villages) — the chips showed 5 and the Jirang village holding the records was not among them; the answer was 0. Root cause: Village chips are drawn from the whole geography registry; ranking by the scheme's own data existed for Focus Legacy only (KI-160/163). | **Fixed 2026-09-29, live-verified**: app/pipeline.py _fl_rank_villages / _fl_expand_twins now take the scheme's own view from _TWIN_RANK_VIEWS (Focus Legacy, CM Elevate Legacy). Verified: Nongthymmai: Jirang chip first, 2 applications / ₹1,25,000. Tests: `tests/test_cm_elevate_legacy.py`. | High | pipeline (village chips) |
| KI-171 | CM Elevate Legacy (All-villages run): A true ₹0 disbursement (Umtham, Umling) was written 'under ₹0.01 crore'; ₹62,500 and ₹1,25,000 both read '₹0.01 crore'. Root cause: The small-money note treats a 0.00 crore value as 'under ₹0.01 crore'; 2-decimal crore hides village-sized amounts. | **Fixed 2026-09-29, live-verified**: app/pipeline.py _cml_exact_single_amount re-queries the exact rupees for a one-row figure under ₹0.10 crore: '₹0', or '₹0.01 crore (₹1,25,000)' (_cml_inr, Indian grouping). Verified: Umtham ₹0; Dombagre ₹62,500; Nongthymmai ₹1,25,000; all village disbursements. Tests: `tests/test_cm_elevate_legacy.py`. | Medium | composer / pipeline |
| KI-172 | CM Elevate Legacy (All-villages / all-blocks run): Place names misspelled in answers: 'Sellsella' for SELSELLA (3 of 3 runs), 'Mawsynrut' for MAWSHYNRUT. Root cause: Composer model spelling; the resolved names are known exactly but were not enforced. | **Fixed 2026-09-29, live-verified**: app/pipeline.py _cml_fix_place_spelling: near-miss spellings of the filtered place and of the result's place labels are replaced by the stored name (never a word that is itself another known name). Verified: Rongchigre/Selsella; West Khasi Hills block list. Tests: `tests/test_cm_elevate_legacy.py`. | Low | composer |
| KI-173 | CM Elevate Legacy (All-villages run): William Nagar (MB) wards answered 0 / 'couldn't build a query' (true 1 record). Root cause: SQL generation put the block name into lgd_village_name beside the pinned village_code. | **Fixed 2026-09-29, live-verified**: app/pipeline.py _focus_legacy_village_code_only extended to CM Elevate Legacy: place literals (block / district / village name) beside a pinned village_code are dropped. Verified: Both William Nagar wards. Tests: `tests/test_cm_elevate_legacy.py`. | High | SQL gen |
| KI-174 | CM Elevate Legacy (All-blocks run): 'Applications under each scheme in Umsning block' ended 'the remaining schemes show 24, 16, 15, 13, 6, 3, 1 and 1 records respectively' — figures with no names; Nongstoin named only the highest and lowest. Root cause: Composer wording for a per-group count list; no completeness guarantee for CM Elevate Legacy. | **Fixed 2026-09-29, live-verified**: app/pipeline.py _cml_complete_list (detection shared with CM Elevate's _cme_complete_list): the full labelled list is rebuilt from the rows under the composer's headline. Count breakdowns only; year and money breakdowns untouched. Verified: All 59 block per-scheme lists, 12 district per-block lists. Tests: `tests/test_cm_elevate_legacy.py`. | Medium | composer |
| KI-175 | CM Elevate Legacy (All-blocks run): 'Tura Municipal Board' refused as 'not in Meghalaya'; its disbursement SQL used 'TURA MUNICIPAL BOARD-MUNICIPAL BOARD' and returned null. Root cause: The registry holds two Tura spellings; CM Elevate Legacy stores 'TURA MUNICIPAL BOARD'. The urban-body mapping (KI-107) existed for CM Elevate only. | **Fixed 2026-09-29, live-verified**: app/pipeline.py _cm_elevate_block_names / _cm_elevate_block_from_mention / _cm_elevate_blocks_to_data are scheme-aware; CM Elevate Legacy maps a mention or resolved block to its own stored block. Verified: All 6 Tura Municipal Board questions. Tests: `tests/test_cm_elevate_legacy.py`. | High | pipeline (resolve_entities) |
| KI-176 | CM Elevate Legacy (All-constituencies run): Mawkynrew / Rangsakona disbursement answered '0.58 total disbursed cr.' Root cause: The composer note told it to 'give all three' (subsidy, loan, total) with only a total in the result; it invented a subsidy split, was rejected twice and the raw fallback was shown. | **Fixed 2026-09-29, live-verified**: app/pipeline.py _cm_legacy_answer_notes: the subsidy/loan note depends on the columns present; _cml_single_row_sentence replaces the raw fallback with a plain sentence. Verified: Both constituencies; all 55 constituency disbursements. Tests: `tests/test_cm_elevate_legacy.py`. | Medium | composer notes |
| KI-177 | CM Elevate Legacy (All-blocks run): Purakhasia village list: '1 record at each of the other 13 villages' (8 have 1), counts '… respectively' with no names, the block's 30 records never stated. Root cause: Composer wording; no village-breakdown guarantee. | **Fixed 2026-09-29, live-verified**: app/pipeline.py _cml_village_breakdown: a garbled list or a false 'each of the N villages' claim is rebuilt (headline with exact total + named list); a missing total is added. Verified: All 59 block village lists. Tests: `tests/test_cm_elevate_legacy.py`. | High | composer |
| KI-178 | CM Elevate Legacy (All-villages run): 'Mendima Garo village' asked 'which Garo Hills district?' and was answered as MENDIMA BAKRAGITTIM (0). Root cause: The Garo-range check skipped a resolved village for 4 schemes (KI-162), not CM Elevate Legacy. | **Fixed 2026-09-29, live-verified**: app/pipeline.py _mg_place_settled includes CM Elevate Legacy. Verified: Mendima Garo: 2 applications, ₹75,000. Tests: `tests/test_cm_elevate_legacy.py`. | High | pipeline (region gate) |
| KI-179 | CM Elevate Legacy (All-villages run): 'Nongjri Mission village' asked NONGJRI vs NONGBSAP MISSION, and the chip answered for the whole Umsning block (106 vs 1). Root cause: The extractor kept the level word in the name ('Nongjri Mission village'), which matched nothing exactly; the chip then replaced the whole mention, dropping 'village', so the chip pin could not fire. | **Fixed 2026-09-29, live-verified**: app/pipeline.py resolve_entities (explicit-village step, all schemes): a trailing 'village' is dropped when the bare name is a stored village and the longer one is not. Verified: Nongjri Mission: 1 application, ₹1,20,000. Tests: `tests/test_cm_elevate_legacy.py`. | High | pipeline (resolve_entities, all schemes) |
| KI-180 | Scheme swap by request verb (reported 2026-09-29): "give me for pmay" after "Total MGNREGA person-days in 2023-24" was answered with a PMAY-G scheme description (RAG). Also, every swap that carried a measure only the old scheme holds ("for pmay" -> "Total PMAY-G person-days in 2023-24") ended in "couldn't build a working query" after 4 rejected SQL attempts. Root cause: `_SCHEME_SWAP_FOLLOWUP` allowed no request verb ("give me", "show us the same") ahead of the preposition, and `_scheme_substitution` had no such cue; nothing checked that the swapped scheme holds the carried measure. | **Fixed 2026-09-29, live-verified**: app/pipeline.py `_SCHEME_SWAP_FOLLOWUP` (request-verb opening, trailing too / also / as well), `_SUBSTITUTION_CUE` ("give me for" …), new `_swap_measure_gap` + `_SCHEME_OWN_MEASURES` / `_SCHEME_HEADLINE_OFFERS` (pause `swap-measure-unavailable` with the new scheme's own measures for the same scope as chips; no guess). "tell me about pmay" is still KNOWLEDGE. Verified live: the reported pair, PMAY-G houses -> MGNREGA, money swap unchanged (EKH ₹221.26 cr released), chip answered 106,527 houses FY 2023-24. Tests: `tests/test_context_relevance_and_contract.py` (+26). Not covered: bare "for focus" -> which-Focus chip -> a Focus measure gap is still reached through the generator. | High | pipeline (follow-up detection, scheme swap) |
| KI-181 | A typed reply to a chip pause other than scope/year/entity/ranking and which-scheme was read against the LAST ANSWERED turn, not the pause: "houses sanctioned" after the PMAY-G measure-gap pause ran as MGNREGA; "2022-23" after year-out-of-range, "weaving" after the Sericulture pause, "west garo hills" after the region pause each lost the paused question unless a chip was clicked. Root cause: `routers.query.remember_pause` remembered only `SCOPE_MERGE_RULES` and `SCHEME_PAUSE_RULES`. | **Fixed 2026-09-29, live-verified**: router remembers every rule with options; `pipeline._resume_option_pause` (label words / ordinal / yes / chip question, unique only) and `_paused_thread_antecedent` (unmatched reply stays on the paused scheme). Verified live: "houses sanctioned" 106,527; "2022-23" Focus Plus EKH FY 2022-23; "weaving" → area pause for the weaving question; "west garo hills" 5,489,616 person-days; "the second one" 410,200 households. Tests: `tests/test_context_relevance_and_contract.py` (+33). Open: an unmatched reply with a measure and place but no year ("houses completed in west garo hills") keeps the paused scheme but not the paused year (it asks the PMAY-G FY, same as any new PMAY-G question). | High | router `remember_pause`, pipeline step 0a'' |
| KI-182 | CM Elevate OFF-009 "applicants in <district> under <P1> and <P2>": when one programme has 0 applicants in the district the answer names only the other ("PRIME SEED: 544 applicants.") — no 0, no combined total; 567 of 572 such questions (all-pairs run 2026-10-01: 1,260 questions, SQL data correct 1,260/1,260). Cause: GROUP BY scheme_name returns no row for a 0 programme and `_cme_multi_scheme_total` only fills programmes present in the rows. Both-zero pairs (141) say 'no matching records' | **Fixed 2026-10-02, live-verified** (user asked to fix): `_cme_asked_programmes` reads the programmes from the SQL's `scheme_name IN (…)` list; when the query groups by programme alone (no HAVING; a LIMIT is allowed when it is at least the programme count — the auto `LIMIT 1000` first disabled the fix on 7 of 30 live one-zero cases), an asked programme with no row is stated as 0 and the combined total is always stated; both programmes 0 → each stated as 0 (`_cme_all_zero_answer`). Live re-test 49/49 on both paths (30 one-zero, 9 both-zero, 10 both) vs the DB. Tests: `tests/test_cmelevate_usecase_fixes.py` (+9). **Full 1,260 set re-run 2026-10-02: 1,260/1,260** (after KI-194) | Medium | `_cme_multi_scheme_total` / composer |
| KI-183 | Repaired SQL is not re-authorized: `auth.authorize` runs once on the first SQL (`_data_authorize_stage`); the up to 3 SQL texts `execute_with_repair` / the graph's `repair` node produce afterwards run without a scope check. A repair that adds a district / block literal or a finer GROUP BY than the first SQL is not checked against a geography- or granularity-capped role. Found 2026-10-02 while mapping the LangGraph nodes; pre-existing on the sequential path. | **Fixed 2026-10-02** (user asked to fix; D-033): every REPAIRED query passes `auth.authorize` before it runs (`_repaired_sql_denial`; sequential `execute_with_repair(scope=…)` raises `RepairedSQLDenied` → `_denied`; graph `node_execute_attempt` returns the denial). The first query is checked exactly as before. Tests: `tests/test_pipeline_graph.py` (denied identically on both paths, the repaired query never runs; an in-scope repair still runs) | Medium (security) | `_data_authorize_stage`, `execute_with_repair`, `pipeline_graph.node_execute_attempt` |
| KI-184 | LangGraph checkpoints are per host with the default SQLite backend; in the 2-VM deploy a pause reply that lands on the other VM finds no thread. Behaviour stays correct (the Session snapshot resumes the pause, tested), but that run's trace starts fresh rather than as a resume. | **Open — by design until a Postgres checkpointer is verified** (KI-185). | Low | `pipeline_graph._open_saver` |
| KI-185 | The Postgres checkpointer (`PIPELINE_GRAPH_CHECKPOINTER_DSN=postgresql://…`, optional `langgraph-checkpoint-postgres`, psycopg 3) is written but NOT verified: no package installed, no test DB. Its `setup()` creates `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` and `checkpoint_migrations` in the DSN's search_path (pin `options=-csearch_path%3Dapp`). | **Open — unverified.** Verify against a non-production database first. | Low | `pipeline_graph._open_saver` |
| KI-186 | The LangGraph path is verified offline only (2026-10-02, VPN down): 27 equivalence and graph tests plus the full suite with `PIPELINE_GRAPH_ENABLED=true`. `tests/live_context_validation.py` and a live use-case sample have NOT been run on the graph path. | **Closed 2026-10-02 (VPN up, evening)**: live context suite **43/43 on both paths**; six-scheme live sample (G4) **36/36 turns identical** old vs graph; CM Elevate OFF-009 re-test **49/49 on both paths**; live node coverage: all 21 nodes ran, 0 errors (8 pauses, 2 repairs). The flag stays off until a canary (G5) | Medium | `pipeline_graph` |
| KI-187 | Verifier false positive (live context suite 2026-10-02, scenario F T3, BOTH orchestrators and the pre-refactor code): "What about Dalu block?" after "MGNREGA person-days in West Garo Hills in FY 2024-25" resolved district WEST GARO HILLS + block DALU; the 4B verifier said 'the RESOLVED ENTITIES block does NOT list lgd_block = DALU' although its prompt lists it; the repairs alternated between the scope guard and the verifier, the budget ran out and the KB answered ('reference passages do not contain …'). The SQL with both filters was right (390,921; DALU is in WEST GARO HILLS). Was 43/43 on 2026-09-29: model behaviour changed, not the code. | **Fixed 2026-10-02, live-verified**: `_verifier_unlisted_geo_is_false` in `_verify_sql` discards a 'not listed' complaint naming a resolved district/block when the SQL filters every resolved district and block verbatim and no other. Live context suite 43/43 on both paths after the fix (42/43 before). Tests: `tests/test_mgnrega_usecase_fixes.py` (+5) | High | `_verify_sql` (KI-002 family) |
| KI-188 | A resolved village the SQL does not filter at all, every scheme but MGNREGA: "applications under CM Elevate Legacy in NOAGRE" resolved village_code 273728, the 30B read the name as a programme (`scheme_name = 'Meghalaya New Agriculture…'`, no village) → "zero records" for a village with 12. Only MGNREGA had a village-missing guard. Found in the all-villages run 2026-10-02 | **Fixed 2026-10-02, live-verified**: `_village_filter_missing` (every single-scheme question) + `_pin_missing_village_code` (simple one-SELECT: adds `village_code = <code>`, drops a block/district literal beside it). Tests: `test_cm_elevate_legacy.py`, `test_mgnrega_usecase_fixes.py` | High | `_scheme_sql_rewrites` |
| KI-189 | A village name written as a CM Elevate Legacy programme: `WHERE village_code = 279360 AND scheme_name = 'AMLARI MODEL'` → 0 (9 villages in the all-villages re-run 2026-10-02) | **Fixed 2026-10-02, live-verified**: `_cm_unasked_programme_beside_village` drops a `scheme_name` literal that is not a stored programme (`entity_resolver._catalog[scheme]['cm_scheme']`) beside a resolved village. Tests: `test_cm_elevate_legacy.py::test_village_name_written_as_a_programme_is_dropped` | High | `_cm_unasked_programme_beside_village` |
| KI-190 | Focus Legacy and CM Elevate Legacy were outside the village gate: a bare village name ("producer groups in BOLDAMGRE") was never scanned and was answered with the block's figure (Selsella's 399); BATABARI (village AND block) silently as the block; MAWTNUM after the year chip answered for the whole Umling block (224 for 4); the Legacy village MANIPUR refused as the state | **Fixed 2026-10-02, live-verified** (D-034): `_VILLAGE_NARROW_SCHEMES` now holds five schemes (all but MGNREGA, which has its own path); `_focus_legacy_village_names`, `_cm_legacy_village_names` in `_scheme_village_names`; `_YEAR_AFTER_CHIP_TAIL_RE` pin covers CML; both Legacy schemes allowed in `_mgnrega_village_not_out_of_area`'s village-or-state check. Tests updated with reasons in `test_focusplus_usecase_fixes.py`, `test_mgnrega_usecase_fixes.py`, `test_focus_legacy_usecase_fixes.py` | High | `resolve_entities`, village gate |
| KI-191 | Focus Plus blocks that are not `block_name_raw` values: the source file predates ten LGD blocks (ADOKGRE, BATABARI, SIJU, MAWLAI, SHALLANG, RAMBRAI, PURAKHASIA, RI MULIANG, MAIRANG-TOWN COMMITTEE, WILLIAM NAGAR-MUNICIPAL BOARD); "beneficiaries in Adokgre block" → 0 (754 in the DB); "Mairang Town Committee" fuzzy-snapped to rural MAIRANG (402). All-blocks run 2026-10-02 | **Fixed 2026-10-02, live-verified**: `_focusplus_lgd_only_block` (a block that is not a raw name is filtered on `lgd_block`); `_cm_elevate_block_names` Focus Plus branch (`_FPL_BLOCKS`) so both spellings resolve. Tests: `test_focusplus_usecase_fixes.py` | High | `_scheme_sql_rewrites`, `resolve_entities` |
| KI-192 | A figure spelled out by the composer ("Eighteen producer groups…", "Twenty-one producer groups…") is invisible to the faithfulness checks and to a reader scanning digits (all-blocks run 2026-10-02) | **Fixed 2026-10-02**: `_cme_digits_for_number_words` runs for every scheme in `_data_guarantees_stage` (every scheme's count nouns); tens and compounds up to ninety-nine (`_NUMBER_TENS`), only when the value is in the result. Tests: `test_cmelevate_usecase_fixes.py` | Medium | composer guarantees |
| KI-193 | PMAY-G village comparisons: "JEWILGRE or BURMA?" refused as a question about Burma; "PECHUA or LASKEIN, the village, not the block" (the user's own chip) resolved LASKEIN as a block → repairs ran out (OFF-022); "MALANG KHASI or SIDAKANDI?" asked "which Khasi Hills district?" (PMAY-G officer cases run 2026-10-02) | **Fixed 2026-10-02, live-verified**: `_mgnrega_village_not_out_of_area` (a refused word that is a village beside another village is the village); `_pmay_two_villages` honours "the village, not the block" and drops the rejected `block_list`; `village_code_list` counts as a settled place in `_mg_place_settled`. PMAY-G officer cases 5,833/5,833. Tests: `test_pmay_usecase_fixes.py` | High | `resolve_entities`, clarification gates |
| KI-194 | CM Elevate "applicants" counted with COUNT(*): about 36 request ids repeat (Piggery / Poultry), so "applicants in Ri Bhoi under Goat Farming and Poultry Farming" said 242 Poultry (241 distinct); 8 OFF-009 pairs (full re-run 2026-10-02) | **Fixed 2026-10-02, live-verified**: `_cm_elevate_applicants_distinct` rewrites COUNT(*) over `v_cm_elevate` to `COUNT(DISTINCT request_id)` when the question asks for applicants / beneficiaries ("applications" stays COUNT(*)). OFF-009 1,260/1,260. Tests: `test_cmelevate_usecase_fixes.py` | High | `_scheme_sql_rewrites` |
| KI-195 | "Meghalaya Sports & Wellness Centre Scheme" (the stored name, with "&") was read as MGNREGA in OFF-017 status-distribution questions | **Fixed 2026-10-02, live-verified**: `_CMELEVATE_ONLY_TERMS` accepts `sports (and|&) wellness`. OFF-017 192/192 | Medium | scheme classification |
| KI-196 | A block / district literal beside the one resolved village, in any form: `UPPER(TRIM(lgd_district)) = 'KASHARIPARA'` (the village name in the district column) survived both the beside-village drop and the pin's clean-up → 0 producer groups for a village with 11 (Focus Legacy all-villages run 2026-10-02; also `LOWER(lgd_district) = 'nongthylep'` (3 more villages) | **Fixed 2026-10-02, live-verified** (11): `_GEO_BESIDE_VILLAGE_RE` and `_PINNED_GEO_COND` accept any nesting of UPPER / LOWER / TRIM and an alias; `_mgnrega_drop_geo_beside_village` gates on `_village_scheme`. Tests: `test_focus_legacy_usecase_fixes.py::test_pinned_village_drops_a_trimmed_district_literal` | High | `_scheme_sql_rewrites` |
| KI-197 | One resolved village named by truncated TEXT: "TEPORPARA ( UPPER )" (273637) written as `UPPER(lgd_village_name) = 'TEPORPARA'` matched another village → 2 applications for 1 (CM Elevate Legacy re-run 2026-10-02). The village-missing guard saw `lgd_village_name` and stood down | **Fixed 2026-10-02, live-verified** (1): `_village_name_cond_to_code` (called from `_pin_missing_village_code`): a simple one-SELECT query with exactly one `lgd_village_name` condition and no `village_code` gets that condition replaced by `village_code = <code>`. Tests: `test_cm_elevate_legacy.py::test_village_named_by_truncated_text_is_replaced_by_its_code` | High | `_scheme_sql_rewrites` |
| KI-198 | CM Elevate Legacy place-spelling fix doubled a closing bracket: "Teporpara ( Upper )" written correctly became "Teporpara ( Upper ) )" (letter-word spans missed the trailing ")") | **Fixed 2026-10-02**: `_cml_fix_place_spelling` extends a span over the name's trailing punctuation when the answer has it. Tests: `test_cm_elevate_legacy.py::test_place_spelling_fix_keeps_a_name_that_ends_in_a_bracket` | Low | composer guarantees |
| KI-199 | Harness-only limits of the 2026-10-02 all-places runs (not bot defects): one question shape per level and scheme (a count / headline measure), the chip picker always takes the village / "all years" option, and village truth is computed by `village_code`. Other measures, years and phrasings per village are NOT covered | **Open — test coverage note** | Low (process) | QA harness |
| KI-200 | The Qdrant server the app moved to on 2026-10-03 (`http://115.124.102.167:6335`, Qdrant 1.18.3) is on a public IP over plain HTTP and accepted requests with NO API key when checked. Anyone who can reach it can read, overwrite or delete the KB and conversation-memory collections. It also holds six `Metadata_*` collections (1024-dim schema metadata) that this app does not use. | **Open**: the user will enable an API key and set `QDRANT_API_KEY` in `.env` (and on the VMs); restrict the port by firewall; prefer HTTPS. | High (security) | infra, `vectorstore.init_qdrant` |
| KI-029 | Follow-up provenance check covers schemes, districts and blocks only (not villages, years or categories) | Mitigated 2026-09-26 (field-specific checks; villages covered offline by the denied-text name check only) | Low | context_policy `rewrite_violation` |
| KI-041 | A block that shares its name with a village cannot be queried through the bot's own "The X block" chip ("…, the block, not the village"): the bot gives up, or answers with the VILLAGE figure | **Fixed 2026-09-26** (MGNREGA only; offline and live-verified) | High | pipeline `resolve_entities` village/block collision check |
| KI-042 | Assembly constituency SOUTH TURA is rejected as out of scope: the extractor tags it as a district, and `resolve_entities` raises `OutOfScope` without trying the constituency catalogue | **Fixed 2026-09-26** (MGNREGA only; offline and live-verified) | High | resolve_entities district branch (related KI-036) |
| KI-043 | "Which scheme?" is asked for measures that exist only in MGNREGA (materials, wages, women employment, employment %, "employment generated") | **Fixed 2026-09-26** (MGNREGA only; offline and live-verified) | Medium | scheme gate (`scheme-not-specified`) |
| KI-044 | Ratio SQL divides BIGINT by BIGINT, so Postgres truncates: person-days per household 38.00 instead of 38.16 | **Fixed 2026-09-26** (MGNREGA only; offline and live-verified) | High | SQL generation / execute_with_repair guard |
| KI-045 | "Which villages in <AC> received employment" returns village codes, not names, and counts villages with zero employment | **Fixed 2026-09-26** (MGNREGA only; offline and live-verified) | High | SQL generation / composer |
| KI-046 | MGNREGA administrative expenditure is refused instead of reported as 0.00 lakh (the column is unpopulated at source) | **Fixed 2026-09-26** (MGNREGA only; offline and live-verified) | Low | schema_context MGNREGA rule 10 / column-not-held gate |
| KI-047 | "Was more spent on wages or materials" is answered with no figures (the SQL returns only a CASE label) | **Fixed 2026-09-26** (MGNREGA only; offline and live-verified) | Medium | SQL generation / composer |
| KI-048 | "How much was spent and how many person-days … in <village/block>" never got working SQL: the join guard, then a 4B verifier false positive, then a repair onto `v_district_year_summary`. After 3 repairs it fell back to RAG, which treated the village as a scheme | **Fixed 2026-09-26** (MGNREGA only; offline and live-verified) | High | execute_with_repair / KB fallback |

---

### KI-001 — Clarification resume state is per-worker, in-memory
- **Status:** Open. **Severity:** High. **Component:** `app/session_store.py`, `app/routers/query.py`, `pipeline._run_pipeline` step c.
- **Symptoms:** a reply to "which area/year?" is treated as a fresh fragment, and the user is
  asked again, or the bare reply is answered as a standalone question.
- **Evidence:**
  - `Session.pending_scope_q` / `pending_village_hint` are in-process fields and are not in the
    persisted `ConversationState` (VERIFIED).
  - The deploy runs `--workers 2` on each of 2 VMs, and nginx has a single upstream with no
    stickiness (VERIFIED).
  - Audit (TECHNICAL_BRIEF §4.2 E, not re-measured):
    - 36% of turns were clarifications;
    - 66 sessions had 3 or more consecutive clarifications;
    - "all of meghalaya, all years" was asked 10 times as a standalone question.
- **Root cause:** VERIFIED structural cause: the state is not shared across workers. The share
  of loops that this causes, versus gate over-triggering, is NOT VERIFIED.
- **Expected:** a resume works whichever worker receives it.
- **Next investigation:** move the pending state into `ConversationState` (already persisted
  via `save_context_state`) or into Redis; consider consolidating the gates.
- **Regression risk:** high. Many tests cover resume behaviour (`test_stale_clarification.py`
  and others).

### KI-002 — SQL semantic verifier false positives
- **Status:** Open, mitigated. **Severity:** High. **Component:** `pipeline._verify_sql`, `prompt_builder.build_verify_prompt`.
- **Symptoms:** a correct SQL query is rejected, the repair loop burns its budget, and the user
  gets the KB fallback "couldn't build a working query".
- **Evidence:**
  - About 87 of 172 repair events were verifier flags (TECHNICAL_BRIEF §4.2 A).
  - Focus Legacy TC-22 was flaky (2 in 5) until a few-shot example was added (QA 2026-09-25).
  - Eight post-hoc false-positive filters exist in code (VERIFIED).
- **Root cause (INFERRED):** a 4B model judging long prose rules.
- **Attempts:** a calibration-example list plus 8 suppression filters. Each false positive
  patched so far added another filter.
- **Next investigation:** replace prose judging with deterministic checks (parse the SQL, and
  assert the resolved entities, tables and columns), or restrict the verifier to high-precision
  checks. **Do not simply disable it**: it also catches real bugs (e.g. the MAWLAI AC-vs-block
  case).

### KI-003 — Repair loop observability and convergence
- **Status:** Open. **Severity:** Medium. **Component:** `pipeline.execute_with_repair`, logging.
- **Evidence (VERIFIED):**
  - The loop logs only the error text, never the SQL.
  - Pipeline logs carry no request id, user or question. **Partly addressed 2026-09-26:** the
    `prompt_context` lines (`app/context_budget.py`) now carry `request_id` for every
    model prompt. Other pipeline log lines still do not.
  - `app.query_audit` has no SQL column.
  - Identical failing SQL is retried: 7 hard failures on `admin_recurring_exp` (TECHNICAL_BRIEF
    §4.2 B).
  - The repair prompt omits few-shot examples.
- **Next investigation:**
  - propagate `X-Request-ID` into the pipeline logs;
  - log each attempt's SQL;
  - stop early on identical SQL or an identical error.

### KI-004 — Generated SQL can reach `app.*` and the privacy tables
- **Status:** Open. **Severity:** High (security). **Component:** `app/db.py`, `deploy/sql/01_create_megh_app_role.sql`, `.env`.
- **Evidence (VERIFIED):**
  - `_assert_safe` checks only the statement shape and write keywords, not **which tables** are
    read.
  - `megh_app` has SELECT/INSERT/UPDATE/DELETE on `app` and SELECT on all of `curated`.
  - **The local dev `.env` connects as `postgres`.**
  - The privacy tables (`fact_focus_legacy_disbursement`, `bridge_pg_bank_history`,
    `fact_cm_elevate_disbursement`) are excluded only by prompt text.
  - Functions such as `pg_sleep` and `pg_read_file` are not blocked. They are bounded only by
    the 15 s timeout and the role's privileges.
- **Current behaviour:** a generated `SELECT … FROM app.users`, or from a privacy table, would
  pass every code check.
- **Expected:** a separate read-only role for generated SQL (curated views only), plus a table
  allowlist parsed from the SQL.
- **Documentation conflict:** `docs/SECURITY.md` (A03) previously called `megh_app` a
  "SELECT-only role". That was corrected on 2026-09-26.
- **Unknown:** which role production uses (UNKNOWN — NEEDS VERIFICATION).
- **Concrete exploit path (INFERRED from code, not attempted):**
  1. The user's question is inserted verbatim into the SQL prompt, with no injection
     filtering (AI_PIPELINE §2.5).
  2. A question crafted to make the 30B emit `SELECT username, password_hash FROM app.users`
     passes `_assert_safe`.
  3. `authorize()` checks only scheme/geography/granularity, and admin roles skip even those.
  4. The rows go back to the client in `data`.

  The regex guards and the verifier are not designed to stop this. There is no test for it.

### KI-005 — Auto-LIMIT bypass
- **Status:** Open. **Severity:** Low.
- **Evidence (VERIFIED):** `run_readonly` appends `LIMIT 1000` only if `"limit" not in sql.lower()`.
  A `LIMIT` in a CTE or subquery, or even a column name containing "limit", suppresses the
  outer cap.
- **Next:** check for a top-level LIMIT, or wrap the query: `SELECT * FROM (<sql>) q LIMIT n`.

### KI-006 — Keyword check matches string literals
- **Status:** Open. **Severity:** Low.
- **Evidence (VERIFIED):** `_FORBIDDEN` runs `\b(insert|update|delete|…)\b` over the whole SQL
  text, including quoted literals.
- **Effect:** a legitimate filter on a value containing such a word would be blocked. No
  observed occurrence (ROOT CAUSE NOT VERIFIED as a real-world failure).

### KI-007 — Authorization blind spots
- **Status:** Open. **Severity:** Medium (security). **Component:** `auth.authorize`, `_districts_in_sql`, `_blocks_in_sql`, `infer_granularity`.
- **Evidence (VERIFIED):** geography extraction only matches `lgd_district` / `lgd_block`.
  These escape the checks:
  - Focus Plus block filters (`block_name_raw`);
  - AC filters (`assembly_constituency_name`, `ac_name`);
  - `GROUP BY block_name_raw`, which is inferred as state grain.
- **Effect (INFERRED):** a district-capped or geography-pinned role could see block-level or
  other-area Focus Plus or AC data. Not observed in the logs.

### KI-008 — Dead retry setting
- `config.SQL_GENERATION_MAX_RETRIES=1` is never read. The repair budget is
  `pipeline.MAX_SQL_REPAIRS = 3` (a module constant since 2026-10-02, shared by
  `execute_with_repair` and the LangGraph repair loop) (VERIFIED). **Low.**

### KI-009 — Broad exception catch in `_run_pipeline`
- **Status:** Open. **Severity:** Medium.
- **Evidence (VERIFIED):** the final `except Exception` routes every non-transient error,
  including `KeyError` and `TypeError` bugs, to `_data_path_kb_fallback`. The user sees a
  plausible "couldn't build a query" message, and the bug is visible only as a log warning.
- **Next:** at minimum, count these in `/metrics`.

### KI-010 — Unused SME YAMLs; prose drift
- **Status:** Open. **Severity:** Medium.
- **Evidence (VERIFIED, `annotations.py` docstring):** these are **not loaded**:
  - `*_classification_rules.yaml`
  - `*_default_rules.yaml`
  - `*_response_template.yaml`
  - `*_schema_partitions.yaml`

  Their content was copied by hand into `schema_context.py` and `pipeline.py`. The
  `schema_context` docstring itself warns "do not let it drift".
- **Next:** before changing a scheme's behaviour, diff the YAML contract against
  `schema_context`.

### KI-011 — Prompt size and the all-schemes fallback
- **Status:** Open. **Severity:** Medium.
- **Evidence:**
  - `classify_scheme` returns `list(SCHEME_CATALOG)` on unusable classifier output (VERIFIED).
  - The few-shot block takes 5 examples per scheme (VERIFIED).
  - The all-6 prompt measured about 32K tokens, excluding the live-schema block (TECHNICAL_BRIEF
    §2.2).
  - `docs/INFERENCE_REQUIREMENTS.md` asks for `--max-model-len=16384` on the 30B; whether that
    was applied is UNKNOWN.
- **Risk (INFERRED):** truncated or degraded generation on cross-scheme questions and classifier
  fallbacks.

### KI-012 — No retry on transient errors
- `call_model` retries only when guided decoding is rejected. `httpx` timeouts and 5xx
  responses, and DB connect timeouts, are raised immediately (VERIFIED).
- The brief counted 66 `WinError 121` DB connect timeouts from the dev box.
- Worse than no retry: inside `execute_with_repair`, such errors trigger **LLM repairs** (KI-025).
- **Low.**

### KI-013 — Worst-case latency vs the 60 s ceiling
- The worst-case chain is 4 × (30 s generate + 15 s verify) plus the classifier and composer
  calls, which can exceed `REQUEST_TIMEOUT_SECONDS=60`. The request then returns a 504 rather
  than the KB fallback (INFERRED from the timeouts; VERIFIED config values).
- **Medium.**

### KI-014 — Per-IP, per-worker limits and caches
- **Rate limit:** 30 requests/minute per IP on `/api/query*`, with an in-memory backend by
  default. Officers behind one NAT share that budget.
- **Caches and sessions:** per worker, with Redis optional (it is blank in the local `.env`).
- **Medium at scale** (VERIFIED config; impact INFERRED).

### KI-015 — Gateway TLS verification off by default
- `llm._get_ssl_verify` returns `False` when `AI_MODEL_CA_BUNDLE_PATH` is unset or the file is
  missing (VERIFIED).
- The local `.env` sets the path. Whether `certs/enlight-aiops-internal-ca.pem` exists on each
  host is UNKNOWN.
- This is a deliberate fallback (commit `5c5a100`; D-010).

### KI-016 — No JWT revocation
- Logout is advisory, with a 12 h TTL. This is accepted in `docs/SECURITY.md` (A07).
- **Low.**

### KI-017 — No accuracy benchmark
- **Status:** Open. **Severity:** High (process).
- **Evidence (VERIFIED):**
  - All tests are regression locks: regex checks, prompt-text checks, catalogue checks and
    stubbed LLM calls.
  - Correctness is measured only by ad-hoc QA passes against the live DB and models, whose
    reports live in `docs/*.xlsx`.
  - The KPI workbooks (`data/reference/*_kpi_use_cases.xlsx`) have expected answers, but some
    are stale per the `schema_context` docstring.
- **Next:** a golden set per scheme (question → expected result), run against the live stack.

### KI-018 — `pytest tests` crashes
- **Evidence (VERIFIED 2026-09-26):**
  - `python -m pytest tests` ends with `INTERNALERROR … SystemExit: 0`. Pytest imports the
    plain-script suites, which run their checks and call `sys.exit()` at import time.
  - The run took 214 s, because the scripts' DB lookups time out when the DB is unreachable.
- **Workaround:** run pytest on the 22 pytest-style files only (see TESTING.md).
- **Fix options:** add `__main__` guards, or a `conftest.py` / `pytest.ini` that excludes the
  scripts. Not done; this was a documentation-only task.

### KI-019 — Focus Legacy residual NULL blocks
- **Evidence:** `docs/Focus_Legacy_Fix_Verification_Results.md` (2026-09-25).
  - TC-F1 finds 6 West Khasi Hills rows (5 PGs, ₹2.25 lakh) with a NULL block. They are part of
    the 88 rows that are blank in the source itself, and one of them uses a `PG-LAMP-` prefix.
  - Confirmation from the ingestion team is pending.
- **Not a bot defect.**

### KI-020 — PG alternate-spelling lookup not wired
- **Evidence:** the same document. The DB now provides `curated.v_focus_legacy_pg_search` /
  `dim_producer_group_name_alias` (1,634 PGs have several spellings). **No code in `app/`
  references them** (VERIFIED by grep).
- **Effect:** a user who types an old spelling gets "no such group".
- **Fixed 2026-09-29:** wired into `_focus_legacy_pg_name_answer` (see the table row).

### KI-021 — Repo hygiene
- `.claude/settings.json` holds permission entries for another project's paths
  (`backend/services/gemini_service.py`, `unified-data/`, `gateway.app`).
- `docs/~$*.xlsx` Excel lock files are committed.
- `nlp-service/` is an empty, untracked directory.
- `data/cm_elevate/prompt_assembler.py` and `data/focus_plus/prompt_assembler.py` are tracked but imported by nothing (dead code; ARCHITECTURE §3.13).
- Stale scheme-count strings in code are listed at the end of SCHEMES.md.
- **Low.** Nothing was changed in this documentation task.

### KI-022 — Raw PII file committed and pushed
- **Status:** Open. **Severity:** Critical (data protection). **Component:** repo root.
- **Evidence (VERIFIED 2026-09-26):**
  - `Focus Legacy to share to BLH.csv` is tracked in git, added in commit `7064ab6`.
  - `origin` = `https://github.com/Meghalaya-one/Megh-one-AI.git`, and `main` points at
    `origin/main` = `7064ab6`, so **the file is pushed**.
  - The header includes `name_on_the_account` and `account_no`.
  - Of its 14,569 rows, **14,491 `account_no` values are unmasked all-digit strings (0 look
    masked)**, and 10,353 rows carry an account-holder name.
  - This is exactly the data `schema_context.py` forbids the bot to query, and the view masks
    it.
- **Unknown:** whether the GitHub repo is private, and who has access (UNKNOWN — NEEDS
  VERIFICATION).
- **Expected:** raw source files with PII live outside the repo.
- **Next (a decision for the user, not a doc change):**
  - remove the file from the repo;
  - consider rewriting history (`git filter-repo`), which needs a force-push and coordination;
  - review access to the remote.

  **Not done in the documentation tasks, by instruction.**
- `Use_Cases_-_Focus.csv` (the 28 use cases) contains no PII columns.

### KI-023 — Plaintext seed passwords in `app/users.yaml`
- **Status:** Open. **Severity:** High (security).
- **Evidence (VERIFIED):**
  - `app/users.yaml` holds plaintext `password` values (11 characters) for `superadmin`
    (super_admin) and `rd-admin` (tenant_admin).
  - Its comment says the other accounts default to `CHANGE_ME`.
  - The file is committed and pushed.
  - `appdb.ensure_schema` seeds these accounts **only when `app.users` is empty**.
- **Mitigation that exists:** the `deploy/DEPLOYMENT.md` checklist says to rotate the seeded
  passwords.
- **Unknown:** whether production rotated them.

### KI-024 — Voice recordings saved on the dev box
- **Evidence (VERIFIED):**
  - `ASR_DEBUG_DIR` is set in the local `.env`.
  - `routers/query._save_asr_sample` writes every upload as a WAV plus a JSON transcript.
  - The directory is under the gitignored `logs/`.
- The config comment says to leave it empty outside local debugging.
- **Next:** make sure no deploy `.env` sets it.

### KI-025 — Repairs spent on infrastructure errors
- **Evidence (VERIFIED):** `execute_with_repair` catches `(UnsafeSQLError, Exception)`, so each
  of these triggers an LLM repair:
  - a DB connection failure;
  - a pool error;
  - an asyncpg `command_timeout`.

  After 3 wasted 30B calls, a non-timeout infrastructure error reaches `_run_pipeline`'s generic
  handler, and the user sees "couldn't build a working query" instead of "service busy".
- **Fixed 2026-09-28 (connection loss):**
  - `app/db.py` adds `DatabaseUnavailableError` and `is_connection_error()`. It recognises
    OSError / socket errors (including WinError 121 "semaphore timeout") and asyncpg's
    connection-class errors, anywhere in the cause chain. `TimeoutError` is excluded, because a
    slow query keeps its 504.
  - `execute_with_repair` raises it before any repair.
  - `_run_pipeline` re-raises it instead of calling `_data_path_kb_fallback`.
  - The router returns 503 "Couldn't reach the Megh One data service… retry" with
    `Retry-After: 5`.
  - Live: with megh_db unreachable, "beneficiaries in NANDICHAR II" raised
    `DatabaseUnavailableError` (no KB answer, no repair calls). With the DB up, 204/204 blocks,
    200/200 villages and 43/43 context checks passed.
- **Still open:** a slow-query timeout (`TimeoutError`) is still repaired before its 504. That
  path was not changed.

### KI-026 — No pool acquire timeout
- **Evidence (VERIFIED):** `DB_POOL_TIMEOUT` is passed to `asyncpg.create_pool`, where it becomes
  the per-connection **connect** timeout. `pool.acquire()` is called without a timeout.
- **Effect (INFERRED):** under pool exhaustion, requests wait until the 60 s request ceiling and
  return a 504.

### KI-027 — Startup-only snapshots
- **Evidence (VERIFIED):** `_SCHEME_DATA_YEARS` (`refresh_scheme_years`) and
  `schema_introspect.load()` are called only in `main.lifespan`.
- **Effect:** a new financial year or column in `megh_db` is invisible until the service
  restarts. Year chips and the out-of-range guard would wrongly refuse a newly loaded year.
- **Next:** restart after any ingestion change, or add a periodic or admin-triggered refresh.

### KI-028 — Follow-up context is per-worker
- **Status:** Fixed 2026-09-26, together with KI-001. **Live-verified the same day** with
  `tests/live_context_validation.py`: 8 scenarios, turns rotated across 3 independent worker
  stores through the real `app.conversations`. Every `sync_in` after turn 1 applied the other
  worker's state, and the scheme pause resumed on a different worker. Also verified offline:
  `tests/test_context_hardening.py` runs worker A → B → C over three independent
  `SessionStore`s with a backend that has the same revision rule. **Live check pending:**
  `tests/live_context_validation.py` rotates every turn across three workers through the real
  `app.conversations`.
- **Fix:** `app/session_sync.py`.
  - Postgres `app.conversations.context_state` is the source of truth. It holds a snapshot of
    the state, provenance, the last turn, the pending clarification, the recent questions and a
    `rev`.
  - `sync_in` runs on every request, and `sync_out` runs awaited after every answer and every
    pause.
  - The write is an upsert with a stale-write guard, so it no longer races `persist_turn`, and
    concurrent requests are resolved by revision.
  - The in-process copy is only a fallback when the DB is unreachable.
  - Redis was not used (optional cache, unset in dev). See D-023.
- **Residual:**
  - Two requests on one session at the same time: the second one's state is dropped and logged,
    not merged.
  - Cost: +1 SELECT and +1 UPSERT per request, bounded to 1 s each, not yet measured live.
  - The rest of the original entry follows, for the record.
- **Severity:** High. **Component:** `app/session_store.py`,
  `app/routers/query.py`, `pipeline._run_pipeline` step i.
- **Evidence (VERIFIED in code, 2026-09-26):**
  - `prev = session.last_turn` comes from the in-process `Session.turns`.
  - After a restart, a TTL expiry, or a request landing on the other worker, `turns` is empty.
    The router then rehydrates only `ConversationState` and the summary from
    `app.conversations`.
  - With `prev is None`, `has_antecedent` is False and no rewrite runs. `rewrite_followup`
    returns early for `prev is None`, so the Qdrant "relevant earlier turns", which are gated on
    `len(session.turns) < 2`, cannot reach the rewrite in exactly the resumed case they were
    built for.
- **Effect (INFERRED):** "what about Dalu block?" after a worker switch is answered as a
  standalone fragment, or bounced as "confused". The deploy runs 2 workers per VM with no
  stickiness (see KI-001).
- **Next:**
  - persist the last turn's `question`, `route`, `schemes`, `resolved_entities` and
    `result_summary` in `ConversationState` (it already round-trips through JSONB);
  - rebuild a synthetic `prev` from it on rehydration;
  - then drop or re-gate the `CONTEXT_MEMORY_MIN_SESSION_TURNS` check.

  Same fix family as KI-001.

### KI-029 — Follow-up provenance check scope
- **Status:** Mitigated 2026-09-26. `context_policy.rewrite_violation` now checks each field by
  its own rule:
  - years: permitted-source years, ±1 with a relative-year cue; an explicit user change always
    passes;
  - metrics: by family;
  - categories: closed vocabulary, including every sub-scheme alias;
  - names: capitalised words that appear only in text the rewrite was not allowed to use. This
    covers villages offline.

  **Remaining:** a village name that appears in no text at all (pure hallucination) is not
  caught offline. It is still resolved against the DB downstream, where an unknown village
  becomes a "not found" note, not a filter.
- **Original entry:** **Severity:** Low.
- **Evidence (VERIFIED):** `_rewrite_provenance_violation` checks schemes (`_SCHEME_NAME_PATTERN`)
  and district and block names (`entity_resolver.named_places`) only.
  - Villages are DB-backed and not in the offline catalogue.
  - Years are skipped on purpose: a changed year is usually the follow-up's own change, and
    `substitute_references` has already made "last year" concrete.
  - Categories (gender, occupation, status) are free vocabulary.
- **Mitigation:** the rewrite prompt no longer contains the previous answer unless the
  follow-up points into it, so the main contamination source is gone for these too.

### KI-030 — "All years" is not carried into a follow-up
- **Status:** Fixed 2026-09-29 (offline; see KI-130 to KI-135). **Severity:** Low (an extra question, not a wrong number).
- **Evidence (live 2026-09-26, scenario C):**
  - "How much was disbursed under Focus Plus in West Garo Hills?" led to the year pause, where
    the user chose "All financial years combined".
  - The follow-up "Show it by district." paused for the year again.
  - An all-years choice is not a resolved entity, so neither `prior_resolved` nor
    `ConversationState` carries it. Focus Plus tranches already have the equivalent
    (`tranche_all_combined`).
- **Next:** a `year_all_combined` flag, mirroring `tranche_all_combined`, that the year gate
  reads.

### KI-031 — Scheme swap keeps the old metric words
- **Status:** Open. **Severity:** Low.
- **Evidence (live 2026-09-26, scenario F):**
  - "What about Focus Legacy?" after an MGNREGA person-days turn became "How many person-days
    were generated under Focus Legacy in Dalu block in FY 2024-25?" (`_scheme_swap_rewrite`,
    deterministic, predates the context work).
  - The SQL model answered with Focus Legacy's own payment summary, so the number was right, but
    the question text names a metric that scheme doesn't hold.
- **Next:** when the swapped-in scheme does not carry the metric, drop the metric phrase, or
  route the swap through the model rewrite with METRIC awareness.

### KI-032 to KI-038 — found by the live context validation (2026-09-26)
Evidence for each is the per-turn pipeline capture in `docs/Context_Validation_Report_2026-09-26.md` (§1 findings F1–F8, and §4).
Every item below is **VERIFIED live** unless marked otherwise.

- **KI-032 (F1, High).**
  - `context_policy.plan_state_merge` CLEARs the block when the district changes, and
    `apply_merge_plan` drops it from *that* turn's fallback.
  - But `update_state` only overwrites fields that the result names, so `state.block` keeps the
    old value. K5: South Garo Hills with block=DALU.
  - Every later follow-up then inherits it through `merged_prior_resolved` (K8: SOUTH GARO HILLS
    + DALU).
  - **Next:** apply the turn's plan (CLEAR / cascades) to `session.state` in `update_state`.
  - **Fixed 2026-09-29** (offline): exactly that, plus a question with no plan resets geography
    and year (it inherited nothing).
- **KI-033 (F2, High).**
  - After a knowledge turn, the rewrite's antecedent is that knowledge turn, which has no
    filters.
  - The model took "West Garo Hills, Dalu block, FY 2025-26" from the turn-4 summary instead of
    the current Known-context line (South Garo Hills, FY 2022-23). The check correctly rejected
    it, and the fallback fragment lost the scope.
  - **Next:** use the last DATA turn as the antecedent when the previous turn was a digression.
    Refresh the summary on state change, or drop it from the prompt when the structured state is
    present.
- **KI-034 (F3, High).**
  - K8's fallback fragment carried resolved district/year in the MANDATORY entities block, yet
    the SQL had no WHERE clause, and `_verify_sql` returned ok.
  - **Next:** a deterministic guard, in the project's own pattern (a prompt rule, then a
    few-shot, then a guard). Every resolved district/block/year must appear in the SQL, or the
    query is repaired.
  - **Fixed 2026-09-29** (offline): `_resolved_scope_missing` (see KI-130 to KI-135).
- **KI-035 (F4, High).**
  - "Show beneficiaries for Focus Plus" (all years) returned per-year distinct counts, and the
    composer summed them to "199,099 total beneficiaries".
  - A read-only query shows 105,813 unique across both years. The faithfulness guard accepts
    it, because the sum is in the result digest.
  - **Next:** a schema rule or guard: never sum per-year distinct counts as a total of people.
    Answer with the cross-year `COUNT(DISTINCT)`.
- **KI-036 (F6, Medium).**
  - `resolve_dimension("Tura", "Focus Plus", "district")` gives WEST GARO HILLS, while the
    block resolves to TURA MUNICIPAL BOARD-MUNICIPAL BOARD. MGNREGA has ACs NORTH TURA and SOUTH
    TURA.
  - The extractor tagged a district, and no level-collision question was asked.
- **KI-037 (F7, Medium).**
  - J2/J3: TIKRIKILLA block-or-village was asked twice.
  - After "The TIKRIKILLA block", the resolved entities kept `village_code` 273249. The SQL
    correctly used the block, but the answer said "Tikrikilla village".
- **KI-038 (F8, Low).**
  - On a scope-pause resume, the scheme and metric from the paused question are labelled
    `model_inference`.
  - A year counted back by the model ("the year before that") is labelled
    `validated_database`.
  - No functional effect today, because neither field is an inherited key.

### KI-039 — Scheme substitution routed to RAG (fixed)
- **Reported 2026-09-26, reproduced live:** "how many beneficiaries in focus + for all of
  Meghalaya across all financial years" was answered correctly (105,813 / 385,671). Then
  "give me same for mgnrega" was answered with MGNREGA eligibility and entitlement text.
- **Trace:**
  1. `looks_like_followup` returned False at the `_mentions_scheme` check.
     `_SCHEME_SWAP_FOLLOWUP` did not match, because the message starts with "give me".
  2. No rewrite, merge plan or context was applied.
  3. `classify_intent` saw only "give me same for mgnrega", its keyword fast path missed, and
     the 4B classifier answered KNOWLEDGE.
  4. `rag.answer_from_kb` answered.

  That is context resolution failing at its first step, not a downstream reclassification.
- **Fix:** see AI_PIPELINE.md §5.6. Tests: `tests/test_scheme_substitution.py`. With the
  detector disabled, 82 of its 91 cases fail. Live: the reported conversation now answers from
  `curated.v_employment`, and 6 more substitution conversations kept every non-scheme filter.
- **The first query's SQL is correct:** `curated.v_focus_plus` has no state column. Its
  districts are Meghalaya's 12 (plus 4 NULL-district payments), and it holds only FY 2022-23
  and FY 2025-26. So no WHERE clause is exactly "all of Meghalaya, all financial years" (read
  from `pg_get_viewdef` and read-only counts).

### KI-040 — MGNREGA "beneficiaries" is not a defined measure
- **Evidence (live 2026-09-26):**
  - "how many beneficiaries in MGNREGA for all of Meghalaya across all financial years" gave
    `SUM(households_employed)` = 1,494,437 across 4 FYs.
  - "Show MGNREGA beneficiaries by district in FY 2025-26" gave `SUM(persons_employed)`.
  - Summed across years, the same household counts once per year.
- **Next:** decide with the SME which MGNREGA measure "beneficiaries" means. Add a schema rule
  and a few-shot for it, and say "household-years" when summing across years (the same family
  as KI-035).



### KI-041 to KI-048 — found by the MGNREGA use-case QA (2026-09-26), all fixed the same day
**Fix summary** (all gated on MGNREGA; the other schemes are unchanged by request):
- KI-041: `_NEGATED_LEVEL_RE` in `_explicit_level_in(question, schemes)`.
- KI-042: `_mgnrega_ac_reading` in the district branch of `resolve_entities`.
- KI-043: new MGNREGA-only terms in `_MGNREGA_ONLY_TERMS`.
- KI-044: `_mgnrega_numeric_division`, plus schema rule 8.
- KI-045: `_mgnrega_village_list_issue`, plus rule 9.
- KI-046: `_mgnrega_admin_expenditure_query` and `_answer` (D-025).
- KI-047: `_mgnrega_comparison_without_figures`, plus rule 10.
- KI-048: `_mgnrega_combined_facts_query` (D-025).
- Also fixed:
  - the "100 days" faithfulness false positive (row-dump answers);
  - statewide top-1 wording (`_mgnrega_answer_notes`, `_mgnrega_top_one_wording`);
  - a ÷100 under a `_lakh` alias (`_mgnrega_lakh_not_divided`).
- Tests: `tests/test_mgnrega_usecase_fixes.py`.
- Report: `docs/MGNREGA_UseCase_Test_Report_2026-09-26_v2_after_fixes.xlsx`.
- **Still open elsewhere:** the negated-level chip bug (KI-041) also exists for the other
  schemes. It was left there on purpose, because the user asked that they not change.

Original findings:
Source: the 30 use cases in `Test_Case_Results_21st_Sep_26_MGNREGA (1).csv`, run live through
`pipeline.answer_question` (2 full runs), with every figure checked against megh_db and the raw
CSVs. Result: **16 PASS / 14 FAIL**. Report: `docs/MGNREGA_UseCase_Test_Report_2026-09-26.xlsx`.
Data-side findings: `docs/MGNREGA_DB_Issues.md`.

- **KI-041 (High).** Affects DATA-003, 016, 020 and 029.
  - "…in MAWKYRWAT during 2024-25, the block, not the village" either fails with "couldn't
    build a working query", or answers **273 households** (the village, `village_code =
    277399`) instead of the block's **11,590**.
  - Clicking the bot's own "The MAWKYRWAT block" chip sends exactly this text, so the chip
    always fails.
  - The same question worded "Mawkyrwat block" is correct: 4,079.00 lakh and 11,590.
  - DATA-029 (MAWRYNGKNENG) answered in only 1 of 5 runs.
  - **Cause (VERIFIED by tracing `resolve_entities` live):** `_explicit_level_in` detects the
    stated level finest-first. It read "…, the block, not the village" as level = **village**
    (the negated word), even though the extractor had tagged MAWKYRWAT as a block.
    `resolve_entities` then re-slotted the name as a village and pinned `village_code` 277399.
    (An earlier note blamed the village/block collision check near the village branch. That
    check is never reached on this path.)
  - `execute_with_repair` then rejects the block SQL (the "VILLAGE name placed in a block/district
    column" guard, around L7223), or the SQL uses the village code.
  - 26 LGD names are both a block and a village. This is the same family as KI-037.
- **KI-042 (High).** Affects DATA-004 and DATA-014.
  - "How many persons received employment in SOUTH TURA during 2024-25?" gets the
    out-of-scope reply in both runs. It comes from `resolve_entities` (`OutOfScope`), not
    from the edge layer.
  - Expected: 152 persons. SOUTH TURA has one village in the data, SANGSANGGRE.
  - GAMBEGRE and NONGKREM constituencies work.
- **KI-043 (Medium).** Affects DATA-011, 014, 017, 019, 023 and 030.
  - The figures are correct once MGNREGA is chosen. The extra step is the testers' repeated
    21 Sep complaint.
  - It is inconsistent: a village "women were provided employment" question does not ask, while
    a district "women received employment" question does.
- **KI-044 (High).** Affects DATA-021.
  - The SQL is `ROUND(SUM(person_days) / NULLIF(SUM(households_employed), 0), 2)`, which
    integer-divides.
  - The DB and raw both give 1,632,188 / 42,770 = 38.16. Needs a rule, a few-shot and a guard
    (cast to numeric).
- **KI-045 (High).** Affects DATA-014.
  - For NONGKREM 2024-25 the bot says "Four villages … received employment", listing codes
    278375, 278386, 278399 and 278424.
  - Only URMASI-U-JOH had employment (66 persons). The other three rows are zero.
- **KI-046 (Low, product).** Affects DATA-013.
  - The refusal text is accurate: 0.90 lakh statewide, on one row.
  - The use case expects the aggregate, 0.00 lakh for MAWRYNGKNENG.
- **KI-047 (Medium).** Affects DATA-023.
  - For JAKREM 2024-25: "…with no specific monetary figures provided in the available data".
  - Actual: wages 219.48 lakh against material 113.89 lakh.
- **KI-048 (High).** Affects DATA-028.
  - The JAKREM 2024-25 question is answered from the reference documents in both runs.
  - Expected: 333.37 lakh and 57,391 person-days.

### KI-049 to KI-056 — found by the all-blocks / all-villages MGNREGA run (2026-09-26), all fixed
**Scope of the run:** FY 2024-25, every one of the 56 blocks × 8 questions, and every one of the
6,425 villages × 2 questions (person-days, total expenditure). Expected values came from megh_db
by village_code and from the raw CSVs. The report is
`docs/MGNREGA_AllBlocks_AllVillages_Test_Report_2026-09-26.xlsx`.

**Causes and fixes** (all in `app/pipeline.py`, all gated on MGNREGA):
- **KI-049.** `_explicit_level_in` strips a chip's scope tail only when the tail carries
  ", the village"; the village chip never does. A resume with `village_hint` and only the tail's
  "block" word is therefore not a block question.
- **KI-050.** `_mgnrega_village_chip_pin` reads the ONE village from the chip text itself (the
  tail's block and district, plus the name in the stem, looked up in `dim_geography`). It runs
  before every place branch; the collision gate is skipped once pinned; only `village_code` goes
  to SQL.
- **KI-051.** `_vcap` = 10 for MGNREGA in every village-chip list.
- **KI-052.** `_mgnrega_admin_reading_of_village_mention`: an exact block and/or AC name that is
  not an exact village name is re-read as the block, the AC, or asks which one.
- **KI-053.** A longer exact village name in the raw question that contains the mention
  replaces it (`_scan_village_in_question`). Stray place slots that are part of it are dropped.
- **KI-054.** Three layers:
  - `_mgnrega_village_filter_missing` (a repair guard);
  - `_mgnrega_pin_village_code` (rewrites a wrong single code to the resolved one);
  - `_verifier_village_code_complaint_is_false` (discards a check-2 complaint when the SQL
    filters exactly the resolved code and year).
- **KI-055.** The region check is skipped when a village, block or AC is resolved
  (`_mg_place_settled`).
- **KI-056.** `_mgnrega_empty_answer` (a row count decides between "no records" and "cannot be
  calculated, base is 0"), `_mgnrega_null_zero_notes`, and `_mgnrega_zero_backstop`.
- **Tests:** `tests/test_mgnrega_usecase_fixes.py` (60 in total).
- **Data side (not a bot bug):** 16 villages sit in a different block in the DB than in the raw
  file, and some blocks have no records for a fact (KHATARSHNONG LAITKROH has no expenditure
  rows). See `docs/MGNREGA_DB_Issues.md`.

### KI-057 to KI-059 — found by the full all-villages run (2026-09-26), all fixed
All fixes are in `app/pipeline.py`, all gated on MGNREGA. Tests are in
`tests/test_mgnrega_usecase_fixes.py`.

- **KI-057.** Six fixes, one per kind of name:
  - **A level word inside a village name** ("BLOCK CAMPUS") does not set the stated level.
  - **An out-of-state place word:** `_mgnrega_village_not_out_of_area` handles the edge layer's
    out-of-area refusals when an exact MGNREGA village name carries the place word. A longer name
    is simply the village. A bare "MANIPUR" / "BURMA" asks "the village, or the place outside
    Meghalaya?"; the outside choice is still refused.
  - **Another scheme's word:** scheme detection reads the question with the village name masked
    when that name carries another scheme's vocabulary
    (`_mask_scheme_words_in_village_name`).
  - **"&" / "INCL" names:** `_mgnrega_longest_village_in` finds the whole name (≈6,500 MGNREGA
    names, cached) and replaces the extractor's fragments.
  - **Qualifiers:** `_village_chip_question` (MGNREGA only) no longer doubles a qualifier.
  - **A village in the block slot:** a block-slot mention that is not a block but is an exact
    village moves to the village slot.
- **KI-058.** `_resolve_village_for` handles both quirks. A village's own exact name beats alias
  hits, and "Unresolved" placeholders are dropped. MGNREGA's facts hold none of them (read-only
  check).
- **KI-059.** Two rewrites:
  - `_mgnrega_drop_geo_beside_village` removes `lgd_block` / `lgd_district` literals next to the
    resolved `village_code`;
  - `_mgnrega_pin_village_code` also pins an IN-list to the one resolved code.

### KI-060 to KI-067 — found by the Focus Plus use-case QA (2026-09-27); all but KI-065 fixed the same day
Round 1 scored **19 / 30** test cases (27 / 39 questions), identical on two live runs. Report:
`docs/Focus_Plus_UseCase_Test_Report_2026-09-27.xlsx`, with evidence images in
`docs/Focus_Plus_UseCase_Evidence_2026-09-27/`.

**Round 2 (after fixes, same day): 30 / 30** on 2 of 2 fresh live runs. Report:
`docs/Focus_Plus_UseCase_Test_Report_2026-09-27_v2_after_fixes.xlsx`, with evidence in
`..._after_fixes/`. Rationale is D-026, and the design is in AI_PIPELINE §2.9 step 3 and §5.2.
Tests: `tests/test_focusplus_usecase_fixes.py` (32).

**Root causes (VERIFIED in code) and fixes:**
- **KI-060:** no share was computed anywhere. Fixed by `_fp_breakdown_shares` and
  `_fp_single_count_share` (a parameter-bound recount that must equal the bot's figure).
- **KI-061:** nothing guaranteed a difference. Fixed by `_fp_comparison`, which also drops the
  combined-total sentence.
- **KI-062:** `_answer_numbers_faithful` strips commas before comparing. Fixed by
  `_fix_digit_grouping`, applied in `compose_response`.
- **KI-063:** the bank pause had no options, and its rule was not in `SCHEME_PAUSE_RULES`. Fixed
  with chips under `bank-scheme-not-specified`.
- **KI-064:** "12.5" from `'12.5K'` was flagged as a misquote, which led to the row-dump
  fallback. Fixed by `_sql_literal_numbers`. `amount_raw` was never formatted: fixed by
  `_fp_format_numbers`. The scope gate ignored batch and tranche: fixed in
  `_needs_scope_clarification`.
- **KI-066:** the router remembered `req.question`. Fixed by `pause_question`.
- **KI-067:** a missing dictionary key. Fixed.

The round-1 descriptions follow.

- **KI-060.** Every figure is right, but no percentage appears in the text or in the SQL result.
  The Expected Result asks for "counts and percentages". The base is the 12.5K cohort (12,527):
  - Pending 94.29%, Approved 5.70%, Rejected 0.01%;
  - Female 81.93%;
  - verification Approved 100%.

  The likely fix follows the project pattern: a few-shot with a share column, plus a deterministic
  check.
- **KI-061.**
  - FOCUS-026: both district counts are right, but the answer gives "combined total 48,647" and not
    the difference, 21,431.
  - FOCUS-027: the answer does not name the higher district (East Garo Hills).
  - FOCUS-028: the answer does not give the tranche difference (Rs 233,215,000).
- **KI-062.** "How many female beneficiaries received Focus+ assistance during 2025-26?" printed
  **"1,0263"** in both runs; the SQL returned 10,263. The numeric-faithfulness check did not catch
  the regrouped digits. **Probable cause (INFERRED):** it compares digits with the commas stripped.
  "How many women…" (021a) printed 10,263 correctly.
- **KI-063.** The clarification `column-not-held` offers no chips. The officer's typed reply
  "Focus Plus" is then treated as a new question, a scheme overview from the KB. With "Focus+" in
  the question it answers correctly: State Bank of India, Rs 808,575,000 (FOCUS-029b). The tester
  reported the same failure on 14 Sep.
- **KI-064.** Cosmetic:
  - amounts appear as "126197500.00";
  - FOCUS-006a's whole answer is "31,317,500 amount raw.";
  - gender, occupation and status answers do not say they cover only the 12,527 registration-cohort
    people. The 93,286 legacy beneficiaries have no person columns.

  Statewide batch and tranche questions also first ask "which area?".
- **KI-065.** Data-side. See `docs/Focus_Plus_DB_Issues.md`:
  - 144 Dalu rows are stored as South West Garo Hills;
  - 31 Betasing / Damalgre / Zikzak rows are stored as West Garo Hills.

  West Garo Hills: DB 35,039 beneficiaries vs raw 35,059. The bot matches the DB.

### KI-068 to KI-075 — found by the CM Elevate use-case QA (2026-09-27); all fixed the same day (KI-074 interim)
Scored **23 / 30** test cases (47 / 55 questions). The two live runs gave identical answers. Report:
`docs/CM_Elevate_UseCase_Test_Report_2026-09-27.xlsx`, with evidence images in
`docs/CM_Elevate_UseCase_Evidence_2026-09-27/`. The raw workbook `CM_Elevate_AllSchemes_20260927_full.xlsx`
(8,627 rows) equals `curated.v_cm_elevate` exactly, so there is no data-side issue.

- **KI-068 (005a).** The SQL for a programme-wise count adds `WHERE entity_type <> 'Unresolved'`.
  CM ELEVATE RULES rule 5 allows that only for village counts. The answer text also names only 5
  of the 15 programmes with a figure. It is the same bug class as the Unresolved filter fixed for
  CM Elevate Legacy and Focus Legacy (`_cm_legacy_keep_unresolved_off_village`). VERIFIED: that
  guard's `_UNRESOLVED_PLACEHOLDER_SCHEMES` lists only CM Elevate Legacy and Focus Legacy
  (`app/pipeline.py` ~L7159). Adding `["CM Elevate"]` is the likely one-line fix.
- **KI-069 (009b, 029b).** The SQL is `COUNT(DISTINCT request_id) … scheme_name IN (…)` with no
  GROUP BY. It is correct as a combined total but misses the per-programme figures the use case
  asks for. The rule 4 prose did not hold under sampling. Per project practice a deterministic
  guard is needed.
- **KI-070 (018a).** The SQL is `LOWER(current_level) = 'level2' AND data_verified = 'On Hold'`,
  which returns 0. The composer then calls it "not covered". A 0 from a valid query should be
  stated as 0, and "pending at level N" should count the level (KI-074 decides the "pending" part).
- **KI-071 (027a/b).** `_fp_comparison` (KI-061) is gated on Focus Plus. Extending it to CM Elevate
  would satisfy "highlight the differences".
- **KI-072 (028b).** Use `COUNT(*) FILTER (WHERE data_verified = 'On Hold')` grouped by sector, not
  a WHERE filter, so that zero sectors survive.
- **KI-073 (025a).** Six scheme × sector rows became an unlabelled list. The rows and SQL are
  right. The same question for Ri Bhoi (025b) was readable.
- **KI-074.** DOCUMENTATION CONFLICT — NEEDS VERIFICATION with the product owner: which field
  should "pending" mean? `file_status` also makes "approved / rejected" answerable (1 / 64).
- **KI-075.** An extra click only.

**Round 2 (after fixes, same day): 30 / 30** (55 / 55 questions) on 2 of 2 fresh live runs.
- Report: `docs/CM_Elevate_UseCase_Test_Report_2026-09-27_v2_after_fixes.xlsx`, with evidence in
  `docs/CM_Elevate_UseCase_Evidence_2026-09-27_after_fixes/`.
- Rationale: D-027. Design: AI_PIPELINE §2.8 (SQL rewrites and a verifier filter) and §2.9
  step 3 (answer guarantees).
- Tests: `tests/test_cmelevate_usecase_fixes.py` (33).

**Found and fixed while re-testing:**
- The first prompt line for "pending at level N" made a plain "pending" question
  (019a) filter `current_level = 'level1'`, reporting SEED 3,633 "pending". Fixed by tightening
  the prompt wording and adding `_cm_elevate_pending_without_level`.
- A reworded probe, "rejected in Ri Bhoi", filtered `current_file_status = 'rejected'`, a value
  that column never holds. The confident 0 was against a true 27. Fixed by
  `_cm_elevate_decision_status_column`.
- The 4B verifier then rejected the corrected SQL three times ("v_cm_elevate has no
  scheme_specific column"). Fixed by `_verifier_scheme_specific_complaint_is_false`.

**KI-076** (open, Low): see the table. Not caused by these changes, and not in the use-case sheet.

### KI-077 — MGNREGA women share answered "0.00%" (user report 2026-09-27, fixed)
- **Report:** "What percentage of employment persons were women in ekh?" gave "0.00% … a genuine zero".
  The SQL used `year_key = (SELECT MAX(year_key) …)`.
- **Causes (VERIFIED):** three problems combined.
  1. `_METRIC_OR_BREAKDOWN_CUE` has no percentage or share words, so the FY pause was skipped
     and the model silently chose the latest year.
  2. FY 2025-26 `women_employment_provided` is 0 on every record statewide: it is not populated
     at source (`MGNREGA_DB_Issues.md`, Issue 2).
  3. The `_mgnrega_null_zero_notes` "genuine recorded ZERO" note (KI-056) then told the
     composer to call it genuine.
- **Correct figures** (megh_db = raw), East Khasi Hills:
  - 72.16% in 2022-23, 75.99% in 2023-24 and 77.70% in 2024-25;
  - 74.90% over those three years;
  - FY 2025-26 is not recorded. A naive all-years ratio gives 55.86%, which is wrong.
- **Fix** (`app/pipeline.py`, MGNREGA only):
  - A percentage, share or women question with no year now asks the FY
    (`_needs_year_clarification`). Women questions get only the years that carry women data,
    plus "All years with women data" (`_mgnrega_women_year_clarification`).
  - A women count or share for one area is a deterministic, parameter-bound query over the named
    year, or over every year with women data (`_mgnrega_women_query`). The answer always states
    the years it covers, and says FY 2025-26 is not recorded.
  - Explicit FY 2025-26 gets "not recorded at source" plus the latest year's figure
    (`_mgnrega_women_unrecorded_answer`). A generator query that reads only FY 2025-26 women
    (for example a ranking) gets the same message.
  - A generator query over all years is restricted to the recorded years
    (`_mgnrega_women_years_only`). The genuine-zero note and backstop are skipped for women SQL.
  - Which years carry women data is read live (`_mgnrega_year_women`), so a later ingest that
    fills FY 2025-26 is picked up with no code change.
- **Tests:** `tests/test_mgnrega_usecase_fixes.py` (84).

### KI-078 — a calendar date read as a financial year (user report 2026-09-27, fixed)
- **Report:** "How many PMAY houses were sanctioned on 2017-11-28?" answered "504 houses sanctioned
  for FY 2017-18, not the 11 or 28 figures assumed in the question".
- **Causes (VERIFIED):** the SQL was already right (`sanction_date = '2017-11-28'`, 504, checked in
  megh_db). Two text parsers misread the date:
  1. `_EXPLICIT_FY_RANGE_RE` matched "2017-11", so `_backfill_explicit_year` set FY 2017-18 and
     the composer scoped the answer to that FY.
  2. `premise_check._QTY_RE` took the 11 and 28 as asserted figures ("on" + "sanctioned"
     nearby), so the composer was told to correct them.
- **Fix:**
  - `_EXPLICIT_FY_RANGE_RE` rejects a range followed by a third `-NN` / `/NN` part.
  - `premise_check.extract_premises` blanks full dates (`_DATE_RE`) before looking for numbers.
  - `_needs_scope_clarification` treats a full calendar date (`_CALENDAR_DATE_RE`) as a time pin.
    Without it, removing the wrong FY made the bot ask "which area and time period?".
- **Scope:** all schemes, but the fix only touches questions that contain a full date
  (yyyy-mm-dd or dd-mm-yyyy, with `-`, `/` or `.`). FY ranges, bare years and real premises are
  unchanged.
- **Live:** 2017-11-28 and 28/11/2017 → "504 PMAY houses were sanctioned on …". "2017-18" →
  15,513 for FY 2017-18, still back-filled. The undated question still asks for scope.
- **Tests:** `tests/test_calendar_date_filter.py` (18).

### KI-079 to KI-088 — found by the Focus Plus all-blocks / all-villages run (2026-09-27)
Blocks: 51 blocks × 4 questions. Villages: every village_code in v_focus_plus (3,513 villages and wards,
10 "Unresolved" placeholders excluded) × 2 questions. Report:
`docs/Focus_Plus_AllBlocks_AllVillages_Test_Report_2026-09-27.xlsx`. Rationale: D-028.
- **Before fixes:** blocks 201/204 (only "Nan"); villages 52/301 on the first sample.
- **Root causes (VERIFIED in code) and fixes, all in `app/pipeline.py`:**
  - **KI-079:** every village guard of the MGNREGA all-villages fixes (KI-049 to KI-059) was gated
    `schemes[0] == "MGNREGA"`. They now use `_village_scheme(schemes)` (MGNREGA or Focus Plus): the
    negated-level chip reading, the chip-tail level fix, the chip pin, the level-word-in-name fix,
    the whole-name village lookup (`_mgnrega_longest_village_in(q, scheme)` over
    `_focusplus_village_names`), the widening of truncated mentions, the admin re-read, the
    placeholder drop and `_vcap`. `_focusplus_pin_village_where` rebuilds a single-village WHERE:
    every place condition (lgd_district / lgd_block / block_name_raw / district_name_raw /
    lgd_village_name / entity_type / village_code) is replaced by the one resolved `village_code`,
    and other conditions stay. The verifier's false village complaint filter also accepts a
    Focus Plus `financial_year_short` year.
  - **KI-080:** `_focusplus_narrow_village` (in `_resolve_village_for`): an exact Focus Plus name,
    narrowed by block/district, wins; otherwise ambiguous candidates are narrowed to villages
    that hold Focus Plus data.
  - **KI-081:** `_village_code_tag` adds "(LGD code)" to the label and the chip question only
    when a same-name village sits in the same block (village-grained schemes only). The chip pin
    reads it, also without a `village_hint`. The code is kept out of the premise check and
    allowed by the faithfulness check.
  - **KI-082:** `_focusplus_alias_district_collision` asks district / block / village when a
    bare name reaches a district only via an alias and is an exact Focus Plus village.
    `entity_resolver.collides_across_dimensions` is unchanged, because MGNREGA relies on it.
  - **KI-083:** "Nan" resolves as the stored block value, with a deterministic explanation.
- **Tests:** `tests/test_focusplus_usecase_fixes.py` (+16). Two MGNREGA tests that pinned the old
  MGNREGA-only gates were updated, with the reason in the test.
- **Late fixes, found by the final full pass (KI-085 to KI-088):**
  - **KI-085:** a condition whose literal is a Focus Plus block or district name is dropped from
    a single-village WHERE (`_fp_place_literal`).
  - **KI-086:** a `geography_key IN (SELECT …)` subquery is replaced by the resolved code.
  - **KI-087:** a Focus Plus single-figure answer must contain that figure. Otherwise: strict
    retry, then `_deterministic_answer`. This applies to the retry check too.
  - **KI-088:** `_mgnrega_village_not_out_of_area` accepts Focus Plus questions.
  - Also: `_place_title` keeps a standalone roman numeral ("Nandichar II", not "Ii"), and two
    log lines no longer say "MGNREGA" for Focus Plus.
- **Final result:**
  - blocks **204/204**; villages **7,026/7,026** (8 re-run on the final code, 3/3 each);
  - confirmation pass on the final code: 204/204 and 1,000/1,000 random villages;
  - MGNREGA regression: 448/448 blocks and 300/300 villages;
  - live context 43/43; pytest 936; scripts 14/14.
- Four of the final pass's failures were infrastructure: the VPN dropped mid-query (KB fallback,
  KI-025) and a gateway 502.

### KI-089 to KI-095 — found by the PMAY-G use-case QA (2026-09-28); open, no code changed
- **Run:** the 28 use cases in `PMAY-G.csv` (PMAY-OFF-001 … 028), placeholders filled with real
  values (71 questions), put twice to `pipeline.answer_question` with live megh_db and gateway.
  Result: **17 / 28 test cases, 49 / 71 questions**. Report
  `docs/PMAY_G_UseCase_Test_Report_2026-09-28.xlsx`, evidence
  `docs/PMAY_G_UseCase_Evidence_2026-09-28/`.
- **Data:** `curated.v_pmay` equals the raw `PMAY_FullyMapped_with_dates.csv` row for row
  (171,107 rows by id; 13 fields, 0 mismatches). So every failure is bot-side.
- **Passing:** every village count (beneficiaries, completed, incomplete, status breakdown,
  full / none released, unique sanction numbers), block and district counts, completion % and
  release %, village comparison (run A), ISO and dd/mm/yyyy date counts.
- **KI-089:** the per-year GROUP BY is the main wrong-number source. The deterministic
  answer then dumps rows. A fix in the house style: a prompt rule plus a few-shot for "financial
  summary" (sanctioned, released, sanctioned − released, one row) and "compare A and B" (one row
  per entity), and a deterministic guard that rejects a `financial_year` GROUP BY when the
  question names no year and asks for no trend.
- **KI-091:** `resolved_entities` for 009/028 = `{"village_code": …, "house_status": "House Sanctioned"}`.
  The resolver YAML already says "sanctioned in <year>" is a date filter, not the status. "Sanctioned
  amount" needs the same exclusion.
- **Not failures:** every question without a year pauses for the FY. This was a product decision on
  2026-09-09 (`_needs_year_clarification`), and "till date" / "overall" / "all years" skips it.
  Counts exclude the 126 zero-sanction placeholders, as the tester asked.

**Update (same day): all fixed — KI-089 to KI-097.** Result: use cases **28/28** (71/71 questions on 2 of 2 fresh live runs, identical answers; round 1 was 17/28); all blocks **224/224**; all villages **5,120/5,120** on the final code (round 1 4,393/5,120 → KI-096 fixed).
- **KI-089/090/092/093/095:** a deterministic PMAY-G facts path (`_pmay_facts_query`,
  `_pmay_facts_answer`; D-029) answers the fixed shapes from one parameter-bound query and writes
  the answer in code. The model path keeps rankings / trends / "each" / allotment, with
  `_pmay_sql_issue` (per-year GROUP BY on a whole-period question, AVG-based utilisation),
  `_pmay_comparison_limit` (LIMIT 1 on N named areas), `_PMAY_RULES` 7–9, three few-shots and
  `_pmay_rupee_format`.
- **KI-091:** `resolve_house_status` matches a stage phrase with "sanctioned" only in order and
  not before amount / money / funds; alias "sanctioned stage" added to the resolver YAML.
- **KI-094:** written dates in `_CALENDAR_DATE_RE` and premise `_DATE_RE`.
- **KI-096 (found by the all-villages run, 727 of 5,120):** PMAY-G joined `_village_scheme`
  (`_pmay_village_names`, narrowing in `_resolve_village_for`, alias-district check); MANIPUR /
  BURMA in `_mgnrega_village_not_out_of_area`.
- **KI-097:** "sactioned" typo in `_PMAY_ONLY_TERMS`; `\bblocks?\b`; `_pmay_no_houses_answer`
  (placeholder-only village RTIANG SANPHEW); `_pmay_unknown_place_answer` (a made-up village no
  longer reads as "0 beneficiaries").
- **Reports:** `docs/PMAY_G_UseCase_Test_Report_2026-09-28_v2_after_fixes.xlsx`,
  `docs/PMAY_G_AllBlocks_AllVillages_Test_Report_2026-09-28.xlsx` (+ evidence folders).

### KI-106 to KI-120 — found by the CM Elevate all-districts / all-blocks / all-villages run (2026-09-28), all fixed
7,364 questions: 12 districts, 66 blocks and 2,087 villages x 3 phrasings, across the use-case question types.
Every figure was checked against megh_db and the raw workbook, and the SQL had to be scoped to the asked place.
- Round 1: 4,132 of the 4,208 questions it ran passed. It was stopped once the fixes landed.
- Final code: 7,364 / 7,364, from a complete pass (7,357), the 7 remaining failures fixed and re-run 7/7, and a
  700-question regression sample at 700/700.

Report: `docs/CM_Elevate_AllBlocks_AllVillages_Test_Report_2026-09-28.xlsx`, with before/after evidence in
`docs/CM_Elevate_AllBlocks_AllVillages_Evidence_2026-09-28/`.

Root cause of most failures (61 of the 76 in round 1): CM Elevate's block catalogue is older than its data. It
lacks 21 of the 66 stored blocks, including all 9 urban bodies, and CM Elevate was outside the shared village
guards. Tests: `tests/test_cmelevate_usecase_fixes.py`.

### KI-130 to KI-135 — context relevance and the semantic contract (2026-09-29)
Reported as one conversation after "beneficiaries in focus+ across all financial years"
(105,813). All reproduced **offline** first, with the real `_run_pipeline`, the 4B model, RAG and
the DATA path stubbed (the VPN was down: `10.48.242.4` answered neither 5432 nor 443).
- **KI-130 (root cause).** Two steps agreed to inherit the scheme for any short message:
  `edge.detect_edge_case(has_context=True)` skips its whitelist gate whenever a previous answer
  exists, and `looks_like_followup` reads any ≤6-word message with no anchor as a fragment. The
  4B rewrite then appended the previous scheme, and `inject_scheme_hint` pinned it. **Fix:**
  `context_policy.continuation_signals(q)` lists why a message may continue the turn (domain
  vocabulary, scheme, place, year, metric, category, grouping, comparison, reference, ranking,
  a how-the-scheme-works word, a short "what about / and" lead, non-English script). With none,
  `has_context` is False: the whitelist applies, no rewrite runs, and the KNOWLEDGE path does not
  fall back to the previous scheme. Every real follow-up shape in the test corpus keeps a signal.
- **KI-131.** A first-person request for money ("give me five thousand loan", "I am in crisis")
  is refused politely, before the scheme-intent exit (like `money_advice`), and points at the
  schemes that offer support. Data questions about loans are untouched.
- **KI-132.** Focus Plus pays exactly ₹5,000 (Tranch 1, 93K, FY 2022-23) or ₹2,500 (later
  tranches) — `data/focus_plus/README.md` §6.3. A stated amount is a real filter. Held amounts
  must reach the SQL (guard → repair); an amount Focus Plus never pays pauses with three chips; a
  "loan" gets a "Focus Plus is not a loan" note. Other schemes: `stated_amount_filters` parses
  their amounts but no guard applies (their per-row amount semantics are not SME-confirmed).
- **KI-133.** No alias was invented. The near-miss check offers only districts whose SME acronym
  has the typed letters, and stays silent once the text names the district.
- **KI-034 guard** (`_resolved_scope_missing`): resolved district / block / comparison lists and
  year_key must appear in the SQL. Not required: geography beside a resolved village_code,
  hill-range expansions, any year for CM Elevate or an all-years question. LIKE / ILIKE on the
  place's distinctive word and doubled apostrophes count as present.
- **Not fixed here:** KI-035 (summing per-year distinct counts), KI-040 (MGNREGA
  "beneficiaries"), KI-036/037 (block/village level memory).

