"""Vertriebs-Agenten: Erfassung/Dubletten, Upsell, Priorisierung mit Rückkopplung, Kadenz, Charge, Rücklauf.

Alle Leads sind fiktiv (Domains .invalid, Rufnummern 0000…).
"""

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

INFRA = Path(__file__).resolve().parent.parent / "infrastruktur"
sys.path.insert(0, str(INFRA))

import vertriebsagent
from vertrieb import erfassung, kadenz, scoring, speicher, upsell, versand
from vertrieb import pipeline

KOPF_WELLE = ["Welle", "Klasse", "Score", "Quelle", "Firma", "Gewerk", "PLZ", "Ort", "Straße", "Telefon", "E-Mail", "Website", "Entscheider",
              "Stellenanzeige", "Alter (Tage)", "Infrastruktur", "Probleme", "KUHN-Lösung", "Angebot", "Befund", "Status", "Quell-URL",
              "Akut (Anzeige ≤14 T., verifiziert, KEIN/SCHWACH)"]


def _xlsx(pfad: Path, blaetter: dict[str, list[list]]):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, zeilen in blaetter.items():
        ws = wb.create_sheet(name)
        for z in zeilen:
            ws.append(z)
    pfad.parent.mkdir(parents=True, exist_ok=True)
    wb.save(pfad)


def _welle(i: int, gewerk: str, score: int, **kw) -> list:
    w = {"Welle": "2026-11-08_01", "Klasse": "A", "Score": score, "Quelle": "Jobbörse", "Firma": f"Muster {gewerk} {i} GmbH", "Gewerk": gewerk,
         "PLZ": f"{44000 + i}", "Ort": "Musterstadt", "Straße": f"Teststraße {i}", "Telefon": f"0231 {1000000 + i}", "E-Mail": "",
         "Website": f"https://muster{i}.invalid", "Entscheider": f"Erika Muster{i}", "Stellenanzeige": "", "Alter (Tage)": "",
         "Infrastruktur": "SCHWACH", "Probleme": "P3, P4", "KUHN-Lösung": "", "Angebot": "T2", "Befund": "", "Status": "verifiziert",
         "Quell-URL": f"https://quelle.invalid/{i // 5}", "Akut (Anzeige ≤14 T., verifiziert, KEIN/SCHWACH)": ""}
    w.update(kw)
    return [w[k] for k in KOPF_WELLE]


@pytest.fixture
def wurzel(tmp_path: Path) -> Path:
    w = tmp_path / "leads"
    zeilen = [KOPF_WELLE]
    zeilen += [_welle(i, "Sanitär und Heizung", 60 + i) for i in range(12)]
    zeilen += [_welle(20 + i, "Kfz-Werkstatt", 70 + i, Stellenanzeige="Serviceberater (m/w/d)", Probleme="P8") for i in range(8)]
    zeilen.append(_welle(40, "Maler und Lackierer", 99, Website="", Probleme="P1, P7", Telefon="0171 0000040", Status="kein Interesse"))
    zeilen.append(_welle(41, "Elektro", 90, Straße="", Probleme="P2"))   # nicht versandfähig
    _xlsx(w / "wellen" / "Gesamtliste_Wellen.xlsx", {"Alle Wellen": zeilen,
                                                       "Aussortiert (Prüfung)": [["Firma", "PLZ", "Ort", "Telefon", "Grund"],
                                                                                 ["Muster Elektro 41 GmbH", "44041", "Musterstadt", "0231 1000041", "Gewerbe abgemeldet"]]})
    # Altliste mit abweichendem Schema und einer Dublette (gleiche Rufnummer, andere Schreibweise, Anschrift als Freitext)
    _xlsx(w / "Leads_Problemkatalog_v6_2026-11-07.xlsx", {
        "Übersicht": [["Top-Leads", 1]],
        "Top-Leads": [["Rang", "Belegte Probleme", "Abschluss-Score (0–100)", "Firma", "Entscheider (Name, Funktion)", "Anschrift", "Telefon", "Website"],
                      [1, "P5 Notdienst nur Telefon; P10 keine Karriereseite", 88, "Muster Sanitär und Heizung 3 GmbH & Co. KG", "",
                       "Teststraße 3, 44003 Musterstadt", "+49 231 1000003", "https://muster3.invalid"]],
    })
    return w


