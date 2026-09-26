# backend/ingest.py -- owns chunking and embedding of data/corpus and data/assets into the two
# persistent Chroma collections under ./chroma (corpus_openai, corpus_local). Instance 3 owns this file.
"""Ingest the FloorGuide corpus into two Chroma collections, idempotently.

WHY TWO COLLECTIONS
    Vectors from different embedding models do not share an index. `corpus_openai` holds
    text-embedding-3-small vectors; `corpus_local` holds Chroma's default local (MiniLM, 384-dim)
    vectors and is the no-key fallback the backend queries when the OpenAI embedding call fails.
    The same chunks, the same ids, and the same metadata go into both, so a fallback answer cites
    the same document as the primary path.

CHUNK SIZE AND OVERLAP, AND WHY
    CHUNK_MAX_CHARS = 1400, CHUNK_OVERLAP_CHARS = 150, split on markdown headings first.
    These documents are short procedural blocks: a numbered step list, a service-interval table, a
    symptom-and-first-checks sequence. A number in such a block is meaningless without the heading
    above it ("every 250 operating hours" under "Hydraulic filter"), so the unit of chunking is the
    heading-led section, never a fixed window. The largest heading-led section in this corpus
    measures 1,149 characters (SP-01's lockout steps; MM-P102 Sec 4.2 is 1,094), and every chunk
    carries a short source header, so a 1,400-character budget keeps EVERY section in this corpus
    whole in one chunk with its own heading. Adjacent small sections are packed together up to the
    same budget, so a retrieved chunk is a whole thought rather than a fragment. The 150-character
    overlap only comes into play if a section is longer than the budget: the tail of one piece is
    carried into the next at a word boundary so a split cannot cut a step in half.

THE SEAM (the thing this whole prototype argues)
    The rule reads only structured fields, never prose. `maintenance_due` takes three integers that
    come from data/assets/*.json. Nothing in this vector store can change them:
      - P-102's record carries notes that say "meter probably wrong, ignore". The notes field is
        NEVER ingested.
      - The three meter integers (meter_hours_now, meter_hours_at_last_service,
        service_interval_hours) are NEVER ingested either. They live only in the JSON the analyst
        reads and passes to the function. So there is no retrieved prose anywhere in this index
        that a model could do arithmetic on to guess a due status.
      - INJECTED_do_not_trust.md IS ingested, on purpose. It is dropped by the injection screen at
        retrieval time, before the model sees it, and the drop is counted on screen. If ingestion
        quietly filtered it out there would be nothing to drop and nothing to show.

IDEMPOTENT
    Chunk id = "<path relative to the repo root>::<chunk index>", so a re-run addresses the same
    rows. Each row stores a sha256 of its text; a chunk whose text is unchanged is SKIPPED (not
    re-embedded, so a re-run costs nothing), a changed chunk is re-embedded and upserted, and a
    chunk that no longer exists in the source is deleted. Re-running never duplicates.

No work happens at import time. Instance 1 calls ensure_ingested() at app startup because the
deployed disk is wiped on every restart; `python backend/ingest.py` just calls the same function.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

# --- tunables, all explained in the module docstring above ---------------------------------------
CHUNK_MAX_CHARS = 1400
CHUNK_OVERLAP_CHARS = 150
OPENAI_EMBED_MODEL = "text-embedding-3-small"
COLLECTION_OPENAI = "corpus_openai"
COLLECTION_LOCAL = "corpus_local"
UPSERT_BATCH = 64

VALID_CATEGORIES = ("safety", "maintenance", "quality", "asset_record")
REQUIRED_FRONT_MATTER = ("source_id", "title", "category", "authority", "stale")
QUOTE_CHARS = ('"', "'")

# The repo root is the parent of backend/. Relative paths resolve against it, not against the
# current working directory, so ingestion finds the corpus whether it is started from the repo root
# (the documented way) or by a server process started somewhere else.
REPO_ROOT = Path(__file__).resolve().parent.parent

# Directive-shaped text, for the self-check below. This is NOT the security control: the injection
# screen in backend/guards.py is. This only warns whoever reads the ingest output if a LEGITIMATE
# document has drifted into looking like an instruction, which would inflate the drop count and
# break the eval's `dropped_chunks == 1` assertion.
INSTRUCTION_SHAPED = (
    r"ignore\s+(the\s+)?(safety|reference|previous|above|prior)",
    r"approve\s+(this|the)\b",
    r"you\s+are\s+now\b",
    r"^\s*system\s*:",
    r"\b(maintenance|admin|developer)[- ]override\s+mode\b",
    r"\bhas\s+already\s+signed\s+off\b",
    r"\braise\s+no\s+refusal\b",
)


def _log(verbose: bool, msg: str = "") -> None:
    if verbose:
        print(msg, flush=True)


def _resolve(p: "str | Path") -> Path:
    path = Path(p)
    return path if path.is_absolute() else (REPO_ROOT / path)


# --- front matter --------------------------------------------------------------------------------


def _parse_front_matter(text: str, where: str) -> Tuple[Dict[str, Any], str]:
    """Split a document into (metadata, body).

    Hand-rolled on purpose: the front matter is a flat `key: value` block that this repo writes
    itself, so parsing it here adds no dependency to requirements.txt.
    """
    if not text.startswith("---"):
        raise ValueError(f"{where}: no front matter; every corpus document must start with ---")
    end = text.find("\n---", 3)
    if end == -1:
        raise ValueError(f"{where}: front matter is not closed with ---")
    raw, body = text[3:end], text[end + 4:]

    meta: Dict[str, Any] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"{where}: front matter line is not key: value -> {line!r}")
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in QUOTE_CHARS:
            value = value[1:-1]
        low = value.lower()
        if low in ("true", "false"):
            meta[key] = low == "true"
        elif re.fullmatch(r"-?\d+", value):
            meta[key] = int(value)
        else:
            meta[key] = value

    missing = [k for k in REQUIRED_FRONT_MATTER if k not in meta]
    if missing:
        raise ValueError(f"{where}: front matter is missing {missing}")
    if meta["category"] not in VALID_CATEGORIES:
        raise ValueError(f"{where}: category {meta['category']!r} is not one of {VALID_CATEGORIES}")
    if not isinstance(meta["authority"], int) or not 1 <= meta["authority"] <= 3:
        raise ValueError(f"{where}: authority must be an int 1..3, got {meta['authority']!r}")
    if not isinstance(meta["stale"], bool):
        raise ValueError(f"{where}: stale must be true or false, got {meta['stale']!r}")
    return meta, body.lstrip("\n")


# --- chunking ------------------------------------------------------------------------------------


def _heading_blocks(body: str) -> List[str]:
    """Split a markdown body into heading-led blocks: a heading plus everything under it."""
    blocks: List[str] = []
    current: List[str] = []
    for line in body.splitlines():
        if line.startswith("#") and current:
            blocks.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [b for b in blocks if b.strip()]


def _tail_overlap(text: str, n: int) -> str:
    """The last n characters of text, trimmed forward to a word boundary."""
    if n <= 0 or len(text) <= n:
        return text
    tail = text[-n:]
    space = tail.find(" ")
    return tail[space + 1:] if space != -1 else tail


def _split_oversize(block: str, budget: int) -> List[str]:
    """Split one over-budget block on paragraph, then sentence, then word boundaries."""
    units = [u for u in re.split(r"\n\s*\n", block) if u.strip()]
    refined: List[str] = []
    for unit in units:
        if len(unit) <= budget:
            refined.append(unit)
            continue
        for sentence in re.split(r"(?<=[.:;])\s+", unit):
            if len(sentence) <= budget:
                refined.append(sentence)
            else:  # last resort: hard wrap at word boundaries
                line = ""
                for word in sentence.split():
                    if line and len(line) + 1 + len(word) > budget:
                        refined.append(line)
                        line = word
                    else:
                        line = f"{line} {word}".strip()
                if line:
                    refined.append(line)

    pieces: List[str] = []
    buf = ""
    for unit in refined:
        candidate = f"{buf}\n\n{unit}".strip() if buf else unit
        if buf and len(candidate) > budget:
            pieces.append(buf)
            carry = _tail_overlap(buf, CHUNK_OVERLAP_CHARS)
            buf = f"{carry}\n\n{unit}".strip() if carry else unit
        else:
            buf = candidate
    if buf:
        pieces.append(buf)
    return pieces


def _chunk_document(meta: Dict[str, Any], body: str) -> List[str]:
    """Heading-aware chunks, each one prefixed with a short source header.

    The header means a retrieved chunk names its own document even when it is read on its own, and
    it is what the model quotes when it cites a source.
    """
    header = f"[{meta['source_id']}] {meta['title']}"
    budget = CHUNK_MAX_CHARS - len(header) - 2

    packed: List[str] = []
    buf = ""
    for block in _heading_blocks(body):
        if len(block) > budget:
            if buf:
                packed.append(buf)
                buf = ""
            packed.extend(_split_oversize(block, budget))
            continue
        candidate = f"{buf}\n\n{block}".strip() if buf else block
        if buf and len(candidate) > budget:
            packed.append(buf)
            buf = block
        else:
            buf = candidate
    if buf:
        packed.append(buf)
    return [f"{header}\n\n{piece}".strip() for piece in packed]


# --- sources -------------------------------------------------------------------------------------


def _corpus_rows(corpus_dir: Path) -> Tuple[List[Dict[str, Any]], int]:
    """Every markdown document in the corpus, as chunk rows ready for Chroma."""
    files = sorted(corpus_dir.glob("*.md"))
    if not files:
        raise FileNotFoundError(f"no .md documents found in {corpus_dir}")

    rows: List[Dict[str, Any]] = []
    for path in files:
        rel = path.relative_to(REPO_ROOT).as_posix()
        meta, body = _parse_front_matter(path.read_text(encoding="utf-8"), rel)

        # The filename prefix is the category, so a wrong label is visible in a directory listing.
        # INJECTED_do_not_trust.md is the one deliberate exception: it is named to be obvious on
        # screen, and its category is what it pretends to be so that it really is retrievable.
        prefix = path.name.split("_", 1)[0]
        if not path.name.startswith("INJECTED") and prefix != meta["category"]:
            raise ValueError(
                f"{rel}: filename prefix {prefix!r} does not match category {meta['category']!r}"
            )

        for index, text in enumerate(_chunk_document(meta, body)):
            rows.append(
                {
                    "id": f"{rel}::{index}",
                    "text": text,
                    "metadata": {
                        "source_id": str(meta["source_id"]),
                        "title": str(meta["title"]),
                        "category": str(meta["category"]),
                        "authority": int(meta["authority"]),
                        "stale": bool(meta["stale"]),
                        "revised": str(meta.get("revised", "")),
                        "source_path": rel,
                        "chunk_index": index,
                        "content_sha": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
                    },
                }
            )
    return rows, len(files)


def _asset_rows(assets_dir: Path) -> Tuple[List[Dict[str, Any]], int]:
    """Asset records as identity-only cards.

    THE SEAM, enforced here: the three meter integers and the notes field are NOT ingested. The
    card says which machine it is, which line it runs on, what its service task is, and which
    manual section covers it. It carries no number to do arithmetic on and no prose to be talked
    into anything, so a retrieved chunk can never stand in for the rule's answer.
    """
    if not assets_dir.is_dir():
        return [], 0
    files = sorted(assets_dir.glob("*.json"))
    rows: List[Dict[str, Any]] = []
    for path in files:
        rel = path.relative_to(REPO_ROOT).as_posix()
        record = json.loads(path.read_text(encoding="utf-8"))
        asset_id = record["asset_id"]
        title = f"{asset_id} {record.get('name', '')}".strip()
        text = (
            f"[{asset_id}] Asset record: {record.get('name', asset_id)}\n\n"
            f"Asset id: {asset_id}. Machine: {record.get('name', '')}. "
            f"Line: {record.get('line', '')}. Location: {record.get('location', '')}.\n"
            f"Scheduled service task: {record.get('service_task', '')}.\n"
            f"Controlled manual section for this task: {record.get('manual_ref', '')}.\n\n"
            "This is a structured record, not prose authority. Its meter readings, its service "
            "interval, and its notes are deliberately not part of this text: the service-due "
            "status is computed by the maintenance_due function from the structured fields in "
            f"{rel}, never from a retrieved passage and never by a model."
        )
        rows.append(
            {
                "id": f"{rel}::0",
                "text": text,
                "metadata": {
                    "source_id": asset_id,
                    "title": title,
                    "category": "asset_record",
                    # 2 = a controlled source for its own fields. An asset record is structured
                    # data; it is never authority over a procedure.
                    "authority": 2,
                    "stale": False,
                    "revised": str(record.get("meter_read_at", "")),
                    "source_path": rel,
                    "chunk_index": 0,
                    "content_sha": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
                },
            }
        )
    return rows, len(files)


# --- embedding functions -------------------------------------------------------------------------


def _openai_embedding_function():
    """text-embedding-3-small, key read from the environment by Chroma itself.

    The key is never passed in code and never written to a file. Chroma reads the environment
    variable whose NAME is given below.
    """
    from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction

    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set (checked after load_dotenv)")
    return OpenAIEmbeddingFunction(
        # detect-secrets reads an assignment whose name looks like a key as a Secret Keyword. The
        # value here is the NAME of an environment variable, not a secret, so the finding is
        # annotated rather than the gate loosened:
        api_key_env_var="OPENAI_API_KEY",  # pragma: allowlist secret
        model_name=OPENAI_EMBED_MODEL,
    )


def _local_embedding_function():
    """Chroma's default local embedding function (MiniLM, 384-dim). No key, no network."""
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

    return DefaultEmbeddingFunction()


