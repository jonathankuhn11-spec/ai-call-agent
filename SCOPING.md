# Deployment-Scoping: PV-Lead-Qualifizierung per Voice Agent

**Kunde (fiktiv):** SonnenWerk Energie, PV-Anbieter, DACH
**Use Case:** Outbound-Anruf an Online-Leads innerhalb von 5 Minuten nach Anfrage, Qualifizierung, Terminbuchung
**Owner:** Jonathan Kuhn (Deployment Strategist)

## 1. Problem
- Online-Leads kühlen schnell ab: je später der Rückruf, desto niedriger die Kontaktquote.
- Das Vertriebsteam telefoniert zu 60–70 % mit unqualifizierten Leads (Mieter, Mehrfamilienhäuser).
- Die Fachberater sollen nur noch qualifizierte Termine bekommen.

## 2. Ziel und KPIs
| KPI | Definition | Pilot-Ziel |
|---|---|---|
| Speed-to-Lead | Zeit von Anfrage bis Anruf | < 5 min |
| Erreichbarkeitsquote | angenommene Anrufe / Anrufe | Baseline messen |
| Qualifizierungsquote | vollständig qualifizierte Leads / erreichte Leads | > 80 % |
| Terminquote | gebuchte Termine / qualifizierte Leads | > 35 % |
| Fehlerquote | Tool-Fehler + Guardrail-Flags / Gespräch | < 5 % |
| Übergabequalität | Anteil Termine, die der Berater als „passend“ bewertet | > 90 % |

## 3. Qualifizierungskriterien
- **Muss:** Eigentümer der Immobilie, Gebäudetyp EFH, DHH, RH oder Gewerbe
- **Soll:** Dachfläche ≥ 20 m², Jahresverbrauch ≥ 2.500 kWh
- **Disqualifiziert:** Mieter, Mehrfamilienhaus ohne WEG-Beschluss

## 4. Gesprächsleitfaden
Begrüßung → Anrufgrund → Zeitcheck → 4 Qualifizierungsfragen → Terminangebot (max. 2 Optionen) → Buchung → Verabschiedung

## 5. Edge Cases
| Situation | Verhalten |
|---|---|
| Falsche Person am Telefon | Nach besserem Zeitpunkt fragen, `falsche_person` |
| „Was kostet das?“ | Keine Preise nennen, auf Fachberatung im Termin verweisen |
| Mieter | Höflich disqualifizieren, `disqualifiziert` |
| „Keine Zeit“ | Rückruf anbieten, `rueckruf` |
| Kein passender Termin | Weitere Slots anbieten, sonst Rückruf |
| Aggressiv oder Opt-out | Sofort beenden, Sperrvermerk im CRM |

## 6. Integrationen
- **CRM:** Leads lesen, Qualifizierung schreiben (Prototyp: DuckDB, Produktion: z. B. HubSpot oder Salesforce per API)
- **Kalender:** Slots lesen und buchen
- **Telefonie:** SIP-Trunk (Produktion)

## 7. Guardrails
- Geschäftsregeln hart im Code: Buchung nur bei Status „qualifiziert“ und Eigentümer
- Validierung aller Tool-Parameter per Pydantic; Fehler gehen zurück ans Modell zur Selbstkorrektur
- Output-Check auf Preis- und Prozentangaben
- Maximal 5 Tool-Aufrufe pro Turn

## 8. Rechtliches (vor Go-live klären)
- Einwilligung des Leads für telefonische Kontaktaufnahme (§ 7 UWG)
- Transparenz, dass eine KI anruft (EU AI Act, Art. 50)
- DSGVO: Aufzeichnung und Speicherung der Transkripte, Auftragsverarbeitungsvertrag

## 9. Pilotplan
| Phase | Inhalt | Dauer |
|---|---|---|
| Woche 1 | Setup, Leitfaden, UAT mit 50 simulierten Gesprächen | 5 Tage |
| Woche 2 | Soft-Launch mit 10 % der Leads, tägliche Transkript-Reviews | 5 Tage |
| Woche 3–4 | 100 % der Leads, KPI-Review mit dem Kunden | 10 Tage |
| Go/No-Go | Terminquote und Übergabequalität gegen Ziel | – |
