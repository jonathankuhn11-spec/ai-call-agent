# Kundenunterlagen: alles für den ersten Kunden

Vertragswerk, Datenschutz, Vertrieb, Onboarding und Betrieb. Sämtliche Unterlagen werden aus denselben Stammdaten
erzeugt wie Website, Impressum und Einseiter, damit Anbieterangaben nie auseinanderlaufen. Rechtstexte sind als
**Entwurf zur anwaltlichen Prüfung** gekennzeichnet und vor dem ersten Einsatz einmal prüfen zu lassen.

## Erzeugen

```bash
cp infrastruktur/unterlagen/geschaeftsdaten.vorlage.toml infrastruktur/unterlagen/geschaeftsdaten.toml   # einmalig, ausfüllen
cp infrastruktur/unterlagen/kunden/vorlage.toml infrastruktur/unterlagen/kunden/<betrieb>.toml          # je Betrieb, ausfüllen
python infrastruktur/build.py --unterlagen --kunde infrastruktur/unterlagen/kunden/<betrieb>.toml --pdf
```

Ergebnis: `infrastruktur/dist-unterlagen/<betrieb>/` mit allen Dokumenten, Angebot und Rechnung zusätzlich als PDF.
Offene Werte erscheinen als `[OFFEN: …]`. `geschaeftsdaten.toml`, echte Kundendateien und `dist-unterlagen/` sind
per `.gitignore` ausgeschlossen: Bankverbindung, Steuernummer und Kundendaten gehören nicht in ein öffentliches
Repository. Beträge in Angebot und Rechnung berechnet `build.py` aus `preise.toml` (identisch mit der Website).

## Kundenmappe in der Reihenfolge des Einsatzes

| Phase | Dokument | Zweck | Wer unterschreibt |
| --- | --- | --- | --- |
| Erstkontakt | [vertrieb/gespraechsleitfaden.md](vertrieb/gespraechsleitfaden.md) | Erstgespräch mit Bedarfsnotiz, Einwände, UWG-Grenzen | – |
| | [vertrieb/demo-skript.md](vertrieb/demo-skript.md) | 20-Minuten-Vorführung mit Live-Anruf | – |
| | [vertrieb/email-vorlagen.md](vertrieb/email-vorlagen.md) | acht Mails von „Nach dem Erstgespräch“ bis „Kündigungsbestätigung“ | – |
| Angebot | [vertrieb/angebot.html](vertrieb/angebot.html) | Angebot mit berechneten Beträgen, als PDF | – |
| | [vertrag/leistungsbeschreibung-sla.md](vertrag/leistungsbeschreibung-sla.md) | Pakete, Freigabeklassen, Verfügbarkeit 99,0 %, Prioritäten P1–P4 | Anlage zum Vertrag |
| | [vertrag/agb.md](vertrag/agb.md) | AGB für Unternehmer (§ 14 BGB) | Anlage zum Vertrag |
| | [vertrag/pilotvereinbarung.md](vertrag/pilotvereinbarung.md) | acht Wochen, Erfolgskriterien K1–K6, Übergang | beide |
| Datenschutz | [datenschutz/avv.md](datenschutz/avv.md) | AVV nach Art. 28 DSGVO, Kundenfassung von `plattform/recht/AVV.md` | beide |
| | `plattform/recht/TOM.md` | Anlage 1 zur AVV (liegt im Plattform-Zweig) | – |
| | [datenschutz/dsfa.md](datenschutz/dsfa.md) | Datenschutz-Folgenabschätzung, vorausgefüllt für den Betrieb | Betrieb |
| | [datenschutz/ki-offenlegung.md](datenschutz/ki-offenlegung.md) | Ansage, Baustein Datenschutzerklärung, Nachweis KI-Kompetenz | Betrieb |
| | [datenschutz/vvt-eigene-verarbeitung.md](datenschutz/vvt-eigene-verarbeitung.md) | Verzeichnis für {{auftritt.marke}} als Verantwortlicher | intern |
| Onboarding | [onboarding/fragebogen.md](onboarding/fragebogen.md) | alle Angaben für die Einrichtung | Betrieb füllt aus |
| | [onboarding/einrichtungsprotokoll.md](onboarding/einrichtungsprotokoll.md) | 28 Schritte mit Abzeichnung | intern |
| | [onboarding/schulung.md](onboarding/schulung.md) | Einweisung Büro, Inhaber, Monteure, Kurzkarte | – |
| | [onboarding/go-live-abnahme.md](onboarding/go-live-abnahme.md) | 16 Prüfpunkte, Abnahmeerklärung | beide |
| Betrieb | [vertrieb/rechnung.html](vertrieb/rechnung.html) | Rechnung mit Pflichtangaben nach § 14 Abs. 4 UStG | – |
| | [betrieb/support-eskalation.md](betrieb/support-eskalation.md) | Kanäle, Fristen, Eskalation, Datenschutzvorfall | intern |
| | [betrieb/kuendigung-exit.md](betrieb/kuendigung-exit.md) | Kündigung, Export, Löschung, Rufumleitung zurück | intern |

Bereits an anderer Stelle vorhanden und weiterverwendet: Löschkonzept, Datenschutzhinweis Telefon, VVT als
Auftragsverarbeiter (`plattform/recht/`), Einseiter, Signatur und Akquisemails (`infrastruktur/`).

## Vor dem ersten Einsatz

1. Anwaltliche Prüfung von AGB, Leistungsbeschreibung, Pilotvereinbarung, AVV und KI-Offenlegung (ein Paket).
2. `geschaeftsdaten.toml` ausfüllen; Support- und Notfallnummer müssen tatsächlich erreichbar sein.
3. Zusagen der Leistungsbeschreibung (99,0 %, P1 binnen einer Stunde von 7 bis 20 Uhr) mit der eigenen
   Rufbereitschaft abgleichen; lieber niedriger zusagen als brechen.
4. E-Rechnung: PDF-Rechnungen an Unternehmer sind bis Ende 2026 zulässig (bis Ende 2027 bei Vorjahresumsatz bis
   800.000 €); danach Rechnungen aus Lexware Office oder sevDesk als XRechnung bzw. ZUGFeRD stellen.