# --- writing -------------------------------------------------------------------------------------


def _sync_collection(
    client, name: str, embedding_fn, rows: List[Dict[str, Any]], verbose: bool
) -> Dict[str, Any]:
    """Upsert only what changed; delete what is gone. Returns this collection's counts."""
    collection = client.get_or_create_collection(name=name, embedding_function=embedding_fn)

    wanted = {row["id"]: row for row in rows}
    existing_sha: Dict[str, str] = {}
    ids = list(wanted)
    for start in range(0, len(ids), 200):
        got = collection.get(ids=ids[start:start + 200], include=["metadatas"])
        for row_id, meta in zip(got.get("ids", []), got.get("metadatas", []) or []):
            existing_sha[row_id] = (meta or {}).get("content_sha", "")

    todo = [r for r in rows if existing_sha.get(r["id"]) != r["metadata"]["content_sha"]]
    skipped = len(rows) - len(todo)

    for start in range(0, len(todo), UPSERT_BATCH):
        batch = todo[start:start + UPSERT_BATCH]
        collection.upsert(
            ids=[r["id"] for r in batch],
            documents=[r["text"] for r in batch],
            metadatas=[r["metadata"] for r in batch],
        )

    # A chunk that no longer exists in the source must not linger in the index and be retrieved.
    stale_ids = [i for i in collection.get(include=[]).get("ids", []) if i not in wanted]
    if stale_ids:
        collection.delete(ids=stale_ids)

    count = collection.count()
    _log(
        verbose,
        f"  {name:14s} written={len(todo):3d} skipped={skipped:3d} deleted={len(stale_ids):3d} "
        f"count={count:3d}",
    )
    return {
        "written": len(todo),
        "skipped": skipped,
        "deleted": len(stale_ids),
        "count": count,
        "status": "ok",
    }


