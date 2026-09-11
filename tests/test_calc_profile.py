"""Tests für Lastprofil, Monats-History, Import-Parser und Merge (reine Logik, ohne HA)."""

from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest

if "netzentgelt_calc" in sys.modules:
    calc = sys.modules["netzentgelt_calc"]
else:  # pragma: no cover - nur bei Einzelaufruf dieser Datei
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


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _q(start: datetime, kw: float | None) -> calc.QuarterResult:
    start = start.astimezone(UTC)
    end = start + calc.QUARTER
    if kw is None:
        return calc.QuarterResult(start, end, False, reason="x")
    return calc.QuarterResult(start, end, True, kw=kw, energy_kwh=kw / 4)


# ------------------------------------------------------------------ Profil


def test_slot_labels_and_local_slot() -> None:
    labels = calc.slot_labels()
    assert len(labels) == 96
    assert labels[0] == "00:00" and labels[41] == "10:15" and labels[95] == "23:45"
    # 10:15 Sommerzeit = 08:15 UTC
    assert calc.local_slot(utc(2026, 9, 11, 8, 15), VIENNA) == (local(2026, 9, 11).date(), 41)


def test_profile_fall_back_day_merges_double_slots() -> None:
    """25.10.2026: 02:00–02:59 gibt es zweimal (Sommer- und Winterzeit)."""
    days = calc.DayProfiles(tz=VIENNA)
    tracker = calc.PeakTracker(tz=VIENNA)
    first = utc(2026, 10, 25, 0, 0)  # 02:00 MESZ
    second = utc(2026, 10, 25, 1, 0)  # 02:00 MEZ
    assert calc.local_slot(first, VIENNA)[1] == calc.local_slot(second, VIENNA)[1] == 8
    for result in (_q(first, 3.0), _q(second, 5.0)):
        days.add(result)
        tracker.add(result)
    today = local(2026, 10, 25).date()
    assert days.get(today)[8] == 5.0  # Maximum der beiden
    profile = tracker.current_stats.combined_profile()
    assert profile.max[8] == 5.0
    assert profile.count[8] == 2
    assert profile.avg()[8] == pytest.approx(4.0)  # korrektes Mittel über beide


def test_profile_spring_forward_day_leaves_missing_slots_empty() -> None:
    """29.03.2026: 02:00–02:59 existiert nicht → Slots 8–11 bleiben leer."""
    days = calc.DayProfiles(tz=VIENNA)
    start = local(2026, 3, 29, 0, 0).astimezone(UTC)
    end = local(2026, 3, 30, 0, 0).astimezone(UTC)
    count = 0
    moment = start
    while moment < end:
        days.add(_q(moment, 1.0))
        moment += calc.QUARTER
        count += 1
    assert count == 92
    values = days.get(local(2026, 3, 29).date())
    assert values[8:12] == [None, None, None, None]
    assert all(v == 1.0 for i, v in enumerate(values) if not 8 <= i < 12)


def test_day_profiles_invalid_prune_fill_and_roundtrip() -> None:
    days = calc.DayProfiles(tz=VIENNA)
    days.add(_q(local(2026, 9, 10, 23, 45), 2.0))
    days.add(_q(local(2026, 9, 11, 0, 0), None))  # ungültig → bleibt None
    days.add(_q(local(2026, 9, 11, 0, 15), 4.0))
    assert days.get(local(2026, 9, 11).date())[0] is None
    # Import füllt nur leere Slots
    assert days.fill(local(2026, 9, 11, 0, 0), 9.0) is True
    assert days.fill(local(2026, 9, 11, 0, 15), 9.0) is False
    assert days.get(local(2026, 9, 11).date())[:2] == [9.0, 4.0]
    days.add(_q(local(2026, 9, 9, 12, 0), 1.0))
    days.prune(local(2026, 9, 11).date())
    assert sorted(days.days) == ["2026-09-10", "2026-09-11"]
    restored = calc.DayProfiles.from_dict(VIENNA, days.as_dict())
    assert restored.days == days.days
    assert calc.DayProfiles.from_dict(VIENNA, {"kaputt": [1], "2026-09-11": [1, 2]}).days == {}


