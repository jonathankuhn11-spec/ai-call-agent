# Änderungen

## 1.1.0 (17.10.2026)
- **Kanal-Fallback** (`integrations/nachfass.py`): nie erreichte Leads bekommen per SMS oder WhatsApp einen
  Buchungslink, mit Einwilligung je Kanal, Sperrliste, Sperrfenster und Zustellfenster in Ortszeit; Ereignis
  `nachricht.nachfassen`, Kennzahl Rückgewinnungsquote, Löschung nach Art. 17 inklusive
- **Deployment-Case** (`docs/deployment-case-telli.md`): Enterprise-Rollout end to end mit Business Case
- **Code-Hygiene**: Ruff-Regeln in `pyproject.toml` (Bugbear, Vereinfachungen, Importreihenfolge), Abdeckung
  95 % mit CI-Gate bei 90 %, Makefile
- README mit Reviewer-Überblick, Schnellstart ohne Ollama und englischer Zusammenfassung

## 1.0.0 (11.10.2026)
- Zwei Betriebsarten (geführt, frei), UAT 49 von 50, Sprach-Layer, Integrationsschicht nach den
  telli-Schnittstellen, zwei Use Cases, Playbook, Docker, Lasttest, Abnahme von außen
