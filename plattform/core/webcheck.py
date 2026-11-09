"""Website-Check: automatisierte Prüfung einer Betriebswebsite gegen die Problemcodes der Leadqualifizierung (P1–P10).

Vertriebsinstrument für Erstgespräch und Akquisebrief: Aus höchstens drei öffentlich abrufbaren Seiten (Startseite,
Impressum, Datenschutzerklärung) werden belegbare Befunde abgeleitet, jeweils mit Fundstelle („Beleg“) und der passenden
KUHN-Leistung aus dem Lösungskatalog. Der Check ersetzt keine Rechtsberatung; Befunde der Kategorie P9 sind Indizien
(z. B. extern geladene Google Fonts, LG München I, Urt. v. 20.01.2022 – 3 O 17493/20; Normverweis auf das TMG, das am
14.05.2024 durch das DDG abgelöst wurde; „Privacy Shield“, vom EuGH am 16.07.2020 in C-311/18 für ungültig erklärt).

Sicherheit: nur http/https, Ziel muss auf öffentliche IP-Adressen auflösen (kein SSRF auf interne Dienste), Antwort-
größe ≤ 2 MB, Zeitlimit je Abruf, höchstens fünf Weiterleitungen. Aufruf: `python -m core website-check <url>` (auch
`--datei urls.txt --csv ergebnis.csv` für Leadlisten) oder `POST /betreiber/website-check` (Maschinenschlüssel).
"""
from __future__ import annotations

import ipaddress
import re
import socket
import time
from dataclasses import asdict, dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from core.produkt import __version__

LOESUNG = {"P1": "W1/W2 Website „Start“ bzw. „Betrieb“ (Betriebswebsite aus KUHN)", "P2": "W1/W2 Anfrageformular mit Foto-Upload",
           "P3": "T2 Online- und Telefon-Terminbuchung", "P4": "T2/T3 Kundenportal mit Auftragsstatus", "P5": "T1 24/7-Annahme mit Dringlichkeitsstufe und Eskalation",
           "P6": "W3 Relaunch und Betreuung", "P7": "T1 Rufumleitung bei Nichtannahme auf den KI-Telefonagenten", "P8": "T1–T3 je nach Aufgabenprofil",
           "P9": "W3 Rechts-Check (Impressum, Datenschutz, Einwilligung)", "P10": "W2 Karriereseite mit Kurzbewerbung + S1 Stellenposts"}
TITEL = {"P1": "keine (funktionsfähige) Website", "P2": "kein Kontakt-/Anfrageformular", "P3": "keine Online-Terminbuchung",
         "P4": "kein Kundenportal/Statusprozess", "P5": "Notdienst nur telefonisch", "P6": "veraltete Website", "P7": "Mobilnummer als Hauptnummer",
         "P8": "Büro/Telefon personell nicht gedeckt", "P9": "Rechts-/DSGVO-Risiko", "P10": "keine Karriereseite"}
GEWICHT = {"hoch": 15, "mittel": 8, "niedrig": 3}
MAX_BYTES = 2 * 1024 * 1024
AGENT = f"KUHN-Website-Check/{__version__} (single check, max 3 pages)"

CMS = [(re.compile(r"wordpress\s*([\d.]+)?", re.I), "WordPress"), (re.compile(r"joomla!?\s*([\d.]+)?", re.I), "Joomla"),
       (re.compile(r"typo3\s*(?:cms)?\s*([\d.]+)?", re.I), "TYPO3"), (re.compile(r"jimdo", re.I), "Jimdo"), (re.compile(r"wix\.com", re.I), "Wix"),
       (re.compile(r"ionos|1&1\s*mywebsite|mywebsite", re.I), "IONOS MyWebsite"), (re.compile(r"strato", re.I), "STRATO Homepage-Baukasten"),
       (re.compile(r"drupal\s*([\d.]+)?", re.I), "Drupal"), (re.compile(r"webnode", re.I), "Webnode")]
