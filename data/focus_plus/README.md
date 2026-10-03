# Focus Plus Annotation Layer — How the YAMLs Are Built

This folder holds the hand-curated annotation layer for the **Focus Plus** scheme (Meghalaya)
of the Megh One AI NLP-to-SQL bot. Everything here is derived from two files in
`datasets/Focus+/` and from the live database structure. Nothing in this folder is generated at
runtime — these files are the reviewed, SME-owned contract that the retrieval and SQL-generation
stages read.

It is the sibling of `Annotations/PMAY/README.md` and `Annotations/MGNREGA/README.md`. Read the
PMAY one for the house method; this one records what is different about Focus Plus, and Focus
Plus is different in five ways that matter:

1. **It is the only person-level fact in `megh_db`.** `member_id` and `pincode` describe
   identifiable individuals. Rule 7 of `SCHEMA_FOR_DEVELOPERS.md` governs every query
   written from this folder. `bank_name_raw` is also on the row but identifies a bank,
   not a person, so it is not covered by that rule (see §6.8).
2. **One row is one payment** — not one person, not one household. `COUNT(*)` answers a question
   almost nobody asks.
3. **The data is two datasets stacked in one fact**, split by `batch_label`. Nothing in the DDL
   reveals this, and it governs almost every answer. See §6.1.
4. **There is no date column at all**, no status dimension and no data-quality flags. PMAY has
   all three.
5. **The money unit is unverified.** This is the highest-priority open item in the folder.

Unlike PMAY, Focus Plus has **no flat-table ancestor**. It was loaded straight into the curated
star schema on 2026-09-02, so every file here is v1.0 written against the database as it stands,
with no migration to record.

---

## 1. What is in this folder

| File | Layer | Answers the question | Status |
|---|---|---|---|
| `focusplus_schema_partitions.yaml` | **Semantic schema** (*stage2*) | *What columns exist, what do they mean, how are they aggregated?* | **Built** — 1,366 lines, v1.0 |
| `focusplus_classification_rules.yaml` | **Clarification gate** | *What must the bot ask about instead of guessing?* | **Built** — 310 lines, 89 rules |
| `focusplus_default_rules.yaml` | **Defaults** | *What gets filled silently, and what assumption is stated back?* | **Built** — 315 lines, 67 rules |
| `focusplus_entity_resolver.yaml` | **Entity resolver** | *The user typed "EGH", "Tranch 2", "Farmer" — which stored value is that?* | **Built** — 1,610 lines, v1.0 |
| `focusplus_foreign_key_augmentation.yaml` | **Join graph** | *Which joins are permitted, and which are prohibited?* | **Built** — 511 lines, 13 nodes / 11 edges |
| `focusplus_few_shot.yaml` | **SQL examples** | *What does correct SQL look like for this scheme?* | **Built** — 841 lines, 70 examples |
| `focusplus_response_template.yaml` | **Response composer** | *How is the answer worded, and which caveats travel with it?* | **Built** — 344 lines, 87 templates |
| `README.md` | This guide | *How were these built, what was found, and what must never change?* | This file |

### 1.1 Where they sit in the pipeline

```
user question
   |
   v
[ NODE 1 - scheme classification ]  <-- MASTER/schemes_catalog.yaml
   |                                    NOTE: Focus Plus is NOT in the catalogue yet. See §8.
   v
[ clarification gate ]   <-- focusplus_classification_rules.yaml     BUILT
   |  producer groups, EPIC ids, account/IFSC, dates -> refuse and redirect
   |  batch scope, "beneficiaries", the payment rate -> ask, and pause
   v
[ defaults ]             <-- focusplus_default_rules.yaml            BUILT
   |  top N -> 5, no period -> both years, bare count -> payments
   |  every fill is recorded as an assumption and stated back
   v
[ entity resolution ]    <-- focusplus_entity_resolver.yaml          BUILT
   |  "EGH" -> East Garo Hills; "tranche 2" -> 'Tranch 2 - August'
   |  "12.5k" -> the registration cohort; member_id -> refused
   v
[ schema linking ]       <-- focusplus_schema_partitions.yaml        BUILT
   v
[ join discovery ]       <-- focusplus_foreign_key_augmentation.yaml BUILT
   |  4 inferred FKs, 7 prohibited edges, no joins for ordinary questions
   v
[ SQL generation ]       <-- focusplus_few_shot.yaml                 BUILT
   |  70 validated pairs, all FROM curated.v_focus_plus
   |  -> SQLGlot validation -> read-only execute
   v
[ response ]             <-- focusplus_response_template.yaml        BUILT
      six mandatory caveats travel with every number
```

