"""Tests für den Service ``netzentgelt.import_load_profile`` (Pfadprüfung, Merge, Statistik).

Läuft mit pytest-homeassistant-custom-component; der Statistik-Import wird gegen
den echten Recorder mit In-Memory-SQLite geprüft (Fixture ``recorder_mock``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.netzentgelt.const import DOMAIN, SERVICE_IMPORT_LOAD_PROFILE
from custom_components.netzentgelt.services import ImportPathError, resolve_import_path

from .test_init import _setup, _state

VIENNA = dt_util.get_time_zone("Europe/Vienna")
START = datetime(2026, 9, 11, 9, 56, tzinfo=VIENNA)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Custom Integrations in allen Tests dieses Moduls laden."""


def _csv(rows: list[tuple[datetime, float]]) -> str:
    """Synthetischer Export im Netz-OÖ-Format (Zeitstempel = Beginn, Ortszeit, Dezimalkomma)."""
    lines = ['"Datum";"kWh";"kW";"Status";']
    for start, kw in rows:
        local = start.astimezone(VIENNA)
        kwh = f"{kw / 4:.3f}".replace(".", ",")
        lines.append(f'"{local:%d.%m.%Y %H:%M}";{kwh};{f"{kw:.3f}".replace(".", ",")};"VALID";')
    return "\ufeff" + "\n".join(lines) + "\n"


def _quarters(first: datetime, count: int, kw: float) -> list[tuple[datetime, float]]:
    return [(first + timedelta(minutes=15 * i), kw) for i in range(count)]


async def _call(hass: HomeAssistant, entry_id: str, path: str, **extra: object) -> dict:
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_IMPORT_LOAD_PROFILE,
        {"config_entry_id": entry_id, "path": path, **extra},
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done()
    assert isinstance(response, dict)
    return response


# ------------------------------------------------------------ Pfadprüfung


def test_resolve_import_path_rules(tmp_path: Path) -> None:
    config = tmp_path / "config"
    (config / "netzentgelt").mkdir(parents=True)
    (config / ".storage").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (config / "netzentgelt" / "lastgang.csv").write_text("x")
    (config / ".storage" / "geheim.csv").write_text("x")
    (config / "secrets.yaml").write_text("x")
    (outside / "fremd.csv").write_text("x")
    (config / "link.csv").symlink_to(outside / "fremd.csv")

    def never(_: str) -> bool:
        return False

    def allow_outside(path: str) -> bool:
        return Path(path).is_relative_to(outside)

    assert (
        resolve_import_path(str(config), "netzentgelt/lastgang.csv", never)
        == (config / "netzentgelt" / "lastgang.csv").resolve()
    )
    assert resolve_import_path(str(config), str(config / "netzentgelt" / "lastgang.csv"), never).name == (
        "lastgang.csv"
    )
    cases = {
        "../outside/fremd.csv": "path_not_allowed",  # Traversal
        "netzentgelt/../../outside/fremd.csv": "path_not_allowed",
        str(outside / "fremd.csv"): "path_not_allowed",  # absolut außerhalb
        "link.csv": "path_not_allowed",  # Symlink nach außen
        ".storage/geheim.csv": "path_not_allowed",  # versteckter Ordner
        "secrets.yaml": "invalid_file_type",
        "netzentgelt/fehlt.csv": "file_not_found",
        "": "path_not_allowed",
    }
    for raw, reason in cases.items():
        with pytest.raises(ImportPathError) as err:
            resolve_import_path(str(config), raw, never)
        assert err.value.reason == reason, raw
    # außerhalb nur mit allowlist_external_dirs (hass.config.is_allowed_path)
    assert resolve_import_path(str(config), str(outside / "fremd.csv"), allow_outside).name == "fremd.csv"
    assert resolve_import_path(str(config), "link.csv", allow_outside).name == "fremd.csv"


async def test_service_rejects_traversal_and_bad_entry(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, tmp_path: Path
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (tmp_path / "geheim.csv").write_text(_csv(_quarters(START, 4, 1.0)))
    hass.config.config_dir = str(config)
    entry, _ = await _setup(hass, freezer, START)
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, entry.entry_id, "../geheim.csv")
    assert err.value.translation_key == "path_not_allowed"
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "gibt-es-nicht", "x.csv")
    assert err.value.translation_key == "entry_not_found"
    (config / "leer.csv").write_text("Datum;kW\n")
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, entry.entry_id, "leer.csv")
    assert err.value.translation_key == "no_data"


async def test_service_requires_admin(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_read_only_user: MockUser
) -> None:
    entry, _ = await _setup(hass, freezer, START)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_LOAD_PROFILE,
            {"config_entry_id": entry.entry_id, "path": "x.csv"},
            blocking=True,
            return_response=True,
            context=Context(user_id=hass_read_only_user.id),
        )


# ------------------------------------------------------------ Merge


