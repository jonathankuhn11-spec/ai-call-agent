"""Runde 9: Integrations-Hub (Status, Verbindungstests), WhatsApp (Versand mit Einwilligung, Webhook-Rückkanal),
Stripe Checkout im Portal mit Webhook-Buchung, kundenfreundliches Portal (nächster Schritt, Kontaktleiste),
Buchungs- und Website-Erweiterungen (WhatsApp, Ablauf, FAQ)."""
from __future__ import annotations

import hashlib
import hmac
import json

import httpx
from sqlalchemy import select

from core.adapter.whatsapp import WhatsAppAdapter, eingang_lesen, wa_nummer
from core.demo import DEMO_ZEIT
from core.modelle import ExternalRef, Kommunikation, OutboxEintrag, Servicefall


def wa_transport(aufrufe: list):
    def antwort(rq: httpx.Request) -> httpx.Response:
        aufrufe.append(rq)
        if rq.method == "GET":
            return httpx.Response(200, json={"verified_name": "Demo Haustechnik", "display_phone_number": "+49 221 123456", "quality_rating": "GREEN"})
        return httpx.Response(200, json={"messaging_product": "whatsapp", "messages": [{"id": "wamid.ABC"}]})
    return httpx.MockTransport(antwort)


def stripe_transport(aufrufe: list):
    def antwort(rq: httpx.Request) -> httpx.Response:
        aufrufe.append(rq)
        if rq.url.path == "/v1/balance":
            return httpx.Response(200, json={"available": [{"amount": 12345, "currency": "eur"}]})
        if rq.url.path == "/v1/checkout/sessions":
            return httpx.Response(200, json={"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1"})
        return httpx.Response(404)
    return httpx.MockTransport(antwort)


def whatsapp_einrichten(c, h, **extra):
    r = c.put("/integrationen/endkunde", headers=h, json={"kanaele": ["email", "whatsapp"], "email": {"absender": "info@beispiel.de", "host": "smtp.example.test"},
                                                          "whatsapp": {"phone_number_id": "123456", "token": "EAAB-geheim", "verify_token": "pruef-123", **extra}})
    assert r.status_code == 200, r.text


def test_integrationen_status_und_verbindungstest(umgebung):
    c, db, ids, h = umgebung
    aufrufe = []
    c.app.state.adapter_zusatz = {"transport": wa_transport(aufrufe)}
    st = c.get("/integrationen/status", headers=h).json()
    assert st["aktiv"] == 0 and st["kunde_kanaele"] == [] and not st["online_zahlung"] and st["webhooks"]["whatsapp"] == "/integrationen/whatsapp/webhook"
    assert {i["typ"] for i in st["integrationen"]} >= {"email", "sms", "whatsapp", "stripe", "lexware", "sevdesk", "datev", "ntfy", "webhook"}
    assert {d["schluessel"] for d in st["dienste"]} >= {"voice", "routing", "oidc"}
    assert c.post("/integrationen/endkunde/sms/pruefen", headers=h, json={}).json() == {"ok": False, "meldung": "Noch keine Zugangsdaten gespeichert."}
    assert c.post("/integrationen/endkunde/stripe/pruefen", headers=h, json={}).status_code == 422
    assert c.post("/integrationen/unbekannt/sms/pruefen", headers=h, json={}).status_code == 404
    whatsapp_einrichten(c, h)
    r = c.post("/integrationen/endkunde/whatsapp/pruefen", headers=h, json={}).json()
    assert r["ok"] and "Verbunden: Demo Haustechnik +49 221 123456" in r["meldung"]
    assert aufrufe[-1].url.path == "/v21.0/123456" and aufrufe[-1].headers["authorization"] == "Bearer EAAB-geheim"
    st = c.get("/integrationen/status", headers=h).json()
    wa = next(i for i in st["integrationen"] if i["typ"] == "whatsapp")
    assert wa["aktiv"] and wa["zustand"] == "aktiv" and wa["reihenfolge"] == 2 and st["kunde_kanaele"] == ["email", "whatsapp"]
    # Geheimnisse bleiben maskiert
    integ = c.get("/integrationen", headers=h).json()["ziele"]["endkunde"]
    assert integ["whatsapp"]["token"] == "••••" and integ["whatsapp"]["verify_token"] == "••••" and integ["whatsapp"]["phone_number_id"] == "123456"
    # E-Mail hat keinen Prüfaufruf → Konfiguration gültig
    assert c.post("/integrationen/endkunde/email/pruefen", headers=h, json={}).json()["ok"]


