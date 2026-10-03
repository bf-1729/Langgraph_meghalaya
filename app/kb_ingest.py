"""
Scheme-knowledge base ingest for the RAG path.

Source of truth: the SME-written narrative docs under data/ — the scheme
references and the general FAQs. These answer "what is MGNREGA", "who is
eligible for PMAY-G", "what are the scheme components" — questions that are
NOT database questions and must never be answered by generating SQL.

Chunking follows the NeuralAiGovernanceProject rag_service._chunk() pattern:
one chunk per H2 section, split at H3 when a section is too long, each chunk
prefixed with its heading path so the heading travels into the embedding.

Idempotent: startup calls ingest_kb(), which skips the work if the Qdrant
collection already holds roughly the expected number of points. POST
/api/rag/reingest forces a full rebuild.
"""
import logging
import re
import uuid
from pathlib import Path

from qdrant_client import models

from app import llm, vectorstore
from app.config import settings

logger = logging.getLogger(__name__)

# app/kb_ingest.py -> repo root -> data/
_DATA_PART = Path(__file__).resolve().parents[1] / "data"

# (path, scheme) — scheme is a coarse payload tag for optional filtering later.
# These are the SME-written docs: the authoritative tier.
_SOURCES = [
    ("reference/mgnrega_complete_reference.md", "MGNREGA"),
    ("reference/mgnrega_general_faq.md", "MGNREGA"),
    ("reference/pmay_gramin_complete_reference.md", "PMAY-G"),
    ("reference/pmay_gramin_general_faq.md", "PMAY-G"),
    ("reference/focusplus_complete_reference.md", "Focus Plus"),
    ("reference/focusplus_general_faq.md", "Focus Plus"),
    ("reference/cmelevate_complete_reference.md", "CM Elevate"),
    ("reference/cmelevate_general_faq.md", "CM Elevate"),
    # Delivered with this casing/naming; kept verbatim rather than renamed so the
    # paths still match what the SMEs handed over.
    ("reference/FOCUS_legacy_Complete_Reference.md", "Focus Legacy"),
    ("reference/FOCUS_LEGACY_FAQ.md", "Focus Legacy"),
]

# Scraped encyclopedic / official background, dropped into data/web/*.md by
# the web-ingest step. Picked up automatically. Tagged source_type="web" so the
# retriever / composer can treat them as context, not as the rule of record —
# the SME docs and the live DB stay authoritative.
_WEB_DIR = "web"

_CHUNK_SIZE = 1500

# Spaces and dots are allowed: the three newest schemes are multi-word
# ("Focus Legacy", "Focus Plus", "CM Elevate"), and the original
# `[A-Za-z0-9-]+` silently refused to match any of them — so an explicitly
# tagged doc fell through to filename-guessing or the unqueryable "GENERAL".
_SCHEME_TAG = re.compile(r"<!--\s*scheme:\s*([A-Za-z0-9.\- ]+?)\s*-->", re.IGNORECASE)

# Whatever the tag or the filename produced -> the scheme's canonical spelling,
# which is what every retrieval filter matches on (vectorstore.search uses an
# exact MatchValue). Keyed case-insensitively so "focus legacy", "FOCUS LEGACY"
# and "Focus Legacy" all land on the same tag.
_CANONICAL_SCHEME = {
    "mgnrega": "MGNREGA", "nrega": "MGNREGA",
    "pmay": "PMAY-G", "pmay-g": "PMAY-G", "pmayg": "PMAY-G", "pmay-gramin": "PMAY-G",
    "pmay-u": "PMAY-U", "pmay-urban": "PMAY-U",
    "focus plus": "Focus Plus", "focus+": "Focus Plus", "focusplus": "Focus Plus",
    "focus legacy": "Focus Legacy", "focuslegacy": "Focus Legacy",
    "cm elevate": "CM Elevate", "cmelevate": "CM Elevate", "cm-elevate": "CM Elevate",
    # CM Elevate Legacy is the same CM-ELEVATE programme's sanction and
    # disbursement DATA; its reference material is the programme's, shared with
    # CM Elevate (see rag.kb_scheme). A doc tagged for it lands in that one
    # knowledge base rather than a second, disjoint namespace.
    "cm elevate legacy": "CM Elevate", "cmelevate legacy": "CM Elevate",
    "cmelevatelegacy": "CM Elevate", "cm-elevate legacy": "CM Elevate",
    "cm elevate disbursement": "CM Elevate",
}


