"""
backend/prompt_builder.py — the single place the SQL-generation prompt is built.

Everything the generator sees about the database is composed here, in one order,
from four sources with a clear precedence:

  1. schema_context.build_schema_context(schemes)   — HAND-WRITTEN BACKBONE
        The curated/semantic object list per scheme, the money-unit facts, and
        the "wrong number if broken" hazard rules. Those rules are judgement, not
        structure — they can never be introspected, so they stay hand-maintained
        (see docs/DATA_MODEL.md).
  2. LIVE SCHEMA block                               — schema_introspect.live_columns()
        The columns that actually exist in megh_db right now, pulled from
        information_schema at startup, scheme-scoped. This is what catches
        schema_context.py drifting out of sync with the database. Silently
        omitted when the startup read didn't land (offline dev, catalog perms).
  3. LIVE CATALOG block                              — schema_introspect.catalog_block()
        SME-curated semantic.* descriptions, synonyms and metric formulas.
  4. PROHIBITED JOINS / VERIFIED EXAMPLES / RESOLVED ENTITIES
        annotations.py + entity_resolver output. Resolved entities go LAST, right
        before the question — a wrong-case district literal silently matches zero
        rows, so it is the single most load-bearing part of the prompt.

Public:
  build_sql_prompt(question, schemes, entity_result)      -> str   (first attempt)
  build_repair_prompt(question, schemes, entity_result,
                      failed_sql, error)                    -> str  (retry)

The repair prompt now carries the SAME schema + resolved-entities context as the
first attempt (plus the failure), instead of the thin schema-only prompt used
before — a repair most often needs exactly the entity block it was missing.
"""
import re

from app import context_budget, schema_introspect
from app.annotations import common_mistakes_text, few_shot_examples, prohibited_joins_text
from app.schema_context import build_schema_context

# Bare table name -> owning scheme, so the LIVE SCHEMA block is scoped the same
# way schema_context.py scopes its hand-written one.
_SHARED_PREFIXES = ("dim_scheme", "dim_year", "dim_geography")
_CROSS_PREFIXES = ("v_cross_scheme",)
_MGNREGA_EXACT = {"v_employment", "v_expenditure", "v_district_year_summary"}
_FOCUSPLUS_EXACT = {"v_focus_plus"}
_CMELEVATE_EXACT = {"v_cm_elevate", "dim_cm_elevate_scheme"}
_FOCUSLEGACY_EXACT = {"v_focus_legacy", "dim_producer_group", "dim_pg_entity_type",
                      "bridge_pg_bank_history"}
# Checked BEFORE the CM Elevate substring test below: every one of these names
# also contains "cm_elevate", and would otherwise be filed under the other,
# unrelated CM Elevate dataset.
_CMELEVATELEGACY_EXACT = {"v_cm_elevate_disbursement", "fact_cm_elevate_disbursement",
                          "dim_cm_elevate_disb_scheme"}


def _scheme_of(table: str) -> str:
    """'MGNREGA' | 'PMAY-G' | 'Focus Plus' | 'CM Elevate' | 'Focus Legacy' |
    'CM Elevate Legacy' | 'shared' | 'cross' for a bare (unqualified) table name."""
    t = table.lower()
    if t.startswith(_SHARED_PREFIXES):
        return "shared"
    if t.startswith(_CROSS_PREFIXES):
        return "cross"
    if "cm_elevate_disb" in t or t in _CMELEVATELEGACY_EXACT:
        return "CM Elevate Legacy"
    if "focus_legacy" in t or "focuslegacy" in t or t in _FOCUSLEGACY_EXACT:
        return "Focus Legacy"
    if "focus_plus" in t or "focusplus" in t or t in _FOCUSPLUS_EXACT:
        return "Focus Plus"
    if "cm_elevate" in t or "cmelevate" in t or t in _CMELEVATE_EXACT:
        return "CM Elevate"
    if "mgnrega" in t or t in _MGNREGA_EXACT:
        return "MGNREGA"
    if "pmay" in t:
        return "PMAY-G"
    return "shared"  # unknown object -> show it rather than hide it


