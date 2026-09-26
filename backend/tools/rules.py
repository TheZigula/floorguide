# backend/tools/rules.py -- owns the one piece of arithmetic in FloorGuide: whether a machine
# is due for service. Owns the refusal, too: bad input is named, never guessed around.
"""The service-due rule.

A pure function. Three integers in, one typed result out. No model, no network, no clock,
no file reads: the same inputs give the same answer every time, which is why the golden set
can call it to compute its own expected values.

THE SEAM: this function takes structured fields ONLY. The asset record's `notes` field
(P-102's says "meter probably wrong, ignore") is prose written by a person and never gets
here -- backend/assets.py drops it at load. An injected sentence in a manual or a note
cannot change the number on the screen, because prose has no path into this file.

It refuses rather than guessing. A refusal NAMES the field, so a supervisor knows what to
go and re-read off the machine.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Status = Literal["OK", "DUE_SOON", "OVERDUE"]

# A machine inside this fraction of its interval is called DUE_SOON rather than OK.
DUE_SOON_FRACTION = 0.10


class RuleRefusal(Exception):
    """Typed refusal. `field` is the input to go and re-read; `reason` says what is wrong.

    Worked example, the M-31 record in this prototype: calling

        maintenance_due("M-31", meter_hours_now=2950, meter_hours_at_last_service=3100,
                        service_interval_hours=400)

    raises RuleRefusal(field="meter_hours_now", reason="meter reading went backwards",
    value=2950). The rule does NOT swap the readings, take the absolute value, or fall back
    to an estimate. A meter reading lower than it did at the last service means somebody
    mis-keyed it or the meter was replaced, and only a person on the floor can settle which.
    """

    def __init__(self, field: str, reason: str, value: Any = None) -> None:
        self.field = field
        self.reason = reason
        self.value = value
        super().__init__(field + ": " + reason)


class MaintenanceInput(BaseModel):
    """Typed inputs. Reached only after the field-by-field checks below, so a refusal can
    name the field in plain words instead of handing back a pydantic traceback."""

    asset_id: str = Field(min_length=1)
    meter_hours_now: int
    meter_hours_at_last_service: int
    service_interval_hours: int = Field(gt=0)


class MaintenanceResult(BaseModel):
    asset_id: str
    hours_since_service: int
    hours_remaining: int
    status: Status
    overdue_by: int


def _whole_number(field: str, value: Any) -> int:
    """Return value as an int, or refuse NAMING the field. Bools are not hour readings."""
    if value is None:
        raise RuleRefusal(field, "is missing", value)
    if isinstance(value, bool):
        raise RuleRefusal(field, "is not a meter reading", value)
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str):
        try:
            number = int(value.strip())
        except (TypeError, ValueError) as exc:
            raise RuleRefusal(field, "is not a whole number of hours", value) from exc
    else:
        raise RuleRefusal(field, "is not a whole number of hours", value)
    if number < 0:
        raise RuleRefusal(field, "is negative", value)
    return number


def maintenance_due(
    asset_id: str,
    meter_hours_now: Optional[int],
    meter_hours_at_last_service: Optional[int],
    service_interval_hours: Optional[int],
) -> MaintenanceResult:
    """Is this machine due for service? Arithmetic only.

        hours_since = meter_hours_now - meter_hours_at_last_service
        remaining   = service_interval_hours - hours_since
        OVERDUE  when remaining < 0          (overdue_by = -remaining)
        DUE_SOON when 0 <= remaining <= 10% of the interval
        OK       otherwise

    Refuses, with the field named, when: any field is missing or None; any value is
    negative; the interval is zero or less; or the meter reads lower now than it did at the
    last service. See RuleRefusal for the worked M-31 example.
    """
    if not asset_id or not str(asset_id).strip():
        raise RuleRefusal("asset_id", "is missing", asset_id)

    now = _whole_number("meter_hours_now", meter_hours_now)
    last = _whole_number("meter_hours_at_last_service", meter_hours_at_last_service)
    interval = _whole_number("service_interval_hours", service_interval_hours)

    if interval <= 0:
        raise RuleRefusal(
            "service_interval_hours", "must be greater than zero hours", service_interval_hours
        )
    if now < last:
        raise RuleRefusal("meter_hours_now", "meter reading went backwards", meter_hours_now)

    checked = MaintenanceInput(
        asset_id=str(asset_id).strip(),
        meter_hours_now=now,
        meter_hours_at_last_service=last,
        service_interval_hours=interval,
    )

    hours_since = checked.meter_hours_now - checked.meter_hours_at_last_service
    remaining = checked.service_interval_hours - hours_since

    if remaining < 0:
        status: Status = "OVERDUE"
    elif remaining <= DUE_SOON_FRACTION * checked.service_interval_hours:
        status = "DUE_SOON"
    else:
        status = "OK"

    return MaintenanceResult(
        asset_id=checked.asset_id,
        hours_since_service=hours_since,
        hours_remaining=remaining,
        status=status,
        overdue_by=max(0, -remaining),
    )
