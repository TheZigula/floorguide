// SourcesPanel.tsx — the documents the last answer was grounded in.
// Owns: one card per source with its title, kind, passage, and which embedding index found it; and the empty state.

import type { Source } from "../types";

/** Plain English for the category stamped on every chunk at ingest. */
const CATEGORY_LABEL: Record<string, string> = {
  safety: "Safety procedure",
  maintenance: "Maintenance manual",
  quality: "Quality standard",
  asset_record: "Asset record",
};

/** Plain English for which vector index answered. The local one is the no-key fallback. */
const BACKEND_LABEL: Record<string, string> = {
  openai: "OpenAI embeddings",
  local: "local fallback embeddings",
};

function SourceCard({ source }: { source: Source }) {
  const category = source.category?.toLowerCase() ?? "";
  return (
    <li className="source">
      <div className="source__head">
        <span className={"chip chip--" + category}>
          {CATEGORY_LABEL[category] ?? source.category}
        </span>
        <span className="source__id">{source.source_id}</span>
      </div>
      <div className="source__title">{source.title}</div>
      <blockquote className="source__snippet">{source.snippet}</blockquote>
      <div className="source__backend">
        Found by {BACKEND_LABEL[source.source_backend] ?? source.source_backend}
      </div>
    </li>
  );
}

export function SourcesPanel({
  sources,
  droppedChunks,
  hasAnswer,
}: {
  sources: Source[];
  droppedChunks: number;
  hasAnswer: boolean;
}) {
  return (
    <section className="card sources" aria-label="Sources">
      <h2 className="card__title">Sources</h2>

      {droppedChunks > 0 ? (
        <div className="dropped" role="note">
          <strong>
            {droppedChunks} document{droppedChunks === 1 ? "" : "s"} dropped.
          </strong>{" "}
          A retrieved document tried to give FloorGuide instructions. It was thrown out before the
          model read it, so it did not shape this answer.
        </div>
      ) : null}

      {sources.length === 0 ? (
        <p className="sources__empty">
          {hasAnswer
            ? "No sources were used for this answer."
            : "No sources yet. Ask a question and the documents behind the answer appear here."}
        </p>
      ) : (
        <ul className="sources__list">
          {sources.map((source, index) => (
            <SourceCard key={source.source_id + "-" + index} source={source} />
          ))}
        </ul>
      )}
    </section>
  );
}
