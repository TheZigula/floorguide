# eval/run_eval.py -- owns the fail-closed grading run: it calls the live backend for every case in
# eval/golden.json, grades it, writes eval/results.json, and exits nonzero rather than pass in doubt.
"""FloorGuide eval.

WHAT FAIL-CLOSED MEANS HERE. Three counts are printed and written: attempted, scored,
failed_to_score. A case that could not be scored is never quietly dropped -- if scored != attempted
the run exits 1, so "the judge timed out" can never look like "everything passed". The run also
exits 1 on an empty golden set, on any `grader: code` case failing, and on the must-fail control
NOT failing, because a grader that can no longer fail a wrong answer is not measuring anything.

WHO GRADES WHAT. The judge never grades anything that has a right answer.
    code (the half that blocks):
        rule      -- run_eval imports maintenance_due() and calls it on the asset record named by
                     the case, then requires the API's rule_result to match field for field. The
                     expected numbers are computed here, never typed in the answer key, so the key
                     cannot drift away from the code.
        refusal   -- the literal REFUSED: marker, refused == true, and pending_approval == null.
        injection -- dropped_chunks == 1 and the injected source is not cited.
    judge (evidence, not proof):
        retrieval x3 -- faithfulness, answer relevancy, context precision, against thresholds.
        control      -- MUST fail on faithfulness.
        control_subtle -- reported only, never gated.

WHAT THE JUDGE IS SHOWN. Faithfulness is scored against the authoritative retrieved text, which is
the full chunk behind each cited source, minus the known-stale card and the injected sheet named in
golden.json. Two reasons. (1) The API's `snippet` is an abbreviation for the UI; the model actually
saw the whole chunk, so scoring against the snippet would mark supported claims unsupported. This is
measured, not assumed: a correct, well-cited answer scores 0.5 against one thin sentence and high
against the real chunk. (2) Leaving the stale quick card in the context would mark a correct answer
unfaithful for disagreeing with a document the corpus itself says is out of date.

WHAT THE CONTROLS SCORE. For a control the text handed to the judge is the deliberately wrong
answer from golden.json, not the system's own answer. A control only proves the grader works if the
thing being graded is wrong.

THE SEAM: the rule reads only structured fields, never prose. Neither the note on P-102's record
("meter probably wrong, ignore") nor the injected tip sheet can change the number, because neither
has a path into maintenance_due(). The rule cases in this file are how that is proved rather than
asserted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent
sys.path.insert(0, str(REPO_ROOT))  # so `backend.tools.rules` imports when run as a script

GOLDEN_PATH = EVAL_DIR / "golden.json"
THRESHOLDS_PATH = EVAL_DIR / "thresholds.json"
RESULTS_PATH = EVAL_DIR / "results.json"

DEFAULT_API = os.environ.get("FLOORGUIDE_API", "http://localhost:8000")

# gpt-4o list price, Sep 2026, blended high for a conservative estimate. Used only to enforce the
# spend cap before a call, never reported as an invoice.
USD_PER_1K_TOKENS_BLENDED = 0.005

# Each modern Ragas metric takes a DIFFERENT set of arguments. AnswerRelevancy takes no contexts;
# ContextPrecisionWithReference takes no response. Calling ascore with a uniform bag of kwargs
# raises TypeError, so the accepted names are declared here and filtered per metric.
METRIC_ARGS = {
    "faithfulness": ("user_input", "response", "retrieved_contexts"),
    "answer_relevancy": ("user_input", "response"),
    "context_precision_with_reference": ("user_input", "reference", "retrieved_contexts"),
}
RETRIEVAL_METRICS = ("faithfulness", "answer_relevancy", "context_precision_with_reference")
CONTROL_METRICS = ("faithfulness",)


# --- logging hygiene -----------------------------------------------------------------------------


class _Scrubber(logging.Filter):
    """Redact anything shaped like a key or an OpenAI org id from every log line."""

    PATTERNS = (
        re.compile(r"org-[A-Za-z0-9]{6,}"),
        re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
        re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{8,}"),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = message
        for pattern in self.PATTERNS:
            cleaned = pattern.sub("[redacted]", cleaned)
        if cleaned != message:
            record.msg, record.args = cleaned, ()
        return True


def _quiet_libraries() -> None:
    """Keep library chatter (which can carry the org id) off the transcript."""
    logging.basicConfig(level=logging.WARNING)
    scrubber = _Scrubber()
    root = logging.getLogger()
    root.addFilter(scrubber)
    for handler in root.handlers:
        handler.addFilter(scrubber)
    for name in ("httpx", "httpcore", "openai", "ragas", "langchain", "langchain_core", "chromadb"):
        log = logging.getLogger(name)
        log.setLevel(logging.ERROR)
        log.addFilter(scrubber)
    os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")


# --- small helpers -------------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"{path} is missing; the eval cannot run without it")
    return json.loads(path.read_text(encoding="utf-8"))


def _http_json(url: str, payload: Optional[Dict[str, Any]], timeout: float) -> Tuple[int, Any]:
    """POST json (or GET when payload is None). Returns (status, parsed-or-text)."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="GET" if payload is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            try:
                return response.status, json.loads(body)
            except json.JSONDecodeError:
                return response.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and not math.isnan(value)


