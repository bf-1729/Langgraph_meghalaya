"""
The SQL verifier must not veto geography filters that are plainly in the SQL.

Reported 2026-09-18 (second time for this question): "How many applicants are
there in BATABARI under Agro Tourism Villa Scheme, PRIME Small Enterprise
Empowerment and Development (SEED) and Meghalaya Poultry Farming Scheme,
BATABARI block, WEST GARO HILLS" came back as "I understood the question but
couldn't build a working query for it against the current CM Elevate data",
0 results, 0ms.

Nothing upstream was broken. Entity resolution produced
  {'district': 'WEST GARO HILLS', 'block': 'BATABARI', 'cm_scheme': [...3 schemes]}
and the generator emitted exactly the right query, filtering on
  AND lgd_block = 'BATABARI' AND lgd_district = 'WEST GARO HILLS'
The semantic verifier then returned, on 10 of 10 measured calls:

  "Check 2: RESOLVED ENTITIES block lists lgd_block = 'BATABARI' and
   lgd_district = 'WEST GARO HILLS', but the SQL WHERE clause omits these
   filters entirely. The SQL only filters on scheme_name"

— a plain factual error about text it was looking at. Each rejection fed a
repair prompt, the repair re-sent the same (correct) SQL, the 3-repair budget
ran out, and the question fell through to the KB fallback. The answer is a
plain 46: SEED 43 + Poultry 2 + Agro Tourism Villa 1.

This is the fifth member of the same family as _VERIFIER_FALSE_EMPTY_ENTITIES,
_verifier_complaint_is_cosmetic, _verifier_year_complaint_is_false and
_verifier_wants_suppressed_geography — all cases where the verifier contradicts
ground truth the pipeline established itself. The fourth one has the right
complaint shape here but correctly declines, because it requires a resolved
village_code and this question has none.

The guard (_verifier_missing_geo_is_false) discards a "filter is missing"
complaint ONLY when every resolved geography entity the complaint names is
provably present in the SQL. The negative cases below are the point of the
test: a genuinely absent or wrong filter must still raise, or Check 2 would
stop protecting anything.

Pure unit test over the guard — no model, no DB, no network. Plain script (no
pytest in the venv): `python tests/test_verifier_missing_geo.py`, exit 0 = pass.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from app import pipeline as p  # noqa: E402

_fails: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


# The complaint exactly as the verifier returned it, live.
REAL = ("Check 2: RESOLVED ENTITIES block lists lgd_block = 'BATABARI' and "
        "lgd_district = 'WEST GARO HILLS', but the SQL WHERE clause omits these "
        "filters entirely. The SQL only filters on scheme_name")
RESOLVED = {"district": "WEST GARO HILLS", "block": "BATABARI"}
GOOD_SQL = (
    "SELECT scheme_name,\n"
    "       COUNT(DISTINCT request_id) AS applicants\n"
    "FROM curated.v_cm_elevate\n"
    "WHERE scheme_name IN ('Agro Tourism Villa Scheme',\n"
    "                      'PRIME Small Enterprise Empowerment and Development (SEED)',\n"
    "                      'Meghalaya Poultry Farming Scheme')\n"
    "  AND lgd_block = 'BATABARI'\n"
    "  AND lgd_district = 'WEST GARO HILLS'\n"
    "GROUP BY scheme_name\n"
    "ORDER BY scheme_name\n"
    "LIMIT 100"
)

# ── 1. The reported false positive is discarded ────────────────────────────
print("1. THE BATABARI FALSE POSITIVE IS DISCARDED")
check("the live complaint is recognised as false",
      p._verifier_missing_geo_is_false(REAL, RESOLVED, GOOD_SQL))
check("  an UPPER() wrapper counts as filtering too",
      p._verifier_missing_geo_is_false(
          REAL, RESOLVED, GOOD_SQL.replace("lgd_block = 'BATABARI'",
                                           "UPPER(lgd_block) = 'BATABARI'")))
check("  so does membership in an IN (...) list",
      p._verifier_missing_geo_is_false(
          REAL, RESOLVED, GOOD_SQL.replace("lgd_block = 'BATABARI'",
                                           "lgd_block IN ('BATABARI','SIJU')")))

# ── 2. Real omissions must STILL be caught — the guard's safety margin ─────
print("2. A GENUINELY MISSING OR WRONG FILTER STILL RAISES")
check("a truly absent block filter is not discarded",
      not p._verifier_missing_geo_is_false(
          REAL, RESOLVED, GOOD_SQL.replace("  AND lgd_block = 'BATABARI'\n", "")))
check("a truly absent district filter is not discarded",
      not p._verifier_missing_geo_is_false(
          REAL, RESOLVED, GOOD_SQL.replace("  AND lgd_district = 'WEST GARO HILLS'\n", "")))
check("filtering the WRONG block value is not discarded",
      not p._verifier_missing_geo_is_false(
          REAL, RESOLVED, GOOD_SQL.replace("'BATABARI'", "'SIJU'")))
check("a complaint about something else is left alone",
      not p._verifier_missing_geo_is_false(
          "Check 1: PROHIBITED JOIN - the SQL joins v_cm_elevate to itself",
          RESOLVED, GOOD_SQL))
check("no resolved geography means the guard never fires",
      not p._verifier_missing_geo_is_false(REAL, {}, GOOD_SQL))
check("a complaint naming neither column nor value never fires",
      not p._verifier_missing_geo_is_false(
          "Check 3: the aggregation is missing a GROUP BY", RESOLVED, GOOD_SQL))

# ── 3. The guard is wired into _verify_sql ─────────────────────────────────
print("3. THE GUARD IS WIRED INTO THE VERIFIER PATH")
_SRC = (_ROOT / "app" / "pipeline.py").read_text(encoding="utf-8")
_verify = _SRC[_SRC.index("async def _verify_sql("):_SRC.index("async def execute_with_repair(")]
check("_verify_sql consults the new guard",
      "_verifier_missing_geo_is_false(issue, entity_result.get(\"resolved\") or {}, sql)" in _verify)
check("  and discards by returning None, like its four siblings",
      _verify.count("return None") >= 6, _verify.count("return None"))

# ── 4. The other four guards are untouched ─────────────────────────────────
print("4. THE EXISTING GUARDS ARE UNCHANGED")
for fn in ("_VERIFIER_FALSE_EMPTY_ENTITIES", "_verifier_complaint_is_cosmetic",
           "_verifier_year_complaint_is_false", "_verifier_wants_suppressed_geography"):
    check(f"{fn} still present", hasattr(p, fn.lstrip("_")) or fn in _SRC)
# The village guard must keep declining this question (no village_code resolved),
# which is why a fifth guard was needed rather than widening the fourth.
check("the village guard still declines a question with no village_code",
      not p._verifier_wants_suppressed_geography(REAL, RESOLVED, GOOD_SQL))

print()
if _fails:
    print(f"FAILED ({len(_fails)}): " + ", ".join(_fails))
    sys.exit(1)
print("ALL VERIFIER MISSING-GEO CHECKS PASSED")