def test_month_profile_roundtrip_and_v01_store_compat() -> None:
    tracker = calc.PeakTracker(tz=VIENNA)
    tracker.add(_q(local(2026, 9, 1, 10, 0), 2.5))
    tracker.add(_q(local(2026, 9, 2, 10, 0), 3.5))
    stats = tracker.current_stats
    assert stats.profile.max[40] == 3.5
    assert stats.profile.avg()[40] == pytest.approx(3.0)
    restored = calc.PeakTracker.from_dict(VIENNA, tracker.as_dict())
    assert restored.current_stats.profile.avg()[40] == pytest.approx(3.0)
    assert restored.current_stats.profile.count[40] == 2
    stored = tracker.as_dict()["months"]["2026-09"]
    assert len(stored["profile_max"]) == 96 and len(stored["profile_avg"]) == 96
    # Store aus v0.1 (ohne Profil) lädt ohne Fehler
    old = calc.MonthStats.from_dict(
        {
            "peak_kw": 4.2,
            "peak_start": "2026-08-01T10:00:00+00:00",
            "valid_quarters": 3,
            "invalid_quarters": 1,
        }
    )
    assert old.peak_kw == 4.2 and old.profile.total == 0 and old.source == "measured"


# ---------------------------------------------------------------- Merge


def _imported(months: dict[str, list[tuple[datetime, float]]]) -> dict[str, calc.ImportedMonth]:
    quarters = {start.astimezone(UTC): kw for values in months.values() for start, kw in values}
    return calc.summarize_months(quarters, VIENNA)


def test_merge_rules_imported_mixed_overwrite_skipped() -> None:
    tracker = calc.PeakTracker(tz=VIENNA)
    # eigene Messung im August (Spitze 6 kW) und September
    tracker.add(_q(local(2026, 8, 20, 18, 0), 6.0))
    tracker.add(_q(local(2026, 9, 3, 18, 0), 4.0))
    imported = _imported(
        {
            "2026-07": [(local(2026, 7, 5, 11, 0), 8.0), (local(2026, 7, 6, 11, 0), 2.0)],
            "2026-08": [(local(2026, 8, 2, 19, 0), 7.5)],
            "2026-09": [(local(2026, 9, 1, 12, 0), 3.0)],
            "2026-10": [(local(2026, 10, 1, 12, 0), 3.0)],  # nach dem laufenden Monat
            "2023-01": [(local(2023, 1, 1, 12, 0), 3.0)],  # älter als 36 Monate
        }
    )
    report = calc.merge_import(tracker, imported, "2026-09", overwrite=False)
    assert report.imported == ["2026-07"]
    assert report.merged == ["2026-08", "2026-09"]
    assert report.overwritten == []
    assert report.skipped == ["2023-01", "2026-10"]

    history = tracker.history()
    assert history["2026-07"]["source"] == "imported"
    assert history["2026-07"]["peak_kw"] == 8.0
    assert history["2026-07"]["imported_quarters"] == 2
    assert history["2026-08"]["source"] == "mixed"
    assert history["2026-08"]["peak_kw"] == 7.5  # Maximum aus Messung (6) und Import (7,5)
    assert history["2026-08"]["valid_quarters"] == 1
    assert history["2026-09"]["peak_kw"] == 4.0  # Messung höher als Import
    assert "profile_max" not in history["2026-08"]

    # Erneuter Import derselben Daten ändert nichts (idempotent)
    before = tracker.as_dict()
    calc.merge_import(tracker, imported, "2026-09", overwrite=False)
    assert tracker.as_dict() == before

    # overwrite ersetzt die eigene Messung
    report = calc.merge_import(tracker, {"2026-08": imported["2026-08"]}, "2026-09", overwrite=True)
    assert report.overwritten == ["2026-08"]
    august = tracker.months["2026-08"]
    assert august.source == "imported" and august.peak_kw is None and august.valid_quarters == 0
    assert august.combined_peak()[0] == 7.5


def test_imported_month_then_measured_becomes_mixed_and_profiles_combine() -> None:
    tracker = calc.PeakTracker(tz=VIENNA)
    tracker.roll_to("2026-09")
    imported = _imported({"2026-09": [(local(2026, 9, 1, 10, 0), 2.0)]})
    report = calc.merge_import(tracker, imported, "2026-09")
    assert report.imported == ["2026-09"]  # laufender Monat ohne eigene Viertelstunden
    tracker.add(_q(local(2026, 9, 11, 10, 0), 6.0))
    stats = tracker.current_stats
    assert stats.source == "mixed"
    profile = stats.combined_profile()
    assert profile.max[40] == 6.0
    assert profile.avg()[40] == pytest.approx(4.0)
    assert stats.combined_peak()[0] == 6.0
    restored = calc.PeakTracker.from_dict(VIENNA, tracker.as_dict())
    assert restored.current_stats.source == "mixed"
    assert restored.current_stats.combined_profile().avg()[40] == pytest.approx(4.0)


def test_month_add() -> None:
    assert calc.month_add("2026-09", -35) == "2023-10"
    assert calc.month_add("2026-12", 1) == "2027-01"
    assert calc.month_add("2026-01", -1) == "2025-12"


