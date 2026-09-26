# backend/tools/actions.py -- owns the only write-shaped action in FloorGuide: drafting a
# work order. Owns the promise that drafting is all it does: it files, sends and posts nothing.
"""The work-order draft.

This module imports no HTTP client, no mail client, no queue, no database. There is nothing
here that could reach a real maintenance system even if a model asked it to. It builds an
object and hands it back; a human decides what happens next, at the approval gate in
backend/graph.py.

The fields are not the model's words. `asset_id`, `task` and `source_refs` come off the
asset record, and `priority` is DERIVED from the rule result -- OVERDUE is high, DUE_SOON is
normal -- so a suggested priority that disagrees with the arithmetic is overridden and
logged rather than believed.

The draft carries a fingerprint over its own contents. If the work order changes between
the screen and the click, the fingerprint no longer matches and the gate refuses the
approval: the supervisor approves the thing she read, not a thing that moved underneath her.

No person-level identifier goes in a draft, by design; the approver's identity is merged at
filing time, after approval, in a step no model touches.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional, Sequence

from pydantic import BaseModel, Field

from backend import guards

log = guards.get_logger(__name__)

Priority = Literal["low", "normal", "high"]

# The arithmetic decides urgency, not the model. "low" exists in the API contract but nothing
# in this prototype produces it: a machine that is not due gets no work order at all.
PRIORITY_FOR_STATUS: dict[str, Priority] = {"OVERDUE": "high", "DUE_SOON": "normal"}

FINGERPRINT_CHARS = 12


class DraftRefused(Exception):
    """Typed refusal: this is not a state of affairs that should produce a work order."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class WorkOrderDraft(BaseModel):
    draft_id: str
    fingerprint: str
    asset_id: str
    task: str
    priority: Priority
    source_refs: list[str] = Field(default_factory=list)
    created_at: str


def fingerprint_of(asset_id: str, task: str, priority: str, source_refs: Sequence[str]) -> str:
    """First 12 hex of sha256 over asset_id|task|priority|source_refs (refs comma-joined).

    Written out in full so the caller, the front end and the eval can all recompute it and
    get the same answer.
    """
    material = "|".join([asset_id, task, priority, ",".join(source_refs)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]


def _clean_refs(source_refs: Optional[Sequence[str]]) -> list[str]:
    out: list[str] = []
    for ref in source_refs or []:
        text = str(ref).strip()
        if text and text not in out:
            out.append(text)
    return out


def draft_work_order(
    asset_id: str,
    task: str,
    priority: Optional[str],
    source_refs: Sequence[str],
    rule_result: Any,
) -> WorkOrderDraft:
    """Build a work-order draft. Returns an object; files, sends and posts nothing.

    `rule_result` is the MaintenanceResult (or its dict) that justifies the work. It is
    required: a work order with no arithmetic behind it is exactly the thing this prototype
    refuses to produce.
    """
    if not asset_id or not str(asset_id).strip():
        raise DraftRefused("no asset id: a work order must name the machine")
    if not task or not str(task).strip():
        raise DraftRefused("no task: a work order must say what to do")

    status = _status_of(rule_result)
    if status is None:
        raise DraftRefused("no rule result: nothing may be drafted without the service-due arithmetic")
    if status not in PRIORITY_FOR_STATUS:
        raise DraftRefused(
            "the service-due rule says " + status + "; a machine that is not due gets no work order"
        )

    required = PRIORITY_FOR_STATUS[status]
    if priority and priority != required:
        log.warning(
            "suggested priority %r overridden to %r: the rule says %s", priority, required, status
        )

    refs = _clean_refs(source_refs)
    if not refs:
        raise DraftRefused("no source reference: a work order must cite the manual section behind it")

    clean_asset = str(asset_id).strip()
    clean_task = str(task).strip()
    draft = WorkOrderDraft(
        draft_id="WO-DRAFT-" + uuid.uuid4().hex[:12],
        fingerprint=fingerprint_of(clean_asset, clean_task, required, refs),
        asset_id=clean_asset,
        task=clean_task,
        priority=required,
        source_refs=refs,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    log.info(
        "drafted %s for %s (%s, priority %s) -- held, not filed",
        draft.draft_id, draft.asset_id, status, draft.priority,
    )
    return draft


def _status_of(rule_result: Any) -> Optional[str]:
    if rule_result is None:
        return None
    if isinstance(rule_result, dict):
        status = rule_result.get("status")
    else:
        status = getattr(rule_result, "status", None)
    return str(status) if status else None


# --------------------------------------------------------------------------------------
# The simulated confirmation id handed back after a human approves. This is NOT a tool and
# is not on any worker's allow-list: it mints a string and touches nothing. Nothing is filed
# anywhere real; the id exists so the screen can show that the click was recorded.
# --------------------------------------------------------------------------------------
_confirmation_lock = threading.Lock()
_confirmation_counter = 0


def next_confirmation_id() -> str:
    global _confirmation_counter
    with _confirmation_lock:
        _confirmation_counter += 1
        return "WO-SIM-" + str(_confirmation_counter)
