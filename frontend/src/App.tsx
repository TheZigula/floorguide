// App.tsx — the page shell and the single owner of conversation state.
// Owns: the header, the chat column, the evidence rail, and every call into the API client.

import { useCallback, useMemo, useState } from "react";
import "./App.css";
import "./components/components.css";
import { sendApprove, sendChat, sendReject } from "./api/client";
import { ApprovalCard } from "./components/ApprovalCard";
import { Chat, type Turn } from "./components/Chat";
import { HealthChip } from "./components/HealthChip";
import { MemoryCard } from "./components/MemoryCard";
import { RuleCard } from "./components/RuleCard";
import { SourcesPanel } from "./components/SourcesPanel";
import type { Phase } from "./components/StatusLine";
import { ApiError, type ChatResponse, type PendingApproval } from "./types";

/** CLAUDE.md, instance 2: the demo user is always "dana". */
const USER_ID = "dana";

/** One conversation = one thread id. "New conversation" mints a fresh one; memory must survive it. */
function newThreadId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return "thread-" + Math.random().toString(36).slice(2, 10);
}

let turnCounter = 0;
const nextTurnId = () => "turn-" + ++turnCounter;

const SUGGESTIONS = [
  "What are the lockout steps for Line 3?",
  "P-102 is making a grinding noise — what do I check?",
  "What is the tolerance on bracket B-40?",
  "Is P-102 due for service?",
];

/**
 * One drafted work order and what has happened to it. Kept per draft_id rather than
 * "the latest one", so an approval answered several turns later still finds its card.
 */
interface ApprovalState {
  approval: PendingApproval;
  settled: "approved" | "rejected" | null;
  error: string | null;
}

/**
 * Turn a failed call into something a supervisor can act on rather than a status code.
 * Every branch says what did NOT happen, because the thing worth knowing about a failure
 * here is that no work order was drafted or filed.
 */
function failureText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 503) {
      return (
        "FloorGuide cannot search the plant documents right now, so it has not answered. " +
        "Nothing was drafted and nothing was filed. This is the document index, not your question — " +
        "try again shortly. (" + error.message + ")"
      );
    }
    if (error.status === 0) {
      return error.message + " Nothing was drafted and nothing was filed.";
    }
    return error.message + " Nothing was drafted and nothing was filed.";
  }
  return "FloorGuide could not be reached. Nothing was drafted and nothing was filed.";
}

/** What the status line should say once a reply has landed. */
function phaseFor(response: ChatResponse): Phase {
  if (response.refused) return "refused";
  if (response.pending_approval) return "awaiting_approval";
  if (response.rule_result) return "computed";
  return "answered";
}