def _live_schema_block(schemes: list[str]) -> str:
    """Real columns per table, scoped to the classified scheme(s). Empty string
    when live introspection is unavailable — the caller then relies on the
    hand-written backbone alone, exactly as before this layer existed."""
    live = schema_introspect.live_columns()
    if not live:
        return ""

    want = set(schemes)
    multi = len(schemes) > 1
    lines = [
        "LIVE SCHEMA — columns that exist in megh_db right now (information_schema, "
        "read at startup). If a column is not listed here it does not exist; do not use it.",
    ]
    for qualified, cols in sorted(live.items()):
        _, _, bare = qualified.partition(".")
        owner = _scheme_of(bare)
        if owner == "cross" and not multi:
            continue
        if owner in ("MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate",
                     "Focus Legacy", "CM Elevate Legacy") and owner not in want:
            continue
        lines.append(f"  {qualified}({', '.join(c['column'] for c in cols)})")

    fks = schema_introspect.live_fks()
    scoped_fks = [
        fk for fk in fks
        if _scheme_of(fk["from"].partition(".")[2]) in ({"shared"} | want | ({"cross"} if multi else set()))
    ]
    if scoped_fks:
        lines.append("")
        lines.append("REAL FOREIGN KEYS (valid join paths — PROHIBITED JOINS below still overrides):")
        lines += [f"  {fk['from']}.{fk['from_column']} -> {fk['to']}.{fk['to_column']}"
                  for fk in scoped_fks]
    return "\n".join(lines) + "\n"


def _fewshot_block(schemes: list[str], question: str = "", top_k: int = 5) -> str:
    examples = few_shot_examples(schemes, question, top_k=top_k)
    if not examples:
        return ""
    parts = []
    for ex in examples:
        if ex["sql"] is None:
            parts.append(
                f'Q: "{ex["question"]}"\n'
                f'NOT ANSWERABLE from this data — {ex["reason"]} Do not substitute a '
                'different metric (like a row count) to make it look answerable; state '
                'plainly that the figure is not held in this warehouse.'
            )
        elif ex.get("plan"):
            plan = " -> ".join(ex["plan"])
            parts.append(f'Q: "{ex["question"]}"\nPlan: {plan}\nSQL: {ex["sql"]}')
        else:
            parts.append(f'Q: "{ex["question"]}"\nSQL: {ex["sql"]}')
    return "\nEXAMPLES (verified SQL, and known-unanswerable questions):\n" + "\n\n".join(parts) + "\n"


def _common_mistakes_block(schemes: list[str]) -> str:
    text = common_mistakes_text(schemes)
    if not text:
        return ""
    return ("\nCOMMON MISTAKES — each WRONG query below returns a wrong number or zero "
            "rows; write the RIGHT shape:\n" + text + "\n")


def _focus_legacy_only(schemes: "list[str] | None") -> bool:
    """True when Focus Legacy is the ONLY scheme in play.

    It reaches the assembly constituency through a dim_geography join rather
    than a column on its own view, so the resolved-entity line has to spell out
    a different filter than the MGNREGA one. A cross-scheme question also reads
    MGNREGA, whose AC really is a column, so this is an exact-match test."""
    return list(schemes or []) == ["Focus Legacy"]


def _focus_plus_only(schemes: "list[str] | None") -> bool:
    """True when Focus Plus is the ONLY scheme in play.

    Focus Plus maps its blocks on block_name_raw, not lgd_block — see the block
    branch in _entities_block. Scoped to the single-scheme case on purpose: a
    cross-scheme question also reads MGNREGA / PMAY-G / CM Elevate, whose block
    column IS lgd_block, and pinning a Focus-Plus-only column there would break
    the other side of the comparison."""
    return list(schemes or []) == ["Focus Plus"]


