"""Unit test per la logica pura dei programmi (schedule_logic.py, niente HA)."""

from datetime import date, datetime, timedelta
import importlib.util
import json
import pathlib
import sys
from zoneinfo import ZoneInfo

import pytest
from sonoff_swv_zf2 import (
    EVERY_DAY_MASK,
    IrrigationMode,
    PlanLoopType,
    encode_irrigation_plan,
)

# Caricamento mirato del solo modulo puro, come test_history_logic.py: il suo
# import relativo della quirk ricade sul modulo top-level sonoff_swv_zf2.
_spec = importlib.util.spec_from_file_location(
    "schedule_logic",
    pathlib.Path(__file__).parents[1]
    / "custom_components/zha_sonoff_quirks/schedule_logic.py",
)
sl = importlib.util.module_from_spec(_spec)
# @dataclass risolve i tipi tramite sys.modules[__module__].
sys.modules.setdefault("schedule_logic", sl)
_spec.loader.exec_module(sl)

LA = ZoneInfo("America/Los_Angeles")
#: Payload dei vettori Z2M (tests/fixtures/z2m_vectors.json), per nome.
Z2M = {
    case["name"]: case["payload_hex"]
    for case in json.loads(
        (pathlib.Path(__file__).parent / "fixtures" / "z2m_vectors.json").read_text(
            "utf-8"
        )
    )["plans"]
}
TODAY = date(2026, 10, 22)
CREATED = 0x6ADA3A08  # 2026-10-22T09:30:00-07:00, come nei vettori Z2M

#: Il programma deciso il 2026-10-22: 5:00, ordine fisso, 1 minuto di pausa.
ZONES = [
    sl.ProgramZone("switch.front_lawn", 11, True),
    sl.ProgramZone("switch.front_boxes", 11, True),
    sl.ProgramZone("switch.back_lawn", 11, True),
    sl.ProgramZone("switch.back_boxes", 11, True),
]


def _plans(zones, current=None, start="05:00", gap=1):
    return sl.program_plans(
        start_seconds=sl.parse_start_time(start),
        zones=zones,
        gap_minutes=gap,
        slot=0,
        current=current or {},
        today=TODAY,
        created=CREATED,
    )


# ── orari ──


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("05:00", 18000), ("05:00:00", 18000), ("23:59", 86340), ("00:00", 0)],
)
def test_parse_start_time(value, seconds):
    assert sl.parse_start_time(value) == seconds


@pytest.mark.parametrize("value", ["5", "24:00", "05:60", "ab:cd", ""])
def test_parse_start_time_rejects(value):
    with pytest.raises(ValueError):
        sl.parse_start_time(value)


def test_weekday_mask_accepts_ha_short_names():
    assert sl.weekday_mask(["mon", "wednesday", "Fri"]) == 0x02 | 0x08 | 0x20
    assert sl.weekdays_from_mask(0x2A) == ["monday", "wednesday", "friday"]


# ── build_plan ──


def test_daily_minutes_plan_is_the_z2m_vector():
    """Lo stesso programma del vettore Z2M program_front_lawn (fail_safe 0)."""
    plan = sl.build_plan(
        index=0, start_time="05:00", minutes=11, enable_date=TODAY, created=CREATED
    )

    assert plan.loop_type == PlanLoopType.weekdays
    assert plan.loop_value == EVERY_DAY_MASK
    assert plan.fail_safe == 0
    assert encode_irrigation_plan(plan).hex() == Z2M["program_front_lawn"]


def test_whole_program_matches_z2m_byte_for_byte():
    """I 4 programmi del 2026-10-22, e una zona spenta, come li manda Z2M."""
    plans = _plans(ZONES)
    off = [ZONES[0], sl.ProgramZone("switch.front_boxes", 11, False), *ZONES[2:]]
    disabled = _plans(off, plans)["switch.front_boxes"]

    assert {k: encode_irrigation_plan(p).hex() for k, p in plans.items()} == {
        "switch.front_lawn": Z2M["program_front_lawn"],
        "switch.front_boxes": Z2M["program_front_boxes"],
        "switch.back_lawn": Z2M["program_back_lawn"],
        "switch.back_boxes": Z2M["program_back_boxes"],
    }
    assert encode_irrigation_plan(disabled).hex() == Z2M["program_zone_disabled"]


