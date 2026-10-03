# Megh One AI (nlp-service): Technical Brief

> **Historical snapshot (2026-09-24). Not maintained.** Its findings have been migrated into the
> maintained docs:
> - [ARCHITECTURE.md](ARCHITECTURE.md)
> - [AI_PIPELINE.md](AI_PIPELINE.md)
> - [DATA_MODEL.md](DATA_MODEL.md)
> - [KNOWN_ISSUES.md](KNOWN_ISSUES.md)
>
> Known drift since it was written:
> - Line numbers are stale: `pipeline.py` has grown from 7,587 lines to about 8,400.
> - `_AC_CAPABLE_SCHEMES` now also includes CM Elevate Legacy.
> - Verifier false-positive filters: 8, not 6.
> - Test counts are now 558 pytest cases and 14 scripts.
> - The "ARCHITECTURE.md describes two schemes" finding was fixed on 2026-09-26.
>
> Keep this file for its log-derived evidence (§4), which was not re-measured.

*State of the working tree as of 2026-09-24 (branch `main`, including uncommitted changes).*
*Built by reading the code, running the test suite and mining `logs/`. No code was changed.*

---

## 0. Summary

- **What it is.** A single FastAPI service that answers officers' natural-language questions about **six** Meghalaya scheme datasets: MGNREGA, PMAY-G, Focus Plus, CM Elevate, Focus Legacy and CM Elevate Legacy. `docs/ARCHITECTURE.md` still describes only two of them. Number questions go through NL→SQL against PostgreSQL `megh_db` (schema `curated`). "How does the scheme work" questions go through RAG over Qdrant. All model inference is remote, on a vLLM gateway at `10.48.242.4`.
- **No framework.** There is no LangChain, LlamaIndex or SQLAlchemy. Model calls are raw `httpx` requests to an OpenAI-compatible `/chat/completions` endpoint (`app/llm.py`). The database is reached through raw `asyncpg` (`app/db.py`). Retrieval uses `qdrant-client` and `fastembed`.
- **How NL→SQL works.** It is a multi-step chain with one SQL-writing call:
  1. deterministic regex gates;
  2. an optional LLM intent/scheme classifier;
  3. LLM span extraction, then deterministic entity resolution against YAML catalogues and the database;
  4. one SQL-generation call to `qwen3-coder-30b`, with a large hand-written schema prompt plus few-shot examples;
  5. regex "bug-shape" guards;
  6. an LLM semantic verifier (`qwen3-4b`);
  7. a repair loop of up to 4 attempts;
  8. an LLM answer composer (`qwen3.5-9b`) with a numeric-faithfulness check.
- **Tests.** All tests pass: 423 pytest cases and 14 plain-script suites. But they are regression tests on regex, prompt text and catalogues. **There is no end-to-end accuracy benchmark** (question → expected SQL or answer against the real database and model).
- **Live failure evidence.** This comes from `logs/query_audit.jsonl` (3,141 turns, 2026-08-29 → 09-24) and the uvicorn logs:
  - **36% of all turns are clarification pauses.**
  - Out of 172 SQL repair events, the largest cause is the **semantic verifier** (~87). Many of its flags are false positives.
  - Next is **hallucinated or wrong-table columns** (42), then the known bug-shape guards.
  - **34 requests** used up the whole repair budget and fell back to the knowledge base.
