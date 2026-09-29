"""
UAT-Dashboard für den Voice Agent.
Start: python -m streamlit run dashboard.py
"""
import json
import os

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Voice Agent UAT", page_icon="📞", layout="wide")
st.title("📞 Voice Agent: PV-Lead-Qualifizierung")
st.caption("Deployment-Pilot SonnenWerk Energie (fiktiv) · UAT mit simulierten Kunden")

if not os.path.exists("eval_results.json"):
    st.info("Noch keine Ergebnisse. Erst `python simulate.py` laufen lassen.")
    st.stop()

with open("eval_results.json", encoding="utf-8") as f:
    df = pd.DataFrame(json.load(f))
history = []
if os.path.exists("eval_history.json"):
    with open("eval_history.json", encoding="utf-8") as f:
        history = json.load(f)

# ---- KPIs des letzten Laufs
n = len(df)
halluz = int(df["flags"].apply(lambda f: f.count("halluzination_blockiert")).sum())
prev = history[-2]["pass_rate"] if len(history) >= 2 else None
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Pass-Rate", f"{df['bestanden'].mean():.0%}",
          None if prev is None else f"{(df['bestanden'].mean() - prev) * 100:+.0f} pp")
c2.metric("Gespräche", n)
c3.metric("Ø Turns", f"{df['turns'].mean():.1f}")
c4.metric("Ø Latenz", f"{df['latenz_avg'].mean():.1f} s")
c5.metric("Halluzinationen blockiert", halluz)

# ---- Iterationsverlauf
if len(history) >= 1:
    st.subheader("Verlauf über Iterationen")
    h = pd.DataFrame(history)
    h["Pass-Rate %"] = (h["pass_rate"] * 100).round(0)
    st.line_chart(h.set_index("label")[["Pass-Rate %"]])
    st.dataframe(h[["zeit", "label", "gespraeche", "Pass-Rate %", "halluzinationen", "tool_fehler"]],
                 hide_index=True, width="stretch")

# ---- Pro Persona
st.subheader("Ergebnis pro Persona")
per = (df.groupby("persona")
         .agg(laeufe=("bestanden", "size"), pass_rate=("bestanden", "mean"), turns=("turns", "mean"))
         .sort_values("pass_rate"))
per["pass_rate"] = (per["pass_rate"] * 100).round(0)
col_a, col_b = st.columns([2, 1])
col_a.bar_chart(per[["pass_rate"]])
col_b.dataframe(per, width="stretch")

# ---- Ergebnisverteilung
st.subheader("Gesprächsergebnisse")
st.bar_chart(df["ergebnis"].value_counts())

# ---- Fehlschläge mit Transkript
fails = df[~df["bestanden"]]
st.subheader(f"Fehlschläge ({len(fails)})")
if fails.empty:
    st.success("Alle Tests bestanden.")
for _, r in fails.iterrows():
    with st.expander(f"❌ {r['persona']} · Lauf {r['run']} · {'; '.join(r['fehler'])}"):
        for m in r["transkript"]:
            if not m["content"] or m["content"].startswith("SYSTEM-KORREKTUR"):
                continue
            who = "🧑 Kunde" if m["role"] == "user" else ("🤖 Lena" if m["role"] == "assistant" else "🔧 Tool")
            if m["role"] == "tool":
                st.code(m["content"], language="json")
            else:
                st.markdown(f"**{who}:** {m['content']}")
