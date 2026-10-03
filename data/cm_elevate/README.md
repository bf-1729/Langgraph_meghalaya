# CM Elevate Annotation Layer — How the YAMLs Are Built

**Status: COMPLETE.** All seven YAMLs are written. This file records what was
built, what it was built from, what was verified, and what is still open. Update it
with every change to this folder.

| | |
|---|---|
| Scheme | **CM Elevate** — one `dim_scheme` row (`CM_ELEVATE`) covering **15** individual schemes |
| Database | `megh_db`, schema `curated`, read role `megh_readonly` |
| Primary query surface | `curated.v_cm_elevate` |
| Schema authority | `SCHEMA_FOR_DEVELOPERS.md`, rewritten 2026-09-04 from the live extract |
| Data source | `datasets/Cmelevate/CM_Elevate_AllSchemes_20260818_122217.xlsx` (8,436 rows) |
| Requirement source | `datasets/Cmelevate/Megh One AI - CM Elevate NLP Use Cases.xlsx` (89 use cases) |
| Reference format | `Annotations/PMAY/pmay_schema_partitions.yaml` |

---

## 1. What is in this folder

| File | Status | Lines | Notes |
|---|---|---:|---|
| `cmelevate_schema_partitions.yaml` | **WRITTEN 2026-09-04** | 1,314 | stage2. Structure, columns, semantic rules, NL→SQL rules, 47 few-shot examples |
| `cmelevate_entity_resolver.yaml` | **WRITTEN 2026-09-04** | 2,200 | value resolution. 11 dimensions, 22 worked examples, 26 blocked pairs |
| `cmelevate_classification_rules.yaml` | **WRITTEN 2026-09-04** | 715 | clarification gate. 115 rules, 21 marked critical |
| `cmelevate_default_rules.yaml` | **WRITTEN 2026-09-04** | 871 | node 5.5 defaults. 129 rules, 24 critical, 15 per-sub-scheme |
| `cmelevate_few_shot.yaml` | **WRITTEN 2026-09-04** | 1,691 | node 5.10. 144 examples, 27 unanswerable |
| `cmelevate_foreign_key_augmentation.yaml` | **WRITTEN 2026-09-04** | 832 | join graph. 17 nodes, 4 declared FKs, 12 prohibited, 6 absent |
| `cmelevate_response_template.yaml` | **WRITTEN 2026-09-04** | 528 | node 5.15. 110 templates, 50 follow-ups, 15 refusals, 10 composer checks |

Both written files follow their PMAY counterparts section for section.
`cmelevate_schema_partitions.yaml` follows `pmay_schema_partitions.yaml`: header comment block → `architecture_alignment` → `database` → `datasets`
(primary view, underlying fact, dimensions, cross-scheme views) → `semantic_rules` →
`nlp_sql_rules` → `few_shot_examples` ending in a refusal block.

### 1.1 What PMAY has that CM Elevate does not

This is the whole story of this partition, and every rule in the YAML descends from it.

| | PMAY | CM Elevate |
|---|---|---|
| Money | `sanctioned_amount`, `amount_released`, `amount_pending` (rupees) | **none — no amount column at all** |
| Time | `sanction_date`, `sanction_month`, `year_key`, `financial_year` | **none — no date, no year_key, no FK to `dim_year`** |
| Status dimension | `dim_pmay_house_status` + `is_completed` / `is_in_progress` roll-ups | **none** — four uncontrolled varchars |
| Quality flags | `is_placeholder`, `completed_underfunded`, `mapping_category` | **none** |
| Sub-schemes | one scheme | **15**, via `dim_cm_elevate_scheme` |
| Extension | all columns typed | **`scheme_specific` JSONB**, keys differ per sub-scheme |
| Grain | one house | one application (a **request**, not an award) |

`COUNT(*)` is the entire aggregate vocabulary of CM Elevate.

---

## 2. The source data

### 2.1 `CM_Elevate_AllSchemes_20260818_122217.xlsx`

An API export dated 2026-08-18: 15 sheets, one per scheme, plus a `Summary`.
**8,436 rows, 0 invalid.** The database reports ~8,543 rows (`reltuples` estimate) — the
two do not agree and neither has been counted exactly, so every figure derived from
this file is marked `unverified_in_db: true` in the YAML.

Application counts per scheme, from `Summary`:

| Scheme | Rows | | Scheme | Rows |
|---|---:|---|---|---:|
| PRIME Small Enterprise Empowerment and Development (SEED) | 3,621 | | Meghalaya Any Business Venture Scheme | 136 |
| Meghalaya Piggery Development Scheme | 2,982 | | Meghalaya Goat Farming Scheme | 98 |
| Meghalaya Poultry Farming Scheme | 488 | | Meghalaya Warehouse Scheme | 38 |
| PRIME Tourism Vehicle Scheme | 403 | | Meghalaya Sericulture & Weaving Scheme | 14 |
| PRIME Agriculture Response Vehicle Scheme | 339 | | Chief Minister's Green Taxi Scheme | 10 |
| Meghalaya Dairy Development Scheme | 294 | | Meghalaya Sports & Wellness Centre Scheme | 6 |
| | | | Agro Tourism Villa Scheme | 3 |
| | | | Meghalaya Motorcaravan Scheme | 2 |
| | | | Meghalaya Cinema Theatre Scheme | 2 |

Two schemes are 78% of the programme; six have fewer than 15 applications. The YAML
requires the denominator to be stated for any percentage or ranking.

**The column architecture matches the database's spine-plus-JSONB design exactly:**
25 columns appear in all 15 sheets, 125 appear in exactly one. That confirms the
"~19 promoted, rest to `scheme_specific`" split from the source side.

### 2.2 `Megh One AI - CM Elevate NLP Use Cases.xlsx`

8 sheets: 89 use cases (Easy 52 / Medium 30 / Hard 7), 20 edge cases, 12 follow-ups,
12 ambiguous cases, a 34-term glossary, an index and a Q+A drilldown. Each use case
carries a query, expected answer, visual spec, query logic and cross-links.
Structurally the best-specified requirement input in this repo — **and it describes a
different dataset.** See §5.

---

## 3. Excel to YAML: the derivation pipeline

Every number in the YAML that is not from the live schema came from this profiling
run over the 15 sheets, concatenated.

