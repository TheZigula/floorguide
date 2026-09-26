# backend/tools/vector_search.py -- owns the only read path into the corpus: Chroma search
# over corpus_openai, the corpus_local fallback, and the screen every chunk passes before a model sees it.
"""Vector search.

Two collections, one per embedding model, because vectors from different models must never
share an index: `corpus_openai` (OpenAI text-embedding-3-small) is tried first, and if the
embedding call fails for any reason -- no key, no network, no collection -- the query falls
back to `corpus_local` (Chroma's default local embedding function). Every hit carries the
backend that produced it, so the screen can show which one answered.

The corpus is UNTRUSTED TEXT. Every candidate chunk goes through `guards.screen_chunks`
here, inside the search, not in a prompt and not in the node -- so there is no path by
which a chunk reaches a model unscreened. A document that tries to give the system orders
is dropped and counted.

A chunk without its source metadata is not returned at all. An answer the supervisor cannot
trace back to a document id and title is worth nothing next to a running machine.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional

from backend import guards

log = guards.get_logger(__name__)

Category = Literal["safety", "maintenance", "quality", "asset_record"]

CHROMA_DIR = os.getenv("CHROMA_DIR", str(Path(__file__).resolve().parent.parent.parent / "chroma"))
OPENAI_COLLECTION = os.getenv("CHROMA_COLLECTION_OPENAI", "corpus_openai")
LOCAL_COLLECTION = os.getenv("CHROMA_COLLECTION_LOCAL", "corpus_local")
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")

SNIPPET_CHARS = 240

# The screen sweep (see vector_search) counts an instruction-shaped chunk only when it ranked
# ALONGSIDE the passages actually used -- within this margin of the worst candidate the
# category filter returned. Without it, a tip sheet sitting in the corpus would report itself
# dropped on every unrelated question, and a count that is always 1 tells the supervisor nothing.
SWEEP_MARGIN = 1.10
# Used only when the filtered pass returned nothing at all, so there is no relative yardstick.
SWEEP_FALLBACK_DISTANCE = 0.60

# THE SHELF GUESS IS A HINT, NEVER A WALL (CLAUDE.md, minute 58). A category guess narrows the
# search; it does not get to hide the answer. If the best chunk on the guessed shelf is missing
# or further away than this floor, the search widens to every category and re-ranks.
# The floors are per backend because the two embedding models put their distances on different
# scales, and they are MEASURED against this corpus, not guessed: on corpus_openai a
# right-shelf answer lands at 0.29-0.49 and the known misroute (lockout steps sent to
# maintenance) sat at 0.580 while SP-01 was 0.371 unfiltered; on corpus_local the same pair was
# 0.909 and 0.864. Widening is cheap and safe: it merges and re-ranks by distance, so a good
# filtered hit still wins, and routed_to follows whichever source is actually used.
SIMILARITY_FLOOR = {
    "openai": float(os.getenv("SIMILARITY_FLOOR_OPENAI", "0.50")),
    "local": float(os.getenv("SIMILARITY_FLOOR_LOCAL", "0.85")),
}


@lru_cache(maxsize=1)
def _client():
    """One Chroma client per process.

    chromadb 1.5.9 caches a client per path and REFUSES a second one for the same path with
    different settings ("An instance of Chroma already exists for ... with different
    settings"). backend/ingest.py runs in this same process at startup and builds its client
    with Settings(anonymized_telemetry=False), so this matches it; if some other caller got
    there first with different settings, fall back rather than leave the search dead.
    """
    import chromadb
    from chromadb.config import Settings

    try:
        return chromadb.PersistentClient(path=CHROMA_DIR, settings=Settings(anonymized_telemetry=False))
    except ValueError as exc:
        if "already exists" not in str(exc):
            raise
        log.warning("reusing the Chroma client already built for %s (%s)", CHROMA_DIR, exc)
        return chromadb.PersistentClient(path=CHROMA_DIR)


@lru_cache(maxsize=1)
def _openai_ef():
    from chromadb.utils import embedding_functions

    # api_key_env_var is the NAME of an environment variable, not a secret. detect-secrets
    # reads it as a Secret Keyword, so the line is annotated rather than the gate loosened.
    return embedding_functions.OpenAIEmbeddingFunction(
        api_key_env_var="OPENAI_API_KEY",  # pragma: allowlist secret
        model_name=EMBED_MODEL,
    )


@lru_cache(maxsize=1)
def _local_ef():
    from chromadb.utils import embedding_functions

    return embedding_functions.DefaultEmbeddingFunction()


def _get_collection(name: str, ef: Any):
    client = _client()
    try:
        return client.get_collection(name=name, embedding_function=ef)
    except TypeError:
        return client.get_collection(name=name)


def _rows(result: dict, source_backend: str) -> list[dict]:
    """Flatten one Chroma query result into rows, dropping anything without its source metadata."""
    ids = (result.get("ids") or [[]])[0]
    docs = (result.get("documents") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    dists = (result.get("distances") or [[]])[0]

    rows: list[dict] = []
    for i, chunk_id in enumerate(ids):
        meta = metas[i] if i < len(metas) and isinstance(metas[i], dict) else {}
        text = docs[i] if i < len(docs) else ""
        source_id = meta.get("source_id")
        title = meta.get("title")
        if not source_id or not title:
            log.warning("chunk %s has no source metadata; not returning it", chunk_id)
            continue
        rows.append(
            {
                "chunk_id": chunk_id,
                "source_id": str(source_id),
                "title": str(title),
                "category": str(meta.get("category") or "uncategorised"),
                "authority": int(meta.get("authority") or 2),
                "stale": bool(meta.get("stale")),
                "text": text or "",
                "snippet": _snippet(text or ""),
                "distance": float(dists[i]) if i < len(dists) and dists[i] is not None else 0.0,
                "source_backend": source_backend,
            }
        )
    return rows


def _snippet(text: str) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= SNIPPET_CHARS else flat[: SNIPPET_CHARS - 1].rstrip() + "…"


def _query(collection, embedding: list[float], k: int, category: Optional[str]) -> dict:
    kwargs: dict[str, Any] = {
        "query_embeddings": [embedding],
        "n_results": k,
        "include": ["documents", "metadatas", "distances"],
    }
    if category:
        kwargs["where"] = {"category": category}
    return collection.query(**kwargs)


def vector_search(
    query: str,
    category: Optional[Category] = None,
    k: int = 4,
) -> dict:
    """Search the corpus. Returns {"hits": [...], "dropped": int, "source_backend": str}.

    `category` narrows the search to one kind of source, which is how a safety question is
    answered out of the safety procedures. A SECOND, unfiltered pass runs alongside it for
    one purpose only: an instruction-shaped document must be caught and counted even when
    the category filter would have hidden it. Chunks from that pass are screened and
    counted, never answered from.
    """
    backend, embedding = _embed(query)
    if embedding is None or len(embedding) == 0:
        log.error("no embedding backend available; returning no hits")
        return _result([], 0, backend, False, category)

    collection_name = OPENAI_COLLECTION if backend == "openai" else LOCAL_COLLECTION
    ef = _openai_ef() if backend == "openai" else _local_ef()
    try:
        collection = _get_collection(collection_name, ef)
    except Exception as exc:  # noqa: BLE001 - a missing collection is a normal cold start
        log.error("collection %s unavailable: %s", collection_name, exc)
        return _result([], 0, backend, False, category)

    # The whole-corpus neighbourhood is fetched either way: it is the widened search when the
    # shelf guess misses, and the screen sweep when it holds. One embedding, two index lookups.
    unfiltered = _rows(_query(collection, embedding, k, None), backend)

    if not category:
        kept, dropped = guards.screen_chunks(unfiltered)
        return _result(kept[:k], dropped, backend, False, None)

    filtered = _rows(_query(collection, embedding, k, category), backend)
    kept, dropped = guards.screen_chunks(filtered)
    floor = SIMILARITY_FLOOR.get(backend, SIMILARITY_FLOOR["openai"])

    if kept and kept[0]["distance"] <= floor:
        # The guess held. Screen the wider neighbourhood anyway, so an instruction-shaped
        # document is still caught and counted even though the category filter hid it.
        seen = {row["chunk_id"] for row in filtered}
        cutoff = max((r["distance"] for r in filtered), default=None)
        limit = cutoff * SWEEP_MARGIN if cutoff is not None else SWEEP_FALLBACK_DISTANCE
        _, swept_out = guards.screen_chunks(
            [r for r in unfiltered if r["chunk_id"] not in seen and r["distance"] <= limit]
        )
        return _result(kept[:k], dropped + swept_out, backend, False, category)

    # The guess was a hint, not a wall. Nothing on that shelf, or nothing close enough: search
    # every category and re-rank. Refusing here would refuse a question the corpus answers.
    log.info(
        "widening past the %s shelf (best kept %s, floor %s); searching every category",
        category,
        format(kept[0]["distance"], ".3f") if kept else "none",
        format(floor, ".2f"),
    )
    merged: dict = {}
    for row in filtered + unfiltered:
        merged.setdefault(row["chunk_id"], row)
    ordered = sorted(merged.values(), key=lambda r: r["distance"])
    kept, dropped = guards.screen_chunks(ordered)
    return _result(kept[:k], dropped, backend, True, category)


def _result(hits: list[dict], dropped: int, backend: str, widened: bool, guess: Optional[str]) -> dict:
    """`category_used` is the category of the BEST SOURCE ACTUALLY USED, so the label the screen
    shows is always true, rather than the category a classifier guessed before searching."""
    log.info(
        "vector_search backend=%s guess=%s widened=%s hits=%s dropped=%s used=%s",
        backend, guess, widened, len(hits), dropped, hits[0]["category"] if hits else None,
    )
    return {
        "hits": hits,
        "dropped": dropped,
        "source_backend": backend,
        "widened": widened,
        "category_used": hits[0]["category"] if hits else None,
    }


def _embed(query: str) -> tuple[str, Any]:
    """Embed with OpenAI; fall back to the local model. Never mix the two in one index.

    The vector is handed back exactly as the embedding function produced it (a numpy
    array): chromadb 1.5.9 rejects a list of numpy scalars, which is what list() makes.
    """
    try:
        return "openai", _openai_ef()([query])[0]
    except Exception as exc:  # noqa: BLE001 - no key, no network, bad model: all fall back
        log.warning("openai embedding unavailable (%s: %s); falling back to local", type(exc).__name__, exc)
    try:
        return "local", _local_ef()([query])[0]
    except Exception as exc:  # noqa: BLE001
        log.error("local embedding unavailable too (%s: %s)", type(exc).__name__, exc)
        return "local", None


@lru_cache(maxsize=1)
def probe_backend() -> str:
    """Which backend a search WOULD use right now: "openai" or "local". Cached for the life of
    the process, so /health can be polled without spending an embedding call per hit; a restart
    re-probes."""
    backend, vector = _embed("floorguide health probe")
    return backend if vector is not None else "none"


def index_status() -> dict:
    """What /health reports: which collections exist and how many chunks each holds."""
    status = {"chroma_dir": CHROMA_DIR, "collections": {}, "index_ready": False}
    try:
        client = _client()
        names = {getattr(c, "name", c) for c in client.list_collections()}
    except Exception as exc:  # noqa: BLE001
        log.error("chroma unavailable at %s: %s", CHROMA_DIR, exc)
        return status
    for name in (OPENAI_COLLECTION, LOCAL_COLLECTION):
        count = 0
        if name in names:
            try:
                count = client.get_collection(name=name).count()
            except Exception as exc:  # noqa: BLE001
                log.warning("cannot count %s: %s", name, exc)
        status["collections"][name] = count
    status["index_ready"] = any(v > 0 for v in status["collections"].values())
    return status
