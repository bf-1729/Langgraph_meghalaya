"""
An empty result answers in one line, and a misspelled place never causes one.

Reported 2026-09-18 on "How many applicants are there in tikrikulla under
Piggery Scheme, PRIME Small Enterprise Empowerment and Development (SEED) and
Meghalaya Poultry Farming Scheme for all of Meghalaya". Two faults:

1. PRESENTATION — the "no matching records" reply appended the whole
   capability catalogue ("Data I do have here:" + ~10 metric lines for the
   scheme). A one-line fact was buried under a dump the user did not ask for.
   An empty result answers the question that WAS asked; what else the dataset
   could report is a different question, and the NEXT STEPS chips already
   offer it.

2. CORRECTNESS, and the reason there was no data at all — "tikrikulla" is a
   misspelling of the TIKRIKILLA block, which holds 243 applicants for those
   three schemes (SEED 214 + Piggery 21 + Poultry 8). The extractor dropped
   the name, and the admin-level gate's raw-text scan matched names VERBATIM
   (_canonical_in_question), so the typo was invisible to it. The generator
   passed the user's own spelling into `lgd_block = 'TIKRIKULLA'` and the
   query returned a confident zero.

   resolve_dimension() and collides_across_dimensions() both handle that typo
   through their fuzzy stage — only the scan feeding them was exact-only. The
   gate now falls back to the place-phrase candidates ("in <name>") and lets
   the resolver judge them, so the question pauses and asks instead of
   answering zero. Whether it pauses is still decided by
   collides_across_dimensions, which needs 2+ readings.

Note the message must still NAME its scope: the original reply said "for
<three schemes>" and never mentioned tikrikulla — the very filter that emptied
the result — which is misleading, not merely verbose.

Text/catalogue level — no model, no DB, no network. Plain script (no pytest in
the venv): `python tests/test_no_data_answer.py`, exit 0 = pass.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from app import pipeline as p  # noqa: E402
from app.entity_resolver import (  # noqa: E402
    collides_across_dimensions,
    load_all,
    resolve_dimension,
)

load_all()

_fails: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


# ── 1. The empty-result reply is one crisp line ────────────────────────────
print("1. AN EMPTY RESULT ANSWERS IN ONE LINE")
_msg = p._no_data_answer(["CM Elevate"],
                         {"block": "TIKRIKILLA", "cm_scheme": "Meghalaya Dairy Development Scheme"})
print(f"      -> {_msg}")
check("it is a single line", "\n" not in _msg.strip(), _msg)
check("  the capability dump is gone", "Data I do have here" not in _msg)
check("  no metric lines leak in", "COUNT(*)" not in _msg and "applications by" not in _msg)
check("it still names the scope that emptied the result",
      "TIKRIKILLA" in _msg and "Meghalaya Dairy Development Scheme" in _msg, _msg)
check("it still says plainly that nothing matched",
      "couldn't find any matching records" in _msg, _msg)

_bare = p._no_data_answer(["CM Elevate"], {})
check("with no scope it degrades to the bare sentence",
      _bare.strip() == "I couldn't find any matching records in the data available.", _bare)
check("  and is still one line", "\n" not in _bare.strip())

# available_metrics_text has other callers (the capability/edge replies) and
# must survive — this fix removed one CALL, not the function.
from app.schema_context import available_metrics_text  # noqa: E402
check("available_metrics_text still works for its other callers",
      bool(available_metrics_text(["CM Elevate"])))

# ── 2. The misspelling that caused the empty result is caught ──────────────
print("2. A MISSPELLED PLACE IS RESOLVED, NOT SILENTLY ZEROED")
check("the resolver reads 'tikrikulla' as the TIKRIKILLA block",
      resolve_dimension("tikrikulla", "CM Elevate", "block").canonical == "TIKRIKILLA")
_dims = collides_across_dimensions("tikrikulla", "CM Elevate")
check("  and sees it as level-ambiguous (block + constituency)",
      set(_dims) >= {"block", "assembly_constituency"}, _dims)
check("the verbatim scan alone does NOT find the typo (why the fallback exists)",
      p._canonical_in_question(
          "applicants in tikrikulla under Piggery Scheme", "CM Elevate", "block") is None)

_Q = ("How many applicants are there in tikrikulla under Piggery Scheme "
      ", PRIME Small Enterprise Empowerment and Development (SEED) "
      "and Meghalaya Poultry Farming Scheme for all of Meghalaya")
_cands = p._village_scan_candidates(_Q, "CM Elevate")
check("the place-phrase scan offers 'tikrikulla' as a candidate",
      any("tikrikulla" in c.lower() for c in _cands), _cands)

_SRC = (_ROOT / "app" / "pipeline.py").read_text(encoding="utf-8")
check("the gate falls back to fuzzy candidates when the verbatim scan misses",
      "_village_scan_candidates(question, _scheme0)" in _SRC)
check("  and only when nothing was found verbatim",
      "if not _found:" in _SRC)

# ── 3. The fallback must not manufacture ambiguity ─────────────────────────
print("3. ORDINARY QUESTIONS GAIN NO NEW CANDIDATES")
for q, scheme in (("How many CM Elevate applications are on hold?", "CM Elevate"),
                  ("Top 5 CM Elevate schemes by applications", "CM Elevate"),
                  ("total houses sanctioned in East Khasi Hills", "PMAY-G")):
    _c = p._village_scan_candidates(q, scheme)
    # A district name is excluded by _village_scan_candidates itself (it skips
    # names a district/block/AC already owns), so these stay empty or harmless.
    _amb = [x for x in _c
            if len(collides_across_dimensions(x, scheme)) >= 2]
    check(f"{q[:46]!r} gains no ambiguous candidate", not _amb, _amb)

print()
if _fails:
    print(f"FAILED ({len(_fails)}): " + ", ".join(_fails))
    sys.exit(1)
print("ALL NO-DATA / TYPO CHECKS PASSED")
