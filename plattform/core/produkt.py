"""Produktisierung: Editionen, Lizenzschlüssel, Testphase, White-Label und Betreiberkonsole.

Editionen (`EDITIONEN`) bündeln Funktionen (`features`) und Grenzen (`limits`). Ein Mandant hat eine wirksame
Edition aus (1) einem Lizenzschlüssel, (2) einer Betreiberzuweisung oder (3) der Testphase (30 Tage ab Anlage, Edition
„enterprise“); danach gilt „starter“. Lizenzschlüssel sind Ed25519-signierte JSON-Nutzdaten (base64url):
der Anbieter erzeugt das Schlüsselpaar (`python -m core lizenz schluesselpaar`), hält den privaten Schlüssel zurück und
hinterlegt den öffentlichen in der Installation (`LIZENZ_OEFFENTLICHER_SCHLUESSEL`). Ein Schlüssel ohne passenden
öffentlichen Schlüssel wird abgelehnt; es gibt keine Hintertür. White-Label über Umgebungsvariablen (`PRODUKT_*`).
Der Versionsstand kommt aus der Datei VERSION im Projektverzeichnis."""
from __future__ import annotations

import base64
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.modelle import Kunde, Mandant, Servicefall, Termin

VERSION_DATEI = Path(__file__).resolve().parent.parent / "VERSION"
__version__ = VERSION_DATEI.read_text().strip() if VERSION_DATEI.exists() else "0.0.0"

TESTPHASE_TAGE = 30
FEATURES: dict[str, str] = {
    "disposition": "Disposition, Plantafel, Termine, Freigaben", "crm": "Kunden, Anlagen, Kartei", "voice": "Telefonagent (LiveKit)",
    "rechnungen": "Rechnungen, XRechnung, Mahnwesen", "portal": "Kundenportal", "online_buchung": "Online-Buchung", "website": "Betriebswebsite",
    "upsell": "Upsell-Empfehlungen und Kampagnen", "whatsapp": "WhatsApp-Kanal", "online_zahlung": "Online-Zahlung (Stripe)",
    "finanzanbindung": "Lexware, sevDesk, DATEV", "lager": "Lager und Großhandel", "dienstplan": "Dienstplan, Zeitkonten, ArbZG",
    "auswertungen": "Auswertungen und Excel-Export", "optimierung": "Tourenoptimierung, Prognose, Risiko", "mehrbetrieb": "mehrere Standorte je Mandant",
    "api": "offene API und Webhooks", "white_label": "eigenes Branding je Betrieb",
}
EDITIONEN: dict[str, dict[str, Any]] = {
    "starter": {"titel": "Starter", "preis_monat": 149, "features": ["disposition", "crm", "rechnungen", "portal", "online_buchung", "auswertungen"],
                "limits": {"mitarbeiter": 3, "kunden": 1000, "sprachminuten_monat": 300}},
    "pro": {"titel": "Pro", "preis_monat": 249, "features": ["disposition", "crm", "voice", "rechnungen", "portal", "online_buchung", "website", "upsell", "whatsapp",
                                                            "online_zahlung", "finanzanbindung", "lager", "dienstplan", "auswertungen", "api"],
            "limits": {"mitarbeiter": 10, "kunden": 10000, "sprachminuten_monat": 1500}},
    "enterprise": {"titel": "Enterprise", "preis_monat": 582, "features": list(FEATURES), "limits": {"mitarbeiter": 50, "kunden": 100000, "sprachminuten_monat": 6000}},
}


class Lizenzfehler(ValueError):
    pass


# --- Schlüssel ----------------------------------------------------------------------------------
def _b64(d: bytes) -> str:
    return base64.urlsafe_b64encode(d).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def schluesselpaar() -> dict[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    priv = ed25519.Ed25519PrivateKey.generate()
    return {"privat": _b64(priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())),
            "oeffentlich": _b64(priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))}


def schluessel_erzeugen(privat: str, *, edition: str, kunde: str, gueltig_bis: date, mandant: str | None = None, sitze: int | None = None) -> str:
    from cryptography.hazmat.primitives.asymmetric import ed25519
    if edition not in EDITIONEN:
        raise Lizenzfehler(f"Unbekannte Edition {edition!r}")
    nutzdaten = {"v": 1, "edition": edition, "kunde": kunde, "gueltig_bis": gueltig_bis.isoformat(), "mandant": mandant or "*", "sitze": sitze,
                 "ausgestellt": date.today().isoformat()}
    roh = json.dumps(nutzdaten, separators=(",", ":"), sort_keys=True).encode()
    sig = ed25519.Ed25519PrivateKey.from_private_bytes(_unb64(privat)).sign(roh)
    return f"{_b64(roh)}.{_b64(sig)}"


