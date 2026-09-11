"""Zustandsverwaltung: verbindet Home-Assistant-Ereignisse mit der Rechenlogik."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import calc
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
    DEFAULT_OPTIONS,
    DOMAIN,
    FORECAST_INTERVAL_SECONDS,
    LIVE_OPTION_KEYS,
    QUARTER_MINUTES,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
)

try:  # seit HA 2024.x vorhanden; Fallback nur zur Sicherheit
    from homeassistant.helpers.event import async_track_state_report_event
except ImportError:  # pragma: no cover
    async_track_state_report_event = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

type NetzentgeltConfigEntry = ConfigEntry[NetzentgeltCoordinator]

POWER_SOURCE_SENSOR = "power_sensor"
POWER_SOURCE_SLOPE = "energy_slope"
POWER_SOURCE_AVERAGE = "quarter_average"


def storage_key(entry_id: str) -> str:
    """Store-Schlüssel je Config-Entry."""
    return f"{DOMAIN}.{entry_id}"


class NetzentgeltCoordinator:
    """Hält Engine, Monatsspitzen und abgeleitete Werte eines Config-Entries."""

    def __init__(self, hass: HomeAssistant, entry: NetzentgeltConfigEntry) -> None:
        """Initialisieren (ohne I/O)."""
        self.hass = hass
        self.entry = entry
        self.energy_entity: str = entry.data[CONF_ENERGY_ENTITY]
        self.power_entity: str | None = entry.data.get(CONF_POWER_ENTITY) or None
        self.options: dict[str, float] = {**DEFAULT_OPTIONS, **entry.options}
        self._structure = _structural_snapshot(entry)
        self.tz = dt_util.get_default_time_zone()

        self.engine = calc.QuarterEngine(plausibility_kw=float(self.options[CONF_PLAUSIBILITY_KW]))
        self.tracker = calc.PeakTracker(tz=self.tz)
        self.days = calc.DayProfiles(tz=self.tz)
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, storage_key(entry.entry_id))
        self._listeners: list[CALLBACK_TYPE] = []

        # Letzte abgeschlossene Viertelstunde (gültig oder nicht) und letzte gültige.
        self.last_quarter: calc.QuarterResult | None = None
        self.last_valid_quarter: calc.QuarterResult | None = None

        # Laufende Viertelstunde
        self.current: calc.CurrentQuarter | None = None
        self.power_now_kw: float | None = None
        self.power_source: str | None = None
        self.forecast_kw: float | None = None
        self.headroom_kw: float | None = None
        self.peak_imminent: bool | None = None

        # Tarif
        self.tariff: str = calc.TARIFF_STANDARD
        self.tariff_end: datetime | None = None
        self.energy_price: float | None = None

    # ------------------------------------------------------------ lifecycle
    async def async_setup(self) -> None:
        """Store laden, Quellen abonnieren, Timer starten."""
        self._restore(await self._store.async_load())
        now = dt_util.utcnow()
        self.engine.start(now)
        self.tracker.roll_to(calc.month_key(now, self.tz))

        self._handle_energy_state(self.hass.states.get(self.energy_entity), now)

        entry = self.entry
        entry.async_on_unload(
            async_track_state_change_event(self.hass, [self.energy_entity], self._on_energy_changed)
        )
        if async_track_state_report_event is not None:
            # Unveränderte Werte (Zähler steht) liefern ebenfalls Samples.
            entry.async_on_unload(
                async_track_state_report_event(self.hass, [self.energy_entity], self._on_energy_reported)
            )
        entry.async_on_unload(
            async_track_time_change(self.hass, self._on_quarter, minute=QUARTER_MINUTES, second=0)
        )
        entry.async_on_unload(
            async_track_time_interval(
                self.hass, self._on_tick, timedelta(seconds=FORECAST_INTERVAL_SECONDS)
            )
        )
        self._recompute(now)

    async def async_shutdown(self) -> None:
        """Zustand sofort sichern (Entladen/Neuladen)."""
        await self._store.async_save(self._data_to_store())

    @callback
    def async_apply_entry_update(self, entry: NetzentgeltConfigEntry) -> bool:
        """Geänderte Wert-Optionen live übernehmen.

        Rückgabe ``False``, wenn sich Strukturelles geändert hat (Quellen,
        Plausibilitätsgrenze, Titel) — dann muss neu geladen werden.
        """
        if _structural_snapshot(entry) != self._structure:
            return False
        options = {**DEFAULT_OPTIONS, **entry.options}
        if options != self.options:
            self.options = options
            self._recompute(dt_util.utcnow())
            self._notify()
        return True

    # ------------------------------------------------------------ listeners
    @callback
    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        """Entity-Callback registrieren; liefert Abmelde-Funktion."""
        self._listeners.append(update_callback)

        @callback
        def remove() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return remove

    @callback
    def _notify(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    # --------------------------------------------------------------- events
    @callback
    def _on_energy_changed(self, event: Event[EventStateChangedData]) -> None:
        self._handle_energy_state(event.data["new_state"], dt_util.utcnow())

    @callback
    def _on_energy_reported(self, event: Event[Any]) -> None:
        self._handle_energy_state(event.data["new_state"], dt_util.utcnow())

    @callback
    def _handle_energy_state(self, state: State | None, now: datetime) -> None:
        if state is None:
            results = self.engine.mark_unavailable(now)
        elif state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            results = self.engine.mark_unavailable(state.last_reported)
        else:
            unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
            kwh = calc.energy_to_kwh(state.state, unit)
            if kwh is None:
                _LOGGER.debug(
                    "%s: kein gültiger Energiewert (%s %s)", self.energy_entity, state.state, unit
                )
                results = self.engine.mark_unavailable(state.last_reported)
            else:
                # Zeitstempel = wann HA den Wert erhalten hat (auch bei unverändertem Wert).
                results = self.engine.add_sample(state.last_reported, kwh)
        if self._process(results):
            self._recompute(dt_util.utcnow())
            self._notify()

    @callback
    def _on_quarter(self, now: datetime) -> None:
        results = self.engine.boundary(now)
        self.tracker.roll_to(calc.month_key(now, self.tz))
        self._process(results)
        self._recompute(now)
        self._notify()

    @callback
    def _on_tick(self, now: datetime) -> None:
        results = self.engine.tick(now)
        self.tracker.roll_to(calc.month_key(now, self.tz))
        self._process(results)
        self._recompute(now)
        self._notify()

    # ------------------------------------------------------------- results
    @callback
    def _process(self, results: list[calc.QuarterResult]) -> bool:
        """Abgeschlossene Viertelstunden verbuchen. True, wenn es welche gab."""
        if not results:
            return False
        for result in results:
            self.tracker.add(result)
            self.days.add(result)
            if self.last_quarter is None or result.start >= self.last_quarter.start:
                self.last_quarter = result
            if result.valid and (
                self.last_valid_quarter is None or result.start >= self.last_valid_quarter.start
            ):
                self.last_valid_quarter = result
            if not result.valid:
                _LOGGER.debug(
                    "%s: Viertelstunde ab %s ungültig (%s)",
                    self.energy_entity,
                    result.start.isoformat(),
                    result.reason,
                )
        self._store.async_delay_save(self._data_to_store, STORAGE_SAVE_DELAY)
        return True

    @callback
    def _recompute(self, now: datetime) -> None:
        opts = self.options
        target = float(opts[CONF_TARGET_KW])
        self.current = self.engine.current_quarter(now)
        self.power_now_kw, self.power_source = self._power_now(now)

        if self.current is not None and self.power_now_kw is not None:
            self.forecast_kw = calc.forecast_kw(self.current, self.power_now_kw)
            self.headroom_kw = calc.headroom_kw(self.current, self.power_now_kw, target, now)
        else:
            self.forecast_kw = None
            self.headroom_kw = None
        self.peak_imminent = calc.hysteresis(
            bool(self.peak_imminent),
            self.forecast_kw,
            target,
            float(opts[CONF_HYSTERESIS_KW]),
        )

        self.days.prune(now.astimezone(self.tz).date())
        self.tariff = calc.tariff_window(now, self.tz)
        self.tariff_end = calc.tariff_window_end(now, self.tz)
        self.energy_price = calc.energy_price(
            self.tariff,
            float(opts[CONF_PRICE_STANDARD]),
            float(opts[CONF_PRICE_SNAP]),
            float(opts[CONF_PRICE_WINAP]),
        )

    def _power_now(self, now: datetime) -> tuple[float | None, str | None]:
        if self.power_entity:
            state = self.hass.states.get(self.power_entity)
            if state is not None:
                power = calc.power_to_kw(state.state, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))
                if power is not None:
                    return power, POWER_SOURCE_SENSOR
        slope = self.engine.slope_kw()
        if slope is not None:
            return slope, POWER_SOURCE_SLOPE
        current = self.current
        if current is not None:
            elapsed = (current.ref_ts - current.start).total_seconds()
            if elapsed >= 60:
                return current.used_kwh / (elapsed / 3600), POWER_SOURCE_AVERAGE
        return None, None

    # ----------------------------------------------------------- derived
    @property
    def month_stats(self) -> calc.MonthStats:
        """Kennzahlen des laufenden Monats."""
        return self.tracker.current_stats

    @property
    def minimum_kw(self) -> float:
        """Mindestverrechnung."""
        return calc.minimum_billed_kw(
            float(self.options[CONF_AGREED_KW]), float(self.options[CONF_MINIMUM_KW])
        )

    @property
    def month_peak(self) -> tuple[float | None, str | None]:
        """Monatsspitze (kW, ISO-Beginn) aus eigener Messung und Import."""
        return self.month_stats.combined_peak()

    def billed_for(self, peak_kw: float | None) -> float:
        """Verrechnete Leistung für eine Spitze (mit den aktuellen Einstellungen)."""
        return calc.billed_kw(
            peak_kw,
            float(self.options[CONF_AGREED_KW]),
            float(self.options[CONF_MINIMUM_KW]),
        )

    def cost_for(self, billed: float) -> float:
        """Monatlicher Leistungspreis (€) für eine verrechnete Leistung."""
        return calc.monthly_capacity_cost(
            billed,
            float(self.options[CONF_TIER_LIMIT_KW]),
            float(self.options[CONF_PRICE_TIER1]),
            float(self.options[CONF_PRICE_TIER2]),
        )

    @property
    def billed_kw(self) -> float:
        """Verrechnete Leistung des laufenden Monats."""
        return self.billed_for(self.month_peak[0])

    @property
    def monthly_cost(self) -> float:
        """Geschätzter Leistungspreis des laufenden Monats (€)."""
        return self.cost_for(self.billed_kw)

    def today(self) -> date:
        """Heutiges Datum in der HA-Zeitzone."""
        return dt_util.utcnow().astimezone(self.tz).date()

    # ------------------------------------------------------------- import
    @callback
    def async_import_load_profile(self, profile: calc.LoadProfile, *, overwrite: bool) -> dict[str, Any]:
        """Gelesenen Lastgang in History und Tagesprofile übernehmen."""
        now = dt_util.utcnow()
        months = calc.summarize_months(profile.quarters, self.tz)
        report = calc.merge_import(self.tracker, months, calc.month_key(now, self.tz), overwrite=overwrite)
        today = now.astimezone(self.tz).date()
        keep = {today, today - timedelta(days=1)}
        filled = 0
        for start, kw in profile.quarters.items():
            if start.astimezone(self.tz).date() in keep:
                filled += self.days.fill(start, kw)
        self._recompute(now)
        self._notify()
        return {
            "months_imported": report.imported,
            "months_merged": report.merged,
            "months_overwritten": report.overwritten,
            "months_skipped": report.skipped,
            "day_slots_filled": filled,
        }

    async def async_save(self) -> None:
        """Zustand sofort speichern."""
        await self._store.async_save(self._data_to_store())

    def local_iso(self, moment: datetime | None) -> str | None:
        """Zeitpunkt als ISO-String in lokaler Zeit."""
        if moment is None:
            return None
        return moment.astimezone(self.tz).isoformat()

    # ------------------------------------------------------------ storage
    def _data_to_store(self) -> dict[str, Any]:
        return {
            "tracker": self.tracker.as_dict(),
            "days": self.days.as_dict(),
            "last_quarter": self.last_quarter.as_dict() if self.last_quarter else None,
            "last_valid_quarter": (
                self.last_valid_quarter.as_dict() if self.last_valid_quarter else None
            ),
        }

    def _restore(self, data: dict[str, Any] | None) -> None:
        if not data:
            return
        self.tracker = calc.PeakTracker.from_dict(self.tz, data.get("tracker"))
        self.days = calc.DayProfiles.from_dict(self.tz, data.get("days"))
        self.last_quarter = _quarter_from_dict(data.get("last_quarter"))
        self.last_valid_quarter = _quarter_from_dict(data.get("last_valid_quarter"))

    def diagnostics(self) -> dict[str, Any]:
        """Daten für die Diagnose-Datei."""
        last = self.engine.last_sample
        return {
            "options": self.options,
            "power_entity_configured": self.power_entity is not None,
            "engine": {
                "source_available": self.engine.series_open,
                "pending_boundaries": self.engine.pending_boundaries,
                "last_sample": (
                    {"ts": last.ts.isoformat(), "kwh": last.kwh, "series": last.series}
                    if last
                    else None
                ),
            },
            "current": {
                "power_now_kw": self.power_now_kw,
                "power_source": self.power_source,
                "forecast_kw": self.forecast_kw,
                "headroom_kw": self.headroom_kw,
                "peak_imminent": self.peak_imminent,
                "tariff": self.tariff,
            },
            "stored": self._data_to_store(),
        }


def _structural_snapshot(entry: ConfigEntry) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Alles, dessen Änderung ein Neuladen erfordert."""
    options = {**DEFAULT_OPTIONS, **entry.options}
    return (
        dict(entry.data),
        entry.title,
        {key: value for key, value in options.items() if key not in LIVE_OPTION_KEYS},
    )


def _quarter_from_dict(data: Any) -> calc.QuarterResult | None:
    if not isinstance(data, dict):
        return None
    try:
        start = dt_util.parse_datetime(str(data["start"]))
        end = dt_util.parse_datetime(str(data["end"]))
    except (KeyError, ValueError):
        return None
    if start is None or end is None:
        return None
    kw = data.get("kw")
    energy = data.get("energy_kwh")
    return calc.QuarterResult(
        start=start,
        end=end,
        valid=bool(data.get("valid")),
        kw=float(kw) if isinstance(kw, (int, float)) else None,
        energy_kwh=float(energy) if isinstance(energy, (int, float)) else None,
        reason=data.get("reason"),
    )