**Rule of thumb:** if the fact is about a *column* (meaning, type, unit, aggregation, whether it
is excluded) it belongs in stage2. If the fact is about a *value* (aliases, casing, ambiguity,
derived groups, what to do when it is not found) it belongs in the resolver. If it is about
whether to *ask or assume*, it belongs in one of the two gate files — and in exactly one of them.

---

## 2. The source data

Two files in `datasets/Focus+/`. One is the data; the other is what the SME wants asked of it.

### 2.1 `Focus Plus Master.csv` — the data

**385,671 rows, 21 columns, 58 MB.** This is the source behind
`curated.fact_focus_plus_disbursement`.

| # | Column | Fate at ingest |
|--:|---|---|
| 1 | `id` | → `source_row_id` |
| 2 | `source_sl_no` | kept |
| 3 | `batch` | → `batch_label` |
| 4 | `tranche_label` | kept |
| 5 | `financial_year` | → `year_key` via `dim_year` |
| 6 | `amount_disbursed` | kept |
| 7 | `beneficiary_name` | **DROPPED** |
| 8 | `gender` | kept |
| 9 | `occupation` | kept |
| 10 | `bank_name` | → `bank_name_raw`, kept on the fact and the view |
| 11 | `status` | **DROPPED** (constant `'106'`) |
| 12 | `focus_status` | kept |
| 13 | `verification_status` | kept |
| 14 | `member_id` | kept — **PII** |
| 15 | `mobile_number` | **DROPPED** |
| 16 | `epic_id` | **DROPPED** — see §6.2 |
| 17 | `district_name` | → `geography_key` |
| 18 | `block_name` | → `geography_key` |
| 19 | `village_name` | → `geography_key` |
| 20 | `mapped_village_lgd_code` | → `village_code` |
| 21 | `pincode` | kept — **PII** |

### 2.2 `Copy of Megh One AI - FOCUS+ NLP Use Cases.xlsx` — the requirement

**8 sheets, 352 use-case rows.** This is not data; it is the specification of what the bot must
handle, written by the SME.

| Sheet | Rows | Columns that matter | Feeds |
|---|--:|---|---|
| `Index` | 352 | UC ID, Sheet, Category, Query — **with SME gap annotations after an en dash** | everything |
| `Glossary` | 45 terms | Term, Group, Definition, FOCUS+ Example | resolver, response template |
| `FOCUS+ Use Cases` | 112 | Query, Expected Answer, **Query Logic**, Follow-up, Edge Case | few-shot, stage2 |
| `Descriptive Commands` | 50 | Command, Expected Answer, Visual, Query Logic, Counter Question | few-shot, response template |
| `Edge Cases` | 50 | Scenario, **Expected Behaviour**, Notes | resolver, classification |
| `Follow-up` | 40 | **Trigger Condition**, Follow-up Question, Notes | classification |
| `Ambiguous Cases` | 35 | Ambiguous Input, Clarifying Question, **Notes carry the default** | classification / defaults split |
| `Q+A Drilldown` | 65 | Suggested Answer, **Counter Question** | response template |

Two things make this workbook unusually useful and unusually dangerous:

- **The Ambiguous Cases sheet pre-decides the classification/default split.** Its `Notes` column
  says "Default = state-wide", "Default = paid", "Default = all-time". Those went straight into
  `focusplus_default_rules.yaml`.
- **Its expected answers are partly wrong.** Six of them rest on a flat ₹5,000 payment that the
  data contradicts, two district figures are stale, and one quotes a producer-group count for
  data that holds no producer groups. See §7.

---

## 3. CSV to YAML: the derivation pipeline

No Excel profiling library was needed for the data — the source is a plain CSV, so everything
below is stdlib Python. The workbook was read with `openpyxl`.

### 3.1 The profiling recipe

