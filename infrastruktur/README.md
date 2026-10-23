# Außenauftritt für die Akquise (Bedarfsanalyse SHK)

Glaubwürdigkeitsinfrastruktur für die Interviewphase: statische Landingpage mit Impressum (§ 5 DDG) und
Datenschutzerklärung (Art. 13 DSGVO), Einseiter als PDF mit Datenschutzhinweis nach Art. 14 DSGVO,
E-Mail-Signatur, Mailvorlagen und Schema der Gesprächsdokumentation. Alles wird aus einer einzigen
Datei mit Stammdaten erzeugt, sodass Impressum, Einseiter und Signatur nie auseinanderlaufen.

Das Verzeichnis ist in sich geschlossen. Zieht der Auftritt in ein eigenes Repository um, genügt es,
`infrastruktur/` und `.github/workflows/landingpage.yml` unverändert zu kopieren.

```
infrastruktur/
├── stammdaten.toml     einzige auszufüllende Datei (Name, Anschrift, Domain, Kontakt, Dienstleister)
├── build.py            rendert site/ + einseiter/ nach dist/ und vorlagen/ nach dist-vorlagen/
├── site/               index.html, impressum.html, datenschutz.html, styles.css
├── einseiter/          einseiter.html (A4, Druckvorlage für das PDF)
└── vorlagen/           Signatur (txt/html), Mail nach Telefonat, Widerspruchsbestätigung, CSV-Schema
```

## Marke und Website

Marke ist **KUHN** (`auftritt.marke`). Die Startseite ist eine vollständige Produktseite: Leistungen
(Telefonagent, Kundenverwaltung/Disposition, Angebote/Rechnungen/Mahnwesen, Belegeingang/Steuertools),
Ablauf, Freigabeklassen A/B/C, Rechner „Was kostet ein verpasster Anruf?“ (läuft nur im Browser), Preise
(Pakete „Erreichbar“, „Entlastet“, „Rundum“ = Basis/Pro/Premium, 349/649/999 € netto zzgl. Einrichtung
1.490/2.990/4.990 €, mit Tagespreis, Betriebsgröße und Anrufen statt Minuten), „Wer hinter KUHN steht“,
Einführung, FAQ und Demo-Anfrage. Die Demo-Anfrage läuft bewusst über `mailto:` mit vorausgefülltem Text, nicht über ein
Formular: kein Backend, kein Cookie-Banner, keine zusätzliche Datenverarbeitung. Farben und Logo (Kupfer,
Anthrazit, Bildmarke „K“ als Inline-SVG) stehen in `site/styles.css` bzw. im Kopf jeder Seite.

Vor einer Markenanmeldung oder dem Logodruck eine Markenrecherche zu „KUHN“ durchführen (DPMA, EUIPO):
Das Zeichen ist für andere Waren bereits eingetragen.

## Lokal bauen

```bash
python infrastruktur/build.py --pdf        # Vorschau; offene Werte erscheinen gelb als [OFFEN: …]
python infrastruktur/build.py --strict     # schlägt fehl, solange stammdaten.toml Lücken hat
```

Für `--pdf` wird Chrome oder Chromium gebraucht (sonst Umgebungsvariable `CHROME` auf die Binärdatei setzen).
Ergebnis: `infrastruktur/dist/` (veröffentlicht) und `infrastruktur/dist-vorlagen/` (nur für euch).

## Deployment

Der Workflow [`landingpage.yml`](../.github/workflows/landingpage.yml) baut bei jedem PR eine Vorschau
(Artefakt `landingpage-vorschau`, Warnung bei offenen Stammdaten). Veröffentlicht wird nur auf `main`,
entweder manuell über *Actions → Landingpage → Run workflow* oder automatisch bei jedem Push, sobald die
Repository-Variable `LANDINGPAGE_LIVE` auf `true` steht. Der Produktionsbuild läuft mit `--strict`.

