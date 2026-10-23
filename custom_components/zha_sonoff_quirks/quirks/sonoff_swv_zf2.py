"""SONOFF SWV-ZF2 (dual-channel) - Zigbee smart water valve - custom ZHA quirk.

Fonti (entrambe revisionate riga per riga, nessun codice di rete/IO):
- Sensori: PR zigpy/zha-device-handlers#4993 (xiaoliu0001), confermata da terzi
  su SWV-ZF2U fw 0x00001007 (stesso firmware di questo dispositivo).
- Controllo 0x501D manual_default_settings: pattern bench-verified su fw
  0x00001007 (gist nglessner, companion della PR #4927), qui riscritto senza
  event-system per massima compatibilita'.

La configurazione e' GLOBALE, non per canale
-------------------------------------------
Il cluster 0xFC11 e' presente sia sull'endpoint 1 sia sull'endpoint 2, ma
0x501D esiste SOLO sull'endpoint 1: una read sull'endpoint 2 riceve risposta
dal dispositivo senza valore per l'attributo. Verificato sul dispositivo il
2026-08-09 (fw 0x00001007), coerente con Zigbee2MQTT che espone un solo
`manual_default_settings` per questa famiglia.

Le entita' di configurazione sono quindi UNA sola serie, senza suffisso di
canale, e valgono per entrambe le uscite. I due switch (endpoint 1 e 2)
restano indipendenti: si sceglie QUALE canale aprire, non come configurarlo
separatamente.

Modello d'uso "irrigazione a litri / temporizzata":
  1. impostare le entita' di configurazione: modalita' (duration/capacity),
     durata (min) o volume (L), fail-safe (min);
  2. accendere lo switch del canale desiderato: la valvola si chiude
     AUTONOMAMENTE sul dispositivo (funziona anche se HA e' offline).

Formato wire 0x501D (12 byte uint8 array, campi big-endian):
  [0]     irrigation_mode   0=duration 1=capacity 2=duration_with_interval
  [1..2]  irrigation_total_duration  uint16, minuti, 0-719
  [3..4]  interval_irrigation_duration uint16, minuti
  [5..6]  interval_pause    uint16, minuti
  [7]     capacity_unit     0=US gallon 1=litro
  [8..9]  capacity_amount   uint16, 0-10000
  [10..11] fail_safe        uint16, minuti, 0-719

Programmi on-device (fork hydro-duo, 2026-10)
---------------------------------------------
Fino a 6 programmi per linea, salvati e eseguiti dalla valvola anche con HA
spento. Comandi cluster 0xFC11, linea A sull'endpoint 1 e linea B
sull'endpoint 2 (come Sonoff e Zigbee2MQTT):

  0x06 set plan   (28 byte, vedi encode_irrigation_plan) -> risposta 0x06 status
  0x07 remove     (1 byte: indice 0-5)                    -> report 0x0A indice
  0x08 rain delay (uint32 BE, secondi locali dal 2000; 0 = annulla)
  0x09 report di un programma (stessi 28 byte del set)

Il layout e' quello di Zigbee2MQTT (zigbee-herdsman-converters sonoff.ts),
verificato byte per byte in tests/test_schedules_z2m_vectors.py. Lo script
ZHA ufficiale Sonoff concorda sul layout ma non su tre valori: unita' (Sonoff
0=litro, Z2M e questo quirk 1=litro), create_datetime (Sonoff epoca 2000,
Z2M secondi Unix) e campi non usati dalla modalita' (Sonoff 0, Z2M default).
Entrambi sono di Sonoff: lo script ZHA (2026-06-29, = PR zha-device-handlers
#5110, mai unita) precede il firmware 1.0.9; il codice Z2M e' dell'account
ufficiale SONOFF CUBE (CubeZ2mDeveloper) e dal 2026-07-15 gestisce il 1.0.9
e tiene separate la codifica di 0x5021 (0=litro) e quella di 0x501D/piani
(0=gallone US), che lo script confonde. Qui si segue Z2M, tranne il
fail-safe dei programmi a durata (vedi PLAN_DURATION_FAIL_SAFE).

I programmi scattano sull'orologio LOCALE della valvola, che lo chiede con una
Read Attributes sul cluster Time (0x000A). La signature non ha il cluster
Time in uscita e zigpy scartava la richiesta: SWVZF2TimeCluster la aggiunge e
risponde come Z2M (swvznGenTimeCompatResponse).
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, tzinfo
import logging
from typing import Any, Final

from zigpy.quirks import CustomCluster
from zigpy.quirks.v2 import (
    QuirkBuilder,
    ReportingConfig,
    SensorDeviceClass,
    SensorStateClass,
)
from zigpy.quirks.v2.homeassistant import EntityType, UnitOfTime, UnitOfVolume
from zigpy.quirks.v2.homeassistant.binary_sensor import BinarySensorDeviceClass
from zigpy.quirks.v2.homeassistant.number import NumberDeviceClass
import zigpy.types as t
from zigpy.zcl import ClusterType, foundation
from zigpy.zcl.clusters.general import Time
from zigpy.zcl.foundation import BaseAttributeDefs, BaseCommandDefs, ZCLAttributeDef

from zhaquirks import LocalDataCluster

_LOGGER = logging.getLogger(__name__)

MANUAL_SETTINGS_LEN: Final = 12

#: Endpoint che ospita 0x501D. La configurazione e' globale: l'endpoint 2 non
#: risponde alla read dell'attributo (vedi docstring del modulo).
CONFIG_ENDPOINT: Final = 1

#: Lunghezze osservate del payload 0x501F (vedi decode_irrigation_status).
IRRIGATION_STATUS_LEN_PREAMBLE: Final = 15
IRRIGATION_STATUS_LEN_RUNNING: Final = 21


class ValveState(t.enum8):
    """Water valve abnormal state (bitmask su 0x500C)."""

    Normal = 0
    Water_Shortage = 1 << 0
    Water_Leakage = 1 << 1
    Anti_Frost_Alarm = 1 << 2
    Water_Shortage_Channel_2 = 1 << 4
    Water_Shortage_And_Leakage = Water_Shortage | Water_Leakage
    Water_Shortage_And_Frost = Water_Shortage | Anti_Frost_Alarm
    Water_Leakage_And_Frost = Water_Leakage | Anti_Frost_Alarm
    All_Alarms = Water_Shortage | Water_Leakage | Anti_Frost_Alarm


class IrrigationMode(t.enum8):
    """Modalita' di irrigazione manuale (byte 0 di 0x501D)."""

    duration = 0x00
    capacity = 0x01
    duration_with_interval = 0x02


class IrrigationAmountUnit(t.enum8):
    """Unita' capacita' (byte 7 di 0x501D)."""

    us_gallon = 0x00
    liter = 0x01


class ManualDefaultSettingsPayload(
    t.LVList, item_type=t.uint8_t, length_type=t.uint16_t
):
    """Payload array ZCL usato da manual_default_settings (0x501D)."""

    def __init__(self, value=()):
        """Coercizione anche da un wrapper Array ZCL decodificato."""
        if isinstance(value, foundation.Array):
            value = value.value
        super().__init__(value)


def _uint8(value: Any, field_name: str) -> int:
    try:
        int_value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if not 0 <= int_value <= 0xFF:
        raise ValueError(f"{field_name} must be in the range 0..255")
    return int_value


def normalize_manual_default_settings(value: Any) -> ManualDefaultSettingsPayload:
    """Normalizza un payload 0x501D a un array di 12 uint8."""
    if value is None:
        raise ValueError("manual_default_settings cannot be None")
    if isinstance(value, foundation.Array):
        value = value.value
    try:
        payload = ManualDefaultSettingsPayload(value)
    except TypeError as exc:
        raise ValueError(
            "manual_default_settings must be an iterable of 12 bytes"
        ) from exc
    if len(payload) != MANUAL_SETTINGS_LEN:
        raise ValueError(
            f"manual_default_settings must contain exactly {MANUAL_SETTINGS_LEN} "
            f"bytes, got {len(payload)}"
        )
    return ManualDefaultSettingsPayload(
        [_uint8(item, f"manual_default_settings[{i}]") for i, item in enumerate(payload)]
    )


