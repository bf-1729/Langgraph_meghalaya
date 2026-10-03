"""
Conversation Context Manager — the ChatGPT-style multi-turn layer around the
existing follow-up rewrite (see app.pipeline.looks_like_followup /
rewrite_followup / the prior_resolved entity carry in resolve_entities).

This module does NOT replace any of that. It sits immediately around it:

    question
       |
       v
    substitute_references()   <- deterministic: "the previous year" -> "FY 2023-24",
       |                          "the former/latter/other one" -> a named entity
       v
    [existing] looks_like_followup() / rewrite_followup()   <- unchanged
       |
       v
    inject_scheme_hint()      <- deterministic: append the pinned scheme's name
       |                          when the (possibly rewritten) question still
       |                          names none, so classify_scheme() doesn't have
       |                          to guess or pause
       v
    merged_prior_resolved()   <- extends resolve_entities()'s existing
                                  prior_resolved fallback with session-level
                                  structured state, so a turn that pinned no
                                  entities of its own (a KNOWLEDGE digression)
                                  doesn't erase the DATA context a later
                                  follow-up still needs
       v
    [existing pipeline continues unchanged: classify_intent, classify_scheme,
     resolve_entities, generate_sql, auth.authorize, execute, compose_response]
       |
       v
    update_state()             <- Context Updater: folds the finished turn's
                                   resolved entities back into session.state
    maybe_update_summary()     <- periodic, best-effort conversation summary

Every function here is defensive by construction: on any internal error it
returns the input unchanged (or an empty enrichment) rather than raising, so
CONTEXT_LAYER_ENABLED=false or a bug in this module can never take the core
pipeline down with it (failure-tolerance requirement — see README section 9
of the spec this was built against).

None of this ever touches authorization. Every SQL query the pipeline builds
is still re-authorized from scratch by auth.authorize() against the live
scope on every turn — this module only ever changes question TEXT and
resolved-entity HINTS that feed the existing SQL-generation prompt; it never
grants access to a query the current turn's own scope wouldn't already pass.
"""
import logging
import re
import time
from decimal import Decimal

from app.config import settings
from app.session_store import ConversationState, Session

logger = logging.getLogger(__name__)


class AmbiguousReference(Exception):
    """Raised by substitute_references() when a pronoun-like reference ("the
    other one", "the latter") cannot be resolved with confidence — e.g. no
    comparison_entities on record, or more than two candidates. The caller
    (app.pipeline) turns this into the existing ClarificationNeeded pause
    (same UI, same one-tap-chip mechanism as every other clarification) —
    this module deliberately does not know about ClarificationNeeded so it
    stays free of a dependency back on pipeline.py at import time."""

    def __init__(self, question: str, options: list[dict] | None = None):
        super().__init__(question)
        self.question = question
        self.options = options or []


# ── Reference resolution ────────────────────────────────────────────────────
_PREV_YEAR_RX = re.compile(
    r"\b(the\s+)?(previous|last|prior)\s+(financial\s+)?year\b", re.IGNORECASE)
_CURR_YEAR_RX = re.compile(
    r"\b(the\s+)?(current|this|same)\s+(financial\s+)?year\b", re.IGNORECASE)
_FORMER_RX = re.compile(r"\bthe\s+former\b", re.IGNORECASE)
_LATTER_RX = re.compile(r"\bthe\s+latter\b", re.IGNORECASE)
_OTHER_ONE_RX = re.compile(r"\b(the\s+)?other\s+one\b", re.IGNORECASE)
# Negative lookahead on "schemes?" so "both schemes"/"both scheme" is left
# untouched — that exact phrasing already has its own established meaning
# (pipeline._EXPLICIT_BOTH -> classify_scheme's whole-catalog shortcut) and
# must not be clobbered by a district/block comparison recorded earlier in
# the conversation (e.g. "compare both schemes" after "compare West Garo
# Hills and East Garo Hills" would otherwise garble into "... schemes").
_BOTH_RX = re.compile(r"\bboth\b(?!\s+schemes?\b)", re.IGNORECASE)


def _fy_text(year_key: int) -> str:
    return f"FY {year_key}-{(year_key + 1) % 100:02d}"


