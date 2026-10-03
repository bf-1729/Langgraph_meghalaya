"""
Prompt context accounting — how many (approximate) tokens each section of an
LLM prompt carries, logged once per model call, so a context problem ("the
follow-up rewrite invented a district", "the SQL prompt is creeping toward the
model's limit") can be diagnosed from the logs alone.

Every prompt in this app is ONE user message assembled from named sections
(see prompt_builder.build_sql_prompt and pipeline.rewrite_followup). `assemble`
joins those sections in the given order — byte-for-byte the same string the
callers built before this module existed — and logs one `prompt_context`
record: section sizes, total, the model's budget, and whether it is over.

Over budget, the SQL prompt is COMPRESSED by priority, never pruned blindly
(assemble_prioritized). Every required section passes through untouched:
the schema backbone, live schema, prohibited joins, business rules (common
mistakes), resolved entities, the scheme scope and the question. Each is
there because leaving it out produced a wrong number, so dropping one to fit
a count would trade a visible failure for an invisible wrong answer. Only
optional sections shrink: few-shot examples (5 -> 3 -> 1), and catalogue
lines the schema text already states verbatim. If the required part alone
is over budget, the prompt goes out over budget and says so in the log.
Measured 2026-09-26 (offline, few-shot banks loaded, no LIVE SCHEMA or catalogue), the SQL
prompt is ~3.0k-6.9k tokens per scheme against a 15,360-token budget, so no
compression happens today. The verifier and composer prompts are
log-and-warn only. The follow-up rewrite context is budgeted per tier: see
context_manager.build_rewrite_evidence / build_followup_context.

Token counts are approximate: ~4 characters per token, the same estimate
context_manager uses, with no tokenizer dependency. Nothing here logs prompt
TEXT, only section names and sizes, so questions and result data never reach
the log through this path.
"""
import contextvars
import json
import logging
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger(__name__)

# Set per request by middleware.security_headers (the same id the caller gets
# back as X-Request-ID), so a prompt_context line can be tied to one request.
# "-" outside a request (tests, scripts, startup).
request_id_var: "contextvars.ContextVar[str]" = contextvars.ContextVar("request_id", default="-")


def approx_tokens(text: str | None) -> int:
    """~4 characters per token. Zero for empty text."""
    return len(text or "") // 4


def budget_for(kind: str) -> int:
    """The input-token budget for one kind of prompt; 0 means unchecked.

    Each budget is the served --max-model-len minus that call's max_tokens
    reservation (docs/INFERENCE_REQUIREMENTS.md; llm.py call_* max_tokens):
    qwen-model 16,384 - 1,024 for SQL generation and repair; qwen4-deploy
    8,192 - 200 for the classifier roles (rewrite, verifier). qwen35-9b's
    max-model-len is not recorded anywhere in the repo, so the composer budget
    defaults to 0 (unchecked) rather than a guessed number."""
    return {
        "sql": settings.PROMPT_BUDGET_SQL_TOKENS,
        "sql_repair": settings.PROMPT_BUDGET_SQL_TOKENS,
        "sql_verify": settings.PROMPT_BUDGET_CLASSIFIER_TOKENS,
        "rewrite": settings.PROMPT_BUDGET_CLASSIFIER_TOKENS,
        "compose": settings.PROMPT_BUDGET_COMPOSER_TOKENS,
    }.get(kind, 0)


