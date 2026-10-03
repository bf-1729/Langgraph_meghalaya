# Data Model — `megh_db`

*Conceptual map, reconciled on 2026-09-26. This replaces the earlier two-scheme version of this
file. Labels: **VERIFIED** (code, config or the doc cited) · **INFERRED** · **UNKNOWN — NEEDS
VERIFICATION**.*

**This is the conceptual layer.** The detailed column-level references are:

| Reference | Covers | Status |
|---|---|---|
| [data/schema/schema_for_developers.md](../data/schema/schema_for_developers.md) | MGNREGA + PMAY-G + shared dims + cross-scheme views (information_schema extract 2026-08-24) | **Stale for the four newer schemes.** The scheme READMEs cite a newer `SCHEMA_FOR_DEVELOPERS.md` (rewritten 2026-09-04, live extract 2026-09-22) that is **not in this repo**. DOCUMENTATION CONFLICT — NEEDS VERIFICATION |
| `data/<scheme>/README.md` + `*_schema_partitions.yaml` | Per-scheme profiling, verified facts, rules (SME contract) | Authoritative per scheme, with known stale spots. `data/pmay/README.md` still says "PMAY is not in `megh_db` yet" (§9), but `schema_for_developers.md` records PMAY loaded on 2026-08-24, and the code queries `v_pmay`. The READMEs cite `Annotations/…` and `datasets/…` paths that are not in this repo. The `annotations.py` docstring says `*_schema_partitions.yaml` describe pre-migration Excel table names |
| [app/schema_context.py](../app/schema_context.py) | What the SQL generator is told (tables, columns, rules) | Hand-written; must be kept in step with the above |
| Live DB (`schema_introspect`) | Real columns and FKs, loaded into the prompt at startup and shown by `GET /admin/schema` | Authoritative for "does this column exist" |

---

## 1. Database, schemas, ownership (VERIFIED)

- **Server:** PostgreSQL 18.4, database `megh_db` at `10.48.242.4:5432`.
- **Schemas:**

  | Schema | Role | Owner |
  |---|---|---|
  | `raw`, `staging`, `meta` | ingestion internals | ingestion team; the app never queries them (`_PREAMBLE`) |
  | `curated` | the star schema: dims, facts, `v_*` views | ingestion team; **read-only for the app** |
  | `semantic` | `table_catalog`, `column_catalog`, `glossary`, `metric_definitions`, `join_graph` (read by `schema_introspect`) | ingestion team |
  | `app` | `tenants`, `users`, `login_events`, `conversations`, `conversation_turns`, `query_audit` | **this app** (`appdb.ensure_schema`) |

- **On 2026-09-08** the live load reported 27 tables, 25 FK edges and 21 catalogue tables
  (TECHNICAL_BRIEF §3). The current counts are UNKNOWN.

## 2. Shared dimensions (VERIFIED from `schema_context._SHARED_TABLES` and `schema_for_developers.md`)

| Object | Key facts |
|---|---|
| `curated.dim_geography` | PK `geography_key` (surrogate, **not stable across reloads**). Natural key `village_code` (LGD, unique). `lgd_village_name` (mixed case), `lgd_block` / `lgd_district` (**UPPERCASE names; there is no district or block code**), `entity_type` (includes `'Unresolved'` placeholders), `ac_number`, `ac_name`, `on_roster`, `has_geo_conflict`. GIN trigram index on the name. About 7,364 rows as of 2026-08-24 |
| `curated.dim_geography_alias` | source spelling → `geography_key` (MGNREGA sources) |
| `curated.bridge_geography_source` | village × source coverage |
| `curated.dim_year` | `year_key` = **FY start year** (2023 ⇒ FY 2023-24), `financial_year` `'2023-2024'`, `financial_year_short` `'2023-24'`, `data_quality_note` |
| `curated.dim_scheme` | `scheme_key`, `scheme_code` (PMAY-G is **`'PMAY'`**), `money_unit`, `time_semantics` |

**Geography hierarchy.** State → 12 districts → C&RD blocks → villages.
- District and block exist **only as denormalised uppercase names**. There are no district or
  block tables.
- The hierarchy used for resolution (aliases, block → parent district, regions such as Garo
  Hills) lives in `data/<scheme>/*_entity_resolver.yaml`. **Each scheme has its own copy.**
- **Assembly constituency (AC)** cuts across blocks and districts. It lives on:
  - `dim_geography.ac_name`;
  - MGNREGA's `v_employment.assembly_constituency_name`.

  AC is queryable for MGNREGA, and for Focus Legacy and CM Elevate Legacy through a
  `dim_geography` join on `geography_key`.
- **Focus Legacy place facts (VERIFIED 2026-09-29, live DB and raw file):** every `pg_id` sits in
  exactly one district, block and village (0 span two), so per-place PG counts add up exactly
  (D-031). 88 rows (a few PGs per district) have no block in either source; 1,101 rows are
  `entity_type = 'Unresolved'` (no village, no AC). 12 districts, 56 blocks, 3,384 villages,
  55 constituencies, 11,906 PGs.

## 3. Per-scheme query surfaces (VERIFIED from `schema_context.py`)

