"""Home Assistant-level tests for the on-valve schedules (needs HA installed).

Kept apart from tests/, which deliberately runs without Home Assistant. Run
with a venv that has pytest-homeassistant-custom-component pinned to the HA
release in use (0.13.373 = HA 2026.10.2, Python 3.14):

    pytest tests_ha -o asyncio_mode=auto

ZHA itself is not started (it needs a radio): the valve is simulated at the
quirk boundary — the schedule manager's ``_cluster`` returns a fake cluster
and valve answers are fed through the quirk's own listener hook, exactly as
the patched quirk delivers them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import sys
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zha_sonoff_quirks.quirks import sonoff_swv_zf2 as quirk

pytest_plugins = "pytest_homeassistant_custom_component"

FRONT_IEEE = "a4:c1:38:41:30:9d:ea:53"
BACK_IEEE = "a4:c1:38:00:00:00:00:02"


if sys.platform == "win32":
    # asyncio's Windows loop builds its self-pipe from a 127.0.0.1 socketpair,
    # which phcc's disable_socket() blocks before any fixture can intervene.
    # HA is not supported on Windows and CI runs these on Linux; this only
    # lets a developer run them locally (with fcntl/resource stubs in the
    # venv). Connections stay limited to 127.0.0.1 by socket_allow_hosts.
    import pytest_socket

    pytest_socket.disable_socket = lambda *args, **kwargs: None


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let HA load custom_components/ from the repo."""
    return


@dataclass
class FakeEndpoint:
    """Stands in for the quirk's SWVZF2Cluster on one endpoint."""

    valve: FakeValve
    endpoint_id: int

    async def set_irrigation_plan(self, plan: quirk.IrrigationPlan) -> int | None:
        self.valve.sent.append(("set", self.endpoint_id, plan))
        if self.valve.report_after_set:
            self.valve.emit(self.endpoint_id, "plan_report", plan=plan)
        return self.valve.set_status

    async def remove_irrigation_plan(self, index: int) -> None:
        self.valve.sent.append(("remove", self.endpoint_id, index))
        if self.valve.report_removed:
            self.valve.emit(self.endpoint_id, "plan_removed", index=index)

    async def set_rain_delay(self, end) -> None:
        self.valve.sent.append(("rain_delay", self.endpoint_id, end))


@dataclass
class FakeValve:
    """One simulated SWV-ZF2: what was sent, and how it answers."""

    ieee: str
    device_id: str
    switches: dict[str, str]
    set_status: int | None = 0
    report_after_set: bool = False
    report_removed: bool = True
    manual: dict[str, str] = field(default_factory=dict)
    sent: list[tuple[str, int, Any]] = field(default_factory=list)

    def endpoint(self, endpoint_id: int) -> FakeEndpoint:
        return FakeEndpoint(self, endpoint_id)

    def emit(self, endpoint_id: int, event_type: str, **fields: Any) -> None:
        """Deliver an event through the quirk's listener hook."""
        event = {"type": event_type, "ieee": self.ieee, "endpoint_id": endpoint_id}
        event.update(fields)
        for listener in list(quirk._schedule_listeners):
            listener(event)


def _add_valve(
    hass: HomeAssistant, entry: MockConfigEntry, ieee: str, name: str
) -> FakeValve:
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("zha", ieee)},
        manufacturer="SONOFF",
        model="SWV-ZF2",
        name=name,
    )
    switches = {}
    for channel, line in (("1", "lawn"), ("2", "boxes")):
        reg = ent_reg.async_get_or_create(
            "switch",
            "zha",
            f"{ieee}-{channel}",
            device_id=device.id,
            suggested_object_id=f"{name}_{line}",
        )
        hass.states.async_set(reg.entity_id, "off")
        switches[channel] = reg.entity_id
    # Simone's manual-default entities (0x501D), as the quirk creates them.
    manual = {}
    for domain, suffix, state in (
        ("select", "irrigation_mode", "duration"),
        ("number", "irrigation_duration", "10"),
    ):
        reg = ent_reg.async_get_or_create(
            domain,
            "zha",
            f"{ieee}-1-{suffix}",
            device_id=device.id,
            suggested_object_id=f"{name}_{suffix}",
        )
        hass.states.async_set(reg.entity_id, state)
        manual[suffix] = reg.entity_id
    return FakeValve(ieee=ieee, device_id=device.id, switches=switches, manual=manual)


@pytest.fixture
def manual_writes(hass: HomeAssistant) -> list[tuple[str, Any]]:
    """Stand-in select/number services: record writes, update the state."""
    writes: list[tuple[str, Any]] = []

    async def _write(call):
        value = call.data.get("option", call.data.get("value"))
        entity_ids = call.data["entity_id"]
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        for entity_id in entity_ids:
            writes.append((entity_id, value))
            hass.states.async_set(entity_id, str(value))

    hass.services.async_register("select", "select_option", _write)
    hass.services.async_register("number", "set_value", _write)
    return writes


@pytest.fixture
async def valves(hass: HomeAssistant) -> dict[str, FakeValve]:
    """Front and back valves registered as ZHA devices with two line switches."""
    entry = MockConfigEntry(domain="zha")
    entry.add_to_hass(hass)
    return {
        "front": _add_valve(hass, entry, FRONT_IEEE, "front"),
        "back": _add_valve(hass, entry, BACK_IEEE, "back"),
    }


@pytest.fixture
async def manager(hass: HomeAssistant, valves, monkeypatch):
    """Run log + schedule manager + services, with the fake valves wired in."""
    from custom_components.zha_sonoff_quirks import schedules
    from custom_components.zha_sonoff_quirks.history import SwvRunLog
    from custom_components.zha_sonoff_quirks.services import async_setup_services

    await hass.config.async_update(time_zone="America/Los_Angeles")
    monkeypatch.setattr(schedules, "ACK_TIMEOUT_S", 0.2)
    by_device = {v.device_id: v for v in valves.values()}
    async_setup_services(hass)
    run_log = SwvRunLog(hass)
    await run_log.async_setup()
    hass.data.setdefault("zha_sonoff_quirks", {})["run_log"] = run_log
    mgr = schedules.SwvScheduleManager(hass, run_log)
    monkeypatch.setattr(
        mgr, "_cluster", lambda device_id, ep: by_device[device_id].endpoint(ep)
    )
    await mgr.async_setup()
    schedules.async_setup_schedule_services(hass, mgr)
    yield mgr
    await mgr.async_unload()
    await run_log.async_unload()
    quirk.set_local_timezone(None)
