# CM Elevate Legacy Annotation Layer — How the YAMLs Are Built

This folder holds the hand-curated annotation layer for the **CM Elevate Legacy** scheme
(Meghalaya) of the Megh One AI NLP-to-SQL bot — the scheme the database calls **CM Elevate
Disbursement** (`source_system = 'cm_elevate_disbursement'`). Everything here is derived from the
two workbooks in `datasets/CMelevate Legacy/` and from the live database structure. Nothing in
this folder is generated at runtime — these files are the reviewed, SME-owned contract that the
retrieval and SQL-generation stages read.

It is the sibling of `Annotations/PMAY/README.md`, `Annotations/Focus+/README.md`,
`Annotations/CMElevate/README.md` and `Annotations/Focus_Legacy/README.md`. Read the PMAY one for
the house method; this one records what is different about CM Elevate Legacy.

> **Status: all 7 YAMLs built**, 2026-09-23, 6,195 lines across the seven files, every one
> parsing clean. Both source workbooks were profiled the same day (§2–§5). What remains is
> verification against the live database, not authoring — see §7.
>
> Facts below carry their provenance: **[DB]** from `SCHEMA_FOR_DEVELOPERS.md` (live database,
> 2026-09-22 11:35 UTC), **[SRC]** measured from the source workbook in this profiling run,
> **[BOTH]** independently confirmed by each. Anything still unknown is named as such in §7.

---

## 0. The one thing that must not be got wrong

**This is NOT the CM Elevate scheme in `Annotations/CMElevate/`.** Despite the shared name they
are two different schemes with two different source files, two different fact tables and two
different scheme dimensions:

| | `Annotations/CMElevate/` | `Annotations/CMElevate_Legacy/` (this folder) |
|---|---|---|
| Fact table | `curated.fact_cm_elevate_application` | `curated.fact_cm_elevate_disbursement` |
| Scheme dimension | `curated.dim_cm_elevate_scheme` — **15** schemes | `curated.dim_cm_elevate_disb_scheme` — **13** schemes |
| View | `curated.v_cm_elevate` | `curated.v_cm_elevate_disbursement` |
| Grain | one **application** (`request_id`) | one **applicant's sanction-and-disbursement record** |
| Rows | ~8,627 | **2,822** (2,823 source − 1 quarantined) |
| `source_system` | `cm_elevate` | `cm_elevate_disbursement` |
| Money | **none at all** | `sanctioned_amount` + 6 tranches + 3 totals |
| Time | **none at all** | `year_key` → `dim_year`, from a stated FY label |

The database states it outright, in the table comment on `fact_cm_elevate_disbursement`: there is
**no shared identifier between the two source files**, confirmed separate by the client, and the
fact-to-fact join is prohibited (`semantic.join_graph`, `005_join_graph_prohibitions.sql`). The
profiling confirms it from the source side: this file's only identifiers are `id` and
`application_number`, and neither appears in the CM Elevate application data (`request_id`).

Consequences every file in this folder must encode:

1. A question naming "CM Elevate" is **ambiguous between two schemes** and the classifier must
   resolve it, not guess. That is a `MASTER/schemes_catalog.yaml` problem as much as a local one
   (§9).
2. "How much was disbursed for these applications?" is **unanswerable** — it spans both schemes
   with no key to carry it across.
3. The CM Elevate rule *"no measure, no time — `COUNT(*)` is the entire aggregate vocabulary"*
   belongs to the **application** fact only. It must never be copied into this folder.

---

## 1. What is in this folder

