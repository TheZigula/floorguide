// mock.ts — a scripted stand-in for the FastAPI backend, used until VITE_API_BASE is set.
// Owns: canned replies that obey the CLAUDE.md contract exactly, so the swap to the real API changes no component.

import {
  ApiError,
  type ApproveRequest,
  type ApproveResponse,
  type ChatRequest,
  type ChatResponse,
  type HealthResponse,
  type PendingApproval,
  type RejectRequest,
  type RejectResponse,
  type Source,
} from "../types";

/** Long enough that the status line is readable on camera, short enough not to feel broken. */
const THINKING_MS = 550;

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** Drafts this mock has handed out, so a reused id or a settled draft answers 409 like the real gate. */
type DraftState = { thread_id: string; fingerprint: string; settled: boolean };
const drafts = new Map<string, DraftState>();
/** Threads whose run ended on a rejection; a later approve on one is a 409. */
const rejectedThreads = new Set<string>();
const homeLines = new Map<string, string>();
let confirmationCounter = 1000;
let draftCounter = 1;

const SP01: Source = {
  source_id: "SP-01",
  title: "Lockout / Tagout — Line 3 Equipment",
  category: "safety",
  snippet:
    "Before any service on Line 3 equipment: notify the line, shut down at the local disconnect, isolate all energy sources, apply your personal lock and tag, then verify zero energy by attempting a start. Only the person who applied a lock may remove it.",
  source_backend: "openai",
};

const SP02: Source = {
  source_id: "SP-02",
  title: "PPE by Zone",
  category: "safety",
  snippet:
    "Zone A (press bay): safety glasses with side shields, cut-resistant gloves, hearing protection. Zone C (inspection): safety glasses only.",
  source_backend: "openai",
};

const SP03: Source = {
  source_id: "SP-03",
  title: "Emergency Stop and Restart After a Fault",
  category: "safety",
  snippet:
    "A restart after a fault requires lockout verification and supervisor sign-off. Interlocks are never bypassed, shortened, or deferred to restore production.",
  source_backend: "openai",
};

const MMP102: Source = {
  source_id: "MM-P102",
  title: "Hydraulic Press P-102 — Maintenance Manual",
  category: "maintenance",
  snippet:
    "4.2 Grinding noise under load indicates pump cavitation. Check the hydraulic filter first; a loaded filter starves the pump. Service table: hydraulic filter every 250 h, seal inspection every 500 h, hydraulic oil every 1000 h.",
  source_backend: "openai",
};

const QRP102: Source = {
  source_id: "QR-P102",
  title: "P-102 Quick-Reference Card (superseded)",
  category: "maintenance",
  snippet:
    "Hydraulic filter: every 500 h. Card issued two years before the current manual revision.",
  source_backend: "local",
};

const MMC7: Source = {
  source_id: "MM-C7",
  title: "Conveyor C-7 — Maintenance Manual",
  category: "maintenance",
  snippet:
    "2.1 Bearing lubrication every 200 h. Belt tension checked at the same interval; re-tension before lubricating.",
  source_backend: "openai",
};

const QC01: Source = {
  source_id: "QC-01",
  title: "Dimensional Tolerance — Bracket B-40",
  category: "quality",
  snippet:
    "Bracket B-40 overall length: 120.00 mm plus or minus 0.15. Parts outside tolerance are held under QC-03 and tagged before they leave the cell.",
  source_backend: "openai",
};

const QC02: Source = {
  source_id: "QC-02",
  title: "Inspection Frequency",
  category: "quality",
  snippet:
    "Inspect the first piece of every run, then every 50th piece thereafter. Record each reading on the run sheet.",
  source_backend: "openai",
};

function base(thread_id: string, user_id: string): ChatResponse {
  return {
    thread_id,
    worker: "retriever",
    routed_to: "maintenance",
    answer: "",
    sources: [],
    rule_result: null,
    pending_approval: null,
    dropped_chunks: 0,
    refused: false,
    memory: { home_line: homeLines.get(user_id) ?? null },
  };
}

function newDraft(
  thread_id: string,
  work_order: PendingApproval["work_order"],
): PendingApproval {
  const draft_id = "draft-" + String(draftCounter++).padStart(4, "0");
  // The real backend fingerprints with the first 12 hex of a sha256 over the work-order fields.
  // The mock only needs a stable, draft-specific string for the Approve button to carry back.
  const seed = [work_order.asset_id, work_order.task, work_order.priority, draft_id].join("|");
  let hash = 7;
  for (const ch of seed) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  // Zero-padded to 12 characters so the card shows a fingerprint the same width as the real
  // backend's. Padding with "0" avoids putting any hex-looking placeholder in the source.
  const fingerprint = hash.toString(16).padStart(12, "0");
  drafts.set(draft_id, { thread_id, fingerprint, settled: false });
  return { draft_id, fingerprint, work_order };
}