# --------------------------------------------------------------- Parser

NETZ_OOE = (
    '\ufeff"Datum";"kWh";"kW";"Status";\n'
    '"01.01.2026 00:00";0,354;1,416;"VALID";\n'
    '"01.01.2026 00:15";0,300;1,200;"VALID";\n'
    '"01.01.2026 00:30";2,125;8,500;"VALID";\n'
    '"31.01.2026 23:45";0,500;2,000;"VALID";\n'
    '"01.02.2026 00:00";1,000;4,000;"VALID";\n'
)


def test_parse_netz_ooe_export() -> None:
    profile = calc.parse_load_profile(NETZ_OOE, VIENNA)
    assert profile.rows == 5
    assert profile.rows_skipped == 0
    assert profile.value_column == "kW" and profile.value_column_source == "header"
    # Zeitstempel = Beginn, Ortszeit (MEZ = UTC+1)
    assert profile.first == utc(2025, 12, 31, 23, 0)
    assert profile.quarters[utc(2025, 12, 31, 23, 0)] == pytest.approx(1.416)
    months = calc.summarize_months(profile.quarters, VIENNA)
    assert sorted(months) == ["2026-01", "2026-02"]
    january = months["2026-01"]
    assert january.quarters == 4
    assert january.peak_kw == pytest.approx(8.5)
    assert january.peak_start == utc(2025, 12, 31, 23, 30).isoformat()
    assert january.profile.max[2] == pytest.approx(8.5)  # 00:30 lokal
    assert january.profile.max[95] == pytest.approx(2.0)  # 23:45 am 31.01. gehört zum Jänner
    assert months["2026-02"].quarters == 1


def test_parse_iso_with_offsets_and_comma_delimiter() -> None:
    text = (
        "timestamp,power_kw\n"
        "2026-03-01T10:00:00+01:00,3.5\n"
        "2026-03-01T09:15:00Z,4.25\n"
        "2026-03-01 11:00,1.0\n"  # ISO ohne Zone → Ortszeit
    )
    profile = calc.parse_load_profile(text, VIENNA)
    assert profile.value_column == "kW"
    assert profile.quarters == {
        utc(2026, 3, 1, 9, 0): 3.5,
        utc(2026, 3, 1, 9, 15): 4.25,
        utc(2026, 3, 1, 10, 0): 1.0,
    }


def test_parse_kwh_only_header_is_multiplied_by_four() -> None:
    text = "Zeitpunkt;Verbrauch [kWh]\n01.06.2026 12:00;0,5\n01.06.2026 12:15;1.234,5\n"
    profile = calc.parse_load_profile(text, VIENNA)
    assert profile.value_column == "kWh"
    assert profile.quarters[utc(2026, 6, 1, 10, 0)] == pytest.approx(2.0)
    assert profile.quarters[utc(2026, 6, 1, 10, 15)] == pytest.approx(4938.0)  # 1.234,5 kWh × 4


def test_parse_without_header_detects_kwh_kw_pair_or_assumes_kwh() -> None:
    pair = "01.06.2026 12:00;0,25;1,0\n01.06.2026 12:15;0,5;2,0\n"
    profile = calc.parse_load_profile(pair, VIENNA)
    assert (profile.value_column, profile.value_column_source) == ("kW", "detected")
    assert profile.quarters[utc(2026, 6, 1, 10, 15)] == pytest.approx(2.0)

    single = "01.06.2026 12:00;0,25\n01.06.2026 12:15;0,5\n"
    profile = calc.parse_load_profile(single, VIENNA)
    assert (profile.value_column, profile.value_column_source) == ("kWh", "assumed")
    assert profile.quarters[utc(2026, 6, 1, 10, 15)] == pytest.approx(2.0)

    unrelated = "01.06.2026 12:00;0,25;7\n01.06.2026 12:15;0,5;7\n"
    assert calc.parse_load_profile(unrelated, VIENNA).value_column_source == "assumed"


