"""On-valve schedules through real Home Assistant: services, store, run log."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.zha_sonoff_quirks.quirks import sonoff_swv_zf2 as quirk
from custom_components.zha_sonoff_quirks.schedule_logic import (
    STATUS_FAILED,
    STATUS_ON_VALVE,
    format_start,
    plan_from_dict,
)

DOMAIN = "zha_sonoff_quirks"


async def _set_plan(hass, valve, channel="1", **data):
    await hass.services.async_call(
        DOMAIN,
        "set_irrigation_plan",
        {"device_id": valve.device_id, "channel": channel, **data},
        blocking=True,
    )


# ── set / remove ──


async def test_set_plan_confirmed_by_status(hass: HomeAssistant, manager, valves):
    """Status 0 from the valve: the slot is on the valve."""
    front = valves["front"]

    await _set_plan(hass, front, index=0, start_time="05:00", minutes=11)

    [(kind, endpoint_id, plan)] = front.sent
    assert (kind, endpoint_id) == ("set", 1)
    assert quirk.encode_irrigation_plan(plan)[:24].hex() == (
        "0001037f"
        + quirk._date_to_device(plan.enable_date).to_bytes(4, "big").hex()
        + "0000004650000b0002000301001e000a"
    )
    slot = manager.slots(front.device_id, "1")[0]
    assert slot["status"] == STATUS_ON_VALVE
    assert plan_from_dict(slot["plan"]) == plan


async def test_line_b_goes_to_endpoint_2(hass, manager, valves):
    front = valves["front"]

    await _set_plan(hass, front, channel="B", index=1, start_time="05:12", minutes=11)

    assert front.sent[0][:2] == ("set", 2)
    assert manager.slots(front.device_id, "2")[1]["status"] == STATUS_ON_VALVE


async def test_no_status_but_matching_report_confirms(hass, manager, valves):
    front = valves["front"]
    front.set_status, front.report_after_set = None, True

    await _set_plan(hass, front, index=2, start_time="06:00", liters=40)

    assert manager.slots(front.device_id, "1")[2]["status"] == STATUS_ON_VALVE


async def test_no_answer_is_failed_and_service_says_so(hass, manager, valves):
    front = valves["front"]
    front.set_status = None

    with pytest.raises(HomeAssistantError, match="not confirmed"):
        await _set_plan(hass, front, index=0, start_time="05:00", minutes=11)

    slot = manager.slots(front.device_id, "1")[0]
    assert slot["status"] == STATUS_FAILED
    assert slot["error"] == "no confirmation from the valve"


async def test_rejected_status_is_failed(hass, manager, valves):
    front = valves["front"]
    front.set_status = 3

    with pytest.raises(HomeAssistantError):
        await _set_plan(hass, front, index=0, start_time="05:00", minutes=11)

    assert "status 3" in manager.slots(front.device_id, "1")[0]["error"]


async def test_invalid_plan_is_a_validation_error(hass, manager, valves):
    with pytest.raises(ServiceValidationError):
        await _set_plan(
            hass,
            valves["front"],
            index=0,
            start_time="05:00",
            minutes=11,
            repeat="weekdays",
        )
    assert valves["front"].sent == []


async def test_remove_waits_for_removed_report(hass, manager, valves):
    front = valves["front"]
    await _set_plan(hass, front, index=0, start_time="05:00", minutes=11)

    await hass.services.async_call(
        DOMAIN,
        "remove_irrigation_plan",
        {"device_id": front.device_id, "channel": "1", "index": 0},
        blocking=True,
    )

    assert front.sent[-1] == ("remove", 1, 0)
    assert manager.slots(front.device_id, "1") == {}


async def test_unconfirmed_remove_stays_for_resync(hass, manager, valves):
    front = valves["front"]
    front.report_removed = False

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "remove_irrigation_plan",
            {"device_id": front.device_id, "channel": "1", "index": 4},
            blocking=True,
        )

    assert manager.slots(front.device_id, "1")[4]["plan"] is None


async def test_resync_rewrites_every_slot(hass, manager, valves):
    front = valves["front"]
    await _set_plan(hass, front, index=0, start_time="05:00", minutes=11)
    front.sent.clear()

    await hass.services.async_call(
        DOMAIN, "resync_plans", {"device_id": front.device_id}, blocking=True
    )

    assert [s[0] for s in front.sent].count("set") == 1
    assert [s[0] for s in front.sent].count("remove") == 11


# ── unsolicited valve reports ──


async def test_report_with_other_settings_flags_the_slot(hass, manager, valves):
    """e.g. edited in the eWeLink app: HA notices and says so."""
    front = valves["front"]
    await _set_plan(hass, front, index=0, start_time="05:00", minutes=11)
    plan = front.sent[0][2]

    front.emit(
        1,
        "plan_report",
        plan=quirk.IrrigationPlan(**{**plan.__dict__, "total_duration": 30}),
    )
    await hass.async_block_till_done()

    slot = manager.slots(front.device_id, "1")[0]
    assert slot["status"] == STATUS_FAILED
    assert slot["reported"]["total_duration"] == 30


async def test_time_sync_is_recorded(hass, manager, valves):
    front = valves["front"]
    values = {"time": 1, "time_zone": -28800, "local_time": 2, "dst_shift": 3600}

    front.emit(1, "time_sync", attributes=[0], values=values, at=dt_util.utcnow())
    front.emit(1, "time_sync", attributes=[0], values=values, at=dt_util.utcnow())

    sync = manager.last_time_sync(front.device_id)
    assert sync["count"] == 2
    assert sync["time_zone"] == -28800


async def test_manager_sets_the_quirk_time_zone(hass, manager):
    assert str(quirk.local_timezone()) == "America/Los_Angeles"


# ── run source ──


async def test_scheduled_run_is_tagged_with_plan_and_its_target(hass, manager, valves):
    front = valves["front"]
    await _set_plan(hass, front, index=0, start_time="05:00", minutes=11)
    switch = front.switches["1"]
    run_log = hass.data[DOMAIN]["run_log"]

    # The valve opens by itself (fresh context), then reports 0x501F.
    hass.states.async_set(switch, "on", context=Context())
    await hass.async_block_till_done()
    assert run_log._data["channels"][switch]["in_flight"]["source"] == "physical"
    front.emit(
        1, "session", session_state=2, session_plan_index=0, session_scheduled=True
    )
    inf = run_log._data["channels"][switch]["in_flight"]

    assert (inf["source"], inf["plan_index"], inf["mode"], inf["target"]) == (
        "scheduled",
        0,
        "duration",
        11.0,
    )


async def test_session_report_before_switch_on_waits_for_the_run(hass, manager, valves):
    front = valves["front"]
    switch = front.switches["2"]
    run_log = hass.data[DOMAIN]["run_log"]

    front.emit(
        2, "session", session_state=0, session_plan_index=3, session_scheduled=True
    )
    hass.states.async_set(switch, "on", context=Context())
    await hass.async_block_till_done()

    inf = run_log._data["channels"][switch]["in_flight"]
    assert (inf["source"], inf["plan_index"]) == ("scheduled", 3)


async def test_ha_initiated_run_is_manual(hass, manager, valves):
    switch = valves["front"].switches["1"]
    run_log = hass.data[DOMAIN]["run_log"]

    hass.states.async_set(switch, "on", context=Context(user_id="someone"))
    await hass.async_block_till_done()

    assert run_log._data["channels"][switch]["in_flight"]["source"] == "manual"


# ── program (dashboard settings) ──


async def _helpers(hass):
    assert await async_setup_component(
        hass,
        "input_boolean",
        {
            "input_boolean": {
                f"{z}_enabled": {"initial": True} for z in ("fl", "fb", "bl", "bb")
            }
        },
    )
    assert await async_setup_component(
        hass,
        "input_number",
        {
            "input_number": {
                f"{z}_minutes": {"min": 1, "max": 60, "initial": 11}
                for z in ("fl", "fb", "bl", "bb")
            }
        },
    )
    assert await async_setup_component(
        hass,
        "input_datetime",
        {
            "input_datetime": {
                "irrigation_start": {"has_time": True, "initial": "05:00"}
            }
        },
    )


def _zones(valves):
    order = [
        ("front", "1", "fl"),
        ("front", "2", "fb"),
        ("back", "1", "bl"),
        ("back", "2", "bb"),
    ]
    return [
        {
            "switch": valves[v].switches[ch],
            "minutes_entity": f"input_number.{z}_minutes",
            "enabled_entity": f"input_boolean.{z}_enabled",
        }
        for v, ch, z in order
    ]


def _starts(valves):
    """Latest start written per (valve, endpoint), as HH:MM, with enabled flag."""
    out = {}
    for name in ("front", "back"):
        for kind, ep, plan in valves[name].sent:
            if kind == "set":
                out[(name, ep)] = (format_start(plan.start_seconds), plan.enabled)
    return out


async def test_program_writes_the_decided_schedule(hass, manager, valves):
    await _helpers(hass)

    await hass.services.async_call(
        DOMAIN,
        "set_program",
        {"start_entity": "input_datetime.irrigation_start", "zones": _zones(valves)},
        blocking=True,
    )

    assert _starts(valves) == {
        ("front", 1): ("05:00", True),
        ("front", 2): ("05:12", True),
        ("back", 1): ("05:24", True),
        ("back", 2): ("05:36", True),
    }
    # Front valve first, backyard last.
    assert [v for v in ("front", "back") if valves[v].sent] == ["front", "back"]


async def test_zone_toggle_off_disables_its_plan_and_shifts_later_zones(
    hass, manager, valves
):
    await _helpers(hass)
    await hass.services.async_call(
        DOMAIN,
        "set_program",
        {"start_entity": "input_datetime.irrigation_start", "zones": _zones(valves)},
        blocking=True,
    )
    for v in valves.values():
        v.sent.clear()

    await hass.services.async_call(
        "input_boolean",
        "turn_off",
        {"entity_id": "input_boolean.fb_enabled"},
        blocking=True,
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
    await hass.async_block_till_done()

    assert _starts(valves) == {
        ("front", 2): ("05:12", False),
        ("back", 1): ("05:12", True),
        ("back", 2): ("05:24", True),
    }


async def test_unchanged_program_writes_nothing(hass, manager, valves):
    await _helpers(hass)
    call = {"start_entity": "input_datetime.irrigation_start", "zones": _zones(valves)}
    await hass.services.async_call(DOMAIN, "set_program", call, blocking=True)
    for v in valves.values():
        v.sent.clear()

    await manager.async_apply_program()

    assert all(not v.sent for v in valves.values())


async def test_program_rejects_non_valve_switches(hass, manager, valves):
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "set_program",
            {
                "start_time": "05:00",
                "zones": [{"switch": "switch.kitchen", "minutes": 5}],
            },
            blocking=True,
        )


async def test_rain_delay(hass, manager, valves):
    front = valves["front"]

    await hass.services.async_call(
        DOMAIN, "rain_delay", {"device_id": front.device_id, "hours": 24}, blocking=True
    )

    kind, endpoint_id, end = front.sent[0]
    assert (kind, endpoint_id) == ("rain_delay", 1)
    assert manager.rain_delay_until(front.device_id) == end


# ── schedule sensor ──


async def test_schedule_sensor_shows_next_run_and_plans(hass, manager, valves):
    from homeassistant.helpers import device_registry as dr

    from custom_components.zha_sonoff_quirks.sensor import SwvScheduleSensor

    front = valves["front"]
    await _set_plan(hass, front, index=0, start_time="05:00", minutes=11)
    device = dr.async_get(hass).async_get(front.device_id)
    sensor = SwvScheduleSensor(device, front.switches["1"], "1", manager)
    sensor.hass = hass

    now = dt_util.now()
    nxt = sensor.native_value
    attrs = sensor.extra_state_attributes

    assert nxt > now
    assert (nxt.hour, nxt.minute) == (5, 0)
    assert attrs["plans"] == [
        {
            "index": 0,
            "enabled": True,
            "start": "05:00",
            "repeat": "daily",
            "minutes": 11,
            "from": now.date().isoformat(),
            "status": "on_valve",
        }
    ]
    assert attrs["next_plan_index"] == 0
