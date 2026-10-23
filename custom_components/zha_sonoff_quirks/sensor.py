"""Irrigation history sensors, one pair per SWV channel switch.

Two entities per channel, both attached to the valve's ZHA device through
:class:`~.entity.SwvAttachedEntity` — see that module for why the association
goes through the entity registry and not ``DeviceInfo``. A cleanup pass here
removes any empty shadow devices a previous version created.

The entities:

* ``Irrigation history CH<n>`` — state is the timestamp of the last completed
  run; its ``runs`` attribute carries the recent run list the card reads. The
  (potentially large) ``runs`` attribute is excluded from the recorder so it
  never bloats the database.
* ``Irrigation water total CH<n>`` — cumulative liters
  (``total_increasing`` / ``water``) feeding HA's native water dashboard and
  long-term statistics.

Both pull their values from the :class:`SwvRunLog` manager and refresh on its
``history_signal`` dispatch. unique_ids embed the channel number FIRST so the
card can resolve them with a prefix match (``zha_sonoff_quirks_history_ch1``)
regardless of how the switch entity_id is spelled.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .const import DOMAIN, RUNS_ATTR_CAP, history_signal
from .entity import SwvAttachedEntity
from .helpers import all_devices, find_swv_switches
from .history import SwvRunLog
from .schedule_logic import describe_plan, next_run, plan_from_dict
from .schedules import SwvScheduleManager, schedule_signal


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the history + water-total sensors for each channel switch."""
    dev_reg = dr.async_get(hass)
    added_switches: set[str] = set()

    @callback
    def _add_new_channels() -> None:
        # .get(): during entry unload the registry listener below stays
        # subscribed until after async_unload_entry pops "run_log" — a
        # registry event from any integration in that window must not raise.
        run_log: SwvRunLog | None = hass.data.get(DOMAIN, {}).get("run_log")
        if run_log is None:
            return
        new_entities: list[SensorEntity] = []
        for info in find_swv_switches(hass):
            switch = info["switch"]
            if switch in added_switches:
                continue
            device = dev_reg.async_get(info["device_id"])
            if device is None:
                continue
            added_switches.add(switch)
            new_entities.append(
                SwvIrrigationHistorySensor(device, switch, info["channel"], run_log)
            )
            new_entities.append(
                SwvWaterTotalSensor(device, switch, info["channel"], run_log)
            )
            schedules: SwvScheduleManager | None = hass.data.get(DOMAIN, {}).get(
                "schedules"
            )
            if schedules is not None:
                new_entities.append(
                    SwvScheduleSensor(device, switch, info["channel"], schedules)
                )
        if new_entities:
            async_add_entities(new_entities)

    _add_new_channels()

    async def _cleanup_shadow_devices() -> None:
        """Remove empty duplicate devices left by the 0.5.0 DeviceInfo claim.

        Deferred a few seconds so the entities added above have re-hooked
        themselves onto the real ZHA device first, leaving the shadow devices
        entity-less and safe to delete.
        """
        await asyncio.sleep(10)
        ent_reg = er.async_get(hass)
        for device in all_devices(dev_reg):
            # HA 2026.8+: one owning entry per device (config_entry_id);
            # the config_entries set is deprecated (an error from 2027.10).
            owner = getattr(device, "config_entry_id", None)
            if owner is not None:
                if owner != entry.entry_id:
                    continue
            elif entry.entry_id not in device.config_entries or (
                len(device.config_entries) > 1
            ):
                continue
            if er.async_entries_for_device(
                ent_reg, device.id, include_disabled_entities=True
            ):
                continue
            dev_reg.async_remove_device(device.id)

    entry.async_create_task(hass, _cleanup_shadow_devices())

    @callback
    def _registry_updated(event: Event) -> None:
        # A valve paired (or re-quirked via ZHA reload) after setup registers
        # its switches here; pick them up without requiring a restart. Cheap
        # pre-filter: the event fires for every entity of every integration,
        # and only switch entities can introduce a new channel.
        data = event.data
        if data.get("action") not in ("create", "update"):
            return
        if not str(data.get("entity_id", "")).startswith("switch."):
            return
        _add_new_channels()

    entry.async_on_unload(
        hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _registry_updated)
    )


