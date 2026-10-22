"""On-valve irrigation plans: HA's desired state, Zigbee writes and services.

HA is the only place plans are set and the source of truth for them; the
valve stores and runs them on its own (with HA down too). This module keeps
the desired plans per device / line / slot in a ``helpers.storage.Store``,
writes them through the quirk's cluster commands and tracks a status per
slot (``schedule_logic.STATUS_*``) from what the valve answers:

* 0x06 status reply (or a matching 0x09 report) -> ``on_valve``;
* a non-zero status, no answer within ``ACK_TIMEOUT_S``, or a later 0x09 /
  0x0A that contradicts HA -> ``failed`` (``resync_plans`` puts it right).

There is no command to list a valve's plans, so a slot's status is only as
fresh as the last answer; ``resync_plans`` rewrites all six slots per line.

The **program** is the dashboard's sequential schedule ("start time, minutes
per zone, zone on/off"): ``set_program`` stores it, with each setting either
a literal or a helper entity, and from then on this manager watches those
entities and rewrites the affected zones' plans whenever one changes —
which is also how turning a zone toggle off disables its plan on the valve.

Valve events reach this module through the quirk's listener hook
(``add_schedule_listener``), not ZHA: ZHA has no cluster handler for 0xFC11
commands. Commands go out through the zigpy device ZHA already manages.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.const import EVENT_CORE_CONFIG_UPDATE, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import (
    Event,
    HomeAssistant,
    ServiceCall,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import CHANNELS, DOMAIN, SWV_MODELS
from .helpers import find_swv_switches
from .history import SwvRunLog
from .quirks import sonoff_swv_zf2 as quirk
from .schedule_logic import (
    REPEAT_DAILY,
    REPEATS,
    STATUS_FAILED,
    STATUS_ON_VALVE,
    STATUS_PENDING,
    IrrigationPlan,
    ProgramZone,
    build_plan,
    parse_start_time,
    plan_from_dict,
    plan_target,
    plan_to_dict,
    program_plans,
)
from .services import _channel, _device_from_call

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}_schedules"
STORAGE_VERSION = 1

#: How long a write waits for the valve's 0x06 status / 0x09 / 0x0A answer.
#: The valve is a sleepy end device; its parent holds the frame until it polls.
ACK_TIMEOUT_S = 30.0
#: Settle time before a program re-apply, so dragging a slider or flipping
#: several toggles writes once.
PROGRAM_DEBOUNCE_S = 5.0
#: After an HA start, re-apply the stored program once (normally a no-op:
#: only plans that differ are written) to catch settings changed while down.
PROGRAM_STARTUP_DELAY_S = 120.0

SERVICE_SET_PLAN = "set_irrigation_plan"
SERVICE_REMOVE_PLAN = "remove_irrigation_plan"
SERVICE_RAIN_DELAY = "rain_delay"
SERVICE_RESYNC = "resync_plans"
SERVICE_SET_PROGRAM = "set_program"
SERVICE_CLEAR_PROGRAM = "clear_program"
SCHEDULE_SERVICES = (
    SERVICE_SET_PLAN,
    SERVICE_REMOVE_PLAN,
    SERVICE_RAIN_DELAY,
    SERVICE_RESYNC,
    SERVICE_SET_PROGRAM,
    SERVICE_CLEAR_PROGRAM,
)

_BAD_STATES = ("unknown", "unavailable", "none", "")


def schedule_signal(device_id: str) -> str:
    """Dispatcher signal fired when a device's plans or status change."""
    return f"{DOMAIN}_schedule_{device_id}"


