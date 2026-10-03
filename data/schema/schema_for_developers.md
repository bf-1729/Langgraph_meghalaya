# Megh One AI — Database Schema

**Developer reference.** The structure below is the live `information_schema` extract taken
**2026-08-24** (`data-1787574891914.csv`, 204 columns across 16 objects). It replaces the
MGNREGA-only schema this document described until 2026-08-21.

| | |
|---|---|
| Database | `megh_db` |
| Host | `10.48.242.4:5432` |
| Server | PostgreSQL **18.4** (Ubuntu) |
| Admin UI | `http://10.48.242.4:8080/pgadmin4` |
| Schemas | `raw` / `staging` / `curated` / `semantic` / `meta` |
| Schemes loaded | **MGNREGA** and **PMAY-G** |
| Structure verified | 2026-08-24 (information_schema extract) |
| Volumes / totals verified | 2026-08-20 (MGNREGA only — see §8) |

Read access for applications should use the **`megh_readonly`** role, which has `SELECT` on
`curated` and `semantic` **only** — it cannot see `raw`, `staging` or `meta`, and cannot write
anywhere. Ask the DBA for its password; do not connect applications as `postgres`.

### What changed on 2026-08-24 — read this if you have seen the old version

1. **PMAY-G is now in the database.** It was previously flat Excel only. New: `fact_pmay_house`,
   `dim_pmay_house_status`, `v_pmay`, `v_pmay_monthly_sanctions`.
2. **`dim_scheme` exists.** The old §12 listed it as a gap. It carries `money_unit` and
   `time_semantics` per scheme — the unit of a money column is now a *database* fact, not a
   YAML convention.
3. **Two cross-scheme views exist** — `v_cross_scheme_money_district_year` and
   `v_cross_scheme_village_coverage`. Cross-scheme questions have a sanctioned answer path.
4. **Both MGNREGA facts gained `source_system`.** Defaulted to `'mgnrega'`; `'pmay'` on
   `fact_pmay_house`.
5. **PMAY does not use `alias_key`.** It joins geography directly, and also carries a
   denormalised `village_code`.

### Provenance and its limits

The 2026-08-24 extract carries **structure only**: table, column, type, length, precision,
nullability, default, PK flag, FK target. It does **not** carry row counts, schema
qualification, indexes, check constraints, or `numeric` *scale*.

Therefore, in this document:

- **Column lists, types, nullability, defaults, PKs and FKs** — from the 2026-08-24 extract. Trust these.
- **`curated.` / `raw.` prefixes** — carried forward from the 2026-08-20 read, not re-proved by the extract.
- **`numeric` scales** (`(16,2)`, `(14,4)`) — carried forward from 2026-08-20; the extract gives precision only.
- **Row counts, totals, indexes and every figure in §8** — 2026-08-20, **MGNREGA only**.
- **PMAY row counts and totals** — **not verified against the database.** Any PMAY volume must
  be measured before it is published. Nothing in this document asserts one.

---

## 1. Read this first — six rules

These are not style preferences. Breaking any of them produces a wrong number, and rules 1–5
have already produced a wrong number in this project's history.

**1. Never join the two MGNREGA fact tables to each other.**
`fact_mgnrega_employment` and `fact_mgnrega_expenditure` both allow multiple rows per
(year, village). A row-level join multiplies rows and inflates every total. There is
deliberately **no foreign key** between them — the extract confirms this still holds. For
cross-KPI questions use `curated.v_district_year_summary`, or aggregate each fact independently
to the same grain and then join the aggregates.

**2. Both MGNREGA facts are at SOURCE-ROW grain. Always aggregate before reporting.**
One row = one row of the source workbook, not one village-year. 1,864 employment village-years
and 157 expenditure village-years legitimately carry more than one row. This is data, not
corruption — `SUM ... GROUP BY` reproduces the source totals exactly.

**3. Money units differ by scheme — read `dim_scheme.money_unit`, do not assume.**
MGNREGA expenditure is in **LAKH RUPEES** (divide by 100 for crore). PMAY `sanctioned_amount`,
`amount_released` and `amount_pending` are in **RUPEES**. The `v_pmay_monthly_sanctions` and
`v_cross_scheme_*` views pre-convert to **CRORE** (`sanctioned_cr`, `released_cr`,
`amount_crore`). Three units are live at once. State the unit in every answer.

**4. `job_cards_issued_total` is a cumulative STOCK, not a flow.** Never sum it across financial
years. The four-year sum is 2,128,714; the actual current stock is **520,999** (2025-26). Report
one year at a time.

