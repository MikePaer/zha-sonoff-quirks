"""Programmi on-device e orologio: comportamento della quirk su zigpy vero.

I frame entrano da Device.packet_received, lo stesso ingresso della radio, e
le uscite si leggono sul mock di ControllerApplication.request: si prova cosi'
anche l'instradamento di zigpy, non solo i metodi del cluster.
"""

from __future__ import annotations

import asyncio
import collections
from datetime import UTC, date, datetime
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
import sonoff_swv_zf2
from sonoff_swv_zf2 import (
    CMD_PLAN_REMOVE,
    CMD_PLAN_REPORT,
    CMD_PLAN_SET,
    CMD_RAIN_DELAY,
    EVERY_DAY_MASK,
    IrrigationPlan,
    IrrigationSessionState,
    PlanLoopType,
    SWVZF2TimeCluster,
    decode_session_source,
    device_local_seconds,
    device_local_to_datetime,
    encode_irrigation_plan,
)
import zigpy.types as t
from zigpy.zcl import foundation
from zigpy.zcl.clusters.general import Time

PLAN = IrrigationPlan(
    index=0,
    enabled=True,
    loop_type=PlanLoopType.weekdays,
    loop_value=EVERY_DAY_MASK,
    enable_date=date(2026, 10, 22),
    start_seconds=5 * 3600,
    total_duration=11,
    created=0x6ADA3A08,
)


@pytest.fixture(autouse=True)
def _packet_pipeline(app):
    """Quel che packet_received chiede all'application oltre al mock base."""
    app._req_listeners = collections.defaultdict(list)


@pytest.fixture
def events():
    """Eventi notificati dalla quirk all'integrazione."""
    received: list[dict] = []
    remove = sonoff_swv_zf2.add_schedule_listener(received.append)
    yield received
    remove()


@pytest.fixture
def la_timezone():
    """Fuso di HA durante il test, ripristinato dopo."""
    sonoff_swv_zf2.set_local_timezone(ZoneInfo("America/Los_Angeles"))
    yield
    sonoff_swv_zf2.set_local_timezone(None)


def _packet(device, endpoint_id: int, cluster_id: int, data: bytes) -> t.ZigbeePacket:
    return t.ZigbeePacket(
        src=t.AddrModeAddress(addr_mode=t.AddrMode.NWK, address=device.nwk),
        src_ep=endpoint_id,
        dst=t.AddrModeAddress(addr_mode=t.AddrMode.NWK, address=0x0000),
        dst_ep=1,
        tsn=data[1],
        profile_id=260,
        cluster_id=cluster_id,
        data=t.SerializableBytes(data),
        lqi=255,
        rssi=-60,
    )


def _sent_frames(request) -> list[tuple[int, int, foundation.ZCLHeader, bytes]]:
    """(endpoint di destinazione, cluster, header, payload) di ogni invio."""
    frames = []
    for call in request.await_args_list:
        kwargs = call.kwargs
        hdr, payload = foundation.ZCLHeader.deserialize(bytes(kwargs["data"]))
        frames.append((kwargs["dst_ep"], kwargs["cluster"], hdr, payload))
    return frames


async def _drain() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------- #
# Orologio
# --------------------------------------------------------------------------- #


def test_time_cluster_added_as_output_on_endpoint_1(quirked_device):
    """La valvola e' client di Time: il cluster sta tra gli out_clusters."""
    cluster = quirked_device.endpoints[1].out_clusters[Time.cluster_id]

    assert isinstance(cluster, SWVZF2TimeCluster)
    assert Time.cluster_id not in quirked_device.endpoints[2].out_clusters


@pytest.mark.asyncio
async def test_valve_time_request_gets_z2m_style_reply(
    quirked_device, events, la_timezone
):
    """La read del 2026-10-17 (6 attributi) ora riceve risposta, non e' scartata."""
    requested = [0x0000, 0x0007, 0x0002, 0x0003, 0x0004, 0x0005]
    request = (
        foundation.ZCLHeader.general(
            tsn=0x42,
            command_id=foundation.GeneralCommand.Read_Attributes,
            direction=foundation.Direction.Client_to_Server,
        ).serialize()
        + t.List[t.uint16_t](requested).serialize()
    )
    now = datetime(2026, 10, 22, 16, 41, 22, tzinfo=UTC)

    with (
        mock.patch.object(sonoff_swv_zf2, "datetime", wraps=datetime) as dt,
        mock.patch.object(quirked_device, "request", mock.AsyncMock()) as sent,
    ):
        dt.now.side_effect = lambda tz=None: now if tz is UTC else now.astimezone(tz)
        quirked_device.packet_received(
            _packet(quirked_device, 1, Time.cluster_id, request)
        )
        await _drain()

    [(dst_ep, cluster_id, hdr, payload)] = _sent_frames(sent)
    assert (dst_ep, cluster_id) == (1, Time.cluster_id)
    assert hdr.command_id == foundation.GeneralCommand.Read_Attributes_rsp
    assert hdr.tsn == 0x42
    assert hdr.direction == foundation.Direction.Server_to_Client
    rsp, _ = foundation.GENERAL_COMMANDS[
        foundation.GeneralCommand.Read_Attributes_rsp
    ].schema.deserialize(payload)
    values = {r.attrid: r.value.value for r in rsp.status_records}
    assert all(r.status == foundation.Status.SUCCESS for r in rsp.status_records)
    # Stessi valori del vettore Z2M America/Los_Angeles 2026-10-22T16:41:22Z.
    assert values == {
        0x0000: 846002482,
        0x0007: 845977282,
        0x0002: -28800,
        0x0003: 0,
        0x0004: 0,
        0x0005: 3600,
    }
    [event] = [e for e in events if e["type"] == "time_sync"]
    assert event["ieee"] == str(quirked_device.ieee)
    assert event["attributes"] == requested