```
# Step 1 - sheet inventory and row counts
#   -> feeds scheme_name.sample_values (the 15 names) and the skew rules

# Step 2 - column presence across sheets (common vs single-sheet)
#   -> CONFIRMED THE SPINE/JSONB SPLIT: 25 common, 125 unique-to-one

# Step 3 - value census on every spine column
#   -> THE STEP THAT MATTERS. It found the dead flags and the on-hold routing bug.

# Step 4 - applicant_category normalisation test
#   -> resolved how type_raw (varchar 100) maps to applicant_category (varchar 20)

# Step 5 - key uniqueness on request_id
#   -> 8,400 distinct of 8,436; 36 duplicates, all in Piggery

# Step 6 - date hunt across all 165 distinct column names
#   -> NO DATE COLUMN EXISTS. Confirms the schema, from the source side.

# Step 7 - request_id structure probe
#   -> a date-like segment exists but is ambiguous; naive parse FAILED (see §4.3)

# Step 8 - geography completeness and distribution
#   -> 12 districts, 58 blocks, 2,038 village codes, 16 rows with no village code
```

### 3.1 Choosing `category` and `nlp_sql_priority`

Same convention as PMAY. `critical` is reserved for columns a wrong answer would turn
on: `request_id`, `scheme_name`, the three geography names, `applicant_category`,
`data_verified` and `entity_type`. `low` marks technical identifiers, the dead flags
and `type_raw`.

---

## 4. Verified facts (profiled 2026-09-04, `unverified_in_db`)

### 4.1 The on-hold routing trap — the headline finding

| Column | Values |
|---|---|
| `onhold` | `False` on **all 8,436 rows** — zero TRUE, zero NULL |
| `is_withdraw` | `False` on **all 8,436 rows** |
| `data_verified` | Valid 6,700 · **On Hold 1,199** · NULL 389 · Wrong 81 · Invalid 67 |

*"How many applications are on hold"* answered from the `onhold` boolean returns
**zero, forever, and looks correct.** The only right source is
`data_verified = 'On Hold'` (1,199). The YAML encodes this as a `critical` rule on both
columns, in `semantic_rules.status_rule`, in `dead_flags`, and as a worked few-shot
example.

### 4.2 `applicant_category` vs `type_raw` — resolved

The export's `applicant_category` holds the **full raw string** — `poultry individual`,
`Prime Seed  unregistered` (double space), `Any Bussiness Venture individual` (source
typo), `Sericulture and Weaving Spinning Individual`. Those are 20–42 characters and
cannot be the database's `varchar(20)`.

Strip the scheme prefix and exactly three values remain, which fit:

| Value | Rows |
|---|---:|
| individual | 8,342 |
| registered | 83 |
| unregistered | 11 |

So `type_raw` = the full string (audit only), `applicant_category` = the last token
(the comparable one). This is what the schema comment means by "2-way vs 3-way splits".
**Never `GROUP BY type_raw` across schemes.**

### 4.3 No time dimension — confirmed from the source

No column in any of the 15 sheets is a date. `dob` is the only date-like field and it
is PII. **This is a source limitation, not a loader omission.**

`request_id` (`REQ/001/001/10102025/224638`) *looks* like it embeds one. It does not
usably: the 4th segment is 6, 7 or 8 digits with leading zeros dropped, so `1032026` is
validly 1-Mar-2026 **or** 10-Mar-2026. A naive parse resolved only **3,038 of 8,436**
rows and produced dates *after* the export date. The YAML forbids parsing it in
generated SQL and records why.

### 4.4 Case and wording collisions

| Column | Collision |
|---|---|
| `current_level` | `level1` (8,273) and `Level1` (45) are the same level stored twice → `LOWER()` before grouping |
| `current_file_status` | `Sendback` (1) and `sendback to vdv/citizen` (90) → match case-insensitively |
| `lgd_district` | source writes `Ri-Bhoi`, `dim_geography` stores `RI BHOI` |

### 4.5 Distributions

- **`application_mode`** — the cleanest field on the fact: Online 7,643 (90.6%),
  cmConnectCenter 793 (9.4%). No case variants.
- **`current_file_status`** — 98.4% `Forward`. Near-constant; not a distribution, and
  **not an approval status**.
- **Districts** — all 12 present, heavily skewed: Ri-Bhoi 2,592 → East Jaintia Hills 154.
- **`gender_id`** — Female **5,178 (61.4%)**, Male 3,250, Other 8. Fully populated.
  Despite the `_id` suffix it holds text. **It was not promoted**, so it is reachable
  only via `scheme_specific ->> 'gender_id'`. CM Elevate is female-majority — the most
  interesting finding in the data, and the use-case workbook never asks for it.

### 4.6 Keys and geography integrity

- `request_id`: 8,400 distinct of 8,436 — **36 duplicates across 64 rows, all Piggery.**
  The missing UNIQUE constraint in the DB is correct, not an oversight.
- Geography: 12 districts, 58 blocks, 2,038 distinct `village_lgd_code`, 16 rows with
  none. `district_lgd_code` and `block_lgd_code` are fully populated but
  **`block_lgd_code` is never trusted** — confirmed incompatible numbering.

---

## 5. The use-case conflict — the constraint every YAML in this folder encodes

`Megh One AI - CM Elevate NLP Use Cases.xlsx` was written against a **different
dataset**. This is not a rounding difference.

| | Use cases say | Data Excel | megh_db |
|---|---|---|---|
| Applications | **2,847** | 8,436 | ~8,543 |
| Schemes | **13** | **15** | **15** |
| Blocks | 57 | 58 | — |
| Villages | 1,153 | 2,038 | — |
| Piggery | 1,357 (48%) | 2,982 (35%) | — |
| Sericulture | Spinning 200 + Weaving 197 | one sheet, **14 rows** | — |
| Money | ₹146.01 Cr sanctioned, ₹84.06 Cr disbursed, ₹53.13 Cr subsidy, ₹30.93 Cr loan | **no money column** | **no money column** |
| Dates | Apr-2024 / Nov-2024 waves, 397 undated | **no date column** | **no date column** |
| Loan entity | Bank 1,745 / LIFCOM 690 | **absent** | **absent** |
| Subsidy tranches | 1 / 2 / 3 | **absent** | **absent** |
| Desanctioned / Refused | 102 / 16 | **absent** | **absent** |

The scheme lists do not reconcile either: the workbook references a **Common Facility
Center** scheme that does not exist here, and omits **PRIME Small Enterprise
Empowerment**, the largest scheme in the real data at 3,621 rows (43%).

**Reading:** the workbook describes a *post-sanction disbursement* dataset — a smaller,
approved-and-funded cohort. Our Excel and `megh_db` hold the *pre-approval application
intake*. Two stages of one programme, not two versions of one file.

### 5.1 The 40 deliberate refusals