| File | Layer | Answers the question | Status |
|---|---|---|---|
| `cmelevatelegacy_schema_partitions.yaml` | **Semantic schema** (*stage2*) | *What columns exist, what do they mean, how are they aggregated?* | **Built** — 1,637 lines, 38 columns, 13 scheme profiles, 15-item DQ register |
| `cmelevatelegacy_classification_rules.yaml` | **Clarification gate** | *What must the bot ask about instead of guessing?* | **Built** — 460 lines, 93 rules |
| `cmelevatelegacy_default_rules.yaml` | **Defaults** | *What gets filled silently, and what assumption is stated back?* | **Built** — 491 lines, 79 rules |
| `cmelevatelegacy_entity_resolver.yaml` | **Entity resolver** | *The user typed "piggery", "WGH", "FY 2024-25" — which stored value is that?* | **Built** — 1,581 lines, 9 dimensions, 59 blocks, 17-name ambiguity registry |
| `cmelevatelegacy_foreign_key_augmentation.yaml` | **Join graph** | *Which joins are permitted, and which are prohibited?* | **Built** — 707 lines, 5 declared / 10 prohibited, 7 failure scenarios |
| `cmelevatelegacy_few_shot.yaml` | **SQL examples** | *What does correct SQL look like for this scheme?* | **Built** — 878 lines, 74 examples, 12 refusals |
| `cmelevatelegacy_response_template.yaml` | **Response composer** | *How is the answer worded, and which caveats travel with it?* | **Built** — 441 lines, 92 templates, 8 worked answers, 18 caveat routes |
| `README.md` | This guide | *What are we building, what was found, and what must never change?* | This file |

Filenames follow the PMAY / Focus+ / CM Elevate / Focus Legacy convention — a scheme prefix on
every file. Prefix `cmelevatelegacy_`, formed as `focuslegacy_` was from `focusplus_`. Folder name
`CMElevate_Legacy` matches `Focus_Legacy`.

### 1.1 Where they sit in the pipeline

```
user question
   |
   v
[ NODE 1 - scheme classification ]  <-- MASTER/schemes_catalog.yaml
   |    NOTE: neither CM Elevate scheme is in the catalogue yet, and when they are
   |    added they must be DISAMBIGUATED from each other, not merged. See §9.
   v
[ clarification gate ]   <-- cmelevatelegacy_classification_rules.yaml     BUILT
   |  "CM Elevate" which scheme? "disbursed" which total? "refused" which field?
   |  "sanctioned applications" -> the sanction-rate refusal (§5.1)
   v
[ defaults ]             <-- cmelevatelegacy_default_rules.yaml            BUILT
   |  top N, period, bare "amount", the 395-row NULL-FY bucket - every fill stated back
   v
[ entity resolution ]    <-- cmelevatelegacy_entity_resolver.yaml          BUILT
   |  13 scheme names incl. the two Sericulture spellings; 12 districts; 2 FYs
   |  application_number is NEVER parsed (§4.3); names are never resolved (§4.9)
   v
[ schema linking ]       <-- cmelevatelegacy_schema_partitions.yaml        BUILT
   v
[ join discovery ]       <-- cmelevatelegacy_foreign_key_augmentation.yaml BUILT
   |  5 declared FKs; every fact-to-fact edge prohibited, starting with
   |  fact_cm_elevate_application
   v
[ SQL generation ]       <-- cmelevatelegacy_few_shot.yaml                 BUILT
   |  all examples FROM curated.v_cm_elevate_disbursement
   v
[ response ]             <-- cmelevatelegacy_response_template.yaml        BUILT
      the caveats of §4 travel with the numbers they belong to
```

**Rule of thumb** (unchanged from the other folders): a fact about a *column* belongs in stage2; a
fact about a *value* belongs in the resolver; whether to *ask or assume* belongs in exactly one of
the two gate files.

---

## 2. The source data

`datasets/CMelevate Legacy/`

| File | Size | Sheet | Shape | What it is |
|---|---|---|---|---|
| `Cm Elevate to share to BLH.xlsx` | 599 KB | `Sheet1` | **2,823 × 43** | The disbursement extract — the scheme's data |
| `Use_Cases_-_CM_Elevate.xlsx` | 16 KB | `CM Elevate Legacy` | **36 × 3** | NLP use-case workbook, TC-01…TC-36 |

The naming mirrors `Focus Legacy to share to BLH.xlsx`, which is why this scheme is filed under
"Legacy" here even though the database calls it "Disbursement".

**The load reconciles exactly.** 2,823 source rows → 2,822 curated + 1 quarantined
(`meta.v_reconciliation_cm_elevate_disbursement` requires `raw_rows = curated_rows + quarantined`).
The quarantined row is **`id = 2392`**, reason `corrupted_identity` — see §4.10, where it is
argued the quarantine may be wrong.

### 2.1 The 43 source columns and where each one went

