// MemoryCard.tsx — what FloorGuide remembers about this user between conversations.
// Owns: showing memory.home_line, and saying plainly that it survives the "New conversation" button.

export function MemoryCard({ homeLine, userId }: { homeLine: string | null; userId: string }) {
  return (
    <section className="card memorycard" aria-label="What FloorGuide remembers">
      <h2 className="card__title">Remembered</h2>
      <div className="memorycard__body">
        <div className="memorycard__row">
          <span className="memorycard__label">Home line</span>
          {homeLine ? (
            <span className="chip chip--memory">{homeLine}</span>
          ) : (
            <span className="memorycard__empty">not set</span>
          )}
        </div>
        <p className="memorycard__note">
          {homeLine
            ? "Kept for " +
              userId +
              " across conversations. Starting a new conversation clears the transcript, not this."
            : 'Say "remember I am on Line 3" and it is kept for ' +
              userId +
              " across conversations."}
        </p>
      </div>
    </section>
  );
}