def decode_manual_default_settings(value: Any) -> dict[str, Any]:
    """Decodifica il payload 0x501D in campi nominati."""
    a = list(normalize_manual_default_settings(value))
    try:
        mode = IrrigationMode(a[0])
    except ValueError:
        mode = IrrigationMode.duration
    try:
        unit = IrrigationAmountUnit(a[7])
    except ValueError:
        unit = IrrigationAmountUnit.liter
    return {
        "irrigation_mode": mode,
        "irrigation_total_duration": (a[1] << 8) | a[2],
        "interval_irrigation_duration": (a[3] << 8) | a[4],
        "interval_pause": (a[5] << 8) | a[6],
        "capacity_unit": unit,
        "capacity_amount": (a[8] << 8) | a[9],
        "fail_safe": (a[10] << 8) | a[11],
    }


def pack_manual_default_settings(
    *,
    irrigation_mode: IrrigationMode,
    irrigation_total_duration: int,
    interval_irrigation_duration: int,
    interval_pause: int,
    capacity_unit: IrrigationAmountUnit,
    capacity_amount: int,
    fail_safe: int,
) -> ManualDefaultSettingsPayload:
    """Impacchetta i campi nel payload wire di 12 byte."""
    payload = bytearray(MANUAL_SETTINGS_LEN)
    payload[0] = int(IrrigationMode(irrigation_mode))
    td = int(irrigation_total_duration)
    payload[1] = (td >> 8) & 0xFF
    payload[2] = td & 0xFF
    idur = int(interval_irrigation_duration)
    payload[3] = (idur >> 8) & 0xFF
    payload[4] = idur & 0xFF
    ip = int(interval_pause)
    payload[5] = (ip >> 8) & 0xFF
    payload[6] = ip & 0xFF
    payload[7] = int(IrrigationAmountUnit(capacity_unit))
    vol = int(capacity_amount)
    payload[8] = (vol >> 8) & 0xFF
    payload[9] = vol & 0xFF
    fs = int(fail_safe)
    payload[10] = (fs >> 8) & 0xFF
    payload[11] = fs & 0xFF
    return ManualDefaultSettingsPayload(payload)


class IrrigationSessionState(t.enum8):
    """Byte 0 di 0x501F: fase della sessione di irrigazione."""

    preamble = 0x00
    finished = 0x01
    running = 0x02
    #: Prossima sessione programmata in attesa (Z2M: "standby"). Arriva subito
    #: dopo la fine di una corsa quando c'e' un programma attivo, e descrive la
    #: corsa SUCCESSIVA, non quella appena finita (vedi _sync_progress).
    standby = 0x03


class IrrigationStatusPayload(t.LVList, item_type=t.uint8_t, length_type=t.uint16_t):
    """Payload array ZCL usato da irrigation_status (0x501F)."""

    def __init__(self, value=()):
        """Coercizione anche da un wrapper Array ZCL decodificato."""
        if isinstance(value, foundation.Array):
            value = value.value
        super().__init__(value)


def _u32_be(data: list[int], offset: int) -> int:
    """Legge un uint32 big-endian a partire da `offset`."""
    return (
        (data[offset] << 24)
        | (data[offset + 1] << 16)
        | (data[offset + 2] << 8)
        | data[offset + 3]
    )


def decode_irrigation_status(value: Any) -> dict[str, Any] | None:
    """Decodifica 0x501F, o None se il payload non e' riconoscibile.

    Due varianti osservate sul dispositivo:

      15 byte  header(4) + start(4) + end(4) + coda(3)          -> preambolo
      21 byte  header(4) + start(4) + end(4) + current(4)
               + coda(3) + volume(2)                            -> in corso

    header: [0] stato, [1] riservato, [2] marcatore di formato (sempre 0x01),
    [3] modalita' (0=duration, 1=capacity).

    `start`/`end`/`current` sono un contatore interno del dispositivo, NON un
    orologio: l'ora assoluta e' sfasata di ore. Solo le differenze sono
    affidabili — `end - start` e' la durata obiettivo della sessione e
    `current - start` il tempo trascorso.
    """
    if value is None:
        return None
    if isinstance(value, foundation.Array):
        value = value.value
    try:
        data = [int(item) for item in value]
    except (TypeError, ValueError):
        return None
    if len(data) not in (IRRIGATION_STATUS_LEN_PREAMBLE, IRRIGATION_STATUS_LEN_RUNNING):
        return None

    running = len(data) == IRRIGATION_STATUS_LEN_RUNNING
    start = _u32_be(data, 4)
    end = _u32_be(data, 8)
    try:
        state = IrrigationSessionState(data[0])
    except ValueError:
        state = IrrigationSessionState.preamble
    try:
        mode = IrrigationMode(data[3])
    except ValueError:
        mode = IrrigationMode.duration

    return {
        "session_state": state,
        "session_mode": mode,
        "session_target_duration": max(0, end - start),
        "session_elapsed": max(0, _u32_be(data, 12) - start) if running else 0,
        "session_volume": ((data[19] << 8) | data[20]) if running else 0,
    }


def decode_session_source(value: Any) -> dict[str, Any] | None:
    """Da 0x501F, chi ha avviato la sessione: programma (e quale) o manuale.

    Layout Zigbee2MQTT (irrigationScheduleStatus): byte 1 = indice del
    programma, byte 2 = tipo (0 automatico, 1 manuale). Il "marcatore di
    formato sempre 0x01" di decode_irrigation_status e' questo byte 2: nelle
    catture c'erano solo corse manuali. Separata da decode_irrigation_status
    per non toccare le entita' esistenti.
    """
    if value is None:
        return None
    if isinstance(value, foundation.Array):
        value = value.value
    try:
        data = [int(item) for item in value]
    except (TypeError, ValueError):
        return None
    if len(data) not in (IRRIGATION_STATUS_LEN_PREAMBLE, IRRIGATION_STATUS_LEN_RUNNING):
        return None
    return {
        "session_state": data[0],
        "session_plan_index": data[1],
        "session_scheduled": data[2] == SESSION_TYPE_SCHEDULED,
    }


#: Byte 2 di 0x501F (scheduleTypeMap di Z2M).
SESSION_TYPE_SCHEDULED: Final = 0x00
SESSION_TYPE_MANUAL: Final = 0x01


# --------------------------------------------------------------------------- #
# Programmi on-device: payload dei comandi 0x06 (set) e 0x09 (report)
# --------------------------------------------------------------------------- #

PLAN_PAYLOAD_LEN: Final = 28
PLAN_SLOTS: Final = 6
#: 2000-01-01T00:00:00Z, base dei tempi "locali" del dispositivo.
_Y2K: Final = datetime(2000, 1, 1, tzinfo=UTC)

#: Valori che Zigbee2MQTT manda nei campi che la modalita' scelta non usa
#: (default di parseIntWithDefault in sonoff.ts). Replicandoli, i byte inviati
#: sono identici a quelli di Z2M; lo script Sonoff manda invece 0.
PLAN_DEFAULT_IRRIGATION_DURATION: Final = 2
PLAN_DEFAULT_INTERVAL_DURATION: Final = 3
PLAN_DEFAULT_AMOUNT: Final = 30
PLAN_DEFAULT_FAIL_SAFE: Final = 10
#: Fail-safe dei programmi a DURATA. Eccezione al default Z2M: lo script
#: Sonoff manda 0 nei programmi a durata (il fail-safe e' un limite della
#: modalita' a volume), e un default di 10 min dentro un programma da 11
#: rischierebbe di chiuderlo prima, se il firmware lo applicasse. Z2M accetta
#: 0 (range 0-719): i byte restano quelli che Z2M manda con fail_safe: 0.
PLAN_DURATION_FAIL_SAFE: Final = 0


