"""Service ``netzentgelt.import_load_profile``: Lastgang (z. B. Portal-Export) importieren."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.util import dt as dt_util
import voluptuous as vol

from . import calc
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_IMPORT_STATISTICS,
    ATTR_OVERWRITE,
    ATTR_PATH,
    ATTR_TIMESTAMP_IS_END,
    DOMAIN,
    IMPORT_MAX_BYTES,
    IMPORT_SUFFIXES,
    SERVICE_IMPORT_LOAD_PROFILE,
)
from .coordinator import NetzentgeltConfigEntry, NetzentgeltCoordinator

_LOGGER = logging.getLogger(__name__)

IMPORT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_PATH): cv.string,
        vol.Optional(ATTR_TIMESTAMP_IS_END, default=False): cv.boolean,
        vol.Optional(ATTR_OVERWRITE, default=False): cv.boolean,
        vol.Optional(ATTR_IMPORT_STATISTICS, default=True): cv.boolean,
    }
)

# Einheiten, in denen die Statistik des 15-Min-Sensors geführt werden kann.
_POWER_FACTORS_FROM_KW = {"W": 1000.0, "kW": 1.0, "MW": 0.001}
HOUR = timedelta(hours=1)


class ImportPathError(Exception):
    """Pfad unzulässig (``reason`` = Übersetzungsschlüssel)."""

    def __init__(self, reason: str) -> None:
        """Mit Grund initialisieren."""
        super().__init__(reason)
        self.reason = reason


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Services registrieren (nur Admins: liest Dateien, schreibt Statistik)."""
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_IMPORT_LOAD_PROFILE,
        partial(_async_import_load_profile, hass),
        schema=IMPORT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def resolve_import_path(config_dir: str, raw: str, is_allowed_path: Callable[[str], bool]) -> Path:
    """Pfad prüfen und auflösen (blockierend, im Executor aufrufen).

    * relativ = relativ zum Config-Verzeichnis; ``..``/Symlinks werden aufgelöst,
      das Ergebnis muss im Config-Verzeichnis liegen — oder in einem über
      ``allowlist_external_dirs`` freigegebenen Verzeichnis
      (``hass.config.is_allowed_path``),
    * keine versteckten Verzeichnisse/Dateien (``.storage`` usw.),
    * nur ``.csv``/``.txt``, höchstens ``IMPORT_MAX_BYTES``.
    """
    text = raw.strip()
    if not text or "\x00" in text:
        raise ImportPathError("path_not_allowed")
    base = Path(config_dir).resolve()
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = base / candidate
    resolved = candidate.resolve()
    if resolved.is_relative_to(base):
        if any(part.startswith(".") for part in resolved.relative_to(base).parts):
            raise ImportPathError("path_not_allowed")
    elif not is_allowed_path(str(resolved)):
        raise ImportPathError("path_not_allowed")
    if resolved.suffix.lower() not in IMPORT_SUFFIXES:
        raise ImportPathError("invalid_file_type")
    if not resolved.is_file():
        raise ImportPathError("file_not_found")
    if resolved.stat().st_size > IMPORT_MAX_BYTES:
        raise ImportPathError("file_too_large")
    return resolved


def _read_text(path: Path) -> str:
    """Datei lesen: UTF-8 (mit/ohne BOM), sonst Windows-1252 (Excel-Exporte)."""
    data = path.read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _error(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders or None
    )


