# backend/guards.py -- owns the typed request and response models, the injection screen that
# drops instruction-shaped chunks before a model sees them, the per-run step and cost caps, and the log scrubber.
"""Guards.

Four jobs, all of them boring on purpose:

1. TYPED I/O. Every request and response on the API contract is a pydantic model here, so a
   field that drifts is a 500 in testing rather than a wrong screen in front of a supervisor.

2. THE INJECTION SCREEN. The corpus is untrusted text. `screen_chunks` drops chunks that
   read like INSTRUCTIONS TO THE SYSTEM rather than plant documentation, and logs the count.
   It matches directives -- "ignore the ...", "approve this ...", "you are now ...",
   "SYSTEM:", a role override -- and deliberately NOT the bare words "bypass", "approval" or
   "override". SP-03, the governing safety procedure, says an interlock is "never ...
   bypassed"; a screen that matched that word would throw away the very document a bypass
   request is supposed to be answered with. (Instance 3 audited all 15 legitimate documents
   against an over-broad trigger list: SP-03 is the only near miss, and only on "bypassed".)

3. THE CAPS. A run gets a fixed number of steps and a fixed spend. Past either, the run ends
   with a typed error instead of grinding on.

4. THE LOG SCRUBBER. Emails, phone numbers and anything key-shaped are redacted on every
   line the service emits, because a leak looks like a key in a log.
"""

from __future__ import annotations

import contextvars
import logging
import os
import re
from contextlib import contextmanager
from typing import Any, Iterable, Literal, Optional

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------------------
# Typed errors. Each one has exactly one HTTP meaning, set in backend/app.py.
# --------------------------------------------------------------------------------------
class BudgetExceeded(RuntimeError):
    """A run hit a cap. Typed so the caller can say which cap, and end the run."""

    def __init__(self, cap: str, detail: str) -> None:
        self.cap = cap
        self.detail = detail
        super().__init__(cap + ": " + detail)


class StepCapExceeded(BudgetExceeded):
    pass


class CostCapExceeded(BudgetExceeded):
    pass


class ModelUnavailable(RuntimeError):
    """A model call failed. There is NO silent fallback to another model: the eval would be
    grading a population it was not told about. This surfaces as a typed 502."""


class IndexNotReady(RuntimeError):
    """The corpus was never ingested, so there is nothing to answer from. A typed 503.
    Answering from an empty index would be answering from nothing."""


# --------------------------------------------------------------------------------------
# 1. Typed I/O models -- the API contract, in code.
# --------------------------------------------------------------------------------------
Category = Literal["safety", "maintenance", "quality", "asset_record", "uncategorised"]
RoutedTo = Literal["safety", "maintenance", "quality", "rule", "work_order", "memory", "refused"]
# "refuse" is a superset of the contract's four workers: it is what an out-of-scope refusal
# reports, and it always arrives with routed_to="refused" and refused=true.
Worker = Literal["retriever", "analyst", "actor", "remember", "refuse"]


class ChatRequest(BaseModel):
    thread_id: str = Field(min_length=1)
    user_id: str = "dana"
    message: str = Field(min_length=1)


class SourceRef(BaseModel):
    source_id: str
    title: str
    category: str
    snippet: str
    source_backend: Literal["openai", "local"]


class RuleResultOut(BaseModel):
    asset_id: str
    hours_since_service: int
    hours_remaining: int
    status: Literal["OK", "DUE_SOON", "OVERDUE"]
    overdue_by: int


class WorkOrderOut(BaseModel):
    asset_id: str
    task: str
    priority: Literal["low", "normal", "high"]
    source_refs: list[str]


class PendingApproval(BaseModel):
    draft_id: str
    fingerprint: str
    work_order: WorkOrderOut


class MemoryOut(BaseModel):
    home_line: Optional[str] = None


class ChatResponse(BaseModel):
    thread_id: str
    worker: Worker
    routed_to: RoutedTo
    answer: str
    sources: list[SourceRef] = Field(default_factory=list)
    rule_result: Optional[RuleResultOut] = None
    pending_approval: Optional[PendingApproval] = None
    dropped_chunks: int = 0
    refused: bool = False
    memory: MemoryOut = Field(default_factory=MemoryOut)


class ApproveRequest(BaseModel):
    thread_id: str = Field(min_length=1)
    draft_id: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)


class ApproveResponse(BaseModel):
    status: Literal["approved"] = "approved"
    confirmation_id: str
    answer: str


class RejectRequest(BaseModel):
    thread_id: str = Field(min_length=1)
    draft_id: str = Field(min_length=1)


class RejectResponse(BaseModel):
    status: Literal["rejected"] = "rejected"
    answer: str


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    service: str = "floorguide"
    commit: str = "unknown"
    # "none" is outside the contract's two values on purpose: it is what an unbuilt index
    # reports, and it only ever appears with index_ready=false and status="degraded".
    vector_backend: Literal["openai", "local", "none"]
    index_ready: bool
    chunks: dict[str, int] = Field(default_factory=dict)


