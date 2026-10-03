"""
The whole NL -> SQL -> answer flow. Each step is an ordinary `await`, branches
are ordinary `if`, grouped into stage functions (`_turn_*_stage`,
`_data_*_stage`, `_execute_one_attempt`, ...). By default `_run_pipeline` runs
them in order; with PIPELINE_GRAPH_ENABLED the LangGraph orchestrator in
app/pipeline_graph.py runs the same stages as graph nodes (D-032). Behaviour
lives here, in the stages, never in a graph node.
"""
import asyncio
import calendar
import datetime
import contextvars
import itertools
import json
import functools
import logging
import numbers
import re
from decimal import Decimal

import httpx
from rapidfuzz import fuzz

from app import annotations, auth, context_manager, edge, followups, llm, premise_check, prompt_builder, rag
from app import context_budget, context_policy
from app.config import settings
from app.db import UnsafeSQLError, fetch_rows, run_readonly
from app.db import DatabaseUnavailableError, is_connection_error as db_is_connection_error
from app.entity_resolver import (
    Resolved,
    acronym_near_misses,
    all_districts,
    block_parent_district,
    canonical_names,
    collides_across_dimensions,
    collision_canonical_names,
    AC_CONTENTS_SOURCE,
    constituency_contents,
    detect_region,
    lookup_geo_term,
    named_places,
    resolve_cm_scheme,
    resolve_cm_scheme_group,
    resolve_cm_scheme_group_ambiguity,
    resolve_dimension,
    resolve_house_status,
    resolve_tranche_label,
    resolve_village,
    scan_dimension,
    tranche_labels,
    village_names_exact,
)
from app.schema_context import SCHEME_CATALOG, available_metrics_text
from app.session_store import ConversationState, Session

logger = logging.getLogger(__name__)

# Words that make a question unambiguously a data (numbers) question — no model
# call needed to route it. Anything else with no strong signal is sent to the
# classifier, which decides DATA vs KNOWLEDGE.
_DATA_HINTS = re.compile(
    r"\b(how many|count|total|sum|average|avg|number of|top \d|per capita|"
    r"compare|comparison|trend|by district|by block|by village|by year|"
    r"person[\s-]?days?|expenditure|spend|spent|wage|wages|job cards?|"
    r"houses? (sanction|complet|released|pending)|sanctioned amount|amount released|"
    r"completion rate|utili[sz]ation rate|success rate|per ?cent|percentage|how much|"
    # Focus Plus / CM Elevate metric nouns — a bare "<scheme> beneficiaries?" /
    # "disbursements?" with no "how many" in front of it used to have no strong
    # signal either way and fell through to the LLM classifier, which guessed
    # KNOWLEDGE for "focus + beneficiaries?" and returned the glossary answer
    # instead of a count (confirmed live 2026-09-11). These are count/amount
    # nouns, not process words, so adding them carries the same low
    # misroute risk as "person-days"/"expenditure" above.
    r"beneficiar\w*|disburs\w*|"
    # EXISTENCE / NAME-LOOKUP questions. "Is there any Producer Group named as
    # Sakania PG?" is a lookup against stored records (the SME use-case bank
    # lists it as answer_route: sql, TC-13) but carries NO counting word, so it
    # matched no DATA cue above and fell through to the LLM classifier, which
    # called it KNOWLEDGE and answered from the reference docs — "no mention of
    # a specific Producer Group named ... in the provided reference material",
    # true of the prose and irrelevant to the question (reported 2026-09-22).
    # The naming word is what keeps these narrow: "is there any eligibility
    # criteria" names nothing and is still KNOWLEDGE.
    r"\b(?:is|are)\s+there\s+(?:any|a|an)\b[^?]{0,60}?"
    r"\b(?:named|called|by the name of|with (?:the )?name)\b|"
    r"\bproducer[\s-]?groups?\s+(?:named|called)\b|"
    # Same lookup shape using the "PG" abbreviation, which is what users
    # actually type: "is there any pg group with name sakania?", "pg named X".
    # The optional "group" covers the redundant-but-common "pg group".
    r"\bpgs?(?:\s+group)?\s+(?:named|called|with (?:the )?name)\b|"
    r"\b(?:find|search for|look up|lookup)\s+(?:the\s+|a\s+)?producer[\s-]?group\b|"
    # "How did people apply to CM Elevate?" asks for the recorded
    # application_mode breakdown (online vs cmconnectcenter — a real, answerable
    # count), not the application PROCESS ("how do I apply", "how to apply",
    # already a _KNOWLEDGE_HINTS cue). Third-person/past-tense phrasing is the
    # reliable signal that separates the two; the LLM classifier alone picked
    # KNOWLEDGE here and gave a plausible-sounding but unverifiable portal
    # description instead of the real online/cmconnectcenter split (confirmed
    # live 2026-09-11, this exact CM Elevate few-shot question).
    r"how (?:did|do|does) (?:people|applicants|users|they|most people) apply\b|"
    r"\bfy ?20\d\d|20\d\d-\d\d|crore|lakh|highest|lowest|most|least|"
    # correlation / cross-metric comparison phrasing — "do districts with high X
    # also have high Y", "is A related to B by district". These are answered by
    # querying and comparing the figures, not from the reference docs.
    r"relationship between|correlat\w*|associat\w*|linked to|linked with|"
    r"go together|hand in hand|track (?:each other|together)|move together|"
    r"districts? where|blocks? where|villages? where|"
    r"do (?:the )?districts? with|does (?:the )?district with|"
    r"also (?:high|low|higher|lower|lead|leads|greater|larger|smaller|more|less)|"
    r"versus)\b",
    re.IGNORECASE,
)
_KNOWLEDGE_HINTS = re.compile(
    r"\b(what is|what are|who is eligible|eligibility|how do i|how to apply|"
    r"documents? required|what documents|explain|define|meaning of|"
    r"components? of|features? of|objective|purpose of|when was .* launched|"
    r"difference between|guidelines?|rules? for)\b",
    re.IGNORECASE,
)

# "What is the gender split of CM Elevate applicants?" / "what's the workflow
# level breakdown?" both open with the strong _KNOWLEDGE_HINTS cue "what is",
# which used to short-circuit straight to KNOWLEDGE before classify_intent's
# LLM call ever ran. Adding "split"/"breakdown"/"distribution" into _DATA_HINTS
# only removed that false shortcut — it still left the LLM to arbitrate, and it
# guessed KNOWLEDGE for "workflow level breakdown" too (confirmed live
# 2026-09-11: CM Elevate's own headline finding, "female-majority", and its
# online/cmconnectcenter split were both unreachable this way). A "breakdown /
# split / distribution" noun names a computed grouping over real records by
# construction — there is no scheme-mechanics reading of it — so it can force
# DATA outright, the same way _CROSS_SCHEME_SET_QUESTION does below.
_BREAKDOWN_CUE = re.compile(r"\b(split|breakdown|distribution)\b", re.IGNORECASE)

# Same shape of bug as _BREAKDOWN_CUE above, different trigger word: "What is
# the average amount disbursed per beneficiary?" / "What is the total
# disbursed in West Garo Hills?" open with "what is" (a _KNOWLEDGE_HINTS cue)
# AND also match "average"/"total" (a _DATA_HINTS cue), so neither fast-path
# branch fires and it falls to the LLM classifier — which guessed KNOWLEDGE
# for a plain average-per-beneficiary question live (confirmed 2026-09-12: a
# Focus Plus "average disbursed per beneficiary" question got answered from
# the reference docs with a flat ₹5,000 rate instead of the real computed
# average). "What is the average/total/sum/count/number of X" is a computed
# aggregate by construction, never scheme-mechanics — force DATA the same way
# _BREAKDOWN_CUE does.
_METRIC_WHATIS_CUE = re.compile(
    r"\bwhat (?:is|was|are|were)\b.{0,40}\b(average|avg|mean|total|sum|number|count)\b",
    re.IGNORECASE,
)

# The opposite collision: programme-design questions that happen to contain a
# DATA cue. "Who are the intended beneficiaries of CM-ELEVATE?" trips
# "beneficiar\w*", and "What is the target number of entrepreneurs under
# CM-ELEVATE?" trips _METRIC_WHATIS_CUE ("what is … number"), so both went down
# the data path — one answered "the data doesn't cover the intended
# beneficiaries", the other asked "which area / year?" for a policy target and
# never stated it (CM Elevate Legacy use-case QA, TC-03 / TC-08, 2026-09-25).
# Who a scheme is FOR and what it AIMS at are design facts in the reference
# docs, never a computed figure — the qualifiers below are what keep this
# narrow ("how many beneficiaries" and "beneficiaries by district" stay DATA).
# KI-076 (CM Elevate reworded probe, 2026-09-27): "Applicants under Goat Farming
# and Warehouse in West Khasi Hills?" has no counting word and no knowledge word,
# so it fell to the LLM classifier, which read it as an eligibility question in 2
# of 3 runs and answered from the reference docs. An applicant/application noun
# scoped to a PLACE is a count over stored records: a capitalised place after
# in/at/from, or any "<name> district/block/village/hills". A knowledge word
# ("eligibility", "documents", "what is") still wins, as with _DATA_HINTS.
_APPLICANT_PLACE_CUE = re.compile(
    r"(?i:\b(?:applicants?|applications?|registrations?)\b)[^?.]{0,80}?"
    r"(?:\b(?i:in|at|from)\s+(?:the\s+)?[A-Z][A-Za-z-]+|"
    r"(?i:\b(?:in|at|from)\s+(?:the\s+)?[a-z][\w-]*(?:\s+[a-z][\w-]*){0,2}\s+"
    r"(?:district|block|village|hills)\b))")
_PROGRAMME_DESIGN_CUE = re.compile(
    r"\b(?:intended|target(?:ed)?|eligible)\s+(?:beneficiar\w*|groups?|entrepreneurs?)\b|"
    r"\btarget(?:ed)?\s+(?:number|figure|count)\s+of\b|"
    r"\b(?:entrepreneur|employment|job|outreach)[\s-]+(?:reach\s+)?targets?\b",
    re.IGNORECASE,
)


class OutOfScope(Exception):
    """Raised when the question is about a place the assistant doesn't cover —
    a district or block that isn't in Meghalaya. The pipeline turns this into
    the standard 'I'm Megh One AI — I only cover Meghalaya's MGNREGA / PMAY-G'
    reply instead of dropping the filter and reporting a meaningless 0."""


class ClarificationNeeded(Exception):
    """Raised when the pipeline cannot safely proceed without one more detail
    from the user. `question` is the thing to ask; `options` (optional) is a
    list of {"label", "question"} the UI renders as one-click replies — each
    `question` is a fully-formed standalone question that resumes the flow.
    `rule` (optional) is a short tag for why we paused (shown in the UI).
    `village_hint` (optional) is the exact village-name text the user typed,
    set only for a village-ambiguity pause — see Session.pending_village_hint
    for why a free-text resume needs it re-supplied deterministically rather
    than trusting the LLM mention-extractor to re-tag it in the merged text."""

    def __init__(self, question: str, *, options: list[dict] | None = None,
                 rule: str | None = None, village_hint: str | None = None):
        super().__init__(question)
        self.question = question
        self.options = options or []
        self.rule = rule
        self.village_hint = village_hint


# ── Follow-up ("what about East Garo Hills?") rewriting ──────────────────────
# A cheap heuristic gates the one extra model call: only questions that read
# like a fragment referring back to the previous turn get rewritten.
_FOLLOWUP_LEAD = re.compile(
    r"^\s*(what about|how about|and |what of |and for |also |ok(ay)? and |"
    r"same for |what if |now |then )", re.IGNORECASE,
)
_FOLLOWUP_PRONOUN = re.compile(
    r"\b(it|its|that|those|these|them|they|there|this(?:\s+one)?|the same|same one)\b", re.IGNORECASE,
)
# Something that anchors the question on its own — if present, it's not a fragment.
# Deliberately excludes generic topic words like "documents?", "eligibility?",
# "what is", "who is", "explain": on their own (esp. as a short bare fragment)
# these don't name a scheme or a specific metric, so right after a scheme
# answer "Documents?" / "Eligibility?" reads as a follow-up about THAT scheme,
# not a fresh self-contained question — they used to trip this anchor and get
# judged standalone, which silently dropped the active scheme context.
# "scheme" IS kept as an anchor (unlike those): a fresh, contextless question
# like "Is there any government scheme that can help my family?" uses "that"
# as an ordinary relative pronoun, not a reference to a prior turn, but
# _CONTEXTLESS_REF can't tell the difference — without "scheme" anchoring it,
# this read as a fragment needing prior context and got the "I don't have an
# earlier answer to build on" reply instead of being answered directly (TC-008).
_STANDALONE_ANCHOR = re.compile(
    r"\b(mgnrega|mnrega|nrega|pmay|awaas|person[\s-]?days?|expenditure|houses?|"
    r"job cards?|wages?|sanction|how many|total|list|scheme)\b", re.IGNORECASE,
)
# A bare scheme-name fragment — "in MGNREGA?", "for PMAY-G", "and Focus Plus?",
# "what about MGNREGA" — is the canonical scheme-swap follow-up: it reuses the
# whole previous question and only changes the scheme. It trips _STANDALONE_ANCHOR
# (the scheme names are listed there), so without this it is mistaken for a
# self-contained question and the router reads it as "tell me about MGNREGA".
#
# Reported 2026-09-29: "give me for pmay" after "Total MGNREGA person-days in
# 2023-24" was answered with a PMAY-G scheme description (RAG). The request
# verb ahead of the preposition ("give me", "show us the same", "can you get
# me") was not an allowed opening, so the message read as a self-contained
# question naming PMAY-G. The verb carries no scope of its own. "tell me ABOUT
# pmay" still does not match ("about" is not an opening): that one asks what
# the scheme is.
_SCHEME_SWAP_FOLLOWUP = re.compile(
    r"^\s*(?:ok(?:ay)?\s*,?\s*|so\s+)?"
    r"(?:(?:can|could|would|will)\s+you\s+|please\s+|pls\s+|kindly\s+)?"
    r"(?:(?:give|show|get|tell|fetch|do)\s+(?:me|us)?\s*(?:the\s+)?(?:same\s+)?"
    r"(?:(?:thing|data|numbers?|figures?|results?)\s+)?)?"
    r"(?:(?:and|also|now|then)\s+)?"
    r"(?:in|for|under|and|also|now|then|what about|how about|what of|same for|"
    r"switch to|change to|with)?\s*"
    r"(?:mgnrega|mnrega|nrega|pmay[\s-]?g?|awaas?|awas|"
    r"focus[\s-]?plus|focus\s*\+|focusplus|"
    # Focus Legacy, and a BARE "focus" — which names neither Focus scheme, so
    # _scheme_swap_rewrite substitutes the word "Focus" and the normal "which
    # Focus?" pause asks. Without these, "for focus" after a CM Elevate answer
    # went to the LLM rewrite, which kept CM Elevate and re-answered it.
    r"focus[\s-]?legacy|focuslegacy|focus|"
    r"cm[\s-]?elevate(?:[\s-]?legacy)?|cmelevate(?:[\s-]?legacy)?)"
    r"\s*(?:scheme\s+)?(?:instead|now|then|scheme|too|also|as\s+well)?\s*[?.!]*\s*$",
    re.IGNORECASE,
)
# "same for the remaining schemes" / "give same like for other schemes" / "what
# about the rest of the schemes" — the previous question, asked of every scheme
# it did NOT name. "all schemes" asks it of every scheme. Left to the LLM
# rewrite, "remaining schemes" after a CM Elevate answer became "the remaining
# CM Elevate schemes" (its sub-schemes) and the same answer came back again.
_REST_OF_SCHEMES = re.compile(
    r"\b(?:remaining|other|rest\s+of\s+(?:the\s+)?)\s*schemes?\b|"
    r"\ball\s+(?:the\s+)?other\s+schemes?\b|\ball\s+(?:the\s+)?(?:others|remaining)\b",
    re.IGNORECASE,
)
_ALL_SCHEMES_FOLLOWUP = re.compile(r"\ball\s+(?:the\s+)?(?:\w+\s+)?schemes\b", re.IGNORECASE)


# A follow-up fragment ("and for 2024-25?", "how launched it?") only means
# something against a real scheme answer. Rewriting it against a greeting,
# off-topic reply, clarification pause or a denial produces a confident bogus
# query — e.g. "how launched it?" right after "what is elon musk?" was being
# turned into a data question. So the previous turn must be one of these.
_ANTECEDENT_ROUTES = ("data", "knowledge")


_SCOPELESS_MAX_WORDS = 8
_SCHEME_WORD = re.compile(r"\bschemes?\b", re.IGNORECASE)


def is_scopeless_followup(question: str, prev: "object") -> bool:
    """A short question that asks only for a MEASURE ("How many beneficiaries
    were there?", "What was the total?") right after a DATA answer. It names no
    scheme, place or year of its own, so its scope can only be the previous
    turn's.

    looks_like_followup() reads "how many" / "total" as anchors of a standalone
    question, so these were answered as fresh, scope-less questions: a year
    pause, or statewide figures (live run 2026-09-26, Scenario A T3). Kept as a
    separate predicate, gated on a DATA antecedent, instead of loosening
    _STANDALONE_ANCHOR, which also decides caching for every question. The
    router's is_cacheable uses this too, so such a turn is never cached."""
    if prev is None or getattr(prev, "route", None) != "data":
        return False
    return _is_measure_only(question)


def _is_measure_only(question: str) -> bool:
    """A short request for a measure and nothing else: no scheme, place, year
    or how-the-scheme-works wording ("How many beneficiaries were there?",
    "give me beneficiaries", "What was the total?")."""
    q = (question or "").strip()
    if not q or len(q.split()) > _SCOPELESS_MAX_WORDS:
        return False
    if _SCHEME_WORD.search(q) or _mentions_scheme(q) or _KNOWLEDGE_HINTS.search(q):
        return False
    if _BARE_FOCUS_WORD.search(q):
        return False
    if named_places(q) or context_policy.extract_year_keys(q):
        return False
    return bool(context_policy.metric_families(q) or re.search(r"\btotal\b", q, re.IGNORECASE))


def _measure_after_knowledge(question: str, prev: "object") -> bool:
    """A measure-only request right after a KNOWLEDGE answer about one scheme
    ("what is CM Elevate Legacy" -> "give me beneficiaries"). Its only context
    is that scheme — a knowledge turn has no filters — so it needs no model
    rewrite. Live 2026-09-29 the rewrite paraphrased it into "What are the
    beneficiaries of the CM Elevate Legacy scheme?", which then routed to the
    reference docs and listed eligible applicant categories instead of a count."""
    if not (prev is not None and getattr(prev, "route", None) == "knowledge"
            and len(getattr(prev, "schemes", None) or []) == 1 and _is_measure_only(question)):
        return False
    # A measure only another scheme holds picks that scheme's thread instead:
    # "and person-days?" after a PMAY-G eligibility aside is MGNREGA (live
    # 2026-09-29, this shortcut answered "19,058 person-days" from a PMAY-G
    # house count).
    own = set(_infer_scheme_from_terms(question) or [])
    return not own or prev.schemes[0] in own


def _typed_intent(question: str) -> "str | None":
    """DATA / KNOWLEDGE when the user's own words decide it on the keyword fast
    path (the same regexes classify_intent uses), else None. For a follow-up,
    the typed words outrank the model rewrite's phrasing: "give me
    beneficiaries" is a count even when the rewrite reads "What are the
    beneficiaries of…" (a "what are" knowledge cue)."""
    q = question or ""
    if _DATA_HINTS.search(q) and not _KNOWLEDGE_HINTS.search(q):
        return "DATA"
    if _KNOWLEDGE_HINTS.search(q) and not _DATA_HINTS.search(q):
        return "KNOWLEDGE"
    return None
# An explicit pointer back to the previous turn's area inside a name lookup.
_NAME_LOOKUP_BACKREF = re.compile(
    r"\b(?:in|within|from|of|under)\s+(?:that|this|the\s+same)\s+"
    r"(?:block|district|village|area|constituency|place)\b|"
    r"\b(?:in\s+)?there\s*[?.!]*\s*$|\bsame\s+(?:block|district|village|area)\b",
    re.IGNORECASE)
_CONTEXTLESS_REF = re.compile(r"\b(it|its|that|those|these|them|they|this(?:\s+one)?)\b", re.IGNORECASE)


def _mentions_scheme(question: str) -> bool:
    return any(p.search(question or "") for p in _SCHEME_NAME_PATTERN.values())


# ── Scheme substitution ─────────────────────────────────────────────────────
# "give me same for mgnrega" after "how many beneficiaries in Focus Plus ...":
# the SAME operation on another scheme. Reported live 2026-09-26: the message
# names a scheme, so looks_like_followup() declared it a self-contained question
# (the _mentions_scheme check below). _SCHEME_SWAP_FOLLOWUP only knew a fixed set
# of opening words, and "give me" was not one of them. The intent classifier
# then saw the bare words with no context, returned KNOWLEDGE, and the user got
# general MGNREGA information instead of the MGNREGA beneficiary count.
#
# Generic by construction: the schemes come from _SCHEME_NAME_PATTERN (the
# registry every other scheme check uses), and the continuation phrases are
# scheme-agnostic. A new scheme needs nothing here.
_SUBSTITUTION_CUE = re.compile(
    r"\bsame\b|\bdo\s+(?:it|this|that)\b|\brepeat\b|\blikewise\b|\bsimilarly\b|"
    r"\b(?:what|how)\s+about\b|\bwhat\s+of\b|\b(?:now|and|also|this|then)\s+for\b|"
    r"\binstead\b|\b(?:switch|change)\s+to\b|"
    # "give me for pmay please" (2026-09-29): a request verb straight onto the
    # preposition asks for the previous operation. Not "tell me about".
    r"\b(?:give|show|get|tell|fetch)\s+(?:me|us)\s+(?:the\s+)?(?:same\s+)?(?:for|in|under)\b",
    re.IGNORECASE,
)
# "same" is an explicit reference to the previous OPERATION. Only with it may a
# substitution also carry changes of its own ("same for MGNREGA in 2023-24").
# A weaker cue ("what about MGNREGA eligibility?") keeps today's reading.
_STRONG_SUBSTITUTION_CUE = re.compile(
    r"\bsame\b|\bdo\s+(?:it|this|that)\b|\brepeat\b|\blikewise\b|\bsimilarly\b", re.IGNORECASE)
# Words that carry no scope of their own in a substitution request. If nothing
# but these, the cue and one scheme name remain, the message is a PURE
# substitution: every other part of the previous question stays as it was.
_SUBSTITUTION_FILLER = re.compile(
    r"\b(?:give|show|tell|get|run|do|repeat|me|us|the|a|an|same|thing|things|query|question|"
    r"one|it|this|that|please|pls|kindly|can|could|would|you|now|and|also|then|for|in|under|"
    r"of|on|what|how|about|with|instead|likewise|similarly|switch|change|to|scheme|number|"
    r"numbers|figure|figures|result|results|answer|data|info|information|details|too|as|well|"
    r"again|ok|okay)\b|[,.!?;:&+/-]",
    re.IGNORECASE,
)


def _scheme_substitution(question: str) -> "tuple[str | None, bool]":
    """(scheme, pure) when `question` asks for the previous operation on ONE
    named scheme, else (None, False). `pure` is True when nothing else is asked:
    no new metric, place, year or breakdown. The scheme is a
    _SCHEME_NAME_PATTERN key, or "Focus" for a bare "focus", which names
    neither Focus scheme and is left to the which-Focus pause, exactly as
    _scheme_swap_rewrite does."""
    q = (question or "").strip()
    if not q or len(q.split()) > 16 or not _SUBSTITUTION_CUE.search(q):
        return None, False
    named = sorted(_exact_schemes(q))
    residue = q
    for s in named:
        residue = _SCHEME_NAME_PATTERN[s].sub(" ", residue)
    if not named and _BARE_FOCUS_WORD.search(q):
        named, residue = ["Focus"], _BARE_FOCUS_WORD.sub(" ", residue)
    if len(named) != 1:
        return None, False
    return named[0], not _SUBSTITUTION_FILLER.sub(" ", residue).strip()


def is_scheme_substitution(question: str, prev: "object") -> bool:
    """True when `question` swaps the previous turn's scheme for another one
    and otherwise continues that turn: a pure substitution, or a "same"-cued
    one with changes of its own. Needs a data or knowledge antecedent. Naming
    the scheme the previous turn was already about is not a substitution.
    Used by _run_pipeline (to treat it as a follow-up) and by the router's
    is_cacheable (it only means something inside its own conversation)."""
    if prev is None or getattr(prev, "route", None) not in _ANTECEDENT_ROUTES:
        return False
    target, pure = _scheme_substitution(question)
    if not target or target in (getattr(prev, "schemes", None) or []):
        return False
    return pure or bool(_STRONG_SUBSTITUTION_CUE.search(question or ""))


def looks_like_followup(question: str) -> bool:
    q = question.strip()
    if len(q.split()) > 12:
        return False
    # "in MGNREGA?" / "for PMAY-G" / "and Focus Plus?" — a scheme-swap fragment.
    # Checked before _STANDALONE_ANCHOR, which would otherwise veto it because it
    # names a scheme.
    if _SCHEME_SWAP_FOLLOWUP.match(q):
        return True
    # "give me same for mgnrega", "do the same for PMAY-G", "same thing for CM
    # Elevate Legacy": the same scheme swap with any continuation wording, not
    # only the openings _SCHEME_SWAP_FOLLOWUP lists. A pure substitution names
    # nothing but the scheme, so it cannot stand on its own either.
    if _scheme_substitution(q) != (None, False) and _scheme_substitution(q)[1]:
        return True
    # "same for the remaining schemes" — names no scheme of its own, so it
    # only means something against the previous question.
    if _REST_OF_SCHEMES.search(q) and not _mentions_scheme(q):
        return True
    # "pick any scheme and explain" — its answer depends on what THIS
    # conversation has already covered, so it must never be served from the
    # shared response cache (which skips follow-ups).
    if _is_scheme_pick_request(q):
        return True
    # A recommendation may draw the user's profile from earlier turns, and a
    # "why did you choose X?" is about the previous answer — both depend on
    # this conversation, so neither may be served from the shared cache.
    if _WHY_CHOICE.search(q) or _is_recommendation_request(q):
        return True
    if _FOLLOWUP_LEAD.search(q):
        return True
    # A question that names its own scheme outright is self-anchoring, same as
    # _STANDALONE_ANCHOR's "scheme"/"mgnrega"/"pmay" entries — but that regex
    # predates Focus Plus/CM Elevate and was never extended to them, so "What
    # benefits will I get under this Focus Plus" tripped _FOLLOWUP_PRONOUN on
    # "this", found no anchor, and got rewritten against the PREVIOUS turn's
    # scheme (e.g. "...compared to PMAY-G?") even though it names its own
    # scheme in full. _mentions_scheme covers all four schemes without having
    # to keep two scheme-name lists in sync.
    if _mentions_scheme(q):
        return False
    # "is there any producer group named Sakania PG?" — a NAME LOOKUP is
    # self-contained: a group name identifies the group statewide. The "there"
    # of "is there" tripped _FOLLOWUP_PRONOUN, the rewrite glued on the previous
    # turn's place ("...named Sakania PG in Betasing block"), and the lookup
    # answered a false "no such group" for a group that exists in East Khasi
    # Hills (reported live 2026-09-25). Only an explicit pointer back ("in that
    # block", "...named X there?") keeps it a follow-up.
    if (_PG_NAMED_ENTITY.search(q) or _GROUP_NAME_LOOKUP.search(q)) \
            and not _NAME_LOOKUP_BACKREF.search(q):
        return False
    if _FOLLOWUP_PRONOUN.search(q) and not _STANDALONE_ANCHOR.search(q):
        return True
    # A bare fragment ("in East Garo Hills", "by block") with no anchor of its own.
    return len(q.split()) <= 6 and not _STANDALONE_ANCHOR.search(q)


def _scheme_swap_rewrite(prev_question: str, followup: str) -> "str | None":
    """Deterministic rewrite for a bare scheme-swap follow-up ("in MGNREGA?",
    "what about PMAY-G?"). Substitutes the follow-up's scheme for the one named
    in the previous question, so the intent ("same question, other scheme") is
    preserved exactly. The LLM rewrite tends to *append* the new scheme instead
    of replacing the old one ("... in Focus Plus ... in MGNREGA?"), which then
    reads as a cross-scheme question. Returns None if the fragment names no
    scheme or the previous question can't be adapted — the caller falls back to
    the LLM path."""
    if not (_SCHEME_SWAP_FOLLOWUP.match((followup or "").strip())
            or _scheme_substitution(followup)[1]):
        return None
    target = next((name for name, pat in _SCHEME_NAME_PATTERN.items()
                   if pat.search(followup)), None)
    # A bare "focus" names neither Focus scheme: carry the word itself into the
    # question, so the "which Focus?" pause (_is_ambiguous_focus) asks rather
    # than this rewrite guessing one.
    label = target or ("Focus" if _BARE_FOCUS_WORD.search(followup or "") else None)
    if not label or not prev_question:
        return None
    out = prev_question
    replaced = False
    for name, pat in _SCHEME_NAME_PATTERN.items():
        if name == target:
            continue
        if pat.search(out):
            out = pat.sub(label, out)
            replaced = True
    if not replaced:
        if target and _SCHEME_NAME_PATTERN[target].search(out):
            return out  # previous question was already about the target scheme
        out = f"{out.rstrip(' ?.')} for {label}"
    out = re.sub(r"\s+,", ",", out).strip()
    logger.info("follow-up scheme-swap: %r + %r -> %r", prev_question, followup, out)
    return out


# ── A scheme swap that carries a measure the new scheme does not hold ───────
# Reported 2026-09-29: "give me for pmay" after "Total MGNREGA person-days in
# 2023-24" rewrote, correctly, to "Total PMAY-G person-days in 2023-24". But
# PMAY-G records no person-days. The generator wrote four queries, the
# resolved-scope guard rejected all four, and the user got "couldn't build a
# working query". "wage expenditure" swapped to PMAY-G was answered as the
# amount released, a different measure, without saying so.
#
# The swap keeps the previous operation, and that is right when the new scheme
# has the measure. When the measure belongs to one scheme only, the answer is
# to say so and offer the new scheme's own measures for the same scope, one
# tap each. Never a guess at the "closest" measure. Scheme-specific measures
# only: money and counts exist in every scheme and are left to the generator.
# SCHEME_METRICS (schema_context) is the source for what each scheme holds.
_SCHEME_OWN_MEASURES: "dict[str, list[tuple[str, re.Pattern]]]" = {
    "MGNREGA": [
        ("person-days", re.compile(r"\bperson[\s-]?days?\b|\bman[\s-]?days?\b|"
                                   r"\b100[\s-]?days?\b", re.IGNORECASE)),
        ("job cards", re.compile(r"\bjob[\s-]?cards?\b", re.IGNORECASE)),
        ("wage expenditure", re.compile(r"\bwages?\b", re.IGNORECASE)),
        ("material expenditure", re.compile(r"\bmaterial\s+(?:expenditure|cost|spend)",
                                            re.IGNORECASE)),
        ("employment", re.compile(r"\b(?:households?|persons?|people|women)\s+(?:were\s+)?"
                                  r"employed\b|\bemployment\b", re.IGNORECASE)),
    ],
    "PMAY-G": [
        ("houses", re.compile(r"\bhouses?\b|\bhousing\s+units?\b|\bplinth\b|\broof[\s-]?cast\b",
                              re.IGNORECASE)),
    ],
}
# What to offer instead, per scheme: (chip label, question with a {scope}
# slot). Each is a question the scheme's own use cases answer.
_SCHEME_HEADLINE_OFFERS: "dict[str, list[tuple[str, str]]]" = {
    "MGNREGA": [("Person-days", "Total MGNREGA person-days{scope}"),
                ("Households employed", "How many households were employed under MGNREGA{scope}?"),
                ("Total expenditure", "Total MGNREGA expenditure{scope}")],
    "PMAY-G": [("Houses sanctioned", "How many PMAY-G houses were sanctioned{scope}?"),
               ("Houses completed", "How many PMAY-G houses were completed{scope}?"),
               ("Amount released", "How much PMAY-G amount was released{scope}?")],
    "Focus Plus": [("Disbursements", "How many Focus Plus disbursements were made{scope}?"),
                   ("Amount disbursed", "How much was disbursed under Focus Plus{scope}?")],
    "Focus Legacy": [("Producer groups paid",
                      "How many producer groups were paid under Focus Legacy{scope}?"),
                     ("Amount disbursed", "How much was disbursed under Focus Legacy{scope}?")],
    "CM Elevate": [("Applications", "How many CM Elevate applications were received{scope}?"),
                   ("Valid applications",
                    "How many CM Elevate applications were verified Valid{scope}?")],
    "CM Elevate Legacy": [("Sanctioned amount",
                           "What is the total sanctioned amount under CM Elevate Legacy{scope}?"),
                          ("Total disbursed",
                           "What is the total amount disbursed under CM Elevate Legacy{scope}?")],
}
# CM Elevate (applications) records no financial year at all (SCHEME_METRICS),
# so a carried year is left out of its offers rather than paused on again.
_SCHEMES_WITHOUT_YEAR = {"CM Elevate"}


def _swap_scope_phrase(state: "object", scheme: str, prev: "object" = None) -> str:
    """" in <place> in FY 2023-24" from the conversation state's committed
    scope (the previous turn's resolved filters when there is no state), for
    the offers of _swap_measure_gap. The most specific place only; the
    resolver finds its parents."""
    resolved = getattr(prev, "resolved_entities", None) or {}

    def _get(name: str, key: str):
        return getattr(state, name, None) if state is not None else resolved.get(key)

    parts: list[str] = []
    village = _get("village", "village")
    block = _get("block", "block")
    district = _get("district", "district")
    if village:
        parts.append(f" in {str(village).title()} village"
                     + (f" in {str(block).title()} block" if block else ""))
    elif block:
        parts.append(f" in {str(block).title()} block")
    elif district:
        parts.append(f" in {str(district).title()}")
    if scheme not in _SCHEMES_WITHOUT_YEAR:
        year = _get("year", "year_key")
        if _get("year_all", "year_all"):
            parts.append(" across all financial years")
        elif isinstance(year, int):
            parts.append(f" in FY {year}-{(year + 1) % 100:02d}")
    return "".join(parts)


def _swap_measure_gap(followup: str, rewritten: str, prev: "object",
                      state: "object") -> "ClarificationNeeded | None":
    """The pause to raise when a scheme-swap follow-up ("for pmay", "give me
    for pmay", "same for PMAY-G") carried over a measure only another scheme
    holds, else None. Only after a DATA turn (a knowledge swap such as "how
    are wages paid?" -> "for pmay" is a question about the scheme, not a
    measure), only when the follow-up did not type the measure itself, and
    only when the rewrite names exactly one scheme."""
    if prev is None or getattr(prev, "route", None) != "data":
        return None
    if not (_SCHEME_SWAP_FOLLOWUP.match((followup or "").strip())
            or _scheme_substitution(followup)[0]):
        return None
    named = _exact_schemes(rewritten)
    if len(named) != 1:
        return None
    target = next(iter(named))
    for owner, measures in _SCHEME_OWN_MEASURES.items():
        if owner == target:
            continue
        for label, rx in measures:
            if rx.search(rewritten) and not rx.search(followup or ""):
                scope = _swap_scope_phrase(state, target, prev)
                options = [{"label": lab, "question": tmpl.format(scope=scope)}
                           for lab, tmpl in _SCHEME_HEADLINE_OFFERS.get(target, [])]
                if not options:
                    return None
                example = options[0]["question"].rstrip("?")
                return ClarificationNeeded(
                    f"{target} doesn't record {label} — only {owner} does. "
                    f"Here is what {target} does hold{scope}. Which would you like? "
                    f"Tap one, or ask it in full, for example “{example}”.",
                    options=options, rule="swap-measure-unavailable")
    return None


def _rest_of_schemes_rewrite(prev: "object", followup: str) -> "str | None":
    """Deterministic rewrite for "same for the remaining / other / all schemes":
    the previous question with its scheme replaced by the list of schemes it
    asks about. None when the follow-up is not that shape, names a scheme
    itself, or the previous question named no scheme to swap out.

    On a KNOWLEDGE antecedent, schemes that share one knowledge base
    (rag.kb_scheme — CM Elevate and CM Elevate Legacy) count once: after a CM
    Elevate "how to apply", CM Elevate Legacy is not a "remaining" scheme with
    different material, and listing it would repeat the same answer."""
    f = (followup or "").strip()
    if not f or len(f.split()) > 12 or _mentions_scheme(f):
        return None
    rest = bool(_REST_OF_SCHEMES.search(f))
    every = not rest and bool(_ALL_SCHEMES_FOLLOWUP.search(f))
    if not (rest or every):
        return None
    prev_q = getattr(prev, "question", "") or ""
    prev_named = [s for s, pat in _SCHEME_NAME_PATTERN.items() if pat.search(prev_q)]
    if not prev_named:
        return None
    knowledge = getattr(prev, "route", "") == "knowledge"
    key = rag.kb_scheme if knowledge else (lambda s: s)
    done = {key(s) for s in prev_named} if rest else set()
    targets: list[str] = []
    for s in SCHEME_CATALOG:
        if key(s) in done:
            continue
        done.add(key(s))
        targets.append(s)
    if not targets:
        return None
    names = targets[0] if len(targets) == 1 else ", ".join(targets[:-1]) + " and " + targets[-1]
    # Mark every old scheme mention first, then put the list in at the first
    # mark only — substituting the list directly would let a later pattern
    # match a name INSIDE the list just inserted ("CM Elevate") and delete it.
    out = prev_q
    for s in prev_named:
        out = _SCHEME_NAME_PATTERN[s].sub("\x00", out)
    out = out.replace("\x00", names, 1).replace("\x00", "")
    out = re.sub(r"\s+([,?.])", r"\1", re.sub(r"\s{2,}", " ", out)).strip()
    logger.info("follow-up rest-of-schemes: %r + %r -> %r", prev_q, followup, out)
    return out


async def rewrite_followup(question: str, prev: "object", extra_context: str = "",
                          kind: "str | None" = None, cleared: "dict | None" = None) -> str:
    """Turn a fragment into a standalone question using the previous turn.
    Falls back to the original question on any failure — never raises.

    `extra_context` (optional): the context layer's token-budgeted structured
    state / summary / relevant-older-turns block (see
    context_manager.build_followup_context) — spliced into the prompt ahead
    of the previous turn, for a conversation where "the previous turn" alone
    has lost the thread (e.g. a KNOWLEDGE digression sits between the DATA
    answer being followed up on and this fragment). Blank by default, so
    every existing caller is unaffected."""
    if not settings.FOLLOWUP_REWRITE_ENABLED or prev is None:
        return question
    rest = _rest_of_schemes_rewrite(prev, question)
    if rest:
        return rest
    swap = _scheme_swap_rewrite(getattr(prev, "question", "") or "", question)
    if swap:
        return swap
    sections: list[tuple[str, str]] = []
    tiers: list[str] = []
    guard = "error"
    try:
        sections, tiers, provenance = _build_rewrite_prompt(question, prev, extra_context, kind,
                                                            cleared)
        out = await llm.call_classifier("".join(text for _, text in sections))
        out = out.strip().strip('"').splitlines()[0].strip()
        if 3 <= len(out) <= 300:
            violation = _rewrite_provenance_violation(out, **provenance)
            guard = f"rejected:{violation}" if violation else "ok"
            if violation:
                # The model put a scheme/district/block into the question that
                # neither the user nor the previous turn's scope ever named.
                # Most often it was copied from earlier text. Answering that
                # question would give a confident figure for the wrong scope.
                # The original fragment goes on instead: inject_scheme_hint and
                # the prior_resolved carry still give it the previous turn's
                # scheme and filters, deterministically.
                logger.warning("follow-up rewrite introduced a %s no permitted source names "
                               "(%r -> %r) — using the original question", violation, question, out)
                return question
            logger.info("follow-up rewrite: %r -> %r", question, out)
            return out
        guard = "unusable"
    except Exception as e:  # noqa: BLE001
        logger.warning("follow-up rewrite failed (%s) — using original", e)
    finally:
        if sections:
            context_budget.log_prompt_context("rewrite", sections, tiers=tiers, guard=guard,
                                              followup_kind=kind)
    return question


# A follow-up that asks for every / the other schemes legitimately names schemes
# the previous turn didn't. The deterministic rewrites above catch the common
# phrasings, so this only keeps the provenance check from rejecting the rest.
_CROSS_SCHEME_FOLLOWUP = re.compile(
    r"\b(?:all|every|each|both|other|remaining|across|different)\s+(?:the\s+)?schemes?\b"
    r"|\bcross[\s-]scheme\b",
    re.IGNORECASE,
)


def _build_rewrite_prompt(question: str, prev: "object", extra_context: str = "",
                          kind: "str | None" = None, cleared: "dict | None" = None
                          ) -> "tuple[list[tuple[str, str]], list[str], dict]":
    """The follow-up rewrite prompt as named sections (for the prompt_context
    log), the evidence tiers used, and the provenance sources the rewrite is
    checked against.

    The previous turn reaches this prompt as STRUCTURED evidence
    (context_manager.build_rewrite_evidence): its question, its filters and,
    only when the follow-up points into it, its result. It is no longer a fixed
    300-character slice of its answer, which carried names the follow-up never
    referred to and cut off anything further on."""
    ev = context_manager.build_rewrite_evidence(question, prev, kind)
    pointed = bool(ev["result"] or ev["answer"])
    instructions = (
        "Rewrite the FOLLOW-UP as a complete, standalone question by reusing "
        "context from the PREVIOUS question. Keep the user's intent; change only "
        "what the follow-up changes (e.g. a different district, year, or metric). "
        "Take every district, block, village, year, tranche, scheme, category or other "
        "filter ONLY from the FOLLOW-UP itself, the PREVIOUS question, the PREVIOUS "
        "filters, or the Known context below"
        + (" — and take a name from the PREVIOUS result or answer only for the item "
           "the FOLLOW-UP points at" if pointed else "")
        + ". When in doubt, leave it out rather than guess one. "
        "Return ONLY the rewritten question, nothing else.\n\n"
    )
    sections = [
        ("instructions", instructions),
        ("conversation_state", f"{extra_context}\n\n" if extra_context else ""),
        ("previous_question", f'PREVIOUS question: "{getattr(prev, "question", "") or ""}"\n'),
        ("previous_filters", f"{ev['filters']}\n" if ev["filters"] else ""),
        ("previous_result", f"{ev['result']}\n" if ev["result"] else ""),
        ("previous_answer_excerpt", f"{ev['answer']}\n" if ev["answer"] else ""),
        # Filters the follow-up REMOVES ("show it by district" after a one-district
        # turn). Without this line the model copied the district back from the
        # previous question (live run 2026-09-26, Scenario C) and the breakdown
        # came back filtered to that one district.
        ("removed_filters",
         "REMOVED by the follow-up (do NOT include these): "
         + "; ".join(f"{k}={v}" for k, v in cleared.items()) + "\n" if cleared else ""),
        ("current_question", f'FOLLOW-UP: "{question}"\n\n'),
        ("frame", "Standalone question:"),
    ]
    ctx = context_manager.split_followup_context(extra_context)
    allowed = [question, getattr(prev, "question", "") or "", ev["filters"], ctx["state"],
               ev["result"], ev["answer"]]
    # Text the rewrite must NOT take names from: the summary and older turns
    # without a back-reference, and the previous answer unless its excerpt was
    # sent. A name found only there is contamination (context_policy "name").
    denied = [] if ev["answer"] else [getattr(prev, "answer", "") or ""]
    if context_manager.references_history(question):
        allowed += [ctx["history"], ctx["summary"]]
    else:
        denied += [ctx["history"], ctx["summary"]]
    provenance = {
        "question": question,
        "allowed_text": "\n".join(a for a in allowed if a),
        "allowed_schemes": list(getattr(prev, "schemes", None) or []),
        "denied_text": "\n".join(d for d in denied if d),
        "cleared": dict(cleared or {}),
    }
    return sections, ev["tiers"], provenance


def _exact_schemes(text: str) -> set[str]:
    return {s for s, pattern in _SCHEME_NAME_PATTERN.items() if pattern.search(text or "")}


def _rewrite_provenance_violation(rewritten: str, *, question: str, allowed_text: str,
                                  allowed_schemes: "list[str]", denied_text: str = "",
                                  cleared: "dict | None" = None) -> "str | None":
    """The field ('scheme', 'district', 'block', 'year', 'metric', 'category'
    or 'name') that the rewritten follow-up names with no permitted source,
    else None. Each field has its own rule (context_policy.rewrite_violation,
    documented in AI_PIPELINE.md §5.4). Permitted sources are the follow-up
    itself, the previous question, the previous filters, the structured
    Known-context line, and, only when the follow-up points into them, the
    previous result or answer excerpt. The summary and older turns count only
    when the follow-up reaches back to them. Names are compared canonically,
    so "ekh" in the follow-up permits "East Khasi Hills". An explicit year
    change by the user is always allowed."""
    return context_policy.rewrite_violation(
        rewritten, question=question, allowed_text=allowed_text, denied_text=denied_text,
        allowed_schemes=allowed_schemes, cleared=cleared)


def _cleared_filters(plan, prev, state) -> dict:
    """{field: previous value} for every geography/year field the merge plan
    CLEARs and the previous turn actually had. Told to the rewrite, and
    enforced by the provenance check, so a removed filter can't come back
    through the rewritten text."""
    if plan is None:
        return {}
    resolved = getattr(prev, "resolved_entities", None) or {}
    values = {"district": resolved.get("district") or getattr(state, "district", None),
              "block": resolved.get("block") or getattr(state, "block", None),
              "year": resolved.get("year_key") if resolved.get("year_key") is not None
              else getattr(state, "year", None)}
    out = {}
    for name, value in values.items():
        if value is None or plan.action(name) != context_policy.CLEAR:
            continue
        out[name] = context_manager._fy_text(int(value)) if name == "year" else value
    return out


# ── Guided-decoding schemas ──────────────────────────────────────────────────
# Passed to the classifier/SQL model so it can only emit tokens that fit the
# shape. The _extract_json / _extract_sql parsers below still run, so these are
# a reliability + speed win with no hard dependency on the gateway honouring
# them. Kept next to the prompts they constrain.
_SCHEMES_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "schemes": {
            "type": "array",
            "items": {"type": "string", "enum": list(SCHEME_CATALOG)},
            "minItems": 1,
        }
    },
    "required": ["schemes"],
}
# All optional strings — no null-union type (some xgrammar builds reject it) and
# no `required` (the model emits only the keys it actually finds). The parser in
# extract_entity_mentions already tolerates a partial or empty object.
_ENTITY_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "district": {"type": "string"},
        "block": {"type": "string"},
        "village": {"type": "string"},
        "year": {"type": "string"},
        "assembly_constituency": {"type": "string"},
        # Plural — ONLY when the question names two-or-more blocks to compare
        # against each other ("compare X and Y"). Kept separate from "block"
        # rather than making "block" a string-or-array union (some xgrammar
        # builds reject union types, per the note above).
        "blocks": {"type": "array", "items": {"type": "string"}},
        # Same idea, for districts ("compare X and Y between district A and B").
        "districts": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}
_INTENT_JSON_SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string", "enum": ["DATA", "KNOWLEDGE"]}},
    "required": ["intent"],
}
# No `required: ["issue"]` — the verifier only needs to emit "issue" when
# ok is false; a passing query should cost the fewest tokens possible.
_SQL_VERIFY_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "issue": {"type": "string"},
    },
    "required": ["ok"],
}
# Force the SQL completion to open on a bare SELECT/WITH (optionally after
# whitespace). Body is unconstrained — this enforces statement shape, not a SQL
# grammar, so it can't catch a wrong join, only a prose preamble or a ```fence.
_SQL_SHAPE_REGEX = r"\s*(SELECT|WITH|select|with)[\s\S]*"


def _extract_json(raw: str) -> dict | None:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _extract_sql(raw: str) -> str:
    # Strip a ```sql fence if the model added one despite being told not to.
    cleaned = re.sub(r"```(?:sql)?", "", raw).strip()
    cleaned = cleaned.rstrip(";").strip()
    # The generator sometimes copies a worked example that ends its outer query
    # with ';' and then appends its own trailing "LIMIT n" AFTER that ';'
    # ("... ) d;\nLIMIT 1"). That is one statement, not two — fold the stray
    # terminator away so it isn't rejected as "multiple statements". Only a bare
    # trailing LIMIT/OFFSET/FETCH tail is spliced back; anything else after a ';'
    # is left for the multi-statement guard to reject.
    m = re.search(
        r";\s*((?:LIMIT|OFFSET|FETCH)\b[\s\S]*)$", cleaned, re.IGNORECASE)
    if m:
        cleaned = (cleaned[: m.start()] + "\n" + m.group(1)).strip()
    return cleaned


_SCHEME_NAME_PATTERN = {
    "MGNREGA": re.compile(r"\bmgnrega\b|\bmnrega\b|\bnrega\b", re.IGNORECASE),
    "PMAY-G": re.compile(r"\bpmay[\s-]?g?\b|\bawa+s?\b", re.IGNORECASE),
    "Focus Plus": re.compile(r"\bfocus[\s-]?plus\b|\bfocus\s*\+|\bfocusplus\b", re.IGNORECASE),
    # The lookarounds keep "CM Elevate Legacy" / "CM Elevate Disbursement" /
    # "Legacy CM Elevate" from ALSO reading as the applications dataset — that is
    # the other scheme below, and matching both turned a single-scheme question
    # into a two-scheme comparison.
    "CM Elevate": re.compile(
        r"(?<!legacy )(?<!legacy-)\bcm[\s-]?elevate\b(?![\s-]*(?:legacy|disbursements?)\b)|"
        r"(?<!legacy )(?<!legacy-)\bcmelevate\b(?![\s-]*(?:legacy|disbursements?)\b)",
        re.IGNORECASE),
    # CM Elevate Legacy is the SANCTION-AND-DISBURSEMENT dataset (DB name: CM
    # Elevate Disbursement), NOT the CM Elevate applications dataset. The two share
    # the name and no key (cmelevatelegacy_entity_resolver.yaml
    # scheme.disambiguation). Only a qualified form names it here; a bare "CM
    # Elevate" that carries money / year / lender vocabulary is re-pointed to it
    # by _pin_cm_elevate_dataset() before routing.
    "CM Elevate Legacy": re.compile(
        r"\bcm[\s-]?elevate[\s-]*(?:legacy|disbursements?)\b|"
        r"\bcmelevate[\s-]*(?:legacy|disbursements?)\b|"
        r"\blegacy[\s-]+cm[\s-]?elevate\b|\belevate[\s-]?legacy\b",
        re.IGNORECASE),
    # Focus Legacy is the producer-group scheme, NOT Focus Plus. The two share the
    # word "Focus" and nothing else (no shared key; Focus Plus holds no
    # producer-group column at all — focuslegacy_entity_resolver.yaml
    # scheme.disambiguation). Both patterns demand a qualifier, so a BARE "focus"
    # matches neither and falls through to the "which Focus?" ask below.
    "Focus Legacy": re.compile(
        r"\bfocus[\s-]?legacy\b|\bfocuslegacy\b|\blegacy[\s-]?focus\b|"
        r"\bold[\s-]?focus\b|\bfocus[\s-]?pg\b|\bpg[\s-]?focus\b",
        re.IGNORECASE),
}
# Fuzzy fallback for a scheme name the exact regex above misses because it's
# misspelled ("manrega", "pamay") — mirrors the RapidFuzz tolerance
# entity_resolver.py already gives district/block names. Kept separate from
# _SCHEME_NAME_PATTERN (exact match stays the fast, zero-false-positive path;
# this only runs when nothing named matches outright).
_SCHEME_FUZZY_ALIASES = {
    "MGNREGA": ["mgnrega", "mnrega", "nrega"],
    "PMAY-G": ["pmay", "pmayg", "awaas", "awas"],
    # NOTE: the bare "focus" alias used to live here, so a typo of the short form
    # ("facus", "fokus") still routed to Focus Plus. It was REMOVED when Focus
    # Legacy landed: with two live Focus schemes a bare or misspelled "focus"
    # identifies neither, and guessing Focus Plus would answer a producer-group
    # question from a partition that holds no producer groups at all. Such a
    # question now reaches _focus_ambiguity_clarification() instead.
    "Focus Plus": ["focusplus"],
    "CM Elevate": ["cmelevate"],
    "Focus Legacy": ["focuslegacy"],
    "CM Elevate Legacy": ["cmelevatelegacy"],
}
_FUZZY_SCHEME_ACCEPT = 80


def _fuzzy_named_schemes(question: str) -> list[str]:
    words = [w.lower() for w in re.findall(r"[A-Za-z]+", question or "") if len(w) >= 5]
    if not words:
        return []
    hits: list[str] = []
    for scheme, aliases in _SCHEME_FUZZY_ALIASES.items():
        if any(fuzz.ratio(w, a) >= _FUZZY_SCHEME_ACCEPT for w in words for a in aliases):
            hits.append(scheme)
    return hits


_SCHEME_CANONICAL_SPELLING = {
    "MGNREGA": "MGNREGA", "PMAY-G": "PMAY-G",
    "Focus Plus": "Focus Plus", "CM Elevate": "CM Elevate",
    "Focus Legacy": "Focus Legacy", "CM Elevate Legacy": "CM Elevate Legacy",
}


def _correct_scheme_spelling(question: str) -> str:
    """Fix an obviously misspelled scheme name in the question text itself
    ("manrega" -> "MGNREGA", "pamay" -> "PMAY-G") before routing. Without this,
    a typo'd scheme name only ever gets caught on the DATA path (via
    _fuzzy_named_schemes) — the KNOWLEDGE/RAG path embeds the raw text
    verbatim, so the typo silently tanks vector-search relevance against the
    scheme's own reference docs and the question comes back "not covered".
    Conservative by construction: a word only gets corrected when it does NOT
    already exactly match a scheme alias (nothing to fix) and DOES fuzzy-match
    one closely enough — so it can't mangle an unrelated word."""
    if not question:
        return question

    # A scheme already named correctly elsewhere in the question (exact match,
    # e.g. "CM ELEVATE") must not also be "corrected" word-by-word below — the
    # lone word "Elevate" fuzzy-matches the "cmelevate" alias on its own
    # (fuzz.ratio("elevate", "cmelevate") ~= 87.5, over the 80 threshold), so
    # without this guard "CM ELEVATE" gets the already-present "CM" duplicated
    # into "CM CM Elevate" (and worse on a second pass, e.g. a resumed
    # clarification chip, into "CM CM CM Elevate").
    _already_named = {s for s, pat in _SCHEME_NAME_PATTERN.items() if pat.search(question)}
    # "CM Elevate Legacy" already contains the "CM Elevate" words, but the CM
    # Elevate pattern deliberately does not match it — so without this the lone
    # word "Elevate" would be "corrected" into "CM CM Elevate Legacy".
    if "CM Elevate Legacy" in _already_named:
        _already_named.add("CM Elevate")

    def _sub(m: "re.Match") -> str:
        word = m.group(0)
        if len(word) < 5:
            return word
        wl = word.lower()
        for scheme, aliases in _SCHEME_FUZZY_ALIASES.items():
            if scheme in _already_named:
                continue
            if wl in aliases:
                return word
            if any(fuzz.ratio(wl, a) >= _FUZZY_SCHEME_ACCEPT for a in aliases):
                return _SCHEME_CANONICAL_SPELLING[scheme]
        return word

    return re.sub(r"[A-Za-z]+", _sub, question)


# The user explicitly wants a cross-scheme answer — honour it, don't ask.
_EXPLICIT_BOTH = re.compile(
    r"\b(both schemes?|all schemes?|each scheme|per scheme|by scheme|across schemes?|"
    r"every scheme|either scheme|the two schemes|scheme[\s-]?wise)\b",
    re.IGNORECASE,
)
# Vocabulary that only makes sense for ONE scheme, so we can infer it without
# asking even when the scheme is not named. Kept deliberately narrow — a term
# that both schemes use (e.g. "beneficiaries", "paid", "pending", "amount")
# must NOT appear here, or an ambiguous question gets silently mis-routed.
_MGNREGA_ONLY_TERMS = re.compile(
    r"\b(person[\s-]?days?|job cards?|100[\s-]?days?|hundred days?|muster|"
    # "households/persons [were/are/have been] employed" — the naive adjacent
    # phrase ("households? employed") missed the common "persons WERE employed"
    # wording (TC-024: a plain past-tense employment question with no scheme
    # named kept falling through to the scheme-clarification prompt).
    r"households?\s+(?:were\s+|are\s+|have\s+been\s+|has\s+been\s+)?employed|"
    r"persons?\s+(?:were\s+|are\s+|have\s+been\s+|has\s+been\s+)?employed|"
    # "received employment" is as common a phrasing as "were employed" — same
    # gap as above, just a different verb (DATA-004 used exactly this).
    r"(?:households?|persons?)\s+receiv\w*\s+employment|"
    # "women [were/are] provided/given employment" / "women employed" — same
    # missing-verb-phrasing gap as above (DATA-008 used exactly this; there
    # was no women-employment pattern here at all before, only in edge.py's
    # unrelated domain whitelist).
    r"women\s+(?:were\s+|are\s+)?(?:provided|given)\s+employment|"
    r"women\s+employ\w*|"
    # A constituency pinned MGNREGA on its own while only mgnrega_employment
    # carried one. Focus Legacy and CM Elevate Legacy reach ac_name too
    # (_AC_CAPABLE_SCHEMES), so _infer_scheme_from_terms no longer lets this
    # term decide alone (_MGNREGA_AC_TERM, KI-148).
    r"assembly constituenc\w*|\bAC\s*\d+\b|"
    # unskilled/semi-skilled wage(s) — "wage" without the "s?" only matched the
    # singular; "unskilled wages" (the common plural phrasing, DATA-010) never
    # matched because \b after "wage" can't land inside "wages".
    r"unskilled wages?|semi[\s-]?skilled|"
    # Measures that only MGNREGA holds but that the testers phrased without a
    # scheme name — each one drew an unnecessary "which scheme?" pause (MGNREGA
    # QA 2026-09-26, KI-043): "spent on materials" (DATA-011), "wages or
    # materials" (DATA-023), "villages … received employment" / "women received
    # employment" (DATA-014/017 — the verb, not the subject, is the signal),
    # "employment persons/households" (DATA-019/020), "employment generated"
    # (DATA-030), and administrative expenditure, whose only column is
    # MGNREGA's (DATA-013). No other loaded scheme has wages, materials or
    # employment at all.
    r"(?:spent|spend\w*|expenditure|costs?)\s+on\s+(?:materials?|wages?)|"
    r"materials?\s+(?:expenditure|costs?|spend\w*)|wage\s+expenditure|"
    r"wages?\s+(?:or|and|vs\.?|versus)\s+materials?|materials?\s+(?:or|and|vs\.?|versus)\s+wages?|"
    r"receiv\w*\s+employment|employment\s+(?:persons|households)|"
    r"employment\s+generated|generated\s+employment|"
    r"administrative\s+(?:expenditure|expense|cost)s?|admin\s+(?:expenditure|expense|cost)s?|"
    r"labour budget|works? demanded|wage employment|employment guarantee)\b",
    re.IGNORECASE,
)
_PMAY_ONLY_TERMS = re.compile(
    r"\b(house|houses|housing|dwelling units?|pucca house|kutcha house|"
    r"sanctioned houses?|instal{1,2}ments?|geotag|"
    # "tranche"/"tranch" is deliberately NOT here, even though PMAY-G also has
    # an installments_paid column. Focus Plus's own vocabulary IS "tranche"
    # (its stored column is literally tranche_label); PMAY-G's own vocabulary
    # is "installment" (instal?ments? above already covers it). A bare
    # "tranche 1 vs tranche 2" with no scheme named should default to Focus
    # Plus, not pause — see _FOCUSPLUS_ONLY_TERMS below, which is where
    # "tranche" is claimed.
    r"completion certificate|house status|awaas|awas|"
    # "sanctioned"/"released"/"pending" amount phrasing is PMAY-specific — MGNREGA
    # never "sanctions" anything (it has expenditure), CM Elevate has no money
    # column at all, and Focus Plus vocabulary is "disbursement"/"payment", not
    # "sanctioned". Without these, a bare "how much sanctioned amount has been
    # released in [village]?" (no scheme named) fell through to an unnecessary
    # "which scheme?" pause instead of being understood as PMAY-G.
    # Plural "amounts"/"numbers" ('s?') — the singular-only forms below missed
    # "sanctioned amountS" (PMAY-OFF-023) and "amount released" is fine as a
    # fixed phrase, but sanction NUMBER(S) (PMAY-OFF-026) had no pattern at all.
    # The first form tolerates the usual misspellings of "sanctioned" (sactioned,
    # sancioned, santioned, sanctoned, sanctiond) — the use-case sheet's own
    # "How much sactioned amount has been released" paused for a scheme
    # (PMAY-G QA 2026-09-28, PMAY-OFF-003).
    r"sa[nc]{1,2}t?i?on{1,2}e?d amounts?|amounts? (?:released|sanctioned|pending)|"
    r"released amounts?|pending amounts?|sanction dates?|sanction numbers?|"
    # "received (any/the/no) amount", "received full/partial amount" — the
    # release-status phrasing PMAY-OFF-008/009/010 use, distinct from the
    # "amount released" form already covered above.
    r"receiv\w*\s+(?:any|the|no|full|partial)?\s*(?:sanctioned\s+)?amount)\b",
    re.IGNORECASE,
)
# Focus Plus is a Meghalaya STATE farmer cash-benefit scheme. These terms name it or
# its scheme-specific machinery and belong to no other scheme. "tranche"/"tranch" is
# claimed HERE, not left shared with PMAY-G: it's Focus Plus's actual vocabulary (the
# stored column is tranche_label), while PMAY-G's own word for the same idea is
# "installment" (see _PMAY_ONLY_TERMS). "batch"/"disbursement" are still left out —
# those really are generic enough that an unnamed question using only those still
# asks "which scheme?".
_FOCUSPLUS_ONLY_TERMS = re.compile(
    r"\bfocus[\s-]?plus\b|\bfocus\s*\+|\bfocusplus\b|"
    r"\btranche?s?\b|"
    # "producer group" is NOT here — see _FOCUSLEGACY_ONLY_TERMS. It belongs to
    # Focus Legacy, whose grain IS the producer group; Focus Plus holds no such
    # column and focusplus_classification_rules.yaml refuses the question
    # outright (condition producer_group_requested).
    r"\bmeghalayaone\b|\bmbda\b|\bmeghalaya basin development\b|"
    r"\bfocus\+?\s*card\b|\b93k\b|\b12\.5k\b",
    re.IGNORECASE,
)
# CM Elevate covers 15 sub-schemes under one programme. These terms name the
# programme itself or its scheme-specific vocabulary that belongs to no other
# scheme. "on hold" / "verification" / "application mode" are deliberately left
# out — they read as generic status words, so an unnamed question using only
# those still asks "which scheme?".
_CMELEVATE_ONLY_TERMS = re.compile(
    r"\bcm[\s-]?elevate\b(?![\s-]*(?:legacy|disbursements?)\b)|"
    r"\bcmelevate\b(?![\s-]*(?:legacy|disbursements?)\b)|"
    r"\bpiggery\b|\bpoultry\b|\bgoat farming\b|\bwarehouse scheme\b|"
    r"\bsericulture\b|\bmotorcaravan\b|\bagro tourism villa\b|"
    r"\bprime small enterprise\b|\bprime tourism vehicle\b|"
    r"\bprime agriculture response vehicle\b|\bgreen taxi\b|"
    # "&" too: the stored name is "Meghalaya Sports & Wellness Centre Scheme", and
    # written that way it was read as MGNREGA (OFF-017 full run 2026-10-02)
    r"\bcinema theatre scheme\b|\bsports\s*(?:and|&)\s*wellness\b|"
    r"\bany business venture\b|\brequest_?id\b|"
    # Missing sub-scheme/commodity words and the scheme-FAMILY phrases
    # (vehicle/tourism/PRIME/livestock/enterprise scheme(s)) — without these,
    # a question naming only this vocabulary (no literal "CM Elevate") fell
    # through to the generic 4-way "MGNREGA, PMAY-G, Focus Plus, or CM
    # Elevate?" pause instead of being recognized as CM Elevate at all, even
    # though resolve_cm_scheme's own alias catalogue (or, for the family
    # words, resolve_cm_scheme_group_ambiguity) can place it precisely
    # (confirmed live 2026-09-11: "gender split for the tourism vehicle
    # scheme" and "applications under the vehicle schemes" both asked the
    # top-level 4-scheme question despite being unambiguously CM Elevate).
    r"\bdairy\b|\bvehicle schemes?\b|\btourism vehicle\b|\btourism schemes?\b|"
    r"\bprime schemes?\b|\bprime family\b|\blivestock schemes?\b|"
    r"\benterprise schemes?\b|"
    # "PRIME SEED" is the common short name of PRIME Small Enterprise
    # Empowerment and Development (SEED) — the largest CM Elevate programme and
    # absent from CM Elevate Legacy. Without it every SEED question paused on
    # "which scheme?" (KI-075, CM Elevate QA 2026-09-27: 006b, 007b, 008b …).
    # A bare "seed" is NOT added: Focus Legacy uses "seed money".
    r"\bprime[\s-]+seed\b|\bseed\s+(?:scheme|programme|program)\b|\(seed\)",
    re.IGNORECASE,
)


# Focus Legacy is the legacy producer-group disbursement programme. Its grain IS
# the producer group, so producer-group vocabulary belongs here and nowhere else
# (Focus Plus has no such column at all). "pg"/"pgs" are claimed as whole words —
# the resolver's own disambiguation rule names them as forcing the Focus Legacy
# reading — but "member"/"disbursement"/"payment"/"amount" are deliberately left
# out: they are shared vocabulary, so an unnamed question using only those still
# asks "which scheme?".
_FOCUSLEGACY_ONLY_TERMS = re.compile(
    r"\bfocus[\s-]?legacy\b|\bfocuslegacy\b|\blegacy[\s-]?focus\b|\bold[\s-]?focus\b|"
    r"\bproducer[\s-]?groups?\b|\bpgs?\b|\bpg[\s-]?ids?\b|"
    r"\bpg[\s-]?members?\b|\bpg[\s-]?names?\b|\bpg[\s-]?scheme\b|"
    r"\bpg[\s-]?focus\b|\bpg[\s-]?lamp\b|\bpg[\s-]?existing\b|"
    r"\bfocus[\s-]?\(?addnl\)?\b|\bfocus additional\b|"
    r"\blamp societ(?:y|ies)\b",
    re.IGNORECASE,
)


# ── The two CM Elevate datasets ─────────────────────────────────────────────
# "CM Elevate" names TWO schemes that share nothing but the name
# (cmelevatelegacy_entity_resolver.yaml scheme.disambiguation):
#   * CM Elevate        — 15-scheme APPLICATIONS (curated.v_cm_elevate): status,
#                         on hold, verification, applicant type, gender. No money,
#                         no dates.
#   * CM Elevate Legacy — 13-scheme SANCTION-AND-DISBURSEMENT records
#                         (curated.v_cm_elevate_disbursement): sanctioned amount,
#                         subsidy / loan / total disbursed, lender, financial year.
# The resolver's rule: a bare "CM Elevate" is settled by a field that exists in
# only one of them. Money, a financial year, a lender or a desanction exist only
# in Legacy, so they pin it there — answering them from the applications data
# could only ever refuse. Application-workflow words pin the other one. With
# neither, the bare name keeps its long-standing meaning (the applications data).
#
# Words that name CM Elevate Legacy on their own, with no "CM Elevate" at all.
_CMELEVATELEGACY_ONLY_TERMS = re.compile(
    r"\bcm[\s-]?elevate[\s-]*(?:legacy|disbursements?)\b|"
    r"\bcmelevate[\s-]*(?:legacy|disbursements?)\b|"
    r"\blegacy[\s-]+cm[\s-]?elevate\b|\belevate[\s-]?legacy\b|"
    r"\blifcom\b|\bloan[\s-]?entit(?:y|ies)\b|\blenders?\b|"
    r"\bdesanction\w*|\bde-sanction\w*|\bbank[\s-]?sanctioned\b|"
    r"\bcommon facility cent(?:er|re)\b",
    re.IGNORECASE,
)
# Vocabulary only the sanction-and-disbursement dataset can answer. Consulted
# ONLY once a question is already a CM Elevate question (named, or via a
# sub-scheme word such as "piggery"), so generic money words are safe here.
_CMELEVATELEGACY_FORCING = re.compile(
    r"\bdisburs\w*|\bsanction\w*|\bsubsid(?:y|ies)\b|\bloans?\b|\bgrants?\b|"
    r"\bamounts?\b|\bmoney\b|\bfunds?\b|\brupees?\b|\bcrores?\b|\blakhs?\b|\brs\.?\s*\d|"
    r"\bpaid\b|\bpayments?\b|\breleased\b|\bentitlement\b|\butili[sz]ation\b|"
    # NOT a bare "pending": in the applications data "pending" means on hold
    # (data_verified = 'On Hold'). Only the money reading forces Legacy.
    r"\binstal{1,2}ments?\b|\btranch\w*|\bpending\s+(?:amount|money|disburs\w*)|"
    r"\bpending\s+to\s+be\s+(?:paid|disbursed|released)\b|\byet\s+to\s+be\s+(?:paid|disbursed)\b|"
    r"\brefused\b|\brefusals?\b|\bduplicates?\b|\bdesanction\w*|"
    r"\blifcom\b|\blenders?\b|\bloan[\s-]?entit(?:y|ies)\b|"
    r"\bfinancial\s+years?\b|\bfy\s*\d{2}|\b20\d\d\s*[-/]\s*\d{2,4}\b|\byear[\s-]?wise\b",
    re.IGNORECASE,
)
# Vocabulary only the APPLICATIONS dataset holds. Any of these keeps a bare
# "CM Elevate" on that dataset even when a money word is also present.
_CMELEVATE_APPLICATIONS_ONLY = re.compile(
    r"\bon[\s-]?hold\b|\bverif\w*|\bdata[\s_-]?verified\b|\bwithdraw\w*|"
    r"\bapplicant[\s_-]?categor\w*|\bapplication[\s_-]?mode\b|\bonline\b|"
    r"\bcm\s*connect\w*|\bcurrent[\s_-]?level\b|\bfile[\s_-]?status\b|"
    r"\bapplication[\s_-]?status\b|\bstatus[\s-]?wise\b|\brequest[\s_-]?ids?\b|"
    r"\bgender\b|\bsector\b|\bregistered\b|\bunregistered\b|\bprime small enterprise\b|"
    r"\bseed\b|\bgreen taxi\b|\bcinema\b|\bagro tourism villa\b",
    re.IGNORECASE,
)


def _prefers_cm_elevate_legacy(question: str) -> bool:
    """A CM Elevate question whose vocabulary only the sanction-and-disbursement
    dataset can answer (and none that only the applications dataset holds)."""
    q = question or ""
    if _CMELEVATE_APPLICATIONS_ONLY.search(q):
        return False
    return bool(_CMELEVATELEGACY_FORCING.search(q) or _CMELEVATELEGACY_ONLY_TERMS.search(q))


def _pin_cm_elevate_dataset(question: str) -> str:
    """Re-point a bare "CM Elevate" at CM Elevate Legacy when the question asks
    for something only that dataset holds ("total amount disbursed under CM
    Elevate", "CM Elevate loans by lender", "CM-ELEVATE records in FY 2024-25").

    Done ONCE, on the question text, before routing — the same way a misspelled
    scheme name is corrected — so every later pattern check (scheme shortcut,
    clarification gates, follow-ups, the chips) sees one consistent scheme
    instead of each re-deciding. The rewritten question is returned to the user
    as `rewritten_question`, so the reading is visible, not silent."""
    q = question or ""
    if not q or _SCHEME_NAME_PATTERN["CM Elevate Legacy"].search(q):
        return q
    if not _SCHEME_NAME_PATTERN["CM Elevate"].search(q):
        return q
    if not _prefers_cm_elevate_legacy(q):
        return q
    out = _SCHEME_NAME_PATTERN["CM Elevate"].sub("CM Elevate Legacy", q)
    logger.info("CM Elevate dataset pinned to Legacy: %r -> %r", q, out)
    return out


def _unpin_cm_elevate(question: str) -> str:
    """Undo _pin_cm_elevate_dataset. The pin only ever writes "CM Elevate
    Legacy" into a question that named no Legacy form itself, so every
    occurrence came from the pin and reverts cleanly."""
    return re.sub(r"\bCM Elevate Legacy\b", "CM Elevate", question or "")


# "What is the total amount disbursed for Baghmara assembly constituency?" was
# read as MGNREGA (no disbursement column) and failed on all 55 constituencies
# (Focus Legacy bulk run 2026-09-29). Naming a constituency says nothing about
# the scheme once three schemes hold one: it pins MGNREGA only beside a real
# MGNREGA measure, and otherwise the scheme is asked (_scheme_clarification
# then offers only the schemes that have constituency data).
_MGNREGA_AC_TERM = re.compile(r"\bassembly constituenc\w*|\bAC\s*\d+\b", re.IGNORECASE)


def _mgnrega_terms(question: str) -> bool:
    return bool(_MGNREGA_ONLY_TERMS.search(_MGNREGA_AC_TERM.sub(" ", question or "")))


def _infer_scheme_from_terms(question: str) -> list[str] | None:
    """A single scheme implied by scheme-specific vocabulary, or None if the
    question could plausibly mean more than one."""
    hits = ["MGNREGA"] if _mgnrega_terms(question) else []
    hits += [
        s for s, rx in (
            ("PMAY-G", _PMAY_ONLY_TERMS),
            ("Focus Plus", _FOCUSPLUS_ONLY_TERMS),
            ("CM Elevate", _CMELEVATE_ONLY_TERMS),
            ("Focus Legacy", _FOCUSLEGACY_ONLY_TERMS),
            ("CM Elevate Legacy", _CMELEVATELEGACY_ONLY_TERMS),
        ) if rx.search(question)
    ]
    # A CM Elevate sub-scheme word ("piggery", "dairy") belongs to BOTH CM
    # Elevate datasets. When the rest of the question asks for money, a year or
    # a lender, only CM Elevate Legacy can answer it — settle on that one rather
    # than reading the pair as ambiguous (or sending a money question to the
    # dataset that has no money column).
    if "CM Elevate" in hits and _prefers_cm_elevate_legacy(question):
        hits = [h for h in hits if h != "CM Elevate"]
        if "CM Elevate Legacy" not in hits:
            hits.append("CM Elevate Legacy")
    return hits if len(hits) == 1 else None


# A resumed one-tap question is built by naming the scheme in the stem. Naively
# appending " for <scheme>" reads fine for a DATA stem ("total person-days for
# MGNREGA") but produces broken phrasing for a KNOWLEDGE stem that already says
# the generic word "scheme" ("tell me about scheme" -> "tell me about scheme for
# Focus Plus") — that phrasing is grammatically odd enough that the RAG answer
# composer sometimes reads it as asking about a DIFFERENT, uncovered scheme and
# refuses with "not covered" even though the retrieved passages score well
# above the medium-confidence floor (verified: top score 0.77, well above the
# 0.55 floor, yet the composer still said "not covered" on this exact phrasing).
# When the stem already names "scheme(s)" generically, replace that word with
# the actual scheme name instead of appending — "tell me about scheme" becomes
# "tell me about Focus Plus", which both reads naturally and matches how the
# reference docs open ("Focus Plus is ...").
_GENERIC_SCHEME_WORD = re.compile(r"\bschemes?\b", re.IGNORECASE)


def _scheme_option_question(stem: str, scheme: str) -> str:
    if _GENERIC_SCHEME_WORD.search(stem):
        return _GENERIC_SCHEME_WORD.sub(scheme, stem, count=1)
    return f"{stem} for {scheme}"


def _scheme_clarification(question: str) -> "ClarificationNeeded":
    stem = question.strip().rstrip(" ?.")
    if _MGNREGA_AC_TERM.search(question or ""):
        # only these schemes hold a constituency (KI-148)
        return ClarificationNeeded(
            "Which scheme does your question concern? Assembly-constituency figures are held for "
            "MGNREGA, Focus Legacy and CM Elevate Legacy.",
            options=[{"label": "MGNREGA (rural employment)", "question": _scheme_option_question(stem, "MGNREGA")},
                     {"label": "Focus Legacy (producer group payments)",
                      "question": _scheme_option_question(stem, "Focus Legacy")},
                     {"label": "CM Elevate Legacy (sanctions & disbursements)",
                      "question": _scheme_option_question(stem, "CM Elevate Legacy")}],
            rule="scheme-not-specified")
    options = [
        {"label": "MGNREGA (rural employment)",
         "question": _scheme_option_question(stem, "MGNREGA")},
        {"label": "PMAY-G (rural housing)",
         "question": _scheme_option_question(stem, "PMAY-G")},
        {"label": "Focus Plus (farmer cash benefit)",
         "question": _scheme_option_question(stem, "Focus Plus")},
        {"label": "CM Elevate (livelihood / enterprise schemes)",
         "question": _scheme_option_question(stem, "CM Elevate")},
        {"label": "Focus Legacy (producer group payments)",
         "question": _scheme_option_question(stem, "Focus Legacy")},
        {"label": "CM Elevate Legacy (sanctions & disbursements)",
         "question": _scheme_option_question(stem, "CM Elevate Legacy")},
        {"label": "Compare across schemes",
         "question": (f"{stem} across MGNREGA, PMAY-G, Focus Plus, CM Elevate, "
                      f"Focus Legacy and CM Elevate Legacy")},
    ]
    return ClarificationNeeded(
        "Which scheme does your question concern — MGNREGA, PMAY-G, Focus Plus, "
        "CM Elevate, Focus Legacy, or CM Elevate Legacy? Please select one, or choose "
        "to compare across schemes.",
        options=options,
        rule="scheme-not-specified",
    )


# ── "Focus" alone: which of the TWO Focus schemes? ──────────────────────────
# Two live schemes answer to the word "Focus" and they share NOTHING but the name:
# Focus Legacy pays PRODUCER GROUPS (Rs 5,000 per member), Focus Plus pays
# INDIVIDUAL BENEFICIARIES, there is no key between them, and Focus Plus holds no
# producer-group column at all. focuslegacy_entity_resolver.yaml's own
# scheme.disambiguation rule is explicit that a bare "Focus" with no forcing word
# must be ASKED, not guessed — guessing answers a producer-group question from a
# partition that cannot answer it (or vice versa) and reads as authoritative.
#
# This is a TWO-way ask, not the generic five-way one: the user has already told
# us it's a Focus question, so re-offering MGNREGA / PMAY-G / CM Elevate would
# throw that away.
_BARE_FOCUS_WORD = re.compile(r"\bfocus\b", re.IGNORECASE)


def _is_ambiguous_focus(question: str) -> bool:
    """The question says "Focus" but nothing that pins WHICH Focus scheme."""
    q = question or ""
    if not _BARE_FOCUS_WORD.search(q):
        return False
    # Either scheme named outright (or by its own qualified alias) — settled.
    if _SCHEME_NAME_PATTERN["Focus Plus"].search(q):
        return False
    if _SCHEME_NAME_PATTERN["Focus Legacy"].search(q):
        return False
    # A forcing word from either side's own vocabulary — also settled.
    if _FOCUSLEGACY_ONLY_TERMS.search(q) or _FOCUSPLUS_ONLY_TERMS.search(q):
        return False
    # "How many members are there in Focus Bibari?" — "Focus" is part of a
    # producer-group NAME (only Focus Legacy has groups). Asking "which Focus?"
    # and then swapping in "Focus Legacy" turned it into "Focus Legacy Bibari",
    # which matches nothing (all-PG run 2026-09-29, KI-157).
    if _pg_focus_group_name(q):
        return False
    return True


def _pg_focus_group_name(question: str) -> bool:
    pg = _pg_name_question(question)
    return bool(pg and _BARE_FOCUS_WORD.search(pg[1]) and [
        t for t in _pg_name_tokens(pg[1]) if t not in ("focus", "plus", "legacy", "scheme")])


# "focus" as an ordinary English noun or verb ("the main focus of this scheme",
# "it focuses on women"), not the scheme name.
_FOCUS_AS_NOUN = re.compile(
    r"\b(?:the|its|their|this|that|a|main|key|primary|major|core|central|special|chief)\s+focus\b|"
    r"\bfocus(?:\s+area)?s?\s+(?:of|on|upon)\b|\bfocus\s+areas?\b|\bfocus(?:es|ed|ing)\b",
    re.IGNORECASE)


def _names_bare_focus_scheme(question: str) -> bool:
    """A bare "Focus" used as a SCHEME name ("what is focus", "focus
    beneficiaries", "under focus"), which scheme it is still unsettled.
    Reported 2026-09-29: after a CM Elevate Legacy answer, "what is focus" was
    taken for a fragment and the 4B rewrite read the word as a noun — "What is
    the focus of the CM Elevate Legacy scheme in FY 2024-25?" — so the user got
    CM Elevate material instead of the which-Focus question."""
    return _is_ambiguous_focus(question) and not _FOCUS_AS_NOUN.search(question or "")


# A time filter inside a rewritten how-the-scheme-works question: "…in FY
# 2024-25", "for 2024-25", "across all financial years".
_INHERITED_TIME_RE = re.compile(
    r"\s*,?\s*\b(?:in|for|during|of|from)\s+(?:the\s+)?(?:fy|financial\s+year)\s*\d{4}\s*[-–/]\s*\d{2,4}\b|"
    r"\s*,?\s*\b(?:in|for|during)\s+\d{4}\s*[-–/]\s*\d{2,4}\b|"
    r"\s*,?\s*\b(?:across|for|over)\s+all\s+(?:the\s+)?(?:financial\s+)?years(?:\s+combined)?\b",
    re.IGNORECASE)


_ANY_YEAR_PHRASE_RE = re.compile(
    r"\s*,?\s*\b(?:(?:in|for|during|of|from)\s+)?(?:the\s+)?(?:(?:fy|financial\s+year)\s*)?"
    r"(?:19|20)\d\d(?:\s*[-–/]\s*\d{2,4})?\b", re.IGNORECASE)


def _followup_thread_state(question: str, prev: "object",
                           state: "ConversationState | None") -> "ConversationState | None":
    """The structured state a follow-up continues. Normally the session's. But
    right after a KNOWLEDGE answer about ANOTHER scheme, the session's state
    still holds the earlier DATA thread (a digression does not overwrite it),
    and a follow-up that names nothing continues the antecedent instead:
    after "what is Focus Plus", "give me beneficiaries" is Focus Plus, and the
    earlier CM Elevate Legacy FY 2024-25 must not ride along (live 2026-09-29:
    "…for Focus Plus in FY 2024-25", a year Focus Plus does not hold). A
    follow-up whose own words pick the earlier scheme ("and person-days?" is
    MGNREGA vocabulary) still continues that thread with its filters."""
    if state is None or prev is None or getattr(prev, "route", None) != "knowledge":
        return state
    kschemes = list(getattr(prev, "schemes", None) or [])
    if len(kschemes) != 1 or kschemes[0] == state.scheme:
        return state
    own = set(_named_schemes(question)) | set(_infer_scheme_from_terms(question) or [])
    if state.scheme and state.scheme in own:
        return state
    return ConversationState(scheme=kschemes[0])


def _all_years_rewrite(prev_question: str) -> "str | None":
    """"and all of them combined?" after single-year turns: the previous
    question with its year removed, over all financial years. Deterministic —
    the model rewrite put the last year back (live 2026-09-29), and the
    cleared-field check then left a fragment with no measure in it."""
    if not prev_question or not context_policy.extract_year_keys(prev_question):
        return None
    out = _ANY_YEAR_PHRASE_RE.sub("", prev_question)
    out = re.sub(r"\s+([?.!,])", r"\1", re.sub(r"\s{2,}", " ", out)).strip().rstrip(" ?.,")
    return f"{out} across all financial years?" if out else None


def _drop_inherited_place(question: str, typed: str) -> str:
    """The rewritten KNOWLEDGE question without a district / block the user
    never typed. Live 2026-09-29: "who is eligible?" and "what documents are
    needed?" after an East Khasi Hills figure became "…for PMAY-G houses in
    East Khasi Hills?", and the answers said the documents are "not listed
    for East Khasi Hills" — scheme rules are not per district."""
    typed_places = named_places(typed or "")
    out = question or ""
    for place in named_places(out):
        if place in typed_places:
            continue
        out = re.sub(rf"\s*,?\s*\b(?:in|for|of|within|under)\s+(?:the\s+)?{re.escape(place)}"
                     r"(?:\s+(?:district|block))?\b", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\s+([?.!,])", r"\1", re.sub(r"\s{2,}", " ", out)).strip()
    return out or question


def _data_thread_antecedent(question: str, prev: "object", session: "Session | None",
                            plan: "context_policy.MergePlan | None") -> "object | None":
    """The last DATA turn, when a follow-up that only changes a place or a year
    comes right after KNOWLEDGE answers about the same scheme. Scheme rules do
    not vary by district or year, so "and in West Garo Hills?" after "who is
    eligible?" / "what documents are needed?" continues the earlier figure
    (PMAY-G completed houses, FY 2023-24), not the documents question (live
    2026-09-29, the KI-033 family). None when the follow-up asks about how the
    scheme works itself, or no such DATA turn is within the last 4 turns."""
    if session is None or plan is None or getattr(prev, "route", None) != "knowledge":
        return None
    # The follow-up's own words pick ANOTHER scheme than the knowledge aside
    # ("and person-days?" = MGNREGA after "who is eligible for PMAY-G?"): its
    # antecedent is that scheme's last DATA turn, whatever kind of change it is.
    own = set(_named_schemes(question)) | set(_infer_scheme_from_terms(question) or [])
    kscheme = (getattr(prev, "schemes", None) or [None])[0]
    if own and kscheme not in own:
        for t in reversed(list(session.turns or [])[-4:]):
            if getattr(t, "route", None) == "data" and own & set(t.schemes or []):
                return t
        return None
    if plan.kind not in (context_policy.GEOGRAPHY_CHANGE, context_policy.TIME_CHANGE):
        return None
    if _KNOWLEDGE_HINTS.search(question or "") or context_policy._SCHEME_TOPIC_RX.search(
            re.sub(r"\b(?:how|why|more)\b", " ", question or "", flags=re.IGNORECASE)):
        return None
    scheme = (getattr(prev, "schemes", None) or [None])[0]
    for t in reversed(list(session.turns or [])[-4:]):
        if getattr(t, "route", None) == "data":
            return t if (not scheme or scheme in (t.schemes or [])) else None
    return None


def _drop_inherited_time(question: str, typed: str) -> str:
    """The rewritten KNOWLEDGE question without a year the user never typed.
    The follow-up rewrite gets the previous DATA turn's filters, so "what is
    focus" after an FY 2024-25 figure came back "…in FY 2024-25?", and the
    reference docs (which are not per year) then had "no such detail"
    (reported 2026-09-29). A year the user typed is kept."""
    if context_policy.extract_year_keys(typed or "") or _ALL_YEARS_CUE.search(typed or ""):
        return question
    out = _INHERITED_TIME_RE.sub("", question or "")
    out = re.sub(r"\s+([?.!,])", r"\1", re.sub(r"\s{2,}", " ", out)).strip()
    return out or question


def _focus_ambiguity_clarification(question: str) -> "ClarificationNeeded":
    stem = question.strip().rstrip(" ?.")
    # Replace the bare "Focus" in place rather than appending, so the resumed
    # question reads naturally and re-resolves cleanly on the next turn
    # ("total focus disbursement" -> "total Focus Legacy disbursement").
    def _swap(name: str) -> str:
        swapped = _BARE_FOCUS_WORD.sub(name, stem, count=1)
        return swapped if swapped != stem else f"{stem} for {name}"

    return ClarificationNeeded(
        "Two different schemes are called Focus, and they hold different things — "
        "Focus Legacy pays PRODUCER GROUPS (one payment per group, Rs 5,000 per "
        "member), while Focus Plus pays INDIVIDUAL farmers directly. Which one do "
        "you mean?",
        options=[
            {"label": "Focus Legacy (producer group payments)",
             "question": _swap("Focus Legacy")},
            {"label": "Focus Plus (individual farmer cash benefit)",
             "question": _swap("Focus Plus")},
        ],
        rule="focus-scheme-ambiguous",
    )


# Pauses whose answer is a SCHEME, not a scope fragment. The router remembers
# them (session.pending_scope_rule/options) so that a TYPED answer resumes the
# paused question: "Show beneficiaries" -> "Which scheme?" -> "Focus Plus" used
# to go through as the brand-new question "Focus Plus", so the original ask was
# lost unless the user clicked a chip.
SCHEME_PAUSE_RULES = ("scheme-not-specified", "focus-scheme-ambiguous", "bank-scheme-not-specified")
_SCHEME_REPLY_FILLER = re.compile(
    r"\b(?:the|for|under|in|of|scheme|schemes|data|only|please|pls|i\s+mean|it'?s|its|"
    r"ok|okay|yes|one)\b|[,.!?:;]",
    re.IGNORECASE,
)
_FOCUS_SIDE_WORD = {"plus": "Focus Plus", "legacy": "Focus Legacy"}

# Pauses whose free-text reply is a scope FRAGMENT, merged into the paused
# question ("West Garo Hills 2023-24"). The router remembers them without
# options; _run_pipeline step 0a merges.
SCOPE_MERGE_RULES = ("scope-not-specified", "year-not-specified", "entity-ambiguous",
                     "ranking-count-not-specified")


# ── A typed reply to ANY other pause that offered chips ─────────────────────
# Reported 2026-09-29: after "give me for pmay" paused with PMAY-G's measures
# (swap-measure-unavailable), a typed "houses sanctioned" continued the LAST
# ANSWERED turn, MGNREGA. Only the scope and scheme pauses were remembered;
# every other pause with chips (the measure gap, year out of range, tranche,
# region, AC part, Sericulture spelling, CM sub-scheme group, stated amount,
# village-or-outside, scheme comparison, ...) forgot its question, so a typed
# answer was read against the previous turn and a chip was the only way on.
#
# Now the router remembers every pause that offers options, and a typed reply
# is matched to ONE option: its label's words ("houses sanctioned", "2022-23",
# "spinning", "all years"), an ordinal ("the second one", "2"), a bare "yes"
# when there is only one option, or the chip's own question. Deterministic; an
# ambiguous or unmatched reply picks nothing (_paused_thread_antecedent then
# decides which thread it continues).
_OPTION_FILLER = frozenset(
    "the a an of for in under on at please pls ok okay yes yeah yep sure one only i want "
    "need show give me us tell get what about how many much was were is are total number "
    "fy go with let lets do it that just option choice can you would like to and by "
    "which this those these them then".split())
_ORDINAL_WORDS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2,
                  "fourth": 3, "4th": 3, "fifth": 4, "5th": 4}
_ORDINAL_REPLY = re.compile(
    r"^\s*(?:(?:the|option|choice|number|no\.?|#)\s*)?"
    r"(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|last|[1-9])"
    r"(?:\s+(?:one|option|choice))?\s*(?:please|pls)?\s*[.!?]*\s*$", re.IGNORECASE)
_BARE_YES = re.compile(r"^\s*(?:yes|yeah|yep|sure|ok(?:ay)?|go\s+ahead|do\s+it|please)"
                       r"(?:\s*,?\s*(?:please|pls|go\s+ahead|do\s+it))?\s*[.!]*\s*$", re.IGNORECASE)
_OPTION_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _option_tokens(text: str) -> "set[str]":
    """Content words of a label / reply / chip question, scheme names removed,
    amounts without their ₹ and commas, plurals folded ("houses" = "house")."""
    t = text or ""
    for rx in _SCHEME_NAME_PATTERN.values():
        t = rx.sub(" ", t)
    t = re.sub(r"(?<=\d),(?=\d{2,3}\b)", "", t.lower().replace("₹", " "))
    out = set()
    for w in _OPTION_TOKEN.findall(t):
        if w in _OPTION_FILLER:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def _resume_option_pause(reply: str, options: "list | None") -> "str | None":
    """The chip question a typed reply to a remembered option pause stands for,
    or None (the reply then goes on as a message of its own)."""
    opts = [o for o in (options or []) if isinstance(o, dict) and o.get("question")]
    r = (reply or "").strip()
    if not opts or not r or len(r.split()) > 12:
        return None
    for o in opts:  # a clicked chip sends its own question
        if r.rstrip(" ?.").lower() == str(o["question"]).rstrip(" ?.").lower():
            return o["question"]
    m = _ORDINAL_REPLY.match(r)
    if m:
        w = m.group(1).lower()
        i = len(opts) - 1 if w == "last" else (_ORDINAL_WORDS.get(w, int(w) - 1 if w.isdigit() else -1))
        return opts[i]["question"] if 0 <= i < len(opts) else None
    if _BARE_YES.match(r):
        return opts[0]["question"] if len(opts) == 1 else None
    reply_schemes = _exact_schemes(r)
    R = _option_tokens(r)
    if not R:
        return None
    strong, weak_label, weak_question = [], [], []
    for o in opts:
        # A reply that names a scheme picks only an option about that scheme.
        if reply_schemes and not reply_schemes <= (_exact_schemes(o["question"])
                                                   | _exact_schemes(str(o.get("label", "")))):
            continue
        L, Q = _option_tokens(str(o.get("label", ""))), _option_tokens(o["question"])
        if L and L <= R and R <= (L | Q):
            strong.append(o)
        if R <= L:
            weak_label.append(o)
        if R <= Q:
            weak_question.append(o)
    for picked in (strong, weak_label, weak_question):
        if len(picked) == 1:
            return picked[0]["question"]
        if len(picked) > 1:
            return None
    return None


def _paused_thread_antecedent(reply: str, paused_question: str, prev: "object") -> "object | None":
    """A stand-in previous turn for a typed reply to an option pause that
    picked no chip ("houses completed in West Garo Hills" after the PMAY-G
    measure pause), or None to keep `prev`. The paused question is about
    another scheme than the last answered turn, and the reply names none of
    its own: the reply continues the PAUSED question, not the answered one."""
    schemes = _exact_schemes(paused_question)
    if len(schemes) != 1 or _exact_schemes(reply):
        return None
    target = next(iter(schemes))
    if prev is not None and target in (getattr(prev, "schemes", None) or []):
        return None
    # Its own words pick another scheme ("person-days in EKH" is MGNREGA
    # vocabulary): not a reply to this pause.
    own = set(_named_schemes(reply)) | set(_infer_scheme_from_terms(reply) or [])
    if own and target not in own:
        return None
    from app.session_store import Turn
    return Turn(question=paused_question, raw_question=paused_question, route="data",
                schemes=[target], resolved_entities={}, answer="")


def _resume_scheme_pause(reply: str, options: "list | None") -> "str | None":
    """The chip question a typed reply to a scheme pause stands for, or None.

    Returns something only when the reply is just ONE offered scheme's name,
    optionally with filler ("Focus Plus", "focus plus please", and "legacy"
    for the which-Focus pause). The result is then the exact question that
    scheme's chip would have sent, so a typed pick and a clicked pick run the
    same way downstream. Anything else returns None and the reply goes through
    untouched, exactly as before these pauses were remembered: a fresh
    question, several schemes, "thanks"."""
    if not reply or not options:
        return None
    r = reply.strip()
    if len(r.split()) > 6:
        return None
    offered = {str(o.get("label", "")).split(" (")[0].strip(): o.get("question")
               for o in options if isinstance(o, dict)}
    named = sorted(_exact_schemes(r))
    residue = r
    for s in named:
        residue = _SCHEME_NAME_PATTERN[s].sub(" ", residue)
    residue = _SCHEME_REPLY_FILLER.sub(" ", residue).strip().lower()
    if not named and residue in _FOCUS_SIDE_WORD and set(offered) == set(_FOCUS_SIDE_WORD.values()):
        named, residue = [_FOCUS_SIDE_WORD[residue]], ""
    if residue or len(named) != 1:
        return None
    return offered.get(named[0])


# ── CM Elevate: "the vehicle scheme?" — which of several real sub-schemes? ──
# The phrase used to name a scheme-FAMILY the question named in the singular
# (resolve_cm_scheme_group_ambiguity's match) — swapped out for each candidate's
# real scheme_name so the resumed question resolves cleanly via resolve_cm_scheme's
# own exact-alias stage, same trick _scheme_option_question uses for the top-level
# 4-scheme pause above.
_CM_GROUP_PHRASE = {
    "vehicles": re.compile(r"\bvehicle scheme(?!s)\b", re.IGNORECASE),
    "tourism": re.compile(r"\btourism scheme(?!s)\b", re.IGNORECASE),
    "PRIME": re.compile(r"\bprime scheme(?!s)\b", re.IGNORECASE),
    "livestock": re.compile(r"\blivestock scheme(?!s)\b", re.IGNORECASE),
    "enterprise": re.compile(r"\benterprise scheme(?!s)\b", re.IGNORECASE),
}


def _cm_scheme_group_clarification(question: str, group: dict) -> "ClarificationNeeded":
    stem = question.strip().rstrip(" ?.")
    schemes = group["schemes"]
    phrase_re = _CM_GROUP_PHRASE.get(group["group"])
    options = []
    for s in schemes:
        new_q = phrase_re.sub(s, stem, count=1) if phrase_re else None
        options.append({"label": s, "question": new_q if new_q and new_q != stem else f"{stem} — {s}"})
    all_list = ", ".join(schemes[:-1]) + " and " + schemes[-1]
    return ClarificationNeeded(
        f"“{group['group']}” covers {len(schemes)} separate CM Elevate schemes — "
        f"{all_list} — and they are not interchangeable. Which one did you mean?",
        options=options,
        rule="cm-scheme-group-ambiguous",
    )


# ── CM Elevate Legacy: "Sericulture" — spinning, weaving, or both? ──────────
# Sericulture is TWO stored schemes whose names differ by one space before the
# bracket, and an exact match on the wrong spelling returns zero rows silently
# (cmelevatelegacy_classification_rules.yaml sericulture_spelling_ambiguous; the
# prompt-layer bank's clarification K03). A bare "sericulture" is asked, never
# guessed; "both" is taken at its word.
_CM_LEGACY_SERICULTURE = ("Meghalaya Sericulture & Weaving Scheme (spinning)",
                          "Meghalaya Sericulture & Weaving Scheme(weaving)")
_SERICULTURE_WORD = re.compile(r"\bsericulture\b|\bsilk\b", re.IGNORECASE)
_SERICULTURE_SIDE = re.compile(r"\bspinning\b|\bweaving\b|\bhandloom\b", re.IGNORECASE)
_SERICULTURE_BOTH = re.compile(
    r"\bboth\b|\ball\s+(?:the\s+)?sericulture\b|\bsericulture\s+schemes\b|"
    r"\b(?:together|combined|separately)\b",
    re.IGNORECASE)


def _cm_legacy_sericulture_choice(question: str) -> "list[str] | str | None":
    """Both Sericulture literals when the question asks for both, "ask" when it
    names Sericulture without saying which, else None (resolve normally)."""
    q = question or ""
    if not _SERICULTURE_WORD.search(q) or _SERICULTURE_SIDE.search(q):
        return None
    if _SERICULTURE_BOTH.search(q):
        return list(_CM_LEGACY_SERICULTURE)
    return "ask"


def _cm_legacy_sericulture_clarification(question: str) -> "ClarificationNeeded":
    stem = question.strip().rstrip(" ?.")

    def _swap(repl: str) -> str:
        new = _SERICULTURE_WORD.sub(repl, stem, count=1)
        return new if new != stem else f"{stem} — {repl}"

    return ClarificationNeeded(
        "Sericulture is recorded as two separate schemes — spinning and weaving. "
        "Which did you mean, or both?",
        options=[
            {"label": "Spinning", "question": _swap("Sericulture spinning")},
            {"label": "Weaving", "question": _swap("Sericulture weaving")},
            {"label": "Both, shown separately",
             "question": _swap("both Sericulture schemes (spinning and weaving)")},
        ],
        rule="sericulture-spelling-ambiguous",
    )


# ── CM Elevate Legacy: questions the data cannot answer ─────────────────────
# Each pattern is a refusal class from the prompt-layer bank (refusal_code),
# worded by the bank's own reviewed text via annotations.refusal_reason. Checked
# deterministically before SQL generation: the generator can only emit a SELECT,
# so left to it these came back as an improvised figure or a bare "not
# available" with no reason. The patterns are deliberately narrow — the bank's
# re-verification found four over-broad refusal triggers (a relative "who", the
# word "individual", "overview", "focus") and each is avoided here.
_CM_LEGACY_NOT_HELD = (
    # SANCTION_RATE, NOT_SANCTIONED and CONSTITUENCY used to be refused here.
    # All three are answerable from the view (2026-09-25 use-case QA): the rate is
    # COUNT(sanctioned_amount) / COUNT(*) with a caveat about the separate
    # applications dataset, "not sanctioned" is the records with no sanctioned
    # amount, and constituency comes through the dim_geography join. The few-shot
    # bank (X01 / X02 / X07 / X08) teaches the SQL instead.
    ("APPLICANT_NAME", re.compile(
        r"^\W*(?:who|whose)\b|"
        r"\bwho\s+(?:got|received|has|had|took)\s+the\s+(?:most|maximum|highest|largest|biggest)\b|"
        r"\bnames?\s+of\s+(?:the\s+)?(?:\w+\s+)?(?:beneficiar\w*|applicants?|recipients?|"
        r"people|persons?|entrepreneurs?)\b|"
        r"\b(?:beneficiary|applicant)\s+names?\b|\bname\s+list\b",
        re.IGNORECASE)),
    ("MONTHLY", re.compile(
        r"\bmonth[\s-]?wise\b|\bmonthly\b|\bby\s+month\b|\bper\s+month\b|\beach\s+month\b|"
        r"\bquarter(?:ly|[\s-]?wise)?\b",
        re.IGNORECASE)),
    ("APPLICANT_TYPE", re.compile(
        r"\bindividuals?\s+(?:vs\.?|versus|or|and)\s+groups?\b|\bgroup\s+applicants?\b|"
        r"\bshgs?\b|\bself[\s-]?help\s+groups?\b|\b(?:un)?registered\s+groups?\b|"
        r"\bapplicant\s+type\b",
        re.IGNORECASE)),
    ("REPAYMENT", re.compile(r"\brepa(?:id|y|ying|yments?)\b|\bloan\s+recovery\b|"
                             r"\bdefault(?:ed|ers?)\b", re.IGNORECASE)),
    ("DEMOGRAPHICS", re.compile(
        r"\bwom[ae]n\b|\bfemale\b|\bmale\b|\bgender\b|\bcaste\b|\bsc\s*/?\s*st\b|"
        r"\bscheduled\s+(?:caste|tribe)s?\b|\bage[\s-]?(?:group|wise)\b|\bminorit(?:y|ies)\b",
        re.IGNORECASE)),
    ("BANK_SANCTIONED_SHARE", re.compile(
        r"\bbank[\s-]?sanctioned\b|\bbank(?:'s)?\s+(?:share|contribution)\b|"
        r"\bdid\s+the\s+bank\s+contribute\b",
        re.IGNORECASE)),
    ("TARGETS", re.compile(r"\btargets?\b|\bbudget(?:ed|s)?\b|\ballocations?\b",
                           re.IGNORECASE)),
    ("BUSINESS_OUTCOME", re.compile(
        r"\bjobs?\s+(?:were\s+)?(?:created|generated)\b|\bemployment\s+(?:created|generated)\b|"
        r"\bturnover\b|\bventures?\s+surviv\w*|\bbusiness\s+outcomes?\b",
        re.IGNORECASE)),
    ("LINK_APPLICATIONS", re.compile(
        r"\bapplications?\s+behind\b|\blink\w*\s+(?:to|with)\s+(?:the\s+)?applications?\b|"
        r"\bmatch\w*\s+(?:to|with)\s+(?:the\s+)?applications?\b",
        re.IGNORECASE)),
)


def _cm_legacy_not_held(question: str) -> "ClarificationNeeded | None":
    """A not-held explanation for a CM Elevate Legacy question the data cannot
    answer, or None when it can be answered."""
    for code, rx in _CM_LEGACY_NOT_HELD:
        if not rx.search(question or ""):
            continue
        reason = annotations.refusal_reason("CM Elevate Legacy", code)
        if not reason:
            continue
        main, _, offer = reason.partition("Offer instead:")
        text = main.strip()
        if offer.strip():
            text += " What I can offer instead: " + offer.strip()
        logger.info("CM Elevate Legacy not-held (%s): %r", code, question)
        return ClarificationNeeded(text, rule="column-not-held")
    return None


# ── CM Elevate Legacy: caveats that travel with the numbers ─────────────────
# The prompt layer routes its unit / year / village / refusal caveats by CODE,
# not by asking the model to remember them — each is triggered here by what the
# executed SQL actually did, so a caveat appears exactly when its number does.
_CM_LEGACY_MONEY_RE = re.compile(
    r"\b(?:total_disbursement|total_subsidy_disbursement|total_loan_disbursement|"
    r"sanctioned_amount)\b", re.IGNORECASE)
_CM_LEGACY_YEAR_FILTER_RE = re.compile(
    r"\bfinancial_year(?:_short)?\s*(?:=|IN)\s*\(?\s*'|\byear_key\s*(?:=|IN)\s*\(?\s*\d",
    re.IGNORECASE)
_CM_LEGACY_YEAR_GROUP_RE = re.compile(r"\bGROUP\s+BY\b[^;]*\bfinancial_year", re.IGNORECASE)


def _cm_legacy_answer_notes(sql: str, rows: list[dict]) -> list[str]:
    s = sql or ""
    notes: list[str] = []
    cols = {k for r in (rows or [])[:1] if isinstance(r, dict) for k in r}
    if "1e7" in s or any(c.endswith("_cr") for c in cols):
        notes.append(
            "Columns ending in _cr are already in ₹ crore (divided by 1e7); columns ending "
            "in _rupees are rupees. Write money as ₹<value> crore / ₹<value>, copying the "
            "digits exactly.")
    if re.search(r"\btotal_disbursement\b", s, re.IGNORECASE):
        # Only point at a subsidy / loan split the result actually carries: told
        # "give all three" with no such columns, the composer invented one ("This
        # total includes ₹0.58 crore as subsidy and …"), was rejected twice and the
        # officer got the raw fallback "0.58 total disbursed cr." (Mawkynrew,
        # all-constituencies run 2026-09-29).
        if any(re.search(r"subsid|loan", c, re.IGNORECASE) for c in cols):
            notes.append(
                "Total disbursed means subsidy and loan together. The subsidy and loan columns "
                "are in the result, so give all three; sanctioned is the amount approved, not "
                "the amount paid.")
        else:
            notes.append(
                "Total disbursed means subsidy and loan together. State the total only: this query "
                "did not select the split, so do not mention subsidy or loan at all — no figure, no "
                "estimate, and no remark that a split is unavailable.")
    if _CM_LEGACY_MONEY_RE.search(s) and "desanctioned_reason_raw" not in s:
        notes.append(
            "Desanctioned records (Refused / Duplicate) are included in these money totals, "
            "as they are in the source file's own totals.")
    if _CM_LEGACY_YEAR_FILTER_RE.search(s):
        notes.append(
            "395 records (both Sericulture schemes) carry no financial year, so they are "
            "outside any single-year figure — say so in one short clause.")
    if _CM_LEGACY_YEAR_GROUP_RE.search(s):
        notes.append(
            "The '(no financial year)' row is the 395 Sericulture records, which carry no "
            "year label — describe it that way, not as missing or erroneous data. Only two "
            "financial years exist, so describe this as a comparison, not a trend.")
    if len(rows or []) > 1 and any(c.endswith("_cr") for c in cols):
        # Seen live (TC-36 district summary, 2026-09-25): the composer added up
        # the per-district _cr figures and stated ₹81.09 / ₹29.24 / ₹51.88 crore
        # against the true ₹81.10 / ₹29.23 / ₹51.87 — each row is rounded to 2
        # decimals, so a sum of them drifts. Counts are exact and may be summed.
        def _is_total(r: dict) -> bool:
            return any(isinstance(v, str) and v.upper().startswith("ALL ") for v in r.values())
        has_total_row = any(_is_total(r) for r in rows)
        # Name the extremes here rather than leave them to the composer: with a
        # trailing ALL row it took the row above it as "the lowest" (TC-36,
        # North Garo Hills ₹2.60 Cr instead of East Jaintia Hills ₹2.36 Cr).
        body = [r for r in rows if not _is_total(r)]
        metric = "total_disbursed_cr" if "total_disbursed_cr" in cols else \
            next((c for c in cols if c.endswith("_cr")), None)
        ranked = [r for r in body if _as_number(r.get(metric)) is not None]
        if metric and len(ranked) >= 2:
            def _label(r: dict) -> str:
                return " / ".join(str(v) for v in r.values()
                                  if isinstance(v, str)) or "(unlabelled)"
            hi = max(ranked, key=lambda r: _as_number(r[metric]))
            lo = min(ranked, key=lambda r: _as_number(r[metric]))
            notes.append(
                f"By {metric}: highest is {_label(hi)} ({_fmt_num(_as_number(hi[metric]))}), "
                f"lowest is {_label(lo)} ({_fmt_num(_as_number(lo[metric]))}). Use these exactly "
                "when naming the top or bottom row.")
        notes.append(
            "Each _cr value is rounded to 2 decimals per row, so NEVER add them across rows "
            "to state a combined money total — the sum drifts from the true figure. "
            + (f"The row labelled 'ALL …' holds the exact totals: quote statewide figures from "
               f"that row only. It is a TOTAL, not an area — the breakdown has exactly "
               f"{len(rows) - 1} rows besides it, so say {len(rows) - 1}, never {len(rows)}. "
               "Also name the top and bottom rows of the breakdown. "
               if has_total_row else
               "Describe the rows (highest, lowest, range); a combined money total may "
               "only be quoted from an 'Exact combined totals' note. ")
            + "Record counts are exact and may be summed.")
    if "sanctioned_records" in cols:
        notes.append(
            "sanctioned_records is the number of SANCTIONED cases (records carrying a "
            "sanctioned amount) — report it under that name, separately from records where "
            "both appear; do not call records 'sanctioned'. It is a COUNT: when listing "
            "rows, give every row's sanctioned_records value, never its _cr money value "
            "in place of the count (TC-19: Motorcaravan 1 record was written as '0.50 crore').")
    if "sanctioned_pct" in cols:
        notes.append(
            "sanctioned_pct is the share of records in THIS sanction-and-disbursement dataset "
            "that carry a sanctioned amount. Give the percentage and the two counts, then say "
            "in one clause that applications which never reached sanction sit in the separate "
            "CM Elevate applications dataset, which cannot be linked to this one.")
    if "not_sanctioned_records" in cols:
        notes.append(
            "not_sanctioned_records are records with no sanctioned amount. Desanctioned "
            "(Refused / Duplicate) counts are sanctions withdrawn later — label them that way, "
            "never add them to the not-sanctioned figure.")
    if re.search(r"\bac_name\b", s, re.IGNORECASE):
        notes.append(
            "The constituency comes from the geography registry through each record's "
            "village; records with no village carry no constituency. Say this in one short "
            "clause, and call it the assembly constituency, not a block.")
    if re.search(r"entity_type\s*<>\s*'Unresolved'", s, re.IGNORECASE):
        notes.append(
            "Records that could not be matched to a village are left out of village "
            "figures (they still count in district and block totals); villages are counted "
            "by LGD code.")
    if re.search(r"\b(?:desanctioned_reason_raw|refused_flag_raw|refused_reason_text)\b",
                 s, re.IGNORECASE):
        notes.append(
            "The desanction reason, the refusal flag and the written reason disagree — name "
            "the field each figure comes from and present none of them as the "
            "authoritative refusal count.")
    return notes


_CR_SUM_RE = re.compile(
    r"ROUND\(\s*SUM\(\s*([\w.]+)\s*\)\s*/\s*1e7\s*,\s*2\s*\)\s+AS\s+(\w+_cr)\b", re.IGNORECASE)
_FROM_TO_GROUP_RE = re.compile(r"\bFROM\b(.*?)\bGROUP\s+BY\b", re.IGNORECASE | re.DOTALL)


async def _cm_legacy_exact_totals(sql: str, rows: list[dict]) -> list[str]:
    """Exact combined money totals for a multi-row _cr breakdown with no ALL row.

    Seen live (TC-27, 2026-09-25): the composer added up 12 rounded per-district
    figures and wrote "sums to ₹82.89 crore" against the true ₹82.90 — the prose
    rule not to sum rounded values did not hold. The total is re-queried from the
    same FROM/WHERE with the GROUP BY removed, so the composer can quote it exactly.
    Only the plain single-SELECT shape is handled; anything else returns no note."""
    s = sql or ""
    if len(rows or []) < 2 or re.search(r"\bROLLUP\b|\bWITH\b|\bHAVING\b", s, re.IGNORECASE):
        return []
    if any(isinstance(v, str) and v.upper().startswith("ALL ") for r in rows for v in r.values()):
        return []
    sums = _CR_SUM_RE.findall(s)
    m = _FROM_TO_GROUP_RE.search(s)
    if not sums or not m or re.search(r"\bSELECT\b", m.group(1), re.IGNORECASE):
        return []
    select = ", ".join(f"ROUND(SUM({expr}) / 1e7, 2) AS {alias}" for expr, alias in sums)
    try:
        got = await run_readonly(f"SELECT {select} FROM {m.group(1).strip()}")
    except Exception:  # noqa: BLE001
        logger.warning("CM Legacy exact-total query failed — no total note", exc_info=True)
        return []
    if not got:
        return []
    figures = ", ".join(f"{k} = {_fmt_num(_as_number(v))}" for k, v in got[0].items()
                        if _as_number(v) is not None)
    if not figures:
        return []
    return [f"Exact combined totals across every group (queried separately, not a sum of "
            f"the rounded rows): {figures}. If you state a combined money total, use "
            f"exactly these figures."]


# ── CM Elevate Legacy answer guarantees (use-case re-test 2026-09-29, KI-167) ─
# TC-34 "applications mapped to each village in Tikrikilla block": the SQL and
# table were right (42 villages, 89 records) but the composer wrote "20 villages
# each have 1 record" against a true 24, on 5 of 5 runs. It sees only the first
# 40 rows, and the faithfulness check lets whole numbers under 100 through as
# prose, so a wrong derived count cannot be caught there. Such counts are
# recomputed from EVERY row here and corrected in place. The answer also never
# said that 6 Tikrikilla records have no village code; that is stated from a
# re-query of the same scope.
_NUM_WORDS = {"no": 0, "zero": 0, "one": 1, "a single": 1, "single": 1, "two": 2, "three": 3, "four": 4,
              "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
              "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "twenty": 20}
_NUMW = r"\d[\d,]*|" + "|".join(sorted((w for w in _NUM_WORDS if w != "no"), key=len, reverse=True))
_CML_EACH_HAVE_RE = re.compile(
    rf"\b(?P<n>{_NUMW})\s+(?:other\s+|more\s+|of\s+the\s+)?(?:distinct\s+)?"
    r"(?P<noun>villages|blocks|districts|schemes|constituencies|wards)\s+"
    r"(?:each\s+)?(?:have|has|had|hold|holds|held|record|recorded|show|shows|with|carry|carries)\s+"
    rf"(?:each\s+)?(?:only\s+|just\s+|exactly\s+)?(?P<v>{_NUMW})\s+"
    r"(?:record|application|applicant|case|beneficiar(?:y|ies)|entr(?:y|ies))s?\b",
    re.IGNORECASE)


def _cml_count_column(rows: list[dict]) -> "str | None":
    """The one count column of a breakdown, or None when there is not exactly one."""
    cols = [k for k, v in (rows[0].items() if rows else ())
            if _as_number(v) is not None and not _NONSTAT_COL.search(k)
            and not k.endswith(("_cr", "_rupees", "_pct", "_amount"))]
    return cols[0] if len(cols) == 1 else None


def _cml_fix_each_have_counts(answer: str, rows: list[dict]) -> str:
    col = _cml_count_column(rows or [])
    if not answer or not col or len(rows) < 2:
        return answer
    body = [r for r in rows if not any(isinstance(v, str) and v.upper().startswith(("UNRESOLVED", "ALL "))
                                       for v in r.values())]

    def _num(tok: str) -> "int | None":
        t = tok.strip().lower().replace(",", "")
        return int(t) if t.isdigit() else _NUM_WORDS.get(t)

    def _fix(m: "re.Match[str]") -> str:
        n, v = _num(m.group("n")), _num(m.group("v"))
        if n is None or v is None:
            return m.group(0)
        true = sum(1 for r in body if _as_number(r.get(col)) == v)
        if true == n:
            return m.group(0)
        logger.warning("CM Elevate Legacy: answer said %d %s have %d; the rows give %d — corrected (KI-167)",
                       n, m.group("noun"), v, true)
        return m.group(0)[:m.start("n") - m.start()] + f"{true:,}" + m.group(0)[m.end("n") - m.start():]
    return _CML_EACH_HAVE_RE.sub(_fix, answer)


_CML_VILLAGE_GROUP_RE = re.compile(r"\bGROUP\s+BY\b[^;]*\b(?:lgd_village_name|village_code)\b", re.IGNORECASE)
_CML_UNRESOLVED_NE_RE = re.compile(r"(?:\b\w+\.)?entity_type\s*<>\s*'Unresolved'", re.IGNORECASE)
_CML_NO_VILLAGE_SAID = re.compile(
    r"no\s+village|without\s+a\s+village|not\s+(?:mapped|matched)\s+to\s+(?:a|any)\s+village|"
    r"unmapped|could\s+not\s+be\s+(?:matched|mapped)|not\s+yet\s+mapped|no\s+(?:lgd\s+)?village\s+code",
    re.IGNORECASE)


async def _cml_unmapped_in_scope(sql: str) -> int:
    """Records the village breakdown left out (entity_type = 'Unresolved') in the
    SAME place / year scope, re-queried from the breakdown's own FROM / WHERE."""
    s = sql or ""
    masked = _mask_sql_literals(s)
    if not _CML_VILLAGE_GROUP_RE.search(masked) or not _CML_UNRESOLVED_NE_RE.search(s) \
            or not _simple_select(masked):
        return 0
    m = _FROM_TO_GROUP_RE.search(s)
    if not m:
        return 0
    scope = _CML_UNRESOLVED_NE_RE.sub(lambda mm: mm.group(0).replace("<>", "="), m.group(1)).strip()
    try:
        got = await run_readonly(f"SELECT COUNT(*) AS n FROM {scope}")
    except Exception:  # noqa: BLE001
        logger.warning("CM Legacy unmapped-records query failed — no note", exc_info=True)
        return 0
    return int(_as_number(got[0].get("n")) or 0) if got else 0


def _cml_inr(v: "int | float") -> str:
    """Rupees in Indian grouping: 150000 -> ₹1,50,000; paise kept only when present."""
    neg, v = v < 0, abs(float(v))
    whole, paise = int(v), round((v - int(v)) * 100)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        s = ",".join(re.findall(r"\d{1,2}(?=(?:\d{2})*$)", head)) + "," + tail
    return ("-" if neg else "") + "₹" + s + (f".{paise:02d}" if paise else "")


# A single money figure that rounds to 0.00 crore: the small-money note makes the
# composer write "under ₹0.01 crore", which is also what it wrote for a TRUE zero
# (UMTHAM, Umling: ₹0.00 disbursed; all-villages run 2026-09-29). The exact
# amount is re-queried and written instead — ₹0 or the rupee figure.
_CML_UNDER_CRORE_RE = re.compile(
    r"(?:under|less\s+than|below)\s+₹\s?0\.01\s+crore(?:\s*\((?:less\s+than|under)\s+₹\s?1\s+lakh\))?",
    re.IGNORECASE)


async def _cml_exact_single_amount(sql: str, rows: list[dict]) -> "tuple[bool, float | None]":
    """(found, exact rupees) for a one-row result whose one _cr cell is under
    0.10 crore — at that size the 2-decimal crore figure hides the amount
    (₹62,500 and ₹1,25,000 both read "₹0.01 crore"). A one-row GROUP BY sums
    the same scope, so the grouping is dropped from the re-query."""
    if len(rows or []) != 1:
        return False, None
    small = [k for k, v in rows[0].items() if k.endswith("_cr")]
    if len(small) != 1 or _as_number(rows[0][small[0]]) is None or _as_number(rows[0][small[0]]) >= 0.10:
        return False, None
    exprs = {alias.lower(): expr for expr, alias in _CR_SUM_RE.findall(sql or "")}
    expr = exprs.get(small[0].lower())
    masked = _mask_sql_literals(sql or "")
    m = re.search(r"\bFROM\b(.*?)(?:\bGROUP\s+BY\b.*|\bORDER\s+BY\b.*|\bLIMIT\s+\d+.*|;)?\s*$", sql or "",
                  re.IGNORECASE | re.DOTALL)
    if not expr or not m or not _simple_select(masked) or re.search(r"\bHAVING\b", masked, re.IGNORECASE):
        return False, None
    scope = m.group(1).strip()
    try:
        got = await run_readonly(f"SELECT SUM({expr}) AS v FROM {scope}")
    except Exception:  # noqa: BLE001
        logger.warning("CM Legacy exact small-amount query failed — answer left as is", exc_info=True)
        return False, None
    return (True, _as_number(got[0].get("v"))) if got else (False, None)


def _cml_fix_place_spelling(answer: str, display: dict, rows: "list[dict] | None" = None) -> str:
    """A near-miss spelling of the place the query filtered on, or of a place
    label in the result rows, replaced by its canonical name. The composer wrote
    "Sellsella block" for SELSELLA on 3 of 3 runs, and "Mawsynrut" for the
    MAWSHYNRUT row of a West Khasi Hills block breakdown (all-villages run
    2026-09-29); the stored names are known exactly. A word that IS another
    known name is never touched."""
    import difflib
    labels = list(dict.fromkeys(
        str(v).strip() for r in (rows or [])[:60] for k, v in r.items()
        if isinstance(v, str) and re.search(r"(?:^|_)(?:block|district|village_name|ac_name)$", k)))
    names = [(k, n.strip()) for k in ("village", "block", "district", "constituency")
             for n in re.split(r"\s+and\s+", str(display.get(k) or "")) if n.strip()] \
        + [("row", n) for n in labels]
    known = {n.lower() for _k, n in names}
    for key, name in names:
        words = re.findall(r"[A-Za-z][A-Za-z'\-]*", name)
        if not words or len(name) < 5:
            continue
        toks = list(re.finditer(r"[A-Za-z][A-Za-z'\-]*", answer or ""))
        # a name ending in punctuation: "TEPORPARA ( UPPER )" written right was
        # matched as the near-miss "Teporpara ( Upper" (letter words only) and
        # "corrected" to "Teporpara ( Upper ) )" (all-villages re-run 2026-10-02)
        trail = re.sub(r"\s+", "", re.search(r"[^A-Za-z]*$", name).group(0))
        bad = set()
        for i in range(len(toks) - len(words) + 1):
            end = toks[i + len(words) - 1].end()
            if trail:
                t = re.match(r"\s*" + r"\s*".join(map(re.escape, trail)), answer[end:])
                end += t.end() if t else 0
            span = answer[toks[i].start():end]
            if span.lower() in known or span[0].lower() != name[0].lower()                     or abs(len(span) - len(name)) > 2:
                continue
            if difflib.SequenceMatcher(None, span.lower(), name.lower()).ratio() >= 0.88:
                bad.add(span)
        for sp in bad:
            # keep the answer's own case style: "MAWSYNRUT" -> "MAWSHYNRUT", "Sellsella" -> "Selsella"
            canon = name.upper() if sp.isupper() else (name.title() if name.isupper() else name)
            logger.warning("CM Elevate Legacy: answer spelled %s %r as %r — corrected", key, name, sp)
            answer = re.sub(rf"(?<![A-Za-z]){re.escape(sp)}(?![A-Za-z])", canon, answer)
    return answer


# A per-group COUNT breakdown whose figures are not beside their names. "How
# many applications under each scheme in Umsning block?" came back as "…Piggery
# holding the highest count at 27. The remaining schemes show 24, 16, 15, 13, 6,
# 3, 1, and 1 records respectively" — nine figures, one name (all-blocks run
# 2026-09-29, 3 blocks). Detection is CM Elevate's (_cme_complete_list, KI-068 /
# KI-073); the list is rebuilt from the rows under the composer's headline.
# Money breakdowns keep their composer text (exact-total and extremes notes).
_CML_DIM_NAME = {"scheme_name": "scheme", "lgd_block": "block", "lgd_district": "district",
                 "loan_entity": "loan entity", "financial_year": "financial year",
                 "financial_year_short": "financial year", "ac_name": "constituency",
                 "lgd_village_name": "village"}
_CML_COUNT_NAME = {"records": "Applications", "applications": "Applications",
                   "sanctioned_records": "Sanctioned applications"}


def _cml_complete_list(question: str, answer: str, rows: list[dict]) -> str:
    if not rows or len(rows) < 2:
        return answer
    nums = _fp_numeric_cols(rows)
    if len(nums) != 1 or not re.search(r"records$|applications$|^n$|count", nums[0]):
        return answer
    if _cme_complete_list(question, answer, rows) == answer:
        return answer
    met = nums[0]
    lab = next(k for k in rows[0] if k != met)
    if lab.startswith("financial_year"):
        return answer   # stored "2024-2025" is written "FY 2024-25" — not a missing row
    def _name(r: dict) -> str:
        return re.sub(r"^Meghalaya\s+", "", _fp_title(r[lab])) if r.get(lab) is not None else "(not recorded)"
    items = [f"{_name(r)} {_fmt_num(_as_number(r.get(met)))}" for r in rows if _as_number(r.get(met)) is not None]
    total = sum(_as_number(r.get(met)) or 0 for r in rows)
    listed = (f"{_CML_COUNT_NAME.get(met, _metric_label(met).capitalize())} by "
              f"{_CML_DIM_NAME.get(lab, lab.replace('_', ' '))} ({len(items)}): " + "; ".join(items)
              + f". Total {_fmt_num(total)}.")
    head = re.split(r"(?<=[.!?])\s+", (answer or "").strip())[0]
    logger.info("CM Elevate Legacy: rebuilt a %s list — figures were not beside their names", lab)
    return listed if not head or _CME_GARBLED.search(head) else f"{head}\n\n{listed}"


# When the composer is rejected twice, compose_response falls back to
# _deterministic_answer, which for one row reads "0.58 total disbursed cr." —
# correct digits, unusable wording. For CM Elevate Legacy that dump is replaced
# by a plain sentence naming the place and each figure with its unit.
_CML_FIGURE_WORDING = (
    (re.compile(r"^sanctioned_records$"), "{n} sanctioned applications"),
    (re.compile(r"^(?:records|applications|n)$"), "{n} applications"),
    (re.compile(r"^not_sanctioned_records$"), "{n} records with no sanctioned amount"),
    (re.compile(r"subsid\w*_cr$"), "₹{cr} crore disbursed as subsidy"),
    (re.compile(r"loan\w*_cr$"), "₹{cr} crore disbursed as loan"),
    (re.compile(r"disburs\w*_cr$"), "₹{cr} crore disbursed"),
    (re.compile(r"sanction\w*_cr$"), "₹{cr} crore sanctioned"),
    (re.compile(r"_pct$"), "{v}%"),
)


def _cml_single_row_sentence(row: dict, display: dict) -> "str | None":
    parts = []
    for k, v in _row_metrics(row):
        tmpl = next((t for rx, t in _CML_FIGURE_WORDING if rx.search(k)), None)
        if tmpl is None:
            return None
        parts.append(tmpl.format(n=_fmt_num(v), cr=f"{float(v):.2f}", v=v))
    if not parts:
        return None
    key = next((k for k in ("village", "block", "constituency", "district") if display.get(k)), None)
    name = str(display[key]) if key else "All of Meghalaya"
    name = name.title() if name.isupper() else name
    place = name + (f" {key}" if key in ("village", "block", "constituency") and key not in name.lower() else "")
    yr = next((str(display[k]) for k in ("year", "financial_year") if display.get(k)), None)
    period = f"in {yr}" if yr else "across all financial years"
    return f"{place}: " + "; ".join(parts) + f" under CM Elevate Legacy {period}."


# A village breakdown (TC-34 shape) is one name column + its code + one count.
# Purakhasia (all-blocks run 2026-09-29): "ranging from 6 records at Rapangpanggiri
# down to 1 record at each of the other 13 villages. The village-level record
# counts are 6, 5, 3, 3, 3, 2, 1, … respectively" — a false claim (8 villages
# have 1), figures with no names, and the block's 30 records never stated.
_CML_EACH_OF_RE = re.compile(
    r"\b(\d+|one|a single)\s+(?:record|application)s?\s+(?:at|in|for)\s+each\s+of\s+the\s+"
    r"(?:(other|remaining)\s+)?(\d+)\s+villages\b", re.IGNORECASE)


def _cml_village_breakdown(answer: str, rows: list[dict], display: dict) -> str:
    if len(rows or []) < 2:
        return answer
    name_col = next((k for k in rows[0] if k.endswith("village_name")), None)
    col = _cml_count_column(rows)
    if not name_col or not col:
        return answer
    body = [r for r in rows if not str(r.get(name_col) or "").upper().startswith("UNRESOLVED")]
    total = sum(_as_number(r.get(col)) or 0 for r in body)
    false_claim = False
    for m in _CML_EACH_OF_RE.finditer(answer or ""):
        v = 1 if not m.group(1).isdigit() else int(m.group(1))
        n = int(m.group(3))
        pool = body[1:] if m.group(2) else body     # "the other N" = all but the top row
        if sum(1 for r in pool if _as_number(r.get(col)) == v) != n or len(pool) != n and m.group(2):
            false_claim = True
    stated = {int(t.replace(",", "")) for t in re.findall(r"\d[\d,]*", answer or "") if t.replace(",", "").isdigit()}
    if not (_CME_GARBLED.search(answer or "") or false_claim) and total in stated:
        return answer
    place = next((str(display[k]) for k in ("block", "district", "constituency") if display.get(k)), "")
    place = place.title() if place.isupper() else place
    key = next((k for k in ("block", "district", "constituency") if display.get(k)), "")
    where = f" in {place}{' ' + key if key and key not in place.lower() else ''}" if place else ""
    head = f"{total:,} applications are mapped to {len(body)} villages{where}."
    if not (_CME_GARBLED.search(answer or "") or false_claim):
        logger.info("CM Elevate Legacy: village breakdown did not state its total — added")
        return f"{head} {answer}"
    shown = body[:40]
    items = "; ".join(f"{_fp_title(r[name_col])} {_fmt_num(_as_number(r.get(col)))}" for r in shown)
    more = f"; …and {len(body) - len(shown)} more in the table" if len(body) > len(shown) else ""
    logger.info("CM Elevate Legacy: village breakdown rebuilt (garbled list or false claim)")
    return f"{head}\n\nApplications by village ({len(body)}): {items}{more}."


async def _cm_legacy_answer_guarantees(question: str, sql: str, rows: list[dict], answer: str,
                                       display: dict) -> str:
    if len(rows or []) == 1 and (answer or "").strip() == _deterministic_answer(rows).strip():
        answer = _cml_single_row_sentence(rows[0], display) or answer
    answer = _cml_complete_list(question, answer, rows)
    answer = _cml_village_breakdown(answer, rows, display)
    answer = _cml_fix_each_have_counts(answer, rows)
    answer = _cml_fix_place_spelling(answer, display, rows)
    found, exact = await _cml_exact_single_amount(sql, rows)
    if found and _CML_UNDER_CRORE_RE.search(answer or ""):
        answer = _CML_UNDER_CRORE_RE.sub(_cml_inr(exact or 0), answer)
        logger.info("CM Elevate Legacy: 'under ₹0.01 crore' replaced by the exact amount %s", exact)
    elif found and exact and _cml_inr(exact) not in answer:
        # "₹0.01 crore" -> "₹0.01 crore (₹1,25,000)": the crore figure stays, the exact one is added
        cr = next(f"{_as_number(v):.2f}" for k, v in rows[0].items() if k.endswith("_cr"))
        answer = re.sub(rf"(₹\s?{re.escape(cr)}\s+crore)(?!\s*\(₹)", rf"\1 ({_cml_inr(exact)})", answer, count=1)
    unmapped = await _cml_unmapped_in_scope(sql)
    if unmapped and not _CML_NO_VILLAGE_SAID.search(answer or ""):
        key = next((k for k in ("block", "district", "constituency") if display.get(k)), None)
        place = str(display[key]) if key else ""
        place = place.title() if place.isupper() else place
        where = (f" in {place}" + (" block" if key == "block" and "block" not in place.lower() else "")
                 if place else "")
        answer = (answer.rstrip() + f" A further {unmapped:,} record{'s' if unmapped != 1 else ''}{where} "
                  f"{'have' if unmapped != 1 else 'has'} no village code, so "
                  f"{'they are' if unmapped != 1 else 'it is'} not in the village list "
                  "(counted in block and district totals).")
    return answer


# Focus Legacy answer notes (use-case QA, 2026-09-25).
#
# TC-19 "Which Producer Groups received more than ₹1,00,000?": 541 groups
# qualify, but the generator capped the list at LIMIT 10/100 and the answer named
# 23 groups without ever saying how many qualify — it read as the complete list.
# When a Focus Legacy list comes back exactly at its LIMIT, the true count is
# re-queried from the same statement and the answer must lead with it.
_FINAL_LIMIT_RE = re.compile(r"\s+LIMIT\s+(\d+)\s*;?\s*\Z", re.IGNORECASE)
# TC-12 "Are there any duplicate Producer Groups?": the answer called the 2,655
# groups paid more than once "duplicate producer groups". They are repeat
# payments in later tranches; a duplicate RECORD would be the same group paid
# twice on the same date, and there are none.
_DUPLICATE_Q = re.compile(r"\bduplicat\w*|\brepeated\s+(?:producer\s+)?groups?\b|\bdoubles?\b",
                          re.IGNORECASE)


async def _focus_legacy_list_total(sql: str, rows: list[dict]) -> "tuple[int, str] | None":
    """(true row count, subject) when a Focus Legacy list was cut off by its own
    LIMIT, else None. The subject is "producer groups" for a pg_id-grained list."""
    m = _FINAL_LIMIT_RE.search(sql or "")
    if not m or len(rows or []) != int(m.group(1)) or len(rows) < 2:
        return None
    subject = "producer groups" if _FL_GROUP_GRAIN.search(sql) else "results"
    for key in ("groups_qualifying", "total_matching", "total_groups"):
        n = _as_number((rows[0] or {}).get(key))
        if isinstance(n, (int, float)) and n > len(rows):
            return int(n), subject
    inner = (sql or "")[: m.start()].rstrip().rstrip(";")
    try:
        got = await run_readonly(f"SELECT COUNT(*) AS n FROM ({inner}) q")
    except Exception:  # noqa: BLE001
        logger.warning("Focus Legacy list-total query failed — no total note", exc_info=True)
        return None
    n = _as_number((got or [{}])[0].get("n"))
    if not isinstance(n, (int, float)) or n <= len(rows):
        return None
    return int(n), subject


def _focus_legacy_answer_notes(question: str) -> list[str]:
    if not _DUPLICATE_Q.search(question or ""):
        return []
    return ["A producer group paid more than once is a REPEAT PAYMENT in a later tranche, NOT a "
            "duplicate group — never call those groups duplicates. A duplicate RECORD would be the "
            "same pg_id paid twice on the same date; report that count as the duplicates figure "
            "(0 means there are no duplicates), and mention the repeat-paid groups separately as "
            "legitimate repeat payments."]


# ── Focus Legacy: rows a per-place breakdown left out (KI-145) ──────────────
# "How many PGs are mapped to each block for West Khasi Hills?" — the model adds
# `AND lgd_block IS NOT NULL`, lists 5 blocks (921 PGs) and calls that the total
# "across all blocks", while 5 more PGs of the district have no block in the
# source (raw and DB: 88 rows statewide). Nothing told the officer they were
# left out (Focus Legacy use-case re-test 2026-09-29, TC-26). The left-out figure
# is read from the data — the same query without that filter — and stated.
_FL_PLACE_LEVELS = {"lgd_block": "block", "lgd_village_name": "village", "ac_name": "assembly constituency"}


# The model aliases the place column as often as not ("lgd_block AS block" —
# Ri Bhoi, bulk run 2026-09-29), so a result key is matched to its column here.
_FL_LEVEL_ALIAS = {"lgd_district": r"(?:lgd_)?district(?:_name)?",
                   "lgd_block": r"(?:lgd_)?block(?:_name)?",
                   "lgd_village_name": r"lgd_village_name|(?:lgd_)?village(?:_name)?",
                   "ac_name": r"ac_name|(?:assembly_)?constituency(?:_name)?"}


def _fl_canon(key: str) -> "str | None":
    return next((c for c, rx in _FL_LEVEL_ALIAS.items() if re.fullmatch(rx, str(key), re.I)), None)


def _fl_breakdown_level(sql: str, rows: list[dict], levels: "dict | None" = None) -> "str | None":
    """The result key of the finest place level the query GROUPs BY, else None."""
    levels = levels or _FL_PLACE_LEVELS
    if not rows:
        return None
    gb = re.search(r"\bGROUP\s+BY\b(?P<g>.*?)(?:\bHAVING\b|\bORDER\b|\bLIMIT\b|$)", sql or "", re.I | re.S)
    if not gb:
        return None
    found = [k for k in rows[0] if _fl_canon(k) in levels and re.search(
        rf"(?:\b\w+\.)?\b(?:{_fl_canon(k)}|{re.escape(str(k))}|\d+)\b", gb.group("g"), re.I)]
    order = list(levels)
    return max(found, key=lambda k: order.index(_fl_canon(k))) if found else None


def _fl_drop_not_null(sql: str, col: str) -> str:
    c = rf"(?:\w+\.)?{col}\s+IS\s+NOT\s+NULL"
    out = re.sub(rf"\s+AND\s+{c}\b", "", sql, flags=re.I)
    out = re.sub(rf"\bWHERE\s+{c}\s+AND\s+", "WHERE ", out, flags=re.I)
    out = re.sub(rf"\s*\bWHERE\s+{c}\b(?=\s*(?:GROUP|ORDER|LIMIT|HAVING|;|$))", "", out, flags=re.I)
    return out


async def _focus_legacy_unplaced_row(sql: str, rows: list[dict]) -> "tuple[str, dict] | None":
    """(level word, the result row for records with NO value at that level) for a
    Focus Legacy per-block / per-village / per-constituency breakdown, else None."""
    col = _fl_breakdown_level(sql, rows)
    if not col:
        return None
    hit = next((r for r in rows if r.get(col) in (None, "")), None)
    if hit is None:
        loose = _fl_drop_not_null(sql, _fl_canon(col))
        if loose == sql:
            return None
        loose = _FINAL_LIMIT_RE.sub("", loose)
        try:
            got = await run_readonly(loose)
        except Exception:  # noqa: BLE001 — a note, never a reason to fail the answer
            logger.warning("Focus Legacy unplaced-row query failed — no note", exc_info=True)
            return None
        hit = next((r for r in got or [] if r.get(col) in (None, "")), None)
    if hit is None:
        return None
    metrics = [(k, n) for k, v in hit.items() if k != col
               for n in [_as_number(v)] if n is not None and n > 0]
    return (_FL_PLACE_LEVELS[_fl_canon(col)], hit) if metrics else None


def _fl_metric_phrase(hit: dict) -> str:
    parts = []
    for k, v in hit.items():
        n = _as_number(v)
        if n is None or n <= 0 or _fl_canon(k):
            continue
        label = k.replace("_", " ")
        if _PMAY_MONEY_COL_RE.search(k) and not _PMAY_NOT_MONEY_COL_RE.search(k):
            parts.append(f"{label} {_pmay_money(n)}")
        else:
            parts.append(f"{int(n):,} {label}" if float(n).is_integer() else f"{n:,.2f} {label}")
    return ", ".join(parts)


def _focus_legacy_unplaced_note(level: str, hit: dict) -> str:
    return (f"Rows with NO {level} recorded in the source data are left out of the {level} list: "
            f"{_fl_metric_phrase(hit)}. Say in one sentence that these have no {level} recorded, and "
            f"never call the sum of the listed {level}s the total for the whole area.")


def _fl_unplaced_sentence(level: str, hit: dict) -> str:
    return (f"In addition, {_fl_metric_phrase(hit)} have no {level} recorded in the source data, "
            f"so they are not counted under any {level} above.")


def _focus_legacy_unplaced_guarantee(answer: str, level: str, hit: dict) -> str:
    """State the left-out figure exactly once, in digits. The composer's own version
    wrote it in words ("Two additional …") or without the number, and appending
    beside it said it twice (bulk run 2026-09-29), so its sentence is replaced."""
    said = re.compile(rf"\bno\s+{level}\b|\b{level}\s+(?:is\s+|was\s+)?(?:not\s+recorded|unknown|missing|blank)|"
                      rf"\bwithout\s+an?\s+{level}\b|\bremaining\s+(?:producer\s+)?groups?\b", re.I)
    kept = [s for s in re.split(r"(?<=[.!?])\s+", answer.strip()) if not said.search(s)]
    return (" ".join(kept).rstrip() + "\n\n" + _fl_unplaced_sentence(level, hit)).strip()


# ── Focus Legacy: per-place breakdowns, built from the rows ──────────────────
# "How many PGs are mapped to each block for <district>?" (bulk run 2026-09-29,
# all 12 districts): the composer dropped the per-block counts for West Jaintia
# Hills, called West Khasi Hills' 5 BLOCKS "5 producer groups", and wrote the
# left-out figure in words. The answer to a breakdown is fully determined by its
# rows, so it is written here. Every pg_id sits in exactly one district, block
# and village (verified 2026-09-29), so the per-place counts add up exactly.
_FL_BREAKDOWN_LEVELS = {"lgd_district": "district", **_FL_PLACE_LEVELS}
_FL_BREAKDOWN_MAX_ROWS = 100


# "Which Producer Groups are mapped to NENGKRA AWE village?" — the composer
# listed 11 names but rewrote one ("Bak 13 Nalsa Pepper Producer Group." became
# "… Pepper Group."; all-villages run 2026-09-29). A result that is only group
# ids / names (plus place labels) is a name list, written from the rows.
_FL_GROUP_ID_COLS = {"pg_id", "pg_name", "name_of_pg", "producer_group", "group_name"}


async def _fl_add_group_names(rows: list[dict]) -> list[dict]:
    """A Focus Legacy result of bare pg_ids ("SELECT DISTINCT pg_id … WHERE
    village_code = 272784") answered "which producer groups?" with ids only
    (all-villages run 2026-09-29, KI-165). Add each group's name from the data."""
    if not rows or "pg_name" in rows[0] or "pg_id" not in rows[0] or len(rows) > _FL_BREAKDOWN_MAX_ROWS:
        return rows
    if any(k != "pg_id" and not _fl_canon(k) for k in rows[0]):
        return rows
    try:
        names = {r["pg_id"]: r["pg_name"] for r in await fetch_rows(
            "SELECT pg_id, MAX(pg_name) AS pg_name FROM curated.v_focus_legacy WHERE pg_id = ANY($1::text[]) "
            "GROUP BY pg_id", [[str(r["pg_id"]) for r in rows]])}
    except Exception:  # noqa: BLE001 — keep the ids
        return rows
    return [{**r, "pg_name": names.get(r["pg_id"])} for r in rows]


def _focus_legacy_group_list_answer(rows: list[dict], display: dict,
                                    total: "int | None" = None) -> "str | None":
    if not rows or "pg_name" not in rows[0] or len(rows) > _FL_BREAKDOWN_MAX_ROWS:
        return None
    if any(k not in _FL_GROUP_ID_COLS and not _fl_canon(k) for k in rows[0]):
        return None
    names = []
    for r in rows:
        if r.get("pg_name"):
            names.append(f"- {r['pg_name']}" + (f" ({r['pg_id']})" if r.get("pg_id") else ""))
    if not names:
        return None
    n = max(total or 0, len(names))
    where = [str(display[k]) + (" village" if k == "village" else " block" if k == "block" else
                                " constituency" if k == "assembly_constituency" else "")
             for k in ("village", "block", "assembly_constituency", "district") if display.get(k)]
    scope = f" mapped to {', '.join(where)}" if where else ""
    head = f"{n:,} producer group{'s are' if n != 1 else ' is'}{scope} in the Focus Legacy data"
    if n > len(names):
        head += f"; the first {len(names)} are listed"
    return head + ":\n\n" + "\n".join(names)


def _fl_metric_value(k: str, n) -> str:
    if _PMAY_MONEY_COL_RE.search(k) and not _PMAY_NOT_MONEY_COL_RE.search(k):
        return _pmay_money(n).split(" (")[0]
    return f"{int(n):,}" if float(n).is_integer() else f"{n:,.2f}"


def _focus_legacy_breakdown_answer(sql: str, rows: list[dict], display: dict,
                                   unplaced: "tuple[str, dict] | None") -> "str | None":
    if not rows or len(rows) > _FL_BREAKDOWN_MAX_ROWS:
        return None
    level_col = _fl_breakdown_level(sql, rows, _FL_BREAKDOWN_LEVELS)
    if not level_col:
        return None
    level = _FL_BREAKDOWN_LEVELS[_fl_canon(level_col)]
    labels = [k for k in rows[0] if k != level_col and _fl_canon(k)]
    metrics = [k for k in rows[0] if k != level_col and k not in labels
               and not re.search(r"code|(?:^|_)id$|_key$", k, re.I)]
    # only place labels and plain numbers — anything else (a date, a year, a name)
    # is a different shape of answer and stays with the composer
    if not metrics or len(metrics) > 3 or any(
            _as_number(r.get(k)) is None for r in rows for k in metrics if r.get(k) is not None):
        return None
    null_rows = [r for r in rows if r.get(level_col) in (None, "")]
    listed = [r for r in rows if r.get(level_col) not in (None, "")]
    if not listed:
        return None
    if null_rows and not unplaced:
        unplaced = (level, null_rows[0])
    lim = _FINAL_LIMIT_RE.search(sql or "")
    truncated = bool(lim) and len(rows) == int(lim.group(1)) and int(lim.group(1)) < 1000

    def words(k):
        return k.replace("_", " ")

    def line(r):
        name = _place_title(r[level_col])
        ctx = ", ".join(_place_title(r[c]) for c in labels if r.get(c))
        vals = ", ".join(f"{_fl_metric_value(k, _as_number(r.get(k)) or 0)} {words(k)}" for k in metrics)
        return f"- {name}{f' ({ctx})' if ctx else ''}: {vals}"

    scope_bits = [str(display.get(k)) for k in ("assembly_constituency", "district", "block", "village")
                  if display.get(k)]
    scope = f" in {', '.join(scope_bits)}" if scope_bits else ""
    yr = next((str(display[k]) for k in ("year", "financial_year") if display.get(k)), None)
    period = f", {yr}" if yr else ""
    head = (f"Top {len(listed)} {level}s{scope}{period} by {words(metrics[0])}:" if truncated
            else f"Focus Legacy {words(metrics[0])} by {level}{scope}{period} — {len(listed)} "
                 f"{level}{'s' if len(listed) != 1 else ''}:")
    out = [head, ""] + [line(r) for r in listed]
    if not truncated and len(listed) > 1:
        tot = {k: sum((_as_number(r.get(k)) or 0) for r in listed) for k in metrics}
        together = ", ".join(f"{_fl_metric_value(k, v)} {words(k)}" for k, v in tot.items())
        out += ["", f"Together, the {len(listed)} {level}s above account for {together}."]
        if unplaced:
            extra = {k: _as_number(unplaced[1].get(k)) or 0 for k in metrics}
            if any(extra.values()):
                out.append(_fl_unplaced_sentence(unplaced[0], unplaced[1]) + " Including them, the total is "
                           + ", ".join(f"{_fl_metric_value(k, tot[k] + extra[k])} {words(k)}" for k in metrics)
                           + ".")
    elif unplaced:
        out += ["", _fl_unplaced_sentence(*unplaced)]
    return "\n".join(out)


# ── Focus Legacy: month numbers and bare rupees (KI-147) ─────────────────────
# "How much was remitted during each month for FY 2022-23?" came back as
# "52,035,000.00 in month 4 and 89,755,000.00 in month 5", and single totals as
# "54205000.00" (re-test 2026-09-29, TC-18/21/23). Values were right; the
# presentation is fixed here from the result cells only.
_FL_MONTH_COL_RE = re.compile(r"^(?:remittance_|payment_)?month(?:_(?:num|no|number|of_year))?$", re.I)
_FL_FY_SQL_RE = re.compile(r"financial_year(?:_short)?\s*=\s*'(\d{4})-(?:\d{2}|\d{4})'", re.I)


def _focus_legacy_month_labels(answer: str, sql: str, rows: list[dict]) -> str:
    if not answer or not rows:
        return answer
    col = next((k for k in rows[0] if _FL_MONTH_COL_RE.match(str(k))), None)
    if not col:
        return answer
    fy_sql = _FL_FY_SQL_RE.search(sql or "")
    for r in rows[:24]:
        v = r.get(col)
        if isinstance(v, (datetime.date, datetime.datetime)):
            label = v.strftime("%B %Y")
            for s in {v.isoformat(), str(v), v.strftime("%Y-%m-%d"), v.strftime("%Y-%m")}:
                answer = re.sub(re.escape(s) + r"(?:[T ]00:00:00(?:\+00:00)?)?", label, answer)
            continue
        m = _as_number(v)
        if m is None or not float(m).is_integer() or not 1 <= int(m) <= 12:
            continue
        m = int(m)
        start = None
        fy = str(r.get("financial_year_short") or r.get("financial_year") or "")
        if re.match(r"\d{4}-", fy):
            start = int(fy[:4])
        elif fy_sql:
            start = int(fy_sql.group(1))
        yr = _as_number(r.get("year") or r.get("calendar_year"))
        year = int(yr) if yr else (start + (0 if m >= 4 else 1) if start else None)
        label = calendar.month_name[m] + (f" {year}" if year else "")
        answer = re.sub(rf"\bmonth\s+0?{m}\b(?!\s*(?:{'|'.join(calendar.month_name[1:])}))", label, answer,
                        flags=re.I)
    return answer


def _focus_legacy_answer_guarantees(answer: str, sql: str, rows: list[dict]) -> str:
    answer = _focus_legacy_month_labels(answer, sql, rows)
    # a long list keeps plain ₹ grouping — a lakh/crore gloss on every line is noise
    fmt = _pmay_money if len(rows or []) <= 5 else (lambda v: _pmay_money(v).split(" (")[0])
    return _pmay_rupee_format(answer, rows, fmt=fmt, column_totals=True)


# ── Focus Legacy: "is there a PG named X?" / "how many members are there in X?" ──
# Bulk QA of 290 sampled producer groups (2026-09-25) found the model-written SQL
# unreliable for these two fixed-shape questions: 52 of 131 member-count answers
# were wrong. When several groups share a name ("Chibasal" matches 73) it ended
# the query in LIMIT 1 and reported one arbitrary group's size; it rewrote names
# ("Chelchak Pineapple P.g" -> '%chelchak pineapple p.g.%', zero rows); and some
# never reached the data at all. The answer is fully determined by the data, so
# it is built here: match the name on its WORDS (ignoring "PG" / "Producer
# Group" / punctuation, each word anchored at a word start), rank groups whose
# whole name is exactly those words first, and when several groups match, list
# them instead of guessing.
_PG_STOP_WORDS = {"pg", "pgs", "p", "g", "producer", "producers", "group", "groups", "grp",
                  "the", "shg", "named", "called"}
_PG_SIZE_QUESTION = re.compile(
    r"^\s*(?:how\s+many\s+(?:pg\s+)?members?\s+(?:are\s+(?:there\s+)?|were\s+(?:there\s+)?|is\s+there\s+)?"
    r"(?:in|of)\s+|how\s+many\s+members\s+does\s+|"
    r"what\s+is\s+the\s+(?:member\s+count|group\s+size|number\s+of\s+members)\s+(?:of|in|for)\s+)"
    r"(?:the\s+)?(?:(?:producer\s+group|pg|group)\s+(?:named|called)\s+)?(?P<name>.+?)"
    r"(?:\s+have)?\s*[?.!]*\s*$",
    re.IGNORECASE)
# Scope phrases a clarification chip appends ("... for Focus Legacy for all of
# Meghalaya, all years") — not part of a group name.
_PG_SCOPE_TAIL = re.compile(
    r"\s*,?\s*(?:(?:for|in|under|across|within)\s+(?:the\s+)?(?:focus\s+legacy|focus|all\s+of\s+meghalaya|"
    r"meghalaya|all\s+(?:the\s+)?(?:financial\s+)?years(?:\s+combined)?)|all\s+(?:financial\s+)?years"
    r"(?:\s+combined)?|scheme)\s*[?.!]*\s*$",
    re.IGNORECASE)
_PG_PLACE_WORDS = re.compile(r"\b(?:district|block|village|constituency|state|meghalaya|hills)\b", re.IGNORECASE)
_PG_SUFFIX_WORD = re.compile(r"\b(?:p\.?\s*g\.?|pgs?|producer\s+groups?)\b|\bgroup\b", re.IGNORECASE)


def _pg_rest_is_place(rest: str) -> bool:
    """Whether the words after a group name are a place phrase ("in Betasing
    block", "in Nongstoin") rather than more of the name ("In One Ginger Producer
    Group" in "All In One Ginger Producer Group" — all-PG run 2026-09-29)."""
    if re.match(r"^\s*,?\s*there\b", rest, re.IGNORECASE):
        return True
    m = re.match(r"^\s*,?\s*(?:in|for|under|from|at|within|across)\s+(?:the\s+)?(?P<p>.+?)\s*$", rest, re.IGNORECASE)
    if not m:
        return False
    p = m.group("p")
    return bool(_PG_PLACE_WORDS.search(p)) or \
        re.sub(r"\s+(?:c&rd\s+)?(?:block|district)$", "", p.lower()) in _known_place_names()


def _pg_name_question(question: str, allow_weak: bool = False) -> "tuple[str, str] | None":
    """('exists' | 'size', typed name) for a group-name question, else None.
    With allow_weak, a size question whose name holds a place word and no group
    word ("members in Green Hills") comes back as 'size?': a group only if one is
    named exactly that (checked by the caller against the data)."""
    q = (question or "").strip()
    kind, name = None, None
    m = _PG_SIZE_QUESTION.match(q)
    if m:
        kind, name = "size", m.group("name")
    else:
        m = _PG_NAMED_ENTITY.search(q)
        if m and re.match(r"^\s*(?:is|are)\s+there\s+(?:any|a|an)\b", q, re.IGNORECASE):
            tail = q[m.end("name"):]
            rest = _PG_SCOPE_TAIL.sub("", tail).strip(" ?.!")
            # "…named X in Betasing block" carries its own place: leave it to the
            # full pipeline, which filters on that place.
            if _pg_rest_is_place(rest):
                return None
            # Otherwise the name simply ran on — a comma inside it ("Ka Seng Ki
            # Nongrep Jhur, Shkenpyrsit") or more words than the pattern takes.
            kind, name = "exists", f"{m.group('name')} {rest}".strip() if rest else m.group("name")
    if not kind:
        return None
    prev = None
    while prev != name:
        prev, name = name, _PG_SCOPE_TAIL.sub("", name).strip(" ?.!,\"'“”")
    if not name:
        return None
    if _PG_PLACE_WORDS.search(name):
        # "Green Hills Producer Group", "Umdang Dong Block Producer Group": a place
        # word inside a name the user marked as a group (named / "Producer Group")
        # is part of the name — 20 stored names were unreachable (2026-09-29).
        # A place with a group word bolted on ("West Garo Hills producer groups")
        # is still a place question.
        core = _PG_SUFFIX_WORD.sub(" ", name).strip().lower()
        core = re.sub(r"\s+", " ", core)
        if kind == "exists" or (_PG_SUFFIX_WORD.search(name) and core not in _known_place_names() and not re.fullmatch(
                r"(?:.*\s)?(?:district|block|village|constituency|state|meghalaya)s?", core)):
            pass
        elif allow_weak and kind == "size" and core not in _known_place_names() and re.sub(
                r"\s+(?:c&rd\s+)?(?:block|district|village|constituency)s?$", "", core) not in _known_place_names():
            kind = "size?"
        else:
            return None
    # A bare district/block name is a place question ("members in Nongstoin"),
    # unless the user marked it as a group.
    if kind == "size" and not _PG_SUFFIX_WORD.search(name) \
            and not re.search(r"\b(?:producer\s+group|pg)\s+(?:named|called)\b", q, re.IGNORECASE):
        if name.lower() in _known_place_names():
            return None
    if not [t for t in _pg_name_tokens(name) if t not in _PG_STOP_WORDS]:
        return None
    return kind, name


def _pg_name_tokens(text: str) -> list[str]:
    # Letters of any script, not just a-z: 6 stored names are mis-encoded text
    # ("AÃ£Æ’Ã¦…we Producer Group"), and an ASCII-only read left "a we", which
    # matched a different group, "A.we" (all-PG run 2026-09-29). Tokens stay
    # letters/digits only, so they remain safe inside the SQL regex literal.
    return [t for t in re.findall(r"[^\W_]+", (text or "").lower()) if t not in _PG_STOP_WORDS]


async def _focus_legacy_pg_name_answer(question: str) -> "dict | None":
    parsed = _pg_name_question(question, allow_weak=True)
    if parsed is None:
        return None
    kind, name = parsed
    toks = _pg_name_tokens(name)

    def _where(col: str, word_end: bool) -> str:
        # tokens are letters/digits only, so they are safe inside the regex literal
        end = "\\M" if word_end else ""
        # Postgres counts "_" as a word character, so "\m25\M" never matched
        # "Erifa_25" (all-PG run 2026-09-29, KI-158): compare with "_" as a space.
        return " AND ".join(f"translate({col}, '_', ' ') ~* '\\m{t}{end}'" for t in toks)

    def _sql(where: str) -> str:
        return ("SELECT pg_id, MAX(pg_name) AS pg_name, MAX(lgd_district) AS district, "
                "MAX(lgd_block) AS block, MAX(no_of_pg_members) AS group_size, "
                "MIN(no_of_pg_members) AS smallest_recorded_size, COUNT(*) AS payments, "
                "SUM(amount_disbursed) AS amount_disbursed\n"
                f"FROM curated.v_focus_legacy\nWHERE {where}\nGROUP BY pg_id\nORDER BY pg_id\nLIMIT 1000")

    # Whole words first ("ma" must not match every "Mawlai…"); word prefixes only
    # when nothing matches whole ("Bak15" typed for "Bak-15" still resolves).
    # Earlier spellings the raw file still carries ("Drongpa Pg" for the group now
    # named "Dronpa") are searched TOGETHER with current names: 389 of 1,635 older
    # spellings were "not found", and an older spelling can equal another group's
    # current name ("Ten Star Producer Group"), so a current-name hit must not end
    # the search (all-PG run 2026-09-29). The DB keeps every spelling in
    # v_focus_legacy_pg_search (KI-020).
    rows, alias_of = [], {}
    for word_end in (True, False):
        aw = _where("matched_alias", word_end)
        hits = await run_readonly(
            "SELECT DISTINCT pg_id, matched_alias FROM curated.v_focus_legacy_pg_search "
            f"WHERE NOT is_current AND {aw} LIMIT 1000")
        alias_of = {}
        for h in hits:
            alias_of.setdefault(h["pg_id"], []).append(str(h["matched_alias"]))
        where = _where("pg_name", word_end)
        if alias_of:
            where = (f"(({where}) OR pg_id IN (SELECT pg_id FROM curated.v_focus_legacy_pg_search "
                     f"WHERE NOT is_current AND {aw}))")
        sql = _sql(where)
        rows = await run_readonly(sql)
        if rows:
            break
    exact = [r for r in rows if _pg_name_tokens(r.get("pg_name")) == toks
             or any(_pg_name_tokens(a) == toks for a in alias_of.get(r.get("pg_id"), []))]
    if kind == "size?":
        # "members in Green Hills": a group only when one is named exactly that
        if not exact:
            return None
        kind = "size"
    ranked = exact + [r for r in rows if r not in exact]

    def place(r):
        bits = [f"{str(r['block']).title()} block" if r.get("block") else None,
                str(r.get("district") or "").title() or None]
        return ", ".join(b for b in bits if b)

    def line(r):
        return (f"- **{r['pg_name']}** ({r['pg_id']}) — {place(r)}: "
                f"{r['group_size']} member{'s' if r['group_size'] != 1 else ''}")

    single = exact[0] if len(exact) == 1 else (rows[0] if len(rows) == 1 else None)
    if not rows:
        answer = (f"No producer group named “{name}” was found in the Focus Legacy data "
                  "(names are matched on their words, ignoring “PG” / “Producer Group”)."
                  + (" So there is no member count to report." if kind == "size" else ""))
    elif single is not None:
        r = single
        if kind == "size":
            answer = (f"**{r['pg_name']}** ({r['pg_id']}, {place(r)}) has **{r['group_size']} "
                      f"member{'s' if r['group_size'] != 1 else ''}**.")
            if r["smallest_recorded_size"] != r["group_size"]:
                answer += (f" Its recorded size changed between its {r['payments']} payments "
                           f"({r['smallest_recorded_size']} to {r['group_size']}); "
                           f"{r['group_size']} is the largest recorded.")
        else:
            answer = (f"Yes — **{r['pg_name']}** ({r['pg_id']}) is a Focus Legacy producer group in "
                      f"{place(r)}, with {r['group_size']} members and {r['payments']} "
                      f"payment{'s' if r['payments'] != 1 else ''} totalling "
                      f"{_pmay_money(_as_number(r['amount_disbursed']) or 0)}.")
        if alias_of.get(r["pg_id"]):
            answer += f" “{name}” is an earlier recorded spelling of this group's name."
        if len(rows) > 1:
            answer += (f" ({len(rows) - 1} other group{'s have' if len(rows) > 2 else ' has'} "
                       f"“{name}” within a longer name.)")
    else:
        head = (f"Yes — {len(rows)} producer groups match “{name}”" if kind == "exists"
                else f"{len(rows)} producer groups match “{name}”, so the member count depends on "
                     "which one you mean")
        if exact:
            head += f" ({len(exact)} named exactly that)"
        _via_old = [r for r in ranked[:10] if alias_of.get(r["pg_id"])
                    and _pg_name_tokens(r.get("pg_name")) != toks]
        if _via_old:
            head += (f" ({len(_via_old)} of those shown matched on an earlier recorded spelling; "
                     "current names are shown)")
        shown = ranked[:10]
        answer = head + ":\n\n" + "\n".join(line(r) for r in shown)
        if len(ranked) > len(shown):
            answer += f"\n\n…and {len(ranked) - len(shown)} more in the table."
        if kind == "size":
            answer += "\n\nTell me the PG ID or the district to pin down one group."
    logger.info("Focus Legacy PG-name %s question for %r: %d match(es), %d exact", kind, name,
                len(rows), len(exact))
    return {"route": "data", "intent": "DATA", "confidence": "high", "schemes": ["Focus Legacy"],
            "resolved_entities": {}, "sql": sql, "sql_query": sql, "row_count": len(rows),
            "rows": ranked[:20], "data": ranked, "answer": answer}


def _cm_legacy_small_money_notes(rows: list[dict]) -> list[str]:
    """A _cr value of 0.00 is a real amount under ₹0.5 lakh, not zero (TC-18:
    Sericulture spinning ₹26,000 was written as "₹0.00 crore")."""
    tiny = [r for r in rows or [] for k, v in r.items()
            if k.endswith("_cr") and _as_number(v) == 0]
    if not tiny:
        return []
    return ["A _cr value of 0.00 means under ₹0.01 crore (less than ₹1 lakh), not "
            "nothing — write it as 'under ₹0.01 crore', never '₹0.00 crore'."]


_CM_LEGACY_TRAILING_OFFER = re.compile(
    r"\s*(?:Would you like|Shall I|Do you want)[^?]*\?\s*$", re.IGNORECASE)


def _cm_legacy_style_block(question: str) -> str:
    """Two worked answers from the bank as WORDING examples for the composer.
    The trailing "Would you like…?" is dropped — the UI already offers next-step
    chips, and a second offer in the text would duplicate them."""
    shots = [a for a in annotations.answer_shots("CM Elevate Legacy", question, top_k=4)
             if a.get("rows")][:2]
    if not shots:
        return ""
    parts = []
    for a in shots:
        answer = _CM_LEGACY_TRAILING_OFFER.sub("", " ".join(str(a["answer"]).split()))
        parts.append(f'Q: "{a["question"]}"\nColumns: {", ".join(a["columns"])}\n'
                     f"Rows: {json.dumps(a['rows'], ensure_ascii=False)}\nAnswer: {answer}")
    return ("\nWORDING EXAMPLES for this scheme — copy the style only. Their numbers "
            "belong to OTHER questions and must never appear in this answer:\n"
            + "\n\n".join(parts) + "\n")


# Plain-language, user-facing scheme summaries — separate from SCHEME_CATALOG
# in schema_context.py, which is written for the SQL-generation prompt (DB
# grain, money units, join keys) and reads as database jargon to an end user.
_SCHEME_USER_SUMMARY = {
    "MGNREGA": "Rural employment guarantee scheme — up to 100 days of guaranteed "
               "wage employment per household per year.",
    "PMAY-G": "Rural housing scheme (Gramin) — financial assistance to build a "
              "pucca house for eligible rural households.",
    "Focus Plus": "Meghalaya state farmer cash-benefit scheme — direct cash "
                  "payments (DBT) to registered farmers.",
    "CM Elevate": "Meghalaya livelihood & enterprise support programme — covers "
                  "15 individual schemes (piggery, poultry, small enterprise "
                  "loans, tourism vehicles, and more) under one umbrella.",
    "Focus Legacy": "Meghalaya state producer-group scheme (the original FOCUS) — "
                    "payments to farmer Producer Groups at Rs 5,000 per member. "
                    "Different from Focus Plus, which pays individual farmers.",
    "CM Elevate Legacy": "CM-ELEVATE sanction and disbursement records — the amount "
                         "sanctioned to each applicant across 13 schemes (piggery, "
                         "poultry, dairy, warehouse, tourism vehicles and more) and the "
                         "subsidy and loans actually paid, FY 2024-25 and 2025-26. "
                         "Different from CM Elevate, which holds the applications.",
}

# "What schemes are available?" / "what can you help with?" — answered directly
# and concisely instead of falling through to RAG, which has no single document
# listing all 4 schemes and tends to elaborate at length on whichever one scores
# highest in vector search (QA repeatedly saw an over-detailed PMAY-only answer).
#
# The bare "(what|which) schemes?" alternative used to have NO tail anchor, so it
# also swallowed every "which scheme has the most applications?" / "which scheme
# dominates each district?" / "which schemes are not statewide?" DATA question —
# CM Elevate's few-shot corpus alone has a dozen of exactly this shape (superlative
# or filter questions over its 15 sub-schemes), and every one of them was being
# answered with the generic four-scheme blurb instead of a real query (confirmed
# live 2026-09-11: "which scheme has the most applications under CM Elevate?" ->
# the canned listing, never reaching classify_scheme/resolve_entities/SQL gen).
# Anchoring the bare form to the tail of the question — "which schemes?" / "what
# schemes are there/available/offered/supported" / "do you have/know/cover" —
# keeps the genuinely scheme-agnostic listing asks while letting a superlative or
# filter question (which always has more text after "scheme(s)") fall through to
# normal DATA routing.
_SCHEME_LISTING_CUE = re.compile(
    r"\b(?:what|which) schemes?\??\s*$|"
    r"\b(?:what|which) schemes?\b\s*(?:are\s+(?:there|available|offered|supported)|"
    r"exist|do (?:you|i) (?:have|know|cover)|can you (?:tell|list))\b|"
    r"\bschemes? (?:are|is) available\b|"
    r"\blist (?:the |all )?schemes?\b|\bschemes? (?:do you|you) (?:support|cover|know|have)\b|"
    r"\bwhat (?:can|do) you (?:help with|assist with|cover)\b",
    re.IGNORECASE,
)

# A vague eligibility ask — "is there any scheme that can help my family?",
# "which scheme should I apply for?" — carries no vocabulary naming a scheme
# outright, so it used to fall into the "which scheme does your question
# concern?" pause. That pause is the wrong move here: unlike a DATA question
# (where guessing the scheme risks a confidently wrong number), there's
# nothing to get wrong about naming all four — so skip the tap and lay them
# out directly. When the wording DOES carry scheme-specific vocabulary (e.g.
# "...to help me build a house") _infer_scheme_from_terms already pins it to
# one scheme and _needs_scheme_clarification never pauses in the first place —
# this cue only needs to cover the genuinely scheme-agnostic case, so it
# defers to that inference rather than overriding it.
_SCHEME_HELP_CUE = re.compile(
    r"\bis there (?:any|a) (?:govt\.?|government )?schemes?\b|"
    r"\b(?:any|a) scheme (?:that|which|to) (?:can |could )?help\b|"
    r"\bscheme(?:s)? (?:that|which) (?:can|could) help\b|"
    r"\bwhich scheme (?:can|could|should) (?:i|we)\b|"
    r"\bwhat scheme should (?:i|we)\b",
    re.IGNORECASE,
)


# CM Elevate and CM Elevate Legacy are ONE programme (CM-ELEVATE) held as two
# datasets — its applications, and its sanctions and disbursements. Listed as
# two schemes, the reply read as if there were two unrelated programmes (and the
# count was hard-coded "four" against six lines). The listing shows the
# programme once and names its two data parts; every other scheme's line is
# unchanged. _SCHEME_USER_SUMMARY keeps both entries, for the comparison answer.
_PROGRAMME_DATASETS = {"CM Elevate": ["CM Elevate Legacy"]}
_COUNT_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
                7: "seven", 8: "eight", 9: "nine", 10: "ten"}


def _count_word(n: int) -> str:
    return _COUNT_WORDS.get(n, str(n))


def _scheme_listing_lines() -> list[str]:
    folded = {d for ds in _PROGRAMME_DATASETS.values() for d in ds}
    lines = []
    for name, desc in _SCHEME_USER_SUMMARY.items():
        if name in folded:
            continue
        if name == "CM Elevate":
            desc = (
                "Meghalaya livelihood & enterprise support programme (CM-ELEVATE) — "
                "piggery, poultry, dairy, small enterprise, tourism vehicles and more. "
                "Its data comes in two parts: the **applications** (15 schemes — status, "
                "verification, applicant type; no amounts) and **CM Elevate Legacy**, the "
                "sanctions and disbursements (13 schemes — amount sanctioned, subsidy and "
                "loans paid, FY 2024-25 and 2025-26)."
            )
        lines.append(f"- **{name}** — {desc}")
    return lines


# ── "Pick any scheme and explain it" — the user hands the CHOICE to the bot ──
# "pick any scheme out of these and explain", "no, pick yourself any scheme",
# "you choose one", "tell me about any one of them", "explain a scheme of your
# choice", "surprise me"... The user is not naming a scheme and does not want to
# be asked for one — asking "which scheme?" is the exact opposite of the request
# (reported 2026-09-24: three turns of the bot either asking back or answering
# "not covered" against the raw wording). Answered deterministically: the scheme
# the user did name if any, else one this conversation has not covered yet,
# explained as key points from that scheme's own reference docs.
_PICK_VERB = (r"(?:pick|choose|chose|select|take|go\s+with|explain|describe|"
              r"tell\s+(?:me\s+)?about|talk\s+about|give|show|share|summari[sz]e|brief|elaborate)")
_PICK_DELEGATION = re.compile(
    # "pick any scheme", "choose one of these", "take a random scheme", "pick yourself",
    # "select one scheme", "pick another scheme"
    rf"\b{_PICK_VERB}\b[^.?!]{{0,40}}?\b(?:any(?:\s*one)?|some|a\s+random|random|one\s+of|"
    r"another|your\s*self|yourself|your\s+own|of\s+your\s+choice|whichever|"
    r"(?:a|one)\s+(?:single\s+)?(?:scheme|programm?e))\b|"
    # "any one of these / them", "any scheme", "one of the schemes"
    r"\bany\s*(?:one|1)\s+(?:of\s+)?(?:these|them|those|the\s+schemes?)\b|"
    r"\b(?:any|a\s+random|random)\s+scheme\b|"
    # "you choose", "your choice", "you decide", "up to you", "surprise me"
    r"\b(?:you|u)\s+(?:pick|choose|decide|select)\b|\byour\s+(?:choice|pick|call)\b|"
    r"\bup\s+to\s+you\b|\bsurprise\s+me\b|\bwhichever\s+(?:you|u)\b",
    re.IGNORECASE)
# What makes it a question about a SCHEME (not "pick any district and show ..."):
# the word scheme / programme, a reference back to the list just shown, or a
# delegation phrase that can only mean the scheme choice.
_PICK_OBJECT = re.compile(
    r"\bschemes?\b|\bprogramm?e?s?\b|\b(?:these|them|those)\b|\bone\s+of\b|"
    r"\byour\s*self\b|\byourself\b|\byour\s+(?:own|choice|pick|call)\b|\bup\s+to\s+you\b|"
    r"\bsurprise\s+me\b|\b(?:you|u)\s+(?:pick|choose|decide|select)\b|"
    # a bare "just pick any" / "pick any one" / "choose another" — nothing else in
    # the message, so the only thing on offer to pick is a scheme
    r"^\W*(?:no\W+|ok(?:ay)?\W+|then\W+|so\W+)?(?:just\s+)?(?:pick|choose|select)\s+"
    r"(?:any(?:\s*one)?|one|another(?:\s+one)?|one\s+more|the\s+next\s+one|next\s+one)\W*$",
    re.IGNORECASE)
# "another one" / "one more" / "next one" / "explain another" on its own. Only a
# pick when the PREVIOUS answer was a pick — after a data answer the same words
# mean another district or year, and go to the follow-up rewrite as before.
_PICK_CONTINUE = re.compile(
    r"^\W*(?:no\W+|ok(?:ay)?\W+|then\W+|so\W+|and\W+)?(?:(?:explain|tell\s+(?:me\s+)?about|"
    r"describe|give|show)\s+(?:me\s+)?)?(?:another(?:\s+(?:one|scheme))?|one\s+more|"
    r"(?:the\s+)?next\s+(?:one|scheme))\W*$",
    re.IGNORECASE)
_PICK_OVERVIEW_PREFIX = "Explain its key points — the objective"
# A NEED-based ask ("is there any scheme that can help my family?", "any scheme
# for farmers?") wants the scheme that FITS — the existing help/listing path
# answers that. Picking one arbitrarily would be the wrong answer to it.
_PICK_NEED_GUARD = re.compile(
    r"\b(?:help|helps|helpful|eligible|suitable|should\s+i|can\s+i|could\s+i|"
    r"apply\s+for|is\s+there)\b|"
    r"\bfor\s+(?:farmers?|women|youth|students?|the\s+poor|poor|my|us|families|"
    r"entrepreneurs?|widows?|elderly|disabled)\b",
    re.IGNORECASE)
# A request for a FIGURE is a data question even when it says "pick any".
_PICK_DATA_GUARD = re.compile(
    r"\b(?:how\s+many|how\s+much|total|number\s+of|count|sum|average|top\s*\d|"
    r"highest|lowest|most|least|rank\w*|district|block|village|panchayat|"
    r"financial\s+year|fy\s*\d|20\d\d|expenditure|person[\s-]?days?|houses?|"
    r"disburs\w*|amount|payments?|records?|applications?|beneficiar\w*|"
    r"data|figures?|numbers|statistics|stats)\b",
    re.IGNORECASE)
# "another" / "different" / "other" / "next" — explicitly not the one just done.
_PICK_ANOTHER = re.compile(r"\b(?:another|different|other|next|new|else)\b", re.IGNORECASE)


def _is_scheme_pick_request(question: str) -> bool:
    q = question or ""
    # "why did you choose MGNREGA?" asks for a REASON, not another pick.
    if _WHY_CHOICE.search(q):
        return False
    if not _PICK_DELEGATION.search(q) or not _PICK_OBJECT.search(q):
        return False
    return not (_PICK_DATA_GUARD.search(q) or _PICK_NEED_GUARD.search(q))


def _pickable_schemes() -> list[str]:
    """The programmes a user can be given, in listing order. CM Elevate Legacy
    is CM Elevate's data, not a separate programme (see _PROGRAMME_DATASETS)."""
    folded = {d for ds in _PROGRAMME_DATASETS.values() for d in ds}
    return [s for s in _SCHEME_USER_SUMMARY if s not in folded]


def _pick_scheme(question: str, session: "Session | None") -> tuple[str, bool]:
    """(scheme, chosen_by_bot). A scheme the user named wins. Otherwise the first
    programme this conversation has not been told about yet, so "pick another"
    / a repeated "pick one yourself" moves on instead of repeating itself."""
    named = [s for s in _named_schemes(question) if s in _SCHEME_USER_SUMMARY]
    if named:
        folded = {d: p for p, ds in _PROGRAMME_DATASETS.items() for d in ds}
        return folded.get(named[0], named[0]), False
    options = _pickable_schemes()
    folded = {d: p for p, ds in _PROGRAMME_DATASETS.items() for d in ds}
    covered: list[str] = []
    for t in (getattr(session, "turns", None) or []):
        for s in (t.schemes or []):
            s = folded.get(s, s)
            if s in options and s not in covered:
                covered.append(s)
    fresh = [s for s in options if s not in covered]
    if fresh:
        return fresh[0], True
    # Everything has been covered once — cycle on from the most recent one.
    last = covered[-1] if covered else options[-1]
    return options[(options.index(last) + 1) % len(options)], True


# A request for a summary of the scheme as a whole (see the knowledge route).
_OVERVIEW_REQUEST = re.compile(
    r"\boverview\b|\bbriefing\b|\bbrief\s+(?:note|summary|introduction)\b|"
    r"\bsummar(?:y|ise|ize)\s+(?:of\s+)?(?:the\s+)?(?:scheme|programme|program|focus)\b|"
    r"\bat\s+a\s+glance\b|\bexecutive\s+summary\b",
    re.IGNORECASE)


def _scheme_overview_question(scheme: str) -> str:
    """The standalone knowledge question the pick is answered with — phrased as
    the reference docs are written, so retrieval lands on the overview sections
    rather than on whatever the user's delegation wording happened to match."""
    return (f"What is {scheme}? Explain its key points — the objective, who is "
            f"eligible, the benefits it provides and how to apply.")


# ── "Suggest a scheme that suits me" — a RECOMMENDATION from the user's profile ──
# "i am living in rural area, suggest me the best scheme", "my friend is starting
# a startup, suggest him a scheme", "i am a farmer, suggest a scheme". None of
# these names a scheme, so the knowledge route inherited the PREVIOUS turn's
# scheme and searched only its documents — every one of them was answered from
# MGNREGA's material, or "not covered" (reported 2026-09-24). A recommendation
# is a question ACROSS schemes: it is answered here, deterministically, by
# matching what the user says about themselves to who each scheme is for.
#
# Who each scheme is for — taken from each scheme's own reference FAQ
# (data/reference/*_general_faq.md, FOCUS_LEGACY_FAQ.md), not invented. Focus
# Plus in particular is NOT for any farmer: its FAQ makes Producer Group
# membership the precondition.
_SCHEME_FIT = {
    "MGNREGA": (
        "guaranteed paid work",
        "any adult member of a rural household who is willing to do unskilled manual "
        "work can get up to 100 days of paid work a year, with no income test — you "
        "register for a job card at your Gram Panchayat."),
    "PMAY-G": (
        "a permanent (pucca) house",
        "for rural households that do not own a pucca house and have not had government "
        "housing help before; households are identified from the SECC / Awaas+ list "
        "through the Gram Panchayat."),
    "Focus Plus": (
        "direct cash support for farm and livelihood activity",
        "cash paid directly (DBT) to Meghalaya households that are members of a Producer "
        "Group of 10 or more, for farm inputs and extra income activities such as "
        "piggery, poultry, horticulture, ginger or turmeric."),
    "Focus Legacy": (
        "seed money for a farmers' Producer Group",
        "farmers organised into (or willing to form) a Producer Group get ₹5,000 per "
        "member as seed / working capital; urban groups of 10 or more members are also "
        "eligible — register at the C&RD Block office."),
    "CM Elevate": (
        "starting or growing a business",
        "individuals, registered businesses, SHGs and Producer Groups in Meghalaya can "
        "get support for a venture in one of 15 sectors — piggery, poultry, dairy, goat "
        "farming, warehousing, tourism vehicles and more — or under the \"Any Business "
        "Venture\" category; you apply on the MeghalayaOne portal."),
}
# (profile, what it says about the user, pattern, schemes in order of fit)
_PROFILE_RULES = (
    ("business", "starting or running a business",
     re.compile(r"\bstart[\s-]?ups?\b|\bstart(?:ing|ed)?\s+(?:a\s+|an\s+|my\s+|our\s+|his\s+|her\s+|"
                r"their\s+|the\s+)?(?:own\s+|new\s+|small\s+)*(?:business|company|venture|enterprise|"
                r"shop|unit|firm)\b|\bbusiness\w*\b|\benterprises?\b|\bentrepreneur\w*|"
                r"\bself[\s-]?employ\w*|\bventures?\b|\bcompany\b|\bshop\b|\bmsme\b|"
                r"\bpiggery\b|\bpoultry\b|\bdairy\b|\bgoat\w*|\btourism\b|\btaxi\b|"
                r"\bwarehouse\b|\bhomestay\b", re.IGNORECASE),
     ["CM Elevate"]),
    ("group", "part of a farmers' group / SHG",
     re.compile(r"\bproducer\s+groups?\b|\bfarmers?['’]?\s+groups?\b|\bgroup\s+of\s+farmers\b|"
                r"\bshgs?\b|\bself[\s-]?help\s+groups?\b|\bcollective\b|\bco-?operative\b|\bpgs?\b",
                re.IGNORECASE),
     ["Focus Legacy", "Focus Plus", "CM Elevate"]),
    ("farmer", "a farmer",
     re.compile(r"\bfarm(?:er|ers|ing)?\b|\bagricultur\w*|\bcultivat\w*|\bcrops?\b|\bkisan\b|"
                r"\bhorticultur\w*|\bkheti\b", re.IGNORECASE),
     ["Focus Plus", "Focus Legacy", "CM Elevate", "MGNREGA"]),
    ("housing", "in need of a house",
     re.compile(r"\bhouse\b|\bhouses\b|\bhome\b|\bhousing\b|\bkutcha\b|\bhomeless\b|\bshelter\b|"
                r"\broof\b|\bpucca\b", re.IGNORECASE),
     ["PMAY-G"]),
    ("work", "looking for work / income",
     re.compile(r"\bjobs?\b|\bunemploy\w*|\bemployment\b|\blabou?r\w*|\bwages?\b|\bdaily\s+wage\b|"
                r"\bneed\s+(?:some\s+)?(?:work|income|money)\b|\blooking\s+for\s+work\b|"
                r"\bno\s+(?:work|income|job)\b", re.IGNORECASE),
     ["MGNREGA"]),
    ("rural", "living in a rural area",
     re.compile(r"\brural\b|\bvillages?\b|\bgaon\b|\bcountryside\b", re.IGNORECASE),
     ["MGNREGA", "PMAY-G"]),
)
_RECOMMEND_CUE = re.compile(
    r"\bsuggest\w*|\brecommend\w*|\badvi[cs]e\b|\bbest\s+(?:suited\s+)?schemes?\b|"
    r"\bright\s+scheme\b|\bsuit(?:s|able|ed)?\b|\bfits?\s+(?:me|him|her|us|them|my|our)\b|"
    r"\b(?:which|what)\s+schemes?\s+(?:should|can|could|would|will|do|does)\s+"
    r"(?:i|we|he|she|they|my|our|you\s+(?:suggest|recommend))\b|"
    r"\b(?:which|what)\s+schemes?\s+(?:is|are|would\s+be)\s+(?:the\s+)?"
    r"(?:best|good|right|suitable|useful)\s+for\b|"
    r"\bschemes?\s+for\s+(?:me|him|her|us|them|my|our|a|an)\b|\bany\s+schemes?\s+for\b|"
    r"\beligible\s+for\s+(?:which|what)\b|\bhelp\s+(?:me|him|her|us)\s+(?:choose|find|pick)\b",
    re.IGNORECASE)
# Looser question shapes that are a recommendation ONLY when the message also
# describes the person's need ("my friend is starting a startup, which scheme
# benefits him"). On their own they are far too common in data and listing
# questions ("which scheme has the most houses", "is there any scheme that can
# help my family?"), which keep their existing handling.
_RECOMMEND_CUE_WITH_NEED = re.compile(
    r"\b(?:which|what)\s+schemes?\b|\bany\s+schemes?\b|\bis\s+there\s+(?:any|a)\b|"
    r"\bschemes?\b[^?.!]{0,40}\b(?:benefit|help|support|useful|avail|apply|get|give|offer)\w*|"
    r"\b(?:benefit|help|support)\w*\s+(?:to\s+|for\s+)?(?:me|him|her|us|them|my|our|his)\b|"
    r"\b(?:can|could|will|would)\s+(?:i|he|she|we|they|my\s+\w+)\s+(?:get|avail|apply|benefit)\b|"
    r"\bwhat\s+(?:can|could|will)\s+(?:i|he|she|we|they|my\s+\w+)\s+get\b",
    re.IGNORECASE)
_RECOMMENDATION_LEAD = "Based on what you've told me"


def _user_profile(text: str) -> list[tuple[str, str, list[str]]]:
    return [(k, desc, schemes) for k, desc, rx, schemes in _PROFILE_RULES if rx.search(text or "")]


def _latest_profile(session: "Session | None", n: int = 4) -> list[tuple[str, str, list[str]]]:
    """The profile from the MOST RECENT of the user's last n messages that
    described someone. Never a blend of several messages: "my friend is
    starting a startup" and later "I am a farmer" are two different people,
    and merging them recommended the friend's scheme to the farmer."""
    for t in reversed((getattr(session, "turns", None) or [])[-n:]):
        prof = _user_profile(t.raw_question or "")
        if prof:
            return prof
    return []


# "Is there any Producer Group named Nongstoin PG?" is a NAME LOOKUP in the data,
# not a person describing themselves: the loose cue "is there any" plus the
# "group" profile word sent it to the recommender, which replied with a list of
# schemes (Focus Legacy QA TC-13, 2026-09-25). A naming word attached to a group
# noun vetoes the recommendation; "my friend named Ram is a farmer" is untouched.
_GROUP_NAME_LOOKUP = re.compile(
    r"\b(?:producer\s+groups?|pgs?|groups?|shgs?)\s+(?:(?:is|are|was)\s+)?"
    r"(?:named|called|titled|with\s+(?:the\s+)?name|by\s+(?:the\s+)?name)\b",
    re.IGNORECASE)


def _is_recommendation_request(question: str) -> bool:
    q = question or ""
    if _WHY_CHOICE.search(q):
        return False
    if _GROUP_NAME_LOOKUP.search(q):
        return False
    if not (_RECOMMEND_CUE.search(q)
            or (_RECOMMEND_CUE_WITH_NEED.search(q) and _user_profile(q))):
        return False
    # "is PMAY-G suitable for me?" is an eligibility question about THAT scheme
    # — its own reference docs answer it (the normal knowledge route).
    if _named_schemes(q):
        return False
    if _RECOMMEND_DATA_GUARD.search(q):
        return False
    # It must actually be about a scheme: a scheme word, or a need a scheme can
    # meet stated in the message itself. "give me some suggestions" on its own
    # is not a scheme question — reading it as one borrowed the previous turn's
    # need and recommended PMAY-G to "i want to rob bank, give me some
    # suggestions" (reported 2026-09-24).
    return bool(_SCHEME_CONTEXT.search(q) or _user_profile(q))


_SCHEME_CONTEXT = re.compile(
    r"\bschemes?\b|\byojana\b|\byojna\b|\bprogramm?e?s?\b|\bgovt\.?\b|\bgovernment\b|"
    r"\bsubsid\w*|\bassistance\b|\bbenefits?\b|\beligible\b|\beligibility\b|\bapply\b",
    re.IGNORECASE)


# Only FIGURE words — unlike the pick guard, "house" / "village" / "district"
# describe the user's situation here ("I live in a village, I need a house").
_RECOMMEND_DATA_GUARD = re.compile(
    r"\b(?:how\s+many|how\s+much|total|number\s+of|count|sum|average|top\s*\d|"
    r"highest|lowest|most|least|maximum|minimum|largest|biggest|smallest|"
    r"rank\w*|expenditure|person[\s-]?days?|disburs\w*|amount\s+(?:of|paid|spent)|"
    r"data|figures?|statistics|stats|20\d\d)\b",
    re.IGNORECASE)


def _recommendation_clarification(question: str) -> "ClarificationNeeded":
    stem = question.strip().rstrip(" ?.")
    needs = [("I need paid work", "I need work"), ("I need a house", "I need a house"),
             ("I am a farmer", "I am a farmer"),
             ("I am in a farmers' group / SHG", "I am in a farmers' producer group"),
             ("I want to start a business", "I want to start a business")]
    return ClarificationNeeded(
        "Happy to suggest one — tell me a little about the need, so I can match it to "
        "the right scheme:",
        options=[{"label": lbl, "question": f"{stem} — {why}"} for lbl, why in needs],
        rule="recommendation-needs-profile",
    )


def _scheme_recommendation_answer(question: str, session: "Session | None") -> "dict | None":
    if not _is_recommendation_request(question):
        return None
    profile = _user_profile(question)
    from_history = False
    if not profile:
        profile = _latest_profile(session)
        from_history = bool(profile)
    if not profile:
        raise _recommendation_clarification(question)
    ranked: list[str] = []
    for _k, _d, schemes in profile:
        for s in schemes:
            if s not in ranked:
                ranked.append(s)
    about = " and ".join(d for _k, d, _s in profile)
    lead = f"{_RECOMMENDATION_LEAD} ({about}{', from earlier in our chat' if from_history else ''}), "
    lead += ("this scheme fits best:" if len(ranked) == 1 else "these schemes fit, best match first:")
    lines = []
    for i, s in enumerate(ranked, 1):
        what, who = _SCHEME_FIT[s]
        lines.append(f"{i}. **{s}** — for {what}: {who}")
    tail = ("\n\nThese are suggestions based on each scheme's published eligibility — the "
            "implementing department makes the final decision. Ask me \"how to apply for "
            f"{ranked[0]}\" for the steps.")
    logger.info("scheme recommendation: %r -> %s (profile=%s)", question, ranked,
                [k for k, _d, _s in profile])
    return {"route": "knowledge", "intent": "RAG", "confidence": "high", "sources": [],
            "answer": f"{lead}\n\n" + "\n".join(lines) + tail,
            **_empty_data_fields(), "schemes": [ranked[0]]}


# ── "my friend is starting a startup, which PMAY-G benefits him?" ───────────
# A named scheme plus a stated need it does NOT serve. The scheme's own documents
# cannot say "this is the wrong scheme for you" — they either describe the scheme
# anyway or reply that startups are "not mentioned" (reported 2026-09-24). Said
# plainly here, with the scheme that does fit. When the named scheme DOES fit the
# need, this steps aside and its own documents answer, exactly as before.
_FIT_CUE = re.compile(r"\beligib\w*|\bsuit\w*|\bfits?\b|\buseful\b|\bgood\s+for\b|\bright\s+for\b",
                      re.IGNORECASE)


def _scheme_fit_check_answer(question: str) -> "dict | None":
    q = question or ""
    if _WHY_CHOICE.search(q) or _RECOMMEND_DATA_GUARD.search(q):
        return None
    if not (_FIT_CUE.search(q) or _RECOMMEND_CUE.search(q) or _RECOMMEND_CUE_WITH_NEED.search(q)):
        return None
    folded = {d: p for p, ds in _PROGRAMME_DATASETS.items() for d in ds}
    named = list(dict.fromkeys(folded.get(s, s) for s in _named_schemes(q)))
    if len(named) != 1 or named[0] not in _SCHEME_FIT:
        return None
    profile = _user_profile(q)
    if not profile:
        return None
    ranked: list[str] = []
    for _k, _d, schemes in profile:
        ranked += [s for s in schemes if s not in ranked]
    x = named[0]
    if x in ranked:
        return None
    about = " and ".join(d for _k, d, _s in profile)
    what, who = _SCHEME_FIT[x]
    parts = [f"**{x}** isn't meant for this need ({about}): it is for {what} — {who}"]
    best = ranked[0]
    parts.append(f"For this need, **{best}** fits: {_SCHEME_FIT[best][1]}")
    if len(ranked) > 1:
        parts.append("Also worth a look: " + ", ".join(f"**{s}** ({_SCHEME_FIT[s][0]})"
                                                      for s in ranked[1:]) + ".")
    parts.append(f"Ask me \"how to apply for {best}\" for the steps.")
    logger.info("scheme fit check: %r -> %s does not fit %s; suggest %s", q, x,
                [k for k, _d, _s in profile], best)
    return {"route": "knowledge", "intent": "RAG", "confidence": "high", "sources": [],
            "answer": "\n\n".join(parts), **_empty_data_fields(), "schemes": [best]}


# ── "Why did you choose / give MGNREGA (instead of Focus)?" ─────────────────
# A question about the BOT'S OWN previous answer, not about a scheme's rules —
# no reference document can answer it, so the knowledge route either repeated
# the overview or said "not covered" (reported 2026-09-24). It is answered from
# what the previous answer actually was: a pick, a recommendation, or a scheme
# the conversation was already on.
_WHY_CHOICE = re.compile(
    r"\bwhy\b[^?.!]{0,40}\b(?:choose|chose|chosen|pick|picked|picking|give|given|gave|"
    r"suggest\w*|recommend\w*|select\w*|instead|rather|only|not)\b",
    re.IGNORECASE)
_INSTEAD_OF = re.compile(r"\b(?:instead\s+of|rather\s+than|over|and\s+not|not)\s+(?P<alt>.+)$",
                         re.IGNORECASE)


def _why_choice_answer(question: str, session: "Session | None") -> "dict | None":
    q = question or ""
    if not _WHY_CHOICE.search(q):
        return None
    last = getattr(session, "last_turn", None) if session is not None else None
    if last is None:
        return None
    folded = {d: p for p, ds in _PROGRAMME_DATASETS.items() for d in ds}
    # The scheme the question says was given, and the alternative it names.
    m = _INSTEAD_OF.search(q)
    alt_text = m.group("alt") if m else ""
    given_text = q[: m.start()] if m else q
    given = [folded.get(s, s) for s in _named_schemes(given_text)]
    given = given or [folded.get(s, s) for s in (last.schemes or [])]
    alts = [folded.get(s, s) for s in _named_schemes(alt_text)] if alt_text else []
    if alt_text and not alts and _BARE_FOCUS_WORD.search(alt_text):
        alts = ["Focus Plus", "Focus Legacy"]
    alts = [a for a in dict.fromkeys(alts) if a in _SCHEME_FIT and a not in given]
    x = given[0] if given and given[0] in _SCHEME_FIT else None
    if x is None:
        return None

    was_pick = _PICK_OVERVIEW_PREFIX in (last.question or "")
    was_rec = (last.answer or "").startswith(_RECOMMENDATION_LEAD)
    if was_pick:
        why = (f"I picked **{x}** only because you left the choice to me and it was the "
               "first scheme on my list we hadn't covered yet — it isn't a ranking, and it "
               "doesn't mean it suits you best.")
    elif was_rec:
        # Where each scheme actually sat in that list — the question may assume
        # one was left out when it was in fact ranked higher.
        listed = re.findall(r"^\d+\.\s+\*\*(.+?)\*\*", last.answer or "", re.MULTILINE)
        rank = {s: i + 1 for i, s in enumerate(listed)}
        if x in rank and len(listed) > 1:
            why = (f"**{x}** was number {rank[x]} of {len(listed)} in my suggestions, not the "
                   f"only one — it is there for {_SCHEME_FIT[x][0]}: {_SCHEME_FIT[x][1]}")
        else:
            why = (f"I suggested **{x}** because of what you told me about yourself: it is for "
                   f"{_SCHEME_FIT[x][0]} — {_SCHEME_FIT[x][1]}")
        ranked_above = [a for a in alts if a in rank and (x not in rank or rank[a] < rank[x])]
        if ranked_above:
            why += ("\n\nIn fact " + " and ".join(f"**{a}** (number {rank[a]})" for a in ranked_above)
                    + (" was" if len(ranked_above) == 1 else " were")
                    + f" ranked above {x} in that same list.")
            alts = [a for a in alts if a not in ranked_above]
    else:
        why = (f"My last answer drew only on **{x}**'s reference material because our "
               f"conversation was about {x} at that point — I didn't compare it with the "
               "other schemes.")
    parts = [why]
    for a in alts:
        what, who = _SCHEME_FIT[a]
        parts.append(f"**{a}** is for {what}: {who}")
    profile = _user_profile(q) or _latest_profile(session)
    if profile and not was_rec:
        ranked: list[str] = []
        for _k, _d, schemes in profile:
            ranked += [s for s in schemes if s not in ranked]
        about = " and ".join(d for _k, d, _s in profile)
        parts.append(f"From what you've said ({about}), the best match is **{ranked[0]}**"
                     + (f", then {', '.join(ranked[1:3])}" if len(ranked) > 1 else "") + ".")
    elif not profile:
        parts.append("Tell me what you need — paid work, a house, farming support or starting "
                     "a business — and I'll suggest the scheme that fits best.")
    return {"route": "knowledge", "intent": "RAG", "confidence": "high", "sources": [],
            "answer": "\n\n".join(parts), **_empty_data_fields(),
            "schemes": [alts[0] if alts else x]}


def _last_turn_was_pick(session: "Session | None") -> bool:
    last = getattr(session, "last_turn", None) if session is not None else None
    return bool(last and _PICK_OVERVIEW_PREFIX in (last.question or ""))


async def _scheme_pick_answer(question: str, session: "Session | None") -> "dict | None":
    if not (_is_scheme_pick_request(question)
            or (_PICK_CONTINUE.match(question or "") and _last_turn_was_pick(session))):
        return None
    scheme, chosen = _pick_scheme(question, session)
    overview_q = _scheme_overview_question(scheme)
    kb = None
    try:
        kb = await rag.answer_from_kb(overview_q, scheme=scheme)
    except Exception:  # noqa: BLE001 — fall back to the summary below
        logger.warning("scheme pick: knowledge lookup failed for %s", scheme, exc_info=True)
    lead = (f"I'll pick **{scheme}** — here are its key points:" if chosen
            else f"Here are the key points of **{scheme}**:")
    if kb:
        body, conf, sources = kb["answer"], kb["confidence"], kb["sources"]
    else:
        # No passage retrieved (KB unreachable, or nothing scored): still answer
        # the request from the scheme's own one-line summary, never "not covered".
        body, conf, sources = f"- {_SCHEME_USER_SUMMARY[scheme]}", "medium", []
    others = [s for s in _pickable_schemes() if s != scheme]
    tail = ("\n\nWant me to explain another one? I also cover "
            + ", ".join(others[:-1]) + " and " + others[-1] + "." if chosen and others else "")
    logger.info("scheme pick: %r -> %s (chosen_by_bot=%s)", question, scheme, chosen)
    return {"route": "knowledge", "intent": "RAG", "confidence": conf,
            "answer": f"{lead}\n\n{body}{tail}", "sources": sources,
            "rewritten_question": overview_q,
            **_empty_data_fields(), "schemes": [scheme]}


def _scheme_listing_answer(question: str) -> "dict | None":
    explicit = _SCHEME_LISTING_CUE.search(question or "")
    if not explicit:
        if not _SCHEME_HELP_CUE.search(question or ""):
            return None
        # A "help me" ask that already names scheme-specific vocabulary (a
        # house, job cards, farmer cash, an enterprise, ...) has a clear
        # intent to route on — leave it to the normal KNOWLEDGE/RAG path
        # instead of burying that signal under the generic four-scheme list.
        if _infer_scheme_from_terms(question) is not None:
            return None
    lines = _scheme_listing_lines()
    answer = (
        f"I cover {_count_word(len(lines))} Meghalaya government schemes:\n\n" + "\n".join(lines) +
        "\n\nAsk me about any of these — eligibility, benefits, how to apply, "
        "or the actual data (numbers, by district or year)."
    )
    return {"route": "knowledge", "intent": "RAG", "confidence": "high", "sources": [],
            "answer": answer, **_empty_data_fields()}


# "What is the difference between the schemes?" / "compare MGNREGA and PMAY-G" as
# a KNOWLEDGE question — no per-scheme reference doc contains a cross-scheme
# comparison, so plain RAG retrieval reliably comes back "not covered". Compose
# a structured side-by-side directly from _SCHEME_USER_SUMMARY instead.
_SCHEME_COMPARISON_CUE = re.compile(
    r"\bdifference between\b[^?]{0,40}\bschemes?\b|\bschemes?\b[^?]{0,40}\bdiffer(?:ence)?\b|"
    r"\bcompare\b[^?]{0,40}\bschemes?\b|\bhow (?:do|does)\b[^?]{0,40}\bschemes?\b[^?]{0,20}\bdiffer\b|"
    r"\bdifference between\b.{0,60}\b(mgnrega|pmay|focus\s*plus|cm\s*elevate)\b.{0,20}\b(and|vs\.?|versus)\b",
    re.IGNORECASE,
)
# "compare Focus Plus and CM Elevate schemes BY BENEFICIARIES" / "... by amount" /
# "... by expenditure" names a real figure to pull from megh_db, not "how do the
# schemes differ conceptually" — _SCHEME_COMPARISON_CUE's bare "compare ... schemes"
# still matches that (it doesn't require a metric to be absent), so without this
# guard _scheme_comparison_answer intercepts a genuine statistics question at step
# 0-a, before classify_intent / DATA routing ever runs, and answers it with the
# canned RAG-style scheme blurb instead of a queried number.
_SCHEME_COMPARISON_METRIC_GUARD = re.compile(
    r"\b(beneficiar\w*|amount|amounts|expenditure|spend\w*|wages?|"
    r"person[\s-]?days?|job\s?cards?|houses?|disburs\w*|payments?|"
    r"number of|how many|how much|count of|total|percentage|per ?cent|"
    r"\brate\b)\b",
    re.IGNORECASE,
)


def _scheme_comparison_clarification() -> "ClarificationNeeded":
    names = list(SCHEME_CATALOG)
    options = [
        {"label": f"{a} vs {b}", "question": f"difference between {a} and {b}"}
        for a, b in itertools.combinations(names, 2)
    ]
    _all = ", ".join(names[:-1]) + " and " + names[-1]
    _any = ", ".join(names[:-1]) + ", or " + names[-1]
    options.append({"label": f"All {len(names)} schemes",
                     "question": f"difference between {_all}"})
    return ClarificationNeeded(
        f"Which schemes would you like to compare — {_any}? Pick a pair, or compare "
        f"all {len(names)}.",
        options=options,
        rule="scheme-comparison-not-specified",
    )


def _scheme_comparison_answer(question: str) -> "dict | None":
    if not _SCHEME_COMPARISON_CUE.search(question or ""):
        return None
    if _SCHEME_COMPARISON_METRIC_GUARD.search(question or ""):
        return None  # names a real figure — let classify_intent send it to DATA
    named = _named_schemes(question)
    if len(named) == 1 or (not named and _infer_scheme_from_terms(question) is not None):
        # The question already resolves to exactly one of the 4 top-level schemes
        # — this is a WITHIN-scheme comparison ("compare Piggery and Poultry
        # schemes under CM Elevate", "compare the three PRIME schemes"), not a
        # cross-scheme "MGNREGA vs PMAY-G" ask. CM Elevate's 15 sub-units are
        # themselves called "schemes", so the bare _SCHEME_COMPARISON_CUE word
        # "scheme(s)" fires here too; asking "which of the 4 schemes?" is
        # nonsensical when the question already named the one it means (confirmed
        # live 2026-09-11: "compare Piggery and Poultry schemes under CM Elevate"
        # raised the 4-way clarification instead of running the sub-scheme
        # comparison in cmelevate_few_shot.yaml). Let normal DATA routing handle it.
        return None
    if len(named) < 2:
        raise _scheme_comparison_clarification()
    targets = named
    lines = [f"- **{name}** — {_SCHEME_USER_SUMMARY.get(name, '')}" for name in targets]
    answer = (
        "Here's a high-level comparison:\n\n" + "\n".join(lines) +
        "\n\nAsk about a specific one — eligibility, benefits, or the application "
        "process — for more detail."
    )
    return {"route": "knowledge", "intent": "RAG", "confidence": "high", "sources": [],
            "answer": answer, **_empty_data_fields()}


# ── Named a scheme we simply don't hold ─────────────────────────────────────
# A user can reasonably name another Meghalaya / central scheme ("PM-KISAN",
# "Ujjwala", "Jal Jeevan"). The right answer is not the generic "which scheme?"
# prompt — it's to say plainly that only four schemes are loaded right now,
# then let them pick one. Keep this list to real scheme names/aliases; a bare
# word that is also scheme vocabulary must not appear. CM Elevate is now a
# supported scheme (see _SCHEME_NAME_PATTERN) and must NOT appear here.
_UNSUPPORTED_SCHEME = re.compile(
    r"\b("
    r"prime\s+meghalaya|meghalaya\s+prime|"
    r"pm[\s-]?kisan|pmkisan|kisan\s+samman|"
    r"pmay[\s-]?u\b|pmay\s+urban|awas\s+yojana\s+urban|"
    r"pmgsy|gram\s+sadak|"
    r"pmuy|ujjwala|ujwala|"
    r"saubhagya|"
    r"jal\s+jeevan(\s+mission)?|jjm\b|har\s+ghar\s+jal|nal\s+se\s+jal|"
    r"ayushman(\s+bharat)?|pm[\s-]?jay|pmjay|"
    r"mhis\b|cmhis\b|megha\s+health|"
    r"nsap\b|ignoaps|old[\s-]?age\s+pension|widow\s+pension|disability\s+pension|"
    r"nrlm\b|day[\s-]?nrlm|aajeevika|ajeevika|livelihoods?\s+mission|"
    r"swachh\s+bharat|sbm\b|nirmal\s+bharat|"
    r"mid[\s-]?day\s+meal|pm[\s-]?poshan|"
    r"icds\b|anganwadi|poshan\s+abhiyaan?|"
    r"kanyashree|ladli|sukanya|"
    r"atal\s+pension|apy\b|"
    r"mudra|pmmy\b|stand[\s-]?up\s+india|"
    r"kisan\s+credit\s+card|kcc\b|"
    r"fasal\s+bima|pmfby|crop\s+insurance|"
    r"e[\s-]?shram|"
    r"nfsa\b|one\s+nation\s+one\s+ration|public\s+distribution|ration\s+card"
    r")\b",
    re.IGNORECASE,
)


def _unsupported_scheme_named(question: str) -> "str | None":
    """The name of a scheme the user asked about that we don't hold — or None.
    Only fires when NO supported scheme is also named, so mixed questions
    ("compare CM Elevate with MGNREGA") still route normally. The unsupported
    match is blanked out before the supported-scheme check so a near-miss like
    "PMAY-U" isn't swallowed by the PMAY-G pattern."""
    m = _UNSUPPORTED_SCHEME.search(question or "")
    if not m:
        return None
    without = (question[:m.start()] + " " + question[m.end():])
    if any(p.search(without) for p in _SCHEME_NAME_PATTERN.values()):
        return None
    return m.group(0)


def _unsupported_scheme_clarification(question: str, name: str) -> "ClarificationNeeded":
    # Drop the unsupported scheme name (plus any preposition left dangling by
    # its removal, e.g. "... years in pm-kisan?" -> "... years") so the
    # resumed one-tap question reads cleanly.
    stem = re.sub(re.escape(name), " ", question, flags=re.IGNORECASE)
    stem = re.sub(r"\s+", " ", stem).strip().rstrip(" ?.")
    stem = re.sub(r"\s+(?:in|for|of|under|from|about|on)$", "", stem, flags=re.IGNORECASE).strip()
    stem = stem or question.strip().rstrip(" ?.")
    options = [
        {"label": "MGNREGA (rural employment)",
         "question": _scheme_option_question(stem, "MGNREGA")},
        {"label": "PMAY-G (rural housing)",
         "question": _scheme_option_question(stem, "PMAY-G")},
        {"label": "Focus Plus (farmer cash benefit)",
         "question": _scheme_option_question(stem, "Focus Plus")},
        {"label": "CM Elevate (livelihood / enterprise schemes)",
         "question": _scheme_option_question(stem, "CM Elevate")},
        {"label": "Focus Legacy (producer group payments)",
         "question": _scheme_option_question(stem, "Focus Legacy")},
        {"label": "CM Elevate Legacy (sanctions & disbursements)",
         "question": _scheme_option_question(stem, "CM Elevate Legacy")},
        {"label": "Compare across schemes",
         "question": (f"{stem} across MGNREGA, PMAY-G, Focus Plus, CM Elevate, "
                      f"Focus Legacy and CM Elevate Legacy")},
    ]
    # Names the scheme asked for, says plainly that it is outside what is
    # loaded, then points somewhere useful. Deliberately about SCHEME COVERAGE
    # rather than "no data": the user asked about a real government scheme that
    # simply isn't one of the four here, and "I don't have information about
    # that" reads as though the assistant is broken rather than out of scope
    # (reported 2026-09-18).
    # Echo the scheme name back in a presentable form — the matched text is
    # whatever the user typed ("ujjwala", "pm kisan"), and a reply that opens
    # with a lowercase scheme name reads careless. An all-caps acronym
    # ("PM-KISAN", "JJM") is already right and left alone.
    _display = " ".join(w if w.isupper() else w.capitalize()
                        for w in name.strip().split())
    return ClarificationNeeded(
        f"{_display} isn't one of the schemes I cover, so I can't answer questions "
        "about it — not its rules, eligibility or its data. I cover these Meghalaya "
        "schemes: MGNREGA (rural employment), PMAY-G (rural housing), Focus Plus "
        "(farmer cash benefit), CM Elevate (livelihood and enterprise applications), "
        "Focus Legacy (producer group payments) and CM Elevate Legacy (CM-ELEVATE "
        "sanctions and disbursements). "
        "If one of those is what you need, pick it below and I'll take the question "
        "from there.",
        options=options,
        rule="scheme-not-available",
    )


# ── Bank / financial-channel details — not held for most loaded schemes ─────
# Focus Plus now exposes bank_name_raw via curated.v_focus_plus (added so
# bank-wise disbursement questions can be answered), but it has no
# account-number/IFSC field, so those specific asks still need the
# not-held explanation. PMAY-G, CM Elevate and MGNREGA never captured any
# bank field at ingest at all. Without this check an account/IFSC question
# reaches SQL generation, which correctly finds no column to use but can only
# hand back a bare "not available in this data" — true, but it doesn't say
# *why*, so route it to a real explanation up front.
_BANK_REQUESTED = re.compile(
    r"\bbanks?\b|\bbanking\b|\bifsc\b|\bbank[\s-]?account|\baccount[\s-]?number|"
    r"\bbank[\s-]?wise\b|\bbank[\s-]?transfer|\bdbt\b|\blifcom\b",
    re.IGNORECASE,
)
# Focus Plus only lacks account-number/IFSC fields — bare bank-name questions
# ("which bank", "bank-wise disbursement") should reach SQL generation instead.
_BANK_ACCOUNT_DETAIL_REQUESTED = re.compile(
    r"\bifsc\b|\bbank[\s-]?account|\baccount[\s-]?number|\bbank[\s-]?transfer|"
    r"\bdbt\b",
    re.IGNORECASE,
)
# CM Elevate Legacy's loan_entity holds only the categories 'Bank' / 'LIFCOM'.
# These ask for an individual bank or branch, which is not recorded.
_LENDER_DETAIL_REQUESTED = re.compile(
    r"\bbranch(?:es)?\b|\bbank[\s-]?names?\b|\bname of (?:the )?banks?\b|"
    r"\bwhich (?:particular |specific )?banks?\b|\bsbi\b|\bstate bank\b|\bmrb\b|"
    r"\bmeghalaya rural bank\b|\bcooperative bank\b",
    re.IGNORECASE,
)
# Focus Legacy holds bank_name and ifsc_code (v_focus_legacy), so only an
# account-number / transfer-status question is refused for it.
_FOCUS_LEGACY_BANK_NOT_HELD = re.compile(
    r"\bbank[\s-]?account|\baccount[\s-]?number|\bbank[\s-]?transfer|\bdbt\b",
    re.IGNORECASE,
)
_BANK_NOT_HELD_TEXT = {
    "Focus Plus": (
        "Account numbers and IFSC codes aren't held for Focus Plus — only the "
        "bank name is. Shall I answer using bank name instead?"
    ),
    # There was no Focus Legacy entry, so a Focus Legacy bank question raised
    # KeyError here — which is where the "which bank?" chip for Focus Legacy
    # leads (Focus Plus use-case QA 2026-09-27, FOCUS-029 / KI-063).
    "Focus Legacy": (
        "Account numbers, bank-transfer status and DBT outcomes aren't exposed for "
        "Focus Legacy — only the bank name and IFSC code are. Shall I answer using "
        "bank name or IFSC code instead?"
    ),
    "CM Elevate": (
        "There is no loan-channel field in the CM Elevate applications data — Bank and "
        "LIFCOM are recorded only in CM Elevate Legacy (the sanction and disbursement "
        "records). Shall I answer from CM Elevate Legacy, or on applications by scheme "
        "or district instead?"
    ),
    "CM Elevate Legacy": (
        "Individual bank names, branches, IFSC codes and account numbers are not held "
        "for CM Elevate Legacy — the only lender values recorded are 'Bank' and "
        "'LIFCOM'. Shall I show loans by those two lender categories instead?"
    ),
    "PMAY-G": (
        "Bank transfer status, DBT outcomes and account details are not held for "
        "PMAY-G — only the amount released and the number of installments are. "
        "Shall I use those instead?"
    ),
    "MGNREGA": (
        "Bank details are not held for MGNREGA — only wages, person-days, job "
        "cards and expenditure are. Shall I answer using one of those instead?"
    ),
}
_BANK_GENERIC_TEXT = (
    "Bank name is only held for Focus Plus and Focus Legacy — MGNREGA, PMAY-G "
    "and CM Elevate never captured a bank field at ingest. Focus Legacy also "
    "holds the IFSC code; no scheme exposes account numbers. Shall I answer "
    "using Focus Plus or Focus Legacy bank name, or by scheme, district, block "
    "or village instead?"
)


def _named_or_inferred_schemes(question: str) -> list[str]:
    named = [s for s, rx in _SCHEME_NAME_PATTERN.items() if rx.search(question)]
    return named or _infer_scheme_from_terms(question) or []


def _bank_clarification(question: str) -> "ClarificationNeeded | None":
    schemes = _named_or_inferred_schemes(question)
    is_account_detail = bool(_BANK_ACCOUNT_DETAIL_REQUESTED.search(question))
    if not is_account_detail and schemes == ["Focus Plus"]:
        # Bare bank-name question scoped to Focus Plus only — bank_name_raw is
        # queryable via curated.v_focus_plus, so let SQL generation handle it.
        return None
    if schemes == ["Focus Legacy"] and not _FOCUS_LEGACY_BANK_NOT_HELD.search(question):
        # bank_name / ifsc_code are real columns on v_focus_legacy
        # ("Which banks handle Focus Legacy payments?").
        return None
    if not schemes and not is_account_detail:
        # "Which bank processed the highest total disbursement?" names no
        # scheme. Only Focus Plus and Focus Legacy hold a bank name, so offer
        # exactly those two as chips. This pause used to carry NO options, so
        # the officer had to type "Focus Plus" — and a typed reply to a pause
        # the router does not remember went through as the brand-new question
        # "Focus Plus" (a KB scheme overview; the bank question was lost).
        # The rule is in SCHEME_PAUSE_RULES, so a typed scheme name now resumes
        # the question exactly as its chip does (Focus Plus use-case QA
        # 2026-09-27, FOCUS-029 / KI-063).
        stem = question.strip().rstrip(" ?.")
        return ClarificationNeeded(
            _BANK_GENERIC_TEXT,
            options=[{"label": f"{s} (bank name)", "question": _scheme_option_question(stem, s)}
                     for s in ("Focus Plus", "Focus Legacy")],
            rule="bank-scheme-not-specified",
        )
    if schemes == ["CM Elevate Legacy"] and not is_account_detail \
            and not _LENDER_DETAIL_REQUESTED.search(question):
        # "loans by Bank vs LIFCOM", "how many LIFCOM loans" — loan_entity is a
        # real, queryable lender column there. Only a named bank / branch /
        # account asks for something the data does not hold.
        return None
    text = _BANK_NOT_HELD_TEXT[schemes[0]] if len(schemes) == 1 else _BANK_GENERIC_TEXT
    return ClarificationNeeded(text, rule="column-not-held")


# ── Administrative expenditure — deliberately excluded from reporting ──────
# The raw source has adm_exp_rec / adm_exp_non_rec / adm_exp_total (MGNREGA) and
# an equivalent programme-expenditure figure (PMAY-G), but both are intentionally
# left out of the curated views/facts (data/mgnrega/README.md marks them
# **excluded**; mgnrega_classification_rules.yaml and pmay_classification_rules.yaml
# both define a dedicated "administrative_expenditure_requested" condition for
# exactly this). That YAML condition was never wired into the live pipeline
# (see the "deliberately NOT loaded here yet" note in annotations.py), so without
# this check the question reached SQL generation, which has no such column to
# read and either errors or returns a wrong/empty figure instead of explaining
# the exclusion.
_ADMIN_EXPENDITURE_REQUESTED = re.compile(
    r"\badministrative\s+(?:expenditure|expense|cost|spend(?:ing)?)\b|"
    r"\badmin\s+(?:expenditure|expense|cost)\b|\boverhead\s+(?:expenditure|cost)s?\b|"
    r"\bprogramme\s+expenditure\b|\bprogram\s+expenditure\b",
    re.IGNORECASE,
)
# The MGNREGA wording says the figure is EMPTY rather than absent, because that
# is what the data actually shows. curated.v_expenditure does expose
# admin_total_exp, but it is non-zero on exactly ONE of its 18,818 rows (₹0.90
# lakh, Alokdia / DEMDEMA block / FY 2023-24) against ₹362,866 lakh of total
# expenditure — docs/schema_for_developers.md records it as "effectively
# unpopulated". Saying it "isn't held" was misleading: a user who checks the
# schema finds the column and reasonably concludes the assistant is wrong
# (reported 2026-09-17, "admin expenditure in demdema"). Reporting the real
# 0.00 would be worse — it reads as a measured finding rather than an empty
# column — so the honest answer names the column, says it was never populated,
# and offers the figures that were.
_ADMIN_EXPENDITURE_NOT_HELD_TEXT = {
    "MGNREGA": (
        "MGNREGA's administrative expenditure column exists but was never populated "
        "at ingest — it is zero on every row but one in the whole state, so there is "
        "no real figure to report{scope}. The expenditure that IS recorded is "
        "unskilled wage, semi-skilled wage, material and total. Did you mean total "
        "expenditure instead?"
    ),
    "PMAY-G": (
        "Administrative and programme expenditure are not held for PMAY-G{scope} — only "
        "the per-house sanctioned and released amounts are. Did you mean amount released "
        "instead?"
    ),
}
_ADMIN_EXPENDITURE_GENERIC_TEXT = (
    "There is no usable administrative-expenditure figure{scope} in any scheme I cover. "
    "MGNREGA has the column but it was never populated (zero on every row but one "
    "statewide), and PMAY-G records only sanctioned and released amounts. Shall I "
    "answer using wage, material or total expenditure instead?"
)


# The place the question asked about, so the reply can say "…for Demdema"
# rather than answering in the abstract. Deliberately a light touch: the name
# is echoed back as the user typed it (title-cased), NOT resolved — this check
# runs long before resolve_entities, and a refusal about an empty column is the
# same refusal at any admin level, so there is nothing to disambiguate.
_ADMIN_EXP_PREP = r"(?:in|for|at|of|under|from|within)"
_ADMIN_EXP_PLACE_RE = re.compile(
    rf"\b{_ADMIN_EXP_PREP}\s+(?:{_ADMIN_EXP_PREP}\s+)*"
    r"(?P<name>[A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,2})\s*[?.!]*\s*$",
    re.IGNORECASE,
)
_ADMIN_EXP_NOT_A_PLACE = {
    "mgnrega", "mnrega", "nrega", "pmay", "pmay g", "pmayg", "awaas", "awas",
    "focus plus", "focusplus", "cm elevate", "cmelevate", "meghalaya",
    "the state", "each district", "every district", "all districts",
}


def _admin_expenditure_scope(question: str) -> str:
    """' in Demdema' when the question names a place, else ''."""
    # Strip any scheme name first. "…for PMAY-G in Tura" otherwise lets the
    # trailing-phrase match start at "PMAY-G" and produce "for Pmay-G In Tura",
    # which also duplicates the scheme the sentence has already named.
    text = question or ""
    for rx in _SCHEME_NAME_PATTERN.values():
        text = rx.sub(" ", text)
    m = _ADMIN_EXP_PLACE_RE.search(re.sub(r"\s+", " ", text).strip())
    if not m:
        return ""
    name = m.group("name").strip().rstrip(".,")
    if name.lower() in _ADMIN_EXP_NOT_A_PLACE or len(name) < 3:
        return ""
    return f" in {name.title()}"


def _admin_expenditure_clarification(question: str) -> "ClarificationNeeded":
    schemes = _named_or_inferred_schemes(question)
    if len(schemes) == 1 and schemes[0] in _ADMIN_EXPENDITURE_NOT_HELD_TEXT:
        text = _ADMIN_EXPENDITURE_NOT_HELD_TEXT[schemes[0]]
    else:
        text = _ADMIN_EXPENDITURE_GENERIC_TEXT
    return ClarificationNeeded(text.format(scope=_admin_expenditure_scope(question)),
                               rule="column-not-held")


def _needs_scheme_clarification(question: str) -> bool:
    """True when the question names no scheme, doesn't ask for a cross-scheme
    view outright, and uses no vocabulary that pins it to one scheme. In that
    case we ask rather than guess — a wrong scheme guess produces a confident
    but meaningless answer (e.g. 'PMAY has 0 paid beneficiaries')."""
    if not settings.SCHEME_CLARIFY_ENABLED:
        return False
    if any(p.search(question) for p in _SCHEME_NAME_PATTERN.values()):
        return False
    if _EXPLICIT_BOTH.search(question):
        return False
    # A superlative asked ACROSS schemes ("which scheme spent the most?") names
    # the scheme as its ANSWER — pausing to ask which scheme is meant would be
    # asking the user for the thing they came to find out.
    if _CROSS_SCHEME_SUPERLATIVE.search(question or ""):
        return False
    if _infer_scheme_from_terms(question) is not None:
        return False
    return True


# ── "Top how many?" clarification ───────────────────────────────────────────
# A ranking question over a dimension ("top districts", "which blocks spent the
# most", "rank villages by person-days") that names no count. Silently defaulting
# to a fixed top-N is a guess the user can't see or correct; a one-tap follow-up
# ("Top 3 / 5 / 10 / all") is cheaper than returning the wrong list length.
_RANKING_CUE = re.compile(
    r"\b(top|bottom|highest|lowest|most|least|best|worst|leading|leader|leads?|"
    r"rank(?:ed|ing)?|largest|smallest|biggest|maximum|minimum)\b",
    re.IGNORECASE,
)
# A plural dimension noun — a ranking only needs a length when it returns rows to
# cut. "which district spent the most" (singular) already means the single top row.
_RANK_DIMENSION = re.compile(
    r"\b(districts|blocks|villages|panchayats|gram panchayats|gps|g\.?p\.?s)\b",
    re.IGNORECASE,
)
# An explicit count is already in the question — nothing to ask.
_EXPLICIT_COUNT = re.compile(
    r"\btop[\s-]*\d+\b"
    r"|\b(?:top|bottom|first|last)\s+(?:one|two|three|four|five|six|seven|eight|nine|ten)\b"
    r"|\b\d+\s+(?:highest|lowest|largest|biggest|smallest|top|best|worst|most|least)\b",
    re.IGNORECASE,
)
# The user wants the whole list, not a ranked head — also nothing to ask.
_WANTS_ALL = re.compile(
    r"\b(all|every|each|entire|complete|full)\b.{0,20}\b(districts?|blocks?|villages?|"
    r"panchayats?|list|row|rows)\b"
    r"|\blist\s+(?:of\s+)?all\b|\bfor\s+all\b|\bacross\s+all\b|\bno limit\b|\bevery row\b|"
    # A free-text reply to the "top 3/5/10 or the complete list?" pause — the
    # dimension word (blocks/districts/...) already sits earlier in the merged
    # question, not right after "all", so these stand on their own instead of
    # requiring one of the words above within 20 chars.
    r"\ball of (?:them|it)\b|\beverything\b|"
    r"\b(?:show|give)\s+(?:me\s+)?all\b|\bthe\s+(?:complete|full)\s+list\b|"
    # A bare "all" as the whole reply, merged on as the trailing comma-fragment
    # ("...sanctioned in 2023-24, all") — anchored to end-of-string so an
    # unrelated mid-question "all" (e.g. "all of Meghalaya") isn't caught here;
    # that shape is already handled by the dimension-adjacent alternative above.
    r",\s*all\s*[?.]?\s*$",
    re.IGNORECASE,
)
_TOPN_CHOICES = (3, 5, 10)


def _needs_topn_clarification(question: str) -> bool:
    """True when the question asks for a ranked list over a dimension but never
    says how long the list should be."""
    if not settings.TOPN_CLARIFY_ENABLED:
        return False
    q = question or ""
    if not (_RANKING_CUE.search(q) and _RANK_DIMENSION.search(q)):
        return False
    if _EXPLICIT_COUNT.search(q) or _WANTS_ALL.search(q):
        return False
    return True


def _inject_topn(question: str, n: int) -> str:
    """Rewrite the ranking question so it states the count `n`, so the resumed
    turn is a complete standalone question the SQL layer can LIMIT on."""
    q = question.strip().rstrip(" ?.")
    new_q, hit = re.subn(r"\btop\b(?![\s-]*\d)", f"top {n}", q, count=1, flags=re.IGNORECASE)
    if hit:
        return new_q + "?"
    # No literal "top" to splice into ("which blocks spent the most") — append it.
    return f"{q}, showing only the top {n}?"


def _topn_clarification(question: str) -> "ClarificationNeeded":
    q = question.strip().rstrip(" ?.")
    options = [{"label": f"Top {n}", "question": _inject_topn(question, n)} for n in _TOPN_CHOICES]
    options.append({"label": "All of them", "question": f"{q}, return every row with no limit?"})
    return ClarificationNeeded(
        "How many results should be returned — the top 3, the top 5, the top 10, "
        "or the complete list?",
        options=options,
        rule="ranking-count-not-specified",
    )


# ── "Which area / year?" clarification ──────────────────────────────────────
# An aggregate question ("how many houses sanctioned", "total MGNREGA
# expenditure", or a bare "show MGNREGA spend") that names no place and no year
# gets answered statewide across every year — a scope the user never asked for
# and can't see in the reply. One free-text follow-up ("West Garo Hills
# 2023-24") is cheaper than a number that silently means something else.
# Applies to MGNREGA and PMAY-G alike.
_AGGREGATE_CUE = re.compile(
    r"\b(how many|how much|number of|count of|no\.? of|total(?:\s+number)?|"
    r"sum of|what(?:'s| is| was) the total|average|avg|mean)\b",
    re.IGNORECASE,
)
# A bare metric noun with no quantity word in front of it — "show MGNREGA
# spend", "PMAY-G houses", "person-days". On its own the metric name still means
# "give me the total <metric>", so it needs a place and a year just as much as
# an explicit "how much" does. Kept to unambiguous scheme metrics — generic
# words ("amount", "money", "funds", "works") are left out so a membership or
# list question isn't dragged into a scope pause.
_BARE_METRIC_CUE = re.compile(
    r"\b(person[\s-]?days?|expenditure|spend(?:ing)?|spent|"
    r"wages?|wage bill|job cards?|muster rolls?|"
    r"houses?(?:\s+(?:sanctioned|completed|approved|released|pending))?|"
    r"dwelling units?|sanctioned amount|amount released|"
    r"instal{1,2}ments?|disbursements?|utili[sz]ation)\b",
    re.IGNORECASE,
)
# The question already fixes its own scope (a breakdown, a trend, a comparison
# across a dimension, or an explicit "all of Meghalaya") — nothing to ask.
_HAS_BREAKDOWN = re.compile(
    r"\bby (?:district|block|village|panchayat|gp|year|month|scheme|program(?:me)?|sector)\b|"
    r"\b(?:per|each|every|for all|across all|all the) "
    r"(?:district|block|village|panchayat|year|month|scheme|program(?:me)?|sector)s?\b|"
    # "under each CM ELEVATE program", "for every PMAY-G scheme" — a scheme name
    # or other short qualifier can sit between "each/every" and the dimension
    # word itself; allow up to a few words of slack for scheme/program/sector
    # specifically (kept out of the tight pattern above to avoid over-matching
    # "each ... district" style geography phrasing where slack isn't needed).
    r"\b(?:per|each|every)\b[^?.!]{0,25}\b(?:scheme|program(?:me)?|sector)s?\b|"
    # "in each financial year", "total disbursed through each loan entity" —
    # a qualifier before "year", and category dimensions the tight pattern
    # above never listed. Both are groupings over every value, so asking
    # "which area / year?" first was wrong, and the "all years" reply then got
    # collapsed into ONE total instead of a per-year split (CM Elevate Legacy
    # QA TC-21 / TC-22 / TC-23 / TC-24, 2026-09-25).
    r"\b(?:per|each|every|by)\s+(?:financial|fiscal)\s+years?\b|"
    r"\b(?:per|each|every|by)\s+(?:loan\s+)?(?:entit(?:y|ies)|lenders?|categor(?:y|ies)|"
    r"tranches?|instal{1,2}ments?)\b|"
    r"\b(?:district|block|village|year|scheme|program(?:me)?|sector)[\s-]?wise\b|"
    r"\bbreak[\s-]?down\b|\btrend\b|\byear[\s-]?on[\s-]?year\b|"
    r"\bover (?:the )?(?:last|past) \w+ years?\b|"
    r"\bcompare\b|\bcomparison\b|\bversus\b|\bvs\.?\b|"
    r"\bcorrelat\w*|\brelationship between\b|\blinked (?:to|with)\b|"
    r"\b(?:districts?|blocks?|villages?|panchayats?|gps?)\s+"
    r"(?:with|where|that|having|which)\b|"
    # A rank window over a geography dimension — "top 5 districts", "bottom
    # blocks", "10 largest villages". It inherently spans every area in that
    # dimension, so geography is already scoped; only the year is still open.
    r"\b(?:top|bottom|leading|largest|biggest|smallest|highest|lowest)\s+"
    r"(?:\d+\s+)?(?:districts?|blocks?|villages?|panchayats?|gps?|schemes?|program(?:me)?s?|sectors?)\b|"
    r"\b\d+\s+(?:largest|biggest|smallest|highest|lowest)\s+"
    r"(?:districts?|blocks?|villages?|panchayats?|gps?|schemes?|program(?:me)?s?|sectors?)\b|"
    # "which district received the highest ...", "district with the lowest
    # ..." — the same rank-window logic as "top N districts" above, just
    # phrased as "which <geo> ... <superlative>" instead of "<superlative>
    # <geo>". Still inherently spans every area in the dimension, so
    # geography is already scoped; only the year is still open. Anchored
    # loosely (superlative anywhere within ~40 chars either side of the
    # geography noun) so "which district received the highest total
    # disbursement" and "highest total expenditure in which district" both
    # match.
    r"\bwhich (?:district|block|village|panchayat|gp)s?\b[^?]{0,40}\b"
    r"(?:highest|lowest|most|least|maximum|minimum|greatest|top|biggest|"
    r"largest|smallest)\b|"
    r"\b(?:highest|lowest|most|least|maximum|minimum|greatest|top|biggest|"
    r"largest|smallest)\b[^?]{0,40}\bwhich (?:district|block|village|panchayat|gp)s?\b|"
    # Same rank-window shape as above, but for scheme/program/sector — a scheme
    # name or other qualifier ("CM Elevate", "PMAY-G") often sits between
    # "which" and the dimension word itself ("which CM Elevate programs have
    # the highest..."), so this variant allows slack there too.
    r"\bwhich\b[^?.!]{0,25}\b(?:schemes?|program(?:me)?s?|sectors?)\b[^?]{0,40}\b"
    r"(?:highest|lowest|most|least|maximum|minimum|greatest|top|biggest|"
    r"largest|smallest)\b|"
    r"\b(?:highest|lowest|most|least|maximum|minimum|greatest|top|biggest|"
    r"largest|smallest)\b[^?]{0,40}\bwhich\b[^?.!]{0,25}\b(?:schemes?|program(?:me)?s?|sectors?)\b|"
    r"\bacross (?:the )?(?:districts?|blocks?|villages?|panchayats?|state|years?|schemes?|program(?:me)?s?|sectors?)\b",
    re.IGNORECASE,
)
_EXPLICIT_STATEWIDE = re.compile(
    r"\b(?:in|for|across|over|of) (?:all of |the (?:whole|entire) )?meghalaya\b|"
    r"\bstate[\s-]?(?:wide|level|total)\b|\boverall\b|\bin total\b|\bgrand total\b|"
    r"\ball (?:the )?(?:years|districts|blocks|villages)\b|\bentire state\b|"
    r"\bevery year\b|\bsince inception\b|\ball[\s-]?time\b|"
    r"\b(?:up )?(?:to|till|until) (?:date|now)\b|\bso far\b|\bcumulative\b",
    re.IGNORECASE,
)
# A cross-scheme set / overlap question whose answer IS a geography list or its
# count — "how many common districts in both schemes", "which villages are
# covered by both", "districts with both MGNREGA and PMAY-G activity". Geography
# is the thing being counted, not a filter, so there is no area or year to ask
# for — it is inherently statewide across the whole data window. This must not
# swallow a plain cross-scheme metric question ("how much did both schemes
# spend"), so every branch is anchored to a geography noun.
_CROSS_SCHEME_SET_QUESTION = re.compile(
    r"\b(?:common|shared|overlapping|overlap(?:ping)?|mutual|convergen\w*|"
    r"distinct|unique)\s+(?:districts?|blocks?|villages?|panchayats?|gps?|areas?|"
    r"geograph\w+)\b|"
    r"\b(?:districts?|blocks?|villages?|panchayats?|gps?|areas?)\s+"
    r"(?:(?:are|is|were|was|that|which|have|having|has)\s+){0,2}"
    r"(?:common (?:to|across|between)|shared (?:by|between|across)|"
    r"in (?:both|all|either)|covered (?:by|under) both|"
    r"covered by (?:both )?(?:mgnrega|pmay|schemes)|"
    r"with both|have both|having both|present in both|"
    r"served by both|running both|under both|in common)\b|"
    r"\bhow many (?:districts?|blocks?|villages?|panchayats?|gps?)\b[^?]*"
    r"\b(?:both schemes?|both mgnrega and pmay|pmay and mgnrega|"
    r"in common|overlap)\b|"
    # "blocks (wise) common in both schemes", "which blocks are common in both",
    # "common blocks in both schemes" — a geography noun sitting next to both
    # "common"/"shared"/"overlap" and "both", in either order. Still anchored to
    # the geography noun so a bare metric question can't match.
    r"\b(?:districts?|blocks?|villages?|panchayats?|gps?|areas?)\b(?:[\s-]?wise)?"
    r"[^?]{0,30}\b(?:common|shared|overlap\w*|convergen\w*)\b[^?]{0,20}\bboth\b|"
    r"\b(?:common|shared|overlap\w*|convergen\w*)\b[^?]{0,20}"
    r"\b(?:districts?|blocks?|villages?|panchayats?|gps?|areas?)\b[^?]{0,20}\bboth\b",
    re.IGNORECASE,
)


def _needs_scope_clarification(question: str, resolved: dict) -> bool:
    """True when an aggregate question — an explicit "how many / total …" or a
    bare metric noun on its own ("show MGNREGA spend") — pins no geography and no
    year, neither in its text nor via a resolved entity, and doesn't ask for a
    breakdown or an explicit statewide total. Callers must have run
    `resolve_entities` first so `resolved` reflects any district/block/village/
    year actually named."""
    if not settings.SCOPE_CLARIFY_ENABLED:
        return False
    q = question or ""
    if not (_AGGREGATE_CUE.search(q) or _BARE_METRIC_CUE.search(q)):
        return False
    if _HAS_BREAKDOWN.search(q) or _EXPLICIT_STATEWIDE.search(q):
        return False
    if _CROSS_SCHEME_SET_QUESTION.search(q):
        return False
    if any(resolved.get(k) for k in
           ("district", "district_list", "block", "block_list", "village_code",
            "village_code_list", "assembly_constituency", "year_key")):
        return False
    # A calendar date pins the time more tightly than a year_key does: "How many
    # PMAY houses were sanctioned on 2017-11-28?" is a statewide single-day
    # count, not a question missing its period (2026-09-27 screenshot).
    if _CALENDAR_DATE_RE.search(q):
        return False
    # A Focus Plus tranche or batch IS the scope the question pins ("How much
    # was disbursed under Tranche 2?", "…under the 12.5K batch?"): the SME
    # default for missing geography is statewide (focusplus_default_rules.yaml
    # missing_geography), and asking "which area?" first only added a click
    # (Focus Plus use-case QA 2026-09-27, FOCUS-006/007, KI-064).
    if resolved.get("tranche_label") or _FOCUSPLUS_BATCH_WORD.search(q):
        return False
    return True


# Focus Plus block_name_raw holds the literal "Nan" on the records whose block
# and district the source left blank (4 payments, 1 beneficiary).
_FP_BLANK_BLOCK = "NAN"
_FP_BLANK_BLOCK_NOTE = (
    "'Nan' is not a real C&RD block: it is how the Focus Plus source marks records whose "
    "block and district were left blank. Filter UPPER(block_name_raw) = 'NAN', report the "
    "figure, and say those records have no block or district recorded.")
_FP_BLANK_BLOCK_LEAD = ("'Nan' is not a real block — it is how the Focus Plus source file marks records "
                        "whose block and district were left blank.")

# The two Focus Plus cohorts by their stored batch_label ("12.5K" / "93K").
# Focus Plus-only vocabulary: no other scheme has a batch of that name.
_FOCUSPLUS_BATCH_WORD = re.compile(r"\b(?:12\.5\s*k|93\s*k)\b", re.IGNORECASE)


# A full calendar date: 2017-11-28, 2017/11/28, 28-11-2017, 28/11/2017, and the
# written forms "15 December 2020", "15th Dec, 2020", "December 15, 2020". The
# written forms were added after the PMAY-G use-case QA (2026-09-28, KI-094):
# "How many PMAY houses were sanctioned on 15 December 2020?" paused for an area
# and the premise check read the "15" as a figure the user had assumed.
_MONTH_NAME = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
               r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_CALENDAR_DATE_RE = re.compile(
    r"(?<!\d)(?:(?:19|20|21)\d\d[-/.]\d{1,2}[-/.]\d{1,2}"
    r"|\d{1,2}[-/.]\d{1,2}[-/.](?:19|20|21)\d\d)(?!\d)"
    r"|(?<!\d)\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH_NAME + r"\.?,?\s+(?:19|20|21)\d\d(?!\d)"
    r"|\b" + _MONTH_NAME + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+(?:19|20|21)\d\d(?!\d)",
    re.IGNORECASE)


# A reply to the scope pause is normally a bare fragment ("Ri Bhoi, 2023-24").
# These shapes instead mean the user dropped the earlier question and asked a
# fresh one — don't fold them into the original.
_REPLY_IS_NEW_QUESTION = re.compile(
    r"\b(how many|how much|number of|count of|what(?:'s| is| are| was) the|"
    r"who (?:is|can)|how do i|how to apply|what documents?|which documents?|"
    r"explain|define|difference between|tell me about)\b|"
    # A reply OPENING with "which ..." is a question of its own, not a place or
    # a year — "Which banks handle Focus Legacy payments?" was being merged into
    # the stale paused question. Anchored to the start so "the block, which is
    # in Ri Bhoi" is unaffected.
    r"^\s*which\b",
    re.IGNORECASE,
)


# A reply that OPENS with one of these is giving an instruction, not naming a
# place or a year. _REPLY_IS_NEW_QUESTION covers interrogative phrasings only,
# so an imperative slipped through and got glued onto the stale paused question
# ("List top 5 PGs which has more than 10 of members." came back as the previous
# turn's Nongstoin block-or-village prompt — reported 2026-09-23).
_REPLY_IS_IMPERATIVE = re.compile(
    r"^\s*(?:please\s+)?(?:list|show|give|display|rank|compare|find|search)\b",
    re.IGNORECASE,
)

# A scope pause asks for exactly a place and/or a year, so that vocabulary is
# the EXPECTED reply and must never read as "a new question". Stripping it out
# before the metric test is what separates "West Garo Hills, 2023-24" (a scope
# reply — nothing left once place and year are removed) from "Total amount
# disbursed by district" (a real question — the metric survives). An earlier
# version tested only for a BARE year and wrongly abandoned the pause on the
# commonest reply shape of all, place-plus-year.
_SCOPE_REPLY_VOCAB = re.compile(
    r"\b(?:fy\s*)?\d{4}(?:\s*-\s*\d{2,4})?\b|"           # 2023-24, FY 2021-22
    r"\ball\s+(?:of\s+)?meghalaya\b|\ball\s+years?\b|\ball\s+districts?\b|"
    r"\bstatewide\b|\bthe\s+(?:block|village|district|constituency)\b|"
    r"\bnot\s+(?:the|another)\b|\barea\s+type\b|\bcombined\b|"
    r"[,\.]",
    re.IGNORECASE,
)


@functools.lru_cache(maxsize=1)
def _known_place_names() -> tuple[str, ...]:
    """Every district and block name the resolvers know, lowercased, longest
    first. Used only to strip a place out of a scope reply before testing it for
    a metric — so "West Garo Hills" cannot be mistaken for question vocabulary.
    Built from the catalogues already loaded at startup; empty if they are not,
    in which case the test simply falls back to the year/phrase stripping."""
    from app import entity_resolver

    names: set[str] = set()
    for scheme in list(entity_resolver._catalog):
        for dim in ("district", "block"):
            for v in entity_resolver._catalog.get(scheme, {}).get(dim, []):
                c = str(v.get("canonical") or "").strip().lower()
                if len(c) >= 4:
                    names.add(c)
    return tuple(sorted(names, key=len, reverse=True))


def _reply_abandons_scope_pause(reply: str) -> bool:
    """True when the reply to a 'which area / year?' pause is itself a new,
    self-standing question rather than the scope fragment we asked for."""
    r = (reply or "").strip()
    if len(r.split()) > 12:
        return True
    if _REPLY_IS_NEW_QUESTION.search(r) or _KNOWLEDGE_HINTS.search(r):
        return True
    if _REPLY_IS_IMPERATIVE.search(r):
        return True
    # A scope fragment names a place and/or a year and nothing else. Strip that
    # expected vocabulary, plus any place NAME the resolver knows, and see
    # whether a METRIC survives: if one does, the reply is a question in its own
    # right whatever its grammatical shape (the same signal the DATA router
    # trusts). "West Garo Hills, 2023-24" reduces to nothing and still merges.
    residue = _SCOPE_REPLY_VOCAB.sub(" ", r)
    for place in _known_place_names():
        if place in residue.lower():
            residue = re.sub(re.escape(place), " ", residue, flags=re.IGNORECASE)
    if _DATA_HINTS.search(residue):
        return True
    return False


# ── "Which district in this region?" clarification ─────────────────────────
# "Garo Hills" / "Khasi Hills" / "Jaintia Hills" name a hill RANGE, not a district —
# each covers several districts (region_groupings in the *_entity_resolver.yaml). When
# a question names one and no specific district, offer its districts as one-tap chips
# instead of the generic "which area?" free-text pause. Applies to every scheme.
def _swap_region_phrase(question: str, aliases: list[str], repl: str) -> str:
    """Replace the first hill-range phrase in `question` with `repl`. Longest alias
    first so 'South West Garo Hills' isn't half-matched by 'Garo Hills'."""
    forms = sorted({a for a in aliases if a}, key=len, reverse=True)
    pat = re.compile(r"\b(" + "|".join(re.escape(a) for a in forms) + r")\b", re.IGNORECASE)
    new, n = pat.subn(repl, question, count=1)
    return new if n else f"{question.rstrip(' ?.')} for {repl}"


def _region_clarification(question: str, region: dict) -> "ClarificationNeeded":
    canon = region["canonical"]
    dists = region["districts"]
    aliases = [canon, *region.get("aliases", [])]
    all_list = ", ".join(dists[:-1]) + " and " + dists[-1]
    options = [{"label": d, "question": _swap_region_phrase(question, aliases, d)}
               for d in dists]
    options.append({
        "label": f"All of {canon} combined",
        "question": _swap_region_phrase(question, aliases, f"all of {canon}"),
    })
    return ClarificationNeeded(
        f"“{canon}” is a region covering {len(dists)} districts "
        f"— {all_list}. Which district, or all of {canon} combined?",
        options=options,
        rule="region-needs-district",
    )


def _scope_clarification(question: str, schemes: list[str]) -> "ClarificationNeeded":
    stem = question.strip().rstrip(" ?.")
    # District chips first (picking one still leaves the year open, so it falls
    # straight into `_year_clarification` on the next turn — a second round of
    # buttons rather than free text), then the one-tap statewide/all-years
    # shortcut, mirroring how `_year_clarification` appends its own "combined"
    # option last.
    seen: dict[str, None] = {}
    for s in schemes or []:
        for d in all_districts(s):
            seen.setdefault(d, None)
    # Schemes with NO time dimension at all (CM Elevate — see
    # _needs_year_clarification's identical check) still need the AREA half of
    # this ask (a bare "how many applications" is a real, useful district-level
    # question, and CM Elevate's own few-shot corpus has district-scoped
    # examples) — but asking "and which financial year?" on top is nonsensical
    # when no year exists anywhere on the fact. Reword rather than skip the
    # gate outright (an earlier pass tried skipping it entirely and lost the
    # district ask too — see cmelevate-routing-gates-bug memory).
    live = [s for s in (schemes or []) if s in _SCHEME_DATA_YEARS]
    area_only = bool(live) and all(not _SCHEME_DATA_YEARS[s] for s in live)
    if area_only:
        options = [{"label": d, "question": f"{stem} for {d}"} for d in seen]
        options.append({
            "label": "All of Meghalaya",
            "question": f"{stem} for all of Meghalaya",
        })
        return ClarificationNeeded(
            "Which area should the answer cover — a specific district, block, or "
            "village? Pick a district below, or choose the statewide total; you can "
            "also just type a block or village name.",
            options=options,
            rule="scope-not-specified",
        )
    options = [{"label": d, "question": f"{stem} for {d}"} for d in seen]
    # Statewide chip is area-only (2026-09-26 UI request): like a district chip,
    # it leaves the year open so `_year_clarification` offers the FY chips next,
    # instead of silently pinning "all years".
    options.append({
        "label": "All of Meghalaya",
        "question": f"{stem} for all of Meghalaya",
    })
    return ClarificationNeeded(
        "Which area and time period should the answer cover — a specific district, "
        "block, or village, and which financial year? Pick a district below, or "
        "choose all of Meghalaya; you can also just type a block or "
        "village name (for example, \"West Garo Hills, 2023-24\").",
        options=options,
        rule="scope-not-specified",
    )


# ── "Which financial year?" clarification ───────────────────────────────────
# The scope gate above only fires when a question pins NO geography at all. A
# question that already fixes its place — "compare districts for MGNREGA", "total
# PMAY-G houses in West Garo Hills" — sails past it and is then answered across
# EVERY financial year at once, a scope the user never chose and can't see in the
# reply. This gate catches exactly that: geography is settled, the year is not.
# One-tap year chips (the scheme's FYs + "all years combined") resume the flow.
#
# The financial years each scheme actually holds in megh_db — the SINGLE source
# of truth for every year chip we offer (the "which year?" pause AND the
# out-of-range guard). These are DEFAULTS only: refresh_scheme_years() overwrites
# them at startup with the real DISTINCT year_key set from the curated views, so
# the chips and the guard always match the live data even after a data reload.
# Values below were verified against curated.v_employment / v_expenditure
# (MGNREGA) and curated.v_pmay (PMAY-G) on 2026-08-30 — MGNREGA covers FY
# 2022-23..2025-26; PMAY-G covers FY 2017-18..2023-24 (NOT 2024-25 / 2025-26).
# Focus Plus holds only FY 2022-23 (Tranch 1) and FY 2025-26 (Tranches 2-4) — the two
# middle years are empty for it (focusplus_schema_partitions.yaml). A three-year gap is
# expected, and asking a Focus Plus question for FY 2023-24 / 2024-25 correctly triggers
# the "that year isn't in the data" guard.
# CM Elevate is DIFFERENT IN KIND, not just sparser: it holds an empty list on
# purpose. There is no year_key, no FK to dim_year, no date column of any kind
# anywhere in fact_cm_elevate_application (cmelevate_schema_partitions.yaml
# semantic_rules.time_rule) — refresh_scheme_years() below does not even probe
# it. _needs_year_clarification and _year_out_of_range_clarification both treat
# an empty list here as "no time dimension exists" and refuse a time filter
# outright instead of offering year chips.
_SCHEME_DATA_YEARS: dict[str, list[str]] = {
    "MGNREGA": ["2022-23", "2023-24", "2024-25", "2025-26"],
    "PMAY-G":  ["2017-18", "2018-19", "2019-20", "2020-21", "2021-22", "2022-23", "2023-24"],
    "Focus Plus": ["2022-23", "2025-26"],
    "CM Elevate": [],
    # Focus Legacy holds FOUR years with a HOLE IN THE MIDDLE: FY2023-24 has no
    # payments at all — an absent year, not a zero one (focuslegacy_schema_
    # partitions.yaml semantic_rules.time_gap_rule). It is deliberately NOT
    # listed, so the year chips never offer it and the out-of-range guard
    # correctly refuses a FY2023-24 question instead of returning an empty total.
    "Focus Legacy": ["2021-22", "2022-23", "2024-25", "2025-26"],
    # CM Elevate Legacy holds TWO years. 395 records (both Sericulture schemes)
    # carry no year at all — they are not a third year, so they get no chip; an
    # "all financial years" answer still includes them because it applies no
    # year filter (cmelevatelegacy_schema_partitions.yaml year_key rules).
    "CM Elevate Legacy": ["2024-25", "2025-26"],
}


def _fy_short(year_key: int) -> str:
    """2022 -> '2022-23'."""
    return f"{year_key}-{(year_key + 1) % 100:02d}"


def _fy_start(short: str) -> int:
    """'2022-23' -> 2022."""
    return int(str(short)[:4])


async def refresh_scheme_years() -> None:
    """Load each scheme's real DISTINCT financial years from megh_db so the year
    chips and the out-of-range guard track the live data. Best-effort — on any
    failure the verified defaults in _SCHEME_DATA_YEARS stay in place."""
    from app.db import run_readonly
    sql_by_scheme = {
        "MGNREGA": (
            "SELECT DISTINCT year_key FROM curated.v_employment WHERE year_key IS NOT NULL "
            "UNION "
            "SELECT DISTINCT year_key FROM curated.v_expenditure WHERE year_key IS NOT NULL"
        ),
        "PMAY-G": "SELECT DISTINCT year_key FROM curated.v_pmay WHERE year_key IS NOT NULL",
        "Focus Plus": (
            "SELECT DISTINCT year_key FROM curated.v_focus_plus WHERE year_key IS NOT NULL"
        ),
        "Focus Legacy": (
            "SELECT DISTINCT year_key FROM curated.v_focus_legacy WHERE year_key IS NOT NULL"
        ),
        "CM Elevate Legacy": (
            "SELECT DISTINCT year_key FROM curated.v_cm_elevate_disbursement "
            "WHERE year_key IS NOT NULL"
        ),
        # CM Elevate deliberately has no entry here — curated.v_cm_elevate has no
        # year_key column at all (not merely NULL), so there is nothing to probe.
        # Its default of [] in _SCHEME_DATA_YEARS above is the permanent value.
    }
    for scheme, sql in sql_by_scheme.items():
        try:
            rows = await run_readonly(sql)
            yrs = sorted({int(r["year_key"]) for r in rows if r.get("year_key") is not None})
            if yrs:
                _SCHEME_DATA_YEARS[scheme] = [_fy_short(y) for y in yrs]
                logger.info("scheme years loaded: %s -> %s", scheme, _SCHEME_DATA_YEARS[scheme])
            else:
                logger.warning("scheme years: %s query returned no rows, keeping default %s",
                               scheme, _SCHEME_DATA_YEARS[scheme])
        except Exception as e:  # noqa: BLE001
            logger.warning("scheme years: %s query failed, keeping default %s (%s)",
                           scheme, _SCHEME_DATA_YEARS[scheme], e)

# The question already fixes its time scope — a specific year, an explicit
# "all years", or a request for a per-year series. Any of these => don't ask.
_ALL_YEARS_CUE = re.compile(
    r"\ball[\s-]?(?:the\s+)?(?:financial |fiscal |fy )?years?\b|"
    r"\bevery (?:financial |fiscal )?year\b|\beach year\b|"
    r"\bacross (?:all )?(?:the )?(?:financial |fiscal )?years?\b|"
    r"\ball[\s-]?time\b|\bsince inception\b|"
    r"\b(?:up )?(?:to|till|until) (?:date|now)\b|\bso far\b|\bcumulative\b|"
    r"\ball years combined\b|\boverall\b|\bin total\b|\bgrand total\b",
    re.IGNORECASE,
)
_TIME_SERIES_CUE = re.compile(
    r"\btrend\b|\bover time\b|\bover (?:the )?(?:last|past) \w+ (?:years?|fys?)\b|"
    r"\byear[\s-]?on[\s-]?year\b|\byear[\s-]?wise\b|\bby year\b|\bby financial year\b|"
    r"\bper year\b|\bannually\b|\bannual\b|\bhistory\b|\bhistorical\b|"
    r"\bgrowth\b|\bchange over\b|\beach (?:financial )?year\b",
    re.IGNORECASE,
)
# The question is actually about a metric or a per-dimension breakdown — the only
# shapes where "which year?" is a meaningful missing filter. A pure membership /
# rules question ("which villages have both schemes", "who is eligible") isn't.
_METRIC_OR_BREAKDOWN_CUE = re.compile(
    r"\b(how many|how much|number of|count of|no\.? of|total|sum of|average|avg|mean|"
    r"person[\s-]?days?|expenditure|spend(?:ing)?|spent|wages?|wage bill|job cards?|"
    r"muster rolls?|houses?|dwelling units?|sanctioned amount|amount released|"
    r"instal{1,2}ments?|disbursements?|utili[sz]ation|completion rate|success rate|"
    # Focus Plus's own metric nouns — same gap as _DATA_HINTS (see 2026-09-11
    # fix note there): without these, a bare "<scheme> beneficiaries?" matched
    # neither this cue nor _MENTIONS_TRANCHE_WORD, so both the year AND
    # tranche clarification gates were skipped and an unscoped question
    # reached the SQL generator with no year/tranche pin at all — which then
    # sometimes guessed a specific tranche on its own instead of correctly
    # reasoning "no tranche named -> every tranche".
    r"beneficiar\w*|payments?|"
    r"compare|comparison|versus|\bvs\.?\b|rank(?:ed|ing)?|top \d|highest|lowest|"
    r"most|least|by district|by block|by village|by panchayat|"
    r"district[\s-]?wise|block[\s-]?wise|village[\s-]?wise)\b",
    re.IGNORECASE,
)


def _year_choices_for(schemes: list[str]) -> list[str]:
    """Every financial year the given scheme(s) hold, distinct, oldest first.
    Falls back to the full set when `schemes` is empty or unrecognised."""
    years, _live = _available_years_for(schemes)
    return years


def _needs_year_clarification(question: str, schemes: list[str], resolved: dict) -> bool:
    """True when the question is a metric / breakdown question whose geography is
    already settled (so the scope gate skipped it) but which pins no financial
    year — not in its text, not via a resolved `year_key` — and doesn't ask for a
    time series or an explicit all-years total. Callers must have run
    `resolve_entities` first so `resolved` reflects any year actually named."""
    if not settings.YEAR_CLARIFY_ENABLED:
        return False
    q = question or ""
    # Schemes with NO time dimension at all (CM Elevate has no year_key, no date
    # column of any kind) can never be asked "which year" — there is nothing to
    # choose. Skip the whole gate when every scheme in play is one of these, so
    # _year_clarification never runs on an empty _SCHEME_DATA_YEARS[scheme] list
    # (which would otherwise crash indexing years[-1]).
    live = [s for s in (schemes or []) if s in _SCHEME_DATA_YEARS]
    if live and all(not _SCHEME_DATA_YEARS[s] for s in live):
        return False
    # PMAY-G used to be exempted here (beneficiary counts / house status are
    # cumulative-to-date, so "which year?" felt like an unnecessary interruption
    # to an earlier QA pass) and silently defaulted to "all years to date"
    # instead of asking. Reinstated to ask same as MGNREGA, 2026-09-09, by
    # explicit product decision — a plain PMAY-G question with no year now
    # pauses for a FY choice too; say "all years" / "cumulative" explicitly
    # (see _ALL_YEARS_CUE) to get the old default without the prompt.
    if resolved.get("year_key") or _parse_year_key(q) is not None:
        return False
    if _ALL_YEARS_CUE.search(q) or _TIME_SERIES_CUE.search(q):
        return False
    # A cross-scheme "which / how many districts are in both schemes" question is
    # a membership/overlap set question — inherently across the whole data window,
    # so there is no single financial year to ask for.
    if _CROSS_SCHEME_SET_QUESTION.search(q):
        return False
    # MGNREGA: a percentage / share / women question is a metric question too.
    # Without this, "what percentage of employment persons were women in ekh?"
    # skipped the FY pause and the SQL model silently picked the latest year
    # (FY 2025-26, whose women column is unrecorded) — user report 2026-09-27.
    if not _METRIC_OR_BREAKDOWN_CUE.search(q) and not (
            "MGNREGA" in (schemes or []) and (_RATIO_CUE.search(q) or _WOMEN_CUE.search(q))):
        return False
    return True


def _year_clarification(question: str, schemes: list[str]) -> "ClarificationNeeded":
    stem = question.strip().rstrip(" ?.")
    years, live = _available_years_for(schemes)
    options = [
        {"label": f"FY {y}", "question": f"{stem} for FY {y}"}
        for y in years
    ]
    options.append({
        "label": "All financial years combined",
        "question": f"{stem} across all financial years",
    })
    if len(live) == 1:
        scope_word = live[0]
    elif len(live) == 2:
        scope_word = " and ".join(live)
    else:
        scope_word = ", ".join(live[:-1]) + " and " + live[-1]
    year_list = ", ".join(f"FY {y}" for y in years[:-1]) + f" and FY {years[-1]}"
    return ClarificationNeeded(
        f"{scope_word} data is available for {year_list}. "
        "Which of these is required — a single financial year, or all of them "
        "combined?",
        options=options,
        rule="year-not-specified",
    )


# ── "Which tranche?" clarification (Focus Plus only) ───────────────────────
# Focus Plus's amount_disbursed differs sharply by tranche_label (5,000 for
# Tranch 1, 2,500 for every later tranche — see schema_context.py rule 7), so
# silently summing every tranche together produces a total that reads as a
# single entitlement when it's actually a mix ratio. Mirrors
# _needs_year_clarification / _year_clarification in shape: ask with one-tap
# chips (Tranch 1..4 plus "all combined") rather than guess, unless the
# question already names a tranche, asks for a per-tranche breakdown, or
# explicitly wants every tranche combined.
_TRANCHE_BREAKDOWN_CUE = re.compile(
    r"\bby tranche\b|\btranche[\s-]?wise\b|\bper tranche\b|\beach tranche\b|"
    r"\bacross (?:all )?(?:the )?tranches\b|\btranche breakdown\b|"
    r"\bsplit by tranche\b|\bbreak(?:down|\s+down)? by tranche\b|"
    # "which tranche has the most X" / "what tranche..." asks the data to
    # identify one BY comparing across all of them — the opposite of a
    # question that's missing a tranche pin, so it must not be asked to pick.
    r"\bwhich tranche\b|\bwhat tranche\b",
    re.IGNORECASE,
)
_ALL_TRANCHES_CUE = re.compile(
    r"\ball[\s-]?tranches?\b|\btranches? combined\b|\bcombined tranches?\b|"
    r"\bcumulative\b|\boverall\b|\bin total\b|\bgrand total\b|\ball[\s-]?time\b",
    re.IGNORECASE,
)
# The literal word "tranche"/"tranch" appearing anywhere with no specific
# tranche resolved (resolve_tranche_label found nothing) is itself the
# clearest possible signal that the question is tranche-scoped but doesn't
# say which one — regardless of whether the wording also happens to match
# _METRIC_OR_BREAKDOWN_CUE. That cue was borrowed from the year gate and is
# tuned for money/count metrics ("how much", "disbursements"); it has no
# "status" or "breakdown" vocabulary, so "give me status breakdown for
# tranche?" matched neither cue and silently fell through to the SQL
# generator, which picked one tranche (Tranch 4) on its own with nothing
# to base that choice on.
_MENTIONS_TRANCHE_WORD = re.compile(r"\btranche?s?\b", re.IGNORECASE)

# focus_status / verification_status / gender / occupation are populated ONLY
# on the '12.5K' cohort, which per FOCUS PLUS RULES rule 8 exists ONLY at
# Tranch 4 (schema_context.py _FOCUSPLUS_RULES #4, #8). A question about one of
# these columns has no real "which tranche?" to ask — every other tranche has
# zero such rows, so "all tranches combined" and "Tranch 4" are the same
# answer. Pausing to ask anyway produces a rewritten question ("... across all
# tranches") that then fights the Tranch-4-only constraint during SQL
# generation and burns the repair budget for nothing — skip the gate instead
# and let it hit the existing focus_status few-shots directly (see
# "How many Focus Plus registrations are still pending?" in
# data/focus_plus/focusplus_few_shot.yaml).
_PERSON_LEVEL_COLUMN_CUE = re.compile(
    # Bare "status" is included, not just the "focus status" / "verification
    # status" compounds — schema_context.py rule 13 makes an unqualified
    # "status"/"status breakdown"/"status-wise" mean focus_status by default,
    # which is exactly as cohort-locked as the compound forms. Missing this
    # let "give me the status breakdown for tranche 1" slip past both this
    # gate and _person_level_tranche_conflict straight into a guaranteed-empty
    # query (confirmed live 2026-09-12).
    r"\bstatus\b|\bfocus[\s-]?status\b|\bverification[\s-]?status\b|\bverified\b|\bverification\b|"
    r"\bgender\b|\bfemale\b|\bmale\b|\bwomen\b|\bmen\b|"
    r"\boccupation\b|\bfarmers?\b|"
    r"\bpending\b|\bapproved\b|\brejected\b|\bregistrations?\b",
    re.IGNORECASE,
)

# A tranche label is "Tranch 4 - Feb-March" — the ONLY tranche the 12.5K
# person-level cohort belongs to (schema_context.py rule 4/8). Matches the
# canonical value's leading "Tranch 4" regardless of the month suffix.
_TRANCH4_LABEL_RE = re.compile(r"^tranch\s*4\b", re.IGNORECASE)


def _person_level_tranche_conflict(question: str, schemes: list[str], resolved: dict) -> bool:
    """True when the question asks for a person-level column (status/gender/
    occupation/verification — the 12.5K-cohort-only fields, rule 4) AND names
    one or more SPECIFIC tranches, NONE of which is Tranch 4. That combination
    can never have any matching rows — the 12.5K cohort IS Tranch 4, so a
    filter for Tranch 1/2/3 plus any of these columns is a structural
    contradiction, not an ordinary "no rows matched" outcome. Left to run as
    plain SQL this produces the confusing generic "couldn't find any matching
    records" fallback (`_no_data_answer`) with no explanation of WHY — catching
    it here lets `_answer_data` explain the real reason instead, and skips a
    wasted SQL round trip. Scoped to single-scheme Focus Plus only, mirroring
    `_needs_tranche_clarification`."""
    if (schemes or []) != ["Focus Plus"]:
        return False
    if not _PERSON_LEVEL_COLUMN_CUE.search(question or ""):
        return False
    tranche_label = resolved.get("tranche_label")
    if not tranche_label:
        return False
    labels = tranche_label if isinstance(tranche_label, list) else [tranche_label]
    return not any(_TRANCH4_LABEL_RE.match(str(lbl)) for lbl in labels)


def _person_level_tranche_conflict_answer(question: str, schemes: list[str],
                                          entity_result: dict) -> dict:
    """Deterministic explanation for `_person_level_tranche_conflict` — no SQL
    is run because the answer (zero rows, always) is already known from the
    schema, not from the data."""
    display = entity_result.get("display", {}).get("tranche_label") or "that tranche"
    resolved = entity_result["resolved"]
    answer = (
        f"{display} has no status, gender, occupation or verification data. "
        "Those fields are recorded only for the 12.5K registration cohort, and "
        "that cohort falls entirely under Tranch 4 — ask for Tranch 4, or drop "
        "the tranche filter, to see that breakdown."
    )
    return {
        "route": "data",
        "intent": "DATA",
        "confidence": "high",
        "schemes": schemes,
        "resolved_entities": resolved,
        "sql": "",
        "sql_query": "",
        "row_count": 0,
        "rows": [],
        "data": [],
        "answer": answer,
    }


# Client UAT sheet (FOCUS-030, 2026-09-13): "Give me an overall Focus+ data
# summary" got a correct but thin answer — just payments/beneficiaries/amount/
# districts/blocks/villages from one plain SELECT. The client's remark wants a
# richer report: years/batches/tranches available, the top district by each
# of two different rankings (most beneficiaries vs. highest disbursement —
# genuinely different districts in this data), top bank, and the 12.5K-only
# gender/occupation/status/verification splits. That shape can't come from one
# flat SELECT the normal way (each of those needs its own GROUP BY / ORDER BY /
# LIMIT 1), and letting the LLM free-write ad hoc SQL plus prose for a dozen
# figures at once is exactly the kind of multi-fact answer this project's
# numeric-faithfulness guard exists to catch failures of, not prevent them.
# Deterministic instead: one CTE query gets every figure in a single round
# trip, and the answer is built directly from those rows — no composer call,
# so nothing here can be transcribed wrong.
_FOCUSPLUS_OVERALL_SUMMARY_CUE = re.compile(
    r"\boverall\b[^.?!]{0,40}\b(summary|picture|snapshot)\b|"
    r"\b(summary|snapshot|overview)\b[^.?!]{0,40}\boverall\b|"
    r"\b(complete|full|entire)\s+(data\s+)?summary\b|"
    r"\bsummari[sz]e\b.{0,30}\bfocus\b|\bfocus\b.{0,30}\bdata\s+summary\b",
    re.IGNORECASE,
)

_FOCUSPLUS_OVERALL_SUMMARY_SQL = """
WITH totals AS (
  SELECT COUNT(*) AS payments,
         COUNT(DISTINCT beneficiary_key) AS beneficiaries,
         SUM(amount_disbursed) AS amount,
         COUNT(DISTINCT lgd_district) AS districts,
         COUNT(*) FILTER (WHERE lgd_district IS NULL) AS missing_district_rows
  FROM curated.v_focus_plus
),
years AS (
  SELECT string_agg(DISTINCT financial_year_short, ', ' ORDER BY financial_year_short) AS years_list
  FROM curated.v_focus_plus
),
batches AS (
  SELECT string_agg(DISTINCT batch_label, ', ' ORDER BY batch_label) AS batch_list
  FROM curated.v_focus_plus
),
tranches AS (
  SELECT COUNT(DISTINCT tranche_label) AS tranche_count
  FROM curated.v_focus_plus
),
top_beneficiary_district AS (
  SELECT lgd_district AS top_ben_district, COUNT(DISTINCT beneficiary_key) AS top_ben_district_count
  FROM curated.v_focus_plus WHERE lgd_district IS NOT NULL
  GROUP BY lgd_district ORDER BY top_ben_district_count DESC LIMIT 1
),
top_disbursement_district AS (
  SELECT lgd_district AS top_amt_district, SUM(amount_disbursed) AS top_amt_district_amount
  FROM curated.v_focus_plus WHERE lgd_district IS NOT NULL
  GROUP BY lgd_district ORDER BY top_amt_district_amount DESC LIMIT 1
),
top_bank AS (
  SELECT bank_name_raw AS top_bank_name, SUM(amount_disbursed) AS top_bank_amount
  FROM curated.v_focus_plus WHERE bank_name_raw IS NOT NULL AND bank_name_raw !~ '^[0-9]+$'
  GROUP BY bank_name_raw ORDER BY top_bank_amount DESC LIMIT 1
),
gender AS (
  SELECT COUNT(*) FILTER (WHERE gender = 'Female') AS female,
         COUNT(*) FILTER (WHERE gender = 'Male') AS male
  FROM curated.v_focus_plus WHERE batch_label = '12.5K'
),
occupation AS (
  SELECT COUNT(*) FILTER (WHERE occupation = 'Farmer') AS farmers
  FROM curated.v_focus_plus WHERE batch_label = '12.5K'
),
status AS (
  SELECT COUNT(*) FILTER (WHERE focus_status = 'Pending') AS pending,
         COUNT(*) FILTER (WHERE focus_status = 'Approved') AS approved,
         COUNT(*) FILTER (WHERE focus_status = 'Rejected') AS rejected,
         COUNT(*) FILTER (WHERE verification_status = 'Approved') AS verified_approved
  FROM curated.v_focus_plus WHERE batch_label = '12.5K'
)
SELECT totals.payments, totals.beneficiaries, totals.amount, totals.districts,
       totals.missing_district_rows, years.years_list, batches.batch_list,
       tranches.tranche_count, top_beneficiary_district.top_ben_district,
       top_beneficiary_district.top_ben_district_count,
       top_disbursement_district.top_amt_district,
       top_disbursement_district.top_amt_district_amount,
       top_bank.top_bank_name, top_bank.top_bank_amount,
       gender.female, gender.male, occupation.farmers,
       status.pending, status.approved, status.rejected, status.verified_approved
FROM totals, years, batches, tranches, top_beneficiary_district,
     top_disbursement_district, top_bank, gender, occupation, status
LIMIT 1;
""".strip()


# ── "Which scheme has the highest spend?" — the scheme IS the answer ────────
# A superlative asked ACROSS schemes ("which scheme paid out the most?", "the
# scheme with the highest money spent") is answered by ranking the schemes
# against each other. Asking "which scheme does your question concern?" first
# is backwards — the scheme is the thing being asked for, not a filter the
# user forgot (reported 2026-09-17: "what about the scheme with highest money
# paid?" raised the four-way scheme pause instead of answering).
#
# _EXPLICIT_BOTH already recognises the phrasings that ask for every scheme
# ("both schemes", "by scheme", "scheme-wise"), but not this superlative
# shape, where the cross-scheme intent is carried by "which/what scheme" plus
# a ranking word rather than by an "all/each" quantifier.
_CROSS_SCHEME_SUPERLATIVE = re.compile(
    r"\b(?:which|what)\s+scheme\b[^?.!]{0,60}\b"
    r"(?:highest|lowest|most|least|maximum|minimum|greatest|biggest|largest|"
    r"smallest|top|best|worst|more|less)\b|"
    r"\b(?:highest|lowest|most|least|maximum|minimum|greatest|biggest|largest|"
    r"smallest|top)\b[^?.!]{0,40}\bscheme\b|"
    r"\bscheme\s+with\s+(?:the\s+)?(?:highest|lowest|most|least|maximum|minimum|"
    r"greatest|biggest|largest|smallest|top)\b|"
    r"\b(?:rank|compare)\s+(?:the\s+)?schemes\b",
    re.IGNORECASE,
)
# The superlative has to be about MONEY for the ranking below to apply — a
# "which scheme has the most applications" question is a different figure and
# is left to normal SQL generation.
_MONEY_SUPERLATIVE = re.compile(
    r"\bmoney\b|\bamount\b|\bspend\w*\b|\bspent\b|\bexpenditure\b|\bpaid\b|"
    r"\bpayment\w*\b|\bdisburs\w*\b|\breleas\w*\b|\bfunds?\b|\bcrore\b|\blakh\b|"
    r"\bcost\b|\bbudget\b|\boutlay\b",
    re.IGNORECASE,
)


def _wants_cross_scheme_money_ranking(question: str) -> bool:
    """True when the question asks which scheme spent/paid the most — a
    comparison ACROSS schemes whose answer names a scheme."""
    q = question or ""
    return bool(_CROSS_SCHEME_SUPERLATIVE.search(q) and _MONEY_SUPERLATIVE.search(q))


# One query, every scheme that records money, each normalised to CRORE.
# curated.v_cross_scheme_money_district_year is the sanctioned MGNREGA-vs-PMAY
# comparison object (docs/DATA_MODEL.md); Focus Plus keeps its money on its own
# view in RUPEES (/1e7 -> crore). CM Elevate is deliberately absent — it has no
# money column of any kind (v_cm_elevate carries applications only), so it is
# reported as "not held" rather than as a misleading zero.
_CROSS_SCHEME_MONEY_SQL = """
SELECT scheme_code AS scheme,
       ROUND(SUM(amount_crore), 2) AS amount_crore,
       MIN(measure_semantics) AS measure_semantics
FROM curated.v_cross_scheme_money_district_year
GROUP BY scheme_code
UNION ALL
SELECT 'Focus Plus' AS scheme,
       ROUND(SUM(amount_disbursed) / 1e7, 2) AS amount_crore,
       'amount disbursed to farmers (DBT), rupees' AS measure_semantics
FROM curated.v_focus_plus
UNION ALL
SELECT 'Focus Legacy' AS scheme,
       ROUND(SUM(amount_disbursed) / 1e7, 2) AS amount_crore,
       'amount remitted to producer groups, rupees' AS measure_semantics
FROM curated.v_focus_legacy
UNION ALL
SELECT 'CM Elevate Legacy' AS scheme,
       ROUND(SUM(total_disbursement) / 1e7, 2) AS amount_crore,
       'subsidy and loan disbursed to sanctioned applicants, rupees' AS measure_semantics
FROM curated.v_cm_elevate_disbursement
ORDER BY amount_crore DESC
""".strip()

_SCHEME_DISPLAY_NAME = {"MGNREGA": "MGNREGA", "PMAY": "PMAY-G", "PMAY-G": "PMAY-G",
                        "Focus Plus": "Focus Plus", "CM Elevate": "CM Elevate",
                        "Focus Legacy": "Focus Legacy",
                        "CM Elevate Legacy": "CM Elevate Legacy"}
# Plain-English rendering of each scheme's measure_semantics. The stored strings
# are written for the SQL prompt ("annual FLOW (lakh rupees)", "an EVENT, not a
# clean annual flow") and read as database jargon in a chat bubble; the caveat
# they carry is preserved in the closing paragraph either way.
_MEASURE_PLAIN = {
    "MGNREGA": "expenditure actually incurred",
    "PMAY-G": "money released against sanctions",
    "Focus Plus": "cash disbursed to farmers (DBT)",
    "Focus Legacy": "cash remitted to producer groups",
    "CM Elevate Legacy": "subsidy and loans disbursed to sanctioned applicants",
}


async def _cross_scheme_money_answer(question: str) -> dict:
    """Rank the schemes by money, deterministically. Built from the rows rather
    than composed by the LLM, for the same reason as the Focus Plus overall
    summary above: several figures of DIFFERENT kinds in one answer is exactly
    where a composer misattributes numbers, and the measure_semantics caveat
    must survive verbatim — docs/DATA_MODEL.md requires carrying it into any
    cross-scheme money comparison, because MGNREGA's figure is expenditure
    incurred while PMAY-G's is money released against sanctions."""
    rows = await run_readonly(_CROSS_SCHEME_MONEY_SQL)
    ranked = [r for r in rows if r.get("amount_crore") is not None]
    if not ranked:
        return {"route": "data", "intent": "DATA", "confidence": "low",
                "answer": "I couldn't read the scheme spending figures just now.",
                **_empty_data_fields()}
    top = ranked[0]
    top_name = _SCHEME_DISPLAY_NAME.get(str(top["scheme"]), str(top["scheme"]))
    lines = [
        f"{top_name} has the highest amount at ₹{float(top['amount_crore']):,.2f} crore. "
        "Across every scheme that records money:"
    ]
    for r in ranked:
        name = _SCHEME_DISPLAY_NAME.get(str(r["scheme"]), str(r["scheme"]))
        lines.append(f"- **{name}** — ₹{float(r['amount_crore']):,.2f} crore "
                     f"({_MEASURE_PLAIN.get(name, r['measure_semantics'])})")
    lines.append(
        "\nThese are not the same kind of figure, so treat the ranking as indicative "
        "rather than like-for-like: MGNREGA's is expenditure actually incurred, PMAY-G's "
        "is money released against sanctions, Focus Plus's is cash disbursed to "
        "individual farmers, and Focus Legacy's is cash remitted to producer groups "
        "(where every payment is Rs 5,000 per member, so the figure is really a "
        "membership count), and CM Elevate Legacy's is subsidy and loans together paid "
        "to sanctioned applicants. The CM Elevate applications data records no payment "
        "of any kind, so it cannot appear in a money comparison at all."
    )
    return {
        "route": "data", "intent": "DATA", "confidence": "high",
        "schemes": [_SCHEME_DISPLAY_NAME.get(str(r["scheme"]), str(r["scheme"])) for r in ranked],
        "resolved_entities": {},
        "sql": _CROSS_SCHEME_MONEY_SQL, "sql_query": _CROSS_SCHEME_MONEY_SQL,
        "row_count": len(ranked), "rows": ranked, "data": ranked,
        "answer": "\n".join(lines),
    }


def _focusplus_wants_overall_summary(question: str, schemes: list[str]) -> bool:
    return schemes == ["Focus Plus"] and bool(_FOCUSPLUS_OVERALL_SUMMARY_CUE.search(question or ""))


def _money(v) -> str:
    return f"₹{float(v):,.2f}"


async def _focusplus_overall_summary_answer(schemes: list[str], entity_result: dict) -> dict:
    rows = await run_readonly(_FOCUSPLUS_OVERALL_SUMMARY_SQL)
    r = rows[0]
    avg_per_beneficiary = float(r["amount"]) / r["beneficiaries"]
    answer = (
        f"Focus+ overall summary:\n"
        f"- Disbursement records (payments): {r['payments']:,}\n"
        f"- Unique beneficiaries: {r['beneficiaries']:,}\n"
        f"- Total amount disbursed: {_money(r['amount'])}\n"
        f"- Average disbursement per beneficiary: {_money(avg_per_beneficiary)}\n"
        f"- Financial years available: {r['years_list']}\n"
        f"- Batches: {r['batch_list']} (93K = legacy paid cohort, 12.5K = registration cohort)\n"
        f"- Tranches: {r['tranche_count']}\n"
        f"- Districts represented: {r['districts']}, plus {r['missing_district_rows']} "
        "payment records with no district resolved\n"
        f"- District with the most beneficiaries: {r['top_ben_district']} — "
        f"{r['top_ben_district_count']:,}\n"
        f"- District with the highest disbursement: {r['top_amt_district']} — "
        f"{_money(r['top_amt_district_amount'])}\n"
        f"- Top bank by disbursement: {r['top_bank_name']} — {_money(r['top_bank_amount'])}\n"
        f"- Gender split (12.5K cohort only, ~3% of beneficiaries): "
        f"{r['female']:,} female, {r['male']:,} male\n"
        f"- Farmers (12.5K cohort only): {r['farmers']:,}\n"
        f"- Status (12.5K cohort only): {r['pending']:,} Pending, {r['approved']:,} Approved, "
        f"{r['rejected']:,} Rejected\n"
        f"- Verification status (12.5K cohort only): {r['verified_approved']:,} Approved"
    )
    return {
        "route": "data",
        "intent": "DATA",
        "confidence": "high",
        "schemes": schemes,
        "resolved_entities": entity_result["resolved"],
        "sql": _FOCUSPLUS_OVERALL_SUMMARY_SQL,
        "sql_query": _FOCUSPLUS_OVERALL_SUMMARY_SQL,
        "row_count": 1,
        "rows": rows,
        "data": rows,
        "answer": answer,
    }


def _needs_tranche_clarification(question: str, schemes: list[str], resolved: dict,
                                  *, already_all_combined: bool = False) -> bool:
    """True when Focus Plus is the ONLY scheme in play, the question is a
    metric / breakdown question, and it pins no tranche — not in its text
    (resolve_tranche_label found nothing during resolve_entities) and not via
    a resolved entity — and doesn't ask for a per-tranche breakdown or an
    explicit "all tranches combined". Callers must have run resolve_entities
    first so `resolved` reflects any tranche actually named. Scoped to
    single-scheme Focus Plus questions only, not "Focus Plus" in schemes —
    a cross-scheme comparison (schemes has more than one entry) wants one
    row per scheme, not Focus Plus fragmented into its four tranches on top,
    and that flow already has its own tuned behaviour this must not disturb.

    `already_all_combined` (optional): True when this session already
    answered a Focus Plus question with "all tranches combined" earlier in
    the conversation (see context_manager's ConversationState.tranche_all_combined
    / session_store) and the current turn is a short follow-up that never
    re-says "tranche" at all — e.g. "give me top 3 only" right after "...
    across all tranches". Without this, that kind of bare follow-up has no
    tranche cue of its own, re-trips this gate, and either re-asks a question
    the user just answered or (worse) falls through to the SQL generator with
    no tranche signal and no memory of the choice already made."""
    if not settings.TRANCHE_CLARIFY_ENABLED:
        return False
    if (schemes or []) != ["Focus Plus"]:
        return False
    q = question or ""
    if resolved.get("tranche_label"):
        return False
    if already_all_combined:
        return False
    if _TRANCHE_BREAKDOWN_CUE.search(q) or _ALL_TRANCHES_CUE.search(q):
        return False
    # A question about a person-level / cohort-locked column is always
    # confined to the 12.5K cohort at Tranch 4 regardless of what the user
    # says about tranche — asking "which tranche?" has no real answer to
    # collect, so skip straight past the gate.
    if _PERSON_LEVEL_COLUMN_CUE.search(q):
        return False
    # Trigger ONLY when the question literally says "tranche"/"tranch" without
    # pinning which one. `_METRIC_OR_BREAKDOWN_CUE` used to also trigger this
    # gate (mirroring the year gate), but that cue matches "beneficiar*" and
    # "payments?" — i.e. almost every Focus Plus money/count question, not
    # just tranche-sensitive ones. None of the Focus Plus few-shot SQL (totals,
    # geography, gender, batch, status, FY breakdowns) scopes by tranche_label
    # at all — SUM(amount_disbursed) is well-defined across tranches, and the
    # one genuine mix-ratio trap (a naive per-beneficiary AVERAGE) is handled
    # by scoping to batch_label, not by asking the user to pick a tranche (see
    # "What is the average Focus Plus payment per member?" in
    # focusplus_few_shot.yaml). Confirmed live 2026-09-12: this over-broad
    # trigger paused nearly every Focus Plus beneficiary/disbursement question
    # for an unwanted tranche pick, and a scope-pause reply that re-entered
    # here with tranche_label already resolved but not yet threaded through
    # the merged question text sent the SQL generator into a repair loop that
    # burned the retry budget and crashed out to the KB fallback.
    if not _MENTIONS_TRANCHE_WORD.search(q):
        return False
    return True


def _tranche_clarification(question: str) -> "ClarificationNeeded":
    stem = question.strip().rstrip(" ?.")
    labels = tranche_labels("Focus Plus")
    options = [
        {"label": lbl, "question": f"{stem} for {lbl}"}
        for lbl in labels
    ]
    options.append({
        "label": "All tranches combined",
        "question": f"{stem} across all tranches",
    })
    label_list = ", ".join(labels[:-1]) + f" and {labels[-1]}" if len(labels) > 1 else labels[0]
    return ClarificationNeeded(
        f"Focus Plus data is split into {label_list}. Which of these is required "
        "— a single tranche, or all of them combined?",
        options=options,
        rule="tranche-not-specified",
    )


# ── "That year isn't in the data" guard ────────────────────────────────────
# Year coverage per scheme lives in _SCHEME_DATA_YEARS (loaded from megh_db at
# startup by refresh_scheme_years()). This guard is PER SCHEME: a year MGNREGA
# lacks (say FY 2019-20) but PMAY-G has is fine for a PMAY-G question and only
# blocked for an MGNREGA one.
#
# A question that names a financial year no scheme in play holds ("1999-20",
# "2010", "FY 2027-28", or "2019-20" for MGNREGA) can't be answered. Without the
# guard the year is dropped as a note and the flow either falls into the generic
# "which year?" pause (looks like we ignored what the user typed) or answers
# across the years that DO exist (a figure for the wrong period). Instead: tell
# the user which years that scheme has, with those years as one-tap chips.

# A raw mention that is clearly meant as a year/financial-year even though it
# didn't resolve — a 4-digit 19xx/20xx/21xx, or an "NN-NN" / "NNNN-NN" range.
_YEAR_SHAPED_RE = re.compile(
    r"\b(?:19|20|21)\d{2}\b|\b\d{2}\s*[-/]\s*\d{2}\b|\b\d{4}\s*[-/]\s*\d{2,4}\b"
)


def _looks_like_year_mention(text: str) -> bool:
    return bool(_YEAR_SHAPED_RE.search(text or ""))


def _allowed_year_starts(schemes: "list[str] | None") -> set:
    """FY start years the given scheme(s) actually hold (union). Empty schemes =>
    every scheme's years."""
    years, _live = _available_years_for(schemes or [])
    return {_fy_start(y) for y in years}


def _year_in_data_range(year_key: int, schemes: "list[str] | None" = None) -> bool:
    return year_key in _allowed_year_starts(schemes)


# A token that is unambiguously a financial year: an "NNNN-NN" / "NNNN-NNNN"
# range (any century), a 19xx or 21xx four-digit year, or a 20xx year in the
# 2010-2039 plausible band. A bare "2000" / "2500" is deliberately NOT matched
# so "top 2000 villages" isn't mistaken for a year.
_YEAR_RANGE_TOKEN_RE = re.compile(
    r"\b(?:fy\s*|financial\s+year\s*|fiscal(?:\s+year)?\s*)?"
    r"((?:19|20|21)\d\d\s*[-/]\s*\d{2,4}|(?:19|21)\d\d|20[1-3]\d)\b",
    re.IGNORECASE,
)


_MALFORMED_YEAR_CUE_RE = re.compile(
    # "of" deliberately excluded — it's the ending of ordinary amount phrasing
    # too ("total expenditure of 150000"), not just year phrasing, so it would
    # false-positive on a real currency figure. "in"/"for"/"during"/"fy" are
    # unambiguously temporal.
    r"\b(?:in|for|during|fy|financial\s+year|fiscal(?:\s+year)?)\s*[:\-]?\s*(\d{4,8})"
    r"\s*[?.!]*\s*\Z",
    re.IGNORECASE,
)


def _years_in_question(text: str, schemes: "list[str] | None" = None
                       ) -> "tuple[list[str], list[str]]":
    """(available, unavailable) raw year tokens named in `text`.

    _out_of_range_year_in below returns only the FIRST unavailable token, which
    is the right answer for "is there a bad year here?" but the wrong basis for
    "should the whole question be refused?" — a question can name a year the
    scheme lacks AND a year it holds ("compare FY 2023-24 and FY 2024-25", the
    Focus Legacy gap case). Refusing that discards a question the data can
    largely answer."""
    ok: list[str] = []
    bad: list[str] = []
    for m in _YEAR_RANGE_TOKEN_RE.finditer(text or ""):
        tok = m.group(1)
        yk = _parse_year_key(tok)
        if yk is not None:
            (ok if _year_in_data_range(yk, schemes) else bad).append(tok)
        elif _YEAR_SHAPED_RE.search(tok):
            bad.append(tok)
    return ok, bad


# A question that asks for a COMPARISON needs two operands. Dropping one of
# them answers a different, narrower question than the one asked.
_COMPARES_YEARS = re.compile(
    r"\bcompare\b|\bcomparison\b|\bversus\b|\bvs\.?\b|\bagainst\b|"
    r"\bdifference between\b|\bbetween\b.{0,40}\band\b|"
    r"\bhigher than\b|\blower than\b|\bmore than\b.{0,20}\bfy\b",
    re.IGNORECASE,
)


def _nearest_available_year(bad_token: str, schemes: "list[str] | None") -> "str | None":
    """The year with data closest to `bad_token`, preferring the one BEFORE it.

    The scheme's own contract calls for exactly this on a gap comparison —
    focuslegacy_few_shot.yaml: "The preceding year WITH PAYMENTS is FY2022-23,
    not FY2023-24", and response_template's fy_gap_comparison_note. Preferring
    the earlier year keeps "compare X with the year before it" meaning what it
    says; a later year is used only when nothing earlier exists."""
    target = _parse_year_key(bad_token)
    if target is None:
        return None
    available, _live = _available_years_for(schemes or [])
    keys = sorted(k for k in (_parse_year_key(y) for y in available) if k is not None)
    if not keys:
        return None
    earlier = [k for k in keys if k < target]
    if earlier:
        return _fy_short(earlier[-1])
    later = [k for k in keys if k > target]
    return _fy_short(later[0]) if later else None


def _substitute_year_token(question: str, old_tok: str, new_short: str) -> str:
    """Replace one year token in place, keeping the surrounding wording (and any
    "FY " prefix) so the sentence still reads as the comparison it is."""
    return re.sub(
        r"(?:fy\s*)?" + re.escape(old_tok.strip()) + r"\b",
        f"FY {new_short}", question, count=1, flags=re.IGNORECASE,
    )


def _apply_year_gap(question: str, schemes: "list[str] | None"
                    ) -> "tuple[str, str | None, bool]":
    """Decide what to do with a question naming a financial year the scheme has
    no data for. Returns (question, note, handled).

    `handled` is False only when NOTHING in the question is answerable — the
    caller then raises the out-of-range clarification. Otherwise the question is
    rewritten and a note explains what changed, so the answer states the gap
    rather than silently ignoring it.

    Two rewrites, and which one applies is the whole point:

    * COMPARISON ("compare FY 2023-24 and FY 2024-25", "X vs Y") — a comparison
      needs two operands, so the absent year is SUBSTITUTED with the nearest
      year that holds data. Deleting it left one year and answered with a single
      figure, leaving the verb the user typed unmet (reported 2026-09-23). The
      substitute is the preceding year with payments, which is exactly what the
      scheme's own exemplar and fy_gap_comparison_note prescribe.
    * ANYTHING ELSE — the absent year is dropped. There is no second operand to
      preserve, and substituting would answer about a year the user never named.

    Lives here rather than inline in resolve_entities so the tests exercise the
    real decision: an earlier version of this logic was inline, the regression
    test re-implemented it, and the test stayed green when the production branch
    was disabled."""
    ok_years, bad_years = _years_in_question(question, schemes)
    if not ok_years:
        return question, None, False

    swapped: list[tuple[str, str]] = []
    if _COMPARES_YEARS.search(question) and len(ok_years) + len(bad_years) >= 2:
        for bad in list(bad_years):
            near = _nearest_available_year(bad, schemes)
            if near and near not in ok_years:
                question = _substitute_year_token(question, bad, near)
                ok_years.append(near)
                bad_years.remove(bad)
                swapped.append((bad, near))
    if bad_years:
        question = _strip_year_tokens(question, bad_years)

    parts: list[str] = []
    if swapped:
        parts.append(
            "; ".join(
                f"FY {b} holds no data for this scheme, so FY {n} — the nearest "
                f"financial year that does — is compared instead"
                for b, n in swapped
            )
            + ". That is a gap in the records, not a zero."
        )
    if bad_years:
        parts.append(
            ", ".join(f"FY {y}" for y in bad_years)
            + " holds no data for this scheme, so it is left out — "
            "that is a gap in the records, not a zero."
        )
    logger.info("year gap: swapped=%s dropped=%s, answering for %s",
                swapped, bad_years, ok_years)
    return question, (" ".join(parts) or None), True


def _strip_year_tokens(question: str, tokens: "list[str]") -> str:
    """Remove the given year tokens (and any "for "/"in "/"FY " lead-in, or a
    dangling "and"/",") from the question, so what remains reads naturally and
    cannot re-trip the year guard on a later turn."""
    out = question
    for tok in tokens:
        # Take any connector that FOLLOWS the token too ("between FY 2023-24
        # and ..." -> "between ..."), otherwise dropping the first of a pair
        # leaves a dangling "and": "Compare the total remittance and FY
        # 2024-25." Both sides are optional, so a lone year is still removed.
        out = re.sub(
            r"\s*(?:,|\band\b)?\s*(?:for\s+|in\s+|during\s+|of\s+|between\s+)?"
            r"(?:fy\s*)?" + re.escape(tok.strip()) + r"\b\s*(?:,|\band\b)?",
            " ", out, count=1, flags=re.IGNORECASE,
        )
    out = re.sub(r"\s{2,}", " ", out)
    # "between" / "from" left with nothing to join, and a trailing connector
    # before the closing punctuation.
    out = re.sub(r"\b(?:between|from)\s+(?=[\"\u201d.?]|$)", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\s+(?:and|,)\s*(?=[\"\u201d.?]|$)", "", out, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", out).strip().strip(",").strip()


def _out_of_range_year_in(text: str, schemes: "list[str] | None" = None) -> "str | None":
    """Raw text of the first financial-year token in `text` that NONE of the
    given scheme(s) hold, or None if every year mentioned is available / none is
    mentioned. Runs on raw text only — no LLM, no DB."""
    for m in _YEAR_RANGE_TOKEN_RE.finditer(text or ""):
        tok = m.group(1)
        yk = _parse_year_key(tok)
        if yk is not None:
            if not _year_in_data_range(yk, schemes):
                return tok
        elif _YEAR_SHAPED_RE.search(tok):   # e.g. "1999-20" — a year we can't parse
            return tok
    # A digit run right after an explicit temporal cue, at the very END of the
    # question ("in 20217", "...beneficiaries in Bamil Reserve Apal in 20217?")
    # that _YEAR_RANGE_TOKEN_RE above didn't match at all — its 20[1-3]\d
    # alternative needs a \b right after the 4th digit, so one extra trailing
    # digit ("20217") makes the WHOLE token invisible to it, not just
    # unparseable. Without this, "in 20217" skipped the guard entirely and the
    # SQL generator copied the literal straight into year_key = 20217, ran
    # clean, and silently returned 0 (2026-09-09 bug report — no clarification
    # was ever offered). Anchored to end-of-question (not just gated on a cue
    # word) so a real amount stated mid-sentence ("sanctioned amount of 500000
    # in Siju") is never mistaken for a year — a year mention is normally the
    # last thing named in these questions, an amount normally isn't.
    m = _MALFORMED_YEAR_CUE_RE.search(text or "")
    if m and _parse_year_key(m.group(1)) is None:
        return m.group(1)
    return None


def _available_years_for(schemes: list[str]) -> "tuple[list[str], list[str]]":
    """(distinct FY list across the given scheme(s), scheme names used). Falls
    back to every scheme when `schemes` is empty or unrecognised.

    Years are merged scheme-by-scheme, so without an explicit sort the result
    lands in whatever order the schemes happen to combine in (e.g. MGNREGA's
    2022-23..2025-26 ahead of PMAY-G's earlier 2017-18..2021-22) rather than
    chronological order. Sort ascending (oldest first) so the year chips read
    in a sane order regardless of scheme combination."""
    live = [s for s in (schemes or []) if s in _SCHEME_DATA_YEARS] or list(_SCHEME_DATA_YEARS)
    seen: set[str] = set()
    years: list[str] = []
    for s in live:
        for y in _SCHEME_DATA_YEARS[s]:
            if y not in seen:
                seen.add(y)
                years.append(y)
    years.sort(key=_fy_start)
    return years, live


def _year_out_of_range_clarification(question: str, raw_year: str,
                                    schemes: list[str]) -> "ClarificationNeeded":
    stem = question.strip().rstrip(" ?.")
    # Strip the offending year (and any "for "/"in "/"FY " lead-in, or a bare
    # comma before it) out of the stem, so the chip questions we build below
    # don't carry it back in and trip this same pause on the next turn.
    stem = re.sub(
        r"\s*,?\s*(?:for\s+|in\s+|during\s+|of\s+)?(?:fy\s*)?" +
        re.escape(raw_year.strip()) + r"\b",
        "", stem, count=1, flags=re.IGNORECASE,
    ).strip().rstrip(",").strip() or question.strip().rstrip(" ?.")

    years, live = _available_years_for(schemes)
    if not years:
        # Every scheme in play has NO time dimension at all (CM Elevate: no
        # year_key, no date column of any kind) — there is no "these years are
        # available" chip list to offer. Refuse the time filter outright rather
        # than showing an out-of-range message with zero year options.
        scheme_word = live[0] if len(live) == 1 else (" and ".join(live) if live else "This scheme")
        return ClarificationNeeded(
            f"{scheme_word} has no date field in this data, so records cannot be "
            f"placed in a financial year or any other time period. I can answer "
            "without a time filter instead.",
            options=[{"label": "Answer without a time filter", "question": stem}],
            rule="no-time-dimension",
        )
    if len(live) == 1:
        coverage = (f"For {live[0]}, data is available only for the financial "
                    f"years {', '.join(years)}.")
    else:
        per_scheme = "; ".join(
            f"{s} — {', '.join(_SCHEME_DATA_YEARS[s])}" for s in live
        )
        coverage = ("Data is available only for the following financial years: "
                    f"{per_scheme}.")

    options = [
        {"label": f"FY {y}", "question": f"{stem} for FY {y}"}
        for y in years
    ]
    options.append({
        "label": "All available years combined",
        "question": f"{stem} across all financial years",
    })
    return ClarificationNeeded(
        f"{coverage} No data is held for “{raw_year.strip()}”. "
        "Please select one of the financial years listed above, or all of them "
        "combined.",
        options=options,
        rule="year-out-of-range",
    )


def _named_schemes(question: str) -> list[str]:
    """Schemes named outright in the question text, in catalog order. Falls
    back to fuzzy matching (_fuzzy_named_schemes) when the exact regex finds
    nothing — otherwise a misspelled scheme name ("manrega", "pamay") reads as
    "no scheme named" and forces an unnecessary clarification prompt instead
    of being understood."""
    exact = [s for s, pattern in _SCHEME_NAME_PATTERN.items() if pattern.search(question)]
    if exact:
        return exact
    return _fuzzy_named_schemes(question)


def _shortcut_scheme(question: str) -> list[str] | None:
    """Skip the classifier call when the text already pins the scheme set —
    faster and more reliable than the model for the common case, and one fewer
    model call per query at scale. Deterministic whenever:
      * two or more schemes are named  -> use exactly those (the user listed
        them; "compare MGNREGA and PMAY" needs no classification), or
      * an explicit cross-scheme phrase ("both schemes", "across schemes")
        is present  -> the whole catalog, or
      * exactly one scheme is named    -> that one, or
      * no scheme is named but the vocabulary pins exactly one (same check
        _needs_scheme_clarification uses to skip its own pause) -> that one.
    Without this last case, a vocabulary-only question ("sanctioned amount",
    "person-days") skipped the "which scheme?" pause (correctly inferring
    MGNREGA/PMAY-G) but then still went to the LLM classifier here, which
    doesn't apply the same vocabulary rule and can return every scheme "to be
    safe" — inconsistent with the pause having just been skipped, and it broke
    the PMAY-G year-gate exemption (which only applies when the scheme set is
    exactly ["PMAY-G"]).
    Returns None only when nothing in the text pins it, so the classifier still
    handles the genuinely unnamed/ambiguous case."""
    named = _named_schemes(question)
    if len(named) >= 2:
        return named
    # CM Elevate Legacy's 13 sub-units are themselves called "schemes"
    # (Piggery, Poultry, ...), so "CM Elevate Legacy records by scheme" /
    # "scheme-wise" asks for ITS scheme_name breakdown — not every scheme in
    # the catalog. Read as cross-scheme, it pulled in all six schemes and the
    # year pause offered their union (FY 2017-18..2025-26) for a scheme that
    # holds only FY 2024-25 and 2025-26.
    if named == ["CM Elevate Legacy"]:
        return named
    if _EXPLICIT_BOTH.search(question):
        return list(SCHEME_CATALOG)
    if named:
        return named
    return _infer_scheme_from_terms(question)


async def classify_scheme(question: str) -> list[str]:
    """Which scheme(s) — MGNREGA, PMAY-G, or both — does this question touch?"""
    shortcut = _shortcut_scheme(question)
    if shortcut:
        logger.info("classify_scheme: shortcut matched %s, skipping model call", shortcut)
        return shortcut

    catalog = "\n".join(f'  - "{name}": {desc}' for name, desc in SCHEME_CATALOG.items())
    scheme_options = " | ".join(f'"{name}"' for name in SCHEME_CATALOG)
    prompt = f"""Classify which scheme(s) this question needs. Available schemes:
{catalog}

Return ONLY JSON: {{"schemes": [{scheme_options}, ...]}}
Use several if the question compares or combines schemes. If it names none specifically
and gives no scheme-specific vocabulary, return every scheme.

Question: "{question}"
JSON:"""
    raw = await llm.call_classifier(prompt, guided={"guided_json": _SCHEMES_JSON_SCHEMA})
    payload = _extract_json(raw)
    if payload and isinstance(payload.get("schemes"), list):
        schemes = [s for s in payload["schemes"] if s in SCHEME_CATALOG]
        # Safety net: a scheme the user named outright must never be dropped by
        # the classifier. Union it back in (catalog order) so "across MGNREGA and
        # PMAY-G" can't come back PMAY-only.
        for s in _named_schemes(question):
            if s not in schemes:
                schemes.append(s)
        if schemes:
            return [s for s in SCHEME_CATALOG if s in schemes]
    # Deterministic fallback if the model call fails or returns unusable JSON.
    logger.warning("classify_scheme: unusable response %r — defaulting to both schemes", raw[:200])
    return list(SCHEME_CATALOG)


# Bare dimension nouns and the state name are NOT place mentions — "which
# villages have activity in Meghalaya" names no specific village or district.
# The classifier sometimes extracts them anyway; drop them before resolution,
# or `resolve_village("villages")` fuzzy-matches "Model Village" etc. and raises
# a nonsense clarification.
_GENERIC_PLACE_TERMS = {
    "village", "villages", "vill", "hamlet", "hamlets", "gaon",
    "district", "districts", "dist", "distt",
    "block", "blocks", "dev block", "development block", "cd block", "c.d. block",
    "panchayat", "panchayats", "gram panchayat", "gp", "gps", "vec",
    "state", "meghalaya", "region", "regions", "area", "areas",
    "place", "places", "location", "locations", "zone", "zones",
    "year", "years", "fy", "financial year", "all", "every", "each", "any",
}


# Real Meghalaya village names carry a parenthesised suffix that is part of the
# name: "NONGCHRAM (I)", "NONGCHRAM (II)", "Existing site(Old House)". Stripping
# trailing punctuation blindly removed the CLOSING paren while leaving the
# opening one, so "NONGCHRAM (I)" became "NONGCHRAM (I" — which matches no
# stored name exactly, stays permanently ambiguous, and makes the
# village-disambiguation chip regenerate the identical question forever
# (reported 2026-09-17: the pause repeated on every click). Strip only what is
# genuinely punctuation around the name, and keep a closing bracket whenever it
# balances an opening one still inside the value.
_MENTION_EDGE_CHARS = "\"'`.,?!;: \t"


def _drop_place_phrase(question: str, name: str) -> str:
    """Remove "in <name>" from `question`, taking any part-marker suffix with
    it. Stripping the bare name left an orphan fragment behind — dropping
    "NONGSPUNG" from "... in NONGSPUNG - A, UMLING block, RI BHOI" produced
    "... to be released - A, UMLING block, RI BHOI", which then read as a
    brand-new question and sent the next few turns badly wrong (reported
    2026-09-17). Also tidies a doubled comma left by the removal."""
    out = re.sub(
        rf"\s*\b(?:in|for|of|at|from|within)\s+{re.escape(name)}"
        r"(?:\s*\([^)]{0,20}\)|\s*-\s*[A-Za-z0-9]{1,12})?",
        "", question or "", count=1, flags=re.IGNORECASE)
    out = re.sub(r"\s*,\s*,", ",", out)
    return re.sub(r"\s{2,}", " ", out).strip().lstrip(",").strip()


def _place_title(value: str) -> str:
    """Title-case a place name without mangling a roman-numeral or acronym
    suffix: str.title() turns "NONGCHRAM (II)" into "Nongchram (Ii)". Any
    parenthesised run of roman numerals / digits is preserved as-is."""
    out = str(value or "").title()
    out = re.sub(r"\(([IVXLCDM\d]+)\)", lambda m: "(" + m.group(1).upper() + ")",
                 out, flags=re.IGNORECASE)
    # A standalone numeral word too: "NANDICHAR II" -> "Nandichar II", not "Ii"
    # (Focus Plus all-villages run 2026-09-27).
    return re.sub(r"(?<![A-Za-z])(Ii|Iii|Iv|Vi|Vii|Viii|Ix|Xi|Xii)(?![A-Za-z])",
                  lambda m: m.group(1).upper(), out)


def _strip_mention_punctuation(value: str) -> str:
    v = (value or "").strip()
    # Leading: brackets are never part of a name at the start.
    v = v.lstrip(_MENTION_EDGE_CHARS + "([{")
    # Trailing: drop plain punctuation always, but a bracket only when it is
    # unbalanced (i.e. nothing opened it earlier in the value).
    while v:
        last = v[-1]
        if last in _MENTION_EDGE_CHARS:
            v = v[:-1]
            continue
        if last in ")]}":
            opener = {")": "(", "]": "[", "}": "{"}[last]
            if v.count(opener) >= v.count(last):
                break            # balanced — "(I)" belongs to the name
            v = v[:-1]
            continue
        break
    return v.strip()


def _clean_mention(value: str) -> str | None:
    """Normalise a raw mention; return None if it's a bare dimension word / the
    state name (i.e. not an actual place or period)."""
    v = _strip_mention_punctuation(value)
    core = re.sub(r"^(the|a|an|this|that|each|every|all)\s+", "", v, flags=re.IGNORECASE).strip()
    if not core or core.lower() in _GENERIC_PLACE_TERMS:
        return None
    # A scheme name is never a place — "... for Focus Plus" reads like a place
    # after a preposition (the extractor prompt deliberately teaches it to
    # follow "of"/"for"/"under" onto place names), so the model occasionally
    # tags the scheme itself as the district/block/village. Reject it here as
    # a deterministic backstop regardless of what the LLM returned, or it goes
    # on to fail district/block/village resolution and gets reported as a
    # place "not in Meghalaya" (reported 2026-09-10: "which district received
    # highest total disbursement for Focus Plus" -> mentions.district ==
    # "Focus Plus" -> OutOfScope).
    if any(rx.search(core) for rx in _SCHEME_NAME_PATTERN.values()):
        return None
    return v


def _mention_in_question(value: str, question: str) -> bool:
    """True when `value` actually occurs in `question`, case-insensitively and
    tolerant of whitespace differences. The extractor prompt requires every
    span to be copied verbatim from the question text, so a value that fails
    this check isn't a real span — it's a hallucination, not an extraction.
    Guards against the classifier echoing one of its own few-shot examples
    (reported 2026-09-11: "Top 5 CM Elevate schemes by applications" — no
    year mentioned anywhere in the text — came back with {"year": "2017-18"},
    lifted straight from the FY 2017-18 example in the prompt. For a
    zero-time-dimension scheme like CM Elevate that phantom year immediately
    tripped the "no date field" refusal, and because "2017-18" isn't literally
    in the question, the refusal's own year-stripping regex had nothing to
    strip — the follow-up chip resent the identical question text and looped
    forever)."""
    norm = lambda s: re.sub(r"\s+", " ", s or "").strip().lower()
    return norm(value) in norm(question)


# A name the question itself introduces as a PRODUCER GROUP. Focus Legacy group
# names routinely collide with real places ("Nongstoin PG", "Mairang Producer
# Group") because groups are named after where they are, so the LLM extractor
# tags them as a block or village despite being told not to — and the geography
# branches then ask "did you mean the block or the village?" about a name the
# user never offered as a place (reported 2026-09-23).
_PG_NAMED_ENTITY = re.compile(
    r"\b(?:producer[\s-]?group|pg|group)s?\s+"
    r"(?:named|called|by the name of)\s+"
    r"(?:as\s+)?"
    # Name words may start with a digit or a bracket \u2014 "Bak 15 Banana Dijogre",
    # "Ieintylli Pg (cham Cham Pig Fattening Pg)" \u2014 and run to 8 words.
    # Any non-space run is a name word: 219 of the 9,452 stored names have words
    # like "-13", "Pg-83/21", "[pg]" or a lone "'" (Focus Legacy all-PG run
    # 2026-09-29), and the old letter/digit/bracket start missed every one.
    r"[\"\u201c\u2018']?(?P<name>[^\s?\"\u201c\u201d]+(?:\s+[^\s?\"\u201c\u201d]+){0,7}?)"
    # The name ends the sentence, or is followed by a place / scope phrase:
    # "named Nongstoin PG in Betasing block" \u2014 without the second branch the
    # whole pattern missed and "Nongstoin" was resolved as a BLOCK.
    r"[\"\u201d\u2019']?\s*(?=[?.,;:]|$|(?:in|for|under|from|at|within|across|there)\b)",
    re.IGNORECASE,
)
# The group-type suffix, so "Nongstoin PG" strips to the core "Nongstoin" that
# the extractor actually tagged as a block.
_PG_SUFFIX = re.compile(
    r"[\s,]*\b(?:producer\s+groups?|producer\s+grp|p\.?\s*g\.?|group)\.?\s*$",
    re.IGNORECASE,
)


def _question_without_pg_name(question: str) -> str:
    """The question with a producer-group NAME the user labelled as one blanked
    out, for the place-name SCANS in resolve_entities. _drop_producer_group_names
    only cleans the extractor's mentions; the admin-level gate and the block
    backstop then re-scan the raw text and put the name straight back — "Is
    there any Producer Group named Nongstoin PG?" (Focus Legacy QA TC-13,
    2026-09-25) became "Nongstoin: the block or the village?", and the chip
    turned a name lookup into a geography question. Unchanged when no group is
    named."""
    m = _PG_NAMED_ENTITY.search(question or "")
    if not m:
        return question
    return (question[: m.start("name")] + question[m.end("name"):]).strip()


def _drop_producer_group_names(question: str, mentions: dict) -> dict:
    """Remove geography mentions that the question introduced as a PRODUCER
    GROUP name. The name still reaches SQL generation in the question text,
    where the pg_name ILIKE rule handles it; what must not happen is a
    block/village disambiguation about a group the user named.

    Narrow by construction: only a name the question itself labelled with group
    phrasing is stripped, so "disbursement in Nongstoin" still resolves as
    geography."""
    m = _PG_NAMED_ENTITY.search(question or "")
    if not m:
        return mentions
    named = m.group("name").strip()
    core = _PG_SUFFIX.sub("", named).strip().lower()
    if not core:
        return mentions

    out = dict(mentions)
    for dim in ("district", "block", "village"):
        val = str(out.get(dim) or "").strip().lower()
        if val and (val == core or val in core or core in val):
            out.pop(dim, None)
            logger.info("dropped %s mention %r — the question names it as a producer group",
                        dim, mentions.get(dim))
    for dim in ("districts", "blocks"):
        vals = out.get(dim)
        if isinstance(vals, list):
            kept = [v for v in vals
                    if str(v).strip().lower() not in (core,)
                    and core not in str(v).strip().lower()]
            if len(kept) != len(vals):
                logger.info("dropped %s entries naming the producer group %r", dim, named)
            if kept:
                out[dim] = kept
            else:
                out.pop(dim, None)
    return out


# An unmistakable financial-year range: "2024-25", "FY 2024-25", "2024-2025".
# Never the head of a calendar date: "sanctioned on 2017-11-28" read "2017-11"
# as FY 2017-18, and the composer then scoped a single-day count to that FY
# (2026-09-27 screenshot). The lookahead rejects a third -NN / /NN part.
_EXPLICIT_FY_RANGE_RE = re.compile(
    r"\b((?:19|20|21)\d\d\s*[-/]\s*\d{2}(?:\d{2})?)\b(?!\s*[-/]\s*\d)")


def _backfill_explicit_year(question: str, schemes: list[str], mentions: dict) -> dict:
    """Fill the year slot from the text when the LLM extractor dropped it.

    The extractor sometimes returns no "year" for a question that plainly names
    one (TC-23, 2026-09-25: "What was the total amount remitted in FY 2024-25
    for Focus Legacy" -> {}). With no year_key resolved, the scope gate then
    asked "which area and time period?" and its "all years" chip made the SQL
    drop the FY — ₹51.01 Cr (every year) reported as FY 2024-25's ₹11.50 Cr.

    Deliberately narrow: only an explicit NNNN-NN range, only when exactly one
    distinct one is named, and only when the scheme holds that year (an absent
    year is the year-gap guard's business, already settled before this runs)."""
    if mentions.get("year"):
        return mentions
    toks = {yk for m in _EXPLICIT_FY_RANGE_RE.finditer(question or "")
            if (yk := _parse_year_key(m.group(1))) is not None}
    if len(toks) != 1:
        return mentions
    yk = next(iter(toks))
    if not _year_in_data_range(yk, schemes):
        return mentions
    fy = f"{yk}-{(yk + 1) % 100:02d}"
    logger.info("year mention back-filled from the question text: FY %s", fy)
    return {**mentions, "year": fy}


async def extract_entity_mentions(question: str) -> dict:
    """Which spans of the question name a district, block, village, year or
    assembly constituency? Span-finding only — resolving each span to a
    canonical DB value is a separate, deterministic step (entity_resolver),
    not this LLM call."""
    prompt = f"""Extract place/time names from this question, verbatim as the user typed
them. Do not correct spelling or guess the canonical form.

Return ONLY JSON with keys from: "district", "block", "village", "year",
"assembly_constituency", "blocks", "districts". Include a key ONLY when the
question NAMES a specific one. A bare word like "village", "villages",
"district", "block", "year", or the state name "Meghalaya" is NOT a name —
omit it. If the question names none, return {{}}.

When the question names TWO OR MORE blocks to compare against each other
("compare X and Y", "X vs Y", "X and Y, the block, not the village"), put ALL
of their names in the plural "blocks" array — do NOT use the singular "block"
key and keep only one, that silently drops the other block from the answer.
Use "block" only when exactly one block is named. The same rule applies to
districts: TWO OR MORE named to compare against each other go in the plural
"districts" array, never the singular "district" key — even when one of them
is a short acronym like "EKH" or "WGH". Use "district" only when exactly one
district is named.

Block, assembly-constituency and village names overlap heavily in Meghalaya —
a bare name (e.g. "Mawlai") could be any of the three. Use "assembly_constituency"
ONLY when the question itself signals a constituency: it says "constituency",
"AC", "assembly", "MLA", or gives a number before the name (e.g. "15 Mawlai",
"AC 9"). Otherwise tag an ambiguous bare name as "block".

A place name can follow ANY preposition, not just "in"/"for" — "disbursement
OF Selsella", "expenditure OF West Garo Hills", "spending OF Abagre" all name
Selsella/West Garo Hills/Abagre as the AREA the figure is about, exactly like
"disbursement in Selsella" would. Do not read "of" as meaning the metric noun
(disbursement, expenditure, spending, amount, total) is itself the thing being
named — the name after "of" is still a place, and must still be extracted.

A SCHEME name (MGNREGA, PMAY-G, Focus Plus, CM Elevate, Focus Legacy, or a
close variant) is NEVER a place — do not extract it as a district/block/village
even when it follows "for"/"of"/"under" exactly like a place would
("disbursement for Focus Plus" names the scheme, not an area; extract nothing).

A PRODUCER GROUP name or id is NEVER a place either. A Focus Legacy pg_id looks
like "PG-FOCUS-WGH-7089" and contains a district abbreviation — do NOT extract
that abbreviation, or any part of the id, as a district. A producer group NAME
("Muskan Producer Group", "Bak 15 Banana", "Iainehlang Pg") often reads like a
village name; it is a group, not an area, so extract nothing from it.

Likewise, a CM Elevate SUB-SCHEME name is NEVER a place, even though several
of them sound like plausible village/block names in isolation: Piggery,
Poultry, Dairy, Goat (Farming), Warehouse, Sericulture (& Weaving), Green
Taxi, Motorcaravan, Cinema Theatre, Sports & Wellness (Centre), Any Business
Venture, Agro Tourism Villa, PRIME Tourism Vehicle, PRIME Agriculture
Response Vehicle, PRIME Small Enterprise Empowerment / SEED. "What is the
status distribution for Piggery?" names a sub-scheme, not a district, block
or village — extract nothing.

Examples:
Question: "Tell me about total disbursement of Selsella across all financial years for MGNREGA."
JSON: {{"block": "Selsella"}}
Question: "What is the total expenditure of West Garo Hills under PMAY-G?"
JSON: {{"district": "West Garo Hills"}}
Question: "how many job cards issued in Ri Bhoi"
JSON: {{"district": "Ri Bhoi"}}
Question: "What is the status distribution for Piggery?"
JSON: {{}}
Question: "How many applicants under Poultry are on hold?"
JSON: {{}}
Question: "show me MGNREGA spend"
JSON: {{}}
Question: "Compare the sanctioned amounts of Dambo Rongjeng and Samanda, the block, not the village"
JSON: {{"blocks": ["Dambo Rongjeng", "Samanda"]}}
Question: "Compare PMAY performance between ekh and wgh for FY 2017-18"
JSON: {{"districts": ["ekh", "wgh"], "year": "2017-18"}}
Question: "which district received highest total disbursement for Focus Plus"
JSON: {{}}

Question: "{question}"
JSON:"""
    raw = await llm.call_classifier(prompt, guided={"guided_json": _ENTITY_JSON_SCHEMA})
    payload = _extract_json(raw)
    if not isinstance(payload, dict):
        return {}
    out: dict[str, object] = {}
    for k, v in payload.items():
        if k in ("district", "block", "village", "year", "assembly_constituency") \
                and isinstance(v, str) and v.strip():
            cleaned = _clean_mention(v)
            if cleaned and _mention_in_question(cleaned, question):
                out[k] = cleaned
    blocks_raw = payload.get("blocks")
    if isinstance(blocks_raw, list):
        cleaned_blocks: list[str] = []
        seen: set[str] = set()
        for v in blocks_raw:
            if not isinstance(v, str):
                continue
            c = _clean_mention(v)
            if c and _mention_in_question(c, question) and c.lower() not in seen:
                seen.add(c.lower())
                cleaned_blocks.append(c)
        if len(cleaned_blocks) >= 2:
            out["blocks"] = cleaned_blocks
        elif len(cleaned_blocks) == 1 and "block" not in out:
            # A single-element array is just a singular mention the model
            # phrased as a list — fold it back so the existing singular path
            # (block-vs-village disambiguation etc.) still runs for it.
            out["block"] = cleaned_blocks[0]
    districts_raw = payload.get("districts")
    if isinstance(districts_raw, list):
        cleaned_districts: list[str] = []
        seen_d: set[str] = set()
        for v in districts_raw:
            if not isinstance(v, str):
                continue
            c = _clean_mention(v)
            if c and _mention_in_question(c, question) and c.lower() not in seen_d:
                seen_d.add(c.lower())
                cleaned_districts.append(c)
        if len(cleaned_districts) >= 2:
            out["districts"] = cleaned_districts
        elif len(cleaned_districts) == 1 and "district" not in out:
            # A single-element array is just a singular mention the model
            # phrased as a list — fold it back so the existing singular path
            # runs for it.
            out["district"] = cleaned_districts[0]
    return out


def _parse_year_key(text: str) -> "int | None":
    """FY start year as an int from '2023', '2023-24', '2023-2024', 'FY 2023-24',
    'FY23', or a bare two-digit range like '25-26' with no century at all —
    users type financial years this way constantly (2026-09-10 UAT: "25-26" was
    not understood as a year), and without this branch the mention never
    resolves to a year_key at all. Returns None if no plausible year
    (2010-2039) is present."""
    m = re.search(r"\b(20[1-3]\d)\s*[-/]\s*(?:20)?\d{2}\b", text)   # 2023-24 / 2023-2024
    if m:
        return int(m.group(1))
    m = re.search(r"\bfy\s*'?(\d{2})\b", text, re.IGNORECASE)        # FY23
    if m:
        return 2000 + int(m.group(1))
    m = re.search(r"(?<!\d)(\d{2})\s*[-/]\s*(\d{2})(?!\d)", text)    # bare 25-26
    if m:
        y1, y2 = int(m.group(1)), int(m.group(2))
        if 10 <= y1 <= 39 and y2 == (y1 + 1) % 100:
            return 2000 + y1
    m = re.search(r"\b(20[1-3]\d)\b", text)                          # bare 2023
    if m:
        return int(m.group(1))
    return None


def _village_chip_question(question: str, raw_text: str, candidate: dict,
                           schemes: "list[str] | None" = None,
                           peers: "list[dict] | None" = None) -> str:
    """The follow-up question text for one village-disambiguation chip.

    The old approach appended "<block> block, <district>" to the ORIGINAL
    ambiguous fragment (e.g. "siju") and left the fragment itself unchanged.
    That only narrows resolve_village's search scope — it does not say WHICH
    candidate was picked. Two candidates that both fuzzy-match the same raw
    text and also sit in the same block (e.g. "siju" fuzzy-matches both SIJU
    SONGMONG and Siju Arteka, both in SIJU block, SOUTH GARO HILLS) stay tied
    even after the block/district is appended, so every chip regenerates the
    IDENTICAL question and the clarification loops forever — reported
    2026-09-09 for both an exact-duplicate dim_geography row (Asimgre, DALU
    block) and this fuzzy-match case (siju). Substituting the candidate's own
    canonical name into the question fixes it: re-resolution then runs an
    EXACT match on a name that is (almost always, and after the duplicate-row
    fix in entity_resolver.resolve_village, effectively always) unique.

    A second, compounding bug (also reported 2026-09-09, "nongthymmai" in
    EAST/WEST KHASI HILLS): when the LLM mention-extractor can't cleanly split
    a resumed chip's OWN text ("Nongthymmai, RI MULIANG block, WEST KHASI
    HILLS") into village/block, resolve_entities lands back on this same
    ambiguous-village branch and calls this function AGAIN — with `question`
    now already carrying the previous round's appended ", <block> block,
    <district>". Blindly appending another one lets the suffix grow every
    round ("..., RI MULIANG block, WEST KHASI HILLS, MAWSHYNRUT block, WEST
    KHASI HILLS, ...", naming more and more blocks at once), which defeats any
    single-candidate text match downstream and loops forever. Strip a
    previously-appended suffix (there is at most one meaningful one — this
    function is the only writer of that shape) before appending the current
    candidate's, so the text never grows past one such suffix."""
    q = _village_chip_question_text(question, raw_text, candidate, schemes)
    tag = _village_code_tag(candidate, peers, schemes)
    tail = f", {candidate['block']} block, {candidate['district']}"
    if tag and q.endswith(tail):
        head = q[:-len(tail)]
        nm = re.compile(rf"(?<![A-Za-z0-9]){re.escape(str(candidate['name']))}(?![A-Za-z0-9])", re.IGNORECASE)
        head = nm.sub(lambda m: m.group(0) + tag, head, count=1) if nm.search(head) else head + tag
        q = f"{head}{tail}"
    return q


# Two DIFFERENT villages can share a name AND a block: BOLDAMGRE 272895 and
# 272933 both sit in SELSELLA block, APALGRE 273033 and 273103 in RERAPARA. Their
# chips read identically ("BOLDAMGRE — SELSELLA block, WEST GARO HILLS"), so
# neither the officer nor the chip pin could tell them apart and the second
# village was unreachable (Focus Plus all-villages run 2026-09-27). The LGD code
# is added to the label and the chip question only when such a twin exists.
_CHIP_LGD_CODE_RE = re.compile(r"\(LGD (\d+)\)")


def _village_code_tag(candidate: dict, peers: "list[dict] | None", schemes: "list[str] | None") -> str:
    # Focus Legacy too: UMSAW and DEWSAW each exist twice in ONE block, and the
    # untagged chips pinned the wrong twin (all-villages run 2026-09-29, KI-163).
    if not peers or not (_village_scheme(schemes) or schemes == ["Focus Legacy"]):
        return ""
    key = lambda c: (str(c.get("name", "")).strip().upper(), str(c.get("block") or "").upper(),  # noqa: E731
                     str(c.get("district") or "").upper())
    twin = any(key(p) == key(candidate) and p.get("village_code") != candidate.get("village_code")
               for p in peers)
    return f" (LGD {candidate['village_code']})" if twin else ""


def _village_chip_label(candidate: dict, peers: "list[dict] | None" = None,
                        schemes: "list[str] | None" = None) -> str:
    return (f"{candidate['name']}{_village_code_tag(candidate, peers, schemes)} — "
            f"{candidate['block']} block, {candidate['district']}")


def _village_chip_question_text(question: str, raw_text: str, candidate: dict,
                                schemes: "list[str] | None" = None) -> str:
    stem = question.strip().rstrip(" ?.")
    stem = re.sub(r"(,\s*[^,]+?\s+block,\s*[^,]+)+$", "", stem, flags=re.IGNORECASE).rstrip()
    stem = _CHIP_LGD_CODE_RE.sub("", stem).replace("  ", " ").rstrip()
    name = candidate["name"]
    # MGNREGA: the stem may already hold the candidate's FULL name when the
    # extractor truncated the mention ("LOWER NALBARI" out of "LOWER NALBARI -
    # I"). Substituting then doubled the qualifier ("LOWER NALBARI - I - I"), the
    # resumed text no longer began with the paused question, the router did not
    # treat it as a chip resume, and the village was lost (all-villages QA
    # 2026-09-26).
    if _village_scheme(schemes) and re.search(
            rf"(?<![A-Za-z0-9]){re.escape(str(name))}(?![A-Za-z0-9])", stem, re.IGNORECASE):
        return f"{stem}, {candidate['block']} block, {candidate['district']}"
    if raw_text and re.search(re.escape(raw_text), stem, re.IGNORECASE):
        pinned = re.sub(re.escape(raw_text), name, stem, count=1, flags=re.IGNORECASE)
    else:
        pinned = f"{stem} ({name})"
    return f"{pinned}, {candidate['block']} block, {candidate['district']}"


# ── "Is that a village, a block, or a constituency?" ────────────────────────
# Block / assembly-constituency / village names overlap massively in Meghalaya
# (see entity_resolver.collides_across_dimensions and the
# cross_dimension_collisions block in mgnrega_entity_resolver.yaml). A bare
# "Sohra" is a real assembly constituency (AC 28, East Khasi Hills); it is NOT
# a block and NOT a village, but "Sohrarim" IS a village, so the old flow —
# extractor tags the bare name "block" -> no such block -> fall back to
# resolve_village -> fuzzy-match Sohrarim -> answer — reported a specific
# village's 643 beneficiaries as though the user had asked about Sohra
# (reported 2026-09-15). Every level the name could mean is now offered as a
# one-tap chip, and nothing is filtered until the user picks one.
#
# The chip question appends an explicit level word, which the resolvers and the
# mention-extractor both already key on: the extractor's own prompt uses
# "constituency"/"AC"/"assembly" to tag assembly_constituency, and the
# block/village branches below check for a literal "block"/"village" word to
# skip their own disambiguation. So a resumed chip resolves straight through
# without re-triggering this pause.
_DIM_CHIP_WORD = {
    "district": "district",
    "block": "block",
    "assembly_constituency": "assembly constituency",
    "village": "village",
}
_DIM_CHIP_LABEL = {
    "district": "district",
    "block": "C&RD block",
    "assembly_constituency": "assembly constituency",
    "village": "village",
}


def _dimension_collision_clarification(question: str, name: str,
                                       dims: "dict[str, str]") -> "ClarificationNeeded":
    """Ask which ADMIN LEVEL a bare, level-ambiguous place name refers to.

    `dims` is {dimension: canonical display name} from
    entity_resolver.collides_across_dimensions. Each chip substitutes that
    canonical name for whatever the user typed, so a misspelling is corrected
    at the same time as the level is chosen ("malwai" -> "Mawlai"); without
    that substitution the resumed question carries the typo forward and fails
    block/constituency resolution all over again, silently landing back on the
    village. The village chip keeps the user's own text — village names are
    DB-backed and resolved by their own branch."""
    stem = question.strip().rstrip(" ?.")
    options = []
    for dim, canonical in dims.items():
        shown = canonical or name
        # Swap the typed name for the canonical one in the question itself.
        if canonical and canonical.lower() != str(name).lower():
            resumed = re.sub(re.escape(str(name)), canonical, stem, count=1,
                             flags=re.IGNORECASE)
        else:
            resumed = stem
        options.append({
            "label": f"The {shown} {_DIM_CHIP_LABEL[dim]}",
            "question": f"{resumed}, the {_DIM_CHIP_WORD[dim]}, not another area type",
        })
    labels = list(dims)
    listed = ", ".join(_DIM_CHIP_LABEL[d] for d in labels[:-1]) + \
        f" or {_DIM_CHIP_LABEL[labels[-1]]}"
    return ClarificationNeeded(
        f"“{name}” could refer to more than one kind of area in Meghalaya — the "
        f"{listed}. These cover different places and give different numbers, so "
        "please pick the one you mean.",
        options=options,
        rule="entity-ambiguous",
    )


# The level word a resumed collision chip (or the user, unprompted) put in the
# question — "..., the assembly constituency, not another area type". When
# present, the level is already settled and the collision gate must not fire
# again; it also tells the branches below which dimension to force.
_EXPLICIT_LEVEL_RE = {
    "assembly_constituency": re.compile(
        r"\b(assembly\s+constituenc\w*|constituenc\w*|\bAC\b|assembly|MLA)\b", re.IGNORECASE),
    "block": re.compile(r"\bblock\b", re.IGNORECASE),
    "village": re.compile(r"\bvillage\b", re.IGNORECASE),
    "district": re.compile(r"\bdistrict\b", re.IGNORECASE),
}


# Assembly constituency is recorded on exactly ONE fact directly:
# curated.fact_mgnrega_employment (surfaced as curated.v_employment, see
# data/schema/schema_for_developers.md). It does not exist on MGNREGA
# expenditure, nor on PMAY-G / Focus Plus / CM Elevate at all — the resolver
# YAML says so outright ("expenditure: null — this dimension does not exist in
# mgnrega_expenditure"). Offering "the X assembly constituency" as a chip for a
# question about expenditure or houses would therefore invite the user to pick
# a reading that can never be answered, so the chip is suppressed for those.
#
# FOCUS LEGACY IS THE EXCEPTION, and its own contract is explicit about it:
# v_focus_legacy exposes geography_key, so joining curated.dim_geography for
# ac_name/ac_number is a permitted dimension join on a declared FK
# (focuslegacy_schema_partitions.yaml semantic_rules.constituency_rule,
# status ANSWERABLE_ONLY_BY_AN_EXPLICIT_DIMENSION_JOIN; the worked SQL is
# sanctioned_patterns.constituency in the join-graph YAML, and
# schema_context's Focus Legacy block already ships it). The rule's own
# runtime_behavior says "Answer the question ... Do not silently refuse", so
# suppressing the AC reading for this scheme hid a level the data can answer:
# "How many Producer Groups are mapped to Amlarem?" offered only block and
# village, though Amlarem is also a constituency (reported 2026-09-23).
#
# The employment measures that DO carry it, per that same schema: person-days,
# households/persons employed, job cards, 100-days completions, women
# employment. Anything else on MGNREGA is expenditure-side.
_AC_CAPABLE_METRIC = re.compile(
    r"\bperson[\s-]?days?\b|\bjob\s?cards?\b|\bmuster\b|"
    r"\b100[\s-]?days?\b|\bhundred\s+days?\b|"
    r"\b(?:households?|persons?|people|women|men)\b[^?.!]{0,30}\b"
    r"(?:employ\w*|work\w*|receiv\w*)\b|"
    r"\bemploy\w*\b|\bbeneficiar\w*\b|\bworkers?\b",
    re.IGNORECASE,
)
# Metric words that are unambiguously expenditure-side — no AC column exists
# for these even within MGNREGA.
_AC_INCAPABLE_METRIC = re.compile(
    r"\bexpenditure\b|\bspend(?:ing)?\b|\bspent\b|\bwages?\b|\bwage\s+bill\b|"
    r"\bmaterial\s+cost\b|\bamount\b|\bcost\b|\butili[sz]ation\b|"
    r"\bhouses?\b|\bsanction\w*\b|\breleas\w*\b|\bdisburs\w*\b|"
    r"\binstal{1,2}ments?\b|\bapplications?\b",
    re.IGNORECASE,
)


# The schemes whose data can answer a constituency question at all. MGNREGA
# carries ac_name on its employment fact; Focus Legacy reaches it through the
# documented dim_geography join on geography_key. PMAY-G, Focus Plus and CM
# Elevate have no route to it and keep the unconditional refusal. CM Elevate
# Legacy reaches it the same way as Focus Legacy (geography_key -> dim_geography,
# a declared FK), and the join matches the source workbook's own
# mapped_constituency_name exactly (Mairang 52 = 52, verified 2026-09-25).
_AC_CAPABLE_SCHEMES = ("MGNREGA", "Focus Legacy", "CM Elevate Legacy")


# The tail a village-disambiguation chip writes: ", RERAPARA block, SOUTH WEST
# GARO HILLS" (see _village_chip_question). A year chip tapped afterwards appends
# its period (" across all financial years", " for FY 2022-23"), which must not
# hide the tail (Focus Legacy all-villages run 2026-09-29, KI-156).
_CHIP_TAIL_PARSE_RE = re.compile(
    r",\s*(?P<block>[A-Za-z][A-Za-z.'()\- ]*?)\s+block\s*,\s*(?P<district>[A-Za-z][A-Za-z.'\- ]*?)"
    r"(?:\s+(?:across|for|in|during|over)\s+(?:all\s+)?(?:the\s+)?(?:financial\s+years?|years?|fy)\b[^,?]*)?"
    r"\s*[?.]?\s*$", re.IGNORECASE)


# A village chip's ", X block, DISTRICT" tail with a year chip's phrase after it
# ("…, JIRANG block, RI BHOI across all financial years" / "… for FY 2022-23").
_YEAR_AFTER_CHIP_TAIL_RE = re.compile(
    r",\s*[A-Za-z][A-Za-z.'()\- ]*?\s+block\s*,\s*[A-Za-z][A-Za-z.'\- ]*?\s+"
    r"(?:across|for|in|during|over)\s+(?:all\s+)?(?:the\s+)?(?:financial\s+years?|years?|fy)\b",
    re.IGNORECASE)


# "<preposition> NAME village" — the name the user typed, read from the text (see
# the explicit-level village step in resolve_entities). A name word is never a
# preposition, so "for East Garo Hills in Abima village" yields "Abima".
# "&", a spaced "-" and a spaced ")" too: "MAWKOHMIT & MAWKYNSAH", "SIEJLIEH -
# MAWIABAN", "Jong - U - Shen" and "RAMJONGGRE ( R )" are each ONE village —
# without the ")" the last was answered as its neighbour "RAMJONGGRE ( L )"
# (all-villages run 2026-09-29).
_VILLAGE_NAME_WORD = r"(?!(?:in|for|of|at|to|from|within|the|and)\b)[A-Za-z0-9()&\-][\w().'&\-]*"
_VILLAGE_PHRASE_RE = re.compile(
    rf"\b(?:in|for|of|at|to|from|within)\s+(?:the\s+)?({_VILLAGE_NAME_WORD}(?:\s+{_VILLAGE_NAME_WORD}){{0,6}})"
    r"\s+village\b", re.IGNORECASE)


async def _mgnrega_village_chip_pin(question: str, scheme: "str | None" = None) -> "dict | None":
    """The ONE village a resumed village-disambiguation chip names, read from the
    chip's own text: the block and district in its tail, and the village name in
    the stem (the chip substituted the candidate's canonical name there).

    Without this, the resume was re-resolved from scratch and looped (all-villages
    QA 2026-09-26): the extractor tagged CHIGITCHAKGRE as a *block*, whose
    fallback village lookup ignores the tail; and a chip that also carried
    ", the village, not another area type" cleared the block slot, leaving two
    KATALBARIs in the district — the same question came back every time."""
    m = _CHIP_TAIL_PARSE_RE.search(question or "")
    if not m or m.group("block").strip().lower() in ("none", ""):
        return None
    block, district = m.group("block").strip(), m.group("district").strip()
    stem = question[:m.start()]
    # A twin-village chip names its LGD code ("BOLDAMGRE (LGD 272933), SELSELLA
    # block, …" — see _village_code_tag): that code IS the choice.
    _coded = _CHIP_LGD_CODE_RE.search(stem)
    if _coded:
        try:
            rows = await fetch_rows(
                "SELECT village_code, lgd_village_name, lgd_block, lgd_district "
                "FROM curated.dim_geography WHERE village_code = $1 LIMIT 1", [int(_coded.group(1))])
        except Exception:  # noqa: BLE001 — fall back to the name match below
            rows = []
        if rows:
            return rows[0]
    try:
        rows = await fetch_rows(
            "SELECT DISTINCT village_code, lgd_village_name, lgd_block, lgd_district "
            "FROM curated.dim_geography WHERE UPPER(lgd_block) = UPPER($1) "
            "AND UPPER(lgd_district) = UPPER($2)", [block, district])
    except Exception:  # noqa: BLE001 — fall back to normal resolution
        return None
    hits = [r for r in rows if r.get("lgd_village_name") and re.search(
        rf"(?<![A-Za-z0-9]){re.escape(str(r['lgd_village_name']))}(?![A-Za-z0-9])", stem, re.IGNORECASE)]
    if not hits:
        return None
    longest = max(len(str(r["lgd_village_name"])) for r in hits)
    hits = [r for r in hits if len(str(r["lgd_village_name"])) == longest]
    # Two dim rows with the same name in the same block, only one of which the
    # asking scheme holds: "Bolbokgre, DEMDEMA block" is 272918 AND 273107 in
    # dim_geography, CM Elevate has 273107 only, and the chip answered 0 for
    # 272918 (CM Elevate all-villages run 2026-09-28). Keep the scheme's own.
    if scheme in _VILLAGE_NARROW_SCHEMES and len({r["village_code"] for r in hits}) > 1:
        try:
            _own = {c["village_code"] for v in (await _scheme_village_names(scheme)).values() for c in v}
        except Exception:  # noqa: BLE001
            _own = set()
        hits = [r for r in hits if r["village_code"] in _own] or hits
    if scheme == "CM Elevate Legacy" and len({r["village_code"] for r in hits}) > 1:
        try:
            _own = {r["village_code"] for r in await fetch_rows(
                "SELECT DISTINCT village_code FROM curated.v_cm_elevate_disbursement "
                "WHERE village_code = ANY($1::int[])", [[int(r["village_code"]) for r in hits]])}
        except Exception:  # noqa: BLE001
            _own = set()
        hits = [r for r in hits if r["village_code"] in _own] or hits
    if len({r["village_code"] for r in hits}) == 1:
        return hits[0]
    # Two dim rows with the same name in the same block: resolve_village collapses them.
    rv = await resolve_village(str(hits[0]["lgd_village_name"]), district=district, block=block)
    if rv.status == "resolved":
        return {"village_code": rv.canonical, "lgd_village_name": hits[0]["lgd_village_name"],
                "lgd_block": hits[0]["lgd_block"], "lgd_district": hits[0]["lgd_district"]}
    return None


# The single-scheme views whose twin-village chips are ranked / expanded by where
# the scheme's own records sit (see the two helpers below).
_TWIN_RANK_VIEWS = {"Focus Legacy": "curated.v_focus_legacy",
                    "CM Elevate Legacy": "curated.v_cm_elevate_disbursement"}


async def _fl_rank_villages(schemes: "list[str] | None", cands: list[dict]) -> list[dict]:
    """Focus Legacy: same-named villages that hold Focus Legacy groups first. The
    chips show 5 of the candidates drawn from the whole geography registry, and
    the one village with data could be left off ("MAWLONG": the Mawpat-block one
    was the 6th of 9; all-villages run 2026-09-29, KI-160). The rest are kept —
    a village with no groups is a real question with a real zero answer.
    CM Elevate Legacy joined 2026-09-29 (all-villages run): "Nongthymmai" matched
    10 registry villages and the one holding its records (JIRANG) was not among
    the chips shown."""
    view = _TWIN_RANK_VIEWS.get(schemes[0]) if schemes and len(schemes) == 1 else None
    if not view or len(cands or []) < 2:
        return cands
    codes = [int(c["village_code"]) for c in cands if str(c.get("village_code") or "").isdigit()]
    try:
        have = {r["village_code"] for r in await fetch_rows(
            f"SELECT DISTINCT village_code FROM {view} WHERE village_code = ANY($1::int[])", [codes])}
    except Exception:  # noqa: BLE001 — keep the registry order
        return cands
    return sorted(cands, key=lambda c: 0 if str(c.get("village_code")).isdigit()
                  and int(c["village_code"]) in have else 1)


async def _fl_expand_twins(schemes: "list[str] | None", cands: list[dict]) -> list[dict]:
    """Focus Legacy: add back a candidate's same-name twin in the SAME block when
    the twin holds Focus Legacy groups. resolve_village keeps one of each twin
    pair by all-scheme data volume, and for DEWSAW (Mairang) it kept the twin
    with no Focus Legacy data (all-villages run 2026-09-29, KI-163). CM Elevate
    Legacy uses the same rule against its own view."""
    view = _TWIN_RANK_VIEWS.get(schemes[0]) if schemes and len(schemes) == 1 else None
    if not view or not cands:
        return cands
    codes = [int(c["village_code"]) for c in cands if str(c.get("village_code") or "").isdigit()]
    try:
        extra = await fetch_rows(
            "SELECT DISTINCT g2.village_code, g2.lgd_village_name AS name, g2.lgd_block AS block, "
            "g2.lgd_district AS district FROM curated.dim_geography g1 JOIN curated.dim_geography g2 "
            "ON UPPER(g2.lgd_village_name) = UPPER(g1.lgd_village_name) "
            "AND g2.lgd_block IS NOT DISTINCT FROM g1.lgd_block AND g2.lgd_district = g1.lgd_district "
            "AND g2.village_code <> g1.village_code "
            f"WHERE g1.village_code = ANY($1::int[]) AND EXISTS (SELECT 1 FROM {view} f "
            "WHERE f.village_code = g2.village_code)", [codes])
    except Exception:  # noqa: BLE001 — keep the resolver's candidates
        return cands
    have = {str(c.get("village_code")) for c in cands}
    return cands + [dict(e) for e in extra if str(e["village_code"]) not in have]


async def _resolve_village_for(schemes: "list[str] | None", text: str, **kw) -> "Resolved":
    """resolve_village, minus the "Unresolved / Not Yet Mapped" placeholder rows
    for MGNREGA. Those rows belong to other schemes' unmapped records; MGNREGA's
    facts hold none (0 in v_employment and v_expenditure), yet an alias pointed
    "BLOCK CAMPUS" at one, so a unique village drew a "which of these?" pause
    (all-villages QA 2026-09-26)."""
    r = await resolve_village(text, **kw)
    if r.status == "ambiguous":
        r.candidates = await _fl_rank_villages(schemes, await _fl_expand_twins(schemes, r.candidates))
    if _village_scheme(schemes) in _VILLAGE_NARROW_SCHEMES:
        r = await _focusplus_narrow_village(r, text, kw, _village_scheme(schemes))
    if _village_scheme(schemes) and r.status == "ambiguous":
        keep = [c for c in r.candidates if not str(c.get("name", "")).strip().lower().startswith("unresolved")]
        # A village's OWN name beats an alias hit: an alias row maps the text
        # "WEST RANGASORA" to EAST RANGASORA too, so the exact name drew a
        # "which one?" pause (all-villages QA 2026-09-26).
        _own = [c for c in keep if re.sub(r"\s+", " ", str(c.get("name", ""))).strip().lower()
                == re.sub(r"\s+", " ", str(text or "")).strip().lower()]
        if len({c["village_code"] for c in _own}) == 1:
            keep = _own[:1]
        if len(keep) == 1:
            return Resolved("resolved", "village", text, canonical=keep[0]["village_code"],
                            confidence=1.0, display=_place_title(str(keep[0]["name"])))
        if keep and len(keep) < len(r.candidates):
            r.candidates = keep
    if schemes == ["Focus Legacy"] and r.status == "resolved" and str(r.canonical or "").isdigit():
        # resolve_village merges same-name twins in ONE block by data volume
        # across all schemes. TIEHSAW and UMSAW (Nongstoin) are twins that BOTH
        # hold Focus Legacy groups, so the merge answered for the wrong one
        # (all-villages run 2026-09-29, KI-163): ask, with LGD-coded chips.
        try:
            twins = await fetch_rows(
                "SELECT g2.village_code, g2.lgd_village_name AS name, g2.lgd_block AS block, "
                "g2.lgd_district AS district FROM curated.dim_geography g1 JOIN curated.dim_geography g2 "
                "ON UPPER(g2.lgd_village_name) = UPPER(g1.lgd_village_name) "
                "AND g2.lgd_block IS NOT DISTINCT FROM g1.lgd_block AND g2.lgd_district = g1.lgd_district "
                "WHERE g1.village_code = $1 AND EXISTS (SELECT 1 FROM curated.v_focus_legacy f "
                "WHERE f.village_code = g2.village_code) ORDER BY g2.village_code", [int(r.canonical)])
        except Exception:  # noqa: BLE001 — keep the resolver's choice
            twins = []
        if len(twins) > 1:
            return Resolved("ambiguous", "village", text, candidates=[dict(t) for t in twins],
                            message=f"“{text}” corresponds to more than one village.")
    return r


async def _focusplus_alias_district_collision(name: str, scheme: str = "Focus Plus") -> "dict[str, str]":
    """{level: canonical} when a bare Focus Plus place name reaches a DISTRICT
    only through an alias (a district HQ town) and is ALSO an exact Focus Plus
    village — else {}. "BAGHMARA" is South Garo Hills' HQ-town alias, the
    Baghmara block and village 273089 (37 beneficiaries). The shared collision
    check settles any district hit outright (right for district NAMES), so the
    village question was answered with the whole district's 8,612 (Focus Plus
    all-villages run 2026-09-27). Focus Plus has no constituency data, so no AC
    reading is offered."""
    d = resolve_dimension(name, scheme, "district")
    if d.status != "resolved" or not d.canonical:
        return {}
    if re.sub(r"\s+", " ", str(name)).strip().upper() == str(d.canonical).strip().upper():
        return {}          # the district's own name — never level-ambiguous
    try:
        names = await _scheme_village_names(scheme)
    except Exception:  # noqa: BLE001
        return {}
    if re.sub(r"\s+", " ", str(name)).strip().upper() not in names:
        return {}
    dims = {"district": d.display or str(d.canonical).title()}
    b = resolve_dimension(name, scheme, "block")
    if b.status == "resolved" and str(b.canonical).strip().upper() == str(name).strip().upper():
        dims["block"] = b.display or str(b.canonical)
    dims["village"] = ""
    return dims


async def _focusplus_narrow_village(r: "Resolved", text: str, kw: dict,
                                    scheme: str = "Focus Plus") -> "Resolved":
    """Prefer the villages the asking scheme (Focus Plus or PMAY-G) actually holds.
    resolve_village reads the statewide dim_geography, so a name shared with
    villages that have no record in the scheme drew a "which of these?" pause, or
    resolved to a code with no rows (Salpara - Ward No.9 → 277769; Focus Plus
    all-villages run 2026-09-27). PMAY-G hit the same on 2026-09-28: "Bhangarpar"
    (unique in PMAY-G) paused against a fuzzy ANGARIPARA, "Bholarbhita (w)" went to
    272775 instead of 272776. An exact name of the scheme, narrowed by any block /
    district in `kw`, wins."""
    try:
        names = await _scheme_village_names(scheme)
    except Exception:  # noqa: BLE001 — without the catalogue, keep the generic answer
        return r
    key = re.sub(r"\s+", " ", str(text or "")).strip().upper()
    hits = names.get(key) or []
    for k in ("district", "block"):
        want = kw.get(k)
        if want and hits:
            narrowed = [h for h in hits if str(h.get(k) or "").upper() == str(want).upper()]
            hits = narrowed or hits
    codes = {h["village_code"] for h in hits}
    if len(codes) == 1:
        return Resolved("resolved", "village", text, canonical=hits[0]["village_code"],
                        confidence=1.0, display=_place_title(str(hits[0]["name"])))
    if len(codes) > 1:
        return Resolved("ambiguous", "village", text, candidates=hits)
    if r.status == "ambiguous":
        fp = {c["village_code"] for v in names.values() for c in v}
        keep = [c for c in r.candidates if c.get("village_code") in fp]
        if len(keep) == 1:
            return Resolved("resolved", "village", text, canonical=keep[0]["village_code"],
                            confidence=1.0, display=_place_title(str(keep[0]["name"])))
        if keep:
            r.candidates = keep
    return r


_MGNREGA_VILLAGE_NAMES: "dict[str, list[dict]] | None" = None
_FOCUSPLUS_VILLAGE_NAMES: "dict[str, list[dict]] | None" = None

# Schemes whose facts are village-grained and carry village_code on the row.
# The village-resolution guards below were built for MGNREGA in the 2026-09-26
# all-villages QA; the Focus Plus all-villages run (2026-09-27) failed on every
# one of the same shapes (village read as a block, chip tail read as a block
# question, a block literal beside the village_code, a wrong code), so they
# apply to both.
# PMAY-G joined on 2026-09-28 (PMAY-G all-villages run): v_pmay is house-grained
# with village_code on every row, and failed the same shapes (exact name paused
# against a fuzzy look-alike, chip tail answered as the block, wrong code).
# CM Elevate joined on 2026-09-28 (CM Elevate all-villages run, KI-106): v_cm_elevate
# is application-grained with village_code on every row, and its smoke run failed
# the same shapes — a block total reported for the village, the village name put in
# the district slot, a neighbour's code (LENMAWTAP vs LENMAWTAP A), a chip that
# re-asked, "UPPER KAMARI (GARO)" read as the Garo Hills range.
_VILLAGE_FACT_SCHEMES = ("MGNREGA", "Focus Plus", "PMAY-G", "CM Elevate")
# the single-scheme members that share the Focus Plus narrowing / pinning guards
# Focus Legacy joined 2026-10-02 (all-villages run, bare village names): a name
# typed without the word "village" was never scanned — "producer groups in
# BOLDAMGRE" / "BALACHANDA I" were answered with Selsella block's 399, BATABARI
# (village AND block) silently as the block, KASHARIPARA with a stray district
# filter. Its questions passed on 2026-09-29 only because they said "village".
# CM Elevate Legacy joined the same night, for the same bare-name failures
# (AMLARI MODEL / NAPAKGRE written as scheme_name, "(B)" qualifiers lost, MANIPUR
# refused as the state, NONGSTOIN village vs block).
_VILLAGE_NARROW_SCHEMES = ("Focus Plus", "PMAY-G", "CM Elevate", "Focus Legacy", "CM Elevate Legacy")


def _village_scheme(schemes: "list[str] | None") -> "str | None":
    """The village-grained scheme the guards run for, or None. MGNREGA keeps the
    exact old gate (`schemes[0] == "MGNREGA"`, any list length); Focus Plus,
    PMAY-G and CM Elevate only when they are the one scheme asked about."""
    if not schemes:
        return None
    if schemes[0] == "MGNREGA":
        return "MGNREGA"
    if len(schemes) == 1 and schemes[0] in _VILLAGE_NARROW_SCHEMES:
        return schemes[0]
    return None


async def _focusplus_village_names() -> "dict[str, list[dict]]":
    """UPPER(name) -> villages, for every village / ward in v_focus_plus. Loaded
    once per process (≈3,500 names). Placeholder "Unresolved" rows excluded."""
    global _FOCUSPLUS_VILLAGE_NAMES
    if _FOCUSPLUS_VILLAGE_NAMES is None:
        rows = await fetch_rows(
            "SELECT DISTINCT village_code, lgd_village_name, UPPER(lgd_block) AS block, "
            "UPPER(lgd_district) AS district FROM curated.v_focus_plus "
            "WHERE village_code IS NOT NULL AND entity_type <> 'Unresolved'", [])
        out: dict[str, list[dict]] = {}
        for r in rows:
            n = re.sub(r"\s+", " ", str(r["lgd_village_name"] or "")).strip()
            if n and not n.lower().startswith("unresolved"):
                out.setdefault(n.upper(), []).append(
                    {"village_code": r["village_code"], "name": n, "block": r["block"], "district": r["district"]})
        _FOCUSPLUS_VILLAGE_NAMES = out
    return _FOCUSPLUS_VILLAGE_NAMES


_FOCUSPLUS_RAW_BLOCKS: "set[str] | None" = None


async def _focusplus_raw_block_names() -> "set[str]":
    """UPPER(block_name_raw) of every Focus Plus row, loaded once per process."""
    global _FOCUSPLUS_RAW_BLOCKS
    if _FOCUSPLUS_RAW_BLOCKS is None:
        rows = await fetch_rows("SELECT DISTINCT UPPER(block_name_raw) AS b FROM curated.v_focus_plus "
                                "WHERE block_name_raw IS NOT NULL", [])
        _FOCUSPLUS_RAW_BLOCKS = {str(r["b"]) for r in rows if r["b"]}
    return _FOCUSPLUS_RAW_BLOCKS


_FP_RAW_BLOCK_EQ_RE = re.compile(
    r"(?:UPPER\s*\(\s*)?(?:\w+\.)?block_name_raw(?:\s*\))?\s*(?:=|ILIKE)\s*(?:UPPER\s*\(\s*)?'((?:[^']|'')*)'(?:\s*\))?",
    re.IGNORECASE)


async def _focusplus_lgd_only_block(schemes: list[str], sql: str) -> str:
    """Focus Plus counts a block on block_name_raw (lgd_block is NULL on 15% of
    rows — prompt_builder._entities_block). But the source file predates ten
    LGD blocks (ADOKGRE, BATABARI, SIJU, MAWLAI, SHALLANG, RAMBRAI, PURAKHASIA,
    RI MULIANG, MAIRANG-TOWN COMMITTEE, WILLIAM NAGAR-MUNICIPAL BOARD — measured
    2026-10-02): no row carries those names, so "beneficiaries in Adokgre block"
    filtered block_name_raw = 'ADOKGRE', matched nothing and was answered "0 /
    the data doesn't cover it" — Adokgre has 754 (all-blocks run 2026-10-02).
    A block that is not a raw block name is filtered on lgd_block, where those
    villages were mapped."""
    if schemes != ["Focus Plus"] or not sql or "block_name_raw" not in sql.lower():
        return sql
    try:
        raw = await _focusplus_raw_block_names()
    except Exception:  # noqa: BLE001 — the lookup is an aid, never a reason to fail
        return sql
    if not raw:
        return sql

    def _swap(m: "re.Match[str]") -> str:
        name = m.group(1).replace("''", "'").strip().upper()
        if not name or name in raw:
            return m.group(0)
        logger.info("Focus Plus: block %r is not a block_name_raw value — filtered on lgd_block", name)
        return "lgd_block = '" + name.replace("'", "''") + "'"
    return _FP_RAW_BLOCK_EQ_RE.sub(_swap, sql)


_PMAY_VILLAGE_NAMES: "dict[str, list[dict]] | None" = None


async def _pmay_village_names() -> "dict[str, list[dict]]":
    """UPPER(name) -> villages, for every village in v_pmay (≈4,900 names, 5,121
    codes). Loaded once per process. Placeholder rows are kept: a village whose
    only record is a zero-sanction placeholder (Rtiang Sanphew) is still a PMAY-G
    village, and its answer is a stated zero, not another village."""
    global _PMAY_VILLAGE_NAMES
    if _PMAY_VILLAGE_NAMES is None:
        rows = await fetch_rows(
            "SELECT DISTINCT village_code, lgd_village_name, UPPER(lgd_block) AS block, "
            "UPPER(lgd_district) AS district FROM curated.v_pmay WHERE village_code IS NOT NULL", [])
        out: dict[str, list[dict]] = {}
        for r in rows:
            n = re.sub(r"\s+", " ", str(r["lgd_village_name"] or "")).strip()
            if n and not n.lower().startswith("unresolved"):
                out.setdefault(n.upper(), []).append(
                    {"village_code": r["village_code"], "name": n, "block": r["block"], "district": r["district"]})
        _PMAY_VILLAGE_NAMES = out
    return _PMAY_VILLAGE_NAMES


async def _pmay_village_beside_block(question: str, resolved: dict,
                                     scheme: str = "PMAY-G") -> "dict | None":
    """The ONE PMAY-G village a question names beside its own block / district —
    "…still to be released in NONGSOHRAM across all financial years, RI MULIANG
    block, WEST KHASI HILLS" resolved to the block (the word "block" sets the level)
    and answered with Ri Muliang's ₹33,84,000 instead of the village's figure
    (user report 2026-09-28). A match must be an exact PMAY-G village name found
    OUTSIDE the block / district mention (the block mention is "<block> block" when
    written so; "RONGJENG" inside "DAMBO RONGJENG block" is not a village) and lie in
    that block (and district). So "Umsning block" and "BATABARI block, WEST GARO
    HILLS" (the 2026-09-18 report: one mention, with "block") stay block questions,
    while "in DEMDEMA …, DEMDEMA block" (the name twice) and "RONGRAM BAZAR, RONGRAM
    block" are the village."""
    try:
        names = await _scheme_village_names(scheme)
    except Exception:  # noqa: BLE001
        return None
    blk = str(resolved.get("block") or "").strip().upper()
    dist = str(resolved.get("district") or "").strip().upper()
    q = re.sub(r"\s+", " ", _CHIP_LGD_CODE_RE.sub(" ", question or "").upper())
    # The block mention is "<block> block" when the text says so; a bare repeat of
    # the same name before it is then the village ("in DEMDEMA …, DEMDEMA block").
    area_spans = []
    if blk:
        _bs = [m.span() for m in re.finditer(rf"(?<![A-Z0-9]){re.escape(blk)}(?=\s+BLOCK\b)", q)]
        area_spans += _bs or [m.span() for m in re.finditer(rf"(?<![A-Z0-9]){re.escape(blk)}(?![A-Z0-9])", q)]
    if dist:
        area_spans += [m.span() for m in re.finditer(rf"(?<![A-Z0-9]){re.escape(dist)}(?![A-Z0-9])", q)]
    best = None
    for n, cands in names.items():
        # A village named like the very block / district it is asked beside is
        # ambiguous (the 2026-09-18 BATABARI rule); named like ANOTHER block
        # ("MAWLAI …, SHALLANG block") it can only be the village.
        if len(n) < 3 or n not in q or (best and len(n) <= len(best[0])):
            continue
        # a 3-letter name (BIR, KUT) only right after a place preposition, as in
        # _mgnrega_longest_village_in, so an ordinary word is never a village
        pat = (r"\b(?:IN|OF|AT|FOR|FROM) (" + re.escape(n) + r")(?![A-Z0-9])" if len(n) == 3
               else r"(?<![A-Z0-9])(" + re.escape(n) + r")(?![A-Z0-9])")
        spans = [m.span(1) for m in re.finditer(pat, q)]
        spans = [(x, y) for x, y in spans if not any(ax <= x and y <= ay for ax, ay in area_spans)]
        if not spans:
            continue
        keep = [c for c in cands if (not blk or str(c.get("block") or "").upper() == blk)
                and (not dist or str(c.get("district") or "").upper() == dist)]
        if keep:
            best = (n, keep)
    if not best or len({c["village_code"] for c in best[1]}) != 1:
        return None
    return best[1][0]


_PMAY_COMPARE_CUE_RE = re.compile(r"\b(?:which\s+has\s+(?:more|less|fewer|higher|lower)|compar\w*|versus|vs\.?)\b",
                                  re.IGNORECASE)


async def _pmay_two_villages(question: str) -> "list[dict] | None":
    """The two PMAY-G villages an "A or B" / "compare A and B" question names, when
    both are exact PMAY-G village names with one code each and neither is a block
    or district name. "Which has more completed houses: Nongtalang Mission or Sohkha
    Mission?" put one name in the block slot and the model compared a village with
    lgd_block = 'SOHKHA MISSION' (PMAY-G scenario run 2026-09-28)."""
    try:
        names = await _pmay_village_names()
    except Exception:  # noqa: BLE001
        return None
    admin = {str(c).strip().upper() for d in ("block", "district") for c in canonical_names("PMAY-G", d)}
    q = re.sub(r"\s+", " ", (question or "").upper())
    # The user already chose the village reading of a name that is also a block
    # ("The LASKEIN village" chip: "…PECHUA or LASKEIN, the village, not the
    # block"). Without this LASKEIN stayed a block, the comparison became
    # village + block, and the SQL repairs ran out — "couldn't build a working
    # query", or PECHUA alone (PMAY-G officer cases run 2026-10-02, OFF-022).
    chose_village = bool(re.search(r",\s*THE VILLAGE, NOT (?:THE BLOCK|ANOTHER AREA TYPE)", q))
    q = re.sub(r",\s*THE VILLAGE, NOT (?:THE BLOCK|ANOTHER AREA TYPE)", "", q)
    taken: list[tuple[int, int]] = []
    found: list[dict] = []
    for n in sorted(names, key=len, reverse=True):
        if len(n) < 4 or n not in q or (n in admin and not chose_village):
            continue
        for m in re.finditer(r"(?<![A-Z0-9])" + re.escape(n) + r"(?![A-Z0-9])", q):
            if any(not (m.end() <= x or m.start() >= y) for x, y in taken):
                continue
            taken.append(m.span())
            cands = names[n]
            if len({c["village_code"] for c in cands}) != 1:
                return None            # a twin name: the normal village pause handles it
            found.append(cands[0])
            break
    uniq = {c["village_code"]: c for c in found}
    return list(uniq.values()) if len(uniq) == 2 else None


_CME_VILLAGE_NAMES: "dict[str, list[dict]] | None" = None


async def _cm_elevate_village_names() -> "dict[str, list[dict]]":
    """UPPER(name) -> villages, for every village / ward in v_cm_elevate (≈2,050
    names, 2,087 codes). Loaded once per process. Placeholder "Unresolved" rows
    excluded (51 applications with no village)."""
    global _CME_VILLAGE_NAMES
    if _CME_VILLAGE_NAMES is None:
        rows = await fetch_rows(
            "SELECT DISTINCT village_code, lgd_village_name, UPPER(lgd_block) AS block, "
            "UPPER(lgd_district) AS district FROM curated.v_cm_elevate "
            "WHERE village_code IS NOT NULL AND entity_type <> 'Unresolved'", [])
        out: dict[str, list[dict]] = {}
        for r in rows:
            n = re.sub(r"\s+", " ", str(r["lgd_village_name"] or "")).strip()
            if n and not n.lower().startswith("unresolved"):
                out.setdefault(n.upper(), []).append(
                    {"village_code": r["village_code"], "name": n, "block": r["block"], "district": r["district"]})
        _CME_VILLAGE_NAMES = out
    return _CME_VILLAGE_NAMES


_CME_BLOCKS: "list[str] | None" = None
# the urban local bodies the data writes as "<TOWN>-MUNICIPAL BOARD" / "-TOWN COMMITTEE"
_URBAN_BODY_RE = re.compile(r"[\s-]*(?:MUNICIPAL\s+BOARD|TOWN\s+COMMITTEE)\b", re.IGNORECASE)
_URBAN_WORD_RE = re.compile(r"\b(?:municipal(?:\s+board)?|town\s+committee|m\.?\s?b\.?|t\.?\s?c\.?)\b",
                            re.IGNORECASE)


_CML_BLOCKS: "list[str] | None" = None
_FPL_BLOCKS: "list[str] | None" = None


async def _cm_elevate_block_names(scheme: str = "CM Elevate") -> "list[str]":
    """Every lgd_block spelling v_cm_elevate actually stores (66), loaded once.
    CM Elevate Legacy's own list (59) comes from v_cm_elevate_disbursement: it
    stores "TURA MUNICIPAL BOARD" where the registry also carries "TURA MUNICIPAL
    BOARD-MUNICIPAL BOARD", and "Tura Municipal Board" was refused as outside
    Meghalaya (CM Elevate Legacy all-blocks run 2026-09-29)."""
    global _CME_BLOCKS, _CML_BLOCKS, _FPL_BLOCKS
    if scheme == "Focus Plus":
        # Focus Plus names a block by block_name_raw OR, for the ten LGD-only
        # blocks, lgd_block (_focusplus_lgd_only_block): both spellings count.
        # "Mairang Town Committee" fuzzy-snapped to the rural MAIRANG and was
        # answered with Mairang's 402 (all-blocks run 2026-10-02).
        if _FPL_BLOCKS is None:
            rows = await fetch_rows(
                "SELECT DISTINCT UPPER(lgd_block) AS b FROM curated.v_focus_plus WHERE lgd_block IS NOT NULL "
                "UNION SELECT DISTINCT UPPER(block_name_raw) FROM curated.v_focus_plus WHERE block_name_raw IS NOT NULL",
                [])
            _FPL_BLOCKS = sorted(str(r["b"]) for r in rows if r["b"])
        return _FPL_BLOCKS
    if scheme == "CM Elevate Legacy":
        if _CML_BLOCKS is None:
            rows = await fetch_rows("SELECT DISTINCT UPPER(lgd_block) AS b FROM curated.v_cm_elevate_disbursement "
                                    "WHERE lgd_block IS NOT NULL AND lgd_block <> ''", [])
            _CML_BLOCKS = sorted(str(r["b"]) for r in rows)
        return _CML_BLOCKS
    if _CME_BLOCKS is None:
        rows = await fetch_rows("SELECT DISTINCT UPPER(lgd_block) AS b FROM curated.v_cm_elevate "
                                "WHERE lgd_block IS NOT NULL AND lgd_block <> ''", [])
        _CME_BLOCKS = sorted(str(r["b"]) for r in rows)
    return _CME_BLOCKS


def _cme_block_base(name: str) -> str:
    """'TURA MUNICIPAL BOARD-MUNICIPAL BOARD' -> 'TURA'; 'RI-MULIANG' -> 'RIMULIANG'."""
    from app.entity_resolver import _squash, fold   # the resolver's own normalisation
    return _squash(fold(_URBAN_BODY_RE.sub("", str(name))))


def _cme_data_block(canon: str, question: str, data_blocks: "list[str]") -> "str | None":
    """The data's own lgd_block for a resolved block, or None to keep it.
    - "X Municipal Board / Town Committee / MB / TC" asked → the urban body X.
    - a resolved name the data does not store → the data spelling with the same
      letters (RI-MULIANG → RI MULIANG), else X's only urban body (JOWAI →
      JOWAI-MUNICIPAL BOARD; CM Elevate has no rural Jowai rows)."""
    want = str(canon).strip().upper()
    base = _cme_block_base(want)
    urban = [b for b in data_blocks if _URBAN_BODY_RE.search(b) and _cme_block_base(b) == base]
    asked_urban = bool(re.search(
        r"(?<![A-Za-z])" + r"[\s-]*".join(re.escape(w) for w in re.findall(r"[A-Za-z]+", base.lower()) or [base])
        + r"[\s-]*(?:municipal(?:\s+board)?|town\s+committee|m\.?\s?b\b|t\.?\s?c\b)", question or "", re.IGNORECASE)) \
        or bool(re.search(r"[\s-]*".join(re.findall(r"[A-Za-z]+", want)) or want, question or "", re.IGNORECASE)
                and _URBAN_WORD_RE.search(question or ""))
    if asked_urban and len(urban) == 1:
        return urban[0]
    if want in data_blocks:
        return None
    same = [b for b in data_blocks if not _URBAN_BODY_RE.search(b) and _cme_block_base(b) == base]
    if len(same) == 1:
        return same[0]
    return urban[0] if len(urban) == 1 else None


async def _cm_elevate_block_from_mention(mention: str, schemes: "list[str] | None") -> "str | None":
    """The stored CM Elevate block a place mention names, when nothing else
    resolved it: an urban body ("Jowai-municipal Board", "Nongpoh TC") or the
    data's exact block spelling. Used before a mention is refused as outside
    Meghalaya (KI-107). CM Elevate Legacy uses its own stored blocks."""
    if schemes not in (["CM Elevate"], ["CM Elevate Legacy"]) or not mention:
        return None
    try:
        data_blocks = await _cm_elevate_block_names(schemes[0])
    except Exception:  # noqa: BLE001
        return None
    m = re.sub(r"\s+", " ", str(mention)).strip()
    base = _cme_block_base(re.sub(r"(?i)\b(?:m\.?\s?b|t\.?\s?c)\.?$", "", m).rstrip(" -"))
    if _URBAN_WORD_RE.search(m):
        urban = [b for b in data_blocks if _URBAN_BODY_RE.search(b) and _cme_block_base(b) == base]
        if len(urban) == 1:
            return urban[0]
    exact = [b for b in data_blocks if _cme_block_base(b) == base and not _URBAN_BODY_RE.search(b)]
    return exact[0] if len(exact) == 1 else None


async def _cm_elevate_blocks_to_data(question: str, resolved: dict, display: dict,
                                     scheme: str = "CM Elevate") -> None:
    """KI-107 (CM Elevate all-blocks run, 2026-09-28): the CM Elevate block
    catalogue is older than its data — 21 of the 66 stored blocks are missing,
    including all 9 urban bodies ("BAGHMARA-MUNICIPAL BOARD"), and 8 catalogue
    names are spelled differently or absent in the data. "Baghmara Municipal
    Board" fuzzy-snapped to the rural BAGHMARA (56 applications instead of 18);
    "Jowai Municipal Board" to JOWAI, which has no rows ("no records"); a
    resolved RI-MULIANG never matched the stored "RI MULIANG". Rewrite the
    resolved block(s) to the spelling the data stores."""
    try:
        data_blocks = await _cm_elevate_block_names(scheme)
    except Exception:  # noqa: BLE001 — without the live list, keep the resolver's answer
        return
    if isinstance(resolved.get("block"), str):
        new = _cme_data_block(resolved["block"], question, data_blocks)
        if new and new != resolved["block"]:
            logger.info("CM Elevate block %r -> stored %r", resolved["block"], new)
            resolved["block"] = new
            display["block"] = _place_title(new)
    if isinstance(resolved.get("block_list"), list):
        resolved["block_list"] = [_cme_data_block(b, question, data_blocks) or b for b in resolved["block_list"]]


_CML_VILLAGE_NAMES: "dict[str, list[dict]] | None" = None


async def _cm_legacy_village_names() -> "dict[str, list[dict]]":
    """UPPER(name) -> villages for every village in v_cm_elevate_disbursement."""
    global _CML_VILLAGE_NAMES
    if _CML_VILLAGE_NAMES is None:
        rows = await fetch_rows(
            "SELECT DISTINCT village_code, lgd_village_name, UPPER(lgd_block) AS block, "
            "UPPER(lgd_district) AS district FROM curated.v_cm_elevate_disbursement "
            "WHERE village_code IS NOT NULL AND village_code > 0 AND entity_type <> 'Unresolved'", [])
        out: dict[str, list[dict]] = {}
        for r in rows:
            n = re.sub(r"\s+", " ", str(r["lgd_village_name"] or "")).strip()
            if n and not n.lower().startswith("unresolved"):
                out.setdefault(n.upper(), []).append(
                    {"village_code": r["village_code"], "name": n, "block": r["block"], "district": r["district"]})
        _CML_VILLAGE_NAMES = out
    return _CML_VILLAGE_NAMES


_FL_VILLAGE_NAMES: "dict[str, list[dict]] | None" = None


async def _focus_legacy_village_names() -> "dict[str, list[dict]]":
    """UPPER(name) -> villages for every village in v_focus_legacy, loaded once.
    Placeholder "Unresolved" rows excluded (records with no village)."""
    global _FL_VILLAGE_NAMES
    if _FL_VILLAGE_NAMES is None:
        rows = await fetch_rows(
            "SELECT DISTINCT village_code, lgd_village_name, UPPER(lgd_block) AS block, "
            "UPPER(lgd_district) AS district FROM curated.v_focus_legacy "
            "WHERE village_code IS NOT NULL AND village_code > 0 AND entity_type <> 'Unresolved'", [])
        out: dict[str, list[dict]] = {}
        for r in rows:
            n = re.sub(r"\s+", " ", str(r["lgd_village_name"] or "")).strip()
            if n and not n.lower().startswith("unresolved"):
                out.setdefault(n.upper(), []).append(
                    {"village_code": r["village_code"], "name": n, "block": r["block"], "district": r["district"]})
        _FL_VILLAGE_NAMES = out
    return _FL_VILLAGE_NAMES


async def _scheme_village_names(scheme: str) -> "dict[str, list[dict]]":
    if scheme == "CM Elevate":
        return await _cm_elevate_village_names()
    if scheme == "Focus Legacy":
        return await _focus_legacy_village_names()
    if scheme == "CM Elevate Legacy":
        return await _cm_legacy_village_names()
    if scheme == "PMAY-G":
        return await _pmay_village_names()
    return await (_focusplus_village_names() if scheme == "Focus Plus" else _mgnrega_village_names())


async def _mgnrega_village_names() -> "dict[str, list[dict]]":
    """UPPER(name) -> villages, for every village in MGNREGA's two facts. Loaded
    once per process (≈6,500 names)."""
    global _MGNREGA_VILLAGE_NAMES
    if _MGNREGA_VILLAGE_NAMES is None:
        rows = await fetch_rows(
            "SELECT DISTINCT village_code, lgd_village_name, UPPER(lgd_block) AS block, "
            "UPPER(lgd_district) AS district FROM curated.v_employment "
            "UNION SELECT DISTINCT village_code, lgd_village_name, UPPER(lgd_block), "
            "UPPER(lgd_district) FROM curated.v_expenditure", [])
        out: dict[str, list[dict]] = {}
        for r in rows:
            n = re.sub(r"\s+", " ", str(r["lgd_village_name"] or "")).strip()
            if n and not n.lower().startswith("unresolved"):
                out.setdefault(n.upper(), []).append(
                    {"village_code": r["village_code"], "name": n, "block": r["block"], "district": r["district"]})
        _MGNREGA_VILLAGE_NAMES = out
    return _MGNREGA_VILLAGE_NAMES


async def _mgnrega_longest_village_in(question: str, scheme: str = "MGNREGA"
                                      ) -> "tuple[str, list[dict]] | None":
    """The longest village name of `scheme` (MGNREGA or Focus Plus) that appears
    verbatim (whole words) in the question. Unlike _village_scan_candidates this
    does not split on "&", "INCL" or punctuation: "MAWKOHMIT & MAWKYNSAH",
    "UMTHLONG KYRSEN & KHARJANA" and "LAWRIAT INCL DOMMUSUR" are single villages
    that the extractor split into several places (all-villages QA 2026-09-26);
    Focus Plus wards read "Chobagok - Ward No.3" (2026-09-27)."""
    try:
        names = await (_mgnrega_village_names() if scheme == "MGNREGA" else _scheme_village_names(scheme))
    except Exception:  # noqa: BLE001
        return None
    qu = re.sub(r"\s+", " ", (question or "").upper())
    best = None
    for n, cands in names.items():
        if len(n) < 3 or n not in qu or (best and len(n) <= len(best[0])):
            continue
        # A 3-letter name (ATS, BIR, KUT, MOT — the only ones) counts only right
        # after a place preposition, so an ordinary word can never be read as a
        # village; "person-days in MOT" found no place at all and answered with a
        # null (all-villages QA 2026-09-26).
        pat = (rf"\b(?:IN|OF|AT|FOR|FROM)\s+{re.escape(n)}(?![A-Z0-9])" if len(n) == 3
               else rf"(?<![A-Z0-9]){re.escape(n)}(?![A-Z0-9])")
        if re.search(pat, qu):
            best = (cands[0]["name"], cands)
    return best


async def _mask_scheme_words_in_village_name(question: str) -> str:
    """The question with an exact MGNREGA village name masked, when that name on
    its own reads as scheme vocabulary ("UMRAN DAIRY" -> CM Elevate). Otherwise
    the question unchanged. Used for scheme detection only."""
    try:
        found = await _mgnrega_longest_village_in(question)
    except Exception:  # noqa: BLE001
        return question
    if not found:
        return question
    name = found[0]
    if not (_infer_scheme_from_terms(name) or any(rx.search(name) for rx in _SCHEME_NAME_PATTERN.values())):
        return question
    masked = re.sub(re.escape(name), "the village", question, count=1, flags=re.IGNORECASE)
    logger.info("scheme detection: village name %r masked (its words read as scheme vocabulary)", name)
    return masked


async def _mgnrega_village_not_out_of_area(question: str, hit: "dict | None") -> bool:
    """True when an out-of-area edge hit is really a Meghalaya VILLAGE name.

    Three MGNREGA villages carry a place word the edge layer refuses as outside
    Meghalaya: COAL INDIA COLONY ("India"), BURMA and MANIPUR (all-villages QA
    2026-09-26 — "person-days in COAL INDIA COLONY" got "I don't hold all-India
    figures"). A longer name ("COAL INDIA COLONY"), or a question that says
    "village", is the village. A bare "MANIPUR" / "BURMA" is genuinely both, so
    the user is asked. Only for an MGNREGA or unscoped question."""
    if not hit or hit.get("type") != "off_topic":
        return False
    # Focus Plus too: its village BURMA (278981) got the same refusal in the
    # Focus Plus all-villages run (2026-09-27); PMAY-G's MANIPUR (277554) and
    # BURMA (278981) in the PMAY-G all-villages run (2026-09-28). CM Elevate's
    # MANIPUR (277554, Umling block, 29 applications) in its all-villages run the
    # same day (KI-118).
    _sch = _named_or_inferred_schemes(question)
    # Focus Legacy / CM Elevate Legacy too since 2026-10-02 (CM Elevate Legacy's village
    # MANIPUR was refused as the state in the all-villages re-run)
    if _sch not in ([], ["MGNREGA"], ["Focus Plus"], ["PMAY-G"], ["CM Elevate"], ["Focus Legacy"],
                    ["CM Elevate Legacy"]):
        return False
    ql = (question or "").lower()
    m = edge._OUT_OF_AREA.search(ql) or edge._FOREIGN_PLACE.search(ql)
    if not m or re.search(r"\boutside\s+meghalaya\b|\bthe\s+(?:state|country)\b", ql):
        return False
    try:
        scanned = await _scan_village_in_question(question, _sch[0] if _sch else "MGNREGA")
    except Exception:  # noqa: BLE001
        return False
    # A comparison of two villages, one of them BURMA: "Which has more completed
    # PMAY-G houses: JEWILGRE or BURMA?" was refused as a question about Burma
    # (PMAY-G officer cases run 2026-10-02) — the scan returns ONE village
    # (JEWILGRE) and the refused word was not part of it. A refused word that is
    # itself a village name, beside another village, is the village.
    word = m.group(0).strip().upper()
    if not scanned or word not in scanned[0].upper():
        try:
            _names = await _scheme_village_names(_sch[0] if _sch else "MGNREGA")
        except Exception:  # noqa: BLE001
            _names = {}
        # beside another village name of the same scheme: a village comparison
        _others = [n for n in _names if n != word and len(n) >= 4
                   and re.search(rf"(?<![A-Z0-9]){re.escape(n)}(?![A-Z0-9])", (question or "").upper())]
        if word in _names and _others:
            return True
    if not scanned or m.group(0).strip() not in scanned[0].lower():
        return False
    name, hits = scanned
    if len(name.strip()) > len(m.group(0).strip()) or re.search(r"\bvillage\b", ql):
        return True
    c = hits[0]
    stem = (question or "").strip().rstrip(" ?.")
    raise ClarificationNeeded(
        f"“{name}” is a village in Meghalaya ({c.get('block')} block, {c.get('district')}) and also "
        "a place outside Meghalaya. Which did you mean?",
        options=[{"label": f"{name} village — {c.get('block')} block, {c.get('district')}",
                  "question": f"{stem}, the village, not another area type"},
                 {"label": f"{name.title()} outside Meghalaya", "question": f"{stem} (the state or country, not the village)"}],
        # Not a scope-pause rule on purpose: each chip must run as a fresh
        # question, so the "outside Meghalaya" one still meets the edge refusal
        # (a resumed pause skips the edge layer).
        rule="village-or-outside-place")


async def _mgnrega_admin_reading_of_village_mention(name: str, scheme: str = "MGNREGA") -> "dict[str, str]":
    """{dimension: canonical} for the MGNREGA block and/or assembly constituency
    that `name` spells EXACTLY, when `name` is not also an exact village name;
    else {}. Used to re-read a village mention that is really a block or a
    constituency (see resolve_entities)."""
    want = re.sub(r"\s+", " ", str(name or "")).strip().upper()
    if not want:
        return {}
    hits: dict[str, str] = {}
    for dim in ("block", "assembly_constituency"):
        for canon in canonical_names(scheme, dim):
            if re.sub(r"\s+", " ", str(canon)).strip().upper() == want:
                hits[dim] = canon
                break
    if not hits:
        return {}
    try:
        if (await village_names_exact([str(name).strip()])).get(str(name).strip().upper()):
            return {}          # a real village of this name: leave it to the village branch
    except Exception:  # noqa: BLE001 — without the DB check, do not re-read
        return {}
    return hits


def _mgnrega_ac_reading(name: str, schemes: "list[str] | None") -> "str | None":
    """The MGNREGA assembly-constituency name `name` spells EXACTLY (case and
    spacing aside), or None. Exact only — never fuzzy — because it is used to
    rescue a mention that would otherwise be refused as out of scope, and a
    fuzzy hit there would silently answer about a different place."""
    if not name or not schemes or schemes[0] != "MGNREGA":
        return None
    want = re.sub(r"\s+", " ", str(name)).strip().upper()
    for canon in canonical_names("MGNREGA", "assembly_constituency"):
        if re.sub(r"\s+", " ", str(canon)).strip().upper() == want:
            return canon
    return None


def _ac_dimension_available(question: str, schemes: list[str]) -> bool:
    """True when an assembly-constituency reading of a place name could
    actually be queried for THIS question. False suppresses the AC chip."""
    _live = list(schemes or [])
    if len(_live) != 1 or _live[0] not in _AC_CAPABLE_SCHEMES:
        return False
    if _live[0] == "CM Elevate Legacy":
        # Every measure on this view (records, sanctioned, subsidy, loan,
        # disbursement) hangs off the same geography_key, so the MGNREGA
        # employment-vs-expenditure metric test does not apply.
        return True
    q = question or ""
    # An explicit expenditure/housing metric rules it out even if an
    # employment-ish word also appears ("wage employment expenditure").
    if _AC_INCAPABLE_METRIC.search(q) and not _AC_CAPABLE_METRIC.search(q):
        return False
    if _AC_CAPABLE_METRIC.search(q):
        return True
    # No metric named at all ("figures for Sohra") — leave the reading open
    # rather than silently dropping a valid choice.
    return not _AC_INCAPABLE_METRIC.search(q)


# ── Step 2 of the hierarchy: narrowing inside a chosen constituency ─────────
# Once the user has said "I meant the constituency", they may still want only
# part of it. An AC is an electoral boundary rather than an administrative
# parent — 8 of the 56 straddle two districts — so the narrowing offered is
# built from what that constituency ACTUALLY contains in the data
# (entity_resolver.constituency_contents), never from a static hierarchy.
#
# The phrasing of each chip is what makes the next turn resolve cleanly: the
# district/block chips keep the constituency name AND add the area, so the
# question stays scoped to both; "the whole constituency" simply confirms.
_AC_SCOPED_RE = re.compile(r",\s*within\s+the\s+", re.IGNORECASE)
# The narrowing a resumed drill-down chip carries: ", within the <NAME>
# <district|block> only". Read deterministically rather than left to the LLM
# mention-extractor — the extractor has no reason to tag a second place name
# in a question that already names a constituency, and when it doesn't, the
# narrowing is silently lost and the answer covers the whole constituency
# again (the very thing the user just declined).
_AC_NARROW_RE = re.compile(
    r",\s*within\s+the\s+(?P<name>.+?)\s+(?P<level>district|block)\s+only\b",
    re.IGNORECASE,
)


def _ac_drilldown_clarification(question: str, ac_display: str,
                                contents: dict, scheme: "str | None" = None) -> "ClarificationNeeded | None":
    """Offer to narrow inside a just-chosen assembly constituency, or None when
    there is nothing meaningful to narrow to."""
    districts = contents.get("districts") or []
    blocks = contents.get("blocks") or []
    if not districts and not blocks:
        return None
    stem = question.strip().rstrip(" ?.")
    options = [{"label": f"The whole {ac_display} constituency",
                "question": f"{stem}, the whole constituency"}]
    # A constituency spanning two districts is the one case where the district
    # step is a real question rather than a formality.
    if len(districts) > 1:
        for d in districts:
            options.append({
                "label": f"Only the {str(d).title()} part",
                "question": f"{stem}, within the {d} district only",
            })
    for b in blocks[:8]:
        options.append({
            "label": f"{str(b).title()} block",
            "question": f"{stem}, within the {b} block only",
        })
    if len(options) < 2:
        return None
    where = (f"spans {len(districts)} districts and " if len(districts) > 1 else "covers ")
    return ClarificationNeeded(
        f"The {ac_display} assembly constituency {where}"
        f"{len(blocks)} C&RD block(s), with {contents.get('villages', 0)} villages in the "
        f"{AC_CONTENTS_SOURCE.get(scheme or 'MGNREGA', 'MGNREGA employment data')}. "
        "Do you want the whole constituency, or just part of it?",
        options=options,
        rule="ac-narrow-scope",
    )


# ── Deterministic village backstop ──────────────────────────────────────────
# scan_dimension() already backstops a district the LLM mention-extractor
# dropped. Villages had no equivalent, and the extractor drops them too —
# confirmed live 2026-09-15: "total beneficiaries in ASIMGRE for MGNREGA for
# East Khasi Hills" came back {"district": "East Khasi Hills"} with ASIMGRE
# missing entirely. With no village mention there is nothing to resolve, the
# ambiguity check never runs, and the filter silently disappears: the query
# counted the WHOLE district while the answer still said "for ASIMGRE".
#
# A village scan has to be much more careful than the district one. There are
# ~6,000 villages and resolve_village()'s trigram stage matches loosely enough
# that ordinary words — and fragments of district names like "East", "Garo",
# "Hills" — all hit something. So this scan is doubly constrained:
#   1. candidates come only from a place-preposition phrase ("in X", "for X",
#      "of X"), the grammar that actually introduces a place; and
#   2. a candidate must match a village name EXACTLY (village_names_exact),
#      never fuzzily.
# A name that is already a known district / block / constituency is skipped —
# those dimensions own it, and the admin-level collision gate handles the
# genuinely ambiguous ones.
# The name may carry a parenthesised suffix that is PART of it — Meghalaya has
# "NONGCHRAM (I)" and "NONGCHRAM (II)" as two distinct villages in the same
# block. Without the optional "(...)" tail the scan proposes a bare "NONGCHRAM",
# which matches no stored name exactly, so the backstop finds nothing and the
# village filter is silently lost (reported 2026-09-17).
# Candidate place phrases after a preposition. Deliberately tolerant on TWO
# axes, because both were observed dropping a real village:
#   CASE — users type lowercase ("in william nagar(mb) - ward no.4"). A
#     capital-first pattern skipped those entirely, so the backstop never ran
#     and the truncated extractor mention went unchallenged.
#   SUFFIX CHAIN — a name can carry MORE THAN ONE part-marker:
#     "William Nagar (MB) - Ward No.4" is a parenthesised marker AND a
#     hyphenated one. Matching only the first stops at "William Nagar (MB)",
#     which is ambiguous across 12 wards and loops the clarification forever
#     (reported 2026-09-17).
# Precision still comes from village_names_exact(): a candidate only counts
# if it matches a stored village name EXACTLY, so a loose phrase costs one
# lookup and nothing more.
_PLACE_PREP_RE = re.compile(
    r"\b(?:in|for|of|at|from|within|under)\s+"
    r"(?P<name>[A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,3}"
    # A hyphenated marker is at most TWO short tokens ("- A", "- Ward No.4") —
    # bounded so it cannot run on into the rest of the sentence ("- Ward No.4
    # for CM Elevate"), which would never match a stored name.
    r"(?:\s*\([A-Za-z0-9 .'-]{1,20}\)"
    r"|\s*-\s*[A-Za-z0-9][A-Za-z0-9.']{0,11}(?:\s+[A-Za-z0-9][A-Za-z0-9.']{0,11})?"
    r")"
    r"{0,2})",
)
# Words that open a phrase without naming a place; a candidate that is only
# these is never a village.
_NOT_A_PLACE_WORD = {
    "mgnrega", "mnrega", "nrega", "pmay", "pmayg", "awaas", "awas",
    "focus", "focus plus", "focusplus", "cm", "cm elevate", "cmelevate",
    "meghalaya", "fy", "financial", "financial year", "all", "each", "every",
    "the", "a", "an", "this", "that", "total", "district", "block", "village",
    "assembly", "constituency", "state", "india", "government", "scheme",
}


def _village_scan_candidates(question: str, scheme: str) -> list[str]:
    """Names in `question` that could be a village the extractor missed."""
    known: set[str] = set()
    for dim in ("district", "block", "assembly_constituency"):
        for n in canonical_names(scheme, dim):
            known.add(n.strip().upper())
    out: list[str] = []
    # Lookahead so matches can OVERLAP: finditer consumes what it matches, so a
    # phrase starting at an earlier preposition ("under goat farming scheme in")
    # would swallow the "in" that introduces the real place and the village
    # would never be scanned at all (reported 2026-09-17, lowercase "in william
    # nagar(mb) - ward no.4"). Every preposition now gets its own attempt.
    for m in re.finditer(rf"(?={_PLACE_PREP_RE.pattern})", question or "",
                         re.IGNORECASE if _PLACE_PREP_RE.flags & re.IGNORECASE else 0):
        raw = (m.group("name") or "").strip().rstrip(".,")
        if not raw:
            continue
        # Try the longest phrase first, then progressively shorter prefixes, so
        # "ASIMGRE for MGNREGA" still yields the bare "ASIMGRE".
        words = raw.split()
        for take in range(len(words), 0, -1):
            cand = " ".join(words[:take]).strip()
            low = cand.lower()
            if len(cand) < 4 or low in _NOT_A_PLACE_WORD:
                continue
            if cand.upper() in known:
                break        # a district/block/AC owns this name — not our job
            if cand not in out:
                out.append(cand)
    return out


async def _scan_village_in_question(question: str, scheme: str) -> "tuple[str, list[dict]] | None":
    """(name, candidate villages) for a village named in the question that the
    extractor dropped, or None. Exact matches only."""
    cands = _village_scan_candidates(question, scheme)
    if not cands:
        return None
    found = await village_names_exact(cands)
    if not found:
        return None
    # Preserve the order the names appear in the question.
    for c in cands:
        hits = found.get(c.upper())
        if hits:
            return c, hits
    return None


def _canonical_in_question(question: str, scheme: str, dimension: str) -> "str | None":
    """The catalogue name for `dimension` that appears verbatim in `question`,
    or None. Used when resuming an admin-level collision chip: the chip put the
    CANONICAL name into the question text, so reading it back from there is
    more reliable than trusting the LLM extractor, which may still be echoing
    the user's original typo. Longest match wins, so "North Tura" is not
    shadowed by "Tura"."""
    best: str | None = None
    # collision_canonical_names, not canonical_names: for block / assembly
    # constituency it falls back to another scheme's catalogue when the asking
    # scheme has none, which is what lets the admin-level gate see an AC
    # reading under CM Elevate (its catalogue has no AC dimension at all).
    for canon in collision_canonical_names(scheme, dimension):
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(canon)}(?![A-Za-z0-9])",
                     question or "", re.IGNORECASE):
            if best is None or len(canon) > len(best):
                best = canon
    return best


# A village-disambiguation chip appends its scope as ", <BLOCK> block,
# <DISTRICT>" (see _village_chip_question). Those words name the CONTAINING
# area, not the level the question is about — the question is about the
# village. Left in place they make _explicit_level_in report "block", which
# both suppresses the village backstop and lets the block slot win, so the chip
# resolves to block grain and the village is lost (confirmed 2026-09-15 on the
# ASIMGRE chips). Strip that trailing scope before detecting the level.
# Matches only the village-chip scope tail: a block name (never the word "the")
# followed by a district name, both in CAPS as _village_chip_question writes
# them, at the very end. The admin-level CHOICE chip (", the block, not another
# area type") does not match — it has "the" before "block" and trailing prose
# after the second comma.
_CHIP_SCOPE_SUFFIX_RE = re.compile(
    r",\s*[A-Z][A-Za-z.'\- ]*\s+block\s*,\s*[A-Z][A-Za-z.'\- ]*\s*$")
# The level phrase a disambiguation chip always carries. Its presence is what
# marks a question as a chip resume, so the scope tail above is only stripped
# then — never from an ordinary question that happens to end the same way.
_CHIP_LEVEL_PHRASE_RE = re.compile(
    r",\s*the\s+(?:village|block|district|assembly\s+constituency)\b", re.IGNORECASE)
# "each village", "by block", "per district", "village-wise", "all villages" —
# these name the GROUPING a breakdown is computed over, never the place the
# question is scoped to. Removed before level detection so the surviving level
# word (if any) is the one that actually names a place.
_BREAKDOWN_LEVEL_PHRASE_RE = re.compile(
    r"\b(?:by|per|each|every|all|across)\s+(?:the\s+)?"
    r"(?:village|block|district|assembly\s+constituenc\w*|constituenc\w*)s?\b|"
    r"\b(?:village|block|district)[\s-]?wise\b",
    re.IGNORECASE,
)


# "…, the block, not the village" — the level-choice chip the block/village
# collision pause offers. The NEGATED level names what the user ruled out, so it
# must not count as the stated level. Detection is finest-first, so the
# negated "village" won over the chosen "block": MAWKYRWAT resolved to village
# 277399 and the block question got the village's 273 households instead of the
# block's 11,590, or no answer at all (MGNREGA QA 2026-09-26, DATA-003/016/020/
# 029, KNOWN_ISSUES KI-041). Stripped for MGNREGA only (the user asked that
# other schemes stay unchanged).
_NEGATED_LEVEL_RE = re.compile(
    r"\bnot\s+(?:the|a|an|another)\s+"
    r"(?:village|block|district|assembly\s+constituenc\w*|constituenc\w*)s?\b",
    re.IGNORECASE)


def _explicit_level_in(question: str, schemes: "list[str] | None" = None) -> "str | None":
    """The admin level the question names outright, or None. Finest-first so a
    question naming two levels ("village in X district") reports the one being
    asked about rather than the containing area."""
    q = question or ""
    if schemes and ("MGNREGA" in schemes or _village_scheme(schemes)):
        q = _NEGATED_LEVEL_RE.sub(" ", q)
    # Only strip the chip's scope tail when the question ALSO carries a chip's
    # level phrase (", the village, not another area type"). A user-typed
    # question can end in the same ", <NAME> block, <DISTRICT>" shape —
    # "... , BATABARI block, WEST GARO HILLS" — and stripping that deleted the
    # word "block" the user had explicitly written, so the level read as
    # unstated and a same-named VILLAGE won instead (reported 2026-09-18: the
    # BATABARI block question resolved to village 272854, 1 row, against 55 in
    # the block).
    if _CHIP_LEVEL_PHRASE_RE.search(q):
        q = _CHIP_SCOPE_SUFFIX_RE.sub("", q)
    # A BREAKDOWN phrase names the grouping, not the place being asked about:
    # "in each village of Betasing block" asks for a per-village split OF THE
    # BLOCK. Reading "village" as the stated level there made the block branch
    # prefer a village reading and resolve a same-named village instead, so the
    # query filtered one village while grouping by village — 0 rows against a
    # real 6 (reported 2026-09-18). Drop the grouping words before detecting.
    q = _BREAKDOWN_LEVEL_PHRASE_RE.sub(" ", q)
    for dim in ("village", "assembly_constituency", "block", "district"):
        if _EXPLICIT_LEVEL_RE[dim].search(q):
            return dim
    return None


async def resolve_entities(question: str, schemes: list[str],
                            prior_resolved: "dict | None" = None,
                            village_hint: "str | None" = None) -> dict:
    """Resolve every extracted mention to a canonical DB value. Ambiguous ->
    raise ClarificationNeeded (ask, per the resolver's own hard rule — never
    guess). Not-found is not an error: it means the value genuinely is not in
    this data, which the response composer should say plainly, not silently
    drop the filter.

    `prior_resolved` (optional): the previous turn's resolved entities, passed
    only when this question is a rewritten follow-up. Used purely as a
    fallback — for any dimension (district/block/village_code/year_key) the
    CURRENT question names nothing for at all, carry the prior turn's value
    forward instead of leaving it unset. This covers a village/year the
    follow-up rewrite paraphrased away in its wording (the rewrite works from
    prev.question/answer TEXT, so an entity can silently vanish if it doesn't
    literally reappear). A dimension the current question DOES name something
    for (even something that fails to resolve) is left alone — the user is
    talking about something new for that slot, not carrying the old one over.

    `village_hint` (optional): the exact village-name text from a just-resumed
    village-ambiguity pause (ClarificationNeeded.village_hint / Session.
    pending_village_hint). Used ONLY when this question's own mention
    extraction finds no village at all — the merged resume text ("...the one
    in Betasing block") does not reliably make the LLM re-tag the original
    village name, and without it SQL generation has no village_code to filter
    on and can invent one instead of using the now-resolved block/district."""
    # Out-of-range financial year — checked FIRST, before the LLM mention call
    # and any geography resolution. Works purely off the raw question text, so it
    # still fires when the extractor is unavailable or drops the year slot (a
    # comma-joined scope-pause reply like "wgh, 1999-20" does exactly that). A
    # year outside the data window makes the whole question unanswerable
    # regardless of the place, so this must not depend on anything downstream.
    # How many same-name villages a disambiguation pause lists. ASIMGRE is 7
    # villages and BOLCHUGRE / UMSAW 6 each; with 5 chips the last ones could never
    # be chosen (all-villages QA 2026-09-26). MGNREGA only.
    _vcap = 10 if _village_scheme(schemes) else 5
    _year_gap_note: "str | None" = None
    if settings.YEAR_RANGE_GUARD_ENABLED:
        _yraw = _out_of_range_year_in(question, schemes)
        if _yraw is not None:
            # Only refuse when NOTHING in the question is answerable. A question
            # naming an absent year AND a valid one ("compare FY 2023-24 and FY
            # 2024-25" — the Focus Legacy gap) used to be refused outright, which
            # asked the user to re-pick years they had already named, from a list
            # that deliberately excludes the one they asked about. Drop the
            # absent year, note the gap so the answer states it, and answer for
            # the years that exist — which is what the scheme's own response
            # contract requires (fy_gap_note).
            question, _year_gap_note, _handled = _apply_year_gap(question, schemes)
            if not _handled:
                raise _year_out_of_range_clarification(question, _yraw, schemes)

    mentions = await extract_entity_mentions(question)
    mentions = _drop_producer_group_names(question, mentions)
    mentions = _backfill_explicit_year(question, schemes, mentions)
    # Every raw-text place SCAN below reads this copy, so a producer-group name
    # the user labelled as one is never re-found as a block/village/constituency.
    _scan_q = _question_without_pg_name(question)
    # CM Elevate: an urban local body ("Jowai-municipal Board", "Mawkyrwat-town
    # Committee", "Nongpoh TC") is a BLOCK the data stores with that suffix. It
    # went to the district slot and was refused as outside Meghalaya, or was asked
    # "the Mawkyrwat C&RD block or village?" — both chips wrong (CM Elevate
    # all-blocks run 2026-09-28, KI-107). Move it to the block slot; the
    # collision gate below leaves it alone.
    _cme_urban_block = None
    if schemes in (["CM Elevate"], ["CM Elevate Legacy"]):
        for _uslot in ("district", "village", "block"):
            _uname = mentions.get(_uslot)
            if not _uname:
                continue
            _utext = str(_uname) if _URBAN_WORD_RE.search(str(_uname)) else (
                m.group(0) if (m := re.search(re.escape(str(_uname)) + r"[\s-]*(?:municipal(?:\s+board)?|"
                                              r"town\s+committee|m\.?\s?b\b|t\.?\s?c\b)", question or "",
                                              re.IGNORECASE)) else None)
            _ublk = await _cm_elevate_block_from_mention(_utext, schemes) if _utext else None
            if _ublk and _URBAN_BODY_RE.search(_ublk):
                mentions = {k: v for k, v in mentions.items() if k != _uslot}
                mentions["block"] = _cme_urban_block = _ublk
                logger.info("CM Elevate urban body %r -> block %r", _uname, _ublk)
                break

    resolved: dict[str, object] = {}
    notes: list[str] = []
    if _year_gap_note:
        # Stated in the answer, so a dropped gap year is visible to the user
        # rather than silently ignored.
        notes.append(_year_gap_note)
    # Human-readable names for whatever the query ends up filtering on, keyed by
    # dimension. Handed to the response composer so it says "West Garo Hills",
    # not the "wgh" the user typed or the "WEST GARO HILLS" DB literal.
    display: dict[str, str] = {}

    # The admin level the question states outright — either because the user
    # said it ("the Sohra constituency") or because this is a resumed
    # admin-level collision chip ("..., the assembly constituency, not another
    # area type"). Computed HERE, before any per-dimension branch runs: the
    # district branch below can resolve a name as a village on its own
    # fall-through, so a guard placed later would come too late to stop it.
    _stated_level = _explicit_level_in(question, schemes)
    # MGNREGA: a resumed VILLAGE-disambiguation chip ends ", DEMDEMA block, WEST
    # GARO HILLS" (_village_chip_question) with no ", the village" phrase, so the
    # chip-tail strip in _explicit_level_in does not run and the tail's "block"
    # read as the stated level. The village was then cleared and the query
    # filtered the whole BLOCK — Belbari's expenditure came back as DEMDEMA's
    # block total, BERUPARA's as 41.33 crore instead of 50.34 lakh (all-villages
    # QA 2026-09-26). village_hint is set only on exactly this resume, which is
    # what separates it from a user who typed ", BATABARI block, WEST GARO HILLS"
    # and meant the block (2026-09-18).
    if _village_scheme(schemes) and village_hint and _stated_level == "block" \
            and _CHIP_SCOPE_SUFFIX_RE.search(question) \
            and _explicit_level_in(_CHIP_SCOPE_SUFFIX_RE.sub("", question), schemes) is None:
        _stated_level = None
    # MGNREGA: a level word that is part of an exact village NAME is not a stated
    # level. "person-days in BLOCK CAMPUS" (village 274903, DAMBO RONGJENG block)
    # read as a block question and was refused as "not a C&RD block"
    # (all-villages QA 2026-09-26).
    if _village_scheme(schemes) and _stated_level in ("block", "district", "village"):
        _named = await _scan_village_in_question(question, schemes[0])
        if _named and re.search(rf"\b{_stated_level}\b", _named[0], re.IGNORECASE):
            _rest = re.sub(re.escape(_named[0]), " ", question, flags=re.IGNORECASE)
            if _explicit_level_in(_rest, schemes) is None:
                _stated_level = None
    # MGNREGA: a resumed village chip names exactly one village — pin it from the
    # chip text and skip every place branch (see _mgnrega_village_chip_pin).
    _chip_pinned = False
    # A twin-village chip's "(LGD 272933)" is the bot's own exact choice — pin it
    # even when the pause that offered it set no village_hint.
    # Focus Legacy is not a _village_scheme (none of its village guards apply),
    # but its village-disambiguation chips carry the same ", X block, DISTRICT"
    # tail — without the pin, tapping "RONGSIGRE — GASUAPARA block" re-asked the
    # same question forever (Focus Legacy all-villages run 2026-09-29, KI-156).
    # A later year chip carries no village hint, so for Focus Legacy the chip's
    # own tail is enough to pin (the year chip keeps it in the question text).
    # CM Elevate Legacy has the same placeholder design and the same chips, and
    # looped the same way: "DOMBAGRE — RERAPARA block" re-asked "which DOMBAGRE?"
    # on every tap (CM Elevate Legacy all-villages run 2026-09-29, KI-156).
    _tail_pin_scheme = schemes[0] if schemes in (["Focus Legacy"], ["CM Elevate Legacy"]) else None
    _pin_scheme = _village_scheme(schemes) or _tail_pin_scheme
    # The same year-chip case for the village schemes (PMAY-G officer cases run
    # 2026-10-02): "UMSAW — JIRANG block" then "All financial years combined"
    # gave "…UMSAW…, JIRANG block, RI BHOI across all financial years"; the year
    # pause had replaced the remembered village hint, so the pin was skipped,
    # UMSAW was re-resolved in RI BHOI (two of them) and asked again — tapping
    # the chip dropped the year, which asked the year again, forever. A chip
    # tail followed by the year chip's phrase is the bot's own village choice.
    # CM Elevate Legacy too (its own tail pin needs the word "village"): "MAWTNUM —
    # UMLING block" then "All financial years combined" was answered for the whole
    # Umling block, 224 instead of 4 (all-villages run 2026-10-02).
    _year_after_tail = bool(_pin_scheme and _YEAR_AFTER_CHIP_TAIL_RE.search(question or "")
                            and _CHIP_TAIL_PARSE_RE.search(question or ""))
    if _pin_scheme and (village_hint or _CHIP_LGD_CODE_RE.search(question or "") or _year_after_tail or (
            _pin_scheme == _tail_pin_scheme and re.search(r"\bvillage\b", question or "", re.IGNORECASE)
            and _CHIP_TAIL_PARSE_RE.search(question or ""))):
        _pin = await _mgnrega_village_chip_pin(
            question, _village_scheme(schemes) or ("CM Elevate Legacy" if schemes == ["CM Elevate Legacy"] else None))
        if _pin:
            _chip_pinned = True
            resolved["village_code"] = _pin["village_code"]
            display["village"] = _place_title(str(_pin["lgd_village_name"]))
            # village_code alone goes to SQL, as for any single resolved village —
            # a block/district filter alongside it tempted the generator to drop
            # the village and answer for the whole block (MEGAPGRE, NONGLUM).
            if _pin.get("lgd_block"):
                display["block"] = str(_pin["lgd_block"]).upper()
            if _pin.get("lgd_district"):
                display["district"] = str(_pin["lgd_district"]).title()
            mentions = {k: v for k, v in mentions.items() if k not in
                        ("district", "block", "village", "assembly_constituency", "districts", "blocks")}
            village_hint = None
            _stated_level = None
            logger.info("%s village chip pinned village_code %s (%s, %s block)", schemes[0],
                        _pin["village_code"], _pin["lgd_village_name"], _pin.get("lgd_block"))

    # A resumed collision chip states the level outright, but the LLM
    # mention-extractor still has to notice and re-tag the name into the
    # matching slot — the same extractor whose mis-tagging is what raised the
    # pause to begin with. When the level is stated and the extractor put the
    # name in a DIFFERENT slot, move it deterministically: the user has said
    # which level they mean, so it is no longer the model's call. Runs before
    # every resolution branch, so the name resolves at the chosen level and
    # nowhere else.
    # When the question states a level outright, that level is settled — put
    # the place name there and clear every other place slot, so nothing
    # downstream can re-resolve it somewhere else.
    #
    # The name is re-derived from the question TEXT rather than taken from the
    # extractor. A resumed chip rewrites the question to carry the CANONICAL
    # name ("...in MAWLAI..."), but the extractor frequently echoes the user's
    # ORIGINAL typo back — often into the very slot the chip names
    # ("block": "malwai" for the Mawlai-block chip). That value is present but
    # unresolvable, so trusting it makes the branch fail and fall through to
    # the village: the typo's own version of the bug this gate exists to stop.
    if _stated_level in ("district", "block", "assembly_constituency", "village"):
        _placed = None
        if _stated_level == "village":
            # Village is DB-backed — its own branch resolves it. Keep whatever
            # name is already in hand (extractor value, else the hint).
            _placed = mentions.get("village") or mentions.get("block")                 or mentions.get("district") or village_hint
            # The extractor can return a FRAGMENT of a multi-word name that is
            # itself another village: "…for PAKREGRE CHIKAMA village" came back
            # "CHIKAMA" (274919, same block) and the answer counted that village
            # (₹5,000 for the true ₹1,40,000; Focus Legacy all-villages run
            # 2026-09-29). The text names the village outright, so the whole
            # phrase wins when the DB holds it exactly.
            _vt = _VILLAGE_PHRASE_RE.findall(_scan_q or question or "")
            _full = _vt[-1].strip(" ,") if _vt else None
            # ...or returns nothing at all ("ADUGRE (NENGSRANG ADUGRE) village" —
            # the question then asked "which area?"; KI-153).
            if _full and (not _placed or (str(_placed).strip().upper() != _full.upper()
                                          and str(_placed).strip().upper() in _full.upper())):
                try:
                    if (await village_names_exact([_full])).get(_full.upper()):
                        logger.info("village %r read whole from the text (extractor gave %r)", _full, _placed)
                        _placed = _full
                except Exception:  # noqa: BLE001 — keep the extractor's value
                    logger.warning("village phrase check failed", exc_info=True)
            # ...or keeps the level word inside the name: "Nongjri Mission village"
            # matched no village exactly, the fuzzy match asked NONGJRI MISSION or
            # NONGBSAP MISSION, and the chip (which replaced the whole mention,
            # "village" included) was then answered for the whole Umsning block
            # (CM Elevate Legacy all-villages run 2026-09-29). Only when the bare
            # name is a stored village and the longer one is not.
            if _placed and re.search(r"\s+village\s*$", str(_placed), re.IGNORECASE):
                _bare = re.sub(r"\s+village\s*$", "", str(_placed), flags=re.IGNORECASE).strip()
                try:
                    _ex = await village_names_exact([_bare, str(_placed).strip()])
                    if _bare and _ex.get(_bare.upper()) and not _ex.get(str(_placed).strip().upper()):
                        logger.info("village %r read without its level word (extractor gave %r)", _bare, _placed)
                        _placed = _bare
                except Exception:  # noqa: BLE001 — keep the extractor's value
                    logger.warning("village level-word check failed", exc_info=True)
        else:
            _placed = _canonical_in_question(_scan_q, schemes[0], _stated_level)
            if not _placed:
                # No catalogue name in the text — fall back to whatever the
                # extractor found, but only if it resolves at this level.
                for _slot in ("district", "block", "village", "assembly_constituency"):
                    _v = mentions.get(_slot)
                    if _v and resolve_dimension(
                            str(_v), schemes[0], _stated_level).status == "resolved":
                        _placed = str(_v)
                        break
        if _placed:
            for _slot in ("district", "block", "village", "assembly_constituency"):
                mentions.pop(_slot, None)
            mentions[_stated_level] = _placed
            logger.info("explicit level %r — placed %r, cleared other place slots",
                        _stated_level, _placed)

    # ── Admin-level collision gate ──────────────────────────────────────────
    # Before resolving ANY single-name mention, check whether the bare name can
    # name more than one KIND of area (block vs assembly constituency vs
    # village vs district). This runs ahead of every per-dimension branch below
    # on purpose: those branches each resolve one slot in isolation and cannot
    # see that the same text also names a different level, which is how a bare
    # "Sohra" (assembly constituency) ended up fuzzy-matched to the village
    # Sohrarim and answered silently.
    #
    # Skipped when the question already names the level outright ("the Sohra
    # constituency", or a resumed chip's "..., the assembly constituency, not
    # another area type") — the level is settled, nothing to ask. Also skipped
    # for a plural comparison ("districts"/"blocks" arrays), which name their
    # own level by construction, and when a village_hint is carrying a
    # just-resumed village-ambiguity choice.
    if _village_scheme(schemes) in _VILLAGE_NARROW_SCHEMES and _stated_level is None and not village_hint \
            and not _chip_pinned and not mentions.get("districts") and not mentions.get("blocks"):
        _fp_m = next((str(mentions[s]) for s in ("district", "block", "village") if mentions.get(s)), None)
        _fp_dims = await _focusplus_alias_district_collision(_fp_m, _village_scheme(schemes)) if _fp_m else {}
        if _fp_dims:
            logger.info("Focus Plus: %r is a district alias AND a village — asking", _fp_m)
            raise _dimension_collision_clarification(question, _fp_m, _fp_dims)
    if _stated_level is None and not village_hint and not _chip_pinned \
            and not mentions.get("districts") and not mentions.get("blocks"):
        _scheme0 = schemes[0] if schemes else ""
        # The gate can only examine names the extractor handed over, and the
        # extractor drops a plainly-named place often enough to matter: it
        # returned {} on 5 of 5 calls for "applicants in mylliem under Green
        # Taxi CM Elevate and ware house Scheme" (2026-09-18), the lowercase
        # name buried between two scheme names. With no mention there was
        # nothing to test for ambiguity, the gate stayed silent, and the
        # generator filtered lgd_village_name = 'MYLLIEM' — a confident false
        # zero, where MYLLIEM is really a block (and an assembly constituency)
        # holding 2 applicants for those two schemes.
        #
        # So when no place mention survived, scan the raw question for a
        # catalogue name at either ambiguous level. This only ever ADDS a
        # candidate to test; whether it actually pauses is still decided by
        # collides_across_dimensions below, which needs 2+ readings.
        if not any(mentions.get(s) for s in
                   ("district", "block", "village", "assembly_constituency")):
            _found = None
            for _dim in ("block", "assembly_constituency"):
                _found = _canonical_in_question(_scan_q, _scheme0, _dim)
                if _found:
                    mentions = {**mentions, _dim: _found}
                    logger.info("admin-level gate: %r scanned from question text "
                                "(extractor returned no place)", _found)
                    break
            # _canonical_in_question matches names VERBATIM, so a MISSPELLED
            # place the extractor also dropped stayed invisible: "applicants in
            # tikrikulla ..." (a typo of the TIKRIKILLA block) resolved no
            # place at all, the generator passed the user's own spelling into
            # `lgd_block = 'TIKRIKULLA'`, and the query returned a confident
            # zero where the block really holds 243 applicants for those three
            # schemes (2026-09-18). resolve_dimension and
            # collides_across_dimensions both handle that typo; only the scan
            # feeding them was exact-only.
            #
            # So fall back to the place-phrase candidates ("in <name>") and let
            # the resolver's own fuzzy stage judge them. Whether this actually
            # pauses is still decided by collides_across_dimensions below,
            # which needs 2+ readings — this only supplies a name to test.
            if not _found:
                for _cand in _village_scan_candidates(question, _scheme0):
                    for _dim in ("block", "assembly_constituency"):
                        _r = resolve_dimension(_cand, _scheme0, _dim)
                        if _r.status == "resolved":
                            mentions = {**mentions, _dim: _cand}
                            logger.info("admin-level gate: %r fuzzy-matched %s %r "
                                        "(extractor returned no place)",
                                        _cand, _dim, _r.canonical)
                            _found = _cand
                            break
                    if _found:
                        break
        for _slot in ("district", "block", "village", "assembly_constituency"):
            _name = mentions.get(_slot)
            if not _name:
                continue
            if _cme_urban_block and _name == _cme_urban_block:
                break           # an urban body has one reading: the block (see above)
            # Every admin level this name resolves to. The in-memory catalogues
            # (district / block / assembly_constituency) are checked exact +
            # alias only — never fuzzy, or the gate would fire on almost every
            # name. The village catalogue is DB-backed (curated.dim_geography)
            # so it cannot be in that check; probe it here and pass the result
            # in. That village half is what catches "Sohra": an exact
            # assembly-constituency hit whose only competing reading is a
            # village. Without it the AC hit stands alone, the gate stays
            # quiet, and the old fall-through silently answers about the wrong
            # place.
            # The extractor routinely returns a TRUNCATED mention for a village
            # whose name carries part-markers: "william nagar(mb) - ward no.4"
            # comes back as "william nagar(mb)", which is ambiguous across 12
            # wards and makes this gate ask a question the text already answers
            # (reported 2026-09-17 — the pause repeated forever, the question
            # growing each round). When the raw text contains a LONGER name
            # that resolves to exactly one village, that is the real mention:
            # nothing is ambiguous, so the gate must stand down.
            _scanned = await _scan_village_in_question(_scan_q, _scheme0)
            if _scanned and len(_scanned[1]) == 1 \
                    and str(_name).strip().lower() in _scanned[0].strip().lower() \
                    and len(_scanned[0]) > len(str(_name)):
                logger.info("admin-level gate: %r is a truncation of %r, which resolves "
                            "to one village — not ambiguous", _name, _scanned[0])
                break
            _vr_probe = await _resolve_village_for(schemes, str(_name))
            _dims = collides_across_dimensions(
                str(_name), _scheme0,
                village_hit=_vr_probe.status in ("resolved", "ambiguous"))
            # CM Elevate's block catalogue lacks 21 of its 66 stored blocks, so a
            # bare "Mawhati" (a block with 204 applications AND two villages) was
            # offered the two villages only (CM Elevate all-blocks run 2026-09-28,
            # KI-107). A name the data stores as a block is a block reading.
            if schemes == ["CM Elevate"] and "block" not in _dims \
                    and (_dblk := await _cm_elevate_block_from_mention(str(_name), schemes)):
                _readings = {**_dims}
                if _vr_probe.status in ("resolved", "ambiguous"):
                    _readings.setdefault("village", "")
                if _readings:
                    _order = ("district", "block", "village", "assembly_constituency")
                    _readings["block"] = _place_title(_dblk)
                    _dims = {k: _readings[k] for k in _order if k in _readings}
            # Drop the assembly-constituency reading when this question's
            # metric has no AC column to read (see _ac_dimension_available) —
            # offering it would invite a choice that can never be answered.
            # If that leaves fewer than two readings, there is nothing left to
            # ask about and the remaining branches resolve it normally.
            if "assembly_constituency" in _dims and not _ac_dimension_available(question, schemes):
                _dims = {k: v for k, v in _dims.items() if k != "assembly_constituency"}
                logger.info("admin-level collision on %r: AC reading suppressed "
                            "(no constituency data for this metric)", _name)
                if len(_dims) < 2:
                    _dims = {}
            if _dims:
                logger.info("admin-level collision on %r: %s — asking", _name, list(_dims))
                raise _dimension_collision_clarification(question, str(_name), _dims)
            break

    # MGNREGA: a bare BLOCK / CONSTITUENCY name the extractor tagged as a village.
    # The collision gate above cannot see it: a block that shares its name with
    # its district's HQ town ("MAIRANG", "MAWKYRWAT") counts as a district hit,
    # which by design ends the check, and a name with a single reading
    # ("KHATARSHNONG LAITKROH") is never ambiguous. The village branch then
    # fuzzy-matched MAIRANG to five look-alike villages, or noted "not a known
    # village", after which the verifier rejected every query and the question
    # fell back to the reference documents (all-blocks QA 2026-09-26). Only when
    # the name is NOT itself an exact village name — a real village of that name
    # keeps the existing block-or-village pause.
    # MGNREGA: the extractor truncates a village whose name carries a qualifier —
    # "JONGCHIPARA (NOKAT)" came back as "JONGCHIPARA", which is a DIFFERENT
    # village (273377, not 273406), answered silently with its 8,848 person-days
    # instead of 0 (all-villages QA 2026-09-26). When the raw question holds a
    # LONGER exact village name that contains the mention, that is the village.
    if _village_scheme(schemes) and not _chip_pinned and _stated_level in (None, "village"):
        _slot = "village" if mentions.get("village") else (
            "block" if mentions.get("block") and resolve_dimension(
                str(mentions["block"]), schemes[0], "block").status != "resolved" else None)
        if _slot:
            _scanned = await _scan_village_in_question(_scan_q, schemes[0])
            _m = str(mentions[_slot]).strip().lower()
            # ...or the SAME name, when the extractor put a village in the block
            # slot and it is not a block: "RONGRA" (village 275928) fuzzy-tied
            # the RONGARA / RONGRAM blocks and drew an option-less "which block?".
            if _scanned and ((len(_scanned[0]) > len(_m) and _m in _scanned[0].lower())
                             or (_slot == "block" and _scanned[0].strip().lower() == _m)):
                logger.info("%s village mention %r widened to %r from the question text", schemes[0],
                            mentions[_slot], _scanned[0])
                # Also drop any other place slot that is just a piece of the full
                # name — "LASKARPARA (HAJONG)" came back as block "LASKARPARA" AND
                # district "HAJONG", and the stray "HAJONG" fuzzy-matched five
                # Hajong villages.
                _full = _scanned[0].lower()
                mentions = {**{k: v for k, v in mentions.items()
                               if k not in ("village", "block") and not (
                                   k in ("district", "assembly_constituency") and isinstance(v, str)
                                   and v.strip().lower() in _full)},
                            "village": _scanned[0]}

    # MGNREGA: a village name with "&" / "INCL" / punctuation, found whole in the
    # question, replaces the fragments the extractor made of it (see
    # _mgnrega_longest_village_in). Not when the level is stated as something
    # else, and not when the name is also a block / constituency / district —
    # the collision pause handles those.
    if _village_scheme(schemes) and not _chip_pinned and _stated_level in (None, "village"):
        _full = await _mgnrega_longest_village_in(_scan_q, schemes[0])
        if _full:
            _fn, _fl = _full[0], _full[0].strip().lower()
            # A name that is also a block / AC / district is left to the collision
            # pause — unless the question already chose "the village" (SELSELLA:
            # the extractor returned no place at all, so the village was lost and
            # the SQL compared village_code with the text 'SELSELLA').
            _admin = _stated_level is None and any(
                re.sub(r"\s+", " ", str(c)).strip().lower() == _fl
                for d in ("block", "assembly_constituency", "district")
                for c in canonical_names(schemes[0], d))
            _place = {k: v for k, v in mentions.items() if k in
                      ("village", "block", "blocks", "district", "districts", "assembly_constituency")}

            def _inside(v) -> bool:
                return all(isinstance(x, str) and x.strip().lower() in _fl
                           for x in (v if isinstance(v, list) else [v]))
            if not _admin and all(_inside(v) for v in _place.values()) \
                    and not (list(_place) == ["village"] and str(_place["village"]).strip().lower() == _fl):
                logger.info("%s village %r found whole in the question; replaces %s", schemes[0], _fn, _place)
                mentions = {**{k: v for k, v in mentions.items() if k not in _place}, "village": _fn}

    if _village_scheme(schemes) and _stated_level is None and not village_hint \
            and mentions.get("village") and not any(
                mentions.get(s) for s in ("district", "block", "assembly_constituency",
                                          "districts", "blocks")):
        _reread = await _mgnrega_admin_reading_of_village_mention(str(mentions["village"]), schemes[0])
        if len(_reread) >= 2:
            logger.info("MGNREGA village mention %r is %s — asking", mentions["village"], list(_reread))
            raise _dimension_collision_clarification(question, str(mentions["village"]), _reread)
        if _reread:
            (_dim, _canon), = _reread.items()
            logger.info("MGNREGA village mention %r re-read as %s %r", mentions["village"], _dim, _canon)
            mentions = {**{k: v for k, v in mentions.items() if k != "village"}, _dim: _canon}

    district_canon = None
    if mentions.get("districts"):
        # Two-or-more named districts to compare against each other (see
        # extract_entity_mentions) — resolve each independently and carry the
        # whole set forward as an IN-list, instead of the singular "district"
        # dimension which can only ever hold one value and would silently
        # drop every district but the last (the "compare ekh and wgh" bug).
        _district_canons: list[str] = []
        _district_displays: list[str] = []
        for _dname in mentions["districts"]:
            r = resolve_dimension(_dname, schemes[0], "district")
            if r.status == "ambiguous":
                raise ClarificationNeeded(f"Which district is being referred to by “{_dname}”?",
                                           rule="entity-ambiguous")
            if r.status != "resolved":
                notes.append(f"'{_dname}' is not a known district — say so, do not filter on it.")
                continue
            _district_canons.append(r.canonical)
            _district_displays.append(r.display or str(_dname).title())
        if _district_canons:
            resolved["district_list"] = _district_canons
            display["district"] = " and ".join(_district_displays)
    else:
        if not mentions.get("district"):
            # The LLM mention-extractor intermittently drops a plainly-named district
            # ("how many villages are covered in West Garo Hills" -> {}); fall back to
            # a deterministic scan of the raw question against the 12-name closed set.
            backstop = scan_dimension(question, schemes[0], "district")
            if backstop and backstop.status == "resolved":
                district_canon = backstop.canonical
                resolved["district"] = backstop.canonical
                display["district"] = backstop.display or str(backstop.canonical).title()
                logger.info("resolve_entities: district backstop matched %r in question text",
                            backstop.canonical)
        # A hill-range name ("Garo Hills", "Khasi region") the extractor tagged as a
        # district isn't one — leave it unresolved so _answer_data's region handler can
        # offer the range's districts as chips (or expand it). Don't raise the generic
        # "which district?" here.
        _mdist = mentions.get("district")
        if _mdist and detect_region(_mdist, schemes[0] if schemes else "") is not None:
            _mdist = None
        if _mdist:
            r = resolve_dimension(_mdist, schemes[0], "district")
            if r.status == "ambiguous":
                raise ClarificationNeeded(f"Which district is being referred to by “{_mdist}”?",
                                           rule="entity-ambiguous")
            if r.status == "resolved":
                district_canon = r.canonical
                resolved["district"] = r.canonical
                display["district"] = r.display or str(r.canonical).title()
            else:
                # Not a known district — it may be a VILLAGE the extractor
                # mistagged as "district" (a bare place name after "in" gives the
                # model no reliable signal for which admin level it is). Try
                # village resolution before concluding this is out of scope.
                _vr = await _resolve_village_for(schemes, _mdist)
                if _vr.status == "ambiguous":
                    listed = ", ".join(
                        f"{c['name']} in {c['block']} block ({c['district']})" for c in _vr.candidates[:_vcap])
                    options = [
                        {"label": _village_chip_label(c, _vr.candidates[:_vcap], schemes),
                         "question": _village_chip_question(question, _mdist, c, schemes, _vr.candidates[:_vcap])}
                        for c in _vr.candidates[:_vcap]
                    ]
                    raise ClarificationNeeded(
                        f"“{_mdist}” corresponds to more than one village: {listed}. "
                        "Which of these is intended?",
                        options=options, rule="entity-ambiguous", village_hint=_mdist)
                if _vr.status == "resolved":
                    resolved["village_code"] = _vr.canonical
                    display["village"] = _vr.display or _place_title(_mdist)
                elif (_cme_blk := await _cm_elevate_block_from_mention(_mdist, schemes)):
                    # "Jowai-municipal Board" tagged as a district: an urban body the
                    # CM Elevate data stores as a block, refused as outside Meghalaya
                    # (CM Elevate all-blocks run 2026-09-28, KI-107).
                    resolved["block"] = _cme_blk
                    display["block"] = _place_title(_cme_blk)
                elif _mgnrega_ac_reading(_mdist, schemes):
                    # An assembly constituency the extractor tagged as a
                    # district. "SOUTH TURA" is neither a district nor a village,
                    # and with only ONE reading the admin-level gate stays quiet,
                    # so it fell through to OutOfScope — "persons employed in
                    # SOUTH TURA" was refused as outside Meghalaya (MGNREGA QA
                    # 2026-09-26, DATA-004/014, KI-042). Hand it to the AC
                    # branch below, which also offers the drill-down.
                    mentions = {**mentions, "assembly_constituency": _mgnrega_ac_reading(_mdist, schemes)}
                    logger.info("district mention %r re-read as MGNREGA assembly constituency", _mdist)
                elif settings.OUT_OF_SCOPE_GUARD_ENABLED:
                    # Genuinely not a district, block, or village of ours — almost
                    # always a place in another state ("districts in Guwahati").
                    # Don't silently drop the filter and count 0; say plainly
                    # this is out of scope.
                    raise OutOfScope(f"district '{_mdist}' is not in Meghalaya")
                else:
                    notes.append(f"'{_mdist}' is not a known district — say so, do not filter on it.")

    if mentions.get("blocks"):
        # Two-or-more named blocks to compare against each other (see
        # extract_entity_mentions) — resolve each independently and carry the
        # whole set forward as an IN-list, instead of the singular "block"
        # dimension which can only ever hold one value and would silently
        # drop every block but the last.
        # "compare X and Y blocks" names blocks as plainly as "X block" (PMAY-G QA
        # 2026-09-28: PMAY-OFF-023 asked "block or village?" for Mawphlang).
        _has_block_word = bool(re.search(r"\bblocks?\b", question, re.IGNORECASE))
        _block_canons: list[str] = []
        _block_displays: list[str] = []
        # A bare comparison name with no admin-level word ("X or Y", "compare X
        # and Y") gets tagged into this "blocks" array by default (see the
        # extractor prompt's own "tag an ambiguous bare name as block" rule),
        # even when both names are actually VILLAGES — e.g. "which has more
        # completed houses: <village 1> or <village 2>". When a name here
        # isn't a resolvable block at all, it used to just get a "not a known
        # block" note and get dropped; with every name dropped, geography
        # stayed fully unresolved and the generic scope pause fired, offering
        # every district statewide instead of just the two villages named
        # (reported 2026-09-10, PMAY-OFF-022). Try village resolution as a
        # fallback for exactly the names that fail as a block, mirroring the
        # `_vcheck` block-vs-village disambiguation a few lines below.
        _village_canons: list[int] = []
        _village_displays: list[str] = []
        for _bname in mentions["blocks"]:
            r = resolve_dimension(_bname, schemes[0], "block")
            if r.status == "ambiguous":
                raise ClarificationNeeded(f"Which block is being referred to by “{_bname}”?",
                                           rule="entity-ambiguous")
            if r.status != "resolved":
                _vr = await _resolve_village_for(schemes, _bname, district=district_canon)
                if _vr.status == "ambiguous":
                    listed = ", ".join(
                        f"{c['name']} in {c['block']} block ({c['district']})" for c in _vr.candidates[:_vcap])
                    options = [
                        {"label": _village_chip_label(c, _vr.candidates[:_vcap], schemes),
                         "question": _village_chip_question(question, _bname, c, schemes, _vr.candidates[:_vcap])}
                        for c in _vr.candidates[:_vcap]
                    ]
                    raise ClarificationNeeded(
                        f"“{_bname}” corresponds to more than one village: {listed}. "
                        "Which of these is intended?",
                        options=options, rule="entity-ambiguous", village_hint=_bname)
                if _vr.status == "resolved":
                    _village_canons.append(_vr.canonical)
                    _village_displays.append(_vr.display or str(_bname).title())
                else:
                    notes.append(f"'{_bname}' is not a known block or village — say so, "
                                 "do not filter on it.")
                continue
            if not _has_block_word:
                _vcheck = await _resolve_village_for(schemes, _bname)
                if _vcheck.status in ("resolved", "ambiguous"):
                    stem = question.strip().rstrip(" ?.")
                    raise ClarificationNeeded(
                        f"“{_bname}” could mean either the block or a village with "
                        "that name. Which did you mean?",
                        options=[
                            {"label": f"The {_bname} block", "question": f"{stem}, the block, not the village"},
                            {"label": f"The {_bname} village", "question": f"{stem}, the village, not the block"},
                        ],
                        rule="entity-ambiguous")
            _block_canons.append(r.canonical)
            _block_displays.append(r.display or str(_bname).title())
        if _block_canons:
            resolved["block_list"] = _block_canons
            display["block"] = " and ".join(_block_displays)
            # A district named in the same breath as several blocks normally
            # qualifies just ONE of them — it is there to separate a
            # same-named block from its twin elsewhere ("BATABARI block, WEST
            # GARO HILLS": Batabari exists in both South and West Garo Hills).
            # ANDing that district against the WHOLE block list then silently
            # deletes every block that legitimately sits somewhere else:
            # "applicants in Shallang ... BATABARI block, WEST GARO HILLS"
            # returned nothing for Shallang, because Shallang is a WEST KHASI
            # HILLS block and `lgd_district = 'WEST GARO HILLS' AND lgd_block
            # IN ('SHALLANG','BATABARI')` can never match it (reported
            # 2026-09-18). Each block already knows its own district, so drop
            # the standalone district filter whenever the named blocks do not
            # all belong to it — the block names are the more specific filter
            # and they carry their own geography.
            _parents = {b: block_parent_district(b, schemes[0] if schemes else "")
                        for b in _block_canons}
            if district_canon:
                _outside = [b for b, d in _parents.items()
                            if d and d.upper() != str(district_canon).upper()]
                if _outside:
                    logger.info(
                        "district %r dropped: blocks %s sit outside it (block list spans "
                        "districts)", district_canon, _outside)
                    resolved.pop("district", None)
                    display.pop("district", None)
                    district_canon = None
            # Tell the generator which district each block belongs to, so a
            # name that exists in two districts is still pinned to the right
            # one rather than double-counted across both.
            _known = {b: d for b, d in _parents.items() if d}
            if _known:
                resolved["block_list_districts"] = _known
        if _village_canons:
            resolved["village_code_list"] = _village_canons
            display["village"] = " and ".join(_village_displays)
    elif mentions.get("block") or (
            _explicit_level_in(question, schemes) == "block"
            and not mentions.get("village") and not mentions.get("blocks")
            and (_block_scan := scan_dimension(question, schemes[0], "block",
                                               level_is_explicit=True)) is not None
            and _block_scan.status == "resolved"):
        # The extractor drops a plainly-named block just as it drops districts
        # — "in shallang block under Piggery Scheme, PRIME ... (SEED) and
        # Meghalaya Poultry Farming Scheme" returned {} on 5 of 5 calls
        # (2026-09-18), the lowercase name buried between two long scheme
        # names. With no resolved block the generator invented
        # `lgd_block = 'SHALLANG'` off the raw text; the verifier demanded a
        # `lgd_district = 'MEGHALAYA'` filter instead, the state-pseudo-row
        # guard rejected that, the repair put it back, and the loop burned its
        # budget and fell through to the KB fallback. The answer is a plain 45.
        #
        # Only runs when the question names the level outright, so a bare
        # "Shallang" still reaches the village/AC clarification flow.
        if not mentions.get("block"):
            mentions = {**mentions, "block": _block_scan.canonical}
            logger.info("resolve_entities: block backstop matched %r in question text",
                        _block_scan.canonical)
        r = resolve_dimension(mentions["block"], schemes[0], "block")
        if r.status == "ambiguous":
            raise ClarificationNeeded(f"Which block is being referred to by “{mentions['block']}”?",
                                       rule="entity-ambiguous")
        if r.status == "resolved":
            # Block and village names collide constantly in Meghalaya (26 of 56
            # block names are also village names, per the resolver YAML) — a bare
            # name the user didn't explicitly call a "block" is genuinely
            # ambiguous. Silently answering at the block level here is exactly
            # the bug QA reported (bot picks village data for a block question,
            # or vice versa, without ever asking).
            # Same truncation guard as the admin-level gate above: when the raw
            # text carries a LONGER name than this mention and that longer name
            # resolves to exactly one village, the mention is a fragment of it
            # ("william nagar(mb)" from "william nagar(mb) - ward no.4"), not a
            # genuine block-vs-village ambiguity. Resolve the village instead of
            # asking a question the text already answers.
            _longer = await _scan_village_in_question(_scan_q, schemes[0] if schemes else "")
            if _longer and len(_longer[1]) == 1 \
                    and str(mentions["block"]).strip().lower() in _longer[0].strip().lower() \
                    and len(_longer[0]) > len(str(mentions["block"])):
                _pick = _longer[1][0]
                resolved["village_code"] = _pick["village_code"]
                display["village"] = _place_title(_pick["name"])
                logger.info("block mention %r is a truncation of village %r — resolved it",
                            mentions["block"], _longer[0])
            elif not re.search(r"\bblock\b", question, re.IGNORECASE) \
                    and not (_cme_urban_block and mentions["block"] == _cme_urban_block):
                _vcheck = await _resolve_village_for(schemes, mentions["block"])
                if _vcheck.status in ("resolved", "ambiguous"):
                    stem = question.strip().rstrip(" ?.")
                    raise ClarificationNeeded(
                        f"“{mentions['block']}” could mean either the block or a village with "
                        "that name. Which did you mean?",
                        options=[
                            {"label": f"The {mentions['block']} block", "question": f"{stem}, the block, not the village"},
                            {"label": f"The {mentions['block']} village", "question": f"{stem}, the village, not the block"},
                        ],
                        rule="entity-ambiguous")
            # Not when the mention turned out to be a truncated VILLAGE name
            # (handled just above): village_code already pins the exact place,
            # and adding the block it was a fragment of would filter a
            # different, larger area alongside it.
            if "village_code" not in resolved:
                resolved["block"] = r.canonical
                display["block"] = r.display or str(r.canonical).title()
                # Same conflict as the block_list branch above, for one block:
                # a district named alongside a block that provably sits in a
                # DIFFERENT district makes `lgd_district = X AND lgd_block = Y`
                # match zero rows. The block is the more specific filter and
                # knows its own district, so keep the block and drop the
                # contradicting district rather than answering "no records".
                _bp = r.parent_district or block_parent_district(
                    r.canonical, schemes[0] if schemes else "")
                if _bp and district_canon and _bp.upper() != str(district_canon).upper():
                    logger.info("district %r dropped: block %r sits in %r",
                                district_canon, r.canonical, _bp)
                    resolved.pop("district", None)
                    display.pop("district", None)
                    district_canon = None
        else:
            # The mention-extractor sometimes tags a full DISTRICT name as the
            # "block" when the question itself says "by block" right next to it
            # ("compare ... by block in West Garo Hills") — it reads the phrase
            # "by block in X" as naming X as the block. Before treating this as
            # not-a-known-block (or, worse, OutOfScope — a real district getting
            # rejected as "not in Meghalaya" is a confusing wrong answer), check
            # whether the same text is actually a district and use it as one.
            _as_district = resolve_dimension(mentions["block"], schemes[0], "district")
            if _as_district.status == "resolved":
                # Either newly discovered here, or (commonly) the SAME text was
                # already captured as the district via the raw-text backstop
                # scan above (mentions.get("district") was empty, so that scan
                # ran on the full question text and found "West Garo Hills"
                # there too) — in that case district_canon is already set and
                # this "block" mention is just a duplicate tag for it. Either
                # way, there's nothing wrong here: don't overwrite an existing
                # district_canon, and don't fall through to "not a known block".
                # Also skip when a district_list comparison already resolved —
                # this "block" mention is then just the extractor's other tag
                # for one of those same districts.
                if not district_canon and not resolved.get("district_list"):
                    district_canon = _as_district.canonical
                    resolved["district"] = _as_district.canonical
                    display["district"] = _as_district.display or str(_as_district.canonical).title()
            elif schemes == ["Focus Plus"] and str(mentions["block"]).strip().upper() == _FP_BLANK_BLOCK:
                # "Nan" is a stored block_name_raw value — the source file's mark
                # for records whose block and district were left blank. It is not
                # a C&RD block, but it IS in the data: saying "not in this data"
                # (and running SELECT 'not available') was wrong. Filter on it
                # and explain it (Focus Plus all-blocks run 2026-09-27).
                resolved["block"] = _FP_BLANK_BLOCK
                display["block"] = "Nan (block not recorded)"
                notes.append(_FP_BLANK_BLOCK_NOTE)
            else:
                # Also not a district — it may be a VILLAGE the extractor
                # mistagged as "block" (villages far outnumber blocks, and a
                # bare name after "in" gives the model no reliable signal for
                # which admin level it is — e.g. "PMAY beneficiaries in
                # Abagre", a real West Garo Hills village). Try village
                # resolution before concluding this is out of scope. Scope by
                # district_canon when the question already pinned one down —
                # otherwise a village-disambiguation chip's own answer text
                # ("Nongthymmai, MAWRYNGKNENG block, EAST KHASI HILLS") gets
                # re-resolved with no district scope, rediscovers the exact
                # same statewide candidate set the chip was meant to narrow,
                # and raises the identical clarification forever (reported
                # 2026-09-09, "nongthymmai" in EAST KHASI HILLS).
                #
                # NOT when the user explicitly said "the block" (an admin-level
                # choice chip, or their own wording): the whole point of that
                # answer is that they do not want the village reading, so
                # quietly resolving one anyway answers at a grain they just
                # declined. Say the block doesn't exist instead.
                if _stated_level == "block":
                    notes.append(
                        f"'{mentions['block']}' is not a C&RD block in this data — say so "
                        "plainly. Do NOT answer using a village or district of the same "
                        "name; the question asked specifically for the block.")
                    _vr = None
                else:
                    _vr = await _resolve_village_for(schemes, mentions["block"], district=district_canon)
                if _vr is not None and _vr.status == "ambiguous":
                    # District scoping alone doesn't always get to one candidate
                    # (e.g. several distinctly-blocked villages share a name inside
                    # the same district). The chip that got us here already names
                    # the intended block in its own text ("Nongthymmai, MAWRYNGKNENG
                    # block, EAST KHASI HILLS") — the LLM extractor just keeps
                    # re-tagging the village as "block" and dropping the real block,
                    # so re-resolving lands back on the SAME candidate set every
                    # round and the clarification loops forever (reported
                    # 2026-09-09, "nongthymmai" in EAST KHASI HILLS). Before asking
                    # again, check the raw text deterministically: if exactly one
                    # candidate's own block name appears in it, that already IS the
                    # user's answer.
                    _text_hits = [
                        c for c in _vr.candidates
                        if c.get("block") and re.search(
                            rf"\b{re.escape(c['block'])}\b", question, re.IGNORECASE)
                    ]
                    if len(_text_hits) == 1:
                        _pick = _text_hits[0]
                        resolved["village_code"] = _pick["village_code"]
                        display["village"] = _place_title(_pick["name"])
                        _vr = None
                    else:
                        listed = ", ".join(
                            f"{c['name']} in {c['block']} block ({c['district']})" for c in _vr.candidates[:_vcap])
                        options = [
                            {"label": _village_chip_label(c, _vr.candidates[:_vcap], schemes),
                             "question": _village_chip_question(question, mentions["block"], c, schemes, _vr.candidates[:_vcap])}
                            for c in _vr.candidates[:_vcap]
                        ]
                        raise ClarificationNeeded(
                            f"“{mentions['block']}” corresponds to more than one village: {listed}. "
                            "Which of these is intended?",
                            options=options, rule="entity-ambiguous", village_hint=mentions["block"])
                if _vr is None:
                    pass  # already resolved directly from the single text hit above
                elif _vr.status == "resolved":
                    resolved["village_code"] = _vr.canonical
                    display["village"] = _vr.display or _place_title(mentions["block"])
                elif (_cme_blk := await _cm_elevate_block_from_mention(mentions["block"], schemes)):
                    resolved["block"] = _cme_blk          # an urban body stored as a block (KI-107)
                    display["block"] = _place_title(_cme_blk)
                elif settings.OUT_OF_SCOPE_GUARD_ENABLED:
                    raise OutOfScope(f"block '{mentions['block']}' is not in Meghalaya")
                else:
                    notes.append(f"'{mentions['block']}' is not a known block — say so, do not filter on it.")

    if mentions.get("assembly_constituency"):
        r = resolve_dimension(mentions["assembly_constituency"], schemes[0], "assembly_constituency")
        if r.status == "ambiguous":
            raise ClarificationNeeded(
                f"Which assembly constituency is being referred to by "
                f"“{mentions['assembly_constituency']}”?", rule="entity-ambiguous")
        if r.status == "resolved":
            resolved["assembly_constituency"] = r.canonical
            display["assembly_constituency"] = r.display or str(r.canonical).title()
            # Step 2 of the hierarchy: having settled that this IS the
            # constituency, offer to narrow inside it. Only when the question
            # hasn't already narrowed itself — a resumed drill-down chip says
            # "within the X block only" / "the whole constituency", and a
            # question that independently names a district/block/village has
            # answered this too. Best-effort: if the contents can't be read,
            # the flow continues with the whole constituency, as before.
            _already_narrowed = (
                _AC_SCOPED_RE.search(question)
                or re.search(r"\bwhole\s+constituency\b", question, re.IGNORECASE)
                or any(resolved.get(k) for k in
                       ("district", "district_list", "block", "block_list",
                        "village_code", "village_code_list"))
            )
            _narrow = _AC_NARROW_RE.search(question)
            if _narrow:
                # A resumed drill-down chip — apply its district/block filter
                # alongside the constituency, so the answer covers exactly the
                # overlap the user asked for.
                _lvl = _narrow.group("level").lower()
                _nm = _narrow.group("name").strip()
                _nr = resolve_dimension(_nm, schemes[0], _lvl)
                if _nr.status == "resolved":
                    _canon, _disp = _nr.canonical, _nr.display or str(_nr.canonical).title()
                else:
                    # These chips are built from values read live out of
                    # curated.v_employment (constituency_contents), and the
                    # YAML block catalogue does not necessarily list every one
                    # of them. The value came from the database itself, so it
                    # is a valid literal even when the catalogue has no entry
                    # — use it rather than dropping the narrowing the user
                    # explicitly picked. Storage is upper-case for both
                    # lgd_district and lgd_block (docs/DATA_MODEL.md).
                    _canon, _disp = _nm.upper(), _nm.title()
                resolved[_lvl] = _canon
                display[_lvl] = _disp
                if _lvl == "district":
                    district_canon = _canon
                logger.info("AC drill-down: also filtering %s = %r", _lvl, _canon)
            elif not _already_narrowed:
                _contents = await constituency_contents(str(r.canonical), schemes[0] if schemes else None)
                _drill = _ac_drilldown_clarification(
                    question, display["assembly_constituency"], _contents,
                    schemes[0] if schemes else None)
                if _drill is not None:
                    raise _drill
        else:
            # assembly_constituency data exists ONLY in mgnrega_employment (no
            # catalogue loaded for other schemes, or the name genuinely isn't
            # one) — say so instead of silently falling back to a block/
            # district filter, which would answer a different, unintended level.
            notes.append(
                f"'{mentions['assembly_constituency']}' is not a known assembly constituency "
                f"for {schemes[0] if schemes else 'this scheme'} — say so, do not silently "
                "filter on a block or district instead.")

    if mentions.get("year"):
        raw_year = mentions["year"]
        year_key = None
        # MGNREGA has a curated year catalogue (handles aliases like "FY24",
        # "last year"); try it first when MGNREGA is in play.
        if "MGNREGA" in schemes:
            r = resolve_dimension(raw_year, "MGNREGA", "year")
            if r.status == "ambiguous":
                raise ClarificationNeeded(f"Which financial year is being referred to by “{raw_year}”?")
            if r.status == "resolved":
                year_key = r.canonical
        # Fallback / PMAY path — PMAY has no year catalogue, so parse the FY start
        # year straight from the text ("2023", "2023-24", "FY 2023-24" -> 2023).
        if year_key is None:
            year_key = _parse_year_key(raw_year)
        if year_key is not None and not _year_in_data_range(year_key, schemes):
            # A real, parseable year — just not one these scheme(s) hold.
            if settings.YEAR_RANGE_GUARD_ENABLED:
                raise _year_out_of_range_clarification(question, raw_year, schemes)
            _yrs, _lv = _available_years_for(schemes)
            notes.append(f"'{raw_year}' is outside the data "
                         f"({', '.join(_lv)} cover FY {_yrs[0]} to FY {_yrs[-1]} only).")
        elif year_key is None:
            # Couldn't pin it to a year at all. If it was plainly typed as one
            # ("1999-20", "FY 2019-20"), it's an out-of-range year we simply
            # failed to parse — say the coverage, same as above; otherwise it's
            # gibberish in the year slot, so leave a soft note and move on.
            if settings.YEAR_RANGE_GUARD_ENABLED and _looks_like_year_mention(raw_year):
                raise _year_out_of_range_clarification(question, raw_year, schemes)
            notes.append(f"'{raw_year}' is not a recognisable financial year.")
        else:
            # Both v_employment and v_pmay key the year on `year_key` (smallint).
            resolved["year_key"] = year_key
            display["year"] = f"FY {year_key}-{(year_key + 1) % 100:02d}"

    # Prefer the current question's own village mention; fall back to the
    # remembered hint from a just-resumed village-ambiguity pause only when
    # this turn's extraction found no village at all (see the docstring above).
    #
    # A question that states a NON-village level outright ("..., the assembly
    # constituency, not another area type" — a resumed admin-level collision
    # chip) must not resolve a village at all: the pending village hint is
    # stale in that case (it was remembered when the level was still open), and
    # the block/district branches above have already placed the name at the
    # level the user chose. Letting it through set village_code alongside the
    # chosen level, which then filters the query down to one village even
    # though the user explicitly asked for the constituency.
    _village_text = mentions.get("village") or village_hint
    if _stated_level is not None and _stated_level != "village":
        _village_text = None
    # Deterministic backstop for a village the extractor dropped (see
    # _scan_village_in_question). Only when NOTHING else already placed this
    # turn's geography at village grain, and only when the question names no
    # block/AC of its own for the same span — those dimensions own their names.
    # Skipped when the question states a level of its own: either the user
    # chose "village" (in which case the extractor's own mention already went
    # down the normal village path above) or they chose a NON-village level,
    # and filling a village in behind that choice would answer at a grain they
    # explicitly declined.
    if not _village_text and not resolved.get("village_code") \
            and not resolved.get("village_code_list") and _stated_level is None:
        _scanned = await _scan_village_in_question(_scan_q, schemes[0] if schemes else "")
        if _scanned:
            _vname, _vhits = _scanned
            if _village_scheme(schemes):   # see _resolve_village_for
                _vhits = [c for c in _vhits if not str(c.get("name", "")).strip().lower()
                          .startswith("unresolved")] or _vhits
            _vhits = await _fl_rank_villages(schemes, _vhits)
            logger.info("village backstop: %r matched %d village(s) in the question text "
                        "the extractor dropped", _vname, len(_vhits))
            # Narrow by a district the question already pinned before deciding
            # whether this is still ambiguous — "ASIMGRE ... for East Garo
            # Hills" has exactly one ASIMGRE in that district.
            # Narrow by a block the question already pinned first — it is the
            # finer of the two scopes, and a resumed village chip names both
            # ("..., SELSELLA block, WEST GARO HILLS"). Without this the
            # district scope alone can still leave several same-named villages
            # and the chip resolves only to block grain, losing the village.
            _blk = resolved.get("block")
            if _blk:
                _by_block = [c for c in _vhits
                             if str(c.get("block") or "").upper() == str(_blk).upper()]
                if _by_block:
                    _vhits = _by_block
            if district_canon:
                _scoped = [c for c in _vhits
                           if str(c["district"]).upper() == str(district_canon).upper()]
                if _scoped:
                    _vhits = _scoped
                elif _blk and len(_vhits) == 1:
                    # The block filter already pinned exactly one village; the
                    # district mismatch is just the stale one from the original
                    # question, so let the block's own district stand.
                    district_canon = _vhits[0]["district"]
                    resolved["district"] = _vhits[0]["district"]
                    display["district"] = str(_vhits[0]["district"]).title()
                else:
                    # The question named a district AND a village, but no
                    # village of that name exists there ("ASIMGRE ... for East
                    # Khasi Hills" — every ASIMGRE is in the Garo Hills). The
                    # two filters contradict each other, so neither answering
                    # district-wide (the old silent-drop bug, which reported a
                    # whole district's figure as though it were the village's)
                    # nor offering villages in OTHER districts is right. Say
                    # so plainly instead.
                    _dname = display.get("district") or str(district_canon).title()
                    _elsewhere = ", ".join(sorted({str(c["district"]).title() for c in _vhits}))
                    # Each village chip must drop the district the user named,
                    # or the resumed question carries TWO conflicting districts
                    # ("... for East Khasi Hills ..., SELSELLA block, WEST GARO
                    # HILLS") and resolves to neither the village nor the right
                    # district. Strip the original district phrase first, then
                    # let _village_chip_question pin the village.
                    _no_dist = re.sub(
                        rf"\s*\b(?:in|for|of|at|from)\s+{re.escape(str(_dname))}\b",
                        "", question, count=1, flags=re.IGNORECASE).strip()
                    raise ClarificationNeeded(
                        f"There is no village called “{_vname}” in {_dname}. "
                        f"Villages with that name are in {_elsewhere}. Did you mean one of "
                        f"those, or the whole of {_dname}?",
                        options=[
                            {"label": _village_chip_label(c, _vhits[:_vcap], schemes),
                             "question": _village_chip_question(_no_dist, _vname, c, schemes, _vhits[:_vcap])}
                            for c in _vhits[:_vcap]
                        ] + [{
                            "label": f"All of {_dname}",
                            "question": _drop_place_phrase(question, _vname),
                        }],
                        rule="entity-ambiguous")
            if len(_vhits) == 1:
                resolved["village_code"] = _vhits[0]["village_code"]
                display["village"] = _place_title(_vhits[0]["name"])
            else:
                # Several real villages share this name — ask, never guess.
                listed = ", ".join(
                    f"{c['name']} in {c['block']} block ({c['district']})" for c in _vhits[:_vcap])
                raise ClarificationNeeded(
                    f"“{_vname}” corresponds to more than one village: {listed}. "
                    "Which of these is intended?",
                    options=[
                        {"label": _village_chip_label(c, _vhits[:_vcap], schemes),
                         "question": _village_chip_question(question, _vname, c, schemes, _vhits[:_vcap])}
                        for c in _vhits[:_vcap]
                    ],
                    rule="entity-ambiguous", village_hint=_vname)
    if _village_text:
        r = await _resolve_village_for(schemes, _village_text, district=district_canon,
                                   block=resolved.get("block"))
        if r.status == "ambiguous":
            # Two candidates can share the same (name, district) while being
            # genuinely different villages in different blocks with their own
            # data (e.g. two "Adugre"s in SOUTH WEST GARO HILLS — one in
            # Betasing block, one in Rerapara) — block is the distinguishing
            # feature, so it MUST be in both the message and the chip label, or
            # they read as duplicates and the user can't tell them apart.
            listed = ", ".join(
                f"{c['name']} in {c['block']} block ({c['district']})" for c in r.candidates[:_vcap])
            options = [
                {"label": _village_chip_label(c, r.candidates[:_vcap], schemes),
                 "question": _village_chip_question(question, _village_text, c, schemes, r.candidates[:_vcap])}
                for c in r.candidates[:_vcap]
            ]
            raise ClarificationNeeded(
                f"“{_village_text}” corresponds to more than one village: {listed}. "
                "Which of these is intended?",
                options=options,
                rule="entity-ambiguous",
                village_hint=_village_text)
        if r.status == "resolved":
            # Symmetric check to the block branch above: this name might also be
            # a block name. Only worth asking when the district/block scope
            # didn't already pin it down (a village resolved WITH a district
            # scope is unambiguous) and the user didn't already say "village".
            if not district_canon and not re.search(r"\bvillage\b", question, re.IGNORECASE):
                _bcheck = resolve_dimension(_village_text, schemes[0], "block")
                if _bcheck.status == "resolved":
                    stem = question.strip().rstrip(" ?.")
                    raise ClarificationNeeded(
                        f"“{_village_text}” could mean either the village or a block "
                        "with that name. Which did you mean?",
                        options=[
                            {"label": f"The {_village_text} village", "question": f"{stem}, the village, not the block"},
                            {"label": f"The {_village_text} block", "question": f"{stem}, the block, not the village"},
                        ],
                        rule="entity-ambiguous")
            resolved["village_code"] = r.canonical
            display["village"] = r.display or _place_title(_village_text)
        else:
            notes.append(f"'{_village_text}' is not a known village — say so, do not filter on it.")

    # PMAY house-construction-stage ("Proposed Site", "Existing site(Old House)",
    # "plinth stage", "not started", …). A closed set the LLM mention-extractor
    # above doesn't cover — matched deterministically against the SME alias
    # catalogue over the whole question. "completed" / "in progress" are left to
    # the is_completed / is_in_progress boolean path on purpose.
    if "PMAY-G" in schemes:
        hs = resolve_house_status(question, "PMAY-G")
        if hs and hs.values:
            resolved["house_status"] = hs.values if len(hs.values) > 1 else hs.values[0]
            display["house_status"] = hs.display or " and ".join(hs.values)

    # Focus Plus tranche_label ("Tranch 1".."Tranch 4 - Feb-March") — closed set,
    # not covered by the LLM mention-extractor, and routinely mistyped against
    # its odd stored spelling ("tranche2" vs "Tranch 2 - August"). Resolved
    # deterministically (with a RapidFuzz fallback for misspellings) so a
    # near-miss maps to the exact stored label instead of silently filtering
    # the SQL to zero rows and reporting a misleading null total.
    if "Focus Plus" in schemes:
        tr = resolve_tranche_label(question, "Focus Plus")
        if tr and tr.values:
            resolved["tranche_label"] = tr.values if len(tr.values) > 1 else tr.values[0]
            display["tranche_label"] = tr.display or " and ".join(tr.values)

    # CM Elevate sub-scheme ("Piggery", "Dairy Development", ... — a 15-value
    # closed set) — resolved deterministically against the SME alias catalogue.
    # Without this, a typo'd or loosely-phrased sub-scheme name reaches the SQL
    # generator as raw text, which then guesses a scheme_name literal that
    # doesn't exactly match storage and silently counts zero rows (see
    # resolve_cm_scheme's docstring).
    if "CM Elevate" in schemes:
        cs = resolve_cm_scheme(question, "CM Elevate")
        if cs and cs.values:
            resolved["cm_scheme"] = cs.values if len(cs.values) > 1 else cs.values[0]
            display["cm_scheme"] = cs.display or " and ".join(cs.values)
        else:
            # resolve_cm_scheme found no single real sub-scheme — check whether
            # the question instead named a scheme-FAMILY word in the singular
            # ("the vehicle scheme") that covers 2+ real, non-interchangeable
            # sub-schemes. Left unhandled, this reached the SQL generator as
            # bare text, which guessed a scheme_name literal that doesn't exist
            # ('Meghalaya Vehicle Scheme') and silently returned 0 rows dressed
            # up as a refusal (confirmed live 2026-09-11) — exactly the "never
            # guess, ask" case cmelevate_entity_resolver.yaml's overloaded_terms
            # section documents but was never wired to any code path.
            grp = resolve_cm_scheme_group_ambiguity(question, "CM Elevate")
            if grp:
                raise _cm_scheme_group_clarification(question, grp)
            # Not singular-ambiguous — check the PLURAL/collective reading
            # ("vehicle schemes", "livestock", "PRIME family"): a real,
            # already-answerable group-breakdown question (cmelevate_few_shot.yaml
            # has worked IN-list examples), not something to ask about. Populate
            # cm_scheme as a multi-value resolve, same shape resolve_cm_scheme
            # itself uses for "compare Piggery and Poultry" — without this, the
            # SQL generator's hardcoded IN-list for the group got rejected by the
            # semantic verifier for having no resolved entity to justify it
            # (confirmed live 2026-09-11, once the routing fix above let this
            # question reach SQL generation for the first time).
            grp2 = resolve_cm_scheme_group(question, "CM Elevate")
            if grp2:
                resolved["cm_scheme"] = grp2["schemes"]
                display["cm_scheme"] = f"the {grp2['group']} schemes ({', '.join(grp2['schemes'])})"

    # CM Elevate Legacy's 13 schemes — its OWN catalogue (the stored spellings
    # differ from the applications dataset's: 'Prime Tourism Vehicle Scheme' vs
    # 'PRIME Tourism Vehicle Scheme'), resolved into the same cm_scheme slot.
    # Skipped when the applications dataset is also in play, so the two
    # catalogues never overwrite each other's literal.
    if "CM Elevate Legacy" in schemes and "CM Elevate" not in schemes:
        _seri = _cm_legacy_sericulture_choice(question)
        if _seri == "ask":
            raise _cm_legacy_sericulture_clarification(question)
        cs = resolve_cm_scheme(question, "CM Elevate Legacy")
        if _seri:
            resolved["cm_scheme"] = _seri if len(_seri) > 1 else _seri[0]
            display["cm_scheme"] = " and ".join(_seri)
        elif cs and cs.values:
            resolved["cm_scheme"] = cs.values if len(cs.values) > 1 else cs.values[0]
            display["cm_scheme"] = cs.display or " and ".join(cs.values)

    # An assembly constituency cuts across districts (8 of 56 straddle two), so a
    # district the user never named must not narrow it. Seen 2026-09-25 (CM
    # Elevate Legacy TC-31, "applications mapped to Mairang constituency"):
    # resolution returned district EASTERN WEST KHASI HILLS beside the AC — Mairang
    # is also a block there — and the SQL ANDed it in. Harmless for Mairang, which
    # sits wholly in that district; a silent undercount for a straddling one. A
    # district the user did name (or picked via the ", within the X district only"
    # chip) is in the question text, so the resolver's own scan finds it and it stays.
    if resolved.get("assembly_constituency") and resolved.get("district"):
        _named = scan_dimension(_scan_q, schemes[0] if schemes else "", "district")
        if not (_named and _named.status == "resolved"
                and str(_named.canonical).upper() == str(resolved["district"]).upper()):
            logger.info("AC %r: dropped district %r — not named in the question",
                        resolved["assembly_constituency"], resolved["district"])
            resolved.pop("district", None)
            display.pop("district", None)

    if prior_resolved:
        if not mentions.get("district") and "district" not in resolved and prior_resolved.get("district") \
                and not resolved.get("assembly_constituency"):
            resolved["district"] = prior_resolved["district"]
        if not mentions.get("block") and "block" not in resolved and prior_resolved.get("block"):
            resolved["block"] = prior_resolved["block"]
        if not mentions.get("year") and "year_key" not in resolved and prior_resolved.get("year_key"):
            resolved["year_key"] = prior_resolved["year_key"]
        if not mentions.get("village") and "village_code" not in resolved and prior_resolved.get("village_code"):
            resolved["village_code"] = prior_resolved["village_code"]
        # Focus Plus tranche pin — same fallback as district/block/village/year
        # above. There is no LLM `mentions` signal for tranche_label (it's
        # resolved deterministically, not via mention extraction — see
        # resolve_tranche_label), so the only guard needed is that THIS
        # question's own resolution found nothing: a bare short follow-up
        # ("top 3 only") that never re-says "tranche" would otherwise lose the
        # tranche the previous turn pinned. Only ever carries a real stored
        # label — never a sentinel — so it stays safe to drop straight into
        # the SQL prompt's tranche_label filter (see prompt_builder._entities_block).
        if (schemes == ["Focus Plus"] and "tranche_label" not in resolved
                and prior_resolved.get("tranche_label")):
            resolved["tranche_label"] = prior_resolved["tranche_label"]
        # CM Elevate sub-scheme pin — same fallback as tranche_label above:
        # there is no LLM `mentions` signal for cm_scheme (resolved
        # deterministically, not via mention extraction), so a bare follow-up
        # that never re-names the sub-scheme would otherwise lose it.
        if (schemes == ["CM Elevate"] and "cm_scheme" not in resolved
                and prior_resolved.get("cm_scheme")):
            resolved["cm_scheme"] = prior_resolved["cm_scheme"]
        # Same carry for CM Elevate Legacy — but only a value from ITS catalogue,
        # so a sub-scheme pinned while talking about the applications dataset
        # cannot leak a literal this view does not store.
        if (schemes == ["CM Elevate Legacy"] and "cm_scheme" not in resolved
                and prior_resolved.get("cm_scheme")):
            _prior = prior_resolved["cm_scheme"]
            _vals = _prior if isinstance(_prior, list) else [_prior]
            _known = set(canonical_names("CM Elevate Legacy", "cm_scheme"))
            if _vals and all(v in _known for v in _vals):
                resolved["cm_scheme"] = _prior

    # PMAY-G: a village named beside its own block / district is the village, not
    # the block (see _pmay_village_beside_block). village_code alone goes to SQL,
    # as for a pinned village chip.
    # The block / district reach `resolved` in whatever slot the extractor picked
    # (block, a one-element block_list, district, a one-element district_list), or
    # not at all ("Baghmara …, RERAPARA block" came back as block BAGHMARA). An
    # explicit ", <X> block, <DISTRICT>" tail in the text is read directly and wins
    # (PMAY-G village-with-block run, 2026-09-28).
    # CM Elevate uses the same step (all-villages run 2026-09-28, KI-106): "RANGSAGRE,
    # Dadenggiri block" put RANGSAGRE in the district slot and returned no record.
    _vb_scheme = schemes[0] if schemes in (["PMAY-G"], ["CM Elevate"]) else None
    # CM Elevate: map the block to its stored spelling FIRST, so "Umrangksai,
    # Nongpoh-town Committee block" matches the villages stored under
    # "NONGPOH-TOWN COMMITTEE" (all-villages run 2026-09-28: every urban-body
    # village lost its village and was answered for the whole body, KI-107).
    if _vb_scheme == "CM Elevate" and (resolved.get("block") or resolved.get("block_list")):
        await _cm_elevate_blocks_to_data(question, resolved, display)
    # CM Elevate: "Mawker, Mawhati block" came back as village_code_list [277780] +
    # block_list ['MAWHATI'] — the comparison shape — and the SQL filtered the block
    # alone (204 applications for a village of 3; final pass 2026-09-28, KI-119).
    # One village plus only its own block is one village.
    if _vb_scheme == "CM Elevate" and len(resolved.get("village_code_list") or []) == 1:
        _code = resolved["village_code_list"][0]
        try:
            _own = next((c for v in (await _cm_elevate_village_names()).values() for c in v
                         if c["village_code"] == _code), None)
        except Exception:  # noqa: BLE001
            _own = None
        _blks = {str(b).upper() for b in (resolved.get("block_list") or [])} | (
            {str(resolved["block"]).upper()} if resolved.get("block") else set())
        if _own and _blks <= {str(_own["block"]).upper()}:
            resolved["village_code"] = _code
            for _k in ("village_code_list", "block_list", "block_list_districts", "block"):
                resolved.pop(_k, None)
            display["village"] = _place_title(str(_own["name"]))
            logger.info("CM Elevate: one village beside its own block — village %s, not a list", _code)
    _pm_vname = None
    if _vb_scheme and resolved.get("village_code") is not None:
        try:
            _pm_vname = next((n for n, cs in (await _scheme_village_names(_vb_scheme)).items()
                              if any(c["village_code"] == resolved["village_code"] for c in cs)), None)
        except Exception:  # noqa: BLE001
            _pm_vname = None
    if (_vb_scheme and not resolved.get("village_code_list")
            and (resolved.get("village_code") is None or _pm_vname)
            and len(set(resolved.get("block_list") or [])) <= 1 and len(set(resolved.get("district_list") or [])) <= 1
            and not resolved.get("district_list_region")):
        _blk = resolved.get("block") or (resolved.get("block_list") or [None])[0]
        _dst = resolved.get("district") or (resolved.get("district_list") or [None])[0]
        # ", <X> block[, <DISTRICT>]" anywhere in the text (a chip tail ends the
        # question; a typed "baghmara, Rerapara block across all financial years"
        # does not). X must be a PMAY-G block name.
        _pm_blocks = {str(c).strip().upper() for c in canonical_names(_vb_scheme, "block")}
        if _vb_scheme == "CM Elevate":       # the stored spellings, urban bodies included
            try:
                _pm_blocks |= set(await _cm_elevate_block_names())
            except Exception:  # noqa: BLE001
                pass
        _pm_dists = {str(c).strip().upper() for c in canonical_names(_vb_scheme, "district")}
        for _tm in re.finditer(r"(?:,|\bin|\bof)\s*([A-Za-z][A-Za-z .()-]*?)\s+block\b(?:\s*,\s*([A-Za-z][A-Za-z ]*?)(?=\s*(?:[,?.]|$|\s+across\b|\s+during\b|\s+for\b|\s+till\b|\s+in\b)))?",
                               question or "", re.IGNORECASE):
            _tb = re.sub(r"\s+", " ", _tm.group(1)).strip().upper()
            _tb = next((b for b in _pm_blocks if _tb == b or _tb.endswith(" " + b)), None)
            if _tb:
                _blk = _tb
                _td = re.sub(r"\s+", " ", _tm.group(2) or "").strip().upper()
                _dst = _td if _td in _pm_dists else (_dst if _blk == resolved.get("block") else None)
        _vb = (await _pmay_village_beside_block(question, {"block": _blk, "district": _dst}, _vb_scheme)
               if (_blk or _dst) else None)
        # A village already resolved stays, unless it is only the stated block's
        # namesake ("model village, Umling block" resolved to the village UMLING and
        # answered for it — PMAY-G scenario run 2026-09-28).
        if _vb and resolved.get("village_code") is not None and not (
                _pm_vname and _blk and _pm_vname == _blk and _vb["village_code"] != resolved["village_code"]):
            _vb = None
        if _vb:
            logger.info("%s village %r named beside %s — the village, not the area", _vb_scheme, _vb["name"],
                        _blk or _dst)
            resolved["village_code"] = _vb["village_code"]
            for _k in ("block", "block_list", "block_list_districts", "district", "district_list"):
                resolved.pop(_k, None)
            display["village"] = _place_title(str(_vb["name"]))
            display["block"] = str(_vb.get("block") or "").upper()
            _areas = {str(x).upper() for x in (_blk, _dst) if x}
            notes = [n for n in notes if not (
                (m := re.match(r"^'([^']+)' is not a known", n or "")) and m.group(1).strip().upper() in _areas)]

    # A one-village list beside a block is not a finished comparison: "PECHUA or
    # LASKEIN, the village, not the block" resolved village_code_list [PECHUA] +
    # block_list [LASKEIN] — the reading the user's chip had just REJECTED — and
    # every SQL attempt failed (PMAY-G officer cases run 2026-10-02, OFF-022).
    _chose_village = bool(re.search(r",\s*the village, not (?:the block|another area type)", question or "",
                                    re.IGNORECASE))
    if (schemes == ["PMAY-G"] and len(resolved.get("village_code_list") or []) < 2
            and (not resolved.get("block_list") or _chose_village)
            and not resolved.get("district_list") and _PMAY_COMPARE_CUE_RE.search(question or "")):
        _two = await _pmay_two_villages(question)
        if _two:
            logger.info("PMAY-G comparison of two villages %s", [c["name"] for c in _two])
            for _k in ("village_code", "block", "block_list", "block_list_districts", "district"):
                resolved.pop(_k, None)
            resolved["village_code_list"] = [c["village_code"] for c in _two]
            display["village"] = " and ".join(_place_title(str(c["name"])) for c in _two)

    if schemes == ["CM Elevate"] and (resolved.get("block") or resolved.get("block_list")):
        await _cm_elevate_blocks_to_data(question, resolved, display)
    if schemes == ["CM Elevate Legacy"] and (resolved.get("block") or resolved.get("block_list")):
        await _cm_elevate_blocks_to_data(question, resolved, display, "CM Elevate Legacy")
    if schemes == ["Focus Plus"] and (resolved.get("block") or resolved.get("block_list")):
        await _cm_elevate_blocks_to_data(question, resolved, display, "Focus Plus")

    out = {"resolved": resolved, "notes": notes, "display": display}
    if _year_gap_note:
        # _apply_year_gap rewrote the question (the absent year stripped, or
        # swapped for the nearest year with data on a comparison). SQL must be
        # generated from THAT text: from the original, the generator filtered
        # on the absent year and the note above then contradicted the result
        # (TC-24, 2026-09-25: "2024-25: 114,990,000" with no 2023-24 mention).
        out["question"] = question
    return out


_FOCUSPLUS_BENEFICIARY_QUALIFIER = re.compile(
    r"\b(wom[ae]n|female|male|gender|farmer|occupation|status|pending|approved|"
    r"rejected|verif\w*|tranch\w*|instal{1,2}ments?|batch\w*|legacy|93k|12\.5k|"
    r"average|mean|\bper\b|percentage|per ?cent|compare\w*|\bvs\.?\b|versus|each|"
    r"every|wise|breakdown|split|distribution|top\s*\d|highest|lowest|rank\w*)\b",
    re.IGNORECASE,
)


def _focusplus_single_district_beneficiary_guard(
        question: str, schemes: list[str], entity_result: dict, sql: str) -> str:
    """User-directed exception (2026-09-13): "how many Focus Plus beneficiaries
    in <one district>" must answer with ONE number, not the three-reading
    ambiguity table (FOCUS PLUS RULES rule 6). The prose exception added to
    schema_context.py doesn't reliably win against the concrete multi-column
    worked example under LLM sampling — confirmed live, the generator kept
    emitting the 3/4-column shape even with the exception text in-prompt and
    the correctly-ranked single-column few-shot example alongside it.
    Deterministic rewrite instead, scoped narrowly so every OTHER beneficiary
    shape (statewide, "each district", a comparison, a gender/status/tranche/
    batch-scoped question, or one with a specific year or tranche pinned) is
    left completely alone — those still need their own, different SQL.

    Updated 2026-09-13 (client UAT sheet, FOCUS-001): the original version of
    this template used `COUNT(DISTINCT source_sl_no) WHERE batch_label = '93K'
    AND NOT has_geo_conflict`, written before curated.fact_focus_plus_disbursement
    grew the generated beneficiary_key column (batch_label || ':' || source_sl_no,
    see semantic.column_catalog). That template was reproduced live against
    megh_db and silently dropped every real beneficiary it wasn't built to see:
    the entire 12.5K cohort (4,670 people in West Garo Hills alone — batch_label
    = '93K' excludes them outright) and every has_geo_conflict row (136 more in
    West Garo Hills) even though has_geo_conflict only means the source's block
    name disagreed with the LGD roster for that village — the roster's district
    is what v_focus_plus.lgd_district stores either way, so those rows belong in
    the district count as much as any other row. beneficiary_key already spans
    both cohorts as one identifier space, so neither filter is needed any more."""
    if schemes != ["Focus Plus"]:
        return sql
    if not re.search(r"beneficiar\w*", question, re.IGNORECASE):
        return sql
    resolved = entity_result.get("resolved", {})
    district = resolved.get("district")
    if not district or resolved.get("district_list"):
        return sql
    if resolved.get("block") or resolved.get("village_code"):
        return sql
    if resolved.get("year_key") or resolved.get("tranche_label"):
        return sql
    if _FOCUSPLUS_BENEFICIARY_QUALIFIER.search(question):
        return sql
    return (
        "SELECT COUNT(DISTINCT beneficiary_key) AS beneficiaries\n"
        "FROM curated.v_focus_plus\n"
        f"WHERE lgd_district = '{district}';"
    )


async def generate_sql(question: str, schemes: list[str], entity_result: dict) -> str:
    # The whole prompt — hand-written backbone + live schema + SME catalog +
    # prohibited joins + few-shot + resolved entities — is assembled in one place.
    prompt = prompt_builder.build_sql_prompt(question, schemes, entity_result)
    raw = await llm.call_sql_generator(prompt, guided={"guided_regex": _SQL_SHAPE_REGEX})
    sql = _extract_sql(raw)
    return _focusplus_single_district_beneficiary_guard(question, schemes, entity_result, sql)


# The generator sometimes reads "in Meghalaya" as a place filter and invents a
# WHERE on a state pseudo-row that no fact row matches — the query then runs
# clean but counts 0. Catch that shape and force one repair pass (the repair
# prompt carries the "whole dataset is Meghalaya" rule, so it drops the filter).
# An assembly constituency cuts ACROSS blocks and districts, so its name is
# usually not a block/district name at all. When only the constituency was
# resolved, the generator still reaches for `lgd_block = '<same name>'` next to
# the AC filter — two conditions that can never both hold, giving a clean,
# confident ZERO (confirmed live 2026-09-17: "which villages in Rangsakona
# received employment in FY 2025-26" answered "no matching records"; RANGSAKONA
# spans the BETASING, RERAPARA and RONGRAM blocks and the real answer is 146
# villages). The prompt now warns against it (prompt_builder._entities_block),
# but a prose rule doesn't reliably win under sampling — this catches the shape
# deterministically, the same way the village_code guards above do.
_AC_FILTER_RE = re.compile(r"\bassembly_constituency_name\b", re.IGNORECASE)
# Matches the filter in every shape the generator writes it: a bare column, a
# table-qualified one (dg.lgd_block), and either side wrapped in UPPER().
_GEO_EQ_LITERAL_RE = re.compile(
    r"(?:\w+\.)?\blgd_(?P<col>district|block)\b\s*\)?\s*(?:=|ILIKE)\s*"
    r"(?:UPPER\s*\(\s*)?'(?P<val>[^']+)'",
    re.IGNORECASE,
)


def _ac_with_invented_geo_filter(entity_result: dict, sql: str) -> "str | None":
    """The geography literal a query added alongside an assembly-constituency
    filter that entity resolution never produced, or None.

    Only fires when the resolved entities carry an assembly_constituency and NO
    district/block of their own — i.e. the filter cannot have come from
    resolution, so the generator invented it from the question text."""
    resolved = entity_result.get("resolved") or {}
    if not resolved.get("assembly_constituency"):
        return None
    if resolved.get("district") or resolved.get("block") \
            or resolved.get("district_list") or resolved.get("block_list"):
        return None
    if not _AC_FILTER_RE.search(sql or ""):
        return None
    m = _GEO_EQ_LITERAL_RE.search(sql or "")
    return f"lgd_{m.group('col')} = '{m.group('val')}'" if m else None


# MGNREGA's two facts have NO shared grain — both are at source-row level, many
# rows per village-year on each side. Joining them directly (however sensible
# the ON clause looks: year_key + lgd_block, or geography_key) is a cartesian
# fan-out: every expenditure row pairs with every employment row for the same
# area, and the SUMs come back multiplied. schema_context MGNREGA rule 1 has
# always forbidden it, but the rule alone does not hold under sampling —
# confirmed live 2026-09-17, "compare expenditure and person-days in RERAPARA":
# the generator joined v_expenditure to v_employment and returned ₹367,647 lakh
# / 109,448,220 person-days against a truth of ₹2,450.98 lakh / 986,020. That is
# far more dangerous than the missing-column error it replaced, because it runs
# clean and the numbers look plausible. The correct shape is one CTE per fact,
# each aggregated independently, then combined.
_MGNREGA_FACT_OBJECTS = (
    "v_employment", "fact_mgnrega_employment",
    "v_expenditure", "fact_mgnrega_expenditure",
)
_EMPLOYMENT_OBJ_RE = re.compile(r"\b(?:curated\.)?(?:v_employment|fact_mgnrega_employment)\b",
                                re.IGNORECASE)
_EXPENDITURE_OBJ_RE = re.compile(r"\b(?:curated\.)?(?:v_expenditure|fact_mgnrega_expenditure)\b",
                                 re.IGNORECASE)
_JOIN_RE = re.compile(r"\bJOIN\b", re.IGNORECASE)
_CTE_RE = re.compile(r"^\s*WITH\b", re.IGNORECASE)


# ── MGNREGA use-case fixes (QA 2026-09-26, KI-041 … KI-048) ─────────────────
# Every MGNREGA employment measure is a BIGINT, and Postgres divides two
# integers as integers: ROUND(SUM(person_days) / NULLIF(SUM(households_employed),
# 0), 2) returned 38.00 for East Khasi Hills where the true figure is
# 1,632,188 / 42,770 = 38.16 (DATA-021, KI-044). The prompt rule (schema_context
# MGNREGA rule 8) asks for a cast; this rewrite guarantees it.
_MGNREGA_INT_MEASURES = (
    "person_days", "households_employed", "persons_employed",
    "households_completed_100_days", "job_cards_issued_total", "women_employment_provided",
)
_MGNREGA_INT_DIVISION_RE = re.compile(
    r"(SUM\s*\(\s*(?:[A-Za-z_][A-Za-z0-9_]*\.)?(?:" + "|".join(_MGNREGA_INT_MEASURES)
    + r")\s*\))(?!\s*::)(\s*/)", re.IGNORECASE)


def _mgnrega_numeric_division(schemes: list[str], sql: str) -> str:
    """Cast the numerator of SUM(<integer measure>) / … to numeric (MGNREGA only)."""
    if "MGNREGA" not in schemes or not sql:
        return sql
    return _MGNREGA_INT_DIVISION_RE.sub(r"\1::numeric\2", sql)


# MGNREGA money is natively LAKH. "ROUND(SUM(unskilled_wage_exp + semi_skilled_
# wage_exp) / 100.0, 2) AS wages_lakh" divided a lakh figure by 100 and still
# called it lakh: JAKREM's 219.48 lakh of wages was reported as "2.19 lakh"
# (MGNREGA QA 2026-09-26, DATA-023 run 2). _crore_conversion_for_single_village
# only fires when the SQL says "crore", so a ÷100 under a *_lakh alias slipped
# through. The alias states the unit the composer will print, so the ÷100 is
# the error — drop it.
_MGNREGA_MONEY_COL = (r"(?:total_exp|unskilled_wage_exp|semi_skilled_wage_exp|material_exp|"
                      r"tax_exp|admin_total_exp)")
_MGNREGA_LAKH_DIV100_RE = re.compile(
    r"(SUM\s*\([^()]*\b" + _MGNREGA_MONEY_COL + r"\b[^()]*\))\s*/\s*100(?:\.0+)?\b"
    r"(?=(?:\s*,\s*\d+\s*\))?\s*(?:::\s*\w+\s*)?\s*AS\s+\w*lakh)", re.IGNORECASE)


def _mgnrega_lakh_not_divided(schemes: list[str], sql: str) -> str:
    """Remove a ÷100 applied to MGNREGA money whose alias still says lakh."""
    if "MGNREGA" not in schemes or not sql:
        return sql
    return _MGNREGA_LAKH_DIV100_RE.sub(r"\1", sql)


# "Which villages in NONGKREM received employment" was answered with four
# village CODES and no names — and all four were counted although three had
# zero employment (DATA-014, KI-045).
_VILLAGE_LIST_ASK_RE = re.compile(
    r"\bwhich\s+villages\b|\blist\s+(?:of\s+|all\s+)?(?:the\s+)?villages\b|"
    r"\bname\s+(?:the|all)\s+villages\b|\bwhat\s+villages\b", re.IGNORECASE)
_RECEIVED_EMPLOYMENT_RE = re.compile(
    r"\b(?:receiv\w*|got|get|provided|given)\s+(?:any\s+)?employment\b|\bwere\s+employed\b",
    re.IGNORECASE)


def _mgnrega_village_list_issue(question: str, schemes: list[str], sql: str) -> "str | None":
    """Repair instruction for a MGNREGA "which villages…" query that lists no
    village names, or that keeps zero-employment villages; else None."""
    if schemes != ["MGNREGA"] or not _VILLAGE_LIST_ASK_RE.search(question or ""):
        return None
    if not re.search(r"\blgd_village_name\b", sql or "", re.IGNORECASE):
        return ("this question asks WHICH villages, but the query returns no village names. "
                "Select lgd_village_name AND village_code, GROUP BY both, include the metric "
                "(e.g. SUM(persons_employed) AS persons_employed), and ORDER BY it DESC. Keep "
                "every filter exactly as it was.")
    if _RECEIVED_EMPLOYMENT_RE.search(question) and not re.search(
            r"\bHAVING\b|>\s*0\b", sql or "", re.IGNORECASE):
        return ("this question asks which villages RECEIVED employment, but the query also "
                "lists villages whose employment is zero (they have rows with 0 persons). Add "
                "HAVING SUM(persons_employed) > 0 after the GROUP BY. Keep every other clause "
                "exactly as it was.")
    return None


# "Was more spent on wages or materials in JAKREM?" returned only a CASE label
# ('wages'), so the answer had no amounts and even claimed none were available
# (DATA-023, KI-047). A comparison must return the figures it compares.
_QUANTITY_COMPARISON_RE = re.compile(
    r"\b(?:more|less|higher|lower|greater|bigger|smaller)\b.{0,60}?\b(?:or|than)\b|"
    r"\bcompar\w*\b|\bvs\.?\b|\bversus\b", re.IGNORECASE)


def _mgnrega_comparison_without_figures(question: str, schemes: list[str],
                                        rows: list[dict]) -> bool:
    """True when a MGNREGA comparison question's result holds no number at all."""
    if schemes != ["MGNREGA"] or not rows or not _QUANTITY_COMPARISON_RE.search(question or ""):
        return False
    return not any(_as_number(v) is not None for r in rows for v in r.values())


# ── MGNREGA deterministic queries ───────────────────────────────────────────
# "How much was spent and how many person-days in JAKREM" needs one CTE per
# fact (rule 1). The 30B model kept writing the join; the join guard fired; the
# 4B verifier then flagged the correct two-CTE rewrite as a "prohibited join";
# the next repair moved to v_district_year_summary, which has no village or
# block column — and after three repairs the question fell back to the
# reference documents ("no data for the JAKREM scheme", DATA-028) or said
# "couldn't build a working query" (DATA-029, 4 of 5 runs). The query's shape is
# fixed, so it is built here from the resolved filters and executed
# parameter-bound — no model call, no verifier.
_MG_SPEND_WORD_RE = re.compile(r"\b(?:spent|spend\w*|expenditure|expenses?|costs?)\b", re.IGNORECASE)
_MG_EMPLOYMENT_WORD_RE = re.compile(
    r"\b(?:person[\s-]?days?|man[\s-]?days?|employment|households?\s+employed|"
    r"persons?\s+employed)\b", re.IGNORECASE)
# Anything that needs a different query shape stays with the SQL generator.
_MG_COMBINED_EXCLUDE_RE = re.compile(
    r"\b(?:each|every|per|by|wise|breakdown|trend|top|highest|lowest|most|least|rank\w*|"
    r"which|wages?|materials?|unskilled|semi[\s-]?skilled|tax|administrative|admin|"
    r"average|avg|ratio|percent\w*|share|women|job\s+cards?|100\s+days?)\b|%",
    re.IGNORECASE)
_MG_GEO_FILTERS = (("village_code", "village_code"), ("block", "lgd_block"),
                   ("district", "lgd_district"), ("year_key", "year_key"))


def _mgnrega_filters(resolved: dict, *, allow_ac: bool = False
                     ) -> "tuple[list[str], list[object], list[str]] | None":
    """(parameterised WHERE parts, params, literal WHERE parts for display) from
    the resolved single-valued filters, or None when a filter is multi-valued or
    needs another shape (assembly constituency, lists)."""
    if any(resolved.get(k) for k in ("village_code_list", "block_list", "district_list")):
        return None
    if resolved.get("assembly_constituency") and not allow_ac:
        return None
    where, params, shown = [], [], []
    pairs = list(_MG_GEO_FILTERS)
    if allow_ac:
        pairs.append(("assembly_constituency", "assembly_constituency_name"))
    for key, col in pairs:
        v = resolved.get(key)
        if v is None or v == "":
            continue
        if isinstance(v, (list, tuple, dict, set)):
            return None
        params.append(v)
        if isinstance(v, str):
            where.append(f"UPPER({col}) = UPPER(${len(params)})")
            shown.append(f"UPPER({col}) = UPPER('{v.replace(chr(39), chr(39) * 2)}')")
        else:
            where.append(f"{col} = ${len(params)}")
            shown.append(f"{col} = {int(v)}")
    return where, params, shown


def _mgnrega_combined_facts_query(question: str, schemes: list[str], resolved: dict
                                  ) -> "tuple[str, list, str] | None":
    """(parameterised SQL, params, display SQL) for a single-area MGNREGA
    "expenditure AND person-days/employment" question, else None."""
    q = question or ""
    if schemes != ["MGNREGA"] or not (_MG_SPEND_WORD_RE.search(q) and _MG_EMPLOYMENT_WORD_RE.search(q)):
        return None
    if _MG_COMBINED_EXCLUDE_RE.search(q):
        return None
    f = _mgnrega_filters(resolved)
    if f is None:
        return None
    where, params, shown = f
    emp_cols = ["SUM(person_days) AS person_days"]
    if re.search(r"\bhouseholds?\b", q, re.IGNORECASE):
        emp_cols.append("SUM(households_employed) AS households_employed")
    if re.search(r"\b(?:persons?|people|workers?)\b(?![\s-]*days?\b)", q, re.IGNORECASE):
        emp_cols.append("SUM(persons_employed) AS persons_employed")
    emp_names = [c.rsplit(" AS ", 1)[1] for c in emp_cols]

    def build(w: list[str]) -> str:
        wh = (" WHERE " + " AND ".join(w)) if w else ""
        return ("WITH exp AS (SELECT ROUND(SUM(total_exp)::numeric, 2) AS total_expenditure_lakh "
                f"FROM curated.v_expenditure{wh}),\n"
                f"     emp AS (SELECT {', '.join(emp_cols)} FROM curated.v_employment{wh})\n"
                f"SELECT exp.total_expenditure_lakh, {', '.join('emp.' + n for n in emp_names)}\n"
                "FROM exp CROSS JOIN emp")
    return build(where), params, build(shown)


def _mgnrega_admin_expenditure_query(question: str, schemes: list[str], resolved: dict
                                     ) -> "tuple[str, list, str] | None":
    """(parameterised SQL, params, display SQL) for a MGNREGA administrative-
    expenditure question (DATA-013, KI-046), else None."""
    if schemes != ["MGNREGA"] or not _ADMIN_EXPENDITURE_REQUESTED.search(question or ""):
        return None
    f = _mgnrega_filters(resolved)
    if f is None:
        return None
    where, params, shown = f

    def build(w: list[str]) -> str:
        wh = (" WHERE " + " AND ".join(w)) if w else ""
        return ("SELECT ROUND(SUM(admin_total_exp)::numeric, 2) AS admin_expenditure_lakh, "
                f"COUNT(*) AS source_rows FROM curated.v_expenditure{wh}")
    return build(where), params, build(shown)


# ---------------------------------------------------------------------------
# PMAY-G deterministic facts (PMAY-G use-case QA 2026-09-28, KI-089..KI-095).
#
# The use-case sheet's PMAY-G questions are fixed shapes over ONE view
# (curated.v_pmay, one row = one house): counts, money, stage breakdowns,
# release status, rates, summaries and A-vs-B comparisons for a village / block /
# district / the state, optionally in one FY or on one date. Put to the 30B, 11
# of 28 use cases failed on sampling alone: "financial summary" grouped by year
# and reported a sum of ROUNDED yearly crore as "sanctioned credit released"
# (012), comparisons came back as per-year row dumps or invented statistics
# ("38,899 for both districts", 023b/024b), utilisation was AVG(released) /
# AVG(sanctioned) (016), "which has more" got LIMIT 1 (022b), the remaining
# amount / difference / sanctioned amount were dropped (012/016/023/024/025) and
# a village's ₹9,40,000 read "0.09 crore" (007). For these shapes the query and
# the wording are both built here, parameter-bound, from the resolved filters —
# the same approach as _mgnrega_combined_facts_query. Anything else (rankings,
# "each district", trends, allotment, instalments, averages, names) is left to
# the model path, which has its own guards (_pmay_sql_issue / _pmay_comparison_limit).

_PMAY_DET_KEYS = {"village_code", "village_code_list", "block", "block_list", "block_list_districts",
                  "district", "district_list", "district_list_region", "year_key", "house_status"}
# Shapes the deterministic path does not answer (the model path keeps them).
_PMAY_DET_EXCLUDE_RE = re.compile(
    r"\b(?:each|every|per\s+(?:district|block|village|year|month|house|beneficiary)|"
    r"by\s+(?:district|block|village|year|month|financial\s+year|fy|date|gender|allotment)|"
    r"(?:district|block|village|year|month|date|gender)[\s-]?wise|monthly|month|months|weekly|quarter\w*|"
    r"trend\w*|over\s+time|growth|increase\w*|decrease\w*|declin\w*|change[sd]?|"
    r"top|bottom|rank\w*|list|lists|listing|name\s+(?:the|all)|names|"
    r"average|avg|mean|median|typical|"
    r"wom[ae]n|female|male|widow\w*|husband|wife|joint|allot\w*|gender|"
    r"instal+ments?|tranches?|reg(?:istration)?\s*(?:no|number)s?|reg_no|"
    r"mapping|mapped|fuzzy|placeholders?|data\s+quality|missing|null|duplicate\w*|"
    r"(?:between|from|since|before|after|until)\s+(?:the\s+)?(?:fy\s*)?(?:(?:19|20)\d\d|\d{1,2}[/.-]|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)|calendar\s+year|"
    r"in\s+progress|ongoing|under\s+construction|"
    r"mgnrega|focus|elevate|scheme[s]?\s+(?:compare|vs)|cross[\s-]?scheme)\b",
    re.IGNORECASE)
# Ranking words: a ranking over every area is the model path's; between the
# entities the question names ("which has more: A or B") it is a comparison.
# Counting or listing areas themselves ("how many villages …") — the model path's.
_PMAY_AREA_PLURAL_RE = re.compile(r"\b(?:villages|blocks|districts)\b", re.IGNORECASE)
_PMAY_RANK_RE = re.compile(r"\b(?:which|highest|lowest|most|least|maximum|minimum|max|min|best|worst)\b",
                           re.IGNORECASE)
_PMAY_M = {
    "fin_summary": re.compile(
        r"\bfinancial\s+(?:summary|position|status|overview|details?|picture|progress|report)|"
        r"\bfunds?\s+(?:summary|status|position|flow)|\bmoney\s+summary|\bfinance\s+summary", re.I),
    "performance": re.compile(
        r"\bperformance\b|\bperforming\b|\boverall\s+(?:status|progress|picture|position|summary|figures)|"
        r"\bprogress\s+(?:report|summary)|\bsummary\b|\boverview\b|\bstatus\s+report|"
        r"\bkey\s+(?:figures|numbers|indicators|statistics)|\bhow\s+(?:is|are)\b.{0,60}\bdoing\b", re.I),
    "status": re.compile(
        r"\bstatus[\s-]*(?:breakdown|wise|distribution|split|summary|position)|"
        r"\bbreak[\s-]*down\b.{0,40}\b(?:status|stages?)|\bhouse\s+status\b|"
        r"\bconstruction\s+(?:stages?|status)|\bstages?[\s-]*wise|\bstages?\s+of\s+construction|"
        r"\b(?:breakdown|split|distribution)\s+(?:by|across)\s+(?:stage|status)|"
        r"\bhouses?\s+(?:by|at\s+each)\s+(?:stage|status)|\bstage\s+breakdown", re.I),
    "full_not_done": re.compile(
        r"(?:\bfull\w*\b|\bentire\b|\bwhole\b|\bcomplete\s+(?:sanctioned\s+)?amount).{0,80}"
        r"(?:not\s+(?:yet\s+)?(?:been\s+)?complet|incomplete|still\s+not|unfinished|not\s+finished)",
        re.I),
    "none": re.compile(
        r"\bnot\s+(?:yet\s+)?received\s+any|\b(?:received|got|been\s+(?:paid|released))\s+"
        r"(?:nothing|no\s+(?:amount|money|funds?|payment|instal\w*))|"
        r"\bno\s+(?:amount|money|funds?|payment|release)s?\s+(?:has\s+|have\s+)?(?:yet\s+)?(?:been\s+)?(?:received|released|paid)|"
        r"\bnothing\s+(?:has\s+been\s+|was\s+|yet\s+)?(?:released|received|paid)|\bzero\s+(?:amount|release|payment)|"
        r"\bnot\s+(?:been\s+)?(?:paid|released)\s+(?:any|anything)|\bwithout\s+(?:any\s+)?(?:release|payment|money)|"
        r"\bunpaid\b", re.I),
    "part": re.compile(
        r"\bpart(?:ial|ially|ly)?\b.{0,40}\b(?:amount|sanction\w*|releas\w*|paid|payment)|\bonly\s+(?:a\s+)?part\b|"
        r"\bnot\s+(?:the\s+)?full\s+(?:amount|release|payment)|\bsome\s+but\s+not\s+all", re.I),
    "full": re.compile(
        r"\bfull(?:y)?\b.{0,40}\b(?:amount|sanction\w*|releas\w*|paid|payment|funded)|"
        r"\b(?:entire|whole)\s+(?:sanctioned\s+)?amount|\breceived\s+(?:their|the)\s+(?:full|entire|whole|complete)\b|"
        r"\b100\s*%\s*(?:of\s+)?(?:the\s+)?(?:amount\s+)?releas", re.I),
    "sanction_numbers": re.compile(
        r"\bsanction\s+(?:numbers?|nos?\.?|orders?)\b|\b(?:unique|distinct)\s+sanctions?\b", re.I),
    "completion_pct": re.compile(
        r"\b(?:percent\w*|%|share|proportion|ratio|rate)\b.{0,60}\bcomplet\w*|\bcomplet\w*\b.{0,40}"
        r"(?:\bpercent\w*|%|\brate\b|\bratio\b|\bshare\b)", re.I),
    "release_pct": re.compile(
        r"\b(?:percent\w*|%|share|proportion|ratio|rate)\b.{0,60}\b(?:releas\w*|disburs\w*|utili[sz]\w*|paid)|"
        r"\b(?:releas\w*|disburs\w*)\b.{0,40}(?:\bpercent\w*|%)|\butili[sz]ation\b|\butili[sz]ed\b", re.I),
    "remaining": re.compile(
        r"\b(?:still|yet)\s+to\s+be\s+(?:released|paid|disbursed)|\bto\s+be\s+released|\bremaining\b|\bbalance\b|"
        r"\bgap\b|\boutstanding\b|\bunreleased\b|\bnot\s+(?:yet\s+)?(?:been\s+)?released\b|"
        r"\bpending\s+(?:amount|release|money|funds?|payment)|\bamount\s+pending\b|\bstill\s+(?:due|pending)\b|"
        r"\bleft\s+to\s+(?:be\s+)?releas", re.I),
    "released": re.compile(
        r"\breleas\w*|\bdisburs\w*|\bpaid\s+out\b|\bspent\b|\bspending\b|\bexpenditure\b|\bamount\s+paid\b|\boutlay\b", re.I),
    "sanctioned": re.compile(
        r"\b(?:amounts?|money|funds?|rupees|value)\b.{0,40}\bsanction\w*|\bsanction\w*\s+(?:amounts?|money|funds?|value)\b|"
        r"\bhow\s+much\b.{0,40}\bsanction\w*|₹.{0,20}sanction", re.I),
    "incomplete": re.compile(
        r"\bincomplete\b|\bnot\s+(?:yet\s+)?(?:been\s+)?complet\w*|\bpending\s+houses?\b|\bunfinished\b|"
        r"\bnot\s+finished\b", re.I),
    "completed": re.compile(r"\bcomplet(?:ed|e|ion)\b|\bfinished\b|\bbuilt\b", re.I),
    "houses": re.compile(
        r"\b(?:how\s+many|number\s+of|no\.?\s+of|count\s+of|total\s+number|count)\b.{0,60}"
        r"\b(?:beneficiar\w*|houses?|homes?|households?|dwellings?)\b|"
        r"\b(?:beneficiar\w*|houses?)\b.{0,30}\b(?:are\s+there|count)\b", re.I),
    "compare": re.compile(r"\bcompar\w*|\bversus\b|\bvs\.?\b|\bdifference\b|\bagainst\b", re.I),
}
_PMAY_STAGE_ORDER = ["Completed", "Roof Cast", "Plinth", "House Sanctioned", "Existing site(Old House)",
                     "Proposed Site"]
_PMAY_STAGE_COL = {"Completed": "st_completed", "Roof Cast": "st_roof_cast", "Plinth": "st_plinth",
                   "House Sanctioned": "st_house_sanctioned", "Existing site(Old House)": "st_existing_site",
                   "Proposed Site": "st_proposed_site"}
_PMAY_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _pmay_date(question: str):
    """The single calendar date a question names (ISO, dd/mm/yyyy or written), else None."""
    import datetime as _dt
    m = _CALENDAR_DATE_RE.search(question or "")
    if not m:
        return None
    t = m.group(0).lower()
    try:
        g = re.fullmatch(r"((?:19|20|21)\d\d)[-/.](\d{1,2})[-/.](\d{1,2})", t)
        if g:
            return _dt.date(int(g.group(1)), int(g.group(2)), int(g.group(3)))
        g = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.]((?:19|20|21)\d\d)", t)
        if g:   # Indian convention: day first
            return _dt.date(int(g.group(3)), int(g.group(2)), int(g.group(1)))
        g = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]{3})[a-z]*\.?,?\s+(\d{4})", t)
        if g:
            return _dt.date(int(g.group(3)), _PMAY_MONTHS[g.group(2)], int(g.group(1)))
        g = re.search(r"([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", t)
        if g:
            return _dt.date(int(g.group(3)), _PMAY_MONTHS[g.group(1)], int(g.group(2)))
    except (ValueError, KeyError):
        return None
    return None


def _pmay_metrics(question: str, list_scope: bool) -> "list[str] | None":
    """The PMAY-G facts a question asks for, in answer order, or None when the
    question is not one of the fixed shapes this path answers."""
    q = question or ""
    # The year-pause chip appends "across all financial years"; it scopes, it asks nothing.
    q = re.sub(r"\b(?:across|for|over)\s+all\s+(?:the\s+)?(?:financial\s+|fiscal\s+)?years(?:\s+combined)?\b",
               " ", q, flags=re.I)
    if _PMAY_DET_EXCLUDE_RE.search(q):
        return None
    if not list_scope and (_PMAY_RANK_RE.search(q) or _PMAY_AREA_PLURAL_RE.search(q)):
        return None
    M = {k: bool(rx.search(q)) for k, rx in _PMAY_M.items()}
    out: list[str] = []
    if M["fin_summary"]:
        # "financial summary" = sanctioned, released, remaining (PMAY-OFF-012/016). The
        # house count and release % were extra lines nobody asked for (user, 2026-09-28).
        return ["sanctioned", "released", "remaining"]
    if M["performance"] and not M["status"]:
        # "overall performance" = beneficiaries, sanctioned, released, completed (PMAY-OFF-019/024)
        return ["houses", "sanctioned", "released", "completed"]
    if M["status"]:
        out.append("status")
    release_counts = False
    if M["full_not_done"]:
        out.append("full_not_done"); release_counts = True
    else:
        if M["none"]:
            out.append("none"); release_counts = True
        if M["part"]:
            out.append("part"); release_counts = True
        if M["full"] and not M["part"]:
            out.append("full"); release_counts = True
    if M["sanction_numbers"]:
        out.append("sanction_numbers")
    if M["completion_pct"]:
        out.append("completion_pct")
    if M["release_pct"]:
        out.append("release_pct")
    if not release_counts and not M["release_pct"]:
        if M["remaining"] and (M["released"] or M["sanctioned"]
                               or re.search(r"\b(?:amount|money|how\s+much|funds?|₹|rupees)\b", q, re.I)):
            out.append("remaining")
        elif M["released"]:
            out.append("released")
        if M["sanctioned"] and "remaining" not in out and "released" not in out:
            out.append("sanctioned")
    if not M["completion_pct"] and "status" not in out and "full_not_done" not in out:
        if M["incomplete"]:
            out.append("incomplete")
        elif M["completed"] and not release_counts:
            out.append("completed")
    if M["houses"] and not any(m in out for m in ("completed", "incomplete", "status", "full_not_done",
                                                  "none", "part", "full", "sanction_numbers")):
        out.insert(0, "houses")
        if "released" in out and "sanctioned" not in out:
            # "How many houses were sanctioned and how much was released" — the
            # use case (025) expects the sanctioned amount beside the release.
            out.insert(out.index("released"), "sanctioned")
    if not out and list_scope and M["compare"]:
        return ["houses", "sanctioned", "released", "completed"]
    return list(dict.fromkeys(out)) or None


# A capitalised word after "in / at / for / of" that is not the scheme or a year:
# a place the resolver did not settle. Never answer that as a statewide figure.
_PMAY_UNSETTLED_PLACE_RE = re.compile(
    r"\b(?:in|at|for|of|under)\s+(?!PMAY|Pmay|FY|Meghalaya|MEGHALAYA|The\b|All\b|Each\b)[A-Z][A-Za-z]")
_PMAY_STATEWIDE_RE = re.compile(r"\b(?:meghalaya|state|statewide|state[\s-]?level|whole|entire|all\s+of)\b", re.I)


# A bare four-digit year ("sanctioned in 2023"), not an FY form ("2023-24", "FY 2023").
_PMAY_BARE_YEAR_RE = re.compile(r"(?<![\d/.-])(20[1-3]\d)(?![\d/.-])")


# The text the user typed this turn, when the follow-up rewrite changed it. "and how many
# received only part of it?" was rewritten to "…have not received any amount … and how many
# received only part of it?" and the answer repeated the previous figure (user, 2026-09-28).
_PMAY_TYPED_TURN: "contextvars.ContextVar[str | None]" = contextvars.ContextVar("_PMAY_TYPED_TURN", default=None)


def _pmay_facts_query(question: str, schemes: list[str], resolved: dict,
                      notes: "list[str] | None" = None, calendar_year: "int | None" = None) -> "dict | None":
    """Parameter-bound query + answer spec for a fixed-shape PMAY-G question, else None.
    `calendar_year` filters sanction_date to that calendar year instead of year_key."""
    if schemes != ["PMAY-G"]:
        return None
    if any(v not in (None, "", [], {}) for k, v in resolved.items() if k not in _PMAY_DET_KEYS):
        return None
    yk = resolved.get("year_key")
    if yk is not None and not isinstance(yk, int):
        return None
    if resolved.get("village_code") is not None:
        dim, ents = "village", [resolved["village_code"]]
    elif resolved.get("village_code_list"):
        dim, ents = "village", list(resolved["village_code_list"])
    elif resolved.get("block"):
        dim, ents = "block", [resolved["block"]]
    elif resolved.get("block_list"):
        dim, ents = "block", list(resolved["block_list"])
    elif resolved.get("district"):
        dim, ents = "district", [resolved["district"]]
    elif resolved.get("district_list"):
        if resolved.get("district_list_region"):
            return None   # "Garo Hills" region expansion: a sum over districts, not a comparison
        dim, ents = "district", list(resolved["district_list"])
    else:
        # statewide only when no place was left unresolved (an unknown village
        # must never be answered with Meghalaya's total)
        if any(re.search(r"not a known|not found|no such|isn.t a known|not in the", n or "", re.I)
               for n in (notes or [])):
            return None
        if not _PMAY_STATEWIDE_RE.search(question or "") and _PMAY_UNSETTLED_PLACE_RE.search(question or ""):
            return None
        dim, ents = "state", ["MEGHALAYA"]
    if any(isinstance(e, (list, dict, set, tuple)) for e in ents) or len(ents) > 6:
        return None
    metrics = _pmay_metrics(question, len(ents) > 1)
    if not metrics:
        return None
    _typed = _PMAY_TYPED_TURN.get()
    if _typed and _typed != question:
        _own = _pmay_metrics(_typed, len(ents) > 1)
        if _own and set(_own) < set(metrics):
            metrics = _own
    day = _pmay_date(question)
    # A year the user typed is never dropped. "How many PMAY houses were sanctioned in
    # 2023?" left year_key unresolved and this path answered for ALL years (170,981;
    # 21 Sep test-sheet recheck, 2026-09-28). A bare year is read as FY 2023-24, the
    # project convention (year_key); the answer adds the calendar-year figure too,
    # because pmay_entity_resolver.yaml reads "sanctioned in 2023" as calendar 2023.
    bare_year = None
    if day is None and calendar_year is None:
        _bm = _PMAY_BARE_YEAR_RE.search(question or "")
        if _bm and not re.search(r"\bfy\s*'?$", (question or "")[:_bm.start()], re.IGNORECASE):
            bare_year = int(_bm.group(1))
        if yk is None:
            yk = _parse_year_key(question or "")
    hs = resolved.get("house_status")
    stages = [hs] if isinstance(hs, str) else list(hs or [])
    if any(s not in _PMAY_STAGE_COL for s in stages):
        return None

    params: list = []
    where = ["NOT is_placeholder"]
    if dim == "village":
        params.append([int(e) for e in ents]); where.append(f"village_code = ANY(${len(params)}::int[])")
        key, name = "village_code", "MAX(lgd_village_name) AS name, MAX(lgd_block) AS block, MAX(lgd_district) AS district"
    elif dim == "block":
        params.append([str(e).upper() for e in ents]); where.append(f"UPPER(lgd_block) = ANY(${len(params)}::text[])")
        key, name = "UPPER(lgd_block)", "MAX(lgd_block) AS name, MAX(lgd_district) AS district"
    elif dim == "district":
        params.append([str(e).upper() for e in ents]); where.append(f"UPPER(lgd_district) = ANY(${len(params)}::text[])")
        key, name = "UPPER(lgd_district)", "MAX(lgd_district) AS name"
    else:
        key, name = "'MEGHALAYA'", "'Meghalaya' AS name"
    if day is not None:
        params.append(day); where.append(f"sanction_date = ${len(params)}")
    elif calendar_year is not None:
        import datetime as _dt
        params.append(_dt.date(calendar_year, 1, 1)); where.append(f"sanction_date >= ${len(params)}")
        params.append(_dt.date(calendar_year + 1, 1, 1)); where.append(f"sanction_date < ${len(params)}")
    elif yk is not None:
        params.append(yk); where.append(f"year_key = ${len(params)}")
    if stages:
        params.append(stages); where.append(f"status_name = ANY(${len(params)}::text[])")
    rel = "COALESCE(amount_released, 0)"
    cols = [
        f"{key} AS entity_key", name,
        "COUNT(*) AS houses",
        "COALESCE(SUM(sanctioned_amount), 0) AS sanctioned",
        f"COALESCE(SUM({rel}), 0) AS released",
        f"COALESCE(SUM({rel}) FILTER (WHERE sanctioned_amount > 0), 0) AS released_on_sanctioned",
        "COALESCE(SUM(sanctioned_amount) FILTER (WHERE sanctioned_amount > 0), 0) AS sanctioned_positive",
        "COUNT(*) FILTER (WHERE is_completed) AS completed",
        "COUNT(*) FILTER (WHERE NOT COALESCE(is_completed, FALSE)) AS incomplete",
        "COUNT(*) FILTER (WHERE status_name IS NULL) AS no_status",
        f"COUNT(*) FILTER (WHERE {rel} >= sanctioned_amount) AS full_release",
        f"COUNT(*) FILTER (WHERE {rel} > 0 AND {rel} < sanctioned_amount) AS part_release",
        f"COUNT(*) FILTER (WHERE {rel} = 0) AS no_release",
        f"COUNT(*) FILTER (WHERE {rel} >= sanctioned_amount AND NOT COALESCE(is_completed, FALSE)) AS full_not_done",
        "COUNT(DISTINCT sanction_no) AS sanction_numbers",
    ]
    for st, c in _PMAY_STAGE_COL.items():
        cols.append(f"COUNT(*) FILTER (WHERE status_name = '{st}') AS {c}")
        cols.append(f"COUNT(*) FILTER (WHERE status_name = '{st}' AND {rel} >= sanctioned_amount) AS fnd_{c}")
    group = "" if dim == "state" else f"\nGROUP BY {key}"
    sql = ("SELECT " + ",\n       ".join(cols) + "\nFROM curated.v_pmay\nWHERE " + "\n  AND ".join(where) + group)
    shown = sql
    for i, p in reversed(list(enumerate(params, 1))):
        lit = (("ARRAY[" + ", ".join(str(x) if isinstance(x, int) else "'" + str(x).replace("'", "''") + "'"
                                     for x in p) + "]") if isinstance(p, list)
               else ("'" + str(p) + "'" if not isinstance(p, int) else str(p)))
        shown = re.sub(rf"\${i}(?:::\w+\[\])?(?!\d)", lit, shown)
    return {"sql": sql, "params": params, "shown": shown, "metrics": metrics, "dim": dim,
            "entities": ents, "year_key": None if calendar_year is not None else yk, "date": day, "stages": stages,
            "calendar_year": calendar_year, "bare_year": bare_year,
            "word": "beneficiaries" if re.search(r"beneficiar", question or "", re.I) else "houses",
            "question": question}


def _pmay_money(v) -> str:
    """₹ with Indian digit grouping, plus lakh / crore — exact, never rounded away
    (KI-095: a village's ₹9,40,000 was printed "0.09 crore")."""
    v = int(round(float(v or 0)))
    s = str(abs(v))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:]); head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    out = ("-" if v < 0 else "") + "₹" + s
    if abs(v) >= 10_000_000:
        return f"{out} (₹{v / 10_000_000:,.2f} crore)"
    if abs(v) >= 100_000:
        return f"{out} (₹{v / 100_000:,.2f} lakh)"
    return out


def _pmay_money_short(v) -> str:
    """₹ in crore / lakh only, for a figure already given exactly elsewhere."""
    v = float(v or 0)
    if abs(v) >= 10_000_000:
        return f"₹{v / 10_000_000:,.2f} crore"
    if abs(v) >= 100_000:
        return f"₹{v / 100_000:,.2f} lakh"
    return _pmay_money(v)


def _pmay_period(spec: dict) -> str:
    if spec.get("date") is not None:
        d = spec["date"]
        return f"on {d.day} {d.strftime('%B %Y')}"
    if spec.get("calendar_year") is not None:
        y = spec["calendar_year"]
        return f"in calendar year {y} (sanction dates 1 January – 31 December {y})"
    if spec.get("year_key") is not None:
        y = spec["year_key"]
        return f"in FY {y}-{(y + 1) % 100:02d}"
    yrs = _SCHEME_DATA_YEARS.get("PMAY-G") or []
    return f"across all financial years (FY {yrs[0]} to FY {yrs[-1]})" if yrs else "across all financial years"


def _pmay_entity_name(spec: dict, row: dict) -> str:
    disp = spec.get("display") or {}
    if not row.get("block") and spec["dim"] == "village" and len(spec["entities"]) == 1:
        row = {**row, "name": row.get("name") if row.get("name") != row.get("entity_key") else None,
               "block": disp.get("block"), "district": disp.get("district")}
        if not row.get("name"):
            row["name"] = disp.get("village") or row.get("entity_key")
        if not row.get("block"):
            _n0 = _place_title(row["name"])
            return _n0 if re.search(r"\bvillage$", _n0, re.IGNORECASE) else f"{_n0} village"
    n = _place_title(row.get("name") or row.get("entity_key") or "")
    if spec["dim"] == "block":
        return f"{n} block"
    if spec["dim"] == "village":
        # "MODEL VILLAGE" is already a village's name — not "Model Village village"
        _vw = "" if re.search(r"\bvillage$", n, re.IGNORECASE) else " village"
        return f"{n}{_vw} ({_place_title(row.get('block') or '')} block, {_place_title(row.get('district') or '')})" \
            if len(spec["entities"]) == 1 else n
    return n


def _pmay_pct(num, den) -> "float | None":
    return (100.0 * float(num) / float(den)) if den else None


def _pmay_stage_split(row: dict, prefix: str = "") -> str:
    parts = [f"{st} {int(row.get(prefix + c) or 0):,}" for st, c in _PMAY_STAGE_COL.items()
             if int(row.get(prefix + c) or 0)]
    return ", ".join(parts)


def _pmay_lines(spec: dict, row: dict) -> list[str]:
    """One line per requested metric for one entity."""
    h = int(row["houses"])
    word = spec["word"]
    L: list[str] = []
    for m in spec["metrics"]:
        if m == "houses":
            L.append(f"PMAY-G {word}: {h:,}"
                     if word == "beneficiaries" or (spec.get("date") is None and spec.get("year_key") is None
                                                    and spec.get("calendar_year") is None)
                     else f"PMAY-G houses sanctioned: {h:,}")
        elif m == "sanctioned":
            L.append(f"Total amount sanctioned: {_pmay_money(row['sanctioned'])}")
        elif m == "released":
            L.append(f"Amount released: {_pmay_money(row['released'])}")
        elif m == "remaining":
            L.append(f"Amount still to be released (sanctioned − released): "
                     f"{_pmay_money(float(row['sanctioned']) - float(row['released']))}")
        elif m == "completed":
            L.append(f"Completed houses: {int(row['completed']):,}")
        elif m == "incomplete":
            split = _pmay_stage_split({k: v for k, v in row.items() if k != "st_completed"})
            ns = int(row["no_status"])
            L.append(f"Houses not yet completed: {int(row['incomplete']):,}")
        elif m == "status":
            ns = int(row["no_status"])
            L.append(f"House status breakdown ({h:,} houses): " + (_pmay_stage_split(row) or "none")
                     + (f"; no status recorded: {ns:,}" if ns else ""))
        elif m == "full":
            L.append(f"Beneficiaries who have received their full sanctioned amount: {int(row['full_release']):,}")
        elif m == "part":
            L.append(f"Beneficiaries who have received only part of their sanctioned amount: "
                     f"{int(row['part_release']):,}")
        elif m == "none":
            L.append(f"Beneficiaries who have not received any amount yet: {int(row['no_release']):,}")
        elif m == "full_not_done":
            n = int(row["full_not_done"])
            split = _pmay_stage_split({k: v for k, v in row.items() if k != "fnd_st_completed"}, "fnd_")
            ns = n - sum(int(row.get("fnd_" + c) or 0) for s, c in _PMAY_STAGE_COL.items() if s != "Completed")
            L.append("Beneficiaries who have received the full sanctioned amount but whose house is not yet "
                     f"completed: {n:,}")
        elif m == "sanction_numbers":
            L.append(f"Unique sanction numbers: {int(row['sanction_numbers']):,}")
        elif m == "completion_pct":
            p = _pmay_pct(row["completed"], h)
            L.append(f"Completion rate: {p:.2f}%" if p is not None else "Completion rate: not available (no houses)")
        elif m == "release_pct":
            p = _pmay_pct(row["released_on_sanctioned"], row["sanctioned_positive"])
            L.append(f"Share of the sanctioned amount released: {p:.2f}%" if p is not None
                     else "Share of the sanctioned amount released: not available (nothing sanctioned)")
    return L


_PMAY_CMP = {  # metric -> (row value, label, is money)
    "houses": (lambda r: int(r["houses"]), "houses", False),
    "sanctioned": (lambda r: float(r["sanctioned"]), "sanctioned amount", True),
    "released": (lambda r: float(r["released"]), "amount released", True),
    "remaining": (lambda r: float(r["sanctioned"]) - float(r["released"]), "amount still to be released", True),
    "completed": (lambda r: int(r["completed"]), "completed houses", False),
    "incomplete": (lambda r: int(r["incomplete"]), "houses not yet completed", False),
    "full": (lambda r: int(r["full_release"]), "beneficiaries fully paid", False),
    "part": (lambda r: int(r["part_release"]), "beneficiaries partly paid", False),
    "none": (lambda r: int(r["no_release"]), "beneficiaries with nothing released", False),
    "full_not_done": (lambda r: int(r["full_not_done"]), "fully paid but incomplete houses", False),
    "sanction_numbers": (lambda r: int(r["sanction_numbers"]), "unique sanction numbers", False),
    "completion_pct": (lambda r: _pmay_pct(r["completed"], r["houses"]), "completion rate", None),
    "release_pct": (lambda r: _pmay_pct(r["released_on_sanctioned"], r["sanctioned_positive"]),
                    "share of sanctioned amount released", None),
}


_PMAY_ROW_COLS = {  # metric -> [(column shown in the result table, value from the working row)]
    "houses": [("houses", lambda r: int(r["houses"]))],
    "sanctioned": [("sanctioned amount (Rs)", lambda r: round(float(r["sanctioned"])))],
    "released": [("amount released (Rs)", lambda r: round(float(r["released"])))],
    "remaining": [("still to be released (Rs)", lambda r: round(float(r["sanctioned"]) - float(r["released"])))],
    "completed": [("completed houses", lambda r: int(r["completed"]))],
    "incomplete": [("houses not yet completed", lambda r: int(r["incomplete"]))],
    "full": [("fully paid beneficiaries", lambda r: int(r["full_release"]))],
    "part": [("partly paid beneficiaries", lambda r: int(r["part_release"]))],
    "none": [("beneficiaries with nothing released", lambda r: int(r["no_release"]))],
    "full_not_done": [("fully paid, house not completed", lambda r: int(r["full_not_done"]))],
    "sanction_numbers": [("unique sanction numbers", lambda r: int(r["sanction_numbers"]))],
    "completion_pct": [("completion rate (%)", lambda r: round(_pmay_pct(r["completed"], r["houses"]) or 0, 2))],
    "release_pct": [("share released (%)", lambda r: round(
        _pmay_pct(r["released_on_sanctioned"], r["sanctioned_positive"]) or 0, 2))],
}


def _pmay_display_rows(spec: dict, rows: list[dict]) -> list[dict]:
    """The result table the UI shows: one row per place with ONLY the figures the
    question asked for. The working row (30 columns: fnd_st_plinth, sanctioned_positive,
    …) was returned as-is and the UI printed every column (user report 2026-09-29)."""
    out = []
    for r in rows:
        d = {spec["dim"] if spec["dim"] != "state" else "area": _place_title(str(r.get("name") or r.get("entity_key")))}
        for m in spec["metrics"]:
            if m == "status":
                for st, c in _PMAY_STAGE_COL.items():
                    if int(r.get(c) or 0):
                        d[st] = int(r[c])
                continue
            for col, fn in _PMAY_ROW_COLS.get(m, []):
                d[col] = fn(r)
        out.append(d)
    return out


def _pmay_facts_answer(spec: dict, rows: list[dict]) -> "str | None":
    """The answer text for _pmay_facts_query's result, or None to fall back to the
    model path (an entity came back with no houses at all)."""
    by_key = {str(r["entity_key"]).upper(): r for r in rows}
    ordered = []
    for e in spec["entities"]:
        r = by_key.get(str(e).upper())
        if r is None or not int(r["houses"]):
            if (spec.get("date") is None and spec.get("year_key") is None and spec.get("calendar_year") is None
                    and not spec.get("stages")):
                return None          # the place holds no PMAY-G rows under this name: let the model path handle it
            r = r or {"entity_key": e, "name": e, "houses": 0, **{k: 0 for k in (
                "sanctioned", "released", "released_on_sanctioned", "sanctioned_positive", "completed", "incomplete",
                "no_status", "full_release", "part_release", "no_release", "full_not_done", "sanction_numbers")},
                **{c: 0 for c in _PMAY_STAGE_COL.values()}, **{"fnd_" + c: 0 for c in _PMAY_STAGE_COL.values()}}
        ordered.append(r)
    period = _pmay_period(spec)
    stage_note = (f" (houses at the {' / '.join(spec['stages'])} stage)" if spec.get("stages") else "")
    if len(ordered) == 1:
        r = ordered[0]
        where = "Meghalaya (statewide)" if spec["dim"] == "state" else _pmay_entity_name(spec, r)
        if not int(r["houses"]):
            return (f"No PMAY-G houses were sanctioned in {where} {period}{stage_note}: 0 houses — there are no "
                    "PMAY-G records for that period, so every figure is zero.")
        lines = _pmay_lines(spec, r)
        if len(lines) == 1:
            label, _, value = lines[0].partition(": ")
            return f"{label} in {where}, {period}{stage_note}: {value}."
        return f"PMAY-G — {where}, {period}{stage_note}:\n" + "\n".join(f"- {ln}" for ln in lines)
    # comparison of the named entities
    names = [_pmay_entity_name(spec, r) for r in ordered]
    out = [f"PMAY-G comparison, {period}{stage_note}:"]
    for n, r in zip(names, ordered):
        out.append(f"{n}:")
        out.extend(f"- {ln}" for ln in _pmay_lines(spec, r))
    diffs = []
    for m in spec["metrics"]:
        if m == "status" or m not in _PMAY_CMP:
            continue
        fn, label, money = _PMAY_CMP[m]
        vals = [fn(r) for r in ordered]
        if any(v is None for v in vals):
            continue
        if len(ordered) == 2:
            a, b = vals
            if a == b:
                diffs.append(f"{label.capitalize()}: {names[0]} and {names[1]} are equal.")
                continue
            hi, lo = (0, 1) if a > b else (1, 0)
            d = abs(a - b)
            ds = (_pmay_money(d) if money else f"{d:.2f} percentage points" if money is None else f"{int(d):,}")
            diffs.append(f"{label.capitalize()}: {names[hi]} is higher by {ds}.")
        else:
            i = max(range(len(vals)), key=lambda k: vals[k])
            v = vals[i]
            vs = (_pmay_money(v) if money else f"{v:.2f}%" if money is None else f"{int(v):,}")
            diffs.append(f"Highest {label}: {names[i]} ({vs}).")
    if diffs:
        out.append("Difference:" if len(ordered) == 2 else "Summary:")
        out.extend(f"- {d}" for d in diffs)
    return "\n".join(out)


# Model-path guards for the PMAY-G shapes the deterministic path leaves alone.
# A question that asks for a series names it; the year chip's "across all
# financial years" is a scope, not a request for a per-year split.
_PMAY_SERIES_ASK_RE = re.compile(
    r"\b(?:year|years|yearly|annual\w*|fy|fys|month\w*|dates?|when|trend\w*|over\s+time|history|historical|"
    r"each|per|wise)\b", re.IGNORECASE)
_PMAY_ALL_YEARS_CHIP_RE = re.compile(
    r"\b(?:across|for|over)\s+all\s+(?:the\s+)?(?:financial\s+|fiscal\s+)?years(?:\s+combined)?\b", re.IGNORECASE)
_PMAY_YEAR_GROUP_RE = re.compile(
    r"\bGROUP\s+BY\b(?:(?!\bORDER\s+BY\b|\bLIMIT\b|\bHAVING\b)[^;])*?"
    r"\b(?:financial_year(?:_short)?|year_key|sanction_month|sanction_date)\b", re.IGNORECASE | re.DOTALL)
_PMAY_AVG_RELEASED_RE = re.compile(r"\bAVG\s*\((?:[^()]|\([^()]*\))*\bamount_released\b", re.IGNORECASE)
_PMAY_RATE_ASK_RE = re.compile(r"percent|%|utili[sz]|\brate\b|\bshare\b|proportion|ratio", re.IGNORECASE)


def _pmay_sql_issue(question: str, schemes: list[str], sql: str) -> "str | None":
    """Repair message for the PMAY-G SQL shapes that produced wrong answers in the
    2026-09-28 use-case QA, else None (KI-089, KI-092)."""
    if schemes != ["PMAY-G"] or not sql:
        return None
    q = _PMAY_ALL_YEARS_CHIP_RE.sub(" ", question or "")
    if _PMAY_YEAR_GROUP_RE.search(sql) and not _PMAY_SERIES_ASK_RE.search(q):
        return ("this question asks for ONE figure per area over the whole period, but the query groups "
                "by financial year / sanction date, so the answer becomes a per-year row dump or a sum of "
                "rounded yearly values (\"financial summary for Mawphlang\" read 41.79 crore for a true "
                "41.78 crore and never gave the released amount). Remove the year column from SELECT and "
                "GROUP BY and return one row per area named in the question (a single row if one area): "
                "COUNT(*) AS houses, SUM(sanctioned_amount) AS sanctioned, SUM(COALESCE(amount_released, 0)) "
                "AS released, SUM(sanctioned_amount) - SUM(COALESCE(amount_released, 0)) AS remaining in "
                "RUPEES, unrounded. Keep every filter (NOT is_placeholder, geography, year_key) as it was.")
    if _PMAY_AVG_RELEASED_RE.search(sql) and _PMAY_RATE_ASK_RE.search(q):
        return ("utilisation / the share of the sanctioned amount released is ONE ratio of totals: "
                "100.0 * SUM(COALESCE(amount_released, 0)) / NULLIF(SUM(sanctioned_amount), 0) over rows with "
                "sanctioned_amount > 0. The query uses AVG(...) over amount_released, which skips the 336 houses "
                "whose release is NULL and averages per-house ratios — a different, wrong number (98.82% for a "
                "true 98.76% in East Khasi Hills). Replace the AVG with that SUM ratio and keep every filter.")
    return None


def _pmay_no_houses_answer(spec: dict, row: "dict | None", display: dict) -> str:
    """A village the resolver settled by LGD code but which holds no counted PMAY-G house —
    a genuine zero (the code cannot be misspelled). Rtiang Sanphew's only record is a
    zero-sanction placeholder; the model path answered "the data doesn't cover … shows 0"."""
    name = _place_title((row or {}).get("name") or display.get("village") or str(spec["entities"][0]))
    where = name + (f" village ({_place_title(row['block'])} block, {_place_title(row['district'])})"
                    if row and row.get("block") else " village")
    ph = int((row or {}).get("placeholders") or 0)
    return (f"No PMAY-G houses are recorded for {where}: it has no sanctioned house in the data, so the "
            "beneficiary count, amounts and completion are all zero." + (
                f" Its {ph} record{'s are' if ph > 1 else ' is a'} placeholder{'s' if ph > 1 else ''} with a sanctioned "
                "amount of ₹0, which is not counted as a beneficiary." if ph else ""))


# Money cells in a model-path PMAY-G result (rupees, not already crore / lakh).
_PMAY_MONEY_COL_RE = re.compile(r"sanction|releas|pending|amount|rupee|spent|expend|outlay|disburs|remaining|gap",
                                re.IGNORECASE)
_PMAY_NOT_MONEY_COL_RE = re.compile(r"_cr\b|crore|lakh|pct|percent|rate|share|ratio|count|houses|number|_no\b|date|year",
                                    re.IGNORECASE)


def _pmay_rupee_format(answer: str, rows: list[dict], fmt=None, column_totals: bool = False) -> str:
    """Rewrite the exact money cells of a PMAY-G result that the composer printed bare
    ("795,860,000.00", "130000.00 rupees") as ₹ with Indian grouping and lakh / crore —
    only the figures that ARE result cells, so nothing new is stated (KI-095).
    `fmt` overrides the formatter; `column_totals` also rewrites each money column's
    sum over the rows, which the composer states as "a total of …" (Focus Legacy, KI-147)."""
    if not answer or not rows:
        return answer
    fmt = fmt or _pmay_money
    vals = set()
    sums: dict[str, float] = {}
    for r in rows[:50]:
        for k, v in r.items():
            if (isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) and abs(float(v)) >= 1000
                    and _PMAY_MONEY_COL_RE.search(str(k)) and not _PMAY_NOT_MONEY_COL_RE.search(str(k))):
                vals.add(float(v))
                sums[k] = sums.get(k, 0.0) + float(v)
    if column_totals and len(rows) > 1:
        vals.update(sums.values())
    for v in sorted(vals, reverse=True):
        forms = {f"{v:,.2f}", f"{v:,.0f}", f"{v:.2f}", f"{v:.0f}"}
        for s in sorted(forms, key=len, reverse=True):
            answer = re.sub(r"(?:₹\s?|Rs\.?\s?|INR\s?)?(?<![\d,.])" + re.escape(s) + r"(?!\d|[,.]\d)(?:\s*(?:rupees|INR))?",
                            lambda _m, v=v: fmt(v), answer)
    return answer


_PMAY_UNKNOWN_PLACE_NOTE_RE = re.compile(
    r"^'(?P<name>[^']+)' is not a known (?P<level>village|block or village|block|district)\b")


def _pmay_unknown_place_answer(schemes: list[str], resolved: dict, notes: "list[str] | None") -> "str | None":
    """A plain "no such place" for a PMAY-G question whose only place the resolver
    could not find. The model path answered "the data available doesn't cover …
    the result shows 0 beneficiaries" for a made-up village, which reads like a real
    zero (PMAY-G probes 2026-09-28)."""
    if schemes != ["PMAY-G"] or any(resolved.get(k) for k in (
            "village_code", "village_code_list", "block", "block_list", "district", "district_list")):
        return None
    hits = [m for m in (_PMAY_UNKNOWN_PLACE_NOTE_RE.match(n or "") for n in (notes or [])) if m]
    if len(hits) != 1:
        return None
    name, level = hits[0].group("name"), hits[0].group("level")
    return (f"“{name}” is not a {level} in Meghalaya's LGD list or in the PMAY-G data, so there is no PMAY-G figure "
            "for it. Please check the spelling, or name the block or district it is in.")


def _pmay_fy_labels(answer: str, rows: list[dict]) -> str:
    """A model-path answer that printed year_key values as bare years ("2,390 houses
    sanctioned in 2017") reads as a calendar year; year_key is the financial year
    (2017 = FY 2017-18). Relabel a year that IS a year_key cell of the result, after
    in / for / during / and / a comma (PMAY-G scenario run 2026-09-28)."""
    if not answer or not rows or not any("year_key" in r for r in rows[:1]):
        return answer
    yks = {int(r["year_key"]) for r in rows[:60] if isinstance(r.get("year_key"), (int, float)) and r.get("year_key")}
    for y in sorted(yks, reverse=True):
        answer = re.sub(rf"(\b(?:in|for|during|and|of)\s+|,\s*)(?:FY\s*)?{y}(?![\d-])",
                        lambda m, y=y: f"{m.group(1)}FY {y}-{(y + 1) % 100:02d}", answer)
    return answer


_PMAY_CRORE_DIV_RE = re.compile(r"\s*/\s*(?:10000000(?:\.0*)?|1e7|1E7)(?![\d.])")
_PMAY_CRORE_ALIAS_RE = re.compile(r"\bAS\s+(\w*?)_?(?:crores?|cr)\b", re.IGNORECASE)


def _pmay_crore_to_rupees(schemes: list[str], sql: str) -> str:
    """PMAY-G money is rupees; the model's ROUND(x / 10000000.0, 2) turned a ₹1,30,000
    average into "0.01 crore" (PMAY-G scenario run 2026-09-28; the model-path twin of
    KI-095). Drop the crore division and name the column in rupees — the rows then
    carry exact rupees and _pmay_rupee_format writes ₹ with lakh / crore."""
    if schemes != ["PMAY-G"] or not sql or not _PMAY_CRORE_DIV_RE.search(sql):
        return sql
    sql = _PMAY_CRORE_DIV_RE.sub("", sql)
    return _PMAY_CRORE_ALIAS_RE.sub(lambda m: f"AS {m.group(1) or 'amount'}_rupees", sql)


def _pmay_comparison_limit(schemes: list[str], entity_result: dict, sql: str) -> str:
    """A comparison of N named areas must return N rows. "Which has more completed
    houses: Maweitnar or Rohonpara?" came back with LIMIT 1 and the answer named only
    the winner (KI-093)."""
    if schemes != ["PMAY-G"] or not sql:
        return sql
    r = (entity_result or {}).get("resolved") or {}
    n = max((len(r.get(k) or []) for k in ("village_code_list", "block_list", "district_list")), default=0)
    if n >= 2 and re.search(r"\bLIMIT\s+1\b\s*;?\s*$", sql, re.IGNORECASE):
        sql = re.sub(r"\bLIMIT\s+1\b(\s*;?\s*)$", rf"LIMIT {n}\1", sql, flags=re.IGNORECASE)
    return sql


_TOP_ONE_RE = re.compile(r"\bORDER\s+BY\b[^;]*?\bDESC\b[^;]*?\bLIMIT\s+1\b", re.IGNORECASE | re.DOTALL)
# A WHERE clause (read only up to GROUP BY / ORDER BY) that pins a district or
# block: the top row is then the top WITHIN that area, not statewide. Scanning
# past the WHERE clause matched "GROUP BY lgd_district" and made every ranking
# look scoped.
_WHERE_SCOPED_RE = re.compile(
    r"\bWHERE\b(?:(?!\bGROUP\s+BY\b|\bORDER\s+BY\b)[^;])*?\blgd_(?:district|block)\b",
    re.IGNORECASE | re.DOTALL)


def _mgnrega_answer_notes(question: str, sql: str, rows: list[dict]) -> list[str]:
    """Composer notes for MGNREGA result shapes it described misleadingly
    (MGNREGA QA 2026-09-26): a statewide top-1 row was called "the highest …
    in the WEST GARO HILLS district" (DATA-025) or "the only district recorded"
    (DATA-027), and raw column names leaked ("lgd_district", DATA-026)."""
    notes: list[str] = []
    if len(rows) == 1 and _TOP_ONE_RE.search(sql or "") and not _WHERE_SCOPED_RE.search(sql or ""):
        notes.append(
            "This row is the TOP-ranked one out of all of Meghalaya (the query ranked every "
            "area and kept the first). Say it is the highest in Meghalaya. Do not call it the "
            "only one, and do not say it is the highest within its own district or block.")
    if re.search(r"\b" + _MGNREGA_MONEY_COL + r"\b", sql or "", re.IGNORECASE) \
            and "crore" not in (sql or "").lower():
        # "Wages spending of 219.48 exceeded materials spending of 113.89" —
        # right numbers, no unit (DATA-023, 2026-09-26). MGNREGA money is lakh.
        notes.append("Every money figure in this result is in ₹ LAKH. Write \"lakh\" after "
                     "each amount (e.g. \"219.48 lakh\").")
    if rows and any(re.fullmatch(r"lgd_\w+|village_code|assembly_constituency_name\w*", str(k))
                    for k in rows[0]):
        notes.append(
            "Name places in plain words (\"Mawryngkneng village, Mawryngkneng block, East Khasi "
            "Hills\"). Never write column names such as lgd_district, lgd_block or village_code.")
    return notes


_ONLY_ONE_SENTENCE_RE = re.compile(
    r"\bonly\s+(?:district|block|village|area|entry|record|row)\b|\bin\s+the\s+result\b|"
    r"\bhighest\b[^.]*\bin\s+the\s+[A-Z][A-Za-z ]+\s+(?:district|block)\b", re.IGNORECASE)


def _mgnrega_top_one_wording(answer: str, sql: str, rows: list[dict]) -> str:
    """Deterministic backstop for _mgnrega_answer_notes' ranking note, which the
    composer ignored on 2 of 2 live runs ("This is the only block recorded for
    that financial year in the result", DATA-025/027): drop that sentence from a
    statewide top-1 answer and say what the row actually is."""
    if not (len(rows) == 1 and _TOP_ONE_RE.search(sql or "")) or _WHERE_SCOPED_RE.search(sql or ""):
        return answer
    sentences = re.split(r"(?<=[.!?])\s+", (answer or "").strip())
    kept = [s for s in sentences if not _ONLY_ONE_SENTENCE_RE.search(s)]
    if len(kept) == len(sentences) or not kept:
        return answer
    return " ".join(kept) + " It is the highest in Meghalaya."


# ── MGNREGA women employment: which years actually carry it ─────────────────
# women_employment_provided is 0 on EVERY record for FY 2025-26 — not populated
# at source (docs/MGNREGA_DB_Issues.md, Issue 2). "What percentage of employment
# persons were women in ekh?" named no year; the percentage cue was missing from
# the year gate, so the SQL model silently took MAX(year_key) = FY 2025-26 and
# the bot answered "0.00% … a genuine zero" — East Khasi Hills is really 77.70%
# in FY 2024-25 (user report, 2026-09-27). Women answers therefore use only the
# years that carry women data, say which years they cover, and call FY 2025-26
# "not recorded", never zero. Read live, so a later ingest that fills the year
# is picked up without a code change.
_WOMEN_CUE = re.compile(r"\b(?:women|woman|female|females|mahila)\b", re.IGNORECASE)
_RATIO_CUE = re.compile(r"\bper\s*cent\w*|\bpercent\w*|%|\bshare\b|\bproportion\b|\bratio\b",
                        re.IGNORECASE)
# Rankings / breakdowns / comparisons stay with the SQL generator.
_MG_WOMEN_EXCLUDE_RE = re.compile(
    r"\b(?:each|every|by|wise|breakdown|trend|top|highest|lowest|most|least|rank\w*|which|"
    r"compare\w*|versus|vs|men|male)\b|\bper\b(?!\s*cent)", re.IGNORECASE)
_MGNREGA_YEAR_WOMEN: "list[dict] | None" = None


async def _mgnrega_year_women() -> "list[dict]":
    """[{year_key, fy, women}] for every MGNREGA employment year, oldest first."""
    global _MGNREGA_YEAR_WOMEN
    if _MGNREGA_YEAR_WOMEN is None:
        rows = await fetch_rows(
            "SELECT year_key, MAX(financial_year_short) AS fy, "
            "COALESCE(SUM(women_employment_provided), 0) AS women "
            "FROM curated.v_employment GROUP BY year_key ORDER BY year_key", [])
        _MGNREGA_YEAR_WOMEN = [{"year_key": int(r["year_key"]), "fy": str(r["fy"]),
                                "women": float(r["women"] or 0)} for r in rows]
    return _MGNREGA_YEAR_WOMEN


def _women_span(years: "list[dict]") -> str:
    labels = [f"FY {y['fy']}" for y in years]
    return labels[0] if len(labels) == 1 else f"{labels[0]} to {labels[-1]}"


async def _mgnrega_women_query(question: str, schemes: list[str], resolved: dict) -> "dict | None":
    """A deterministic women count / share for ONE area (village, block, district,
    constituency or statewide), over the requested year or — when no year was
    named — every year that carries women data. None when the question is not a
    single-area women question (rankings etc. stay with the generator)."""
    q = question or ""
    if schemes != ["MGNREGA"] or not _WOMEN_CUE.search(q) or _MG_WOMEN_EXCLUDE_RE.search(q):
        return None
    yk = resolved.get("year_key")
    if isinstance(yk, (list, tuple, set)):
        return None
    f = _mgnrega_filters({k: v for k, v in resolved.items() if k != "year_key"}, allow_ac=True)
    if f is None:
        return None
    try:
        years = await _mgnrega_year_women()
    except Exception:  # noqa: BLE001 — without the year map, leave it to the generator
        return None
    have = [y for y in years if y["women"] > 0]
    missing = [y for y in years if y["women"] <= 0]
    if not have:
        return None
    where, params, shown = f
    if yk is not None:
        if any(int(yk) == y["year_key"] for y in missing):
            return {"kind": "unrecorded", "year": next(y for y in missing if y["year_key"] == int(yk)),
                    "have": have, "where": where, "params": params}
        sel = [y for y in years if y["year_key"] == int(yk)]
    else:
        sel = have
    keys = [y["year_key"] for y in sel]
    p2 = params + [keys]
    w2 = where + [f"year_key = ANY(${len(p2)}::int[])"]
    s2 = shown + [f"year_key IN ({', '.join(map(str, keys))})"]

    def build(w: list[str]) -> str:
        return ("SELECT SUM(women_employment_provided) AS women_employed, "
                "SUM(persons_employed) AS persons_employed, "
                "ROUND(100.0 * SUM(women_employment_provided)::numeric / "
                "NULLIF(SUM(persons_employed), 0), 2) AS women_share_pct\n"
                "FROM curated.v_employment WHERE " + " AND ".join(w))
    return {"kind": "query", "sql": build(w2), "params": p2, "shown": build(s2),
            "years": sel, "all_years": yk is None, "missing": missing}


async def _mgnrega_women_unrecorded_answer(info: dict, display: dict) -> str:
    """FY 2025-26 (or any year without women data) was asked for explicitly."""
    fy = info["year"]["fy"]
    latest = info["have"][-1]
    place = _mgnrega_scope_words({k: v for k, v in display.items() if k != "year"})
    head = (f"Women employment is not recorded in the MGNREGA data for FY {fy} — the women column "
            "is 0 on every record statewide for that year (not populated at source), so no women "
            "figure or share can be given for it.")
    try:
        w = info["where"] + [f"year_key = ${len(info['params']) + 1}"]
        r = (await fetch_rows(
            "SELECT SUM(women_employment_provided) AS w, SUM(persons_employed) AS p, "
            "ROUND(100.0 * SUM(women_employment_provided)::numeric / NULLIF(SUM(persons_employed), 0), 2) "
            "AS pct FROM curated.v_employment WHERE " + " AND ".join(w),
            info["params"] + [latest["year_key"]]))[0]
    except Exception:  # noqa: BLE001
        return head
    if r["pct"] is None:
        return head
    return (f"{head} The latest year with women data is FY {latest['fy']}: {float(r['pct']):.2f}% of "
            f"employed persons{place} were women ({int(r['w']):,} of {int(r['p']):,}).")


async def _mgnrega_women_year_clarification(question: str) -> "ClarificationNeeded":
    """The FY pause for a women question: only years that carry women data."""
    years = await _mgnrega_year_women()
    have = [y for y in years if y["women"] > 0]
    missing = [y for y in years if y["women"] <= 0]
    stem = (question or "").strip().rstrip(" ?.")
    options = [{"label": f"FY {y['fy']}", "question": f"{stem} for FY {y['fy']}"} for y in have]
    options.append({"label": f"All years with women data ({_women_span(have)})",
                    "question": f"{stem} across all financial years"})
    gap = (f" Women employment is not recorded for {', '.join('FY ' + y['fy'] for y in missing)}."
           if missing else "")
    return ClarificationNeeded(
        f"MGNREGA women-employment data is available for {_women_span(have)}.{gap} Which financial "
        "year is required — a single year, or all of these years combined?",
        options=options, rule="year-not-specified")


def _mgnrega_women_years_only(schemes: list[str], sql: str) -> str:
    """Generator SQL that reads women_employment_provided across ALL years (no
    year_key anywhere) would count FY 2025-26's unrecorded women as zero against
    its real persons — East Khasi Hills' all-years share read 55.86% instead of
    74.90%. Restrict such a query to the years that carry women data."""
    if "MGNREGA" not in schemes or not sql or _MGNREGA_YEAR_WOMEN is None:
        return sql
    if "women_employment_provided" not in sql.lower() or re.search(r"\byear_key\b", sql, re.IGNORECASE):
        return sql
    have = [y["year_key"] for y in _MGNREGA_YEAR_WOMEN if y["women"] > 0]
    if not have or len(have) == len(_MGNREGA_YEAR_WOMEN):
        return sql
    sub = f"(SELECT * FROM curated.v_employment WHERE year_key IN ({', '.join(map(str, have))}))"

    def _swap(m: "re.Match") -> str:
        alias = m.group(1)
        return f"{sub} AS {alias}" if alias else f"{sub} AS v_employment"
    return re.sub(
        r"\bcurated\.v_employment\b(?:\s+(?:AS\s+)?(?!WHERE\b|GROUP\b|ORDER\b|LIMIT\b|JOIN\b|INNER\b|"
        r"LEFT\b|RIGHT\b|FULL\b|CROSS\b|ON\b|UNION\b|HAVING\b)([A-Za-z_]\w*))?",
        _swap, sql, flags=re.IGNORECASE)


def _mgnrega_women_sql_years(sql: str) -> "set[int] | None":
    """The year_keys a women query filters (literal = / IN), or None."""
    keys: set[int] = set()
    for m in re.finditer(r"\byear_key\s*=\s*(\d{4})\b", sql or "", re.IGNORECASE):
        keys.add(int(m.group(1)))
    for m in re.finditer(r"\byear_key\s+IN\s*\(([\d\s,]+)\)", sql or "", re.IGNORECASE):
        keys |= {int(x) for x in re.findall(r"\d{4}", m.group(1))}
    return keys or None


def _mgnrega_scope_words(display: dict) -> str:
    """"for Demdema block in FY 2024-25" from the resolved display names."""
    place = " ".join(p for p in (
        display.get("village") and f"{display['village']} village",
        display.get("block") and f"{str(display['block']).title()} block",
        display.get("assembly_constituency") and f"{display['assembly_constituency']} constituency",
        display.get("district")) if p)
    year = f" in {display['year']}" if display.get("year") else ""
    return (f" for {place}" if place else "") + year


def _mgnrega_fact_word(sql: str) -> str:
    s = (sql or "").lower()
    emp, exp = "v_employment" in s or "fact_mgnrega_employment" in s, \
        "v_expenditure" in s or "fact_mgnrega_expenditure" in s
    return "employment" if emp and not exp else "expenditure" if exp and not emp else "MGNREGA data"


async def _mgnrega_empty_answer(sql: str, rows: list[dict], display: dict,
                                resolved: dict) -> "str | None":
    """A single aggregate row whose every value is NULL. Either the filters
    matched no source rows at all (SUM over nothing — KHATARSHNONG LAITKROH has
    no expenditure rows, Mairangbah no 2024-25 employment rows), or a ratio's
    base is zero (Demdema's 100-day % is 0 / 0). The composer described both as
    "the result shows a null value for total_expenditure_crore" (all-blocks QA
    2026-09-26). Which one it is is checked with a parameter-bound row count."""
    if len(rows) != 1 or not rows[0] or any(v is not None for v in rows[0].values()):
        return None
    what = _mgnrega_fact_word(sql)
    view = {"employment": "curated.v_employment", "expenditure": "curated.v_expenditure"}.get(what)
    n = None
    f = _mgnrega_filters(resolved, allow_ac=(what == "employment"))
    if view and f is not None:
        where, params, _ = f
        try:
            n = (await fetch_rows(f"SELECT COUNT(*) AS n FROM {view}"
                                  + (" WHERE " + " AND ".join(where) if where else ""), params))[0]["n"]
        except Exception:  # noqa: BLE001 — fall back to the composer
            return None
    scope = _mgnrega_scope_words(display)
    if n == 0:
        return (f"No MGNREGA {what} is recorded{scope} — the data has no {what} records for it, "
                "so there is no figure to report (this is not the same as zero).")
    if n and re.search(r"\bNULLIF\b", sql or "", re.IGNORECASE):
        return (f"This cannot be calculated{scope}: the figure it is divided by is 0 in the "
                "recorded data (for example, no households received employment), so there is no "
                "percentage or average to report.")
    return None


def _mgnrega_null_zero_notes(rows: list[dict]) -> list[str]:
    """Composer notes for a single-row result with an empty side, or genuine zeros."""
    if len(rows) != 1 or not rows[0]:
        return []
    row, notes = rows[0], []
    empty = [k for k, v in row.items() if v is None]
    if empty and len(empty) < len(row):
        notes.append(
            f"{', '.join(empty)} is empty because no MGNREGA records of that kind exist for this "
            "area and year. Say so plainly (e.g. \"no expenditure is recorded for it\"), still "
            "report the other figures, never write \"null\", and never call it zero.")
    nums = [_as_number(v) for v in row.values() if v is not None]
    if nums and all(n == 0 for n in nums if n is not None) and all(n is not None for n in nums):
        notes.append(
            "Every figure here is a genuine recorded ZERO (the area has records, all 0). State "
            "it as 0 (e.g. \"0 households received employment\"). Do not say the data does not "
            "cover it.")
    return notes


def _mgnrega_zero_backstop(answer: str, rows: list[dict], display: dict) -> str:
    """The composer hedged over a genuine zero ("the data available doesn't cover the
    number of households … Demdema") — replace it with the recorded 0."""
    if len(rows) != 1 or not rows[0] or not _HEDGE_RE.search(answer or ""):
        return answer
    vals = [(k, _as_number(v)) for k, v in rows[0].items()]
    if not vals or any(n is None or n != 0 for _, n in vals):
        return answer
    parts = "; ".join(f"0 {_metric_label(k)}" for k, _ in vals)
    return f"{parts}{_mgnrega_scope_words(display)} — the data records zero."


_MONEY_COL_NAME_RE = re.compile(r"exp|lakh|wage|material|spend|cost|amount", re.IGNORECASE)


def _mgnrega_money_units(answer: str, sql: str, rows: list[dict]) -> str:
    """Deterministic backstop for the "write lakh" note: add " lakh" after any
    MGNREGA money value from the result that the answer states without a unit.
    Skipped for crore SQL (the value is then crore, and the composer names it)."""
    if not answer or not rows or "crore" in (sql or "").lower() or not re.search(
            r"\b" + _MGNREGA_MONEY_COL + r"\b", sql or "", re.IGNORECASE):
        return answer
    forms: set[str] = set()
    for r in rows[:40]:
        for k, v in r.items():
            n = _as_number(v)
            if n is None or not _MONEY_COL_NAME_RE.search(str(k)) or isinstance(n, int) and n == 0:
                continue
            f = float(n)
            forms |= {f"{f:,.2f}", f"{f:.2f}"}
    for form in sorted(forms, key=len, reverse=True):
        answer = re.sub(rf"(?<![\d.,]){re.escape(form)}(?![\d])(?!\s*(?:lakh|lac|crore|cr\b|%))",
                        f"{form} lakh", answer)
    return answer


async def _mgnrega_admin_expenditure_answer(rows: list[dict], display: dict) -> str:
    """The recorded figure, stated with the reason it cannot be read as a real
    zero. The caveat is the whole point of the earlier refusal (2026-09-17: a
    bare 0.00 reads as a measured finding), so it is always attached; the
    statewide figure is read live so the caveat can never go stale."""
    val = _as_number((rows or [{}])[0].get("admin_expenditure_lakh")) or 0
    place = " ".join(p for p in (display.get("village") and f"{display['village']} village",
                                 display.get("block") and f"{display['block']} block",
                                 display.get("district")) if p) or "Meghalaya"
    year = f" in {display['year']}" if display.get("year") else ""
    try:
        st = (await fetch_rows(
            "SELECT ROUND(SUM(admin_total_exp)::numeric, 2) AS total, "
            "COUNT(*) FILTER (WHERE admin_total_exp <> 0) AS nonzero, COUNT(*) AS n "
            "FROM curated.v_expenditure", []))[0]
        state = (f" Statewide, the column is non-zero on only {st['nonzero']} of {st['n']:,} "
                 f"records (₹{float(st['total'] or 0):,.2f} lakh in total).")
    except Exception:  # noqa: BLE001 — the caveat stands without the statewide figure
        state = ""
    return (f"Recorded MGNREGA administrative expenditure for {place}{year}: "
            f"₹{float(val):,.2f} lakh.\n\n"
            "Please read this with care: the administrative-expenditure column was never "
            "populated at source, so this figure reflects what was recorded, not a confirmed "
            f"zero spend.{state} The expenditure that is recorded is unskilled wages, "
            "semi-skilled wages, material and total expenditure — ask for any of those.")


def _mgnrega_facts_joined(sql: str) -> bool:
    """True when the query JOINs the employment fact to the expenditure fact in
    one statement — the fan-out shape rule 1 forbids.

    The safe shapes name both objects too, but wrap each in its own aggregated
    scope so anything joined afterwards is one row per side:
      * a CTE per fact  (WITH emp AS (...), exp AS (...) SELECT ...), and
      * an inline subquery per fact  (FROM (SELECT SUM(...) ...) exp CROSS JOIN
        (SELECT SUM(...) ...) emp) — which is what the repair prompt actually
        produces most often, and is equally correct.
    Both are recognised by each fact object sitting inside a parenthesised
    SELECT that aggregates. Only a bare top-level join of the two raw facts
    multiplies, so only that is flagged."""
    s = sql or ""
    if not (_EMPLOYMENT_OBJ_RE.search(s) and _EXPENDITURE_OBJ_RE.search(s)
            and _JOIN_RE.search(s)):
        return False
    if _CTE_RE.match(s.strip()):
        return False
    # Each fact reference that sits inside a parenthesised aggregating SELECT is
    # already collapsed to one row; if BOTH are, nothing can fan out.
    return not all(_fact_ref_is_aggregated(s, rx)
                   for rx in (_EMPLOYMENT_OBJ_RE, _EXPENDITURE_OBJ_RE))


_AGG_SELECT_RE = re.compile(r"\b(?:SUM|COUNT|AVG|MIN|MAX)\s*\(", re.IGNORECASE)


def _fact_ref_is_aggregated(sql: str, obj_re: "re.Pattern") -> bool:
    """True when every reference to this fact object lies inside a parenthesised
    SELECT that aggregates — i.e. it contributes one row, not many."""
    for m in obj_re.finditer(sql):
        depth, start = 0, None
        for i in range(m.start() - 1, -1, -1):   # walk back to the enclosing "("
            c = sql[i]
            if c == ")":
                depth += 1
            elif c == "(":
                if depth == 0:
                    start = i
                    break
                depth -= 1
        if start is None:
            return False                          # top-level reference
        if not _AGG_SELECT_RE.search(sql[start:m.start()]):
            return False                          # a plain subquery, not aggregated
    return True


_STATE_PSEUDO_FILTER = re.compile(
    r"entity_type\s*=\s*'\s*state\s*'"
    r"|lgd_(?:village_name|district|block)\s*(?:=|ilike)\s*'\s*%?\s*meghalaya\s*%?\s*'",
    re.IGNORECASE,
)


# curated.* stores lgd_district / lgd_block UPPERCASE (docs/DATA_MODEL.md). When
# entity resolution misses and the generator copies the question's Title-Case
# spelling straight into the literal (lgd_district = 'West Garo Hills'), the
# query runs clean and counts zero. Upper-case *only* the string literals
# compared with `=` / `!=` / `IN` against those two columns — nothing else is
# touched, and an already-uppercase or ILIKE clause is left as-is.
_GEO_LITERAL_RE = re.compile(
    r"(?P<pre>\blgd_(?:district|block)\s*(?:=|!=|<>|\bIN\b)\s*\(?\s*)"
    r"(?P<lits>'(?:[^']|'')*'(?:\s*,\s*'(?:[^']|'')*')*)",
    re.IGNORECASE,
)


def _uppercase_geo_literals(sql: str) -> str:
    changed = _GEO_LITERAL_RE.sub(lambda m: m.group("pre") + m.group("lits").upper(), sql)
    if changed != sql:
        logger.info("normalised lgd_district/lgd_block literal(s) to upper-case for storage match")
    return changed


# Client UAT sheet (FOCUS-018/019, 2026-09-13): "Give me the status breakdown
# for [tranche]" / "for [batch]" came back GROUP BY focus_status,
# verification_status even though schema_context.py rule 13 and the matching
# focusplus_few_shot.yaml examples both say an unqualified status breakdown
# means focus_status alone — verification_status is a near-constant single
# value ("Approved" on every 12.5K row) that only adds noise. The prose rule
# and the correctly-written few-shot examples exist and are scoped right; the
# generator still occasionally copies the two-column GROUP BY from the
# deliberately-different "what status values are recorded" enumeration
# example once a tranche/batch filter is also in play — the same class of
# prose-doesn't-reliably-win gap as _focusplus_single_district_beneficiary_guard
# above. Deterministic strip instead of a repair round trip, since the fix
# (drop one column) is unambiguous once the shape is detected.
# CM Elevate Legacy: the Unresolved-placeholder exclusion belongs ONLY on village
# counts and village lists (schema_context rule 9, geography_unresolved_rule).
# The 404 no-village records still have a known district, so a district / scheme
# / year total that drops them is simply wrong. Confirmed in the 2026-09-25
# use-case QA (TC-27, "total disbursement for each district"): the generator
# copied the filter from the village shots and returned ₹53.30 Cr statewide
# against a true ₹81.10 Cr. The rule is in the prompt; it does not reliably win,
# and the fix (delete one predicate) is unambiguous once the shape is detected.
_UNRESOLVED_NE = r"(?:\w+\.)?entity_type\s*(?:<>|!=)\s*'Unresolved'"
_UNRESOLVED_WHERE_AND_RE = re.compile(rf"\bWHERE\s+{_UNRESOLVED_NE}\s+AND\s+", re.IGNORECASE)
_UNRESOLVED_AND_RE = re.compile(rf"\s+AND\s+{_UNRESOLVED_NE}", re.IGNORECASE)
_UNRESOLVED_WHERE_ONLY_RE = re.compile(rf"\s*\bWHERE\s+{_UNRESOLVED_NE}(?=\s*(?:GROUP|ORDER|LIMIT|;|\)|$))",
                                       re.IGNORECASE)
_VILLAGE_SQL_RE = re.compile(r"\bvillage_code\b|\blgd_village_name\b", re.IGNORECASE)
_VILLAGE_WORD_RE = re.compile(r"\bvillages?\b", re.IGNORECASE)
# Focus Legacy has the same placeholder design and the same rule (README §5,
# "entity_type <> 'Unresolved' on village counts and lists only — never on
# money, district"). Its 2026-09-25 QA hit the identical bug: TC-18 "total amount
# disbursed for East Khasi Hills" dropped 11 no-village payments (₹5,35,15,000
# against a true ₹5,42,05,000).
# CM Elevate (applications) has the same design and rule (CM ELEVATE RULES
# rule 5). Its 2026-09-27 QA hit it too (KI-068, CM-ELEVATE-OFF-005): "how many
# applicants under each CM ELEVATE program" dropped the 51 no-village
# applications — SEED 3,617 against a true 3,633, 8 of 15 programmes low.
_UNRESOLVED_PLACEHOLDER_SCHEMES = (["CM Elevate Legacy"], ["Focus Legacy"], ["CM Elevate"])


def _cm_legacy_keep_unresolved_off_village(question: str, schemes: list[str], sql: str) -> str:
    if (schemes not in _UNRESOLVED_PLACEHOLDER_SCHEMES
            or not re.search(_UNRESOLVED_NE, sql or "", re.IGNORECASE)):
        return sql
    if _VILLAGE_SQL_RE.search(sql) or _VILLAGE_WORD_RE.search(question or ""):
        return sql
    out = _UNRESOLVED_WHERE_AND_RE.sub("WHERE ", sql)
    out = _UNRESOLVED_AND_RE.sub("", out)
    out = _UNRESOLVED_WHERE_ONLY_RE.sub("", out)
    if out != sql:
        logger.info("%s: dropped entity_type <> 'Unresolved' from a non-village query — "
                    "the no-village records still count at district grain", schemes[0])
    return out


# ── CM Elevate SQL shape guards (use-case QA 2026-09-27, KI-069 / 070 / 072) ─
# Each prose rule below already sat in schema_context (rules 4, 13, 14) and
# did not hold under sampling; each fix is one unambiguous edit once the shape
# is detected, so it is applied deterministically, like the Unresolved guard.
def _mask_sql_literals(sql: str) -> str:
    """The SQL with every string literal's contents blanked, SAME LENGTH, so
    keyword searches ignore literals and positions still index the original."""
    return _SQL_LITERAL.sub(lambda m: "'" + "_" * (len(m.group(0)) - 2) + "'", sql or "")


def _drop_where_conjunct(sql: str, pred: str) -> str:
    """Remove one AND-ed predicate (a regex source) from a single-level WHERE."""
    out = re.sub(rf"\bWHERE\s+{pred}\s+AND\s+", "WHERE ", sql, flags=re.IGNORECASE)
    out = re.sub(rf"\s+AND\s+{pred}", "", out, flags=re.IGNORECASE)
    return re.sub(rf"\s*\bWHERE\s+{pred}(?=\s*(?:GROUP|ORDER|LIMIT|HAVING|;|$))", "", out,
                  flags=re.IGNORECASE)


def _simple_select(masked: str) -> bool:
    return (len(re.findall(r"\bselect\b", masked, re.IGNORECASE)) == 1
            and not re.search(r"\b(?:join|union|with)\b", masked, re.IGNORECASE))


# literals, not [^)]*: "PRIME … (SEED)" carries its own parenthesis
_CME_SCHEME_IN_RE = re.compile(r"\bscheme_name\s+IN\s*\(\s*((?:'(?:[^']|'')*'\s*,?\s*)+)\)", re.IGNORECASE)


def _cm_elevate_split_scheme_in_list(schemes: list[str], sql: str) -> str:
    """KI-069 (CM-ELEVATE-OFF-009b, 029b): "applicants in East Khasi Hills under
    PRIME SEED and PRIME Tourism Vehicle" was answered by ONE merged
    COUNT(DISTINCT request_id) over `scheme_name IN (…)` — 716, with no figure
    for either programme, although rule 4 says a named multi-scheme ask is
    always a GROUP BY scheme_name breakdown plus the combined total. Add
    scheme_name to the SELECT and the GROUP BY; the combined total is then
    stated by _cme_multi_scheme_total. request_id never repeats ACROSS
    programmes (sum of per-programme distinct = 8,600 = statewide distinct), so
    that sum is exact."""
    if schemes != ["CM Elevate"] or not sql:
        return sql
    m = _CME_SCHEME_IN_RE.search(sql)
    if not m or len(_SQL_LITERAL.findall(m.group(1))) < 2:
        return sql
    masked = _mask_sql_literals(sql)
    sel = re.search(r"\bselect\s+(?!distinct\b)", masked, re.IGNORECASE)
    frm = re.search(r"\bfrom\b", masked, re.IGNORECASE)
    if not _simple_select(masked) or not sel or not frm or frm.start() < sel.end():
        return sql
    select_list = masked[sel.end():frm.start()]
    if re.search(r"\bscheme_name\b", select_list, re.IGNORECASE) or not re.search(
            r"\bcount\s*\(", select_list, re.IGNORECASE):
        return sql
    ins = "scheme_name, "
    out = sql[:sel.end()] + ins + sql[sel.end():]
    gb = re.search(r"\bgroup\s+by\s+", masked, re.IGNORECASE)
    if gb:
        pos = gb.end() + len(ins)
        out = out[:pos] + "scheme_name, " + out[pos:]
    else:
        om = _mask_sql_literals(out)
        tail = re.search(r"\s+(?:order\s+by|limit)\b", om[frm.start() + len(ins):], re.IGNORECASE)
        pos = frm.start() + len(ins) + tail.start() if tail else len(out.rstrip().rstrip(";").rstrip())
        out = out[:pos] + "\nGROUP BY scheme_name" + out[pos:]
    out = re.sub(r"\s+LIMIT\s+1\s*(;?)\s*$", r"\1", out, flags=re.IGNORECASE)
    logger.info("CM Elevate: split a merged multi-programme count by scheme_name (rule 4)")
    return out


_CME_STATUS_PRED = r"(?:\w+\.)?data_verified\s*=\s*'(?:[^']|'')*'"
_CME_PLAIN_COUNT_RE = re.compile(r"\bCOUNT\s*\(\s*(?:\*|DISTINCT\s+(?:\w+\.)?request_id)\s*\)(?!\s*FILTER\b)",
                                 re.IGNORECASE)


def _cm_elevate_sector_zero_groups(schemes: list[str], sql: str) -> str:
    """KI-072 (CM-ELEVATE-OFF-028b): "applications pending in each sector in
    West Khasi Hills" filtered data_verified = 'On Hold' in the WHERE and then
    grouped by sector, so every sector with 0 pending vanished and the answer
    said "No other sectors are listed" — the district has 17 sector-tagged
    applications (Poultry 9, Dairy 4, Piggery 3, Goatery 1), none on hold. Move
    the status test into COUNT(*) FILTER (…), the shape the pendency-by-sector
    few-shot already uses, so a sector with none shows 0."""
    if schemes != ["CM Elevate"] or not sql or "sector_id" not in sql.lower():
        return sql
    masked = _mask_sql_literals(sql)
    if not _simple_select(masked) or not re.search(r"\bgroup\s+by\b", masked, re.IGNORECASE):
        return sql
    frm = re.search(r"\bfrom\b", masked, re.IGNORECASE)
    sel = re.search(r"\bselect\b", masked, re.IGNORECASE)
    # the key is a literal ('sector_id'), so look in the unmasked text
    if not frm or not sel or "sector_id" not in sql[sel.end():frm.start()].lower():
        return sql
    preds = re.findall(_CME_STATUS_PRED, sql, re.IGNORECASE)
    counts = list(_CME_PLAIN_COUNT_RE.finditer(sql[:frm.start()]))
    if len(preds) != 1 or len(counts) != 1:
        return sql
    pred = preds[0]
    c = counts[0]
    out = sql[:c.end()] + f" FILTER (WHERE {pred})" + sql[c.end():]
    out = _drop_where_conjunct(out, re.escape(pred))
    if len(re.findall(re.escape(pred), out, re.IGNORECASE)) != 1:
        return sql  # the predicate sat somewhere the conjunct remover cannot reach
    logger.info("CM Elevate: moved %s into COUNT FILTER so zero-count sectors stay listed", pred)
    return out


# How an officer names each programme — used to tell whether a question named one.
_CME_PROGRAMME_WORDS = {
    "PRIME Small Enterprise Empowerment and Development (SEED)": r"small enterprise|\bseed\b",
    "Meghalaya Piggery Development Scheme": r"piggery", "Meghalaya Poultry Farming Scheme": r"poultry",
    "PRIME Tourism Vehicle Scheme": r"tourism vehicle",
    "PRIME Agriculture Response Vehicle Scheme": r"agriculture response|response vehicle",
    "Meghalaya Dairy Development Scheme": r"dairy", "Meghalaya Any Business Venture Scheme": r"any business",
    "Meghalaya Goat Farming Scheme": r"goat", "Meghalaya Warehouse Scheme": r"warehouse",
    "Meghalaya Sericulture & Weaving Scheme": r"sericulture|weaving", "Chief Minister's Green Taxi Scheme": r"green taxi",
    "Meghalaya Sports & Wellness Centre Scheme": r"sports|wellness", "Agro Tourism Villa Scheme": r"agro[\s-]?tourism",
    "Meghalaya Cinema Theatre Scheme": r"cinema", "Meghalaya Motorcaravan Scheme": r"motor\s?caravan",
}
_CME_SECTOR_WHOLE_Q = re.compile(r"\b(?:each|every|all|by|wise|breakdown|distribution)\b[^?]{0,20}\bsectors?\b|"
                                 r"\bsectors?[\s-]*(?:wise|breakdown|distribution)\b", re.IGNORECASE)


def _cm_elevate_sector_all_programmes(question: str, schemes: list[str], sql: str) -> str:
    """KI-111 (CM Elevate all-blocks run, 2026-09-28): "applicants associated with
    each sector in West Khasi Hills" came back with scheme_name IN ('…Poultry…',
    '…Dairy…') — Goat Farming (the only Goatery rows) silently dropped — and
    grouped by scheme_name too, so no sector's total was stated. A sector
    question that names NO programme covers every programme: drop the invented
    scheme filter and the scheme_name grouping. A named programme is kept."""
    if schemes != ["CM Elevate"] or not sql or "sector_id" not in sql.lower() \
            or not _CME_SECTOR_WHOLE_Q.search(question or ""):
        return sql
    q = question or ""
    m = _CME_SCHEME_IN_RE.search(sql)
    lits = [x.replace("''", "'") for x in _SQL_LITERAL.findall(m.group(1))] if m else []
    eq = re.search(r"\s+AND\s+scheme_name\s*=\s*'((?:[^']|'')*)'|\bWHERE\s+scheme_name\s*=\s*'((?:[^']|'')*)'\s+AND\s+",
                   sql, re.IGNORECASE)
    if eq:
        lits.append((eq.group(1) or eq.group(2)).replace("''", "'"))
    if not lits or any(re.search(_CME_PROGRAMME_WORDS.get(x, re.escape(x)), q, re.IGNORECASE) for x in lits):
        return sql
    out = sql
    if m:
        out = _drop_where_conjunct(out, re.escape(m.group(0)))
    if eq:
        out = _drop_where_conjunct(out, r"scheme_name\s*=\s*'" + re.escape(eq.group(1) or eq.group(2)) + "'")
    masked = _mask_sql_literals(out)
    frm = re.search(r"\bfrom\b", masked, re.IGNORECASE)
    if frm and re.search(r"\bscheme_name\b", masked[:frm.start()], re.IGNORECASE):
        head = re.sub(r"\bscheme_name\s*,\s*", "", out[:frm.start()], count=1, flags=re.IGNORECASE)
        out = head + out[frm.start():]
        out = re.sub(r"(\bGROUP\s+BY\s+)scheme_name\s*,\s*", r"\1", out, flags=re.IGNORECASE)
        out = re.sub(r",\s*scheme_name\b(?=[^()]*$)", "", out) if re.search(
            r"\bGROUP\s+BY\b[^()]*,\s*scheme_name\b", out, re.IGNORECASE) else out
        out = re.sub(r"(\bORDER\s+BY\s+)scheme_name\s*,\s*", r"\1", out, flags=re.IGNORECASE)
    if out != sql:
        logger.info("CM Elevate: sector question names no programme — dropped the invented scheme filter")
    return out


# the stored district literals (UPPER), used when the resolver catalogue is not loaded
_MEGHALAYA_12_DISTRICTS = ("RI BHOI", "WEST GARO HILLS", "EAST KHASI HILLS", "EAST GARO HILLS", "WEST KHASI HILLS",
                           "SOUTH GARO HILLS", "SOUTH WEST KHASI HILLS", "EASTERN WEST KHASI HILLS",
                           "NORTH GARO HILLS", "WEST JAINTIA HILLS", "SOUTH WEST GARO HILLS", "EAST JAINTIA HILLS")
_CME_LITERAL_COL_RE = re.compile(
    r"\b(scheme_name|lgd_block|lgd_district)\s*\)?\s*(=|I?LIKE|IN\s*\()\s*", re.IGNORECASE)


async def _cm_elevate_fix_literals(schemes: list[str], sql: str) -> str:
    """KI-112 (CM Elevate all-blocks run, 2026-09-28): the generator mistyped stored
    names inside SQL literals — `scheme_name IN ('PRIME Tourism Vehicle', …)` (no
    " Scheme": Mawpat's 54 dropped from a two-programme answer), `lgd_block =
    'SHILLONG-MUNICIPAL_BOARD'` (0 instead of 11), `lgd_district = 'WEST JAINTEIA
    HILLS'` (0). A literal that matches no stored value is snapped to the ONE stored
    value it clearly names: a programme by its words (_CME_PROGRAMME_WORDS), a block
    by its letters (punctuation / underscore / spacing ignored), a district by a
    single close spelling. Anything that matches nothing unambiguously is left."""
    if schemes != ["CM Elevate"] or not sql or not _CME_LITERAL_COL_RE.search(sql):
        return sql
    import difflib
    try:
        blocks = await _cm_elevate_block_names()
    except Exception:  # noqa: BLE001
        blocks = []
    districts = [str(d).upper() for d in canonical_names("CM Elevate", "district")] or list(_MEGHALAYA_12_DISTRICTS)
    programmes = list(_CME_PROGRAMME_WORDS)
    sq = lambda x: re.sub(r"[^A-Z0-9]", "", str(x).upper())  # noqa: E731

    def fix(col: str, lit: str) -> "str | None":
        v = lit.replace("''", "'")
        if col == "scheme_name":
            if v in programmes:
                return None
            hits = [p for p in programmes if re.search(_CME_PROGRAMME_WORDS[p], v, re.IGNORECASE)]
            return hits[0] if len(hits) == 1 else None
        if col == "lgd_block":
            if not blocks or v.upper() in blocks:
                return None
            hits = [b for b in blocks if sq(b) == sq(v)]
            return hits[0] if len(hits) == 1 else None
        if v.upper() in districts or not districts:
            return None
        same = [d for d in districts if sq(d) == sq(v)]       # "RI-BHOI" -> "RI BHOI"
        if len(same) == 1:
            return same[0]
        # a clear winner: "WEST JAINTEIA HILLS" is 0.97 to West and 0.92 to East Jaintia
        scored = sorted(((difflib.SequenceMatcher(None, v.upper(), d).ratio(), d) for d in districts),
                        reverse=True)
        best, second = scored[0], (scored[1] if len(scored) > 1 else (0.0, ""))
        return best[1] if best[0] >= 0.9 and best[0] - second[0] >= 0.04 else None

    out, pos, changed = [], 0, []
    for m in _CME_LITERAL_COL_RE.finditer(sql):
        col = m.group(1).lower()
        j = m.end()
        while True:
            lm = re.match(r"'((?:[^']|'')*)'", sql[j:])
            if not lm:
                break
            new = fix(col, lm.group(1))
            if new is not None:
                out.append(sql[pos:j] + "'" + new.replace("'", "''") + "'")
                pos = j + lm.end()
                changed.append((lm.group(1), new))
            j += lm.end()
            sep = re.match(r"\s*,\s*", sql[j:])
            if not m.group(2).upper().startswith("IN") or not sep:
                break
            j += sep.end()
    if not changed:
        return sql
    logger.info("CM Elevate: snapped mistyped literals to stored names %s", changed)
    return "".join(out) + sql[pos:]


_CME_FAMILY_WORDS_RE = re.compile(r"\b(?:vehicle|livestock|animal|prime|tourism|enterprise|business|agri\w*|"
                                  r"farming|family|group)\s+(?:schemes?|programmes?|programs?)\b|"
                                  r"\b(?:schemes?|programmes?|programs?)\s+(?:like|such as|including)\b",
                                  re.IGNORECASE)


def _cm_elevate_unasked_programme_filter(question: str, schemes: list[str], sql: str) -> str:
    """KI-116 (CM Elevate all-villages run, 2026-09-28): "applicants under each CM
    ELEVATE program in MIKKA SIMDAM, Resubelpara block" came back with
    scheme_name IN ('…Piggery…', '…Poultry…', …) — a list nobody asked for — and
    the village's only programme (Agriculture Response Vehicle) fell outside it:
    "no matching records". A programme filter whose programmes the question never
    names is dropped (the per-programme grouping stays). A question naming a
    programme, or a programme FAMILY ("vehicle schemes"), keeps its filter."""
    if schemes != ["CM Elevate"] or not sql or _CME_FAMILY_WORDS_RE.search(question or ""):
        return sql
    q = question or ""
    m = _CME_SCHEME_IN_RE.search(sql)
    if not m:
        return sql
    lits = [x.replace("''", "'") for x in _SQL_LITERAL.findall(m.group(1))]
    if not lits or any(re.search(_CME_PROGRAMME_WORDS.get(x, re.escape(x)), q, re.IGNORECASE) for x in lits):
        return sql
    out = _drop_where_conjunct(sql, re.escape(m.group(0)))
    if out != sql:
        logger.info("CM Elevate: dropped a programme IN-list the question never named: %s", lits)
    return out


_CME_PERSON_COUNT_Q = re.compile(r"\b(?:applicants?|beneficiar(?:y|ies)|unique\s+applications?|distinct\s+applications?)\b",
                                 re.IGNORECASE)


def _cm_elevate_applicants_distinct(question: str, schemes: list[str], sql: str) -> str:
    """CM Elevate "applicants" = COUNT(DISTINCT request_id): about 36 request ids
    repeat (all Piggery / Poultry), so COUNT(*) over-counts them (schema rule,
    "applications" alone is COUNT(*)). The prose rule did not hold: OFF-009 full
    re-run 2026-10-02 — "applicants in Ri Bhoi under Goat Farming and Poultry
    Farming" ran COUNT(*) AS applicants, 242 Poultry against 241 (8 such pairs;
    Piggery Ri Bhoi 1,944 against 1,935). A COUNT(*) over v_cm_elevate becomes the
    distinct count when the question asks for applicants / beneficiaries."""
    if schemes != ["CM Elevate"] or not sql or "v_cm_elevate" not in sql.lower() \
            or "v_cm_elevate_" in sql.lower() or not _CME_PERSON_COUNT_Q.search(question or ""):
        return sql
    out = re.sub(r"\bCOUNT\s*\(\s*\*\s*\)", "COUNT(DISTINCT request_id)", sql, flags=re.IGNORECASE)
    if out != sql:
        logger.info("CM Elevate: applicants counted with COUNT(*) — counted DISTINCT request_id")
    return out


_SCHEME_EQ_RE = re.compile(r"\bscheme_name\s*=\s*'((?:[^']|'')*)'", re.IGNORECASE)


def _cm_unasked_programme_beside_village(question: str, schemes: list[str], entity_result: dict,
                                         sql: str) -> str:
    """A village name read as a programme (live 2026-10-02, all-villages run):
    "How many applications are there under CM Elevate Legacy in NOAGRE?" — the
    village resolved (village_code 273728) and the 30B wrote, on the first
    attempt and on all three repairs, WHERE scheme_name = 'Meghalaya New
    Agriculture and Allied Activities Scheme' (a name it built from the letters
    N-O-AGRE). A scheme_name = '…' filter is dropped when a village is resolved,
    no programme was resolved, and the question names no programme or family;
    _pin_missing_village_code then adds the village."""
    if schemes not in (["CM Elevate"], ["CM Elevate Legacy"]) or not sql:
        return sql
    resolved = entity_result.get("resolved") or {}
    if not resolved.get("village_code") or resolved.get("cm_scheme") or _CME_FAMILY_WORDS_RE.search(question or ""):
        return sql
    if resolve_cm_scheme(question, schemes[0]) is not None:
        return sql
    m = _SCHEME_EQ_RE.search(sql)
    if not m:
        return sql
    lit = m.group(1).replace("''", "'")
    # Not a stored programme at all: the village's own name written as one
    # ("…WHERE village_code = 279360 AND scheme_name = 'AMLARI MODEL'" — CM Elevate
    # Legacy all-villages re-run 2026-10-02, 9 villages answered 0). Dropped even
    # though the name is in the question: it is in the question AS THE VILLAGE.
    from app.entity_resolver import _catalog as _er_catalog
    _progs = {str(v.get("canonical", "")).strip().upper() for v in _er_catalog.get(schemes[0], {}).get("cm_scheme", [])}
    if _progs and lit.strip().upper() not in _progs:
        out = _drop_where_conjunct(sql, re.escape(m.group(0)))
        if out != sql:
            logger.info("%s: dropped scheme_name = %r — not a programme (a place name)", schemes[0], lit)
        return out
    if re.search(_CME_PROGRAMME_WORDS.get(lit, re.escape(lit)), question or "", re.IGNORECASE):
        return sql
    out = _drop_where_conjunct(sql, re.escape(m.group(0)))
    if out != sql:
        logger.info("%s: dropped a programme filter beside a resolved village the question never named: %r",
                    schemes[0], lit)
    return out


def _pin_missing_village_code(schemes: list[str], entity_result: dict, sql: str) -> str:
    """The deterministic half of _village_filter_missing: a simple one-SELECT
    query over a resolved village that filters no village at all gets
    `village_code = <code>` added (every curated view carries it). The guard
    alone sent NOAGRE to "couldn't build a working query" — the 30B repeated the
    same village-less SQL on every repair. Anything more complex (a join, a
    CTE, a subquery) is left to the guard and the repair."""
    code = _village_filter_missing(schemes, entity_result, sql)
    if code is None:
        return _village_name_cond_to_code(schemes, entity_result, sql)
    masked = re.sub(r"'(?:[^']|'')*'", lambda m: "'" + "x" * (len(m.group(0)) - 2) + "'", sql)
    if not _simple_select(masked):
        return sql
    w = re.search(r"\bWHERE\b", masked, re.IGNORECASE)
    if w:
        out = f"{sql[:w.end()]} village_code = {code} AND{sql[w.end():]}"
    else:
        t = re.search(r"\b(?:GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT)\b|;|\s*$", masked, re.IGNORECASE)
        out = f"{sql[:t.start()].rstrip()}\nWHERE village_code = {code}\n{sql[t.start():].lstrip()}".rstrip()
    logger.info("%s: the SQL filtered no village — pinned the resolved village_code %s", schemes[0], code)
    return _drop_geo_beside_pinned_village(out)


# UPPER( / TRIM( / an alias too: "WHERE UPPER(TRIM(lgd_district)) = 'KASHARIPARA'"
# (the village name in the district column, no village filter) got the code
# pinned but kept the district literal, which the old `UPPER(col)` pattern
# missed — 0 producer groups for a village with 11 (Focus Legacy all-villages
# re-run 2026-10-02).
_PINNED_GEO_COND = (r"(?:(?:UPPER|LOWER|TRIM)\s*\(\s*)*(?:\w+\.)?{col}(?:\s*\))*\s*=\s*"
                    r"(?:(?:UPPER|LOWER|TRIM)\s*\(\s*)*'(?:[^']|'')*'(?:\s*\))*")


def _drop_geo_beside_pinned_village(sql: str) -> str:
    # the village is the whole scope: a block / district literal beside it can
    # only narrow it wrongly (the same rule as _mgnrega_drop_geo_beside_village)
    for col in ("lgd_block", "lgd_district"):
        sql = _drop_where_conjunct(sql, _PINNED_GEO_COND.format(col=col))
    return sql


_VILLAGE_NAME_COND_RE = re.compile(
    r"(?:(?:UPPER|LOWER|TRIM)\s*\(\s*)*(?:\w+\.)?lgd_village_name(?:\s*\))*\s*(?:=|I?LIKE)\s*"
    r"(?:(?:UPPER|LOWER)\s*\(\s*)?'(?:[^']|'')*'(?:\s*\))?", re.IGNORECASE)


def _village_name_cond_to_code(schemes: list[str], entity_result: dict, sql: str) -> str:
    """One resolved village the SQL names by TEXT only: the condition becomes
    `village_code = <code>`. "TEPORPARA ( UPPER )" (code 273637) was written as
    `UPPER(lgd_village_name) = 'TEPORPARA'` — the generator cut the name at the
    parenthesis — and matched a different village: 2 applications for a village
    with 1 (CM Elevate Legacy all-villages re-run 2026-10-02). The resolved code
    is the exact place; a name can be truncated, re-cased or shared by twins.
    Only a simple one-SELECT query with exactly one such condition and no
    village_code filter of its own."""
    if not sql or re.search(r"\bvillage_code\b", sql, re.IGNORECASE):
        return sql
    conds = list(_VILLAGE_NAME_COND_RE.finditer(sql))
    if len(conds) != 1:
        return sql
    m = conds[0]
    code = _village_filter_missing(schemes, entity_result, sql[:m.start()] + sql[m.end():])
    if code is None:
        return sql
    masked = re.sub(r"'(?:[^']|'')*'", lambda x: "'" + "x" * (len(x.group(0)) - 2) + "'", sql)
    if not _simple_select(masked):
        return sql
    out = f"{sql[:m.start()]}village_code = {code}{sql[m.end():]}"
    logger.info("%s: village named by text (%r) — replaced by the resolved village_code %s",
                schemes[0], m.group(0), code)
    return _drop_geo_beside_pinned_village(out)


_CME_LEVEL_PENDING_Q = re.compile(
    r"\bpending\b[^?.]{0,25}?\blevel\s*-?\s*\d|\blevel\s*-?\s*\d\b[^?.]{0,25}?\bpending\b", re.IGNORECASE)
_CME_ON_HOLD_PRED = r"(?:\w+\.)?data_verified\s*=\s*'On Hold'"


def _cm_elevate_level_pending(question: str, schemes: list[str], sql: str) -> str:
    """KI-070 (CM-ELEVATE-OFF-018a): "applications pending at level 2" was read
    as level 2 AND data_verified = 'On Hold' — 0 rows (no level-2 application
    is on hold) — and composed as "the data doesn't cover" it. An application
    pending AT a level is one whose file currently sits at that level
    (current_level); all 165 level-2 applications have file_status 'Pending'.
    Drop the On Hold conjunct when the level itself is the scope. An explicit
    "on hold" in the question keeps it."""
    if (schemes != ["CM Elevate"] or not sql or not _CME_LEVEL_PENDING_Q.search(question or "")
            or re.search(r"\bon[\s-]?hold\b", question or "", re.IGNORECASE)
            or not re.search(r"\bcurrent_level\b", sql, re.IGNORECASE)
            or not re.search(_CME_ON_HOLD_PRED, sql, re.IGNORECASE)):
        return sql
    out = _drop_where_conjunct(sql, _CME_ON_HOLD_PRED)
    if out != sql:
        logger.info("CM Elevate: 'pending at level N' — dropped the On Hold filter, level is the scope")
    return out


# Product decision 2026-09-28 (tester sheet OFF-015/016/017/030): CM Elevate
# "status" = current_file_status, send-back spellings merged (rule 7).
_CME_FILE_STATUS_EXPR = ("CASE WHEN LOWER(current_file_status) LIKE 'sendback%' THEN 'Sent back' "
                         "WHEN current_file_status IS NULL THEN '(not recorded)' "
                         "ELSE INITCAP(LOWER(current_file_status)) END")
_CME_STATUS_Q = re.compile(r"\bstatus[\s-]*(?:wise|distribution|breakdown|split|counts?)\b|"
                           r"\b(?:application|current|file|overall)\s+status\b|\bstatus\s+(?:of|for|in)\b|"
                           r"\bby\s+status\b|\beach\s+status\b", re.IGNORECASE)
_CME_VERIFICATION_Q = re.compile(r"verif\w*|\bvalid\b|\binvalid\b|\bwrong\b|on[\s-]?hold|\bpending\b|\bpendency\b|"
                                 r"\bapproved?\b|\brejected\b|\bdecision\b|\bwithdraw\w*", re.IGNORECASE)
_DV_GROUP_EXPR_RE = re.compile(
    r"COALESCE\s*\(\s*(?:\w+\.)?data_verified\s*,\s*'(?:[^']|'')*'\s*\)|(?<![\w.'])(?:\w+\.)?data_verified\b"
    r"(?!\s*(?:=|<>|!=|IS\b|IN\b|I?LIKE\b))", re.IGNORECASE)


def _cm_elevate_status_is_file_status(question: str, schemes: list[str], sql: str) -> str:
    """A plain CM Elevate "status" question grouped by data_verified is rewritten
    to group by current_file_status. Comparisons on data_verified (a verification
    filter the question asked for) are never touched; a question with any
    verification / pending / on-hold / decision word keeps data_verified."""
    q = question or ""
    if (schemes != ["CM Elevate"] or not sql or not _CME_STATUS_Q.search(q) or _CME_VERIFICATION_Q.search(q)
            or not re.search(r"\bGROUP\s+BY\b", sql, re.IGNORECASE) or "data_verified" not in sql.lower()):
        return sql
    masked = _mask_sql_literals(sql)
    frm = re.search(r"\bFROM\b", masked, re.IGNORECASE)
    if not frm or not _DV_GROUP_EXPR_RE.search(sql[:frm.start()]):
        return sql                       # data_verified only inside a WHERE / FILTER — not the grouping
    head = _DV_GROUP_EXPR_RE.sub(_CME_FILE_STATUS_EXPR, sql[:frm.start()])
    tail = sql[frm.start():]
    gb = re.search(r"\bGROUP\s+BY\b(.*?)(?=\bORDER\s+BY\b|\bHAVING\b|\bLIMIT\b|$)", tail, re.IGNORECASE | re.DOTALL)
    if gb:
        tail = tail[:gb.start(1)] + _DV_GROUP_EXPR_RE.sub(_CME_FILE_STATUS_EXPR, gb.group(1)) + tail[gb.end(1):]
    out = head + tail
    if out != sql:
        logger.info("CM Elevate: 'status' grouped by current_file_status, not data_verified (2026-09-28)")
    return out


_CME_ANY_LEVEL_PRED = r"(?:LOWER\s*\(\s*)?(?:\w+\.)?current_level\s*\)?\s*=\s*'(?:[^']|'')*'"


def _cm_elevate_unasked_level_filter(question: str, schemes: list[str], sql: str) -> str:
    """KI-120 (final pass 2026-09-28): "pending in QUININE NONGLADEW, Umling block"
    got `current_level = 'quinary'` — a level the village name suggested to the
    generator — and a confident 0 against 2. A level filter the question never
    asks for (no "level" / "stage") is dropped."""
    if schemes != ["CM Elevate"] or not sql or re.search(r"\blevels?\b|\bstages?\b|\blevel\s*-?\d", question or "",
                                                          re.IGNORECASE):
        return sql
    if not re.search(_CME_ANY_LEVEL_PRED, sql, re.IGNORECASE):
        return sql
    out = _drop_where_conjunct(sql, _CME_ANY_LEVEL_PRED)
    if out != sql:
        logger.info("CM Elevate: dropped a current_level filter the question never asked for")
    return out


_CME_DECISION_PRED = re.compile(
    r"(?:(?:LOWER|UPPER)\s*\(\s*)?(?:\w+\.)?current_file_status\s*\)?\s*(?:=|I?LIKE)\s*"
    r"'(approved|rejected|pending)'", re.IGNORECASE)


def _cm_elevate_decision_status_column(schemes: list[str], sql: str) -> str:
    """KI-074 follow-on (found by a generalisation probe, 2026-09-27): "how many
    applications were rejected in Ri Bhoi" filtered current_file_status =
    'rejected' — a value that column never holds (forward / sendback /
    resubmit) — and a confident 0 went out against a true 27. The decision
    states Approved / Rejected / Pending live only in scheme_specific ->>
    'file_status'; point the predicate there."""
    if schemes != ["CM Elevate"] or not sql or not _CME_DECISION_PRED.search(sql):
        return sql
    out = _CME_DECISION_PRED.sub(
        lambda m: f"scheme_specific ->> 'file_status' = '{m.group(1).title()}'", sql)
    logger.info("CM Elevate: approved/rejected/pending moved from current_file_status to file_status")
    return out


_CME_LEVEL_PRED = r"(?:LOWER\s*\(\s*)?(?:\w+\.)?current_level\s*\)?\s*=\s*'level\d'"


def _cm_elevate_pending_without_level(question: str, schemes: list[str], sql: str) -> str:
    """The other side of KI-070, found in its own re-test (2026-09-27): "which
    programs have the highest number of pending applications" — no level named —
    came back as current_level = 'level1' (SEED 3,633 "pending" against a true
    286 on hold). A plain "pending" is data_verified = 'On Hold' (vocabulary);
    a level filter the question never asked for is swapped back to it."""
    q = question or ""
    if (schemes != ["CM Elevate"] or not sql or not re.search(r"\bpending\b|\bpendency\b", q, re.IGNORECASE)
            or re.search(r"\blevel\s*-?\s*\d|\blevels?\b|\bstage\b", q, re.IGNORECASE)
            or re.search(_CME_ON_HOLD_PRED, sql, re.IGNORECASE)
            or len(re.findall(_CME_ANY_LEVEL_PRED, sql, re.IGNORECASE)) != 1):
        return sql
    # any level literal, not just 'level<d>': "pending in 12TH MER" came back as
    # current_level = 'level12' (final pass 2026-09-28, KI-120)
    logger.info("CM Elevate: plain 'pending' was filtered on current_level — restored data_verified = 'On Hold'")
    return re.sub(_CME_ANY_LEVEL_PRED, "data_verified = 'On Hold'", sql, flags=re.IGNORECASE)


# CM Elevate Legacy constituency SQL joins curated.dim_geography for ac_name, and
# the view already carries every geography column dim_geography has
# (lgd_district, lgd_block, …). An unqualified one is then "ambiguous" to
# Postgres, and the repair loop kept re-emitting it until the budget ran out
# (TC-31 "applications mapped to Mairang constituency", 2026-09-25). The view's
# copy is always the intended one, so qualify bare references with its alias.
_CML_VIEW_ALIAS_RE = re.compile(
    r"\bFROM\s+curated\.v_cm_elevate_disbursement\s+(?:AS\s+)?(?!JOIN\b|WHERE\b|GROUP\b|ORDER\b|LIMIT\b)(\w+)",
    re.IGNORECASE)
_DIM_GEO_JOIN_RE = re.compile(r"\bJOIN\s+curated\.dim_geography\b", re.IGNORECASE)
_SHARED_GEO_COLS = ("lgd_district", "lgd_block", "lgd_village_name", "village_code",
                    "entity_type", "on_roster", "has_geo_conflict")


def _cm_legacy_qualify_shared_geo_cols(schemes: list[str], sql: str) -> str:
    if schemes != ["CM Elevate Legacy"] or not _DIM_GEO_JOIN_RE.search(sql or ""):
        return sql
    m = _CML_VIEW_ALIAS_RE.search(sql)
    if not m:
        return sql
    alias = m.group(1)
    out = sql
    for col in _SHARED_GEO_COLS:
        # Leave output aliases ("AS lgd_district") and already-qualified refs alone.
        out = re.sub(rf"(\bAS\s+)?(?<![\w.]){col}\b",
                     lambda mm, c=col: mm.group(0) if mm.group(1) else f"{alias}.{c}",
                     out, flags=re.IGNORECASE)
    if out != sql:
        logger.info("CM Elevate Legacy: qualified view geography columns with %r "
                    "(dim_geography join)", alias)
    return out


# CM Elevate Legacy: "sanctioned applications" = COUNT(sanctioned_amount). 3 Any
# Business Venture records (marked Refused) carry no sanctioned amount, so
# COUNT(*) overstates it. The rule sat in schema_context (rule 2) and the
# few-shots, and still failed 5 of 5 live runs: "How many applications have been
# sanctioned?" ran SELECT COUNT(*) AS sanctioned_records and answered 2,823 against
# a true 2,820 (use-case re-test TC-14, 2026-09-29, KI-166). Fixed in the SQL.
_CML_SANCTION_COUNT_Q = re.compile(
    r"\bhow\s+many\b[^?.]*\bsanction(?:ed)?\b|\bnumber\s+of\s+sanction(?:ed)?\b|"
    r"\bsanctioned\s+(?:applications?|cases?|records?|applicants?|beneficiar\w+|entrepreneurs?|units?)\b|"
    r"\bcount\s+(?:of\s+)?sanction(?:ed)?\b",
    re.IGNORECASE)
_CML_NOT_SANCTIONED_Q = re.compile(r"\b(?:not|never|non|un)[\s-]?sanction|\bdesanction|\bwithout\s+(?:a\s+)?sanction",
                                   re.IGNORECASE)
# COUNT(*) directly aliased as a sanctioned count; a not_ / non_ / un / de- alias
# (not_sanctioned_records, desanctioned) is a different measure and is left alone,
# and COUNT(*) FILTER (...) is not matched because AS must follow the parenthesis.
_CML_COUNT_STAR_SANCTIONED = re.compile(
    r"\bCOUNT\s*\(\s*\*\s*\)(\s+AS\s+(?!(?:not|non|un|de)_?)[a-z_]*sanction\w*)", re.IGNORECASE)
_CML_COUNT_STAR_ANY = re.compile(r"\bCOUNT\s*\(\s*\*\s*\)\s+AS\s+(\w+)", re.IGNORECASE)


def _cm_legacy_sanctioned_count(question: str, schemes: list[str], sql: str) -> str:
    if schemes != ["CM Elevate Legacy"] or not sql:
        return sql
    q = question or ""
    if not _CML_SANCTION_COUNT_Q.search(q) or _CML_NOT_SANCTIONED_Q.search(q):
        return sql
    if re.search(r"\bsanctioned\s+amount\b|\bamount\s+sanctioned\b", q, re.IGNORECASE) \
            and not re.search(r"\bhow\s+many\b|\bnumber\s+of\b|\bcount\b", q, re.IGNORECASE):
        return sql
    out = _CML_COUNT_STAR_SANCTIONED.sub(r"COUNT(sanctioned_amount)\1", sql)
    masked = _mask_sql_literals(out)
    if out == sql and "sanction" not in masked.lower():
        # No sanction column anywhere: the only count is the sanctioned count the
        # question asked for ("SELECT COUNT(*) AS records ... " read as "N sanctioned").
        stars = list(_CML_COUNT_STAR_ANY.finditer(masked))
        if len(stars) == 1:
            m = stars[0]
            head = sql[:m.start()] + "COUNT(sanctioned_amount) AS sanctioned_records"
            # the old alias may be reused later (ORDER BY records DESC)
            out = head + re.sub(rf"\b{re.escape(m.group(1))}\b", "sanctioned_records", sql[m.end():])
    if out != sql:
        logger.info("CM Elevate Legacy: sanctioned count is COUNT(sanctioned_amount), not COUNT(*) (KI-166)")
    return out


# CM Elevate Legacy: a ranking over a small dimension cut by a LIMIT the question
# never asked for. "Which schemes have the highest number of applications?" ran
# ... ORDER BY records DESC LIMIT 10 in 4 of 5 live runs — 3 of the 13 schemes
# dropped — and the answer then called the 10th row the bottom of the range
# ("down to 33 records", true lowest Motorcaravan 1; TC-13, 2026-09-29, KI-168).
# The top-N pause (_needs_topn_clarification) only knows districts / blocks /
# villages, so a scheme ranking reaches the generator without a length. Without a
# stated count the whole list is returned (the executor's own cap still applies);
# a singular "which scheme has the most" keeps its LIMIT 1.
_CML_TRAILING_LIMIT = re.compile(r"\s+LIMIT\s+(\d+)\s*;?\s*$", re.IGNORECASE)
_CML_PLURAL_DIM_Q = re.compile(
    r"\b(?:schemes|districts|blocks|villages|constituencies|entities|lenders|banks|years|sectors)\b",
    re.IGNORECASE)


# CM Elevate Legacy: a GROUP BY whose columns are not selected. "How many
# applications in CHIOKGRE village … across all financial years" ran
#   SELECT COUNT(*) AS records … WHERE village_code = 275499
#   GROUP BY COALESCE(financial_year_short, '(no financial year)') ORDER BY financial_year_short
# — invalid (ORDER BY an ungrouped column), and the repair model returned the same
# SQL 4 times: "couldn't build a working query" (final pass 2026-09-29). Unlabelled
# per-group counts can never be the answer, so the grouping (and its ORDER BY) is
# dropped and the single total asked for is returned.
_CML_GROUP_TAIL_RE = re.compile(r"\s+GROUP\s+BY\s+(?P<g>.*?)(?P<rest>\s+(?:HAVING|ORDER\s+BY|LIMIT)\b.*|\s*;?\s*)$",
                                re.IGNORECASE | re.DOTALL)


def _cm_legacy_unselected_group_by(schemes: list[str], sql: str) -> str:
    if schemes != ["CM Elevate Legacy"] or not sql:
        return sql
    masked = _mask_sql_literals(sql)
    if not _simple_select(masked) or re.search(r"\bHAVING\b", masked, re.IGNORECASE):
        return sql
    m = _CML_GROUP_TAIL_RE.search(masked)
    sel = re.search(r"\bSELECT\b(.*?)\bFROM\b", masked, re.IGNORECASE | re.DOTALL)
    if not m or not sel:
        return sql
    cols = set(re.findall(r"\b(?:[a-z_]+\.)?([a-z_][a-z0-9_]*)\b", m.group("g").lower())) - {
        "coalesce", "upper", "lower", "trim", "as"}
    selected = set(re.findall(r"\b([a-z_][a-z0-9_]*)\b", sel.group(1).lower()))
    if not cols or cols & selected:
        return sql
    rest = re.sub(r"^\s*ORDER\s+BY\b.*?(?=\s+LIMIT\b|\s*;?\s*$)", "", sql[m.start("rest"):],
                  flags=re.IGNORECASE | re.DOTALL)
    logger.info("CM Elevate Legacy: dropped a GROUP BY on unselected %s", sorted(cols))
    return sql[:m.start()] + rest


def _cm_legacy_unrequested_limit(question: str, schemes: list[str], sql: str) -> str:
    if schemes != ["CM Elevate Legacy"] or not sql:
        return sql
    m = _CML_TRAILING_LIMIT.search(sql)
    masked = _mask_sql_literals(sql)
    if not m or not re.search(r"\bGROUP\s+BY\b", masked, re.IGNORECASE):
        return sql
    q = question or ""
    if _EXPLICIT_COUNT.search(q) or re.search(r"\b(?:top|bottom|first|last)\s+\d+\b", q, re.IGNORECASE):
        return sql
    n = int(m.group(1))
    if n == 1 and not _CML_PLURAL_DIM_Q.search(q):
        return sql
    logger.info("CM Elevate Legacy: dropped an unrequested LIMIT %d from a grouped result (KI-168)", n)
    return sql[:m.start()]


# Focus Legacy: a group's member count is recorded on EACH of its payments, so
# "how many members does <group> have" / "groups with more than N members" is the
# group's SIZE — MAX(no_of_pg_members) per pg_id. The generator SUMmed it across
# the group's payments instead, and 2,655 groups were paid more than once: a
# 20-member group paid twice read "40 members", the 190-member outlier "201"
# (use-case QA TC-14b / TC-15, 2026-09-25). SUM(no_of_pg_members) stays correct
# for memberships PAID FOR (a district / year / scheme total), so this fires only
# when the SQL works at group grain — GROUP BY pg_id, or a filter on one group.
_FL_GROUP_SIZE_Q = re.compile(
    r"\bhow\s+many\s+members\b|\bnumber\s+of\s+members\s+(?:in|of)\b|"
    r"\b(?:more|less|fewer)\s+than\s+\d+\s+(?:of\s+)?members\b|"
    r"\b(?:over|above|under|below|at\s+least|at\s+most)\s+\d+\s+members\b|"
    r"\bgroup\s+size\b|\bmember\s+count\b|\b(?:largest|biggest|smallest)\s+(?:producer\s+)?groups?\b",
    re.IGNORECASE)
_FL_MONEY_Q = re.compile(r"\bpaid\b|\bmemberships\b|\bamount\b|\bdisburs\w*|\breceiv\w*|\bmoney\b|"
                         r"\bremit\w*|\brupees?\b|₹|\brs\.?\s*\d", re.IGNORECASE)
_FL_SUM_MEMBERS = re.compile(r"\bSUM\s*\(\s*(?:\w+\.)?no_of_pg_members\s*\)", re.IGNORECASE)
_FL_GROUP_GRAIN = re.compile(
    r"\bGROUP\s+BY\s+(?:[\w.]+\s*,\s*)*(?:\w+\.)?pg_id\b|\b(?:\w+\.)?pg_(?:name|id)\s*(?:=|ILIKE|LIKE|IN)\b",
    re.IGNORECASE)


# Focus Legacy: the view's audit copies block_name_raw / district_name_raw are NULL
# on every row since the 2026-09-25 database change (the curated lgd_block /
# lgd_district were fixed to carry the source values instead). A query filtering
# on them returns nothing: "How many Producer Groups are mapped to Nongstoin
# block?" answered "no matching records" against a true 460 (bulk block QA, 3 of
# 168 questions). Read the curated columns, which hold the same values.
_FL_RAW_GEO_COL = re.compile(r"\b(block|district)_name_raw\b", re.IGNORECASE)


def _focus_legacy_geo_columns(schemes: list[str], sql: str) -> str:
    if schemes != ["Focus Legacy"] or not _FL_RAW_GEO_COL.search(sql or ""):
        return sql
    out = _FL_RAW_GEO_COL.sub(lambda m: f"lgd_{m.group(1).lower()}", sql)
    logger.info("Focus Legacy: block/district_name_raw -> lgd_block/lgd_district (raw copies are empty)")
    return out


# NIKWATGRE, RONGARA block: the village chip pinned village_code 276311, and the
# model ALSO filtered lgd_block = 'RONGBARA' (its own misspelling) — ₹0 / null
# for a true ₹95,000 (all-villages run 2026-09-29, KI-159). A village code fixes
# the block and district, so beside it those literals can only be wrong.
def _focus_legacy_village_code_only(schemes: list[str], resolved: dict, sql: str) -> str:
    code = (resolved or {}).get("village_code")
    if schemes not in (["Focus Legacy"], ["CM Elevate Legacy"]) or code is None \
            or isinstance(code, (list, tuple)) or not re.search(
            rf"\bvillage_code\s*=\s*'?{re.escape(str(code))}\b", sql or ""):
        return sql
    # CM Elevate Legacy also drops a village-name literal: for the ward "William
    # Nagar (MB) - Ward No.10" the generator wrote lgd_village_name = 'WILLIAM
    # NAGAR-MUNICIPAL BOARD' (the block) beside village_code = 70681 and answered
    # 0 for a ward holding 1 record (all-villages run 2026-09-29).
    cols = "block|district|village_name" if schemes == ["CM Elevate Legacy"] else "block|district"
    geo = rf"(?:UPPER\s*\(\s*)?(?:\b\w+\.)?lgd_(?:{cols})(?:\s*\))?\s*=\s*(?:UPPER\s*\(\s*)?'(?:[^']|'')*'(?:\s*\))?" \
        if schemes == ["CM Elevate Legacy"] else r"(?:\b\w+\.)?lgd_(?:block|district)\s*=\s*'[^']*'"
    out = re.sub(rf"\s+AND\s+{geo}", "", sql, flags=re.I)
    out = re.sub(rf"\bWHERE\s+{geo}\s+AND\s+", "WHERE ", out, flags=re.I)
    if out != sql:
        logger.info("%s: dropped place literals beside village_code %s", schemes[0], code)
    return out


# DATE_TRUNC on a DATE returns timestamptz in the session zone (IST), and the
# driver hands it back in UTC: 1 April 2022 00:00 IST became 31 March 18:30 UTC
# and the answer said "₹5.20 crore in March 2022" — every month one early (TC-21,
# final-code re-test 2026-09-29, KI-164). Cast the period start back to a date.
_DATE_TRUNC_RE = re.compile(
    r"(DATE_TRUNC\s*\(\s*'(?:year|quarter|month|week|day)'\s*,\s*[\w.]*date_of_remittance\s*\))(?!\s*::\s*date)",
    re.IGNORECASE)


def _focus_legacy_date_trunc_as_date(schemes: list[str], sql: str) -> str:
    if schemes != ["Focus Legacy"] or not _DATE_TRUNC_RE.search(sql or ""):
        return sql
    return _DATE_TRUNC_RE.sub(r"\1::date", sql)


def _focus_legacy_group_size_summed(question: str, schemes: list[str], sql: str) -> bool:
    return (schemes == ["Focus Legacy"]
            and bool(_FL_GROUP_SIZE_Q.search(question or ""))
            and not _FL_MONEY_Q.search(question or "")
            and bool(_FL_SUM_MEMBERS.search(sql or ""))
            and bool(_FL_GROUP_GRAIN.search(sql or "")))


_VERIFICATION_MENTION = re.compile(r"verif\w*", re.IGNORECASE)
_STATUS_ENUMERATE_AUDIT = re.compile(
    r"what\s+\S+\s+status\s+values|which\s+\S+\s+status\s+values|"
    r"status\s+values\s+are\s+recorded|what\s+status(?:es)?\s+(?:exist|are\s+there)",
    re.IGNORECASE,
)
_GROUP_BY_COLS_RE = re.compile(r"\bGROUP BY\s+([^\n;]+)", re.IGNORECASE)


def _focusplus_drop_unrequested_verification_status(question: str, schemes: list[str],
                                                     sql: str) -> str:
    if schemes != ["Focus Plus"]:
        return sql
    if _VERIFICATION_MENTION.search(question) or _STATUS_ENUMERATE_AUDIT.search(question):
        return sql
    m = _GROUP_BY_COLS_RE.search(sql)
    if not m:
        return sql
    group_cols = [c.strip() for c in m.group(1).split(",")]
    if "focus_status" not in group_cols or "verification_status" not in group_cols:
        return sql
    new_group_by = "GROUP BY " + ", ".join(c for c in group_cols if c != "verification_status")
    sql = sql[:m.start()] + new_group_by + sql[m.end():]
    sql = re.sub(r"\bverification_status\s*,\s*", "", sql, count=1)
    sql = re.sub(r",\s*verification_status\b(?!\s*=)", "", sql, count=1)
    logger.info("dropped unrequested verification_status column from a focus_status breakdown")
    return sql


# A `column "X" does not exist` error usually means the generator picked a view
# that lacks a geography column (e.g. v_pmay_monthly_sanctions) rather than a
# genuine typo. Point the repair at the objects that DO carry the column instead
# of just echoing the Postgres message back.
_MISSING_COL_RE = re.compile(r'column "([\w.]+)" does not exist', re.IGNORECASE)
_COL_HOMES = {
    "lgd_district": "curated.v_pmay, curated.v_employment, curated.v_expenditure, "
                    "curated.v_district_year_summary, curated.v_focus_plus, "
                    "curated.v_cm_elevate, "
                    "curated.v_cross_scheme_money_district_year, curated.dim_geography",
    "lgd_block": "curated.v_pmay, curated.v_employment, curated.v_expenditure, "
                 "curated.v_focus_plus, curated.v_cm_elevate, curated.dim_geography",
    "lgd_village_name": "curated.v_pmay, curated.v_employment, curated.v_expenditure, "
                        "curated.v_focus_plus, curated.v_cm_elevate, curated.dim_geography",
}


# MGNREGA's two facts are separate objects with NO overlapping measures:
# employment (person_days, persons_employed, households_employed,
# households_completed_100_days, job_cards_issued_total) lives on
# curated.v_employment; money (total_exp, unskilled_wage_exp,
# semi_skilled_wage_exp, material_exp) lives on curated.v_expenditure. A
# question that wants one of each ("compare expenditure and person-days in
# RERAPARA") cannot be answered from a single view, and the generator reliably
# tries anyway — selecting person_days off v_expenditure, which errors, and
# then failing to recover because the generic missing-column hint only says
# "select from an object that exposes every column" without explaining that no
# such object exists at block grain (confirmed live 2026-09-17: the question
# burned its whole repair budget and fell through to "couldn't build a working
# query"). docs/schema_for_developers.md: v_district_year_summary is the only
# sanctioned combined object, and it is district x year ONLY — so at block or
# village grain the answer is two CTEs.
_EMPLOYMENT_MEASURES = {
    "person_days", "persons_employed", "households_employed",
    "households_completed_100_days", "job_cards_issued_total",
    "women_employment_provided",
}
_EXPENDITURE_MEASURES = {
    "total_exp", "unskilled_wage_exp", "semi_skilled_wage_exp",
    "material_exp", "tax_exp", "admin_total_exp",
}


def _mgnrega_split_fact_hint(col: str) -> "str | None":
    """The recipe for combining MGNREGA employment and expenditure measures,
    when the missing column is one that lives on the OTHER fact."""
    if col in _EMPLOYMENT_MEASURES:
        wanted, home, other = col, "curated.v_employment", "curated.v_expenditure"
    elif col in _EXPENDITURE_MEASURES:
        wanted, home, other = col, "curated.v_expenditure", "curated.v_employment"
    else:
        return None
    return (
        f'"{wanted}" lives on {home}, NOT on {other} — MGNREGA keeps employment and '
        "expenditure in two separate facts that share no measures, so ONE view can "
        "never supply both. To report a measure from each, aggregate them "
        "independently and join the results:\n"
        "  WITH emp AS (SELECT SUM(person_days) AS person_days FROM curated.v_employment "
        "WHERE <same filters>),\n"
        "       exp AS (SELECT SUM(total_exp) AS total_exp_lakh FROM curated.v_expenditure "
        "WHERE <same filters>)\n"
        "  SELECT exp.total_exp_lakh, emp.person_days FROM emp, exp\n"
        "Put the SAME geography and year_key filters on BOTH CTEs. Add a GROUP BY plus a "
        "FULL OUTER JOIN on the grain columns only if the question asks for a per-district "
        "/ per-block / per-year breakdown. At DISTRICT x YEAR grain you may instead select "
        "both measures directly from curated.v_district_year_summary, which already "
        "combines the two facts; it has no block or village column, so it cannot be used "
        "for a block- or village-level question."
    )


def _missing_column_hint(error: str) -> "str | None":
    m = _MISSING_COL_RE.search(error)
    if not m:
        return None
    col = m.group(1).split(".")[-1].lower()
    split_fact = _mgnrega_split_fact_hint(col)
    if split_fact:
        return split_fact
    if col in ("scheme_key", "scheme_code"):
        # The generator added a scheme filter/join to a per-scheme object that
        # carries neither column. scheme_key / scheme_code live ONLY on
        # curated.dim_scheme and curated.v_cross_scheme_money_district_year.
        # Every row in curated.v_pmay / v_expenditure / v_employment /
        # fact_pmay_house / fact_mgnrega_* already belongs to one scheme, so a
        # single-scheme question needs no scheme filter and no join to
        # dim_scheme at all.
        return (f'"{col}" does not exist on the object the previous query selected from. '
                "It lives ONLY on curated.dim_scheme and "
                "curated.v_cross_scheme_money_district_year. The per-scheme fact tables "
                "and views (curated.v_pmay, curated.v_expenditure, curated.v_employment, "
                "curated.v_focus_plus, curated.v_cm_elevate, curated.fact_pmay_house, "
                "curated.fact_mgnrega_*, curated.fact_focus_plus_disbursement, "
                "curated.fact_cm_elevate_application) are each already a single scheme — "
                "DELETE the scheme filter and any JOIN to curated.dim_scheme entirely, and "
                "keep every other clause (year_key, geography, NOT is_placeholder, "
                "aggregation) exactly as it was.")
    homes = _COL_HOMES.get(col)
    if not homes:
        return (f'The previous query used a column "{col}" that its source object does not '
                "have. Select from a curated object that exposes every column you reference.")
    tail = (" — for a per-district PMAY-G breakdown use curated.v_pmay and GROUP BY lgd_district")
    if col in ("lgd_block", "lgd_village_name"):
        # The usual cause here is a cross-scheme money question at block/village grain
        # pointed at v_cross_scheme_money_district_year, which is district x year only.
        tail = (". For combined MGNREGA + PMAY-G money at block or village grain there is no "
                "cross-scheme view: aggregate curated.v_expenditure and curated.v_pmay to that "
                "grain in separate CTEs (MGNREGA total_exp is LAKH -> /100; PMAY amount_released "
                "is RUPEES -> /1e7, WHERE NOT is_placeholder), then FULL OUTER JOIN the two CTEs "
                "on the grain columns and sum the two crore figures")
    return (f'"{col}" does not exist on the object the previous query selected from. That '
            f"column lives on: {homes}. Rebuild against one of those{tail}.")


# The generator reaches for COUNT(DISTINCT x) OVER (...) on "top N% / decile /
# concentration" questions; Postgres rejects DISTINCT (and nested aggregates)
# inside a window function. A plain error echo doesn't get the model out of the
# pattern — hand it the rank-in-a-CTE recipe explicitly.
_WINDOW_FN_ERR_RE = re.compile(
    r"DISTINCT is not implemented for window functions"
    r"|window function calls cannot contain"
    r"|aggregate function calls cannot contain window function calls",
    re.IGNORECASE,
)


def _window_fn_hint(error: str) -> "str | None":
    if not _WINDOW_FN_ERR_RE.search(error):
        return None
    return (
        "PostgreSQL forbids DISTINCT and nested aggregates inside a window function, so "
        "COUNT(DISTINCT ...) OVER (...) cannot work. Rebuild as: (1) a CTE that aggregates "
        "the metric per unit — SUM(<metric>) AS m ... GROUP BY <unit>; (2) a CTE that ranks "
        "those units — NTILE(<100/percent>) OVER (ORDER BY m DESC) AS bucket (top 10% -> "
        "NTILE(10), quartile -> NTILE(4)); (3) an outer SELECT returning "
        "ROUND(100.0 * SUM(m) FILTER (WHERE bucket = 1) / NULLIF(SUM(m), 0), 1). "
        "Keep every filter (year, scheme, geography) from the failed query on the first CTE."
    )


def _repair_hint(error: str) -> "str | None":
    """The single targeted hint fed to the repair prompt — most specific first."""
    return _missing_column_hint(error) or _window_fn_hint(error)


# curated.v_employment and curated.v_expenditure are at source-row grain (many
# rows per village) — schema_context MGNREGA rule 2: "always SUM ... GROUP BY,
# never read a row raw". The generator sometimes answers an aggregate question
# ("how many job cards", "how much was spent") with a bare
# `SELECT <metric> FROM curated.v_employment WHERE ... LIMIT 1` — it runs clean
# and returns one village's number as if it were the statewide total (the
# "116 job cards for all of Meghalaya" bug). Detect that shape and force a
# repair pass; the hint tells the generator to wrap the metric in SUM(...).
_ROWGRAIN_VIEWS = ("curated.v_employment", "curated.v_expenditure")
_OUTER_SELECT_FROM_RE = re.compile(
    r"^\s*SELECT\b(?P<cols>.*?)\bFROM\b\s+(?P<src>[A-Za-z_][\w.]*)",
    re.IGNORECASE | re.DOTALL,
)
_AGG_CALL_RE = re.compile(r"\b(?:SUM|COUNT|AVG|MIN|MAX)\s*\(", re.IGNORECASE)
_GROUP_BY_RE = re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)


def _rowgrain_no_aggregate(question: str, sql: str) -> "str | None":
    """The source-row-grain view a bare-row aggregate query reads from, or None.

    Fires only when the question is aggregate-shaped, the statement is a plain
    (non-CTE) SELECT whose FROM target is one of the source-row-grain views, the
    outer select list has no aggregate call, and there is no GROUP BY."""
    q = question or ""
    if not (_AGGREGATE_CUE.search(q) or _BARE_METRIC_CUE.search(q)):
        return None
    s = sql.strip()
    if re.match(r"^\s*WITH\b", s, re.IGNORECASE):
        return None
    m = _OUTER_SELECT_FROM_RE.match(s)
    if not m:
        return None
    src = m.group("src").lower().strip('"')
    if src not in _ROWGRAIN_VIEWS:
        return None
    if _AGG_CALL_RE.search(m.group("cols")) or _GROUP_BY_RE.search(s):
        return None
    if re.match(r"^\s*SELECT\s+'", s, re.IGNORECASE):  # canned text answer, not a data read
        return None
    return src


_VILLAGE_CODE_FILTER_RE = re.compile(r"\bvillage_code\s*(?:=|IN)\s*", re.IGNORECASE)
_VILLAGE_NAME_FILTER_RE = re.compile(r"\blgd_village_name\s*(?:=|ILIKE|IN)\s*", re.IGNORECASE)
# The generator also substitutes the WRONG ADMIN LEVEL for a resolved village:
# it writes lgd_block = 'NONGLADEW' or lgd_district = 'NONGLADEW' — a village
# name placed in a block/district column. That matches zero rows and returns a
# clean, confident 0 ("the data doesn't cover applicants in Nongladew") when the
# village has 32 real records. Same root cause as the lgd_village_name case
# above, and just as invisible: non-deterministic across runs, so the same
# question answers district one time and block the next (reported 2026-09-17).
_WRONG_LEVEL_FILTER_RE = re.compile(
    r"(?:\w+\.)?\blgd_(?P<col>district|block)\b\s*\)?\s*(?:=|ILIKE|IN)\s*\(?\s*"
    r"(?:UPPER\s*\(\s*)?'(?P<val>[^']+)'",
    re.IGNORECASE,
)
_DIV_100_RE = re.compile(r"/\s*100\b")
# CM Elevate's is_withdraw is a real, well-defined boolean column that is FALSE
# on every one of the 8,543 rows today (schema_context.py rule 15) — a filtered
# COUNT against it is a genuine, correct zero, not a sign of a missing/hallucinated
# metric. compose_response's generic zero-hedging guidance (written to catch
# hallucinated columns that always read NULL/0) can't tell the two apart on its
# own, so a query that visibly filters is_withdraw gets an explicit note telling
# the composer this particular zero IS the real, complete answer (confirmed live
# 2026-09-12: without this, "how many applications have been withdrawn in Ri
# Bhoi" — SQL correctly `is_withdraw = TRUE`, 0 rows — was composed as "doesn't
# cover a withdrawal count", a false refusal over a query that ran exactly right).
_IS_WITHDRAW_FILTER_RE = re.compile(r"\bis_withdraw\b", re.IGNORECASE)


def _genuine_zero_notes(sql: str) -> list[str]:
    """Notes overriding compose_response's default zero-hedge for CM Elevate
    columns where a real 0 is the correct, complete answer (not a missing
    metric) — see _IS_WITHDRAW_FILTER_RE above."""
    if _IS_WITHDRAW_FILTER_RE.search(sql):
        return [
            "is_withdraw is a real, always-queryable boolean column (FALSE on every "
            "current row) — if the result is 0, that IS the true, complete withdrawal "
            "count for this scope. State it plainly as '0 applications withdrawn', "
            "never as data that 'isn't tracked' or 'doesn't cover' withdrawals."
        ]
    return []


def _sector_not_tracked_notes(rows: list[dict]) -> list[str]:
    """CM Elevate scheme_specific ->> 'sector_id' rule 9b requires stating
    plainly that sector isn't recorded for a scheme rather than reading a bare
    sector_recorded=0 as "checked and found none" — but that instruction lives
    in the SQL-generation prompt (schema_context.py), which compose_response
    never sees. Without a note here, a row like {scheme_name: Piggery,
    poultry_sector_applicants: 0, sector_recorded: 0, scheme_total: 1944} gets
    composed as "Piggery has 0" — technically not wrong, but exactly the
    misleading "checked and found none" reading rule 9b exists to avoid
    (confirmed live 2026-09-13 UAT on "applicants under Piggery and Poultry
    associated with poultry sector"). Detected generically from the result
    shape (a sector_recorded column that's 0 in a row where some OTHER count
    in that same row is non-zero) rather than a hardcoded scheme list, so it
    keeps working if which schemes carry sector_id ever changes."""
    notes: list[str] = []
    for r in rows:
        if not isinstance(r, dict) or "sector_recorded" not in r:
            continue
        if r.get("sector_recorded"):
            continue
        if any(k != "sector_recorded" and isinstance(v, (int, float)) and v
               for k, v in r.items()):
            name = r.get("scheme_name") or "this scheme"
            notes.append(
                f"sector_recorded is 0 for {name} in this result, with no sector "
                f"value recorded for it at all in this scope. Say exactly this, as "
                f"a plain fact alongside the other rows' real numbers: 'sector "
                f"isn't tracked for {name}.' Do NOT phrase it as 'has 0', and do "
                "NOT use hedge wording like 'doesn't cover', 'not covered', 'no "
                "data' or 'not available' — this is one specific, known fact about "
                "one row, not a reason to doubt or soften the OTHER rows' real, "
                "reportable numbers in the same result."
            )
    return notes


def _crore_conversion_for_single_village(entity_result: dict, sql: str) -> bool:
    """True when the SQL converts a MGNREGA money column to CRORE (÷100) while
    the question is scoped to a single village — a grain small enough that
    the true figure is routinely well under 1 crore, so rounding the crore
    value to 2 decimal places can display a real, non-zero lakh amount as
    "0.00 crore" (reported 2026-09-10 UAT: Maska's real 0.49 lakh MGNREGA
    expenditure came back as "0.00 crore" for exactly this reason). MGNREGA
    money is natively LAKH (schema_context's MGNREGA rule 4) — the crore
    conversion exists only for cross-scheme/statewide normalisation against
    PMAY's rupee figures, never for a single village's own figure."""
    if not entity_result.get("resolved", {}).get("village_code"):
        return False
    # "crore" is checked as a plain substring, not \bcrore\b — it is always used
    # here as an identifier suffix ("total_expenditure_crore"), and an
    # underscore is a word character, so \b never falls between "_" and "c".
    return bool(_DIV_100_RE.search(sql)) and "crore" in sql.lower()


def _village_name_filter_instead_of_code(entity_result: dict, sql: str) -> "int | None":
    """The resolved village_code when the question resolved to one but the
    generated SQL filters on lgd_village_name instead — the exact "Bamil
    Reserve Apal" bug (2026-09-09): entity resolution correctly picked
    village_code, prompt_builder's RESOLVED ENTITIES block told the generator
    to use it verbatim and NOT filter on lgd_village_name, and the generator
    did it anyway, inventing an upper-cased lgd_village_name literal. Storage
    keeps village names in mixed/title case (curated.v_pmay has "Bamil
    Reserve Apal", not "BAMIL RESERVE APAL"), so the literal silently matches
    zero rows and the query runs clean but returns 0. village_code is the
    only column guaranteed to match."""
    code = entity_result.get("resolved", {}).get("village_code")
    if code is None or _VILLAGE_CODE_FILTER_RE.search(sql):
        return None
    return code if _VILLAGE_NAME_FILTER_RE.search(sql) else None


def _village_filtered_at_wrong_level(entity_result: dict, sql: str) -> "tuple[int, str] | None":
    """(village_code, offending clause) when a resolved village is filtered as a
    BLOCK or DISTRICT instead, else None.

    Only fires when the literal is the village's OWN display name — a genuine
    district scope alongside a village ("... in NONGLADEW, Ri Bhoi") is a real,
    correct filter and must be left alone.

    A correct `village_code` filter being present is NOT on its own a reason to
    pass: the generator sometimes emits BOTH, ANDing a bogus
    `lgd_block = '<village name>'` onto the right village_code. That still
    matches zero rows, so the query returns a confident 0 for a village with
    real records — confirmed live 2026-09-17 on "william nagar(mb) - ward
    no.4", where village_code = 70675 was correct and the added
    `lgd_block = 'WILLIAM NAGAR(MB)'` zeroed a true count of 1."""
    resolved = entity_result.get("resolved") or {}
    code = resolved.get("village_code")
    if code is None:
        return None
    display = (entity_result.get("display") or {}).get("village")
    if not display:
        return None
    # Compare with punctuation and spacing squashed out, and accept a PREFIX of
    # the village name as well as the whole of it: the literal the generator
    # writes is often the same truncation the extractor produced
    # ("WILLIAM NAGAR(MB)" for the village "William Nagar (MB) - Ward No.4").
    # It is still a village name sitting in a block/district column either way.
    _squash = lambda t: re.sub(r"[^A-Z0-9]", "", str(t).upper())
    want = _squash(display)
    for m in _WRONG_LEVEL_FILTER_RE.finditer(sql or ""):
        got = _squash(m.group("val"))
        # Guard against a 1-2 character fragment matching by accident.
        if got and len(got) >= 4 and want.startswith(got):
            return code, f"lgd_{m.group('col').lower()} = '{m.group('val')}'"
    return None


def _village_code_as_geography_key(entity_result: dict, sql: str) -> "int | None":
    """The resolved village_code when the generated SQL filters `geography_key`
    directly to that same integer literal instead of `village_code` — a
    silent-wrong-number bug distinct from the lgd_village_name one above.
    geography_key is a small surrogate key on curated.dim_geography, entirely
    unrelated to the LGD village_code (e.g. MASKA village is geography_key =
    5999 but village_code = 277769), so plugging the resolved village_code
    value straight into geography_key's WHERE clause matches no row and the
    query runs clean but returns NULL (reported 2026-09-10 UAT: a real ~0.5
    lakh MGNREGA expenditure for a small village came back "doesn't cover
    that metric" because the generated SQL filtered geography_key = 277769 —
    a village_code — instead of village_code = 277769). Filtering geography_key
    via a dim_geography subquery keyed on village_code is fine and NOT
    flagged — only a bare literal placed in geography_key's slot is."""
    code = entity_result.get("resolved", {}).get("village_code")
    if code is None:
        return None
    if re.search(rf"\bgeography_key\s*=\s*{code}\b", sql):
        return code
    return None


# The SQL verifier (a small model) occasionally hallucinates that the
# RESOLVED ENTITIES block it was just handed "is empty" even when
# prompt_builder._entities_block plainly rendered entries into it — confirmed
# live 2026-09-13: "How many Focus+ beneficiaries are there in wgh across all
# financial years" resolved district = WEST GARO HILLS correctly, the verify
# prompt genuinely contained "lgd_district = 'WEST GARO HILLS'" under
# "RESOLVED ENTITIES — MANDATORY...", and the verifier still claimed the
# block was empty on every one of 5 repeat calls with an unchanged prompt.
# Each "repair" attempt then re-sent the same (correct) SQL, got the same
# false complaint back, and after 4 attempts the whole question fell through
# to the KB fallback instead of ever running the query. entity_result
# ["resolved"] is ground truth this process built itself (not the model's
# guess), so a claim that contradicts it is a verifier error to discard, not
# a real Check 2 hit — unlike the case where resolved really is empty, which
# this guard leaves alone.
_VERIFIER_FALSE_EMPTY_ENTITIES = re.compile(
    r"resolved entities\b[^.]{0,200}\bis empty\b", re.IGNORECASE)

# Second known verifier false positive, same family as the one above. The
# RESOLVED ENTITIES block renders an assembly-constituency filter in a
# case-insensitive wrapper — UPPER(assembly_constituency_name) = UPPER('X') —
# because the SME catalogue's spelling and the stored spelling can differ in
# case. assembly_constituency_name is stored ALL CAPS
# (mgnrega_entity_resolver.yaml: stored_case: ALL CAPS), so a generator that
# writes the equivalent bare `assembly_constituency_name = 'X'` has produced a
# query that selects exactly the same rows. The verifier nonetheless flags the
# difference in FORM as a Check-2 entity mismatch (confirmed live 2026-09-15:
# "…within the MAWLAI block only" — attempt 1 was flagged purely for dropping
# the UPPER() wrapper). That rejection then feeds a repair prompt telling the
# generator its entity filter is "missing", and the repair reliably "fixes" it
# by dropping the OTHER filter instead, so every later attempt is flagged for a
# genuinely missing entity and the whole question falls through to the KB
# fallback ("couldn't build a working query").
#
# Only discarded when the column really is filtered to the resolved value in
# the SQL — a genuinely absent filter still raises, which is the check's whole
# purpose.
_VERIFIER_CASE_FORM_COMPLAINT = re.compile(
    r"\b(?:lowercase|uppercase|upper\(|case[- ]insensitive|verbatim|"
    r"correct case)\b", re.IGNORECASE)


def _verifier_complaint_is_cosmetic(issue: str, resolved: dict, sql: str) -> bool:
    """True when the verifier's complaint is about the FORM of a filter that is
    in fact present and correct in the SQL."""
    if not _VERIFIER_CASE_FORM_COMPLAINT.search(issue or ""):
        return False
    ac = (resolved or {}).get("assembly_constituency")
    if not ac:
        return False
    # The AC value is genuinely filtered on, in either form.
    return bool(re.search(
        rf"assembly_constituency_name\s*\)?\s*=\s*(?:UPPER\s*\(\s*)?'{re.escape(str(ac))}'",
        sql or "", re.IGNORECASE))


# Third known verifier false positive. When a village_code is resolved, the
# RESOLVED ENTITIES block deliberately SUPPRESSES the district/block that
# merely scoped the village lookup (prompt_builder._entities_block: they are
# redundant with village_code, which already pins one exact row-set, and
# rendering them as equally MANDATORY made the verifier demand filters the
# generator was right to omit). The verifier nonetheless sometimes "quotes" a
# district entity that was never in its prompt and rejects village-only SQL for
# omitting it — confirmed live 2026-09-15 on "ASIMGRE ... for East Garo Hills",
# where the block plainly contained only `village_code = 275373` yet the issue
# read "RESOLVED ENTITIES block lists lgd_district = 'EAST GARO HILLS'".
# Correct SQL is then repaired away and the question dies in the KB fallback.
#
# Discarded only when a village_code IS resolved AND the SQL genuinely filters
# on it — i.e. exactly the shape where the suppressed district is redundant.
_VERIFIER_SUPPRESSED_GEO_COMPLAINT = re.compile(
    r"\b(?:lgd_district|lgd_block|district|block)\b[^.]{0,120}"
    r"\b(?:missing|not present|omitted|absent|instead)\b|"
    r"\b(?:missing|not present|omitted|absent)\b[^.]{0,120}"
    r"\b(?:lgd_district|lgd_block|district|block)\b",
    re.IGNORECASE,
)


def _verifier_wants_suppressed_geography(issue: str, resolved: dict, sql: str) -> bool:
    """True when the verifier demands a district/block that _entities_block
    intentionally left out because a resolved village_code supersedes it."""
    code = (resolved or {}).get("village_code")
    if code is None:
        return False
    if not _VERIFIER_SUPPRESSED_GEO_COMPLAINT.search(issue or ""):
        return False
    return bool(re.search(rf"\bvillage_code\s*(?:=|IN)\s*\(?\s*{re.escape(str(code))}\b",
                          sql or "", re.IGNORECASE))


# Fourth known verifier false positive. A financial year is stored by its START
# year — FY 2023-24 IS year_key = 2023 (docs/DATA_MODEL.md; entity resolution
# emits exactly that). The verifier repeatedly reads the "-24" half as the value
# that should appear and flags correct SQL as using the wrong year: confirmed
# live 2026-09-17 on "…in RERAPARA … for FY 2023-24", where SQL filtering
# year_key = 2023 — matching the resolved entity verbatim — was rejected on 5 of
# 5 calls ("the question asks for FY 2023-24, but the SQL filters on year_key =
# 2023"). Every repair then re-sent the same correct query, the budget ran out,
# and a perfectly answerable question died in the KB fallback.
#
# Discarded only when the SQL's year_key genuinely equals the resolved one, so a
# real year mismatch still raises.
_VERIFIER_YEAR_COMPLAINT = re.compile(r"\byear_key\b|\bfinancial year\b|\bfy\b", re.IGNORECASE)


def _verifier_year_complaint_is_false(issue: str, resolved: dict, sql: str) -> bool:
    """True when the verifier disputes the year but the SQL already filters on
    exactly the resolved year_key."""
    year = (resolved or {}).get("year_key")
    if year is None:
        return False
    if not _VERIFIER_YEAR_COMPLAINT.search(issue or ""):
        return False
    filters = set(re.findall(r"\byear_key\s*=\s*(\d{4})\b", sql or ""))
    # A view that carries the FY label filters on it instead — financial_year_short
    # = '2024-25' IS year_key 2024 (Focus Legacy QA TC-23, 2026-09-25: rejected as
    # "the resolved entity value (2024) is not present in the WHERE clause").
    filters |= set(re.findall(r"\bfinancial_year(?:_short)?\s*=\s*'(\d{4})-\d{2}'", sql or "",
                              re.IGNORECASE))
    return filters == {str(int(year))}


# Fifth known verifier false positive, and the most clear-cut of the family: the
# verifier states as fact that the WHERE clause "omits these filters entirely"
# when the filters are sitting in it verbatim. Confirmed live 2026-09-18 on
# "How many applicants are there in BATABARI under Agro Tourism Villa Scheme,
# PRIME Small Enterprise Empowerment and Development (SEED) and Meghalaya
# Poultry Farming Scheme, BATABARI block, WEST GARO HILLS": entity resolution
# produced block=BATABARI + district=WEST GARO HILLS, the generator emitted
#   AND lgd_block = 'BATABARI' AND lgd_district = 'WEST GARO HILLS'
# and the verifier still returned "RESOLVED ENTITIES block lists lgd_block =
# 'BATABARI' and lgd_district = 'WEST GARO HILLS', but the SQL WHERE clause
# omits these filters entirely" on 10 of 10 calls. Each repair re-sent the same
# correct SQL, the 3-repair budget ran out, and a question whose answer is a
# plain 46 (SEED 43 + Poultry 2 + Agro Tourism Villa 1) died in the KB fallback
# as "couldn't build a working query".
#
# Unlike the assembly-constituency guard above, this is not about the FORM of a
# filter — the verifier is misreading a long multi-line WHERE clause and denying
# a literal that is plainly there. So the test is the strongest one available:
# discard the complaint only when EVERY resolved geography entity it names is
# provably filtered on in the SQL. A genuinely missing filter still raises,
# which keeps Check 2 doing its job.
_VERIFIER_MISSING_GEO_COMPLAINT = re.compile(
    r"\b(?:omits?|omitted|omitting|missing|absent|not present|no filter|lacks?|"
    r"does not (?:include|filter|contain)|fails to (?:include|filter))\b",
    re.IGNORECASE)

# resolved-entity key -> the SQL column prompt_builder._entities_block renders
# it as, which is the column the verifier names back in its complaint.
_RESOLVED_GEO_COLUMNS = {
    "district": "lgd_district",
    "block": "lgd_block",
    "assembly_constituency": "assembly_constituency_name",
}


def _sql_filters_on(sql: str, column: str, value: str) -> bool:
    """True when `sql` constrains `column` to `value`, in any of the forms the
    generator legitimately produces: bare equality, an UPPER()/LOWER() wrapper
    on either side, or membership in an IN (...) list."""
    val = re.escape(str(value))
    col = re.escape(column)
    # col = 'V'  |  UPPER(col) = 'V'  |  col = UPPER('V')
    eq = (rf"(?:UPPER|LOWER)?\s*\(?\s*{col}\s*\)?\s*=\s*"
          rf"(?:(?:UPPER|LOWER)\s*\(\s*)?'{val}'")
    if re.search(eq, sql or "", re.IGNORECASE):
        return True
    # col IN ('A', 'V', ...) — the value must be one of the listed literals.
    for m in re.finditer(
            rf"(?:UPPER|LOWER)?\s*\(?\s*{col}\s*\)?\s+IN\s*\(([^)]*)\)",
            sql or "", re.IGNORECASE):
        if re.search(rf"'{val}'", m.group(1), re.IGNORECASE):
            return True
    return False


# MGNREGA single-village queries (all-villages QA 2026-09-26). The 30B wrote a
# different village_code than the resolved one on every attempt for AMPATIGRI
# (276411 for 273749); the verifier rightly rejected it, three repairs repeated
# it, and the question died in the KB fallback. Exactly one village was
# resolved, so the only correct literal is known — substitute it.
_VILLAGE_CODE_EQ_RE = re.compile(r"\bvillage_code\s*=\s*'?(\d+)'?", re.IGNORECASE)


# A village_code filter already names the place. A block/district literal the
# generator adds beside it can only narrow it wrongly: MAWLIEH (village 277048,
# EASTERN WEST KHASI HILLS) got "AND lgd_district = 'EAST KHASI HILLS'" and
# returned no expenditure for a village with 26.22 lakh (all-villages QA
# 2026-09-26). Removed when one village was resolved and the SQL filters it.
# UPPER(TRIM(lgd_district)) too (Focus Legacy all-villages run 2026-10-02: the
# village name KASHARIPARA written into the district column beside its code), and
# LOWER(lgd_district) = 'nongthylep' (the same run, 3 villages).
_GEO_BESIDE_VILLAGE_RE = re.compile(
    r"\s+AND\s+(?:(?:UPPER|LOWER|TRIM)\s*\(\s*)*(?:[A-Za-z_]+\.)?lgd_(?:block|district)(?:\s*\))*\s*=\s*"
    r"(?:(?:UPPER|LOWER|TRIM)\s*\(\s*)*'[^']*'(?:\s*\))*", re.IGNORECASE)


# Focus Plus single-village queries (all-villages run 2026-09-27). With ONE
# village resolved, the generator still wrote `UPPER(block_name_raw) =
# 'WAGESIK'` (a ward name in the block column), `UPPER(district_name_raw) =
# 'CHOBAGOK'`, `lgd_village_name = 'WARD NO. 4'` (Focus Plus stores it mixed
# case) or a block/district beside the right village_code — every one returned
# 0 or NULL for a place with real payments. The code alone names the place, so
# the WHERE is rebuilt condition by condition: place conditions go, the one
# village_code goes in, everything else (year, tranche, status…) stays.
_FP_PLACE_COND_RE = re.compile(
    r"^\s*(?:UPPER\s*\(\s*|TRIM\s*\(\s*)*(?:[A-Za-z_]+\.)?"
    r"(?:lgd_district|lgd_block|block_name_raw|district_name_raw|lgd_village_name|village_name_raw|"
    r"entity_type|village_code|geography_key)\b", re.IGNORECASE)
_FP_WHERE_SPAN_RE = re.compile(
    r"\bWHERE\b(?P<body>.*?)(?=\bGROUP\s+BY\b|\bORDER\s+BY\b|\bHAVING\b|\bLIMIT\b|$)",
    re.IGNORECASE | re.DOTALL)


def _fp_place_literal(cond: str, scheme: str = "Focus Plus") -> bool:
    """True when a condition's literal is a PLACE name on some other column —
    `tranche_label ILIKE '%GASUAPARA%'` put the village's block into the tranche
    filter and zeroed RONGBOKGRE's disbursement (all-villages run 2026-09-27)."""
    lits = [re.sub(r"[%_]", " ", x).strip().upper() for x in _SQL_LITERAL.findall(cond)]
    lits = [re.sub(r"\s+", " ", x) for x in lits if x]
    if not lits:
        return False
    places = {re.sub(r"\s+", " ", str(n)).strip().upper()
              for d in ("block", "district") for n in canonical_names(scheme, d)}
    return any(x in places for x in lits)


# CM Elevate (all-villages smoke run 2026-09-28, KI-106) hit every shape this
# rebuild handles: the block alone (RONGAP SONGGITAL answered with Songsak's 47),
# the village name in lgd_district ('MAWDEM DOMPHLANG'), a misspelled district
# beside the code ('WEST JAINTEIA HILLS' → 0), and an upper-cased lgd_village_name
# that matched nothing (PDENGSHNONG MAWPHYLLUN MARBISU → 0).
_PIN_VILLAGE_WHERE_SCHEMES = (["Focus Plus"], ["CM Elevate"])


def _top_level_where_span(sql: str) -> "tuple[int, int, int] | None":
    """(start of WHERE, start of its body, end of its body) for the query's OWN
    WHERE — never one inside parentheses. CM Elevate verification counts carry
    `COUNT(*) FILTER (WHERE data_verified = 'Valid')` in the SELECT list, and the
    first-WHERE regex rebuilt from inside that FILTER (2026-09-28)."""
    masked = _mask_sql_literals(sql)
    depth, where_at = 0, None
    for m in re.finditer(r"[()]|\bWHERE\b|\bGROUP\s+BY\b|\bORDER\s+BY\b|\bHAVING\b|\bLIMIT\b",
                         masked, re.IGNORECASE):
        tok = m.group(0)
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == 0:
            if tok.upper() == "WHERE" and where_at is None:
                where_at = m
            elif where_at is not None:
                return where_at.start(), where_at.end(), m.start()
    return (where_at.start(), where_at.end(), len(sql)) if where_at is not None else None


def _focusplus_pin_village_where(schemes: list[str], entity_result: dict, sql: str) -> str:
    code = (entity_result.get("resolved") or {}).get("village_code")
    if schemes not in _PIN_VILLAGE_WHERE_SCHEMES or not code or isinstance(code, (list, tuple)) or not sql:
        return sql
    # "geography_key IN (SELECT geography_key FROM curated.dim_geography WHERE
    # lgd_village_name = …)" names the village by text (mixed case in Focus
    # Plus → NULL for NONGRIM HILLS, 2026-09-27); the resolved code replaces it.
    sql = re.sub(r"(?:\w+\.)?geography_key\s+IN\s*\(\s*SELECT\b[^()]*\)",
                 f"village_code = {int(code)}", sql, flags=re.IGNORECASE)
    bare = _SQL_LITERAL.sub("''", sql)
    if len(re.findall(r"\bSELECT\b", bare, re.IGNORECASE)) != 1 or re.search(
            r"\b(JOIN|UNION|OR|WITH)\b", bare, re.IGNORECASE):
        return sql
    span = _top_level_where_span(sql)
    pin = f"village_code = {int(code)}"
    if span is None:
        # no WHERE at all: the village was dropped entirely — add it
        tail = re.search(r"\bGROUP\s+BY\b|\bORDER\s+BY\b|\bHAVING\b|\bLIMIT\b", sql, re.IGNORECASE)
        at = tail.start() if tail else len(sql)
        new = f"{sql[:at].rstrip()} WHERE {pin} {sql[at:]}".rstrip()
    else:
        w_start, b_start, b_end = span
        # split on AND OUTSIDE string literals: 'PRIME Small Enterprise Empowerment and
        # Development (SEED)' was cut in two and re-joined as "… AND …", matching no
        # row (CM Elevate all-villages smoke run 2026-09-28)
        body_masked = _mask_sql_literals(sql[b_start:b_end])
        cuts = [0] + [x for m in re.finditer(r"\bAND\b", body_masked, re.IGNORECASE) for x in (m.start(), m.end())] \
            + [b_end - b_start]
        conds = [sql[b_start:b_end][cuts[i]:cuts[i + 1]].strip() for i in range(0, len(cuts), 2)]
        kept = [c for c in conds if c and not _FP_PLACE_COND_RE.match(c) and not _fp_place_literal(c, schemes[0])]
        body = " AND ".join([pin] + kept)
        new = f"{sql[:w_start]}WHERE {body} {sql[b_end:].lstrip()}".rstrip()
    if new != sql:
        logger.info("%s: WHERE pinned to the one resolved village_code %s", schemes[0], code)
    return new


def _mgnrega_drop_geo_beside_village(schemes: list[str], entity_result: dict, sql: str) -> str:
    # Every village scheme since 2026-10-02: a district / block literal beside the
    # one resolved village_code can only narrow it wrongly (Focus Legacy wrote the
    # village name into lgd_district: KASHARIPARA, KANDARGAON, KAIMBATAPARA -> 0).
    code = (entity_result.get("resolved") or {}).get("village_code")
    if not _village_scheme(schemes) or not code or isinstance(code, (list, tuple)) or not sql:
        return sql
    if not re.search(rf"\bvillage_code\s*=\s*'?{int(code)}\b", sql):
        return sql
    return _GEO_BESIDE_VILLAGE_RE.sub("", sql)


def _mgnrega_village_filter_missing(schemes: list[str], entity_result: dict, sql: str) -> "int | None":
    """The resolved village_code when a MGNREGA query does not filter on any
    village at all. The generator dropped the village for MEGAPGRE / NONGLUM
    (filtered the block) and UMTYRNGA (no filter: the statewide 106,669.85 lakh
    reported as one village's spend), and the verifier passed it (KI-034 family,
    all-villages QA 2026-09-26)."""
    code = (entity_result.get("resolved") or {}).get("village_code")
    if "MGNREGA" not in schemes or not code or isinstance(code, (list, tuple)) or not sql:
        return None
    if re.search(r"\bvillage_code\b|\blgd_village_name\b|\bgeography_key\b", sql, re.IGNORECASE):
        return None
    return int(code)


def _village_filter_missing(schemes: list[str], entity_result: dict, sql: str) -> "int | None":
    """_mgnrega_village_filter_missing for every scheme. A resolved village the
    SQL does not filter on at all is a wider area reported as that village.
    _resolved_scope_missing deliberately leaves villages to "the village
    guards", but the only one was MGNREGA's, so the other five schemes had
    none. Live 2026-10-02 (all-villages run): "How many applications are there
    under CM Elevate Legacy in NOAGRE?" resolved village_code 273728, and the
    30B read the name as a programme: WHERE scheme_name = 'Meghalaya New
    Agriculture and Allied Activities Scheme', no village, "zero records" for a
    village with 12. Every curated view carries village_code."""
    if not schemes or (schemes[0] != "MGNREGA" and len(schemes) != 1):
        return None
    code = (entity_result.get("resolved") or {}).get("village_code")
    if not code or isinstance(code, (list, tuple)) or not sql:
        return None
    if re.search(r"\bvillage_code\b|\blgd_village_name\b|\bgeography_key\b", sql, re.IGNORECASE):
        return None
    return int(code)


def _geo_value_in_sql(value: str, sql_upper: str) -> bool:
    """A resolved place name is present in the (upper-cased, apostrophe-
    unescaped) SQL: as a literal, or as the pattern of a LIKE / ILIKE / regex
    match on its first distinctive word ("%TURA%" for TURA MUNICIPAL BOARD)."""
    v = str(value).upper().replace("''", "'").strip()
    if not v:
        return True
    if f"'{v}'" in sql_upper or v in sql_upper:
        return True
    first = next((w for w in re.split(r"[\s\-()/,]+", v) if len(w) >= 4), None)
    return bool(first and re.search(rf"(?:LIKE|~\*?)\s*'[^']*{re.escape(first)}[^']*'", sql_upper))


def _resolved_scope_missing(question: str, schemes: list[str], entity_result: dict,
                            sql: str) -> "tuple[str, str] | None":
    """(field, repair instruction) when a place or year the question pinned is
    absent from the SQL, else None — the deterministic half of the semantic
    contract between entity resolution and SQL generation.

    KI-034 (live 2026-09-26, context validation K8): a fallback fragment carried
    the resolved district and year in the MANDATORY entities block, the SQL had no
    WHERE clause at all, and the verifier passed it — a statewide all-years figure
    reported for one district and year. The prompt rule (MANDATORY entities) and
    the verifier are prose checks that sampling can skip; this one cannot.

    Checked: a resolved district / block (and every member of a district_list /
    block_list comparison) must appear in the SQL; a resolved year_key must too.
    Deliberately NOT checked, because the SQL is right without them:
      - district / block when a village_code is resolved: _entities_block
        suppresses them as redundant, and the village guards own that case;
      - a hill-range expansion (district_list_region): the SQL may cover it
        by pattern;
      - the year for a scheme with no time dimension (CM Elevate), or when the
        question itself asks for all years."""
    resolved = entity_result.get("resolved") or {}
    if not sql or not resolved:
        return None
    text = sql.upper().replace("''", "'")
    village = resolved.get("village_code") or resolved.get("village_code_list")
    if not village:
        for key in ("district", "block"):
            v = resolved.get(key)
            if isinstance(v, str) and v.strip() and not _geo_value_in_sql(v, text):
                # Focus Plus maps blocks on block_name_raw (prompt_builder._entities_block)
                col = ("UPPER(block_name_raw)" if key == "block" and schemes == ["Focus Plus"]
                       else f"lgd_{key}")
                return key, (
                    f"the question is about {key} {v} (a RESOLVED, MANDATORY filter), but this "
                    f"query does not filter on it, so it returns a figure for a different or "
                    f"wider area. Add {col} = '{v}' to the WHERE clause, exactly as the RESOLVED "
                    "ENTITIES block says, and keep every other filter, the aggregation and the "
                    "grouping exactly as they were.")
        for key, dim in (("district_list", "district"), ("block_list", "block")):
            vals = resolved.get(key)
            if key == "district_list" and resolved.get("district_list_region"):
                continue
            if isinstance(vals, (list, tuple)) and len(vals) >= 2:
                absent = [v for v in vals if isinstance(v, str) and not _geo_value_in_sql(v, text)]
                if absent:
                    return key, (
                        f"the question compares the {dim}s {', '.join(map(str, vals))}, but this "
                        f"query leaves out {', '.join(map(str, absent))}. Filter lgd_{dim} IN "
                        f"({', '.join(repr(str(v)) for v in vals)}), GROUP BY lgd_{dim} so each one "
                        "gets its own figure, and keep every other clause as it was.")
    yk = resolved.get("year_key")
    years = [yk] if isinstance(yk, int) else [y for y in (yk or []) if isinstance(y, int)] \
        if isinstance(yk, (list, tuple)) else []
    timed = bool(schemes) and all(_SCHEME_DATA_YEARS.get(s) for s in schemes)
    if years and timed and not _ALL_YEARS_CUE.search(question or ""):
        absent_y = [y for y in years if not re.search(rf"(?<!\d){y}(?!\d)", sql)]
        if absent_y:
            fy = ", ".join(f"FY {y}-{(y + 1) % 100:02d}" for y in absent_y)
            return "year", (
                f"the question is about {fy} (a RESOLVED, MANDATORY filter; year_key = "
                f"{absent_y[0]} is the FY start year), but this query does not filter on it, so "
                "it adds up every year. Add the year filter in the scheme's own form (year_key = "
                f"{absent_y[0]}, or its financial_year column) and keep every other clause as it was.")
    return None


def _mgnrega_pin_village_code(schemes: list[str], entity_result: dict, sql: str) -> str:
    code = (entity_result.get("resolved") or {}).get("village_code")
    if "MGNREGA" not in schemes or not code or isinstance(code, (list, tuple)) or not sql:
        return sql
    # An IN-list with extra codes: "MAWKOHMIT & MAWKYNSAH" (277073) came back as
    # village_code IN (277073, 277074) — a neighbour's 5,387 person-days added to
    # a village that recorded 0 (all-villages QA 2026-09-26).
    in_re = re.compile(r"\bvillage_code\s+IN\s*\(\s*[\d\s,']+\)", re.IGNORECASE)
    if in_re.search(sql):
        logger.info("MGNREGA: SQL filtered a village_code IN-list, resolved one village %s — pinned", code)
        return in_re.sub(f"village_code = {int(code)}", sql)
    found = {int(m.group(1)) for m in _VILLAGE_CODE_EQ_RE.finditer(sql)}
    if len(found) != 1 or int(code) in found:
        return sql
    logger.info("MGNREGA: SQL filtered village_code %s, resolved %s — substituted", found, code)
    return _VILLAGE_CODE_EQ_RE.sub(f"village_code = {int(code)}", sql)


# The converse false positive: the SQL filters exactly the resolved village_code
# and year, and the verifier still invents a check-2 complaint — "RESOLVED
# ENTITIES specifies lgd_district = 'UPPER DARENGRE'" (it is a village), or
# "273990 is the code for a different village (likely from the calibration
# examples)" for MALIPARA, whose code it is. Correct SQL died in the KB fallback.
_VERIFIER_CHECK2_RE = re.compile(r"\bcheck\s*2\b|RESOLVED ENTITIES", re.IGNORECASE)


def _verifier_village_code_complaint_is_false(issue: str, schemes: list[str],
                                              resolved: dict, sql: str) -> bool:
    # Focus Legacy too: a pinned village chip carries its district, the SQL filters
    # the code alone (_focus_legacy_village_code_only), and the verifier's "district
    # dropped" complaint sent the list to the KB fallback (Belbari, DAPGIRI —
    # all-villages run 2026-09-29, KI-161).
    # CM Elevate Legacy too, since its village-code guard drops the chip's block /
    # district literals (KI-173): DARUGRE / MARENGPARA, Purakhasia, were rejected
    # 4 times and answered "couldn't build a working query" (final pass 2026-09-29).
    if not ("MGNREGA" in schemes or _village_scheme(schemes) or schemes in (["Focus Legacy"], ["CM Elevate Legacy"])) \
            or not _VERIFIER_CHECK2_RE.search(issue or ""):
        return False
    code = (resolved or {}).get("village_code")
    if not code or isinstance(code, (list, tuple)):
        return False
    found = {int(m.group(1)) for m in _VILLAGE_CODE_EQ_RE.finditer(sql or "")}
    if found != {int(code)}:
        return False
    yk = (resolved or {}).get("year_key")
    # Focus Plus filters the year as financial_year_short = '2025-26'
    if isinstance(yk, int) and not (re.search(rf"\byear_key\s*=\s*{yk}\b", sql or "") or re.search(
            rf"\bfinancial_year(?:_short)?\s*=\s*'(?:FY\s*)?{yk}-", sql or "", re.IGNORECASE)):
        return False
    return True


def _verifier_missing_geo_is_false(issue: str, resolved: dict, sql: str) -> bool:
    """True when the verifier claims resolved geography filters are absent but
    every one it could be referring to is in fact present in the SQL."""
    if not _VERIFIER_MISSING_GEO_COMPLAINT.search(issue or ""):
        return False
    # Restrict to the geography entities the complaint actually names, by
    # column name or by value — so an unrelated grievance never trips this.
    named = {}
    for key, col in _RESOLVED_GEO_COLUMNS.items():
        val = (resolved or {}).get(key)
        if not val or not isinstance(val, str):
            continue
        if re.search(rf"\b{re.escape(col)}\b", issue or "", re.IGNORECASE) or \
           re.search(re.escape(val), issue or "", re.IGNORECASE):
            named[col] = val
    if not named:
        return False
    return all(_sql_filters_on(sql, col, val) for col, val in named.items())


# A verifier complaint that a RESOLVED place is "not listed" in the RESOLVED
# ENTITIES block, on SQL that filters exactly the resolved district and block.
# Live 2026-10-02 (live context suite, scenario F T3, both orchestrators):
# "What about Dalu block?" after "MGNREGA person-days in West Garo Hills in FY
# 2024-25" kept WEST GARO HILLS and added DALU; the verifier, given
#   lgd_district = 'WEST GARO HILLS' / lgd_block = 'DALU'
# in its prompt, answered "the RESOLVED ENTITIES block does NOT list
# 'lgd_block = DALU'". The repairs then alternated between dropping the block
# (the scope guard re-added it) and the district, the budget ran out and the
# question was answered from the reference documents. The SQL was right
# (390,921). Discarded only when every resolved district / block is filtered
# verbatim and the SQL names no other district or block.
_VERIFIER_NOT_LISTED = re.compile(
    r"\b(?:does\s+not|doesn['’]t|do\s+not|don['’]t|is\s+not|isn['’]t|are\s+not|aren['’]t|not)\s+"
    r"(?:list|listed|include|included|contain|contained|mention|mentioned|appear|present|specif\w*)\b",
    re.IGNORECASE)
_GEO_EQ_LITERAL = re.compile(r"\b(lgd_district|lgd_block)\s*=\s*(?:UPPER\s*\(\s*)?'((?:[^']|'')*)'", re.IGNORECASE)


def _verifier_unlisted_geo_is_false(issue: str, resolved: dict, sql: str) -> bool:
    """True when the verifier says a resolved district / block is not in the
    RESOLVED ENTITIES block while the SQL filters exactly the resolved ones."""
    if not _VERIFIER_NOT_LISTED.search(issue or ""):
        return False
    geo = {col: str(resolved[key]).upper() for key, col in (("district", "lgd_district"), ("block", "lgd_block"))
           if isinstance((resolved or {}).get(key), str) and resolved[key]}
    if not geo or not any(re.search(rf"\b{re.escape(v)}\b", issue or "", re.IGNORECASE) for v in geo.values()):
        return False
    in_sql: dict[str, set[str]] = {}
    for m in _GEO_EQ_LITERAL.finditer(sql or ""):
        in_sql.setdefault(m.group(1).lower(), set()).add(m.group(2).replace("''", "'").upper())
    return (all(_sql_filters_on(sql, col, val) for col, val in geo.items())
            and all(vals <= {geo.get(col)} for col, vals in in_sql.items()))


# Sixth known verifier false positive: a scheme name containing an apostrophe.
# In SQL a literal apostrophe is written doubled ('Chief Minister''s Green Taxi
# Scheme'), which is the SAME string as the resolved entity once parsed. The
# verifier reads the two spellings as different values and rejects correct SQL,
# quoting the identical text on both sides of its own complaint — confirmed
# live 2026-09-18 on "applicants in mylliem, the block ... under Green Taxi CM
# Elevate and ware house Scheme", flagged on 8 of 8 calls with
#   "RESOLVED ENTITIES block specifies scheme_name = 'Chief Minister''s Green
#    Taxi Scheme' (verbatim), but the SQL filters on ... 'Chief Minister''s
#    Green Taxi Scheme'"
# The query is right and returns 2; only the verifier disagrees, so the repair
# loop burned its budget and the question died in the KB fallback.
#
# Discarded only when un-doubling the SQL's apostrophes makes the resolved
# value genuinely present — a real value mismatch still raises.
_VERIFIER_VERBATIM_COMPLAINT = re.compile(
    r"\bverbatim\b|\bexact(?:ly)?\b|\bdoes not match\b|\bmismatch\b|"
    r"\bdiffer(?:ent|s)?\b|\bslightly different\b", re.IGNORECASE)


def _verifier_apostrophe_complaint_is_false(issue: str, resolved: dict, sql: str) -> bool:
    """True when the verifier disputes a resolved value that the SQL does carry,
    differing only by SQL's doubled-apostrophe escaping."""
    if not _VERIFIER_VERBATIM_COMPLAINT.search(issue or ""):
        return False
    # Only values that actually contain an apostrophe can hit this.
    vals = [v for v in (resolved or {}).values() if isinstance(v, str) and "'" in v]
    for v in (resolved or {}).values():
        if isinstance(v, list):
            vals.extend(x for x in v if isinstance(x, str) and "'" in x)
    if not vals:
        return False
    unescaped = (sql or "").replace("''", "'")
    return all(v in unescaped for v in vals)


# Seventh known verifier false positive: a "prohibited join" in SQL that joins
# nothing. The PROHIBITED JOINS block carries self-edges that are really grain
# rules ("NEVER join curated.v_focus_legacy -> curated.v_focus_legacy directly.
# Use GROUP BY pg_id instead.") and "any -> <base fact>" edges, and the small
# verifier reads them as forbidding the view itself. Confirmed 2026-09-25 in the
# Focus Legacy QA: TC-22 "records for each financial year" was rejected on every
# attempt ("The SQL joins directly to 'curated.v_focus_legacy', which is a
# prohibited join") for a one-table GROUP BY, and TC-20/TC-21 intermittently
# ("joins directly to curated.fact_focus_legacy_disbursement" — a table the SQL
# never names). The repair budget ran out and a plain COUNT(*) died as
# "couldn't build a working query".
#
# Discarded only when the complaint is about a PROHIBITED join and is provably
# false: the SQL has no JOIN and reads a single table, or every table the
# complaint names is absent from the SQL. A query that really joins a named
# table still raises.
_VERIFIER_PROHIBITED_JOIN = re.compile(r"\bprohibit\w*", re.IGNORECASE)
_SQL_TABLE_REF = re.compile(r"\b(?:curated|raw|semantic|meta|app)\.\w+", re.IGNORECASE)
_SQL_JOIN_KW = re.compile(r"\bJOIN\b", re.IGNORECASE)
_SQL_FROM_LIST = re.compile(r"\bFROM\s+[\w.]+(?:\s+(?:AS\s+)?\w+)?\s*,", re.IGNORECASE)


# Eighth: a Check 2 (resolved-entity) complaint when NOTHING was resolved. The
# verify prompt tells the verifier that "an empty or absent block means there is
# nothing to check here, so answer this check true" — yet it flagged "How many
# Producer Groups are mapped to each district" (GROUP BY lgd_district, no WHERE)
# on every attempt: "The RESOLVED ENTITIES block is empty ... the SQL attempts
# to group by lgd_district, which implies a geography filter exists" (Focus
# Legacy QA TC-25, 2026-09-25). Discarded only when the resolved block really is
# empty of filter entities and the complaint names no other check.
_VERIFIER_CHECK2 = re.compile(r"\bcheck\s*2\b|\bresolved\s+entit", re.IGNORECASE)
# Only an explicit reference to another check keeps the complaint alive — its
# wording ("filtering/aggregating on a dimension that was not resolved") is the
# same false Check 2 complaint, not a grain or metric finding.
_VERIFIER_OTHER_CHECK = re.compile(r"\bcheck\s*[134]\b|\bprohibit\w*|\bmissing\s+(?:sum|count|avg)\b",
                                   re.IGNORECASE)
def _verifier_check2_on_empty_entities(issue: str, resolved: dict) -> bool:
    if any(v not in (None, "", [], {}) for v in (resolved or {}).values()):
        return False
    return bool(_VERIFIER_CHECK2.search(issue or "")) and not _VERIFIER_OTHER_CHECK.search(issue or "")


def _verifier_join_complaint_is_false(issue: str, sql: str) -> bool:
    if not (_VERIFIER_PROHIBITED_JOIN.search(issue or "") and re.search(r"\bjoin", issue or "", re.IGNORECASE)):
        return False
    in_sql = {t.lower() for t in _SQL_TABLE_REF.findall(sql or "")}
    joins = bool(_SQL_JOIN_KW.search(sql or "") or _SQL_FROM_LIST.search(sql or ""))
    if not joins and len(in_sql) <= 1:
        return True
    named = {t.lower() for t in _SQL_TABLE_REF.findall(issue or "")}
    return bool(named) and not (named & in_sql)


# Keys read live from curated.v_cm_elevate.scheme_specific on 2026-09-27
# (jsonb_object_keys): populated on every row except sector_id.
_CME_LIVE_JSON_KEYS = ("file_status", "sector_id", "gender_id", "occupation_id")
_VERIFIER_NO_COLUMN = re.compile(r"not\s+(?:list|exist|a\s+column|present|defined|in\s+the\s+(?:view|schema))|"
                                 r"non-?existent|does\s+not\s+(?:have|contain|include)|no\s+such|"
                                 r"isn['’]?t\s+(?:a\s+column|listed)", re.IGNORECASE)


def _cme_resolved_places_in_sql(resolved: "dict | None", sql: str) -> bool:
    """Every resolved place value (district / block / village_code) appears in the SQL."""
    vals = [str(resolved.get(k)) for k in ("district", "block", "village_code") if (resolved or {}).get(k)]
    s = (sql or "").upper()
    return bool(vals) and all(v.upper() in s for v in vals)


def _verifier_scheme_specific_complaint_is_false(issue: str, schemes: list[str], sql: str,
                                                 resolved: "dict | None" = None) -> bool:
    # 2026-09-28 (CM Elevate all-blocks run): "Check 2: … lists 'lgd_block = LASKEIN', but
    # the SQL uses a JSON path extraction ('scheme_specific ->> file_status') …" rejected a
    # correct "rejected in Laskein block" query that filters lgd_block = 'LASKEIN' — three
    # times, then the KB fallback. Same false claim, check-2 wording.
    # Tester-phrasing re-run (2026-09-28): "applicants in UMSHAKEN" (village 277617,
    # resolved) died after three "Check 3: Table/grain — v_cm_elevate … multiple rows
    # per applicant (one per scheme/year) … aggregate the raw fact table". False:
    # the view is one row per application, and COUNT(DISTINCT request_id) on it is
    # the documented applicant count (CM ELEVATE RULES rule 8).
    if schemes == ["CM Elevate"] and re.search(r"\bcheck\s*3\b|\bgrain\b", issue or "", re.IGNORECASE) \
            and re.search(r"multiple rows|denormali[sz]ed|raw fact|fact_cm_elevate", issue or "", re.IGNORECASE):
        _tables = {t.lower() for t in re.findall(r"\b(?:FROM|JOIN)\s+([\w.]+)", sql or "", re.IGNORECASE)}
        if _tables == {"curated.v_cm_elevate"} and re.search(
                r"\bCOUNT\s*\(\s*(?:\*|DISTINCT\s+(?:\w+\.)?request_id)\s*\)", sql or "", re.IGNORECASE):
            return True
    # Mawpat final pass (2026-09-28): after the literal snap wrote the stored
    # 'PRIME Tourism Vehicle Scheme', the verifier insisted the name has no "Scheme"
    # and rejected it three times. Every programme literal is an exact stored name
    # and every resolved place is filtered: a check-2 complaint there is false.
    if schemes == ["CM Elevate"] and _VERIFIER_CHECK2_RE.search(issue or "") and re.search(
            r"scheme_name", sql or "", re.IGNORECASE):
        _m = _CME_SCHEME_IN_RE.search(sql or "")
        _lits = [x.replace("''", "'") for x in _SQL_LITERAL.findall(_m.group(1))] if _m else []
        _lits += [x.replace("''", "'") for x in re.findall(r"\bscheme_name\s*=\s*'((?:[^']|'')*)'", sql or "",
                                                           re.IGNORECASE)]
        if _lits and all(x in _CME_PROGRAMME_WORDS for x in _lits) and (
                not any((resolved or {}).get(k) for k in ("district", "block", "village_code"))
                or _cme_resolved_places_in_sql(resolved, sql)):
            return True
    if schemes == ["CM Elevate"] and "scheme_specific" in (issue or "").lower() \
            and _VERIFIER_CHECK2_RE.search(issue or "") and _cme_resolved_places_in_sql(resolved, sql):
        keys = re.findall(r"scheme_specific\s*->>?\s*'(\w+)'", sql or "", re.IGNORECASE)
        if keys and all(k.lower() in _CME_LIVE_JSON_KEYS for k in keys):
            return True
    """The 4B verifier claimed v_cm_elevate has no scheme_specific column and
    that file_status must be current_file_status, rejecting a correct
    "rejected in Ri Bhoi" query three times until the repair budget ran out
    (generalisation probe, 2026-09-27). scheme_specific IS a view column
    (schema_context CM ELEVATE TABLES) and these keys were read live."""
    if schemes != ["CM Elevate"] or "scheme_specific" not in (issue or "").lower():
        return False
    if not _VERIFIER_NO_COLUMN.search(issue or ""):
        return False
    keys = re.findall(r"scheme_specific\s*->>?\s*'(\w+)'", sql or "", re.IGNORECASE)
    return bool(keys) and all(k.lower() in _CME_LIVE_JSON_KEYS for k in keys)


async def _verify_sql(question: str, schemes: list[str], entity_result: dict, sql: str) -> "str | None":
    """One short issue sentence if the semantic verifier (SQL_VERIFY_MODEL,
    qwen4-deploy — see app/config.py) flags this SQL as not actually
    answering the question, else None.

    This is the catch-all for the class of bug the regex guards above can't
    be: they only recognise SQL shapes a past incident already taught them to
    match. The verifier judges the query against the same schema rules and
    resolved entities the generator itself was given, instead of a fixed
    pattern.

    Best-effort like every other auxiliary check in this module (premise_check,
    followups, context layer): a verifier outage, timeout, or unparseable
    response degrades to "no issue found" rather than blocking an answer the
    pipeline would otherwise have produced successfully."""
    if not settings.SQL_VERIFY_ENABLED:
        return None
    try:
        prompt = prompt_builder.build_verify_prompt(question, schemes, entity_result, sql)
        raw = await llm.call_sql_verifier(prompt, guided={"guided_json": _SQL_VERIFY_JSON_SCHEMA})
        data = _extract_json(raw)
    except Exception:  # noqa: BLE001
        logger.warning("SQL verifier call failed — continuing without it", exc_info=True)
        return None
    if not data or data.get("ok", True):
        return None
    issue = data.get("issue") or "the SQL verifier flagged this query as not answering the question"
    if entity_result.get("resolved") and _VERIFIER_FALSE_EMPTY_ENTITIES.search(issue):
        logger.warning(
            "SQL verifier claimed RESOLVED ENTITIES is empty when resolved=%r says otherwise "
            "— discarding as a known verifier hallucination: %s", entity_result["resolved"], issue)
        return None
    if _verifier_complaint_is_cosmetic(issue, entity_result.get("resolved") or {}, sql):
        logger.warning(
            "SQL verifier complained about the FORM of an assembly-constituency filter that "
            "is present and correct — discarding as cosmetic: %s", issue)
        return None
    if _verifier_year_complaint_is_false(issue, entity_result.get("resolved") or {}, sql):
        logger.warning(
            "SQL verifier disputed the financial year when the SQL already filters on the "
            "resolved year_key (a FY is stored by its START year) — discarding: %s", issue)
        return None
    if _verifier_apostrophe_complaint_is_false(issue, entity_result.get("resolved") or {}, sql):
        logger.warning(
            "SQL verifier disputed a value that differs only by SQL apostrophe escaping "
            "('' is a literal ') — discarding: %s", issue)
        return None
    if _verifier_missing_geo_is_false(issue, entity_result.get("resolved") or {}, sql):
        logger.warning(
            "SQL verifier claimed the resolved geography filters are missing when the "
            "SQL filters on every one of them verbatim — discarding: %s", issue)
        return None
    if _verifier_unlisted_geo_is_false(issue, entity_result.get("resolved") or {}, sql):
        logger.warning(
            "SQL verifier said a resolved district/block is not in RESOLVED ENTITIES while the "
            "SQL filters exactly the resolved ones — discarding: %s", issue)
        return None
    if _verifier_wants_suppressed_geography(issue, entity_result.get("resolved") or {}, sql):
        logger.warning(
            "SQL verifier demanded a district/block that the RESOLVED ENTITIES block "
            "deliberately suppressed as redundant with village_code — discarding: %s", issue)
        return None
    if _verifier_check2_on_empty_entities(issue, entity_result.get("resolved") or {}):
        logger.warning(
            "SQL verifier raised a resolved-entity (check 2) complaint with no resolved "
            "entities — its own instructions make that check pass — discarding: %s", issue)
        return None
    if _verifier_join_complaint_is_false(issue, sql):
        logger.warning(
            "SQL verifier reported a prohibited join the SQL does not make (no JOIN, or the "
            "named table is not in the query) — discarding: %s", issue)
        return None
    if _verifier_scheme_specific_complaint_is_false(issue, schemes, sql, entity_result.get("resolved") or {}):
        logger.warning(
            "SQL verifier claimed v_cm_elevate has no scheme_specific column / key when the SQL "
            "reads only live-verified keys — discarding: %s", issue)
        return None
    if _verifier_village_code_complaint_is_false(issue, schemes, entity_result.get("resolved") or {}, sql):
        logger.warning(
            "SQL verifier raised a resolved-entity complaint although the SQL filters exactly the "
            "resolved village_code and year (MGNREGA) — discarding: %s", issue)
        return None
    return issue


# The repair budget: execute_with_repair runs at most MAX_SQL_REPAIRS + 1
# attempts. The LangGraph path's execute_attempt -> repair loop reads the same
# constant, so the two orchestrators stop at the same attempt.
MAX_SQL_REPAIRS = 3


class RepairedSQLDenied(Exception):
    """KI-183: a REPAIRED query asks for more than the user's scope allows. The
    caller answers with the standard denial (_denied), exactly as for a first
    query that fails auth.authorize."""

    def __init__(self, decision: "auth.AuthDecision"):
        super().__init__(decision.reason)
        self.decision = decision


def _repaired_sql_denial(scope: "auth.UserScope | None", schemes: list[str], entity_result: dict,
                         sql: str) -> "auth.AuthDecision | None":
    """KI-183 (found 2026-10-02): authorization ran once, on the FIRST SQL; the up
    to MAX_SQL_REPAIRS queries the repair model wrote afterwards ran unchecked,
    so a repair that added a district / block literal or a finer GROUP BY than
    the first query was never checked against a capped role. Every repaired
    query now passes the same auth.authorize before it runs. The denied
    decision, or None."""
    if scope is None or not settings.AUTH_ENABLED:
        return None
    decision = auth.authorize(scope, schemes=schemes, resolved_entities=entity_result["resolved"], sql=sql)
    if decision.allow:
        return None
    logger.info("auth deny on repaired SQL (%s) user=%s: %s", decision.check, scope.user_id, decision.reason)
    return decision


async def execute_with_repair(question: str, schemes: list[str], entity_result: dict,
                              initial_sql: str | None = None, *,
                              max_repairs: int = MAX_SQL_REPAIRS,
                              scope: "auth.UserScope | None" = None) -> tuple[str, list[dict]]:
    sql = initial_sql if initial_sql is not None else await generate_sql(question, schemes, entity_result)
    for attempt in range(max_repairs + 1):
        sql = await _scheme_sql_rewrites(question, schemes, entity_result, sql)
        if attempt:
            denial = _repaired_sql_denial(scope, schemes, entity_result, sql)
            if denial is not None:
                raise RepairedSQLDenied(denial)
        try:
            return sql, await _execute_one_attempt(question, schemes, entity_result, sql)
        except (UnsafeSQLError, Exception) as e:
            # A dropped VPN / DB connection is not a SQL problem: repairing it
            # spent three 30B calls and then answered from the reference
            # documents ("the reference material does not contain…") — NANDICHAR
            # II in the Focus Plus all-villages run, 2026-09-27 (KI-025).
            if db_is_connection_error(e):
                raise DatabaseUnavailableError(str(e)) from e
            if attempt == max_repairs:
                raise  # repair budget spent — let the caller fall back
            logger.warning("SQL failed (attempt %d/%d), repairing: %s",
                           attempt + 1, max_repairs + 1, e)
            sql = await _repair_sql(question, schemes, entity_result, sql, str(e))
    return sql, []  # unreachable: the loop always returns rows or re-raises
# ── One SQL attempt, split out of execute_with_repair (D-032) ───────────────
# The three pieces of one loop iteration. execute_with_repair runs them in its
# loop; the LangGraph path runs them as its execute_attempt -> repair nodes.
# Same code, same order, so the two paths repair identically.

async def _scheme_sql_rewrites(question: str, schemes: list[str], entity_result: dict,
                               sql: str) -> str:
    """The deterministic per-scheme SQL rewrites run before every attempt."""
    sql = _focus_legacy_geo_columns(schemes, sql)
    sql = _focus_legacy_village_code_only(schemes, entity_result.get("resolved") or {}, sql)
    sql = _focus_legacy_date_trunc_as_date(schemes, sql)
    sql = _uppercase_geo_literals(sql)
    sql = _focusplus_drop_unrequested_verification_status(question, schemes, sql)
    sql = _cm_legacy_keep_unresolved_off_village(question, schemes, sql)
    sql = _cm_legacy_sanctioned_count(question, schemes, sql)
    sql = _cm_legacy_unselected_group_by(schemes, sql)
    sql = _cm_legacy_unrequested_limit(question, schemes, sql)
    sql = await _cm_elevate_fix_literals(schemes, sql)
    sql = _cm_elevate_split_scheme_in_list(schemes, sql)
    sql = _cm_elevate_sector_all_programmes(question, schemes, sql)
    sql = _cm_elevate_unasked_programme_filter(question, schemes, sql)
    sql = _cm_elevate_applicants_distinct(question, schemes, sql)
    sql = _cm_elevate_sector_zero_groups(schemes, sql)
    sql = _cm_elevate_level_pending(question, schemes, sql)
    sql = _cm_elevate_pending_without_level(question, schemes, sql)
    sql = _cm_elevate_unasked_level_filter(question, schemes, sql)
    sql = _cm_elevate_status_is_file_status(question, schemes, sql)
    sql = _cm_elevate_decision_status_column(schemes, sql)
    sql = _cm_legacy_qualify_shared_geo_cols(schemes, sql)
    sql = _mgnrega_numeric_division(schemes, sql)
    sql = _mgnrega_lakh_not_divided(schemes, sql)
    sql = _mgnrega_pin_village_code(schemes, entity_result, sql)
    sql = _mgnrega_drop_geo_beside_village(schemes, entity_result, sql)
    sql = _focusplus_pin_village_where(schemes, entity_result, sql)
    sql = await _focusplus_lgd_only_block(schemes, sql)
    sql = _mgnrega_women_years_only(schemes, sql)
    sql = _pmay_comparison_limit(schemes, entity_result, sql)
    sql = _pmay_crore_to_rupees(schemes, sql)
    # a village the SQL dropped (all schemes; live 2026-10-02, NOAGRE)
    sql = _cm_unasked_programme_beside_village(question, schemes, entity_result, sql)
    sql = _pin_missing_village_code(schemes, entity_result, sql)
    return sql


async def _execute_one_attempt(question: str, schemes: list[str], entity_result: dict,
                               sql: str) -> list[dict]:
    """The regex guards, the semantic verifier, execution and the post-execution
    check. Returns the rows; raises (ValueError / UnsafeSQLError / DB errors) for
    the caller to repair or give up on."""
    _pm_issue = _pmay_sql_issue(question, schemes, sql)
    if _pm_issue:
        raise ValueError(_pm_issue)
    _vlist = _mgnrega_village_list_issue(question, schemes, sql)
    if _vlist:
        raise ValueError(_vlist)
    _vmiss = _village_filter_missing(schemes, entity_result, sql)
    if _vmiss is not None:
        raise ValueError(
            f"the question resolved to ONE village, village_code = {_vmiss}, but this query "
            "does not filter on it, so it returns a block / district / statewide total and "
            "reports it as the village's figure. Add WHERE village_code = "
            f"{_vmiss} (the curated views carry village_code directly), drop any "
            "lgd_block / lgd_district filter, and keep the metric, year_key and "
            "aggregation exactly as they were.")
    # The semantic contract: what the question and resolver pinned
    # must be in the SQL (KI-034; stated amount 2026-09-29).
    _amt_issue = _focusplus_stated_amount_missing(question, schemes, sql)
    if _amt_issue:
        context_budget.log_decision("sql_guard", guard="stated_amount", verdict="reject")
        raise ValueError(_amt_issue)
    _scope_issue = _resolved_scope_missing(question, schemes, entity_result, sql)
    if _scope_issue:
        context_budget.log_decision("sql_guard", guard="resolved_scope", verdict="reject",
                                    missing=_scope_issue[0])
        raise ValueError(_scope_issue[1])
    if _focus_legacy_group_size_summed(question, schemes, sql):
        raise ValueError(
            "this question asks for a producer group's SIZE (its member count), but the "
            "query SUMs no_of_pg_members across the group's payments. A group's member "
            "count is recorded on EACH payment and 2,655 groups were paid more than once, "
            "so a SUM double-counts them (a 20-member group paid twice reads 40). Use "
            "MAX(no_of_pg_members) AS group_size per pg_id — GROUP BY pg_id with "
            "MAX(pg_name) for display, and put any 'more than N members' test in HAVING "
            "MAX(no_of_pg_members) > N. Keep every other clause as it was."
        )
    bad_code = _village_name_filter_instead_of_code(entity_result, sql)
    if bad_code is not None:
        raise ValueError(
            f"the question resolved to village_code = {bad_code} but this query filters on "
            "lgd_village_name instead — village name spelling/case is not reliable for "
            "matching (storage keeps mixed/title case, not upper-case), so that filter can "
            "silently match zero rows. Replace the lgd_village_name filter with "
            f"village_code = {bad_code} exactly, and keep every other clause as it was."
        )
    wrong_level = _village_filtered_at_wrong_level(entity_result, sql)
    if wrong_level is not None:
        _code, _clause = wrong_level
        if _VILLAGE_CODE_FILTER_RE.search(sql):
            # village_code is already correct — the bogus clause just
            # has to go, not be replaced.
            raise ValueError(
                f"this query already filters village_code = {_code} correctly, but it "
                f"ALSO filters {_clause} — a VILLAGE name placed in a block/district "
                "column, which matches no row and forces the whole query to 0 even "
                f"though village {_code} has real records. DELETE that clause entirely "
                "and keep village_code and every other filter exactly as they are."
            )
        raise ValueError(
            f"the question resolved to village_code = {_code}, but this query filters "
            f"{_clause} — that is a VILLAGE name placed in a block/district column, so "
            "it matches zero rows and returns a confident 0 for a village that has "
            f"real records. Replace that clause with village_code = {_code} exactly "
            "(the curated view carries village_code as a direct column), and keep "
            "every other clause as it was."
        )
    bad_geo_code = _village_code_as_geography_key(entity_result, sql)
    if bad_geo_code is not None:
        raise ValueError(
            f"the question resolved to village_code = {bad_geo_code} but this query filters "
            f"geography_key = {bad_geo_code} instead — geography_key is a different surrogate "
            "key, unrelated in value to village_code, so this filter matches no row and "
            "silently returns NULL/zero instead of the real figure. Replace "
            f"geography_key = {bad_geo_code} with village_code = {bad_geo_code} exactly (the "
            "curated view carries village_code as a direct column), and keep every other "
            "clause as it was."
        )
    if _crore_conversion_for_single_village(entity_result, sql):
        raise ValueError(
            "this query converts a MGNREGA money column to CRORE (dividing by 100) for a "
            "question scoped to a single village — MGNREGA money is natively LAKH RUPEES, "
            "and a single village's figure is routinely well under 1 crore, so rounding it "
            "to crore at 2 decimal places can display a real, non-zero amount as a "
            "misleading '0.00 crore'. Report the figure directly in LAKH instead (remove "
            "the ÷100 conversion and the crore alias/label), and keep every other clause "
            "as it was."
        )
    if _mgnrega_facts_joined(sql):
        raise ValueError(
            "this query JOINs curated.v_employment to curated.v_expenditure "
            "directly. MGNREGA's two facts have no shared grain — both hold many "
            "rows per village-year — so that join fans out and every SUM comes back "
            "multiplied (a real 986,020 person-days became 109,448,220). Rebuild it "
            "with one CTE per fact, each aggregated on its own and carrying the SAME "
            "filters, then combine the aggregates:\n"
            "  WITH emp AS (SELECT SUM(person_days) AS person_days "
            "FROM curated.v_employment WHERE <filters>),\n"
            "       exp AS (SELECT SUM(total_exp) AS total_exp_lakh "
            "FROM curated.v_expenditure WHERE <filters>)\n"
            "  SELECT exp.total_exp_lakh, emp.person_days FROM emp, exp\n"
            "Add GROUP BY inside each CTE and FULL OUTER JOIN them on the grain "
            "columns only if a per-area or per-year breakdown was asked for. Keep "
            "every filter exactly as it was."
        )
    bad_geo = _ac_with_invented_geo_filter(entity_result, sql)
    if bad_geo is not None:
        raise ValueError(
            f"this query filters on the assembly constituency AND on {bad_geo}, but "
            "no district or block was resolved for this question — that geography "
            "filter was invented. An assembly constituency cuts ACROSS blocks and "
            "districts, so its name is usually not a block or district name, and "
            f"ANDing {bad_geo} with the constituency filter matches zero rows and "
            "returns a false zero. Remove that filter entirely and keep only "
            "assembly_constituency_name (plus year_key and the aggregation) exactly "
            "as they were."
        )
    if _STATE_PSEUDO_FILTER.search(sql):
        raise ValueError(
            "generated SQL filters on a non-existent 'Meghalaya' / entity_type='State' "
            "pseudo-row — the whole dataset is already Meghalaya; remove that geographic "
            "filter entirely and keep every other clause"
        )
    bad_view = _rowgrain_no_aggregate(question, sql)
    if bad_view:
        raise ValueError(
            f"this is an aggregate question but the query reads raw rows from {bad_view}, "
            "which is at source-row grain (many rows per village) — a bare "
            "SELECT <metric> ... LIMIT 1 returns ONE arbitrary row, not a total. Wrap the "
            "metric in SUM(...): for a statewide total use "
            "SELECT SUM(<metric>) AS <name> FROM <view> [WHERE year_key = ...] with no "
            "GROUP BY; add GROUP BY <unit> only if the question asks for a per-district / "
            "per-block / per-year breakdown. job_cards_issued_total is a STOCK — pin a "
            "single year_key (the latest if none is named) and SUM across geographies, "
            "never across years. Keep every other clause exactly as it was."
        )
    # Last — the free regex guards above catch known bug shapes without
    # spending a model call; only a query that clears all of them goes to
    # the semantic verifier, which is the paid check.
    verify_issue = await _verify_sql(question, schemes, entity_result, sql)
    if verify_issue:
        raise ValueError(
            f"semantic verifier flagged this query: {verify_issue}. Fix the SQL to "
            "address that specific problem and keep every other clause (filters, "
            "year_key, geography, aggregation) that is not implicated."
        )
    rows = await run_readonly(sql)
    if _mgnrega_comparison_without_figures(question, schemes, rows):
        raise ValueError(
            "this question compares amounts, but the query returns only a label and "
            "no figures, so the answer cannot state what was compared. Return each "
            "compared quantity as its own numeric column (for wages vs materials: "
            "SUM(unskilled_wage_exp + semi_skilled_wage_exp) AS wage_exp_lakh and "
            "SUM(material_exp) AS material_exp_lakh), no CASE label. Keep every "
            "filter exactly as it was."
        )
    return rows


async def _repair_sql(question: str, schemes: list[str], entity_result: dict,
                      failed_sql: str, error: str) -> str:
    """One repair call: the failed SQL and its error back to the SQL model."""
    sql = failed_sql
    # The repair prompt carries the same schema + resolved-entities context as
    # the first attempt (not a schema-only stub) — a repair usually fails for
    # want of exactly that context — plus a targeted hint when the error names
    # a missing column.
    repair_prompt = prompt_builder.build_repair_prompt(
        question, schemes, entity_result, failed_sql=sql, error=error,
        extra_hint=_repair_hint(error))
    raw = await llm.call_sql_generator(repair_prompt, guided={"guided_regex": _SQL_SHAPE_REGEX})
    sql = _extract_sql(raw)
    return sql


# ── Numeric faithfulness guard for the composed answer ──────────────────────
# The composer is an LLM and will occasionally transcribe a number wrong
# ("170981" -> "17098"). On a government dashboard that is unacceptable, so the
# composed sentence is checked against the actual result values: any number it
# states that is not in the data (beyond rounding tolerance) triggers one strict
# retry, then a deterministic fallback sentence built straight from the rows.
_NUM_TOKEN = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

# "the data doesn't cover / isn't available / can't be broken down" — a hedge the
# composer must not use when the query actually returned a usable non-zero value.
_HEDGE_RE = re.compile(
    r"do(?:es)?n['’]?t\s+cover|does\s+not\s+cover(?:ed)?|not\s+cover(?:ed)?|isn['’]?t\s+covered|"
    r"not\s+available|no\s+data\b|"
    r"(?:doesn['’]?t|does\s+not|don['’]?t|do\s+not)\s+(?:have|include|contain|provide)|"
    r"only\s+provides?\b|can(?:not|['’]?t)\s+(?:be\s+)?(?:broken\s+down|split)|"
    r"no\s+(?:specific\s+)?(?:breakdown|split)\b",
    re.IGNORECASE,
)


def _as_number(v: object) -> "int | float | None":
    """Coerce a result cell to int/float, or None if it isn't numeric. Handles
    decimal.Decimal (asyncpg returns it for SUM/AVG over numeric columns — the
    reason a cross-scheme SUM was being skipped) without relying on
    Decimal.is_integer(), which is Python 3.12+ only."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, numbers.Number):          # float, Decimal, Fraction, …
        f = float(v)
        return int(f) if f.is_integer() else f
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if re.fullmatch(r"-?\d+(?:\.\d+)?", s):
            return int(s) if "." not in s else float(s)
    return None


def _data_numbers(rows: list[dict]) -> set[str]:
    """Every numeric leaf value in the result, as normalised digit strings."""
    out: set[str] = set()
    for row in rows:
        for v in row.values():
            n = _as_number(v)
            if n is None:
                continue
            if isinstance(n, int):
                out.add(str(n))
            else:
                out.add(f"{n:g}")
                out.add(f"{n:.2f}".rstrip("0").rstrip("."))
    return out


def _answer_numbers_faithful(answer: str, data_nums: set[str]) -> bool:
    """True if every 'reported' number in the answer traces back to a data value.
    Years and small integers (< 100, no decimal) are treated as prose, not data."""
    data_floats: list[float] = []
    for d in data_nums:
        try:
            data_floats.append(float(d))
        except ValueError:
            pass
    for tok in _NUM_TOKEN.findall(answer):
        raw = tok.replace(",", "")
        # A year or a financial-year range fragment — "2020", "2020-2021",
        # "2020-21" — is prose, not a measured quantity. The number tokenizer
        # splits "2020-2021" into "2020" and "-2021" (the hyphen read as a
        # sign), so the second-half forms "-YYYY" / "-YY" are matched here too.
        if re.fullmatch(r"-?(19|20|21)\d\d|-\d{2}", raw):
            continue
        try:
            fval = float(raw)
        except ValueError:
            continue
        if abs(fval) < 100 and "." not in raw:         # ordinal / "one or two"
            continue
        if raw in data_nums:
            continue
        if any(dv == fval or (dv and abs(dv - fval) / abs(dv) <= 0.005)
               or abs(dv - fval) < 0.5 for dv in data_floats):
            continue
        return False
    return True


# A comma-grouped number in either house style: Western 1,234,567 or Indian
# 12,34,567. Anything else ("1,0263") is a mis-grouped figure.
_GROUPED_NUM = re.compile(r"(?<![\d,.])\d{1,3}(?:,\d+)+(?:\.\d+)?(?![\d,])")
_VALID_GROUPING = re.compile(r"\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3}")


def _fix_digit_grouping(answer: str, data_nums: set[str]) -> str:
    """Re-group a mis-grouped number whose digits ARE a result value.

    The composer wrote "1,0263 female beneficiaries" for 10,263 on both live
    runs, and the faithfulness check strips commas before comparing, so the
    garbled figure reached the officer (Focus Plus use-case QA 2026-09-27,
    FOCUS-021b, KI-062). Only a token that is valid in neither Western nor
    Indian grouping AND whose digits match the data is rewritten — a list like
    "Tranches 1,2" is never a data value and is left alone."""
    def _fix(m: "re.Match[str]") -> str:
        tok = m.group(0)
        whole, _, frac = tok.partition(".")
        if _VALID_GROUPING.fullmatch(whole):
            return tok
        digits = whole.replace(",", "")
        n = _as_number(digits + ("." + frac if frac else ""))
        if n is None or len(digits) < 4:   # "1,2" is a list, never a grouped figure
            return tok
        forms = {digits + ("." + frac if frac else ""), f"{n:g}" if isinstance(n, float) else str(n)}
        if not forms & data_nums:
            return tok
        return f"{int(digits):,}" + ("." + frac if frac else "")
    return _GROUPED_NUM.sub(_fix, answer)


# Numbers inside the executed SQL's quoted literals — '12.5K', 'Tranch 4 -
# Feb-March', '2025-26' — are the filter's LABELS, not measured figures. An
# answer that names its own filter ("under the 12.5K batch") must not be
# flagged as a misquote for it: that flag threw away a correct sentence and
# put the row-dump fallback "31,317,500 amount raw." in front of the officer
# (Focus Plus use-case QA 2026-09-27, FOCUS-006a, KI-064).
_SQL_LITERAL = re.compile(r"'((?:[^']|'')*)'")


def _sql_literal_numbers(sql: str) -> set[str]:
    out: set[str] = set()
    for lit in _SQL_LITERAL.findall(sql or ""):
        for tok in re.findall(r"\d+(?:\.\d+)?", lit):
            out.add(tok)
    return out


def _row_metrics(row: dict) -> list[tuple[str, "int | float"]]:
    """(column, numeric value) for every numeric cell in the row — Decimal included."""
    out: list[tuple[str, "int | float"]] = []
    for k, v in row.items():
        n = _as_number(v)
        if n is not None:
            out.append((k, n))
    return out


def _fmt_num(v: "int | float") -> str:
    return f"{v:,}" if isinstance(v, int) else f"{v:,.2f}"


def _answer_covers_metrics(answer: str, row: dict) -> bool:
    """Every numeric metric in a single aggregate row must appear in the answer —
    catches a cross-scheme result where the composer names only one side."""
    ans = answer.replace(",", "")
    for _k, v in _row_metrics(row):
        forms = {str(v)}
        if isinstance(v, float):
            forms.add(f"{v:.2f}".rstrip("0").rstrip("."))
        if not any(f in ans for f in forms):
            return False
    return True


# Numeric-looking columns that must NOT be summed/averaged — years, codes, ids,
# rank/serial numbers, resolved *_key columns. Reported as dimension coverage
# (distinct values) instead. Deliberately narrow: "no"/"sr" are omitted because
# they collide with "no_of_*" count columns; the real offenders are
# financial_year, *_code, *_id and pincode.
_NONSTAT_COL = re.compile(
    r"(^|_)(year|yr|fy|pincode|rank|serial)($|_)|_key$|_code$|_id$|(^|_)id$",
    re.IGNORECASE,
)


def _result_digest(rows: list[dict], max_values: int = 40) -> "tuple[str, set[str]]":
    """A deterministic whole-result summary, computed from EVERY row (not just the
    preview the composer is shown): the distinct coverage of each dimension
    column, and sum / mean / max / min of each numeric column together with which
    dimension row holds the max and the min.

    Returns (indented_text_block, allowed_number_strings). The strings are unioned
    into the numeric-faithfulness whitelist so a total or average the composer
    copies out of this block is not flagged as an invented number. Without this,
    any multi-row result (a district x year matrix, a per-block list) could only
    be described one visible cell at a time — never "the total is X" or "Y is
    highest" — because those figures are not present in the raw rows."""
    if len(rows) < 2:
        return "", set()

    dim_order: dict[str, list[str]] = {}
    dim_seen: dict[str, set[str]] = {}
    num_cols: list[str] = []
    for row in rows:
        for k, v in row.items():
            is_num = _as_number(v) is not None
            if is_num and not _NONSTAT_COL.search(k):
                if k not in num_cols:
                    num_cols.append(k)
                continue
            if v is None:
                continue
            s = str(v)
            if k not in dim_order:
                dim_order[k], dim_seen[k] = [], set()
            if s not in dim_seen[k]:
                dim_seen[k].add(s)
                dim_order[k].append(s)

    def _label(row: dict) -> str:
        parts = [str(row[k]) for k in dim_order if row.get(k) is not None]
        return " / ".join(parts) if parts else "(row)"

    lines: list[str] = [f"total rows: {len(rows)}"]
    allowed: set[str] = set()

    for k, vals in dim_order.items():
        shown = ", ".join(vals) if len(vals) <= max_values else f"{len(vals)} distinct values"
        lines.append(f"{k} ({len(vals)}): {shown}")

    for k in num_cols:
        pairs = [(r, _as_number(r.get(k))) for r in rows if _as_number(r.get(k)) is not None]
        if not pairs:
            continue
        nums = [n for _r, n in pairs]
        total = sum(nums)
        hi_row, hi = max(pairs, key=lambda p: p[1])
        lo_row, lo = min(pairs, key=lambda p: p[1])
        mean = total / len(nums)
        for val in (total, hi, lo, mean):
            f = float(val)
            allowed.add(str(int(f)) if f.is_integer() else f"{f:g}")
            allowed.add(f"{f:.2f}".rstrip("0").rstrip("."))
        lines.append(
            f"{k}: sum={_fmt_num(total)}; mean={_fmt_num(round(mean, 2))}; "
            f"max={_fmt_num(hi)} at [{_label(hi_row)}]; min={_fmt_num(lo)} at [{_label(lo_row)}]"
        )

    return "\n".join(f"  - {ln}" for ln in lines), allowed


def _deterministic_answer(rows: list[dict]) -> str:
    """A plain, exact sentence from the rows — used only when the LLM composer
    keeps misquoting or dropping numbers."""
    if len(rows) == 1:
        row = rows[0]
        nums = _row_metrics(row)
        # A single-row GROUP BY result (e.g. a category breakdown that happens
        # to have exactly one value present, like verification_status =
        # 'Approved' on every 12.5K row) still carries the category as a
        # non-numeric column. Drop the number alone and the answer misreports
        # a labelled breakdown as an unlabelled total.
        labels = [str(v) for k, v in row.items() if v is not None and _as_number(v) is None]
        prefix = ", ".join(labels) + ": " if labels else ""
        if len(nums) == 1:
            k, v = nums[0]
            return f"{prefix}{_fmt_num(v)} {k.replace('_', ' ')}."
        if len(nums) >= 2:
            return prefix + "; ".join(f"{k.replace('_', ' ')}: {_fmt_num(v)}" for k, v in nums) + "."
    # Multi-row: one labelled line per row. This used to be a raw
    # "Results — col: val; col: val" dump of the first 5 rows only, which read as
    # debug output and silently dropped the rest (a 12-district summary showed 5
    # districts — CM Elevate Legacy use-case QA TC-25 / TC-36, 2026-09-25).
    shown = rows[:_DETERMINISTIC_MAX_ROWS]
    lines = []
    for r in shown:
        labels = [str(v) for v in r.values() if v is not None and _as_number(v) is None]
        metrics = ", ".join(f"{_metric_label(k)}: {_fmt_num(v)}" for k, v in _row_metrics(r))
        head = " / ".join(labels) or "(no label)"
        lines.append(f"- {head} — {metrics}" if metrics else f"- {head}")
    more = len(rows) - len(shown)
    tail = f"\n…and {more} more row{'s' if more != 1 else ''} in the table." if more > 0 else ""
    return f"Here are the {len(rows)} results:\n" + "\n".join(lines) + tail


_DETERMINISTIC_MAX_ROWS = 15


def _metric_label(col: str) -> str:
    """A readable name for a result column: total_disbursed_cr -> 'total disbursed
    (₹ crore)'. The unit suffixes are the SQL-prompt conventions (_cr, _lakh,
    _pct, _rupees)."""
    for suffix, unit in (("_cr", " (₹ crore)"), ("_lakh", " (₹ lakh)"),
                         ("_pct", " (%)"), ("_rupees", " (₹)")):
        if col.endswith(suffix):
            return col[: -len(suffix)].replace("_", " ") + unit
    return col.replace("_", " ")


def _no_data_answer(schemes: list[str] | None, entities: dict[str, str] | None) -> str:
    """Plain 'nothing matched' message for a query that returned no rows at all.
    Built deterministically rather than left to the composer — on an empty
    result it sometimes free-forms RAG-style refusal wording ("the reference
    material does not contain...") that reads like an internal document search
    failed, when the honest answer is just that no records match the filters."""
    scope_bits = [v for v in (entities or {}).values() if v]
    scope = f" for {', '.join(scope_bits)}" if scope_bits else ""
    # One sentence, nothing else. The full capability catalogue used to be
    # appended here ("Data I do have here:" + every metric for the scheme),
    # which buried a one-line fact under a ~10-line dump the user did not ask
    # for and could not act on (reported 2026-09-18). An empty result answers
    # the question asked; what else the dataset could report is a different
    # question, and the NEXT STEPS chips already offer it.
    return f"I couldn't find any matching records{scope} in the data available."


def _is_plain_list_result(rows: list[dict]) -> bool:
    """A multi-row result whose rows carry no numeric metric — a pure list of
    dimension values (e.g. the districts that satisfy a coverage filter). Those
    rows ARE the answer; a 'not covered / can't tell' hedge over them is wrong."""
    return len(rows) >= 2 and not any(_row_metrics(r) for r in rows)


def _deterministic_list_answer(rows: list[dict]) -> str:
    """Plain sentence naming every value in a single-column list result — used
    when the composer hedges over a membership answer whose rows already are the
    answer set."""
    cols = list(rows[0].keys())
    if len(cols) == 1:
        vals = [str(r[cols[0]]) for r in rows if r.get(cols[0]) is not None]
        label = cols[0].replace("_", " ")
        if len(vals) <= 40:
            return f"{len(vals)} {label} values match: " + ", ".join(vals) + "."
        return (f"{len(vals)} {label} values match, including: "
                + ", ".join(vals[:40]) + ", …")
    return _deterministic_answer(rows)


_DIM_LABEL = {
    "district": "district",
    "block": "block (C&RD block)",
    "village": "village",
    "year": "financial year",
    "house_status": "house construction stage",
    "cm_scheme": "CM Elevate sub-scheme",
}


def _entity_names_block(entities: dict[str, str] | None) -> str:
    """A context block naming the canonical form of every place / year / category
    the query actually filtered on, so the composer uses the full proper name in
    its answer instead of echoing the user's abbreviation, code or misspelling
    ("wgh" -> "West Garo Hills", "fy24" -> "FY 2024-25")."""
    if not entities:
        return ""
    lines = [f"  - {_DIM_LABEL.get(k, k)}: {v}" for k, v in entities.items() if v]
    if not lines:
        return ""
    return (
        "\nEntity names — the query filtered on exactly these values. In your "
        "answer, refer to each place, year or category by the full name given "
        "here, even when the question used a short form, code or misspelling; "
        "never echo the user's shorthand back as the name.\n" + "\n".join(lines) + "\n"
    )


# ── Focus Plus answer guarantees (use-case QA 2026-09-27) ────────────────────
# The composer stated every figure correctly but left out what the use cases
# ask for: the share of each status / gender / occupation (KI-060), the
# difference and the higher side of a two-way comparison (KI-061), and it
# printed rupee amounts as bare "126197500.00" (KI-064). Prose rules had not
# held these under sampling, so, per the project pattern, each is guaranteed
# deterministically after composing — computed from the result rows, or from a
# parameter-bound recount that must reproduce the bot's own figure first.
_FP_PERSON_COLS = {"focus_status": "status", "verification_status": "verification status",
                   "gender": "gender", "occupation": "occupation"}
_FP_MONEY_COL = re.compile(r"amount|disburs|rupee|_rs$|_inr$", re.IGNORECASE)
_FP_COHORT_TAIL = ("these fields are recorded only for the 12.5K registration cohort; "
                   "the 93K legacy cohort has none of them")
_PCT_TOKEN = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _fp_rupees(v: "int | float") -> str:
    return f"₹{float(v):,.2f}"


def _fp_title(label: object) -> str:
    s = str(label)
    s = s.title() if s.isupper() else s
    # the stored tranche spelling is "Tranch 1"; say it the way officers do
    return re.sub(r"\bTranch\b", "Tranche", s)


def _fp_numeric_cols(rows: list[dict]) -> list[str]:
    cols: list[str] = []
    for r in rows:
        for k, v in r.items():
            if k not in cols and _as_number(v) is not None and not _NONSTAT_COL.search(k):
                cols.append(k)
    return cols


def _fp_count_col(rows: list[dict]) -> "str | None":
    """The one count column (not money) of a Focus Plus result, or None."""
    cols = [c for c in _fp_numeric_cols(rows) if not _FP_MONEY_COL.search(c)]
    if len(cols) == 1:
        return cols[0]
    ben = [c for c in cols if "benefic" in c.lower()]
    return ben[0] if len(ben) == 1 else None


def _answer_has_pcts(answer: str, pcts: list[float]) -> bool:
    stated = [float(m.group(1)) for m in _PCT_TOKEN.finditer(answer or "")]
    return all(any(abs(s - p) <= 0.06 for s in stated) for p in pcts)


def _fp_breakdown_shares(answer: str, rows: list[dict]) -> str:
    """'Share of the 12,527 beneficiaries with a recorded status: Pending
    94.29%, …' for a GROUP BY status / verification / gender / occupation
    result (FOCUS-008, 011, 012). Shares are of the rows' own total, NULL group
    excluded, so they add to 100."""
    person = [c for c in (rows[0] if rows else {}) if c in _FP_PERSON_COLS]
    count_col = _fp_count_col(rows)
    if len(person) != 1 or not count_col:
        return answer
    attr = person[0]
    numeric = set(_fp_numeric_cols(rows))
    if any(k != attr and k not in numeric for k in rows[0]):
        # a second label column (district x status, year x gender): shares of
        # the grand total would mix groups — leave it to the table
        return answer
    pairs: list[tuple[object, "int | float"]] = []
    for r in rows:
        n = _as_number(r.get(count_col))
        if r.get(attr) is not None and n is not None:
            pairs.append((r[attr], n))
    total = sum(v for _l, v in pairs)
    if not pairs or total <= 0:
        return answer
    shares = [(lab, round(100.0 * v / total, 2)) for lab, v in pairs]
    if _answer_has_pcts(answer, [p for _l, p in shares]):
        return answer
    line = (f"Share of the {total:,} {_metric_label(count_col)} with a recorded "
            f"{_FP_PERSON_COLS[attr]}: " + ", ".join(f"{lab} {p:.2f}%" for lab, p in shares)
            + f" ({_FP_COHORT_TAIL}).")
    return f"{answer.rstrip()}\n\n{line}"


# The WHERE shapes the share recount understands. Anything else (OR, a
# subquery, a join, IN-lists) is left alone — better no percentage than a
# percentage over a different population than the bot's own figure.
_FP_WHERE_COLS = {"batch_label", "focus_status", "verification_status", "gender", "occupation",
                  "financial_year_short", "financial_year", "lgd_district", "block_name_raw",
                  "lgd_block", "lgd_village_name", "tranche_label"}
_FP_EQ_PRED = re.compile(
    r"^\s*(?P<up>UPPER\s*\(\s*)?(?P<col>[a-z_]+)\s*(?(up)\))\s*=\s*"
    r"(?P<up2>UPPER\s*\(\s*)?'(?P<val>(?:[^']|'')*)'\s*(?(up2)\))\s*$", re.IGNORECASE)
_FP_NOTNULL_PRED = re.compile(r"^\s*[a-z_]+\s+IS\s+NOT\s+NULL\s*$", re.IGNORECASE)


def _fp_parse_where(sql: str) -> "list[tuple[str, str, bool]] | None":
    """[(column, value, compared_upper)] for a single-SELECT Focus Plus count,
    or None when the WHERE holds anything but plain equality / IS NOT NULL."""
    s = re.sub(r"\s+", " ", sql or "").strip()
    bare = _SQL_LITERAL.sub("''", s)   # keywords inside a literal ('Others') don't count
    if len(re.findall(r"\bselect\b", bare, re.IGNORECASE)) != 1 or re.search(
            r"\b(join|with|union|or|in|not\s+in|like|between)\b|[<>!]|\(\s*select", bare, re.IGNORECASE):
        return None
    m = re.search(r"\bwhere\b(.*?)(?:\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b|$)",
                  s, re.IGNORECASE)
    if not m:
        return None
    out: list[tuple[str, str, bool]] = []
    for part in re.split(r"\band\b", m.group(1), flags=re.IGNORECASE):
        if _FP_NOTNULL_PRED.match(part):
            continue
        pm = _FP_EQ_PRED.match(part)
        if not pm or pm.group("col").lower() not in _FP_WHERE_COLS:
            return None
        out.append((pm.group("col").lower(), pm.group("val").replace("''", "'"),
                    bool(pm.group("up") or pm.group("up2"))))
    return out


def _fp_scope_text(preds: list[tuple[str, str, bool]]) -> str:
    bits = []
    for col, val, _u in preds:
        if col in ("lgd_district",):
            bits.append(f"in {_fp_title(val)}")
        elif col in ("block_name_raw", "lgd_block"):
            bits.append(f"in {_fp_title(val)} block")
        elif col in ("financial_year_short", "financial_year"):
            bits.append(f"in FY {val}")
        elif col == "tranche_label":
            bits.append(f"in {val}")
    return (" " + " ".join(bits)) if bits else ""


async def _fp_single_count_share(answer: str, sql: str, rows: list[dict]) -> str:
    """'That is 94.29% of the 12,527 beneficiaries in the 12.5K registration
    cohort …' for a one-number count filtered on status / verification /
    gender / occupation (FOCUS-013, 014, 016). The share is computed by a
    parameter-bound recount of the SAME filters, and is stated only when that
    recount reproduces the bot's own figure — so it can never describe a
    different population than the number beside it."""
    if len(rows) != 1:
        return answer
    metrics = [(k, v) for k, v in _row_metrics(rows[0]) if not _FP_MONEY_COL.search(k)]
    if len(metrics) != 1 or len(_row_metrics(rows[0])) != 1:
        return answer
    value = metrics[0][1]
    preds = _fp_parse_where(sql)
    if not preds or not any(c in _FP_PERSON_COLS for c, _v, _u in preds):
        return answer

    def _where(ps: list[tuple[str, str, bool]]) -> "tuple[str, list[str]]":
        conds, params = [], []
        for col, val, up in ps:
            params.append(val.upper() if up else val)
            conds.append(f"{'UPPER(' + col + ')' if up else col} = ${len(params)}")
        return " AND ".join(conds), params

    num_preds = preds if any(c == "batch_label" for c, _v, _u in preds) \
        else preds + [("batch_label", "12.5K", False)]
    den_preds = [p for p in num_preds if p[0] not in _FP_PERSON_COLS]
    try:
        nw, npar = _where(num_preds)
        dw, dpar = _where(den_preds)
        base = "SELECT COUNT(DISTINCT beneficiary_key) AS n FROM curated.v_focus_plus WHERE "
        num_rows, den_rows = await asyncio.gather(fetch_rows(base + nw, npar),
                                                  fetch_rows(base + dw, dpar))
        num, den = num_rows[0]["n"], den_rows[0]["n"]
    except Exception:  # noqa: BLE001
        logger.warning("Focus Plus share recount failed — answer left unchanged", exc_info=True)
        return answer
    if not den or int(num) != int(value):
        logger.info("Focus Plus share skipped: recount %s != bot figure %s", num, value)
        return answer
    pct = round(100.0 * int(num) / int(den), 2)
    if _answer_has_pcts(answer, [pct]):
        return answer
    fields = ", ".join(sorted({_FP_PERSON_COLS[c] for c, _v, _u in preds if c in _FP_PERSON_COLS}))
    line = (f"That is {pct:.2f}% of the {int(den):,} beneficiaries in the 12.5K registration "
            f"cohort{_fp_scope_text(den_preds)} ({fields} is recorded only for that cohort; "
            f"the 93K legacy cohort has none).")
    return f"{answer.rstrip()}\n\n{line}"


_FP_COMPARE_CUE = re.compile(
    r"\bcompar\w*|\bversus\b|\bvs\b\.?|\bdifference between\b|"
    r"\bwhich\b[\w\s]{0,30}\b(?:higher|more|larger|bigger|greater)\b", re.IGNORECASE)
_FP_COMBINED_SENT = re.compile(r"\b(?:combined|together|in total|across both|both [a-z]+ (?:is|are))\b",
                               re.IGNORECASE)


def _fp_comparison(question: str, rows: list[dict], answer: str) -> str:
    """State the difference and the higher side of a two-way comparison
    (FOCUS-026, 027, 028). The composer gave both figures and then their
    combined total — a number nobody asked for — in place of the difference."""
    if len(rows) != 2 or not _FP_COMPARE_CUE.search(question or ""):
        return answer
    labels = [k for k in rows[0] if _as_number(rows[0][k]) is None and rows[0][k] != rows[1].get(k)]
    if len(labels) != 1:
        return answer
    num_cols = _fp_numeric_cols(rows)
    q = question.lower()
    pick = None
    if "benefic" in q:
        pick = next((c for c in num_cols if "benefic" in c.lower()), None)
    if pick is None and re.search(r"disburs|amount|money|paid|fund|rupee", q):
        pick = next((c for c in num_cols if _FP_MONEY_COL.search(c)), None)
    if pick is None and "payment" in q:
        pick = next((c for c in num_cols if "payment" in c.lower()), None)
    if pick is None and len(num_cols) == 1:
        pick = num_cols[0]
    if pick is None:
        return answer
    a, b = _as_number(rows[0].get(pick)), _as_number(rows[1].get(pick))
    if a is None or b is None:
        return answer
    la, lb = _fp_title(rows[0][labels[0]]), _fp_title(rows[1][labels[0]])
    money = bool(_FP_MONEY_COL.search(pick))

    def f(v: "int | float") -> str:
        return _fp_rupees(v) if money else f"{v:,}"

    unit = "" if money else f" {_metric_label(pick)}"
    if a == b:
        line = f"{la} and {lb} are level, at {f(a)}{unit} each."
    else:
        (hl, hv), (ll, lv) = sorted([(la, a), (lb, b)], key=lambda p: p[1], reverse=True)
        diff = abs(a - b)
        diff = round(diff, 2) if isinstance(diff, float) else diff
        line = f"{hl} is higher by {f(diff)}{unit} ({hl} {f(hv)} vs {ll} {f(lv)})."
        nums = [float(t.replace(",", "")) for t in _NUM_TOKEN.findall(answer.replace("₹", ""))]
        if any(abs(n - diff) < 0.01 for n in nums) and re.search(
                rf"{re.escape(hl)}[^.]*\b(?:higher|more|exceed|ahead|greater|larger)\b", answer, re.IGNORECASE):
            return answer
    total = a + b
    kept = [s for s in re.split(r"(?<=[.!?])\s+", answer.strip())
            if not (_FP_COMBINED_SENT.search(s) and any(
                abs(float(t.replace(",", "")) - total) < 0.01
                for t in _NUM_TOKEN.findall(s.replace("₹", ""))))]
    return (" ".join(kept) + " " + line).strip()


_FP_BARE_NUM = re.compile(
    r"(?P<cur>₹\s?|\bRs\.?\s?|\bINR\s?)?(?<![\w.,])(?P<num>\d(?:[\d,]*\d)?(?:\.\d+)?)(?![\d,]*\.?\d)"
    r"(?!\s*(?:%|crore|cr\b|lakh|lac|million|mn\b|billion|[kK]\b))")


def _fp_format_numbers(answer: str, rows: list[dict]) -> str:
    """Rupee amounts from the result read '₹126,197,500.00', never
    '126197500.00'; large counts get thousands separators (KI-064). Only a
    token whose value IS a result value is touched. Also renames the
    SQL-alias 'amount raw' that a row-dump fallback exposes."""
    money: list[float] = []
    counts: list[int] = []
    for r in rows:
        for k, v in r.items():
            n = _as_number(v)
            if n is None or _NONSTAT_COL.search(k):
                continue
            if _FP_MONEY_COL.search(k):
                money.append(float(n))
            elif isinstance(n, int) and n >= 10000:
                counts.append(n)
    # the whole-result total the composer quotes from the digest ("the total
    # disbursed across all genders is 31317500") is money too
    for k in {k for r in rows for k in r if _FP_MONEY_COL.search(k)}:
        vals = [_as_number(r.get(k)) for r in rows]
        money.append(float(sum(v for v in vals if v is not None)))

    def _sub(m: "re.Match[str]") -> str:
        tok = m.group("num")
        try:
            val = float(tok.replace(",", ""))
        except ValueError:
            return m.group(0)
        if val >= 1000 and any(abs(val - mv) < 0.005 for mv in money):
            return _fp_rupees(val)
        if not m.group("cur") and "," not in tok and "." not in tok and int(val) in counts:
            return f"{int(val):,}"
        return m.group(0)

    out = _FP_BARE_NUM.sub(_sub, answer)
    out = re.sub(r"\bamount raw\b", "amount disbursed", out)
    # the row-dump fallback's bare "₹31,317,500.00 amount disbursed."
    return re.sub(r"^(₹[\d,]+\.\d{2}) amount disbursed\.$", r"Amount disbursed: \1.", out.strip())


_FP_LABEL_NAME = {"lgd_district": "district", "block_name_raw": "block", "lgd_block": "block",
                  "lgd_village_name": "village", "tranche_label": "tranche", "batch_label": "batch",
                  "bank_name_raw": "bank", "scheme_name": "programme"}   # scheme_name: CM Elevate lists


def _fp_complete_list(answer: str, rows: list[dict]) -> str:
    """A per-category list (one label column, one figure, up to 15 rows) must
    name every category. The composer wrote "WEST GARO HILLS has 35,039 …
    The remaining districts show 11,379, 11,007, 8,612, …" — ten figures with
    no district beside them (FOCUS-002, both QA rounds). When any label is
    missing from the text, the answer is rebuilt from the rows."""
    if not 3 <= len(rows) <= 15:
        return answer
    nums = _fp_numeric_cols(rows)
    labels = [k for k in rows[0] if k not in nums]
    if len(nums) != 1 or len(labels) != 1:
        return answer
    lab, met = labels[0], nums[0]
    low = answer.lower()
    if all(r.get(lab) is None or str(r[lab]).lower() in low for r in rows):
        return answer
    name = _FP_LABEL_NAME.get(lab, lab.replace("_", " "))
    money = bool(_FP_MONEY_COL.search(met))
    items = []
    for r in rows:
        v = _as_number(r.get(met))
        if v is None:
            continue
        who = _fp_title(r[lab]) if r.get(lab) is not None else f"(no {name} recorded)"
        items.append(f"{who} {_fp_rupees(v) if money else f'{v:,}'}")
    return f"{_metric_label(met).capitalize()} by {name}: " + "; ".join(items) + "."


async def _focusplus_answer_guarantees(question: str, sql: str, rows: list[dict], answer: str) -> str:
    """Every Focus Plus DATA answer passes through here after the composer."""
    if not rows:
        return answer
    answer = _fp_complete_list(answer, rows)
    answer = _fp_breakdown_shares(answer, rows)
    answer = await _fp_single_count_share(answer, sql, rows)
    answer = _fp_comparison(question, rows, answer)
    answer = _fp_format_numbers(answer, rows)
    if re.search(r"block_name_raw\s*\)?\s*=\s*'NAN'", sql or "", re.IGNORECASE) \
            and "real block" not in answer:
        answer = f"{_FP_BLANK_BLOCK_LEAD} {answer}"
    return answer


# ── CM Elevate answer guarantees (use-case QA 2026-09-27) ────────────────────
# The SQL was right in every one of these; the composed TEXT dropped or garbled
# what the use case asks for: a programme list naming 5 of 15 (KI-068 part 2),
# no combined total beside a programme split (KI-069), no difference in a
# comparison (KI-071), a scheme x sector x pendency result written as "9 and 3
# for Poultry and Piggery respectively … 1, 0, 14, 5, 0, and 0 for each
# corresponding row" (KI-073), and a genuine 0 hedged as "doesn't cover"
# (KI-070). Same project pattern as the Focus Plus guarantees: check the text
# against the rows, and rebuild or append only what is missing.
_CME_LABEL_STOP = {"meghalaya", "prime", "scheme", "development", "farming", "and", "the", "of",
                   "chief", "minister", "minister's", "centre", "hills", "not", "block"}
_CME_GARBLED = re.compile(r"\brespectively\b|\bcorresponding\s+rows?\b|\bfor\s+each\s+row\b|"
                          r"\beach\s+row\b", re.IGNORECASE)


def _cme_short(label: object) -> str:
    """'Meghalaya Piggery Development Scheme' -> 'Piggery Development'; district
    capitals -> title case; anything else as stored."""
    s = _fp_title(label)   # title-case stored capitals first, so "PRIME SEED" survives
    s = re.sub(r"^PRIME Small Enterprise Empowerment and Development \(SEED\)$", "PRIME SEED", s)
    return re.sub(r"^Meghalaya\s+|\s+Scheme$", "", s)


_CME_WHOLE_LIST_Q = re.compile(r"\beach\b|\bevery\b|\bwise\b|\bdistribution\b|\bbreak\s*down\b|"
                               r"\bbreakdown\b|\ball\s+(?:the\s+)?(?:programmes?|programs?|schemes?|"
                               r"districts?|blocks?|villages?|sectors?)\b", re.IGNORECASE)


def _cme_complete_list(question: str, answer: str, rows: list[dict]) -> str:
    """KI-068 (text half): "applicants under each CM ELEVATE program" named 5 of
    the 15 programmes ("the remaining schemes range from 4 … up to 3,617").
    When the question asks for EVERY value (each / distribution / breakdown)
    and some row's figure is not beside its label, rebuild the list from the
    rows. A "which is highest" question naming its top few is left alone."""
    if not _CME_WHOLE_LIST_Q.search(question or "") or not 2 <= len(rows) <= 40:
        return answer
    nums = _fp_numeric_cols(rows)
    labels = [k for k in rows[0] if k not in nums]
    if len(nums) != 1 or len(labels) != 1:
        return answer
    lab, met = labels[0], nums[0]
    sents = _cme_sentences(answer)
    # "…Warehouse, Agriculture Response Vehicle, Goat Farming and Any Business Venture
    # had 4, 4, 4, 1, and 1 applicants respectively" (Laskein, all-blocks run
    # 2026-09-28) states every figure but not beside its name — rebuilt too.
    if not _CME_GARBLED.search(answer or "") and all(
            _as_number(r.get(met)) is None or _cme_row_stated(
                sents, [r.get(lab) if r.get(lab) is not None else "(not recorded)"], _as_number(r.get(met)))
            for r in rows):
        return answer
    name = _FP_LABEL_NAME.get(lab, lab.replace("_", " "))
    items = [f"{_cme_short(r[lab]) if r.get(lab) is not None else '(not recorded)'} "
             f"{_fmt_num(_as_number(r.get(met)))}" for r in rows if _as_number(r.get(met)) is not None]
    total = sum(_as_number(r.get(met)) or 0 for r in rows)
    logger.info("CM Elevate: rebuilt a %s list — the answer left out some %s", name, name)
    return (f"{_metric_label(met).capitalize()} by {name} ({len(items)}): " + "; ".join(items)
            + f". Total {_fmt_num(total)}.")


def _cme_label_keys(label: object) -> list[str]:
    words = re.findall(r"[a-z][a-z']+", str(label).lower())
    keys = [w for w in words if w not in _CME_LABEL_STOP and len(w) > 2]
    return keys or words


def _cme_sentences(answer: str) -> list[str]:
    return [s.lower() for s in re.split(r"(?<=[.;!?])\s+|\n+", answer or "") if s.strip()]


def _cme_nums(text: str) -> set[float]:
    return {float(t.replace(",", "")) for t in _NUM_TOKEN.findall(text) if t.replace(",", "").lstrip("-")}


def _cme_row_stated(sentences: list[str], labels: list[object], value: "int | float") -> bool:
    """Some sentence names one of the row's labels AND states its value."""
    for s in sentences:
        if any(k in s for lab in labels for k in _cme_label_keys(lab)) and any(
                abs(n - float(value)) < 0.005 for n in _cme_nums(s)):
            return True
    return False


def _cme_grouped_rows(answer: str, rows: list[dict]) -> str:
    """KI-073: a result with TWO label columns (scheme x sector, scheme x
    district) must state every figure next to its own labels. When any row's
    figure cannot be found beside one of its labels, or the text falls back on
    "respectively" / "each corresponding row", rebuild it grouped by the first
    label, with that group's total."""
    if not 2 <= len(rows) <= 30:
        return answer
    nums = _fp_numeric_cols(rows)
    labels = [k for k in rows[0] if k not in nums]
    if len(labels) != 2 or not 1 <= len(nums) <= 3:
        return answer
    sents = _cme_sentences(answer)
    ok = not _CME_GARBLED.search(answer or "") and all(
        _cme_row_stated(sents, [r.get(labels[0]), r.get(labels[1])], _as_number(r.get(m)) or 0)
        for r in rows for m in nums if _as_number(r.get(m)) is not None)
    if ok:
        return answer
    g, sub = (labels if labels[0] == "scheme_name" or labels[1] != "scheme_name"
              else list(reversed(labels)))
    groups: dict[object, list[dict]] = {}
    for r in rows:
        groups.setdefault(r.get(g), []).append(r)

    def figs(vals: dict) -> str:
        return ", ".join(f"{_metric_label(m)} {_fmt_num(vals[m])}" for m in nums if vals.get(m) is not None)

    lines = []
    for key, rs in groups.items():
        parts = "; ".join(f"{_cme_short(r.get(sub)) if r.get(sub) is not None else '(not recorded)'}: "
                          f"{figs({m: _as_number(r.get(m)) for m in nums})}" for r in rs)
        if len(rs) > 1:
            tot = {m: sum(_as_number(r.get(m)) or 0 for r in rs) for m in nums}
            lines.append(f"- {_cme_short(key)} — total {figs(tot)}. By "
                         f"{_FP_LABEL_NAME.get(sub, sub.replace('_', ' '))}: {parts}.")
        else:
            lines.append(f"- {_cme_short(key)} — {parts}.")
    logger.info("CM Elevate: rebuilt a two-label answer from the rows (figures were not beside their labels)")
    return "Here is the breakdown:\n" + "\n".join(lines)


_CME_STATUS_WORDS = (("hold", r"on[\s_-]?hold|pending"), ("valid", r"\bvalid\b|verified"),
                     ("sector", r"sector"), ("withdraw", r"withdr"))


def _cme_pick_count_col(question: str, rows: list[dict]) -> "str | None":
    cols = _fp_numeric_cols(rows)
    if len(cols) == 1:
        return cols[0]
    q = (question or "").lower()
    for colword, qrx in _CME_STATUS_WORDS:
        hit = [c for c in cols if colword in c.lower()]
        if len(hit) == 1 and re.search(qrx, q):
            return hit[0]
    return None


# KI-182: "GROUP BY scheme_name" and nothing after it but ORDER BY / LIMIT. Only
# then does a programme missing from the rows mean 0 under the query's filters
# (a HAVING could have filtered it, another GROUP BY key split it, a LIMIT
# smaller than the programme count cut it).
_CME_GROUP_BY_SCHEME_ONLY = re.compile(
    r"\bGROUP\s+BY\s+scheme_name\s*(?:ORDER\s+BY\b[^;]*?)?(?:\bLIMIT\s+(?P<limit>\d+)\s*)?;?\s*$",
    re.IGNORECASE)
_CME_AGG_ALIAS = re.compile(r"\b(?:COUNT|SUM)\s*\((?:[^()]|\([^()]*\))*\)\s+AS\s+(\w+)", re.IGNORECASE)


def _cme_asked_programmes(sql: str) -> list[str]:
    """The programmes the query was asked about (its scheme_name IN list), when
    an absent one can only mean 0; else []. A LIMIT is harmless when it is at
    least the number of programmes — live 2026-10-02 the SQL usually ended
    "LIMIT 1000" (the model's or run_readonly's auto-limit), which disabled this
    guard on 7 of 30 one-zero questions."""
    m = _CME_SCHEME_IN_RE.search(sql or "")
    g = _CME_GROUP_BY_SCHEME_ONLY.search(sql or "")
    if not m or not g or re.search(r"\bHAVING\b", _SQL_LITERAL.sub("''", sql or ""), re.IGNORECASE):
        return []
    names = [v.replace("''", "'") for v in re.findall(r"'((?:[^']|'')*)'", m.group(1))]
    if g.group("limit") is not None and int(g.group("limit")) < len(names):
        return []
    return names


def _cme_all_zero_answer(sql: str, answer: str, display: dict) -> str:
    """KI-182 (CM-ELEVATE-OFF-009, both programmes 0): the SQL ran and returned
    no row for any asked programme — each one has 0. The generic "couldn't find
    any matching records for <district>, <P1> and <P2>" read as if the question
    had failed (141 'pass with remark' in the 2026-10-01 all-pairs run)."""
    asked = _cme_asked_programmes(sql)
    aliases = _CME_AGG_ALIAS.findall(sql or "")
    if len(asked) < 2 or len(aliases) != 1:
        return answer
    label = _metric_label(aliases[0])
    place = next((display.get(k) for k in ("village", "block", "district") if (display or {}).get(k)), None)
    lead = f"In {place}, none of these {len(asked)} programmes has any {label}: " if place else         f"None of these {len(asked)} programmes has any {label}: "
    return (lead + "; ".join(f"{_cme_short(n)}: 0" for n in asked)
            + f". Combined across these {len(asked)} programmes: 0 {label}.")


def _cme_multi_scheme_total(question: str, sql: str, rows: list[dict], answer: str) -> str:
    """KI-069: a question naming 2+ programmes gets each programme's figure
    AND their combined total. Rows come split by scheme_name (the SQL guard
    ensures it); this adds any programme figure the text left out, then the
    combined total if it is not stated. Exact: request_id never repeats across
    programmes.

    KI-182: a programme with no applications in the place returns NO row, so
    "<district> under PRIME SEED and Cinema Theatre" was answered "PRIME SEED:
    524 applicants." — Cinema Theatre and the total silently left out (567 of
    1,260 questions in the 2026-10-01 all-pairs run). An asked programme with
    no row is now stated as 0 (_cme_asked_programmes says when that is exact)."""
    m = _CME_SCHEME_IN_RE.search(sql or "")
    if not m or not 1 <= len(rows) <= 15 or not all("scheme_name" in r for r in rows):
        return answer
    labels = [k for k in rows[0] if k not in _fp_numeric_cols(rows)]
    if labels != ["scheme_name"]:
        return answer
    col = _cme_pick_count_col(question, rows)
    if not col:
        return answer
    vals = [(r["scheme_name"], _as_number(r.get(col))) for r in rows]
    if any(v is None for _n, v in vals):
        return answer
    present = {str(n).casefold() for n, _v in vals}
    zeros = [(n, 0) for n in _cme_asked_programmes(sql) if n.casefold() not in present]
    vals += zeros
    if len(vals) < 2:
        return answer
    sents = _cme_sentences(answer)
    missing = [(n, v) for n, v in vals if not _cme_row_stated(sents, [n], v)]
    out = answer.rstrip()
    if missing:
        out += "\n\n" + "; ".join(f"{_cme_short(n)}: {_fmt_num(v)}" for n, v in vals) + "."
    total = sum(v for _n, v in vals)
    # With a programme filled in as 0 the total equals a figure already stated,
    # so it is said outright (the OFF-009 expected result names the total).
    if zeros or not any(abs(n - total) < 0.005 for n in _cme_nums(answer)):
        out += f"\n\nCombined across these {len(vals)} programmes: {_fmt_num(total)} {_metric_label(col)}."
    return out


def _cme_comparison(question: str, rows: list[dict], answer: str) -> str:
    """KI-071 (CM-ELEVATE-OFF-027): "compare Piggery and Poultry across Ri Bhoi
    and West Khasi Hills" gave the four counts and no difference. For a
    programme x place result, state for each programme which place is higher
    and by how much, and for each place which programme is higher. A plain
    two-row comparison reuses _fp_comparison."""
    if not _FP_COMPARE_CUE.search(question or "") or not rows:
        return answer
    nums = _fp_numeric_cols(rows)
    labels = [k for k in rows[0] if k not in nums]
    if len(labels) == 1:
        return _fp_comparison(question, rows, answer)
    col = _cme_pick_count_col(question, rows)
    if len(labels) != 2 or not col:
        return answer
    vals = {(r.get(labels[0]), r.get(labels[1])): _as_number(r.get(col)) for r in rows}
    a_vals = list(dict.fromkeys(k[0] for k in vals))
    b_vals = list(dict.fromkeys(k[1] for k in vals))
    lines: list[str] = []
    diffs: list[float] = []

    def one(fixed: object, x: object, y: object, vx: "int | float", vy: "int | float") -> str:
        if vx == vy:
            return f"{_cme_short(fixed)}: {_cme_short(x)} and {_cme_short(y)} are level ({_fmt_num(vx)} each)"
        (hn, hv), (ln, lv) = sorted([(x, vx), (y, vy)], key=lambda p: p[1], reverse=True)
        diffs.append(abs(hv - lv))
        return (f"{_cme_short(fixed)}: {_cme_short(hn)} is higher by {_fmt_num(hv - lv)} "
                f"({_fmt_num(hv)} vs {_fmt_num(lv)})")

    for fixed_vals, other_vals, pos in ((a_vals, b_vals, 1), (b_vals, a_vals, 0)):
        if len(other_vals) != 2:
            continue
        for f in fixed_vals:
            x, y = other_vals
            kx, ky = ((f, x), (f, y)) if pos == 1 else ((x, f), (y, f))
            if vals.get(kx) is None or vals.get(ky) is None:
                continue
            lines.append(one(f, x, y, vals[kx], vals[ky]))
    if not lines:
        return answer
    stated = _cme_nums(answer)
    if diffs and all(any(abs(n - d) < 0.005 for n in stated) for d in diffs) and re.search(
            r"\bhigher\b|\bmore\b|\bdifference\b", answer or "", re.IGNORECASE):
        return answer
    return answer.rstrip() + "\n\nDifferences: " + "; ".join(lines) + "."


_NUMBER_WORDS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                 "fifteen sixteen seventeen eighteen nineteen twenty").split()
# tens and their compounds too: "Twenty-one producer groups received payments under
# Focus Legacy in Khliehriat East" (Focus Legacy all-villages run 2026-10-02)
_NUMBER_TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
_NUMBER_WORD_ALT = (r"(?:" + "|".join(_NUMBER_TENS) + r")(?:[\s-]+(?:" + "|".join(_NUMBER_WORDS[1:10]) + r"))?|"
                    + "|".join(_NUMBER_WORDS))
_NUMBER_WORD_RE = re.compile(r"\b(" + _NUMBER_WORD_ALT + r")\b(?!\s+of\b)(?=(?:\s+[A-Za-z-]+){0,3}?\s+"
                             r"(?:applicants?|applications?|verified|pending|rejected|withdrawn|villages?|"
                             r"programmes?|programs?|schemes?|records?|"
                             # every scheme's count nouns (all-blocks run 2026-10-02: "Eighteen
                             # producer groups received payments under Focus Legacy in Shella
                             # Bholaganj block")
                             r"groups?|PGs?|beneficiar(?:y|ies)|houses?|households?|payments?|members?|"
                             r"person[\s-]?days?|blocks?|districts?)\b)", re.IGNORECASE)


def _cme_digits_for_number_words(answer: str, rows: list[dict]) -> str:
    """"Five CM ELEVATE applicants in Sasatgre village have completed data
    verification" (all-villages run 2026-09-28): the figure was right, but a
    spelled-out number is invisible to the numeric-faithfulness checks, so a WRONG
    one would pass too. A number word before a count noun becomes digits when that
    value is in the result; otherwise it is left for the checks to see."""
    vals = {int(v) for r in rows for _k, v in _row_metrics(r) if isinstance(v, (int, float)) and float(v).is_integer()}

    def sub(m: "re.Match[str]") -> str:
        parts = re.split(r"[\s-]+", m.group(1).lower())
        n = (_NUMBER_TENS.index(parts[0]) + 2) * 10 if parts[0] in _NUMBER_TENS else _NUMBER_WORDS.index(parts[0])
        n += _NUMBER_WORDS.index(parts[1]) if len(parts) > 1 else 0
        return str(n) if n in vals else m.group(0)
    return _NUMBER_WORD_RE.sub(sub, answer or "")


def _cme_sector_answer_names_wrong_programme(answer: str, sql: str, rows: list[dict]) -> str:
    """Tester re-run (2026-09-28): "How many applicants under Meghalaya Poultry
    Farming Scheme belong to Piggery" was answered "The Meghalaya Piggery Development
    Scheme had 5 applicants under the Meghalaya Poultry Farming Scheme" — the SECTOR
    read as a programme. When a one-row sector count names a programme that is
    neither filtered nor in the result, the sentence is rebuilt from the row."""
    sec = re.search(r"sector_id'?\s*(?:I?LIKE|=)\s*'%?([A-Za-z]+)%?'", sql or "", re.IGNORECASE)
    if not sec or len(rows) != 1:
        return answer
    filtered = set(re.findall(r"scheme_name\s*=\s*'((?:[^']|'')*)'", sql or "", re.IGNORECASE))
    m = _CME_SCHEME_IN_RE.search(sql or "")
    if m:
        filtered |= {x.replace("''", "'") for x in _SQL_LITERAL.findall(m.group(1))}
    named = {p for p in _CME_PROGRAMME_WORDS if p.lower() in (answer or "").lower()}
    if not named or named <= filtered | {str(rows[0].get("scheme_name") or "")}:
        return answer
    metrics = _row_metrics(rows[0])
    if not metrics:
        return answer
    sector = sec.group(1).title()
    who = (" under the " + " and ".join(sorted(filtered))) if filtered else ""
    parts = [f"{_fmt_num(metrics[0][1])} applicants{who} have their sector recorded as {sector}"]
    extra = {k: v for k, v in metrics[1:]}
    if "sector_recorded" in extra:
        parts.append(f"sector is recorded for {_fmt_num(extra['sector_recorded'])} of them")
    if "scheme_total" in extra:
        parts.append(f"out of {_fmt_num(extra['scheme_total'])} applicants in total")
    logger.info("CM Elevate: sector answer named a programme it never counted — rebuilt")
    return "; ".join(parts) + "."


async def _cm_elevate_answer_guarantees(question: str, sql: str, rows: list[dict], answer: str,
                                        display: dict) -> str:
    """Every CM Elevate DATA answer passes through here after the composer."""
    if not rows:
        return _cme_all_zero_answer(sql, answer, display)
    answer = _cme_digits_for_number_words(answer, rows)
    answer = _cme_sector_answer_names_wrong_programme(answer, sql, rows)
    answer = _mgnrega_zero_backstop(answer, rows, display)   # a genuine 0 is stated as 0 (KI-070)
    answer = _cme_grouped_rows(answer, rows)
    answer = _cme_complete_list(question, answer, rows)
    answer = _cme_multi_scheme_total(question, sql, rows, answer)
    # KI-074 decided 2026-09-28 (product owner): "pending" = verification On Hold
    # only. The interim second line with the file_status Pending count is gone.
    return _cme_comparison(question, rows, answer)


# schemes whose one-figure answers must state THAT figure (see compose_response)
_ONE_FIGURE_SCHEMES = (["Focus Plus"], ["CM Elevate"])


async def compose_response(question: str, sql: str, rows: list[dict],
                           notes: list[str] | None = None,
                           entities: dict[str, str] | None = None,
                           schemes: list[str] | None = None,
                           style_examples: str = "",
                           extra_numbers: "set[str] | None" = None) -> str:
    # extra_numbers: figures a caller re-queried and handed over in a note (a
    # list's true total), which the answer may quote although no row holds
    # them. None for every caller that doesn't pass it — unchanged behaviour.
    # style_examples: optional worked answers for the scheme (CM Elevate
    # Legacy's answer shots). Empty for every other scheme, which leaves the
    # prompt exactly as it was.
    if not rows:
        return _no_data_answer(schemes, entities)
    preview = rows[:40]
    truncated = len(rows) > len(preview)
    # "No usable value" — every numeric cell is 0 or null (rows is non-empty
    # here; a truly empty result returns via _no_data_answer above). This is
    # the shape a metric the data simply doesn't track comes back as; when we
    # see it, hand the composer the real metric list so it can tell the user
    # exactly what IS available rather than a vague "not covered".
    _nums = [n for r in rows for _k, n in _row_metrics(r)]
    # An aggregate that matched NOTHING comes back as one row of NULLs, not as
    # zero rows, so the `if not rows` guard above cannot catch it. _row_metrics
    # drops NULL cells (they are not numbers), which left _nums empty and this
    # test False — the composer then received a row whose only value was None
    # and reported "the membership count is null", as if the database held a
    # null for that group rather than nothing having matched the filters
    # (reported 2026-09-22, "members in Sakania Producer Group"). Detect the
    # all-NULL row explicitly.
    _all_null = bool(rows) and not _nums and all(
        v is None for r in rows for v in r.values()
    )
    no_usable_value = (bool(_nums) and all(n in (0, None) for n in _nums)) or _all_null
    metrics_block = ""
    if no_usable_value:
        metrics_block = (
            "\nMetrics the data DOES carry — if the question asked for something "
            "that is not in this list, say plainly it isn't tracked in the "
            "available scheme data, then name what is:\n"
            + available_metrics_text(schemes or []) + "\n"
        )
    # A metric the schema simply doesn't carry comes back as no rows, or as a
    # single 0 / NULL. Tell the composer to say that plainly instead of
    # reporting a confident "0" the user will read as a real measurement.
    guidance = (
        "If the result contains a number that answers the question, state it "
        "plainly as the answer — a nonzero COUNT is exactly the count that was "
        "asked for; never reply that the data 'doesn't cover' a metric the query "
        "just counted. ONLY when the result is empty, or the value is 0 or null, "
        "do NOT assert a real count of zero — instead say the data available "
        "doesn't cover that metric for the scheme/area asked, and name what IS "
        "available if you can tell from the query. Never describe a column the "
        "query didn't select. Copy every number digit-for-digit from the result "
        "JSON or the whole-result summary — do not shorten, round or reformat it "
        "(adding thousands separators is fine). If a Context note below says the "
        "question assumed a figure the data contradicts, correct that figure in "
        "your first sentence and answer from the real value — never echo the "
        "user's assumed number as if it were right. Make that first sentence ONE "
        "clean clause naming the real figure (e.g. 'Tranche 1 shows 0 beneficiaries, "
        "not the 4 the question assumes.') — never a reasoning trail like 'contradicting "
        "the assumed figure of X, so Y cannot be calculated because Z is empty'. Put "
        "the supporting figures in a separate, plainly listed second sentence (e.g. "
        "'Tranches 2, 3 and 4 each had 93,286 beneficiaries, out of 373,144 total.') "
        "rather than chaining them onto the correction with 'The available figures "
        "are...'. If the result is a list of "
        "names or rows and carries no numeric column, that list IS the answer — "
        "it is the exact set the query already selected as matching the question "
        "(e.g. 'which districts have both schemes'). Name those values as the "
        "answer; never say the data 'only lists names' or lacks the detail to "
        "decide — the filtering happened in the query. Never state or imply a "
        "scope the query wasn't actually filtered to — a specific tranche, year, "
        "district, block, village or category — unless it appears in the Entity "
        "names block below or literally in the question; if the question and the "
        "Entity names block name no tranche/year/area, the result covers all of "
        "them and must be described that way (e.g. 'across all tranches'), not "
        "attributed to one you're not told about."
    )
    # For any multi-row result, hand the composer a deterministic summary built
    # from EVERY row — totals, mean, extremes, full dimension coverage — so its
    # answer matches the charts/table instead of describing only the ~40 rows it
    # can see. Also stops it declaring that rows it can't see "have no data".
    digest_text, digest_nums = _result_digest(rows)
    summary_block = ""
    if digest_text:
        warn = ""
        if truncated:
            warn = (
                f" Only the first {len(preview)} of {len(rows)} rows appear below; "
                "do NOT say any district, block, village, year or category is "
                "missing or has no data merely because it is absent from them."
            )
        summary_block = (
            "\nWhole-result summary — computed from EVERY row. Use THESE figures "
            "for any total, average, highest/lowest or coverage statement; they "
            "are authoritative even though only some rows are shown below." + warn
            + "\n" + digest_text + "\n"
        )
    notes_block = ""
    if notes:
        notes_block = "\nContext (question premises to reconcile against the data — " \
                      "correct any the result contradicts):\n" + \
                      "\n".join(f"  - {n}" for n in notes) + "\n"
    names_block = _entity_names_block(entities)
    prompt = f"""Answer the user's question in one to three plain sentences, using ONLY the
numbers in the result and the whole-result summary below — do not invent or round
differently than shown. Write like a direct briefing: lead with the fact or figure
itself, not with "The data shows..." or a description of what the query returned.
Keep each sentence to one idea — short and declarative, not a chain of clauses
joined by "so" / "as" / "which means".
{guidance}
{metrics_block}{names_block}{summary_block}{notes_block}{style_examples}
Question: "{question}"
Result ({len(rows)} row(s), showing up to {len(preview)}):
{json.dumps(preview, default=str)}

Answer:"""
    context_budget.log_prompt_context("compose", [
        ("guidance", guidance), ("available_metrics", metrics_block),
        ("entity_names", names_block), ("result_summary", summary_block),
        ("premise_notes", notes_block), ("style_examples", style_examples),
        ("question", question), ("result_rows", json.dumps(preview, default=str)),
    ], prompt=prompt, rows=len(rows), rows_shown=len(preview))
    answer = await llm.call_response_composer(prompt)

    data_nums = _data_numbers(preview) | digest_nums | set(extra_numbers or ())
    # A note may ask the composer to name the figure the question wrongly assumed
    # ("not the 1.71 L assumed…") — allow those premise numbers through the
    # faithfulness check so the correction itself isn't flagged as a misquote.
    if notes:
        for _p in premise_check.extract_premises(question):
            data_nums.add(f"{_p.value:g}")
            data_nums.add(re.sub(r"[^\d.]", "", _p.text) or f"{_p.value:g}")
    # "households completed 100 days" — the 100 is the MGNREGA measure's NAME,
    # not a figure. Every correct sentence about it ("3.39% of households
    # completed 100 days") was flagged as a misquote and replaced by the
    # row-dump fallback "3.39 completion 100 day pct." (MGNREGA QA 2026-09-26,
    # DATA-009/020). Allowed only when the question itself asks about it.
    if schemes and "MGNREGA" in schemes and re.search(
            r"\b100[\s-]?days?\b|\bhundred\s+days?\b", question or "", re.IGNORECASE):
        data_nums.add("100")
    data_nums |= _sql_literal_numbers(sql)
    data_nums |= set(_CHIP_LGD_CODE_RE.findall(question or ""))   # a twin-village chip's own code
    answer = _fix_digit_grouping(answer, data_nums)
    if schemes in _ONE_FIGURE_SCHEMES and schemes == ["CM Elevate"]:
        # spelled-out figures ("Five applicants") become digits BEFORE the checks, so a
        # wrong one leaves the true figure missing and is caught below (2026-09-28)
        answer = _cme_digits_for_number_words(answer, preview)
    misquoted = bool(data_nums) and not _answer_numbers_faithful(answer, data_nums)
    # A single aggregate row with 2+ metrics (e.g. a cross-scheme MGNREGA + PMAY
    # count) — the answer must report every one, not just the first scheme.
    dropped_metric = (len(preview) == 1 and len(_row_metrics(preview[0])) >= 2
                      and not _answer_covers_metrics(answer, preview[0]))
    # Focus Plus: a one-figure answer must state THAT figure. The check above
    # ignores whole numbers under 100 (ordinals, "one or two"), so "There is 1
    # beneficiary" for a result of 11 passed (NONGLANG, all-villages run
    # 2026-09-27). Numbers equal to 0 are left to the zero / no-data wording.
    # CM Elevate joined 2026-09-28 (all-blocks smoke run: "There is 1 pending CM
    # ELEVATE application in Ranikor block" for a result of 12).
    if schemes in _ONE_FIGURE_SCHEMES and not dropped_metric and len(preview) == 1:
        _one = _row_metrics(preview[0])
        if len(_one) == 1 and _one[0][1] not in (0, None) and not _answer_covers_metrics(answer, preview[0]):
            dropped_metric = True

    if misquoted or dropped_metric:
        why = "MISQUOTED a number" if misquoted else "left out one of the result values"
        logger.warning("compose_response: answer %s (%r) — strict retry", why, answer[:160])
        if len(preview) == 1:
            allowed = "; ".join(f"{k} = {v}" for k, v in preview[0].items())
        else:
            allowed = ", ".join(sorted(data_nums, key=len, reverse=True))
        strict_prompt = (
            prompt + "\n" + answer.strip() +
            f"\n\nThat answer {why}. State EVERY value in the result, each labelled with what "
            "it measures, digit-for-digit (thousands separators allowed). The values are:\n  "
            + allowed + "\nRewrite the answer now.\n\nAnswer:"
        )
        answer = _fix_digit_grouping(await llm.call_response_composer(strict_prompt), data_nums)
        still_bad =(bool(data_nums) and not _answer_numbers_faithful(answer, data_nums)) or (
            len(preview) == 1 and len(_row_metrics(preview[0])) >= 2
            and not _answer_covers_metrics(answer, preview[0])) or (
            # the Focus Plus one-figure rule above, re-applied to the retry
            schemes in _ONE_FIGURE_SCHEMES and len(preview) == 1 and len(_row_metrics(preview[0])) == 1
            and _row_metrics(preview[0])[0][1] not in (0, None)
            and not _answer_covers_metrics(answer, preview[0]))
        if still_bad:
            logger.warning("compose_response: retry still wrong — using deterministic answer")
            answer = _deterministic_answer(preview)

    # Deterministic safety net for the opposite failure: the query DID return a
    # real, non-zero number, but the composer hedged with a "not covered / no
    # data / can't break it down" phrasing anyway (seen when the question names
    # two categories joined by "or" and the query returns their combined COUNT).
    # A usable number must be reported as the answer.
    if _HEDGE_RE.search(answer):
        metrics_now = _row_metrics(preview[0]) if len(preview) == 1 else []
        if metrics_now and all(v not in (0, None) for _k, v in metrics_now):
            logger.warning("compose_response: hedged over a real value %r — deterministic answer",
                           answer[:160])
            answer = _deterministic_answer(preview)
        elif _is_plain_list_result(rows):
            logger.warning("compose_response: hedged over a %d-row list result %r — "
                           "deterministic list answer", len(rows), answer[:160])
            answer = _deterministic_list_answer(rows)
        elif len(preview) >= 2 and any(
            v not in (0, None) for r in preview for _k, v in _row_metrics(r)
        ):
            # A multi-row breakdown (e.g. GROUP BY category, COUNT(*)) that
            # carries at least one real, non-zero metric — neither of the two
            # branches above catches this shape (not a single row, and
            # _is_plain_list_result excludes rows with numeric metrics), so a
            # composer hedge here used to slip through unchecked while the
            # chart/table built from the same rows showed real data.
            logger.warning("compose_response: hedged over a %d-row breakdown %r — "
                           "deterministic answer", len(preview), answer[:160])
            answer = _deterministic_answer(preview)
    return answer


async def classify_intent(question: str) -> str:
    """DATA (a number from megh_db) vs KNOWLEDGE (how the scheme works, from the
    reference docs). Keyword fast-path first; one classifier call otherwise."""
    # "what are common districts in both schemes?" opens with "what are", which
    # otherwise reads as KNOWLEDGE — but it is a list computed from the coverage
    # data, never something in the reference docs. Force DATA before the keyword
    # fast-path so the "what are" knowledge cue can't win.
    if _CROSS_SCHEME_SET_QUESTION.search(question):
        return "DATA"
    # "How many members are there in Bak-15 Wachal Pg?" is a lookup in the data;
    # with no counting noun the classifier sometimes sent it to the reference
    # docs ("the reference material does not contain…").
    if _pg_name_question(question):
        return "DATA"
    if _PROGRAMME_DESIGN_CUE.search(question):
        return "KNOWLEDGE"
    if _BREAKDOWN_CUE.search(question):
        return "DATA"
    if _METRIC_WHATIS_CUE.search(question):
        return "DATA"
    if _DATA_HINTS.search(question) and not _KNOWLEDGE_HINTS.search(question):
        return "DATA"
    if _KNOWLEDGE_HINTS.search(question) and not _DATA_HINTS.search(question):
        return "KNOWLEDGE"
    if _APPLICANT_PLACE_CUE.search(question) and not _KNOWLEDGE_HINTS.search(question):
        return "DATA"
    prompt = f"""Classify the question into exactly one label:
  DATA      - needs a number/count/list computed from the scheme database. This
              INCLUDES comparison and correlation questions ("do districts with
              high X also have high Y", "is A related to B by district", "which
              districts lead on both schemes") — answered by querying the figures
              and comparing them, NOT from the reference docs.
  KNOWLEDGE - asks how a scheme works: eligibility, documents, components, rules, history

Return ONLY JSON: {{"intent": "DATA"|"KNOWLEDGE"}}

Question: "{question}"
JSON:"""
    raw = await llm.call_classifier(prompt, guided={"guided_json": _INTENT_JSON_SCHEMA})
    payload = _extract_json(raw)
    if payload and payload.get("intent") in ("DATA", "KNOWLEDGE"):
        return payload["intent"]
    logger.warning("classify_intent: unusable response %r — defaulting to DATA", raw[:120])
    return "DATA"


def _empty_data_fields() -> dict:
    # `data` / `sql_query` are the keys the frontend renderer reads; keep them
    # present (empty) on every route so the UI never sees `undefined`.
    return {"schemes": [], "resolved_entities": {}, "sql": None, "sql_query": None,
            "row_count": 0, "rows": [], "data": []}


def _denied(decision: "auth.AuthDecision", schemes: list[str], resolved: dict) -> dict:
    return {
        "route": "denied", "intent": "DATA", "confidence": "high",
        "answer": decision.reason, "denied": True, "denied_by": decision.check,
        **{**_empty_data_fields(), "schemes": schemes, "resolved_entities": resolved},
    }


def _out_of_scope_result(question: str, raw_question: str) -> dict:
    """Standard 'I'm Megh One AI …' reply for an OutOfScope raised mid-pipeline —
    shaped like the edge returns in _run_pipeline so the frontend renders it the
    same way as an off-topic hit caught up front."""
    hit = edge.out_of_scope()
    return {
        "route": "edge", "intent": "EDGE", "confidence": "high",
        "answer": hit["response"], "edge_type": hit["type"],
        "suggestions": hit.get("suggestions", []),
        "rewritten_question": question if question != raw_question else None,
        **_empty_data_fields(),
    }


# ── Village NAME search: "how many villages are named X?" ───────────────────
# An entity SEARCH, not an entity lookup. Before this, "How many villages are
# named Songsak?" went through resolve_entities, which narrows the name to ONE
# place (the block SONGSAK, or one village) and counted that one place's
# records. The operation must be settled BEFORE any entity is resolved: a count
# or a list of every village with that name (or containing it), by LGD
# village_code — 345 village names are shared (docs/DATA_MODEL.md rule 3).
_VNS_NAME = r"[\"'“]?(?P<name>[A-Za-z][A-Za-z0-9.&'()\- ]{1,48}?)[\"'”]?"
_VNS_TAIL = r"(?=\s+(?:in|under|within|for|across|of)\b|\s*[?.!]*\s*$)"
_VILLAGE_NAME_SEARCH = [
    # exact-name count / list
    ("exact", re.compile(
        r"\b(?:how\s+many|number\s+of|count\s+(?:of\s+)?(?:the\s+)?|list\s+(?:all\s+)?(?:the\s+)?|"
        r"which|show\s+(?:me\s+)?(?:all\s+)?(?:the\s+)?)\s*villages?\s+(?:are\s+|were\s+|is\s+)?"
        r"(?:there\s+)?(?:named|called|with\s+(?:the\s+)?name(?:\s+of)?|by\s+the\s+name(?:\s+of)?|"
        r"having\s+(?:the\s+)?name|have\s+the\s+name)\s+" + _VNS_NAME + _VNS_TAIL, re.IGNORECASE)),
    # partial-name list / count
    ("contains", re.compile(
        r"\bvillages?\s+(?:that\s+|which\s+)?(?:contain|contains|include|includes|have|has|having)\s+"
        + _VNS_NAME + r"\s+in\s+(?:the|their|its)\s+names?\b", re.IGNORECASE)),
    ("contains", re.compile(
        r"\bvillages?\s+(?:whose|with)\s+(?:the\s+)?names?\s+(?:contains?|includes?|has|have|with)\s+"
        + _VNS_NAME + _VNS_TAIL, re.IGNORECASE)),
    ("contains", re.compile(
        r"\bvillages?\s+with\s+" + _VNS_NAME + r"\s+in\s+(?:the|their|its)\s+names?\b", re.IGNORECASE)),
]
_VILLAGE_CATALOGUES = {"MGNREGA": "_mgnrega_village_names", "Focus Plus": "_focusplus_village_names",
                       "PMAY-G": "_pmay_village_names", "CM Elevate": "_cm_elevate_village_names"}


def _village_name_search(question: str) -> "tuple[str, str] | None":
    """(mode, name) for a village-name search question, else None. mode is
    "exact" (named / called X) or "contains" (X in the name)."""
    for mode, rx in _VILLAGE_NAME_SEARCH:
        m = rx.search(question or "")
        if m:
            name = re.sub(r"\s+", " ", m.group("name")).strip(" .?!'\"")
            name = re.sub(r"\s+(?:village|villages)$", "", name, flags=re.IGNORECASE).strip()
            if len(name) >= 2:
                return mode, name
    return None


async def _village_name_search_answer(question: str) -> "dict | None":
    """The deterministic answer to a village-name search, or None when the
    question is not one. Scoped to one scheme's villages when the question
    names exactly one scheme with a village catalogue; otherwise the statewide
    LGD list, curated.dim_geography. App SQL, parameter-bound."""
    found = _village_name_search(question)
    if not found:
        return None
    mode, name = found
    named = [s for s in _named_schemes(question) if s in _VILLAGE_CATALOGUES]
    scheme = named[0] if len(named) == 1 else None
    key = re.sub(r"\s+", " ", name).upper()
    if scheme:
        catalogue = await globals()[_VILLAGE_CATALOGUES[scheme]]()
        hits = [v for n, vs in catalogue.items()
                if (n == key if mode == "exact" else key in n) for v in vs]
        where = f"{scheme} data"
        shown = (f"-- {scheme} village catalogue (distinct village_code by name)\n"
                 f"WHERE UPPER(lgd_village_name) {'=' if mode == 'exact' else 'LIKE'} "
                 f"'{key if mode == 'exact' else '%' + key + '%'}'")
    else:
        cond = ("UPPER(REGEXP_REPLACE(TRIM(lgd_village_name), '\\s+', ' ', 'g')) = $1" if mode == "exact"
                else "UPPER(lgd_village_name) LIKE $1 ESCAPE '\\'")
        param = key if mode == "exact" else "%" + re.sub(r"([%_\\])", r"\\\1", key) + "%"
        sql = ("SELECT DISTINCT village_code, lgd_village_name AS name, UPPER(lgd_block) AS block, "
               "UPPER(lgd_district) AS district FROM curated.dim_geography "
               f"WHERE village_code IS NOT NULL AND entity_type <> 'Unresolved' AND {cond} "
               "ORDER BY 4, 3, 2")
        hits = [dict(r) for r in await fetch_rows(sql, [param])]
        where = "the LGD village list (all schemes)"
        shown = sql.replace("$1", "'" + param.replace("'", "''") + "'")
    by_code: dict = {}
    for h in hits:
        by_code.setdefault(h["village_code"], h)
    rows = sorted(by_code.values(), key=lambda h: (str(h.get("district") or ""), str(h.get("block") or ""),
                                                   str(h.get("name") or "")))
    # "…named Songsak in East Garo Hills": a district / block named besides the
    # searched name narrows the search (the name itself is not a scope).
    scope = {p: d for p, d in named_places(question).items() if p != key and d in ("district", "block")}
    if scope:
        rows = [r for r in rows if any(str(r.get(d) or "").upper() == p for p, d in scope.items())]
        where += " in " + " / ".join(p.title() + (" block" if d == "block" else "") for p, d in scope.items())
    n = len(rows)
    what = f"named \"{name}\"" if mode == "exact" else f"with \"{name}\" in their name"
    if n == 0:
        answer = f"No village {what.replace('their', 'its')} is in {where}."
        if mode == "exact":
            answer += f" For partial matches, ask \"which villages contain {name} in their name?\""
    else:
        items = [f"{r['name']} ({str(r.get('block') or '?').title()} block, "
                 f"{str(r.get('district') or '?').title()}; LGD {r['village_code']})" for r in rows[:25]]
        answer = (f"{n:,} village{'s' if n != 1 else ''} {what} {'are' if n != 1 else 'is'} in {where}: "
                  + "; ".join(items) + (f"; and {n - 25:,} more." if n > 25 else "."))
        if n > 1 and mode == "exact":
            answer += " They are different villages that share a name, told apart by their LGD code."
    also = {d for p, d in named_places(name).items() if p == key}
    if also:
        answer += f" \"{name.title()}\" is also the name of a {' and a '.join(sorted(also))}."
    context_budget.log_decision("entity_search", mode=mode, scheme=scheme, matches=n)
    rows_out = [{"village_code": r["village_code"], "village": r["name"], "block": r.get("block"),
                 "district": r.get("district")} for r in rows]
    return {"route": "data", "intent": "DATA", "confidence": "high",
            "schemes": [scheme] if scheme else [],
            "resolved_entities": {"village_name_search": name, "match": mode},
            "sql": shown, "sql_query": shown, "row_count": n, "rows": rows_out[:20],
            "data": rows_out, "answer": answer}


# ── Focus Plus: an amount stated as the size of each payment ────────────────
# Reported 2026-09-29: "What is the total disbursement amount for the Focus Plus
# scheme for a loan of five thousand across all financial years for all of
# Meghalaya" ran SELECT SUM(amount_disbursed) FROM curated.v_focus_plus — the
# stated amount silently dropped. Focus Plus pays exactly two amounts
# (data/focus_plus/README.md §6.3; schema_context Focus Plus rules): ₹5,000 is
# Tranch 1 (93K batch, FY 2022-23) and ₹2,500 is every later tranche (FY
# 2025-26). So the amount is a real filter (₹46.64 crore vs ₹119.74 crore for
# all payments), and Focus Plus is a cash benefit, not a loan. A held amount
# must reach the SQL (_focusplus_stated_amount_missing, a repair guard); one it
# never pays is asked about, never dropped.
_FOCUSPLUS_PAYMENT_AMOUNTS = {
    5000: "the Tranch 1 payments (93K batch, FY 2022-23)",
    2500: "the Tranch 2, 3 and 4 payments (FY 2025-26)",
}


def _focusplus_stated_amount(question: str, schemes: list[str]) -> "premise_check.StatedAmount | None":
    if (schemes or []) != ["Focus Plus"]:
        return None
    found = premise_check.stated_amount_filters(question)
    return found[0] if found else None


def _focusplus_amount_clarification(question: str, amt: "premise_check.StatedAmount") -> ClarificationNeeded:
    """Focus Plus never paid the stated amount: say what it does pay, offer
    each held amount and "all payments" as full questions."""
    _pat = re.compile(r"\s*\b(?:for|of|with|on|at)?\s*(?:a|an|the)?\s*" + re.escape(amt.text), re.IGNORECASE)
    options = [{"label": f"₹{v:,} payments ({'Tranch 1' if v == 5000 else 'Tranches 2–4'})",
                "question": _pat.sub(f" for payments of ₹{v:,}", question, count=1)}
               for v in sorted(_FOCUSPLUS_PAYMENT_AMOUNTS, reverse=True)]
    options.append({"label": "All Focus Plus payments",
                    "question": re.sub(r"\s{2,}", " ", _pat.sub("", question, count=1)).strip()})
    loan = " Focus Plus is a cash benefit to farmers, not a loan." if amt.noun == "loan" else ""
    context_budget.log_decision("clarify", rule="amount-not-held", scheme="Focus Plus",
                                amount=amt.value)
    return ClarificationNeeded(
        f"Focus Plus has no {amt.noun}s of ₹{amt.value:,.0f}.{loan} Every Focus Plus payment is "
        "either ₹5,000 (Tranch 1, FY 2022-23) or ₹2,500 (Tranches 2–4, FY 2025-26). Which "
        "did you mean?", options=options, rule="amount-not-held")


def _focusplus_amount_note(amt: "premise_check.StatedAmount") -> str:
    loan = ("Focus Plus is a cash benefit, not a loan — say so in one short clause. "
            if amt.noun == "loan" else "")
    return (f"{loan}The question asks only about payments of ₹{amt.value:,.0f}; in this data those "
            f"are {_FOCUSPLUS_PAYMENT_AMOUNTS[int(amt.value)]}. Say that the figure covers the "
            f"₹{amt.value:,.0f} payments only.")


def _focusplus_stated_amount_missing(question: str, schemes: list[str], sql: str) -> "str | None":
    """The repair instruction when a Focus Plus question states a held payment
    amount and the SQL does not filter amount_disbursed on it."""
    amt = _focusplus_stated_amount(question, schemes)
    if amt is None or int(amt.value) not in _FOCUSPLUS_PAYMENT_AMOUNTS or not sql:
        return None
    n = int(amt.value)
    if re.search(rf"\bamount_disbursed\s*(?:=\s*'?{n}(?:\.0+)?'?\b|IN\s*\([^)]*\b{n}(?:\.0+)?\b)",
                 sql, re.IGNORECASE):
        return None
    return (f"the question restricts the answer to payments of ₹{n:,} (\"{amt.text}\"), but this "
            "query does not filter on the payment amount, so it counts every Focus Plus payment. "
            f"Add amount_disbursed = {n} to the WHERE clause (Focus Plus pays only 5000 or 2500 "
            "per row) and keep every other filter, the aggregation and the grouping exactly as "
            "they were.")


def _acronym_near_miss_clarification(question: str, near: "dict[str, list[str]]") -> ClarificationNeeded:
    """"Did you mean West Khasi Hills (WKH)?" for the first unrecognised
    acronym, with one chip per catalogued candidate. Each chip is the question
    with the token replaced by the district's name, so it resumes as a full
    question; a typed reply that names the district satisfies the check
    (acronym_near_misses skips a candidate the text already names)."""
    token, cands = next(iter(near.items()))
    titled = [c.title() for c in cands]
    options = [{"label": t, "question": re.sub(rf"\b{re.escape(token)}\b", t, question, count=1)}
               for t in titled]
    ask = (f"I don't recognise \"{token}\" as a district. Did you mean "
           + " or ".join(titled) + "?")
    context_budget.log_decision("clarify", rule="entity-ambiguous", reason="acronym near-miss",
                                candidates=cands)
    return ClarificationNeeded(ask, options=options, rule="entity-ambiguous")


# ── DATA path stages (D-032) ────────────────────────────────────────────────
# _answer_data was one function. Its steps are now the stages below, moved
# verbatim, so the LangGraph orchestrator (app/pipeline_graph.py) runs exactly
# the code this sequential path runs. A stage reads its inputs from the
# per-turn dict `d` into the same local names the code always used, writes its
# outputs back, and returns a finished result dict to end the turn early or
# None to go on. A new DATA step goes into a stage, never into a graph node, so
# the two orchestrators cannot drift.

def _new_data_turn(question: str, *, skip_scope_clarify: bool = False,
                   prior_resolved: "dict | None" = None,
                   village_hint: "str | None" = None) -> dict:
    return {"question": question, "skip_scope_clarify": skip_scope_clarify,
            "prior_resolved": prior_resolved, "village_hint": village_hint,
            "schemes": None, "fp_amount": None, "entity_result": None,
            "mg_admin": None, "mg_women": None, "mg_det": None,
            "sql": None, "rows": None, "notes": None, "style": "",
            "fl_total": None, "fl_unplaced": None, "fl_breakdown": None, "answer": None}


async def _answer_data(question: str, scope: "auth.UserScope | None" = None,
                       skip_scope_clarify: bool = False,
                       prior_resolved: "dict | None" = None,
                       village_hint: "str | None" = None) -> dict:
    d = _new_data_turn(question, skip_scope_clarify=skip_scope_clarify,
                       prior_resolved=prior_resolved, village_hint=village_hint)
    for stage in (_data_scheme_stage, _data_entity_stage, _data_clarification_stage,
                  _data_deterministic_stage):
        result = await stage(d, scope)
        if result is not None:
            return result
    await _data_sql_stage(d, scope)
    denied = await _data_authorize_stage(d, scope)
    if denied is not None:
        return denied
    if d["mg_det"]:
        await _data_deterministic_rows(d, scope)
    else:
        try:
            d["sql"], d["rows"] = await execute_with_repair(d["question"], d["schemes"], d["entity_result"],
                                                            initial_sql=d["sql"], scope=scope)
        except RepairedSQLDenied as e:   # KI-183
            return _denied(e.decision, d["schemes"], d["entity_result"]["resolved"])
    for stage in (_data_post_rows_stage, _data_compose_stage, _data_guarantees_stage):
        result = await stage(d, scope)
        if result is not None:
            return result
    return _data_assemble(d)


async def _data_scheme_stage(d: dict, scope: "auth.UserScope | None" = None) -> "dict | None":
    """Scheme gates, classify_scheme and the scheme re-routes (graph: scope_scheme)."""
    question = d["question"]
    # Named a scheme we don't hold ("CM Elevate", "PM-KISAN", …) — say so plainly
    # instead of the generic "which scheme?" prompt, then offer the three we have.
    _unsupported = _unsupported_scheme_named(question)
    if _unsupported:
        raise _unsupported_scheme_clarification(question, _unsupported)

    # Ask which scheme before doing anything expensive, when the question could
    # honestly mean either one. Guessing here is worse than a one-tap follow-up.
    # "Which scheme paid out the most?" — the scheme is the ANSWER, not a
    # missing filter, so this must run before the "which scheme?" pause below
    # (which would otherwise ask the user to supply the very thing they asked
    # for). Answered deterministically across every scheme that records money.
    if _wants_cross_scheme_money_ranking(question):
        return await _cross_scheme_money_answer(question)

    # "Focus" with nothing to say WHICH Focus — a two-way ask that keeps what the
    # user already told us, rather than the generic five-way pause below.
    if _is_ambiguous_focus(question):
        raise _focus_ambiguity_clarification(question)

    # A village NAME that contains another scheme's vocabulary is not a scheme
    # signal: "total expenditure in UMRAN DAIRY" was classified CM Elevate (its
    # dairy sub-scheme) and answered ₹1.27 crore of CM Elevate money for an
    # MGNREGA village (all-villages QA 2026-09-26). Scheme detection reads the
    # question with that exact village name masked; everything else reads it
    # unchanged. Only when the masked name itself carries scheme vocabulary.
    _scheme_q = await _mask_scheme_words_in_village_name(question)

    if _needs_scheme_clarification(_scheme_q):
        raise _scheme_clarification(question)

    # Ask "top how many?" before spending model calls when the question wants a
    # ranked list over a dimension but never says how long.
    if _needs_topn_clarification(question):
        raise _topn_clarification(question)

    schemes = await classify_scheme(_scheme_q)
    # Only Focus Legacy holds producer groups and their members. A group name can
    # contain another scheme's word — "How many members are there in Chisam
    # Piggery?" was classified CM Elevate (its Piggery sub-scheme) and answered
    # "3010 members" of the Piggery scheme. A group-name question that names no
    # scheme of its own is a Focus Legacy question.
    if schemes != ["Focus Legacy"] and _pg_name_question(question) and not _mentions_scheme(question):
        logger.info("group-name question %r -> Focus Legacy (was %s)", question, schemes)
        schemes = ["Focus Legacy"]
    # Focus Legacy group-name questions are answered deterministically (see
    # _focus_legacy_pg_name_answer) before any geography resolution, which would
    # otherwise read a group name like "Nongstoin PG" as a place.
    if schemes == ["Focus Legacy"]:
        _pg_answer = await _focus_legacy_pg_name_answer(question)
        if _pg_answer is not None:
            return _pg_answer
    # CM Elevate Legacy: a question its data cannot answer (a sanction rate,
    # applicant names, constituency, monthly figures, ...) gets the reviewed
    # not-held explanation now, before any model call.
    if schemes == ["CM Elevate Legacy"]:
        _not_held = _cm_legacy_not_held(question)
        if _not_held is not None:
            raise _not_held
    d["schemes"] = schemes
    return None


async def _data_entity_stage(d: dict, scope: "auth.UserScope | None" = None) -> "dict | None":
    """Pre-resolution gates and resolve_entities (graph: entities)."""
    question, schemes = d["question"], d["schemes"]
    prior_resolved, village_hint = d["prior_resolved"], d["village_hint"]
    # A district acronym typed with its letters in another order — "Compare …
    # in WHK and EKH" (reported 2026-09-29). WHK is in no SME catalogue, so it
    # was not_found and silently left out: the comparison ran for EKH alone.
    # Never guessed: ask, offering only the district(s) whose catalogued
    # acronym has those letters (entity_resolver.acronym_near_misses).
    _near = acronym_near_misses(question)
    if _near:
        raise _acronym_near_miss_clarification(question, _near)
    # Focus Plus: a stated payment size it never pays is asked about, not dropped.
    _fp_amount = _focusplus_stated_amount(question, schemes)
    if _fp_amount is not None and int(_fp_amount.value) not in _FOCUSPLUS_PAYMENT_AMOUNTS:
        raise _focusplus_amount_clarification(question, _fp_amount)
    # raises ClarificationNeeded if ambiguous
    entity_result = await resolve_entities(question, schemes, prior_resolved=prior_resolved,
                                            village_hint=village_hint)
    if _fp_amount is not None:
        entity_result.setdefault("notes", []).append(_focusplus_amount_note(_fp_amount))
    # A year-gap rewrite (see resolve_entities) replaces the question for
    # everything downstream — SQL generation above all.
    if entity_result.get("question"):
        question = entity_result["question"]
    d.update(question=question, fp_amount=_fp_amount, entity_result=entity_result)
    return None


async def _data_clarification_stage(d: dict, scope: "auth.UserScope | None" = None) -> "dict | None":
    """Region, overall-summary and scope / year / tranche gates (graph: clarification_gate)."""
    question, schemes, entity_result = d["question"], d["schemes"], d["entity_result"]
    prior_resolved, skip_scope_clarify = d["prior_resolved"], d["skip_scope_clarify"]
    # "Garo Hills" / "Khasi Hills" name a hill RANGE, not a district. When one is
    # named and no specific district resolved, either offer its districts as one-tap
    # chips ("which Garo Hills district?") or, if the question already says "all of
    # Garo Hills" or asks for a per-district breakdown, expand it to every district in
    # the range and carry that forward for SQL generation.
    # MGNREGA: a resolved village / block / constituency already settles the
    # place. "THORIKAKONA GARO" and "DANGKONG GARO" are villages whose names
    # contain "Garo", and the range check asked "which Garo Hills district?" and
    # lost the village (all-villages QA 2026-09-26).
    # PMAY-G too: "shyamding ( garo ), Demdema block" asked "which Garo Hills
    # district?" and answered for the block (PMAY-G scenario run 2026-09-28).
    # CM Elevate too: "UPPER KAMARI (GARO), Tikrikilla block" was answered for all of
    # Garo Hills — 273 applications instead of 2 (CM Elevate all-villages smoke run
    # 2026-09-28).
    # Focus Legacy too: "MENDIMA GARO", "SHYAMDING ( GARO )" — the region chip then
    # rewrote the name to "MENDIMA all of Garo Hills" (all-villages run 2026-09-29, KI-162).
    # CM Elevate Legacy too: the same "MENDIMA GARO" was answered as MENDIMA
    # BAKRAGITTIM with 0 records (CM Elevate Legacy all-villages run 2026-09-29).
    # A two-village comparison too (village_code_list): "Which has more completed
    # PMAY-G houses: MALANG KHASI or SIDAKANDI?" asked "which Khasi Hills
    # district?" (PMAY-G officer cases run 2026-10-02).
    _mg_place_settled = schemes in (["MGNREGA"], ["PMAY-G"], ["CM Elevate"], ["Focus Legacy"],
                                    ["CM Elevate Legacy"]) and any(
        entity_result["resolved"].get(k) for k in ("village_code", "village_code_list", "block",
                                                   "assembly_constituency"))
    if not entity_result["resolved"].get("district") and not entity_result["resolved"].get("district_list") \
            and not _mg_place_settled:
        _scheme0 = schemes[0] if schemes else ""
        region = detect_region(question, _scheme0)
        if region:
            _want_all = region["expand"] or bool(_HAS_BREAKDOWN.search(question))
            if not _want_all and not skip_scope_clarify:
                raise _region_clarification(question, region)
            entity_result["resolved"]["district_list"] = [d.upper() for d in region["districts"]]
            entity_result["resolved"]["district_list_region"] = region["canonical"]
            entity_result["display"]["district"] = region["canonical"]

    # "Give me an overall Focus+ data summary" — a genuinely whole-scheme
    # question with no district/year/tranche to pin. Handled before the scope/
    # year/tranche clarification gates below so it never gets mistaken for an
    # aggregate question that merely forgot to name a scope.
    if _focusplus_wants_overall_summary(question, schemes):
        return await _focusplus_overall_summary_answer(schemes, entity_result)

    # Ask which district / block / village / year when an aggregate question
    # pins none of them — unless we're already resuming that very clarification
    # (a reply that still names no scope must not loop us back here).
    if not skip_scope_clarify and _needs_scope_clarification(question, entity_result["resolved"]):
        raise _scope_clarification(question, schemes)

    # Geography is settled but the year isn't: ask which financial year (one-tap
    # chips) rather than silently answering across every year. The chips resume
    # with a concrete year or "all financial years", so this can't loop.
    if _needs_year_clarification(question, schemes, entity_result["resolved"]):
        # MGNREGA women questions offer only the years that carry women data.
        if schemes == ["MGNREGA"] and _WOMEN_CUE.search(question or ""):
            try:
                raise await _mgnrega_women_year_clarification(question)
            except ClarificationNeeded:
                raise
            except Exception:  # noqa: BLE001 — year map unavailable: the generic pause
                pass
        raise _year_clarification(question, schemes)

    # Focus Plus only: year is settled but the tranche isn't — ask which one
    # (one-tap chips) rather than silently combining every tranche's payments.
    # `already_all_combined` lets a session that already answered this once
    # ("all tranches combined") skip a redundant re-ask on a later bare
    # follow-up that doesn't restate "tranche" itself.
    if _needs_tranche_clarification(
            question, schemes, entity_result["resolved"],
            already_all_combined=bool((prior_resolved or {}).get("tranche_all_combined"))):
        raise _tranche_clarification(question)

    # A specific non-Tranch-4 tranche plus a person-level column (status/
    # gender/occupation/verification) can never match any row — that data
    # exists only on the 12.5K cohort, which is entirely Tranch 4. Explain why
    # instead of running SQL that is guaranteed to come back empty.
    if _person_level_tranche_conflict(question, schemes, entity_result["resolved"]):
        return _person_level_tranche_conflict_answer(question, schemes, entity_result)
    d["entity_result"] = entity_result
    return None


async def _data_deterministic_stage(d: dict, scope: "auth.UserScope | None" = None) -> "dict | None":
    """The fixed-shape MGNREGA and PMAY-G queries built in code (graph: deterministic)."""
    question, schemes, entity_result = d["question"], d["schemes"], d["entity_result"]
    # MGNREGA shapes that are fixed by the question and that the model kept
    # getting wrong — built from the resolved filters and run parameter-bound
    # (see _mgnrega_combined_facts_query / _mgnrega_admin_expenditure_query).
    _mg_admin = _mgnrega_admin_expenditure_query(question, schemes, entity_result["resolved"])
    _mg_women = None
    if schemes == ["MGNREGA"]:
        try:
            await _mgnrega_year_women()          # warms the cache _mgnrega_women_years_only reads
        except Exception:  # noqa: BLE001
            pass
        if not _mg_admin:
            _mg_women = await _mgnrega_women_query(question, schemes, entity_result["resolved"])
    if _mg_women and _mg_women["kind"] == "unrecorded":
        answer = await _mgnrega_women_unrecorded_answer(_mg_women, entity_result.get("display") or {})
        return {
            "route": "data", "intent": "DATA", "confidence": "high", "schemes": schemes,
            "resolved_entities": entity_result["resolved"], "sql": None, "sql_query": None,
            "row_count": 0, "rows": [], "data": [], "answer": answer,
        }
    # PMAY-G fixed shapes (counts, money, stages, release status, rates, summaries,
    # A-vs-B comparisons): query and wording built here, parameter-bound — the
    # 30B's per-year GROUP BY, AVG-based rates, LIMIT 1 comparisons and dropped
    # figures failed 11 of 28 use cases (PMAY-G QA 2026-09-28, KI-089..KI-095).
    _pm_unknown = _pmay_unknown_place_answer(schemes, entity_result["resolved"], entity_result.get("notes"))
    if _pm_unknown:
        return {
            "route": "data", "intent": "DATA", "confidence": "high", "schemes": schemes,
            "resolved_entities": entity_result["resolved"], "sql": None, "sql_query": None,
            "row_count": 0, "rows": [], "data": [], "answer": _pm_unknown,
        }
    _pm_det = _pmay_facts_query(question, schemes, entity_result["resolved"], entity_result.get("notes"))
    if _pm_det:
        if scope is not None and settings.AUTH_ENABLED:
            decision = auth.authorize(scope, schemes=schemes,
                                      resolved_entities=entity_result["resolved"], sql=_pm_det["shown"])
            if not decision.allow:
                logger.info("auth deny (%s) user=%s: %s", decision.check, scope.user_id, decision.reason)
                return _denied(decision, schemes, entity_result["resolved"])
        _pm_det["display"] = entity_result.get("display") or {}
        _pm_rows = [dict(r) for r in await fetch_rows(_pm_det["sql"], _pm_det["params"])]
        logger.info("PMAY-G deterministic query (%s): %s", ",".join(_pm_det["metrics"]),
                    _pm_det["shown"].replace("\n", " ")[:400])
        _pm_answer = _pmay_facts_answer(_pm_det, _pm_rows)
        if _pm_answer and _pm_det.get("bare_year") and _pm_det.get("year_key") == _pm_det["bare_year"]:
            # A plain year ("during 2017") is the CALENDAR year: the answer, the result table
            # and the SQL shown are the calendar-year figures, with the FY reading as a one-line
            # note. The FY figures used to lead while the calendar ones sat in the note, so the
            # table disagreed with what the officer asked (user report 2026-09-29; the SME file
            # pmay_entity_resolver.yaml reads "sanctioned in 2023" as calendar 2023).
            _cy = _pm_det["bare_year"]
            _cal = _pmay_facts_query(question, schemes, {k: v for k, v in entity_result["resolved"].items()
                                                         if k != "year_key"},
                                     entity_result.get("notes"), calendar_year=_cy)
            if _cal:
                _cal["display"] = _pm_det.get("display") or {}
                _cal_rows = [dict(r) for r in await fetch_rows(_cal["sql"], _cal["params"])]
                _cal_answer = _pmay_facts_answer(_cal, _cal_rows)
                if _cal_answer:
                    _fy_lines = [ln for r in _pm_rows if int(r.get("houses") or 0) for ln in _pmay_lines(_pm_det, r)]
                    if _fy_lines and len(_pm_rows) == 1:
                        _cal_answer += (f"\n\n(\"{_cy}\" was read as calendar year {_cy}, January–December. For FY "
                                        f"{_cy}-{(_cy + 1) % 100:02d}, April {_cy} – March {_cy + 1}: " + "; ".join(
                                            ln if ln[:2].isupper() else ln[0].lower() + ln[1:]   # keep "PMAY-G"
                                            for ln in _fy_lines) + ".)")
                    _pm_det, _pm_rows, _pm_answer = _cal, _cal_rows, _cal_answer
        if _pm_answer:
            _pm_rows = _pmay_display_rows(_pm_det, _pm_rows)
            return {
                "route": "data", "intent": "DATA", "confidence": "high", "schemes": schemes,
                "resolved_entities": entity_result["resolved"], "sql": _pm_det["shown"],
                "sql_query": _pm_det["shown"], "row_count": len(_pm_rows), "rows": _pm_rows[:20],
                "data": _pm_rows, "answer": _pm_answer,
            }
        if (_pm_det["dim"] == "village" and len(_pm_det["entities"]) == 1 and _pm_det.get("date") is None
                and _pm_det.get("year_key") is None and not _pm_det.get("stages")):
            _ph = await fetch_rows(
                "SELECT COUNT(*) AS placeholders, MAX(lgd_village_name) AS name, MAX(lgd_block) AS block, "
                "MAX(lgd_district) AS district FROM curated.v_pmay WHERE village_code = $1",
                [int(_pm_det["entities"][0])])
            _ph_row = dict(_ph[0]) if _ph and _ph[0]["placeholders"] else None
            return {
                "route": "data", "intent": "DATA", "confidence": "high", "schemes": schemes,
                "resolved_entities": entity_result["resolved"], "sql": _pm_det["shown"],
                "sql_query": _pm_det["shown"], "row_count": 0, "rows": [], "data": [],
                "answer": _pmay_no_houses_answer(_pm_det, _ph_row, entity_result.get("display") or {}),
            }
        logger.info("PMAY-G deterministic query found no houses for the place — model path")
    _mg_det = _mg_admin or (
        (_mg_women["sql"], _mg_women["params"], _mg_women["shown"]) if _mg_women else None
    ) or _mgnrega_combined_facts_query(question, schemes, entity_result["resolved"])
    d.update(mg_admin=_mg_admin, mg_women=_mg_women, mg_det=_mg_det)
    return None


async def _data_sql_stage(d: dict, scope: "auth.UserScope | None" = None) -> None:
    """The SQL to run: the deterministic query's, or generate_sql (graph: sql_generate)."""
    question, schemes, entity_result, _mg_det = d["question"], d["schemes"], d["entity_result"], d["mg_det"]
    sql = _mg_det[2] if _mg_det else await generate_sql(question, schemes, entity_result)
    d["sql"] = sql


async def _data_authorize_stage(d: dict, scope: "auth.UserScope | None" = None) -> "dict | None":
    """auth.authorize on the SQL; the denial result, or None (graph: authorize)."""
    schemes, entity_result, sql = d["schemes"], d["entity_result"], d["sql"]
    # Authorization — role/scope vs. what the query actually asks for. Runs on the
    # generated SQL so granularity (GROUP BY) and geography literals are visible.
    if scope is not None and settings.AUTH_ENABLED:
        decision = auth.authorize(scope, schemes=schemes,
                                  resolved_entities=entity_result["resolved"], sql=sql)
        if not decision.allow:
            logger.info("auth deny (%s) user=%s: %s", decision.check, scope.user_id, decision.reason)
            return _denied(decision, schemes, entity_result["resolved"])
    return None


async def _data_deterministic_rows(d: dict, scope: "auth.UserScope | None" = None) -> None:
    """Run the deterministic MGNREGA query (parameter-bound, no repair loop)."""
    _mg_det = d["mg_det"]
    rows = [dict(r) for r in await fetch_rows(_mg_det[0], _mg_det[1])]
    logger.info("MGNREGA deterministic query: %s | params=%s", _mg_det[2].replace("\n", " "),
                _mg_det[1])
    d["rows"] = rows


async def _data_post_rows_stage(d: dict, scope: "auth.UserScope | None" = None) -> "dict | None":
    """Deterministic post-row answers, composer notes and the premise check (graph: post_rows)."""
    question, schemes, entity_result = d["question"], d["schemes"], d["entity_result"]
    sql, rows, _mg_admin, _mg_women = d["sql"], d["rows"], d["mg_admin"], d["mg_women"]
    if _mg_admin:
        answer = await _mgnrega_admin_expenditure_answer(rows, entity_result.get("display") or {})
        return {
            "route": "data", "intent": "DATA", "confidence": "high", "schemes": schemes,
            "resolved_entities": entity_result["resolved"], "sql": sql, "sql_query": sql,
            "row_count": len(rows), "rows": rows[:20], "data": rows, "answer": answer,
        }
    if schemes == ["MGNREGA"]:
        _empty = await _mgnrega_empty_answer(sql, rows, entity_result.get("display") or {},
                                             entity_result["resolved"])
        if _empty:
            return {
                "route": "data", "intent": "DATA", "confidence": "high", "schemes": schemes,
                "resolved_entities": entity_result["resolved"], "sql": sql, "sql_query": sql,
                "row_count": len(rows), "rows": rows[:20], "data": rows, "answer": _empty,
            }

    # A number the question states as already-true ("the 1.71 L sanctioned
    # houses") is never part of the SQL — check it against the result so the
    # composer corrects a false premise instead of repeating it as fact.
    notes = list(entity_result.get("notes") or [])
    notes.extend(_genuine_zero_notes(sql))
    notes.extend(_sector_not_tracked_notes(rows))
    _style = ""
    if schemes == ["MGNREGA"]:
        notes.extend(_mgnrega_answer_notes(question, sql, rows))
        _women_sql = "women_employment_provided" in (sql or "").lower()
        if not _women_sql:
            # A women figure over an unrecorded year is NOT a genuine zero.
            notes.extend(_mgnrega_null_zero_notes(rows))
        if _mg_women:
            span = _women_span(_mg_women["years"])
            notes.append(
                f"This figure covers {span}" + (
                    " — every year that has women-employment data; no financial year was named"
                    if _mg_women["all_years"] else "") + ". Say which year(s) it covers." + (
                    f" Women employment is not recorded at source for "
                    f"{', '.join('FY ' + y['fy'] for y in _mg_women['missing'])}; say so, never call it zero."
                    if _mg_women["all_years"] and _mg_women["missing"] else ""))
        elif _women_sql and _MGNREGA_YEAR_WOMEN:
            _miss = {y["year_key"]: y["fy"] for y in _MGNREGA_YEAR_WOMEN if y["women"] <= 0}
            _yrs = _mgnrega_women_sql_years(sql)
            if _miss and _yrs and _yrs <= set(_miss):
                # Every year the query reads has no women data (e.g. a ranking for FY 2025-26).
                answer = (f"Women employment is not recorded in the MGNREGA data for "
                          f"{', '.join('FY ' + _miss[k] for k in sorted(_yrs))} — the women column is 0 "
                          "on every record statewide (not populated at source), so no women figure, "
                          "share or ranking can be given for that year. Ask for FY "
                          f"{[y['fy'] for y in _MGNREGA_YEAR_WOMEN if y['women'] > 0][-1]} instead.")
                return {
                    "route": "data", "intent": "DATA", "confidence": "high", "schemes": schemes,
                    "resolved_entities": entity_result["resolved"], "sql": sql, "sql_query": sql,
                    "row_count": len(rows), "rows": rows[:20], "data": rows, "answer": answer,
                }
            if _miss:
                notes.append(
                    f"Women employment is not recorded at source for "
                    f"{', '.join('FY ' + v for v in _miss.values())} (0 on every record). Never describe "
                    "a women figure for that year as zero or as a genuine zero.")
    if schemes == ["CM Elevate Legacy"]:
        notes.extend(_cm_legacy_answer_notes(sql, rows))
        notes.extend(_cm_legacy_small_money_notes(rows))
        notes.extend(await _cm_legacy_exact_totals(sql, rows))
        _style = _cm_legacy_style_block(question)
    _fl_total = _fl_unplaced = None
    if schemes == ["Focus Legacy"]:
        notes.extend(_focus_legacy_answer_notes(question))
        _fl_unplaced = await _focus_legacy_unplaced_row(sql, rows)
        if _fl_unplaced:
            notes.append(_focus_legacy_unplaced_note(*_fl_unplaced))
        _fl_total = await _focus_legacy_list_total(sql, rows)
        if _fl_total:
            notes.append(f"{_fl_total[0]:,} {_fl_total[1]} match in total; the result lists only the "
                         f"first {len(rows)}. Say that {_fl_total[0]:,} {_fl_total[1]} match, then name "
                         "the top ones — never present the list as complete.")
    if settings.PREMISE_CHECK_ENABLED:
        try:
            # an "(LGD 904712)" chip tag is an identifier, not an assumed figure
            notes.extend(premise_check.check_premises(_CHIP_LGD_CODE_RE.sub("", question), rows))
        except Exception:  # noqa: BLE001
            logger.warning("premise check failed — continuing without it", exc_info=True)

    if schemes == ["Focus Legacy"]:
        rows = await _fl_add_group_names(rows)
    _fl_breakdown = (_focus_legacy_breakdown_answer(sql, rows, entity_result.get("display") or {}, _fl_unplaced)
                     or _focus_legacy_group_list_answer(rows, entity_result.get("display") or {},
                                                        _fl_total[0] if _fl_total else None)
                     if schemes == ["Focus Legacy"] else None)
    d.update(rows=rows, notes=notes, style=_style, fl_total=_fl_total, fl_unplaced=_fl_unplaced,
             fl_breakdown=_fl_breakdown)
    return None


async def _data_compose_stage(d: dict, scope: "auth.UserScope | None" = None) -> None:
    """compose_response, unless a deterministic Focus Legacy answer exists (graph: compose)."""
    question, schemes, entity_result, sql, rows = (d["question"], d["schemes"], d["entity_result"],
                                                   d["sql"], d["rows"])
    notes, _style, _fl_breakdown = d["notes"], d["style"], d["fl_breakdown"]
    _fl_total, _fl_unplaced = d["fl_total"], d["fl_unplaced"]
    answer = _fl_breakdown or await compose_response(question, sql, rows, notes=notes,
                                    entities=entity_result.get("display"),
                                    schemes=schemes, style_examples=_style,
                                    extra_numbers=({str(_fl_total[0])} if _fl_total else set()) | {
                                        str(int(n)) for v in (_fl_unplaced[1] if _fl_unplaced else {}).values()
                                        for n in [_as_number(v)] if n is not None} or None)
    d["answer"] = answer


async def _data_guarantees_stage(d: dict, scope: "auth.UserScope | None" = None) -> None:
    """Per-scheme deterministic answer guarantees after composing (graph: guarantees)."""
    question, schemes, entity_result, sql, rows = (d["question"], d["schemes"], d["entity_result"],
                                                   d["sql"], d["rows"])
    answer, _fp_amount, _mg_women = d["answer"], d["fp_amount"], d["mg_women"]
    _fl_total, _fl_unplaced, _fl_breakdown = d["fl_total"], d["fl_unplaced"], d["fl_breakdown"]
    # A figure the composer spelled out ("Eighteen producer groups") becomes
    # digits when that value is in the result, every scheme (all-blocks run
    # 2026-10-02); a spelled-out number is invisible to the faithfulness checks.
    answer = _cme_digits_for_number_words(answer, rows)
    # A stated Focus Plus payment amount: the answer must say which payments it
    # covers, and that Focus Plus is not a loan when the user called it one.
    # The composer note alone was ignored live (2026-09-29: "Amount disbursed:
    # ₹466,430,000.00."), so it is guaranteed here (CLAUDE.md §5).
    if _fp_amount is not None and int(_fp_amount.value) in _FOCUSPLUS_PAYMENT_AMOUNTS \
            and re.search(rf"\bamount_disbursed\s*=\s*'?{int(_fp_amount.value)}\b", sql or ""):
        _n = f"₹{int(_fp_amount.value):,}"
        if _fp_amount.noun == "loan" and not re.search(r"not\s+a\s+loan", answer, re.IGNORECASE):
            answer = (answer.rstrip() + f" Focus Plus is a cash benefit, not a loan; this covers only the "
                      f"{_n} payments — {_FOCUSPLUS_PAYMENT_AMOUNTS[int(_fp_amount.value)]}.")
        elif not re.search(rf"{_n}\s+payments|{int(_fp_amount.value):,}\s+payments", answer):
            answer = (answer.rstrip() + f" This covers only the {_n} payments — "
                      f"{_FOCUSPLUS_PAYMENT_AMOUNTS[int(_fp_amount.value)]}.")
    if schemes == ["MGNREGA"]:
        answer = _mgnrega_top_one_wording(answer, sql, rows)
        answer = _mgnrega_money_units(answer, sql, rows)
        if "women_employment_provided" not in (sql or "").lower():
            answer = _mgnrega_zero_backstop(answer, rows, entity_result.get("display") or {})
        if _mg_women:
            # The years a women figure covers are part of the figure (see
            # _mgnrega_women_query); state them even if the composer did not.
            _yrs = _mg_women["years"]
            if not all(y["fy"] in answer for y in (_yrs[0], _yrs[-1])):
                answer = answer.rstrip() + f" This covers {_women_span(_yrs)}" + (
                    " — the years that have women-employment data" if _mg_women["all_years"] else "") + "."
            if _mg_women["all_years"] and _mg_women["missing"] and not any(
                    y["fy"] in answer for y in _mg_women["missing"]):
                answer = answer.rstrip() + (
                    f" Women employment is not recorded at source for "
                    f"{', '.join('FY ' + y['fy'] for y in _mg_women['missing'])}.")
    if schemes == ["PMAY-G"]:
        # bare rupee cells ("795,860,000.00") -> ₹ with lakh / crore (KI-095)
        answer = _pmay_rupee_format(answer, rows)
        answer = _pmay_fy_labels(answer, rows)
    if schemes == ["Focus Plus"]:
        # shares, comparison difference, rupee formatting (KI-060/061/064)
        answer = await _focusplus_answer_guarantees(question, sql, rows, answer)
    if schemes == ["CM Elevate"]:
        # complete lists, programme split + combined, comparison differences,
        # readable two-label breakdowns, stated zeros (KI-068..074)
        answer = await _cm_elevate_answer_guarantees(question, sql, rows, answer,
                                                     entity_result.get("display") or {})
    if schemes == ["CM Elevate Legacy"]:
        # derived "N villages each have V records" counts, no-village records (KI-167)
        answer = await _cm_legacy_answer_guarantees(question, sql, rows, answer,
                                                    entity_result.get("display") or {})
    if schemes == ["Focus Legacy"]:
        # month numbers -> month names, bare rupees -> ₹ Indian grouping (KI-147);
        # records with no block / village / AC stated (KI-145)
        answer = _focus_legacy_answer_guarantees(answer, sql, rows)
        if _fl_unplaced and not _fl_breakdown:
            answer = _focus_legacy_unplaced_guarantee(answer, *_fl_unplaced)
    if _fl_total and not re.search(rf"\b{_fl_total[0]:,}\b|\b{_fl_total[0]}\b",
                                   answer.split("\n", 1)[0]):
        # Deterministic guarantee: the composer (or its row-by-row fallback)
        # did not lead with the true count, so the list would read as complete.
        answer = (f"{_fl_total[0]:,} {_fl_total[1]} match in total; the top {len(rows)} are "
                  f"shown below.\n\n{answer}")
    # Year-gap guarantee: the question named a year the scheme does not hold and
    # _apply_year_gap answered for another. The note says so, but the composer
    # dropped it (TC-24: "Compare FY 2023-24 and FY 2024-25" answered with
    # 2022-23 and 2024-25 and no word about 2023-24). Lead with the note.
    _gap_note = next((n for n in (entity_result.get("notes") or []) if "holds no data" in n), None)
    if entity_result.get("question") and _gap_note:
        _absent = re.search(r"\bFY\s*(\d{4}-\d{2})", _gap_note)
        if _absent and _absent.group(1) not in answer:
            answer = f"{_gap_note}\n\n{answer}"
    d["answer"] = answer


def _data_assemble(d: dict) -> dict:
    """The DATA result dict (graph: assemble)."""
    schemes, entity_result, sql, rows, answer = (d["schemes"], d["entity_result"], d["sql"],
                                                 d["rows"], d["answer"])
    return {
        "route": "data",
        "intent": "DATA",
        "confidence": "high" if rows else "low",
        "schemes": schemes,
        "resolved_entities": entity_result["resolved"],
        "sql": sql,
        "sql_query": sql,
        "row_count": len(rows),
        "rows": rows[:20],
        # The full result the query returned, for the table/chart renderer.
        # Was capped at 200, which silently truncated a legitimate answer — a
        # "which villages…" question over a constituency returns ~150 rows and
        # the user has no way to reach the rest (reported 2026-09-17).
        # run_readonly() already bounds every query at SQL_MAX_RESULT_ROWS
        # (1000), so this is not an unbounded payload; `row_count` above stays
        # the true total either way.
        "data": rows,
        "answer": answer,
    }


# ── "What is EKH?" — spell out a district / block name or abbreviation ──────
# A bare "what is <term>", "<term> full form", "what does <term> stand for"
# where <term> is one of Meghalaya's 12 districts or its ~56 C&RD blocks. The
# KB has no glossary for these, and the edge whitelist bounces a lone "ekh" as
# off-topic — so both routes fail the user. Answer it straight from the
# entity-resolver catalogue instead. A question that also wants a figure
# ("person-days in EKH") carries a data cue and is left for the DATA path.
_DEFN_CUE_RE = re.compile(
    r"\b(what(?:'?s| is| are| was| does| do)?|whats|what do you (?:mean by|call)|"
    r"full[\s-]?form|full name|long form|short form|expand(?:ed)?|expansion|"
    r"meaning|abbreviat\w*|acronym|stands? for|stand for|define|definition)\b",
    re.IGNORECASE,
)
_DEFN_STRIP_RE = re.compile(
    r"\b(what(?:'?s| is| are| was| does| do)?|whats|what do you (?:mean by|call)|"
    r"the full form of|full[\s-]?form of|full name of|full form for|long form of|"
    r"short form of|full[\s-]?form|full name|long form|short form|"
    r"the meaning of|meaning of|abbreviat\w* of|abbreviat\w* for|"
    r"acronym for|acronym of|expansion of|expanded|expand|"
    r"definition of|define|tell me|please|does|do|stands? for|stand for|"
    r"means?|in full|name of|the name of|"
    r"districts?|distt|dist|blocks?|c&rd|cd|"
    r"in meghalaya|of meghalaya|meghalaya)\b",
    re.IGNORECASE,
)


def _format_geo_definition(hit: dict) -> str:
    name = hit["display"]
    acronym = str(hit.get("acronym") or "").strip()
    if hit["type"] == "district":
        lead = (f"“{acronym}” is short for {name}"
                if acronym and hit.get("used_abbrev") else name)
        hq = hit.get("hq")
        seat = f" (headquarters: {hq})" if hq else ""
        return (
            f"{lead} — one of the 12 districts of Meghalaya{seat}. "
            f"Ask for its MGNREGA or PMAY-G figures, e.g. "
            f"“PMAY-G houses completed in {name}” or "
            f"“MGNREGA person-days in {name} in 2023-24”."
        )
    parent = hit.get("district")
    where = f" in {parent} district" if parent else ""
    return (
        f"{name} is a C&RD (community & rural development) block{where} of "
        f"Meghalaya. Ask for its MGNREGA or PMAY-G figures, e.g. "
        f"“MGNREGA person-days in {name}” or "
        f"“PMAY-G houses sanctioned in {name}”."
    )


def _geo_definition_answer(question: str) -> "dict | None":
    """A direct answer for a 'spell out this district / block' question, or None
    to let normal routing handle it."""
    q = (question or "").strip()
    if not q or len(q.split()) > 10:
        return None
    if not _DEFN_CUE_RE.search(q):
        return None
    if _DATA_HINTS.search(q) or _AGGREGATE_CUE.search(q):
        return None
    core = _DEFN_STRIP_RE.sub(" ", q)
    core = re.sub(r"[^\w&./ -]+", " ", core)
    core = re.sub(r"\s+", " ", core).strip(" -.")
    if not core or len(core.split()) > 5:
        return None
    hit = lookup_geo_term(core)
    if not hit:
        return None
    logger.info("geo-definition shortcut: %r -> %s %r", question, hit["type"], hit["display"])
    return {
        "route": "knowledge", "intent": "RAG", "confidence": "high",
        "answer": _format_geo_definition(hit), "sources": [],
        **_empty_data_fields(),
    }


async def answer_question(question: str, session: "Session | None" = None,
                          scope: "auth.UserScope | None" = None) -> dict:
    """Public entry point. Runs the pipeline, then attaches deterministic
    'Next steps' suggestions to any data/knowledge answer.

    With PIPELINE_GRAPH_ENABLED (or this session in the canary), the same
    stages run as a LangGraph graph instead (app/pipeline_graph.py, D-032):
    same result dict, same exceptions, same context update."""
    if _pipeline_graph_selected(session):
        from app import pipeline_graph
        return await pipeline_graph.answer_question_via_graph(question, session=session, scope=scope)
    raw_question = question
    result = await _run_pipeline(question, session=session, scope=scope)
    _attach_followups(result, result.get("rewritten_question") or raw_question)
    await _update_context(session, raw_question, result)
    return result


def _pipeline_graph_selected(session: "Session | None") -> bool:
    """Whether this request runs on the LangGraph orchestrator. Off by default.
    The canary is sticky per session (a stable hash of the session id), so one
    conversation never switches orchestrator between turns."""
    if settings.PIPELINE_GRAPH_ENABLED:
        return True
    pct = settings.PIPELINE_GRAPH_CANARY_PERCENT
    if pct <= 0 or session is None:
        return False
    import zlib
    return zlib.crc32(session.session_id.encode("utf-8")) % 100 < pct


async def _update_context(session: "Session | None", raw_question: str, result: dict) -> None:
    # Context Updater — fold this turn into the session's structured state and
    # refresh the rolling summary (both best-effort; see context_manager).
    # Skipped for a clarification pause: nothing was actually answered yet,
    # and _run_pipeline never reaches here for one anyway (it raises).
    if settings.CONTEXT_LAYER_ENABLED and session is not None:
        try:
            context_manager.update_state(
                session, raw_question, result.get("rewritten_question") or raw_question, result)
            await context_manager.maybe_update_summary(session)
        except Exception:  # noqa: BLE001 — the context layer must never break an answer
            logger.warning("context layer post-processing failed (non-fatal)", exc_info=True)


def _attach_followups(result: dict, question: str) -> None:
    """Add `follow_up_options` (+ back-compat `follow_ups` / `follow_up`) to a
    data/knowledge result. Best-effort — a failure here never breaks the answer."""
    if not settings.FOLLOWUP_SUGGEST_ENABLED:
        return
    if result.get("route") not in ("data", "knowledge"):
        return
    try:
        opts = followups.build_followups(
            result["route"], question,
            result.get("schemes") or [], result.get("resolved_entities") or {},
            sql=result.get("sql"),
            rows=result.get("rows") or result.get("data"))
    except Exception:  # noqa: BLE001
        logger.warning("follow-up suggestion build failed", exc_info=True)
        return
    if opts:
        result["follow_up_options"] = opts
        result["follow_ups"] = [o["question"] for o in opts]
        result["follow_up"] = opts[0]["question"]


# ── Turn stages (D-032) ─────────────────────────────────────────────────────
# _run_pipeline was one function. Its steps are now the stages below, moved
# verbatim; the LangGraph orchestrator runs the same stages as its context ->
# edge -> followup -> route -> knowledge / data_context nodes. Each reads its
# inputs from the per-turn dict `t` into the local names the code always used,
# writes its outputs back, and returns a finished result to end the turn early
# or None to go on. `t` holds the live objects (Turn, ConversationState,
# MergePlan); the graph stores them as plain dicts between nodes.

def _new_turn(question: str) -> dict:
    return {"question": question, "raw_question": question, "cm_pinned": False,
            "scope_resumed": False, "prev": None, "village_hint": None, "paused_state": None,
            "signals": [], "has_context": False, "plan": None, "thread_state": None,
            "is_followup_rewrite": False, "intent": None, "prior_resolved": None}


def _turn_ctx_state(session: "Session | None") -> "ConversationState | None":
    return (session.state if (session is not None and settings.CONTEXT_LAYER_ENABLED
                              and settings.CONTEXT_STATE_ENABLED) else None)


async def _run_pipeline(question: str, session: "Session | None" = None,
                        scope: "auth.UserScope | None" = None) -> dict:
    t = _new_turn(question)
    for stage in (_turn_context_stage, _turn_edge_stage, _turn_followup_stage,
                  _turn_route_stage, _turn_knowledge_stage):
        result = await stage(t, session, scope)
        if result is not None:
            return result
    await _turn_data_context_stage(t, session, scope)
    question, raw_question = t["question"], t["raw_question"]
    scope_resumed, _village_hint, _prior_resolved = t["scope_resumed"], t["village_hint"], t["prior_resolved"]
    try:
        # the typed text of a rewritten follow-up, for the PMAY-G facts path (_PMAY_TYPED_TURN)
        _typed_token = _PMAY_TYPED_TURN.set(raw_question if question != raw_question else None)
        try:
            result = await _answer_data(question, scope=scope, skip_scope_clarify=scope_resumed,
                                         prior_resolved=_prior_resolved,
                                         village_hint=_village_hint if scope_resumed else None)
        finally:
            _PMAY_TYPED_TURN.reset(_typed_token)
        return _with_rewritten_question(result, question, raw_question)
    except ClarificationNeeded:
        raise
    except Exception as e:  # noqa: BLE001 — mapped exactly as before, see _data_path_error_result
        return await _data_path_error_result(e, question, raw_question)


def _with_rewritten_question(result: dict, question: str, raw_question: str) -> dict:
    """A DATA result carries the standalone question it was answered as."""
    if question != raw_question:
        result["rewritten_question"] = question
    return result


async def _data_path_error_result(error: Exception, question: str, raw_question: str) -> dict:
    """What a failure on the DATA path becomes: the scope reply, a re-raise for
    the router (gateway / DB trouble), or the KB fallback. Shared by both
    orchestrators; the except chain is the one _run_pipeline always had."""
    try:
        raise error
    except OutOfScope as e:
        logger.info("out of scope (%s) — returning Megh One AI scope reply", e)
        return _out_of_scope_result(question, raw_question)
    except (llm.ModelBusyError, asyncio.TimeoutError, httpx.TimeoutException,
            httpx.TransportError) as e:
        # The model gateway hiccuped (saturated slot, read timeout, connection
        # reset) part-way through the DATA path — this is NOT "the question can't
        # be answered from the data". Propagate it so the router returns a
        # 503/504 "busy, please retry" instead of the misleading "couldn't build
        # a working query" fallback below, which reads as if the question itself
        # were at fault.
        logger.warning("data path hit a transient gateway error (%s) — re-raising", e)
        raise
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code >= 500:
            logger.warning("data path hit gateway %s — re-raising", e.response.status_code)
            raise
        logger.warning("data path failed (%s) — trying KB fallback", e, exc_info=True)
        return await _data_path_kb_fallback(question)
    except Exception as e:  # noqa: BLE001
        # megh_db unreachable anywhere on the DATA path (village lookup, SQL,
        # a recount): say so — the reference documents cannot answer a data
        # question, and "not in the reference material" read as if the figure
        # did not exist (KI-025).
        if db_is_connection_error(e):
            logger.warning("data path lost the database (%s) — re-raising", e)
            if isinstance(e, DatabaseUnavailableError):
                raise
            raise DatabaseUnavailableError(str(e)) from e
        logger.warning("data path failed (%s) — trying KB fallback", e, exc_info=True)
        return await _data_path_kb_fallback(question)


async def _turn_context_stage(t: dict, session: "Session | None" = None,
                              scope: "auth.UserScope | None" = None) -> "dict | None":
    """Spelling, dataset pin, turn facts, a pending pause's reply, and whether
    this message continues the conversation at all (graph: context)."""
    question = t["question"]
    raw_question = question
    question = _correct_scheme_spelling(question)
    # "total amount disbursed under CM Elevate" can only be answered by CM
    # Elevate Legacy — settle which CM Elevate dataset is meant, once, here.
    # A DATA decision only: the KNOWLEDGE route undoes it (see _unpin_cm_elevate).
    _pinned = _pin_cm_elevate_dataset(question)
    _cm_pinned = _pinned != question
    question = _pinned
    scope_resumed = False

    # Computed early (moved ahead of the original follow-up step) so the step-0
    # edge check below can relax its whitelist gate for a plausible follow-up —
    # see edge.detect_edge_case's has_context param.
    prev = session.last_turn if session is not None else None
    has_antecedent = prev is not None and prev.route in _ANTECEDENT_ROUTES
    ctx_state = _turn_ctx_state(session)
    # Facts about this turn for provenance (context_manager.update_state):
    # the state before it, the previous question, and, set below, the pause
    # reply and the question after reference substitution. Never persisted.
    if session is not None:
        _before = session.state.to_dict()
        session.turn_context = {
            "state_before": {k: _before.get(k) for k in
                             ("scheme", "district", "block", "village", "year", "metric", "tranche")},
            "previous_question": getattr(prev, "question", "") or "",
            "clarification_reply": None,
            "substituted_question": None,
            # The full question a pause reply resumed into. If this turn pauses
            # AGAIN, the router remembers this instead of the raw reply: a typed
            # "Focus Plus" resumed the bank question, the area pause that
            # followed was remembered as "Focus Plus", and the typed "All of
            # Meghalaya" then ran as "Focus Plus, All of Meghalaya" — a scheme
            # overview (Focus Plus use-case QA 2026-09-27, FOCUS-029).
            "resumed_question": None,
            # The rewritten follow-up, as it stood before any pause (see step 1f).
            "standalone_question": None,
        }

    # 0a. Resuming a "which area / year?" pause — fold the user's free-text reply
    #     ("West Garo Hills 2023-24", "all of Meghalaya, all years") back into the
    #     question that triggered it. A reply that stands on its own as a fresh
    #     question, or an edge case like "thanks", is left alone; either way the
    #     pending state is consumed so it never leaks into a later turn.
    pending = getattr(session, "pending_scope_q", None) if session is not None else None
    _village_hint = getattr(session, "pending_village_hint", None) if session is not None else None
    _pending_rule = getattr(session, "pending_scope_rule", None) if session is not None else None
    _pending_options = getattr(session, "pending_scope_options", None) if session is not None else None
    if pending and _pending_rule in SCHEME_PAUSE_RULES:
        # 0a'. A "which scheme?" / "which Focus?" pause. This is a scheme choice,
        #      not a scope fragment, so the scope merge below does not apply. A
        #      typed scheme name becomes the chip question it stands for; any
        #      other reply goes through untouched. scope_resumed stays False
        #      either way, because the chip path never set it: the resumed
        #      question may still need its own area/year pause.
        session.pending_scope_q = None
        session.pending_village_hint = None
        session.pending_scope_rule = None
        session.pending_scope_options = None
        _picked = _resume_scheme_pause(question, _pending_options)
        if _picked:
            logger.info("scheme clarification resumed from typed reply %r -> %r", question, _picked)
            session.turn_context["clarification_reply"] = question
            # The same CM Elevate dataset decision the chip's text gets at the top.
            question = _pin_cm_elevate_dataset(_picked)
            _cm_pinned = question != _picked
            session.turn_context["resumed_question"] = question
        pending = None
    _paused_state = None
    if pending and _pending_rule and _pending_rule not in SCOPE_MERGE_RULES:
        # 0a''. Any other pause that offered chips (_resume_option_pause). A
        #       typed pick becomes that chip's question. An unmatched reply
        #       that names no scheme continues the PAUSED question's scheme,
        #       not the last answered turn's (_paused_thread_antecedent).
        session.pending_scope_q = None
        session.pending_village_hint = None
        session.pending_scope_rule = None
        session.pending_scope_options = None
        _picked = _resume_option_pause(question, _pending_options)
        if _picked:
            logger.info("option clarification (%s) resumed from typed reply %r -> %r",
                        _pending_rule, question, _picked)
            context_budget.log_decision("pause_reply", rule=_pending_rule, resumed="option")
            session.turn_context["clarification_reply"] = question
            question = _pin_cm_elevate_dataset(_picked)
            _cm_pinned = question != _picked
            session.turn_context["resumed_question"] = question
        else:
            _stand_in = _paused_thread_antecedent(question, pending, prev)
            if _stand_in is not None:
                context_budget.log_decision("pause_reply", rule=_pending_rule,
                                            resumed="paused-thread", schemes=_stand_in.schemes)
                prev, has_antecedent = _stand_in, True
                _st = ctx_state
                _paused_state = ConversationState(
                    scheme=_stand_in.schemes[0],
                    district=getattr(_st, "district", None), block=getattr(_st, "block", None),
                    village=getattr(_st, "village", None), year=getattr(_st, "year", None),
                    year_all=bool(getattr(_st, "year_all", False)))
        pending = None
    if pending:
        session.pending_scope_q = None
        session.pending_village_hint = None
        session.pending_scope_rule = None
        session.pending_scope_options = None
        # A scope-pause reply is normally a bare fragment ("Ri Bhoi, 2023-24",
        # "wgh, 1999-20") with no scheme vocabulary of its own, so the edge
        # whitelist would tag it "off_topic" every time — do NOT use that as the
        # signal to drop the pause. Only a clearly conversational reply (a
        # greeting / thanks / goodbye / abuse) or a fresh standalone question
        # abandons it; everything else is merged back into the paused question.
        _edge_hit = edge.detect_edge_case(question)
        # "off_topic" is the whitelist gate mis-firing on a fragment with no
        # scheme words — NOT a reason to drop the pause. Any other edge verdict
        # (greeting / thanks / goodbye / abuse / "never mind") genuinely is.
        _bailed = bool(_edge_hit) and _edge_hit.get("type") != "off_topic"
        # A one-tap chip (year pause) sends the whole rewritten question, which
        # already begins with the paused stem — merging would just duplicate it
        # ("total expenditure for MGNREGA, total expenditure for MGNREGA for FY
        # 2023-24"). Detect that and pass the chip's question straight through.
        _stem = pending.rstrip(" ?.").lower()
        _is_chip_resume = question.strip().lower().startswith(_stem)
        if _is_chip_resume:
            scope_resumed = True
            logger.info("scope/year clarification resumed via full question -> %r", question)
        elif not _bailed and not _reply_abandons_scope_pause(question):
            session.turn_context["clarification_reply"] = question
            question = f"{pending.rstrip(' ?.')}, {question.strip()}"
            scope_resumed = True
            logger.info("scope clarification resumed -> %r", question)
        if scope_resumed and not session.turn_context.get("clarification_reply"):
            session.turn_context["clarification_reply"] = question
        if scope_resumed:
            session.turn_context["resumed_question"] = question

    # 0-c. Does this message continue the conversation at all? A previous
    #      answer is context only for a message that shares something with it:
    #      scheme or measure words, a place, a year, a grouping, a reference into
    #      the result, a question about how a scheme works
    #      (context_policy.continuation_signals). "who is harshit" and "he is my
    #      collik remember" after a Focus Plus answer share nothing, yet were
    #      rewritten "…under Focus Plus" and answered from its reference docs
    #      (reported 2026-09-29): the edge whitelist was switched off by the mere
    #      existence of a previous answer, and looks_like_followup took any short
    #      anchorless message as a fragment. Without a signal, the message is a
    #      new question: the whitelist applies and no rewrite runs. A pause reply
    #      always continues its paused question.
    _signals = context_policy.continuation_signals(question) if has_antecedent else []
    has_context = has_antecedent and (bool(_signals) or scope_resumed)
    if session is not None:
        session.turn_context["continuation"] = _signals
    if has_antecedent and not has_context:
        context_budget.log_decision("continuation", continues=False, signals=[],
                                    previous_route=getattr(prev, "route", None))
    t.update(raw_question=raw_question, question=question, cm_pinned=_cm_pinned,
             scope_resumed=scope_resumed, prev=prev, village_hint=_village_hint,
             paused_state=_paused_state, signals=_signals, has_context=has_context)
    return None


async def _turn_edge_stage(t: dict, session: "Session | None" = None,
                           scope: "auth.UserScope | None" = None) -> "dict | None":
    """Harmful requests, definitions, scheme listing / pick, and the edge layer (graph: edge)."""
    question, raw_question = t["question"], t["raw_question"]
    scope_resumed, has_context = t["scope_resumed"], t["has_context"]
    # 0--. A request for help with something illegal ("i want to rob a bank,
    #      give me suggestions") is refused FIRST — before the recommendation /
    #      pick / listing steps below, any of which could otherwise claim it on a
    #      word like "suggestions" and answer with an unrelated scheme.
    _harm = edge.detect_harmful(question)
    if _harm:
        return {"route": "edge", "intent": "EDGE", "confidence": "high",
                "answer": _harm["response"], "edge_type": _harm["type"],
                "suggestions": [], **_empty_data_fields()}

    # 0-. "What is EKH?" / "MYLLIEM full form" — spell out a district or block
    #     straight from the resolver catalogue. Must run BEFORE the edge layer
    #     (which bounces a lone abbreviation as off-topic) and before routing
    #     (KNOWLEDGE / RAG has no glossary for these). Skipped on a scope-pause
    #     resume — that text is a merged fragment, never a definition request.
    if not scope_resumed:
        geo_def = _geo_definition_answer(question)
        if geo_def:
            if question != raw_question:
                geo_def["rewritten_question"] = question
            return geo_def

    # 0-a. "What schemes are available?" / "what's the difference between the
    #      schemes?" — answered directly (see _scheme_listing_answer /
    #      _scheme_comparison_answer) rather than falling through to RAG, which
    #      has no single document covering either and either over-elaborates on
    #      one scheme or says "not covered". Same scope-pause carve-out as above.
    if not scope_resumed:
        # "Pick any scheme and explain it" — checked FIRST: it is neither a
        # listing nor a follow-up, and must not reach the "which scheme?" pause
        # or the follow-up rewrite (which re-reads it against the last scheme).
        # "Why did you choose MGNREGA?" / "suggest a scheme that suits me" — a
        # question about the bot's own choice, and a recommendation ACROSS
        # schemes. Neither is in any one scheme's documents, so both are
        # answered here, before the knowledge route narrows to one scheme.
        explained = _why_choice_answer(question, session)
        if explained:
            return explained
        recommended = (_scheme_fit_check_answer(question)
                       or _scheme_recommendation_answer(question, session))
        if recommended:
            return recommended
        picked = await _scheme_pick_answer(question, session)
        if picked:
            return picked
        direct = _scheme_listing_answer(question) or _scheme_comparison_answer(question)
        if direct:
            if question != raw_question:
                direct["rewritten_question"] = question
            return direct

    # 0. Edge — greetings, identity, thanks, off-topic, abuse. Checked on the RAW
    #    text first: the follow-up heuristic treats any short anchorless phrase as
    #    a fragment, which would otherwise turn "hello" into a bogus follow-up.
    #    Skipped when we just merged a scope-pause reply: the merged text is a
    #    bare "<question>, <place>, <year>" fragment that the whitelist gate
    #    would wrongly flag as off-topic, and the conversational-reply case was
    #    already handled at the merge above.
    hit = None if scope_resumed else edge.detect_edge_case(question, has_context=has_context)
    if hit and await _mgnrega_village_not_out_of_area(question, hit):
        hit = None
    if hit:
        context_budget.log_decision("edge", edge_type=hit["type"], has_context=has_context)
        return {"route": "edge", "intent": "EDGE", "confidence": "high",
                "answer": hit["response"], "edge_type": hit["type"],
                "suggestions": hit.get("suggestions", []),
                **_empty_data_fields()}
    return None


async def _turn_followup_stage(t: dict, session: "Session | None" = None,
                               scope: "auth.UserScope | None" = None) -> "dict | None":
    """Reference substitution, the follow-up merge plan and rewrite, and the
    scheme / year hints (graph: followup)."""
    question, raw_question, prev = t["question"], t["raw_question"], t["prev"]
    scope_resumed, has_context, _signals = t["scope_resumed"], t["has_context"], t["signals"]
    _paused_state, _cm_pinned = t["paused_state"], t["cm_pinned"]
    ctx_state = _turn_ctx_state(session)
    # 0f. Deterministic reference resolution ("the previous year", "the
    #     current year", "the former/latter", "the other one", "both") against
    #     the session's structured conversation state — see
    #     context_manager.substitute_references. Runs before the follow-up
    #     detector so a bare "compare that with the previous year" already has
    #     a concrete year by the time it gets there. Ambiguous ("the other
    #     one" with no recorded comparison) reuses the existing clarification
    #     mechanism rather than guessing. Any other failure degrades to the
    #     question unchanged — the pre-existing follow-up path still runs.
    if not scope_resumed and ctx_state is not None:
        try:
            question = context_manager.substitute_references(question, ctx_state)
            if session is not None:
                session.turn_context["substituted_question"] = question
        except context_manager.AmbiguousReference as e:
            raise ClarificationNeeded(e.question, options=e.options, rule="entity-ambiguous")
        except Exception:  # noqa: BLE001
            logger.warning("context_manager.substitute_references failed — continuing unchanged",
                           exc_info=True)

    # 1. Follow-up — rewrite a fragment ("what about EGH?") to a standalone
    #    question using the previous turn, before routing. Only when the previous
    #    turn was an actual scheme answer; otherwise the fragment has nothing
    #    coherent to attach to. (prev / has_antecedent computed above, ahead of
    #    the step-0 edge check.)
    is_followup_rewrite = False
    _plan = None
    # Which conversation thread a follow-up continues (see _followup_thread_state).
    _thread_state = _followup_thread_state(question, prev, ctx_state)
    if _paused_state is not None:
        # A reply to a pause about another scheme continues that scheme's thread
        # (step 0a''), with the scope the paused question carried.
        _thread_state = _paused_state
    if _thread_state is not ctx_state:
        context_budget.log_decision("followup", thread="antecedent-scheme",
                                    scheme=getattr(_thread_state, "scheme", None))
    # A bare "Focus" used as a scheme name is never handed to the model rewrite:
    # the which-Focus question below must ask (CLAUDE.md §4 — never guessed).
    # The deterministic scheme swap ("for focus", "same for focus") keeps the
    # word "Focus" itself, so it still runs.
    _bare_focus = (_names_bare_focus_scheme(question)
                   and not _SCHEME_SWAP_FOLLOWUP.match(question.strip())
                   and not _scheme_substitution(question)[1])
    if _bare_focus:
        context_budget.log_decision("followup", skipped="bare-focus-scheme-name")
    if (looks_like_followup(question) or is_scopeless_followup(question, prev)
            or is_scheme_substitution(question, prev)) and not _bare_focus:
        if has_context:
            # Field-by-field effect of this follow-up on the state (KEEP / REPLACE
            # / CLEAR / REQUIRE_CLARIFICATION) and its kind, which picks the
            # context layers the rewrite gets. Deterministic, no model call.
            try:
                _plan = context_policy.plan_state_merge(question, _thread_state, prev=prev,
                                                        is_followup=True)
                if session is not None:
                    session.turn_context["plan"] = _plan.to_dict()
            except Exception:  # noqa: BLE001 — no plan means the pre-plan behaviour
                logger.warning("context_policy.plan_state_merge failed — continuing without it",
                               exc_info=True)
                _plan = None
            context_budget.log_decision(
                "followup", signals=_signals, kind=_plan.kind if _plan else None,
                actions={k: v for k, v in (_plan.actions if _plan else {}).items()
                         if v != context_policy.KEEP})
            # "Which one?" with nothing to pick by: ask, never let the rewrite
            # guess an item (the plan marks it REQUIRE_CLARIFICATION). Not a
            # remembered pause: the reply is read as a question of its own.
            if _plan is not None and _plan.action("reference") == context_policy.REQUIRE_CLARIFICATION:
                raise ClarificationNeeded(
                    "Which one do you mean? Please name it — the district, block, village or "
                    "scheme — or ask the whole question, for example \"Which district had the "
                    "highest disbursement?\"", rule="reference-ambiguous")
            _extra_ctx = ""
            if settings.CONTEXT_LAYER_ENABLED and session is not None:
                try:
                    _extra_ctx = await context_manager.build_followup_context(
                        session, question,
                        state=_thread_state if _thread_state is not ctx_state else None)
                except Exception:  # noqa: BLE001
                    logger.warning("context_manager.build_followup_context failed — continuing without it",
                                   exc_info=True)
            _data_prev = _data_thread_antecedent(question, prev, session, _plan)
            if _data_prev is not None:
                prev = _data_prev
                context_budget.log_decision("followup", antecedent="last-data-turn",
                                            schemes=list(getattr(prev, "schemes", None) or []))
            _all_years_q = (_all_years_rewrite(getattr(prev, "question", "") or "")
                            if _plan is not None and _plan.action("year") == context_policy.CLEAR
                            and (_plan.reasons.get("year") or "").startswith("'all of them'") else None)
            if _all_years_q:
                question = _all_years_q
                context_budget.log_decision("followup", rewrite="deterministic-all-years")
            elif _measure_after_knowledge(question, prev):
                # After a KNOWLEDGE answer the only context is its scheme:
                # append it; the typed measure stays word for word.
                question = f"{question.strip().rstrip(' ?.')} under {prev.schemes[0]}?"
                context_budget.log_decision("followup", rewrite="deterministic-measure-after-knowledge",
                                            scheme=prev.schemes[0])
            else:
                question = await rewrite_followup(question, prev, extra_context=_extra_ctx,
                                                  kind=_plan.kind if _plan else None,
                                                  cleared=_cleared_filters(_plan, prev, _thread_state))
            # The rewrite can introduce a bare "CM Elevate" alongside a money word
            # ("...and the amount disbursed?") — same dataset decision as above.
            _pinned = _pin_cm_elevate_dataset(question)
            _cm_pinned = _pinned != question
            question = _pinned
            is_followup_rewrite = True
            # "give me for pmay" after MGNREGA person-days: PMAY-G has no
            # person-days. Offer PMAY-G's own measures for the same scope
            # instead of a query that cannot be built (_swap_measure_gap).
            _gap = _swap_measure_gap(raw_question, question, prev, _thread_state)
            if _gap is not None:
                context_budget.log_decision("followup", rewrite="swap-measure-unavailable",
                                            question=question)
                # Remembered as the pause's question (routers.query.pause_question):
                # the first offer is a real question in the new scheme with the
                # carried scope, so an unmatched typed reply ("houses completed
                # in West Garo Hills") is rewritten against IT, not against the
                # measure the new scheme does not hold.
                if session is not None:
                    session.turn_context["standalone_question"] = _gap.options[0]["question"]
                raise _gap
        elif _CONTEXTLESS_REF.search(question) and not _mentions_scheme(question):
            # "how launched it?" with no prior scheme answer — don't guess.
            return {"route": "edge", "intent": "EDGE", "confidence": "high",
                    "edge_type": "confused",
                    "answer": ("I don't have an earlier answer to build on, so I'm not "
                               "sure what that refers to. Tell me the scheme — MGNREGA, "
                               "PMAY-G, Focus Plus, CM Elevate, Focus Legacy, or CM Elevate "
                               "Legacy — and what "
                               "you'd like to know."),
                    "suggestions": list(edge.STARTERS),
                    **_empty_data_fields()}

    # 1b. Re-check edge on the rewrite (cheap, and the rewrite can surface one).
    #     Same scope-resume carve-out as step 0.
    hit = None if scope_resumed else edge.detect_edge_case(question, has_context=has_context)
    if hit and await _mgnrega_village_not_out_of_area(question, hit):
        hit = None
    if hit:
        context_budget.log_decision("edge", edge_type=hit["type"], has_context=has_context,
                                    after_rewrite=True)
        return {"route": "edge", "intent": "EDGE", "confidence": "high",
                "answer": hit["response"], "edge_type": hit["type"],
                "suggestions": hit.get("suggestions", []),
                "rewritten_question": question if question != raw_question else None,
                **_empty_data_fields()}

    # 1f. A follow-up fragment that rewrote to a standalone question still
    #     naming no scheme (and no scheme-specific vocabulary of its own) gets
    #     the session's pinned scheme appended deterministically — see
    #     context_manager.inject_scheme_hint. Only for an actual continuation
    #     (is_followup_rewrite), never for a brand-new question: that case is
    #     deliberately left to the existing "which scheme?" pause below.
    if is_followup_rewrite and _thread_state is not None:
        try:
            question = context_manager.inject_scheme_hint(question, _thread_state)
        except Exception:  # noqa: BLE001
            logger.warning("context_manager.inject_scheme_hint failed — continuing unchanged",
                           exc_info=True)
        # "All financial years", once chosen, is a resolved time scope like a
        # year is (KI-030): carried into a follow-up that keeps the year, so the
        # year pause is not asked again.
        try:
            question = context_manager.inject_year_scope(
                question, _thread_state, _plan.action("year") if _plan else context_policy.KEEP)
        except Exception:  # noqa: BLE001
            logger.warning("context_manager.inject_year_scope failed — continuing unchanged",
                           exc_info=True)
    # The question as resolved so far (rewrite, scheme hint, year scope). If
    # this turn pauses, the router remembers THIS, not the typed fragment
    # (routers.query.pause_question). Live 2026-09-29: "give me beneficiaries"
    # was rewritten "…under CM Elevate Legacy", paused for the year, and the
    # typed "2024-25" merged into "give me beneficiaries, 2024-25" — no scheme,
    # so it needed a second model rewrite to find it again.
    if is_followup_rewrite and session is not None:
        session.turn_context["standalone_question"] = question
    t.update(question=question, prev=prev, cm_pinned=_cm_pinned, plan=_plan,
             thread_state=_thread_state, is_followup_rewrite=is_followup_rewrite)
    return None


async def _turn_route_stage(t: dict, session: "Session | None" = None,
                            scope: "auth.UserScope | None" = None) -> "dict | None":
    """Not-held checks, the village-name search and DATA / KNOWLEDGE (graph: route)."""
    question, raw_question = t["question"], t["raw_question"]
    is_followup_rewrite, _plan = t["is_followup_rewrite"], t["plan"]
    # 1c. Bank / financial-channel details are not held for any loaded scheme —
    #     say so, with the reason, before routing. Checked ahead of the DATA/
    #     KNOWLEDGE split: phrasing like "what is the bank-wise disbursement…"
    #     trips _KNOWLEDGE_HINTS on "what is" and would otherwise dead-end in
    #     RAG ("not covered in the reference material") instead of explaining
    #     that the column itself isn't queryable.
    if _BANK_REQUESTED.search(question):
        _clarification = _bank_clarification(question)
        if _clarification is not None:
            raise _clarification

    # 1d. Administrative expenditure is deliberately excluded from reporting for
    #     every scheme that has it (see _ADMIN_EXPENDITURE_REQUESTED above) —
    #     same reasoning and same place as the bank check just above.
    #     Except MGNREGA: its admin_total_exp column exists, and the use case
    #     (DATA-013) asks for the aggregate, so the DATA path reports the real
    #     recorded figure together with the "never populated at source" caveat
    #     (_mgnrega_admin_expenditure_answer). PMAY-G and the multi-scheme
    #     wording are unchanged.
    if _ADMIN_EXPENDITURE_REQUESTED.search(question) \
            and _named_or_inferred_schemes(question) != ["MGNREGA"]:
        raise _admin_expenditure_clarification(question)

    # 1e. Named a scheme we simply don't hold ("PM-KISAN", "Ujjwala", "Jal
    #     Jeevan"). Checked HERE, before the DATA/KNOWLEDGE split, because the
    #     question is just as often a KNOWLEDGE one ("what is PM Kisan
    #     Yojana?") as a data one. The check used to live only inside
    #     _answer_data, so a knowledge-shaped ask sailed past it into RAG,
    #     found nothing — correctly, it isn't in the reference docs — and got
    #     the flat "I don't have information about that for MGNREGA, PMAY-G,
    #     Focus Plus or CM Elevate", which never says WHY or what to do next
    #     (reported 2026-09-18). The clarification below names the scheme the
    #     user asked for, says plainly that only four are loaded, and offers
    #     them as one-tap chips.
    _unsupported_named = _unsupported_scheme_named(question)
    if _unsupported_named:
        raise _unsupported_scheme_clarification(question, _unsupported_named)

    # 1g. "How many villages are named X?" / "which villages contain X in their
    #     name?" — an entity SEARCH. Answered before routing, so X is never
    #     narrowed to one resolved place first (see _village_name_search_answer).
    if _village_name_search(question):
        try:
            _vns = await _village_name_search_answer(question)
        except Exception as e:  # noqa: BLE001
            if db_is_connection_error(e):
                raise DatabaseUnavailableError(str(e)) from e
            logger.warning("village name search failed — routing normally", exc_info=True)
            _vns = None
        if _vns:
            if question != raw_question:
                _vns["rewritten_question"] = question
            return _vns

    # 2. Route: number question or scheme-rules question? For a rewritten
    #    follow-up, the user's own words decide when they are decisive — the
    #    rewrite's phrasing ("What are the beneficiaries of…") must not turn a
    #    count request into a reference-docs question (live 2026-09-29).
    intent = (_typed_intent(raw_question) if is_followup_rewrite else None) \
        or await classify_intent(question)
    context_budget.log_decision("intent", intent=intent, followup=is_followup_rewrite,
                                kind=_plan.kind if _plan else None,
                                schemes_named=sorted(_named_schemes(question)))
    t["intent"] = intent
    return None


async def _turn_knowledge_stage(t: dict, session: "Session | None" = None,
                                scope: "auth.UserScope | None" = None) -> "dict | None":
    """The KNOWLEDGE route: RAG over the reference documents (graph: knowledge)."""
    question, raw_question, prev, intent = t["question"], t["raw_question"], t["prev"], t["intent"]
    is_followup_rewrite, has_context, _cm_pinned = t["is_followup_rewrite"], t["has_context"], t["cm_pinned"]
    # 3. KNOWLEDGE -> RAG over the scheme reference docs.
    if intent == "KNOWLEDGE":
        # Both CM Elevate datasets share one knowledge base (rag.kb_scheme), so
        # the data-side "which CM Elevate?" pin means nothing here — give the
        # question back in the user's own words rather than showing a rewrite.
        if _cm_pinned:
            question = _unpin_cm_elevate(question)
        # How a scheme works is not per year: drop a year the rewrite carried
        # over from the previous DATA turn (the user never typed it).
        if is_followup_rewrite:
            question = _drop_inherited_place(_drop_inherited_time(question, raw_question), raw_question)
        # Scope retrieval to a single scheme when we're confident which one this
        # is about — named outright, or (for a follow-up with nothing named of
        # its own) the scheme the previous turn was about. Without this, vector
        # search has no scheme filter at all and can blend in another scheme's
        # content (e.g. PMAY-Urban passages into a PMAY-G-scoped answer).
        _kb_scheme = None
        _named = _named_schemes(question)
        if len(_named) == 1:
            _kb_scheme = _named[0]
        else:
            # Not named outright — but scheme-specific VOCABULARY pins it just
            # as reliably, and this is the same signal the DATA path has always
            # used (_infer_scheme_from_terms returns a scheme only when exactly
            # one scheme's vocabulary matches). Without it, "What is the role of
            # Producer Groups under FOCUS?" searched the whole KB unfiltered:
            # measured on the real corpus, 3 of the top 8 chunks came back from
            # Focus Plus — which holds no producer-group data at all — and the
            # single best hit was one of them, so the composer answered the
            # wrong scheme's question.
            _inferred = _infer_scheme_from_terms(question)
            if _inferred and len(_inferred) == 1:
                _kb_scheme = _inferred[0]
            # Not when the question says a bare "Focus": that names a scheme —
            # just not WHICH of the two — so inheriting the previous turn's
            # scheme would answer the wrong one ("for focus" after a CM Elevate
            # answer re-answered CM Elevate). It goes to the "which Focus?"
            # pause below instead.
            # Only for a message that continues the conversation (step 0-c):
            # "who is harshit" is not a question about the last turn's scheme.
            elif (has_context and prev is not None and len(prev.schemes or []) == 1
                  and not _is_ambiguous_focus(question)):
                _kb_scheme = prev.schemes[0]

        # Genuinely scheme-agnostic ("tell me about the scheme", "how do I
        # apply", "what are the benefits") — no scheme named, no follow-up
        # antecedent, and no vocabulary that pins it to one. Guessing here (or
        # letting bare vector search pick whichever doc scores highest) is how
        # a vague question came back "not covered" while quietly assuming
        # MGNREGA. Ask which of the four schemes instead, same one-tap chips
        # the DATA path already uses (see _needs_scheme_clarification).
        # ...but a question that NAMES a scheme we don't recognise ("what is
        # amma yedi scheme?") is not vague — the user was specific, we simply
        # don't hold it. Asking "which scheme does your question concern?"
        # there ignores what they actually asked; say plainly that it isn't
        # one of the four (reported 2026-09-18).
        if _kb_scheme is None and _names_unknown_scheme(question):
            return {"route": "knowledge", "intent": "RAG", "confidence": "low", "sources": [],
                    "answer": _knowledge_not_covered_answer(question, None),
                    "rewritten_question": question if question != raw_question else None,
                    "schemes": [],
                    **_empty_data_fields()}
        if _kb_scheme is None and _is_ambiguous_focus(question):
            raise _focus_ambiguity_clarification(question)
        if _kb_scheme is None and _needs_scheme_clarification(question):
            raise _scheme_clarification(question)

        # Two or more schemes named outright ("how do I apply across MGNREGA,
        # PMAY-G, Focus Plus and CM Elevate") — retrieve each scheme's chunks
        # separately (see rag.answer_from_kb_multi) instead of one unscoped
        # search, which otherwise lets one scheme's passages crowd out another's.
        if len(_named) > 1:
            kb = await rag.answer_from_kb_multi(question, schemes=_named)
            base = {"rewritten_question": question if question != raw_question else None,
                    "schemes": _named}
        else:
            # "Give me a short overview of Focus Legacy for an official briefing":
            # the words "official"/"briefing" pulled the where-to-find-official-info
            # and institutional-structure chunks, so the answer was a list of
            # departments with no benefit amount, objective or scale (Focus Legacy
            # QA TC-09, 2026-09-25). Retrieve with the scheme-overview phrasing
            # instead; the answer is still written to the user's own question.
            _retrieval_q = (_scheme_overview_question(_kb_scheme)
                            if _kb_scheme == "Focus Legacy" and _OVERVIEW_REQUEST.search(question)
                            else None)
            if _retrieval_q:
                kb = await rag.answer_from_kb(question, scheme=_kb_scheme, retrieval_query=_retrieval_q)
            else:
                kb = await rag.answer_from_kb(question, scheme=_kb_scheme)
            base = {"rewritten_question": question if question != raw_question else None,
                    "schemes": [_kb_scheme] if _kb_scheme else []}
        if kb:
            return {"route": "knowledge", "intent": "RAG", "confidence": kb["confidence"],
                    "answer": kb["answer"], "sources": kb["sources"],
                    **_empty_data_fields(), **base}
        return {"route": "knowledge", "intent": "RAG", "confidence": "low", "sources": [],
                "answer": _knowledge_not_covered_answer(question, _kb_scheme),
                **_empty_data_fields(), **base}
    return None


async def _turn_data_context_stage(t: dict, session: "Session | None" = None,
                                   scope: "auth.UserScope | None" = None) -> None:
    """The filters a DATA follow-up inherits (graph: data_context)."""
    prev, is_followup_rewrite = t["prev"], t["is_followup_rewrite"]
    _thread_state, _plan = t["thread_state"], t["plan"]
    # 4. DATA -> NL->SQL. On a hard failure, try the KB once before giving up.
    # A rewritten follow-up's entities are seeded with the previous turn's
    # resolved district/block/village/year as a fallback (see resolve_entities'
    # `prior_resolved` param) — the LLM rewrite works from prev.question/answer
    # TEXT, so an entity can silently drop if it doesn't literally reappear in
    # the rewritten wording (e.g. a village name the rewrite paraphrases away).
    _prior_resolved = prev.resolved_entities if (is_followup_rewrite and prev is not None) else None
    if is_followup_rewrite and _thread_state is not None:
        # Extends the fallback above with session-level structured state, so a
        # KNOWLEDGE/EDGE turn sitting between the last DATA answer and this
        # follow-up (which leaves resolved_entities empty — see
        # context_manager.update_state) doesn't erase district/block/village/
        # year a later "and in 2023-24?" still needs. Turn-level prev values
        # still win where both are present.
        try:
            _prior_resolved = context_manager.merged_prior_resolved(_prior_resolved, _thread_state)
        except Exception:  # noqa: BLE001
            logger.warning("context_manager.merged_prior_resolved failed — using turn-level only",
                           exc_info=True)
    if is_followup_rewrite and _plan is not None:
        # Drop from the fallback any field this follow-up REPLACEs, CLEARs or
        # can't pin. Before this, "show it by district" after a West Garo Hills
        # turn still inherited the district filter (the LLM extractor finds no
        # district in "...by district"), and "what about South Garo Hills?"
        # after a Dalu turn kept block DALU under the new district.
        _prior_resolved = context_policy.apply_merge_plan(_prior_resolved, _plan)
    t["prior_resolved"] = _prior_resolved


# "what is <something> scheme/yojana/mission?" — the user named a specific
# programme by name. When it is none of our four and not in the known
# unsupported catalogue either, it is still a NAMED ask, not a vague one, so it
# must not get the "which scheme does your question concern?" pause.
_NAMED_UNKNOWN_SCHEME_RE = re.compile(
    r"\b(?:what|which|tell me about|explain|describe|about)\b[^?.!]{0,60}?"
    r"\b(?P<name>[A-Za-z][\w'-]*(?:\s+[A-Za-z][\w'-]*){0,3})\s+"
    r"(?:scheme|yojana|yojna|mission|abhiyaan?|programme|program)\b",
    re.IGNORECASE,
)
# Words that make the phrase generic rather than a name ("what is THIS scheme",
# "about the scheme"), so they must not count as naming one.
_GENERIC_SCHEME_WORDS = {
    "the", "this", "that", "a", "an", "any", "each", "every", "all", "these",
    "those", "your", "which", "what", "some", "other", "another", "such",
    "government", "govt", "state", "central", "rural", "welfare", "above",
    # Verbs / fillers the opener can leave in the captured span ("what IS the
    # scheme") — on their own they name nothing.
    "is", "are", "was", "were", "do", "does", "did", "me", "about", "of",
    "for", "in", "on", "it", "they", "you", "i", "we", "tell", "explain",
    # Generic nouns a scheme question asks ABOUT, never the scheme's name
    # ("what are the BENEFITS of the scheme").
    "benefit", "benefits", "eligibility", "criteria", "document", "documents",
    "purpose", "objective", "objectives", "feature", "features", "detail",
    "details", "rule", "rules", "process", "procedure", "amount", "subsidy",
}


def _names_unknown_scheme(question: str) -> bool:
    """True when the question names a specific scheme by name that is neither
    one of ours nor in the unsupported catalogue."""
    if _named_schemes(question) or _infer_scheme_from_terms(question):
        return False                       # one of ours — nothing unknown here
    # A bare "Focus" names one of OUR schemes — we just don't yet know which of
    # the two (see _is_ambiguous_focus). Without this, "what is the FOCUS
    # scheme?" matched the "<name> scheme" pattern below, found "focus" in
    # neither registry above, and was answered "that isn't a scheme I hold" —
    # for a scheme with a full reference doc and FAQ in the KB. It must fall
    # through to the which-Focus ask instead.
    if _is_ambiguous_focus(question):
        return False
    if _unsupported_scheme_named(question):
        return False                       # handled by its own, better reply
    m = _NAMED_UNKNOWN_SCHEME_RE.search(question or "")
    if not m:
        return False
    words = m.group("name").split()
    if not words:
        return False
    # The scheme's NAME is the word immediately before "scheme"/"yojana"/… —
    # "amma yedi scheme" names one, "the benefits of the scheme" does not.
    # Testing the last word alone (rather than the whole captured span) keeps
    # this from firing on any question that merely mentions a generic noun on
    # its way to the word "scheme".
    if words[-1].lower() in _GENERIC_SCHEME_WORDS:
        return False
    # A bare "<word> scheme" where that word is an ordinary English filler is
    # still generic; require something that reads like a proper name.
    return len(words[-1]) >= 3


def _knowledge_not_covered_answer(question: str, scheme: "str | None") -> str:
    """The reply when the knowledge base genuinely has nothing for a question.

    The old text — "I don't have information about that for MGNREGA, PMAY-G,
    Focus Plus or CM Elevate" — reads as a dead end: it never says whether the
    SUBJECT is out of scope or the assistant simply failed, and offers nowhere
    to go (reported 2026-09-18, "what is PM kisan yojana?"). A question that
    named a scheme gets a scheme-scoped answer; one that named none is told
    what IS covered."""
    if scheme:
        # Name the reference material actually searched — CM Elevate Legacy has
        # none of its own, it reads CM Elevate's (rag.kb_scheme). Identity for
        # every other scheme.
        return (
            f"I don't have that detail in the {rag.kb_scheme(scheme)} reference material. I can "
            f"cover {scheme}'s eligibility, benefits, documents and how to apply, "
            "and its data by district, block, village or financial year — so it may "
            "just be worth rephrasing. If you meant a different scheme, tell me which."
        )
    return (
        "That isn't something I hold. I cover five Meghalaya schemes — MGNREGA "
        "(rural employment), PMAY-G (rural housing), Focus Plus (farmer cash "
        "benefit), CM Elevate (livelihood and enterprise support — its applications, "
        "and as CM Elevate Legacy its sanctions and disbursements) and Focus Legacy "
        "(producer group payments) — both how "
        "each one works and its actual data. If your question is about one of "
        "those, name it and I'll answer; if it's about another scheme or another "
        "state, that's outside what I can see."
    )


async def _data_path_kb_fallback(question: str) -> dict:
    """The DATA path genuinely couldn't produce a query (bad/uncoverable question,
    not an infra blip) — try the knowledge base once, then give the 'rephrase it'
    reply. Transient gateway errors are handled by the caller and never reach here."""
    kb = await rag.answer_from_kb(question)
    if kb:
        return {"route": "knowledge", "intent": "RAG", "confidence": kb["confidence"],
                "answer": kb["answer"], "sources": kb["sources"], **_empty_data_fields()}
    # Name the scheme(s) actually in play, not a hardcoded pair — this message used
    # to always say "MGNREGA / PMAY-G data" even for a Focus Plus / CM Elevate
    # question, which reads as if the conversation's own context had been dropped
    # (it hadn't — this text just never grew past the original two-scheme build).
    _fallback_schemes = _named_schemes(question) or _infer_scheme_from_terms(question)
    _schemes_text = " / ".join(_fallback_schemes) if _fallback_schemes else " / ".join(SCHEME_CATALOG)
    return {"route": "data", "intent": "DATA", "confidence": "low",
            "answer": ("I understood the question but couldn't build a working query for it "
                       f"against the current {_schemes_text} data. Try rephrasing it, or ask "
                       "for a simpler breakdown first (e.g. \"PMAY-G sanctions by district "
                       "for 2023\")."),
            **_empty_data_fields()}
