"""Pure logic for the valve's on-device irrigation plans.

No Home Assistant imports on purpose, same as ``history_logic.py``: the plan
fields, the storage format, the sequential-program arithmetic and the
next-run computation are all unit-testable with plain pytest. The HA glue
(services, store, Zigbee I/O) lives in ``schedules.py``.

Vocabulary:

* a **plan** is one of the 6 slots per line the valve stores and runs by
  itself (:class:`IrrigationPlan`, encoded by the quirk);
* the **desired** plans are HA's source of truth; each slot carries a status:
  ``pending`` (being written), ``on_valve`` (confirmed by the valve) or
  ``failed`` (rejected, unconfirmed, or changed on the valve since);
* a **program** is an ordered list of zones (line switches) run back to back
  from one start time — the dashboard's "start time, minutes per zone, zone
  on/off". Each zone's plan is computed here and written to one fixed slot.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

try:
    from .quirks.sonoff_swv_zf2 import (
        EVERY_DAY_MASK,
        PLAN_DEFAULT_FAIL_SAFE,
        PLAN_SLOTS,
        WEEKDAY_BITS,
        IrrigationAmountUnit,
        IrrigationMode,
        IrrigationPlan,
        PlanLoopType,
    )
except ImportError:  # loaded standalone by the tests (no package parent)
    from sonoff_swv_zf2 import (  # type: ignore[no-redef]
        EVERY_DAY_MASK,
        PLAN_DEFAULT_FAIL_SAFE,
        PLAN_SLOTS,
        WEEKDAY_BITS,
        IrrigationAmountUnit,
        IrrigationMode,
        IrrigationPlan,
        PlanLoopType,
    )

STATUS_PENDING = "pending"
STATUS_ON_VALVE = "on_valve"
STATUS_FAILED = "failed"

REPEAT_DAILY = "daily"
REPEAT_WEEKDAYS = "weekdays"
REPEAT_ODD_DAYS = "odd_days"
REPEAT_EVEN_DAYS = "even_days"
REPEAT_EVERY_N_DAYS = "every_n_days"
REPEATS = (
    REPEAT_DAILY,
    REPEAT_WEEKDAYS,
    REPEAT_ODD_DAYS,
    REPEAT_EVEN_DAYS,
    REPEAT_EVERY_N_DAYS,
)

#: Python's date.weekday() order (Monday = 0) -> the valve's day names.
WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
#: HA's own short spellings (time-condition ``weekday:``) are accepted too.
_WEEKDAY_ALIASES = {name[:3]: name for name in WEEKDAY_NAMES} | {
    name: name for name in WEEKDAY_NAMES
}

#: How far ahead next_run looks. Every repeat type fires within 30 days.
_NEXT_RUN_HORIZON_DAYS = 62


def parse_start_time(value: Any) -> int:
    """``"05:00"``, ``"05:00:00"`` or a ``time`` -> seconds after midnight.

    Minute resolution: the valve takes seconds, but Z2M and the eWeLink app
    only ever send whole minutes, so seconds are dropped rather than sent.
    """
    if isinstance(value, time):
        return value.hour * 3600 + value.minute * 60
    parts = str(value).strip().split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        raise ValueError(f"start time must be HH:MM, got {value!r}")
    hours, minutes = int(parts[0]), int(parts[1])
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError(f"start time must be HH:MM, got {value!r}")
    return hours * 3600 + minutes * 60


def format_start(seconds: int) -> str:
    """Seconds after midnight -> ``"HH:MM"``."""
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"


def weekday_mask(days: Iterable[str]) -> int:
    """Day names (``monday``/``mon`` ...) -> the valve's bitmask (bit0 = Sun)."""
    mask = 0
    for day in days:
        name = _WEEKDAY_ALIASES.get(str(day).strip().lower())
        if name is None:
            raise ValueError(f"unknown weekday {day!r}")
        mask |= WEEKDAY_BITS[name]
    return mask


def weekdays_from_mask(mask: int) -> list[str]:
    """Turn the valve's bitmask into day names, Monday first."""
    return [name for name in WEEKDAY_NAMES if mask & WEEKDAY_BITS[name]]