## Handgriffe, die eure Konten oder Zahlungen brauchen

Reihenfolge so wählen, dass Impressumsangaben (Adresse, Telefon, E-Mail) vor dem Go-live feststehen.
Preise sind Richtwerte und vor Vertragsschluss zu prüfen.

### 1. Rechtsform und Anschrift festlegen
- Bezeichnung in `stammdaten.toml`: ohne eigenen Namen „Vorname Nachname und Vorname Nachname GbR“.
  Keine „UG (haftungsbeschränkt)“ vor Handelsregistereintragung, nach dem Notartermin allenfalls „UG i. G.“.
- Ladungsfähige Anschrift: Privatadresse oder Geschäftsadresse mit c/o-Service. Ein Postfach genügt nicht.
- Die Anschrift sollte im Kölner Ortsnetz liegen, weil Anbieter eine 0221-Ortsnetzrufnummer nur bei Wohn- oder
  Geschäftssitz im Ortsnetz vergeben (Zuteilungsregeln der Bundesnetzagentur).

### 2. Domain kaufen
- Eine .de-Domain bei einem Registrar mit DNS-Verwaltung (z. B. beim späteren Mailanbieter, IONOS, INWX).
  Kriterien: frei editierbare DNS-Einträge (A, AAAA, CNAME, MX, TXT), keine Weiterleitungspflicht.
- Domaininhaber (Holder) ist einer der Gesellschafter bzw. später die UG; als `auftritt.domain` eintragen.

### 3. Postfach einrichten
- Postfach auf der eigenen Domain, Hosting möglichst in Deutschland (z. B. mailbox.org oder IONOS; Posteo
  scheidet aus, weil es keine eigenen Domains unterstützt). Pro Gründer eine persönliche Adresse, z. B. `vorname@domain.de`.
- Im Kundenbereich des Anbieters den Auftragsverarbeitungsvertrag (AVV, Art. 28 DSGVO) abschließen und den
  Anbieter als `dienstleister.email_anbieter` (Firma, Anschrift) eintragen.
- DNS-Einträge laut Anbieter-Dokumentation setzen, typischerweise:

  | Typ | Name | Wert (Beispiel, Anbieterangaben maßgeblich) |
  | --- | --- | --- |
  | MX | `@` | Mailserver des Anbieters mit Priorität |
  | TXT | `@` | `v=spf1 include:<anbieter> ~all` |
  | CNAME/TXT | `<selector>._domainkey` | DKIM-Schlüssel des Anbieters |
  | TXT | `_dmarc` | `v=DMARC1; p=quarantine; rua=mailto:<eure-adresse>` |
  | TXT | `@` | ggf. Verifikationsnummer des Anbieters |

  SPF, DKIM und DMARC sind für Kaltmails an Betriebe faktisch Pflicht, sonst landet ihr im Spam.
  Prüfen mit einer Testmail an ein Gmail-Konto: „Original anzeigen“ muss `SPF: PASS`, `DKIM: PASS`, `DMARC: PASS` zeigen.
- Signatur aus `dist-vorlagen/email-signatur.html` (bzw. `.txt`) im Mailprogramm hinterlegen.

### 4. VoIP-Nummer 0221
- Tarif mit Ortsnetzrufnummer Köln und App für Smartphone/Rechner (z. B. sipgate). Beim Vertrag die
  Anschrift aus Schritt 1 angeben.
- Rufnummernanzeige einschalten; die Nummer darf bei Anrufen nicht unterdrückt werden (für Werbeanrufe
  ausdrücklich untersagt, TKG). Mailbox-Ansage mit Namen des Vorhabens und Rückrufversprechen.
- AVV abschließen, Anbieter als `dienstleister.telefon_anbieter` eintragen; Nummer als `kontakt.telefon`
  und im E.164-Format (`+49221…`) als `kontakt.telefon_e164`.

