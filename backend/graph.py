# backend/graph.py -- owns the LangGraph supervisor, the four workers, the per-worker tool
# allow-list (enforced in code, not in a prompt), and the human approval gate.
"""FloorGuide graph.

Shape of a turn:

    START -> supervisor -> exactly ONE of: retriever | analyst | remember | refuse
                           analyst -> actor -> approval_gate   (same turn, when service is due)

THE SEAM THIS FILE ARGUES: the model routes and writes prose. It never does the arithmetic
and it never fills in a work order's fields. Status and hours come from `maintenance_due`;
the draft's fields come from the asset record and the rule result. So neither a note on an
asset record nor an injected sentence in a document can change a number on the screen.

Tool ownership is a table in this file (WORKER_TOOLS). Every tool call goes through
`call_tool(worker, name, ...)`, which RAISES ToolPermissionError when a worker reaches for a
tool it does not own. It never silently no-ops.
"""

from __future__ import annotations

import os
import re
from typing import Annotated, Any, Optional, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.store.memory import InMemoryStore
from langgraph.types import interrupt

from backend import guards
from backend.assets import AssetRecordNotFound, load_asset_record
from backend.tools.actions import draft_work_order, next_confirmation_id
from backend.tools.rules import RuleRefusal, maintenance_due
from backend.tools.vector_search import vector_search

log = guards.get_logger(__name__)

ROUTER_MODEL = os.getenv("ROUTER_MODEL", "claude-haiku-4-5")
AGENT_MODEL = os.getenv("AGENT_MODEL", "claude-opus-5")

REFUSAL_MARKER = "REFUSED:"


# --------------------------------------------------------------------------------------
# Tool allow-list. One row per worker. This is the security boundary, and it is CODE.
# --------------------------------------------------------------------------------------
WORKER_TOOLS: dict[str, tuple[str, ...]] = {
    "retriever": ("vector_search",),      # read-only search over the corpus
    "analyst": ("maintenance_due",),      # the arithmetic, and nothing else
    "actor": ("draft_work_order",),       # drafts; cannot file, send, or approve
    "remember": (),                       # no tools at all; writes ONE Store key
}

_TOOL_IMPLS = {
    "vector_search": vector_search,
    "maintenance_due": maintenance_due,
    "draft_work_order": draft_work_order,
}


class ToolPermissionError(RuntimeError):
    """A worker reached for a tool that is not on its row of WORKER_TOOLS."""


def call_tool(worker: str, tool_name: str, **kwargs: Any) -> Any:
    """The ONLY way a worker may reach a tool. Wrong worker -> raise, never a quiet no-op."""
    allowed = WORKER_TOOLS.get(worker)
    if allowed is None:
        raise ToolPermissionError(f"unknown worker {worker!r}; it owns no tools")
    if tool_name not in allowed:
        raise ToolPermissionError(
            f"worker {worker!r} may not call {tool_name!r}; it owns {allowed or '()'}"
        )
    impl = _TOOL_IMPLS.get(tool_name)
    if impl is None:
        raise ToolPermissionError(f"no implementation registered for tool {tool_name!r}")
    guards.charge_step("tool:" + tool_name)
    return impl(**kwargs)


# --------------------------------------------------------------------------------------
# State. Per-turn fields are cleared by the supervisor at the top of every turn.
# --------------------------------------------------------------------------------------
class FloorGuideState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    user_id: str
    intent: str
    worker: str
    routed_to: str
    answer: str
    sources: list[dict]
    rule_result: Optional[dict]
    pending_approval: Optional[dict]
    dropped_chunks: int
    refused: bool
    memory: dict
    asset_id: Optional[str]
    approval_outcome: Optional[str]      # "approved" | "rejected" | None
    confirmation_id: Optional[str]
    gate_refusals: list[str]             # every bad decision the gate turned away


# Cross-thread memory: namespace ("user", user_id), key "home_line". In-process for the
# prototype; the same interface is a database in production.
STORE = InMemoryStore()


def read_home_line(user_id: str) -> Optional[str]:
    item = STORE.get(("user", user_id), "home_line")
    if item is None:
        return None
    return (item.value or {}).get("value")