class PlanLoopType(t.enum8):
    """Ricorrenza di un programma (byte alto del loop type)."""

    odd_days = 0x00
    even_days = 0x01
    day_interval = 0x02
    weekdays = 0x03


#: Bit di ciascun giorno nella maschera weekdays (bit0 = domenica).
WEEKDAY_BITS: Final = {
    "sunday": 0x01,
    "monday": 0x02,
    "tuesday": 0x04,
    "wednesday": 0x08,
    "thursday": 0x10,
    "friday": 0x20,
    "saturday": 0x40,
}
EVERY_DAY_MASK: Final = 0x7F


@dataclass(frozen=True)
class IrrigationPlan:
    """Un programma, nei campi e nelle unita' del payload di 28 byte."""

    index: int
    enabled: bool
    loop_type: PlanLoopType
    #: Maschera dei giorni (weekdays), giorni di intervallo (day_interval), 0
    #: altrimenti.
    loop_value: int
    #: Data locale da cui il programma vale.
    enable_date: date
    #: Ora di partenza in secondi dalla mezzanotte LOCALE.
    start_seconds: int
    mode: IrrigationMode = IrrigationMode.duration
    #: Minuti totali (duration e duration_with_interval).
    total_duration: int = 10
    irrigation_duration: int = PLAN_DEFAULT_IRRIGATION_DURATION
    interval_duration: int = PLAN_DEFAULT_INTERVAL_DURATION
    amount_unit: IrrigationAmountUnit = IrrigationAmountUnit.liter
    #: Volume (capacity), nell'unita' amount_unit.
    amount: int = PLAN_DEFAULT_AMOUNT
    fail_safe: int = PLAN_DEFAULT_FAIL_SAFE
    #: Secondi Unix UTC (Z2M; lo script Sonoff usa l'epoca 2000).
    created: int = 0

    def same_settings(self, other: "IrrigationPlan") -> bool:
        """Uguale a `other` a meno della data di creazione."""
        return replace(self, created=0) == replace(other, created=0)


def _ranged(value: Any, low: int, high: int, field_name: str) -> int:
    try:
        int_value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if not low <= int_value <= high:
        raise ValueError(f"{field_name} must be in the range {low}..{high}")
    return int_value


def _date_to_device(value: date) -> int:
    """Mezzanotte della data, come se fosse UTC, in secondi dal 2000 (Z2M)."""
    midnight = datetime(value.year, value.month, value.day, tzinfo=UTC)
    return int((midnight - _Y2K).total_seconds())


def encode_irrigation_plan(plan: IrrigationPlan) -> bytes:
    """Payload di 28 byte del comando 0x06, campi big-endian.

    [0]      indice 0-5          [1]      abilitato 0/1
    [2]      loop type           [3]      maschera giorni / giorni intervallo
    [4..7]   data di validita'   [8]      modalita' (come 0x501D)
    [9..12]  partenza, s dalla mezzanotte locale
    [13..14] durata totale min   [15..16] durata irrigazione min
    [17..18] durata intervallo   [19]     unita' (0 gal US, 1 litro)
    [20..21] volume              [22..23] fail-safe min
    [24..27] creazione, secondi Unix UTC
    """
    loop_type = PlanLoopType(plan.loop_type)
    if loop_type == PlanLoopType.day_interval:
        loop_value = _ranged(plan.loop_value, 1, 30, "loop_value (interval days)")
    elif loop_type == PlanLoopType.weekdays:
        loop_value = _ranged(plan.loop_value, 0, EVERY_DAY_MASK, "loop_value (weekday mask)")
    elif loop_type in (PlanLoopType.odd_days, PlanLoopType.even_days):
        loop_value = 0
    else:
        raise ValueError(f"unsupported loop_type {plan.loop_type!r}")
    enable = _date_to_device(plan.enable_date)
    if not 0 <= enable <= 0xFFFFFFFF:
        raise ValueError("enable_date must not be before 2000-01-01")

    payload = bytearray()
    payload.append(_ranged(plan.index, 0, PLAN_SLOTS - 1, "index"))
    payload.append(1 if plan.enabled else 0)
    payload += bytes((int(loop_type), loop_value))
    payload += enable.to_bytes(4, "big")
    payload.append(int(IrrigationMode(plan.mode)))
    payload += _ranged(plan.start_seconds, 0, 86399, "start_seconds").to_bytes(4, "big")
    payload += _ranged(plan.total_duration, 0, 719, "total_duration").to_bytes(2, "big")
    payload += _ranged(plan.irrigation_duration, 1, 60, "irrigation_duration").to_bytes(2, "big")
    payload += _ranged(plan.interval_duration, 1, 60, "interval_duration").to_bytes(2, "big")
    payload.append(int(IrrigationAmountUnit(plan.amount_unit)))
    payload += _ranged(plan.amount, 1, 10000, "amount").to_bytes(2, "big")
    payload += _ranged(plan.fail_safe, 0, 719, "fail_safe").to_bytes(2, "big")
    payload += _ranged(plan.created, 0, 0xFFFFFFFF, "created").to_bytes(4, "big")
    assert len(payload) == PLAN_PAYLOAD_LEN
    return bytes(payload)


def decode_irrigation_plan(data: bytes) -> IrrigationPlan | None:
    """Decodifica il payload di un report 0x09 (o di un set 0x06)."""
    if data is None or len(data) < PLAN_PAYLOAD_LEN:
        return None
    d = bytes(data)
    enable = _Y2K + timedelta(seconds=int.from_bytes(d[4:8], "big"))
    return IrrigationPlan(
        index=d[0],
        enabled=d[1] == 1,
        loop_type=PlanLoopType(d[2]),
        loop_value=d[3],
        enable_date=enable.date(),
        start_seconds=int.from_bytes(d[9:13], "big"),
        mode=IrrigationMode(d[8]),
        total_duration=int.from_bytes(d[13:15], "big"),
        irrigation_duration=int.from_bytes(d[15:17], "big"),
        interval_duration=int.from_bytes(d[17:19], "big"),
        amount_unit=IrrigationAmountUnit(d[19]),
        amount=int.from_bytes(d[20:22], "big"),
        fail_safe=int.from_bytes(d[22:24], "big"),
        created=int.from_bytes(d[24:28], "big"),
    )


# --------------------------------------------------------------------------- #
# Orologio: valori della risposta Time e tempi "locali dal 2000"
# --------------------------------------------------------------------------- #

#: Fuso usato per l'ora della valvola. None = fuso del processo; l'integrazione
#: lo imposta al fuso configurato in HA (set_local_timezone).
_local_tz: tzinfo | None = None


def set_local_timezone(tz: tzinfo | None) -> None:
    """Imposta il fuso con cui rispondere alla valvola e codificare gli orari."""
    global _local_tz  # noqa: PLW0603
    _local_tz = tz


def local_timezone() -> tzinfo:
    """Fuso in uso: quello impostato, altrimenti quello del processo."""
    return _local_tz or datetime.now().astimezone().tzinfo or UTC