| Scheme | Query this | Grain | Money | Years (`_SCHEME_DATA_YEARS` defaults; refreshed from the DB at startup) |
|---|---|---|---|---|
| MGNREGA | `v_employment`, `v_expenditure`, `v_district_year_summary` | source row (many per village-year) | **LAKH ₹** | 2022-23 … 2025-26 |
| PMAY-G | `v_pmay`, `v_pmay_monthly_sanctions` (statewide, already **crore**) | one house | ₹ | year_key 2017 … 2023, plus NULL |
| Focus Plus | `v_focus_plus` | one payment (member × tranche) | unverified (`dim_scheme.money_unit`) | 2022-23, 2025-26 |
| CM Elevate | `v_cm_elevate` (+ `dim_cm_elevate_scheme`, 15 rows) | one application | **none** | **none** |
| Focus Legacy | `v_focus_legacy` (+ `dim_producer_group`, `dim_pg_entity_type`) | one payment to a producer group | unverified; `amount_disbursed = no_of_pg_members × 5000` on every row | 2021-22, 2022-23, 2024-25, 2025-26 (**2023-24 absent**) |
| CM Elevate Legacy | `v_cm_elevate_disbursement` (+ `dim_cm_elevate_disb_scheme`, 13 rows) | one applicant's sanction and disbursement | ₹ (unverified) | 2024-25, 2025-26 (Sericulture rows have no year) |
| Cross-scheme | `v_cross_scheme_money_district_year` (MGNREGA + PMAY only, **crore**), `v_cross_scheme_village_coverage` | district × year / village | crore | – |

Always query the `v_*` views. They pre-join geography, year and scheme dims.
- `scheme_key` / `scheme_code` exist only on `dim_scheme` and on
  `v_cross_scheme_money_district_year`. Adding a scheme filter elsewhere errors.
- Per-scheme details: [SCHEMES.md](SCHEMES.md).

### Privacy boundary — never query these (VERIFIED: `schema_context.py`)
- `curated.fact_focus_legacy_disbursement` and `curated.bridge_pg_bank_history`: the unmasked
  `account_no` and `name_on_the_account`.
- `curated.fact_cm_elevate_disbursement`: applicant first, middle and last names.
- Focus Plus: `member_id`, `pincode` (person-level). Governed by rule 7 of
  `schema_for_developers.md`.

These are enforced **only by the prompt** today (KI-004).

**The same banking PII is committed to git.** `Focus Legacy to share to BLH.csv`, at the repo
root, is the raw source for Focus Legacy: 14,569 rows, with **14,491 unmasked all-digit
`account_no` values and 10,353 `name_on_the_account` values**. It was committed in `7064ab6`,
and `origin/main` points at that commit (KI-022).

## 4. Join model (VERIFIED)

- Each fact joins `dim_geography`, `dim_year` and `dim_scheme` by surrogate key.
- **No fact is ever joined to another fact:**
  - MGNREGA employment ⟂ expenditure: both are source-row grain, so a join fans out (986,020
    person-days became 109,448,220). Use `v_district_year_summary`, or one CTE per fact.
  - Scheme ⟂ scheme: use the cross-scheme views, or aggregate per scheme and combine at district
    level.
  - Focus Plus ⟂ Focus Legacy, and CM Elevate ⟂ CM Elevate Legacy: no shared key.
- Prohibited joins are loaded from `data/<scheme>/*_foreign_key_augmentation.yaml` into the
  prompt (`annotations.prohibited_joins_text`).
- Real FKs are introspected live from `pg_constraint`.
- `dim_producer_group` must be queried **alone**. Joining it back to the view double-counts
  repeat-paid groups.
- CM Elevate Legacy: never INNER JOIN `dim_year` yourself; it drops the Sericulture rows.

## 5. Rules that produce a wrong number if broken (cross-scheme; VERIFIED in `schema_context.py`)

1. **The whole dataset is Meghalaya.** Never filter on `'Meghalaya'` or `entity_type='State'`.
   Enforced by `_STATE_PSEUDO_FILTER`.
2. **Case:** `lgd_district` and `lgd_block` are UPPERCASE, so a Title-Case literal silently
   matches 0 rows. The app rewrites literals with `_uppercase_geo_literals`.
   - Exception: Focus Plus blocks use **`block_name_raw`** (Title Case, compared with `UPPER()`),
     because `lgd_block` is NULL on 15% of its rows.
3. **Count villages by `village_code` / `geography_key`, never by name.** 345 names are shared.
4. **`geography_key` ≠ `village_code`.** Enforced by the `_village_code_as_geography_key` guard.
5. **Years:** `year_key` is the FY start year. PMAY `financial_year` is `'2023-2024'`.
6. **Units:** never mix units in one SUM. MGNREGA money stays in lakh; there is no ÷100 for
   single villages.
7. **Stocks:** MGNREGA `job_cards_issued_total` is cumulative. Report one year and never sum
   across years.
