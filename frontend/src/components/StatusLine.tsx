// StatusLine.tsx — the one line under the conversation that says what the system is doing right now.
// Owns: the wording of each phase; it reports what happened, it never guesses at a phase it cannot see.

export type Phase =
  | "idle"
  | "searching"
  | "deciding"
  | "awaiting_approval"
  | "computed"
  | "answered"
  | "refused"
  | "error";

const TEXT: Record<Phase, string> = {
  idle: "Ready. Ask about a procedure, a manual, or a quality standard.",
  searching: "Searching documents…",
  deciding: "Recording your decision…",
  awaiting_approval: "Waiting for your approval.",
  computed: "Computed by the service-due rule in code, not by the model.",
  answered: "Answered from the documents shown on the right.",
  refused: "Refused. Nothing was drafted and nothing was filed.",
  error: "Something went wrong. Nothing was filed.",
};

const BUSY: Phase[] = ["searching", "deciding"];

export function StatusLine({ phase }: { phase: Phase }) {
  const busy = BUSY.includes(phase);
  return (
    <div
      className={"statusline statusline--" + phase}
      role="status"
      aria-live="polite"
    >
      {busy ? <span className="statusline__dot" aria-hidden="true" /> : null}
      <span>{TEXT[phase]}</span>
    </div>
  );
}