def time_cluster_values(now: datetime, tz: tzinfo) -> dict[str, int]:
    """Attributi Time come li manda Zigbee2MQTT a questa famiglia di valvole.

    getTimeClusterAttributes di zigbee-herdsman, con DstStart/DstEnd forzati
    a 0 da swvznGenTimeCompatResponse ("the device's Zigbee behavior is a bit
    non-standard"): TimeZone e' l'offset STANDARD, LocalTime l'ora locale
    effettiva, DstShift lo spostamento dell'ora legale dell'anno (anche
    d'inverno), 0 dove l'ora legale non c'e'.
    """
    now = now.astimezone(UTC)
    utc_time = int((now - _Y2K).total_seconds())
    offset_now = now.astimezone(tz).utcoffset() or timedelta(0)
    offsets = [
        datetime(now.year, month, 1, 12, tzinfo=UTC).astimezone(tz).utcoffset()
        or timedelta(0)
        for month in (1, 7)
    ]
    standard = min(offsets) if offsets[0] != offsets[1] else offset_now
    return {
        "time": utc_time,
        "time_zone": int(standard.total_seconds()),
        "local_time": utc_time + int(offset_now.total_seconds()),
        "dst_start": 0,
        "dst_end": 0,
        "dst_shift": int(abs(offsets[0] - offsets[1]).total_seconds()),
    }


def device_local_seconds(moment: datetime, tz: tzinfo) -> int:
    """Istante -> secondi locali dal 2000, la base dei tempi del dispositivo."""
    offset = moment.astimezone(tz).utcoffset() or timedelta(0)
    return int((moment.astimezone(UTC) - _Y2K + offset).total_seconds())


def device_local_to_datetime(value: int, tz: tzinfo) -> datetime:
    """Secondi locali dal 2000 (es. tempi di 0x501F) -> datetime nel fuso tz."""
    naive_local = datetime(2000, 1, 1) + timedelta(seconds=int(value))
    return naive_local.replace(tzinfo=tz)


def encode_rain_delay(end: datetime | None, tz: tzinfo) -> bytes:
    """Payload del comando 0x08: fine del rinvio, 0 per annullarlo."""
    value = 0 if end is None else device_local_seconds(end, tz)
    return _ranged(value, 0, 0xFFFFFFFF, "rain delay end").to_bytes(4, "big")


# --------------------------------------------------------------------------- #
# Notifiche verso l'integrazione
# --------------------------------------------------------------------------- #

#: Callback registrati dall'integrazione; ricevono un dict con "type", "ieee",
#: "endpoint_id" e i campi dell'evento. Chiamati nel loop di zigpy/HA.
_schedule_listeners: list[Callable[[dict[str, Any]], None]] = []


def add_schedule_listener(
    listener: Callable[[dict[str, Any]], None],
) -> Callable[[], None]:
    """Registra un listener degli eventi dei programmi; ritorna la rimozione."""
    _schedule_listeners.append(listener)

    def _remove() -> None:
        if listener in _schedule_listeners:
            _schedule_listeners.remove(listener)

    return _remove


def _notify(cluster: CustomCluster, event_type: str, **fields: Any) -> None:
    event = {
        "type": event_type,
        "ieee": str(cluster.endpoint.device.ieee),
        "endpoint_id": cluster.endpoint.endpoint_id,
        **fields,
    }
    for listener in list(_schedule_listeners):
        try:
            listener(event)
        except Exception:  # noqa: BLE001 - un listener rotto non ferma zigpy
            _LOGGER.exception("SWV-ZF2 schedule listener failed on %s", event)


class RawPayload(bytes):
    """Byte grezzi fino a fine frame: i comandi 0x06-0x0A non hanno lunghezza."""

    def serialize(self) -> bytes:
        """Byte cosi' come sono."""
        return bytes(self)

    @classmethod
    def deserialize(cls, data: bytes) -> tuple["RawPayload", bytes]:
        """Consuma tutto il resto del frame."""
        return cls(data), b""


#: Id dei comandi del cluster 0xFC11 usati dai programmi.
CMD_PLAN_SET: Final = 0x06
CMD_PLAN_REMOVE: Final = 0x07
CMD_RAIN_DELAY: Final = 0x08
CMD_PLAN_REPORT: Final = 0x09
CMD_PLAN_REMOVED: Final = 0x0A