`semantic_rules.unanswerable_categories` enumerates them by UC ID, grouped by cause:

| Cause | Count | UC IDs |
|---|---:|---|
| Money — no amount column | 38 | UC-007…UC-018, UC-020, UC-021, UC-024, UC-025, UC-030, UC-031, UC-038…UC-041, UC-043, UC-044, UC-058, UC-060, UC-063, UC-079…UC-089 |
| Time — no date column | 3 | UC-052, UC-053, UC-054 |
| Loan channel — no field | 7 | UC-033…UC-037, UC-046, UC-047 |
| Outcome flags — no field | 4 | UC-048…UC-051 |
| Beneficiary identity — stripped | 4 | UC-042, UC-044, UC-045, UC-066 |

(Groups overlap; 40 distinct use cases.) The YAML's `few_shot_examples` block ends with
**18 refusal examples** (`sql: null`) covering every cause. **The rule they all share:
never answer a money question with an application count.**

Roughly **49 of 89 survive** — counts, per-scheme and per-district breakdowns,
coverage, rankings by count, cross-tabs and data quality — plus questions the workbook
never asked: gender split, application mode, and the `data_verified` funnel.

---

## 6. Anatomy of `cmelevate_classification_rules.yaml`

The clarification gate. First rule that fires wins; its question is asked and the
pipeline pauses. **116 rules, 21 marked `critical`, no duplicate conditions.**
Follows `pmay_classification_rules.yaml` in structure and voice — a flat
`clarification_rules:` list of `condition` / `question` pairs, ordered so that
requests the source cannot answer are caught *before* missing detail is asked for.

Where PMAY used `v2_note` to explain what changed in a retarget, this file uses
`note:` — plain string for a short remark, or `{critical: true, text: >}` for the
ones a wrong answer turns on. There is no v1.0 predecessor to annotate against.

### 6.1 How it differs from PMAY's gate file

| | PMAY | CM Elevate |
|---|---:|---|
| Total rules | 96 | **116** |
| "Cannot answer at all" | 28 | **44** — the largest section, not a preamble |
| Time clarifications | 8 (which year did you mean?) | **0** — collapsed into 6 refusals; there is no year to mean |
| Which-measure rules | 11 (houses? released? rate?) | 10, all resolving *what is counted* — there is only one measure |
| Sections | 8 | **9** — scheme resolution is new, because CM Elevate is 15 schemes |

PMAY's time section asked which of several readings of a year the user meant.
CM Elevate has no date at all, so every one of those became a refusal. The
which-measure section survives but changed job: it no longer picks between
measures, it confirms *what is being counted*, since `COUNT(*)` is the whole
vocabulary.

### 6.2 The nine sections

1. **The source cannot answer this at all** (44) — money, time, outcome fields,
   identity, uncollected attributes, scope, cross-scheme
2. **The requirement workbook describes a different dataset** (5) — new, no PMAY
   counterpart
3. **Data quality** (10)
4. **Fields that exist but must not be read as asked** (11)
5. **Which scheme** (9) — new
6. **Which measure** (10)
7. **Geography** (13)
8. **Identifiers and values** (7)
9. **Shape of the answer** (10)

### 6.3 The rules that matter most

Five carry the weight of the partition:

- **`onhold_flag_used_for_on_hold_question`** — the single most likely wrong-answer
  path. The `onhold` boolean is FALSE on every row; a query against it returns zero
  and looks correct. Routes to `data_verified = 'On Hold'` (~1,199). Caught twice,
  here and in `dead_flag_explicitly_requested`.
- **`any_money_amount_requested`** — no amount column exists. Backed by
  `application_count_offered_as_money_proxy`, which stops a count from being emitted
  as an answer to a money question if the intent survives the first gate.
- **`any_time_period_requested`** — no date column exists. Backed by
  `request_id_date_parse_attempted`, which blocks the tempting-but-broken parse.
- **`unresolved_geography_in_village_answer`** — `entity_type = 'Unresolved'` is a
  placeholder, excluded from village counts, kept in every other total.
- **`scheme_specific_cross_scheme_key`** — a JSONB read without a scheme filter
  returns NULL for schemes lacking the key, indistinguishable from empty.

### 6.4 Section 2 is new and exists because of the conflict

Users briefed from the requirement workbook *will* ask for things it promises. Five
rules catch them by name rather than letting the query fail confusingly:

| Condition | Catches |
|---|---|
| `thirteen_schemes_asserted` | "13 schemes" — it is **15** |
| `common_facility_center_requested` | UC-083's scheme, which does not exist here |
| `sericulture_spinning_weaving_split_assumed` | UC-086/UC-089's two schemes — one sheet, 14 rows |
| `workbook_figure_quoted_by_user` | 2,847 · 1,357 Piggery · 1,153 villages · any ₹ total |
| `prime_seed_scheme_unknown_to_user` | the workbook omits the **largest** scheme (43%) |

`workbook_application_number_format` in section 8 does the same for identifiers —
`MPDSI018599` and `PTVSI000078` from UC-042/UC-043 belong to the other extract.
Real CM Elevate numbers begin `REQ/`.

### 6.5 One rule that is an offer, not a refusal

`gender_breakdown_requested` is deliberately *not* a refusal. Gender is populated on
every row, sits in `scheme_specific ->> 'gender_id'`, and shows ~61% female. It is
the most interesting finding in the data and the workbook never asks for it — so the
gate offers it. `map_requested` works the same way: UC-030's disbursement choropleth
is unanswerable, but a choropleth of application *counts* is, so it is offered
relabelled.

### 6.6 One thing the gate cannot fix

`region_grouping_requested` asks which of Garo / Khasi / Jaintia the user means, and
says the grouping is the resolver's own. **There is no region column in
`dim_geography`.** Any regional answer is a construction and must be declared as one
in the response — a rule the response template will need to enforce too.

---

## 7. Anatomy of `cmelevate_default_rules.yaml`

Node 5.5, running *after* the clarification gate. Each rule fills a missing detail
silently and hands the response composer an assumption line, so the user can correct
it without being interrupted. **129 rules, 24 marked `critical`, 10 carrying an
explicit `sql_effect`.**

Follows `pmay_default_rules.yaml`: a flat `default_rules:` list of `condition` /
`default_value` / `assumption_text`, with `sql_effect` where the default changes the
emitted SQL, and `note` where it needs justifying.

**The invariant that matters:** a condition lives in *either* the gate file or this
one, never both — the gate runs first, so a duplicated condition would always ask and
never default. Verified programmatically: **zero collisions** between the 115 gate
conditions and these 129.