def test_parse_skips_garbage_rows() -> None:
    text = (
        "Zählpunkt: BEISPIEL\n"  # Metadaten vor der Kopfzeile
        "Zeitraum: 01.06.2026 - 02.06.2026\n"
        '"Datum";"kWh";"kW";"Status";\n'
        '"01.06.2026 00:00";0,1;0,4;"VALID";\n'
        "Summe;;;\n"  # Müll
        '"01.06.2026 00:15";;;"MISSING";\n'  # leerer Wert
        '"01.06.2026 00:30";-0,1;-0,4;"VALID";\n'  # negativ
        '"01.06.2026 00:37";0,1;0,4;"VALID";\n'  # nicht viertelstündlich
        '"31.06.2026 00:00";0,1;0,4;"VALID";\n'  # ungültiges Datum
        '"01.06.2026 00:45";0,2;0,8;"VALID";\n'
        '"01.06.2026 00:45";0,3;1,2;"VALID";\n'  # doppelt → letzter Wert
    )
    profile = calc.parse_load_profile(text, VIENNA)
    assert profile.rows == 3
    assert profile.duplicates == 1
    assert profile.rows_skipped == 5  # Summe, leer, negativ, :37, 31.06.
    assert profile.quarters == {utc(2026, 5, 31, 22, 0): 0.4, utc(2026, 5, 31, 22, 45): 1.2}


def test_parse_dst_fall_back_repeated_hour_and_spring_gap() -> None:
    clocks = ("01:45", "02:00", "02:15", "02:00", "02:15", "03:00")
    fall = "".join(f"25.10.2026 {clock};0,25;1,0;VALID\n" for clock in clocks)
    profile = calc.parse_load_profile("Datum;kWh;kW;Status\n" + fall, VIENNA)
    assert list(profile.quarters) == [
        utc(2026, 10, 24, 23, 45),
        utc(2026, 10, 25, 0, 0),  # 02:00 MESZ
        utc(2026, 10, 25, 0, 15),
        utc(2026, 10, 25, 1, 0),  # 02:00 MEZ (zweites Auftreten)
        utc(2026, 10, 25, 1, 15),
        utc(2026, 10, 25, 2, 0),
    ]
    spring = "Datum;kW\n29.03.2026 01:45;1\n29.03.2026 02:00;1\n29.03.2026 03:00;1\n"
    profile = calc.parse_load_profile(spring, VIENNA)
    assert profile.rows_skipped == 1  # 02:00 gibt es an diesem Tag nicht
    assert list(profile.quarters) == [utc(2026, 3, 29, 0, 45), utc(2026, 3, 29, 1, 0)]


def test_parse_timestamp_is_end_including_2400() -> None:
    text = "Ende;kW\n01.06.2026 00:15;1\n01.06.2026 24:00;2\n"
    profile = calc.parse_load_profile(text, VIENNA, timestamp_is_end=True)
    assert profile.quarters == {
        utc(2026, 5, 31, 22, 0): 1.0,  # 00:00–00:15 lokal
        utc(2026, 6, 1, 21, 45): 2.0,  # 23:45–24:00 lokal
    }


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "no_data"),
        ("\ufeff\n\n", "no_data"),
        ("Datum;kWh;kW\nkeine;Daten;hier\n", "no_data"),
        ("Datum;kW\n01.06.2026 00:00;abc\n", "no_valid_rows"),
        ("01.06.2026 00:00;VALID\n", "no_value_column"),
    ],
)
def test_parse_errors(text: str, reason: str) -> None:
    with pytest.raises(calc.LoadProfileError) as err:
        calc.parse_load_profile(text, VIENNA)
    assert err.value.reason == reason


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0,354", 0.354), ("1.234,5", 1234.5), ("1,234.5", 1234.5), ("12", 12.0), ("1e3", 1000.0)],
)
def test_parse_number(text: str, expected: float) -> None:
    assert calc.parse_number(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "abc", "1,2,3", "nan", "inf", "VALID"])
def test_parse_number_rejects(text: str) -> None:
    assert calc.parse_number(text) is None


def test_hourly_statistics() -> None:
    quarters = {
        utc(2026, 6, 1, 10, 0): 1.0,
        utc(2026, 6, 1, 10, 15): 2.0,
        utc(2026, 6, 1, 10, 30): 3.0,
        utc(2026, 6, 1, 10, 45): 6.0,
        utc(2026, 6, 1, 11, 15): 5.0,  # Stunde nur teilweise vorhanden
    }
    hours = calc.hourly_statistics(quarters)
    assert [h.start for h in hours] == [utc(2026, 6, 1, 10), utc(2026, 6, 1, 11)]
    assert (hours[0].mean, hours[0].min, hours[0].max, hours[0].quarters) == (3.0, 1.0, 6.0, 4)
    assert (hours[1].mean, hours[1].quarters) == (5.0, 1)
    assert all(h.start.minute == 0 and h.start.tzinfo is UTC for h in hours)


def test_summarize_months_peak_tie_keeps_first() -> None:
    quarters = {utc(2026, 6, 1, 10, 0): 5.0, utc(2026, 6, 2, 10, 0): 5.0}
    june = calc.summarize_months(quarters, VIENNA)["2026-06"]
    assert june.peak_start == utc(2026, 6, 1, 10, 0).isoformat()
