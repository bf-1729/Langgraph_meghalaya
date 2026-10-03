# PMAY-G Annotation Layer — How the YAMLs Are Built

This folder holds the hand-curated annotation layer for the **PMAY-G** scheme
(Pradhan Mantri Awaas Yojana – Gramin, Meghalaya) of the Megh One AI NLP-to-SQL bot.
Everything here is derived from two Excel workbooks in `datasets/PMAY/`. Nothing in this
folder is generated at runtime — these files are the reviewed, SME-owned contract that
the retrieval and SQL-generation stages read.

It is the sibling of `Annotations/MGNREGA/README.md`. Read that one for the house method;
this one records what is different about PMAY, and PMAY is different in four ways that
matter:

1. **One fact table, not two.** No join to design, no fan-out, no casing bridge.
2. **Plus a 7-row pre-aggregate** (`pmay_fy_summary`) that is *not* a join target — and it
   is denominated in **crore** while the detail table is in **rupees**.
3. **`sanctioned_amount` is effectively a constant** (₹130,000 per house). Money questions
   are house-count questions wearing a disguise.
4. **PMAY is not in `megh_db` yet.** MGNREGA has moved to Postgres; PMAY has not. See §9.

---

## 1. What is in this folder

| File | Layer | Answers the question |
|---|---|---|
| `pmay_schema_partitions.yaml` | **Semantic schema** (referred to throughout as *stage2*) | *What columns exist, what do they mean, how are they aggregated?* |
| `pmay_entity_resolver.yaml` | **Entity resolver** | *The user typed "EGH", "FY 2023-24", "Asimgre", "women" — which stored value is that?* |
| `README.md` | This guide | *How do I build or regenerate these, and what must never change?* |

They are **companions, not duplicates**. Keep the split clean:

```
user question
   |
   v
[ clarification gate ] <-- pmay_classification_rules.yaml      NOT BUILT YET
   |  bare year, "amount" with no qualifier, colliding place name -> ask, and pause
   v
[ defaults ]           <-- pmay_default_rules.yaml             NOT BUILT YET
   |  top N -> 5, bare year -> calendar, "beneficiaries" -> COUNT(*)
   |  every fill is recorded as an assumption and stated back
   v
[ entity resolution ]  <-- pmay_entity_resolver.yaml
   |  "EGH" -> "East Garo Hills" (lgd_code 273)
   |  "women" -> IN ('Woman-Married','Woman-Widow','Woman-Unmarried','Woman')
   |  "FY 2023-24" -> sanction_date >= '2023-04-01' AND < '2024-04-01'
   v
[ schema linking ]     <-- pmay_schema_partitions.yaml
   |  "funds released" -> amount_released
   |  "construction stage" -> house_status
   v
[ SQL generation ] -> SQLGlot validation -> read-only execute
   |
   v
[ response ]           <-- pmay_response_template.yaml         NOT BUILT YET
```

**Rule of thumb:** if the fact is about a *column* (meaning, type, unit, aggregation,
whether it is excluded) it belongs in stage2. If the fact is about a *value* (aliases,
casing, ambiguity, derived groups, what to do when it is not found) it belongs in the
resolver.

### 1.1 What MGNREGA has that PMAY does not

MGNREGA carries seven YAMLs; PMAY carries two. The gap is the near-term work:

| Missing file | MGNREGA equivalent | Why PMAY needs it |
|---|---|---|
| `pmay_classification_rules.yaml` | `mgnrega_classification_rules.yaml` (48 rules) | The bare-year ambiguity and the naked word *amount* both **must** ask, not default. There is nowhere to put that gate today. |
| `pmay_default_rules.yaml` | `mgnrega_default_rules.yaml` (40 rules) | Top-N → 5, bare year → calendar, colliding name → block. All three assumptions are currently described in prose inside the resolver instead of being machine-readable. |
| `few_shot.yaml` | `few_shot.yaml` (41 pairs) | PMAY's 33 examples live **inside** stage2. MGNREGA split them out. Decide which shape wins before writing more. |
| `foreign_key_augmentation.yaml` | `foreign_key_augmentation.yaml` (10 nodes / 13 edges) | PMAY has no joins — which is exactly why the *prohibited* edges need recording: detail↔summary, and PMAY↔MGNREGA. |
| `pmay_response_template.yaml` | `response_template.yaml` (50 templates) | Six caveats must travel with the numbers: unit (₹ vs Cr), the flat sanctioned amount, the 2023-24 partial year, record-count-not-people, the 462 null releases, and the 20.4% fuzzy mapping. |
| Scheme document | `MGNREGA_Scheme_Document.docx` | No PMAY-G scheme document in this folder. Policy questions ("what is the unit assistance?") have no grounding source. |
| Data definitions workbook | `MGNREGA_Data_Definitions.xlsx` (repo root) | No PMAY equivalent. The 18 column meanings in stage2 are SME-written, not sourced from a definitions sheet. |

**None of these were created.** Per standing instruction, no YAML is written until told
which and how.

---

## 2. The source data

Two workbooks under `datasets/PMAY/`. **Read-only. Never edit them.**

### 2.1 `PMAY_FullyMapped_with_dates.xlsx` — the detail table