def test_time_reply_log_line(quirked_device, la_timezone, caplog):
    """Una riga INFO per risposta: da' la frequenza delle richieste."""
    cluster = quirked_device.endpoints[1].out_clusters[Time.cluster_id]
    hdr = foundation.ZCLHeader.general(
        tsn=1, command_id=foundation.GeneralCommand.Read_Attributes
    )
    args = mock.Mock(attribute_ids=[0x0000, 0x0007])

    with (
        caplog.at_level("INFO", logger=sonoff_swv_zf2.__name__),
        mock.patch.object(cluster, "create_catching_task"),
    ):
        cluster.handle_cluster_general_request(hdr, args)

    [record] = [r for r in caplog.records if "asked for the time" in r.message]
    assert "0x0000" in record.message and "0x0007" in record.message


def test_device_local_seconds_round_trip():
    """Tempi di 0x501F (locali dal 2000) <-> datetime nel fuso di HA."""
    tz = ZoneInfo("America/Los_Angeles")
    moment = datetime(2026, 10, 22, 5, 0, tzinfo=tz)

    seconds = device_local_seconds(moment, tz)

    assert device_local_to_datetime(seconds, tz) == moment


# --------------------------------------------------------------------------- #
# Invio dei comandi
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_line_b_plan_is_sent_on_endpoint_2(quirked_device, app):
    """Linea B: comando 0x06 sull'endpoint 2, payload identico all'encoder."""
    app.request.return_value = (foundation.Status.SUCCESS, "done")
    cluster = quirked_device.endpoints[2].swvzf2_cluster
    plan = IrrigationPlan(**{**PLAN.__dict__, "start_seconds": 5 * 3600 + 12 * 60})

    with mock.patch.object(
        cluster.endpoint,
        "request",
        mock.AsyncMock(
            return_value=foundation.GENERAL_COMMANDS[
                foundation.GeneralCommand.Default_Response
            ].schema(command_id=CMD_PLAN_SET, status=foundation.Status.SUCCESS)
        ),
    ) as request:
        status = await cluster.set_irrigation_plan(plan)

    assert status == 0
    kwargs = request.await_args.kwargs
    hdr, payload = foundation.ZCLHeader.deserialize(kwargs["data"])
    assert kwargs["cluster"] == 0xFC11
    assert cluster.endpoint.endpoint_id == 2
    assert hdr.frame_control.is_cluster
    assert not hdr.frame_control.is_manufacturer_specific
    assert hdr.direction == foundation.Direction.Client_to_Server
    assert not hdr.frame_control.disable_default_response
    assert hdr.command_id == CMD_PLAN_SET
    assert payload == encode_irrigation_plan(plan)
    assert kwargs["expect_reply"] is True


@pytest.mark.asyncio
async def test_tsn_matched_status_reply_is_reported(quirked_device, events):
    """Una risposta 0x06 consegnata come risultato vale come esito del set."""
    cluster = quirked_device.endpoints[1].swvzf2_cluster
    reply = cluster.ClientCommandDefs.irrigation_plan_set_rsp.schema(
        payload=sonoff_swv_zf2.RawPayload(b"\x00")
    )

    with mock.patch.object(cluster, "command", mock.AsyncMock(return_value=reply)):
        assert await cluster.set_irrigation_plan(PLAN) == 0

    assert [e["type"] for e in events] == ["plan_ack"]
    assert events[0]["status"] == 0
    assert events[0]["endpoint_id"] == 1