def test_liters_plan_uses_capacity_mode():
    plan = sl.build_plan(
        index=1,
        start_time="07:15",
        liters=50,
        fail_safe_minutes=15,
        repeat=sl.REPEAT_WEEKDAYS,
        weekdays=["sat", "sun"],
        enable_date=TODAY,
        created=CREATED,
    )

    assert plan.mode == IrrigationMode.capacity
    assert (plan.amount, plan.fail_safe, plan.loop_value) == (50, 15, 0x41)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"minutes": 5, "liters": 5},
        {},
        {"minutes": 0},
        {"minutes": 5, "repeat": "weekdays", "weekdays": []},
        {"minutes": 5, "repeat": "every_n_days"},
        {"minutes": 5, "repeat": "hourly"},
        {"minutes": 5, "index": 6},
    ],
)
def test_build_plan_rejects(kwargs):
    base = {"index": 0, "start_time": "05:00", "enable_date": TODAY, "created": 0}
    with pytest.raises(ValueError):
        sl.build_plan(**{**base, **kwargs})


def test_storage_round_trip():
    plan = sl.build_plan(
        index=4,
        start_time="23:59",
        minutes=719,
        repeat=sl.REPEAT_EVERY_N_DAYS,
        interval_days=3,
        enable_date=TODAY,
        created=CREATED,
        enabled=False,
    )

    assert sl.plan_from_dict(sl.plan_to_dict(plan)) == plan


def test_describe_plan():
    plan = sl.build_plan(
        index=0, start_time="05:12", minutes=11, enable_date=TODAY, created=CREATED
    )

    assert sl.describe_plan(plan) == {
        "index": 0,
        "enabled": True,
        "start": "05:12",
        "repeat": "daily",
        "minutes": 11,
        "from": "2026-10-22",
    }


# ── programma ──


def test_program_starts_back_to_back_with_gap():
    """5:00, 5:12, 5:24, 5:36: 11 minuti + 1 di pausa."""
    plans = _plans(ZONES)

    assert {k: sl.format_start(p.start_seconds) for k, p in plans.items()} == {
        "switch.front_lawn": "05:00",
        "switch.front_boxes": "05:12",
        "switch.back_lawn": "05:24",
        "switch.back_boxes": "05:36",
    }
    assert all(
        p.enabled and p.total_duration == 11 and p.index == 0 for p in plans.values()
    )


def test_reapplying_the_same_program_writes_nothing():
    current = _plans(ZONES)

    assert _plans(ZONES, current) == {}


def test_changing_minutes_rewrites_only_that_zone_and_later_ones():
    current = _plans(ZONES)
    zones = [ZONES[0], sl.ProgramZone("switch.front_boxes", 15, True), *ZONES[2:]]

    changed = _plans(zones, current)

    assert sorted(changed) == [
        "switch.back_boxes",
        "switch.back_lawn",
        "switch.front_boxes",
    ]
    assert sl.format_start(changed["switch.back_lawn"].start_seconds) == "05:28"


def test_disabled_zone_is_skipped_and_its_plan_disabled():
    """Zona spenta: il programma resta sulla valvola con enabled=0, le altre scalano."""
    current = _plans(ZONES)
    zones = [ZONES[0], sl.ProgramZone("switch.front_boxes", 11, False), *ZONES[2:]]

    changed = _plans(zones, current)

    disabled = changed["switch.front_boxes"]
    assert disabled.enabled is False
    assert disabled.start_seconds == current["switch.front_boxes"].start_seconds
    assert sl.format_start(changed["switch.back_lawn"].start_seconds) == "05:12"
    assert sl.format_start(changed["switch.back_boxes"].start_seconds) == "05:24"
    assert "switch.front_lawn" not in changed


def test_reenabling_restores_the_zone():
    current = _plans(ZONES)
    off = [ZONES[0], sl.ProgramZone("switch.front_boxes", 11, False), *ZONES[2:]]
    current |= _plans(off, current)

    changed = _plans(ZONES, current)

    assert changed["switch.front_boxes"].enabled is True
    assert sl.format_start(changed["switch.back_lawn"].start_seconds) == "05:24"