- Sheet name: `Sheet1`
- **171,107 rows x 18 columns**
- Grain: **one row per beneficiary–sanction–house record**
- Warehouse table: `pmay_beneficiaries`

| Excel column | YAML `name` | dtype | Role | Nulls | Distinct |
|---|---|---|---|---|---|
| `id` | `id` | int64 | **primary key** — the only true one | 0 | 171,107 |
| `reg_no` | `reg_no` | object | beneficiary identifier, **not unique** | 0 | 170,739 |
| `beneficiary_name` | `beneficiary_name` | object | beneficiary dimension | 0 | 159,584 |
| `father_s_mother_s_name` | `father_mother_name` | object | beneficiary dimension | **114,118 (66.7%)** | 51,814 |
| `house_alloted_to` | `house_alloted_to` | object | allotment dimension (closed set) | 0 | 7 |
| `sanction_no` | `sanction_no` | object | sanction identifier, **very** not unique | 126 | 35,131 |
| `sanction_date` | `sanction_date` | datetime64 | **the only time dimension** | 126 | 1,122 |
| `sanctioned_amount` | `sanctioned_amount` | int64 | financial measure — **constant** | 0 | **2** |
| `installment_paid` | `installment_paid` | float64 | payment measure | 462 | 3 |
| `amount_released` | `amount_released` | float64 | financial measure | 462 | 4 |
| `house_status` | `house_status` | object | construction stage (closed set) | 12 | 6 |
| `Mapped_District_LGD_Code` | `mapped_district_lgd_code` | int64 | geo identifier | 0 | 12 |
| `Mapped_District_LGD_Name` | `mapped_district_lgd_name` | object | geo dimension | 0 | 12 |
| `Mapped_Block_LGD_Code` | `mapped_block_lgd_code` | int64 | geo identifier | 0 | 56 |
| `Mapped_Block_LGD_Name` | `mapped_block_lgd_name` | object | geo dimension | 0 | 56 |
| `Mapped_Village_LGD_Code` | `mapped_village_lgd_code` | int64 | geo identifier — **the village key** | 0 | 5,121 |
| `Mapped_Village_LGD_Name` | `mapped_village_lgd_name` | object | geo dimension — **not a key** | 0 | 4,855 |
| `Mapping_Status` | `mapping_status` | object | data-quality metadata | 0 | 44 |

> **Casing.** Unlike the MGNREGA workbooks, PMAY geography is **Title Case throughout**,
> in both name columns and in every dimension value. There is no ALL-CAPS/Title-Case
> bridge to build here. The 21 documented `casing_exceptions` in the resolver are real
> stored village names that legitimately break Title Case (`5th Mile`, `Bogularbhita-II`)
> — not dirt to clean.

### 2.2 `PMAY_FullyMapped_FY_summary.xlsx` — the pre-aggregate

- Sheet name: `Sheet1`
- **7 rows x 8 columns**
- Grain: **one row per sanction financial year**
- Warehouse table: `pmay_fy_summary`

| Column | dtype | Definition | Unit |
|---|---|---|---|
| `sanction_fy` | object | FY label, e.g. `2023-24` | — |
| `houses` | int64 | `COUNT(*)` for that FY | records |
| `sanctioned_cr` | float64 | `SUM(sanctioned_amount) / 10,000,000` | **crore ₹** |
| `released_cr` | float64 | `SUM(amount_released) / 10,000,000` | **crore ₹** |
| `completed` | int64 | `COUNT(*) WHERE house_status = 'Completed'` | records |
| `pending_cr` | float64 | `sanctioned_cr - released_cr` | **crore ₹** |
| `completion_pct` | float64 | `completed / houses * 100` | % |
| `utilisation_pct` | float64 | `released_cr / sanctioned_cr * 100` | % |

```
sanction_fy   houses  sanctioned_cr  released_cr  completed  pending_cr  completion_pct  utilisation_pct
    2017-18    15513        201.669    201.46230      15456     0.20670          99.633           99.898
    2018-19     3535         45.955     45.89325       3524     0.06175          99.689           99.866
    2019-20     9952        129.376    129.02890       9702     0.34710          97.488           99.732
    2020-21    24215        314.795    309.18290      20664     5.61210          85.336           98.217
    2021-22     3014         39.182     37.87420       2371     1.30780          78.666           96.662
    2022-23     8225        106.925    105.36505       6248     1.55995          75.964           98.541
    2023-24   106527       1384.851   1356.45325      71976    28.39775          67.566           97.949
     TOTALS   170981       2222.753   2185.25985     129941    37.49315
```

> **The unit trap.** `sanctioned_cr` / `released_cr` / `pending_cr` are in **crore**.
> `sanctioned_amount` / `amount_released` in the detail table are in **rupees**.
> 1 crore = 10,000,000. Never add or compare across the two without converting, and always
> state the unit in the answer. This is the PMAY analogue of the MGNREGA *lakh* omission.

> **The 126-row gap.** Summary total is **170,981**, detail total is **171,107**. The
> difference is exactly the 126 records with a NULL `sanction_date`, which belong to no
> financial year. An "all-time total" must say which population it used.

