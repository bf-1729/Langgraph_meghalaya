# MGNREGA Annotation Layer — How the YAMLs Are Built

This folder holds the hand-curated annotation layer for the MGNREGA scheme of the
Megh One AI NLP-to-SQL bot. Everything here is derived from two Excel workbooks in
`quadrent/datasets/`. Nothing in this folder is generated at runtime — these files are
the reviewed, SME-owned contract that the retrieval and SQL-generation stages read.

---

## 1. What is in this folder

| File | Layer | Answers the question |
|---|---|---|
| `mgnrega_schema_partitions.yaml` | **Semantic schema** (referred to throughout as *stage2*) | *What columns exist, what do they mean, how are they aggregated?* |
| `mgnrega_classification_rules.yaml` | **Clarification rules** | *What is missing from this query, and what should the bot ask before answering?* |
| `mgnrega_default_rules.yaml` | **Default rules** | *What can be filled in silently, and what assumption must be stated back?* |
| `few_shot.yaml` | **Few-shot examples** | *What does a correct question-to-SQL pair look like for this scheme?* |
| `foreign_key_augmentation.yaml` | **Foreign-key augmentation** | *Which joins are permitted, and which must the generator refuse?* |
| `response_template.yaml` | **Response templates** | *How is the final answer worded, and what must be stated alongside the numbers?* |
| `mgnrega_entity_resolver.yaml` | **Entity resolver** | *The user typed "WKH", "2023-24", "Asimgre" — which stored value is that?* |
| `README.md` | This guide | *How do I build or regenerate any of these, and what must never change?* |

They are **companions, not duplicates**. Keep the split clean:

```
user question
   |
   v
[ clarification gate ] <-- mgnrega_classification_rules.yaml
   |  no year given, or "top performing" with no metric -> ask, and pause
   v
[ defaults ]           <-- mgnrega_default_rules.yaml
   |  top N -> 5, unit -> crore, no year -> FY 2025-26
   |  every fill is recorded as an assumption and stated back
   v
[ entity resolution ]  <-- mgnrega_entity_resolver.yaml
   |  "WKH" -> "West Khasi Hills" / "WEST KHASI HILLS"
   |  "2023-24" -> "2023-2024" (exp) or "2023-24" (emp)
   v
[ schema linking ]     <-- mgnrega_schema_partitions.yaml
   |  "spending" -> total_exp
   |  "people employed" -> employment_provided_persons
   v
[ SQL generation ] -> SQLGlot validation -> read-only execute
```

**Rule of thumb:** if the fact is about a *column* (meaning, type, unit, aggregation,
whether it is excluded) it belongs in stage2. If the fact is about a *value* (aliases,
casing, ambiguity, join keys, what to do when it is not found) it belongs in the
resolver.

---

## 2. The source data

Two workbooks, both under `quadrent/datasets/`. **Read-only. Never edit them.**

### 2.1 `Updated MNGREGA Expenditure WIth Year.xlsx`

- Sheet name: `Mactched Village MNGREGA` — note the typo. It is in the file. Do not
  "fix" it in code that reads by sheet name; read by index or match loosely.
- **18,818 rows x 14 columns**
- Grain: village x financial year expenditure record
- Warehouse table: `mgnrega_expenditure`

| Excel column | YAML `name` | Type | Role |
|---|---|---|---|
| `Exp_Unskilled_Wage` | `exp_unskilled_wage` | decimal | measure |
| `Exp_Semi_Skilled_Wage` | `exp_semi_skilled_wage` | decimal | measure |
| `Exp_Material` | `exp_material` | decimal | measure |
| `Exp_Tax` | `exp_tax` | decimal | measure |
| `Adm_Exp_Rec` | `adm_exp_rec` | decimal | **excluded** |
| `Adm_Exp_Non_Rec` | `adm_exp_non_rec` | integer | **excluded** |
| `Adm_Exp_Total` | `adm_exp_total` | decimal | **excluded** |
| `Total_Exp` | `total_exp` | decimal | measure (primary) |
| `Year` | `year` | string | time dimension |
| `Village_Clean` | `village_clean` | string | raw mapping field |
| `Village_Code` | `village_code` | integer | **join key** |
| `LGD_Village_Name` | `lgd_village_name` | string | geo dimension |
| `LGD_District` | `lgd_district` | string | geo dimension |
| `LGD_Block` | `lgd_block` | string | geo dimension |

