// Kundenportal: eigenständige Seite ohne Build-Schritt; CSP erlaubt nur externe Skripte derselben Herkunft.
(function () {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const EURO = new Intl.NumberFormat("de-DE", { style: "currency", currency: "EUR" });
  const DATUM = new Intl.DateTimeFormat("de-DE", { dateStyle: "medium" });
  const [fallId, token] = location.hash.slice(1).split("/");
  const main = document.getElementById("inhalt");
  const basis = `/portal/faelle/${encodeURIComponent(fallId || "")}`;
  const q = `?token=${encodeURIComponent(token || "")}`;
  if (!fallId || !token) { main.innerHTML = `<div class="karte"><h2>Link unvollständig</h2><p>Bitte öffnen Sie den Link aus Ihrer Terminbestätigung.</p></div>`; return; }
  async function laden() {
    const r = await fetch(basis + q);
    if (!r.ok) { main.innerHTML = `<div class="karte"><h2>Vorgang nicht gefunden</h2><p>Der Link ist ungültig oder abgelaufen. Bitte rufen Sie uns an.</p></div>`; return; }
    const d = await r.json();
    const b = d.betrieb;
    main.innerHTML = `<header><h1>${esc(b.name)}</h1><p>Guten Tag ${esc(d.kunde.name)}, hier sehen Sie Ihren Vorgang „${esc(d.vorgang.anliegen)}“.</p></header>
      <div class="karte"><h2>Termine</h2>${d.termine.length ? d.termine.map((t) => `<div class="termin" data-termin="${esc(t.id)}"><div><strong>${esc(t.text)}</strong><br><small>Monteur: ${esc(t.monteur)}</small></div>
        <div class="aktionen"><span class="status ${esc(t.status)}">${t.status === "bestaetigt" ? "bestätigt" : "vorgeschlagen"}</span>
          ${t.status === "vorgeschlagen" ? `<button data-aktion="bestaetigen">Termin bestätigen</button>` : ""}<button class="leise" data-aktion="absagen">Absagen</button></div></div>`).join("")
        : `<p>Derzeit ist kein Termin geplant. Wir melden uns bei Ihnen.</p>`}
        <div id="absage" hidden><textarea id="grund" rows="2" placeholder="Grund (optional), z. B. Terminwunsch"></textarea><div class="aktionen"><button id="absage-ok">Termin verbindlich absagen</button><button class="leise" id="absage-nein">Abbrechen</button></div></div>
        <div id="meldung"></div></div>
      ${d.angebote.length ? `<div class="karte"><h2>Angebote</h2><table>${d.angebote.map((a) => `<tr><td>${esc(a.nummer)} · ${esc(a.titel)}<br><small>gültig bis ${esc(DATUM.format(new Date(a.gueltig_bis)))}${a.status === "angenommen" ? " · angenommen" : ""}</small></td><td>${esc(EURO.format(a.brutto))}<br><a href="${basis}/angebote/${esc(a.id)}.pdf${q}" target="_blank" rel="noopener">PDF</a></td></tr>`).join("")}</table></div>` : ""}
      ${d.rechnungen.length ? `<div class="karte"><h2>Rechnungen</h2><table>${d.rechnungen.map((r) => `<tr><td>${esc(r.nummer)}<br><small>${r.status === "bezahlt" ? "bezahlt" : `fällig ${esc(DATUM.format(new Date(r.faellig)))}`}</small></td><td>${esc(EURO.format(r.brutto))}<br><a href="${basis}/rechnungen/${esc(r.id)}.pdf${q}" target="_blank" rel="noopener">PDF</a></td></tr>`).join("")}</table></div>` : ""}
      ${d.haus && d.haus.anlagen.length ? `<div class="karte"><h2>Ihr Haus</h2><p>${d.haus.anlagen.map((a) => `${esc(a.typ.replace(/_/g, " "))}${a.hersteller ? ` ${esc(a.hersteller)}` : ""}${a.baujahr ? ` (Bj. ${a.baujahr})` : ""}`).join(", ")}</p>
        ${d.haus.ueberfaellig.length ? `<p><strong>Überfällig:</strong> ${d.haus.ueberfaellig.map((e) => esc(e.bezeichnung)).join(", ")}</p>` : ""}
        ${d.haus.anstehend.length ? `<table>${d.haus.anstehend.map((j) => `<tr><td><strong>${j.jahr}</strong></td><td>${j.eintraege.map((e) => `${esc(e.bezeichnung)}${e.pflicht ? " (Prüfung)" : ""}${e.kosten ? ` – ca. ${esc(EURO.format(e.kosten))}` : ""}`).join("<br>")}</td></tr>`).join("")}</table><p><small>Nutzungsdauern und Prüfintervalle sind Richtwerte; wir beraten Sie gern.</small></p>` : ""}
        <h3>Zählerstand melden</h3><form id="zaehler" class="aktionen"><select name="art">${Object.entries(d.haus.zaehlerarten).map(([k, a]) => `<option value="${k}">${esc(a.bezeichnung)} (${esc(a.einheit)})</option>`).join("")}</select>
          ${d.haus.anlagen.length > 1 ? `<select name="anlage_id">${d.haus.anlagen.map((a) => `<option value="${esc(a.id)}">${esc(a.typ.replace(/_/g, " "))}</option>`).join("")}</select>` : `<input type="hidden" name="anlage_id" value="${esc(d.haus.anlagen[0].id)}">`}
          <input name="wert" type="number" step="0.1" min="0" placeholder="Zählerstand" required><button type="submit">Senden</button></form>
        ${d.haus.letzte_staende.length ? `<p><small>Zuletzt: ${d.haus.letzte_staende.map((z) => `${esc(z.bezeichnung)} ${esc(z.wert)} ${esc(z.einheit)} (${esc(DATUM.format(new Date(z.datum)))})`).join(" · ")}</small></p>` : ""}<div id="zaehler-meldung"></div></div>` : ""}
      <div class="karte"><h2>Kontakt</h2><p>${esc(b.name)}${b.strasse ? `, ${esc(b.strasse)}, ${esc(b.plz || "")} ${esc(b.ort || "")}` : ""}<br>${b.telefon ? `Telefon <a href="tel:${esc(b.telefon)}">${esc(b.telefon)}</a>` : ""}${b.email ? ` · <a href="mailto:${esc(b.email)}">${esc(b.email)}</a>` : ""}</p></div>
      <footer>${b.impressum_url ? `<a href="${esc(b.impressum_url)}">Impressum</a> · ` : ""}${b.datenschutz_url ? `<a href="${esc(b.datenschutz_url)}">Datenschutz</a> · ` : ""}Diese Seite zeigt nur diesen Vorgang. Geben Sie den Link nicht weiter.</footer>`;
    let aktiverTermin = null;
    main.querySelectorAll("[data-aktion]").forEach((k) => k.addEventListener("click", async () => {
      const terminId = k.closest("[data-termin]").dataset.termin;
      if (k.dataset.aktion === "absagen") { aktiverTermin = terminId; document.getElementById("absage").hidden = false; return; }
      await senden(terminId, "bestaetigen");
    }));
    document.getElementById("absage-nein").addEventListener("click", () => { document.getElementById("absage").hidden = true; });
    document.getElementById("absage-ok").addEventListener("click", () => senden(aktiverTermin, "absagen", document.getElementById("grund").value));
    document.getElementById("zaehler")?.addEventListener("submit", async (ev) => {
      ev.preventDefault(); const fd = new FormData(ev.target); const m = document.getElementById("zaehler-meldung");
      const r = await fetch(`${basis}/zaehlerstand${q}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ art: fd.get("art"), wert: Number(fd.get("wert")), anlage_id: fd.get("anlage_id") || null }) });
      const d2 = await r.json().catch(() => ({}));
      if (!r.ok) { m.innerHTML = `<div class="fehler">${esc(d2.detail || "Das hat nicht geklappt.")}</div>`; return; }
      await laden(); document.getElementById("zaehler-meldung").innerHTML = `<div class="hinweis">Vielen Dank, der Zählerstand ist gespeichert.</div>`;
    });
  }
  async function senden(terminId, aktion, grund) {
    const m = document.getElementById("meldung");
    const r = await fetch(`${basis}/termine/${encodeURIComponent(terminId)}${q}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ aktion, grund: grund || null }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { m.innerHTML = `<div class="fehler">${esc(d.detail || "Das hat nicht geklappt. Bitte rufen Sie uns an.")}</div>`; return; }
    await laden();
    document.getElementById("meldung").innerHTML = `<div class="hinweis">${esc(d.hinweis)}</div>`;
  }
  laden().catch(() => { main.innerHTML = `<div class="karte"><h2>Verbindung fehlgeschlagen</h2><p>Bitte versuchen Sie es später erneut.</p></div>`; });
})();
