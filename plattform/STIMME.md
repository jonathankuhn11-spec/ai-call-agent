# Eigene Stimme für den Sprachagenten

Der Agent spricht mit einer synthetischen Stimme (TTS). Soll er mit der Stimme des Inhabers sprechen,
wird diese bei einem Anbieter **geklont** (Voice Cloning): aus Aufnahmen entsteht ein Stimmmodell, das
jeden beliebigen Text in dieser Stimme ausgibt. Der Agent selbst ändert sich nicht; nur `TTS_ANBIETER`
und `TTS_STIMME` in `.env` zeigen auf den Klon (`voice/agent.py`, `tts_erzeugen`).

## Anbieterwahl

| Anbieter | Verfahren | Deutsch | Latenz (erste Silbe) | Datenverarbeitung | Einschätzung |
| --- | --- | --- | --- | --- | --- |
| **ElevenLabs** (`elevenlabs`) | Instant Voice Clone (1–3 min Audio) oder Professional Voice Clone (30 min+, mehrere Stunden Training) | sehr gut, Modell `eleven_flash_v2_5` für Echtzeit | ca. 75–150 ms | USA/EU-Rechenzentren je nach Vertrag, AVV (DPA) verfügbar, Enterprise mit EU-Datenresidenz | schnellster Weg zur eigenen Stimme; für den Pilot empfohlen |
| **Cartesia** (`cartesia`) | Instant Clone (ab ca. 10 s) und Professional Clone, Modell Sonic | gut | ca. 40–90 ms | USA, DPA verfügbar | sehr geringe Latenz; Qualität im Deutschen vorab mit einem Satz aus der Branche prüfen |
| **Azure Custom Neural Voice** (`azure`) | Professional: Aufnahmestudio-Skript (300–2000 Sätze), Freigabeprozess von Microsoft (Antrag, Nachweis der Einwilligung); Lite: 20–50 Sätze (nur Test) | sehr gut | ca. 150–300 ms | EU-Region (`germanywestcentral`), AVV im Microsoft-Vertrag | DSGVO-seitig die sauberste Lösung, aber Freigabe und Aufwand von Wochen; für den Dauerbetrieb |
| Selbst gehostet (Chatterbox, MIT-Lizenz, mehrsprachig) | Zero-Shot-Clone aus wenigen Sekunden, GPU nötig | brauchbar | je nach GPU 200–500 ms | eigener Server | ohne Anbieter, aber eigener GPU-Betrieb und kein Support; XTTS, Fish Speech, F5-TTS scheiden wegen nichtkommerzieller Lizenzen aus |

Empfehlung: **ElevenLabs Professional Voice Clone** für den Pilot (wenige Tage bis zum Ergebnis),
parallel Antrag auf **Azure Custom Neural Voice**, wenn die Stimme dauerhaft und ausschließlich in der
EU verarbeitet werden soll. Beide lassen sich ohne Codeänderung umschalten.

## Aufnahmen vorbereiten

- **Umfang:** 30–60 Minuten eigene Sprache für einen Professional Clone (Instant Clone: 2–3 Minuten
  reichen für einen ersten Eindruck).
- **Inhalt:** normale Gesprächssprache aus dem Betriebsalltag, nicht vorgelesen klingend: Begrüßung,
  Terminabsprachen, Rückfragen, Beruhigung bei Störungen, Zahlen, Adressen, Uhrzeiten, Fachbegriffe
  (Therme, Brennwert, Zirkulationspumpe, Abgasmessung). Alle Satzarten: Fragen, Aussagen, Listen.
- **Technik:** ruhiger Raum ohne Hall, 20–30 cm Abstand zum Mikrofon (USB-Kondensator- oder gutes
  Headset-Mikrofon), 44,1 oder 48 kHz, 16 Bit oder besser, WAV oder FLAC, kein Rauschfilter, keine
  Musik, keine zweite Stimme, keine Versprecher stehen lassen (neu einsprechen).
