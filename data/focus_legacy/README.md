# Focus Legacy Annotation Layer — How the YAMLs Are Built

This folder holds the hand-curated annotation layer for the **Focus Legacy** scheme (Meghalaya)
of the Megh One AI NLP-to-SQL bot. Everything here is derived from one Excel workbook in
`datasets/Focus legacy/` and from the live database structure. Nothing in this folder is
generated at runtime — these files are the reviewed, SME-owned contract that the retrieval and
SQL-generation stages read.

It is the sibling of `Annotations/PMAY/README.md`, `Annotations/Focus+/README.md` and
`Annotations/CMElevate/README.md`. Read the PMAY one for the house method; this one records what
is different about Focus Legacy, and Focus Legacy is different in **six** ways that matter:

1. **It is the only group-level fact in `megh_db`.** PMAY is house-grain, Focus Plus is
   person-grain, CM Elevate is application-grain, MGNREGA is village-year-grain. Focus Legacy
   pays a **producer group**. There is no individual anywhere in the partition. See §4.8.
2. **The money is fully determined by the member count.** `amount_disbursed =
   no_of_pg_members × 5000` on every row, zero exceptions, verified twice. A rupee question and
   a member question are the same question. This governs almost every answer. See §4.1.
3. **It has a real date, but only 30 of them.** Payments go out in bulk treasury runs. Daily and
   monthly groupings are valid SQL and misleading prose. See §4.3.
4. **FY 2023-24 is absent from the data, not empty in it.** Four years with a hole in the middle.
   See §4.3.
5. **The `pg_id` encodes a district that is sometimes the wrong one** — stale on 1,075 rows,
   because of district bifurcation. See §4.4.
6. **It carries banking PII, and the view transforms rather than drops it.** `account_no` is
   masked to the last four digits; `name_on_the_account` is dropped outright. A third pattern,
   different from both PMAY and Focus Plus. See §4.7.

Like Focus Plus and CM Elevate, Focus Legacy has **no flat-table ancestor**. It was loaded
straight into the curated star schema, so every file here is v1.0 written against the database as
it stands, with no migration to record.

> **Status: all 7 YAMLs built.** 6,649 lines across the seven files, every one parsing clean.
> What remains is verification against the live database, not authoring — see §11 and §15.

---

## 1. What is in this folder

| File | Layer | Answers the question | Status |
|---|---|---|---|
| `focuslegacy_schema_partitions.yaml` | **Semantic schema** (*stage2*) | *What columns exist, what do they mean, how are they aggregated?* | **Built** — 1,796 lines, v1.0 |
| `focuslegacy_classification_rules.yaml` | **Clarification gate** | *What must the bot ask about instead of guessing?* | **Built** — 430 lines, 124 rules |
| `focuslegacy_default_rules.yaml` | **Defaults** | *What gets filled silently, and what assumption is stated back?* | **Built** — 390 lines, 81 rules |
| `focuslegacy_entity_resolver.yaml` | **Entity resolver** | *The user typed "WGH", "piggery", "FY 2024-25" — which stored value is that?* | **Built** — 1,481 lines, 15 dimensions |
| `focuslegacy_foreign_key_augmentation.yaml` | **Join graph** | *Which joins are permitted, and which are prohibited?* | **Built** — 1,103 lines, 21 nodes / 28 edges |
| `focuslegacy_few_shot.yaml` | **SQL examples** | *What does correct SQL look like for this scheme?* | **Built** — 1,063 lines, 81 examples |
| `focuslegacy_response_template.yaml` | **Response composer** | *How is the answer worded, and which caveats travel with it?* | **Built** — 385 lines, 101 templates |
| `README.md` | This guide | *How were these built, what was found, and what must never change?* | This file |

The seven filenames follow the PMAY / Focus+ / CM Elevate convention — a scheme prefix on every
file. (MGNREGA is the odd one out: three of its seven carry no prefix. The majority convention
was used here.)

### 1.1 Where they sit in the pipeline

```
user question
   |
   v
[ NODE 1 - scheme classification ]  <-- MASTER/schemes_catalog.yaml
   |                                    NOTE: Focus Legacy is NOT in the catalogue yet. See §11.
   v
[ clarification gate ]   <-- focuslegacy_classification_rules.yaml     BUILT
   |  124 rules. "beneficiaries", a named group, a monthly trend -> ask, and pause
   |  the x5000 identity, PII, the FY2023-24 gap -> refuse and redirect
   v
[ defaults ]             <-- focuslegacy_default_rules.yaml            BUILT
   |  81 rules. top N -> 5, no period -> all four years, bare count -> payments
   |  every fill is recorded as an assumption and stated back
   v
[ entity resolution ]    <-- focuslegacy_entity_resolver.yaml          BUILT
   |  15 dimensions. "WGH" -> WEST GARO HILLS; "piggery" -> 3 spellings
   |  a pg_id is never parsed for geography; account numbers are refused
   v
[ schema linking ]       <-- focuslegacy_schema_partitions.yaml        BUILT
   v
[ join discovery ]       <-- focuslegacy_foreign_key_augmentation.yaml BUILT
   |  11 declared FKs, 16 prohibited edges, 6 sanctioned patterns
   |  the only SNOWFLAKE in megh_db - dimensions are not terminal here
   v
[ SQL generation ]       <-- focuslegacy_few_shot.yaml                 BUILT
   |  81 question-to-SQL pairs, all FROM curated.v_focus_legacy
   |  the 11 refusals stay in stage2, as PMAY and Focus+ do
   |  -> SQLGlot validation -> read-only execute
   v
[ response ]             <-- focuslegacy_response_template.yaml        BUILT
      101 templates, 45 follow-ups, 16 composer checks
      seven mandatory caveats travel with the numbers they belong to
```

**Rule of thumb:** if the fact is about a *column* (meaning, type, unit, aggregation, whether it
is excluded) it belongs in stage2. If the fact is about a *value* (aliases, casing, ambiguity,
derived groups, what to do when it is not found) it belongs in the resolver. If it is about
whether to *ask or assume*, it belongs in one of the two gate files — and in exactly one of them.

---

## 2. The source data

### 2.1 `Focus Legacy to share to BLH.xlsx` — the data

One sheet, `Sheet1`. **14,569 rows × 22 columns.** Profiled 2026-09-21.

| # | Source column | Fate at ingest | Notes |
|---|---|---|---|
| 0 | `id` | → `fact.source_row_id` | UNIQUE constraint. 14,569 distinct |
| 1 | `name_of_the_programme_updated` | → `programme_variant` | 2 values |
| 2 | `pg_id` | → `dim_producer_group.pg_id` | 11,906 distinct; 2 malformed |
| 3 | `name_of_pg` | → `dim_producer_group.current_name` → `pg_name` | 10,678 distinct — fewer than groups |
| 4 | `product` | → `product_raw` (verbatim) | 3,112 populated (21%); 62 spellings |
| 5 | `no_of_pg_members` | → `no_of_pg_members` | NOT NULL; 21 distinct values |
| 6 | `name_on_the_account` | → `fact.name_on_the_account` | **PII. View drops it** |
| 7 | `account_no` | → `fact.account_no` → `account_no_masked` | **PII. View masks it** |
| 8 | `bank_name` | → `bank_name` | 13 values, 276 nulls |
| 9 | `branch` | → `branch_raw` | 3,192 populated (22%) |
| 10 | `ifsc_code` | → `ifsc_code` | 113 values, no nulls |
| 11 | `amount` | → `amount_disbursed` | = col 5 × 5000, always |
| 12 | `date_of_remittance` | → `date_of_remittance` | 30 distinct dates; 171 nulls |
| 13 | `financial_year` | → `year_key` → `dim_year` | 4 values; matches `financial_year_short` |
| 14 | `mapped_district_lgd_code` | **DROPPED** | No district code in the curated layer |
| 15 | `mapped_district_lgd_name` | → `dim_geography.lgd_district` | 12 districts; **UPPERCASED in the dimension** |
| 16 | `mapped_block_lgd_code` | **DROPPED** | No block code in the curated layer |
| 17 | `mapped_block_lgd_name` | → `dim_geography.lgd_block` | 56 blocks; **UPPERCASED** |
| 18 | `mapped_village_lgd_code` | → `village_code` / `geography_key` | 3,384 codes; 1,101 rows blank |
| 19 | `mapped_village_lgd_name` | → `dim_geography.lgd_village_name` | 3,270 names — fewer than codes |
| 20 | `mapped_constituency_name` | **NOT CARRIED ON THE FACT** | Roster `ac_name` is reachable instead — see §5.4 |
| 21 | `mapped_constituency_name_and_number` | **NOT CARRIED ON THE FACT** | Source form `41 SONGSAK` |

