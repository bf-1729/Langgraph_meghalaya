"""
RAG path — answers general scheme-knowledge questions from the data/
narrative docs (see kb_ingest.py), never from the database.

Flow (the NeuralAiGovernanceProject standard, adapted to Qdrant + qwen3):
  1. embed the question           -> qwen3-embedding
  2. vector search the KB         -> Qdrant, RAG_TOP_K candidates
  3. rerank the candidates        -> qwen3-reranker, keep RAG_RERANK_TOP_N
  4. confidence tiers:
       >= RAG_HIGH_CONFIDENCE   -> return the top chunk verbatim (no compose call)
       >= RAG_MEDIUM_CONFIDENCE -> compose an answer from the kept chunks
       below                    -> return None; the pipeline says "not in the
                                   scheme reference material"
"""
import logging
import re

from app import context_budget, llm, vectorstore
from app.config import settings

logger = logging.getLogger(__name__)

# The KB docs are Markdown, but the chat UI renders answers as plain text, so
# raw "### " heading markers leak through and look unprofessional. Drop the
# leading hashes and keep the heading text as its own line.
_MD_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}[ \t]+")

# Matches the exact refusal sentence the compose prompt is told to use (see
# answer_from_kb below), with or without the leading "That" — the composer
# sometimes tacks this on as a trailing hedge after an otherwise complete
# answer, and only that sentence should be stripped, not the whole response.
_REFUSAL_SENTENCE = re.compile(
    r"(?:that\s+)?isn'?t covered in the (?:scheme )?reference material\.?",
    re.IGNORECASE,
)

# Safety net for when the composer ignores the instructed refusal sentence
# above and free-forms its own — seen in the wild as "The provided reference
# material does not contain a specific status breakdown for X.", which
# _REFUSAL_SENTENCE doesn't match, so it leaked straight to the user instead
# of falling back to the pipeline's plain "I don't have information..."
# message. Any answer matching this is treated as a full refusal (return
# None), the same as one that's empty after _REFUSAL_SENTENCE stripping.
_LIKELY_REFUSAL = re.compile(
    r"reference material|do(?:es)?n'?t (?:contain|have|cover|include)|"
    r"not (?:covered|available)|no information (?:is )?available",
    re.IGNORECASE,
)


def _is_whole_refusal(text: str) -> bool:
    """True when every sentence in `text` reads as a refusal, even though it
    doesn't match the exact instructed template (_REFUSAL_SENTENCE) — the
    composer sometimes free-forms its own wording instead of the requested
    sentence. A partial caveat inside an otherwise substantive answer (e.g.
    one sentence noting a scheme "doesn't cover private land") has other,
    non-matching sentences alongside it and is left alone."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]
    return bool(sentences) and all(_LIKELY_REFUSAL.search(s) for s in sentences)

# The general_faq.md sources are flat "### Question?\nAnswer." pairs (see
# kb_ingest._chunk), so a chunk's own body opens by restating the question as
# its heading. The high-confidence path below returns that body verbatim, and
# without this the chat bubble reads as a literal transcript — "What documents
# are needed for X? Typically..." — instead of a natural answer. Drop that
# leading heading line whenever it's phrased as a question (ends in "?");
# non-FAQ section headings (e.g. "### Eligibility Criteria") don't match and
# are left as useful context.
_LEADING_QUESTION_HEADING = re.compile(r"\A#{1,6}[ \t]+.*\?[ \t]*\n+")


def _clean_for_display(text: str) -> str:
    return _MD_HEADING.sub("", text).strip()


def _strip_heading(text: str) -> str:
    # Chunks are stored as "[heading path]\n<body>" — drop the tag for display.
    if text.startswith("[") and "]\n" in text:
        text = text.split("]\n", 1)[1]
    text = _LEADING_QUESTION_HEADING.sub("", text, count=1)
    return _clean_for_display(text)


# How each scheme is actually WRITTEN in its own reference docs, where that
# differs from the label this system uses internally. Only Focus Legacy truly
# needs it — "Focus Legacy" is our own disambiguation label (coined to separate
# the producer-group scheme from Focus Plus) and appears NOWHERE in the SME
# prose, which says "FOCUS" throughout. Told to answer only from the passages,
# the composer then reports the scheme as "not mentioned" while sitting on the
# right material. The others are listed so the note reads naturally for them
# too, and so a future doc that prefers one spelling does not regress.
_SCHEME_DOC_NAMES = {
    "Focus Legacy": ("FOCUS", "the FOCUS scheme (Farmers' Collectivization for "
                     "Upscaling Production and Marketing Systems), the Meghalaya "
                     "producer-group scheme"),
    "Focus Plus": ("FOCUS+", "FOCUS+, the Meghalaya farmer cash-benefit scheme"),
    "CM Elevate": ("CM-ELEVATE", "CM-ELEVATE"),
    "CM Elevate Legacy": ("CM-ELEVATE", "CM-ELEVATE, the Meghalaya programme — CM "
                          "Elevate Legacy is its sanction and disbursement records and "
                          "shares the programme's reference material with CM Elevate"),
    "MGNREGA": ("MGNREGA", "MGNREGA"),
    "PMAY-G": ("PMAY-G", "PMAY-G / PMAY-Gramin"),
}


def _naming_note(scheme: str | None) -> str:
    """One line telling the composer that the passages ARE this scheme's own
    material, under whatever name the documents happen to use. Empty when the
    call is unscoped — we cannot assert whose material it is then."""
    if not scheme:
        return ""
    doc_name, description = _SCHEME_DOC_NAMES.get(scheme, (scheme, scheme))
    note = (
        f'The passages below are the reference material for {scheme}. '
        f'In those documents this scheme is written as "{doc_name}" '
        f'({description}). '
        f'"{scheme}" and "{doc_name}" are the SAME scheme — never say {scheme} '
        f'is not mentioned, or that the documents describe a different scheme, '
        f'merely because the passages use the other spelling.'
    )
    return note + "\n\n"


# Schemes that are different DATASETS of one programme share ONE knowledge base.
# CM Elevate (applications) and CM Elevate Legacy (sanctions and disbursements)
# are both the CM-ELEVATE programme: its eligibility, benefits, sub-schemes and
# application process are the same whichever dataset a user is looking at, so
# both read the same reference docs — stored under "CM Elevate" (kb_ingest folds
# a Legacy-tagged doc into that tag too). The vector filter is an exact match,
# so every retrieval goes through kb_scheme(). Every other scheme maps to itself.
_KB_SCHEME_ALIAS = {"CM Elevate Legacy": "CM Elevate"}


def kb_scheme(scheme: str | None) -> str | None:
    """The knowledge-base tag a scheme's reference material is stored under."""
    return _KB_SCHEME_ALIAS.get(scheme, scheme) if scheme else scheme


