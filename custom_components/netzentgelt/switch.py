"""Schalter „Peak-Shaving aktiv“ — Freigabe für eigene Automationen und Blueprints.

Die Integration selbst schaltet nichts; der Schalter merkt sich nur seinen
Zustand über Neustarts hinweg (Standard: aus).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import NetzentgeltConfigEntry
from .entity import NetzentgeltEntity

PEAK_SHAVING = SwitchEntityDescription(key="peak_shaving", translation_key="peak_shaving")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NetzentgeltConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Schalter anlegen."""
    async_add_entities([PeakShavingSwitch(entry.runtime_data, PEAK_SHAVING)])


class PeakShavingSwitch(NetzentgeltEntity, SwitchEntity, RestoreEntity):
    """Master-Freigabe für Peak-Shaving-Automationen (Zustand wird wiederhergestellt)."""

    _attr_is_on = False

    async def async_added_to_hass(self) -> None:
        """Letzten Zustand wiederherstellen."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == STATE_ON

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Freigabe erteilen."""
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Freigabe entziehen."""
        self._attr_is_on = False
        self.async_write_ha_state()
