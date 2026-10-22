# Datenschutz-Folgenabschätzung: KI-Telefonassistent

> **ENTWURF.** Vorausgefüllte Vorlage nach Art. 35 DSGVO für den Betrieb (Verantwortlicher) {{kunde.firma}}.
> {{auftritt.marke}} unterstützt als Auftragsverarbeiter (Art. 28 Abs. 3 lit. f DSGVO). Der Betrieb prüft,
> ergänzt die markierten Stellen und bewahrt die DSFA auf. Überprüfung: jährlich und bei wesentlichen Änderungen.

## 1 Schwellwertanalyse

Die DSFA ist erforderlich, wenn die Verarbeitung voraussichtlich ein hohes Risiko birgt. Kriterien nach den
Leitlinien WP 248 rev.01 und der DSK-Liste nach Art. 35 Abs. 4 DSGVO:

| Kriterium | erfüllt | Begründung |
| --- | --- | --- |
| Neue Technologien | ja | Sprachverarbeitung und Gesprächsführung durch ein KI-Sprachmodell |
| Daten schutzbedürftiger Personen | teilweise | Anrufende können ältere oder in einer Notlage befindliche Personen sein |
| Systematische Überwachung | nein | keine Beobachtung, Anrufende wählen selbst |
| Bewertung, Scoring | teilweise | Einstufung der Dringlichkeit, ohne Rechtswirkung (Art. 22 DSGVO nicht berührt, Freigabe durch Menschen) |
| Daten in großem Umfang | nein | ein Handwerksbetrieb, wenige Hundert Gespräche je Monat |
| Drittlandübermittlung | ja | Sprachmodell eines US-Anbieters |

Ergebnis: Zwei Kriterien sind erfüllt; die DSFA wird vorsorglich durchgeführt.

## 2 Systematische Beschreibung

- **Zweck:** Annahme von Anrufen außerhalb der Erreichbarkeit des Büros, Erfassung des Anliegens, Terminvorschlag,
  Erkennung von Notfällen, Weitergabe an das Büro zur Freigabe.
- **Ablauf:** Rufumleitung beim Telefonanbieter des Betriebs → SIP-Trunk ({{subunternehmer.carrier}}) → Server in
  Deutschland ({{subunternehmer.hoster}}) → Echtzeit-Spracherkennung und Sprachausgabe (Microsoft Azure, Frankfurt)
  → Sprachmodell (Anthropic, {{subunternehmer.llm_grundlage}}) → Vorgang in der Plattform → Freigabe durch das Büro.
- **Daten:** Rufnummer, Name, Objektadresse, Anliegen, Anlagenangaben, Gesprächstext und Zusammenfassung.
  Kein Gesprächsaudio wird gespeichert.
- **Betroffene:** Anrufende (Kunden, Interessenten, sonstige), Beschäftigte des Betriebs.
- **Rechtsgrundlage:** Art. 6 Abs. 1 lit. b DSGVO (Anbahnung und Durchführung von Aufträgen), im Übrigen lit. f
  (Erreichbarkeit des Betriebs). Information nach Art. 13 durch Ansage und Datenschutzerklärung des Betriebs.
- **Speicherdauer:** nach dem Löschkonzept (`plattform/recht/LOESCHKONZEPT.md`), Gesprächstexte mit kurzer Frist.

## 3 Notwendigkeit und Verhältnismäßigkeit

- Datenminimierung: Der Assistent fragt nur nach Name, Rückrufnummer, Objektadresse, Anliegen und Anlagentyp;
  Geburtsdaten, Gesundheits- und Zahlungsdaten werden nicht erfragt und nicht aufgegriffen.
- Transparenz: KI-Hinweis und Datenschutzhinweis zu Gesprächsbeginn; Verweis auf die Datenschutzerklärung.
- Wahlfreiheit: Anrufende können jederzeit einen Rückruf durch einen Menschen verlangen.
- Keine automatisierte Entscheidung mit Rechtswirkung: Termine, Angebote und Rechnungen werden von Menschen freigegeben.

## 4 Risiken und Maßnahmen

| Risiko | Eintritt | Schwere | Maßnahmen | Restrisiko |
| --- | --- | --- | --- | --- |
| Unbefugter Zugriff auf Gesprächsinhalte | gering | mittel | Mandantentrennung per Row-Level-Security, TOTP für Inhaber, Rollen, Verschlüsselung der Verbindungen und der Zweitsicherung | gering |
| Drittlandzugriff beim Sprachmodell | gering | mittel | Übermittlungsgrundlage {{subunternehmer.llm_grundlage}}, keine Nutzung zu Trainingszwecken, nur der Gesprächstext des laufenden Telefonats | gering |
| Falsche Dringlichkeitseinstufung, übersehener Notfall | mittel | hoch | Gasgeruch und Gefahr für Leib und Leben stets als Sicherheitsfall mit Verweis auf 112 bzw. Entstörungsdienst; Notdienstnummer; Freigabe durch Büro; Pilot-Kriterium K3 | gering |
| Anrufende erkennen die KI nicht | gering | mittel | Pflichtansage, ehrliche Bestätigung auf Nachfrage (Art. 50 KI-VO) | gering |
| Unerwünschte Gesundheitsangaben im Gesprächstext | mittel | mittel | keine Auswertung, kein Merkmal, kurze Löschfrist | gering |
| Fehlerhafte Erfassung führt zu Fehleinsatz | mittel | gering | Vorlesen der Adresse zur Kontrolle, Freigabe, Rückrufmöglichkeit | gering |
| Ausfall der Plattform | gering | mittel | Rückfallebene beim Carrier (Mailbox oder Mobilnummer), Monitoring | gering |

## 5 Ergebnis

Nach Umsetzung der Maßnahmen verbleibt ein geringes Risiko. Eine Konsultation der Aufsichtsbehörde nach
Art. 36 DSGVO ist nicht erforderlich.

Durchgeführt von: ______________________ · Datum: __________ · Freigabe Inhaber/in: ______________________