def log_prompt_context(kind: str, sections: "list[tuple[str, str]]", *,
                       prompt: "str | None" = None, **meta) -> dict:
    """Log one `prompt_context` record for a prompt built from `sections`
    ((name, text) pairs, in prompt order) and return it. Pass `prompt` when
    `sections` names only the variable parts of an f-string prompt: the
    total is then taken from the real prompt, and the fixed wording is
    reported as "frame". `meta` carries small, non-sensitive facts about the
    call (tier names, a guard verdict), never question or result text."""
    sizes: dict[str, int] = {}
    for name, text in sections:
        t = approx_tokens(text)
        if t:
            sizes[name] = sizes.get(name, 0) + t
    total = approx_tokens(prompt if prompt is not None
                          else "".join(text for _, text in sections))
    if prompt is not None:
        frame = total - sum(sizes.values())
        if frame > 0:
            sizes["frame"] = sizes.get("frame", 0) + frame
    budget = budget_for(kind)
    record = {
        "event": "prompt_context",
        "request_id": request_id_var.get(),
        "kind": kind,
        "sections": sizes,
        "total_tokens": total,
        "budget": budget or None,
        "over_budget": bool(budget and total > budget),
        **meta,
    }
    level = logging.WARNING if record["over_budget"] else logging.INFO
    logger.log(level, "prompt_context %s", json.dumps(record, default=str, sort_keys=True))
    return record


def log_decision(stage: str, **facts) -> dict:
    """Log one `pipeline_decision` record: WHY a stage chose what it chose —
    the continuation signals and follow-up kind, the intent, the schemes, a
    clarification rule, an SQL guard's verdict. Companion to prompt_context
    (sizes) and llm_call (tokens, latency); joined to both by request_id.
    `facts` are labels, rule names, scheme names and small structured values
    only, never question, prompt or result text."""
    record = {"event": "pipeline_decision", "request_id": request_id_var.get(),
              "stage": stage, **facts}
    logger.info("pipeline_decision %s", json.dumps(record, default=str, sort_keys=True))
    return record


@dataclass
class Section:
    """One named part of a prompt, for priority-aware assembly.

    required  never shortened or removed, whatever the budget: the question,
              the scheme scope, the schema, joins, business rules, filters.
    compress  for an optional section, progressively SMALLER versions of it,
              tried in order only while the prompt is over budget (e.g. fewer
              few-shot examples; catalogue lines the schema already states).
              Compression before removal: a compressor never returns less
              than the section still needs to say.
    priority  among optional sections, the lower number is compressed first."""
    name: str
    text: str
    required: bool = True
    compress: tuple = ()
    priority: int = 100


def assemble_prioritized(kind: str, sections: "list[Section]", **meta) -> str:
    """Join `sections` in order. When the total is over the kind's budget,
    compress optional sections (lowest priority first, each through its own
    compressors in order) until it fits or nothing optional is left to
    compress. Required sections are passed through untouched, so a prompt
    whose required part alone is over budget is sent over budget and logged as
    such. That is a visible warning, never a silently wrong answer.

    Under budget, the compressors are never called and the output is exactly
    "".join(section.text); the logged record matches assemble()."""
    texts = [s.text for s in sections]
    compressed: list[str] = []
    budget = budget_for(kind)
    if budget and approx_tokens("".join(texts)) > budget:
        order = sorted((i for i, s in enumerate(sections) if not s.required and s.compress),
                       key=lambda i: sections[i].priority)
        for i in order:
            for fn in sections[i].compress:
                if approx_tokens("".join(texts)) <= budget:
                    break
                try:
                    smaller = fn()
                except Exception:  # noqa: BLE001 — a failed compressor leaves the section as is
                    logger.debug("compressor for %s failed", sections[i].name, exc_info=True)
                    continue
                if isinstance(smaller, str) and len(smaller) < len(texts[i]):
                    texts[i] = smaller
                    if sections[i].name not in compressed:
                        compressed.append(sections[i].name)
    prompt = "".join(texts)
    try:
        log_prompt_context(kind, [(s.name, t) for s, t in zip(sections, texts)],
                           **({"compressed": compressed} if compressed else {}), **meta)
    except Exception:  # noqa: BLE001
        logger.debug("prompt_context logging failed", exc_info=True)
    return prompt


def assemble(kind: str, sections: "list[tuple[str, str]]", **meta) -> str:
    """Join `sections` in order into the prompt string, logging its make-up.
    Logging can never break prompt assembly."""
    prompt = "".join(text for _, text in sections)
    try:
        log_prompt_context(kind, sections, **meta)
    except Exception:  # noqa: BLE001 — observability must never cost an answer
        logger.debug("prompt_context logging failed", exc_info=True)
    return prompt