def schluessel_pruefen(schluessel: str, oeffentlich: str | None, mandant_id: str, heute: date) -> dict[str, Any]:
    """Signatur, Mandantenbindung und Gültigkeit prüfen; liefert die Nutzdaten."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric import ed25519
    if not oeffentlich:
        raise Lizenzfehler("Kein öffentlicher Lizenzschlüssel hinterlegt (LIZENZ_OEFFENTLICHER_SCHLUESSEL)")
    try:
        roh_b64, sig_b64 = schluessel.strip().split(".")
        roh, sig = _unb64(roh_b64), _unb64(sig_b64)
        ed25519.Ed25519PublicKey.from_public_bytes(_unb64(oeffentlich)).verify(sig, roh)
        nutzdaten = json.loads(roh)
    except (ValueError, InvalidSignature) as e:
        raise Lizenzfehler("Lizenzschlüssel ungültig (Format oder Signatur)") from e
    if nutzdaten.get("edition") not in EDITIONEN:
        raise Lizenzfehler("Lizenzschlüssel nennt eine unbekannte Edition")
    if nutzdaten.get("mandant") not in ("*", mandant_id):
        raise Lizenzfehler("Lizenzschlüssel ist an einen anderen Mandanten gebunden")
    if date.fromisoformat(nutzdaten["gueltig_bis"]) < heute:
        raise Lizenzfehler(f"Lizenz am {nutzdaten['gueltig_bis']} abgelaufen")
    return nutzdaten


def oeffentlicher_schluessel() -> str | None:
    return os.environ.get("LIZENZ_OEFFENTLICHER_SCHLUESSEL") or None


# --- Wirksame Edition ---------------------------------------------------------------------------
def _datum(w: Any) -> date | None:
    if not w:
        return None
    return w if isinstance(w, date) else date.fromisoformat(str(w)[:10])


def testphase_bis(m: Mandant) -> date:
    if m.testphase_bis:
        return _datum(m.testphase_bis)
    erstellt = m.erstellt or datetime.now(timezone.utc)
    return (erstellt.date() if isinstance(erstellt, datetime) else erstellt) + timedelta(days=TESTPHASE_TAGE)


def status(m: Mandant, heute: date) -> dict[str, Any]:
    """{edition, quelle (lizenz|betreiber|testphase|starter), gueltig_bis, tage_rest, features, limits, testphase_bis, hinweis}"""
    liz = m.lizenz or {}
    edition, quelle_, gueltig_bis = None, None, None
    if liz.get("edition") in EDITIONEN and (not liz.get("gueltig_bis") or _datum(liz["gueltig_bis"]) >= heute):
        edition, quelle_, gueltig_bis = liz["edition"], liz.get("quelle", "lizenz"), _datum(liz.get("gueltig_bis"))
    tp = testphase_bis(m)
    if edition is None and tp >= heute:
        edition, quelle_, gueltig_bis = "enterprise", "testphase", tp
    if edition is None:
        edition, quelle_ = "starter", "starter"
    e = EDITIONEN[edition]
    tage = (gueltig_bis - heute).days if gueltig_bis else None
    hinweis = None
    if quelle_ == "testphase":
        hinweis = f"Testphase mit allen Funktionen, noch {tage} Tage. Danach gilt Starter; Lizenz unter Einstellungen eintragen."
    elif quelle_ == "starter" and liz.get("edition"):
        hinweis = f"Lizenz {liz.get('edition')} ist am {liz.get('gueltig_bis')} abgelaufen; Funktionen auf Starter begrenzt."
    elif tage is not None and tage <= 14:
        hinweis = f"Lizenz läuft in {tage} Tagen ab."
    return {"edition": edition, "titel": e["titel"], "quelle": quelle_, "gueltig_bis": gueltig_bis.isoformat() if gueltig_bis else None, "tage_rest": tage,
            "features": list(e["features"]), "limits": dict(e["limits"]), "testphase_bis": tp.isoformat(), "hinweis": hinweis,
            "lizenz_kunde": liz.get("kunde"), "editionen": {k: {"titel": v["titel"], "preis_monat": v["preis_monat"], "features": v["features"], "limits": v["limits"]}
                                                           for k, v in EDITIONEN.items()}, "feature_texte": FEATURES}


def hat_feature(m: Mandant, heute: date, feature: str) -> bool:
    return feature in status(m, heute)["features"]


def limit_pruefen(s: Session, m: Mandant, heute: date, art: str) -> None:
    """Wirft Lizenzfehler, wenn ein Mengenlimit der Edition erreicht ist (z. B. vor dem Anlegen eines Kunden/Mitarbeiters)."""
    from core.modelle import Ressource
    st = status(m, heute)
    grenze = st["limits"].get(art)
    if grenze is None:
        return
    if art == "kunden":
        n = s.scalar(select(func.count()).select_from(Kunde).where(Kunde.mandant_id == m.id)) or 0
    elif art == "mitarbeiter":
        n = s.scalar(select(func.count()).select_from(Ressource).where(Ressource.mandant_id == m.id, Ressource.art == "person", Ressource.aktiv.is_(True))) or 0
    else:
        return
    if n >= grenze:
        raise Lizenzfehler(f"Edition {st['titel']} erlaubt höchstens {grenze} {art}; aktuell {n}. Höhere Edition unter Einstellungen → Lizenz.")


def lizenz_setzen(m: Mandant, schluessel: str, heute: date) -> dict[str, Any]:
    nutz = schluessel_pruefen(schluessel, oeffentlicher_schluessel(), m.id, heute)
    m.lizenz = {"edition": nutz["edition"], "gueltig_bis": nutz["gueltig_bis"], "kunde": nutz.get("kunde"), "sitze": nutz.get("sitze"), "quelle": "lizenz",
                "schluessel": schluessel.strip(), "eingetragen": heute.isoformat()}
    return status(m, heute)


def edition_zuweisen(m: Mandant, edition: str | None, gueltig_bis: date | None, heute: date, akteur: str = "betreiber") -> dict[str, Any]:
    """Betreiberkonsole: Edition ohne Schlüssel setzen (z. B. nach Vertragsabschluss) oder zurücknehmen (None)."""
    if edition is None:
        m.lizenz = None
    else:
        if edition not in EDITIONEN:
            raise Lizenzfehler(f"Unbekannte Edition {edition!r}")
        m.lizenz = {"edition": edition, "gueltig_bis": gueltig_bis.isoformat() if gueltig_bis else None, "quelle": "betreiber", "akteur": akteur,
                    "eingetragen": heute.isoformat()}
    return status(m, heute)


def testphase_verlaengern(m: Mandant, tage: int, heute: date) -> date:
    neu = max(testphase_bis(m), heute) + timedelta(days=tage)
    m.testphase_bis = neu
    return neu


# --- White-Label und Produktinfo ----------------------------------------------------------------
def produkt() -> dict[str, Any]:
    return {"name": os.environ.get("PRODUKT_NAME") or "SHK-Backoffice", "anbieter": os.environ.get("PRODUKT_ANBIETER") or "",
            "farbe": os.environ.get("PRODUKT_FARBE") or "", "logo": os.environ.get("PRODUKT_LOGO_URL") or "", "version": __version__,
            "support_url": os.environ.get("PRODUKT_SUPPORT_URL") or "", "registrierung_offen": registrierung_offen(), "testphase_tage": TESTPHASE_TAGE,
            "editionen": {k: {"titel": v["titel"], "preis_monat": v["preis_monat"], "features": [FEATURES[f] for f in v["features"]], "limits": v["limits"]}
                          for k, v in EDITIONEN.items()}}


def registrierung_offen() -> bool:
    return os.environ.get("REGISTRIERUNG_OFFEN", "").lower() in ("1", "true", "ja")


# --- Betreiberkonsole ---------------------------------------------------------------------------
def mandanten_uebersicht(s: Session, heute: date) -> list[dict[str, Any]]:
    from core.domaene import als_utc
    seit = datetime.combine(heute - timedelta(days=30), datetime.min.time(), timezone.utc)
    ergebnis = []
    for m in s.scalars(select(Mandant).order_by(Mandant.erstellt.desc())):
        st = status(m, heute)
        kunden = s.scalar(select(func.count()).select_from(Kunde).where(Kunde.mandant_id == m.id)) or 0
        faelle = s.scalar(select(func.count()).select_from(Servicefall).where(Servicefall.mandant_id == m.id, Servicefall.erstellt >= seit)) or 0
        termine = s.scalar(select(func.count()).select_from(Termin).join(Termin.servicefall).where(Servicefall.mandant_id == m.id, Termin.beginn >= seit)) or 0
        letzter = s.scalar(select(func.max(Servicefall.erstellt)).where(Servicefall.mandant_id == m.id))
        ergebnis.append({"id": m.id, "name": m.name, "gewerk": m.gewerk, "erstellt": als_utc(m.erstellt).isoformat() if m.erstellt else None,
                         "edition": st["edition"], "quelle": st["quelle"], "gueltig_bis": st["gueltig_bis"], "tage_rest": st["tage_rest"], "kunden": kunden,
                         "vorgaenge_30d": faelle, "termine_30d": termine, "letzter_vorgang": als_utc(letzter).isoformat() if letzter else None,
                         "website": bool((m.stammdaten or {}).get("website", {}).get("aktiv")), "lizenz_kunde": (m.lizenz or {}).get("kunde")})
    return ergebnis


def summen(uebersicht: list[dict[str, Any]]) -> dict[str, Any]:
    je = {k: 0 for k in EDITIONEN}
    quellen = {"lizenz": 0, "betreiber": 0, "testphase": 0, "starter": 0}
    mrr = 0
    for e in uebersicht:
        je[e["edition"]] += 1
        quellen[e["quelle"]] += 1
        if e["quelle"] in ("lizenz", "betreiber"):
            mrr += EDITIONEN[e["edition"]]["preis_monat"]
    return {"mandanten": len(uebersicht), "je_edition": je, "je_quelle": quellen, "mrr_listenpreis": mrr, "aktiv_30d": sum(1 for e in uebersicht if e["vorgaenge_30d"])}
