# Vertriebs-Agenten: Steuerung der Kette vom Lead bis zum Abschluss

Die Lead-Agenten finden Betriebe, die Paket-Agenten erzeugen Brief, Angebot und Demo je Lead. Die Vertriebs-Agenten
schließen die Lücke dazwischen und dahinter: Sie führen alle Listen zu einem Bestand zusammen, entscheiden, welcher
Betrieb wann welchen Brief mit welchem Angebot erhält, verfolgen den Rücklauf und lernen daraus. Sie rufen keine
Webseite ab und belasten das gemeinsame Abrufkontingent nicht; versendet wird nichts ohne Freigabe.

```bash
python infrastruktur/vertriebsagent.py lauf                      # alle Listen → Pipeline, Cockpit, Charge zur Freigabe
python infrastruktur/vertriebsagent.py einlesen <Charge_…_Freigabe.xlsx>   # Freigabe, Versanddatum, Reaktionen
python infrastruktur/vertriebsagent.py ereignis L1a2b3c4d5 antwort_positiv --notiz "Rückruf erbeten, ab 16 Uhr"
python infrastruktur/vertriebsagent.py status
# Briefe, Angebote, Demo für die freigegebenen Leads (Paketliste aus „einlesen“):
python infrastruktur/paketagent.py <charge>/paketliste.xlsx --plattform ../kuhn-plattform/plattform --out /mnt/project-files/leads/pakete/
```

Ausgabe unter `/mnt/project-files/leads/vertrieb/` (Standard, `--wurzel` ändert den Leadordner): `pipeline.db`
(Ereignisse, Chargen, Sperrliste; nie im Repository), `Vertriebscockpit.xlsx`, `abrufbudget.json`,
`chargen/<ID>/Charge_<ID>_Freigabe.xlsx`, nach Freigabe `serienbrief.csv` und `paketliste.xlsx`.

## Agenten

| Agent | Modul | Leistung |
| --- | --- | --- |
| Erfassung | `erfassung.py` | liest alle Listenfamilien (jeweils jüngste Fassung), erkennt Spalten über Synonyme, zerlegt Freitext-Anschriften, führt Dubletten per Union-Find (Rufnummer, Firmenname+PLZ) zusammen; Ausschluss- und Sperrlisten |
| Upsell | `upsell.py` | Edition und Branche vom Bedarfs-Agenten der Kundenpakete; Zusatzmodule nach `katalog.toml` (WhatsApp, Karriereseite, Website, Disposition …); Kundenwert (CLV) |
| Priorisierung | `scoring.py` | Prior aus dem Abschluss-Score, globale Kalibrierung und Empirical-Bayes-Lift je Branche und Signaltyp aus dem Rücklauf, Rechtszensierung laufender Kadenzen, Erwartungswert je Brief |
| Nachfass | `kadenz.py` | Kadenzstatus und nächste Aufgabe je Lead (Nachfassbrief nach 14 Tagen, Rückruf binnen 24 h nach positiver Rückmeldung, Angebot nachfassen …) |
| Versand | `versand.py` | Charge (Nachfass zuerst, 80 % nach EV, 20 % Thompson-Exploration, höchstens 40 % je Branche), Freigabe- und Rückmeldebogen, Serienbriefdatei, Paketliste |
| Abrufbudget | `budget.py` | neue Leads und Vertriebswert je Webseitenabruf nach Quelle × Branche, Empfehlung Abrufe/h für die Wellen-Agenten |
| Cockpit | `cockpit.py` | Übersicht, Pipeline, Charge, Aufgaben, Upsell-Potenzial, Segmente (Bayes), Abrufbudget, Sperren, Quellen, Methodik |

## Rückkopplung

Jede eingetragene Reaktion verändert die Priorisierung aller noch nicht angeschriebenen Leads: Antworten Kfz-Betriebe
mit Personalengpass überdurchschnittlich, rücken alle Kfz- und Personalengpass-Leads nach oben, und das Abrufbudget
verschiebt sich zu den Quellen, die solche Leads liefern. Kontakte zählen erst als Versuch, wenn ein Ergebnis vorliegt
oder die Kadenz abgelaufen ist; laufende Kadenzen würden die Quote sonst systematisch unterschätzen.

## Rechtsrahmen

Erst- und Nachfasskontakt ausschließlich per Brief (§ 7 Abs. 1 UWG). Telefon, E-Mail und WhatsApp erst nach
Rückmeldung bzw. dokumentierter Einwilligung (§ 7 Abs. 2 Nr. 1 und 2 UWG). Ablehnung oder Widerspruch sperren
dauerhaft über Lead-ID und Rufnummer, auch nach jedem Neuaufbau aus den Listen (Art. 21 Abs. 3 DSGVO). „Keine
Website“ (P1) wird nur bei ausdrücklichem Beleg gesetzt, damit kein Brief eine falsche Tatsachenbehauptung enthält.
Alle Modellkonstanten in `katalog.toml` sind Annahmen bis zum Vorliegen eigener Rücklaufdaten; Zusatzmodulpreise mit
Status „Vorschlag“ sind nicht beschlossen.
