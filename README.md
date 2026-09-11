<img src="https://raw.githubusercontent.com/neuhubereco/ha-netzentgelt/main/custom_components/netzentgelt/brand/icon.png" alt="Icon: vier Viertelstunden unter der 10-kW-Grenze, der Teil einer Spitze darüber ist rot" width="96" align="right">

# Netzentgelt AT (Leistungspreis) für Home Assistant

Custom Integration für den geplanten **Leistungspreis** im österreichischen Netznutzungsentgelt:
misst die 15-Minuten-Bezugsleistung wie der Netzbetreiber, führt die Monatsspitze, schätzt den
Leistungspreis und liefert mit **Prognose** und **Spielraum** die Grundlage für Peak-Shaving
(z. B. Wallbox drosseln).

> **⚠️ Verordnung im Entwurf, Preise sind Richtwerte.** Grundlage ist der *Entwurf* der
> Systemnutzungsentgelte-Grundsatzverordnung (SNE-G-V) der E-Control (Begutachtung
> 30.06.–24.07.2026). Die endgültige Fassung und die Preise (Tarifverordnung SNE-T-V) stehen aus.
> Alle Werte dieser Integration sind eine **Näherung aus deinen eigenen Sensoren — maßgeblich ist
> ausschließlich der Zähler des Netzbetreibers.** Keine Rechts- oder Tarifberatung.

## Die Regel (Stand Entwurf)

- Ab **1.1.2027** wird für den Leistungspreis der **höchste Viertelstunden-Mittelwert der
  Bezugsleistung im Kalendermonat** herangezogen. Viertelstunden sind uhrzeitgebunden
  (:00, :15, :30, :45); der Mittelwert wird kaufmännisch auf 2 Nachkommastellen gerundet.
  Jeder Monat beginnt neu.
- **Netzebene 7 zweistufig:** bis einschließlich 10 kW gilt der günstigere Preis, für den
  darüber liegenden Anteil der höhere.
- **Mindestverrechnung:** 20 % der vertraglich vereinbarten Leistung, jedenfalls 2 kW.
  Bestandsanschlüsse (NE7) gelten als mit 10 kW vereinbart.
- **Zeitvariable Arbeitspreise:** SNAP (Sommer-Niedrigpreis) 1.4.–30.9. 10:00–16:00,
  WiNAP (Winter-Niedrigpreis) 1.10.–31.3. 22:00–04:00.

Eine einzige Viertelstunde mit Wallbox + Herd + Wärmepumpe bestimmt also den Preis für den ganzen
Monat — genau hier setzt Peak-Shaving an.

## Installation

### HACS (benutzerdefiniertes Repository)

1. HACS → Integrationen → Menü (⋮) → **Benutzerdefinierte Repositories**.
2. URL `https://github.com/neuhubereco/ha-netzentgelt`, Kategorie **Integration**, hinzufügen.
3. „Netzentgelt AT (Leistungspreis)“ installieren, Home Assistant neu starten.
4. Einstellungen → Geräte & Dienste → **Integration hinzufügen** → „Netzentgelt AT“.

### Manuell

Ordner `custom_components/netzentgelt` nach `<config>/custom_components/netzentgelt` kopieren,
Home Assistant neu starten, Integration wie oben hinzufügen.

Mindestversion: Home Assistant 2025.1.

## Konfiguration

**Einrichtung (Config-Flow)**

| Feld | Pflicht | Beschreibung |
|---|---|---|
| Name | ja | Gerätename, bestimmt die Entity-IDs |
| Energie Netzbezug | ja | Zählerstand des **Bezugs** (device_class `energy`, state_class `total`/`total_increasing`, Einheit Wh/kWh/MWh — wird umgerechnet) |
| Leistung Netzbezug | nein | Aktueller Bezug in W oder kW. Verbessert Prognose und Spielraum. Negative Werte (Einspeisung) zählen als 0 |

Mehrere Einträge (z. B. mehrere Zählpunkte) sind möglich — je Energiesensor einer.

**Optionen** (Zahnrad am Eintrag; Änderung lädt die Integration neu)

