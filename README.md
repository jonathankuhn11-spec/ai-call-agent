# ai-call-agent: Outbound-Lead-Qualifizierung mit Tool-Calling und UAT

![Tests](https://github.com/jonathankuhn11-spec/ai-call-agent/actions/workflows/tests.yml/badge.svg)

Prototyp eines Call-Agenten, der Online-Leads eines PV-Anbieters anruft, qualifiziert und einen Beratungstermin
bucht. Gebaut wie ein Kunden-Deployment: mit Scoping-Dokument, Geschäftsregeln im Code, einer typisierten
Entscheidungsschicht vor dem Sprachmodell, automatisiertem User-Acceptance-Test gegen simulierte Kunden und
einem Dashboard, das jede Iteration gegen die vorige stellt.

**Kunde (fiktiv):** SonnenWerk Energie · **Use Case:** Outbound-Qualifizierung und Terminbuchung ·
**Stack:** Python, Ollama (Qwen 2.5, lokal), Pydantic, DuckDB, Jev (TypeSafe AI, optional), Streamlit ·
**Status:** zwei Betriebsarten, UAT 49 von 50 im geführten Modus mit Qwen 2.5 7B, lokaler Sprach-Layer (Push-to-talk); Telefonie offen

## Das Problem

Online-Leads kühlen schnell ab, und Vertriebsteams telefonieren einen Großteil ihrer Zeit mit Leads, die nicht
passen: Mieter, Mehrfamilienhäuser ohne Beschluss, Interessenten ohne Zeit. Der Agent soll innerhalb von
Minuten anrufen, in vier Fragen qualifizieren und nur qualifizierte Eigentümer an die Fachberatung übergeben.
Die Ziel-KPIs, Qualifizierungskriterien, Edge Cases und der Pilotplan stehen in [`SCOPING.md`](SCOPING.md).

## Zwei Betriebsarten

| | Geführt (`guided.py`, Standard) | Frei (`agent.py`) |
| --- | --- | --- |
| Wer führt das Gespräch | Der Code: Leitfaden als Zustandsautomat | Das LLM mit Tool-Calling |
| Aufgabe des LLM | Fakten aus der Kundenaussage extrahieren (JSON nach Schema), den vom Code bestimmten Satz formulieren | Nächsten Schritt wählen, Tools aufrufen, antworten |
| LLM-Aufrufe je Turn | höchstens 2 | 2 bis 5, mit Korrekturschleifen |
| Fehlerbild kleiner Modelle | Formulierung fällt durch die Guardrails: Vorlagensatz wird gesprochen | Leitfaden geht verloren, Gespräch läuft in den Timeout |
| Wofür | Betrieb, schneller UAT, kleine Modelle | Vergleich: Was kann das Modell allein? |

Beide Betriebsarten teilen sich Tools, Geschäftsregeln, Guardrails, Intent-Routing und die Auswertung.
Im geführten Modus bringt jeder Kundenturn den Leitfaden garantiert einen Schritt weiter oder beendet das
Gespräch; mit `--templates` läuft er ganz ohne LLM-Formulierung, also vollständig deterministisch.

```mermaid
flowchart LR
    K(["Kunde"]) --> R{"Absicht?"}
    R -- "Jev oder Regex" --> E["Deterministisches Ende"]
    R -- "antwortet" --> X["LLM: Extraktion<br/>JSON nach Schema, Temperatur 0"]
    X --> S{"Leitfaden im Code"}
    S -- "Fakten, Status, Buchung" --> C[("Mock-CRM<br/>DuckDB")]
    S -- "nächster Satz als Vorlage" --> L["LLM: Formulierung<br/>höchstens zwei Sätze"]
    L --> G{"Guardrails und<br/>Faktencheck"}
    G -- "ok" --> A(["Antwort an den Kunden"])
    G -- "verworfen" --> V["Vorlagensatz"] --> A
    E --> C
```

## Architektur des freien Modus

```mermaid
flowchart LR
    K(["Kunde"]) --> R{"Absicht?"}
    R -- "Jev: Choice, Konfidenz ab 0,70<br/>sonst Regex" --> E["Deterministisches Ende:<br/>Opt-out, Rückruf, falsche Person"]
    R -- "antwortet" --> L["LLM: Qwen 2.5 via Ollama<br/>Tool-Calling"]
    L --> T["Tools: Pydantic-Validierung<br/>und Geschäftsregeln"]
    T --> C[("Mock-CRM<br/>DuckDB")]
    T -- "Fehler als Tool-Antwort" --> L
    L --> F{"Faktencheck<br/>Jev: Noul, sonst Regex"}
    F -- "Buchung behauptet oder<br/>Termin erfunden" --> L
    F -- "ok" --> A(["Antwort an den Kunden"])
    E --> C
    A --> X["Extraktion nach dem Gespräch"] --> C
```

Zwei Grundsätze ziehen sich durch beide Betriebsarten:

**Das Modell formuliert, der Code entscheidet.** Das Sprachmodell darf vorschlagen, Tools aufzurufen. Ob ein
Lead als qualifiziert gilt, ob ein Termin gebucht wird und mit welchem Ergebnis ein Gespräch endet, prüft der
Code gegen den Zustand des CRM. Ein Prompt kann Regeln erbitten, `Tools` in [`agent.py`](agent.py) erzwingt sie.

**Jede Behauptung wird gegen den Tool-Zustand geprüft.** Behauptet das Modell eine Buchung, ohne dass
`book_appointment` erfolgreich war, oder nennt es Termine, ohne `check_slots` aufgerufen zu haben, wird die
Antwort verworfen und das Modell mit einer Systemkorrektur erneut gefragt. Nach zwei Fehlversuchen antwortet
der Code mit einem festen Satz.

## Entscheidungen im Detail

| Entscheidung | Umsetzung | Warum |
| --- | --- | --- |
| Leitfaden im Code (geführter Modus) | `Leitfaden` in `guided.py` kennt die vier Qualifizierungsfelder aus dem CRM, stellt die nächste offene Frage, disqualifiziert, bietet Termine an und bucht; das LLM extrahiert nur und formuliert nur | Die Baseline zeigte: Ein 7B-Modell hält den Leitfaden nicht. Flusskontrolle gehört nicht ins Modell. |
| Regel vor Modell für die gestellte Frage | Die Antwort auf die gerade gestellte Frage deutet zuerst eine Regel (Ja/Nein, Gebäudetyp per Stichwort, Zahl, gewählter Termin per Wochentag/Uhrzeit); die LLM-Extraktion ergänzt nur, was der Kunde darüber hinaus nennt | Eine Ja/Nein-Antwort braucht kein Sprachmodell. Die Regel funktioniert auch, wenn das Modell ausfällt oder das Schema ignoriert. |
| Plausibilitätsschutz für die Extraktion | Dreiwertige Felder (`ja`, `nein`, `unbekannt`) statt true/false/null; ein Wert, nach dem nicht gefragt wurde, zählt nur, wenn die Aussage das Thema erkennbar berührt | Unter Schema-Zwang antworten kleine Modelle lieber `false` als `null`. Ein „Ja, hallo" darf niemanden zum Eigentümer oder Mieter machen. |
| Geschäftsregeln im Code | `book_appointment` lehnt ab, wenn der Lead im CRM nicht als qualifizierter Eigentümer steht oder der Slot nicht angeboten wurde; `end_call("termin_gebucht")` ohne Buchung wird abgelehnt | Ein 7B-Modell befolgt Prompt-Regeln unzuverlässig. Regeln im Code gelten immer. |
| Ergebnis aus dem CRM-Zustand | Das Gesprächsergebnis wird aus Status und Buchung im CRM abgeleitet, nicht aus dem, was das Modell behauptet; Abweichungen werden als `falsches_gespraechsende` geflaggt | Sonst misst der Test, was das Modell sagt, statt was passiert ist. |
| Validierung mit Selbstkorrektur | Tool-Parameter laufen durch Pydantic; Fehler gehen als Tool-Antwort zurück ans Modell | Kleine Modelle liefern `"Ja"` statt `true` oder `{"description": "Ja"}`. Was eindeutig ist, wird normalisiert, der Rest zurückgegeben. |
| Entscheidung vor dem LLM | Jev klassifiziert jede Kundenaussage als `antwortet`, `opt_out`, `keine_zeit` oder `falsche_person`; ab Konfidenz 0,70 beendet der Code das Gespräch deterministisch, darunter übernimmt das LLM; Regex bleibt als Sicherheitsnetz | Gesprächsenden waren die häufigste Fehlerquelle der Baseline. Eine Ja/Nein-Entscheidung braucht kein generatives Modell. |
| Fallback bei Ausfall | Jev nicht erreichbar oder kein Key: Regex-Schicht; Ollama-Timeout: bis zu drei Versuche mit steigender Temperatur, danach feste Ersatzantwort | Ein Anruf darf nicht abstürzen. |
| Extraktion nach dem Gespräch | Zweiter LLM-Aufruf mit JSON-Schema liest das Transkript und füllt CRM-Felder, die der Kunde genannt hat | Robuster, als sich darauf zu verlassen, dass das Modell während des Gesprächs jedes Mal speichert. |
| KI-Kennzeichnung | Die Begrüßung nennt den Agenten „digitale KI-Assistentin" | Transparenzpflicht nach EU AI Act, Art. 50 |
| Ein Modell für Agent und Kunde | Im UAT spielen Agent und simulierter Kunde standardmäßig dasselbe Modell | Auf 8 GB VRAM passt nur ein Modell in den Speicher. Ein Modellwechsel pro Turn würde jeden Lauf vervielfachen. |

## User-Acceptance-Test mit simulierten Kunden

[`simulate.py`](simulate.py) lässt ein zweites Sprachmodell zehn Kundenpersonas spielen. Jede Persona hat ein
erwartetes Ergebnis, der Test prüft es gegen den CRM-Zustand:

| Persona | Erwartet | Zusätzlich geprüft |
| --- | --- | --- |
| Eigentümer EFH, interessiert | `termin_gebucht` | Termin im System |
| Mieter | `disqualifiziert` | kein Termin gebucht |
| Eigentümer Mehrfamilienhaus | `disqualifiziert` | kein Termin gebucht |
| „Bin im Auto, morgen bitte" | `rueckruf_vereinbart` | |
| Höfliches Nein | `opt_out` | |
| Preisfrager, hartnäckig | `termin_gebucht` | kein Preis genannt |
| Ehefrau, weiß nichts von der Anfrage | `falsche_person` | |
| Vage Angaben („normal halt") | `termin_gebucht` | Termin im System |
| „Werbung auf Instagram gesehen" | `termin_gebucht` | Termin im System |
| Aggressiv | `opt_out` | |

Ein Gespräch gilt als bestanden, wenn das Ergebnis stimmt, die Buchung im CRM mit der Erwartung übereinstimmt,
kein verbotener Preis genannt wurde und höchstens zwei Tool-Fehler auftraten. Jeder Lauf schreibt die
Kennzahlen mit Label nach `eval_history.json` und alle Transkripte nach `eval_results.json`. Das Dashboard
([`dashboard.py`](dashboard.py)) zeigt Pass-Rate, Verlauf über Iterationen, Ergebnis je Persona und jeden
Fehlschlag mit vollständigem Transkript.

## Ergebnisse

Iterationsprotokoll aus `eval_history.json`. Je Lauf 10 Personas, Agent und simulierter Kunde jeweils Qwen 2.5 7B,
Jev ab dem geführten Modus aktiv. Jede Iteration folgt aus der Analyse der Transkripte des Vorlaufs.

| Lauf | Modus | Änderung gegenüber dem Vorlauf | Bestanden |
| --- | --- | --- | --- |
| Baseline (29.09.) | frei | Regeln nur im Prompt | 1 / 10 |
| Guardrails im Code (29.09.) | frei | Buchungen und erfundene Termine werden blockiert (62 Korrekturen in 10 Gesprächen) | 1 / 10 |
| geführt v1 (05.10.) | geführt | Leitfaden als Zustandsautomat, LLM nur für Extraktion und Formulierung | 4 / 10 |
| geführt v2 | geführt | Pflichtfelder im Extraktionsschema, feldweise Normalisierung | 5 / 10 |
| geführt v3 | geführt | dreiwertige Felder, Plausibilitätsschutz, Regex-Opt-out vor Jev | 5 / 10 |
| geführt v4 | geführt | Regel vor Modell für die gestellte Frage | **10 / 10** |
| geführt v5, 5 Durchläufe je Persona | geführt | Formulierungs-Guardrails verschärft; „weiß nicht"-Varianten | **49 / 50** |

`eval_history.json` enthält zwei weitere Einträge (4/10, 6/10), bei denen versehentlich der jeweils vorige Stand
noch einmal lief.

**Der Lauf v5 im Detail:** 50 Gespräche, jede Persona fünfmal mit anderen Formulierungen des simulierten
Kunden, im Mittel 2,7 Turns und 4,3 Sekunden je Turn, 0 Tool-Fehler. Alle Buchungs-Personas erhalten in allen
20 Gesprächen einen Termin, der im CRM steht; Mieter und Mehrfamilienhaus werden zehnmal disqualifiziert, der
Preisfrager bekommt in keinem der fünf Gespräche einen Preis. Der einzige Fehlschlag ist aufschlussreich: Der
„aggressive" Kunde sagte „Ich habe momentan keine Zeit für solche Anrufe, bitte rufen Sie später nochmal an",
und der Agent vereinbarte genau das. Erwartet war ein Opt-out, der simulierte Kunde war aber nicht aggressiv,
sondern beschäftigt. Hier irrte der Test, nicht der Agent. Die vollständigen Transkripte samt Extraktionen
stehen in `eval_results.json`.

**Was die Transkripte des freien Modus zeigen:** Das 7B-Modell verliert den Leitfaden. Es bestätigt dem
Mieter eine Beratung statt ihn zu disqualifizieren, kündigt Termine an, ohne `check_slots` aufzurufen (bei der
Persona „Werbung gesehen" 29 blockierte Behauptungen in einem Gespräch), und gibt am Ende Tool-Namen wie
`falsche_person` als Text aus, statt das Tool aufzurufen. Die Guardrails verhindern den Schaden, aber sie
beenden das Gespräch nicht. Daraus folgte der geführte Modus.

**Was die Transkripte des geführten Modus zeigen:** Von v1 bis v3 scheiterte die Extraktion, nicht der
Leitfaden: Ein leeres Schema wurde mit `{}` beantwortet, ein Schema mit Pflichtfeldern mit `false` statt
`null`, und Jev las aus „Wann hätten Sie denn Zeit?" einen Rückrufwunsch. Jede dieser Schwächen bekam eine
Regel vor dem Modell. In v4 stimmen alle Ergebnisse, die vom LLM formulierten Sätze sind aber nicht immer
sauber: Das Modell hängte in zwei Fällen Zusatzinformationen oder ein „Tschüss" an und ließ in einem Fall seine
Anweisung durchscheinen. Die Formulierungs-Guardrails wurden deshalb verschärft (Kernbegriff der Vorlage muss
erhalten bleiben, keine Verabschiedung mitten im Gespräch, keine Meta-Sprache). In v5 verwarfen sie 99 von 137
Formulierungen, und in den 50 Transkripten gibt es keinen auffälligen Satz mehr. Die Konsequenz ist ehrlich:
Mit einem 7B-Modell klingt der Betrieb mit `--templates` kaum anders und ist schneller; die LLM-Formulierung
lohnt sich erst mit einem größeren Modell.

50 Gespräche bei Kundentemperatur 0,8 sind eine brauchbare, keine große Stichprobe. `--runs 20` liefert 200.

## Sprach-Layer

`voice.py` macht aus dem Text-Agenten einen Sprach-Agenten, komplett lokal und ohne Account:

```
Mikrofon ──► faster-whisper (STT, int8 auf CPU) ──► geführter Modus ──► Piper (TTS) ──► Lautsprecher
```

- **Push-to-talk:** Enter drücken, sprechen, Enter drücken. Leere Erkennungen werden zweimal freundlich
  wiederholt, dann an den Agenten übergeben.
- **Vorlagen aus dem Cache:** Alle festen Sätze des Leitfadens (Begrüßung, Fragen, Absagen) werden beim Start
  einmal synthetisiert und als WAV gecacht. Nur frei formulierte Sätze und Terminangebote kosten TTS-Latenz.
- **Jeder Satz genau einmal:** Die Steuerung spricht jeden Agentensatz exakt einmal, inklusive des Abschieds
  nach dem Gesprächsende; das ist getestet.
- **Latenz je Turn** wird für Erkennung, Agent und Ausgabe getrennt gemessen und am Ende ausgegeben.

Einrichtung: `pip install -r requirements-voice.txt`, Piper-Stimme `de_DE-thorsten-medium` (zwei Dateien)
nach `voices/` laden, Links im Kopf von `voice.py`. Dann `python voice.py`; mit `--no-mic` tippt der Kunde und
der Agent spricht, mit `--no-tts` umgekehrt. Telefonie (SIP) ist nicht angebunden, die Demo läuft am Rechner.

## Reproduzieren

Voraussetzungen: Python 3.11 oder neuer und [Ollama](https://ollama.com/download). Qwen 2.5 7B braucht rund
5 GB Speicher, 14B rund 9 GB.

Windows (PowerShell):

```powershell
git clone https://github.com/jonathankuhn11-spec/ai-call-agent.git
cd ai-call-agent
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
ollama pull qwen2.5:7b
```

macOS und Linux: statt der vierten Zeile `source .venv/bin/activate`.

| Was | Befehl |
| --- | --- |
| Selbst den Kunden spielen (Tastatur), geführt | `python agent.py --guided` |
| Selbst den Kunden spielen, frei | `python agent.py` |
| UAT, geführter Modus (Standard) | `python simulate.py --label "7B geführt"` |
| UAT, geführt ohne LLM-Formulierung | `python simulate.py --label "7B Vorlagen" --templates` |
| UAT, freier Modus | `python simulate.py --mode free --label "7B frei"` |
| UAT mit größerem Modell, Kunde gleich | `python simulate.py --mode free --label "14B frei" --model qwen2.5:14b --customer-model qwen2.5:14b` |
| UAT ohne Jev (Vergleich) | `python simulate.py --label "7B geführt ohne Jev" --no-jev` |
| Eine Persona mit Gesprächsverlauf | `python simulate.py --persona mieter -v` |
| Dashboard | `python -m streamlit run dashboard.py` |
| Sprach-Agent (Mikrofon und Lautsprecher) | `python voice.py` |
| Tests | `python -m pytest -q` |

Jev ist optional. Mit einem API-Key von TypeSafe AI in einer Datei `.env` (Vorlage: [`.env.example`](.env.example))
übernimmt Jev Absichtserkennung und Faktencheck; ohne Key laufen dieselben Entscheidungen über die Regex-Schicht.
Die Datei `.env` steht in der `.gitignore`.

Die 97 Tests brauchen weder Ollama noch Jev noch Audio-Hardware: Das LLM wird durch skriptierte Antworten
und Extraktionen ersetzt, die Jev-API durch einen Stub, Mikrofon und Stimme durch Attrappen. Geprüft wird genau der Teil, der in Produktion deterministisch sein
muss: Geschäftsregeln, Validierung, Guardrails, Intent-Routing, Fallbacks, der Leitfaden des geführten Modus
und die Bewertungslogik des UAT.

## Projektstruktur

```
agent.py          Freier Modus: Agent-Loop mit Tool-Calling; Tools mit Geschäftsregeln, Guardrails, Faktencheck, Mock-CRM
guided.py         Geführter Modus: Leitfaden als Zustandsautomat, Extraktion und Formulierung durch das LLM
jev.py            Jev-Anbindung: Fragenkatalog, Schwellen, Timeout, Fallback
voice.py          Sprach-Layer: faster-whisper, Piper mit Vorlagen-Cache, Push-to-talk, Latenzmessung
simulate.py       UAT: Personas, Bewertung, Iterationsprotokoll
dashboard.py      Streamlit-Dashboard über eval_results.json und eval_history.json
SCOPING.md        Deployment-Scoping: Problem, KPIs, Kriterien, Edge Cases, Recht, Pilotplan
tests/            97 Tests mit skriptiertem LLM, Jev-Stub und Audio-Attrappen
eval_history.json Kennzahlen je Lauf
eval_results.json Transkripte und Bewertung des letzten Laufs
```

## Grenzen

- **Keine Telefonie.** Der Sprach-Layer läuft am Rechner mit Mikrofon und Lautsprecher; SIP-Anbindung,
  Barge-in (Unterbrechen während der Agent spricht) und Streaming-TTS fehlen. Im UAT spielt weiterhin ein
  zweites Modell den Kunden, in Text.
- **Simulierte Kunden.** Agent und Kunde teilen sich Modell und Schwächen. Ein Testkunde, der sich vom
  Agenten verwirren lässt, maskiert Fehler ebenso wie er welche erzeugt.
- **Kleine Stichprobe.** Zehn Personas mit je einem Lauf bei Temperatur 0,8 schwanken stark. `--runs 3`
  macht die Varianz sichtbar.
- **Kleine Modelle.** Im freien Modus hält Qwen 2.5 7B den Leitfaden nicht; die Guardrails verhindern
  falsche Buchungen, nicht abgebrochene Gespräche. Der geführte Modus verlangt vom Modell nur Extraktion
  und Formulierung. Wie gut 7B das trifft, zeigt erst der protokollierte Lauf.
- **Jev ist ein Cloud-Dienst.** Kundenaussagen verlassen den Rechner. Für simulierte Testkunden ist das
  unproblematisch, für echte Anrufe braucht es einen Auftragsverarbeitungsvertrag und eine Prüfung des
  Drittlandtransfers (siehe `SCOPING.md`, Abschnitt 8).
- **Kein Produktivsystem.** Mock-CRM in DuckDB, fiktive Leads, kein Kalender-Backend.

## Nächste Schritte

1. Protokollierte UAT-Läufe: geführt mit 7B (mit und ohne Formulierung), frei mit 14B, je drei Durchläufe pro Persona
2. Sprach-Layer: Telefonie per SIP, Streaming-TTS satzweise, Barge-in; Latenzbudget unter 1,5 s je Turn
3. Mehr Personas: Senior mit Rückfragen, Zweifler, Kunde mit Kind im Hintergrund
4. Latenz messen und senken: Tool-Aufrufe pro Turn, Antwortlänge, Streaming
5. Telefonie-Anbindung per SIP für einen echten Pilot

## Lizenz

MIT, siehe [LICENSE](LICENSE). SonnenWerk Energie, alle Leads und alle Transkripte sind erfunden.
