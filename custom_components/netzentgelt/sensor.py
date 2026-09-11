"""Sensoren der Netzentgelt-Integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import calc
from .const import (
    ATTRIBUTION,
    CONF_AGREED_KW,
    CONF_PRICE_TIER1,
    CONF_PRICE_TIER2,
    CONF_TARGET_KW,
    CONF_TIER_LIMIT_KW,
    MINIMUM_SHARE,
)
from .coordinator import NetzentgeltConfigEntry, NetzentgeltCoordinator
from .entity import NetzentgeltEntity

CURRENCY_EURO = "EUR"


def _r(value: float | None, digits: int) -> float | None:
    return None if value is None else calc.round_half_up(value, digits)


# ----------------------------------------------------------------- Werte


def _quarter_value(c: NetzentgeltCoordinator) -> float | None:
    q = c.last_valid_quarter
    return _r(q.kw, 3) if q else None


def _quarter_attrs(c: NetzentgeltCoordinator) -> dict[str, Any]:
    valid_q = c.last_valid_quarter
    last_q = c.last_quarter
    return {
        # Viertelstunde, zu der der angezeigte Wert gehört
        "quarter_start": c.local_iso(valid_q.start) if valid_q else None,
        "quarter_end": c.local_iso(valid_q.end) if valid_q else None,
        "energy_kwh": _r(valid_q.energy_kwh, 4) if valid_q else None,
        # zuletzt abgeschlossene Viertelstunde (auch ungültige)
        "last_quarter_start": c.local_iso(last_q.start) if last_q else None,
        "last_quarter_valid": last_q.valid if last_q else None,
        "last_quarter_reason": last_q.reason if last_q else None,
        "invalid_quarters_month": c.month_stats.invalid_quarters,
    }


def _live_attrs(c: NetzentgeltCoordinator) -> dict[str, Any]:
    cur = c.current
    return {
        "power_now_kw": _r(c.power_now_kw, 3),
        "power_source": c.power_source,
        "used_kwh": _r(cur.used_kwh, 4) if cur else None,
        "quarter_start": c.local_iso(cur.start) if cur else None,
        "quarter_end": c.local_iso(cur.end) if cur else None,
        "estimated_start": cur.estimated_start if cur else None,
        "target_kw": c.options[CONF_TARGET_KW],
    }


def _peak_value(c: NetzentgeltCoordinator) -> float | None:
    return _r(c.month_peak[0], 3)


def _peak_attrs(c: NetzentgeltCoordinator) -> dict[str, Any]:
    stats = c.month_stats
    peak, peak_start = c.month_peak
    start = _parse(peak_start)
    return {
        "month": c.tracker.current,
        "peak_quarter_start": c.local_iso(start),
        "peak_quarter_end": c.local_iso(start + calc.QUARTER) if start else None,
        "peak_rounded_kw": _r(peak, 2),
        "valid_quarters_month": stats.valid_quarters,
        "invalid_quarters_month": stats.invalid_quarters,
        "source": stats.source,
        "history": {month: _history_entry(c, values) for month, values in c.tracker.history().items()},
    }


def _history_entry(c: NetzentgeltCoordinator, values: dict[str, Any]) -> dict[str, Any]:
    """Monat im Verlauf, Kosten mit den AKTUELLEN Preiseinstellungen gerechnet."""
    peak = values["peak_kw"]
    billed = c.billed_for(peak) if peak is not None else None
    return {
        **values,
        "peak_kw": _r(peak, 3),
        "peak_start": c.local_iso(_parse(values["peak_start"])),
        "billed_kw": billed,
        "capacity_cost_eur": c.cost_for(billed) if billed is not None else None,
    }


def _parse(value: str | None) -> datetime | None:
    return dt_util.parse_datetime(value) if value else None


def _billed_attrs(c: NetzentgeltCoordinator) -> dict[str, Any]:
    peak = c.month_peak[0]
    minimum = c.minimum_kw
    return {
        "peak_rounded_kw": _r(peak, 2),
        "minimum_kw": _r(minimum, 2),
        "agreed_kw": c.options[CONF_AGREED_KW],
        "minimum_share": MINIMUM_SHARE,
        "basis": "peak" if peak is not None and calc.round_half_up(peak, 2) > minimum else "minimum",
    }


def _cost_attrs(c: NetzentgeltCoordinator) -> dict[str, Any]:
    billed = c.billed_kw
    limit = float(c.options[CONF_TIER_LIMIT_KW])
    p1 = float(c.options[CONF_PRICE_TIER1])
    p2 = float(c.options[CONF_PRICE_TIER2])
    return {
        "billed_kw": billed,
        "tier1_kw": _r(min(billed, limit), 2),
        "tier2_kw": _r(max(billed - limit, 0.0), 2),
        "price_tier1_eur_per_kw_year": p1,
        "price_tier2_eur_per_kw_year": p2,
        "annual_cost_eur": _r(calc.annual_capacity_cost(billed, limit, p1, p2), 2),
        "note": "Richtwert — Tarifverordnung (SNE-T-V) steht aus",
    }


def _profile_value(c: NetzentgeltCoordinator) -> str | None:
    """Uhrzeit (HH:MM, Ortszeit) der Viertelstunde mit der Monatsspitze."""
    start = _parse(c.month_peak[1])
    return start.astimezone(c.tz).strftime("%H:%M") if start else None


def _profile_attrs(c: NetzentgeltCoordinator) -> dict[str, Any]:
    today = c.today()
    profile = c.month_stats.combined_profile()
    return {
        "month": c.tracker.current,
        "today_kw": calc.round_list(c.days.get(today)),
        "yesterday_kw": calc.round_list(c.days.get(today - timedelta(days=1))),
        "month_max_kw": calc.round_list(profile.max),
        "month_avg_kw": calc.round_list(profile.avg()),
        "labels": LABELS,
    }


LABELS = calc.slot_labels()


def _tariff_attrs(c: NetzentgeltCoordinator) -> dict[str, Any]:
    return {
        "window_end": c.local_iso(c.tariff_end),
        "energy_price_ct_kwh": c.energy_price,
    }


@dataclass(frozen=True, kw_only=True)
class NetzentgeltSensorDescription(SensorEntityDescription):
    """Beschreibung mit Wert- und Attribut-Funktion."""

    value_fn: Callable[[NetzentgeltCoordinator], float | str | None]
    attrs_fn: Callable[[NetzentgeltCoordinator], dict[str, Any]] | None = None


POWER_KW = {
    "native_unit_of_measurement": UnitOfPower.KILO_WATT,
    "device_class": SensorDeviceClass.POWER,
    "state_class": SensorStateClass.MEASUREMENT,
}

SENSORS: tuple[NetzentgeltSensorDescription, ...] = (
    NetzentgeltSensorDescription(
        key="quarter_power",
        translation_key="quarter_power",
        suggested_display_precision=2,
        value_fn=_quarter_value,
        attrs_fn=_quarter_attrs,
        **POWER_KW,  # type: ignore[arg-type]
    ),
    NetzentgeltSensorDescription(
        key="forecast",
        translation_key="forecast",
        suggested_display_precision=2,
        value_fn=lambda c: _r(c.forecast_kw, 3),
        attrs_fn=_live_attrs,
        **POWER_KW,  # type: ignore[arg-type]
    ),
    NetzentgeltSensorDescription(
        key="month_peak",
        translation_key="month_peak",
        suggested_display_precision=2,
        value_fn=_peak_value,
        attrs_fn=_peak_attrs,
        **POWER_KW,  # type: ignore[arg-type]
    ),
    NetzentgeltSensorDescription(
        key="billed_power",
        translation_key="billed_power",
        suggested_display_precision=2,
        value_fn=lambda c: c.billed_kw,
        attrs_fn=_billed_attrs,
        **POWER_KW,  # type: ignore[arg-type]
    ),
    NetzentgeltSensorDescription(
        key="capacity_cost_month",
        translation_key="capacity_cost_month",
        native_unit_of_measurement=CURRENCY_EURO,
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda c: c.monthly_cost,
        attrs_fn=_cost_attrs,
    ),
    NetzentgeltSensorDescription(
        key="headroom",
        translation_key="headroom",
        suggested_display_precision=2,
        value_fn=lambda c: _r(c.headroom_kw, 3),
        attrs_fn=_live_attrs,
        **POWER_KW,  # type: ignore[arg-type]
    ),
    NetzentgeltSensorDescription(
        key="load_profile",
        translation_key="load_profile",
        value_fn=_profile_value,
        attrs_fn=_profile_attrs,
    ),
    NetzentgeltSensorDescription(
        key="tariff_window",
        translation_key="tariff_window",
        device_class=SensorDeviceClass.ENUM,
        options=calc.TARIFF_OPTIONS,
        value_fn=lambda c: c.tariff,
        attrs_fn=_tariff_attrs,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NetzentgeltConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Sensoren anlegen."""
    coordinator = entry.runtime_data
    classes = {"month_peak": NetzentgeltPeakSensor, "load_profile": NetzentgeltProfileSensor}
    async_add_entities(
        classes.get(description.key, NetzentgeltSensor)(coordinator, description) for description in SENSORS
    )


class NetzentgeltSensor(NetzentgeltEntity, SensorEntity):
    """Sensor, dessen Wert aus dem Coordinator kommt."""

    entity_description: NetzentgeltSensorDescription
    _attr_attribution = ATTRIBUTION

    @property
    def native_value(self) -> float | str | None:
        """Aktueller Wert."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Zusätzliche Attribute."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator)


class NetzentgeltPeakSensor(NetzentgeltSensor):
    """Monatsspitze: der Verlauf (36 Monate) wird nicht in den Recorder geschrieben."""

    _unrecorded_attributes = frozenset({"history"})


class NetzentgeltProfileSensor(NetzentgeltSensor):
    """Lastprofil: die 96er-Listen werden nicht in den Recorder geschrieben (groß)."""

    _unrecorded_attributes = frozenset(
        {"today_kw", "yesterday_kw", "month_max_kw", "month_avg_kw", "labels"}
    )