def substitute_references(question: str, state: "ConversationState | None") -> str:
    """Deterministically resolve the reference phrases the spec calls out
    explicitly ("the previous year", "the current year", "the former/latter",
    "the other one", "both") against structured session state, BEFORE the
    question reaches the existing follow-up detector. Anything this function
    doesn't recognise is left untouched — "it"/"that"/"this"/"them"/"there"
    stay exactly as typed, for the existing looks_like_followup /
    rewrite_followup LLM path to handle from the previous turn's text (that
    path already exists and already works for plain pronouns; duplicating it
    here would be the "duplicate follow-up system" the spec says not to
    build).

    Raises AmbiguousReference (never guesses) when a comparison reference
    ("the former", "the other one") has no recorded comparison_entities to
    resolve against, or more than two candidates."""
    if not question or not state:
        return question
    q = question

    if state.year is not None and _PREV_YEAR_RX.search(q):
        q = _PREV_YEAR_RX.sub(_fy_text(state.year - 1), q)
    if state.year is not None and _CURR_YEAR_RX.search(q):
        q = _CURR_YEAR_RX.sub(_fy_text(state.year), q)

    if _FORMER_RX.search(q) or _LATTER_RX.search(q) or _OTHER_ONE_RX.search(q):
        ents = state.comparison_entities or []
        if len(ents) < 2:
            raise AmbiguousReference(
                "Which one did you mean? I don't have two things on the table to "
                "compare yet — please name it directly."
            )
        if len(ents) > 2 and (_FORMER_RX.search(q) or _LATTER_RX.search(q)):
            # "the former"/"the latter" only make sense for exactly two —
            # with 3+ recorded, don't guess which pair the user means.
            raise AmbiguousReference(
                f"You mentioned {len(ents)} — {', '.join(ents)}. Which one do you mean?",
                options=[{"label": e, "question": q.replace("the former", e).replace("the latter", e)}
                         for e in ents],
            )
        if _FORMER_RX.search(q):
            q = _FORMER_RX.sub(ents[0], q)
        if _LATTER_RX.search(q):
            q = _LATTER_RX.sub(ents[-1], q)
        if _OTHER_ONE_RX.search(q):
            if len(ents) != 2:
                raise AmbiguousReference(
                    f"You mentioned {len(ents)} — {', '.join(ents)}. Which one is "
                    "\"the other one\"?",
                    options=[{"label": e, "question": q.replace("the other one", e)} for e in ents],
                )
            # Which one is "the other" depends on which one the rest of the
            # question already names; if neither/both are named, don't guess.
            named = [e for e in ents if e.lower() in q.lower()]
            other = [e for e in ents if e not in named]
            if len(named) == 1 and len(other) == 1:
                q = _OTHER_ONE_RX.sub(other[0], q)
            else:
                raise AmbiguousReference(
                    f"\"The other one\" of {', '.join(ents)} — which is already named and "
                    "which is \"the other\"?",
                    options=[{"label": e, "question": q.replace("the other one", e)} for e in ents],
                )

    if _BOTH_RX.search(q) and len(state.comparison_entities or []) == 2:
        q = _BOTH_RX.sub(" and ".join(state.comparison_entities), q)

    if q != question:
        logger.info("context_manager.substitute_references: %r -> %r", question, q)
    return q


# ── Scheme-hint injection ───────────────────────────────────────────────────
def inject_scheme_hint(question: str, state: "ConversationState | None") -> str:
    """When a follow-up fragment (already rewritten to standalone form by the
    existing rewrite_followup) still names no scheme and no scheme-specific
    vocabulary of its own, deterministically append the session's pinned
    scheme — so classify_scheme()'s existing exact-match shortcut
    (_named_schemes) picks it up instead of falling through to "which
    scheme?" or guessing "both". Only fires when state.scheme is a single
    pinned scheme; a multi-scheme comparison state is left alone (nothing
    safe to inject). Never touches a question that already names/implies a
    scheme — this is purely a gap-filler, never an override."""
    if not question or not state or not state.scheme:
        return question
    try:
        # Lazy import: avoids a module-load-order cycle with app.pipeline,
        # which imports this module. By the time this runs, pipeline is
        # already fully loaded (this is only ever called from within a
        # pipeline request handler).
        from app import pipeline as _pipeline
    except Exception:  # noqa: BLE001
        return question
    try:
        if _pipeline._named_schemes(question):
            return question
        if _pipeline._infer_scheme_from_terms(question):
            return question
    except Exception:  # noqa: BLE001 — private helpers may change shape; degrade safely
        return question
    out = f"{question.rstrip(' ?.')} under {state.scheme}?"
    logger.info("context_manager.inject_scheme_hint: %r -> %r", question, out)
    return out


def inject_year_scope(question: str, state: "ConversationState | None",
                      year_action: str = "KEEP") -> str:
    """Carry an "all financial years" choice into a follow-up that keeps the
    year (KI-030). Live 2026-09-26: after the user picked "All financial years
    combined", "Show it by district." paused for the year again — an all-years
    choice is not a resolved entity, so neither prior_resolved nor the state
    carried it. Appends the phrase the year gate reads (_ALL_YEARS_CUE) only
    when: the state holds year_all; the follow-up neither names nor changes the
    year; it is not a how-the-scheme-works question; and no scheme it names
    lacks a time dimension (CM Elevate). Otherwise the question is unchanged."""
    # A follow-up that CLEARs the year ("all years", "all of them combined"
    # after two single-year turns) gets the phrase too, so the year gate reads
    # the choice instead of asking it again.
    if not question or not state:
        return question
    if not ((getattr(state, "year_all", False) and year_action == "KEEP") or year_action == "CLEAR"):
        return question
    try:
        from app import context_policy as _cp
        from app import pipeline as _pipeline
        if _cp.extract_year_keys(question) or _cp._RELATIVE_YEAR_RX.search(question):
            return question
        if _pipeline._ALL_YEARS_CUE.search(question) or _pipeline._KNOWLEDGE_HINTS.search(question):
            return question
        schemes = set(_pipeline._named_schemes(question)) | ({state.scheme} if state.scheme else set())
        if any(not _pipeline._SCHEME_DATA_YEARS.get(s, ["?"]) for s in schemes):
            return question
    except Exception:  # noqa: BLE001 — private helpers may change shape; degrade safely
        return question
    out = f"{question.rstrip(' ?.')} across all financial years?"
    logger.info("context_manager.inject_year_scope: %r -> %r", question, out)
    return out


