# Löschkonzept

Entwurf nach dem Aufbau der DIN 66398 (Datenarten, Löschklassen, Fristen, Umsetzung). Die Fristen sind
im Code hinterlegt (`core/loeschung.py`) und je Betrieb anpassbar (`Mandant.policies["aufbewahrung"]`),
die Umsetzung läuft täglich um 02:45 Uhr (`python -m core loeschlauf`, cron in `deploy/server/cloud-init.yaml`)
und wird je Lauf im Ereignisprotokoll nachgewiesen (`loeschung.lauf`).

## 1 Grundsätze

- Speicherbegrenzung (Art. 5 Abs. 1 lit. e DSGVO): jede Datenart hat eine Frist und einen Startzeitpunkt.
- Keine Audioaufzeichnung; die Verarbeitung von Sprache ist auf das laufende Gespräch beschränkt.
- Aufbewahrungspflichten gehen vor (Art. 17 Abs. 3 lit. b): Rechnungen und Buchungsbelege bleiben
  10 Jahre; der Personenbezug im Kundendatensatz wird stattdessen durch Anonymisierung entfernt, die
  Rechnung trägt den Empfänger als Momentaufnahme (§ 14 Abs. 4 UStG).
- Löschen heißt physisches Entfernen aus der Datenbank; Sicherungskopien laufen nach 14 Tagen lokal
  und spätestens nach 12 Monaten außer Haus aus (Aufbewahrungsschema in `backup.sh`).

## 2 Datenarten, Löschklassen und Fristen

| Datenart | Startzeitpunkt | Frist (Standard) | Löschklasse | Umsetzung |
| --- | --- | --- | --- | --- |
| Gesprächstranskript | Gesprächsende | 90 Tage | Transkript entfernen, Zusammenfassung und Extraktion bleiben | `transkript_tage` |
| Gespräche ohne Kunden- und Vorgangsbezug (Falschwahl, Werbung) | Gesprächsende | 30 Tage | vollständig | `kommunikation_lose_tage` |
| Zustellnachrichten (Outbox) zugestellt / ohne Kanal | Zustellung | 90 Tage | vollständig | `outbox_tage` |
| Zustellnachrichten gescheitert (Dead-Letter) | letzter Versuch | 365 Tage | vollständig (Fehleranalyse) | `outbox_tot_tage` |
| Ereignisprotokoll (fachlich) | Ereignis | 3 Jahre | vollständig, außer `rechnung.*`, `freigabe.*`, `loeschung.*`, `kunde.geloescht` (10 Jahre) | `ereignis_tage` |
| Abgeschlossene Vorgänge ohne Rechnung (Termine, Kommunikation, Ereignisse) | Abschluss (übergeben, storniert, abgerechnet) | 3 Jahre; bei Arbeiten an Bauwerken 5 Jahre einstellen (§ 634a BGB) | vollständig | `vorgang_tage` |
| Vorgänge mit Rechnung | Rechnungsdatum | 10 Jahre | mit der Rechnung | `rechnung_tage` |
| Kundendatensatz ohne Vorgang, Vertrag und Rechnung | letzte Aktivität | 3 Jahre | Anonymisierung (Name, Kontakt, Anschrift, Notiz, Einwilligungen, Objektanschrift) | `kunde_inaktiv_tage` |
| Kundendatensatz mit Wartungsvertrag | Vertragsende | 3 Jahre nach Vertragsende | Anonymisierung | Vertragsstatus pflegen |
| Rechnungen, Stornos, Belegpakete | Rechnungsdatum (Jahresende) | 10 Jahre | vollständig | `rechnung_tage`; Belegpakete bei der Kanzlei |
| Einwilligungen | Widerruf oder Anonymisierung | mit dem Kundendatensatz | Nachweis bis 3 Jahre nach Widerruf im Ereignisprotokoll | Protokoll |
| Anmeldekonten der Mitarbeitenden (Keycloak) | Ausscheiden | unverzüglich sperren, 90 Tage danach löschen | manuell durch Inhaber | Keycloak-Konsole |
| Server-Logs (Caddy, Anwendung) | Eintrag | 5 Dateien à 20 MB (Rotation), in der Regel unter 30 Tagen | Rotation | `docker-compose.prod.yml` |
| Sicherungen | Erstellung | 14 Tage lokal; außer Haus 14 täglich / 8 wöchentlich / 12 monatlich | Ablauf | `backup.sh` |
| Mandant nach Vertragsende | Übergabe des Exports | 6 Wochen, spätestens 3 Monate | vollständig inkl. Sicherungen (AVV Ziffer 9) | manuell, Protokoll |
| Eingangsbelege (Rechnungen, Lieferscheine, Quittungen) mit Datei | Belegdatum | 10 Jahre (§ 147 AO, § 14b UStG) | vollständig inkl. Datei | `beleg_tage` |
| Scan-Eingang: Importkennungen (Message-ID, Dateihash) und Übergabereferenzen zu Steuertools | mit dem Beleg | wie Beleg | mit dem Beleg | Teil des Belegdatensatzes (`import_kennung`, `external_ref`); Dateien im Eingangsordner `verarbeitet/` löscht der Betrieb nach dem Abruf selbst |
| Zeiteinträge ohne Vorgangsbezug | Beginn | 2 Jahre (§ 16 Abs. 2 ArbZG) | vollständig | `zeit_tage`; mit Vorgang: mit dem Vorgang |
| Rapporte, Fotos, Unterschriften am Vorgang | Abschluss des Vorgangs | mit dem Vorgang (3 Jahre bzw. 10 Jahre mit Rechnung) | vollständig | `_vorgangsanhang_loeschen` |
| Dokumente und Fotos am Kunden oder Objekt | Löschung des Kunden | mit dem Kundendatensatz | vollständig | `kunde_loeschen` |
| Angebote ohne Auftrag (Entwurf, abgelehnt, abgelaufen) | Angebotsdatum | 3 Jahre (Geschäftsbrief, § 257 Abs. 4 HGB: 6 Jahre nur bei Zugang; angenommene Angebote 10 Jahre mit der Rechnung) | vollständig | `angebot_tage`; angenommene mit der Rechnung |
| Zahlungen und Mahnungen | Rechnungsdatum | 10 Jahre mit der Rechnung (§ 147 AO) | mit der Rechnung | `rechnung_tage` |
| Wartungsverträge (beendet oder gekündigt) | Vertragsende | 3 Jahre (§ 195 BGB), Kundendatensatz 3 Jahre nach Vertragsende | vollständig | `vertrag_tage`; aktive Verträge sperren die Löschung |
| Abwesenheiten der Monteure | Ende der Abwesenheit | 2 Jahre (wie Zeiteinträge, § 16 Abs. 2 ArbZG) | vollständig | `zeit_tage` |
| Schriftliche Anfragen (Webformular, Postfach) als Rohdaten | Eingang | 1 Jahr; der daraus entstandene Vorgang folgt den Vorgangsfristen | vollständig | `anfrage_tage`; Nachrichten verbleiben zusätzlich im Postfach des Betriebs |

