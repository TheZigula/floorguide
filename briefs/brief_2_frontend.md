# Instance 2 — front end, designed from the spec, from minute 8

Read `CLAUDE.md` first. You own `frontend/` only. Do not touch `backend/`, `eval/`, `data/`, `docs/`, or deploy files.

The backend API is being built in parallel; its contract is in `CLAUDE.md` under Key File Locations (`/health`, `/chat`, `/approve`, `/reject`). Build against that contract with a mocked client first, then swap to the real base URL from an env var (`VITE_API_BASE`) when instance 1 reports the API is up.

Build, in this order, printing a five-line summary after each step. KEEP THE TREE BUILDABLE AFTER EVERY STEP: run `npm run build` and confirm green before reporting a step done; a deploy can land in any window, and an `App.tsx` importing a file you deleted is a red deploy.

1. Vite + React + TypeScript scaffold, with exactly this command (npm 11 swallows the `--template` flag when given through `npm create`): `npx create-vite@latest frontend --template react-ts`. If it prompts anyway, choose React, then TypeScript, and answer NO to "install and start now" so the terminal is not taken over by the dev server; run `npm install` yourself. Plain CSS or Tailwind, whichever installs in one command. No component library that needs configuration.
2. `Chat`: a message list and an input a non-technical person can use without instructions. The user's turn, the assistant's answer, and a visible status line ("searching documents", "computing", "waiting for your approval").
3. `SourcesPanel`: for every answer, the source documents it was grounded in (title, snippet, and `source_backend`: `openai` embeddings or the `local` fallback). Empty state says "no sources were used for this answer" rather than hiding the panel.
4. `ApprovalCard`: when the backend returns a pending approval, render the drafted action in full with two buttons, Approve and Reject, and the plain-English consequence of each under the button. Approve calls `/approve`; Reject calls `/reject`. The card must be impossible to miss: it is the guardrail made visible and it is the whole point of the demo.
5. A small banner in the header: "Prototype. Documents are synthetic. Nothing is sent or filed without your approval."
6. Wire to the real API. Show the four states live: an answer with sources, a computed rule result, an approval that resumes, a rejection that ends the run.

After EVERY step, append your five-line summary to `STATUS_2.md` at the repo root (newest at the bottom, with the time) and keep working; the operator reads the status files, not your scrollback. You do NOT run `git add`, `git commit`, or `git push`; instance 4 is the only committer (four writers to one index collide on `index.lock`); `git status` and `git diff` are fine. Constraints: every command you run or hand me starts from an absolute path (`cd D:\...` on its own line); never chain `cd` with an action, because PowerShell 5.1 continues past a failed `cd`.  pinned dependencies in `package.json`; no keys anywhere in the front end; the API base URL from `VITE_API_BASE` only. END by printing a file map, one line per file you created, in plain English, and the one-line build and dev commands. Then wait.