def _entities_block(entity_result: dict, question: str = "",
                    schemes: "list[str] | None" = None) -> str:
    resolved = entity_result.get("resolved", {})
    notes = entity_result.get("notes", [])
    lines: list[str] = []
    if resolved:
        lines.append(
            "RESOLVED ENTITIES — MANDATORY: the WHERE clause MUST use these exact "
            "values, not any name or spelling from the question text. These are already "
            "resolved against the database (correct case, correct code, correct year_key):"
        )
        # A resolved village_code already pins one exact village — a finer grain than
        # district/block, which entity resolution also carries here only because they
        # scoped the village lookup (resolve_village(text, district=..., block=...)),
        # not because the question needs them enforced as their own separate filter.
        # Rendering "lgd_block = 'MAWSHYNRUT'" as an equally MANDATORY entity alongside
        # village_code told the SQL verifier the block filter must appear verbatim too;
        # a generator that reasonably left it out (village_code already implies it) then
        # got its correct SQL rejected as "missing the resolved block entity" (reported
        # 2026-09-12 UAT: Nongthymmai in Mawshynrut block, WEST KHASI HILLS). Suppress
        # district/block here whenever village_code is present — they are redundant
        # with it, never an independent filter to double up on.
        _skip = {"district", "block"} if resolved.get("village_code") else set()
        for k, v in resolved.items():
            if k in _skip:
                continue
            if k == "district_list_region":
                continue  # rendered alongside district_list
            if k == "block_list_districts":
                continue  # rendered alongside block_list
            if k == "village_code":
                lines.append(f"  village_code = {v!r}   -- do NOT filter on lgd_village_name instead")
            elif k == "district":
                lines.append(f"  lgd_district = {v!r}   -- already uppercase, matches storage exactly")
            elif k == "district_list":
                vals = v if isinstance(v, list) else [v]
                quoted = ", ".join(f"'{s}'" for s in vals)
                region = resolved.get("district_list_region")
                if region:
                    lines.append(
                        f"  lgd_district IN ({quoted})   -- the {len(vals)} districts that make up "
                        f"\"{region}\" (a hill range, not a single district). Filter on ALL of them "
                        f"with IN, and state in the answer which districts \"{region}\" covers.")
                else:
                    lines.append(
                        f"  lgd_district IN ({quoted})   -- the {len(vals)} districts explicitly "
                        "named for comparison. Filter on ALL of them with IN and GROUP BY district "
                        "so each gets its own row in the result — do NOT sum them into one figure.")
            elif k == "block":
                if _focus_plus_only(schemes):
                    # FOCUS PLUS ONLY: block_name_raw is the block column for
                    # this scheme. lgd_block is NULL on 57,649 of 385,671 rows
                    # (15%), hiding ₹18.01 crore of disbursement — Mylliem
                    # loses 57% of its money, Rongram 50% — because those rows
                    # were never mapped to the LGD registry. block_name_raw is
                    # populated on every row. A block-level figure taken from
                    # lgd_block therefore silently under-reports; measured
                    # 2026-09-18. Stored Title Case ("Songsak"), NOT uppercase,
                    # so the filter must be case-insensitive or it matches zero
                    # rows.
                    lines.append(
                        f"  UPPER(block_name_raw) = {str(v).upper()!r}   -- Focus Plus maps "
                        "blocks on block_name_raw; lgd_block is NULL on 15% of rows and "
                        "silently drops that money. Filter and GROUP BY block_name_raw, and "
                        "do NOT also filter lgd_block (ANDing them re-introduces the same "
                        "15% loss). It is stored Title Case, so compare with UPPER(...).")
                else:
                    lines.append(
                        f"  lgd_block = {v!r}   -- already uppercase, matches storage exactly")
            elif k == "block_list":
                vals = v if isinstance(v, list) else [v]
                quoted = ", ".join(f"'{s}'" for s in vals)
                lines.append(
                    f"  lgd_block IN ({quoted})   -- the {len(vals)} blocks explicitly named "
                    "for comparison. Filter on ALL of them with IN and GROUP BY block so each "
                    "gets its own row in the result — do NOT sum them into one figure.")
                # Blocks named together often sit in DIFFERENT districts
                # (Shallang is West Khasi Hills, Batabari is West Garo Hills).
                # Spell out each block's own district so the generator does not
                # reach for a single lgd_district filter to cover the whole
                # list — ANDing one district against blocks that sit outside it
                # returns zero rows for those blocks.
                _parents = resolved.get("block_list_districts") or {}
                if _parents:
                    pairs = ", ".join(f"{b} = {d}" for b, d in _parents.items())
                    lines.append(
                        f"      district of each block: {pairs}. These blocks span "
                        "different districts, so do NOT add a single lgd_district "
                        "filter alongside this IN-list — the block names already pin "
                        "the geography. Mention each block's district in the answer.")
            elif k == "village_code_list":
                vals = v if isinstance(v, list) else [v]
                quoted = ", ".join(str(s) for s in vals)
                lines.append(
                    f"  village_code IN ({quoted})   -- the {len(vals)} villages explicitly "
                    "named for comparison. Filter on ALL of them with IN and GROUP BY "
                    "village_code (and lgd_village_name, which is on the same row) so each "
                    "village gets its own row in the result — do NOT sum them into one figure, "
                    "and do NOT filter on lgd_village_name instead.")
            elif k == "assembly_constituency":
                if list(schemes or []) == ["CM Elevate Legacy"]:
                    # Same shape as Focus Legacy: v_cm_elevate_disbursement has
                    # no constituency column but carries geography_key, a
                    # declared FK to dim_geography, which holds ac_name
                    # (cmelevatelegacy_foreign_key_augmentation.yaml). The
                    # join reproduces the source workbook's
                    # mapped_constituency_name exactly (Mairang: 52 = 52).
                    lines.append(
                        f"  the {v!r} assembly constituency   -- v_cm_elevate_disbursement "
                        "has NO constituency column. Join it: FROM "
                        "curated.v_cm_elevate_disbursement v JOIN curated.dim_geography g "
                        "ON g.geography_key = v.geography_key, then filter "
                        f"UPPER(g.ac_name) = UPPER({v!r}). This is the constituency, NOT a "
                        "block of the same name — never filter lgd_block for it. Records "
                        "with no village carry no constituency; say the constituency comes "
                        "from the geography registry.")
                elif _focus_legacy_only(schemes):
                    # v_focus_legacy has NO constituency column. The AC is
                    # reached through the documented dimension join on
                    # geography_key — a declared FK, explicitly permitted
                    # (focuslegacy_schema_partitions.yaml constituency_rule /
                    # sanctioned_patterns.constituency). Emitting the MGNREGA
                    # column here made the filter unusable, so the constituency
                    # the user had just picked was dropped and the pipeline
                    # asked for a district instead (reported 2026-09-23).
                    lines.append(
                        f"  the {v!r} assembly constituency   -- v_focus_legacy has NO "
                        "constituency column. Join it: FROM curated.v_focus_legacy f JOIN "
                        "curated.dim_geography g ON g.geography_key = f.geography_key, then "
                        f"filter UPPER(g.ac_name) = UPPER({v!r}). Also add "
                        "f.entity_type <> 'Unresolved' — a placeholder row has no real "
                        "village and so no meaningful constituency — and say the AC comes "
                        "from the geography registry, so the total will not reconcile to "
                        "the scheme total.")
                else:
                    lines.append(
                        f"  UPPER(assembly_constituency_name) = UPPER({v!r})   -- this column "
                        "exists ONLY in mgnrega_employment. If the question also needs "
                        "expenditure, say that level isn't available there instead of silently "
                        "switching to a block/district filter.")
                # An assembly constituency is an electoral boundary that CUTS
                # ACROSS the administrative hierarchy — it is not a parent of
                # the block, and neither filter implies the other. When both
                # are resolved the user asked for the OVERLAP (a drill-down
                # inside the constituency, see pipeline._ac_drilldown_
                # clarification), so both must appear, ANDed. Spelled out
                # because the generator otherwise reads two geography entities
                # as alternatives and emits whichever it judges more specific
                # — and a repair round then "fixes" the wrong one by dropping
                # the other, which is how this question burned its whole
                # repair budget and fell through to the KB fallback
                # (confirmed live 2026-09-15).
                if not resolved.get("block") and not resolved.get("district"):
                    # An assembly constituency is an electoral boundary that
                    # CUTS ACROSS blocks and districts — its name is very often
                    # not a block name at all. The generator nevertheless
                    # reaches for `lgd_block = '<that same name>'` alongside the
                    # constituency filter, which ANDs two conditions that can
                    # never both hold and returns a clean, confident ZERO
                    # (confirmed live 2026-09-17: "which villages in Rangsakona
                    # received employment in FY 2025-26" answered "no matching
                    # records" — RANGSAKONA is an AC spanning the BETASING,
                    # RERAPARA and RONGRAM blocks, and the real answer is 146
                    # villages). Say so outright, since nothing in the resolved
                    # entities tells the generator not to.
                    lines.append(
                        "      CONSTITUENCY ONLY: filter on assembly_constituency_name and "
                        "NOTHING ELSE for geography. Do NOT add lgd_block, lgd_district or "
                        "village_code conditions — an assembly constituency cuts across "
                        "blocks and districts, so its name is usually NOT a block or "
                        "district name, and ANDing one in matches zero rows and returns a "
                        "false zero. Aggregate or list across the whole constituency.")
                if resolved.get("block") or resolved.get("district"):
                    _other = []
                    if resolved.get("district"):
                        _other.append(f"lgd_district = {resolved['district']!r}")
                    if resolved.get("block"):
                        _other.append(f"lgd_block = {resolved['block']!r}")
                    lines.append(
                        "      BOTH REQUIRED: keep this constituency filter AND "
                        + " AND ".join(_other) +
                        " in the same WHERE clause, joined with AND. The constituency is an "
                        "electoral boundary that cuts across blocks/districts, so neither "
                        "filter implies the other and the question asks for the rows where "
                        "they overlap. Do NOT drop either one, and do NOT substitute one "
                        "for the other.")
            elif k == "year_key":
                lines.append(f"  year_key = {v!r}")
            elif k == "house_status":
                vals = v if isinstance(v, list) else [v]
                quoted = ", ".join(f"'{s}'" for s in vals)
                if len(vals) == 1:
                    lines.append(f"  status_name = {quoted}   -- exact stored PMAY-G stage "
                                 "label; use it verbatim, do not paraphrase or re-case")
                else:
                    lines.append(f"  status_name IN ({quoted})   -- exact stored PMAY-G stage "
                                 "labels; match with IN / OR, never AND")
                if set(vals) & {"Proposed Site", "Existing site(Old House)"}:
                    lines.append(
                        "      NOTE: in curated.v_pmay EVERY 'Proposed Site' and 'Existing "
                        "site(Old House)' row has is_placeholder = TRUE (they are the "
                        "sanctioned_amount = 0 / year_key NULL bucket). For this status "
                        "count do NOT add 'AND NOT is_placeholder' — that guard would drop "
                        "the entire category and return a false 0. Count status_name over "
                        "all rows.")
            elif k == "tranche_label":
                vals = v if isinstance(v, list) else [v]
                quoted = ", ".join(f"'{s}'" for s in vals)
                if len(vals) == 1:
                    lines.append(f"  tranche_label = {quoted}   -- exact stored Focus Plus "
                                 "label (spelled \"Tranch\", not \"Tranche\", plus a month "
                                 "suffix); use it verbatim, do NOT substitute the question's "
                                 "own spelling")
                else:
                    lines.append(
                        f"  tranche_label IN ({quoted})   -- the {len(vals)} tranches "
                        "explicitly named for comparison. Filter on ALL of them with IN and "
                        "GROUP BY tranche_label so each gets its own row in the result — do "
                        "NOT sum them into one figure.")
            elif k == "cm_scheme":
                vals = v if isinstance(v, list) else [v]
                quoted = ", ".join(f"'{s}'" for s in vals)
                # Same entity key for both CM Elevate datasets (each resolves its
                # own sub-scheme catalogue into it); only the label differs.
                _label = ("CM Elevate Legacy scheme" if "CM Elevate Legacy" in (schemes or [])
                          else "CM Elevate sub-scheme")
                if len(vals) == 1:
                    lines.append(f"  scheme_name = {quoted}   -- exact stored {_label} "
                                 "name (mixed case, stored exactly); use it "
                                 "verbatim, do NOT substitute the question's own spelling "
                                 "or wording for it")
                else:
                    lines.append(
                        f"  scheme_name IN ({quoted})   -- the {len(vals)} sub-schemes "
                        "explicitly named for comparison. Filter on ALL of them with IN and "
                        "GROUP BY scheme_name so each gets its own row in the result — do "
                        "NOT sum them into one figure.")
            else:
                lines.append(f"  {k} = {v!r}")
    if notes:
        lines.append("NOT FOUND (do not filter on these — state plainly they're not in the data):")
        lines += [f"  {n}" for n in notes]
    return "\n" + "\n".join(lines) + "\n" if lines else ""