### 2.2 `MGNREGA_Employment.xlsx`

- Sheet name: `Sheet1`
- **26,375 rows x 16 columns**
- Grain: village x financial year employment record
- Warehouse table: `mgnrega_employment`

| Excel column | YAML `name` | Type | Role |
|---|---|---|---|
| `Year` | `year` | string | time dimension |
| `Benefit to Households` | `benefit_to_households` | decimal | measure (**unit: thousands**) |
| `Employment Provided Households` | `employment_provided_households` | integer | measure |
| `Employment Provided Persons` | `employment_provided_persons` | integer | measure |
| `Employment Provided Total Person Days` | `employment_provided_total_person_days` | integer | measure |
| `Households Completed 100 Days Work` | `households_completed_100_days_work` | integer | measure |
| `Job Cards Issued Total` | `job_cards_issued_total` | integer | measure |
| `Women Employment Provided` | `women_employment_provided` | integer | measure |
| `Village` | `village` | string | raw mapping field |
| `Village.1` | `village_code` | integer | **join key** |
| `LGD_Village_Name` | `lgd_village_name` | string | geo dimension |
| `LGD_District` | `lgd_district` | string | geo dimension |
| `LGD_Block` | `lgd_block` | string | geo dimension |
| `Assembly Constituency Name` | `assembly_constituency_name` | string | political geo |
| `Assembly Constituency Name and Number` | `assembly_constituency_name_and_number` | string | political geo |
| `Match_Method` | `match_method` | string | ETL metadata |

> **The `Village.1` trap.** The employment sheet has **two columns both literally
> headed `Village`**. Pandas de-duplicates the second into `Village.1`. The first is the
> village *name*; the second is the village *code*. `Village.1` is not a real header — it
> is a pandas artifact. Any loader reading this file must map positionally or by dtype,
> not by trusting the header text. Getting this backwards silently destroys the join key.

---

## 3. Excel to YAML: the derivation pipeline

Do not hand-write numbers into the YAML. Every count, list and sample value in both
files must come out of a profiling run against the workbooks, so it can be re-derived
and audited later.

```
Step 0  Load          read both workbooks, no dtype coercion, keep NaN as NaN
Step 1  Normalise     snake_case every header -> YAML `name`, keep `source_column`
Step 2  Type          infer decimal / integer / string from dtype + value shape
Step 3  Classify      assign `category` and `nlp_sql_priority` per column
Step 4  Profile       per column: distinct count, nulls, min/max, 6 sample values
Step 5  Semantics     SME writes `meaning`, `synonyms`, `business_rules`, `unit`
Step 6  Values        distinct dimension values -> resolver canonical + alias tables
Step 7  Keys          test the join, count overlap/orphans, record the stats
Step 8  Diff          raw vs LGD name comparison -> variant patterns
Step 9  Gaps          nulls, ambiguity, disagreements -> known_gap / notes
Step 10 Emit          write stage2 YAML + resolver YAML, stamp last_generated
```

### 3.1 The profiling recipe

Steps 4, 6, 7 and 8 are one short script. This is the exact shape used to produce the
numbers in `mgnrega_entity_resolver.yaml`:

