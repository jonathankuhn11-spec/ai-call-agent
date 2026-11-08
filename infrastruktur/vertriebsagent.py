"""Vertriebs-Agenten: steuern die Kette vom Lead bis zum Abschluss, ohne das Abrufkontingent zu belasten.

    python infrastruktur/vertriebsagent.py lauf      [--wurzel /mnt/project-files/leads] [--charge 25] [--keine-charge] [--heute JJJJ-MM-TT]
    python infrastruktur/vertriebsagent.py einlesen  <Charge_…_Freigabe.xlsx>     # Freigabe, Versanddatum, Reaktionen übernehmen
    python infrastruktur/vertriebsagent.py ereignis  <Lead-ID> <typ> [--datum …] [--notiz …]
    python infrastruktur/vertriebsagent.py status

Agenten (Module unter vertrieb/):
  Erfassungs-Agent      alle Leadlisten lesen, Schemata angleichen, Dubletten zusammenführen, Sperr-/Ausschlusslisten
  Upsell-Agent          Edition + Zusatzmodule je Lead aus Problemcodes, Branche, Stellenanzeige; Kundenwert (CLV)
  Priorisierungs-Agent  Erwartungswert je Brief, Empirical-Bayes-Rückkopplung aus dem Rücklauf, Thompson-Exploration
  Nachfass-Agent        Kadenzstatus und fällige Aufgaben je Lead (Brief 2 nach 14 T., Rückruf nach Rückmeldung …)
  Versand-Agent         Briefcharge zur Freigabe, Rückmeldebogen, Serienbriefdatei und Paketliste nach Freigabe
  Abrufbudget-Agent     Wert je Webseitenabruf je Quelle × Branche, Empfehlung für die Wellen-Agenten (JSON)
  Cockpit-Agent         Vertriebscockpit.xlsx mit allen Sichten

Ausgabe unter <wurzel>/vertrieb/: pipeline.db (Historie, nie im Repository), Vertriebscockpit.xlsx,
chargen/<ID>/Charge_<ID>_Freigabe.xlsx, abrufbudget.json. Es wird nichts versendet und keine Webseite abgerufen.
SQLite arbeitet auf einer lokalen Kopie; die Datei auf dem Netzlaufwerk wird erst am Ende ersetzt.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import tempfile
from pathlib import Path

BASIS = Path(__file__).resolve().parent
sys.path.insert(0, str(BASIS))

from vertrieb import budget, cockpit, pipeline, speicher, upsell, versand

STANDARD_WURZEL = Path("/mnt/project-files/leads")


class LokaleDB:
    """Kopiert pipeline.db vom (Netz-)Laufwerk in ein lokales Temp-Verzeichnis und schreibt sie atomar zurück."""

    def __init__(self, ziel: Path):
        self.ziel = ziel
        self.tmp = Path(tempfile.mkdtemp(prefix="vertrieb-"))
        self.lokal = self.tmp / "pipeline.db"

    def __enter__(self) -> Path:
        if self.ziel.exists():
            shutil.copy2(self.ziel, self.lokal)
        return self.lokal

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.ziel.parent.mkdir(parents=True, exist_ok=True)
            zwischen = self.ziel.with_suffix(".db.neu")
            shutil.copy2(self.lokal, zwischen)
            zwischen.replace(self.ziel)
        shutil.rmtree(self.tmp, ignore_errors=True)


def _heute(text: str | None) -> dt.date:
    return dt.date.fromisoformat(text) if text else dt.date.today()


def cmd_lauf(args) -> int:
    heute = _heute(args.heute)
    out = args.wurzel / "vertrieb"
    with LokaleDB(out / "pipeline.db") as db_pfad:
        erg = pipeline.lauf(args.wurzel, db_pfad, heute, args.charge, not args.keine_charge)
    cockpit.schreiben(out / "Vertriebscockpit.xlsx", erg, heute, pipeline.ansprechpartner)
    budget.schreiben(out / "abrufbudget.json", erg.budget)
    if erg.charge_id:
        ordner = out / "chargen" / erg.charge_id
        versand.bogen_schreiben(ordner / f"Charge_{erg.charge_id}_Freigabe.xlsx", erg.charge_id, erg.charge, pipeline.ansprechpartner)
    p = erg.protokoll
    print(f"{p['datensaetze']} Datensätze aus {len(p['dateien'])} Listen → {p['leads']} Leads ({p['dubletten']} Dubletten zusammengeführt)")
    print(f"aktiv {sum(1 for r in erg.zeilen if not r['lead'].gesperrt and not r['lead'].ausgeschlossen)}, "
          f"Aufgaben {len(erg.aufgaben)}, Kontaktversuche {erg.modell.versuche}, positive Reaktionen {erg.modell.erfolge}")
    if erg.charge_id:
        print(f"Charge {erg.charge_id}: {len(erg.charge)} Briefe zur Freigabe → {out / 'chargen' / erg.charge_id}")
    else:
        print("keine neue Charge (offene Charge vorhanden oder keine Kandidaten)")
    print(f"Cockpit: {out / 'Vertriebscockpit.xlsx'}")
    return 0


def cmd_einlesen(args) -> int:
    heute = _heute(args.heute)
    out = args.wurzel / "vertrieb"
    with LokaleDB(out / "pipeline.db") as db_pfad:
        db = speicher.oeffnen(db_pfad)
        bericht = versand.bogen_einlesen(db, args.bogen, heute)
        frei = bericht["freigegeben"]
        if frei:
            leads = {le.id: le for le in pipeline._db_leads(db)}
            katalog, preise = upsell.katalog_laden(), upsell.preise_laden()
            arten = {r["lead_id"]: r["auswahl"].split(" · ")[0] for r in db.execute(
                "SELECT lead_id, auswahl FROM chargen_leads WHERE charge_id=?", (bericht["charge"],))}
            paar = [(leads[i], upsell.zuordnen(leads[i], katalog, preise)) for i in frei if i in leads and not leads[i].gesperrt]
            ordner = args.bogen.parent
            versand.paketliste_schreiben(ordner / "paketliste.xlsx", paar)
            zeilen = []
            for le, z in paar:
                name = pipeline.ansprechpartner(le)
                zeilen.append({"Lead-ID": le.id, "Firma": le.firma, "Ansprechpartner": name,
                               "Anrede": f"Guten Tag {name}," if name else "Sehr geehrte Damen und Herren,",
                               "Straße": le.werte.get("strasse", ""), "PLZ": le.werte.get("plz", ""), "Ort": le.werte.get("ort", ""),
                               "Art": arten.get(le.id, "Erstbrief"), "Edition": z.edition.capitalize(), "Zusatzmodule": z.modul_codes})
            versand.serienbrief_schreiben(ordner / "serienbrief.csv", zeilen)
            bericht["dateien"] = [str(ordner / "paketliste.xlsx"), str(ordner / "serienbrief.csv")]
        db.close()
    print(json.dumps({k: (len(v) if k == "freigegeben" else v) for k, v in bericht.items()}, ensure_ascii=False, indent=1))
    if frei:
        print("Briefe erzeugen: python infrastruktur/paketagent.py", bericht["dateien"][0], "--plattform … --out", args.bogen.parent / "pakete")
    return 1 if bericht["fehler"] else 0


def cmd_ereignis(args) -> int:
    out = args.wurzel / "vertrieb"
    with LokaleDB(out / "pipeline.db") as db_pfad:
        db = speicher.oeffnen(db_pfad)
        neu = speicher.ereignis(db, args.lead, args.typ, args.datum or dt.date.today().isoformat(), args.notiz or "", "manuell")
        db.commit()
        db.close()
    print("erfasst" if neu else "bereits vorhanden")
    return 0


def cmd_status(args) -> int:
    pfad = args.wurzel / "vertrieb" / "pipeline.db"
    if not pfad.exists():
        print("noch kein Lauf")
        return 0
    with LokaleDB(pfad) as db_pfad:
        db = speicher.oeffnen(db_pfad)
        print("Leads:", db.execute("SELECT COUNT(*), SUM(gesperrt) FROM leads").fetchone()[:])
        for r in db.execute("SELECT typ, COUNT(*) n FROM ereignisse GROUP BY typ ORDER BY n DESC"):
            print(f"  {speicher.EREIGNISSE[r['typ']]}: {r['n']}")
        for r in db.execute("SELECT c.id, c.status, COUNT(cl.lead_id) n FROM chargen c LEFT JOIN chargen_leads cl ON cl.charge_id=c.id GROUP BY c.id"):
            print(f"  Charge {r['id']}: {r['status']} ({r['n']} Briefe)")
        db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--wurzel", type=Path, default=STANDARD_WURZEL, help="Ordner mit den Leadlisten (Standard: Projektordner leads/)")
    ap.add_argument("--heute", help="Stichtag JJJJ-MM-TT (Tests, Nachberechnung)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("lauf", help="alle Listen einlesen, priorisieren, Charge vorschlagen, Cockpit schreiben")
    a.add_argument("--charge", type=int, help="Chargengröße (Standard aus katalog.toml)")
    a.add_argument("--keine-charge", action="store_true")
    a.set_defaults(f=cmd_lauf)
    a = sub.add_parser("einlesen", help="Freigabe- und Rückmeldebogen übernehmen")
    a.add_argument("bogen", type=Path)
    a.set_defaults(f=cmd_einlesen)
    a = sub.add_parser("ereignis", help="einzelnes Ereignis erfassen (z. B. Anruf eines Betriebs auf den Brief)")
    a.add_argument("lead")
    a.add_argument("typ", choices=list(speicher.EREIGNISSE))
    a.add_argument("--datum")
    a.add_argument("--notiz")
    a.set_defaults(f=cmd_ereignis)
    a = sub.add_parser("status")
    a.set_defaults(f=cmd_status)
    args = ap.parse_args(argv)
    return args.f(args)


if __name__ == "__main__":
    sys.exit(main())