8. **Row-grain views** need `SUM … GROUP BY`, never a raw row read (`_rowgrain_no_aggregate`).
9. **PMAY:** `WHERE NOT is_placeholder`; completion comes from the `is_completed` /
   `is_in_progress` booleans; exclude the 126 `sanctioned_amount = 0` rows from rates.
   `amount_released` is NULL on 336 non-placeholder houses (nothing released yet): COALESCE it
   to 0 in every release count or sum (VERIFIED 2026-09-28). `v_pmay` equals the raw
   `PMAY_FullyMapped_with_dates.csv` row for row (171,107 rows by `source_house_id` = raw `id`).
10. **Metrics that do not exist:** MGNREGA dues or pending liabilities, admin expenditure, bank
    channel, CM Elevate money or dates. Refuse; never estimate.
11. **`'Unresolved'` geography rows:** CM Elevate Legacy and Focus Legacy exclude them only for
    village-level questions. District and statewide totals keep them
    (`_cm_legacy_keep_unresolved_off_village`).

## 6. Database access layer (VERIFIED: `app/db.py`)

| Function | Use |
|---|---|
| `init_pool` / `ensure_pool` | asyncpg pool, min 10 / max 30 per worker; lazy rebuild if startup failed. Two timeouts, both easy to misread: **`DB_POOL_TIMEOUT=30` is the per-connection *connect* timeout** (passed through `create_pool(**connect_kwargs)`), **not** an acquire timeout. `pool.acquire()` is called with no timeout, so a saturated pool waits until the 60 s request ceiling (VERIFIED against the asyncpg signature, 2026-09-26). **`command_timeout` (15 s) is client-side:** asyncpg cancels the query and raises `asyncio.TimeoutError`; it is not a server `statement_timeout` |
| `run_readonly(sql)` | **the only path for LLM SQL**: `_assert_safe` + auto-`LIMIT 1000` |
| `fetch_rows` / `fetchrow` / `fetchval` | the app's own parameter-bound reads (entity resolution, year refresh, catalogue) |
| `execute` / `execute_script` | the app's own writes and DDL (`app.*` only) |

- **Connection role:** see ARCHITECTURE §5. The local `.env` uses `postgres`, and the
  recommended `megh_app` role has read/write on `app.*`.
- **UNKNOWN — NEEDS VERIFICATION:** whether a separate `megh_readonly` credential
  (`DATABASE_URL_READONLY`, referenced in `docs/Focus_Legacy_Fix_Verification.md`) is used
  anywhere. **No code in `app/` reads such a setting** (VERIFIED by grep).

### Startup-only snapshots (VERIFIED: `main.lifespan`)
Three things are read **once per worker at startup** and never refreshed:
- `refresh_scheme_years` fills `_SCHEME_DATA_YEARS`;
- `schema_introspect.load` loads the live columns, FKs and `semantic.*` catalogue;
- the YAML entity catalogues are loaded.

After the ingestion team adds a year or a column, or changes a view, **restart the service**.
Until then the year chips, the out-of-range guard and the prompt's column list are stale.
Villages are the exception: they are resolved live on every request.

### Connection budget (INFERRED from config)
Each worker's pool allows up to 30 connections. With 2 workers on each of 2 VMs, that is up to
120 connections to a DB host that is shared with pgAdmin and analysts. The server's
`max_connections` is UNKNOWN — NEEDS VERIFICATION.

## 7. App schema (VERIFIED: `app/appdb.py`)

Created idempotently at startup (`CREATE TABLE IF NOT EXISTS` + `ADD COLUMN IF NOT EXISTS`):

| Table | Holds |
|---|---|
| `app.tenants` | departments; default `RD` seeded |
| `app.users` | username, PBKDF2 `password_hash`, role, tenant, `districts`/`blocks`/`schemes` arrays, `granularity_cap`, `is_active`. Seeded from `app/users.yaml` if empty. **That file holds plaintext seed passwords** for `superadmin` and `rd-admin` (KI-023) |
| `app.login_events` | login, logout, fail and bootstrap events, with IP and user agent |
| `app.conversations` | one per session_id; `pinned`, `archived`, `summary`, `context_state` JSONB |
| `app.conversation_turns` | question, answer, final SQL, `response` JSONB (so a reopened chat can redraw its charts) |
| `app.query_audit` | per-turn audit (no SQL) |

Retention: `deploy/sql/02_retention_policy.sql` adds `app.purge_expired()`. Its windows are
placeholders, and whether it is scheduled is UNKNOWN.

## 8. Known data-load issues (from QA records; details in the docs cited)

- **Block missing for rows without a village code:**
  - Focus Legacy: 1,013 rows. **Fixed** by the ingestion team, 2026-09-25.
  - CM Elevate Legacy: 404 rows. **Fixed**.
  - Residual: 88 Focus Legacy rows whose block is blank in the source itself, with a question
    open on TC-F1. See `docs/Focus_Legacy_Fix_Verification_Results.md`.
- **Producer-group alternate spellings:**
  - The DB now has `curated.v_focus_legacy_pg_search` / `dim_producer_group_name_alias`.
  - **The chatbot does not use them yet** (PLANNED; KI-020).
- **Focus Legacy `block_name_raw` / `district_name_raw`** became NULL after the DB change. They
  are mapped to `lgd_*` by `_focus_legacy_geo_columns`.