def write_home_line(user_id: str, value: str) -> None:
    STORE.put(("user", user_id), "home_line", {"value": value})


# --------------------------------------------------------------------------------------
# Model plumbing. Two models, one env var each, so either can be swapped without a code edit.
# --------------------------------------------------------------------------------------
def _text(resp: Any) -> str:
    content = getattr(resp, "content", resp)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                out.append(block.get("text", ""))
            elif isinstance(block, str):
                out.append(block)
        return "".join(out).strip()
    return str(content).strip()


# Models that reject `temperature` (claude-opus-5 deprecates it and returns a 400). Learned
# at runtime on the first refusal rather than hard-coded, so swapping AGENT_MODEL by env var
# cannot leave a stale model list behind.
_NO_TEMPERATURE: set[str] = set()


def _chat(model: str, system: str, user: str, max_tokens: int = 2000) -> str:
    """One model call, metered. No fallback to another model: a failure is a typed error.

    max_tokens is generous because claude-opus-5 thinks before it answers, and the thinking
    is charged against the same budget: a tight cap truncates the answer mid-sentence.
    """
    from langchain_anthropic import ChatAnthropic

    guards.charge_step("model:" + model)
    messages = [SystemMessage(content=system), HumanMessage(content=user)]
    for _ in range(2):
        kwargs = {"model": model, "max_tokens": max_tokens, "timeout": 60}
        if model not in _NO_TEMPERATURE:
            kwargs["temperature"] = 0
        try:
            resp = ChatAnthropic(**kwargs).invoke(messages)
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed 502 by app.py
            if "temperature" in str(exc) and model not in _NO_TEMPERATURE:
                log.info("%s does not accept temperature; retrying without it", model)
                _NO_TEMPERATURE.add(model)
                continue
            raise guards.ModelUnavailable(model + ": " + type(exc).__name__ + ": " + str(exc)) from exc
        guards.charge_model(model, getattr(resp, "usage_metadata", None))
        return _text(resp)
    raise guards.ModelUnavailable(model + ": could not be called")


# --------------------------------------------------------------------------------------
# Deterministic pre-checks. Safety is not left to a classifier's judgement.
# --------------------------------------------------------------------------------------
_BYPASS_VERB = r"(skip|bypass|defeat|disable|override|ignore|shorten|jump|forget|work around|get around|without)"
_SAFETY_NOUN = r"(lockout|lock out|loto|tag ?out|interlock|e-?stop|emergency stop|guard|ppe|permit|confined space|sign-?off|safety step|safety procedure)"
_BYPASS_RE = re.compile(_BYPASS_VERB + r"\b[^.?!]{0,40}\b" + _SAFETY_NOUN, re.IGNORECASE)
_ASSET_RE = re.compile(r"\b([A-Z]{1,2}-\d{1,4})\b")

# THE SHELF IS DECIDED BY THE KIND OF ANSWER WANTED, NOT BY THE NOUNS IN THE SENTENCE.
# "What are the lockout steps before I change the hydraulic filter on P-102?" is a safety
# question that happens to name a machine and a maintenance task. The classifier kept voting
# maintenance on the nouns, so the safety shelf was never searched and the retriever refused a
# question SP-01 answers. These topics mean safety even in that company, and safety wins ties
# because a safety procedure outranks a manual (source precedence).
_SAFETY_TOPIC_RE = re.compile(
    r"\b("
    r"lock ?out|loto|tag ?out|lock and tag|"
    r"ppe|personal protective|"
    r"e-?stop|emergency stop|"
    r"interlock|light curtain|two-hand|"
    r"guard|guards|guarding|guarded|"
    r"pinch point|"
    r"confined space|"
    r"spill|"
    r"zero energy|energy isolation|isolate the energy|stored energy|"
    r"restart after|after a fault|"
    r"permit to work|hot work"
    r")\b",
    re.IGNORECASE,
)
_LINE_RE = re.compile(r"\bline\s+([A-Za-z0-9][A-Za-z0-9-]{0,15})\b", re.IGNORECASE)


def is_safety_bypass(text: str) -> bool:
    """A request to skip a safety step. Code decides this, not the model."""
    return bool(_BYPASS_RE.search(text or ""))


