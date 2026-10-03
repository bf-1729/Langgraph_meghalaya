"""
Short-lived chat session store for follow-up ("what about East Garo Hills?")
resolution.

In-process, per worker, everything expires — the same stance as cache.py and
semantic_cache.py. It holds only what a follow-up rewrite needs: the last few
turns (question + how it was answered) and the user's scope for the session, so
the authorization check doesn't have to rebuild it every turn.

Nothing here is authoritative and none of it survives a restart. A caller that
sends no session id just gets no follow-up context — the pipeline still works.
"""
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock

from app.config import settings


@dataclass
class Turn:
    question: str                    # the standalone question actually run
    raw_question: str                # what the user typed (may be a fragment)
    route: str                       # "data" | "knowledge" | "edge" | "denied"
    schemes: list[str] = field(default_factory=list)
    resolved_entities: dict = field(default_factory=dict)
    answer: str = ""
    # Deterministic one-line summary of this turn's result ROWS
    # (context_manager.summarize_result). This is the evidence a later
    # "what about the top one?" is resolved against. It replaced a fixed
    # character slice of `answer`, which could carry names the follow-up never
    # referred to. Empty for knowledge, edge and failed turns.
    result_summary: str = ""


@dataclass
class ConversationState:
    """Structured multi-turn state — the "what are we talking about" the
    context layer maintains alongside the raw turn list, so a follow-up like
    "what about 2023-24?" doesn't have to be re-derived from prose every time.

    Deliberately a plain, JSON-round-trippable bag of scalars/lists (see
    to_dict/from_dict) so it can be persisted on app.conversations.context_state
    (see conversation_store.save_context_state) and survive a worker restart —
    the in-process Session is L1, that JSONB column is L2, same split as the
    turn list vs app.conversation_turns.

    Nothing here is ever used for authorization — every SQL query is still
    re-authorized from scratch against the live scope (see auth.authorize);
    this only feeds question rewriting / entity hints.
    """
    scheme: str | None = None                    # single active scheme, if pinned
    district: str | None = None
    block: str | None = None
    village: str | None = None
    year: int | None = None                       # year_key, e.g. 2024 for FY2024-25
    previous_year: int | None = None
    metric: str | None = None                     # last metric keyword the user asked about
    tranche: str | None = None                    # single pinned Focus Plus tranche_label, if any
    tranche_all_combined: bool = False            # True once "all tranches combined" was chosen
    # True once a DATA turn ran over all financial years ("across all financial
    # years", the "all years combined" chip) with no year of its own. A
    # resolved time scope like `year`, carried into follow-ups that keep the
    # year (context_manager.inject_year_scope); cleared by a named year (KI-030).
    year_all: bool = False
    # The dimension the last follow-up changed ("year", "district", "block"),
    # so "all of them combined" after "what about 2025-26?" means all YEARS
    # (context_policy.plan_state_merge). None after a question of its own.
    last_dimension: str | None = None
    comparison_entities: list[str] = field(default_factory=list)  # for "the former/latter/other one"
    comparison_kind: str | None = None            # "district" | "block" | "scheme" | "year"
    last_intent: str | None = None                # "DATA" | "KNOWLEDGE" | "EDGE" | "CLARIFY"
    last_route: str | None = None
    last_question: str | None = None              # raw text as typed
    last_standalone_question: str | None = None   # fully resolved/rewritten form
    turn_count: int = 0
    # Where each committed field's value came from — {field: {"value", "source",
    # "confidence", "turn"}}. `source` is one of context_manager.PROVENANCE_SOURCES.
    # merged_prior_resolved refuses to inherit a value whose source is
    # "model_inference": a value no user turn, pause reply or resolved reference
    # ever supplied is not carried into the next question.
    provenance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "scheme": self.scheme, "district": self.district, "block": self.block,
            "village": self.village, "year": self.year, "previous_year": self.previous_year,
            "metric": self.metric, "tranche": self.tranche,
            "tranche_all_combined": self.tranche_all_combined,
            "year_all": self.year_all,
            "last_dimension": self.last_dimension,
            "comparison_entities": list(self.comparison_entities),
            "comparison_kind": self.comparison_kind, "last_intent": self.last_intent,
            "last_route": self.last_route, "last_question": self.last_question,
            "last_standalone_question": self.last_standalone_question,
            "turn_count": self.turn_count,
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "ConversationState":
        d = d or {}
        return cls(
            scheme=d.get("scheme"), district=d.get("district"), block=d.get("block"),
            village=d.get("village"), year=d.get("year"), previous_year=d.get("previous_year"),
            metric=d.get("metric"), tranche=d.get("tranche"),
            tranche_all_combined=bool(d.get("tranche_all_combined")),
            year_all=bool(d.get("year_all")),
            last_dimension=d.get("last_dimension"),
            comparison_entities=list(d.get("comparison_entities") or []),
            comparison_kind=d.get("comparison_kind"), last_intent=d.get("last_intent"),
            last_route=d.get("last_route"), last_question=d.get("last_question"),
            last_standalone_question=d.get("last_standalone_question"),
            turn_count=int(d.get("turn_count") or 0),
            provenance=dict(d.get("provenance") or {}),
        )