export default function App() {
  const [threadId, setThreadId] = useState(newThreadId);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [last, setLast] = useState<ChatResponse | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [busy, setBusy] = useState(false);
  const [approvals, setApprovals] = useState<Record<string, ApprovalState>>({});
  /** Deliberately NOT cleared by "New conversation": that is the cross-thread memory demo. */
  const [homeLine, setHomeLine] = useState<string | null>(null);

  const patchApproval = useCallback((draftId: string, patch: Partial<ApprovalState>) => {
    setApprovals((prior) => {
      const current = prior[draftId];
      if (!current) return prior;
      return { ...prior, [draftId]: { ...current, ...patch } };
    });
  }, []);

  const handleSend = useCallback(
    async (message: string) => {
      setTurns((prior) => [...prior, { id: nextTurnId(), role: "user", text: message }]);
      setPhase("searching");
      setBusy(true);
      try {
        const response = await sendChat({ thread_id: threadId, user_id: USER_ID, message });
        setLast(response);
        setHomeLine(response.memory?.home_line ?? null);
        const pending = response.pending_approval;
        if (pending) {
          setApprovals((prior) => ({
            ...prior,
            [pending.draft_id]: { approval: pending, settled: null, error: null },
          }));
        }
        setTurns((prior) => [
          ...prior,
          {
            id: nextTurnId(),
            role: "assistant",
            text: response.answer,
            routedTo: response.routed_to,
            refused: response.refused,
            approvalDraftId: pending?.draft_id,
          },
        ]);
        setPhase(phaseFor(response));
      } catch (error) {
        setTurns((prior) => [
          ...prior,
          { id: nextTurnId(), role: "note", text: failureText(error), tone: "error" },
        ]);
        setPhase("error");
      } finally {
        setBusy(false);
      }
    },
    [threadId],
  );

  const handleApprove = useCallback(
    async (draftId: string) => {
      const entry = approvals[draftId];
      if (!entry || entry.settled) return;
      patchApproval(draftId, { error: null });
      setPhase("deciding");
      setBusy(true);
      try {
        const response = await sendApprove({
          thread_id: threadId,
          draft_id: draftId,
          fingerprint: entry.approval.fingerprint,
        });
        patchApproval(draftId, { settled: "approved" });
        setTurns((prior) => [
          ...prior,
          {
            id: nextTurnId(),
            role: "note",
            text: response.answer + " Confirmation id: " + response.confirmation_id + ".",
          },
        ]);
        setPhase("answered");
      } catch (error) {
        // A 409 means the gate is still waiting: the draft stays open, the card stays live.
        const message =
          error instanceof ApiError
            ? error.message
            : "The approval could not be sent. Nothing was filed.";
        patchApproval(draftId, { error: message });
        setPhase("awaiting_approval");
      } finally {
        setBusy(false);
      }
    },
    [approvals, patchApproval, threadId],
  );

  const handleReject = useCallback(
    async (draftId: string) => {
      const entry = approvals[draftId];
      if (!entry || entry.settled) return;
      patchApproval(draftId, { error: null });
      setPhase("deciding");
      setBusy(true);
      try {
        const response = await sendReject({ thread_id: threadId, draft_id: draftId });
        patchApproval(draftId, { settled: "rejected" });
        setTurns((prior) => [
          ...prior,
          { id: nextTurnId(), role: "note", text: response.answer },
        ]);
        setPhase("answered");
      } catch (error) {
        const message =
          error instanceof ApiError
            ? error.message
            : "The rejection could not be sent. Nothing was filed.";
        patchApproval(draftId, { error: message });
        setPhase("awaiting_approval");
      } finally {
        setBusy(false);
      }
    },
    [approvals, patchApproval, threadId],
  );

  /**
   * Hang each approval card on the turn that produced it, so the transcript keeps its history.
   * A thread paused at the gate re-returns the SAME draft on every later turn, so the card goes
   * only on the most recent turn carrying that draft id — never two live Approve buttons at once.
   */
  const renderedTurns = useMemo(() => {
    const lastTurnForDraft = new Map<string, string>();
    for (const turn of turns) {
      if (turn.approvalDraftId) lastTurnForDraft.set(turn.approvalDraftId, turn.id);
    }
    return turns.map((turn) => {
      const entry = turn.approvalDraftId ? approvals[turn.approvalDraftId] : undefined;
      if (!entry || lastTurnForDraft.get(turn.approvalDraftId!) !== turn.id) {
        return { ...turn, approvalDraftId: undefined };
      }
      return {
        ...turn,
        slot: (
          <ApprovalCard
            approval={entry.approval}
            busy={busy}
            settled={entry.settled}
            error={entry.error}
            onApprove={() => handleApprove(entry.approval.draft_id)}
            onReject={() => handleReject(entry.approval.draft_id)}
          />
        ),
      };
    });
  }, [turns, approvals, busy, handleApprove, handleReject]);

  function startNewConversation() {
    setThreadId(newThreadId());
    setTurns([]);
    setLast(null);
    setApprovals({});
    setPhase("idle");
  }

  return (
    <div className="app">
      <header className="app__header">
        <div className="app__titlerow">
          <h1 className="app__title">FloorGuide</h1>
          <span className="app__subtitle">Northbridge Fabrication, Plant 2 — Line 3</span>
          <div className="app__headeractions">
            <HealthChip />
            <button type="button" onClick={startNewConversation} disabled={busy}>
              New conversation
            </button>
          </div>
        </div>
        <div className="app__banner" role="note">
          <strong>Prototype.</strong> Documents are synthetic. Nothing is sent or filed without your
          approval.
        </div>
      </header>

      <div className="app__body">
        <main className="app__main">
          <Chat
            turns={renderedTurns}
            phase={phase}
            busy={busy}
            onSend={handleSend}
            suggestions={SUGGESTIONS}
          />
        </main>
        <aside className="app__rail">
          {last?.rule_result ? <RuleCard result={last.rule_result} /> : null}
          <SourcesPanel
            sources={last?.sources ?? []}
            droppedChunks={last?.dropped_chunks ?? 0}
            hasAnswer={last !== null}
          />
          <MemoryCard homeLine={homeLine} userId={USER_ID} />
        </aside>
      </div>
    </div>
  );
}