| Option | Standard | Bedeutung |
|---|---|---|
| Ziel-Leistung | 10 kW | Peak-Shaving-Ziel für den 15-Min-Mittelwert (Prognose, Spielraum, „Spitze droht“) |
| Staffelgrenze | 10 kW | bis einschließlich: Stufe 1, darüber Stufe 2 |
| Vereinbarte Leistung | 10 kW | vertraglich vereinbart (Bestandsanschluss NE7: 10 kW) |
| Mindestleistung | 2 kW | untere Grenze der Verrechnung (zusätzlich ≥ 20 % der vereinbarten Leistung) |
| Leistungspreis Stufe 1 | 33,82 €/kW/Jahr | **Richtwert** (Endausbau lt. stromliste.at, nicht amtlich) |
| Leistungspreis Stufe 2 | 67,64 €/kW/Jahr | **Richtwert** (dto.) |
| Arbeitspreis Standard / SNAP / WiNAP | 0 ct/kWh | 0 = nicht gesetzt; nur zur Anzeige im Tarifzeitfenster |
| Plausibilitätsgrenze | 60 kW | Viertelstunden darüber gelten als Messfehler |
| Hysterese | 0,2 kW | „Spitze droht“ geht erst unter *Ziel − Hysterese* wieder aus |

## Entities

Entity-IDs entstehen aus Gerätename und Entity-Name in der Systemsprache (Beispiele: Gerät
„Netzentgelt“, Deutsch).

| Entity | Beispiel-ID | Inhalt |
|---|---|---|
| 15-Min-Leistung | `sensor.netzentgelt_15_min_leistung` | Mittelwert der zuletzt abgeschlossenen **gültigen** Viertelstunde (kW). Attribute: `quarter_start/_end`, `energy_kwh`, `last_quarter_start`, `last_quarter_valid`, `last_quarter_reason`, `invalid_quarters_month` |
| Prognose Viertelstunde | `sensor.netzentgelt_prognose_viertelstunde` | (verbraucht seit Grenze + P_jetzt × Restzeit) × 4, alle 30 s |
| Monatsspitze | `sensor.netzentgelt_monatsspitze` | höchste gültige Viertelstunde im Kalendermonat. Attribute: `peak_quarter_start/_end`, `peak_rounded_kw`, `valid_quarters_month`, `invalid_quarters_month`, `history` (24 Monate: Monat → `peak_kw`, `peak_start`, Zähler; wird nicht in den Recorder geschrieben) |
| Verrechnete Leistung | `sensor.netzentgelt_verrechnete_leistung` | max(Monatsspitze kaufmännisch gerundet, Mindestleistung, 20 % der vereinbarten Leistung) |
| Leistungspreis Monat (geschätzt) | `sensor.netzentgelt_leistungspreis_monat_geschatzt` | (Stufe 1 bis Staffelgrenze + Stufe 2 darüber) / 12, in € |
| Spielraum | `sensor.netzentgelt_spielraum` | zusätzliche **konstante** Last (kW), die bis Viertelstundenende noch dazukommen darf, ohne das Ziel zu reißen; **negativ = drosseln** |
| Tarifzeitfenster | `sensor.netzentgelt_tarifzeitfenster` | `snap` / `winap` / `standard`; Attribute `window_end`, `energy_price_ct_kwh` (falls gesetzt) |
| Spitze droht | `binary_sensor.netzentgelt_spitze_droht` | ein, wenn Prognose > Ziel; aus erst unter Ziel − Hysterese |

Spielraum = ((Ziel / 4 − verbraucht_kWh) / Rest_h) − P_jetzt; die Restzeit wird auf mindestens
30 s begrenzt. P_jetzt kommt aus dem Leistungssensor, sonst aus der Steigung der letzten
Zählerstände (≈ 2 min), sonst aus dem Mittel der laufenden Viertelstunde (Attribut `power_source`).

## Messprinzip und Verhalten bei Messlücken

Das Vorbild (ein YAML-Paket) nahm an jeder Viertelstundengrenze einfach den aktuellen Zählerstand
— war der Zähler gerade `unavailable`, wurde daraus 0, und die nächste Differenz ergab eine
„Spitze“ von über 270 000 kW. Diese Integration rechnet deshalb so:

1. **Samples:** Jeder gemeldete Zählerstand wird mit Zeitstempel gesammelt — auch unveränderte
   Werte (HA-Ereignis `state_reported`), damit ein stehender Zähler nicht als Lücke gilt.
2. **Interpolation an der Grenze:** Der Zählerstand um :00/:15/:30/:45 wird **linear
   interpoliert** aus dem letzten gültigen Sample davor und dem ersten danach. Damit ist die
   Poll-Latenz der Quelle ausgeglichen. Die Viertelstunde wird deshalb erst ausgewertet, sobald
   das erste Sample *nach* der Grenze eingetroffen ist (bei pollenden Zählern nach Sekunden).
