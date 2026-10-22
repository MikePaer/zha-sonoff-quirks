"""Entity-registry resolution helpers shared by services, run log and sensors.

Everything here resolves entities by registry unique_id, never by entity_id:
users rename entity_ids freely, but unique_ids are stable. The quirk entities
get unique_id = f"{ieee}-{endpoint}-{unique_id_suffix}"; the channel switches
are plain zha OnOff entities with unique_id "<ieee>-<endpoint>" or
"<ieee>-<endpoint>-6" depending on the endpoint's Zigbee device_type.
"""

from __future__ import annotations

import re

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import SWV_MODELS

# unique_id suffixes of the quirk entities, WITH the joining hyphen included:
# matching without the leading "-" is ambiguous (e.g. bare "duration" is a
# suffix of both "irrigation_duration" and "session_target_duration").
SUFFIX_MODE = "-irrigation_mode"
SUFFIX_DURATION = "-irrigation_duration"
SUFFIX_VOLUME = "-irrigation_volume"
SUFFIX_FAIL_SAFE = "-fail_safe"
SUFFIX_SESSION_VOLUME = "-session_volume"

# Channel switch unique_id tail (see module docstring). Only ever applied to
# switch-domain entries: outside that domain a bare "...-1" tail is ambiguous
# (the battery sensor's unique_id "<ieee>-1-1" also ends with "-1").
SWITCH_UNIQUE_ID_RE = re.compile(r"-([12])(-6)?$")


@callback
def resolve_entities(
    hass: HomeAssistant, device_id: str, channel: str
) -> dict[str, str | None]:
    """Map a device-registry id to the entity_ids the integration drives.

    Returns a dict with keys ``mode``/``duration``/``volume``/``fail_safe``/
    ``session_volume``/``switch`` (the switch is the one for ``channel``);
    unmatched keys are None so the caller can report exactly what is missing.
    """
    ent_reg = er.async_get(hass)
    found: dict[str, str | None] = {
        "mode": None,
        "duration": None,
        "volume": None,
        "fail_safe": None,
        "session_volume": None,
        "switch": None,
    }
    for entry in er.async_entries_for_device(ent_reg, device_id):
        # Registry unique_ids keep the ieee's colons; lowercase both sides so
        # a differently-cased ieee cannot break the suffix match.
        uid = entry.unique_id.lower()
        if entry.domain == "select" and uid.endswith(SUFFIX_MODE):
            found["mode"] = entry.entity_id
        elif entry.domain == "number":
            if uid.endswith(SUFFIX_DURATION):
                found["duration"] = entry.entity_id
            elif uid.endswith(SUFFIX_VOLUME):
                found["volume"] = entry.entity_id
            elif uid.endswith(SUFFIX_FAIL_SAFE):
                found["fail_safe"] = entry.entity_id
        elif entry.domain == "sensor" and uid.endswith(SUFFIX_SESSION_VOLUME):
            found["session_volume"] = entry.entity_id
        elif entry.domain == "switch":
            match = SWITCH_UNIQUE_ID_RE.search(uid)
            if match is not None and match.group(1) == channel:
                found["switch"] = entry.entity_id
    return found


def all_devices(dev_reg: dr.DeviceRegistry) -> list[dr.DeviceEntry]:
    """Every device entry, on any HA version.

    HA 2026.10 deprecated using ``DeviceRegistry.devices`` as a mapping
    (``.values()``; an error from 2027.9): iterating it now yields the
    entries. Older releases iterate a dict of ids, looked up here.
    """
    return [
        dev_reg.async_get(item) if isinstance(item, str) else item
        for item in dev_reg.devices
    ]


@callback
def find_swv_switches(hass: HomeAssistant) -> list[dict[str, str]]:
    """Every channel switch of every SWV-ZF2* device currently registered.

    Returns a list of ``{"switch": entity_id, "device_id": ..., "channel":
    "1"|"2"}`` dicts. Matching is on the device registry's model string, so it
    works whether or not the quirk is currently applied (the switches are
    plain zha entities that exist either way).
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    out: list[dict[str, str]] = []
    for device in all_devices(dev_reg):
        if device.model not in SWV_MODELS:
            continue
        for entry in er.async_entries_for_device(ent_reg, device.id):
            if entry.domain != "switch":
                continue
            match = SWITCH_UNIQUE_ID_RE.search(entry.unique_id.lower())
            if match is None:
                continue
            out.append(
                {
                    "switch": entry.entity_id,
                    "device_id": device.id,
                    "channel": match.group(1),
                }
            )
    return out