class SWVZF2Cluster(CustomCluster):
    """Cluster privato Sonoff 0xFC11 per SWV-ZF2 (per endpoint/canale)."""

    cluster_id = 0xFC11
    ep_attribute = "swvzf2_cluster"

    class ServerCommandDefs(BaseCommandDefs):
        """Comandi verso la valvola (programmi on-device)."""

        irrigation_plan_set = foundation.ZCLCommandDef(
            id=CMD_PLAN_SET,
            schema={"payload": RawPayload},
            is_manufacturer_specific=False,
        )
        irrigation_plan_remove = foundation.ZCLCommandDef(
            id=CMD_PLAN_REMOVE,
            schema={"plan_index": t.uint8_t},
            is_manufacturer_specific=False,
        )
        rain_delay = foundation.ZCLCommandDef(
            id=CMD_RAIN_DELAY,
            schema={"payload": RawPayload},
            is_manufacturer_specific=False,
        )

    class ClientCommandDefs(BaseCommandDefs):
        """Comandi dalla valvola: esito del set, report e rimozione."""

        irrigation_plan_set_rsp = foundation.ZCLCommandDef(
            id=CMD_PLAN_SET,
            schema={"payload": RawPayload},
            is_manufacturer_specific=False,
        )
        irrigation_plan_report = foundation.ZCLCommandDef(
            id=CMD_PLAN_REPORT,
            schema={"payload": RawPayload},
            is_manufacturer_specific=False,
        )
        irrigation_plan_removed_report = foundation.ZCLCommandDef(
            id=CMD_PLAN_REMOVED,
            schema={"payload": RawPayload},
            is_manufacturer_specific=False,
        )

    class AttributeDefs(BaseAttributeDefs):
        """Attributi del cluster privato Sonoff."""

        # Il dispositivo espone 0x500C come uint8, non come enum8: dichiararlo
        # enum8 fa fallire la configure_reporting con INVALID_DATA_TYPE, e i
        # binary sensor di allarme non riportano mai. Il tipo Python resta
        # ValveState per comodita' di lettura dei bit.
        water_valve_state = ZCLAttributeDef(
            id=0x500C,
            type=ValveState,
            zcl_type=foundation.DataTypeId.uint8,
            manufacturer_code=None,
        )
        # Contatore di consumo live, per canale. Riportato spontaneamente ogni
        # ~60 s durante una corsa; NON accetta configure_reporting.
        valve_open_duration = ZCLAttributeDef(
            id=0x5006,
            type=t.uint32_t,
            manufacturer_code=None,
        )
        irrigation_volume = ZCLAttributeDef(
            id=0x5007,
            type=t.uint32_t,
            manufacturer_code=None,
        )
        # Durata irrigazione oraria (minuti). Presente ma non configurabile per
        # il reporting (UNSUPPORTED_ATTRIBUTE) e riportata di rado: le entita'
        # usano 0x5006/0x5007.
        water_usage_duration = ZCLAttributeDef(
            id=0x501C,
            type=t.uint32_t,
            manufacturer_code=None,
        )
        # Volume irrigazione orario (litri). Stesse limitazioni di 0x501C.
        water_usage_volume = ZCLAttributeDef(
            id=0x501B,
            type=t.uint32_t,
            manufacturer_code=None,
        )
        # Stato della sessione di irrigazione in corso (array, 15 o 21 byte).
        # Riportato spontaneamente ogni ~6 s mentre la valvola eroga.
        irrigation_status = ZCLAttributeDef(
            id=0x501F,
            type=IrrigationStatusPayload,
            zcl_type=foundation.DataTypeId.array,
            manufacturer_code=None,
        )
        # Fine del rinvio pioggia (secondi locali dal 2000, 0 = nessuno).
        # Dichiarato per poterlo rileggere dopo il comando 0x08.
        rain_delay_end = ZCLAttributeDef(
            id=0x5014,
            type=t.uint32_t,
            manufacturer_code=None,
        )
        # Unita' di misura dell'acqua (fw >= 1.0.9): 0 litro, 1 gallone US,
        # 2 gallone imperiale. E' una codifica DIVERSA dal byte unita' di
        # 0x501D e dei programmi (0 gallone US, 1 litro): vedi Z2M,
        # SWVZNEUnifiedWaterFlowUnitByCode. Dichiarato solo per la lettura.
        unit_of_water_flow = ZCLAttributeDef(
            id=0x5021,
            type=t.uint8_t,
            manufacturer_code=None,
        )
        # Impostazioni irrigazione manuale singola (array 12 byte)
        manual_default_settings = ZCLAttributeDef(
            id=0x501D,
            type=ManualDefaultSettingsPayload,
            zcl_type=foundation.DataTypeId.array,
            manufacturer_code=None,
        )

    def _repair_malformed_array_read_response(self, data: bytes) -> bytes | None:
        """Ripara le read-response 0x501D con element-type array duplicato.

        Alcune firmware della famiglia SWV rispondono al read di un attributo
        array con DataTypeId.array ripetuto anche come element type; zigpy non
        riesce a deserializzare. Riscriviamo l'element type in uint8.
        """
        try:
            hdr, payload = foundation.ZCLHeader.deserialize(data)
        except Exception:
            return None
        if (
            hdr.frame_control.frame_type != foundation.FrameType.GLOBAL_COMMAND
            or hdr.direction != foundation.Direction.Server_to_Client
            or hdr.command_id != foundation.GeneralCommand.Read_Attributes_rsp
        ):
            return None
        malformed_prefix = (
            self.AttributeDefs.manual_default_settings.id.serialize()
            + foundation.Status.SUCCESS.serialize()
            + foundation.DataTypeId.array.serialize()
            + foundation.DataTypeId.array.serialize()
        )
        repaired_prefix = (
            self.AttributeDefs.manual_default_settings.id.serialize()
            + foundation.Status.SUCCESS.serialize()
            + foundation.DataTypeId.array.serialize()
            + foundation.DataTypeId.uint8.serialize()
        )
        if malformed_prefix not in payload:
            return None
        return hdr.serialize() + payload.replace(malformed_prefix, repaired_prefix)

    def deserialize(self, data: bytes):
        """Deserializza, riparando i frame array malformati quando possibile."""
        try:
            return super().deserialize(data)
        except Exception as exc:
            repaired = self._repair_malformed_array_read_response(data)
            if repaired is not None:
                return super().deserialize(repaired)
            self.warning(
                "Failed to deserialize SWV-ZF2 cluster frame on endpoint %s: %s; raw=%s",
                self.endpoint.endpoint_id,
                exc,
                data.hex(" "),
            )
            raise

    def _sync_manual_config(self, value: Any) -> None:
        """Propaga un payload 0x501D al cluster locale di configurazione."""
        local = getattr(self.endpoint, "swvzf2_manual_config", None)
        if local is None:
            return
        try:
            local.update_from_manual_default_settings(value)
        except ValueError as exc:
            self.warning(
                "Ignoring invalid manual_default_settings payload %r: %s", value, exc
            )

    def _sync_progress(self, value: Any) -> None:
        """Propaga un payload 0x501F al cluster locale di avanzamento.

        Il cluster di avanzamento vive solo sull'endpoint di configurazione, ma
        0x501F puo' arrivare da entrambi gli endpoint: si instrada sempre li'.
        """
        try:
            endpoint = self.endpoint.device.endpoints[CONFIG_ENDPOINT]
        except (AttributeError, KeyError):
            return
        local = getattr(endpoint, "swvzf2_progress", None)
        if local is None:
            return
        decoded = decode_irrigation_status(value)
        if decoded is None:
            self.warning("Ignoring unrecognised irrigation_status payload %r", value)
            return
        source = decode_session_source(value)
        if source is not None and source["session_state"] == IrrigationSessionState.standby:
            # Annuncio del prossimo programma (osservato il 2026-10-22, 9 ms
            # dopo il report finale): passarlo al cluster locale azzererebbe
            # il volume della corsa appena chiusa, che lo storico legge dopo
            # 12 s, e lo farebbe sembrare l'avvio di una sessione programmata.
            raw = value.value if isinstance(value, foundation.Array) else value
            _notify(
                self,
                "next_session",
                plan_index=source["session_plan_index"],
                start=_u32_be([int(b) for b in raw], 4),
            )
            return
        local.update_from_irrigation_status(decoded)
        if source is not None:
            # Endpoint del report = linea, per attribuire la sessione nello
            # storico (Programma #n o manuale).
            _notify(self, "session", **source)

    # ── Programmi on-device ──

    async def _send_schedule_command(
        self, command_id: int, *, expect_reply: bool, **kwargs: Any
    ) -> int | None:
        """Invia un comando dei programmi; ritorna lo status (0 = ok) o None.

        Se la risposta della valvola (0x06 con lo status, o un Default
        Response) porta lo stesso TSN della richiesta, zigpy la consegna qui
        come risultato e NON a handle_cluster_request: lo status va letto da
        entrambe le parti. None = nessuno status (timeout o comando senza
        risposta): a confermare il set restano il report 0x09.
        """
        try:
            rsp = await self.command(
                command_id, manufacturer=None, expect_reply=expect_reply, **kwargs
            )
        except TimeoutError:
            self.debug("No reply to command 0x%02X", command_id)
            return None
        payload = getattr(rsp, "payload", None)
        if isinstance(payload, bytes | bytearray):
            if payload:
                self._handle_schedule_frame(CMD_PLAN_SET, bytes(payload))
                return payload[0]
            return None
        status = getattr(rsp, "status", None)
        return int(status) if status is not None else None

    async def set_irrigation_plan(self, plan: IrrigationPlan) -> int | None:
        """Scrive un programma su questa linea (endpoint 1 = A, 2 = B)."""
        payload = encode_irrigation_plan(plan)
        _LOGGER.info(
            "SWV-ZF2 %s ep%s: writing plan %s: %s",
            self.endpoint.device.ieee,
            self.endpoint.endpoint_id,
            plan.index,
            payload.hex(),
        )
        # Come Z2M: Default Response richiesto.
        return await self._send_schedule_command(
            CMD_PLAN_SET, expect_reply=True, payload=RawPayload(payload)
        )

    async def remove_irrigation_plan(self, index: int) -> int | None:
        """Cancella un programma; ritorna lo status del Default Response.

        Il report 0x0A non arriva per le cancellazioni via Zigbee (Z2M lo
        chiama "local irrigation plan removed report": e' per quelle fatte
        sul dispositivo/app; verificato il 2026-10-22, nessun 0x0A in 30 s).
        La conferma e' quindi il Default Response, che si chiede e si attende.
        """
        index = _ranged(index, 0, PLAN_SLOTS - 1, "index")
        _LOGGER.info(
            "SWV-ZF2 %s ep%s: removing plan %s",
            self.endpoint.device.ieee,
            self.endpoint.endpoint_id,
            index,
        )
        return await self._send_schedule_command(
            CMD_PLAN_REMOVE, expect_reply=True, plan_index=t.uint8_t(index)
        )

    async def set_rain_delay(self, end: datetime | None) -> None:
        """Sospende i programmi fino a `end` (None annulla). Globale, endpoint 1."""
        payload = encode_rain_delay(end, local_timezone())
        _LOGGER.info(
            "SWV-ZF2 %s: rain delay until %s (%s)",
            self.endpoint.device.ieee,
            end,
            payload.hex(),
        )
        await self._send_schedule_command(
            CMD_RAIN_DELAY, expect_reply=False, payload=RawPayload(payload)
        )

    def _handle_schedule_frame(self, command_id: int, payload: bytes) -> None:
        """Esito del set (0x06), report (0x09) o rimozione (0x0A) dalla valvola."""
        if command_id == CMD_PLAN_SET:
            status = payload[0] if payload else None
            if status not in (None, 0):
                self.warning("Irrigation plan write rejected, status=%s", status)
            _notify(self, "plan_ack", status=status)
        elif command_id == CMD_PLAN_REPORT:
            plan = decode_irrigation_plan(payload)
            if plan is None:
                self.warning("Ignoring short irrigation plan report %s", payload.hex())
                return
            _notify(self, "plan_report", plan=plan)
        elif command_id == CMD_PLAN_REMOVED and payload:
            _notify(self, "plan_removed", index=payload[0])

    def handle_cluster_request(
        self,
        hdr: foundation.ZCLHeader,
        args: Any,
        *,
        dst_addressing: Any = None,
    ) -> None:
        """Intercetta i comandi dei programmi in arrivo dalla valvola."""
        if hdr.direction == foundation.Direction.Server_to_Client and hdr.command_id in (
            CMD_PLAN_SET,
            CMD_PLAN_REPORT,
            CMD_PLAN_REMOVED,
        ):
            payload = getattr(args, "payload", b"") or b""
            _LOGGER.info(
                "SWV-ZF2 %s ep%s: received 0x%02X %s",
                self.endpoint.device.ieee,
                self.endpoint.endpoint_id,
                hdr.command_id,
                bytes(payload).hex(),
            )
            self._handle_schedule_frame(hdr.command_id, bytes(payload))
        super().handle_cluster_request(hdr, args, dst_addressing=dst_addressing)

    def _update_attribute(self, attrid, value):
        """Intercetta gli attributi array per evitare errori appdb.

        Entrambi sono di tipo Array, che l'appdb non sa salvare: verrebbe
        loggato un errore a ogni report. Vengono quindi decodificati nei
        cluster locali e consumati qui.

        - 0x501D: configurazione dell'irrigazione manuale.
        - 0x501F: stato della sessione in corso, riportato ogni ~6 s mentre la
          valvola eroga. E' l'unica sorgente di avanzamento in tempo reale.
        """
        if attrid == self.AttributeDefs.manual_default_settings.id:
            self._sync_manual_config(value)
            return
        if attrid == self.AttributeDefs.irrigation_status.id:
            self._sync_progress(value)
            return
        super()._update_attribute(attrid, value)

    async def apply_custom_configuration(self, *args, **kwargs):
        """Legge 0x501D al (re)configure per inizializzare le entita'."""
        try:
            await self.read_attributes(
                [self.AttributeDefs.manual_default_settings.id]
            )
        except Exception as exc:
            self.warning(
                "Unable to read manual_default_settings during configuration: %s; "
                "continuing",
                exc,
            )

    async def read_attributes(self, attributes, *args, **kwargs):
        """Legge gli attributi propagando 0x501D al cluster locale."""
        result = await super().read_attributes(attributes, *args, **kwargs)
        success = result[0] if result else {}
        for key, value in dict(success).items():
            attr_id = key if isinstance(key, int) else getattr(key, "id", None)
            if attr_id == self.AttributeDefs.manual_default_settings.id or key == (
                self.AttributeDefs.manual_default_settings.name
            ):
                self._sync_manual_config(value)
        return result

    async def write_attributes(
        self,
        attributes: dict[str | int | foundation.ZCLAttributeDef, Any],
        manufacturer: int | None = None,
        **kwargs,
    ) -> list[list[foundation.WriteAttributesStatusRecord]]:
        """Scrive gli attributi instradando 0x501D sul path raw per array."""
        other_attributes: dict[Any, Any] = {}
        manual_value: Any | None = None

        for attr, value in attributes.items():
            attr_def = self.find_attribute(attr)
            if attr_def.id == self.AttributeDefs.manual_default_settings.id:
                manual_value = value
            else:
                other_attributes[attr] = value

        results: list[foundation.WriteAttributesStatusRecord] = []

        if other_attributes:
            results.extend(
                (
                    await super().write_attributes(
                        other_attributes, manufacturer, **kwargs
                    )
                )[0]
            )

        if manual_value is not None:
            payload = normalize_manual_default_settings(manual_value)
            zcl_attr = foundation.Attribute(
                self.AttributeDefs.manual_default_settings.id,
                foundation.TypeValue(),
            )
            zcl_attr.value.type = foundation.DataTypeId.array
            zcl_attr.value.value = foundation.Array(
                type=foundation.DataTypeId.uint8,
                value=payload,
            )
            raw_result = await self.write_attributes_raw([zcl_attr], manufacturer)
            record = None
            if raw_result and isinstance(raw_result[0], list) and raw_result[0]:
                record = raw_result[0][0]
            status = getattr(record, "status", foundation.Status.SUCCESS)
            results.append(
                foundation.WriteAttributesStatusRecord(
                    status=status,
                    attrid=self.AttributeDefs.manual_default_settings.id,
                )
            )
            if status == foundation.Status.SUCCESS:
                # Aggiorna il cluster locale con quanto appena scritto.
                self._sync_manual_config(payload)

        return [results]


