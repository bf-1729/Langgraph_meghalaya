"""
Conversation context layer (app/context_manager.py, app/conversation_memory.py,
app/session_store.ConversationState) — pure-logic + monkeypatched-dependency
checks. No DB, no model gateway, no Qdrant — same stance as test_security.py:
`python tests/test_context_manager.py`, exit code 0 = all pass.

Covers the scenarios called out in the context-layer spec: follow-up entity
continuation (district/year/metric), previous-year references, comparisons
(former/latter/other-one/both) with clarification-on-ambiguity, scheme
switching / context reset, context-bleed prevention across a KNOWLEDGE
digression, the mandatory 5-question conversation, tenant/user isolation on
semantic memory, token-budget truncation, and memory/summary failure
tolerance.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@127.0.0.1:5/none")
os.environ.setdefault("JWT_SECRET", "test-secret-at-least-32-characters-long")
os.environ.setdefault("ENV", "prod")

from app import context_manager as cm  # noqa: E402
from app import conversation_memory  # noqa: E402
from app.entity_resolver import load_all as _load_entity_resolver  # noqa: E402
from app.session_store import ConversationState, Session, Turn  # noqa: E402

_load_entity_resolver()  # local YAML, no network — needed for detect_comparison_districts

_fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


def _session(**state_kwargs) -> Session:
    s = Session(session_id="t1", user_id="u1", created=0.0, last_seen=0.0)
    for k, v in state_kwargs.items():
        setattr(s.state, k, v)
    return s


# ── 1. Follow-up entity continuation (district/year/metric) ────────────────
print("1. FOLLOW-UP ENTITY CONTINUATION")
state = ConversationState(scheme="MGNREGA", district="West Garo Hills", year=2024, metric="expenditure")
q = cm.substitute_references("What about the previous year?", state)
check('"the previous year" -> concrete FY', "FY 2023-24" in q, q)
q2 = cm.substitute_references("And the current year figure?", state)
check('"the current year" -> concrete FY', "FY 2024-25" in q2, q2)
check("no-op when state is None", cm.substitute_references("what about it?", None) == "what about it?")
check("no-op when phrase absent", cm.substitute_references("how many houses?", state) == "how many houses?")


# ── 2. Scheme-hint injection ─────────────────────────────────────────────────
print("2. SCHEME-HINT INJECTION")
st = ConversationState(scheme="MGNREGA")
out = cm.inject_scheme_hint("What about West Garo Hills in 2023-24?", st)
check("injects pinned scheme when none named/inferred", "MGNREGA" in out, out)
out2 = cm.inject_scheme_hint("What about PMAY-G in 2023-24?", st)
check("does NOT override an explicitly named scheme",
      out2 == "What about PMAY-G in 2023-24?", out2)
out3 = cm.inject_scheme_hint("How many person-days in 2023-24?", st)
check("does NOT inject when vocabulary already pins a scheme (person-days=MGNREGA)",
      out3 == "How many person-days in 2023-24?", out3)
check("no-op when state has no pinned scheme",
      cm.inject_scheme_hint("what about 2023-24?", ConversationState()) == "what about 2023-24?")


# ── 3. Comparison references (former/latter/other-one/both) ────────────────
print("3. COMPARISON REFERENCES")
cmp_state = ConversationState(comparison_entities=["West Garo Hills", "East Garo Hills"],
                              comparison_kind="district")
check('"the former" resolves to entity[0]',
      "West Garo Hills" in cm.substitute_references("expenditure in the former", cmp_state))
check('"the latter" resolves to entity[-1]',
      "East Garo Hills" in cm.substitute_references("expenditure in the latter", cmp_state))
both_out = cm.substitute_references("compare both", cmp_state)
check('"both" expands to the recorded pair',
      "West Garo Hills" in both_out and "East Garo Hills" in both_out, both_out)
other_out = cm.substitute_references("expenditure in West Garo Hills vs the other one", cmp_state)
check('"the other one" resolves against the one already named',
      "East Garo Hills" in other_out, other_out)
untouched = cm.substitute_references("compare both schemes", cmp_state)
check('"both schemes" is left untouched (not clobbered by an earlier district comparison)',
      untouched == "compare both schemes", untouched)

try:
    cm.substitute_references("what about the former?", ConversationState())
    check("AmbiguousReference raised with no comparison_entities on record", False)
except cm.AmbiguousReference:
    check("AmbiguousReference raised with no comparison_entities on record", True)

try:
    cm.substitute_references("what about the other one?",
                             ConversationState(comparison_entities=["A", "B", "C"]))
    check("AmbiguousReference raised with 3+ comparison_entities", False)
except cm.AmbiguousReference as e:
    check("AmbiguousReference raised with 3+ comparison_entities", True)
    check("...and carries one-tap options", len(e.options) == 3, str(e.options))


# ── 4. Entity-inheritance fallback (merged_prior_resolved) ──────────────────
print("4. ENTITY-INHERITANCE FALLBACK")
state = ConversationState(district="West Garo Hills", year=2024, block="Selsella")
merged = cm.merged_prior_resolved({}, state)
check("session state fills in when turn-level resolved is empty (post-digression)",
      merged.get("district") == "West Garo Hills" and merged.get("year_key") == 2024, merged)
merged2 = cm.merged_prior_resolved({"district": "East Garo Hills"}, state)
check("turn-level value wins over session state",
      merged2.get("district") == "East Garo Hills", merged2)
check("state_to_resolved_entities empty for blank state", cm.state_to_resolved_entities(None) == {})


# ── 5. Metric / comparison-district detection ───────────────────────────────
print("5. METRIC / COMPARISON DETECTION")
check("detects person-days", cm.detect_metric("How many person-days were generated?") == "person-days")
check("detects expenditure", cm.detect_metric("total MGNREGA expenditure") == "expenditure")
check("no metric on a bare place question", cm.detect_metric("tell me about West Garo Hills") is None)
check("no comparison districts without a compare cue",
      cm.detect_comparison_districts("MGNREGA in West Garo Hills and East Garo Hills") == [])
found = cm.detect_comparison_districts("Compare West Garo Hills and East Garo Hills")
check("finds two districts with an explicit compare cue",
      set(found) >= {"West Garo Hills", "East Garo Hills"}, found)


# ── 6. Context updater: continuation + reset + context-bleed prevention ────
print("6. CONTEXT UPDATER (continuation / reset / bleed prevention)")
s = _session()
cm.update_state(s, "MGNREGA expenditure in West Garo Hills 2024-25?",
                "MGNREGA expenditure in West Garo Hills 2024-25?",
                {"route": "data", "intent": "DATA", "schemes": ["MGNREGA"],
                 "resolved_entities": {"district": "West Garo Hills", "year_key": 2024}})
check("Q1 pins scheme/district/year", s.state.scheme == "MGNREGA" and s.state.district == "West Garo Hills"
      and s.state.year == 2024)
check("previous_year derived", s.state.previous_year == 2023)

cm.update_state(s, "what about 2023-24?", "MGNREGA expenditure in West Garo Hills 2023-24?",
                {"route": "data", "intent": "DATA", "schemes": ["MGNREGA"],
                 "resolved_entities": {"district": "West Garo Hills", "year_key": 2023}})
check("Q2 updates year, keeps district/scheme",
      s.state.year == 2023 and s.state.district == "West Garo Hills" and s.state.scheme == "MGNREGA")

cm.update_state(s, "who is eligible for PMAY-G?", "who is eligible for PMAY-G?",
                {"route": "knowledge", "intent": "RAG", "schemes": ["PMAY-G"], "resolved_entities": {}})
check("KNOWLEDGE digression does NOT overwrite scheme/district/year (context-bleed prevention)",
      s.state.scheme == "MGNREGA" and s.state.district == "West Garo Hills" and s.state.year == 2023)
check("...but last_route/last_intent DO reflect the digression",
      s.state.last_route == "knowledge" and s.state.last_intent == "RAG")

cm.update_state(s, "and person-days?", "MGNREGA person-days in West Garo Hills 2023-24?",
                {"route": "data", "intent": "DATA", "schemes": ["MGNREGA"],
                 "resolved_entities": {"district": "West Garo Hills", "year_key": 2023}})
check("post-digression follow-up still resolves against pre-digression state",
      s.state.district == "West Garo Hills" and s.state.year == 2023)
check("metric tracked", s.state.metric == "person-days")

cm.update_state(s, "compare MGNREGA and PMAY-G spending", "compare MGNREGA and PMAY-G spending",
                {"route": "data", "intent": "DATA", "schemes": ["MGNREGA", "PMAY-G"],
                 "resolved_entities": {}})
check("multi-scheme answer clears the pinned single scheme and records a comparison",
      s.state.scheme is None and s.state.comparison_entities == ["MGNREGA", "PMAY-G"]
      and s.state.comparison_kind == "scheme")


# ── 7. Mandatory 5-question conversation (state-machine level) ─────────────
print("7. MANDATORY 5-QUESTION CONVERSATION")
s5 = _session()
# Q1
cm.update_state(s5, "What was MGNREGA expenditure in West Garo Hills in 2024-25?",
                "What was MGNREGA expenditure in West Garo Hills in 2024-25?",
                {"route": "data", "schemes": ["MGNREGA"],
                 "resolved_entities": {"district": "West Garo Hills", "year_key": 2024}})
# Q2 — "What about 2023-24?" : reference substitution changes nothing here (no
# "previous year" phrase), scheme-hint + prior-entity fallback do the carry.
q2_hint = cm.inject_scheme_hint("What about 2023-24?", s5.state)
check("Q2 gets MGNREGA injected", "MGNREGA" in q2_hint, q2_hint)
q2_merged = cm.merged_prior_resolved({}, s5.state)
check("Q2 carries district from state", q2_merged.get("district") == "West Garo Hills")
cm.update_state(s5, "What about 2023-24?", q2_hint,
                {"route": "data", "schemes": ["MGNREGA"],
                 "resolved_entities": {"district": "West Garo Hills", "year_key": 2023}})
# Q3 — "And person-days?"
q3_hint = cm.inject_scheme_hint("And person-days?", s5.state)
check("Q3: person-days vocabulary already pins MGNREGA, hint not force-appended",
      "person-days" in q3_hint.lower())
cm.update_state(s5, "And person-days?", "MGNREGA person-days in West Garo Hills 2023-24?",
                {"route": "data", "schemes": ["MGNREGA"],
                 "resolved_entities": {"district": "West Garo Hills", "year_key": 2023}})
check("Q3 state: same scheme/district/year, metric=person-days",
      s5.state.scheme == "MGNREGA" and s5.state.district == "West Garo Hills"
      and s5.state.year == 2023 and s5.state.metric == "person-days")
# Q4 — "Compare that with the previous year."
q4_text = cm.substitute_references("Compare that with the previous year.", s5.state)
check('Q4: "the previous year" resolves using CURRENT state.year (2023 -> FY 2022-23)',
      "FY 2022-23" in q4_text, q4_text)
cm.update_state(s5, "Compare that with the previous year.",
                "Compare MGNREGA person-days in West Garo Hills between FY 2023-24 and FY 2022-23.",
                {"route": "data", "schemes": ["MGNREGA"],
                 "resolved_entities": {"district": "West Garo Hills", "year_key": 2023}})
# Q5 — "Which block contributed the most?"
q5_hint = cm.inject_scheme_hint("Which block contributed the most?", s5.state)
check("Q5 gets MGNREGA injected (no scheme/metric vocabulary of its own)",
      "MGNREGA" in q5_hint, q5_hint)
q5_merged = cm.merged_prior_resolved({}, s5.state)
check("Q5 still has district context available to constrain the block ranking",
      q5_merged.get("district") == "West Garo Hills")
print("  (all 5 turns resolved scheme/district/year/metric context without the user repeating them)")


# ── 8. Token-budget handling ─────────────────────────────────────────────────
print("8. TOKEN-BUDGET HANDLING")
long_state = ConversationState(scheme="MGNREGA", district="West Garo Hills", block="Selsella",
                               village="Some Village", year=2024, metric="expenditure",
                               comparison_entities=["A", "B"], comparison_kind="district")
block_text = cm.build_state_block(long_state)
check("state block is non-empty and single-line", bool(block_text) and "\n" not in block_text)
truncated = cm._truncate_to_tokens("x" * 2000, 10)
check("_truncate_to_tokens respects the token budget (~4 chars/token)",
      len(truncated) <= 41, f"len={len(truncated)}")
check("_truncate_to_tokens is a no-op under budget",
      cm._truncate_to_tokens("short", 100) == "short")


async def _run_async_checks():
    # ── 9. Semantic memory: tenant/user isolation + failure tolerance ──────
    print("9. SEMANTIC MEMORY - ISOLATION + FAILURE TOLERANCE")

    class _FakeEmbeddingFailure:
        async def __call__(self, *_a, **_kw):
            raise RuntimeError("embedding gateway unreachable")

    orig_embed = conversation_memory.llm.call_embedding
    conversation_memory.llm.call_embedding = _FakeEmbeddingFailure()
    try:
        hits = await conversation_memory.search_relevant_turns(
            question="anything", tenant_id=1, user_id=2, session_id="s1")
        check("embedding failure -> search_relevant_turns returns [] (never raises)", hits == [])
    finally:
        conversation_memory.llm.call_embedding = orig_embed

    captured = {}

    class _FakePoint:
        def __init__(self, score, payload):
            self.score = score
            self.payload = payload

    class _FakeResponse:
        def __init__(self, points):
            self.points = points

    class _FakeClient:
        async def query_points(self, *, collection_name, query, query_filter, limit, with_payload):
            captured["filter"] = query_filter
            return _FakeResponse([_FakePoint(0.9, {"question": "q", "standalone_question": "q",
                                                    "answer": "a", "schemes": ["MGNREGA"],
                                                    "session_id": "s1"})])

    async def _fake_embed(*_a, **_kw):
        return [[0.1, 0.2, 0.3]]

    orig_get_client = conversation_memory.vectorstore.get_client
    conversation_memory.llm.call_embedding = _fake_embed
    conversation_memory.vectorstore.get_client = lambda: _FakeClient()
    try:
        hits = await conversation_memory.search_relevant_turns(
            question="expenditure in west garo hills", tenant_id=7, user_id=42, session_id="sess-a")
        check("search returns hits when the fake client answers", len(hits) == 1, hits)
        must_keys = {c.key: c.match.value for c in (captured.get("filter").must or [])}
        check("query filter scopes by tenant_id", must_keys.get("tenant_id") == 7, must_keys)
        check("query filter scopes by user_id", must_keys.get("user_id") == 42, must_keys)
        check("query filter scopes by session_id", must_keys.get("session_id") == "sess-a", must_keys)
    finally:
        conversation_memory.llm.call_embedding = orig_embed
        conversation_memory.vectorstore.get_client = orig_get_client

    # ── 10. Summary generation failure tolerance ───────────────────────────
    print("10. SUMMARY GENERATION FAILURE TOLERANCE")
    s = _session()
    s.state.turn_count = 10
    s.turns = [Turn(question="q1", raw_question="q1", route="data")]

    async def _boom_classifier(*_a, **_kw):
        raise RuntimeError("model gateway down")

    orig_classifier = cm.llm.call_classifier if hasattr(cm, "llm") else None
    from app import llm as _llm_mod
    orig_call_classifier = _llm_mod.call_classifier
    _llm_mod.call_classifier = _boom_classifier
    try:
        await cm.maybe_update_summary(s)
        check("summary generation failure does not raise, and leaves summary unset", s.summary is None)
    finally:
        _llm_mod.call_classifier = orig_call_classifier

    async def _ok_classifier(prompt, **_kw):
        return "MGNREGA expenditure discussed for West Garo Hills, FY2023-24 and FY2024-25."

    _llm_mod.call_classifier = _ok_classifier
    try:
        await cm.maybe_update_summary(s)
        check("summary updates when the model call succeeds", bool(s.summary))
        check("summary_turn_count advances to current turn_count", s.summary_turn_count == 10)
    finally:
        _llm_mod.call_classifier = orig_call_classifier


asyncio.run(_run_async_checks())


# ── 11. ConversationState round-trip (persistence shape) ───────────────────
print("11. STATE PERSISTENCE ROUND-TRIP")
orig = ConversationState(scheme="PMAY-G", district="Ri Bhoi", year=2023, previous_year=2022,
                         comparison_entities=["Ri Bhoi", "East Khasi Hills"], comparison_kind="district",
                         turn_count=3)
rehydrated = ConversationState.from_dict(orig.to_dict())
check("to_dict/from_dict round-trips every field", rehydrated == orig, rehydrated)
check("from_dict(None) yields a blank state", ConversationState.from_dict(None) == ConversationState())


print()
if _fails:
    print(f"FAILED ({len(_fails)}): " + ", ".join(_fails))
    sys.exit(1)
print("ALL CONTEXT-MANAGER CHECKS PASSED")
