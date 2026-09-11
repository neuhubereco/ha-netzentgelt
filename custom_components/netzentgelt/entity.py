"""Gemeinsame Basisklasse der Entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import NetzentgeltCoordinator


class NetzentgeltEntity(Entity):
    """Basis: Gerät, unique_id, Aktualisierung über den Coordinator."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: NetzentgeltCoordinator, description: EntityDescription) -> None:
        """Initialisieren."""
        self.coordinator = coordinator
        self.entity_description = description
        entry = coordinator.entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_translation_key = description.translation_key or description.key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Beim Coordinator anmelden."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))