**5. Count villages with `village_code` or `geography_key`, never with a name.** 345 village
names are shared by more than one village. Name-based counts give ~6,676; the correct figure is
6,419.

**6. PMAY and MGNREGA are at different grains — never join their facts directly.**
`fact_pmay_house` is one row per **house**; the MGNREGA facts are one row per **source row** of
a village-year workbook. There is no FK between them and no meaningful row-level join. Use
`v_cross_scheme_money_district_year` (district × year) or `v_cross_scheme_village_coverage`
(village), which aggregate each side independently first.

---

## 2. Layer architecture

| Schema | Purpose | App access |
|---|---|---|
| `raw` | Source records verbatim as JSONB. Append-only. Rebuild source for everything else. | none |
| `staging` | Quarantine — rows that failed validation, with a reason code. | none |
| `curated` | Typed, FK-linked facts and conformed dimensions. **The query surface.** | `SELECT` |
| `semantic` | What the curated layer *means*: units, additivity, metrics, join graph, glossary. | `SELECT` |
| `meta` | Ingestion audit, lineage, layer register, reconciliation. | none |

---

## 3. Star schema

Three facts, five dimensions, one bridge — as of the 2026-08-24 extract.

```
                dim_scheme                      dim_year
              (scheme_key)                     (year_key)
                     │                      ▲      ▲      ▲
          scheme_key │           year_key   │      │      │ year_key
                     ▼                      │      │      │
             fact_pmay_house ───────────────┘      │      └─── fact_mgnrega_expenditure
                  │      │                         │                      │
       status_key │      │ geography_key           │ year_key             │ geography_key
                  ▼      │                fact_mgnrega_employment         │
      dim_pmay_house_status      │                 │                      │
                                 │  geography_key  │  geography_key       │
                                 ▼                 ▼                      ▼
                              ─────────── dim_geography ───────────────────
                                                 ▲
                                    ┌────────────┴─────────────┐
                          dim_geography_alias        bridge_geography_source
                                    ▲                          ▲
                          alias_key │  (MGNREGA facts only)     │ first/last_year_key
                                    │                     → dim_year

    ✗  NO join edge between any two fact tables — by design (rules 1 and 6)
    ✗  fact_pmay_house has NO alias_key — PMAY resolves geography at ingest
```

`dim_geography` is the conformed dimension both schemes share. `dim_year` is shared but
optional for PMAY (`fact_pmay_house.year_key` is **nullable**; the MGNREGA ones are `NOT NULL`).

---

## 4. Dimensions

### `curated.dim_scheme` — 8 columns · NEW 2026-08-24

Scheme-level metadata. **This is the authority on a scheme's money unit and time semantics** —
read it rather than hardcoding either.

| Column | Type | Null | Notes |
|---|---|---|---|
| `scheme_key` | `smallint` | NO | **PK** |
| `scheme_code` | `varchar(20)` | NO | short code, e.g. the value surfacing as `scheme_code` in the cross-scheme view |
| `scheme_name` | `text` | NO | full name |
| `ministry` | `varchar(100)` | YES | |
| `grain_note` | `text` | NO | what one fact row means for this scheme |
| `money_unit` | `varchar(20)` | NO | **read this before formatting any amount** (rule 3) |
| `time_semantics` | `text` | NO | what the year on a row means — MGNREGA reporting year vs PMAY sanction FY |
| `created_at` | `timestamptz` | NO | `now()` |

Only `fact_pmay_house` carries a `scheme_key` FK. The MGNREGA facts identify their scheme
through the `source_system` default instead.

### `curated.dim_pmay_house_status` — 5 columns · NEW 2026-08-24

Construction-stage lookup, with the two roll-ups precomputed so the application never
string-matches a status name.

| Column | Type | Null | Notes |
|---|---|---|---|
| `status_key` | `smallint` | NO | **PK** |
| `status_name` | `varchar(60)` | NO | the stage as recorded |
| `is_completed` | `boolean` | YES | **use this for "completed houses"**, not `status_name = '...'` |
| `is_in_progress` | `boolean` | YES | |
| `created_at` | `timestamptz` | NO | `now()` |

Both flags are nullable — a status whose roll-up is undecided is `NULL`, not `false`. Write
`WHERE is_completed` (which excludes NULL), and treat `NOT is_completed` as *not* the complement.

### `curated.dim_year` — 9 columns · 4 rows

Conformed time dimension. The two MGNREGA workbooks disagree on format (`2022-23` vs
`2022-2023`); both normalise here.

