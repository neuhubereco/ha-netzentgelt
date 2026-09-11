"""Reine Rechenlogik für die Netzentgelt-Integration.

Dieses Modul importiert bewusst NICHTS aus Home Assistant, damit die
gesamte Mess- und Tariflogik mit einfachem ``pytest`` prüfbar ist.

Begriffe
--------
* Sample: ein gültiger Zählerstand (kWh) mit Zeitstempel.
* Serie: eine lückenlose Folge von Samples. Jeder Ausfall der Quelle
  (unavailable/unknown/nicht numerisch), jeder fallende Zählerstand und
  jeder unplausible Sprung beendet die Serie. Über Seriengrenzen hinweg
  wird NIE gerechnet.
* Grenze (Boundary): ein Viertelstundenzeitpunkt (:00/:15/:30/:45). Der
  Zählerstand an der Grenze wird aus dem letzten Sample davor und dem
  ersten danach linear interpoliert.
* Viertelstunde (Quarter): Intervall zwischen zwei Grenzen. Gültig nur,
  wenn beide Grenzen gültig und in derselben Serie sind und die
  Leistung plausibel ist.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
import csv
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from itertools import pairwise
import math
import re
from typing import Any

QUARTER = timedelta(minutes=15)
QUARTER_SECONDS = 900
QUARTER_HOURS = 0.25

# --- Gründe für ungültige Grenzen / Viertelstunden --------------------------
REASON_SOURCE_UNAVAILABLE = "source_unavailable"
REASON_COUNTER_DECREASED = "counter_decreased"
REASON_IMPLAUSIBLE_JUMP = "implausible_jump"
REASON_IMPLAUSIBLE_POWER = "implausible_power"
REASON_SAMPLE_GAP = "sample_gap"
REASON_NO_SAMPLE_BEFORE = "no_sample_before"
REASON_NO_SAMPLE_AFTER = "no_sample_after"
REASON_NO_START_BOUNDARY = "no_start_boundary"
REASON_SERIES_BREAK = "series_break"

# --- Tarifzeitfenster --------------------------------------------------------
TARIFF_SNAP = "snap"
TARIFF_WINAP = "winap"
TARIFF_STANDARD = "standard"
TARIFF_OPTIONS = [TARIFF_SNAP, TARIFF_WINAP, TARIFF_STANDARD]

SNAP_START = time(10, 0)
SNAP_END = time(16, 0)
WINAP_START = time(22, 0)
WINAP_END = time(4, 0)

# Uhrzeiten, an denen sich das Tarifzeitfenster ändern kann.
_TARIFF_CHANGE_TIMES = (WINAP_END, SNAP_START, SNAP_END, WINAP_START)

# --- Einheiten ---------------------------------------------------------------
_ENERGY_FACTORS_TO_KWH = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}
_POWER_FACTORS_TO_KW = {"W": 0.001, "kW": 1.0}


# =============================================================================
# Hilfsfunktionen: Einheiten, Rundung, Zeit
# =============================================================================


def energy_to_kwh(value: Any, unit: str | None) -> float | None:
    """Rechne einen Energiewert in kWh um.

    Liefert ``None`` für nicht numerische Werte, NaN/Inf oder unbekannte
    Einheiten. ``None`` bedeutet für den Aufrufer: Quelle ungültig.
    """
    factor = _ENERGY_FACTORS_TO_KWH.get(unit or "")
    number = _to_float(value)
    if factor is None or number is None:
        return None
    return number * factor


def power_to_kw(value: Any, unit: str | None) -> float | None:
    """Rechne eine Leistung in kW um; negative Werte (Einspeisung) zählen als 0."""
    factor = _POWER_FACTORS_TO_KW.get(unit or "")
    number = _to_float(value)
    if factor is None or number is None:
        return None
    return max(number * factor, 0.0)


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def round_half_up(value: float, digits: int = 2) -> float:
    """Kaufmännisch runden (ROUND_HALF_UP), nicht Pythons Banker's Rounding.

    Zweistufig: zuerst auf 9 Nachkommastellen (entfernt Gleitkomma-Rauschen
    wie 12.344999999999999), dann kaufmännisch auf ``digits`` Stellen.
    """
    dec = Decimal(repr(float(value))).quantize(Decimal("1e-9"), ROUND_HALF_EVEN)
    quantum = Decimal(1).scaleb(-digits)
    return float(dec.quantize(quantum, ROUND_HALF_UP))


def quarter_floor(moment: datetime) -> datetime:
    """Beginn der Viertelstunde, in der ``moment`` liegt (UTC).

    Rechnet auf Epochensekunden; für alle Zeitzonen mit Offsets in
    Vielfachen von 15 Minuten (u. a. Europe/Vienna) identisch mit der
    lokalen Viertelstundeneinteilung.
    """
    _require_aware(moment)
    stamp = math.floor(moment.timestamp() / QUARTER_SECONDS) * QUARTER_SECONDS
    return datetime.fromtimestamp(stamp, tz=UTC)


def quarter_nearest(moment: datetime) -> datetime:
    """Nächstgelegene Viertelstundengrenze (für leicht verspätete Timer)."""
    return quarter_floor(moment + QUARTER / 2)


def month_key(moment: datetime, tz: tzinfo) -> str:
    """Kalendermonat ``YYYY-MM`` in lokaler Zeit."""
    _require_aware(moment)
    local = moment.astimezone(tz)
    return f"{local.year:04d}-{local.month:02d}"


def quarter_month_key(quarter_start: datetime, tz: tzinfo) -> str:
    """Monat einer Viertelstunde — Zuordnung über den BEGINN des Intervalls.

    23:45–00:00 am Monatsletzten gehört zum alten Monat, 00:00–00:15 zum neuen.
    """
    return month_key(quarter_start, tz)


def _require_aware(moment: datetime) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("datetime muss zeitzonenbehaftet sein")


# =============================================================================
# Messung: Samples → Grenzen → Viertelstunden
# =============================================================================


@dataclass(slots=True, frozen=True)
class Sample:
    """Gültiger Zählerstand."""

    ts: datetime
    kwh: float
    series: int


@dataclass(slots=True)
class Boundary:
    """Zählerstand an einer Viertelstundengrenze."""

    ts: datetime
    resolved: bool = False
    valid: bool = False
    kwh: float | None = None
    series: int | None = None
    reason: str | None = None


@dataclass(slots=True, frozen=True)
class QuarterResult:
    """Ergebnis einer abgeschlossenen Viertelstunde."""

    start: datetime
    end: datetime
    valid: bool
    kw: float | None = None
    energy_kwh: float | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialisierbare Darstellung."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "valid": self.valid,
            "kw": self.kw,
            "energy_kwh": self.energy_kwh,
            "reason": self.reason,
        }


@dataclass(slots=True, frozen=True)
class CurrentQuarter:
    """Stand der laufenden Viertelstunde (für Prognose und Spielraum)."""

    start: datetime
    end: datetime
    used_kwh: float
    ref_ts: datetime
    estimated_start: bool
    # Unbeobachteter Anfang der Viertelstunde (Start/Ausfall mitten drin): wird
    # in Prognose und Spielraum mit der aktuellen Leistung angenommen, sonst
    # läge die Prognose nach einem Neustart systematisch zu niedrig.
    unseen_h: float = 0.0


@dataclass(slots=True)
class QuarterEngine:
    """Zustandsautomat für die Viertelstundenmessung.

    Parameter:
        max_gap: maximaler Abstand der beiden Samples um eine Grenze. 20 min
            belegt durch Replay echter Daten (Mai/Juni 2026) gegen den
            Netzbetreiber-Lastgang: HA meldete nachts bei 12 kW Ladeleistung
            nur alle 5–18 min; bei 5 min fielen diese Spitzen weg, bei 20 min
            blieb die Abweichung unverändert (Ø 0,008 kW, max 0,25 kW).
        gap_tolerance_kwh: Ausnahme von ``max_gap`` — ist der Zähler zwischen
            den beiden Samples um höchstens diesen Betrag gestiegen, ist der
            interpolierte Stand auf diesen Betrag genau (typisch: Zähler, die
            nur bei Änderung melden, bei ~0 Bezug). Fehler der Viertelstunde
            dann höchstens ``4 × gap_tolerance_kwh`` kW.
        plausibility_kw: Obergrenze für die mittlere Leistung; darüber gilt
            eine Viertelstunde bzw. ein Zählersprung als unplausibel.
        max_pending: wie lange auf das erste Sample nach einer Grenze
            gewartet wird, bevor die Grenze als ungültig gilt.
    """

    max_gap: timedelta = timedelta(minutes=20)
    gap_tolerance_kwh: float = 0.01
    plausibility_kw: float = 60.0
    max_pending: timedelta = timedelta(hours=3)

    _samples: list[Sample] = field(default_factory=list)
    _sample_ts: list[datetime] = field(default_factory=list)
    _series: int = 0
    _series_open: bool = False
    _breaks: list[tuple[datetime, str]] = field(default_factory=list)
    _boundaries: dict[datetime, Boundary] = field(default_factory=dict)
    _emitted: set[datetime] = field(default_factory=set)
    _started: datetime | None = None

    # ------------------------------------------------------------------ input
    def start(self, now: datetime) -> None:
        """Merke den Startzeitpunkt (für die erste, angebrochene Viertelstunde)."""
        _require_aware(now)
        self._started = now

    def add_sample(self, ts: datetime, kwh: float) -> list[QuarterResult]:
        """Neuer numerischer Zählerstand in kWh."""
        _require_aware(ts)
        ts = ts.astimezone(UTC)
        last = self._samples[-1] if self._samples else None
        if last is not None and ts <= last.ts:
            # Doppelt oder außer der Reihe → ignorieren.
            return []

        if self._series_open and last is not None and last.series == self._series:
            delta = kwh - last.kwh
            hours = (ts - last.ts).total_seconds() / 3600
            if delta < 0:
                self._break(ts, REASON_COUNTER_DECREASED)
            elif delta > self.plausibility_kw * max(hours, QUARTER_HOURS):
                # Mehr Energie, als bei Plausibilitätsleistung in einer ganzen
                # Viertelstunde (bzw. in der seither vergangenen Zeit) fließen
                # kann → z. B. 0 → 68 615 kWh nach einem Ausfall.
                self._break(ts, REASON_IMPLAUSIBLE_JUMP)
        elif not self._series_open:
            self._series += 1
            self._series_open = True

        self._samples.append(Sample(ts, kwh, self._series))
        self._sample_ts.append(ts)
        return self._resolve_pending(ts)

    def mark_unavailable(self, ts: datetime) -> list[QuarterResult]:
        """Quelle ist unavailable/unknown/nicht numerisch → Serie beenden."""
        _require_aware(ts)
        if self._series_open:
            self._series_open = False
            self._breaks.append((ts.astimezone(UTC), REASON_SOURCE_UNAVAILABLE))
        return []

    def _break(self, ts: datetime, reason: str) -> None:
        self._series += 1
        self._series_open = True
        self._breaks.append((ts, reason))

    def boundary(self, ts: datetime) -> list[QuarterResult]:
        """Viertelstundengrenze erreicht (Timer)."""
        _require_aware(ts)
        ts = quarter_nearest(ts)
        if ts not in self._boundaries:
            self._boundaries[ts] = Boundary(ts)
        # Aufgelöst wird die Grenze erst, wenn ein Sample danach vorliegt.
        return self._resolve_pending(ts)

    def tick(self, now: datetime) -> list[QuarterResult]:
        """Periodischer Aufruf: abgelaufene Grenzen verwerfen, Speicher kürzen."""
        _require_aware(now)
        results = self._resolve_pending(now)
        self._prune(now)
        return results

    # -------------------------------------------------------------- resolving
    def _resolve_pending(self, now: datetime) -> list[QuarterResult]:
        results: list[QuarterResult] = []
        for ts in sorted(self._boundaries):
            boundary = self._boundaries[ts]
            if boundary.resolved:
                continue
            self._resolve(boundary, now)
            if boundary.resolved:
                results.extend(self._emit_ready(ts))
                results.extend(self._emit_ready(ts + QUARTER))
        results.sort(key=lambda r: r.start)
        return results

    def _resolve(self, boundary: Boundary, now: datetime) -> None:
        t = boundary.ts
        idx_after = bisect_left(self._sample_ts, t)
        after = self._samples[idx_after] if idx_after < len(self._samples) else None
        idx_before = bisect_right(self._sample_ts, t) - 1
        before = self._samples[idx_before] if idx_before >= 0 else None

        if after is None:
            if now - t > self.max_pending:
                self._invalidate(boundary, REASON_NO_SAMPLE_AFTER)
            return
        if before is None:
            self._invalidate(boundary, REASON_NO_SAMPLE_BEFORE)
            return
        if before.series != after.series:
            self._invalidate(boundary, self._break_reason(before.ts, after.ts))
            return
        if before.ts == after.ts:
            self._validate(boundary, before.kwh, before.series)
            return
        gap = after.ts - before.ts
        delta = after.kwh - before.kwh
        if gap > self.max_gap and delta > self.gap_tolerance_kwh:
            self._invalidate(boundary, REASON_SAMPLE_GAP)
            return
        fraction = (t - before.ts) / gap
        self._validate(boundary, before.kwh + delta * fraction, before.series)

    @staticmethod
    def _validate(boundary: Boundary, kwh: float, series: int) -> None:
        boundary.resolved = True
        boundary.valid = True
        boundary.kwh = kwh
        boundary.series = series

    @staticmethod
    def _invalidate(boundary: Boundary, reason: str) -> None:
        boundary.resolved = True
        boundary.valid = False
        boundary.reason = reason

    def _break_reason(self, start: datetime, end: datetime) -> str:
        # inklusiv: ein Ausfall kann denselben Zeitstempel wie das letzte Sample haben
        for ts, reason in self._breaks:
            if start <= ts <= end:
                return reason
        return REASON_SERIES_BREAK

    def _emit_ready(self, end: datetime) -> list[QuarterResult]:
        """Viertelstunde [end-15, end] melden, sobald beide Grenzen feststehen."""
        start = end - QUARTER
        if start in self._emitted:
            return []
        end_b = self._boundaries.get(end)
        if end_b is None or not end_b.resolved:
            return []
        start_b = self._boundaries.get(start)
        if start_b is None:
            # Startgrenze nie beobachtet: nur melden, wenn die Viertelstunde
            # während des Laufs begann/lief (Neustart mitten in der Viertelstunde).
            if self._started is not None and self._started < end:
                return [self._emit(QuarterResult(start, end, False, reason=REASON_NO_START_BOUNDARY))]
            return []
        if not start_b.resolved:
            return []
        return [self._emit(self._evaluate(start_b, end_b))]

    def _emit(self, result: QuarterResult) -> QuarterResult:
        self._emitted.add(result.start)
        return result

    def _evaluate(self, start_b: Boundary, end_b: Boundary) -> QuarterResult:
        start, end = start_b.ts, end_b.ts
        if not start_b.valid:
            return QuarterResult(start, end, False, reason=start_b.reason)
        if not end_b.valid:
            return QuarterResult(start, end, False, reason=end_b.reason)
        if start_b.series != end_b.series:
            return QuarterResult(start, end, False, reason=self._break_reason(start, end))
        assert start_b.kwh is not None and end_b.kwh is not None
        energy = end_b.kwh - start_b.kwh
        if energy < 0:  # pragma: no cover - durch Serienlogik ausgeschlossen
            return QuarterResult(start, end, False, reason=REASON_COUNTER_DECREASED)
        kw = energy / QUARTER_HOURS
        if kw > self.plausibility_kw:
            return QuarterResult(start, end, False, reason=REASON_IMPLAUSIBLE_POWER)
        # Ungerundet speichern — gerundet wird erst bei Anzeige/Verrechnung
        # (sonst Doppelrundung: 12.34499 → 12.345 → 12.35).
        return QuarterResult(start, end, True, kw=kw, energy_kwh=energy)

    def _prune(self, now: datetime) -> None:
        pending = [ts for ts, b in self._boundaries.items() if not b.resolved]
        # Samples seit Beginn der laufenden Viertelstunde (Prognose) und rund um
        # offene Grenzen (Interpolation) werden gebraucht.
        keep_from = min([now - timedelta(minutes=5), quarter_floor(now), *pending])
        # Letztes Sample vor keep_from behalten (wird für Interpolation gebraucht).
        idx = bisect_right(self._sample_ts, keep_from) - 1
        if idx > 0:
            del self._samples[:idx]
            del self._sample_ts[:idx]
        horizon = now - self.max_pending - 2 * QUARTER
        self._boundaries = {
            ts: b for ts, b in self._boundaries.items() if ts >= horizon or not b.resolved
        }
        self._emitted = {ts for ts in self._emitted if ts >= horizon - timedelta(hours=1)}
        self._breaks = [(ts, r) for ts, r in self._breaks if ts >= horizon]

    # ---------------------------------------------------------------- queries
    @property
    def last_sample(self) -> Sample | None:
        """Letztes gültiges Sample (egal welche Serie)."""
        return self._samples[-1] if self._samples else None

    @property
    def series_open(self) -> bool:
        """True, wenn die Quelle zuletzt einen gültigen Wert lieferte."""
        return self._series_open

    @property
    def pending_boundaries(self) -> int:
        """Anzahl noch nicht aufgelöster Grenzen."""
        return sum(1 for b in self._boundaries.values() if not b.resolved)

    def slope_kw(self, window: timedelta = timedelta(minutes=2)) -> float | None:
        """Leistung aus der Steigung der letzten Samples derselben Serie."""
        if not self._series_open or len(self._samples) < 2:
            return None
        last = self._samples[-1]
        first: Sample | None = None
        for sample in reversed(self._samples[:-1]):
            if sample.series != last.series:
                break
            first = sample
            if last.ts - sample.ts >= window:
                break
        if first is None:
            return None
        seconds = (last.ts - first.ts).total_seconds()
        if seconds < 20 or seconds > 600:
            return None
        return max((last.kwh - first.kwh) / (seconds / 3600), 0.0)

    def current_quarter(self, now: datetime) -> CurrentQuarter | None:
        """Verbrauch der laufenden Viertelstunde bis zum letzten Sample.

        Ist die Startgrenze gültig, wird exakt ab dieser gerechnet. Ist sie
        noch offen, dient das letzte Sample vor der Grenze als (etwas zu hohe,
        also konservative) Näherung. Fehlt sie ganz (Start/Ausfall), wird ab
        dem ersten Sample der laufenden Serie in der Viertelstunde gerechnet
        und ``estimated_start`` gesetzt — nur für Prognose/Spielraum, nie für
        die Monatsspitze.
        """
        _require_aware(now)
        start = quarter_floor(now)
        end = start + QUARTER
        last = self.last_sample
        if last is None or not self._series_open:
            return None
        boundary = self._boundaries.get(start)
        if boundary is not None and boundary.resolved and boundary.valid:
            if boundary.series != last.series:
                return self._estimated_current(start, end, last)
            if last.ts < start:
                return CurrentQuarter(start, end, 0.0, start, False)
            assert boundary.kwh is not None
            return CurrentQuarter(start, end, max(last.kwh - boundary.kwh, 0.0), last.ts, False)
        if boundary is not None and not boundary.resolved:
            idx = bisect_right(self._sample_ts, start) - 1
            before = self._samples[idx] if idx >= 0 else None
            if before is not None and before.series == last.series:
                if last.ts <= start:
                    return CurrentQuarter(start, end, 0.0, start, False)
                return CurrentQuarter(start, end, max(last.kwh - before.kwh, 0.0), last.ts, False)
        return self._estimated_current(start, end, last)

    def _estimated_current(self, start: datetime, end: datetime, last: Sample) -> CurrentQuarter | None:
        first = None
        for sample in reversed(self._samples):
            if sample.series != last.series or sample.ts < start:
                break
            first = sample
        if first is None:
            return None
        unseen_h = max((first.ts - start).total_seconds(), 0.0) / 3600
        return CurrentQuarter(start, end, max(last.kwh - first.kwh, 0.0), last.ts, True, unseen_h)


# =============================================================================
# Prognose, Spielraum, Hysterese
# =============================================================================

MIN_REST = timedelta(seconds=30)


def forecast_kw(current: CurrentQuarter, power_kw: float) -> float:
    """Prognose der laufenden Viertelstunde in kW.

    (verbrauchte kWh bis zum letzten Sample + P_jetzt × Zeit bis Fensterende) × 4.
    Die Restzeit zählt ab dem Zeitpunkt des letzten Samples, damit die
    Poll-Latenz der Quelle nicht verloren geht.
    """
    rest_h = max((current.end - current.ref_ts).total_seconds(), 0.0) / 3600
    return (current.used_kwh + power_kw * (rest_h + current.unseen_h)) / QUARTER_HOURS


def headroom_kw(
    current: CurrentQuarter,
    power_kw: float,
    target_kw: float,
    now: datetime,
) -> float:
    """Zusätzliche konstante Last (kW), die bis Viertelstundenende noch dazukommen darf.

    ((Ziel/4 − verbraucht_kWh) / Rest_h) − P_jetzt; negativ = drosseln.
    ``verbraucht`` wird vom letzten Sample bis ``now`` mit P_jetzt
    fortgeschrieben; die Restzeit wird auf mindestens 30 s begrenzt.
    """
    since_ref_h = max((now - current.ref_ts).total_seconds(), 0.0) / 3600
    used_now = current.used_kwh + power_kw * (since_ref_h + current.unseen_h)
    rest = max(current.end - now, MIN_REST)
    rest_h = rest.total_seconds() / 3600
    return (target_kw * QUARTER_HOURS - used_now) / rest_h - power_kw


def headroom_formula(target_kw: float, used_kwh: float, rest_s: float, power_kw: float) -> float:
    """Spielraum-Formel ohne Zeitobjekte (Restzeit in Sekunden, min. 30 s)."""
    rest_h = max(rest_s, MIN_REST.total_seconds()) / 3600
    return (target_kw * QUARTER_HOURS - used_kwh) / rest_h - power_kw


def hysteresis(previous: bool, value: float | None, threshold: float, hyst: float) -> bool | None:
    """Ein bei ``value > threshold``, aus erst bei ``value < threshold - hyst``."""
    if value is None:
        return None
    if previous:
        return value >= threshold - max(hyst, 0.0)
    return value > threshold


# =============================================================================
# Verrechnung
# =============================================================================


def minimum_billed_kw(agreed_kw: float, minimum_kw: float, share: float = 0.2) -> float:
    """Mindestverrechnung: max(20 % der vereinbarten Leistung, Mindestleistung)."""
    return max(agreed_kw * share, minimum_kw)


def billed_kw(peak_kw: float | None, agreed_kw: float, minimum_kw: float) -> float:
    """Verrechnete Leistung = max(Monatsspitze kaufmännisch gerundet, Minimum)."""
    floor = minimum_billed_kw(agreed_kw, minimum_kw)
    if peak_kw is None:
        return round_half_up(floor, 2)
    return round_half_up(max(round_half_up(peak_kw, 2), floor), 2)


def annual_capacity_cost(billed: float, tier_limit_kw: float, price_1: float, price_2: float) -> float:
    """Jahresbetrag: Stufe 1 bis einschließlich Staffelgrenze, Stufe 2 für den Überschuss."""
    tier_1 = min(billed, tier_limit_kw)
    tier_2 = max(billed - tier_limit_kw, 0.0)
    return tier_1 * price_1 + tier_2 * price_2


def monthly_capacity_cost(billed: float, tier_limit_kw: float, price_1: float, price_2: float) -> float:
    """Monatsbetrag (€) = Jahresbetrag / 12, kaufmännisch auf Cent gerundet."""
    return round_half_up(annual_capacity_cost(billed, tier_limit_kw, price_1, price_2) / 12, 2)


# =============================================================================
# Tarifzeitfenster SNAP / WiNAP
# =============================================================================


def _in_snap_season(day: date) -> bool:
    return 4 <= day.month <= 9


def _in_winap_season(day: date) -> bool:
    return day.month >= 10 or day.month <= 3


def tariff_window(moment: datetime, tz: tzinfo) -> str:
    """Tarifzeitfenster zum Zeitpunkt ``moment`` (lokale Zeit ``tz``).

    * SNAP: 1.4.–30.9., 10:00–16:00.
    * WiNAP: Nächte, die an einem Tag zwischen 1.10. und 31.3. um 22:00
      BEGINNEN, bis 04:00 des Folgetags. Die Nacht 31.3.→1.4. ist damit bis
      04:00 WiNAP; die Nacht 30.9.→1.10. ist es nicht (erste WiNAP-Nacht
      beginnt am 1.10. um 22:00).
    * sonst: standard.
    """
    _require_aware(moment)
    local = moment.astimezone(tz)
    day = local.date()
    clock = local.time()
    if _in_snap_season(day) and SNAP_START <= clock < SNAP_END:
        return TARIFF_SNAP
    if clock >= WINAP_START and _in_winap_season(day):
        return TARIFF_WINAP
    if clock < WINAP_END and _in_winap_season(day - timedelta(days=1)):
        return TARIFF_WINAP
    return TARIFF_STANDARD


def tariff_window_end(moment: datetime, tz: tzinfo) -> datetime:
    """Zeitpunkt (lokal, tz-behaftet), an dem das aktuelle Fenster endet."""
    current = tariff_window(moment, tz)
    local = moment.astimezone(tz)
    for day_offset in range(4):
        day = local.date() + timedelta(days=day_offset)
        for clock in _TARIFF_CHANGE_TIMES:
            candidate = datetime.combine(day, clock, tzinfo=tz)
            if candidate <= local:
                continue
            if tariff_window(candidate, tz) != current:
                return candidate
    raise RuntimeError("kein Fensterwechsel innerhalb von 4 Tagen gefunden")  # pragma: no cover


def energy_price(window: str, standard: float, snap: float, winap: float) -> float | None:
    """Arbeitspreis (ct/kWh) des Fensters; ``None``, wenn nicht gesetzt (0)."""
    price = {TARIFF_STANDARD: standard, TARIFF_SNAP: snap, TARIFF_WINAP: winap}[window]
    return price if price and price > 0 else None


# =============================================================================
# Lastprofil: 96 Viertelstunden des lokalen Tages
# =============================================================================

SLOTS_PER_DAY = 96


def local_slot(start: datetime, tz: tzinfo) -> tuple[date, int]:
    """(lokales Datum, Viertelstunde des Tages 0–95) einer Viertelstunde.

    Zuordnung über die lokale Wanduhrzeit des Beginns: An 25-Stunden-Tagen
    (Umstellung auf Winterzeit) fallen 02:00–02:45 zweimal in denselben Slot,
    an 23-Stunden-Tagen bleiben 02:00–02:45 leer.
    """
    _require_aware(start)
    local = start.astimezone(tz)
    return local.date(), local.hour * 4 + local.minute // 15


def slot_labels() -> list[str]:
    """96 Beschriftungen ``HH:MM`` (Beginn der Viertelstunde)."""
    return [f"{slot // 4:02d}:{slot % 4 * 15:02d}" for slot in range(SLOTS_PER_DAY)]


def round_list(values: list[float | None], digits: int = 3) -> list[float | None]:
    """Liste runden (für Attribute); ``None`` bleibt ``None``."""
    return [None if v is None else round(v, digits) for v in values]


def _float_list(data: Any, length: int = SLOTS_PER_DAY) -> list[float | None] | None:
    if not isinstance(data, list) or len(data) != length:
        return None
    return [_to_float(v) for v in data]


@dataclass(slots=True)
class SlotProfile:
    """Maximum, Summe und Anzahl je Viertelstunde des Tages (Monatsprofil).

    Summe und Anzahl statt Mittelwert, damit doppelte Slots (Zeitumstellung)
    und spätere Viertelstunden korrekt in den Mittelwert eingehen.
    """

    max: list[float | None] = field(default_factory=lambda: [None] * SLOTS_PER_DAY)
    sum: list[float] = field(default_factory=lambda: [0.0] * SLOTS_PER_DAY)
    count: list[int] = field(default_factory=lambda: [0] * SLOTS_PER_DAY)

    def add(self, slot: int, kw: float) -> None:
        """Eine gültige Viertelstunde verbuchen."""
        current = self.max[slot]
        self.max[slot] = kw if current is None else max(current, kw)
        self.sum[slot] += kw
        self.count[slot] += 1

    @property
    def total(self) -> int:
        """Anzahl verbuchter Viertelstunden."""
        return sum(self.count)

    def avg(self) -> list[float | None]:
        """Mittelwert je Slot (``None`` ohne Werte)."""
        return [s / n if n else None for s, n in zip(self.sum, self.count, strict=True)]

    def merged(self, other: SlotProfile) -> SlotProfile:
        """Kombination zweier Profile: Maximum je Slot, gemeinsamer Mittelwert."""
        return SlotProfile(
            max=[_max_or_none(a, b) for a, b in zip(self.max, other.max, strict=True)],
            sum=[a + b for a, b in zip(self.sum, other.sum, strict=True)],
            count=[a + b for a, b in zip(self.count, other.count, strict=True)],
        )

    def as_dict(self) -> dict[str, Any]:
        """Für den Store: ``profile_max``, ``profile_avg``, ``profile_count``."""
        return {
            "profile_max": round_list(self.max, 4),
            "profile_avg": round_list(self.avg(), 4),
            "profile_count": list(self.count),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SlotProfile:
        """Aus dem Store laden; unvollständige Daten ergeben ein leeres Profil."""
        maxima = _float_list(data.get("profile_max"))
        averages = _float_list(data.get("profile_avg"))
        counts = data.get("profile_count")
        if (
            maxima is None
            or averages is None
            or not isinstance(counts, list)
            or len(counts) != SLOTS_PER_DAY
        ):
            return cls()
        count = [int(c) if isinstance(c, (int, float)) and c > 0 else 0 for c in counts]
        return cls(
            max=[m if n else None for m, n in zip(maxima, count, strict=True)],
            sum=[(a or 0.0) * n for a, n in zip(averages, count, strict=True)],
            count=count,
        )


def _max_or_none(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


@dataclass(slots=True)
class DayProfiles:
    """Viertelstundenwerte (kW) von heute und gestern, je lokalem Datum."""

    tz: tzinfo
    days: dict[str, list[float | None]] = field(default_factory=dict)

    def add(self, result: QuarterResult) -> None:
        """Gültige Viertelstunde eintragen (doppelte Slots: Maximum)."""
        if not result.valid or result.kw is None:
            return
        day, slot = local_slot(result.start, self.tz)
        self._set(day, slot, result.kw, only_empty=False)

    def fill(self, quarters: dict[datetime, float], today: date) -> int:
        """Import: leere Slots von heute und gestern füllen; Rückgabe = gefüllte Slots.

        Slots mit eigenem Messwert bleiben unverändert. Fallen zwei importierte
        Viertelstunden in denselben Slot (Umstellung auf Winterzeit), gilt wie
        bei :meth:`add` das Maximum.
        """
        low = datetime.combine(today - timedelta(days=1), time(), self.tz).astimezone(UTC)
        high = datetime.combine(today + timedelta(days=1), time(), self.tz).astimezone(UTC)
        best: dict[tuple[date, int], float] = {}
        for start, kw in quarters.items():
            if low <= start < high:
                key = local_slot(start, self.tz)
                best[key] = max(best.get(key, kw), kw)
        return sum(self._set(day, slot, kw, only_empty=True) for (day, slot), kw in best.items())

    def _set(self, day: date, slot: int, kw: float, *, only_empty: bool) -> bool:
        values = self.days.setdefault(day.isoformat(), [None] * SLOTS_PER_DAY)
        current = values[slot]
        if current is not None and only_empty:
            return False
        values[slot] = kw if current is None else max(current, kw)
        return True

    def get(self, day: date) -> list[float | None]:
        """96 Werte eines Tages (``None`` = fehlend/ungültig)."""
        return list(self.days.get(day.isoformat(), [None] * SLOTS_PER_DAY))

    def prune(self, today: date) -> None:
        """Nur heute und gestern behalten."""
        keep = {today.isoformat(), (today - timedelta(days=1)).isoformat()}
        for key in [k for k in self.days if k not in keep]:
            del self.days[key]

    def as_dict(self) -> dict[str, Any]:
        """Für den Store."""
        return {key: round_list(values, 4) for key, values in self.days.items()}

    @classmethod
    def from_dict(cls, tz: tzinfo, data: Any) -> DayProfiles:
        """Aus dem Store laden (tolerant)."""
        profiles = cls(tz=tz)
        if isinstance(data, dict):
            for key, values in data.items():
                parsed = _float_list(values)
                if parsed is not None and _valid_date_key(key):
                    profiles.days[key] = parsed
        return profiles


def _valid_date_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    try:
        date.fromisoformat(key)
    except ValueError:
        return False
    return True


# =============================================================================
# Monatsspitze und Verlauf
# =============================================================================

HISTORY_MONTHS = 36

SOURCE_MEASURED = "measured"
SOURCE_IMPORTED = "imported"
SOURCE_MIXED = "mixed"


@dataclass(slots=True)
class ImportedMonth:
    """Aus einem Lastgang-Import stammende Kennzahlen eines Monats."""

    peak_kw: float | None = None
    peak_start: str | None = None
    quarters: int = 0
    profile: SlotProfile = field(default_factory=SlotProfile)

    def add(self, start: datetime, kw: float, tz: tzinfo) -> None:
        """Importierte Viertelstunde verbuchen (bei Gleichstand gilt die frühere)."""
        self.quarters += 1
        if self.peak_kw is None or kw > self.peak_kw:
            self.peak_kw = kw
            self.peak_start = start.isoformat()
        self.profile.add(local_slot(start, tz)[1], kw)

    def as_dict(self) -> dict[str, Any]:
        """Für den Store."""
        return {
            "peak_kw": self.peak_kw,
            "peak_start": self.peak_start,
            "quarters": self.quarters,
            **self.profile.as_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> ImportedMonth | None:
        """Aus dem Store laden (tolerant)."""
        if not isinstance(data, dict):
            return None
        peak = _to_float(data.get("peak_kw"))
        return cls(
            peak_kw=peak,
            peak_start=data.get("peak_start") if peak is not None else None,
            quarters=int(data.get("quarters", 0) or 0),
            profile=SlotProfile.from_dict(data),
        )


@dataclass(slots=True)
class MonthStats:
    """Kennzahlen eines Kalendermonats.

    ``peak_kw``/``peak_start``, die Zähler und ``profile`` stammen aus eigener
    Messung; ``imported`` getrennt davon aus einem Lastgang-Import. Angezeigt
    wird die Kombination (``combined_peak``/``combined_profile``). Die Trennung
    macht einen wiederholten Import idempotent.

    ``measured_first``/``measured_last``: Beginn der ersten/letzten eigenen
    Viertelstunde (gültig oder nicht) — damit ``overwrite`` nur Messungen
    ersetzt, die ganz im importierten Zeitraum liegen. ``None`` bei Daten aus
    Versionen ohne dieses Feld.
    """

    peak_kw: float | None = None
    peak_start: str | None = None
    valid_quarters: int = 0
    invalid_quarters: int = 0
    profile: SlotProfile = field(default_factory=SlotProfile)
    imported: ImportedMonth | None = None
    measured_first: datetime | None = None
    measured_last: datetime | None = None

    def note_measured(self, start: datetime) -> None:
        """Zeitraum der eigenen Messung erweitern."""
        if self.measured_first is None or start < self.measured_first:
            self.measured_first = start
        if self.measured_last is None or start > self.measured_last:
            self.measured_last = start

    @property
    def has_measurement(self) -> bool:
        """True, wenn eigene Viertelstunden (gültig oder nicht) verbucht sind."""
        return self.peak_kw is not None or self.valid_quarters > 0 or self.invalid_quarters > 0

    @property
    def source(self) -> str:
        """``measured``, ``imported`` oder ``mixed``."""
        if self.imported is None:
            return SOURCE_MEASURED
        return SOURCE_MIXED if self.has_measurement else SOURCE_IMPORTED

    def combined_peak(self) -> tuple[float | None, str | None]:
        """Höchste Viertelstunde aus Messung und Import (kW, ISO-Beginn)."""
        peak, start = self.peak_kw, self.peak_start
        imp = self.imported
        if imp is not None and imp.peak_kw is not None and (peak is None or imp.peak_kw > peak):
            return imp.peak_kw, imp.peak_start
        return peak, start

    def combined_profile(self) -> SlotProfile:
        """Monatsprofil aus Messung und Import.

        Überlappen Messung und Import zeitlich, gehen die gemeinsamen
        Viertelstunden doppelt in den Mittelwert ein (Näherung).
        """
        if self.imported is None:
            return self.profile
        return self.profile.merged(self.imported.profile)

    def reset_measurement(self) -> None:
        """Eigene Messwerte verwerfen (Import mit ``overwrite``)."""
        self.peak_kw = None
        self.peak_start = None
        self.valid_quarters = 0
        self.invalid_quarters = 0
        self.profile = SlotProfile()
        self.measured_first = None
        self.measured_last = None

    def summary(self) -> dict[str, Any]:
        """Kennzahlen für das ``history``-Attribut (ohne Profile)."""
        peak, start = self.combined_peak()
        return {
            "peak_kw": peak,
            "peak_start": start,
            "valid_quarters": self.valid_quarters,
            "invalid_quarters": self.invalid_quarters,
            "imported_quarters": self.imported.quarters if self.imported else 0,
            "source": self.source,
        }

    def as_dict(self) -> dict[str, Any]:
        """Serialisierbare Darstellung (Store)."""
        data: dict[str, Any] = {
            "peak_kw": self.peak_kw,
            "peak_start": self.peak_start,
            "valid_quarters": self.valid_quarters,
            "invalid_quarters": self.invalid_quarters,
            **self.profile.as_dict(),
        }
        if self.measured_first is not None and self.measured_last is not None:
            data["measured_first"] = self.measured_first.isoformat()
            data["measured_last"] = self.measured_last.isoformat()
        if self.imported is not None:
            data["imported"] = self.imported.as_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MonthStats:
        """Aus gespeicherten Daten laden (tolerant, auch Daten aus v0.1)."""
        peak = _to_float(data.get("peak_kw"))
        first = parse_aware(data.get("measured_first"))
        last = parse_aware(data.get("measured_last"))
        if first is None or last is None:
            first = last = None
        return cls(
            peak_kw=peak,
            peak_start=data.get("peak_start") if peak is not None else None,
            valid_quarters=int(data.get("valid_quarters", 0) or 0),
            invalid_quarters=int(data.get("invalid_quarters", 0) or 0),
            profile=SlotProfile.from_dict(data),
            imported=ImportedMonth.from_dict(data.get("imported")),
            measured_first=first,
            measured_last=last,
        )


def parse_aware(value: Any) -> datetime | None:
    """ISO-Zeitpunkt mit Zeitzone lesen (tolerant: sonst ``None``)."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


