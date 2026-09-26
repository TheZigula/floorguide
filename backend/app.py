# backend/app.py -- owns the HTTP surface: /health, /chat, /approve, /reject, CORS, the SQLite
# checkpointer wiring, and the startup ingestion check that refuses to serve answers from an empty index.
"""FloorGuide API.

Import order in this file is deliberate and must not be tidied: `load_dotenv` runs BEFORE
anything else is imported. Keys live in .env at the repo root, not in the shell, and
backend.graph reads ROUTER_MODEL and AGENT_MODEL at import time -- so a dotenv call placed
after that import would silently miss them. load_dotenv does not override a variable that is
already set, so a platform-provided key still wins in deployment.

Two kinds of memory are visible here: SqliteSaver keeps each thread's state on disk, so an
approval answered later picks up where it paused; and the in-process Store keeps one
cross-thread preference, the supervisor's home line, which survives a New conversation.

Nothing is filed anywhere real. /approve returns a simulated confirmation id.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import threading

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

from dotenv import load_dotenv  # noqa: E402 - must run before anything reads os.environ

# override=True is deliberate and load-bearing. CLAUDE.md's rule is that keys come from .env at
# the repo root, NOT from the shell, and python-dotenv does the OPPOSITE by default: it leaves an
# already-exported variable alone. These terminals carry a stale OPENAI_API_KEY that was rotated in
# .env, so without override every process started here silently used the dead value while .env held
# a working one -- identical from the outside to a key that simply died. Safe in deployment: App
# Platform has no .env file, so this call is a no-op there and the platform's own env vars still win.
# The explicit path matters too: bare load_dotenv() walks the caller's stack frame and looks in the
# process cwd, which uvicorn may change.
load_dotenv(REPO_ROOT / ".env", override=True)

from contextlib import asynccontextmanager  # noqa: E402

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402
from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from backend import guards  # noqa: E402
from backend.graph import build_graph, read_home_line  # noqa: E402
from backend.tools.vector_search import index_status, last_backend_used, probe_backend  # noqa: E402

log = guards.get_logger(__name__)

SERVICE_NAME = "floorguide"
CHECKPOINT_DB = os.getenv("CHECKPOINT_DB", str(REPO_ROOT / "checkpoints.db"))
CORS_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()
]

# Per-thread state on disk. NEVER the Postgres checkpointer on this box (CLAUDE.md hazard).
_conn = sqlite3.connect(CHECKPOINT_DB, check_same_thread=False)
GRAPH = build_graph(checkpointer=SqliteSaver(_conn))

# One turn at a time. The prototype has one supervisor and one eval runner, and serialising
# turns removes a class of flaky SQLite interleavings that would only show up on camera.
_turn_lock = threading.Lock()

INDEX: dict = {"ready": False, "backend": "none", "chunks": {}}


# --------------------------------------------------------------------------------------
# Startup: build the index before serving. A deployed container starts with an empty disk.
# --------------------------------------------------------------------------------------
def _refresh_index_state() -> None:
    """Fall back to whatever is already on disk, so an existing ./chroma still serves."""
    status = index_status()
    INDEX["chunks"] = status.get("collections", {})
    INDEX["ready"] = bool(status.get("index_ready"))
    if INDEX["ready"]:
        INDEX["backend"] = probe_backend()


def _startup_ingest() -> None:
    try:
        from backend.ingest import ensure_ingested
    except Exception as exc:  # noqa: BLE001 - a missing ingest module must not stop the service
        log.error("cannot import backend.ingest (%s: %s); no index will be built at startup",
                  type(exc).__name__, exc)
        _refresh_index_state()
        return
    try:
        summary = ensure_ingested(verbose=False)
    except Exception as exc:  # noqa: BLE001 - report it and serve 503s rather than nonsense
        log.error("ingestion failed (%s: %s)", type(exc).__name__, exc)
        _refresh_index_state()
        return
    INDEX["chunks"] = {
        name: int((info or {}).get("count") or 0)
        for name, info in (summary.get("collections") or {}).items()
    }
    INDEX["ready"] = bool(summary.get("ready"))
    # ensure_ingested reports "openai" whenever both collections are already full, because
    # nothing needed re-embedding -- it never proves a live query can embed. Probe once at boot
    # so /health is truthful from the first second, not only after the first search.
    INDEX["backend"] = probe_backend() if INDEX["ready"] else str(summary.get("vector_backend") or "none")
    log.info(
        "ingest: %s documents + %s asset records -> %s chunks (%s written, %s skipped) "
        "in %ss; collections=%s backend=%s ready=%s",
        summary.get("documents_read"), summary.get("asset_records_read"),
        summary.get("chunks_total"), summary.get("chunks_written"),
        summary.get("chunks_skipped"), summary.get("elapsed_s"),
        INDEX["chunks"], INDEX["backend"], INDEX["ready"],
    )
    if not INDEX["ready"]:
        log.error("the index is NOT ready: /chat will refuse with 503 rather than answer from nothing")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    guards.install_log_scrubber()
    _startup_ingest()
    log.info(
        "%s ready: commit=%s index_ready=%s backend=%s cors=%s checkpoints=%s",
        SERVICE_NAME, _commit(), INDEX["ready"], INDEX["backend"], CORS_ORIGINS, CHECKPOINT_DB,
    )
    yield
    _conn.close()


app = FastAPI(title="FloorGuide", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------------------
# The running commit, so a check against a stale deploy announces itself.
# --------------------------------------------------------------------------------------
def _commit() -> str:
    """GIT_SHA, else parse .git in pure Python, else "unknown". No git binary is invoked."""
    from_env = os.getenv("GIT_SHA")
    if from_env and from_env.strip():
        return from_env.strip()[:7]
    try:
        git = REPO_ROOT / ".git"
        if git.is_file():  # a worktree: .git is a file holding "gitdir: <path>"
            pointer = git.read_text(encoding="utf-8").strip()
            if pointer.startswith("gitdir:"):
                candidate = pathlib.Path(pointer.split(":", 1)[1].strip())
                git = candidate if candidate.is_absolute() else (REPO_ROOT / candidate).resolve()
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head[:7] or "unknown"
        ref = head.split(":", 1)[1].strip()
        loose = git / ref
        if loose.is_file():
            return loose.read_text(encoding="utf-8").strip()[:7] or "unknown"
        packed = git / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0][:7]
    except OSError as exc:
        log.warning("cannot read the commit from .git (%s); reporting unknown", exc)
    return "unknown"


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------
@app.get("/health", response_model=guards.HealthResponse)
def health() -> guards.HealthResponse:
    return guards.HealthResponse(
        status="ok" if INDEX["ready"] else "degraded",
        service=SERVICE_NAME,
        commit=_commit(),
        # what a search ACTUALLY used most recently, not what was latched at boot
        vector_backend=last_backend_used() or INDEX["backend"],
        index_ready=bool(INDEX["ready"]),
        chunks=INDEX["chunks"],
    )


@app.post("/chat", response_model=guards.ChatResponse)
def chat(req: guards.ChatRequest) -> guards.ChatResponse:
    _require_index()
    config = {"configurable": {"thread_id": req.thread_id}}

    # A draft already waiting is answered before anything else runs. The pause is the point:
    # the checkpoint holds the conversation exactly where it stopped.
    held = _pending_interrupt(GRAPH.get_state(config))
    if held is not None:
        return _held_response(req, config)

    try:
        with guards.begin_run():
            with _turn_lock:
                state = GRAPH.invoke(
                    {"messages": [HumanMessage(content=req.message)], "user_id": req.user_id},
                    config,
                )
    except guards.BudgetExceeded as exc:
        log.error("run stopped by the %s: %s", exc.cap, exc.detail)
        return guards.ChatResponse(
            thread_id=req.thread_id,
            worker="refuse",
            routed_to="refused",
            answer=guards.scrub(
                "REFUSED: this run hit its " + exc.cap + " and was stopped before finishing. "
                "Ask again, or ask a narrower question."
            ),
            refused=True,
            memory={"home_line": read_home_line(req.user_id)},
        )
    except guards.ModelUnavailable as exc:
        log.error("model unavailable: %s", exc)
        raise HTTPException(
            status_code=502, detail=guards.scrub("a model call failed: " + str(exc))
        ) from exc
    except guards.IndexNotReady as exc:
        # The index went away after startup -- an expired key, a collection that vanished. Say
        # that, rather than telling a supervisor the corpus does not cover her question.
        log.error("index not ready mid-run: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=guards.scrub("the document index cannot be searched right now: " + str(exc)),
        ) from exc

    return _chat_response(req, state)


@app.post("/approve", response_model=guards.ApproveResponse)
def approve(req: guards.ApproveRequest) -> guards.ApproveResponse:
    config = {"configurable": {"thread_id": req.thread_id}}
    if _pending_interrupt(GRAPH.get_state(config)) is None:
        raise HTTPException(
            status_code=409,
            detail="no work order is waiting for approval on thread " + req.thread_id
                   + ". It was already approved or rejected, or it never existed.",
        )
    decision = {
        "decision": "approve",
        "draft_id": req.draft_id,
        "fingerprint": req.fingerprint,
    }
    state = _resume(config, decision)
    problem = _interrupt_error(state, config)
    if problem:
        # The gate recorded the refusal and interrupted AGAIN. It is still waiting, so this is
        # a 409 and not a 500: nothing was lost and the correct click still works.
        raise HTTPException(status_code=409, detail=problem)
    return guards.ApproveResponse(
        confirmation_id=str(state.get("confirmation_id") or ""),
        answer=str(state.get("answer") or ""),
    )


@app.post("/reject", response_model=guards.RejectResponse)
def reject(req: guards.RejectRequest) -> guards.RejectResponse:
    config = {"configurable": {"thread_id": req.thread_id}}
    if _pending_interrupt(GRAPH.get_state(config)) is None:
        raise HTTPException(
            status_code=409,
            detail="no work order is waiting on thread " + req.thread_id
                   + ". It was already approved or rejected, or it never existed.",
        )
    state = _resume(config, {"decision": "reject", "draft_id": req.draft_id})
    problem = _interrupt_error(state, config)
    if problem:
        raise HTTPException(status_code=409, detail=problem)
    return guards.RejectResponse(answer=str(state.get("answer") or ""))


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def _require_index() -> None:
    if not INDEX["ready"]:
        raise HTTPException(
            status_code=503,
            detail="the document index is not ready, so there is nothing to answer from. "
                   "Run `python backend/ingest.py` (or restart the service) and try again.",
        )


def _resume(config: dict, decision: dict) -> dict:
    try:
        with guards.begin_run():
            with _turn_lock:
                return GRAPH.invoke(Command(resume=decision), config)
    except guards.ModelUnavailable as exc:
        raise HTTPException(
            status_code=502, detail=guards.scrub("a model call failed: " + str(exc))
        ) from exc
    except guards.BudgetExceeded as exc:
        raise HTTPException(
            status_code=502, detail=guards.scrub("run stopped by the " + exc.cap)
        ) from exc
    except guards.IndexNotReady as exc:
        raise HTTPException(
            status_code=503,
            detail=guards.scrub("the document index cannot be searched right now: " + str(exc)),
        ) from exc


def _pending_interrupt(snapshot) -> object:
    """The interrupt this thread is paused on, or None. `snapshot.next` is NOT reliable after a
    resume that interrupted again (measured); `snapshot.interrupts` is."""
    interrupts = tuple(getattr(snapshot, "interrupts", ()) or ())
    return interrupts[0] if interrupts else None


def _interrupt_error(state: dict, config: dict) -> str:
    """After a resume: the reason the gate turned the decision away, or "" if it went through."""
    payloads = list(state.get("__interrupt__") or [])
    if not payloads:
        held = _pending_interrupt(GRAPH.get_state(config))
        if held is None:
            return ""
        payloads = [held]
    value = getattr(payloads[0], "value", None)
    if isinstance(value, dict) and value.get("error"):
        return str(value["error"])
    return "the approval gate is still waiting on this thread"


def _chat_response(req: guards.ChatRequest, state: dict) -> guards.ChatResponse:
    return guards.ChatResponse(
        thread_id=req.thread_id,
        worker=state.get("worker") or "refuse",
        routed_to=state.get("routed_to") or "refused",
        answer=str(state.get("answer") or ""),
        sources=state.get("sources") or [],
        rule_result=state.get("rule_result"),
        pending_approval=state.get("pending_approval"),
        dropped_chunks=int(state.get("dropped_chunks") or 0),
        refused=bool(state.get("refused")),
        memory={"home_line": read_home_line(req.user_id)},
    )


def _held_response(req: guards.ChatRequest, config: dict) -> guards.ChatResponse:
    """A draft is on screen and unanswered. Show it again rather than starting a new turn."""
    values = GRAPH.get_state(config).values or {}
    pending = values.get("pending_approval")
    log.info("thread %s is paused at the approval gate; returning the draft again", req.thread_id)
    return guards.ChatResponse(
        thread_id=req.thread_id,
        worker="actor",
        routed_to="work_order",
        answer="This conversation is paused on a work order that is waiting for you. "
               "Press Approve to record it or Reject to throw it away, and I will carry on "
               "from there.",
        sources=values.get("sources") or [],
        rule_result=values.get("rule_result"),
        pending_approval=pending,
        dropped_chunks=int(values.get("dropped_chunks") or 0),
        refused=False,
        memory={"home_line": read_home_line(req.user_id)},
    )