def _prohibited_block(schemes: list[str]) -> str:
    prohibited = prohibited_joins_text(schemes)
    return f"\nPROHIBITED JOINS:\n{prohibited}\n" if prohibited else ""


def _fewshot_ranking_text(question: str, entity_result: dict) -> str:
    """`question` plus any resolved district/block canonical names, so few-shot
    retrieval matches geography-specific worked examples even when the question
    uses an abbreviation or alias (e.g. "wgh") that doesn't textually overlap
    with an example's spelled-out district name. Ranking-only — the literal
    question sent to the LLM is untouched (see build_sql_prompt below).
    Confirmed live 2026-09-13: without this, "...beneficiaries in wgh across
    all financial years" scored every financial-year example above the correct
    single-district worked example, because "wgh" shares no tokens with "West
    Garo Hills" and financial/year did double duty for four separate examples."""
    resolved = entity_result.get("resolved", {})
    extra = " ".join(
        str(resolved[k]) for k in ("district", "block") if resolved.get(k)
    )
    return f"{question} {extra}".strip() if extra else question


# Every builder below hands context_budget.assemble() NAMED sections, joined in
# the same order as before, so the prompt text is byte-identical. The names
# exist so each call logs its make-up (tokens per section, total vs the model's
# budget) as one prompt_context line. Nothing is pruned to fit a budget:
# every section here is load-bearing, see context_budget's docstring.
def _dedupe_catalog(catalog_text: str, *already: str) -> str:
    """The catalogue block minus any line the schema text above it already
    states verbatim (ignoring surrounding whitespace). Compression, not
    removal: every distinct description, synonym and metric formula stays."""
    seen = {ln.strip() for block in already for ln in block.splitlines() if ln.strip()}
    kept = [ln for ln in catalog_text.splitlines() if not ln.strip() or ln.strip() not in seen]
    return "\n".join(kept) + ("\n" if catalog_text.endswith("\n") else "")


