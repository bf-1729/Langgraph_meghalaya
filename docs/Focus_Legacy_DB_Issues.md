# Focus Legacy — Database Issues Found in Use-Case Testing

**Date:** 2026-09-25
**Dataset:** Focus Legacy — `curated.v_focus_legacy` (producer-group payments)
**Raw source compared:** `Focus Legacy to share to BLH.csv` (14,569 rows)
**Full test report:** `docs/Focus_Legacy_UseCase_Test_Report.xlsx`

## Summary

All 28 Focus Legacy use cases were run through the bot and compared with both the DB and the raw
file. The DB loads every source row (14,569 = 14,569). Row by row it matches the source on pg_id,
member count, amount, district, financial year, remittance date and assembly constituency (through
`dim_geography.ac_name`).

Two issues were found in the data load:

| # | Issue | Records affected | Test cases affected | Severity |
|---|---|---|---|---|
| 1 | Block is missing for records with no village code | 1,013 rows (821 PGs, ₹3.82 Cr) | TC-26 fails; TC-28 (block option) is also off | **High**: every block figure undercounts |
| 2 | Only one "current" name is kept per PG; other source spellings are lost | 1,263 PGs whose source spelling cannot be found | None of the 28 cases; affects name searches | Medium: needs a decision |

Of the 9 failed use cases, only TC-26 is caused by the DB. The other 8 are bot issues and are
tracked separately.

---

## Issue 1: Block is NULL for the 1,013 records with no village code

### What is wrong

1,101 source rows have no `mapped_village_lgd_code`. All of them load into the DB with
`entity_type = 'Unresolved'`, `lgd_village_name = 'Unresolved / Not Yet Mapped'` and
**`lgd_block = NULL`**. For **1,013** of those rows, the source file gives the block in
`mapped_block_lgd_name` / `mapped_block_lgd_code`.

As with CM Elevate Legacy, the block seems to be derived only through the village
(`geography_key` → `dim_geography`), so a row with no village loses its block. District is
unaffected.

The correct block is already in the view, in `block_name_raw`. On all 1,013 rows it equals the
source block name exactly. All 37 affected block names already exist as `lgd_block` values, so
nothing new needs to be added to the block list.

| | Raw file | DB (`lgd_block`) |
|---|---:|---:|
| Rows with no block | 88 | **1,101** |
| Rows whose block differs from the source | – | **1,013** |

(The 88 rows are blank in the source file as well, so they are not a DB defect.)

### How it happened

1. **The payment row does not store its own block.** At ingest, the source's
   `mapped_block_lgd_code` is dropped and the block name is not written to the fact as a geography
   field (dataset README §2.1). The fact holds only a `geography_key`, and the view reads district,
   block and village through it from `curated.dim_geography`. So a row's block is whatever block
   its `dim_geography` row holds.
2. **A village row carries its block.** For the 13,468 rows with a village code, `geography_key`
   points to that village's `dim_geography` row, which has the correct block. All of these rows
   have a block.
