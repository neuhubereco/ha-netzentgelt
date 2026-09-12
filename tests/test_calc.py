"""Tests für die reine Rechenlogik (ohne Home Assistant lauffähig)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest

# calc.py direkt per Pfad laden: das Paket-__init__ importiert Home Assistant,
# die Rechenlogik soll aber auch ohne HA testbar sein.
_CALC_PATH = Path(__file__).parents[1] / "custom_components" / "netzentgelt" / "calc.py"
_spec = importlib.util.spec_from_file_location("netzentgelt_calc", _CALC_PATH)
assert _spec is not None and _spec.loader is not None
calc = importlib.util.module_from_spec(_spec)
sys.modules["netzentgelt_calc"] = calc
_spec.loader.exec_module(calc)

VIENNA = ZoneInfo("Europe/Vienna")


def local(*args: int) -> datetime:
    """Lokale Wiener Zeit."""
    return datetime(*args, tzinfo=VIENNA)


def engine_started(at: datetime, **kwargs: object) -> calc.QuarterEngine:
    engine = calc.QuarterEngine(**kwargs)  # type: ignore[arg-type]
    engine.start(at)
    return engine


def feed(engine: calc.QuarterEngine, events: list[tuple[datetime, float | None]]) -> list:
    """Samples (None = unavailable) und Grenzen chronologisch einspielen."""
    results = []
    for ts, value in events:
        if value is None:
            results += engine.mark_unavailable(ts)
        else:
            results += engine.add_sample(ts, value)
    return results


def run_quarter(
    engine: calc.QuarterEngine,
    samples: list[tuple[datetime, float | None]],
    boundaries: list[datetime],
    *,
    keep_startup: bool = False,
) -> list:
    """Samples und Grenzen gemischt, zeitlich sortiert, einspielen.

    Die Engine startet in den Tests vor der ersten Grenze; die dadurch
    angebrochene (ungültige) Viertelstunde davor wird standardmäßig
    ausgefiltert und in ``test_restart_mid_quarter_is_invalid`` eigens geprüft.
    """
    timeline: list[tuple[datetime, int, float | None]] = []
    for ts, value in samples:
        timeline.append((ts, 1, value))
    for ts in boundaries:
        timeline.append((ts, 0, None))
    timeline.sort(key=lambda item: (item[0], item[1]))
    results = []
    for ts, kind, value in timeline:
        if kind == 0:
            results += engine.boundary(ts)
        elif value is None:
            results += engine.mark_unavailable(ts)
        else:
            results += engine.add_sample(ts, value)
    if not keep_startup:
        first = min(boundaries)
        results = [r for r in results if r.start >= first]
    return results


# ----------------------------------------------------------------- Einheiten


def test_energy_units() -> None:
    assert calc.energy_to_kwh("1500", "Wh") == pytest.approx(1.5)
    assert calc.energy_to_kwh(2, "kWh") == 2
    assert calc.energy_to_kwh("0.5", "MWh") == pytest.approx(500)
    assert calc.energy_to_kwh("unavailable", "kWh") is None
    assert calc.energy_to_kwh("unknown", "kWh") is None
    assert calc.energy_to_kwh("nan", "kWh") is None
    assert calc.energy_to_kwh(1, "kW") is None
    assert calc.energy_to_kwh(1, None) is None


def test_power_units_and_negative() -> None:
    assert calc.power_to_kw("2500", "W") == pytest.approx(2.5)
    assert calc.power_to_kw(3, "kW") == 3
    assert calc.power_to_kw(-1200, "W") == 0.0
    assert calc.power_to_kw("abc", "W") is None


# ------------------------------------------------------------------ Messung


def test_normal_quarter_025_kwh_is_1_kw() -> None:
    t0 = local(2026, 9, 11, 10, 0)
    engine = engine_started(t0 - timedelta(minutes=1))
    samples = [(t0 + timedelta(minutes=m), 100.0 + 0.25 * m / 15) for m in range(-1, 17)]
    results = run_quarter(engine, samples, [t0, t0 + calc.QUARTER])
    valid = [r for r in results if r.valid]
    assert len(valid) == 1
    assert valid[0].start == t0
    assert valid[0].kw == pytest.approx(1.0)
    assert valid[0].energy_kwh == pytest.approx(0.25)


def test_interpolation_with_offset_samples() -> None:
    """Poll-Latenz: Samples 20 s vor und 40 s nach der Grenze."""
    t0 = local(2026, 9, 11, 10, 0)
    t1 = t0 + calc.QUARTER
    engine = engine_started(t0 - timedelta(minutes=2))
    # 2 kW konstant: 2/3600 kWh pro Sekunde. Zählerstand(t) = 50 + 2 * h
    def meter(ts: datetime) -> float:
        return 50.0 + 2.0 * (ts - (t0 - timedelta(minutes=2))).total_seconds() / 3600

    stamps = [
        t0 - timedelta(seconds=20),
        t0 + timedelta(seconds=40),
        t0 + timedelta(minutes=7),
        t1 - timedelta(seconds=50),
        t1 + timedelta(seconds=70),
    ]
    results = run_quarter(engine, [(s, meter(s)) for s in stamps], [t0, t1])
    valid = [r for r in results if r.valid]
    assert len(valid) == 1
    assert valid[0].kw == pytest.approx(2.0)
    # ohne Interpolation (Differenz der Samples) wären es 0.5 kWh * ... ≠ 2 kW
    naive = (meter(stamps[4]) - meter(stamps[1])) * 4
    assert naive != pytest.approx(2.0)


def test_interpolation_explicit_values() -> None:
    """Grenzwert = lineare Interpolation zwischen Sample davor und danach."""
    t0 = local(2026, 9, 11, 10, 0)
    t1 = t0 + calc.QUARTER
    engine = engine_started(t0 - timedelta(minutes=2))
    samples = [
        (t0 - timedelta(seconds=60), 100.0),
        (t0 + timedelta(seconds=60), 100.1),  # → Stand 10:00 = 100.05
        (t1 - timedelta(seconds=30), 101.0),
        (t1 + timedelta(seconds=90), 101.2),  # → Stand 10:15 = 101.05
    ]
    results = run_quarter(engine, samples, [t0, t1])
    assert len(results) == 1 and results[0].valid
    assert results[0].energy_kwh == pytest.approx(1.0)
    assert results[0].kw == pytest.approx(4.0)


def test_quarter_is_reported_only_after_first_sample_past_boundary() -> None:
    t0 = local(2026, 9, 11, 10, 0)
    t1 = t0 + calc.QUARTER
    engine = engine_started(t0 - timedelta(minutes=1))
    feed(engine, [(t0 - timedelta(seconds=10), 10.0)])
    assert engine.boundary(t0) == []
    feed(engine, [(t0 + timedelta(seconds=10), 10.001)])
    assert feed(engine, [(t1 - timedelta(seconds=10), 10.5)]) == []
    assert engine.boundary(t1) == []  # noch kein Sample nach t1
    results = feed(engine, [(t1 + timedelta(seconds=5), 10.51)])
    assert len(results) == 1 and results[0].valid


def test_unavailable_at_boundary_is_invalid_regression_274461() -> None:
    """Regression: 0 → 68 615 kWh an einer Grenze darf NIE 274 461 kW ergeben."""
    t0 = local(2026, 8, 31, 18, 0)
    t1 = t0 + calc.QUARTER
    t2 = t1 + calc.QUARTER
    engine = engine_started(t0 - timedelta(minutes=1))
    samples: list[tuple[datetime, float | None]] = [
        (t0 - timedelta(seconds=30), 68614.0),
        (t0 + timedelta(seconds=30), 68614.01),
        (t1 - timedelta(seconds=40), 68614.5),
        (t1 - timedelta(seconds=5), None),  # unavailable über die Grenze
        (t1 + timedelta(seconds=50), 68615.0),
        (t2 - timedelta(seconds=30), 68615.4),
        (t2 + timedelta(seconds=30), 68615.42),
    ]
    results = run_quarter(engine, samples, [t0, t1, t2])
    assert len(results) == 2
    assert all(not r.valid for r in results)
    assert results[0].reason == calc.REASON_SOURCE_UNAVAILABLE
    assert all(r.kw is None for r in results)


def test_source_reporting_zero_then_jump_is_never_a_peak() -> None:
    """Quelle meldet numerisch 0 statt unavailable (Bug des YAML-Pakets)."""
    t0 = local(2026, 8, 31, 18, 0)
    t1 = t0 + calc.QUARTER
    t2 = t1 + calc.QUARTER
    engine = engine_started(t0 - timedelta(minutes=1))
    samples: list[tuple[datetime, float | None]] = [
        (t0 - timedelta(seconds=30), 68614.0),
        (t0 + timedelta(seconds=30), 68614.01),
        (t1 - timedelta(seconds=20), 0.0),
        (t1 + timedelta(seconds=20), 0.0),
        (t1 + timedelta(seconds=60), 68614.5),
        (t2 - timedelta(seconds=30), 68614.9),
        (t2 + timedelta(seconds=30), 68614.92),
    ]
    results = run_quarter(engine, samples, [t0, t1, t2])
    assert results and all(not r.valid for r in results)
    assert max((r.kw or 0.0) for r in results) < 100


def test_counter_decrease_is_invalid() -> None:
    t0 = local(2026, 9, 11, 10, 0)
    t1 = t0 + calc.QUARTER
    engine = engine_started(t0 - timedelta(minutes=1))
    samples = [
        (t0 - timedelta(seconds=10), 500.0),
        (t0 + timedelta(seconds=10), 500.01),
        (t0 + timedelta(minutes=5), 12.0),  # Zählertausch / Reset
        (t1 - timedelta(seconds=10), 12.3),
        (t1 + timedelta(seconds=10), 12.31),
    ]
    results = run_quarter(engine, samples, [t0, t1])
    assert len(results) == 1
    assert not results[0].valid
    assert results[0].reason == calc.REASON_COUNTER_DECREASED


def test_plausibility_limit() -> None:
    t0 = local(2026, 9, 11, 10, 0)
    t1 = t0 + calc.QUARTER
    engine = engine_started(t0 - timedelta(minutes=1), plausibility_kw=60.0)
    # 16 kWh in 15 min = 64 kW > 60 kW; in kleinen Schritten, damit kein
    # Einzelsprung erkannt wird, sondern die Viertelstunden-Prüfung greift.
    samples = [(t0 + timedelta(minutes=m), 1000.0 + 16.0 * m / 15) for m in range(-1, 17)]
    results = run_quarter(engine, samples, [t0, t1])
    assert len(results) == 1
    assert not results[0].valid
    assert results[0].reason == calc.REASON_IMPLAUSIBLE_POWER

    engine = engine_started(t0 - timedelta(minutes=1), plausibility_kw=60.0)
    samples = [(t0 + timedelta(minutes=m), 1000.0 + 14.0 * m / 15) for m in range(-1, 17)]
    results = run_quarter(engine, samples, [t0, t1])
    assert results[0].valid and results[0].kw == pytest.approx(56.0)


def test_gap_longer_than_max_gap_is_invalid() -> None:
    t0 = local(2026, 9, 11, 10, 0)
    t1 = t0 + calc.QUARTER
    engine = engine_started(t0 - timedelta(minutes=30))
    samples = [
        (t0 - timedelta(minutes=11), 100.0),
        (t0 + timedelta(minutes=11), 101.5),  # 22 min Lücke, 1.5 kWh → raten verboten
        (t1 - timedelta(seconds=10), 101.0),
        (t1 + timedelta(seconds=10), 101.01),
    ]
    results = run_quarter(engine, samples, [t0, t1])
    assert len(results) == 1 and not results[0].valid
    assert results[0].reason == calc.REASON_SAMPLE_GAP


def test_sparse_samples_during_constant_load_are_valid() -> None:
    """Echter Fall 23.06.2026: nachts 12,4 kW, HA meldet nur alle 5–18 min."""
    t0 = local(2026, 6, 23, 2, 45)
    t1 = t0 + calc.QUARTER
    engine = engine_started(t0 - timedelta(minutes=30))

    def meter(ts: datetime) -> float:
        return 67221.0 + 12.4 * (ts - (t0 - timedelta(minutes=30))).total_seconds() / 3600

    stamps = [
        t0 - timedelta(minutes=9),
        t0 + timedelta(minutes=7),   # 16 min Lücke über 02:45
        t1 + timedelta(minutes=3),   # 11 min Lücke über 03:00
    ]
    results = run_quarter(engine, [(s, meter(s)) for s in stamps], [t0, t1])
    assert len(results) == 1 and results[0].valid
    assert results[0].kw == pytest.approx(12.4)


def test_long_gap_without_consumption_is_exact() -> None:
    """Zähler meldet nur bei Änderung: lange Lücke ohne Anstieg ist exakt 0 kW."""
    t0 = local(2026, 9, 11, 2, 0)
    t1 = t0 + calc.QUARTER
    engine = engine_started(t0 - timedelta(minutes=30))
    samples = [
        (t0 - timedelta(minutes=20), 100.0),
        (t1 + timedelta(minutes=20), 100.0),
    ]
    results = run_quarter(engine, samples, [t0, t1])
    assert len(results) == 1 and results[0].valid
    assert results[0].kw == pytest.approx(0.0)


def test_missing_sample_after_times_out() -> None:
    t0 = local(2026, 9, 11, 10, 0)
    t1 = t0 + calc.QUARTER
    engine = engine_started(t0 - timedelta(minutes=1), max_pending=timedelta(minutes=30))
    run_quarter(engine, [(t0 - timedelta(seconds=5), 1.0), (t0 + timedelta(seconds=5), 1.0)], [t0, t1])
    assert engine.tick(t1 + timedelta(minutes=10)) == []
    results = engine.tick(t1 + timedelta(minutes=31))
    assert len(results) == 1 and not results[0].valid
    assert results[0].reason == calc.REASON_NO_SAMPLE_AFTER


def test_restart_mid_quarter_is_invalid() -> None:
    """Start 10:07: 10:00–10:15 ungültig, 10:15–10:30 gültig."""
    started = local(2026, 9, 11, 10, 7)
    engine = engine_started(started)
    t1 = local(2026, 9, 11, 10, 15)
    t2 = t1 + calc.QUARTER
    samples = [(started + timedelta(minutes=m), 10.0 + 0.1 * m) for m in range(25)]
    results = run_quarter(engine, samples, [t1, t2], keep_startup=True)
    assert [r.valid for r in results] == [False, True]
    assert results[0].reason == calc.REASON_NO_START_BOUNDARY
    assert results[0].start == local(2026, 9, 11, 10, 0)
    assert results[1].kw == pytest.approx(6.0)  # 0.1 kWh/min = 1.5 kWh je Viertelstunde


def test_boundary_timer_slightly_late_or_early() -> None:
    t0 = local(2026, 9, 11, 10, 0)
    engine = engine_started(t0 - timedelta(minutes=1))
    samples = [(t0 + timedelta(minutes=m), 1.0 + 0.25 * m / 15) for m in range(-1, 17)]
    results = run_quarter(
        engine,
        samples,
        [t0 - timedelta(milliseconds=3), t0 + calc.QUARTER + timedelta(seconds=2)],
    )
    assert len(results) == 1 and results[0].valid
    assert results[0].start == t0


# ------------------------------------------------------------- Monatsspitze


def _q(start: datetime, kw: float | None) -> calc.QuarterResult:
    end = start + calc.QUARTER
    if kw is None:
        return calc.QuarterResult(start, end, False, reason="x")
    return calc.QuarterResult(start, end, True, kw=kw, energy_kwh=kw / 4)


def test_month_change_including_2345_quarter() -> None:
    tracker = calc.PeakTracker(tz=VIENNA)
    tracker.add(_q(local(2026, 8, 31, 12, 0), 5.0))
    tracker.add(_q(local(2026, 8, 31, 23, 45), 7.5))  # gehört noch zum August
    tracker.add(_q(local(2026, 9, 1, 0, 0), 3.0))  # erster September-Wert
    assert tracker.current == "2026-09"
    assert tracker.months["2026-08"].peak_kw == 7.5
    assert tracker.months["2026-08"].peak_start == local(2026, 8, 31, 23, 45).isoformat()
    assert tracker.months["2026-09"].peak_kw == 3.0


def test_late_quarter_of_old_month_after_rollover() -> None:
    """23:45-Viertelstunde wird erst nach dem Monatswechsel fertig."""
    tracker = calc.PeakTracker(tz=VIENNA)
    tracker.add(_q(local(2026, 8, 31, 12, 0), 5.0))
    tracker.roll_to("2026-09")
    tracker.add(_q(local(2026, 8, 31, 23, 45), 9.0))
    assert tracker.current == "2026-09"
    assert tracker.months["2026-08"].peak_kw == 9.0
    assert tracker.current_stats.peak_kw is None


def test_month_key_uses_local_time_and_utc_input() -> None:
    # 31.08. 22:30 UTC = 01.09. 00:30 Wien (Sommerzeit)
    assert calc.quarter_month_key(datetime(2026, 8, 31, 22, 30, tzinfo=UTC), VIENNA) == "2026-09"
    assert calc.quarter_month_key(datetime(2026, 8, 31, 21, 45, tzinfo=UTC), VIENNA) == "2026-08"


def test_invalid_quarters_never_enter_peak() -> None:
    tracker = calc.PeakTracker(tz=VIENNA)
    tracker.add(_q(local(2026, 9, 1, 0, 0), 2.0))
    tracker.add(_q(local(2026, 9, 1, 0, 15), None))
    stats = tracker.current_stats
    assert stats.peak_kw == 2.0
    assert stats.invalid_quarters == 1
    assert stats.valid_quarters == 1


def test_history_keeps_36_months_and_roundtrips() -> None:
    tracker = calc.PeakTracker(tz=VIENNA)
    for i in range(40):
        year, month = 2025 + (i // 12), i % 12 + 1
        tracker.add(_q(local(year, month, 2, 12, 0), float(i)))
    assert len(tracker.months) == 36
    assert min(tracker.months) == "2025-05"
    restored = calc.PeakTracker.from_dict(VIENNA, tracker.as_dict())
    assert restored.as_dict() == tracker.as_dict()
    assert next(iter(restored.history())) == "2028-04"


# --------------------------------------------------------------- Verrechnung


def test_tier_price_12_kw() -> None:
    p1, p2 = 33.82, 67.64
    assert calc.annual_capacity_cost(12.0, 10.0, p1, p2) == pytest.approx(10 * p1 + 2 * p2)
    assert calc.monthly_capacity_cost(12.0, 10.0, p1, p2) == calc.round_half_up((10 * p1 + 2 * p2) / 12, 2)
    assert calc.annual_capacity_cost(8.0, 10.0, p1, p2) == pytest.approx(8 * p1)
    assert calc.annual_capacity_cost(10.0, 10.0, p1, p2) == pytest.approx(10 * p1)


def test_minimum_billing() -> None:
    assert calc.minimum_billed_kw(12.0, 2.0) == pytest.approx(2.4)
    assert calc.minimum_billed_kw(4.0, 2.0) == pytest.approx(2.0)
    assert calc.billed_kw(1.2, 12.0, 2.0) == pytest.approx(2.4)
    assert calc.billed_kw(None, 10.0, 2.0) == pytest.approx(2.0)
    assert calc.billed_kw(7.456, 10.0, 2.0) == pytest.approx(7.46)


def test_round_half_up() -> None:
    assert calc.round_half_up(12.345, 2) == 12.35
    assert calc.round_half_up(12.344999999999999, 2) == 12.35  # Gleitkomma-Rauschen
    assert calc.round_half_up(2.675, 2) == 2.68  # round() liefert hier 2.67
    assert calc.round_half_up(12.344, 2) == 12.34
    assert calc.round_half_up(0.125, 2) == 0.13  # round() liefert 0.12


# ------------------------------------------------------------- SNAP / WiNAP


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (local(2027, 3, 31, 21, 59), calc.TARIFF_STANDARD),
        (local(2027, 3, 31, 22, 0), calc.TARIFF_WINAP),
        (local(2027, 3, 31, 23, 59), calc.TARIFF_WINAP),
        (local(2027, 4, 1, 3, 59), calc.TARIFF_WINAP),  # Nacht 31.3.→1.4. endet 04:00
        (local(2027, 4, 1, 4, 0), calc.TARIFF_STANDARD),
        (local(2027, 4, 1, 9, 59), calc.TARIFF_STANDARD),
        (local(2027, 4, 1, 10, 0), calc.TARIFF_SNAP),
        (local(2027, 4, 1, 15, 59), calc.TARIFF_SNAP),
        (local(2027, 4, 1, 16, 0), calc.TARIFF_STANDARD),
        (local(2027, 4, 1, 22, 30), calc.TARIFF_STANDARD),
        (local(2027, 9, 30, 15, 59), calc.TARIFF_SNAP),
        (local(2027, 9, 30, 23, 0), calc.TARIFF_STANDARD),
        (local(2027, 10, 1, 3, 59), calc.TARIFF_STANDARD),  # Nacht 30.9.→1.10. keine WiNAP
        (local(2027, 10, 1, 12, 0), calc.TARIFF_STANDARD),
        (local(2027, 10, 1, 22, 0), calc.TARIFF_WINAP),
        (local(2027, 10, 2, 3, 59), calc.TARIFF_WINAP),
        (local(2027, 1, 1, 0, 30), calc.TARIFF_WINAP),
        (local(2027, 1, 1, 12, 0), calc.TARIFF_STANDARD),
    ],
)
def test_tariff_window(moment: datetime, expected: str) -> None:
    assert calc.tariff_window(moment, VIENNA) == expected


def test_tariff_window_end() -> None:
    assert calc.tariff_window_end(local(2027, 3, 31, 23, 0), VIENNA) == local(2027, 4, 1, 4, 0)
    assert calc.tariff_window_end(local(2027, 4, 1, 4, 0), VIENNA) == local(2027, 4, 1, 10, 0)
    assert calc.tariff_window_end(local(2027, 4, 1, 12, 0), VIENNA) == local(2027, 4, 1, 16, 0)
    assert calc.tariff_window_end(local(2027, 9, 30, 16, 0), VIENNA) == local(2027, 10, 1, 22, 0)
    assert calc.tariff_window_end(local(2027, 1, 5, 12, 0), VIENNA) == local(2027, 1, 5, 22, 0)


def test_energy_price_only_if_set() -> None:
    assert calc.energy_price(calc.TARIFF_SNAP, 8.0, 0.0, 5.0) is None
    assert calc.energy_price(calc.TARIFF_WINAP, 8.0, 0.0, 5.0) == 5.0
    assert calc.energy_price(calc.TARIFF_STANDARD, 8.0, 0.0, 5.0) == 8.0


# ------------------------------------------------- Prognose / Spielraum


def test_headroom_formula() -> None:
    # Ziel 10 kW → 2.5 kWh je Viertelstunde. Verbraucht 1.5 kWh, 6 min Rest,
    # aktuell 4 kW: (2.5 − 1.5) / 0.1 h − 4 = 6 kW Spielraum.
    assert calc.headroom_formula(10.0, 1.5, 360, 4.0) == pytest.approx(6.0)
    # Bereits zu viel: negativ = drosseln
    assert calc.headroom_formula(10.0, 2.4, 360, 4.0) == pytest.approx(-3.0)
    # Restzeit < 30 s wird auf 30 s begrenzt
    assert calc.headroom_formula(10.0, 2.45, 5, 0.0) == pytest.approx(0.05 / (30 / 3600))


def test_headroom_cap() -> None:
    # Kurz vor Ende der Viertelstunde wird die Formel absurd groß (30 s Restzeit,
    # kaum verbraucht → mehrere hundert kW). Die Obergrenze schneidet das ab.
    ungekappt = calc.headroom_formula(10.0, 0.07, 30, 0.4)
    assert ungekappt > 200.0
    assert calc.headroom_formula(10.0, 0.07, 30, 0.4, cap_kw=60.0) == pytest.approx(60.0)
    # Unterhalb der Grenze ändert die Kappung nichts — auch nicht im Negativen.
    assert calc.headroom_formula(10.0, 1.5, 360, 4.0, cap_kw=60.0) == pytest.approx(6.0)
    assert calc.headroom_formula(10.0, 2.4, 360, 4.0, cap_kw=60.0) == pytest.approx(-3.0)


def test_headroom_cap_from_quarter() -> None:
    t0 = local(2026, 9, 11, 10, 0)
    engine = engine_started(t0 - timedelta(minutes=2))

    def meter(ts: datetime) -> float:
        return 20.0 + 0.4 * (ts - (t0 - timedelta(minutes=2))).total_seconds() / 3600

    stamps = [t0 - timedelta(minutes=2) + timedelta(seconds=10 * i) for i in range(100)]
    run_quarter(engine, [(s, meter(s)) for s in stamps], [t0])
    # 20 s vor Ende der Viertelstunde, Haus zieht nur 0.4 kW
    now = t0 + calc.QUARTER - timedelta(seconds=20)
    current = engine.current_quarter(now)
    assert current is not None
    assert calc.headroom_kw(current, 0.4, 10.0, now) > 200.0
    assert calc.headroom_kw(current, 0.4, 10.0, now, cap_kw=60.0) == pytest.approx(60.0)


def test_forecast_and_headroom_from_engine() -> None:
    t0 = local(2026, 9, 11, 10, 0)
    engine = engine_started(t0 - timedelta(minutes=2))
    # 6 kW konstant seit t0 - 2 min
    def meter(ts: datetime) -> float:
        return 20.0 + 6.0 * (ts - (t0 - timedelta(minutes=2))).total_seconds() / 3600

    stamps = [t0 - timedelta(minutes=2) + timedelta(seconds=10 * i) for i in range(60)]
    run_quarter(engine, [(s, meter(s)) for s in stamps], [t0])
    now = stamps[-1]
    current = engine.current_quarter(now)
    assert current is not None and not current.estimated_start
    power = engine.slope_kw()
    assert power == pytest.approx(6.0)
    assert calc.forecast_kw(current, power) == pytest.approx(6.0)
    # Ziel 10 kW: bis zum Ende dürfen im Schnitt (2.5 − verbraucht)/Rest_h kW
    # fließen; abzüglich der laufenden 6 kW ergibt das den Spielraum.
    used = 6.0 * (now - t0).total_seconds() / 3600
    rest_h = (t0 + calc.QUARTER - now).total_seconds() / 3600
    expected = (2.5 - used) / rest_h - 6.0
    assert calc.headroom_kw(current, power, 10.0, now) == pytest.approx(expected)
    assert calc.headroom_formula(10.0, used, rest_h * 3600, 6.0) == pytest.approx(expected)


def test_hysteresis() -> None:
    assert calc.hysteresis(False, 10.1, 10.0, 0.2) is True
    assert calc.hysteresis(False, 10.0, 10.0, 0.2) is False
    assert calc.hysteresis(True, 9.85, 10.0, 0.2) is True
    assert calc.hysteresis(True, 9.79, 10.0, 0.2) is False
    assert calc.hysteresis(True, None, 10.0, 0.2) is None


def test_forecast_after_start_mid_quarter_counts_unseen_part() -> None:
    """Start mitten in der Viertelstunde: der unbeobachtete Anfang zählt mit P_jetzt."""
    t0 = local(2026, 9, 11, 20, 45)
    engine = engine_started(t0 + timedelta(minutes=3))
    feed(engine, [(t0 + timedelta(minutes=3), 100.0), (t0 + timedelta(minutes=4), 100.04)])
    current = engine.current_quarter(t0 + timedelta(minutes=4))
    assert current is not None and current.estimated_start
    assert current.unseen_h == pytest.approx(3 / 60)
    # konstant 2,4 kW über die ganze Viertelstunde → Prognose 2,4 kW
    assert calc.forecast_kw(current, 2.4) == pytest.approx(2.4)
    # verbraucht bis jetzt: 0,04 kWh gesehen + 2,4 kW × 3 min ungesehen = 0,16 kWh; Rest 11 min
    expected = (10.0 / 4 - 0.16) / (11 / 60) - 2.4
    assert calc.headroom_kw(current, 2.4, 10.0, t0 + timedelta(minutes=4)) == pytest.approx(expected)
