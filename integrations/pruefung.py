"""Abnahme eines laufenden Deployments von außen: python -m integrations check --url https://backend.kunde.example

Die Checkliste (docs/playbook/02, Abschnitt G) und die Go/No-Go-Liste des Runbooks (04) haben Punkte,
die sich maschinell prüfen lassen, und zwar genau so, wie die Plattform das Backend sieht: über HTTP,
mit den Geheimnissen, die auch telli benutzt. Dieses Modul schickt diese Requests, misst die Latenz
und fasst das Ergebnis als Go/No-Go zusammen. Es ändert keine Geschäftsdaten: Es liest, schickt
absichtlich ungültige oder unsignierte Anfragen (die abgelehnt werden müssen) und ein signiertes
Ereignis, das die Nachbereitung laut Kontrakt ignoriert. Einzige Spur: Ist der Testlead qualifiziert,
vermerkt /calendar/available die ihm gezeigten Slots als Angebot (24 Stunden gültig), wie bei jedem
Gespräch; deshalb einen eigenen Testlead benennen (--lead), nie einen echten Kunden.

Jeder Prüfpunkt hat einen Ausgang: ok, warnung (Go mit Hinweis) oder fehler (No-Go). Der Bericht
geht als Tabelle auf die Konsole, als JSON in die CI und als Abnahmeprotokoll (Markdown) in die Akte.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx

from integrations.konfig import Konfig
from integrations.modelle import iso_lesen, iso_utc
from integrations.signatur import geheimnisse, svix_kopfzeilen, telli_signatur
from integrations.uhr import Uhr

UNBEKANNTE_NUMMER = "+49 000 0000000"       # existiert in keinem CRM: Contact Lookup muss {"contact": null} liefern
BUDGET_MS = {"tools": 800, "calendar": 1000, "lookup": 800, "webhook": 1500}


@dataclass
class Pruefpunkt:
    name: str
    ausgang: str            # ok | warnung | fehler | uebersprungen
    detail: str = ""
    ms: Optional[float] = None


@dataclass
class Bericht:
    url: str
    zeit: str
    punkte: list[Pruefpunkt] = field(default_factory=list)

    @property
    def go(self) -> bool:
        return not any(p.ausgang == "fehler" for p in self.punkte)

    @property
    def zusammenfassung(self) -> dict[str, int]:
        aus = {"ok": 0, "warnung": 0, "fehler": 0, "uebersprungen": 0}
        for p in self.punkte:
            aus[p.ausgang] += 1
        return aus

    def als_dict(self) -> dict[str, Any]:
        return {"url": self.url, "zeit": self.zeit, "go": self.go, "zusammenfassung": self.zusammenfassung,
                "punkte": [p.__dict__ for p in self.punkte]}

    def als_tabelle(self) -> str:
        zeichen = {"ok": "✓", "warnung": "!", "fehler": "✗", "uebersprungen": "-"}
        zeilen = [f"Abnahme {self.url} ({self.zeit})", ""]
        for p in self.punkte:
            ms = f"{p.ms:6.0f} ms" if p.ms is not None else " " * 9
            zeilen.append(f"  {zeichen[p.ausgang]} {p.name:<28} {ms}  {p.detail}")
        z = self.zusammenfassung
        zeilen += ["", f"{'GO' if self.go else 'NO-GO'}: {z['ok']} ok, {z['warnung']} Warnungen, {z['fehler']} Fehler, "
                       f"{z['uebersprungen']} übersprungen"]
        return "\n".join(zeilen)

    def als_markdown(self, kunde: str = "") -> str:
        zeichen = {"ok": "ok", "warnung": "Warnung", "fehler": "Fehler", "uebersprungen": "übersprungen"}
        zeilen = [f"# Abnahmeprotokoll Integration{' ' + kunde if kunde else ''}", "",
                  f"Backend: `{self.url}`  ", f"Zeitpunkt: {self.zeit}  ",
                  f"Ergebnis: **{'GO' if self.go else 'NO-GO'}** ({self.zusammenfassung['fehler']} Fehler, "
                  f"{self.zusammenfassung['warnung']} Warnungen)", "",
                  "| Prüfpunkt | Ergebnis | Latenz | Befund |", "| --- | --- | --- | --- |"]
        for p in self.punkte:
            zeilen.append(f"| {p.name} | {zeichen[p.ausgang]} | {f'{p.ms:.0f} ms' if p.ms is not None else ''} | {p.detail} |")
        zeilen += ["", "Prüfung von außen über HTTP mit den Geheimnissen der Plattform (`python -m integrations check`); "
                       "verändert keine Geschäftsdaten (einzige Spur: Angebotsvermerke für den Testlead, 24 h gültig). "
                       "Nicht maschinell prüfbar und von Hand abzunehmen: echte `call_ended`-Zustellung aus dem Portal, "
                       "Testanruf mit Telefon, Spiegelung im Kunden-CRM, Sperrlisten-Import."]
        return "\n".join(zeilen) + "\n"


class Abnahme:
    """Prüft ein laufendes Backend über seine HTTP-Schnittstelle. `client` ist für Tests (App im Prozess)."""

    def __init__(self, url: str, konfig: Konfig, client: Optional[httpx.Client] = None, lead: str = "1",
                 produktiv: bool = False, uhr: Optional[Uhr] = None, timeout: float = 10.0):
        self.url = url.rstrip("/")
        self.konfig, self.lead, self.produktiv = konfig, lead, produktiv
        self.client = client or httpx.Client(base_url=self.url, timeout=timeout)
        self.uhr = uhr or Uhr()
        self.bericht = Bericht(self.url, iso_utc(self.uhr()))
        self.latenzen: dict[str, list[float]] = {}

    # ------------------------------------------------------------ Hilfen
    def _anfrage(self, methode: str, pfad: str, gruppe: Optional[str] = None, **kw) -> tuple[Optional[httpx.Response], float, str]:
        t0 = time.perf_counter()
        try:
            r = self.client.request(methode, pfad, **kw)
        except httpx.HTTPError as e:
            return None, (time.perf_counter() - t0) * 1000, f"{type(e).__name__}: {str(e)[:120]}"
        ms = (time.perf_counter() - t0) * 1000
        if gruppe:
            self.latenzen.setdefault(gruppe, []).append(ms)
        return r, ms, ""

    def _punkt(self, name: str, ausgang: str, detail: str = "", ms: Optional[float] = None):
        self.bericht.punkte.append(Pruefpunkt(name, ausgang, detail, ms))

    def _json(self, r: Optional[httpx.Response]) -> dict:
        if r is None:
            return {}
        try:
            d = r.json()
        except ValueError:
            return {}
        return d if isinstance(d, dict) else {}

    def _bearer(self) -> dict[str, str]:
        """Bei Schlüsselrotation ("alt,neu") gilt das neueste Geheimnis, wie beim Server."""
        return {"Authorization": f"Bearer {geheimnisse(self.konfig.tool_secret)[-1]}"} if self.konfig.tool_secret else {}

    def _signiert(self, body: dict) -> tuple[bytes, dict[str, str]]:
        roh = json.dumps(body).encode()
        kopf = {"Content-Type": "application/json"}
        if self.konfig.telli_api_key:
            kopf["x-telli-signature"] = telli_signatur(geheimnisse(self.konfig.telli_api_key)[-1], roh)
        return roh, kopf

    # ------------------------------------------------------------ Prüfpunkte
    def pruefe_erreichbarkeit(self):
        teile = urlparse(self.url)
        if teile.scheme != "https" and teile.hostname not in ("localhost", "127.0.0.1"):
            self._punkt("https", "fehler" if self.produktiv else "warnung",
                        "telli ruft nur HTTPS auf; Staging und Produktion brauchen ein gültiges Zertifikat")
        else:
            self._punkt("https", "ok", "HTTPS" if teile.scheme == "https" else "lokal, HTTP erlaubt")
        r, ms, fehler = self._anfrage("GET", "/health")
        d = self._json(r)
        if r is None or r.status_code != 200 or d.get("status") != "ok":
            self._punkt("health", "fehler", fehler or f"HTTP {r.status_code if r else '-'}: {str(d)[:100]}", ms)
            return False
        self._punkt("health", "ok", f"Version {d.get('version')}, Serverzeit {d.get('zeit')}", ms)
        if d.get("geheimnisse_vollstaendig"):
            self._punkt("geheimnisse", "ok", "alle Prüfungen aktiv")
        else:
            self._punkt("geheimnisse", "fehler" if self.produktiv else "warnung",
                        "Server meldet fehlende Geheimnisse: Endpunkte laufen teils unsigniert (nur lokal vertretbar)")
        self._punkt("planer", "ok" if d.get("planer") else "warnung",
                    "Hintergrundplaner läuft" if d.get("planer") else "Hintergrundplaner läuft nicht: Outbox, Spiegelung, "
                    "Erinnerungen und Lead-Push bleiben liegen")
        try:
            abstand = abs((self.uhr() - iso_lesen(d["zeit"])).total_seconds())
            self._punkt("uhrzeit", "ok" if abstand <= 60 else "fehler",
                        f"Abweichung Server/Prüfer {abstand:.0f} s" + ("" if abstand <= 60 else " (Svix-Toleranz 300 s, Gesprächsfenster 60 s)"))
        except (KeyError, ValueError):
            self._punkt("uhrzeit", "warnung", "/health ohne lesbare Zeit")
        return True

    def pruefe_authentifizierung(self):
        r, ms, _ = self._anfrage("POST", "/tools/lookup_lead", json={"external_id": self.lead})
        if self.konfig.tool_secret:
            self._punkt("auth tools", "ok" if r is not None and r.status_code == 401 else "fehler",
                        "ohne Bearer-Token 401" if r is not None and r.status_code == 401 else
                        f"ohne Bearer-Token HTTP {r.status_code if r else '-'}: Custom Tools sind offen", ms)
            r, ms, _ = self._anfrage("POST", "/tools/lookup_lead", json={"external_id": self.lead},
                                     headers={"Authorization": "Bearer falsch"})
            if r is None or r.status_code != 401:
                self._punkt("auth tools falsch", "fehler", f"falsches Token HTTP {r.status_code if r else '-'}", ms)
        else:
            self._punkt("auth tools", "uebersprungen", "TOOL_SECRET nicht gesetzt")
        roh, _ = self._signiert({"contact": {}, "contact_id": "x", "external_contact_id": self.lead, "contact_details": {}})
        r, ms, _ = self._anfrage("POST", "/calendar/available", content=roh, headers={"Content-Type": "application/json",
                                                                                   "x-telli-signature": "0" * 64})
        if self.konfig.telli_api_key:
            self._punkt("auth kalender", "ok" if r is not None and r.status_code == 401 else "fehler",
                        "falsche x-telli-signature 401" if r is not None and r.status_code == 401 else
                        f"falsche Signatur HTTP {r.status_code if r else '-'}: Kalender und Contact Lookup sind offen", ms)
        else:
            self._punkt("auth kalender", "uebersprungen", "TELLI_API_KEY nicht gesetzt")
        body = json.dumps({"event": "contact_status_changed"}).encode()
        r, ms, _ = self._anfrage("POST", "/webhooks/telli", content=body, headers={"Content-Type": "application/json"})
        if self.konfig.telli_webhook_secret:
            geheimnis = geheimnisse(self.konfig.telli_webhook_secret)[-1]
            alt = svix_kopfzeilen(geheimnis, "msg_pruefung_alt", body, int(self.uhr().timestamp()) - 3600)
            r2, ms2, _ = self._anfrage("POST", "/webhooks/telli", content=body, headers={"Content-Type": "application/json", **alt})
            ok = r is not None and r.status_code == 401 and r2 is not None and r2.status_code == 401
            self._punkt("auth webhook", "ok" if ok else "fehler",
                        "unsigniert 401, Zeitstempel von vor einer Stunde 401" if ok else
                        f"unsigniert HTTP {r.status_code if r else '-'}, alter Zeitstempel HTTP {r2.status_code if r2 else '-'}", max(ms, ms2))
        else:
            self._punkt("auth webhook", "uebersprungen", "TELLI_WEBHOOK_SECRET nicht gesetzt")

    def pruefe_tools(self):
        r, ms, fehler = self._anfrage("POST", "/tools/lookup_lead", "tools", json={"external_id": self.lead}, headers=self._bearer())
        d = self._json(r)
        if r is None or r.status_code != 200:
            self._punkt("tool lookup_lead", "fehler", fehler or f"HTTP {r.status_code}: {str(d)[:100]}", ms)
        elif d.get("ok"):
            self._punkt("tool lookup_lead", "ok", f"Lead {self.lead}: {d.get('name')}, Status {d.get('status')}", ms)
        elif d.get("verifizierung_noetig"):
            self._punkt("tool lookup_lead", "ok", "Verifizierungspflicht: keine Auskunft vor verify_customer (nach Use Case)", ms)
        else:
            self._punkt("tool lookup_lead", "warnung", f"Lead {self.lead} nicht im CRM (--lead <ID> eines Testleads angeben): "
                                                        f"{str(d.get('fehler'))[:80]}", ms)
        r, ms, fehler = self._anfrage("POST", "/tools/update_lead", "tools", headers=self._bearer(),
                                      json={"external_id": self.lead, "status": "in_qualifizierung", "dachflaeche_m2": 99999})
        d = self._json(r)
        if r is not None and r.status_code == 200 and d.get("ok") is False and (d.get("validierungsfehler") or d.get("fehler")):
            self._punkt("tool fehler als 200", "ok", "ungültiger Wert kommt als HTTP 200 mit Fehlertext zurück (der Agent kann reagieren)", ms)
        elif r is not None and r.status_code == 404:
            self._punkt("tool fehler als 200", "uebersprungen", "update_lead gehört nicht zum Use Case", ms)
        else:
            self._punkt("tool fehler als 200", "fehler", fehler or f"HTTP {r.status_code if r else '-'}: {str(d)[:100]}", ms)
        r, ms, _ = self._anfrage("POST", "/tools/gibt_es_nicht", json={"external_id": self.lead}, headers=self._bearer())
        self._punkt("tool unbekannt", "ok" if r is not None and r.status_code == 404 else "fehler",
                    "unbekanntes Tool 404" if r is not None and r.status_code == 404 else f"HTTP {r.status_code if r else '-'}", ms)

    def pruefe_definitionen(self):
        r, ms, fehler = self._anfrage("GET", "/tools/definitions", headers=self._bearer())
        d = self._json(r)
        if r is None or r.status_code != 200 or "telli_custom_tools" not in d:
            self._punkt("tool-definitionen", "fehler", fehler or f"HTTP {r.status_code if r else '-'}", ms)
            return
        tools = d["telli_custom_tools"]
        fremd = [t["url"] for t in tools if not t["url"].startswith(self.url + "/")]
        namen = ", ".join(t["name"] for t in tools)
        if fremd:
            self._punkt("tool-definitionen", "fehler", f"Tool-URLs zeigen nicht auf {self.url}: {fremd[0]} "
                                                      "(Proxy oder Basis-URL im Portal prüfen)", ms)
        else:
            self._punkt("tool-definitionen", "ok", f"{len(tools)} Tools unter {self.url}/tools/: {namen}", ms)
        try:
            from integrations.usecases import alle
            erwartet = alle()[self.konfig.usecase].tool_namen()
            if [t["name"] for t in tools] != erwartet:
                self._punkt("tool-drift", "warnung", f"Server bietet {namen}; Use Case {self.konfig.usecase} erwartet "
                                                     f"{', '.join(erwartet)} (USECASE auf Server und Prüfer gleich?)")
            else:
                self._punkt("tool-drift", "ok", f"Tools entsprechen Use Case {self.konfig.usecase}")
        except KeyError:
            self._punkt("tool-drift", "warnung", f"Use Case {self.konfig.usecase} unbekannt")

    def pruefe_kalender(self):
        roh, kopf = self._signiert({"contact": {}, "contact_id": f"ct_{self.lead}", "external_contact_id": self.lead, "contact_details": {}})
        r, ms, fehler = self._anfrage("POST", "/calendar/available", "calendar", content=roh, headers=kopf)
        d = self._json(r)
        if r is None or r.status_code != 200 or not isinstance(d.get("available"), list):
            self._punkt("kalender available", "fehler", fehler or f"HTTP {r.status_code if r else '-'}: {str(d)[:100]}", ms)
            return
        slots = d["available"]
        form_ok = all(isinstance(s, dict) and str(s.get("start_iso", "")).endswith("Z") and str(s.get("end_iso", "")).endswith("Z") for s in slots)
        self._punkt("kalender available", "ok" if form_ok else "fehler",
                    f"{len(slots)} Slots, Form {{start_iso, end_iso}} in UTC" + ("" if slots else " (leer: Lead nicht qualifiziert oder keine freien Zeiten)")
                    if form_ok else "Slots nicht nach Kontrakt (start_iso/end_iso mit Z)", ms)
        roh, kopf = self._signiert({"contact": {}, "contact_id": f"ct_{self.lead}", "external_contact_id": self.lead,
                                    "contact_details": {}, "start_iso": "2000-01-01T09:00:00.000Z"})
        r, ms, fehler = self._anfrage("POST", "/calendar/book", "calendar", content=roh, headers=kopf)
        d = self._json(r)
        if r is not None and r.status_code == 200 and d.get("status") == "failed" and d.get("reason"):
            self._punkt("kalender book", "ok", f"Buchung eines vergangenen Slots: status failed, reason \"{d['reason'][:60]}\" (HTTP 200, nach Kontrakt)", ms)
        else:
            self._punkt("kalender book", "fehler", fehler or f"HTTP {r.status_code if r else '-'}: {str(d)[:100]}", ms)

    def pruefe_contact_lookup(self):
        roh, kopf = self._signiert({"event": "contact_lookup", "phone_number": UNBEKANNTE_NUMMER, "to_number": "+4900000"})
        r, ms, fehler = self._anfrage("POST", "/webhooks/contact-lookup", "lookup", content=roh, headers=kopf)
        d = self._json(r)
        if r is not None and r.status_code == 200 and "contact" in d and d["contact"] is None:
            self._punkt("contact lookup", "ok", "unbekannte Nummer: {\"contact\": null} nach Kontrakt", ms)
        else:
            self._punkt("contact lookup", "fehler", fehler or f"HTTP {r.status_code if r else '-'}: {str(d)[:100]}", ms)

    def pruefe_webhook(self):
        body = json.dumps({"event": "contact_status_changed", "pruefung": True}).encode()
        kopf = {"Content-Type": "application/json"}
        msg_id = f"msg_pruefung_{int(self.uhr().timestamp())}"
        if self.konfig.telli_webhook_secret:
            kopf.update(svix_kopfzeilen(self.konfig.telli_webhook_secret, msg_id, body, int(self.uhr().timestamp())))
        else:
            kopf["svix-id"] = msg_id
        r, ms, fehler = self._anfrage("POST", "/webhooks/telli", "webhook", content=body, headers=kopf)
        d = self._json(r)
        if r is not None and r.status_code == 200 and d.get("status") == "ignoriert":
            self._punkt("webhook signiert", "ok", "signiertes Ereignis angenommen, Typ ohne Verarbeitung: status ignoriert", ms)
        else:
            self._punkt("webhook signiert", "fehler", fehler or f"HTTP {r.status_code if r else '-'}: {str(d)[:100]}", ms)

    def pruefe_betrieb(self):
        r, ms, fehler = self._anfrage("GET", "/metrics", headers=self._bearer())
        d = self._json(r)
        if r is None or r.status_code != 200:
            self._punkt("metrics", "fehler", fehler or f"HTTP {r.status_code if r else '-'}", ms)
            return
        outbox = d.get("outbox") or {}
        tot = (outbox.get("tot") or 0) if isinstance(outbox, dict) else 0
        spiegel = d.get("spiegel") or {}
        tot_spiegel = (spiegel.get("tot") or 0) if isinstance(spiegel, dict) else 0
        kalender = d.get("kalender") or {}
        tot_kalender = ((kalender.get("auftraege") or {}).get("tot") or 0) if isinstance(kalender, dict) else 0
        offen = len(d.get("zuordnung_offen") or [])
        detail = (f"Outbox {outbox or 'nicht konfiguriert'}, Spiegel {spiegel or '-'}, Kalender {kalender.get('auftraege') if kalender else '-'}, "
                  f"Zuordnung offen {offen}")
        if tot or tot_spiegel or tot_kalender:
            self._punkt("metrics", "warnung", f"tote Einträge: Outbox {tot}, Spiegel {tot_spiegel}, Kalender {tot_kalender}; " + detail, ms)
        else:
            self._punkt("metrics", "ok", detail, ms)

    def pruefe_latenz(self):
        for gruppe, budget in BUDGET_MS.items():
            werte = self.latenzen.get(gruppe)
            if not werte:
                continue
            schlechtester = max(werte)
            self._punkt(f"latenz {gruppe}", "ok" if schlechtester <= budget else "warnung",
                        f"max {schlechtester:.0f} ms bei {len(werte)} Anfragen, Budget {budget} ms", schlechtester)

    # ------------------------------------------------------------ Lauf
    def ausfuehren(self) -> Bericht:
        schritte: list[Callable[[], Any]] = [self.pruefe_authentifizierung, self.pruefe_tools, self.pruefe_definitionen,
                                             self.pruefe_kalender, self.pruefe_contact_lookup, self.pruefe_webhook,
                                             self.pruefe_betrieb, self.pruefe_latenz]
        if not self.pruefe_erreichbarkeit():
            return self.bericht
        for schritt in schritte:
            try:
                schritt()
            except Exception as e:  # noqa: BLE001 - ein Prüfpunkt, der selbst abstürzt, ist ein Befund, kein Abbruch
                self._punkt(schritt.__name__.replace("pruefe_", ""), "fehler", f"Prüfung abgebrochen: {type(e).__name__}: {str(e)[:120]}")
        return self.bericht