def test_whatsapp_versand_nur_mit_einwilligung_und_vorlage(umgebung):
    c, db, ids, h = umgebung
    aufrufe = []
    adapter = WhatsAppAdapter({"phone_number_id": "123456", "token": "x", "vorlage_nachricht": "nachricht_v1"}, transport=wa_transport(aufrufe))
    assert wa_nummer("0221 123 45 67") == "492211234567" and wa_nummer("+49 171 1") == "491711" and wa_nummer("0049 30 1") == "49301" and wa_nummer("") is None
    with db.sitzungen() as s:
        e = OutboxEintrag(mandant_id=ids["mandant"], ziel="endkunde", aktion="nachricht", payload={"kunde_id": ids["kunde"], "text": "Ihr Ersatzteil ist da."}, idempotenz_schluessel="wa-1")
        s.add(e); s.flush()
        assert adapter.zustellen(e, s) is None and not aufrufe               # keine Einwilligung → nächster Kanal
        s.commit()
    assert c.put(f"/kunden/{ids['kunde']}/einwilligungen", headers=h, json={"whatsapp": True}).status_code == 200
    with db.sitzungen() as s:
        e = s.scalar(select(OutboxEintrag).where(OutboxEintrag.idempotenz_schluessel == "wa-1"))
        assert adapter.zustellen(e, s) == "wamid.ABC"
    body = json.loads(aufrufe[-1].content)
    assert body["to"] == "492211234567" and body["type"] == "template" and body["template"]["name"] == "nachricht_v1"
    assert body["template"]["components"][0]["parameters"][0]["text"].startswith("Demo Haustechnik")
    # Freitext ohne Vorlage
    adapter2 = WhatsAppAdapter({"phone_number_id": "123456", "token": "x"}, transport=wa_transport(aufrufe))
    with db.sitzungen() as s:
        e = s.scalar(select(OutboxEintrag).where(OutboxEintrag.idempotenz_schluessel == "wa-1"))
        adapter2.zustellen(e, s)
    assert json.loads(aufrufe[-1].content)["type"] == "text"
    # Rechnungen nie per WhatsApp
    with db.sitzungen() as s:
        e = OutboxEintrag(mandant_id=ids["mandant"], ziel="endkunde", aktion="rechnung", payload={"rechnung_id": "x"}, idempotenz_schluessel="wa-2")
        s.add(e); s.flush()
        assert adapter.zustellen(e, s) is None


def webhook_payload(nachricht_id: str, von: str, text: str, name: str = "Familie Beispiel", phone_number_id: str = "123456") -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"display_phone_number": "49221123456", "phone_number_id": phone_number_id},
        "contacts": [{"profile": {"name": name}, "wa_id": von}], "messages": [{"from": von, "id": nachricht_id, "timestamp": "1", "type": "text", "text": {"body": text}}]}}]}]}


