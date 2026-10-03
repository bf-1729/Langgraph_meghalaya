# AI Pipeline — Megh One AI

*Reconciled against the code on 2026-09-26 (commit `7064ab6`). Function names are exact; line
numbers are omitted on purpose because `pipeline.py` moves constantly. Use Grep to find a
function.*
*Labels: **VERIFIED** (code, config or test) · **INFERRED** · **UNKNOWN — NEEDS VERIFICATION** · **PLANNED**.*

Source of truth: [app/pipeline.py](../app/pipeline.py), [app/prompt_builder.py](../app/prompt_builder.py),
[app/llm.py](../app/llm.py), [app/rag.py](../app/rag.py), [app/config.py](../app/config.py).

---

## 0. Model roster and timeouts (VERIFIED: `config.py`, `llm.py`)

| Role | Setting | Model (served name) | Timeout | max_tokens | Guided decoding |
|---|---|---|---|---|---|
| Classifier (intent, scheme, entity spans, follow-up rewrite, summary) | `CLASSIFIER_MODEL` | `qwen4-deploy` (Qwen3-4B-Instruct) | 30 s | 200 | `guided_json` |
| SQL generation + repair | `SQL_GENERATION_MODEL` | `qwen-model` (Qwen3-Coder-30B FP8) | 30 s | 1024 | `guided_regex` `\s*(SELECT\|WITH\|select\|with)[\s\S]*` |
| SQL semantic verifier | `SQL_VERIFY_MODEL` | `qwen4-deploy` | 15 s | 200 | `guided_json` |
| Answer composer (DATA + RAG) | `RESPONSE_MODEL` | `qwen35-9b` | 20 s | 800 | none |
| Embeddings | `EMBEDDING_PROVIDER=local` | fastembed `BAAI/bge-small-en-v1.5` (384-dim, CPU) | – | – | – |
| Reranker | `RERANKER_ENABLED=False` | `qwen3-reranker` (deployed, **disabled**: it inverted relevance, 2026-08-29) | 30 s | – | – |
| ASR | `ASR_MODEL` | `qwen3-asr` | 60 s | – | `prompt` = `ASR_PROMPT` vocabulary hint |

- **Temperature and message shape:**
  - All temperatures are 0.0.
  - Every chat call sends **one `user` message, with no system prompt**.
  - Every call sets `chat_template_kwargs.enable_thinking=False`.
- **Retries** (all VERIFIED). The complete list:
  1. **`call_model`:** guided→unguided, once, on HTTP 400/404/422. There is **no retry on a
     timeout or a 5xx**.
  2. **SQL:** up to 3 repair calls (§2.8).
  3. **DATA composer:** one strict retry on a misquoted or dropped number (§2.9).
  4. **RAG composer:** one retry when the first answer is an outright refusal (§4).
- **Concurrency:**
  - Every call waits on the per-worker semaphore (`MODEL_MAX_CONCURRENCY=24`).
  - A wait longer than 20 s raises `ModelBusyError`, which the router turns into a 503.
- **Request ceiling:** `REQUEST_TIMEOUT_SECONDS=60` covers the whole `answer_question` call.
- **Dead setting:** `SQL_GENERATION_MAX_RETRIES=1` is never read. The repair budget is hardcoded
  as `max_repairs=3` (KI-008).

---

## 1. Top-level control flow — `_run_pipeline(question, session, scope)` (VERIFIED)

Called by `answer_question`, which then runs `_attach_followups`, `context_manager.update_state`
and `maybe_update_summary` (`_update_context`), all best-effort.

**Stages (2026-10-02, D-032).** The steps below are grouped, verbatim, into stage functions, run
in order by the sequential driver `_run_pipeline`, or as nodes by the LangGraph orchestrator (§8):
- `_turn_context_stage`: a–c'';
- `_turn_edge_stage`: d–g;
- `_turn_followup_stage`: reference substitution, rewrite, edge re-check, hints;
- `_turn_route_stage`: not-held checks, village search, intent;
- `_turn_knowledge_stage`: the KNOWLEDGE route;
- `_turn_data_context_stage`: `prior_resolved`.

The DATA path (§2) is `_data_scheme_stage` → `_data_entity_stage` →
`_data_clarification_stage` → `_data_deterministic_stage` → `_data_sql_stage` →
`_data_authorize_stage` → execution → `_data_post_rows_stage` → `_data_compose_stage` →
`_data_guarantees_stage` → `_data_assemble`. Change behaviour inside a stage, never in a graph
node. The order matters: many steps exist to run
*before* a later step that would otherwise misroute.

| # | Step | Function(s) | Model? | Returns early with |
|---|---|---|---|---|
| a | Spelling fix of scheme names | `_correct_scheme_spelling` | – | – |
| b | Pin bare "CM Elevate" to a dataset | `_pin_cm_elevate_dataset` | – | – |
| c | **Resume a paused clarification**: merge `"<paused q>, <reply>"` unless the reply is a chip (full question), conversational, or a new question (`_reply_abandons_scope_pause`) | reads `session.pending_scope_q` | – | – |
| c' | **Resume a scheme pause** (`SCHEME_PAUSE_RULES`): a typed scheme name becomes that chip's question; anything else passes through unchanged (§5.2) | `_resume_scheme_pause`, reads `pending_scope_rule/options` | – | – |
| c'' | **Continuation gate** (2026-09-29, KI-130): does the message share anything with the previous turn? `has_context` = antecedent AND a signal (or a pause resume). Without one: edge whitelist applies, no rewrite, no KNOWLEDGE scheme fallback (§5.7) | `context_policy.continuation_signals` | – | – |
| d | Harmful-request refusal | `edge.detect_harmful` | – | `edge` |
| e | "What is EKH?" district/block definition | `_geo_definition_answer` | – | `knowledge` |
| f | Why-choice, scheme fit, recommendation, pick, listing, comparison | `_why_choice_answer`, `_scheme_fit_check_answer`, `_scheme_recommendation_answer`, `_scheme_pick_answer`, `_scheme_listing_answer`, `_scheme_comparison_answer` | – | `knowledge`/`edge` |
| g | **Edge layer** (skipped on a resume). Includes `personal_request` ("give me five thousand loan", KI-131) | `edge.detect_edge_case(q, has_context)` | – | `edge` |
| h | Deterministic references ("previous year", "the former") | `context_manager.substitute_references` | – | clarification if ambiguous |
| i | **Follow-up rewrite** when `looks_like_followup` and the previous turn was `data`/`knowledge`. Structured previous-turn evidence plus a provenance check (§5.1) | `context_manager.build_followup_context` → `rewrite_followup` | **4B** (+ optional Qdrant memory) | `edge` "confused" if there is no antecedent and a bare pronoun |
| j | Edge re-check on the rewrite; inject the session's scheme hint | `edge.detect_edge_case`, `context_manager.inject_scheme_hint` | – | `edge` |
| k | Pre-route refusals: bank details, admin expenditure, unsupported named scheme | `_bank_clarification`, `_admin_expenditure_clarification`, `_unsupported_scheme_clarification` | – | clarification |
| k' | **Village name search** ("how many villages are named X?", "which villages contain X in their name?"), before routing and before any entity is resolved (KI-135) | `_village_name_search_answer` (scheme catalogue or `dim_geography`, parameter-bound) | – | `data` |
| l | **Intent** | `classify_intent` (regex fast path; since 2026-09-28 also `_APPLICANT_PLACE_CUE`: an applicant / application noun scoped to a place → DATA unless a knowledge word is present, KI-076) | regex fast path, else **4B** | – |
| m | KNOWLEDGE → RAG (§4) | `rag.answer_from_kb` / `answer_from_kb_multi` | embed + **9B** | `knowledge` |
| n | DATA → `_answer_data` (§2) | | several | `data` / `denied` / clarification |
| o | Exceptions from DATA | see §3.4 | | |

**Clarification (Flow B)** is an exception, not a return value: `raise ClarificationNeeded(question, options, rule)`.
1. The router turns it into `route:"clarification"` with one-tap `options`.
2. For the rules `scope-not-specified`, `year-not-specified`, `entity-ambiguous` and
   `ranking-count-not-specified`, the router stores `session.pending_scope_q` (and
   `pending_village_hint`, `pending_scope_rule`) **in process**. For `scheme-not-specified` and
   `focus-scheme-ambiguous` it also stores the options, and those resume at step c' instead
   (§5.2).
3. On the next turn, step **c** merges the reply into the paused question and runs `_answer_data`
   with `skip_scope_clarify=True`, so the same gate cannot re-ask.
