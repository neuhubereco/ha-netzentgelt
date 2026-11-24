# 0.2.2 — Angleichung an die kundgemachte SNE-G-V

Stand 24.11.2026. Die Integration rechnete nach dem Begutachtungsentwurf (Juli 2026). Seit
12.10.2026 ist die Systemnutzungsentgelte-Grundsatzverordnung kundgemacht (BGBl. II Nr. 299/2026).
Die Tarifverordnung (SNE-T-V 2027) liegt nur als Begutachtungsentwurf vor.

## Abweichungen Entwurf → Verordnung (am RIS-Text gelesen)

| Thema | Entwurf (bisher im Code) | Verordnung | Umsetzung |
|---|---|---|---|
| WiNAP-Fenster | 22:00–04:00 | 23:00–05:00 (§ 7 Abs. 4) | `WINAP_START` / `WINAP_END` |
| WiNAP-Beginn | mit 1.1.2027 | erstmals ab 1.10.2027 (§ 32 Abs. 4) | `WINAP_FIRST_DAY`; Fensterende sucht bis zu 190 Tage |
| Mindestverrechnung NE7 | max(20 % vereinbart, 2 kW) | 2 kW; 20 % nur NE 1–6 (§ 6 Abs. 3) | `billed_kw(peak, minimum)` |
| Übergang 2027 | nicht vorgesehen | 1.1.–30.11.2027 Pauschale bzw. einheitlicher Preis, je Netzbereich verschiebbar bis 30.6.2028 (§ 6 Abs. 3a, 3b) | Option `billing_start`, Attribute `billing_start` / `billing_active` |
| Bestandsanschluss | gilt als 10 kW | max(10 kW, vereinbart, Mittel der zwei höchsten Monatsspitzen 2026) (§ 34 Abs. 1) | Attribut `grandfathered_kw` |
| Standardpreise | 33,82 / 67,64 €/kW/Jahr (stromliste.at) | — | Median des SNE-T-V-Entwurfs: 30,42 / 45,66; Tabelle je Netzbereich im README |

## Entscheidungen

- **Kosten bleiben eine Vorschau auf das Endsystem**, auch während des Übergangs. Der Verlauf
  rechnet seit jeher zurückliegende Monate mit den aktuellen Preisen durch; ein Sprung auf 0 € oder
  auf die Pauschale würde diese Vergleichsreihe zerstören. Der Stichtag macht die Vorschau
  kenntlich (`billing_active`).
- **Keine neuen Entities.** Der Stichtag ist eine Option im Dialog, die neuen Werte sind Attribute —
  wegen der sprachabhängigen Entity-IDs (Issue #2) kommt bis 0.3 nichts dazu.
- **`agreed_kw` bleibt** als Einstellung erhalten, wirkt aber nur noch auf `grandfathered_kw`.
- **Preise nicht als Auswahl je Netzbereich**: erst, wenn die SNE-T-V kundgemacht ist (Folgepunkt).

## Offen

- SNE-T-V 2027 nach Kundmachung: Tabelle und Standardwerte prüfen, Stichtag je Netzbereich,
  Auswahl des Netzbereichs im Options-Dialog.
- Screenshots im README zeigen noch das WiNAP-Band 22–04 Uhr.
