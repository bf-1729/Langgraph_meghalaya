"""
"Which scheme paid out the most?" — a superlative asked ACROSS schemes must be
ANSWERED (the scheme is the answer), never turned into a "which scheme does
your question concern?" pause.

Regression for the 2026-09-17 report: "what about the scheme with highest money
paid?" raised the four-way scheme-clarification chips. That pause exists for a
question that forgot to name a scheme it needs as a FILTER — but here the
scheme is the thing being asked for, so asking the user to supply it hands back
the very answer they came for.

_EXPLICIT_BOTH already exempts the phrasings that ask for every scheme ("both
schemes", "by scheme", "scheme-wise"); this shape carries its cross-scheme
intent through "which scheme" + a ranking word instead, which nothing matched.

Pure regex / pure-Python assertions — no model or DB. Plain script (no pytest
in the venv): `python tests/test_cross_scheme_money.py`, exit code 0 = all pass.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pipeline as p  # noqa: E402

_fails: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


# ── 1. The reported question ───────────────────────────────────────────────
print("1. REPORTED QUESTION")
REPORTED = "what about the scheme with highest money paid?"
check("it is recognised as a cross-scheme money ranking",
      p._wants_cross_scheme_money_ranking(REPORTED))
check("  and no longer raises the 'which scheme?' pause",
      not p._needs_scheme_clarification(REPORTED))

# ── 2. Other phrasings of the same question ────────────────────────────────
print("2. OTHER PHRASINGS")
for q in ("which scheme has the highest expenditure?",
          "which scheme spent the most?",
          "what scheme paid out the most money",
          "which scheme has the lowest spend",
          "rank the schemes by amount spent",
          "the scheme with the largest disbursement"):
    check(f"{q[:46]!r} -> ranking", p._wants_cross_scheme_money_ranking(q))
    check("  and no scheme pause", not p._needs_scheme_clarification(q))

# ── 3. Superlatives that are NOT about money stay on the normal path ───────
# "which scheme has the most applications" is a different figure (CM Elevate
# application counts), answered by ordinary SQL generation — the deterministic
# money ranking must not intercept it.
print("3. NON-MONEY SUPERLATIVES ARE LEFT ALONE")
for q in ("which scheme has the most applications?",
          "which scheme has the most beneficiaries?",
          "which scheme covers the most villages?"):
    check(f"{q[:46]!r} -> not the money ranking",
          not p._wants_cross_scheme_money_ranking(q))

# ── 4. Ordinary questions are untouched ────────────────────────────────────
print("4. ORDINARY QUESTIONS UNTOUCHED")
for q in ("total MGNREGA expenditure in West Garo Hills",
          "how many houses completed by district",
          "PMAY-G houses sanctioned in 2023-24",
          "compare MGNREGA and PMAY-G spending",
          "which district spent the most?"):
    check(f"{q[:46]!r} -> not the money ranking",
          not p._wants_cross_scheme_money_ranking(q))

# A question that names its own scheme never needed the pause anyway.
check("a scheme-named question still skips the pause",
      not p._needs_scheme_clarification("total MGNREGA expenditure in West Garo Hills"))

# ── 5. The SQL covers every scheme that records money ──────────────────────
# CM Elevate is deliberately ABSENT — v_cm_elevate has no money column at all
# (applications only), so including it would report a misleading zero.
print("5. THE RANKING SQL")
_sql = p._CROSS_SCHEME_MONEY_SQL
check("uses the sanctioned cross-scheme money view",
      "curated.v_cross_scheme_money_district_year" in _sql)
check("includes Focus Plus, whose money lives on its own view",
      "curated.v_focus_plus" in _sql)
check("converts Focus Plus rupees to crore", "/ 1e7" in _sql)
check("carries measure_semantics (required for any cross-scheme money answer)",
      "measure_semantics" in _sql)
check("orders by amount so the top scheme is first", "ORDER BY amount_crore DESC" in _sql)
# Word-bounded: curated.v_cm_elevate_disbursement (CM Elevate Legacy, which
# DOES record money) contains the applications view's name as a prefix.
check("does NOT read CM Elevate (it records no money)",
      re.search(r"\bv_cm_elevate\b", _sql) is None)
check("includes CM Elevate Legacy, whose subsidy + loan lives on its own view",
      "curated.v_cm_elevate_disbursement" in _sql and "total_disbursement" in _sql)
check("is a read-only single statement",
      _sql.lstrip().upper().startswith("SELECT") and ";" not in _sql)

print()
if _fails:
    print(f"FAILED ({len(_fails)}): " + ", ".join(_fails))
    sys.exit(1)
print("ALL CROSS-SCHEME MONEY CHECKS PASSED")
