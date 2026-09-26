// SourcesPanel.tsx — the documents the last answer was grounded in.
// Owns: one card per DOCUMENT (its matching passages grouped under it), plus the drop notice and the empty state.

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

/** Retrieval returns chunks, and several can come from one document. Show the document once. */
interface Document {
  source_id: string;
  title: string;
  category: string;
  backends: string[];
  snippets: string[];
}

function groupByDocument(sources: Source[]): Document[] {
  const order: string[] = [];
  const byId = new Map<string, Document>();
  for (const source of sources) {
    let doc = byId.get(source.source_id);
    if (!doc) {
      doc = {
        source_id: source.source_id,
        title: source.title,
        category: (source.category ?? "").toLowerCase(),
        backends: [],
        snippets: [],
      };
      byId.set(source.source_id, doc);
      order.push(source.source_id);
    }
    if (!doc.backends.includes(source.source_backend)) doc.backends.push(source.source_backend);
    const snippet = source.snippet?.trim();
    if (snippet && !doc.snippets.includes(snippet)) doc.snippets.push(snippet);
  }
  return order.map((id) => byId.get(id)!);
}

function DocumentCard({ doc }: { doc: Document }) {
  return (
    <li className="source">
      <div className="source__head">
        <span className={"chip chip--" + doc.category}>
          {CATEGORY_LABEL[doc.category] ?? doc.category}
        </span>
        <span className="source__id">{doc.source_id}</span>
      </div>
      <div className="source__title">{doc.title}</div>
      {doc.snippets.map((snippet, index) => (
        <blockquote key={index} className="source__snippet">
          {snippet}
        </blockquote>
      ))}
      <div className="source__backend">
        {doc.snippets.length > 1 ? doc.snippets.length + " passages · " : ""}
        Found by {doc.backends.map((b) => BACKEND_LABEL[b] ?? b).join(" and ")}
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
  const documents = groupByDocument(sources);

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

      {documents.length === 0 ? (
        <p className="sources__empty">
          {hasAnswer
            ? "No sources were used for this answer."
            : "No sources yet. Ask a question and the documents behind the answer appear here."}
        </p>
      ) : (
        <ul className="sources__list">
          {documents.map((doc) => (
            <DocumentCard key={doc.source_id} doc={doc} />
          ))}
        </ul>
      )}
    </section>
  );
}