> **`pending_cr` is money, not houses.** A user asking for "pending houses" means
> `house_status <> 'Completed'`. A user asking for "pending amount" means `pending_cr` /
> the derived `sanctioned_amount - amount_released`. Two completely different answers.

---

## 3. Excel to YAML: the derivation pipeline

Same ten steps as MGNREGA. Do not hand-write numbers into the YAML — every count, list
and sample value must come out of a profiling run against the workbooks so it can be
re-derived and audited later.

```
Step 0  Load          read both workbooks, no dtype coercion, keep NaN as NaN
Step 1  Normalise     snake_case every header -> YAML `name`, keep `source_column`
Step 2  Type          infer decimal / integer / string / date from dtype + value shape
Step 3  Classify      assign `category` and `nlp_sql_priority` per column
Step 4  Profile       per column: distinct count, nulls, min/max, 6 sample values
Step 5  Semantics     SME writes `meaning`, `synonyms`, `business_rules`, `unit`
Step 6  Values        distinct dimension values -> resolver canonical + alias tables
Step 7  Keys          test uniqueness of every candidate key, record the failures
Step 8  Reconcile     re-derive the FY summary from the detail table, column by column
Step 9  Gaps          nulls, singletons, constants, ambiguity -> known_gap / notes
Step 10 Emit          write stage2 YAML + resolver YAML, stamp last_generated
```

PMAY replaces MGNREGA's Step 7 (join health) and Step 8 (raw-vs-LGD drift) with
**key-uniqueness testing** and **summary reconciliation** — there is no join to test and
no raw pre-LGD text column to diff.

### 3.1 The profiling recipe

```python
import pandas as pd, numpy as np

DET = "datasets/PMAY/PMAY_FullyMapped_with_dates.xlsx"
FY  = "datasets/PMAY/PMAY_FullyMapped_FY_summary.xlsx"

d = pd.read_excel(DET)   # 171107 x 18
f = pd.read_excel(FY)    #      7 x 8

# Step 4 - per-column profile -> feeds stage2 type / sample_values
for c in d.columns:
    print(c, d[c].dtype, d[c].nunique(), d[c].isna().sum(), d[c].dropna().unique()[:6])

# Step 6 - dimension catalogue -> feeds resolver canonical lists
d.Mapped_District_LGD_Name.value_counts()      # 12, Title Case, complete
d.Mapped_Block_LGD_Name.nunique()              # 56
d.house_status.value_counts(dropna=False)      # 6 + 12 NaN
d.house_alloted_to.value_counts()              # 7, incl. two n=1 artefacts

# Step 7 - key uniqueness -> feeds resolver identifiers.*
d.id.nunique()                                 # 171107 -> the ONLY real key
d.reg_no.nunique()                             # 170739 -> 368 duplicates
d.sanction_no.nunique()                        # 35131  -> max 322 rows per sanction
d.groupby('Mapped_Village_LGD_Code').Mapped_Village_LGD_Name.nunique().max()        # 1
d.groupby('Mapped_Village_LGD_Name').Mapped_Village_LGD_Code.nunique().gt(1).sum()  # 198
len(set(d.Mapped_Block_LGD_Name) & set(d.Mapped_Village_LGD_Name))                  # 26

# Step 8 - FY reconciliation -> the single most important check in this folder
dd = d.dropna(subset=['sanction_date']).copy()
fy = np.where(dd.sanction_date.dt.month >= 4,
              dd.sanction_date.dt.year, dd.sanction_date.dt.year - 1)
dd['fylab'] = fy.astype(str) + '-' + ((fy + 1) % 100).astype(str).str.zfill(2)
agg = dd.groupby('fylab').agg(houses=('id','size'),
        sanctioned_cr=('sanctioned_amount', lambda s: s.sum()/1e7),
        released_cr=('amount_released',     lambda s: s.sum()/1e7),
        completed=('house_status', lambda s: (s=='Completed').sum()))
# must match f on all four measures across all seven years, exactly
```

### 3.2 Choosing `category` and `nlp_sql_priority`

| `category` | Use for | `sql_operations` |
|---|---|---|
| `financial_measure` | `sanctioned_amount`, `amount_released` | SUM, AVG, MIN, MAX |
| `payment_measure` | `installment_paid` | SUM, AVG, MIN, MAX, WHERE, GROUP BY |
| `time_dimension` | `sanction_date` | WHERE, GROUP BY, MIN, MAX |
| `geographic_dimension` | the three LGD *name* columns | WHERE, GROUP BY, COUNT |
| `geographic_identifier` | the three LGD *code* columns | WHERE, GROUP BY, COUNT DISTINCT — **never SUM** |
| `house_status_dimension` | `house_status` | WHERE, GROUP BY, COUNT |
| `allotment_dimension` | `house_alloted_to` | WHERE, GROUP BY, COUNT |
| `beneficiary_dimension` | `beneficiary_name`, `father_mother_name` | WHERE, GROUP BY, COUNT |
| `beneficiary_identifier` / `sanction_identifier` | `reg_no`, `sanction_no` | WHERE, GROUP BY, COUNT DISTINCT — **never SUM** |
| `technical_identifier` | `id` | WHERE, GROUP BY, COUNT DISTINCT |
| `mapping_quality_metadata` | `mapping_status` | WHERE, GROUP BY, COUNT (low priority) |

