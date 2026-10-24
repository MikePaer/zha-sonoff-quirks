# zha-sonoff-quirks

Custom ZHA quirk for the **SONOFF SWV-ZF2** — a **dual-channel** Zigbee
irrigation valve (sold as `SWV-ZF2E` / `SWV-ZF2U` depending on market, same
firmware `0x00001007`).

On top of upstream support, this quirk adds **control of the valve's on-device
autonomous irrigation**: set a duration or a volume, turn the channel's switch
on, and **the valve closes by itself** — even if Home Assistant is offline.

## What it exposes

| Entity | Type | Endpoint | Attribute |
|---|---|---|---|
| Water leak | binary_sensor (moisture) | 1 | `0x500C` bit 1 |
| Water depletion | binary_sensor (problem) | 1 | `0x500C` bit 0 / bit 4 |
| Water usage duration CH1 / CH2 | sensor (minutes) | 1 / 2 | `0x5006` |
| Water usage volume | sensor (litres) | 1 | `0x5007` |
| Irrigating | binary_sensor (running) | 1 | `0x501F` byte 0 |
| Session volume | sensor (litres) | 1 | `0x501F` bytes 19–20 |
| Session elapsed | sensor (seconds) | 1 | `0x501F` current − start |
| Session target duration | sensor (seconds) | 1 | `0x501F` end − start |
| Irrigation mode | select | 1 | `0x501D` byte 0 |
| Irrigation duration | number (0–719 min) | 1 | `0x501D` bytes 1–2 |
| Irrigation volume | number (0–10000 L) | 1 | `0x501D` bytes 8–9 |
| Fail-safe timeout | number (0–719 min) | 1 | `0x501D` bytes 10–11 |
| Irrigation history CH1 / CH2 | sensor (timestamp + `runs` attribute) | — | run log (integration) |
| Irrigation water total CH1 / CH2 | sensor (litres, `total_increasing`) | — | run log (integration) |
| Line name A / B | text (config) | — | entry options (integration) |

> **The configuration is global, not per channel.** `0x501D` only exists on
> endpoint 1 — verified on the device, see [TODO.md](TODO.md) #2. The four
> configuration entities apply to **both** outlets; the two switches stay
> independent, so you choose *which* channel to open, not how to configure each
> one separately.

The four session entities come from `0x501F`, which the device reports
spontaneously **every ~6 seconds while it is running**: that is the source for a
real-time progress bar. The attribute layout and the rest of the reverse
engineering are documented in [TESTS.md](TESTS.md).

The two history entity pairs (0.5.0+) are **not** device attributes: the valve
keeps no run log, so the integration records one server-side. Every run — no
matter how it started: the services below, an automation, a bare
`switch.turn_on`, the physical button, the firmware's own auto-close — is
observed on the channel switches and persisted (last 50 per channel) in
`.storage/zha_sonoff_quirks_history`, surviving restarts. Each record carries
start/end, duration, litres (from the session feed), mode, target, source and
an inferred close reason (`completed` vs `manual_off`). The water-total
sensors plug straight into HA's water dashboard and long-term statistics, and
each recorded run also fires a `zha_sonoff_quirks_irrigation_completed` event
for automations. When both channels run simultaneously the litres cannot be
attributed (the session feed is global) and are recorded as unknown.

> **Startup note.** ZHA loads before this integration on a cold Home
> Assistant start, so the SWV devices can come up without the quirk (all the
> entities above `unavailable`). The integration detects that after startup
> and reloads ZHA once, automatically; you may notice ZHA briefly restarting
> right after boot. This is expected.

