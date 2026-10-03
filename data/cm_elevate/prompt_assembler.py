"""
prompt_assembler.py  (CM Elevate)
==================================

Standalone prompt-assembler / validator for the **CM Elevate** scheme
(Meghalaya) of the Megh One AI NLP-to-SQL bot.

This module turns the seven hand-curated annotation YAMLs in this folder
(`data/cm_elevate/`) into the prompts each pipeline stage would send to an
LLM (schema linking, entity resolution, join discovery, the clarification
gate, defaults, few-shot retrieval, response composition), plus a
dependency-free regex-based SQL safety validator and one "everything"
master prompt.

NOTE ON THE LIVE SERVICE
------------------------
The running service does NOT import this module. CM Elevate is wired into
the uniform pipeline exactly like MGNREGA, PMAY-G and Focus Plus:
  * app/schema_context.py   - hand-written CM ELEVATE tables / rules / vocab
  * app/annotations.py      - loads cmelevate_few_shot.yaml + the FK graph
  * app/entity_resolver.py  - loads cmelevate_entity_resolver.yaml
  * app/pipeline.py         - scheme detection, clarification gate, no-time/no-money refusal
  * app/kb_ingest.py        - CM_Elevate_Complete_Reference.md -> RAG

This file is kept alongside the YAMLs as an OFFLINE tool: run its
`--self-test` to sanity-check the YAML contract after an edit, or use it to
inspect what a fully YAML-driven prompt for this scheme would look like.
It does NOT call an LLM itself.

Usage
-----
    from data.cm_elevate.prompt_assembler import CMElevateKB, assemble

    kb = CMElevateKB()          # defaults to this folder
    p = assemble(kb, "how many piggery applications are on hold?")
    print(p.sql_prompt)

CLI
---
    python data/cm_elevate/prompt_assembler.py --self-test
    python data/cm_elevate/prompt_assembler.py --mode master \
        --question "how many piggery applications are on hold?"
    python data/cm_elevate/prompt_assembler.py --mode sql \
        --question "gender split for the tourism vehicle scheme"
    python data/cm_elevate/prompt_assembler.py --mode gate \
        --question "how much was disbursed under the warehouse scheme"
    python data/cm_elevate/prompt_assembler.py --mode entities \
        --question "applications in EGH"
    python data/cm_elevate/prompt_assembler.py --mode response
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import difflib
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

import yaml

SCHEME_NAME = "CM Elevate"
VIEW_NAME = "curated.v_cm_elevate"

# This folder holds the seven annotation YAMLs (single source of truth -
# NOT duplicated as embedded strings in this file, so there is exactly one
# place to edit a rule).
DEFAULT_DIR = Path(__file__).resolve().parent

FILES = {
    "schema": "cmelevate_schema_partitions.yaml",
    "classification": "cmelevate_classification_rules.yaml",
    "defaults": "cmelevate_default_rules.yaml",
    "resolver": "cmelevate_entity_resolver.yaml",
    "fk": "cmelevate_foreign_key_augmentation.yaml",
    "few_shot": "cmelevate_few_shot.yaml",
    "response": "cmelevate_response_template.yaml",
}


STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "of", "for", "in", "on",
    "to", "and", "or", "how", "many", "much", "what", "which", "who",
    "with", "by", "has", "have", "had", "do", "does", "did", "me", "my",
    "there", "this", "that", "under", "cm", "elevate", "scheme", "please",
    "can", "you", "tell", "give", "show", "list", "get", "i", "want",
    "please", "it", "its", "as", "than", "at", "from", "all", "total",
}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOPWORDS}


def _overlap_score(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / ((len(a) * len(b)) ** 0.5)  # cosine-ish on set indicators


# ---------------------------------------------------------------------------
# Knowledge base: loads the 7 YAMLs from disk (this folder) once
# ---------------------------------------------------------------------------

class CMElevateKB:
    def __init__(self, data_dir: str | Path | None = None):
        """Loads the 7 cmelevate_*.yaml files from `data_dir` (defaults to
        this file's own folder, data/cm_elevate/). There is no embedded
        fallback copy: the YAML files are the single source of truth, the
        same convention used by data/focus_plus/prompt_assembler.py."""
        self.data_dir = Path(data_dir) if data_dir else DEFAULT_DIR
        self.schema = self._load("schema")
        self.resolver = self._load("resolver")
        self.gate_rules: list[dict] = self._load("classification")["clarification_rules"]
        self.default_rules: list[dict] = self._load("defaults")["default_rules"]
        self.few_shot: list[dict] = self._load("few_shot")["sql_generation_examples"]
        self.fk = self._load("fk")["foreign_key_augmentation"]
        self.templates = self._load("response")["response_templates"]

    def _load(self, key: str) -> Any:
        path = self.data_dir / FILES[key]
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    # -- convenience accessors -------------------------------------------------
    @property
    def view_columns(self) -> list[dict]:
        return self.schema["datasets"]["v_cm_elevate"]["columns"]

    @property
    def dimensions(self) -> dict:
        return self.resolver["dimensions"]


def _rule_text(note) -> str:
    """gate/default rule `note` fields are either a plain string or
    {critical: bool, text: str}. Normalise to plain text."""
    if note is None:
        return ""
    if isinstance(note, dict):
        return note.get("text", "")
    return str(note)


def _is_critical(note) -> bool:
    return isinstance(note, dict) and note.get("critical") is True


# ---------------------------------------------------------------------------
# Node 3 — Entity resolution
#
# Implements the resolver's own matching_pipeline (cmelevate_entity_resolver
# .yaml: normalisation, matching_pipeline, blocked_matches) rather than a
# generic substring scan, so it reproduces the resolver's documented
# behaviour: fold/squash normalisation, staged matching with confidence,
# and forced ambiguity for the pairs the resolver explicitly says fuzzy
# matching gets wrong. Stage 7 (BGE-small embedding fallback) has no
# offline equivalent here and is skipped — documented in resolve_entities.
# ---------------------------------------------------------------------------

_LEADING_QUALIFIERS = ["MEGHALAYA", "THE"]
_TRAILING_QUALIFIERS = [
    "C & RD BLOCK", "C&RD", "MUNICIPAL BOARD", "TOWN COMMITTEE",
    "SCHEME", "BLOCK", "DISTRICT", "VILLAGE", "MB", "TC",
]


def _fold(text: str) -> str:
    """cmelevate_entity_resolver.yaml: normalisation.fold, applied in order."""
    t = text.strip()
    t = re.sub(r"[-/().]", " ", t)          # treat -/(). as a single space
    t = re.sub(r"\s+", " ", t).strip()
    t = t.upper()
    t = re.sub(r"\s*&\s*", " AND ", t)      # map & to AND
    for w in _LEADING_QUALIFIERS:
        t = re.sub(rf"^{w}\b\s*", "", t)
    changed = True
    while changed:
        changed = False
        for w in _TRAILING_QUALIFIERS:
            new_t = re.sub(rf"\s+{re.escape(w)}$", "", t)
            if new_t != t:
                t = new_t
                changed = True
    return re.sub(r"\s+", " ", t).strip()


def _squash(text: str) -> str:
    """normalisation.squash: remove every non-alphanumeric char from the folded form."""
    return re.sub(r"[^A-Z0-9]", "", _fold(text))


def _squash_window_match(cand_folded: str, question_folded: str) -> bool:
    """Stage 3 (squash) is meant for multi-word canonicals that differ only
    in punctuation ('C&RD' vs 'C AND RD' vs 'CRD'), not for single-word
    aliases — squashing removes ALL whitespace, so a raw substring check
    against the fully-squashed question would let a short word match
    inside an unrelated longer one (e.g. squashed 'VILLA' is a substring of
    squashed 'VILLAGE'). Comparing squashed EQUALITY over same-width word
    windows avoids that while still catching real punctuation variants."""
    cand_words = cand_folded.split()
    if len(cand_words) < 2:
        return False
    cand_squashed = _squash(cand_folded)
    if len(cand_squashed) < 5:
        return False
    q_words = question_folded.split()
    n = len(cand_words)
    for i in range(len(q_words) - n + 1):
        window = " ".join(q_words[i:i + n])
        if _squash(window) == cand_squashed:
            return True
    return False


_GENERIC_QUERY_WORDS = {
    "HOW", "MANY", "WHAT", "WHATS", "IS", "ARE", "A", "AN", "OF", "FOR",
    "IN", "ON", "UNDER", "TOTAL", "COUNT", "NUMBER", "APPLICATIONS",
    "APPLICATION", "SHOW", "LIST", "GIVE", "ME", "PLEASE", "WHICH", "TELL",
    "THERE", "THIS", "THAT", "ALL", "BY", "WITH", "DOES", "DO", "TO", "AND",
    "BREAKDOWN", "SPLIT", "GET", "WANT", "MUCH",
}


def _content_words(question_folded: str) -> str:
    """Strips generic query scaffolding ('how many applications for') so
    fuzzy stage 6 compares the entity-bearing words of the question against
    the catalogue, not the whole sentence — a short, specific span like
    'PRIME vehicle' otherwise gets diluted into a low/noisy ratio against
    a 3-4 word scheme name once 6+ filler words are mixed in."""
    clean = re.sub(r"[^A-Z0-9 ]", " ", question_folded)
    words = [w for w in clean.split() if w not in _GENERIC_QUERY_WORDS]
    return " ".join(words) if words else question_folded


def _token_set_ratio(a: str, b: str) -> float:
    """Approximates RapidFuzz's token_set_ratio (0-100) using stdlib difflib,
    since rapidfuzz isn't available offline. Order-independent, tolerant of
    one string being a superset of the other's tokens — matches the shape
    of matching_pipeline stage 6."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    common = ta & tb
    only_a = ta - tb
    only_b = tb - ta
    base = " ".join(sorted(common))
    t1 = base
    t2 = (base + " " + " ".join(sorted(only_a))).strip()
    t3 = (base + " " + " ".join(sorted(only_b))).strip()
    pairs = [(t1, t2), (t1, t3), (t2, t3)]
    return max(difflib.SequenceMatcher(None, x, y).ratio() for x, y in pairs) * 100


@dataclass
class EntityMatch:
    dimension: str
    status: str  # "resolved" | "ambiguous" | "not_found"
    matched_text: str
    canonical: str | None = None
    sql_fragment: str | None = None
    confidence: float = 0.0
    matched_by: str = ""
    candidates: list = field(default_factory=list)  # for ambiguous
    note: str = ""


# Order matters, per resolver.how_to_use.step_1_classify: scheme first
# (place-like words collide, and scheme words beat place words per
# cross_dimension_collisions.disambiguation_rules), then geography.
_DIMENSION_ORDER = [
    "cm_scheme", "district", "block", "applicant_category",
    "data_verified", "current_file_status", "current_level",
    "application_mode", "gender",
]


def _stage_match(kb: "CMElevateKB", dim_key: str, dim: dict, question_folded: str,
                  question_squashed: str) -> "EntityMatch | None":
    """Runs matching_pipeline stages 1/2/3/5/6 for one dimension against the
    whole question. Returns the best match, an ambiguous match, or None."""
    values = dim.get("values", [])
    name_col = dim.get("name_column", dim_key)
    predicate_tpl = dim.get("sql_predicate", f"{name_col} = '<canonical>'")

    # Stages 1-3 and 5: exact / alias / squash / contains — all deterministic,
    # scanned by looking for a word-boundary hit of each candidate string
    # inside the folded question (stage 5 dominates for multi-word schemes,
    # per the resolver's own scheme_note).
    hits = []  # (confidence, stage, matched_text, value)
    for val in values:
        canonical = val.get("canonical")
        if not canonical:
            continue
        candidates = [canonical] + list(val.get("aliases", []))
        for cand in candidates:
            cand_folded = _fold(cand)
            if not cand_folded:
                continue
            if question_folded == cand_folded:
                hits.append((1.0, "1_exact", cand, val))
                continue
            pattern = r"(?<![A-Z0-9])" + re.escape(cand_folded) + r"(?![A-Z0-9])"
            if re.search(pattern, question_folded):
                stage = "2_alias" if cand.lower() != canonical.lower() else "5_contains"
                hits.append((0.98 if stage == "2_alias" else 0.85, stage, cand, val))
                continue
            if _squash_window_match(cand_folded, question_folded):
                hits.append((0.95, "3_squash", cand, val))

    # acronym stage — districts only
    if dim_key == "district":
        for val in values:
            acr = val.get("acronym")
            if acr and re.search(r"(?<![A-Z0-9])" + re.escape(acr) + r"(?![A-Z0-9])", question_folded):
                hits.append((0.95, "4_acronym", acr, val))

    if hits:
        # collapse to distinct canonicals, keep best confidence + longest match per canonical
        best_per_canonical = {}
        for conf, stage, text, val in hits:
            key = val["canonical"]
            cur = best_per_canonical.get(key)
            if cur is None or (conf, len(text)) > (cur[0], len(cur[2])):
                best_per_canonical[key] = (conf, stage, text, val)

        if len(best_per_canonical) == 1:
            conf, stage, text, val = next(iter(best_per_canonical.values()))
            return EntityMatch(
                dimension=dim_key, status="resolved", matched_text=text,
                canonical=val["canonical"],
                sql_fragment=predicate_tpl.replace("<canonical>", val["canonical"]),
                confidence=conf, matched_by=stage,
                note=val.get("note", "") if isinstance(val.get("note"), str) else "",
            )
        # more than one distinct canonical fired deterministically — genuinely
        # ambiguous input (e.g. overlapping aliases), not a fuzzy tie.
        return EntityMatch(
            dimension=dim_key, status="ambiguous", matched_text=question_folded,
            candidates=sorted(best_per_canonical.keys()), matched_by="multiple_deterministic_hits",
        )

    # Stage 6 — fuzzy fallback (token_set_ratio approximation of RapidFuzz),
    # compared against the content-word-only form of the question so a
    # short specific span isn't diluted by surrounding filler words.
    question_content = _content_words(question_folded)
    scored = []
    for val in values:
        canonical = val.get("canonical")
        if not canonical:
            continue
        fuzzy_candidates = [c for c in [canonical] + list(val.get("aliases", [])) if len(_fold(c)) >= 3]
        best_cand_score = max(
            (_token_set_ratio(question_content, _fold(c)) for c in fuzzy_candidates),
            default=0.0,
        )
        scored.append((best_cand_score, val))
    scored.sort(key=lambda t: t[0], reverse=True)
    if not scored or scored[0][0] < 80:
        return None
    top_score, top_val = scored[0]
    runner_score = scored[1][0] if len(scored) > 1 else 0.0
    runner_val = scored[1][1] if len(scored) > 1 else None

    blocked_pairs = {tuple(p) for p in kb.resolver["blocked_matches"]["pairs"]}
    forced_ambiguous = (
        runner_val is not None
        and (top_val["canonical"], runner_val["canonical"]) in blocked_pairs
    )

    if top_score >= 90 and (top_score - runner_score) >= 5 and not forced_ambiguous:
        return EntityMatch(
            dimension=dim_key, status="resolved", matched_text=question_folded,
            canonical=top_val["canonical"],
            sql_fragment=predicate_tpl.replace("<canonical>", top_val["canonical"]),
            confidence=top_score / 100, matched_by="6_fuzzy",
        )
    if forced_ambiguous or 80 <= top_score < 90 or (runner_val and top_score - runner_score < 5):
        cands = [top_val["canonical"]] + ([runner_val["canonical"]] if runner_val else [])
        return EntityMatch(
            dimension=dim_key, status="ambiguous", matched_text=question_folded,
            candidates=cands, matched_by="6_fuzzy",
            note="Forced ambiguous: this pair is in blocked_matches (fuzzy matching gets it wrong)." if forced_ambiguous else "",
        )
    return None


_VILLAGE_COLLISION_NAMES = None  # populated lazily from the resolver YAML


def _village_collision_check(kb: "CMElevateKB", question: str) -> "EntityMatch | None":
    global _VILLAGE_COLLISION_NAMES
    if _VILLAGE_COLLISION_NAMES is None:
        reg = kb.dimensions["village"].get("ambiguity_registry", {})
        _VILLAGE_COLLISION_NAMES = reg.get("names", [])
    q_folded = _fold(question)
    for name in _VILLAGE_COLLISION_NAMES:
        pattern = r"(?<![A-Z0-9])" + re.escape(_fold(name)) + r"(?![A-Z0-9])"
        if re.search(pattern, q_folded):
            return EntityMatch(
                dimension="village", status="ambiguous", matched_text=name,
                candidates=["(multiple LGD codes — see dim_geography)"],
                matched_by="ambiguity_registry",
                note="This village name maps to more than one LGD code. Per "
                     "resolution_strategy: resolve to village_code (not name), "
                     "scoped by district/block if given, else ask which one.",
            )
    return None


def resolve_entities(kb: "CMElevateKB", question: str) -> list:
    """Runs the resolver's matching pipeline (stages 1/2/3/4/5/6; stage 7
    embedding fallback is not available offline) over each dimension in
    classification order. Returns resolved AND ambiguous matches — the SQL
    prompt must treat 'ambiguous' as a reason to ask, not to guess."""
    q_folded = _fold(question)
    q_squashed = _squash(question)
    matches = []

    for dim_key in _DIMENSION_ORDER:
        dim = kb.dimensions.get(dim_key)
        if not dim or "values" not in dim:
            continue
        m = _stage_match(kb, dim_key, dim, q_folded, q_squashed)
        if m:
            matches.append(m)

    village_hit = _village_collision_check(kb, question)
    if village_hit:
        matches.append(village_hit)

    return matches


_DIRECTIONAL_PREFIXES = {"EAST", "WEST", "NORTH", "SOUTH", "EASTERN"}


def _grouping_match(kb: "CMElevateKB", groupings_key: str, question: str):
    """region_groupings / scheme_groupings: THE RESOLVER'S OWN CONSTRUCTION,
    not a stored column. Detected via the curated alias lists only (no fuzzy
    fallback). A directional-prefix guard stops a real district name like
    'East Garo Hills' from being mistaken for the 'Garo Hills' region group
    just because it contains that substring."""
    groupings = kb.resolver[groupings_key]["groups"]
    q_folded = _fold(question)
    for group_name, group in groupings.items():
        candidates = [group_name] + list(group.get("aliases", []))
        for cand in candidates:
            cand_folded = _fold(cand)
            if not cand_folded:
                continue
            pattern = r"(?<![A-Z0-9])" + re.escape(cand_folded) + r"(?![A-Z0-9])"
            m = re.search(pattern, q_folded)
            if not m:
                continue
            preceding = q_folded[:m.start()].split()
            if preceding and preceding[-1] in _DIRECTIONAL_PREFIXES:
                continue  # e.g. "East Garo Hills" — a real district, not the region group
            if len(preceding) >= 2 and " ".join(preceding[-2:]) == "SOUTH WEST":
                continue
            members_key = "districts" if groupings_key == "region_groupings" else "schemes"
            return {
                "group": group_name,
                "matched_text": cand,
                "members": group.get(members_key, []),
                "must_declare": True,
            }
    return None


def detect_region_grouping(kb: "CMElevateKB", question: str):
    return _grouping_match(kb, "region_groupings", question)


def detect_scheme_grouping(kb: "CMElevateKB", question: str):
    return _grouping_match(kb, "scheme_groupings", question)


def _applicant_name_trigger(question: str) -> bool:
    q = question.lower()
    if re.search(r"\bnamed\s+\w+", q):
        return True
    if re.search(r"\b(applicant|citizen|beneficiary)'?s?\s+name\b", q):
        return True
    if re.search(r"\bname of (the )?(applicant|citizen|beneficiary|person)\b", q):
        return True
    return False


def detect_absent_dimensions(kb: "CMElevateKB", question: str) -> list:
    """Flags money / time / loan-channel / outcome-flag / person-identity
    spans using the resolver's OWN aliases_that_trigger_it lists
    (absent_dimensions.*), not a hand-guessed keyword set. Each hit carries
    the exact resolver_behaviour/reason text to surface to the user."""
    q_folded = _fold(question)
    hits = []
    absent = kb.resolver["absent_dimensions"]
    for dim_name in ("time", "money", "loan_channel", "outcome_flags"):
        spec = absent.get(dim_name)
        if not spec:
            continue
        aliases = spec.get("aliases_that_trigger_it", [])
        for alias in aliases:
            alias_folded = _fold(str(alias))
            if not alias_folded:
                continue
            pattern = r"(?<![A-Z0-9])" + re.escape(alias_folded) + r"(?![A-Z0-9])"
            if re.search(pattern, q_folded):
                behaviour = spec.get("resolver_behaviour", "")
                reason = behaviour.get("text", "") if isinstance(behaviour, dict) else behaviour
                hits.append({
                    "dimension": dim_name,
                    "matched_text": alias,
                    "status": spec.get("status", "NOT_AVAILABLE"),
                    "reason": reason,
                })
                break  # one hit per dimension is enough
    if _applicant_name_trigger(question):
        spec = absent.get("person_identity", {})
        behaviour = spec.get("resolver_behaviour", {})
        reason = behaviour.get("text", "") if isinstance(behaviour, dict) else str(behaviour)
        hits.append({
            "dimension": "person_identity",
            "matched_text": "(name reference)",
            "status": spec.get("status", "DROPPED_AT_INGEST"),
            "reason": reason,
        })
    return hits


# ---------------------------------------------------------------------------
# Node 5.10 — Few-shot retrieval
# ---------------------------------------------------------------------------

def select_few_shots(kb: CMElevateKB, question: str, k: int = 6,
                      force_include_refusal: bool = True) -> list[dict]:
    q_tok = _tokens(question)
    scored = []
    for ex in kb.few_shot:
        ex_tok = _tokens(ex["question"])
        score = _overlap_score(q_tok, ex_tok)
        score += 0.15 * difflib.SequenceMatcher(None, question.lower(), ex["question"].lower()).ratio()
        scored.append((score, ex))
    scored.sort(key=lambda t: t[0], reverse=True)

    selected = [ex for score, ex in scored[:k] if score > 0]
    if not selected:
        selected = [ex for _, ex in scored[:k]]

    if force_include_refusal:
        absent = detect_absent_dimensions(kb, question)
        if absent and not any(ex.get("sql") is None for ex in selected):
            # Pull in the best-matching refusal example for the detected
            # absent dimension(s) so the generator sees a concrete refusal
            # pattern, not just answerable examples.
            refusal_pool = [(s, ex) for s, ex in scored if ex.get("sql") is None]
            if refusal_pool:
                refusal_pool.sort(key=lambda t: t[0], reverse=True)
                selected = selected[: max(k - 1, 1)] + [refusal_pool[0][1]]
    return selected


def format_few_shots(examples: list[dict]) -> str:
    blocks = []
    for i, ex in enumerate(examples, 1):
        if ex.get("sql") is None:
            blocks.append(
                f"Example {i} (REFUSAL)\n"
                f"Q: {ex['question']}\n"
                f"A: UNANSWERABLE — {ex.get('reason', '').strip()}"
            )
        else:
            note = f"\nNote: {ex['note'].strip()}" if ex.get("note") else ""
            blocks.append(
                f"Example {i}\n"
                f"Q: {ex['question']}\n"
                f"SQL:\n{ex['sql'].strip()}{note}"
            )
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Node 5 — Fast heuristic pre-gate (defense-in-depth ahead of the LLM gate)
# ---------------------------------------------------------------------------

# Maps a detect_absent_dimensions() hit (built from the resolver's OWN
# aliases_that_trigger_it lists for money/time) to the matching gate
# condition, so the two most dangerous refusal paths are keyword-checked
# against the resolver's curated trigger words, not a hand-guessed list.
_ABSENT_DIM_TO_GATE_CONDITION = {
    "money": "any_money_amount_requested",
    "time": "any_time_period_requested",
    "person_identity": "applicant_name_requested",
}

# loan_channel / outcome_flags have no aliases_that_trigger_it list in the
# resolver YAML (they're keyed off named entities — "Bank", "LIFCOM",
# "desanctioned" — not a curated phrase catalogue), so these stay as
# hand-written, deliberately conservative predicates. Same for the
# workbook-conflict conditions, which are about specific numbers/words the
# requirement workbook uses, not a resolver dimension at all.
_STATIC_FAST_GATE_TRIGGERS: list[tuple[str, Any, str]] = [
    ("onhold_flag_used_for_on_hold_question", lambda t: "hold" in t or "onhold" in t, "critical"),
    ("approval_status_requested", lambda t: bool({"approved", "approval", "approvals"} & t), "critical"),
    ("refused_or_rejected_requested", lambda t: bool({"refused", "rejected", "refusal", "rejection"} & t), "note"),
    ("desanctioned_requested", lambda t: bool({"desanctioned", "desanction"} & t), "note"),
    ("loan_entity_requested",
     lambda t: "lifcom" in t or ("bank" in t and bool({"loan", "channel", "disbursed", "disbursement"} & t)),
     "note"),
    ("thirteen_schemes_asserted", lambda t: "13" in t and ("scheme" in t or "schemes" in t), "note"),
    ("common_facility_center_requested",
     lambda t: "common" in t and ("facility" in t or "center" in t or "centre" in t), "note"),
    ("sericulture_spinning_weaving_split_assumed", lambda t: "spinning" in t, "note"),
    ("workbook_figure_quoted_by_user", lambda t: bool({"2847", "1357", "1153"} & t), "note"),
]


def fast_gate_check(kb: CMElevateKB, question: str) -> dict | None:
    """Deterministic keyword pre-check. Returns a clarification dict if a
    high-confidence trigger fires, else None (fall through to the LLM gate
    prompt built by build_gate_prompt). This is a conservative,
    false-positive-averse subset of the 115 rules in
    cmelevate_classification_rules.yaml — it exists so the handful of
    highest-cost mistakes (money, time, on-hold, applicant names,
    wrong-dataset figures) don't depend on the LLM gate call succeeding.
    The full rule set still needs build_gate_prompt() run through an LLM
    for everything else, including every rule not listed here."""
    absent = detect_absent_dimensions(kb, question)
    for hit in absent:
        condition = _ABSENT_DIM_TO_GATE_CONDITION.get(hit["dimension"])
        if condition:
            return {
                "condition": condition,
                "matched_by": f"absent_dimension:{hit['dimension']}",
                "severity": "critical",
                "matched_text": hit["matched_text"],
            }

    toks = _tokens(question)
    for condition, predicate, severity in _STATIC_FAST_GATE_TRIGGERS:
        if predicate(toks):
            return {"condition": condition, "matched_by": "fast_gate_static", "severity": severity}
    return None


def fast_offer_check(kb: CMElevateKB, question: str) -> list[dict]:
    """Non-blocking 'offer' rules — gender_breakdown_requested and
    map_requested are explicitly NOT refusals in the gate file (the
    question is answerable), so they must never stop SQL generation. This
    returns hints to fold into the SQL/response prompt instead."""
    offers = []
    toks = _tokens(question)
    if toks & {"gender", "female", "male", "women", "men"}:
        rule = find_gate_rule(kb, "gender_breakdown_requested")
        if rule:
            offers.append({"condition": "gender_breakdown_requested", "note": rule["note"]})
    if "map" in toks or "choropleth" in toks:
        rule = find_gate_rule(kb, "map_requested")
        if rule:
            offers.append({"condition": "map_requested", "note": rule["note"]})
    return offers


def find_gate_rule(kb: CMElevateKB, condition: str) -> dict | None:
    for r in kb.gate_rules:
        if r["condition"] == condition:
            return r
    return None


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _column_ref_block(kb: CMElevateKB, question: str | None = None) -> str:
    """Compact column reference. If `question` is given, always includes
    'critical' priority columns plus any column whose synonyms fire on the
    question; otherwise dumps the full 22-column reference."""
    cols = kb.view_columns
    if question:
        q_tok = _tokens(question)
        keep = []
        for c in cols:
            if c.get("nlp_sql_priority") in ("critical", "high"):
                keep.append(c)
                continue
            syns = " ".join(c.get("synonyms", []))
            if _tokens(syns) & q_tok:
                keep.append(c)
        # de-dup while preserving order
        seen = set()
        cols = [c for c in keep if not (c["name"] in seen or seen.add(c["name"]))]

    lines = []
    for c in cols:
        rules = c.get("business_rules", [])
        rule_txt = " ".join(rules[:2]) if rules else ""
        syns = ", ".join(c.get("synonyms", [])[:5])
        lines.append(f"- `{c['name']}` ({c.get('type', '?')}) — {c.get('meaning', '')} "
                      f"[synonyms: {syns}] {('| ' + rule_txt) if rule_txt else ''}".strip())
    return "\n".join(lines)


def _critical_semantic_rules(kb: CMElevateKB) -> str:
    sr = kb.schema["semantic_rules"]
    parts = []
    for key in ("geography_exclusion_rule", "time_rule", "measures", "status_rule",
                "applicant_rule", "scheme_rule", "beneficiary_term"):
        val = sr.get(key)
        if val is None:
            continue
        if isinstance(val, dict):
            text = val.get("text") or val.get("money_rule") or val.get("reason") or ""
            if key == "beneficiary_term":
                text = (val.get("reason", "") + " " + val.get("runtime_behavior", "")).strip()
            parts.append(f"[{key}] {text.strip()}")
        else:
            parts.append(f"[{key}] {val}")
    return "\n".join(f"- {p}" for p in parts)


def _join_graph_block(kb: CMElevateKB) -> str:
    fk = kb.fk
    lines = [f"Default FROM clause: `{fk['primary_query_surface']}` (schema-linking should almost never need to leave it)."]
    lines.append("\nDeclared FK edges (only these joins are sanctioned as row-level joins):")
    for e in fk["edges"]:
        if not e.get("declared") or e.get("join_type") is None:
            continue
        lines.append(f"- {e['from_table']}.{e['from_column']} -> {e['to_table']}.{e['to_column']} "
                      f"({e['join_type']}, prefer instead: {e.get('prefer_instead', 'n/a')})")
    prohibited = [e for e in fk["edges"] if not e.get("declared")]
    if prohibited:
        lines.append("\nPROHIBITED joins (never emit these — no join_type exists for a reason):")
        for e in prohibited:
            lines.append(f"- {e['from_table']} -> {e['to_table']}")
    if fk.get("absent_edges"):
        lines.append("\nAbsent edges the generator must NOT invent:")
        for ae in fk["absent_edges"]:
            txt = ae if isinstance(ae, str) else json.dumps(ae)
            lines.append(f"- {txt}")
    lines.append("\nSanctioned non-default query patterns:")
    for name, pat in fk.get("sanctioned_patterns", {}).items():
        lines.append(f"\n[{name}] {pat.get('purpose', '').strip()}\n```sql\n{pat.get('sql', '').strip()}\n```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Safety — regex-based SQL guardrail
#
# architecture_alignment.safety specifies "SQLGlot read-only validation,
# enforced LIMIT, SELECT-only database role." sqlglot isn't installable in
# this offline environment, so this is a regex/heuristic stand-in covering
# the CM-Elevate-specific failure modes documented across the seven YAMLs
# (no SUM/AVG, no date handling, the on-hold trap, the prohibited joins,
# the ungrounded scheme_specific read, type_raw, case folding). It is NOT a
# substitute for a real parser-based validator (it can be fooled by
# adversarial SQL — e.g. keywords inside string literals or comments) and
# the read-only DB role remains the actual security boundary; treat this as
# a second, defense-in-depth check on top of that role, not instead of it.
# ---------------------------------------------------------------------------

_FORBIDDEN_STATEMENTS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|"
    r"CALL|EXECUTE|COPY|VACUUM|MERGE|REFRESH)\b", re.IGNORECASE,
)
_FORBIDDEN_TABLE_HINTS = [
    "fact_pmay_house", "fact_focus_plus_disbursement", "fact_mgnrega_employment",
    "fact_mgnrega_expenditure", "dim_geography_alias", "dim_pmay_house_status",
    "dim_year", "v_cross_scheme_money_district_year", "raw.source_rows",
]
# Readable_by_read_role: false in the FK graph — legitimate objects, but a
# standard megh_readonly NL->SQL query can never actually run against them.
# Flagged separately from _FORBIDDEN_TABLE_HINTS because the reason is a
# permissions boundary, not a data-modelling mistake (unlike the prohibited
# cross-scheme joins above).
_OUTSIDE_READ_ROLE_TABLES = ["meta.v_reconciliation_cm_elevate", "staging.quarantine"]
_ALLOWED_CM_TABLES = [
    "v_cm_elevate", "fact_cm_elevate_application", "dim_cm_elevate_scheme",
    "dim_geography", "dim_scheme", "v_cross_scheme_village_coverage",
]
# gender_id is the one scheme_specific key documented as populated on every
# row across all 15 schemes (cmelevate_schema_partitions.yaml §4.5 /
# entity_resolver dimensions.gender.populated) — the mandatory
# cm_scheme_key/scheme_name scope exists to stop ->> returning a silent
# NULL for a key that ONLY exists in some schemes, which does not apply here.
_UNIVERSAL_JSONB_KEYS = {"gender_id"}


@dataclass
class SQLIssue:
    severity: str  # "error" | "warning"
    rule: str
    message: str


def _strip_filter_clauses(lowered_sql: str) -> str:
    """Removes the contents of every FILTER (WHERE ...) clause, so a
    diagnostic query like
        COUNT(*) FILTER (WHERE onhold IS TRUE)
    (checking whether a flag is populated) isn't mistaken for a query that
    uses the flag as its row-selection predicate."""
    return re.sub(r"filter\s*\([^()]*\)", "", lowered_sql)


def validate_sql(kb: CMElevateKB, sql: str) -> list[SQLIssue]:
    """Checks generated SQL against the rules gathered from all seven YAMLs.
    Returns a list of SQLIssue; an empty list means nothing was flagged.
    `[i for i in validate_sql(kb, sql) if i.severity == "error"]` should be
    empty before the SQL is executed."""
    issues: list[SQLIssue] = []
    if sql is None:
        return issues
    stripped = sql.strip()
    lowered = stripped.lower()
    lowered_no_filter = _strip_filter_clauses(lowered)

    # 1. Single read-only statement.
    body = stripped[:-1] if stripped.endswith(";") else stripped
    if ";" in body:
        issues.append(SQLIssue("error", "multiple_statements",
                                "More than one statement — only a single SELECT is allowed."))
    if not re.match(r"^\s*(WITH|SELECT)\b", stripped, re.IGNORECASE):
        issues.append(SQLIssue("error", "not_a_select",
                                "Statement must start with SELECT or WITH (a CTE feeding a SELECT)."))
    if _FORBIDDEN_STATEMENTS.search(stripped):
        issues.append(SQLIssue("error", "write_operation",
                                "Contains a write/DDL keyword; only SELECT is permitted (nlp_sql_rules.allowed_operations)."))

    # 2. No measure at all (schema_partitions: no_measure, nlp_sql_rules.aggregation_note).
    # Only flags SUM/AVG applied directly to a REAL v_cm_elevate column —
    # none of which are numeric measures, so that's always wrong. SUM/AVG
    # over a COUNT(...) or over a derived alias (e.g. a CTE's `applications`
    # column, as in the share/concentration few-shot examples) is a
    # legitimate rollup of a count and is not flagged.
    real_columns = {c["name"].lower() for c in kb.view_columns}
    for m in re.finditer(r"\b(SUM|AVG)\s*\(\s*([a-zA-Z_][a-zA-Z0-9_.]*)", stripped, re.IGNORECASE):
        arg_bare = m.group(2).split(".")[-1].lower()
        if arg_bare in real_columns:
            issues.append(SQLIssue("error", "no_measure_exists",
                                    f"{m.group(1).upper()}({m.group(2)}) — there is no numeric measure column on "
                                    "v_cm_elevate; every column is an identifier, category or flag."))

    # 3. Date handling (semantic_rules.time_rule, nlp_sql_rules.date_behavior).
    date_patterns = [r"\bdate_trunc\s*\(", r"\bextract\s*\(\s*(year|month|quarter)\b",
                      r"\bto_date\s*\(", r"::date\b", r"\bnow\s*\(\s*\)", r"\bcurrent_date\b",
                      r"\bingested_at\b"]
    for pat in date_patterns:
        if re.search(pat, lowered):
            issues.append(SQLIssue("error", "date_handling",
                                    f"Matches forbidden date pattern /{pat}/ — CM Elevate has no time dimension; "
                                    "never filter/group by date, and never substitute ingested_at."))
            break
    if re.search(r"request_id[^,)\n]*(substring|split_part|regexp|~)", lowered) or \
       re.search(r"(substring|split_part|regexp\w*)\s*\(\s*request_id\b", lowered):
        issues.append(SQLIssue("error", "request_id_date_parse",
                                "Parses request_id — its date-like segment is genuinely ambiguous "
                                "(resolved only 3,038/8,436 rows in testing) and must never be parsed."))

    # 2b. Hallucinated money columns. These names come straight from
    # absent_dimensions.money.columns_that_do_not_exist — they don't exist
    # anywhere on this fact under ANY name, so a bare reference (not just
    # inside SUM/AVG) is always a hallucination, not a legitimate alias.
    money_cols = kb.resolver["absent_dimensions"]["money"].get("columns_that_do_not_exist", [])
    for col in money_cols:
        if re.search(rf"(?<![a-zA-Z_]){re.escape(col.lower())}(?![a-zA-Z_])", lowered):
            issues.append(SQLIssue("error", "hallucinated_money_column",
                                    f"References '{col}', which does not exist on fact_cm_elevate_application "
                                    "or anywhere else in this partition — no amount column exists at all."))

    # 4. The on-hold trap (semantic_rules.status_rule, dead flag). Ignores
    # FILTER(WHERE ...) diagnostic usage (checking whether the flag is
    # populated is fine and is exactly what the dead-flags few-shot does);
    # only flags onhold used as an actual row-selection predicate.
    if re.search(r"\bonhold\b", lowered_no_filter):
        issues.append(SQLIssue("error", "onhold_boolean_used",
                                "Uses the `onhold` boolean as a row filter — it is FALSE on every row. "
                                "'On hold' must route to data_verified = 'On Hold'."))
    if re.search(r"\bis_withdraw\b", lowered_no_filter):
        issues.append(SQLIssue("warning", "is_withdraw_dead_flag",
                                "is_withdraw used as a row filter — it is FALSE on every row (unverified_in_db) "
                                "and will silently return nothing."))

    # 5. applicant_category vs type_raw (semantic_rules.applicant_rule).
    # The rule is "never GROUP BY type_raw ACROSS schemes" — grouping by it
    # within a single scheme (WHERE scheme_name = '...' / cm_scheme_key = N)
    # is the documented audit use case and is fine.
    if re.search(r"group\s+by[^;]*\btype_raw\b", lowered, re.DOTALL):
        scheme_scoped = re.search(r"where[^;]*\b(scheme_name|cm_scheme_key)\s*=", lowered, re.DOTALL)
        if not scheme_scoped:
            issues.append(SQLIssue("error", "grouped_by_type_raw_unscoped",
                                    "GROUP BY type_raw with no single-scheme filter — the taxonomy isn't "
                                    "comparable across schemes. Use applicant_category, or scope to one scheme_name."))

    # 6. Case folding (semantic_rules.status_rule: current_level / current_file_status).
    for col in ("current_level", "current_file_status"):
        if col in lowered:
            has_group_or_where_use = re.search(rf"(where|group\s+by)[^;]*\b{col}\b", lowered, re.DOTALL)
            wrapped_in_lower = re.search(rf"lower\s*\(\s*{col}\b", lowered)
            if has_group_or_where_use and not wrapped_in_lower:
                issues.append(SQLIssue("warning", "case_not_folded",
                                        f"{col} is filtered/grouped without LOWER() — 'level1'/'Level1' and similar "
                                        "case variants will be split into separate groups."))

    # 7. Village exclusion rule (semantic_rules.geography_exclusion_rule).
    village_agg = re.search(r"count\s*\(\s*distinct\s+village_code\s*\)", lowered) or \
        re.search(r"\blgd_village_name\b", lowered)
    has_unresolved_filter = re.search(r"entity_type\s*(<>|!=)\s*'unresolved'", lowered)
    if village_agg and not has_unresolved_filter:
        issues.append(SQLIssue("warning", "unresolved_not_excluded",
                                "Counts/lists villages without excluding entity_type = 'Unresolved' "
                                "(the synthetic placeholder is not a real village)."))

    # 8. scheme_specific JSONB reads must be scoped (nlp_sql_rules.jsonb_behavior),
    # except the one key documented as universal across all 15 schemes.
    if "scheme_specific" in lowered and ("->>" in lowered or "@>" in lowered):
        keys_read = set(re.findall(r"scheme_specific\s*->>\s*'([a-zA-Z0-9_]+)'", lowered))
        is_scoped = bool(re.search(r"cm_scheme_key\s*=|scheme_name\s*=", lowered))
        unscoped_non_universal_keys = keys_read - _UNIVERSAL_JSONB_KEYS
        contains_query_unscoped = "@>" in lowered and not is_scoped
        if (unscoped_non_universal_keys or (contains_query_unscoped and not keys_read)) and not is_scoped:
            issues.append(SQLIssue("error", "unscoped_jsonb_read",
                                    "Reads scheme_specific without a cm_scheme_key/scheme_name filter — "
                                    "->> returns NULL for a missing key, indistinguishable from empty. "
                                    "(gender_id is the one key documented as populated on every row and exempt "
                                    "from this rule.)"))

    # 9. request_id + LIMIT 1 (schema: request_id may return more than one row).
    if "request_id" in lowered and re.search(r"\blimit\s+1\b", lowered):
        issues.append(SQLIssue("warning", "limit_1_on_request_id",
                                "request_id is not unique (36 duplicates in the source export) — "
                                "LIMIT 1 may silently drop a real duplicate row."))

    # 10. Join graph — prohibited cross-scheme tables / read-role boundary / unrecognised FROM object.
    for hint in _FORBIDDEN_TABLE_HINTS:
        if hint in lowered:
            issues.append(SQLIssue("error", "prohibited_join_or_table",
                                    f"References '{hint}' — a prohibited cross-scheme join "
                                    "or a table not exposed to CM Elevate questions."))
    for hint in _OUTSIDE_READ_ROLE_TABLES:
        if hint in lowered:
            issues.append(SQLIssue("error", "outside_read_role_boundary",
                                    f"References '{hint}', which is readable_by_read_role: false in the FK graph — "
                                    "a data-team/ops query, not something the standard megh_readonly NL->SQL "
                                    "pipeline can execute for an end-user question."))
    if not any(t in lowered for t in _ALLOWED_CM_TABLES + _OUTSIDE_READ_ROLE_TABLES):
        issues.append(SQLIssue("warning", "no_recognised_cm_elevate_table",
                                "Doesn't reference curated.v_cm_elevate or any sanctioned CM Elevate table — "
                                "double-check this is actually answering the question asked."))

    return issues


def _relevant_default_rules(kb: CMElevateKB, question: str, max_rules: int = 10) -> list[dict]:
    q_tok = _tokens(question)
    always = {"unresolved_geography_in_village_count", "duplicate_request_ids_in_count"}
    scored = []
    for r in kb.default_rules:
        cond_tok = _tokens(r["condition"].replace("_", " "))
        text_tok = _tokens(r.get("assumption_text", ""))
        score = _overlap_score(q_tok, cond_tok | text_tok)
        if r["condition"] in always:
            score += 1.0
        if _is_critical(r.get("note")):
            score += 0.05
        scored.append((score, r))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [r for s, r in scored[:max_rules] if s > 0]


def build_entity_resolution_context(kb: CMElevateKB, question: str) -> str:
    matches = resolve_entities(kb, question)
    absent = detect_absent_dimensions(kb, question)
    region_grp = detect_region_grouping(kb, question)
    scheme_grp = detect_scheme_grouping(kb, question)
    offers = fast_offer_check(kb, question)
    lines = []

    resolved = [m for m in matches if m.status == "resolved"]
    ambiguous = [m for m in matches if m.status == "ambiguous"]

    # A region-grouping match already gives a clean, correct answer (the
    # named district list); don't also surface a fuzzy district-dimension
    # "ambiguous" hit whose candidates are just a subset of that same group
    # — it's the same underlying span, resolved twice, one way correctly.
    if region_grp:
        region_districts = set(region_grp["members"])
        ambiguous = [
            m for m in ambiguous
            if not (m.dimension == "district" and set(m.candidates) <= region_districts)
        ]

    if resolved:
        lines.append("Resolved entities:")
        for m in resolved:
            lines.append(f"- \"{m.matched_text}\" -> {m.dimension} = {m.canonical!r} "
                         f"(matched_by={m.matched_by}, confidence={m.confidence:.2f})  =>  WHERE {m.sql_fragment}")
    if ambiguous:
        lines.append("\nAMBIGUOUS — do not guess, ask which one:")
        for m in ambiguous:
            lines.append(f"- \"{m.matched_text}\" in dimension {m.dimension} could be: "
                         f"{', '.join(m.candidates)}." + (f" {m.note}" if m.note else ""))
    if not resolved and not ambiguous:
        lines.append("Resolved entities: none detected.")

    if absent:
        lines.append("\nAbsent-dimension spans detected (must resolve to NOT_AVAILABLE / not_found, never a guessed column):")
        for a in absent:
            lines.append(f"- \"{a['matched_text']}\" -> {a['dimension']} ({a['status']}): {a['reason'].strip()}")

    if region_grp:
        lines.append(f"\nREGION GROUPING detected ({region_grp['matched_text']!r} -> {region_grp['group']}): "
                      f"there is no region column in dim_geography — this is the resolver's own construction. "
                      f"Expand to districts IN {region_grp['members']} and state in the answer that the "
                      f"grouping was applied, not read from stored data.")
    if scheme_grp:
        lines.append(f"\nSCHEME GROUPING detected ({scheme_grp['matched_text']!r} -> {scheme_grp['group']}): "
                      f"no sector column exists — this is the resolver's own construction. "
                      f"Expand to scheme_name IN {scheme_grp['members']} and state in the answer that the "
                      f"grouping was applied, not read from stored data.")

    for offer in offers:
        lines.append(f"\nOFFER (not a refusal — this is answerable, worth surfacing): {_rule_text(offer['note'])}")

    return "\n".join(lines)


def build_gate_prompt(kb: CMElevateKB, question: str) -> str:
    """Full clarification-gate prompt (Node 5). First rule that matches
    wins; the pipeline should pause and ask `question` back to the user
    instead of generating SQL."""
    rule_lines = []
    for r in kb.gate_rules:
        crit = " [CRITICAL]" if _is_critical(r.get("note")) else ""
        rule_lines.append(f"- condition: {r['condition']}{crit}\n  ask: {r['question']}")
    rules_block = "\n".join(rule_lines)

    return f"""You are the clarification gate for the CM Elevate scheme (curated.v_cm_elevate).
CM Elevate is an APPLICATION-only dataset: no money column, no date column,
15 sub-schemes. Before any SQL is generated, decide whether the user's
question can be answered as asked, or whether one of the rules below fires.

Evaluate the rules IN ORDER. The first rule whose condition matches the
question wins — stop there, do not keep scanning.

RULES ({len(kb.gate_rules)} total):
{rules_block}

USER QUESTION:
{question}

Respond ONLY as JSON:
{{"action": "clarify", "condition": "<condition>", "message": "<the ask text, verbatim>"}}
or, if no rule fires:
{{"action": "proceed"}}
"""


def build_sql_prompt(kb: CMElevateKB, question: str, few_shot_k: int = 6) -> str:
    """Node 5 / 5.5 / 5.10 combined: the actual NL->SQL generation prompt,
    assuming the gate already returned {"action": "proceed"}."""
    ds = kb.schema["datasets"]["v_cm_elevate"]
    nsr = kb.schema["nlp_sql_rules"]
    entity_ctx = build_entity_resolution_context(kb, question)
    few_shots = select_few_shots(kb, question, k=few_shot_k)
    relevant_defaults = _relevant_default_rules(kb, question)

    default_lines = "\n".join(
        f"- {d['condition']}: default={d.get('default_value')!r} — {d.get('assumption_text', '').strip()}"
        + (f" | SQL effect: {d['sql_effect']}" if d.get("sql_effect") else "")
        for d in relevant_defaults
    ) or "- (none matched; use ordinary judgement, no silent measure/time substitution)"

    return f"""You generate READ-ONLY PostgreSQL for the CM Elevate scheme.

DATASET
Table: {ds['table_name']} ({ds['object_type']}, grain: {ds['grain']})
{ds.get('row_count_note', '').strip()}
{ds.get('money_unit_note', '').strip()}

COLUMN REFERENCE (relevant subset)
{_column_ref_block(kb, question)}

SEMANTIC RULES
{_critical_semantic_rules(kb)}

NLP -> SQL RULES
- default FROM: {nsr['default_from']}
- allowed operations: {', '.join(nsr['allowed_operations'])} only — read-only
- aggregation defaults: {json.dumps(nsr['aggregation_defaults'])}
- {nsr['aggregation_note'].strip()}
- mandatory predicate: {nsr['mandatory_predicate']['clause']} — {nsr['mandatory_predicate']['note'].strip()}
- date_behavior: {nsr['date_behavior'].strip()}
- money_behavior: {nsr['money_behavior'].strip()}
- ranking: {nsr['ranking'].strip()}
- jsonb_behavior: {nsr['jsonb_behavior'].strip()}
- join_behavior: {nsr['join_behavior'].strip()}

JOIN GRAPH
{_join_graph_block(kb)}

ENTITY RESOLUTION (Node 3 output for this question)
{entity_ctx}

DEFAULTS TO APPLY SILENTLY WHEN THE QUESTION LEAVES THEM UNSPECIFIED
(only apply these AFTER the clarification gate has already returned "proceed";
never let a default substitute for a refusal)
{default_lines}

NON-NEGOTIABLE BEHAVIOURS (violating any one of these makes the SQL wrong
even if it runs and returns a plausible-looking number):
1. No SUM, no AVG — there is no measure column at all.
2. No date filter, no date GROUP BY, no date_trunc, no parsing request_id as
   a date, and never substitute ingested_at.
3. An "on hold" question routes to data_verified = 'On Hold', never onhold
   (which is FALSE on every row).
4. entity_type <> 'Unresolved' on every village count / village list, and on
   nothing else.
5. GROUP BY applicant_category, never type_raw.
6. LOWER() current_level and current_file_status before grouping/filtering.
7. Every scheme_specific (JSONB) read carries a cm_scheme_key or scheme_name
   filter (->> returns NULL for a missing key, indistinguishable from empty).
8. request_id may return more than one row — never assume LIMIT 1 is safe.
9. There are 15 schemes, not 13. Never adopt a figure from the requirement
   workbook (2,847 applications / 1,357 Piggery / 1,153 villages / any ₹
   total) — those describe a different dataset.

FEW-SHOT EXAMPLES (retrieved for this question)
{format_few_shots(few_shots)}

USER QUESTION
{question}

OUTPUT
If the question can be answered from curated.v_cm_elevate under the rules
above, output ONLY the SQL in a ```sql fence. If it cannot — no money
column, no date column, an absent field, or a workbook figure that does not
exist here — output exactly:
UNANSWERABLE: <one-sentence reason, naming the missing column/dimension>
"""


def build_response_prompt(kb: CMElevateKB, question: str, sql: str | None,
                           rows: list[dict] | None, assumptions: list[str] | None = None) -> str:
    """Node 5.15: compose the final natural-language answer from query
    results (or a refusal), enforcing formatting.py + composer_checks.
    Runs validate_sql() first — if the SQL fails a safety check, the
    prompt tells the composer to stop and report the problem instead of
    describing query results that should never have been produced."""
    fmt = kb.templates["formatting"]
    checks = kb.templates["composer_checks"]["checks"]
    templates = kb.templates["templates"]
    assumptions = assumptions or []

    safety_issues = validate_sql(kb, sql) if sql else []
    safety_errors = [i for i in safety_issues if i.severity == "error"]
    safety_warnings = [i for i in safety_issues if i.severity == "warning"]

    if safety_errors:
        issue_lines = "\n".join(f"- [{i.rule}] {i.message}" for i in safety_errors)
        return f"""STOP — the generated SQL failed safety validation and must NOT be
described to the user as if it produced a real answer.

QUESTION
{question}

SQL THAT FAILED VALIDATION
{sql}

FAILED CHECKS
{issue_lines}

Do not compose a normal answer. Instead: discard this SQL, and either
regenerate it correctly against curated.v_cm_elevate following the rules in
the SQL-generation prompt, or — if the question genuinely cannot be
answered — say so plainly, in the shape of a response_templates refusal
(name the missing column/dimension, offer the nearest real question as an
explicitly different question).
"""

    result_block = (
        f"SQL executed:\n{sql}\n\nRows:\n{json.dumps(rows, default=str, indent=2)}"
        if sql else "No SQL was executed — this question was refused upstream."
    )
    warnings_block = (
        "\n\nSAFETY WARNINGS (non-blocking — account for these in the answer, don't ignore them):\n"
        + "\n".join(f"- [{i.rule}] {i.message}" for i in safety_warnings)
    ) if safety_warnings else ""

    picked_templates = "\n".join(f"- {k}: \"{v}\"" for k, v in list(templates.items())[:20])

    return f"""You compose the final answer shown to the user for a CM Elevate
question. Follow the formatting rules and self-check list before sending.

FORMATTING RULES
- number_format: {fmt['number_format']}
- currency_symbol: {fmt['currency_symbol']} (never render one — {_rule_text(fmt.get('money_rule')).strip()})
- date_format: {fmt['date_format']} (never render a date/period — {_rule_text(fmt.get('time_rule')).strip()})
- {_rule_text(fmt.get('scope_must_name_scheme')).strip()}
- {_rule_text(fmt.get('small_denominator_rule')).strip()}
- {_rule_text(fmt.get('application_not_award_rule')).strip()}
- null_display: "{fmt['null_display']}", zero_display: "{fmt['zero_display']}"
- empty_result_message: "{fmt['empty_result_message']}"

RELEVANT ANSWER TEMPLATES (pick the closest fit, fill placeholders; don't invent new ones)
{picked_templates}

ASSUMPTIONS TO SURFACE TO THE USER (from node 5.5, if any)
{chr(10).join(f'- {a}' for a in assumptions) if assumptions else '- (none)'}

QUESTION
{question}

QUERY RESULT
{result_block}{warnings_block}

SELF-CHECK BEFORE SENDING — every one of these must hold:
{chr(10).join(f'{i+1}. {c}' for i, c in enumerate(checks))}

Now write the final answer (plain prose, 1-3 sentences unless a breakdown
table is clearly needed).
"""


# ---------------------------------------------------------------------------
# Full-pipeline orchestration (assembly only — no LLM call)
# ---------------------------------------------------------------------------

@dataclass
class AssembledPipeline:
    question: str
    fast_gate: dict | None
    gate_prompt: str
    entity_matches: list[EntityMatch]
    absent_dimensions: list[dict]
    few_shots: list[dict]
    sql_prompt: str
    response_prompt_template: str  # response prompt with SQL/rows left as placeholders


def assemble(kb: CMElevateKB, question: str, few_shot_k: int = 6) -> AssembledPipeline:
    fast = fast_gate_check(kb, question)
    gate_prompt = build_gate_prompt(kb, question)
    matches = resolve_entities(kb, question)
    absent = detect_absent_dimensions(kb, question)
    few_shots = select_few_shots(kb, question, k=few_shot_k)
    sql_prompt = build_sql_prompt(kb, question, few_shot_k=few_shot_k)
    response_prompt = build_response_prompt(kb, question, sql=None, rows=None)
    return AssembledPipeline(
        question=question,
        fast_gate=fast,
        gate_prompt=gate_prompt,
        entity_matches=matches,
        absent_dimensions=absent,
        few_shots=few_shots,
        sql_prompt=sql_prompt,
        response_prompt_template=response_prompt,
    )


# ---------------------------------------------------------------------------
# Static "master" system prompt (question-agnostic, for teams that want one
# fixed prompt to paste into a single-call pipeline rather than wiring up
# the per-question assembler above).
# ---------------------------------------------------------------------------

# A curated, diverse subset of the 144 few-shot examples: enough to show
# every SQL pattern and every refusal cause without dumping all 144 into a
# static prompt. Picked by question text so this stays stable if the YAML
# is regenerated with examples in a different order.
_MASTER_PROMPT_EXAMPLE_QUESTIONS = [
    # core counts / grain
    "How many CM Elevate applications are there?",
    "How many beneficiaries are there under CM Elevate?",
    "How many distinct applications are there?",
    # the on-hold trap
    "How many applications are on hold?",
    # scheme dimension
    "How many applications are under the Piggery scheme?",
    "List all CM Elevate schemes",
    "What share of applications does each scheme hold?",
    "Compare the three PRIME schemes",
    # geography
    "How many applications are in Ri Bhoi?",
    "Applications by district",
    "How many villages are covered by CM Elevate?",
    # status / quality
    "Applications by verification outcome",
    "How many applications have no village match?",
    # applicant / gender (jsonb)
    "Applications by applicant type",
    "What is the gender split of CM Elevate applicants?",
    # mode
    "Applications by mode of submission",
    # ranking
    "Which scheme has the most applications on hold?",
    # cross-scheme
    "Show MGNREGA person-days for the villages in this CM Elevate result",
    # refusals — money (3), time (2), loan (1), outcome (2), identity (3),
    # workbook-conflict (2), write-op (1)
    "What is the total amount disbursed under CM Elevate?",
    "What is the total amount sanctioned under CM Elevate?",
    "What is the average disbursement per beneficiary?",
    "Show the CM Elevate application trend by month",
    "How many applications were made in 2024 versus 2025?",
    "How are loans split between Bank and LIFCOM?",
    "How many applications were desanctioned, and why?",
    "How many applications were approved?",
    "What is the name of the applicant on request REQ/001/001/10102025/188877?",
    "Find an applicant named Benadvin",
    "Show the date of birth and address for applications in Zikzak block",
    "How many applications are under the Common Facility Center scheme?",
    "Look up application MPDSI018599",
    "Update the status of a CM Elevate application",
]


def _curated_few_shots(kb: CMElevateKB) -> list[dict]:
    by_q = {ex["question"]: ex for ex in kb.few_shot}
    out = []
    for q in _MASTER_PROMPT_EXAMPLE_QUESTIONS:
        if q in by_q:
            out.append(by_q[q])
    return out


def build_master_system_prompt(kb: CMElevateKB) -> str:
    """One static, self-contained system prompt for the CM Elevate scheme —
    everything a single-call NL->SQL->answer pipeline needs, with no
    per-question retrieval step. Use assemble()/build_sql_prompt() instead
    when you can afford the extra retrieval call and want a tighter,
    question-specific prompt."""
    ds = kb.schema["datasets"]["v_cm_elevate"]
    nsr = kb.schema["nlp_sql_rules"]
    fmt = kb.templates["formatting"]
    checks = kb.templates["composer_checks"]["checks"]

    critical_gate_rules = [r for r in kb.gate_rules if _is_critical(r.get("note"))]
    critical_default_rules = [r for r in kb.default_rules if _is_critical(r.get("note"))]

    gate_lines = "\n".join(f"- {r['condition']}: {r['question']}" for r in critical_gate_rules)
    default_lines = "\n".join(
        f"- {d['condition']}: default={d.get('default_value')!r} — {d.get('assumption_text', '').strip()}"
        for d in critical_default_rules
    )
    templates_lines = "\n".join(f"- {k}: \"{v}\"" for k, v in kb.templates["templates"].items())

    return f"""# CM ELEVATE — MASTER SYSTEM PROMPT
Scheme: CM Elevate (`{kb.fk['database']}.{kb.fk['schema']}`, read role `{kb.fk['read_role']}`)
Primary query surface: `{kb.fk['primary_query_surface']}`
This scheme is 15 individual sub-schemes under one programme, application
data only — no money column exists anywhere in this partition, and no date
column exists anywhere in this partition. `COUNT(*)` is the entire
aggregate vocabulary.

## 1. Dataset
Table: {ds['table_name']} ({ds['object_type']} over {ds['over']})
Grain: {ds['grain']}
{ds.get('row_count_note', '').strip()}

## 2. Column reference (all {len(kb.view_columns)} columns)
{_column_ref_block(kb)}

## 3. Semantic rules
{_critical_semantic_rules(kb)}

## 4. SQL generation rules
- default FROM: {nsr['default_from']}
- allowed operations: {', '.join(nsr['allowed_operations'])} only (read-only role enforced at the DB too)
- aggregation defaults: {json.dumps(nsr['aggregation_defaults'])}
- {nsr['aggregation_note'].strip()}
- date_behavior: {nsr['date_behavior'].strip()}
- money_behavior: {nsr['money_behavior'].strip()}
- ranking: {nsr['ranking'].strip()}
- jsonb_behavior: {nsr['jsonb_behavior'].strip()}
- join_behavior: {nsr['join_behavior'].strip()}
- cross_scheme_behavior: {nsr['cross_scheme_behavior'].strip()}

## 5. Join graph
{_join_graph_block(kb)}

## 6. Clarification gate — CRITICAL conditions ({len(critical_gate_rules)} of {len(kb.gate_rules)} total rules)
If the question matches one of these, ask the paired question back to the
user INSTEAD of generating SQL. (The full 115-rule gate file has many more
non-critical conditions; this master prompt carries only the ones a wrong
answer would turn on. Use `cmelevate_classification_rules.yaml` directly, or
`build_gate_prompt()`, for full gate coverage.)
{gate_lines}

## 7. Silent defaults — CRITICAL ones ({len(critical_default_rules)} of {len(kb.default_rules)} total rules)
Apply these automatically when the gate above did not fire and the question
leaves the detail unspecified. State the assumption in the final answer.
{default_lines}

## 8. Non-negotiable behaviours
1. No SUM, no AVG — there is no measure column at all.
2. No date filter, no date GROUP BY, no date_trunc, no parsing request_id as
   a date, and never substitute ingested_at.
3. An "on hold" question routes to data_verified = 'On Hold', never onhold
   (FALSE on every row).
4. entity_type <> 'Unresolved' on every village count / village list, and on
   nothing else.
5. GROUP BY applicant_category, never type_raw.
6. LOWER() current_level and current_file_status before grouping/filtering.
7. Every scheme_specific (JSONB) read carries a cm_scheme_key or scheme_name
   filter.
8. request_id may return more than one row — never assume LIMIT 1 is safe.
9. There are 15 schemes, not 13; never adopt a figure from the requirement
   workbook (2,847 applications / 1,357 Piggery / 1,153 villages / any ₹
   total) — that describes a different dataset.

## 9. Few-shot examples (curated {len(_curated_few_shots(kb))} of {len(kb.few_shot)})
{format_few_shots(_curated_few_shots(kb))}

## 10. Answer composition
Formatting: number_format={fmt['number_format']}, currency_symbol=NEVER,
date_format=NEVER, null_display="{fmt['null_display']}",
zero_display="{fmt['zero_display']}",
empty_result_message="{fmt['empty_result_message']}".
{_rule_text(fmt.get('scope_must_name_scheme')).strip()}
{_rule_text(fmt.get('small_denominator_rule')).strip()}
{_rule_text(fmt.get('application_not_award_rule')).strip()}

Answer templates (fill placeholders, don't invent new phrasing):
{templates_lines}

Self-check before sending — every one of these must hold:
{chr(10).join(f'{i+1}. {c}' for i, c in enumerate(checks))}

## 11. Output contract
- If answerable: return the SQL, then the composed natural-language answer.
- If a gate condition fires: ask the paired clarification question, do not
  generate SQL.
- If genuinely unanswerable (no matching gate rule, but still not held in
  this data): "UNANSWERABLE: <reason naming the missing column/dimension>",
  and offer the nearest real question, named explicitly as a different
  question.
"""


# ---------------------------------------------------------------------------
# CLI / self-test
# ---------------------------------------------------------------------------

def _self_test(kb: CMElevateKB) -> None:
    """Sanity-checks the YAML contract: every file loads, every KB accessor
    resolves, and a handful of known question -> resolution / gate / SQL
    outcomes hold. Not a substitute for running the SQL against megh_db."""
    failures = []

    def check(label: str, cond: bool) -> None:
        print(f"{'OK  ' if cond else 'FAIL'} {label}")
        if not cond:
            failures.append(label)

    check("schema loaded", bool(kb.schema.get("datasets", {}).get("v_cm_elevate")))
    check("resolver loaded", bool(kb.resolver.get("dimensions")))
    check("22 view columns", len(kb.view_columns) == 22)
    check("15 cm_scheme values", len(kb.dimensions["cm_scheme"]["values"]) == 15)
    check("gate rules loaded", len(kb.gate_rules) > 50)
    check("default rules loaded", len(kb.default_rules) > 50)
    check("few-shot examples loaded", len(kb.few_shot) > 50)
    check("fk graph loaded", bool(kb.fk.get("nodes")))
    check("response templates loaded", bool(kb.templates.get("templates")))

    # Entity resolution
    m = resolve_entities(kb, "how many piggery applications are on hold")
    piggery = next((x for x in m if x.dimension == "cm_scheme"), None)
    check("resolves 'piggery' -> Meghalaya Piggery Development Scheme",
          bool(piggery and piggery.status == "resolved"
               and piggery.canonical == "Meghalaya Piggery Development Scheme"))

    m2 = resolve_entities(kb, "applications in EGH")
    egh = next((x for x in m2 if x.dimension == "district"), None)
    check("resolves district acronym 'EGH' -> East Garo Hills",
          bool(egh and egh.status == "resolved" and egh.canonical == "East Garo Hills"))

    m3 = resolve_entities(kb, "the vehicle scheme")
    vehicle_amb = next((x for x in m3 if x.dimension == "cm_scheme"), None)
    check("'the vehicle scheme' is ambiguous (blocked_matches)",
          bool(vehicle_amb and vehicle_amb.status == "ambiguous"))

    # Absent dimensions
    absent = detect_absent_dimensions(kb, "how much was disbursed under the warehouse scheme")
    check("'disbursed' triggers money absent-dimension",
          any(a["dimension"] == "money" for a in absent))

    absent2 = detect_absent_dimensions(kb, "show the application trend by month")
    check("'by month' triggers time absent-dimension",
          any(a["dimension"] == "time" for a in absent2))

    # Fast gate
    fg = fast_gate_check(kb, "how many applications are on hold")
    check("fast gate fires on 'on hold'",
          bool(fg and fg["condition"] == "onhold_flag_used_for_on_hold_question"))

    fg2 = fast_gate_check(kb, "what is the total amount disbursed")
    check("fast gate fires on money question",
          bool(fg2 and fg2["condition"] == "any_money_amount_requested"))

    fg3 = fast_gate_check(kb, "how many applications are in Ri Bhoi")
    check("fast gate does NOT fire on an ordinary geography question", fg3 is None)

    # SQL validator
    bad_sql = "SELECT COUNT(*) FROM curated.v_cm_elevate WHERE onhold = true;"
    issues = validate_sql(kb, bad_sql)
    check("validator flags onhold boolean misuse",
          any(i.rule == "onhold_boolean_used" and i.severity == "error" for i in issues))

    bad_sql2 = "SELECT SUM(village_code) FROM curated.v_cm_elevate;"
    issues2 = validate_sql(kb, bad_sql2)
    check("validator flags SUM over a real column",
          any(i.rule == "no_measure_exists" and i.severity == "error" for i in issues2))

    good_sql = ("SELECT scheme_name, COUNT(*) AS applications FROM curated.v_cm_elevate "
                "GROUP BY scheme_name ORDER BY applications DESC LIMIT 5;")
    issues3 = validate_sql(kb, good_sql)
    check("validator passes a clean scheme-ranking query with no errors",
          not any(i.severity == "error" for i in issues3))

    # Few-shot retrieval
    fs = select_few_shots(kb, "how many applications are on hold", k=3)
    check("few-shot retrieval returns results", len(fs) > 0)

    # Prompt assembly end-to-end
    p = assemble(kb, "how many piggery applications are on hold?")
    check("assemble() produces a non-empty sql_prompt", len(p.sql_prompt) > 500)
    check("assemble() produces a non-empty gate_prompt", len(p.gate_prompt) > 500)

    master = build_master_system_prompt(kb)
    check("master system prompt is non-empty", len(master) > 2000)

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED: {failures}")
        sys.exit(1)
    print("All self-tests passed.")


def _main() -> None:
    parser = argparse.ArgumentParser(description="CM Elevate prompt assembler / validator")
    parser.add_argument("--data-dir", default=None, help="Folder holding the 7 cmelevate_*.yaml files (default: this file's folder)")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--mode", choices=["master", "sql", "gate", "defaults", "entities", "response"], default="master")
    parser.add_argument("--question", default="How many CM Elevate applications are there?")
    parser.add_argument("--k", type=int, default=6, help="few-shot k for --mode sql")
    args = parser.parse_args()

    kb = CMElevateKB(data_dir=args.data_dir)

    if args.self_test:
        _self_test(kb)
        return

    if args.mode == "master":
        print(build_master_system_prompt(kb))
    elif args.mode == "sql":
        print(build_sql_prompt(kb, args.question, few_shot_k=args.k))
    elif args.mode == "gate":
        print(build_gate_prompt(kb, args.question))
    elif args.mode == "defaults":
        for d in _relevant_default_rules(kb, args.question, max_rules=20):
            print(f"- {d['condition']}: {d.get('assumption_text', '').strip()}")
    elif args.mode == "entities":
        print(build_entity_resolution_context(kb, args.question))
    elif args.mode == "response":
        print(build_response_prompt(kb, args.question, sql=None, rows=None))


if __name__ == "__main__":
    _main()
