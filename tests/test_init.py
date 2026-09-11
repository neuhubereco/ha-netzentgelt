"""End-to-End-Test im HA-Testharness: Zeit vorspulen, Zähler füttern, Entities prüfen.

Läuft mit pytest-homeassistant-custom-component (simulierte HA-Instanz), nicht
gegen eine echte Home-Assistant-Installation.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.netzentgelt.const import (
    CONF_ENERGY_ENTITY,
    CONF_POWER_ENTITY,
    DEFAULT_OPTIONS,
    DOMAIN,
)

ENERGY = "sensor.grid_import_energy"
POWER = "sensor.grid_import_power"
ENERGY_ATTRS = {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Custom Integrations in allen Tests dieses Moduls laden."""


class Meter:
    """Simulierter Zähler, der alle ``step`` Sekunden meldet."""

    def __init__(self, hass: HomeAssistant, freezer: FrozenDateTimeFactory, kwh: float) -> None:
        self.hass = hass
        self.freezer = freezer
        self.kwh = kwh

    def publish(self, value: str | None = None) -> None:
        state = f"{self.kwh:.4f}" if value is None else value
        self.hass.states.async_set(ENERGY, state, ENERGY_ATTRS)

    async def run(self, seconds: int, kw: float, step: int = 10, publish: bool = True) -> None:
        for _ in range(seconds // step):
            self.freezer.tick(timedelta(seconds=step))
            self.kwh += kw * step / 3600
            if publish:
                self.publish()
            async_fire_time_changed(self.hass, dt_util.utcnow())
            await self.hass.async_block_till_done()


def _entity_id(hass: HomeAssistant, entry: MockConfigEntry, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")
    assert entity_id is not None
    return entity_id


def _state(hass: HomeAssistant, entry: MockConfigEntry, key: str, platform: str = "sensor"):
    state = hass.states.get(_entity_id(hass, entry, platform, key))
    assert state is not None
    return state


async def _setup(hass: HomeAssistant, freezer: FrozenDateTimeFactory, start: datetime, power: bool = False):
    await hass.config.async_set_time_zone("Europe/Vienna")
    freezer.move_to(start)
    meter = Meter(hass, freezer, 68600.0)
    meter.publish()
    data = {CONF_ENERGY_ENTITY: ENERGY}
    if power:
        data[CONF_POWER_ENTITY] = POWER
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=ENERGY, title="Netzentgelt", data=data, options=dict(DEFAULT_OPTIONS)
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, meter


async def test_quarter_peak_billing_and_regression(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    vienna = dt_util.get_time_zone("Europe/Vienna")
    start = datetime(2026, 9, 11, 9, 56, 0, tzinfo=vienna)
    entry, meter = await _setup(hass, freezer, start)
    assert entry.state is ConfigEntryState.LOADED

    # 09:56–10:15:30 mit 2 kW; 09:45–10:00 ist angebrochen (Start) → ungültig
    await meter.run(19 * 60 + 30, kw=2.0)
    quarter = _state(hass, entry, "quarter_power")
    assert float(quarter.state) == pytest.approx(2.0, abs=0.01)
    assert quarter.attributes["last_quarter_valid"] is True
    assert quarter.attributes["quarter_start"].startswith("2026-09-11T10:00:00+02:00")
    assert quarter.attributes["invalid_quarters_month"] == 1

    peak = _state(hass, entry, "month_peak")
    assert float(peak.state) == pytest.approx(2.0, abs=0.01)
    assert peak.attributes["month"] == "2026-09"
    assert "2026-09" in peak.attributes["history"]

    # verrechnet: max(2.00, 2 kW, 20 % von 10 kW) = 2.0 → 2 × 33.82 / 12 = 5.64 €
    assert float(_state(hass, entry, "billed_power").state) == pytest.approx(2.0, abs=0.01)
    assert float(_state(hass, entry, "capacity_cost_month").state) == pytest.approx(5.64, abs=0.01)
    assert _state(hass, entry, "tariff_window").state == "snap"
    assert _state(hass, entry, "tariff_window").attributes["window_end"].startswith("2026-09-11T16:00")

    # Prognose aus Steigung (kein Leistungssensor): ~2 kW, Spielraum ~8 kW, keine Spitze
    forecast = _state(hass, entry, "forecast")
    assert float(forecast.state) == pytest.approx(2.0, abs=0.05)
    assert forecast.attributes["power_source"] == "energy_slope"
    assert float(_state(hass, entry, "headroom").state) > 7.0
    assert _state(hass, entry, "peak_imminent", "binary_sensor").state == "off"

    # Regression 31.08.2026: Quelle unavailable über die Grenze, dann 0, dann
    # wieder echter Stand → darf NIE eine Riesen-Spitze ergeben.
    await meter.run(12 * 60, kw=2.0)  # bis 10:27:30
    meter.publish(STATE_UNAVAILABLE)
    await meter.run(4 * 60, kw=2.0, publish=False)  # über 10:30 hinweg unavailable
    meter.publish("0")
    await meter.run(20, kw=2.0, publish=False)
    # Ungültige Viertelstunde: Wert bleibt beim letzten gültigen, Attribut sagt warum
    quarter = _state(hass, entry, "quarter_power")
    assert float(quarter.state) == pytest.approx(2.0, abs=0.01)
    assert quarter.attributes["quarter_start"] == "2026-09-11T10:00:00+02:00"
    assert quarter.attributes["last_quarter_start"] == "2026-09-11T10:15:00+02:00"
    assert quarter.attributes["last_quarter_valid"] is False
    assert quarter.attributes["last_quarter_reason"] == "source_unavailable"
    await meter.run(40 * 60, kw=2.0)  # wieder normal bis ~11:12
    peak = _state(hass, entry, "month_peak")
    assert float(peak.state) == pytest.approx(2.0, abs=0.01)
    # ungültig: 09:45 (Start), 10:15 (Ende unavailable), 10:30 (Start unavailable)
    assert peak.attributes["invalid_quarters_month"] == 3
    assert peak.attributes["valid_quarters_month"] == 2  # 10:00 und 10:45
    quarter = _state(hass, entry, "quarter_power")
    assert float(quarter.state) == pytest.approx(2.0, abs=0.01)
    assert quarter.attributes["quarter_start"] == "2026-09-11T10:45:00+02:00"
    assert quarter.attributes["last_quarter_valid"] is True

    # Hohe Last → Spitze droht, Spielraum negativ
    await meter.run(5 * 60, kw=14.0)
    assert _state(hass, entry, "peak_imminent", "binary_sensor").state == "on"
    assert float(_state(hass, entry, "headroom").state) < 0

    # Entladen: Entities unavailable, keine weiteren Updates
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert _state(hass, entry, "month_peak").state == STATE_UNAVAILABLE


async def test_power_sensor_is_used_for_forecast(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    vienna = dt_util.get_time_zone("Europe/Vienna")
    start = datetime(2026, 12, 1, 22, 58, 0, tzinfo=vienna)
    hass.states.async_set(POWER, "-500", {"unit_of_measurement": "W", "device_class": "power"})
    entry, meter = await _setup(hass, freezer, start, power=True)
    await meter.run(3 * 60, kw=0.0)
    forecast = _state(hass, entry, "forecast")
    assert forecast.attributes["power_source"] == "power_sensor"
    assert forecast.attributes["power_now_kw"] == 0.0  # Einspeisung zählt als 0
    assert _state(hass, entry, "tariff_window").state == "winap"

    hass.states.async_set(POWER, "12000", {"unit_of_measurement": "W", "device_class": "power"})
    await meter.run(60, kw=12.0)
    assert _state(hass, entry, "forecast").attributes["power_now_kw"] == 12.0
    assert _state(hass, entry, "peak_imminent", "binary_sensor").state == "on"


async def test_peak_survives_reload(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    vienna = dt_util.get_time_zone("Europe/Vienna")
    start = datetime(2026, 9, 30, 23, 13, 0, tzinfo=vienna)
    entry, meter = await _setup(hass, freezer, start)
    # 23:15–23:30 und 23:30–23:45 und 23:45–00:00 (letzte Viertelstunde September)
    await meter.run(32 * 60, kw=4.0)  # bis 23:45
    await meter.run(15 * 60 + 30, kw=6.0)  # bis 00:00:30 → 23:45-Viertelstunde fertig
    peak = _state(hass, entry, "month_peak")
    assert peak.attributes["month"] == "2026-10"
    history = peak.attributes["history"]
    assert history["2026-09"]["peak_kw"] == pytest.approx(6.0, abs=0.01)
    # Zuordnung über den Intervallbeginn: 23:45–00:00 gehört zum September
    assert history["2026-09"]["peak_start"] == "2026-09-30T23:45:00+02:00"
    assert peak.state == "unknown"  # Oktober hat noch keine gültige Viertelstunde

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    history = _state(hass, entry, "month_peak").attributes["history"]
    assert history["2026-09"]["peak_kw"] == pytest.approx(6.0, abs=0.01)


async def test_unchanged_meter_uses_state_reported(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Stehender Zähler (gleicher Wert, nur state_reported) ergibt gültige 0-kW-Viertelstunden."""
    vienna = dt_util.get_time_zone("Europe/Vienna")
    start = datetime(2026, 9, 12, 1, 58, 0, tzinfo=vienna)
    entry, meter = await _setup(hass, freezer, start)
    await meter.run(18 * 60, kw=0.0)  # bis 02:16, Wert ändert sich nie
    quarter = _state(hass, entry, "quarter_power")
    assert quarter.attributes["quarter_start"] == "2026-09-12T02:00:00+02:00"
    assert quarter.attributes["last_quarter_valid"] is True
    assert float(quarter.state) == 0.0