> One gate rule was removed to keep that invariant. `ranking_size_unspecified` asked
> "top 5, top 10, or all?" — which would have blocked `missing_top_n` from ever
> defaulting. PMAY defaults top-N silently, so the gate rule went and the default
> stayed. The gate is now 115 rules, not 116.

### 7.1 Why this file is longer than PMAY's

| | PMAY | CM Elevate |
|---|---:|---:|
| Total rules | 96 | **129** |
| Per-sub-scheme rules | 0 | **15** |
| Time defaults | 14 | 6 — and all six *prevent* a date being used |
| Money/unit defaults | 8 | 4 — all four say "no money exists" |
| JSONB defaults | 0 | **8** |

Two forces pull in opposite directions. **Subtraction:** no money column and no date
column collapse PMAY's two largest default families. **Multiplication:** 15 sub-schemes
mean a default that fits Piggery is wrong for Sericulture.

### 7.2 Section 2 — a default per sub-scheme

This is what the file was expanded for. A default that is right for Piggery (2,982
applications, 12 districts, 74% female) is wrong for Sericulture (14 applications,
**one** district, 100% female) and meaningless for Motorcaravan (2 applications). So
each of the 15 gets a rule stating its own shape, plus 12 cross-scheme shape rules.

The per-scheme facts that drive them, profiled from the export:

| Scheme | Apps | Districts | Online | Valid | Female |
|---|---:|---:|---:|---:|---:|
| PRIME Small Enterprise Empowerment | 3,621 | 12 | 99.2% | 81.9% | 60.9% |
| Piggery Development | 2,982 | 12 | 90.6% | 78.2% | **74.3%** |
| Poultry Farming | 488 | 12 | 89.8% | 82.4% | 66.0% |
| PRIME Tourism Vehicle | 403 | 12 | 79.9% | 74.9% | **19.9%** |
| PRIME Agriculture Response Vehicle | 339 | 12 | 70.5% | 83.8% | **14.7%** |
| Dairy Development | 294 | **11** | **39.8%** | **52.7%** | 57.8% |
| Any Business Venture | 136 | 12 | 75.7% | 83.1% | 37.5% |
| Goat Farming | 98 | 12 | 60.2% | 82.7% | 50.0% |
| Warehouse | 38 | **9** | 97.4% | 78.9% | 39.5% |
| Sericulture and Weaving | 14 | **1** | 100% | 92.9% | **100%** |
| Chief Ministers Green Taxi | 10 | 7 | 100% | 100% | **0%** |
| Sports and Wellness Centre | 6 | 3 | 100% | 100% | 50.0% |
| Agro Tourism Villa | 3 | 2 | 66.7% | 100% | 66.7% |
| Motorcaravan | 2 | **1** | 100% | 100% | 50.0% |
| Cinema Theatre | 2 | 2 | 50.0% | 50.0% | 50.0% |

Four findings became `critical` defaults:

- **Gender is not a programme-level fact.** 61% female overall hides a range from
  100% (Sericulture) to 0% (Green Taxi). `gender_by_scheme_requested` forces the
  per-scheme figure.
- **Warehouse is the only scheme where registered bodies outnumber individuals**
  (20 vs 18). Every "applicants are individuals" default is wrong there.
- **Dairy is a double outlier** — 39.8% online against 70–100% elsewhere, and 52.7%
  Valid against 75–100%. Reported as observed, not interpreted.
- **Eight schemes are not statewide.** Sericulture and Motorcaravan reach one
  district each, so a "by district" answer for them is a single row.

### 7.3 Defaults whose job is to refuse

Three rules deliberately invert their PMAY namesake:

| Condition | PMAY default | CM Elevate default |
|---|---|---|
| `time_dimension_unspecified` | `sanction_date` | **`no_time_dimension`** |
| `money_unit_unspecified` | `crore` | **`not_applicable`** |
| `completion_term` | `is_completed` flag | **`no_completion_measure`** |

Plus two guards with no PMAY counterpart: `ingested_at_offered_as_date` (the load
timestamp will look like a usable date to a generator — it is ETL activity, not
applicant behaviour) and `request_id_date_segment_offered`.

And `pending_term` is the routing default that matters most — it resolves to
`data_verified = 'On Hold'` (~1,199), never the `onhold` boolean, which is FALSE on
every row and would return zero while looking correct.

### 7.4 Section 8 — JSONB, no PMAY counterpart

Eight rules for `scheme_specific`. The load-bearing one is
`scheme_specific_read_without_scheme_filter`: 25 fields are common to all 15 schemes,
125 appear in exactly one, and `->>` returns NULL for a missing key rather than
erroring — so an absent field is indistinguishable from an empty one.

`id_suffixed_field_assumed_numeric` is the other one worth knowing. `gender_id` holds
text, not a code. But `occupation_id`, `bank_id`, `designation_id`, `sector_id`,
`type_of_land_id`, `farming_experience_id` and `loan_defaulter_id` **have no lookup
table anywhere in `megh_db`** — so those codes cannot be resolved to labels at all.
The default reports the stored value and says it is uncoded.

### 7.5 Two small deliberate departures from PMAY

- **Percentages round to one decimal, not two.** With six schemes under 15
  applications, two decimals imply precision the denominators do not support.
  `percentage_on_small_denominator` goes further and shows the count instead.
- **Zero rows are included, not omitted.** `zero_count_scheme_in_breakdown` reads the
  scheme list from `dim_cm_elevate_scheme` so a scheme with no matching applications
  still appears with a zero. A missing row and a zero row read very differently when
  eight of 15 schemes are not statewide.

---

## 8. Anatomy of `cmelevate_entity_resolver.yaml`

The value-level resolver: the user typed some text — which stored database value did they
mean? **2,200 lines, 11 dimensions, 22 worked examples, 26 blocked match pairs.**

Follows `pmay_entity_resolver.yaml` section for section — `scheme` → `tables` →
`dataset_shape` → `how_to_use` → `normalisation` → `matching_pipeline` →
`blocked_matches` → `output_contract` → `overloaded_terms` →
`cross_dimension_collisions` → `dimensions` → `measure_vocabulary` → `region_groupings`
→ `worked_examples` → `maintenance`. Two sections are additions:
**`absent_dimensions`** and **`scheme_groupings`**.

### 8.1 The catalogues