```python
import pandas as pd

EXP = "quadrent/datasets/Updated MNGREGA Expenditure WIth Year.xlsx"
EMP = "quadrent/datasets/MGNREGA_Employment.xlsx"

e = pd.read_excel(EXP)   # 18818 x 14
m = pd.read_excel(EMP)   # 26375 x 16

# Step 4 - per-column profile -> feeds stage2 type / sample_values
for col in e.columns:
    print(col, e[col].dtype, e[col].nunique(), e[col].isna().sum(),
          e[col].dropna().unique()[:6])

# Step 6 - dimension catalogue -> feeds resolver canonical lists
sorted(e.LGD_District.dropna().unique())          # 12, Title Case
sorted(m.LGD_District.dropna().unique())          # same 12, ALL CAPS
e.LGD_Block.nunique(), m.LGD_Block.nunique()      # 55 vs 56

# Step 7 - join health -> feeds join_keys.match_stats
ec = set(e.Village_Code.dropna().astype(int))
mc = set(m["Village.1"].dropna().astype(int))
len(ec), len(mc), len(ec & mc), len(ec - mc), len(mc - ec)

# Step 8 - raw vs LGD drift -> feeds village_normalisation.patterns_seen
raw, lgd = e.Village_Clean.astype(str), e.LGD_Village_Name.astype(str)
case_only = (raw.str.upper() == lgd.str.upper()) & (raw != lgd)
real_diff = (raw.str.upper() != lgd.str.upper())
```

### 3.2 Choosing `category` and `nlp_sql_priority`

| `category` | Use for | `sql_operations` |
|---|---|---|
| `expenditure_measure` / `employment_measure` / `benefit_measure` | numeric facts users ask totals of | SUM, AVG, MIN, MAX |
| `excluded_administrative_measure` | numeric but policy-excluded | `[]` |
| `time_dimension` | the financial year | WHERE, GROUP BY, IN |
| `geographic_dimension` | district / block / LGD village name | WHERE, GROUP BY, COUNT |
| `geographic_identifier` | `village_code` | WHERE, GROUP BY, COUNT DISTINCT — **never SUM** |
| `political_geography_dimension` | assembly constituency | WHERE, GROUP BY, COUNT |
| `mapping_field` | raw pre-LGD text | WHERE, GROUP BY (low priority) |
| `etl_metadata` | `match_method` | excluded from business answers |

`nlp_sql_priority` drives retrieval ranking, not correctness:
`critical` (users name it directly) > `high` > `medium` > `low` > `excluded`
(never retrieved, never emitted into SQL).

---

## 4. Anatomy of the entity resolver YAML

Every section, what it is for, and where its content comes from.

| Section | Purpose | Derived from |
|---|---|---|
| `scheme` | Partition key for Qdrant retrieval | Fixed: `mgnrega` |
| `tables` | Logical name to physical table | stage2 `datasets.*.table_name` |
| `join_keys` | The one safe join plus measured health | Step 7 |
| `region_groupings` | "Garo Hills" to its 5 districts | Domain knowledge over the 12 districts |
| `district_normalisation` | Canonical, casing variants, acronyms | Step 6 |
| `block_normalisation` | Canonical blocks plus coverage gap | Step 6 |
| `year_normalisation` | Cross-table FY format bridge | Step 6 |
| `village_normalisation` | Ambiguity, drift patterns, strategy | Steps 6 + 8 |
| `assembly_constituency` | Employment-only dimension plus null gap | Step 4 |
| `resolver_maintenance` | Provenance and regeneration trigger | Step 10 |

### 4.1 Non-negotiable resolver behaviours

1. **Resolve on `village_code`, never on a village name.** Names are not unique.
2. **Never match against `village_clean` / `village`.** Always go through
   `lgd_village_name`. The raw columns are pre-LGD text kept for traceability only.
3. **Ambiguous means ask, never guess.** If a village name maps to several codes and no
   district or block was given, return a did-you-mean list.
4. **Unknown means not-found, never invent.** Same `unknown_entity` contract as stage2.
5. **Append, never replace, on regeneration.** Old questions must keep resolving against
   older years and villages.

### 4.2 Alias table conventions

```yaml
canonical_form:
  matching_rule: case_insensitive_exact_match
  canonical:
    "Canonical Stored Value": ["VARIANT ONE", "SHORTCODE", "typo variant"]
```

- The **key** is the value as stored in the *canonical* table (expenditure, for geography).
- The **list** holds every other form: the other table's casing, acronyms, common typos.
- Acronyms are matched *after* stripping spaces and punctuation, and *before* fuzzy fallback.
- If a new value's acronym would collide with an existing one, **do not silently pick** —
  flag it for SME review.

---