def _web_sources() -> list[tuple[str, str]]:
    """(relative_path, scheme) for every data/web/*.md, tagged with the scheme's
    CANONICAL spelling. Scheme comes from a `<!-- scheme: X -->` comment, else
    the filename.

    A file we cannot place is SKIPPED, not ingested under a placeholder label:
    every retrieval path filters on an exact scheme name, so a chunk tagged
    anything else (the old 'GENERAL') can never be returned by a scoped search
    and only wastes an embedding. Skipping it logs a warning naming the file, so
    an untagged doc is a visible problem rather than a silently dead one."""
    web = _DATA_PART / _WEB_DIR
    if not web.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for p in sorted(web.glob("*.md")):
        raw = ""
        try:
            head = p.read_text(encoding="utf-8")[:2000]
            m = _SCHEME_TAG.search(head)
            if m:
                raw = m.group(1).strip()
            else:
                name = p.name.upper()
                # "LEGACY" is tested before the bare "FOCUS": both Focus schemes
                # carry that word, and the qualifier is what tells them apart.
                if "PMAY" in name:
                    raw = "PMAY-G"
                elif "MGNREGA" in name or "NREGA" in name:
                    raw = "MGNREGA"
                # Covers CM Elevate Legacy files too — both CM Elevate datasets
                # share one knowledge base (see _CANONICAL_SCHEME).
                elif "ELEVATE" in name:
                    raw = "CM Elevate"
                elif "LEGACY" in name:
                    raw = "Focus Legacy"
                elif "FOCUS" in name:
                    raw = "Focus Plus"
        except OSError:
            pass

        scheme = _CANONICAL_SCHEME.get(raw.lower().replace("_", " ").strip())
        if not scheme:
            logger.warning(
                "kb_ingest: skipping %s — no scheme could be determined (add a "
                "'<!-- scheme: Focus Legacy -->' comment); an untagged doc is not "
                "retrievable by any scheme-scoped search",
                p.name,
            )
            continue
        out.append((f"{_WEB_DIR}/{p.name}", scheme))
    return out


def _chunk(content: str) -> list[tuple[str, str]]:
    """Return [(heading_path, text), ...]. H2 sections, split at H3 when large."""
    lines = content.split("\n")
    sections: list[tuple[str, str]] = []
    current_h2 = "(intro)"
    buf: list[str] = []

    for line in lines:
        if line.startswith("## "):
            if buf:
                sections.append((current_h2, "\n".join(buf).strip()))
            current_h2 = line.lstrip("#").strip()
            buf = []
        else:
            buf.append(line)
    if buf:
        sections.append((current_h2, "\n".join(buf).strip()))

    chunks: list[tuple[str, str]] = []
    for heading, text in sections:
        if not text.strip():
            continue
        if len(text) <= _CHUNK_SIZE:
            chunks.append((heading, f"[{heading}]\n{text}"))
            continue
        # Too big — break at H3 boundaries.
        subs = re.split(r"\n(?=### )", text)

        if heading == "(intro)":
            # No real H2 in this doc — the general_faq.md files are flat
            # "### Question?" lists with no H2 at all, so the whole file lands
            # here as one oversized "(intro)" section. Packing several
            # unrelated Q&As into one chunk buried specific answers (e.g.
            # PMAY-G's "what documents are required" answer ended up inside a
            # chunk labeled after a different, unrelated question, and the
            # compose LLM skimmed past it). Each "###" is a self-contained
            # unit here, so give it its own chunk instead of packing.
            for sub in subs:
                sub = sub.strip()
                if not sub:
                    continue
                m = re.match(r"### (.+)", sub)
                label = m.group(1).strip() if m else heading
                chunks.append((label, f"[{label}]\n{sub}"))
            continue

        cur_label, cur = heading, ""
        for sub in subs:
            m = re.match(r"### (.+)", sub)
            label = f"{heading} > {m.group(1).strip()}" if m else heading
            if len(cur) + len(sub) <= _CHUNK_SIZE:
                cur = f"{cur}\n{sub}" if cur else sub
                cur_label = label
            else:
                if cur.strip():
                    chunks.append((cur_label, f"[{cur_label}]\n{cur.strip()}"))
                cur, cur_label = sub, label
        if cur.strip():
            chunks.append((cur_label, f"[{cur_label}]\n{cur.strip()}"))

    return [(h, t) for h, t in chunks if len(t.strip()) > 50]