### 2.2 There is no use-case workbook

Focus Plus has `Copy of Megh One AI - FOCUS+ NLP Use Cases.xlsx`; CM Elevate has
`Megh One AI - CM Elevate NLP Use Cases.xlsx`. **Focus Legacy has neither.** `datasets/Focus
legacy/` holds the data file and nothing else.

This is a real gap, not an oversight to paper over. It means:

- No SME-stated requirement to check the annotation against, and no list of the questions the
  client actually wants answered.
- No stale-figure reconciliation is possible — the check that found the wrong district numbers in
  the Focus+ workbook (its README §7) has no counterpart here.
- The clarification gate and the defaults file have **no external evidence of what users ask.**
  They have to be derived from the data's own traps plus the conventions of the sibling folders.
  `focuslegacy_classification_rules.yaml` says exactly that in its own header, and its wording
  should be treated as a first draft to be corrected against real user questions. See §6.6.

---

## 3. Excel to YAML: the derivation pipeline

### 3.1 The profiling recipe

Run in this order against `Sheet1`. Every count in `focuslegacy_schema_partitions.yaml` comes
from it.

```
# Step 1 - shape, header, per-column null census
#   -> feeds the stage2 column list and the ingest-fate table in §2.1

# Step 2 - the entitlement identity test
#   -> THE MOST IMPORTANT STEP IN THIS FOLDER.
#      assert (amount == no_of_pg_members * 5000).all()
#      It passed on 14,569 of 14,569. Nothing about the money reads
#      correctly until this is known.

# Step 3 - low-cardinality value census
#   -> programme (2), financial_year (4), bank_name (13), members (21),
#      amount (21), product (62 raw / 45 case-folded)
#   -> feeds stage2 sample_values and, later, the resolver catalogues

# Step 4 - pg_id structure probe
#   -> prefix split (PG-FOCUS / PG-LAMP / PG-EXISTING), whitespace check,
#      and the district-token-vs-mapped-district comparison
#   -> THE STEP THAT FOUND THE STALE DISTRICT TOKEN (§4.4) AND THE TWO
#      MALFORMED IDS (§4.2)

# Step 5 - duplicate and key probes
#   -> full-row, (pg_id, date, amount), (pg_id, date), (pg_id, FY)
#   -> isolated the single disputed pair the loader quarantined (§4.2)

# Step 6 - time consistency
#   -> derive FY from date_of_remittance, compare against the stated
#      financial_year on all 14,398 dated rows
#   -> zero mismatches. Confirms the two time columns are interchangeable
#      for bucketing, and that a future mismatch is a defect

# Step 7 - geography completeness and the reconciliation arithmetic
#   -> district / block / village coverage, the 1,101 unmapped rows,
#      and 14,569 - 3 = 14,566 against the live extract
```

Steps 2, 4, 5 and 7 are the ones that changed what the YAML says. Steps 1, 3 and 6 confirm and
populate.

### 3.2 Choosing `category` and `nlp_sql_priority`

Same convention as the sibling folders. `nlp_sql_priority: critical` is reserved for columns a
user will name directly or that carry a trap — here: `pg_id`, `pg_entity_type`,
`programme_variant`, `financial_year`, `financial_year_short`, `date_of_remittance`,
`lgd_district`, `lgd_block`, `lgd_village_name`, `entity_type`, `no_of_pg_members`,
`amount_disbursed`. Surrogate keys are `low`. Audit columns are not in the view at all.

New `category` values this partition introduces, because no sibling scheme has them:
`producer_group_identifier`, `producer_group_dimension`, `programme_dimension`,
`membership_measure`, `product_dimension`, `banking_dimension`, `banking_identifier_masked`,
`banking_quality`.

---

## 4. Verified facts (profiled 2026-09-21, `unverified_in_db` unless stated)

### 4.1 The ×5000 identity — the headline finding

```
amount_disbursed = no_of_pg_members * 5000
```

Holds on **14,569 of 14,569 source rows.** Independently asserted by the DB table comment, which
records it as verified against the source file on 2026-09-16. This is the best-verified claim in
the partition, and it is the one that reshapes the most answers:

- `SUM(amount_disbursed)` = `5000 × SUM(no_of_pg_members)`, always.
- `AVG(amount_disbursed)` is **not** an average entitlement — it is 5000 × average group size. It
  moves when group sizes move, never when policy does.
- "How much per member" has the constant answer 5,000. Say the rate; do not compute a ratio that
  can only return 5,000.
- A ranking by amount and a ranking by members are **the same ranking**. They are not two
  findings.
- A correlation or regression between the two is degenerate — it is one column against itself.

Source totals: **102,021 member-payments, ₹51,01,05,000** (510,105,000), across 21 distinct
amounts all of which are multiples of 5,000. Maximum 950,000.

**Caveat carried into the YAML:** the 5000 rate is an *observed invariant*, not a documented
policy parameter. Nothing in `megh_db` states it as a rule. If a future load breaks it, the break
is the finding.

### 4.2 The load reconciles exactly — and the three held-back rows are identified

```
14,569 source rows   -  3 quarantined  =  14,566 curated   (matches the live extract)
11,906 source pg_ids -  2 rejected     =  11,904 groups    (matches the live extract)
```

This is why the source figures in this folder are unusually safe to reason with — rarely true of
a v1.0 annotation. All three held-back rows were located in the spreadsheet:

| Reason code | Count | Which rows | Judgement |
|---|---|---|---|
| `DUPLICATE_DISBURSEMENT` | 1 | `PG-FOCUS-WKH-5956`, source ids **1148** and **2552** | Same 19 members, same ₹95,000, same HDFC account — but different dates (2022-04-20, 2022-05-18) and different product (Vegetables, Squash). Genuinely ambiguous: two real tranches would look identical |
| `BAD_PG_ID_PREFIX` | 2 | source ids **3101** (`PG-FOCUS- RB- 7751`) and **14565** (`PG- FOCUS-EKH-12359`) | **Malformed by stray spaces, not unknown prefixes.** A whitespace normalisation at ingest recovers both. Worth raising with the data owner |

The loader's duplicate judgement is defensible and the probe confirms why: seven `(pg_id, FY)`
pairs exist in the source, but six of them are September-2025 payments followed by January- or
March-2026 top-ups with **different member counts** — legitimate second payments. Only the
WKH-5956 pair is identical in members and amount. There are **zero** full-row duplicates and
**zero** `(pg_id, date)` duplicates.

**Consequence for answers:** the database total is ₹95,000 lower than the spreadsheet's. If a
user's own figure is slightly higher, this is the likely difference — explain it rather than
disputing their number.

### 4.3 Time — a real date, four years, and a hole

| Financial year | Payments | Programme |
|---|---|---|
| 2021-22 | 965 | FOCUS only |
| 2022-23 | 2,139 | FOCUS only |
| **2023-24** | **0** | **no data at all** |
| 2024-25 | 2,653 | FOCUS only |
| 2025-26 | 8,812 | FOCUS 8,106 + Focus (Addnl) 706 |

`dim_year` holds 2023-24, so a `GROUP BY` returns **no row** for it rather than a zero. Never draw
a line through the gap; whether it is a programme pause or an extract gap is **unknown**.

`date_of_remittance` spans 2021-08-31 to 2026-03-31 and takes **30 distinct values.** Three dates
carry 9,193 of 14,569 rows:

| Date | Payments |
|---|---|
| 2025-09-11 | 3,503 |
| 2025-06-18 | 3,359 |
| 2024-10-08 | 2,331 |
| 2025-06-20 | 1,041 |
| 2022-05-17 | 783 |

A monthly chart is a **treasury-batch chart**. Label it as one, and never report a month with no
batch as a month of zero disbursement.

**Time consistency (step 6):** the April–March bucket derived from `date_of_remittance` matches
the stated `financial_year` on **all 14,398 dated rows** — zero mismatches. The 171 dateless rows
(170 of them FY2025-26) still carry a financial year, so a date-grouped answer and an FY-grouped
answer return different totals for the same question. Prefer the FY when completeness matters.

### 4.4 `pg_id` structure — and the stale district token

Pattern: `PG-<TYPE>-<DISTRICT_ABBR>-<NUMBER>`, e.g. `PG-FOCUS-WGH-7089`.

| Prefix | Rows |
|---|---|
| `PG-FOCUS` | 12,721 |
| `PG-LAMP` | 1,794 |
| `PG-EXISTING` | 53 |

Three prefixes, three rows in `dim_pg_entity_type`. The correspondence is clear; **the stored
`type_code` strings are not** — the extract does not carry them. Enumerate before filtering.

