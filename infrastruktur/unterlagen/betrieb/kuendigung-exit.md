# Kündigung und Exit

Verfahren bei Vertragsende, Pilotende ohne Übergang oder außerordentlicher Kündigung für {{kunde.firma}}.
Rechtliche Grundlage: AGB § 7 und § 9, AVV Ziffer 9, Pilotvereinbarung Abschnitt 7.

## 1 Fristen

| Fall | Frist | Wirksam |
| --- | --- | --- |
| Ordentliche Kündigung nach Mindestlaufzeit | ein Monat zum Monatsende | Ende des Folgemonats |
| Kündigung während der Mindestlaufzeit | zum Ende der Mindestlaufzeit (zwölf Monate) | Ende der Mindestlaufzeit |
| Pilot | sieben Tage, jederzeit; endet sonst automatisch | Pilotende |
| Preisanpassung über 5 %, wesentliche Funktionsänderung, AGB-Änderung | zum Wirksamwerden der Änderung | Wirksamwerden |
| Außerordentlich aus wichtigem Grund | sofort | Zugang der Kündigung |

## 2 Ablauf

| Nr. | Schritt | Wer | Frist | erledigt |
| --- | --- | --- | --- | --- |
| 1 | Kündigung in Textform eingegangen, Datum und Wirksamkeit geprüft | {{auftritt.marke}} | Eingang | |
| 2 | Kündigungsbestätigung mit Exit-Terminen senden (`vertrieb/email-vorlagen.md` Nr. 8) | {{auftritt.marke}} | 2 Werktage | |
| 3 | Kurzes Gespräch zum Kündigungsgrund anbieten (freiwillig) | {{auftritt.marke}} | vor Vertragsende | |
| 4 | Termin zum Abschalten der Rufumleitung zum Vertragsende vereinbaren | beide | vor Vertragsende | |
| 5 | Offene Freigaben, Rechnungsentwürfe und Belegübergaben abschließen oder verwerfen | Betrieb | bis Vertragsende | |
| 6 | Rufumleitung beim Telefonanbieter abschalten, Testanruf: klingelt wieder im Betrieb | Betrieb, begleitet | Vertragsende | |
| 7 | Telefonassistent für den Mandanten deaktivieren, Rufnummer im Trunk freigeben | {{auftritt.marke}} | Vertragsende | |
| 8 | Vollständigen Datenexport bereitstellen (CSV/JSON, Rechnungen als PDF und XRechnung, Rapporte als PDF) | {{auftritt.marke}} | bis 30 Tage nach Vertragsende | |
| 9 | Export vom Betrieb bestätigt (Vollständigkeit, Lesbarkeit) | Betrieb | 14 Tage nach Bereitstellung | |
| 10 | Benutzerzugänge des Mandanten sperren | {{auftritt.marke}} | nach Bestätigung des Exports | |
| 11 | Löschung aller Mandantendaten einschließlich Sicherungen (Sicherungen laufen nach 14 Tagen aus, Zweitsicherung nach deren Frist) | {{auftritt.marke}} | 6 Wochen nach Export, spätestens 3 Monate nach Vertragsende | |
| 12 | Anbindungsschlüssel (Lexware, sevDesk, Postfach) löschen; Betrieb widerruft sie in den Portalen | beide | mit Schritt 11 | |
| 13 | Löschbestätigung in Textform senden | {{auftritt.marke}} | nach Schritt 11 | |
| 14 | Letzte Rechnung (anteilige Mehrminuten), Lastschriftmandat beenden | {{auftritt.marke}} | Folgemonat | |

## 3 Was nicht gelöscht wird

Rechnungen und Vertragsunterlagen, die {{auftritt.marke}} für die eigene Buchführung benötigt (Aufbewahrung nach
§ 147 AO, § 257 HGB), sowie die Löschbestätigung selbst.

## 4 Exit bei Zahlungsverzug oder Insolvenz des Betriebs

Der Telefonassistent kann nach Ankündigung ausgesetzt werden (AGB § 6.4); die Rufumleitung fällt dann auf die
Rückfallebene des Betriebs. Der Datenexport wird auch in diesem Fall bereitgestellt.
