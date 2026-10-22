"""Unit test per la logica pura del run log (history_logic.py, niente HA)."""

import importlib.util
import pathlib

import pytest

# Caricamento mirato del solo modulo puro: il pacchetto dell'integrazione non
# e' importabile qui (il suo __init__ richiede homeassistant), e aggiungere la
# directory del pacchetto al pythonpath esporrebbe TUTTI i suoi moduli come
# nomi top-level (const, helpers, ...) pronti a collidere.
_spec = importlib.util.spec_from_file_location(
    "history_logic",
    pathlib.Path(__file__).parents[1]
    / "custom_components/zha_sonoff_quirks/history_logic.py",
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
build_record = _module.build_record
classify_reason = _module.classify_reason
session_liters = _module.session_liters
should_discard = _module.should_discard
source_from_context = _module.source_from_context


# ── classify_reason ──
def test_capacity_run_reaching_target_is_completed():
    assert classify_reason("capacity", 10, 300.0, 10.0) == "completed"


def test_capacity_run_one_liter_short_is_still_completed():
    # I report di volume sono in litri interi: 9/10 e' l'arrotondamento
    # dell'auto-chiusura, non uno stop manuale.
    assert classify_reason("capacity", 10, 300.0, 9.0) == "completed"


def test_capacity_run_stopped_early_is_manual_off():
    assert classify_reason("capacity", 200, 60.0, 51.0) == "manual_off"


def test_capacity_run_without_liters_is_manual_off():
    # Attribuzione ambigua (due canali aperti) -> niente litri -> non si puo'
    # dimostrare il completamento.
    assert classify_reason("capacity", 10, 300.0, None) == "manual_off"


def test_duration_run_reaching_target_is_completed():
    assert classify_reason("duration", 5, 300.0, None) == "completed"


def test_duration_run_closing_slightly_early_is_completed():
    assert classify_reason("duration", 5, 293.0, None) == "completed"


def test_duration_run_stopped_early_is_manual_off():
    assert classify_reason("duration", 5, 120.0, None) == "manual_off"


def test_duration_with_interval_uses_duration_rule():
    assert classify_reason("duration with interval", 2, 118.0, None) == "completed"


def test_unknown_mode_is_manual_off():
    assert classify_reason(None, None, 300.0, 10.0) == "manual_off"


# ── session_liters ──
def test_session_value_after_run_start_is_attributed():
    assert session_liters("12", 1000.0, 990.0, False) == 12.0


def test_session_value_within_clock_slack_is_attributed():
    # last_updated appena PRIMA dello start (jitter di scrittura stati HA).
    assert session_liters("3", 999.0, 1000.0, False) == 3.0


def test_stale_session_value_belongs_to_previous_run():
    # Il quirk conserva di proposito il totale della sessione precedente:
    # un valore piu' vecchio dello start non appartiene a questa corsa.
    assert session_liters("7", 900.0, 1000.0, False) is None


def test_ambiguous_dual_channel_run_gets_no_liters():
    assert session_liters("12", 1000.0, 990.0, True) is None


@pytest.mark.parametrize("bad", ["unavailable", "unknown", None, ""])
def test_non_numeric_session_value_gives_none(bad):
    assert session_liters(bad, 1000.0, 990.0, False) is None


def test_negative_session_value_clamps_to_zero():
    assert session_liters("-3", 1000.0, 990.0, False) == 0.0


# ── should_discard ──
def test_short_manual_flap_is_discarded():
    assert should_discard("manual", 1.2, 3, forced=False) is True


def test_short_integration_run_is_kept():
    assert should_discard("integration", 1.2, 3, forced=False) is False


def test_forced_finalize_is_always_kept():
    # Recovery da shutdown / force-finalize: registrare sempre.
    assert should_discard("manual", 0.5, 3, forced=True) is False


def test_normal_manual_run_is_kept():
    assert should_discard("manual", 45.0, 3, forced=False) is False


# ── build_record ──
def test_record_shape_and_rounding():
    record = build_record(
        start_iso="2026-08-10T10:00:00+00:00",
        end_iso="2026-08-10T10:05:04+00:00",
        duration_s=304.4,
        liters=12.345,
        mode="capacity",
        target=12.0,
        source="integration",
        reason="completed",
        channel="2",
    )
    assert record == {
        "start": "2026-08-10T10:00:00+00:00",
        "end": "2026-08-10T10:05:04+00:00",
        "duration_s": 304,
        "liters": 12.35,
        "mode": "capacity",
        "target": 12.0,
        "source": "integration",
        "reason": "completed",
        "channel": "2",
    }


def test_record_keeps_none_liters():
    record = build_record(
        start_iso=None,
        end_iso="2026-08-10T10:05:04+00:00",
        duration_s=10,
        liters=None,
        mode=None,
        target=None,
        source="manual",
        reason="shutdown",
        channel="1",
    )
    assert record["liters"] is None
    assert record["start"] is None


# ── run source (fork hydro-duo) ──
def test_record_carries_plan_index_only_for_scheduled_runs():
    record = build_record(
        start_iso=None,
        end_iso="2026-10-23T12:11:04+00:00",
        duration_s=660,
        liters=None,
        mode="duration",
        target=11.0,
        source="scheduled",
        reason="completed",
        channel="1",
        plan_index=0,
    )
    assert record["source"] == "scheduled"
    assert record["plan_index"] == 0


@pytest.mark.parametrize(
    ("user_id", "parent_id", "expected"),
    [
        ("abc", None, "manual"),  # UI / script via API
        (None, "def", "manual"),  # automation
        (None, None, "physical"),  # the valve reported it on its own
    ],
)
def test_source_from_context(user_id, parent_id, expected):
    assert source_from_context(user_id, parent_id) == expected


@pytest.mark.parametrize(
    ("source", "discarded"),
    [
        ("manual", True),
        ("physical", True),
        ("integration", False),
        ("scheduled", False),
    ],
)
def test_only_manual_or_physical_flaps_are_discarded(source, discarded):
    assert should_discard(source, 1.0, 3, False) is discarded