```python
# Step 1 - shape and header
#   -> feeds stage2 column list, and the ingest-fate table in §2.1
csv.DictReader; count rows; compare header against the 22 fact columns

# Step 2 - per-column null/blank census
#   -> THE MOST IMPORTANT STEP IN THIS FOLDER. It is what exposed the batch duality.
for each column: count '' and None

# Step 3 - low-cardinality value census
#   -> feeds stage2 sample_values and the resolver catalogues
Counter() over batch, tranche_label, financial_year, amount_disbursed,
             gender, occupation, focus_status, verification_status, district_name

# Step 4 - split every count by batch_label
#   -> THE STEP THAT MATTERS. Nothing about this scheme reads correctly unsplit.
defaultdict(Counter) keyed on batch

# Step 5 - key uniqueness and cross-batch overlap
#   -> feeds beneficiary_term, paid_unpaid_term and the 111-EPIC finding
set() per batch for epic_id, source_sl_no, member_id, mapped_village_lgd_code
then intersect the two batches

# Step 6 - money reconciliation
#   -> feeds the unit discussion and the two-rate finding
sum(amount_disbursed); Counter over (batch, tranche, FY, amount)

# Step 7 - workbook reconciliation
#   -> THE CHECK THAT FOUND THE STALE DISTRICT FIGURES
legacy district count / 4 == workbook's per-district beneficiary figure?
# ten of twelve match exactly; two do not, and their sum is preserved
```

Step 7 is the Focus Plus equivalent of PMAY's FY reconciliation: the one check that compares the
SME's published numbers against the data and refuses to assume they agree.

### 3.2 Choosing `category` and `nlp_sql_priority`

Same method as PMAY. `category` groups a column by what it *is*
(`financial_measure`, `geographic_dimension`, `person_attribute`, `person_identifier`,
`batch_dimension`, `technical_identifier`). `nlp_sql_priority` records how often a real user
question touches it, read off the 352 workbook rows rather than guessed.

One Focus-Plus-only rule: a column with **`pii: true` gets `sample_values: WITHHELD_PII`**
regardless of its priority. `member_id` is `high` priority and still carries no samples.

---

## 4. Anatomy of `focusplus_schema_partitions.yaml` (stage2)

**1,366 lines, `schema_version: '1.0'`.** Structured exactly as `pmay_schema_partitions.yaml`.

| Section | Purpose |
|---|---|
| `architecture_alignment` | Pipeline stages, safety contract, **plus a `pii_boundary` clause PMAY does not have** |
| `database` | `megh_db` / `curated` / `megh_readonly` / `v_focus_plus`, plus the grant warning (§8) |
| `datasets.v_focus_plus` | The 23 column documents — in the view's exact declared order |
| `datasets.fact_focus_plus_disbursement` | Lineage only: inferred FKs, `keys_absent`, `columns_not_in_v_focus_plus`, `dropped_at_ingest` |
| `datasets.dim_scheme` / `dim_year` / `dim_geography` | The three shared dimensions |
| `datasets.v_cross_scheme_*` | Both flagged incomplete for this scheme |
| `semantic_rules` | 28 keys, opening with `batch_duality`, `beneficiary_term` and `paid_unpaid_term` |
| `nlp_sql_rules` | 21 keys, including `never_query` and `mandatory_predicate: NONE` |
| `few_shot_examples` | **34 examples, of which 13 have `sql: null`** — the refusals |

### 4.1 The twelve deliberate refusals

Well over PMAY's seven, because more of the use-case workbook rests on columns that do not
exist. Bank name is no longer one of them — `bank_name_raw` was added to `v_focus_plus` on
2026-09-09 (§6.8) — but these still teach the generator to say *no*:

| Refused question | Why |
|---|---|
| EPIC lookup | Column dropped at ingest, **and** rule 7 forbids person lookup |
| Member name lookup / payment history | Name dropped; no date to build a history from; person lookup |
| How many producer groups | No such column anywhere in source or database |
| Disbursement trend by month | No date column at all |
| Beneficiaries with no mobile number | Column dropped at ingest |
| Shared account numbers | Never held |
| Coverage rate per district | No eligible-population denominator |
| EPIC overlap with Ginger Mission | Other dataset absent **and** join key dropped |
| Total of all village LGD codes | Identifiers are not measures |
| List member IDs in Selsella | PII — offer the aggregate count |
| Update a disbursement amount | Read-only role |
| MGNREGA person-days for these villages | All six fact pairs are prohibited (rule 6) |

### 4.2 Non-negotiable stage2 behaviours