| Dimension | Values | Notes |
|---|---:|---|
| `cm_scheme` | **15** | new — no PMAY counterpart; resolve this first |
| `district` | 12 | all 12 reached; LGD codes for resolution only |
| `block` | 58 | 44 rural C&RD, 6 Municipal Board, 4 Town Committee, 4 unsuffixed |
| `village` | registry only | 2,038 codes not enumerated; `dim_geography` is the authority |
| `applicant_category` | 3 | individual / registered / unregistered |
| `data_verified` | 4 + NULL | the only status field that varies usefully |
| `current_file_status` | 4 | 98.4% one value |
| `current_level` | 3 after folding | `level1` and `Level1` are the same level |
| `application_mode` | 2 | cleanest categorical on the fact |
| `gender` | 3 | lives in JSONB, not a column |
| `identifiers` | 8 | incl. two entries that exist only to refuse |

**Internal consistency check:** the scheme, district and block catalogues each sum
independently to **8,436** applications, matching the export exactly.

### 8.2 `request_id` carries a scheme code — a genuine find

Segment 1 of the application number is a **stable three-digit per-scheme code**, verified
1:1 across all 15 schemes: `REQ/001/…` is always Piggery, `REQ/233/…` always PRIME Small
Enterprise. An application number therefore identifies its scheme with no lookup, which
is useful for validating a user's number and for saying which scheme they're asking about
before any query runs. The full map is in `identifiers.request_id.scheme_prefix`.

It is a *measured regularity, not a documented contract* — flagged for re-checking after
any reload.

### 8.3 `absent_dimensions` — new, and the reason the file works

PMAY's resolver has a `financial_year` dimension and a money vocabulary. CM Elevate has
neither. A resolver that simply *lacked* them would fall through to `not_found`, implying
the user mistyped something. So the absences are declared, with reasons, and a new
`output_contract` shape — **`not_available`** — distinguishes "this data has no such
thing" from "I couldn't match your spelling".

Six entries: `time`, `money`, `loan_channel`, `outcome_flags`, `person_identity`, and the
`retained_but_forbidden` PII list. Each carries the columns that don't exist, what exists
instead, and the trigger aliases.

The two guards that matter most sit here: **never resolve a time span to `ingested_at`**
(it's the load timestamp — charting it presents ETL activity as applicant behaviour), and
**never parse a date out of `request_id`**.

### 8.4 `overloaded_terms` — 13, and most resolve to a refusal

PMAY's overloaded terms pick between real readings. Half of CM Elevate's resolve to
`not_available`:

- **`on_hold`** — the most dangerous term in the partition. Resolves to
  `data_verified = 'On Hold'` (~1,199). The `onhold` boolean is FALSE on every row, so
  resolving to it returns zero *and looks correct*.
- **`approved` / `sanctioned`** — no approval field exists. `current_file_status =
  'forward'` means the file moved on, not that it was approved, and covers 98.4% of rows.
- **`amount`** — PMAY has a three-way ambiguity here; CM Elevate has nothing to
  disambiguate between.
- **`vehicle`** and **`tourism`** — genuinely ambiguous across schemes of wildly
  different size (403 vs 3 applications). Both are blocked pairs; ask, never guess.

### 8.5 What the blocked pairs protect

26 pairs, and the scheme ones matter more than the geography ones. The four vehicle
schemes and the three PRIME schemes are mutually blocked, because fuzzy matching on
"PRIME vehicle" would otherwise pick between two schemes with very different applicant
profiles (19.9% female vs 14.7%) and sizes.

`Common Facility Center` gets its own `not_in_this_data` entry: it's asked for by
workbook UC-083, doesn't exist here, and its nearest string match — "Sports and Wellness
**Centre**" — is a completely different scheme.

### 8.6 Geography findings this file records

- **CM Elevate reaches urban bodies.** Unlike PMAY-G, which is rural-only, 10 of the 58
  blocks are Municipal Boards or Town Committees, and **92 village names are urban
  wards** carrying ~495 applications. Those wards are real localities — they map to
  `entity_type = 'Ward'` and must be counted. They are *not* the `Unresolved` placeholder.
- **36 village names map to more than one LGD code** (Chandigre → three). The ambiguity
  registry lists 25 of them.
- **269 of 2,001 village names are ALL-CAPS** in the source while the rest are Title
  Case — fold before matching.

### 8.7 The weakest part of the file, stated plainly

**The 58 block canonical forms were derived, not read from the database.** The source
writes `Umling C & RD Block`; the roster is expected to store `UMLING`. Each entry keeps
the full source form as an alias so either matches, but the canonical value is a
best-effort strip. This is the top item in
`maintenance.open_verification_tasks` — 13 tasks in all, each a query to run before any
CM Elevate answer ships.

---

## 9. Anatomy of `cmelevate_few_shot.yaml`

Validated question-to-SQL pairs, node 5.10. **1,691 lines, 144 examples** — 117
answerable, **27 `UNANSWERABLE`** carrying `sql: null`. 74 carry a `note`.

Follows `pmay_few_shot.yaml`: a flat `sql_generation_examples:` list of
`question` / `sql` / `tables`, with `note` where the SQL encodes a rule that isn't
obvious, and `status` + `reason` on the negative examples.

### 9.1 More than double PMAY's set, and why

| | PMAY | CM Elevate |
|---|---:|---:|
| Examples | 65 | **144** |
| Answerable | 55 | 117 |
| Negative examples | 10 (`RETIRED_v2.0`) | **27 (`UNANSWERABLE`)** |
| Per-scheme examples | 0 | **15** — one per sub-scheme |

Three forces made it bigger. **15 sub-schemes** each need their own example so the
generator learns the exact stored `scheme_name` string. **The refusals are load-bearing
here** — 40 of the 89 requirement-workbook use cases can't be answered, and a negative
example is the only thing standing between a money question and an application count
dressed up as an answer. And **the traps are unusual enough to need showing, not just
telling** — the on-hold routing, the `LOWER()` folding, the JSONB scoping.

PMAY's negative examples are historical (`RETIRED_v2.0` — their premise disappeared in a
migration). CM Elevate's are permanent: the columns never existed.

### 9.2 House rules encoded in every example

Checked programmatically across all 144 — **zero violations**:

- `FROM curated.v_cm_elevate` — the primary surface; both joins are INNER on NOT NULL keys
- **`COUNT(*)` only.** No `SUM`, no `AVG` over a view column. There is no measure — if an
  example contained one it would be wrong by construction
- **No date anything** — no `date_trunc`, no `EXTRACT`, no `ingested_at`, no `request_id` parse
- `entity_type <> 'Unresolved'` on village counts **only**
- `data_verified = 'On Hold'`, never the `onhold` boolean
- `LOWER(current_level)` / `LOWER(current_file_status)` before grouping
- `applicant_category`, never `type_raw` across schemes
- UPPERCASE district and block literals
- `COUNT(DISTINCT village_code)`, never by name
- `scheme_specific ->>` always scoped to one scheme — except `gender_id`