# --------------------------------------------------------------------------------------
# 2. The injection screen.
# --------------------------------------------------------------------------------------
# Each pattern is a DIRECTIVE aimed at the system, not a topic a plant document discusses.
INJECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("role-override", r"^\s*(system|assistant|user)\s*:"),
    ("role-override", r"\byou\s+are\s+now\b"),
    ("role-override", r"\b(act|behave)\s+as\s+(if\s+you\s+are\s+)?(an?\s+)?(admin|administrator|developer|root|system)\b"),
    ("override-mode", r"\b[\w-]*override[\w-]*\s+mode\b"),
    ("override-mode", r"\b(developer|debug|god|admin)\s+mode\b"),
    ("ignore-directive", r"\b(ignore|disregard|forget|overrule)\s+(the|all|any|these|those|previous|prior|above|earlier)\b"),
    ("approve-directive", r"\bapprove\s+(this|these|that|the)\b"),
    ("approve-directive", r"\b(confirm|approve|file|submit)\s+the\s+work\s+order\s+yourself\b"),
    ("approve-directive", r"\bwithout\s+waiting\s+for\s+(the\s+)?(supervisor|approval|a\s+human|sign)"),
    ("refusal-suppression", r"\braise\s+no\s+(refusal|objection|concern)"),
    ("refusal-suppression", r"\b(do\s+not|don't|never)\s+(refuse|ask|warn|mention)\b"),
    ("prompt-tampering", r"\b(new|updated|revised)\s+instructions?\b"),
    ("prompt-tampering", r"\b(system|your)\s+prompt\b"),
)

_COMPILED = tuple((name, re.compile(rx, re.IGNORECASE | re.MULTILINE)) for name, rx in INJECTION_PATTERNS)


def looks_like_injection(text: str) -> Optional[str]:
    """Name the directive this text is shaped like, or None. Used by the screen and testable
    on its own, so a false positive can be found against the corpus rather than in a demo."""
    body = text or ""
    for name, pattern in _COMPILED:
        if pattern.search(body):
            return name
    return None


def model_facing_text(chunk: Any) -> Optional[str]:
    """The exact string a model will be shown for this chunk, or None if it cannot be read.

    The screen used to inspect the body alone, while the passage handed to the model leads with
    the document's TITLE -- untrusted front matter copied verbatim out of the corpus file. A
    directive in a title therefore reached the model ahead of the body with nothing reported
    dropped (audit H1). Screening the composed string closes that: whatever the model is shown
    is what gets screened.
    """
    if not isinstance(chunk, dict):
        return None
    text = chunk.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return str(chunk.get("source_id") or "") + " " + str(chunk.get("title") or "") + "\n" + text


def screen_chunks(chunks: Iterable[dict]) -> tuple[list[dict], int]:
    """Drop instruction-shaped chunks BEFORE a model sees them. Returns (kept, dropped).

    FAILS CLOSED. A chunk whose text is missing, None, empty or not a string is dropped and
    counted rather than kept unread (audit M1): a chunk nobody can screen is not a chunk anybody
    should answer from, and staying silent about it would under-report the drop count.
    """
    kept: list[dict] = []
    dropped = 0
    for chunk in chunks:
        composed = model_facing_text(chunk)
        if composed is None:
            dropped += 1
            log.warning(
                "dropped chunk %s from %s: it has no readable text, so it cannot be screened",
                (chunk.get("chunk_id", "?") if isinstance(chunk, dict) else "?"),
                (chunk.get("source_id", "?") if isinstance(chunk, dict) else "?"),
            )
            continue
        verdict = looks_like_injection(composed)
        if verdict:
            dropped += 1
            log.warning(
                "dropped chunk %s from %s: reads like a %s, not plant documentation",
                chunk.get("chunk_id", "?"), chunk.get("source_id", "?"), verdict,
            )
            continue
        kept.append(chunk)
    return kept, dropped


# --------------------------------------------------------------------------------------
# 3. The caps. One budget per HTTP request, carried on a contextvar.
# --------------------------------------------------------------------------------------
MAX_STEPS = int(os.getenv("MAX_STEPS_PER_RUN", "24"))
MAX_USD = float(os.getenv("MAX_USD_PER_RUN", "0.50"))

# Dollars per million tokens, for the LOCAL SPEND CAP ONLY -- never billing. Over-estimating
# is the safe direction: the cap trips early rather than late. Override per deployment.
PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-5": (15.00, 75.00),
}
DEFAULT_PRICE = (15.00, 75.00)


class RunBudget:
    def __init__(self, max_steps: int = MAX_STEPS, max_usd: float = MAX_USD) -> None:
        self.max_steps = max_steps
        self.max_usd = max_usd
        self.steps = 0
        self.usd = 0.0
        self.trail: list[str] = []


_budget: contextvars.ContextVar[Optional[RunBudget]] = contextvars.ContextVar("floorguide_budget", default=None)


