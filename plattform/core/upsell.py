"""Upsell: datengetriebene Empfehlungen je Kunde und ein Klick bis zum Angebot.

Quellen der Empfehlungen (keine Speicherung, jederzeit neu berechnet):
- Erneuerungskalender (core/lebenszyklus): überfällige und binnen zwölf Monaten fällige Bauteile/Prüfungen mit Kostenrahmen.
- Ausfallrisiko (core/risiko): Anlagen mit hoher Zwölf-Monats-Ausfallwahrscheinlichkeit → Austausch prüfen.
- Wartungsvertrag: Kunden mit Anlagen, aber ohne aktiven Vertrag (Preis aus Stammdaten `wartungsvertrag_preis_jahr` oder aus der
  Wartungsleistung des Katalogs abgeleitet).
- Förderung: fossile Wärmeerzeuger ab 20 Jahren → Heizungstausch mit KfW-458-Zuschuss (core/foerderung).
- Pakete je Branche (PAKETE): saisonale und anlassbezogene Zusatzleistungen (Klimaservice im Frühjahr, Reifeneinlagerung, hydraulischer
  Abgleich, Pflegeprodukt nach Färbung …), gefiltert nach Anlagentyp, Monat und Stichworten im Anliegen.
- Wiederkehr (Salons/Praxen): Folgetermin, wenn der letzte Besuch länger als das Wiederkehr-Intervall zurückliegt.

Abgelehnte oder bereits angebotene Empfehlungen werden 180 Tage unterdrückt (Ereignisse upsell.abgelehnt / upsell.angeboten).
Werbliche Ansprache (Kampagne im Tageslauf) nur mit Einwilligung E-Mail/SMS (§ 7 UWG) und als Freigabe Klasse B.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import (
    angebote,
    branchen,
    domaene,
    foerderung,
    hinweise,
    lebenszyklus,
    risiko,
    vertraege,
)
from core.domaene import als_utc
from core.modelle import (
    Angebot,
    Anlage,
    Artikel,
    Bauteil,
    Ereignis,
    Kunde,
    Mandant,
    Objekt,
    Servicefall,
    Termin,
    Vertrag,
)

UNTERDRUECKUNG_TAGE = 180
STANDARD_UST = Decimal("19")


@dataclass(frozen=True)
class Paket:
    schluessel: str
    bezeichnung: str
    beschreibung: str
    preis_brutto: str
    anlagentypen: tuple[str, ...] = ()
    monate: tuple[int, ...] = ()
    stichworte: tuple[str, ...] = ()
    dauer_minuten: int = 60


def _p(schluessel, bez, text, preis, anlagentypen=(), monate=(), stichworte=(), dauer=60) -> Paket:
    return Paket(schluessel, bez, text, preis, tuple(anlagentypen), tuple(monate), tuple(stichworte), dauer)


# Branchenpakete; "*" gilt für alle Branchen mit Anlagen
PAKETE: dict[str, list[Paket]] = {
    "shk": [
        _p("hydraulischer_abgleich", "Hydraulischer Abgleich", "Bis zu 15 % weniger Heizenergie; Voraussetzung für Förderung nach BEG.", "449.00", ("gas_brennwert", "oel", "waermepumpe", "pellet"), stichworte=("heizung", "wartung", "heizkörper")),
        _p("thermostate", "Smarte Thermostatventile", "Raumweise Zeitsteuerung, Fensteroffen-Erkennung; schnell amortisiert.", "289.00", ("gas_brennwert", "oel", "waermepumpe", "pellet")),
        _p("wasserfilter_rueckspuel", "Rückspülfilter prüfen und tauschen", "Schutz vor Partikeln im Trinkwasser nach DIN 1988; Filtereinsatz alle 6 Monate rückspülen.", "189.00", ("trinkwasser",), stichworte=("wasser", "druck")),
        _p("legionellen_pruefung", "Legionellenprüfung Trinkwarmwasser", "Pflicht in Mehrfamilienhäusern mit Großanlage (TrinkwV § 31), empfohlen bei Speicher > 400 l.", "149.00", ("trinkwasser",)),
        _p("klima_service", "Klimaanlagen-Service vor dem Sommer", "Filter, Kältemittel, Kondensatablauf, Desinfektion; Dichtheitsprüfung nach F-Gase-Verordnung.", "179.00", ("klima",), monate=(3, 4, 5)),
        _p("frostschutz_check", "Frost-Check Außenleitungen", "Außenzapfstellen, Gartenleitungen und Dachrinnenheizung vor dem ersten Frost.", "89.00", (), monate=(10, 11)),
        _p("bad_barrierefrei", "Beratung barrierefreies Bad", "KfW-Zuschuss 455-B bzw. Pflegekasse bis 4.180 €; Bestandsaufnahme vor Ort.", "0.00", ("trinkwasser",), stichworte=("bad", "dusche", "wanne")),
    ],
    "elektro": [
        _p("e_check", "E-CHECK der Elektroanlage", "Prüfung nach DIN VDE 0100-600/0105-100 mit Prüfprotokoll; von Versicherern anerkannt.", "249.00", ("elektroanlage",)),
        _p("wallbox", "Wallbox 11 kW mit Anmeldung", "Installation, Netzbetreiber-Anmeldung, Lastmanagement; förderfähig je Bundesland.", "1890.00", ("elektroanlage", "pv"), stichworte=("auto", "laden", "wallbox")),
        _p("speicher_nachruesten", "Batteriespeicher nachrüsten", "Eigenverbrauch der PV-Anlage von 30 auf bis zu 70 % steigern.", "6900.00", ("pv",)),
        _p("rauchmelder", "Rauchwarnmelder-Wartung", "Jährliche Prüfung nach DIN 14676; Austausch nach zehn Jahren.", "59.00", ("elektroanlage",)),
    ],
    "kfz": [
        _p("klimaservice", "Klimaservice", "Kältemittel prüfen und auffüllen, Desinfektion, Innenraumfilter – vor dem Sommer.", "129.00", ("fahrzeug",), monate=(3, 4, 5, 6), stichworte=("klima", "kühlt")),
        _p("reifen_einlagerung", "Reifenwechsel mit Einlagerung", "Saisonwechsel, Wuchten, Einlagerung und Reifencheck; wir erinnern Sie automatisch.", "89.00", ("fahrzeug",), monate=(3, 4, 10, 11), stichworte=("reifen",)),
        _p("innenreinigung", "Innen- und Außenaufbereitung", "Beim Werkstattbesuch gleich mit: Innenreinigung, Lederpflege, Lackversiegelung.", "149.00", ("fahrzeug",)),
        _p("scheibenversiegelung", "Scheibenwischer und Scheibenversiegelung", "Neue Wischerblätter plus Nanoversiegelung für bessere Sicht bei Regen.", "59.00", ("fahrzeug",), monate=(9, 10, 11)),
        _p("batterie_check", "Batterie- und Lichttest vor dem Winter", "Kapazitätsmessung, Ladesystem, Beleuchtung; häufigste Pannenursache im Winter.", "29.00", ("fahrzeug",), monate=(10, 11, 12)),
        _p("hu_vorbereitung", "HU-Vorbereitung mit Vorab-Check", "Licht, Bremsen, Abgas vor der Hauptuntersuchung; spart Nachprüfung.", "49.00", ("fahrzeug",), stichworte=("hu", "tüv", "hauptuntersuchung")),
    ],
    "fahrrad": [
        _p("fruehjahrscheck", "Frühjahrscheck", "Bremsen, Schaltung, Kette, Reifen – bevor die Saison beginnt.", "49.00", ("fahrrad",), monate=(2, 3, 4)),
        _p("ebike_akku", "E-Bike-Akku-Diagnose", "Kapazitätsmessung und Software-Update des Antriebs.", "29.00", ("fahrrad",), stichworte=("akku", "e-bike", "motor")),
        _p("wintereinlagerung", "Wintereinlagerung mit Service", "Trocken eingelagert, im Frühjahr fahrbereit.", "79.00", ("fahrrad",), monate=(10, 11)),
    ],
    "dachdecker": [
        _p("dachcheck_herbst", "Dach-Check vor dem Winter", "Sichtprüfung, Dachrinnen reinigen, lose Ziegel – vor Sturm und Schnee.", "189.00", ("dach",), monate=(9, 10, 11)),
        _p("pv_reinigung", "PV-Reinigung und Ertragscheck", "Bis zu 10 % mehr Ertrag; Prüfung der Unterkonstruktion.", "249.00", ("pv",), monate=(3, 4, 5)),
        _p("daemmung_beratung", "Dämmberatung oberste Geschossdecke", "GEG-Pflicht prüfen, Förderung bis 20 % (BEG EM).", "0.00", ("dach",)),
    ],
    "gartenbau": [
        _p("fruehjahrsschnitt", "Frühjahrsschnitt und Beetpflege", "Gehölzschnitt, Rückschnitt, Mulchen – Saisonstart.", "249.00", ("gartenanlage",), monate=(2, 3)),
        _p("bewaesserung", "Automatische Bewässerung", "Tropfbewässerung mit Zeitsteuerung; spart Wasser und Zeit.", "1290.00", ("gartenanlage",), monate=(4, 5, 6)),
        _p("winterdienst", "Winterdienst-Vertrag", "Räumen und Streuen nach Satzung; Haftung abgesichert.", "0.00", ("gartenanlage",), monate=(9, 10, 11)),
    ],
    "friseur": [_p("pflege_zuhause", "Pflegeprodukt für Zuhause", "Passend zur Farbe: Farbschutzshampoo und Maske.", "34.90", stichworte=("farbe", "färben", "strähnen")),
                _p("kopfhautmassage", "Kopfhautmassage und Kur", "10 Minuten Entspannung, kräftigt das Haar.", "15.00")],
    "kosmetik": [_p("heimpflege", "Heimpflege-Set", "Ergänzt die Behandlung; auf den Hauttyp abgestimmt.", "49.00"), _p("kur_serie", "Kur-Serie (5er-Karte)", "Fünf Behandlungen zum Preis von vier.", "0.00")],
    "massage": [_p("zehnerkarte", "Zehnerkarte", "Zehn Behandlungen, zwei geschenkt.", "0.00"), _p("waermepackung", "Wärmepackung", "Vor der Massage, löst die Muskulatur.", "12.00")],
    "physiotherapie": [_p("praevention", "Präventionskurs (§ 20 SGB V)", "Rückenschule oder Beckenboden; Krankenkasse erstattet bis 80 %.", "0.00")],
    "hundesalon": [_p("krallen_zaehne", "Krallen- und Zahnpflege", "Beim Besuch gleich mit.", "15.00"), _p("pflegeset", "Pflegeset für Zuhause", "Bürste und Shampoo passend zum Fell.", "29.90")],
    "maler": [_p("fassadencheck", "Fassadencheck", "Risse, Algen, Anstrichzustand; Angebot für Fassadenschutz.", "0.00", monate=(3, 4, 5, 6))],
    "gebaeudereinigung": [_p("glasreinigung", "Glas- und Rahmenreinigung", "Viermal im Jahr zum Festpreis.", "0.00"), _p("grundreinigung", "Grundreinigung", "Jährlich: Böden versiegeln, Polster, Teppiche.", "0.00")],
    "tischler": [_p("pflege_holz", "Holzpflege-Service", "Ölen, Nachjustieren, Beschläge – jährlich.", "149.00")],
    "schluesseldienst": [_p("sicherheitscheck", "Einbruchschutz-Check", "Sicherheitsbeschläge, Zusatzschlösser; KfW-Zuschuss 455-E.", "0.00")],
}

FOSSIL = {"gas_brennwert", "oel", "gas"}


def pakete(b: branchen.Branche) -> list[Paket]:
    return list(PAKETE.get(b.schluessel, []))


def _netto(brutto: str, kleinunternehmer: bool) -> Decimal:
    b = Decimal(brutto)
    return b if kleinunternehmer else (b / (1 + STANDARD_UST / 100)).quantize(Decimal("0.01"))


def _unterdrueckt(s: Session, m: Mandant, kunde_id: str, heute: date) -> set[str]:
    grenze = datetime.combine(heute - timedelta(days=UNTERDRUECKUNG_TAGE), datetime.min.time())
    schluessel = set()
    for e in s.scalars(select(Ereignis).where(Ereignis.mandant_id == m.id, Ereignis.entitaet == "kunde", Ereignis.entitaet_id == kunde_id,
                                              Ereignis.typ.in_(("upsell.abgelehnt", "upsell.angeboten", "upsell.angefordert")), Ereignis.zeit >= grenze)):
        if e.payload.get("schluessel"):
            schluessel.add(e.payload["schluessel"])
    return schluessel


def _wartungspreis(s: Session, m: Mandant, anzahl_anlagen: int) -> Decimal:
    st = m.stammdaten or {}
    if st.get("wartungsvertrag_preis_jahr"):
        return Decimal(str(st["wartungsvertrag_preis_jahr"]))
    wartung = s.scalar(select(Artikel).where(Artikel.mandant_id == m.id, Artikel.kategorie == "leistung", Artikel.bezeichnung.ilike("%wartung%")).order_by(Artikel.nummer))
    basis = Decimal(wartung.verkauf_netto) if wartung is not None else Decimal("189.00")
    return (basis * max(anzahl_anlagen, 1) * Decimal("0.9")).quantize(Decimal("0.01"))   # 10 % Vertragsrabatt gegenüber Einzelwartung


def empfehlungen(s: Session, m: Mandant, kunde: Kunde, heute: date, fall: Servicefall | None = None, mit_unterdrueckung: bool = True) -> list[dict[str, Any]]:
    b = branchen.branche(m)
    st = m.stammdaten or {}
    klein = bool(st.get("kleinunternehmer"))
    anlagen = s.scalars(select(Anlage).join(Objekt, Objekt.id == Anlage.objekt_id).where(Objekt.kunde_id == kunde.id)).all()
    typen = {a.anlagentyp for a in anlagen}
    unterdrueckt = _unterdrueckt(s, m, kunde.id, heute) if mit_unterdrueckung else set()
    aus: list[dict[str, Any]] = []

    def hinzu(schluessel, art, titel, begruendung, brutto, prioritaet, aktion="angebot", anlage_id=None, bauteil_id=None, positionen=None, dauer=None):
        if schluessel in unterdrueckt:
            return
        brutto_d = Decimal(brutto) if brutto not in (None, "", "0.00") else None
        pos = positionen if positionen is not None else ([{"bezeichnung": titel, "menge": 1, "einheit": "psch", "einzelpreis_netto": str(_netto(str(brutto_d), klein)),
                                                           "ust_satz": "0" if klein else str(STANDARD_UST)}] if brutto_d else [])
        aus.append({"schluessel": schluessel, "art": art, "titel": titel, "begruendung": begruendung, "preis_brutto": f"{brutto_d:.2f}" if brutto_d else None,
                    "prioritaet": prioritaet, "aktion": aktion, "anlage_id": anlage_id, "bauteil_id": bauteil_id, "positionen": pos, "dauer_minuten": dauer})

    # 1) Erneuerungskalender
    if anlagen:
        kal = lebenszyklus.kalender(s, m, kunde, heute, jahre=2)
        for e in kal["ueberfaellig"]:
            hinzu(f"erneuerung:{e['id']}", "erneuerung", f"{'Prüfung' if e['pflicht'] else 'Erneuerung'}: {e['bezeichnung']}",
                  f"Überfällig seit {e['faellig']}" + (f" ({e['rechtsgrundlage']})" if e.get("rechtsgrundlage") else "") + ".", e.get("kosten_schaetzung"), 3,
                  anlage_id=e["anlage_id"], bauteil_id=e["id"])
        for j in kal["jahre"]:
            for e in j["eintraege"]:
                if e["faellig"] and e["faellig"] <= (heute + timedelta(days=365)).isoformat() and e["status"] != "faellig":
                    hinzu(f"erneuerung:{e['id']}", "erneuerung", f"{'Prüfung' if e['pflicht'] else 'Erneuerung'}: {e['bezeichnung']}",
                          f"Fällig am {e['faellig']}; rechtzeitig planen statt im Winter ausfallen.", e.get("kosten_schaetzung"), 2, anlage_id=e["anlage_id"], bauteil_id=e["id"])
    # 2) Ausfallrisiko
    for a in anlagen:
        try:
            r = risiko.bewerten(s, a, heute)
        except Exception:   # Anlage ohne Risikomodell (unbekannter Typ)
            continue
        if r["p_12_monate"] >= 0.25:
            kosten = s.scalar(select(Bauteil.kosten_schaetzung).where(Bauteil.anlage_id == a.id, Bauteil.art.in_(("waermeerzeuger", "kompressor", "motor", "verdichter"))))
            hinzu(f"risiko:{a.id}", "risiko", f"Austausch prüfen: {r['bezeichnung']}" + (f" {a.hersteller}" if a.hersteller else ""),
                  f"Ausfallwahrscheinlichkeit {round(r['p_12_monate'] * 100)} % in zwölf Monaten ({', '.join(r['treiber']) or 'Alter'}). " + " ".join(r.get("empfehlungen", [])[:1]),
                  kosten, 3 if r["klasse"] == "kritisch" else 2, anlage_id=a.id)
        # 3) Förderung Heizungstausch
        if a.anlagentyp in FOSSIL and a.baujahr and heute.year - a.baujahr >= 20:
            try:
                f = foerderung.heizung("25000", "waermepumpe", alte_heizung_ersetzt=True)
                zuschuss = int(f["zuschuss_gesamt"])
            except Exception:
                zuschuss = None
            hinzu(f"foerderung:{a.id}", "foerderung", "Heizungstausch mit Förderung (KfW 458)",
                  f"Wärmeerzeuger Baujahr {a.baujahr}: ab 20 Jahren Zuschuss bis 70 % mit Klimageschwindigkeits-Bonus" + (f", bei 25.000 € ca. {zuschuss} €" if zuschuss else "") + ". Beratung und Förderantrag inklusive.",
                  None, 2, aktion="beratung", anlage_id=a.id)
    # 4) Wartungsvertrag
    aktiv = s.scalar(select(Vertrag).where(Vertrag.kunde_id == kunde.id, Vertrag.status == "aktiv"))
    if anlagen and aktiv is None and b.begriffe.get("anlagen"):
        preis = _wartungspreis(s, m, len(anlagen))
        brutto = preis if klein else (preis * (1 + STANDARD_UST / 100)).quantize(Decimal("0.01"))
        hinzu("wartungsvertrag", "vertrag", f"Wartungsvertrag für {len(anlagen)} Anlage{'n' if len(anlagen) > 1 else ''}",
              "Jährliche Wartung mit Terminvorrang, 10 % Rabatt gegenüber Einzelwartung, Gewährleistung des Herstellers bleibt erhalten.",
              f"{brutto:.2f}", 2, aktion="vertrag", positionen=[{"bezeichnung": "Wartungsvertrag (Jahrespreis)", "menge": 1, "einheit": "Jahr", "einzelpreis_netto": str(preis), "ust_satz": "0" if klein else "19"}])
    # 5) Pakete
    anliegen = (fall.anliegen if fall else "").lower()
    gekauft = {p["bezeichnung"] for a in s.scalars(select(Angebot).where(Angebot.kunde_id == kunde.id, Angebot.status == "angenommen", Angebot.datum >= heute - timedelta(days=365))) for p in a.positionen}
    for p in pakete(b):
        treffer_monat = heute.month in p.monate if p.monate else False
        treffer_wort = any(w in anliegen for w in p.stichworte) if p.stichworte else False
        if p.anlagentypen and not (typen & set(p.anlagentypen)) and not treffer_wort:
            continue
        if p.bezeichnung in gekauft:
            continue
        if p.monate and not treffer_monat and not treffer_wort:
            continue
        hinzu(f"paket:{p.schluessel}", "paket", p.bezeichnung, p.beschreibung, p.preis_brutto, 2 if (treffer_monat or treffer_wort) else 1,
              aktion="angebot" if p.preis_brutto != "0.00" else "beratung", dauer=p.dauer_minuten)
    # 6) Wiederkehr
    if b.wiederkehr_tage:
        letzter = s.scalar(select(Termin).join(Servicefall, Servicefall.id == Termin.servicefall_id).where(Servicefall.kunde_id == kunde.id, Termin.status == "bestaetigt").order_by(Termin.beginn.desc()))
        if letzter is not None and (heute - als_utc(letzter.beginn).date()).days > b.wiederkehr_tage and als_utc(letzter.beginn).date() < heute:
            hinzu("folgetermin", "wiederkehr", "Folgetermin vereinbaren", f"Letzter Besuch vor {(heute - als_utc(letzter.beginn).date()).days} Tagen; empfohlen alle {b.wiederkehr_tage} Tage.", None, 1, aktion="buchung")
    aus.sort(key=lambda x: (-x["prioritaet"], x["art"], x["titel"]))
    return aus


def potenzial(empf: list[dict[str, Any]]) -> Decimal:
    return sum((Decimal(e["preis_brutto"]) for e in empf if e["preis_brutto"]), Decimal(0))


def finden(s: Session, m: Mandant, kunde: Kunde, schluessel: str, heute: date, fall: Servicefall | None = None, mit_unterdrueckung: bool = False) -> dict[str, Any]:
    for e in empfehlungen(s, m, kunde, heute, fall, mit_unterdrueckung=mit_unterdrueckung):
        if e["schluessel"] == schluessel:
            return e
    raise domaene.Domaenenfehler(f"Empfehlung {schluessel} ist für diesen Kunden nicht (mehr) verfügbar")


def angebot_erstellen(s: Session, m: Mandant, kunde: Kunde, schluessel: str, heute: date, akteur: str = "mensch", akteur_name: str | None = None,
                      servicefall_id: str | None = None, versenden: bool = False) -> Angebot:
    fall = s.get(Servicefall, servicefall_id) if servicefall_id else None
    e = finden(s, m, kunde, schluessel, heute, fall, mit_unterdrueckung=True)
    if not e["positionen"]:
        raise domaene.Domaenenfehler("Diese Empfehlung ist eine Beratung ohne Festpreis; bitte Angebot manuell kalkulieren")
    a = angebote.angebot_erzeugen(s, m, {"kunde_id": kunde.id, "servicefall_id": servicefall_id, "titel": e["titel"], "einleitung": e["begruendung"], "positionen": e["positionen"]}, heute, akteur_name)
    domaene.protokollieren(s, m.id, "upsell.angeboten", "kunde", kunde.id, akteur, {"schluessel": schluessel, "art": e["art"], "angebot_id": a.id, "brutto": a.brutto, "quelle": "rapport" if fall else "buero"}, akteur_name)
    if versenden:
        angebote.versand_beantragen(s, m, a, akteur_name)
    return a


def vertrag_erstellen(s: Session, m: Mandant, kunde: Kunde, heute: date, akteur_name: str | None = None, beginn: date | None = None) -> Vertrag:
    e = finden(s, m, kunde, "wartungsvertrag", heute, mit_unterdrueckung=True)
    anlagen = s.scalars(select(Anlage).join(Objekt, Objekt.id == Anlage.objekt_id).where(Objekt.kunde_id == kunde.id)).all()
    v = vertraege.vertrag_anlegen(s, m, {"kunde_id": kunde.id, "bezeichnung": "Wartungsvertrag", "anlagen_ids": [a.id for a in anlagen], "beginn": beginn or heute,
                                         "leistungen": "Jährliche Wartung aller Anlagen, Terminvorrang, 10 % Rabatt auf Einzelleistungen",
                                         "preis_jahr_netto": e["positionen"][0]["einzelpreis_netto"]}, heute, akteur_name)
    domaene.protokollieren(s, m.id, "upsell.angeboten", "kunde", kunde.id, "mensch", {"schluessel": "wartungsvertrag", "art": "vertrag", "vertrag_id": v.id, "brutto": e["preis_brutto"]}, akteur_name)
    return v


def ablehnen(s: Session, m: Mandant, kunde: Kunde, schluessel: str, grund: str | None, akteur: str = "mensch", akteur_name: str | None = None) -> None:
    domaene.protokollieren(s, m.id, "upsell.abgelehnt", "kunde", kunde.id, akteur, {"schluessel": schluessel, "grund": (grund or "")[:300] or None}, akteur_name)


def anfordern(s: Session, m: Mandant, f: Servicefall, schluessel: str, jetzt: datetime) -> dict[str, Any]:
    """Kunde fordert im Portal ein Angebot zu einer Empfehlung an: Angebotsentwurf fürs Büro, Hinweis, Kommunikation."""
    from core.portal import _kommunikation
    heute = als_utc(jetzt).date()
    e = finden(s, m, f.kunde, schluessel, heute, f)
    a = None
    if e["positionen"]:
        a = angebote.angebot_erzeugen(s, m, {"kunde_id": f.kunde_id, "servicefall_id": f.id, "titel": e["titel"], "einleitung": e["begruendung"], "positionen": e["positionen"]}, heute, f.kunde.name)
    domaene.protokollieren(s, m.id, "upsell.angefordert", "kunde", f.kunde_id, "mensch", {"schluessel": schluessel, "art": e["art"], "angebot_id": a.id if a else None, "servicefall_id": f.id}, f.kunde.name)
    _kommunikation(s, f, f"Kunde interessiert sich im Portal für: {e['titel']}" + (f" (Angebotsentwurf {a.nummer})" if a else ""), jetzt)
    hinweise.senden(s, m, "info", "Angebotswunsch im Portal", f"{f.kunde.name}: {e['titel']}" + (f" – Entwurf {a.nummer} prüfen und senden" if a else " – bitte Angebot kalkulieren"), f"upsell:{f.id}:{schluessel}", "angebote")
    return {"hinweis": "Vielen Dank, wir senden Ihnen in Kürze ein Angebot." if a else "Vielen Dank, wir melden uns zur Beratung.", "angebot_id": a.id if a else None}


def uebersicht(s: Session, m: Mandant, heute: date, limit: int = 30) -> dict[str, Any]:
    grenze = datetime.combine(heute - timedelta(days=90), datetime.min.time())
    angeboten = s.scalars(select(Ereignis).where(Ereignis.mandant_id == m.id, Ereignis.typ.in_(("upsell.angeboten", "upsell.angefordert")), Ereignis.zeit >= grenze)).all()
    angebot_ids = [e.payload.get("angebot_id") for e in angeboten if e.payload.get("angebot_id")]
    angenommen = s.scalars(select(Angebot).where(Angebot.id.in_(angebot_ids), Angebot.status == "angenommen")).all() if angebot_ids else []
    vertraege_n = sum(1 for e in angeboten if e.payload.get("vertrag_id"))
    kunden_aus = []
    gesamt = Decimal(0)
    for k in s.scalars(select(Kunde).where(Kunde.mandant_id == m.id)):
        empf = empfehlungen(s, m, k, heute)
        if not empf:
            continue
        p = potenzial(empf)
        gesamt += p
        kunden_aus.append({"kunde_id": k.id, "name": k.name, "ort": k.ort, "anzahl": len(empf), "potenzial": f"{p:.2f}", "prioritaet": max(e["prioritaet"] for e in empf),
                           "top": [{"schluessel": e["schluessel"], "titel": e["titel"], "preis_brutto": e["preis_brutto"], "art": e["art"]} for e in empf[:3]]})
    kunden_aus.sort(key=lambda x: (-x["prioritaet"], -Decimal(x["potenzial"])))
    je_art: dict[str, int] = {}
    for e in angeboten:
        je_art[e.payload.get("art", "?")] = je_art.get(e.payload.get("art", "?"), 0) + 1
    return {"potenzial": f"{gesamt:.2f}", "kunden_mit_empfehlung": len(kunden_aus), "kunden": kunden_aus[:limit],
            "letzte_90_tage": {"angeboten": len(angeboten), "angenommen": len(angenommen) + vertraege_n, "umsatz_angenommen": f"{sum((Decimal(a.brutto) for a in angenommen), Decimal(0)):.2f}",
                               "quote": round((len(angenommen) + vertraege_n) / len(angeboten), 2) if angeboten else None, "je_art": je_art}}


def kampagne(s: Session, m: Mandant, jetzt: datetime, maximal: int = 20) -> int:
    """Tageslauf: Kunden mit hoher Empfehlung einmal je 180 Tage ansprechen (Freigabe B, nur mit Einwilligung E-Mail/SMS)."""
    heute = als_utc(jetzt).date()
    grenze = datetime.combine(heute - timedelta(days=UNTERDRUECKUNG_TAGE), datetime.min.time())
    n = 0
    for k in s.scalars(select(Kunde).where(Kunde.mandant_id == m.id)):
        if n >= maximal:
            break
        ew = k.einwilligungen or {}
        if not (ew.get("email") or ew.get("sms")) or not (k.email or k.telefon):
            continue
        if s.scalar(select(Ereignis).where(Ereignis.mandant_id == m.id, Ereignis.typ == "upsell.erinnert", Ereignis.entitaet_id == k.id, Ereignis.zeit >= grenze)):
            continue
        empf = [e for e in empfehlungen(s, m, k, heute) if e["prioritaet"] >= 2][:3]
        if not empf:
            continue
        zeilen = "\n".join(f"- {e['titel']}" + (f" (ca. {e['preis_brutto'].replace('.', ',')} €)" if e["preis_brutto"] else "") + f": {e['begruendung']}" for e in empf)
        text = (f"Guten Tag {k.name},\n\nbei der Durchsicht Ihrer Daten sind uns Punkte aufgefallen, die sich für Sie lohnen:\n{zeilen}\n\n"
                f"Antworten Sie kurz oder rufen Sie uns an: {(m.stammdaten or {}).get('telefon', '')}\n\nMit freundlichen Grüßen\n{m.name}")
        domaene.aktion_beantragen(s, m, "nachricht_an_endkunde", {"kunde_id": k.id, "betreff": f"Empfehlungen von {m.name}", "text": text,
                                                                   "kurz": f"{m.name}: {len(empf)} Empfehlung(en) für Sie – wir melden uns."}, "system", None, "Upsell-Kampagne")
        domaene.protokollieren(s, m.id, "upsell.erinnert", "kunde", k.id, "system", {"schluessel": [e["schluessel"] for e in empf]})
        n += 1
    return n
