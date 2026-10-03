# CM Elevate Legacy — Database Issues Found in Use-Case Testing

**Date:** 2026-09-25
**Dataset:** CM Elevate Legacy — `curated.v_cm_elevate_disbursement` (DB: CM Elevate Disbursement)
**Raw source compared:** `Cm Elevate to share to BLH (1).csv` (2,823 rows)
**Full test report:** `docs/CM_Elevate_Legacy_UseCase_Test_Report_v2_after_fixes.xlsx`

## Summary

All 36 CM Elevate Legacy use cases were run through the bot and compared with both the DB and the
raw file. **In every case the bot's answer equals the DB.** The 16 cases that do not match the raw
file are caused by two issues in the data load, not by the bot:

| # | Issue | Records affected | Test cases affected |
|---|---|---|---|
| 1 | Block is missing for records with no village code | 404 | TC-29, TC-30, TC-33 (3) |
| 2 | Source row `id 2392` is quarantined and not loaded | 1 | TC-11, 14, 17, 18, 19, 21, 22, 23, 24, 25, 26, 27, 36 (13) |

Fixing both in the load makes all 16 match the raw file with **no change to the bot** — its SQL
already queries the right columns.

---

## Issue 1 — Block is NULL for the 404 records with no village code

### What is wrong

404 source rows have no `mapped_village_lgd_code`. In the DB, all 404 load as
`entity_type = 'Unresolved'` with **`lgd_block = NULL`** — but the source file gives the block for
every one of them in `mapped_block_lgd_name` / `mapped_block_lgd_code`.

The block appears to be derived through the village (`geography_key` → `dim_geography`), so a
record with no village loses its block. The district is unaffected: it matches the source on all
2,822 loaded rows.

Verified on the live DB during testing: the set of rows with `lgd_block IS NULL` is exactly the set
of source rows with no village code (404 = 404, one-to-one), and no row that has a block in the DB
has a different block from the source.

### Impact

Every block-level figure undercounts. Across the 404 records: **₹49.89 Cr sanctioned and ₹27.81 Cr
disbursed** have no block. They span **46 blocks in all 12 districts**.

**By district** (records with no block in the DB):

| District | Records | Blocks affected | Disbursed (₹ Cr) |
|---|---:|---:|---:|
| West Khasi Hills | 77 | 5 | 3.19 |
| Ri Bhoi | 61 | 3 | 1.74 |
| West Garo Hills | 54 | 8 | 6.56 |
| East Garo Hills | 41 | 3 | 1.37 |
| North Garo Hills | 39 | 4 | 1.09 |
| West Jaintia Hills | 34 | 3 | 2.98 |
| East Khasi Hills | 29 | 7 | 4.03 |
| Eastern West Khasi Hills | 26 | 2 | 0.76 |
| South Garo Hills | 18 | 4 | 3.07 |
| South West Khasi Hills | 11 | 2 | 1.58 |
| South West Garo Hills | 8 | 3 | 0.88 |
| East Jaintia Hills | 6 | 2 | 0.55 |
| **Total** | **404** | **46** | **27.81** |

**Most affected blocks:** Nongstoin 59 records, Umsning 43, Samanda 28, Rongram 25 (₹2.28 Cr),
Thadlaskein 22 (₹1.89 Cr), Resubelpara 20, Mawthadraishan 16, Mylliem 15 (₹2.87 Cr).

**By scheme:** Piggery 120, Poultry 75, Agriculture Warehouse 58, Prime Agriculture Response Vehicle
42, Prime Tourism Vehicle 33, Sericulture spinning 28, Sericulture weaving 20, Any Business Venture
10, Dairy 9, Goat 5, Sports & Wellness 3, Common Facility Center 1.

### Failing test cases

| Test case | Question | DB / bot | Raw file |
|---|---|---|---|
| TC-29 | How many applications are recorded in Tikrikilla block? | 89 | 95 |
| TC-30 | What is the total disbursement for Tikrikilla block? | ₹1.47 Cr | ₹2.80 Cr |
| TC-33 | Applications in each block of West Garo Hills | Rongram 181, Tikrikilla 89, Demdema 40, Gambegre 39, Selsella 33, Dadenggiri 27, Dalu 23, Batabari 7, **no block 54** | Rongram 206, Tikrikilla 95, Demdema 45, Gambegre 40, Dadenggiri 38, Selsella 35, Dalu 25, Batabari 8 |

The 6 Tikrikilla records missing from the DB figure are source ids **11, 12, 13, 32, 81, 1415**. Most
are large Agriculture Warehouse records, which is why 6 records account for ₹1.34 Cr.