## 5. Verified facts (profiled 2026-08-21)

Re-derived from the workbooks directly. Use these as the regression baseline when you
regenerate.

### 5.1 Shape and coverage

| Fact | Expenditure | Employment |
|---|---|---|
| Rows | 18,818 | 26,375 |
| Columns | 14 | 16 |
| Distinct village codes | 4,673 | 6,419 |
| Distinct LGD village names | 4,433 | 6,106 |
| Distinct blocks | 55 | 56 |
| Distinct districts | 12 | 12 |
| Distinct assembly constituencies | — | 56 |

- **Code overlap: 4,601.** Expenditure-only: **72**. Employment-only: **1,818**.
- Both code columns are already **int64** in the workbooks — no float artifacts.

### 5.2 Casing map — this is where joins break

| Field | Expenditure | Employment |
|---|---|---|
| `lgd_district` | Title Case | **ALL CAPS** |
| `lgd_block` | ALL CAPS | ALL CAPS |
| `lgd_village_name` | **Title Case (0% caps)** | **ALL CAPS (100% caps)** |
| `village_clean` / `village` (raw) | ALL CAPS | ALL CAPS |

### 5.3 Year formats

| Expenditure (canonical) | Employment (alias) |
|---|---|
| `2022-2023` | `2022-23` |
| `2023-2024` | `2023-24` |
| `2024-2025` | `2024-25` |
| `2025-2026` | `2025-26` |

Only these four years exist. A bare `2023` is ambiguous — ask, do not assume.

### 5.4 Name ambiguity

- **183** expenditure village names map to more than one code — worst: `Asimgre` to 6.
- **233** employment village names map to more than one code — worst: `ASIMGRE` to 7.

### 5.5 Raw vs LGD drift

| | Rows differing | Case-only | Real spelling change |
|---|---|---|---|
| Expenditure (`village_clean` vs LGD) | 18,818 (all) | 16,860 | **1,958** |
| Employment (`village` vs LGD) | 2,270 | **0** | **2,270** |

Observed patterns: case fold; parentheses added (`ANANGPARA CHRISTIAN` to
`ANANGPARA(CHRISTIAN)`); roman-numeral hyphenation (`ALLABAGRI I` to `ALLABAGRI-I`);
spacing collapse (`AMSOH RHONG` to `AMSOHRHONG`); double-letter corrections that only
*look* like case folds (`AGRENGGITTIM` vs `Agrenggitim` is GITTIM to GITIM); and genuine
renames (`CHAMBIL BADIMAGRE` to `Upper Chambil Badimagre`) that no formatting rule
catches — those need the code.

### 5.6 Nulls and zeros

| Column | Nulls | Reading |
|---|---|---|
| `benefit_to_households` | **11,633 of 26,375 (44%)** | missing, **not zero** |
| `assembly_constituency_name` (and `_and_number`) | 1,240 | exactly the shifted-village rows |
| everything else | 0 | — |

`match_method`: `Direct - current LGD` = 25,135; `Via shifted-village list` = 1,240.
The 1,240 AC nulls are **exactly** the 1,240 shifted rows — expected, not a bug.

`employment_provided_persons` is `0` in **7,171** rows. Those zeros are real recorded
zeros, not missing data. Do not filter them out of counts, but be aware they drag AVG down.

### 5.7 Measure ranges (sanity bounds)

| Measure | Min | Max | Mean |
|---|---|---|---|
| `total_exp` | 0 | 651.50 | 19.28 |
| `exp_unskilled_wage` | 0 | 496.20 | 12.28 |
| `exp_material` | 0 | 210.39 | 6.08 |
| `benefit_to_households` | 230 | 23,803.55 | 14,501.71 |
| `employment_provided_total_person_days` | 0 | 89,630 | 3,447 |
| `job_cards_issued_total` | 0 | 1,919 | 80.7 |

`exp_unskilled + semi_skilled + material + tax` reconciles to `total_exp` in all but
**1** row — the components are trustworthy, but `total_exp` stays the preferred field.

### 5.8 Cross-table agreement on the 4,601 shared codes

