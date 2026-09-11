"""Tests für Config- und Options-Flow (pytest-homeassistant-custom-component)."""

from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.netzentgelt.const import (
    CONF_ENERGY_ENTITY,
    CONF_HYSTERESIS_KW,
    CONF_POWER_ENTITY,
    CONF_TARGET_KW,
    DEFAULT_OPTIONS,
    DOMAIN,
)

ENERGY = "sensor.grid_import_energy"
POWER = "sensor.grid_import_power"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Custom Integrations in allen Tests dieses Moduls laden."""


def _set_sources(
    hass: HomeAssistant, energy_unit: str = "kWh", state_class: str = "total_increasing"
) -> None:
    hass.states.async_set(
        ENERGY,
        "1234.567",
        {"unit_of_measurement": energy_unit, "device_class": "energy", "state_class": state_class},
    )
    hass.states.async_set(POWER, "850", {"unit_of_measurement": "W", "device_class": "power"})


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    _set_sources(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "Hausanschluss", CONF_ENERGY_ENTITY: ENERGY, CONF_POWER_ENTITY: POWER},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Hausanschluss"
    assert result["data"] == {CONF_ENERGY_ENTITY: ENERGY, CONF_POWER_ENTITY: POWER}
    assert result["options"] == DEFAULT_OPTIONS


async def test_user_flow_wh_without_power(hass: HomeAssistant) -> None:
    _set_sources(hass, energy_unit="Wh", state_class="total")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Netzentgelt", CONF_ENERGY_ENTITY: ENERGY}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ENERGY_ENTITY: ENERGY}


@pytest.mark.parametrize(
    ("unit", "state_class", "error"),
    [
        ("kW", "total_increasing", "invalid_energy_unit"),
        ("kWh", "measurement", "invalid_state_class"),
    ],
)
async def test_user_flow_rejects_bad_energy_sensor(
    hass: HomeAssistant, unit: str, state_class: str, error: str
) -> None:
    _set_sources(hass, energy_unit=unit, state_class=state_class)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Netzentgelt", CONF_ENERGY_ENTITY: ENERGY}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ENERGY_ENTITY: error}


async def test_user_flow_rejects_bad_power_unit(hass: HomeAssistant) -> None:
    _set_sources(hass)
    hass.states.async_set(POWER, "1", {"unit_of_measurement": "kWh"})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Netzentgelt", CONF_ENERGY_ENTITY: ENERGY, CONF_POWER_ENTITY: POWER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_POWER_ENTITY: "invalid_power_unit"}


async def test_user_flow_aborts_if_already_configured(hass: HomeAssistant) -> None:
    _set_sources(hass)
    MockConfigEntry(domain=DOMAIN, unique_id=ENERGY, data={CONF_ENERGY_ENTITY: ENERGY}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Netzentgelt", CONF_ENERGY_ENTITY: ENERGY}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow_updates_and_reloads(hass: HomeAssistant) -> None:
    _set_sources(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ENERGY,
        title="Netzentgelt",
        data={CONF_ENERGY_ENTITY: ENERGY},
        options=dict(DEFAULT_OPTIONS),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator_before = entry.runtime_data

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    new_options = {**DEFAULT_OPTIONS, CONF_TARGET_KW: 8.5}
    result = await hass.config_entries.options.async_configure(result["flow_id"], new_options)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_TARGET_KW] == 8.5
    # Options-Änderung → Reload → neuer Coordinator mit neuen Werten
    assert entry.runtime_data is not coordinator_before
    assert entry.runtime_data.options[CONF_TARGET_KW] == 8.5


async def test_options_flow_rejects_hysteresis_above_target(hass: HomeAssistant) -> None:
    _set_sources(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=ENERGY, data={CONF_ENERGY_ENTITY: ENERGY}, options=dict(DEFAULT_OPTIONS)
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**DEFAULT_OPTIONS, CONF_TARGET_KW: 1.0, CONF_HYSTERESIS_KW: 1.5}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_HYSTERESIS_KW: "hysteresis_too_large"}
