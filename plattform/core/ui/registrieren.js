/* Selbstregistrierung (/registrieren): sendet das Formular als JSON an /registrierung; kein Inline-Skript wegen CSP script-src 'self'. */
const OIDC = document.currentScript.dataset.oidc === "1";
document.getElementById("f").addEventListener("submit", async (ev) => {
  ev.preventDefault(); const f = ev.target, fehler = document.getElementById("fehler"); fehler.textContent = "";
  const daten = Object.fromEntries(new FormData(f).entries()); daten.datenschutz = f.datenschutz.checked;
  f.querySelector("button").disabled = true;
  try {
    const r = await fetch("/registrierung", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(daten) });
    const a = await r.json(); if (!r.ok) throw new Error(a.detail || "Registrierung fehlgeschlagen");
    f.className = "karte erfolg"; f.innerHTML = `<h2>Willkommen, ${a.name}!</h2><p>Ihre Testphase läuft bis <strong>${a.testphase_bis}</strong> mit allen Funktionen.</p>
      ${OIDC ? "<p>Der Anbieter richtet jetzt Ihren Zugang ein und meldet sich per E-Mail.</p>" : `<p>Ihre Mandantenkennung: <code>${a.mandant_id}</code> (bitte notieren).</p><p><a href="${a.app_url}">Zur Oberfläche: Einrichtung in wenigen Schritten abschließen →</a></p>`}`;
    try { localStorage.setItem("mandant", a.mandant_id); localStorage.setItem("mandantName", a.name); } catch {}
  } catch (e) { fehler.textContent = e.message; f.querySelector("button").disabled = false; }
});
