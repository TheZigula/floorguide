// Chat.tsx — the conversation column: the message list, the status line, and the box you type in.
// Owns: how a turn looks on screen and when the Send button is live; it never calls the API itself.

import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { RoutedTo } from "../types";
import { RoutedToChip } from "./RoutedToChip";
import { StatusLine, type Phase } from "./StatusLine";

/** One line in the transcript. `note` turns are the system's own words about an approval or a rejection. */
export interface Turn {
  id: string;
  role: "user" | "assistant" | "note";
  text: string;
  routedTo?: RoutedTo;
  refused?: boolean;
  /** The draft this turn produced, so the approval card stays attached to it in the transcript. */
  approvalDraftId?: string;
  /** Anything the turn should carry inline, e.g. the approval card. */
  slot?: ReactNode;
}

/** Split an answer on blank lines so paragraphs survive; no markdown, no HTML, no dangerouslySetInnerHTML. */
function paragraphs(text: string): string[] {
  return text
    .split(/\n{2,}/)
    .map((block) => block.trim())
    .filter(Boolean);
}

function TurnView({ turn }: { turn: Turn }) {
  if (turn.role === "user") {
    return (
      <div className="turn turn--user">
        <div className="turn__who">You</div>
        <div className="bubble bubble--user">{turn.text}</div>
      </div>
    );
  }

  if (turn.role === "note") {
    return (
      <div className="turn turn--note">
        <div className="bubble bubble--note">{turn.text}</div>
      </div>
    );
  }

  return (
    <div className="turn turn--assistant">
      <div className="turn__who">FloorGuide</div>
      <div className={"bubble bubble--assistant" + (turn.refused ? " bubble--refused" : "")}>
        <div className="bubble__head">
          {turn.routedTo ? <RoutedToChip routedTo={turn.routedTo} /> : null}
          {turn.refused ? <span className="chip chip--refusal-flag">Refusal</span> : null}
        </div>
        {paragraphs(turn.text).map((block, index) => (
          <p key={index}>{block}</p>
        ))}
        {turn.slot}
      </div>
    </div>
  );
}

export function Chat({
  turns,
  phase,
  busy,
  onSend,
  suggestions,
}: {
  turns: Turn[];
  phase: Phase;
  busy: boolean;
  onSend: (message: string) => void;
  suggestions: string[];
}) {
  const [draft, setDraft] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, phase]);

  function submit() {
    const message = draft.trim();
    if (!message || busy) return;
    setDraft("");
    onSend(message);
  }

  return (
    <section className="chat" aria-label="Conversation">
      <div className="chat__scroll">
        {turns.length === 0 ? (
          <div className="chat__empty">
            <h2>Ask about anything on the floor.</h2>
            <p>
              FloorGuide reads this plant's safety procedures, maintenance manuals, and quality
              standards, and tells you which one the answer came from.
            </p>
            <div className="chat__suggestions">
              {suggestions.map((text) => (
                <button
                  key={text}
                  type="button"
                  className="suggestion"
                  onClick={() => onSend(text)}
                  disabled={busy}
                >
                  {text}
                </button>
              ))}
            </div>
          </div>
        ) : (
          turns.map((turn) => <TurnView key={turn.id} turn={turn} />)
        )}
        <div ref={endRef} />
      </div>

      <StatusLine phase={phase} />

      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <label className="visually-hidden" htmlFor="composer-input">
          Type your question
        </label>
        <textarea
          id="composer-input"
          className="composer__input"
          placeholder="Type your question, for example: is P-102 due for service?"
          rows={2}
          value={draft}
          disabled={busy}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
        />
        <button type="submit" className="composer__send" disabled={busy || draft.trim() === ""}>
          {busy ? "Working…" : "Send"}
        </button>
      </form>
    </section>
  );
}
