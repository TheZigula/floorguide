<!-- README.md — owns the public front door of this repo: what FloorGuide is, how to run it, how to grade it, and where it is deployed.
     Instance 4 (deploy/docs) owns this file. The one-pager for a non-technical reader is docs/one_pager.md. -->

# FloorGuide

A floor supervisor asks a question in plain words. FloorGuide picks the right **kind** of source — a safety
procedure, a maintenance manual, or a quality standard — answers out of that document, and shows her the
document, the passage, and which kind of source it came from.

Three things it will not do, by design:

- **It does not do the arithmetic.** Whether a machine is due for service is computed by a fixed function,
  `maintenance_due`, from the machine's meter readings and the manual's interval. The model never restates a
  number the function did not return. A missing or backwards reading gets a refusal that **names the field**.
- **It does not file anything.** When service is due it *drafts* a work order — machine, task, priority, and the
  manual section behind it — and stops. The draft waits for a human to press Approve. Reject throws it away and
  nothing is filed.
- **It does not take orders from documents.** A document that tries to instruct the system is thrown out before
  the model reads it, and the screen says one was dropped.

When two documents disagree it says which one governs — a safety procedure beats a manual, a manual beats its
quick-reference card — and which one is out of date, instead of splitting the difference. It never advises
skipping a safety step, and it cannot approve anything itself.

Every document, machine, and person in this repo is invented.

## Live

| | |
|---|---|
| Front end | https://floorguide.vercel.app |
| Backend | https://floorguide-3avql.ondigitalocean.app |
| Health | https://floorguide-3avql.ondigitalocean.app/health |
| Repo | https://github.com/TheZigula/floorguide |

`/health` reports the service name, the commit it is actually running, and which embedding backend is live:

```json
{"status":"ok","service":"floorguide","commit":"1aeec8e","vector_backend":"local",
 "index_ready":true,"chunks":{"corpus_local":35,"corpus_openai":0}}
```

Read that payload rather than the status code. `commit` is how you tell which build is actually serving —
compare it to `git rev-parse --short origin/main`. `vector_backend` reads `openai` once an OpenAI key is
configured and `corpus_openai` has been populated, and `local` when it is running on the fallback embeddings.

## Try it

| | |
|---|---|
| Open this | https://floorguide.vercel.app |
| It talks to this | https://floorguide-3avql.ondigitalocean.app |

There is a short guide for a non-technical tester in **[docs/tester_guide.md](docs/tester_guide.md)** — a
ten-minute walk-through, things worth trying to break, and the honest limits of the prototype.

If you only have a minute, ask these three. Each one demonstrates a different guarantee.

1. **"What are the lockout steps before I change the hydraulic filter on P-102?"**
   The chip should read *Routed to: Safety procedures* and cite SP-01. The sentence names a maintenance task and a
   machine, and it still goes to the safety shelf — a safety procedure wins the tie.

2. **"Is P-102 due for service?"**
   Watch the numbers: OVERDUE by 30 hours, 280 hours run against a 250-hour interval. Those come from a fixed
   calculation, not from the model. A work-order draft then appears and **waits** — Approve returns a
   confirmation id, Reject throws it away and nothing is filed.

3. **"Is there a maintenance override mode for Line 3?"**
   Look for the notice *1 document dropped*. A planted "tip sheet" in the documents tries to give the system
   orders; it is thrown out before the model reads it, and the screen tells you one was dropped.

Every document, machine and person is invented, and nothing leaves the system without a human click.

## Run it locally

First, one file: copy `.env.example` to `.env` and fill in the two API keys. `.env` is git-ignored and is never
printed. Every entry point calls `load_dotenv()` before reading the environment.

Then five commands. **PowerShell 5.1: each `cd` is on its own line and never chained to an action** — `;` does
not stop on error, so a failed `cd` would otherwise run the next command in the wrong directory.

```powershell
cd D:\proto\floorguide
pip install -r requirements.txt
python backend/ingest.py
python -m uvicorn backend.app:app --port 8000
```

```powershell
cd D:\proto\floorguide\frontend
npm install
npm run dev
```

The front end is at http://localhost:5173 and expects `VITE_API_BASE=http://localhost:8000`.

Two notes that will cost you time otherwise:

- **Do not add `--reload`** while anything else is calling the backend. It watches every `.py` file, so an edit
  anywhere in `eval/` or `docs/` restarts the server and cuts requests already in flight.
- `python backend/ingest.py` builds two vector collections, one per embedding model — `corpus_openai` and
  `corpus_local`. Vectors from different models never share an index. With no OpenAI key the local collection
  is used and `/health` reports `vector_backend: local`.

## Grade it

```powershell
cd D:\proto\floorguide
python eval/run_eval.py
```

The runner is **fail-closed**: it exits non-zero if it cannot score a case, and it refuses to pass if a
deliberately wrong control case passes. It prints one line and writes `eval/results.json`:

```
EVAL: attempted=10 scored=10 failed_to_score=0 ... control=FAILED_AS_REQUIRED
```

Ten cases: three retrieval, two rule, two refusal, one injection, plus two controls. The **must-fail control**
asserts a wrong filter interval and has to fail — if it ever passes, the grader is not measuring anything. The
**subtle control** is reported, not enforced. The judge is OpenAI `gpt-4o` while the agents run on Claude, so a
different model family grades the one being graded, under a per-run spend cap.

## Environment variables

Names only — values live in `.env` locally, and in the App Platform component env (encrypted) and the Vercel
project env in production. Never in the repo.

| Name | Where | What it is |
|---|---|---|
| `OPENAI_API_KEY` | backend, **encrypted** | embeddings, and the eval judge |
| `ANTHROPIC_API_KEY` | backend, **encrypted** | the router and the agents |
| `CORS_ORIGINS` | backend | comma-separated browser origins allowed to call the API |
| `ROUTER_MODEL` | backend | routing is classification, so this one stays small |
| `AGENT_MODEL` | backend | the drafted work order is not classification |
| `GIT_SHA` | backend, optional | App Platform does not inject it; `/health` falls back to parsing `.git/HEAD` |
| `VITE_API_BASE` | front end | the backend's public base URL, read at build time |

## Deploy

The backend is a Docker image on DigitalOcean App Platform, built from `Dockerfile` against `main`; the spec is
`.do/app.yaml`. The front end is a Vite build on Vercel from `frontend/`.

Four things in that setup are deliberate and easy to undo by accident:

- **`.dockerignore` must not exclude `.git`.** The platform does not inject `GIT_SHA`, so `/health` parses
  `.git/HEAD` in pure Python to report the commit. Without `.git` in the build context the commit reads
  `unknown` and you cannot tell which build is live.
- **`/app` is chowned to the non-root user.** `WORKDIR` creates it as root, and `COPY --chown` only sets
  ownership of the files inside it. Without the chown the app dies at import with
  `sqlite3.OperationalError: unable to open database file`.
- **The health check allows 90 seconds before it starts counting failures.** The first container start rebuilds
  the vector index, and a shorter delay kills it mid-warm-up so it never finishes.
- **`doctl apps update --spec .do/app.yaml` would delete the two API keys**, because they exist only in the
  dashboard and not in that file. Any spec change starts from `doctl apps spec get`.

## Audit

The prototype was audited against its own contract. The full record, with file and line references for every
finding, is in **[docs/audit_f1899c7.md](docs/audit_f1899c7.md)**.

**Fixed:** H1 (a document's title reached the model unscreened, and the screen reported nothing dropped),
H2 (a corpus outage was reported to the supervisor as "the plant corpus does not cover that"), M1 (the injection
screen failed open on a chunk it could not read), and M7 (exception text reached the client screen unscrubbed).

### Known, not yet fixed

Listed because a prototype that hides its open findings is worth less than one that names them. None of these is
reachable from the demo script, and the scripted injection, bypass, rule and approval paths were each verified
against the deployed build.

- **M2** — The "is this thread paused?" check sits outside the lock that serialises turns.
- **M3** — Every failure that is not `ModelUnavailable` or `BudgetExceeded` is an untyped 500.
- **M4** — The cost cap silently does not count a model call that reports no usage.
- **M5** — `is_safety_bypass` refuses innocent safety questions.
- **M6** — Values computed and never emitted.
- **L1** — `dropped_chunks` is a ranking heuristic, not a fact.
- **L2** — `worker: "refuse"` is outside the API contract.
- **L3** — The per-worker allow-list is a convention, not a construction.
- **L4** — Permissive default on an unparseable router reply.
- **L5** — `extract_asset_id` matches document ids.
- **L6** — Cap breaches answer inconsistently.
- **L7** — `_commit()` catches only `OSError`.

## What I would add with a week

**Deploy and operations**

- **Postgres instead of SQLite for conversation state.** The checkpointer is the same interface either way; the
  prototype uses SQLite because this box has no Postgres. Today the container's disk is ephemeral, so a restart
  loses in-flight approvals.
- **Ship the vector index, stop rebuilding it.** The index is currently rebuilt on every container start, which
  costs an embedding run per restart and per redeploy. A prebuilt index or a managed vector store makes boots
  fast and deterministic.
- **Authenticate the approver.** Right now the approver is *assumed* trusted. The identity should be
  authenticated and merged onto the work order at filing time, in a step no model touches.
- **File the work order for real** behind the approval, against a maintenance system, with the confirmation id
  coming back from that system instead of being simulated.
- **Widen the security battery.** `detect-secrets` runs before every push; `pip-audit` and `bandit` were
  declined for a 150-minute prototype and belong in CI, along with the mutation gate that was also declined.
- **Structured logs and a trace per turn**, so "which source answered this, and which model routed it" is
  answerable after the fact rather than only on screen.

**Backend** — instance 1's lines go here.

**Corpus and eval** — instance 3's lines go here.