# ── Entity-inheritance fallback (extends resolve_entities' prior_resolved) ──
def state_to_resolved_entities(state: "ConversationState | None") -> dict:
    """The subset of structured state that resolve_entities' prior_resolved
    fallback already knows how to consume (district/block/year_key/
    village_code) — see app.pipeline.resolve_entities. Used as the
    session-level floor beneath the turn-level prev.resolved_entities, so a
    KNOWLEDGE digression in between (which leaves state untouched — see
    update_state) doesn't erase what a later DATA follow-up still needs."""
    if not state:
        return {}
    out: dict = {}
    if state.district:
        out["district"] = state.district
    if state.block:
        out["block"] = state.block
    if state.village:
        out["village_code"] = state.village
    if state.year is not None:
        out["year_key"] = state.year
    if state.tranche:
        out["tranche_label"] = state.tranche
    if state.tranche_all_combined:
        # Not a real stored value — must never be copied into resolve_entities'
        # `resolved` dict (that feeds the SQL prompt's WHERE-clause filter
        # verbatim, see prompt_builder._entities_block). This key exists only
        # so _answer_data can tell _needs_tranche_clarification "the user
        # already picked 'all tranches combined' earlier this session" without
        # re-asking on a bare follow-up that doesn't restate "tranche".
        out["tranche_all_combined"] = True
    return out


def merged_prior_resolved(turn_resolved: dict | None, state: "ConversationState | None") -> dict:
    """turn-level prior_resolved (the immediately previous turn's own
    resolved_entities) takes priority; session-level structured state fills
    in whatever that turn didn't have (typically because it was a KNOWLEDGE
    or EDGE turn with no resolved_entities of its own)."""
    base = state_to_resolved_entities(state)
    # A state value whose recorded source is model_inference (no user turn,
    # pause reply, resolved reference or validated lookup supplied it) is not
    # inherited. Provenance is recorded by update_state (context_policy).
    prov = getattr(state, "provenance", None) or {}
    for fld, key in _STATE_FIELD_KEYS.items():
        if (prov.get(fld) or {}).get("source") == "model_inference":
            base.pop(key, None)
    base.update({k: v for k, v in (turn_resolved or {}).items() if v is not None})
    return base


# ConversationState field -> its resolve_entities key.
_STATE_FIELD_KEYS = {"district": "district", "block": "block", "village": "village_code",
                     "year": "year_key", "tranche": "tranche_label"}


# ── Metric-label tracking (for structured state / summary only — never fed
#    into SQL generation, which reads the question text directly as today) ──
_METRIC_KEYWORDS: list[tuple[str, "re.Pattern"]] = [
    ("person-days", re.compile(r"person[\s-]?days?|man[\s-]?days?|work[\s-]?days?|muster", re.IGNORECASE)),
    ("job-cards", re.compile(r"job\s*cards?", re.IGNORECASE)),
    ("100-days-completion", re.compile(r"100[\s-]?days?|hundred[\s-]?days?", re.IGNORECASE)),
    ("expenditure", re.compile(r"expenditure|wages?|spend|spent|material cost|\bcost\b", re.IGNORECASE)),
    ("houses-sanctioned", re.compile(r"sanction", re.IGNORECASE)),
    ("houses-completed", re.compile(r"complet", re.IGNORECASE)),
    ("fund-utilisation", re.compile(r"utili[sz]ation|fund releas|amount released", re.IGNORECASE)),
    ("disbursement", re.compile(r"disburs", re.IGNORECASE)),
    ("applications", re.compile(r"applications?", re.IGNORECASE)),
    ("beneficiaries", re.compile(r"beneficiar", re.IGNORECASE)),
]


def detect_metric(text: str) -> "str | None":
    for label, rx in _METRIC_KEYWORDS:
        if rx.search(text or ""):
            return label
    return None


_COMPARE_CUE_RX = re.compile(r"\b(compare|comparison|versus|\bvs\.?\b|between)\b", re.IGNORECASE)


def detect_comparison_districts(text: str) -> list[str]:
    """Best-effort: two-or-more district names in a question that reads like
    a comparison ("compare X and Y", "X versus Y", "between X and Y"). Feeds
    comparison_entities so a later "the former"/"the latter"/"the other one"
    has something concrete to resolve against. Deliberately conservative —
    only fires with an explicit comparison cue, so an ordinary "in X and Y
    both" state-wide question doesn't get mistaken for a two-way comparison."""
    if not text or not _COMPARE_CUE_RX.search(text):
        return []
    try:
        from app.entity_resolver import all_districts
        names = all_districts("MGNREGA") or all_districts("PMAY-G")
    except Exception:  # noqa: BLE001
        return []
    tl = text.lower()
    found = [d for d in names if d.lower() in tl]
    return found[:4]