@dataclass
class Session:
    session_id: str
    user_id: str | None
    created: float
    last_seen: float
    turns: list[Turn] = field(default_factory=list)
    scope: object | None = None      # backend.auth.UserScope, cached for the session
    # Set when the pipeline paused to ask "which area / year?" (scope-not-specified).
    # Holds the original question so the next turn's free-text reply can be merged
    # back into it. Cleared as soon as it's consumed. Not persisted, per-worker.
    pending_scope_q: str | None = None
    # Set alongside pending_scope_q when the pause was a village-name ambiguity
    # ("entity-ambiguous"). Holds the exact text the user typed for the village
    # (e.g. "Adugre") so the resume can re-run resolve_village on it directly —
    # the merged reply text ("...the one in Betasing block") does NOT reliably
    # make the LLM mention-extractor re-tag the village on the merged sentence,
    # which otherwise leaves village_code unresolved and lets SQL generation
    # invent one instead of asking again or using the block. Cleared with
    # pending_scope_q.
    pending_village_hint: str | None = None
    # Which clarification rule set pending_scope_q, plus that pause's one-tap
    # options. The "which scheme?" / "which Focus?" pauses resume differently
    # from the scope pauses: a typed scheme name is mapped onto the chip the user
    # could have clicked (pipeline._resume_scheme_pause), and anything else
    # goes through unchanged, exactly as before those pauses were remembered.
    # Cleared with pending_scope_q.
    pending_scope_rule: str | None = None
    pending_scope_options: list | None = None
    # Structured conversation state (app/context_manager.py) — the L1 copy,
    # mirrored to app.conversations.context_state (L2) on each turn.
    state: ConversationState = field(default_factory=ConversationState)
    # Compact rolling summary of the conversation so far (schemes/locations/
    # years/metrics/comparisons/unresolved references) — see
    # context_manager.maybe_update_summary. None until the turn count first
    # crosses CONTEXT_SUMMARY_EVERY_N_TURNS.
    summary: str | None = None
    summary_turn_count: int = 0   # turn_count as of the last summary update

    # Revision of the durable snapshot this in-process copy was last synced
    # with (app/session_sync.py). Every successful save increments it, so a
    # worker can tell whether its own L1 copy is current or stale.
    rev: int = 0
    # Per-request facts the pipeline hands to context_manager.update_state for
    # provenance (the state before this turn, the question after reference
    # substitution, the pause reply, the merge plan). Reset every request and
    # never persisted.
    turn_context: dict = field(default_factory=dict)

    @property
    def last_turn(self) -> Turn | None:
        return self.turns[-1] if self.turns else None

    # ── Durable snapshot (KI-028) ────────────────────────────────────────────
    # Everything the NEXT request needs, on whichever worker it lands: the
    # structured state, the previous turn a follow-up is rewritten against, the
    # pending clarification a reply resumes, and the recent questions the
    # rolling summary is built from. Stored in app.conversations.context_state
    # (JSONB) next to the state fields it already held, so a record written
    # before this existed still loads (every extra key defaults to empty).
    _SNAPSHOT_ANSWER_CHARS = 2000   # the answer is only ever used as a sentence-bounded Tier-3 excerpt

    def to_snapshot(self) -> dict:
        last = self.last_turn
        snap = self.state.to_dict()
        snap["_session"] = {
            "last_turn": None if last is None else {
                "question": last.question, "raw_question": last.raw_question,
                "route": last.route, "schemes": list(last.schemes),
                "resolved_entities": dict(last.resolved_entities),
                "answer": (last.answer or "")[: self._SNAPSHOT_ANSWER_CHARS],
                "result_summary": last.result_summary,
            },
            "pending": None if not self.pending_scope_q else {
                "question": self.pending_scope_q,
                "village_hint": self.pending_village_hint,
                "rule": self.pending_scope_rule,
                "options": list(self.pending_scope_options or []) or None,
            },
            "recent_questions": [t.question for t in self.turns[-8:] if t.question],
        }
        return snap

    def apply_snapshot(self, snap: dict | None, summary: str | None = None) -> None:
        """Replace this in-process copy with a durable snapshot. The last turn
        becomes the only turn held (older turns only ever fed the summary,
        which is restored alongside); questions before it are kept as
        question-only turns so the summary can still be rebuilt."""
        snap = snap or {}
        self.state = ConversationState.from_dict(snap)
        extra = snap.get("_session") or {}
        turns: list[Turn] = [Turn(question=q, raw_question=q, route="history")
                             for q in (extra.get("recent_questions") or [])[:-1]]
        last = extra.get("last_turn")
        if last:
            turns.append(Turn(
                question=last.get("question") or "", raw_question=last.get("raw_question") or "",
                route=last.get("route") or "data", schemes=list(last.get("schemes") or []),
                resolved_entities=dict(last.get("resolved_entities") or {}),
                answer=last.get("answer") or "", result_summary=last.get("result_summary") or ""))
        self.turns = turns
        pending = extra.get("pending") or {}
        self.pending_scope_q = pending.get("question")
        self.pending_village_hint = pending.get("village_hint")
        self.pending_scope_rule = pending.get("rule")
        self.pending_scope_options = pending.get("options")
        if summary is not None:
            self.summary = summary
            self.summary_turn_count = self.state.turn_count