def is_safety_topic(text: str) -> bool:
    """Does this question want a safety answer? Code decides, so a machine id in the sentence
    cannot outvote the lockout procedure."""
    return bool(_SAFETY_TOPIC_RE.search(text or ""))


def extract_asset_id(text: str) -> Optional[str]:
    m = _ASSET_RE.search((text or "").upper())
    return m.group(1) if m else None


def last_user_text(state: FloorGuideState) -> str:
    for msg in reversed(state.get("messages") or []):
        if isinstance(msg, HumanMessage) or getattr(msg, "type", "") == "human":
            return _text(msg)
    return ""


# --------------------------------------------------------------------------------------
# supervisor: classifies the turn with ROUTER_MODEL and routes to exactly one worker.
# It owns NO tools. It clears the per-turn fields so nothing leaks from the previous turn.
# --------------------------------------------------------------------------------------
SUPERVISOR_SYSTEM = """You are the router for FloorGuide, an assistant for a manufacturing floor supervisor.
Classify the message into EXACTLY ONE label. Reply with the label alone, lowercase, nothing else.

document      a question answerable from a plant document: a safety procedure, a maintenance manual, or a quality standard.
service_due   asks whether a machine is due for service, or about its meter hours, service interval, or overdue status.
work_order    explicitly asks to raise, draft, or open a work order for a machine.
remember      tells you to remember a fact about the user, such as which line they run.
safety_bypass asks to skip, bypass, defeat, shorten, or postpone a safety step, lockout, interlock, guard, or PPE.
out_of_scope  anything else: people, discipline, HR, payroll, personal data, small talk, or anything no plant document covers."""

_LABELS = ("document", "service_due", "work_order", "remember", "safety_bypass", "out_of_scope")


def supervisor_node(state: FloorGuideState) -> dict:
    guards.charge_step("supervisor")
    text = last_user_text(state)
    user_id = state.get("user_id") or "dana"

    if is_safety_bypass(text):
        intent = "safety_bypass"          # code wins; no classifier vote can unlock this
    else:
        raw = _chat(ROUTER_MODEL, SUPERVISOR_SYSTEM, text, max_tokens=16).lower()
        intent = next((lab for lab in _LABELS if lab in raw), "document")

    log.info("supervisor routed turn to intent=%s", intent)
    return {
        "intent": intent,
        "user_id": user_id,
        "worker": "",
        "routed_to": "",
        "answer": "",
        "sources": [],
        "rule_result": None,
        "pending_approval": None,
        "dropped_chunks": 0,
        "refused": False,
        "asset_id": None,
        "approval_outcome": None,
        "confirmation_id": None,
        "gate_refusals": [],
        "memory": {"home_line": read_home_line(user_id)},
    }


def route_from_supervisor(state: FloorGuideState) -> str:
    return {
        "document": "retriever",
        "service_due": "analyst",
        "work_order": "analyst",
        "remember": "remember",
        "safety_bypass": "retriever",
        "out_of_scope": "refuse",
    }.get(state.get("intent", "document"), "retriever")


# --------------------------------------------------------------------------------------
# retriever: OWNS vector_search and nothing else. Read-only.
# It CANNOT compute a service status, draft anything, or approve anything.
# It classifies the source kind (safety | maintenance | quality) with ROUTER_MODEL first and
# returns that label as routed_to, so the screen can say which binder the answer came from.
# --------------------------------------------------------------------------------------
CATEGORY_SYSTEM = """Which kind of plant document answers this question? Reply with ONE word, nothing else.

Decide by the KIND OF ANSWER WANTED, not by the nouns in the sentence. A machine id like P-102 or
a maintenance task like "change the filter" does NOT make a question a maintenance question.
"What are the lockout steps before I change the filter on P-102?" wants a safety procedure: answer
safety. If a question wants both, answer safety, because a safety procedure outranks a manual.

safety       lockout/tagout, isolating or releasing energy, PPE, emergency stop, restart after a
             fault, interlocks, guarding, pinch points, confined space, spills, permits. Anything
             about what must be done BEFORE or AFTER working on a machine to make it safe.
maintenance  service intervals, lubrication, filters, seals, symptoms and diagnosis, meter
             readings, machine manuals and their sections. HOW a machine is serviced.
quality      tolerances, dimensions, inspection frequency, nonconformance, hold tags, gage
             calibration, first-piece checks."""