`nlp_sql_priority` drives retrieval ranking, not correctness:
`critical` (users name it directly) > `high` > `medium` > `low`.
Current split across the 18 columns: 9 critical, 4 high, 1 medium, 4 low.

---

## 4. Anatomy of `pmay_schema_partitions.yaml` (stage2)

**782 lines, `schema_version: '1.0'`.** Renamed from
`stage2_pmay_production_semantic_schema.yaml` on 2026-08-24; content unchanged. The
shorthand *stage2* is kept throughout this guide.

| Section | Purpose |
|---|---|
| `architecture_alignment` | Which pipeline stage consumes this file, and the safety contract (SQLGlot read-only, enforced LIMIT, SELECT-only role) |
| `datasets.pmay_beneficiaries` | The 18 column documents — `meaning`, `type`, `category`, `nlp_sql_priority`, `sql_operations`, `synonyms`, 6 `sample_values`, `business_rules`. These are what get embedded for Qdrant retrieval. |
| `semantic_rules` | Geography hierarchy, the time dimension, the *beneficiary* definition, financial measures, null behaviour, identifier behaviour |
| `nlp_sql_rules` | Read-only, aggregation defaults, date behaviour, ranking (top-N → 5, singular → LIMIT 1), unknown-entity contract |
| `few_shot_examples` | **33 question→SQL pairs**, of which **4 have `sql: null`** — the refusals |

### 4.1 The four deliberate refusals

These teach the generator to say *no*, and they are as important as the 29 that produce SQL:

| Question | Why it is refused |
|---|---|
| "total of all village LGD codes" | Identifiers are not measures. Offer `COUNT(DISTINCT ...)` instead. |
| "update the sanctioned amount" | Schema is SELECT-only. Never translate a write. |
| "administrative expenditure under PMAY" | No such column. Do not invent one. |
| "total expenditure for PMAY" | Ambiguous between `sanctioned_amount` and `amount_released`. **Ask**, do not pick. |

### 4.2 Non-negotiable stage2 behaviours

1. **`COUNT(*)` is the default for "how many beneficiaries/houses"** — and the answer must
   say it is a *record* count. `COUNT(DISTINCT reg_no)` only on an explicit distinct/unique.
2. **Identifiers are never SUMed or AVGed.** `id`, `reg_no`, `sanction_no`, all three LGD codes.
3. **NULL is missing, never zero.** Never coerce.
4. **`house_status` is authoritative for construction stage.** Never infer completion from
   `amount_released` or `installment_paid`, even though they correlate.
5. **Canonical LGD *name* fields are the business-query geography.** `mapping_status` is a
   quality filter only, never a silent one.
6. **Never invent a join** to another scheme or table.

---

## 5. Anatomy of `pmay_entity_resolver.yaml`

**1,597 lines, `resolver_version: '1.0'`, generated 2026-08-21.** It answers exactly one
question: *the user typed some text — which stored database value did they mean?*

| Section | Purpose | Derived from |
|---|---|---|
| `scheme` | Qdrant partition key, plus 16 scheme aliases including **IAY** (predecessor — accept the alias, never label the data IAY) | Domain knowledge |
| `tables` / `dataset_shape` | Logical name → physical table; grain and row count | Step 4 |
| `how_to_use` | 5 steps, plus **8 hard rules** | Steps 7 + 9 |
| `normalisation` | `fold` (7 rules), `squash`, numeric-id and registration-id handling | Domain knowledge |
| `matching_pipeline` | **8 stages** — exact → alias → squash → acronym → contains → fuzzy → embedding → fail, each with a confidence | Design |
| `blocked_matches` | **11 pairs** fuzzy gets wrong (`Laskein`/`Thadlaskein`, `Woman-Married`/`Woman-Unmarried`) | Step 9 |
| `output_contract` | The three shapes: `resolved` / `ambiguous` / `not_found` | Design |
| `overloaded_terms` | 5 words that map to more than one column | Step 9 |
| `cross_dimension_collisions` | **26** block names that are also village names, and the default | Step 7 |
| `dimensions` | The catalogues — see below | Step 6 |
| `measure_vocabulary` | User phrasing → column; stage2 still owns aggregation | Step 6 |
| `fy_summary_table` | The pre-aggregate, its unit warning and its totals caveat | Step 8 |
| `region_groupings` | 4 hill-range groups over the 12 districts | Domain knowledge |
| `worked_examples` | **15** end-to-end traces, each with the expected number | Steps 4–8 |
| `maintenance` | Provenance, row counts at generation, 7 regeneration rules | Step 10 |

### 5.1 The dimension catalogues

