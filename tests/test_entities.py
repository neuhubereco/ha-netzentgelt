"""Tests für Einstellungs-Entities, Peak-Shaving-Schalter, Lastprofil und History-Kosten.

Läuft mit pytest-homeassistant-custom-component (simulierte HA-Instanz).
"""

from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.netzentgelt.const import (
    CONF_HYSTERESIS_KW,
    CONF_PLAUSIBILITY_KW,
    CONF_PRICE_TIER1,
    CONF_TARGET_KW,
)

from .test_init import _entity_id, _setup, _state

VIENNA = dt_util.get_time_zone("Europe/Vienna")

# Entity-IDs bei Gerätename „Netzentgelt“ und Systemsprache Deutsch (README, Dashboards)
GERMAN_IDS = {
    "quarter_power": "sensor.netzentgelt_15_min_leistung",
    "forecast": "sensor.netzentgelt_prognose_viertelstunde",
    "month_peak": "sensor.netzentgelt_monatsspitze",
    "billed_power": "sensor.netzentgelt_verrechnete_leistung",
    "capacity_cost_month": "sensor.netzentgelt_leistungspreis_monat_geschatzt",
    "headroom": "sensor.netzentgelt_spielraum",
    "load_profile": "sensor.netzentgelt_lastprofil",
    "tariff_window": "sensor.netzentgelt_tarifzeitfenster",
    "peak_imminent": "binary_sensor.netzentgelt_spitze_droht",
    "peak_shaving": "switch.netzentgelt_peak_shaving_aktiv",
    "target_kw": "number.netzentgelt_ziel_leistung",
    "tier_limit_kw": "number.netzentgelt_staffelgrenze",
    "agreed_kw": "number.netzentgelt_vereinbarte_leistung",
    "minimum_kw": "number.netzentgelt_mindestleistung",
    "price_tier1": "number.netzentgelt_leistungspreis_stufe_1",
    "price_tier2": "number.netzentgelt_leistungspreis_stufe_2",
    "hysteresis_kw": "number.netzentgelt_hysterese",
}