Hinweis zur Frist für Buchungsbelege: Das Vierte Bürokratieentlastungsgesetz verkürzt die Frist für
Buchungsbelege ab 2025 auf 8 Jahre; Rechnungen nach § 14b UStG bleiben bei 10 Jahren. Die Plattform
verwendet einheitlich 10 Jahre; eine Verkürzung ist mit der Kanzlei abzustimmen.

## 3 Betroffenenrechte

| Recht | Umsetzung | Frist |
| --- | --- | --- |
| Auskunft (Art. 15) und Datenübertragbarkeit (Art. 20) | `GET /kunden/{id}/auskunft` liefert alle Daten des Kunden strukturiert; Abruf wird protokolliert | ein Monat |
| Löschung (Art. 17) | `POST /kunden/{id}/loeschen`: vollständig, wenn kein offener Vorgang und kein Wartungsvertrag; bei Rechnungen in der Aufbewahrungsfrist Anonymisierung; Ergebnis mit Grund im Protokoll | ein Monat |
| Berichtigung (Art. 16) | Bearbeitung des Kundendatensatzes in der Oberfläche | unverzüglich |
| Widerspruch / Widerruf (Art. 21, Art. 7 Abs. 3) | Einwilligungen (z. B. SMS) im Kundendatensatz entfernen; Widerruf wirkt sofort auf den Versand | unverzüglich |
| Einschränkung (Art. 18) | Vorgang stornieren und Notiz; technische Sperrung ist für den Pilot manuell | unverzüglich |

Anfragen gehen an den Betrieb als Verantwortlichen; der Anbieter unterstützt (AVV Ziffer 6).

## 4 Verantwortlichkeiten und Nachweis

- Der Betrieb bestätigt die Fristen bei Vertragsschluss und kann sie in den Einstellungen ändern
  (geplant: Oberfläche; derzeit über `policies.aufbewahrung` per API).
- Der Anbieter betreibt den Löschlauf, prüft monatlich die Protokolle (`loeschung.lauf` mit Zählern
  und Fristen) und dokumentiert Abweichungen.
- Jeder Löschantrag erzeugt ein Ereignis `kunde.geloescht` mit Art (vollständig/anonymisiert) und Grund;
  dieses Ereignis bleibt als Nachweis 10 Jahre erhalten, enthält aber keine personenbezogenen Daten mehr.
- Nachweis der Umsetzung: Testfälle `tests/test_loeschung.py` laufen bei jeder Änderung in der CI.

## 5 Offene Punkte

- Oberfläche für Aufbewahrungsfristen und für Auskunft/Löschung im Kundendatensatz (derzeit API).
- Unterschriften auf Rapporten sind biometrienah; sie werden als einfaches Bild ohne Druck- oder
  Zeitdaten gespeichert und nur mit dem Rapport gelöscht. Hinweis im Datenschutzhinweis ergänzen.
- Löschung in Sicherungen vor Fristablauf ist technisch nicht vorgesehen; der Betrieb wird im AVV
  darauf hingewiesen (üblich und zulässig bei kurzen Aufbewahrungsschemata).
- Audioaufzeichnung ist nicht implementiert; falls sie je eingeführt wird, braucht es Einwilligung
  (§ 201 StGB) und eine eigene Frist (Vorschlag 30 Tage).
