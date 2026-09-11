"""Einstellungen als Number-Entities (Ziel-Leistung, Staffel, Preise, Hysterese).

Einzige Wahrheitsquelle bleiben die Optionen des Config-Entries: ``set_value``
schreibt dorthin, der Update-Listener übernimmt den Wert live (ohne Neuladen)
und aktualisiert alle Entities.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_AGREED_KW,
    CONF_HYSTERESIS_KW,
    CONF_MINIMUM_KW,
    CONF_PLAUSIBILITY_KW,
    CONF_PRICE_TIER1,
    CONF_PRICE_TIER2,
    CONF_TARGET_KW,
    CONF_TIER_LIMIT_KW,
    DOMAIN,
    TARGET_MAX_KW,
    TARGET_MIN_KW,
)
from .coordinator import NetzentgeltConfigEntry
from .entity import NetzentgeltEntity

PRICE_UNIT = "€/kW/a"


@dataclass(frozen=True, kw_only=True)
class NetzentgeltNumberDescription(NumberEntityDescription):
    """Number-Beschreibung; ``key`` ist zugleich der Options-Schlüssel."""


def _power(key: str, maximum: float, step: float, **kwargs: object) -> NetzentgeltNumberDescription:
    return NetzentgeltNumberDescription(
        key=key,
        translation_key=key,
        native_min_value=0,
        native_max_value=maximum,
        native_step=step,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=NumberDeviceClass.POWER,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        **kwargs,  # type: ignore[arg-type]
    )


def _price(key: str) -> NetzentgeltNumberDescription:
    return NetzentgeltNumberDescription(
        key=key,
        translation_key=key,
        native_min_value=0,
        native_max_value=10000,
        native_step=0.01,
        native_unit_of_measurement=PRICE_UNIT,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
    )


NUMBERS: tuple[NetzentgeltNumberDescription, ...] = (
    # Prominent (keine Konfigurations-Kategorie): Slider fürs Dashboard.
    NetzentgeltNumberDescription(
        key=CONF_TARGET_KW,
        translation_key=CONF_TARGET_KW,
        native_min_value=TARGET_MIN_KW,
        native_max_value=TARGET_MAX_KW,  # tatsächlich: Plausibilitätsgrenze, siehe native_max_value
        native_step=0.1,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=NumberDeviceClass.POWER,
        mode=NumberMode.SLIDER,
    ),
    _power(CONF_TIER_LIMIT_KW, 1000, 0.1),
    _power(CONF_AGREED_KW, 1000, 0.1),
    _power(CONF_MINIMUM_KW, 1000, 0.1),
    _price(CONF_PRICE_TIER1),
    _price(CONF_PRICE_TIER2),
    _power(CONF_HYSTERESIS_KW, 100, 0.05),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NetzentgeltConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Number-Entities anlegen."""
    async_add_entities(NetzentgeltNumber(entry.runtime_data, description) for description in NUMBERS)


class NetzentgeltNumber(NetzentgeltEntity, NumberEntity):
    """Wert aus den Optionen; Schreiben aktualisiert die Optionen."""

    entity_description: NetzentgeltNumberDescription

    @property
    def native_max_value(self) -> float:
        """Ziel-Leistung: bis zur Plausibilitätsgrenze (wie im Options-Flow validiert)."""
        if self.entity_description.key == CONF_TARGET_KW:
            return min(float(self.coordinator.options[CONF_PLAUSIBILITY_KW]), TARGET_MAX_KW)
        return super().native_max_value

    @property
    def native_value(self) -> float:
        """Aktueller Wert aus den Optionen."""
        return float(self.coordinator.options[self.entity_description.key])

    async def async_set_native_value(self, value: float) -> None:
        """Wert in die Optionen schreiben (Update-Listener übernimmt ihn live)."""
        key = self.entity_description.key
        options = {**self.coordinator.options, key: float(value)}
        _validate(options)
        entry = self.coordinator.entry
        self.hass.config_entries.async_update_entry(entry, options={**entry.options, key: float(value)})


def _validate(options: dict[str, float]) -> None:
    """Dieselben Regeln wie im Options-Flow."""
    target = float(options[CONF_TARGET_KW])
    if float(options[CONF_HYSTERESIS_KW]) >= target:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="hysteresis_too_large",
            translation_placeholders={"target": f"{target:g}"},
        )
    if target > float(options[CONF_PLAUSIBILITY_KW]):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="target_above_plausibility",
            translation_placeholders={"plausibility": f"{float(options[CONF_PLAUSIBILITY_KW]):g}"},
        )
