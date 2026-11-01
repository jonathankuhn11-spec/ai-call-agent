"""Textvorlagen für Kundennachrichten (Terminbestätigung, Nachricht, Angebot, Rechnung, Mahnung) auf Basis von Jinja2.

Jede Vorlage hat einen Standardtext; Betriebe überschreiben einzelne Vorlagen in `stammdaten["vorlagen"]`.
Gerendert wird in einer Sandbox (jinja2.sandbox.SandboxedEnvironment): kein Zugriff auf Attribute mit Unterstrich,
keine Dateisystem- oder Importfunktionen, nur die übergebenen Variablen. Platzhalter sind bewusst flach gehalten,
damit ein Büro sie ohne Programmierkenntnis anpassen kann (`{{ kunde.name }}`, `{{ termin.datum }}` …)."""
from __future__ import annotations

from typing import Any

from jinja2 import StrictUndefined, TemplateSyntaxError
from jinja2.exceptions import SecurityError, UndefinedError
from jinja2.sandbox import ImmutableSandboxedEnvironment

from core.modelle import Mandant

GRUSS = "Mit freundlichen Grüßen\n{{ betrieb.name }}\n{{ betrieb.telefon }}"

STANDARD: dict[str, dict[str, Any]] = {
    "terminbestaetigung_email": {
        "titel": "Terminbestätigung (E-Mail)", "kanal": "email",
        "betreff": "Terminbestätigung {{ termin.datum }} {{ termin.uhrzeit }}",
        "text": ("Guten Tag {{ kunde.name }},\n\nwir bestätigen Ihren Termin am {{ termin.wochentag }}, {{ termin.datum }} um {{ termin.uhrzeit }} Uhr.\n"
                 "Anliegen: {{ anliegen }}\nMonteur: {{ monteur }}\n\n"
                 "{% if portal_link %}Termin online bestätigen oder absagen: {{ portal_link }}\n{% endif %}"
                 "{% for h in hinweise %}Hinweis: {{ h }}\n{% endfor %}"
                 "{% if no_show_hinweis %}{{ no_show_hinweis }}\n{% endif %}"
                 "Sollte der Termin nicht passen, rufen Sie uns bitte an.\n\n" + GRUSS)},
    "terminbestaetigung_sms": {
        "titel": "Terminbestätigung (SMS)", "kanal": "sms",
        "text": "{{ betrieb.name }}: Ihr Termin am {{ termin.datum_kurz }} um {{ termin.uhrzeit }} Uhr ({{ anliegen[:60] }}) ist bestätigt. Fragen: {{ betrieb.telefon }}"},
    "terminbestaetigung_whatsapp": {
        "titel": "Terminbestätigung (WhatsApp)", "kanal": "whatsapp",
        "text": ("{{ betrieb.name }}: Ihr Termin am {{ termin.datum }} um {{ termin.uhrzeit }} Uhr ({{ anliegen[:80] }}) ist bestätigt. "
                 "Antworten Sie einfach auf diese Nachricht, wenn Sie Fragen haben.")},
    "rechnung_email": {
        "titel": "Rechnungsversand (E-Mail)", "kanal": "email",
        "betreff": "{{ art }} {{ nummer }}",
        "text": "Guten Tag {{ kunde.name }},\n\nanbei erhalten Sie die {{ art }} {{ nummer }} über {{ brutto }} €.\n\n" + GRUSS},
    "angebot_email": {
        "titel": "Angebotsversand (E-Mail)", "kanal": "email",
        "betreff": "Angebot {{ nummer }}: {{ titel }}",
        "text": ("Guten Tag {{ kunde.name }},\n\nanbei erhalten Sie unser Angebot {{ nummer }} über {{ brutto }} € (gültig bis {{ gueltig_bis }}). "
                 "Für Rückfragen stehen wir gern zur Verfügung.\n\n" + GRUSS)},
    "nachricht_email": {
        "titel": "Freie Nachricht (E-Mail, Abschluss)", "kanal": "email",
        "text": "{{ text }}\n\n" + GRUSS},
}