class _SwvHistoryEntity(SwvAttachedEntity, SensorEntity):
    """Shared run-log subscription for the history sensors."""

    def __init__(
        self,
        device: dr.DeviceEntry,
        switch_entity: str,
        channel: str,
        run_log: SwvRunLog,
    ) -> None:
        """Remember the switch/channel pair and the run log behind it."""
        super().__init__(device, switch_entity, channel)
        self._run_log = run_log

    async def async_added_to_hass(self) -> None:
        """Attach to the ZHA device and subscribe to the refresh signal."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                history_signal(self._switch_entity),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class SwvIrrigationHistorySensor(_SwvHistoryEntity):
    """Timestamp of the last completed run; carries the recent-run list."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    # Keep the (potentially large) run list out of the recorder DB.
    _unrecorded_attributes = frozenset({"runs"})

    def __init__(
        self,
        device: dr.DeviceEntry,
        switch_entity: str,
        channel: str,
        run_log: SwvRunLog,
    ) -> None:
        """Set the channel-prefixed unique_id and display name."""
        super().__init__(device, switch_entity, channel, run_log)
        # Channel-first unique_id: the card prefix-matches on
        # f"{DOMAIN}_history_ch{n}" to find this entity.
        self._attr_unique_id = f"{DOMAIN}_history_ch{channel}_{switch_entity}"
        self._attr_name = f"Irrigation history CH{channel}"

    @property
    def native_value(self) -> datetime | None:
        """End time of the most recent recorded run."""
        last = self._run_log.last_run(self._switch_entity)
        if not last or not last.get("end"):
            return None
        return dt_util.parse_datetime(last["end"])

    @property
    def extra_state_attributes(self) -> dict:
        """Recent runs (capped) plus the total recorded count."""
        runs = self._run_log.runs_for(self._switch_entity)
        return {"runs": runs[:RUNS_ATTR_CAP], "run_count": len(runs)}


class SwvWaterTotalSensor(_SwvHistoryEntity):
    """Cumulative liters delivered — feeds HA's water dashboard + LTS."""

    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS

    def __init__(
        self,
        device: dr.DeviceEntry,
        switch_entity: str,
        channel: str,
        run_log: SwvRunLog,
    ) -> None:
        """Set the channel-prefixed unique_id and display name."""
        super().__init__(device, switch_entity, channel, run_log)
        self._attr_unique_id = f"{DOMAIN}_water_total_ch{channel}_{switch_entity}"
        self._attr_name = f"Irrigation water total CH{channel}"

    @property
    def native_value(self) -> float:
        """Cumulative recorded liters for this channel."""
        return round(self._run_log.water_total(self._switch_entity), 2)


class SwvScheduleSensor(SwvAttachedEntity, SensorEntity):
    """Next on-valve plan start for a line; the plan list rides along.

    State: when the valve should next open this line by itself, computed from
    HA's desired plans (there is no way to read the plans back from the
    valve). Attributes: every slot with its status (pending / on_valve /
    failed), the rain delay HA last set, and when the valve last asked for
    the time — the clock the plans fire on.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self,
        device: dr.DeviceEntry,
        switch_entity: str,
        channel: str,
        schedules: SwvScheduleManager,
    ) -> None:
        """Set the channel-prefixed unique_id and display name."""
        super().__init__(device, switch_entity, channel)
        self._schedules = schedules
        self._device_id = device.id
        self._attr_unique_id = f"{DOMAIN}_schedule_ch{channel}_{switch_entity}"
        self._attr_name = f"Next scheduled run CH{channel}"
        self._unsub_timer = None

    async def async_added_to_hass(self) -> None:
        """Attach to the ZHA device and follow the device's plan changes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, schedule_signal(self._device_id), self._handle_update
            )
        )
        self.async_on_remove(self._cancel_timer)
        self._handle_update()

    @callback
    def _cancel_timer(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    @callback
    def _handle_update(self, _now: datetime | None = None) -> None:
        self._cancel_timer()
        nxt = self._next()
        if nxt is not None:
            # Recompute just after the start passes, to roll to the next one.
            self._unsub_timer = async_track_point_in_utc_time(
                self.hass, self._handle_update, nxt[0] + timedelta(seconds=1)
            )
        self.async_write_ha_state()

    def _next(self):
        return next_run(
            self._schedules.desired_plans(self._device_id, self._channel),
            dt_util.now(),
            dt_util.get_default_time_zone(),
            self._schedules.rain_delay_until(self._device_id),
        )

    @property
    def native_value(self) -> datetime | None:
        """Start of the next enabled plan on this line."""
        nxt = self._next()
        return nxt[0] if nxt else None

    @property
    def extra_state_attributes(self) -> dict:
        """Slots with status, next plan index, rain delay, last clock sync."""
        plans = []
        for index, slot in sorted(
            self._schedules.slots(self._device_id, self._channel).items()
        ):
            entry = (
                describe_plan(plan_from_dict(slot["plan"]))
                if slot.get("plan")
                else {"index": index, "removing": True}
            )
            entry["status"] = slot.get("status")
            if slot.get("error"):
                entry["error"] = slot["error"]
            plans.append(entry)
        nxt = self._next()
        rain = self._schedules.rain_delay_until(self._device_id)
        sync = self._schedules.last_time_sync(self._device_id) or {}
        return {
            "plans": plans,
            "next_plan_index": nxt[1].index if nxt else None,
            "rain_delay_until": rain.isoformat() if rain else None,
            "last_time_sync": sync.get("at"),
            "time_sync_count": sync.get("count", 0),
            "valve_next_session": self._schedules.valve_next_session(
                self._device_id, self._channel
            ),
        }