def build_sql_prompt(question: str, schemes: list[str], entity_result: dict) -> str:
    S = context_budget.Section
    catalog = schema_introspect.catalog_block(schemes)
    catalog_text = (catalog + "\n") if catalog else ""
    backbone = build_schema_context(schemes)
    live = _live_schema_block(schemes)
    rank_text = _fewshot_ranking_text(question, entity_result)
    # Priority-aware: every section that decides WHICH rows and HOW they are
    # counted is required and never shortened. Only the worked examples and
    # the catalogue's restatements of the schema can shrink, and only when
    # the prompt is over PROMPT_BUDGET_SQL_TOKENS (context_budget).
    return context_budget.assemble_prioritized("sql", [
        S("schema_backbone", backbone), S("schema_backbone", "\n\n"),
        S("live_schema", live),
        S("catalog", catalog_text, required=False, priority=20,
          compress=(lambda: _dedupe_catalog(catalog_text, backbone, live),)),
        S("prohibited_joins", _prohibited_block(schemes)),
        S("few_shot", _fewshot_block(schemes, rank_text), required=False, priority=10,
          compress=(lambda: _fewshot_block(schemes, rank_text, top_k=3),
                    lambda: _fewshot_block(schemes, rank_text, top_k=1))),
        S("common_mistakes", _common_mistakes_block(schemes)),
        S("resolved_entities", _entities_block(entity_result, question, schemes)),
        S("scheme_scope", f"\nThe user's question is about: {', '.join(schemes)}.\n"),
        S("question", f'\nQuestion: "{question}"\nSQL:'),
    ], schemes=schemes)