@pytest.mark.asyncio
async def test_rejected_status_is_returned(quirked_device):
    """Status diverso da 0: il set e' fallito."""
    cluster = quirked_device.endpoints[1].swvzf2_cluster
    reply = cluster.ClientCommandDefs.irrigation_plan_set_rsp.schema(
        payload=sonoff_swv_zf2.RawPayload(b"\x01")
    )

    with mock.patch.object(cluster, "command", mock.AsyncMock(return_value=reply)):
        assert await cluster.set_irrigation_plan(PLAN) == 1


@pytest.mark.asyncio
async def test_timeout_means_no_status(quirked_device):
    """Valvola addormentata: nessuno status, l'integrazione aspetta il 0x09."""
    cluster = quirked_device.endpoints[1].swvzf2_cluster

    with mock.patch.object(
        cluster, "command", mock.AsyncMock(side_effect=TimeoutError)
    ):
        assert await cluster.set_irrigation_plan(PLAN) is None


@pytest.mark.asyncio
async def test_remove_and_rain_delay_frames(quirked_device, la_timezone):
    """0x07 con l'indice (Default Response atteso), 0x08 con la fine del rinvio."""
    ep1 = quirked_device.endpoints[1]
    end = datetime(2026, 10, 23, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

    with mock.patch.object(ep1, "request", mock.AsyncMock()) as request:
        await ep1.swvzf2_cluster.remove_irrigation_plan(3)
        await ep1.swvzf2_cluster.set_rain_delay(end)
        await ep1.swvzf2_cluster.set_rain_delay(None)

    sent = [
        (foundation.ZCLHeader.deserialize(c.kwargs["data"]), c.kwargs["expect_reply"])
        for c in request.await_args_list
    ]
    assert [(h.command_id, p.hex(), r) for (h, p), r in sent] == [
        (CMD_PLAN_REMOVE, "03", True),
        # Vettore Z2M America/Los_Angeles 2026-10-23T09:00:00-07:00.
        (CMD_RAIN_DELAY, "326dde90", False),
        (CMD_RAIN_DELAY, "00000000", False),
    ]


@pytest.mark.asyncio
async def test_remove_rejects_bad_index(quirked_device):
    """Gli slot sono 0-5."""
    with pytest.raises(ValueError, match="index"):
        await quirked_device.endpoints[1].swvzf2_cluster.remove_irrigation_plan(6)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"index": 6}, "index"),
        ({"loop_type": PlanLoopType.day_interval, "loop_value": 0}, "interval"),
        ({"loop_value": 0x80}, "weekday mask"),
        ({"start_seconds": 86400}, "start_seconds"),
        ({"total_duration": 720}, "total_duration"),
        ({"enable_date": date(1999, 12, 31)}, "enable_date"),
    ],
)
def test_encoder_rejects_out_of_range_fields(changes, message):
    """Mai byte troncati in silenzio: un campo fuori range e' un errore."""
    with pytest.raises(ValueError, match=message):
        encode_irrigation_plan(IrrigationPlan(**{**PLAN.__dict__, **changes}))


# --------------------------------------------------------------------------- #
# Frame dalla valvola
# --------------------------------------------------------------------------- #


def _cluster_frame(command_id: int, payload: bytes, tsn: int = 0x10) -> bytes:
    hdr = foundation.ZCLHeader.cluster(
        tsn=tsn,
        command_id=command_id,
        direction=foundation.Direction.Server_to_Client,
    )
    return hdr.serialize() + payload


@pytest.mark.asyncio
async def test_plan_report_from_line_b(quirked_device, events):
    """0x09 dall'endpoint 2: report del programma della linea B."""
    plan = IrrigationPlan(**{**PLAN.__dict__, "index": 2})

    quirked_device.packet_received(
        _packet(
            quirked_device,
            2,
            0xFC11,
            _cluster_frame(CMD_PLAN_REPORT, encode_irrigation_plan(plan)),
        )
    )
    await _drain()

    [event] = events
    assert event["type"] == "plan_report"
    assert event["endpoint_id"] == 2
    assert event["plan"] == plan


@pytest.mark.asyncio
async def test_status_and_removed_reports(quirked_device, events):
    """0x06 con lo status e 0x0A con l'indice arrivano come eventi."""
    quirked_device.packet_received(
        _packet(quirked_device, 1, 0xFC11, _cluster_frame(CMD_PLAN_SET, b"\x00", 1))
    )
    quirked_device.packet_received(
        _packet(quirked_device, 1, 0xFC11, _cluster_frame(0x0A, b"\x04", 2))
    )
    await _drain()

    assert [(e["type"], e.get("status"), e.get("index")) for e in events] == [
        ("plan_ack", 0, None),
        ("plan_removed", None, 4),
    ]


@pytest.mark.asyncio
async def test_short_plan_report_is_ignored(quirked_device, events):
    """Un 0x09 troncato non diventa un programma."""
    quirked_device.packet_received(
        _packet(quirked_device, 1, 0xFC11, _cluster_frame(CMD_PLAN_REPORT, b"\x00\x01"))
    )
    await _drain()

    assert events == []