| Check | Disagreements |
|---|---|
| District (case-folded) | **0** |
| Block (case-folded) | **4** — codes 276472 / 276474 / 276645 (`SHALLANG` vs `MAWSHYNRUT`), 276888 (`NONGSTOIN` vs `RAMBRAI`) |
| Village name (case-folded) | **1** — code 276888 (`MAWPHANLUR` vs `MAWBYRKONG`) |

Geography is effectively consistent across the two tables. Expenditure is the canonical
side for all three.

---

## 6. Open issues in the current resolver

Found while profiling. Recorded here for the next revision; **the YAMLs were not modified.**

1. **The fallback join is unusable as written.** `join_keys.fallback_join` matches on
   `lgd_village_name`, but expenditure stores Title Case and employment stores ALL CAPS —
   0% overlap on a raw string compare. The fallback must upper-case both sides, and
   `lgd_district` too. Without this, all 72 orphan expenditure codes fail silently.

2. **The village-code dtype note is wrong.** The resolver warns the codes arrive as floats
   (`"272755.0"`). In both current workbooks they are `int64`. Harmless, but it will send
   the next maintainer looking for a bug that is not there.

3. **Neither file records that the grain is not unique.** `(village_code, year)` repeats
   **181** times in expenditure and **2,069** times in employment. Consequences:
   - `COUNT(*)` is *not* a village count — use `COUNT(DISTINCT village_code)`.
   - A village-level join on `village_code` alone **fans out** rows and inflates SUMs.
     Aggregate each side to the join grain *before* joining, or join on
     `(village_code, year)` with the year normalised first.

4. **The 44% null rate on `benefit_to_households` is undocumented.** stage2 says "NULL is
   missing, not zero" but never says how much is missing. `AVG` over it silently answers
   for a little over half the rows. Worth surfacing as a caveat in any answer that uses it.

5. **Employment-side ambiguity is missing.** The resolver documents the 183 ambiguous
   expenditure names but not the **233** on the employment side.

6. **`region_groupings` puts Ri Bhoi under `"Other"`.** Correct that it belongs to none of
   the three hills ranges, but "Other" is a poor label to surface to a user. Consider naming it.

7. **1,818 employment-only village codes are never mentioned.** The resolver documents the
   72 expenditure orphans but not the far larger reverse gap. An expenditure-side question
   about those villages must return "no expenditure data", not zero.

8. **The expenditure sheet name is misspelled** (`Mactched Village MNGREGA`). Any loader
   hardcoding the correct spelling will fail.

---

## 7. Regeneration checklist

Run this whenever the workbooks change — new FY, corrections, new columns.

- [ ] Re-run the profiling script in section 3.1 against both workbooks.
- [ ] Diff the new counts against section 5. **Every** change must be explained, not accepted.
- [ ] New district / block / year values: **append** to the canonical alias tables.
- [ ] New acronym: check it collides with none of the existing 12 before adding.
- [ ] Re-run the join health check; if overlap dropped, find out why before shipping.
- [ ] Re-run the ambiguity count; if it grew, the did-you-mean lists need updating.
- [ ] New column: add to stage2 with `meaning`, `category`, `nlp_sql_priority`,
      `sql_operations`, `synonyms`, six `sample_values`, and `business_rules`.
- [ ] Bump `resolver_version` / `schema_version`; update `last_generated` and
      `row_counts_at_generation`.
- [ ] Validate both files parse with `yaml.safe_load`.
- [ ] Review the two rule files for data-derived numbers that have moved: the four
      financial years, 12 districts, 164 wards, the 1,240 unassigned-constituency
      records, and the latest-year default. They do not regenerate from the
      workbooks, so nothing else will catch a stale figure.
- [ ] Re-run the golden question set end to end before promoting.

---

## 8. House rules

- The workbooks in `quadrent/datasets/` are **read-only inputs**. Never write to them.
- The two YAMLs are **independent**. A change to one must not silently require a change
  to the other; if it does, say so explicitly.
- Prefer **ask over guess** everywhere: ambiguous entity, ambiguous year, ambiguous
  "beneficiary" (households vs persons — still `SME_CONFIRMATION_REQUIRED` in stage2).