BEISPIEL: dict[str, Any] = {
    "kunde": {"name": "Familie Beispiel", "anrede": "Sehr geehrte Familie Beispiel"},
    "termin": {"datum": "17.11.2026", "datum_kurz": "17.11.", "uhrzeit": "10:00", "wochentag": "Dienstag"},
    "anliegen": "Wartung der Gastherme", "monteur": "Monteur A", "portal_link": "https://beispiel.de/portal/?f=…",
    "hinweise": ["Bitte halten Sie den Zugang zur Anlage frei."], "no_show_hinweis": "",
    "betrieb": {"name": "Demo Haustechnik GmbH", "telefon": "0221 123456"},
    "art": "Rechnung", "nummer": "RE-2026-0001", "brutto": "238,00", "titel": "Austausch Umwälzpumpe", "gueltig_bis": "17.12.2026",
    "text": "Ihre Ersatzteile sind eingetroffen.",
}

_umgebung = ImmutableSandboxedEnvironment(autoescape=False, keep_trailing_newline=True, trim_blocks=False, undefined=StrictUndefined)
_umgebung.filters["euro"] = lambda w: str(w).replace(".", ",")


class Vorlagenfehler(ValueError):
    pass


def eigene(m: Mandant) -> dict[str, dict[str, str]]:
    return dict((m.stammdaten or {}).get("vorlagen") or {})


def quelle(m: Mandant, schluessel: str) -> dict[str, Any]:
    """Wirksame Vorlage (Betrieb vor Standard)."""
    if schluessel not in STANDARD:
        raise KeyError(schluessel)
    v = dict(STANDARD[schluessel])
    v.update({k: w for k, w in eigene(m).get(schluessel, {}).items() if k in ("text", "betreff") and w})
    return v


def _rendern(text: str, kontext: dict[str, Any]) -> str:
    try:
        return _umgebung.from_string(text).render(**kontext).strip()
    except TemplateSyntaxError as e:
        raise Vorlagenfehler(f"Vorlage fehlerhaft (Zeile {e.lineno}): {e.message}") from e
    except SecurityError as e:
        raise Vorlagenfehler(f"Vorlage nicht erlaubt: {e}") from e
    except UndefinedError as e:
        raise Vorlagenfehler(f"Unbekannter Platzhalter: {e}") from e


def rendern(m: Mandant, schluessel: str, kontext: dict[str, Any]) -> dict[str, str]:
    """Liefert {"text": …, "betreff": …} der wirksamen Vorlage; bei fehlerhafter eigener Vorlage fällt der Versand auf den
    Standard zurück (Zustellung darf nicht an einem Tippfehler im Büro scheitern)."""
    kontext = {**_betrieb(m), **kontext}
    v = quelle(m, schluessel)
    try:
        return {"text": _rendern(v["text"], kontext), "betreff": _rendern(v.get("betreff", ""), kontext)}
    except Vorlagenfehler:
        st = STANDARD[schluessel]
        return {"text": _rendern(st["text"], kontext), "betreff": _rendern(st.get("betreff", ""), kontext)}


def _betrieb(m: Mandant) -> dict[str, Any]:
    st = m.stammdaten or {}
    return {"betrieb": {"name": m.name, "telefon": st.get("telefon", ""), "email": st.get("email", ""), "whatsapp": st.get("whatsapp_nummer", "")}}


def vorschau(m: Mandant, schluessel: str, text: str | None = None, betreff: str | None = None) -> dict[str, str]:
    """Rendert einen Entwurf (oder die wirksame Vorlage) mit Beispieldaten; Fehler werden als Vorlagenfehler gemeldet."""
    v = quelle(m, schluessel)
    kontext = {**BEISPIEL, **_betrieb(m)}
    return {"text": _rendern(text if text is not None else v["text"], kontext), "betreff": _rendern(betreff if betreff is not None else v.get("betreff", ""), kontext)}