class SwvScheduleManager:
    """Desired plans per valve line, their writes and their status."""

    def __init__(self, hass: HomeAssistant, run_log: SwvRunLog) -> None:
        """Prepare the (not yet loaded) store."""
        self.hass = hass
        self._run_log = run_log
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: dict[str, Any] = {"devices": {}, "program": None}
        self._locks: dict[str, asyncio.Lock] = {}
        # (ieee, endpoint_id) -> (matcher, future) of the write in progress.
        self._waiters: dict[
            tuple[str, int], tuple[Callable[[dict], bool], asyncio.Future]
        ] = {}
        self._unsubs: list[Callable[[], None]] = []
        self._program_unsub: Callable[[], None] | None = None
        self._program_debouncer: Debouncer | None = None

    # ── lifecycle ──
    async def async_setup(self) -> None:
        """Load the store, hook the quirk, start the program watcher."""
        loaded = await self._store.async_load()
        if isinstance(loaded, dict):
            self._data.update(loaded)
        self._data.setdefault("devices", {})
        self._apply_time_zone()
        self._unsubs.append(quirk.add_schedule_listener(self._on_quirk_event))

        @callback
        def _core_config_updated(_event: Event) -> None:
            self._apply_time_zone()

        self._unsubs.append(
            self.hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, _core_config_updated)
        )
        self._program_debouncer = Debouncer(
            self.hass,
            _LOGGER,
            cooldown=PROGRAM_DEBOUNCE_S,
            immediate=False,
            function=self.async_apply_program,
        )
        self._watch_program()

        @callback
        def _started(_event: Event | None = None) -> None:
            self._unsubs.append(
                async_call_later(
                    self.hass, PROGRAM_STARTUP_DELAY_S, self._startup_apply
                )
            )

        if self.hass.is_running:
            _started()
        else:
            self._unsubs.append(
                self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _started)
            )

    async def async_unload(self) -> None:
        """Unhook everything and flush the store."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._program_unsub is not None:
            self._program_unsub()
            self._program_unsub = None
        if self._program_debouncer is not None:
            self._program_debouncer.async_shutdown()
        for _matcher, future in self._waiters.values():
            future.cancel()
        self._waiters.clear()
        await self._store.async_save(self._data)

    @callback
    def _apply_time_zone(self) -> None:
        """Make the valve's clock and plan times follow HA's configured zone."""
        tz = dt_util.get_time_zone(self.hass.config.time_zone)
        quirk.set_local_timezone(tz)

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(lambda: self._data, 1)

    # ── registry / zigpy resolution ──
    def _ieee(self, device_id: str) -> str:
        device = dr.async_get(self.hass).async_get(device_id)
        if device is not None:
            for domain, ident in device.identifiers:
                if domain == "zha":
                    return str(ident).lower()
        raise HomeAssistantError(f"Device {device_id} is not a ZHA device")

    def _device_id_for_ieee(self, ieee: str) -> str | None:
        dev_reg = dr.async_get(self.hass)
        identifiers = {("zha", ieee)}
        if hasattr(dev_reg, "async_get_devices"):
            # HA 2026.8+: identifiers are unique per config entry only, and
            # async_get_device(identifiers=...) is deprecated.
            devices = dev_reg.async_get_devices(identifiers=identifiers)
        else:
            device = dev_reg.async_get_device(identifiers=identifiers)
            devices = [device] if device else []
        for device in devices:
            if device.model in SWV_MODELS:
                return device.id
        return None

    def _cluster(self, device_id: str, endpoint_id: int) -> Any:
        """Return the quirk's 0xFC11 cluster on one endpoint (ZHA's zigpy device)."""
        try:
            from homeassistant.components.zha.helpers import get_zha_gateway
            from zigpy.types import EUI64

            gateway = get_zha_gateway(self.hass)
            zha_device = gateway.devices.get(EUI64.convert(self._ieee(device_id)))
        except HomeAssistantError:
            raise
        except Exception as err:  # noqa: BLE001 - ZHA internals
            raise HomeAssistantError(f"ZHA is not ready: {err}") from err
        zigpy_device = getattr(zha_device, "device", None)
        endpoint = getattr(zigpy_device, "endpoints", {}).get(endpoint_id)
        cluster = getattr(endpoint, "swvzf2_cluster", None)
        if cluster is None or not hasattr(cluster, "set_irrigation_plan"):
            raise HomeAssistantError(
                f"Valve {device_id} endpoint {endpoint_id} has no schedule-capable "
                "quirk cluster; restart HA so the patched quirk loads, then "
                "Reconfigure the valve"
            )
        return cluster

    def _lock(self, device_id: str) -> asyncio.Lock:
        return self._locks.setdefault(device_id, asyncio.Lock())

    # ── desired-state accessors (sensors read these) ──
    def _line(self, device_id: str, channel: str) -> dict[str, Any]:
        device = self._data["devices"].setdefault(device_id, {"lines": {}})
        return device["lines"].setdefault(channel, {"slots": {}})

    def slots(self, device_id: str, channel: str) -> dict[int, dict[str, Any]]:
        """Slot index -> {plan (dict|None), status, error, updated}."""
        line = self._data["devices"].get(device_id, {}).get("lines", {}).get(channel)
        if not line:
            return {}
        return {int(k): v for k, v in line["slots"].items()}

    def desired_plans(self, device_id: str, channel: str) -> list[IrrigationPlan]:
        """Return the plans HA wants on a line, by slot."""
        return [
            plan_from_dict(slot["plan"])
            for _index, slot in sorted(self.slots(device_id, channel).items())
            if slot.get("plan")
        ]

    def rain_delay_until(self, device_id: str) -> datetime | None:
        """End of the rain delay HA last set on the valve, if still ahead."""
        value = self._data["devices"].get(device_id, {}).get("rain_delay_until")
        until = dt_util.parse_datetime(value) if value else None
        return until if until and until > dt_util.utcnow() else None

    def last_time_sync(self, device_id: str) -> dict[str, Any] | None:
        """When the valve last asked for the time, and what it was told."""
        return self._data["devices"].get(device_id, {}).get("time_sync")

    @callback
    def _set_slot(
        self,
        device_id: str,
        channel: str,
        index: int,
        *,
        plan: IrrigationPlan | None,
        status: str,
        error: str | None = None,
        reported: IrrigationPlan | None = None,
    ) -> None:
        slots = self._line(device_id, channel)["slots"]
        entry: dict[str, Any] = {
            "plan": plan_to_dict(plan) if plan else None,
            "status": status,
            "error": error,
            "updated": dt_util.utcnow().isoformat(),
        }
        if reported is not None:
            entry["reported"] = plan_to_dict(reported)
        slots[str(index)] = entry
        self._save()
        async_dispatcher_send(self.hass, schedule_signal(device_id))

    @callback
    def _drop_slot(self, device_id: str, channel: str, index: int) -> None:
        self._line(device_id, channel)["slots"].pop(str(index), None)
        self._save()
        async_dispatcher_send(self.hass, schedule_signal(device_id))

    # ── writes ──
    async def _wait_for(
        self, ieee: str, endpoint_id: int, matcher: Callable[[dict], bool]
    ) -> dict | None:
        future: asyncio.Future = self.hass.loop.create_future()
        self._waiters[(ieee, endpoint_id)] = (matcher, future)
        try:
            async with asyncio.timeout(ACK_TIMEOUT_S):
                return await future
        except TimeoutError:
            return None
        finally:
            self._waiters.pop((ieee, endpoint_id), None)

    async def async_set_plan(
        self, device_id: str, channel: str, plan: IrrigationPlan
    ) -> str:
        """Store ``plan`` as desired and write it; returns the final status."""
        async with self._lock(device_id):
            return await self._write_plan(device_id, channel, plan)

    async def _write_plan(
        self, device_id: str, channel: str, plan: IrrigationPlan
    ) -> str:
        endpoint_id = int(channel)
        self._set_slot(device_id, channel, plan.index, plan=plan, status=STATUS_PENDING)
        ieee = self._ieee(device_id)

        def _answers(event: dict) -> bool:
            return event["type"] == "plan_ack" or (
                event["type"] == "plan_report" and event["plan"].index == plan.index
            )

        waiter = asyncio.ensure_future(self._wait_for(ieee, endpoint_id, _answers))
        await asyncio.sleep(0)  # register the waiter before anything can answer
        try:
            status_code = await self._cluster(
                device_id, endpoint_id
            ).set_irrigation_plan(plan)
        except Exception as err:  # noqa: BLE001 - surfaced as the slot status
            waiter.cancel()
            self._set_slot(
                device_id,
                channel,
                plan.index,
                plan=plan,
                status=STATUS_FAILED,
                error=f"write failed: {err}",
            )
            return STATUS_FAILED

        if status_code is None:
            event = await waiter
        else:
            waiter.cancel()
            event = {"type": "plan_ack", "status": status_code}

        if event is None:
            status, error, reported = (
                STATUS_FAILED,
                "no confirmation from the valve",
                None,
            )
        elif event["type"] == "plan_ack":
            ok = event.get("status") in (0, None)
            status = STATUS_ON_VALVE if ok else STATUS_FAILED
            error = (
                None if ok else f"valve rejected the plan (status {event['status']})"
            )
            reported = None
        elif event["plan"].same_settings(plan):
            status, error, reported = STATUS_ON_VALVE, None, None
        else:
            status, error = STATUS_FAILED, "valve reports different settings"
            reported = event["plan"]
        self._set_slot(
            device_id,
            channel,
            plan.index,
            plan=plan,
            status=status,
            error=error,
            reported=reported,
        )
        return status

    async def async_remove_plan(self, device_id: str, channel: str, index: int) -> str:
        """Forget the slot's plan and remove it from the valve."""
        async with self._lock(device_id):
            return await self._remove_plan(device_id, channel, index)

    async def _remove_plan(self, device_id: str, channel: str, index: int) -> str:
        endpoint_id = int(channel)
        self._set_slot(device_id, channel, index, plan=None, status=STATUS_PENDING)
        ieee = self._ieee(device_id)
        waiter = asyncio.ensure_future(
            self._wait_for(
                ieee,
                endpoint_id,
                lambda e: e["type"] == "plan_removed" and e["index"] == index,
            )
        )
        await asyncio.sleep(0)
        try:
            await self._cluster(device_id, endpoint_id).remove_irrigation_plan(index)
        except Exception as err:  # noqa: BLE001 - surfaced as the slot status
            waiter.cancel()
            self._set_slot(
                device_id,
                channel,
                index,
                plan=None,
                status=STATUS_FAILED,
                error=f"remove failed: {err}",
            )
            return STATUS_FAILED
        if await waiter is None:
            # The 0x0A report is the only confirmation a remove gets. Keep the
            # slot as a failed "nothing" so Resync tries again.
            self._set_slot(
                device_id,
                channel,
                index,
                plan=None,
                status=STATUS_FAILED,
                error="removal not confirmed by the valve",
            )
            return STATUS_FAILED
        self._drop_slot(device_id, channel, index)
        return STATUS_ON_VALVE

    async def async_resync(self, device_id: str) -> dict[str, str]:
        """Rewrite every slot of both lines so the valve matches HA."""
        results: dict[str, str] = {}
        async with self._lock(device_id):
            for channel in CHANNELS:
                slots = self.slots(device_id, channel)
                for index in range(quirk.PLAN_SLOTS):
                    slot = slots.get(index)
                    if slot and slot.get("plan"):
                        status = await self._write_plan(
                            device_id, channel, plan_from_dict(slot["plan"])
                        )
                    elif slot:
                        # A removal HA is still tracking: wait for its 0x0A.
                        status = await self._remove_plan(device_id, channel, index)
                    else:
                        # Slot HA never used: clear it, but do not wait half a
                        # minute for a report an empty slot may never send.
                        try:
                            await self._cluster(
                                device_id, int(channel)
                            ).remove_irrigation_plan(index)
                            status = STATUS_ON_VALVE
                        except Exception as err:  # noqa: BLE001
                            _LOGGER.warning(
                                "Resync: clearing %s/%s failed: %s", channel, index, err
                            )
                            status = STATUS_FAILED
                    results[f"{channel}/{index}"] = status
        return results

    async def async_rain_delay(self, device_id: str, hours: float) -> None:
        """Suspend the valve's plans for ``hours`` (0 cancels). Valve-wide."""
        until = dt_util.utcnow() + timedelta(hours=hours) if hours > 0 else None
        async with self._lock(device_id):
            await self._cluster(device_id, 1).set_rain_delay(until)
        device = self._data["devices"].setdefault(device_id, {"lines": {}})
        device["rain_delay_until"] = until.isoformat() if until else None
        self._save()
        async_dispatcher_send(self.hass, schedule_signal(device_id))

    # ── valve events ──
    @callback
    def _on_quirk_event(self, event: dict[str, Any]) -> None:
        """Every plan / clock / session event from any SWV valve."""
        key = (event["ieee"], event["endpoint_id"])
        waiter = self._waiters.get(key)
        if waiter is not None:
            matcher, future = waiter
            if not future.done() and matcher(event):
                future.set_result(event)
                return
        device_id = self._device_id_for_ieee(event["ieee"])
        if device_id is None:
            return
        channel = str(event["endpoint_id"])
        kind = event["type"]
        if kind == "plan_report":
            self._reconcile_report(device_id, channel, event["plan"])
        elif kind == "plan_removed":
            slot = self.slots(device_id, channel).get(event["index"])
            if slot and slot.get("plan"):
                self._set_slot(
                    device_id,
                    channel,
                    event["index"],
                    plan=plan_from_dict(slot["plan"]),
                    status=STATUS_FAILED,
                    error="removed on the valve",
                )
        elif kind == "time_sync":
            device = self._data["devices"].setdefault(device_id, {"lines": {}})
            previous = device.get("time_sync") or {}
            device["time_sync"] = {
                "at": event["at"].isoformat(),
                "count": int(previous.get("count", 0)) + 1,
                "local_time": event["values"]["local_time"],
                "time_zone": event["values"]["time_zone"],
                "dst_shift": event["values"]["dst_shift"],
            }
            self._save()
            async_dispatcher_send(self.hass, schedule_signal(device_id))
        elif kind == "session" and event.get("session_scheduled"):
            index = event["session_plan_index"]
            slot = self.slots(device_id, channel).get(index) or {}
            mode, target = (
                plan_target(plan_from_dict(slot["plan"]))
                if slot.get("plan")
                else (None, None)
            )
            self._run_log.tag_session(device_id, channel, index, mode, target)

    @callback
    def _reconcile_report(
        self, device_id: str, channel: str, reported: IrrigationPlan
    ) -> None:
        """Confirm or flag the slot an unsolicited 0x09 report describes."""
        slot = self.slots(device_id, channel).get(reported.index) or {}
        desired = plan_from_dict(slot["plan"]) if slot.get("plan") else None
        if desired is not None and desired.same_settings(reported):
            if slot.get("status") != STATUS_ON_VALVE:
                self._set_slot(
                    device_id,
                    channel,
                    reported.index,
                    plan=desired,
                    status=STATUS_ON_VALVE,
                )
            return
        self._set_slot(
            device_id,
            channel,
            reported.index,
            plan=desired,
            status=STATUS_FAILED,
            error=(
                "valve has different settings (changed elsewhere?)"
                if desired
                else "valve has a plan HA does not know"
            ),
            reported=reported,
        )

    # ── program ──
    def program(self) -> dict[str, Any] | None:
        """Return the stored program config, if any."""
        return self._data.get("program")

    async def async_set_program(self, config: dict[str, Any] | None) -> None:
        """Store (or clear) the program, re-watch its entities, apply it."""
        self._data["program"] = config
        self._save()
        self._watch_program()
        if config:
            await self.async_apply_program()

    @callback
    def _watch_program(self) -> None:
        if self._program_unsub is not None:
            self._program_unsub()
            self._program_unsub = None
        config = self._data.get("program")
        if not config:
            return
        entities = [config.get("start_entity")]
        for zone in config["zones"]:
            entities += [zone.get("minutes_entity"), zone.get("enabled_entity")]
        entities = sorted({e for e in entities if e})
        if not entities or self._program_debouncer is None:
            return

        @callback
        def _changed(_event: Event) -> None:
            self._program_debouncer.async_schedule_call()

        self._program_unsub = async_track_state_change_event(
            self.hass, entities, _changed
        )

    async def _startup_apply(self, _now: datetime | None = None) -> None:
        if self._data.get("program"):
            await self.async_apply_program()

    def _resolve(self, literal: Any, entity_id: str | None, kind: str) -> Any:
        if not entity_id:
            return literal
        state = self.hass.states.get(entity_id)
        if state is None or state.state in _BAD_STATES:
            raise HomeAssistantError(
                f"{entity_id} is {state.state if state else 'missing'}"
            )
        if kind == "minutes":
            return int(round(float(state.state)))
        if kind == "enabled":
            return state.state == "on"
        return state.state

    async def async_apply_program(self) -> None:
        """Write the zones whose program plan changed (no-op when none did)."""
        config = self._data.get("program")
        if not config:
            return
        try:
            start_seconds = parse_start_time(
                self._resolve(
                    config.get("start_time"), config.get("start_entity"), "start"
                )
            )
            zones = [
                ProgramZone(
                    key=zone["switch"],
                    minutes=self._resolve(
                        zone.get("minutes"), zone.get("minutes_entity"), "minutes"
                    ),
                    enabled=self._resolve(
                        zone.get("enabled", True), zone.get("enabled_entity"), "enabled"
                    ),
                )
                for zone in config["zones"]
            ]
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning("Irrigation program not applied: %s", err)
            return

        lines = {info["switch"]: info for info in find_swv_switches(self.hass)}
        slot = int(config.get("slot", 0))
        current: dict[str, IrrigationPlan | None] = {}
        for zone in zones:
            info = lines.get(zone.key)
            if info is None:
                _LOGGER.warning("Irrigation program: %s is not an SWV line", zone.key)
                return
            entry = self.slots(info["device_id"], info["channel"]).get(slot) or {}
            current[zone.key] = (
                plan_from_dict(entry["plan"]) if entry.get("plan") else None
            )

        now = dt_util.now()
        try:
            changed = program_plans(
                start_seconds=start_seconds,
                zones=zones,
                gap_minutes=int(config.get("gap_minutes", 1)),
                slot=slot,
                current=current,
                today=now.date(),
                created=int(now.timestamp()),
            )
        except ValueError as err:
            _LOGGER.warning("Irrigation program not applied: %s", err)
            return
        # Zone order: the program lists the valves in watering order, and a
        # bad write on a later valve should not hold up an earlier one.
        for zone in zones:
            plan = changed.get(zone.key)
            if plan is None:
                continue
            info = lines[zone.key]
            status = await self.async_set_plan(info["device_id"], info["channel"], plan)
            _LOGGER.info(
                "Irrigation program: %s plan %s %s at %s for %s min -> %s",
                zone.key,
                plan.index,
                "enabled" if plan.enabled else "disabled",
                plan.start_seconds,
                plan.total_duration,
                status,
            )