### 5. GitHub Pages aktivieren
1. *Repository → Settings → Pages → Build and deployment → Source:* **GitHub Actions**.
2. *Konto → Settings → Pages → Add a domain:* Domain eintragen und den angezeigten TXT-Eintrag
   `_github-pages-challenge-jonathankuhn11-spec` beim Registrar anlegen, dann *Verify*. Das schützt vor Domain-Übernahme.
3. DNS beim Registrar:

   | Typ | Name | Wert |
   | --- | --- | --- |
   | A | `@` | `185.199.108.153`, `185.199.109.153`, `185.199.110.153`, `185.199.111.153` |
   | AAAA | `@` | `2606:50c0:8000::153`, `2606:50c0:8001::153`, `2606:50c0:8002::153`, `2606:50c0:8003::153` |
   | CNAME | `www` | `jonathankuhn11-spec.github.io.` |

4. *Repository → Settings → Pages → Custom domain:* Domain eintragen, nach Zertifikatsausstellung **Enforce HTTPS** aktivieren.
5. `stammdaten.toml` vollständig ausfüllen, auf `main` mergen, *Actions → Landingpage → Run workflow*.
   Danach optional die Variable `LANDINGPAGE_LIVE=true` setzen (*Settings → Secrets and variables → Actions → Variables*).

### 6. Vor dem ersten Anruf prüfen
- Impressum, Datenschutz und Einseiter im Browser gegenlesen; keine gelben `[OFFEN: …]`-Marker mehr.
- Ein Anwalt oder die IHK-Gründungsberatung sollte die Rechtstexte einmal gegenlesen; sie sind sorgfältig
  formuliert, aber keine Rechtsberatung.
- Gesprächsdokumentation führen (Schema `vorlagen/gespraechsdokumentation.csv`), aber **nicht** in diesem
  öffentlichen Repository: Die Leadliste mit personenbezogenen Daten bleibt in der privaten Projektablage.

## Kundenunterlagen

`unterlagen/` enthält alles, was für den ersten Kunden stehen muss: AGB, Leistungsbeschreibung mit SLA,
Pilotvereinbarung, AVV-Kundenfassung, DSFA, KI-Offenlegung, Gesprächsleitfaden, Demo-Skript, E-Mail-Vorlagen,
Angebot und Rechnung (PDF), Onboarding-Fragebogen, Einrichtungsprotokoll, Schulung, Go-live-Abnahme, Support- und
Exit-Prozess. Erzeugt mit `python infrastruktur/build.py --unterlagen --kunde …`; Einzelheiten in
[unterlagen/README.md](unterlagen/README.md). Unterlagen werden nie veröffentlicht.

## Bewusste Entscheidungen und offene Punkte

- **Kein Kontaktformular, kein Tracking, keine externen Schriften.** Damit entfallen Cookie-Banner (§ 25 TDDDG),
  Formular-Backend und Einwilligungsmanagement; die Datenschutzerklärung bleibt kurz.
- **Hosting bei GitHub (USA).** Die Übermittlung stützt sich auf das EU-U.S. Data Privacy Framework.
  Vor Go-live die aktuelle Zertifizierung von GitHub, Inc. auf dataprivacyframework.gov prüfen. Wer Hosting in
  Deutschland bevorzugt, lädt `dist/` unverändert bei einem deutschen Webhoster hoch und passt Abschnitt 2 der
  Datenschutzerklärung an. GitHub Pages ist laut Nutzungsbedingungen nicht für Online-Shops oder SaaS gedacht;
  eine reine Informationsseite ohne Transaktionen ist davon nach unserem Verständnis nicht erfasst.
- **Eigenes Repository empfohlen.** Dieses Repository ist ein öffentlicher Prototyp mit fiktivem PV-Kunden.
  Für den Außenauftritt gegenüber SHK-Betrieben ist ein separates Repository (z. B. `shk-backoffice-web`)
  sauberer; die Dateien sind dafür bereits portabel angelegt.
