"""
A plainly-named block must survive the LLM mention-extractor dropping it.

Reported 2026-09-18: "How many applicants are there in shallang block under
Piggery Scheme, PRIME Small Enterprise Empowerment and Development (SEED) and
Meghalaya Poultry Farming Scheme for all of Meghalaya" returned "I understood
the question but couldn't build a working query", 0 results.

Two faults compounded:

1. extract_entity_mentions() returned {} on 5 of 5 calls — the lowercase
   "shallang" sits between two long scheme names and the model skipped it.
   With no resolved block the generator guessed `lgd_block = 'SHALLANG'` off
   the raw question text, unvalidated. The verifier then demanded a
   `lgd_district = 'MEGHALAYA'` filter instead; the state-pseudo-row guard in
   execute_with_repair correctly rejected that ("the whole dataset is already
   Meghalaya"); the next repair put it back. The two fought until the repair
   budget ran out and the question fell to the KB fallback.

2. scan_dimension() — the deterministic backstop that already saves dropped
   DISTRICTS — refused blocks outright, and searched only schemes[0]'s
   catalogue. CM Elevate's block list is missing 21 blocks its own rows span
   (the known catalogue gap), SHALLANG among them, so even once blocks were
   allowed the scan still found nothing.

The fix pairs a gate with a fallback:
  * blocks are scannable only when the question names the level outright
    (_explicit_level_in(q) == "block"). The 26-of-56 block/AC name collision
    that motivated the original refusal is only a hazard while the level is in
    doubt; "in shallang block" removes that doubt. A BARE name still returns
    None and goes to the clarification flow — that is the case the collision
    rule protects, and section 2 below pins it.
  * the scan falls back to other schemes' block catalogues, mirroring what
    resolve_dimension() already does for the same gap.

Ground truth, queried directly: 45 = Piggery 32 + Poultry 8 + SEED 5.

Catalogue/regex level only — no model, no DB, no network. Plain script (no
pytest in the venv): `python tests/test_block_backstop.py`, exit 0 = pass.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from app import pipeline as p  # noqa: E402
from app.entity_resolver import load_all, scan_dimension  # noqa: E402

load_all()

_fails: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


Q = ("How many applicants are there in shallang block under Piggery Scheme "
     ", PRIME Small Enterprise Empowerment and Development (SEED) "
     "and Meghalaya Poultry Farming Scheme for all of Meghalaya")

# ── 1. The reported question now finds its block ───────────────────────────
print("1. THE DROPPED BLOCK IS RECOVERED")
check("the question is recognised as naming the block level",
      p._explicit_level_in(Q) == "block", p._explicit_level_in(Q))
_hit = scan_dimension(Q, "CM Elevate", "block", level_is_explicit=True)
check("the block backstop resolves SHALLANG for CM Elevate",
      _hit is not None and _hit.status == "resolved" and _hit.canonical == "SHALLANG", _hit)
check("  even though CM Elevate's own catalogue lacks it",
      "SHALLANG" not in {str(b).upper() for b in __import__(
          "app.entity_resolver", fromlist=["x"]).canonical_names("CM Elevate", "block")})

# ── 2. The collision hazard the original refusal guarded is still guarded ──
# This is the important half: a block scan off a BARE name must not happen.
print("2. A BARE NAME IS STILL NOT SCANNED AS A BLOCK")
check("no level word -> the gate refuses to scan",
      scan_dimension(Q, "CM Elevate", "block") is None)
for bare in ("How many applicants are there in Shallang",
             "total beneficiaries in Sohra",
             "how many houses in Mairang"):
    check(f"  {bare[:44]!r} is not treated as block-level",
          p._explicit_level_in(bare) != "block", p._explicit_level_in(bare))
# "block wise" is a GROUPING, not a filter on one block.
check("'block wise' grouping is not read as an explicit block filter",
      p._explicit_level_in("give me block wise breakdown for MGNREGA") != "block",
      p._explicit_level_in("give me block wise breakdown for MGNREGA"))

# ── 3. Districts are untouched ─────────────────────────────────────────────
print("3. THE DISTRICT BACKSTOP IS UNCHANGED")
_d = scan_dimension("how many villages are covered in West Garo Hills",
                    "CM Elevate", "district")
check("a plainly-named district still resolves",
      _d is not None and _d.status == "resolved" and "GARO" in str(_d.canonical).upper(), _d)
check("  and needs no explicit-level flag (default call site still works)",
      scan_dimension("beneficiaries in West Garo Hills", "MGNREGA", "district") is not None)
check("the longest overlapping district name still wins",
      str(scan_dimension("figures for South West Garo Hills", "MGNREGA",
                         "district").canonical).upper() == "SOUTH WEST GARO HILLS")
check("an unknown dimension is still refused",
      scan_dimension(Q, "MGNREGA", "assembly_constituency") is None)

# ── 4. The pipeline actually consults the backstop ─────────────────────────
print("4. THE BACKSTOP IS WIRED INTO resolve_entities")
_SRC = (_ROOT / "app" / "pipeline.py").read_text(encoding="utf-8")
check("resolve_entities scans for a block when the level is explicit",
      'scan_dimension(question, schemes[0], "block",' in _SRC)
check("  gated on level_is_explicit=True",
      "level_is_explicit=True" in _SRC)
check("  and only when no village was mentioned",
      'not mentions.get("village")' in _SRC)
check("the district call site still passes no flag",
      'scan_dimension(question, schemes[0], "district")' in _SRC)

print()
if _fails:
    print(f"FAILED ({len(_fails)}): " + ", ".join(_fails))
    sys.exit(1)
print("ALL BLOCK-BACKSTOP CHECKS PASSED")