async def _async_import_load_profile(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Lastgang lesen, in History/Tagesprofile übernehmen, optional Statistik importieren."""
    entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
    entry: NetzentgeltConfigEntry | None = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise _error("entry_not_found")
    if entry.state is not ConfigEntryState.LOADED:
        raise _error("entry_not_loaded")
    coordinator = entry.runtime_data
    raw_path = call.data[ATTR_PATH]

    try:
        path = await hass.async_add_executor_job(
            resolve_import_path, hass.config.config_dir, raw_path, hass.config.is_allowed_path
        )
        text = await hass.async_add_executor_job(_read_text, path)
        profile = await hass.async_add_executor_job(
            partial(
                calc.parse_load_profile,
                text,
                coordinator.tz,
                timestamp_is_end=call.data[ATTR_TIMESTAMP_IS_END],
            )
        )
    except ImportPathError as err:
        raise _error(err.reason, path=raw_path) from err
    except calc.LoadProfileError as err:
        raise _error(err.reason, path=raw_path) from err
    except OSError as err:
        raise _error("file_not_readable", path=raw_path) from err

    merged = coordinator.async_import_load_profile(profile, overwrite=call.data[ATTR_OVERWRITE])
    await coordinator.async_save()

    statistics: dict[str, Any] = {
        "statistics_hours": 0,
        "statistics_hours_skipped": 0,
        "statistics_until": None,
        "statistics_note": "disabled",
    }
    if call.data[ATTR_IMPORT_STATISTICS]:
        statistics = await _async_import_statistics(hass, entry, coordinator, profile)

    first, last = profile.first, profile.last
    response: dict[str, Any] = {
        "rows": profile.rows,
        "rows_skipped": profile.rows_skipped,
        "duplicates": profile.duplicates,
        "quarters": len(profile.quarters),
        "value_column": profile.value_column,
        "value_column_source": profile.value_column_source,
        "period_start": coordinator.local_iso(first),
        "period_end": coordinator.local_iso(last + calc.QUARTER) if last else None,
        "months": sorted({calc.quarter_month_key(start, coordinator.tz) for start in profile.quarters}),
        **merged,
        **statistics,
    }
    _LOGGER.info(
        "%s: Lastgang importiert (%s Viertelstunden, Monate übernommen %s, zusammengeführt %s, "
        "übersprungen %s, Statistik-Stunden %s)",
        entry.title,
        response["quarters"],
        merged["months_imported"],
        merged["months_merged"],
        merged["months_skipped"],
        statistics["statistics_hours"],
    )
    return response if call.return_response else None


def _floor_hour(moment: datetime) -> datetime:
    return datetime.fromtimestamp(moment.timestamp() // 3600 * 3600, tz=UTC)


async def _async_import_statistics(
    hass: HomeAssistant,
    entry: NetzentgeltConfigEntry,
    coordinator: NetzentgeltCoordinator,
    profile: calc.LoadProfile,
) -> dict[str, Any]:
    """Stundenwerte als Langzeitstatistik des 15-Min-Sensors importieren.

    Nur Stunden VOR der ersten vorhandenen Statistik-Stunde dieses Sensors und
    vor Anlage der Entity — eigene Messwerte werden nie überschrieben, und
    der Recorder gerät nicht mit später selbst berechneten Stunden in Konflikt.
    """
    hours = calc.hourly_statistics(profile.quarters)
    result: dict[str, Any] = {
        "statistics_hours": 0,
        "statistics_hours_skipped": len(hours),
        "statistics_until": None,
        "statistics_note": None,
    }
    if not hours:
        return result
    if "recorder" not in hass.config.components:
        result["statistics_note"] = "recorder_not_loaded"
        return result
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_quarter_power")
    reg_entry = registry.async_get(entity_id) if entity_id else None
    if entity_id is None or reg_entry is None:
        result["statistics_note"] = "entity_not_found"
        return result

    # Recorder erst hier importieren: die Integration funktioniert auch ohne ihn.
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMeanType,
        StatisticMetaData,
    )
    from homeassistant.components.recorder.statistics import async_import_statistics
    from homeassistant.util.unit_conversion import PowerConverter

    start, end = hours[0].start, hours[-1].start + HOUR
    first_existing, stat_unit = await get_instance(hass).async_add_executor_job(
        _existing_statistics, hass, entity_id, start, end
    )
    cutoff = min(end, _floor_hour(reg_entry.created_at))
    if first_existing is not None:
        cutoff = min(cutoff, first_existing)

    unit = stat_unit
    if unit is None:
        state = hass.states.get(entity_id)
        unit = state.attributes.get("unit_of_measurement") if state else None
        unit = unit if unit in _POWER_FACTORS_FROM_KW else "kW"
    factor = _POWER_FACTORS_FROM_KW.get(unit)
    result["statistics_until"] = coordinator.local_iso(cutoff)
    if factor is None:
        result["statistics_note"] = "unsupported_unit"
        return result

    selected = [hour for hour in hours if hour.start < cutoff]
    if selected:
        metadata: StatisticMetaData = {
            "mean_type": StatisticMeanType.ARITHMETIC,
            "has_sum": False,
            "name": None,
            "source": "recorder",
            "statistic_id": entity_id,
            "unit_class": PowerConverter.UNIT_CLASS,
            "unit_of_measurement": unit,
        }
        async_import_statistics(
            hass,
            metadata,
            [
                StatisticData(
                    start=hour.start,
                    mean=hour.mean * factor,
                    min=hour.min * factor,
                    max=hour.max * factor,
                )
                for hour in selected
            ],
        )
    result["statistics_hours"] = len(selected)
    result["statistics_hours_skipped"] = len(hours) - len(selected)
    return result


def _existing_statistics(
    hass: HomeAssistant, statistic_id: str, start: datetime, end: datetime
) -> tuple[datetime | None, str | None]:
    """(Beginn der ersten vorhandenen Statistik-Stunde ≤ ``end``, Statistik-Einheit).

    Liegt irgendeine Statistik vor ``start``, wird ``start`` geliefert — dann
    ist nichts zu importieren. Läuft im Recorder-Executor.
    """
    from homeassistant.components.recorder.statistics import (
        get_metadata,
        statistics_during_period,
    )

    metadata = get_metadata(hass, statistic_ids={statistic_id})
    if statistic_id not in metadata:
        return None, None
    unit = metadata[statistic_id][1]["unit_of_measurement"]
    # Nur die Langzeit-Tabelle fragen: ``statistic_during_period`` mit offenem
    # Beginn bezieht die 5-Minuten-Statistik mit ein und meldete im Live-Betrieb
    # (HA 2026.9) auch dann Werte „vor start“, wenn es keine gab.
    # Stundenwerte statt "month": Monats-Buckets reichen über ``start`` hinaus.
    before = statistics_during_period(
        hass, datetime(1970, 1, 1, tzinfo=UTC), start, {statistic_id}, "hour", None, {"mean"}
    )
    if before.get(statistic_id):
        return start, unit
    rows = statistics_during_period(hass, start, end, {statistic_id}, "hour", None, {"mean"})
    series = rows.get(statistic_id) or []
    if series:
        return dt_util.utc_from_timestamp(series[0]["start"]), unit
    return None, unit
