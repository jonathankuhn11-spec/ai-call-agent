// Online-Terminbuchung: eigenständige Seite ohne Build-Schritt; CSP erlaubt nur externe Skripte derselben Herkunft.
(function () {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const EURO = new Intl.NumberFormat("de-DE", { style: "currency", currency: "EUR" });
  const TAG = new Intl.DateTimeFormat("de-DE", { weekday: "long", day: "numeric", month: "long" });
  const token = location.hash.slice(1);
  const main = document.getElementById("inhalt");
  const basis = `/buchung/${encodeURIComponent(token)}`;
  const zustand = { angebot: null, leistung: null, ressource: "", zeit: null };
  if (!token) { main.innerHTML = `<div class="karte"><h2>Link unvollständig</h2><p>Bitte öffnen Sie den Buchungslink des Betriebs.</p></div>`; return; }

  async function laden() {
    const r = await fetch(basis);
    if (!r.ok) { main.innerHTML = `<div class="karte"><h2>Buchung nicht verfügbar</h2><p>Der Link ist ungültig oder die Online-Buchung ist abgeschaltet. Bitte rufen Sie uns an.</p></div>`; return; }
    zustand.angebot = await r.json();
    zeichnen();
  }

  function zeichnen() {
    const a = zustand.angebot, b = a.betrieb;
    main.innerHTML = `<header><h1>${esc(b.name)}</h1><p>Termin online buchen${b.telefon ? ` · oder telefonisch <a href="tel:${esc(b.telefon)}">${esc(b.telefon)}</a>` : ""}</p></header>
      <div class="karte"><div class="schritt">1. Leistung</div>
        ${a.leistungen.length ? a.leistungen.map((l) => `<button class="leistung${zustand.leistung && zustand.leistung.id === l.id ? " aktiv" : ""}" data-leistung="${esc(l.id)}">
          <span>${esc(l.bezeichnung)}<br><small>ca. ${esc(l.dauer_minuten)} Min.</small></span><strong>${esc(EURO.format(Number(l.preis_brutto)))}</strong></button>`).join("")
          : `<p class="info">Derzeit sind keine Leistungen online buchbar.</p>`}
        ${a.mitarbeiter.length > 1 ? `<label for="ressource">${esc(a.branche.ressource)} (optional)</label><select id="ressource"><option value="">Egal</option>${a.mitarbeiter.map((m) => `<option value="${esc(m.id)}"${zustand.ressource === m.id ? " selected" : ""}>${esc(m.name)}</option>`).join("")}</select>` : ""}
      </div>
      <div class="karte" id="zeiten-karte"${zustand.leistung ? "" : " hidden"}><div class="schritt">2. Zeit</div><div id="zeiten"><p>Freie Zeiten werden geladen …</p></div></div>
      <div class="karte" id="daten-karte"${zustand.zeit ? "" : " hidden"}><div class="schritt">3. Ihre Daten</div>
        <form id="formular" autocomplete="on">
          <label for="name">Name</label><input id="name" name="name" required maxlength="200">
          <label for="telefon">Telefon</label><input id="telefon" name="telefon" type="tel" autocomplete="tel">
          <label for="email">E-Mail</label><input id="email" name="email" type="email" autocomplete="email">
          ${a.branche.adresse_noetig ? `<label for="strasse">Straße und Hausnummer (Einsatzort)</label><input id="strasse" name="strasse" required>
          <div class="zeile"><div><label for="plz">PLZ</label><input id="plz" name="plz" required maxlength="5"></div><div><label for="ort">Ort</label><input id="ort" name="ort" required></div></div>` : ""}
          <label for="hinweis">Hinweis (optional)</label><textarea id="hinweis" name="hinweis" rows="2" maxlength="500"></textarea>
          <div class="honig" aria-hidden="true"><label for="website">Website</label><input id="website" name="website" tabindex="-1" autocomplete="off"></div>
          <div class="kreuz"><input type="checkbox" id="einwilligung_email"><label for="einwilligung_email">Ich möchte Terminbestätigung und Erinnerungen per E-Mail erhalten (jederzeit widerrufbar).</label></div>
          ${a.branche.hinweise.length ? `<div class="info" style="margin-top:10px">${a.branche.hinweise.map(esc).join("<br>")}</div>` : ""}
          <button class="haupt" type="submit" id="senden">Termin buchen</button>
          <div id="meldung"></div>
        </form>
      </div>
      <footer>${b.impressum_url ? `<a href="${esc(b.impressum_url)}">Impressum</a> · ` : ""}${b.datenschutz_url ? `<a href="${esc(b.datenschutz_url)}">Datenschutz</a> · ` : ""}Ihre Angaben dienen nur der Terminvereinbarung mit ${esc(b.name)}.</footer>`;
    main.querySelectorAll("[data-leistung]").forEach((k) => k.addEventListener("click", () => {
      zustand.leistung = a.leistungen.find((l) => l.id === k.dataset.leistung); zustand.zeit = null; zeichnen(); zeitenLaden();
    }));
    const res = document.getElementById("ressource");
    if (res) res.addEventListener("change", () => { zustand.ressource = res.value; zustand.zeit = null; if (zustand.leistung) zeitenLaden(); });
    document.getElementById("formular").addEventListener("submit", senden);
    if (zustand.leistung) zeitenLaden();
  }

  async function zeitenLaden() {
    const ziel = document.getElementById("zeiten");
    const u = `${basis}/zeiten?leistung_id=${encodeURIComponent(zustand.leistung.id)}${zustand.ressource ? `&ressource_id=${encodeURIComponent(zustand.ressource)}` : ""}`;
    const r = await fetch(u);
    if (!r.ok) { ziel.innerHTML = `<div class="fehler">Freie Zeiten konnten nicht geladen werden.</div>`; return; }
    const d = await r.json();
    if (!d.tage.length) { ziel.innerHTML = `<p class="info">Im Buchungszeitraum ist leider nichts frei. Bitte rufen Sie uns an.</p>`; return; }
    ziel.innerHTML = d.tage.map((t) => `<div class="tag"><h3>${esc(TAG.format(new Date(t.tag + "T12:00:00")))}</h3><div class="zeiten">${t.zeiten.map((z) =>
      `<button type="button" class="zeit${zustand.zeit && zustand.zeit.beginn === z.beginn ? " aktiv" : ""}" data-beginn="${esc(z.beginn)}" data-ressource="${esc(z.ressource_id)}" title="${esc(z.name)}">${esc(z.uhrzeit)}</button>`).join("")}</div></div>`).join("");
    ziel.querySelectorAll("[data-beginn]").forEach((k) => k.addEventListener("click", () => {
      zustand.zeit = { beginn: k.dataset.beginn, ressource_id: k.dataset.ressource };
      ziel.querySelectorAll(".zeit").forEach((x) => x.classList.toggle("aktiv", x === k));
      document.getElementById("daten-karte").hidden = false;
      document.getElementById("name").focus();
    }));
  }

  async function senden(ev) {
    ev.preventDefault();
    const m = document.getElementById("meldung"), knopf = document.getElementById("senden");
    const wert = (id) => { const e = document.getElementById(id); return e ? e.value.trim() || null : null; };
    if (!zustand.zeit) { m.innerHTML = `<div class="fehler">Bitte zuerst eine Zeit wählen.</div>`; return; }
    if (!wert("telefon") && !wert("email")) { m.innerHTML = `<div class="fehler">Bitte Telefon oder E-Mail angeben.</div>`; return; }
    knopf.disabled = true;
    const daten = { leistung_id: zustand.leistung.id, ressource_id: zustand.zeit.ressource_id, beginn: zustand.zeit.beginn, name: wert("name"), telefon: wert("telefon"),
      email: wert("email"), strasse: wert("strasse"), plz: wert("plz"), ort: wert("ort"), hinweis: wert("hinweis"),
      einwilligung_email: document.getElementById("einwilligung_email").checked, website: wert("website") || "" };
    const r = await fetch(basis, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(daten) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { knopf.disabled = false; m.innerHTML = `<div class="fehler">${esc(typeof d.detail === "string" ? d.detail : "Die Buchung hat nicht geklappt. Bitte rufen Sie uns an.")}</div>`; return; }
    const wann = new Intl.DateTimeFormat("de-DE", { dateStyle: "full", timeStyle: "short" }).format(new Date(d.beginn));
    main.innerHTML = `<header><h1>${esc(zustand.angebot.betrieb.name)}</h1></header><div class="karte"><h2>Vielen Dank!</h2><p>${esc(zustand.leistung.bezeichnung)} am ${esc(wann)} Uhr.</p>
      <div class="hinweis">${esc(d.hinweis)}</div><p style="margin-top:12px">Über <a href="${esc(d.portal)}">Ihre Terminseite</a> können Sie den Termin einsehen oder absagen.</p></div>`;
  }
  laden();
})();