@dataclass(slots=True)
class PeakTracker:
    """Monatsspitze je Kalendermonat (lokale Zeit) mit Verlauf."""

    tz: tzinfo
    months: dict[str, MonthStats] = field(default_factory=dict)
    current: str | None = None

    def add(self, result: QuarterResult) -> bool:
        """Viertelstunde verbuchen. Rückgabe: True, wenn die Spitze stieg."""
        key = quarter_month_key(result.start, self.tz)
        self.roll_to(key)
        too_old = len(self.months) >= HISTORY_MONTHS and key < min(self.months)
        if key not in self.months and too_old:
            return False  # älter als der Verlauf → verwerfen
        stats = self.months.setdefault(key, MonthStats())
        stats.note_measured(result.start)
        if not result.valid or result.kw is None:
            stats.invalid_quarters += 1
            return False
        stats.valid_quarters += 1
        stats.profile.add(local_slot(result.start, self.tz)[1], result.kw)
        if stats.peak_kw is None or result.kw > stats.peak_kw:
            stats.peak_kw = result.kw
            stats.peak_start = result.start.isoformat()
            return True
        return False

    def roll_to(self, key: str) -> None:
        """Auf einen (neueren) Monat weiterschalten. Ältere Keys ändern nichts."""
        if self.current is None or key > self.current:
            self.current = key
            self.months.setdefault(key, MonthStats())
        self._trim()

    def _trim(self) -> None:
        for key in sorted(self.months)[:-HISTORY_MONTHS]:
            del self.months[key]

    @property
    def current_stats(self) -> MonthStats:
        """Kennzahlen des laufenden Monats."""
        if self.current is None:
            return MonthStats()
        return self.months.setdefault(self.current, MonthStats())

    def history(self) -> dict[str, dict[str, Any]]:
        """Verlauf (neuester Monat zuerst): Monat → {kw, Zeitpunkt, Quelle, …}."""
        return {key: self.months[key].summary() for key in sorted(self.months, reverse=True)}

    def as_dict(self) -> dict[str, Any]:
        """Serialisierbare Darstellung für den Store."""
        return {"current": self.current, "months": {k: v.as_dict() for k, v in self.months.items()}}

    @classmethod
    def from_dict(cls, tz: tzinfo, data: dict[str, Any] | None) -> PeakTracker:
        """Aus dem Store laden."""
        tracker = cls(tz=tz)
        if not data:
            return tracker
        for key, value in (data.get("months") or {}).items():
            if isinstance(value, dict) and _valid_month_key(key):
                tracker.months[key] = MonthStats.from_dict(value)
        current = data.get("current")
        tracker.current = current if isinstance(current, str) and _valid_month_key(current) else None
        tracker._trim()
        return tracker