async def retrieve(question: str, scheme: str | None = None) -> list[dict]:
    """Return the reranked, score-filtered candidate chunks, best first.
    `scheme`, when given, restricts retrieval to that scheme's chunks."""
    try:
        vecs = await llm.call_embedding(question)
    except Exception as e:  # noqa: BLE001
        logger.warning("rag.retrieve: embedding failed — %s", e)
        return []
    candidates = await vectorstore.search(vecs[0], top_k=settings.RAG_TOP_K,
                                          scheme=kb_scheme(scheme))
    if not candidates:
        return []

    # Reranker is opt-in — the deployed qwen3-reranker inverts relevance on this
    # KB, so by default we trust the vector order (bge-small) directly.
    if not settings.RERANKER_ENABLED:
        return [c for c in candidates[: settings.RAG_RERANK_TOP_N]
                if c.get("score", 0.0) >= settings.RAG_MIN_SCORE] or candidates[: settings.RAG_RERANK_TOP_N]

    order = await llm.call_reranker(question, [c["text"] for c in candidates])
    reranked: list[dict] = []
    for idx, score in order[: settings.RAG_RERANK_TOP_N]:
        if 0 <= idx < len(candidates) and score >= settings.RAG_MIN_SCORE:
            c = dict(candidates[idx])
            c["score"] = score
            reranked.append(c)
    # If the reranker filtered everything out, fall back to the raw vector order.
    return reranked or candidates[: settings.RAG_RERANK_TOP_N]


async def answer_from_kb(question: str, scheme: str | None = None,
                         retrieval_query: str | None = None) -> dict | None:
    """Answer a scheme-knowledge question, or None if the KB doesn't cover it.
    `retrieval_query`, when given, is embedded for retrieval in place of the
    question (the answer is still written to `question`)."""
    chunks = await retrieve(retrieval_query or question, scheme=scheme)
    if not chunks:
        return None

    top = chunks[0]
    top_score = float(top.get("score", 0.0))
    sources = [{"doc": c["doc"], "heading": c["heading"],
                "source_type": c.get("source_type", "sme")} for c in chunks]

    if top_score >= settings.RAG_HIGH_CONFIDENCE:
        return {
            "answer": _strip_heading(top["text"]),
            "confidence": "high",
            "sources": sources[:1],
        }

    if top_score >= settings.RAG_MEDIUM_CONFIDENCE:
        context = "\n\n---\n\n".join(_strip_heading(c["text"]) for c in chunks)
        prompt = f"""{_naming_note(scheme)}Answer the question using ONLY the reference passages below. If they do
not contain the answer, say "That isn't covered in the scheme reference material."
Do not invent numbers, dates, or amounts. No Markdown headings ("#", "##", "###").

Formatting — this is read in a chat bubble, so structure it for a quick scan:
- If the answer is a single fact, give it in one to two plain sentences.
- If the answer lists two or more items (schemes, sectors, steps, documents,
  eligibility conditions, etc.), use one short bullet per item on its own line,
  formatted exactly as "- **Item name** — one-line description", never a
  paragraph that runs items together in prose.
- Bold (**text**) only the item name being defined in each bullet, nothing else.
- Keep each bullet to one line. Put a blank line between an intro sentence and
  the bullets, and between the bullets and any closing sentence.

Question: "{question}"

Reference passages:
{context}

Answer:"""
        context_budget.log_prompt_context(
            "compose", [("retrieved_passages", context), ("question", question)],
            prompt=prompt, route="rag", chunks=len(chunks))
        answer = await llm.call_response_composer(prompt)
        # The composer sometimes answers correctly AND tacks on a stray
        # "that isn't covered..." hedge sentence (about some tangential detail
        # it wasn't asked for) even though the actual question was answered in
        # full from the passages above it. A blind substring check on the
        # whole answer used to discard that entire good answer whenever the
        # phrase appeared anywhere — strip just the refusal sentence instead,
        # and only treat it as "no answer" when nothing substantive remains.
        cleaned = _REFUSAL_SENTENCE.sub("", answer).strip()
        if cleaned and _is_whole_refusal(cleaned):
            cleaned = ""
        if not cleaned:
            # Not just a stray hedge sentence — the composer refused outright.
            # Runs at non-zero temperature, so on passages that genuinely do
            # answer the question this is sometimes just a bad sample: verified
            # directly on CM Elevate's intro chunk at top_score ~0.77 (well
            # above RAG_MEDIUM_CONFIDENCE) — the identical prompt outright
            # refused twice in one run, then answered correctly on the next
            # sample with no code change. One retry on the same (already-good)
            # context is cheap; a second outright refusal is trusted as real.
            answer = await llm.call_response_composer(prompt)
            cleaned = _REFUSAL_SENTENCE.sub("", answer).strip()
            if cleaned and _is_whole_refusal(cleaned):
                cleaned = ""
            if not cleaned:
                return None
        return {
            "answer": _clean_for_display(cleaned),
            "confidence": "high" if top_score >= settings.RAG_HIGH_CONFIDENCE else "medium",
            "sources": sources,
        }

    logger.info("rag.answer_from_kb: top score %.3f below medium floor — no answer", top_score)
    return None