def test_disabled_zone_without_a_plan_writes_nothing():
    zones = [ZONES[0], sl.ProgramZone("switch.front_boxes", 11, False)]

    assert list(_plans(zones)) == ["switch.front_lawn"]


def test_unchanged_plan_keeps_its_enable_date():
    current = _plans(ZONES)
    later = sl.program_plans(
        start_seconds=sl.parse_start_time("06:00"),
        zones=ZONES,
        gap_minutes=1,
        slot=0,
        current=current,
        today=TODAY + timedelta(days=5),
        created=CREATED + 1,
    )

    assert all(p.enable_date == TODAY for p in later.values())


def test_program_must_end_the_same_day():
    with pytest.raises(ValueError, match="next day"):
        _plans(ZONES, start="23:30")


# ── prossima partenza ──


def _daily(start="05:00", **kw):
    return sl.build_plan(
        index=0, start_time=start, minutes=11, enable_date=TODAY, created=CREATED, **kw
    )


def test_next_run_today_and_tomorrow():
    plan = _daily()

    before = datetime(2026, 10, 22, 4, 0, tzinfo=LA)
    after = datetime(2026, 10, 22, 6, 0, tzinfo=LA)

    assert sl.next_run([plan], before, LA)[0] == datetime(2026, 10, 22, 5, 0, tzinfo=LA)
    assert sl.next_run([plan], after, LA)[0] == datetime(2026, 10, 23, 5, 0, tzinfo=LA)


def test_next_run_across_dst_end_is_still_5_local():
    """1 novembre 2026: l'ora legale finisce, la partenza resta alle 5:00 locali."""
    start, _ = sl.next_run([_daily()], datetime(2026, 10, 31, 6, 0, tzinfo=LA), LA)

    assert start == datetime(2026, 11, 1, 5, 0, tzinfo=LA)
    assert start.utcoffset() == timedelta(hours=-8)


def test_next_run_skips_disabled_and_picks_earliest():
    early = _daily("05:00", enabled=False)
    late = sl.build_plan(
        index=1, start_time="05:12", minutes=11, enable_date=TODAY, created=CREATED
    )

    start, plan = sl.next_run(
        [early, late], datetime(2026, 10, 22, 4, 0, tzinfo=LA), LA
    )

    assert (start.hour, start.minute, plan.index) == (5, 12, 1)


def test_next_run_respects_rain_delay():
    now = datetime(2026, 10, 22, 4, 0, tzinfo=LA)
    delay = datetime(2026, 10, 23, 9, 0, tzinfo=LA)

    start, _ = sl.next_run([_daily()], now, LA, rain_delay_until=delay)

    assert start == datetime(2026, 10, 24, 5, 0, tzinfo=LA)


@pytest.mark.parametrize(
    ("repeat", "extra", "expected"),
    [
        (sl.REPEAT_ODD_DAYS, {}, date(2026, 10, 23)),
        (sl.REPEAT_EVEN_DAYS, {}, date(2026, 10, 22)),
        (sl.REPEAT_EVERY_N_DAYS, {"interval_days": 3}, date(2026, 10, 22)),
        (sl.REPEAT_WEEKDAYS, {"weekdays": ["sat"]}, date(2026, 10, 24)),
    ],
)
def test_next_run_repeat_rules(repeat, extra, expected):
    plan = _daily(repeat=repeat, **extra)

    start, _ = sl.next_run([plan], datetime(2026, 10, 22, 4, 0, tzinfo=LA), LA)

    assert start.date() == expected


def test_every_n_days_counts_from_enable_date():
    plan = _daily(repeat=sl.REPEAT_EVERY_N_DAYS, interval_days=3)

    start, _ = sl.next_run([plan], datetime(2026, 10, 22, 6, 0, tzinfo=LA), LA)

    assert start.date() == date(2026, 10, 25)


def test_nothing_before_enable_date():
    plan = sl.build_plan(
        index=0,
        start_time="05:00",
        minutes=11,
        enable_date=date(2026, 11, 2),
        created=CREATED,
    )

    start, _ = sl.next_run([plan], datetime(2026, 10, 22, 4, 0, tzinfo=LA), LA)

    assert start.date() == date(2026, 11, 2)


def test_plan_target_for_run_log():
    assert sl.plan_target(_daily()) == ("duration", 11.0)