4. If a chip sends a full rewritten question, it passes through unmerged.
5. **This state is not persisted**, so a resume that lands on another worker is lost (KI-001).
6. `scope-not-specified` (`_scope_clarification`) offers one chip per district plus a last
   **"All of Meghalaya"** chip (area only, since 2026-09-26; it was "All of Meghalaya, all
   years"). Picking it leaves the year open, so `year-not-specified` asks for the FY next, the
   same as a district chip. VERIFIED.

Rules raised in code (VERIFIED by grep): `scheme-not-specified`, `focus-scheme-ambiguous`,
`cm-scheme-group-ambiguous`, `sericulture-spelling-ambiguous`, `ranking-count-not-specified`,
`region-needs-district`, `scope-not-specified`, `year-not-specified`, `year-out-of-range`,
`no-time-dimension`, `tranche-not-specified`, `entity-ambiguous`, `ac-narrow-scope`,
`column-not-held`, `scheme-not-available`, `scheme-comparison-not-specified`,
`recommendation-needs-profile`, and since 2026-09-29 `reference-ambiguous` (a bare "which
one?"; not remembered) and `amount-not-held` (a Focus Plus amount it never pays). The WHK
near-miss pause uses `entity-ambiguous`.

---

## 2. DATA path — `_answer_data(question, scope, skip_scope_clarify, prior_resolved, village_hint)` (VERIFIED)

### 2.1 Pre-classification gates (no model call)
In order:
1. `_unsupported_scheme_named` → a clarification naming the scheme we don't hold.
2. `_wants_cross_scheme_money_ranking` → `_cross_scheme_money_answer`: deterministic SQL across
   every money-bearing scheme. **Returns.**
3. `_is_ambiguous_focus` → the "which Focus?" two-way ask.
4. `_needs_scheme_clarification` (`SCHEME_CLARIFY_ENABLED`) → the which-scheme chips.
5. `_needs_topn_clarification` (`TOPN_CLARIFY_ENABLED`) → the Top 3/5/10/all chips.

### 2.2 Scheme classification — `classify_scheme`
- **Source:** `pipeline.py`. **Input:** the question. **Output:** `list[str]` of canonical
  scheme names.
- **Order:**
  1. The deterministic `_shortcut_scheme` (named schemes and scheme-only vocabulary).
  2. Otherwise the **4B** with the `SCHEME_CATALOG` descriptions and `guided_json`.
- **Failure:** unusable output → **returns every scheme** (KI-011).
- **Overrides after classification:**
  - A producer-group name question that names no scheme → `["Focus Legacy"]`.
  - For Focus Legacy, `_focus_legacy_pg_name_answer` answers group-name lookups
    **deterministically** (whole-word match, then prefix match) and returns. Since 2026-09-29
    (KI-149) `_PG_NAMED_ENTITY` accepts any non-space name word, `_pg_rest_is_place` decides
    whether words after the name are a place, and a size question whose name holds a place word
    but no group word comes back as `size?` — answered only when a group has exactly that name.
  - An assembly constituency alone no longer pins MGNREGA (`_mgnrega_terms`, KI-148); with no
    scheme implied, `_scheme_clarification` offers only the three AC-capable schemes.
  - Edge (`edge.detect_edge_case`, step 0b): a labelled producer-group name is masked from the
    out-of-area test (`_mask_group_name`, KI-152), so "Rakkam China Banana Group" is not refused.
  - Resolution, explicit "X village": the whole `_VILLAGE_PHRASE_RE` phrase from the text
    replaces an extractor fragment when the DB holds it exactly (KI-153; all village schemes).
  - Focus Legacy id/name-only group lists are written by `_focus_legacy_group_list_answer`
    (KI-154, D-031).
  - Village-disambiguation chips are pinned for Focus Legacy too (`_mgnrega_village_chip_pin`,
    KI-156), including after a later year chip (`_CHIP_TAIL_PARSE_RE` allows the period tail).
  - Group-name lookup (`_focus_legacy_pg_name_answer`) searches current names AND earlier spellings
    (`curated.v_focus_legacy_pg_search`, `NOT is_current`) in one pass, compares with "_" as a
    space, and keeps non-ASCII letters (KI-020, KI-155, KI-158). A "Focus" inside a group name
    does not raise "which Focus?" (`_pg_focus_group_name`, KI-157).
  - Focus Legacy village candidates: `_fl_expand_twins` adds same-block twins holding Focus
    Legacy groups, `_fl_rank_villages` lists villages with Focus Legacy rows first, twins get
    LGD-coded chips (KI-160, KI-163). Region questions stand down once a village is resolved
    (Focus Legacy in `_mg_place_settled`, KI-162).
  - SQL guards in `execute_with_repair` (Focus Legacy): `_focus_legacy_village_code_only` drops
    block/district literals beside a resolved village_code (KI-159);
    `_focus_legacy_date_trunc_as_date` casts `DATE_TRUNC(...)` to `::date` so months are not
    shifted by the IST→UTC conversion (KI-164). The verifier's "district dropped" complaint beside
    a pinned village_code is discarded (`_verifier_village_code_complaint_is_false`, KI-161).
  - For CM Elevate Legacy, `_cm_legacy_not_held` raises a "not held" explanation.

### 2.3 Entity resolution — `resolve_entities(question, schemes, prior_resolved, village_hint)`
- **Source:** `pipeline.py` + `entity_resolver.py`.
- **Output:**
  - `{"resolved": {...}}`: SQL-ready values (e.g. `district: 'EAST KHASI HILLS'`,
    `village_code`, `year_key`, `assembly_constituency`, `tranche`, `district_list`, …);
  - `"display"`, `"notes"`;
  - optionally `"question"`, a year-gap rewrite that replaces the question downstream.
- **Steps:**
  1. **Year guards:** `_out_of_range_year_in`, `_years_in_question` and `_apply_year_gap`.
     - If only unavailable years are named → a `year-out-of-range` pause.
     - A comparison that names one absent year → the absent year is **substituted** with the
       nearest year that has data.
     - Otherwise the absent year is stripped, and a note says so.
  2. **Span extraction:** `extract_entity_mentions` (**4B**, `guided_json`). It returns verbatim
     `district/block/village/year/assembly_constituency/blocks/districts` spans. Spans not found
     in the question are dropped (`_mention_in_question`), and so are producer-group names
     (`_drop_producer_group_names`).
  3. **Deterministic resolution** per dimension, via `entity_resolver.resolve_dimension`:
     - district, block, year, tranche and sub-scheme come from the per-scheme YAML catalogue
       (exact → alias → fuzzy, with blocked pairs), plus a cross-scheme fallback;
     - villages come from `resolve_village` [DB]: exact match on name or alias, else `pg_trgm`
       `similarity > 0.4`, and duplicates are collapsed by activity counts across the 7 views.
  4. **Collisions:**
     - a name that is several levels at once (`collides_across_dimensions`) → an
       `entity-ambiguous` chip set;
     - the assembly-constituency arm is kept only for `_AC_CAPABLE_SCHEMES = ("MGNREGA",
       "Focus Legacy", "CM Elevate Legacy")`. The whole-or-part drill-down reads the asking
       scheme's own rows (`constituency_contents(ac, scheme)`, KI-146).
     - **MGNREGA only (2026-09-26, KI-041):** `_explicit_level_in(question, schemes)` ignores
       a NEGATED level (`_NEGATED_LEVEL_RE`, "not the village").
       - The block/village chip sends "…, the block, not the village". The finest-first
         detector used to read that as "village", so MAWKYRWAT resolved to its village
         (273 households) instead of the block (11,590).
       - Other schemes keep the old reading, because the user asked they stay unchanged.
         The same chip text is generated for them, so the bug remains there.
     - **MGNREGA only (KI-042):** a district mention that is not a district or a village, but
       is EXACTLY an MGNREGA assembly-constituency name (`_mgnrega_ac_reading`), is re-read as
       the constituency before `OutOfScope`. An example is "SOUTH TURA": with only one reading,
       the collision gate stays silent.
- **MGNREGA village and block resolution hardening (2026-09-26, all-blocks / all-villages run;
  VERIFIED live).** These run in this order inside `resolve_entities`:
  1. **Village-chip resume.** When `village_hint` is set and the question ends with the chip
     tail ", <BLOCK> block, <DISTRICT>":
     - the tail's "block" is not a stated level;
     - `_mgnrega_village_chip_pin` pins the single village from the chip text (the name in the
       stem, looked up in `dim_geography` within that block and district);
     - only `village_code` goes to SQL;
     - the collision gate is skipped (`_chip_pinned`).
  2. **Truncated qualified names.** A longer exact village name in the question replaces the
     extracted mention ("JONGCHIPARA (NOKAT)").
  3. **Bare block or AC names tagged as a village.** `_mgnrega_admin_reading_of_village_mention`
     re-reads the name as the block or the AC, or asks which one when it is both. It runs only
     when the name is not an exact village name. It is needed because the collision gate ends on
     a district hit, and a block named after its district HQ town (MAIRANG, MAWKYRWAT) counts as
     one.
  4. **Chip caps.** Village chip lists show up to 10 names for MGNREGA (`_vcap`).
  5. **Special-word and punctuated names** (KI-057, KI-058):
     - a level word inside an exact village name ("BLOCK CAMPUS") is not a stated level;
     - `_mgnrega_longest_village_in` finds "&" / "INCL" / qualified names whole;
     - a village the extractor put in the block slot moves to the village slot;
     - `_resolve_village_for` prefers a village's own exact name over alias hits and drops
       "Unresolved" placeholders.
- **Before routing (MGNREGA or unscoped questions).** `_mgnrega_village_not_out_of_area`
  overrides the edge out-of-area refusal when an exact village name carries the place word
  (COAL INDIA COLONY). A bare MANIPUR / BURMA asks "village or the place outside Meghalaya?".
- **In `_answer_data`,** scheme detection runs on `_mask_scheme_words_in_village_name(question)`,
  so a village called "UMRAN DAIRY" is not read as the CM Elevate dairy scheme.
- In `_answer_data`, the Garo/Khasi range prompt is skipped once a village, block or AC is
  resolved (`_mg_place_settled`).
- **MGNREGA women employment (2026-09-27, KI-077).** FY 2025-26 women data is unrecorded at
  source, and that is handled as follows:
  - The FY pause also fires for percentage, share and women questions. Women questions offer
    only the years that carry women data (`_mgnrega_year_women`, read live).
  - A single-area women count or share is a deterministic query over the recorded years
    (`_mgnrega_women_query`). The answer is guaranteed to state the years it covers.
  - Explicit FY 2025-26 gets "not recorded" plus the latest year's figure.
  - Generator SQL is restricted to the recorded years when it reads women data with no
    `year_key` (`_mgnrega_women_years_only`, in `execute_with_repair`).
- **Calendar dates are not financial years (2026-09-27, KI-078).** "sanctioned on 2017-11-28" is
  a date filter:
  - `_EXPLICIT_FY_RANGE_RE` (the FY back-fill) skips an `NNNN-NN` that continues as a date;
  - `premise_check.extract_premises` blanks full dates, so their parts are never "assumed
    figures";
  - a full date satisfies the scope gate (`_CALENDAR_DATE_RE` in `_needs_scope_clarification`).
- **Failure behaviour:**
  - `ClarificationNeeded` when a name is ambiguous;
  - `OutOfScope` when a place is outside Meghalaya (`OUT_OF_SCOPE_GUARD_ENABLED`);
  - otherwise a not-found note is added.

#### Village-grained schemes (all six since 2026-10-02, D-034) — VERIFIED 2026-10-02
`_village_scheme(schemes)` is set when the question is about MGNREGA (first scheme), or about exactly
one of Focus Plus, PMAY-G, CM Elevate, Focus Legacy or CM Elevate Legacy (`_VILLAGE_NARROW_SCHEMES`;
the two Legacy schemes joined 2026-10-02, KI-190, with `_focus_legacy_village_names` /
`_cm_legacy_village_names` in `_scheme_village_names`).
Those are the facts with `village_code` on every row. It turns on these guards, which were
MGNREGA-only until the Focus Plus all-villages run (D-028, KI-079 to KI-083):
- the negated-level chip reading and the chip-tail level fix;
- the chip pin: `_mgnrega_village_chip_pin` reads the chip's own ", X block, DISTRICT" tail, or its
  "(LGD code)";
- the level-word-in-name fix;
- the whole-name village lookup: `_mgnrega_longest_village_in(q, scheme)`, over `v_employment` /
  `v_expenditure` or `_focusplus_village_names()` (`v_focus_plus`);
- the truncated-mention widening, the admin re-read, the "Unresolved" placeholder drop, and 10
  chips instead of 5.

Focus Plus only:
- `_focusplus_narrow_village` prefers villages that hold Focus Plus data;
- `_focusplus_alias_district_collision` asks when a district HQ alias ("BAGHMARA") is also a
  village;
- "Nan" resolves as the stored blank-block value.

Twin villages (same name, same block): the label and the chip question carry "(LGD code)"
(`_village_code_tag`).

SQL side (`execute_with_repair`, every attempt): MGNREGA uses `_mgnrega_pin_village_code` and
`_mgnrega_drop_geo_beside_village`. Focus Plus uses `_focusplus_pin_village_where`, which rebuilds
the WHERE of a single-SELECT query around the one resolved `village_code`:
- it drops every place condition (block_name_raw, lgd_*, district_name_raw, lgd_village_name,
  entity_type);
- it keeps the year, tranche and status conditions;
- it adds the code when the WHERE was missing it.
- it drops a condition whose literal is a Focus Plus block or district name, whatever the column
  (`tranche_label ILIKE '%GASUAPARA%'`);
- it replaces a `geography_key IN (SELECT …)` subquery with the code.

In `compose_response`, a Focus Plus single-figure result must be stated in the answer. The
misquote check skips whole numbers under 100, so "1" for 11 had passed. The fallback is a strict
retry, then the deterministic answer.

**CM Elevate additions (2026-09-28, all-blocks / all-villages run, KI-106 to KI-110):**
- CM Elevate is a village-grained scheme: `_VILLAGE_FACT_SCHEMES` and `_VILLAGE_NARROW_SCHEMES`
  include it, so `_village_scheme(["CM Elevate"])` turns on every guard above.
  - Its village catalogue is `_cm_elevate_village_names()` (`v_cm_elevate`, "Unresolved" rows
    excluded).
  - `_focusplus_narrow_village`, the alias-district collision and the village-beside-block step
    (`_pmay_village_beside_block(..., scheme)`) run for it.
- `_focusplus_pin_village_where` runs for `["Focus Plus"]` and `["CM Elevate"]`
  (`_PIN_VILLAGE_WHERE_SCHEMES`). Since KI-108 it:
  - finds the query's own WHERE with `_top_level_where_span`, never one inside
    `COUNT(*) FILTER (WHERE …)`;
  - splits conditions on AND outside string literals, so "…Empowerment and Development (SEED)"
    survives.
- **Block spelling (KI-107):** at the end of `resolve_entities`, `_cm_elevate_blocks_to_data`
  rewrites a resolved block to the `lgd_block` spelling `v_cm_elevate` stores, loaded live by
  `_cm_elevate_block_names`.
  - "X Municipal Board / Town Committee / MB / TC" → the urban body.
  - A name the data does not store → the same letters (RI-MULIANG → RI MULIANG), else X's only
    urban body (JOWAI → JOWAI-MUNICIPAL BOARD).
  - A rural block the data holds is kept (BAGHMARA).
- The one-figure rule in `compose_response` applies to `_ONE_FIGURE_SCHEMES` = Focus Plus and CM
  Elevate (KI-109). The Garo/Khasi range check is skipped once a CM Elevate village or block is
  resolved (KI-110).
- **Urban bodies (KI-113):**
  - At the start of `resolve_entities`, a mention written as an urban local body ("Jowai-municipal
    Board", "Nongpoh TC") is moved to the block slot as the stored `"<TOWN>-MUNICIPAL BOARD"` /
    `"-TOWN COMMITTEE"` block, and both block-vs-village gates leave it alone.
  - An unresolvable district or block mention is checked with `_cm_elevate_block_from_mention`
    before `OutOfScope`.
  - In the collision gate, a name the data stores as a block (`Mawhati`, `Siju`) is always a
    block reading, so the chips offer it.
  - The block spelling step now also runs BEFORE the village-beside-block step, whose block set
    includes the stored spellings, so "<village>, Nongpoh-town Committee block" is the village.
- **SQL guards added to `execute_with_repair`:**
  - `_cm_elevate_fix_literals` (KI-112) snaps a programme / block / district literal that
    matches no stored value to the one stored value it clearly names.
  - `_cm_elevate_sector_all_programmes` (KI-111) drops an invented programme filter (and its
    grouping) from a sector question that names no programme.
- **Village chip pin (KI-114):** `_mgnrega_village_chip_pin(question, scheme)` keeps the
  same-block twin the asking scheme holds (MGNREGA passes its scheme and is unaffected).
- **Status = current_file_status (KI-123, product decision 2026-09-28):** schema_context rule 13 and the
  vocabulary now send "status" / "status distribution" / "status-wise" / "application status" to
  `current_file_status`, merging the send-back spellings. Two few-shots were rewritten to match.
  `_cm_elevate_status_is_file_status` (in `execute_with_repair`) rewrites a `data_verified` GROUP BY to that
  expression for a plain status question. Verification / pending / on hold / approved / rejected / withdrawn
  wording keeps its column.
- **Verifier (KI-115):** `_verifier_scheme_specific_complaint_is_false` also discards a check-2
  complaint about a live `scheme_specific` key when the SQL already filters every resolved
  place.

**PMAY-G additions (2026-09-28):**
- `resolve_house_status` matches a stage phrase containing "sanctioned" only in order, as a
  phrase, and never as "sanctioned amount / money / funds" (KI-091: "received only part of their
  sanctioned amount" had resolved to House Sanctioned).
- PMAY-G is a village-grained scheme (`_village_scheme`, D-029): village resolution prefers the
  villages in `v_pmay` (`_pmay_village_names`), and the chip / level / longest-name guards apply.
- `_has_block_word` accepts "blocks" ("compare X and Y blocks" no longer asks block or village).
- **Village beside its block (KI-098, user report):** at the end of `resolve_entities`, when no village
  was resolved but a block / district was (in any slot, or in an explicit ", <X> block, <DISTRICT>"
  tail read from the text), `_pmay_village_beside_block` looks for an exact PMAY-G village name
  outside the block / district mention and inside that block; if exactly one, `village_code` replaces
  the block / district. One mention written "X block" stays the block (the 2026-09-18 rule).
- **Full scenario run (KI-099 to KI-105):** the hill-range check is skipped once a PMAY-G village / block is resolved
  (`_mg_place_settled`); stage phrases with old / house / site / new / sanctioned match only as a phrase; ", X block" is
  read anywhere in the text; a village that is only the stated block's namesake yields to the longer exact name;
  `_pmay_two_villages` turns "A or B" into a village list; model-path answers get `_pmay_fy_labels` (year_key → FY) and
  `_pmay_crore_to_rupees` (no 2-decimal crore; exact rupees, formatted by `_pmay_rupee_format`).
- **Bare year (KI-125, revised 2026-09-29):** a plain four-digit year (`_PMAY_BARE_YEAR_RE`; not "FY 2017" or
  "2017-18") is the CALENDAR year: the data path runs `_pmay_facts_query(..., calendar_year=y)`
  (sanction_date 1 Jan – 31 Dec) and returns ITS answer, result table and SQL; the FY y-(y+1) figures
  follow as a one-line note. An explicit FY is unchanged. A year is never dropped.
- **Only what was asked (KI-128):** summaries list exactly the use-case figures (financial summary = sanctioned,
  released, remaining; performance = beneficiaries, sanctioned, released, completed); single figures carry no side
  details. For a rewritten follow-up, `_run_pipeline` sets `_PMAY_TYPED_TURN` to the typed text and
  `_pmay_facts_query` keeps only the figures the user typed when they are a subset of the rewrite's (a typed
  follow-up with no figure, e.g. 'and in East Khasi Hills?', keeps the rewrite's).
- **Result table (KI-129):** the facts path returns `_pmay_display_rows` (place + asked figures, readable
  column names) as `rows` / `data`, not its 30-column working row.

### 2.4 Post-resolution gates
1. **Region:** `detect_region` handles "Garo Hills", which is a range, not a district. It either
   expands to the range's districts or asks (`region-needs-district`).
2. **Focus Plus overall summary:** `_focusplus_wants_overall_summary` → a deterministic answer.
3. **Scope gate:** `_needs_scope_clarification` fires on an aggregate with no place **and** no
   year. It is skipped on a resume.
4. **Year gate:** `_needs_year_clarification` fires when the place is fixed but the year isn't.
   The chips come from `_SCHEME_DATA_YEARS`, which `refresh_scheme_years` refreshes from the DB
   at startup.
5. **Tranche gate:** `_needs_tranche_clarification` (Focus Plus only).
6. **Tranche conflict:** `_person_level_tranche_conflict` (Focus Plus; person-level columns exist
   only on Tranch 4) → a deterministic explanation.

### 2.4a PMAY-G deterministic facts path (2026-09-28, D-029, KI-089 to KI-097)
Runs after the gates in §2.4, before SQL generation, only for `schemes == ["PMAY-G"]`:
1. `_pmay_facts_query(question, schemes, resolved, notes)` returns None unless every resolved
   key is one it handles (`_PMAY_DET_KEYS`), the question is one of the fixed shapes
   (`_pmay_metrics`; `_PMAY_DET_EXCLUDE_RE` sends rankings, "each", trends, allotment,
   instalments, averages, lists and "in progress" to the model path) and, for a statewide answer,
   no place was left unresolved (`_PMAY_UNSETTLED_PLACE_RE`, resolver notes).
2. The query is parameter-bound over `curated.v_pmay` (`NOT is_placeholder`; `COALESCE(amount_released, 0)`
   — 336 houses have a NULL release), one row per named area: houses, sanctioned, released,
   stage counts, full / partial / no release, fully-paid-but-incomplete (+ stage split), unique
   sanction numbers. A date (`_pmay_date`: ISO, dd/mm/yyyy, written) or `year_key` filters it.
3. `_pmay_facts_answer` writes the answer: exact ₹ with Indian grouping plus lakh / crore
   (`_pmay_money`); for 2 areas the higher one and the difference per metric; for more, the
   highest. An area with no houses returns None → model path, except a village resolved by code
   → `_pmay_no_houses_answer` (a stated zero, with its placeholder count).
4. The result is returned with `sql` = the literal-substituted query, like the MGNREGA fixed shapes.

Model-path PMAY-G guards: `_pmay_sql_issue` (repair) and `_pmay_comparison_limit` (rewrite) in
`execute_with_repair`; `_pmay_rupee_format` after the composer; `_PMAY_RULES` 7–9 and three
few-shots in `pmay_few_shot.yaml`.

### 2.5 Prompt assembly — `prompt_builder.build_sql_prompt(question, schemes, entity_result)`
The prompt is concatenated in this order:

```
build_schema_context(schemes)       # schema_context.py: _PREAMBLE, _SHARED_TABLES, per-scheme
                                    #   (_X_TABLES, _X_RULES, _X_VOCAB), _CROSS_SCHEME if >1 scheme,
                                    #   _SHARED_RULES, _CLOSING
_live_schema_block(schemes)         # schema_introspect: real columns + real FKs from megh_db
schema_introspect.catalog_block()   # semantic.table_catalog / glossary / metric_definitions
_prohibited_block(schemes)          # annotations: PROHIBITED JOINS from *_foreign_key_augmentation.yaml
_fewshot_block(...)                 # top_k=5 examples PER SCHEME (incl. UNANSWERABLE, sql=null)
_common_mistakes_block(schemes)     # WRONG → RIGHT shapes
_entities_block(entity_result, …)   # "RESOLVED ENTITIES — MANDATORY" + "NOT FOUND" lines
"The user's question is about: …"
'Question: "…"\nSQL:'
```

- **Few-shot ranking:** `annotations.few_shot_examples` ranks lexically by IDF. Tokens found in
  ≥25% of a scheme's pool are weighted 0.05. The ranking text is expanded with the resolved names
  (`_fewshot_ranking_text`).
- **Pool files:** `data/<scheme>/*few_shot*.yaml`. CM Elevate Legacy uses
  `cmelevatelegacy_prompt_few_shots.yaml`.
- **Size:** measured offline 2026-09-24 without the live block (TECHNICAL_BRIEF §2.2): about
  3–7K tokens for one scheme and about 32K tokens for all six.
- **If the live schema load failed at startup**, `_live_schema_block` is empty and the prompt
  falls back to the hand-written text. The live block, the catalogue and the year list are
  loaded only at startup (DATA_MODEL §6).
- **Prompt injection (VERIFIED behaviour, risk INFERRED):**
  - The user's question is inserted verbatim into the classifier, SQL, verifier and composer
    prompts.
  - There is no instruction-stripping or input classifier. The edge layer only blocks off-topic
    and harmful *topics*.
  - A question engineered to make the generator emit a `SELECT` against `app.*` or a privacy
    table would pass `run_readonly`, and `authorize()` checks only
    scheme/geography/granularity.
  - The rows are returned to the client in `data`. See KI-004.

### 2.6 SQL generation — `generate_sql(question, schemes, entity_result)`
1. Call the **30B** with `guided_regex`.
2. `_extract_sql` strips code fences and prose.
3. `_focusplus_single_district_beneficiary_guard` post-processes the result.

**MGNREGA deterministic queries (2026-09-26, VERIFIED live).** For two MGNREGA shapes,
`_answer_data` skips the generator, the repair loop and the verifier. It builds the SQL from the
resolved single-valued filters (village_code, block, district, year_key) and runs it with
parameters bound, through `db.fetch_rows`. Authorization still runs, on a literal display copy of
the SQL.
- `_mgnrega_combined_facts_query`: "expenditure/spent AND person-days/employment" for one area.
  The query is one CTE per fact joined by `CROSS JOIN`, not the fan-out join.
  - Before this, the 30B wrote the join. The 4B verifier then flagged the correct two-CTE
    rewrite as a "prohibited join", and the repair moved to `v_district_year_summary`, which
    has no village or block column.
  - The result was a KB fallback (DATA-028) or "couldn't build a query" (DATA-029, 4 runs of 5).
  - Rankings, breakdowns, components (wages/material), ratios and constituencies stay with the
    generator (`_MG_COMBINED_EXCLUDE_RE`).
- `_mgnrega_admin_expenditure_query`: `SUM(admin_total_exp)`, answered by
  `_mgnrega_admin_expenditure_answer`. That answer states the recorded figure plus the
  "never populated at source" caveat, with the statewide non-zero count read live (D-025).
  - The pre-routing gate 1d now refuses only when the question is not MGNREGA.
  - PMAY-G and the multi-scheme wording are unchanged.

### 2.7 Authorization — `auth.authorize(scope, schemes, resolved_entities, sql)`
- Runs only when `AUTH_ENABLED` and a scope is present.
- Checks scheme, geography and granularity using regexes over the SQL.
- A deny returns `route:"denied"` and **no SQL runs**.

### 2.8 Validate → execute → repair — `execute_with_repair(..., max_repairs=MAX_SQL_REPAIRS)` (Flow C)
The budget is 4 attempts in total (the initial one plus `MAX_SQL_REPAIRS = 3` repairs). Each attempt
runs these steps in order. Since 2026-10-02 one attempt is three functions:
- the rewrites (1) are `_scheme_sql_rewrites`;
- the guards, verifier, execution and post-check (2–4) are `_execute_one_attempt`;
- the repair call is `_repair_sql`.

`execute_with_repair` loops over them. The LangGraph path runs them as its `execute_attempt` ⇄
`repair` nodes, with the same budget and the same DB / budget re-raises. **Authorization:** the first
SQL is checked by `_data_authorize_stage`, and since 2026-10-02 every repaired SQL is checked too, after
the rewrites and before it runs (`_repaired_sql_denial`; D-033, KI-183). A denial returns `_denied`.

**1. In-place rewrites:**
- `_focus_legacy_geo_columns`
- `_uppercase_geo_literals`
- `_focusplus_drop_unrequested_verification_status`
- `_cm_legacy_keep_unresolved_off_village` (CM Elevate Legacy, Focus Legacy and, since
  2026-09-27, CM Elevate: KI-068)
- **CM Elevate only (2026-09-27, use-case QA, KI-069 / 070 / 072 / 074):**
  - `_cm_elevate_split_scheme_in_list`: a `scheme_name IN (…)` with 2+ programmes and no
    `GROUP BY scheme_name` gets `scheme_name` added to the SELECT and the GROUP BY. The IN-list
    regex matches literals, since "(SEED)" carries its own parenthesis.
  - `_cm_elevate_sector_zero_groups`: a GROUP BY `sector_id` query with one
    `data_verified = '…'` in the WHERE moves that test into `COUNT(*) FILTER (…)`, so sectors
    with 0 stay listed.
  - `_cm_elevate_level_pending`: "pending at level N" drops an AND-ed `data_verified = 'On
    Hold'` when `current_level` is filtered.
  - `_cm_elevate_pending_without_level`: a plain "pending" (no level or stage named) that was
    filtered on `current_level = 'levelN'` is swapped back to `data_verified = 'On Hold'`.
  - `_cm_elevate_decision_status_column`: `current_file_status = 'approved' / 'rejected' /
    'pending'` (values that column never holds) → `scheme_specific ->> 'file_status' = 'Approved'`
    and so on.
- `_cm_legacy_qualify_shared_geo_cols`
- **CM Elevate Legacy only (2026-09-29, use-case re-test + all-levels run):**
  - `_cm_legacy_sanctioned_count` (KI-166): a "sanctioned applications" count written as
    `COUNT(*) AS sanctioned…` becomes `COUNT(sanctioned_amount)`; a sanctioned question whose SQL
    has no sanction column gets `COUNT(sanctioned_amount) AS sanctioned_records`. Not / de- /
    non-sanctioned aliases and FILTER forms are left alone.
  - `_cm_legacy_unrequested_limit` (KI-168): a trailing LIMIT on a grouped result is dropped
    unless the question states a count (a singular "which scheme has" keeps LIMIT 1).
  - `_focus_legacy_village_code_only` now also covers CM Elevate Legacy and drops a
    `lgd_village_name` literal beside a pinned `village_code` (KI-173).
- `_mgnrega_numeric_division` (MGNREGA only, 2026-09-26, KI-044): casts the numerator of
  `SUM(<integer employment measure>) / …` to `numeric`, so person-days per household reads
  38.16, not the integer-truncated 38.00.
- `_mgnrega_lakh_not_divided` (MGNREGA only, 2026-09-26): removes a `÷100` applied to
  MGNREGA money whose alias still says `…lakh`. In the DATA-023 run, 219.48 lakh of wages was
  printed as "2.19 lakh". `_crore_conversion_for_single_village` only fires when the SQL says
  "crore".

- `_mgnrega_pin_village_code` (MGNREGA): when one village was resolved and the SQL filters a
  single different `village_code`, or an IN-list, the resolved code is substituted (AMPATIGRI,
  MAWKOHMIT & MAWKYNSAH).
- `_mgnrega_drop_geo_beside_village` (MGNREGA): removes `lgd_block` / `lgd_district` literals
  next to the resolved `village_code`. In the MAWLIEH case the generator added the wrong
  district.

**2. Deterministic guards.** Each one raises a `ValueError` whose message becomes the repair
instruction:

| Guard | Catches |
|---|---|
| `_focusplus_stated_amount_missing` (Focus Plus, KI-132, 2026-09-29) | a stated payment amount ("a loan of five thousand", "₹2,500 payments") with no `amount_disbursed = N` filter |
| `_resolved_scope_missing` (all schemes, KI-034, 2026-09-29) | a resolved district / block / comparison list / year_key absent from the SQL. Not required: geography beside a resolved village_code, a hill-range expansion, a year for CM Elevate or an all-years question; LIKE on the distinctive word counts |
| `_mgnrega_village_filter_missing` (MGNREGA, KI-054) | a village was resolved but the SQL filters no village (it would return a block or statewide total) |
| `_village_filter_missing` (every single-scheme question, KI-188, 2026-10-02) | the same for the other five schemes. Its deterministic half, `_pin_missing_village_code` (in `_scheme_sql_rewrites`), first rewrites a simple one-SELECT query: adds `village_code = <code>`, or replaces a lone `lgd_village_name` text condition with the code (`_village_name_cond_to_code`, KI-197), and drops a block / district literal beside it in any UPPER / TRIM form (`_drop_geo_beside_pinned_village`, KI-196). D-034 |
| `_mgnrega_village_list_issue` (MGNREGA, KI-045) | "which villages …" SQL with no `lgd_village_name`, or "received employment" without `HAVING SUM(persons_employed) > 0` |
| `_mgnrega_comparison_without_figures` (MGNREGA, KI-047; checked on the result, after execution) | a comparison ("more on wages or materials?") whose rows hold no number, only a CASE label |
| `_focus_legacy_group_size_summed` | SUM of `no_of_pg_members` for a group-size question (must be MAX per `pg_id`) |
| `_village_name_filter_instead_of_code` | filters `lgd_village_name` when a `village_code` was resolved |
| `_village_filtered_at_wrong_level` | a village name placed in `lgd_block`/`lgd_district` |
| `_village_code_as_geography_key` | `geography_key = <village_code>` |
| `_crore_conversion_for_single_village` | MGNREGA lakh÷100 for one village (shows "0.00 crore") |
| `_mgnrega_facts_joined` | `v_employment` JOIN `v_expenditure` (row fan-out) |
| `_ac_with_invented_geo_filter` | an AC filter ANDed with an invented block or district |
| `_STATE_PSEUDO_FILTER` | a `'Meghalaya'` / `entity_type='State'` filter |
| `_rowgrain_no_aggregate` | a bare row read on a row-grain view for an aggregate question |

**3. Semantic verifier:** `_verify_sql` (**4B**, `build_verify_prompt`, `guided_json {ok, issue}`).
- It judges the SQL against the same schema, the resolved entities and the calibration examples.
- **Failure-open:** any error means "no issue".
- **False-positive filters** discard known-bad complaints:
  - `_VERIFIER_FALSE_EMPTY_ENTITIES`
  - `_verifier_complaint_is_cosmetic`
  - `_verifier_year_complaint_is_false`
  - `_verifier_apostrophe_complaint_is_false`
  - `_verifier_missing_geo_is_false`
  - `_verifier_wants_suppressed_geography`
  - `_verifier_check2_on_empty_entities`
  - `_verifier_join_complaint_is_false`
  - `_verifier_scheme_specific_complaint_is_false` (CM Elevate, 2026-09-27): the 4B model said
    `v_cm_elevate` has no `scheme_specific` column. The complaint is discarded when the SQL reads
    only the live-verified keys (`file_status`, `sector_id`, `gender_id`, `occupation_id`).
  - `_verifier_village_code_complaint_is_false` (MGNREGA)

**4. `db.run_readonly(sql)` [DB]:** the safety check, auto-`LIMIT 1000`, a 15 s statement
timeout.

**5. On any exception:** `build_repair_prompt` (the same context **without few-shot examples**,
plus the error and `_repair_hint`) → **30B** → `_extract_sql` → loop.

**6. When the budget is spent**, the exception propagates. See §3.4.

**The catch is `except (UnsafeSQLError, Exception)`, so *every* failure inside an attempt
triggers an LLM repair**, not only SQL errors (VERIFIED). That includes:
- a DB connection error (e.g. `WinError 121`);
- a pool or connect failure;
- asyncpg's 15 s `command_timeout`, which surfaces as `asyncio.TimeoutError`;
- `UnsafeSQLError`.

What happens once the budget is spent:
- **An infrastructure error** (e.g. a DB outage) costs up to 3 pointless 30B calls. It then
  reaches `_run_pipeline`'s generic handler, and the user gets the misleading "couldn't build a
  working query" (KI-025).
- **A slow query** re-raises `asyncio.TimeoutError`, which `_run_pipeline` treats as transient.
  The router returns **504**.
- **An `UnsafeSQLError`** ends in the KB fallback. The router's own `UnsafeSQLError → 500`
  branch is therefore effectively unreachable.

What is *outside* the try: the repair call itself (`call_sql_generator` inside the `except`
block). A gateway timeout, a 5xx or a `ModelBusyError` there propagates immediately.
Separately, `_verify_sql` swallows every exception, including `ModelBusyError`. **Under load,
verification is silently skipped.**

**Gaps (KI-003):** there is no convergence check (the identical SQL can be retried 4 times), and
the failing SQL is never logged.

### 2.9a CM Elevate Legacy answer guarantees (2026-09-29)
After the composer, every CM Elevate Legacy DATA answer passes `_cm_legacy_answer_guarantees`
(deterministic; prose rules had failed under sampling):
- `_cml_single_row_sentence` replaces the raw fallback dump ("0.58 total disbursed cr.") with a
  sentence naming the place and each figure (KI-176).
- `_cml_complete_list` rebuilds a per-group COUNT list whose figures are not beside their names
  ("… respectively"), under the composer's headline (KI-174; detection shared with CM Elevate's
  `_cme_complete_list`; year and money breakdowns untouched).
- `_cml_village_breakdown` rebuilds a garbled village list or one with a false "each of the N
  villages" claim, and adds the total when it is missing (KI-177).
- `_cml_fix_each_have_counts` recomputes every "N villages each have V records" from ALL rows —
  the composer sees 40 rows and the faithfulness check ignores whole numbers < 100 (KI-167).
- `_cml_fix_place_spelling` restores the stored spelling of the filtered place and of the
  result's place labels ("Sellsella" → Selsella; KI-172).
- `_cml_exact_single_amount` re-queries the exact rupees of a one-row figure under ₹0.10 crore:
  "₹0" instead of "under ₹0.01 crore", "₹0.01 crore (₹1,25,000)" (KI-171; `_cml_inr` = Indian
  grouping).
- `_cml_unmapped_in_scope` states the records with no village code that a village list leaves
  out, re-queried from the same scope (KI-167).

Resolution changes the same day: the village chip-tail pin runs for CM Elevate Legacy (KI-169);
twin-village chips rank by the scheme's own records (`_TWIN_RANK_VIEWS`, KI-170); urban bodies map
to CM Elevate Legacy's own stored blocks (`_cm_elevate_block_names(scheme)`, KI-175); a resolved
village settles the Garo-range check (KI-178); and, for ALL schemes, a trailing "village" is
dropped from the extracted village name when only the bare name is a stored village (KI-179).

### 2.9 Result handling and composition
1. **Notes** are assembled from:
   - the resolver notes;
   - `_genuine_zero_notes`;
   - `_sector_not_tracked_notes`;
   - CM Elevate Legacy: `_cm_legacy_answer_notes`, `_cm_legacy_small_money_notes`, and
     `_cm_legacy_exact_totals`, which **re-queries exact totals in code** because the composer
     summed rounded rows wrongly. The subsidy/loan note now depends on the columns present
     (KI-176: told to "give all three" with only a total, the composer invented a split);
   - Focus Legacy: `_focus_legacy_answer_notes`, and `_focus_legacy_list_total`, which gives the
     true count of a truncated list; `_focus_legacy_unplaced_row` (KI-145), which re-runs a
     per-place breakdown without the model's `IS NOT NULL` filter to read the records that have
     no block / village / constituency. A one-place-column breakdown is then written by
     `_focus_legacy_breakdown_answer` and the composer is skipped (D-031, KI-150);
   - MGNREGA: `_mgnrega_answer_notes` (2026-09-26). It says three things:
     - a top-1 row is the statewide top, not "the only one" and not "the highest in its
       district";
     - money is in ₹ lakh, so write "lakh";
     - places are named in plain words, not `lgd_*` column names.

     These are backed deterministically after composition by `_mgnrega_top_one_wording`, which
     drops an "only district … in the result" sentence, and by `_mgnrega_money_units`, which
     adds " lakh" after a result amount stated without a unit.
   - `premise_check.check_premises` (`PREMISE_CHECK_ENABLED`).
2. **`compose_response(question, sql, rows, notes, entities, schemes, style_examples, extra_numbers)`**
   runs the **9B**:
   - **No rows (Flow D):** `_no_data_answer` is deterministic, with no model call.
   - **An all-NULL row, or all zero/NULL metrics:** the prompt adds "metrics the data DOES carry"
     (`available_metrics_text`) so the model says the metric is not tracked instead of reporting
     0.
   - **Multi-row results:** `_result_digest` supplies deterministic totals, mean, max and min
     over *all* rows. The composer sees at most the first 40 rows.
   - **Faithfulness checks:**
     - `_answer_numbers_faithful`: every number stated must appear in the result or the digest,
       within 0.5% tolerance;
     - `_answer_covers_metrics`: a single row must have every metric reported.
     - MGNREGA empty and zero results (KI-056): `_mgnrega_empty_answer` handles an all-NULL
       single row. A parameter-bound row count decides the wording:
       - "No MGNREGA … is recorded for X" when nothing matched;
       - "cannot be calculated … divided by 0" for a NULLIF ratio over existing rows.

       It skips the composer. `_mgnrega_null_zero_notes` and `_mgnrega_zero_backstop` handle one
       empty side of a combined row and genuine zeros.
     - MGNREGA exception (2026-09-26): when the question itself names the "100 days" measure,
       `100` is added to the allowed numbers. It is the measure's name, and correct sentences
       were being replaced by the row dump "3.39 completion 100 day pct.".
       `_answer_numbers_faithful` itself is unchanged, and any other wrong number is still
       caught (test `test_a_real_misquote_is_still_caught`).
     - **All schemes (2026-09-27, Focus Plus QA):**
       - **Mis-grouped digits (KI-062):** `_fix_digit_grouping` runs on the composed answer
         (and on the strict retry) before the check. The check strips commas, so "1,0263"
         passed as 10,263. A token valid in neither Western nor Indian grouping is re-grouped
         only when it has 4+ digits **and** its digits are a result value ("Tranches 1,2" is
         untouched).
       - **SQL filter literals (KI-064):** `_sql_literal_numbers(sql)` adds the numbers inside
         the executed SQL's quoted literals (`'12.5K'`, `'Tranch 4 - Feb-March'`) to the
         allowed set. They are the filter's labels. "…under the 12.5K batch" had been flagged
         as a misquote ("12.5"), and the fallback printed "31,317,500 amount raw.".
     - On failure → **one strict retry (9B)** → then `_deterministic_answer`.
   - **Hedge guard:** if the answer hedges ("not covered", "no data") over a real non-zero value
     → `_deterministic_answer` or `_deterministic_list_answer`.
3. **Post-composition guarantees:**
   - Focus Legacy (2026-09-29): `_focus_legacy_answer_guarantees` — month numbers become month
     names in the right calendar year of the FY (`_focus_legacy_month_labels`), money cells and
     their column sums become ₹ with Indian grouping (`_pmay_rupee_format(fmt=, column_totals=True)`,
     KI-147); `_focus_legacy_unplaced_guarantee` replaces the composer's own "no block" sentence
     with one exact, digit-form sentence (KI-145);
   - the Focus Legacy true-total line is prepended if the composer dropped it;
   - the year-gap note is prepended if the composer dropped it;
   - **Focus Plus (2026-09-27, KI-060/061/064): `_focusplus_answer_guarantees`**, gated on
     `schemes == ["Focus Plus"]`, in this order:
     1. `_fp_complete_list`: a per-category list (one label column, one figure, 3–15 rows)
        that leaves out any label is rebuilt from the rows ("The remaining districts show
        11,379, 11,007, …" had no district names).
     2. `_fp_breakdown_shares`: a GROUP BY status / verification / gender / occupation count
        gets "Share of the N … with a recorded X: A p%, …" plus the 12.5K-cohort note. It is
        skipped when a second label column exists, for money columns, or when the shares are
        already stated.
     3. `_fp_single_count_share`: a one-number count filtered on a person attribute gets "That
        is p% of the N beneficiaries in the 12.5K registration cohort …". Numerator and
        denominator are **parameter-bound recounts** (`COUNT(DISTINCT beneficiary_key)`) of the
        same WHERE, parsed by `_fp_parse_where`. That parser accepts only equality and
        IS NOT NULL on whitelisted columns; OR, IN, joins and subqueries → skipped. The share
        is stated **only if the recount equals the bot's own figure**. Any DB error leaves the
        answer unchanged.
     4. `_fp_comparison`: a compare / versus / "which is higher" question with a 2-row result
        gets "X is higher by D (X a vs Y b)". The metric is chosen from the question
        (beneficiaries / disbursement / payments). A "combined total" sentence equal to a+b is
        dropped. Skipped when the difference and a "higher" word are already stated.
     5. `_fp_format_numbers`: an amount equal to a result money value (or a money column's
        total) is written `₹126,197,500.00`; counts ≥ 10,000 get separators; "amount raw" →
        "amount disbursed".
   - **CM Elevate (2026-09-27, KI-068 to KI-074): `_cm_elevate_answer_guarantees`**, gated
     on `schemes == ["CM Elevate"]`, in this order:
     1. `_mgnrega_zero_backstop` (reused): a hedge over an all-zero single row → "0 … — the data
        records zero".
     2. `_cme_grouped_rows`: a result with two label columns (scheme × sector, scheme ×
        district) and up to 30 rows is rebuilt, grouped by programme with group totals, when
        some row's figure is not in a sentence naming one of its labels, or when the text says
        "respectively" or "each corresponding row".
     3. `_cme_complete_list`: only for "each / every / wise / distribution / breakdown / all …"
        questions. A one-label list with a row missing from the text is rebuilt, ending
        "Total N". A "which is highest" answer naming its top few is left alone.
     4. `_cme_multi_scheme_total`: SQL with a `scheme_name IN (…)` list gets any missing
        programme figure plus "Combined across these N programmes". The sum is exact because
        `request_id` never repeats across programmes.
     5. `_cme_comparison`: a compare cue on a programme × place result gets "Differences: …
        higher by D (a vs b)" per programme and per place. A one-label two-row result reuses
        `_fp_comparison`.
     KI-074 was decided on 2026-09-28 (product owner): "pending" = `data_verified = 'On Hold'`
     only. The interim `_cme_pending_file_status` second line was removed.
4. **Result:** `route:"data"`, `rows[:20]`, `data` = all rows, `row_count`, `sql`.

---

## 3. Other flows

### 3.1 Flow D — no data
- **Valid SQL, 0 rows** → `_no_data_answer(schemes, entities)`: one deterministic line that
  names the scope.
- **An aggregate over nothing** returns one row of NULLs, not zero rows. It is handled by the
  `_all_null` branch in `compose_response`.
- **A genuine zero** vs **not tracked:** handled by `_genuine_zero_notes` and the composer
  guidance. The user can still find the two hard to tell apart (TECHNICAL_BRIEF §4.2 F; current
  status UNKNOWN — NEEDS VERIFICATION).

### 3.2 Flow E — out of domain
Checked, in order:
1. `edge.detect_harmful` (a harmful-request refusal, first);
2. `edge.detect_edge_case`: hard off-topic (weather, markets, sport), greeting, identity,
   thanks, goodbye, profanity, confused, and a **domain whitelist** (no scheme vocabulary →
   `off_topic`);
3. unsupported named schemes (`_unsupported_scheme_named`, e.g. PM-KISAN) → a clarification that
   lists what is held;
4. out-of-Meghalaya places → `OutOfScope` → the standard "I'm Megh One AI" reply.

Edge replies carry `suggestions` (the `edge.STARTERS` chips). No model call is made.

### 3.3 Flow F — streaming
**Not implemented.** There is no `response_start`/`delta`/`complete`. Every route returns one
JSON body (VERIFIED).

### 3.4 Exception handling around DATA (in `_run_pipeline`)

| Exception | Result |
|---|---|
| `ClarificationNeeded` | re-raised → the router returns `route:"clarification"` |
| `OutOfScope` | `_out_of_scope_result` (an `edge` route) |
| `ModelBusyError`, `asyncio.TimeoutError`, `httpx.TimeoutException`, `httpx.TransportError`, HTTP ≥500 | re-raised → router 503/504/502 |
| a megh_db connection failure anywhere on the DATA path (`db.is_connection_error`: OSError / socket, asyncpg connection errors; not `TimeoutError`) | `DatabaseUnavailableError` → router **503** "Couldn't reach the Megh One data service… retry". `execute_with_repair` raises it before any repair; never the KB fallback (KI-025, 2026-09-28) |
| anything else (incl. a spent repair budget, `UnsafeSQLError`, DB connection errors, programming errors) | `_data_path_kb_fallback`: tries RAG once, else "couldn't build a working query" (`route:"data"`, `confidence:"low"`, empty `schemes`) |

The broad catch also hides programming bugs as "couldn't answer" (KI-009).

This except-chain is `_data_path_error_result` (2026-10-02). The LangGraph DATA nodes map errors
through the same function.

---

## 4. KNOWLEDGE path (RAG) (VERIFIED)

1. **Scope selection in `_run_pipeline`** picks `_kb_scheme`:
   1. exactly one scheme named;
   2. else exactly one scheme inferred from vocabulary (`_infer_scheme_from_terms`);
   3. else the previous turn's single scheme (not for a bare "Focus").
2. **Refusals and asks:**
   - a named unknown scheme → `_knowledge_not_covered_answer`;
   - a bare "Focus" → the which-Focus ask;
   - no scope at all → the which-scheme ask;
   - two or more schemes named → `rag.answer_from_kb_multi` (per-scheme retrieval).
3. **`rag.retrieve(question, scheme)`:**
   1. embed with local bge-small;
   2. `vectorstore.search` with an **exact** payload filter on `scheme`, taking `RAG_TOP_K=12`;
   3. with the reranker off, keep the top 8 candidates scoring ≥ `RAG_MIN_SCORE=0.30`. If
      **none** clear it, the top 8 are returned anyway. The confidence tiers below still apply,
      so a low-scoring set ends as "not covered".
4. **`rag.answer_from_kb`** answers by the top score:
   - ≥ `RAG_HIGH_CONFIDENCE=0.84` → the top chunk verbatim;
   - ≥ `RAG_MEDIUM_CONFIDENCE=0.55` → the **9B** composes from the kept chunks. The scoped prompt
     includes `_naming_note`, which says for example that the docs call Focus Legacy "FOCUS".
     - A stray "that isn't covered" sentence is stripped from the answer.
     - An *outright* refusal gets **one retry** with the same prompt. A second refusal → None.
     - The code comment there says the composer "runs at non-zero temperature", but
       `RESPONSE_TEMPERATURE=0.0`. That is a stale code comment, not changed here.
   - otherwise → None → a "not covered" reply.

   Focus Legacy overview questions retrieve with a rewritten `retrieval_query`.
5. **CM Elevate Legacy** has no docs of its own. `kb_scheme()` maps it to CM Elevate
   (`_KB_SCHEME_ALIAS`).
6. **KB ingest** (`kb_ingest.ingest_kb`, a background task at startup):
   - takes the 10 `_SOURCES` in `data/reference/` plus `data/web/*.md` tagged
     `<!-- scheme: X -->`;
   - chunks them by heading;
   - rebuilds the collection when it is missing, has too few points, or lacks any scheme.
   - **The collection is shared.** After editing the KB, call admin `POST /api/rag/reingest` or
     restart.

---

## 5. Conversation context (VERIFIED)

| Mechanism | Where | Model? |
|---|---|---|
| Follow-up detection | `looks_like_followup`: ≤12 words; lead words, pronouns, scheme-swap fragments; name lookups stay standalone | – |
| Follow-up rewrite | `rewrite_followup(question, prev, extra_context)`; prompt from `_build_rewrite_prompt`; output checked by `_rewrite_provenance_violation` | 4B |
| Previous-turn evidence | `context_manager.build_rewrite_evidence`: the tiers below, replacing the old `prev.answer[:300]` slice | – |
| Extra context | `context_manager.build_followup_context`: state block, summary, Qdrant memory (≤1,200 approximate tokens) | embed |
| Structured state | `ConversationState`: schemes, entities, years, comparison districts. `update_state` runs after every answer but commits only a **successful** DATA turn. Persisted as `app.conversations.context_state` | – |
| Prior entities | `merged_prior_resolved` seeds `resolve_entities` for follow-ups; tranche / sub-scheme are only taken for their own scheme | – |
| Summary | `maybe_update_summary` every 4 turns | 4B |

### 5.1 What the follow-up rewrite sees (VERIFIED, 2026-09-26)

Every model call is one stateless user message, so the rewrite is the only place earlier turns
reach a model. The SQL generator, verifier and composer see only its output, plus the
`prior_resolved` entities.

The prompt, in order:
1. The instructions. Entities may come only from the follow-up, the previous question, the
   previous filters or the Known context. A name from the previous result may be used only for
   the item the follow-up points at.
2. `extra_context`: the `Known context:` state line, the summary, and older turns from memory.
3. `PREVIOUS question: "…"`, the previous standalone question.
4. **Tier 1 — `PREVIOUS filters:`**, always. The previous turn's schemes and resolved entities,
   e.g. `scheme=Focus Plus; district=WEST GARO HILLS; year=FY 2024-25`. `village_code` is left
   out: its name is already in the previous question, and `prior_resolved` carries the code.
5. **Tier 2 — `PREVIOUS result:`**, only when `references_previous_result` matches ("the top
   one", "that block", "the second one", "each of them"). It is
   `Turn.result_summary`, a deterministic summary of the result **rows** in result order, built
   by `summarize_result` in the router (`CONTEXT_PREV_RESULT_MAX_TOKENS`, 120).
6. **Tier 3 — `PREVIOUS answer (excerpt):`**, only when the follow-up points into the result and
   there were no rows (a knowledge answer). Whole sentences within
   `CONTEXT_PREV_ANSWER_MAX_TOKENS` (120), never a mid-word cut.
7. `FOLLOW-UP: "…"`, then `Standalone question:`.

**Why the slice went:**
- Measured with the real prompt code: every follow-up carried answer text.
- A long answer's first 300 characters carried four block names (Dalu, Rongram, Tura,
  Phulbari) into "How many beneficiaries were there?", and the prompt allowed the model to use
  them.
- Anything past character 300 was invisible, even when the follow-up pointed at it.

**Provenance check:** `_rewrite_provenance_violation` runs on the model's output. It rejects a
rewrite that names a scheme, a district or a block that none of the permitted sources contains.
- The permitted sources are items 3–6, plus the follow-up and the state line.
- The summary and older turns count only when the follow-up reaches back ("earlier",
  "we discussed").
- Names are compared canonically with `entity_resolver.named_places`, so "ekh" permits "East
  Khasi Hills".
- Years are not checked, because the year change is usually the follow-up's own. Villages are
  not checked, because they are not catalogued offline.
- A cross-scheme follow-up ("all schemes") skips the scheme check.
- On a violation the original fragment goes on, and `inject_scheme_hint` plus `prior_resolved`
  still carry the scheme and filters. No extra model call is made.

**Failed turns:** a DATA result with no `sql`, no `schemes` and no `resolved_entities` is the
"couldn't build a working query" fallback. `update_state` does not commit it, so metric,
comparison, scheme and geography keep their last good values.

### 5.2 Scheme-pause resume (VERIFIED, 2026-09-26)

`scheme-not-specified` and `focus-scheme-ambiguous` (`SCHEME_PAUSE_RULES`) are now remembered
by the router. It stores `pending_scope_q`, plus `pending_scope_rule` and
`pending_scope_options`.

On the next turn, step 0a' runs `_resume_scheme_pause`:
- A reply that is just one offered scheme's name, e.g. "Focus Plus", "focus plus please", or
  "legacy" for the which-Focus pause, becomes **that chip's exact question**. It then gets the
  same `_pin_cm_elevate_dataset` pass, and `scope_resumed` stays False, as it does on the chip
  path.
- Any other reply goes through untouched, as before.

Previously a typed "Focus Plus" was answered as a new question and the paused ask was lost.

**2026-09-29 (KI-181): every chip pause resumes from a typed reply. VERIFIED live.**
- Three pause families now. `SCOPE_MERGE_RULES` (scope / year / entity / ranking count): the
  free-text reply is merged into the paused question, unchanged. `SCHEME_PAUSE_RULES`: as above.
  **Any other rule that offers options** (`swap-measure-unavailable`, `year-out-of-range`,
  `tranche-not-specified`, `region-needs-district`, `ac-narrow-scope`,
  `sericulture-spelling-ambiguous`, `cm-scheme-group-ambiguous`, `amount-not-held`,
  `village-or-outside-place`, `no-time-dimension`, `scheme-comparison-not-specified`, …): the
  router (`remember_pause`, generic branch) stores the question and the options.
- Step 0a'' runs `_resume_option_pause(reply, options)`: the chip's own question; an ordinal
  ("the second one", "2", "last"); "yes" when there is one option; otherwise the ONE option whose
  label words the reply names (strong: label ⊆ reply ⊆ label + chip question), or whose label /
  chip question holds all of the reply's words (weak). Tokens drop scheme names, filler, ₹ and
  thousands commas, and fold plurals. Two candidates at a level = no pick (never a guess). A reply
  naming a scheme picks only an option about that scheme.
- No pick: `_paused_thread_antecedent` — when the paused question names one scheme that the last
  answered turn was not about, and the reply names no scheme and its own vocabulary picks no other
  (`_infer_scheme_from_terms`), the paused question becomes `prev` and the thread state becomes
  that scheme with the carried place/year. Otherwise the reply goes on untouched.
- The measure-gap pause is remembered as its first offer (`turn_context["standalone_question"]`),
  a real question in the new scheme with the carried scope.
- Pauses with no options (`reference-ambiguous`, `column-not-held`) are not remembered.

**2026-09-27 (Focus Plus QA, FOCUS-029):**
- **Bank pause chips.** The "which bank?" pause (`_bank_clarification`) for a question naming
  no scheme had **no options**. It now offers "Focus Plus (bank name)" / "Focus Legacy (bank
  name)" under rule `bank-scheme-not-specified`, which is in `SCHEME_PAUSE_RULES`, so a typed
  scheme name resumes it too. A Focus Legacy bank question used to raise `KeyError` (no
  `_BANK_NOT_HELD_TEXT` entry). Now bank name / IFSC go to SQL, and only account-number /
  transfer / DBT questions are refused.
- **A resumed reply that pauses again.** `session.turn_context["resumed_question"]` holds the
  full question a reply resumed into, whether from a typed scheme name, a merged area/year reply
  or a chip. The router remembers it via `routers.query.pause_question(session, req.question)`
  instead of the bare reply. Before this, a typed "Focus Plus" → area pause was remembered as
  "Focus Plus", and the typed "All of Meghalaya" then ran as "Focus Plus, All of Meghalaya", a
  KB scheme overview. The same loss hit any typed area reply followed by a year pause. Chip
  clicks were unaffected, because a chip sends the whole question.
- **Batch / tranche is a scope.** `_needs_scope_clarification` no longer asks "which area?"
  when a Focus Plus tranche resolved or the question names the 12.5K / 93K batch. This follows
  the SME default `missing_geography → all_districts`.

### 5.3 Prompt budgets and observability (VERIFIED offline, 2026-09-26)

`app/context_budget.py` logs one `prompt_context` JSON line per model prompt:
- kind (`sql`, `sql_repair`, `sql_verify`, `rewrite`, `compose`);
- approximate tokens per named section;
- total, budget, `over_budget` and, if any, the `compressed` sections;
- `request_id`, set per request by `middleware/security_headers.py`;
- for `rewrite`, the tiers used, the follow-up kind and the guard verdict.

`llm.call_model` also logs one `llm_call` line per model call: role, model, queue ms, latency ms,
and the gateway's own `prompt_tokens` / `completion_tokens`. No prompt text is logged.

**Budgets** are `PROMPT_BUDGET_*` in config: SQL 15,360, classifier 7,992, and composer 0 (unchecked,
because qwen35-9b's max-model-len is not recorded).

The SQL prompt is **priority-aware** (`context_budget.assemble_prioritized`). **Required**
sections are never shortened:
- schema backbone, live schema, prohibited joins;
- common mistakes (business rules);
- resolved entities, scheme scope, question.

Only when over budget, the **optional** sections are compressed:
1. first the few-shot examples, from 5 to 3 to 1;
2. then catalogue lines the schema already states verbatim (`_dedupe_catalog`).

Nothing is removed to satisfy a count. If the required part alone is over, the prompt goes out
over budget and is logged as such. Under budget, the output is byte-identical to before.

Other prompts warn only.

**Measured offline, with the few-shot banks loaded:** the SQL prompt is 3.0k–6.9k tokens per
scheme, from MGNREGA 3,256 to CM Elevate Legacy 6,867. This excludes the LIVE SCHEMA and catalogue
blocks, which need the DB. No compression happens today.

The earlier "2.5k–5.6k" figure was measured without the few-shot banks loaded, and was wrong.

### 5.4 Field-level merge, follow-up kinds and provenance (VERIFIED offline, 2026-09-26)

`app/context_policy.py` is deterministic: no model call and no DB round trip. It runs in about
0.6 ms per follow-up.

**`plan_state_merge(question, state, prev)`** gives every field one action:

| Action | When |
|---|---|
| **KEEP** | The question says nothing about the field. |
| **REPLACE** | The question names a new value. For geography, this clears the finer levels under it. |
| **CLEAR** | The question says "all / by / every X" or "all years". |
| **REQUIRE_CLARIFICATION** | An unpinnable reference. "the other one" / "former" / "latter" goes to the existing `entity-ambiguous` pause via `substitute_references`. A relative year with no known year is left to the existing year pause. |

**Scope-less measure follow-ups** (`is_scopeless_followup`, live finding 2026-09-26): a short
question (≤8 words) right after a DATA answer is treated as a follow-up when it asks only for a
measure and names no scheme, place or year. Examples: "How many beneficiaries were there?",
"What was the total?". `looks_like_followup` treats "how many" / "total" as standalone
anchors, so such questions used to lose the previous scope. The router's `is_cacheable` uses the
same predicate.

**Removed filters reach the rewrite** (live finding): for every field the plan CLEARs, the
prompt gets a line `REMOVED by the follow-up (do NOT include these): district=…`. The check
rejects a rewrite that re-adds the value (`cleared:<field>`). Live, "show it by district" had
been rewritten "…by district in West Garo Hills", which the check allowed because the previous
question names that district.

**`apply_merge_plan`** removes, from `resolve_entities`' `prior_resolved` fallback, every field
that is REPLACEd, CLEARed or not pinned. It never adds a value. It fixes two live-risk cases:
- "show it by district" after a West Garo Hills turn used to inherit the district filter;
- "what about South Garo Hills?" after a Dalu turn used to keep `block=DALU` under the new
  district.

**Follow-up kind → context layers:**

| Kind | Layers the rewrite gets |
|---|---|
| `NEW_QUERY` | none |
| `RESULT_REFERENCE` ("the top one", "that block", "how much was it?") | previous filters + result rows (or an answer excerpt when there are no rows) |
| `METRIC_CHANGE`, `TIME_CHANGE`, `GEOGRAPHY_CHANGE`, `GROUP_BY_CHANGE`, `COMPARISON`, `FILTER_CHANGE`, `CLARIFICATION_RESPONSE` | previous filters only |

The summary and older turns are added, for any kind, only on an explicit back-reference.

**Per-field policy** (`FIELD_POLICIES`, keyed by field and never by scheme):

| Field | Inherit? | User can replace? | User can clear? | Clarifies when | Validated against |
|---|---|---|---|---|---|
| scheme | yes | yes | no | nothing named and no vocabulary; a bare "Focus" | scheme registry |
| district | yes | yes | yes ("all districts", "statewide", "by district") | a hill-range name | resolver catalogue |
| block | yes (while the district is unchanged) | yes | yes | name collides with a village or AC | all catalogues' block lists |
| village | yes (while the block and district are unchanged) | yes | yes | several villages share the name | DB (`resolve_village`); not offline |
| year / financial_year | yes | **always** | yes ("all years", "by year", "cumulative") | relative year with no known year; scheme needs a year | `_SCHEME_DATA_YEARS`, year-gap handling |
| category (tranche, sub-scheme, demographics) | yes | yes | yes ("all tranches", "everyone") | sub-scheme family name; tranche not stated | catalogue + closed vocabulary |
| metric | yes | yes | no | never (unknown → "not tracked") | metric vocabulary |
| group_by | no | yes | yes | never | dimension vocabulary |

**Provenance** is recorded in `ConversationState.provenance` by `update_state`, with the facts
from `session.turn_context`. Each field gets a `{value, source, confidence, turn}` record.
- The sources are `current_user`, `previous_user`, `clarification_response`,
  `resolved_reference`, `validated_database` and `model_inference` (confidence 1.0, 1.0, 1.0,
  0.9, 0.9 and 0.5).
- `merged_prior_resolved` does not inherit a `model_inference` value.
- Provenance is persisted with the state.

**Field-specific rewrite checks** (`context_policy.rewrite_violation`, behind
`_rewrite_provenance_violation`), KI-029:
- **scheme, district, block:** must appear, canonically, in a permitted source.
- **year:** any year a permitted source names, or ±1 of one with a relative-year cue. An
  explicit year change by the user always passes.
- **metric:** compared by family (money / count / days / completion), so a paraphrase passes
  and a new measure does not.
- **category:** closed vocabulary. Demographics, tranche numbers, and every CM Elevate
  sub-scheme name and alias.
- **name:** a capitalised word found in text the rewrite was *not* allowed to use (the previous
  answer, or the summary and older turns without a back-reference) and in no permitted source.
  This is how villages are covered offline.

### 5.5 Shared conversation state across workers (VERIFIED offline, 2026-09-26; KI-028)

`app/session_sync.py` makes `app.conversations.context_state`, in Postgres, the source of truth.
- `sync_in` runs at the start of every request. It applies the durable snapshot when its `rev`
  is newer than the worker's own copy.
- `sync_out` runs, **awaited**, after every answered turn and every clarification pause.
  `conversation_store.save_session_state` is an upsert with a revision guard: a concurrent stale
  write is refused, so last writer does not win.
- The snapshot is `Session.to_snapshot`: the state and provenance, the last turn (question,
  route, schemes, entities, answer ≤2,000 chars, `result_summary`), the pending clarification
  including its options, and the recent questions.
- Each call is bounded by `CONTEXT_STATE_SYNC_TIMEOUT_SECONDS` (1.0) and never raises. With the
  DB down, the request runs on the worker's local copy, as before.
- `maybe_update_summary` no longer writes on its own, because a fire-and-forget write could land
  after the versioned one.
- Redis was not used: in this codebase it is an optional, best-effort cache that is unset in dev.
- Cost, measured live on 2026-09-26: `sync_in` p50 52 ms / p95 359 ms, and `sync_out` p50 50
  ms / p95 153 ms. That is about 100 ms added to a p50 turn of ~2.3 s. Across the live suite
  the state crossed workers on every turn.


### 5.7 Continuation gate and the semantic contract (VERIFIED offline, 2026-09-29; D-030)

**Why:** after a Focus Plus answer, "who is harshit" and "he is my collik remember" were rewritten
"…under Focus Plus" (RAG / DATA), and "now give me five thousand loan for me i am in crisis"
became a Focus Plus DATA pause. Two steps inherited the scheme for any short message: the edge
whitelist was skipped whenever a previous answer existed, and `looks_like_followup` reads any
≤6-word anchorless message as a fragment.

**Gate:** `context_policy.continuation_signals(q)` → signal names: `domain` (edge vocabulary),
`scheme`, `place`, `year`, `metric`, `category`, `group_by`, `compare`, `reference` (it / that /
one / same / before / said …), `rank`, `scheme_topic` (benefits, eligibility, documents, apply,
launched, amount …), `lead` (a ≤6-word "what about / and …"), `non_english`. Empty → the turn is a
new question: `has_context=False` for both edge checks, no merge plan, no rewrite, no KNOWLEDGE
fallback to the previous turn's scheme. Logged as `pipeline_decision` stage `continuation`.

**Other asks instead of guesses:** a bare "which one?" (`is_bare_reference` → plan
REQUIRE_CLARIFICATION → `reference-ambiguous`); a letter-swapped district acronym
(`entity_resolver.acronym_near_misses`, e.g. WHK → West Khasi Hills chip; runs in `_answer_data`
before `resolve_entities`); a Focus Plus payment amount it never pays (`amount-not-held`).

**Contract enforcement before execution** (§2.8): `_resolved_scope_missing` and
`_focusplus_stated_amount_missing` turn a dropped constraint into a repair instruction, before
the 4B verifier.

**State:** `update_state` now drops a field the turn's plan CLEARs / REPLACEs when the result does
not name it (KI-032), and a DATA turn with no plan (a question of its own) resets geography and
year. `ConversationState.year_all` records an all-financial-years scope; `inject_year_scope`
appends "across all financial years" to a follow-up that keeps the year (KI-030); the Known-context
line shows `year=all financial years`. Persisted in the snapshot.

**Observability:** `context_budget.log_decision(stage, **facts)` writes one `pipeline_decision`
JSON line (request_id, stage, labels only): `continuation`, `followup` (signals, kind, non-KEEP
actions), `edge`, `intent`, `clarify` (rule, reason), `sql_guard` (guard, verdict, missing field),
`entity_search`. With `prompt_context` (sizes) and `llm_call` (tokens, latency) that answers why
an intent, scheme, clarification or SQL rejection happened.

**Second pass (2026-09-29, live-verified, KI-136 to KI-144):**
- a bare "Focus" used as a scheme name (`_names_bare_focus_scheme`, not `_FOCUS_AS_NOUN`) is never
  sent to the model rewrite, so the which-Focus pause asks;
- which thread a follow-up continues is decided per follow-up (`_followup_thread_state`): after a
  KNOWLEDGE answer about another scheme, a follow-up naming nothing continues that scheme with a clean
  state; one whose own words pick the earlier scheme ("and person-days?" = MGNREGA) continues the
  earlier thread, with that scheme's last DATA turn as the antecedent (`_data_thread_antecedent`,
  which also takes a place / year-only change after knowledge answers back to the last DATA turn).
  The digression never overwrites the session state; `update_state` drops unnamed filters when the
  committed scheme changes;
- after a KNOWLEDGE answer, a measure-only request gets the scheme appended with no model rewrite
  (`_measure_after_knowledge`), and a rewritten follow-up's intent comes from the typed words when
  the fast path decides them (`_typed_intent`);
- the rewrite must keep the follow-up's own metric family (`rewrite_violation`);
- the KNOWLEDGE path drops a year and a place the user never typed (`_drop_inherited_time`,
  `_drop_inherited_place`);
- a Focus Plus stated-amount answer is guaranteed to say which payments it covers and, for "loan",
  that Focus Plus is not a loan (the composer note alone was ignored live);
- a paused follow-up is remembered as its standalone form (`turn_context["standalone_question"]`,
  `routers.query.pause_question`);
- "all of them / combined / in total / both" CLEARs the dimension the last follow-up varied
  (`ConversationState.last_dimension`); for the year the rewrite is deterministic (`_all_years_rewrite`).

**Cost:** under 1 ms per turn in total (measured: continuation_signals 0.63 ms, the rest
≤0.16 ms each); no model call added.

### 5.6 Scheme substitution (VERIFIED live, 2026-09-26)

"give me same for mgnrega" after "how many beneficiaries in focus + for all of Meghalaya across
all financial years" means the **same operation on another scheme**.

**The failure it fixes, traced live:**
1. `looks_like_followup` returned False: the message names a scheme, and
   `_SCHEME_SWAP_FOLLOWUP` only knew a fixed set of openings.
2. So no rewrite ran, and `classify_intent` saw the bare words. The 4B classifier returned
   KNOWLEDGE.
3. RAG then answered with general MGNREGA information.

**The fix:**
- `pipeline._scheme_substitution(q)` returns `(scheme, pure)`. It needs a continuation cue
  (same / do it / repeat / likewise / what about / now for / and for / instead / switch to …)
  plus exactly one scheme from `_SCHEME_NAME_PATTERN`, the registry. No scheme name is
  hard-coded. `pure` means nothing else is asked once fillers are removed.
- A **pure** substitution is a follow-up (`looks_like_followup`). The existing deterministic
  `_scheme_swap_rewrite` swaps only the scheme in the previous question, so intent, metric,
  geography, time and grouping carry over verbatim, and `prior_resolved` inherits the filters.
- **With a "same" cue and changes of its own** ("give me the same for mgnrega in 2023-24"),
  `is_scheme_substitution(q, prev)` makes it a follow-up. It then goes through the checked model
  rewrite and the merge plan.
- The merge plan labels both kinds `SCHEME_SUBSTITUTION`. The router's `is_cacheable` excludes
  them.
- **Not substitutions:**
  - two schemes (a comparison);
  - the scheme already in play;
  - no antecedent;
  - a weak cue with other content ("what about MGNREGA eligibility?"), which keeps its previous
    reading;
  - no scheme at all ("same number by district", "same for Dalu", "same but last year"), which
    goes through the normal field-level plan.

**Request-verb swaps and measure gaps (KI-180, VERIFIED live 2026-09-29):**
- `_SCHEME_SWAP_FOLLOWUP` also accepts a request verb before the preposition ("give me for
  pmay", "show us the same for PMAY-G", "can you give me for pmay?", "pmay too"), and
  `_SUBSTITUTION_CUE` reads "give/show/get/tell/fetch me [the same] for|in|under" as a cue ("give
  me for pmay please"). "tell me about pmay" / "what is pmay" / "give me pmay houses" are not
  swaps. A bare "focus plus please" is not a follow-up either (it stays cacheable).
- `_swap_measure_gap(followup, rewritten, prev, state)`, called in `_run_pipeline` right after
  the follow-up rewrite: after a DATA turn, when a scheme swap carried over a measure that only
  another scheme holds (`_SCHEME_OWN_MEASURES`: MGNREGA person-days / job cards / wages /
  material / employment; PMAY-G houses) and the follow-up did not type it, it raises
  `ClarificationNeeded(rule="swap-measure-unavailable")`: "PMAY-G doesn't record person-days —
  only MGNREGA does", with the new scheme's own measures (`_SCHEME_HEADLINE_OFFERS`) as chips,
  for the same scope (`_swap_scope_phrase`: state place + FY / all years; CM Elevate gets no
  year). Money and counts exist in every scheme and are left to the generator. Since KI-181 the pause is remembered (§5.2): a typed "houses sanctioned" or "the second one"
  resumes the chip.

---

## 6. Post-answer
- `followups.build_followups(route, question, schemes, resolved, sql, rows)`: up to 3
  deterministic `follow_up_options`, from per-scheme banks. No model call.
- Caching is done in the router (exact + semantic). Clarifications, follow-up fragments and any
  reply to a pending clarification (`session.pending_scope_q` set) are never looked up in or
  written to the cache.

---

## 7. Where to change what (guidance)

| Symptom | Look at |
|---|---|
| Wrong scheme picked | `_shortcut_scheme`, `_SCHEME_NAME_PATTERN`, `_<X>_ONLY_TERMS`, `_infer_scheme_from_terms`, `_pin_cm_elevate_dataset` |
| Place misresolved | `entity_resolver.resolve_dimension` / YAML catalogues, `extract_entity_mentions` prompt, `_drop_producer_group_names` |
| Wrong SQL shape | few-shot YAML (add a right-shaped example), `schema_context` rules, then a guard in `execute_with_repair` |
| Verifier false positive | add a filter to `_verify_sql` (e.g. `_verifier_village_code_complaint_is_false`) or a calibration example in `prompt_builder._VERIFY_CALIBRATION` |
| Wrong number in prose | `compose_response` guards; never loosen `_answer_numbers_faithful` |
| Clarification loop | `_reply_abandons_scope_pause`, the gate's `_needs_*` function, `pending_scope_q` handling |
| Typed scheme reply not resumed | `_resume_scheme_pause`, `SCHEME_PAUSE_RULES`, router `pending_scope_rule/options` |
| Follow-up lost or gained a filter | `context_policy.plan_state_merge` / `apply_merge_plan` (the plan is in `session.turn_context["plan"]`), `context_manager.build_rewrite_evidence`, `context_policy.rewrite_violation`; the `prompt_context` line with `"kind": "rewrite"` shows the kind, tiers and guard verdict |
| A message inherited a scheme it has nothing to do with / a follow-up lost its context | `context_policy.continuation_signals` (§5.7); the `pipeline_decision` line with `"stage": "continuation"` or `"followup"` shows the signals |
| A stated constraint missing from the SQL | `_resolved_scope_missing`, `_focusplus_stated_amount_missing` (§2.8); `pipeline_decision` `"stage": "sql_guard"` |
| Follow-up broke after a worker switch | `session_sync.sync_in/sync_out` log lines, `app.conversations.context_state->>'rev'` |
| KB answer wrong scheme | `_kb_scheme` selection, `rag._SCHEME_DOC_NAMES`, KB tags |

## 8. LangGraph orchestrator (2026-10-02, D-032; opt-in, offline-verified only)

The same stages, run as a 21-node graph:

```
context → edge → followup → route → (knowledge | data_context → scope_scheme → entities →
clarification_gate → deterministic → sql_generate → authorize → execute_attempt ⇄ repair →
post_rows → compose → guarantees → assemble) → finalize → context_update
```

- Any `ClarificationNeeded` → `pause_gate` (`interrupt()`), with a checkpoint under
  `thread_id = session_id`. The resume loops to `context`, where step c/c'/c'' decides.
- No prompt, model role, guard, gate or composer check changed. The graph spends exactly the
  model and DB calls of the sequential path (tested).
- Details: [LANGGRAPH_ARCHITECTURE.md](LANGGRAPH_ARCHITECTURE.md),
  [CONTEXT_AND_LANGGRAPH.md](CONTEXT_AND_LANGGRAPH.md),
  [LANGGRAPH_PAUSE_RESUME.md](LANGGRAPH_PAUSE_RESUME.md),
  [LANGGRAPH_MIGRATION.md](LANGGRAPH_MIGRATION.md).

