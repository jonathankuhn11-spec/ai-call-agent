import asyncio

import httpx

from voice.werkzeuge import KernClient


def test_kernclient_gegen_core(umgebung):
    c, _, ids, h = umgebung

    async def ablauf():
        kern = KernClient("http://core", ids["mandant"], transport=httpx.ASGITransport(app=c.app))
        kunde = await kern.kunde_suchen("0221 1234567")
        fenster = await kern.zeitfenster("Therme zeigt Störung", "gas_brennwert")
        z = fenster["zeitfenster"][0]
        fall = await kern.servicefall_anlegen(telefon="0221 1234567", name=kunde["name"], anliegen="Therme Störung",
                                              anlagentyp="gas_brennwert", ressource_id=z["ressource_id"], beginn=z["beginn"])
        ende = await kern.gespraech_beendet("Agent: Guten Tag …", telefon="0221 1234567",
                                            servicefall_id=fall["servicefall_id"])
        await kern.schliessen()
        return kunde, fall, ende

    kunde, fall, ende = asyncio.run(ablauf())
    assert kunde["gefunden"] and kunde["anlagen"][0]["typ"] == "Gas-Brennwertkessel"
    assert fall["termin"]["verbindlich"] is False and "Büro" in fall["termin"]["hinweis"]
    assert ende["kommunikation_id"]


def test_neuer_anrufer_wird_angelegt(umgebung):
    c, _, ids, h = umgebung

    async def ablauf():
        kern = KernClient("http://core", ids["mandant"], transport=httpx.ASGITransport(app=c.app))
        assert (await kern.kunde_suchen("0170 9999999"))["gefunden"] is False
        fall = await kern.servicefall_anlegen(telefon="0170 9999999", name="Neu", anliegen="Wärmepumpe kalt",
                                              anlagentyp="waermepumpe", ort="Hürth")
        await kern.schliessen()
        return fall

    fall = asyncio.run(ablauf())
    kunden = c.get("/kunden", headers=h, params={"telefon": "+491709999999"}).json()
    assert len(kunden) == 1 and kunden[0]["ort"] == "Hürth"
    detail = c.get(f"/servicefaelle/{fall['servicefall_id']}", headers=h).json()["servicefall"]
    assert detail["benoetigte_qualifikation"] == "kaeltetechnik" and detail["quelle"] == "telefon"