- Never delete a canonical value or alias. Data ages, and questions about old years must
  keep working.

---

## 9. Database alignment - the target moved from Excel to Postgres

Both YAMLs in this folder describe the **flat Excel world**: two standalone tables, two
year formats, mismatched casing, a village_code join that fans out. That world is now an
ingest-time artefact. The bot queries **`megh_db`** - see `SCHEMA_FOR_DEVELOPERS.md` at
the repo root for the full reference.

What the database normalises away:

| Excel-era problem | How the database solves it |
|---|---|
| `2022-23` vs `2022-2023` | `curated.dim_year` holds both forms, PK `year_key` = FY start year |
| Title Case vs ALL CAPS geography | `curated.dim_geography`, uppercased, PK `geography_key`, natural key `village_code` |
| Village name is not unique | resolve through `curated.dim_geography_alias` (18,959 spellings), never a name |
| Fan-out join on `village_code` | there is deliberately **no FK between the two fact tables**; use `curated.v_district_year_summary` |
| Coverage questions | `curated.bridge_geography_source`, never a fact-to-fact join |

**Consequence for these files.** The casing rules, the year-translation table and the
`joins.join_predicate_template` in the resolver are obsolete *for SQL generation* - the
ingest already applied them. They remain valid for matching **raw user input**, which is
still typed in any casing and any year format. Do not delete them; scope them.

### 9.1 Five places where these YAMLs contradict the database

Verified on 2026-08-21 by re-profiling both workbooks. Every figure in
`SCHEMA_FOR_DEVELOPERS.md` sections 7 and 8 reproduced exactly. **The YAMLs were not
modified** - recorded here, same as section 6.

1. **`job_cards_issued_total` is a STOCK, not a flow.** stage2 says "Use SUM for totals".
   Summing it across the four years gives 2,128,714; the actual current stock is
   **520,999** (2025-26 alone). A 4x overstatement. Report one year at a time.

2. **`benefit_to_households` is unusable.** stage2 documents it as a measure in thousands
   with a `value_scale: 1000`. The database marks it UNVERIFIED and does not expose it in
   either view. NULL on 11,633 of 26,375 rows (44%), and the values do not reconcile with
   any household count. Do not use it.

3. **Expenditure has no unit in stage2. It is LAKH RUPEES.** Four-year total is
   362,866.57 lakh = **Rs 3,628.67 Cr**. Divide by 100 for Crore. Reading lakh as rupees
   understates by 100,000x. This is the single most dangerous omission in the file.

4. **`women_employment_provided` has an unconfirmed definition.** stage2 marks it
   `critical` with "Use SUM for totals". The data rules out both readings: it exceeds
   `employment_provided_persons` on **582** rows and `..._total_person_days` on **143**,
   and it is **zero on every row in 2025-26**. The quoted "0.9% women's share" figure
   (0.92% measured) is this column over person-days and is not supportable. Publish no
   ratio from it.

5. **Resolver casing / year / join rules are ingest-time, not query-time.** See the table
   above. Scope them to user-input matching.

### 9.2 Database-only facts these YAMLs never mention

- **2024-25 employment is likely a partial extract.** Person-days -46.2%, 100-day
  completions -91.9% (70,452 -> 5,723), while expenditure *rose* 40% to its four-year
  peak and cost per person-day doubled to Rs 727 against Rs 279-392 elsewhere. Carried by
  `dim_year.data_quality_note`; surface it with any answer covering that year.
- **The 60:40 statutory ratio uses `unskilled / total`** - semi-skilled counts on the
  *material* side. Statewide 63.69% passes; **South West Garo Hills 56.70%** and
  **West Khasi Hills 58.15%** fail on the four-year aggregate, as do 2022-23 (56.61%)
  and 2023-24 (59.64%). The looser `(unskilled + semi) / total` reading (67.76%) hides
  all four failures. Use the statutory formula.
- **Facts are at source-row grain**, not village-year: 1,864 employment village-years and
  157 expenditure village-years carry more than one row. Always `SUM ... GROUP BY`.