| # | Source column | → Database | Note |
|---|---|---|---|
| 0 | `id` | `source_row_id` | UNIQUE, 2,823 distinct **[BOTH]** |
| 1–3 | `first_name`, `middle_name`, `last_name` | same names | audit only; dropped from the view (§4.9) |
| 4 | `application_number` | `application_number` | UNIQUE, all exactly 11 chars **[SRC]** |
| 5 | `desanctioned` | `desanctioned_reason_raw` | §4.4 |
| 6 | `scheme` | `disb_scheme_key` | looked up by **name**, never parsed from the id (§4.3) |
| 7 | `sanctioned` | `sanctioned_amount` | 3 nulls **[SRC]** |
| 8 | `bank_santioned` *(sic)* | `bank_sanctioned_amount` | §4.5 |
| 9,11,13 | `subsidy_disbursement_1/2/3` | same | |
| 10,12,14 | `disbursement_date`, `disbursement_date_1`, `disbursement_date_2` | `subsidy_disbursement_date_1/2/3` | **names are offset by one — see the trap below** |
| 15 | `total_subsidy_disbursement` | same | derived, verified (§4.1) |
| 16,18,20 | `loan_disrbusement_1/2/3` *(sic)* | `loan_disbursement_1/2/3` | |
| 17,19,21 | `loan_disbursement_date`, `_date_1`, `_date_2` | `loan_disbursement_date_1/2/3` | same offset trap |
| 22 | `total_loan` | `total_loan_disbursement` | derived, verified |
| 23 | `total_disbursement` | same | derived, verified |
| 24 | `loan_entity` | `loan_entity` | Bank / LIFCOM |
| 25 | `refused_y_n` | `refused_flag_raw` | §4.4 |
| 26 | `if_refused_why` | `refused_reason_text` | |
| 28 | `financial_year` | `year_key` via `dim_year` | §4.6 |
| 29 | `loan_disbursed` | `loan_disbursed_status` | fully derived from the money (§4.7) |
| 39 | `mapped_village_lgd_code` | `village_code` + `geography_key` | §4.8 |
| 35–38, 40 | `mapped_district/block` code + name, `mapped_village_lgd_name` | *not stored* | resolved away into `dim_geography` |
| 41,42 | `mapped_constituency_name`, `..._name_and_number` | **not loaded at all** | §5.2 — breaks two use cases |
| 27 | `month` | **not loaded at all** | §4.6 — and it is not a real month anyway |
| 30–34 | `loan_repaid`, `version`, `created_date`, `updated_date`, `scheme_master_id` | **not loaded** | dead: single constant value each **[SRC]** |

> **The date-naming trap.** In the source, `disbursement_date` belongs to
> `subsidy_disbursement_1`, `disbursement_date_1` belongs to `subsidy_disbursement_2`, and
> `disbursement_date_2` belongs to `subsidy_disbursement_3` — the dates are numbered one behind
> their amounts. The loader realigns them, so the database's `subsidy_disbursement_date_2` is the
> source's `disbursement_date_1`. Anyone re-profiling the Excel against the database must not
> read the two `_1` columns as the same thing. Same offset on the loan side.

**The five dead columns** are extract artefacts, not data: `loan_repaid` = 0 on all 2,823 rows,
`version` = 1, `scheme_master_id` = 11, and `created_date` = `updated_date` = a single timestamp
`2026-09-03 04:40:48.282`. The database is right to drop them, and no YAML should mention them —
in particular `loan_repaid` must never be presented as "no loans have been repaid".

---

## 3. The profiling recipe

```
# Step 1 - shape, dtype, null census, cardinality on all 43 columns
#   -> found the 5 dead constants and the 3 columns with mixed types

# Step 2 - THE MONEY IDENTITIES. three of them, all three exact.
#   -> total_subsidy == s1+s2+s3, total_loan == l1+l2+l3,
#      total == subsidy+loan, on all 2,823 rows, max abs diff 0.00
#   -> this is what makes a money answer defensible. §4.1

# Step 3 - value census on every low-cardinality column
#   -> scheme (13), desanctioned (2), loan_entity (2), loan_disbursed (2),
#      month (4!), financial_year (2!)  -> §4.6 falls out of this

# Step 4 - date probe: type, range, corruption, future dates
#   -> the 4 string dates with a corrupted century, CONFIRMING the DB comment,
#      plus TWO anomalies the DB does not record (§4.2)

# Step 5 - stated financial_year vs FY derived from the disbursement date
#   -> 114 mismatches of 1,842 comparable rows. The two time signals are NOT
#      interchangeable. Focus Legacy's equivalent step found zero. §4.6

# Step 6 - application_number structure probe
#   -> prefix x scheme crosstab; 7 schemes use more than one prefix;
#      the 5th character is a type code for some schemes and not others. §4.3

# Step 7 - refusal-field agreement matrix, and duplicate probes
#   -> reproduced the DB's "4 rows", "id=429" and "6 of 31" exactly. §4.4

# Step 8 - geography completeness, and the use-case workbook read
#   -> 12 districts, 59 blocks, 1,051 village codes, 404 rows with none. §4.8, §5
```