# ── services ──

_PROGRAM_ZONE = vol.Schema(
    {
        vol.Required("switch"): cv.entity_id,
        vol.Exclusive("minutes", "minutes"): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=719)
        ),
        vol.Exclusive("minutes_entity", "minutes"): cv.entity_id,
        vol.Exclusive("enabled", "enabled"): cv.boolean,
        vol.Exclusive("enabled_entity", "enabled"): cv.entity_id,
    }
)


def _zone_has_minutes(zone: dict) -> dict:
    if "minutes" not in zone and "minutes_entity" not in zone:
        raise vol.Invalid(f"{zone['switch']}: give minutes or minutes_entity")
    return zone


SET_PLAN_SCHEMA = vol.All(
    vol.Schema(
        {
            **cv.ENTITY_SERVICE_FIELDS,
            vol.Required("channel"): _channel,
            vol.Required("index"): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=quirk.PLAN_SLOTS - 1)
            ),
            vol.Optional("enabled", default=True): cv.boolean,
            vol.Optional("repeat", default=REPEAT_DAILY): vol.In(REPEATS),
            vol.Optional("weekdays"): vol.All(cv.ensure_list, [cv.string]),
            vol.Optional("interval_days"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=30)
            ),
            vol.Required("start_time"): cv.string,
            vol.Exclusive("minutes", "amount"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=719)
            ),
            vol.Exclusive("liters", "amount"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=10000)
            ),
            vol.Optional("fail_safe_minutes"): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=719)
            ),
        }
    ),
    cv.has_at_least_one_key(*cv.ENTITY_SERVICE_FIELDS),
    cv.has_at_least_one_key("minutes", "liters"),
)