**The district token is stale on 1,075 of 14,569 rows**, and every disagreement is explained by
district bifurcation:

- 990 rows whose `pg_id` says `WKH` now map to **Eastern West Khasi Hills** (LGD 740).
- 85 rows whose `pg_id` says `WGH` now map to **South West Garo Hills**.

The mapped LGD district is correct; the token records the district at enrolment. **Never derive
geography from `pg_id`.** Note also that the numeric suffix is unique only within a prefix —
`PG-FOCUS-WKH-5956` and `PG-LAMP-WGH-5956` are different groups.

### 4.5 Geography

- **12 districts**, all of Meghalaya. Largest: West Garo Hills 3,493, North Garo Hills 1,476,
  West Jaintia Hills 1,325, Ri Bhoi 1,233, West Khasi Hills 1,104.
- **56 blocks.** Largest: Mairang 699, Laskein 647, Resubelpara 569, Rongram 561, Selsella 558.
- **3,384 village codes** against **3,270 village names** — names are not unique, so village
  counts use `COUNT(DISTINCT village_code)`.
- **1,101 rows (7.6%) have no village**: 1,013 have a block but no village, 88 have neither.
  These become `entity_type = 'Unresolved'` placeholders — real payments, placed under a district
  so totals reconcile, but never a real village.
- **Geography is stable per group**: zero `pg_id`s map to more than one village, matching the DB
  comment's "0 conflicts across 11,906 groups".
- **Casing trap:** the spreadsheet is Title Case throughout; `dim_geography` stores district and
  block **UPPERCASE** and village names mixed case. A value copied from the file matches the
  village column and silently returns zero rows against the other two.

**Open:** whether an `Unresolved` placeholder preserves the source block. If it does not, the
1,013 block-only rows vanish from block totals — a bigger loss than the village figure implies.

### 4.6 The coverage cliff — `product` and `branch`

| Financial year | `product` populated | `branch` populated |
|---|---|---|
| 2021-22 | 965 / 965 (100%) | 965 / 965 (100%) |
| 2022-23 | 2,139 / 2,139 (100%) | 2,002 / 2,139 (94%) |
| 2024-25 | **0 / 2,653** | 1 / 2,653 |
| 2025-26 | **8 / 8,812** | 224 / 8,812 |
| **Total** | **3,112 / 14,569 (21%)** | 3,192 / 14,569 (22%) |

Both fields were captured properly for two years and then effectively stopped. The DB column
comment confirms the gap is in the **source**, not the load. Every product answer therefore
describes FY2021-22 and FY2022-23 only, and must say so. A product question about 2024-25 or
2025-26 is unanswerable — because the field stopped being captured, *not* because the groups had
no products.

`product` also needs case folding: **62 raw spellings collapse to 45.** Known collisions include
`Piggery / PIGGERY / piggery`, `Ginger / GINGER / ginger`, `Squash / SQUASH / squash`,
`Poultry / POULTRY / poultry`, `Betel Leaf / Betel leaf`, `Long Pepper / Long pepper /
long pepper`. A bare `GROUP BY product_raw` splits piggery into three rows and can change which
product ranks first.

### 4.7 Banking, and the PII boundary

- **13 banks**, Title Case, clean. **Two carry 92%**: Meghalaya Rural Bank 8,174 and Meghalaya
  Co-operative Apex Bank 5,270. Unlike Focus Plus — where the view withholds `bank_name_raw`
  because the values are inconsistent — **bank-wise analysis is legitimate here.**
- `bank_name` is NULL on 276 rows where `ifsc_code` is present, so the IFSC identifies the bank
  where the name is missing.
- **113 IFSC codes**, present on every row. One covers 8,328 rows — `SBIN0RRMEGB`, Meghalaya Rural
  Bank's sponsor-issued code. **It is not a branch.** Do not present IFSC counts as branch counts.
- **9,615 distinct account numbers.** 250 are used by more than one `pg_id`; 107 `pg_id`s use more
  than one account. An account is **not** a group identity.
- **78 account numbers contain an embedded space** (`20200 1319274`), all in the FY2021-22 North
  Garo Hills run. Whether the loader stripped it is unverified.
- `name_on_the_account` is present on 10,353 rows and **absent on 4,216, every one of them
  FY2025-26.** It is PII, already `is_chatbot_visible = false`, and outside the boundary anyway.

**The boundary itself is a third pattern.** PMAY de-identified at ingest (names dropped). Focus
Plus keeps person columns on the fact and relies on the view to withhold them. Focus Legacy loads
the identifying columns and the view **transforms** one (`account_no` → `account_no_masked`) and
**drops** the other. `bridge_pg_bank_history` also holds full account numbers — so **two** objects
must stay out of reach, not one.

### 4.8 Groups, names, and the outlier

- **11,906 groups**, **10,678 distinct names.** 1,634 groups have more than one spelling on file.
  Names both split one group and merge several — `COUNT(DISTINCT pg_name)` is wrong in both
  directions.
- Naming is unstable across a group's own payments: `Muskan Producer Group` in June 2025,
  `Muskan Pg` in March 2026.
- **Payments per group:** 9,251 paid once, 2,647 twice, 8 three times. So `COUNT(*)` exceeds
  `COUNT(DISTINCT pg_id)` by about 2,663 — a payment count and a group count are not
  interchangeable.
- A group's recorded size can change between its own payments: `PG-FOCUS-RB-3210` was paid for 1
  member in September 2025 and 12 in January 2026. So even a single-year membership sum is a sum
  of snapshots.
- **One extreme outlier:** `PG-FOCUS-WKH-3565`, **190 members / ₹9,50,000**, against a
  next-largest of 20 members. It obeys the ×5000 rule so it loaded normally. It will dominate any
  average or maximum — exclude it explicitly or mention it.
- **Group size distribution** is dominated by small groups: 2,296 rows at 1 member, with visible
  spikes at the round numbers 10 (1,569) and 20 (548).

---

## 5. Anatomy of `focuslegacy_schema_partitions.yaml` (stage2)

1,796 lines, v1.0, written 2026-09-21. Longer than PMAY's 1,085 and Focus+'s 1,366 — the extra
length is the five scheme-specific objects and the semantic rules the ×5000 identity forces.

### 5.1 The eleven dataset blocks

| Block | Why it is there |
|---|---|
| `v_focus_legacy` | **The primary query surface.** All 28 columns, fully annotated |
| `fact_focus_legacy_disbursement` | Lineage only. Documents the 9 columns the view withholds, the 5 declared FKs, the unique constraint, and `dropped_at_ingest` |
| `dim_producer_group` | Scheme-scoped. Also documents `first_payment_year_key` / `last_payment_year_key`, which the view does **not** expose |
| `dim_pg_entity_type` | Scheme-scoped, 3 rows, codes unknown — enumerate first |
| `bridge_pg_bank_history` | Scheme-scoped. Marked `contains_pii: true` and `never_query_directly: true` |
| `dim_scheme` | Now **5 rows**, not the 4 older annotation files assert |
| `dim_year` | 9 rows; this scheme occupies 4 of them |
| `dim_geography` | Conformed across all five schemes; holds the `ac_number` / `ac_name` route |
| `bridge_geography_source` | **Not in the sibling files.** Added because Focus Legacy is in neither cross-scheme view, so this is the only join-free route to "villages in A but not B" |
| `v_cross_scheme_money_district_year` | Documented **only** to record `includes_focus_legacy: false` |
| `v_cross_scheme_village_coverage` | Same |

Both cross-scheme exclusions are **verified from the view definitions** in the 2026-09-21 extract,
not assumed: the first unions MGNREGA and PMAY, the second joins PMAY and MGNREGA. Filtering
either by a Focus Legacy scheme code returns zero rows, which reads as "no money" rather than "not
modelled" — which is exactly why it is written down.

### 5.2 The eleven deliberate refusals

`few_shot_examples` carries 37 entries: 26 with SQL, 11 with `sql: null`. The refusals are the
point of the file as much as the queries are — and now that `focuslegacy_few_shot.yaml` exists
(§8), **the 11 refusals are what stage2 keeps**, matching how PMAY and Focus+ split the two files.
The 26 query examples here are a subset of the 81 in the few-shot set and can be pruned if the
duplication ever matters.

| Refused question | Ground |
|---|---|
| Who is the account holder for *group X*? | Privacy — not availability |
| Give me the full account numbers | The unmasked column is audit-only, on two objects |
| How many individual beneficiaries? | No person exists in the partition; three different numbers |
| How much was disbursed in FY 2023-24? | The year has no data — a gap, not a zero |
| What did groups spend the money on in 2025-26? | 8 of 8,812 rows populated |
| Which district is *pg_id* in, based on its ID? | The token is stale on 1,075 rows |
| Compare with PMAY using the cross-scheme view | Focus Legacy is not in it |
| Show the Focus Plus beneficiaries for these groups | Prohibited, and no key exists |
| Total of all village LGD codes | Identifiers are not measures |
| Update the member count | Read-only role |
| How many groups are pending payment? | No status dimension exists |