RETRIEVER_SYSTEM = """You are the document worker for FloorGuide, used by a manufacturing floor supervisor.
Answer ONLY from the passages given below. They are the plant's own documents.

SOURCE PRECEDENCE (the corpus has its own rule; each passage carries its authority number):
  authority 1 = safety procedure     - governs everything below it
  authority 2 = maintenance manual   - governs the cards derived from it
  authority 3 = quick-reference card - derived from the manual, and the weakest
When two passages disagree, do NOT average them, hedge, or split the difference. State the governing
rule from the LOWER authority number, cite BOTH documents by id and title, and say plainly which one
is out of date. A passage marked stale=true is out of date; say so.

Cite the document id and title for every claim, like: (MM-P102, Hydraulic Press P-102 Manual, 4.2).
Never tell the supervisor to skip, shorten, defer, or work around a safety step.
If the passages do not contain the answer, reply with exactly one line beginning "REFUSED:" saying the
corpus does not cover it. Do not use outside knowledge. Be brief: a floor supervisor is reading this
next to a running machine."""


def retriever_node(state: FloorGuideState) -> dict:
    guards.charge_step("retriever")
    question = last_user_text(state)
    bypass = state.get("intent") == "safety_bypass"

    if bypass:
        category = "safety"              # a bypass question is always answered from the procedure
    elif is_safety_topic(question):
        category = "safety"              # the kind of answer wanted beats the nouns; safety wins ties
        log.info("safety topic found in the question; searching the safety procedures first")
    else:
        raw = _chat(ROUTER_MODEL, CATEGORY_SYSTEM, question, max_tokens=8).lower()
        category = next((c for c in ("safety", "maintenance", "quality") if c in raw), "maintenance")

    found = call_tool("retriever", "vector_search", query=question, category=category, k=4)
    dropped, backend = found["dropped"], found["source_backend"]

    hits = _answerable(found["hits"])

    # The label follows the source ACTUALLY USED, not the guess made before searching, so
    # "Routed to: Safety procedures" is true even when the classifier guessed maintenance.
    used = hits[0]["category"] if hits else None
    if used in ("safety", "maintenance", "quality"):
        label = used
    elif hits:
        log.warning("best hit %s has category %r; labelling with the guess", hits[0]["source_id"], used)
        label = category
    else:
        label = "refused"
    if hits and label != category:
        log.info("routed_to corrected from the %s guess to %s, the shelf the answer came from",
                 category, label)

    answer = _answer_from(hits, question)
    refused = answer.startswith(REFUSAL_MARKER)

    # A REFUSAL IS A TRIGGER, NOT AN ANSWER. A chunk can sit close to the question and still not
    # contain the answer -- MM-P102 is near "lockout steps before I change the filter on P-102"
    # without holding the lockout steps, so the distance floor alone never widened. Before the
    # supervisor is told no, look on every shelf. Refuse only if that finds nothing either.
    if refused and category and not found["widened"]:
        wider = call_tool("retriever", "vector_search", query=question, category=None, k=4)
        wider_hits = _answerable(wider["hits"])
        # Both searches screen the same wider neighbourhood, so take the larger count rather
        # than adding them: one instruction-shaped document must not be reported as two.
        dropped = max(dropped, wider["dropped"])
        if wider_hits and {h["chunk_id"] for h in wider_hits} != {h["chunk_id"] for h in hits}:
            log.info("refusal on the %s shelf; re-searching every category before refusing", category)
            second = _answer_from(wider_hits, question)
            if not second.startswith(REFUSAL_MARKER):
                hits, answer, refused = wider_hits, second, False
                used = hits[0]["category"]
                label = used if used in ("safety", "maintenance", "quality") else category
                log.info("the wider search answered it from %s (%s)", hits[0]["source_id"], label)
            else:
                hits, answer = wider_hits, second   # show what was searched, then refuse
                label = "refused"
        elif not wider_hits:
            label = "refused"
        else:
            label = "refused"

    sources = [
        {
            "source_id": h["source_id"],
            "title": h["title"],
            "category": h["category"],
            "snippet": h["snippet"],
            "source_backend": backend,
        }
        for h in hits
    ]

    if bypass:
        # Code prepends the refusal; the model never gets to decide whether a bypass is allowed.
        body = answer[len(REFUSAL_MARKER):].strip() if answer.startswith(REFUSAL_MARKER) else answer
        answer = (
            REFUSAL_MARKER + " I cannot advise skipping, shortening, or working around a safety "
            "step, and I cannot approve one. Here is what the procedure actually requires:\n\n" + body
        )
        refused = True
        if label == "refused":
            label = "safety"   # the lane is still the safety procedures, even with nothing found

    return {
        "worker": "retriever",
        "routed_to": label,
        "answer": answer,
        "sources": sources,
        "dropped_chunks": dropped,
        "refused": refused,
        "messages": [AIMessage(content=answer)],
    }


