"""Per-channel irrigation run log — the system of record for run history.

Same architecture proven in ``tuya-cards-for-ha``: recording hangs off the
**channel switch entity's state**, the one signal common to every run origin
(the integration's services, a bare ``switch.turn_on``, an automation, the
physical button, the firmware's on-device auto-close). The services only
provide attribution ("integration" vs "manual") via a transient ``pending``
hand-off; everything else is read from device truth when the run opens.

SWV-ZF2 specifics versus the Tuya original:

* two channels per device, tracked independently (one record stream per
  switch entity);
* mode and target are NOT service-private knowledge — the quirk exposes the
  device's global irrigation config as entities, so the observer snapshots
  mode/target at run open and gets correct metadata even for manual runs;
* liters come from the quirk's ``session_volume`` sensor (per-session, global
  endpoint-1 feed). If both channels were open at any point during a run the
  attribution is ambiguous and liters are recorded as None;
* the device closes the valve itself, so "completed vs stopped early" is
  inferred by comparing the outcome with the snapshotted target
  (:func:`history_logic.classify_reason`).

Durability: finalized runs append to a ``helpers.storage.Store`` JSON file
under ``.storage/`` — atomic, versioned, outside the recorder DB (no bloat, no
purge), surviving HA restarts. An ``in_flight`` snapshot lets a run that spans
a restart still be recorded (reason ``shutdown``).
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    EVENT_IRRIGATION_COMPLETED,
    MIN_RUN_S,
    RUNS_STORE_CAP,
    SESSION_SETTLE_S,
    STORAGE_KEY,
    STORAGE_VERSION,
    history_signal,
)
from .helpers import find_swv_switches, resolve_entities
from .history_logic import (
    build_record,
    classify_reason,
    session_liters,
    should_discard,
    source_from_context,
)

_LOGGER = logging.getLogger(__name__)

_BAD_STATES = ("unknown", "unavailable", "none", "")

#: A pending service attribution older than this is stale: the switch never
#: confirmed the start (lost Zigbee report, device offline). Without a TTL it
#: would linger and mislabel a much later manual run as integration-driven.
_PENDING_TTL_S = 120.0

#: A "this session is plan #n" report (0x501F) that arrives before the switch
#: turns on waits this long for the run to open.
_SESSION_TAG_TTL_S = 60.0


def _empty_channel() -> dict:
    """Fresh per-switch record."""
    return {"water_total_l": 0.0, "in_flight": None, "runs": []}


class SwvRunLog:
    """Observes the channel switches and records every completed run."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Prepare the (not yet loaded) store and the tracking maps."""
        self.hass = hass
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: dict = {"channels": {}}
        # switch entity_id -> {"device_id", "channel"} for every tracked switch.
        self._switch_info: dict[str, dict[str, str]] = {}
        self._unsubs: dict[str, callable] = {}
        self._reg_unsub: callable | None = None
        self._call_unsub: callable | None = None
        # switch -> asyncio.Task awaiting the post-close grace before finalizing.
        self._finalize_tasks: dict[str, asyncio.Task] = {}
        # switch -> ISO of the REAL off-transition, captured when the grace
        # starts so the deferred finalize records the true close time.
        self._off_at: dict[str, str] = {}
        # switch -> scheduled-session tag that arrived before the run opened.
        self._session_tags: dict[str, dict] = {}

    # ── lifecycle ──
    async def async_setup(self) -> None:
        """Load the store, subscribe to switches, recover any in-flight run."""
        loaded = await self._store.async_load()
        if isinstance(loaded, dict) and "channels" in loaded:
            self._data = loaded
        self._data.setdefault("channels", {})
        self._refresh_subscriptions()
        self._reg_unsub = self.hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_registry_updated
        )
        self._call_unsub = self.hass.bus.async_listen(
            EVENT_CALL_SERVICE, self._on_call_service
        )
        await self._recover_in_flight()

    async def async_unload(self) -> None:
        """Unsubscribe and flush the store."""
        for unsub in self._unsubs.values():
            unsub()
        self._unsubs.clear()
        if self._reg_unsub is not None:
            self._reg_unsub()
            self._reg_unsub = None
        if self._call_unsub is not None:
            self._call_unsub()
            self._call_unsub = None
        for switch in list(self._finalize_tasks):
            self._cancel_finalize(switch)
        self._off_at.clear()
        await self._store.async_save(self._data)

    # ── subscriptions / discovery ──
    @callback
    def _refresh_subscriptions(self) -> None:
        """Subscribe to every SWV channel switch (idempotent)."""
        for info in find_swv_switches(self.hass):
            switch = info["switch"]
            self._switch_info[switch] = info
            if switch in self._unsubs:
                continue
            self._unsubs[switch] = async_track_state_change_event(
                self.hass, [switch], self._on_state_change
            )
            _LOGGER.debug("Run log now tracking %s (ch%s)", switch, info["channel"])

    @callback
    def _on_registry_updated(self, event: Event) -> None:
        # A valve paired after setup registers its switches here; pick them
        # up. Cheap pre-filter first: this bus event fires for EVERY entity of
        # every integration (hundreds in startup bursts) and the full registry
        # scan is only worth running for switch entities appearing or renaming.
        data = event.data
        if data.get("action") not in ("create", "update"):
            return
        if not str(data.get("entity_id", "")).startswith("switch."):
            return
        self._refresh_subscriptions()

    @callback
    def _on_call_service(self, event: Event) -> None:
        """Attribute HA's own switch commands as ``manual`` runs.

        ZHA writes a valve switch's state from the valve's own report, which
        carries no HA context, so an automation's ``switch.turn_on`` looked
        exactly like the button ("physical", 2026-10-22). The service call
        itself is the reliable signal: it leaves a short-lived pending
        attribution, which the integration's own services (set just before
        their own call) take precedence over.
        """
        data = event.data
        if data.get("domain") not in ("switch", "homeassistant"):
            return
        if data.get("service") not in ("turn_on", "toggle"):
            return
        entity_ids = (data.get("service_data") or {}).get("entity_id") or []
        if isinstance(entity_ids, str):
            entity_ids = [e.strip() for e in entity_ids.split(",")]
        tracked = [e for e in entity_ids if e in self._switch_info]
        if not tracked:
            return
        pending = self.hass.data.setdefault(DOMAIN, {}).setdefault("pending", {})
        now = dt_util.utcnow().timestamp()
        for switch in tracked:
            pending.setdefault(switch, {"source": "manual", "ts": now})

    # ── state observation ──
    @callback
    def _on_state_change(self, event: Event) -> None:
        switch: str = event.data["entity_id"]
        new = event.data.get("new_state")
        if new is None:
            return
        state = new.state
        channel = self._data["channels"].get(switch)
        has_inflight = bool(channel and channel.get("in_flight"))
        if state == "on":
            # Reopened (possibly after a transient drop within the grace
            # window): keep the run going rather than finalizing it, and drop
            # the captured off time — it is no longer a run end. The persisted
            # copy in in_flight must go too, or a later restart would treat
            # the stale stamp as the run's true end.
            self._cancel_finalize(switch)
            self._off_at.pop(switch, None)
            if has_inflight:
                if channel["in_flight"].pop("off_at", None) is not None:
                    self._save()
            else:
                self._open_run(switch, new.context)
        elif state == "off":
            if has_inflight:
                self._schedule_finalize(switch)
        # 'unavailable' / 'unknown': ignore — a Zigbee drop must not end a run.

    @callback
    def _open_run(self, switch: str, context: Context | None = None) -> None:
        channel = self._data["channels"].setdefault(switch, _empty_channel())
        if channel.get("in_flight"):
            return
        info = self._switch_info.get(switch, {})
        pending = (self.hass.data.get(DOMAIN, {}).get("pending", {}) or {}).pop(
            switch, None
        ) or {}
        age = dt_util.utcnow().timestamp() - float(pending.get("ts", 0.0))
        if pending and age > _PENDING_TTL_S:
            _LOGGER.debug("Ignoring stale pending attribution on %s", switch)
            pending = {}
        mode, target = self._snapshot_config(info)
        source = pending.get("source") or source_from_context(
            getattr(context, "user_id", None), getattr(context, "parent_id", None)
        )
        channel["in_flight"] = {
            "start": dt_util.utcnow().isoformat(),
            "source": source,
            "mode": mode,
            "target": target,
            "channel": info.get("channel"),
            "ambiguous": False,
        }
        tag = self._session_tags.pop(switch, None)
        if tag and dt_util.utcnow().timestamp() - tag["ts"] <= _SESSION_TAG_TTL_S:
            self._apply_session_tag(channel["in_flight"], tag)
        # The session feed is global: if the sibling channel is already
        # running, neither run's liters can be attributed. Poison both.
        for other_switch, other in self._data["channels"].items():
            if other_switch == switch or not other.get("in_flight"):
                continue
            if (
                self._switch_info.get(other_switch, {}).get("device_id")
                == info.get("device_id")
            ):
                other["in_flight"]["ambiguous"] = True
                channel["in_flight"]["ambiguous"] = True
        _LOGGER.debug(
            "Run opened on %s (%s, mode=%s target=%s)",
            switch,
            channel["in_flight"]["source"],
            mode,
            target,
        )
        self._save()

    @callback
    def tag_session(
        self,
        device_id: str,
        channel: str,
        plan_index: int,
        mode: str | None,
        target: float | None,
    ) -> None:
        """Mark the device's current run as on-valve plan ``plan_index``.

        Called by the schedule manager for each 0x501F report that says the
        session was started by a plan (reported every few seconds while the
        valve runs). The line's in-flight run is re-tagged ``scheduled``, and
        its mode/target come from the plan rather than the manual default
        config the observer snapshotted. If the report is ahead of the switch
        state, the tag waits for the run to open. If the line has no run but
        exactly one line of the device does, that one is tagged: on some
        firmware both lines may report on endpoint 1.
        """
        tag = {
            "plan_index": plan_index,
            "mode": mode,
            "target": target,
            "ts": dt_util.utcnow().timestamp(),
        }
        device_switches = [
            switch
            for switch, info in self._switch_info.items()
            if info.get("device_id") == device_id
        ]
        running = [
            switch
            for switch in device_switches
            if self._data["channels"].get(switch, {}).get("in_flight")
        ]
        target_switch = next(
            (
                s
                for s in device_switches
                if self._switch_info[s].get("channel") == channel
            ),
            None,
        )
        if target_switch not in running and len(running) == 1:
            target_switch = running[0]
        if target_switch is None:
            return
        inf = self._data["channels"].get(target_switch, {}).get("in_flight")
        if not inf:
            self._session_tags[target_switch] = tag
            return
        if inf.get("source") == "scheduled" and inf.get("plan_index") == plan_index:
            return
        self._apply_session_tag(inf, tag)
        self._save()

    @staticmethod
    def _apply_session_tag(inf: dict, tag: dict) -> None:
        inf["source"] = "scheduled"
        inf["plan_index"] = tag["plan_index"]
        if tag.get("mode") is not None:
            inf["mode"] = tag["mode"]
            inf["target"] = tag["target"]

    @callback
    def _snapshot_config(self, info: dict) -> tuple[str | None, float | None]:
        """Mode and target active when the run opens (device-global config).

        The firmware snapshots the global config when a channel opens, so
        reading the config entities at the on-transition matches what the
        device will actually do — for manual runs too, which is metadata the
        Tuya original never had.

        Target units follow the mode: liters for ``capacity``, minutes for
        ``duration``/``duration with interval``.
        """
        device_id = info.get("device_id")
        if not device_id:
            return None, None
        entities = resolve_entities(self.hass, device_id, info.get("channel", "1"))
        mode = self._state_value(entities["mode"])
        if mode not in ("capacity", "duration", "duration with interval"):
            return None, None
        target_entity = (
            entities["volume"] if mode == "capacity" else entities["duration"]
        )
        target_raw = self._state_value(target_entity)
        try:
            target = float(target_raw)
        except (TypeError, ValueError):
            target = None
        return mode, target

    @callback
    def _state_value(self, entity_id: str | None) -> str | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in _BAD_STATES:
            return None
        return state.state

    @callback
    def _schedule_finalize(self, switch: str) -> None:
        self._cancel_finalize(switch)
        # Capture the REAL off-transition time now; the grace below only
        # defers reading the final session volume — it must not inflate the
        # recorded duration. Persisted into in_flight too: a restart landing
        # inside the grace window must not lose the observed close time.
        off_iso = dt_util.utcnow().isoformat()
        self._off_at[switch] = off_iso
        inf = self._data["channels"].get(switch, {}).get("in_flight")
        if inf is not None:
            inf["off_at"] = off_iso
            self._save()
        self._finalize_tasks[switch] = self.hass.async_create_task(
            self._finalize_after_grace(switch)
        )

    @callback
    def _cancel_finalize(self, switch: str) -> None:
        task = self._finalize_tasks.pop(switch, None)
        if task and not task.done():
            task.cancel()

    async def _finalize_after_grace(self, switch: str) -> None:
        try:
            await asyncio.sleep(SESSION_SETTLE_S)
        except asyncio.CancelledError:
            return
        self._finalize_tasks.pop(switch, None)
        await self._finalize(switch)

    async def _finalize(self, switch: str, reason_override: str | None = None) -> None:
        channel = self._data["channels"].get(switch)
        if not channel or not channel.get("in_flight"):
            return
        inf = channel["in_flight"]
        start = (
            dt_util.parse_datetime(inf.get("start", "")) if inf.get("start") else None
        )
        # End = the real off-transition time captured at _schedule_finalize,
        # so the grace delay never inflates it. Falls back to now for the
        # shutdown-recovery and force-finalize paths where no off was observed.
        off_iso = self._off_at.pop(switch, None) or inf.get("off_at")
        end = (dt_util.parse_datetime(off_iso) if off_iso else None) or dt_util.utcnow()
        duration_s = (end - start).total_seconds() if start else 0.0
        source = inf.get("source", "manual")

        if should_discard(source, duration_s, MIN_RUN_S, reason_override is not None):
            channel["in_flight"] = None
            self._save()
            _LOGGER.debug("Discarded %s manual flap (%.1fs)", switch, duration_s)
            return

        # Runs that span a restart never get liters: at boot the session
        # sensor's last_updated is the state-restore time, so the freshness
        # check against the (pre-restart) run start would happily attribute
        # the PREVIOUS session's persisted total and double-count it into the
        # water statistic. Undercounting is the safe direction.
        recovered = bool(inf.get("recovered")) or reason_override is not None
        liters = None if recovered else self._session_liters_for(switch, inf, start)
        reason = reason_override or classify_reason(
            inf.get("mode"), inf.get("target"), duration_s, liters
        )
        record = build_record(
            start_iso=inf.get("start"),
            end_iso=end.isoformat(),
            duration_s=duration_s,
            liters=liters,
            mode=inf.get("mode"),
            target=inf.get("target"),
            source=source,
            reason=reason,
            channel=inf.get("channel") or "?",
            plan_index=inf.get("plan_index"),
        )
        runs = channel.setdefault("runs", [])
        runs.insert(0, record)
        del runs[RUNS_STORE_CAP:]
        if liters:
            channel["water_total_l"] = round(
                float(channel.get("water_total_l", 0.0)) + float(liters), 2
            )
        channel["in_flight"] = None
        self._save()

        async_dispatcher_send(self.hass, history_signal(switch))
        self._fire_event(switch, record)
        _LOGGER.info(
            "Recorded irrigation on %s: %ss, %s L (%s/%s)",
            switch,
            record["duration_s"],
            record["liters"],
            source,
            reason,
        )

    @callback
    def _session_liters_for(
        self, switch: str, inf: dict, start
    ) -> float | None:
        """Read the final session_volume, attributed to this run if possible."""
        info = self._switch_info.get(switch, {})
        device_id = info.get("device_id")
        if not device_id:
            return None
        entities = resolve_entities(self.hass, device_id, info.get("channel", "1"))
        state = (
            self.hass.states.get(entities["session_volume"])
            if entities["session_volume"]
            else None
        )
        if state is None or state.state in _BAD_STATES:
            return None
        return session_liters(
            state.state,
            state.last_updated.timestamp(),
            start.timestamp() if start else None,
            bool(inf.get("ambiguous")),
        )

    async def _recover_in_flight(self) -> None:
        """Finalize runs whose close we missed while HA was down.

        A non-None ``in_flight`` at startup means a run was open at the
        previous shutdown. Decision table, in order:

        * switch confirmed still ``on`` — the run is genuinely continuing;
          leave it to the live observer (marked ``recovered`` so its liters
          are not mis-attributed across the restart);
        * ``off_at`` persisted — the close WAS observed before shutdown (the
          restart landed inside the settle grace): finalize now with the true
          end time and the normal reason classification;
        * switch confirmed ``off`` — the close happened while HA was down:
          finalize best-effort with reason ``shutdown``;
        * switch ``unavailable``/unknown (the norm at boot, ZHA restores
          availability later) — DEFER: keep ``in_flight`` and let the
          observer's first real state decide. Finalizing here would cut a run
          that is still going in the garden into two bogus records.
        """
        for switch, channel in list(self._data["channels"].items()):
            inf = channel.get("in_flight")
            if not inf:
                continue
            state = self.hass.states.get(switch)
            if state is not None and state.state == "on":
                inf["recovered"] = True
                self._save()
                continue
            if inf.get("off_at"):
                inf["recovered"] = True
                await self._finalize(switch)
                continue
            if state is not None and state.state == "off":
                await self._finalize(switch, reason_override="shutdown")
                continue
            inf["recovered"] = True
            self._save()

    # ── helpers ──
    @callback
    def _fire_event(self, switch: str, record: dict) -> None:
        info = self._switch_info.get(switch, {})
        self.hass.bus.async_fire(
            EVENT_IRRIGATION_COMPLETED,
            {
                "switch_entity": switch,
                "device_id": info.get("device_id"),
                **record,
            },
        )

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(lambda: self._data, 1)

    # ── read accessors (used by the sensor entities) ──
    @callback
    def runs_for(self, switch: str) -> list[dict]:
        """Return the recorded runs for a switch, most recent first."""
        return list(self._data["channels"].get(switch, {}).get("runs", []))

    @callback
    def last_run(self, switch: str) -> dict | None:
        """Most recent recorded run for a switch, if any."""
        runs = self._data["channels"].get(switch, {}).get("runs", [])
        return runs[0] if runs else None

    @callback
    def water_total(self, switch: str) -> float:
        """Cumulative liters recorded for a switch."""
        return float(self._data["channels"].get(switch, {}).get("water_total_l", 0.0))
