# Focus Plus — Database Issues Found in Use-Case Testing

**Date:** 2026-09-27
**Dataset:** Focus Plus — `curated.v_focus_plus`
**Raw source compared:** `Focus Plus Master.csv` (repo root, 385,671 rows; holds names, mobile numbers
and EPIC ids — only aggregates were computed).
**Full test report:** `docs/Focus_Plus_UseCase_Test_Report_2026-09-27.xlsx` (sheet "DB vs Raw").

## Summary

The DB loads every source row. Raw joined to the DB on `batch + source_sl_no + tranche` matches
385,671 of 385,671 rows. Everything below is identical between the DB and raw:
- the statewide totals: 385,671 payments, Rs 1,197,392,500 and 105,813 beneficiaries
  (`COUNT(DISTINCT beneficiary_key)`);
- every batch, tranche, FY, gender, occupation, focus status, verification status, bank and block
  (`block_name_raw`) group.

| # | Issue | Records affected | Test cases affected | Severity |
|---|---|---|---|---|
| 1 | 175 rows sit in a different district in the DB than in the raw file (Dalu ↔ South West Garo Hills) | 175 payment rows, 82 beneficiaries | FOCUS-001a, 002, 023, 025, 026a, 030 (WGH / SWGH figures). The bot's figures match the DB. | Medium: West Garo Hills and South West Garo Hills totals differ from the source |

None of the 11 round-1 failures was caused by the DB. They were all bot issues (KNOWN_ISSUES
KI-060 to KI-064), fixed the same day (30 / 30 after fixes). This issue is KI-065.

---

## Issue 1: Dalu block rows mapped to South West Garo Hills

| Raw district / block | DB `lgd_district` | DB `block_name_raw` | Rows | Villages (LGD code) |
|---|---|---|---:|---|
| West Garo Hills / Dalu | SOUTH WEST GARO HILLS | Dalu | 144 | Dolbapara and Kotchu Adok (124 rows, no village code); 274225, 274226, 274229, 274231, 274243, 274304, 274305, 274306, 274311 |
| South West Garo Hills / Betasing, Damalgre, Zikzak | WEST GARO HILLS | same as raw | 31 | 273457, 273460, 274336, 272856, 273314, 273407 |

- **Net effect in the DB:**
  - West Garo Hills has 113 fewer rows, 20 fewer beneficiaries and Rs 3,60,000 less;
  - South West Garo Hills gains the same.
- **Inconsistency inside the DB:** the moved Dalu rows keep `block_name_raw = 'Dalu'` under
  district SOUTH WEST GARO HILLS. A block total for Dalu is unaffected. The district totals are.
- `has_geo_conflict` is **False** on all 175 rows, so the view gives no signal that the district
  was re-mapped.
- This is the same Dalu / South West Garo Hills boundary pattern as MGNREGA's GENAPARA
  (`docs/MGNREGA_DB_Issues.md`, issue 1). It is probably one `dim_geography` roster decision.
  **INFERRED**, not confirmed with the ingestion team.

**Ask to the ingestion team (`megh-ingestion`):** confirm which district is authoritative for these
villages. If the roster re-map is intended, set `has_geo_conflict` on the moved rows so the
difference from the source is visible.

## Issue 2: village totals — the DB maps rows the raw file left uncoded (all-villages run, 2026-09-27)

- **What:** the raw file leaves `mapped_village_lgd_code` blank on 102,923 of 385,671 rows, and
  the DB gives many of them a village. Per village_code:
  - **417 real villages** hold more in the DB than the raw file's coded rows. For example,
    Burirjhar (272744) has 197 beneficiaries in the DB and 31 by raw code.
  - **104 villages** carry DB-only codes from 900000 up (Focus Plus villages without an LGD code).
  - **10 "Unresolved / Not Yet Mapped"** placeholders hold the rows that could not be mapped.
  - **3 raw codes** (801536, 936751, 936752) are not in the DB as codes.
- **Blocks are unaffected:** all 51 block totals are identical in the DB and raw.
- **Effect on the bot:** none wrong. It follows the DB, and the all-villages test accepts the DB
  or the raw figure.
- **Ask to the ingestion team:** confirm the name-based village mapping is intended, and publish
  how the 900000+ codes are assigned.

## Row-level correction package (2026-09-28)

`docs/Focus_Plus_Data_Corrections_for_Ingestion_2026-09-28.xlsx` (non-personal columns only):
- **README:** what is needed, and the decision each issue needs.
- **KI-065 summary / rows:** 175 rows, 82 beneficiaries, Rs 5,15,000 gross (the raw → DB district
  change in both directions; the net for West Garo Hills is -113 rows / -Rs 3,60,000).
- **KI-084 by village / rows:** the DB gave all 102,923 uncoded source rows a village_code. Of
  those, 57,643 went to the "Unresolved / Not Yet Mapped" placeholders. The DB also changed the
  code on 21 coded rows; 445 villages are affected.
- **Verification SQL:** read-only queries that reproduce every count, before and after a fix.

The chatbot team does not modify megh_db (CLAUDE.md §6), so these need the ingestion team.
