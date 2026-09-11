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
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
import math
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
# Monatsspitze und Verlauf
# =============================================================================

HISTORY_MONTHS = 24


@dataclass(slots=True)
class MonthStats:
    """Kennzahlen eines Kalendermonats."""

    peak_kw: float | None = None
    peak_start: str | None = None
    valid_quarters: int = 0
    invalid_quarters: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Serialisierbare Darstellung."""
        return {
            "peak_kw": self.peak_kw,
            "peak_start": self.peak_start,
            "valid_quarters": self.valid_quarters,
            "invalid_quarters": self.invalid_quarters,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MonthStats:
        """Aus gespeicherten Daten laden (tolerant)."""
        peak = _to_float(data.get("peak_kw"))
        return cls(
            peak_kw=peak,
            peak_start=data.get("peak_start") if peak is not None else None,
            valid_quarters=int(data.get("valid_quarters", 0) or 0),
            invalid_quarters=int(data.get("invalid_quarters", 0) or 0),
        )


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
        if not result.valid or result.kw is None:
            stats.invalid_quarters += 1
            return False
        stats.valid_quarters += 1
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
        """Verlauf (neuester Monat zuerst): Monat → {kw, Zeitpunkt, …}."""
        return {key: self.months[key].as_dict() for key in sorted(self.months, reverse=True)}

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