# Dieselben Entities bei Systemsprache Englisch (examples/dashboard-apexcharts.en.yaml).
# Beitrag aus der Community (Issue #3): Die Beispiel-Dashboards passten nur zu deutschen
# Installationen, weil die IDs aus der Systemsprache entstehen.
ENGLISH_IDS = {
    "quarter_power": "sensor.netzentgelt_15_minute_power",
    "forecast": "sensor.netzentgelt_15_minute_forecast",
    "month_peak": "sensor.netzentgelt_monthly_peak",
    "billed_power": "sensor.netzentgelt_billed_power",
    "capacity_cost_month": "sensor.netzentgelt_capacity_charge_month_estimate",
    "headroom": "sensor.netzentgelt_headroom",
    "load_profile": "sensor.netzentgelt_load_profile",
    "tariff_window": "sensor.netzentgelt_tariff_window",
    "peak_imminent": "binary_sensor.netzentgelt_peak_imminent",
    "peak_shaving": "switch.netzentgelt_peak_shaving_active",
    "target_kw": "number.netzentgelt_target_power",
    "tier_limit_kw": "number.netzentgelt_tier_limit",
    "agreed_kw": "number.netzentgelt_agreed_capacity",
    "minimum_kw": "number.netzentgelt_minimum_billed_power",
    "price_tier1": "number.netzentgelt_capacity_price_tier_1",
    "price_tier2": "number.netzentgelt_capacity_price_tier_2",
    "hysteresis_kw": "number.netzentgelt_hysteresis",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Custom Integrations in allen Tests dieses Moduls laden."""


async def _set_number(hass: HomeAssistant, entity_id: str, value: float) -> None:
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
    )
    await hass.async_block_till_done()


async def test_entity_ids_in_german(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Die Übersetzungen ergeben genau die im README/Dashboard verwendeten IDs."""
    hass.config.language = "de"
    entry, _ = await _setup(hass, freezer, datetime(2026, 9, 11, 9, 56, tzinfo=VIENNA))
    registry = er.async_get(hass)
    ids = {
        e.unique_id.removeprefix(f"{entry.entry_id}_"): e.entity_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert ids == GERMAN_IDS
    # Ziel-Leistung prominent, übrige Einstellungen in der Kategorie Konfiguration
    assert registry.async_get("number.netzentgelt_ziel_leistung").entity_category is None
    assert registry.async_get("number.netzentgelt_hysterese").entity_category == "config"
    target = hass.states.get("number.netzentgelt_ziel_leistung")
    # Bereich wie im Options-Flow: 0,5 kW bis zur Plausibilitätsgrenze (Standard 60 kW)
    assert target.attributes["min"] == 0.5 and target.attributes["max"] == 60
    assert target.attributes["step"] == 0.1 and target.attributes["mode"] == "slider"


async def test_entity_ids_in_english(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Auf einer englischen Installation entstehen genau die IDs des englischen Dashboards."""
    hass.config.language = "en"
    entry, _ = await _setup(hass, freezer, datetime(2026, 9, 11, 9, 56, tzinfo=VIENNA))
    registry = er.async_get(hass)
    ids = {
        e.unique_id.removeprefix(f"{entry.entry_id}_"): e.entity_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert ids == ENGLISH_IDS


async def test_number_writes_options_live_without_reload(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    entry, meter = await _setup(hass, freezer, datetime(2026, 9, 11, 9, 56, tzinfo=VIENNA))
    await meter.run(9 * 60, kw=2.0)  # bis 10:05, Viertelstunde ab 10:00 läuft
    coordinator = entry.runtime_data
    target_id = _entity_id(hass, entry, "number", "target_kw")
    assert float(hass.states.get(target_id).state) == 10.0

    await _set_number(hass, target_id, 7.5)
    assert entry.options[CONF_TARGET_KW] == 7.5
    assert entry.runtime_data is coordinator  # kein Neuladen
    assert coordinator.options[CONF_TARGET_KW] == 7.5
    assert float(hass.states.get(target_id).state) == 7.5
    assert _state(hass, entry, "peak_imminent", "binary_sensor").attributes["target_kw"] == 7.5
    assert _state(hass, entry, "forecast").attributes["target_kw"] == 7.5

    # Die laufende Viertelstunde bleibt gültig (bei einem Reload wäre sie verworfen worden)
    await meter.run(11 * 60, kw=2.0)  # über 10:15 hinweg
    quarter = _state(hass, entry, "quarter_power")
    assert quarter.attributes["quarter_start"] == "2026-09-11T10:00:00+02:00"
    assert quarter.attributes["last_quarter_valid"] is True

    # Preisänderung wirkt sofort auf den Leistungspreis (2 kW × 60 €/kW/a / 12 = 10 €)
    await _set_number(hass, _entity_id(hass, entry, "number", CONF_PRICE_TIER1), 60.0)
    assert float(_state(hass, entry, "capacity_cost_month").state) == pytest.approx(10.0)
    assert entry.runtime_data is coordinator


async def test_number_rejects_hysteresis_not_below_target(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    entry, _ = await _setup(hass, freezer, datetime(2026, 9, 11, 9, 56, tzinfo=VIENNA))
    with pytest.raises(ServiceValidationError) as err:
        await _set_number(hass, _entity_id(hass, entry, "number", CONF_HYSTERESIS_KW), 10.0)
    assert err.value.translation_key == "hysteresis_too_large"
    assert entry.options[CONF_HYSTERESIS_KW] == 0.2
    # Slider-Grenzen = Options-Bereich: 0,5 kW bis Plausibilitätsgrenze (60 kW)
    target_id = _entity_id(hass, entry, "number", CONF_TARGET_KW)
    with pytest.raises(ServiceValidationError):
        await _set_number(hass, target_id, 0.4)
    with pytest.raises(ServiceValidationError):
        await _set_number(hass, target_id, 61.0)
    await _set_number(hass, target_id, 45.0)  # früher auf 30 kW begrenzt
    assert entry.options[CONF_TARGET_KW] == 45.0
    await _set_number(hass, target_id, 1.0)
    assert entry.options[CONF_TARGET_KW] == 1.0


async def test_target_slider_follows_plausibility_option(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Ziel 80 kW im Options-Flow (Plausibilität 150) ist auch am Schieberegler einstellbar."""
    entry, _ = await _setup(hass, freezer, datetime(2026, 9, 11, 9, 56, tzinfo=VIENNA))
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_PLAUSIBILITY_KW: 150.0, CONF_TARGET_KW: 80.0}
    )
    await hass.async_block_till_done()  # Plausibilität ist strukturell → Neuladen
    target_id = _entity_id(hass, entry, "number", CONF_TARGET_KW)
    state = hass.states.get(target_id)
    assert float(state.state) == 80.0 and state.attributes["max"] == 150
    await _set_number(hass, target_id, 120.0)
    assert entry.options[CONF_TARGET_KW] == 120.0


async def test_peak_shaving_switch_defaults_off(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    entry, _ = await _setup(hass, freezer, datetime(2026, 9, 11, 9, 56, tzinfo=VIENNA))
    switch_id = _entity_id(hass, entry, "switch", "peak_shaving")
    assert hass.states.get(switch_id).state == STATE_OFF
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch_id}, blocking=True)
    assert hass.states.get(switch_id).state == STATE_ON
    # Neuladen: Zustand bleibt (RestoreEntity)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(switch_id).state == STATE_ON