def build_repair_prompt(question: str, schemes: list[str], entity_result: dict,
                        *, failed_sql: str, error: str, extra_hint: str | None = None) -> str:
    return context_budget.assemble("sql_repair", [
        ("schema_backbone", build_schema_context(schemes)), ("schema_backbone", "\n\n"),
        ("live_schema", _live_schema_block(schemes)),
        ("prohibited_joins", _prohibited_block(schemes)),
        ("common_mistakes", _common_mistakes_block(schemes)),
        ("resolved_entities", _entities_block(entity_result, question, schemes)),
        ("failure", "\nThe previous query FAILED and must be corrected.\n"),
        ("failure", f"Error: {error}\n"),
        ("failure", (f"Hint: {extra_hint}\n" if extra_hint else "")),
        ("failed_sql", f"Previous query:\n{failed_sql}\n"),
        ("question", f'\nQuestion: "{question}"\n'),
        ("question", "Return the corrected single read-only SELECT. SQL:"),
    ], schemes=schemes)


_VERIFY_CALIBRATION = """
CALIBRATION — how strict to be:
Q: "How many payments were made in Tranch 2?"
RESOLVED ENTITIES: tranche_label = 'Tranch 2 - August'
SQL: SELECT COUNT(*) AS payments FROM curated.v_focus_plus WHERE tranche_label = 'Tranch 2 - August';
{"ok": true}
  (The resolved value IS the whole tranche — "Tranch 2 - August" is a closed-set
  label, not a month filter layered on top of "Tranch 2". Using it verbatim is
  correct, not a narrowing of scope.)

Q: "How many MGNREGA person days in East Garo Hills?"
RESOLVED ENTITIES: lgd_district = 'EAST GARO HILLS'
SQL: SELECT SUM(person_days) AS person_days FROM curated.v_employment WHERE lgd_district = 'EAST GARO HILLS';
{"ok": true}
  (District-level question, district-level filter, on the resolved column. No
  finer grain was asked for or required.)

Q: "Compare person days between East Garo Hills and West Garo Hills"
RESOLVED ENTITIES: lgd_district IN ('EAST GARO HILLS', 'WEST GARO HILLS')
SQL: SELECT SUM(person_days) AS person_days FROM curated.v_employment WHERE lgd_district = 'EAST GARO HILLS';
{"ok": false, "issue": "Comparison names two districts but the SQL filters on only one (WEST GARO HILLS is dropped), so the result has no second value to compare against."}
  (This IS a real violation — a resolved value is silently missing from the SQL.)

Q: "How much has been disbursed in Dalu block for Focus Plus?"
RESOLVED ENTITIES: lgd_block = 'DALU'
SQL: SELECT SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus WHERE lgd_block = 'DALU' LIMIT 1;
{"ok": true}
  (SUM(...) IS the aggregation — a SUM/COUNT/AVG with no GROUP BY always returns
  exactly one row, so the trailing LIMIT 1 is a harmless no-op, not evidence the
  query is missing an aggregate. Read whether SUM/COUNT/AVG wraps the metric
  column, never the presence of "LIMIT 1" by itself, to answer check 3.)

Q: "How many households completed 100 days of work in Maska?"
RESOLVED ENTITIES: village_code = 277769
SQL: SELECT SUM(households_completed_100_days) AS households_completed_100_days FROM curated.v_employment WHERE village_code = 277769;
{"ok": true}
  (village_code is a surrogate integer key, not the place name — it will NEVER
  textually resemble "Maska" the way lgd_district = 'EAST GARO HILLS' resembles
  the district name in the question. That mismatch is expected and correct, not
  a sign the entity was dropped or substituted. Check 2 asks whether the
  RESOLVED ENTITY's value (277769) is in the WHERE clause verbatim — it is —
  never whether the question's own place name also appears literally.)

Q: "How much was disbursed under Focus Plus Tranch 2 for all of Meghalaya, all years"
RESOLVED ENTITIES: tranche_label = 'Tranch 2 - August'
SQL: SELECT SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus WHERE tranche_label = 'Tranch 2 - August';
{"ok": true}
  (Confirmed live 2026-09-12 as a repeat false-positive: a verifier run kept
  rejecting this exact query, arguing "the question asks for all of Meghalaya,
  all years" so the SQL must ALSO filter lgd_district and year_key — that is
  backwards. "For all of Meghalaya, all years" is the ONE-TAP CHIP TEXT for
  "no geography filter, no year filter" (see the calibration instruction above)
  — it is not a second and third resolved entity on top of the tranche. The
  ONLY RESOLVED ENTITY here is tranche_label, and it IS present in the WHERE
  clause verbatim. A correct query for "all of Meghalaya, all years" has NO
  lgd_district and NO year_key filter — adding one, or rejecting the query for
  lacking one, is the actual violation.)

Q: "How many farmers received Focus Plus assistance during FY 2025-26?"
RESOLVED ENTITIES: (empty)
SQL: SELECT COUNT(*) AS registrations FROM curated.v_focus_plus WHERE batch_label = '12.5K' AND occupation = 'Farmer' AND year_key = 2025;
{"ok": true}
  (RESOLVED ENTITIES is empty, but that only means the question named no
  district/block/village/tranche for the entity resolver to pin. batch_label
  and occupation are NOT entities — they are business-vocabulary filters the
  SCHEMA RULES require for this question shape (a schema rule literally says
  "farmers" -> occupation = 'Farmer', scoped to batch_label = '12.5K' because
  occupation is only ever populated on that cohort). A schema-rule-mandated
  categorical filter with no matching row in an empty RESOLVED ENTITIES block
  is expected and correct — it is not "inventing an unlisted filter". Check 2
  only flags a RESOLVED ENTITY's value being dropped or substituted; it never
  flags a category value (batch_label, gender, occupation, focus_status,
  verification_status, or any other closed-vocabulary column) that the SCHEMA
  RULES derive directly from a word in the question, whether or not the
  entities block happens to be empty.)

Q: "What was the total expenditure in Nongthymmai, Mawshynrut block, West Khasi Hills for FY 2022-23?"
RESOLVED ENTITIES: village_code = 276411
           year_key = 2022
SQL: SELECT SUM(total_exp) AS total_expenditure_lakh FROM curated.v_expenditure WHERE village_code = 276411 AND year_key = 2022;
{"ok": true}
  (The question names a block ("Mawshynrut") and a district ("West Khasi
  Hills") in its own text, but the RESOLVED ENTITIES block above lists ONLY
  village_code and year_key — no lgd_block, no lgd_district. That is
  deliberate: village_code already pins one exact village, which is already
  inside that one exact block and district, so a separate lgd_block /
  lgd_district filter would be pure redundancy, not a missing requirement.
  NEVER treat a block/district name that appears in the QUESTION text as an
  entity the SQL must separately filter on — check 2 only ever applies to
  values that appear in the RESOLVED ENTITIES block itself; an unlisted
  district/block name is not a violation to invent.)
""".strip()


