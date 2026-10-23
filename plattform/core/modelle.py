"""Kanonisches Datenmodell (Abschnitt „Kanonisches Datenmodell“ der Backend-Architektur).

Das Modell bildet den Betrieb als Digital Twin ab und abstrahiert von der Fachsoftware: CRM
(Kunde, Objekt, Anlage), Disposition (Ressource, Termin), Vorgangssteuerung (Servicefall mit
Statusautomat), Kommunikation, Freigabe, Event-Log, Outbox und ExternalRef. Betriebe ohne
Fachsoftware nutzen es direkt als CRM; Betriebe mit Fachsoftware über Adapter (ExternalRef).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy import JSON, Date, Float, ForeignKey, Index, Integer, LargeBinary, String, Text, Time, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column, object_session, relationship

from core.db import Basis, UtcZeit


def neue_id() -> str:
    return uuid.uuid4().hex


def jetzt() -> datetime:
    return datetime.now(timezone.utc)


class Mandant(Basis):
    __tablename__ = "mandant"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    name: Mapped[str] = mapped_column(String(200))
    gewerk: Mapped[str] = mapped_column(String(50), default="SHK")
    # Freigabe-Policies je Aktion, z. B. {"termin_bestaetigen": "auto"}; Klasse C ist nie automatisierbar
    policies: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Fahrzeitpuffer vor und nach Terminen, Schrittweite der Zeitfenster
    puffer_minuten: Mapped[int] = mapped_column(Integer, default=30)
    raster_minuten: Mapped[int] = mapped_column(Integer, default=30)
    # Betriebsstammdaten für Rechnungen und Schreiben: strasse, plz, ort, telefon, email, ust_id,
    # steuernummer, iban, bic, bank, inhaber, zahlungsziel_tage, kleinunternehmer (§ 19 UStG)
    stammdaten: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Anbindungen je Ziel (fachsoftware, endkunde, finanz, kanzlei): {"typ": "lexware", "api_key": "enc:…"}
    # Geheimnisse liegen verschlüsselt (core.geheimnisse), nie im Klartext in der Datenbank
    integrationen: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rechnungsnummer_zaehler: Mapped[int] = mapped_column(Integer, default=0)
    angebotsnummer_zaehler: Mapped[int] = mapped_column(Integer, default=0)
    erstellt: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)


class _Mandantenbezug:
    mandant_id: Mapped[str] = mapped_column(String(32), ForeignKey("mandant.id"), index=True)


class Kunde(_Mandantenbezug, Basis):
    __tablename__ = "kunde"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    name: Mapped[str] = mapped_column(String(200))
    telefon: Mapped[str | None] = mapped_column(String(32), index=True)   # E.164, Dublettenschlüssel
    email: Mapped[str | None] = mapped_column(String(200))
    strasse: Mapped[str | None] = mapped_column(String(200))
    plz: Mapped[str | None] = mapped_column(String(10))
    ort: Mapped[str | None] = mapped_column(String(100))
    # Einwilligungen mit Zeitstempel, z. B. {"aufzeichnung": "2026-10-20T10:00:00Z", "sms": ...}
    einwilligungen: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    vertragsstatus: Mapped[str] = mapped_column(String(30), default="ohne")  # ohne, wartungsvertrag
    notiz: Mapped[str | None] = mapped_column(Text)
    kartei: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)      # Branchenfelder (Rezeptur, Allergien …), siehe branchen.py
    nicht_erschienen: Mapped[int] = mapped_column(Integer, default=0)       # No-Show-Zähler
    # Geokoordinaten (WGS 84) für Kundenkarte und Anfahrt; geo_quelle: nominatim, manuell, demo
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    geo_quelle: Mapped[str | None] = mapped_column(String(20))
    geo_stand: Mapped[datetime | None] = mapped_column(UtcZeit)
    erstellt: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)

    objekte: Mapped[list[Objekt]] = relationship(back_populates="kunde")


class Objekt(_Mandantenbezug, Basis):
    """Gebäude oder Liegenschaft, an der Anlagen installiert sind."""
    __tablename__ = "objekt"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    kunde_id: Mapped[str] = mapped_column(String(32), ForeignKey("kunde.id"), index=True)
    bezeichnung: Mapped[str] = mapped_column(String(200), default="Hauptobjekt")
    strasse: Mapped[str | None] = mapped_column(String(200))
    plz: Mapped[str | None] = mapped_column(String(10))
    ort: Mapped[str | None] = mapped_column(String(100))
    gebaeudetyp: Mapped[str | None] = mapped_column(String(50))  # Einfamilienhaus, Mehrfamilienhaus, Gewerbe
    # Geokoordinaten (WGS 84) für Kundenkarte und Anfahrt; geo_quelle: nominatim, manuell, demo
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    geo_quelle: Mapped[str | None] = mapped_column(String(20))
    geo_stand: Mapped[datetime | None] = mapped_column(UtcZeit)

    kunde: Mapped[Kunde] = relationship(back_populates="objekte")
    anlagen: Mapped[list[Anlage]] = relationship(back_populates="objekt")


class Anlage(_Mandantenbezug, Basis):
    __tablename__ = "anlage"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    objekt_id: Mapped[str] = mapped_column(String(32), ForeignKey("objekt.id"), index=True)
    anlagentyp: Mapped[str] = mapped_column(String(50))  # Schlüssel aus ontologie.ANLAGENTYPEN
    hersteller: Mapped[str | None] = mapped_column(String(100))
    modell: Mapped[str | None] = mapped_column(String(100))
    baujahr: Mapped[int | None] = mapped_column(Integer)
    wartungsintervall_monate: Mapped[int | None] = mapped_column(Integer, default=12)
    letzte_wartung: Mapped[date | None] = mapped_column(Date)

    objekt: Mapped[Objekt] = relationship(back_populates="anlagen")


class Ressource(_Mandantenbezug, Basis):
    """Monteur oder Team; Qualifikationen sind Constraints der Terminvergabe."""
    __tablename__ = "ressource"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    name: Mapped[str] = mapped_column(String(200))
    qualifikationen: Mapped[list[str]] = mapped_column(JSON, default=list)  # z. B. ["gas", "kaelteschein"]
    arbeitstage: Mapped[list[int]] = mapped_column(JSON, default=lambda: [0, 1, 2, 3, 4])  # 0 = Montag
    arbeitsbeginn: Mapped[time] = mapped_column(Time, default=time(7, 30))
    arbeitsende: Mapped[time] = mapped_column(Time, default=time(16, 30))
    aktiv: Mapped[bool] = mapped_column(default=True)
    # Anmeldename (preferred_username) des Monteurs in Keycloak; damit sieht er nur seine eigenen Einsätze
    benutzer: Mapped[str | None] = mapped_column(String(100), index=True)
    qualifikationen_ablauf: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)   # {"gas": "2027-05-01"} Ablauf/Nachschulung, siehe qualifikationen.py
    wochenstunden: Mapped[float] = mapped_column(Float, default=40.0)                    # Soll für das Zeitkonto
    urlaubsanspruch_tage: Mapped[int] = mapped_column(Integer, default=30)


class Servicefall(_Mandantenbezug, Basis):
    __tablename__ = "servicefall"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    kunde_id: Mapped[str] = mapped_column(String(32), ForeignKey("kunde.id"), index=True)
    anlage_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("anlage.id"))
    anliegen: Mapped[str] = mapped_column(Text)
    symptom: Mapped[str | None] = mapped_column(Text)
    dringlichkeit: Mapped[str] = mapped_column(String(20), default="normal")  # notdienst, hoch, normal, niedrig
    status: Mapped[str] = mapped_column(String(30), default="eingegangen")
    quelle: Mapped[str] = mapped_column(String(20), default="manuell")  # telefon, email, web, manuell
    benoetigte_qualifikation: Mapped[str | None] = mapped_column(String(50))
    dauer_minuten: Mapped[int] = mapped_column(Integer, default=90)
    erstellt: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)
    geaendert: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt, onupdate=jetzt)

    kunde: Mapped[Kunde] = relationship()
    anlage: Mapped[Anlage | None] = relationship()
    termine: Mapped[list[Termin]] = relationship(back_populates="servicefall")


class Termin(_Mandantenbezug, Basis):
    __tablename__ = "termin"
    __table_args__ = (Index("ix_termin_ressource_zeit", "ressource_id", "beginn"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    servicefall_id: Mapped[str] = mapped_column(String(32), ForeignKey("servicefall.id"), index=True)
    ressource_id: Mapped[str] = mapped_column(String(32), ForeignKey("ressource.id"))
    beginn: Mapped[datetime] = mapped_column(UtcZeit)
    ende: Mapped[datetime] = mapped_column(UtcZeit)
    status: Mapped[str] = mapped_column(String(20), default="vorgeschlagen")  # vorgeschlagen, bestaetigt, abgesagt

    servicefall: Mapped[Servicefall] = relationship(back_populates="termine")
    ressource: Mapped[Ressource] = relationship()


class Kommunikation(_Mandantenbezug, Basis):
    __tablename__ = "kommunikation"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    kunde_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("kunde.id"), index=True)
    servicefall_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("servicefall.id"))
    kanal: Mapped[str] = mapped_column(String(20))  # telefon, email, sms, whatsapp
    richtung: Mapped[str] = mapped_column(String(10), default="eingehend")
    transkript: Mapped[str | None] = mapped_column(Text)
    zusammenfassung: Mapped[str | None] = mapped_column(Text)
    extraktion: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    zeit: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)


class Freigabe(_Mandantenbezug, Basis):
    """Eintrag der Freigabe-Inbox (Human-in-the-Loop für Klasse B und C)."""
    __tablename__ = "freigabe"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    aktion: Mapped[str] = mapped_column(String(50))
    risikoklasse: Mapped[str] = mapped_column(String(1))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    servicefall_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("servicefall.id"))
    status: Mapped[str] = mapped_column(String(20), default="offen")  # offen, freigegeben, abgelehnt
    begruendung: Mapped[str | None] = mapped_column(Text)
    erstellt: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)
    entschieden_von: Mapped[str | None] = mapped_column(String(100))
    entschieden_am: Mapped[datetime | None] = mapped_column(UtcZeit)


class Ereignis(_Mandantenbezug, Basis):
    """Append-only Event-Log: Grundlage für Audit, Replay und Nachvollziehbarkeit jeder KI-Entscheidung."""
    __tablename__ = "ereignis"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    typ: Mapped[str] = mapped_column(String(80), index=True)  # z. B. servicefall.eingegangen
    entitaet: Mapped[str] = mapped_column(String(40))
    entitaet_id: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    akteur: Mapped[str] = mapped_column(String(10))  # mensch, ki, system
    akteur_name: Mapped[str | None] = mapped_column(String(100))
    zeit: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)


class OutboxEintrag(_Mandantenbezug, Basis):
    """Transactional Outbox: wird in derselben Transaktion wie die Fachänderung geschrieben."""
    __tablename__ = "outbox"
    __table_args__ = (UniqueConstraint("mandant_id", "idempotenz_schluessel"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ziel: Mapped[str] = mapped_column(String(50))     # Adaptername, z. B. fachsoftware, sms, csv
    aktion: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    idempotenz_schluessel: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="ausstehend")  # ausstehend, zugestellt, tot
    versuche: Mapped[int] = mapped_column(Integer, default=0)
    naechster_versuch: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)
    letzter_fehler: Mapped[str | None] = mapped_column(Text)


class Rechnung(_Mandantenbezug, Basis):
    """Ausgangsrechnung oder Stornorechnung (GoBD: fortlaufende Nummer, unveränderlich nach Stellung)."""
    __tablename__ = "rechnung"
    __table_args__ = (UniqueConstraint("mandant_id", "nummer"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    nummer: Mapped[str] = mapped_column(String(40))
    kunde_id: Mapped[str] = mapped_column(String(32), ForeignKey("kunde.id"), index=True)
    servicefall_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("servicefall.id"))
    art: Mapped[str] = mapped_column(String(20), default="rechnung")  # rechnung, storno
    storno_von_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("rechnung.id"))
    datum: Mapped[date] = mapped_column(Date)
    leistungsdatum: Mapped[date | None] = mapped_column(Date)
    faellig: Mapped[date] = mapped_column(Date)
    # [{"bezeichnung", "menge", "einheit", "einzelpreis_netto", "ust_satz"}], Beträge als Zeichenkette (Decimal)
    positionen: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    netto: Mapped[str] = mapped_column(String(20))
    umsatzsteuer: Mapped[str] = mapped_column(String(20))
    brutto: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="gestellt")  # gestellt, storniert, bezahlt
    empfaenger: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # Anschrift zum Rechnungszeitpunkt
    leitweg_id: Mapped[str | None] = mapped_column(String(60))  # nur B2G (XRechnung Pflichtfeld)
    erstellt: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)

    kunde: Mapped[Kunde] = relationship()


class ExternalRef(_Mandantenbezug, Basis):
    __tablename__ = "external_ref"
    __table_args__ = (UniqueConstraint("mandant_id", "entitaet", "entitaet_id", "zielsystem"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entitaet: Mapped[str] = mapped_column(String(40))
    entitaet_id: Mapped[str] = mapped_column(String(32))
    zielsystem: Mapped[str] = mapped_column(String(50))
    externe_id: Mapped[str] = mapped_column(String(120))
    letzter_sync: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)


# --- Betriebsmodule: Dokumente, Belegeingang, Zeiten, Rapport, Bereitschaft, Fristen -----------
class Anhang(_Mandantenbezug, Basis):
    """Datei am Fachobjekt (Foto vom Einsatz, Unterschrift, Belegbild, Vertrag). Der Inhalt liegt in der
    Datenbank: damit greifen RLS, Sicherung und Löschkonzept ohne zweiten Speicherpfad; Bilder werden
    beim Hochladen verkleinert (core.einsatz.bild_verkleinern), Obergrenze 10 MB je Datei."""
    __tablename__ = "anhang"
    __table_args__ = (Index("ix_anhang_bezug", "bezug_typ", "bezug_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    bezug_typ: Mapped[str] = mapped_column(String(20))   # servicefall, kunde, objekt, anlage, beleg, rapport, betrieb
    bezug_id: Mapped[str] = mapped_column(String(32))
    art: Mapped[str] = mapped_column(String(20), default="dokument")  # foto, unterschrift, beleg, dokument
    name: Mapped[str] = mapped_column(String(200))
    mime: Mapped[str] = mapped_column(String(100))
    groesse: Mapped[int] = mapped_column(Integer)
    inhalt: Mapped[bytes] = mapped_column(LargeBinary)
    notiz: Mapped[str | None] = mapped_column(String(300))
    hochgeladen_von: Mapped[str | None] = mapped_column(String(100))
    hochgeladen: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)


class Beleg(_Mandantenbezug, Basis):
    """Eingangsbeleg (Lieferantenrechnung, Quittung). Beträge als Zeichenkette (Decimal); Konten vergibt die
    Kanzlei, die Plattform ordnet nur dem Auftrag zu (Nachkalkulation) und reicht weiter (Belegpaket)."""
    __tablename__ = "beleg"
    __table_args__ = (UniqueConstraint("mandant_id", "import_kennung"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    lieferant: Mapped[str] = mapped_column(String(200))
    nummer: Mapped[str | None] = mapped_column(String(80))
    datum: Mapped[date] = mapped_column(Date)
    faellig: Mapped[date | None] = mapped_column(Date)
    netto: Mapped[str] = mapped_column(String(20))
    umsatzsteuer: Mapped[str] = mapped_column(String(20))
    brutto: Mapped[str] = mapped_column(String(20))
    ust_satz: Mapped[str] = mapped_column(String(6), default="19")
    skonto_prozent: Mapped[str | None] = mapped_column(String(6))
    skonto_bis: Mapped[date | None] = mapped_column(Date)
    positionen: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    kategorie: Mapped[str] = mapped_column(String(30), default="material")  # material, fremdleistung, fahrzeug, sonstiges
    servicefall_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("servicefall.id"), index=True)
    anhang_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("anhang.id"))
    quelle: Mapped[str] = mapped_column(String(20), default="manuell")  # xrechnung, zugferd, ocr, manuell, email
    erkennung: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # Rohdaten der Extraktion, Konfidenz, Hinweise
    status: Mapped[str] = mapped_column(String(20), default="erfasst")  # erfasst, geprueft, uebergeben, bezahlt
    notiz: Mapped[str | None] = mapped_column(Text)
    geprueft_von: Mapped[str | None] = mapped_column(String(100))
    import_kennung: Mapped[str | None] = mapped_column(String(200))  # Scan-Eingang: Message-ID bzw. Dateihash, verhindert Dubletten
    erstellt: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)

    @property
    def uebergaben(self) -> list[dict[str, Any]]:
        """Übergaben an Steuertools (Lexware Office, sevDesk, DATEV-Belegtransfer, Upload-Mail) aus ExternalRef."""
        s = object_session(self)
        if s is None:
            return []
        refs = s.scalars(select(ExternalRef).where(ExternalRef.mandant_id == self.mandant_id, ExternalRef.entitaet == "beleg",
                                                   ExternalRef.entitaet_id == self.id).order_by(ExternalRef.id)).all()
        return [{"zielsystem": r.zielsystem, "externe_id": r.externe_id, "zeitpunkt": r.letzter_sync} for r in refs]


class Zeiteintrag(_Mandantenbezug, Basis):
    """Zeiterfassung je Monteur (BAG-Urteil 2022, § 3 ArbSchG): Arbeit, Fahrt, Pause, Bereitschaft.
    ende leer = läuft noch. Grundlage für Nachkalkulation und Lohnvorbereitung."""
    __tablename__ = "zeiteintrag"
    __table_args__ = (Index("ix_zeit_ressource_beginn", "ressource_id", "beginn"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    ressource_id: Mapped[str] = mapped_column(String(32), ForeignKey("ressource.id"))
    servicefall_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("servicefall.id"), index=True)
    art: Mapped[str] = mapped_column(String(15), default="arbeit")  # arbeit, fahrt, pause, bereitschaft
    beginn: Mapped[datetime] = mapped_column(UtcZeit)
    ende: Mapped[datetime | None] = mapped_column(UtcZeit)
    notiz: Mapped[str | None] = mapped_column(String(300))
    quelle: Mapped[str] = mapped_column(String(10), default="app")  # app, manuell, rapport
    erstellt: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)

    ressource: Mapped[Ressource] = relationship()


class Rapport(_Mandantenbezug, Basis):
    """Arbeitsbericht zum Einsatz mit Material, Zeiten, Fotos und Kundenunterschrift (Beweiskraft bei
    Streit über Stunden und Material, § 632 BGB). Nach Unterschrift unveränderlich."""
    __tablename__ = "rapport"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    servicefall_id: Mapped[str] = mapped_column(String(32), ForeignKey("servicefall.id"), index=True)
    termin_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("termin.id"))
    ressource_id: Mapped[str] = mapped_column(String(32), ForeignKey("ressource.id"))
    nummer: Mapped[str] = mapped_column(String(40))
    taetigkeiten: Mapped[str] = mapped_column(Text)
    material: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # [{"bezeichnung","menge","einheit"}]
    arbeitszeit_minuten: Mapped[int] = mapped_column(Integer, default=0)
    fahrzeit_minuten: Mapped[int] = mapped_column(Integer, default=0)
    ergebnis: Mapped[str] = mapped_column(String(20), default="erledigt")  # erledigt, nacharbeit, ersatzteil, abgebrochen
    hinweise_kunde: Mapped[str | None] = mapped_column(Text)
    messwerte: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # z. B. Abgaswerte, Druck; frei je Gewerk
    unterschrift_anhang_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("anhang.id"))
    unterzeichnet_von: Mapped[str | None] = mapped_column(String(200))
    unterzeichnet_am: Mapped[datetime | None] = mapped_column(UtcZeit)
    erstellt: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)

    ressource: Mapped[Ressource] = relationship()
    servicefall: Mapped[Servicefall] = relationship()


class Bereitschaft(_Mandantenbezug, Basis):
    """Not- und Bereitschaftsdienst: wer ist wann erreichbar; der Sprachassistent nennt bzw. verbindet."""
    __tablename__ = "bereitschaft"
    __table_args__ = (Index("ix_bereitschaft_zeit", "von", "bis"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    ressource_id: Mapped[str] = mapped_column(String(32), ForeignKey("ressource.id"))
    von: Mapped[datetime] = mapped_column(UtcZeit)
    bis: Mapped[datetime] = mapped_column(UtcZeit)
    telefon: Mapped[str] = mapped_column(String(32))
    notiz: Mapped[str | None] = mapped_column(String(200))

    ressource: Mapped[Ressource] = relationship()


class Frist(_Mandantenbezug, Basis):
    """Wiederkehrende Pflichten und Termine außerhalb der Anlagenwartung: DGUV-Prüfungen von Leitern,
    Werkzeugen und Fahrzeugen, Unterweisungen, Zertifikate (Kälteschein), Herstellergarantien, Verträge."""
    __tablename__ = "frist"
    __table_args__ = (Index("ix_frist_faellig", "faellig"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    bezug_typ: Mapped[str] = mapped_column(String(20), default="betrieb")  # betrieb, ressource, anlage, fahrzeug
    bezug_id: Mapped[str | None] = mapped_column(String(32))
    art: Mapped[str] = mapped_column(String(20))  # pruefung, unterweisung, zertifikat, garantie, vertrag, sonstiges
    bezeichnung: Mapped[str] = mapped_column(String(200))
    faellig: Mapped[date] = mapped_column(Date)
    intervall_monate: Mapped[int | None] = mapped_column(Integer)
    erledigt_am: Mapped[date | None] = mapped_column(Date)
    hinweis: Mapped[str | None] = mapped_column(String(300))
    erstellt: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)


# --- Zweite Runde Betriebsmodule: Angebote, Forderungen, Verträge, Abwesenheiten, Anfragen ----------
class Artikel(_Mandantenbezug, Basis):
    """Artikel- und Leistungsstamm für Angebots- und Rechnungspositionen (Material, Arbeit, Pauschalen)."""
    __tablename__ = "artikel"
    __table_args__ = (UniqueConstraint("mandant_id", "nummer"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    nummer: Mapped[str] = mapped_column(String(40))
    bezeichnung: Mapped[str] = mapped_column(String(200))
    kategorie: Mapped[str] = mapped_column(String(20), default="material")  # material, arbeit, fahrt, pauschale
    einheit: Mapped[str] = mapped_column(String(10), default="Stk")
    einkauf_netto: Mapped[str | None] = mapped_column(String(20))
    verkauf_netto: Mapped[str] = mapped_column(String(20))
    ust_satz: Mapped[str] = mapped_column(String(6), default="19")
    lieferant: Mapped[str | None] = mapped_column(String(200))
    aktiv: Mapped[bool] = mapped_column(default=True)
    dauer_minuten: Mapped[int | None] = mapped_column(Integer)          # Leistungen (Friseur, Massage …): Terminlänge
    qualifikation: Mapped[str | None] = mapped_column(String(50))       # Leistungen: nötige Qualifikation der Ressource


class Bestand(_Mandantenbezug, Basis):
    """Lagerbestand je Artikel und Lagerort (zentral oder Fahrzeug = Ressourcen-ID); Rapportmaterial bucht ab."""
    __tablename__ = "bestand"
    __table_args__ = (UniqueConstraint("mandant_id", "artikel_id", "lager"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    artikel_id: Mapped[str] = mapped_column(String(32), ForeignKey("artikel.id"), index=True)
    lager: Mapped[str] = mapped_column(String(32), default="zentral")  # "zentral" oder Ressourcen-ID (Fahrzeug)
    menge: Mapped[float] = mapped_column(Float, default=0)
    mindestbestand: Mapped[float] = mapped_column(Float, default=0)
    geaendert: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt, onupdate=jetzt)

    artikel: Mapped[Artikel] = relationship()


class Gutschein(_Mandantenbezug, Basis):
    """Wertgutschein (Friseur, Kosmetik, Massage …): Ausgabe, Teileinlösungen, Restwert; Verjährung 3 Jahre (§ 195 BGB)."""
    __tablename__ = "gutschein"
    __table_args__ = (UniqueConstraint("mandant_id", "code"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    code: Mapped[str] = mapped_column(String(20))
    wert: Mapped[str] = mapped_column(String(20))
    restwert: Mapped[str] = mapped_column(String(20))
    kunde_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("kunde.id"))
    empfaenger: Mapped[str | None] = mapped_column(String(200))
    gueltig_bis: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="aktiv")  # aktiv, eingeloest, abgelaufen, storniert
    einloesungen: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # [{"am","betrag","rechnung_id","wer"}]
    ausgestellt: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)


class Wartelisteneintrag(_Mandantenbezug, Basis):
    """Warteliste für frühere Termine: bei Absagen wird die Lücke automatisch dem bestpassenden Eintrag angeboten
    (Reservierung mit Frist); siehe warteliste.py."""
    __tablename__ = "wartelisteneintrag"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    servicefall_id: Mapped[str] = mapped_column(String(32), ForeignKey("servicefall.id"), index=True)
    kunde_id: Mapped[str] = mapped_column(String(32), ForeignKey("kunde.id"), index=True)
    wunsch_von: Mapped[date] = mapped_column(Date)
    wunsch_bis: Mapped[date] = mapped_column(Date)
    tageszeiten: Mapped[list[str]] = mapped_column(JSON, default=list)      # ["vormittag", "nachmittag"]; leer = egal
    wochentage: Mapped[list[int]] = mapped_column(JSON, default=list)       # 0 = Montag; leer = egal
    dauer_minuten: Mapped[int] = mapped_column(Integer, default=90)
    qualifikation: Mapped[str | None] = mapped_column(String(50))
    prioritaet: Mapped[int] = mapped_column(Integer, default=30)            # aus Dringlichkeit: 100 notdienst, 60 hoch, 30 normal, 10 niedrig
    status: Mapped[str] = mapped_column(String(20), default="offen")        # offen, angeboten, gebucht, abgelaufen, zurueckgezogen
    angebot: Mapped[dict[str, Any] | None] = mapped_column(JSON)            # {"termin_id","beginn","ende","ressource_id","frist"}
    angeboten_am: Mapped[datetime | None] = mapped_column(UtcZeit)
    angebote_anzahl: Mapped[int] = mapped_column(Integer, default=0)
    notiz: Mapped[str | None] = mapped_column(String(300))
    erstellt: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)

    servicefall: Mapped[Servicefall] = relationship()
    kunde: Mapped[Kunde] = relationship()


class Bauteil(_Mandantenbezug, Basis):
    """Bauteil oder Prüfpflicht einer Anlage mit Nutzungsdauer bzw. Intervall – Grundlage des Erneuerungskalenders (lebenszyklus.py)."""
    __tablename__ = "bauteil"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    anlage_id: Mapped[str] = mapped_column(String(32), ForeignKey("anlage.id"), index=True)
    art: Mapped[str] = mapped_column(String(50))                 # Schlüssel aus dem Katalog oder frei
    bezeichnung: Mapped[str] = mapped_column(String(200))
    pflicht: Mapped[bool] = mapped_column(default=False)         # True: wiederkehrende Prüfung, False: Verschleißteil mit Nutzungsdauer
    nutzungsdauer_jahre: Mapped[int | None] = mapped_column(Integer)
    intervall_monate: Mapped[int | None] = mapped_column(Integer)
    einbau: Mapped[date | None] = mapped_column(Date)
    letzte_erneuerung: Mapped[date | None] = mapped_column(Date)   # bzw. letzte Prüfung
    kosten_schaetzung: Mapped[str | None] = mapped_column(String(20))
    rechtsgrundlage: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="aktiv")   # aktiv, entfernt
    notiz: Mapped[str | None] = mapped_column(String(300))
    erstellt: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)

    anlage: Mapped[Anlage] = relationship()


class Dienst(_Mandantenbezug, Basis):
    """Dienstplan: geplante Arbeits-, Bereitschafts- und Freizeiten je Mitarbeiter und Tag (dienstplan.py)."""
    __tablename__ = "dienst"
    __table_args__ = (UniqueConstraint("ressource_id", "datum", "beginn"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    ressource_id: Mapped[str] = mapped_column(String(32), ForeignKey("ressource.id"), index=True)
    datum: Mapped[date] = mapped_column(Date, index=True)
    beginn: Mapped[time] = mapped_column(Time)
    ende: Mapped[time] = mapped_column(Time)
    art: Mapped[str] = mapped_column(String(20), default="arbeit")   # arbeit, bereitschaft, frei
    pause_minuten: Mapped[int] = mapped_column(Integer, default=30)
    schicht: Mapped[str | None] = mapped_column(String(20))          # frueh, spaet, tag, bereitschaft
    notiz: Mapped[str | None] = mapped_column(String(200))
    erstellt: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)

    ressource: Mapped[Ressource] = relationship()


class Zaehlerstand(_Mandantenbezug, Basis):
    """Zähler- und Betriebswerte je Anlage/Kunde (Gas, Strom, Wasser, Betriebsstunden, Brennerstarts) für die Verbrauchsanalyse (zaehler.py)."""
    __tablename__ = "zaehlerstand"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    kunde_id: Mapped[str] = mapped_column(String(32), ForeignKey("kunde.id"), index=True)
    anlage_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("anlage.id"), index=True)
    art: Mapped[str] = mapped_column(String(30))                 # gas_kwh, gas_m3, strom_kwh, wasser_m3, waerme_kwh, betriebsstunden, brennerstarts
    wert: Mapped[float] = mapped_column(Float)
    datum: Mapped[date] = mapped_column(Date)
    quelle: Mapped[str] = mapped_column(String(20), default="buero")   # buero, monteur, kunde, import
    notiz: Mapped[str | None] = mapped_column(String(200))
    erstellt: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)


class Angebot(_Mandantenbezug, Basis):
    """Angebot mit eigenem Nummernkreis; Annahme erzeugt den Auftrag (Servicefall), Rechnung über Freigabe C."""
    __tablename__ = "angebot"
    __table_args__ = (UniqueConstraint("mandant_id", "nummer"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    nummer: Mapped[str] = mapped_column(String(40))
    kunde_id: Mapped[str] = mapped_column(String(32), ForeignKey("kunde.id"), index=True)
    servicefall_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("servicefall.id"))
    rechnung_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("rechnung.id"))
    titel: Mapped[str] = mapped_column(String(200))
    einleitung: Mapped[str | None] = mapped_column(Text)
    datum: Mapped[date] = mapped_column(Date)
    gueltig_bis: Mapped[date] = mapped_column(Date)
    positionen: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    netto: Mapped[str] = mapped_column(String(20))
    umsatzsteuer: Mapped[str] = mapped_column(String(20))
    brutto: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="entwurf")  # entwurf, versendet, angenommen, abgelehnt, abgelaufen
    empfaenger: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    entschieden_am: Mapped[date | None] = mapped_column(Date)
    erstellt: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)

    kunde: Mapped[Kunde] = relationship()


class Zahlung(_Mandantenbezug, Basis):
    """Zahlungseingang zu einer Rechnung (manuell, Bankimport, Skonto, Ausbuchung)."""
    __tablename__ = "zahlung"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    rechnung_id: Mapped[str] = mapped_column(String(32), ForeignKey("rechnung.id"), index=True)
    datum: Mapped[date] = mapped_column(Date)
    betrag: Mapped[str] = mapped_column(String(20))
    art: Mapped[str] = mapped_column(String(20), default="ueberweisung")  # ueberweisung, bar, karte, skonto, ausbuchung
    referenz: Mapped[str | None] = mapped_column(String(200))
    quelle: Mapped[str] = mapped_column(String(20), default="manuell")  # manuell, bankimport
    erstellt: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)

    rechnung: Mapped[Rechnung] = relationship()


class Mahnung(_Mandantenbezug, Basis):
    """Zahlungserinnerung (Stufe 1) und Mahnungen (Stufe 2, 3) zu einer Rechnung; entstehen über Freigaben."""
    __tablename__ = "mahnung"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    rechnung_id: Mapped[str] = mapped_column(String(32), ForeignKey("rechnung.id"), index=True)
    stufe: Mapped[int] = mapped_column(Integer)
    datum: Mapped[date] = mapped_column(Date)
    frist: Mapped[date] = mapped_column(Date)
    gebuehr: Mapped[str] = mapped_column(String(20), default="0.00")
    offen: Mapped[str] = mapped_column(String(20))  # offener Betrag zum Mahnzeitpunkt
    erstellt: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)

    rechnung: Mapped[Rechnung] = relationship()


class Vertrag(_Mandantenbezug, Basis):
    """Wartungsvertrag: Laufzeit, Kündigungsfrist, Verlängerung, jährliche Abrechnung über Freigabe C."""
    __tablename__ = "vertrag"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    kunde_id: Mapped[str] = mapped_column(String(32), ForeignKey("kunde.id"), index=True)
    bezeichnung: Mapped[str] = mapped_column(String(200))
    anlagen_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    leistungen: Mapped[str | None] = mapped_column(Text)
    beginn: Mapped[date] = mapped_column(Date)
    laufzeit_monate: Mapped[int] = mapped_column(Integer, default=12)
    kuendigungsfrist_monate: Mapped[int] = mapped_column(Integer, default=3)
    verlaengerung_monate: Mapped[int] = mapped_column(Integer, default=12)
    preis_jahr_netto: Mapped[str] = mapped_column(String(20))
    ust_satz: Mapped[str] = mapped_column(String(6), default="19")
    abrechnung_monate: Mapped[int] = mapped_column(Integer, default=12)  # Abrechnungsintervall
    naechste_abrechnung: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="aktiv")  # aktiv, gekuendigt, beendet
    gekuendigt_zum: Mapped[date | None] = mapped_column(Date)
    notiz: Mapped[str | None] = mapped_column(Text)
    erstellt: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)

    kunde: Mapped[Kunde] = relationship()


class Abwesenheit(_Mandantenbezug, Basis):
    """Urlaub, Krankheit, Schulung: blockiert die Disposition ganztägig."""
    __tablename__ = "abwesenheit"
    __table_args__ = (Index("ix_abwesenheit_ressource", "ressource_id", "von"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    ressource_id: Mapped[str] = mapped_column(String(32), ForeignKey("ressource.id"))
    von: Mapped[date] = mapped_column(Date)
    bis: Mapped[date] = mapped_column(Date)
    art: Mapped[str] = mapped_column(String(20), default="urlaub")  # urlaub, krank, schulung, sonstiges
    notiz: Mapped[str | None] = mapped_column(String(300))

    ressource: Mapped[Ressource] = relationship()


class Anfrage(_Mandantenbezug, Basis):
    """Eingang über Webformular oder E-Mail-Postfach; wird zum Servicefall, Rohdaten bleiben zur Nachvollziehbarkeit."""
    __tablename__ = "anfrage"
    __table_args__ = (UniqueConstraint("mandant_id", "externe_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=neue_id)
    kanal: Mapped[str] = mapped_column(String(20))  # web, email
    externe_id: Mapped[str] = mapped_column(String(200))  # Message-ID bzw. Formularkennung
    name: Mapped[str | None] = mapped_column(String(200))
    telefon: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(200))
    betreff: Mapped[str | None] = mapped_column(String(300))
    nachricht: Mapped[str | None] = mapped_column(Text)
    kunde_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("kunde.id"))
    servicefall_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("servicefall.id"))
    status: Mapped[str] = mapped_column(String(20), default="neu")  # neu, uebernommen, verworfen
    eingegangen: Mapped[datetime] = mapped_column(UtcZeit, default=jetzt)