1. **`COUNT(*)` is a PAYMENT count** and the answer must say so. A legacy member is four rows.
2. **`COUNT(DISTINCT member_id)` is not the scheme total.** It covers 3.2% of rows.
3. **There is NO mandatory predicate.** Do not carry PMAY's `WHERE NOT is_placeholder` across —
   the column does not exist and the query will error.
4. **Read `dim_scheme.money_unit` before formatting any amount.** Never assume rupees.
5. **`batch_label` is checked before answering anything cohort-sensitive.**
6. **`tranche_label` is never ordered as time.**
7. **Identifiers are never SUMed or AVGed**, and `member_id` / `pincode` are never displayed.
8. **Query `v_focus_plus`, never the fact.**

---

## 5. Anatomy of the two gate files

They are a **pair with a hard contract: a condition lives in exactly one of them.** The gate runs
first, so a condition present in both would always ask and never default. This is verified
mechanically — see §9.

### 5.1 `focusplus_classification_rules.yaml` — 89 rules

Flat list of `condition` + `question`, ordered so that the unanswerable is caught before missing
detail is collected.

| Section | Rules | Note |
|---|--:|---|
| the source cannot answer this at all | 19 | |
| what privacy forbids | 5 | **No PMAY counterpart** |
| the batch duality | 5 | **No PMAY counterpart** |
| terms that look answerable but mean several things | 10 | |
| money | 4 | |
| time | 6 | |
| geography | 16 | |
| status | 5 | |
| cross-scheme | 4 | **No PMAY counterpart** |
| data quality | 2 | |
| shape of the answer | 13 | |

### 5.2 `focusplus_default_rules.yaml` — 67 rules

`condition` + `default_value` + `assumption_text`, with `sql_effect` on 4. Every fill is stated
back to the user so it can be corrected without an interruption first.

Sections: data quality · **batch** · time · geography · which measure · which table · units ·
shape of the answer · nulls and zeros.

### 5.3 The four defaults that invert PMAY

| Condition | PMAY | Focus Plus | Why |
|---|---|---|---|
| `placeholder_filter_attempted` | `WHERE NOT is_placeholder` on every count | **no filter** | The column does not exist — a copied predicate errors |
| `null_measure_values` | exclude 462 nulls from averages | **not applicable** | `amount_disbursed` is `NOT NULL`, the only such money column in `megh_db` |
| `zero_measure_values` | include with note | **not present** | Only two values exist: 5,000 and 2,500 |
| `money_unit_unspecified` | `crore` | **as stored, per `dim_scheme`** | The unit is unverified |

### 5.4 The two conditions that split across both files

These are the finest lines in the pair and the easiest to get wrong on a future edit:

| Situation | Gate (ask) | Default (assume) |
|---|---|---|
| The word *beneficiaries* | `beneficiary_reading_unclear` — fires when the term arrives **bare** | `bare_beneficiary_term_in_scoped_question` — counts payments, labelled, when the question is **already scoped** |
| Villages with `has_geo_conflict` | `geo_conflict_villages_in_scope` — fires when the answer **turns on** block or district | `geo_conflict_rows_in_non_geographic_answer` — included silently otherwise |

---

## 6. Verified facts (profiled from the CSV, 2026-09-03)

Everything in this section is a **source-CSV observation. None of it has been measured against
`megh_db`.** Run `meta.v_reconciliation_focus_plus` before quoting any of it.

### 6.1 The batch duality — the headline finding

`batch_label` splits the fact into two structurally different datasets. Nothing in the DDL says
so, and no other scheme in `megh_db` has this shape.

| | `93K` — legacy paid | `12.5K` — newer registrations |
|---|--:|--:|
| Rows | 373,144 | 12,527 |
| Beneficiaries | 93,286 | 12,527 |
| Grain | 4 rows per person (one per tranche) | 1 row per person |
| Tranches | all four | Tranch 4 only |
| Financial years | 2022-23 (T1), 2025-26 (T2–T4) | 2025-26 |
| Districts reached | 12 | **6** |
| Distinct village codes | 2,696 | 1,351 (826 shared) |
| `gender` / `occupation` / `focus_status` / `verification_status` / `member_id` / `pincode` | **all blank** | populated |
| `bank_name` | real bank names | **numeric codes** |
| Identifier | `epic_id` | `member_id` |