| Column | Type | Null | Notes |
|---|---|---|---|
| `year_key` | `smallint` | NO | **PK.** FY start year, e.g. `2022` = FY 2022-2023 |
| `financial_year` | `char(9)` | NO | `'2022-2023'` — canonical, UNIQUE |
| `financial_year_short` | `char(7)` | NO | `'2022-23'` — UNIQUE |
| `start_year`, `end_year` | `smallint` | NO | |
| `start_date`, `end_date` | `date` | NO | 1 Apr → 31 Mar |
| `is_complete` | `boolean` | NO | default `true` |
| `data_quality_note` | `text` | YES | **Surface this with any answer covering that year** |

Two years carry notes: **2024-25** (employment appears to be a partial extract) and **2025-26**
(women's column is zero on every row).

> The 4 rows cover FY 2022-23 → 2025-26 and were counted for MGNREGA. **PMAY sanction dates
> outside that window will not resolve** — `fact_pmay_house.year_key` is nullable precisely so
> such a row can still load. Check for `year_key IS NULL` before reporting PMAY by year.

### `curated.dim_geography` — 13 columns · 7,364 rows

Conformed geography, keyed on the **LGD village code** — a national identifier, which is what
lets MGNREGA and PMAY be compared at all.

| Column | Type | Null | Notes |
|---|---|---|---|
| `geography_key` | `integer` | NO | **PK, surrogate** (`GENERATED ALWAYS AS IDENTITY`) |
| `village_code` | `integer` | NO | **Natural key**, UNIQUE. The LGD code |
| `canonical_village_name` | `varchar(200)` | YES | from the roster |
| `lgd_village_name` | `varchar(200)` | YES | |
| `lgd_block` | `varchar(150)` | YES | uppercased |
| `lgd_district` | `varchar(100)` | YES | uppercased |
| `entity_type` | `varchar(10)` | YES | `Village` (7,200) or `Ward` (164) |
| `ac_number`, `ac_name` | `smallint`, `varchar(100)` | YES | assembly constituency |
| `on_roster` | `boolean` | NO | default `false` — in the official roster vs only seen in a data file |
| `has_geo_conflict` | `boolean` | NO | default `false` — sources disagree on block/district (**16 rows**) |
| `geo_conflict_note` | `text` | YES | what disagreed |
| `created_at` | `timestamptz` | NO | `now()` |

> ⚠️ **`geography_key` is not stable across a reload.** Use `village_code` in anything that
> leaves this database — exports, vector-store payloads, cross-scheme joins. Every view exposes
> both columns, and `fact_pmay_house` carries `village_code` directly, so you can filter on it
> without a join.

Indexes (2026-08-20): `idx_geo_district`, `idx_geo_block`, `idx_geo_name`, `idx_geo_roster`,
`idx_geo_name_trgm` (GIN trigram, for fuzzy name lookup).

### `curated.dim_geography_alias` — 6 columns · 18,959 rows

Every spelling each source used for a village. `CHIRAKHAWA` and `CHIRAKAWA` both resolve to one
LGD code here.

| Column | Type | Null | Notes |
|---|---|---|---|
| `alias_key` | `bigint` | NO | **PK** |
| `geography_key` | `integer` | NO | FK → `dim_geography` |
| `source_system` | `varchar(20)` | NO | `EMPLOYMENT` / `EXPENDITURE` / `ROSTER` |
| `source_village_name` | `varchar(200)` | YES | as written in the source |
| `match_method` | `varchar(50)` | YES | `Direct - current LGD` / `Via shifted-village list` |
| `created_at` | `timestamptz` | NO | `now()` |

UNIQUE on `(geography_key, source_system, source_village_name)`. GIN trigram index on the name.

**Use this for name lookup. Never as a join key for aggregation.** Only the two MGNREGA facts
reference it; PMAY does not.

### `curated.bridge_geography_source` — 5 columns · 18,456 rows

Which villages appear in which source, and over which years. **Use this for coverage questions
instead of joining the facts.**

| Column | Type | Null | Notes |
|---|---|---|---|
| `geography_key` | `integer` | NO | **PK part**, FK → `dim_geography` |
| `source_system` | `varchar(20)` | NO | **PK part** |
| `first_year_key`, `last_year_key` | `smallint` | YES | FK → `dim_year` |
| `row_count` | `integer` | NO | default `0` |

Coverage as of 2026-08-20: **EMPLOYMENT 6,419 · EXPENDITURE 4,673 · ROSTER 7,364**. Whether
PMAY now registers a `source_system` here is **not shown by the extract** — check before relying
on the bridge for PMAY coverage, and use `v_cross_scheme_village_coverage` meanwhile.

---

## 5. Fact tables

### `curated.fact_mgnrega_employment` — 18 columns · 26,375 rows

**Grain: one row per source row of the employment workbook.** `(year_key, geography_key)` is
intentionally **not unique**.

| Column | Type | Null | Notes |
|---|---|---|---|
| `employment_fact_id` | `bigint` | NO | **PK** |
| `year_key` | `smallint` | NO | FK → `dim_year` |
| `geography_key` | `integer` | NO | FK → `dim_geography` |
| `alias_key` | `bigint` | YES | FK → `dim_geography_alias` |
| `benefit_to_households` | `numeric(14,4)` | YES | ⚠️ **UNVERIFIED — do not use** |
| `households_employed` | `integer` | YES | additive; sums to household-**instances**, not distinct households |
| `persons_employed` | `integer` | YES | same instance caveat |
| `person_days` | `integer` | YES | additive. **The headline output measure** |
| `households_completed_100_days` | `integer` | YES | additive |
| `job_cards_issued_total` | `integer` | YES | ⚠️ **STOCK — never sum across years** |
| `women_employment_provided` | `integer` | YES | ⚠️ **DEFINITION UNCONFIRMED — see §9** |
| `assembly_constituency_name` | `varchar(100)` | YES | |
| `assembly_constituency_name_number` | `varchar(120)` | YES | |
| `raw_id` | `bigint` | YES | FK → `raw.source_rows` — full lineage |
| `source_row_num` | `integer` | YES | audit |
| `batch_id` | `uuid` | NO | audit |
| `source_system` | `varchar(50)` | NO | default `'mgnrega'` |
| `ingested_at` | `timestamptz` | NO | `now()` |

### `curated.fact_mgnrega_expenditure` — 17 columns · 18,818 rows

**Grain: one row per source row of the expenditure workbook.** Same non-unique key by design.
**All amounts in LAKH RUPEES.**

| Column | Type | Null | Notes |
|---|---|---|---|
| `expenditure_fact_id` | `bigint` | NO | **PK** |
| `year_key` | `smallint` | NO | FK → `dim_year` |
| `geography_key` | `integer` | NO | FK → `dim_geography` |
| `alias_key` | `bigint` | YES | FK → `dim_geography_alias` |
| `unskilled_wage_exp` | `numeric(16,2)` | YES | **numerator of the 60:40 statutory ratio** |
| `semi_skilled_wage_exp` | `numeric(16,2)` | YES | counts on the **material** side of that ratio |
| `material_exp` | `numeric(16,2)` | YES | |
| `tax_exp` | `numeric(16,2)` | YES | |
| `admin_recurring_exp`, `admin_non_recurring_exp`, `admin_total_exp` | `numeric(16,2)` | YES | effectively unpopulated — non-zero on 1 row |
| `total_exp` | `numeric(16,2)` | YES | sum of components; 4,968 rows differ by ≤0.02 lakh rounding |
| `raw_id` | `bigint` | YES | FK → `raw.source_rows` |
| `source_row_num` | `integer` | YES | audit |
| `batch_id` | `uuid` | NO | audit |
| `source_system` | `varchar(50)` | NO | default `'mgnrega'` |
| `ingested_at` | `timestamptz` | NO | `now()` |

Both MGNREGA facts indexed on `year_key`, `geography_key`, `(year_key, geography_key)` and
`batch_id` (2026-08-20).

### `curated.fact_pmay_house` — 26 columns · NEW 2026-08-24 · row count not verified

**Grain: one row per sanctioned PMAY-G house.** Not a village-year grain — this is the finest
grain in the database, and the only fact with a within-year date.

| Column | Type | Null | Notes |
|---|---|---|---|
| `pmay_fact_id` | `bigint` | NO | **PK** |
| `source_house_id` | `integer` | NO | the source table's own id |
| `scheme_key` | `smallint` | NO | FK → `dim_scheme` |
| `year_key` | `smallint` | **YES** | FK → `dim_year`. **Nullable** — a sanction outside FY 2022-26 has no key |
| `geography_key` | `integer` | NO | FK → `dim_geography` |
| `village_code` | `integer` | NO | denormalised LGD code — filter on this, no join needed |
| `status_key` | `smallint` | YES | FK → `dim_pmay_house_status` |
| `reg_no` | `varchar(30)` | YES | beneficiary registration number |
| `sanction_no` | `varchar(60)` | YES | |
| `sanction_date` | `date` | YES | **the only true date in the database** |
| `sanction_month` | `date` | YES | month truncation of `sanction_date` — group on this, not on `date_trunc` |
| `house_alloted_to` | `varchar(40)` | YES | allottee category (drives the "women" grouping) |
| `sanctioned_amount` | `numeric(12,·)` | YES | **RUPEES.** Effectively a per-house constant — see §9 |
| `amount_released` | `numeric(12,·)` | YES | **RUPEES.** The measure that actually varies |
| `amount_pending` | `numeric(12,·)` | YES | **RUPEES.** NEW — was not in the flat source |
| `installments_paid` | `smallint` | YES | |
| `mapping_status_raw` | `varchar(60)` | YES | LGD mapping status as recorded |
| `mapping_category` | `varchar(20)` | YES | bucketed form of the above — **filter on this** |
| `mapping_confidence_pct` | `numeric(5,·)` | YES | mapping confidence |
| `is_placeholder` | `boolean` | NO | default `false` — ⚠️ **exclude from counts unless asked** |
| `completed_underfunded` | `boolean` | NO | default `false` — completed but released < sanctioned |
| `raw_id` | `bigint` | YES | FK → `raw.source_rows` |
| `source_row_num` | `integer` | YES | audit |
| `batch_id` | `uuid` | NO | audit |
| `source_system` | `varchar(50)` | NO | default `'pmay'` |
| `ingested_at` | `timestamptz` | NO | `now()` |

**Notably absent:** `beneficiary_name` and `father_mother_name`, which the flat source carried.
Personal names are not in the curated layer — do not write SQL that expects them.

Three flags encode judgements that used to be the application's problem — `is_placeholder`,
`completed_underfunded`, and `mapping_category`. Use them; do not re-derive them.

---

## 6. Views — query these, not the raw facts

### `curated.v_employment` — 21 columns
Employment fact + year + geography resolved. Includes `year_note` so a data-quality caveat
travels with the numbers. **Still source-row grain — aggregate before reporting.**
Drops `benefit_to_households`, `alias_key` and the audit columns.

### `curated.v_expenditure` — 18 columns
Expenditure fact + year + geography resolved. Lakh rupees. **Still source-row grain.**
Drops `admin_recurring_exp` / `admin_non_recurring_exp` (keeps `admin_total_exp`) and the audit
columns.

### `curated.v_district_year_summary` — 16 columns
**The only sanctioned way to combine the two MGNREGA facts.** Each is aggregated independently
to district × year, then full-outer-joined. Precomputes:

- `unskilled_wage_share_pct` — the **correct** MGNREGA Schedule I formula, `unskilled ÷ total`
- `cost_per_person_day_rupees`
- `employment_villages` / `expenditure_villages` — distinct village counts per side

### `curated.v_pmay` — 28 columns · NEW
PMAY fact + scheme + year + geography + status resolved. **House grain — one row is one house**,
so `COUNT(*)` is a house count and needs no aggregation first. Adds `status_name`,
`is_completed`, `is_in_progress`, `financial_year`, `financial_year_short` and the geography
names. Drops `scheme_key`, `mapping_status_raw` and the audit columns; keeps `is_placeholder`
and `completed_underfunded` so you can still exclude placeholders.

### `curated.v_pmay_monthly_sanctions` — 4 columns · NEW
Statewide PMAY sanctions by month: `sanction_month`, `houses_sanctioned`, `sanctioned_cr`,
`released_cr`. **Already in CRORE and already aggregated** — never `SUM()` its money columns
against the rupee columns of `v_pmay`. No geography breakdown; for district-level money use the
cross-scheme view.

### `curated.v_cross_scheme_money_district_year` — 6 columns · NEW
`scheme_code`, `year_key`, `financial_year_short`, `lgd_district`, `amount_crore`,
`measure_semantics`. **The sanctioned way to compare scheme spending.** Long format — one row
per scheme × district × year — with both schemes normalised to **crore**.

> `measure_semantics` exists because the two amounts are not the same kind of number: MGNREGA's
> is expenditure incurred, PMAY's is money released against sanctions. **Always carry that string
> into the answer.** A bare "MGNREGA vs PMAY spend" comparison without it is misleading.

### `curated.v_cross_scheme_village_coverage` — 4 columns · NEW
`village_code`, `lgd_district`, `pmay_houses`, `mgnrega_person_days`. One row per village, each
side aggregated independently. Answers "which villages get both / only one" without any
fact-to-fact join. Expect NULLs on either measure — that is the finding, not a defect.

---

## 7. Query patterns

**Aggregate before reporting — always (MGNREGA)**
```sql
SELECT y.financial_year_short, SUM(f.person_days) AS person_days
FROM curated.fact_mgnrega_employment f
JOIN curated.dim_year y USING (year_key)
GROUP BY 1 ORDER BY 1;
```

**MGNREGA expenditure in Crore, by district**
```sql
SELECT lgd_district, ROUND(SUM(total_exp)/100, 2) AS crore
FROM curated.v_expenditure
GROUP BY 1 ORDER BY 2 DESC;
```

**MGNREGA cross-KPI — via the summary view, never a fact-to-fact join**
```sql
SELECT financial_year, lgd_district,
       cost_per_person_day_rupees, unskilled_wage_share_pct
FROM curated.v_district_year_summary
WHERE year_key = 2025;
```

**PMAY house counts — one row is one house, exclude placeholders**
```sql
SELECT lgd_district,
       COUNT(*)                        AS houses_sanctioned,
       COUNT(*) FILTER (WHERE is_completed) AS houses_completed
FROM curated.v_pmay
WHERE NOT is_placeholder
GROUP BY 1 ORDER BY 2 DESC;
```

**PMAY money — rupees in the detail view, so convert explicitly**
```sql
SELECT financial_year_short,
       ROUND(SUM(amount_released)/1e7, 2) AS released_cr,
       ROUND(SUM(amount_pending) /1e7, 2) AS pending_cr
FROM curated.v_pmay
WHERE NOT is_placeholder AND year_key IS NOT NULL
GROUP BY 1 ORDER BY 1;
```

**PMAY by month — already aggregated, already crore**
```sql
SELECT sanction_month, houses_sanctioned, sanctioned_cr, released_cr
FROM curated.v_pmay_monthly_sanctions
ORDER BY sanction_month;
```

**Cross-scheme money — carry `measure_semantics` into the answer**
```sql
SELECT lgd_district, scheme_code, amount_crore, measure_semantics
FROM curated.v_cross_scheme_money_district_year
WHERE financial_year_short = '2024-25'
ORDER BY lgd_district, scheme_code;
```

**Cross-scheme coverage — which villages get both**
```sql
SELECT COUNT(*) FILTER (WHERE pmay_houses > 0 AND mgnrega_person_days > 0) AS both,
       COUNT(*) FILTER (WHERE pmay_houses > 0 AND mgnrega_person_days IS NULL) AS pmay_only,
       COUNT(*) FILTER (WHERE pmay_houses IS NULL AND mgnrega_person_days > 0) AS mgnrega_only
FROM curated.v_cross_scheme_village_coverage;
```

**Money unit — ask the database, do not hardcode**
```sql
SELECT scheme_code, money_unit, time_semantics, grain_note FROM curated.dim_scheme;
```

**Job cards — one year at a time**
```sql
SELECT SUM(job_cards_issued_total) AS stock
FROM curated.fact_mgnrega_employment WHERE year_key = 2025;   -- 520,999
```

**Village name lookup — through the alias table (MGNREGA spellings)**
```sql
SELECT DISTINCT g.village_code, g.lgd_village_name, g.lgd_district
FROM curated.dim_geography_alias a
JOIN curated.dim_geography g USING (geography_key)
WHERE a.source_village_name ILIKE '%chirak%';
```

**Lineage — trace any number to its source cell (works for all three facts)**
```sql
SELECT f.person_days, r.source_file, r.source_row_num, r.payload
FROM curated.fact_mgnrega_employment f
JOIN raw.source_rows r ON r.raw_id = f.raw_id
WHERE f.employment_fact_id = 1;
```

---

## 8. Verified totals — MGNREGA only, as of 2026-08-20

Every figure below was derived twice — once in pandas from the source workbooks, once from this
database — and the two agree exactly. **These predate the PMAY load; nothing here has been
re-run against the 2026-08-24 database.**

| Measure | Value |
|---|---:|
| Total expenditure, 4 years | **₹3,628.67 Cr** (362,867 lakh) |
| Person-days, 4 years | **90,915,181** |
| Household-instances, 4 years | **1,494,437** |
| Persons, 4 years | **1,865,234** |
| 100-day completions, 4 years | **171,125** |
| Job-card stock, 2025-26 | **520,999** |
| Villages with any MGNREGA activity | **6,491** |

Expenditure by year: 2022-23 ₹886.29 Cr · 2023-24 ₹759.61 Cr · 2024-25 ₹1,066.70 Cr · 2025-26 ₹916.06 Cr
Person-days by year: 25,596,237 · 27,264,054 · 14,667,872 · 23,387,018

**Reconciliation is exact and should be checked after every load:**

```sql
SELECT * FROM meta.v_reconciliation;
-- employment  26,375 raw = 26,375 curated + 0 quarantined
-- expenditure 18,818 raw = 18,818 curated + 0 quarantined
-- pmay        ? — confirm the PMAY line reconciles before publishing any PMAY figure
```

> **PMAY has no verified totals yet.** House counts, sanctioned/released/pending crore, and
> completion rates must be measured and double-derived before they are quoted anywhere.

---

## 9. Data caveats you must handle in the application

### MGNREGA

**`women_employment_provided` — do not publish any ratio from this column.**
The definition is unconfirmed and the data rules out both readings: it exceeds
`persons_employed` on **582 rows** (so it is not a headcount of persons) and exceeds
`person_days` on **143 rows** (so it is not person-days). The widely quoted *"0.9% women's share,
below the 33% statutory norm"* is this column divided by `person_days` and is **not supportable**.
It is zero on every row in 2025-26. Awaiting confirmation from the state MGNREGA cell.

**2024-25 employment is likely a partial extract.** Person-days −46.2%, 100-day completions
−91.9%, while expenditure *rose* 40% to its four-year peak and cost per person-day doubled to
₹727 against ₹279–392 elsewhere. Report the figures, but always with the caveat from
`dim_year.data_quality_note`.

**Wage-share compliance depends on the formula.** Using the correct `unskilled ÷ total`, two
districts fall below the 60% floor on the four-year aggregate — **South West Garo Hills 56.70%**
and **West Khasi Hills 58.15%** — and two of four years fall below statewide. Using the looser
`(unskilled + semi) ÷ total`, everything passes. Use the statutory formula.

**`benefit_to_households`** — null on 11,633 of 26,375 rows, values inconsistent with any
household count. Not exposed in the views. Do not use.

**Not available for MGNREGA:** pending dues / unpaid liabilities (`Due_*`), fund availability and
releases (`Total_Availability`, `Release_Current_Year`, `Opening_Balance_1`, `Balance`), and any
asset/works data. Questions about these must return *"not available in the current source"* —
never an improvised answer.

### PMAY

**`sanctioned_amount` is effectively a per-house constant** (₹130,000 in the flat source). A
question about total sanctioned money is a house-count question wearing a disguise, and
`amount_released` is the measure that actually varies. Whether the constant survived the load is
**not confirmed** — check the distribution before treating it as one.

**`is_placeholder = TRUE` rows are not real houses.** Exclude them from every count and every sum
unless the question is explicitly about data completeness.

**`year_key` is nullable.** Sanctions outside FY 2022-26 load with a NULL year. A `GROUP BY
year_key` silently drops them; a `JOIN dim_year` drops them too. Count them before reporting any
PMAY time series.

**`completed_underfunded` marks houses recorded complete with released < sanctioned.** It is a
real finding about the data, not a defect to filter away — surface the count when a question
touches completion or disbursement.

**`mapping_confidence_pct` and `mapping_category` exist because LGD mapping is imperfect.**
Low-confidence rows are still real houses; they are just less certain about *where*. Filter on
`mapping_category` for geography-sensitive questions, and say so in the answer.

**Personal names are not in the curated layer.** `beneficiary_name` and `father_mother_name` were
dropped at ingest. Do not build a beneficiary-lookup feature against this schema.

### Shared

**16 villages have conflicting block/district** between sources. Flagged
`has_geo_conflict = TRUE`, resolved to the roster's value, with the disagreement recorded rather
than hidden.

**Three money units are live at once** — lakh (MGNREGA facts), rupees (PMAY facts), crore
(monthly and cross-scheme views). Rule 3. Read `dim_scheme.money_unit`.

**`v_cross_scheme_money_district_year` compares unlike measures.** MGNREGA expenditure incurred
vs PMAY money released. `measure_semantics` says so on every row — pass it through.

---

## 10. Semantic layer — for the NL→SQL and retrieval work

| Table | Rows (2026-08-20) | Contents |
|---|---:|---|
| `semantic.table_catalog` | 9 | one row per curated object: role, grain, description, synonyms, live row count |
| `semantic.column_catalog` | 123 | per column: unit, `semantic_role`, `aggregation`, **`is_additive`**, `data_quality_note` |
| `semantic.join_graph` | 12 | permitted joins, harvested from real FKs. `is_prohibited` marks joins the generator must refuse |
| `semantic.metric_definitions` | *0 — to be seeded* | canonical SQL per business metric |
| `semantic.glossary` | *0 — to be seeded* | domain vocabulary → table/column |

> **These counts are stale.** The curated layer now holds **16 objects and 204 columns**, and the
> FK set has grown to 17 edges. Run `semantic.refresh_catalog()` and re-check before trusting the
> catalog for retrieval — a catalog that has not seen `fact_pmay_house` will silently answer PMAY
> questions from MGNREGA columns.

**`semantic.v_embedding_documents`** emits one embeddable text document per table, column, metric
and glossary term — with unit, additivity and data-quality warnings inline.

> **Embed from this view, not from a spreadsheet.** The published KPI glossary contains at least
> three errors we verified against source (job cards 4× overstated, the women's ratio, and
> four-year totals presented as single-year). Embedding the spreadsheet puts those errors into
> retrieval as authoritative; embedding this view means catalog corrections propagate
> automatically.

`semantic.refresh_catalog()` re-harvests structure, comments, FKs and row counts. Run it after
every migration and every load. Rows flagged `manually_edited` are never overwritten.

**Join graph — the prohibitions that must be encoded:**

| From | To | Status |
|---|---|---|
| `fact_mgnrega_employment` | `fact_mgnrega_expenditure` | **PROHIBITED** (rule 1) |
| `fact_pmay_house` | either MGNREGA fact | **PROHIBITED** (rule 6) |
| `fact_pmay_house` | `dim_geography_alias` | no such FK — PMAY has no `alias_key` |

---

## 11. Operational views

```sql
SELECT * FROM meta.v_layer_summary;    -- architecture, one row per layer
SELECT * FROM meta.v_reconciliation;   -- raw = curated + quarantined
SELECT * FROM meta.v_lineage;          -- which file produced which table
SELECT * FROM meta.v_access_matrix;    -- live permission check per role
SELECT * FROM staging.quarantine;      -- 0 rows as of 2026-08-20
```

`meta.v_access_matrix` reads permissions live from the catalog — it *proves* the read boundary
rather than asserting it. `megh_readonly` must show `can_select` on `curated` and `semantic`
only, and `can_write` nowhere.

---

## 12. Known gaps / roadmap

**Closed since the previous version:**
- ~~No `dim_scheme`~~ — exists as of 2026-08-24, with `money_unit` and `time_semantics`.
- ~~Onboarding beyond MGNREGA~~ — PMAY-G is loaded, with its own status dimension and two
  cross-scheme views.
- ~~Finest time grain is the financial year~~ — for **PMAY only**. `sanction_date` and
  `sanction_month` make monthly analysis possible on that scheme. MGNREGA is still FY-only.

**Open:**
- **PMAY volumes and totals are unverified.** Nothing has been double-derived. This is the next
  task, and until it is done no PMAY figure should leave the system.
- `semantic.metric_definitions` and `semantic.glossary` are **empty**, and the catalog counts
  predate the PMAY load. Re-harvest, then seed.
- **PMAY may not be registered in `bridge_geography_source`** — the extract does not show its
  `source_system` values. Confirm, or coverage questions will quietly answer for MGNREGA only.
- **MGNREGA has no `scheme_key`.** It identifies its scheme through a `source_system` default
  instead, so `dim_scheme` is not yet a uniform entry point across all three facts.
- `semantic.refresh_catalog()` still infers `subject_area` from a hardcoded name pattern; drive it
  from a source registry before the third scheme.
- No sub-region (Garo / Khasi / Jaintia) grouping — a 12-row district lookup would add it.
- The annotation layer under `Annotations/` still describes the pre-2026-08-24 world; see the
  change note there.

---

## 13. Change log

| Date | Change |
|---|---|
| 2026-08-20 | Initial load — MGNREGA employment + expenditure, 4 dimensions, 3 views. Totals double-derived and verified. |
| 2026-08-24 | **Rewritten from the live `information_schema` extract.** PMAY-G added (`fact_pmay_house`, `dim_pmay_house_status`), `dim_scheme` added, two cross-scheme views added, `source_system` added to both MGNREGA facts. Rule 3 rewritten for three money units; rule 6 added. §8 scoped to MGNREGA-only and marked as predating the PMAY load. |