export async function mockChat(req: ChatRequest): Promise<ChatResponse> {
  await wait(THINKING_MS);
  const text = req.message.toLowerCase();
  const out = base(req.thread_id, req.user_id);

  // --- memory: "remember I am on Line 3" ------------------------------------
  const lineMatch = text.match(/line\s*([0-9]+)/);
  if (text.includes("remember") && lineMatch) {
    const line = "Line " + lineMatch[1];
    homeLines.set(req.user_id, line);
    out.worker = "remember";
    out.routed_to = "memory";
    out.memory = { home_line: line };
    out.answer =
      "Saved. I will treat " + line + " as your home line, including in new conversations.";
    return out;
  }
  if (/(what line|my home line|which line am i)/.test(text)) {
    const line = homeLines.get(req.user_id);
    out.worker = "remember";
    out.routed_to = "memory";
    out.answer = line
      ? "You are on " + line + ". I kept that from an earlier conversation."
      : "I do not have a home line for you yet. Tell me, for example: remember I am on Line 3.";
    return out;
  }

  // --- refusals -------------------------------------------------------------
  if (
    /(skip|bypass|shorten|defer|ignore|override)[^.?]*(lockout|safety|interlock|procedure|guard)/.test(text) ||
    /(lockout|interlock|guard)[^.?]*(skip|bypass|off|override)/.test(text)
  ) {
    out.worker = "retriever";
    out.routed_to = "refused";
    out.refused = true;
    out.sources = [SP01, SP03];
    out.answer =
      "REFUSED: I cannot advise skipping, shortening, or deferring a lockout step, and I cannot draft a work order that bypasses one.\n\n" +
      "Here is what the procedure requires instead. SP-01 (Lockout / Tagout, Line 3): isolate every energy source, apply your personal lock and tag, then verify zero energy by attempting a start. SP-03 (Emergency Stop and Restart After a Fault): a restart after a fault requires lockout verification and supervisor sign-off; interlocks are never bypassed to restore production.\n\n" +
      "Only a person can sign off on a restart. I cannot approve anything myself.";
    return out;
  }
  if (/(disciplin|fired|reprimand|who was blamed|personnel|hr file)/.test(text)) {
    out.worker = "retriever";
    out.routed_to = "refused";
    out.refused = true;
    out.answer =
      "REFUSED: That is outside what FloorGuide covers. I answer from the safety procedures, maintenance manuals, and quality standards in this plant's document set. I hold no personnel records and no disciplinary history, and I will not guess at one.";
    return out;
  }

  // --- the rule, and the hand-off to a draft in the same turn ---------------
  if (/m-?31/.test(text)) {
    out.worker = "analyst";
    out.routed_to = "rule";
    out.refused = true;
    out.answer =
      "REFUSED: I cannot compute a service status for M-31. Field meter_hours_now is 2950 while meter_hours_at_last_service is 3100 — the meter reading went backwards, so the hours since service cannot be calculated. Have the reading re-taken, or the meter replacement logged, and ask me again.";
    return out;
  }
  if (/p-?102/.test(text) && /(due|service|overdue|schedule|filter change)/.test(text)) {
    out.worker = "actor";
    out.routed_to = "work_order";
    out.rule_result = {
      asset_id: "P-102",
      hours_since_service: 280,
      hours_remaining: -30,
      status: "OVERDUE",
      overdue_by: 30,
    };
    out.sources = [MMP102];
    out.pending_approval = newDraft(req.thread_id, {
      asset_id: "P-102",
      task: "Replace hydraulic filter",
      priority: "high",
      source_refs: ["MM-P102 4.2"],
    });
    out.answer =
      "P-102 is OVERDUE for service by 30 hours. The meter reads 4180 h and the last service was at 3900 h, which is 280 hours; the manual's interval for the hydraulic filter is 250 h (MM-P102 4.2). Those numbers come from the service-due calculation, not from me.\n\n" +
      "I have drafted a work order. Nothing is filed until you approve it.";
    return out;
  }
  if (/c-?7/.test(text)) {
    out.worker = "actor";
    out.routed_to = "work_order";
    out.rule_result = {
      asset_id: "C-7",
      hours_since_service: 190,
      hours_remaining: 10,
      status: "DUE_SOON",
      overdue_by: 0,
    };
    out.sources = [MMC7];
    out.pending_approval = newDraft(req.thread_id, {
      asset_id: "C-7",
      task: "Lubricate bearings",
      priority: "normal",
      source_refs: ["MM-C7 2.1"],
    });
    out.answer =
      "C-7 is DUE SOON: 10 hours remain. The meter reads 1590 h against a last service at 1400 h, which is 190 hours of the manual's 200 h bearing-lubrication interval (MM-C7 2.1). The calculation produced those numbers.\n\n" +
      "I have drafted a work order. Nothing is filed until you approve it.";
    return out;
  }

  // --- retrieval ------------------------------------------------------------
  if (/(lockout|tagout|lock out|de-?energi|zero energy|restart after|e-?stop)/.test(text)) {
    out.routed_to = "safety";
    out.sources = [SP01, SP03];
    out.answer =
      "SP-01 (Lockout / Tagout, Line 3 equipment) gives six steps: notify the line, shut down at the local disconnect, isolate every energy source, apply your personal lock and tag, verify zero energy by attempting a start, and only then begin work. Only the person who applied a lock may remove it.\n\n" +
      "If this follows a fault, SP-03 also requires lockout verification and supervisor sign-off before the restart.";
    return out;
  }
  if (/(ppe|goggles|glasses|glove|hearing protection|zone)/.test(text)) {
    out.routed_to = "safety";
    out.sources = [SP02];
    out.answer =
      "SP-02 sets PPE by zone. In the press bay (Zone A): safety glasses with side shields, cut-resistant gloves, and hearing protection. In inspection (Zone C): safety glasses only.";
    return out;
  }
  if (/(tolerance|b-?40|dimension|inspect|nonconform|hold tag)/.test(text)) {
    out.routed_to = "quality";
    out.sources = [QC01, QC02];
    out.answer =
      "Bracket B-40's overall length tolerance is 120.00 mm plus or minus 0.15 (QC-01). QC-02 sets the checking frequency: the first piece of every run, then every 50th piece. Anything outside tolerance is held and tagged under QC-03.";
    return out;
  }
  if (/(filter|interval|how often|grinding|noise|cavitat|hydraulic|pump)/.test(text)) {
    out.routed_to = "maintenance";
    out.sources = [MMP102, QRP102];
    // The Line 3 tip sheet is retrieved by this query and thrown out before the model reads it.
    out.dropped_chunks = 1;
    out.answer =
      "Change the hydraulic filter on P-102 every 250 hours. That is MM-P102, the current manual, and it governs.\n\n" +
      "The quick-reference card QR-P102 says 500 hours. It is out of date — it was issued two years before this manual revision — so do not use it. The manual outranks the card derived from it.\n\n" +
      "On the grinding noise: MM-P102 4.2 attributes grinding under load to pump cavitation and says to check the hydraulic filter first, because a loaded filter starves the pump.";
    return out;
  }

  out.routed_to = "maintenance";
  out.sources = [MMP102];
  out.answer =
    "I searched the maintenance manuals and the closest passage is MM-P102 4.2 on P-102. Name a machine (P-102, C-7, M-31), a procedure (lockout, PPE), or a quality check (B-40 tolerance) and I will route to the right binder.";
  return out;
}