### 5.3 Non-negotiable stage2 behaviours

1. **Query `curated.v_focus_legacy`.** Never the fact, never the bank bridge — both carry
   unmasked account numbers.
2. **There is no mandatory predicate.** Do **not** copy `WHERE NOT is_placeholder` from PMAY; the
   column does not exist and the query will error.
3. **`entity_type <> 'Unresolved'` on village counts and lists only** — never on money, district
   or programme totals, which would understate them.
4. **Read `dim_scheme.money_unit` before formatting any figure.** The fact's comment does not
   state a unit, unlike PMAY's.
5. **`COUNT(*)` is payments.** Say so, every time.
6. **Fold case on `product_raw`; uppercase district and block literals.**
7. **Never present an amount finding and a membership finding as independent evidence.**

### 5.4 Three judgement calls, recorded so they can be reversed

1. **Constituency is treated as answerable, with caveats.** `v_focus_legacy` exposes
   `geography_key`, so joining `dim_geography` for `ac_name` is a dimension join on a declared FK
   — not a fact-to-fact join and not prohibited. Focus Plus refuses the equivalent question, but
   its view does not expose the key. Two caveats are mandatory: the AC comes from the **roster**,
   not the scheme's own dropped column; and `Unresolved` rows must be excluded, so a constituency
   total will not reconcile to the scheme total.
2. **Unknown enumerations are marked `UNVERIFIED_ENUMERATE_FIRST` rather than guessed.** This
   applies to `pg_entity_type.type_code`, `programme_variant`'s stored form, and the Focus Legacy
   `scheme_code`. The resolver will not filter on a guessed `'FOCUS'`.
3. **The money unit is `unverified`**, following the Focus+ precedent. ₹5,000-per-member
   magnitudes corroborate rupees; nothing in the extract confirms it.

---

## 6. Anatomy of the two gate files

Both are built. They are **companions, not duplicates**, and the split is the one the sibling
folders use: the gate decides what must be **asked**, the defaults decide what may be **filled
silently and stated back**. A condition must live in exactly one of them — the gate runs first, so
anything present in both would always ask and never default. The assertion that proves they are
disjoint is in §6.5.

### 6.1 `focuslegacy_classification_rules.yaml` — 124 rules

430 lines, v1.0, written 2026-09-21. The format is PMAY's exactly: a flat `clarification_rules`
list, `condition` + `question` and **nothing else**, `# ---- section ----` dividers,
`{placeholders}` filled from the user's query at runtime, first rule that fires wins and the
pipeline pauses. No `v2_note` anywhere — there is no migration to record, so commentary lives in
the header block and the reasoning lives in stage2.

For scale: PMAY has 90 rules, Focus+ 89, CM Elevate 148. Focus Legacy's 124 sit between them, and
the extra weight is in the four sections the siblings do not have.

**The thirteen sections:**

| Rules | Section | What it catches |
|---:|---|---|
| 31 | the source cannot answer this at all | No person, no status, no outcome, no budget, no codes. Also the **Focus Plus / Focus Legacy name collision** — two schemes share the word "Focus" and only this one pays groups |
| 6 | what privacy forbids | Account holder name, full and bulk account numbers, account-change history. Refused on privacy grounds even where the column exists |
| 7 | **the entitlement identity** | No PMAY counterpart. Every question that is really a membership question wearing a rupee sign |
| 10 | terms that look answerable but mean several things | Payments vs groups vs memberships — three numbers, no person reading |
| 5 | money | The unconfirmed unit, conversions, cross-unit arithmetic |
| 13 | time | The FY2023-24 gap, the 30-date batch structure, calendar-vs-financial year, the 171 dateless rows |
| 10 | **producer groups and the `pg_id`** | No PMAY counterpart. Unstable names, the stale district token, the non-unique numeric suffix |
| 5 | **product** | No PMAY counterpart. The two-year coverage cliff and the 62-spelling problem |
| 4 | **banking** | No PMAY counterpart. Bank-as-performance, IFSC-as-branch, the inferred "current" flag |
| 13 | geography | `Unresolved` placeholders, district bifurcation, name collisions, the constituency offer |
| 6 | data quality | The three quarantined rows, the 190-member outlier, unverified totals |
| 5 | cross-scheme | Focus Legacy is in neither cross-scheme view |
| 9 | shape of the answer | Level, denominator, direction, grid size, columns |

### 6.2 The four gate sections with no PMAY counterpart

1. **The entitlement identity (7 rules).** `amount = members × 5000` means "average amount per
   group", "amount per member", "has the entitlement changed", "correlate amount with membership"
   and "rank by both" are all questions the data cannot answer the way the user expects. Each one
   asks before producing a number that would read as a finding about policy when it is a finding
   about group size.
2. **Producer groups and the `pg_id` (10 rules).** The id looks parseable and is not: the district
   letters are stale on 1,075 payments, and the number is unique only within its prefix. Three
   rules exist purely to stop the bot counting groups by name.
3. **Product (5 rules).** The field is populated for two of five years. Without a gate, "what do
   groups produce in 2025-26" answers confidently from eight rows.
4. **Banking (4 rules).** Focus+ refuses bank questions outright because its view withholds the
   column. Here the data is clean and bank analysis is legitimate — so the rules are about
   *reading* it correctly, not about refusing it.

### 6.3 `focuslegacy_default_rules.yaml` — 81 rules

390 lines, v1.0, written 2026-09-21. Format matched to PMAY: a flat `default_rules` list,
`condition` + `default_value` + `assumption_text`, with `sql_effect` on the seven rules that
change the emitted SQL rather than only the wording. No `v2_note`. Every fill hands the response
composer an assumption line, so the user can correct it without being interrupted first.

For scale: PMAY has 63 rules, Focus+ 67. Focus Legacy's 81 carry two extra sections.

| Rules | Section | What it fills |
|---:|---|---|
| 5 | data quality — no always-on filter, unlike PMAY | `Unresolved` and conflicted rows stay in; the three quarantined rows are explained; unverified totals get a caveat |
| 4 | **the entitlement identity** | No PMAY counterpart. A bare "amount" sums `amount_disbursed` — and the ×5000 relationship is stated back with it |
| 15 | time | All four years by default, the FY2023-24 gap shown as a gap, "last year" skipping it, the 171 dateless rows excluded and stated |
| 11 | geography | All 12 districts, district-level breakdown, **uppercase** LGD literals, bifurcated districts kept apart, village counts excluding `Unresolved` |
| 7 | **producer groups** | No PMAY counterpart. Identity is `pg_id`, groups counted once, names shown with the instability caveat |
| 9 | which measure | Bare count → payments, product folded and period-scoped, branch questions routed to IFSC |
| 6 | which table | The view, never the fact; cross-scheme handled by aggregating each scheme separately |
| 4 | units | Read `dim_scheme.money_unit` and state it; Indian digit grouping |
| 13 | shape of the answer | Top 5, first 10 rows, 100-row listings, masked accounts, table output |
| 7 | nulls and zeros | What each kind of missing means — and that money and members are never NULL |

### 6.4 The four defaults that depart from PMAY

1. **No always-on predicate.** PMAY opens every count and sum with `WHERE NOT is_placeholder`.
   That column does not exist here, and copying the habit produces a query that errors. The only
   conditional filter is the `Unresolved` exclusion, and it applies to **village answers only**.
2. **The money unit is not assumed.** PMAY defaults to crore. Here the unit is unconfirmed, so the
   default reads `curated.dim_scheme.money_unit` and states whatever it says. When that cannot
   answer, the gate asks — that condition is reserved, not defaulted.
3. **"Last year" skips a year.** `comparison_without_second_period` resolves to the preceding year
   that *holds payments*, so a comparison against FY2024-25 lands on FY2022-23, not on an empty
   FY2023-24.
4. **Every money default states the ×5000 identity back.** `amount_answer_without_membership_context`
   exists so a rupee total is never presented as a finding about policy when it is a finding about
   how many memberships were paid for.

### 6.5 How the two files divide, and the assertion that proves it

The gate carries 124 conditions, the defaults 81, and **the intersection is empty** — checked with
the sibling folders' assertion (§12, step 3):

```
c & d  ==  set()      # 124 gate conditions, 81 default conditions, 0 shared
```

