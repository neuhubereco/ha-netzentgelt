#!/usr/bin/env python3
"""Zählerstände durch die Rechenlogik spielen und optional mit dem Netzbetreiber-Lastgang vergleichen.

Eingabe ``samples``: CSV mit ``Zeitstempel,kWh`` je Zeile (ISO 8601 mit Zeitzone, z. B. ein
InfluxDB-Export des Energiesensors; Kopfzeilen und Zeilen ohne Zahl werden übersprungen).
Eingabe ``--reference``: Portal-Export ``TT.MM.JJJJ HH:MM;kWh;kW`` (Zeitstempel = Beginn der
Viertelstunde, Ortszeit).

Beispiel:
    python3 tools/replay.py zaehler.csv --reference lastgang.csv --tz Europe/Vienna
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components" / "netzentgelt"))
import calc  # noqa: E402


def read_samples(path: Path) -> list[tuple[datetime, float]]:
    rows: list[tuple[datetime, float]] = []
    with path.open(encoding="utf-8-sig") as fh:
        for rec in csv.reader(fh):
            cells = [c for c in rec if c]
            for i in range(len(cells) - 1):
                try:
                    ts = datetime.fromisoformat(cells[i].replace("Z", "+00:00"))
                    kwh = float(cells[i + 1])
                except ValueError:
                    continue
                if ts.tzinfo is not None:
                    rows.append((ts, kwh))
                break
    rows.sort()
    return rows


def read_reference(path: Path) -> dict[datetime, float]:
    ref: dict[datetime, float] = {}
    with path.open(encoding="utf-8-sig") as fh:
        for rec in csv.reader(fh, delimiter=";"):
            try:
                ref[datetime.strptime(rec[0], "%d.%m.%Y %H:%M")] = float(rec[2].replace(",", "."))
            except (ValueError, IndexError):
                continue
    return ref


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("samples", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--tz", default="Europe/Vienna")
    parser.add_argument("--max-gap-min", type=float, default=20.0)
    args = parser.parse_args()

    tz = ZoneInfo(args.tz)
    rows = read_samples(args.samples)
    if not rows:
        print("Keine Zählerstände gelesen.", file=sys.stderr)
        return 1

    engine = calc.QuarterEngine(max_gap=timedelta(minutes=args.max_gap_min))
    engine.start(rows[0][0])
    results: list[calc.QuarterResult] = []
    next_boundary = calc.quarter_floor(rows[0][0]) + calc.QUARTER
    for ts, kwh in rows:
        while next_boundary <= ts:
            results += engine.boundary(next_boundary)
            next_boundary += calc.QUARTER
        results += engine.add_sample(ts, kwh)
        results += engine.tick(ts)

    valid = {r.start.astimezone(tz).replace(tzinfo=None): r.kw for r in results if r.valid and r.kw is not None}
    invalid = [r for r in results if not r.valid]
    print(f"Zählerstände: {len(rows)} ({rows[0][0]:%d.%m.%Y} – {rows[-1][0]:%d.%m.%Y})")
    print(f"Viertelstunden: {len(results)}, gültig {len(valid)}, ungültig {len(invalid)}")
    if invalid:
        print("Gründe ungültig:", dict(Counter(r.reason for r in invalid).most_common()))

    months = sorted({(k.year, k.month) for k in valid})
    ref = read_reference(args.reference) if args.reference else {}
    for year, month in months:
        peak_kw, peak_at = max((v, k) for k, v in valid.items() if (k.year, k.month) == (year, month))
        line = f"{month:02d}/{year}: Spitze {calc.round_half_up(peak_kw):.2f} kW am {peak_at:%d.%m. %H:%M}"
        ref_month = [(v, k) for k, v in ref.items() if (k.year, k.month) == (year, month) and k >= min(valid)]
        if ref_month:
            rv, rk = max(ref_month)
            line += f"  |  Netzbetreiber {rv:.2f} kW am {rk:%d.%m. %H:%M}"
        print(line)

    if ref:
        diffs = sorted(abs(v - ref[k]) for k, v in valid.items() if k in ref)
        if diffs:
            print(
                f"Abweichung zum Lastgang (n={len(diffs)}): Ø {sum(diffs) / len(diffs):.3f} kW, "
                f"95 % ≤ {diffs[int(len(diffs) * 0.95)]:.3f} kW, max {diffs[-1]:.3f} kW"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