def _truncate(text: str, limit: int = 1200) -> str:
    return text if len(text) <= limit else text[:limit] + " ...[truncated for the record]"


# --- pacing and the spend cap --------------------------------------------------------------------


class Pacer:
    """Keep judge traffic under the org's tokens-per-minute limit, and under the spend cap.

    The org allows 30,000 gpt-4o tokens a minute; this paces at the 20,000 in thresholds.json so a
    burst cannot trip a 429 in the middle of a recorded run. Token counts are ESTIMATED from
    characters (the metric's internal calls are not exposed), which is why they are reported as
    estimates and the cap is enforced conservatively.
    """

    def __init__(self, tokens_per_minute: int, spend_cap_usd: float) -> None:
        self.limit = max(1, int(tokens_per_minute))
        self.spend_cap_usd = float(spend_cap_usd)
        self.window: deque = deque()
        self.tokens_used = 0

    @staticmethod
    def estimate_tokens(texts: List[str]) -> int:
        chars = sum(len(t or "") for t in texts)
        # ~4 chars per token, doubled: a metric makes several internal calls per score (statement
        # extraction, then verdicts) and the prompt template is re-sent each time.
        return max(200, int(chars / 4) * 2)

    @property
    def estimated_cost_usd(self) -> float:
        return round(self.tokens_used / 1000.0 * USD_PER_1K_TOKENS_BLENDED, 4)

    def would_exceed_cap(self, estimate: int) -> bool:
        projected = (self.tokens_used + estimate) / 1000.0 * USD_PER_1K_TOKENS_BLENDED
        return projected > self.spend_cap_usd

    def wait_for(self, estimate: int) -> float:
        now = time.monotonic()
        while self.window and now - self.window[0][0] > 60.0:
            self.window.popleft()
        in_window = sum(t for _, t in self.window)
        slept = 0.0
        if in_window + estimate > self.limit and self.window:
            sleep_for = max(0.0, 60.0 - (now - self.window[0][0])) + 0.25
            print(
                f"    pacing: {in_window} estimated tokens already in this minute, "
                f"sleeping {sleep_for:.1f}s to stay under {self.limit}/min",
                flush=True,
            )
            time.sleep(sleep_for)
            slept = sleep_for
            now = time.monotonic()
            while self.window and now - self.window[0][0] > 60.0:
                self.window.popleft()
        self.window.append((now, estimate))
        self.tokens_used += estimate
        return slept


# --- the authoritative context -------------------------------------------------------------------


class ContextBuilder:
    """Rebuild the full retrieved chunk behind each cited source.

    The API returns a snippet for the UI, not the whole chunk the model saw. Scoring faithfulness
    against an abbreviation punishes claims that the real context supports, so the chunk is looked
    up in the same Chroma collection the backend searched, matched by source id and by the snippet
    itself. If the index cannot be opened the snippet is used and the row says so, because a quiet
    substitution would make the scores unreadable.
    """

    def __init__(self, persist_dir: Path, exclude_source_ids: List[str]) -> None:
        self.exclude = set(exclude_source_ids or [])
        self.by_source: Dict[str, List[str]] = {}
        self.available = False
        self.detail = ""
        try:
            import chromadb
            from chromadb.config import Settings

            client = chromadb.PersistentClient(
                path=str(persist_dir), settings=Settings(anonymized_telemetry=False)
            )
            # Read the documents only; no embedding function is needed for a metadata get, so this
            # never spends an embedding call and never needs a key.
            for name in ("corpus_openai", "corpus_local"):
                try:
                    collection = client.get_collection(name=name)
                except Exception:
                    continue
                got = collection.get(include=["documents", "metadatas"])
                for text, meta in zip(got.get("documents") or [], got.get("metadatas") or []):
                    source_id = (meta or {}).get("source_id", "")
                    if source_id and text:
                        self.by_source.setdefault(source_id, [])
                        if text not in self.by_source[source_id]:
                            self.by_source[source_id].append(text)
                if self.by_source:
                    self.available = True
                    self.detail = f"chunks read from {name}"
                    break
        except Exception as exc:
            self.detail = f"index unavailable ({type(exc).__name__}: {exc}); snippets used instead"

    def build(self, sources: List[Dict[str, Any]]) -> Tuple[List[str], List[str], List[str]]:
        """Returns (contexts, used_source_ids, excluded_source_ids)."""
        contexts: List[str] = []
        used: List[str] = []
        excluded: List[str] = []
        for source in sources or []:
            source_id = str(source.get("source_id", ""))
            if source_id in self.exclude:
                excluded.append(source_id)
                continue
            snippet = str(source.get("snippet", "") or "")
            text = snippet
            candidates = self.by_source.get(source_id, [])
            if candidates:
                probe = re.sub(r"\s+", " ", snippet).strip()[:60]
                match = next(
                    (c for c in candidates if probe and re.sub(r"\s+", " ", c).find(probe) != -1),
                    None,
                )
                text = match or candidates[0]
            if text and text not in contexts:
                contexts.append(text)
                used.append(source_id)
        return contexts, used, excluded