The division is not arbitrary. A condition is **asked** when getting it wrong would change what
the number *means* — which reading of "beneficiaries", whether a product figure covers two years
or five, whether a district came from the registry or from a stale id. It is **defaulted** when
getting it wrong would only change the presentation — top 5 versus top 10, sort direction, chart
type — or when there is exactly one defensible reading that can be stated back in a sentence.

Eight conditions were reserved for the defaults file in the gate's header before that file
existed. All eight landed there, and none leaked back into the gate.

### 6.6 The gap neither file can close

There is no use-case workbook for Focus Legacy (§2.2), so **no rule in either file is evidenced by
a stated SME requirement.** Every condition is derived from a trap in the data or from the
conventions of the sibling folders. Both headers say so. Two consequences:

- The *conditions* are sound — they come from verified data findings — but the *phrasing* of the
  questions and the assumption lines is a guess at how users talk about this scheme.
- Rules the data cannot suggest are certainly missing, and the ask-versus-default line may be drawn
  in the wrong place for some of them. The first real user session should be treated as the
  evidence these files were written without, and both corrected against it.

---

## 7. Anatomy of `focuslegacy_entity_resolver.yaml`

1,481 lines, v1.0, written 2026-09-21. Same top-level shape as PMAY's resolver, in the same order:
`scheme` → `tables` → `dataset_shape` → `how_to_use` → `normalisation` → `matching_pipeline` →
`blocked_matches` → `output_contract` → `overloaded_terms` → `cross_dimension_collisions` →
`dimensions` → `measure_vocabulary` → `region_groupings` → `worked_examples` → `maintenance`. The
eight-stage pipeline (exact → alias → squash → acronym → contains → fuzzy → embedding → fail) and
its accept/ask/reject thresholds are carried over unchanged.

For scale: PMAY has 1,836 lines, Focus+ 1,610, CM Elevate 2,200.

### 7.1 The fifteen dimensions

| Dimension | Catalogue | Notes |
|---|---|---|
| `district` | **12**, complete | Acronyms, regions, aliases shared with the PMAY resolver |
| `block` | **56**, all with district + LGD code | 17 of them are also village names |
| `village` | 3,384 codes / 3,270 names | Ambiguity registry, not a full list — as PMAY does |
| `producer_group` | identity rules | `pg_id` is the only identity; names split and merge groups |
| `pg_entity_type` | 3 prefixes | Stored codes **unknown** — enumerate first |
| `programme_variant` | 2 | `Focus (Addnl)` is one day, not an era |
| `product` | **45 canonical / 62 raw** | Every spelling mapped; folding is the resolver's job |
| `bank` | **13** + nulls | Usable here, unlike Focus+ |
| `ifsc` | 113 | The sponsor-code trap and the O-vs-0 confusion |
| `branch` | 182, 22% coverage | Routed to IFSC instead |
| `financial_year` | 4, closed set | With the FY2023-24 gap rule |
| `calendar_year` | 2021–2026 | Calendar 2023 is empty, matching the gap |
| `year_disambiguation` | — | Calendar 2022 = 2,804 vs FY2022-23 = 2,139 |
| `remittance_date` | **all 30 batch dates** | With payment and amount per batch |
| `identifiers` | 8 | Two marked `OUT_OF_SCOPE` |

### 7.2 Five things this resolver does that PMAY's does not

1. **It resolves the scheme before anything else.** Two live schemes answer to "Focus", and the
   word is also a `programme_variant` value inside this one. `scheme.disambiguation` settles which
   partition the question belongs to before a single value is looked up.
2. **It forbids parsing the `pg_id`.** The id embeds a district abbreviation that is stale on
   1,075 payments. `producer_group.district_token_trap` records the evidence and the rule; the
   district acronym table carries a matching warning so "WGH" typed by a *user* still resolves.
3. **It owns product normalisation.** The database stores `product_raw` verbatim, so the 62 → 45
   folding has to happen here. Every raw spelling is listed against its canonical, with aliases for
   the words users actually say — *pig*, *kwai*, *supari*, *tejpatta*, *paan*.
4. **It has a fourth output shape, `refused`.** Focus+ introduced this for PII; here it covers
   `account_no` and `name_on_the_account`, both marked `OUT_OF_SCOPE` in `identifiers` so the
   resolver can never return them even by accident.
5. **It records dirt it must not clean.** 19 IFSC codes carry more than one bank name — SBIN0RRMEGB
   carries seven. The rule is explicit: do **not** use one column to correct the other, and do not
   report the cross-tab as a finding about banking.

### 7.3 Every catalogue total reconciles

The catalogues were checked back against the workbook rather than transcribed:

| Catalogue | Sums to | Expected |
|---|---:|---:|
| 12 districts | 14,569 | 14,569 payments |
| 56 blocks | 14,481 | 14,569 − 88 with no block |
| 45 products | 3,112 | the product-bearing rows |
| 13 banks | 14,293 | 14,569 − 276 nulls |
| 4 financial years | 14,569 / ₹51,01,05,000 | the scheme totals |
| 30 batch dates | 14,398 | 14,569 − 171 dateless |
| 4 regions | 14,569 | the scheme total |
| 2 programme variants | 14,569 | the scheme total |

### 7.4 What it refuses, and what it only warns about

**Refused outright** — `account_no`, `name_on_the_account`. The resolver emits `refused`, not
`not_found`: the ground is privacy, not absence.

**Resolved but warned** — `account_no_masked` (displayable, never countable: masking is not
collision-free), `ifsc_code` (a branch id, reportable, but not a branch *count*), `bank_name`
(clean and usable — the opposite of the Focus+ ruling, and the file says why).

**Resolved but reframed** — every money term. `overloaded_terms.amount` and
`measure_vocabulary.amount_disbursed` both carry the ×5000 identity, so a rupee answer never
escapes without it.

### 7.5 What it cannot know yet

Three catalogues are marked `UNVERIFIED_ENUMERATE_FIRST` rather than guessed: the three
`dim_pg_entity_type.type_code` strings, the stored form of `programme_variant`, and this scheme's
`dim_scheme.scheme_code` and `money_unit`. Each blocks a filter the resolver would otherwise
emit, and each is one query away — see `maintenance.open_verification_tasks`, carried into §11.

---

## 8. Anatomy of `focuslegacy_few_shot.yaml`

1,063 lines, **81 question-to-SQL pairs**, v1.0, written 2026-09-21. Format matched to PMAY: a flat
`sql_generation_examples` list, `question` + `sql` + `tables`, with an optional `note` where the
pattern needs explaining. No `status: RETIRED` entries — there is no flat-table ancestor and so no
stale v1 SQL to keep as a negative example.

For scale: PMAY has 65 examples (8 of them retired), Focus+ 70.

**The refusals are not here.** The 11 questions that must be declined live in stage2 with
`sql: null`, exactly as PMAY and Focus+ arrange it. This file holds only SQL that may be emitted.

### 8.1 The fourteen sections

| Examples | Section | What it teaches |
|---:|---|---|
| 6 | the three counting subjects | payments vs groups vs memberships, and the default shape that shows all three |
| 7 | money, and the ×5000 identity | the membership column always travels with the money column |
| 11 | geography | uppercase literals, the `Unresolved` filter, regions as IN lists, bifurcated districts |
| 6 | time: financial year | four rows not five, and "the year before" skipping the gap |
| 7 | time: dates and batches | 30 payment dates, the month view labelled as batches, the dateless rows |
| 7 | producer groups | `pg_id` grouping with `MAX(pg_name)` for display, repeat payments, size changes |
| 5 | programme variant and group type | enumerate-first, and `Focus (Addnl)` carried with its year |
| 6 | product | `UPPER(TRIM(...))` everywhere, plus the coverage query that frames every product answer |
| 6 | banking | `COALESCE` for the 276 nulls, the IFSC-is-not-a-branch column, `IS NULL` for old accounts |
| 6 | rankings and grids | four year columns, not five |
| 6 | data quality | reconciliation, quarantine, field completeness, the not-really-duplicates |
| 3 | documented dimension joins | constituency via `dim_geography`, coverage via `bridge_geography_source` |
| 2 | cross-scheme | aggregate each side, then join the aggregates — never the facts |
| 3 | listings | capped, with the block-vs-village default stated |

### 8.2 The house rules, and the check that they hold

The header lists 16 rules every example obeys. They were **verified mechanically**, not by eye:

| Check | Result |
|---|---|
| Examples touching `fact_focus_legacy_disbursement` or `bridge_pg_bank_history` | **0** |
| Examples using `is_placeholder` outside a PMAY subquery | **0** |
| Examples selecting unmasked `account_no` | **0** |
| Examples touching `name_on_the_account` | **0** |
| Examples counting `DISTINCT pg_name` | **0** |
| District/block literals that are not UPPERCASE | **0** |
| `COUNT(DISTINCT village_code)` without the `Unresolved` filter | **0** |
| `product_raw` grouped without `UPPER(TRIM(...))` | **0** |
| Duplicate questions | **0** |
| Examples missing `sql` or `tables` | **0** |

The one place `is_placeholder` appears is inside the PMAY side of the cross-scheme CTE — where it
is required. That contrast is the point: the example carries both schemes' predicates correctly and
the note explains why they differ.

### 8.3 The patterns worth copying

- **`SUM(amount_disbursed)` never travels alone.** Every money example carries
  `SUM(no_of_pg_members)` beside it, so the ×5000 relationship is visible in the result rather than
  buried in prose.
- **`GROUP BY pg_id ... MAX(pg_name) AS pg_name`.** Groups are keyed on the id and the name is
  picked for display — the only safe way to show a name for a group that has two spellings.
- **`COUNT(DISTINCT village_code) FILTER (WHERE entity_type <> 'Unresolved')`.** The exclusion sits
  *inside* the village aggregate, so payment and group columns in the same row stay complete.
- **`FILTER (WHERE financial_year_short = ...)` for grids**, with four year columns and no zero
  column for FY2023-24.
- **The cross-scheme CTE.** Both sides aggregated independently, joined on the shared key, with a
  note that a remitted rupee and a released rupee are different measures.

### 8.4 What is not verified

**Not one of these queries has been executed against `megh_db`.** They are correct against the
2026-09-21 structure and unverified against the data. Every figure quoted in a `note` is a
source-workbook observation. Four examples depend on enumerations this folder does not yet have —
`programme_variant = 'Focus (Addnl)'` assumes the stored spelling, and the entity-type examples
enumerate rather than filter for exactly that reason. Three examples reach outside `curated`
(`staging.quarantine`, `meta.ingestion_audit`, `meta.v_reconciliation_focus_legacy`) and may need a
role other than `megh_readonly`.

---

## 9. Anatomy of `focuslegacy_foreign_key_augmentation.yaml`

1,103 lines, v1.0, written 2026-09-21. Same shape as PMAY's — `nodes` → `edges` → `join_graph` —
plus the three sections the newer siblings added: `absent_edges`, `sanctioned_patterns` and
`prohibited_fact_pairs`. No `retired_edges`: there is no flat-table ancestor and so no
embedded-dimension fiction to retire.

**21 nodes, 28 edges** — 11 declared, 16 prohibited, 1 performed by the view.

### 9.1 This is the only snowflake in `megh_db`

PMAY, Focus Plus and CM Elevate are flat stars: the fact joins a dimension and the dimension is
terminal. Focus Legacy is not. **`dim_producer_group` has four outbound foreign keys of its own** —
to `dim_pg_entity_type`, to `dim_geography`, and to `dim_year` *twice*, for the first and last
payment year.

Three consequences the file spells out:

- **A generator that learned "dimensions are terminal" from the PMAY graph will be wrong here.**
- **`dim_year` is reached three times** — once from the fact, twice from the group. Any query
  touching more than one needs an explicit alias.
- **There is a diamond.** The fact reaches `dim_geography` directly *and* through
  `dim_producer_group`. The two paths agree today — geography is stable per group, zero conflicts
  in the source — but traversing both produces a self-join on the dimension. The view uses the
  fact's path; so should everything else.

### 9.2 Eleven declared foreign keys, and all eleven verified

PMAY has five, Focus Plus has five *inferred* ones because its extract carried no constraint data.
Focus Legacy has eleven **real** constraints, and each is cited in the file by its constraint name.
Checked mechanically against the 2026-09-21 extract:

| Check | Result |
|---|---|
| Constraints cited here that are absent from the live schema doc | **0** |
| Focus-Legacy-related constraints in the doc not cited here | **0** |
| Declared edges missing an `on_clause` | **0** |
| Prohibited edges missing `use_instead` or a reason | **0** |
| Tables referenced in an edge but not declared as a node | **0** |
| Nodes missing from the `join_graph` | **0** |

Five sit on the fact, five on `dim_producer_group`, one on `bridge_pg_bank_history`.

### 9.3 The edge with a predicate inside it

The view's join to `bridge_pg_bank_history` is the only edge in `megh_db` that carries a predicate
in its `ON` clause:

```sql
LEFT JOIN curated.bridge_pg_bank_history bh
       ON bh.producer_group_key = f.producer_group_key
      AND bh.account_no = f.account_no
      AND bh.is_current
```

That third condition is *why* `bank_details_current` is TRUE-or-NULL and never FALSE. Reproducing
the join without it would produce FALSE rows and change every "paid to an old account" answer. It
also reads the **unmasked** `account_no` on both sides — one more reason the fact is out of bounds.

### 9.4 Two nodes are a privacy boundary, not a preference

`fact_focus_legacy_disbursement` and `bridge_pg_bank_history` both hold unmasked account numbers,
and the fact also holds `name_on_the_account`. Both are marked `never_traverse: true`, and the
first two prohibited edges are blanket bans — *any* table joining to either.

This is stricter than PMAY, where reaching for the fact is merely a missed optimisation. Here it is
the same breach as querying it directly, and the file says so: **there is no column on the fact
worth the breach.**

### 9.5 Sixteen prohibitions, and what they protect

Five are the fact pairs — every Focus Legacy × other-scheme combination, all refused, all with a
grain and a unit mismatch recorded. The one worth naming is **Focus Legacy × Focus Plus**: the most
tempting prohibited edge in the database, because the two schemes share a name and share *nothing
else*. Focus Plus holds no producer-group column at all, so there is no key to join on and a name
match between a person and a group is not a match.

The rest guard against self-joins that fan out (`pg_id` squares the 2,655 multi-payment groups;
`account_no_masked` creates false matches because masking is not collision-free; `pg_name` both
merges and misses groups), the alias table that Focus Legacy has no key into, and re-joining
`dim_producer_group` to a view that already contains it.

### 9.6 Six sanctioned patterns, written out in full

`constituency` (the documented `dim_geography` re-join), `money_unit` (read `dim_scheme` alone),
`group_history` (query `dim_producer_group` alone for the first/last payment year),
`entity_types_including_zero` (the one LEFT JOIN with the dimension as the driving table),
`cross_scheme_coverage` and `cross_scheme_money` (aggregate each side, then join the aggregates).

Each carries its SQL and its rules. Two rules are worth lifting out:

- `COUNT(f.focus_legacy_fact_id)`, never `COUNT(*)`, in the LEFT-JOIN pattern — `COUNT(*)` returns
  1 for an empty type because the join produces one all-NULL row.
- The scope predicate belongs **in the `ON` clause**, not a `WHERE` — a `WHERE` would filter away
  the all-NULL rows and defeat the pattern.

### 9.7 Eight absences a generator will trip over

No status dimension. No `alias_key`. No `is_placeholder`, `mapping_category` or
`mapping_confidence_pct` — emitting PMAY's `WHERE NOT is_placeholder` does not mislead, it
**errors**. No constituency column on the fact. No monthly pre-aggregate. No Focus Legacy row in
either cross-scheme view. And no key of any kind between Focus Legacy and Focus Plus — the absence
most likely to be papered over with a name match.

---

## 10. Anatomy of `focuslegacy_response_template.yaml`

385 lines, v1.0, written 2026-09-21. Same shape as PMAY's — `formatting` → `templates` →
`follow_up_rules` — plus the two sections CM Elevate added: a first-class block of **refusal
templates** inside `templates`, and **`composer_checks`**, assertions the composer runs against its
own draft before sending.

**26 formatting keys · 101 templates (12 of them refusals) · 45 follow-up rules · 16 composer
checks.**

### 10.1 The seven caveats, and how they are enforced twice

The header names seven caveats that must travel with the answers they belong to. Each is not
decoration — each is a way this partition produces a *true number that reads as a false claim*.
Every one has both a **template** that states it and a **`composer_check`** that fails the draft if
it is missing:

| Caveat | Template | Check |
|---|---|---|
| The unit is unconfirmed | `unit_unconfirmed`, `unit_statement` | no bare ₹ unless `dim_scheme` was read |
| Money is membership | `rate_note`, `amount_is_membership_note`, `average_amount_note` | no amount stands alone; no average described as an entitlement |
| Payments ≠ groups ≠ people | `payment_not_group_note`, `membership_not_people_note` | every count names its subject; "people" never used for memberships |
| FY2023-24 is a gap | `fy_gap_note`, `fy_gap_comparison_note` | the gap is named, never shown as zero |
| Dates are batches | `batch_note`, `single_batch_note` | any monthly view says so |
| Product covers two years | `product_coverage_note`, `product_absent_note` | the FY range is named |
| Village answers exclude placeholders | `unresolved_excluded`, `unresolved_included` | the shortfall is stated |