def _valid_month_key(key: Any) -> bool:
    if not isinstance(key, str) or len(key) != 7 or key[4] != "-":
        return False
    try:
        return 1 <= int(key[5:]) <= 12 and int(key[:4]) > 0
    except ValueError:
        return False


def month_add(key: str, delta: int) -> str:
    """Monatsschlüssel ``YYYY-MM`` um ``delta`` Monate verschieben."""
    index = int(key[:4]) * 12 + int(key[5:]) - 1 + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_bounds(key: str, tz: tzinfo) -> tuple[datetime, datetime]:
    """Beginn und Ende (exklusiv, UTC) eines Kalendermonats in Ortszeit ``tz``."""
    nxt = month_add(key, 1)
    start = datetime(int(key[:4]), int(key[5:]), 1, tzinfo=tz)
    end = datetime(int(nxt[:4]), int(nxt[5:]), 1, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def history_window(newest: str) -> tuple[str, str]:
    """(ältester, neuester) Monatsschlüssel des Verlaufs."""
    return month_add(newest, -(HISTORY_MONTHS - 1)), newest


@dataclass(slots=True)
class MergeReport:
    """Ergebnis von :func:`merge_import` (Monatsschlüssel je Kategorie).

    ``overwrite_skipped``: ``overwrite`` war verlangt, die eigene Messung
    reicht aber über den importierten Zeitraum hinaus → zusammengeführt
    (steht zusätzlich in ``merged``).
    """

    imported: list[str] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    overwritten: list[str] = field(default_factory=list)
    overwrite_skipped: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def merge_import(
    tracker: PeakTracker,
    months: dict[str, ImportedMonth],
    current: str,
    *,
    overwrite: bool = False,
    coverage: dict[str, tuple[datetime, datetime]] | None = None,
) -> MergeReport:
    """Importierte Monate in den Verlauf übernehmen.

    ``months`` enthält je Monat den **vollständigen** Import-Teil (bei
    :func:`plan_import` schon mit früheren Importen vereinigt) und ersetzt den
    bisherigen Import-Teil — ein erneuter Import derselben Datei ändert also
    nichts.

    * Monat ohne eigene Messung → Import übernehmen (``source: imported``).
    * Monat mit eigener Messung → zusammenführen: Spitze = Maximum aus beidem
      (``source: mixed``). Mit ``overwrite`` wird die eigene Messung nur
      verworfen, wenn sie ganz im Zeitraum ``coverage[key]`` dieses Imports
      liegt — sonst gingen Messwerte außerhalb verloren (``overwrite_skipped``).
      Messungen ohne gespeicherten Zeitraum (ältere Versionen) nur, wenn der
      Import den ganzen Kalendermonat abdeckt.
    * Übersprungen: Monate ohne Werte, nach dem laufenden Monat oder älter
      als der Verlauf (``HISTORY_MONTHS``).
    """
    report = MergeReport()
    tracker.roll_to(current)
    oldest, newest = history_window(tracker.current or current)
    for key in sorted(months):
        imported = months[key]
        if imported.quarters == 0 or not _valid_month_key(key) or key < oldest or key > newest:
            report.skipped.append(key)
            continue
        stats = tracker.months.setdefault(key, MonthStats())
        if stats.has_measurement:
            span = coverage.get(key) if coverage else None
            if overwrite and span is not None and _measurement_within(stats, key, span, tracker.tz):
                stats.reset_measurement()
                report.overwritten.append(key)
            else:
                report.merged.append(key)
                if overwrite:
                    report.overwrite_skipped.append(key)
        else:
            report.imported.append(key)
        stats.imported = imported
    tracker.roll_to(newest)
    return report


def _measurement_within(stats: MonthStats, key: str, span: tuple[datetime, datetime], tz: tzinfo) -> bool:
    """True, wenn alle eigenen Viertelstunden des Monats im Zeitraum ``span`` liegen."""
    begin, end = span
    if stats.measured_first is None or stats.measured_last is None:
        month_start, month_end = month_bounds(key, tz)
        return begin <= month_start and end >= month_end
    return begin <= stats.measured_first and stats.measured_last + QUARTER <= end


# --- Rohwerte früherer Importe (eigener Store, nur beim Import geschrieben) ----

IMPORT_DIGITS = 4


@dataclass(slots=True)
class ImportPlan:
    """Ergebnis von :func:`plan_import` — im Executor berechnet.

    * ``months``: vollständiger Import-Teil je Monat dieser Datei (vereinigt
      mit früher importierten Viertelstunden, neuere Werte gewinnen),
    * ``coverage``: Zeitraum dieser Datei je Monat ``[erste, letzte + 15 min)``,
    * ``extended``: Monate, in denen früher importierte Viertelstunden
      außerhalb dieser Datei erhalten blieben,
    * ``replaced_quarters``: früher importierte Viertelstunden, die diese
      Datei mit neuen Werten ersetzt hat,
    * ``file_months``: alle Monate der Datei (auch übersprungene),
    * ``store``: alle Rohwerte nach dem Import, fertig für den Import-Store.
    """

    months: dict[str, ImportedMonth]
    coverage: dict[str, tuple[datetime, datetime]]
    extended: list[str]
    replaced_quarters: int
    file_months: list[str]
    store: dict[str, Any]


def plan_import(
    stored: Any,
    quarters: dict[datetime, float],
    tz: tzinfo,
    newest: str,
    legacy_peaks: dict[str, tuple[datetime, float]] | None = None,
) -> ImportPlan:
    """Neue Viertelstunden je Monat mit früher importierten vereinigen.

    ``stored`` ist der Inhalt des Import-Stores (tolerant gelesen).
    ``legacy_peaks``: Monatsspitzen früherer Importe, für die keine Rohwerte
    gespeichert sind (Import mit einer Vorversion) — sie bleiben als einzelne
    Viertelstunde erhalten, sofern diese Datei sie nicht mit einem neuen Wert
    ersetzt. Rein rechnend, ohne Seiteneffekte (Executor-tauglich).
    """
    oldest, newest = history_window(newest)
    previous_all = imported_quarters_from_dict(stored)
    kept = {key: values for key, values in previous_all.items() if oldest <= key <= newest}
    by_month: dict[str, dict[datetime, float]] = {}
    for start, kw in quarters.items():
        by_month.setdefault(quarter_month_key(start, tz), {})[start] = round(kw, IMPORT_DIGITS)

    months: dict[str, ImportedMonth] = {}
    coverage: dict[str, tuple[datetime, datetime]] = {}
    extended: list[str] = []
    replaced = 0
    for key in sorted(by_month):
        new = by_month[key]
        if not _valid_month_key(key) or key < oldest or key > newest:
            continue  # merge_import meldet den Monat als übersprungen
        previous = kept.get(key)
        if previous is None and legacy_peaks and key in legacy_peaks:
            start, kw = legacy_peaks[key]
            previous = {start: round(kw, IMPORT_DIGITS)}
        previous = previous or {}
        replaced += sum(1 for start in previous if start in new)
        if any(start not in new for start in previous):
            extended.append(key)
        union = {**previous, **new}
        kept[key] = union
        months[key] = _summarize(union, tz)
        coverage[key] = (min(new), max(new) + QUARTER)
    # Monate außerhalb des Fensters: nur zur Anzeige als übersprungen weiterreichen
    for key in by_month:
        if key not in months:
            months[key] = ImportedMonth()
    return ImportPlan(
        months=months,
        coverage=coverage,
        extended=extended,
        replaced_quarters=replaced,
        file_months=sorted(by_month),
        store=imported_quarters_as_dict(kept),
    )


def imported_quarters_as_dict(months: dict[str, dict[datetime, float]]) -> dict[str, Any]:
    """Rohwerte je Monat kompakt: Beginn der ersten Viertelstunde + Werteliste (``None`` = Lücke)."""
    data: dict[str, Any] = {}
    for key in sorted(months):
        values = months[key]
        if not values:
            continue
        first = min(values)
        base = first.timestamp()
        count = int((max(values).timestamp() - base) // QUARTER_SECONDS) + 1
        series: list[float | None] = [None] * count
        for start, kw in values.items():
            series[int((start.timestamp() - base) // QUARTER_SECONDS)] = round(kw, IMPORT_DIGITS)
        data[key] = {"first": first.astimezone(UTC).isoformat(), "kw": series}
    return data


def imported_quarters_from_dict(data: Any) -> dict[str, dict[datetime, float]]:
    """Gegenstück zu :func:`imported_quarters_as_dict` (tolerant: Unlesbares wird ignoriert)."""
    months: dict[str, dict[datetime, float]] = {}
    if not isinstance(data, dict):
        return months
    for key, entry in data.items():
        if not _valid_month_key(key) or not isinstance(entry, dict):
            continue
        first = parse_aware(entry.get("first"))
        series = entry.get("kw")
        if first is None or not isinstance(series, list) or first.timestamp() % QUARTER_SECONDS:
            continue
        first = first.astimezone(UTC)
        values = {
            first + QUARTER * index: kw
            for index, raw in enumerate(series)
            if (kw := _to_float(raw)) is not None and kw >= 0
        }
        if values:
            months[key] = values
    return months


# =============================================================================
# Import eines Lastgangs (z. B. Portal-Export des Netzbetreibers)
# =============================================================================

VALUE_KW = "kW"
VALUE_KWH = "kWh"
COLUMN_FROM_HEADER = "header"
COLUMN_DETECTED = "detected"
COLUMN_ASSUMED = "assumed"

_DE_TIMESTAMP = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_KW_HEADER = re.compile(r"(?<![a-z])kw(?![a-z])")
_KWH_HEADER = re.compile(r"(?<![a-z])kwh(?![a-z])")
_DE_DATE = re.compile(r"\d{1,2}\.\d{1,2}\.\d{4}")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME_ONLY = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")
_INTEGER = re.compile(r"[+-]?\d+")
_DETECT_ROWS = 200


class LoadProfileError(ValueError):
    """Die Datei enthält keinen verwertbaren Lastgang (``reason`` = Übersetzungsschlüssel)."""

    def __init__(self, reason: str) -> None:
        """Mit Grund initialisieren."""
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class LoadProfile:
    """Gelesener Lastgang: Beginn der Viertelstunde (UTC) → mittlere Leistung (kW)."""

    quarters: dict[datetime, float]
    rows: int
    rows_skipped: int
    duplicates: int
    value_column: str
    value_column_source: str

    @property
    def first(self) -> datetime | None:
        """Beginn der ersten Viertelstunde."""
        return min(self.quarters) if self.quarters else None

    @property
    def last(self) -> datetime | None:
        """Beginn der letzten Viertelstunde."""
        return max(self.quarters) if self.quarters else None


def parse_number(text: str) -> float | None:
    """Zahl mit Dezimalpunkt oder -komma (``0,354``, ``1.234,5``, ``1,234.5``)."""
    s = text.strip().replace("\u00a0", "").replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        if s.count(",") > 1:
            return None
        s = s.replace(",", ".")
    if not _NUMBER.fullmatch(s):
        return None
    return float(s)


def parse_timestamp(text: str) -> datetime | None:
    """``TT.MM.JJJJ HH:MM[:SS]`` (naiv, Ortszeit) oder ISO 8601 (mit/ohne Zeitzone).

    ``24:00`` wird als 00:00 des Folgetags gelesen (bei Zeitstempel = Ende
    üblich). Reine Datumsangaben ohne Uhrzeit werden abgelehnt.
    """
    s = text.strip()
    match = _DE_TIMESTAMP.match(s)
    if match:
        day, month, year, hour, minute, second = (int(g) if g else 0 for g in match.groups())
        extra = timedelta()
        if hour == 24 and minute == 0 and second == 0:
            hour, extra = 0, timedelta(days=1)
        try:
            return datetime(year, month, day, hour, minute, second) + extra
        except ValueError:
            return None
    if len(s) < 16 or s[4] != "-" or ":" not in s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _localize(naive: datetime, tz: tzinfo, seen: set[datetime]) -> datetime | None:
    """Naive Ortszeit → zeitzonenbehaftet; ``None`` für nicht existente Zeiten.

    Mehrdeutige Zeiten (Umstellung auf Winterzeit, 02:00–02:59 zweimal)
    gelten beim ersten Auftreten als Sommerzeit, beim zweiten als Winterzeit.
    """
    first = naive.replace(tzinfo=tz, fold=0)
    if first.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != naive:
        return None  # Sprung auf Sommerzeit: Uhrzeit gibt es nicht
    second = naive.replace(tzinfo=tz, fold=1)
    repeated = naive in seen
    seen.add(naive)
    if repeated and first.utcoffset() != second.utcoffset():
        return second
    return first


def _detect_delimiter(lines: list[str]) -> str:
    sample = lines[:20]
    if any(";" in line for line in sample):
        return ";"
    if any("\t" in line for line in sample):
        return "\t"
    return ","


def _find_timestamp(cells: list[str]) -> tuple[int, datetime] | None:
    """(Index der Zeitspalte, Zeitstempel) einer Zeile.

    Auch getrennte Spalten Datum + Uhrzeit (``01.08.2026;00:00;00:15;…``,
    ``Datum;Zeit von;Zeit bis;kWh``): die erste Uhrzeit nach dem Datum gilt.
    Zurückgegeben wird dann der Index der Uhrzeit-Spalte.
    """
    for index, cell in enumerate(cells):
        if (ts := parse_timestamp(cell)) is not None:
            return index, ts
        if index + 1 < len(cells) and (ts := _date_and_time(cell, cells[index + 1])) is not None:
            return index + 1, ts
    return None


def _date_and_time(date_text: str, time_text: str) -> datetime | None:
    """Datum (``TT.MM.JJJJ`` oder ``JJJJ-MM-TT``) + Uhrzeit (``HH:MM[:SS]``, auch ``24:00``)."""
    if not _TIME_ONLY.fullmatch(time_text):
        return None
    if _DE_DATE.fullmatch(date_text):
        return parse_timestamp(f"{date_text} {time_text}")
    if _ISO_DATE.fullmatch(date_text):
        try:
            day = date.fromisoformat(date_text)
        except ValueError:
            return None
        return parse_timestamp(f"{day:%d.%m.%Y} {time_text}")
    return None


def _header_columns(cells: list[str]) -> tuple[int | None, int | None]:
    """(Index kW-Spalte, Index kWh-Spalte) einer Kopfzeile."""
    kw_col = kwh_col = None
    for index, cell in enumerate(cells):
        name = cell.lower()
        if kwh_col is None and _KWH_HEADER.search(name):
            kwh_col = index
        elif kw_col is None and _KW_HEADER.search(name):
            kw_col = index
    return kw_col, kwh_col


def _numeric_columns(cells: list[str], after: int) -> list[tuple[int, float]]:
    return [
        (index, value)
        for index, cell in enumerate(cells)
        if index > after and (value := parse_number(cell)) is not None
    ]


def _detect_value_column(data_rows: list[tuple[int, list[str]]]) -> tuple[int, str, str]:
    """Ohne Kopfzeile: (Spalte, Einheit, Herkunft) der Wertspalte bestimmen.

    Zwei Zahlenspalten, bei denen die zweite das Vierfache der ersten ist,
    werden als kWh/kW erkannt (kW wird genommen). Sonst gilt die erste
    Zahlenspalte als Energie der Viertelstunde in kWh (Annahme).
    """
    # erste Zeile, die überhaupt eine Zahl nach der Zeitspalte hat (die erste
    # Datenzeile kann leer sein, z. B. fehlender Wert)
    first: list[tuple[int, float]] = next(
        (cols for ts_col, cells in data_rows if (cols := _numeric_columns(cells, ts_col))), []
    )
    if not first:
        raise LoadProfileError("no_value_column")
    if len(first) >= 2:
        col_a, col_b = first[0][0], first[1][0]
        checked = nonzero = 0
        for _ts_col, cells in data_rows[:_DETECT_ROWS]:
            if len(cells) <= col_b:
                continue
            a, b = parse_number(cells[col_a]), parse_number(cells[col_b])
            if a is None or b is None:
                continue
            checked += 1
            nonzero += a > 0
            if abs(b - 4 * a) > 0.01 + 0.01 * abs(b):
                break
        else:
            if checked and nonzero:
                return col_b, VALUE_KW, COLUMN_DETECTED
    return first[0][0], VALUE_KWH, COLUMN_ASSUMED


def parse_load_profile(text: str, tz: tzinfo, *, timestamp_is_end: bool = False) -> LoadProfile:
    """Lastgang-CSV lesen.

    * Trennzeichen ``;``, Tabulator oder ``,``; BOM und Anführungszeichen
      werden entfernt; Kopfzeile optional.
    * Zeitstempel ``TT.MM.JJJJ HH:MM`` (Ortszeit ``tz``) oder ISO 8601.
      Standard: Beginn der Viertelstunde; mit ``timestamp_is_end`` das Ende.
    * Wertspalte: Spalte „kW“ (Leistung) vor „kWh“ (Energie × 4). Ohne
      Kopfzeile siehe :func:`_detect_value_column`. Weitere Spalten (Status)
      werden ignoriert.
    * Zeitstempel in einer Spalte oder getrennt als Datum + Uhrzeit (erste
      Uhrzeit nach dem Datum, z. B. „Zeit von“).
    * Absteigend sortierte Dateien (neueste Zeile zuerst) werden umgedreht.
    * ``,`` als Trenn- und Dezimalzeichen zugleich (unquotiert) wird mit
      ``decimal_comma_ambiguous`` abgelehnt statt still falsch gelesen.
    * Übersprungen (``rows_skipped``): Zeilen ohne Zeitstempel oder Wert,
      negative Werte, nicht existente Ortszeiten, nicht viertelstündliche
      Zeitstempel. Mehrfach vorkommende Viertelstunden: der in Zeitfolge
      letzte Wert gilt.
    """
    text = text.lstrip("\ufeff")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise LoadProfileError("no_data")
    delimiter = _detect_delimiter(lines)
    rows = [
        [cell.strip().strip('"').strip() for cell in row] for row in csv.reader(lines, delimiter=delimiter)
    ]

    header_cols: tuple[int | None, int | None] = (None, None)
    header: list[str] | None = None
    data_rows: list[tuple[int, list[str], datetime]] = []
    skipped = 0  # Zeilen ohne Zeitstempel nach Kopfzeile bzw. erster Datenzeile
    for cells in rows:
        found = _find_timestamp(cells)
        if found is not None:
            data_rows.append((found[0], cells, found[1]))
            continue
        if not data_rows:
            cols = _header_columns(cells)
            if cols != (None, None):
                # Kopfzeile; Zeilen davor (z. B. Zählpunkt-Metadaten) zählen nicht.
                header_cols, header, skipped = cols, cells, 0
                continue
        skipped += 1
    if not data_rows:
        raise LoadProfileError("no_data")
    if delimiter == ",":
        _check_comma_decimals(header, data_rows)
    if _descending([ts for _col, _cells, ts in data_rows if ts.tzinfo is None]):
        # Neueste Zeile zuerst: umdrehen, damit die doppelte Stunde im Oktober
        # beim ersten Auftreten als Sommerzeit gelesen wird (siehe _localize).
        data_rows.reverse()

    kw_col, kwh_col = header_cols
    if kw_col is not None:
        value_col, unit, source = kw_col, VALUE_KW, COLUMN_FROM_HEADER
    elif kwh_col is not None:
        value_col, unit, source = kwh_col, VALUE_KWH, COLUMN_FROM_HEADER
    else:
        value_col, unit, source = _detect_value_column([(ts_col, cells) for ts_col, cells, _ in data_rows])
    factor = 1.0 if unit == VALUE_KW else 1 / QUARTER_HOURS

    quarters: dict[datetime, float] = {}
    duplicates = accepted = 0
    seen: set[datetime] = set()
    for _ts_col, cells, raw_ts in data_rows:
        value = parse_number(cells[value_col]) if value_col < len(cells) else None
        if value is None or value < 0:
            skipped += 1
            continue
        moment = raw_ts if raw_ts.tzinfo is not None else _localize(raw_ts, tz, seen)
        if moment is None:
            skipped += 1
            continue
        start = moment.astimezone(UTC) - (QUARTER if timestamp_is_end else timedelta())
        if start.timestamp() % QUARTER_SECONDS:
            skipped += 1
            continue
        if start in quarters:
            duplicates += 1
        quarters[start] = value * factor
        accepted += 1
    if not quarters:
        raise LoadProfileError("no_valid_rows")
    return LoadProfile(
        quarters=dict(sorted(quarters.items())),
        rows=accepted,
        rows_skipped=skipped,
        duplicates=duplicates,
        value_column=unit,
        value_column_source=source,
    )


def _descending(stamps: list[datetime]) -> bool:
    """True, wenn die Zeitstempel überwiegend absteigend sortiert sind."""
    down = sum(1 for a, b in pairwise(stamps) if b < a)
    up = sum(1 for a, b in pairwise(stamps) if b > a)
    return down > up


def _width(cells: list[str]) -> int:
    """Anzahl Zellen ohne leere Zellen am Zeilenende (``;`` am Ende ist üblich)."""
    width = len(cells)
    while width and not cells[width - 1]:
        width -= 1
    return width


def _check_comma_decimals(header: list[str] | None, data_rows: list[tuple[int, list[str], datetime]]) -> None:
    """Komma als Trennzeichen UND als Dezimalzeichen (``…00:00,0,5``) → klarer Fehler.

    Unquotierte Dezimalkommas zerlegen eine Zahl in zwei Zellen; die Werte
    wären still falsch. Erkannt an: mehr Zellen als die Kopfzeile, ungleich
    vielen Zellen je Zeile, oder (ohne Kopfzeile) nur ganzen Zahlen in
    mindestens zwei Spalten nach der Zeitspalte.
    """
    widths = {_width(cells) for _col, cells, _ts in data_rows}
    if header is not None:
        if max(widths) > _width(header):
            raise LoadProfileError("decimal_comma_ambiguous")
        return
    if len(widths) > 1:
        raise LoadProfileError("decimal_comma_ambiguous")
    split_like = [
        [cell for cell in cells[col + 1 :] if cell]
        for col, cells, _ts in data_rows[:_DETECT_ROWS]
    ]
    if all(len(values) >= 2 and all(_INTEGER.fullmatch(v) for v in values) for values in split_like):
        raise LoadProfileError("decimal_comma_ambiguous")


def summarize_months(quarters: dict[datetime, float], tz: tzinfo) -> dict[str, ImportedMonth]:
    """Importierte Viertelstunden je Kalendermonat (Zuordnung über den Beginn)."""
    months: dict[str, ImportedMonth] = {}
    for start, kw in sorted(quarters.items()):
        months.setdefault(quarter_month_key(start, tz), ImportedMonth()).add(start, kw, tz)
    return months


def _summarize(quarters: dict[datetime, float], tz: tzinfo) -> ImportedMonth:
    """Kennzahlen der Viertelstunden eines Monats."""
    month = ImportedMonth()
    for start, kw in sorted(quarters.items()):
        month.add(start, kw, tz)
    return month


@dataclass(slots=True, frozen=True)
class HourStat:
    """Stundenwert für die Langzeitstatistik (Beginn in UTC)."""

    start: datetime
    mean: float
    min: float
    max: float
    quarters: int


def hourly_statistics(quarters: dict[datetime, float]) -> list[HourStat]:
    """Mittel/Min/Max der Viertelstunden je voller UTC-Stunde (Beginn der Viertelstunde zählt).

    Stunden mit fehlenden Viertelstunden werden aus den vorhandenen gebildet.
    """
    buckets: dict[datetime, list[float]] = {}
    for start, kw in quarters.items():
        hour = datetime.fromtimestamp(start.timestamp() // 3600 * 3600, tz=UTC)
        buckets.setdefault(hour, []).append(kw)
    return [
        HourStat(hour, sum(values) / len(values), min(values), max(values), len(values))
        for hour, values in sorted(buckets.items())
    ]