async def test_import_merges_history_and_day_profiles(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, tmp_path: Path
) -> None:
    hass.config.config_dir = str(tmp_path)
    entry, meter = await _setup(hass, freezer, START)
    await meter.run(19 * 60 + 30, kw=2.0)  # eigene Messung: 10:00–10:15 mit 2 kW

    rows = [
        *_quarters(datetime(2026, 7, 3, 18, 0, tzinfo=VIENNA), 4, 9.0),  # Juli: nur Import
        *_quarters(datetime(2026, 8, 14, 12, 0, tzinfo=VIENNA), 2, 4.0),
        (datetime(2026, 9, 10, 10, 30, tzinfo=VIENNA), 7.0),  # gestern
        (datetime(2026, 9, 11, 8, 0, tzinfo=VIENNA), 1.5),  # heute, eigener Slot leer
        (datetime(2026, 9, 11, 10, 0, tzinfo=VIENNA), 5.0),  # heute, eigener Wert existiert
    ]
    (tmp_path / "netzentgelt").mkdir()
    (tmp_path / "netzentgelt" / "lastgang.csv").write_text(_csv(rows), encoding="utf-8")

    response = await _call(hass, entry.entry_id, "netzentgelt/lastgang.csv", import_statistics=True)
    assert response["rows"] == 9 and response["rows_skipped"] == 0 and response["quarters"] == 9
    assert response["value_column"] == "kW"
    assert response["period_start"] == "2026-07-03T18:00:00+02:00"
    assert response["period_end"] == "2026-09-11T10:15:00+02:00"
    assert response["months"] == ["2026-07", "2026-08", "2026-09"]
    assert response["months_imported"] == ["2026-07", "2026-08"]
    assert response["months_merged"] == ["2026-09"]
    assert response["months_skipped"] == [] and response["months_overwritten"] == []
    assert response["day_slots_filled"] == 2  # gestern 10:30 und heute 08:00
    assert response["statistics_note"] == "recorder_not_loaded"

    # September: Spitze = Maximum aus Messung (2 kW) und Import (7 kW, gestern 10:30)
    peak = _state(hass, entry, "month_peak")
    assert float(peak.state) == pytest.approx(7.0)
    assert peak.attributes["source"] == "mixed"
    history = peak.attributes["history"]
    assert history["2026-07"]["source"] == "imported"
    assert history["2026-07"]["peak_kw"] == pytest.approx(9.0)
    assert history["2026-07"]["billed_kw"] == pytest.approx(9.0)
    assert history["2026-07"]["capacity_cost_eur"] == pytest.approx(25.37, abs=0.01)  # 9 × 33,82 / 12
    assert history["2026-08"]["source"] == "imported"
    assert float(_state(hass, entry, "billed_power").state) == pytest.approx(7.0)

    profile = _state(hass, entry, "load_profile")
    assert profile.state == "10:30"
    assert profile.attributes["yesterday_kw"][42] == pytest.approx(7.0)
    assert profile.attributes["today_kw"][32] == pytest.approx(1.5)  # 08:00 aus dem Import
    assert profile.attributes["today_kw"][40] == pytest.approx(2.0, abs=0.01)  # eigene Messung bleibt

    # Erneuter Import: nichts ändert sich (idempotent), Store enthält den Import-Teil
    before = entry.runtime_data.tracker.as_dict()
    await _call(hass, entry.entry_id, "netzentgelt/lastgang.csv", import_statistics=False)
    assert entry.runtime_data.tracker.as_dict() == before

    # overwrite: September nur noch aus dem Import
    response = await _call(hass, entry.entry_id, "netzentgelt/lastgang.csv", overwrite=True)
    assert response["months_overwritten"] == ["2026-09"]
    assert _state(hass, entry, "month_peak").attributes["source"] == "imported"


async def test_partial_reimport_and_overwrite_keep_data_outside_file(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, tmp_path: Path, hass_storage: dict
) -> None:
    """Befunde 2 und 3 über den Service: Teil-Import und overwrite verlieren nichts."""
    hass.config.config_dir = str(tmp_path)
    entry, meter = await _setup(hass, freezer, START)
    await meter.run(19 * 60 + 30, kw=2.0)  # eigene Messung 11.09. 10:00–10:15 (2 kW)

    first = [
        *_quarters(datetime(2026, 7, 15, 0, 0, tzinfo=VIENNA), 4, 1.0),
        (datetime(2026, 8, 4, 18, 0, tzinfo=VIENNA), 12.0),
        *_quarters(datetime(2026, 8, 14, 0, 0, tzinfo=VIENNA), 4, 1.0),
    ]
    second = [
        *_quarters(datetime(2026, 8, 15, 0, 0, tzinfo=VIENNA), 4, 1.0),
        (datetime(2026, 8, 20, 18, 0, tzinfo=VIENNA), 4.0),
        *_quarters(datetime(2026, 9, 3, 0, 0, tzinfo=VIENNA), 4, 1.0),
    ]
    (tmp_path / "a.csv").write_text(_csv(first), encoding="utf-8")
    (tmp_path / "b.csv").write_text(_csv(second), encoding="utf-8")

    response = await _call(hass, entry.entry_id, "a.csv", import_statistics=False)
    assert response["months_import_extended"] == [] and response["import_quarters_replaced"] == 0
    response = await _call(hass, entry.entry_id, "b.csv", import_statistics=False, overwrite=True)
    assert response["months"] == ["2026-08", "2026-09"]
    assert response["months_import_extended"] == ["2026-08"]
    assert response["import_quarters_replaced"] == 0
    assert response["months_overwritten"] == []
    assert response["months_overwrite_skipped"] == ["2026-09"]  # Messung 11.09. liegt nach dem Import

    peak = _state(hass, entry, "month_peak")
    history = peak.attributes["history"]
    assert history["2026-08"]["peak_kw"] == pytest.approx(12.0)  # früherer Import-Teil bleibt
    assert history["2026-08"]["imported_quarters"] == 10
    assert history["2026-09"]["source"] == "mixed"
    assert history["2026-09"]["valid_quarters"] >= 1  # eigene Messung nicht verworfen
    assert float(peak.state) == pytest.approx(2.0, abs=0.01)

    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}.import"]["data"]
    assert sorted(stored) == ["2026-07", "2026-08", "2026-09"]

    # Eintrag löschen entfernt auch die Rohwerte
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.{entry.entry_id}.import" not in hass_storage
    assert f"{DOMAIN}.{entry.entry_id}" not in hass_storage