---

## 4. Verified facts (profiled 2026-09-23)

### 4.1 The three money identities hold exactly — the headline finding

**[BOTH]** On all 2,823 source rows, with a maximum absolute difference of **0.00**:

```
total_subsidy_disbursement == subsidy_disbursement_1 + _2 + _3
total_loan_disbursement    == loan_disbursement_1 + _2 + _3
total_disbursement         == total_subsidy_disbursement + total_loan_disbursement
```

So the totals are safe to use and never need re-deriving from the tranches. What this does *not*
settle is which one a user means. **"Disbursed" is a three-way ambiguity** — subsidy, loan, or
both — and sanctioned is a fourth number that is not disbursement at all:

| Measure | Total **[SRC]** |
|---|---|
| `sanctioned` | Rs 142.74 Cr |
| `total_subsidy_disbursement` | Rs 52.57 Cr |
| `total_loan_disbursement` | Rs 30.33 Cr |
| `total_disbursement` | Rs 82.90 Cr |
| `bank_sanctioned_amount` | Rs 40.21 Cr — **do not publish**, §4.5 |

Disbursement is **58.1%** of sanctioned overall, and ranges from **0.1%** (Sericulture spinning)
to **100%** (Motorcaravan, one row) by scheme. A bare "how much under CM Elevate Legacy?" that
silently picks one of these is wrong four ways out of five.

Tranche shape **[SRC]**: 2 subsidy tranches is the norm (1,863 rows), 1 tranche 469 rows, 3
tranches 13, none 478. Loans are single-tranche almost always (802 rows with money, 13 with a
second, 4 with a third).

**`sanctioned` is a per-scheme constant for 6 of the 13 schemes** **[SRC]** — Piggery 1,25,000
(×1,353 rows), Poultry 1,20,000 (×454), Dairy 3,00,000 (×132), Goat 1,00,000 (×48), Motorcaravan
50,00,000 (×1), and Sericulture spinning near-constant. This is the PMAY `sanctioned_amount =
130000` situation again: **"average sanctioned amount for Piggery" is not a statistic, it is the
entitlement restated**, and the answer should say so rather than printing a mean.

The same structure shows up in the disbursement: **1,145 of 1,353 Piggery rows disbursed exactly
Rs 62,500** — two tranches of Rs 31,250, half the entitlement.

### 4.2 Date corruption — the DB records four, the file has seven

**[BOTH]** The four rows the database documents are real, and are **literal strings** in the
Excel, which is why that column loads as mixed-type:

| `id` | scheme | raw value | `subsidy_disbursement_1` |
|---|---|---|---|
| 1351 | Piggery | `1015-01-15` | 31,250 |
| 1355 | Piggery | `1014-11-06` | 31,250 |
| 1378 | Piggery | `1014-11-20` | 31,250 |
| 1556 | Piggery | `1015-02-18` | 31,250 |

All four load as NULL; the amounts are real and unaffected. *Do not guess a corrected date.*

**[SRC] Three further date anomalies the database comments do not mention:**

- **`id = 532`** — `loan_disbursement_date` = **1985-07-22**, with a real Rs 20,000 loan against
  a Piggery record. Not a century typo of the same shape as the four above; it parses as a valid
  date, so it will survive into the database and into any date filter.
- **`id = 1602` and `id = 1696`** — first subsidy date **2026-09-04**, which is *after* the
  extract's own `created_date` of 2026-09-03. A disbursement dated after the file was produced.