def _record_provenance(session, raw_question: str, resolved: dict) -> None:
    """Record where each committed value came from (context_policy.provenance_for).
    Reads the turn's facts from session.turn_context, which the pipeline sets:
    the question after reference substitution, the pause reply when this turn
    resumed one, and the previous question. A field whose value did not change
    keeps its record, with source previous_user unless it was model_inference."""
    from app import context_policy
    state = session.state
    ctx = getattr(session, "turn_context", None) or {}
    before = ctx.get("state_before") or {}
    values = {"scheme": state.scheme, "district": state.district, "block": state.block,
              "village": state.village, "year": state.year, "metric": state.metric,
              "tranche": state.tranche}
    for fld, value in values.items():
        if value is None:
            state.provenance.pop(fld, None)
            continue
        key = _STATE_FIELD_KEYS.get(fld)
        rec = context_policy.provenance_for(
            fld, value, raw_question=raw_question,
            substituted_question=ctx.get("substituted_question") or raw_question,
            prior_value=before.get(fld), prior_record=state.provenance.get(fld),
            clarification_reply=ctx.get("clarification_reply"),
            previous_question=ctx.get("previous_question") or "",
            validated=bool(key and key in resolved))
        rec["turn"] = state.turn_count
        state.provenance[fld] = rec


# ── Context Updater ──────────────────────────────────────────────────────────
def update_state(session: "Session | None", raw_question: str, standalone_question: str,
                 result: dict) -> None:
    """Fold the finished turn back into session.state. Never raises."""
    if session is None or not settings.CONTEXT_STATE_ENABLED:
        return
    try:
        state = session.state
        route = result.get("route")
        state.last_question = raw_question
        state.last_standalone_question = standalone_question
        state.last_intent = result.get("intent")
        state.last_route = route
        state.turn_count += 1

        if route != "data":
            # A KNOWLEDGE / EDGE / denied turn is a digression, not a topic
            # reset: leave scheme/district/block/village/year/metric exactly
            # as they were so a later "and in 2023-24?" still resolves
            # against the last DATA context (context-bleed-prevention: this
            # is the "must NOT inherit INTO the digression" rule working in
            # the other direction — the digression must not overwrite what
            # came before it either).
            return

        if not (result.get("sql") or result.get("schemes") or result.get("resolved_entities")):
            # A DATA turn that resolved nothing and ran nothing: the "couldn't build
            # a working query" fallback (pipeline._data_path_kb_fallback) returns
            # exactly this shape. Its question was never validated against the
            # data, so it must not overwrite the last known-good state. It
            # previously replaced `metric`, and a failed "compare X and Y" replaced
            # comparison_entities, so a later "the former" resolved against a
            # comparison that never ran.
            return

        schemes = result.get("schemes") or []
        resolved = result.get("resolved_entities") or {}
        # The committed scheme changed: a filter this result does not name was the
        # OLD scheme's and was not carried (a substitution that carries one
        # resolves it, so it is named). Live 2026-09-29: a CM Elevate Legacy FY
        # 2024-25 turn, then a Focus Plus answer, would otherwise leave year=2024
        # under Focus Plus.
        _scheme_changed = bool(len(schemes) == 1 and state.scheme and schemes[0] != state.scheme)
        if len(schemes) == 1:
            state.scheme = schemes[0]
        elif len(schemes) >= 2:
            state.scheme = None
            state.comparison_entities = list(schemes)
            state.comparison_kind = "scheme"

        # Fields this turn no longer holds (KI-032, live 2026-09-26: after
        # "what about South Garo Hills?" the committed state still said
        # block=DALU, and every later follow-up inherited it). A follow-up's
        # merge plan says which fields it REPLACEd or CLEARed; one the result
        # does not name again is dropped. A turn with no plan was a question of
        # its own — it inherited nothing (prior_resolved is follow-up only), so
        # its resolved entities ARE its whole scope, and older places and years
        # are dropped with it.
        _plan = ((getattr(session, "turn_context", None) or {}).get("plan") or {})
        _actions = _plan.get("actions") or {}
        _held = {"district": resolved.get("district"), "block": resolved.get("block"),
                 "village": resolved.get("village") or resolved.get("village_code"),
                 "year": resolved.get("year_key")}
        for _fld, _val in _held.items():
            if _val is not None:
                continue
            if not _plan or _scheme_changed or _actions.get(_fld) in ("CLEAR", "REPLACE"):
                setattr(state, _fld, None)
                if _fld == "year":
                    state.previous_year = None
        # The dimension this follow-up changed, for a later "all of them".
        _changed = [f for f in ("year", "district", "block") if _actions.get(f) == "REPLACE"]
        state.last_dimension = _changed[0] if (_plan and len(_changed) == 1) else None
        # "All financial years" as a resolved time scope (KI-030).
        if resolved.get("year_key") is not None:
            state.year_all = False
        else:
            try:
                from app import pipeline as _pipeline
                _all_years = bool(_pipeline._ALL_YEARS_CUE.search(standalone_question or raw_question or ""))
            except Exception:  # noqa: BLE001
                _all_years = False
            if _all_years:
                state.year_all = True
            elif not _plan or _scheme_changed or _actions.get("year") in ("CLEAR", "REPLACE"):
                state.year_all = False

        if resolved.get("district"):
            state.district = str(resolved["district"])
        if resolved.get("block"):
            state.block = str(resolved["block"])
        if resolved.get("village_code") or resolved.get("village"):
            state.village = str(resolved.get("village") or resolved.get("village_code"))
        if resolved.get("year_key") is not None:
            try:
                y = int(resolved["year_key"])
                state.year = y
                state.previous_year = y - 1
            except (TypeError, ValueError):
                pass

        # Focus Plus tranche — mirrors district/block/village/year above, plus
        # an explicit "all combined" flag (see state_to_resolved_entities):
        # resolve_tranche_label only ever returns something for a SPECIFIC
        # named tranche (see app.entity_resolver), so a Focus Plus-only turn
        # that reached here with no tranche_label at all only did so because
        # _needs_tranche_clarification's gate was already satisfied by an
        # explicit "all tranches combined" / breakdown cue (or a prior
        # all-combined choice) — never by silent default. Recording that
        # keeps a later bare follow-up ("top 3 only") from losing the choice
        # or getting re-asked.
        if schemes == ["Focus Plus"]:
            tl = resolved.get("tranche_label")
            if tl:
                state.tranche = tl if isinstance(tl, str) else (tl[0] if len(tl) == 1 else None)
                state.tranche_all_combined = False
            else:
                state.tranche = None
                state.tranche_all_combined = True

        metric = detect_metric(standalone_question or raw_question)
        if metric:
            state.metric = metric

        _record_provenance(session, raw_question, resolved)

        districts = detect_comparison_districts(standalone_question or raw_question)
        if len(districts) >= 2:
            state.comparison_entities = districts
            state.comparison_kind = "district"
    except Exception:  # noqa: BLE001 — the context layer must never break an answer
        logger.warning("context_manager.update_state failed (non-fatal)", exc_info=True)


