"""
The admin-level clarification must fire for every scheme, not just MGNREGA.

Reported 2026-09-18: "How many applicants are there in mylliem under Green Taxi
CM Elevate and ware house Scheme for all of Meghalaya" answered a confident
**0** — the SQL had filtered `lgd_village_name = 'MYLLIEM'`. MYLLIEM is really a
C&RD block (and an assembly constituency) holding 2 applicants for those two
schemes. Asked under MGNREGA the same name pauses and asks which level is
meant; under CM Elevate it did not.

Three faults stacked, each hiding the next:

1. collides_across_dimensions() probed ONLY the asking scheme's catalogue.
   Of the four schemes just MGNREGA carries an assembly_constituency catalogue
   at all — CM Elevate, PMAY-G and Focus Plus have none — so a block/AC
   collision was structurally invisible to three of them. A collision the
   asking scheme cannot see is one the user is never asked about.

2. The gate only inspected names the LLM extractor handed over, and the
   extractor returned {} on 5 of 5 calls for this question (lowercase
   "mylliem" buried between two scheme names). No mention meant nothing to
   test, so the gate stayed silent even once fault 1 was fixed.

3. Having chosen "the block", the answer was still blocked: the verifier
   rejected correct SQL over SQL's doubled-apostrophe escaping in
   'Chief Minister''s Green Taxi Scheme', quoting the identical text on both
   sides of its own complaint, 8 of 8 calls.

Fixes, in the same order:
  * _collision_values() falls back to another scheme's catalogue for the
    dimensions every scheme shares (block, assembly_constituency). Districts
    are excluded — a complete, collision-free 12-name set everywhere.
  * the gate scans the raw question for a catalogue name when no place mention
    survived. It only ADDS a candidate; collides_across_dimensions still
    decides whether to pause, and still needs 2+ readings.
  * _verifier_apostrophe_complaint_is_false discards a "not verbatim" complaint
    when un-doubling the SQL's apostrophes makes the resolved value present.

Ground truth: MYLLIEM block holds Green Taxi 1 + Warehouse 1 = 2.

Catalogue/regex level — no model, no DB, no network. Plain script (no pytest in
the venv): `python tests/test_cross_scheme_collision.py`, exit 0 = pass.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from app import entity_resolver as er  # noqa: E402
from app import pipeline as p  # noqa: E402
from app.entity_resolver import collides_across_dimensions, load_all  # noqa: E402

load_all()

_fails: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


# ── 1. Every scheme now sees the collision ─────────────────────────────────
print("1. THE COLLISION IS VISIBLE TO EVERY SCHEME")
for scheme in ("CM Elevate", "MGNREGA", "PMAY-G", "Focus Plus"):
    dims = collides_across_dimensions("mylliem", scheme)
    check(f"{scheme:12s} sees MYLLIEM as block + constituency",
          set(dims) >= {"block", "assembly_constituency"}, dims)

# Only MGNREGA actually ships an AC catalogue — the others borrow it. If that
# ever changes this test still passes; if the FALLBACK breaks, section 1 fails.
_ac_owners = [s for s in ("CM Elevate", "MGNREGA", "PMAY-G", "Focus Plus")
              if er._catalog.get(s, {}).get("assembly_constituency")]
check("  and only MGNREGA actually owns an AC catalogue (so the fallback is load-bearing)",
      _ac_owners == ["MGNREGA"], _ac_owners)

# ── 2. The fallback must not invent collisions ─────────────────────────────
print("2. THE FALLBACK DOES NOT MANUFACTURE AMBIGUITY")
check("a plain district is still unambiguous",
      collides_across_dimensions("West Garo Hills", "CM Elevate") == {},
      collides_across_dimensions("West Garo Hills", "CM Elevate"))
check("a name that is only a block stays unambiguous",
      "assembly_constituency" not in collides_across_dimensions("BATABARI", "CM Elevate"),
      collides_across_dimensions("BATABARI", "CM Elevate"))
check("nonsense collides with nothing",
      collides_across_dimensions("zzzqqq", "CM Elevate") == {})
check("empty input is handled",
      collides_across_dimensions("", "CM Elevate") == {})
# Districts are deliberately excluded from the shared-dimension fallback.
check("district is not a shared-fallback dimension",
      "district" not in er._SHARED_ADMIN_DIMENSIONS, er._SHARED_ADMIN_DIMENSIONS)

# ── 3. The gate scans the question when the extractor returns nothing ──────
print("3. THE GATE HAS A RAW-TEXT BACKSTOP")
_SRC = (_ROOT / "app" / "pipeline.py").read_text(encoding="utf-8")
check("the gate scans when no place mention survived",
      'if not any(mentions.get(s) for s in' in _SRC)
check("  using the collision-aware name list",
      "collision_canonical_names(scheme, dimension)" in _SRC)
check("_canonical_in_question finds MYLLIEM for CM Elevate as a block",
      p._canonical_in_question("applicants in mylliem under Green Taxi",
                               "CM Elevate", "block") is not None)
check("  and as an assembly constituency, via the fallback",
      p._canonical_in_question("applicants in mylliem under Green Taxi",
                               "CM Elevate", "assembly_constituency") is not None)

# ── 4. The apostrophe false positive is discarded ──────────────────────────
print("4. SQL APOSTROPHE ESCAPING IS NOT A VALUE MISMATCH")
_ISSUE = ("Check 2: RESOLVED ENTITIES block specifies scheme_name = 'Chief Minister''s "
          "Green Taxi Scheme' (verbatim), but the SQL filters on scheme_name IN "
          "('Chief Minister''s Green Taxi Scheme', 'Meghalaya Warehouse Scheme')")
_RES = {"block": "MYLLIEM", "cm_scheme": "Chief Minister's Green Taxi Scheme"}
_SQL = ("SELECT COUNT(DISTINCT request_id) AS applicants FROM curated.v_cm_elevate "
        "WHERE lgd_block = 'MYLLIEM' AND scheme_name IN "
        "('Chief Minister''s Green Taxi Scheme', 'Meghalaya Warehouse Scheme')")
check("the escaped-apostrophe complaint is discarded",
      p._verifier_apostrophe_complaint_is_false(_ISSUE, _RES, _SQL))
check("  a value genuinely absent from the SQL still raises",
      not p._verifier_apostrophe_complaint_is_false(
          _ISSUE, _RES, _SQL.replace("Chief Minister''s Green Taxi Scheme", "Something Else")))
check("  a value with no apostrophe never trips this guard",
      not p._verifier_apostrophe_complaint_is_false(
          _ISSUE, {"block": "MYLLIEM"}, _SQL))
check("  an unrelated complaint is left alone",
      not p._verifier_apostrophe_complaint_is_false(
          "Check 1: PROHIBITED JOIN - v_cm_elevate joined to itself", _RES, _SQL))
check("the guard is wired into _verify_sql",
      "_verifier_apostrophe_complaint_is_false(issue, entity_result" in _SRC)

print()
if _fails:
    print(f"FAILED ({len(_fails)}): " + ", ".join(_fails))
    sys.exit(1)
print("ALL CROSS-SCHEME COLLISION CHECKS PASSED")