The state and usage sensors derive from upstream PR
[zigpy/zha-device-handlers#4993](https://github.com/zigpy/zha-device-handlers/pull/4993);
the `manual_default_settings` (`0x501D`) control comes from the bench-verified
pattern in nglessner's gist, companion to PR #4927, rewritten here without the
event system and adapted to the dual-channel model.

## Installation

### Via HACS (recommended)

1. HACS → ⋮ menu → **Custom repositories** → add
   `https://github.com/SimoneAvogadro/zha-sonoff-quirks` with category
   **Integration**.
2. Search for *Sonoff ZHA*, then **Download**.
3. **Restart Home Assistant.**
4. `Settings → Devices & services → Add integration → Sonoff ZHA` →
   Submit. There is nothing to configure: the integration exists only to make
   the quirk get imported at startup.
5. Continue with the device **Reconfigure** (see below).

### Manual

1. Copy `custom_components/zha_sonoff_quirks/quirks/sonoff_swv_zf2.py` into a
   folder on your Home Assistant, e.g. `/config/custom_zha_quirks/`.

2. Point ZHA at that folder in `configuration.yaml`:

   ```yaml
   zha:
     custom_quirks_path: /config/custom_zha_quirks/
   ```

3. **Restart Home Assistant** — reloading ZHA is not enough, quirks are loaded
   at startup.

### Reconfigure (required either way)

On the SWV-ZF2 device page in ZHA, press **Reconfigure**. The valve is a sleepy
battery device: **wake it first** (press the physical button) and keep it awake
until the reconfigure finishes, otherwise binding and reporting are never
applied and the entities stay `unknown`.

Then check under `Settings → Devices → SWV-ZF2` that the configuration
`number` / `select` entities have appeared.

### Checking that the quirk is active

On the device page, *Zigbee info* should report the applied quirk as
`sonoff_swv_zf2`. Alternatively, in the logs:

```yaml
logger:
  logs:
    zhaquirks: debug
```

## Services (for automations)

The integration bundles the "configure, then open" sequence into two
device-centric services, so automations don't need to know the entity layout:

```yaml
action: zha_sonoff_quirks.irrigation_by_liters
target:
  device_id: abc123...        # target picker (SONOFF SWV-ZF2 only)
data:
  channel: "2"                # radio buttons: Line A / Line B
  liters: 250
  fail_safe_minutes: 60       # optional; leaves the current value if omitted

action: zha_sonoff_quirks.irrigation_by_minutes
target:
  device_id: abc123...
data:
  channel: "1"
  minutes: 15
```

The valve is a standard HA **target**, which is also what lists the two
services in the automation editor's *"By target"* tab when you select the
valve device (HA device actions cannot do that since 2026.8 — see TODO #12).
The target picks the *device* only; the line is always `channel`, and a target
holding more than one valve is rejected. Automations saved before the target existed, with
`device_id` under `data:`, keep working unchanged; the editor shows them with a
"No target set" badge until you re-pick the valve and save.

The *Line* radio shows the names you gave the lines (the two *Line name*
text entities on the device page): `A — Giardino` / `B — Prato davanti`, an
unnamed line as its bare letter. The labels follow a rename within a few
seconds, no page reload needed. This only works while a single SWV valve is
registered: `services.yaml` is one description for every valve and the form
does not know which valve the target holds, so with two or more valves the
radio falls back to the generic *Line A / Line B*.

`channel` takes `"1"`/`"2"`, the values the radio buttons send — and accepts
`"A"`/`"B"` (either case) as aliases for them, so a hand-written automation can
use the letters printed on the valve. The letters are normalised on the way in;
nothing downstream ever sees anything but `"1"` or `"2"`. The **value stays
numeric on purpose**: it is what lands in your automation YAML, and renaming it
would break every existing automation and orphan the history sensors, whose
`unique_id` embeds `_ch1`/`_ch2`. Only the labels changed.

Minutes rather than seconds by design: `0x501D` has 1-minute granularity
(0–719). The service writes mode + target (+ fail-safe when given) and turns
the chosen channel's switch on — the valve closes by itself on-device, so there
is nothing to stop server-side. Stopping early is a plain `switch.turn_off` on
the channel switch. Entities are resolved from the registry by `unique_id`, so
renaming entities does not break the services. Starting a channel that is
already irrigating raises an error instead of silently retargeting the run.

## On-valve schedules

The valve can store up to **6 plans per line** and run them by itself — with
Home Assistant (or the whole network) down. This integration writes them; HA
is the only place they are set and keeps the desired copy.

```yaml
action: zha_sonoff_quirks.set_irrigation_plan
target:
  device_id: abc123...
data:
  channel: "1"
  index: 1                    # slot 0-5; writing a used slot replaces it
  start_time: "05:00"         # valve-local time
  minutes: 11                 # or liters: 50 (+ fail_safe_minutes)
  repeat: daily               # weekdays (+ weekdays: [mon, thu]), odd_days,
                              # even_days, every_n_days (+ interval_days: 3)
  enabled: true               # false keeps it stored but idle
```

Also `remove_irrigation_plan` (channel + index), `rain_delay` (`hours`, 0
cancels; suspends every plan of the valve) and `resync_plans` (rewrites all 6
slots of both lines so the valve matches HA). The valve has no command to list
its plans, so HA cannot read them back: each slot carries a status instead —
*pending*, *on_valve* (the valve acknowledged it, or reported the same
settings) or *failed* (rejected, unanswered, or changed elsewhere — e.g. in
the eWeLink app; resync fixes it).

**Lost "closed" reports.** The valve closes a line on its own timer, but the
report that tells HA can get lost, leaving the switch on in HA. While a line
runs the valve reports every ~6 s, so a line HA shows open that has been silent
for 30 s is read back from the valve (at most once a minute) and the switch
follows the answer. The read never opens or closes anything.

**A program** runs zones back to back from one start time — the usual
"lawn, then boxes, then the backyard" morning. Each setting can be a literal or
a helper entity, and from then on the integration watches those helpers and
rewrites only the plans that change:

```yaml
action: zha_sonoff_quirks.set_program
data:
  start_entity: input_datetime.irrigation_start
  gap_minutes: 1
  slot: 0                     # the program owns this slot on every line it uses
  zones:
    - switch: switch.front_lawn
      minutes_entity: input_number.front_lawn_minutes
      enabled_entity: input_boolean.front_lawn_enabled
    - switch: switch.front_boxes
      minutes: 11
      enabled_entity: input_boolean.front_boxes_enabled
```

A zone switched off is skipped (the later zones move up) and its plan stays on
the valve with the enabled flag cleared; switching it back on restores it.
`clear_program` stops the syncing and leaves the plans on the valves.

Each line gets a **Next scheduled run** sensor: the next start computed from
the plans (and the rain delay), with every slot and its status as attributes,
plus when the valve last asked for the time.

**The valve's clock.** Plans fire on the valve's own local clock, which it
sets by reading the Zigbee Time cluster from the coordinator. ZHA used to drop
that request (the signature has no Time output cluster); the quirk now answers
it the way Zigbee2MQTT does, in HA's configured time zone, and logs one `INFO`
line per answer (`custom_components.zha_sonoff_quirks.quirks.sonoff_swv_zf2`).

**Run history** now says who started each run: `scheduled` (with
`plan_index`, from the valve's session report), `integration` (these
services), `manual` (any other HA action) or `physical` (no HA context: the
valve's button).

The plan bytes are Zigbee2MQTT's, checked byte for byte against its converter
(`tests/test_schedules_z2m_vectors.py`); the official Sonoff ZHA script agrees
on the layout but not on the unit and timestamp fields (see the quirk's
docstring). Not yet verified on a valve: see *Status and known limits*.

## Naming the two lines

The valve's outlets are silk-screened **A** and **B**, and the UI calls them
that. Since 0.9.0 you can give each one the name you actually use for it —
"33 davanti", "33 dietro" — from the **device page → Configuration → Line name
A / Line name B**. The name is stored on the device (in the config entry's
options), so every card showing that valve picks it up: name it once, not once
per dashboard.

The card's own optional *Line A name* / *Line B name* fields still exist and
**win when set** — use them when one dashboard wants a shorter label than the
device-level name. Order of precedence, most specific first:

1. `name_1` / `name_2` in that card's config
2. the *Line name A / B* entity on the device
3. `Line A` / `Line B`

Naming a line never hides its letter. The letter rides **inside** its start
button — `(A ▶)` — so the button itself says which outlet it opens; the name
sits underneath, and the history rows, the pending overlay and the button
tooltip read `A · 33 davanti`. That keeps the pairing in front of you, which is
what you need when the automation editor asks for *Line A*. A line with no name
shows no caption at all: the button already carries the letter.

Two caveats. The name is **not** the switch entity's name, so the logbook, the
history and the voice assistant keep showing whatever the ZHA switch entities
are called — rename those too if you want the name everywhere. And the names
live in the config entry: removing the integration takes them with it.

## Lovelace card

The integration ships and auto-registers `sonoff-valve-card` — same look and
feel as the Tuya irrigation card, adapted to this valve's model: one shared
configuration block (liters or minutes) and **two green start buttons, one per
line**. The running line's button turns into a red stop. Progress comes from
the `0x501F` session feed (device truth: survives a browser refresh and shows
automation-started runs too).

When idle, the "last session" row shows the previous run (start time, duration,
litres) and — from 0.5.0 — a chevron that expands the **run history**: a
scrollable list of the recent runs of both lines merged (start time, line,
duration, litres and an outcome dot), fed by the integration's history sensors.
From 0.7.0 the list matches the Tuya irrigation card in
[tuya-cards-for-ha](https://github.com/simoneavogadro/tuya-cards-for-ha) row for
row, bar the line column. Configs saved with 0.4.0 pick the history entities up
automatically at runtime; no need to reopen the editor.

Add it from the dashboard card picker (*Sonoff Valve (Irrigation)*): pick the
device in the visual editor and it resolves all entities by `unique_id`,
storing them in the card config. Optional fields rename the card and the two
lines. If the card does not appear in the picker, hard-refresh the browser
after the first restart.

From 0.8.0 the lines are labelled **A** and **B** — the letters printed on the
valve — instead of 1 and 2, and from 0.9.0 they take their name from the
device; see [Naming the two lines](#naming-the-two-lines) above. A card saved
before 0.9.0 adopts the line-name entities at runtime, so there is no need to
reopen its editor.

A small **Zigbee signal icon** (three arcs + dot, the same as in the
tuya-cards-for-ha cards) sits left of the battery once the device exposes a
signal-quality entity: ZHA's `LQI` / `RSSI` sensors are *diagnostic* and
**disabled by default** — enable at least one from the device page (Settings →
Devices & Services → the valve → "+N entities not shown"); Zigbee2MQTT's
`linkquality` is created automatically. Nothing to configure: the card finds the
sensor on its device, whatever its entity id, and re-checks once a minute.
LQI is preferred (4 arcs ≥ 200, 3 ≥ 150, 2 ≥ 100, 1 red below); RSSI is the
fallback (−60 / −70 / −80 dBm). Hovering shows the raw values. The icon is
hidden with the battery while the valve is offline.

## Usage: autonomous irrigation

The model is **configure → turn on**:

1. set `Irrigation mode` to `duration` (timed) or `capacity` (by volume);
2. set `Irrigation duration` (minutes) or `Irrigation volume` (litres);
3. set `Fail-safe timeout` as a safety net;
4. turn on the switch for the channel you want to irrigate.

The valve closes on its own once the target is reached. No shutdown automation
is needed, and the cycle completes even with Home Assistant powered off.

Example script:

```yaml
irrigate_garden_250_litres:
  sequence:
    - target: {entity_id: select.swv_zf2_irrigation_mode}
      action: select.select_option
      data: {option: capacity}
    - target: {entity_id: number.swv_zf2_irrigation_volume}
      action: number.set_value
      data: {value: 250}
    - target: {entity_id: number.swv_zf2_fail_safe_timeout}
      action: number.set_value
      data: {value: 60}
    - target: {entity_id: switch.swv_zf2_switch}
      action: switch.turn_on
```

> `zha.set_zigbee_cluster_attribute` **does not work** on `0x501D`: it is a ZCL
> array and the service cannot handle it. Use the entities exposed by the quirk,
> which go through the raw write path implemented in the cluster.

### Channel mapping

`endpoint 1 → switch`, `endpoint 2 → switch_2`. The UI presents them as **line
A** (channel `"1"`, endpoint 1) and **line B** (channel `"2"`, endpoint 2).

**Confirmed on the device on 2026-08-27** (SWV-ZF2E, fw `0x00001009`): opening
line A from the card opens the outlet silk-screened *A*. Nothing in the
protocol guarantees it — endpoint order and silk-screen order are independent —
so if a future firmware or hardware revision ever swapped them, the fix is two
strings: `CHANNEL_LABELS` in `const.py` and `CH_LETTER` in the card. No ids,
entities or stored history would change.

Because the configuration is shared, irrigating the two channels with different
parameters means **serialising**: configure, open CH1, wait for it to close,
reconfigure, open CH2.

## Development

```bash
uv venv --python 3.13 .venv
uv pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
```

Layout:

```
custom_components/zha_sonoff_quirks/
  __init__.py        # imports quirks/ at startup; serves the card; services
  config_flow.py     # one-click flow, no options
  services.py        # irrigation_by_liters / irrigation_by_minutes
  schedules.py       # on-valve plans: store, writes, status, program, services
  schedule_logic.py  # pure plan / program / next-run logic (no HA imports)
  services.yaml      # field selectors only; labels live in strings.json
  icons.json         # per-action icons, so the action picker stays readable
  strings.json       # source of truth; translations/ mirrors its key tree
  quirks/
    sonoff_swv_zf2.py  # THE quirk (single copy; integrity in checksums.sha256)
  www/
    sonoff-valve-card.js  # Lovelace card (auto-registered as a resource)
tests/               # offline tests, no device and no Home Assistant required
  z2m_oracle/        # regenerates fixtures/z2m_vectors.json from Zigbee2MQTT
tests_ha/            # schedules against real Home Assistant (see below)
```

`tests_ha/` needs Home Assistant itself, so it has its own environment
(Python 3.14; CI runs it as the `test-ha` job):

```bash
uv venv --python 3.14 .venv-ha
uv pip install --python .venv-ha "pytest-homeassistant-custom-component==0.13.373" \
  "zigpy==2.3.0" "zha-quirks==2.3.0"
.venv-ha/bin/python -m pytest tests_ha -o asyncio_mode=auto
```

Action names are deliberately **front-loaded with the unit** (*Liters🪣💧
(🌱volume irrigation)* / *Minutes⏰💧 (🌱timed irrigation)*): Home Assistant's
action picker renders one truncated line per action, so a shared prefix makes
the two indistinguishable on mobile. The emoji and the per-action icons in
`icons.json` carry the same distinction without depending on the reader's
language. `tests/test_translations.py` enforces the prefix rule for every
translation.

The quirk is deliberately **self-contained**: it imports nothing from the
integration, so it behaves identically whether loaded via HACS or dropped into
`custom_quirks_path`.

The tests run entirely offline against a fake `ControllerApplication`: no
network I/O, no device needed. The byte layouts are exercised against payloads
captured verbatim from a real valve.

## Status and known limits

See [TODO.md](TODO.md).

**Verified on the device** (fw `0x00001007`): the configuration is global, and
autonomous closing in `capacity` mode works **on both channels** — the valve
closes when the volume target is reached, well before the fail-safe.

**Not yet verified**: `duration` mode as a timed auto-close. The `0x501F` feed
reports it correctly, but the actual closing time has not been measured.

**On-valve schedules — not yet verified on a valve** (written and tested
offline against Zigbee2MQTT's encoder and real Home Assistant): whether the
valve accepts the Time reply and how often it asks; whether it answers a plan
write with the 0x06 status, a Default Response or only the 0x09 report;
whether `0x501F` for line B arrives on endpoint 2; odd/even/every-N-days
semantics as assumed by the next-run sensor.

The usage sensors were moved from `0x501B`/`0x501C` to `0x5006`/`0x5007`: the
former reject `configure_reporting` and left the entities `unknown` forever.
Details in [TESTS.md](TESTS.md).

### Upgrading from 0.1.0

The configuration entities lost their channel suffix, so their `unique_id` and
`entity_id` changed. After upgrading: delete the orphaned `*_ch1` and `*_ch2`
entities from the registry, and update any automation or script that referenced
them.

## License

Apache-2.0, same as `zha-device-handlers`.
