"""Config- und Options-Flow."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_NAME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)
import voluptuous as vol

from .const import (
    CONF_AGREED_KW,
    CONF_ENERGY_ENTITY,
    CONF_HYSTERESIS_KW,
    CONF_MINIMUM_KW,
    CONF_PLAUSIBILITY_KW,
    CONF_POWER_ENTITY,
    CONF_PRICE_SNAP,
    CONF_PRICE_STANDARD,
    CONF_PRICE_TIER1,
    CONF_PRICE_TIER2,
    CONF_PRICE_WINAP,
    CONF_TARGET_KW,
    CONF_TIER_LIMIT_KW,
    DEFAULT_NAME,
    DEFAULT_OPTIONS,
    DOMAIN,
    ENERGY_STATE_CLASSES,
    ENERGY_UNITS,
    POWER_UNITS,
)

ATTR_STATE_CLASS = "state_class"


def _source_attributes(hass: HomeAssistant, entity_id: str) -> tuple[bool, str | None, str | None]:
    """(existiert, Einheit, state_class) — aus dem State, sonst aus der Entity-Registry."""
    state = hass.states.get(entity_id)
    if state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return True, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT), state.attributes.get(ATTR_STATE_CLASS)
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None:
        capabilities = entry.capabilities or {}
        unit = entry.unit_of_measurement
        state_class = capabilities.get(ATTR_STATE_CLASS)
        if state is not None:
            unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT, unit)
            state_class = state.attributes.get(ATTR_STATE_CLASS, state_class)
        return True, unit, state_class
    if state is not None:
        return True, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT), state.attributes.get(ATTR_STATE_CLASS)
    return False, None, None


def validate_sources(hass: HomeAssistant, energy: str, power: str | None) -> dict[str, str]:
    """Quellen prüfen; liefert Fehler je Feld."""
    errors: dict[str, str] = {}
    exists, unit, state_class = _source_attributes(hass, energy)
    if not exists:
        errors[CONF_ENERGY_ENTITY] = "entity_not_found"
    elif unit not in ENERGY_UNITS:
        errors[CONF_ENERGY_ENTITY] = "invalid_energy_unit"
    elif state_class not in ENERGY_STATE_CLASSES:
        errors[CONF_ENERGY_ENTITY] = "invalid_state_class"
    if power:
        exists, unit, _ = _source_attributes(hass, power)
        if not exists:
            errors[CONF_POWER_ENTITY] = "entity_not_found"
        elif unit not in POWER_UNITS:
            errors[CONF_POWER_ENTITY] = "invalid_power_unit"
    return errors


def _user_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=DEFAULT_NAME): TextSelector(),
            vol.Required(CONF_ENERGY_ENTITY): EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class=SensorDeviceClass.ENERGY)
            ),
            vol.Optional(CONF_POWER_ENTITY): EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class=SensorDeviceClass.POWER)
            ),
        }
    )


class NetzentgeltConfigFlow(ConfigFlow, domain=DOMAIN):
    """Einrichtung über die UI."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Quellen wählen."""
        errors: dict[str, str] = {}
        if user_input is not None:
            energy = user_input[CONF_ENERGY_ENTITY]
            power = user_input.get(CONF_POWER_ENTITY) or None
            errors = validate_sources(self.hass, energy, power)
            if not errors:
                await self.async_set_unique_id(energy)
                self._abort_if_unique_id_configured()
                data: dict[str, Any] = {CONF_ENERGY_ENTITY: energy}
                if power:
                    data[CONF_POWER_ENTITY] = power
                return self.async_create_entry(
                    title=user_input.get(CONF_NAME) or DEFAULT_NAME,
                    data=data,
                    options=dict(DEFAULT_OPTIONS),
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_user_schema(), user_input),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> NetzentgeltOptionsFlow:
        """Options-Flow."""
        return NetzentgeltOptionsFlow()


def _number(
    minimum: float, maximum: float, step: float, unit: str
) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            unit_of_measurement=unit,
            mode=NumberSelectorMode.BOX,
        )
    )


OPTION_FIELDS: dict[str, NumberSelector] = {
    CONF_TARGET_KW: _number(0.5, 1000, 0.1, "kW"),
    CONF_TIER_LIMIT_KW: _number(0, 1000, 0.1, "kW"),
    CONF_AGREED_KW: _number(0, 1000, 0.1, "kW"),
    CONF_MINIMUM_KW: _number(0, 1000, 0.1, "kW"),
    CONF_PRICE_TIER1: _number(0, 10000, 0.01, "€/kW/a"),
    CONF_PRICE_TIER2: _number(0, 10000, 0.01, "€/kW/a"),
    CONF_PRICE_STANDARD: _number(0, 1000, 0.001, "ct/kWh"),
    CONF_PRICE_SNAP: _number(0, 1000, 0.001, "ct/kWh"),
    CONF_PRICE_WINAP: _number(0, 1000, 0.001, "ct/kWh"),
    CONF_PLAUSIBILITY_KW: _number(1, 10000, 1, "kW"),
    CONF_HYSTERESIS_KW: _number(0, 100, 0.05, "kW"),
}


class NetzentgeltOptionsFlow(OptionsFlow):
    """Schwellwerte und Preise; Änderungen laden den Eintrag neu."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Optionen bearbeiten."""
        errors: dict[str, str] = {}
        if user_input is not None:
            values = {key: float(user_input[key]) for key in OPTION_FIELDS}
            if values[CONF_HYSTERESIS_KW] >= values[CONF_TARGET_KW]:
                errors[CONF_HYSTERESIS_KW] = "hysteresis_too_large"
            elif values[CONF_TARGET_KW] > values[CONF_PLAUSIBILITY_KW]:
                errors[CONF_TARGET_KW] = "target_above_plausibility"
            else:
                return self.async_create_entry(data=values)

        current = {**DEFAULT_OPTIONS, **self.config_entry.options}
        if user_input is not None:
            current.update(user_input)
        schema = vol.Schema(
            {
                vol.Required(key, default=current[key]): selector
                for key, selector in OPTION_FIELDS.items()
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