# ── Token-aware context window for the follow-up rewrite prompt ────────────
def _approx_tokens(text: str) -> int:
    """~4 chars/token — good enough for a soft budget; no tokenizer dependency."""
    return max(1, len(text or "") // 4)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    limit_chars = max(0, max_tokens * 4)
    return text if len(text) <= limit_chars else text[:limit_chars].rstrip() + "…"


# Section headers of the follow-up context block (build_followup_context).
# split_followup_context parses the block by these same constants, so the
# provenance check in pipeline.rewrite_followup can tell the structured-state
# line apart from the LLM summary and the older turns.
STATE_HEADER = "Known context:"
HISTORY_HEADER = "Relevant earlier turns:"
SUMMARY_HEADER = "Conversation summary so far:"


# ── Follow-up rewrite evidence (tiers 1-3) ──────────────────────────────────
# The follow-up rewrite used to put the first 300 characters of the previous
# ANSWER into its prompt on every follow-up, and told the model it could take
# districts, years and schemes from it. That slice is the wrong unit in both
# directions:
#   * too much: a list answer's opening names several districts or blocks the
#     follow-up never mentioned, and the model is free to copy one into the
#     new question (measured 2026-09-26: a long answer's slice carried Dalu,
#     Rongram, Tura and Phulbari into "How many beneficiaries were there?");
#   * too little: a character cut can end mid-name, and anything past character
#     300 is invisible even when the follow-up points at it ("the last one").
# What the rewrite needs is the previous turn's SCOPE, and that is already
# structured: the schemes and the resolved entities the SQL was filtered on.
# So the evidence is tiered:
#   Tier 1  PREVIOUS filters — schemes + resolved entities. Always sent.
#   Tier 2  PREVIOUS result — a deterministic summary of the result ROWS, in
#           result order. Sent only when the follow-up points into the result.
#   Tier 3  a sentence-bounded excerpt of the previous answer. Only when the
#           follow-up points into the result and there are no rows (a
#           knowledge answer).
# pipeline.rewrite_followup then checks the rewrite against exactly these
# sources (_rewrite_provenance_violation), so every inherited name has one.

_RESULT_REF_RX = re.compile(
    r"\b(?:the\s+)?(?:first|second|third|fourth|fifth|last|top|bottom|highest|lowest|"
    r"largest|smallest|biggest)\s+(?:one|ones|two|three|five|ten|\d+|district|districts|"
    r"block|blocks|village|villages|scheme|schemes|group|groups|pgs?|entry|entries|item|"
    r"items|row|rows|component|components|tranche|tranches)\b"
    # "this scheme" is deliberately NOT here — it names the scheme in play,
    # not an item of the previous result.
    r"|\b(?:that|those|these|this)\s+(?:one|ones|district|districts|block|blocks|village|"
    r"villages|group|groups|pgs?|component|components|tranche|tranches)\b"
    r"|\b(?:of|among|each\s+of|all\s+of|any\s+of)\s+(?:them|those|these)\b"
    # "How much was it?" / "what were they?": a pronoun standing for the
    # previous result's subject (Scenario D, 2026-09-26). Only as the question's
    # last words, so "show it by district" is not a result reference.
    r"|\b(?:was|is|were|are)\s+(?:it|that|they|those)\s*[?.!]*\s*$",
    re.IGNORECASE,
)
_HISTORY_REF_RX = re.compile(
    r"\b(?:earlier|previously|at\s+the\s+start|in\s+the\s+beginning|"
    r"we\s+(?:discussed|talked\s+about|looked\s+at)|you\s+(?:said|mentioned|showed)|"
    r"(?:first|original|initial)\s+question)\b",
    re.IGNORECASE,
)


def references_previous_result(question: str) -> bool:
    """True when the follow-up points at an ITEM of the previous result ("the
    top one", "that block", "the second one", "each of them"), which only the
    result itself can resolve. A new ranking question ("which one had the
    highest?") or a re-cut ("show it by district") does not need the result's
    content, and so does not get it."""
    return bool(_RESULT_REF_RX.search(question or ""))


def references_history(question: str) -> bool:
    """True when the follow-up explicitly reaches further back than the last
    turn ("the district we discussed earlier"). Only then are names from the
    conversation summary or older turns an acceptable source for the rewrite."""
    return bool(_HISTORY_REF_RX.search(question or ""))


_NUMERIC_STR_RX = re.compile(r"^-?\d+(?:\.\d+)?$")
# Columns that identify a row rather than measure it, even when numeric
# (year_key 2023, village_code 277769).
_LABEL_LIKE_COL_RX = re.compile(r"(?:_key|_code|_id|year|_name|label)$|^(?:year|fy)",
                                re.IGNORECASE)


def _is_number(v) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float, Decimal)):
        return True
    return isinstance(v, str) and bool(_NUMERIC_STR_RX.match(v.strip()))