**Six columns are populated on 3.2% of rows.** Any breakdown by one of them describes the newer
cohort and not the scheme.

### 6.2 The `epic_id` loss — why the headline number is uncomputable

The SME glossary states *"Total Focus+ beneficiaries = 93,286 distinct Member IDs."* That is
wrong twice over:

- In the source, 93,286 is the count of distinct **`epic_id`**, not `member_id`.
- `member_id` is **blank on all 373,144 legacy rows**.
- `epic_id` was **dropped at ingest** and is not in `curated`.

So **no column in `megh_db` reproduces the 93,286 figure the entire workbook is built on.**
`COUNT(DISTINCT member_id)` returns ~12,527. `source_sl_no` is the only proxy, and it is a file
serial, not a person key. This single fact drives `semantic_rules.beneficiary_term`,
`beneficiary_reading_unclear` and the refusal of every EPIC-keyed use case.

### 6.3 Tranche, year and amount — an exact matrix

| Batch | Tranche | FY | Amount | Rows |
|---|---|---|--:|--:|
| 93K | Tranch 1 | 2022-23 | 5,000 | 93,286 |
| 93K | Tranch 2 - August | 2025-26 | 2,500 | 93,286 |
| 93K | Tranch 3 - December | 2025-26 | 2,500 | 93,286 |
| 93K | Tranch 4 - Feb-March | 2025-26 | 2,500 | 93,286 |
| 12.5K | Tranch 4 - Feb-March | 2025-26 | 2,500 | 12,527 |

**Total = ₹1,19,73,92,500 (119.74 crore)** if the unit is rupees — unconfirmed, see §8.

Consequences: only two amounts exist, so `AVG(amount_disbursed)` is a mix ratio and not an
entitlement; Tranch 4 is the two cohorts merged (105,813 rows), so a four-way tranche split is
not clean; and the stored spelling is **`Tranch`**, not `Tranche`.

### 6.4 Time is two points and a gap

Only **FY 2022-23** and **FY 2025-26** hold data. `dim_year` holds four years; the two middle
years are empty for this scheme. A "trend over time" is two points — a line drawn through the gap
would be a fabrication.

Both years fall inside the `dim_year` window, so `year_key IS NULL` should be **empty** here.
Unlike PMAY, a non-zero count is a load defect, not an expected case.

### 6.5 Geography

| District | Legacy rows | ÷4 = beneficiaries | Workbook says |
|---|--:|--:|--:|
| West Garo Hills | 121,600 | **30,400** | **34,720** ✗ |
| South West Garo Hills | 51,288 | **12,822** | **8,502** ✗ |
| East Khasi Hills | 48,856 | 12,214 | 12,214 ✓ |
| East Garo Hills | 39,100 | 9,775 | 9,775 ✓ |
| North Garo Hills | 36,372 | 9,093 | 9,093 ✓ |
| South Garo Hills | 32,432 | 8,108 | 8,108 ✓ |
| West Khasi Hills | 27,016 | 6,754 | 6,754 ✓ |
| South West Khasi Hills | 7,168 | 1,792 | 1,792 ✓ |
| Eastern West Khasi Hills | 4,884 | 1,221 | 1,221 ✓ |
| Ri Bhoi | 1,676 | 419 | 419 ✓ |
| East Jaintia Hills | 1,616 | 404 | 404 ✓ |
| West Jaintia Hills | 1,132 | 283 | 283 ✓ |

Ten of twelve reproduce **exactly**. The two that do not sum identically either way
(30,400 + 12,822 = 34,720 + 8,502 = **43,222**), so this is a reallocation of **4,320 members**
between the two districts under a newer mapping — not a data loss.

Also: **77 distinct block labels** (including municipal boards, which are wards); 4,574 distinct
village *names* against 3,221 distinct village *codes*; 4 rows with no district.

**102,923 rows (26.7%) carry no `mapped_village_lgd_code`** while `fact.geography_key` is
`NOT NULL`. How they resolved is unknown — see §8.

### 6.6 Status, gender, occupation — the 12.5K cohort only

| `focus_status` | rows | | `verification_status` | rows |
|---|--:|---|---|--:|
| Pending | 11,812 | | Approved | 12,527 |
| Approved | 714 | | *(everything else)* | NULL |
| Rejected | **1** | | | |