- **Biggest risks when scaling to 25 schemes and ~200 concurrent users:**
  - Schemes are hand-enumerated in about 13 registries. `pipeline.py` alone is 7,587 lines, has 155 compiled regexes and names a scheme 569 times.
  - The schema prompt grows linearly with the number of schemes. With all 6 schemes it is already about 32K tokens.
  - Clarification-resume state is kept **per worker, in memory**, with no sticky routing.
  - LLM-generated SQL runs under the **same DB role that can read and write the `app` schema** (users, password hashes, other tenants' conversations) and the unmasked privacy tables. Nothing checks which tables the SQL touches.

---

## 1. Architecture overview

### 1.1 Components

| Layer | File(s) | Responsibility |
|---|---|---|
| App / lifecycle | `app/main.py` | FastAPI app, middleware stack, startup (`lifespan` L61-82: DB pool, LLM client, Qdrant, YAML annotations, entity catalogues, `app.*` schema, live schema catalogue, per-scheme FY coverage, background KB ingest), `/health`, `/metrics`, static UI |
| Config | `app/config.py` | Every knob: model roles and endpoints, timeouts, caches, clarification gate toggles, pool sizes |
| API: query | `app/routers/query.py` | `POST /api/query` (L62): auth → session → exact cache / semantic cache → `answer_question` under `asyncio.wait_for(60s)` → persistence and audit. `POST /api/query/transcribe` (ASR) |
| API: other | `app/routers/{auth,admin,history,rag}.py` | JWT login and bootstrap, admin CRUD, conversation history, direct RAG |
| Orchestration | `app/pipeline.py` (7,587 lines) | The whole routing and NL→SQL chain (§1.2) |
| Edge handler | `app/edge.py` | Regex-only greetings, off-topic, abuse, identity; no model call |
| Prompting | `app/prompt_builder.py` | Builds the SQL, repair and verifier prompts |
| Schema text | `app/schema_context.py` (1,575 lines) | Hand-written per-scheme TABLES / RULES / VOCAB blocks |
| Live schema | `app/schema_introspect.py` | At startup, reads `information_schema` plus `semantic.*` catalogue into prompt blocks |
| Few-shot / joins | `app/annotations.py` | Loads `data/<scheme>/*few_shot*.yaml` and FK/prohibited-join YAML; lexical IDF ranking |
| Entity resolution | `app/entity_resolver.py` | District/block/year/AC/tranche/sub-scheme resolved in memory from YAML; villages resolved live from `dim_geography` / `dim_geography_alias` (exact, then trigram) |
| LLM client | `app/llm.py` | One shared `httpx.AsyncClient`, a per-worker semaphore (24), role wrappers (classifier, SQL generator, verifier, composer, embedding, reranker, ASR) |
| DB | `app/db.py` | `asyncpg` pool; `run_readonly()` with a SELECT/WITH-only check and auto-LIMIT |
| AuthZ | `app/auth.py`, `app/security.py`, `app/deps.py` | Role → scope; post-generation `authorize()` checks scheme, geography and granularity by regex over the SQL |
| RAG | `app/rag.py`, `app/kb_ingest.py`, `app/vectorstore.py`, `app/local_embed.py` | Chunks `data/reference/*.md` → bge-small (local) → Qdrant → compose |
| Conversation | `app/session_store.py` (L1, in-process), `app/conversation_store.py` (L2, Postgres), `app/context_manager.py`, `app/conversation_memory.py` (Qdrant) | Follow-up rewrite, reference substitution, summaries |
| Post-answer | `app/followups.py`, `app/premise_check.py` | "Next step" chips; checks numeric premises in the question |
| Caches | `app/cache.py`, `app/semantic_cache.py` | Exact-match LRU (optional Redis L2) and a cosine semantic cache, both per worker |
| UI | `web/ai_query.html`, `web/admin.html` | Vanilla JS chat and admin console |

### 1.2 End-to-end request flow (DATA path)

Each step lists the file:function and whether it makes a model call (**[LLM]**) or a database call (**[DB]**).

1. **HTTP in.** `routers/query.py:query` (L62).
   - `_identity` requires a JWT.
   - The session id comes from the client, or falls back to a per-day bucket (L75-80).
   - `session_store.ensure()`. If the session is new, its state is rehydrated from Postgres via `conversation_store.load_context_state` (L90-99). **[DB]**
2. **Cache.** If the question is not a follow-up fragment (`looks_like_followup`):
   - check `response_cache.get` (exact match);
   - otherwise check `semantic_cache.lookup` (one local embedding, threshold 0.93) (L112-121).
3. **Pipeline.** `pipeline.answer_question` (L7081) → `_run_pipeline` (L7124), under `asyncio.wait_for(REQUEST_TIMEOUT_SECONDS=60)`.
   1. `_correct_scheme_spelling`, `_pin_cm_elevate_dataset` (L7127-7133).
   2. **Resume a paused clarification.** If `session.pending_scope_q` is set, the reply is merged: `"<paused question>, <reply>"` (L7149-7177).
   3. Early deterministic answers:
      - `_geo_definition_answer` ("what is EKH");
      - `_why_choice_answer`, `_scheme_recommendation_answer`, `_scheme_pick_answer`;
      - `_scheme_listing_answer` / `_scheme_comparison_answer` (L7184-7217).
   4. `edge.detect_edge_case` (L7226). Regex only.
   5. `context_manager.substitute_references` ("previous year", "the former") (L7242).
   6. **Follow-up rewrite.** When `looks_like_followup` is true and the previous turn was data or knowledge: `context_manager.build_followup_context`, then `rewrite_followup` (L361). **[LLM classifier]**, sometimes **[Qdrant]**
   7. Pre-routing refusals: bank details (`_bank_clarification`), admin expenditure, and named-but-unsupported schemes (L7314-7338).
   8. **Intent.** `classify_intent` (L6788). A regex fast path first; otherwise **[LLM classifier]** returns `{"intent": "DATA" | "KNOWLEDGE"}`. Unusable output defaults to DATA.
   9. The KNOWLEDGE branch goes to `rag.answer_from_kb` / `answer_from_kb_multi` (L7344-7422).
   10. **DATA.** `_answer_data` (L6854):
       1. Unsupported scheme → cross-scheme money ranking (deterministic SQL) → "which Focus?" → **scheme gate** `_needs_scheme_clarification` (L2081) → **top-N gate** (L2146).
       2. `classify_scheme` (L3530). Deterministic `_shortcut_scheme` (L3492) first; otherwise **[LLM classifier]** with a guided-JSON schema. **On unusable output it returns ALL schemes** (L3562).
       3. `resolve_entities` (L4329):
          - year-range guard (`_out_of_range_year_in` / `_apply_year_gap`);
          - `extract_entity_mentions` (L3738) **[LLM classifier]**, span extraction only;
          - deterministic resolution per dimension via `entity_resolver.resolve_dimension`, `resolve_village` **[DB]**, cross-level collision detection, and the AC drill-down;
          - raises `ClarificationNeeded` when a name is ambiguous and `OutOfScope` when a place is not in Meghalaya.
       4. Region expansion ("Garo Hills" → 5 districts), then the Focus Plus overall summary, then the **scope gate** (L2301), **year gate** (L2636), **tranche gate** (L3091) and the person-level tranche conflict check (L6898-6948).
       5. `generate_sql` (L5349): `prompt_builder.build_sql_prompt` → **[LLM SQL generator]**, `guided_regex` forcing the output to start with SELECT/WITH → `_extract_sql` (L474) → `_focusplus_single_district_beneficiary_guard`.
       6. `auth.authorize` (L6954), using regexes over the SQL.
       7. `execute_with_repair` (L6191), up to 4 attempts:
          - rewrites: `_uppercase_geo_literals`, `_focusplus_drop_unrequested_verification_status`;
          - **9 regex guards**, each of which raises a `ValueError` carrying a repair instruction;
          - `_verify_sql` (L6131) **[LLM verifier]**;
          - `db.run_readonly` (L130) **[DB]**;
          - on any exception: `build_repair_prompt` → **[LLM SQL generator]**, then loop.
       8. Notes: `_genuine_zero_notes`, `_sector_not_tracked_notes`, CM Legacy notes, `premise_check.check_premises`.
       9. `compose_response` (L6608) **[LLM composer]**. If the answer misquotes a number or drops a metric, one strict retry **[LLM]**, then a deterministic fallback sentence.
   11. Exceptions from `_answer_data` (L7450-7473):
       - `ClarificationNeeded` is re-raised;
       - `OutOfScope` returns the scope reply;
       - transient gateway errors are re-raised (the router turns them into 503/504);
       - **anything else goes to `_data_path_kb_fallback`** (L7568), which tries RAG and then returns "couldn't build a working query".
   12. `_attach_followups`, then `context_manager.update_state` and `maybe_update_summary` (L7087-7098).
4. **Back in the router:**
   - `ClarificationNeeded` becomes a `route:"clarification"` response, and `pending_scope_q` is stored **in-process** (L129-165);
   - `session_store.add_turn`;
   - fire-and-forget `conversation_store.persist_turn` (turn, SQL, audit), `conversation_memory.index_turn`, `save_context_state`;
   - JSONL audit mirror;
   - cache write-back;
   - metrics (L183-228).

**Model calls per DATA request:**
- Best case: 3. The intent and scheme steps short-circuit, leaving entity extraction, SQL generation and composition; add 1 for the verifier.
- Typical: 4-6.
- Worst case: about 13 (follow-up rewrite + intent + scheme + entities + 4 × (generate + verify) + composer + strict retry).
- The logs confirm each repair attempt costs 2 HTTP calls (generate + verify).

### 1.3 Dependencies that matter

From `requirements.txt`:

| Package | Used for |
|---|---|
| `fastapi` / `uvicorn` | API server |
| `asyncpg` | All database access; no ORM |
| `httpx` | Every model call; OpenAI-compatible JSON shape, hand-built |
| `rapidfuzz` | Fuzzy matching of district/block names and scheme names |
| `qdrant-client` | Vector store |
| `fastembed` | Local `BAAI/bge-small-en-v1.5` (384-dim) embeddings, because the gateway has no embedding model deployed (`config.py:224`) |
| `redis` | Optional shared cache and rate limit |
| `PyYAML` | SME artefacts |

vLLM-specific request fields are used: `guided_json`, `guided_regex`, and `chat_template_kwargs.enable_thinking=False` (`llm.py:158-235`).

**Model roster** (`config.py:162-247`):

| Role | Model | Notes |
|---|---|---|
| SQL generation | `qwen-model` (qwen3-coder-30b-fp8) | |
| Classifier and SQL verifier | `qwen4-deploy` (Qwen3-4B) | Two roles share one deployment |
| Answer composer | `qwen35-9b` | |
| Reranker | `qwen3-reranker` | **Disabled**: `RERANKER_ENABLED=False`, because it "inverts relevance" (`config.py:232-237`) |
| ASR | `qwen3-asr` | Voice input |

---

## 2. NL→SQL logic in detail

### 2.1 Shape of the chain

The text-to-SQL step is **not** a single call and **not** an agent. It is a fixed pipeline:

1. **Regex pre-routing and gates:** more than 150 compiled patterns in `pipeline.py`.
2. **Scheme classification.** Regex shortcut first, otherwise the LLM. Prompt at `pipeline.py:3539`:
   ```
   Classify which scheme(s) this question needs. Available schemes:
     - "MGNREGA": Rural employment guarantee — village-year grain, ...
     - "PMAY-G": ...
     ... (SCHEME_CATALOG, schema_context.py:20)
   Return ONLY JSON: {"schemes": ["MGNREGA" | "PMAY-G" | ..., ...]}
   Use several if the question compares or combines schemes. If it names none specifically
   and gives no scheme-specific vocabulary, return every scheme.
   Question: "{question}"
   JSON:
   ```
3. **Entity span extraction** with the LLM, then **deterministic resolution**. The extraction prompt (`pipeline.py:3743-3816`) says "Extract place/time names ... verbatim", with keys `district`, `block`, `village`, `year`, `assembly_constituency`, `blocks`, `districts`. It carries detailed negative rules: scheme names, producer-group names and CM Elevate sub-scheme names are never places; use `assembly_constituency` only with an explicit "constituency/AC/MLA" signal. It includes 9 worked examples. Spans that do not appear verbatim in the question are dropped as hallucinations (`_mention_in_question`, L3659).
4. **SQL generation.** One call; the prompt is described below.
5. **Validation:** regex guards, the LLM verifier, then execution. Any failure feeds a **repair prompt**. Up to 3 repairs (`max_repairs=3`, `pipeline.py:6193`).
   - Note that `config.SQL_GENERATION_MAX_RETRIES=1` (`config.py:179`) is **never read**.
6. **Composition** by the LLM, with deterministic faithfulness checks.

### 2.2 What the SQL generator is sent

**Message format.** Every model call is a **single `user` message with no system prompt** (`llm.py:173-181`). The prompt is assembled by `prompt_builder.build_sql_prompt` (L416-428) in this order:

```
build_schema_context(schemes)                          # hand-written backbone, schema_context.py:1552
_live_schema_block(schemes)                            # "LIVE SCHEMA — columns that exist in megh_db right now ...
                                                       #  If a column is not listed here it does not exist; do not use it."
                                                       #  + "REAL FOREIGN KEYS (valid join paths — PROHIBITED JOINS below still overrides)"
schema_introspect.catalog_block(schemes)               # semantic.table_catalog / glossary / metric_definitions (max 24 terms)
"PROHIBITED JOINS:\n..."                               # annotations.prohibited_joins_text
"EXAMPLES (verified SQL, and known-unanswerable questions):"  # top-5 few-shot PER SCHEME
"COMMON MISTAKES — each WRONG query below returns a wrong number or zero rows; write the RIGHT shape:"
"RESOLVED ENTITIES — MANDATORY: the WHERE clause MUST use these exact values ..."
"NOT FOUND (do not filter on these — state plainly they're not in the data):"
"The user's question is about: {schemes}."
'Question: "{question}"\nSQL:'
```

**Backbone.** `schema_context.build_schema_context` (L1552-1571) concatenates:
- `_PREAMBLE` ("Query only these curated/semantic objects ... Never query raw, staging or meta");
- `_SHARED_TABLES` (dim_scheme, dim_year, dim_geography, plus the scheme_code literal rules, e.g. PMAY is `'PMAY'` and not `'PMAY-G'`);
- for each scheme, `(_X_TABLES, _X_RULES, _X_VOCAB)`;
- `_CROSS_SCHEME` when more than one scheme is involved;
- `_SHARED_RULES` (no 'Meghalaya' filter, no DISTINCT inside window functions, the Pareto/NTILE template);
- `_CLOSING`:
  ```
  Every query MUST end with a LIMIT clause (the executor adds one if you omit it, but include it).
  Generate a single read-only SELECT statement only — no comments, no explanation, SQL only.
  ```

Example of the rule style (MGNREGA, `schema_context.py:235-287`):
```
MGNREGA RULES (breaking these produces a wrong number, not just an ugly query):
  1. NEVER join fact_mgnrega_employment to fact_mgnrega_expenditure directly (no shared grain). ...
  2. Both MGNREGA facts are at source-row grain — always SUM ... GROUP BY, never read a row raw.
  3. job_cards_issued_total is a cumulative STOCK. Report ONE financial year ...
  4. Money unit: MGNREGA expenditure is LAKH RUPEES — ... do NOT divide by 100 ...
  ...
```

**Entity block.** `prompt_builder._entities_block` (L165-391) renders each resolved key with an inline instruction. For example:
- `lgd_district = 'EAST KHASI HILLS'   -- already uppercase, matches storage exactly`
- `village_code = 277769   -- do NOT filter on lgd_village_name instead`
- Focus Plus blocks become `UPPER(block_name_raw) = 'SONGSAK'` with a note about 15% NULLs in `lgd_block`.
- Assembly constituencies become either `UPPER(assembly_constituency_name) = UPPER(...)` (MGNREGA) or a `dim_geography` join on `ac_name` (Focus Legacy), plus "CONSTITUENCY ONLY" / "BOTH REQUIRED" instructions.
- District and block are suppressed when a `village_code` is present (L187).

**Few-shot selection.** `annotations.few_shot_examples` (L302) ranks each scheme's YAML pool by **lexical token overlap weighted by IDF**, with synonyms and down-weighting of tokens that appear everywhere. It is not embedding-based. It takes **top 5 per scheme**, including `UNANSWERABLE` negative examples with `sql: null`. The ranking text is expanded with the resolved district/block names (`_fewshot_ranking_text`, L399).

Pool sizes (lines containing `question:`):

| Scheme | File | Count |
|---|---|---|
| MGNREGA | `few_shot.yaml` | 68 |
| PMAY-G | `pmay_few_shot.yaml` | 71 |
| Focus Plus | `focusplus_few_shot.yaml` | 87 |
| CM Elevate | `cmelevate_few_shot.yaml` | 155 |
| Focus Legacy | `focuslegacy_few_shot.yaml` | 81 |
| CM Elevate Legacy | `cmelevatelegacy_prompt_few_shots.yaml` | 154 |

**Measured prompt size.** `build_sql_prompt` was run offline, without the live-schema block (which adds more when the database is reachable):

| Schemes in play | Backbone chars | Full prompt chars | ≈ tokens |
|---|---:|---:|---:|
| MGNREGA | 10,788 | 13,048 | 3.3K |
| PMAY-G | 9,579 | 12,179 | 3.0K |
| Focus Plus | 21,493 | 24,947 | 6.2K |
| CM Elevate | 22,000 | 25,646 | 6.4K |
| Focus Legacy | 21,565 | 25,752 | 6.4K |
| CM Elevate Legacy | 15,978 | 26,899 | 6.7K |
| **All 6 (cross-scheme or classifier fallback)** | — | **127,926** | **~32K** |

**Guided decoding.** SQL generation sends `guided_regex = r"\s*(SELECT|WITH|select|with)[\s\S]*"` (`pipeline.py:461`). This only forces the output to start with SELECT/WITH; it is not a SQL grammar. If the gateway rejects guided fields with 400/404/422, `call_model` retries once without them (`llm.py:209-228`).

### 2.3 Validation, sanitisation and execution

**`execute_with_repair` guards (`pipeline.py:6195-6307`).** They run in order; each raises a `ValueError` whose message becomes the repair error text:

| Guard | Bug shape it catches |
|---|---|
| `_village_name_filter_instead_of_code` | Filter on `lgd_village_name` when a `village_code` was resolved |
| `_village_filtered_at_wrong_level` | A village name placed in `lgd_block` / `lgd_district` |
| `_village_code_as_geography_key` | `geography_key = <village_code>` |
| `_crore_conversion_for_single_village` | MGNREGA lakh ÷ 100 on a single village |
| `_mgnrega_facts_joined` | `v_employment` JOIN `v_expenditure` (row fan-out) |
| `_ac_with_invented_geo_filter` | AC filter ANDed with an invented block or district filter |
| `_STATE_PSEUDO_FILTER` | A `'Meghalaya'` / `entity_type='State'` filter |
| `_rowgrain_no_aggregate` | Raw row read on a row-grain view for an aggregate question |
| `_verify_sql` (LLM) | Anything else, checked against 4 criteria: prohibited join, resolved entity dropped, grain, metric column |

- Before the guards, two in-place rewrites run: `_uppercase_geo_literals` and `_focusplus_drop_unrequested_verification_status`.
- **Verifier false-positive suppression.** Six post-hoc filters discard verifier complaints already known to be wrong (`pipeline.py:5917-6130`):
  - `_VERIFIER_FALSE_EMPTY_ENTITIES`
  - `_verifier_complaint_is_cosmetic`
  - `_verifier_year_complaint_is_false`
  - `_verifier_apostrophe_complaint_is_false`
  - `_verifier_missing_geo_is_false`
  - `_verifier_wants_suppressed_geography`
- The verifier prompt (`prompt_builder.build_verify_prompt`, L542-621) also carries about 90 lines of calibration examples (`_VERIFY_CALIBRATION`, L448-539), each written after a verifier false positive.

**Safety check in `db.run_readonly` (L130-141) and `_assert_safe` (L81-89):**
- No `;` after stripping a trailing one.
- The first word must be `select` or `with`.
- The text must not match `\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum)\b` anywhere, including inside string literals, so it can produce false blocks.
- A `LIMIT {SQL_MAX_RESULT_ROWS=1000}` is appended only when the substring `"limit"` does not already occur anywhere in the SQL. A `LIMIT 1` in a subquery or CTE is enough to suppress the outer cap.
- The statement timeout is the pool `command_timeout` = 15 s (`db.py:46`).

**Repair prompt (`prompt_builder.build_repair_prompt`, L431-445).** Same backbone, live schema, prohibited joins, common mistakes and entities, but **no few-shot examples**, then:
```
The previous query FAILED and must be corrected.
Error: {error}
Hint: {extra_hint}          # _repair_hint: missing-column home table / MGNREGA split-fact / window-fn hints
Previous query:
{failed_sql}
Question: "{question}"
Return the corrected single read-only SELECT. SQL:
```

**Error handling after the loop.** Once the budget is spent, the exception propagates to `_run_pipeline`:
- a transient gateway error → HTTP 503/504;
- anything else → `_data_path_kb_fallback` (RAG, then "couldn't build a working query").

`UnsafeSQLError` raised at the router becomes an HTTP 500 (`query.py:173-176`). In practice it is caught inside the repair loop first, because of `except (UnsafeSQLError, Exception)` at `pipeline.py:6310`.

**Gaps in this loop:**
- There is **no convergence check**. Logs show the model producing the **same failing SQL four times in a row**; see §4.2 category B.
- **The failing SQL is never logged**, and neither is the question nor the request id. See §5.2.

### 2.4 How the database hierarchy is represented to the model

- **No hierarchy tables.** There is no district or block dimension. District and block are **denormalised UPPERCASE name strings** on `dim_geography` and on every view (`lgd_district`, `lgd_block`). The model is told this in prose; the only codes are `village_code` (LGD) and `geography_key`.
- **Hierarchy lives in the resolver YAMLs**, not in the database:
  - `data/<scheme>/<scheme>_entity_resolver.yaml`: 12 districts and 50-58 blocks **per scheme** (each file has its own copy), aliases, blocked fuzzy pairs, region groupings (Garo / Khasi / Jaintia hills), and block → parent district;
  - `entity_resolver.block_parent_district` (L773).
- **Assembly constituency is a cross-cutting dimension, not part of the hierarchy.**
  - It exists as `dim_geography.ac_name` / `ac_number`, and as `assembly_constituency_name` on `fact_mgnrega_employment` / `v_employment` only.
  - Focus Legacy reaches it through a `dim_geography` join.
  - Other schemes cannot answer AC questions at all; `_AC_CAPABLE_SCHEMES = ("MGNREGA", "Focus Legacy")` (`pipeline.py:4061`).
  - `entity_resolver.constituency_contents` (L1251) reads the AC → block/district overlap live from `v_employment`.
- **Villages** are resolved live:
  1. exact case-insensitive match on `lgd_village_name` or an alias;
  2. `pg_trgm` `similarity > 0.4`, limit 5 (`entity_resolver.py:1292-1388`);
  3. duplicates with the same name, block and district are collapsed by data volume across all 7 scheme views (`_activity_counts`, L1183).
- **Name collisions across levels** (a name that is a block and an AC and a village) are detected by `collides_across_dimensions` (`entity_resolver.py:943`) and turned into "which level did you mean?" chips.

---

## 3. Database layer

Sources: `data/schema/schema_for_developers.md`, `docs/DATA_MODEL.md`, `schema_context.py` and the logs of the live schema load. On 2026-09-08, `schema_introspect` reported **27 tables and 25 FK edges**, plus 21 catalogue tables, 16 glossary terms and 18 metrics.

### 3.1 Objects

The database is **`megh_db`** (PostgreSQL 18.4 at `10.48.242.4`), with schemas raw / staging / curated / semantic / meta / app.

**Shared dimensions (`curated`):**

| Object | Grain / rows | Keys and notes |
|---|---|---|
| `dim_geography` | 7,364 (7,200 Village + 164 Ward) | PK `geography_key` (surrogate, **not stable across reload**); natural key `village_code` (LGD, UNIQUE). Columns `lgd_village_name` (mixed case), `lgd_block` and `lgd_district` (UPPERCASE), `ac_number`, `ac_name`, `entity_type`, `on_roster`, `has_geo_conflict` (16 rows). GIN trigram index on the name |
| `dim_geography_alias` | 18,959 | Every source spelling → `geography_key`. MGNREGA sources only |
| `bridge_geography_source` | 18,456 | Village × source coverage |
| `dim_year` | 4 rows (FY 2022-23 → 2025-26) | `year_key` = FY **start** year |
| `dim_scheme` | one row per scheme | `money_unit`, `time_semantics` |
| `dim_pmay_house_status` | 6 stages | `is_completed` / `is_in_progress` (nullable) |

**Facts and views per scheme** (always queried through the `v_*` views):

| Scheme | Query surface | Grain | Money | Years |
|---|---|---|---|---|
| MGNREGA | `v_employment` (26,375 rows; has `assembly_constituency_name`), `v_expenditure` (18,818), `v_district_year_summary` | source row (many per village-year) | LAKH ₹ | 2022-25 |
| PMAY-G | `v_pmay`, `v_pmay_monthly_sanctions` (statewide, crore) | one house | ₹ | year_key 2017-2023 + NULL |
| Focus Plus | `v_focus_plus` (385,671 rows) | one payment | unverified | partial; split into tranches and cohorts (93K / 12.5K) |
| CM Elevate | `v_cm_elevate` | one application | **none** | **no date or year** |
| Focus Legacy | `v_focus_legacy` (14,569 rows, 11,904 PGs) | one payment to a producer group | unverified (= members × 5000) | with a gap year |
| CM Elevate Legacy | `v_cm_elevate_disbursement` | one applicant × scheme | ₹ | FY 2024-25, 2025-26 |
| Cross-scheme | `v_cross_scheme_money_district_year` (MGNREGA + PMAY only, crore), `v_cross_scheme_village_coverage` | district × year / village | crore | |

- **Privacy tables that must never be queried:** `fact_focus_legacy_disbursement` and `bridge_pg_bank_history` (unmasked account numbers) (`schema_context.py:777-816`). **This is enforced only by the prompt**; see §5.3.
- **FK / join model.** Every fact joins `dim_geography`, `dim_year` and `dim_scheme` by surrogate key. **No two facts are joined to each other.** Prohibited joins come from `data/*/…foreign_key_augmentation.yaml` via `annotations.prohibited_joins_text`.

### 3.2 Known schema-to-question mismatches

Each of these is encoded somewhere in the code, which shows it has already caused a wrong answer.

| # | Mismatch | Where handled |
|---|---|---|
| 1 | Block and district are names, not codes. Case differs (UPPER for block/district, mixed for village). A Title Case literal silently matches 0 rows | `_uppercase_geo_literals`; entity-block comments |
| 2 | Focus Plus `lgd_block` is NULL on 15% of rows (₹18.01 Cr hidden); the real block column is `block_name_raw`, stored Title Case | `prompt_builder.py:214-230`; `test_focusplus_block_columns.py` |
| 3 | AC exists only on MGNREGA employment (and `dim_geography.ac_name`). AC names collide with block names (MAWLAI, RANGSAKONA ≠ a block). The generator ANDs `lgd_block='<AC name>'` and gets a false zero | `_ac_with_invented_geo_filter` (`pipeline.py:5382`), "CONSTITUENCY ONLY" prompt text |
| 4 | 345 village names are shared by more than one village; 57 duplicate `dim_geography` groups | `resolve_village` collapse + chips |
| 5 | `geography_key` ≠ `village_code`; the generator confuses them | `_village_code_as_geography_key` |
| 6 | Three money units live at once (lakh / rupees / crore), plus unverified and absent units | `_crore_conversion_for_single_village`; rules in each block |
| 7 | `year_key` is the FY start year. PMAY `financial_year` is CHAR `'2023-2024'`. Year windows differ per scheme | `refresh_scheme_years`; year gap/range guards; verifier year false-positive filter |
| 8 | MGNREGA's two facts are at source-row grain with no join; `job_cards_issued_total` is a stock | `_mgnrega_facts_joined`, `_rowgrain_no_aggregate`, `_mgnrega_split_fact_hint` |
| 9 | `scheme_code` for PMAY-G is `'PMAY'` | `_SHARED_TABLES` text |
| 10 | Focus Legacy producer-group names are place names ("Nongstoin PG") and `pg_name` carries a type suffix | `_drop_producer_group_names` (`pipeline.py:3699`) |
| 11 | A metric the user expects has no column: MGNREGA dues, admin expenditure, bank channel, CM Elevate money or dates | Pre-route refusals (`pipeline.py:1909-2080`), UNANSWERABLE few-shot examples |
| 12 | Resolver block catalogues are incomplete per scheme (CM Elevate lists 53 blocks while its data spans 66) | Cross-catalogue fallback (`entity_resolver.py:680-700`) |
| 13 | `semantic.metric_definitions` / `glossary` were documented as empty; loaded counts (16 / 18) were seen in the 09-08 log. `schema_for_developers.md` §10 is stale | — |

---

## 4. Failure analysis

### 4.1 What evidence exists

- **Tests.** Result of running them for this brief:
  - `python -m pytest` on the 16 pytest-style files: **423 passed, 0 failed** (13.5 s).
  - The 14 plain-script tests (`python tests/test_*.py`): **all exit 0**.
  - `tests/smoke_restructure.py` needs a server on `:8502` and was not run.
  - Every test is a **regression lock for a past incident**: regex, prompt text, catalogue or stubbed-LLM checks. **There is no golden set** of questions with expected SQL or answers run against the real models and database. The KPI workbooks in `data/reference/*_kpi_use_cases.xlsx` have "Expected Answer" columns, but the `schema_context.py` docstring deliberately does not use them ("several are stale").
  - So the green suite says nothing about current NL→SQL accuracy.
- **`logs/query_audit.jsonl`:** 3,141 turns from 2026-08-29 to 09-24. It records route, schemes, row_count and latency, **but not the SQL**.
- **uvicorn logs** (`logs/*.log|*.err`, about 24K lines concatenated): repair-loop warnings, tracebacks, fallbacks.

**Audit profile:**

| Route | Turns | Share |
|---|---:|---:|
| data | 1,358 | 43% |
| **clarification** | **1,119** | **36%** |
| knowledge | 481 | 15% |
| edge | 156 | 5% |
| denied | 27 | 1% (19 geography, 8 granularity) |

- Among data answers:
  - 108 returned **0 rows** (8%);
  - 67 have **empty `schemes`**, which is the shape of the "couldn't build a working query" fallback.
- Data latency (including cache hits): p50 1.6 s, p90 3.5 s, **p99 8.9 s, max 36 s**.

**Log counts:**

| Signature | Count |
|---|---:|
| `SQL failed (attempt n/4), repairing` | 172 |
| `data path failed → trying KB fallback` (repair budget exhausted) | 34 |
| `compose_response` misquote / dropped-metric / hedge corrections | 14 |
| `OSError: [WinError 121] semaphore timeout` (DB connect from dev box) | 66 |

### 4.2 Failure categories, ranked by frequency in the repair loop

The 172 repair events were categorised by their error text.

#### A. Semantic verifier flags: ~87 repair events, and the top cause of hard failures

Mostly **false positives from the 4B verifier**, especially "Check 2" (resolved entities); about 89 check-2 flags against 23 check-1 and 1 check-3. Real examples from the logs:

- *"RESOLVED ENTITIES specifies year_key = 2025, but the question asks for 'FY 2025-26' … should be year_key = 2026"*. Wrong: FY is stored by its start year. This led to `_verifier_year_complaint_is_false`.
- *"The SQL joins 'curated.v_cm_elevate' directly to itself (via the WHERE clause filtering on its own columns) which is explicitly prohibited"*. Nonsense, and it caused **2 hard failures** (budget exhausted → KB fallback).
- *"The SQL does not join any tables, but the question asks for investment advice"*, labelled Check 1.
- *"…includes a GROUP BY clause on lgd_district. This violates the grain…"*
- *"RESOLVED ENTITIES block is empty, but the question explicitly asks for 'Meghalaya' … and 'all financial years'"*. This is the chip text "all of Meghalaya, all years" being read as an entity; it led to a calibration example (`prompt_builder.py:491-504`).

Some flags are **true positives the generator could not fix**:
- *"RESOLVED ENTITIES lists UPPER(assembly_constituency_name) = UPPER('MAWLAI'), but the SQL filters on lgd_block = 'MAWLAI' instead"*.
- The question (audit 2026-09-15 22:50:58) was *"give me total beneficiaries in MAWLAI for MGNREGA across all financial years, the assembly constituency, not another area type, within the MAWLAI block"*.
- All 4 attempts kept the wrong filter → KB fallback → a "data" answer with `schemes=[]`.

**Root cause.** A small model is used as a judge with long natural-language rules. Every new false positive is patched with a suppression regex or another calibration example, so the verifier's reliability depends on an ever-growing prompt and filter list.

#### B. Hallucinated or wrong-table columns: 42 events, about 15 of the 34 hard failures

These are PostgreSQL `UndefinedColumnError` / `AmbiguousColumnError`:

- `column "admin_recurring_exp" does not exist`: 7 hard failures.
  - Question: *"what was the admin recurring expenditure in demdema in mgnrega?"* (2026-09-17 18:28-18:29).
  - A metric that does not exist was invented as a column.
  - **The same error came back on all 4 attempts**, because the repair did not converge.
  - The same session then asked the same question **13 times in 70 s**, cycling through block/village chips.
  - Since then a pre-route refusal was added: `_ADMIN_EXPENDITURE_REQUESTED`, `pipeline.py:1997, 7322`.
- `column "lgd_block" does not exist`: 4 hard failures (2026-09-09 11:49).
  - Question: *"give me block wise only in ekh for MGNREGA across all financial years"* (answered from the KB instead).
  - This is the shape of the generator picking `v_district_year_summary` (no block column) for a block breakdown. The SQL is not logged, so this cannot be confirmed.
- `column e.person_days does not exist`, `e.lgd_block`, `e.financial_year_short`: MGNREGA split facts. Employment measures were aliased to the expenditure view, which led to `_mgnrega_split_fact_hint` (`pipeline.py:5586`) and `tests/test_mgnrega_split_facts.py`.
- `column reference "financial_year_short" is ambiguous`: a join of two views without qualifying the column.

**Root cause.** Column knowledge is spread across prose rules, the live column list and few-shot examples, all in one long prompt. The generator mixes up columns that live on sibling views. When the requested metric does not exist, it invents one instead of refusing, unless an UNANSWERABLE few-shot example or a pre-route regex happens to match.

#### C. Hierarchy and level mistakes on geography: ~28 events

- **Village placed at the wrong level:** 8 events. For example, `village_code = 273707` resolved but the SQL had `lgd_block = 'BETASING'`; `lgd_district = 'NONGLADEW'`; `lgd_district = 'MAWPREM'`; `lgd_block = 'BATABARI'`. One of these was a hard failure.
- **Village name used instead of its code:** 8 events. For example, codes 276536, 277212, 278111 and 1298478 were resolved, but the SQL filtered `lgd_village_name`. One was a hard failure.
- **'Meghalaya' state pseudo-filter:** 5 events, **all 5 hard failures**. The generator kept re-adding the filter in every repair.
- **AC + invented block filter.** RANGSAKONA: "no matching records" versus the real 146 villages (documented at `pipeline.py:5362-5371`).
- **Crore conversion on a single village:** 12 events (0.49 lakh shown as "0.00 crore").

**Root cause.** Resolution itself is right: the entity is resolved correctly. The generator then ignores the "MANDATORY" entity block and pattern-matches on names in the question text. Names are shared across village, block, AC and district.

#### D. Row fan-out and grain errors: 5 events plus prompt-level

`v_employment` JOIN `v_expenditure` fans out rows; the documented example turned 986,020 person-days into 109,448,220. Now caught by `_mgnrega_facts_joined`. Earlier incidents with a bare `SELECT job_cards_issued_total … LIMIT 1` led to `_rowgrain_no_aggregate`.

#### E. Clarification over-triggering and loops: the largest UX failure, and not visible in the SQL logs

- 36% of turns are clarifications. On 2026-09-09, 227 of 446 turns.
- **66 sessions** had three or more consecutive clarifications. **44 (session, question) pairs** were asked three or more times. The top one is *"all of meghalaya, all years"* asked **10 times as a standalone data question**: the reply to a scope pause arrived without its paused question.
- Users fighting the scope/year gate with out-of-range years (2026-08-30): *"wgh, 2023"*, *"wgh,1997-98"*, *"all,1989"*, *"all of meghalaya,1999"*, *"all,199"*.
- Village-name chip loops, fixed one by one. The comments give the history:
  - parenthesis stripping, "NONGCHRAM (I" (`pipeline.py:3581-3589`);
  - identical duplicate-village chips (`entity_resolver.py:1349-1359`);
  - the CM Elevate phantom year (`pipeline.py:3665-3672`).
- **Structural contributor.** `session.pending_scope_q` / `pending_village_hint` live only in the in-process `Session` object (`session_store.py:105-114`) and are **not** part of the persisted `ConversationState`. The service runs `--workers 2` per VM on two VMs, and nginx has a single upstream with no stickiness (`deploy/nginx/nginx-nlpservice.conf`). So a reply to a clarification can land on a worker that never saw the pause, and is then treated as a fresh fragment.

**Root cause.** Eight or more independent gates run in sequence, each tuned in isolation: scheme, Focus ambiguity, top-N, region, scope, year, tranche, entity ambiguity, level collision, AC drill-down. Together with resume state that does not survive switching workers, they compound into loops.

#### F. Empty or zero results reported as answers: 108 zero-row data answers

Examples:
- *"Give me the house status breakdown for Nongthymmai, RI MULIANG block…"*: a village and block mismatch.
- *"Compare PMAY performance between ekh and wgh for FY 2017-18"*.
- *"tranche 2"*, *"give in blocks"*: fragments after a follow-up rewrite.
- *"How many applicants are there in BATABARI under Agro Tourism Villa Scheme, PRIME …"*: a multi-sub-scheme list plus a block that was missing from the catalogue (fixed 09-18).

`compose_response` → `_no_data_answer` turns these into "no matching records", which the user cannot tell apart from a genuine zero.

#### G. Composer faithfulness: 14 corrected events

Misquoted numbers (4), dropped metrics (3) and hedging over real values (7) were caught by `_answer_numbers_faithful`, `_answer_covers_metrics` and `_HEDGE_RE`, which triggers a strict retry and then the deterministic fallback (`pipeline.py:6723-6784`). This works as designed, but each case costs an extra model call.

#### H. Infrastructure and timeouts

- 66 `WinError 121` DB connect timeouts from the development machine to `10.48.242.4` (`asyncpg pool.acquire → connect`). Not a code defect, but there is no retry.
- One "transient gateway" re-raise.
- No `ModelBusyError` was seen in the logs; this was single-user testing.
- Worst-case budget: 4 × (30 s generate + 15 s verify) + classifier and composer calls can exceed the 60 s request ceiling, so a long repair ends as a 504, not as the KB fallback.

### 4.3 Hard failures (the 34 `data path failed`), grouped

| Final error | Count | Category |
|---|---:|---|
| `admin_recurring_exp` does not exist | 7 | B (non-existent metric) |
| Meghalaya pseudo-filter | 5 | C |
| `lgd_block` does not exist | 4 | B (wrong view) |
| Verifier flags: MAWLAI AC-vs-block (2), CM Elevate "self-join" (2), EKH district omitted, "empty entities / Meghalaya", "last three years", year_key IN, year_key = 4, multi-view join, "investment advice" | ~12 | A |
| Village at wrong level / name instead of code | 2 | C |
| `e.person_days`, `e.lgd_block`, ambiguous `financial_year_short` | 3 | B |

---

## 5. Gaps and risks

### 5.1 Hardcoding that breaks when growing to 25 schemes

1. **Scheme registries are hand-enumerated.** Adding a scheme means editing about 13 places:
   - `annotations._SCHEME_DIRS` / `_FEW_SHOT_FILE` / `_FK_FILE`
   - `entity_resolver._RESOLVER_FILE` / `_ACTIVITY_VIEWS`
   - `schema_context.SCHEME_CATALOG` / `SCHEME_METRICS` / `_SCHEME_BLOCKS`
   - pipeline scheme patterns, fuzzy aliases, `_X_ONLY_TERMS`, year tables, blurbs, cross-scheme money SQL
   - `followups.py` banks
   - `edge.py` regexes and starters
   - `prompt_builder._scheme_of` / `_X_EXACT`
   - `schema_introspect` name maps
   - `kb_ingest._SOURCES`
   - `rag._SCHEME_DOC_NAMES`
   - `auth.ROLE_PERMISSIONS` (every role's scheme list)
   - `web/ai_query.html` cards

   Count of literal scheme names per file: `pipeline.py` 569, `schema_context.py` 201, `edge.py` 136, `ai_query.html` 134, `followups.py` 120, `auth.py` 48. Hardcoded year text also exists, e.g. `edge.py:563` "FY 2024-25 and 2025-26".
2. **Prompt size grows linearly with schemes.** Per-scheme prose blocks are 10-22K characters. Six schemes already produce a ~32K-token cross-scheme prompt before the live-schema block. When `classify_scheme` cannot parse its output it returns **all schemes** (`pipeline.py:3562`), and `few_shot_examples` adds 5 examples **per scheme**. At 25 schemes a fallback or cross-scheme prompt would be over 130K tokens, well past what a 30B coder handles reliably and very likely past the deployed context length.
3. **Name collisions multiply with the scheme count.** Scheme-vs-scheme ambiguity is handled pair by pair: Focus vs Focus Plus (`_is_ambiguous_focus`), CM Elevate vs Legacy (`_pin_cm_elevate_dataset`), sericulture spelling, and "every regex whose scheme name is a prefix of the new one". This is an O(n²) set of special cases.
4. **Scheme-specific repair guards and gates live in shared code:** Focus Plus tranche gate, 12.5K cohort, verification status, CM Legacy "not held" list, MGNREGA crore/join guards, Focus Legacy AC join. Each new scheme's data quirks will add more of them to `pipeline.py`.
5. **Unused SME artefacts.** `*_classification_rules.yaml`, `*_default_rules.yaml`, `*_response_template.yaml` and `*_schema_partitions.yaml` are **not loaded** (`annotations.py:8-24`). Their content was copied by hand into `schema_context.py` prose, which drifts; the docstring says "do not let it drift".
6. **Per-scheme copies of the same geography.** Twelve districts are duplicated in six resolver YAMLs, and block coverage differs per file. The database already holds the truth in `dim_geography`.
7. **Auth geography regex only knows `lgd_district` / `lgd_block`** (`auth.py:199-203`). Focus Plus block filters use `block_name_raw`, and AC filters use `assembly_constituency_name` or `ac_name`. Those escape both the block-scope check and granularity inference: `GROUP BY block_name_raw` is inferred as "state" grain, which lets a district-capped role see block-level data.
8. **Stale docs.** `docs/ARCHITECTURE.md` and `DATA_MODEL.md` describe two schemes and `backend/` paths.

### 5.2 Error handling, logging and retries

- **No correlation in logs.** Pipeline log lines carry no request id, user or question. The failing SQL is never logged at any attempt; only the error text is. `app.conversation_turns.sql` stores only the final successful SQL. The JSONL audit has no SQL. Failures cannot be reproduced from the logs; this brief had to match timestamps by hand.
- **Repair loop has no convergence detection.** Identical SQL or an identical error is retried up to 4 times (§4.2 B), and the repair prompt drops the few-shot examples.
- **No retry on transient model or DB errors.** `call_model` retries only when guided decoding is rejected (`llm.py:209`). An `httpx` timeout or 5xx is re-raised immediately, and so is a DB connect timeout (`db.fetch_rows` / `run_readonly`).
- **Silent degradations that are logged as warnings but not surfaced in `/metrics`:** verifier failure → "no issue"; live schema load failure → prompt falls back to hand-written text (`prompt_builder.py:82-84`); context layer failures.
- **Dead config.** `SQL_GENERATION_MAX_RETRIES` is not read anywhere; the loop hardcodes `max_repairs=3` (`pipeline.py:6193`).
- **Broad catch.** `except Exception` in `_run_pipeline` (L7471) sends programming bugs (KeyError, TypeError) to the KB fallback, where they look like "couldn't answer".
- **No accuracy telemetry.** Nothing records verifier-flag rate, repair count per request, or zero-row rate per scheme.

### 5.3 Security gaps relevant to generated SQL

- **One DB role for everything.** The single `DATABASE_URL` pool runs LLM-generated SQL and the app's own writes. The deployed role `megh_app` (`deploy/sql/01_create_megh_app_role.sql`) has SELECT on **all** of `curated` and `semantic` and **SELECT/INSERT/UPDATE/DELETE on `app`**.
- **No table allowlist.** `_assert_safe` checks only the leading keyword and write keywords. A generated `SELECT … FROM app.users` (password hashes), `app.conversation_turns` (other tenants' chats), or `curated.fact_focus_legacy_disbursement` (unmasked account numbers) would pass every code check. The privacy boundary is prompt text only.
- **Unguarded functions.** Functions such as `pg_sleep`, `pg_read_file` or `current_setting` are not blocked. This is limited by the 15 s statement timeout and by role privileges.
- **TLS verification off by default.** `verify=False` is used for the model gateway when no CA bundle is configured (`llm.py:39-77`).

### 5.4 Scaling to ~200 concurrent officers

The design target in `docs/ARCHITECTURE.md` is 20-40 concurrent users and 200 daily active users, not 200 concurrent.

| Concern | Evidence | Effect at 200 concurrent |
|---|---|---|
| Model gateway is the bottleneck | Semaphore of 24 per worker (`llm.py:101`, `config.py:83`) × 2 workers × 2 VMs = 96 slots, all on one vLLM box; 4-13 calls per data request; SQL prompts of 3-7K tokens (up to 32K) | 200 × ~5 calls ≈ 1,000 queued calls against 96 slots. The 20 s queue timeout will shed most with 503s. Needs gateway replicas and a lower call count per request |
| Clarification state per worker | `pending_scope_q` only in process; no sticky upstream | Wrong merges and resume loops grow with the number of workers |
| Caches, sessions and rate limit per worker | `cache.py`, `semantic_cache.py`, `session_store.py`; Redis optional; `RATELIMIT_BACKEND="memory"` | Low hit rate; inconsistent follow-up context |
| Rate limit per IP: 30 req/min on `/api/query*` (`middleware/rate_limit.py:34`) | Government offices sit behind a NAT or proxy | Many officers share one IP and get throttled together |
| DB pool 10-30 per worker, on a shared DB box | `config.py:133-134` | 4 workers × 30 = 120 connections against a box that also serves pgAdmin; entity resolution adds DB round-trips before SQL runs |
| Result payload | `data: rows` returns up to 1,000 rows, and the LIMIT can be skipped (see §2.3) | Large JSON bodies |
| Local embeddings on CPU | `fastembed` on one worker thread per worker (semantic cache, KB, conversation memory) | CPU contention under load |
| Fire-and-forget persistence | `asyncio.create_task` in `conversation_store._spawn` | Lost on worker restart; no back-pressure |
| Request ceiling 60 s vs worst-case chain | §4.2 H | 504s under load when the repair loop runs |

---

## 6. Suggested priorities

These follow from the evidence above. None of them has been done.

1. **Observability first.** Add a request id to every pipeline log line, log each attempt's SQL and error, and store the attempts in `app.query_audit`. Add metrics for repair count, verifier-flag rate and zero-row rate per scheme.
2. **An accuracy benchmark.** Build a golden set per scheme (question → expected SQL result) from the few-shot YAMLs and the KPI workbooks, and run it against the real models and database in CI.
3. **Database least privilege.** Use a separate read-only role for generated SQL (`curated` views only; no `app`, no privacy tables), plus a table allowlist parsed from the SQL.
4. **Move clarification resume state into `ConversationState` or Redis**, and consolidate the gates into one planner so users are not asked several questions in a row.
5. **Replace the verifier's prose judging** with deterministic checks (parse the SQL and assert the resolved entities, tables and columns), or restrict it to high-precision checks.
6. **Drive schemes from a registry** (one YAML or database table per scheme holding patterns, years, AC capability and privacy tables), and retrieve schema context per table and column instead of per-scheme prose, so prompt size stays flat as schemes are added.
