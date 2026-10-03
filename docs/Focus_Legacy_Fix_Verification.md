# Focus Legacy — both reported issues are fixed. Please re-verify.

**From:** the ingestion/database side (`megh-ingestion` project)
**Re:** `Focus_Legacy_DB_Issues.md` (your report, 2026-09-25)

Both issues you found are fixed. The fixes are applied to the live database and were proven with a full real reload, not just a patch. Details are below. Please re-run your own use cases and the extra test cases below yourself: we only count a fix as done once someone outside the fix confirms it.

All queries below are read-only and safe to run directly against `megh_readonly` (`DATABASE_URL_READONLY`, database `megh_db`).

---

## Issue 1 — Missing block for 1,013 rows: FIXED

**What changed:** rows with no village match now take their block from the source's own `mapped_block_lgd_code` instead of losing it. This is built into the loader permanently (`load_focus_legacy.py`), not a one-time patch. We proved it by running a real, full `--force` reload after the fix and confirming the result was identical.

### TC-F1 — Re-run your TC-26 (West Khasi Hills, the case you found)

```sql
SELECT lgd_block, count(DISTINCT pg_id) AS producer_groups, count(*) AS rows,
       sum(amount_disbursed)/100000.0 AS amount_lakh
FROM curated.v_focus_legacy
WHERE lgd_district = 'WEST KHASI HILLS'
GROUP BY lgd_block ORDER BY producer_groups DESC;
```

**Expect:** these producer-group counts, matching your report's raw-file figures exactly, with no `NULL` block left:

| Block | Producer groups |
|---|---:|
| Nongstoin | 460 |
| Mawshynrut | 286 |
| Shallang | 78 |
| Rambrai | 56 |
| Ri Muliang | 41 |

### TC-F2 — A district your report didn't check (West Garo Hills, one of the most affected)

```sql
SELECT lgd_block, count(DISTINCT pg_id) AS producer_groups, count(*) AS rows,
       sum(amount_disbursed)/100000.0 AS amount_lakh
FROM curated.v_focus_legacy
WHERE lgd_district = 'WEST GARO HILLS'
GROUP BY lgd_block ORDER BY producer_groups DESC;
```

**Expect** (verified live 2026-09-25):

| Block | Producer groups |
|---|---:|
| Demdema | 419 |
| Dadenggiri | 407 |
| Selsella | 399 |
| Rongram | 393 |
| Tikrikilla | 370 |
| Dalu | 306 |
| Gambegre | 222 |
| Batabari | 126 |

The query also returns **59 rows with a blank block**. These are blank in the source file too (see TC-F3).

### TC-F3 — Overall count should now match your report's "after the fix" number

```sql
-- Expect: 88 (was 1,101 before the fix)
SELECT count(*) FROM curated.v_focus_legacy WHERE lgd_block IS NULL;

-- Expect: 0 -- every row that HAS block_name_raw now also has lgd_block
SELECT count(*) FROM curated.v_focus_legacy
WHERE lgd_block IS NULL AND block_name_raw IS NOT NULL;
```

### TC-F4 — Nothing else changed

```sql
-- Expect: 14569 rows, 0 quarantined (unchanged from before the fix)
SELECT count(*) FROM curated.fact_focus_legacy_disbursement;
```

---

## Issue 2 — Old PG name spellings could not be searched: FIXED

**What changed:** a new table, `curated.dim_producer_group_name_alias`, keeps every spelling a PG's name has ever appeared under, not just the current one. A new search view, `curated.v_focus_legacy_pg_search`, sits on top of it. The client chose this approach: Option 2 from your report (full name history), rather than Option 1 (leave it as is) or Option 3 (one extra copy per row).

### TC-F5 — Re-run your own example (the name your report couldn't find)

```sql
SELECT pg_id, current_name, matched_alias
FROM curated.v_focus_legacy_pg_search
WHERE matched_alias ILIKE 'Bak 15 Banana Watenanggre';
```

**Expect:** one row, `pg_id = PG-FOCUS-EGH-2117`.

### TC-F6 — Regression: searching by the current (already correct) name still works

This checks that the fix didn't break the case that already worked.

```sql
SELECT pg_id, current_name, matched_alias, is_current
FROM curated.v_focus_legacy_pg_search
WHERE matched_alias ILIKE 'Bak-15 Banana Pg- 43/2020 ( Watenanggre)';
```

**Expect:** one row, `pg_id = PG-FOCUS-EGH-2117`, `is_current = true`.

### TC-F7 — A PG that only ever had one spelling

Most PGs are like this. This checks that the fix didn't complicate the simple, common case.

```sql
SELECT pg_id, current_name, matched_alias, is_current
FROM curated.v_focus_legacy_pg_search
WHERE pg_id = 'PG- FOCUS-EKH-12359';
```

**Expect:** exactly one row, `matched_alias = current_name`, `is_current = true`.

### TC-F8 — A name that never existed (should return nothing, not an error)

```sql
SELECT count(*) FROM curated.v_focus_legacy_pg_search
WHERE matched_alias ILIKE 'This Name Was Never In The File';
```

**Expect:** `0`, with no error.

### TC-F9 — Integrity across all PGs, not just one example

```sql
-- Expect: 1634 (exact match to your report's count)
SELECT count(*) FROM (
  SELECT producer_group_key FROM curated.dim_producer_group_name_alias
  GROUP BY producer_group_key HAVING count(*) > 1
) x;

-- Expect: 0 rows -- every PG has EXACTLY one is_current=TRUE alias, never zero or two+
SELECT producer_group_key, count(*) FILTER (WHERE is_current) c
FROM curated.dim_producer_group_name_alias
GROUP BY producer_group_key HAVING count(*) FILTER (WHERE is_current) <> 1;

-- Expect: 0 rows -- every PG has at least one alias (none fell through the cracks)
SELECT dp.pg_id FROM curated.dim_producer_group dp
WHERE NOT EXISTS (
  SELECT 1 FROM curated.dim_producer_group_name_alias a
  WHERE a.producer_group_key = dp.producer_group_key
);
```

---

## Test case summary

| ID | Issue | What it checks | Expected result |
|---|---|---|---|
| TC-F1 | 1 | West Khasi Hills blocks (your TC-26) | 5 blocks, no `NULL` block |
| TC-F2 | 1 | West Garo Hills blocks (not in your report) | 8 blocks, plus 59 rows blank in source |
| TC-F3 | 1 | Total rows with a `NULL` block | 88 (was 1,101); 0 rows with a raw block but no `lgd_block` |
| TC-F4 | 1 | Fact table row count unchanged | 14,569 rows, 0 quarantined |
| TC-F5 | 2 | Old spelling can be found | 1 row, `PG-FOCUS-EGH-2117` |
| TC-F6 | 2 | Current spelling still found | 1 row, `is_current = true` |
| TC-F7 | 2 | PG with a single spelling | 1 row, alias = current name |
| TC-F8 | 2 | Name that never existed | 0, no error |
| TC-F9 | 2 | Alias table integrity | 1,634 multi-spelling PGs; 0 bad `is_current`; 0 PGs without an alias |

---

## One caveat (not a database issue)

The new table makes every past spelling of every name **findable in the database**. That alone does not mean the chatbot's query generation actually searches `curated.v_focus_legacy_pg_search` when someone asks a name-based question. It may still check only `dim_producer_group.current_name`. That is a decision and implementation detail on the chatbot side and is worth checking: the database half of this fix is complete, but both halves need to match.

If anything above doesn't reproduce exactly as stated, that is a real finding. Please report it back the same way you reported the original issues.
