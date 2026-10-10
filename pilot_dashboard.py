"""
Pilot-Cockpit: Kennzahlen des laufenden Piloten aus der Integrationsdatenbank.
Start: python -m streamlit run pilot_dashboard.py            (Datenbank: DB_PATH oder crm.duckdb)
Daten zum Ausprobieren: python -m integrations demo --db pilot.duckdb, dann DB_PATH=pilot.duckdb

Zahlen und Definitionen kommen aus integrations/kennzahlen.py (dieselben wie im Wochenreport);
die Review-Liste aus integrations/review.py. Das UAT-Dashboard für Simulationsläufe bleibt dashboard.py.
"""
import os

import pandas as pd
import streamlit as st

import agent
from integrations import kennzahlen, review
from integrations.crm import LocalCRM
from integrations.konfig import Konfig

st.set_page_config(page_title="Pilot-Cockpit", page_icon="📊", layout="wide")
st.title("📊 Pilot-Cockpit: Voice-Agent SonnenWerk Energie")
st.caption("Ground Truth aus CRM, Kalender und Protokoll · Definitionen in docs/playbook/05-kpis-und-reporting.md")

konfig = Konfig.aus_umgebung()
pfad = st.sidebar.text_input("Datenbank", os.environ.get("DB_PATH", konfig.db_pfad))
tage = st.sidebar.slider("Zeitraum (Tage)", 1, 90, 7)
if not os.path.exists(pfad):
    st.info(f"Keine Datenbank unter `{pfad}`. Erst `python -m integrations demo --db {pfad}` oder den Server laufen lassen.")
    st.stop()

try:
    con = agent.init_db(path=pfad)
except Exception as e:  # noqa: BLE001 - DuckDB erlaubt nur einen schreibenden Prozess
    if "lock" in str(e).lower():
        st.error(f"`{pfad}` ist vom laufenden Server gesperrt. Für das Cockpit eine Kopie der Datei verwenden "
                 "(Server kurz stoppen oder `python -m integrations demo --db kopie.duckdb`).")
        st.stop()
    raise
crm = LocalCRM(con, konfig.zeitzone)
k = kennzahlen.kennzahlen(crm, tage=tage)


def wert(name, art="%"):
    return kennzahlen._fmt(k[name], art) + f"  {kennzahlen.status(k, name)}"


c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Speed-to-Lead (Median)", wert("speed_to_lead_min", "min"), f"{k['speed_to_lead_n']} Leads")
c2.metric("Erreichbarkeit", wert("erreichbarkeit"), f"{k['leads_erreicht']}/{k['leads_angerufen']} Leads")
c3.metric("Qualifizierungsquote", wert("qualifizierungsquote"))
c4.metric("Terminquote", wert("terminquote"), f"{k['termine']} Termine")
c5.metric("Fehlerquote", wert("fehlerquote"), f"{k['fehler_anrufe']} Anrufe")
c6.metric("Opt-out-Rate", wert("opt_out_rate"), f"{k['opt_outs']} Opt-outs")
c7, c8, c9, c10 = st.columns(4)
c7.metric("Anrufe im Zeitraum", k["anrufe"], f"{k['anrufe_erreicht']} erreicht")
c8.metric("Outbox", f"{wert('zustellquote')}", f"{k['outbox_tot']} tot")
c9.metric("Offene Rückruf-Aufgaben", k["aufgaben_offen"])
c10.metric("Gesperrte Nummern", k["gesperrt"])

links, rechts = st.columns(2)
with links:
    st.subheader("Gesprächsergebnisse")
    if k["ergebnisse"]:
        st.bar_chart(pd.Series(k["ergebnisse"]))
    else:
        st.write("Noch keine Anrufe im Zeitraum.")
with rechts:
    st.subheader("Anrufe je Tag")
    if k["anrufe_je_tag"]:
        st.line_chart(pd.Series(k["anrufe_je_tag"]))

st.subheader("Hinweise aus der Nachbereitung (Flags)")
if k["flags"]:
    st.dataframe(pd.DataFrame({"Flag": list(k["flags"]), "Anzahl": list(k["flags"].values())}), hide_index=True, width="stretch")
else:
    st.success("Keine Flags im Zeitraum.")

st.subheader("Transkript-Review")
reviews = review.aus_db(crm, tage=tage)
auffaellig = [r for r in reviews if r.befunde]
if not auffaellig:
    st.success(f"{len(reviews)} Anrufe geprüft, keine Befunde.")
for r in sorted(auffaellig, key=lambda r: (review.SCHWERE[r.schwerste], r.kennung)):
    with st.expander(f"{r.schwerste} · Anruf {r.kennung} · Lead {r.lead_id} · {r.ergebnis} · "
                     + ", ".join(dict.fromkeys(b.regel for b in r.befunde))):
        for b in r.befunde:
            st.markdown(f"**{b.schwere} {b.regel}** (Turn {b.turn}): {b.hinweis}" + (f"\n\n> {b.auszug}" if b.auszug else ""))

st.subheader("Wochenreport")
bericht = kennzahlen.wochenreport(k)
st.download_button("Wochenreport als Markdown", bericht, file_name="wochenreport.md")
with st.expander("Vorschau"):
    st.markdown(bericht)
