"""Konstanten der Netzentgelt-Integration."""

from __future__ import annotations

from typing import Any, Final

DOMAIN: Final = "netzentgelt"
DEFAULT_NAME: Final = "Netzentgelt"
MANUFACTURER: Final = "ha-netzentgelt"
MODEL: Final = "Leistungspreis-Rechner (SNE-G-V)"

# --- Config (Quellen) -------------------------------------------------------
CONF_ENERGY_ENTITY: Final = "energy_entity"
CONF_POWER_ENTITY: Final = "power_entity"

# --- Options ----------------------------------------------------------------
CONF_TARGET_KW: Final = "target_kw"
CONF_TIER_LIMIT_KW: Final = "tier_limit_kw"
CONF_AGREED_KW: Final = "agreed_kw"
CONF_MINIMUM_KW: Final = "minimum_kw"
CONF_PRICE_TIER1: Final = "price_tier1"
CONF_PRICE_TIER2: Final = "price_tier2"
CONF_PRICE_STANDARD: Final = "price_standard"
CONF_PRICE_SNAP: Final = "price_snap"
CONF_PRICE_WINAP: Final = "price_winap"
CONF_PLAUSIBILITY_KW: Final = "plausibility_kw"
CONF_HYSTERESIS_KW: Final = "hysteresis_kw"
CONF_BILLING_START: Final = "billing_start"

# Richtwerte — NICHT amtlich. Leistungspreise: Median der 14 Netzbereiche im
# Begutachtungsentwurf der SNE-T-V 2027 (253,5 / 380,5 Cent/kW und Monat, mal 12);
# die Tarifverordnung ist noch nicht kundgemacht, je Netzbereich weichen die Werte
# stark ab (Tabelle im README). Arbeitspreise 0 = nicht gesetzt.
# Stichtag: Ende der Übergangsregel laut § 6 Abs. 3a SNE-G-V (30.11.2027).
DEFAULT_BILLING_START: Final = "2027-12-01"
DEFAULT_OPTIONS: Final[dict[str, Any]] = {
    CONF_TARGET_KW: 10.0,
    CONF_TIER_LIMIT_KW: 10.0,
    CONF_AGREED_KW: 10.0,
    CONF_MINIMUM_KW: 2.0,
    CONF_PRICE_TIER1: 30.42,
    CONF_PRICE_TIER2: 45.66,
    CONF_PRICE_STANDARD: 0.0,
    CONF_PRICE_SNAP: 0.0,
    CONF_PRICE_WINAP: 0.0,
    CONF_PLAUSIBILITY_KW: 60.0,
    CONF_HYSTERESIS_KW: 0.2,
    CONF_BILLING_START: DEFAULT_BILLING_START,
}

# Bereich der Ziel-Leistung (Options-Flow und Number-Entity). Obergrenze des
# Schiebereglers ist die Plausibilitätsgrenze (höchstens TARGET_MAX_KW) — ein
# Ziel darüber lehnt die Validierung ohnehin ab.
TARGET_MIN_KW: Final = 0.5
TARGET_MAX_KW: Final = 1000.0

# Reine Wert-Optionen: Änderung wird live übernommen, ohne die Integration neu
# zu laden (ein Neuladen würde die laufende Viertelstunde ungültig machen).
# Alles andere (Quell-Sensoren, Plausibilitätsgrenze, Titel) lädt neu.
LIVE_OPTION_KEYS: Final = frozenset(
    {
        CONF_TARGET_KW,
        CONF_TIER_LIMIT_KW,
        CONF_AGREED_KW,
        CONF_MINIMUM_KW,
        CONF_PRICE_TIER1,
        CONF_PRICE_TIER2,
        CONF_PRICE_STANDARD,
        CONF_PRICE_SNAP,
        CONF_PRICE_WINAP,
        CONF_HYSTERESIS_KW,
        CONF_BILLING_START,
    }
)

# --- Service import_load_profile ---------------------------------------------
SERVICE_IMPORT_LOAD_PROFILE: Final = "import_load_profile"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_PATH: Final = "path"
ATTR_TIMESTAMP_IS_END: Final = "timestamp_is_end"
ATTR_OVERWRITE: Final = "overwrite"
ATTR_IMPORT_STATISTICS: Final = "import_statistics"
IMPORT_SUFFIXES: Final = (".csv", ".txt")
IMPORT_MAX_BYTES: Final = 20 * 1024 * 1024  # 3 Jahre Viertelstunden ≈ 4 MB

ENERGY_UNITS: Final = ("Wh", "kWh", "MWh")
POWER_UNITS: Final = ("W", "kW")
ENERGY_STATE_CLASSES: Final = ("total", "total_increasing")

STORAGE_VERSION: Final = 1
STORAGE_SAVE_DELAY: Final = 10  # Sekunden
FORECAST_INTERVAL_SECONDS: Final = 30
QUARTER_MINUTES: Final = [0, 15, 30, 45]

ATTRIBUTION: Final = (
    "Näherung nach SNE-G-V (BGBl. II Nr. 299/2026); Preise sind Richtwerte, "
    "maßgeblich ist der Zähler des Netzbetreibers."
)
