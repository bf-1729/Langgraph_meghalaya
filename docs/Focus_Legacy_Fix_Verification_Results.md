# Focus Legacy fix: verification results

**From:** chatbot side
**Re:** `Focus_Legacy_Fix_Verification.md`
**Run on:** 2026-09-25, against `megh_db`

**Verdict:** 8 of 9 test cases pass exactly. TC-F1 does not match its "Expect" line as written. Its five named blocks match exactly, but one `NULL` block row group remains. It looks like the same source-blank case you already describe for TC-F2, but your document says there should be no `NULL` block, so we're reporting it.

**How we ran it:** we don't have `megh_readonly` credentials on this machine. We connected as the app's `postgres` user and ran every query inside a `READ ONLY` transaction that was rolled back at the end, so nothing was written. Queries are exactly as given in your document.

## Summary

| ID | Expected | Actual | Result |
|---|---|---|---|
| TC-F1 | 5 blocks, no `NULL` block | 5 blocks match exactly, **plus a `NULL` block: 5 PGs, 6 rows, ₹2.25 lakh** | **Fail (as written)** |
| TC-F2 | 8 blocks as listed, plus 59 blank rows | Exact match | Pass |
| TC-F3 | 88 `NULL` blocks; 0 with a raw block but no `lgd_block` | 88; 0 | Pass |
| TC-F4 | 14,569 rows, 0 quarantined | 14,569 rows; 0 Focus Legacy rows in `staging.quarantine` | Pass |
| TC-F5 | 1 row, `PG-FOCUS-EGH-2117` | 1 row, `PG-FOCUS-EGH-2117` | Pass |
| TC-F6 | 1 row, `PG-FOCUS-EGH-2117`, `is_current = true` | Exact match | Pass |
| TC-F7 | 1 row, alias = current name, `is_current = true` | 1 row, `"Law ' Arliang Pg"` in both, `is_current = true` | Pass |
| TC-F8 | 0, no error | 0, no error | Pass |
| TC-F9 | 1,634 multi-spelling PGs; 0 rows; 0 rows | 1,634; 0 rows; 0 rows | Pass |

## Detail

### TC-F1: West Khasi Hills (Fail as written)

| lgd_block | producer_groups | rows | amount_lakh |
|---|---:|---:|---:|
| NONGSTOIN | 460 | 553 | 262.60 |
| MAWSHYNRUT | 286 | 348 | 142.30 |
| SHALLANG | 78 | 87 | 23.05 |
| RAMBRAI | 56 | 59 | 13.80 |
| RI MULIANG | 41 | 51 | 17.20 |
| **NULL** | **5** | **6** | **2.25** |

The five named blocks match your expected figures exactly, so the original issue (block lost for rows with no village match) is fixed for this district.

The extra `NULL` row group is the finding. These are the 6 rows:

| pg_id | block_name_raw | amount_disbursed |
|---|---|---:|
| PG-FOCUS-WKH-8324 | NULL | 80,000 |
| PG-FOCUS-WKH-10095 | NULL | 65,000 |
| PG-FOCUS-WKH-10095 | NULL | 5,000 |
| PG-FOCUS-WKH-16209 | NULL | 20,000 |
| PG-FOCUS-WKH-16211 | NULL | 30,000 |
| PG-LAMP-WKH-6358 | NULL | 25,000 |

All six have `block_name_raw` blank too, so they look like part of the 88 source-blank rows from TC-F3, not a loader bug. Please confirm that, and correct the TC-F1 expectation to include them. One of the six IDs uses a `PG-LAMP-` prefix instead of `PG-FOCUS-`. That may be worth a look.

### TC-F2: West Garo Hills (Pass)

| lgd_block | producer_groups | rows | amount_lakh |
|---|---:|---:|---:|
| DEMDEMA | 419 | 477 | 200.70 |
| DADENGGIRI | 407 | 459 | 90.80 |
| SELSELLA | 399 | 558 | 165.55 |
| RONGRAM | 393 | 561 | 196.45 |
| TIKRIKILLA | 370 | 425 | 109.70 |
| DALU | 306 | 419 | 133.90 |
| GAMBEGRE | 222 | 330 | 121.85 |
| BATABARI | 126 | 205 | 66.20 |
| NULL | 59 | 59 | 8.20 |

### TC-F3: Overall (Pass)

- `lgd_block IS NULL`: **88**
- `lgd_block IS NULL AND block_name_raw IS NOT NULL`: **0**
- We also checked: `lgd_block IS NULL AND block_name_raw IS NULL` gives **88**, so every remaining `NULL` block is blank in the source.

### TC-F4: Row count (Pass)

- `curated.fact_focus_legacy_disbursement`: **14,569** rows
- `staging.quarantine`: **0** rows where `source_dataset` or `target_table` mentions Focus / focus_legacy

### TC-F5 to TC-F8: Name search (Pass)

| Test | pg_id | current_name | matched_alias | is_current |
|---|---|---|---|---|
| F5 (old spelling) | PG-FOCUS-EGH-2117 | Bak-15 Banana Pg- 43/2020 ( Watenanggre) | Bak 15 Banana Watenanggre | false |
| F6 (current spelling) | PG-FOCUS-EGH-2117 | Bak-15 Banana Pg- 43/2020 ( Watenanggre) | Bak-15 Banana Pg- 43/2020 ( Watenanggre) | true |
| F7 (single spelling) | PG- FOCUS-EKH-12359 | Law ' Arliang Pg | Law ' Arliang Pg | true |
| F8 (never existed) | — | — | count = 0 | — |

### TC-F9: Alias integrity (Pass)

- PGs with more than one alias: **1,634**
- PGs without exactly one `is_current` alias: **0 rows**
- PGs with no alias at all: **0 rows**

## About your caveat (chatbot side)

Confirmed: the chatbot does **not** use the new view yet. No chatbot code refers to `curated.v_focus_legacy_pg_search` or `dim_producer_group_name_alias`. The database now finds old spellings, but the chatbot won't until we update its PG name lookup to use the view. That work is ours.