| Dimension | Distinct | Key notes |
|---|---|---|
| `district` | 12 | Complete — all 12 Meghalaya districts present. Acronym table (EGH, WKH, SWGH…). No collisions. Each value carries `lgd_code`, `region`, `records`, `block_count`, `village_count` and ~10 aliases. |
| `block` | 56 | Verified clean: no code carries two names, no code spans two districts. Code and name are interchangeable. **26 names are also village names.** |
| `village` | 4,855 names / **5,121 codes** | Resolve on the **code**, never the name. 198 shared names, registered individually. 21 legitimate casing exceptions. |
| `house_status` | 6 (+12 NULL) | Closed, **ordered** set: Proposed Site → Existing site(Old House) → House Sanctioned → Plinth → Roof Cast → Completed. Derived groups: `in_progress` (41,041), `not_started` (2,170). |
| `house_alloted_to` | 7 | Closed set. Derived groups: **`women_any` (46,520)**, `women_or_joint` (156,885), `solely_male` (14,221). Two n=1 artefacts (`Self`, `Woman`). |
| `mapping_status` | 44 | Two families: `Fully Mapped (100%)` (136,141) and **43 distinct** `Fuzzy Mapped (xx.x%)` labels (34,966). |
| `financial_year` | 7 | **Derived**, not stored. See §6.1. |
| `calendar_year` | 7 (2017–2023) | `EXTRACT(YEAR FROM sanction_date)`. |
| `sanction_date` | 1,122 dates | Span **2017-08-09 → 2023-07-04**. 126 NULL. |
| `identifiers` | — | `id` unique; `reg_no` 368 dupes; `sanction_no` up to **322** rows per value. |

### 5.2 Non-negotiable resolver behaviours

1. **Villages resolve by `mapped_village_lgd_code`, never by name.** 198 names are shared.
2. **Ambiguous means ask, never guess.** Never pick the first row.
3. **Unknown means `not_found` with up to 5 suggestions, never invent** a district, block,
   village, status or category.
4. **"Women" means all four `Woman-*` categories.** Answering with `Woman-Married` alone
   (42,331) understates by **4,189**. Offer the 156,885 figure that includes joint
   allotments; never silently substitute it.
5. **`mapping_status = 'Fuzzy Mapped'` matches nothing.** Use `LIKE 'Fuzzy Mapped%'`.
6. **A bare year is a question, not an answer.** See §6.2.
7. **Append, never replace, on regeneration.** Old questions must keep resolving.

### 5.3 The five overloaded terms

| Word | Readings | Default |
|---|---|---|
| **sanctioned** | `house_status = 'House Sanctioned'` \| `SUM(sanctioned_amount)` \| a `sanction_date` filter | "sanctioned in 2023" is a **date filter**. The word *stage* flips it to the status. |
| **completed** | `house_status = 'Completed'` | Always. Never infer from money or installments. |
| **beneficiaries** | `COUNT(*)` \| `COUNT(DISTINCT reg_no)` | `COUNT(*)`, and say it is a record count. |
| **houses** | `COUNT(*)` | One row is one house record. |
| **amount** | `SUM(sanctioned_amount)` \| `SUM(amount_released)` | **Ask.** Do not silently pick one. |

---

## 6. Verified facts (profiled 2026-08-21, re-verified 2026-08-24)

Re-derived from the workbooks directly. **34 of 34 checks reproduced exactly — zero
mismatches.** Use these as the regression baseline when you regenerate.

### 6.1 The FY derivation — the headline finding

`pmay_beneficiaries` has no financial-year column. The resolver derives one:

```sql
CASE WHEN EXTRACT(MONTH FROM sanction_date) >= 4
     THEN EXTRACT(YEAR FROM sanction_date)
     ELSE EXTRACT(YEAR FROM sanction_date) - 1 END
```

Re-checked against `pmay_fy_summary` on 2026-08-24: **all four measures reconcile exactly
across all seven years.** `houses` and `completed` match to the integer; `sanctioned_cr`
and `released_cr` match to `0.0` absolute difference. The derived row total is 170,981 —
exactly the summary's `houses` total.

**FY questions are therefore answerable. Derive, do not invent, and do not refuse.**

### 6.2 Year ambiguity — why the gate matters

| Bare year | Calendar-year records | Financial-year records | Gap |
|---|---|---|---|
| 2022 | 4,056 | 8,225 (FY 2022-23) | **+103%** |
| 2023 | 110,890 | 106,527 (FY 2023-24) | −3.9% |

Silently choosing one is a wrong answer roughly half the time. Rules: a hyphenated or
slashed year, or the words *financial year* / *FY* / *fiscal*, force the financial
reading; a bare four-digit year defaults to **calendar** and the assumption must be stated;
*utilisation*, *completion rate* and *pending amount* force the financial reading because
the summary defines them per FY. When genuinely unclear — **ask**.

> **2023-24 is PARTIAL.** `sanction_date` stops at **2023-07-04**, so that FY covers only
> April–June 2023. It holds 106,527 of 170,981 dated records (62%) but is not a full year.
> Never compare it against a complete year without saying so, and never call its 67.57%
> completion rate a decline — those houses are simply newer.

### 6.3 Key uniqueness

| Candidate key | Distinct | Verdict |
|---|---|---|
| `id` | 171,107 / 171,107 | **The only true primary key** |
| `reg_no` | 170,739 | 368 duplicates, max 2 repeats. Not a key. |
| `sanction_no` | 35,131 | Max **322** rows per value, median 2. A sanction is an office order covering many houses. Counting sanctions = `COUNT(DISTINCT sanction_no)`. |
| `beneficiary_name` | 159,584 | Never a key. Sangma / Marak / Momin / Lyngdoh repeat heavily. |