# A PMAY-G-tagged chunk whose OWN heading doesn't say "Urban" can still carry a
# nested subsection that does — e.g. pmay_complete_reference.md's H2 "## 7.
# Application and Registration Process" is short enough to survive as ONE
# chunk (heading "7. Application and Registration Process", no "Urban" in it)
# while its body has both "### 7.1 PMAY-Urban" and "### 7.2 PMAY-Gramin" as
# sub-headings. The heading-only check below can't see that. Strip any such
# Urban sub-heading's block (through the next heading of any level, or end of
# text) out of a PMAY-G chunk's body before it's embedded — the Gramin content
# in the rest of the chunk is unaffected and still answers the question.
_URBAN_SUBSECTION = re.compile(
    r"\n#{2,6}[ \t]*[^\n]*\burban\b[^\n]*\n.*?(?=\n#{2,6}[ \t]|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def _strip_urban_subsections(text: str) -> str:
    return _URBAN_SUBSECTION.sub("\n", text)


def _collect_chunks() -> list[dict]:
    out: list[dict] = []
    sources = [(f, s, "sme") for f, s in _SOURCES] + \
              [(f, s, "web") for f, s in _web_sources()]
    for fname, scheme, source_type in sources:
        path = _DATA_PART / fname
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("kb_ingest: source not found, skipping — %s", path)
            continue
        for heading, chunk_text in _chunk(text):
            # A PMAY doc can carry both Gramin and Urban sections under one
            # filename-derived "PMAY-G" tag (e.g. data/web/PMAY_Wikipedia.md's
            # "Income Categories (Urban)", "Eligibility Conditions (Urban
            # CLSS)"). Re-tag those sections "PMAY-U" — a scheme this bot
            # doesn't hold — so a PMAY-G-scoped retrieval (vectorstore.search's
            # scheme filter) can never surface Urban content in a PMAY-G answer.
            section_scheme = scheme
            if scheme == "PMAY-G" and re.search(r"\burban\b", heading, re.IGNORECASE):
                section_scheme = "PMAY-U"
            elif scheme == "PMAY-G":
                chunk_text = _strip_urban_subsections(chunk_text)
            out.append({"scheme": section_scheme, "doc": fname, "heading": heading,
                        "text": chunk_text, "source_type": source_type})
    return out


async def _embed_all(texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    batch = settings.EMBED_BATCH_SIZE
    for i in range(0, len(texts), batch):
        vectors.extend(await llm.call_embedding(texts[i : i + batch]))
    return vectors


async def ingest_kb(force: bool = False) -> dict:
    """Build (or rebuild) the Qdrant KB collection. Returns a small status dict."""
    chunks = _collect_chunks()
    if not chunks:
        logger.warning("kb_ingest: no source chunks found under %s", _DATA_PART)
        return {"ingested": 0, "skipped": True, "reason": "no sources"}

    existing = await vectorstore.collection_count()
    want_schemes = {c["scheme"] for c in chunks}
    if not force and existing >= int(len(chunks) * 0.9):
        # The count alone only says the collection is roughly the right SIZE,
        # which is not the same as it holding the right CONTENT. A scheme added
        # to data/ but never ingested answers "not covered" for every question
        # forever, and a count-based check cannot see it whenever the new
        # scheme is under 10% of the corpus (measured: 18 chunks or fewer on top
        # of the existing 170 never trips the threshold). Confirm every scheme
        # the sources produce is actually PRESENT before trusting the skip.
        have_schemes = await vectorstore.distinct_schemes()
        missing = want_schemes - have_schemes
        if missing:
            logger.warning(
                "kb_ingest: collection holds %d points but is MISSING %s — "
                "rebuilding (a scheme with no points answers 'not covered' for "
                "every question)",
                existing, ", ".join(sorted(missing)),
            )
        else:
            logger.info("kb_ingest: collection already holds %d points (~%d expected) "
                        "covering %s — skipping",
                        existing, len(chunks), ", ".join(sorted(have_schemes)))
            return {"ingested": 0, "skipped": True, "points": existing}

    n_docs = len(_SOURCES) + len(_web_sources())
    logger.info("kb_ingest: embedding %d chunks from %d docs", len(chunks), n_docs)
    vectors = await _embed_all([c["text"] for c in chunks])
    if len(vectors) != len(chunks):
        raise RuntimeError(f"embedding count {len(vectors)} != chunk count {len(chunks)}")

    await vectorstore.recreate_collection(dim=len(vectors[0]))
    points = [
        models.PointStruct(id=str(uuid.uuid4()), vector=vec, payload=chunk)
        for vec, chunk in zip(vectors, chunks)
    ]
    for i in range(0, len(points), 128):
        await vectorstore.upsert(points[i : i + 128])

    logger.info("kb_ingest: upserted %d points into %s", len(points), settings.QDRANT_COLLECTION)
    return {"ingested": len(points), "skipped": False}