### 9.3 The examples that teach something non-obvious

- **"How many applications are on hold?"** — the single most important example. Routes to
  `data_verified`, with a note saying the `onhold` boolean returns zero *and looks correct*.
- **"Show every scheme including those with no applications"** — the one sanctioned
  `LEFT JOIN` in the partition, from `dim_cm_elevate_scheme` to the fact. A scheme with
  zero applications is invisible in the fact, and a missing row reads differently from a
  zero row.
- **"Are the onhold and withdrawn flags populated?"** — a pure data-quality query that
  proves the routing rule rather than asserting it.
- **"Which scheme-specific fields are common to every scheme?"** — builds the key manifest
  this partition otherwise lacks. Fields it returns are the only ones safe to read across
  schemes.
- **"Show the raw applicant labels for one scheme"** — the *only* sanctioned use of
  `type_raw`: audit, scoped to one scheme, shown beside the normalised category.
- **"Which scheme relies most on assisted application?"** — carries a
  `HAVING COUNT(*) >= 15` guard, dropping the six schemes too small for a percentage.

### 9.4 The 27 refusals

Grouped by cause, each naming the use cases it covers:

| Cause | Examples | Covers |
|---|---:|---|
| No money column | 9 | UC-008…UC-020, UC-030, UC-031, UC-058, UC-060, UC-079…UC-089 |
| No date column | 4 | UC-052, UC-053, UC-054, ED-018 |
| No loan channel | 1 | UC-033…UC-037, UC-046, UC-047 |
| No outcome flags | 3 | UC-048…UC-051 |
| Identity stripped / PII | 4 | UC-042, UC-045, UC-066 |
| Wrong dataset | 2 | UC-083 (Common Facility Center), MPDSI/PTVSI numbers |
| Prohibited or invalid SQL | 4 | fact-to-fact joins, `SUM` on an LGD code, writes |

The rule they share, stated in the file header and repeated in the notes: **never answer
a money question with an application count.** Offering the count is fine — as an
explicitly different question, relabelled.

---

## 10. Anatomy of `cmelevate_foreign_key_augmentation.yaml`

The join graph, node 5.9. **832 lines, 17 nodes, 16 edges** — 4 declared foreign keys,
12 prohibited — plus 6 `absent_edges` and 3 `sanctioned_patterns`.

Follows `pmay_foreign_key_augmentation.yaml`: `nodes` → `edges` → `join_graph`, with
`declared` / `is_prohibited` / `use_instead` on every edge and the exact `on_clause` the
generator must emit. Three sections are additions.

### 10.1 Four declared FKs, not five

| Edge | Nullable | Note |
|---|---|---|
| `cm_scheme_key` → `dim_cm_elevate_scheme` | **NOT NULL** | no PMAY counterpart |
| `geography_key` → `dim_geography` | **NOT NULL** | |
| `scheme_key` → `dim_scheme` | **NOT NULL** | constant in the partition |
| `raw_id` → `raw.source_rows` | nullable | outside the read boundary |

**Every non-lineage key is NOT NULL**, so `v_cm_elevate`'s two INNER joins are
**lossless** — the view row count equals the fact's. `v_pmay` and `v_focus_plus` can't
say that: their nullable `year_key` forces LEFT joins and a "count the dropped rows"
check before any time series. CM Elevate needs no such check, and the file says so with
a runnable assertion in the verification tasks.

### 10.2 `absent_edges` — new, and the point of the file

PMAY has `retired_edges` for joins that *used to* exist. CM Elevate's problem is the
opposite: a generator trained on the other four partitions will reach for edges that
**never** existed here. Six are declared, each naming which facts *do* have it:

- **`year_key` → `dim_year`** — the critical one. *Every other fact in `megh_db` has a
  `year_key`.* CM Elevate is the only one with no time dimension at all. This is not a
  nullable-key problem solvable with a LEFT join — there is nothing to join on, no
  fallback, and no derivable substitute.
- `status_key` → `dim_pmay_house_status` — PMAY-only
- `alias_key` → `dim_geography_alias` — resolved at ingest
- a monthly pre-aggregate — no date, so no `v_pmay_monthly_sanctions` analogue
- presence in `v_cross_scheme_money_district_year` — verified absent from the live definition
- **a money column** — not a join edge, but recorded because it governs which joins are
  worth making: a join undertaken to "get the money" is futile as well as prohibited

### 10.3 Twelve prohibited edges

Four are the cross-scheme fact pairs — **there are five facts in `megh_db` now, so ten
pairs, and CM Elevate contributes four.** The file lists all ten (its four plus the other
six) so it agrees with the other partitions, with a query to confirm they're encoded in
`semantic.join_graph`.

The other eight are CM Elevate specific. Two are worth calling out:

- **`request_id` self-join** — not unique, ~36 numbers repeat, so a self-join squares
  those groups.
- **`v_cm_elevate` → `v_cross_scheme_village_coverage`** — *prohibited at row level,
  permitted as an aggregate.* Joining directly repeats each village's `pmay_houses` once
  per CM Elevate application in that village. That distinction is why the next section
  exists.

### 10.4 `sanctioned_patterns` — the joins that *are* allowed

Rather than only saying what's forbidden, three permitted shapes are given in full so the
generator copies rather than invents:

- **`schemes_including_zero`** — the only LEFT JOIN this partition sanctions, and the
  only case where the dimension drives. Uses `COUNT(f.cm_elevate_fact_id)`, not
  `COUNT(*)`, since `COUNT(*)` returns 1 for a scheme with no applications.
- **`cross_scheme_coverage`** — aggregate-then-join, with the `Unresolved` exclusion
  *inside* the CTE and a reminder that the view COALESCEs to 0 so absence tests `= 0`.
- **`jsonb_containment`** — against the fact, not the view, so the GIN index applies.

### 10.5 Consistency checks that passed

- All 17 nodes appear in both `nodes` and `join_graph` — no orphans in either direction
- The three `maintenance` counts (4 declared / 12 prohibited / 6 absent) match the actual
  edge lists

---

## 11. Anatomy of `cmelevate_response_template.yaml`

The response composer, node 5.15 — wording, not SQL. **528 lines, 110 templates, 50
follow-up rules**, plus two sections with no PMAY counterpart: **15 refusal templates**
and **10 `composer_checks`**.

Follows `pmay_response_template.yaml`: `formatting` → `templates` → `follow_up_rules`,
with `{placeholders}` filled from the result set and numbers locked by the query.