### 10.2 The formatting block is mostly restraint

Three keys exist to stop the composer asserting something it does not know:

- **`currency_symbol_rule`** — do not print ₹ until `dim_scheme.money_unit` has answered. *A ₹ sign
  is itself a claim about the unit.* PMAY can default to crore; this partition cannot default at
  all.
- **`no_mandatory_predicate_note`** — never write "excluding placeholder records". That is PMAY's
  sentence, and there is no such column here. It has a `composer_check` of its own.
- **`rate_statement_always: true`** — the ₹5,000-per-member rule accompanies every money answer.

Casing is per-column rather than global, because this partition stores three conventions at once:
district and block **title-cased for display but filtered uppercase**, village names and bank names
**passed through as stored**, product **folded for grouping and title-cased for display**.

### 10.3 Twelve refusals, worded as redirects

PMAY handles absence with `unavailable_in_source`. Focus Legacy needs more, because more is absent
and two refusals are on **privacy** grounds rather than availability — the account number and the
account-holder name. Each refusal names the nearest real question rather than stopping:

> *"Focus Legacy records payments to producer groups, so there is no count of individuals. I can
> give you payments, producer groups, or memberships paid for — which did you mean?"*

Three of the twelve have a matching `follow_up_rule` that fires straight after, so a refusal ends
in an offer rather than a dead end.

### 10.4 Follow-ups that chase the caveat, not just the hierarchy

PMAY's follow-ups move up and down geography, across time and across measures. Focus Legacy keeps
all three and adds a fourth axis — **follow the caveat that was just stated**. Seven rules do this:
having just told the user that village figures exclude the unmatched payments, the natural next
offer is to show them; having just said money moves in batches, offer the size of each run.

There is also a block for moving between the **three counting subjects**, which no sibling needs:
payments → groups → memberships → amount, each offering the next.

### 10.5 What the composer checks catch

Sixteen assertions. The ones that matter most are the ones a fluent draft would otherwise pass:

- *"No average amount is described as an entitlement, a benefit level or a change in policy."*
- *"The words 'people', 'individuals' or 'persons' are not used for a membership figure."*
- *"If a district was reported for a named group, it came from the geography registry and not from
  the letters in the PG id."*
- *"The phrase 'excluding placeholder records' does not appear."*

---

## 11. Open issues

Ordered by risk. None can be closed from this folder — every one needs a database session.

1. **Read `curated.dim_scheme.money_unit` for the Focus Legacy row.** No money figure ships until
   this is done. Also capture the `scheme_code`, which the extract does not carry.
2. **Enumerate `curated.dim_pg_entity_type`** — `type_code`, `type_name`, `source_id_prefix`. Three
   rows. Blocks the entity resolver and any entity-type filter or chart legend.
3. **Enumerate `programme_variant`.** Confirm `'FOCUS'` and `'Focus (Addnl)'` survived the load
   verbatim into `varchar(20)`.
4. **Run `meta.v_access_matrix`.** These objects were created after the last access review. Confirm
   `megh_readonly` can read `v_focus_legacy` — and confirm it **cannot** read
   `fact_focus_legacy_disbursement` or `bridge_pg_bank_history`.
5. **Run `meta.v_reconciliation_focus_legacy`.** Expect `14,569 = 14,566 + 3`. No total is quotable
   until it agrees.
6. **Establish what an `Unresolved` placeholder stores in `lgd_block`.** If it is NULL, block
   totals lose 1,013 rows on top of the 88.
7. **Confirm `year_key` is never NULL.** The DDL allows it; the source carries a financial year on
   every row, so a NULL is a load defect worth reporting.
8. **Check whether `first_payment_year_key` / `last_payment_year_key` were populated.** Both are
   nullable and unverified; they are the cheap route to "groups paid in more than one year".
9. **Check whether `bridge_geography_source` has `focus_legacy` rows at all.** `SELECT DISTINCT
   source_system` before relying on it.
10. **Confirm the embedded-space account numbers** (78 rows) were normalised at ingest, and whether
    the masked form is one character longer than expected.
11. **Raise the two malformed `pg_id`s with the data owner.** Stray spaces, trivially recoverable —
    two groups are missing from the warehouse for no good reason.
12. **`MASTER/schemes_catalog.yaml` has no Focus Legacy entry.** Node 1 cannot route a Focus Legacy
    question yet. Stage2 now exists, so the keywords can be derived from it.
13. **`semantic.refresh_catalog()` has never seen this fact.** When it runs, `account_no` must be
    kept out of `sample_values` and `name_on_the_account` must stay at
    `is_chatbot_visible = false`.

---

## 12. Regeneration checklist

Run in this order. Steps 1–2 are mechanical and should be automated before the next load.

1. **Re-profile the workbook** with the §3.1 recipe. Every count in every YAML comes from it.
2. **Re-run the entitlement identity test (step 2).** If `amount = members × 5000` ever stops
   holding, that break is the headline finding and half of `semantic_rules` needs rewriting.
3. **Validate both gate files parse and do not overlap** — currently passing, 124 against 81:
   ```python
   import yaml
   c = {x['condition'] for x in yaml.safe_load(open('focuslegacy_classification_rules.yaml'))['clarification_rules']}
   d = {x['condition'] for x in yaml.safe_load(open('focuslegacy_default_rules.yaml'))['default_rules']}
   assert not (c & d), sorted(c & d)      # MUST be empty
   ```
4. **Re-check the reconciliation arithmetic.** `source rows − quarantined = curated rows` must
   still be exact. If the quarantine count changes, find out which rows and why.
5. **Check stage2's column list against the live view** — currently 28 columns on
   `v_focus_legacy`.
6. **Confirm no PII column carries `sample_values`**, and that neither the fact nor the bank
   bridge appears in `default_from` or any example.
7. **Validate the YAML parses:**
   ```python
   import yaml
   d = yaml.safe_load(open('focuslegacy_schema_partitions.yaml', encoding='utf-8'))
   assert len(d['datasets']['v_focus_legacy']['columns']) == d['datasets']['v_focus_legacy']['column_count']
   ```
8. **Check every new default has a template that states it back** (§10.1), and every new caveat
   has both a template and a `composer_check`.
9. **Re-diff the declared edges against the live extract** (§9.2). A new constraint is a new node
   in the join graph; a constraint that disappears is a prohibition that must not disappear with it.
10. **Re-run the few-shot house-rule checks** (§8.2). Every one must return zero, and no example
   may reference the fact table or the bank bridge.
11. **Re-derive every resolver catalogue total** and check it reconciles as §7.3 does — districts
   and regions to 14,569, blocks to 14,481, products to 3,112, banks to 14,293, batch dates to
   14,398. A catalogue that stops summing correctly has drifted from the source.
12. **Re-read `SCHEMA_FOR_DEVELOPERS.md`.** These files are pinned to the **2026-09-21** extract.
13. **Run `meta.v_reconciliation_focus_legacy`** before any figure is published.
14. **Append, never replace.** Old questions must keep resolving.

---

## 13. House rules

1. **A fact about a column belongs in stage2; a fact about a value belongs in the resolver; a fact
   about asking-versus-assuming belongs in exactly one gate file.**
2. **Every number in this folder is a source-workbook observation until measured against the
   database.** Mark it `unverified_in_db` and say so in the answer. The two exceptions are the
   reconciliation arithmetic and the ×5000 identity, both of which the database independently
   confirms.
3. **PII is not negotiable.** `account_no` and `name_on_the_account` never reach a user, an export
   or a vector store. `account_no_masked` is displayable for one group's history, never in bulk.
4. **Never copy a PMAY predicate across.** `is_placeholder`, `is_completed`, `mapping_category`,
   `sanction_date` and `installments_paid` do not exist here.
5. **Never present a 21%-coverage product breakdown as a scheme figure.**
6. **`COUNT(*)` is payments; `COUNT(DISTINCT pg_id)` is groups; `SUM(no_of_pg_members)` is
   memberships.** Three numbers, never interchangeable.
7. **Never derive geography, or anything else, from the `pg_id` string.**
8. **Query the view, never the fact or the bridge.**
9. **Match the sibling file format exactly.** Same top-level keys, same record fields, same
   `# ---- section ----` dividers, comparable comment density. A v1.0 file carries no `v2_note`.
10. **No YAML is written until told which and how.** All seven are now built, each on its own
    instruction. Nothing further is authored without one.

---

## 14. Change log

