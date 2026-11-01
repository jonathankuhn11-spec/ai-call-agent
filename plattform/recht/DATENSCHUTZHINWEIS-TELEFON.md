# Datenschutz am Telefon: Ansage, Informationspflichten, rechtliche Leitplanken

Entwurf, vor Einsatz prüfen lassen. Gilt für den Sprachassistenten (`voice/agent.py`, `voice/prompt.md`).

## 1 Ansagetext zu Gesprächsbeginn

Kurzfassung (empfohlen, vor dem ersten Satz des Assistenten, als TTS-Ansage oder im Prompt):

> „Guten Tag, hier ist der digitale Assistent von [Betrieb]. Ich nehme Ihr Anliegen auf und schlage
> Ihnen einen Termin vor; Ihre Angaben werden dafür in Text umgewandelt und gespeichert, das Gespräch
> wird nicht aufgezeichnet. Mehr dazu unter [Domain]/datenschutz. Wie kann ich helfen?“

Langfassung für die Datenschutzseite des Betriebs (Art. 13 DSGVO, Pflichtangaben):

- Verantwortlicher: [Betrieb, Anschrift, Telefon, E-Mail]; Datenschutzbeauftragte/r: [falls benannt].
- Zwecke und Rechtsgrundlagen: Annahme des Anliegens, Terminvergabe, Vorbereitung des Einsatzes
  (Art. 6 Abs. 1 lit. b DSGVO); Erreichbarkeit und Qualitätssicherung (lit. f); Abrechnung und
  Aufbewahrung (lit. c).
- Verarbeitung: Das Gespräch wird in Echtzeit in Text umgewandelt (Spracherkennung) und von einem
  Sprachmodell verstanden und beantwortet; die Antwort wird als Sprache ausgegeben. Es entsteht ein
  Transkript und eine Zusammenfassung. Eine Audioaufzeichnung findet nicht statt.
- Empfänger: Dienstleister für Betrieb der Software (Auftragsverarbeiter in Deutschland),
  Spracherkennung und Sprachausgabe (Microsoft Azure, Frankfurt), Sprachmodell ([Anthropic];
  Übermittlung in Drittland auf Grundlage von [Standardvertragsklauseln / Data Privacy Framework]).
- Speicherdauer: Transkript 90 Tage, Zusammenfassung mit dem Vorgang, Rechnungen 10 Jahre; Gespräche
  ohne Auftrag 30 Tage.
- Rechte: Auskunft, Berichtigung, Löschung, Einschränkung, Widerspruch, Datenübertragbarkeit,
  Beschwerde bei einer Aufsichtsbehörde ([zuständige Landesbehörde]).
- Keine ausschließlich automatisierte Entscheidung mit rechtlicher Wirkung (Art. 22): Terminbestätigung,
  Aufträge und Rechnungen werden durch Mitarbeitende freigegeben; ein Sicherheitsfall (Gasgeruch) führt
  zu einer Notfallansage, nicht zu einer Entscheidung über den Anrufenden.

## 2 Rechtliche Leitplanken und wie der Code sie einhält

| Thema | Regel | Umsetzung |
| --- | --- | --- |
| Aufzeichnung (§ 201 StGB) | Das nichtöffentlich gesprochene Wort darf ohne Einwilligung nicht aufgezeichnet werden. Echtzeit-Transkription zur Erfüllung des Anliegens ist keine Aufzeichnung des Tons, aber eine Verarbeitung, über die informiert werden muss. | Kein Audio wird gespeichert; Transkript mit Frist. Soll Audio je gespeichert werden: ausdrückliche Einwilligung einholen und dokumentieren (`Kunde.einwilligungen["aufzeichnung"]`). |
| Transparenz (Art. 13) | Information bei Erhebung, in verständlicher Sprache; am Telefon Kurzinformation plus Verweis. | Ansage oben; Datenschutzseite des Betriebs (Außenauftritt, PR #3). |
| Offenlegung des KI-Einsatzes | Anrufende müssen erkennen können, dass sie mit einem automatisierten System sprechen (Transparenzpflicht der KI-Verordnung, Art. 50 Abs. 1, anwendbar ab August 2026; bis dahin Gebot der Fairness). | Der Assistent stellt sich als digitaler Assistent vor und bestätigt auf Nachfrage, dass er eine KI ist (`voice/prompt.md`). |
| Notfälle | Keine Verzögerung bei Gefahr für Leib und Leben. | Deterministische Regel: Gasgeruch/CO → Anweisung „Fenster öffnen, Gebäude verlassen, 112 bzw. Störungsdienst des Netzbetreibers“, kein Terminverkauf (`core/regeln.py`). |
| Einwilligung für SMS | Werbliche und informierende SMS nur mit Einwilligung (§ 7 UWG, Art. 6 Abs. 1 lit. a). Terminbestätigung an Vertragskunden ist nach lit. b vertretbar; die Plattform verlangt dennoch eine dokumentierte Einwilligung, um Streit zu vermeiden. | `SmsAdapter` sendet nur mit `einwilligungen["sms"]`. |
| Minderjährige, Dritte | Der Assistent nimmt Anliegen von Mietern oder Angehörigen auf, legt aber keine Verträge an; Freigabe durch das Büro. | Freigabeklassen B/C. |
| Datensparsamkeit | Nur erfragen, was für Einsatz und Rückruf nötig ist (Name, Rückrufnummer, Adresse des Objekts, Anliegen, Anlagentyp). | Werkzeuge des Agenten haben nur diese Felder (`voice/werkzeuge.py`). |
| Übermittlung ins Drittland | Sprachmodell-Anbieter in den USA nur mit geeigneten Garantien; EU-Endpunkt bevorzugen. | `LLM_BASE_URL` in `.env` erlaubt EU-Gateway; Datenübermittlung dokumentiert in AVV Anlage 2. |

## 3 Prompt-Bausteine (Auszug für `voice/prompt.md`)

- „Du bist der digitale Assistent von {betrieb}. Sage im ersten Satz, dass du ein digitaler Assistent
  bist. Wenn jemand fragt, ob er mit einer Maschine spricht, bestätige das ehrlich.“
- „Frage nur nach Angaben, die für den Einsatz nötig sind. Frage nicht nach Geburtsdatum,
  Gesundheitszustand oder Zahlungsdaten.“
- „Biete auf Wunsch an, dass ein Mensch zurückruft (Werkzeug rueckruf_vormerken).“
- „Verweise bei Fragen zum Datenschutz auf {domain}/datenschutz und auf das Büro.“