@contextmanager
def begin_run(max_steps: int = MAX_STEPS, max_usd: float = MAX_USD):
    budget = RunBudget(max_steps=max_steps, max_usd=max_usd)
    token = _budget.set(budget)
    try:
        yield budget
    finally:
        log.info("run used %s steps and about $%.4f: %s", budget.steps, budget.usd, " -> ".join(budget.trail))
        _budget.reset(token)


def current_budget() -> Optional[RunBudget]:
    return _budget.get()


def charge_step(label: str) -> None:
    """Count one unit of work. Outside a run (unit tests, the eval calling a tool directly)
    this is a no-op, so nothing here can make a pure function depend on a web request."""
    budget = _budget.get()
    if budget is None:
        return
    budget.steps += 1
    budget.trail.append(label)
    if budget.steps > budget.max_steps:
        raise StepCapExceeded(
            "step cap",
            "this run passed " + str(budget.max_steps) + " steps at " + label + " and was stopped",
        )


def charge_model(model: str, usage: Any) -> None:
    budget = _budget.get()
    if budget is None or not usage:
        return
    if isinstance(usage, dict):
        tokens_in = int(usage.get("input_tokens") or 0)
        tokens_out = int(usage.get("output_tokens") or 0)
    else:
        tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
    price_in, price_out = PRICE_PER_MTOK.get(model, DEFAULT_PRICE)
    budget.usd += (tokens_in * price_in + tokens_out * price_out) / 1_000_000
    if budget.usd > budget.max_usd:
        raise CostCapExceeded(
            "cost cap",
            "this run passed $" + format(budget.max_usd, ".2f") + " at " + model + " and was stopped",
        )


# --------------------------------------------------------------------------------------
# 4. The log scrubber. Runs on every line the service emits.
# --------------------------------------------------------------------------------------
_SCRUBBERS: tuple[tuple[re.Pattern, Optional[str]], ...] = (
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[email redacted]"),
    (re.compile(r"\b(sk|pk|rk)[-_][A-Za-z0-9_\-]{8,}", re.IGNORECASE), "[key redacted]"),
    (re.compile(r"\b(bearer|token|api[_-]?key|secret|password)\b\s*[:=]?\s*\S+", re.IGNORECASE), r"\1 [redacted]"),
    (re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"), "[key redacted]"),
    (re.compile(r"(?<!\d)(\+?\d[\d\s().-]{8,}\d)(?!\d)"), None),  # handled by _phone_sub
)

# Anything CONTAINING a date is not a phone number. The formatted log line carries its own
# timestamp ("2026-09-26 13:03:50,254"), and an earlier version redacted the first half of it.
_LOOKS_LIKE_DATE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}")


def _phone_sub(match: "re.Match") -> str:
    """Redact phone numbers, but leave dates and short readings alone. A log line reading
    "2026-09-26" or "250" is not a phone number, and a scrubber that eats them makes the
    service harder to debug for no gain in safety."""
    candidate = match.group(0)
    digits = sum(ch.isdigit() for ch in candidate)
    if digits < 10 or _LOOKS_LIKE_DATE.search(candidate):
        return candidate
    return "[phone redacted]"


def scrub(text: str) -> str:
    """Redact emails, phone numbers and anything key-shaped. Applied to every emitted line."""
    out = str(text)
    for pattern, replacement in _SCRUBBERS:
        out = pattern.sub(_phone_sub if replacement is None else replacement, out)
    return out


class ScrubbingFormatter(logging.Formatter):
    """Wraps a handler's existing formatter and scrubs whatever it produced.

    It deliberately does NOT rewrite record.msg or clear record.args. An earlier version did,
    and it broke uvicorn's access log: AccessFormatter unpacks record.args into five values,
    so clearing them raised inside logging on every request. Scrubbing the formatted string
    instead leaves every other formatter intact and also covers the timestamp, the logger
    name and any exception text.
    """

    def __init__(self, inner: logging.Formatter) -> None:
        super().__init__()
        self.inner = inner

    def format(self, record: logging.LogRecord) -> str:
        try:
            return scrub(self.inner.format(record))
        except Exception:  # noqa: BLE001 - a scrubber must never take the service down
            return "[log line suppressed: it could not be scrubbed]"


_installed = False


def install_log_scrubber() -> None:
    """Wrap every handler's formatter, including uvicorn's. Idempotent, and safe to call again
    after another library has added handlers of its own."""
    global _installed
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    handlers = list(root.handlers)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        handlers.extend(logging.getLogger(name).handlers)
    for handler in handlers:
        if isinstance(handler.formatter, ScrubbingFormatter):
            continue
        handler.setFormatter(ScrubbingFormatter(handler.formatter or logging.Formatter("%(message)s")))
    _installed = True


def get_logger(name: str) -> logging.Logger:
    if not _installed:
        install_log_scrubber()
    return logging.getLogger(name)


log = get_logger(__name__)