REMOVE_PLAN_SCHEMA = vol.All(
    vol.Schema(
        {
            **cv.ENTITY_SERVICE_FIELDS,
            vol.Required("channel"): _channel,
            vol.Required("index"): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=quirk.PLAN_SLOTS - 1)
            ),
        }
    ),
    cv.has_at_least_one_key(*cv.ENTITY_SERVICE_FIELDS),
)

RAIN_DELAY_SCHEMA = vol.All(
    vol.Schema(
        {
            **cv.ENTITY_SERVICE_FIELDS,
            vol.Required("hours"): vol.All(
                vol.Coerce(float), vol.Range(min=0, max=168)
            ),
        }
    ),
    cv.has_at_least_one_key(*cv.ENTITY_SERVICE_FIELDS),
)

RESYNC_SCHEMA = vol.All(
    vol.Schema(cv.ENTITY_SERVICE_FIELDS),
    cv.has_at_least_one_key(*cv.ENTITY_SERVICE_FIELDS),
)

SET_PROGRAM_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Exclusive("start_time", "start"): cv.string,
            vol.Exclusive("start_entity", "start"): cv.entity_id,
            vol.Optional("gap_minutes", default=1): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=60)
            ),
            vol.Optional("slot", default=0): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=quirk.PLAN_SLOTS - 1)
            ),
            vol.Required("zones"): vol.All(
                cv.ensure_list,
                [vol.All(_PROGRAM_ZONE, _zone_has_minutes)],
                vol.Length(min=1),
            ),
        }
    ),
    cv.has_at_least_one_key("start_time", "start_entity"),
)


