# Paket-Agenten: Kundenpaket je Lead

Erzeugt für jeden Lead aus den Leadlisten automatisch alles, was dessen **belegter Bedarf** erfordert – vom
Branchenprofil über eine vorkonfigurierte KUHN-Instanz mit Website bis zu Angebot, Brief und Gesprächsleitfaden.

```bash
# einmalig: Plattform-Zweig auschecken (enthält core/, Branchenprofile, Website, Registrierung)
git worktree add ../kuhn-plattform origin/plattform/backend-grundgeruest   # bzw. aktueller Plattform-Zweig
pip install -r ../kuhn-plattform/plattform/requirements.txt openpyxl

# akute Leads der Gesamtliste, vier Agenten parallel
python infrastruktur/paketagent.py /mnt/project-files/leads/wellen/Gesamtliste_Wellen.xlsx --akut \
       --plattform ../kuhn-plattform/plattform --parallel 4 --out /pfad/zu/pakete

# weitere Listen, nur Klasse A+/A, max. 20 neue Pakete (bereits erzeugte werden übersprungen)
python infrastruktur/paketagent.py leads/agents/*.xlsx --klasse A+ A --limit 20 --plattform … --out …
```

## Pipeline je Lead

| Agent | Modul | Ergebnis |
| --- | --- | --- |
| Lead-Agent | `leads.py` | liest xlsx/csv (Schema der Wellen, Synonyme für Altlisten), überspringt Widerspruch/„kein Interesse“ und Dubletten |
| Bedarfs-Agent | `bedarf.py` | P1–P10 + Infrastruktur + Stellenanzeige → Branchenprofil (17 Profile der Plattform), Edition, Website-Modus, Module |
| Instanz-Agent | `instanz.py` | Mandant über die Plattform-API (Selbstregistrierung, Stammdaten, Online-Buchung, Rufumleitung) → `kuhn-demo.db`, `agent.json` |
| Website-Agent | `instanz.py` | Betriebswebsite + Karriereseite mit der gesuchten Stelle, statischer Export `website/`, Bildschirmfotos |
| Angebots-Agent | `dokumente.py` | `kunde.toml` (direkt nutzbar für `build.py --unterlagen --kunde`) und `angebot.pdf` aus `preise.toml` |
| Brief-Agent | `dokumente.py` | `brief.pdf`, DIN 5008, Seite 1 drei Hauptbedarfe, Seite 2 Bedarfsanalyse, Website-Entwurf, Konditionen |
| Gesprächs-Agent | `dokumente.py` | `gespraechsleitfaden.md`: Bedarfsfragen je P-Code, Vorführablauf, Einwände, UWG-Rahmen |

Je Lead entsteht `<out>/<firma-plz>/index.html` als Einstieg; `<out>/Pakete_Uebersicht.xlsx` und `<out>/index.html`
fassen alle Pakete zusammen.

## Regeln des Bedarfs-Agenten

- **Edition:** Vorschlag der Leadliste (T1/T2/T3 → Basis/Pro/Premium), angehoben auf die Mindestedition der belegten
  Funktionen (P3 Online-Buchung → Pro, siehe `plattform/VERKAUF.md`).
- **Website:** P1 oder keine Website → neu; P2/P6/P9 oder Infrastruktur KEIN/SCHWACH → Ablösung; sonst Ergänzung.
- **Keine erfundenen Inhalte:** Preise, Kundenstimmen, Team und Standard-FAQ bleiben aus; Notdienst nur bei P5 oder
  belegtem Notdienst im Befund. Jede Seite trägt den Hinweis „Unverbindlicher Entwurf … nicht veröffentlicht“ und `noindex`.
- **Branchennäherung** (z. B. Bautenschutz → Maler, Bestattung → Tischler) wird im Paket als Hinweis ausgewiesen.

## Live-Demo

```bash
cd ../kuhn-plattform/plattform
DATABASE_URL=sqlite:////absoluter/pfad/<firma-plz>/kuhn-demo.db python -m core serve
# Website: http://localhost:8100/web/<slug>?vorschau=1 · Büro: http://localhost:8100/app/
```

Mit `--demo-url https://demo.<domain>` verweisen Brief, Formular und Buchungsknopf des Website-Exports auf eine
laufende Demo-Plattform.

## Datenschutz und Wettbewerbsrecht

- Pakete enthalten personenbezogene Daten: Ausgabe nie ins Repository (Standard `infrastruktur/dist-pakete/` ist ignoriert).
- Erstkontakt per Brief (§ 7 Abs. 1 UWG) mit Art.-14-Hinweis; Anruf nur als Nachfass mit Sachbezug; E-Mail erst nach Einwilligung.
- Die Website-Entwürfe werden nicht veröffentlicht, sondern im Brief abgebildet bzw. in der Vorführung gezeigt.
- Es werden keine Webseiten abgerufen; das gemeinsame Abrufkontingent der Lead-Agenten bleibt unberührt.