def _answerable(hits: list) -> list:
    """Asset records are structured data, not prose authority: the retriever never answers out
    of one. Keeping them out also keeps routed_to inside the three labels a supervisor knows."""
    return [h for h in hits if h["category"] != "asset_record"]


def _answer_from(hits: list, question: str) -> str:
    """Write the answer from these passages, or refuse if there are none."""
    if not hits:
        return (
            REFUSAL_MARKER + " I have no passage in the plant corpus that answers that. "
            "Nothing here is a substitute for the binder on the floor."
        )
    passages = "\n\n".join(
        "[" + h["source_id"] + "] " + h["title"]
        + " (category=" + str(h["category"]) + ", authority=" + str(h["authority"])
        + ", stale=" + str(h["stale"]) + ")\n" + h["text"]
        for h in hits
    )
    return _chat(
        AGENT_MODEL, RETRIEVER_SYSTEM, "PASSAGES:\n" + passages + "\n\nQUESTION: " + question
    )


# --------------------------------------------------------------------------------------
# analyst: OWNS maintenance_due and nothing else.
# It reads ONLY the three integers off the asset record (the loader never hands it `notes`),
# and it invents no prose: the sentence below is built from the function's return value.
# It CANNOT search documents, draft, or approve.
# --------------------------------------------------------------------------------------
def analyst_node(state: FloorGuideState) -> dict:
    guards.charge_step("analyst")
    text = last_user_text(state)
    asset_id = extract_asset_id(text)

    if not asset_id:
        return _refusal(
            "I could not tell which machine you mean. Name it the way the plate does, "
            "for example P-102, C-7, or M-31.",
            worker="analyst",
            routed_to="rule",
        )

    try:
        record = load_asset_record(asset_id)
    except AssetRecordNotFound:
        return _refusal(
            "I have no asset record for " + asset_id + ". I will not estimate a service status "
            "without meter readings.",
            worker="analyst",
            routed_to="rule",
        )

    try:
        result = call_tool(
            "analyst",
            "maintenance_due",
            asset_id=record.asset_id,
            meter_hours_now=record.meter_hours_now,
            meter_hours_at_last_service=record.meter_hours_at_last_service,
            service_interval_hours=record.service_interval_hours,
        )
    except RuleRefusal as refusal:
        return _refusal(
            "I cannot compute a service status for " + asset_id + ". The field `"
            + refusal.field + "` " + refusal.reason + " (value: " + repr(refusal.value) + "). "
            "A supervisor needs to re-read the meter before this machine is scheduled.",
            worker="analyst",
            routed_to="rule",
            asset_id=asset_id,
        )

    rr = result.model_dump()
    headline = {
        "OVERDUE": asset_id + " is OVERDUE by " + str(rr["overdue_by"]) + " hours.",
        "DUE_SOON": asset_id + " is DUE SOON: " + str(rr["hours_remaining"]) + " hours remaining.",
        "OK": asset_id + " is OK: " + str(rr["hours_remaining"]) + " hours remaining.",
    }[rr["status"]]
    answer = (
        headline + " It has run " + str(rr["hours_since_service"]) + " hours since its last service "
        "against a " + str(record.service_interval_hours) + "-hour interval for "
        + record.service_task + " (" + record.manual_ref + "). "
        "These numbers are computed by the service-due rule, not estimated."
    )
    if rr["status"] == "OK":
        answer += " No work order drafted."

    return {
        "worker": "analyst",
        "routed_to": "rule",
        "answer": answer,
        "rule_result": rr,
        "asset_id": asset_id,
        "refused": False,
        "messages": [AIMessage(content=answer)],
    }