def _cell(v) -> str:
    s = str(v)
    return s if len(s) <= 60 else s[:57] + "..."


def _join_within(head: str, items: list[str], limit_chars: int, total: int) -> str:
    """head + as many whole items as fit in limit_chars, then '... +N more'.
    Items are never cut part-way."""
    out, used = head, 0
    for it in items:
        piece = ("; " if used else "") + it
        if len(out) + len(piece) > limit_chars:
            break
        out += piece
        used += 1
    if total - used > 0:
        out += ("; " if used else "") + f"... +{total - used} more"
    return out


def summarize_result(result: "dict | None", max_tokens: "int | None" = None) -> str:
    """Tier 2: a deterministic, token-budgeted summary of a DATA turn's result
    ROWS, in the order the query returned them. Built from the rows, not the
    composed prose, so it holds exactly the values that were queried. Empty for
    anything that isn't a DATA result with rows. Never raises.

        1 row: amount_raw=1234567; beneficiaries=88
        5 rows by block_name_raw, in result order: Dalu (20000); Rongram (19000); ... +3 more
    """
    try:
        if not result or result.get("route") != "data":
            return ""
        rows = result.get("rows") or result.get("data") or []
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            return ""
        budget = settings.CONTEXT_PREV_RESULT_MAX_TOKENS if max_tokens is None else max_tokens
        limit = max(0, budget * 4)
        total = int(result.get("row_count") or 0) or len(rows)
        first = rows[0]
        if total == 1:
            items = [f"{k}={_cell(v)}" for k, v in first.items()]
            return _join_within("1 row: ", items, limit, len(items))
        cols = list(first.keys())
        label = next((c for c in cols if isinstance(first[c], str) and not _is_number(first[c])),
                     None) or next((c for c in cols if _LABEL_LIKE_COL_RX.search(c)), None)
        if label is None:
            return f"{total} rows; columns: {', '.join(cols)}"
        value = next((c for c in cols if c != label and _is_number(first.get(c))
                      and not _LABEL_LIKE_COL_RX.search(c)), None)
        items = [f"{_cell(r.get(label))} ({_cell(r.get(value))})" if value else _cell(r.get(label))
                 for r in rows]
        return _join_within(f"{total} rows by {label}, in result order: ", items, limit, total)
    except Exception:  # noqa: BLE001 — evidence is optional; a bad row shape just means none
        logger.warning("context_manager.summarize_result failed (non-fatal)", exc_info=True)
        return ""


_SENTENCE_SPLIT_RX = re.compile(r"(?<=[.!?])\s+|\n+")


def answer_excerpt(answer: str, max_tokens: "int | None" = None) -> str:
    """Tier 3: the previous answer's opening, cut at a sentence or line
    boundary within the token budget. It is never cut mid-sentence or
    mid-name: when even the first sentence is over budget, it returns ''
    rather than a fragment."""
    budget = settings.CONTEXT_PREV_ANSWER_MAX_TOKENS if max_tokens is None else max_tokens
    limit = max(0, budget * 4)
    out: list[str] = []
    used = 0
    for s in (p.strip() for p in _SENTENCE_SPLIT_RX.split(answer or "")):
        if not s:
            continue
        add = len(s) + (1 if out else 0)
        if used + add > limit:
            break
        out.append(s)
        used += add
    return " ".join(out)


