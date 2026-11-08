"""Angebots-, Brief- und Gesprächs-Agent sowie Paketübersicht.

Angebot: rendert unterlagen/vertrieb/angebot.html mit denselben Funktionen wie build.py --unterlagen (Beträge aus
preise.toml, Anbieterangaben aus stammdaten.toml und unterlagen/geschaeftsdaten.toml); die Kundendatei kunde.toml
liegt im Paket und kann direkt für die vollständige Kundenmappe genutzt werden.
Brief: DIN 5008 Form B wie vorlagen/brief-pilot.html, Seite 1 mit den drei wichtigsten belegten Bedarfen, Seite 2 mit
Bedarfsanalyse, Website-Entwurf (Bildschirmfoto) und Konditionen; Art.-14-Hinweis im Fuß (Erstkontakt per Brief,
§ 7 Abs. 1 UWG).
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import subprocess
from pathlib import Path

import build
from kundenpaket.bedarf import Bedarf, ansprechpartner
from kundenpaket.leads import Lead

BASIS = Path(build.__file__).resolve().parent
VORLAGEN = Path(__file__).resolve().parent / "vorlagen"
MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]
MODUS_TEXT = {"neu": "neue Betriebswebsite", "abloesung": "Ablösung der bestehenden Website", "ergaenzung": "Ergänzung der bestehenden Website (Buchung, Formular, Portal)"}
_e = html.escape


def datum_lang(d: dt.date) -> str:
    return f"{d.day}. {MONATE[d.month - 1]} {d.year}"


def anbieterdaten() -> dict[str, str]:
    daten = build.stammdaten_laden(BASIS / "stammdaten.toml")
    geschaeft = BASIS / "unterlagen" / "geschaeftsdaten.toml"
    if not geschaeft.exists():
        geschaeft = BASIS / "unterlagen" / "geschaeftsdaten.vorlage.toml"
    daten.update(build.flach(build.toml_laden(geschaeft)))
    return daten


def preise() -> dict:
    return build.toml_laden(BASIS / "unterlagen" / "preise.toml")


def _toml_str(v: str) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def angebot(lead: Lead, bedarf: Bedarf, ziel: Path, nummer: str, heute: dt.date, pdf: bool) -> dict[str, str]:
    p = preise()
    nachlass = str(p.get("pilot", {}).get("nachlass_einrichtung_prozent", ""))
    kunde = {"kunde": {"firma": lead.firma, "rechtsform": "", "strasse": lead.strasse, "plz_ort": f"{lead.plz} {lead.ort}".strip(),
                       "vertreten_durch": lead.entscheider, "ansprechpartner": "", "email": lead.email, "telefon": lead.telefon,
                       "ust_idnr": "", "kundennummer": "", "paket": bedarf.edition},
             "angebot": {"nummer": nummer, "datum": heute.strftime("%d.%m.%Y"), "gueltig_bis": (heute + dt.timedelta(days=30)).strftime("%d.%m.%Y"),
                         "nachlass_einrichtung_prozent": nachlass, "vertragsbeginn": "nach Vereinbarung"},
             "pilot": {"beginn": "", "ende": "", "verguetung": "kostenfrei"},
             "rechnung": {"nummer": "", "datum": "", "leistungszeitraum": "", "mehrminuten": "", "mit_einrichtung": "", "faellig_bis": ""}}
    toml = "# Kundendatei, erzeugt vom Paket-Agenten. Für die vollständige Kundenmappe:\n" \
           "#   python infrastruktur/build.py --unterlagen --kunde <dieser pfad> --pdf\n\n" + \
           "\n\n".join(f"[{abschnitt}]\n" + "\n".join(f"{k} = {_toml_str(v)}" for k, v in werte.items()) for abschnitt, werte in kunde.items()) + "\n"
    (ziel / "kunde.toml").write_text(toml, encoding="utf-8")

    daten = {**anbieterdaten(), **build.flach(kunde)}
    daten.update(build.abgeleitet(daten, p))
    offen: set[str] = set()
    unbekannt: set[str] = set()
    text = build.rendern((BASIS / "unterlagen" / "vertrieb" / "angebot.html").read_text(encoding="utf-8"), daten, offen, unbekannt)
    datei = ziel / "angebot.html"
    datei.write_text(text, encoding="utf-8")
    if pdf:
        build.pdf_drucken(datei, ziel / "angebot.pdf")
    return {**{k: daten.get(k, "") for k in ("paket.name", "paket.monat_netto", "paket.einrichtung_netto", "paket.minuten", "angebot.einrichtung_netto",
                                             "angebot.nachlass")}, "offen": ", ".join(sorted(offen))}


def bildschirmfoto(html_datei: Path, png: Path, breite: int, hoehe: int) -> bool:
    chrome = build.chrome_finden()
    if not chrome or not html_datei.exists():
        return False
    subprocess.run([chrome, "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--disable-background-networking", "--no-first-run", "--force-device-scale-factor=1",
                    f"--window-size={breite},{hoehe}", f"--screenshot={png}", html_datei.as_uri()], check=False, capture_output=True, timeout=60)
    return png.exists()


def _ersetzen(vorlage: str, werte: dict[str, str]) -> str:
    return re.sub(r"\[\[([a-z_]+)\]\]", lambda m: werte.get(m.group(1), m.group(0)), vorlage)


def brief(lead: Lead, bedarf: Bedarf, ziel: Path, konditionen: dict[str, str], heute: dt.date, pdf: bool, demo_url: str | None) -> Path:
    p = preise()
    person = ansprechpartner(lead)
    anrede = f"Guten Tag {person}," if person else "Sehr geehrte Damen und Herren,"
    punkte = "".join(f"<li><b>{_e(m.beobachtung)}</b> {_e(m.loesung)}</li>" for m in bedarf.brief_module if m.beobachtung)
    tabelle = "".join(f"<tr><td>{_e(m.code)}</td><td>{_e(m.problem)}</td><td>{_e(m.name)}</td><td>{_e(m.loesung)}</td></tr>" for m in bedarf.module)
    bild_desktop = (ziel / "website-desktop.png").exists()
    bild_mobil = (ziel / "website-mobil.png").exists()
    bilder = ""
    if bild_desktop or bild_mobil:
        bilder = ('<div class="bilder">' + ('<img class="desk" src="website-desktop.png" alt="Website-Entwurf, Desktop">' if bild_desktop else "")
                  + ('<img class="mobil" src="website-mobil.png" alt="Website-Entwurf, Smartphone">' if bild_mobil else "") + "</div>")
    pilot = p.get("pilot", {})
    werte = {
        "firma": _e(lead.firma), "zu_haenden": f"z. Hd. {_e(person)}<br>" if person else "", "strasse": _e(lead.strasse or "[Straße fehlt]"),
        "plz": _e(lead.plz), "ort": _e(lead.ort), "datum": datum_lang(heute), "anrede": _e(anrede),
        "betreff": _e(f"Für {lead.firma}: weniger Telefonlast im Büro, mehr Aufträge" if any(m.code == "P8" for m in bedarf.module)
                      else f"Für {lead.firma}: kein Anruf und keine Anfrage geht mehr verloren"),
        "punkte": punkte, "tabelle": tabelle, "bilder": bilder,
        "website_modus": _e(MODUS_TEXT[bedarf.website_modus]),
        "demo": (f'Ihre Demo-Instanz: <b>{_e(demo_url)}</b>' if demo_url else
                 "Eine Demo-Instanz für Ihren Betrieb ist eingerichtet; wir zeigen sie Ihnen in 20 Minuten – vor Ort oder per Bildschirmfreigabe."),
        "paket": _e(konditionen.get("paket.name", "")), "monat": _e(konditionen.get("paket.monat_netto", "")),
        "einrichtung": _e(konditionen.get("paket.einrichtung_netto", "")), "minuten": _e(konditionen.get("paket.minuten", "")),
        "pilot_wochen": str(pilot.get("dauer_wochen", 8)), "nachlass_einrichtung": str(pilot.get("nachlass_einrichtung_prozent", 50)),
        "nachlass_monat": str(pilot.get("nachlass_monat_prozent", 30)), "quelle": _e(lead.quelle or "öffentliche Stellenanzeige, Impressum") +
        (f" ({_e(lead.quell_url.split('?')[0])})" if lead.quell_url else ""),
    }
    vorlage = build.rendern((VORLAGEN / "brief-paket.html").read_text(encoding="utf-8"), anbieterdaten(), set(), set())
    datei = ziel / "brief.html"
    datei.write_text(_ersetzen(vorlage, werte), encoding="utf-8")
    if pdf:
        build.pdf_drucken(datei, ziel / "brief.pdf")
    return datei


def gespraech(lead: Lead, bedarf: Bedarf, ziel: Path, instanz: dict | None, konditionen: dict[str, str]) -> Path:
    person = ansprechpartner(lead) or "Geschäftsleitung"
    fragen = {
        "P1": "Wie finden Neukunden Sie heute – Empfehlung, Google, Verzeichnisse?", "P2": "Wie kommen schriftliche Anfragen herein, und wer tippt sie ab?",
        "P3": "Wie viele Anrufe am Tag drehen sich nur um Terminvereinbarung?", "P4": "Wie oft rufen Kunden an, um nach dem Stand zu fragen?",
        "P5": "Wer nimmt nachts und am Wochenende Notrufe an, und wie wird das vergütet?", "P6": "Wann wurde die Website zuletzt überarbeitet, wer pflegt sie?",
        "P7": "Wer trägt das Handy, wenn Sie auf der Baustelle oder beim Kunden sind?", "P8": "Seit wann ist die Stelle offen, und was bleibt bis dahin liegen?",
        "P9": "Wer hat Impressum und Datenschutzerklärung zuletzt geprüft?", "P10": "Wo veröffentlichen Sie Stellen heute, und wie viele Bewerbungen kommen?",
    }
    demo = {
        "P1": "Website-Entwurf zeigen (Seite 2 des Briefs bzw. Live-Demo /web/…).", "P2": "Formular mit Foto absenden → Vorgang erscheint im Büro.",
        "P3": "Online-Termin buchen → Freigabe-Inbox → Bestätigung.", "P4": "Portal-Link eines Vorgangs öffnen: Status, Termin, Rechnung.",
        "P5": "„Anruf simulieren“ mit Notfall → Weiterleitung an Bereitschaft.", "P6": "Alt/Neu-Vergleich auf dem Smartphone.",
        "P7": "Einrichtung › Rufumleitung: GSM-Code antippen, Nummer bleibt.", "P8": "Cockpit und Freigabe-Inbox: was der Agent heute vorbereitet hätte.",
        "P9": "Website ohne Cookies/Fremdskripte; Impressum aus Stammdaten (Entwurf).", "P10": "Karriereseite mit der gesuchten Stelle, Kurzbewerbung.",
    }
    zeilen = [f"# Gesprächsleitfaden: {lead.firma}", "",
              f"**Ansprechpartner:** {person} · **Telefon:** {lead.telefon or '–'} · **E-Mail:** {lead.email or '–'} · **Website:** {lead.website or 'keine'}",
              f"**Gewerk:** {lead.gewerk} · **Ort:** {lead.plz} {lead.ort} · **Klasse/Score:** {lead.klasse} / {lead.score} · **Akut:** {'ja' if lead.akut else 'nein'}",
              f"**Branchenprofil:** {bedarf.branche}{' (Näherung)' if bedarf.branche_naeherung else ''} · **Edition:** {konditionen.get('paket.name', bedarf.edition)} "
              f"({konditionen.get('paket.monat_netto', '')} / Monat, Einrichtung {konditionen.get('paket.einrichtung_netto', '')}) · **Website:** {MODUS_TEXT[bedarf.website_modus]}",
              "", "## Rechtsrahmen", "",
              "Erstkontakt per Brief (§ 7 Abs. 1 UWG). Anruf nur als Nachfass mit konkretem Sachbezug zum Brief und bei mutmaßlichem Interesse "
              "(§ 7 Abs. 2 Nr. 1 UWG, B2B); bei Ablehnung sofort Widerspruch vermerken. E-Mail erst nach Einwilligung.",
              "", "## Interner Befund (nicht vorlesen)", "", lead.befund or "–", "", f"Stellenanzeige: {lead.stelle or '–'} ({lead.alter or '?'} Tage)", "",
              "## Einstieg", "",
              f"„Guten Tag, hier ist … von KUHN. Ich hatte Ihnen vor ein paar Tagen einen Brief geschickt – mit einem Entwurf für {lead.firma}. "
              "Haben Sie zwei Minuten, oder passt ein anderer Zeitpunkt besser?“", "", "## Bedarfsfragen", ""]
    zeilen += [f"- **{m.code} {m.problem}:** {fragen[m.code]}" for m in bedarf.module if m.code in fragen]
    zeilen += ["", "## Vorführung (20 Minuten)", ""]
    zeilen += [f"{i}. {demo[m.code]}" for i, m in enumerate([m for m in bedarf.module if m.code in demo], 1)]
    if instanz:
        zeilen += ["", f"Demo starten: `cd plattform && DATABASE_URL=sqlite:///<Paketordner>/kuhn-demo.db python -m core serve` (absoluter Pfad) → "
                       f"`http://localhost:8100/web/{instanz['slug']}?vorschau=1`, Büro `http://localhost:8100/app/` (X-Mandant {instanz['mandant_id']})."]
    zeilen += ["", "## Einwände", "",
               "- *„Zu teuer.“* → Gegenrechnung offene Bürostelle bzw. verlorene Aufträge; Pilot acht Wochen ohne Grundgebühr.",
               "- *„Meine Kunden wollen keinen Roboter.“* → Agent stellt sich als Assistent vor, verbindet bei Bedarf sofort; Mitschnitt nur mit Einwilligung.",
               "- *„Wir haben schon eine Software.“* → Übergabe an die Fachsoftware bzw. Export; nichts geht ohne Freigabe raus.",
               "", "## Nächster Schritt", "", "Vor-Ort-Termin (20 Minuten Demo) oder Pilotvereinbarung; danach Kundenmappe aus `kunde.toml` erzeugen.", ""]
    if bedarf.hinweise:
        zeilen += ["## Hinweise zur Datenlage", ""] + [f"- {h}" for h in bedarf.hinweise] + [""]
    datei = ziel / "gespraechsleitfaden.md"
    datei.write_text("\n".join(zeilen), encoding="utf-8")
    return datei


def uebersicht(lead: Lead, bedarf: Bedarf, ziel: Path, ergebnis: dict) -> Path:
    def link(datei: str, text: str) -> str:
        return f'<li><a href="{datei}">{_e(text)}</a></li>' if (ziel / datei).exists() else f"<li>{_e(text)} – nicht erzeugt</li>"
    zeilen = "".join(f"<tr><td>{_e(m.code)}</td><td>{_e(m.problem)}</td><td>{_e(m.name)}</td></tr>" for m in bedarf.module)
    html_text = f"""<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Kundenpaket {_e(lead.firma)}</title>
