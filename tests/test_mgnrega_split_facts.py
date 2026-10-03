"""
A question wanting one measure from EACH MGNREGA fact ("compare expenditure and
person-days in RERAPARA for FY 2023-24") must be answered — and answered with
the real numbers, not a fanned-out multiple of them.

Regression for the 2026-09-17 report, which uncovered three separate faults on
one question:

  1. NO ANSWER. person_days lives on curated.v_employment, total_exp on
     curated.v_expenditure, and the two facts share no measures. The generator
     selected both from v_expenditure, Postgres rejected the column, and the
     generic missing-column hint ("select from an object that exposes every
     column") could not steer the repair because at BLOCK grain no such object
     exists — v_district_year_summary is district x year only. The question
     burned its repair budget and fell through to the KB fallback.

  2. FAN-OUT. Once the prompt named both views, the generator JOINed them
     directly. Both are at source-row grain, so every expenditure row paired
     with every employment row: ₹367,647 lakh / 109,448,220 person-days against
     a truth of ₹2,450.98 lakh / 986,020. Worse than fault 1 — it runs clean and
     the numbers look plausible.

  3. VERIFIER FALSE POSITIVE. With the correct SQL finally produced, the
     semantic verifier rejected it on 5 of 5 calls, claiming "the question asks
     for FY 2023-24 but the SQL filters on year_key = 2023" — a financial year
     IS stored by its start year, so those are the same thing.

Pure-Python assertions — no model or DB. Plain script (no pytest in the venv):
`python tests/test_mgnrega_split_facts.py`, exit code 0 = all pass.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pipeline as p  # noqa: E402
from app import schema_context  # noqa: E402

_fails: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


_FILTERS = "WHERE lgd_block = 'RERAPARA' AND year_key = 2023"
_DIRECT_JOIN = (
    "SELECT SUM(e.total_exp) AS total_exp_lakh, SUM(emp.person_days) AS person_days "
    "FROM curated.v_expenditure e JOIN curated.v_employment emp "
    "ON e.year_key = emp.year_key AND e.lgd_block = emp.lgd_block "
    "WHERE e.lgd_block = 'RERAPARA' AND e.year_key = 2023"
)
_SUBQUERIES = (
    "SELECT exp.total_exp_lakh, emp.person_days FROM "
    f"(SELECT SUM(total_exp) AS total_exp_lakh FROM curated.v_expenditure {_FILTERS}) exp, "
    f"(SELECT SUM(person_days) AS person_days FROM curated.v_employment {_FILTERS}) emp"
)
_CTES = (
    f"WITH emp AS (SELECT SUM(person_days) AS pd FROM curated.v_employment {_FILTERS}), "
    f"exp AS (SELECT SUM(total_exp) AS te FROM curated.v_expenditure {_FILTERS}) "
    "SELECT exp.te, emp.pd FROM emp, exp"
)

# ── 1. The fan-out guard ───────────────────────────────────────────────────
print("1. A DIRECT JOIN OF THE TWO FACTS IS BLOCKED")
check("the reported join is flagged", p._mgnrega_facts_joined(_DIRECT_JOIN))
check("  an unaggregated subquery join is flagged too",
      p._mgnrega_facts_joined(
          "SELECT * FROM (SELECT total_exp FROM curated.v_expenditure) e "
          "JOIN (SELECT person_days FROM curated.v_employment) m ON 1=1"))

print("2. THE SAFE SHAPES ARE NOT")
check("inline aggregated subqueries pass", not p._mgnrega_facts_joined(_SUBQUERIES))
check("a CTE per fact passes", not p._mgnrega_facts_joined(_CTES))
check("joining a fact to dim_geography passes",
      not p._mgnrega_facts_joined(
          "SELECT SUM(person_days) FROM curated.v_employment "
          "JOIN curated.dim_geography USING (geography_key) WHERE lgd_block = 'RERAPARA'"))
check("a single-view query passes",
      not p._mgnrega_facts_joined(f"SELECT SUM(total_exp) FROM curated.v_expenditure {_FILTERS}"))

# ── 3. The repair hint explains the split ──────────────────────────────────
print("3. THE MISSING-COLUMN HINT NAMES THE RIGHT FACT")
_pd = p._missing_column_hint('column "person_days" does not exist')
check("person_days is sent to v_employment",
      "curated.v_employment" in _pd and "NOT on curated.v_expenditure" in _pd, _pd[:90])
_te = p._missing_column_hint('column "total_exp" does not exist')
check("total_exp is sent to v_expenditure",
      "curated.v_expenditure" in _te and "NOT on curated.v_employment" in _te, _te[:90])
check("  and the hint gives the two-CTE recipe", "WITH emp AS" in _pd)
check("  and warns that the summary view is district x year only",
      "district" in _pd.lower() and "v_district_year_summary" in _pd)
check("an unrelated missing column keeps the generic hint",
      "v_employment" not in (p._missing_column_hint('column "foo" does not exist') or ""))

# ── 4. The prompt rule states the split up front ───────────────────────────
print("4. THE SQL PROMPT STATES THE SPLIT")
_rules = schema_context._MGNREGA_RULES
check("it says the facts share no measures", "share NO measures" in _rules)
check("  and gives the block/village CTE recipe",
      "BLOCK or VILLAGE grain" in _rules and "WITH emp AS" in _rules)
check("  and still forbids the direct join", "NEVER join" in _rules)

# ── 5. The financial-year verifier guard ───────────────────────────────────
# FY 2023-24 IS year_key = 2023. The verifier reads the "-24" half as the value
# it expects and rejects correct SQL.
print("5. A FALSE YEAR COMPLAINT IS DISCARDED")
_resolved = {"block": "RERAPARA", "year_key": 2023}
_year_issue = ("Check 1: The question asks for FY 2023-24, but the SQL filters on "
               "year_key = 2023. The resolved year_key is wrong.")
check("correct SQL + a year complaint is discarded",
      p._verifier_year_complaint_is_false(_year_issue, _resolved, _SUBQUERIES))
check("  but a genuinely wrong year still raises",
      not p._verifier_year_complaint_is_false(
          _year_issue, _resolved,
          "SELECT 1 FROM curated.v_expenditure WHERE year_key = 2024"))
check("  and a non-year complaint is never discarded",
      not p._verifier_year_complaint_is_false(
          "Check 2: the query counts houses, not person-days.", _resolved, _SUBQUERIES))
check("  and nothing is discarded when no year was resolved",
      not p._verifier_year_complaint_is_false(_year_issue, {"block": "RERAPARA"}, _SUBQUERIES))

print()
if _fails:
    print(f"FAILED ({len(_fails)}): " + ", ".join(_fails))
    sys.exit(1)
print("ALL MGNREGA SPLIT-FACT CHECKS PASSED")