class SessionStore:
    def __init__(self, ttl: float, max_sessions: int, max_turns: int):
        self.ttl = ttl
        self.max_sessions = max_sessions
        self.max_turns = max_turns
        self._store: "OrderedDict[str, Session]" = OrderedDict()
        self._lock = Lock()

    def _expired(self, s: Session, now: float) -> bool:
        return now - s.last_seen > self.ttl

    def get(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        now = time.monotonic()
        with self._lock:
            s = self._store.get(session_id)
            if s is None:
                return None
            if self._expired(s, now):
                del self._store[session_id]
                return None
            s.last_seen = now
            self._store.move_to_end(session_id)
            return s

    def ensure(self, session_id: str | None, user_id: str | None) -> Session:
        """Return the live session for this id, creating it if needed. A blank id
        gets a fresh random one (single-turn — the caller just won't send it back)."""
        now = time.monotonic()
        sid = session_id or f"anon-{uuid.uuid4().hex[:16]}"
        with self._lock:
            s = self._store.get(sid)
            if s is not None and not self._expired(s, now):
                s.last_seen = now
                if user_id and not s.user_id:
                    s.user_id = user_id
                self._store.move_to_end(sid)
                return s
            s = Session(session_id=sid, user_id=user_id, created=now, last_seen=now)
            self._store[sid] = s
            self._store.move_to_end(sid)
            while len(self._store) > self.max_sessions:
                self._store.popitem(last=False)
            return s

    def add_turn(self, session_id: str, turn: Turn) -> None:
        with self._lock:
            s = self._store.get(session_id)
            if s is None:
                return
            s.turns.append(turn)
            if len(s.turns) > self.max_turns:
                s.turns = s.turns[-self.max_turns :]
            s.last_seen = time.monotonic()

    def stats(self) -> dict:
        now = time.monotonic()
        with self._lock:
            live = sum(1 for s in self._store.values() if not self._expired(s, now))
            return {"sessions": len(self._store), "live": live}


session_store = SessionStore(
    ttl=settings.SESSION_TTL_SECONDS,
    max_sessions=settings.SESSION_MAX,
    max_turns=settings.SESSION_MAX_TURNS,
)