- **Tonlage:** so sprechen, wie der Agent klingen soll: freundlich, ruhig, mittleres Tempo. Der Klon
  übernimmt Stimmung und Tempo der Aufnahmen.

## Rechtliches und Einwilligung

- Die Stimme ist ein personenbezogenes Datum (Art. 4 Nr. 1 DSGVO) und bei biometrischer Auswertung
  besonders geschützt (Art. 9). Jeder Anbieter verlangt eine **Einwilligungserklärung** des Sprechers;
  ElevenLabs und Azure prüfen sie technisch (gesprochener Einwilligungssatz) bzw. formal (Antrag).
- **AVV** mit dem Anbieter abschließen (Art. 28 DSGVO), Standort der Verarbeitung festhalten und in das
  Verzeichnis der Verarbeitungstätigkeiten (`recht/VVT.md`) aufnehmen.
- **Offenlegung:** Der Agent sagt zu Beginn, dass eine KI spricht (Art. 50 KI-VO, bereits in
  `voice/prompt.md`). Mit der geklonten Stimme des Inhabers bleibt das Pflicht; der Satz lautet dann
  sinngemäß „Sie sprechen mit dem digitalen Assistenten von <Betrieb>, der mit der Stimme von <Name>
  spricht“.
- Nur die eigene Stimme oder die eines Mitarbeiters mit schriftlicher Einwilligung klonen; Widerruf
  ermöglichen (Stimmmodell beim Anbieter löschen).

## Einrichtung Schritt für Schritt

1. Konto beim Anbieter anlegen, AVV annehmen, API-Schlüssel erzeugen.
2. Aufnahmen hochladen, Einwilligung abgeben, Klon erzeugen; die **Voice-ID** notieren.
3. In `.env` auf dem Server eintragen und den Voice-Agenten neu starten:

   ```
   # ElevenLabs
   TTS_ANBIETER=elevenlabs
   TTS_STIMME=<voice_id>
   ELEVEN_API_KEY=<Schlüssel>
   TTS_MODELL=eleven_flash_v2_5

   # Cartesia
   TTS_ANBIETER=cartesia
   TTS_STIMME=<voice id>
   CARTESIA_API_KEY=<Schlüssel>

   # Azure Custom Neural Voice (nach Freigabe und Deployment in Speech Studio)
   TTS_ANBIETER=azure
   TTS_STIMME=<Name der Custom Neural Voice>
   AZURE_TTS_ENDPOINT_ID=<Deployment-ID>
   AZURE_SPEECH_KEY=<Schlüssel>
   AZURE_SPEECH_REGION=germanywestcentral
   ```

   ```
   docker compose --profile voice up -d voice-agent
   ```

4. Probeanruf (`TELEFONIE.md`): Begrüßung, Terminabsprache mit Uhrzeit und Adresse, Störungsfall.
   Prüfen: Aussprache von Zahlen und Straßennamen, Betonung von Fragen, Latenz bis zur ersten Silbe
   (Ziel unter 1 s). Bei Auffälligkeiten einzelne Wörter im Prompt phonetisch ausschreiben oder
   Aufnahmen mit den betreffenden Lauten ergänzen und den Klon neu trainieren.
5. Dokumentieren: Anbieter, Voice-ID, Datum der Einwilligung, AVV, Speicherort der Aufnahmen
   (Aufnahmen nach dem Training nur noch als Sicherung, Zugriff beschränkt).

## Kosten (Größenordnung, aktuelle Preislisten prüfen)

ElevenLabs Creator/Pro-Tarife ab ca. 20–100 € je Monat inkl. Zeichenkontingent, Professional Clone im
Creator-Tarif enthalten; Cartesia ähnlich; Azure Custom Neural Voice: Training ab ca. 50 € je Stunde
Rechenzeit, Hosting je Modell ca. 4 € je Stunde, Synthese ca. 20–25 € je 1 Mio. Zeichen. Ein
Telefonat mit 2 Minuten Agentensprache entspricht etwa 1.500–2.000 Zeichen.
