"""Paket-Agenten: Bedarfsanalyse je Lead und Kundenpaket (Angebot, Brief, Leitfaden, optional KUHN-Instanz + Website).

Alle Leads sind fiktiv. Der Instanz-Test läuft nur, wenn KUHN_PLATTFORM auf ein Verzeichnis plattform/ zeigt
(Zweig plattform/backend-grundgeruest) und dessen Abhängigkeiten installiert sind.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parent.parent / "infrastruktur"
sys.path.insert(0, str(INFRA))

from kundenpaket import bedarf, leads  # noqa: E402

KOPF = "Klasse;Score;Firma;Gewerk;PLZ;Ort;Straße;Telefon;E-Mail;Website;Entscheider;Stellenanzeige;Infrastruktur;Probleme;Angebot;Befund;Status;Akut"
ZEILEN = [
    "A+;95;Muster Haustechnik GmbH;Sanitär und Heizung;59555;Lippstadt;Beispielweg 1;02941 000000;info@muster-haustechnik.invalid;https://muster.invalid;"
    "Dipl.-Ing. Erika Muster, Max Muster;Bürokraft (m/w/d) in Teilzeit – 20 Std.;SCHWACH;P3, P5, P7;T1 Basis;Notdienst nur per Handy;verifiziert;AKUT",
    "A;80;Beispiel Malerbetrieb;Maler und Lackierer;48143;Münster;Testgasse 2;0251 000000;;;Inh. Paul Beispiel;;KEIN;P2;;;Indiz;",
    "B;60;Gesperrt KG;Elektro;33098;Paderborn;Weg 3;05251 000000;;;;;;P1;;;kein Interesse;",
    "A+;95;Muster Haustechnik GmbH;Sanitär und Heizung;59555;Lippstadt;Beispielweg 1;;;;;;;P3;;;verifiziert;",
]


@pytest.fixture
def liste(tmp_path):
    datei = tmp_path / "leads.csv"
    datei.write_text("\n".join([KOPF, *ZEILEN]) + "\n", encoding="utf-8")
    return datei


def test_lead_agent_filtert_widerspruch_und_dubletten(liste):
    alle = leads.laden(liste)
    assert len(alle) == 4 and alle[0].probleme == ["P3", "P5", "P7"] and alle[0].akut
    auswahl = leads.filtern(alle)
    assert [le.firma for le in auswahl] == ["Muster Haustechnik GmbH", "Beispiel Malerbetrieb"]
    assert [le.firma for le in leads.filtern(alle, akut=True)] == ["Muster Haustechnik GmbH"]
    assert auswahl[0].schluessel == "muster-haustechnik-gmbh-59555"


def test_bedarfs_agent_leitet_branche_edition_und_module_ab(liste):
    shk, maler = leads.filtern(leads.laden(liste))
    b = bedarf.analysieren(shk)
    assert b.branche == "shk" and not b.branche_naeherung
    assert b.edition == "pro"                       # T1 aus der Liste, P3 (Online-Buchung) verlangt Pro
    assert b.website_modus == "abloesung" and b.notdienst and b.rufumleitung
    codes = [m.code for m in b.module]
    assert codes == ["P3", "P5", "P7", "P8"]         # P8 aus der Stellenanzeige ergänzt
    assert b.stelle == {**b.stelle, "titel": "Bürokraft (m/w/d)", "art": "teilzeit", "ort": "Lippstadt"}
    assert [m.code for m in b.brief_module] == ["P8", "P5", "P3"]
    assert bedarf.ansprechpartner(shk) == "Erika Muster"

    b2 = bedarf.analysieren(maler)
    assert b2.branche == "maler" and b2.edition == "basis" and b2.website_modus == "neu" and not b2.notdienst
    assert [m.code for m in b2.module][:2] == ["P1", "P2"] and b2.module[-1].name == "Telefonagent"
    assert bedarf.ansprechpartner(maler) == "Paul Beispiel"


def test_naeherung_wird_gekennzeichnet():
    le = leads.Lead(firma="Abdicht GmbH", gewerk="Bautenschutz / Abdichtung", ort="Bonn", strasse="", website="https://x.invalid")
    b = bedarf.analysieren(le)
    assert b.branche == "maler" and b.branche_naeherung
    assert any("Näherung" in h for h in b.hinweise) and any("Straße fehlt" in h for h in b.hinweise)


def _lauf(liste, out, *zusatz, env=None):
    return subprocess.run([sys.executable, str(INFRA / "paketagent.py"), str(liste), "--out", str(out), "--ohne-pdf", "--datum", "2026-11-09", *zusatz],
                          check=True, capture_output=True, text=True, env=env)


def test_paket_ohne_plattform_liefert_angebot_brief_und_leitfaden(liste, tmp_path):
    out = tmp_path / "pakete"
    env = {**os.environ, "KUHN_PLATTFORM": str(tmp_path / "fehlt")}
    r = _lauf(liste, out, "--plattform", str(tmp_path / "fehlt"), env=env)
    assert "2 Pakete zu erzeugen" in r.stdout
    paket = out / "muster-haustechnik-gmbh-59555"
    brief = (paket / "brief.html").read_text(encoding="utf-8")
    assert "Guten Tag Erika Muster," in brief and "Bürokraft (m/w/d)" in brief and "Art. 14 DSGVO" in brief
    assert not re.search(r"\[\[[a-z_]+\]\]|\{\{[a-z_.]+\}\}", brief)
    angebot = (paket / "angebot.html").read_text(encoding="utf-8")
    assert "Pro" in angebot and "649,00 €" in angebot and "Muster Haustechnik GmbH" in angebot
    assert 'paket = "pro"' in (paket / "kunde.toml").read_text(encoding="utf-8")
    leitfaden = (paket / "gespraechsleitfaden.md").read_text(encoding="utf-8")
    assert "§ 7 Abs. 1 UWG" in leitfaden and "P5 Notdienst nur telefonisch" in leitfaden
    meta = json.loads((paket / "paket.json").read_text(encoding="utf-8"))
    assert meta["edition"] == "pro" and meta["fehler"] and "Plattform nicht gefunden" in meta["fehler"][0]
    assert (out / "Pakete_Uebersicht.xlsx").exists() and "Kundenpakete (2)" in (out / "index.html").read_text(encoding="utf-8")
    # inkrementell: zweiter Lauf erzeugt nichts neu
    assert "0 Pakete zu erzeugen" in _lauf(liste, out, "--plattform", str(tmp_path / "fehlt"), env=env).stdout


@pytest.mark.skipif(not os.environ.get("KUHN_PLATTFORM"), reason="KUHN_PLATTFORM (Verzeichnis plattform/) nicht gesetzt")
def test_paket_mit_plattform_richtet_instanz_und_website_ein(liste, tmp_path):
    out = tmp_path / "pakete"
    _lauf(liste, out, "--akut", "--plattform", os.environ["KUHN_PLATTFORM"])
    paket = out / "muster-haustechnik-gmbh-59555"
    meta = json.loads((paket / "paket.json").read_text(encoding="utf-8"))
    assert not meta["fehler"], meta["fehler"]
    assert meta["instanz"]["website_seiten"] == ["index.html", "impressum.html", "datenschutz.html", "karriere.html"]
    web = (paket / "website" / "index.html").read_text(encoding="utf-8")
    assert "Unverbindlicher Entwurf von KUHN" in web and "noindex" in web and "Bürokraft (m/w/d)" in web
    assert 'href="impressum.html"' in web and "/web/" not in web.split("<body>", 1)[1].replace("/web/muster", "")
    assert "Notdienst" in web and "Kundenstimmen" not in web
    agent = json.loads((paket / "agent.json").read_text(encoding="utf-8"))
    assert agent["branche"] == "shk" and agent["rufumleitung"] and agent["notdienst"]["aktiv"]
    assert (paket / "kuhn-demo.db").stat().st_size > 0


def test_p3_ohne_online_buchung_wird_besichtigungsanfrage():
    le = leads.Lead(firma="Holz GmbH", gewerk="Schreinerei", ort="Bonn", strasse="Weg 1", website="https://x.invalid", probleme=["P3"], angebot="T1 Basis")
    b = bedarf.analysieren(le)
    assert b.branche == "tischler" and b.edition == "basis"
    assert b.module[0].name == "Online-Besichtigungsanfrage"
