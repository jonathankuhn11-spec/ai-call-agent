"""Cockpit-Agent: schreibt das Vertriebscockpit (Excel) mit Pipeline, Aufgaben, Charge, Upsell, Segmenten und Abrufbudget."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from vertrieb import scoring

METHODIK = [
    ("Zweck", "Steuerung der gesamten Vertriebskette: welcher Betrieb wann welchen Brief mit welchem Angebot erhält, und wohin das Abrufkontingent der Lead-Agenten fließt."),
    ("Erfassung", "Alle Leadlisten (Wellen, Segment-Agenten, Altlisten) werden gelesen, Spalten über Synonyme erkannt und Dubletten per Union-Find über Rufnummer bzw. Firmenname+PLZ zusammengeführt. Jüngster nichtleerer Wert gewinnt; Problemcodes werden vereinigt."),
    ("P1-Regel", "„keine Website“ nur bei ausdrücklichem Beleg (Websuche/Verzeichnis negativ oder „ohne Website“-Liste). Eine leere Spalte in Altlisten gilt als nicht recherchiert."),
    ("Upsell", "Edition und Branchenprofil vom Bedarfs-Agenten der Kundenpakete; Zusatzmodule nach Auslöseregeln (katalog.toml). In der Edition enthaltene Module sind Argumente, kein Mehrumsatz."),
    ("Prior", "p0 = 0,8 % + 6,2 % · (Score/100)², akut · 1,5, Kappung 25 %. Annahme bis zum Vorliegen eigener Rücklaufdaten."),
    ("Rückkopplung", "Empirical Bayes: Beta-Binomial je Segment (Branche, Signaltyp), Priorstärke 30 Pseudo-Beobachtungen; Lift = Posterior/Prior, je Lead geometrisches Mittel beider Lifts. Kontakte zählen erst nach Ergebnis oder Kadenzende (Rechtszensierung)."),
    ("Erwartungswert", "EV je Brief = p · P(Abschluss | Reaktion) · CLV − Kontaktkosten. CLV = Einmalbeträge + Monatsbeträge · 24 Monate · DB-Quote 88 %."),
    ("Exploration", "20 % jeder Charge per Thompson-Sampling; höchstens 40 % einer Charge aus einer Branche (Diversifikation, Lernen über Segmente)."),
    ("Kadenz", "Erstbrief → 14 Tage → Nachfassbrief → 35 Tage → Kadenzende, Wiedervorlage nach 120 Tagen. Positive Rückmeldung → Rückruf binnen 24 h."),
    ("Recht", "Erst- und Nachfasskontakt ausschließlich per Brief (§ 7 Abs. 1 UWG). Telefon/E-Mail/WhatsApp nur nach Rückmeldung bzw. dokumentierter Einwilligung (§ 7 Abs. 2 Nr. 1, 2 UWG). Ablehnung oder Widerspruch sperrt dauerhaft (Sperrliste über Lead-ID und Rufnummer, Art. 21 Abs. 3 DSGVO). Art.-14-Information mit dem Erstbrief."),
    ("Freigabe", "Es wird nichts automatisch versendet. Chargen sind Vorschläge; nur Zeilen mit „Freigabe = ja“ gelangen in Serienbrief und Paketliste."),
    ("Abrufbudget", "Wert je Abruf = neue Leads je Ergebnisseite · mittleres p · CLV; UCB1-Zuschlag für kleine Stichproben. Empfehlung als JSON für die Wellen-Agenten."),
]


def _blatt(wb, titel: str, kopf: list[str], zeilen: list[list], breiten: dict[str, int] | None = None, prozent: tuple[str, ...] = (),
           euro: tuple[str, ...] = ()):
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    ws = wb.create_sheet(titel)
    ws.append(kopf)
    for z in zeilen:
        ws.append(z)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1B6EC2")
        c.alignment = Alignment(wrap_text=True, vertical="top")
    for j, k in enumerate(kopf, 1):
        col = get_column_letter(j)
        ws.column_dimensions[col].width = (breiten or {}).get(k, max(10, min(40, len(k) + 4)))
        fmt = "0.0%" if k in prozent else ("#,##0.00 €" if k in euro else None)
        if fmt:
            for c in ws[col][1:]:
                c.number_format = fmt
    ws.freeze_panes = "B2"
    if zeilen:
        ws.auto_filter.ref = ws.dimensions
    return ws


def schreiben(pfad: Path, erg, heute: dt.date, ansprechpartner) -> None:
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Übersicht"
    aktiv = [r for r in erg.zeilen if not r["lead"].gesperrt and not r["lead"].ausgeschlossen]
    versandfaehig = sum(1 for r in aktiv if r["lead"].werte.get("strasse") and r["lead"].werte.get("plz"))
    ev_summe = sum(max(r["ev"], 0) for r in aktiv)
    m = erg.modell
    kpi = [
        ("Stand", heute.isoformat()),
        ("Gelesene Datensätze", erg.protokoll["datensaetze"]),
        ("Eindeutige Leads", erg.protokoll["leads"]),
        ("Zusammengeführte Dubletten", erg.protokoll["dubletten"]),
        ("Gesperrt (Widerspruch/kein Interesse)", sum(r["lead"].gesperrt for r in erg.zeilen)),
        ("Aussortiert (Prüfung/Ausschlusslisten)", sum(bool(r["lead"].ausgeschlossen) for r in erg.zeilen)),
        ("Aktiv", len(aktiv)),
        ("davon briefversandfähig (Straße + PLZ)", versandfaehig),
        ("davon akut", sum(r["lead"].akut for r in aktiv)),
        ("Summe EV aller aktiven Leads (ein Brief je Lead)", round(ev_summe, 2)),
        ("Abgeschlossene Kontaktversuche / positive Reaktionen", f"{m.versuche} / {m.erfolge}"),
        ("Fällige Aufgaben (7 Tage)", len(erg.aufgaben)),
        ("Neue Charge", f"{erg.charge_id} ({len(erg.charge)} Briefe, zur Freigabe)" if erg.charge_id else "keine (offene Charge vorhanden oder keine Kandidaten)"),
    ]
    ws.append(["Vertriebscockpit KUHN"])
    ws["A1"].font = Font(bold=True, size=14)
    for k, v in kpi:
        ws.append([k, v])
    ws.append([])
    ws.append(["Trichter (Kadenzstatus)", "Leads"])
    ws.cell(ws.max_row, 1).font = Font(bold=True)
    for k, v in sorted(erg.trichter.items(), key=lambda x: -x[1]):
        ws.append([k, v])
    ws.column_dimensions["A"].width = 52
    ws.column_dimensions["B"].width = 60

    kopf = ["Rang", "Lead-ID", "Firma", "Ansprechpartner", "Branche", "Signal", "Ort", "PLZ", "Straße", "Telefon", "Website", "Probleme", "Akut",
            "Score Liste", "p Reaktion", "Edition", "Zusatzmodule", "Monat netto", "Einmalig netto", "CLV", "EV je Brief", "Kadenzstatus",
            "Nächste Aufgabe", "fällig", "versandfähig", "Herkunft"]
    zeilen = []
    for i, r in enumerate(aktiv, 1):
        le, z, st = r["lead"], r["zuordnung"], r["stand"]
        w = le.werte
        zeilen.append([i, le.id, le.firma, ansprechpartner(le), z.branche, r["signal"], w.get("ort", ""), w.get("plz", ""), w.get("strasse", ""),
                       w.get("telefon", ""), w.get("website", ""), ", ".join(le.probleme), "AKUT" if le.akut else "", le.score, r["p"],
                       z.edition.capitalize(), z.modul_codes, z.monat_gesamt, z.einmalig_gesamt, z.clv, r["ev"], st.status, st.aufgabe,
                       st.faellig.isoformat() if st.faellig else "", "ja" if w.get("strasse") and w.get("plz") else "nein",
                       " · ".join(le.herkunft)[:200]])
    _blatt(wb, "Pipeline (priorisiert)", kopf, zeilen, {"Firma": 36, "Herkunft": 50, "Nächste Aufgabe": 36, "Straße": 24},
           prozent=("p Reaktion",), euro=("Monat netto", "Einmalig netto", "CLV", "EV je Brief"))

    if erg.charge:
        kopf = ["Rang", "Lead-ID", "Art", "Auswahl", "Firma", "Ansprechpartner", "Ort", "Branche", "Signal", "Edition", "Zusatzmodule", "p Reaktion", "EV je Brief"]
        zeilen = [[k["rang"], k["lead"].id, k["art"], k["auswahl"], k["lead"].firma, ansprechpartner(k["lead"]), k["lead"].werte.get("ort", ""),
                   k["zuordnung"].branche, k["signal"], k["zuordnung"].edition.capitalize(), k["zuordnung"].modul_codes, k["p"], k["ev"]]
                  for k in erg.charge]
        _blatt(wb, f"Charge {erg.charge_id}", kopf, zeilen, {"Firma": 36}, prozent=("p Reaktion",), euro=("EV je Brief",))

    idx = {r["lead"].id: r for r in erg.zeilen}
    zeilen = [[s.faellig.isoformat(), s.aufgabe, s.kanal, idx[lid]["lead"].firma, lid, s.status, idx[lid]["lead"].werte.get("telefon", "")
               if "Einwilligung" in s.kanal else "", "überfällig" if s.faellig < heute else ""] for lid, s in erg.aufgaben]
    _blatt(wb, "Aufgaben", ["fällig", "Aufgabe", "Kanal", "Firma", "Lead-ID", "Status", "Telefon (nur mit Einwilligung)", "Hinweis"], zeilen,
           {"Aufgabe": 50, "Firma": 36})

    kopf = ["Code", "Modul", "Leads", "MRR-Potenzial", "Einmal-Potenzial", "erwarteter MRR (p-gewichtet)", "Status", "Begründung"]
    _blatt(wb, "Upsell-Potenzial", kopf, [[e[k] for k in kopf] for e in erg.upsell], {"Modul": 40, "Begründung": 70, "Status": 30},
           euro=("MRR-Potenzial", "Einmal-Potenzial", "erwarteter MRR (p-gewichtet)"))

    seg = m.segmente()
    kopf = ["Dimension", "Segment", "Leads", "Prior μ", "Versuche n", "Erfolge k", "Posterior θ̂", "Lift λ", "90%-Intervall"]
    _blatt(wb, "Segmente (Bayes)", kopf, [[s[k] for k in kopf] for s in seg], {"Segment": 24, "90%-Intervall": 18}, prozent=("Prior μ", "Posterior θ̂"))

    if erg.budget:
        kopf = ["Quelle", "Branche", "Datensätze", "neu", "Abrufe (≈ URLs)", "Neuquote", "neue Leads je Abruf (geschrumpft)", "mittleres p",
                "Wert je Abruf (€, vor p_abschluss)", "UCB", "Empfehlung Abrufe/h"]
        _blatt(wb, "Abrufbudget", kopf, [[z[k] for k in kopf] for z in erg.budget["quellen"]], {"Quelle": 50},
               prozent=("Neuquote", "mittleres p"), euro=("Wert je Abruf (€, vor p_abschluss)",))

    gesperrt = [[r["lead"].id, r["lead"].firma, r["lead"].werte.get("ort", ""), r["lead"].sperrgrund or r["lead"].ausgeschlossen,
                 "gesperrt" if r["lead"].gesperrt else "aussortiert"] for r in erg.zeilen if r["lead"].gesperrt or r["lead"].ausgeschlossen]
    _blatt(wb, "Gesperrt und aussortiert", ["Lead-ID", "Firma", "Ort", "Grund", "Art"], gesperrt, {"Firma": 36, "Grund": 60})

    quellen = [[k, v["leads"], v["ausschluesse"]] for k, v in erg.protokoll["dateien"].items()]
    _blatt(wb, "Quellen", ["Datei", "Datensätze", "Ausschlüsse"], quellen, {"Datei": 70})
    _blatt(wb, "Methodik und Recht", ["Thema", "Festlegung"], [list(x) for x in METHODIK], {"Thema": 18, "Festlegung": 140})
    pfad.parent.mkdir(parents=True, exist_ok=True)
    wb.save(pfad)


def kontaktkosten_text(katalog: dict) -> str:
    return f"{scoring.kontaktkosten(katalog['versand']):.2f} €"
