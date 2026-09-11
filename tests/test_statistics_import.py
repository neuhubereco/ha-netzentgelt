"""Statistik-Import des Services ``import_load_profile`` gegen den echten Recorder.

Recorder mit In-Memory-SQLite (Fixture ``recorder_mock``, muss vor ``hass``
aufgelöst werden — deshalb eigenes Modul mit eigener Autouse-Fixture).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from .test_init import _entity_id, _setup
from .test_services import START, _call, _csv, _quarters


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock: Recorder, enable_custom_integrations: None) -> None:
    """Recorder vor Home Assistant starten, Custom Integrations laden."""


def _hour_rows(
    hass: HomeAssistant, statistic_id: str, start: datetime, end: datetime
) -> dict[datetime, dict]:
    from homeassistant.components.recorder.statistics import statistics_during_period

    rows = statistics_during_period(hass, start, end, {statistic_id}, "hour", None, {"mean", "min", "max"})
    return {dt_util.utc_from_timestamp(row["start"]): row for row in rows.get(statistic_id, [])}


async def test_statistics_only_before_first_existing_hour(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, tmp_path: Path
) -> None:
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import (
        async_import_statistics,
        get_metadata,
    )

    hass.config.config_dir = str(tmp_path)
    entry, _ = await _setup(hass, freezer, START)
    sensor_id = _entity_id(hass, entry, "sensor", "quarter_power")

    # "Eigene" Statistik ab 10.09. 10:00 UTC (so, wie sie der Recorder anlegen würde)
    metadata = {
        "mean_type": StatisticMeanType.ARITHMETIC,
        "has_sum": False,
        "name": None,
        "source": "recorder",
        "statistic_id": sensor_id,
        "unit_class": "power",
        "unit_of_measurement": "kW",
    }
    own = [
        {"start": datetime(2026, 9, 10, h, tzinfo=UTC), "mean": 99.0, "min": 99.0, "max": 99.0}
        for h in (10, 11)
    ]
    async_import_statistics(hass, metadata, own)  # type: ignore[arg-type]
    await async_wait_recording_done(hass)

    # Import 10.09. 06:00–13:45 UTC: 2 kW, in jeder Stunde eine Viertelstunde mit 6 kW
    rows = []
    for hour in range(6, 14):
        first = datetime(2026, 9, 10, hour, tzinfo=UTC)
        rows += [
            (first, 2.0),
            (first + timedelta(minutes=15), 6.0),
            *_quarters(first + timedelta(minutes=30), 2, 2.0),
        ]
    (tmp_path / "lastgang.csv").write_text(_csv(rows), encoding="utf-8")

    response = await _call(hass, entry.entry_id, "lastgang.csv")
    await async_wait_recording_done(hass)
    assert response["statistics_hours"] == 4  # 06, 07, 08, 09 UTC
    assert response["statistics_hours_skipped"] == 4
    assert response["statistics_until"] == "2026-09-10T12:00:00+02:00"  # = 10:00 UTC

    stats = await get_instance(hass).async_add_executor_job(
        _hour_rows,
        hass,
        sensor_id,
        datetime(2026, 9, 10, 0, tzinfo=UTC),
        datetime(2026, 9, 11, 0, tzinfo=UTC),
    )
    assert sorted(stats) == [datetime(2026, 9, 10, h, tzinfo=UTC) for h in (6, 7, 8, 9, 10, 11)]
    imported = stats[datetime(2026, 9, 10, 6, tzinfo=UTC)]
    assert imported["mean"] == pytest.approx(3.0)
    assert imported["min"] == pytest.approx(2.0)
    assert imported["max"] == pytest.approx(6.0)
    assert stats[datetime(2026, 9, 10, 10, tzinfo=UTC)]["mean"] == pytest.approx(99.0)  # eigene bleibt
    assert stats[datetime(2026, 9, 10, 11, tzinfo=UTC)]["mean"] == pytest.approx(99.0)

    meta = await get_instance(hass).async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={sensor_id})
    )
    stored = meta[sensor_id][1]
    assert stored["mean_type"] is StatisticMeanType.ARITHMETIC
    assert (stored["source"], stored["unit_of_measurement"], stored["unit_class"]) == (
        "recorder",
        "kW",
        "power",
    )
    assert stored["has_sum"] is False


async def test_statistics_without_existing_stop_before_entity_creation(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, tmp_path: Path
) -> None:
    from homeassistant.components.recorder import get_instance

    hass.config.config_dir = str(tmp_path)
    entry, _ = await _setup(hass, freezer, START)  # Entity angelegt 11.09. 07:56 UTC
    sensor_id = _entity_id(hass, entry, "sensor", "quarter_power")
    rows = _quarters(datetime(2026, 9, 11, 5, 0, tzinfo=UTC), 12, 3.0)  # 05:00–07:45 UTC
    (tmp_path / "lastgang.csv").write_text(_csv(rows), encoding="utf-8")

    response = await _call(hass, entry.entry_id, "lastgang.csv")
    await async_wait_recording_done(hass)
    assert response["statistics_hours"] == 2  # 05 und 06 UTC; 07 UTC = Stunde der Anlage
    assert response["statistics_hours_skipped"] == 1
    stats = await get_instance(hass).async_add_executor_job(
        _hour_rows,
        hass,
        sensor_id,
        datetime(2026, 9, 11, 0, tzinfo=UTC),
        datetime(2026, 9, 11, 9, tzinfo=UTC),
    )
    assert sorted(stats) == [datetime(2026, 9, 11, 5, tzinfo=UTC), datetime(2026, 9, 11, 6, tzinfo=UTC)]
    assert stats[datetime(2026, 9, 11, 5, tzinfo=UTC)]["mean"] == pytest.approx(3.0)


async def test_statistics_import_ignores_short_term_rows_of_new_sensor(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, tmp_path: Path
) -> None:
    """Regression: 5-Minuten-Statistik eines frisch angelegten Sensors ist kein Vorbestand.

    ``statistic_during_period`` mit offenem Beginn bezieht bei jungen Sensoren
    den Kopf aus der 5-Minuten-Tabelle — auch wenn er nach dem Importzeitraum
    liegt. Die frühere Prüfung meldete deshalb „Werte vor dem Import“ und
    importierte 0 Stunden (live HA 2026.9.1).
    """
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.db_schema import Statistics, StatisticsShortTerm
    from homeassistant.components.recorder.models import StatisticMeanType

    hass.config.config_dir = str(tmp_path)
    entry, _ = await _setup(hass, freezer, START)  # Entity angelegt 11.09. 07:56 UTC
    sensor_id = _entity_id(hass, entry, "sensor", "quarter_power")
    metadata = {
        "mean_type": StatisticMeanType.ARITHMETIC,
        "has_sum": False,
        "name": None,
        "source": "recorder",
        "statistic_id": sensor_id,
        "unit_class": "power",
        "unit_of_measurement": "kW",
    }
    # Wie beim echten Recorder kurz nach Anlage: erste Stunde 07:00 UTC, 5-Minuten-Werte ab 07:40
    instance = get_instance(hass)
    instance.async_import_statistics(
        metadata,  # type: ignore[arg-type]
        [{"start": datetime(2026, 9, 11, 7, tzinfo=UTC), "mean": 1.0, "min": 1.0, "max": 1.0}],
        Statistics,
    )
    instance.async_import_statistics(
        metadata,  # type: ignore[arg-type]
        [
            {"start": datetime(2026, 9, 11, 7, minute, tzinfo=UTC), "mean": 1.0, "min": 1.0, "max": 1.0}
            for minute in (40, 45, 50)
        ],
        StatisticsShortTerm,
    )
    await async_wait_recording_done(hass)

    rows = _quarters(datetime(2026, 9, 10, 20, 0, tzinfo=UTC), 16, 2.0)  # 20:00–23:45 UTC am Vortag
    (tmp_path / "lastgang.csv").write_text(_csv(rows), encoding="utf-8")
    response = await _call(hass, entry.entry_id, "lastgang.csv")
    await async_wait_recording_done(hass)
    assert response["statistics_hours"] == 4
    assert response["statistics_hours_skipped"] == 0
    stats = await instance.async_add_executor_job(
        _hour_rows,
        hass,
        sensor_id,
        datetime(2026, 9, 10, 0, tzinfo=UTC),
        datetime(2026, 9, 12, 0, tzinfo=UTC),
    )
    assert sorted(stats) == [datetime(2026, 9, 10, h, tzinfo=UTC) for h in (20, 21, 22, 23)] + [
        datetime(2026, 9, 11, 7, tzinfo=UTC)
    ]
    assert stats[datetime(2026, 9, 11, 7, tzinfo=UTC)]["mean"] == pytest.approx(1.0)  # eigene bleibt
