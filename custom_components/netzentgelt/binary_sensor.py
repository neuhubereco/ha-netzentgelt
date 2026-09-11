"""Binärsensor „Spitze droht“."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import calc
from .const import ATTRIBUTION, CONF_HYSTERESIS_KW, CONF_TARGET_KW
from .coordinator import NetzentgeltConfigEntry
from .entity import NetzentgeltEntity

PEAK_IMMINENT = BinarySensorEntityDescription(
    key="peak_imminent",
    translation_key="peak_imminent",
    device_class=BinarySensorDeviceClass.PROBLEM,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NetzentgeltConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Binärsensor anlegen."""
    async_add_entities([PeakImminentBinarySensor(entry.runtime_data, PEAK_IMMINENT)])


class PeakImminentBinarySensor(NetzentgeltEntity, BinarySensorEntity):
    """Ein, wenn die Prognose der laufenden Viertelstunde das Ziel überschreitet.

    Aus erst, wenn die Prognose um mehr als die Hysterese unter das Ziel fällt.
    Unbekannt, solange keine Prognose möglich ist.
    """

    _attr_attribution = ATTRIBUTION

    @property
    def is_on(self) -> bool | None:
        """Zustand."""
        return self.coordinator.peak_imminent

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Zusätzliche Attribute."""
        c = self.coordinator
        forecast = c.forecast_kw
        return {
            "forecast_kw": None if forecast is None else calc.round_half_up(forecast, 3),
            "target_kw": c.options[CONF_TARGET_KW],
            "hysteresis_kw": c.options[CONF_HYSTERESIS_KW],
        }
