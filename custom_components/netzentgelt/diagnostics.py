"""Diagnose-Daten."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import NetzentgeltConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NetzentgeltConfigEntry
) -> dict[str, Any]:
    """Diagnose für einen Config-Entry (enthält keine Zählpunkt-/Personendaten)."""
    return {
        "entry": {
            "title": entry.title,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "coordinator": entry.runtime_data.diagnostics(),
    }