# resolved_entities key -> the label shown in the PREVIOUS filters line.
# village_code is left out on purpose: a bare surrogate integer tells the
# rewrite nothing (the village NAME is already in the previous question), and
# resolve_entities' prior_resolved fallback carries the code itself.
_FILTER_LABELS = (
    ("district", "district"), ("district_list", "districts"), ("block", "block"),
    ("block_list", "blocks"), ("assembly_constituency", "constituency"),
    ("year_key", "year"), ("tranche_label", "tranche"), ("cm_scheme", "sub-scheme"),
    ("house_status", "house status"),
)


def previous_filters_line(prev: object) -> str:
    """Tier 1: the previous turn's scope as one structured line, e.g.
    'PREVIOUS filters: scheme=Focus Plus; district=WEST GARO HILLS; year=FY 2024-25'.
    Empty when the turn carried no schemes or entities (an older Turn, or a
    knowledge turn)."""
    parts: list[str] = []
    schemes = list(getattr(prev, "schemes", None) or [])
    if schemes:
        parts.append("scheme=" + " + ".join(schemes))
    resolved = getattr(prev, "resolved_entities", None) or {}
    for key, label in _FILTER_LABELS:
        v = resolved.get(key)
        if v is None or v == "" or v == []:
            continue
        vals = v if isinstance(v, (list, tuple)) else [v]
        if key == "year_key":
            try:
                vals = [_fy_text(int(x)) for x in vals]
            except (TypeError, ValueError):
                continue
        parts.append(f"{label}=" + ", ".join(str(x) for x in vals))
    return "PREVIOUS filters: " + "; ".join(parts) if parts else ""


def build_rewrite_evidence(question: str, prev: object, kind: "str | None" = None) -> dict:
    """What the follow-up rewrite gets to see about the previous turn, by tier
    (see the block comment above). Returns {"filters", "result", "answer",
    "tiers"}: each text is empty when its tier doesn't apply, and "tiers" names
    the tiers actually used (for the prompt_context log). Never raises: on
    any error it returns no evidence, and the rewrite still has the previous
    question."""
    empty = {"filters": "", "result": "", "answer": "", "tiers": []}
    try:
        # The follow-up's kind (context_policy.plan_state_merge) picks the
        # layers. Without a kind (older callers), the result layer is gated by
        # the result-reference cue, as before.
        from app import context_policy
        layers = (context_policy.context_layers(kind) if kind
                  else ("filters", "result") if references_previous_result(question)
                  else ("filters",))
        filters = previous_filters_line(prev) if "filters" in layers else ""
        out = {"filters": filters, "result": "", "answer": "",
               "tiers": ["filters"] if filters else []}
        if "result" in layers:
            summary = getattr(prev, "result_summary", "") or ""
            if summary:
                out["result"] = f"PREVIOUS result: {summary}"
                out["tiers"].append("result")
            else:
                excerpt = answer_excerpt(getattr(prev, "answer", "") or "")
                if excerpt:
                    out["answer"] = f'PREVIOUS answer (excerpt): "{excerpt}"'
                    out["tiers"].append("answer_excerpt")
        return out
    except Exception:  # noqa: BLE001
        logger.warning("context_manager.build_rewrite_evidence failed (non-fatal)", exc_info=True)
        return empty


def split_followup_context(extra_context: str) -> dict:
    """Split build_followup_context's block back into {"state", "history",
    "summary"}. The provenance check treats these differently: the
    structured-state line is always a valid source, while the LLM summary and
    older turns are a valid source only when the follow-up reaches back to them
    (references_history). Unrecognised lines count as history, the stricter
    side."""
    out = {"state": "", "history": "", "summary": ""}
    history: list[str] = []
    for line in (extra_context or "").splitlines():
        if line.startswith(STATE_HEADER):
            out["state"] = line
        elif line.startswith(SUMMARY_HEADER):
            out["summary"] = line
        elif line.strip():
            history.append(line)
    out["history"] = "\n".join(history)
    return out


def build_state_block(state: "ConversationState | None") -> str:
    """One compact line of KNOWN CONTEXT — the "current structured state"
    layer of the context window. Deterministic, no model call."""
    if not state:
        return ""
    parts = []
    if state.scheme:
        parts.append(f"scheme={state.scheme}")
    if state.comparison_entities:
        parts.append(f"comparing={'/'.join(state.comparison_entities)} ({state.comparison_kind})")
    if state.district:
        parts.append(f"district={state.district}")
    if state.block:
        parts.append(f"block={state.block}")
    if state.village:
        parts.append(f"village={state.village}")
    if state.year is not None:
        parts.append(f"year={_fy_text(state.year)}")
    elif getattr(state, "year_all", False):
        parts.append("year=all financial years")
    if state.metric:
        parts.append(f"metric={state.metric}")
    if state.tranche:
        parts.append(f"tranche={state.tranche}")
    elif state.tranche_all_combined:
        parts.append("tranche=all combined")
    return STATE_HEADER + " " + ", ".join(parts) if parts else ""