These three are inside the loaded data, unflagged. Any "earliest/latest disbursement" answer hits
one of them, so date extrema need a sanity caveat and the 1985 row should be raised with the
data owner.

### 4.3 `application_number` must never be parsed — now proven twice

**[DB]** The database says the prefixes are *observed*, not enforced, and cites the one Poultry
row carrying Piggery's `MPDSI` prefix.

**[SRC] Confirmed, and it is worse than one row:** **7 of the 13 schemes use more than one
prefix.**

| Scheme | Prefixes used |
|---|---|
| Prime Agriculture Response Vehicle | `ARVSI` 85, `ARVSR` 104, `ARVSU` 3 |
| Sericulture & Weaving (weaving) | `MSWWI` 180, `MSWWR` 11, `MSWWU` 6 |
| Sericulture & Weaving (spinning) | `MSWSI` 192, `MSWSR` 2, `MSWSU` 4 |
| Agriculture Warehouse | `MEWSI` 10, `MEWSR` 77 |
| Common Facility Center | `MCFCR` 26, `MCFCU` 1 |
| Prime Tourism Vehicle | `PTVSI` 88, `PTVSR` 4 |
| **Poultry Farming** | `MPFSI` 453, **`MPDSI` 1** ← Piggery's prefix |

> **Correction to the database comment.** `dim_cm_elevate_disb_scheme.application_id_prefixes`
> cites this row as **`id=2045`**. It is not: `id=2045` is `MPFSI000961`, an ordinary Poultry
> record. The actual cross-prefixed row is **`id = 1894`, `MPDSI001154`**. The finding is right,
> the citation is wrong — worth correcting in the DB comment so the next person can find the row.

The 5th character (`I` 2,542 / `R` 225 / `U` 14) looks like an applicant-type code —
Individual / Registered / Unregistered, matching the CM Elevate application taxonomy — **but it
is not reliable**: two schemes (Any Business Venture, Sports & Wellness) have five-letter
acronyms, so their 5th character `S` is part of the scheme code, not a type. There is **no
applicant-type column** in this fact table, and this near-pattern must not be used to invent one.

### 4.4 No refusal field is authoritative — reproduced exactly

**[BOTH]** Three columns describe refusal and they disagree:

| `desanctioned` | rows | of which `refused_y_n` set |
|---|---|---|
| *(null)* | 2,738 | **2** ← flagged refused with no desanction reason |
| `Refused` | 47 | **5** |
| `Duplicate` | 30 | **1** |

`refused_y_n` is set on only **8 rows in total, and its only value is `1`** — there is no
recorded "not refused", so a false is *absent*, not false. `if_refused_why` is populated on 7
rows with 6 distinct free-text reasons ("Government Employee", "Wife is a government employee",
"Unable to connect", "Repeated name", "Family emergency", "Economical reason").

This profiling reproduces the database's two specific claims exactly: **4** rows carry
`Refused` + flag + a reason, with **`id = 429`** the fifth candidate whose reason is blank; and
of the 31 `Duplicate` rows only **6** have an observable (name, scheme) twin in the file —
ids 405, 1064, 1160, 1288, 1415, 1448, three name-pairs, all Piggery.

**[SRC] And refusal does not stop the money:** **9 of 52 `Refused` rows and 23 of 31 `Duplicate`
rows still have a disbursement recorded** — `id = 429` (Refused) received Rs 62,500; `id = 1415`
(Duplicate, "Repeated name") also Rs 62,500. So excluding refused rows from a money total is a
*choice* that changes the answer, and it must be stated, never made silently.

"How many were refused?" is therefore a **clarification**, not a default, and every refusal count
must name the field it came from.

### 4.5 `bank_sanctioned_amount` — meaning UNCONFIRMED

**[BOTH]** **50.8%** of rows are exactly 0 (1,435 of 2,823). Where non-zero, the ratio to
`sanctioned` clusters at **0.20** (508 rows), **0.50** (419), **0.496** (405) and **0.60** (30),
with a long tail of one-off ratios — and it **varies within a single scheme**: Prime Tourism
Vehicle shows 9 distinct ratios, Prime Agriculture Response Vehicle 8, Poultry 3.

So it is not a scheme constant and not a clean percentage; it looks like a per-applicant workflow
stage or subsidy tier, unverified. *Do not build a metric on this column.* No ratio, no "bank
share" phrasing; a question needing it gets an explicit "meaning unconfirmed" caveat or a refusal.