class SWVZF2ManualConfigCluster(LocalDataCluster):
    """Cluster locale che espone 0x501D come attributi individuali.

    Le scritture su singoli attributi vengono unite allo stato in cache,
    reimpacchettate nei 12 byte wire e inoltrate a SWVZF2Cluster dello
    stesso endpoint come un'unica write di manual_default_settings.
    """

    cluster_id = 0xFBFC
    ep_attribute = "swvzf2_manual_config"

    class AttributeDefs(BaseAttributeDefs):
        """Attributi virtuali, uno per campo di manual_default_settings."""

        irrigation_mode: Final = ZCLAttributeDef(id=0x0000, type=IrrigationMode)
        irrigation_total_duration: Final = ZCLAttributeDef(
            id=0x0001, type=t.uint16_t
        )
        interval_irrigation_duration: Final = ZCLAttributeDef(
            id=0x0002, type=t.uint16_t
        )
        interval_pause: Final = ZCLAttributeDef(id=0x0003, type=t.uint16_t)
        capacity_unit: Final = ZCLAttributeDef(
            id=0x0004, type=IrrigationAmountUnit
        )
        capacity_amount: Final = ZCLAttributeDef(id=0x0005, type=t.uint16_t)
        fail_safe: Final = ZCLAttributeDef(id=0x0006, type=t.uint16_t)

    _SETTING_ATTRS = (
        AttributeDefs.irrigation_mode,
        AttributeDefs.irrigation_total_duration,
        AttributeDefs.interval_irrigation_duration,
        AttributeDefs.interval_pause,
        AttributeDefs.capacity_unit,
        AttributeDefs.capacity_amount,
        AttributeDefs.fail_safe,
    )

    def update_from_manual_default_settings(self, value: Any) -> None:
        """Aggiorna gli attributi locali da un payload 0x501D."""
        decoded = decode_manual_default_settings(value)
        for name, attr_value in decoded.items():
            self._update_attribute(self.find_attribute(name), attr_value)

    def _current_settings(self) -> dict[str, Any]:
        settings: dict[str, Any] = {}
        for attr_def in self._SETTING_ATTRS:
            value = self.get(attr_def.id)
            if value is None:
                raise ValueError(
                    "manual_default_settings are not initialized yet; "
                    "read the device first"
                )
            settings[attr_def.name] = value
        return settings

    async def write_attributes(
        self,
        attributes: dict[str | int | ZCLAttributeDef, Any],
        *args,
        **kwargs,
    ) -> list:
        """Unisce le modifiche allo stato in cache e scrive il payload."""
        try:
            settings = self._current_settings()
        except ValueError:
            await self.endpoint.swvzf2_cluster.read_attributes(
                [SWVZF2Cluster.AttributeDefs.manual_default_settings.id]
            )
            try:
                settings = self._current_settings()
            except ValueError as exc:
                # La read e' andata a buon fine come richiesta ma non ha
                # restituito 0x501D: il messaggio "leggi prima il dispositivo"
                # sarebbe fuorviante, l'abbiamo appena fatto.
                raise ValueError(
                    "the device did not return manual_default_settings (0x501D) "
                    f"on endpoint {self.endpoint.endpoint_id}; irrigation "
                    "settings cannot be written"
                ) from exc

        written_names: set[str] = set()
        for attr, value in attributes.items():
            attr_name = self.find_attribute(attr).name
            written_names.add(attr_name)
            if attr_name == "irrigation_mode":
                settings[attr_name] = IrrigationMode(int(value))
            elif attr_name == "capacity_unit":
                settings[attr_name] = IrrigationAmountUnit(int(value))
            else:
                settings[attr_name] = int(value)

        # L'entita' number del volume e' dichiarata in LITRI, ma il dispositivo
        # esce di fabbrica con capacity_unit = us_gallon: il payload 0x501D
        # reale letto il 2026-08-09 (fw 0x00001007) e'
        # [0,0,5,0,0,0,0,0,0,1,0,1], con byte 7 = 0 (gallone US). Il merge con
        # la cache conserverebbe quindi il gallone quando l'utente scrive solo
        # il volume: la UI direbbe litri e il dispositivo erogherebbe galloni.
        # Se il volume viene scritto senza un'unita' esplicita, si forza il
        # litro; chi scrive capacity_unit di proposito resta rispettato.
        if (
            self.AttributeDefs.capacity_amount.name in written_names
            and self.AttributeDefs.capacity_unit.name not in written_names
        ):
            settings["capacity_unit"] = IrrigationAmountUnit.liter

        payload = pack_manual_default_settings(
            irrigation_mode=settings["irrigation_mode"],
            irrigation_total_duration=int(settings["irrigation_total_duration"]),
            interval_irrigation_duration=int(
                settings["interval_irrigation_duration"]
            ),
            interval_pause=int(settings["interval_pause"]),
            capacity_unit=settings["capacity_unit"],
            capacity_amount=int(settings["capacity_amount"]),
            fail_safe=int(settings["fail_safe"]),
        )

        return await self.endpoint.swvzf2_cluster.write_attributes(
            {SWVZF2Cluster.AttributeDefs.manual_default_settings.id: payload}
        )


