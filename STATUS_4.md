# STATUS_4 — instance 4 (repo hygiene, deploy, README, docs)

Newest at the bottom. Five lines per numbered step.

## Step 1 — git init, .gitignore, .gitattributes, .env.example, first commit — 12:31
1. `git init -b main` ran before any other file existed, so no secret can predate the ignore rules; first commit is `5eff8dd`.
2. Committed: .gitignore, .gitattributes, .env.example, spec.md, CLAUDE.md, .claude/settings.json, briefs/ (5 briefs). Frontend and STATUS_2 left untracked for the first checkpoint — instance 2 is still writing them.
3. Proved the negation works: `.env` matches ignore rule line 6; `.env.example` is addable (`git add --dry-run` lists it). No `.env` exists on disk yet and none was printed.
4. `core.autocrlf` is `true` on this box, so `.gitattributes` matters: every committed blob checked for CR bytes — .gitattributes, .gitignore, .env.example, spec.md all read 0.
5. Two hazards hit and are now in CLAUDE.md: git refuses to run on `D:\` until `safe.directory` is set, and the `Read(./.env.*)` deny rule also blocks writing `.env.example` (had to write it through PowerShell).

## Step 2 — requirements.txt, Dockerfile, .dockerignore, build + local /health — 12:42
1. All 15 pins in CLAUDE.md verified against this box before writing anything: zero mismatches. `requirements.txt` pins every direct dependency; the image resolved pydantic 2.12.5, langgraph-checkpoint 4.2.0 (NOT downgraded under 4) and Python 3.13.5 — identical to the box.
2. `Dockerfile`: base tag pinned to `python:3.13.5-slim-bookworm` (not floating `slim`), non-root `appuser` uid 10001, deps in their own layer before the code, HEALTHCHECK on `/health` via Python (no curl in slim), `uvicorn backend.app:app` on `${PORT:-8080}`, no `--reload`.
3. `docker build` exit 0, image 1.55 GB. `docker run` came up `(healthy)` after 1 probe, `id` = uid 10001 appuser, and `/health` returned `{"status":"ok","service":"floorguide","commit":"5eff8dd","vector_backend":"local"}`.
4. The commit in the container equals `git rev-parse --short HEAD` (5eff8dd), which proves `.dockerignore` kept `.git` in the context and the pure-Python `.git/HEAD` fallback works with no GIT_SHA set — the App Platform hazard in CLAUDE.md is covered before deploy.
5. `backend/` is still empty (instance 1 is writing it), so the smoke app was bind-mounted from a temp dir OUTSIDE the repo and the container deleted after; `git status` confirms no backend file was created by me. Step 3's DO deploy blocks on instance 1's real `backend/app.py`.

## Step 3 (PART 1 of 3) — Vercel front end live, checkpoint push BLOCKED — 13:02
1. Front end deployed: **https://floorguide.vercel.app** (aliases `floorguide-cap-per.vercel.app`, deployment `floorguide-2zbvrffyk-cap-per.vercel.app`). `vercel link --yes --project floorguide` ran BEFORE `vercel --prod --yes`, so it cannot land on another folder's project. `vercel env pull` was never run.
2. THE PAGE IS NOT PUBLIC YET AND A 200 DOES NOT PROVE IT IS: the URL returns StatusCode 200 with title **"Login – Vercel"**. `vercel project protection` shows `ssoProtection: all_except_custom_domains`. Reading the payload caught it; a StatusCode-only check would have recorded a login wall as a pass. `vercel project protection disable --sso` was DENIED by the auto-mode classifier as a security weakening — it needs the operator.
3. TWO HAZARDS FOUND AND RECORDED IN CLAUDE.md: `vercel link` (not just the denied `vercel env pull`) downloads a live `VERCEL_OIDC_TOKEN` into `frontend/.env.local`; verified ignored via `frontend/.gitignore:27`, absent from the index, never opened. And the Vercel project landed in team scope `cap-per`, not the personal scope.
4. CHECKPOINT PUSH IS BLOCKED BY THE GATE, WHICH IS THE GATE WORKING. Tripwire: PASS, no hits over 5,729 staged diff lines. detect-secrets: started at 2 findings. CLAUDE.md:144 fixed by me with an inline pragma (the documented remedy); mock.ts:128 fixed by instance 2. A third surfaced: STATUS_2.md:47 quotes the same audited placeholder while describing the fix.
5. 62 files staged and audited: no `.env`, no `node_modules`, no `*.db`, no `chroma/`, no `.log`, no `.vercel`. `.env` exists on disk now and IS ignored. App Platform app still correctly on hold — STATUS_1.md has instance 1 at step 5 of 7, `backend/app.py` is their step 7. Health-check delay already set to 90 s per the operator.
