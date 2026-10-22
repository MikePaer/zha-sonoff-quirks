"""Pure decision logic for the irrigation run log.

No Home Assistant imports on purpose: everything here is unit-testable with
plain pytest (the venv has no ``homeassistant`` package), and the HA-facing
glue in ``history.py`` stays thin.

Times are handled as POSIX timestamps (float seconds); the caller converts
from/to datetimes.
"""

from __future__ import annotations

from typing import Any

#: A duration run may close a few seconds shy of its target (the device's
#: internal tick), and still count as completed.
DURATION_TOLERANCE_S = 10.0
#: Volume reports are integer liters; one liter under target is still a
#: completed run, not a manual stop.
VOLUME_TOLERANCE_L = 1.0
#: A session report older than the run start by more than this belongs to the
#: previous run (clock jitter allowance between HA state writes).
SESSION_FRESH_SLACK_S = 2.0


def classify_reason(
    mode: str | None,
    target: float | None,
    duration_s: float,
    liters: float | None,
) -> str:
    """Why the run ended: ``completed`` (target plausibly reached) or ``manual_off``.

    The SWV-ZF2 closes the valve on-device when the configured target is
    reached, without telling HA why; the only way to distinguish the firmware
    auto-close from a user's early stop is to compare the outcome with the
    target that was active when the run started.
    """
    if mode == "capacity":
        if target and liters is not None and liters >= target - VOLUME_TOLERANCE_L:
            return "completed"
        return "manual_off"
    if mode is not None and mode.startswith("duration"):
        if target and duration_s >= target * 60.0 - DURATION_TOLERANCE_S:
            return "completed"
        return "manual_off"
    return "manual_off"


def session_liters(
    session_value: Any,
    session_updated_ts: float | None,
    run_start_ts: float | None,
    ambiguous: bool,
) -> float | None:
    """Liters delivered in this run, from the global session_volume sensor.

    Returns None when the value cannot be attributed to the run:

    * ``ambiguous`` — both channels were open at some point during the run;
      the session feed is global (endpoint 1) so per-channel attribution is
      impossible;
    * the sensor never updated since the run started — its value is the
      PREVIOUS run's total (the quirk deliberately persists it);
    * the value is not a number.
    """
    if ambiguous:
        return None
    try:
        value = float(session_value)
    except (TypeError, ValueError):
        return None
    if session_updated_ts is None or run_start_ts is None:
        return None
    if session_updated_ts < run_start_ts - SESSION_FRESH_SLACK_S:
        return None
    return max(0.0, value)


def build_record(
    *,
    start_iso: str | None,
    end_iso: str,
    duration_s: float,
    liters: float | None,
    mode: str | None,
    target: float | None,
    source: str,
    reason: str,
    channel: str,
    plan_index: int | None = None,
) -> dict[str, Any]:
    """Build the run record persisted in the store and shown by the card.

    ``source`` is ``integration`` (this integration's services), ``manual``
    (any other HA-initiated start: UI, automation, script), ``physical`` (no
    HA context: the valve's button) or ``scheduled`` (an on-valve plan, whose
    slot is then in ``plan_index``).
    """
    record = {
        "start": start_iso,
        "end": end_iso,
        "duration_s": int(round(duration_s)),
        "liters": round(liters, 2) if liters is not None else None,
        "mode": mode,
        "target": target,
        "source": source,
        "reason": reason,
        "channel": channel,
    }
    if plan_index is not None:
        record["plan_index"] = plan_index
    return record


def source_from_context(user_id: str | None, parent_id: str | None) -> str:
    """Origin of a switch's off->on transition with no pending attribution.

    ZHA writes the state in the calling service's context, so an HA-initiated
    start (UI, automation, script) carries a user or a parent; a start the
    valve reports on its own — button press, or an on-valve plan before its
    0x501F report re-tags it as ``scheduled`` — carries neither.
    """
    return "manual" if (user_id or parent_id) else "physical"


def should_discard(
    source: str, duration_s: float, min_run_s: float, forced: bool
) -> bool:
    """Whether a finished run is a glitch to drop rather than record.

    Only sub-``min_run_s`` *manual* or *physical* toggles are discarded (a
    physical double-tap or a Zigbee blip). Integration-driven and scheduled
    runs are always kept, as are forced finalizations (shutdown recovery,
    superseded runs): those paths record deliberately.
    """
    return (
        not forced and source in ("manual", "physical") and duration_s < min_run_s
    )