| `gender` | rows | | `occupation` | rows |
|---|--:|---|---|--:|
| Female | 10,263 | | Farmer | 8,533 |
| Male | 2,264 | | Unemployed | 2,494 |
| | | | Others | 904 |
| | | | Business | 296 |
| | | | Private Employee | 194 |
| | | | Student | 83 |
| | | | Government Employee | 23 |

`verification_status` has **one value** and therefore zero discriminating power. `focus_status`
has a **singleton** — one Rejected record is not a trend. And *Approved* means the registration
was approved, **not** that a payment was made: the Approved rows are in the cohort that has not
been paid.

### 6.7 Paid vs unpaid is encoded nowhere

The workbook asks which field signals payment across **eleven use cases** (UC-013, 027–032, 073,
074, 085, 090, 098, 102–107). The answer from the data:

- `status` was the constant `'106'` and was **dropped at ingest**.
- `verification_status` is `Approved` on every populated row.
- `focus_status` describes an *application*, and exists only on the unpaid cohort.

**`batch_label` is the only available discriminator, and using it is an inference, not a
declared fact.** Every answer resting on it must say so.

Worse: **111 `epic_id`s appear in both cohorts** — people already paid who registered again. So
the batches are not a clean partition of people, 12,527 overstates the unpaid backlog by an
unknown amount, and because `epic_id` is gone **this overlap cannot be detected in `megh_db` at
all.**

### 6.8 `bank_name_raw` is queryable but dirty

Added to `curated.v_focus_plus` on 2026-09-09 — it is a bank name, not a person identifier, so it
is exempt from the PII rule in §1 and may appear in an answer (bank-wise breakdowns, "which bank"
questions).

42 distinct source values = **27 real bank names + 15 numeric codes**. The legacy batch stores
names, the 12.5K batch stores codes. Casing is inconsistent (`State Bank of India` vs
`BANK OF BARODA`), one value carries a **leading space** (`' UJJIVAN SMALL FINANCE BANK'`), and
one is misspelled (`Cananra Bank`). Top three by total `amount_disbursed` (raw, unit unverified —
see §6.3): SBI 808,575,000 · Meghalaya Rural Bank 235,087,500 · Meghalaya Co-op Apex 78,625,000 — the
numeric-code rows sit far below these, so a top-1 "which bank" answer is unaffected, but a fuller
breakdown should flag any numeric-code row as an unresolved raw value rather than a bank name.

There is no normalisation table, so do not present the 42 raw values as 42 banks. Account numbers
and IFSC codes are still not held anywhere in `megh_db`, for any scheme.

---

## 7. Where the workbook and the data disagree

Five verified contradictions. All are encoded as rules; none should be silently "fixed" in the
workbook without SME agreement.

| # | Workbook says | Data says | Encoded as |
|--:|---|---|---|
| 1 | "93,286 distinct Member IDs" | 93,286 distinct `epic_id`; `member_id` blank on all legacy rows, and `epic_id` dropped at ingest | `beneficiary_reading_unclear`, `semantic_rules.beneficiary_term` |
| 2 | Flat ₹5,000 per beneficiary (UC-004, 006, 014, DC-014, DC-030, DC-045) | Two rates: 5,000 in T1, 2,500 in T2–T4 | `standard_payment_reading_unclear`, `average_payment_requested` |
| 3 | WGH 34,720 / SWGH 8,502 | 30,400 / 12,822 — sum preserved | `workbook_district_figures_differ` |
| 4 | "25,037 producer groups" (QA-002) | No producer-group column exists anywhere | `producer_group_requested` + a `sql: null` refusal |
| 5 | One member appears in several tranches (rule 7 example, DC-034) | True for legacy rows — which carry **no** `member_id`; within the 12.5K batch it is unique per row | `member_id` business rules |

A sixth, milder one: the workbook's UC-003 Query Logic proposes a coverage rate against
"eligible farming-household count", then annotates itself that the logic is irrelevant. There is
no denominator in `megh_db` — encoded as `eligible_population_or_coverage_rate_requested`.

---

## 8. Open issues

Ordered by how likely each is to produce a wrong published number.

1. **The money unit is unverified.** `amount_disbursed` is a bare `numeric` with no documented
   unit and **no known precision**. Read `curated.dim_scheme.money_unit` for the Focus Plus row.
   Source values of 5,000 and 2,500 corroborate rupees but do not settle it. Three units are
   already live in `megh_db`; a wrong fourth is the most likely way to publish a wrong figure.