### 4.6 Time is a two-value label, and the dates disagree with it

**[SRC]** This is the finding that most constrains the response layer.

- **`financial_year` has exactly two values**: `2024-25` (2,291 rows) and `2025-26` (137), with
  **395 rows null**. A "trend over the years" has two points and a hole.
- **`month` has exactly four values**: Nov (1,141), Apr (1,154), Feb (108), Mar (25) — null on
  the same 395 rows. This is a **disbursement-batch label, not a monthly time series**, and the
  database does not load it at all. Monthly questions are unanswerable *and* would be misleading
  if the column were reinstated.
- **Stated FY vs FY derived from the first subsidy date: 114 mismatches out of 1,842 comparable
  rows** — 77 stated `2024-25` that derive to `2025-26`, 34 the other way, 2 deriving to
  `2026-27`, 1 to `2023-24`. Focus Legacy's equivalent check found **zero** mismatches; here the
  two time signals are **not interchangeable**. The database's `year_key` comes from the stated
  label, so an FY answer and a date-range answer over the same period will not agree.
- **9 rows have a real disbursement date but no stated FY** (all Sericulture weaving,
  2025-09-24) — they land in the NULL-`year_key` bucket despite being datable.

**[DB]** `year_key` is nullable and `v_cm_elevate_disbursement` reaches `dim_year` through a
**LEFT JOIN**, so those 395 rows are kept, not dropped. Every FY grouping must carry a NULL-FY
bucket, and an FY-filtered total must say that 395 records (14% of the file) sit outside it.

### 4.7 `loan_disbursed` is derived, not independent

**[SRC]** `loan_disbursed = 'disbursed'` ⟺ `total_loan > 0`, on every row without exception
(802 / 802 and 2,020 / 2,020). It carries no information the money does not already carry. It may
be used as a convenient filter, never as corroboration of the amount.

`loan_entity` **[SRC]**: `Bank` 1,733, `LIFCOM` 689, **null 401** — and 19 of those nulls have
loan money anyway. So "disbursement by loan entity" (use case TC-22) has an unattributed bucket
that must appear in the answer rather than being dropped by a `GROUP BY`.

### 4.8 Geography

**[SRC]** 12 districts (Title Case in the source), 59 blocks, 1,051 distinct village codes,
1,033 distinct village names. Village code → name is **1:1** (0 codes with two names), but **17
village names map to more than one code** (Amonggre, Ampanggre, Apalgre, Babupara, Bolchugre …),
which is the usual reason names must never be a join key or the basis of a count.

**404 rows (14.3%) have no village code at all** — spread across all 12 districts, worst in West
Khasi Hills (77), Ri Bhoi (61), West Garo Hills (54). District and block are always present.
**[DB]** These become the `entity_type = 'Unresolved'` placeholders: included at district grain so
totals reconcile, and **excluded from every village-count or village-list answer**.

19 rows sit in blocks that are Municipal Boards (Resubelpara, William Nagar, Tura), i.e. urban
wards rather than villages — worth knowing before anyone writes "rural coverage".

District distribution **[SRC]**: West Garo Hills 493, Ri Bhoi 486, South West Garo Hills 461,
East Khasi Hills 241, West Khasi Hills 217, Eastern West Khasi Hills 210, East Garo Hills 200,
West Jaintia Hills 153, North Garo Hills 141, South West Khasi Hills 94, South Garo Hills 92,
East Jaintia Hills 35.

> **Casing.** The source is Title Case (`West Garo Hills`). The database's conformed
> `dim_geography.lgd_district` is documented as **UPPERCASE-only** (stated on `v_focus_plus`,
> and `dim_geography` is shared by every scheme) — a mixed-case exact-match filter returns zero
> rows **silently, not as an error**. The resolver must normalise. Confirm against the live table
> before stage2 (§7).

### 4.9 Names are audit-only, and they are not clean either

**[DB]** `first_name` / `middle_name` / `last_name` live on the fact for audit and are **dropped
entirely from `v_cm_elevate_disbursement`**, and excluded from `semantic.v_embedding_documents`
(`is_chatbot_visible = false`). This is a third PII pattern in the warehouse — PMAY/Focus+
exclude at load, Focus Legacy masks the account and drops the name, here the columns load but the
view drops them.