async def answer_from_kb_multi(question: str, schemes: list[str]) -> dict | None:
    """A KNOWLEDGE question that names two or more schemes outright ("how do I
    apply across MGNREGA, PMAY-G, Focus Plus and CM Elevate"). A single
    unscoped `retrieve()` pulls RAG_TOP_K candidates from the whole KB in one
    shot, so whichever scheme's passages happen to embed closest to the
    question crowd out the others — a named scheme with real reference
    material can come back "not covered" simply because its chunks never made
    the cut. Retrieve each named scheme separately instead, then compose one
    answer that addresses every scheme that had material."""
    # Schemes sharing one knowledge base (CM Elevate + CM Elevate Legacy) are
    # retrieved ONCE, under one header naming both — retrieving per scheme
    # would put the same passages in twice and invite two identical answers.
    groups: dict[str, list[str]] = {}
    for s in schemes:
        groups.setdefault(kb_scheme(s), []).append(s)
    per_scheme: dict[str, list[dict]] = {}
    covered: set[str] = set()
    for kb, members in groups.items():
        chunks = await retrieve(question, scheme=kb)
        if chunks:
            per_scheme[" / ".join(members)] = chunks[:4]  # cap so the composer isn't flooded
            covered.update(members)

    if not per_scheme:
        return None

    sections = []
    sources = []
    for s, chunks in per_scheme.items():
        body = "\n\n".join(_strip_heading(c["text"]) for c in chunks)
        sections.append(f"=== {s} ===\n{body}")
        sources.extend({"doc": c["doc"], "heading": c["heading"],
                         "source_type": c.get("source_type", "sme")} for c in chunks)
    context = "\n\n---\n\n".join(sections)

    prompt = f"""Answer the question using ONLY the reference passages below, which are
grouped by scheme under "=== SchemeName ===" headers. Answer separately for EVERY
scheme that has a section below — do not skip one, and do not invent an answer
for a scheme that has no section. Do not invent numbers, dates, or amounts.
No Markdown headings ("#", "##", "###").

Formatting — one short bullet per scheme, formatted exactly as
"- **SchemeName** — one or two sentence answer", one bullet per line.

Question: "{question}"

Reference passages:
{context}

Answer:"""
    context_budget.log_prompt_context(
        "compose", [("retrieved_passages", context), ("question", question)],
        prompt=prompt, route="rag_multi", chunks=len(sources))
    answer = await llm.call_response_composer(prompt)
    cleaned = _REFUSAL_SENTENCE.sub("", answer).strip()
    if cleaned and _is_whole_refusal(cleaned):
        cleaned = ""
    if not cleaned:
        # Same one-retry tolerance as answer_from_kb — a non-zero-temperature
        # outright refusal on passages that do answer the question is
        # sometimes just a bad sample.
        answer = await llm.call_response_composer(prompt)
        cleaned = _REFUSAL_SENTENCE.sub("", answer).strip()
        if cleaned and _is_whole_refusal(cleaned):
            cleaned = ""
        if not cleaned:
            return None

    missing = [s for s in schemes if s not in covered]
    if missing:
        cleaned += ("\n\nI don't have reference material covering this for "
                    f"{', '.join(missing)}.")

    return {
        "answer": _clean_for_display(cleaned),
        "confidence": "medium",
        "sources": sources,
    }