2. **The 102,923 unmapped rows.** 26.7% of source rows have no village code, yet
   `fact.geography_key` is `NOT NULL`. Quarantined? Mapped to a placeholder? **Unknown.** Resolve
   before publishing any village- or block-level figure. Check `staging.quarantine` and
   `meta.v_reconciliation_focus_plus`.
3. **No verified volumes or totals.** Nothing has been double-derived. Same bar as PMAY: measure
   twice, then quote.
4. **Focus Plus is not in `MASTER/schemes_catalog.yaml`.** Node 1 cannot route a Focus Plus
   question yet. The catalogue has two entries; this is a four-step job per that folder's README
   §6.4, and stage2 now exists, so keywords can be derived from it.
5. **Grant status unconfirmed.** Both objects were created after the last access review. A new
   object does not inherit a grant unless `ALTER DEFAULT PRIVILEGES` was in place, so they may be
   invisible — or over-granted — to `megh_readonly`. Run `meta.v_access_matrix`.
6. **Presence in `v_cross_scheme_money_district_year` is unknowable from structure.** Run
   `SELECT DISTINCT scheme_code` before claiming any three-scheme comparison.
7. **`v_cross_scheme_village_coverage` has no Focus Plus column.** Use the aggregate-then-join
   pattern until it does.
8. **`dim_year.data_quality_note` is scheme-blind.** Both current notes are MGNREGA facts, and
   2025-26 — where three quarters of Focus Plus rows sit — carries one. Do not surface it on a
   Focus Plus answer.
9. **The semantic catalog has never seen this fact.** `semantic.table_catalog` (9 rows) and
   `column_catalog` (123 rows) were harvested when `curated` held 9 objects and 123 columns; it
   now holds 18 and 249. Run `semantic.refresh_catalog()`, then set
   `is_chatbot_visible = false` on the fact and keep `member_id` and `pincode` out of
   `sample_values`. `bank_name_raw` is exempt (§6.8) and may appear in sample_values.
10. **`bank_name_raw` retention — RESOLVED 2026-09-09.** PMAY dropped its names at ingest;
    Focus Plus kept two identifying columns (`member_id`, `pincode`) plus `bank_name_raw`, which
    is not identifying. The retention was intentional and `bank_name_raw` was added to
    `v_focus_plus` accordingly; see §6.8.
11. **No Focus Plus scheme document.** Policy questions ("what is the entitlement?") have no
    grounding source, and the workbook's ₹5,000 claim is contradicted by the data.

---

## 9. Regeneration checklist

Run in this order. Steps 1–3 are mechanical and should be automated before the next load.

1. **Re-profile the CSV** with the §3.1 recipe. Every count in every YAML comes from it.
2. **Re-run the workbook reconciliation** (step 7). If a district figure that used to match stops
   matching, the mapping changed — do not silently absorb it.
3. **Validate both gate files parse and do not overlap:**
   ```python
   import yaml, collections
   c = {x['condition'] for x in yaml.safe_load(open('focusplus_classification_rules.yaml'))['clarification_rules']}
   d = {x['condition'] for x in yaml.safe_load(open('focusplus_default_rules.yaml'))['default_rules']}
   assert not (c & d), sorted(c & d)      # MUST be empty
   ```
4. **Check stage2's column list against the live view** — count and order must match
   `v_focus_plus` exactly, currently 23 columns.
5. **Confirm no PII column carries `sample_values`.**
6. **Re-read `SCHEMA_FOR_DEVELOPERS.md`'s change log.** These files are pinned to the 2026-09-02
   extract.
7. **Run all three reconciliation views** before any figure is published.
8. **Append, never replace.** Old questions must keep resolving.

---

## 10. House rules

1. **A fact about a column belongs in stage2; a fact about a value belongs in the resolver; a
   fact about asking-versus-assuming belongs in exactly one gate file.**
2. **Every number in this folder is a source-CSV observation until measured against the
   database.** Mark it `unverified_in_db` and say so in the answer.
3. **PII is not negotiable.** `member_id` and `pincode` never reach a user, an export, or a
   vector store. `member_id` is permitted only inside `COUNT(DISTINCT)`. `bank_name_raw` is not
   PII and is exempt (§6.8).
