"""
Focus Plus maps its BLOCKS on block_name_raw, not lgd_block — every block-level
figure for this scheme must come from that column.

Measured on megh_db 2026-09-18:
  block_name_raw  populated on all 385,671 rows.
  lgd_block       NULL on 57,649 rows (15%), hiding Rs 18.01 crore of
                  disbursement. Mylliem loses 57% of its money that way,
                  Rongram 50%, Lawsohtun 66%.
A block total read from lgd_block therefore silently under-reports and looks
entirely plausible — the worst failure shape on a government dashboard.
lgd_district (4 nulls) and lgd_village_name (0) are unaffected; this is specific
to the block column.

This supersedes an earlier reading in which lgd_block was the trustworthy
default and block_name_raw was only for "as per the spreadsheet" questions. The
divergence those figures showed (Songsak: 38,187,500 vs 38,767,500) is real, but
it is the unmapped 15% showing up, not a spreadsheet-versus-registry choice.

Also covers the retrieval-layer faults found alongside it: the static schema
context omitted block_name_raw entirely, and catalog_block() capped column notes
at max_terms and truncated each at 280 chars — so a documented data_quality_note
could be dropped outright or cut before its worked figures.

Pure-Python assertions against a simulated catalogue — no model, no DB. Plain
script (no pytest in the venv):
`python tests/test_focusplus_block_columns.py`, exit code 0 = all pass.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import prompt_builder as pb  # noqa: E402
from app import schema_introspect as si  # noqa: E402
from app.schema_context import build_schema_context  # noqa: E402

_fails: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


# ── 1. The static prompt knows both columns exist ──────────────────────────
print("1. STATIC SCHEMA CONTEXT")
_ctx = build_schema_context(["Focus Plus"])
check("v_focus_plus lists block_name_raw", "block_name_raw" in _ctx)
check("  and names it as THE block column for the scheme",
      "blocks are mapped on block_name_raw" in _ctx.lower(), _ctx[:0])
check("  and quantifies what lgd_block loses",
      "57,649" in _ctx and "15%" in _ctx, _ctx[:0])
check("  and names a block that loses most of its money",
      "Mylliem" in _ctx and "57%" in _ctx)
check("  and warns the under-report is silent",
      "silently under-report" in _ctx.lower())
check("  and says the stored casing is Title Case",
      "Title Case" in _ctx and "UPPER(block_name_raw)" in _ctx)
check("  and confirms district/village are unaffected",
      "lgd_district" in _ctx and "lgd_village_name" in _ctx)

# A scheme whose block column IS lgd_block must not inherit the override.
check("the override is scoped to Focus Plus",
      "blocks are mapped on block_name_raw" not in build_schema_context(["PMAY-G"]).lower())

# ── 2. A data_quality_note survives a crowded catalogue ────────────────────
# The failure mode: many description-only columns for the same scheme crowd out
# the notes that actually prevent wrong numbers.
print("2. CATALOG NOTES SURVIVE THE CAP AND THE TRUNCATION")
_LGD_NOTE = (
    "Official government-registry-corrected block name. Use for any normal block-level "
    "question. Differs from block_name_raw for the same block: Songsak totals 38,187,500 "
    "here versus 38,767,500 by block_name_raw, because the source spreadsheet was "
    "hand-typed and contains typos and wrong block assignments that the registry "
    "correction fixes. Neither number is wrong - they answer different questions."
)
_RAW_NOTE = (
    "The source spreadsheet's own, uncorrected block name. Use ONLY when the question "
    "explicitly asks what the spreadsheet or source file itself says - trigger phrases: "
    "as per the spreadsheet, as per the source file, the raw block, the original block, "
    "what does the file say, reconcile against the client's export."
)

_saved = {k: si._cache[k] for k in ("loaded", "tables", "glossary", "metrics", "columns")}
try:
    si._cache["loaded"] = True
    si._cache["tables"] = []
    si._cache["metrics"] = []
    si._cache["glossary"] = [{
        "term": "spreadsheet block name (Focus+)", "subject_area": "focus_plus",
        "definition": "The block name exactly as the source spreadsheet recorded it.",
        "maps_to_column": "block_name_raw",
        "synonyms": ["as per the spreadsheet", "the raw block", "the original block"],
    }]
    # 30 description-only columns — more than max_terms (24) on their own.
    si._cache["columns"] = [
        {"table_schema": "curated", "table_name": "v_focus_plus",
         "column_name": f"filler_{i}", "description": f"filler description {i}",
         "data_quality_note": None} for i in range(30)
    ] + [
        {"table_schema": "curated", "table_name": "v_focus_plus",
         "column_name": "lgd_block", "description": None, "data_quality_note": _LGD_NOTE},
        {"table_schema": "curated", "table_name": "v_focus_plus",
         "column_name": "block_name_raw", "description": None, "data_quality_note": _RAW_NOTE},
    ]

    out = si.catalog_block(["Focus Plus"])

    check("the lgd_block note is emitted", "v_focus_plus.lgd_block:" in out)
    check("the block_name_raw note is emitted", "v_focus_plus.block_name_raw:" in out)
    check("  neither is dropped by the max_terms cap",
          out.count("v_focus_plus.lgd_block:") == 1
          and out.count("v_focus_plus.block_name_raw:") == 1)
    check("the trigger phrasing survives truncation",
          "reconcile against the client's export" in out, out[-200:])
    check("  and so do the worked figures",
          "38,187,500" in out and "38,767,500" in out)
    check("descriptions are still capped, so the block does not run away",
          out.count("filler description ") <= 24, out.count("filler description "))
    check("the glossary entry reaches the prompt with its target column",
          "spreadsheet block name (Focus+)" in out and "block_name_raw" in out)
    check("  and with its synonyms, so the phrasing is matchable",
          "as per the spreadsheet" in out, out[:0])
finally:
    si._cache.update(_saved)

# ── 3. The RESOLVED ENTITIES line must name the right column ───────────────
# This is the half that actually decides the query: the entity line is marked
# MANDATORY and outranks the catalogue guidance, so if it pins lgd_block the
# generator uses lgd_block and the 15% loss lands in the answer.
print("3. THE RESOLVED-ENTITY LINE PICKS PER SCHEME")
_ent = {"resolved": {"block": "SONGSAK"}, "notes": []}

_fp = pb._entities_block(_ent, "total Focus Plus disbursement in Songsak block", ["Focus Plus"])
check("Focus Plus pins block_name_raw", "block_name_raw" in _fp, _fp[-110:])
check("  case-insensitively, since it is stored Title Case",
      "UPPER(block_name_raw) = 'SONGSAK'" in _fp, _fp[-110:])
check("  and tells the generator NOT to also filter lgd_block",
      "do NOT also filter lgd_block" in _fp, _fp[-140:])
check("  and says why (the 15% loss)", "15%" in _fp)

# Every other scheme keeps lgd_block — its block column is complete.
for _scheme in ("MGNREGA", "PMAY-G", "CM Elevate"):
    _other = pb._entities_block(_ent, "expenditure in Songsak block", [_scheme])
    check(f"{_scheme} still pins lgd_block",
          "lgd_block = 'SONGSAK'" in _other and "block_name_raw" not in _other, _other[-90:])

# A cross-scheme question reads objects whose block column IS lgd_block, so the
# Focus-Plus-only override must not leak into it.
_cross = pb._entities_block(_ent, "compare Focus Plus and MGNREGA by block",
                            ["Focus Plus", "MGNREGA"])
check("a cross-scheme question keeps lgd_block",
      "lgd_block = 'SONGSAK'" in _cross and "block_name_raw" not in _cross, _cross[-90:])

check("the scheme test itself is exact, not a substring match",
      pb._focus_plus_only(["Focus Plus"])
      and not pb._focus_plus_only(["Focus Plus", "PMAY-G"])
      and not pb._focus_plus_only([]) and not pb._focus_plus_only(None))


print()
if _fails:
    print(f"FAILED ({len(_fails)}): " + ", ".join(_fails))
    sys.exit(1)
print("ALL FOCUS+ BLOCK-COLUMN CHECKS PASSED")