| Date | Change |
|---|---|
| 2026-09-16 | Focus Legacy loaded into `megh_db`. The ×5000 entitlement identity verified against the source file on this date, per the `fact_focus_legacy_disbursement` table comment. |
| 2026-09-21 | **Folder `Annotations/Focus_Legacy/` created** with seven empty (0-byte) YAML placeholders, named on the PMAY / Focus+ / CM Elevate convention (scheme prefix on every file). Folder named `Focus_Legacy` to match the `Focus+` / `CMElevate` style. |
| 2026-09-21 | **`SCHEMA_FOR_DEVELOPERS.md` replaced at the repository root.** The 2026-09-04 extract (1,559 lines, 38 objects, 4 schemes) was superseded by the **2026-09-21 06:02 UTC** extract (1,804 lines, **44 objects, 5 schemes**). The new file is the first to document Focus Legacy at all: `fact_focus_legacy_disbursement`, `dim_producer_group`, `dim_pg_entity_type`, `bridge_pg_bank_history`, `v_focus_legacy` and `meta.v_reconciliation_focus_legacy`. Note the new file is auto-generated (`docs/generate_schema_doc.py`) and says so — **do not hand-edit it**, re-run the script after a migration. A copy of the old version was kept only in the session scratchpad, which is **not durable**; recover it from version control if it is ever needed. |
| 2026-09-21 | Source workbook profiled — `Focus Legacy to share to BLH.xlsx`, Sheet1, 14,569 rows × 22 columns. The ×5000 identity, the exact 3-row reconciliation, the FY2023-24 gap, the 30-date batch structure, the stale `pg_id` district token, the product/branch coverage cliff and the account-sharing pattern were all found and recorded. |
| 2026-09-21 | `focuslegacy_schema_partitions.yaml` **v1.0 written** — 1,796 lines. 11 dataset blocks, all 28 view columns annotated, 30 semantic rules, 22 NLP-SQL rules, 37 few-shot examples of which 11 are refusals. Validates under `yaml.safe_load`. Three judgement calls recorded in §5.4. |
| 2026-09-21 | This README added. |
| 2026-09-21 | `focuslegacy_classification_rules.yaml` **v1.0 written** — 430 lines, **124 rules** across 13 sections. Format matched to PMAY exactly: `condition` + `question` only, `# ---- section ----` dividers, no per-rule commentary, no `v2_note`. Four sections have no PMAY counterpart — the entitlement identity, the `pg_id`, product coverage and banking. The eight conditions reserved for the unwritten defaults file are named in the header so the two files stay disjoint. Validates under `yaml.safe_load`; zero duplicate conditions; no rule carries a key beyond `condition` and `question`. |
| 2026-09-21 | README updated for the gate file — status banner, file table, pipeline diagram, new §6 anatomy, §2.2 reworded, sections 6–10 renumbered to 7–11, backlog reduced to five files. |
| 2026-09-21 | `focuslegacy_default_rules.yaml` **v1.0 written** — 390 lines, **81 rules** across 10 sections, 7 carrying `sql_effect`. Format matched to PMAY: `condition` + `default_value` + `assumption_text`, no `v2_note`. Two sections have no PMAY counterpart — the entitlement identity and producer groups. Four deliberate departures from PMAY are recorded in §6.4, the load-bearing one being that **no always-on predicate exists**. All eight conditions the gate reserved landed here. **Overlap assertion against the gate passes: 124 ∩ 81 = ∅.** |
| 2026-09-21 | README updated for the defaults file — §6 restructured into "Anatomy of the two gate files" with six subsections, regeneration step 3 now records the passing assertion, backlog reduced to four files. |
| 2026-09-21 | `focuslegacy_entity_resolver.yaml` **v1.0 written** — 1,481 lines, **15 dimensions**, same top-level shape and eight-stage matching pipeline as PMAY's. Catalogues: 12 districts, 56 blocks, 45 products against 62 raw spellings, 13 banks, 4 financial years, all 30 batch dates, a 94-name village ambiguity registry and 26 blocked fuzzy pairs. Adds a fourth output shape, `refused`, for the two PII columns. **Every catalogue total was re-derived from the workbook and reconciles exactly** — see §7.3. Three catalogues left as `UNVERIFIED_ENUMERATE_FIRST` rather than guessed. |
| 2026-09-21 | README updated for the resolver — new §7 anatomy, sections 7–11 renumbered to 8–12, backlog reduced to three files. |
| 2026-09-21 | `focuslegacy_few_shot.yaml` **v1.0 written** — 1,063 lines, **81 question-to-SQL pairs** across 14 sections. Format matched to PMAY: `question` + `sql` + `tables` with an optional `note`, and no retired entries. The 11 refusals stay in stage2, as the siblings arrange it. **All 10 house-rule checks pass mechanically** — see §8.2 — including zero references to the fact table or the bank bridge, zero unmasked account selections and zero unfolded product groupings. |
| 2026-09-21 | README updated for the few-shot set — new §8 anatomy, sections 8–12 renumbered to 9–13, regeneration checklist gained the house-rule re-check, backlog reduced to two files. |
| 2026-09-21 | `focuslegacy_foreign_key_augmentation.yaml` **v1.0 written** — 1,103 lines, **21 nodes / 28 edges**: 11 declared FKs, 16 prohibited edges, 1 performed by the view, plus 8 `absent_edges`, 6 `sanctioned_patterns` and all 5 `prohibited_fact_pairs`. Records the finding that **this is the only snowflake in `megh_db`** — `dim_producer_group` carries four outbound FKs, `dim_year` is reached three times, and there is a geography diamond. **All 11 declared constraints cross-check exactly against the live extract, in both directions** — see §9.2. Both PII-bearing nodes are marked `never_traverse`. |
| 2026-09-21 | README updated for the join graph — new §9 anatomy, sections 9–13 renumbered to 10–14, regeneration checklist gained the declared-edge diff, backlog reduced to one file. |
| 2026-09-21 | `focuslegacy_response_template.yaml` **v1.0 written** — 385 lines: 26 formatting keys, **101 templates** of which 12 are refusals, 45 follow-up rules and 16 `composer_checks`. Format matched to PMAY, plus CM Elevate's two additions. Encodes the **seven mandatory caveats twice over** — a template that states each one and a check that fails the draft without it. **All seven YAMLs now built and parsing clean: 6,649 lines.** |
| 2026-09-21 | README updated for the response composer — new §10 anatomy, sections 10–14 renumbered to 11–15, regeneration checklist gained the template/caveat pairing check, backlog closed. |

---

## 15. Backlog — what remains

**All seven YAMLs are built.** Nothing further is committed work; per standing instruction, no
YAML is written until told which and how. What remains is **verification, not authoring** — and
none of it can be done from this folder.

| File | Lines | Built |
|---|---:|---|
| `focuslegacy_schema_partitions.yaml` | 1,796 | 2026-09-21 |
| `focuslegacy_entity_resolver.yaml` | 1,481 | 2026-09-21 |
| `focuslegacy_foreign_key_augmentation.yaml` | 1,104 | 2026-09-21 |
| `focuslegacy_few_shot.yaml` | 1,063 | 2026-09-21 |
| `focuslegacy_classification_rules.yaml` | 430 | 2026-09-21 |
| `focuslegacy_default_rules.yaml` | 390 | 2026-09-21 |
| `focuslegacy_response_template.yaml` | 385 | 2026-09-21 |
| **Total** | **6,649** | |

The four database questions that block publication, from §11:

1. **Read `curated.dim_scheme.money_unit`.** No money figure ships until this is done.
2. **Enumerate `curated.dim_pg_entity_type`.** Three rows, unknown codes, blocking a filter in
   four of the seven files.
3. **Run `meta.v_access_matrix`.** Confirm `megh_readonly` can read the view and **cannot** read
   the fact or the bank bridge. Every PII rule in this folder assumes that boundary holds.
4. **Run `meta.v_reconciliation_focus_legacy`.** Expect 14,569 = 14,566 + 3.

Two authoring jobs sit outside this folder:

- **`MASTER/schemes_catalog.yaml` has no Focus Legacy entry.** Node 1 cannot route a Focus Legacy
  question yet. Stage2 and the resolver now exist, so the keywords can be derived from them — and
  the catalogue must distinguish Focus Legacy from Focus Plus, which share a name.
- **`semantic.refresh_catalog()` has never seen this fact.** When it runs, `account_no` must be
  kept out of `sample_values` and `name_on_the_account` must stay at `is_chatbot_visible = false`.

**None of this is blocked on authoring effort.** It is blocked on the database questions in §11 — and writing them before those answers arrive would bake guesses into the
contract the whole pipeline reads.