4. **Never copy a PMAY predicate across.** `is_placeholder`, `is_completed`, `mapping_category`,
   `sanction_date` and `installments_paid` do not exist here.
5. **Never present a 3.2%-coverage breakdown as a scheme figure.**
6. **`COUNT(*)` is payments.** Say so, every time.
7. **Do not repeat a workbook figure this folder has contradicted** — see §7.
8. **Query the view, never the fact.**
9. **Match the PMAY file format exactly.** Same top-level keys, same record fields, same
   `# ---- section ----` dividers, comparable comment density. A v1.0 file carries no `v2_note`;
   put commentary in comments, and put reasoning in stage2 rather than repeating it per rule.

---

## 11. Change log

| Date | Change |
|---|---|
| 2026-09-02 | Focus Plus loaded into `megh_db` — `curated.fact_focus_plus_disbursement` (22 cols) and `curated.v_focus_plus` (23 cols). `meta.v_reconciliation_focus_plus` added. Folder `Annotations/Focus+/` created with seven empty files. |
| 2026-09-03 | Source CSV profiled (385,671 rows) and the use-case workbook read (8 sheets, 352 rows). The batch duality, the `epic_id` loss, the two payment rates, the WGH/SWGH reallocation and the 111-EPIC overlap were found and recorded. |
| 2026-09-03 | `focusplus_schema_partitions.yaml` v1.0 written — 1,366 lines, 23 view columns in declared order, 34 few-shot examples of which 13 are refusals. |
| 2026-09-03 | `focusplus_classification_rules.yaml` v1.0 written — 89 rules. Reformatted to match PMAY exactly: `condition` + `question` only, `# ---- section ----` dividers, per-rule commentary removed (comment share cut from 49% to 13%). |
| 2026-09-03 | `focusplus_default_rules.yaml` v1.0 written — 67 rules, 4 with `sql_effect`. Verified zero condition overlap with the gate file. |
| 2026-09-03 | `focusplus_entity_resolver.yaml` v1.0 written — 1,610 lines. 11 dimensions, 50 blocks, 186-entry shared-village registry, 18 blocked pairs, 12 worked examples. Found the trailing-space block artefact (26 of 77 raw strings) and the `'Nan'` null-text values. Added a fourth output shape, `refused`, for PII. |
| 2026-09-03 | `focusplus_few_shot.yaml` v1.0 written — 70 question-to-SQL pairs, 841 lines. Refusals deliberately left in stage2, matching PMAY's split. None executed against `megh_db`. |
| 2026-09-03 | `focusplus_foreign_key_augmentation.yaml` v1.0 written — 511 lines, 13 nodes, 11 edges. **Every FK marked `declared: inferred`** — the 2026-09-02 extract carries no constraint data. 7 prohibited edges including a self-join on `member_id` and the detail-to-coverage-view join. Added an `absent_objects` section. |
| 2026-09-03 | `focusplus_response_template.yaml` v1.0 written — 344 lines, 27 formatting keys, 87 templates, 45 follow-up rules. Encodes the six mandatory caveats. **All seven YAMLs are now built.** |
| 2026-09-03 | This README added, then updated as each file landed. |

---

## 12. Backlog — what remains

All seven YAMLs are built. Nothing further is committed work; per standing instruction, no YAML
is written until told which and how. What remains is **verification, not authoring** — and none
of it can be done from this folder. See §8 for the full list, ordered by risk. The four that
block publication:

1. **Read `curated.dim_scheme.money_unit`.** No Focus Plus money figure ships until this is done.
2. **Resolve the 102,923 unmapped rows.** Blocks every village- and block-level answer.
3. **Run `meta.v_reconciliation_focus_plus`.** No total is quotable until it agrees.
4. **Run `meta.v_access_matrix`.** Both objects were created after the last access review, so
   `megh_readonly` may not be able to read them at all — or may be over-granted.

Two authoring jobs sit outside this folder:

- **`MASTER/schemes_catalog.yaml` has no Focus Plus entry.** Node 1 cannot route a Focus Plus
  question yet. Stage2 now exists, so the keywords can be derived from it per that folder's
  README §6.4.
- **`semantic.refresh_catalog()` has never seen this fact**, and when it runs, `member_id` and
  `pincode` must be kept out of `sample_values` and the embedding set. `bank_name_raw` is exempt
  (§6.8).
