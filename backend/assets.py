# backend/assets.py -- owns reading an asset record off disk for the analyst, and owns the
# rule that prose on that record (the `notes` field) is dropped at load and never returned.
"""Asset records: structured data, not prose authority.

The loader returns a typed record with SIX structured fields. It deliberately does not
return `notes`. P-102's record carries the note "meter probably wrong, ignore"; that
sentence is prose written by a person, and prose must never reach the arithmetic. Because
the loader drops it here, no later change to a prompt can reintroduce it.

Missing values are preserved as None so `maintenance_due` can refuse and NAME the field,
rather than the loader guessing a default.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

ASSETS_DIR = Path(os.getenv("ASSETS_DIR", Path(__file__).resolve().parent.parent / "data" / "assets"))

# The only fields that leave this module. `notes` is absent ON PURPOSE.
ALLOWED_FIELDS = (
    "asset_id",
    "name",
    "line",
    "meter_hours_now",
    "meter_hours_at_last_service",
    "service_interval_hours",
    "service_task",
    "manual_ref",
)


class AssetRecordNotFound(LookupError):
    """No record on disk for that asset id."""


class AssetRecord(BaseModel):
    asset_id: str
    name: str = ""
    line: str = ""
    meter_hours_now: Optional[int] = None
    meter_hours_at_last_service: Optional[int] = None
    service_interval_hours: Optional[int] = None
    service_task: str = Field(default="scheduled service")
    manual_ref: str = ""


def asset_ids() -> list[str]:
    if not ASSETS_DIR.is_dir():
        return []
    return sorted(p.stem.upper() for p in ASSETS_DIR.glob("*.json"))


def load_asset_record(asset_id: Optional[str]) -> AssetRecord:
    """Read one record, keeping only ALLOWED_FIELDS. Raises AssetRecordNotFound."""
    if not asset_id:
        raise AssetRecordNotFound("no asset id given")
    wanted = str(asset_id).strip().upper()
    path = ASSETS_DIR / (wanted + ".json")
    if not path.is_file():
        for candidate in ASSETS_DIR.glob("*.json") if ASSETS_DIR.is_dir() else []:
            if candidate.stem.upper() == wanted:
                path = candidate
                break
        else:
            raise AssetRecordNotFound("no asset record for " + wanted)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AssetRecordNotFound("asset record for " + wanted + " is unreadable") from exc
    if not isinstance(raw, dict):
        raise AssetRecordNotFound("asset record for " + wanted + " is not an object")

    kept = {k: v for k, v in raw.items() if k in ALLOWED_FIELDS}
    kept.setdefault("asset_id", wanted)
    return AssetRecord(**kept)
