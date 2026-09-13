"""Beispiel-Dashboards und README verwenden nur Entity-IDs, die die Integration (Deutsch) anlegt."""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest
import yaml

pytest.importorskip("pytest_homeassistant_custom_component")

from .test_entities import ENGLISH_IDS, GERMAN_IDS

ROOT = Path(__file__).parents[1]
ID_PATTERN = re.compile(r"\b(?:sensor|binary_sensor|number|switch)\.netzentgelt_[a-z0-9_]+")


def _referenced(name: str) -> set[str]:
    text = (ROOT / name).read_text(encoding="utf-8")
    if name.endswith(".yaml"):
        # nur echte Konfiguration, keine Kommentare (dort stehen englische Beispiel-IDs)
        text = json.dumps(yaml.safe_load(text), ensure_ascii=False)
    else:
        text = text.split("## English summary")[0]
    return set(ID_PATTERN.findall(text))


FILES = ["examples/dashboard.yaml", "examples/dashboard-apexcharts.yaml", "README.md"]


@pytest.mark.parametrize("name", FILES)
def test_referenced_entity_ids_exist(name: str) -> None:
    used = _referenced(name)
    assert used, name
    assert used <= set(GERMAN_IDS.values()), used - set(GERMAN_IDS.values())


def test_dashboards_contain_new_controls() -> None:
    for name in ("examples/dashboard.yaml", "examples/dashboard-apexcharts.yaml"):
        used = _referenced(name)
        assert {"number.netzentgelt_ziel_leistung", "switch.netzentgelt_peak_shaving_aktiv"} <= used, name
    assert "sensor.netzentgelt_lastprofil" in _referenced("examples/dashboard-apexcharts.yaml")


def test_english_dashboard_uses_english_ids() -> None:
    """Das englische Beispiel verwendet nur IDs einer englischen Installation — und dieselben Karten."""
    used = _referenced("examples/dashboard-apexcharts.en.yaml")
    assert used <= set(ENGLISH_IDS.values()), used - set(ENGLISH_IDS.values())
    german = _referenced("examples/dashboard-apexcharts.yaml")
    to_en = {GERMAN_IDS[k]: v for k, v in ENGLISH_IDS.items()}
    assert used == {to_en[i] for i in german}