# --------------------------------------------------------------------------- #
# 0x501F: chi ha avviato la sessione
# --------------------------------------------------------------------------- #

#: Preambolo di una corsa da programma: byte 1 = indice 3, byte 2 = 0.
SCHEDULED_PREAMBLE = [0, 3, 0, 0, 50, 11, 195, 23, 50, 11, 196, 67, 1, 0, 0]


def test_session_source_from_captured_manual_run():
    """Le catture di agosto (corse manuali) hanno byte 2 = 1."""
    decoded = decode_session_source(
        [0, 0, 1, 0, 50, 11, 195, 23, 50, 11, 196, 67, 0, 0, 1]
    )

    assert decoded == {
        "session_state": 0,
        "session_plan_index": 0,
        "session_scheduled": False,
    }


def test_session_source_for_scheduled_run():
    """Byte 2 = 0: corsa da programma, byte 1 = quale."""
    assert decode_session_source(SCHEDULED_PREAMBLE) == {
        "session_state": 0,
        "session_plan_index": 3,
        "session_scheduled": True,
    }


@pytest.mark.parametrize("value", [None, [1, 2, 3], "x"])
def test_session_source_rejects_unknown_payloads(value):
    """Stesse regole di decode_irrigation_status."""
    assert decode_session_source(value) is None


def test_status_report_notifies_session_source(quirked_device, events):
    """0x501F dall'endpoint 2 porta linea e programma all'integrazione."""
    cluster = quirked_device.endpoints[2].swvzf2_cluster

    cluster._update_attribute(
        cluster.AttributeDefs.irrigation_status.id, SCHEDULED_PREAMBLE
    )

    [event] = events
    assert event["type"] == "session"
    assert event["endpoint_id"] == 2
    assert event["session_scheduled"] is True
    assert event["session_plan_index"] == 3


def test_broken_listener_does_not_break_zigpy(quirked_device, events):
    """Un'eccezione in un listener non risale nel cluster."""
    remove = sonoff_swv_zf2.add_schedule_listener(mock.Mock(side_effect=RuntimeError))
    try:
        cluster = quirked_device.endpoints[1].swvzf2_cluster
        cluster._update_attribute(
            cluster.AttributeDefs.irrigation_status.id, SCHEDULED_PREAMBLE
        )
    finally:
        remove()

    assert len(events) == 1


# --------------------------------------------------------------------------- #
# Correzioni dopo la Fase 5 (2026-10-22)
# --------------------------------------------------------------------------- #

#: Frame catturati il 2026-10-22: fine della corsa da programma #5 (17 L) e,
#: 9 ms dopo, l'annuncio "standby" del programma #5 del giorno seguente.
END_OF_SCHEDULED_RUN = [
    1,
    5,
    0,
    0,
    50,
    108,
    255,
    0,
    50,
    109,
    0,
    60,
    50,
    109,
    0,
    63,
    1,
    0,
    30,
    0,
    17,
]
STANDBY_NEXT_DAY = [3, 5, 0, 0, 50, 110, 87, 68, 50, 110, 87, 128, 1, 0, 30]


def test_standby_frame_keeps_the_finished_runs_volume(
    quirked_device, progress_cluster, events
):
    """Lo standby descrive la corsa SUCCESSIVA: non azzera il volume."""
    cluster = quirked_device.endpoints[1].swvzf2_cluster
    attr = cluster.AttributeDefs.irrigation_status.id

    cluster._update_attribute(attr, END_OF_SCHEDULED_RUN)
    cluster._update_attribute(attr, STANDBY_NEXT_DAY)

    assert progress_cluster.get("session_volume") == 17
    assert progress_cluster.get("session_state") == IrrigationSessionState.finished
    assert [e["type"] for e in events] == ["session", "next_session"]
    nxt = events[1]
    assert nxt["plan_index"] == 5
    assert device_local_to_datetime(nxt["start"], ZoneInfo("UTC")).replace(
        tzinfo=None
    ) == datetime(2026, 10, 23, 17, 35)


@pytest.mark.asyncio
async def test_remove_returns_the_default_response_status(quirked_device):
    """La conferma di una cancellazione via Zigbee e' il Default Response."""
    cluster = quirked_device.endpoints[1].swvzf2_cluster
    default_rsp = foundation.GENERAL_COMMANDS[
        foundation.GeneralCommand.Default_Response
    ].schema(command_id=CMD_PLAN_REMOVE, status=foundation.Status.SUCCESS)

    with mock.patch.object(
        cluster.endpoint, "request", mock.AsyncMock(return_value=default_rsp)
    ):
        assert await cluster.remove_irrigation_plan(5) == 0
