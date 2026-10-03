"""
A constituency-wide question must return EVERY row, and must not have a
geography filter invented for it.

Two failures from the same 2026-09-17 question — "which villages in Rangsakona
received employment in MGNREGA for FY 2025-26":

  1. FALSE ZERO. Only the constituency was resolved, but the generated SQL also
     filtered `lgd_block = 'RANGSAKONA'`. An assembly constituency cuts ACROSS
     blocks (RANGSAKONA spans BETASING, RERAPARA and RONGRAM), so there is no
     block of that name and the two conditions can never both hold. The query
     ran clean and reported "no matching records" — the real answer is 146
     villages.

  2. TRUNCATED RESULT. Once answered, only 100 of the 146 rows reached the
     table: the API capped `data` at 200 and the UI sliced it to 100, with no
     way to see the rest even when the user asked for all of them.

Pure-Python assertions — no model or DB. Plain script (no pytest in the venv):
`python tests/test_ac_full_results.py`, exit code 0 = all pass.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pipeline as p  # noqa: E402
from app import prompt_builder  # noqa: E402

_fails: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


_AC_ONLY = {"resolved": {"assembly_constituency": "RANGSAKONA", "year_key": 2025}}
_AC_AND_BLOCK = {"resolved": {"assembly_constituency": "MAWLAI", "block": "MAWLAI"}}
_NO_AC = {"resolved": {"district": "WEST GARO HILLS"}}

# The exact SQL the generator produced for the reported question.
_REPORTED_SQL = (
    "SELECT DISTINCT dg.village_code FROM curated.v_employment ve "
    "JOIN curated.dim_geography dg ON ve.geography_key = dg.geography_key "
    "WHERE ve.year_key = 2025 AND UPPER(dg.lgd_block) = UPPER('RANGSAKONA') "
    "AND UPPER(ve.assembly_constituency_name) = UPPER('RANGSAKONA')"
)

# ── 1. The invented-geography guard ────────────────────────────────────────
print("1. INVENTED GEOGRAPHY FILTER IS CAUGHT")
check("the reported SQL is flagged",
      p._ac_with_invented_geo_filter(_AC_ONLY, _REPORTED_SQL) == "lgd_block = 'RANGSAKONA'",
      p._ac_with_invented_geo_filter(_AC_ONLY, _REPORTED_SQL))
check("  a bare (unaliased) invented district filter is flagged too",
      p._ac_with_invented_geo_filter(
          _AC_ONLY,
          "SELECT 1 FROM curated.v_employment WHERE assembly_constituency_name = 'RANGSAKONA' "
          "AND lgd_district = 'WEST GARO HILLS'") is not None)

print("2. AND NOTHING ELSE IS")
check("clean AC-only SQL passes",
      p._ac_with_invented_geo_filter(
          _AC_ONLY,
          "SELECT DISTINCT village_code FROM curated.v_employment WHERE year_key = 2025 "
          "AND UPPER(assembly_constituency_name) = UPPER('RANGSAKONA')") is None)
check("a genuine AC+block drill-down passes (the block WAS resolved)",
      p._ac_with_invented_geo_filter(
          _AC_AND_BLOCK,
          "SELECT SUM(persons_employed) FROM curated.v_employment "
          "WHERE UPPER(assembly_constituency_name) = UPPER('MAWLAI') "
          "AND lgd_block = 'MAWLAI'") is None)
check("a query with no constituency at all passes",
      p._ac_with_invented_geo_filter(
          _NO_AC,
          "SELECT SUM(total_exp) FROM curated.v_expenditure "
          "WHERE lgd_district = 'WEST GARO HILLS'") is None)

# ── 3. The prompt warns about it up front ──────────────────────────────────
print("3. THE PROMPT WARNS WHEN ONLY A CONSTITUENCY IS RESOLVED")
_blk = prompt_builder._entities_block(_AC_ONLY)
check("it says to filter on the constituency only", "CONSTITUENCY ONLY" in _blk, _blk[-200:])
check("  and names the false-zero risk", "false zero" in _blk.lower())
check("a resolved AC+block does NOT get that warning (both are real)",
      "CONSTITUENCY ONLY" not in prompt_builder._entities_block(_AC_AND_BLOCK))
check("  it gets the BOTH REQUIRED pairing instead",
      "BOTH REQUIRED" in prompt_builder._entities_block(_AC_AND_BLOCK))

# ── 4. Full results reach the client ───────────────────────────────────────
# The API must hand back every row it read, and the UI must render every row it
# is handed. run_readonly() already bounds the query itself at
# SQL_MAX_RESULT_ROWS, so neither layer needs a second cap of its own.
print("4. NO SILENT ROW TRUNCATION")
_src = (Path(__file__).resolve().parents[1] / "app" / "pipeline.py").read_text(encoding="utf-8")
check("the API returns the full result, not data[:200]",
      '"data": rows,' in _src and '"data": rows[:200],' not in _src)

_ui = (Path(__file__).resolve().parents[1] / "web" / "ai_query.html").read_text(encoding="utf-8")
check("the table renders every row, not data.slice(0, 100)",
      "const shown = data;" in _ui and not re.search(r"const shown = data\.slice\(0,\s*100\)", _ui))
check("  and still reports a trim if one ever happens",
      "Showing ${shown.length} of ${data.length} rows." in _ui)

print()
if _fails:
    print(f"FAILED ({len(_fails)}): " + ", ".join(_fails))
    sys.exit(1)
print("ALL AC / FULL-RESULT CHECKS PASSED")