3. **Ungültig statt raten:** Eine Viertelstunde ist ungültig, wenn
   - die beiden Samples um eine Grenze mehr als **20 min** auseinanderliegen *und* der Zähler
     dazwischen um mehr als 0,01 kWh gestiegen ist (bei höchstens 0,01 kWh ist der interpolierte
     Stand auf 0,01 kWh genau — Fehler der Viertelstunde ≤ 0,04 kW; typisch für Zähler, die nur bei
     Änderung melden, bei nahezu null Bezug),
   - die Quelle zwischendurch `unavailable`/`unknown`/nicht numerisch war oder die Einheit fehlt,
   - der Zähler **fällt** (Reset, Zählertausch) oder unplausibel springt,
   - die Leistung über der **Plausibilitätsgrenze** liegt,
   - nach dem Start von Home Assistant bzw. einem Neuladen der Integration die Startgrenze nicht
     beobachtet wurde (die laufende Viertelstunde ist dann ungültig),
   - binnen 3 h nach der Grenze kein Sample mehr kam.
4. **Ungültige Viertelstunden erzeugen keinen Wert.** Der Sensor *15-Min-Leistung* behält den
   letzten gültigen Wert (die Attribute `quarter_start`/`quarter_end` sagen, zu welcher
   Viertelstunde er gehört) und meldet über `last_quarter_valid: false` und
   `last_quarter_reason`, dass die jüngste Viertelstunde verworfen wurde. Ungültige
   Viertelstunden gehen **nie** in die Monatsspitze ein und werden in `invalid_quarters_month`
   gezählt. Bewusst gewählt: Ein Sprung auf `unknown` würde Automationen und Statistiken
   stören, ein erfundener Wert wäre schlimmer als keiner.
5. **Monatszuordnung** über den **Beginn** des Intervalls in Ortszeit (HA-Zeitzone):
   23:45–00:00 am Monatsletzten gehört zum alten Monat, 00:00–00:15 zum neuen — auch wenn die
   23:45-Viertelstunde erst nach Mitternacht fertig ausgewertet wird.
6. Monatsspitzen und Verlauf (24 Monate) werden im HA-Speicher (`.storage/netzentgelt.<id>`)
   abgelegt und überstehen Neustarts.

**WiNAP-Auslegung:** Das Fenster 22:00–04:00 reicht über Mitternacht. Die Integration ordnet eine
Nacht dem Tag zu, an dem sie **beginnt**: WiNAP-Nächte beginnen an Tagen vom 1.10. bis 31.3.
um 22:00 und enden um 04:00 des Folgetags. Die Nacht 31.3.→1.4. ist damit bis 04:00 noch WiNAP;
die Nacht 30.9.→1.10. ist es nicht (erste WiNAP-Nacht: 1.10. 22:00). Sollte die endgültige
Verordnung anders abgrenzen, wird das angepasst.

## Beispiel-Automation: Wallbox über den Spielraum regeln

Generisches Beispiel — `number.wallbox_ladestrom` durch die eigene Ladestrom-Entity ersetzen und
an Phasenzahl/Mindeststrom anpassen. **Nicht gegen eine echte Wallbox getestet; zuerst mit
Benachrichtigungen statt Stellbefehlen ausprobieren.**

```yaml
automation:
  - alias: "Netzentgelt: Wallbox an Spielraum anpassen"
    mode: single
    triggers:
      - trigger: state
        entity_id: sensor.netzentgelt_spielraum
    conditions:
      - condition: template
        value_template: >
          {{ states('sensor.netzentgelt_spielraum') | is_number
             and states('number.wallbox_ladestrom') | is_number }}
    actions:
      - variables:
          # 3-phasig, 230 V: 1 A ≈ 0,69 kW
          kw_pro_ampere: 0.69
          aktuell_a: "{{ states('number.wallbox_ladestrom') | float }}"
          spielraum_kw: "{{ states('sensor.netzentgelt_spielraum') | float }}"
          ziel_a: >
            {{ [[(aktuell_a + spielraum_kw / kw_pro_ampere) | round(0, 'floor'), 6] | max, 16] | min }}
      - condition: template
        value_template: "{{ (ziel_a - aktuell_a) | abs >= 1 }}"
      - action: number.set_value
        target:
          entity_id: number.wallbox_ladestrom
        data:
          value: "{{ ziel_a }}"
```

Reicht der Mindeststrom nicht (Spielraum bleibt negativ), muss die Automation das Laden
pausieren — das ist wallbox-spezifisch. Wärmepumpen besser nur begrenzen, nicht hart abschalten.

Ein Beispiel-Dashboard liegt in [`examples/dashboard.yaml`](examples/dashboard.yaml).

## Genauigkeit und Grenzen