def build_plan(
    *,
    index: int,
    start_time: Any,
    enable_date: date,
    created: int,
    repeat: str = REPEAT_DAILY,
    weekdays: Iterable[str] | None = None,
    interval_days: int | None = None,
    minutes: int | None = None,
    liters: int | None = None,
    fail_safe_minutes: int | None = None,
    enabled: bool = True,
) -> IrrigationPlan:
    """Service-level fields -> an encodable plan.

    Exactly one of ``minutes`` (duration mode) or ``liters`` (capacity mode).
    Fields the chosen mode does not use keep the Z2M defaults the quirk
    declares, so the bytes on the air are the ones Z2M sends.
    """
    if (minutes is None) == (liters is None):
        raise ValueError("give exactly one of minutes or liters")
    if index not in range(PLAN_SLOTS):
        raise ValueError(f"plan index must be 0..{PLAN_SLOTS - 1}")

    if repeat == REPEAT_DAILY:
        loop_type, loop_value = PlanLoopType.weekdays, EVERY_DAY_MASK
    elif repeat == REPEAT_WEEKDAYS:
        loop_type, loop_value = PlanLoopType.weekdays, weekday_mask(weekdays or ())
        if not loop_value:
            raise ValueError("weekdays repeat needs at least one day")
    elif repeat == REPEAT_ODD_DAYS:
        loop_type, loop_value = PlanLoopType.odd_days, 0
    elif repeat == REPEAT_EVEN_DAYS:
        loop_type, loop_value = PlanLoopType.even_days, 0
    elif repeat == REPEAT_EVERY_N_DAYS:
        if interval_days is None or not 1 <= int(interval_days) <= 30:
            raise ValueError("every_n_days repeat needs interval_days 1..30")
        loop_type, loop_value = PlanLoopType.day_interval, int(interval_days)
    else:
        raise ValueError(f"repeat must be one of {', '.join(REPEATS)}")

    common: dict[str, Any] = {
        "index": index,
        "enabled": bool(enabled),
        "loop_type": loop_type,
        "loop_value": loop_value,
        "enable_date": enable_date,
        "start_seconds": parse_start_time(start_time),
        "created": int(created),
    }
    if minutes is not None:
        if not 1 <= int(minutes) <= 719:
            raise ValueError("minutes must be 1..719")
        return IrrigationPlan(
            **common, mode=IrrigationMode.duration, total_duration=int(minutes)
        )
    if not 1 <= int(liters) <= 10000:
        raise ValueError("liters must be 1..10000")
    return IrrigationPlan(
        **common,
        mode=IrrigationMode.capacity,
        amount_unit=IrrigationAmountUnit.liter,
        amount=int(liters),
        fail_safe=(
            PLAN_DEFAULT_FAIL_SAFE
            if fail_safe_minutes is None
            else int(fail_safe_minutes)
        ),
    )


# ── storage format (JSON) ──


def plan_to_dict(plan: IrrigationPlan) -> dict[str, Any]:
    """Plan -> JSON-safe dict for the store (lossless)."""
    return {
        "index": plan.index,
        "enabled": plan.enabled,
        "loop_type": PlanLoopType(plan.loop_type).name,
        "loop_value": plan.loop_value,
        "enable_date": plan.enable_date.isoformat(),
        "start": format_start(plan.start_seconds),
        "start_seconds": plan.start_seconds,
        "mode": IrrigationMode(plan.mode).name,
        "total_duration": plan.total_duration,
        "irrigation_duration": plan.irrigation_duration,
        "interval_duration": plan.interval_duration,
        "amount_unit": IrrigationAmountUnit(plan.amount_unit).name,
        "amount": plan.amount,
        "fail_safe": plan.fail_safe,
        "created": plan.created,
    }


def plan_from_dict(data: Mapping[str, Any]) -> IrrigationPlan:
    """Inverse of :func:`plan_to_dict`."""
    return IrrigationPlan(
        index=int(data["index"]),
        enabled=bool(data["enabled"]),
        loop_type=PlanLoopType[data["loop_type"]],
        loop_value=int(data["loop_value"]),
        enable_date=date.fromisoformat(data["enable_date"]),
        start_seconds=int(data["start_seconds"]),
        mode=IrrigationMode[data["mode"]],
        total_duration=int(data["total_duration"]),
        irrigation_duration=int(data["irrigation_duration"]),
        interval_duration=int(data["interval_duration"]),
        amount_unit=IrrigationAmountUnit[data["amount_unit"]],
        amount=int(data["amount"]),
        fail_safe=int(data["fail_safe"]),
        created=int(data["created"]),
    )


def describe_plan(plan: IrrigationPlan) -> dict[str, Any]:
    """Human-oriented summary for sensor attributes / the card."""
    loop_type = PlanLoopType(plan.loop_type)
    if loop_type == PlanLoopType.weekdays and plan.loop_value == EVERY_DAY_MASK:
        repeat: dict[str, Any] = {"repeat": REPEAT_DAILY}
    elif loop_type == PlanLoopType.weekdays:
        repeat = {
            "repeat": REPEAT_WEEKDAYS,
            "weekdays": weekdays_from_mask(plan.loop_value),
        }
    elif loop_type == PlanLoopType.day_interval:
        repeat = {"repeat": REPEAT_EVERY_N_DAYS, "interval_days": plan.loop_value}
    else:
        repeat = {"repeat": loop_type.name}
    amount: dict[str, Any] = (
        {"liters": plan.amount, "fail_safe_minutes": plan.fail_safe}
        if IrrigationMode(plan.mode) == IrrigationMode.capacity
        else {"minutes": plan.total_duration}
    )
    return {
        "index": plan.index,
        "enabled": plan.enabled,
        "start": format_start(plan.start_seconds),
        **repeat,
        **amount,
        "from": plan.enable_date.isoformat(),
    }