### 6.4 Geography integrity

| Check | Result |
|---|---|
| Village codes carrying more than one name | **0** |
| Block codes carrying more than one name | **0** |
| Block codes appearing in more than one district | **0** |
| Village names shared by 2+ codes | **198** (159 resolve by district, 39 need the block) |
| Block names that are also village names | **26** |
| Districts | **12** — complete |

Codes are reliable; names are not. Use `COUNT(DISTINCT mapped_village_lgd_code)` for
village counts — counting distinct *names* loses **266** villages.

### 6.5 The measures

| Measure | Distinct values | Reading |
|---|---|---|
| `sanctioned_amount` | **2** — `130000` on every real row, `0` on the 126 dateless rows | **Effectively a constant.** `SUM` is `130000 × count`. Ranking districts by sanctioned amount is *identical* to ranking them by house count. `AVG` is always ~130,000 and carries no information. |
| `amount_released` | **4** — 52,000 / 110,500 / 120,000 / 130,000 | A stepped entitlement, not a continuous amount. Never exceeds sanctioned. No zero rows. |
| `installment_paid` | **3** — 1, 2, 3 | Ladder confirmed: 1→52,000 (1,986 rows); 2→110,500 or 120,000 (15,836); 3→130,000 (152,823). |

> **Never present "which district got the most money sanctioned" as a funding insight.**
> It is purely a count of houses. Answer, but say so.

### 6.6 Nulls and singletons

| Column | Nulls | Reading |
|---|---|---|
| `father_s_mother_s_name` | **114,118 (66.7%)** | Missing in two thirds of rows. Never filter or group on it without stating that. Format is `FATHER[MOTHER]` — split on `[`. |
| `installment_paid` + `amount_released` | **462 each — the same 462 rows** | No release recorded. Not zero. `AVG` silently skips them; `SUM` treats them as absent. |
| `sanction_date` + `sanction_no` | **126 each** | Belong to no financial year. Excluded from every FY figure. |
| `house_status` | **12** | Missing, not a stage. Exclude from status breakdowns and say so. |
| `house_alloted_to` = `Self` | n=1 | Almost certainly a data-entry artefact. In totals, never in a breakdown without `n=1`. |
| `house_alloted_to` = `Woman` | n=1 | Same — but it **is** a member of `women_any`. |
| everything else | 0 | — |

### 6.7 Distributions

```
house_status                       house_alloted_to                   district (records)
Completed                 129941   Joint(Husband and Wife)   110365   West Garo Hills            33147
Roof Cast                  25000   Woman-Married              42331   East Khasi Hills           23952
Plinth                     13984   Man                        14221   Ri Bhoi                    17565
House Sanctioned            2057   Woman-Widow                 3133   East Garo Hills            16899
Existing site(Old House)      73   Woman-Unmarried             1055   North Garo Hills           16028
Proposed Site                 40   Self                            1   West Jaintia Hills         12570
(NULL)                        12   Woman                           1   South West Garo Hills      11947
                                                                       South Garo Hills           11901
calendar year (records)            region groupings                    West Khasi Hills            8437
2017  13469    2021  16869         Garo Hills      89922               South West Khasi Hills      7411
2018   5457    2022   4056         Khasi Hills     45294               East Jaintia Hills          5756
2019   5394    2023 110890         Jaintia Hills   18326               Eastern West Khasi Hills    5494
2020  14846   (NULL)   126         Ri Bhoi         17565
```

`Ri Bhoi` belongs to no hill range — it is its own district and its own group. Both
readings give the same filter, so no clarification is needed. Do not fold it into
Khasi Hills.

---

## 7. Open issues — where the two files disagree

Found while profiling. Recorded here for the next revision; **the YAMLs were not modified**
beyond the rename.

1. **stage2 forbids financial years; the resolver derives them — and the resolver is right.**
   stage2's `time_runtime_rule` says *"do not invent a separate financial-year column
   because none exists in this dataset"*, and its `date_behavior` says *"do not invent FY
   semantics from the source."* The resolver's `financial_year.supersedes_stage2_note`
   overrides this, and §6.1 confirms the derivation reconciles exactly. **The resolver
   wins.** stage2 must be corrected so a reader of stage2 alone does not refuse a valid FY
   question. This is the single most important fix in the folder.

2. **stage2 does not know `pmay_fy_summary` exists.** It declares one dataset. The resolver
   declares two tables and gives the summary a full section. stage2's `join_behavior` —
   *"This source contains one PMAY table"* — is now false. Add the 7×8 table to stage2 as a
   second dataset marked *pre-aggregate, not a join target*, or say explicitly that stage2
   is scoped to the detail table only.

3. **stage2 never mentions that `sanctioned_amount` is a constant.** It carries the flat
   rule *"Use SUM for total sanctioned amount"* and offers "total sanctioned amount in East
   Khasi Hills" as an example, with no hint that the answer is `130000 × 23952`. A reader of
   stage2 alone will present a house count as a funding insight. This is the PMAY analogue
   of the MGNREGA `job_cards_issued_total` stock/flow error.