async def build_followup_context(session: "Session | None", question: str,
                                 state: "ConversationState | None" = None) -> str:
    """The enrichment block passed to rewrite_followup() as `extra_context` —
    structured state + conversation summary + (only for a long/resumed
    conversation, where the in-process turn window is thin) semantically
    relevant older turns pulled from Qdrant. Token-budgeted per
    CONTEXT_MAX_TOKENS; each section gets its own sub-budget so one long
    section can't crowd out the others entirely.

    Priority order (highest first, per the spec): current question (not
    built here — the caller appends it separately), system instructions
    (also the caller's), structured state, recent turns (already in
    rewrite_followup's own prompt as PREVIOUS question / filters / result —
    see build_rewrite_evidence), relevant
    historical turns, summary. Truncation drops the LOWEST-priority section
    first when the budget is tight.
    """
    if session is None or not settings.CONTEXT_LAYER_ENABLED:
        return ""
    budget = settings.CONTEXT_MAX_TOKENS
    sections: list[str] = []

    try:
        # `state`: the thread this follow-up continues, when the caller decided
        # it is not the session's (pipeline._followup_thread_state).
        state_block = build_state_block(state if state is not None else session.state)
    except Exception:  # noqa: BLE001
        state_block = ""
    if state_block:
        sections.append(state_block)
        budget -= _approx_tokens(state_block)

    # Relevant historical turns — only reached for once the in-process window
    # is thin (a resumed/long conversation), to keep the common case free of
    # an extra embed + Qdrant round trip.
    if (settings.CONTEXT_SEMANTIC_MEMORY_ENABLED and budget > 0
            and len(session.turns) < settings.CONTEXT_MEMORY_MIN_SESSION_TURNS):
        try:
            from app import conversation_memory
            scope = getattr(session, "scope", None)
            hits = await conversation_memory.search_relevant_turns(
                question=question,
                tenant_id=getattr(scope, "tenant_id", None),
                user_id=getattr(scope, "db_user_id", None),
                session_id=session.session_id,
                top_k=settings.CONTEXT_HISTORICAL_TURNS_MAX,
            )
        except Exception:  # noqa: BLE001 — memory retrieval failing must never break the turn
            logger.warning("context_manager: semantic memory lookup failed, continuing without it",
                           exc_info=True)
            hits = []
        if hits:
            lines = [f'- Q: "{h["standalone_question"] or h["question"]}" A: "{h["answer"][:160]}"'
                     for h in hits]
            block = HISTORY_HEADER + "\n" + "\n".join(lines)
            block = _truncate_to_tokens(block, max(0, min(budget, 400)))
            if block:
                sections.append(block)
                budget -= _approx_tokens(block)

    if settings.CONTEXT_SUMMARY_ENABLED and session.summary and budget > 0:
        block = _truncate_to_tokens(
            f"{SUMMARY_HEADER} {session.summary}",
            min(budget, settings.CONTEXT_SUMMARY_MAX_TOKENS),
        )
        sections.append(block)

    return "\n".join(sections)


# ── Conversation summary (periodic, best-effort) ────────────────────────────
def should_update_summary(session: "Session") -> bool:
    if not settings.CONTEXT_SUMMARY_ENABLED:
        return False
    n = session.state.turn_count
    return n > 0 and (n - session.summary_turn_count) >= settings.CONTEXT_SUMMARY_EVERY_N_TURNS


async def maybe_update_summary(session: "Session | None") -> None:
    """Refresh session.summary from the last few standalone questions, every
    CONTEXT_SUMMARY_EVERY_N_TURNS turns. Never raises: a failed summary just
    means the conversation keeps running without an updated one (requirement
    9) — the caller doesn't need to check the return value."""
    if session is None or not should_update_summary(session):
        return
    try:
        from app import llm

        recent = [t.question for t in session.turns[-settings.CONTEXT_RECENT_TURNS:] if t.question]
        if not recent:
            return
        state_block = build_state_block(session.state)
        prompt = (
            "Summarise this conversation in ONE short paragraph (max 60 words): which "
            "scheme(s), locations, financial years, metrics and comparisons have come up, "
            "and any question left unresolved. Plain prose, no headings.\n\n"
            + (f"{state_block}\n" if state_block else "")
            + "Questions so far:\n"
            + "\n".join(f"- {q}" for q in recent)
            + "\n\nSummary:"
        )
        out = await llm.call_classifier(prompt)
        out = out.strip().strip('"')
        if out and len(out) <= 800:
            session.summary = out
            session.summary_turn_count = session.state.turn_count
            logger.info("context_manager: summary updated (turn_count=%d)", session.state.turn_count)
            # Not persisted here. The router's awaited session_sync.sync_out
            # carries session.summary in the same versioned write as the rest of
            # the turn. A separate fire-and-forget write from here could finish
            # AFTER that write and replace the newer snapshot with this older
            # state (2026-09-26, KI-028).
    except Exception:  # noqa: BLE001 — requirement 9: continue without the new summary
        logger.warning("context_manager: summary generation failed (non-fatal)", exc_info=True)