### Suggested fix

When loading `curated.fact_cm_elevate_disbursement`, populate the block for `Unresolved` records from
the source's `mapped_block_lgd_code` / `mapped_block_lgd_name`, instead of only through the village.
The view should then expose it in `lgd_block`.

Note: some source records sit in Municipal Board areas rather than C&RD blocks (e.g.
"Resubelpara-Municipal Board", 11 of the 404). Decide whether those should load as the Municipal
Board or its surrounding block.

### How to check

```sql
-- Before the fix: 404. After the fix: 0.
SELECT COUNT(*) AS records_without_block
FROM curated.v_cm_elevate_disbursement
WHERE lgd_block IS NULL;

-- After the fix: 95 records, ₹2.80 Cr.
SELECT COUNT(*) AS records, ROUND(SUM(total_disbursement) / 1e7, 2) AS total_disbursed_cr
FROM curated.v_cm_elevate_disbursement
WHERE lgd_block = 'TIKRIKILLA';
```

---

## Issue 2 — Source row `id 2392` is quarantined

### What is wrong

The loader held back one source row as `corrupted_identity`, so the DB has **2,822** rows against
the source's **2,823**. The row:

| Field | Source value |
|---|---|
| id / application number | 2392 / MSWCS000042 |
| Name fields | first "4", middle "For", last "All" → **"4 For All"** |
| Scheme | Meghalaya Sports & Wellness Scheme |
| District / block / village | East Khasi Hills / Mawpat / Mawdiang Diang |
| Financial year / loan entity | 2024-25 / Bank |
| Sanctioned | ₹3,00,00,000 (₹3.00 Cr) |
| Subsidy disbursed / loan disbursed | ₹70,00,000 / ₹1,10,00,000 |
| Total disbursement | ₹1,80,00,000 (₹1.80 Cr) |

The name "4 For All" looks like a real team or organisation name (8 of the 9 Sports & Wellness
records are group applications), not a corrupted identity. **Please confirm with the data owner
before reversing the quarantine.** Until then, Sports & Wellness totals — and every total that
includes East Khasi Hills, FY 2024-25 or Bank — are understated by this one record.

### Affected test cases

In each case the bot's answer equals the DB exactly; the difference is this one row.

| Test case | Figure | DB / bot | Raw file |
|---|---|---|---|
| TC-11 | Total records | 2,822 | 2,823 |
| TC-14 | Sanctioned records | 2,819 | 2,820 |
| TC-17 | Total disbursed | ₹81.10 Cr | ₹82.90 Cr |
| TC-18 | Sports & Wellness disbursed | ₹4.07 Cr | ₹5.87 Cr |
| TC-19 | Sports & Wellness sanctioned records | 8 | 9 |
| TC-21 | Bank records | 1,732 | 1,733 |
| TC-22 | Disbursed through Bank | ₹41.92 Cr | ₹43.72 Cr |
| TC-23 | FY 2024-25 records | 2,290 | 2,291 |
| TC-24 | FY 2024-25 disbursed | ₹58.02 Cr | ₹59.82 Cr |
| TC-25 | FY 2024-25 Sports & Wellness records | 8 | 9 |
| TC-26 | East Khasi Hills records | 240 | 241 |
| TC-27 | East Khasi Hills disbursed | ₹20.80 Cr | ₹22.60 Cr |
| TC-36 | East Khasi Hills: records / sanctioned / disbursed | 240 / 239 / ₹20.80 Cr | 241 / 240 / ₹22.60 Cr |

TC-20 and TC-28 also quote a count that differs by this row (e.g. Bank 1,732 vs 1,733), but what they
ask for — the loan-entity names and the top districts — is correct, so they are not counted as
failures.

### How to check

```sql
-- After loading the row: 2,823 records, ₹82.90 Cr disbursed, ₹142.74 Cr sanctioned.
SELECT COUNT(*) AS records,
       ROUND(SUM(total_disbursement) / 1e7, 2) AS total_disbursed_cr,
       ROUND(SUM(sanctioned_amount)  / 1e7, 2) AS sanctioned_cr
FROM curated.v_cm_elevate_disbursement;

SELECT * FROM curated.v_cm_elevate_disbursement WHERE source_row_id = 2392;
```

The loader's reconciliation view (`meta.v_reconciliation_cm_elevate_disbursement`, per the dataset
README) should then show 2,823 curated and 0 quarantined.

---

## After both fixes

Re-run the 36 use cases. All 16 affected cases should then match the raw file with no bot change,
taking the result to 36 of 36 against both the DB and the raw data. The bot team can re-run the test
harness on request.