def test_erfassung_dubletten_schema_und_sperren(wurzel: Path):
    leads, prot = erfassung.alle_lesen(erfassung.quellen_finden(wurzel))
    assert prot["datensaetze"] == 23 and prot["leads"] == 22 and prot["dubletten"] == 1
    m3 = next(le for le in leads if le.werte.get("plz") == "44003")
    assert {"P3", "P4", "P5", "P10"} <= set(m3.probleme) and m3.score == 88.0 and len(m3.herkunft) == 2
    maler = next(le for le in leads if "Maler" in le.firma)
    assert maler.gesperrt and "P1" in maler.probleme and "P7" in maler.probleme
    e41 = next(le for le in leads if le.firma.startswith("Muster Elektro 41"))
    assert e41.ausgeschlossen == "Gewerbe abgemeldet"
    assert erfassung.telefon_norm("+49 (0)231 / 100-00-03") == erfassung.telefon_norm("0231 1000003")
    assert erfassung.firma_norm("Autofuchs – Inh. Alexander Galster") == "autofuchs"


def test_p1_nur_mit_beleg(tmp_path: Path):
    _xlsx(tmp_path / "SHK_Leads_Alt_2026-10-01.xlsx", {"Leads": [["Firmenname", "PLZ", "Ort", "Telefon", "Website"],
                                                                 ["Alt GmbH", "33100", "Paderborn", "05251 000001", ""]]})
    _xlsx(tmp_path / "Leads_ohne_Website_NRW_2026-11-04.xlsx", {"Leads ohne Website": [["Firmenname", "PLZ", "Ort", "Telefon", "Website-Status"],
                                                                                         ["Ohne KG", "33100", "Paderborn", "05251 000002", "keine Website im Verzeichnis eingetragen"]]})
    leads, _ = erfassung.alle_lesen(sorted(tmp_path.glob("*.xlsx")))
    p1 = {le.firma: "P1" in le.probleme for le in leads}
    assert p1 == {"Alt GmbH": False, "Ohne KG": True}


def test_upsell_regeln():
    katalog, preise = upsell.katalog_laden(), upsell.preise_laden()
    le = erfassung.Lead(id="L1", firma="Muster Haustechnik GmbH", werte={"gewerk": "Sanitär und Heizung", "stelle": "Disponent (m/w/d)",
                                                                         "website": "https://m.invalid", "angebot": "T2"},
                        probleme=["P7", "P8", "P10"], score=90)
    z = upsell.zuordnen(le, katalog, preise)
    codes = {m["code"] for m in z.module}
    assert z.branche == "shk" and z.edition == "pro"
    assert {"WA", "KS", "DI", "S1"} <= codes and "W1" not in codes
    assert {m["code"] for m in z.enthalten} >= {"WV", "DB"}         # in Pro enthalten: Argument, kein Mehrumsatz
    assert z.clv == pytest.approx(z.einmalig_gesamt + z.monat_gesamt * 24 * 0.88)


