# E-Mail-Vorlagen Kundenbeziehung

Platzhalter in eckigen Klammern beim Versand ersetzen. Signatur aus `dist-vorlagen/email-signatur.txt`.
Werbliche E-Mails nur mit Einwilligung oder im Rahmen von § 7 Abs. 3 UWG an Bestandskunden.

---

## 1 Nach dem Erstgespräch

**Betreff:** Unser Gespräch zu Telefon und Büro bei {{kunde.firma}}

Guten Tag [Anrede Nachname],

vielen Dank für das offene Gespräch. Ich fasse kurz zusammen, was ich mitgenommen habe:

- [Schmerz 1, z. B. etwa 15 Anrufe je Woche auf der Mailbox]
- [Schmerz 2, z. B. Rechnungen am Wochenende]

Vorgeschlagener nächster Schritt: [Pilot über acht Wochen im Paket {{paket.name}} / Demo vor Ort am Datum].
Im Anhang finden Sie den Einseiter. Passt Ihnen [Termin] für den nächsten Schritt?

Mit freundlichen Grüßen

---

## 2 Angebot

**Betreff:** Angebot {{angebot.nummer}}: {{auftritt.marke}} für {{kunde.firma}}

Guten Tag [Anrede Nachname],

anbei unser Angebot {{angebot.nummer}} über das Paket {{paket.name}}, gültig bis {{angebot.gueltig_bis}}, mit
Leistungsbeschreibung, AGB und Vereinbarung zur Auftragsverarbeitung. Zum Annehmen genügt eine Antwort auf diese
E-Mail mit „Angebot angenommen“ und die unterschriebene AVV.

Mit freundlichen Grüßen

---

## 3 Pilotstart

**Betreff:** Ihr Pilot startet am {{pilot.beginn}}

Guten Tag [Anrede Nachname],

wir freuen uns auf den Pilot. Damit wir zum {{pilot.beginn}} startklar sind, brauchen wir bis [Datum]:

1. den ausgefüllten Onboarding-Fragebogen (Anhang),
2. die unterschriebene Pilotvereinbarung und AVV,
3. eine Kunden- und Artikelliste als Excel oder CSV, falls vorhanden.

Den Termin für Einrichtung und Testanrufe schlage ich für [Datum, Uhrzeit] vor (etwa 90 Minuten).

Mit freundlichen Grüßen

---

## 4 Go-live

**Betreff:** {{auftritt.marke}} ist ab heute für Sie am Telefon

Guten Tag [Anrede Nachname],

die Abnahme ist erfolgt, die Rufumleitung ist aktiv. Das Wichtigste in Kürze:

- Freigaben: Büro-Oberfläche unter https://backoffice.{{auftritt.domain}}/app oder auf dem Handy.
- Störung: {{vertrag.support_telefon}} ({{vertrag.support_zeiten}}), dringend: {{vertrag.notfall_telefon}}.
- Rückfall: Rufumleitung beim Telefonanbieter ausschalten, dann klingelt es wie früher bei Ihnen.

Wir melden uns in einer Woche zur ersten Abstimmung.

Mit freundlichen Grüßen

---

## 5 Wochen- bzw. Monatsbericht

**Betreff:** {{auftritt.marke}}: Ihr Bericht für [Zeitraum]

Guten Tag [Anrede Nachname],

im [Zeitraum] hat der Assistent [n] Anrufe angenommen ([n] Notfälle an die Bereitschaft), daraus [n] Vorgänge
und [n] Termine. Verbraucht wurden [n] von {{paket.minuten}} Gesprächsminuten. Auffälligkeiten: [kurz].
Vorschlag zur Anpassung der Gesprächsregeln: [kurz oder „keiner“].

Mit freundlichen Grüßen

---

## 6 Störungsmeldung an den Kunden

**Betreff:** Störung des Telefonassistenten: [behoben / in Bearbeitung]

Guten Tag [Anrede Nachname],

seit [Uhrzeit] nimmt der Telefonassistent [keine / nur eingeschränkt] Anrufe an. Ihre Anrufe gehen in dieser Zeit
auf [Mailbox / Mobilnummer]. Ursache: [kurz]. [Behoben um Uhrzeit. / Nächste Information spätestens um Uhrzeit.]
Entschuldigen Sie die Unannehmlichkeit.

Mit freundlichen Grüßen

---

## 7 Meldung einer Datenschutzverletzung an den Verantwortlichen (AVV Ziffer 4.4, binnen 24 Stunden)

**Betreff:** Meldung nach Art. 33 DSGVO: Vorfall vom [Datum]

Guten Tag [Anrede Nachname],

wir informieren Sie als Verantwortlichen über eine Verletzung des Schutzes personenbezogener Daten:

- Art der Verletzung: [Beschreibung]
- Betroffene Kategorien und ungefähre Zahl: [Personen, Datensätze]
- Wahrscheinliche Folgen: [kurz]
- Ergriffene und vorgeschlagene Maßnahmen: [kurz]
- Ansprechpartner: {{vertrag.datenschutz_kontakt}}

Sie entscheiden über die Meldung an die Aufsichtsbehörde binnen 72 Stunden ab Kenntnis (Art. 33 Abs. 1 DSGVO).
Wir unterstützen Sie dabei.

Mit freundlichen Grüßen

---

## 8 Kündigungsbestätigung

**Betreff:** Bestätigung Ihrer Kündigung zum [Datum]

Guten Tag [Anrede Nachname],

wir bestätigen Ihre Kündigung zum [Datum]. Den Datenexport stellen wir Ihnen bis [Datum] bereit; danach löschen wir
Ihre Daten nach der Vereinbarung zur Auftragsverarbeitung und bestätigen die Löschung schriftlich. Bitte schalten Sie
zum Vertragsende die Rufumleitung bei Ihrem Telefonanbieter ab; gern machen wir das gemeinsam am Telefon.

Mit freundlichen Grüßen