def build_verify_prompt(question: str, schemes: list[str], entity_result: dict, sql: str) -> str:
    """Second-opinion check on already-generated SQL (app.llm.call_sql_verifier),
    run before the query touches the database.

    Deliberately narrower than the SQL-generation prompt: earlier versions
    reused the full hand-written rules backbone (build_schema_context) and
    told the small verifier model to check the SQL against "every rule" —
    in testing that made it hallucinate violations on already-correct,
    already-verified SQL (including the project's own few-shot examples) at
    a high rate, because most of those rules describe how the ANSWER TEXT
    should be worded (label multiple readings, state a figure as
    provisional, read money_unit before formatting) rather than what the
    SQL must contain, and a small model given a long prose rule list tends
    to find "a" violation rather than confirm there is none. This version
    hands it only the mechanical, checkable facts — real tables/columns,
    prohibited joins, resolved entities — plus a closed checklist and two
    worked "ok: true" examples so it has a calibration anchor for what a
    passing query looks like, not just failing ones. Still does NOT include
    the SQL-generation few-shot: those are for writing SQL, not judging it."""
    return context_budget.assemble("sql_verify", [
        ("live_schema", _live_schema_block(schemes)),
        ("prohibited_joins", _prohibited_block(schemes)),
        ("resolved_entities", _entities_block(entity_result, question, schemes)),
        ("calibration", "\n"), ("calibration", _VERIFY_CALIBRATION), ("calibration", "\n"),
        ("checklist", "\nCheck the SQL below against ONLY these four things:\n"
        "  1. Every PROHIBITED JOIN above — is one of them actually used?\n"
        "  2. Every RESOLVED ENTITY above — is its exact value present in the "
        "WHERE clause (not dropped, not substituted with different text from "
        "the question)? Judge this ONLY against the RESOLVED ENTITIES block "
        "above — an empty or absent block means there is nothing to check here, "
        "so answer this check true regardless of the question's own wording. "
        "A phrase in the QUESTION itself like 'all years', 'all tranches', "
        "'all financial years', 'all districts', 'all of Meghalaya', 'statewide', "
        "'the whole state', 'every district', 'combined', 'overall' or "
        "'cumulative' is NEVER a resolved entity requiring a WHERE-clause value "
        "— each names a dimension (year, tranche, geography, ...) and is an "
        "instruction to filter on NOTHING for THAT dimension only, so a WHERE "
        "clause that omits a filter for that one dimension is correct, not a "
        "violation — this holds independently for every dimension the question "
        "mentions, so 'Tranch 2 for all of Meghalaya, all years' correctly keeps "
        "the tranche_label filter (a real RESOLVED ENTITY) while correctly "
        "omitting BOTH a geography filter (because of 'all of Meghalaya') AND a "
        "year filter (because of 'all years') — omitting those two is not "
        "'missing requirements from the question's scope', it is exactly what "
        "'all of Meghalaya, all years' asks for. Do not invent a resolved entity "
        "from question text that isn't in the RESOLVED ENTITIES block. Separately: "
        "a WHERE-clause filter on a CLOSED-VOCABULARY CATEGORY COLUMN (e.g. "
        "batch_label, gender, occupation, focus_status, verification_status, "
        "status_name, data_verified, applicant_category — any column whose schema "
        "rules or business-vocabulary mapping tie it to a word in the question, "
        "such as 'farmers' -> occupation = 'Farmer' or a gender/status breakdown "
        "requiring batch_label = '12.5K') is NEVER something Check 2 evaluates, "
        "REGARDLESS of whether the RESOLVED ENTITIES block is empty, non-empty, or "
        "says nothing about that column. RESOLVED ENTITIES only ever lists "
        "geography/time/tranche-style identifiers the entity resolver pinned "
        "(district, block, village, year, tranche); it is not, and was never meant "
        "to be, an exhaustive list of every WHERE-clause value the SQL is allowed "
        "to contain. Judge check 2 ONLY on whether a RESOLVED ENTITY's own value "
        "was dropped or substituted — never flag a category-column filter as "
        "'not in the resolved entities' or 'not mandated by the question', that is "
        "not what this check is for.\n"
        "  3. Table/grain — does it read a raw per-row table when the question "
        "asks for a total (missing SUM/COUNT), or vice versa? Judge this ONLY by "
        "whether SUM/COUNT/AVG wraps the metric column — a trailing LIMIT clause "
        "proves nothing either way (a SUM/COUNT/AVG with no GROUP BY always "
        "returns one row, so LIMIT 1 after one is normal, not a sign the "
        "aggregate is missing).\n"
        "  4. Metric column — does it aggregate a column that has nothing to "
        "do with what the question asks for?\n"
        "Nothing else is in scope. Do not judge phrasing, labelling, rounding, "
        "unit formatting, or whether the answer text will explain a caveat — "
        "those happen after this query runs, in a separate step, and are not "
        "the SQL's job. If you cannot point to a SPECIFIC one of the four "
        "checks above that this exact SQL fails, answer ok: true.\n"),
        ("question", f'\nQuestion: "{question}"\n'),
        ("generated_sql", f"\nGenerated SQL:\n{sql}\n"),
        ("frame", '\nRespond with ONLY a JSON object: {"ok": true} if none of the four '
                  'checks are violated, or {"ok": false, "issue": "<which of the four '
                  'checks it fails, and how>"} if one is.\nJSON:'),
    ], schemes=schemes)