**Nachgeprüft mit echten Daten:** Sieben Wochen Zählerstände eines Haushalts (Mai/Juni 2026,
Fronius Smart Meter über Home Assistant, ~35.500 Werte) wurden durch die Rechenlogik gespielt und
mit dem Viertelstunden-Lastgang des Netzbetreibers verglichen (4.473 Viertelstunden):
mittlere Abweichung **0,008 kW**, 95 % der Viertelstunden unter 0,033 kW, größte Abweichung
0,25 kW. Die Monatsspitze lag in beiden Monaten auf **derselben Viertelstunde** wie beim
Netzbetreiber, aber rund **0,5 % niedriger** (16,54 statt 16,64 kW, 17,41 statt 17,47 kW) — der
Hauszähler misst etwas anders als der Netzzähler. **Deshalb das Ziel mit Puffer setzen**
(z. B. 9,5 kW, wenn 10 kW die Grenze ist). Rund 3 % der Viertelstunden waren wegen Datenlücken
(Neustarts, fehlende Werte) ungültig. Das Skript dafür liegt unter `tools/replay.py`.

- Die Werte sind nur so gut wie der Quellsensor. Zeitstempel ist der Moment, in dem Home
  Assistant den Wert erhält; Verzögerungen im Quellgerät selbst bleiben unsichtbar.
- Innerhalb einer Poll-Periode wird konstante Leistung angenommen (Interpolation). Springt die
  Last genau an der Grenze, verschiebt sich ein kleiner Anteil zwischen den Viertelstunden.
- Flexible Entnahme (§ 10 Entwurf) und Leistungssaldierung bei Energiegemeinschaften (§ 9) sind
  nicht abgebildet.
- Keine aktive Steuerung: Die Integration misst und rechnet nur. Schalten ist Sache eigener
  Automationen.

## Quellen

- E-Control: Begutachtungsentwurf der Systemnutzungsentgelte-Grundsatzverordnung (SNE-G-V),
  Begutachtung 30.06.–24.07.2026 — insbesondere § 3/§ 6 (Leistungspreis, Staffel,
  Mindestverrechnung), § 7 (SNAP/WiNAP), § 34 (Bestandsanschlüsse). Veröffentlicht auf
  [e-control.at](https://www.e-control.at) (Begutachtungen/Verordnungsentwürfe).
- Richtwerte der Leistungspreise (Endausbau): [stromliste.at](https://www.stromliste.at) — nicht
  amtlich.

## Entwicklung

```bash
uv venv -p 3.13 .venv
uv pip install --python .venv/bin/python -r requirements_test.txt
.venv/bin/python -m pytest -q
```

Die Rechenlogik (`custom_components/netzentgelt/calc.py`) hat keine Home-Assistant-Abhängigkeit;
`tests/test_calc.py` läuft auch mit reinem `pytest`.

Eigene Daten gegen den Netzbetreiber-Lastgang prüfen:

```bash
python3 tools/replay.py zaehler.csv --reference lastgang.csv
# zaehler.csv:  ISO-Zeitstempel,kWh   (z. B. InfluxDB-Export des Energiesensors)
# lastgang.csv: TT.MM.JJJJ HH:MM;kWh;kW  (Portal-Export, Zeitstempel = Beginn der Viertelstunde)
```

## Lizenz

MIT © 2026 Florian Neuhuber — siehe [LICENSE](LICENSE).

---

## English summary

**Grid capacity charge Austria** — a Home Assistant custom integration for the capacity-based grid
fee planned in Austria from 2027 (draft regulation SNE-G-V by E-Control): the highest
clock-aligned 15-minute average grid import power per calendar month is billed (rounded half-up to
0.01 kW), grid level 7 has two tiers (≤ 10 kW / above), minimum billing is max(20 % of the agreed
capacity, 2 kW). Time-variable energy prices: SNAP 1 Apr–30 Sep 10:00–16:00, WiNAP 1 Oct–31 Mar
22:00–04:00 (a night belongs to the day it starts on).

- **Install:** HACS → custom repository `https://github.com/neuhubereco/ha-netzentgelt`
  (category *Integration*), restart, add “Netzentgelt AT”.
- **Configure:** grid import energy sensor (Wh/kWh/MWh, `total`/`total_increasing`), optional grid
  import power sensor (W/kW). Thresholds and prices in the options; default prices are indicative
  only.
- **Entities:** 15-minute power, forecast, monthly peak (+ 24-month history), billed power,
  estimated monthly capacity charge, headroom (negative = reduce load), tariff window, binary
  “peak imminent”.
- **Measurement:** meter readings are linearly interpolated at each quarter-hour boundary; if the
  source was unavailable, the meter decreased/jumped, the samples around a boundary are more than
  20 minutes apart (unless the meter rose by ≤ 0.01 kWh), or the power is implausible, the quarter
  is marked invalid, never enters the monthly peak, and the 15-minute sensor keeps its last valid
  value with `last_quarter_valid: false`.

> **Draft regulation, indicative prices. The network operator's meter is authoritative.**

License: MIT © 2026 Florian Neuhuber.