class SWVZF2ProgressCluster(LocalDataCluster):
    """Cluster locale che espone 0x501F come attributi individuali.

    Sola lettura: alimentato dai report spontanei del dispositivo, non scrive
    nulla. Esiste su un solo endpoint — i report di entrambi i canali vengono
    instradati qui da SWVZF2Cluster._sync_progress.
    """

    cluster_id = 0xFBFD
    ep_attribute = "swvzf2_progress"

    class AttributeDefs(BaseAttributeDefs):
        """Attributi virtuali derivati da irrigation_status."""

        session_state: Final = ZCLAttributeDef(
            id=0x0000, type=IrrigationSessionState
        )
        session_mode: Final = ZCLAttributeDef(id=0x0001, type=IrrigationMode)
        #: Durata obiettivo della sessione, in secondi (end - start).
        session_target_duration: Final = ZCLAttributeDef(id=0x0002, type=t.uint32_t)
        #: Tempo trascorso dall'apertura, in secondi (current - start).
        session_elapsed: Final = ZCLAttributeDef(id=0x0003, type=t.uint32_t)
        #: Volume erogato nella sessione, in litri.
        session_volume: Final = ZCLAttributeDef(id=0x0004, type=t.uint16_t)
        #: Comodita' per il binary sensor: sessione in corso si'/no.
        irrigating: Final = ZCLAttributeDef(id=0x0005, type=t.Bool)

    def update_from_irrigation_status(self, decoded: dict[str, Any]) -> None:
        """Aggiorna gli attributi locali da un payload 0x501F decodificato."""
        for name, value in decoded.items():
            self._update_attribute(self.find_attribute(name), value)
        self._update_attribute(
            self.find_attribute(self.AttributeDefs.irrigating.name),
            t.Bool(decoded["session_state"] == IrrigationSessionState.running),
        )


class SWVZF2TimeCluster(CustomCluster, Time):
    """Cluster Time in USCITA (client) sull'endpoint 1: l'ora per la valvola.

    La valvola chiede Time, LocalTime, TimeZone, DstStart, DstEnd e DstShift
    (osservato il 2026-10-17 su fw 0x00001009). Senza questo cluster zigpy
    scarta la richiesta ("Ignoring message on unknown cluster: 0x000a") e la
    valvola, che esegue i programmi sul proprio orologio locale, resta
    all'ora che ha. Le risposte seguono time_cluster_values; ogni richiesta
    lascia una riga di log INFO e un evento "time_sync" per l'integrazione.
    """

    _snapshot: dict[str, int] | None = None

    def handle_cluster_general_request(
        self,
        hdr: foundation.ZCLHeader,
        args: Any,
        *,
        dst_addressing: Any = None,
    ) -> None:
        """Fotografa l'ora una volta per richiesta, la logga, poi risponde."""
        if hdr.command_id == foundation.GeneralCommand.Read_Attributes:
            tz = local_timezone()
            self._snapshot = time_cluster_values(datetime.now(UTC), tz)
            requested = [int(a) for a in getattr(args, "attribute_ids", [])]
            local = _Y2K + timedelta(seconds=self._snapshot["local_time"])
            _LOGGER.info(
                "SWV-ZF2 %s asked for the time (attributes %s); replying local "
                "%s, time zone %+d s, DST shift %d s",
                self.endpoint.device.ieee,
                [f"0x{a:04X}" for a in requested],
                local.strftime("%Y-%m-%d %H:%M:%S"),
                self._snapshot["time_zone"],
                self._snapshot["dst_shift"],
            )
            _notify(
                self,
                "time_sync",
                attributes=requested,
                values=dict(self._snapshot),
                at=datetime.now(UTC),
            )
        super().handle_cluster_general_request(
            hdr, args, dst_addressing=dst_addressing
        )

    def _value(self, name: str) -> int:
        snapshot = self._snapshot or time_cluster_values(
            datetime.now(UTC), local_timezone()
        )
        return snapshot[name]

    def handle_read_attribute_time(self) -> t.UTCTime:
        """Secondi UTC dal 2000."""
        return t.UTCTime(self._value("time"))

    def handle_read_attribute_time_zone(self) -> t.int32s:
        """Offset standard (senza ora legale), in secondi."""
        return t.int32s(self._value("time_zone"))

    def handle_read_attribute_local_time(self) -> t.LocalTime:
        """Ora locale effettiva, secondi dal 2000."""
        return t.LocalTime(self._value("local_time"))

    def handle_read_attribute_dst_start(self) -> t.uint32_t:
        """Sempre 0, come Z2M per questa famiglia."""
        return t.uint32_t(self._value("dst_start"))

    def handle_read_attribute_dst_end(self) -> t.uint32_t:
        """Sempre 0, come Z2M per questa famiglia."""
        return t.uint32_t(self._value("dst_end"))

    def handle_read_attribute_dst_shift(self) -> t.int32s:
        """Spostamento dell'ora legale dell'anno, in secondi."""
        return t.int32s(self._value("dst_shift"))