3. **Rows with no village are pointed at one placeholder per district.** For the 1,101 rows with
   no village code, the loader cannot look up a village, so it points every one of them to a
   single **district-level placeholder** row (`entity_type = 'Unresolved'`, name "Unresolved /
   Not Yet Mapped", negative sentinel `village_code`, e.g. `-900012` for West Khasi Hills). There
   are 12 placeholders, `geography_key` 7365–7376, one per district.
4. **One placeholder cannot hold more than one block, so its block is left NULL.** The unmapped rows
   in a district come from several blocks. In West Khasi Hills the one placeholder (key 7376)
   stands for 111 Nongstoin rows, 66 Mawshynrut rows and 6 rows with no block. A single row can
   hold only one `lgd_block`, so the loader leaves it NULL, and all 1,013 rows inherit NULL through
   the join.
5. **The block survives only as an audit copy.** The loader also writes the source text to
   `block_name_raw` on the fact. That is why the correct value is still in the view, but in a
   column block-level queries do not use.

**Why it was not caught.** The load reconciliation checks row counts, totals and districts, and all
of those match: the placeholder keeps the district, and every rupee is still counted. Nothing
compares block to block. The dataset README flagged this exact risk before go-live, as open
verification item 6 ("Establish what an `Unresolved` placeholder stores in `lgd_block`. If it is
NULL, block totals lose 1,013 rows"), but the check was not run. The CM Elevate Legacy load uses
the same placeholder design and has the same defect (404 rows).

**Why the source has no village for these rows.** The field data mapped these PGs to a block but not
to an LGD village code (1,013 payments, 7% of the total; 79% of them in FY 2024-25 and 2025-26). The DB is
right to leave the village unresolved. The defect is only that the known block is thrown away
along with it.

### Impact

Every block-level figure undercounts. The 1,013 rows carry **₹3,81,75,000** paid to **821
Producer Groups** (7,635 memberships). They span **37 blocks in 11 of 12 districts** (East Jaintia
Hills is unaffected). By financial year: 2025-26 has 532 rows, 2024-25 264, 2022-23 156 and
2021-22 61.

**By district** (rows with no block in the DB):

| District | Rows | Blocks affected | PGs | Amount (₹ lakh) |
|---|---:|---:|---:|---:|
| Ri Bhoi | 183 | 3 | 137 | 74.00 |
| West Khasi Hills | 177 | 2 | 156 | 88.20 |
| West Garo Hills | 169 | 8 | 128 | 50.15 |
| Eastern West Khasi Hills | 127 | 2 | 102 | 49.45 |
| North Garo Hills | 116 | 3 | 98 | 46.65 |
| West Jaintia Hills | 91 | 3 | 74 | 26.00 |
| East Garo Hills | 48 | 3 | 39 | 11.90 |
| South West Garo Hills | 45 | 4 | 33 | 12.35 |
| South Garo Hills | 41 | 4 | 38 | 15.15 |
| East Khasi Hills | 11 | 3 | 11 | 6.90 |
| South West Khasi Hills | 5 | 2 | 5 | 1.00 |
| **Total** | **1,013** | **37** | **821** | **381.75** |

**Most affected blocks:**

| Block | Rows missing | PGs missing | Amount missing (₹ lakh) |
|---|---:|---:|---:|
| Umsning | 161 | 120 | 66.50 |
| Nongstoin | 111 | 103 | 67.85 |
| Mairang | 86 | 74 | 28.90 |
| Thadlaskein | 67 | 57 | 19.10 |
| Mawshynrut | 66 | 53 | 20.35 |
| Selsella | 57 | 38 | 18.65 |
| Bajengdoba | 55 | 42 | 19.75 |
| Resubelpara | 45 | 44 | 19.85 |
| Mawthadraishan | 41 | 28 | 20.55 |
| Dambo Rongjeng | 38 | 29 | 9.70 |

(Missing PGs are counted per block. A PG with both a mapped and an unmapped payment is still
counted once in the block, which is why the block-level shortfalls in the table below can be
smaller than these.)

### Affected test cases

| Test case | Question | DB / bot | Raw file |
|---|---|---|---|
| TC-26 (**FAIL**) | How many Producer Groups are mapped to each block for West Khasi Hills? | Nongstoin 357, Mawshynrut 233, Shallang 78, Rambrai 56, Ri Muliang 41, **no block 161** | Nongstoin 460, Mawshynrut 286, Shallang 78, Rambrai 56, Ri Muliang 41 (5 blank in source) |
| TC-28 (block option) | How many Producer Groups are mapped to Songsak? (Songsak **block** chip) | 338 PGs | 344 PGs |

TC-28 is marked PASS because the use case asks for the constituency, which is correct (387 = 387).
The block option the bot offers first returns 338 because of this issue.

Other block figures for the same blocks (for re-testing after the fix):

| Block | Rows DB / Raw | PGs DB / Raw | Amount DB / Raw (₹ lakh) |
|---|---|---|---|
| Nongstoin | 442 / 553 | 357 / 460 | 194.75 / 262.60 |
| Mawshynrut | 282 / 348 | 233 / 286 | 121.95 / 142.30 |
| Songsak | 379 / 385 | 338 / 344 | 80.90 / 81.40 |

### Suggested fix

When loading the Focus Legacy fact, populate the block for `Unresolved` rows from the source's
`mapped_block_lgd_name` / `mapped_block_lgd_code`, not only through the village. The view should
then expose it in `lgd_block`.

If changing the load is not possible soon, a view-level fallback gives the same result:
`COALESCE(lgd_block, UPPER(block_name_raw)) AS lgd_block`.

This is the same defect already reported for CM Elevate Legacy (`docs/CM_Elevate_Legacy_DB_Issues.md`,
Issue 1), so one fix in the shared loader logic may cover both datasets.

### How to check

```sql
-- Before the fix: 1,101. After the fix: 88 (the rows that are blank in the source too).
SELECT COUNT(*) AS rows_without_block
FROM curated.v_focus_legacy
WHERE lgd_block IS NULL;

-- Before the fix: 1,013. After the fix: 0.
SELECT COUNT(*) AS rows_block_lost
FROM curated.v_focus_legacy
WHERE lgd_block IS NULL AND block_name_raw IS NOT NULL;

-- After the fix: Nongstoin 460, Mawshynrut 286, Shallang 78, Rambrai 56, Ri Muliang 41, NULL 5.
SELECT lgd_block, COUNT(DISTINCT pg_id) AS producer_groups
FROM curated.v_focus_legacy
WHERE lgd_district = 'WEST KHASI HILLS'
GROUP BY lgd_block
ORDER BY producer_groups DESC;
```

---

## Issue 2: Only one name per PG is kept; other source spellings are lost

### What is wrong

In the source, a PG's name is often spelled differently across its own payments. **1,634 pg_ids**
have more than one spelling. The DB keeps a single current name per pg_id (`pg_name`, from
`dim_producer_group.current_name`) and shows it on every payment row. The dataset README (§4.8)
describes this as intentional.

The side effect is that the source's other spellings are not held anywhere in the view:

| | Raw file | DB |
|---|---:|---:|
| Distinct PG names | 10,678 | 9,452 |
| Rows whose name differs from the source row | – | 1,636 |
| PGs whose source spelling does not exist anywhere in the DB | – | **1,263 PGs (1,226 spellings)** |

Examples:

| pg_id | Name in raw file | Name in DB |
|---|---|---|
| PG-FOCUS-EGH-755 | Bak 15 Banana | Bak-15 Banana Pg 40/2020 |
| PG-FOCUS-EGH-2117 | Bak 15 Banana Watenanggre | Bak-15 Banana Pg- 43/2020 ( Watenanggre) |
| PG-FOCUS-WJH-285 | Di Ke Mi Don Ka Jingmut Producer Group | Di-ke-mi-don-ke-jingmut |

### How it happened

1. The source is one row per **payment**, and the name is typed on each payment. A group paid in
   2021-22 and again in 2025-26 often has its name written differently each time (with or
   without "Pg", hyphens, a registration number, the village in brackets).
2. The load separates the group from its payments. The name goes to
   `dim_producer_group.current_name` (one row per `pg_id`), and the fact keeps only `pg_id`, not
   the name that was on that payment.
3. When a `pg_id` has several spellings, the loader keeps **the spelling on its most recent
   payment**. Verified: in 1,583 of the 1,634 multi-spelling groups the DB name equals the name on
   the latest-dated payment, and in the rest (same-date ties) it is still one of the group's own
   source spellings. No name is invented or cleaned. Groups with a single spelling are unchanged.
4. The view shows that one name on every payment row, so the older spellings are not held anywhere
   a query can reach.

This is a design choice, not a load error. The README chose it so that a group is never split
into two because of a spelling change. It only becomes a problem for looking up a group by name.

### Impact

None of the 28 use cases failed because of this. Counts are made on `pg_id`, which is complete and
correct. But a user who searches for a PG by the name in the source file or bank records ("Is
there a PG named Bak 15 Banana Watenanggre?", "How many members are in Di Ke Mi Don Ka Jingmut
Producer Group?") will get **"not found"** for any of these 1,263 PGs. Searching "Bak 15 Banana"
also returns a different group (PG-FOCUS-EGH-754), which still carries that name.

This was measured on the data (exact, case-insensitive name match). It was not run through the bot.

### Decision needed

Please confirm with the data owner which of these is wanted:

1. **Keep as is.** The README choice stands, and the bot tells users to search by pg_id when a
   name is not found.
2. **Expose the name history.** Add a `pg_name_aliases` column, or a `dim_producer_group_name`
   table (pg_id → every source spelling), so name searches can match any spelling the group has
   used.
3. **Keep the source name per row.** Add `pg_name_raw` to the view (the name exactly as on that
   payment), alongside the current `pg_name`.

Option 2 or 3 lets the bot find all 11,906 groups by any name they appear under in the source.

### How to check

```sql
-- Today: 0 rows. After option 2 or 3, the original spelling is found.
SELECT pg_id, pg_name
FROM curated.v_focus_legacy
WHERE pg_name ILIKE 'Bak 15 Banana Watenanggre';

-- Distinct names: 9,452 today; 10,678 in the source.
SELECT COUNT(DISTINCT pg_name) FROM curated.v_focus_legacy;
```

---

## After the fixes

Re-run the use cases. After the Issue 1 fix, TC-26 should match the raw file with no bot change,
and so should the Songsak block option in TC-28. The bot already queries `lgd_block`, which is the
column being corrected. Issue 2 does not change any of the 28 results but should be decided before
name search is released. The bot team can re-run the test harness on request.

## 2026-09-29 — mis-encoded producer-group names (found by the all-PG bulk run)

Six `pg_name` values are double-encoded UTF-8 (mojibake) in **both** `Focus Legacy to share to
BLH.csv` and `curated.v_focus_legacy`, so this is a source-data defect, not an ingestion error:

| pg_id | stored name (as stored) |
|---|---|
| PG-FOCUS-NGH-6415 | `AÃ£Æ’Ã¦â€™Ã£Â¢Ã¢â€šÂ¬Ã¥Â¡Ã£Æ’Ã¢â‚¬Å¡Ã£â€šÃ¢Â·we Producer Group` |
| PG-FOCUS-NGH-10785 | `Sildoe Producer GroupÃ£Æ’…s` |
| PG-FOCUS-EKH-15157 | `MawleiÃ±` |
| PG-FOCUS-WKH-9993 | `MawehdÃ n` |
| PG-LAMP-EJH-9026 | `ThaiÃ£Æ’…lang Handicrafts` |
| PG-LAMP-NGH-5019 | `KuÃ£Æ’…` |

**Ask:** correct the names at source (likely `Mawleiñ`, `Mawehdàn`, and a curly apostrophe or
dot in the others). The bot now matches them only against themselves (KI-155), but an officer
cannot type them.

Also re-confirmed on 2026-09-29: DB = raw row for row on 14,569 rows (pg_id, members, amount,
date, FY, district, block, village, constituency); 88 rows have no block in either source.

