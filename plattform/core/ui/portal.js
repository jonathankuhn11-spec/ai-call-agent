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
  let sterneWahl = 0;

  function verlauf(d) {
    return `<div class="karte"><h2>Wo steht Ihr Auftrag?</h2><ul class="verlauf">${d.verlauf.map((s) => `<li class="${esc(s.zustand)}"><span class="punkt"></span><span class="titel">${esc(s.titel)}</span>${s.hinweis ? `<small>${esc(s.hinweis)}</small>` : ""}</li>`).join("")}</ul></div>`;
  }
  function termine(d) {
    return `<div class="karte" id="termine"><h2>Termine</h2>${d.termine.length ? d.termine.map((t) => `<div class="termin" data-termin="${esc(t.id)}"><div><strong>${esc(t.text)}</strong><br><small>Monteur: ${esc(t.monteur)}</small><br><a href="${basis}/termine/${esc(t.id)}.ics${q}">In meinen Kalender übernehmen</a></div>
        <div class="aktionen"><span class="status ${esc(t.status)}">${t.status === "bestaetigt" ? "bestätigt" : "vorgeschlagen"}</span>
          ${t.status === "vorgeschlagen" ? `<button data-aktion="bestaetigen">Termin bestätigen</button>` : ""}<button class="leise" data-aktion="absagen">Absagen</button></div></div>`).join("")
        : `<p>Derzeit ist kein Termin geplant.${d.alternativen_moeglich ? " Wählen Sie unten eine freie Zeit, die Ihnen passt." : " Wir melden uns bei Ihnen."}</p>`}
        ${d.alternativen_moeglich ? `<div id="alternativen"><button class="leise" id="alternativen-zeigen">${d.termine.length ? "Anderen Termin wählen" : "Freie Zeiten anzeigen"}</button><div id="fenster" class="fenster" hidden></div></div>` : ""}
        <div id="absage" hidden><textarea id="grund" rows="2" placeholder="Grund (optional), z. B. Terminwunsch"></textarea><div class="aktionen"><button id="absage-ok">Termin verbindlich absagen</button><button class="leise" id="absage-nein">Abbrechen</button></div></div>
        <div id="meldung"></div></div>`;
  }
  function rechnungen(d) {
    if (!d.rechnungen.length) return "";
    return `<div class="karte" id="rechnungen"><h2>Rechnungen</h2><table>${d.rechnungen.map((r) => `<tr><td>${esc(r.nummer)}<br><small>${r.status === "bezahlt" ? "bezahlt, vielen Dank" : `fällig ${esc(DATUM.format(new Date(r.faellig)))}`}</small>
        ${r.girocode ? `<div class="girocode"><img src="${basis}/rechnungen/${esc(r.id)}/girocode.png${q}" alt="GiroCode zur Überweisung"><div><strong>Bequem bezahlen:</strong><br><small>QR-Code mit der Banking-App scannen, die Überweisung ist ausgefüllt.${d.zahlung ? `<br>IBAN ${esc(d.zahlung.iban)}` : ""}</small></div></div>` : ""}</td>
        <td>${esc(EURO.format(r.brutto))}<br><div class="aktionen" style="justify-content:flex-end">${r.online_zahlung ? `<button data-bezahlen="${esc(r.id)}">Online bezahlen</button>` : ""}<a class="knopf leise" href="${basis}/rechnungen/${esc(r.id)}.pdf${q}" target="_blank" rel="noopener">PDF</a></div></td></tr>`).join("")}</table><div id="zahlung-meldung"></div></div>`;
  }
  function bewertung(d) {
    if (!d.bewertung_moeglich) return "";
    if (d.bewertung) return `<div class="karte"><h2>Ihre Bewertung</h2><p>${"★".repeat(d.bewertung.sterne)}${"☆".repeat(5 - d.bewertung.sterne)}${d.bewertung.text ? ` – ${esc(d.bewertung.text)}` : ""}</p><p><small>Vielen Dank!</small></p></div>`;
    return `<div class="karte" id="bewertung"><h2>Wie zufrieden waren Sie?</h2><div class="sterne" id="sterne">${[1, 2, 3, 4, 5].map((n) => `<button type="button" data-stern="${n}" aria-label="${n} von 5 Sternen">★</button>`).join("")}</div>
      <textarea id="bewertung-text" rows="2" placeholder="Was lief gut, was können wir besser machen? (optional)"></textarea><div class="aktionen"><button id="bewertung-senden" disabled>Bewertung senden</button></div><div id="bewertung-meldung"></div></div>`;
  }
  function empfehlungen(d) {
    if (!d.empfehlungen || !d.empfehlungen.length) return "";
    return `<div class="karte"><h2>Für Sie empfohlen</h2><p><small>Passend zu Ihren Anlagen und zur Jahreszeit. Unverbindlich: Sie erhalten erst ein Angebot, das Sie in Ruhe prüfen.</small></p>
      ${d.empfehlungen.map((e) => `<div class="termin" data-empfehlung="${esc(e.schluessel)}"><div><strong>${esc(e.titel)}</strong><br><small>${esc(e.text)}</small></div>
        <div class="aktionen">${e.preis_brutto ? `<span class="status">${esc(EURO.format(Number(e.preis_brutto)))}</span>` : ""}<button class="leise" data-anfordern="${esc(e.schluessel)}">${e.aktion === "angebot" ? "Angebot anfordern" : "Beratung anfragen"}</button></div></div>`).join("")}
      <div id="empfehlung-meldung"></div></div>`;
  }
  function haus(d) {
    if (!d.haus || !d.haus.anlagen.length) return "";
    const nurFahrzeuge = d.haus.anlagen.every((a) => a.typ === "Fahrzeug");
    return `<div class="karte"><h2>${nurFahrzeuge ? (d.haus.anlagen.length > 1 ? "Ihre Fahrzeuge" : "Ihr Fahrzeug") : "Ihre Anlagen"}</h2><p>${d.haus.anlagen.map((a) => `${nurFahrzeuge ? "" : esc(a.typ.replace(/_/g, " ")) + " "}${a.hersteller ? `${esc(a.hersteller)} ` : ""}${a.modell ? `${esc(a.modell)} ` : ""}${a.merkmale && a.merkmale.kennzeichen ? `(${esc(a.merkmale.kennzeichen)}) ` : ""}${a.baujahr ? `(Bj. ${a.baujahr})` : ""}${a.merkmale && a.merkmale.hu_bis ? ` · HU bis ${esc(DATUM.format(new Date(a.merkmale.hu_bis)))}` : ""}`.trim()).join(", ")}</p>
        ${d.haus.ueberfaellig.length ? `<p><strong>Überfällig:</strong> ${d.haus.ueberfaellig.map((e) => esc(e.bezeichnung)).join(", ")}</p>` : ""}
        ${d.haus.anstehend.length ? `<table>${d.haus.anstehend.map((j) => `<tr><td><strong>${j.jahr}</strong></td><td>${j.eintraege.map((e) => `${esc(e.bezeichnung)}${e.pflicht ? " (Prüfung)" : ""}${e.kosten ? ` – ca. ${esc(EURO.format(e.kosten))}` : ""}`).join("<br>")}</td></tr>`).join("")}</table><p><small>Nutzungsdauern und Prüfintervalle sind Richtwerte; wir beraten Sie gern.</small></p>` : ""}
        <h3>Zählerstand melden</h3><form id="zaehler" class="aktionen"><select name="art">${Object.entries(d.haus.zaehlerarten).map(([k, a]) => `<option value="${k}">${esc(a.bezeichnung)} (${esc(a.einheit)})</option>`).join("")}</select>
          ${d.haus.anlagen.length > 1 ? `<select name="anlage_id">${d.haus.anlagen.map((a) => `<option value="${esc(a.id)}">${esc(a.typ.replace(/_/g, " "))}${a.modell ? ` ${esc(a.modell)}` : ""}</option>`).join("")}</select>` : `<input type="hidden" name="anlage_id" value="${esc(d.haus.anlagen[0].id)}">`}
          <input name="wert" type="number" step="0.1" min="0" placeholder="Stand" required><button type="submit">Senden</button></form>
        ${d.haus.letzte_staende.length ? `<p><small>Zuletzt: ${d.haus.letzte_staende.map((z) => `${esc(z.bezeichnung)} ${esc(z.wert)} ${esc(z.einheit)} (${esc(DATUM.format(new Date(z.datum)))})`).join(" · ")}</small></p>` : ""}<div id="zaehler-meldung"></div></div>`;
  }

  async function laden() {
    const r = await fetch(basis + q);
    if (!r.ok) { main.innerHTML = `<div class="karte"><h2>Vorgang nicht gefunden</h2><p>Der Link ist ungültig oder abgelaufen. Bitte rufen Sie uns an.</p></div>`; return; }
    const d = await r.json();
    const b = d.betrieb;
    if (b.farbe) document.documentElement.style.setProperty("--akzent", b.farbe);
    const bezahlt = new URLSearchParams(location.search).get("bezahlt") === "1";
    const ns = d.naechster_schritt;
    main.innerHTML = `<header><h1>${esc(b.name)}</h1><p>Guten Tag ${esc(d.kunde.name)}, hier sehen Sie Ihren Vorgang „${esc(d.vorgang.anliegen)}“.</p></header>
      ${bezahlt ? `<div class="hinweis" role="status">Vielen Dank, Ihre Zahlung ist unterwegs. Sobald sie bei uns gebucht ist, zeigt die Rechnung „bezahlt“.</div>` : ""}
      ${ns ? `<div class="karte naechster ${esc(ns.art)}"><div class="etikett">Als Nächstes</div><h2>${esc(ns.titel)}</h2><p>${esc(ns.text)}</p>${ns.aktion ? `<button class="leise" data-springen="${esc(ns.aktion)}">${ns.art === "termin" ? "Zum Termin" : ns.art === "angebot" ? "Angebot ansehen" : ns.art === "rechnung" ? "Zur Rechnung" : "Jetzt bewerten"} ↓</button>` : ""}</div>` : ""}
      ${verlauf(d)}${termine(d)}
      ${d.angebote.length ? `<div class="karte" id="angebote"><h2>Angebote</h2><table>${d.angebote.map((a) => `<tr><td>${esc(a.nummer)} · ${esc(a.titel)}<br><small>gültig bis ${esc(DATUM.format(new Date(a.gueltig_bis)))}${a.status === "angenommen" ? " · angenommen, vielen Dank" : a.status === "versendet" ? " · wartet auf Ihre Entscheidung" : ""}</small></td><td>${esc(EURO.format(a.brutto))}<br><div class="aktionen"><a class="knopf leise" href="${basis}/angebote/${esc(a.id)}.pdf${q}" target="_blank" rel="noopener">PDF</a>${a.status === "versendet" ? `<button data-annehmen="${esc(a.id)}">Angebot annehmen</button>` : ""}</div></td></tr>`).join("")}</table><div id="angebot-meldung"></div></div>` : ""}
      ${empfehlungen(d)}
      ${rechnungen(d)}${bewertung(d)}${haus(d)}
      <div class="karte" id="kontakt"><h2>Kontakt</h2><p>${esc(b.name)}${b.strasse ? `, ${esc(b.strasse)}, ${esc(b.plz || "")} ${esc(b.ort || "")}` : ""}</p><div class="aktionen">${b.telefon ? `<a class="knopf" href="tel:${esc(b.telefon)}">Anrufen</a>` : ""}${b.email ? `<a class="knopf leise" href="mailto:${esc(b.email)}">E-Mail schreiben</a>` : ""}</div></div>
      <nav class="leiste" aria-label="Schnellkontakt">${b.telefon ? `<a href="tel:${esc(b.telefon)}">📞 Anrufen</a>` : ""}${b.whatsapp ? `<a href="${esc(b.whatsapp)}" rel="noopener" target="_blank">💬 WhatsApp</a>` : ""}${b.email ? `<a href="mailto:${esc(b.email)}">✉️ E-Mail</a>` : ""}</nav>
      <footer>${b.impressum_url ? `<a href="${esc(b.impressum_url)}">Impressum</a> · ` : ""}${b.datenschutz_url ? `<a href="${esc(b.datenschutz_url)}">Datenschutz</a> · ` : ""}Diese Seite zeigt nur diesen Vorgang. Geben Sie den Link nicht weiter.</footer>`;
    main.querySelectorAll("[data-springen]").forEach((k) => k.addEventListener("click", () => document.getElementById(k.dataset.springen)?.scrollIntoView({ behavior: "smooth", block: "start" })));
    main.querySelectorAll("[data-bezahlen]").forEach((k) => k.addEventListener("click", async () => {
      const m = document.getElementById("zahlung-meldung"); k.disabled = true; k.textContent = "Einen Moment …";
      const r2 = await fetch(`${basis}/rechnungen/${encodeURIComponent(k.dataset.bezahlen)}/zahlung${q}`, { method: "POST" });
      const d2 = await r2.json().catch(() => ({}));
      if (!r2.ok || !d2.url) { m.innerHTML = `<div class="fehler">${esc(d2.detail || "Die Online-Zahlung ist gerade nicht möglich. Bitte überweisen Sie per QR-Code oder rufen Sie uns an.")}</div>`; k.disabled = false; k.textContent = "Online bezahlen"; return; }
      location.href = d2.url;
    }));
    let aktiverTermin = null;
    main.querySelectorAll("[data-aktion]").forEach((k) => k.addEventListener("click", async () => {
      const terminId = k.closest("[data-termin]").dataset.termin;
      if (k.dataset.aktion === "absagen") { aktiverTermin = terminId; document.getElementById("absage").hidden = false; return; }
      await senden(terminId, "bestaetigen");
    }));
    document.getElementById("absage-nein").addEventListener("click", () => { document.getElementById("absage").hidden = true; });
    document.getElementById("absage-ok").addEventListener("click", () => senden(aktiverTermin, "absagen", document.getElementById("grund").value));
    document.getElementById("alternativen-zeigen")?.addEventListener("click", async (ev) => {
      const box = document.getElementById("fenster"); ev.target.disabled = true;
      const fenster = await (await fetch(`${basis}/alternativen${q}`)).json();
      box.hidden = false;
      box.innerHTML = fenster.length ? fenster.map((f) => `<button type="button" data-ressource="${esc(f.ressource_id)}" data-beginn="${esc(f.beginn)}" data-sachmittel="${esc(f.sachmittel_id || "")}"><span>${esc(f.text)}</span><span>wählen ›</span></button>`).join("")
        : `<p>In den nächsten Tagen ist leider nichts frei. Bitte rufen Sie uns an.</p>`;
      box.querySelectorAll("button").forEach((k) => k.addEventListener("click", async () => {
        const m = document.getElementById("meldung");
        const r2 = await fetch(`${basis}/alternativen${q}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ressource_id: k.dataset.ressource, beginn: k.dataset.beginn, sachmittel_id: k.dataset.sachmittel || null }) });
        const d2 = await r2.json().catch(() => ({}));
        if (!r2.ok) { m.innerHTML = `<div class="fehler">${esc(d2.detail || "Das Fenster ist inzwischen belegt. Bitte ein anderes wählen.")}</div>`; return; }
        await laden(); document.getElementById("meldung").innerHTML = `<div class="hinweis">${esc(d2.hinweis)}</div>`;
      }));
    });
    main.querySelectorAll("[data-anfordern]").forEach((k) => k.addEventListener("click", async () => {
      const m = document.getElementById("empfehlung-meldung"); k.disabled = true;
      const r2 = await fetch(`${basis}/empfehlungen/${encodeURIComponent(k.dataset.anfordern)}/anfordern${q}`, { method: "POST" });
      const d2 = await r2.json().catch(() => ({}));
      if (!r2.ok) { m.innerHTML = `<div class="fehler">${esc(d2.detail || "Das hat nicht geklappt. Bitte rufen Sie uns an.")}</div>`; k.disabled = false; return; }
      await laden(); document.getElementById("empfehlung-meldung")?.insertAdjacentHTML("beforeend", `<div class="hinweis">${esc(d2.hinweis)}</div>`);
      if (!document.getElementById("empfehlung-meldung")) document.getElementById("angebot-meldung")?.insertAdjacentHTML("beforeend", `<div class="hinweis">${esc(d2.hinweis)}</div>`);
    }));
    main.querySelectorAll("[data-annehmen]").forEach((k) => k.addEventListener("click", async () => {
      if (!confirm("Angebot verbindlich annehmen? Damit erteilen Sie den Auftrag.")) return;
      const m = document.getElementById("angebot-meldung"); k.disabled = true;
      const r2 = await fetch(`${basis}/angebote/${encodeURIComponent(k.dataset.annehmen)}/annehmen${q}`, { method: "POST" });
      const d2 = await r2.json().catch(() => ({}));
      if (!r2.ok) { m.innerHTML = `<div class="fehler">${esc(d2.detail || "Das hat nicht geklappt. Bitte rufen Sie uns an.")}</div>`; k.disabled = false; return; }
      await laden(); document.getElementById("angebot-meldung")?.insertAdjacentHTML("beforeend", `<div class="hinweis">${esc(d2.hinweis)}</div>`);
    }));
    document.querySelectorAll("#sterne button").forEach((k) => k.addEventListener("click", () => {
      sterneWahl = Number(k.dataset.stern);
      document.querySelectorAll("#sterne button").forEach((x) => x.classList.toggle("an", Number(x.dataset.stern) <= sterneWahl));
      document.getElementById("bewertung-senden").disabled = false;
    }));
    document.getElementById("bewertung-senden")?.addEventListener("click", async () => {
      const m = document.getElementById("bewertung-meldung");
      const r2 = await fetch(`${basis}/bewertung${q}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sterne: sterneWahl, text: document.getElementById("bewertung-text").value || null }) });
      const d2 = await r2.json().catch(() => ({}));
      if (!r2.ok) { m.innerHTML = `<div class="fehler">${esc(d2.detail || "Das hat nicht geklappt.")}</div>`; return; }
      await laden();
    });
    document.getElementById("zaehler")?.addEventListener("submit", async (ev) => {
      ev.preventDefault(); const fd = new FormData(ev.target); const m = document.getElementById("zaehler-meldung");
      const r2 = await fetch(`${basis}/zaehlerstand${q}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ art: fd.get("art"), wert: Number(fd.get("wert")), anlage_id: fd.get("anlage_id") || null }) });
      const d2 = await r2.json().catch(() => ({}));
      if (!r2.ok) { m.innerHTML = `<div class="fehler">${esc(d2.detail || "Das hat nicht geklappt.")}</div>`; return; }
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