### 11.1 The formatting block is mostly subtraction

| Key | PMAY | CM Elevate |
|---|---|---|
| `currency_symbol` | `₹` | **`null`** |
| `money_unit` | `crore` | **`null`** |
| `date_format` | `DD Month YYYY` | **`null`** |
| `financial_year_format` | `YYYY-YY` | **`null`** |
| `percent_decimal_places` | 2 | **1** |

Four of PMAY's formatting keys are nulled outright, each replaced by a `critical` rule
explaining why. `money_rule` and `time_rule` are the load-bearing ones: **if a rupee
symbol or a year ever renders in a CM Elevate answer, something upstream is wrong.**

Three formatting rules have no PMAY equivalent:

- **`scope_must_name_scheme`** — a figure that doesn't name its scheme, or say it covers
  all 15, is ambiguous in a way no PMAY figure is
- **`small_denominator_rule`** — below 15 applications show the count instead of the
  percentage; at or above, show both. Six schemes are under the threshold
- **`application_not_award_rule`** — say "applications", never "beneficiaries who
  received", "funded" or "approved"

### 11.2 Refusal templates — a first-class section

PMAY has one line, `unavailable_in_source`. CM Elevate needs 15, because 40 of the 89
use cases can't be answered. Each has three parts:

```
text:   what is not held, and why
offer:  the nearest real question
never:  the specific wrong thing the composer might otherwise do
```

The `never` field is what makes them work. On `money_not_held` it reads: *"Do not render
a rupee figure, and do not present the application count as the answer to the money
question."* That is the failure mode the whole partition is built to prevent — offering
the count is fine, **as an explicitly different question**.

Others cover time, approval, outcome flags, names, personal details, loan channel, the
cross-scheme comparisons, and three that correct the requirement workbook directly:
`scheme_not_in_data`, `workbook_figure_correction`, `scheme_count_correction`.

### 11.3 Notes that carry the partition's traps into the prose

The `templates` block includes the caveats that must travel with an answer:

- `dead_flag_note` — *"the withdrawn flag is unpopulated… so this zero reflects a gap in
  the data rather than applicants withdrawing"*
- `forward_status_note` — 'forward' means the file moved on, not that it was approved
- `gender_varies_by_scheme_note` — the programme figure hides a range from all-female to
  all-male
- `grouping_is_constructed_note` — region and sector groupings are applied by the
  resolver, not stored; the answer must say so and list the members
- `overlapping_groups_note` — two scheme groupings can't be added
- `unresolved_excluded_note`, `village_identity_note`, `urban_ward_note`,
  `duplicate_application_note`, `uncoded_field_note`

### 11.4 Follow-ups lead with scheme, not time

PMAY's follow-up set is built around moving through time — eleven of its rules offer a
different year or period. CM Elevate has none of that, so the eight **scheme** follow-ups
take that place: break the programme total down, compare against other schemes, expand a
grouping, see which districts a scheme misses.

Five follow-ups fire **after a refusal**, offering the nearest real question — each
carrying a note that it must be named as a different question, not slipped in as the
answer.

### 11.5 `composer_checks` — assertions on the draft

Ten checks the composer runs against its own output before sending. They exist because
this partition's failure modes are silent: a rupee figure that looks plausible, a zero
that reads as a finding, a percentage with no denominator. Among them:

- no currency symbol, and no "crore", "lakh", "rupees", "disbursed", "subsidy"
- no year, month or word implying recency
- every figure names its scheme, or says it covers all 15
- every percentage carries its count
- if a zero was reported for a flag, the answer says whether the flag is unpopulated
- if something was refused, the answer offered the nearest real question **and named it
  as a different question**

Verified programmatically: no renderable template emits a currency symbol, a money word,
or a time expression.

---

## 12. Non-negotiable behaviours (stage2)

1. **No `SUM`, no `AVG`.** If generated SQL contains either over a `v_cm_elevate`
   column, it is wrong. There is no measure.
2. **No date filter, no date `GROUP BY`, no `date_trunc`, no `request_id` parse**, and
   never `ingested_at` as a substitute — it describes the load.
3. **On-hold routes to `data_verified`**, never to the `onhold` boolean.
4. **`entity_type <> 'Unresolved'` on every village count and village list** — and on
   nothing else. Unresolved rows are kept in totals on purpose so they reconcile.
5. **`GROUP BY applicant_category`, never `type_raw`**, across schemes.
6. **`LOWER()` `current_level` and `current_file_status`** before grouping.
7. **Every `scheme_specific` read carries a `cm_scheme_key` / `scheme_name` filter.**
   `->>` returns NULL for a missing key rather than erroring, so a missing key is silent.
8. **`request_id` may return more than one row.** No `LIMIT 1` assumption.
9. **15 schemes, not 13.**

---

## 13. Open issues

1. **Row-count discrepancy.** 8,436 (export) vs ~8,543 (DB reltuples). Neither exact.
   Run `meta.v_reconciliation_cm_elevate` — it must balance **per endpoint**, not just
   in aggregate — and correct the YAML's `row_count_note`.
2. **PII: the loader's strip list is incomplete.** It removes `epic_id`,
   `citizen_name`, `mobile_number`, `pan_number` and `bank_account*`. The export also
   carries **`dob`, `email_id`, `address`, `member_id`, `family_id`, `pds_id`,
   `account_holder_name`, `ifsc_code`** — none on the strip list, so they land in
   `scheme_specific` and are queryable. `dob` + `address` + `village` is
   re-identifying. The YAML forbids surfacing them; **the real fix is at ingest.**
   Raised for the data team.
3. **`gender_id` is not promoted.** The one genuinely useful demographic is reachable
   only through JSONB. Promoting it to a typed column would be a cheap, high-value
   migration.
4. **The use-case conflict is unresolved** (§5). Either the disbursement dataset gets
   loaded as a second fact table, or the 40 use cases stay refused. **This is a
   decision for the data owner, not a YAML change.**
5. **`has_geo_conflict` count is contested** — the live column comment says four codes,
   an earlier pass counted 16. Neither re-derived.
6. **`applicant_types_seen` is an observation dated 2026-09-03** and will drift as the
   API returns new labels. Nothing re-checks it.
7. **No `scheme_specific` key manifest.** A developer must discover keys empirically
   per sub-scheme. A generated manifest would close this.
8. **`Annotations/MASTER/schemes_catalog.yaml` still lists only `mgnrega` and `pmay`.**
   Node 1 cannot route to CM Elevate (or Focus Plus) until it is extended. Not yet done.

