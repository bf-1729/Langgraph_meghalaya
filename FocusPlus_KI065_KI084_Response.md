# Focus+ — response to KI-065 and KI-084

**From:** the ingestion/database side (`megh-ingestion` project)

Checked both against the live database and, for KI-065, the official government village list
directly. Result: **neither is a database defect.**

---

## KI-065 — Dalu-area district mismatch: not a bug, database is correct

Reproduced your exact numbers: **175 rows, 82 distinct beneficiaries, ₹5,15,000**, across 15
distinct villages near Dalu, all disagreeing between the source file's district and our database's
district.

We checked every one of these 15 villages directly against `shared/data/geography.xlsx` — the
actual official government village roster, not our own table's opinion of itself:

| Villages | Roster says | Source file says |
|---|---|---|
| Dolbapara, Kotchu Adok, Monupara, Chongnapara, Gobindapara, Jarangpara, Dobokgre, Bhatua Gaon, Dobakura B (9 villages, all in Purakhasia block) | **South West Garo Hills** | West Garo Hills |
| Boldamgiri, Nogorpara, Tarapara, Rongramgre, Watregre, Nokatgre (6 villages, in Tikrikilla/Batabari/Rongram/Dalu blocks) | **West Garo Hills** | South West Garo Hills |

**All 15 matched our database exactly, zero exceptions.** This is the same district-pair confusion
we've now independently confirmed in three other schemes on this project (MGNREGA, Focus Legacy,
CM Elevate) — South West Garo Hills was carved out of West Garo Hills as a newer district, and a
number of source files across different government programs haven't caught up. Our database
reflects the current, correct boundary.

**Recommendation**: mark this as an expected, source-side difference. `district_name_raw` (already
in `curated.v_focus_plus`) preserves exactly what the source file said, for anyone who specifically
wants the "as per the spreadsheet" answer; `lgd_district` is the current, correct one.

## KI-084 — blank village codes and 21 code corrections: intentional, working as designed

**102,923 blank-code rows**: confirmed exactly. Of those, **57,643** had no other way to identify
the village (no name that matched anything) and correctly landed on an "Unresolved / Not Yet
Mapped" placeholder rather than being dropped. The remaining **45,280** were successfully matched
to a real village by name — either because the exact same village name appeared elsewhere in the
file with a real code (self-match), or because the district + village name combination uniquely
matched the official roster. This is the same six-tier geography resolution used identically by
every scheme in this project (MGNREGA, PMAY, CM Elevate, Focus Legacy, CM Disbursement) — not
something built specifically for this report, and already relied on for years of prior loads.

**21 code corrections**: checked every one. All 21 had the exact same root cause: the source gave
a code like `801536` for a place named `"Tura (M)"` — the `(M)` marking it as a **Municipality**
(a town), not a village. That code does not exist anywhere in the real village roster, because it
isn't a village. Rather than blindly trusting a code that points nowhere real, the loader correctly
refused it and fell back to the district-level "Unresolved" placeholder instead — the alternative
would have been silently attaching real payment data to a fabricated or wrong location.

**Answer to your question: yes, the name-based matching is intentional.** It's a deliberate,
long-standing design choice (documented in `shared/pipeline/common.py`), not something introduced
for this dataset, and it's the reason 45,280 rows have a real village today instead of sitting in
"Unresolved."

---

## Summary

| # | Verdict |
|---|---|
| KI-065 | Not a bug — database matches the official village roster on all 15 villages. Source file uses the older district boundary. |
| KI-084 | Not a bug — both figures (102,923 / 57,643+45,280, and 21 corrections) reproduce exactly and are the intended, documented behavior of the geography-matching logic used across the whole project. |

Nothing further needed from the database side on either of these.