# --- the live pass -------------------------------------------------------------------------------


def ask_backend(api: str, case: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """One /chat turn per case, on its own thread id so no case inherits another's state."""
    thread_id = f"eval-{case['id']}-{int(time.time())}"
    status, body = _http_json(
        f"{api.rstrip('/')}/chat",
        {"thread_id": thread_id, "user_id": "dana", "message": case["question"]},
        timeout,
    )
    if status != 200 or not isinstance(body, dict):
        return {"_error": f"HTTP {status}: {_truncate(str(body), 400)}", "_thread_id": thread_id}
    body["_thread_id"] = thread_id
    return body


# --- code graders --------------------------------------------------------------------------------


def _check(name: str, ok: bool, detail: str = "") -> Dict[str, Any]:
    return {"check": name, "ok": bool(ok), "detail": detail}


def _load_asset_record(rel_path: str) -> Dict[str, Any]:
    return json.loads((REPO_ROOT / rel_path).read_text(encoding="utf-8"))


def grade_rule(case: Dict[str, Any], response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Call maintenance_due on the record and require the API to agree with it exactly."""
    from backend.tools.rules import RuleRefusal, maintenance_due

    checks: List[Dict[str, Any]] = []
    spec = case.get("checks", {})
    record = _load_asset_record(spec["rule_from_asset_record"])
    args = dict(
        asset_id=record.get("asset_id"),
        meter_hours_now=record.get("meter_hours_now"),
        meter_hours_at_last_service=record.get("meter_hours_at_last_service"),
        service_interval_hours=record.get("service_interval_hours"),
    )

    expected_field = spec.get("expect_rule_refusal_field")
    if expected_field:
        try:
            result = maintenance_due(**args)
            checks.append(
                _check(
                    "function_refuses",
                    False,
                    f"maintenance_due returned {result.model_dump()} but the record should be "
                    f"refused on {expected_field}",
                )
            )
            return checks
        except RuleRefusal as refusal:
            checks.append(
                _check(
                    "function_refuses_naming_field",
                    refusal.field == expected_field,
                    f"RuleRefusal(field={refusal.field!r}, reason={refusal.reason!r}); "
                    f"expected field {expected_field!r}",
                )
            )
        answer = str(response.get("answer", ""))
        checks.append(
            _check(
                "api_refused",
                bool(response.get("refused")) and answer.startswith("REFUSED:"),
                f"refused={response.get('refused')!r} answer_starts={answer[:40]!r}",
            )
        )
        if spec.get("answer_must_name_field"):
            checks.append(
                _check(
                    "answer_names_the_field",
                    expected_field in answer,
                    f"the answer must name {expected_field!r} so a supervisor knows what to re-read",
                )
            )
        checks.append(
            _check(
                "no_draft_on_a_refusal",
                response.get("pending_approval") in (None, {}),
                f"pending_approval={response.get('pending_approval')!r}",
            )
        )
        return checks

    # The computed case: every asserted field must match the function's own result.
    expected = maintenance_due(**args).model_dump()
    actual = response.get("rule_result")
    if not isinstance(actual, dict):
        checks.append(
            _check("rule_result_present", False, f"rule_result={actual!r}; nothing to compare")
        )
        return checks
    for field in spec.get("assert_rule_result_matches_function", []):
        checks.append(
            _check(
                f"rule_result.{field}_matches_function",
                actual.get(field) == expected.get(field),
                f"function={expected.get(field)!r} api={actual.get(field)!r}",
            )
        )
    return checks


def grade_refusal(case: Dict[str, Any], response: Dict[str, Any]) -> List[Dict[str, Any]]:
    spec = case.get("checks", {})
    answer = str(response.get("answer", ""))
    marker = spec.get("assert_refusal_marker", "REFUSED:")
    checks = [
        _check("refused_flag", bool(response.get("refused")), f"refused={response.get('refused')!r}"),
        _check("refusal_marker", answer.startswith(marker), f"answer starts {answer[:40]!r}"),
        _check(
            "no_pending_approval",
            response.get("pending_approval") in (None, {}),
            f"pending_approval={response.get('pending_approval')!r}",
        ),
    ]
    return checks


def grade_injection(case: Dict[str, Any], response: Dict[str, Any]) -> List[Dict[str, Any]]:
    spec = case.get("checks", {})
    expected_drops = spec.get("assert_dropped_chunks_equals", 1)
    dropped = response.get("dropped_chunks")
    cited = [str(s.get("source_id", "")) for s in response.get("sources") or []]
    forbidden = spec.get("assert_source_id_not_cited")
    return [
        _check(
            "dropped_chunks_exactly_one",
            dropped == expected_drops,
            f"dropped_chunks={dropped!r} expected={expected_drops!r}",
        ),
        _check(
            "injected_source_not_cited",
            forbidden not in cited,
            f"cited={cited!r} must not contain {forbidden!r}",
        ),
    ]


# --- the judge pass ------------------------------------------------------------------------------


async def score_modern(
    rows: List[Dict[str, Any]], judge_model: str, pacer: Pacer
) -> Tuple[Dict[str, Dict[str, Optional[float]]], List[str]]:
    """Ragas 0.4.3 modern path: build the metrics on AsyncOpenAI and score each row directly.

    evaluate() rejects these metrics, so every row is scored with `await metric.ascore(...)`, with
    only the arguments that metric actually accepts.
    """
    from openai import AsyncOpenAI
    from ragas.embeddings.base import embedding_factory
    from ragas.llms import llm_factory
    from ragas.metrics.collections import (
        AnswerRelevancy,
        ContextPrecisionWithReference,
        Faithfulness,
    )

    client = AsyncOpenAI()
    llm = llm_factory(judge_model, client=client)
    embeddings = embedding_factory(
        provider="openai", model="text-embedding-3-small", client=client
    )
    metrics = {
        "faithfulness": Faithfulness(llm=llm),
        "answer_relevancy": AnswerRelevancy(llm=llm, embeddings=embeddings),
        "context_precision_with_reference": ContextPrecisionWithReference(llm=llm),
    }

    scores: Dict[str, Dict[str, Optional[float]]] = {}
    notes: List[str] = []
    for row in rows:
        case_id = row["id"]
        scores[case_id] = {}
        for metric_name in row["metrics"]:
            available = {
                "user_input": row["user_input"],
                "response": row["response"],
                "retrieved_contexts": row["retrieved_contexts"],
                "reference": row["reference"],
            }
            kwargs = {k: available[k] for k in METRIC_ARGS[metric_name]}
            estimate = Pacer.estimate_tokens(
                [row["user_input"], row["response"], row["reference"]] + row["retrieved_contexts"]
            )
            if pacer.would_exceed_cap(estimate):
                notes.append(
                    f"{case_id}/{metric_name}: NOT SCORED, the judge spend cap of "
                    f"${pacer.spend_cap_usd:.2f} would be exceeded "
                    f"(estimated ${pacer.estimated_cost_usd:.2f} spent)"
                )
                scores[case_id][metric_name] = None
                continue
            pacer.wait_for(estimate)
            print(f"    judging {case_id} / {metric_name} ...", flush=True)
            try:
                result = await metrics[metric_name].ascore(**kwargs)
                value = getattr(result, "value", None)
                scores[case_id][metric_name] = float(value) if _is_number(value) else None
                if not _is_number(value):
                    notes.append(f"{case_id}/{metric_name}: returned {value!r}, failed to score")
            except Exception as exc:
                scores[case_id][metric_name] = None
                notes.append(f"{case_id}/{metric_name}: {type(exc).__name__}: {exc}")
    return scores, notes


def score_legacy(
    rows: List[Dict[str, Any]], judge_model: str
) -> Tuple[Dict[str, Dict[str, Optional[float]]], List[str]]:
    """Fallback for an older Ragas: the legacy metric objects through evaluate().

    Kept because the modern collections module is new; raise_exceptions=True so a judge error is a
    failure to score rather than a silent NaN in the table.
    """
    from datasets import Dataset  # type: ignore
    from langchain_openai import ChatOpenAI  # type: ignore
    from ragas import evaluate  # type: ignore
    from ragas.llms import LangchainLLMWrapper  # type: ignore
    from ragas.metrics import answer_relevancy, context_precision, faithfulness  # type: ignore

    wrapped = LangchainLLMWrapper(ChatOpenAI(model=judge_model, temperature=0))
    legacy_names = {
        "faithfulness": faithfulness,
        "answer_relevancy": answer_relevancy,
        "context_precision_with_reference": context_precision,
    }
    scores: Dict[str, Dict[str, Optional[float]]] = {}
    notes: List[str] = []
    for row in rows:
        dataset = Dataset.from_dict(
            {
                "question": [row["user_input"]],
                "answer": [row["response"]],
                "contexts": [row["retrieved_contexts"]],
                "ground_truth": [row["reference"]],
            }
        )
        chosen = [legacy_names[m] for m in row["metrics"]]
        scores[row["id"]] = {}
        try:
            outcome = evaluate(dataset, metrics=chosen, llm=wrapped, raise_exceptions=True)
            frame = outcome.to_pandas()
            for metric_name in row["metrics"]:
                column = "context_precision" if metric_name.startswith("context_precision") else metric_name
                value = frame[column].iloc[0] if column in frame else None
                scores[row["id"]][metric_name] = float(value) if _is_number(value) else None
        except Exception as exc:
            for metric_name in row["metrics"]:
                scores[row["id"]][metric_name] = None
            notes.append(f"{row['id']}: legacy path failed: {type(exc).__name__}: {exc}")
    return scores, notes


# --- the run -------------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="FloorGuide fail-closed eval")
    parser.add_argument("--api", default=DEFAULT_API, help="backend base url")
    parser.add_argument(
        "--judge-model",
        default=os.environ.get("JUDGE_MODEL"),
        help="override the judge model in thresholds.json (gpt-4o-mini while iterating)",
    )
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="run only the code-graded gate; spends nothing on the judge and cannot report a "
             "judge aggregate, so the final line says so",
    )
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env")  # keys live in .env at the repo root, not in the shell
    except ImportError:
        pass
    _quiet_libraries()

    started_at = _now_iso()
    started_monotonic = time.monotonic()

    golden = _load_json(GOLDEN_PATH)
    thresholds = _load_json(THRESHOLDS_PATH)
    cases: List[Dict[str, Any]] = golden.get("cases") or []
    if not cases:
        print("EVAL FAILED: the golden set is empty. There is nothing to grade.", file=sys.stderr)
        return 1

    judge_model = args.judge_model or thresholds.get("judge_model", "gpt-4o")
    timeout = float(thresholds.get("api_timeout_seconds", 180))
    metric_thresholds = thresholds.get("metrics", {})

    import ragas

    print("FloorGuide eval")
    print(f"  golden set   {golden.get('golden_set_id')} ({len(cases)} cases) from {GOLDEN_PATH}")
    print(f"  thresholds   {metric_thresholds} (set {thresholds.get('set_at')}, read at runtime)")
    print(f"  library      ragas=={ragas.__version__}   judge={judge_model}")
    print(f"  backend      {args.api}")

    status, health = _http_json(f"{args.api.rstrip('/')}/health", None, 30)
    if status != 200 or not isinstance(health, dict):
        print(
            f"EVAL FAILED: {args.api}/health returned {status}. The backend must be running: "
            f"python -m uvicorn backend.app:app --port 8000",
            file=sys.stderr,
        )
        return 1
    print(
        f"  health       service={health.get('service')!r} commit={health.get('commit')!r} "
        f"vector_backend={health.get('vector_backend')!r}"
    )

    # --- pass 1: ask the live system every question ---------------------------------------------
    print("\n  asking the backend one question per case, each on its own thread")
    responses: Dict[str, Dict[str, Any]] = {}
    for case in cases:
        response = ask_backend(args.api, case, timeout)
        responses[case["id"]] = response
        if "_error" in response:
            print(f"    {case['id']:34s} ERROR {response['_error']}")
        else:
            print(
                f"    {case['id']:34s} routed_to={str(response.get('routed_to')):12s} "
                f"worker={str(response.get('worker')):10s} refused={str(response.get('refused')):5s} "
                f"dropped={response.get('dropped_chunks')} "
                f"sources={[s.get('source_id') for s in response.get('sources') or []]}"
            )

    # --- pass 2: the code-graded gate -----------------------------------------------------------
    print("\n  code-graded cases (the gate)")
    rows: List[Dict[str, Any]] = []
    context_builder = ContextBuilder(
        REPO_ROOT / "chroma", (golden.get("exclude_from_judge_context") or {}).get("source_ids", [])
    )
    print(f"    context source: {context_builder.detail or 'snippets from the API'}")

    judge_rows: List[Dict[str, Any]] = []
    for case in cases:
        case_id = case["id"]
        kind = case["kind"]
        response = responses[case_id]
        contexts, used, excluded = context_builder.build(response.get("sources") or [])
        cited = [str(s.get("source_id", "")) for s in response.get("sources") or []]
        expected_routes = case.get("expected_routed_to")
        expected_routes = (
            [expected_routes] if isinstance(expected_routes, str) else (expected_routes or [])
        )

        row: Dict[str, Any] = {
            "id": case_id,
            "kind": kind,
            "grader": case["grader"],
            "question": case["question"],
            "expected_source_id": case.get("expected_source_id"),
            "expected_source_document": case.get("expected_source_document"),
            "thread_id": response.get("_thread_id"),
            "error": response.get("_error"),
            "worker": response.get("worker"),
            "routed_to": response.get("routed_to"),
            "answer": _truncate(str(response.get("answer", ""))),
            "cited_source_ids": cited,
            "dropped_chunks": response.get("dropped_chunks"),
            "refused": response.get("refused"),
            "rule_result": response.get("rule_result"),
            "pending_approval_present": bool(response.get("pending_approval")),
            "judge_context_source_ids": used,
            "judge_context_excluded_source_ids": excluded,
            "checks": [],
            "reported": {},
            "scores": {},
            "threshold_pass": {},
            "passed": None,
            "scored": False,
        }

        # Reported, never gating: routing and citation have right answers, so they are checked in
        # code and shown, but the brief's exit code is driven by the enumerated gates alone.
        row["reported"]["routed_to_as_expected"] = (
            (response.get("routed_to") in expected_routes) if expected_routes else None
        )
        if case.get("expected_source_id"):
            row["reported"]["expected_source_cited"] = case["expected_source_id"] in cited
        if case.get("checks", {}).get("report_safety_procedure_cited"):
            wanted = case["checks"]["report_safety_procedure_cited"]
            row["reported"]["safety_procedure_cited"] = any(w in cited for w in wanted)
        if kind == "rule":
            row["reported"]["ended_at_an_approval_card"] = bool(response.get("pending_approval"))

        if response.get("_error"):
            row["checks"].append(_check("backend_answered", False, response["_error"]))
            row["passed"] = False
            row["scored"] = False
            rows.append(row)
            print(f"    {case_id:34s} FAILED TO SCORE  {response['_error']}")
            continue

        if case["grader"] == "code":
            if kind == "rule":
                row["checks"] = grade_rule(case, response)
            elif kind == "refusal":
                row["checks"] = grade_refusal(case, response)
            elif kind == "injection":
                row["checks"] = grade_injection(case, response)
            else:
                row["checks"] = [_check("known_kind", False, f"no code grader for kind {kind!r}")]
            row["passed"] = all(c["ok"] for c in row["checks"])
            row["scored"] = True
            verdict = "PASS" if row["passed"] else "FAIL"
            print(f"    {case_id:34s} {verdict}")
            for check in row["checks"]:
                flag = "ok  " if check["ok"] else "FAIL"
                print(f"        [{flag}] {check['check']}: {check['detail']}")
        else:
            under_test = (
                case["expected_answer"]
                if case.get("response_under_test") == "golden_expected_answer"
                else str(response.get("answer", ""))
            )
            judge_rows.append(
                {
                    "id": case_id,
                    "kind": kind,
                    "metrics": list(CONTROL_METRICS if kind.startswith("control") else RETRIEVAL_METRICS),
                    "user_input": case["question"],
                    "response": under_test,
                    "retrieved_contexts": contexts or [""],
                    "reference": case["expected_answer"],
                }
            )
            row["judged_text_is"] = case.get("response_under_test", "live")
        rows.append(row)

    by_id = {row["id"]: row for row in rows}

    # --- pass 3: the judge ----------------------------------------------------------------------
    pacer = Pacer(
        int(thresholds.get("pace_tokens_per_minute", 20000)),
        float(thresholds.get("judge_spend_cap_usd", 2.0)),
    )
    judge_path = "none"
    judge_notes: List[str] = []
    scores: Dict[str, Dict[str, Optional[float]]] = {}

    pending = [r for r in judge_rows if by_id[r["id"]]["error"] is None]
    if args.no_judge:
        print("\n  judge skipped (--no-judge): no judge-graded case can be scored this run")
        judge_path = "skipped"
    elif pending:
        print(
            f"\n  judging {len(pending)} cases on {judge_model}, paced at "
            f"{pacer.limit} estimated tokens/min, spend cap ${pacer.spend_cap_usd:.2f}"
        )
        try:
            scores, judge_notes = asyncio.run(score_modern(pending, judge_model, pacer))
            judge_path = "modern (ragas.metrics.collections, ascore per row)"
        except Exception as exc:
            print(f"    modern path unavailable ({type(exc).__name__}: {exc}); trying legacy")
            try:
                scores, judge_notes = score_legacy(pending, judge_model)
                judge_path = "legacy (ragas.metrics + evaluate)"
            except Exception as exc2:
                judge_path = "FAILED"
                judge_notes.append(f"both paths failed: {type(exc2).__name__}: {exc2}")
                scores = {r["id"]: {m: None for m in r["metrics"]} for r in pending}
        print(f"    judge path ran: {judge_path}")

    for judge_row in judge_rows:
        row = by_id[judge_row["id"]]
        row_scores = scores.get(judge_row["id"], {m: None for m in judge_row["metrics"]})
        row["scores"] = row_scores
        row["scored"] = all(_is_number(v) for v in row_scores.values()) and bool(row_scores)
        for metric_name, value in row_scores.items():
            floor = metric_thresholds.get(metric_name)
            if _is_number(value) and floor is not None:
                row["threshold_pass"][metric_name] = value >= float(floor)
        if row["kind"] == "control":
            faith = row_scores.get("faithfulness")
            floor = float(metric_thresholds.get("faithfulness", 0.7))
            # A control has no "passed": the question is whether the grader FAILED it, as required.
            # Recorded as its own field so nobody reads a true in a passed column and relaxes.
            row["passed"] = None
            row["control_failed_as_required"] = _is_number(faith) and faith < floor
            row["judge_let_the_wrong_answer_through"] = _is_number(faith) and faith >= floor
        elif row["kind"] == "control_subtle":
            faith = row_scores.get("faithfulness")
            floor = float(thresholds.get("subtle_control_caught_below", 0.7))
            if not _is_number(faith):
                # "missed" would be a claim about the judge. Not scoring it is a claim about the run.
                row["subtle_control"] = "not_scored"
            else:
                row["subtle_control"] = "caught" if faith < floor else "missed"
            row["passed"] = None  # never gated, by design
        else:
            row["passed"] = bool(row["scored"]) and all(row["threshold_pass"].values())

    # --- counts and the exit code ---------------------------------------------------------------
    attempted = len(rows)
    scored = sum(1 for r in rows if r["scored"])
    failed_to_score = attempted - scored

    code_rows = [r for r in rows if r["grader"] == "code"]
    code_passed = [r for r in code_rows if r["passed"]]
    retrieval_rows = [r for r in rows if r["kind"] == "retrieval"]
    control_row = next((r for r in rows if r["kind"] == "control"), None)
    subtle_row = next((r for r in rows if r["kind"] == "control_subtle"), None)

    aggregate: Dict[str, Optional[float]] = {}
    for metric_name in RETRIEVAL_METRICS:
        values = [
            r["scores"].get(metric_name)
            for r in retrieval_rows
            if _is_number(r["scores"].get(metric_name))
        ]
        aggregate[metric_name] = round(sum(values) / len(values), 3) if values else None

    control_verdict = "NOT_RUN"
    if control_row is not None:
        if control_row.get("control_failed_as_required"):
            control_verdict = "FAILED_AS_REQUIRED"
        elif not control_row["scored"]:
            control_verdict = "NOT_SCORED"
        else:
            control_verdict = "PASSED_WHICH_IS_A_BROKEN_GRADER"

    reasons: List[str] = []
    if failed_to_score:
        reasons.append(
            f"{failed_to_score} case(s) could not be scored; a run that cannot score a case does "
            f"not pass"
        )
    if len(code_passed) != len(code_rows):
        failed = [r["id"] for r in code_rows if not r["passed"]]
        reasons.append(f"code-graded case(s) failed: {failed}")
    if control_verdict != "FAILED_AS_REQUIRED":
        reasons.append(
            f"the must-fail control is {control_verdict}: if a deliberately wrong answer is not "
            f"failed, the grading is broken and no other result can be trusted"
        )

    below = [
        f"{r['id']}:{m}={r['scores'].get(m)}"
        for r in retrieval_rows
        for m, ok in r["threshold_pass"].items()
        if not ok
    ]

    finished_at = _now_iso()
    results = {
        "golden_set_id": golden.get("golden_set_id"),
        "golden_set_file": str(GOLDEN_PATH.relative_to(REPO_ROOT).as_posix()),
        "library": "ragas",
        "library_version": ragas.__version__,
        "judge_model": judge_model,
        "judge_path": judge_path,
        "judge_notes": judge_notes,
        "judge_tokens_estimated": pacer.tokens_used,
        "judge_cost_estimated_usd": pacer.estimated_cost_usd,
        "judge_spend_cap_usd": pacer.spend_cap_usd,
        "api_base": args.api,
        "backend_health": health,
        "thresholds": thresholds,
        "judge_context_policy": golden.get("exclude_from_judge_context"),
        "judge_context_source": context_builder.detail,
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_seconds": round(time.monotonic() - started_monotonic, 1),
        "counts": {
            "attempted": attempted,
            "scored": scored,
            "failed_to_score": failed_to_score,
            "code_graded_passed": len(code_passed),
            "code_graded_total": len(code_rows),
        },
        "control": control_verdict,
        "subtle_control": (subtle_row or {}).get("subtle_control"),
        "subtle_control_faithfulness": (subtle_row or {}).get("scores", {}).get("faithfulness"),
        "aggregate_retrieval": aggregate,
        "retrieval_below_threshold": below,
        "exit_code": 1 if reasons else 0,
        "failure_reasons": reasons,
        "cases": rows,
    }
    RESULTS_PATH.write_text(
        json.dumps(results, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )

    # --- the transcript -------------------------------------------------------------------------
    print("\n  judge-graded cases")
    for row in rows:
        if row["grader"] != "judge":
            continue
        pretty = {k: (round(v, 3) if _is_number(v) else v) for k, v in row["scores"].items()}
        extra = ""
        if row["kind"] == "control":
            extra = f"  control={control_verdict}"
        elif row["kind"] == "control_subtle":
            extra = f"  subtle_control={row.get('subtle_control')} (reported only, never gated)"
        print(f"    {row['id']:34s} {pretty}{extra}")
    for note in judge_notes:
        print(f"    note: {note}")
    if below:
        print(f"\n  RETRIEVAL_BELOW_THRESHOLD: {below}")
        print(
            "    Reported, not the gate: the brief's blocking conditions are the code-graded half, "
            "a case that cannot be scored, and the control. Judge scores are evidence."
        )

    print(f"\n  results written to {RESULTS_PATH}")
    print(
        f"  judge spend    ~{pacer.tokens_used} estimated tokens, "
        f"~${pacer.estimated_cost_usd:.2f} of the ${pacer.spend_cap_usd:.2f} cap"
    )
    if subtle_row is not None:
        faith = subtle_row.get("scores", {}).get("faithfulness")
        print(
            f"JUDGE_CALIBRATION: subtle_control={subtle_row.get('subtle_control')} "
            f"faithfulness={round(faith, 3) if _is_number(faith) else faith}"
        )

    aggregate_text = " ".join(f"{k}={v}" for k, v in aggregate.items())
    print(
        f"EVAL: attempted={attempted} scored={scored} failed_to_score={failed_to_score} "
        f"code_graded={len(code_passed)}/{len(code_rows)} control={control_verdict} "
        f"judge={judge_model} aggregate={aggregate_text}"
    )
    if reasons:
        print("EVAL FAILED:")
        for reason in reasons:
            print(f"  - {reason}")

    print(
        "\nWhat this eval does NOT measure: judge scores are evidence, not proof (a judge can miss "
        "a subtle contradiction and it favours longer answers, which is exactly what the subtle "
        "control is there to expose); the part that actually blocks is the code-graded half, which "
        "is arithmetic and string presence, not opinion; these ten cases are synthetic and are not "
        "the real distribution of questions a supervisor asks on a bad shift; and latency, cost per "
        "answer, and behaviour under load are not measured here at all."
    )
    return 1 if reasons else 0


if __name__ == "__main__":
    raise SystemExit(main())
