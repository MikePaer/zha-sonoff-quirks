"""Programmi on-device: byte per byte contro Zigbee2MQTT.

tests/fixtures/z2m_vectors.json e' generato da tests/z2m_oracle/gen_vectors.cjs
eseguendo il codice VERO di zigbee-herdsman-converters / zigbee-herdsman
(versioni registrate nel file). Ogni caso qui traduce l'input Z2M nei campi
della quirk e confronta i byte prodotti.
"""

from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sonoff_swv_zf2 import (
    PLAN_DEFAULT_AMOUNT,
    PLAN_DEFAULT_FAIL_SAFE,
    PLAN_DEFAULT_INTERVAL_DURATION,
    PLAN_DEFAULT_IRRIGATION_DURATION,
    WEEKDAY_BITS,
    IrrigationAmountUnit,
    IrrigationMode,
    IrrigationPlan,
    PlanLoopType,
    RawPayload,
    SWVZF2Cluster,
    decode_irrigation_plan,
    encode_irrigation_plan,
    encode_rain_delay,
    time_cluster_values,
)

VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "z2m_vectors.json").read_text("utf-8")
)


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def plan_from_z2m(value: dict) -> IrrigationPlan:
    """Input di irrigation_plan_settings (Z2M) -> IrrigationPlan, default Z2M."""
    loop_type = PlanLoopType[value["loop_type_mode"]]
    if loop_type == PlanLoopType.weekdays:
        loop_value = 0
        for day, enabled in value.get("loop_type_week_days", {}).items():
            if enabled:
                loop_value |= WEEKDAY_BITS[day]
    elif loop_type == PlanLoopType.day_interval:
        loop_value = value.get("loop_type_interval_days", 1)
    else:
        loop_value = 0
    hours, minutes = (int(part) for part in value["start_time"].split(":"))
    return IrrigationPlan(
        index=value["plan_index"],
        enabled=bool(value["enable_state"]),
        loop_type=loop_type,
        loop_value=loop_value,
        enable_date=date.fromisoformat(value["enable_date"]),
        start_seconds=hours * 3600 + minutes * 60,
        mode=IrrigationMode[value["irrigation_mode"]],
        total_duration=value.get("irrigation_total_duration", 10),
        irrigation_duration=value.get(
            "irrigation_duration", PLAN_DEFAULT_IRRIGATION_DURATION
        ),
        interval_duration=value.get(
            "interval_duration", PLAN_DEFAULT_INTERVAL_DURATION
        ),
        amount_unit=IrrigationAmountUnit[value["irrigation_amount_unit"]],
        amount=value.get("irrigation_amount", PLAN_DEFAULT_AMOUNT),
        fail_safe=value.get("fail_safe", PLAN_DEFAULT_FAIL_SAFE),
        created=int(_parse_iso(value["create_datetime"]).timestamp()),
    )


def test_vectors_come_from_a_pinned_z2m():
    """Il file registra da quale Z2M vengono i byte attesi."""
    assert VECTORS["zigbee-herdsman-converters"]
    assert VECTORS["zigbee-herdsman"]
    assert len(VECTORS["plans"]) >= 10


@pytest.mark.parametrize("case", VECTORS["plans"], ids=lambda c: c["name"])
def test_plan_payload_matches_z2m(case):
    """Stessi 28 byte che Z2M mette nel comando irrigationPlanSettings."""
    payload = encode_irrigation_plan(plan_from_z2m(case["value"]))

    assert case["command"] == "irrigationPlanSettings"
    assert payload.hex() == case["payload_hex"]


@pytest.mark.parametrize("case", VECTORS["plans"], ids=lambda c: c["name"])
def test_plan_goes_to_the_line_endpoint(case):
    """Linea A = endpoint 1, linea B = endpoint 2, come mappa Z2M."""
    assert case["endpoint_id"] == int(case["endpoint_name"])


@pytest.mark.parametrize("case", VECTORS["plans"], ids=lambda c: c["name"])
def test_plan_report_decodes_z2m_payload(case):
    """Un report 0x09 con quei byte ridà lo stesso programma."""
    plan = plan_from_z2m(case["value"])

    decoded = decode_irrigation_plan(bytes.fromhex(case["payload_hex"]))

    assert decoded == plan


@pytest.mark.parametrize("case", VECTORS["plans"], ids=lambda c: c["name"])
def test_set_command_serializes_payload_verbatim(case):
    """Lo schema del comando 0x06 non aggiunge lunghezze ne' prefissi."""
    schema = SWVZF2Cluster.ServerCommandDefs.irrigation_plan_set.schema
    raw = bytes.fromhex(case["payload_hex"])

    assert schema(payload=RawPayload(raw)).serialize() == raw


@pytest.mark.parametrize("case", VECTORS["removes"], ids=lambda c: c["payload_hex"])
def test_remove_payload_matches_z2m(case):
    """Il comando 0x07 porta il solo indice."""
    schema = SWVZF2Cluster.ServerCommandDefs.irrigation_plan_remove.schema

    assert case["command"] == "irrigationPlanRemove"
    assert case["endpoint_id"] == int(case["endpoint_name"])
    assert (
        schema(plan_index=case["plan_index"]).serialize().hex() == case["payload_hex"]
    )


@pytest.mark.parametrize("case", VECTORS["time"], ids=lambda c: f"{c['tz']}@{c['now']}")
def test_time_reply_matches_z2m(case):
    """Gli attributi Time sono quelli di swvznGenTimeCompatResponse."""
    values = time_cluster_values(_parse_iso(case["now"]), ZoneInfo(case["tz"]))

    assert values == case["expected"]


@pytest.mark.parametrize(
    "case", VECTORS["rain_delay"], ids=lambda c: f"{c['tz']}:{c['value']}"
)
def test_rain_delay_matches_z2m(case):
    """Il comando 0x08 porta la fine del rinvio in secondi locali dal 2000."""
    end = None if case["value"] == "0" else _parse_iso(case["value"])

    payload = encode_rain_delay(end, ZoneInfo(case["tz"]))

    assert case["expected"]["command"] == "rainDelay"
    assert payload.hex() == case["expected"]["payload_hex"]