**[SRC]** `middle_name` is null on 1,829 of 2,823 rows and `last_name` on 14; 2,595 distinct
first names over 2,823 rows. 45 rows share a full name with another row (22 name groups), of
which only 8 rows share both name **and** scheme. A name is not an identifier here.

Every example in `few_shot` must query `curated.v_cm_elevate_disbursement`, never the fact table.
"Who received the money" is a refusal.

### 4.10 The quarantined row may not deserve it

**[SRC]** The one row the loader held back as `corrupted_identity` is **`id = 2392`**, and its
name fields read `first_name = "4"`, `middle_name = "For"`, `last_name = "All"` — i.e. **"4 For
All"**, under `MSWCS000042`, Meghalaya Sports & Wellness Scheme. Eight of the nine rows in that
scheme are `S`-coded (group) applications.

That is very likely a real **team or organisation name**, not corruption. If so, the file's
Sports & Wellness scheme is short one record in the database and its sanctioned total is
understated. **Raise with the data owner before any Sports & Wellness figure is published**; do
not silently reverse the quarantine.

---

## 5. The use-case workbook, and the conflict it creates

`Use_Cases_-_CM_Elevate.xlsx`, sheet `CM Elevate Legacy`, **36 use cases** (TC-01…TC-36) with an
example bot query each. Sorted against what the data can actually do:

| Band | Cases | Verdict |
|---|---|---|
| Programme narrative | TC-01 … TC-10 | **Unanswerable from this data — 10 of 36** |
| Counts and scheme mix | TC-11, TC-12, TC-13 | Answerable |
| Sanction status | TC-14, TC-15, TC-16, TC-19, TC-35 | **Unanswerable as written — see §5.1** |
| Money | TC-17, TC-18, TC-24, TC-27, TC-30, TC-32 | Answerable **only after the "which total" clarification** (§4.1) |
| Loan entity | TC-20, TC-21, TC-22 | Answerable, with the 401-row null bucket (§4.7) |
| Financial year | TC-23, TC-24, TC-25 | Answerable, two FYs + 395-row NULL bucket (§4.6) |
| Geography | TC-26 … TC-30, TC-33, TC-34, TC-36 | Answerable, minus the 404 unresolved villages (§4.8) |
| Constituency | TC-31, TC-32 | **Not answerable from the view — see §5.2** |

### 5.1 The sanction-rate conflict — the constraint every file here encodes

