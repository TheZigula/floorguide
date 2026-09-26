// ApprovalCard.tsx — the human gate: the drafted work order, in full, with Approve and Reject.
// Owns: showing every field of the draft and the plain-English consequence of each button. It decides nothing itself.

import type { PendingApproval, Priority } from "../types";

const PRIORITY_LABEL: Record<Priority, string> = {
  low: "Low",
  normal: "Normal",
  high: "High",
};

export function ApprovalCard({
  approval,
  busy,
  settled,
  error,
  onApprove,
  onReject,
}: {
  approval: PendingApproval;
  busy: boolean;
  /** "approved" or "rejected" once the click has been answered; the buttons then go away. */
  settled: "approved" | "rejected" | null;
  error: string | null;
  onApprove: () => void;
  onReject: () => void;
}) {
  const order = approval.work_order;
  // CLAUDE.md, instance 2: Approve is live only when a fingerprint came with the draft.
  // Without one there is nothing tying the click to this exact text, so the click must not be offered.
  const canApprove = Boolean(approval.fingerprint) && !busy && settled === null;

  return (
    <section
      className={"approval" + (settled ? " approval--settled" : "")}
      aria-label="Work order awaiting your approval"
    >
      <header className="approval__header">
        <span className="approval__flag">Waiting for you</span>
        <h2 className="approval__title">Draft work order — nothing is filed yet</h2>
      </header>

      <dl className="approval__fields">
        <div>
          <dt>Machine</dt>
          <dd className="approval__mono">{order.asset_id}</dd>
        </div>
        <div>
          <dt>Task</dt>
          <dd>{order.task}</dd>
        </div>
        <div>
          <dt>Priority</dt>
          <dd>
            <span className={"badge badge--priority-" + order.priority}>
              {PRIORITY_LABEL[order.priority] ?? order.priority}
            </span>
          </dd>
        </div>
        <div>
          <dt>Because of</dt>
          <dd>{order.source_refs.length ? order.source_refs.join(", ") : "—"}</dd>
        </div>
        <div>
          <dt>Draft id</dt>
          <dd className="approval__mono approval__faint">{approval.draft_id}</dd>
        </div>
        <div>
          <dt>Fingerprint</dt>
          <dd className="approval__mono approval__faint">
            {approval.fingerprint || "missing — Approve is disabled"}
          </dd>
        </div>
      </dl>

      {error ? (
        <p className="approval__error" role="alert">
          {error}
        </p>
      ) : null}

      {settled === "approved" ? (
        <p className="approval__outcome approval__outcome--approved">
          You approved this. It is recorded as a simulation; nothing went to a real system.
        </p>
      ) : settled === "rejected" ? (
        <p className="approval__outcome approval__outcome--rejected">
          You rejected this. The draft was thrown away, nothing was filed, and this run has ended.
        </p>
      ) : (
        <div className="approval__actions">
          <div className="approval__action">
            <button
              type="button"
              className="btn btn--approve"
              onClick={onApprove}
              disabled={!canApprove}
            >
              Approve
            </button>
            <p className="approval__consequence">
              Files this work order and returns a confirmation number. In this prototype the filing
              is simulated — nothing leaves the system.
            </p>
          </div>
          <div className="approval__action">
            <button
              type="button"
              className="btn btn--reject"
              onClick={onReject}
              disabled={busy || settled !== null}
            >
              Reject
            </button>
            <p className="approval__consequence">
              Throws the draft away. Nothing is filed and this run ends. You can ask again from
              scratch.
            </p>
          </div>
        </div>
      )}
    </section>
  );
}