---

## 14. Golden questions

Answers not yet measured against the database — these are the checks to run once
`megh_readonly` access is available.

| # | Question | Expected shape |
|---|---|---|
| 1 | How many CM Elevate applications? | one number; ~8,436–8,543 |
| 2 | How many schemes? | **15** |
| 3 | Applications by scheme | 15 rows, PRIME Small Enterprise top |
| 4 | How many villages covered? | `COUNT(DISTINCT village_code)` with Unresolved excluded |
| 5 | How many on hold? | 1,199 via `data_verified`, **not** 0 via `onhold` |
| 6 | Gender split | Female ~61%, from `scheme_specific ->> 'gender_id'` |
| 7 | Online vs cmConnectCenter | ~90.6% / 9.4% |
| 8 | Duplicate `request_id`s | 36 |
| 9 | Total disbursed | **REFUSED** — no money column |
| 10 | Trend by month | **REFUSED** — no date column |

---

## 15. House rules

- The live schema wins over any spreadsheet. Where they disagree, record both and say
  which is unverified — never silently pick one.
- Every figure derived from the export is `unverified_in_db: true` until measured.
- Refusals are first-class. A question the data cannot answer gets an explicit
  refusal example with a reason, not a plausible-looking approximation.
- Update this README in the same commit as any YAML change in this folder.

---

## 16. Change log

| Date | Change |
|---|---|
| 2026-09-04 | `cmelevate_response_template.yaml` written (528 lines, 110 templates, 50 follow-up rules), following `pmay_response_template.yaml`. **The folder is now complete — all seven YAMLs written.** Two sections have no PMAY counterpart: 15 refusal templates (each with `text` / `offer` / `never`, where `never` names the specific wrong thing the composer might otherwise do) and 10 `composer_checks` (assertions run against the draft answer before sending). Four formatting keys nulled outright — `currency_symbol`, `money_unit`, `date_format`, `financial_year_format` — each replaced by a critical rule. Follow-ups lead with scheme rather than time, since PMAY’s eleven time-based rules have no analogue here. Verified: no renderable template emits a currency symbol, money word or time expression. README section 11 added. |
| 2026-09-04 | `cmelevate_foreign_key_augmentation.yaml` written (832 lines, 17 nodes, 16 edges — 4 declared FKs and 12 prohibited — plus 6 `absent_edges` and 3 `sanctioned_patterns`), following `pmay_foreign_key_augmentation.yaml`. Two sections have no PMAY counterpart: `absent_edges` (joins a generator trained on the other four partitions will reach for and that never existed here — above all `year_key` → `dim_year`, which every other fact in megh_db has) and `sanctioned_patterns` (the three permitted join shapes, given in full). Recorded that all CM Elevate non-lineage keys are NOT NULL, so `v_cm_elevate` is lossless where `v_pmay` and `v_focus_plus` are not. Ten prohibited fact pairs listed, CM Elevate contributing four. Verified: all 17 nodes appear in both `nodes` and `join_graph`; maintenance counts match the edge lists. README section 10 added. One YAML remains empty. |
| 2026-09-04 | `cmelevate_few_shot.yaml` written (1,691 lines, 144 examples — 117 answerable, 27 `UNANSWERABLE`), following `pmay_few_shot.yaml`. More than double PMAY’s 65: 15 per-sub-scheme examples so the generator learns the exact stored `scheme_name` strings, and 27 negative examples because the refusals are load-bearing where 40 of 89 use cases cannot be answered. Verified programmatically across all 144: zero `SUM`/`AVG` over a view column, zero date handling, zero `onhold`-boolean routing, no duplicate questions, every answerable example carries `tables`. README section 9 added. Two YAMLs remain empty. |
| 2026-09-04 | `cmelevate_entity_resolver.yaml` written (2,200 lines, 11 dimensions, 22 worked examples, 26 blocked pairs), following `pmay_entity_resolver.yaml`. Two sections have no PMAY counterpart: `absent_dimensions` (with a new `not_available` output shape, so money and time spans refuse with a reason rather than degrading to not_found) and `scheme_groupings`. Catalogues: 15 schemes, 12 districts, 58 blocks, 3 applicant categories, 4 verification states, 3 genders — the scheme, district and block catalogues each sum independently to 8,436. New findings recorded: `request_id` segment 1 is a stable per-scheme code (1:1 across all 15), CM Elevate reaches urban bodies and 92 ward names unlike rural-only PMAY-G, 36 village names map to multiple LGD codes, and 269 village names are ALL-CAPS. Block canonical forms are derived not read — top item in 13 open verification tasks. README section 8 added. Three YAMLs remain empty. |
| 2026-09-04 | `cmelevate_default_rules.yaml` written (871 lines, 129 rules, 24 critical, 11 sections), following `pmay_default_rules.yaml`. Section 2 is new: one default per sub-scheme (15) plus 12 cross-scheme shape rules, driven by a per-scheme profile of the export. Three defaults deliberately invert their PMAY namesake (`time_dimension_unspecified`, `money_unit_unspecified`, `completion_term`); a JSONB section (8 rules) has no PMAY counterpart. Verified zero condition collisions with the gate file; `ranking_size_unspecified` removed from the gate so `missing_top_n` can default silently. README section 7 added. Four YAMLs remain empty. |
| 2026-09-04 | `cmelevate_classification_rules.yaml` written (720 lines, 116 rules, 21 critical, 9 sections), following `pmay_classification_rules.yaml`. Section 2 is new with no PMAY counterpart: five rules that catch requirement-workbook assumptions by name (13-schemes, Common Facility Center, the Sericulture split, quoted figures, and the omitted PRIME Small Enterprise scheme). Time clarifications collapsed to refusals; which-measure rules retargeted from picking a measure to confirming what is counted. README section 6 added. Five YAMLs remain empty. |
| 2026-09-04 | **Folder started.** `SCHEMA_FOR_DEVELOPERS.md` rewritten against the 2026-09-04 live extract (38 objects / 449 columns), adding CM Elevate. Both `datasets/Cmelevate/` workbooks profiled. `cmelevate_schema_partitions.yaml` written (1,314 lines, 22 view columns, 47 few-shot examples of which 18 are refusals), following `pmay_schema_partitions.yaml` as the format reference. Findings recorded: the on-hold routing trap, the `applicant_category` / `type_raw` normalisation, no-time-dimension confirmed from source, the failed `request_id` date parse, case collisions, `gender_id` in JSONB, 36 duplicate `request_id`s, and the 40-use-case conflict with the requirement workbook. Six YAMLs remain empty. |