export async function mockApprove(req: ApproveRequest): Promise<ApproveResponse> {
  await wait(300);
  const draft = drafts.get(req.draft_id);
  if (!draft || draft.thread_id !== req.thread_id) {
    throw new ApiError(409, "No such draft on this thread.");
  }
  if (rejectedThreads.has(req.thread_id) || draft.settled) {
    throw new ApiError(409, "That draft was already settled. Nothing further was filed.");
  }
  if (draft.fingerprint !== req.fingerprint) {
    throw new ApiError(409, "Fingerprint mismatch: the draft changed since it was shown to you.");
  }
  draft.settled = true;
  const confirmation_id = "WO-SIM-" + confirmationCounter++;
  return {
    status: "approved",
    confirmation_id,
    answer:
      "Approved. Work order " +
      confirmation_id +
      " is recorded as a simulation — nothing was filed to a real system.",
  };
}

export async function mockReject(req: RejectRequest): Promise<RejectResponse> {
  await wait(300);
  const draft = drafts.get(req.draft_id);
  if (!draft || draft.thread_id !== req.thread_id) {
    throw new ApiError(409, "No such draft on this thread.");
  }
  if (draft.settled) {
    throw new ApiError(409, "That draft was already settled.");
  }
  draft.settled = true;
  rejectedThreads.add(req.thread_id);
  return {
    status: "rejected",
    answer: "Rejected. The draft was thrown away and nothing was filed. This run has ended.",
  };
}

export async function mockHealth(): Promise<HealthResponse> {
  await wait(120);
  return {
    status: "ok",
    service: "floorguide (mock)",
    commit: "mock",
    vector_backend: "openai",
  };
}