async def test_peak_shaving_switch_restores_after_restart(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    mock_restore_cache(hass, [State("switch.netzentgelt_peak_shaving_active", STATE_ON)])
    entry, _ = await _setup(hass, freezer, datetime(2026, 9, 11, 9, 56, tzinfo=VIENNA))
    assert _entity_id(hass, entry, "switch", "peak_shaving") == "switch.netzentgelt_peak_shaving_active"
    assert hass.states.get("switch.netzentgelt_peak_shaving_active").state == STATE_ON


async def test_load_profile_sensor_and_history_costs(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    entry, meter = await _setup(hass, freezer, datetime(2026, 9, 11, 9, 56, tzinfo=VIENNA))
    await meter.run(34 * 60, kw=2.0)  # 10:00 und 10:15 mit 2 kW, bis 10:30
    await meter.run(15 * 60 + 30, kw=12.0)  # 10:30–10:45 mit 12 kW

    profile = _state(hass, entry, "load_profile")
    assert profile.state == "10:30"  # Uhrzeit der Monatsspitze
    attrs = profile.attributes
    assert attrs["month"] == "2026-09"
    assert len(attrs["labels"]) == 96 and attrs["labels"][42] == "10:30"
    today = attrs["today_kw"]
    assert len(today) == 96
    assert today[39] is None  # 09:45 angebrochen → ungültig
    assert today[40] == pytest.approx(2.0, abs=0.01)
    assert today[41] == pytest.approx(2.0, abs=0.01)
    assert today[42] == pytest.approx(12.0, abs=0.01)
    assert attrs["yesterday_kw"] == [None] * 96
    assert attrs["month_max_kw"][42] == pytest.approx(12.0, abs=0.01)
    assert attrs["month_avg_kw"][40] == pytest.approx(2.0, abs=0.01)

    # History: verrechnet 12 kW → (10 × 33,82 + 2 × 67,64) / 12 = 39,46 €
    peak = _state(hass, entry, "month_peak")
    month = peak.attributes["history"]["2026-09"]
    assert month["source"] == "measured"
    assert month["billed_kw"] == pytest.approx(12.0, abs=0.01)
    assert month["capacity_cost_eur"] == pytest.approx(39.46, abs=0.01)
    # mit geänderter Staffelgrenze sofort neu gerechnet (12 × 33,82 / 12 = 33,82 €)
    await _set_number(hass, _entity_id(hass, entry, "number", "tier_limit_kw"), 15.0)
    month = _state(hass, entry, "month_peak").attributes["history"]["2026-09"]
    assert month["capacity_cost_eur"] == pytest.approx(33.82, abs=0.01)

    # Profile überstehen ein Neuladen (Store)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    attrs = _state(hass, entry, "load_profile").attributes
    assert attrs["today_kw"][42] == pytest.approx(12.0, abs=0.01)

    # Tageswechsel: heute wird zu gestern
    freezer.move_to(datetime(2026, 9, 12, 8, 0, tzinfo=VIENNA))
    await meter.run(60, kw=1.0)
    attrs = _state(hass, entry, "load_profile").attributes
    assert attrs["yesterday_kw"][42] == pytest.approx(12.0, abs=0.01)
    assert attrs["today_kw"] == [None] * 96
    assert attrs["month_max_kw"][42] == pytest.approx(12.0, abs=0.01)


async def test_profile_attributes_are_not_recorded(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    entry, _ = await _setup(hass, freezer, datetime(2026, 9, 11, 9, 56, tzinfo=VIENNA))
    entity = hass.data["entity_components"]["sensor"].get_entity(
        _entity_id(hass, entry, "sensor", "load_profile")
    )
    assert {
        "today_kw",
        "yesterday_kw",
        "month_max_kw",
        "month_avg_kw",
        "labels",
    } <= entity._unrecorded_attributes
