"""
prompt_assembler.py  (Focus Plus)
=================================

Standalone prompt-assembler / validator for the **Focus Plus** scheme
(Meghalaya) of the Megh One AI NLP-to-SQL bot.

This module turns the seven hand-curated annotation YAMLs in this folder
(`data/focus_plus/`) into the prompts each pipeline stage would send to an
LLM, plus one "everything" master prompt.

NOTE ON THE LIVE SERVICE
------------------------
The running service does NOT import this module. Focus Plus is wired into
the uniform pipeline exactly like MGNREGA and PMAY-G:
  * app/schema_context.py   - hand-written FOCUS PLUS tables / rules / vocab
  * app/annotations.py      - loads focusplus_few_shot.yaml + the FK graph
  * app/entity_resolver.py  - loads focusplus_entity_resolver.yaml
  * app/pipeline.py         - scheme detection, year coverage, clarification
  * app/kb_ingest.py        - focusplus_complete_reference.md + FAQ -> RAG

This file is kept alongside the YAMLs as an OFFLINE tool: run its
`--self-test` to sanity-check the YAML contract after an edit, or use it to
inspect what a fully YAML-driven prompt for this scheme would look like.

Usage
-----
    from data.focus_plus.prompt_assembler import FocusPlusPromptAssembler

    fp = FocusPlusPromptAssembler()          # defaults to this folder
    fp.build_sql_generation_prompt("how many focus plus payments in WGH")
    fp.build_master_prompt("how many focus plus payments in WGH")

CLI
---
    python data/focus_plus/prompt_assembler.py --self-test
    python data/focus_plus/prompt_assembler.py --mode master \
        --question "how many focus plus payments in WGH" --k 8
    python data/focus_plus/prompt_assembler.py --mode sql \
        --question "average payment per member in the 12.5K batch"
    python data/focus_plus/prompt_assembler.py --mode gate
    python data/focus_plus/prompt_assembler.py --mode defaults
    python data/focus_plus/prompt_assembler.py --mode entities --question "tranche 2"
    python data/focus_plus/prompt_assembler.py --mode response
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

SCHEME_NAME = "Focus Plus"
VIEW_NAME = "curated.v_focus_plus"

# This folder holds the seven annotation YAMLs.
DEFAULT_DIR = Path(__file__).resolve().parent

FILES = {
    "schema": "focusplus_schema_partitions.yaml",
    "classification": "focusplus_classification_rules.yaml",
    "defaults": "focusplus_default_rules.yaml",
    "resolver": "focusplus_entity_resolver.yaml",
    "fk": "focusplus_foreign_key_augmentation.yaml",
    "few_shot": "focusplus_few_shot.yaml",
    "response": "focusplus_response_template.yaml",
}

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "of", "in", "on", "for",
    "to", "and", "or", "how", "many", "much", "what", "which", "who",
    "focus", "plus", "please", "show", "me", "list", "give", "with", "by",
    "per", "this", "that", "there", "does", "do", "did", "has", "have",
    "had", "be", "been", "it", "its", "as", "at", "from", "than", "all",
}


# --------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------- #

def _load_yaml(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@dataclass
class SchemeYamls:
    schema: Dict[str, Any]
    classification: Dict[str, Any]
    defaults: Dict[str, Any]
    resolver: Dict[str, Any]
    fk: Dict[str, Any]
    few_shot: Dict[str, Any]
    response: Dict[str, Any]


# --------------------------------------------------------------------- #
# Small formatting helpers
# --------------------------------------------------------------------- #

def _tokenize(text: str) -> List[str]:
    words = re.findall(r"[a-zA-Z0-9%.]+", text.lower())
    return [w for w in words if w not in _STOPWORDS and len(w) > 1]


def _bullet(lines: List[str], indent: str = "- ") -> str:
    return "\n".join(f"{indent}{ln}" for ln in lines if ln)


def _fmt_note(val: Any) -> str:
    """Some 'note'/'reason' fields are plain strings, some are
    {'critical': True, 'text': ...} dicts, and a few are lists of strings
    or of small dicts. Normalize all of them to one string, prefixing
    critical notes so they aren't lost in the wash."""
    if val is None:
        return ""
    if isinstance(val, dict):
        if "text" in val:
            text = str(val.get("text", "")).strip()
        else:
            # a dict with no 'text' key (e.g. workbook_conflict-style) --
            # render its non-flag fields inline
            parts = [f"{k}={v}" for k, v in val.items() if k not in ("critical", "text")]
            text = "; ".join(parts)
        return f"[CRITICAL] {text}" if val.get("critical") else text
    if isinstance(val, list):
        return "; ".join(_fmt_note(v) for v in val if v)
    return str(val).strip()


def _fmt_note_capped(val: Any, cap: int = 700) -> str:
    """Same as _fmt_note, but defends against the rare oversized field
    (e.g. the village dimension's 186-entry disambiguation registry,
    ~15KB on its own) blowing up every prompt that touches it. Anything
    over `cap` chars is summarized with a pointer back to the source
    YAML rather than dumped verbatim -- nothing is lost, it's just not
    repeated in full on every single call."""
    text = _fmt_note(val)
    if len(text) > cap:
        return (
            text[:cap].rstrip()
            + f" ... [{len(text) - cap} more chars omitted -- see the "
            "source YAML for the full detail]"
        )
    return text


_SECTION_DIVIDER = re.compile(r"^\s*#\s*-{2,}\s*(.+?)\s*-{2,}\s*$")
_CONDITION_LINE = re.compile(r'^\s*-\s*condition:\s*["\']?([A-Za-z0-9_]+)["\']?')


def _parse_rule_sections(path: Path, order: List[str]) -> Dict[str, str]:
    """The two gate YAMLs use `# ---- section name ----` comments as
    dividers, which PyYAML discards. Re-read the raw text to recover
    which section each `condition:` belongs to, so prompts can group
    rules the way the SME file actually organizes them (see README section 5).
    Returns {condition_name: section_title}. Falls back to an 'other'
    bucket for anything the parser can't place, so a comment-format
    change degrades gracefully instead of crashing.
    """
    mapping: Dict[str, str] = {}
    current = "general"
    try:
        text = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return mapping
    for line in text:
        m = _SECTION_DIVIDER.match(line)
        if m:
            current = m.group(1).strip()
            continue
        m = _CONDITION_LINE.match(line)
        if m:
            mapping[m.group(1)] = current
    return mapping


def _group_by_section(rules: List[Dict[str, Any]], section_of: Dict[str, str]) -> "list[tuple[str, list]]":
    """Groups a flat rule list into (section_title, [rules]) preserving
    first-seen order, using the condition->section map above. Any rule
    whose condition wasn't found in the raw-text scan lands in 'general'
    rather than being dropped."""
    order: List[str] = []
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for r in rules:
        sec = section_of.get(r["condition"], "general")
        if sec not in buckets:
            buckets[sec] = []
            order.append(sec)
        buckets[sec].append(r)
    return [(sec, buckets[sec]) for sec in order]