Five use cases ask for **sanctioned vs non-sanctioned applications** and a **sanction rate**
(TC-14 "How many applications have been sanctioned?", TC-15 "What percentage of applications have
been sanctioned?", TC-16, TC-19, TC-35).

**There is no sanction-status column in this data, and there cannot be a rate.** Every row in this
file *is* a sanctioned record — 2,820 of 2,823 carry a `sanctioned` amount, and the 3 that do not
are refusals (§4.4). The denominator "all applications" lives in the **other** CM Elevate scheme,
`fact_cm_elevate_application`, which has **no join key to this one** (§0).

So the honest answers are:
- *"How many sanctioned?"* → the row count, stated as **"every record in this dataset is a
  sanction record"**, not as a filtered subset.
- *"What is the sanction rate?"* → **refuse**, and explain that the application population is a
  separate dataset with no linkage.
- *"How many not sanctioned?"* → **refuse** for the same reason; the 83 desanctioned/duplicate
  rows are a *post*-sanction outcome, not a rejected application, and offering them as the answer
  would be the single most misleading substitution available here.

This mirrors the CM Elevate folder's own use-case conflict and is recorded the same way: the
conflict is encoded in the YAMLs as deliberate refusals, not resolved by picking a plausible
number.

### 5.2 Constituency is in the file but not in the database

TC-31 and TC-32 ask for applications and disbursement **by constituency**. The source has
`mapped_constituency_name` (55 values, e.g. `30 MAIRANG`, `27 PYNURSLA`) on 2,419 rows — **but
the loader does not carry it into `fact_cm_elevate_disbursement`, and
`v_cm_elevate_disbursement` does not expose it.**

`dim_geography` does hold `ac_number` / `ac_name`, so the data exists in the warehouse and the
view could be extended. Until it is, **TC-31 and TC-32 are refusals with a named fix** — a view
change, not a data gap. Record it as such so it is not mistaken for missing data.

### 5.3 Vocabulary mismatch

The workbook calls every row an "application" and the programme "CM-ELEVATE". The grain here is
**one applicant's sanction-and-disbursement record**, and "CM-ELEVATE" is the name of a *different*
scheme in this warehouse. The resolver must map the user's "application" to this grain without
adopting the word in answers, and "CM-ELEVATE" must go through the §0 disambiguation first.

---

## 6. Anatomy of each YAML

All seven are built against `Annotations/PMAY/` as the structural template — same top-level
keys, same per-rule shape, same `unverified_in_db` provenance convention. What each one owns,
and where it deliberately departs from PMAY because the data differs:

| File | Load-bearing decisions |
|---|---|
| `schema_partitions` | 43 source → 37 fact columns; the 5 dead columns excluded; 3 totals marked derived-and-verified; `bank_sanctioned_amount` marked unconfirmed; `month`/constituency marked absent-by-design; units in **rupees** (confirm §7) |
| `classification_rules` | which CM Elevate; which total; which refusal field; sanction-rate refusal; "average sanctioned" for the 6 constant schemes |
| `default_rules` | top N, default period, bare "amount", the NULL-FY and null-`loan_entity` buckets — every fill stated back |
| `entity_resolver` | 13 scheme names incl. the `(spinning)` / `(weaving)` spacing trap; 12 districts Title-Case→UPPER; 2 FY labels; never parse `application_number`; never resolve a person |
| `foreign_key_augmentation` | 5 declared FKs; all fact-to-fact prohibited, `fact_cm_elevate_application` named explicitly |
| `few_shot` | every example `FROM curated.v_cm_elevate_disbursement`; the refusals stay in stage2 as the other folders do |
| `response_template` | caveats from §4.1 (which total), §4.2 (date extrema), §4.5 (bank amount), §4.6 (395 NULL-FY, FY≠dates), §4.8 (404 unresolved villages), §5.1 (sanction rate) |

---

## 7. Open items to confirm before stage2 is written

1. **`dim_scheme.scheme_code` for this scheme.** `dim_scheme` is now 6 rows; the schema doc does
   not print row values. Read it live — do not invent a code.
2. **`money_unit`, `grain_note` and `time_semantics` on that row.** `money_unit` decides rupees
   vs lakhs in every template. The source is plainly in **rupees** (125000 = Rs 1.25 lakh
   entitlement), but the stored value must be read, not assumed.
3. **`dim_geography.lgd_district` casing** — confirm UPPERCASE, since §4.8's resolver rule
   depends on it.
4. **The 13 scheme names exactly as stored** in `dim_cm_elevate_disb_scheme`, including whether
   the loader preserved `Scheme(weaving)` without the space.
5. **`id = 2392` ("4 For All")** — is the quarantine correct? (§4.10)
6. **`id = 532`, the 1985 loan date**, and the two 2026-09-04 dates (§4.2) — real or corrupt?
7. Whether `mapped_constituency_name` should be added to the view (§5.2).

---

## 8. Cross-folder work this scheme creates

- `MASTER/schemes_catalog.yaml` lists **only `mgnrega` and `pmay`**. Focus+, CM Elevate and Focus
  Legacy are all missing, and this scheme will be too. When both CM Elevate schemes are added,
  their `keywords` must **disambiguate** rather than overlap — otherwise Node 1 routes a money
  question to the fact that has no money.
- `Annotations/CMElevate/README.md` states CM Elevate has "no measure and no time". Still true of
  `fact_cm_elevate_application`, **not** true of this scheme. Worth a pointer there.
- Two corrections are owed to the database itself: the `id=2045` citation (§4.3) and, pending
  confirmation, the `id=2392` quarantine (§4.10).

---

## 9. Build order

1. ~~Profile both workbooks~~ — **done 2026-09-23**, §2–§5.
2. Confirm the §7 open items against the live database.
3. ~~All seven YAMLs~~ — **done 2026-09-23**, v1.0, built against PMAY's files as the style
   reference (§6).
4. Update this README as each file lands.

Nothing is authored until it is asked for, file by file.