def route_from_analyst(state: FloorGuideState) -> str:
    """Due or overdue hands off to the actor IN THE SAME TURN, so one question ends at the card."""
    rr = state.get("rule_result")
    if rr and rr.get("status") in ("OVERDUE", "DUE_SOON"):
        return "actor"
    return END


# --------------------------------------------------------------------------------------
# actor: OWNS draft_work_order and nothing else. It drafts and then STOPS at the gate.
# It CANNOT file, send, post, or approve; it cannot search; it cannot recompute a number.
# Its fields come from the asset record and the rule result, never from a model.
# --------------------------------------------------------------------------------------
def actor_node(state: FloorGuideState) -> dict:
    guards.charge_step("actor")
    rr = state.get("rule_result") or {}
    asset_id = state.get("asset_id") or rr.get("asset_id")
    record = load_asset_record(asset_id)

    draft = call_tool(
        "actor",
        "draft_work_order",
        asset_id=record.asset_id,
        task=record.service_task,
        priority="high" if rr.get("status") == "OVERDUE" else "normal",
        source_refs=[record.manual_ref],
        rule_result=rr,
    )
    pending = {
        "draft_id": draft.draft_id,
        "fingerprint": draft.fingerprint,
        "work_order": {
            "asset_id": draft.asset_id,
            "task": draft.task,
            "priority": draft.priority,
            "source_refs": list(draft.source_refs),
        },
    }
    answer = (
        state.get("answer", "") + "\n\nI have drafted a work order: " + draft.asset_id + " - "
        + draft.task + ", priority " + draft.priority + ", citing "
        + ", ".join(draft.source_refs) + ". Nothing is filed. It waits for your Approve or Reject."
    ).strip()

    return {
        "worker": "actor",
        "routed_to": "work_order",
        "answer": answer,
        "pending_approval": pending,
        "messages": [AIMessage(content=answer)],
    }


# --------------------------------------------------------------------------------------
# approval_gate: the human gate. NOTHING runs between the interrupt and the click.
# A bad decision is RECORDED and the gate interrupts AGAIN. It never raises after
# interrupt() returns: a raise is checkpointed and replays on the next resume, which would
# poison the following correct approval (CLAUDE.md hazard, dry run 1).
# The gate is its own node so the draft above is created ONCE and is not re-made on resume.
# --------------------------------------------------------------------------------------
def approval_gate_node(state: FloorGuideState) -> dict:
    pending = state.get("pending_approval") or {}
    refusals = list(state.get("gate_refusals") or [])
    request = {
        "draft_id": pending.get("draft_id"),
        "fingerprint": pending.get("fingerprint"),
        "work_order": pending.get("work_order"),
        "prompt": "Approve or Reject this work order.",
    }

    decision = interrupt(request)
    while True:
        problem = _gate_problem(decision, pending)
        if problem is None:
            break
        refusals.append(problem)
        log.warning("approval gate turned a decision away: %s", problem)
        # Record and PAUSE AGAIN. No raise. The gate keeps waiting for a good click.
        decision = interrupt({**request, "error": problem, "attempts_refused": len(refusals)})

    if decision.get("decision") == "reject":
        answer = (
            "Rejected. Draft " + str(pending.get("draft_id")) + " was thrown away and nothing "
            "was filed. This run is over."
        )
        return {
            "approval_outcome": "rejected",
            "pending_approval": None,
            "answer": answer,
            "gate_refusals": refusals,
            "messages": [AIMessage(content=answer)],
        }

    confirmation_id = next_confirmation_id()
    wo = pending["work_order"]
    answer = (
        "Approved. Work order " + confirmation_id + " for " + wo["asset_id"] + " - " + wo["task"]
        + " (priority " + wo["priority"] + ", " + ", ".join(wo["source_refs"]) + "). "
        "Simulated confirmation only: this prototype files nothing to a real system."
    )
    return {
        "approval_outcome": "approved",
        "confirmation_id": confirmation_id,
        "pending_approval": None,
        "answer": answer,
        "gate_refusals": refusals,
        "messages": [AIMessage(content=answer)],
    }