def plan_target(plan: IrrigationPlan) -> tuple[str, float]:
    """(mode option, target) of a plan, in the run log's vocabulary."""
    if IrrigationMode(plan.mode) == IrrigationMode.capacity:
        return "capacity", float(plan.amount)
    return "duration", float(plan.total_duration)


# ── when does a plan fire ──


def runs_on(plan: IrrigationPlan, day: date) -> bool:
    """Whether the plan's repeat rule selects this local date.

    Interpretation of the firmware's rules (not documented by Sonoff):
    odd/even refer to the day of the month, every-N-days counts from the
    plan's enable date. Nothing fires before the enable date.
    """
    if day < plan.enable_date:
        return False
    loop_type = PlanLoopType(plan.loop_type)
    if loop_type == PlanLoopType.weekdays:
        return bool(plan.loop_value & WEEKDAY_BITS[WEEKDAY_NAMES[day.weekday()]])
    if loop_type == PlanLoopType.odd_days:
        return day.day % 2 == 1
    if loop_type == PlanLoopType.even_days:
        return day.day % 2 == 0
    if loop_type == PlanLoopType.day_interval:
        return (
            plan.loop_value > 0 and (day - plan.enable_date).days % plan.loop_value == 0
        )
    return False


def next_run(
    plans: Iterable[IrrigationPlan],
    now: datetime,
    tz: tzinfo,
    rain_delay_until: datetime | None = None,
) -> tuple[datetime, IrrigationPlan] | None:
    """Earliest start after ``now`` among the enabled plans, or None.

    Starts before ``rain_delay_until`` are skipped: the valve suppresses its
    plans until the delay ends.
    """
    best: tuple[datetime, IrrigationPlan] | None = None
    today = now.astimezone(tz).date()
    for plan in plans:
        if not plan.enabled:
            continue
        clock = time(plan.start_seconds // 3600, plan.start_seconds % 3600 // 60)
        for offset in range(_NEXT_RUN_HORIZON_DAYS):
            day = today + timedelta(days=offset)
            if not runs_on(plan, day):
                continue
            start = datetime.combine(day, clock, tzinfo=tz)
            if start <= now:
                continue
            if rain_delay_until is not None and start < rain_delay_until:
                continue
            if best is None or start < best[0]:
                best = (start, plan)
            break
    return best


# ── sequential program ──


@dataclass(frozen=True)
class ProgramZone:
    """One line in the program, already resolved to its current settings."""

    key: str  # the line's switch entity_id
    minutes: int
    enabled: bool


def program_starts(
    start_seconds: int, zones: Iterable[ProgramZone], gap_minutes: int
) -> dict[str, int]:
    """Start (seconds after midnight) of every ENABLED zone, back to back.

    Each zone starts after the previous enabled zone's minutes plus the gap;
    disabled zones are skipped and get no entry. The whole program must
    finish the same day: a plan's start is a time of day, so a run crossing
    midnight would land on the wrong day's schedule.
    """
    starts: dict[str, int] = {}
    cursor = start_seconds
    for zone in zones:
        if not zone.enabled:
            continue
        if not 1 <= zone.minutes <= 719:
            raise ValueError(f"{zone.key}: minutes must be 1..719")
        starts[zone.key] = cursor
        cursor += (zone.minutes + gap_minutes) * 60
    last_end = cursor - gap_minutes * 60
    if starts and last_end > 24 * 3600:
        raise ValueError(
            f"the program would end at {format_start(last_end % 86400)} the next day; "
            "start earlier or shorten the zones"
        )
    return starts


def program_plans(
    *,
    start_seconds: int,
    zones: Iterable[ProgramZone],
    gap_minutes: int,
    slot: int,
    current: Mapping[str, IrrigationPlan | None],
    today: date,
    created: int,
) -> dict[str, IrrigationPlan]:
    """Desired program-slot plan per zone; only zones whose plan CHANGES.

    * enabled zone: a daily duration plan at its computed start;
    * disabled zone: its stored plan with the enabled flag cleared (the plan
      stays on the valve, re-enabling restores it); nothing if it never had
      one.

    An unchanged plan keeps its enable date and creation stamp, so applying
    the same settings twice writes nothing.
    """
    zones = list(zones)
    starts = program_starts(start_seconds, zones, gap_minutes)
    changed: dict[str, IrrigationPlan] = {}
    for zone in zones:
        previous = current.get(zone.key)
        if zone.enabled:
            wanted = build_plan(
                index=slot,
                start_time=format_start(starts[zone.key]),
                minutes=zone.minutes,
                enable_date=previous.enable_date if previous else today,
                created=created,
            )
        elif previous is not None:
            wanted = replace(previous, enabled=False, created=created)
        else:
            continue
        if previous is None or not previous.same_settings(wanted):
            changed[zone.key] = wanted
    return changed