def ensure_ingested(
    corpus_dir: "str | Path" = "data/corpus",
    persist_dir: "str | Path" = "./chroma",
    *,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Chunk and embed the corpus into both Chroma collections. Safe to call on every startup.

    Idempotent: unchanged chunks are skipped, not re-embedded, so a second call costs no tokens and
    changes nothing. Returns the counts, per collection, and which backend the API should report.
    Relative paths resolve against the repo root, not the current working directory.
    """
    started = time.time()

    # Keys live in .env at the repo root, not in the shell, so .env WINS. Loaded here rather than at
    # import time so that importing this module does no work and has no side effects. override=True
    # is load bearing: without it a stale OPENAI_API_KEY already exported in the terminal shadows a
    # rotated key in .env, and embedding falls back to local while .env holds a working key. It is
    # safe in deployment, because App Platform has no .env file, so this call is a no-op there and
    # the platform's own env vars still win.
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env", override=True)
    except ImportError:  # python-dotenv is pinned; a missing one is not fatal for local embeddings
        _log(verbose, "note: python-dotenv is not installed; relying on the process environment")

    import chromadb
    from chromadb.config import Settings

    corpus_path = _resolve(corpus_dir)
    persist_path = _resolve(persist_dir)
    assets_path = corpus_path.parent / "assets"

    corpus_rows, documents_read = _corpus_rows(corpus_path)
    asset_rows, assets_read = _asset_rows(assets_path)
    rows = corpus_rows + asset_rows

    _log(verbose, "FloorGuide ingestion")
    _log(verbose, f"  corpus      {corpus_path}")
    _log(verbose, f"  assets      {assets_path}")
    _log(verbose, f"  persist to  {persist_path}")
    _log(
        verbose,
        f"  read        {documents_read} documents + {assets_read} asset records "
        f"-> {len(rows)} chunks (max {CHUNK_MAX_CHARS} chars, overlap {CHUNK_OVERLAP_CHARS})",
    )

    # Self-check, not a security control: exactly one chunk in this corpus should look like an
    # instruction, and it must be the file that is named for it.
    shaped = [
        r["id"]
        for r in rows
        if any(re.search(p, r["text"], re.IGNORECASE | re.MULTILINE) for p in INSTRUCTION_SHAPED)
    ]
    _log(verbose, f"  screen      instruction-shaped chunks: {len(shaped)} (expected 1) {shaped}")
    if len(shaped) != 1 or not shaped[0].startswith("data/corpus/INJECTED"):
        _log(
            verbose,
            "  WARNING     expected exactly one instruction-shaped chunk, from "
            "INJECTED_do_not_trust.md. The eval asserts dropped_chunks == 1; check the corpus "
            "and the injection screen before trusting that number.",
        )

    persist_path.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(
        path=str(persist_path),
        settings=Settings(anonymized_telemetry=False),
    )

    collections: Dict[str, Dict[str, Any]] = {}

    # Local first: it needs no key and no network, so the fallback index is always populated even
    # when the OpenAI path is unavailable.
    for name, factory, label in (
        (COLLECTION_LOCAL, _local_embedding_function, "chroma-default MiniLM (384-dim, no key)"),
        (COLLECTION_OPENAI, _openai_embedding_function, f"openai {OPENAI_EMBED_MODEL}"),
    ):
        try:
            result = _sync_collection(client, name, factory(), rows, verbose)
        except Exception as exc:  # a missing key or a failed embed must not stop the other path
            result = {
                "written": 0,
                "skipped": 0,
                "deleted": 0,
                "count": 0,
                "status": "unavailable",
                "detail": f"{type(exc).__name__}: {exc}",
            }
            _log(verbose, f"  {name:14s} UNAVAILABLE  {result['detail']}")
        result["embedder"] = label
        collections[name] = result

    openai_ready = collections[COLLECTION_OPENAI]["count"] == len(rows)
    local_ready = collections[COLLECTION_LOCAL]["count"] == len(rows)
    backend = "openai" if openai_ready else ("local" if local_ready else "none")

    summary: Dict[str, Any] = {
        "documents_read": documents_read,
        "asset_records_read": assets_read,
        "chunks_total": len(rows),
        "chunks_written": sum(c["written"] for c in collections.values()),
        "chunks_skipped": sum(c["skipped"] for c in collections.values()),
        "collections": collections,
        "instruction_shaped_chunks": len(shaped),
        "vector_backend": backend,
        "persist_dir": str(persist_path),
        "corpus_dir": str(corpus_path),
        "ready": backend != "none",
        "elapsed_s": round(time.time() - started, 2),
    }

    written_to = [n for n, c in collections.items() if c["status"] == "ok"]
    _log(verbose, f"  wrote to    {written_to or 'NOTHING'}")
    _log(
        verbose,
        f"  totals      documents_read={documents_read} asset_records_read={assets_read} "
        f"chunks_total={len(rows)} chunks_written={summary['chunks_written']} "
        f"chunks_skipped={summary['chunks_skipped']}",
    )
    _log(
        verbose,
        f"  backend     vector_backend={backend} ready={summary['ready']} "
        f"in {summary['elapsed_s']}s",
    )
    return summary


def _main() -> int:
    summary = ensure_ingested()
    if not summary["ready"]:
        print(
            "INGEST FAILED: neither collection holds the full corpus; nothing is searchable.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