def test_bayes_rueckkopplung_hebt_und_senkt_segmente():
    katalog = upsell.katalog_laden()
    heute = dt.date(2026, 12, 31)
    leads = [erfassung.Lead(id=f"S{i}", firma=f"S{i}", werte={}, probleme=["P3"], score=70) for i in range(40)]
    leads += [erfassung.Lead(id=f"K{i}", firma=f"K{i}", werte={}, probleme=["P3"], score=70) for i in range(40)]
    branchen = {le.id: ("shk" if le.id[0] == "S" else "kfz") for le in leads}
    hist = {}
    for i in range(30):    # 30 Kontakte je Segment, abgelaufen; SHK 9 Erfolge, Kfz 0
        hist[f"S{i}"] = [{"typ": "brief_1", "datum": "2026-11-01"}] + ([{"typ": "termin", "datum": "2026-11-10"}] if i < 9 else [])
        hist[f"K{i}"] = [{"typ": "brief_1", "datum": "2026-11-01"}]
    hist["K39"] = [{"typ": "brief_1", "datum": "2026-12-20"}]          # laufende Kadenz: zensiert, zählt nicht
    m = scoring.Modell(leads, hist, katalog, heute, branchen)
    assert m.versuche == 60 and m.erfolge == 9
    assert m.g > 1                                                         # Rücklauf global über Prior (9 statt ~2 erwartet)
    assert m.p("S35") > m.p0["S35"] * m.g > m.p("K35")                     # SHK über, Kfz unter dem kalibrierten Niveau
    assert m.p("S35") > 1.5 * m.p("K35")                                   # gedämpft durch Priorstärke und geom. Mittel
    assert m.dim["branche"]["kfz"].n == 30


def test_kadenz():
    k = upsell.katalog_laden()["kadenz"]
    h = [{"typ": "brief_1", "datum": "2026-11-01"}]
    assert kadenz.stand(h, False, k, dt.date(2026, 11, 10)).status == "wartet auf Reaktion (Brief 1)"
    assert kadenz.stand(h, False, k, dt.date(2026, 11, 15)).status == "Nachfassbrief fällig"
    h2 = h + [{"typ": "brief_2", "datum": "2026-11-15"}]
    assert kadenz.stand(h2, False, k, dt.date(2026, 12, 25)).status == "Kadenz beendet"
    s = kadenz.stand(h + [{"typ": "antwort_positiv", "datum": "2026-11-05"}], False, k, dt.date(2026, 11, 5))
    assert s.status == "Rückmeldung positiv" and s.faellig == dt.date(2026, 11, 6) and "Einwilligung" in s.kanal
    assert kadenz.stand(h + [{"typ": "widerspruch", "datum": "2026-11-03"}], False, k, dt.date(2026, 11, 5)).status == "gesperrt"


