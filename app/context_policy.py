"""
Field-level rules for carrying conversation state into a follow-up.

context_manager decides WHAT the previous turn was about (structured state,
evidence tiers). This module decides, field by field, what a follow-up does
to that state, and checks what a model rewrite is allowed to add. Everything
here is deterministic: no model call, no DB round trip.

    plan_state_merge(question, state, prev)  -> MergePlan
        per field: KEEP | REPLACE | CLEAR | REQUIRE_CLARIFICATION, plus the
        follow-up KIND (METRIC_CHANGE, RESULT_REFERENCE, ...), which picks the
        context layers the rewrite gets (context_layers()).
    apply_merge_plan(prior_resolved, plan)   -> what resolve_entities may inherit
    record_provenance(state, ...)            -> where each committed value came from
    rewrite_violation(rewritten, ...)        -> the field a rewrite added with no source

Why per-field and not one rule: fields differ in whether they can be inherited
at all, whether a coarser change invalidates them, whether they can be
validated offline, and whether a changed value is the user's intent or a
contamination. FIELD_POLICIES records that per field (docs/AI_PIPELINE.md
§5.4 has the same table). The policy is keyed by FIELD, never by scheme:
adding a scheme to SCHEME_CATALOG / _SCHEME_NAME_PATTERN needs nothing here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

KEEP = "KEEP"
REPLACE = "REPLACE"
CLEAR = "CLEAR"
REQUIRE_CLARIFICATION = "REQUIRE_CLARIFICATION"

# Where a committed state value came from. Only model_inference is refused as an
# inheritance source (see merged_prior_resolved): a value no user turn, pause
# reply, resolved reference or validated lookup supplied is not carried forward.
PROVENANCE_SOURCES = ("current_user", "previous_user", "clarification_response",
                      "validated_database", "resolved_reference", "model_inference")
_CONFIDENCE = {"current_user": 1.0, "clarification_response": 1.0, "previous_user": 1.0,
               "resolved_reference": 0.9, "validated_database": 0.9, "model_inference": 0.5}

# Follow-up kinds, and the previous-turn context layers each one needs.
NEW_QUERY = "NEW_QUERY"
FILTER_CHANGE = "FILTER_CHANGE"
METRIC_CHANGE = "METRIC_CHANGE"
TIME_CHANGE = "TIME_CHANGE"
GEOGRAPHY_CHANGE = "GEOGRAPHY_CHANGE"
GROUP_BY_CHANGE = "GROUP_BY_CHANGE"
COMPARISON = "COMPARISON"
RESULT_REFERENCE = "RESULT_REFERENCE"
CLARIFICATION_RESPONSE = "CLARIFICATION_RESPONSE"
# "same for <another scheme>": the previous operation, with only the scheme
# swapped (pipeline._scheme_substitution). Its layers are the previous filters,
# so geography, year and grouping carry over, and the scheme's own values
# (tranche, sub-scheme) are still inherited only within their own scheme.
SCHEME_SUBSTITUTION = "SCHEME_SUBSTITUTION"

# Layer names match context_manager.build_rewrite_evidence's tiers. "filters" is
# the previous turn's structured scope; "result" its row summary (with the
# answer excerpt as the no-rows fallback). The conversation summary and older
# turns are added only on an explicit back-reference, for every kind
# (context_manager.references_history).
_LAYERS = {
    NEW_QUERY: (),
    RESULT_REFERENCE: ("filters", "result"),
}
_DEFAULT_LAYERS = ("filters",)


def context_layers(kind: str | None) -> tuple[str, ...]:
    """The previous-turn layers a follow-up of this kind is given."""
    return _LAYERS.get(kind or "", _DEFAULT_LAYERS)


@dataclass(frozen=True)
class FieldPolicy:
    """How one state field behaves across turns (see module docstring)."""
    name: str
    inheritable: bool            # can a follow-up that doesn't mention it inherit it?
    replaceable: bool            # can the current question change it?
    clearable: bool              # can the current question remove it ("all years")?
    clarify_when: str            # when the pipeline asks instead of guessing
    validation: str              # how a value is checked against project data
    resolved_keys: tuple[str, ...] = ()      # its keys in resolve_entities' `resolved`
    clears_on_replace: tuple[str, ...] = ()  # finer fields a new value invalidates


FIELD_POLICIES: dict[str, FieldPolicy] = {p.name: p for p in (
    FieldPolicy("scheme", True, True, False,
                "no scheme named and no scheme vocabulary (scheme-not-specified); a bare "
                "'Focus' (focus-scheme-ambiguous)",
                "scheme registry (_SCHEME_NAME_PATTERN / SCHEME_CATALOG)"),
    FieldPolicy("district", True, True, True,
                "a hill-range name covering several districts (region-needs-district)",
                "resolver catalogue (12 districts, acronyms)",
                ("district", "district_list", "district_list_region"), ("block", "village")),
    FieldPolicy("block", True, True, True,
                "a name that is also a village or constituency (level collision)",
                "resolver catalogue (all schemes' block lists)",
                ("block", "block_list", "block_list_districts"), ("village",)),
    FieldPolicy("village", True, True, True,
                "several villages share the name (entity-ambiguous)",
                "database (resolve_village / dim_geography); not checkable offline",
                ("village_code", "village_code_list")),
    FieldPolicy("year", True, True, True,
                "a relative year ('last year') with no known year; a scheme that needs "
                "a year and none is given (year-not-specified)",
                "per-scheme FY coverage (_SCHEME_DATA_YEARS, year-gap handling)",
                ("year_key",)),
    # financial_year is the display form of the same field ("FY 2024-25" <->
    # year_key 2024). One policy, so the two can never disagree.
    FieldPolicy("financial_year", True, True, True, "as year", "as year", ("year_key",)),
    FieldPolicy("category", True, True, True,
                "a sub-scheme family name covering several sub-schemes (CM Elevate group "
                "ambiguity); tranche not stated for Focus Plus",
                "catalogue (tranche_label, cm_scheme) + closed demographic vocabulary",
                ("tranche_label", "cm_scheme", "house_status")),
    FieldPolicy("metric", True, True, False,
                "never asked: an unknown metric is answered as 'not tracked'",
                "metric vocabulary (context_manager._METRIC_KEYWORDS)"),
    FieldPolicy("group_by", False, True, True,
                "never asked", "dimension vocabulary"),
)}


# ── Deterministic signals ───────────────────────────────────────────────────
_YEAR_RANGE_RX = re.compile(r"\b(?:fy\s*)?(20\d{2})\s*[-/–]\s*(\d{2}|20\d{2})\b", re.IGNORECASE)
_YEAR_BARE_RX = re.compile(r"\b(?:fy\s*)?(20\d{2})\b", re.IGNORECASE)
_RELATIVE_YEAR_RX = re.compile(
    r"\b(?:last|previous|prior|next|following|preceding|same|this|current)\s+"
    r"(?:financial\s+|fiscal\s+)?(?:year|fy)\b|\byear\s+(?:before|after)\b", re.IGNORECASE)
_ALL_YEARS_RX = re.compile(
    r"\ball\s+(?:the\s+)?(?:financial\s+)?years\b|\bevery\s+year\b|\bacross\s+(?:all\s+)?years\b|"
    r"\b(?:year[\s-]?wise|by\s+year|per\s+year|each\s+year|cumulative|all[\s-]time)\b",
    re.IGNORECASE)
_ALL_GEO_RX = {
    "district": re.compile(
        r"\ball\s+(?:the\s+)?districts\b|\bevery\s+district\b|\bdistrict[\s-]?wise\b|"
        r"\b(?:by|per|each)\s+district\b|\bstate[\s-]?wide\b|\ball\s+of\s+meghalaya\b|"
        r"\b(?:whole|entire)\s+(?:state|meghalaya)\b|\bacross\s+(?:the\s+)?state\b", re.IGNORECASE),
    "block": re.compile(
        r"\ball\s+(?:the\s+)?blocks\b|\bevery\s+block\b|\bblock[\s-]?wise\b|"
        r"\b(?:by|per|each)\s+block\b", re.IGNORECASE),
    "village": re.compile(
        r"\ball\s+(?:the\s+)?villages\b|\bevery\s+village\b|\bvillage[\s-]?wise\b|"
        r"\b(?:by|per|each)\s+village\b", re.IGNORECASE),
}
_GROUP_BY_RX = re.compile(
    r"\b(?:by|per|each|across)\s+(?:the\s+)?(district|block|village|year|tranche|scheme|"
    r"gender|category|month|status|sub[\s-]?scheme)s?\b|"
    r"\b(district|block|village|year|scheme|tranche|gender|category|month|status)[\s-]?wise\b|"
    r"\bbreak\s+(?:it|that|this|them)?\s*down\b|\bbreakdown\b|\bsplit\b", re.IGNORECASE)
_COMPARE_RX = re.compile(r"\b(?:compare|comparison|versus|vs\.?|between)\b", re.IGNORECASE)
_ALL_CATEGORY_RX = re.compile(
    r"\ball\s+(?:the\s+)?(?:categories|tranches|sub[\s-]?schemes|genders)\b|"
    r"\b(?:everyone|all\s+beneficiaries|regardless\s+of)\b", re.IGNORECASE)
_DEMOGRAPHIC_RX = re.compile(
    r"\b(women|woman|female|females|men|male|males|gender|girls?|boys?|farmers?|widows?|"
    r"youth|disabled|pwd|divyang|minority|minorities|bpl|sc|st|obc|scheduled\s+(?:caste|tribe)s?|"
    r"general\s+category|pvtg)\b", re.IGNORECASE)
_TRANCHE_RX = re.compile(r"\btranch(?:e)?\s*[-#]?\s*(\d)\b", re.IGNORECASE)
# "all of them combined", "in total", "both together": every value of the
# dimension the conversation was just varying, not a filter on the last one.
_ALL_OF_THEM_RX = re.compile(
    r"\ball\s+(?:of\s+)?(?:them|these|those|three|two|four)\b|\bboth\b|\btogether\b|\bcombined\b|"
    r"\baltogether\b|\bin\s+total\b|\boverall\b", re.IGNORECASE)
_AMBIGUOUS_REF_RX = re.compile(r"\b(?:the\s+)?other\s+one\b|\bthe\s+former\b|\bthe\s+latter\b",
                               re.IGNORECASE)

# Metric FAMILIES for the rewrite check. Finer than "is there a metric", coarser
# than the labels context_manager tracks, so that a paraphrase ("how much" ->
# "amount disbursed") is the same family and only a genuinely new measure
# (money -> beneficiary count) is a change.
_METRIC_FAMILIES: dict[str, re.Pattern] = {
    "money": re.compile(r"\bhow\s+much\b|\bamount|disburs|\bpaid\b|\bpayments?\b|\bspen[dt]|"
                        r"expenditure|\bfunds?\b|releas|sanction(?:ed)?\s+amount|\bwages?\b|"
                        r"\bcost|\brs\.?\b|rupee|\blakh|\bcrore", re.IGNORECASE),
    "count": re.compile(r"\bhow\s+many\b|\bnumber\s+of\b|\bcount\b|beneficiar|applica|"
                        r"households?|job\s*cards?|registrations?|members|\bhouses\b|farmers|"
                        r"groups|\bpgs?\b", re.IGNORECASE),
    "days": re.compile(r"person[\s-]?days?|man[\s-]?days?|work[\s-]?days?|100[\s-]?days?",
                       re.IGNORECASE),
    "completion": re.compile(r"complet", re.IGNORECASE),
}

# Words a rewrite may capitalise without them being names (sentence starts,
# units, the state itself). Kept small on purpose; the name check only fires
# for a capitalised word that ALSO appears in text the rewrite was not allowed
# to draw from.
_NAME_STOPWORDS = {
    "what", "how", "which", "who", "when", "where", "show", "list", "give", "tell", "total",
    "the", "for", "under", "in", "and", "of", "was", "were", "is", "are", "did", "does",
    "fy", "financial", "year", "years", "meghalaya", "district", "districts", "block",
    "blocks", "village", "villages", "scheme", "schemes", "tranche", "tranch", "amount",
    "number", "all", "each", "by", "compare", "between", "during", "from", "to", "with",
    "rs", "inr", "lakh", "crore", "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
}
_CAP_WORD_RX = re.compile(r"\b[A-Z][A-Za-z]{2,}\b")


def extract_year_keys(text: str) -> set[int]:
    """Financial-year start years named in the text: "FY 2024-25", "2024-25",
    "2024/25" -> 2024; a bare "2024" -> 2024."""
    t = text or ""
    years = {int(m.group(1)) for m in _YEAR_RANGE_RX.finditer(t)}
    stripped = _YEAR_RANGE_RX.sub(" ", t)
    years |= {int(m.group(1)) for m in _YEAR_BARE_RX.finditer(stripped)}
    return years


def metric_families(text: str) -> set[str]:
    return {name for name, rx in _METRIC_FAMILIES.items() if rx.search(text or "")}


def category_terms(text: str) -> set[str]:
    """Closed-vocabulary category values in the text, normalised: demographic
    words, Focus Plus tranche numbers, and every CM Elevate sub-scheme name
    any catalogue holds."""
    t = text or ""
    out = {m.group(1).lower().rstrip("s") for m in _DEMOGRAPHIC_RX.finditer(t)}
    out |= {f"tranche {m.group(1)}" for m in _TRANCHE_RX.finditer(t)}
    low = t.lower()
    for form, canonical in _catalog_category_forms():
        if re.search(rf"(?<![a-z]){re.escape(form)}(?![a-z])", low):
            out.add(canonical)
    return out


def _catalog_category_forms() -> list[tuple[str, str]]:
    """(lower-case name form, lower-case canonical) for every sub-scheme the
    catalogues hold, canonical name and aliases alike, so "Piggery" and
    "Meghalaya Piggery Development Scheme" are the same category. Forms under
    5 characters ("pig") are skipped as too likely to occur by accident."""
    try:
        from app import entity_resolver as er
        forms: set[tuple[str, str]] = set()
        for dims in er._catalog.values():
            for v in dims.get("cm_scheme") or []:
                canonical = str(v.get("canonical") or "").lower()
                for f in [canonical, *(v.get("aliases") or [])]:
                    f = str(f).lower().strip()
                    if canonical and len(f) >= 5:
                        forms.add((f, canonical))
        return sorted(forms, key=lambda x: -len(x[0]))
    except Exception:  # noqa: BLE001 — no catalogue loaded: demographic words only
        return []


def _places(text: str) -> dict[str, str]:
    try:
        from app.entity_resolver import named_places
        return named_places(text)
    except Exception:  # noqa: BLE001
        return {}


def _schemes_named(text: str) -> set[str]:
    try:
        from app import pipeline as _p
        return set(_p._named_schemes(text)) | set(_p._infer_scheme_from_terms(text) or [])
    except Exception:  # noqa: BLE001
        return set()


# ── Does the message continue the conversation at all? ─────────────────────
# Reported 2026-09-29, after a Focus Plus answer: "who is harshit", then "he is
# my collik remember", were both answered with Focus Plus reference material,
# and "now give me five thousand loan for me i am in crisis" became a Focus Plus
# DATA question that paused for the year. looks_like_followup() reads any short
# message with no anchor word as a fragment, and edge.detect_edge_case skips its
# off-topic whitelist whenever a previous answer exists, so the previous scheme
# was inherited by messages that share nothing with it.
#
# A message may continue the previous turn only when it carries something that
# can: scheme or measure vocabulary, a place, a year, a grouping or comparison,
# a reference into the previous answer, or a question about how a scheme works.
# Deterministic; a message with none of these is a NEW query, and routes as one.
_REFERENCE_RX = re.compile(
    r"\b(?:it|its|that|those|these|them|they|there|this|same|one|ones|former|latter|other|"
    r"both|each|either|neither|rest|remaining|previous|above|earlier|again|instead|"
    # meta: "what did I ask before?", "you said 5,000"
    r"before|asked|said|told|answer(?:ed)?|question)\b",
    re.IGNORECASE)
_RANK_RX = re.compile(
    r"\b(?:top|bottom|first|second|third|last|next|highest|lowest|largest|smallest|biggest|"
    r"most|least|more|less|fewer|higher|lower|bigger|smaller|greater|max(?:imum)?|min(?:imum)?|"
    r"average|mean|sum|total|count|rank|ranking|sort|order|percent(?:age)?|share|ratio|rate|"
    r"difference|change|growth|trend|increase|decrease)\b", re.IGNORECASE)
# How-the-scheme-works vocabulary: the knowledge follow-ups ("Documents?",
# "who can apply?", "when was it launched?") that name no scheme of their own.
_SCHEME_TOPIC_RX = re.compile(
    r"\b(?:benefits?|eligib\w*|documents?|apply|applying|application|applicants?|criteria|"
    r"launch\w*|start(?:ed)?|objectives?|purpose|aims?|goals?|components?|rules?|guidelines?|"
    r"procedure|process|steps?|entitle\w*|subsid\w*|grants?|loans?|interest|amount|payments?|"
    r"instal+ments?|tranch\w*|batch\w*|status|agency|agencies|department|ministry|officer|"
    r"nodal|implement\w*|fund\w*|budget|covered|coverage|register\w*|enrol\w*|helpline|"
    r"contact|portal|website|office|deadline|age|income|land|farmers?|women|men|gender|"
    r"categor\w*|sectors?|programmes?|programs?|sub[\s-]?schemes?|details?|more|explain|"
    r"elaborate|why|how)\b", re.IGNORECASE)
_LEAD_RX = re.compile(
    r"^\s*(?:what|how)\s+about\b|^\s*what\s+of\b|^\s*(?:and|also|same|then)\b", re.IGNORECASE)


def continuation_signals(question: str) -> list[str]:
    """Why `question` may continue the previous turn, as a list of signal
    names (empty = it shares nothing with a scheme conversation). Read by
    pipeline._run_pipeline before the edge check and the follow-up rewrite,
    and logged, so "why did this turn inherit the scheme?" has an answer."""
    q = (question or "").strip()
    if not q:
        return []
    out: list[str] = []
    try:
        from app import edge
        if edge.has_domain_vocabulary(q):
            out.append("domain")
    except Exception:  # noqa: BLE001 — no edge vocabulary: the other signals still count
        pass
    if _schemes_named(q):
        out.append("scheme")
    if _places(q):
        out.append("place")
    if extract_year_keys(q) or _RELATIVE_YEAR_RX.search(q) or _ALL_YEARS_RX.search(q):
        out.append("year")
    if metric_families(q):
        out.append("metric")
    if category_terms(q):
        out.append("category")
    if _GROUP_BY_RX.search(q) or any(rx.search(q) for rx in _ALL_GEO_RX.values()):
        out.append("group_by")
    if _COMPARE_RX.search(q):
        out.append("compare")
    if _REFERENCE_RX.search(q) or _AMBIGUOUS_REF_RX.search(q):
        out.append("reference")
    if _RANK_RX.search(q):
        out.append("rank")
    if _SCHEME_TOPIC_RX.search(q):
        out.append("scheme_topic")
    # "what about Mawlai?" / "and Nongpoh?": an explicit continuation lead with
    # a short remainder. Villages are not catalogued offline, so the remainder
    # cannot be checked here; the lead itself is the user saying "continue".
    if _LEAD_RX.search(q) and len(q.split()) <= 6:
        out.append("lead")
    # Khasi / Garo / Bengali / Hindi script: none of the English vocabularies
    # above can read it, and edge.detect_edge_case lets it through for the model
    # to read (its step 5). Unknown is not "unrelated", so it keeps its context.
    if sum(1 for c in q if ord(c) > 127) >= 3:
        out.append("non_english")
    return out


# "Which one?" / "that one?" / "which of them?" with nothing else: a pointer
# with no predicate. What it points at cannot be read from the previous result
# (which one of what, by what measure?), so the answer is a question back, not
# a rewrite the model has to guess.
_BARE_REFERENCE_RX = re.compile(
    r"^\s*(?:and|so|ok(?:ay)?|then)?\s*,?\s*(?:which|what|that|this|the)\s+(?:one|ones)"
    r"(?:\s+(?:then|now|exactly|again))?\s*[?.!]*\s*$|"
    r"^\s*(?:and\s+)?which\s+of\s+(?:them|these|those)\s*[?.!]*\s*$",
    re.IGNORECASE)


def is_bare_reference(question: str) -> bool:
    return bool(_BARE_REFERENCE_RX.match(question or ""))


# ── Merge plan ──────────────────────────────────────────────────────────────
@dataclass
class MergePlan:
    actions: dict[str, str] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    kind: str = FILTER_CHANGE

    def action(self, name: str) -> str:
        return self.actions.get(name, KEEP)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "actions": dict(self.actions)}


def plan_state_merge(question: str, state=None, *, prev=None, is_followup: bool = True,
                     clarification_response: bool = False) -> MergePlan:
    """Field-by-field effect of `question` on the conversation state.

    Only what the question itself says counts, as read by deterministic
    scanners (scheme registry, resolver catalogue, year/metric/category
    vocabularies). A field the question says nothing about is KEEP; a named
    new value is REPLACE (and clears the finer geography under it); an "all
    X" / "by X" is CLEAR; a reference that cannot be pinned is
    REQUIRE_CLARIFICATION. Never guesses a value."""
    q = question or ""
    plan = MergePlan()
    act, why = plan.actions, plan.reasons

    def set_(name, action, reason):
        act[name] = action
        why[name] = reason

    for name in ("scheme", "district", "block", "village", "year", "category", "metric",
                 "group_by"):
        act[name] = KEEP

    prev_schemes = set(getattr(prev, "schemes", None) or [])
    state_scheme = getattr(state, "scheme", None)
    current = prev_schemes | ({state_scheme} if state_scheme else set())
    named = _schemes_named(q)
    if named and named != current:
        set_("scheme", REPLACE, f"names {sorted(named)}")

    places = _places(q)
    districts = [n for n, d in places.items() if d == "district"]
    blocks = [n for n, d in places.items() if d == "block"]
    if districts:
        set_("district", REPLACE, f"names {districts}")
    if blocks:
        set_("block", REPLACE, f"names {blocks}")
    for dim, rx in _ALL_GEO_RX.items():
        if rx.search(q) and act[dim] != REPLACE:
            set_(dim, CLEAR, "all / by " + dim)
    # A coarser geography change invalidates the finer levels under it, unless
    # the question names them too ("Dalu block in West Garo Hills").
    for coarse in ("district", "block"):
        if act[coarse] in (REPLACE, CLEAR):
            for fine in FIELD_POLICIES[coarse].clears_on_replace:
                if act[fine] == KEEP:
                    set_(fine, CLEAR, f"{coarse} changed")

    years = extract_year_keys(q)
    if years:
        set_("year", REPLACE, f"names {sorted(years)}")
    elif _RELATIVE_YEAR_RX.search(q):
        known = getattr(state, "year", None)
        prev_year = (getattr(prev, "resolved_entities", None) or {}).get("year_key")
        if known is None and prev_year is None:
            set_("year", REQUIRE_CLARIFICATION, "relative year with no known year")
        else:
            set_("year", REPLACE, "relative year")
    if _ALL_YEARS_RX.search(q) and act["year"] != REPLACE:
        set_("year", CLEAR, "all years / by year")
    # "and all of them combined?" after "what about 2025-26?": the dimension the
    # user was just varying (live 2026-09-29: it kept FY 2025-26 and repeated 137
    # instead of both years together). Only when the question names no value of
    # its own; with no varied dimension on record the model rewrite decides.
    _last = getattr(state, "last_dimension", None)
    if (_ALL_OF_THEM_RX.search(q) and _last in ("year", "district", "block")
            and act.get(_last) == KEEP and not places and not years):
        set_(_last, CLEAR, f"'all of them' = every {_last} just compared")
        for fine in FIELD_POLICIES[_last].clears_on_replace if _last != "year" else ():
            if act[fine] == KEEP:
                set_(fine, CLEAR, f"{_last} changed")

    if metric_families(q):
        set_("metric", REPLACE, f"names {sorted(metric_families(q))}")
    cats = category_terms(q)
    if cats:
        set_("category", REPLACE, f"names {sorted(cats)}")
    elif _ALL_CATEGORY_RX.search(q):
        set_("category", CLEAR, "all categories")

    group = _GROUP_BY_RX.search(q)
    if group:
        set_("group_by", REPLACE, group.group(0))

    if _AMBIGUOUS_REF_RX.search(q):
        ents = list(getattr(state, "comparison_entities", None) or [])
        if len(ents) != 2:
            set_("reference", REQUIRE_CLARIFICATION,
                 f"'other one/former/latter' with {len(ents)} candidates")
    elif is_bare_reference(q):
        set_("reference", REQUIRE_CLARIFICATION, "a bare 'which one?' with nothing to pick by")

    plan.kind = _kind(plan, q, is_followup=is_followup,
                      clarification_response=clarification_response)
    return plan


def _kind(plan: MergePlan, q: str, *, is_followup: bool, clarification_response: bool) -> str:
    from app.context_manager import references_previous_result
    if clarification_response:
        return CLARIFICATION_RESPONSE
    if not is_followup:
        return NEW_QUERY
    if plan.action("scheme") == REPLACE:
        from app import pipeline as _p
        if _p._scheme_substitution(q)[0]:
            return SCHEME_SUBSTITUTION
    if references_previous_result(q):
        return RESULT_REFERENCE
    if _COMPARE_RX.search(q):
        return COMPARISON
    if plan.action("group_by") == REPLACE:
        return GROUP_BY_CHANGE
    changed = {n for n in ("district", "block", "village", "year", "metric", "category", "scheme")
               if plan.action(n) in (REPLACE, CLEAR)}
    geo = {"district", "block", "village"}
    if not changed:
        return FILTER_CHANGE
    if changed <= geo:
        return GEOGRAPHY_CHANGE
    if changed == {"year"}:
        return TIME_CHANGE
    if changed == {"metric"}:
        return METRIC_CHANGE
    return FILTER_CHANGE


def apply_merge_plan(prior_resolved: dict | None, plan: MergePlan | None) -> dict:
    """What resolve_entities may inherit, given the plan. A field the question
    REPLACEs, CLEARs or can't pin is removed from the fallback: the question
    supplies it, removed it, or the existing gate must ask. KEEP leaves it.
    Only ever removes keys; never adds or rewrites a value."""
    out = dict(prior_resolved or {})
    if plan is None:
        return out
    for name, policy in FIELD_POLICIES.items():
        if plan.action(name) in (REPLACE, CLEAR, REQUIRE_CLARIFICATION):
            for key in policy.resolved_keys:
                out.pop(key, None)
            if name == "category" and plan.action(name) == CLEAR:
                out.pop("tranche_all_combined", None)
    return out


# ── Provenance ──────────────────────────────────────────────────────────────
def _value_named_in(field_name: str, value, text: str) -> bool:
    if value is None or not text:
        return False
    if field_name in ("district", "block"):
        return str(value).upper() in _places(text)
    if field_name == "year":
        try:
            return int(value) in extract_year_keys(text)
        except (TypeError, ValueError):
            return False
    if field_name == "scheme":
        return value in _schemes_named(text)
    if field_name == "metric":
        from app.context_manager import detect_metric
        return detect_metric(text) == value
    return str(value).lower() in text.lower()


def provenance_for(field_name: str, value, *, raw_question: str, substituted_question: str,
                   prior_value, prior_record: dict | None, clarification_reply: str | None,
                   previous_question: str, validated: bool) -> dict:
    """{"value", "source", "confidence"} for one committed value. Checked in
    order of strength: the pause reply, the user's own words, a deterministic
    reference substitution, the previous turn, a validated lookup, and only
    then model inference."""
    if clarification_reply and _value_named_in(field_name, value, clarification_reply):
        source = "clarification_response"
    elif _value_named_in(field_name, value, raw_question):
        source = "current_user"
    elif substituted_question != raw_question and _value_named_in(field_name, value,
                                                                   substituted_question):
        source = "resolved_reference"
    elif prior_value is not None and str(prior_value) == str(value):
        prior_source = (prior_record or {}).get("source")
        source = "model_inference" if prior_source == "model_inference" else "previous_user"
    elif _value_named_in(field_name, value, previous_question):
        source = "previous_user"
    elif validated:
        source = "validated_database"
    else:
        source = "model_inference"
    return {"value": value, "source": source, "confidence": _CONFIDENCE[source]}


# ── Rewrite checks ──────────────────────────────────────────────────────────
def rewrite_violation(rewritten: str, *, question: str, allowed_text: str, denied_text: str,
                      allowed_schemes: list[str], state_year: int | None = None,
                      cleared: dict | None = None) -> str | None:
    """The field ('scheme' | 'district' | 'block' | 'year' | 'metric' |
    'category' | 'name') that a rewritten follow-up names with no permitted
    source, else None. Each field has its own rule:

      scheme / district / block  must appear, canonically, in a permitted source.
      year      may be any year a permitted source names, or +-1 of one when
                the follow-up uses a relative year ("last year"), so an
                explicit year change by the user is always allowed.
      metric    compared by family (money / count / days / completion), so a
                paraphrase passes and a new measure from nowhere does not.
      category  closed vocabulary (demographics, tranche numbers, sub-schemes).
      name      any capitalised word that appears in text the rewrite was NOT
                allowed to use (the previous answer, the summary and older
                turns without a back-reference) and in no permitted source.
                This is how villages are covered offline: a village name can
                only leak from text the model saw.
    """
    from app import pipeline as _p

    # A filter the follow-up REMOVED ("show it by district") must not come back
    # through the rewrite, even though the previous question (a permitted
    # source) names it.
    for fld, value in (cleared or {}).items():
        if fld in ("district", "block") and str(value).upper() in _places(rewritten):
            return f"cleared:{fld}"
        if fld == "year" and extract_year_keys(str(value)) & extract_year_keys(rewritten):
            return "cleared:year"

    if not _p._CROSS_SCHEME_FOLLOWUP.search(question or ""):
        ok = (set(allowed_schemes) | _p._exact_schemes(allowed_text) | _schemes_named(question))
        if _p._exact_schemes(rewritten) - ok:
            return "scheme"
    permitted = _places(allowed_text)
    for name, dim in _places(rewritten).items():
        if name not in permitted:
            return dim
    new_years = extract_year_keys(rewritten)
    if new_years:
        ok_years = extract_year_keys(allowed_text) | ({state_year} if state_year else set())
        if _RELATIVE_YEAR_RX.search(question or ""):
            ok_years |= {y + d for y in set(ok_years) for d in (-1, 1)}
        if new_years - ok_years:
            return "year"
    if metric_families(rewritten) - metric_families(allowed_text):
        return "metric"
    # The follow-up's OWN measure must survive the rewrite. Live 2026-09-29:
    # "give me beneficiaries" became "What is the disbursement for Focus Plus…"
    # — money was allowed (the state line said metric=disbursement), so the
    # check above passed while the count the user asked for was gone. "how
    # much" is left out: it is money or quantity depending on the noun.
    own = metric_families(re.sub(r"\bhow\s+much\b", " ", question or "", flags=re.IGNORECASE))
    if own - metric_families(rewritten):
        return "metric"
    if category_terms(rewritten) - category_terms(allowed_text):
        return "category"
    if denied_text:
        allowed_low = (allowed_text or "").lower()
        denied_low = denied_text.lower()
        for w in _CAP_WORD_RX.findall(rewritten):
            lw = w.lower()
            if lw in _NAME_STOPWORDS:
                continue
            in_denied = re.search(rf"(?<![a-z]){re.escape(lw)}(?![a-z])", denied_low)
            in_allowed = re.search(rf"(?<![a-z]){re.escape(lw)}(?![a-z])", allowed_low)
            if in_denied and not in_allowed:
                return "name"
    return None