def async_setup_schedule_services(
    hass: HomeAssistant, manager: SwvScheduleManager
) -> None:
    """Register the plan, rain-delay, resync and program services."""

    async def _set_plan(call: ServiceCall) -> None:
        device_id = _device_from_call(hass, call)
        channel = call.data["channel"]
        index = call.data["index"]
        previous = manager.slots(device_id, channel).get(index) or {}
        now = dt_util.now()
        try:
            plan = build_plan(
                index=index,
                enabled=call.data["enabled"],
                repeat=call.data["repeat"],
                weekdays=call.data.get("weekdays"),
                interval_days=call.data.get("interval_days"),
                start_time=call.data["start_time"],
                minutes=call.data.get("minutes"),
                liters=call.data.get("liters"),
                fail_safe_minutes=call.data.get("fail_safe_minutes"),
                enable_date=(
                    plan_from_dict(previous["plan"]).enable_date
                    if previous.get("plan")
                    else now.date()
                ),
                created=int(now.timestamp()),
            )
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        status = await manager.async_set_plan(device_id, channel, plan)
        if status != STATUS_ON_VALVE:
            raise HomeAssistantError(
                f"Plan {index} on line {channel} was stored but not confirmed by "
                f"the valve ({status}); see the schedule sensor, or run resync_plans"
            )

    async def _remove_plan(call: ServiceCall) -> None:
        status = await manager.async_remove_plan(
            _device_from_call(hass, call), call.data["channel"], call.data["index"]
        )
        if status != STATUS_ON_VALVE:
            raise HomeAssistantError(
                "The valve did not confirm the removal; run resync_plans to retry"
            )

    async def _rain_delay(call: ServiceCall) -> None:
        await manager.async_rain_delay(
            _device_from_call(hass, call), call.data["hours"]
        )

    async def _resync(call: ServiceCall) -> None:
        results = await manager.async_resync(_device_from_call(hass, call))
        failed = sorted(k for k, v in results.items() if v != STATUS_ON_VALVE)
        if failed:
            raise HomeAssistantError(
                f"Resync finished; not confirmed by the valve: {', '.join(failed)}"
            )

    async def _set_program(call: ServiceCall) -> None:
        lines = {info["switch"] for info in find_swv_switches(hass)}
        unknown = [z["switch"] for z in call.data["zones"] if z["switch"] not in lines]
        if unknown:
            raise ServiceValidationError(
                f"Not SONOFF SWV-ZF2 line switches: {', '.join(unknown)}"
            )
        config = {
            key: call.data[key]
            for key in ("start_time", "start_entity", "gap_minutes", "slot", "zones")
            if key in call.data
        }
        await manager.async_set_program(config)

    async def _clear_program(call: ServiceCall) -> None:
        await manager.async_set_program(None)

    handlers = {
        SERVICE_SET_PLAN: (_set_plan, SET_PLAN_SCHEMA),
        SERVICE_REMOVE_PLAN: (_remove_plan, REMOVE_PLAN_SCHEMA),
        SERVICE_RAIN_DELAY: (_rain_delay, RAIN_DELAY_SCHEMA),
        SERVICE_RESYNC: (_resync, RESYNC_SCHEMA),
        SERVICE_SET_PROGRAM: (_set_program, SET_PROGRAM_SCHEMA),
        SERVICE_CLEAR_PROGRAM: (_clear_program, vol.Schema({})),
    }
    for service, (handler, schema) in handlers.items():
        hass.services.async_register(DOMAIN, service, handler, schema=schema)


def async_unload_schedule_services(hass: HomeAssistant) -> None:
    """Remove the schedule services."""
    for service in SCHEDULE_SERVICES:
        hass.services.async_remove(DOMAIN, service)
