"""Blueprints: statisch laden/prüfen und im HA-Testharness tatsächlich ausführen.

Die Ausführung läuft gegen simulierte Entities (gesetzte States, gemockte
Services) — nicht gegen eine echte Wallbox oder ein echtes Handy.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import shutil
from typing import Any

import pytest
import yaml

BLUEPRINT_DIR = Path(__file__).parents[1] / "blueprints" / "automation" / "netzentgelt"
BLUEPRINTS = sorted(BLUEPRINT_DIR.glob("*.yaml"))
REPO_URL = "https://github.com/neuhubereco/ha-netzentgelt/blob/main/blueprints/automation/netzentgelt/"


class _Input:
    def __init__(self, name: str) -> None:
        self.name = name


class _BlueprintLoader(yaml.SafeLoader):
    """YAML-Loader mit dem Custom-Tag ``!input``."""


_BlueprintLoader.add_constructor("!input", lambda loader, node: _Input(loader.construct_scalar(node)))


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.load(fh, Loader=_BlueprintLoader)  # SafeLoader-Unterklasse, nur !input ergänzt


def _declared_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    """Eingaben inkl. Abschnitten (``input`` innerhalb eines Abschnitts)."""
    declared: dict[str, Any] = {}
    for key, value in inputs.items():
        if isinstance(value, dict) and "input" in value and "selector" not in value:
            declared.update(value["input"])
        else:
            declared[key] = value
    return declared


def _used_inputs(node: Any) -> set[str]:
    if isinstance(node, _Input):
        return {node.name}
    if isinstance(node, dict):
        return set().union(*(_used_inputs(v) for v in node.values())) if node else set()
    if isinstance(node, list):
        return set().union(*(_used_inputs(v) for v in node)) if node else set()
    return set()


def test_three_blueprints_exist() -> None:
    names = [p.name for p in BLUEPRINTS]
    assert names == ["benachrichtigung.yaml", "last_abwerfen.yaml", "wallbox_spielraum.yaml"]


@pytest.mark.parametrize("path", BLUEPRINTS, ids=lambda p: p.name)
def test_blueprint_required_fields_and_inputs(path: Path) -> None:
    data = _load(path)
    meta = data["blueprint"]
    assert meta["name"].startswith("Netzentgelt: ")
    assert meta["domain"] == "automation"
    assert len(meta["description"]) > 80
    assert meta["source_url"] == REPO_URL + path.name
    assert meta["homeassistant"]["min_version"]
    assert data["mode"] in {"single", "queued", "restart", "parallel"}
    assert data["triggers"] and data["actions"]

    declared = _declared_inputs(meta["input"])
    for key, spec in declared.items():
        assert spec.get("name"), key
        assert "selector" in spec, key
    body = {k: v for k, v in data.items() if k != "blueprint"}
    used = _used_inputs(body)
    assert used == set(declared), f"unbenutzt: {set(declared) - used}, unbekannt: {used - set(declared)}"


# ---------------------------------------------------------------- im Testharness

pytest.importorskip("pytest_homeassistant_custom_component")

from freezegun.api import FrozenDateTimeFactory  # noqa: E402
from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA  # noqa: E402
from homeassistant.core import HomeAssistant, ServiceCall  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from homeassistant.util import dt as dt_util, yaml as yaml_util  # noqa: E402
from pytest_homeassistant_custom_component.common import async_fire_time_changed  # noqa: E402


@pytest.mark.parametrize("path", BLUEPRINTS, ids=lambda p: p.name)
def test_blueprint_matches_home_assistant_schema(path: Path) -> None:
    """Validiert Blueprint-Kopf inkl. aller Selector-Konfigurationen mit HA selbst."""
    BLUEPRINT_SCHEMA(yaml_util.load_yaml(str(path)))


def _install_blueprints(hass: HomeAssistant, tmp_path: Path) -> None:
    hass.config.config_dir = str(tmp_path)
    target = tmp_path / "blueprints" / "automation" / "netzentgelt"
    target.parent.mkdir(parents=True)
    shutil.copytree(BLUEPRINT_DIR, target)


async def _setup_automation(hass: HomeAssistant, blueprint: str, inputs: dict[str, Any]) -> str:
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": "test",
                    "alias": "test",
                    "use_blueprint": {"path": f"netzentgelt/{blueprint}", "input": inputs},
                }
            ]
        },
    )
    await hass.async_block_till_done()
    state = hass.states.get("automation.test")
    assert state is not None and state.state == "on", "Blueprint-Automation ungültig"
    return "automation.test"


def _mock_service(hass: HomeAssistant, domain: str, service: str, apply: Any = None) -> list[ServiceCall]:
    calls: list[ServiceCall] = []

    async def handler(call: ServiceCall) -> None:
        calls.append(call)
        if apply is not None:
            apply(call)

    hass.services.async_register(domain, service, handler)
    return calls


async def _set(hass: HomeAssistant, entity_id: str, state: str, **attrs: Any) -> None:
    hass.states.async_set(entity_id, state, attrs)
    await hass.async_block_till_done()


async def _settle() -> None:
    """Event-Loop laufen lassen, ohne auf wartende Automationen zu blocken.

    ``async_block_till_done`` wartet auch auf Automationen, die in einem
    ``wait_template``/``wait_for_trigger`` hängen — das würde den Test blockieren.
    """
    for _ in range(50):
        await asyncio.sleep(0)


async def _set_nowait(hass: HomeAssistant, entity_id: str, state: str, **attrs: Any) -> None:
    hass.states.async_set(entity_id, state, attrs)
    await _settle()


async def test_wallbox_blueprint_regulates_pauses_and_resets(hass: HomeAssistant, tmp_path: Path) -> None:
    _install_blueprints(hass, tmp_path)
    current = "number.wallbox_ladestrom"
    release = "switch.wallbox_freigabe"
    headroom = "sensor.netzentgelt_spielraum"
    shaving = "switch.netzentgelt_peak_shaving_aktiv"

    def set_current(call: ServiceCall) -> None:
        for entity_id in call.data["entity_id"]:
            hass.states.async_set(entity_id, str(float(call.data["value"])))

    def set_switch(state: str) -> Any:
        def apply(call: ServiceCall) -> None:
            for entity_id in call.data["entity_id"]:
                hass.states.async_set(entity_id, state)

        return apply

    set_calls = _mock_service(hass, "number", "set_value", set_current)
    off_calls = _mock_service(hass, "switch", "turn_off", set_switch("off"))
    on_calls = _mock_service(hass, "switch", "turn_on", set_switch("on"))
    hass.states.async_set(current, "10")
    hass.states.async_set(release, "on")
    hass.states.async_set(headroom, "2.0", {"unit_of_measurement": "kW"})
    hass.states.async_set(shaving, "off")
    await _setup_automation(
        hass,
        "wallbox_spielraum.yaml",
        {
            "headroom_sensor": headroom,
            "peak_shaving_switch": shaving,
            "current_entity": current,
            "pause_switch": release,
        },
    )

    # Peak-Shaving aus → keine Regelung
    await _set(hass, headroom, "3.0")
    assert set_calls == []

    # Einschalten → 10 A + 3 kW / 0,69 kW/A = 14,3 → 14 A
    await _set(hass, shaving, "on")
    assert [c.data["value"] for c in set_calls] == [14]
    # Kleine Änderung innerhalb des Totbands → nichts
    await _set(hass, headroom, "0.5")
    assert len(set_calls) == 1
    # Negativer Spielraum → drosseln: 14 − 3 / 0,69 = 9,65 → 9 A
    await _set(hass, headroom, "-3.0")
    assert set_calls[-1].data["value"] == 9
    # Selbst der Mindeststrom ist zu viel → 6 A und Laden pausieren (Automation wartet dann)
    await _set_nowait(hass, headroom, "-6.0")
    assert set_calls[-1].data["value"] == 6
    assert [c.data["entity_id"] for c in off_calls] == [[release]]
    assert hass.states.get(release).state == "off"
    # Noch zu wenig Platz (< (6 + 1) × 0,69 = 4,83 kW) → bleibt pausiert
    await _set_nowait(hass, headroom, "3.0")
    assert on_calls == []
    # Wieder Platz → Laden fortsetzen
    await _set(hass, headroom, "5.0")
    assert [c.data["entity_id"] for c in on_calls] == [[release]]
    # Peak-Shaving aus → Höchststrom
    await _set(hass, shaving, "off")
    assert set_calls[-1].data["value"] == 16


async def test_load_shedding_blueprint_restores_only_own_loads(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, tmp_path: Path
) -> None:
    _install_blueprints(hass, tmp_path)
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(
        hass,
        "input_boolean",
        {"input_boolean": {"heizstab": {"initial": True}, "pool": {"initial": False}}},
    )
    imminent = "binary_sensor.netzentgelt_spitze_droht"
    shaving = "switch.netzentgelt_peak_shaving_aktiv"
    hass.states.async_set(imminent, "off")
    hass.states.async_set(shaving, "on")
    await _setup_automation(
        hass,
        "last_abwerfen.yaml",
        {
            "peak_imminent": imminent,
            "peak_shaving_switch": shaving,
            "loads": ["input_boolean.heizstab", "input_boolean.pool"],
            "wait_minutes": 5,
        },
    )

    await _set_nowait(hass, imminent, "on")
    assert hass.states.get("input_boolean.heizstab").state == "off"
    assert hass.states.get("input_boolean.pool").state == "off"

    # Spitze vorbei, aber Wartezeit läuft noch
    await _set_nowait(hass, imminent, "off")
    freezer.tick(timedelta(minutes=4))
    async_fire_time_changed(hass)
    await _settle()
    assert hass.states.get("input_boolean.heizstab").state == "off"

    freezer.tick(timedelta(minutes=1, seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("input_boolean.heizstab").state == "on"  # selbst ausgeschaltet → wieder an
    assert hass.states.get("input_boolean.pool").state == "off"  # war vorher aus → bleibt aus

    # Peak-Shaving während der Wartezeit ausgeschaltet → sofort zurück
    await _set_nowait(hass, imminent, "on")
    assert hass.states.get("input_boolean.heizstab").state == "off"
    await _set(hass, shaving, "off")
    assert hass.states.get("input_boolean.heizstab").state == "on"

    # Peak-Shaving aus → Automation schaltet gar nichts
    await _set(hass, imminent, "off")
    await _set(hass, imminent, "on")
    assert hass.states.get("input_boolean.heizstab").state == "on"


async def test_notification_blueprint_messages_and_throttle(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, tmp_path: Path
) -> None:
    _install_blueprints(hass, tmp_path)
    await hass.config.async_set_time_zone("Europe/Vienna")  # Uhrzeit in der Nachricht = HA-Ortszeit
    notes = _mock_service(hass, "notify", "handy")
    imminent = "binary_sensor.netzentgelt_spitze_droht"
    peak = "sensor.netzentgelt_monatsspitze"
    hass.states.async_set(imminent, "off", {"target_kw": 10.0})
    hass.states.async_set("sensor.netzentgelt_prognose_viertelstunde", "11.24")
    hass.states.async_set("sensor.netzentgelt_spielraum", "-1.43")
    hass.states.async_set(peak, "8.0", {"peak_quarter_start": "2026-09-11T18:30:00+02:00"})
    hass.states.async_set("number.netzentgelt_staffelgrenze", "10.0")
    await _setup_automation(
        hass,
        "benachrichtigung.yaml",
        {
            "peak_imminent": imminent,
            "forecast_sensor": "sensor.netzentgelt_prognose_viertelstunde",
            "headroom_sensor": "sensor.netzentgelt_spielraum",
            "month_peak_sensor": peak,
            "tier_limit": "number.netzentgelt_staffelgrenze",
            "notify_action": "notify.handy",
        },
    )

    await _set(hass, imminent, "on", target_kw=10.0)
    assert len(notes) == 1
    assert notes[0].data["title"] == "Netzentgelt: Spitze droht"
    assert "Prognose 11.2 kW" in notes[0].data["message"]
    assert "Ziel 10.0 kW" in notes[0].data["message"]
    assert "1.4 kW weniger" in notes[0].data["message"]

    # Innerhalb der Drossel (15 min) keine zweite Nachricht
    await _set(hass, imminent, "off", target_kw=10.0)
    freezer.tick(timedelta(minutes=5))
    await _set(hass, imminent, "on", target_kw=10.0)
    assert len(notes) == 1

    # Monatsspitze steigt über die Staffelgrenze → Nachricht
    await _set(hass, peak, "10.5", peak_quarter_start="2026-09-11T18:45:00+02:00")
    assert len(notes) == 2
    assert notes[1].data["title"] == "Netzentgelt: neue Monatsspitze"
    assert notes[1].data["message"].startswith("10.5 kW (11.09. 18:45)")
    # nur +0,2 kW → keine Nachricht; +0,7 kW → Nachricht
    await _set(hass, peak, "10.7")
    assert len(notes) == 2
    await _set(hass, peak, "11.2")
    assert len(notes) == 3

    # Nach Ablauf der Drossel wieder „Spitze droht“
    freezer.tick(timedelta(minutes=16))
    await _set(hass, imminent, "off", target_kw=10.0)
    await _set(hass, imminent, "on", target_kw=10.0)
    assert len(notes) == 4


async def test_load_shedding_restore_at_next_quarter(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, tmp_path: Path
) -> None:
    """Option „erst zur nächsten Viertelstunde wieder einschalten“."""
    vienna = dt_util.get_time_zone("Europe/Vienna")
    await hass.config.async_set_time_zone("Europe/Vienna")
    freezer.move_to(datetime(2026, 9, 11, 10, 2, 0, tzinfo=vienna))
    _install_blueprints(hass, tmp_path)
    assert await async_setup_component(hass, "homeassistant", {})
    booleans = {"input_boolean": {"boiler": {"initial": True}}}
    assert await async_setup_component(hass, "input_boolean", booleans)
    imminent = "binary_sensor.netzentgelt_spitze_droht"
    hass.states.async_set(imminent, "off")
    hass.states.async_set("switch.netzentgelt_peak_shaving_aktiv", "on")
    await _setup_automation(
        hass,
        "last_abwerfen.yaml",
        {
            "peak_imminent": imminent,
            "peak_shaving_switch": "switch.netzentgelt_peak_shaving_aktiv",
            "loads": ["input_boolean.boiler"],
            "wait_minutes": 2,
            "restore_at_quarter": True,
        },
    )
    await _set_nowait(hass, imminent, "on")
    assert hass.states.get("input_boolean.boiler").state == "off"
    await _set_nowait(hass, imminent, "off")
    # 10:04:05: Wartezeit (2 min) vorbei, aber noch nicht :15 → bleibt aus
    freezer.tick(timedelta(minutes=2, seconds=5))
    async_fire_time_changed(hass)
    await _settle()
    assert hass.states.get("input_boolean.boiler").state == "off"
    # 10:15:00 → neue Viertelstunde → wieder ein
    freezer.move_to(datetime(2026, 9, 11, 10, 15, 0, tzinfo=vienna))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("input_boolean.boiler").state == "on"

    async def at(minute: int, second: int = 0) -> None:
        freezer.move_to(datetime(2026, 9, 11, 10, minute, second, tzinfo=vienna))
        async_fire_time_changed(hass)
        await _settle()

    # Droht an der Grenze wieder eine Spitze, bleibt die Last bis zur nächsten Grenze aus
    await at(20)
    await _set_nowait(hass, imminent, "on")
    assert hass.states.get("input_boolean.boiler").state == "off"
    await _set_nowait(hass, imminent, "off")
    await at(23, 5)  # Wartezeit vorbei, Automation wartet auf :30
    await _set_nowait(hass, imminent, "on")  # erneut drohende Spitze (Automation läuft noch)
    await at(30)
    assert hass.states.get("input_boolean.boiler").state == "off"
    await at(31)
    await _set_nowait(hass, imminent, "off")  # nach der ersten Minute der Viertelstunde
    await at(33)
    assert hass.states.get("input_boolean.boiler").state == "off"
    await at(45)
    await hass.async_block_till_done()
    assert hass.states.get("input_boolean.boiler").state == "on"

    # Peak-Shaving während des Wartens auf die Viertelstunde ausgeschaltet → sofort ein
    await at(50)
    await _set_nowait(hass, imminent, "on")
    await _set_nowait(hass, imminent, "off")
    await at(52, 5)
    assert hass.states.get("input_boolean.boiler").state == "off"
    await _set(hass, "switch.netzentgelt_peak_shaving_aktiv", "off")
    assert hass.states.get("input_boolean.boiler").state == "on"
