"""Netzentgelt AT (Leistungspreis) — 15-Minuten-Spitze, SNAP/WiNAP, Peak-Shaving."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, STORAGE_VERSION
from .coordinator import NetzentgeltConfigEntry, NetzentgeltCoordinator, storage_key
from .services import async_setup_services

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.NUMBER, Platform.SWITCH]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Services der Integration registrieren (einmal, unabhängig von Einträgen)."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: NetzentgeltConfigEntry) -> bool:
    """Config-Entry einrichten."""
    coordinator = NetzentgeltCoordinator(hass, entry)
    await coordinator.async_setup()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NetzentgeltConfigEntry) -> bool:
    """Config-Entry entladen; Listener werden über ``async_on_unload`` entfernt."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: NetzentgeltConfigEntry) -> None:
    """Gespeicherte Monatsspitzen beim Löschen des Eintrags entfernen."""
    await Store(hass, STORAGE_VERSION, storage_key(entry.entry_id)).async_remove()


async def _async_update_listener(hass: HomeAssistant, entry: NetzentgeltConfigEntry) -> None:
    """Eintrag geändert: Wert-Optionen live übernehmen, sonst neu laden.

    Ziel, Preise, Hysterese usw. (Number-Entities, Options-Flow) ändern nur
    Rechengrößen; ein Neuladen würde die laufende Viertelstunde ungültig
    machen. Quellen, Plausibilitätsgrenze oder Titel erfordern ein Neuladen.
    """
    if entry.runtime_data.async_apply_entry_update(entry):
        return
    await hass.config_entries.async_reload(entry.entry_id)