def _gate_problem(decision: Any, pending: dict) -> Optional[str]:
    """Return a human-readable problem with this decision, or None when it is good."""
    if not isinstance(decision, dict):
        return "decision was not an approve or reject payload"
    if decision.get("decision") not in ("approve", "reject"):
        return "decision must be approve or reject, got " + repr(decision.get("decision"))
    if decision.get("draft_id") != pending.get("draft_id"):
        return (
            "draft_id " + repr(decision.get("draft_id")) + " does not match the draft waiting on "
            "this thread (" + repr(pending.get("draft_id")) + ")"
        )
    if decision["decision"] == "approve" and decision.get("fingerprint") != pending.get("fingerprint"):
        return "fingerprint does not match the draft on screen; the work order changed underneath it"
    return None


# --------------------------------------------------------------------------------------
# remember: OWNS NO TOOLS. It writes exactly ONE cross-thread Store key, the home line.
# It cannot search, compute, draft, or approve.
# --------------------------------------------------------------------------------------
def remember_node(state: FloorGuideState) -> dict:
    guards.charge_step("remember")
    text = last_user_text(state)
    user_id = state.get("user_id") or "dana"
    match = _LINE_RE.search(text)

    if not match:
        answer = 'Which line is yours? Say it like "remember I am on Line 3" and I will keep it.'
        return {
            "worker": "remember",
            "routed_to": "memory",
            "answer": answer,
            "memory": {"home_line": read_home_line(user_id)},
            "messages": [AIMessage(content=answer)],
        }

    home_line = "Line " + match.group(1)
    write_home_line(user_id, home_line)
    answer = (
        "Noted. I will remember your home line is " + home_line
        + ", in this conversation and the next one."
    )
    return {
        "worker": "remember",
        "routed_to": "memory",
        "answer": answer,
        "memory": {"home_line": home_line},
        "messages": [AIMessage(content=answer)],
    }


# --------------------------------------------------------------------------------------
# refuse: out of scope. No tools, no sources, no draft.
# --------------------------------------------------------------------------------------
def refuse_node(state: FloorGuideState) -> dict:
    guards.charge_step("refuse")
    return _refusal(
        "That is outside what FloorGuide covers. I answer from the plant safety procedures, "
        "maintenance manuals, and quality standards, and I compute service status from meter "
        "readings. I hold no records about people.",
        worker="refuse",
        routed_to="refused",
    )


def _refusal(reason: str, *, worker: str, routed_to: str, asset_id: Optional[str] = None) -> dict:
    answer = REFUSAL_MARKER + " " + reason
    out = {
        "worker": worker,
        "routed_to": routed_to,
        "answer": answer,
        "refused": True,
        "pending_approval": None,
        "messages": [AIMessage(content=answer)],
    }
    if asset_id:
        out["asset_id"] = asset_id
    return out


# --------------------------------------------------------------------------------------
# Build. SqliteSaver for per-thread state; InMemoryStore for the one cross-thread preference.
# NEVER the Postgres checkpointer on this box (CLAUDE.md hazard).
# --------------------------------------------------------------------------------------
def build_graph(checkpointer=None):
    g = StateGraph(FloorGuideState)
    g.add_node("supervisor", supervisor_node)
    g.add_node("retriever", retriever_node)
    g.add_node("analyst", analyst_node)
    g.add_node("actor", actor_node)
    g.add_node("approval_gate", approval_gate_node)
    g.add_node("remember", remember_node)
    g.add_node("refuse", refuse_node)

    g.add_edge(START, "supervisor")
    g.add_conditional_edges(
        "supervisor",
        route_from_supervisor,
        {"retriever": "retriever", "analyst": "analyst", "remember": "remember", "refuse": "refuse"},
    )
    g.add_edge("retriever", END)
    g.add_conditional_edges("analyst", route_from_analyst, {"actor": "actor", END: END})
    g.add_edge("actor", "approval_gate")
    g.add_edge("approval_gate", END)
    g.add_edge("remember", END)
    g.add_edge("refuse", END)
    return g.compile(checkpointer=checkpointer, store=STORE)