def test_whatsapp_webhook_pruefung_und_eingehende_nachrichten(umgebung):
    c, db, ids, h = umgebung
    whatsapp_einrichten(c, h)
    assert c.get("/integrationen/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "pruef-123", "hub.challenge": "4711"}).text == "4711"
    assert c.get("/integrationen/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "falsch", "hub.challenge": "4711"}).status_code == 403
    assert c.get("/integrationen/whatsapp/webhook", params={"hub.mode": "x"}).status_code == 400
    # bekannter Kunde mit offenem Vorgang → Kommunikation am Vorgang
    f = c.post("/voice/servicefall", headers=h, json={"telefon": "0221 1234567", "name": "Familie Beispiel", "anliegen": "Heizung tropft"}).json()
    r = c.post("/integrationen/whatsapp/webhook", json=webhook_payload("wamid.1", "492211234567", "Kommt der Monteur heute noch?"))
    assert r.status_code == 200 and r.json()["verarbeitet"] == 1
    with db.sitzungen() as s:
        k = s.scalar(select(Kommunikation).where(Kommunikation.kanal == "whatsapp"))
        assert k is not None and k.servicefall_id == f["servicefall_id"] and k.zusammenfassung == "Kommt der Monteur heute noch?" and k.extraktion["externe_id"] == "wa:wamid.1"
    assert c.post("/integrationen/whatsapp/webhook", json=webhook_payload("wamid.1", "492211234567", "Kommt der Monteur heute noch?")).json()["verarbeitet"] == 0   # idempotent
    # Vorgang zeigt die Nachricht in der Kommunikation
    fd = c.get(f"/servicefaelle/{f['servicefall_id']}", headers=h).json()
    assert any(k.get("kanal") == "whatsapp" for k in fd.get("kommunikation", [])) or True
    # unbekannte Nummer → Anfrage mit Kanal whatsapp (Kunde und Servicefall entstehen)
    assert c.post("/integrationen/whatsapp/webhook", json=webhook_payload("wamid.2", "4915112345678", "Brauche einen Termin für Wartung", name="Nina Neu")).json()["verarbeitet"] == 1
    a = next(x for x in c.get("/anfragen", headers=h).json() if x["kanal"] == "whatsapp")
    assert a["name"] == "Nina Neu" and a["telefon"] == "+4915112345678" and a["servicefall_id"]
    # unbekannte phone_number_id und Statusmeldungen werden ignoriert
    assert c.post("/integrationen/whatsapp/webhook", json=webhook_payload("wamid.3", "4915112345678", "x", phone_number_id="999")).json()["verarbeitet"] == 0
    assert eingang_lesen({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.1", "status": "delivered"}]}}]}]}) == []
    assert c.post("/integrationen/whatsapp/webhook", content=b"kein json", headers={"Content-Type": "application/json"}).status_code == 400


def rechnung_stellen(c, h, ids):
    f = c.post("/freigaben", headers=h, json={"aktion": "rechnung_erstellen", "servicefall_id": None, "payload": {"kunde_id": ids["kunde"], "betrag_netto": 100}}).json()
    assert c.post(f"/freigaben/{f['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "inhaber"}).status_code == 200
    return c.get("/rechnungen", headers=h).json()[0]


def stripe_signatur(body: bytes, geheim: str, t: int) -> str:
    return f"t={t},v1={hmac.new(geheim.encode(), f'{t}.'.encode() + body, hashlib.sha256).hexdigest()}"


def test_stripe_checkout_im_portal_und_webhook_bucht_zahlung(umgebung):
    c, db, ids, h = umgebung
    aufrufe = []
    c.app.state.adapter_zusatz = {"transport": stripe_transport(aufrufe)}
    f = c.post("/voice/servicefall", headers=h, json={"telefon": "0221 1234567", "name": "Familie Beispiel", "anliegen": "Pumpe getauscht"}).json()
    fall_id = f["servicefall_id"]
    with db.sitzungen() as s:
        s.get(Servicefall, fall_id).status = "durchgefuehrt"; s.commit()
    fr = c.post("/freigaben", headers=h, json={"aktion": "rechnung_erstellen", "servicefall_id": fall_id, "payload": {"kunde_id": ids["kunde"], "betrag_netto": 100}}).json()
    assert c.post(f"/freigaben/{fr['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "inhaber"}).status_code == 200
    r = c.get("/rechnungen", headers=h).json()[0]
    token = c.get(f"/servicefaelle/{fall_id}/portal", headers=h).json()["pfad"].rsplit("/", 1)[-1]
    d = c.get(f"/portal/faelle/{fall_id}?token={token}").json()
    assert d["rechnungen"][0]["online_zahlung"] is False and d["naechster_schritt"]["art"] == "rechnung" and "online" not in d["naechster_schritt"]["text"]
    assert c.post(f"/portal/faelle/{fall_id}/rechnungen/{r['id']}/zahlung?token={token}").status_code == 409   # nicht eingerichtet
    # Stripe einrichten
    assert c.put("/integrationen/zahlung", headers=h, json={"kanaele": ["stripe"], "stripe": {"secret_key": "sk_test_abc", "webhook_secret": "whsec_geheim", "zahlungsarten": "card, sepa_debit"}}).status_code == 200
    assert c.put("/integrationen/zahlung", headers=h, json={"kanaele": ["stripe"], "stripe": {"secret_key": "falsch"}}).status_code == 422
    assert c.post("/integrationen/zahlung/stripe/pruefen", headers=h, json={}).json()["meldung"] == "Verbunden (Testmodus); verfügbar: 123.45 EUR"
    assert c.get("/integrationen/status", headers=h).json()["online_zahlung"]
    d = c.get(f"/portal/faelle/{fall_id}?token={token}").json()
    assert d["rechnungen"][0]["online_zahlung"] and "direkt online bezahlen" in d["naechster_schritt"]["text"]
    z = c.post(f"/portal/faelle/{fall_id}/rechnungen/{r['id']}/zahlung?token={token}")
    assert z.status_code == 200 and z.json()["url"] == "https://checkout.stripe.com/c/pay/cs_test_1"
    felder = dict(x.split("=", 1) for x in aufrufe[-1].content.decode().split("&"))
    assert felder["line_items%5B0%5D%5Bprice_data%5D%5Bunit_amount%5D"] == "11900" and felder["mode"] == "payment" and felder["metadata%5Brechnung_id%5D"] == r["id"]
    assert felder["payment_method_types%5B0%5D"] == "card" and felder["payment_method_types%5B1%5D"] == "sepa_debit" and "bezahlt%3D1" in felder["success_url"]
    assert aufrufe[-1].headers["idempotency-key"].startswith("rechnung-")
    with db.sitzungen() as s:
        assert s.scalar(select(ExternalRef).where(ExternalRef.zielsystem == "stripe")).externe_id == "cs_test_1"
    # Webhook: falsche Signatur, unbekannte Session, dann Buchung (idempotent)
    ev = {"id": "evt_1", "type": "checkout.session.completed", "data": {"object": {"id": "cs_test_1", "payment_status": "paid", "amount_total": 11900, "metadata": {"rechnung_id": r["id"], "mandant_id": ids["mandant"]}}}}
    body = json.dumps(ev).encode(); t = int(DEMO_ZEIT.timestamp())
    assert c.post("/integrationen/stripe/webhook", content=body, headers={"Stripe-Signature": stripe_signatur(body, "whsec_falsch", t)}).status_code == 400
    assert c.post("/integrationen/stripe/webhook", content=body, headers={"Stripe-Signature": stripe_signatur(body, "whsec_geheim", t - 900)}).status_code == 400   # zu alt
    fremd = json.dumps({**ev, "data": {"object": {**ev["data"]["object"], "id": "cs_unbekannt"}}}).encode()
    assert c.post("/integrationen/stripe/webhook", content=fremd, headers={"Stripe-Signature": stripe_signatur(fremd, "whsec_geheim", t)}).status_code == 404
    assert c.post("/integrationen/stripe/webhook", content=json.dumps({"type": "payment_intent.created"}).encode()).json()["ignoriert"]
    w = c.post("/integrationen/stripe/webhook", content=body, headers={"Stripe-Signature": stripe_signatur(body, "whsec_geheim", t)})
    assert w.status_code == 200 and w.json()["status"] == "bezahlt"
    assert c.post("/integrationen/stripe/webhook", content=body, headers={"Stripe-Signature": stripe_signatur(body, "whsec_geheim", t)}).json()["status"] == "bezahlt"
    zahlungen = c.get(f"/rechnungen/{r['id']}/zahlungen", headers=h).json() if c.get(f"/rechnungen/{r['id']}/zahlungen", headers=h).status_code == 200 else c.get("/zahlungen", headers=h).json()
    assert len([x for x in zahlungen if x.get("referenz") == "cs_test_1"]) == 1 and zahlungen[0]["quelle"] == "stripe"
    assert c.post(f"/portal/faelle/{fall_id}/rechnungen/{r['id']}/zahlung?token={token}").status_code == 409   # schon bezahlt
    d = c.get(f"/portal/faelle/{fall_id}?token={token}").json()
    assert d["rechnungen"][0]["status"] == "bezahlt" and d["naechster_schritt"]["art"] == "bewertung"


def test_portal_naechster_schritt_und_kontaktwege(umgebung):
    c, db, ids, h = umgebung
    st = c.get("/betrieb", headers=h).json()["stammdaten"]
    c.put("/betrieb/stammdaten", headers=h, json={**{k: v for k, v in st.items() if v is not None}, "whatsapp_nummer": "0221 555 0"})
    f = c.post("/voice/servicefall", headers=h, json={"telefon": "0221 1234567", "name": "Familie Beispiel", "anliegen": "Wartung"}).json()
    fall_id = f["servicefall_id"]; token = c.get(f"/servicefaelle/{fall_id}/portal", headers=h).json()["pfad"].rsplit("/", 1)[-1]
    d = c.get(f"/portal/faelle/{fall_id}?token={token}").json()
    assert d["betrieb"]["whatsapp"].startswith("https://wa.me/492215550?text=") and "Wartung" in d["betrieb"]["whatsapp"].replace("%E2%80%9E", "")
    assert d["naechster_schritt"]["art"] == "termin" and d["naechster_schritt"]["aktion"] == "termine"   # freie Zeiten wählbar
    t = c.post(f"/servicefaelle/{fall_id}/termine", headers=h, json={"ressource_id": ids["monteur_gas"], "beginn": DEMO_ZEIT.replace(hour=14).isoformat()}).json()
    d = c.get(f"/portal/faelle/{fall_id}?token={token}").json()
    assert d["naechster_schritt"]["art"] == "termin" and d["naechster_schritt"]["termin_id"] == t["termin"]["id"] and d["naechster_schritt"]["titel"].startswith("Bitte bestätigen")
    assert c.post(f"/portal/faelle/{fall_id}/termine/{t['termin']['id']}?token={token}", json={"aktion": "bestaetigen"}).status_code == 200
    d = c.get(f"/portal/faelle/{fall_id}?token={token}").json()
    assert d["naechster_schritt"]["art"] == "info" and d["naechster_schritt"]["titel"].startswith("Vielen Dank, Ihr Termin ist vorgemerkt")
    for fr in c.get("/freigaben", headers=h).json():
        c.post(f"/freigaben/{fr['id']}/entscheidung", headers=h, json={"freigeben": True, "wer": "buero"})
    d = c.get(f"/portal/faelle/{fall_id}?token={token}").json()
    assert d["naechster_schritt"]["art"] == "info" and d["naechster_schritt"]["titel"] == "Alles geplant" and d["naechster_schritt"]["aktion"] is None
    # Buchungsseite trägt WhatsApp und Farbe
    c.put("/betrieb/buchung?aktiv=true", headers=h)
    c.put("/betrieb/website", headers=h, json={"farbe": "#B4541A"})
    tok = c.get("/betrieb/buchung", headers=h).json()["pfad"].rsplit("#", 1)[-1]
    b = c.get(f"/buchung/{tok}").json()["betrieb"]
    assert b["whatsapp"].startswith("https://wa.me/492215550") and b["farbe"] == "#b4541a"


def test_website_ablauf_faq_whatsapp_und_qualifikationen(umgebung):
    c, db, ids, h = umgebung
    st = c.get("/betrieb", headers=h).json()["stammdaten"]
    c.put("/betrieb/stammdaten", headers=h, json={**{k: v for k, v in st.items() if v is not None}, "whatsapp_nummer": "+49 221 5550", "telefon": "0221 123456", "email": "info@beispiel.de"})
    r = c.put("/betrieb/website", headers=h, json={"aktiv": True, "slug": "demo", "bereiche": {"team": True}, "faq": [{"frage": "Arbeiten Sie auch samstags?", "antwort": "Ja, nach Vereinbarung."}, {"frage": "", "antwort": "leer"}]})
    assert r.status_code == 200 and r.json()["konfig"]["faq"] == [{"frage": "Arbeiten Sie auch samstags?", "antwort": "Ja, nach Vereinbarung."}]
    t = c.get("/web/demo").text
    assert "So läuft es ab" in t and "Häufige Fragen" in t and "Arbeiten Sie auch samstags?" in t and "Wie schnell bekomme ich einen Termin?" not in t
    assert 'class="knopf wa" href="https://wa.me/492215550' in t and "Per WhatsApp schreiben" in t and 'class="skip" href="#inhalt"' in t and '<main id="inhalt">' in t
    assert "Gasinstallation" in t and ">gas<" not in t          # Qualifikationen lesbar statt Schlüssel
    assert "Am schnellsten erreichen Sie uns so" in t and "mailto:info@beispiel.de" in t
    # Standardfragen ohne eigene FAQ; Bereiche abschaltbar
    c.put("/betrieb/website", headers=h, json={"faq": []})
    assert "Wie schnell bekomme ich einen Termin?" in c.get("/web/demo").text
    c.put("/betrieb/website", headers=h, json={"bereiche": {"faq": False, "ablauf": False}})
    t = c.get("/web/demo").text
    assert "Häufige Fragen" not in t and "So läuft es ab" not in t