<style>:root{{--t:#1b2129;--l:#59636f;--r:#e2e6eb;--b:#fff;--a:#b25a26}}@media (prefers-color-scheme:dark){{:root{{--t:#e8edf2;--l:#9aa8b5;--r:#2c3a48;--b:#121a24}}}}
body{{margin:0;padding:24px 16px;font:16px/1.5 system-ui,sans-serif;color:var(--t);background:var(--b)}}main{{max-width:900px;margin:auto}}
h1{{margin:0 0 4px}}p.l{{color:var(--l);margin:0 0 20px}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid var(--r);padding:6px 8px;text-align:left;vertical-align:top}}
.k{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px;margin:20px 0}}.k>div{{border:1px solid var(--r);border-radius:12px;padding:14px}}
img{{max-width:100%;border:1px solid var(--r);border-radius:8px}}a{{color:var(--a)}}</style></head><body><main>
<h1>{_e(lead.firma)}</h1><p class="l">{_e(lead.gewerk)} · {_e(lead.plz)} {_e(lead.ort)} · Klasse {_e(lead.klasse)} · Score {_e(lead.score)}{' · AKUT' if lead.akut else ''}</p>
<div class="k"><div><h2>Bedarf</h2><table>{zeilen}</table></div>
<div><h2>Paket</h2><p>Edition <b>{_e(ergebnis.get('paket', ''))}</b>, {_e(ergebnis.get('monat', ''))} / Monat, Einrichtung {_e(ergebnis.get('einrichtung', ''))}<br>
Branchenprofil {_e(bedarf.branche)}{' (Näherung)' if bedarf.branche_naeherung else ''}<br>Website: {_e(MODUS_TEXT[bedarf.website_modus])}</p>
<ul>{link('brief.pdf', 'Brief (PDF, 2 Seiten)')}{link('angebot.pdf', 'Angebot (PDF)')}{link('website/index.html', 'Website-Entwurf')}
{link('website/karriere.html', 'Karriereseite')}{link('gespraechsleitfaden.md', 'Gesprächsleitfaden')}{link('agent.json', 'Telefonagent-Konfiguration')}
{link('kunde.toml', 'Kundendatei für die Kundenmappe')}{link('kuhn-demo.db', 'KUHN-Demo-Instanz (SQLite)')}</ul></div></div>
{'<p><img src="website-desktop.png" alt="Website-Entwurf"></p>' if (ziel / 'website-desktop.png').exists() else ''}
{'<h2>Hinweise</h2><ul>' + ''.join(f'<li>{_e(h)}</li>' for h in bedarf.hinweise) + '</ul>' if bedarf.hinweise else ''}
</main></body></html>"""
    datei = ziel / "index.html"
    datei.write_text(html_text, encoding="utf-8")
    (ziel / "paket.json").write_text(json.dumps(ergebnis, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return datei