- **Not in the source at all:** pending dues, fund availability and releases, works and
  assets, and any grain finer than the financial year. These must answer "not available
  in the current source", never an improvised number.

### 9.3 Counts that look like contradictions but are not

The database reports 345 shared village names and a name-based village count of ~6,676;
this file reports 238 and the union of the two facts is 6,171. Different populations:
the database counts over the 7,364-row roster in `dim_geography` (which includes 164
Wards and roster-only villages), these YAMLs count only villages present in the two
workbooks. Both are right. State which population any count is over.

---

## 10. Change log

| Date | Change |
|---|---|
| 2026-08-21 | `stage2_mgnrega_combined_production.yaml` renamed to `mgnrega_schema_partitions.yaml`. Content unchanged (v5.0, 728 lines). The shorthand *stage2* is kept throughout this guide. |
| 2026-08-21 | `mgnrega_classification_rules.yaml` created and populated with **48 clarification rules** - node 5.3, the clarification gate. Format is `condition` + `question`, first match wins. Ordered so that requests the source cannot answer fire before requests for missing detail, and questions name the valid options wherever the data has a closed set. Despite the filename it holds *clarification* rules, not scope/scheme classification. |
| 2026-08-21 | Section 9 added: database alignment, five YAML-vs-database conflicts, and the database-only facts. Re-profiled from both workbooks; section 5 baseline reconfirmed unchanged. |
| 2026-08-21 | `mgnrega_default_rules.yaml` created and populated with **40 default rules** - node 5.5, generic defaults. Format is `condition` + `default_value` + `assumption_text`; each rule fills a value silently and hands the composer a line to surface. Three conditions also appear in the clarification file (`missing_time_period`, `geography_source_conflict`, `constituency_unassigned_rows`); the gate runs first, so those will ask and never default until one copy is removed. |
| 2026-08-21 | README brought in line with the new files: section 1 flow diagram now shows the clarification gate and defaults ahead of entity resolution, the README's own table row corrected, and section 7 given a checklist item for the data-derived numbers baked into the rule files. |
| 2026-08-21 | `foreign_key_augmentation.yaml` created and populated - node 5.9. Three blocks: **10 nodes** (role, grain, primary key, whether it may be aggregated), **13 edges** (12 real constraints, 1 augmented) each carrying the exact ON clause, and a **9-node join graph**. Three edges are `is_prohibited`: the fact-to-fact join, and both `raw.source_rows` lineage edges that `megh_readonly` cannot read. Deliberately carries no row counts or statistics - those live in `SCHEMA_FOR_DEVELOPERS.md` and section 5 of this guide, and would go stale here. Six consistency checks pass across nodes, edges and graph. |
| 2026-08-21 | `few_shot.yaml` created and populated with **41 question-to-SQL examples** - node 5.10. Format is `question` + `sql` + `tables`. Written against the `curated` views, and every example obeys the house rules: aggregate before reporting, `COUNT(DISTINCT village_code)` for village counts, job cards pinned to one year, lakh divided by 100 for crore, no fact-to-fact join, no `benefit_to_households`, no women's ratio, no `raw` schema. All 41 parse clean under SQLGlot as `SELECT` with no write nodes, and each example's declared `tables` matches the tables SQLGlot finds in its SQL. |
| 2026-08-21 | `response_template.yaml` created and populated - node 5.15. Three blocks: **14 formatting settings**, **50 templates** and **30 follow-up rules**. Indian number grouping, rupee sign, and geography shown in title case because the database stores it in capitals. `date_format` was replaced with `financial_year_format` - the source holds nothing finer than a financial year. Templates cover result shapes, situations with no usable result (empty, all-zero, not found, ambiguous, trimmed period, permission denied, execution failure), and the lines that must travel alongside the numbers - unit, assumptions, `dim_year` quality note, the 2024-25 caveat, the women's-definition caveat, the 60% floor, record-grain and null-exclusion notes. Follow-ups are grouped: move up or down the geography hierarchy, move across time, move across measures, work with the list returned, and recover from a thin or failed answer. |