def _register_progress_entities(builder: QuirkBuilder) -> QuirkBuilder:
    """Registra i sensori di avanzamento della sessione (da 0x501F)."""
    return (
        builder.binary_sensor(
            SWVZF2ProgressCluster.AttributeDefs.irrigating.name,
            SWVZF2ProgressCluster.cluster_id,
            endpoint_id=CONFIG_ENDPOINT,
            device_class=BinarySensorDeviceClass.RUNNING,
            unique_id_suffix="irrigating",
            translation_key="irrigating",
            fallback_name="Irrigating",
        )
        .sensor(
            attribute_name=SWVZF2ProgressCluster.AttributeDefs.session_volume.name,
            cluster_id=SWVZF2ProgressCluster.cluster_id,
            endpoint_id=CONFIG_ENDPOINT,
            device_class=SensorDeviceClass.VOLUME,
            state_class=SensorStateClass.MEASUREMENT,
            unit=UnitOfVolume.LITERS,
            unique_id_suffix="session_volume",
            translation_key="session_volume",
            fallback_name="Session volume",
        )
        .sensor(
            attribute_name=SWVZF2ProgressCluster.AttributeDefs.session_elapsed.name,
            cluster_id=SWVZF2ProgressCluster.cluster_id,
            endpoint_id=CONFIG_ENDPOINT,
            device_class=SensorDeviceClass.DURATION,
            state_class=SensorStateClass.MEASUREMENT,
            unit=UnitOfTime.SECONDS,
            unique_id_suffix="session_elapsed",
            translation_key="session_elapsed",
            fallback_name="Session elapsed",
        )
        .sensor(
            attribute_name=(
                SWVZF2ProgressCluster.AttributeDefs.session_target_duration.name
            ),
            cluster_id=SWVZF2ProgressCluster.cluster_id,
            endpoint_id=CONFIG_ENDPOINT,
            device_class=SensorDeviceClass.DURATION,
            unit=UnitOfTime.SECONDS,
            entity_type=EntityType.DIAGNOSTIC,
            unique_id_suffix="session_target_duration",
            translation_key="session_target_duration",
            fallback_name="Session target duration",
        )
    )


def _register_config_entities(builder: QuirkBuilder) -> QuirkBuilder:
    """Registra le entita' di configurazione dell'irrigazione manuale.

    Un blocco solo, sull'endpoint 1: 0x501D e' un attributo GLOBALE del
    dispositivo, non per canale (vedi la nota nel docstring del modulo). I nomi
    non portano quindi alcun suffisso di canale.
    """
    return (
        builder.enum(
            SWVZF2ManualConfigCluster.AttributeDefs.irrigation_mode.name,
            IrrigationMode,
            SWVZF2ManualConfigCluster.cluster_id,
            endpoint_id=CONFIG_ENDPOINT,
            entity_type=EntityType.CONFIG,
            unique_id_suffix="irrigation_mode",
            translation_key="irrigation_mode",
            fallback_name="Irrigation mode",
        )
        .number(
            SWVZF2ManualConfigCluster.AttributeDefs.irrigation_total_duration.name,
            SWVZF2ManualConfigCluster.cluster_id,
            endpoint_id=CONFIG_ENDPOINT,
            entity_type=EntityType.CONFIG,
            min_value=0,
            max_value=719,
            step=1,
            unit=UnitOfTime.MINUTES,
            device_class=NumberDeviceClass.DURATION,
            unique_id_suffix="irrigation_duration",
            translation_key="irrigation_duration",
            fallback_name="Irrigation duration",
        )
        .number(
            SWVZF2ManualConfigCluster.AttributeDefs.capacity_amount.name,
            SWVZF2ManualConfigCluster.cluster_id,
            endpoint_id=CONFIG_ENDPOINT,
            entity_type=EntityType.CONFIG,
            min_value=0,
            max_value=10000,
            step=1,
            unit=UnitOfVolume.LITERS,
            unique_id_suffix="irrigation_volume",
            translation_key="irrigation_volume",
            fallback_name="Irrigation volume",
        )
        .number(
            SWVZF2ManualConfigCluster.AttributeDefs.fail_safe.name,
            SWVZF2ManualConfigCluster.cluster_id,
            endpoint_id=CONFIG_ENDPOINT,
            entity_type=EntityType.CONFIG,
            min_value=0,
            max_value=719,
            step=1,
            unit=UnitOfTime.MINUTES,
            unique_id_suffix="fail_safe",
            translation_key="fail_safe",
            fallback_name="Fail-safe timeout",
        )
    )


_builder = (
    QuirkBuilder("SONOFF", "SWV-ZF2")
    .also_applies_to("SONOFF", "SWV-ZF2U")
    .also_applies_to("SONOFF", "SWV-ZF2E")
    .replaces(SWVZF2Cluster)
    .replaces(SWVZF2Cluster, endpoint_id=2)
    # Il cluster locale di configurazione esiste solo sull'endpoint 1: 0x501D
    # e' globale, sull'endpoint 2 non risponde (UNSUPPORTED_ATTRIBUTE).
    .adds(SWVZF2ManualConfigCluster, endpoint_id=CONFIG_ENDPOINT)
    # Idem per l'avanzamento: 0x501F puo' arrivare da entrambi gli endpoint ma
    # confluisce in un unico cluster locale.
    .adds(SWVZF2ProgressCluster, endpoint_id=CONFIG_ENDPOINT)
    # Cluster Time in uscita: la valvola chiede l'ora (vedi SWVZF2TimeCluster).
    # replaces e non adds: se una firmware futura lo dichiarasse gia', lo
    # sostituisce invece di duplicarlo.
    .replaces(SWVZF2TimeCluster, cluster_type=ClusterType.Client, endpoint_id=1)
    # Perdita acqua (bit1 di 0x500C)
    .binary_sensor(
        SWVZF2Cluster.AttributeDefs.water_valve_state.name,
        SWVZF2Cluster.cluster_id,
        device_class=BinarySensorDeviceClass.MOISTURE,
        attribute_converter=lambda x: x & ValveState.Water_Leakage,
        unique_id_suffix="water_leak_status",
        reporting_config=ReportingConfig(
            min_interval=30, max_interval=900, reportable_change=1
        ),
        translation_key="water_leak",
        fallback_name="Water leak",
    )
    # Mancanza acqua (bit0 canale 1 / bit4 canale 2 di 0x500C)
    .binary_sensor(
        SWVZF2Cluster.AttributeDefs.water_valve_state.name,
        SWVZF2Cluster.cluster_id,
        device_class=BinarySensorDeviceClass.PROBLEM,
        attribute_converter=lambda x: x
        & (ValveState.Water_Shortage | ValveState.Water_Shortage_Channel_2),
        unique_id_suffix="water_depletion_status",
        translation_key="water_depletion",
        fallback_name="Water depletion",
    )
    # Durata irrigazione - canale 1 (endpoint 1, 0x5006).
    # Niente reporting_config: 0x5006/0x5007 arrivano spontaneamente ogni ~60 s
    # mentre la valvola eroga, e la configure_reporting su 0x501B/0x501C viene
    # rifiutata dal dispositivo con UNSUPPORTED_ATTRIBUTE.
    .sensor(
        attribute_name=SWVZF2Cluster.AttributeDefs.valve_open_duration.name,
        cluster_id=SWVZF2Cluster.cluster_id,
        endpoint_id=1,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        unit=UnitOfTime.MINUTES,
        unique_id_suffix="water_usage_duration_ch1",
        translation_key="water_usage_duration_ch1",
        fallback_name="Water usage duration CH1",
    )
    # Durata irrigazione - canale 2 (endpoint 2, 0x5006)
    .sensor(
        attribute_name=SWVZF2Cluster.AttributeDefs.valve_open_duration.name,
        cluster_id=SWVZF2Cluster.cluster_id,
        endpoint_id=2,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        unit=UnitOfTime.MINUTES,
        unique_id_suffix="water_usage_duration_ch2",
        translation_key="water_usage_duration_ch2",
        fallback_name="Water usage duration CH2",
    )
    # Volume irrigazione (litri, 0x5007)
    .sensor(
        attribute_name=SWVZF2Cluster.AttributeDefs.irrigation_volume.name,
        cluster_id=SWVZF2Cluster.cluster_id,
        endpoint_id=1,
        device_class=SensorDeviceClass.VOLUME,
        state_class=SensorStateClass.TOTAL_INCREASING,
        unit=UnitOfVolume.LITERS,
        unique_id_suffix="water_usage_volume",
        translation_key="water_usage_volume",
        fallback_name="Water usage volume",
    )
)

_builder = _register_config_entities(_builder)
_builder = _register_progress_entities(_builder)
_builder.add_to_registry()
