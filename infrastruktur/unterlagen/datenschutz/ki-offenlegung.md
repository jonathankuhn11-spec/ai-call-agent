# KI-Offenlegung und KI-Kompetenz

> **ENTWURF zur anwaltlichen Prüfung.** Textbausteine für den Betrieb {{kunde.firma}} und für {{auftritt.marke}}.
> Grundlage: Verordnung (EU) 2024/1689 (KI-Verordnung), Art. 50 Abs. 1 (Transparenz gegenüber Personen,
> die mit einem KI-System interagieren, anwendbar seit 2. August 2026) und Art. 4 (KI-Kompetenz, seit 2. Februar 2025).

## 1 Ansage zu Gesprächsbeginn (Pflicht)

> „Guten Tag, Sie sprechen mit dem digitalen KI-Assistenten von {{kunde.firma}}. Ich nehme Ihr Anliegen auf und
> gebe es an das Büro weiter. Das Gespräch wird nicht aufgezeichnet, aber zur Bearbeitung in Text umgewandelt.
> Mehr dazu in der Datenschutzerklärung von {{kunde.firma}}. Wie kann ich Ihnen helfen?“

Verbindlich: Der Hinweis auf das KI-System steht im ersten Satz, bevor Angaben erfragt werden. Fragt jemand,
ob er mit einem Menschen spricht, bestätigt der Assistent wahrheitsgemäß, dass er ein KI-System ist, und bietet
einen Rückruf an (umgesetzt in `plattform/voice/prompt.md`).

## 2 Baustein für die Datenschutzerklärung des Betriebs

> **Telefonassistent.** Wenn wir Ihren Anruf nicht selbst annehmen können, nimmt ihn ein KI-gestützter
> Telefonassistent entgegen, den {{anbieter.bezeichnung}} in unserem Auftrag betreibt (Art. 28 DSGVO). Er erfasst
> Ihr Anliegen, Ihren Namen, Ihre Rückrufnummer und die Adresse des Objekts und schlägt einen Termin vor, den
> unser Büro bestätigt. Das Gespräch wird nicht als Audio gespeichert; die Spracherkennung erfolgt in Echtzeit in
> einem Rechenzentrum in Deutschland, die Gesprächsführung übernimmt ein Sprachmodell der Anthropic PBC (USA,
> Übermittlung auf Grundlage von: {{subunternehmer.llm_grundlage}}). Rechtsgrundlage ist Art. 6 Abs. 1 lit. b
> DSGVO, soweit es um Ihren Auftrag geht, im Übrigen lit. f (unsere Erreichbarkeit). Der Gesprächstext wird nach
> kurzer Frist gelöscht, die Auftragsdaten nach den gesetzlichen Aufbewahrungsfristen. Sie können jederzeit
> verlangen, mit einem Menschen zu sprechen.

## 3 Hinweis auf Website und in E-Mails des Betriebs (empfohlen)

> „Außerhalb unserer Bürozeiten nimmt ein KI-Assistent Ihren Anruf entgegen. Notfälle leitet er an unsere
> Bereitschaft weiter. Bei Gasgeruch: Gebäude verlassen und 112 bzw. den Entstörungsdienst Ihres Netzbetreibers anrufen.“

## 4 Vertragsklausel (AGB § 3 Abs. 2 und 3)

Bereits in den AGB von {{auftritt.marke}} enthalten: Kennzeichnung als KI-System, Ergebnisse als Vorschläge,
Freigabe von Handlungen mit Außenwirkung.

## 5 KI-Kompetenz (Art. 4 KI-VO)

Betreiber eines KI-Systems sorgen dafür, dass ihr Personal über ausreichende KI-Kompetenz verfügt. Nachweis
durch die Einweisung nach `onboarding/schulung.md`, Abschnitt 1:

| Name | Rolle | Datum der Einweisung | Inhalt bestätigt (Unterschrift) |
| --- | --- | --- | --- |
| | | | |
| | | | |
| | | | |

Inhalte: Funktionsweise und Grenzen des Assistenten, typische Fehler (Verhörer bei Namen und Straßen,
Fehleinschätzung der Dringlichkeit), Pflicht zur Prüfung vor Freigabe, Umgang mit Beschwerden, Rückfallebene.