def pruefen(schluessel: str, text: str, betreff: str | None = None) -> None:
    if schluessel not in STANDARD:
        raise Vorlagenfehler(f"Unbekannte Vorlage {schluessel!r}")
    if len(text) > 4000 or len(betreff or "") > 200:
        raise Vorlagenfehler("Vorlage zu lang (Text max. 4000, Betreff max. 200 Zeichen)")
    kontext = {**BEISPIEL, **{"betrieb": BEISPIEL["betrieb"]}}
    _rendern(text, kontext)
    if betreff:
        _rendern(betreff, kontext)


def alle(m: Mandant) -> list[dict[str, Any]]:
    eig = eigene(m)
    return [{"schluessel": k, "titel": v["titel"], "kanal": v["kanal"], "standard_text": v["text"], "standard_betreff": v.get("betreff", ""),
             "text": eig.get(k, {}).get("text") or "", "betreff": eig.get(k, {}).get("betreff") or "", "angepasst": bool(eig.get(k, {}).get("text") or eig.get(k, {}).get("betreff")),
             "platzhalter": PLATZHALTER.get(v["kanal"], []) + PLATZHALTER_JE.get(k, [])} for k, v in STANDARD.items()]


def speichern(m: Mandant, daten: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """Leere Texte löschen die Anpassung (Standard gilt wieder)."""
    neu = eigene(m)
    for k, v in daten.items():
        if k not in STANDARD:
            raise Vorlagenfehler(f"Unbekannte Vorlage {k!r}")
        text, betreff = (v or {}).get("text") or "", (v or {}).get("betreff") or ""
        if not text and not betreff:
            neu.pop(k, None)
            continue
        pruefen(k, text or STANDARD[k]["text"], betreff)
        neu[k] = {kk: vv for kk, vv in {"text": text, "betreff": betreff}.items() if vv}
    m.stammdaten = {**(m.stammdaten or {}), "vorlagen": neu}
    return neu


PLATZHALTER = {"email": ["betrieb.name", "betrieb.telefon", "betrieb.email", "kunde.name"], "sms": ["betrieb.name", "betrieb.telefon", "kunde.name"],
               "whatsapp": ["betrieb.name", "betrieb.telefon", "kunde.name"]}
PLATZHALTER_JE = {
    "terminbestaetigung_email": ["termin.datum", "termin.uhrzeit", "termin.wochentag", "anliegen", "monteur", "portal_link", "hinweise", "no_show_hinweis"],
    "terminbestaetigung_sms": ["termin.datum_kurz", "termin.uhrzeit", "anliegen"],
    "terminbestaetigung_whatsapp": ["termin.datum", "termin.uhrzeit", "anliegen"],
    "rechnung_email": ["art", "nummer", "brutto"], "angebot_email": ["nummer", "titel", "brutto", "gueltig_bis"], "nachricht_email": ["text"],
}


WOCHENTAGE = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")


def termin_kontext(t, fall, kunde, m: Mandant, beginn) -> dict[str, Any]:
    """Gemeinsamer Kontext der Terminbestätigung für E-Mail, SMS und WhatsApp (beginn: Ortszeit)."""
    from core import branchen, portal
    return {"kunde": {"name": kunde.name}, "termin": {"datum": beginn.strftime("%d.%m.%Y"), "datum_kurz": beginn.strftime("%d.%m."), "uhrzeit": beginn.strftime("%H:%M"),
                                                      "wochentag": WOCHENTAGE[beginn.weekday()]},
            "anliegen": fall.anliegen or "", "monteur": t.ressource.name if t.ressource else "", "portal_link": portal.link(m, fall.id) or "",
            "hinweise": list(branchen.branche(m).hinweise_kunde), "no_show_hinweis": (m.stammdaten or {}).get("no_show_hinweis") or ""}
