"""Realistische, vollständig fiktive Demodaten relativ zum aktuellen Datum.

    python -m core seed            legt den Demo-Mandanten an (einmalig) und gibt seine ID aus
    python -m core serve --demo    startet die API und legt Demodaten an, falls die DB leer ist

Namen, Adressen und Rufnummern sind erfunden und dienen nur der Vorführung von Oberfläche,
Disposition und Digital Twin. Die Rufnummern können zufällig real vergeben sein: niemals anrufen.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import domaene, einsatz
from core.disposition import ZEITZONE
from core.modelle import Anlage, Beleg, Ereignis, Frist, Kommunikation, Kunde, Mandant, Objekt, Ressource, Servicefall, Termin
from core.regeln import dringlichkeit_bewerten, qualifikation_fuer

DEMO_NAME = "Demo Haustechnik Köln (fiktiv)"
WOCHENTAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

KUNDEN = [
    # name, telefon, strasse, plz, ort, vertrag, anlagen [(typ, hersteller, baujahr, monate seit letzter Wartung)]
    ("Familie Achternbusch", "0221 990101", "Lindenallee 4", "50968", "Köln", "wartungsvertrag",
     [("gas_brennwert", "Viessmann", 2014, 13), ("trinkwasser", None, 2014, 10)]),
    ("Dr. Brehmer", "0221 990102", "Am Kirchweg 17", "50858", "Köln", "ohne", [("waermepumpe", "Stiebel Eltron", 2021, 11)]),
    ("Hausverwaltung Carstens", "0221 990103", "Severinstraße 120", "50678", "Köln", "wartungsvertrag",
     [("gas_brennwert", "Buderus", 2009, 14), ("gas_brennwert", "Buderus", 2009, 14), ("trinkwasser", None, 2009, 12)]),
    ("Familie Dörrenbach", "02233 990104", "Kölnstraße 8", "50354", "Hürth", "ohne", [("oel", "Weishaupt", 2005, 25)]),
    ("Eiscafé Ferrante", "0221 990105", "Zülpicher Straße 33", "50674", "Köln", "ohne", [("klima", "Daikin", 2019, 9)]),
    ("Familie Gerhardt", "02236 990106", "Rheinweg 2", "50389", "Wesseling", "wartungsvertrag",
     [("gas_brennwert", "Vaillant", 2017, 11), ("solarthermie", "Vaillant", 2017, 20)]),
    ("Herr Hollmann", "0221 990107", "Venloer Straße 410", "50825", "Köln", "ohne", [("warmwasser", "AEG", 2012, None)]),
    ("Praxis Dr. Ilgner", "0221 990108", "Aachener Straße 222", "50931", "Köln", "wartungsvertrag",
     [("trinkwasser", None, 2016, 11), ("klima", "Mitsubishi Electric", 2020, 12)]),
    ("Familie Jankowski", "0221 990109", "Bergisch Gladbacher Str. 55", "51065", "Köln", "ohne",
     [("waermepumpe", "Viessmann", 2023, 3)]),
    ("Bäckerei Kremer", "02203 990110", "Hauptstraße 71", "51143", "Köln-Porz", "ohne", [("gas_brennwert", "Wolf", 2011, 30)]),
    ("Frau Lindqvist", "0221 990111", "Neusser Straße 300", "50733", "Köln", "ohne", [("sanitaer", None, None, None)]),
    ("Familie Moritz", "02232 990112", "Liblarer Straße 9", "50321", "Brühl", "wartungsvertrag",
     [("gas_brennwert", "Viessmann", 2019, 6)]),
]

# kunde_index, anliegen, symptom, status, tag_offset (None = kein Termin), uhrzeit, monteur_index, termin bestätigt
VORGAENGE = [
    (0, "Heizung ist aus, Therme zeigt Störung F28", "kein Warmwasser seit heute früh", "eingeplant", 0, time(9, 0), 0, True),
    (2, "Heizung im 3. OG wird nicht warm", None, "eingeplant", 1, time(8, 0), 2, True),
    (4, "Klimaanlage tropft im Gastraum", None, "qualifiziert", 1, time(13, 0), 1, False),
    (3, "Ölheizung macht laute Geräusche beim Anspringen", None, "qualifiziert", 2, time(10, 0), 0, False),
    (6, "Warmwasserspeicher wird nicht mehr heiß", None, "eingegangen", None, None, None, False),
    (10, "WC-Spülkasten läuft dauerhaft nach", None, "eingegangen", None, None, None, False),
    (8, "Wärmepumpe zeigt Hochdruckstörung", "seit dem Frost", "eingeplant", 2, time(13, 30), 1, True),
    (11, "Angebot für Badsanierung erbeten", None, "eingegangen", None, None, None, False),
    (5, "Solarthermie: Pumpe läuft nachts", None, "durchgefuehrt", -2, time(10, 0), 2, True),
    (7, "Legionellenprüfung Trinkwasser fällig", None, "abgerechnet", -6, time(8, 0), 0, True),
]

MONTEURE = [  # letztes Feld: Anmeldename in Keycloak (Attribut preferred_username)
    ("Monteur Arndt (Kundendienst Gas)", ["gas", "oel", "trinkwasser"], time(7, 30), time(16, 30), "m.arndt"),
    ("Monteur Bilgin (Kälte/Wärmepumpe)", ["kaeltetechnik", "elektro"], time(7, 0), time(15, 30), "m.bilgin"),
    ("Monteur Cordes (Geselle)", ["gas", "trinkwasser"], time(8, 0), time(17, 0), "m.cordes"),
]


def _monate_zurueck(heute: date, monate: int) -> date:
    m = heute.month - 1 - monate
    return date(heute.year + m // 12, m % 12 + 1, min(heute.day, 28))


def _naechster_werktag(tag: date, versatz: int) -> date:
    schritt = 1 if versatz >= 0 else -1
    rest = abs(versatz)
    while tag.weekday() >= 5:
        tag += timedelta(days=1)
    while rest:
        tag += timedelta(days=schritt)
        if tag.weekday() < 5:
            rest -= 1
    return tag


def demodaten_anlegen(s: Session, jetzt_: datetime | None = None) -> str:
    vorhanden = s.scalar(select(Mandant).where(Mandant.name == DEMO_NAME))
    if vorhanden:
        return vorhanden.id
    jetzt_ = jetzt_ or datetime.now(timezone.utc)
    heute = jetzt_.astimezone(ZEITZONE).date()
    m = Mandant(name=DEMO_NAME, policies={}, stammdaten={  # fiktive Betriebsstammdaten
        "strasse": "Werkstattstraße 12", "plz": "50823", "ort": "Köln", "telefon": "0221 000000", "email": "buero@example.invalid",
        "inhaber": "Max Muster", "ust_id": "DE000000000", "handwerksrolle": "HWK Köln, Nr. 000000", "iban": "DE02120300000000202051",
        "bic": "BYLADEM1001", "bank": "Musterbank", "zahlungsziel_tage": 14, "kontenrahmen": "SKR03"})
    s.add(m)
    s.flush()
    monteure = []
    for name, quali, von, bis, benutzer in MONTEURE:
        r = Ressource(mandant_id=m.id, name=name, qualifikationen=quali, arbeitsbeginn=von, arbeitsende=bis, benutzer=benutzer)
        s.add(r)
        monteure.append(r)
    kunden, anlagen_je_kunde = [], []
    for name, tel, strasse, plz, ort, vertrag, anlagen in KUNDEN:
        k = Kunde(mandant_id=m.id, name=name, telefon="+49" + tel.replace(" ", "")[1:], strasse=strasse, plz=plz,
                  ort=ort, vertragsstatus=vertrag)
        s.add(k)
        s.flush()
        o = Objekt(mandant_id=m.id, kunde_id=k.id, strasse=strasse, plz=plz, ort=ort)
        s.add(o)
        s.flush()
        liste = []
        for typ, hersteller, baujahr, monate in anlagen:
            a = Anlage(mandant_id=m.id, objekt_id=o.id, anlagentyp=typ, hersteller=hersteller, baujahr=baujahr,
                       wartungsintervall_monate=None if typ == "sanitaer" else (24 if typ in ("solarthermie", "warmwasser") else 12),
                       letzte_wartung=_monate_zurueck(heute, monate) if monate is not None else None)
            s.add(a)
            liste.append(a)
        kunden.append(k)
        anlagen_je_kunde.append(liste)
        domaene.protokollieren(s, m.id, "kunde.angelegt", "kunde", k.id, "mensch", {"quelle": "import"})
    s.flush()

    faelle: list[tuple[Servicefall, int]] = []
    for i, (ki, anliegen, symptom, status, versatz, uhrzeit, mi, bestaetigt) in enumerate(VORGAENGE):
        k = kunden[ki]
        anlage = anlagen_je_kunde[ki][0] if anlagen_je_kunde[ki] else None
        bewertung = dringlichkeit_bewerten(f"{anliegen} {symptom or ''}", heute.month)
        f = Servicefall(mandant_id=m.id, kunde_id=k.id, anlage_id=anlage.id if anlage else None, anliegen=anliegen,
                        symptom=symptom, dringlichkeit=bewertung.dringlichkeit, quelle="telefon" if i % 3 else "email",
                        benoetigte_qualifikation=qualifikation_fuer(anlage.anlagentyp if anlage else None),
                        erstellt=jetzt_ - timedelta(hours=2 + i * 5))
        s.add(f)
        s.flush()
        faelle.append((f, mi))
        eingang = domaene.protokollieren(s, m.id, "servicefall.eingegangen", "servicefall", f.id,
                                         "ki" if f.quelle == "telefon" else "system",
                                         {"quelle": f.quelle, "dringlichkeit": f.dringlichkeit, "regel": bewertung.grund})
        eingang.zeit = f.erstellt
        s.flush()
        if f.quelle == "telefon":
            s.add(Kommunikation(mandant_id=m.id, kunde_id=k.id, servicefall_id=f.id, kanal="telefon",
                                transkript="(Demo) Transkript liegt vor", zusammenfassung=anliegen,
                                zeit=jetzt_ - timedelta(hours=2 + i * 5)))
        if versatz is not None:
            tag = _naechster_werktag(heute, versatz)
            beginn = datetime.combine(tag, uhrzeit, ZEITZONE)
            t = Termin(mandant_id=m.id, servicefall_id=f.id, ressource_id=monteure[mi].id, beginn=beginn,
                       ende=beginn + timedelta(minutes=90), status="bestaetigt" if bestaetigt else "vorgeschlagen")
            s.add(t)
            s.flush()
            domaene.uebergang(s, f, "qualifiziert", "ki", "Termin vorgeschlagen")
            if bestaetigt:
                domaene.uebergang(s, f, "eingeplant", "mensch", "Termin bestätigt", "buero")
            else:
                domaene.aktion_beantragen(s, m, "termin_bestaetigen", {"termin_id": t.id}, "ki", f.id,
                                          f"{monteures_name(monteure[mi])}, {WOCHENTAGE[beginn.weekday()]} {beginn:%d.%m. %H:%M}")
        for ziel in ("durchgefuehrt", "abgerechnet"):
            if status in ("durchgefuehrt", "abgerechnet") and f.status != status:
                domaene.uebergang(s, f, ziel, "mensch", None, "buero")
        if status == "abgerechnet":
            domaene.aktion_beantragen(s, m, "rechnung_erstellen", {"servicefall_id": f.id, "betrag_netto": 189.0},
                                      "system", f.id, "Rechnungsentwurf aus Rapport")
        # Folgeereignisse auf die Demo-Uhr legen (sonst stünden sie auf der Systemzeit)
        for n, e in enumerate(s.scalars(select(Ereignis).where(Ereignis.mandant_id == m.id, Ereignis.id > eingang.id)
                                        .order_by(Ereignis.id))):
            e.zeit = f.erstellt + timedelta(minutes=25 + 15 * n)
    _betriebsdaten(s, m, monteure, faelle, jetzt_, heute)
    s.commit()
    return m.id


def _betriebsdaten(s: Session, m: Mandant, monteure: list[Ressource], faelle: list[tuple[Servicefall, int]],
                   jetzt_: datetime, heute: date) -> None:
    """Zeiten, Rapport, Eingangsbelege, Bereitschaft und Fristen für die Betriebsmodule."""
    erledigt = [(f, mi) for f, mi in faelle if f.status in ("durchgefuehrt", "abgerechnet")]
    for n, (f, mi) in enumerate(erledigt[:2]):
        r = monteure[mi]
        tag = datetime.combine(heute - timedelta(days=2 + n), time(8, 0), ZEITZONE)
        einsatz.zeit_starten(s, m, r, "fahrt", tag, f.id, akteur_name="seed")
        einsatz.zeit_starten(s, m, r, "arbeit", tag + timedelta(minutes=35), f.id, akteur_name="seed")
        laufend = einsatz.laufender_eintrag(s, r.id)
        einsatz.zeit_beenden(s, laufend, tag + timedelta(minutes=35 + 95 + 20 * n))
        einsatz.rapport_erzeugen(s, m, f, r, {
            "taetigkeiten": "Störung eingegrenzt, Bauteil getauscht, Anlage in Betrieb genommen und Funktion geprüft.",
            "material": [{"menge": "1", "bezeichnung": "Ersatzteil laut Beleg"}], "arbeitszeit_minuten": 95 + 20 * n,
            "fahrzeit_minuten": 35, "ergebnis": "erledigt", "hinweise_kunde": "Wartung in 12 Monaten empfohlen.",
            "messwerte": {"abgasverlust_prozent": "6.5", "co_ppm": "12", "druck_bar": "1.6"}, "termin_id": None},
            tag + timedelta(minutes=140), akteur_name="seed")
    # Eingangsbelege: zwei Großhandelsrechnungen (eine dem ersten Vorgang zugeordnet), eine Tankquittung
    belege = [
        ("Sanitärgroßhandel Rheinland (fiktiv)", "RE-2026-18811", 3, "186.50", "material", erledigt[0][0].id if erledigt else None, "geprueft"),
        ("Heizungsgroßhandel West (fiktiv)", "LS-77120", 1, "412.90", "material", erledigt[1][0].id if len(erledigt) > 1 else None, "erfasst"),
        ("Tankstelle Ehrenfeld (fiktiv)", None, 0, "84.20", "fahrzeug", None, "erfasst"),
    ]
    for lieferant, nummer, tage, brutto, kategorie, fall_id, status in belege:
        b = float(brutto)
        netto = round(b / 1.19, 2)
        s.add(Beleg(mandant_id=m.id, lieferant=lieferant, nummer=nummer, datum=heute - timedelta(days=tage),
                    faellig=heute - timedelta(days=tage) + timedelta(days=30), netto=f"{netto:.2f}",
                    umsatzsteuer=f"{b - netto:.2f}", brutto=f"{b:.2f}", ust_satz="19", positionen=[], kategorie=kategorie,
                    servicefall_id=fall_id, quelle="manuell", erkennung={"konfidenz": 1.0, "hinweise": []}, status=status,
                    geprueft_von="buero" if status == "geprueft" else None, erstellt=jetzt_ - timedelta(days=tage)))
    # Bereitschaft: aktuelle Woche (ab gestern bis in acht Tagen), damit der Sprachagent sie findet
    einsatz.bereitschaft_anlegen(s, m, monteure[0], jetzt_ - timedelta(days=1), jetzt_ + timedelta(days=8),
                                 "+49221000001", "Demo-Bereitschaft")
    fristen = [
        ("betrieb", None, "pruefung", "UVV-Prüfung Transporter K-DH 123", 12, 12, "Prüfplakette im Fahrzeug"),
        ("betrieb", None, "pruefung", "Prüfung Leitern und Tritte (DGUV 208-016)", 45, 12, None),
        ("betrieb", None, "unterweisung", "Erste-Hilfe-Auffrischung (DGUV Vorschrift 1)", -9, 24, "Kurs beim DRK buchen"),
        ("ressource", monteure[1].id, "zertifikat", "Sachkundenachweis Kältemittel (Kat. I)", 150, None, None),
        ("betrieb", None, "vertrag", "Kfz-Versicherung Flotte: Kündigungsfrist", 300, 12, None),
    ]
    for bezug_typ, bezug_id, art, bez, tage, intervall, hinweis in fristen:
        s.add(Frist(mandant_id=m.id, bezug_typ=bezug_typ, bezug_id=bezug_id, art=art, bezeichnung=bez,
                    faellig=heute + timedelta(days=tage), intervall_monate=intervall, hinweis=hinweis))
    s.flush()


def monteures_name(r: Ressource) -> str:
    return r.name.split(" (")[0]
