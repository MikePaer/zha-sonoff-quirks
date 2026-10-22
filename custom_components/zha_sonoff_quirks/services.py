"""Device-centric irrigation services for the SONOFF SWV-ZF2 water valve.

The quirk (see ``quirks/sonoff_swv_zf2.py``) exposes the valve's irrigation
configuration as plain HA entities: a mode select, target number entities
(duration in minutes / volume in liters), a fail-safe number, and one On/Off
switch per channel. Starting a volume- or time-limited run therefore means
"write the config entities, then flip the channel switch" — the valve firmware
reads the (global, endpoint-1) config when a channel opens and closes the
channel by itself once the target is reached.

The services registered here bundle that multi-entity sequence into a single
device-centric call so cards and automations don't have to know the entity
layout:

    zha_sonoff_quirks.irrigation_by_liters(target | device_id, channel, liters,
                                           fail_safe_minutes?)
    zha_sonoff_quirks.irrigation_by_minutes(target | device_id, channel, minutes,
                                            fail_safe_minutes?)

The valve is addressed by a standard HA service *target* (`services.yaml`
declares ``target:``), which is what makes the two services show up in the
automation editor's "by target" tab when the valve device is selected — device
actions cannot do that since HA 2026.8 (see TODO #12). HA merges the target
into ``call.data`` before validation, so ``device_id`` / ``entity_id`` /
``area_id`` / ``floor_id`` / ``label_id`` are plain data fields here, and an
automation saved before the target existed (``data: {device_id: …}``) keeps
working unchanged. The target only picks the DEVICE; ``channel`` stays the
one and only way to choose the line.

``services.yaml`` is static, but the line radio can still show the names the
user gave the lines (the ``text`` line-name entities, stored in the config
entry options): ``async_publish_service_descriptions`` rewrites the two
service descriptions at runtime through ``async_set_service_schema``, with
``A — Giardino`` style option labels, and ``async_refresh_service_descriptions``
re-registers the services afterwards so the frontend (which refetches all
service descriptions a few seconds after a ``service_registered`` event) picks
the new labels up without a page reload. Labels are only possible while
exactly ONE SWV valve is registered: the form does not know which device the
target holds, and one label set must fit every valve.

There is deliberately no server-side timer or monitoring task: the SWV-ZF2
auto-closes on-device, so once the switch is on the job is done. Stopping a
run early is a plain ``switch.turn_off`` on the channel switch.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import target as target_helpers
from homeassistant.helpers.selector import TargetSelector
from homeassistant.helpers.service import async_set_service_schema
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml_dict
import voluptuous as vol

from .const import (
    CHANNEL_LABELS,
    CHANNELS,
    DOMAIN,
    OPTIONS_LINE_NAMES,
    SWV_MODELS,
    normalize_channel,
)
from .helpers import find_swv_switches, resolve_entities

_LOGGER = logging.getLogger(__name__)

SERVICE_IRRIGATION_BY_LITERS = "irrigation_by_liters"
SERVICE_IRRIGATION_BY_MINUTES = "irrigation_by_minutes"

_SERVICES_YAML = Path(__file__).parent / "services.yaml"
# hass.data[DOMAIN] keys used by the description publisher.
_DATA_SERVICES_YAML = "services_yaml"  # parsed services.yaml, loaded once
_DATA_HANDLERS = "service_handlers"  # {service: (handler, schema)} for re-registration
_DATA_PUBLISHED_LABELS = "published_channel_labels"  # last labels pushed to HA

ATTR_CHANNEL = "channel"
ATTR_LITERS = "liters"
ATTR_MINUTES = "minutes"
ATTR_FAIL_SAFE_MINUTES = "fail_safe_minutes"

# Exact option strings of the quirk's irrigation-mode select as HA exposes
# them: zha's ZCLEnumSelectEntity turns enum member names into options via
# name.replace("_", " "), so IrrigationMode.duration -> "duration" and
# IrrigationMode.capacity -> "capacity" (lowercase, and the third option is
# "duration with interval" WITH spaces). select.select_option only accepts
# these exact strings.
MODE_OPTION_DURATION = "duration"
MODE_OPTION_CAPACITY = "capacity"

#: Accepted spellings, for the error message: 1, 2, A, B.
_CHANNEL_CHOICES = ", ".join([*CHANNELS, *CHANNEL_LABELS.values()])


def _channel(value: Any) -> str:
    """Normalize the channel field, accepting the valve's A/B panel letters.

    The selector sends the canonical "1"/"2", but a hand-written automation
    may well use the letters printed on the device — or the bare number 1,
    which YAML parses as an int. All of them normalize here, so nothing
    downstream ever sees anything but "1" or "2".
    """
    channel = normalize_channel(value)
    if channel is None:
        raise vol.Invalid(f"channel must be one of {_CHANNEL_CHOICES} (got {value!r})")
    return channel


# Shared fields of both services. The target keys (``device_id`` included —
# ``cv.ENTITY_SERVICE_FIELDS`` accepts each as a string or a list) come from
# HA's standard target, merged into call.data; at least one of them is
# required (``_HAS_TARGET`` below), so a call that names no valve at all is
# rejected by the schema, before the handler runs.
_COMMON_FIELDS = {
    **cv.ENTITY_SERVICE_FIELDS,
    vol.Required(ATTR_CHANNEL): _channel,
    vol.Optional(ATTR_FAIL_SAFE_MINUTES): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=719)
    ),
}
_HAS_TARGET = cv.has_at_least_one_key(*cv.ENTITY_SERVICE_FIELDS)

LITERS_SCHEMA = vol.All(
    vol.Schema(
        {
            **_COMMON_FIELDS,
            vol.Required(ATTR_LITERS): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=10000)
            ),
        }
    ),
    _HAS_TARGET,
)

MINUTES_SCHEMA = vol.All(
    vol.Schema(
        {
            **_COMMON_FIELDS,
            vol.Required(ATTR_MINUTES): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=719)
            ),
        }
    ),
    _HAS_TARGET,
)


def _device_from_call(hass: HomeAssistant, call: ServiceCall) -> str:
    """Resolve the service target to the ONE SWV-ZF2 device it designates.

    Devices come straight from the target (or through an area / floor /
    label); an entity target — a history sensor of this integration, which
    is what the target picker's entity filter offers, or any other entity of
    the valve — is mapped through its registry device. Only SWV-ZF2* devices
    count, so a stray device in a targeted area is ignored rather than
    driven. Exactly one valve is required: the run is keyed by its channel
    switch, and an automation that wants two valves adds two actions. Both
    failure modes are user errors, reported as ServiceValidationError so the
    automation editor / Developer Tools show the message, not a traceback.
    """
    selection = target_helpers.TargetSelection(call.data)
    if not selection.has_any_target:
        # ``device_id: none`` & co. pass the schema but select nothing.
        raise ServiceValidationError("No SONOFF SWV-ZF2 valve was targeted")
    extracted = target_helpers.async_extract_referenced_entity_ids(
        hass, selection, expand_group=False, primary_entities_only=False
    )
    ent_reg = er.async_get(hass)
    candidates = set(extracted.referenced_devices)
    for entity_id in extracted.referenced:
        entry = ent_reg.async_get(entity_id)
        if entry is not None and entry.device_id is not None:
            candidates.add(entry.device_id)

    dev_reg = dr.async_get(hass)
    valves: dict[str, str] = {}
    for device_id in candidates:
        device = dev_reg.async_get(device_id)
        if device is not None and device.model in SWV_MODELS:
            valves[device_id] = device.name_by_user or device.name or device_id
    if not valves:
        raise ServiceValidationError(
            "The target contains no SONOFF SWV-ZF2 valve"
        )
    if len(valves) > 1:
        raise ServiceValidationError(
            "The target contains several SONOFF SWV-ZF2 valves "
            f"({', '.join(sorted(valves.values()))}); pick one valve per action"
        )
    return next(iter(valves))


async def _async_start_irrigation(
    hass: HomeAssistant,
    device_id: str,
    channel: str,
    mode_option: str,
    target_key: str,
    target_label: str,
    target_value: int,
    fail_safe_minutes: int | None,
    context: Context | None,
) -> None:
    """Write the irrigation config to the valve, then open the channel.

    Order matters: the irrigation config attribute is GLOBAL for both
    channels and the firmware snapshots it when a channel opens, so mode,
    target and fail-safe must all be written BEFORE the switch turns on.
    Every call uses blocking=True so a Zigbee write failure surfaces here
    (and aborts the sequence) instead of opening the valve half-configured.

    The originating ServiceCall's context is threaded into every sub-call so
    the logbook attributes the valve opening to the user/automation that
    asked for it, and HA's per-user entity permission checks apply.
    """
    entities = resolve_entities(hass, device_id, channel)
    if not any(entities.values()):
        raise HomeAssistantError(
            f"No SWV-ZF2 entities found for device {device_id} — is it a "
            "SONOFF SWV-ZF2 with the zha_sonoff_quirks quirk applied?"
        )

    missing = []
    if entities["mode"] is None:
        missing.append("irrigation mode select (irrigation_mode)")
    if entities[target_key] is None:
        missing.append(target_label)
    if entities["switch"] is None:
        missing.append(f"channel {channel} switch")
    # fail_safe is optional overall, but if the caller explicitly asked for a
    # fail-safe we must not silently drop a safety setting.
    if fail_safe_minutes is not None and entities["fail_safe"] is None:
        missing.append("fail-safe number (fail_safe)")
    if missing:
        raise HomeAssistantError(
            f"Missing required entities for device {device_id}: "
            f"{', '.join(missing)}. The zha_sonoff_quirks quirk may not be "
            "applied to this device (try reconfiguring it in ZHA)."
        )

    # Refuse to reconfigure a channel mid-run: rewriting the global config
    # while a session is active would silently retarget the running session.
    switch_entity = entities["switch"]
    switch_state = hass.states.get(switch_entity)
    if switch_state is not None and switch_state.state == "on":
        raise HomeAssistantError(
            f"Channel {channel} ({switch_entity}) is already irrigating — "
            "stop it with switch.turn_off before starting a new run."
        )

    _LOGGER.debug(
        "Starting irrigation on %s ch%s: mode=%s target=%s fail_safe=%s",
        device_id,
        channel,
        mode_option,
        target_value,
        fail_safe_minutes,
    )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entities["mode"], "option": mode_option},
        blocking=True,
        context=context,
    )
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": entities[target_key], "value": target_value},
        blocking=True,
        context=context,
    )
    if fail_safe_minutes is not None:
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entities["fail_safe"], "value": fail_safe_minutes},
            blocking=True,
            context=context,
        )
    # Hand the run log its attribution BEFORE the switch turns on: the
    # observer reads (and consumes) this on the off→on transition. Cleared on
    # failure so a run the valve never started can't tag the next manual one.
    pending = hass.data.setdefault(DOMAIN, {}).setdefault("pending", {})
    pending[switch_entity] = {
        "source": "integration",
        # The run log drops entries older than its TTL: if the switch never
        # confirms this start, the attribution must not stick to a much later
        # manual run.
        "ts": dt_util.utcnow().timestamp(),
    }
    try:
        # Open the valve last. Nothing else to do afterwards: the SWV-ZF2
        # closes the channel on-device when the target is reached.
        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": switch_entity},
            blocking=True,
            context=context,
        )
    except Exception:
        pending.pop(switch_entity, None)
        raise


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the two irrigation services.

    Plain sync helper (registration itself is synchronous) called from
    ``async_setup_entry``; the singleton config flow guarantees this runs at
    most once per HA session, so no double-registration guard is needed.
    """

    async def _handle_liters(call: ServiceCall) -> None:
        """Start a volume-limited run ("capacity" mode)."""
        await _async_start_irrigation(
            hass,
            _device_from_call(hass, call),
            call.data[ATTR_CHANNEL],
            MODE_OPTION_CAPACITY,
            "volume",
            "volume number (irrigation_volume)",
            call.data[ATTR_LITERS],
            call.data.get(ATTR_FAIL_SAFE_MINUTES),
            call.context,
        )

    async def _handle_minutes(call: ServiceCall) -> None:
        """Start a time-limited run ("duration" mode)."""
        await _async_start_irrigation(
            hass,
            _device_from_call(hass, call),
            call.data[ATTR_CHANNEL],
            MODE_OPTION_DURATION,
            "duration",
            "duration number (irrigation_duration)",
            call.data[ATTR_MINUTES],
            call.data.get(ATTR_FAIL_SAFE_MINUTES),
            call.context,
        )

    handlers = {
        SERVICE_IRRIGATION_BY_LITERS: (_handle_liters, LITERS_SCHEMA),
        SERVICE_IRRIGATION_BY_MINUTES: (_handle_minutes, MINUTES_SCHEMA),
    }
    hass.data.setdefault(DOMAIN, {})[_DATA_HANDLERS] = handlers
    for service, (handler, schema) in handlers.items():
        hass.services.async_register(DOMAIN, service, handler, schema=schema)


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the irrigation services (called from ``async_unload_entry``)."""
    hass.services.async_remove(DOMAIN, SERVICE_IRRIGATION_BY_LITERS)
    hass.services.async_remove(DOMAIN, SERVICE_IRRIGATION_BY_MINUTES)
    data = hass.data.get(DOMAIN, {})
    data.pop(_DATA_HANDLERS, None)
    data.pop(_DATA_PUBLISHED_LABELS, None)


# ---------------------------------------------------------------------------
# Runtime service descriptions: the line radio labelled with the line names.
# ---------------------------------------------------------------------------


def _channel_labels(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, str] | None:
    """Option labels for the ``channel`` radio, or None to keep the generic ones.

    ``A — Giardino`` for a named line, the bare letter for an unnamed one.
    None when no line is named (nothing to gain over the translated "Line A /
    Line B") and when several SWV valves are registered: the description is
    one for all of them and the form cannot know which valve the target
    holds, so per-device names would be wrong for every valve but one.
    """
    switches = find_swv_switches(hass)
    devices = {info["device_id"] for info in switches}
    if len(devices) != 1:
        return None
    names = entry.options.get(OPTIONS_LINE_NAMES) or {}
    by_channel = {info["channel"]: names.get(info["switch"], "") for info in switches}
    if not any(by_channel.values()):
        return None
    labels: dict[str, str] = {}
    for channel in CHANNELS:
        letter = CHANNEL_LABELS[channel]
        name = by_channel.get(channel, "")
        labels[channel] = f"{letter} — {name}" if name else letter
    return labels


async def _async_services_yaml(hass: HomeAssistant) -> dict[str, Any]:
    """Return the parsed services.yaml, loaded once per HA session."""
    data = hass.data.setdefault(DOMAIN, {})
    if _DATA_SERVICES_YAML not in data:
        data[_DATA_SERVICES_YAML] = await hass.async_add_executor_job(
            load_yaml_dict, str(_SERVICES_YAML)
        )
    return data[_DATA_SERVICES_YAML]


async def async_publish_service_descriptions(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Push the two service descriptions to HA, with the current line labels.

    Starts from services.yaml (so selectors, target and field layout have a
    single source of truth) and, when `_channel_labels` has something to say,
    swaps the ``channel`` select's translated options for labelled ones.
    Returns True when the labels differ from the last published set — the
    caller decides whether that is worth a service re-registration.
    """
    data = hass.data.setdefault(DOMAIN, {})
    labels = _channel_labels(hass, entry)
    if _DATA_PUBLISHED_LABELS in data and data[_DATA_PUBLISHED_LABELS] == labels:
        return False
    yaml_desc = await _async_services_yaml(hass)
    for service in (SERVICE_IRRIGATION_BY_LITERS, SERVICE_IRRIGATION_BY_MINUTES):
        desc = copy.deepcopy(yaml_desc.get(service) or {})
        # HA validates services.yaml through TargetSelector.CONFIG_SCHEMA when
        # it loads the file itself, which turns a scalar ``domain: sensor`` /
        # ``device_class: timestamp`` into one-element lists. We read the raw
        # YAML instead, and ``async_set_service_schema`` stores the target
        # verbatim; ``get_services_for_target`` (the "by target" tab) then
        # does ``set("sensor")`` — a set of letters — and never matches, so
        # the services vanish from the tab. Normalise the same way HA does.
        if "target" in desc:
            desc["target"] = TargetSelector.CONFIG_SCHEMA(desc["target"])
        if labels:
            select = desc["fields"][ATTR_CHANNEL]["selector"]["select"]
            select["options"] = [
                {"value": channel, "label": labels[channel]} for channel in CHANNELS
            ]
            # The translation would win over the labels in the frontend.
            select.pop("translation_key", None)
        async_set_service_schema(hass, DOMAIN, service, desc)
    data[_DATA_PUBLISHED_LABELS] = labels
    _LOGGER.debug("Published service descriptions with channel labels %s", labels)
    return True


async def async_refresh_service_descriptions(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Re-publish the descriptions and make connected frontends reload them.

    ``async_set_service_schema`` alone is invisible to an open browser: the
    frontend caches ``hass.services`` and only refetches the whole set a few
    seconds after a ``service_registered`` event. Re-registering the two
    services (remove + register) fires exactly that. Done only when the labels
    actually changed, so a startup or a no-op rename does not churn events.
    """
    if not await async_publish_service_descriptions(hass, entry):
        return
    handlers = hass.data.get(DOMAIN, {}).get(_DATA_HANDLERS)
    if not handlers:
        return
    # HA's per-service description cache survives the remove/register pair
    # (nothing evicts it), so the descriptions published above are what the
    # frontend's refetch will see.
    for service, (handler, schema) in handlers.items():
        hass.services.async_remove(DOMAIN, service)
        hass.services.async_register(DOMAIN, service, handler, schema=schema)
