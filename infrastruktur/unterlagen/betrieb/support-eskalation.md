# Support- und Eskalationsprozess

Intern für {{auftritt.marke}}; die Kundensicht steht in der Leistungsbeschreibung, Abschnitt 6.

## 1 Kanäle

| Kanal | Erreichbarkeit | Für |
| --- | --- | --- |
| Telefon {{vertrag.support_telefon}} | {{vertrag.support_zeiten}} | alle Prioritäten |
| E-Mail {{vertrag.support_email}} | Eingang jederzeit, Bearbeitung in Supportzeiten | P2–P4 |
| Rufbereitschaft {{vertrag.notfall_telefon}} | täglich 7–20 Uhr | nur P1 |
| Monitoring-Alarm | automatisch, jederzeit | Ausfall Telefonassistent, Büro-Oberfläche, SIP-Registrierung |

## 2 Prioritäten und Fristen

| Priorität | Beispiele | Reaktion | Umgehung | Lösung |
| --- | --- | --- | --- | --- |
| P1 | Assistent nimmt nicht an; Oberfläche für alle weg; Verdacht auf Datenschutzverletzung | 1 h (7–20 Uhr) | 4 h | so schnell wie möglich |
| P2 | Terminvorschläge oder Rechnungsversand gestört; falsche Notdienstnummer wird genannt | 4 h in Supportzeiten | nächster Werktag | 3 Werktage |
| P3 | einzelne Funktion fehlerhaft; Beschwerde eines Endkunden über den Assistenten | nächster Werktag | 5 Werktage | nächste Version |
| P4 | Frage, Änderungswunsch, neue Gesprächsregel | 2 Werktage | – | nach Absprache |

Eine falsche Notdienstnummer oder eine fehlerhafte Einstufung eines Sicherheitsfalls wird immer mindestens als P2
behandelt und am selben Tag behoben.

## 3 Ablauf je Meldung

1. Ticket anlegen (Betrieb, Melder, Zeitpunkt, Beschreibung, Priorität). Bis ein Ticketsystem steht: Tabelle im
   gemeinsamen Postfach, nie im öffentlichen Repository.
2. Reaktion an den Kunden innerhalb der Frist: Eingang bestätigt, Priorität, nächste Information.
3. Diagnose: Logbuch des Mandanten, Telefonie-Protokoll, Gesundheitsprüfung der Dienste (`deploy/server/deploy.sh`).
4. Umgehung herstellen (z. B. Rufumleitung auf Rückfallebene, Gesprächsregel deaktivieren).
5. Lösung ausrollen, Kunden informieren (`vertrieb/email-vorlagen.md` Nr. 6), Ticket schließen.
6. Bei P1 und bei jedem Sicherheits- oder Notfallfehler: Nachbetrachtung in Schriftform binnen fünf Werktagen
   (Ursache, Wirkung, Maßnahme, Vorbeugung).

## 4 Eskalationsstufen

| Stufe | Wer | Wann |
| --- | --- | --- |
| 1 | Supportverantwortliche/r des Tages | jede Meldung |
| 2 | zweiter Gründer | P1 nicht binnen 1 h umgangen; P2 nicht binnen 4 h umgangen; jede Meldung zu einem Sicherheitsfall |
| 3 | Dienstleister (Hoster, Carrier, Azure, Anthropic) über deren Supportkanal | Ursache liegt beim Dienstleister |
| 4 | Rechtsberatung, ggf. Aufsichtsbehörde über den Verantwortlichen | Datenschutzverletzung mit Risiko für Betroffene |

## 5 Datenschutzverletzung (Art. 33, 34 DSGVO)

1. Sofort eindämmen (Zugänge sperren, Schlüssel rotieren, betroffenen Dienst trennen).
2. Sachverhalt sichern: Zeitpunkt der Kenntnis, betroffene Mandanten, Datenarten, Zahl der Personen.
3. **Jeden betroffenen Betrieb binnen 24 Stunden** nach Kenntnis informieren (AVV Ziffer 4.4,
   Vorlage `vertrieb/email-vorlagen.md` Nr. 7). Der Betrieb meldet als Verantwortlicher binnen 72 Stunden an die
   Aufsichtsbehörde; {{auftritt.marke}} unterstützt.
4. Betrifft der Vorfall eigene Daten von {{auftritt.marke}} (VVT eigene Verarbeitung), meldet {{auftritt.marke}} selbst
   binnen 72 Stunden an die Landesbeauftragte für Datenschutz und Informationsfreiheit NRW.
5. Dokumentation jedes Vorfalls, auch ohne Meldepflicht (Art. 33 Abs. 5 DSGVO).

## 6 Rufbereitschaft

Die Rufbereitschaft wechselt wöchentlich zwischen den Gründern. Übergabe montags: offene Tickets, geplante
Wartungen, Besonderheiten einzelner Betriebe. Die Bereitschaft hat Zugriff auf Server (SSH-Schlüssel mit
FIDO2), Carrier-Portal, Monitoring und das Notfallhandbuch (`plattform/HOSTING.md`, Abschnitt Wiederherstellung).