EINWILLIGUNG = re.compile(r"cookiebot|usercentrics|borlabs|klaro|consentmanager|cookie-script|onetrust|complianz|real-cookie-banner|ccm19|cookiefirst|iubenda", re.I)
TRACKING = re.compile(r"googletagmanager\.com|google-analytics\.com|gtag\(|connect\.facebook\.net|fbq\(|hotjar|clarity\.ms", re.I)
FONTS = re.compile(r"fonts\.(googleapis|gstatic)\.com", re.I)
MAPS = re.compile(r"(maps\.google\.|google\.[a-z.]+/maps|maps\.googleapis\.com)", re.I)
PLATZHALTER = re.compile(r"lorem ipsum|musterstra(ss|ß)e|\[(name|firma|adresse|inhaber)[^\]]{0,20}\]|ihr firmenname|max mustermann", re.I)
BUCHUNG = re.compile(r"termin\s*(online\s*)?buchen|online[- ]?termin|terminbuchung|calendly|doctolib|terminland|etermin|shore\.com|simplybook|timify|bookingkit|cituro", re.I)
PORTAL = re.compile(r"kundenportal|kunden-login|kundenlogin|auftragsstatus|reparaturstatus|mein konto", re.I)
NOTDIENST = re.compile(r"notdienst|notruf|24\s*/\s*7|24\s*stunden|rund um die uhr|störungsdienst|bereitschaft", re.I)
KARRIERE = re.compile(r"karriere|stellenangebot|jobs?\b|wir stellen ein|ausbildung|bewerb", re.I)
MOBIL = re.compile(r"(?:\+49|0049|\b0)\s*\(?0?\)?\s*1[5-7]\d[\d\s/\-]{6,12}\d")
JAHR = re.compile(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(\d{4})", re.I)
JQUERY = re.compile(r"jquery[.-]?(\d)\.(\d+)(?:\.\d+)?(?:\.min)?\.js|jquery\.js\?ver=(\d)\.(\d+)", re.I)


class Fehler(ValueError):
    pass


@dataclass
class Befund:
    code: str
    schwere: str
    titel: str
    beleg: str

    @property
    def loesung(self) -> str:
        return LOESUNG[self.code]


class _Seite(HTMLParser):
    """Sammelt Links (href, Text), Formulare, Eingabefelder, iframes, Skripte und Meta-Angaben ohne externe Abhängigkeit."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.meta: dict[str, str] = {}
        self.quellen: list[str] = []
        self.formulare = 0
        self.felder: list[str] = []
        self.text: list[str] = []
        self._a: str | None = None
        self._a_text: list[str] = []
        self._still = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "a":
            self._a, self._a_text = a.get("href", ""), []
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            if name:
                self.meta[name] = a.get("content", "")
        elif tag in ("script", "iframe", "link", "img"):
            q = a.get("src") or a.get("href") or ""
            if q:
                self.quellen.append(q)
            if tag == "script":
                self._still += 1
        elif tag == "style":
            self._still += 1
        elif tag == "form":
            self.formulare += 1
        elif tag in ("input", "textarea", "select"):
            self.felder.append("textarea" if tag == "textarea" else a.get("type", "text").lower())

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._a is not None:
            self.links.append((self._a, " ".join(self._a_text).strip()))
            self._a = None
        elif tag in ("script", "style") and self._still:
            self._still -= 1

    def handle_data(self, data: str) -> None:
        if self._still:
            return
        if self._a is not None:
            self._a_text.append(data.strip())
        if data.strip():
            self.text.append(data.strip())


def _oeffentlich(host: str) -> None:
    try:
        adressen = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except socket.gaierror as e:
        raise Fehler(f"Domain {host} nicht auflösbar") from e
    for adr in adressen:
        ip = ipaddress.ip_address(adr.split("%")[0])
        if not ip.is_global:
            raise Fehler(f"{host} zeigt auf eine nicht öffentliche Adresse ({ip})")


def url_normalisieren(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise Fehler("Keine Adresse angegeben")
    if "://" not in url:
        url = "https://" + url
    teile = urlparse(url)
    if teile.scheme not in ("http", "https") or not teile.hostname or "." not in teile.hostname:
        raise Fehler(f"Ungültige Adresse: {url}")
    return url


def _abrufen(client: httpx.Client, url: str, pruefen_dns: bool) -> tuple[httpx.Response, str, float]:
    aktuell = url
    beginn = time.monotonic()
    for _ in range(6):
        host = urlparse(aktuell).hostname or ""
        if pruefen_dns:
            _oeffentlich(host)
        with client.stream("GET", aktuell) as r:
            if r.is_redirect and r.headers.get("location"):
                aktuell = urljoin(aktuell, r.headers["location"])
                if urlparse(aktuell).scheme not in ("http", "https"):
                    raise Fehler("Weiterleitung auf ein unzulässiges Schema")
                continue
            daten = b""
            for teil in r.iter_bytes():
                daten += teil
                if len(daten) > MAX_BYTES:
                    break
            antwort = httpx.Response(r.status_code, headers=r.headers, content=daten[:MAX_BYTES], request=r.request)
            return antwort, aktuell, time.monotonic() - beginn
    raise Fehler("Zu viele Weiterleitungen")


def _parsen(html_text: str) -> _Seite:
    p = _Seite()
    try:
        p.feed(html_text)
    except Exception:  # noqa: BLE001  defektes HTML: verwerten, was gelesen wurde
        pass
    return p


def _link(seite: _Seite, muster: re.Pattern[str]) -> str | None:
    for href, text in seite.links:
        if muster.search(href) or muster.search(text):
            return href
    return None


def pruefen(url: str, transport: httpx.BaseTransport | None = None, heute: date | None = None, unterseiten: bool = True) -> dict[str, Any]:
    """Führt den Check aus; `transport` (Tests) ersetzt Netzwerk und DNS-Prüfung."""
    heute = heute or date.today()
    url = url_normalisieren(url)
    befunde: list[Befund] = []
    info: dict[str, Any] = {"url": url, "geprueft_am": heute.isoformat(), "seiten": []}
    with httpx.Client(transport=transport, timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False,
                      headers={"User-Agent": AGENT, "Accept": "text/html,application/xhtml+xml"}) as client:
        try:
            r, final, dauer = _abrufen(client, url, transport is None)
        except (httpx.HTTPError, Fehler) as e:
            if isinstance(e, Fehler) and "nicht öffentliche" in str(e):
                raise
            if url.startswith("https://"):   # manche Altseiten laufen nur über http
                try:
                    r, final, dauer = _abrufen(client, "http://" + url.removeprefix("https://"), transport is None)
                except (httpx.HTTPError, Fehler):
                    r = None
            else:
                r = None
            if r is None:
                befunde.append(Befund("P1", "hoch", "Website nicht erreichbar", f"Abruf von {url} fehlgeschlagen ({type(e).__name__}: {str(e)[:120]})"))
                return _ergebnis(info, befunde)
        info.update({"final_url": final, "status": r.status_code, "ladezeit_s": round(dauer, 2), "groesse_kb": round(len(r.content) / 1024, 1)})
        if r.status_code >= 400:
            befunde.append(Befund("P1", "hoch", "Website liefert einen Fehler", f"HTTP {r.status_code} unter {final}"))
            return _ergebnis(info, befunde)
        html_text = r.text
        seite = _parsen(html_text)
        info["seiten"].append(final)
        sichtbar = " ".join(seite.text)
        quellen = " ".join(seite.quellen) + " " + html_text[:400000]

        if len(sichtbar) < 200 and seite.formulare == 0:
            befunde.append(Befund("P1", "mittel", "Website ohne nennenswerten Inhalt (Platzhalter- oder Parkseite)", f"{len(sichtbar)} Zeichen sichtbarer Text"))
        # --- Technik und Aktualität (P6)
        if not final.startswith("https://"):
            befunde.append(Befund("P9", "hoch", "keine verschlüsselte Verbindung (HTTPS)", f"Endadresse {final}; Formulareingaben werden unverschlüsselt übertragen"))
        if "viewport" not in seite.meta:
            befunde.append(Befund("P6", "mittel", "nicht für Smartphones optimiert", "kein <meta name=\"viewport\">; über 60 % der Handwerkersuchen erfolgen mobil"))
        jahre = [int(j) for j in JAHR.findall(sichtbar) if 1995 <= int(j) <= heute.year]
        if jahre and max(jahre) <= heute.year - 3:
            befunde.append(Befund("P6", "niedrig", "Website seit Jahren nicht gepflegt", f"Copyright-Vermerk {max(jahre)}"))
        generator = seite.meta.get("generator", "")
        info["cms"] = None
        for muster, name in CMS:
            treffer = muster.search(generator) or (muster.search(quellen) if name in ("Jimdo", "Wix", "IONOS MyWebsite", "STRATO Homepage-Baukasten", "Webnode") else None)
            if treffer:
                version = treffer.group(1) if treffer.groups() and treffer.group(1) else None
                info["cms"] = f"{name} {version}".strip() if version else name
                if name == "WordPress" and version and int(version.split(".")[0]) < 6:
                    befunde.append(Befund("P6", "hoch", "veraltete WordPress-Version", f"Generator „{generator}“ (ohne Sicherheitsupdates)"))
                elif name == "Joomla" and version and int(version.split(".")[0]) < 4:
                    befunde.append(Befund("P6", "hoch", "veraltete Joomla-Version", f"Generator „{generator}“ (Joomla 3 ohne Support seit 17.08.2023)"))
                elif name in ("Jimdo", "IONOS MyWebsite", "STRATO Homepage-Baukasten", "Webnode"):
                    befunde.append(Befund("P6", "niedrig", "Baukastenwebsite", f"{name}: begrenzte Funktionen (Buchung, Portal, SEO)"))
                break
        jq = JQUERY.search(quellen)
        if jq:
            haupt = int(jq.group(1) or jq.group(3))
            if haupt < 3:
                befunde.append(Befund("P6", "mittel", "veraltete Skriptbibliothek", f"jQuery {haupt}.x eingebunden (bekannte Sicherheitslücken)"))
        if dauer > 4:
            befunde.append(Befund("P6", "niedrig", "lange Ladezeit", f"{dauer:.1f} s bis zur Antwort"))
        # --- Recht und Datenschutz (P9)
        einwilligung = bool(EINWILLIGUNG.search(quellen))
        if FONTS.search(quellen):
            befunde.append(Befund("P9", "mittel", "Google Fonts werden vom Google-Server geladen", "fonts.googleapis.com/fonts.gstatic.com ohne Einwilligung (LG München I, 3 O 17493/20)"))
        if MAPS.search(quellen) and not einwilligung:
            befunde.append(Befund("P9", "mittel", "Google Maps ohne erkennbare Einwilligungslösung", "Karteneinbindung überträgt IP-Adressen an Google"))
        if TRACKING.search(quellen) and not einwilligung:
            befunde.append(Befund("P9", "hoch", "Tracking ohne erkennbares Einwilligungsbanner", "Analyse-/Werbeskripte (Google, Meta o. ä.) ohne Consent-Manager (§ 25 TDDDG)"))
        if PLATZHALTER.search(sichtbar):
            befunde.append(Befund("P9", "mittel", "Platzhaltertext auf der Website", f"„{PLATZHALTER.search(sichtbar).group(0)}“"))
        impressum = _link(seite, re.compile(r"impressum|imprint|anbieterkennzeichnung", re.I))
        datenschutz = _link(seite, re.compile(r"datenschutz|privacy", re.I))
        if not impressum:
            befunde.append(Befund("P9", "hoch", "kein Impressum verlinkt", "Pflichtangaben nach § 5 DDG nicht auffindbar"))
        if not datenschutz:
            befunde.append(Befund("P9", "hoch", "keine Datenschutzerklärung verlinkt", "Informationspflicht nach Art. 13 DSGVO"))
        # --- Kontaktwege und Prozesse (P2–P5, P7, P10)
        hat_formular = seite.formulare > 0 and ("textarea" in seite.felder or "email" in seite.felder)
        if not hat_formular:
            befunde.append(Befund("P2", "mittel", "kein Kontakt- oder Anfrageformular", "Anfragen nur per Telefon/E-Mail möglich"))
        if not BUCHUNG.search(sichtbar + " " + quellen):
            befunde.append(Befund("P3", "mittel", "keine Online-Terminbuchung", "kein Buchungslink oder -widget gefunden"))
        if not PORTAL.search(sichtbar + " " + " ".join(h for h, _ in seite.links)):
            befunde.append(Befund("P4", "niedrig", "kein Kundenportal/Auftragsstatus", "Status nur auf Nachfrage"))
        if NOTDIENST.search(sichtbar) and not hat_formular:
            befunde.append(Befund("P5", "hoch", "Notdienst nur telefonisch erreichbar", "Notdienst beworben, aber kein schriftlicher Eingang/keine 24/7-Annahme erkennbar"))
        telefone = [h.removeprefix("tel:") for h, _ in seite.links if h.startswith("tel:")] + MOBIL.findall(sichtbar)
        mobil = next((t for t in telefone if MOBIL.search(t)), None)
        if mobil:
            befunde.append(Befund("P7", "mittel", "Mobilnummer als Kontaktnummer", f"{mobil.strip()}"))
        if not _link(seite, KARRIERE) and not re.search(r"wir stellen ein|stellenangebote", sichtbar, re.I):
            befunde.append(Befund("P10", "niedrig", "keine Karriereseite", "kein Link auf Stellen/Karriere (relevant, falls Personal gesucht wird)"))
        info["kontakt"] = {"formular": hat_formular, "telefon": telefone[:3], "impressum": impressum, "datenschutz": datenschutz}

        # --- Unterseiten: Impressum, Datenschutz (veraltete Normverweise)
        if unterseiten:
            for art, href in (("impressum", impressum), ("datenschutz", datenschutz)):
                if not href or href.startswith(("mailto:", "tel:", "javascript:")):
                    continue
                ziel = urljoin(final, href)
                if urlparse(ziel).hostname != urlparse(final).hostname:
                    continue
                try:
                    ru, final_u, _ = _abrufen(client, ziel, transport is None)
                except (httpx.HTTPError, Fehler):
                    continue
                if ru.status_code >= 400:
                    befunde.append(Befund("P9", "hoch", f"{art.capitalize()} verlinkt, aber nicht abrufbar", f"HTTP {ru.status_code} unter {final_u}"))
                    continue
                info["seiten"].append(final_u)
                text_u = " ".join(_parsen(ru.text).text)
                if art == "impressum" and re.search(r"§\s*5\s*(abs\.\s*1\s*)?tmg|telemediengesetz", text_u, re.I):
                    befunde.append(Befund("P9", "niedrig", "Impressum verweist noch auf das TMG", "seit 14.05.2024 gilt § 5 DDG"))
                if art == "datenschutz" and re.search(r"privacy\s*shield", text_u, re.I):
                    befunde.append(Befund("P9", "mittel", "Datenschutzerklärung beruft sich auf „Privacy Shield“", "vom EuGH 2020 für ungültig erklärt (C-311/18)"))
                if PLATZHALTER.search(text_u):
                    befunde.append(Befund("P9", "hoch", f"Platzhalter im {art.capitalize()}", f"„{PLATZHALTER.search(text_u).group(0)}“"))
    return _ergebnis(info, befunde)


def _ergebnis(info: dict[str, Any], befunde: list[Befund]) -> dict[str, Any]:
    rang = {"hoch": 0, "mittel": 1, "niedrig": 2}
    befunde.sort(key=lambda b: (rang[b.schwere], int(b.code[1:])))
    codes = sorted({b.code for b in befunde}, key=lambda c: int(c[1:]))
    return {**info, "befunde": [{**asdict(b), "loesung": b.loesung} for b in befunde], "probleme": codes,
            "problemtitel": {c: TITEL[c] for c in codes}, "loesungen": {c: LOESUNG[c] for c in codes},
            "bedarf_score": min(100, sum(GEWICHT[b.schwere] for b in befunde)),
            "zusammenfassung": (f"{len(befunde)} Befunde ({', '.join(codes)})" if befunde else "keine Auffälligkeiten")}


def bericht_text(e: dict[str, Any]) -> str:
    """Kurzbericht für Gesprächsnotiz oder Akquisebrief (Klartext)."""
    zeilen = [f"Website-Check {e['url']} am {e['geprueft_am']}: {e['zusammenfassung']}, Bedarfsscore {e['bedarf_score']}/100"]
    if e.get("cms"):
        zeilen.append(f"System: {e['cms']}")
    for b in e["befunde"]:
        zeilen.append(f"- [{b['code']}, {b['schwere']}] {b['titel']}: {b['beleg']} → {b['loesung']}")
    return "\n".join(zeilen)