def test_charge_freigabe_ruecklauf_ende_zu_ende(wurzel: Path, capsys):
    heute = "2026-11-08"
    assert vertriebsagent.main(["--wurzel", str(wurzel), "--heute", heute, "lauf", "--charge", "10"]) == 0
    out = wurzel / "vertrieb"
    assert (out / "Vertriebscockpit.xlsx").exists() and (out / "abrufbudget.json").exists()
    bogen = next((out / "chargen").glob("*/Charge_*_Freigabe.xlsx"))
    wb = openpyxl.load_workbook(bogen)
    ws = wb["Freigabe und Rücklauf"]
    kopf = [c.value for c in ws[1]]
    zeilen = list(ws.iter_rows(min_row=2))
    assert len(zeilen) == 10
    firmen = [r[kopf.index("Firma")].value for r in zeilen]
    assert not any("Maler" in f or "Elektro 41" in f for f in firmen)            # gesperrt bzw. nicht versandfähig
    assert sum("Sanitär" in f for f in firmen) >= 4                               # Diversifikation: Kfz (höherer EV) gedeckelt
    # Jonathan gibt 6 frei, lehnt 1 ab; trägt Versand und zwei Reaktionen ein
    for i, r in enumerate(zeilen):
        r[kopf.index("Freigabe (ja/nein)")].value = "ja" if i < 6 else ("nein" if i == 6 else None)
        if i < 6:
            r[kopf.index("Versandt am")].value = "09.11.2026"
    zeilen[0][kopf.index("Reaktion")].value = "termin"
    zeilen[0][kopf.index("Reaktionsdatum")].value = "12.11.2026"
    zeilen[1][kopf.index("Reaktion")].value = "widerspruch"
    zeilen[1][kopf.index("Reaktionsdatum")].value = "11.11.2026"
    wb.save(bogen)
    capsys.readouterr()
    assert vertriebsagent.main(["--wurzel", str(wurzel), "--heute", "2026-11-12", "einlesen", str(bogen)]) == 0
    bericht = json.loads(capsys.readouterr().out.split("\nBriefe erzeugen")[0])
    assert bericht["freigegeben"] == 6 and bericht["versandt"] == 6 and bericht["reaktionen"] == 2 and bericht["status"] == "versandt"
    assert (bogen.parent / "serienbrief.csv").read_text(encoding="utf-8-sig").count("\n") == 6   # Kopf + 6 − gesperrt
    # erneutes Einlesen ist idempotent
    assert vertriebsagent.main(["--wurzel", str(wurzel), "--heute", "2026-11-12", "einlesen", str(bogen)]) == 0
    db = speicher.oeffnen(out / "pipeline.db")
    assert db.execute("SELECT COUNT(*) FROM ereignisse").fetchone()[0] == 8
    gesperrt_id = zeilen[1][kopf.index("Lead-ID")].value
    assert db.execute("SELECT gesperrt FROM leads WHERE id=?", (gesperrt_id,)).fetchone()[0] == 1
    db.close()
    # 20 Tage später: Nachfassbriefe für die 4 ohne Reaktion, Sperre bleibt nach Neuaufbau bestehen, Termin als Aufgabe
    capsys.readouterr()
    assert vertriebsagent.main(["--wurzel", str(wurzel), "--heute", "2026-11-25", "lauf", "--charge", "10"]) == 0
    db = speicher.oeffnen(out / "pipeline.db")
    assert db.execute("SELECT gesperrt FROM leads WHERE id=?", (gesperrt_id,)).fetchone()[0] == 1
    neue = db.execute("SELECT auswahl, lead_id FROM chargen_leads WHERE charge_id LIKE 'C20261125%'").fetchall()
    assert sum(r[0].startswith("Nachfassbrief") for r in neue) == 4
    assert gesperrt_id not in {r[1] for r in neue}
    db.close()
    aufgaben = list(openpyxl.load_workbook(out / "Vertriebscockpit.xlsx", read_only=True)["Aufgaben"].iter_rows(values_only=True))
    assert any("Vorführung" in str(z[1]) for z in aufgaben[1:])


def test_offene_charge_blockiert_neue(wurzel: Path, capsys):
    vertriebsagent.main(["--wurzel", str(wurzel), "--heute", "2026-11-08", "lauf", "--charge", "5"])
    vertriebsagent.main(["--wurzel", str(wurzel), "--heute", "2026-11-09", "lauf", "--charge", "5"])
    assert "keine neue Charge" in capsys.readouterr().out


def test_versandfaehig():
    le = erfassung.Lead(id="x", firma="x", werte={"strasse": "Postfach 12", "plz": "44000", "ort": "X"})
    assert not versand.versandfaehig(le)
    le.werte["strasse"] = "Weg 1"
    assert versand.versandfaehig(le)


def test_veraltete_ids_ohne_historie_entfallen(wurzel: Path, tmp_path: Path):
    db = tmp_path / "p.db"
    pipeline.lauf(wurzel, db, dt.date(2026, 11, 8), neue_charge=False)
    con = speicher.oeffnen(db)
    con.execute("INSERT INTO leads SELECT 'Lalt0000000', firma, werte, probleme, score, akut, gesperrt, sperrgrund, ausgeschlossen, "
                "herkunft, erstmals, aktualisiert FROM leads LIMIT 1")
    con.commit()
    con.close()
    erg = pipeline.lauf(wurzel, db, dt.date(2026, 11, 9), neue_charge=False)
    assert "Lalt0000000" not in {r["lead"].id for r in erg.zeilen}