4. **stage2 carries no unit anywhere.** Neither `sanctioned_amount` nor `amount_released`
   declares rupees, and the crore-vs-rupee split against the summary table is absent. The
   MGNREGA README calls the missing lakh unit *"the single most dangerous omission in the
   file"*; this is the same omission.

5. **stage2 has no null counts and no `women_any` group.** It says *"NULL is missing data"*
   without ever saying how much — 66.7% on `father_mother_name`, 462 on the payment pair.
   And its `house_alloted_to` rule (*"do not infer gender or marital status beyond the
   stored category"*) is correct but, read alone, invites the exact `Woman-Married`-only
   answer that understates women by 4,189. The derived groups exist only in the resolver.

6. **stage2 lists 6 sample values for `house_alloted_to` but there are 7 categories.** The
   two n=1 artefacts are invisible in stage2. `Self` happens to appear in the sample list
   while `Woman` does not — so the file neither documents the singleton problem nor lists
   the full closed set.

7. **The resolver says `single_table: true` while `tables:` lists two.**
   `dataset_shape.single_table` and `tables.fy_summary` contradict each other on the face of
   the file. The *intent* is clear from `no_joins` and `fy_summary_table.relationship`, but
   the boolean is wrong.

8. **The village ambiguity registry keys are ALL CAPS; the stored values are Title Case.**
   The registry reads `ASIMGRE: { codes: 6, ... }` while the database holds `Asimgre`. These
   keys are *folded lookup keys*, not canonical values — but the file never says so. An
   implementation that emits a registry key straight into a `WHERE` clause returns zero
   rows. Add an explicit note, or store the canonical form alongside.

9. **Three dangling filename references.** After the rename, `pmay_entity_resolver.yaml`
   still points at `stage2_pmay_production_semantic_schema.yaml` at lines **7**
   (`generated_from`), **9** (`companion_schema`) and **1596**
   (`maintenance.companion_files`). Left untouched deliberately — fixing them is a content
   edit, which is not authorised yet. **This is the first thing to change when the resolver
   is next opened.**

10. **`maintenance.source_files` uses a `quadrent/datasets/PMAY/…` prefix** that does not
    exist in this repo; the workbooks are at `datasets/PMAY/…`. Inherited from the MGNREGA
    files, which carry the same stale prefix. Cosmetic, but it will send the next
    maintainer looking for a directory that is not there.

11. **The two n=1 categories have no policy.** `Self` and `Woman` are almost certainly
    data-entry artefacts. The resolver says to include them in totals but not in breakdowns
    without `n=1`. Nobody has confirmed with an SME whether `Woman` should be folded into
    `Woman-Unmarried` and `Self` into `Man`. Until someone does, `women_any` = 46,520 rests
    on an unreviewed judgement call.

12. **No coverage note on `mapping_status`.** 20.4% of records (34,966) are fuzzy-mapped at
    85.3–97.7% confidence, which means one record in five may not be exactly located. Both
    files say to use it only when asked about quality — neither says that *every geography
    answer* carries this 20.4% uncertainty. It belongs in the response templates.

---

## 8. Golden questions

The 15 worked examples in the resolver and the 33 in stage2 are the regression set. These
are the ones with hard expected answers — run them end to end before promoting any change:

| Question | Expected | What it tests |
|---|---|---|
| "how many PMAY houses in EGH" | **16,899** | Acronym stage |
| "houses sanctioned in 2023" | **110,890** | *sanctioned* → date filter; calendar default |
| "houses sanctioned in FY 2023-24" | **106,527** | FY derivation; the year gate |
| "how many houses are at sanctioned stage" | **2,057** | *stage* flips the reading |
| "how many houses went to women" | **46,520** | `women_any`, all four categories |
| "how many fuzzy mapped records" | **34,966** | `LIKE`, not `=` |
| "how many beneficiaries are there" | **171,107** | `COUNT(*)` default, stated as records |
| "distinct beneficiaries" | **170,739** | `COUNT(DISTINCT reg_no)` |
| "PMAY houses in Asimgre" | **ambiguous — 6 codes** | Never pick the first row |
| "PMAY houses in Mawlai" | **block, assumption stated** | Colliding-name default |
| "houses completed in Nagaland" | **not_found** | Never invent geography |
| "total expenditure under PMAY" | **ask which measure** | The naked *amount* |
| "which district got the most sanctioned amount" | **answer + flat-rate caveat** | The constant |
| "total funds released in crores" | **₹2,185.26 Cr** | Unit conversion |
| "average sanctioned amount per beneficiary" | **~130,000 + caveat** | The constant again |

---

## 9. Database alignment — PMAY is not in `megh_db` yet

MGNREGA has moved from flat Excel to Postgres: `megh_db` carries a curated star schema
(`dim_year`, `dim_geography`, `dim_geography_alias`, `bridge_geography_source`,
`fact_mgnrega_employment`, `fact_mgnrega_expenditure`) and its annotation YAMLs now have to
be *scoped* to user-input matching, because the ingest already normalised casing, year
formats and the village join. See `SCHEMA_FOR_DEVELOPERS.md` at the repo root.

**PMAY appears nowhere in that document.** No `fact_pmay_*` table, no PMAY partition, no
mention of the scheme. So:

- Both PMAY YAMLs describe the **Excel world**, and for PMAY that world is still the
  target. Nothing here is obsolete the way the MGNREGA casing and year rules are.
- `pmay_beneficiaries` and `pmay_fy_summary` are **logical names for tables that do not yet
  exist in the database.** Whoever ingests PMAY decides whether they stay standalone or
  fold into the shared `curated` dimensions.
- **The decision to watch:** PMAY geography is already LGD-mapped with district, block and
  village codes — the same LGD code space as `curated.dim_geography`. If PMAY facts are
  ingested against the shared geography dimension, the resolver's district, block and
  village catalogues stop being PMAY-specific and become a lookup into
  `dim_geography_alias`. That is the point at which §5.1 needs rewriting, and the point at
  which cross-scheme questions ("MGNREGA spend vs PMAY houses per district") become
  possible. Today both files correctly forbid them.
- **Do not forbid the cross-scheme join forever.** Both files say *"never invent a join to
  MGNREGA"* — right today, because no verified path exists. Once one does it must be added
  deliberately, with measured overlap, not discovered by the LLM.

---

## 10. Regeneration checklist

Run this whenever the workbooks change — new FY, corrections, new columns.

- [ ] Re-run the profiling script in §3.1 against both workbooks.
- [ ] Diff the new counts against §6. **Every** change must be explained, not accepted.
- [ ] **Re-verify the FY derivation against `pmay_fy_summary` on all four measures.** If
      they stop reconciling, the derivation rule is wrong — fix it before shipping.
- [ ] Re-check whether `sanctioned_amount` is still constant. If a second real value
      appears, the *ranking-equals-counting* guidance must be removed.
- [ ] Re-check the `amount_released` / `installment_paid` ladder. A new step invalidates the
      `value_ladder`.
- [ ] Re-run the village ambiguity registry. If a name gains a code it must appear there.
- [ ] Re-run the block↔village collision list. If it grew, update
      `cross_dimension_collisions.colliding_names`.
- [ ] Re-test key uniqueness. If `reg_no` becomes unique, or `id` stops being unique, both
      files change.
- [ ] New district / block / village / status / category values: **append** to the canonical
      alias tables. Never delete one.
- [ ] New acronym: check it collides with none of the existing 12 before adding.
- [ ] Watch the **2023-24 partial-year caveat**. Once later data lands, update or drop it.
- [ ] New column: add to stage2 with `meaning`, `category`, `nlp_sql_priority`,
      `sql_operations`, `synonyms`, six `sample_values`, and `business_rules`.
- [ ] Bump `resolver_version` / `schema_version`; update `last_generated` and
      `row_counts_at_generation`.
- [ ] Validate both files parse with `yaml.safe_load`.
- [ ] Re-run the §8 golden questions end to end before promoting.

---

## 11. House rules

- The workbooks in `datasets/PMAY/` are **read-only inputs**. Never write to them.
- The two YAMLs are **independent**. A change to one must not silently require a change to
  the other; if it does, say so explicitly. §7 items 1–6 are where that has already failed.
- Prefer **ask over guess** everywhere: ambiguous year, ambiguous place name, the naked
  word *amount*.
- **State the unit, always.** Rupees in the detail table, crore in the summary.
- **State the population, always.** 171,107 records or the 170,981 dated ones; record count
  or distinct registrations; 12 districts or a filtered subset.
- Never delete a canonical value or alias. Data ages, and questions about old years must
  keep working.
- **No YAML in this folder is created or edited without an explicit instruction naming the
  file and the change.**

---

## 12. Change log

| Date | Change |
|---|---|
| 2026-08-21 | `pmay_entity_resolver.yaml` created (v1.0, 1,597 lines) — 12 districts, 56 blocks, 198 ambiguous village names, 6 house statuses, 7 allotment categories, 44 mapping-status values, 7 financial years, an 8-stage matching pipeline, 11 blocked pairs and 15 worked examples. |
| 2026-08-21 | `stage2_pmay_production_semantic_schema.yaml` created (v1.0, 782 lines) — 18 column documents, semantic rules, NLP-to-SQL rules, and 33 few-shot examples including 4 deliberate refusals. |
| 2026-08-24 | `stage2_pmay_production_semantic_schema.yaml` renamed to **`pmay_schema_partitions.yaml`**, matching the MGNREGA convention. **Content unchanged** (v1.0, 782 lines). The shorthand *stage2* is kept throughout this guide. Three references to the old filename remain inside `pmay_entity_resolver.yaml` (lines 7, 9, 1596) and were deliberately left untouched — see §7 item 9. |
| 2026-08-24 | `README.md` created. Both workbooks re-profiled and both YAMLs read end to end. **34 of 34 verified figures reproduced exactly, zero mismatches**, and the financial-year derivation reconciles with `pmay_fy_summary` to `0.0` difference on all four measures across all seven years. §7 records **12 open issues** — chiefly that stage2 forbids the FY questions the resolver proves are answerable, does not know the summary table exists, and never states that `sanctioned_amount` is a flat ₹130,000 constant. **No YAML content was modified.** |