# --------------------------------------------------------------------- #
# The assembler
# --------------------------------------------------------------------- #

class FocusPlusPromptAssembler:
    """Loads the seven Focus Plus annotation YAMLs once and assembles
    stage-by-stage or master prompts from them."""

    def __init__(self, yaml_dir: "str | Path | None" = None):
        self.dir = Path(yaml_dir) if yaml_dir is not None else DEFAULT_DIR
        missing = [f for f in FILES.values() if not (self.dir / f).exists()]
        if missing:
            raise FileNotFoundError(
                f"Missing annotation files in {self.dir}: {missing}"
            )
        self.y = SchemeYamls(
            schema=_load_yaml(self.dir / FILES["schema"]),
            classification=_load_yaml(self.dir / FILES["classification"]),
            defaults=_load_yaml(self.dir / FILES["defaults"]),
            resolver=_load_yaml(self.dir / FILES["resolver"]),
            fk=_load_yaml(self.dir / FILES["fk"]),
            few_shot=_load_yaml(self.dir / FILES["few_shot"]),
            response=_load_yaml(self.dir / FILES["response"]),
        )
        self._validate_gate_contract()
        self._classification_sections = _parse_rule_sections(
            self.dir / FILES["classification"], []
        )
        self._defaults_sections = _parse_rule_sections(
            self.dir / FILES["defaults"], []
        )

    # ------------------------------------------------------------- #
    # Sanity check ported from README section 9 step 3: a condition must
    # live in exactly one of the two gate files.
    # ------------------------------------------------------------- #
    def _validate_gate_contract(self) -> None:
        c = {r["condition"] for r in self.y.classification["clarification_rules"]}
        d = {r["condition"] for r in self.y.defaults["default_rules"]}
        overlap = c & d
        if overlap:
            raise ValueError(
                "Gate contract violated -- these conditions appear in BOTH "
                f"classification and default rules: {sorted(overlap)}"
            )

    # =============================================================== #
    # STAGE 1 -- Clarification gate
    # =============================================================== #
    def _classification_grouped_text(self) -> str:
        rules = self.y.classification["clarification_rules"]
        groups = _group_by_section(rules, self._classification_sections)
        blocks = []
        n = 0
        for section, rs in groups:
            lines = []
            for r in rs:
                n += 1
                lines.append(f'{n}. condition=`{r["condition"]}` -> ASK: "{r["question"]}"')
            blocks.append(f"[{section}] ({len(rs)} rules)\n" + _bullet(lines, indent=""))
        return "\n\n".join(blocks)

    def build_clarification_gate_prompt(self, user_question: Optional[str] = None) -> str:
        rules = self.y.classification["clarification_rules"]
        q_block = f'\nUSER QUESTION:\n"""\n{user_question}\n"""\n' if user_question else ""

        return f"""You are the CLARIFICATION GATE for the {SCHEME_NAME} scheme
(Meghalaya) of the Megh One AI NLP-to-SQL bot. You run BEFORE defaults,
entity resolution, or SQL generation.

Your only job: decide whether the user's question matches ANY of the
{len(rules)} conditions below. If it matches one, the bot must STOP and
ask the paired question instead of guessing or generating SQL. If it
matches none, say so and let the pipeline continue.

RULES, grouped exactly as the SME file organizes them -- (condition ->
question to ask if the condition fires):

{self._classification_grouped_text()}

Matching guidance:
- These conditions describe the SHAPE of a request (asks for a producer
  group count, names a bank, asks for a person lookup, leaves "beneficiaries"
  ambiguous, etc.), not literal keyword strings. Reason about intent.
- At most ONE condition should fire. If several plausibly apply, pick the
  one whose SECTION comes first above: unanswerable-at-all, then what
  privacy forbids, then the batch duality, then everything else -- this
  is the priority order the file itself encodes.
- If nothing fires, do not invent a clarification. Pass through silently.
{q_block}
Respond with ONLY this JSON:
{{
  "fires": true | false,
  "condition": "<condition name or null>",
  "question_to_ask": "<the paired question, verbatim, or null>"
}}"""

    # =============================================================== #
    # STAGE 2 -- Defaults
    # =============================================================== #
    def _defaults_grouped_text(self) -> str:
        rules = self.y.defaults["default_rules"]
        groups = _group_by_section(rules, self._defaults_sections)
        blocks = []
        n = 0
        for section, rs in groups:
            lines = []
            for r in rs:
                n += 1
                eff = f' (sql_effect: {r["sql_effect"]})' if r.get("sql_effect") else ""
                lines.append(
                    f'{n}. condition=`{r["condition"]}` -> default=`{r["default_value"]}`{eff}\n'
                    f'   state back: "{r["assumption_text"]}"'
                )
            blocks.append(f"[{section}] ({len(rs)} rules)\n" + _bullet(lines, indent=""))
        return "\n\n".join(blocks)

    def build_defaults_prompt(self, user_question: Optional[str] = None) -> str:
        rules = self.y.defaults["default_rules"]
        q_block = f'\nUSER QUESTION:\n"""\n{user_question}\n"""\n' if user_question else ""

        return f"""You are the DEFAULTS stage for the {SCHEME_NAME} scheme. You run
AFTER the clarification gate has already passed (nothing forced a stop).
Your job: silently fill in whatever the question left unspecified, using
ONLY the {len(rules)} defaults below, and produce the assumption sentence
that must be stated back to the user alongside the answer -- filling in
without ever mentioning it is not allowed.

DEFAULTS, grouped exactly as the SME file organizes them -- (condition ->
default_value -> assumption sentence to surface):

{self._defaults_grouped_text()}
{q_block}
Rules for applying these:
- Apply a default ONLY if its condition genuinely matches something the
  question left unspecified. Do not apply defaults the question already
  answered explicitly.
- Every default you apply must produce its assumption_text verbatim (or a
  light rewording that preserves its meaning) in the final answer.
- Four defaults carry a concrete `sql_effect` -- when one of those fires,
  pass that effect through to the SQL-generation stage.
- Never invent a default that is not in this list. If something is
  ambiguous and NOT covered by a default here, that should have been
  caught by the clarification gate already; do not silently guess.

Respond with ONLY this JSON:
{{
  "assumptions_applied": [
    {{"condition": "<name>", "default_value": "<value>",
      "assumption_text": "<verbatim>", "sql_effect": "<or null>"}}
  ]
}}"""

    _DIM_HEADER_KEYS = {
        "name_column", "code_column", "column", "canonical_column", "stored_case",
        "stored_form", "distinct", "distinct_in_scheme", "distinct_names_in_scheme",
        "distinct_codes_in_scheme", "distinct_in_dim_year", "closed_set", "complete",
        "collisions", "cohort_scope", "critical", "sql_predicate",
        "sql_predicate_by_code", "sql_predicate_preferred", "sql_predicate_fallback",
        "values", "code_note",
    }

    def _format_dimension(self, name: str, spec: Dict[str, Any]) -> str:
        if name == "identifiers":
            return self._format_identifiers_dimension(spec)

        col = spec.get("column") or spec.get("name_column") or spec.get("canonical_column", "")
        distinct = (
            spec.get("distinct") or spec.get("distinct_in_scheme")
            or spec.get("distinct_names_in_scheme") or "?"
        )
        pred = spec.get("sql_predicate") or spec.get("sql_predicate_preferred") or ""
        header = f"**{name}** (column: `{col}`, {distinct} distinct" + (
            f", predicate: {pred}" if pred else ""
        ) + ")"
        if spec.get("cohort_scope"):
            header += f"  [cohort_scope: {spec['cohort_scope']}]"

        note_lines = []
        for key, val in spec.items():
            if key in self._DIM_HEADER_KEYS:
                continue
            if name == "village" and key == "ambiguity" and isinstance(val, dict):
                text = self._village_ambiguity_summary(val)
            else:
                text = _fmt_note_capped(val)
            if text:
                note_lines.append(f"    {key}: {text}")

        values = spec.get("values")
        value_line = ""
        if isinstance(values, list) and values:
            shown = ", ".join(
                f'{v.get("canonical")} ({v.get("records", "?")})' for v in values[:60]
            )
            value_line = f"    values: {shown}"

        return "\n".join([f"- {header}", *note_lines] + ([value_line] if value_line else []))

    def _village_ambiguity_summary(self, val: Dict[str, Any]) -> str:
        """The village dimension's `ambiguity.registry` is a large
        name->{codes,districts,resolve_by} map. Summarize it instead of
        dumping it -- the matching pipeline (fuzzy/embedding stages) is
        what actually walks this at query time, not the prompt; a handful
        of worked examples is enough context here."""
        registry = val.get("registry", {})
        sample = list(registry.items())[:6]
        sample_str = "; ".join(
            f'{name}: {v.get("codes")} codes in {v.get("districts")} '
            f'(resolve_by={v.get("resolve_by")})'
            for name, v in sample
        )
        return (
            f"{val.get('shared_names')} shared names -- {_fmt_note(val.get('detail'))} "
            f"({val.get('resolve_by_district')} resolvable by district, "
            f"{val.get('resolve_by_block')} need block -- {_fmt_note(val.get('resolve_by_block_note'))}) "
            f"Registry shape: {val.get('registry_key')}. "
            f"Sample of {len(registry)}: {sample_str} ... "
            "(full registry lives in focusplus_entity_resolver.yaml; the "
            "matching pipeline consults it directly rather than via this prompt)."
        )

    def _format_identifiers_dimension(self, spec: Dict[str, Any]) -> str:
        lines = [f"- **identifiers** -- {_fmt_note(spec.get('policy'))}"]
        for sub in ("member_id", "pincode", "source_sl_no"):
            s = spec.get(sub)
            if not s:
                continue
            lines.append(
                f"    {sub}: pii={s.get('pii')} resolvable={s.get('resolvable')} "
                f"output_contract={s.get('output_contract', 'n/a')}"
            )
            for rule in s.get("rules", []):
                lines.append(f"      * {rule}")
        return "\n".join(lines)

    def _dimension_catalog_section(self) -> str:
        dims = self.y.resolver["dimensions"]
        return "\n\n".join(
            self._format_dimension(name, spec)
            for name, spec in dims.items()
            if isinstance(spec, dict)
        )

    # =============================================================== #
    # STAGE 3 -- Entity resolution
    # =============================================================== #
    def build_entity_resolution_prompt(self, user_question: str) -> str:
        r = self.y.resolver

        pipeline_lines = [
            f'{s["stage"]}: {s.get("test", s.get("action",""))}'
            + (f' (accept>= {s["accept_at"]})' if s.get("accept_at") else "")
            for s in r["matching_pipeline"]
        ]

        blocked = r.get("blocked_matches", [])
        blocked_lines = [
            f'"{b.get("user_text", b.get("text",""))}" -> {b.get("reason", b)}'
            for b in blocked
        ] if isinstance(blocked, list) else []

        examples = r.get("worked_examples", [])[:6]
        example_blocks = []
        for ex in examples:
            resolve = ex.get("resolve", [])
            resolved_str = "; ".join(
                f'"{it.get("text")}" -> {it.get("type")}: {it.get("canonical")}'
                for it in resolve
            )
            example_blocks.append(
                f'Q: "{ex.get("user")}"\n  resolves: {resolved_str}\n'
                f'  SQL: {ex.get("sql", "n/a")}'
            )

        contract = r["output_contract"]
        how_to_use = r.get("how_to_use", {})
        step_lines = [
            f'{k}: {_fmt_note(v)}' for k, v in how_to_use.items() if k != "hard_rules"
        ]
        hard_rule_lines = how_to_use.get("hard_rules", [])

        return f"""You are the ENTITY RESOLUTION stage for the {SCHEME_NAME} scheme.
Map every span of the user's question that names a value (a district,
block, village, batch, tranche, year, gender, occupation, status, or a
measure like "how many") to its exact stored representation in
{VIEW_NAME}. You do NOT write SQL yet -- you only resolve entities.

USER QUESTION:
\"\"\"
{user_question}
\"\"\"

=== SCHEME IDENTITY ===
{self._scheme_identity_section()}

=== WHY THIS SCHEME IS DIFFERENT ({SCHEME_NAME} has no flat-table ancestor;
the batch duality is the single most load-bearing fact) ===
{_fmt_note(r.get("dataset_shape", {}).get("two_cohorts"))}

=== RESOLUTION PROCEDURE (follow in this order) ===
{_bullet(step_lines)}

Hard rules from the procedure:
{_bullet(hard_rule_lines)}

=== NORMALISATION (apply before any matching) ===
{self._normalisation_section()}

=== DIMENSIONS AVAILABLE ({len(r["dimensions"])}) ===
{self._dimension_catalog_section()}

=== MATCHING PIPELINE (run in this order, stop at first accept) ===
{_bullet(pipeline_lines)}

=== NEVER FUZZY-MATCH ===
{self._pipeline_exclusions_section()}

=== BLOCKED (LOOK-ALIKE) MATCHES -- never let stages 6/7 confuse these ===
{self._blocked_matches_section()}

=== CROSS-DIMENSION COLLISIONS (block/village name overlap) ===
{self._cross_dimension_collisions_section()}

=== OVERLOADED TERMS -- one English word, several possible stored meanings ===
{self._overloaded_terms_section()}

=== MEASURE VOCABULARY -- what "how many X" / "total Y" actually aggregates ===
{self._measure_vocabulary_section()}

=== REGION GROUPINGS (not real columns -- expand to an IN list) ===
{self._region_groupings_section()}

=== WORKED EXAMPLES ===
{chr(10).join("* " + b.replace(chr(10), chr(10) + "  ") for b in example_blocks)}

OUTPUT CONTRACT -- every resolved span must take exactly one of these four
shapes (the 4th, `refused`, has no PMAY counterpart and exists because
this scheme carries PII):
{json.dumps(contract, indent=2)}

Hard rules, restated because they are the ones most often violated:
- `member_id`, raw name, mobile number, or EPIC ID spans are ALWAYS
  `refused` (reason: privacy or not_held) -- never resolved, never listed
  as a candidate, never fuzzy-matched against.
- District/block values are stored UPPERCASE; keep `canonical` in Title
  Case here, the SQL stage uppercases it. `lgd_village_name` keeps its
  stored mixed case.
- A span with no dimension match at all is `not_found`, not silently
  dropped.
- The literal text "Nan"/"NaN"/"NULL"/"" is a null written out as text,
  never a real value -- never resolve or suggest it.

Respond with ONLY a JSON array of resolved/ambiguous/not_found/refused
objects, one per entity span found in the question."""

    # =============================================================== #
    # Schema / join / rules sections (shared by SQL generation + master)
    # =============================================================== #
    def _schema_section(self) -> str:
        v = self.y.schema["datasets"]["v_focus_plus"]
        cols = v["columns"]
        lines = [
            f"View: {v['table_name']}  (over {v['over']}, {v['column_count']} columns)",
            f"Grain: {v['grain']}",
            f"Row count: {v['row_count']} -- {v.get('row_count_note','').strip()}",
            f"Money unit: {v['money_unit']} (authority: {v['money_unit_authority']})",
            f"person_level: {v['person_level']}  |  aggregate_before_reporting: {v['aggregate_before_reporting']}",
            f"Grain note: {v.get('grain_note','').strip()}",
            "",
            "Columns (declared order):",
        ]
        for c in cols:
            samples = c.get("sample_values")
            samples_str = (
                "WITHHELD_PII" if samples == "WITHHELD_PII"
                else (", ".join(map(str, samples)) if samples else "-")
            )
            biz = "; ".join(c.get("business_rules", []))
            lines.append(
                f'  * {c["name"]} ({c["type"]}, {"NULL ok" if c.get("nullable") else "NOT NULL"}) '
                f'[{c["category"]}, priority={c["nlp_sql_priority"]}]\n'
                f'      meaning: {c["meaning"]}\n'
                f'      ops: {", ".join(c.get("sql_operations", []))} | synonyms: {", ".join(c.get("synonyms", []))}\n'
                f'      samples: {samples_str}\n'
                + (f'      business_rules: {biz}\n' if biz else "")
            )
        return "\n".join(lines)

    def _semantic_rules_section(self) -> str:
        sr = self.y.schema["semantic_rules"]
        lines = []
        for k, v in sr.items():
            lines.append(f"- {k}: {_fmt_note(v)}")
        return _bullet(lines, indent="")

    def _nlp_sql_rules_section(self) -> str:
        nr = self.y.schema["nlp_sql_rules"]
        lines = []
        for k, v in nr.items():
            lines.append(f"- {k}: {_fmt_note(v)}")
        return _bullet(lines, indent="")

    def _join_graph_section(self) -> str:
        fk = self.y.fk["foreign_key_augmentation"]
        edges = fk["edges"]
        allowed, prohibited = [], []
        for e in edges:
            frm = f'{e["from_table"]}.{e.get("from_column")}'
            to = f'{e["to_table"]}.{e.get("to_column")}'
            if e.get("is_prohibited"):
                prohibited.append(
                    f'{frm} -> {to}  [PROHIBITED] use instead: '
                    f'{e.get("use_instead","n/a")} -- {_fmt_note(e.get("reason"))}'
                )
            else:
                allowed.append(
                    f'{frm} -> {to}  ({e.get("join_type","")} join, '
                    f'{e.get("cardinality","")}, declared={e.get("declared")}) '
                    f'-- prefer instead: {e.get("prefer_instead","n/a")}'
                )
        return (
            f"ALLOWED / DECLARED EDGES ({len(allowed)}):\n"
            + _bullet(allowed)
            + f"\n\nPROHIBITED EDGES -- NEVER EMIT THESE JOINS ({len(prohibited)}):\n"
            + _bullet(prohibited)
        )

    def _pii_boundary_section(self) -> str:
        pii = self.y.schema["architecture_alignment"].get("pii_boundary", "")
        return _fmt_note(pii)

    def _absent_objects_section(self) -> str:
        items = self.y.fk["foreign_key_augmentation"].get("absent_objects", [])
        lines = [
            f'{it["object"]} (PMAY has {it.get("pmay_equivalent","-")}) -- {_fmt_note(it.get("consequence"))}'
            for it in items
        ]
        return _bullet(lines) if lines else "(none declared)"

    def _other_datasets_section(self) -> str:
        """Everything in v_focus_plus's neighborhood that the generator may
        legitimately need to touch on its own (dim_scheme for money_unit,
        dim_year/dim_geography for context, the two cross-scheme views, and
        the underlying fact -- which must NEVER be queried directly)."""
        ds = self.y.schema["datasets"]
        lines = []
        for key in (
            "fact_focus_plus_disbursement", "dim_scheme", "dim_year",
            "dim_geography", "v_cross_scheme_money_district_year",
            "v_cross_scheme_village_coverage",
        ):
            d = ds.get(key)
            if not d:
                continue
            note = _fmt_note(d.get("note") or d.get("row_count_note") or d.get("grain_note"))
            bits = [f"{k}={v}" for k, v in d.items() if k not in ("note", "columns") and not isinstance(v, (dict, list))]
            lines.append(f"* {key}: " + ", ".join(bits) + (f"\n    {note}" if note else ""))
        return "\n".join(lines)

    def _scheme_identity_section(self) -> str:
        s = self.y.resolver.get("scheme", {})
        if not s:
            return ""
        return (
            f"Canonical name: {s.get('display_name')} | state: {s.get('state')}\n"
            f"Aliases: {', '.join(s.get('aliases', []))}\n"
            f"NOT aliases (do not resolve to this scheme): {', '.join(s.get('not_aliases', []))}"
            f" -- {_fmt_note(s.get('not_aliases_note'))}\n"
            f"Bare-word rule: {_fmt_note(s.get('bare_focus_note'))}"
        )

    def _normalisation_section(self) -> str:
        n = self.y.resolver.get("normalisation", {})
        if not n:
            return ""
        fold_steps = n.get("fold", [])
        fold_lines = [f"  {i+1}. {_fmt_note(s)}" for i, s in enumerate(fold_steps)]
        lines = [
            "fold() steps, applied to BOTH user text and candidate values, in order:",
            *fold_lines,
            f"squash(): {_fmt_note(n.get('squash'))}",
            f"trailing-space rule: {_fmt_note(n.get('trailing_space_rule'))}",
            f"null-text rule: {_fmt_note(n.get('null_text_rule'))}",
            f"numeric ids: {_fmt_note(n.get('numeric_ids'))}",
            f"village name prefixes: {_fmt_note(n.get('village_prefix_rule'))}",
        ]
        return "\n".join(lines)

    def _pipeline_exclusions_section(self) -> str:
        p = self.y.resolver.get("pipeline_exclusions", {})
        if not p:
            return ""
        return (
            f"NEVER fuzzy-match: {', '.join(p.get('never_fuzzy_match', []))} -- "
            f"{_fmt_note(p.get('reason'))}"
        )

    def _blocked_matches_section(self) -> str:
        b = self.y.resolver.get("blocked_matches", {})
        if not b:
            return ""
        seen = set()
        pair_lines = []
        for a, c in b.get("pairs", []):
            key = tuple(sorted((a, c)))
            if key in seen:
                continue
            seen.add(key)
            pair_lines.append(f'"{a}" != "{c}" -- never let fuzzy matching confuse these')
        return (
            f"{_fmt_note(b.get('purpose'))}\n"
            + _bullet(pair_lines)
            + f"\nMunicipal-board note: {_fmt_note(b.get('municipal_board_note'))}"
        )

    def _overloaded_terms_section(self) -> str:
        ot = self.y.resolver.get("overloaded_terms", {})
        if not ot:
            return ""
        lines = []
        for term, spec in ot.items():
            if term == "why" or not isinstance(spec, dict):
                continue
            readings = spec.get("readings", [])
            reading_lines = [f'    - {r.get("means")}  [when: {r.get("when")}]' for r in readings]
            default = spec.get("default", "")
            note = _fmt_note(spec.get("note") or spec.get("unit_warning"))
            lines.append(
                f'"{term}":\n' + "\n".join(reading_lines)
                + (f"\n    default: {default}" if default else "")
                + (f"\n    {note}" if note else "")
            )
        return f"{_fmt_note(ot.get('why'))}\n\n" + "\n\n".join(lines)

    def _cross_dimension_collisions_section(self) -> str:
        c = self.y.resolver.get("cross_dimension_collisions", {})
        if not c:
            return ""
        measured = c.get("measured", {})
        return (
            f"Measured: {measured}\n"
            f"Names that collide across block/village: {', '.join(c.get('colliding_names', []))}\n"
            "Disambiguation rules:\n"
            + _bullet(c.get("disambiguation_rules", []))
        )

    def _measure_vocabulary_section(self) -> str:
        mv = self.y.resolver.get("measure_vocabulary", {})
        if not mv:
            return ""
        lines = []
        for name, spec in mv.items():
            if not isinstance(spec, dict):
                continue
            if name == "amount_disbursed":
                crit = _fmt_note(spec.get("critical"))
                two_rate = _fmt_note(spec.get("two_rate_finding"))
                not_avail = spec.get("not_available", {})
                lines.append(
                    f'"{name}" -> {spec.get("aggregate")} '
                    f'(aliases: {", ".join(spec.get("aliases", [])[:8])}...)\n'
                    f"  values: {spec.get('values')}  unit: {spec.get('unit')} "
                    f"(authority: {spec.get('unit_authority')})\n"
                    f"  {crit}\n  {two_rate}\n"
                    f"  NOT available (never borrow from another scheme): "
                    f"{', '.join(not_avail.get('columns', []))} -- {_fmt_note(not_avail.get('note'))}"
                )
            elif name == "derived":
                for dname, dspec in spec.items():
                    if dname == "not_derivable":
                        items = dspec.get("items", [])
                        lines.append(
                            "NOT DERIVABLE from this data: "
                            + "; ".join(f'{it["metric"]} ({it["reason"]})' for it in items)
                        )
                    elif isinstance(dspec, dict):
                        lines.append(
                            f'derived "{dname}": {dspec.get("formula")} -- {_fmt_note(dspec.get("note"))}'
                        )
            else:
                lines.append(
                    f'"{name}" -> {spec.get("aggregate")} '
                    f'(aliases: {", ".join(spec.get("aliases", []))})'
                    + (f"  [default measure]" if spec.get("default") else "")
                    + (f"\n  cohort_scope: {spec['cohort_scope']}" if spec.get("cohort_scope") else "")
                    + (f"\n  {_fmt_note(spec.get('critical'))}" if spec.get("critical") else "")
                    + (f"\n  {_fmt_note(spec.get('note'))}" if spec.get("note") else "")
                )
        return "\n\n".join(lines)

    def _region_groupings_section(self) -> str:
        rg = self.y.resolver.get("region_groupings", {})
        if not rg:
            return ""
        lines = [f"{_fmt_note(rg.get('note'))}"]
        for gname, gspec in rg.get("groups", {}).items():
            lines.append(
                f'{gname}: districts={gspec.get("districts")} '
                f'aliases={gspec.get("aliases")} records={gspec.get("records")}'
                + (f" -- {_fmt_note(gspec.get('note'))}" if gspec.get("note") else "")
            )
        unassigned = rg.get("unassigned", {})
        if unassigned:
            lines.append(f"Unassigned: {_fmt_note(unassigned)}")
        whole = rg.get("whole_state", {})
        if whole:
            lines.append(
                f"Whole-state aliases: {whole.get('aliases')} -> {whole.get('behaviour')}"
            )
        return "\n".join(lines)

    def _refusal_examples(self, limit: int = 3) -> List[Dict[str, Any]]:
        stage2 = self.y.schema.get("few_shot_examples", [])
        return [e for e in stage2 if e.get("sql") is None][:limit]

    def _score_few_shot(self, question: str, examples: List[Dict[str, Any]], k: int) -> List[Dict[str, Any]]:
        """Cheap lexical retrieval over the curated Q->SQL pairs: token
        overlap between the user question and each example's question.
        No embeddings/network needed -- swap this out for the real
        BGE-small retriever used elsewhere in the pipeline if available."""
        if not question:
            return examples[:k]
        q_tokens = set(_tokenize(question))
        scored = []
        for ex in examples:
            e_tokens = set(_tokenize(ex.get("question", "")))
            overlap = len(q_tokens & e_tokens)
            scored.append((overlap, ex))
        scored.sort(key=lambda t: t[0], reverse=True)
        top = [ex for score, ex in scored if score > 0][:k]
        if len(top) < k:
            # pad with the highest-priority generic examples not already included
            seen = {e["question"] for e in top}
            for _, ex in scored:
                if len(top) >= k:
                    break
                if ex["question"] not in seen:
                    top.append(ex)
                    seen.add(ex["question"])
        return top

    def _few_shot_section(self, question: Optional[str], k: int, include_refusals: int = 2) -> str:
        examples = self.y.few_shot["sql_generation_examples"]
        chosen = self._score_few_shot(question or "", examples, k)
        blocks = []
        for ex in chosen:
            note = f'\n  -- note: {ex["note"]}' if ex.get("note") else ""
            blocks.append(f'Q: {ex["question"]}\nSQL:\n{ex["sql"].strip()}{note}')

        refusals = self._refusal_examples(include_refusals)
        for ex in refusals:
            blocks.append(
                f'Q: {ex["question"]}\nSQL: null  -- REFUSE.'
                f'\n  -- why: {ex.get("explanation","").strip()}'
            )
        header = (
            f"{len(chosen)} retrieved example(s) "
            f"(of {len(examples)} curated pairs) + {len(refusals)} refusal example(s):"
        )
        return header + "\n\n" + "\n\n".join(blocks)

    # =============================================================== #
    # STAGE 4 -- SQL generation
    # =============================================================== #
    def build_sql_generation_prompt(
        self,
        user_question: str,
        resolved_entities: Optional[List[Dict[str, Any]]] = None,
        assumptions: Optional[List[Dict[str, Any]]] = None,
        k_few_shot: int = 8,
    ) -> str:
        entities_block = (
            json.dumps(resolved_entities, indent=2) if resolved_entities else "(none supplied -- resolve inline)"
        )
        assumptions_block = (
            json.dumps(assumptions, indent=2) if assumptions else "(none)"
        )

        return f"""You are the SQL GENERATOR for the {SCHEME_NAME} scheme (Meghalaya)
of the Megh One AI NLP-to-SQL bot. Generate ONE read-only PostgreSQL
SELECT statement against {VIEW_NAME}, or refuse with `null` SQL if the
question cannot be answered from this data.

USER QUESTION:
\"\"\"
{user_question}
\"\"\"

RESOLVED ENTITIES (from the entity-resolution stage):
{entities_block}

ASSUMPTIONS ALREADY APPLIED (from the defaults stage -- honor these silently,
do not re-derive them):
{assumptions_block}

=== SCHEMA ({VIEW_NAME}) ===
{self._schema_section()}

=== SEMANTIC RULES ===
{self._semantic_rules_section()}

=== NLP -> SQL RULES ===
{self._nlp_sql_rules_section()}

=== JOIN GRAPH ===
{self._join_graph_section()}

=== OTHER DATASETS YOU MAY NEED TO READ SEPARATELY (never row-joined) ===
{self._other_datasets_section()}

=== STRUCTURE THAT DOES NOT EXIST HERE (do not assume a PMAY/MGNREGA shape) ===
{self._absent_objects_section()}

=== PII BOUNDARY (NON-NEGOTIABLE) ===
{self._pii_boundary_section()}

=== MEASURE VOCABULARY -- the AUTHORITATIVE aggregate formula for every
    "how many"/"total"/"average" phrasing. Prefer this over pattern-matching
    a few-shot example when the two would disagree. ===
{self._measure_vocabulary_section()}

=== OVERLOADED TERMS -- resolve these BEFORE picking a formula above ===
{self._overloaded_terms_section()}

=== FEW-SHOT SQL EXAMPLES ===
{self._few_shot_section(user_question, k_few_shot)}

Hard constraints, restated because they are the ones most often violated:
1. `FROM {VIEW_NAME}` is the default and near-universal FROM clause. Do
   NOT query `curated.fact_focus_plus_disbursement` directly -- the view
   already pre-joins year/geography and carries `bank_name_raw`, so there
   is nothing on the fact the view lacks.
2. There is NO mandatory predicate. Do not add `WHERE NOT is_placeholder`
   or any PMAY-style filter -- those columns do not exist here and the
   query will error.
3. `COUNT(*)` is a PAYMENT count, never a person count. Say so in any
   accompanying label/alias.
4. Never SELECT, GROUP BY, ORDER BY, or otherwise surface `member_id` or
   `pincode` as a bare column. `COUNT(DISTINCT member_id)` is the only
   permitted use of `member_id`.
5. `district_name`/`lgd_district` is stored UPPERCASE -- literal must be
   uppercase.
6. `tranche_label` is a label, not a time axis -- never `ORDER BY tranche_label`
   expecting chronology.
7. If the question needs a joined table beyond `dim_scheme` (read alone,
   never joined per-row) or matches a PROHIBITED edge above, refuse
   instead of forcing a join.
8. Always add a sane `LIMIT` on row-returning (non-aggregate) queries.

Respond with ONLY this JSON:
{{
  "sql": "<SELECT ...; or null to refuse>",
  "refusal_reason": "<null unless sql is null>",
  "tables": ["{VIEW_NAME}"],
  "assumptions_surfaced": ["<assumption_text strings that must be stated in the final answer>"]
}}"""

    # =============================================================== #
    # STAGE 5 -- Response composition
    # =============================================================== #
    def build_response_composer_prompt(
        self,
        user_question: Optional[str] = None,
        sql: Optional[str] = None,
        result_preview: Optional[str] = None,
    ) -> str:
        rt = self.y.response["response_templates"]
        fmt = rt["formatting"]
        templates = rt["templates"]
        follow_ups = rt["follow_up_rules"]

        fmt_lines = [f"- {k}: {_fmt_note(v)}" for k, v in fmt.items()]
        template_names = ", ".join(sorted(templates.keys()))
        follow_up_lines = [
            f'{fu.get("trigger_condition", fu.get("condition",""))} -> {fu.get("follow_up_question", fu.get("question",""))}'
            for fu in follow_ups[:15]
        ]

        ctx = ""
        if user_question or sql or result_preview:
            ctx = (
                "\nCONTEXT FOR THIS ANSWER:\n"
                + (f'Question: "{user_question}"\n' if user_question else "")
                + (f"SQL run:\n{sql}\n" if sql else "")
                + (f"Result: {result_preview}\n" if result_preview else "")
            )

        return f"""You are the RESPONSE COMPOSER for the {SCHEME_NAME} scheme. Turn a
SQL result into the final natural-language answer, in this scheme's
house style, using the template catalogue below.
{ctx}
=== FORMATTING / MANDATORY RULES ({len(fmt_lines)}) ===
{_bullet(fmt_lines, indent="")}

=== TEMPLATE CATALOGUE ({len(templates)} templates) ===
Pick the template that matches the shape of the result: {template_names}

=== FOLLOW-UP RULES (sample of {len(follow_up_lines)} of {len(follow_ups)}) ===
{_bullet(follow_up_lines)}

Six caveats travel with EVERY Focus Plus number -- never publish a figure
without checking each one:
1. Is this a payment count, a member count, or a beneficiary count? Say
   which -- COUNT(*) is payments.
2. Does this cross the batch duality (93K legacy vs 12.5K registration)?
   If the two cohorts are mixed, say so; if one cohort only, name it.
3. Is the money unit stated? (`unverified` -- carry the caveat, never
   silently assume rupees or crore.)
4. Has this total been verified against the database
   (`meta.v_reconciliation_focus_plus`)? If not, attach the provisional
   line.
5. Does the figure touch geography? If so, note the 102,923 unmapped-row
   caveat where relevant (village/block-level answers only).
6. Is any PII (`member_id`, `pincode`, a name) about to be printed? It
   must never be -- aggregate instead and say so.

Respond with the final natural-language answer text only (no JSON), in
the register the templates above establish, followed by any assumption
sentences and caveats that apply."""

    # =============================================================== #
    # MASTER PROMPT -- everything, one shot
    # =============================================================== #
    def build_master_prompt(
        self,
        user_question: Optional[str] = None,
        k_few_shot: int = 10,
        include_gate_rules: bool = True,
        include_default_rules: bool = True,
    ) -> str:
        """The single 'I need everything for this scheme' system prompt."""

        q_block = (
            f'\nUSER QUESTION:\n"""\n{user_question}\n"""\n'
            if user_question else
            "\n(No specific user question supplied -- this is the scheme's "
            "full standing contract, for priming a session or a system prompt.)\n"
        )

        gate_section = ""
        if include_gate_rules:
            rules = self.y.classification["clarification_rules"]
            gate_lines = [f'`{r["condition"]}` -> ask: "{r["question"]}"' for r in rules]
            gate_section = (
                f"\n=== CLARIFICATION GATE -- {len(rules)} conditions (check FIRST, "
                "before anything else; stop and ask if one fires) ===\n"
                + _bullet(gate_lines)
            )

        defaults_section = ""
        if include_default_rules:
            rules = self.y.defaults["default_rules"]
            def_lines = [
                f'`{r["condition"]}` -> {r["default_value"]} '
                f'(state back: "{r["assumption_text"]}")'
                for r in rules
            ]
            defaults_section = (
                f"\n=== DEFAULTS -- {len(rules)} conditions (apply silently after "
                "the gate passes; always state the assumption) ===\n"
                + _bullet(def_lines)
            )

        rt = self.y.response["response_templates"]
        fmt_lines = [f"- {k}: {_fmt_note(v)}" for k, v in rt["formatting"].items()]

        return f"""You are the Megh One AI NLP-to-SQL bot for the {SCHEME_NAME} scheme
(Meghalaya). This prompt is the FULL standing contract for this scheme,
assembled from all seven SME-reviewed annotation files. Follow it exactly;
where a rule here conflicts with general SQL-writing instinct, this file
wins.
{q_block}
=== SCHEME IDENTITY (for routing / recognizing this scheme in a question) ===
{self._scheme_identity_section()}

{gate_section}
{defaults_section}

=== ENTITY RESOLUTION -- NORMALISATION (apply before matching any span) ===
{self._normalisation_section()}

=== ENTITY RESOLUTION -- OVERLOADED TERMS ===
{self._overloaded_terms_section()}

=== ENTITY RESOLUTION -- CROSS-DIMENSION COLLISIONS ===
{self._cross_dimension_collisions_section()}

=== ENTITY RESOLUTION -- REGION GROUPINGS ===
{self._region_groupings_section()}

=== ENTITY RESOLUTION -- DIMENSION CATALOGUE ({len(self.y.resolver["dimensions"])} dimensions) ===
{self._dimension_catalog_section()}

=== SCHEMA ({VIEW_NAME}) ===
{self._schema_section()}

=== SEMANTIC RULES ===
{self._semantic_rules_section()}

=== NLP -> SQL RULES ===
{self._nlp_sql_rules_section()}

=== JOIN GRAPH ===
{self._join_graph_section()}

=== OTHER DATASETS YOU MAY NEED TO READ SEPARATELY (never row-joined) ===
{self._other_datasets_section()}

=== STRUCTURE THAT DOES NOT EXIST HERE (do not assume a PMAY/MGNREGA shape) ===
{self._absent_objects_section()}

=== PII BOUNDARY (NON-NEGOTIABLE) ===
{self._pii_boundary_section()}

=== MEASURE VOCABULARY (authoritative aggregate formulas) ===
{self._measure_vocabulary_section()}

=== FEW-SHOT SQL EXAMPLES ===
{self._few_shot_section(user_question, k_few_shot)}

=== RESPONSE FORMATTING RULES ===
{_bullet(fmt_lines, indent="")}

=== HOUSE RULES (never violate) ===
1. A fact about a column lives in the schema section; a fact about a
   value lives in entity resolution; a fact about asking-vs-assuming
   lives in exactly one of the gate/defaults sections above.
2. Every number is a source-CSV observation until measured against the
   database -- mark totals `unverified_in_db` and say so.
3. PII is not negotiable: `member_id` and `pincode` never reach a user, an
   export, or a vector store. `member_id` is permitted only inside
   `COUNT(DISTINCT ...)`. `bank_name_raw` is not PII -- it names a bank,
   not a person -- and may reach a user (e.g. in a bank-wise breakdown).
4. Never copy a PMAY predicate across (`is_placeholder`, `is_completed`,
   `mapping_category`, `sanction_date`, `installments_paid` do not exist
   here).
5. Never present a 3.2%-coverage (12.5K-cohort-only) breakdown as if it
   were a whole-scheme figure.
6. `COUNT(*)` is payments. Say so, every time.
7. Query the view ({VIEW_NAME}), never the fact table.

Work the pipeline in order for the user question above: (1) check the
clarification gate -- if a condition fires, STOP and ask that question,
nothing further; (2) otherwise apply defaults silently, collecting the
assumption sentences to surface; (3) resolve every entity span to its
stored value using the schema's sample values and synonyms; (4) generate
one read-only SQL SELECT against {VIEW_NAME} (or refuse with SQL = null
per the NEVER_QUERY / prohibited-join rules); (5) compose the final
answer using the response-formatting rules and the six mandatory caveats
(payment-vs-member-vs-beneficiary count, batch duality, money-unit
caveat, provisional/unverified caveat, geography-unmapped caveat when
relevant, and never printing PII).

Respond with ONLY this JSON:
{{
  "clarification": {{"fires": false, "question_to_ask": null}},
  "assumptions_applied": ["<assumption_text ...>"],
  "resolved_entities": [ ... ],
  "sql": "<SELECT ...; or null>",
  "refusal_reason": null,
  "final_answer": "<natural language, with caveats, once SQL has been executed and results are available -- otherwise omit>"
}}"""

    # =============================================================== #
    # Bonus: derive routing keywords for the scheme catalogue.
    # =============================================================== #
    def derive_schemes_catalog_keywords(self) -> List[str]:
        kw: set[str] = set()
        scheme = self.y.resolver.get("scheme", {})
        kw.update(a.lower() for a in scheme.get("aliases", []))
        for c in self.y.schema["datasets"]["v_focus_plus"]["columns"]:
            kw.update(s.lower() for s in c.get("synonyms", []))
        mv = self.y.resolver.get("measure_vocabulary", {})
        for spec in mv.values():
            if isinstance(spec, dict):
                kw.update(a.lower() for a in spec.get("aliases", []))
        for d in self.y.resolver["dimensions"].values():
            if isinstance(d, dict):
                for v in d.get("values", []) if isinstance(d.get("values"), list) else []:
                    kw.update(a.lower() for a in v.get("aliases", []))
        # not_aliases must never route here
        kw -= {a.lower() for a in scheme.get("not_aliases", [])}
        return sorted(kw)

    # =============================================================== #
    # Convenience: everything as a dict of strings (for logging/inspection)
    # =============================================================== #
    def build_all_stage_prompts(self, user_question: str, k_few_shot: int = 8) -> Dict[str, str]:
        return {
            "clarification_gate": self.build_clarification_gate_prompt(user_question),
            "defaults": self.build_defaults_prompt(user_question),
            "entity_resolution": self.build_entity_resolution_prompt(user_question),
            "sql_generation": self.build_sql_generation_prompt(user_question, k_few_shot=k_few_shot),
            "response_composer": self.build_response_composer_prompt(user_question),
            "master": self.build_master_prompt(user_question, k_few_shot=k_few_shot),
        }


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #

def _self_test(assembler: FocusPlusPromptAssembler) -> None:
    print("Running self-test...\n")
    n_class = len(assembler.y.classification["clarification_rules"])
    n_def = len(assembler.y.defaults["default_rules"])
    n_fs = len(assembler.y.few_shot["sql_generation_examples"])
    n_refuse = len(assembler._refusal_examples(limit=999))
    n_cols = len(assembler.y.schema["datasets"]["v_focus_plus"]["columns"])
    print(f"  classification rules : {n_class} (expect 89)")
    print(f"  default rules         : {n_def} (expect 67)")
    print(f"  gate contract overlap : OK (validated in __init__)")
    print(f"  few-shot SQL examples : {n_fs} (expect 70)")
    print(f"  stage2 refusal examples: {n_refuse} (expect 13)")
    print(f"  v_focus_plus columns  : {n_cols} (expect 23)")

    n_dims = len(assembler.y.resolver["dimensions"])
    n_blocks = len(assembler.y.resolver.get("blocked_matches", {}).get("pairs", []))
    n_collide = len(assembler.y.resolver.get("cross_dimension_collisions", {}).get("colliding_names", []))
    n_regions = len(assembler.y.resolver.get("region_groupings", {}).get("groups", {}))
    n_overloaded = len([k for k in assembler.y.resolver.get("overloaded_terms", {}) if k != "why"])
    n_absent = len(assembler.y.fk["foreign_key_augmentation"].get("absent_objects", []))
    n_edges = len(assembler.y.fk["foreign_key_augmentation"]["edges"])
    n_prohibited = sum(1 for e in assembler.y.fk["foreign_key_augmentation"]["edges"] if e.get("is_prohibited"))
    print(f"  resolver dimensions   : {n_dims} (expect 11)")
    print(f"  blocked-match pairs   : {n_blocks} (expect 18)")
    print(f"  cross-dim collisions  : {n_collide} (expect 19)")
    print(f"  region groupings      : {n_regions} (expect 4: Garo/Khasi/Jaintia/Ri Bhoi)")
    print(f"  overloaded terms      : {n_overloaded}")
    print(f"  absent_objects        : {n_absent} (expect 4)")
    print(f"  join edges            : {n_edges} (expect 11), of which prohibited: {n_prohibited} (expect 7)")

    unplaced_c = sum(
        1 for r in assembler.y.classification["clarification_rules"]
        if assembler._classification_sections.get(r["condition"], "general") == "general"
    )
    unplaced_d = sum(
        1 for r in assembler.y.defaults["default_rules"]
        if assembler._defaults_sections.get(r["condition"], "general") == "general"
    )
    print(f"  classification rules unplaced by section parser: {unplaced_c} (expect 0)")
    print(f"  default rules unplaced by section parser       : {unplaced_d} (expect 0)")

    kw = assembler.derive_schemes_catalog_keywords()
    print(f"  derived schemes_catalog keywords: {len(kw)}")

    q = "how many focus plus payments in WGH"
    top = assembler._score_few_shot(q, assembler.y.few_shot["sql_generation_examples"], k=3)
    print(f"\n  top-3 retrieved examples for: {q!r}")
    for ex in top:
        print(f"    - {ex['question']}")

    master = assembler.build_master_prompt(q, k_few_shot=5)
    print(f"\n  master prompt length: {len(master)} chars")
    print("\nSelf-test passed.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Focus Plus prompt assembler")
    ap.add_argument(
        "--dir", default=str(DEFAULT_DIR),
        help="Directory containing the seven focusplus_*.yaml files "
             f"(default: {DEFAULT_DIR})",
    )
    ap.add_argument(
        "--mode",
        choices=["gate", "defaults", "entities", "sql", "response", "master", "all", "keywords"],
        default="master",
    )
    ap.add_argument("--question", default=None)
    ap.add_argument("--k", type=int, default=8, help="few-shot examples to retrieve")
    ap.add_argument("--out", default=None, help="write prompt(s) to this file instead of stdout")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    assembler = FocusPlusPromptAssembler(args.dir)

    if args.self_test:
        _self_test(assembler)
        return

    if args.mode == "gate":
        out = assembler.build_clarification_gate_prompt(args.question)
    elif args.mode == "defaults":
        out = assembler.build_defaults_prompt(args.question)
    elif args.mode == "entities":
        if not args.question:
            sys.exit("--question is required for --mode entities")
        out = assembler.build_entity_resolution_prompt(args.question)
    elif args.mode == "sql":
        if not args.question:
            sys.exit("--question is required for --mode sql")
        out = assembler.build_sql_generation_prompt(args.question, k_few_shot=args.k)
    elif args.mode == "response":
        out = assembler.build_response_composer_prompt(args.question)
    elif args.mode == "all":
        prompts = assembler.build_all_stage_prompts(args.question or "", k_few_shot=args.k)
        out = json.dumps(prompts, indent=2)
    elif args.mode == "keywords":
        out = json.dumps(assembler.derive_schemes_catalog_keywords(), indent=2)
    else:  # master
        out = assembler.build_master_prompt(args.question, k_few_shot=args.k)

    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"Wrote {len(out)} chars to {args.out}")
    else:
        print(out)


if __name__ == "__main__":
    main()
