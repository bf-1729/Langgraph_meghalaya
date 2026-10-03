# MGNREGA — Database Issues Found in Use-Case Testing

**Date:** 2026-09-26
**Dataset:** MGNREGA — `curated.v_employment`, `curated.v_expenditure`
**Raw sources compared:** `MGNREGA_Combined - MGNREGA Employment.csv` (26,375 rows) and
`MGNREGA_Combined - MGNREGA Expenditure.csv` (18,818 rows), both in the repo root.
**Full test report:** `docs/MGNREGA_UseCase_Test_Report_2026-09-26.xlsx` (sheet "DB vs Raw").

## Summary

The DB loads every source row: 26,375 = 26,375 and 18,818 = 18,818. All 12 statewide measure
totals are identical between the DB and raw.

- **Expenditure:** at district × FY × measure grain, 336 of 336 cells match.
- **Employment:** 286 of 336 cells match. The 50 that differ are all caused by one village.

| # | Issue | Records affected | Test cases affected | Severity |
|---|---|---|---|---|
| 1 | GENAPARA (village_code 274268) is mapped to a different district and block than in the raw file | 4 employment rows (one per FY) | DATA-019, DATA-027, DATA-030 (small differences; bot figures match the DB) | Medium: West Garo Hills, South West Garo Hills, DALU and PURAKHASIA employment totals differ from the source |
| 1b | 15 more villages are counted under a different block than in the raw file (16 with GENAPARA); village figures themselves match exactly | 12 employment + 4 expenditure villages | Block totals for MAWSHYNRUT, SHALLANG, RAMBRAI, NONGSTOIN, PURAKHASIA, ZIKZAK, DALU, BATABARI, SELSELLA, CHOKPOT, SIJU, UMSNING, MAWHATI (68 of 448 block figures in the all-blocks run) | Medium: block totals differ from a raw-file total |
| 2 | `women_employment_provided` is 0 on every row for FY 2025-26 | 6,891 rows | Any FY 2025-26 women question | Info: identical in raw; source-side |
| 3 | `admin_total_exp` is 0 on every row except one (0.90 lakh) | 18,817 rows | DATA-013 | Info: identical in raw; the bot refuses this measure by design |

None of the 14 failed use cases is caused by the DB. All 14 are bot issues (KNOWN_ISSUES
KI-041 to KI-048), and all 14 were fixed the same day (v2 report: 30/30).

---

## Issue 1: GENAPARA mapped to DALU / West Garo Hills instead of PURAKHASIA / South West Garo Hills

| | Raw CSV | megh_db `curated.v_employment` |
|---|---|---|
| Village | GENAPARA (274268) | GENAPARA (274268) |
| Block | PURAKHASIA | DALU |
| District | SOUTH WEST GARO HILLS | WEST GARO HILLS |
| AC | DALU (2025-26); blank in the other 3 years | DALU (2025-26); NULL in the other 3 years |
| Match_Method | "Via shifted-village list" (2022-23 to 2024-25), "Direct - current LGD" (2025-26) | – |

**Effect:**
- In FY 2024-25, West Garo Hills persons employed is 56,774 in the DB against 56,733 in raw.
- Person-days in the same year are 2,808,417 in the DB against 2,806,403 in raw.

Every year and every employment measure shifts by GENAPARA's value, between the two districts
and between the two blocks.

**Reproduce (read-only):**

```sql
SELECT financial_year_short, lgd_block, lgd_district, persons_employed
FROM curated.v_employment WHERE village_code = 274268 ORDER BY 1;
```

**Ask:** confirm which geography is correct for village 274268. The raw file follows the
shifted-village list; the DB follows the current LGD, which may be intentional. If the DB is
right, record it as an expected difference.

## Issue 1b: Fifteen more villages sit in a different block than in the raw file (all-blocks QA)

Found by the all-blocks / all-villages run (2026-09-26, FY 2024-25; comparison over all years).
Every village's own figures match the raw file exactly (6,425 villages, 0 differences). Only the
**block** each village is counted under differs, so block totals for the blocks below differ from
a raw-file block total. The bot follows the DB. 68 of the 448 block-level expected figures were
affected; each one was accepted if it matched either the DB or the raw file.

| Fact | village_code | Village | Raw block (district) | DB block (district) |
|---|---|---|---|---|
| employment | 276539 | DOHDE MAWKOHRAM | MAWSHYNRUT (WKH) | SHALLANG (WKH) |
| employment | 276474 | JYNRUNIANGBRAK | MAWSHYNRUT (WKH) | SHALLANG (WKH) |
| employment | 276645 | MAWSHUT | MAWSHYNRUT (WKH) | SHALLANG (WKH) |
| employment | 276472 | RIANGSHIANG | MAWSHYNRUT (WKH) | SHALLANG (WKH) |
| employment | 276372 | NONGMAWSHUT | SHALLANG (WKH) | MAWSHYNRUT (WKH) |
| employment | 276612 | PYNDENGRATHAW | SHALLANG (WKH) | MAWSHYNRUT (WKH) |
| employment | 276411 | NONGTHYMMAI | SHALLANG (WKH) | MAWSHYNRUT (WKH) |
| employment | 276668 | DOLEDONGA | RAMBRAI (WKH) | NONGSTOIN (WKH) |
| employment | 276693 | SYNIA | RAMBRAI (WKH) | NONGSTOIN (WKH) |
| employment | 274268 | GENAPARA | PURAKHASIA (SWGH) | DALU (WGH) — Issue 1 |
| employment | 904742 | SAMTANGPARA | PURAKHASIA (SWGH) | ZIKZAK (SWGH) |
| employment | 272912 | TAIRONGGRE | BATABARI (WGH) | SELSELLA (WGH) |
| expenditure | 275575 | Dabalgre | CHOKPOT (SGH) | SIJU (SGH) |
| expenditure | 936507 | Krombaroh | UMSNING (Ri Bhoi) | MAWHATI (Ri Bhoi) |
| expenditure | 945386 | Umborloi | UMSNING (Ri Bhoi) | MAWHATI (Ri Bhoi) |
| expenditure | 276888 | Mawphanlur | NONGSTOIN (WKH) | RAMBRAI (WKH) |

**Ask:** confirm the correct block for each (the DB may follow the current LGD on purpose). If the
DB is right, these are expected differences from the raw file.

## Issue 2: Women employment is 0 for FY 2025-26

`SUM(women_employment_provided)` by FY:
- 2022-23: 309,225
- 2023-24: 314,863
- 2024-25: 210,328
- 2025-26: **0**

Raw has the same values. Until this is filled, the bot treats FY 2025-26 women employment as **not
recorded**, never as zero. It offers only FY 2022-23 to 2024-25 for women questions, and it
excludes FY 2025-26 from any all-years women figure (KNOWN_ISSUES KI-077, 2026-09-27). Once the
ingestion team fills the year, the bot picks it up automatically, because the year coverage is
read live.
**Ask:** is the 2025-26 column not yet populated at source?

## Issue 3: Administrative expenditure is not populated

`admin_total_exp` sums to 0.90 lakh statewide across all 4 years, from a single row. Raw has the
same. The bot deliberately refuses this measure (schema_context MGNREGA rule 10). Use case
DATA-013 expects a figure, so this needs a product decision (KNOWN_ISSUES KI-046).
