"use strict";
// Büro-Oberfläche des Core-Service: spricht ausschließlich die REST-API (Bearer-Token oder X-API-Key, X-Mandant).

const STATUS = ["eingegangen", "qualifiziert", "eingeplant", "durchgefuehrt", "abgerechnet", "uebergeben"];
const STATUS_TEXT = { eingegangen: "Eingegangen", qualifiziert: "Qualifiziert", eingeplant: "Eingeplant",
  durchgefuehrt: "Durchgeführt", abgerechnet: "Abgerechnet", uebergeben: "An Kanzlei", storniert: "Storniert" };
const UEBERGAENGE = { eingegangen: ["qualifiziert", "storniert"], qualifiziert: ["eingeplant", "storniert"],
  eingeplant: ["durchgefuehrt", "qualifiziert", "storniert"], durchgefuehrt: ["abgerechnet"], abgerechnet: ["uebergeben"] };
const AKTION_TEXT = { termin_bestaetigen: "Termin bestätigen", nachricht_an_endkunde: "Nachricht an Endkunde",
  rechnung_erstellen: "Rechnung erstellen", auftrag_an_fachsoftware: "Auftrag an Fachsoftware", storno: "Storno",
  belegversand_kanzlei: "Belegversand an Kanzlei" };
const KLASSE_TEXT = { A: "A · reversibel", B: "B · kundenwirksam", C: "C · finanzwirksam" };
// Betriebszeit ist Europe/Berlin, unabhängig von der Zeitzone des Browsers
const TZ = "Europe/Berlin";
const WOCHENTAG = new Intl.DateTimeFormat("de-DE", { weekday: "short", day: "2-digit", month: "2-digit", timeZone: TZ });
const UHRZEIT = new Intl.DateTimeFormat("de-DE", { hour: "2-digit", minute: "2-digit", timeZone: TZ });
const DATUMZEIT = new Intl.DateTimeFormat("de-DE", { dateStyle: "short", timeStyle: "short", timeZone: TZ });
const TAGESSCHLUESSEL = new Intl.DateTimeFormat("sv-SE", { timeZone: TZ });  // liefert JJJJ-MM-TT

const $ = (sel, wurzel = document) => wurzel.querySelector(sel);
const esc = (w) => String(w ?? "").replace(/[&<>"']/g, (z) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[z]);
const tag = (wert, klasse = wert, text = wert) => `<span class="marke-tag t-${esc(klasse)}">${esc(text)}</span>`;
const zeit = (iso) => (iso ? DATUMZEIT.format(new Date(iso)) : "–");
// Rückmeldungen als Toast statt blockierender melden()-Dialoge: art = fehler | ok | info
function melden(text, art = "fehler", dauer = art === "fehler" ? 7000 : 3500) {
  let box = $("#toasts"); if (!box) { box = document.createElement("div"); box.id = "toasts"; box.className = "toasts"; box.setAttribute("aria-live", "polite"); document.body.append(box); }
  const el = document.createElement("div"); el.className = `toast t-${art}`; el.textContent = String(text ?? ""); el.setAttribute("role", art === "fehler" ? "alert" : "status");
  box.append(el); setTimeout(() => { el.classList.add("weg"); setTimeout(() => el.remove(), 300); }, dauer);
}
const leer = (text, aktion) => `<div class="leer">${esc(text)}${aktion ? `<br>${aktion}` : ""}</div>`;

function speicher(schluessel, wert) {
  try { if (wert === undefined) return sessionStorage.getItem(schluessel); sessionStorage.setItem(schluessel, wert); } catch { return null; }
}

function authKopf(mitMandant = true) {
  const kopf = {};
  const token = speicher("token"), key = speicher("apiKey");
  if (token) kopf.Authorization = `Bearer ${token}`; else if (key) kopf["X-API-Key"] = key;
  if (mitMandant) kopf["X-Mandant"] = speicher("mandant") || "";
  return kopf;
}
async function api(pfad, { methode = "GET", daten, mitMandant = true } = {}, erneut = true) {
  const kopf = authKopf(mitMandant);
  const formular = daten instanceof FormData;  // Multipart (Datei-Upload): Content-Type setzt der Browser mit Boundary
  if (!formular) kopf["Content-Type"] = "application/json";
  const token = speicher("token");
  const r = await fetch(pfad, { method: methode, headers: kopf, body: daten === undefined ? undefined : formular ? daten : JSON.stringify(daten) });
  if (r.status === 401 && token && erneut && await tokenErneuern()) return api(pfad, { methode, daten, mitMandant }, false);
  if (r.status === 401 || (mitMandant && r.status === 404 && (await r.clone().text()).includes("Mandant unbekannt"))) {
    anmelden(); throw new Error("Anmeldung erforderlich");
  }
  const text = await r.text();
  const inhalt = r.headers.get("content-type")?.includes("json") ? JSON.parse(text || "null") : text;
  if (!r.ok) throw new Error(typeof inhalt === "object" && inhalt?.detail ? (Array.isArray(inhalt.detail) ? inhalt.detail.map((d) => d.msg).join("; ") : inhalt.detail) : `HTTP ${r.status}`);
  return inhalt;
}

// --- Anmeldung: OIDC (Authorization Code mit PKCE, öffentlicher Client) oder API-Key ---------
// Personen melden sich über Keycloak an; Mandant und Rollen stehen im Token. Der API-Key bleibt
// für Administration und Entwicklung. Tokens liegen nur im Sitzungsspeicher dieses Tabs.
let KONFIG = null, ENDPUNKTE = null;
async function konfiguration() {
  if (!KONFIG) KONFIG = await (await fetch("/auth/konfiguration")).json();
  if (KONFIG.oidc && !ENDPUNKTE) ENDPUNKTE = await (await fetch(`${KONFIG.oidc.issuer}/.well-known/openid-configuration`)).json();
  return KONFIG;
}
const b64url = (puffer) => btoa(String.fromCharCode(...new Uint8Array(puffer))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const zufall = (n) => b64url(crypto.getRandomValues(new Uint8Array(n)));
const ruecksprung = () => location.origin + location.pathname;

async function oidcAnmelden() {
  await konfiguration();
  const verifier = zufall(32), zustand = zufall(16);
  speicher("pkce", JSON.stringify({ verifier, zustand, ziel: location.hash }));
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  const q = new URLSearchParams({ response_type: "code", client_id: KONFIG.oidc.client_id, redirect_uri: ruecksprung(),
    scope: "openid profile", state: zustand, code_challenge: challenge, code_challenge_method: "S256" });
  location.assign(`${ENDPUNKTE.authorization_endpoint}?${q}`);
}
async function tokenHolen(parameter) {
  await konfiguration();
  const r = await fetch(ENDPUNKTE.token_endpoint, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: KONFIG.oidc.client_id, ...parameter }) });
  if (!r.ok) throw new Error(`Anmeldung fehlgeschlagen (HTTP ${r.status})`);
  const t = await r.json();
  speicher("token", t.access_token); speicher("refresh", t.refresh_token || ""); speicher("idToken", t.id_token || "");
}
async function tokenErneuern() {
  if (!speicher("refresh")) return false;
  try { await tokenHolen({ grant_type: "refresh_token", refresh_token: speicher("refresh") }); return true; } catch { return false; }
}
async function oidcRueckkehr() {
  const q = new URLSearchParams(location.search);
  if (!q.has("code") && !q.has("error")) return;
  const pkce = JSON.parse(speicher("pkce") || "{}");
  speicher("pkce", "");
  history.replaceState(null, "", location.pathname + (pkce.ziel || ""));
  if (q.has("error")) throw new Error(`Anmeldung abgebrochen: ${q.get("error_description") || q.get("error")}`);
  if (!pkce.zustand || q.get("state") !== pkce.zustand) throw new Error("Anmeldung abgewiesen: state stimmt nicht überein");
  await tokenHolen({ grant_type: "authorization_code", code: q.get("code"), redirect_uri: ruecksprung(), code_verifier: pkce.verifier });
  ICH = null;
  const wir = await ich();
  const betrieb = (await api("/mandanten", { mitMandant: false }))[0];
  speicher("mandant", wir.mandant); speicher("mandantName", `${betrieb?.name || wir.mandant} · ${wir.name}`);
}
function abmelden() {
  const idToken = speicher("idToken");
  ["token", "refresh", "idToken", "mandant", "mandantName"].forEach((k) => speicher(k, "")); ICH = null;
  if (idToken && ENDPUNKTE?.end_session_endpoint) {
    location.assign(`${ENDPUNKTE.end_session_endpoint}?${new URLSearchParams({ id_token_hint: idToken, post_logout_redirect_uri: ruecksprung() })}`);
  } else anmelden();
}

async function mandantenLaden() {
  speicher("apiKey", $("#api-key").value.trim());
  const fehler = $("#anmeldefehler"); fehler.hidden = true;
  try {
    const liste = await api("/mandanten", { mitMandant: false });
    $("#mandant-auswahl").innerHTML = liste.map((m) => `<option value="${esc(m.id)}">${esc(m.name)}</option>`).join("")
      || `<option value="">Keine Betriebe: python -m core seed</option>`;
  } catch (e) { fehler.textContent = e.message; fehler.hidden = false; }
}
async function anmelden(meldung) {
  const d = $("#anmeldung"); if (d.open) return;
  $("#api-key").value = speicher("apiKey") || ""; d.showModal();
  $("#anmeldefehler").textContent = meldung || ""; $("#anmeldefehler").hidden = !meldung;
  try { $("#oidc-bereich").hidden = !(await konfiguration()).oidc; } catch { /* Core nicht erreichbar */ }
  if ($("#oidc-bereich").hidden && !meldung) mandantenLaden();
}
$("#oidc-anmelden").addEventListener("click", () => oidcAnmelden().catch((e) => { $("#anmeldefehler").textContent = e.message; $("#anmeldefehler").hidden = false; }));
$("#mandanten-laden").addEventListener("click", () => mandantenLaden());
$("#anmeldeformular").addEventListener("submit", () => {
  const auswahl = $("#mandant-auswahl");
  ["token", "refresh", "idToken"].forEach((k) => speicher(k, "")); ICH = null;
  speicher("apiKey", $("#api-key").value.trim()); speicher("mandant", auswahl.value);
  speicher("mandantName", auswahl.selectedOptions[0]?.textContent || ""); zeigen();
});
$("#abmelden").addEventListener("click", () => abmelden());

// --- Routing ---------------------------------------------------------------------------------
const ANSICHTEN = { einrichtung: einrichtungAnsicht, cockpit, anruf, freigaben, vorgaenge, plantafel, kunden, wartung, ontologie, rechnungen, protokoll, einstellungen,
  einsatz: einsatzAnsicht, belege: belegeAnsicht, zeiten: zeitenAnsicht, betrieb: betriebAnsicht, angebote: angeboteAnsicht,
  forderungen: forderungenAnsicht, vertraege: vertraegeAnsicht, anfragen: anfragenAnsicht, auswertungen: auswertungenAnsicht, lager: lagerAnsicht, gutscheine: gutscheineAnsicht,
  warteliste: wartelisteAnsicht, dienstplan: dienstplanAnsicht, website: websiteAnsicht, upsell: upsellAnsicht, integrationen: integrationenAnsicht };
async function zeigen() {
  if (!speicher("mandant")) return anmelden();
  $("#mandant-name").textContent = speicher("mandantName") || "";
  try {
    const wir = await ich();
    if (!BRANCHE) await brancheLaden();
    $("#nav-einstellungen").hidden = !wir.rechte.includes("verwalten");
    document.querySelectorAll(".seitenleiste a[data-recht]").forEach((a) => { a.hidden = !wir.rechte.includes(a.dataset.recht); });
  } catch { /* Anmeldung läuft */ }
  const name = (location.hash.slice(1) || "cockpit").split("/")[0];
  document.querySelectorAll(".seitenleiste a").forEach((a) => a.classList.toggle("aktiv", a.dataset.ansicht === name));
  const ziel = $("#inhalt");
  try { await (ANSICHTEN[name] || cockpit)(ziel); } catch (e) { if (e.message !== "Anmeldung erforderlich") ziel.innerHTML = `<p class="fehler">${esc(e.message)}</p>`; }
  freigabenZaehlen();
}
window.addEventListener("hashchange", zeigen);

// --- Globale Suche (Strg+K) ---------------------------------------------------------------------
const SUCHE_TYP = { kunde: "Kunden", servicefall: "Vorgänge", rechnung: "Rechnungen", angebot: "Angebote", beleg: "Belege", anfrage: "Anfragen", artikel: "Artikel" };
async function sucheOeffnen(t) {
  $("#suche-treffer").hidden = true;
  if (t.typ === "kunde") return kundeDetail(t.id);
  if (t.typ === "servicefall") return fallDetail(t.id);
  if (t.typ === "beleg") return belegDetail(t.id);
  location.hash = { rechnung: "#rechnungen", angebot: "#angebote", anfrage: "#anfragen", artikel: "#lager" }[t.typ] || "#cockpit";
}
(() => {
  const feld = $("#suche"), kasten = $("#suche-treffer"); let warte;
  const suchen = async () => {
    const q = feld.value.trim();
    if (q.length < 2) { kasten.hidden = true; return; }
    try {
      const { treffer } = await api(`/suche?q=${encodeURIComponent(q)}`);
      const gruppen = {}; treffer.forEach((t) => (gruppen[t.typ] ||= []).push(t));
      kasten.innerHTML = Object.entries(gruppen).map(([typ, liste]) => `<div class="gruppe">${esc(SUCHE_TYP[typ] || typ)}</div>${liste.map((t, i) => `<button type="button" data-typ="${esc(typ)}" data-id="${esc(t.id)}">${esc(t.titel)} ${t.status ? tag(t.status, "neutral") : ""}<small>${esc(t.untertitel || "")}</small></button>`).join("")}`).join("") || `<div class="gruppe">Keine Treffer</div>`;
      kasten.hidden = false;
      kasten.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => sucheOeffnen({ typ: b.dataset.typ, id: b.dataset.id })));
    } catch { kasten.hidden = true; }
  };
  feld.addEventListener("input", () => { clearTimeout(warte); warte = setTimeout(suchen, 200); });
  feld.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { kasten.hidden = true; feld.blur(); } if (ev.key === "ArrowDown") kasten.querySelector("button")?.focus(); if (ev.key === "Enter") kasten.querySelector("button")?.click(); });
  kasten.addEventListener("keydown", (ev) => { const b = [...kasten.querySelectorAll("button")]; const i = b.indexOf(document.activeElement); if (ev.key === "ArrowDown") b[i + 1]?.focus(); if (ev.key === "ArrowUp") (b[i - 1] || feld).focus(); if (ev.key === "Escape") { kasten.hidden = true; feld.focus(); } });
  document.addEventListener("keydown", (ev) => { if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "k") { ev.preventDefault(); feld.focus(); feld.select(); } });
  document.addEventListener("click", (ev) => { if (!ev.target.closest(".suche")) kasten.hidden = true; });
})();
async function freigabenZaehlen() {
  try { const n = (await api("/freigaben")).length; $("#zaehler-freigaben").innerHTML = n ? tag(n, "offen", n) : ""; } catch { /* Anmeldung läuft */ }
  try { if (ICH?.rechte.includes("finanzen")) { const n = (await api("/belege?status=erfasst")).length; $("#zaehler-belege").innerHTML = n ? tag(n, "offen", n) : ""; } } catch { /* Anmeldung läuft */ }
  try { if (ICH?.rechte.includes("finanzen")) { const n = (await api("/offene-posten")).filter((p) => p.tage_ueberfaellig > 0).length; $("#zaehler-op").innerHTML = n ? tag(n, "ueberfaellig", n) : ""; } } catch { /* Anmeldung läuft */ }
}
async function dateiLaden(pfad, name) {
  const r = await fetch(pfad, { headers: authKopf() });
  if (!r.ok) return melden(`HTTP ${r.status}`);
  const url = URL.createObjectURL(await r.blob()); const a = document.createElement("a"); a.href = url; a.download = name; a.target = "_blank"; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}
async function bildUrl(anhangId) {  // Anhänge liegen hinter der Authentifizierung, daher als Blob-URL laden
  const r = await fetch(`/anhaenge/${anhangId}/inhalt`, { headers: authKopf() });
  return r.ok ? URL.createObjectURL(await r.blob()) : "";
}
let ICH = null;  // Prinzipal der Sitzung (Name, Rollen, Rechte), nach jedem Anmelden neu geladen
async function ich() { return ICH || (ICH = await api("/ich", { mitMandant: false })); }
let BRANCHE = null;
async function brancheLaden() {
  try { BRANCHE = await api("/betrieb/branche"); } catch { BRANCHE = null; return; }
  const b = BRANCHE.begriffe;
  const setzen = (ansicht, text) => { const a = document.querySelector(`.seitenleiste a[data-ansicht="${ansicht}"]`); if (a && a.firstChild?.nodeType === 3) a.firstChild.textContent = text + " "; };
  setzen("einsatz", b.mein_einsatz); setzen("kunden", b.kartei.replace("&", "&")); setzen("vorgaenge", b.vorgaenge); setzen("wartung", b.wartung);
  const w = document.querySelector('.seitenleiste a[data-ansicht="wartung"]'); if (w) w.hidden = !b.anlagen;
  const o = document.querySelector('.seitenleiste a[data-ansicht="ontologie"]'); if (o) o.hidden = !b.anlagen;
}
function wort(schluessel, standard) { return BRANCHE?.begriffe?.[schluessel] || standard; }
function detail(html) { $("#detail-inhalt").innerHTML = html; const d = $("#detail"); if (!d.open) d.showModal(); return $("#detail-inhalt"); }

// --- Cockpit ---------------------------------------------------------------------------------
async function cockpit(ziel) {
  const [k, z, notfaelle, karte, voll, touren] = await Promise.all([api("/kennzahlen"), api("/twin/betrieb"), api("/servicefaelle?dringlichkeit=notdienst"),
    api("/kunden/karte"), api("/kunden/vollstaendigkeit"), api("/tour").catch(() => ({ touren: [] }))]);
  const linien = touren.touren.filter((x) => x.linie.length > 1).map((x) => ({ name: x.name.split(" (")[0], punkte: x.linie, minuten: x.ist.minuten, km: x.ist.km }));
  const hoch = await api("/servicefaelle?dringlichkeit=hoch");
  const [risiko, prognose] = await Promise.all([api("/risiko?limit=5").catch(() => null), api("/prognose/auslastung?wochen=8").catch(() => null)]);
  const dringend = [...notfaelle, ...hoch].filter((f) => ["eingegangen", "qualifiziert"].includes(f.status));
  const kachel = (wert, bez) => `<div class="karte kachel"><div class="wert">${esc(wert ?? "–")}</div><div class="bez">${esc(bez)}</div></div>`;
  const einr = ICH?.rechte?.includes("verwalten") ? await api("/betrieb/einrichtung").catch(() => null) : null;
  const ups = ICH?.rechte?.includes("bearbeiten") ? await api("/upsell/uebersicht?limit=1").catch(() => null) : null;
  ziel.innerHTML = `
    <h1>Cockpit</h1><p class="unterzeile">Digital Twin des Betriebs · letzte ${k.zeitraum_tage} Tage</p>
    ${einr && !einr.fertig ? `<a class="karte banner klickbar" href="#einrichtung"><div><strong>Einrichtung: ${einr.erledigt} von ${einr.pflicht} Schritten erledigt</strong><div class="balken"><span style="width:${Math.round(einr.fortschritt * 100)}%"></span></div>
      <small>Als Nächstes: ${einr.naechste.map((s) => esc(s.titel)).join(" · ")}</small></div><span class="knopf">Weiter einrichten ›</span></a>` : ""}
    <div class="kacheln">
      ${kachel(k.offene_vorgaenge, "offene Vorgänge")}
      ${kachel(k.offene_freigaben, "offene Freigaben")}
      ${kachel(k.anrufe_erfasst, "Anrufe erfasst")}
      ${kachel(z.notdienst_offen, "Notdienst offen")}
      ${kachel(k.median_stunden_bis_einplanung === null ? "–" : `${k.median_stunden_bis_einplanung} h`, "Median Eingang → Einplanung")}
      ${kachel(k.automatisierungsquote === null ? "–" : `${Math.round(k.automatisierungsquote * 100)} %`, "automatisch freigegeben")}
      ${kachel(z.wartungspotenzial.length, "Wartungen fällig")}
      ${kachel(k.outbox_tot, "Zustellfehler (Dead Letter)")}
      <a class="karte kachel klickbar" href="#kunden/unvollstaendig"><div class="wert">${esc(voll.vollstaendig)} / ${esc(voll.kunden)}</div><div class="bez">Kundendaten vollständig</div></a>
      ${ups ? `<a class="karte kachel klickbar" href="#upsell"><div class="wert">${esc(EURO.format(Number(ups.potenzial)))}</div><div class="bez">Zusatzpotenzial bei ${esc(ups.kunden_mit_empfehlung)} Kunden</div></a>` : ""}
    </div>
    <div class="raster raster-karte">
      <div class="karte"><h2>Wo sind die Kunden</h2><p class="unterzeile" style="margin-bottom:10px">${karte.kunden.length} Kunden mit Koordinaten${karte.ohne_koordinaten.length ? `, ${karte.ohne_koordinaten.length} ohne (<a href="#kunden/unvollstaendig">geokodieren</a>)` : ""} · Klick auf einen Punkt öffnet den Kunden</p><div id="c-karte"></div></div>
      <div>
        <div class="karte" style="margin-bottom:16px"><h2>Heute unterwegs</h2>${touren.touren.some((x) => x.stopps.length) ? `<p class="unterzeile" style="margin-bottom:8px">${touren.touren.filter((x) => x.stopps.length).map((x) => `${esc(x.name.split(" (")[0])}: ${x.ist.minuten} min Fahrt, ${x.ist.km} km${x.vorschlag?.ersparnis_minuten ? ` (<a href="#plantafel">Tour: −${x.vorschlag.ersparnis_minuten} min möglich</a>)` : ""}`).join(" · ")}${touren.touren[0]?.quelle === "luftlinie" ? " · Luftlinie, OSRM nicht erreichbar" : ""}</p>` : ""}${karte.einsaetze_heute.length ? `<table><tbody>${karte.einsaetze_heute.map((e) => `<tr class="klickbar" data-fall="${esc(e.servicefall_id)}"><td>${esc(UHRZEIT.format(new Date(e.beginn)))}</td><td>${esc(e.ressource.split(" (")[0])}</td><td>${esc(e.kunde)}${e.lat == null ? ' <span class="marke-tag t-neutral">ohne Koordinaten</span>' : ""}</td><td>${tag(e.status, e.status, e.status === "bestaetigt" ? "bestätigt" : "vorgemerkt")}</td></tr>`).join("")}</tbody></table>` : "<p>Heute keine Einsätze geplant.</p>"}</div>
        <div class="karte"><h2>Kunden je Ort</h2><table><tbody>${karte.je_ort.map((o) => `<tr><td>${esc(o.ort)}<br><small>${esc(o.plz.join(", "))}</small></td><td style="text-align:right">${esc(o.anzahl)}</td></tr>`).join("") || "<tr><td>Keine Kunden.</td></tr>"}</tbody></table></div>
      </div>
    </div>
    <div class="raster">
      <div class="karte"><h2>Dringend und noch nicht eingeplant</h2>${dringend.length ? `<table><tbody>${dringend.map((f) =>
        `<tr class="klickbar" data-fall="${esc(f.id)}"><td>${tag(f.dringlichkeit)}</td><td>${esc(f.anliegen)}</td><td>${esc(STATUS_TEXT[f.status])}</td></tr>`).join("")}</tbody></table>` : "<p>Nichts offen.</p>"}</div>
      <div class="karte"><h2>Auslastung nächste 5 Tage</h2>${z.auslastung.map((a) => `
        <div style="margin-bottom:10px"><div class="zeile" style="justify-content:space-between"><span>${esc(a.name)}</span><span>${a.gebucht_h} / ${a.kapazitaet_h} h</span></div>
        <div class="balken"><span style="width:${Math.min(100, Math.round((a.quote || 0) * 100))}%"></span></div></div>`).join("")}</div>
    </div>
    ${risiko || prognose ? `<div class="raster">
      ${risiko ? `<div class="karte"><h2>Ausfallrisiko der Anlagen</h2><p class="unterzeile" style="margin-bottom:8px">Weibull-Überlebensmodell je Anlagentyp mit Wartungsrückstand und Störhistorie; erwartete Ausfälle in der Heizperiode: <strong>${esc(risiko.erwartete_ausfaelle_heizperiode)}</strong> (${risiko.je_klasse.kritisch} kritisch, ${risiko.je_klasse.hoch} hoch).</p>
        <table><tbody>${risiko.anlagen.map((a) => `<tr class="klickbar" data-kunde="${esc(a.kunde_id)}"><td>${tag(a.klasse, a.klasse === "kritisch" ? "ueberfaellig" : (a.klasse === "hoch" ? "offen" : "neutral"))}</td><td>${esc(a.kunde)}<br><small>${esc(a.bezeichnung)}${a.alter_jahre ? `, ${a.alter_jahre} J.` : ""} · ${esc(a.treiber.join(", ") || "keine Auffälligkeit")}</small></td><td style="text-align:right">${Math.round(a.p_12_monate * 100)} %</td></tr>`).join("") || "<tr><td>Keine Anlagen.</td></tr>"}</tbody></table>
        ${risiko.ersatzteilvorhaltung.length ? `<p style="margin-top:8px"><small>Ersatzteile vorhalten: ${risiko.ersatzteilvorhaltung.slice(0, 4).map((e) => esc(`${e.teil} (${e.erwarteter_bedarf})`)).join(", ")}</small></p>` : ""}</div>` : ""}
      ${prognose ? `<div class="karte"><h2>Auslastungsprognose 8 Wochen</h2><p class="unterzeile" style="margin-bottom:8px">${esc(prognose.verfahren)}; Bedarf = prognostizierte Vorgänge × ${prognose.mittlere_dauer_h} h.</p>
        ${prognose.wochen.map((w) => `<div style="margin-bottom:8px"><div class="zeile" style="justify-content:space-between"><span>KW ${w.kw} ${w.lage === "engpass" ? tag("Engpass", "ueberfaellig") : (w.lage === "unterauslastung" ? tag("frei", "aktuell") : "")}</span><span>${w.vorgaenge} Vorgänge (${w.untergrenze}–${w.obergrenze}) · ${w.auslastung === null ? "–" : `${Math.round(w.auslastung * 100)} %`}</span></div>
          <div class="balken"><span style="width:${Math.min(100, Math.round((w.auslastung || 0) * 100))}%"></span></div></div>`).join("")}
        ${prognose.warnungen.length ? `<ul class="verlauf">${prognose.warnungen.slice(0, 3).map((x) => `<li><small>${esc(x.text)}</small></li>`).join("")}</ul>` : ""}</div>` : ""}
    </div>` : ""}
    <div class="raster">
      <div class="karte"><h2>Was an Kundendaten fehlt</h2><table><tbody>${voll.je_feld.map((f) => `<tr><td>${esc(f.text)}</td><td style="text-align:right">${f.anzahl ? tag(f.anzahl, "offen", f.anzahl) : tag("0", "aktuell", "0")}</td></tr>`).join("")}</tbody></table></div>
      <div class="karte"><h2>Vorgänge je Status</h2><table><tbody>${STATUS.map((s) => `<tr><td>${STATUS_TEXT[s]}</td><td style="text-align:right">${z.vorgaenge_je_status[s] || 0}</td></tr>`).join("")}</tbody></table></div>
      <div class="karte"><h2>Letzte Ereignisse</h2><ul class="verlauf">${z.letzte_ereignisse.slice(0, 8).map((e) => `<li>${tag(e.akteur, "neutral")} <code>${esc(e.typ)}</code> · ${zeit(e.zeit)}</li>`).join("")}</ul></div>
    </div>`;
  karteZeichnen($("#c-karte"), karte, { beimKlick: kundeDetail, linien });
  ziel.querySelectorAll("[data-fall]").forEach((z) => z.addEventListener("click", () => fallDetail(z.dataset.fall)));
  ziel.querySelectorAll("table [data-kunde]").forEach((z) => z.addEventListener("click", () => kundeDetail(z.dataset.kunde)));
}

// --- Kundenkarte (eigene Zeichnung, Web-Mercator; Hintergrundkacheln vom konfigurierten Kachelserver) ----
const KACHEL = 256;
const WARTUNG_TEXT = { ueberfaellig: "Wartung überfällig", bald_faellig: "Wartung bald fällig", aktuell: "Wartung aktuell" };
function mercator(lat, lon, z) {
  const n = 2 ** z * KACHEL, phi = (lat * Math.PI) / 180;
  return { x: ((lon + 180) / 360) * n, y: ((1 - Math.log(Math.tan(phi) + 1 / Math.cos(phi)) / Math.PI) / 2) * n };
}
const TOURFARBEN = ["#0b5cad", "#1d7a46", "#a86400", "#7c3aed", "#b42318", "#0e7490"];
function karteZeichnen(wurzel, daten, { hoehe = 420, beimKlick, linien = [] } = {}) {
  const punkte = [...daten.kunden.filter((p) => p.lat != null), ...(daten.betrieb.lat != null ? [{ ...daten.betrieb, betrieb: true }] : [])];
  const heute = new Set(daten.einsaetze_heute.map((e) => e.kunde_id));
  wurzel.innerHTML = `<div class="geo" style="height:${hoehe}px" tabindex="0" aria-label="Kundenkarte"><div class="geo-kacheln"></div><svg class="geo-linien" aria-hidden="true"></svg><div class="geo-marker"></div>
    <div class="geo-steuer"><button type="button" class="leise" data-zoom="1" aria-label="Vergrößern">+</button><button type="button" class="leise" data-zoom="-1" aria-label="Verkleinern">−</button></div>
    <div class="geo-hinweis">${esc(daten.kacheln.hinweis || "")}</div></div>
    <div class="geo-legende"><span><i class="m-betrieb"></i>Betrieb</span><span><i class="m-notdienst"></i>Notdienst offen</span><span><i class="m-hoch"></i>dringend</span><span><i class="m-ueberfaellig"></i>Wartung überfällig</span><span><i class="m-vertrag"></i>Wartungsvertrag</span><span><i class="m-kunde"></i>Kunde</span><span><i class="m-heute"></i>heute Einsatz</span>${linien.map((l, i) => `<span><i style="background:${TOURFARBEN[i % TOURFARBEN.length]};border-radius:0;height:3px;width:14px"></i>Tour ${esc(l.name)}${l.minuten != null ? ` (${l.minuten} min, ${l.km} km)` : ""}</span>`).join("")}</div>`;
  const geo = $(".geo", wurzel), kacheln = $(".geo-kacheln", wurzel), marker = $(".geo-marker", wurzel), svg = $(".geo-linien", wurzel);
  if (!punkte.length) { marker.innerHTML = `<p class="geo-leer">Noch keine Koordinaten. Adressen unter „Kunden &amp; Anlagen“ geokodieren.</p>`; return; }
  const B = geo.clientWidth || 800, H = hoehe;
  const lats = [...punkte.map((p) => p.lat), ...linien.flatMap((l) => l.punkte.map((q) => q[0]))], lons = [...punkte.map((p) => p.lon), ...linien.flatMap((l) => l.punkte.map((q) => q[1]))];
  const mitte = { lat: (Math.min(...lats) + Math.max(...lats)) / 2, lon: (Math.min(...lons) + Math.max(...lons)) / 2 };
  let z = 16;
  for (; z > 3; z--) { const a = mercator(Math.max(...lats), Math.min(...lons), z), b = mercator(Math.min(...lats), Math.max(...lons), z); if (b.x - a.x < B - 80 && b.y - a.y < H - 80) break; }
  const lage = { z, ...mercator(mitte.lat, mitte.lon, z) };  // Mittelpunkt in Pixeln der aktuellen Zoomstufe
  const zeichnen = () => {
    const links = lage.x - B / 2, oben = lage.y - H / 2, n = 2 ** lage.z;
    kacheln.innerHTML = "";
    if (daten.kacheln.url) {
      for (let tx = Math.floor(links / KACHEL); tx <= Math.floor((links + B) / KACHEL); tx++) {
        for (let ty = Math.max(0, Math.floor(oben / KACHEL)); ty <= Math.min(n - 1, Math.floor((oben + H) / KACHEL)); ty++) {
          const img = document.createElement("img");
          img.src = daten.kacheln.url.replace("{z}", lage.z).replace("{x}", ((tx % n) + n) % n).replace("{y}", ty);
          img.alt = ""; img.loading = "lazy"; img.draggable = false; img.width = img.height = KACHEL;
          img.addEventListener("error", () => { img.remove(); $(".geo-hinweis", wurzel).textContent = "Kartenhintergrund nicht erreichbar (Kachelserver, CSP oder Netz); Punkte sind lagerichtig zueinander."; });
          img.style.left = `${tx * KACHEL - links}px`; img.style.top = `${ty * KACHEL - oben}px`;
          kacheln.append(img);
        }
      }
    }
    svg.setAttribute("viewBox", `0 0 ${B} ${H}`); svg.setAttribute("width", B); svg.setAttribute("height", H);
    svg.innerHTML = linien.map((l, i) => { const d = l.punkte.map((q, k) => { const m = mercator(q[0], q[1], lage.z); return `${k ? "L" : "M"}${(m.x - links).toFixed(1)} ${(m.y - oben).toFixed(1)}`; }).join(" ");
      return `<path d="${d}" fill="none" stroke="${TOURFARBEN[i % TOURFARBEN.length]}" stroke-width="3" stroke-linejoin="round" stroke-opacity=".85"><title>Tour ${esc(l.name)}</title></path>`; }).join("");
    marker.innerHTML = punkte.map((p) => {
      const pos = mercator(p.lat, p.lon, lage.z);
      const art = p.betrieb ? "betrieb" : p.dringlichkeit === "notdienst" ? "notdienst" : p.dringlichkeit === "hoch" ? "hoch" : p.wartung === "ueberfaellig" ? "ueberfaellig" : p.vertragsstatus === "wartungsvertrag" ? "vertrag" : "kunde";
      const titel = p.betrieb ? `${p.name} (Betrieb)` : `${p.name} · ${p.strasse || ""}, ${p.plz || ""} ${p.ort || ""} · ${p.offene_vorgaenge} offene Vorgänge · ${WARTUNG_TEXT[p.wartung] || ""}`;
      const l = pos.x - links, o = pos.y - oben;
      return `<button type="button" class="m m-${art}${heute.has(p.id) ? " m-heute" : ""}" style="left:${l}px;top:${o}px" title="${esc(titel)}" ${p.betrieb ? "" : `data-kunde="${esc(p.id)}"`} aria-label="${esc(titel)}"></button>${lage.z >= 11 || p.betrieb ? `<span class="m-text" style="left:${l + 11}px;top:${o}px">${esc(p.name)}</span>` : ""}`;
    }).join("");
    marker.querySelectorAll("[data-kunde]").forEach((b) => b.addEventListener("click", () => beimKlick?.(b.dataset.kunde)));
  };
  const zoomen = (delta, px = B / 2, py = H / 2) => {
    const neu = Math.max(3, Math.min(18, lage.z + delta)); if (neu === lage.z) return;
    const f = 2 ** (neu - lage.z);  // Punkt unter dem Zeiger bleibt stehen
    lage.x = (lage.x - B / 2 + px) * f - px + B / 2; lage.y = (lage.y - H / 2 + py) * f - py + H / 2; lage.z = neu; zeichnen();
  };
  wurzel.querySelectorAll("[data-zoom]").forEach((b) => b.addEventListener("click", () => zoomen(Number(b.dataset.zoom))));
  geo.addEventListener("wheel", (ev) => { ev.preventDefault(); const r = geo.getBoundingClientRect(); zoomen(ev.deltaY < 0 ? 1 : -1, ev.clientX - r.left, ev.clientY - r.top); }, { passive: false });
  let zug = null;
  geo.addEventListener("pointerdown", (ev) => { if (ev.target.closest("button")) return; zug = { x: ev.clientX, y: ev.clientY }; geo.setPointerCapture(ev.pointerId); });
  geo.addEventListener("pointermove", (ev) => { if (!zug) return; lage.x -= ev.clientX - zug.x; lage.y -= ev.clientY - zug.y; zug = { x: ev.clientX, y: ev.clientY }; zeichnen(); });
  geo.addEventListener("pointerup", () => { zug = null; });
  geo.addEventListener("keydown", (ev) => { const s = 60; if (ev.key === "+") zoomen(1); else if (ev.key === "-") zoomen(-1); else if (ev.key === "ArrowLeft") { lage.x -= s; zeichnen(); } else if (ev.key === "ArrowRight") { lage.x += s; zeichnen(); } else if (ev.key === "ArrowUp") { lage.y -= s; zeichnen(); } else if (ev.key === "ArrowDown") { lage.y += s; zeichnen(); } else return; ev.preventDefault(); });
  zeichnen();
}

// --- Anrufsimulator --------------------------------------------------------------------------
async function anruf(ziel) {
  const typen = await api("/anlagentypen");
  ziel.innerHTML = `
    <h1>Anruf simulieren</h1>
    <p class="unterzeile">Spielt die Werkzeugkette des Voice-Agenten ohne Telefon durch: genau die Aufrufe, die der LiveKit-Agent im Gespräch an den Core sendet. Sprache und Formulierung übernimmt im Echtbetrieb das LLM, Entscheidungen trifft der Core.</p>
    <div class="raster">
      <div class="karte">
        <div class="zeile">
          <label>Rufnummer des Anrufers <input id="a-tel" value="0221 990101"></label>
          <label>Anlage (optional) <select id="a-typ"><option value="">unbekannt</option>${Object.entries(typen).map(([k, v]) => `<option value="${esc(k)}">${esc(v.bezeichnung)}</option>`).join("")}</select></label>
        </div>
        <label>Anliegen in den Worten des Anrufers <input id="a-text" value="Meine Heizung ist aus, die Therme zeigt eine Störung"></label>
        <div class="zeile"><label>Name (für neue Anrufer) <input id="a-name" placeholder="wird bei bekannten Anrufern übernommen"></label><label>PLZ und Ort <input id="a-ort" placeholder="z. B. 50667 Köln"></label></div>
        <div class="aktionen"><button class="leise" id="a-gas" type="button">Beispiel: Gasgeruch</button><button id="a-start">Anruf starten</button></div>
      </div>
      <div class="karte"><h2>Gesprächsverlauf</h2><div class="gespraech" id="a-verlauf"><p class="unterzeile">Noch kein Anruf.</p></div></div>
    </div>`;
  $("#a-gas").addEventListener("click", () => { $("#a-text").value = "Im Keller riecht es stark nach Gas"; $("#a-typ").value = "gas_brennwert"; });
  $("#a-start").addEventListener("click", () => anrufDurchspielen().catch((e) => sagen("system", `Fehler: ${e.message}`)));
}
function sagen(wer, text, html = false) {
  const v = $("#a-verlauf"); if (v.querySelector(".unterzeile")) v.innerHTML = "";
  const el = document.createElement("div"); el.className = `sprechblase ${wer}`;
  if (html) el.innerHTML = text; else el.textContent = text; v.append(el); return el;
}
async function anrufDurchspielen() {
  $("#a-verlauf").innerHTML = "";
  const telefon = $("#a-tel").value, anliegen = $("#a-text").value, anlagentyp = $("#a-typ").value || null;
  sagen("agent", `Guten Tag, hier ist der digitale Assistent von ${speicher("mandantName")}. Ich bin ein KI-System und helfe Ihnen, einen Termin zu vereinbaren. Worum geht es?`);
  const kunde = await api("/voice/kunde-suchen", { methode: "POST", daten: { telefon } });
  sagen("system", `kunde_suchen(${telefon}) → ${kunde.gefunden ? `${kunde.name}, ${kunde.anlagen.length} Anlage(n), ${kunde.offene_vorgaenge.length} offen` : "unbekannt"}`);
  sagen("anrufer", anliegen);
  const fenster = await api("/voice/zeitfenster", { methode: "POST", daten: { anliegen, anlagentyp } });
  sagen("system", `zeitfenster_abfragen → ${fenster.sicherheitsfall ? "SICHERHEITSFALL" : `Dringlichkeit ${fenster.dringlichkeit}, ${fenster.zeitfenster.length} Fenster`}`);
  if (fenster.sicherheitsfall) {
    sagen("agent", fenster.hinweis).classList.add("sicherheit");
    sagen("system", "Gespräch beendet: kein Terminangebot, Vorgang zur Nachverfolgung anlegen lassen.");
    return;
  }
  if (!fenster.zeitfenster.length) { sagen("agent", "Ich finde gerade keinen freien Termin. Ich lasse Sie vom Büro zurückrufen."); return; }
  const anrede = kunde.gefunden ? `${kunde.name}, ` : "";
  const wahl = sagen("agent", `${esc(anrede)}ich kann Ihnen folgende Termine anbieten:<div class="wahl">${fenster.zeitfenster.slice(0, 3).map((z, i) =>
    `<button data-i="${i}">${esc(WOCHENTAG.format(new Date(z.beginn)))}, ${esc(UHRZEIT.format(new Date(z.beginn)))} Uhr</button>`).join("")}</div>`, true);
  const index = await new Promise((fertig) => wahl.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => fertig(Number(b.dataset.i)))));
  wahl.querySelectorAll("button").forEach((b) => (b.disabled = true));
  const z = fenster.zeitfenster[index];
  sagen("anrufer", `${WOCHENTAG.format(new Date(z.beginn))} um ${UHRZEIT.format(new Date(z.beginn))} passt.`);
  const [plz, ...ort] = ($("#a-ort").value || "").split(" ");
  const fall = await api("/voice/servicefall", { methode: "POST", daten: {
    telefon, name: kunde.gefunden ? kunde.name : ($("#a-name").value || "Unbekannter Anrufer"), anliegen, anlagentyp,
    plz: kunde.gefunden ? null : plz || null, ort: kunde.gefunden ? null : ort.join(" ") || null,
    ressource_id: z.ressource_id, beginn: z.beginn } });
  sagen("system", `servicefall_anlegen → Vorgang ${fall.servicefall_id.slice(0, 8)}…, Dringlichkeit ${fall.dringlichkeit}, Termin ${fall.termin.verbindlich ? "verbindlich" : "vorgemerkt (Freigabe Klasse B)"}`);
  sagen("agent", fall.termin.verbindlich ? "Der Termin ist fest eingetragen. Sie erhalten eine Bestätigung per SMS." : fall.termin.hinweis);
  await api("/voice/gespraech-beendet", { methode: "POST", daten: { telefon, servicefall_id: fall.servicefall_id,
    transkript: [...document.querySelectorAll("#a-verlauf .sprechblase:not(.system)")].map((e) => `${e.classList.contains("agent") ? "Agent" : "Anrufer"}: ${e.textContent}`).join("\n"),
    zusammenfassung: anliegen } });
  sagen("system", "gespraech_beendet → Transkript am Vorgang gespeichert. Die Freigabe liegt jetzt in der Freigabe-Inbox.");
  freigabenZaehlen();
}

// --- Freigabe-Inbox --------------------------------------------------------------------------
async function freigaben(ziel) {
  const [offen, erledigt, wir] = await Promise.all([api("/freigaben"), api("/freigaben?status=freigegeben"), ich()]);
  const sperre = (f) => (f.risikoklasse === "C" && !wir.rechte.includes("freigeben_c")) || !wir.rechte.includes("freigeben")
    ? ` disabled title="Ihre Rolle darf diese Aktion nicht entscheiden"` : "";
  ziel.innerHTML = `
    <h1>Freigabe-Inbox</h1><p class="unterzeile">Human-in-the-Loop: Klasse B (kundenwirksam) nach Mandanten-Policy, Klasse C (finanzwirksam) immer manuell.</p>
    <div class="karte"><div class="tabelle-rahmen"><table><thead><tr><th>Klasse</th><th>Aktion</th><th>Begründung</th><th>Eingang</th><th></th></tr></thead><tbody>
    ${offen.map((f) => `<tr><td>${tag(f.risikoklasse, f.risikoklasse, KLASSE_TEXT[f.risikoklasse])}</td><td>${esc(AKTION_TEXT[f.aktion] || f.aktion)}</td>
      <td>${esc(f.begruendung || "")}${f.servicefall_id ? ` · <a href="#" data-fall="${esc(f.servicefall_id)}">Vorgang</a>` : ""}</td><td>${zeit(f.erstellt)}</td>
      <td><div class="aktionen"><button class="gefahr" data-entscheidung="${esc(f.id)}" data-ja="0"${sperre(f)}>Ablehnen</button><button data-entscheidung="${esc(f.id)}" data-ja="1" data-klasse="${esc(f.risikoklasse)}"${sperre(f)}>Freigeben</button></div></td></tr>`).join("")
      || `<tr><td colspan="5">Keine offenen Freigaben.</td></tr>`}
    </tbody></table></div></div>
    <h2 style="margin-top:24px">Zuletzt freigegeben</h2>
    <div class="karte"><table><tbody>${erledigt.slice(-8).reverse().map((f) => `<tr><td>${tag(f.risikoklasse)}</td><td>${esc(AKTION_TEXT[f.aktion] || f.aktion)}</td><td>${esc(f.entschieden_von)}</td><td>${esc(f.begruendung || "")}</td></tr>`).join("") || "<tr><td>–</td></tr>"}</tbody></table></div>`;
  ziel.querySelectorAll("[data-entscheidung]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    const wer = b.dataset.klasse === "C" ? "inhaber" : "buero";  // ohne Anmeldung: Klasse C ausdrücklich als Inhaber, mit OIDC zählt der Token
    try { await api(`/freigaben/${b.dataset.entscheidung}/entscheidung`, { methode: "POST", daten: { freigeben: b.dataset.ja === "1", wer } }); zeigen(); }
    catch (e) { b.disabled = false; melden(e.message); }
  }));
  ziel.querySelectorAll("a[data-fall]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); fallDetail(a.dataset.fall); }));
}

// --- Vorgänge (Kanban) -----------------------------------------------------------------------
async function vorgaenge(ziel) {
  const faelle = await api("/servicefaelle?limit=500");
  ziel.innerHTML = `<h1>${esc(wort("vorgaenge", "Vorgänge"))}</h1><p class="unterzeile">Statusautomat des Servicefalls; jeder Übergang wird im Ereignisprotokoll festgehalten.</p>
    <div class="kanban">${STATUS.map((s) => { const liste = faelle.filter((f) => f.status === s); return `
      <div class="spalte"><h3><span>${STATUS_TEXT[s]}</span><span>${liste.length}</span></h3>
      ${liste.map((f) => `<div class="fallkarte" data-fall="${esc(f.id)}"><strong>${esc(f.anliegen)}</strong>${tag(f.dringlichkeit)} ${tag(f.quelle, "neutral")}</div>`).join("")}</div>`; }).join("")}</div>`;
  ziel.querySelectorAll("[data-fall]").forEach((k) => k.addEventListener("click", () => fallDetail(k.dataset.fall)));
}
async function fallDetail(id) {
  const d = await api(`/servicefaelle/${id}`);
  const f = d.servicefall;
  const kunde = await api(`/kunden/${f.kunde_id}`);
  const wurzel = detail(`
    <h2>${esc(f.anliegen)}</h2>
    <p>${tag(f.dringlichkeit)} ${tag(STATUS_TEXT[f.status], "neutral")} ${f.benoetigte_qualifikation ? tag(`Qualifikation: ${f.benoetigte_qualifikation}`, "neutral") : ""}</p>
    <p><strong>${esc(kunde.kunde.name)}</strong> · ${esc(kunde.kunde.strasse || "")} ${esc(kunde.kunde.plz || "")} ${esc(kunde.kunde.ort || "")} · ${esc(kunde.kunde.telefon || "")}</p>
    ${f.symptom ? `<p>Symptom: ${esc(f.symptom)}</p>` : ""}
    <h3>Termine</h3>${d.termine.length ? `<ul class="verlauf">${d.termine.map((t) => `<li>${zeit(t.beginn)} ${tag(t.status)}${["vorgeschlagen", "bestaetigt"].includes(t.status) && new Date(t.beginn) > new Date() ? ` <button class="leise" data-absage="${esc(t.id)}" title="Absagen; die Lücke geht an die Warteliste">Absagen</button>` : ""}${t.status === "bestaetigt" && new Date(t.beginn) < new Date() ? ` <button class="leise" data-noshow="${esc(t.id)}">Nicht erschienen</button>` : ""}</li>`).join("")}</ul>` : "<p>Noch kein Termin.</p>"}
    ${kunde.kunde.nicht_erschienen ? `<p>${tag(`${kunde.kunde.nicht_erschienen}× nicht erschienen`, "ueberfaellig")}</p>` : ""}
    <div class="aktionen" style="margin:12px 0">${(UEBERGAENGE[f.status] || []).map((z) => `<button class="${z === "storniert" ? "gefahr" : "leise"}" data-ziel="${z}">${STATUS_TEXT[z]}</button>`).join("")}
      ${["eingegangen", "qualifiziert"].includes(f.status) ? `<button id="termin-suchen">Termin vorschlagen</button>` : ""}
      ${["qualifiziert", "eingeplant"].includes(f.status) ? `<button class="leise" id="warteliste-knopf">Auf die Warteliste</button>` : ""}</div>
    <div id="terminwahl"></div><div id="wartelisteform"></div>
    <h3>Ereignisse</h3><ul class="verlauf">${d.ereignisse.map((e) => `<li>${tag(e.akteur, "neutral")} <code>${esc(e.typ)}</code> · ${zeit(e.zeit)}${e.payload?.grund ? ` · ${esc(e.payload.grund)}` : ""}</li>`).join("")}</ul>`);
  fallErgaenzungen(wurzel, id);
  fallGarantie(wurzel, id);
  wurzel.querySelectorAll("[data-absage]").forEach((b) => b.addEventListener("click", async () => {
    const grund = prompt("Grund der Absage (optional):"); if (grund === null) return;
    try { await api(`/termine/${b.dataset.absage}/absagen`, { methode: "POST", daten: { grund: grund || null } }); await fallDetail(id); zeigen(); } catch (e) { melden(e.message); }
  }));
  $("#warteliste-knopf", wurzel)?.addEventListener("click", () => {
    const heute = TAGESSCHLUESSEL.format(new Date()), bis = TAGESSCHLUESSEL.format(new Date(Date.now() + 14 * 86400000));
    $("#wartelisteform", wurzel).innerHTML = `<form id="wl-form" class="zeile" style="margin:8px 0"><label>von <input type="date" name="wunsch_von" value="${heute}" required></label><label>bis <input type="date" name="wunsch_bis" value="${bis}" required></label>
      <label><span><input type="checkbox" name="vormittag"> vormittags</span></label><label><span><input type="checkbox" name="nachmittag"> nachmittags</span></label><button type="submit" class="leise">Eintragen</button><span id="wl-status"></span></form>`;
    $("#wl-form", wurzel).addEventListener("submit", async (ev) => {
      ev.preventDefault(); const fd = new FormData(ev.target);
      const tageszeiten = ["vormittag", "nachmittag"].filter((x) => fd.get(x));
      try { await api(`/servicefaelle/${id}/warteliste`, { methode: "POST", daten: { wunsch_von: fd.get("wunsch_von"), wunsch_bis: fd.get("wunsch_bis"), tageszeiten } });
        $("#wl-status", wurzel).innerHTML = `<span class="hinweis">Eingetragen. Freie Lücken werden automatisch angeboten.</span>`; }
      catch (e) { $("#wl-status", wurzel).innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
    });
  });
  wurzel.querySelectorAll("[data-noshow]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Termin als „nicht erschienen“ markieren?")) return;
    try { await api(`/termine/${b.dataset.noshow}/nicht-erschienen`, { methode: "POST" }); await fallDetail(id); zeigen(); } catch (e) { melden(e.message); }
  }));
  wurzel.querySelectorAll("[data-ziel]").forEach((b) => b.addEventListener("click", async () => {
    try { await api(`/servicefaelle/${id}/status`, { methode: "POST", daten: { ziel: b.dataset.ziel, wer: "buero" } }); await fallDetail(id); zeigen(); }
    catch (e) { melden(e.message); }
  }));
  $("#termin-suchen", wurzel)?.addEventListener("click", async () => {
    const q = f.benoetigte_qualifikation ? `&qualifikation=${encodeURIComponent(f.benoetigte_qualifikation)}` : "";
    const fenster = await api(`/disposition/zeitfenster?dauer_minuten=${f.dauer_minuten}&anzahl=6${q}`);
    $("#terminwahl", wurzel).innerHTML = fenster.length ? `<div class="wahl">${fenster.map((z, i) => `<button class="leise" data-i="${i}">${esc(WOCHENTAG.format(new Date(z.beginn)))} ${esc(UHRZEIT.format(new Date(z.beginn)))} · ${esc(z.ressource_name.split(" (")[0])}</button>`).join("")}</div>` : "<p>Kein passendes Zeitfenster.</p>";
    $("#terminwahl", wurzel).querySelectorAll("[data-i]").forEach((b) => b.addEventListener("click", async () => {
      const z = fenster[Number(b.dataset.i)];
      try { await api(`/servicefaelle/${id}/termine`, { methode: "POST", daten: { ressource_id: z.ressource_id, beginn: z.beginn } }); await fallDetail(id); zeigen(); }
      catch (e) { melden(e.message); }
    }));
  });
}

// --- Plantafel -------------------------------------------------------------------------------
async function plantafel(ziel) {
  const [p, wir] = await Promise.all([api("/plantafel?tage=5"), ich()]);
  const tage = [...Array(p.tage).keys()].map((i) => new Date(Date.parse(`${p.von}T12:00:00Z`) + i * 86400000));
  const gleicherTag = (iso, d) => TAGESSCHLUESSEL.format(new Date(iso)) === TAGESSCHLUESSEL.format(d);
  ziel.innerHTML = `<h1>Plantafel</h1><p class="unterzeile">${wir.eingeschraenkt ? "Ihre Einsätze" : "Einsätze je Monteur"}; gelb = vorgemerkt (wartet auf Freigabe), blau = bestätigt.</p>
    ${wir.eingeschraenkt && !p.ressourcen.length ? `<p class="hinweis">Ihrem Konto ist noch kein Monteur zugeordnet. Der Inhaber trägt Ihren Anmeldenamen (${esc(wir.name)}) unter „Monteure“ ein.</p>` : ""}
    <div class="tabelle-rahmen"><div class="plantafel" style="grid-template-columns: 180px repeat(${p.tage}, minmax(150px, 1fr)); min-width: ${180 + p.tage * 150}px">
      <div class="pt-zelle pt-kopf">Monteur</div>${tage.map((d) => { const f = (p.feiertage || {})[TAGESSCHLUESSEL.format(d)]; return `<div class="pt-zelle pt-kopf${f ? " pt-feiertag" : ""}">${esc(WOCHENTAG.format(d))}${f ? `<br><small>${esc(f)}</small>` : ""}</div>`; }).join("")}
      ${p.ressourcen.map((r) => `<div class="pt-zelle"><strong>${esc(r.name.split(" (")[0])}</strong><br><small>${esc(r.qualifikationen.join(", "))}</small><br><small>${esc(r.arbeitsbeginn)}–${esc(r.arbeitsende)}</small></div>
        ${tage.map((d) => `<div class="pt-zelle">${(r.abwesenheiten || []).filter((a) => a.von <= TAGESSCHLUESSEL.format(d) && a.bis >= TAGESSCHLUESSEL.format(d)).map((a) => `<div class="einsatz abwesend">${esc(ABWESENHEIT_TEXT[a.art] || a.art)}</div>`).join("")}${r.termine.filter((t) => gleicherTag(t.beginn, d)).map((t) => `
          <div class="einsatz ${esc(t.status)} ${t.dringlichkeit === "notdienst" ? "notdienst" : ""}" data-fall="${esc(t.servicefall_id)}">
          <strong>${esc(UHRZEIT.format(new Date(t.beginn)))}–${esc(UHRZEIT.format(new Date(t.ende)))}</strong> ${esc(t.kunde)}<br>${esc(t.anliegen)}</div>`).join("")}</div>`).join("")}`).join("")}
    </div></div>`;
  ziel.querySelectorAll("[data-fall]").forEach((k) => k.addEventListener("click", () => fallDetail(k.dataset.fall)));
  const leiste = document.createElement("div"); leiste.className = "karte"; leiste.style.margin = "16px 0";
  leiste.innerHTML = `<div class="zeile"><label>Tour planen für <select id="pt-tour-res">${p.ressourcen.map((r) => `<option value="${esc(r.ressource_id)}">${esc(r.name.split(" (")[0])}</option>`).join("")}</select></label>
    <label>Tag <input id="pt-tour-tag" type="date" value="${esc(p.von)}"></label><button class="leise" id="pt-tour">Tour anzeigen</button>
    ${wir.rechte.includes("bearbeiten") ? `<button class="leise" id="pt-kalender">Kalender-Abo (ICS)</button>` : ""}</div><div id="pt-tour-ergebnis"></div>`;
  ziel.append(leiste);
  if (wir.rechte.includes("bearbeiten")) {
    const opt = document.createElement("div"); opt.className = "karte"; opt.style.margin = "16px 0";
    opt.innerHTML = `<h2>Tagesplan optimieren</h2><p class="unterzeile">Alle Monteure eines Tages als Tourenplanungsproblem mit Zeitfenstern (VRPTW): Fahrzeit, Verspätung gegenüber zugesagten Zeiten, Überstunden und Qualifikationen; lokale Suche mit Simulated Annealing. Bestätigte Termine bleiben innerhalb der Toleranz um die zugesagte Uhrzeit.</p>
      <div class="zeile"><label>Tag <input id="pt-opt-tag" type="date" value="${esc(p.von)}"></label><label>Toleranz bestätigter Termine <select id="pt-opt-tol"><option value="0">0 min (fix)</option><option value="30">± 30 min</option><option value="60">± 60 min</option><option value="120">± 2 h</option></select></label>
      <button class="leise" id="pt-opt">Vorschlag berechnen</button></div><div id="pt-opt-ergebnis"></div>`;
    ziel.append(opt);
    $("#pt-opt", opt).addEventListener("click", () => planOptimieren($("#pt-opt-tag", opt).value, $("#pt-opt-tol", opt).value, $("#pt-opt-ergebnis", opt)));
  }
  $("#pt-tour", leiste).addEventListener("click", () => tourAnzeigen($("#pt-tour-res", leiste).value, $("#pt-tour-tag", leiste).value, $("#pt-tour-ergebnis", leiste), wir.rechte.includes("bearbeiten")));
  $("#pt-kalender", leiste)?.addEventListener("click", async () => {
    const l = await api("/kalender/links");
    detail(`<h2>Kalender-Abo</h2><p class="unterzeile">Adresse im Telefon als abonnierten Kalender eintragen (iPhone: Einstellungen → Kalender → Accounts → Kalenderabo; Android: Google Kalender „Per URL hinzufügen“ oder DAVx⁵; Outlook: Kalender abonnieren). Termine, Abwesenheiten und Bereitschaften erscheinen mit Adresse, Rufnummer und 30-Minuten-Erinnerung; Aktualisierung alle 30 Minuten. Die Adresse ist ein Geheimnis: nur an den jeweiligen Monteur geben.</p>
      <table><tbody><tr><td><strong>Betrieb (alle Monteure)</strong></td><td><input readonly value="${esc(l.betrieb)}" onclick="this.select()"></td></tr>
      ${l.monteure.map((m) => `<tr><td>${esc(m.name.split(" (")[0])}</td><td><input readonly value="${esc(m.url)}" onclick="this.select()"></td></tr>`).join("")}</tbody></table>`);
  });
  if (wir.rechte.includes("bearbeiten")) await abwesenheitenVerwalten(ziel, await api("/ressourcen"));
  if (wir.rechte.includes("verwalten")) await monteureVerwalten(ziel);
}

async function tourAnzeigen(ressourceId, tag_, ziel, darf) {
  ziel.innerHTML = "<p>Fahrzeiten werden berechnet …</p>";
  const t = (await api(`/tour?ressource_id=${encodeURIComponent(ressourceId)}${tag_ ? `&tag=${tag_}` : ""}`)).touren[0];
  if (!t) { ziel.innerHTML = "<p>Kein Monteur.</p>"; return; }
  if (!t.stopps.length) { ziel.innerHTML = `<p class="hinweis">Keine Termine am ${esc(t.tag)}.</p>`; return; }
  const zeile = (st, i) => `<tr><td>${i + 1}</td><td>${esc(UHRZEIT.format(new Date(st.beginn)))}–${esc(UHRZEIT.format(new Date(st.ende)))}</td><td>${esc(st.kunde)}<br><small>${esc(st.adresse)}</small></td><td>${esc(st.anliegen)}</td><td style="text-align:right">${st.fahrzeit_minuten != null ? `${st.fahrzeit_minuten} min · ${st.km} km` : tag("ohne Koordinaten", "neutral")}</td></tr>`;
  ziel.innerHTML = `<p class="unterzeile" style="margin:10px 0 6px">${esc(t.name.split(" (")[0])}, ${esc(t.tag)}: ${t.ist.minuten} min Fahrt, ${t.ist.km} km ab Betrieb${t.quelle === "luftlinie" ? " (Luftlinie, OSRM nicht erreichbar)" : ""}${t.hinweis ? ` · ${esc(t.hinweis)}` : ""}</p>
    <table><thead><tr><th>#</th><th>Zeit</th><th>Kunde</th><th>Anliegen</th><th style="text-align:right">Anfahrt</th></tr></thead><tbody>${t.stopps.map(zeile).join("")}</tbody></table>
    ${t.vorschlag ? `<p style="margin-top:10px"><strong>Vorschlag:</strong> ${t.vorschlag.reihenfolge.map((id) => esc(t.stopps.find((s) => s.termin_id === id).kunde)).join(" → ")} · ${t.vorschlag.minuten} min, ${t.vorschlag.km} km${t.vorschlag.ersparnis_minuten ? ` · spart ${t.vorschlag.ersparnis_minuten} min` : " · keine Verbesserung"}
      ${darf && t.vorschlag.ersparnis_minuten ? `<button class="leise" id="pt-tour-umsetzen" style="margin-left:8px">Reihenfolge übernehmen</button>` : ""}</p><p id="pt-tour-status"></p>` : ""}`;
  $("#pt-tour-umsetzen", ziel)?.addEventListener("click", async () => {
    try { const e = await api("/tour/umsetzen", { methode: "POST", daten: { ressource_id: ressourceId, tag: tag_ || null, reihenfolge: t.vorschlag.reihenfolge } });
      $("#pt-tour-status", ziel).innerHTML = `<span class="hinweis">${e.verschoben.length} Termine neu getaktet. Bestätigte Termine mit neuer Uhrzeit dem Kunden mitteilen.</span>`; setTimeout(zeigen, 1200); }
    catch (e) { $("#pt-tour-status", ziel).innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
}

async function planOptimieren(tag_, toleranz, ziel) {
  ziel.innerHTML = "<p>Plan wird berechnet …</p>";
  let v;
  try { v = await api(`/plantafel/optimieren?tag=${encodeURIComponent(tag_)}&toleranz_minuten=${encodeURIComponent(toleranz)}`); } catch (e) { ziel.innerHTML = `<p class="fehler">${esc(e.message)}</p>`; return; }
  if (v.hinweis) { ziel.innerHTML = `<p class="hinweis">${esc(v.hinweis)}</p>`; return; }
  const kz = (k) => `${k.fahrzeit_minuten} min Fahrt · ${k.verspaetung_minuten} min Verspätung · ${k.ueberstunden_minuten} min Überstunden${k.qualifikationsverstoesse ? ` · ${k.qualifikationsverstoesse} Qualifikationsverstöße` : ""}`;
  ziel.innerHTML = `<p style="margin:10px 0 6px"><strong>Heute:</strong> ${esc(kz(v.ist))}<br><strong>Vorschlag:</strong> ${esc(kz(v.vorschlag))}${v.quelle === "luftlinie" ? " <small>(Luftlinie, OSRM nicht erreichbar)</small>" : ""}</p>
    <p class="hinweis">${esc(v.begruendung)}</p>
    ${v.routen.filter((r) => r.stopps.length).map((r) => `<h3 style="margin:10px 0 4px">${esc(r.name.split(" (")[0])} <small>${r.fahrzeit_minuten} min Fahrt</small></h3>
      <table><tbody>${r.stopps.map((s) => `<tr${s.verschoben ? ' class="hervor"' : ""}><td>${esc(s.uhrzeit)}</td><td>${esc(s.kunde)}</td><td>${tag(s.status)}</td>
        <td><small>${s.verschoben ? `vorher ${esc(UHRZEIT.format(new Date(s.vorher)))}${s.ressource_vorher !== r.ressource_id ? ", anderer Monteur" : ""}` : ""}</small></td></tr>`).join("")}</tbody></table>`).join("")}
    ${v.unplanbar.length ? `<p class="fehler">Nicht planbar: ${v.unplanbar.map((u) => esc(`${u.kunde} (${u.grund})`)).join(", ")}</p>` : ""}
    ${v.aenderungen.length ? `<div class="aktionen" style="margin-top:10px"><button id="pt-opt-umsetzen">Plan übernehmen (${v.aenderungen.length} Termine)</button><span id="pt-opt-status"></span></div>` : ""}`;
  $("#pt-opt-umsetzen", ziel)?.addEventListener("click", async () => {
    try { const e = await api("/plantafel/optimieren/umsetzen", { methode: "POST", daten: { plan: v } });
      $("#pt-opt-status", ziel).innerHTML = `<span class="hinweis">${e.verschoben.length} Termine umgeplant${e.neue_bestaetigungen ? `, ${e.neue_bestaetigungen} neue Terminbestätigungen in der Outbox` : ""}.</span>`; setTimeout(zeigen, 1200); }
    catch (e) { $("#pt-opt-status", ziel).innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
}

// --- Warteliste --------------------------------------------------------------------------------
async function wartelisteAnsicht(ziel) {
  const w = await api("/warteliste?tage=7");
  const STATUS_W = { offen: "offen", angeboten: "angeboten", gebucht: "gebucht" };
  ziel.innerHTML = `<h1>Warteliste</h1><p class="unterzeile">Wer früher dran sein möchte, steht hier. Wird ein Termin abgesagt, bekommt der bestpassende Eintrag die Lücke automatisch angeboten (Priorität, Wartezeit, Passung, Fahrweg); die Reservierung verfällt nach Frist und wandert weiter.</p>
    <div class="raster">
      <div class="karte"><h2>Einträge (${w.offen} offen)</h2><div class="tabelle-rahmen"><table><thead><tr><th>Kunde</th><th>Anliegen</th><th>Wunsch</th><th>Status</th><th></th></tr></thead><tbody>
        ${w.eintraege.map((e) => `<tr><td class="klickbar" data-fall="${esc(e.servicefall_id)}">${esc(e.kunde)}<br><small>wartet ${e.wartetage} Tage · Prio ${e.prioritaet}${e.angebote_anzahl ? ` · ${e.angebote_anzahl}× verfallen` : ""}</small></td>
          <td>${esc(e.anliegen)}<br><small>${e.dauer_minuten} min${e.qualifikation ? `, ${esc(e.qualifikation)}` : ""}</small></td>
          <td>${esc(e.wunsch_von)} – ${esc(e.wunsch_bis)}<br><small>${esc(e.tageszeiten.join(", ") || "ganztags")}</small></td>
          <td>${tag(STATUS_W[e.status] || e.status, e.status === "angeboten" ? "offen" : (e.status === "gebucht" ? "aktuell" : "neutral"))}${e.angebot ? `<br><small>${esc(DATUMZEIT.format(new Date(e.angebot.beginn)))}, Frist ${esc(UHRZEIT.format(new Date(e.angebot.frist)))}</small>` : ""}</td>
          <td>${e.status !== "gebucht" ? `<button class="leise" data-zurueck="${esc(e.id)}">Zurückziehen</button>` : ""}</td></tr>`).join("") || "<tr><td colspan='5'>Niemand wartet.</td></tr>"}</tbody></table></div></div>
      <div class="karte"><h2>Lücken der nächsten 7 Tage</h2><div class="tabelle-rahmen"><table><thead><tr><th>Monteur</th><th>Zeit</th><th>Dauer</th><th>Bester Kandidat</th><th></th></tr></thead><tbody>
        ${w.luecken.slice(0, 40).map((l) => `<tr><td>${esc(l.name.split(" (")[0])}</td><td>${esc(DATUMZEIT.format(new Date(l.beginn)))}–${esc(UHRZEIT.format(new Date(l.ende)))}</td><td>${l.minuten} min</td>
          <td>${l.bester ? `${esc(l.bester.kunde)} <small>(${l.bester.score}: ${esc(l.bester.gruende.join(", "))})</small>` : `<small>${l.kandidaten} Kandidaten</small>`}</td>
          <td>${l.bester ? `<button class="leise" data-anbieten="${esc(l.ressource_id)}" data-beginn="${esc(l.beginn)}" data-ende="${esc(l.ende)}">Anbieten</button>` : ""}</td></tr>`).join("") || "<tr><td colspan='5'>Keine Lücken.</td></tr>"}</tbody></table></div></div>
    </div>`;
  ziel.querySelectorAll("[data-fall]").forEach((z) => z.addEventListener("click", () => fallDetail(z.dataset.fall)));
  ziel.querySelectorAll("[data-zurueck]").forEach((b) => b.addEventListener("click", async () => { try { await api(`/warteliste/${b.dataset.zurueck}/zurueckziehen`, { methode: "POST" }); zeigen(); } catch (e) { melden(e.message); } }));
  ziel.querySelectorAll("[data-anbieten]").forEach((b) => b.addEventListener("click", async () => {
    try { const r = await api(`/warteliste/anbieten?ressource_id=${encodeURIComponent(b.dataset.anbieten)}&beginn=${encodeURIComponent(b.dataset.beginn)}&ende=${encodeURIComponent(b.dataset.ende)}`, { methode: "POST" });
      melden(r.hinweis || `Angeboten an ${r.kunde}, Frist ${DATUMZEIT.format(new Date(r.frist))}`); zeigen(); } catch (e) { melden(e.message); }
  }));
}

// Inhaber: Anmeldename je Monteur, damit die Monteur-Rolle nur eigene Einsätze sieht
async function monteureVerwalten(ziel) {
  const liste = await api("/ressourcen");
  const bereich = document.createElement("div");
  const katalog = BRANCHE?.sachmittel_katalog || {};
  const personen = liste.filter((r) => r.art !== "sachmittel"), mittel = liste.filter((r) => r.art === "sachmittel");
  bereich.innerHTML = `<h2 style="margin-top:24px">${esc(wort("ressourcen", "Monteure"))} und Sachmittel</h2>
    <div class="karte"><div class="tabelle-rahmen"><table><thead><tr><th>${esc(wort("ressource", "Monteur"))}</th><th>Qualifikationen</th><th>Anmeldename (Keycloak)</th><th></th></tr></thead><tbody>
    ${personen.map((r) => `<tr><td>${esc(r.name)}${r.aktiv ? "" : ` ${tag("inaktiv", "neutral")}`}</td><td>${esc(r.qualifikationen.join(", "))}</td>
      <td><input data-benutzer="${esc(r.id)}" value="${esc(r.benutzer || "")}" placeholder="z. B. m.arndt" autocomplete="off"></td>
      <td><button class="leise" data-speichern="${esc(r.id)}">Speichern</button></td></tr>`).join("") || `<tr><td colspan="4">${leer("Noch niemand angelegt.")}</td></tr>`}</tbody></table></div>
    <form id="res-neu" class="zeile" style="margin-top:10px"><input name="name" placeholder="Name" required><input name="qualifikationen" placeholder="Qualifikationen, kommagetrennt"><button type="submit" class="leise">${esc(wort("ressource", "Monteur"))} anlegen</button></form>
    ${BRANCHE?.sachmittel?.length || mittel.length ? `<h3 style="margin-top:16px">Sachmittel (Hebebühne, Arbeitsplatz, Raum, Ersatzfahrzeug …)</h3><p class="unterzeile">Ein Termin belegt neben der Person auch ein Sachmittel; die Disposition bietet nur Fenster an, in denen eines frei ist.</p>
      <table><tbody>${mittel.map((r) => `<tr><td>${esc(r.name)}</td><td>${esc(katalog[r.sachmittel_typ] || r.sachmittel_typ || "")}</td><td>${r.aktiv ? "" : tag("inaktiv", "neutral")}</td></tr>`).join("") || `<tr><td colspan="3">${leer("Noch kein Sachmittel angelegt.")}</td></tr>`}</tbody></table>
      <form id="sm-neu" class="zeile" style="margin-top:10px"><input name="name" placeholder="z. B. Bühne 1" required><select name="sachmittel_typ">${Object.entries(katalog).map(([k, b]) => `<option value="${esc(k)}" ${(BRANCHE.sachmittel || [])[0] === k ? "selected" : ""}>${esc(b)}</option>`).join("")}</select><button type="submit" class="leise">Sachmittel anlegen</button></form>` : ""}</div>`;
  ziel.append(bereich);
  $("#res-neu", bereich).addEventListener("submit", async (ev) => {
    ev.preventDefault(); const fd = new FormData(ev.target);
    try { await api("/ressourcen", { methode: "POST", daten: { name: fd.get("name"), qualifikationen: String(fd.get("qualifikationen") || "").split(",").map((x) => x.trim()).filter(Boolean) } }); melden("Angelegt", "ok"); zeigen(); } catch (e) { melden(e.message); }
  });
  $("#sm-neu", bereich)?.addEventListener("submit", async (ev) => {
    ev.preventDefault(); const fd = new FormData(ev.target);
    try { await api("/ressourcen", { methode: "POST", daten: { name: fd.get("name"), art: "sachmittel", sachmittel_typ: fd.get("sachmittel_typ"), arbeitstage: [0, 1, 2, 3, 4, 5, 6], arbeitsbeginn: "00:00", arbeitsende: "23:59" } }); melden("Angelegt", "ok"); zeigen(); } catch (e) { melden(e.message); }
  });
  bereich.querySelectorAll("[data-speichern]").forEach((b) => b.addEventListener("click", async () => {
    const id = b.dataset.speichern, wert = bereich.querySelector(`[data-benutzer="${id}"]`).value.trim();
    b.disabled = true;
    try { await api(`/ressourcen/${id}`, { methode: "PATCH", daten: { benutzer: wert || null } }); b.textContent = "Gespeichert"; }
    catch (e) { melden(e.message); } finally { b.disabled = false; }
  }));
}

// --- Kunden und Anlagen ----------------------------------------------------------------------
async function kunden(ziel) {
  const wir = await ich();
  const nurOffen = location.hash.includes("/unvollstaendig");
  ziel.innerHTML = `<h1>${esc(wort("kartei", "Kunden & Anlagen"))}</h1><p class="unterzeile">Kanonisches Datenmodell: Kunde → Objekt → Anlage; Dubletten über die Rufnummer. Die Spalte „Vollständig“ zeigt, ob alles für den automatischen Betrieb sitzt (Rufnummer, E-Mail, Anschrift, Koordinaten, Objekt, Anlage, Einwilligung).</p>
    <div class="karte" style="margin-bottom:16px"><div class="zeile"><label>Suche <input id="k-suche" placeholder="Name, Ort oder E-Mail"></label>
      <label><span><input type="checkbox" id="k-offen" ${nurOffen ? "checked" : ""}> nur unvollständige</span></label>
      ${wir.rechte.includes("bearbeiten") ? `<button class="leise" id="k-geo" title="Adressen ohne Koordinaten über den Geokodierungsdienst auflösen">Adressen geokodieren</button>` : ""}
      ${wir.rechte.includes("verwalten") ? `<button class="leise" id="k-dubletten" title="Phonetischer und adressbasierter Dublettenabgleich">Dubletten prüfen</button>` : ""}
      ${wir.rechte.includes("bearbeiten") ? `<button id="k-neu">Kunde anlegen</button>` : ""}</div><p id="k-status"></p></div>
    <div class="karte"><div class="tabelle-rahmen"><table><thead><tr><th>Name</th><th>Ort</th><th>Telefon</th><th>Vertrag</th><th>Vollständig</th></tr></thead><tbody id="k-liste"></tbody></table></div></div>`;
  const laden = async () => {
    const q = $("#k-suche").value.trim();
    const [liste, voll] = await Promise.all([api(`/kunden?limit=200${q ? `&q=${encodeURIComponent(q)}` : ""}`), api("/kunden/vollstaendigkeit")]);
    const luecken = Object.fromEntries(voll.unvollstaendig.map((u) => [u.id, u]));
    const zeilen = $("#k-offen").checked ? liste.filter((k) => luecken[k.id]) : liste;
    $("#k-liste").innerHTML = zeilen.map((k) => { const l = luecken[k.id]; return `<tr class="klickbar" data-kunde="${esc(k.id)}"><td>${esc(k.name)}</td><td>${esc(k.ort || "")}</td><td>${esc(k.telefon || "")}</td><td>${k.vertragsstatus === "wartungsvertrag" ? tag("Wartungsvertrag", "A") : ""}</td>
      <td>${l ? `${tag(`${Math.round(l.quote * 100)} %`, "offen")} <small>${esc(l.fehlend.join(", "))}</small>` : tag("vollständig", "aktuell")}</td></tr>`; }).join("") || `<tr><td colspan="5">Keine Treffer.</td></tr>`;
    $("#k-liste").querySelectorAll("[data-kunde]").forEach((z) => z.addEventListener("click", () => kundeDetail(z.dataset.kunde)));
  };
  $("#k-dubletten")?.addEventListener("click", async () => {
    const d = await api("/dubletten");
    const w = detail(`<h2>Dubletten</h2><p class="unterzeile">Kölner Phonetik, Jaro-Winkler, Rufnummer, E-Mail und normalisierte Adresse; ab 0,85 „sicher“. Beim Zusammenführen bleibt der ältere Datensatz, alle Vorgänge, Objekte, Rechnungen und Verträge wandern mit; die Quelle wird im Ereignisprotokoll gesichert.</p>
      ${d.length ? `<table><thead><tr><th>Score</th><th>Bleibt</th><th>Wird zusammengeführt</th><th>Gründe</th><th></th></tr></thead><tbody>${d.map((x, i) => `<tr><td>${tag(x.score, x.sicher ? "aktuell" : "offen")}</td>
        <td>${esc(x.ziel.name)}<br><small>${esc(x.ziel.telefon || "")} ${esc(x.ziel.strasse || "")}</small></td><td>${esc(x.quelle.name)}<br><small>${esc(x.quelle.telefon || "")} ${esc(x.quelle.strasse || "")}</small></td>
        <td><small>${esc(x.gruende.join(", "))}</small></td><td><button class="leise" data-merge="${i}">Zusammenführen</button></td></tr>`).join("")}</tbody></table>` : "<p>Keine Dubletten gefunden.</p>"}<p id="k-dub-status"></p>`);
    w.querySelectorAll("[data-merge]").forEach((b) => b.addEventListener("click", async () => {
      const x = d[Number(b.dataset.merge)];
      if (!confirm(`„${x.quelle.name}“ in „${x.ziel.name}“ zusammenführen?`)) return;
      try { const r = await api(`/kunden/${x.ziel.id}/zusammenfuehren`, { methode: "POST", daten: { quelle_id: x.quelle.id } });
        $("#k-dub-status", w).innerHTML = `<span class="hinweis">Zusammengeführt: ${esc(Object.entries(r.umgehaengt).map(([k, v]) => `${k.split(".")[0]} ${v}`).join(", ") || "keine Verknüpfungen")}.</span>`; b.closest("tr").remove(); laden(); }
      catch (e) { $("#k-dub-status", w).innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
    }));
  });
  let warte; $("#k-suche").addEventListener("input", () => { clearTimeout(warte); warte = setTimeout(laden, 250); });
  $("#k-offen").addEventListener("change", laden);
  $("#k-geo")?.addEventListener("click", async () => {
    $("#k-status").textContent = "Geokodierung läuft (höchstens eine Anfrage je Sekunde) …";
    try { const e = await api("/kunden/geokodieren?limit=100", { methode: "POST" }); $("#k-status").innerHTML = `<span class="hinweis">${e.gesetzt} Koordinaten gesetzt, ${e.ohne_treffer} ohne Treffer, ${e.fehler} Fehler${e.uebrig ? `, ${e.uebrig} verbleiben für den nächsten Lauf` : ""}.</span>`; laden(); }
    catch (e) { $("#k-status").innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
  $("#k-neu")?.addEventListener("click", () => kundeFormular(null, laden));
  await laden();
}
const KUNDE_FELDER = [["name", "Name"], ["telefon", "Rufnummer"], ["email", "E-Mail"], ["strasse", "Straße und Hausnummer"], ["plz", "PLZ"], ["ort", "Ort"], ["lat", "Breitengrad (optional, sonst Geokodierung)"], ["lon", "Längengrad"]];
const EINWILLIGUNGEN = [["email", "E-Mail (Terminbestätigung, Angebote, Rechnungen)"], ["sms", "SMS (Terminerinnerung)"], ["whatsapp", "WhatsApp (Terminbestätigung, Nachrichten)"], ["kontakt", "telefonische Kontaktaufnahme durch den Betrieb"], ["aufzeichnung", "Gesprächsaufzeichnung durch den Sprachagenten"], ["bewertung", "Bewertungsanfrage nach dem Einsatz"], ["gesundheitsdaten", "Gesundheitsdaten in der Kartei (Allergien, Kontraindikationen, Befunde; Art. 9 DSGVO)"]];
function kundeFormular(kunde, danach) {
  const k = kunde || {};
  const wurzel = detail(`<h2>${kunde ? "Kunde bearbeiten" : "Kunde anlegen"}</h2><form id="kf"><div class="raster">
    ${KUNDE_FELDER.map(([f, b]) => `<label>${esc(b)} <input name="${f}" value="${esc(k[f] ?? "")}" ${f === "name" ? "required" : ""}></label>`).join("")}
    <label>Vertragsstatus <select name="vertragsstatus"><option value="ohne" ${k.vertragsstatus !== "wartungsvertrag" ? "selected" : ""}>ohne Vertrag</option><option value="wartungsvertrag" ${k.vertragsstatus === "wartungsvertrag" ? "selected" : ""}>Wartungsvertrag</option></select></label>
    <label>Notiz <input name="notiz" value="${esc(k.notiz ?? "")}"></label></div>
    <p class="fehler" id="kf-fehler" hidden></p><div class="aktionen"><button type="submit">Speichern</button></div></form>`);
  $("#kf", wurzel).addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target); const daten = {};
    for (const [f, v] of fd.entries()) daten[f] = v === "" ? null : (["lat", "lon"].includes(f) ? Number(String(v).replace(",", ".")) : v);
    if ((daten.lat == null) !== (daten.lon == null)) { $("#kf-fehler", wurzel).textContent = "Breiten- und Längengrad nur gemeinsam angeben."; $("#kf-fehler", wurzel).hidden = false; return; }
    try {
      const r = kunde ? await api(`/kunden/${kunde.id}`, { methode: "PATCH", daten }) : await api("/kunden", { methode: "POST", daten: { ...daten, vertragsstatus: daten.vertragsstatus || "ohne" } });
      $("#detail").close(); danach ? danach() : kundeDetail(r.id);
    } catch (e) { $("#kf-fehler", wurzel).textContent = e.message; $("#kf-fehler", wurzel).hidden = false; }
  });
}
async function kundeDetail(id) {
  const [d, wir, typen] = await Promise.all([api(`/kunden/${id}`), ich(), api("/anlagentypen")]);
  const darf = wir.rechte.includes("bearbeiten");
  const anlagen = d.objekte.flatMap((o) => o.anlagen);
  const zwillinge = await Promise.all(anlagen.map((a) => api(`/twin/anlagen/${a.id}`)));
  const v = d.vollstaendigkeit, ew = d.kunde.einwilligungen || {}, k = d.kunde;
  const wurzel = detail(`<div class="zeile" style="justify-content:space-between"><h2 style="margin:0">${esc(d.kunde.name)}</h2>${darf ? `<button class="leise" id="kd-bearbeiten">Bearbeiten</button>` : ""}</div>
    <p>${esc(d.kunde.strasse || "–")}, ${esc(d.kunde.plz || "")} ${esc(d.kunde.ort || "")} · ${esc(d.kunde.telefon || "ohne Rufnummer")} · ${esc(d.kunde.email || "ohne E-Mail")}${d.kunde.lat != null ? ` · <small>${d.kunde.lat.toFixed(4)}, ${d.kunde.lon.toFixed(4)} (${esc(d.kunde.geo_quelle || "")})</small>` : ""}${d.kunde.notiz ? `<br><small>${esc(d.kunde.notiz)}</small>` : ""}</p>
    <div class="raster"><div>
      <h3>Für den Betrieb sitzt alles? ${v.vollstaendig ? tag("ja", "aktuell") : tag(`${Math.round(v.quote * 100)} %`, "offen")}</h3>
      ${v.vollstaendig ? "<p>Alle Pflichtangaben vorhanden.</p>" : `<ul class="pruefliste">${v.fehlend.map((f) => `<li>${esc(f.text)}</li>`).join("")}</ul>`}
    </div><div>
      <h3>Einwilligungen</h3><form id="kd-ew">${EINWILLIGUNGEN.map(([a, b]) => `<label><span><input type="checkbox" name="${a}" ${ew[a] ? "checked" : ""} ${darf ? "" : "disabled"}> ${esc(b)}${ew[a] ? ` <small>(seit ${esc(zeit(ew[a]))})</small>` : ""}</span></label>`).join("")}</form>
    </div></div>
    ${BRANCHE?.kartei?.length ? `<h3>${esc(wort("kartei", "Kundenkartei"))} · ${esc(BRANCHE.bezeichnung)}</h3><form id="kd-kartei" class="raster">
      ${BRANCHE.kartei.map((f) => `<label>${esc(f.titel)}${f.sensibel ? ' <small title="Gesundheitsdatum, nur mit Einwilligung">🔒</small>' : ""}${f.hinweis ? `<br><small>${esc(f.hinweis)}</small>` : ""}
        ${f.art === "ja_nein" ? `<select name="${esc(f.schluessel)}" ${darf && (!f.sensibel || ew.gesundheitsdaten) ? "" : "disabled"}><option value="">–</option><option value="ja" ${k.kartei?.[f.schluessel] === "ja" ? "selected" : ""}>ja</option><option value="nein" ${k.kartei?.[f.schluessel] === "nein" ? "selected" : ""}>nein</option></select>`
        : `<input name="${esc(f.schluessel)}" ${f.art === "datum" ? 'type="date"' : ""} value="${esc(k.kartei?.[f.schluessel] ?? "")}" ${darf && (!f.sensibel || ew.gesundheitsdaten) ? "" : "disabled"}>`}</label>`).join("")}
      ${darf ? `<div class="aktionen" style="align-self:end"><button type="submit" class="leise">Kartei speichern</button></div>` : ""}</form>
      ${BRANCHE.gesundheitsdaten && !ew.gesundheitsdaten ? `<p class="unterzeile">🔒-Felder sind Gesundheitsdaten und erst nach Einwilligung „Gesundheitsdaten“ beschreibbar.</p>` : ""}` : ""}
    ${BRANCHE?.begriffe?.anlagen === false ? "" : `<h3>Objekte und Anlagen (Digital Twin)</h3>
    ${d.objekte.map((o) => `<p style="margin:8px 0 4px"><strong>${esc(o.bezeichnung)}</strong> · ${esc(o.strasse || "")} ${esc(o.plz || "")} ${esc(o.ort || "")}${o.gebaeudetyp ? ` · ${esc(o.gebaeudetyp)}` : ""} ${darf ? `<button class="leise" data-anlage-neu="${esc(o.id)}" style="padding:2px 8px">Anlage hinzufügen</button>` : ""}</p>`).join("") || "<p>Noch kein Objekt erfasst.</p>"}
    ${darf ? `<p><button class="leise" id="kd-objekt-neu">Objekt hinzufügen</button></p>` : ""}
    <table><thead><tr><th>Anlage</th><th>Baujahr</th><th>Letzte Wartung</th><th>Status</th><th>Qualifikation</th></tr></thead><tbody>
    ${zwillinge.map((z) => `<tr><td>${esc(z.anlage.bezeichnung)}${z.anlage.hersteller ? ` · ${esc(z.anlage.hersteller)}` : ""}<br><small><code>shk:${esc(z.anlage.ontologieklasse)}</code></small></td>
      <td>${esc(z.anlage.baujahr || "–")}</td><td>${esc(z.wartung.letzte || "nie")}</td><td>${tag(z.wartung.status)}</td><td>${esc(z.wartung.benoetigte_qualifikation || "–")}</td></tr>`).join("") || `<tr><td colspan="5">Keine Anlagen erfasst.</td></tr>`}
    </tbody></table>`}
    <div id="kd-formular"></div>
    <h3 style="margin-top:16px">${esc(wort("vorgaenge", "Vorgänge"))}</h3><table><tbody>${d.servicefaelle.map((f) => `<tr class="klickbar" data-fall="${esc(f.id)}"><td>${esc(f.anliegen)}</td><td>${tag(f.dringlichkeit)}</td><td>${esc(STATUS_TEXT[f.status])}</td><td>${zeit(f.erstellt)}</td></tr>`).join("") || "<tr><td>Keine.</td></tr>"}</tbody></table>`);
  wurzel.querySelectorAll("[data-fall]").forEach((z) => z.addEventListener("click", () => fallDetail(z.dataset.fall)));
  $("#kd-bearbeiten", wurzel)?.addEventListener("click", () => kundeFormular(d.kunde));
  $("#kd-ew", wurzel).addEventListener("change", async (ev) => {
    try { await api(`/kunden/${id}/einwilligungen`, { methode: "PUT", daten: { [ev.target.name]: ev.target.checked } }); kundeDetail(id); } catch (e) { melden(e.message); }
  });
  $("#kd-kartei", wurzel)?.addEventListener("submit", async (ev) => {
    ev.preventDefault(); const felder = {};
    for (const [key, v2] of new FormData(ev.target).entries()) if (v2 !== "") felder[key] = v2;
    try { await api(`/kunden/${id}/kartei`, { methode: "PUT", daten: { felder } }); kundeDetail(id); } catch (e) { melden(e.message); }
  });
  $("#kd-objekt-neu", wurzel)?.addEventListener("click", () => {
    $("#kd-formular", wurzel).innerHTML = `<form id="kd-of" class="karte"><div class="raster"><label>Bezeichnung <input name="bezeichnung" value="Hauptobjekt"></label><label>Straße <input name="strasse" value="${esc(d.kunde.strasse || "")}"></label><label>PLZ <input name="plz" value="${esc(d.kunde.plz || "")}"></label><label>Ort <input name="ort" value="${esc(d.kunde.ort || "")}"></label>
      <label>Gebäudetyp <select name="gebaeudetyp"><option value="">–</option>${["Einfamilienhaus", "Mehrfamilienhaus", "Gewerbe"].map((g) => `<option>${g}</option>`).join("")}</select></label></div><div class="aktionen"><button type="submit">Objekt anlegen</button></div></form>`;
    $("#kd-of", wurzel).addEventListener("submit", async (ev) => { ev.preventDefault(); const daten = Object.fromEntries([...new FormData(ev.target).entries()].filter(([, v]) => v !== "")); try { await api(`/kunden/${id}/objekte`, { methode: "POST", daten }); kundeDetail(id); } catch (e) { melden(e.message); } });
  });
  wurzel.querySelectorAll("[data-anlage-neu]").forEach((b) => b.addEventListener("click", () => {
    const erlaubt = BRANCHE?.anlagentypen?.length ? Object.entries(typen).filter(([k]) => BRANCHE.anlagentypen.includes(k)) : Object.entries(typen);
    const karteiFelder = (typ) => (BRANCHE?.anlagenkartei?.[typ] || []).map((f) => `<label data-kartei>${esc(f.titel)}${f.hinweis ? `<br><small>${esc(f.hinweis)}</small>` : ""} <input name="merkmale.${esc(f.schluessel)}" ${f.art === "datum" ? 'type="date"' : f.art === "zahl" ? 'type="number" step="any"' : ""}></label>`).join("");
    $("#kd-formular", wurzel).innerHTML = `<form id="kd-af" class="karte"><div class="raster"><label>${erlaubt.length === 1 ? esc(erlaubt[0][1].bezeichnung) : "Anlagentyp"} <select name="anlagentyp" ${erlaubt.length === 1 ? "hidden" : ""}>${erlaubt.map(([k, t]) => `<option value="${k}">${esc(t.bezeichnung)}</option>`).join("")}</select></label>
      <label>Hersteller <input name="hersteller"></label><label>Modell <input name="modell"></label><label>Baujahr <input name="baujahr" type="number" min="1950" max="2100"></label><label>Letzte Wartung <input name="letzte_wartung" type="date"></label><div id="kd-af-kartei" class="raster" style="grid-column:1/-1">${karteiFelder(erlaubt[0]?.[0])}</div></div><div class="aktionen"><button type="submit">${erlaubt.length === 1 ? esc(erlaubt[0][1].bezeichnung) : "Anlage"} anlegen</button></div></form>`;
    $("#kd-af select[name=anlagentyp]", wurzel).addEventListener("change", (ev) => { $("#kd-af-kartei", wurzel).innerHTML = karteiFelder(ev.target.value); });
    $("#kd-af", wurzel).addEventListener("submit", async (ev) => {
      ev.preventDefault(); const daten = {}, merkmale = {};
      for (const [k, v] of new FormData(ev.target).entries()) { if (v === "") continue; if (k.startsWith("merkmale.")) merkmale[k.slice(9)] = v; else daten[k] = v; }
      if (daten.baujahr) daten.baujahr = Number(daten.baujahr); daten.merkmale = merkmale;
      try { await api(`/objekte/${b.dataset.anlageNeu}/anlagen`, { methode: "POST", daten }); melden("Angelegt", "ok"); kundeDetail(id); } catch (e) { melden(e.message); }
    });
  }));
  // Anlagenkartei je Anlage (Fahrzeug: Kennzeichen, FIN, HU, Kilometerstand …), siehe branchen.ANLAGENKARTEI
  for (const a of anlagen) {
    const felder = BRANCHE?.anlagenkartei?.[a.anlagentyp]; if (!felder?.length) continue;
    const mk = a.merkmale || {};
    const box = document.createElement("form"); box.className = "karte"; box.style.marginTop = "10px"; box.dataset.kartei = a.id;
    box.innerHTML = `<h4 style="margin:0 0 8px">${esc(typen[a.anlagentyp]?.bezeichnung || a.anlagentyp)}kartei · ${esc([a.hersteller, a.modell, mk.kennzeichen].filter(Boolean).join(" ") || "ohne Bezeichnung")}</h4><div class="raster">
      ${felder.map((f) => `<label>${esc(f.titel)}${f.hinweis ? `<br><small>${esc(f.hinweis)}</small>` : ""} <input name="${esc(f.schluessel)}" ${f.art === "datum" ? 'type="date"' : f.art === "zahl" ? 'type="number" step="any"' : ""} value="${esc(mk[f.schluessel] ?? "")}" ${darf ? "" : "disabled"}></label>`).join("")}</div>
      ${darf ? `<div class="aktionen"><button type="submit" class="leise">Kartei speichern</button></div>` : ""}`;
    $("#kd-formular", wurzel).before(box);
    box.addEventListener("submit", async (ev) => {
      ev.preventDefault(); const merkmale = {};
      for (const [k, v] of new FormData(box).entries()) if (v !== "") merkmale[k] = v;
      try { await api(`/anlagen/${a.id}`, { methode: "PATCH", daten: { merkmale } }); melden("Kartei gespeichert", "ok"); kundeDetail(id); } catch (e) { melden(e.message); }
    });
  }
  kundeDokumente(wurzel, id);
  if (BRANCHE?.begriffe?.anlagen !== false) kundeLebenszyklus(wurzel, id, anlagen);
  kundeEmpfehlungen(wurzel, id);
}

// --- Wartungsplanung -------------------------------------------------------------------------
async function wartung(ziel) {
  const z = await api("/twin/betrieb");
  ziel.innerHTML = `<h1>Wartungsplanung</h1><p class="unterzeile">Aus dem Digital Twin: Anlagen mit überfälliger oder in 60 Tagen fälliger Wartung. Planen legt Servicefälle an und stellt Wartungserinnerungen an Kunden zur Freigabe (Klasse B).</p>
    <div class="karte" style="margin-bottom:16px"><div class="aktionen" style="justify-content:space-between"><span>${z.wartungspotenzial.length} Anlagen mit Wartungsbedarf</span><button id="w-planen">Wartungen jetzt planen</button></div><div id="w-ergebnis"></div></div>
    <div class="karte"><table><thead><tr><th>Kunde</th><th>Anlage</th><th>Status</th></tr></thead><tbody>
    ${z.wartungspotenzial.map((w) => `<tr><td>${esc(w.kunde)}</td><td>${esc(w.typ)}</td><td>${tag(w.status)}</td></tr>`).join("") || `<tr><td colspan="3">Alles aktuell.</td></tr>`}</tbody></table></div>`;
  $("#w-planen").addEventListener("click", async () => {
    const r = await api("/wartung/planen", { methode: "POST" });
    $("#w-ergebnis").innerHTML = `<p class="hinweis">${r.angelegt} Wartungsfälle angelegt, Erinnerungen liegen in der <a href="#freigaben">Freigabe-Inbox</a>.</p>`;
    freigabenZaehlen();
  });
}

// --- Ontologie -------------------------------------------------------------------------------
const ABFRAGEN = {
  "Anlagen und passende Monteure": `PREFIX shk: <https://w3id.org/shk-backoffice/ont#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?anlagentyp (COUNT(DISTINCT ?anlage) AS ?anlagen) (GROUP_CONCAT(DISTINCT ?monteur; separator=", ") AS ?geeignete_monteure)
WHERE {
  ?anlage a ?klasse . ?klasse rdfs:subClassOf* shk:Anlage ; rdfs:label ?anlagentyp ; shk:erfordertQualifikation ?q .
  ?m shk:hatQualifikation ?q ; rdfs:label ?monteur .
} GROUP BY ?anlagentyp ORDER BY DESC(?anlagen)`,
  "Offene Vorgänge mit Status": `PREFIX shk: <https://w3id.org/shk-backoffice/ont#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?anliegen ?status ?dringlichkeit WHERE {
  ?f a shk:Servicefall ; shk:anliegen ?anliegen ; shk:dringlichkeit ?dringlichkeit ; shk:hatStatus ?s . ?s rdfs:label ?status .
  FILTER (?s IN (shk:S_eingegangen, shk:S_qualifiziert, shk:S_eingeplant))
}`,
  "Wärmeerzeuger je Hersteller": `PREFIX shk: <https://w3id.org/shk-backoffice/ont#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?hersteller (COUNT(?a) AS ?anzahl) WHERE {
  ?a a ?k . ?k rdfs:subClassOf* shk:Waermeerzeuger . ?a shk:hersteller ?hersteller .
} GROUP BY ?hersteller ORDER BY DESC(?anzahl)`,
  "Zulässige Statusübergänge": `PREFIX shk: <https://w3id.org/shk-backoffice/ont#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?von ?nach WHERE { ?n shk:folgtAuf ?v . ?v rdfs:label ?von . ?n rdfs:label ?nach . }`,
};
async function ontologie(ziel) {
  ziel.innerHTML = `<h1>Ontologie</h1><p class="unterzeile">OWL-Ontologie (TBox) plus Instanzdaten dieses Betriebs als RDF-Graph; SPARQL-Abfragen laufen nur lesend. <a href="/ontologie/tbox" target="_blank">TBox als Turtle</a></p>
    <div class="karte" style="margin-bottom:16px"><div class="wahl" style="margin-bottom:10px">${Object.keys(ABFRAGEN).map((n) => `<button class="leise" data-abfrage="${esc(n)}">${esc(n)}</button>`).join("")}</div>
    <textarea id="o-abfrage" rows="8">${esc(Object.values(ABFRAGEN)[0])}</textarea><div class="aktionen" style="margin-top:8px;justify-content:space-between"><label style="margin:0"><span><input type="checkbox" id="o-inferenz"> RDFS-Inferenz (owlrl): Unterklassen und Domänen materialisieren</span></label><button id="o-start">Abfrage ausführen</button></div></div>
    <div class="karte" id="o-ergebnis"></div>`;
  const ausfuehren = async () => {
    try {
      const r = await api("/ontologie/sparql", { methode: "POST", daten: { abfrage: $("#o-abfrage").value, inferenz: $("#o-inferenz").checked } });
      $("#o-ergebnis").innerHTML = r.typ === "ask" ? `<p>${r.ergebnis ? "Ja" : "Nein"}</p>` :
        `<div class="tabelle-rahmen"><table><thead><tr>${r.variablen.map((v) => `<th>${esc(v)}</th>`).join("")}</tr></thead><tbody>${r.zeilen.map((z) => `<tr>${z.map((w) => `<td>${esc(w)}</td>`).join("")}</tr>`).join("") || `<tr><td colspan="${r.variablen.length}">Keine Treffer.</td></tr>`}</tbody></table></div>`;
    } catch (e) { $("#o-ergebnis").innerHTML = `<p class="fehler">${esc(e.message)}</p>`; }
  };
  ziel.querySelectorAll("[data-abfrage]").forEach((b) => b.addEventListener("click", () => { $("#o-abfrage").value = ABFRAGEN[b.dataset.abfrage]; ausfuehren(); }));
  $("#o-start").addEventListener("click", ausfuehren);
  await ausfuehren();
}

// --- Ereignisprotokoll -----------------------------------------------------------------------
// --- Rechnungen ------------------------------------------------------------------------------
const EURO = new Intl.NumberFormat("de-DE", { style: "currency", currency: "EUR" });
const DATUM = new Intl.DateTimeFormat("de-DE", { dateStyle: "medium", timeZone: TZ });
async function rechnungen(ziel) {
  const [liste, wir] = await Promise.all([api("/rechnungen"), ich()]);
  const darfStorno = wir.rechte.includes("bearbeiten");
  ziel.innerHTML = `<h1>Rechnungen</h1><p class="unterzeile">Fortlaufender Nummernkreis je Betrieb; Rechnung und Storno entstehen nur über eine Freigabe der Klasse C (Inhaber). Übermittlung an Buchhaltung und Kunde läuft über die Outbox.</p>
    <div class="karte" style="margin-bottom:16px"><div class="zeile"><label>Belegpaket an die Kanzlei (Monat) <input id="r-zeitraum" type="month"></label><button id="r-belegpaket" class="leise" ${darfStorno ? "" : "disabled"}>Zur Freigabe vorlegen</button></div></div>
    <div class="karte"><div class="tabelle-rahmen"><table><thead><tr><th>Nummer</th><th>Art</th><th>Datum</th><th>Kunde</th><th style="text-align:right">Netto</th><th style="text-align:right">Brutto</th><th>Status</th><th></th></tr></thead><tbody>
    ${liste.map((r) => `<tr><td><strong>${esc(r.nummer)}</strong></td><td>${r.art === "storno" ? tag("Storno", "C", "Storno") : "Rechnung"}</td><td>${esc(DATUM.format(new Date(r.datum)))}</td>
      <td>${esc(r.empfaenger.name || "")}</td><td style="text-align:right">${esc(EURO.format(r.netto))}</td><td style="text-align:right">${esc(EURO.format(r.brutto))}</td>
      <td>${tag(r.status, r.status === "storniert" ? "abgelehnt" : "A", r.status)}</td>
      <td><div class="aktionen"><button class="leise" data-pdf="${esc(r.id)}">PDF</button><button class="leise" data-xml="${esc(r.id)}">XRechnung</button>
        ${darfStorno && r.art === "rechnung" && r.status !== "storniert" ? `<button class="gefahr" data-storno="${esc(r.id)}">Storno</button>` : ""}</div></td></tr>`).join("")
      || `<tr><td colspan="8">Noch keine Rechnungen. Rechnungen entstehen aus der Freigabe-Inbox (Aktion „Rechnung erstellen“).</td></tr>`}
    </tbody></table></div></div>`;
  ziel.querySelectorAll("[data-pdf]").forEach((b) => b.addEventListener("click", () => dateiLaden(`/rechnungen/${b.dataset.pdf}/pdf`, `${b.dataset.pdf}.pdf`)));
  ziel.querySelectorAll("[data-xml]").forEach((b) => b.addEventListener("click", () => dateiLaden(`/rechnungen/${b.dataset.xml}/xrechnung`, `${b.dataset.xml}.xml`)));
  ziel.querySelectorAll("[data-storno]").forEach((b) => b.addEventListener("click", async () => {
    const grund = prompt("Grund des Stornos (erscheint im Protokoll):"); if (!grund) return;
    try { await api(`/rechnungen/${b.dataset.storno}/storno`, { methode: "POST", daten: { grund } }); melden("Storno liegt in der Freigabe-Inbox (Klasse C)."); zeigen(); }
    catch (e) { melden(e.message); }
  }));
  $("#r-belegpaket").addEventListener("click", async () => {
    const z = $("#r-zeitraum").value; if (!z) return melden("Monat wählen");
    try { await api("/kanzlei/belegpaket", { methode: "POST", daten: { zeitraum: z } }); melden("Belegpaket liegt in der Freigabe-Inbox (Klasse C)."); }
    catch (e) { melden(e.message); }
  });
}

// --- Einstellungen (Inhaber): Stammdaten und Anbindungen -------------------------------------
const FELDER_STAMM = [["inhaber", "Inhaber/in"], ["strasse", "Straße"], ["plz", "PLZ"], ["ort", "Ort"], ["telefon", "Telefon"], ["email", "E-Mail"],
  ["ust_id", "USt-IdNr."], ["steuernummer", "Steuernummer"], ["handwerksrolle", "Handwerksrolle"], ["iban", "IBAN"], ["bic", "BIC"], ["bank", "Bank"],
  ["zahlungsziel_tage", "Zahlungsziel (Tage)"], ["rechnungsnummer_praefix", "Rechnungsnummer-Präfix"], ["rechnungshinweis", "Hinweis auf der Rechnung"],
  ["kontenrahmen", "Kontenrahmen (SKR03/SKR04)"], ["datev_beraternummer", "DATEV Beraternummer"], ["datev_mandantennummer", "DATEV Mandantennummer"], ["kanzlei_email", "E-Mail der Kanzlei"], ["whatsapp_nummer", "WhatsApp-Nummer (Klick-zum-Chat auf Website und Portal)"],
  ["stundensatz_kosten", "Stundensatz intern (€/h, Nachkalkulation)"], ["stundensatz_verrechnung", "Stundensatz Verrechnung (€/h)"], ["materialaufschlag_prozent", "Materialaufschlag (%)"],
  ["angebotsnummer_praefix", "Angebotsnummer-Präfix"], ["angebot_gueltig_tage", "Angebote gültig (Tage)"], ["mahn_karenz_tage", "Karenz nach Fälligkeit (Tage)"], ["mahn_frist_tage", "Zahlungsfrist je Mahnstufe (Tage)"],
  ["mahngebuehr_stufe2", "Mahngebühr Stufe 2 (€)"], ["mahngebuehr_stufe3", "Mahngebühr Stufe 3 (€)"], ["bewertungslink", "Bewertungslink (Google/ProvenExpert, Anfrage nach erledigtem Rapport)"], ["basis_url", "Öffentliche Adresse der Plattform (Formular, Kundenportal, Push-Links, DATEV-Rückruf)"],
  ["lat", "Betriebsstandort Breitengrad (leer: Geokodierung)"], ["lon", "Betriebsstandort Längengrad"], ["karte_kacheln", "Kachelserver der Kundenkarte ({z}/{x}/{y}, Standard OpenStreetMap; CSP img-src anpassen)"], ["karte_hinweis", "Urhebervermerk auf der Karte"],
  ["bundesland", "Bundesland für Feiertage (NW, BY, HE …)"], ["routing_url", "OSRM-Server für Fahrzeiten (leer: ROUTING_URL / Demo-Server)"], ["kontoinhaber", "Kontoinhaber auf dem GiroCode (leer: Betriebsname)"],
  ["ids_shop_url", "IDS-Connect-Adresse des Großhandels-Shops (Warenkorbübergabe, https)"], ["absage_frist_stunden", "Portal: Absage online bis (Stunden vor Termin)"], ["impressum_url", "Impressum-URL (Kundenportal)"], ["datenschutz_url", "Datenschutz-URL (Kundenportal)"]];
const ANBINDUNGEN = {
  inhaber: { titel: "Push an den Inhaber", hinweis: "Hinweise auf das Telefon bei Notdienst, Freigaben der Klasse C und gescheiterten Zustellungen (ntfy-App; Server selbst betreiben mit Profil push oder ntfy.sh). Ohne Kanal entsteht kein Hinweis.",
    typen: { ntfy: [["basis_url", "ntfy-Server (Standard https://ntfy.sh)"], ["topic", "Topic (geheimer, schwer erratbarer Name)"], ["token", "Zugriffstoken (optional)", true]], webhook: [["url", "Webhook-URL"], ["signatur_schluessel", "Signaturschlüssel (HMAC)", true]] } },
  fachsoftware: { titel: "Fachsoftware", hinweis: "Termine und Aufträge. Bis die Schreib-API der Fachsoftware geklärt ist: Webhook (z. B. n8n) oder CSV-Export.",
    typen: { webhook: [["url", "Webhook-URL"], ["signatur_schluessel", "Signaturschlüssel (HMAC)", true], ["api_key", "Bearer-Token (optional)", true]], csv: [] } },
  endkunde: { titel: "Kundenkommunikation", hinweis: "Terminbestätigung, Nachrichten, Rechnungen. Reihenfolge: E-Mail, dann SMS (nur mit Einwilligung).",
    typen: { email: [["absender", "Absenderadresse"], ["host", "SMTP-Server"], ["port", "Port (587 STARTTLS / 465 SMTPS)"], ["benutzer", "SMTP-Benutzer"], ["passwort", "SMTP-Passwort", true], ["antwort_an", "Antwort an (optional)"]],
      sms: [["api_key", "seven.io API-Key", true], ["absender", "Absenderkennung (max. 11 Zeichen)"]],
      whatsapp: [["phone_number_id", "Phone-Number-ID (Meta Business)"], ["token", "Zugriffstoken (System-User)", true], ["verify_token", "Verify-Token für den Webhook", true], ["vorlage_termin", "Vorlage Terminbestätigung (Template-Name, optional)"], ["vorlage_nachricht", "Vorlage Nachricht (optional)"], ["sprache", "Sprache der Vorlagen (de)"]], csv: [] } },
  zahlung: { titel: "Online-Zahlung im Kundenportal", hinweis: "Stripe Checkout: Kunden bezahlen Rechnungen per Karte, Apple/Google Pay oder SEPA-Lastschrift; der Zahlungseingang wird über den Webhook automatisch gebucht. Stripe-Konto des Betriebs nötig; Webhook-Ereignis checkout.session.completed auf /integrationen/stripe/webhook.",
    typen: { stripe: [["secret_key", "Secret Key (sk_live_… / sk_test_…)", true], ["webhook_secret", "Webhook-Signaturschlüssel (whsec_…)", true], ["zahlungsarten", "Zahlungsarten (optional, z. B. card,sepa_debit)"]] } },
  finanz: { titel: "Buchhaltung", hinweis: "Rechnungen und Stornos als Entwurf in die Buchhaltungssoftware; geprüfte Eingangsbelege landen dort im Belegeingang (Lexware Office: Files-API, sevDesk: Beleg-Entwurf, DATEV: Belegbilderservice nach Unternehmen online), kontiert wird im Zielsystem. DATEV: nach dem Speichern unten „DATEV verbinden“ (Anmeldung mit DATEV-Login).",
    typen: { lexware: [["api_key", "Lexware Office API-Key", true]], sevdesk: [["api_token", "sevDesk API-Token", true]], datev: [["client_id", "DATEV Client-ID (Developer Portal)"], ["client_secret", "DATEV Client-Secret", true], ["berater_nr", "Beraternummer"], ["mandant_nr", "Mandantennummer"], ["umgebung", "Umgebung (sandbox | produktion)"], ["rechnungsdaten", "XRechnung zusätzlich an den Rechnungsdatenservice (true/false)"]], csv: [] } },
  kanzlei: { titel: "Steuerkanzlei", hinweis: "Monatliches Belegpaket (PDF, XRechnung, Debitorenliste, DATEV-Buchungsstapel) per E-Mail oder als Datei. Einzelne geprüfte Eingangsbelege zusätzlich sofort: in den DATEV-Belegtransfer-Ordner, per Upload-Mail oder direkt über die DATEV-Datenservices (datev: Belegbilder sofort, Buchungsstapel monatlich).",
    typen: { email: [["kanzlei_email", "E-Mail der Kanzlei"], ["upload_email", "Upload-Adresse DATEV Unternehmen online (optional, Einzelbelege)"], ["absender", "Absenderadresse"], ["host", "SMTP-Server"], ["port", "Port"], ["benutzer", "SMTP-Benutzer"], ["passwort", "SMTP-Passwort", true]],
      belegpaket: [["verzeichnis", "Ablageverzeichnis auf dem Server"]], belegtransfer: [["verzeichnis", "DATEV-Belegtransfer-Ordner (gemountet)"], ["belegtyp", "Belegtyp-Ordner (Standard Rechnungseingang)"]], datev: [["client_id", "DATEV Client-ID (Developer Portal)"], ["client_secret", "DATEV Client-Secret", true], ["berater_nr", "Beraternummer"], ["mandant_nr", "Mandantennummer"], ["umgebung", "Umgebung (sandbox | produktion)"], ["rechnungsdaten", "XRechnung zusätzlich an den Rechnungsdatenservice (true/false)"]] } },
};
async function einstellungen(ziel) {
  const [betrieb, integ] = await Promise.all([api("/betrieb"), api("/integrationen")]);
  const st = betrieb.stammdaten || {};
  ziel.innerHTML = `<h1>Einstellungen</h1><p class="unterzeile">${esc(betrieb.name)} · Stammdaten erscheinen auf Rechnungen und Schreiben; Zugangsdaten werden verschlüsselt gespeichert.</p>
    <div class="karte" style="margin-bottom:16px"><h2>Betriebsstammdaten</h2><form id="e-stamm"><div class="raster">
      ${FELDER_STAMM.map(([k, b]) => `<label>${esc(b)} <input name="${k}" value="${esc(st[k] ?? "")}"></label>`).join("")}
      <label><span><input type="checkbox" name="kleinunternehmer" ${st.kleinunternehmer ? "checked" : ""}> Kleinunternehmer (§ 19 UStG, keine Umsatzsteuer)</span></label>
    </div><div class="aktionen"><button type="submit">Speichern</button></div></form></div>
    ${Object.entries(ANBINDUNGEN).map(([ziel, a]) => {
      const e = integ.ziele[ziel] || {}; const kanaele = e.kanaele || [];
      return `<div class="karte" style="margin-bottom:16px"><h2>${esc(a.titel)}</h2><p class="unterzeile">${esc(a.hinweis)}</p>
        <form data-ziel="${ziel}">
        ${Object.entries(a.typen).map(([typ, felder]) => `<div style="margin-bottom:10px"><label><span><input type="checkbox" name="kanal" value="${typ}" ${kanaele.includes(typ) ? "checked" : ""}> <strong>${esc(typ)}</strong></span></label>
          ${felder.length ? `<div class="raster">${felder.map(([k, b, geheim]) => `<label>${esc(b)} <input name="${typ}.${k}" ${geheim ? 'type="password" autocomplete="new-password"' : ""} value="${esc((e[typ] || {})[k] ?? "")}"></label>`).join("")}</div>` : ""}</div>`).join("")}
        <div class="aktionen"><span class="fehler" data-meldung hidden></span><button type="submit">Speichern</button></div></form></div>`;
    }).join("")}`;
  $("#e-stamm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target); const daten = {};
    for (const [k, v] of fd.entries()) if (k !== "kleinunternehmer" && v !== "") daten[k] = ["lat", "lon", "zahlungsziel_tage", "absage_frist_stunden", "stundensatz_kosten", "stundensatz_verrechnung", "materialaufschlag_prozent", "angebot_gueltig_tage", "mahn_karenz_tage", "mahn_frist_tage", "mahngebuehr_stufe2", "mahngebuehr_stufe3"].includes(k) ? Number(String(v).replace(",", ".")) : v;
    daten.kleinunternehmer = fd.has("kleinunternehmer");
    try { await api("/betrieb/stammdaten", { methode: "PUT", daten }); melden("Gespeichert", "ok"); } catch (e) { melden(e.message); }
  });
  ziel.querySelectorAll("form[data-ziel]").forEach((f) => f.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(f); const daten = { kanaele: fd.getAll("kanal") };
    for (const [k, v] of fd.entries()) { if (!k.includes(".")) continue; const [typ, feld] = k.split("."); if (v !== "") (daten[typ] ||= {})[feld] = v; }
    const meldung = f.querySelector("[data-meldung]"); meldung.hidden = true;
    try { await api(`/integrationen/${f.dataset.ziel}`, { methode: "PUT", daten }); meldung.className = "hinweis"; meldung.textContent = "Gespeichert"; meldung.hidden = false; }
    catch (e) { meldung.className = "fehler"; meldung.textContent = e.message; meldung.hidden = false; }
  }));
  await monteureVerwalten(ziel);
  await einstellungenErgaenzen(ziel);
  const brancheKarte = document.createElement("div");
  brancheKarte.className = "karte"; brancheKarte.style.marginBottom = "16px";
  const [branchen, buchung] = await Promise.all([api("/branchen"), api("/betrieb/buchung")]);
  const aktuell = BRANCHE?.schluessel || "shk";
  brancheKarte.innerHTML = `<h2>Branche und Online-Buchung</h2><p class="unterzeile">Das Branchenprofil setzt Begriffe (Monteur/Stylist/Therapeut), Leistungskatalog mit Terminlängen, Felder der Kundenkartei, Takt und Puffer der Disposition, das Wiederkehr-Intervall und die Hinweise des Sprachagenten.</p>
    <div class="zeile"><label>Branche <select id="br-wahl">${branchen.map((b) => `<option value="${esc(b.schluessel)}" ${b.schluessel === aktuell ? "selected" : ""}>${esc(b.bezeichnung)} (${b.leistungen} Leistungen${b.wiederkehr_tage ? `, Wiederkehr ${b.wiederkehr_tage} Tage` : ""})</option>`).join("")}</select></label>
      <label><span><input type="checkbox" id="br-leistungen" checked> Leistungskatalog anlegen (vorhandene Nummern bleiben)</span></label><button class="leise" id="br-setzen">Umstellen</button><span id="br-status"></span></div>
    ${BRANCHE?.rechtliches?.length ? `<ul>${BRANCHE.rechtliches.map((r) => `<li><small>${esc(r)}</small></li>`).join("")}</ul>` : ""}
    <h3>Online-Terminbuchung</h3><p>${buchung.aktiv ? `Aktiv: <a href="${esc(buchung.pfad)}" target="_blank" rel="noopener">${esc(buchung.link || buchung.pfad)}</a> · ${buchung.leistungen} Leistungen buchbar` : "Nicht aktiv."} ${buchung.link ? "" : "<small>(für den Link in E-Mails das Stammdatum „Öffentliche Adresse“ setzen)</small>"}</p>
    <div class="aktionen"><button class="leise" id="bu-an">${buchung.aktiv ? "Link erneuern" : "Buchungsseite aktivieren"}</button>${buchung.aktiv ? `<button class="gefahr" id="bu-aus">Deaktivieren</button>` : ""}</div>`;
  ziel.append(brancheKarte);
  $("#br-setzen").addEventListener("click", async () => {
    try { const e = await api("/betrieb/branche", { methode: "PUT", daten: { branche: $("#br-wahl").value, leistungen_anlegen: $("#br-leistungen").checked } }); BRANCHE = null; $("#br-status").textContent = `${e.leistungen_angelegt} Leistungen angelegt`; zeigen(); }
    catch (e) { $("#br-status").innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
  $("#bu-an").addEventListener("click", async () => { try { if (buchung.aktiv) await api("/betrieb/buchung?aktiv=false", { methode: "PUT" }); await api("/betrieb/buchung?aktiv=true", { methode: "PUT" }); zeigen(); } catch (e) { melden(e.message); } });
  $("#bu-aus")?.addEventListener("click", async () => { try { await api("/betrieb/buchung?aktiv=false", { methode: "PUT" }); zeigen(); } catch (e) { melden(e.message); } });
  const datevKarte = document.createElement("div");
  datevKarte.className = "karte"; datevKarte.style.marginBottom = "16px";
  datevKarte.innerHTML = `<h2>DATEV-Datenservices verbinden</h2><p class="unterzeile">Nach dem Speichern der DATEV-Zugangsdaten (Buchhaltung oder Steuerkanzlei → datev) meldet sich der Betrieb einmalig mit dem DATEV-Login an; die Plattform speichert nur das verschlüsselte Refresh-Token. Voraussetzung: Stammdatum „Öffentliche Adresse“ und Freischaltung der Datenservices durch die Kanzlei.</p>
    <div class="aktionen"><button class="leise" id="datev-verbinden">DATEV verbinden</button><span id="datev-status"></span></div>`;
  ziel.append(datevKarte);
  $("#datev-verbinden").addEventListener("click", async () => {
    try { const a = await api("/integrationen/datev/anmeldung"); $("#datev-status").textContent = `Anmeldung (${a.umgebung}) wird geöffnet; Rückruf an ${a.redirect_uri}`; window.open(a.url, "_blank", "noopener"); }
    catch (e) { $("#datev-status").innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
}

async function protokoll(ziel) {
  const [ereignisse, outbox, tot] = await Promise.all([api("/ereignisse?limit=200"), api("/outbox"), api("/outbox?status=tot")]);
  ziel.innerHTML = `<h1>Ereignisprotokoll</h1><p class="unterzeile">Append-only Event-Log (Audit, Replay) und Transactional Outbox an die Zielsysteme.</p>
    <div class="raster"><div class="karte"><h2>Outbox</h2><table><tbody>${[...tot.map((e) => ({ ...e, s: "tot" })), ...outbox.map((e) => ({ ...e, s: "offen" }))].map((e) =>
      `<tr><td>${tag(e.s === "tot" ? "Dead Letter" : "ausstehend", e.s === "tot" ? "tot" : "offen")}</td><td>${esc(e.ziel)}</td><td>${esc(e.aktion)}</td><td>${e.versuche}</td></tr>`).join("") || "<tr><td>Leer: alles zugestellt.</td></tr>"}</tbody></table></div></div>
    <div class="karte"><div class="tabelle-rahmen"><table><thead><tr><th>Zeit</th><th>Akteur</th><th>Ereignis</th><th>Details</th></tr></thead><tbody>
    ${ereignisse.map((e) => `<tr><td>${zeit(e.zeit)}</td><td>${tag(e.akteur, "neutral")}${e.akteur_name ? ` <small>${esc(e.akteur_name)}</small>` : ""}</td><td><code>${esc(e.typ)}</code></td><td><small>${esc(JSON.stringify(e.payload))}</small></td></tr>`).join("")}</tbody></table></div></div>`;
}

oidcRueckkehr().then(zeigen, (e) => anmelden(e.message));

// --- Mein Einsatz (Monteur, mobil): Tagesliste, Zeitstempel, Rapport mit Foto und Unterschrift ---
const ZEITART_TEXT = { arbeit: "Arbeit", fahrt: "Fahrt", pause: "Pause", bereitschaft: "Bereitschaft" };
const ERGEBNIS_TEXT = { erledigt: "Erledigt", nacharbeit: "Nacharbeit nötig", ersatzteil: "Ersatzteil bestellen", abgebrochen: "Abgebrochen" };
const dauerText = (min) => `${Math.floor(min / 60)}:${String(min % 60).padStart(2, "0")} h`;
const lokalIso = (d = new Date()) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);

async function einsatzAnsicht(ziel) {
  const [wir, ressourcen] = await Promise.all([ich(), api("/ressourcen").catch(() => [])]);
  const buero = !wir.eingeschraenkt;
  const gewaehlt = speicher("einsatzRessource") || ressourcen[0]?.id || "";
  const q = buero && gewaehlt ? `?ressource_id=${encodeURIComponent(gewaehlt)}` : "";
  const h = await api(`/einsatz/heute${q}`);
  const laufend = h.laufend;
  const tour = h.ressource ? (await api(`/tour?ressource_id=${encodeURIComponent(h.ressource.id)}`).catch(() => ({ touren: [] }))).touren[0] : null;
  const anfahrt = (terminId) => tour?.stopps.find((s) => s.termin_id === terminId);
  ziel.innerHTML = `<h1>${esc(wort("mein_einsatz", "Mein Einsatz"))}</h1><p class="unterzeile">${esc(DATUM.format(new Date()))}${h.ressource ? ` · ${esc(h.ressource.name.split(" (")[0])}` : ""}${h.bereitschaft ? ` · Bereitschaft: ${esc(h.bereitschaft.name)} ${esc(h.bereitschaft.telefon)}` : ""}</p>
    ${buero ? `<div class="karte" style="margin-bottom:16px"><label>Monteur <select id="e-ressource">${ressourcen.map((r) => `<option value="${esc(r.id)}" ${r.id === gewaehlt ? "selected" : ""}>${esc(r.name)}</option>`).join("")}</select></label></div>` : ""}
    ${!h.ressource ? `<p class="hinweis">Ihrem Konto ist noch kein Monteur zugeordnet. Der Inhaber trägt Ihren Anmeldenamen (${esc(wir.name)}) unter „Plantafel → Monteure“ ein.</p>` : `
    <div class="karte stempeluhr"><div>
      <strong>${laufend ? `${esc(ZEITART_TEXT[laufend.art])} läuft seit ${esc(UHRZEIT.format(new Date(laufend.beginn)))}` : "Keine laufende Zeit"}</strong>
      ${laufend ? `<br><small>${esc(dauerText(laufend.minuten))}${laufend.notiz ? ` · ${esc(laufend.notiz)}` : ""}</small>` : ""}</div>
      <div class="aktionen">${laufend ? `<button class="gefahr" id="e-stop">Stopp</button>` : ""}
        ${["arbeit", "fahrt", "pause"].map((a) => `<button class="leise" data-start="${a}">${ZEITART_TEXT[a]} starten</button>`).join("")}</div></div>
    <h2 style="margin-top:20px">Heutige Termine</h2>
    ${h.termine.length ? h.termine.map((t) => `<div class="karte termin ${esc(t.dringlichkeit)}" style="margin-bottom:12px">
      <div class="zeile" style="justify-content:space-between"><strong>${esc(UHRZEIT.format(new Date(t.beginn)))}–${esc(UHRZEIT.format(new Date(t.ende)))}</strong>${tag(t.dringlichkeit)} ${tag(t.status)}</div>
      <p style="margin:6px 0"><strong>${esc(t.kunde.name)}</strong><br>${esc(t.kunde.strasse || "")}, ${esc(t.kunde.plz || "")} ${esc(t.kunde.ort || "")}
        ${t.kunde.telefon ? `<br><a href="tel:${esc(t.kunde.telefon)}">${esc(t.kunde.telefon)}</a>` : ""}
        <br>${(() => { const a = anfahrt(t.termin_id); return a?.fahrzeit_minuten != null ? `<small>Anfahrt ${a.fahrzeit_minuten} min · ${a.km} km</small> · ` : ""; })()}<a href="${esc(t.navigation.geo)}">Navigation</a> · <a target="_blank" rel="noopener" href="${esc(t.navigation.google)}">Google Maps</a> · <a target="_blank" rel="noopener" href="${esc(t.navigation.apple)}">Apple Karten</a> · <a target="_blank" rel="noopener" href="${esc(t.navigation.osm)}">OSM</a></p>
      <p style="margin:6px 0">${esc(t.anliegen)}${t.symptom ? `<br><small>${esc(t.symptom)}</small>` : ""}${t.anlage ? `<br><small>Anlage: ${esc(t.anlage.typ)} ${esc(t.anlage.hersteller || "")} ${esc(t.anlage.baujahr || "")}</small>` : ""}</p>
      <div class="aktionen">${t.rapport ? `<span class="marke-tag t-A">Rapport ${esc(t.rapport.nummer)}${t.rapport.unterschrieben ? " · unterschrieben" : ""}</span>` : ""}
        <button class="leise" data-fall="${esc(t.servicefall_id)}">Details</button>
        <button class="leise" data-arbeit="${esc(t.servicefall_id)}">Arbeit starten</button>
        <button data-rapport="${esc(t.servicefall_id)}" data-termin="${esc(t.termin_id)}">Rapport</button></div></div>`).join("")
      : `<p class="karte">Heute keine Termine eingeplant.</p>`}
    <h2 style="margin-top:20px">Beleg fotografieren</h2>
    <div class="karte"><p class="unterzeile">Lieferschein oder Rechnung vom Großhandel: Foto reicht, das Büro prüft die erkannten Felder.</p>
      <div class="zeile"><label>Datei oder Foto <input id="e-beleg" type="file" accept="image/*,application/pdf,.xml" capture="environment"></label>
      <label>Vorgang <select id="e-beleg-fall"><option value="">– ohne –</option>${h.termine.map((t) => `<option value="${esc(t.servicefall_id)}">${esc(t.kunde.name)} · ${esc(t.anliegen)}</option>`).join("")}</select></label>
      <button id="e-beleg-senden" class="leise">Hochladen</button></div><p id="e-beleg-status"></p></div>`}`;
  $("#e-ressource")?.addEventListener("change", (ev) => { speicher("einsatzRessource", ev.target.value); zeigen(); });
  const rid = buero ? gewaehlt : undefined;
  const starten = async (art, fall) => { try { await api("/zeiten/start", { methode: "POST", daten: { art, servicefall_id: fall, ressource_id: rid } }); zeigen(); } catch (e) { melden(e.message); } };
  ziel.querySelectorAll("[data-start]").forEach((b) => b.addEventListener("click", () => starten(b.dataset.start)));
  ziel.querySelectorAll("[data-arbeit]").forEach((b) => b.addEventListener("click", () => starten("arbeit", b.dataset.arbeit)));
  $("#e-stop")?.addEventListener("click", async () => { try { await api(`/zeiten/${laufend.id}/stop`, { methode: "POST" }); zeigen(); } catch (e) { melden(e.message); } });
  ziel.querySelectorAll("[data-fall]").forEach((b) => b.addEventListener("click", () => fallDetail(b.dataset.fall)));
  ziel.querySelectorAll("[data-rapport]").forEach((b) => b.addEventListener("click", () => rapportFormular(b.dataset.rapport, b.dataset.termin, rid)));
  $("#e-beleg-senden")?.addEventListener("click", async () => {
    const datei = $("#e-beleg").files[0]; if (!datei) return melden("Bitte Datei oder Foto wählen");
    const fd = new FormData(); fd.append("datei", datei); if ($("#e-beleg-fall").value) fd.append("servicefall_id", $("#e-beleg-fall").value);
    $("#e-beleg-status").textContent = "Wird hochgeladen …";
    try { const b = await api("/belege", { methode: "POST", daten: fd }); $("#e-beleg-status").innerHTML = `Erfasst: ${esc(b.lieferant)} · ${esc(EURO.format(b.brutto))} ${tag(`Konfidenz ${Math.round((b.erkennung.konfidenz || 0) * 100)} %`, "neutral")}`; $("#e-beleg").value = ""; }
    catch (e) { $("#e-beleg-status").innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
}

// Unterschrift auf Canvas (Maus und Touch), Ergebnis als PNG-Data-URL
function unterschriftFeld(canvas) {
  const ctx = canvas.getContext("2d"); let zeichnet = false, leer = true;
  ctx.lineWidth = 2; ctx.lineCap = "round"; ctx.strokeStyle = "#18212b";
  const punkt = (ev) => { const r = canvas.getBoundingClientRect(); return [(ev.clientX - r.left) * canvas.width / r.width, (ev.clientY - r.top) * canvas.height / r.height]; };
  canvas.addEventListener("pointerdown", (ev) => { zeichnet = true; leer = false; ctx.beginPath(); ctx.moveTo(...punkt(ev)); canvas.setPointerCapture(ev.pointerId); });
  canvas.addEventListener("pointermove", (ev) => { if (zeichnet) { ctx.lineTo(...punkt(ev)); ctx.stroke(); } });
  canvas.addEventListener("pointerup", () => { zeichnet = false; });
  canvas.addEventListener("pointercancel", () => { zeichnet = false; });
  return { leeren() { ctx.clearRect(0, 0, canvas.width, canvas.height); leer = true; }, wert() { return leer ? null : canvas.toDataURL("image/png"); } };
}

async function rapportFormular(fallId, terminId, ressourceId) {
  const d = await api(`/servicefaelle/${fallId}`);
  const zeiten = await api(`/zeiten?servicefall_id=${fallId}`);
  let checkliste = null;
  if (d.servicefall.anlage_id) {
    try {
      const kd = await api(`/kunden/${d.servicefall.kunde_id}`);
      const anlage = kd.objekte.flatMap((o) => o.anlagen).find((a) => a.id === d.servicefall.anlage_id);
      if (anlage) checkliste = await api(`/checklisten/${anlage.anlagentyp}`);
    } catch { /* ohne Checkliste weiter */ }
  }
  const summe = (art) => zeiten.filter((z) => z.art === art).reduce((a, z) => a + z.minuten, 0);
  const wurzel = detail(`<h2>Rapport · ${esc(d.servicefall.anliegen)}</h2>
    <label>Ausgeführte Arbeiten <textarea id="rp-taetigkeiten" style="font-family:inherit" placeholder="Was wurde gemacht, welche Ursache, welche Teile?"></textarea></label>
    <div class="zeile"><label>Arbeitszeit (min) <input id="rp-arbeit" type="number" min="0" value="${summe("arbeit")}"></label>
      <label>Fahrzeit (min) <input id="rp-fahrt" type="number" min="0" value="${summe("fahrt")}"></label>
      <label>Ergebnis <select id="rp-ergebnis">${Object.entries(ERGEBNIS_TEXT).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label></div>
    <label>Material (eine Zeile je Position: Menge; Bezeichnung – oder Menge; Artikelnummer; Bezeichnung, dann bucht das Fahrzeuglager ab) <textarea id="rp-material" style="font-family:inherit" placeholder="1; Umwälzpumpe Grundfos UPM3&#10;2; M-210; Dichtung 3/4&quot;"></textarea></label>
    ${checkliste ? `<details open class="karte" style="margin:8px 0"><summary><strong>Wartungs-Checkliste ${esc(checkliste.bezeichnung)}</strong> <small>(${checkliste.punkte.length} Prüfpunkte; Grenzwerte als Richtwert)</small></summary><div class="pruefpunkte" id="rp-checkliste">
      ${checkliste.punkte.map((p) => `<label for="cl-${esc(p.schluessel)}">${esc(p.titel)}${p.pflicht ? " *" : ""}${p.norm ? `<br><span class="norm">${esc(p.norm)}</span>` : ""}</label>
        ${p.art === "ja_nein" ? `<select id="cl-${esc(p.schluessel)}" data-cl="${esc(p.schluessel)}"><option value="">–</option><option value="ja">ja / i. O.</option><option value="nein">nein / Mangel</option></select>`
        : p.art === "messwert" ? `<input id="cl-${esc(p.schluessel)}" data-cl="${esc(p.schluessel)}" inputmode="decimal" placeholder="${esc([p.min != null ? `≥ ${p.min}` : "", p.max != null ? `≤ ${p.max}` : ""].filter(Boolean).join(" "))} ${esc(p.einheit || "")}">`
        : `<input id="cl-${esc(p.schluessel)}" data-cl="${esc(p.schluessel)}">`}`).join("")}</div></details>` : ""}
    <label>Hinweise an den Kunden <input id="rp-hinweise" placeholder="z. B. Wartung in 12 Monaten, Druck beobachten"></label>
    <div class="zeile"><label>Abgasverlust (%) <input id="rp-abgas" inputmode="decimal"></label><label>CO (ppm) <input id="rp-co" inputmode="decimal"></label><label>Anlagendruck (bar) <input id="rp-druck" inputmode="decimal"></label></div>
    <label>Fotos (vorher/nachher, Typenschild) <input id="rp-fotos" type="file" accept="image/*" capture="environment" multiple></label>
    <label>Unterschrift Kunde (Name) <input id="rp-name" placeholder="Name der unterzeichnenden Person"></label>
    <div class="unterschrift"><canvas id="rp-canvas" width="600" height="200"></canvas><button class="leise" type="button" id="rp-leeren">Löschen</button></div>
    <p id="rp-status"></p>
    <div class="aktionen"><button id="rp-speichern">Rapport abschließen</button></div>`);
  const u = unterschriftFeld($("#rp-canvas", wurzel));
  $("#rp-leeren", wurzel).addEventListener("click", u.leeren);
  $("#rp-speichern", wurzel).addEventListener("click", async () => {
    const knopf = $("#rp-speichern", wurzel); knopf.disabled = true; $("#rp-status", wurzel).textContent = "Wird gespeichert …";
    try {
      const fotos = [...$("#rp-fotos", wurzel).files];
      for (const foto of fotos) {
        const fd = new FormData(); fd.append("datei", foto); fd.append("bezug_typ", "servicefall"); fd.append("bezug_id", fallId); fd.append("art", "foto");
        await api("/anhaenge", { methode: "POST", daten: fd });
      }
      const material = $("#rp-material", wurzel).value.split("\n").map((z) => z.trim()).filter(Boolean).map((z) => { const teile = z.split(";").map((x) => x.trim()); if (teile.length >= 3) return { menge: teile[0], nummer: teile[1], bezeichnung: teile.slice(2).join("; ") }; return teile.length === 2 ? { menge: teile[0], bezeichnung: teile[1] } : { menge: "1", bezeichnung: z }; });
      const messwerte = {}; for (const [id, k] of [["rp-abgas", "abgasverlust_prozent"], ["rp-co", "co_ppm"], ["rp-druck", "druck_bar"]]) { const v = $(`#${id}`, wurzel).value.trim(); if (v) messwerte[k] = v; }
      const cl = {}; wurzel.querySelectorAll("[data-cl]").forEach((el) => { if (el.value.trim()) cl[el.dataset.cl] = el.value.trim(); }); if (Object.keys(cl).length) messwerte.checkliste = cl;
      const r = await api(`/servicefaelle/${fallId}/rapport`, { methode: "POST", daten: {
        taetigkeiten: $("#rp-taetigkeiten", wurzel).value, arbeitszeit_minuten: Number($("#rp-arbeit", wurzel).value || 0), fahrzeit_minuten: Number($("#rp-fahrt", wurzel).value || 0),
        ergebnis: $("#rp-ergebnis", wurzel).value, material, hinweise_kunde: $("#rp-hinweise", wurzel).value || null, messwerte, termin_id: terminId || null,
        ressource_id: ressourceId || null, unterschrift: u.wert(), unterzeichnet_von: $("#rp-name", wurzel).value || null } });
      const bef = r.messwerte?.befunde;
      $("#rp-status", wurzel).innerHTML = `Rapport <strong>${esc(r.nummer)}</strong> gespeichert${fotos.length ? `, ${fotos.length} Foto(s) abgelegt` : ""}${bef ? ` · Checkliste ${bef.gesamt === "ok" ? tag("ohne Befund", "aktuell") : bef.gesamt === "auffaellig" ? tag(`${bef.auffaellig} auffällig`, "ueberfaellig") : tag(`${bef.offen} Pflichtpunkte offen`, "offen")}` : ""}${r.messwerte?.lagerbuchungen?.length ? ` · ${r.messwerte.lagerbuchungen.length} Lagerbuchung(en)` : ""}. <button class="leise" id="rp-pdf">PDF</button>`;
      $("#rp-pdf", wurzel).addEventListener("click", () => dateiLaden(`/rapporte/${r.id}/pdf`, `${r.nummer}.pdf`));
      zeigen();
    } catch (e) { $("#rp-status", wurzel).innerHTML = `<span class="fehler">${esc(e.message)}</span>`; knopf.disabled = false; }
  });
}

// Ergänzungen der Vorgangsdetails: Rapporte, Fotos, Nachkalkulation (Inhaber)
async function fallErgaenzungen(wurzel, id) {
  const wir = await ich();
  const [rapporte, fotos] = await Promise.all([api(`/servicefaelle/${id}/rapporte`).catch(() => []), api(`/anhaenge?bezug_typ=servicefall&bezug_id=${id}`).catch(() => [])]);
  const teil = document.createElement("div");
  teil.innerHTML = `<h3 style="margin-top:16px">Rapporte</h3>${rapporte.length ? `<ul class="verlauf">${rapporte.map((r) => `<li><strong>${esc(r.nummer)}</strong> · ${esc(ERGEBNIS_TEXT[r.ergebnis] || r.ergebnis)} · ${esc(dauerText(r.arbeitszeit_minuten))} Arbeit${r.unterzeichnet_am ? ` · unterschrieben von ${esc(r.unterzeichnet_von || "Kunde")}` : " · ohne Unterschrift"} <button class="leise" data-rpdf="${esc(r.id)}" data-nr="${esc(r.nummer)}">PDF</button></li>`).join("")}</ul>` : "<p>Noch kein Rapport.</p>"}
    ${fotos.filter((a) => a.art === "foto").length ? `<h3 style="margin-top:16px">Fotos</h3><div class="galerie">${fotos.filter((a) => a.art === "foto").map((a) => `<figure><img data-bild="${esc(a.id)}" alt="${esc(a.name)}"><figcaption>${esc(a.name)}</figcaption></figure>`).join("")}</div>` : ""}
    ${wir.rechte.includes("einsatz") ? `<div class="aktionen" style="margin-top:8px"><button class="leise" data-rapport-neu="${esc(id)}">Rapport erfassen</button></div>` : ""}
    <p id="portal-link" class="unterzeile"></p>
    ${wir.rechte.includes("finanzen") ? `<h3 style="margin-top:16px">Nachkalkulation</h3><div id="kalk">Wird geladen …</div>` : ""}`;
  wurzel.append(teil);
  api(`/servicefaelle/${id}/portal`).then((pl) => { $("#portal-link", teil).innerHTML = pl.link ? `Kundenportal: <a href="${esc(pl.link)}" target="_blank" rel="noopener">${esc(pl.link)}</a> (steht in Terminbestätigung und Erinnerung)` : `Kundenportal: <a href="${esc(pl.pfad)}" target="_blank" rel="noopener">lokal öffnen</a> · für Links in E-Mails das Stammdatum „Öffentliche Adresse“ setzen`; }).catch(() => {});
  teil.querySelectorAll("[data-rpdf]").forEach((b) => b.addEventListener("click", () => dateiLaden(`/rapporte/${b.dataset.rpdf}/pdf`, `${b.dataset.nr}.pdf`)));
  teil.querySelectorAll("[data-bild]").forEach(async (img) => { img.src = await bildUrl(img.dataset.bild); });
  teil.querySelector("[data-rapport-neu]")?.addEventListener("click", () => rapportFormular(id, null, null));
  fallEmpfehlungen(wurzel, id);
  if (wir.rechte.includes("finanzen")) {
    try {
      const k = await api(`/servicefaelle/${id}/kalkulation`);
      const db = Number(k.deckungsbeitrag);
      $("#kalk", teil).innerHTML = `<table><tbody>
        <tr><td>Arbeitszeit</td><td style="text-align:right">${esc(k.stunden)} h</td><td style="text-align:right">Lohnkosten ${esc(EURO.format(k.lohnkosten))}</td></tr>
        <tr><td>Material (${k.belege} Belege)</td><td></td><td style="text-align:right">${esc(EURO.format(k.material))}</td></tr>
        <tr><td>Fremdleistung</td><td></td><td style="text-align:right">${esc(EURO.format(k.fremdleistung))}</td></tr>
        <tr><td>Umsatz netto (${k.rechnungen} Rechnungen)</td><td style="text-align:right"><small>Soll ${esc(EURO.format(k.soll_umsatz))}</small></td><td style="text-align:right"><strong>${esc(EURO.format(k.umsatz_netto))}</strong></td></tr>
        <tr><td>Deckungsbeitrag</td><td style="text-align:right">${k.deckungsbeitrag_quote != null ? `${esc(k.deckungsbeitrag_quote)} %` : "–"}</td><td style="text-align:right"><strong class="${db < 0 ? "fehler" : ""}">${esc(EURO.format(k.deckungsbeitrag))}</strong></td></tr></tbody></table>
        <p class="unterzeile" style="margin:8px 0 0">Sätze: ${esc(k.saetze.stundensatz_kosten)} €/h Kosten, ${esc(k.saetze.stundensatz_verrechnung)} €/h Verrechnung, ${esc(k.saetze.materialaufschlag_prozent)} % Materialaufschlag (Einstellungen → Stammdaten). ${k.vorschlag_positionen.length ? `Rechnungsvorschlag: ${k.vorschlag_positionen.length} Positionen.` : ""}</p>`;
    } catch (e) { $("#kalk", teil).innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  }
}

// --- Belegeingang (Büro/Inhaber): Prüfen, Zuordnen, Übergabe im Belegpaket -----------------------
const KATEGORIE_TEXT = { material: "Material", fremdleistung: "Fremdleistung", fahrzeug: "Fahrzeug", sonstiges: "Sonstiges" };
const BELEGSTATUS_TEXT = { erfasst: "Zu prüfen", geprueft: "Geprüft", uebergeben: "Übergeben", bezahlt: "Bezahlt" };
async function belegeAnsicht(ziel) {
  const [wir, liste] = await Promise.all([ich(), api("/belege")]);
  const darf = wir.rechte.includes("bearbeiten");
  const offen = liste.filter((b) => b.status === "erfasst").length;
  const scanner = wir.rechte.includes("verwalten") ? await api("/betrieb/scanner").catch(() => ({})) : {};
  const kanaele = Object.entries(scanner.uebergabekanaele || {}).map(([z, k]) => `${z}: ${k.join(", ")}`).join(" · ");
  ziel.innerHTML = `<h1>Belege</h1><p class="unterzeile">Eingangsrechnungen und Lieferscheine: XRechnung und ZUGFeRD werden strukturiert gelesen, PDF und Fotos per Texterkennung. Geprüfte Belege gehen sofort an die angebundenen Steuertools (${esc(kanaele || "keine Anbindung, nur Belegpaket")}) und mit dem nächsten Belegpaket an die Kanzlei; die Kontierung bleibt dort.</p>
    <div class="kacheln"><div class="karte kachel"><div class="wert">${offen}</div><div class="bez">zu prüfen</div></div>
      <div class="karte kachel"><div class="wert">${liste.filter((b) => b.status === "geprueft").length}</div><div class="bez">bereit für Belegpaket</div></div>
      <div class="karte kachel"><div class="wert">${esc(EURO.format(liste.filter((b) => b.status !== "bezahlt" && b.status !== "uebergeben").reduce((a, b) => a + Number(b.brutto), 0)))}</div><div class="bez">offen (brutto)</div></div></div>
    <div class="karte" style="margin-bottom:16px"><h2>Beleg hochladen</h2><div class="zeile">
      <label>Datei (XRechnung-XML, ZUGFeRD/PDF, Foto) <input id="b-datei" type="file" accept="image/*,application/pdf,.xml,text/xml"></label>
      <label>Kategorie <select id="b-kategorie">${Object.entries(KATEGORIE_TEXT).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label>
      <button id="b-hochladen">Erkennen und erfassen</button></div>
      <details><summary>Ohne Datei von Hand erfassen</summary><div class="zeile" style="margin-top:8px"><label>Lieferant <input id="b-lieferant"></label><label>Nummer <input id="b-nummer"></label><label>Datum <input id="b-datum" type="date"></label><label>Brutto <input id="b-brutto" inputmode="decimal"></label><label>USt <select id="b-ust"><option>19</option><option>7</option><option>0</option></select></label><button id="b-manuell" class="leise">Erfassen</button></div></details>
      <p id="b-status"></p></div>
    <div class="karte" style="margin-bottom:16px"><h2>Mit der Kamera scannen</h2><p class="unterzeile">Mehrere Seiten nacheinander fotografieren; sie werden zu einem PDF zusammengefasst (Graustufen, Kontrast) und erkannt.${scanner.host || scanner.eingangsordner ? " Scan-to-Mail-Postfach bzw. Eingangsordner werden alle 15 Minuten abgerufen." : ""}</p><div class="zeile">
      <label>Seiten <input id="b-bilder" type="file" accept="image/*" capture="environment" multiple></label>
      <label><span><input id="b-scanmodus" type="checkbox" checked> Scanmodus (Graustufen, Kontrast)</span></label>
      <button id="b-scannen">Scannen und erfassen</button>${scanner.host || scanner.eingangsordner ? `<button class="leise" id="b-abrufen">Scanner-Eingang jetzt abrufen</button>` : ""}</div>
      <div id="b-seiten" class="scanseiten"></div><p id="b-scan-status"></p></div>
    <div class="karte"><div class="tabelle-rahmen"><table><thead><tr><th>Datum</th><th>Lieferant</th><th>Nummer</th><th style="text-align:right">Netto</th><th style="text-align:right">Brutto</th><th>Kategorie</th><th>Erkennung</th><th>Status</th><th>Übergabe</th></tr></thead><tbody>
      ${liste.map((b) => `<tr class="klickbar" data-beleg="${esc(b.id)}"><td>${esc(DATUM.format(new Date(b.datum)))}</td><td>${esc(b.lieferant)}</td><td>${esc(b.nummer || "–")}</td><td style="text-align:right">${esc(EURO.format(b.netto))}</td><td style="text-align:right">${esc(EURO.format(b.brutto))}</td>
        <td>${esc(KATEGORIE_TEXT[b.kategorie])}</td><td>${tag(b.quelle, "neutral")} ${b.erkennung.konfidenz != null && b.quelle !== "manuell" ? tag(`${Math.round(b.erkennung.konfidenz * 100)} %`, b.erkennung.konfidenz >= 0.75 ? "A" : "B") : ""}</td>
        <td>${tag(BELEGSTATUS_TEXT[b.status], b.status === "erfasst" ? "offen" : b.status === "bezahlt" ? "neutral" : "A")}</td>
        <td>${(b.uebergaben || []).map((u) => tag(u.zielsystem, "A")).join(" ") || (b.erkennung.eingang ? tag(b.erkennung.eingang, "neutral") : "")}</td></tr>`).join("") || `<tr><td colspan="9">Noch keine Belege.</td></tr>`}</tbody></table></div></div>`;
  const senden = async (fd) => {
    $("#b-status").textContent = "Wird verarbeitet …";
    try { const b = await api("/belege", { methode: "POST", daten: fd }); $("#b-status").textContent = `Erfasst: ${b.lieferant}, ${EURO.format(b.brutto)}.`; zeigen(); belegDetail(b.id); }
    catch (e) { $("#b-status").innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  };
  $("#b-hochladen").addEventListener("click", () => {
    const datei = $("#b-datei").files[0]; if (!datei) return melden("Datei wählen");
    const fd = new FormData(); fd.append("datei", datei); fd.append("kategorie", $("#b-kategorie").value); senden(fd);
  });
  $("#b-manuell").addEventListener("click", () => {
    const fd = new FormData(); for (const [id, feld] of [["b-lieferant", "lieferant"], ["b-nummer", "nummer"], ["b-datum", "datum"], ["b-brutto", "brutto"], ["b-ust", "ust_satz"]]) { const v = $(`#${id}`).value.trim(); if (v) fd.append(feld, v); }
    fd.append("kategorie", $("#b-kategorie").value); senden(fd);
  });
  $("#b-bilder").addEventListener("change", () => {
    const leiste = $("#b-seiten"); leiste.innerHTML = "";
    [...$("#b-bilder").files].forEach((f, i) => { const img = document.createElement("img"); img.alt = `Seite ${i + 1}`; img.src = URL.createObjectURL(f); leiste.append(img); });
  });
  $("#b-scannen").addEventListener("click", async () => {
    const dateien = [...$("#b-bilder").files]; if (!dateien.length) return melden("Seiten fotografieren oder wählen");
    const fd = new FormData(); dateien.forEach((f) => fd.append("bilder", f)); fd.append("kategorie", $("#b-kategorie").value); fd.append("scanmodus", $("#b-scanmodus").checked ? "true" : "false");
    $("#b-scan-status").textContent = `${dateien.length} Seite(n) werden zusammengefasst …`;
    try { const b = await api("/belege/scan", { methode: "POST", daten: fd }); $("#b-scan-status").textContent = `Erfasst: ${b.lieferant}, ${EURO.format(b.brutto)}.`; zeigen(); belegDetail(b.id); }
    catch (e) { $("#b-scan-status").innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
  $("#b-abrufen")?.addEventListener("click", async () => {
    $("#b-scan-status").textContent = "Scanner-Eingang wird abgerufen …";
    try { const r = await api("/belege/scaneingang/abrufen", { methode: "POST", daten: {} }); const n = (r.postfach?.uebernommen || 0) + (r.ordner?.uebernommen || 0); $("#b-scan-status").textContent = `${n} neue Belege übernommen.`; if (n) zeigen(); }
    catch (e) { $("#b-scan-status").innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
  ziel.querySelectorAll("[data-beleg]").forEach((z) => z.addEventListener("click", () => belegDetail(z.dataset.beleg, darf)));
}
async function belegDetail(id, darf = true) {
  const [b, faelle] = await Promise.all([api(`/belege/${id}`), api("/servicefaelle?limit=300")]);
  const fest = b.status === "uebergeben" || b.status === "bezahlt";
  const feld = (name, bez, typ = "text", wert = b[name]) => `<label>${bez} <input data-feld="${name}" type="${typ}" value="${esc(wert ?? "")}" ${fest ? "disabled" : ""}></label>`;
  const wurzel = detail(`<h2>${esc(b.lieferant)} ${b.nummer ? `· ${esc(b.nummer)}` : ""}</h2>
    <p>${tag(BELEGSTATUS_TEXT[b.status], b.status === "erfasst" ? "offen" : "A")} ${tag(b.quelle, "neutral")}${b.erkennung.eingang ? tag(b.erkennung.eingang, "neutral") : ""}${(b.uebergaben || []).length ? `<br><small>Übergeben an: ${b.uebergaben.map((u) => `${esc(u.zielsystem)} (${esc(u.externe_id)}, ${esc(zeit(u.zeitpunkt))})`).join("; ")}</small>` : ""}${b.erkennung.hinweise?.length ? `<br><small>Hinweise der Erkennung: ${esc(b.erkennung.hinweise.join("; "))}</small>` : ""}${b.erkennung.kommission ? `<br><small>Kommission laut Beleg: ${esc(b.erkennung.kommission)}</small>` : ""}</p>
    <div class="belegansicht">${b.anhang_id ? `<div id="b-vorschau" class="vorschau">Wird geladen …</div>` : ""}<div>
    <div class="zeile">${feld("lieferant", "Lieferant")}${feld("nummer", "Belegnummer")}</div>
    <div class="zeile">${feld("datum", "Belegdatum", "date")}${feld("faellig", "Fällig", "date")}</div>
    <div class="zeile">${feld("netto", "Netto")}${feld("umsatzsteuer", "USt")}${feld("brutto", "Brutto")}<label>Satz <select data-feld="ust_satz" ${fest ? "disabled" : ""}>${["19", "7", "0"].map((s) => `<option ${s === b.ust_satz ? "selected" : ""}>${s}</option>`).join("")}</select></label></div>
    <div class="zeile">${feld("skonto_prozent", "Skonto %")}${feld("skonto_bis", "Skonto bis", "date")}</div>
    <div class="zeile"><label>Kategorie <select data-feld="kategorie" ${fest ? "disabled" : ""}>${Object.entries(KATEGORIE_TEXT).map(([k, v]) => `<option value="${k}" ${k === b.kategorie ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      <label>Vorgang (Nachkalkulation) <select data-feld="servicefall_id" ${fest ? "disabled" : ""}><option value="">– keiner –</option>${faelle.map((f) => `<option value="${esc(f.id)}" ${f.id === b.servicefall_id ? "selected" : ""}>${esc(f.anliegen)} (${esc(STATUS_TEXT[f.status])})</option>`).join("")}</select></label></div>
    <label>Notiz <input data-feld="notiz" value="${esc(b.notiz || "")}"></label>
    ${b.positionen.length ? `<details><summary>${b.positionen.length} Positionen</summary><table><tbody>${b.positionen.map((p) => `<tr><td>${esc(p.bezeichnung || "")}</td><td style="text-align:right">${esc(p.menge || "")}</td><td style="text-align:right">${p.netto != null ? esc(EURO.format(p.netto)) : ""}</td></tr>`).join("")}</tbody></table></details>` : ""}
    <p id="b-meldung"></p>
    <div class="aktionen">${b.anhang_id ? `<button class="leise" id="b-datei-laden">Original</button>` : ""}${darf && !fest ? `<button class="gefahr" id="b-loeschen">Löschen</button><button class="leise" id="b-speichern">Speichern</button><button id="b-pruefen">Geprüft, ins Belegpaket</button>` : ""}${darf && b.status === "uebergeben" ? `<button class="leise" id="b-bezahlt">Als bezahlt markieren</button>` : ""}${darf && b.status !== "erfasst" ? `<button class="leise" id="b-uebergabe">An Steuertools übergeben</button>` : ""}</div></div></div>`);
  if (b.anhang_id) {
    const url = await bildUrl(b.anhang_id);
    const a = (await api(`/anhaenge?bezug_typ=beleg&bezug_id=${id}`))[0];
    $("#b-vorschau", wurzel).innerHTML = a?.mime?.startsWith("image/") ? `<img src="${url}" alt="Beleg">` : a?.mime === "application/pdf" ? `<iframe src="${url}" title="Beleg"></iframe>` : `<p>${esc(a?.name || "Datei")} (${esc(a?.mime || "")})</p>`;
    $("#b-datei-laden", wurzel)?.addEventListener("click", () => dateiLaden(`/anhaenge/${b.anhang_id}/inhalt`, a?.name || "beleg"));
  }
  const werte = () => { const d = {}; wurzel.querySelectorAll("[data-feld]").forEach((e) => { d[e.dataset.feld] = e.value === "" ? null : e.value; }); return d; };
  const speichern = async (extra = {}) => {
    try { await api(`/belege/${id}`, { methode: "PATCH", daten: { ...werte(), ...extra } }); $("#b-meldung", wurzel).textContent = "Gespeichert."; zeigen(); if (extra.status) $("#detail").close(); }
    catch (e) { $("#b-meldung", wurzel).innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  };
  $("#b-speichern", wurzel)?.addEventListener("click", () => speichern());
  $("#b-pruefen", wurzel)?.addEventListener("click", () => speichern({ status: "geprueft" }));
  $("#b-uebergabe", wurzel)?.addEventListener("click", async () => { try { await api(`/belege/${id}/uebergabe`, { methode: "POST", daten: {} }); $("#b-meldung", wurzel).textContent = "Übergabe eingereiht; der Worker stellt zu."; } catch (e) { $("#b-meldung", wurzel).innerHTML = `<span class="fehler">${esc(e.message)}</span>`; } });
  $("#b-bezahlt", wurzel)?.addEventListener("click", async () => { try { await api(`/belege/${id}`, { methode: "PATCH", daten: { status: "bezahlt" } }); $("#detail").close(); zeigen(); } catch (e) { melden(e.message); } });
  $("#b-loeschen", wurzel)?.addEventListener("click", async () => { if (!confirm("Beleg und Datei löschen?")) return; try { await api(`/belege/${id}`, { methode: "DELETE" }); $("#detail").close(); zeigen(); } catch (e) { melden(e.message); } });
}

// --- Zeiten: Liste, Nachtrag, Lohnexport ------------------------------------------------------
async function zeitenAnsicht(ziel) {
  const [wir, ressourcen] = await Promise.all([ich(), api("/ressourcen").catch(() => [])]);
  const monat = speicher("zeitenMonat") || TAGESSCHLUESSEL.format(new Date()).slice(0, 7);
  const [j, mo] = monat.split("-").map(Number);
  const von = new Date(Date.UTC(j, mo - 1, 1)).toISOString(), bis = new Date(Date.UTC(j, mo, 1)).toISOString();
  const liste = await api(`/zeiten?von=${von}&bis=${bis}`);
  const name = (id) => ressourcen.find((r) => r.id === id)?.name.split(" (")[0] || id.slice(0, 8);
  const summen = {}; liste.forEach((z) => { summen[z.ressource_id] = summen[z.ressource_id] || { arbeit: 0, fahrt: 0, pause: 0, bereitschaft: 0 }; summen[z.ressource_id][z.art] += z.minuten; });
  ziel.innerHTML = `<h1>Zeiten</h1><p class="unterzeile">Stempelzeiten der Monteure je Vorgang; Grundlage für Nachkalkulation und Lohnvorbereitung (§ 16 ArbZG Aufzeichnung, Nacht- und Sonntagszuschläge ausgewiesen).</p>
    <div class="karte" style="margin-bottom:16px"><div class="zeile"><label>Monat <input id="z-monat" type="month" value="${esc(monat)}"></label>
      ${wir.rechte.includes("verwalten") ? `<button id="z-export" class="leise">Lohnexport (CSV)</button>` : ""}</div></div>
    <div class="kacheln">${Object.entries(summen).map(([rid, s]) => `<div class="karte kachel"><div class="wert">${esc(dauerText(s.arbeit + s.fahrt))}</div><div class="bez">${esc(name(rid))} · ${esc(dauerText(s.arbeit))} Arbeit, ${esc(dauerText(s.fahrt))} Fahrt</div></div>`).join("") || ""}</div>
    <div class="karte" style="margin-bottom:16px"><h2>Nachtragen</h2><div class="zeile">
      ${!wir.eingeschraenkt ? `<label>Monteur <select id="z-ressource">${ressourcen.map((r) => `<option value="${esc(r.id)}">${esc(r.name)}</option>`).join("")}</select></label>` : ""}
      <label>Art <select id="z-art">${Object.entries(ZEITART_TEXT).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label>
      <label>Beginn <input id="z-beginn" type="datetime-local" value="${esc(lokalIso())}"></label><label>Ende <input id="z-ende" type="datetime-local" value="${esc(lokalIso(new Date(Date.now() + 3600000)))}"></label>
      <label>Notiz <input id="z-notiz"></label><button id="z-anlegen" class="leise">Eintragen</button></div><p id="z-status"></p></div>
    <div class="karte"><div class="tabelle-rahmen"><table><thead><tr><th>Beginn</th><th>Ende</th><th>Dauer</th><th>Monteur</th><th>Art</th><th>Quelle</th><th>Notiz</th></tr></thead><tbody>
      ${liste.map((z) => `<tr><td>${esc(zeit(z.beginn))}</td><td>${z.ende ? esc(UHRZEIT.format(new Date(z.ende))) : tag("läuft", "offen")}</td><td>${esc(dauerText(z.minuten))}</td><td>${esc(name(z.ressource_id))}</td><td>${esc(ZEITART_TEXT[z.art])}</td><td>${tag(z.quelle, "neutral")}</td><td>${esc(z.notiz || "")}</td></tr>`).join("") || `<tr><td colspan="7">Keine Zeiten in diesem Monat.</td></tr>`}</tbody></table></div></div>`;
  $("#z-monat").addEventListener("change", (ev) => { speicher("zeitenMonat", ev.target.value); zeigen(); });
  $("#z-export")?.addEventListener("click", () => dateiLaden(`/zeiten/lohnexport?monat=${monat}`, `lohnvorbereitung_${monat}.csv`));
  $("#z-anlegen").addEventListener("click", async () => {
    try {
      await api("/zeiten", { methode: "POST", daten: { art: $("#z-art").value, beginn: new Date($("#z-beginn").value).toISOString(), ende: new Date($("#z-ende").value).toISOString(),
        notiz: $("#z-notiz").value || null, ressource_id: $("#z-ressource")?.value || null } });
      zeigen();
    } catch (e) { $("#z-status").innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
}

// --- Bereitschaft und Fristen ------------------------------------------------------------------
const FRISTART_TEXT = { pruefung: "Prüfung", unterweisung: "Unterweisung", zertifikat: "Zertifikat", garantie: "Garantie", vertrag: "Vertrag", sonstiges: "Sonstiges" };
async function betriebAnsicht(ziel) {
  const [wir, ressourcen, bereitschaften, fristen] = await Promise.all([ich(), api("/ressourcen").catch(() => []), api("/bereitschaft"), api("/fristen?vorlauf_tage=365")]);
  const darf = wir.rechte.includes("bearbeiten");
  const name = (id) => ressourcen.find((r) => r.id === id)?.name.split(" (")[0] || "–";
  const heute = TAGESSCHLUESSEL.format(new Date());
  const tage = (d) => Math.round((Date.parse(d) - Date.parse(heute)) / 86400000);
  const naechsteWoche = new Date(); naechsteWoche.setDate(naechsteWoche.getDate() + ((5 - naechsteWoche.getDay() + 7) % 7 || 7)); naechsteWoche.setHours(16, 0, 0, 0);
  const ende = new Date(naechsteWoche); ende.setDate(ende.getDate() + 3); ende.setHours(7, 0, 0, 0);
  ziel.innerHTML = `<h1>Bereitschaft &amp; Fristen</h1><p class="unterzeile">Der Sprachagent nennt Anrufern bei Notfällen außerhalb der Bürozeit die eingetragene Bereitschaftsnummer. Fristen erinnern an Prüfungen (Gasgeräte, Leitern, Fahrzeuge), Unterweisungen und Zertifikate.</p>
    <div class="raster"><div class="karte"><h2>Bereitschaft</h2>
      <table><thead><tr><th>Von</th><th>Bis</th><th>Monteur</th><th>Telefon</th><th></th></tr></thead><tbody>${bereitschaften.map((b) => `<tr><td>${esc(zeit(b.von))}</td><td>${esc(zeit(b.bis))}</td><td>${esc(name(b.ressource_id))}</td><td>${esc(b.telefon)}</td><td>${darf ? `<button class="gefahr" data-bl="${esc(b.id)}">×</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="5">Keine Bereitschaft eingetragen.</td></tr>`}</tbody></table>
      ${darf ? `<div class="zeile" style="margin-top:12px"><label>Monteur <select id="bs-ressource">${ressourcen.map((r) => `<option value="${esc(r.id)}">${esc(r.name)}</option>`).join("")}</select></label>
        <label>Von <input id="bs-von" type="datetime-local" value="${esc(lokalIso(naechsteWoche))}"></label><label>Bis <input id="bs-bis" type="datetime-local" value="${esc(lokalIso(ende))}"></label>
        <label>Telefon <input id="bs-telefon" placeholder="+49 …"></label><button id="bs-anlegen" class="leise">Eintragen</button></div><p id="bs-status"></p>` : ""}</div>
    <div class="karte"><h2>Fristen (nächste 12 Monate)</h2>
      <table><thead><tr><th>Fällig</th><th>Art</th><th>Bezeichnung</th><th>Bezug</th><th></th></tr></thead><tbody>${fristen.map((f) => { const t = tage(f.faellig); return `<tr><td>${esc(DATUM.format(new Date(f.faellig)))}<br>${tag(t < 0 ? `${-t} Tage überfällig` : t <= 30 ? `in ${t} Tagen` : `in ${Math.round(t / 30)} Monaten`, t < 0 ? "ueberfaellig" : t <= 30 ? "bald_faellig" : "aktuell")}</td><td>${esc(FRISTART_TEXT[f.art])}</td><td>${esc(f.bezeichnung)}${f.hinweis ? `<br><small>${esc(f.hinweis)}</small>` : ""}${f.intervall_monate ? `<br><small>alle ${f.intervall_monate} Monate</small>` : ""}</td><td>${esc(f.bezug_typ === "ressource" ? name(f.bezug_id) : f.bezug_typ)}</td><td><div class="aktionen"><button class="leise" data-fe="${esc(f.id)}">Erledigt</button>${darf ? `<button class="gefahr" data-fl="${esc(f.id)}">×</button>` : ""}</div></td></tr>`; }).join("") || `<tr><td colspan="5">Keine offenen Fristen.</td></tr>`}</tbody></table>
      ${darf ? `<div class="zeile" style="margin-top:12px"><label>Art <select id="fr-art">${Object.entries(FRISTART_TEXT).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label>
        <label>Bezeichnung <input id="fr-bez" placeholder="z. B. UVV-Prüfung Transporter"></label><label>Fällig <input id="fr-faellig" type="date"></label><label>Intervall (Monate) <input id="fr-intervall" type="number" min="1" max="120"></label>
        <label>Bezug <select id="fr-bezug"><option value="betrieb">Betrieb</option><option value="fahrzeug">Fahrzeug</option>${ressourcen.map((r) => `<option value="ressource:${esc(r.id)}">${esc(r.name)}</option>`).join("")}</select></label>
        <button id="fr-anlegen" class="leise">Anlegen</button></div><p id="fr-status"></p>` : ""}</div></div>`;
  $("#bs-anlegen")?.addEventListener("click", async () => {
    try { await api("/bereitschaft", { methode: "POST", daten: { ressource_id: $("#bs-ressource").value, von: new Date($("#bs-von").value).toISOString(), bis: new Date($("#bs-bis").value).toISOString(), telefon: $("#bs-telefon").value } }); zeigen(); }
    catch (e) { $("#bs-status").innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
  ziel.querySelectorAll("[data-bl]").forEach((b) => b.addEventListener("click", async () => { try { await api(`/bereitschaft/${b.dataset.bl}`, { methode: "DELETE" }); zeigen(); } catch (e) { melden(e.message); } }));
  $("#fr-anlegen")?.addEventListener("click", async () => {
    const [typ, id] = $("#fr-bezug").value.split(":");
    if ($("#fr-bez").value.trim().length < 2 || !$("#fr-faellig").value) { $("#fr-status").innerHTML = `<span class="fehler">Bezeichnung und Fälligkeit angeben.</span>`; return; }
    try { await api("/fristen", { methode: "POST", daten: { art: $("#fr-art").value, bezeichnung: $("#fr-bez").value, faellig: $("#fr-faellig").value, intervall_monate: Number($("#fr-intervall").value) || null, bezug_typ: typ, bezug_id: id || null } }); zeigen(); }
    catch (e) { $("#fr-status").innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
  ziel.querySelectorAll("[data-fe]").forEach((b) => b.addEventListener("click", async () => { try { const r = await api(`/fristen/${b.dataset.fe}/erledigt`, { methode: "POST", daten: {} }); if (r.folgefrist) melden(`Folgefrist angelegt: fällig ${DATUM.format(new Date(r.folgefrist.faellig))}`); zeigen(); } catch (e) { melden(e.message); } }));
  ziel.querySelectorAll("[data-fl]").forEach((b) => b.addEventListener("click", async () => { try { await api(`/fristen/${b.dataset.fl}`, { methode: "DELETE" }); zeigen(); } catch (e) { melden(e.message); } }));
}

// Dokumente am Kunden (Angebote, Wartungsverträge, Pläne)
async function kundeDokumente(wurzel, id) {
  const wir = await ich();
  const liste = await api(`/anhaenge?bezug_typ=kunde&bezug_id=${id}`).catch(() => []);
  const teil = document.createElement("div");
  teil.innerHTML = `<h3 style="margin-top:16px">Dokumente</h3>${liste.length ? `<ul class="verlauf">${liste.map((a) => `<li>${tag(a.art, "neutral")} <a href="#" data-dok="${esc(a.id)}" data-name="${esc(a.name)}">${esc(a.name)}</a> · ${esc(Math.round(a.groesse / 1024))} KB · ${esc(zeit(a.hochgeladen))}${a.notiz ? ` · ${esc(a.notiz)}` : ""}</li>`).join("")}</ul>` : "<p>Keine Dokumente abgelegt.</p>"}
    ${wir.rechte.includes("einsatz") ? `<div class="zeile"><label>Datei <input id="kd-datei" type="file"></label><label>Notiz <input id="kd-notiz" placeholder="z. B. Angebot Wärmepumpe"></label><button class="leise" id="kd-hochladen">Ablegen</button></div><p id="kd-status"></p>` : ""}`;
  wurzel.append(teil);
  teil.querySelectorAll("[data-dok]").forEach((a) => a.addEventListener("click", (ev) => { ev.preventDefault(); dateiLaden(`/anhaenge/${a.dataset.dok}/inhalt`, a.dataset.name); }));
  $("#kd-hochladen", teil)?.addEventListener("click", async () => {
    const datei = $("#kd-datei", teil).files[0]; if (!datei) return melden("Datei wählen");
    const fd = new FormData(); fd.append("datei", datei); fd.append("bezug_typ", "kunde"); fd.append("bezug_id", id); fd.append("art", datei.type.startsWith("image/") ? "foto" : "dokument"); if ($("#kd-notiz", teil).value) fd.append("notiz", $("#kd-notiz", teil).value);
    try { await api("/anhaenge", { methode: "POST", daten: fd }); kundeDetail(id); } catch (e) { $("#kd-status", teil).innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
}

// --- Angebote: Artikelstamm, Positionen, Versand, Annahme, Abrechnung ------------------------
const ANGEBOTSTATUS_TEXT = { entwurf: "Entwurf", versendet: "Versendet", angenommen: "Angenommen", abgelehnt: "Abgelehnt", abgelaufen: "Abgelaufen" };
const ANGEBOTSTATUS_KLASSE = { entwurf: "neutral", versendet: "B", angenommen: "aktuell", abgelehnt: "abgelehnt", abgelaufen: "ueberfaellig" };
async function angeboteAnsicht(ziel) {
  const [liste, wir, kunden, artikel] = await Promise.all([api("/angebote"), ich(), api("/kunden?limit=500"), api("/artikel")]);
  const darf = wir.rechte.includes("bearbeiten");
  ziel.innerHTML = `<h1>Angebote</h1><p class="unterzeile">Eigener Nummernkreis. Versand ist kundenwirksam (Freigabe B), die Annahme erzeugt den Auftrag als Vorgang, die Rechnung aus dem Angebot entsteht über eine Freigabe C. Versendete Angebote sind preisgebunden und nicht mehr änderbar.</p>
    ${darf ? `<div class="karte" style="margin-bottom:16px"><h2>Neues Angebot</h2><form id="an-neu"><div class="raster">
      <label>Kunde <select name="kunde_id" required><option value="">– wählen –</option>${kunden.map((k) => `<option value="${esc(k.id)}">${esc(k.name)}${k.ort ? `, ${esc(k.ort)}` : ""}</option>`).join("")}</select></label>
      <label>Titel <input name="titel" placeholder="z. B. Austausch Umwälzpumpe"></label><label>Gültig bis <input name="gueltig_bis" type="date"></label>
      <label style="grid-column:1/-1">Einleitung <textarea name="einleitung" rows="2" placeholder="Sehr geehrte …, wie besprochen bieten wir an:"></textarea></label></div>
      <table id="an-positionen"><thead><tr><th>Artikel / Bezeichnung</th><th>Menge</th><th>Einheit</th><th>Einzelpreis netto</th><th>USt %</th><th></th></tr></thead><tbody></tbody></table>
      <div class="aktionen"><button type="button" class="leise" id="an-zeile">+ Position</button><button type="submit">Als Entwurf anlegen</button></div><p id="an-status"></p></form></div>` : ""}
    ${darf ? `<details class="karte" style="margin-bottom:16px"><summary><strong>BEG-Förderrechner</strong> <small>Heizungsförderung (KfW 458) und Einzelmaßnahmen (BAFA) als Richtwert für die Angebotseinleitung</small></summary>
      <form id="fr" class="raster" style="margin-top:10px">
        <label>Programm <select name="programm"><option value="heizung">Heizungstausch (KfW 458)</option><option value="einzelmassnahme">Einzelmaßnahme (BAFA BEG EM)</option></select></label>
        <label>Förderfähige Kosten brutto (€) <input name="kosten" type="number" step="0.01" min="1" required></label>
        <label>Wohneinheiten <input name="wohneinheiten" type="number" min="1" value="1"></label>
        <label>Heizungsart <select name="heizungsart"><option value="waermepumpe">Wärmepumpe</option><option value="biomasse">Biomasse</option><option value="solarthermie">Solarthermie</option><option value="fernwaerme">Wärmenetz</option><option value="hybrid">Hybrid</option><option value="brennstoffzelle">Brennstoffzelle</option></select></label>
        <label><span><input type="checkbox" name="selbstnutzend" checked> selbstnutzender Eigentümer</span></label>
        <label><span><input type="checkbox" name="alte_heizung_ersetzt"> fossile Altheizung wird ersetzt (Klimageschwindigkeits-Bonus)</span></label>
        <label><span><input type="checkbox" name="einkommen_unter_40k"> Haushaltseinkommen ≤ 40.000 € (Einkommens-Bonus)</span></label>
        <label><span><input type="checkbox" name="natuerliches_kaeltemittel"> natürliches Kältemittel / Erd- oder Wasserquelle (Effizienz-Bonus)</span></label>
        <label><span><input type="checkbox" name="isfp"> Maßnahme aus iSFP (Einzelmaßnahme +5 %)</span></label>
        <div class="aktionen" style="align-self:end"><button type="submit" class="leise">Berechnen</button></div></form><div id="fr-ergebnis"></div></details>` : ""}
    <div class="karte"><div class="tabelle-rahmen"><table><thead><tr><th>Nummer</th><th>Datum</th><th>Kunde</th><th>Titel</th><th style="text-align:right">Brutto</th><th>Gültig bis</th><th>Status</th><th></th></tr></thead><tbody>
    ${liste.map((a) => `<tr><td><strong>${esc(a.nummer)}</strong></td><td>${esc(DATUM.format(new Date(a.datum)))}</td><td>${esc(a.empfaenger.name || "")}</td><td>${esc(a.titel)}<br><small>${a.positionen.length} Positionen</small></td>
      <td style="text-align:right">${esc(EURO.format(a.brutto))}</td><td>${esc(DATUM.format(new Date(a.gueltig_bis)))}</td><td>${tag(a.status, ANGEBOTSTATUS_KLASSE[a.status], ANGEBOTSTATUS_TEXT[a.status])}${a.rechnung_id ? "<br><small>abgerechnet</small>" : ""}</td>
      <td><div class="aktionen"><button class="leise" data-pdf="${esc(a.id)}" data-nr="${esc(a.nummer)}">PDF</button>
        ${darf && ["entwurf", "versendet"].includes(a.status) ? `<button class="leise" data-senden="${esc(a.id)}">${a.status === "entwurf" ? "Versenden" : "Erneut senden"}</button>` : ""}
        ${darf && ["entwurf", "versendet"].includes(a.status) ? `<button data-annehmen="${esc(a.id)}">Angenommen</button><button class="gefahr" data-ablehnen="${esc(a.id)}">Abgelehnt</button>` : ""}
        ${darf && a.status === "angenommen" && !a.rechnung_id ? `<button data-rechnung="${esc(a.id)}">Rechnung</button>` : ""}
        ${a.servicefall_id ? `<button class="leise" data-fall="${esc(a.servicefall_id)}">Vorgang</button>` : ""}</div></td></tr>`).join("") || `<tr><td colspan="8">Noch keine Angebote.</td></tr>`}
    </tbody></table></div></div>`;
  $("#fr")?.addEventListener("submit", async (ev) => {
    ev.preventDefault(); const fd = new FormData(ev.target);
    const daten = { programm: fd.get("programm"), kosten: Number(fd.get("kosten")), wohneinheiten: Number(fd.get("wohneinheiten") || 1), heizungsart: fd.get("heizungsart") };
    for (const k of ["selbstnutzend", "alte_heizung_ersetzt", "einkommen_unter_40k", "isfp"]) daten[k] = fd.has(k);
    daten.natuerliches_kaeltemittel = fd.has("natuerliches_kaeltemittel");
    try {
      const e = await api("/foerderung/rechner", { methode: "POST", daten });
      $("#fr-ergebnis").innerHTML = `<p><strong>${esc(e.programm)}</strong>: Fördersatz ${e.foerdersatz_prozent} % auf ${esc(EURO.format(e.foerderfaehige_kosten))} (Deckel ${esc(EURO.format(e.kostendeckel))}) → Zuschuss <strong>${esc(EURO.format(e.zuschuss_gesamt))}</strong>, Eigenanteil ${esc(EURO.format(e.eigenanteil))}${e.gedeckelt_auf_maximum ? " · auf 70 % gedeckelt" : ""}</p>
        ${e.bausteine ? `<p><small>${e.bausteine.map((b) => `${esc(b.titel)} ${b.satz} %`).join(" · ")}</small></p>` : ""}
        <ul>${e.hinweise.map((h) => `<li><small>${esc(h)}</small></li>`).join("")}</ul><p><small>${esc(e.stand)}</small></p>
        <div class="aktionen"><button type="button" class="leise" id="fr-uebernehmen">Text in die Angebotseinleitung übernehmen</button></div>`;
      $("#fr-uebernehmen").addEventListener("click", () => { const ta = document.querySelector('#an-neu [name="einleitung"]'); if (ta) { ta.value = (ta.value ? ta.value + "\n\n" : "") + e.angebotstext; ta.scrollIntoView({ behavior: "smooth" }); } });
    } catch (err) { $("#fr-ergebnis").innerHTML = `<span class="fehler">${esc(err.message)}</span>`; }
  });
  const artikelOptionen = `<option value="">frei</option>${artikel.map((a) => `<option value="${esc(a.id)}" data-preis="${esc(a.verkauf_netto)}" data-einheit="${esc(a.einheit)}" data-ust="${esc(a.ust_satz)}" data-bez="${esc(a.bezeichnung)}">${esc(a.nummer)} ${esc(a.bezeichnung)} (${esc(EURO.format(a.verkauf_netto))})</option>`).join("")}`;
  const zeile = () => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><select name="artikel_id">${artikelOptionen}</select><input name="bezeichnung" placeholder="Bezeichnung" style="margin-top:4px"></td><td><input name="menge" type="number" step="0.01" value="1" style="width:5em"></td>
      <td><input name="einheit" value="Stk" style="width:5em"></td><td><input name="einzelpreis_netto" type="number" step="0.01" style="width:7em"></td><td><input name="ust_satz" value="19" style="width:4em"></td><td><button type="button" class="gefahr" data-weg>×</button></td>`;
    tr.querySelector("select").addEventListener("change", (ev) => { const o = ev.target.selectedOptions[0]; if (!o.value) return;
      tr.querySelector("[name=bezeichnung]").value = o.dataset.bez; tr.querySelector("[name=einzelpreis_netto]").value = o.dataset.preis; tr.querySelector("[name=einheit]").value = o.dataset.einheit; tr.querySelector("[name=ust_satz]").value = o.dataset.ust; });
    tr.querySelector("[data-weg]").addEventListener("click", () => tr.remove());
    $("#an-positionen tbody").append(tr);
  };
  $("#an-zeile")?.addEventListener("click", zeile); if (darf) zeile();
  $("#an-neu")?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target); const daten = { kunde_id: fd.get("kunde_id"), titel: fd.get("titel") || null, einleitung: fd.get("einleitung") || null, gueltig_bis: fd.get("gueltig_bis") || null, positionen: [] };
    for (const tr of $("#an-positionen tbody").rows) {
      const w = (n) => tr.querySelector(`[name=${n}]`).value;
      const p = { menge: w("menge"), einheit: w("einheit"), ust_satz: w("ust_satz") };
      if (w("artikel_id")) p.artikel_id = w("artikel_id"); else p.bezeichnung = w("bezeichnung");
      if (w("einzelpreis_netto")) p.einzelpreis_netto = w("einzelpreis_netto");
      if (w("bezeichnung")) p.bezeichnung = w("bezeichnung");
      daten.positionen.push(p);
    }
    try { const a = await api("/angebote", { methode: "POST", daten }); $("#an-status").innerHTML = `<span class="hinweis">Angebot ${esc(a.nummer)} angelegt (${esc(EURO.format(a.brutto))} brutto)</span>`; zeigen(); }
    catch (e) { $("#an-status").innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
  ziel.querySelectorAll("[data-pdf]").forEach((b) => b.addEventListener("click", () => dateiLaden(`/angebote/${b.dataset.pdf}/pdf`, `${b.dataset.nr}.pdf`)));
  ziel.querySelectorAll("[data-fall]").forEach((b) => b.addEventListener("click", () => fallDetail(b.dataset.fall)));
  const aktion = (attr, pfad, meldung, koerper) => ziel.querySelectorAll(`[${attr}]`).forEach((b) => b.addEventListener("click", async () => {
    try { const d = koerper ? koerper() : undefined; if (d === null) return; await api(`/angebote/${b.getAttribute(attr)}/${pfad}`, { methode: "POST", daten: d }); if (meldung) melden(meldung); zeigen(); } catch (e) { melden(e.message); }
  }));
  aktion("data-senden", "senden", "Versand liegt in der Freigabe-Inbox (Klasse B).");
  aktion("data-annehmen", "annehmen", "Auftrag angelegt; die Auftragsbestätigung liegt in der Freigabe-Inbox.");
  aktion("data-ablehnen", "ablehnen", null, () => { const g = prompt("Grund der Ablehnung (optional):"); return g === null ? null : { grund: g || null }; });
  aktion("data-rechnung", "rechnung", "Rechnung liegt in der Freigabe-Inbox (Klasse C).");
}

// --- Offene Posten: Zahlungen, Bankimport, Mahnwesen -----------------------------------------
const ZAHLART_TEXT = { ueberweisung: "Überweisung", bar: "Bar", karte: "Karte", skonto: "Skonto", ausbuchung: "Ausbuchung" };
const MAHNSTUFE_TEXT = { 0: "–", 1: "Zahlungserinnerung", 2: "Mahnung", 3: "Letzte Mahnung" };
async function forderungenAnsicht(ziel) {
  const [posten, zahlungen, mahnungen, wir] = await Promise.all([api("/offene-posten"), api("/zahlungen?limit=50"), api("/mahnungen"), ich()]);
  const summe = posten.reduce((s, p) => s + Number(p.offen), 0), ueberfaellig = posten.filter((p) => p.tage_ueberfaellig > 0);
  ziel.innerHTML = `<h1>Offene Posten</h1><p class="unterzeile">Zahlungen manuell oder per CSV-Import aus dem Online-Banking (Rechnungsnummer im Verwendungszweck). Der Mahnlauf erzeugt nur Vorschläge: Zahlungserinnerung als Freigabe B, Mahnungen mit Gebühr als Freigabe C.</p>
    <div class="kacheln"><div class="kachel"><div class="wert">${esc(EURO.format(summe))}</div><div class="bez">offen (${posten.length} Rechnungen)</div></div>
      <div class="kachel"><div class="wert">${esc(EURO.format(ueberfaellig.reduce((s, p) => s + Number(p.offen), 0)))}</div><div class="bez">überfällig (${ueberfaellig.length})</div></div>
      <div class="kachel"><div class="wert">${posten.filter((p) => p.mahnstufe > 0).length}</div><div class="bez">gemahnt</div></div></div>
    <div class="karte" style="margin:16px 0"><div class="zeile"><label>Bankumsätze (CSV) <input id="op-csv" type="file" accept=".csv,text/csv"></label><button id="op-import" class="leise">Importieren und buchen</button>
      <button id="op-mahnlauf">Mahnlauf starten</button></div><p id="op-status"></p></div>
    <div class="karte" style="margin-bottom:16px"><h2>Offene Rechnungen</h2><div class="tabelle-rahmen"><table><thead><tr><th>Rechnung</th><th>Kunde</th><th>Fällig</th><th style="text-align:right">Brutto</th><th style="text-align:right">Offen</th><th>Mahnstufe</th><th></th></tr></thead><tbody>
      ${posten.map((p) => `<tr><td><strong>${esc(p.nummer)}</strong><br><small>${tag(p.status, p.status === "teilbezahlt" ? "B" : "neutral", p.status)}</small></td><td>${esc(p.kunde || "")}</td>
        <td>${esc(DATUM.format(new Date(p.faellig)))}${p.tage_ueberfaellig ? `<br>${tag(`${p.tage_ueberfaellig} Tage überfällig`, "ueberfaellig")}` : ""}</td>
        <td style="text-align:right">${esc(EURO.format(p.brutto))}</td><td style="text-align:right"><strong>${esc(EURO.format(p.offen))}</strong></td>
        <td>${esc(MAHNSTUFE_TEXT[p.mahnstufe])}${p.mahnfrist ? `<br><small>Frist ${esc(DATUM.format(new Date(p.mahnfrist)))}</small>` : ""}</td>
        <td><div class="aktionen"><button class="leise" data-zahlung="${esc(p.rechnung_id)}" data-offen="${esc(p.offen)}" data-nr="${esc(p.nummer)}">Zahlung buchen</button></div></td></tr>`).join("") || `<tr><td colspan="7">Keine offenen Posten.</td></tr>`}
    </tbody></table></div></div>
    <div class="raster"><div class="karte"><h2>Letzte Zahlungen</h2><table><thead><tr><th>Datum</th><th>Rechnung</th><th style="text-align:right">Betrag</th><th>Art</th><th>Quelle</th></tr></thead><tbody>
      ${zahlungen.map((z) => `<tr><td>${esc(DATUM.format(new Date(z.datum)))}</td><td><strong>${esc(z.rechnung_nummer || "")}</strong>${z.referenz ? `<br><small>${esc(z.referenz)}</small>` : ""}</td><td style="text-align:right">${esc(EURO.format(z.betrag))}</td><td>${esc(ZAHLART_TEXT[z.art])}</td><td>${esc(z.quelle)}</td></tr>`).join("") || `<tr><td colspan="5">Keine Zahlungen.</td></tr>`}</tbody></table></div>
    <div class="karte"><h2>Mahnungen</h2><table><thead><tr><th>Datum</th><th>Stufe</th><th style="text-align:right">Offen</th><th>Frist</th><th></th></tr></thead><tbody>
      ${mahnungen.map((m) => `<tr><td>${esc(DATUM.format(new Date(m.datum)))}</td><td>${esc(MAHNSTUFE_TEXT[m.stufe])}${Number(m.gebuehr) ? `<br><small>Gebühr ${esc(EURO.format(m.gebuehr))}</small>` : ""}</td><td style="text-align:right">${esc(EURO.format(m.offen))}</td><td>${esc(DATUM.format(new Date(m.frist)))}</td><td><button class="leise" data-mpdf="${esc(m.id)}">PDF</button></td></tr>`).join("") || `<tr><td colspan="5">Keine Mahnungen.</td></tr>`}</tbody></table></div></div>`;
  const status = (html) => { $("#op-status").innerHTML = html; };
  $("#op-import").addEventListener("click", async () => {
    const datei = $("#op-csv").files[0]; if (!datei) return status(`<span class="fehler">CSV-Datei wählen</span>`);
    const fd = new FormData(); fd.append("datei", datei);
    try { const e = await api("/zahlungen/bankimport", { methode: "POST", daten: fd });
      status(`<span class="hinweis">${e.zugeordnet.length} Zahlungen gebucht, ${e.unklar.length} ohne Rechnungsbezug, ${e.uebersprungen} übersprungen.</span>${e.unklar.length ? `<ul class="verlauf">${e.unklar.map((u) => `<li>${esc(u.datum || "")} · ${esc(EURO.format(u.betrag))} · ${esc(u.zweck || "")}</li>`).join("")}</ul>` : ""}`);
      setTimeout(zeigen, 2500);
    } catch (e) { status(`<span class="fehler">${esc(e.message)}</span>`); }
  });
  $("#op-mahnlauf").addEventListener("click", async () => {
    try { const v = await api("/mahnlauf", { methode: "POST" }); status(`<span class="hinweis">${v.length ? `${v.length} Vorschläge liegen in der Freigabe-Inbox: ${esc(v.map((x) => `${x.rechnung} (${MAHNSTUFE_TEXT[x.stufe]})`).join(", "))}` : "Keine Mahnung fällig."}</span>`); freigabenZaehlen(); }
    catch (e) { status(`<span class="fehler">${esc(e.message)}</span>`); }
  });
  ziel.querySelectorAll("[data-zahlung]").forEach((b) => b.addEventListener("click", async () => {
    const betrag = prompt(`Zahlung zu ${b.dataset.nr} (offen ${EURO.format(b.dataset.offen)}). Betrag in €:`, b.dataset.offen); if (!betrag) return;
    const art = prompt("Art: ueberweisung, bar, karte, skonto oder ausbuchung", "ueberweisung") || "ueberweisung";
    try { await api("/zahlungen", { methode: "POST", daten: { rechnung_id: b.dataset.zahlung, betrag: Number(betrag.replace(",", ".")), art } }); zeigen(); } catch (e) { melden(e.message); }
  }));
  ziel.querySelectorAll("[data-mpdf]").forEach((b) => b.addEventListener("click", () => dateiLaden(`/mahnungen/${b.dataset.mpdf}/pdf`, "Mahnung.pdf")));
}

// --- Wartungsverträge ------------------------------------------------------------------------
async function vertraegeAnsicht(ziel) {
  const [liste, wir, kunden] = await Promise.all([api("/vertraege"), ich(), api("/kunden?limit=500")]);
  const darf = wir.rechte.includes("bearbeiten");
  const volumen = liste.filter((v) => v.status === "aktiv").reduce((s, v) => s + Number(v.preis_jahr_netto), 0);
  ziel.innerHTML = `<h1>Wartungsverträge</h1><p class="unterzeile">Laufzeit, Kündigungsfrist und automatische Verlängerung; der Tageslauf legt die periodische Abrechnung als Freigabe C vor und führt die Kündigungsfrist als Frist. Vertragskunden erscheinen in der Wartungsplanung mit Vertragsstatus.</p>
    <div class="kacheln"><div class="kachel"><div class="wert">${liste.filter((v) => v.status === "aktiv").length}</div><div class="bez">aktive Verträge</div></div><div class="kachel"><div class="wert">${esc(EURO.format(volumen))}</div><div class="bez">Jahresvolumen netto</div></div></div>
    ${darf ? `<div class="karte" style="margin:16px 0"><h2>Neuer Vertrag</h2><form id="vt-neu"><div class="raster">
      <label>Kunde <select name="kunde_id" required><option value="">– wählen –</option>${kunden.map((k) => `<option value="${esc(k.id)}">${esc(k.name)}${k.ort ? `, ${esc(k.ort)}` : ""}</option>`).join("")}</select></label>
      <label>Bezeichnung <input name="bezeichnung" value="Wartungsvertrag Heizung"></label><label>Beginn <input name="beginn" type="date" value="${esc(TAGESSCHLUESSEL.format(new Date()))}"></label>
      <label>Preis je Jahr netto (€) <input name="preis_jahr_netto" type="number" step="0.01" required></label><label>Laufzeit (Monate) <input name="laufzeit_monate" type="number" value="12"></label>
      <label>Kündigungsfrist (Monate) <input name="kuendigungsfrist_monate" type="number" value="3"></label><label>Verlängerung (Monate) <input name="verlaengerung_monate" type="number" value="12"></label>
      <label>Abrechnung alle (Monate) <input name="abrechnung_monate" type="number" value="12" min="1" max="12"></label>
      <label style="grid-column:1/-1">Leistungen <textarea name="leistungen" rows="2" placeholder="jährliche Wartung, Abgasmessung, Prüfung Ausdehnungsgefäß …"></textarea></label></div>
      <div class="aktionen"><button type="submit">Anlegen</button></div><p id="vt-status"></p></form></div>` : ""}
    <div class="karte"><div class="tabelle-rahmen"><table><thead><tr><th>Kunde</th><th>Vertrag</th><th>Beginn</th><th>Laufzeit bis</th><th>Kündbar bis</th><th>Nächste Abrechnung</th><th style="text-align:right">Preis/Jahr netto</th><th>Status</th><th></th></tr></thead><tbody>
    ${liste.map((v) => `<tr><td><strong>${esc(v.kunde)}</strong></td><td>${esc(v.bezeichnung)}${v.leistungen ? `<br><small>${esc(v.leistungen)}</small>` : ""}</td><td>${esc(DATUM.format(new Date(v.beginn)))}</td>
      <td>${esc(DATUM.format(new Date(v.laufzeitende)))}</td><td>${esc(DATUM.format(new Date(v.kuendigungstermin)))}</td><td>${esc(DATUM.format(new Date(v.naechste_abrechnung)))}</td>
      <td style="text-align:right">${esc(EURO.format(v.preis_jahr_netto))}</td><td>${tag(v.status, v.status === "aktiv" ? "aktuell" : "abgelehnt", v.status)}${v.gekuendigt_zum ? `<br><small>zum ${esc(DATUM.format(new Date(v.gekuendigt_zum)))}</small>` : ""}</td>
      <td>${darf && v.status === "aktiv" ? `<button class="gefahr" data-kuendigen="${esc(v.id)}">Kündigen</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="9">Keine Verträge.</td></tr>`}
    </tbody></table></div></div>`;
  $("#vt-neu")?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const daten = {}; for (const [k, v] of new FormData(ev.target).entries()) if (v !== "") daten[k] = ["preis_jahr_netto", "laufzeit_monate", "kuendigungsfrist_monate", "verlaengerung_monate", "abrechnung_monate"].includes(k) ? Number(v) : v;
    try { await api("/vertraege", { methode: "POST", daten }); zeigen(); } catch (e) { $("#vt-status").innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
  ziel.querySelectorAll("[data-kuendigen]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Vertrag zum nächstmöglichen Termin kündigen?")) return;
    try { const v = await api(`/vertraege/${b.dataset.kuendigen}/kuendigen`, { methode: "POST", daten: {} }); melden(`Gekündigt zum ${DATUM.format(new Date(v.gekuendigt_zum))}`); zeigen(); } catch (e) { melden(e.message); }
  }));
}

// --- Anfragen (Webformular, Postfach) --------------------------------------------------------
const ANFRAGESTATUS_TEXT = { neu: "Büro-Aufgabe", uebernommen: "Vorgang angelegt", verworfen: "Verworfen" };
async function anfragenAnsicht(ziel) {
  const [liste, wir] = await Promise.all([api("/anfragen"), ich()]);
  ziel.innerHTML = `<h1>Anfragen</h1><p class="unterzeile">Schriftlicher Eingang über das Kontaktformular der Website und das E-Mail-Postfach. Bestandskunden werden an Telefonnummer oder E-Mail erkannt; aus jeder Anfrage mit Rückkanal entsteht ein Vorgang mit Dringlichkeitsbewertung. Einrichtung unter Einstellungen.</p>
    <div class="karte" style="margin-bottom:16px"><div class="zeile"><button id="af-abrufen" class="leise" ${wir.rechte.includes("bearbeiten") ? "" : "disabled"}>Postfach jetzt abrufen</button><span id="af-status"></span></div></div>
    <div class="karte"><div class="tabelle-rahmen"><table><thead><tr><th>Eingang</th><th>Kanal</th><th>Absender</th><th>Betreff / Nachricht</th><th>Status</th><th></th></tr></thead><tbody>
    ${liste.map((a) => `<tr><td>${esc(zeit(a.eingegangen))}</td><td>${tag(a.kanal, "neutral", a.kanal === "web" ? "Webformular" : "E-Mail")}</td><td>${esc(a.name || "–")}<br><small>${esc(a.telefon || "")} ${esc(a.email || "")}</small></td>
      <td>${a.betreff ? `<strong>${esc(a.betreff)}</strong><br>` : ""}<small>${esc((a.nachricht || "").slice(0, 200))}</small></td><td>${tag(a.status, a.status === "uebernommen" ? "aktuell" : a.status === "neu" ? "offen" : "neutral", ANFRAGESTATUS_TEXT[a.status])}</td>
      <td>${a.servicefall_id ? `<button class="leise" data-fall="${esc(a.servicefall_id)}">Vorgang</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="6">Noch keine Anfragen. Webformular und Postfach richten Sie unter Einstellungen ein.</td></tr>`}
    </tbody></table></div></div>`;
  ziel.querySelectorAll("[data-fall]").forEach((b) => b.addEventListener("click", () => fallDetail(b.dataset.fall)));
  $("#af-abrufen").addEventListener("click", async () => {
    try { const e = await api("/posteingang/abrufen", { methode: "POST" }); $("#af-status").innerHTML = `<span class="hinweis">${e.gelesen} gelesen, ${e.uebernommen} Vorgänge, ${e.bekannt} bekannt, ${e.verworfen} verworfen</span>`; setTimeout(zeigen, 2000); }
    catch (e) { $("#af-status").innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
}

// --- Auswertungen ----------------------------------------------------------------------------
async function auswertungenAnsicht(ziel) {
  const a = await api("/auswertungen?monate=12");
  const max = Math.max(1, ...a.monate.map((m) => Math.max(Number(m.umsatz_netto), Number(m.zahlungen))));
  const balken = (w, klasse) => `<div class="saeule-teil ${klasse}" style="height:${Math.round(Number(w) / max * 100)}%" title="${esc(EURO.format(w))}"></div>`;
  ziel.innerHTML = `<h1>Auswertungen</h1><p class="unterzeile">Zeitraum ${esc(DATUM.format(new Date(a.von)))} bis ${esc(DATUM.format(new Date(a.bis)))}. Umsatz aus gestellten Rechnungen (netto), Zahlungseingang aus gebuchten Zahlungen, Material aus zugeordneten Belegen. Deckungsbeitrag je Monteur: Umsatz der rapportierten Aufträge abzüglich Material und interner Lohnkosten (Rapportzeiten × Stundensatz intern).</p>
    <div class="kacheln"><div class="kachel"><div class="wert">${esc(EURO.format(a.umsatz_netto_gesamt))}</div><div class="bez">Umsatz netto (12 Monate)</div></div>
      <div class="kachel"><div class="wert">${esc(EURO.format(a.offene_posten.summe))}</div><div class="bez">offene Posten (${a.offene_posten.anzahl}, davon ${esc(EURO.format(a.offene_posten.ueberfaellig))} überfällig)</div></div>
      <div class="kachel"><div class="wert">${a.angebote.quote === null ? "–" : `${Math.round(a.angebote.quote * 100)} %`}</div><div class="bez">Angebotsquote (${a.angebote.angenommen} von ${a.angebote.entschieden} entschieden; ${esc(EURO.format(a.angebote.volumen_offen_netto))} offen)</div></div>
      <div class="kachel"><div class="wert">${a.vertraege.aktive}</div><div class="bez">Wartungsverträge · ${esc(EURO.format(a.vertraege.jahresvolumen_netto))}/Jahr</div></div></div>
    <div class="karte" style="margin:16px 0"><h2>Umsatz und Zahlungseingang je Monat</h2><div class="diagramm">${a.monate.map((m) => `<div class="saeule"><div class="saeule-balken">${balken(m.umsatz_netto, "umsatz")}${balken(m.zahlungen, "zahlung")}</div><div class="saeule-bez">${esc(m.monat.slice(5))}/${esc(m.monat.slice(2, 4))}</div></div>`).join("")}</div>
      <p class="unterzeile"><span class="legende umsatz"></span> Umsatz netto <span class="legende zahlung"></span> Zahlungseingang</p>
      <div class="tabelle-rahmen"><table><thead><tr><th>Monat</th><th style="text-align:right">Umsatz netto</th><th style="text-align:right">Zahlungen</th><th style="text-align:right">Material (Belege)</th></tr></thead><tbody>
      ${a.monate.map((m) => `<tr><td>${esc(m.monat)}</td><td style="text-align:right">${esc(EURO.format(m.umsatz_netto))}</td><td style="text-align:right">${esc(EURO.format(m.zahlungen))}</td><td style="text-align:right">${esc(EURO.format(m.material_netto))}</td></tr>`).join("")}</tbody></table></div></div>
    <div class="raster"><div class="karte"><h2>Deckungsbeitrag je Monteur</h2><table><thead><tr><th>Monteur</th><th>Rapporte</th><th>Stunden</th><th style="text-align:right">Umsatz</th><th style="text-align:right">Material</th><th style="text-align:right">Lohn</th><th style="text-align:right">DB</th></tr></thead><tbody>
      ${a.monteure.map((m) => `<tr><td>${esc(m.name)}</td><td>${m.rapporte}</td><td>${esc(m.stunden)}</td><td style="text-align:right">${esc(EURO.format(m.umsatz_netto))}</td><td style="text-align:right">${esc(EURO.format(m.material_netto))}</td><td style="text-align:right">${esc(EURO.format(m.lohnkosten))}</td><td style="text-align:right"><strong>${esc(EURO.format(m.deckungsbeitrag))}</strong></td></tr>`).join("") || `<tr><td colspan="7">Keine Rapporte im Zeitraum.</td></tr>`}</tbody></table></div>
    <div class="karte"><h2>Anfragen je Kanal</h2><table><tbody>${Object.entries(a.anfragen_je_kanal || {}).map(([k, n]) => `<tr><td>${esc(k)}</td><td style="text-align:right">${n}</td></tr>`).join("") || "<tr><td>Keine Anfragen.</td></tr>"}</tbody></table>
      ${a.zeiteintraege ? `<h2 style="margin-top:16px">Erfasste Zeiten</h2><table><tbody>${Object.entries(a.zeiteintraege).map(([k, v]) => `<tr><td>${esc(ZEITART_TEXT[k] || k)}</td><td style="text-align:right">${esc(String(v))} h</td></tr>`).join("")}</tbody></table>` : ""}</div></div>`;
}

// --- Abwesenheiten in der Plantafel ----------------------------------------------------------
const ABWESENHEIT_TEXT = { urlaub: "Urlaub", krank: "Krank", schulung: "Schulung", sonstiges: "Abwesend" };
async function abwesenheitenVerwalten(ziel, ressourcen) {
  const liste = await api("/abwesenheiten").catch(() => []);
  const teil = document.createElement("div"); teil.className = "karte"; teil.style.marginTop = "16px";
  teil.innerHTML = `<h2>Abwesenheiten</h2><p class="unterzeile">Urlaub, Krankheit und Schulung blockieren den Monteur ganztägig in der Disposition und im Sprachagenten.</p>
    <table><thead><tr><th>Monteur</th><th>Von</th><th>Bis</th><th>Art</th><th></th></tr></thead><tbody>${liste.map((a) => `<tr><td>${esc(ressourcen.find((r) => r.id === a.ressource_id)?.name.split(" (")[0] || "–")}</td><td>${esc(DATUM.format(new Date(a.von)))}</td><td>${esc(DATUM.format(new Date(a.bis)))}</td><td>${esc(ABWESENHEIT_TEXT[a.art])}${a.notiz ? ` · ${esc(a.notiz)}` : ""}</td><td><button class="gefahr" data-al="${esc(a.id)}">×</button></td></tr>`).join("") || `<tr><td colspan="5">Keine Abwesenheiten eingetragen.</td></tr>`}</tbody></table>
    <div class="zeile" style="margin-top:12px"><label>Monteur <select id="ab-ressource">${ressourcen.map((r) => `<option value="${esc(r.id)}">${esc(r.name)}</option>`).join("")}</select></label>
      <label>Von <input id="ab-von" type="date"></label><label>Bis <input id="ab-bis" type="date"></label><label>Art <select id="ab-art">${Object.entries(ABWESENHEIT_TEXT).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label>
      <button id="ab-anlegen" class="leise">Eintragen</button></div><p id="ab-status"></p>`;
  ziel.append(teil);
  $("#ab-anlegen", teil).addEventListener("click", async () => {
    if (!$("#ab-von", teil).value) { $("#ab-status", teil).innerHTML = `<span class="fehler">Datum „Von“ angeben.</span>`; return; }
    try { await api("/abwesenheiten", { methode: "POST", daten: { ressource_id: $("#ab-ressource", teil).value, von: $("#ab-von", teil).value, bis: $("#ab-bis", teil).value || $("#ab-von", teil).value, art: $("#ab-art", teil).value } }); zeigen(); }
    catch (e) { $("#ab-status", teil).innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
  teil.querySelectorAll("[data-al]").forEach((b) => b.addEventListener("click", async () => { try { await api(`/abwesenheiten/${b.dataset.al}`, { methode: "DELETE" }); zeigen(); } catch (e) { melden(e.message); } }));
}

// --- Einstellungen: Artikelstamm, Webformular, Postfach -------------------------------------
const ARTIKELKATEGORIE_TEXT = { material: "Material", arbeit: "Arbeit", fahrt: "Fahrt", pauschale: "Pauschale", leistung: "Leistung (buchbar)" };
async function einstellungenErgaenzen(ziel) {
  const [artikel, web, post, betrieb, scan] = await Promise.all([api("/artikel?alle=true"), api("/betrieb/webformular"), api("/betrieb/posteingang"), api("/betrieb"), api("/betrieb/scanner")]);
  const teil = document.createElement("div");
  teil.innerHTML = `<div class="karte" style="margin-bottom:16px"><h2>Artikel- und Leistungsstamm</h2><p class="unterzeile">Material, Arbeitszeit, Fahrt und Pauschalen für Angebots- und Rechnungspositionen. Der Einkaufspreis dient der Nachkalkulation.</p>
      <div class="tabelle-rahmen"><table><thead><tr><th>Nummer</th><th>Bezeichnung</th><th>Kategorie</th><th>Einheit</th><th style="text-align:right">Einkauf netto</th><th style="text-align:right">Verkauf netto</th><th>USt</th><th>Aktiv</th></tr></thead><tbody>
      ${artikel.map((a) => `<tr><td>${esc(a.nummer)}</td><td>${esc(a.bezeichnung)}${a.lieferant ? `<br><small>${esc(a.lieferant)}</small>` : ""}</td><td>${esc(ARTIKELKATEGORIE_TEXT[a.kategorie])}</td><td>${esc(a.einheit)}</td><td style="text-align:right">${a.einkauf_netto ? esc(EURO.format(a.einkauf_netto)) : "–"}</td><td style="text-align:right">${esc(EURO.format(a.verkauf_netto))}</td><td>${esc(a.ust_satz)} %</td><td><input type="checkbox" data-aktiv="${esc(a.id)}" ${a.aktiv ? "checked" : ""}></td></tr>`).join("") || `<tr><td colspan="8">Noch keine Artikel.</td></tr>`}</tbody></table></div>
      <form id="art-neu" class="zeile" style="margin-top:12px"><label>Nummer <input name="nummer" required style="width:6em"></label><label>Bezeichnung <input name="bezeichnung" required></label>
        <label>Kategorie <select name="kategorie">${Object.entries(ARTIKELKATEGORIE_TEXT).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label><label>Einheit <input name="einheit" value="Stk" style="width:4em"></label>
        <label>Einkauf <input name="einkauf_netto" type="number" step="0.01" style="width:6em"></label><label>Verkauf <input name="verkauf_netto" type="number" step="0.01" required style="width:6em"></label><label>Lieferant <input name="lieferant"></label>
        <label>Dauer (min, Leistung) <input name="dauer_minuten" type="number" min="5" style="width:5em"></label><label>Qualifikation <input name="qualifikation" style="width:7em"></label>
        <button type="submit" class="leise">Anlegen</button></form><p id="art-status"></p></div>
    <div class="karte" style="margin-bottom:16px"><h2>Kontaktformular der Website</h2><p class="unterzeile">Öffentlicher Endpunkt mit Token; Anfragen werden Vorgänge. Das Formular enthält ein unsichtbares Feld gegen Bots, der Endpunkt ist gedrosselt.</p>
      <p>${web.aktiv ? `Aktiv: <code>${esc(web.url)}</code>` : "Nicht aktiv."}</p>${web.aktiv ? `<textarea readonly rows="7" style="width:100%;font-family:monospace;font-size:12px">${esc(web.snippet)}</textarea>` : ""}
      <div class="zeile" style="margin-top:8px"><label>Danke-Seite (URL nach dem Absenden, optional) <input id="wf-danke" value="${esc(betrieb.stammdaten.webformular_danke_url || "")}"></label>
        <button class="leise" id="wf-an">${web.aktiv ? "Token erneuern" : "Aktivieren"}</button>${web.aktiv ? `<button class="gefahr" id="wf-aus">Abschalten</button>` : ""}</div></div>
    <div class="karte" style="margin-bottom:16px"><h2>E-Mail-Posteingang (IMAP)</h2><p class="unterzeile">Ungelesene Nachrichten des Anfragepostfachs werden täglich bzw. auf Knopfdruck zu Vorgängen; Autoreplies und Newsletter werden verworfen. Zugangsdaten werden verschlüsselt gespeichert.</p>
      <form id="pe-form"><div class="raster"><label>IMAP-Server <input name="host" value="${esc(post.host || "")}"></label><label>Port <input name="port" type="number" value="${esc(post.port || 993)}"></label><label>Benutzer <input name="benutzer" value="${esc(post.benutzer || "")}"></label>
        <label>Passwort <input name="passwort" type="password" autocomplete="new-password" value="${esc(post.passwort || "")}"></label><label>Ordner <input name="ordner" value="${esc(post.ordner || "INBOX")}"></label></div>
      <div class="aktionen"><span id="pe-status"></span><button type="submit">Speichern</button></div></form></div>
    <div class="karte" style="margin-bottom:16px"><h2>Scanner-Eingang</h2><p class="unterzeile">Scan-to-Mail: der Scanner mailt an ein eigenes Postfach, dessen Anhänge (PDF, JPEG, XML) alle 15 Minuten zu Belegen werden; nur die eingetragenen Absender zählen. Eingangsordner: Netzlaufwerk des Scanners, auf dem Server eingehängt; verarbeitete Dateien wandern nach verarbeitet/.</p>
      <form id="sc-form"><div class="raster"><label>IMAP-Server (Scanner-Postfach) <input name="host" value="${esc(scan.host || "")}"></label><label>Port <input name="port" type="number" value="${esc(scan.port || 993)}"></label><label>Benutzer <input name="benutzer" value="${esc(scan.benutzer || "")}"></label>
        <label>Passwort <input name="passwort" type="password" autocomplete="new-password" value="${esc(scan.passwort || "")}"></label><label>Ordner <input name="ordner" value="${esc(scan.ordner || "INBOX")}"></label>
        <label>Erlaubte Absender (Komma, Adresse oder Domain) <input name="erlaubte_absender" value="${esc(scan.erlaubte_absender || "")}" placeholder="scanner@betrieb.de, @betrieb.de"></label>
        <label>Eingangsordner auf dem Server <input name="eingangsordner" value="${esc(scan.eingangsordner || "")}" placeholder="/srv/scans/${esc(betrieb.id?.slice(0, 8) || "betrieb")}"></label>
        <label>Standardkategorie <select name="kategorie">${Object.entries(KATEGORIE_TEXT).map(([k, v]) => `<option value="${k}" ${k === (scan.kategorie || "material") ? "selected" : ""}>${v}</option>`).join("")}</select></label></div>
      <p>Übergabe geprüfter Belege: ${esc(Object.entries(scan.uebergabekanaele || {}).map(([z, k]) => `${z} → ${k.join(", ")}`).join(" · ") || "kein Kanal angebunden (oben unter Buchhaltung bzw. Steuerkanzlei einrichten)")}</p>
      <div class="aktionen"><span id="sc-status"></span><button type="submit">Speichern</button></div></form></div>`;
  ziel.append(teil);
  $("#sc-form", teil).addEventListener("submit", async (ev) => {
    ev.preventDefault(); const daten = Object.fromEntries(new FormData(ev.target).entries()); daten.port = Number(daten.port) || 993;
    try { await api("/betrieb/scanner", { methode: "PUT", daten }); $("#sc-status", teil).innerHTML = `<span class="hinweis">Gespeichert</span>`; } catch (e) { $("#sc-status", teil).innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
  $("#art-neu", teil).addEventListener("submit", async (ev) => {
    ev.preventDefault(); const daten = {}; for (const [k, v] of new FormData(ev.target).entries()) if (v !== "") daten[k] = ["einkauf_netto", "verkauf_netto", "dauer_minuten"].includes(k) ? Number(v) : v;
    try { await api("/artikel", { methode: "POST", daten }); zeigen(); } catch (e) { $("#art-status", teil).innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
  teil.querySelectorAll("[data-aktiv]").forEach((c) => c.addEventListener("change", async () => { try { await api(`/artikel/${c.dataset.aktiv}`, { methode: "PATCH", daten: { aktiv: c.checked } }); } catch (e) { melden(e.message); } }));
  $("#wf-an", teil).addEventListener("click", async () => { try { await api("/betrieb/webformular", { methode: "PUT", daten: { aktiv: true, erneuern: web.aktiv, danke_url: $("#wf-danke", teil).value || null } }); zeigen(); } catch (e) { melden(e.message); } });
  $("#wf-aus", teil)?.addEventListener("click", async () => { try { await api("/betrieb/webformular", { methode: "PUT", daten: { aktiv: false } }); zeigen(); } catch (e) { melden(e.message); } });
  $("#pe-form", teil).addEventListener("submit", async (ev) => {
    ev.preventDefault(); const daten = Object.fromEntries(new FormData(ev.target).entries()); daten.port = Number(daten.port) || 993;
    try { await api("/betrieb/posteingang", { methode: "PUT", daten }); $("#pe-status", teil).innerHTML = `<span class="hinweis">Gespeichert</span>`; } catch (e) { $("#pe-status", teil).innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
}


// --- Lager & Großhandel: Bestände, Buchungen, Bestellvorschlag, IDS-Connect, DATANORM -------------
async function lagerAnsicht(ziel) {
  const wir = await ich();
  const darfBuero = wir.rechte.includes("bearbeiten");
  const [bestand, vorschlag, artikel, ressourcen, betrieb] = await Promise.all([api("/lager"), api("/lager/bestellvorschlag"), api("/artikel?alle=true"), api("/ressourcen"), api("/betrieb")]);
  const lagerOptionen = `<option value="zentral">Zentrallager</option>${ressourcen.map((r) => `<option value="${esc(r.id)}">Fahrzeug ${esc(r.name)}</option>`).join("")}`;
  const material = artikel.filter((a) => a.kategorie === "material");
  ziel.innerHTML = `<h1>Lager &amp; Großhandel</h1><p class="unterzeile">Bestände je Zentrallager und Fahrzeug; Rapportmaterial mit Artikelnummer bucht automatisch ab. Fehlmengen unter Mindestbestand bilden den Bestellvorschlag je Lieferant, der per IDS-Connect als Warenkorb an den Shop des Großhändlers übergeben wird. Preislisten kommen als DATANORM in den Artikelstamm.</p>
    <div class="raster" style="margin-bottom:16px">
      <div class="karte"><h2>Buchung</h2><form id="lg-buchen" class="raster">
        <label>Artikel <select name="artikel_id" required><option value="">– wählen –</option>${material.map((a) => `<option value="${esc(a.id)}">${esc(a.nummer)} ${esc(a.bezeichnung)}</option>`).join("")}</select></label>
        <label>Lagerort <select name="lager">${lagerOptionen}</select></label>
        <label>Menge (+ Zugang / − Abgang) <input name="menge" type="number" step="0.01" required></label>
        <label>Mindestbestand (optional) <input name="mindestbestand" type="number" step="0.01" min="0"></label>
        <label>Grund <input name="grund" value="Wareneingang"></label>
        <div class="aktionen" style="align-self:end"><button type="submit">Buchen</button></div></form><p id="lg-status"></p></div>
      ${darfBuero ? `<div class="karte"><h2>DATANORM-Preisliste einlesen</h2><p class="unterzeile">DATANORM 4 (Satzarten A/P, CP850) oder DATANORM 5 (XML). Erst Trockenlauf, dann Import; Verkaufspreis = Einkauf × (1 + Aufschlag).</p>
        <form id="lg-datanorm" class="raster"><label>Datei <input name="datei" type="file" accept=".001,.dat,.txt,.xml,.datanorm" required></label><label>Lieferant <input name="lieferant" required placeholder="z. B. GC-Gruppe"></label>
        <label>Aufschlag % (leer: Stammdatum) <input name="aufschlag_prozent" inputmode="decimal"></label><label><span><input type="checkbox" name="trocken" checked> Trockenlauf (nichts speichern)</span></label>
        <div class="aktionen" style="align-self:end"><button type="submit" class="leise">Einlesen</button></div></form><div id="lg-dn"></div></div>` : ""}
    </div>
    <div class="karte" style="margin-bottom:16px"><div class="zeile" style="justify-content:space-between"><h2 style="margin:0">Bestellvorschlag</h2><small>${vorschlag.positionen} Position(en) unter Mindestbestand${betrieb.stammdaten.ids_shop_url ? "" : " · IDS-Shop-Adresse in den Stammdaten setzen, um Warenkörbe zu übergeben"}</small></div>
      ${vorschlag.gruppen.map((g, gi) => `<h3>${esc(g.lieferant)} <small>Einkauf netto ca. ${esc(EURO.format(g.summe_einkauf))}</small></h3>
        <div class="tabelle-rahmen"><table><thead><tr><th>Artikel</th><th>Fehlmenge</th><th>Lagerorte</th></tr></thead><tbody>${g.positionen.map((p) => `<tr><td>${esc(p.nummer)} ${esc(p.bezeichnung)}</td><td>${esc(String(p.menge))} ${esc(p.einheit)}</td><td><small>${p.lager.map((l) => `${esc(l.lagername)}: ${esc(String(l.fehlmenge))}`).join(", ")}</small></td></tr>`).join("")}</tbody></table></div>
        ${darfBuero ? `<div class="aktionen"><button class="leise" data-ids="${gi}">Warenkorb an Shop übergeben (IDS-Connect)</button></div>` : ""}`).join("") || "<p>Alle Bestände über Mindestbestand.</p>"}</div>
    <div class="karte"><div class="zeile" style="justify-content:space-between"><h2 style="margin:0">Bestände</h2><button class="leise" id="lg-csv">CSV</button></div>
      <div class="tabelle-rahmen"><table><thead><tr><th>Lagerort</th><th>Artikel</th><th style="text-align:right">Bestand</th><th style="text-align:right">Mindestbestand</th><th>Lieferant</th><th>Stand</th></tr></thead><tbody>
      ${bestand.map((b) => `<tr><td>${esc(b.lagername)}</td><td>${esc(b.nummer)} ${esc(b.bezeichnung)}</td><td style="text-align:right">${b.unter_minimum ? tag(`${b.menge} ${esc(b.einheit)}`, "ueberfaellig") : `${esc(String(b.menge))} ${esc(b.einheit)}`}</td><td style="text-align:right">${esc(String(b.mindestbestand))}</td><td>${esc(b.lieferant || "")}</td><td><small>${zeit(b.geaendert)}</small></td></tr>`).join("") || `<tr><td colspan="6">Noch keine Bestände. Erste Buchung oben erfassen (Zugang mit Mindestbestand).</td></tr>`}</tbody></table></div></div>`;
  $("#lg-buchen").addEventListener("submit", async (ev) => {
    ev.preventDefault(); const fd = new FormData(ev.target);
    const daten = { artikel_id: fd.get("artikel_id"), lager: fd.get("lager"), menge: Number(String(fd.get("menge")).replace(",", ".")), grund: fd.get("grund") || "manuell" };
    if (fd.get("mindestbestand")) daten.mindestbestand = Number(String(fd.get("mindestbestand")).replace(",", "."));
    try { await api("/lager/buchen", { methode: "POST", daten }); zeigen(); } catch (e) { $("#lg-status").innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
  $("#lg-datanorm")?.addEventListener("submit", async (ev) => {
    ev.preventDefault(); const fd = new FormData(ev.target); if (!fd.get("trocken")) fd.delete("trocken"); else fd.set("trocken", "true");
    $("#lg-dn").textContent = "Wird gelesen …";
    try {
      const e = await api("/artikel/datanorm", { methode: "POST", daten: fd });
      $("#lg-dn").innerHTML = `<p class="${e.trocken ? "hinweis" : ""}">${e.trocken ? "Trockenlauf: " : "Importiert: "}${e.gelesen} Zeilen gelesen, ${e.neu} neu, ${e.geaendert} geändert, ${e.deaktiviert} deaktiviert, ${e.uebersprungen} ohne Preis (Aufschlag ${e.aufschlag_prozent} %).</p>
        ${e.beispiele.length ? `<table><thead><tr><th>Nummer</th><th>Bezeichnung</th><th>EK</th><th>VK</th></tr></thead><tbody>${e.beispiele.map((b) => `<tr><td>${esc(b.nummer)}</td><td>${esc(b.bezeichnung)}</td><td>${b.einkauf_netto != null ? esc(EURO.format(b.einkauf_netto)) : "–"}</td><td>${b.verkauf_netto != null ? esc(EURO.format(b.verkauf_netto)) : "–"}</td></tr>`).join("")}</tbody></table>` : ""}`;
      if (!e.trocken) setTimeout(zeigen, 1500);
    } catch (e) { $("#lg-dn").innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
  $("#lg-csv").addEventListener("click", () => dateiLaden("/lager/export.csv", "lagerbestand.csv"));
  ziel.querySelectorAll("[data-ids]").forEach((b) => b.addEventListener("click", async () => {
    const g = vorschlag.gruppen[Number(b.dataset.ids)];
    const positionen = g.positionen.map((p) => ({ nummer: p.nummer, menge: p.menge, einheit: p.einheit, bezeichnung: p.bezeichnung }));
    const r = await fetch("/lager/warenkorb", { method: "POST", headers: { ...authKopf(), "Content-Type": "application/json" }, body: JSON.stringify({ positionen }) });
    if (!r.ok) { const d = await r.json().catch(() => ({})); return melden(d.detail || `HTTP ${r.status}`); }
    const fenster = window.open("", "_blank"); fenster.document.write(await r.text()); fenster.document.close();
  }));
}


// --- Gutscheine ------------------------------------------------------------------------------------
async function gutscheineAnsicht(ziel) {
  const [u, kunden] = await Promise.all([api("/gutscheine"), api("/kunden?limit=500")]);
  ziel.innerHTML = `<h1>Gutscheine</h1><p class="unterzeile">Wertgutscheine mit Code und PDF; Teileinlösung mit Restwert, gültig drei Jahre ab Jahresende (§ 195 BGB). Umsatzsteuer: Einzweck- oder Mehrzweck-Gutschein (§ 3 Abs. 14/15 UStG) klärt die Kanzlei.</p>
    <div class="raster" style="margin-bottom:16px"><div class="karte"><h2>Ausstellen</h2><form id="gs-neu" class="raster">
      <label>Wert (€) <input name="wert" type="number" step="0.01" min="1" required></label><label>Für (Name auf dem Gutschein) <input name="empfaenger"></label>
      <label>Kunde (Käufer, optional) <select name="kunde_id"><option value="">–</option>${kunden.map((k) => `<option value="${esc(k.id)}">${esc(k.name)}</option>`).join("")}</select></label>
      <div class="aktionen" style="align-self:end"><button type="submit">Ausstellen</button></div></form><p id="gs-status"></p></div>
    <div class="karte"><h2>Einlösen</h2><form id="gs-ein" class="raster"><label>Code <input name="code" required placeholder="XXXX-XXXX-XXXX"></label><label>Betrag (€) <input name="betrag" type="number" step="0.01" min="0.01" required></label>
      <div class="aktionen" style="align-self:end"><button type="submit" class="leise">Einlösen</button></div></form><p id="gs-ein-status"></p></div></div>
    <div class="karte"><div class="zeile" style="justify-content:space-between"><h2 style="margin:0">Bestand</h2><small>${u.aktiv} aktiv · offen ${esc(EURO.format(u.offen_summe))}</small></div>
      <div class="tabelle-rahmen"><table><thead><tr><th>Code</th><th>Für</th><th style="text-align:right">Wert</th><th style="text-align:right">Rest</th><th>Gültig bis</th><th>Status</th><th></th></tr></thead><tbody>
      ${u.gutscheine.map((g) => `<tr><td><code>${esc(g.code)}</code></td><td>${esc(g.empfaenger || "")}</td><td style="text-align:right">${esc(EURO.format(g.wert))}</td><td style="text-align:right">${esc(EURO.format(g.restwert))}</td><td>${esc(DATUM.format(new Date(g.gueltig_bis)))}</td><td>${tag(g.status, { aktiv: "aktuell", eingeloest: "neutral", abgelaufen: "ueberfaellig", storniert: "abgelehnt" }[g.status])}${g.einloesungen.length ? `<br><small>${g.einloesungen.length} Einlösung(en)</small>` : ""}</td>
        <td><div class="aktionen"><button class="leise" data-pdf="${esc(g.code)}">PDF</button>${g.status === "aktiv" && !g.einloesungen.length ? `<button class="gefahr" data-storno="${esc(g.code)}">Storno</button>` : ""}</div></td></tr>`).join("") || `<tr><td colspan="7">Noch keine Gutscheine.</td></tr>`}</tbody></table></div></div>`;
  $("#gs-neu").addEventListener("submit", async (ev) => {
    ev.preventDefault(); const fd = new FormData(ev.target);
    try { const g = await api("/gutscheine", { methode: "POST", daten: { wert: Number(fd.get("wert")), empfaenger: fd.get("empfaenger") || null, kunde_id: fd.get("kunde_id") || null } }); $("#gs-status").innerHTML = `Gutschein <code>${esc(g.code)}</code> über ${esc(EURO.format(g.wert))} ausgestellt.`; setTimeout(zeigen, 1200); }
    catch (e) { $("#gs-status").innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
  $("#gs-ein").addEventListener("submit", async (ev) => {
    ev.preventDefault(); const fd = new FormData(ev.target);
    try { const g = await api(`/gutscheine/${encodeURIComponent(fd.get("code").trim())}/einloesen`, { methode: "POST", daten: { betrag: Number(fd.get("betrag")) } }); $("#gs-ein-status").innerHTML = `${esc(EURO.format(g.eingeloest))} eingelöst, Restwert ${esc(EURO.format(g.restwert))}.`; setTimeout(zeigen, 1200); }
    catch (e) { $("#gs-ein-status").innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
  ziel.querySelectorAll("[data-pdf]").forEach((b) => b.addEventListener("click", () => dateiLaden(`/gutscheine/${b.dataset.pdf}/pdf`, `gutschein-${b.dataset.pdf}.pdf`)));
  ziel.querySelectorAll("[data-storno]").forEach((b) => b.addEventListener("click", async () => { if (!confirm("Gutschein stornieren?")) return; try { await api(`/gutscheine/${b.dataset.storno}/storno`, { methode: "POST" }); zeigen(); } catch (e) { melden(e.message); } }));
}

// --- Runde 6: Lebenszyklus, Zähler, Gewährleistung im Kundendetail ----------------------------------
const BAUTEIL_STATUS = { faellig: "ueberfaellig", bald: "bald_faellig", aktuell: "aktuell", ohne_termin: "neutral" };
const BAUTEIL_TEXT = { faellig: "fällig", bald: "bald", aktuell: "aktuell", ohne_termin: "ohne Termin" };
async function kundeLebenszyklus(wurzel, id, anlagen) {
  if (!anlagen.length && !(BRANCHE?.begriffe?.anlagen !== false)) return;
  const teil = document.createElement("div");
  teil.innerHTML = `<h3 style="margin-top:16px">Erneuerungskalender</h3><div id="lz">Wird geladen …</div>
    <h3 style="margin-top:16px">Zählerstände und Verbrauch</h3><div id="zs">Wird geladen …</div>
    <h3 style="margin-top:16px">Gewährleistung und Garantie</h3><div id="gw">Wird geladen …</div>`;
  wurzel.append(teil);
  const [kal, zs, gw, wir] = await Promise.all([
    api(`/kunden/${id}/erneuerungskalender?jahre=10`).catch((e) => ({ fehler: e.message })),
    api(`/kunden/${id}/zaehlerstaende`).catch((e) => ({ fehler: e.message })),
    api(`/kunden/${id}/gewaehrleistung`).catch((e) => ({ fehler: e.message })), ich()]);
  const darf = wir.rechte.includes("einsatz") || wir.rechte.includes("bearbeiten");
  // Erneuerungskalender
  if (kal.fehler) $("#lz", teil).innerHTML = `<span class="fehler">${esc(kal.fehler)}</span>`;
  else {
    const alle = await Promise.all(anlagen.map((a) => api(`/anlagen/${a.id}/bauteile`)));
    $("#lz", teil).innerHTML = `<p class="unterzeile">Rücklage für Erneuerungen: <strong>${esc(EURO.format(kal.ruecklage_monat))}/Monat</strong> (${esc(EURO.format(kal.ruecklage_jahr))}/Jahr, lineare Abschreibung über die Nutzungsdauer) · Kosten der nächsten 10 Jahre ca. ${esc(EURO.format(kal.kosten_horizont))}.</p>
      ${kal.ueberfaellig.length ? `<p>${tag(`${kal.ueberfaellig.length} überfällig`, "ueberfaellig")} ${kal.ueberfaellig.map((e) => esc(e.bezeichnung)).join(" · ")}</p>` : ""}
      <div class="raster">${alle.map((u, i) => `<div><strong>${esc(u.anlage.bezeichnung || u.anlage.anlagentyp)}</strong>${u.anlage.baujahr ? ` · Bj. ${u.anlage.baujahr}` : ""}
        <table><thead><tr><th>Bauteil / Prüfung</th><th>Fällig</th><th>Kosten</th><th></th></tr></thead><tbody>
        ${u.bauteile.map((b) => `<tr><td>${esc(b.bezeichnung)}${b.pflicht ? ` <small title="${esc(b.rechtsgrundlage || "")}">Prüfpflicht${b.intervall_monate ? `, alle ${b.intervall_monate} Mon.` : ""}</small>` : ` <small>${b.nutzungsdauer_jahre} J. Nutzungsdauer</small>`}</td>
          <td>${b.faellig ? esc(b.faellig.split("-").reverse().join(".")) : "–"} ${tag(BAUTEIL_TEXT[b.status], BAUTEIL_STATUS[b.status])}</td><td>${b.kosten_schaetzung ? esc(EURO.format(b.kosten_schaetzung)) : "–"}</td>
          <td>${darf ? `<button class="leise" data-erneuert="${esc(b.id)}" data-pflicht="${b.pflicht}" style="padding:2px 8px">${b.pflicht ? "Geprüft" : "Erneuert"}</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="4">Keine Bauteile${darf ? ` – <button class="leise" data-katalog="${esc(anlagen[i].id)}" style="padding:2px 8px">aus Katalog anlegen</button>` : ""}</td></tr>`}</tbody></table></div>`).join("")}</div>
      <details style="margin-top:8px"><summary>Kalender nach Jahren</summary><table><tbody>${kal.jahre.filter((j) => j.eintraege.length).map((j) => `<tr><td><strong>${j.jahr}</strong><br><small>${esc(EURO.format(j.kosten))}</small></td><td>${j.eintraege.map((e) => `${esc(e.bezeichnung)}${e.pflicht ? " (Prüfung)" : ""}`).join(", ")}</td></tr>`).join("")}</tbody></table></details>
      <p class="unterzeile">Nutzungsdauern nach VDI 2067/Herstellerangaben, Rechtsgrundlagen als Hinweis – keine Rechtsberatung.</p>`;
    $("#lz", teil).querySelectorAll("[data-erneuert]").forEach((b) => b.addEventListener("click", async () => {
      const am = prompt(b.dataset.pflicht === "true" ? "Geprüft am (JJJJ-MM-TT):" : "Erneuert am (JJJJ-MM-TT):", TAGESSCHLUESSEL.format(new Date())); if (am === null) return;
      const kosten = b.dataset.pflicht === "true" ? null : prompt("Tatsächliche Kosten brutto (optional, z. B. 480.00):") || null;
      try { await api(`/bauteile/${b.dataset.erneuert}/erneuert`, { methode: "POST", daten: { am, kosten } }); kundeDetail(id); } catch (e) { melden(e.message); }
    }));
    $("#lz", teil).querySelectorAll("[data-katalog]").forEach((b) => b.addEventListener("click", async () => { try { await api(`/anlagen/${b.dataset.katalog}/bauteile/anlegen`, { methode: "POST" }); kundeDetail(id); } catch (e) { melden(e.message); } }));
  }
  // Zählerstände
  if (zs.fehler) $("#zs", teil).innerHTML = `<span class="fehler">${esc(zs.fehler)}</span>`;
  else {
    const BEW = { auffaellig: ["auffällig", "ueberfaellig"], verbessert: ["verbessert", "aktuell"], unauffaellig: ["unauffällig", "aktuell"], zu_wenig_daten: ["zu wenig Daten", "neutral"] };
    $("#zs", teil).innerHTML = `${zs.hinweise.map((h) => `<p class="hinweis">${esc(h.text)}</p>`).join("")}
      ${zs.reihen.length ? `<table><thead><tr><th>Zähler</th><th>Stände</th><th>Jahresverbrauch${zs.reihen.some((r) => r.klimabereinigt) ? " (klimabereinigt)" : ""}</th><th>Referenz</th><th>Bewertung</th></tr></thead><tbody>
        ${zs.reihen.map((r) => `<tr><td>${esc(r.bezeichnung)}${r.anlagentyp ? ` <small>${esc(r.anlagentyp)}</small>` : ""}</td><td>${r.staende}</td><td>${r.jahresverbrauch_normiert != null ? `${esc(Math.round(r.jahresverbrauch_normiert).toLocaleString("de-DE"))} ${esc(r.einheit)}/a${r.kwh_je_m2 ? `<br><small>${r.kwh_je_m2} kWh/m²a · ${esc(r.effizienzklasse_hinweis)}</small>` : ""}` : "–"}</td>
          <td>${r.referenz != null ? `${esc(Math.round(r.referenz).toLocaleString("de-DE"))} ${esc(r.einheit)}/a${r.abweichung != null ? ` (${r.abweichung > 0 ? "+" : ""}${Math.round(r.abweichung * 100)} %)` : ""}` : "–"}</td><td>${tag(BEW[r.bewertung][0], BEW[r.bewertung][1])}</td></tr>`).join("")}</tbody></table>` : "<p>Noch keine Zählerstände. Zwei Stände im Abstand von mindestens zwei Wochen ergeben eine Periode; ab zwei Perioden wird verglichen.</p>"}
      ${zs.staende.length ? `<details><summary>${zs.staende.length} Stände</summary><table><tbody>${zs.staende.slice().reverse().slice(0, 30).map((z) => `<tr><td>${esc(z.datum.split("-").reverse().join("."))}</td><td>${esc(z.bezeichnung)}</td><td style="text-align:right">${esc(z.wert.toLocaleString("de-DE"))} ${esc(z.einheit)}</td><td><small>${esc(z.quelle)}${z.notiz ? ` · ${esc(z.notiz)}` : ""}</small></td></tr>`).join("")}</tbody></table></details>` : ""}
      ${darf ? `<form id="zs-form" class="zeile" style="margin-top:8px"><select name="art">${Object.entries(zs.arten).map(([k, a]) => `<option value="${k}">${esc(a.bezeichnung)} (${esc(a.einheit)})</option>`).join("")}</select>
        <input name="wert" type="number" step="0.1" min="0" placeholder="Stand" required style="width:120px"><input name="datum" type="date" value="${TAGESSCHLUESSEL.format(new Date())}">
        ${anlagen.length ? `<select name="anlage_id"><option value="">ohne Anlage</option>${anlagen.map((a) => `<option value="${esc(a.id)}">${esc(a.anlagentyp)}${a.hersteller ? ` ${esc(a.hersteller)}` : ""}</option>`).join("")}</select>` : ""}
        <input name="notiz" placeholder="Notiz (z. B. Zählerwechsel)" style="width:180px"><button type="submit" class="leise">Erfassen</button></form>` : ""}`;
    $("#zs-form", teil)?.addEventListener("submit", async (ev) => {
      ev.preventDefault(); const daten = Object.fromEntries([...new FormData(ev.target).entries()].filter(([, v]) => v !== "")); daten.wert = Number(daten.wert);
      try { await api(`/kunden/${id}/zaehlerstaende`, { methode: "POST", daten }); kundeDetail(id); } catch (e) { melden(e.message); }
    });
  }
  // Gewährleistung
  if (gw.fehler) $("#gw", teil).innerHTML = `<span class="fehler">${esc(gw.fehler)}</span>`;
  else $("#gw", teil).innerHTML = `${gw.herstellergarantien.map((g) => `<p>${tag(g.aktiv ? "Herstellergarantie läuft" : "Herstellergarantie abgelaufen", g.aktiv ? "aktuell" : "neutral")} ${esc(g.hersteller || "")} ${esc(g.modell || "")} bis ${esc(g.ende.split("-").reverse().join("."))} <small>(${esc(g.quelle)})</small></p>`).join("")}
    ${gw.leistungen.length ? `<table><thead><tr><th>Eigene Leistung</th><th>Datum</th><th>Gewährleistung bis</th><th>Grundlage</th></tr></thead><tbody>${gw.leistungen.map((l) => `<tr><td>${esc(l.nummer)} · ${esc(l.taetigkeiten)}</td><td>${esc(l.datum.split("-").reverse().join("."))}</td><td>${esc(l.ende.split("-").reverse().join("."))} <small>(${l.resttage} Tage)</small></td><td><small>${esc(l.grundlage)}</small></td></tr>`).join("")}</tbody></table>` : "<p>Keine eigenen Leistungen in laufender Gewährleistung.</p>"}`;
}

async function fallGarantie(wurzel, id) {
  try {
    const g = await api(`/servicefaelle/${id}/garantie`);
    if (!g.hinweise.length) return;
    const p = document.createElement("div");
    p.innerHTML = g.hinweise.map((h) => `<p class="hinweis">⚖️ ${esc(h)}</p>`).join("");
    wurzel.querySelector("h3")?.before(p);
  } catch { /* kein Recht oder kein Kunde */ }
}

// --- Runde 6: Dienstplan, Zeitkonto, Urlaub, Qualifikationsmatrix -----------------------------------
const SCHICHT_TEXT = { frueh: "Früh", spaet: "Spät", tag: "Tag", bereitschaft: "Bereitschaft" };
const QSTATUS = { gueltig: ["gültig", "aktuell"], laeuft_ab: ["läuft ab", "bald_faellig"], abgelaufen: ["abgelaufen", "ueberfaellig"], fehlt: ["fehlt", "neutral"], unbefristet: ["unbefristet", "aktuell"], ohne_datum: ["ohne Datum", "offen"] };
let DIENSTPLAN_MONTAG = null;
async function dienstplanAnsicht(ziel) {
  const wir = await ich();
  const verwalten = wir.rechte.includes("verwalten");
  const von = DIENSTPLAN_MONTAG || TAGESSCHLUESSEL.format(new Date());
  const [w, zk, ul, mtx, schichten] = await Promise.all([api(`/dienstplan?von=${von}`), api("/zeitkonto").catch(() => []), api("/urlaub").catch(() => []),
    api("/qualifikationen/matrix").catch(() => null), api("/dienstplan/schichten")]);
  DIENSTPLAN_MONTAG = w.montag;
  const TAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
  const verstoesse = w.warnungen.filter((x) => x.schwere === "verstoss"), hinweise = w.warnungen.filter((x) => x.schwere !== "verstoss");
  ziel.innerHTML = `<h1>Dienstplan</h1><p class="unterzeile">Schichten je Mitarbeiter und Tag; ohne Eintrag gilt die Regelarbeitszeit. Disposition, Warteliste, Optimierer und Prognose rechnen mit diesem Plan. Prüfung gegen das Arbeitszeitgesetz (§§ 3–5, 11 ArbZG) als Hinweis, keine Rechtsberatung.</p>
    <div class="karte" style="margin-bottom:16px"><div class="aktionen" style="justify-content:space-between"><span><button class="leise" id="dp-zurueck">◀</button> <strong>KW ${w.kw}</strong> · ${esc(w.montag.split("-").reverse().join("."))} – ${esc(w.sonntag.split("-").reverse().join("."))} <button class="leise" id="dp-vor">▶</button></span>
      ${verwalten ? `<span><button class="leise" id="dp-vorschlag">Wochenvorschlag</button></span>` : ""}</div>
      <div class="tabelle-rahmen"><table class="dienstplan"><thead><tr><th>Mitarbeiter</th>${w.zeilen[0]?.tage.map((t, i) => `<th>${TAGE[i]} ${esc(t.datum.slice(8))}.${esc(t.datum.slice(5, 7))}.${t.feiertag ? `<br><small>${esc(t.feiertag)}</small>` : ""}</th>`).join("") || ""}<th>Σ</th></tr></thead><tbody>
      ${w.zeilen.map((z) => `<tr><td>${esc(z.name.split(" (")[0])}<br><small>${z.wochenstunden} h/Woche</small></td>${z.tage.map((t) => `<td class="${verwalten ? "klickbar" : ""} ${t.abwesenheit ? "abwesend" : t.dienste.some((d) => d.art === "frei") ? "frei" : ""}" data-r="${esc(z.ressource_id)}" data-tag="${esc(t.datum)}">
        ${t.abwesenheit ? `<small>${esc(t.abwesenheit)}</small>` : t.dienste.length ? t.dienste.map((d) => `<span class="dienst ${esc(d.art)}" title="${esc(d.notiz || "")}">${d.art === "frei" ? "frei" : d.art === "bereitschaft" ? "Bereitsch." : `${esc(d.beginn)}–${esc(d.ende)}`}${d.schicht && d.art === "arbeit" ? ` <small>${esc(SCHICHT_TEXT[d.schicht] || d.schicht)}</small>` : ""}</span>`).join("") : t.beginn ? `<span class="dienst regel">${esc(t.beginn)}–${esc(t.ende)}</span>` : `<small>–</small>`}
        ${t.gebucht_h ? `<br><small>${t.gebucht_h} h Termine</small>` : ""}</td>`).join("")}<td><strong>${z.summe_h}</strong><br><small class="${z.abweichung_h > 0 ? "fehler" : ""}">${z.abweichung_h > 0 ? "+" : ""}${z.abweichung_h} h</small></td></tr>`).join("") || `<tr><td colspan="9">Keine aktiven Mitarbeiter.</td></tr>`}</tbody></table></div>
      <div id="dp-form"></div><div id="dp-vorschlag-box"></div>
      ${verstoesse.length ? `<h3 style="margin-top:12px">Verstöße (${verstoesse.length})</h3><ul class="pruefliste">${verstoesse.map((x) => `<li>${tag("ArbZG", "ueberfaellig")} ${esc(x.name.split(" (")[0])}, ${esc(x.datum.split("-").reverse().join("."))}: ${esc(x.text)}</li>`).join("")}</ul>` : `<p class="hinweis" style="margin-top:12px">Keine Verstöße gegen das Arbeitszeitgesetz im geprüften Zeitraum.</p>`}
      ${hinweise.length ? `<details><summary>${hinweise.length} Hinweise</summary><ul class="pruefliste">${hinweise.map((x) => `<li>${esc(x.name.split(" (")[0])}, ${esc(x.datum.split("-").reverse().join("."))}: ${esc(x.text)}</li>`).join("")}</ul></details>` : ""}</div>
    <div class="raster">
      <div class="karte"><h2>Zeitkonto (laufender Monat)</h2><table><thead><tr><th>Mitarbeiter</th><th>Soll</th><th>Ist</th><th>Gutschrift</th><th>Saldo</th></tr></thead><tbody>
        ${zk.map((k) => `<tr><td>${esc(k.name.split(" (")[0])}</td><td>${k.soll_h} h</td><td>${k.ist_h} h</td><td>${k.gutschrift_h} h</td><td><strong class="${k.saldo_h < -8 ? "fehler" : ""}">${k.saldo_h > 0 ? "+" : ""}${k.saldo_h} h</strong></td></tr>`).join("") || "<tr><td colspan='5'>–</td></tr>"}</tbody></table>
        <p class="unterzeile">Soll aus Diensten bzw. Regelarbeitszeit, Ist aus der Zeiterfassung (Arbeit + Fahrt), Gutschrift für Urlaub/Krankheit/Schulung.</p></div>
      <div class="karte"><h2>Urlaubskonten ${new Date().getFullYear()}</h2><table><thead><tr><th>Mitarbeiter</th><th>Anspruch</th><th>Genommen</th><th>Geplant</th><th>Rest</th></tr></thead><tbody>
        ${ul.map((u) => `<tr><td>${esc(u.name.split(" (")[0])}</td><td>${u.anspruch}</td><td>${u.genommen}</td><td>${u.geplant}</td><td><strong>${u.rest}</strong></td></tr>`).join("") || "<tr><td colspan='5'>–</td></tr>"}</tbody></table>
        <p class="unterzeile">Urlaub wird über Abwesenheiten (Art „urlaub“) gezählt, nur Arbeitstage ohne Feiertag. Anspruch je Mitarbeiter in den Stammdaten der Ressource.</p></div>
    </div>
    ${mtx ? `<div class="karte" style="margin-top:16px"><h2>Qualifikationsmatrix</h2>
      ${mtx.risiken.map((r) => `<p class="hinweis">${esc(r)}</p>`).join("")}
      <div class="tabelle-rahmen"><table><thead><tr><th>Mitarbeiter</th>${mtx.spalten.map((sp) => `<th title="${esc(sp.grundlage)}">${esc(sp.bezeichnung.split(" (")[0])}${sp.pflicht ? " *" : ""}${sp.nachschulung_monate ? `<br><small>alle ${sp.nachschulung_monate} Mon.</small>` : ""}</th>`).join("")}</tr></thead><tbody>
      ${mtx.zeilen.map((z) => `<tr><td>${esc(z.name.split(" (")[0])}</td>${mtx.spalten.map((sp) => { const c = z.zellen[sp.schluessel]; return `<td class="${verwalten ? "klickbar" : ""}" data-qr="${esc(z.ressource_id)}" data-q="${esc(sp.schluessel)}" data-hat="${c.status !== "fehlt"}">${tag(QSTATUS[c.status][0], QSTATUS[c.status][1])}${c.ablauf ? `<br><small>${esc(c.ablauf.split("-").reverse().join("."))}</small>` : ""}</td>`; }).join("")}</tr>`).join("")}</tbody></table></div>
      <p class="unterzeile">* Pflicht für alle (Stammdatum pflichtqualifikationen). Abgelaufene Qualifikationen zählen in Disposition und Optimierer nicht mehr. ${verwalten ? "Klick auf eine Zelle setzt Qualifikation und Ablaufdatum." : ""}</p></div>` : ""}`;
  $("#dp-zurueck").addEventListener("click", () => { DIENSTPLAN_MONTAG = verschieben(w.montag, -7); zeigen(); });
  $("#dp-vor").addEventListener("click", () => { DIENSTPLAN_MONTAG = verschieben(w.montag, 7); zeigen(); });
  if (!verwalten) return;
  ziel.querySelectorAll("td[data-tag]").forEach((td) => td.addEventListener("click", () => {
    const zeile = w.zeilen.find((z) => z.ressource_id === td.dataset.r), t = zeile.tage.find((x) => x.datum === td.dataset.tag);
    $("#dp-form").innerHTML = `<form id="dp-f" class="zeile" style="margin-top:8px"><strong>${esc(zeile.name.split(" (")[0])}, ${esc(td.dataset.tag.split("-").reverse().join("."))}</strong>
      <select name="schicht"><option value="">eigene Zeiten</option>${Object.entries(schichten).map(([k, s]) => `<option value="${k}">${esc(SCHICHT_TEXT[k] || k)} ${s.beginn}–${s.ende}</option>`).join("")}</select>
      <input name="beginn" type="time" value="${esc(t.beginn || "07:30")}"><input name="ende" type="time" value="${esc(t.ende || "16:30")}"><input name="pause_minuten" type="number" min="0" max="180" value="30" style="width:70px" title="Pause in Minuten">
      <button type="submit" class="leise">Dienst speichern</button><button type="button" class="leise" id="dp-frei">Frei</button>${t.dienste.map((d) => `<button type="button" class="leise gefahr" data-loeschen="${esc(d.id)}">Löschen ${esc(d.beginn)}</button>`).join("")}</form>`;
    $("#dp-f").addEventListener("submit", async (ev) => {
      ev.preventDefault(); const fd = new FormData(ev.target); const schicht = fd.get("schicht") || null;
      const daten = { ressource_id: td.dataset.r, datum: td.dataset.tag, art: "arbeit", schicht, pause_minuten: Number(fd.get("pause_minuten")) };
      if (!schicht) { daten.beginn = fd.get("beginn"); daten.ende = fd.get("ende"); }
      try { await api("/dienste", { methode: "POST", daten }); zeigen(); } catch (e) { melden(e.message); }
    });
    $("#dp-frei").addEventListener("click", async () => { try { await api("/dienste", { methode: "POST", daten: { ressource_id: td.dataset.r, datum: td.dataset.tag, art: "frei" } }); zeigen(); } catch (e) { melden(e.message); } });
    $("#dp-f").querySelectorAll("[data-loeschen]").forEach((b) => b.addEventListener("click", async () => { try { await api(`/dienste/${b.dataset.loeschen}`, { methode: "DELETE" }); zeigen(); } catch (e) { melden(e.message); } }));
  }));
  $("#dp-vorschlag")?.addEventListener("click", async () => {
    const box = $("#dp-vorschlag-box"); box.innerHTML = "Berechne …";
    try {
      const v = await api(`/dienstplan/vorschlag?von=${w.montag}`);
      box.innerHTML = `<div class="karte" style="margin-top:8px"><h3>Vorschlag KW ${v.kw}${v.schichtbetrieb ? " (Schichtbetrieb)" : ""}</h3><p class="unterzeile">Bedarf aus gebuchten Terminen und Prognose (${v.bedarf_woche_h} h/Woche, +15 % Reserve); wer zuletzt am wenigsten Stunden hatte, wird zuerst eingeteilt. Bestehende Dienste bleiben.</p>
        <table><thead><tr><th>Tag</th><th>Bedarf</th><th>Bereits geplant</th><th>Vorschlag</th></tr></thead><tbody>${v.tage.map((t) => `<tr><td>${esc(t.datum.split("-").reverse().join("."))}</td><td>${t.bedarf_h} h (${t.benoetigt} Pers.)${t.unterdeckung_h ? ` <span class="fehler">−${t.unterdeckung_h} h</span>` : ""}</td><td>${esc(t.geplant.join(", ") || "–")}</td><td>${t.vorschlag.map((d) => `${esc(d.name.split(" (")[0])} ${esc(SCHICHT_TEXT[d.schicht])} ${d.beginn}–${d.ende}`).join("<br>") || "–"}</td></tr>`).join("")}</tbody></table>
        <div class="aktionen" style="margin-top:8px">${v.dienste.length ? `<button id="dp-uebernehmen">${v.dienste.length} Dienste übernehmen</button>` : "<span>Nichts zu ergänzen.</span>"}</div></div>`;
      $("#dp-uebernehmen")?.addEventListener("click", async () => { try { await api("/dienstplan/vorschlag", { methode: "POST", daten: { dienste: v.dienste } }); zeigen(); } catch (e) { melden(e.message); } });
    } catch (e) { box.innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
  ziel.querySelectorAll("td[data-q]").forEach((td) => td.addEventListener("click", async () => {
    const r = mtx.zeilen.find((z) => z.ressource_id === td.dataset.qr), q = td.dataset.q;
    const hat = td.dataset.hat === "true";
    const ablauf = prompt(`${r.name.split(" (")[0]} – ${q}: Ablauf/Nachschulung (JJJJ-MM-TT, leer = unbefristet, „-“ = Qualifikation entfernen)`, r.zellen[q].ablauf || "");
    if (ablauf === null) return;
    const res = await api("/ressourcen").then((l) => l.find((x) => x.id === r.ressource_id));
    const quals = new Set(res.qualifikationen);
    if (ablauf === "-") quals.delete(q); else quals.add(q);
    try { await api(`/ressourcen/${r.ressource_id}`, { methode: "PATCH", daten: { qualifikationen: [...quals], qualifikationen_ablauf: { [q]: ablauf && ablauf !== "-" ? ablauf : null } } }); zeigen(); } catch (e) { melden(e.message); }
  }));
}
function verschieben(iso, tage) { const d = new Date(`${iso}T12:00:00`); d.setDate(d.getDate() + tage); return TAGESSCHLUESSEL.format(d); }


// --- Einrichtungsassistent: Fortschritt, Betrieb/Team/Sachmittel in einem Formular, Kundenimport aus CSV ---
async function einrichtungAnsicht(ziel) {
  const [st, branchen, betrieb, ressourcen] = await Promise.all([api("/betrieb/einrichtung"), api("/branchen"), api("/betrieb"), api("/ressourcen").catch(() => [])]);
  const sd = betrieb.stammdaten || {}; const katalog = BRANCHE?.sachmittel_katalog || {};
  const personen = ressourcen.filter((r) => r.art !== "sachmittel"), mittel = ressourcen.filter((r) => r.art === "sachmittel");
  const feld = (k, b, typ = "text") => `<label>${esc(b)} <input name="${k}" type="${typ}" value="${esc(sd[k] ?? "")}"></label>`;
  ziel.innerHTML = `<h1>Einrichtung</h1><p class="unterzeile">${esc(betrieb.name)} · ${esc(st.bezeichnung)} · ${st.erledigt} von ${st.pflicht} Pflichtschritten erledigt</p>
    <div class="karte" style="margin-bottom:16px"><div class="balken"><span style="width:${Math.round(st.fortschritt * 100)}%"></span></div>
      <ul class="schritte">${st.schritte.map((s) => `<li class="${s.erledigt ? "erledigt" : "offen"}"><span class="punkt">${s.erledigt ? "✓" : ""}</span><div><strong>${esc(s.titel)}</strong>${s.optional ? ` ${tag("optional", "neutral")}` : ""}<br><small>${esc(s.hinweis || "")}</small></div>${s.erledigt || s.pfad === "einrichtung" ? "" : `<a class="knopf leise" href="#${esc(s.pfad)}">Öffnen ›</a>`}</li>`).join("")}</ul>
      ${st.fertig ? `<p class="hinweis">Alles eingerichtet. Der Betrieb kann loslegen.</p>` : ""}</div>
    <form id="ea" class="karte" style="margin-bottom:16px"><h2>Betrieb, Team und Sachmittel</h2><p class="unterzeile">Alles in einem Schritt; ein zweiter Lauf legt nichts doppelt an.</p>
      <div class="raster">
        <label>Branche <select name="branche">${branchen.map((b) => `<option value="${esc(b.schluessel)}" ${b.schluessel === st.branche ? "selected" : ""}>${esc(b.bezeichnung)}</option>`).join("")}</select></label>
        <label>Name des Betriebs <input name="name" value="${esc(betrieb.name)}" required></label>
        ${feld("strasse", "Straße und Hausnummer")}${feld("plz", "PLZ")}${feld("ort", "Ort")}${feld("telefon", "Telefon", "tel")}${feld("email", "E-Mail", "email")}
        ${feld("iban", "IBAN (GiroCode auf Rechnungen)")}${feld("basis_url", "Öffentliche Adresse (https://…)", "url")}${feld("impressum_url", "Impressum-URL", "url")}${feld("datenschutz_url", "Datenschutz-URL", "url")}
      </div>
      <h3>${esc(wort("ressourcen", "Team"))}</h3><p class="unterzeile">${personen.length ? `Vorhanden: ${esc(personen.map((p) => p.name).join(", "))}` : "Noch niemand angelegt."} Je Zeile ein Name, optional Qualifikationen nach Doppelpunkt (z. B. <code>Anna Weber: gas, heizung</code>).</p>
      <textarea name="mitarbeiter" rows="3" placeholder="Anna Weber: gas, heizung&#10;Ben Koch"></textarea>
      ${BRANCHE?.sachmittel?.length ? `<h3>Sachmittel</h3><p class="unterzeile">${mittel.length ? `Vorhanden: ${esc(mittel.map((p) => p.name).join(", "))}.` : ""} Je Zeile <code>Name: Typ</code>; Typen: ${Object.entries(katalog).filter(([k]) => BRANCHE.sachmittel.includes(k)).map(([k, b]) => `<code>${esc(k)}</code> (${esc(b)})`).join(", ")}.</p>
      <textarea name="sachmittel" rows="2" placeholder="Bühne 1: ${esc(BRANCHE.sachmittel[0])}"></textarea>` : ""}
      <div class="zeile" style="margin-top:10px"><label><span><input type="checkbox" name="leistungen_anlegen" checked> Leistungskatalog der Branche anlegen</span></label>
        ${BRANCHE?.online_buchung !== false ? `<label><span><input type="checkbox" name="online_buchung" ${sd.buchung_token ? "checked disabled" : ""}> Online-Buchung aktivieren</span></label>` : ""}</div>
      <div class="aktionen" style="margin-top:12px"><button type="submit">Speichern und weiter</button></div></form>
    <div class="karte" style="margin-bottom:16px"><h2>Kunden aus der bisherigen Software übernehmen</h2><p class="unterzeile">CSV-Export (Excel: „Speichern unter → CSV“). Spalten wie Name, Vorname/Nachname, Telefon, E-Mail, Straße, PLZ, Ort werden erkannt${BRANCHE?.anlagentypen?.includes("fahrzeug") ? "; eine Spalte „Kennzeichen“ (mit Marke, Modell, HU bis, Kilometerstand) legt je Kunde das Fahrzeug an" : ""}. Dubletten (Rufnummer, E-Mail) werden übersprungen.</p>
      <form id="ei" class="zeile"><input type="file" name="datei" accept=".csv,text/csv,.txt" required><button type="button" class="leise" id="ei-pruefen">Prüfen</button><button type="submit">Importieren</button></form><div id="ei-ergebnis"></div></div>`;
  $("#ea").addEventListener("submit", async (ev) => {
    ev.preventDefault(); const fd = new FormData(ev.target); const stammdaten = {};
    for (const k of ["strasse", "plz", "ort", "telefon", "email", "iban", "basis_url", "impressum_url", "datenschutz_url"]) if (fd.get(k)) stammdaten[k] = fd.get(k);
    const zeilen = (name) => String(fd.get(name) || "").split("\n").map((z) => z.trim()).filter(Boolean);
    const daten = { branche: fd.get("branche"), name: fd.get("name"), stammdaten, leistungen_anlegen: fd.has("leistungen_anlegen"), online_buchung: fd.has("online_buchung"),
      mitarbeiter: zeilen("mitarbeiter").map((z) => { const [n, q] = z.split(":"); return { name: n.trim(), qualifikationen: (q || "").split(",").map((x) => x.trim()).filter(Boolean) }; }),
      sachmittel: zeilen("sachmittel").map((z) => { const [n, typ] = z.split(":"); return { name: n.trim(), typ: (typ || "").trim() }; }) };
    try { const e = await api("/betrieb/einrichten", { methode: "POST", daten }); BRANCHE = null; melden(`Gespeichert: ${e.mitarbeiter_angelegt} ${wort("ressourcen", "Personen")}, ${e.sachmittel_angelegt} Sachmittel neu`, "ok"); zeigen(); }
    catch (e) { melden(e.message); }
  });
  const importieren = async (pruefen) => {
    const datei = $("#ei input[type=file]").files[0]; if (!datei) return melden("Bitte eine CSV-Datei wählen.");
    const fd = new FormData(); fd.append("datei", datei); fd.append("pruefen", pruefen ? "true" : "false");
    const box = $("#ei-ergebnis"); box.innerHTML = "<p>Wird verarbeitet …</p>";
    try {
      const e = await api("/kunden/import", { methode: "POST", daten: fd });
      box.innerHTML = `<p>${pruefen ? "Vorschau" : "Import"}: Trennzeichen <code>${esc(e.trennzeichen)}</code> · ${e.zeilen} Zeilen · ${e.angelegt} ${pruefen ? "würden angelegt" : "angelegt"} · ${e.uebersprungen} Dubletten übersprungen${e.fahrzeuge ? ` · ${e.fahrzeuge} Fahrzeuge` : ""}${e.fehler.length ? ` · ${e.fehler.length} Fehler` : ""}</p>
        <p><small>Zuordnung: ${Object.entries(e.zuordnung).map(([k, v]) => `${esc(k)} ← ${esc(v)}`).join(", ")}</small></p>
        ${e.vorschau.length ? `<table><thead><tr><th>Name</th><th>Telefon</th><th>E-Mail</th><th>Ort</th>${e.fahrzeuge ? "<th>Kennzeichen</th>" : ""}</tr></thead><tbody>${e.vorschau.map((v) => `<tr><td>${esc(v.name)}</td><td>${esc(v.telefon || "")}</td><td>${esc(v.email || "")}</td><td>${esc(v.ort || "")}</td>${e.fahrzeuge ? `<td>${esc(v.kennzeichen || "")}</td>` : ""}</tr>`).join("")}</tbody></table>` : ""}
        ${e.fehler.length ? `<p class="fehler">${e.fehler.map((f) => `Zeile ${f.zeile}: ${esc(f.grund)}`).join("<br>")}</p>` : ""}`;
      if (!pruefen) melden(`${e.angelegt} Kunden importiert`, "ok");
    } catch (e) { box.innerHTML = `<p class="fehler">${esc(e.message)}</p>`; }
  };
  $("#ei-pruefen").addEventListener("click", () => importieren(true));
  $("#ei").addEventListener("submit", (ev) => { ev.preventDefault(); importieren(false); });
}

// --- Runde 8: Betriebswebsite -----------------------------------------------------------------
const WEBSITE_BEREICHE = [["leistungen", "Leistungen"], ["preise", "Preise (Leistungskatalog mit Preisen)"], ["buchung", "Online-Terminbuchung"], ["bewertungen", "Kundenstimmen"], ["team", "Team"], ["notdienst", "Notdienst-Leiste"], ["anfahrt", "Anfahrt und Einzugsgebiet"], ["ablauf", "So läuft es ab (3 Schritte)"], ["faq", "Häufige Fragen"], ["kontakt", "Kontaktformular und Kontaktwege"]];
async function websiteAnsicht(ziel) {
  const st = await api("/betrieb/website");
  const k = st.konfig, darf = ICH?.rechte?.includes("verwalten");
  const feld = (name, bez, typ = "text", extra = "") => `<label>${esc(bez)} <input name="${name}" type="${typ}" value="${esc(k[name] ?? "")}" ${darf ? "" : "disabled"} ${extra}></label>`;
  ziel.innerHTML = `<h1>Website</h1><p class="unterzeile">Eigene Internetseite des Betriebs aus den Stammdaten: Leistungen, Online-Buchung, Kundenstimmen, Notdienst, Kontaktformular mit Übernahme in die Anfragen. Serverseitig gerendert, ohne Cookies und ohne Fremdskripte.</p>
    <div class="kacheln">
      <div class="karte kachel"><div class="wert">${k.aktiv ? tag("online", "aktuell") : tag("nicht veröffentlicht", "offen")}</div><div class="bez">${esc(st.url)}</div></div>
      <div class="karte kachel"><div class="wert">${esc(st.leistungen)}</div><div class="bez">Leistungen auf der Seite</div></div>
      <div class="karte kachel"><div class="wert">${st.bewertungen.durchschnitt ?? "–"}</div><div class="bez">Ø aus ${esc(st.bewertungen.anzahl)} Bewertungen</div></div>
      <div class="karte kachel"><div class="wert">${st.buchung ? "ja" : "nein"}</div><div class="bez">Online-Buchung aktiv</div></div>
    </div>
    ${st.fehlend.length ? `<div class="karte banner"><div><strong>Noch unvollständig:</strong> ${st.fehlend.map(esc).join(", ")}. Anschrift, Telefon, E-Mail und Inhaber/in pflegen Sie unter <a href="#einstellungen">Einstellungen</a>; Claim und „Über uns“ hier.</div></div>` : ""}
    <div class="raster">
      <form id="ws" class="karte"><h2>Inhalte und Gestaltung</h2>
        <label>Adresse der Seite (Slug) <input name="slug" value="${esc(k.slug ?? "")}" pattern="[a-z0-9-]{3,60}" ${darf ? "" : "disabled"}><small>${esc(st.url)}</small></label>
        ${feld("domain", "Eigene Domain (optional, z. B. mueller-haustechnik.de)")}
        <small class="unterzeile">${esc(st.domain_hinweis)}</small>
        ${feld("claim", "Claim (eine Zeile unter dem Namen)")}
        <label>Über uns <textarea name="ueber_uns" rows="4" ${darf ? "" : "disabled"}>${esc(k.ueber_uns ?? "")}</textarea></label>
        ${feld("einzugsgebiet", "Einzugsgebiet (z. B. Köln, Pulheim, Frechen)")}${feld("oeffnungszeiten", "Öffnungszeiten")}
        ${feld("notdienst_telefon", "Notdienst-Rufnummer", "tel")}${feld("notdienst_text", "Notdienst-Text")}
        <div class="raster"><label>Akzentfarbe <input name="farbe" type="color" value="${esc(k.farbe)}" ${darf ? "" : "disabled"}></label>
          <label>Kundenstimmen ab Sternen <input name="bewertungen_min_sterne" type="number" min="1" max="5" value="${esc(k.bewertungen_min_sterne)}" ${darf ? "" : "disabled"}></label></div>
        ${feld("logo_url", "Logo (https://…)", "url")}${feld("bild_url", "Titelbild (https://…)", "url")}
        <label>Zusatz im Impressum (Kammer, Aufsicht, Berufsbezeichnung) <textarea name="impressum_zusatz" rows="2" ${darf ? "" : "disabled"}>${esc(k.impressum_zusatz ?? "")}</textarea></label>
        <label>Häufige Fragen (je Zeile <code>Frage? | Antwort</code>; leer = Standardfragen der Plattform) <textarea name="faq" rows="4" ${darf ? "" : "disabled"} placeholder="Arbeiten Sie auch samstags? | Ja, nach Vereinbarung.">${esc((k.faq || []).map((f) => `${f.frage} | ${f.antwort}`).join("\n"))}</textarea></label>
        <small class="unterzeile">WhatsApp-Knopf und Kontaktwege erscheinen automatisch, sobald unter Einstellungen eine WhatsApp-Nummer, Telefon und E-Mail hinterlegt sind.</small>
        <h3>Bereiche</h3><div class="raster">${WEBSITE_BEREICHE.map(([s, b]) => `<label><span><input type="checkbox" name="bereiche.${s}" ${k.bereiche?.[s] ? "checked" : ""} ${darf ? "" : "disabled"}> ${esc(b)}</span></label>`).join("")}</div>
        <label style="margin-top:10px"><span><input type="checkbox" name="aktiv" ${k.aktiv ? "checked" : ""} ${darf ? "" : "disabled"}> <strong>Website veröffentlichen</strong> (vorher Impressum und Datenschutzerklärung in der Vorschau prüfen; die Texte sind Entwürfe)</span></label>
        ${darf ? `<div class="aktionen" style="margin-top:12px"><button type="submit">Speichern</button><a class="knopf leise" href="${esc(st.vorschau)}" target="_blank" rel="noopener">Vorschau öffnen</a>${k.aktiv ? `<a class="knopf leise" href="${esc(st.url)}" target="_blank" rel="noopener">Live-Seite</a>` : ""}</div>` : ""}
      </form>
      <div>
        <div class="karte" style="margin-bottom:16px"><h2>Vorschau</h2><iframe id="ws-vorschau" src="${esc(st.vorschau)}" title="Vorschau der Website" style="width:100%;height:520px;border:1px solid var(--linie);border-radius:8px;background:#fff"></iframe></div>
        <div class="karte"><h2>Kundenstimmen freigeben</h2><p class="unterzeile">Texte erscheinen erst nach Freigabe und nur ab ${esc(k.bewertungen_min_sterne)} Sternen; der Durchschnitt zählt alle Bewertungen. Namen werden gekürzt (Datenminimierung).</p>
          ${st.bewertungen.texte.length ? `<table><tbody>${st.bewertungen.texte.map((b) => `<tr><td>${"★".repeat(b.sterne || 0)}${"☆".repeat(5 - (b.sterne || 0))}<br><small>${esc(b.name)} · ${esc(b.zeit)}</small></td><td>${esc(b.text)}</td><td style="text-align:right">${darf ? `<button class="leise" data-bewertung="${esc(b.id)}" data-wert="${b.veroeffentlicht ? "0" : "1"}">${b.veroeffentlicht ? "Zurückziehen" : "Veröffentlichen"}</button>` : (b.veroeffentlicht ? tag("öffentlich", "aktuell") : tag("intern", "neutral"))}</td></tr>`).join("")}</tbody></table>` : "<p>Noch keine Bewertungstexte. Kunden bewerten nach dem Einsatz im Kundenportal.</p>"}</div>
      </div>
    </div>`;
  $("#ws", ziel).addEventListener("submit", async (ev) => {
    ev.preventDefault(); const daten = { bereiche: {} };
    for (const [key, v] of new FormData(ev.target).entries()) { if (key.startsWith("bereiche.")) daten.bereiche[key.slice(9)] = true; else daten[key] = v === "" ? null : v; }
    for (const [s] of WEBSITE_BEREICHE) if (!(s in daten.bereiche)) daten.bereiche[s] = false;
    daten.aktiv = ev.target.aktiv.checked; daten.bewertungen_min_sterne = Number(daten.bewertungen_min_sterne || 4);
    daten.faq = (ev.target.faq.value || "").split("\n").map((z) => z.split("|")).filter((p) => p.length >= 2).map(([frage, ...rest]) => ({ frage: frage.trim(), antwort: rest.join("|").trim() }));
    try { await api("/betrieb/website", { methode: "PUT", daten }); melden("Website gespeichert", "ok"); websiteAnsicht(ziel); } catch (e) { melden(e.message); }
  });
  ziel.querySelectorAll("[data-bewertung]").forEach((b) => b.addEventListener("click", async () => {
    try { await api(`/bewertungen/${b.dataset.bewertung}/veroeffentlichen`, { methode: "POST", daten: { veroeffentlicht: b.dataset.wert === "1" } }); websiteAnsicht(ziel); } catch (e) { melden(e.message); }
  }));
}

// --- Runde 8: Upsell / Empfehlungen ----------------------------------------------------------
const EMPFEHLUNG_ART = { erneuerung: "Erneuerung", risiko: "Ausfallrisiko", foerderung: "Förderung", vertrag: "Wartungsvertrag", paket: "Paket", wiederkehr: "Wiederkehr" };
function empfehlungZeile(e, kundeId, fallId) {
  const preis = e.preis_brutto ? EURO.format(Number(e.preis_brutto)) : "nach Beratung";
  const knoepfe = [];
  if (e.aktion === "vertrag") knoepfe.push(`<button class="leise" data-emp="vertrag" data-kunde="${esc(kundeId)}" data-schluessel="${esc(e.schluessel)}">Vertrag anlegen</button>`);
  if (e.positionen?.length) knoepfe.push(`<button data-emp="angebot" data-kunde="${esc(kundeId)}" data-schluessel="${esc(e.schluessel)}" data-fall="${esc(fallId || "")}">Angebot erstellen</button>`);
  knoepfe.push(`<button class="leise" data-emp="ablehnen" data-kunde="${esc(kundeId)}" data-schluessel="${esc(e.schluessel)}" title="180 Tage nicht mehr vorschlagen">Kein Bedarf</button>`);
  return `<tr><td>${tag(EMPFEHLUNG_ART[e.art] || e.art, e.prioritaet >= 3 ? "ueberfaellig" : e.prioritaet === 2 ? "offen" : "neutral")}</td><td><strong>${esc(e.titel)}</strong><br><small>${esc(e.begruendung)}</small></td><td style="text-align:right;white-space:nowrap">${esc(preis)}${e.dauer_minuten ? `<br><small>${e.dauer_minuten} min</small>` : ""}</td><td><div class="aktionen">${knoepfe.join("")}</div></td></tr>`;
}
function empfehlungenBinden(wurzel, neuLaden) {
  wurzel.querySelectorAll("[data-emp]").forEach((b) => b.addEventListener("click", async () => {
    const { emp, kunde, schluessel, fall } = b.dataset; b.disabled = true;
    try {
      if (emp === "angebot") {
        const senden = ICH?.rechte?.includes("bearbeiten") && confirm("Angebot gleich zum Versand freigeben lassen? (Abbrechen = nur Entwurf)");
        const a = await api(`/kunden/${kunde}/empfehlungen/${encodeURIComponent(schluessel)}/angebot`, { methode: "POST", daten: { servicefall_id: fall || null, versenden: !!senden } });
        melden(`Angebot ${a.nummer} über ${EURO.format(Number(a.brutto))} erstellt${senden ? ", Versand in der Freigabe-Inbox" : " (Entwurf unter Angebote)"}`, "ok");
      } else if (emp === "vertrag") {
        const v = await api(`/kunden/${kunde}/empfehlungen/wartungsvertrag/vertrag`, { methode: "POST", daten: {} });
        melden(`Wartungsvertrag ${v.nummer || ""} angelegt`, "ok");
      } else {
        await api(`/kunden/${kunde}/empfehlungen/${encodeURIComponent(schluessel)}/ablehnen`, { methode: "POST", daten: { grund: null } });
        melden("Für 180 Tage ausgeblendet", "ok");
      }
      neuLaden();
    } catch (e) { melden(e.message); b.disabled = false; }
  }));
}
async function kundeEmpfehlungen(wurzel, id) {
  const wir = await ich(); if (!wir.rechte.includes("bearbeiten")) return;
  const d = await api(`/kunden/${id}/empfehlungen`).catch(() => null); if (!d) return;
  const teil = document.createElement("div");
  teil.innerHTML = `<h3 style="margin-top:16px">Empfehlungen <small>· Potenzial ${esc(EURO.format(Number(d.potenzial)))}</small></h3>
    ${d.empfehlungen.length ? `<table><tbody>${d.empfehlungen.map((e) => empfehlungZeile(e, id)).join("")}</tbody></table>` : "<p>Derzeit keine Empfehlung: Anlagen aktuell, Wartungsvertrag vorhanden oder Vorschläge in den letzten 180 Tagen abgelehnt.</p>"}`;
  $("#kd-formular", wurzel).before(teil);
  empfehlungenBinden(teil, () => kundeDetail(id));
}
async function fallEmpfehlungen(wurzel, fallId) {
  const wir = await ich(); if (!wir.rechte.includes("einsatz")) return;
  const d = await api(`/servicefaelle/${fallId}/empfehlungen`).catch(() => null); if (!d || !d.empfehlungen.length) return;
  const teil = document.createElement("div");
  teil.innerHTML = `<h3 style="margin-top:16px">Vor Ort anbieten</h3><p class="unterzeile">Aus Anlagenalter, Erneuerungskalender, Anliegen und Saison; ein Klick erzeugt das Angebot.</p><table><tbody>${d.empfehlungen.slice(0, 4).map((e) => empfehlungZeile(e, d.kunde_id, fallId)).join("")}</tbody></table>`;
  wurzel.append(teil);
  empfehlungenBinden(teil, () => fallDetail(fallId));
}
async function upsellAnsicht(ziel) {
  const [u, pakete] = await Promise.all([api("/upsell/uebersicht?limit=40"), api("/upsell/pakete")]);
  const l = u.letzte_90_tage;
  ziel.innerHTML = `<h1>Empfehlungen und Zusatzgeschäft</h1><p class="unterzeile">Was dem Betrieb je Kunde fachlich sinnvoll zu bieten hat: Erneuerungen aus dem Lebenszyklus, Ausfallrisiko, Heizungstausch mit Förderung, Wartungsvertrag, Saisonpakete, Folgetermine. Vorschläge erscheinen im Kundendetail, im Rapport, im Kundenportal und beim Anruf; die Tagesaufgabe erinnert Kunden mit Einwilligung höchstens alle 180 Tage.</p>
    <div class="kacheln">
      ${[[EURO.format(Number(u.potenzial)), "Potenzial brutto (offene Empfehlungen)"], [u.kunden_mit_empfehlung, "Kunden mit Empfehlung"], [l.angeboten, "angeboten (90 Tage)"], [l.angenommen, "angenommen (90 Tage)"], [l.quote == null ? "–" : `${Math.round(l.quote * 100)} %`, "Annahmequote"], [EURO.format(Number(l.umsatz_angenommen)), "Umsatz aus Empfehlungen"]].map(([w, b]) => `<div class="karte kachel"><div class="wert">${esc(w)}</div><div class="bez">${esc(b)}</div></div>`).join("")}
    </div>
    <div class="raster">
      <div class="karte"><h2>Kunden mit dem größten Potenzial</h2>${u.kunden.length ? `<table><thead><tr><th></th><th>Kunde</th><th>Vorschläge</th><th style="text-align:right">Potenzial</th></tr></thead><tbody>${u.kunden.map((k) => `<tr class="klickbar" data-kunde="${esc(k.kunde_id)}"><td>${tag(k.prioritaet >= 3 ? "dringend" : k.prioritaet === 2 ? "sinnvoll" : "optional", k.prioritaet >= 3 ? "ueberfaellig" : k.prioritaet === 2 ? "offen" : "neutral")}</td><td>${esc(k.name)}<br><small>${esc(k.ort || "")}</small></td><td><small>${k.top.map((t) => esc(t.titel || t)).join(" · ")}${k.anzahl > 3 ? ` · +${k.anzahl - 3}` : ""}</small></td><td style="text-align:right">${esc(EURO.format(Number(k.potenzial)))}</td></tr>`).join("")}</tbody></table>` : "<p>Keine offenen Empfehlungen.</p>"}</div>
      <div>
        <div class="karte" style="margin-bottom:16px"><h2>Letzte 90 Tage je Art</h2><table><tbody>${Object.entries(l.je_art || {}).map(([a, n]) => `<tr><td>${esc(EMPFEHLUNG_ART[a] || a)}</td><td style="text-align:right">${esc(n)}</td></tr>`).join("") || "<tr><td>Noch nichts angeboten.</td></tr>"}</tbody></table></div>
        <div class="karte"><h2>Pakete der Branche ${esc(BRANCHE?.bezeichnung || "")}</h2><p class="unterzeile">Festpreise brutto; Saisonpakete erscheinen nur in ihren Monaten oder bei passendem Anliegen. Preise lassen sich im Artikelstamm überschreiben.</p>
          <table><tbody>${pakete.map((p) => `<tr><td><strong>${esc(p.bezeichnung)}</strong><br><small>${esc(p.beschreibung)}</small></td><td style="text-align:right;white-space:nowrap">${p.preis_brutto === "0.00" ? "Beratung" : esc(EURO.format(Number(p.preis_brutto)))}${p.monate?.length ? `<br><small>Monate ${p.monate.join(", ")}</small>` : ""}</td></tr>`).join("")}</tbody></table></div>
      </div>
    </div>`;
  ziel.querySelectorAll("[data-kunde]").forEach((z) => z.addEventListener("click", () => kundeDetail(z.dataset.kunde)));
}


// --- Runde 9: Integrations-Hub -----------------------------------------------------------------
const ZUSTAND_TEXT = { aktiv: ["aktiv", "aktuell"], gestoert: ["gestört", "ueberfaellig"], vorbereitet: ["eingerichtet, nicht aktiv", "offen"], aus: ["aus", "neutral"], unbekannt: ["nicht konfiguriert", "neutral"] };
function anbindungFormular(ziel, a, e) {
  const kanaele = e.kanaele || [];
  return `<form data-ziel="${ziel}" class="anbindung">
    ${Object.entries(a.typen).map(([typ, felder]) => `<div style="margin-bottom:10px"><label><span><input type="checkbox" name="kanal" value="${typ}" ${kanaele.includes(typ) ? "checked" : ""}> <strong>${esc(typ)}</strong></span></label>
      ${felder.length ? `<div class="raster">${felder.map(([k, b, geheim]) => `<label>${esc(b)} <input name="${typ}.${k}" ${geheim ? 'type="password" autocomplete="new-password"' : ""} value="${esc((e[typ] || {})[k] ?? "")}"></label>`).join("")}</div>` : ""}</div>`).join("")}
    <div class="aktionen"><span class="fehler" data-meldung hidden></span><button type="submit">Speichern</button></div></form>`;
}
function anbindungBinden(wurzel, danach) {
  wurzel.querySelectorAll("form[data-ziel]").forEach((f) => f.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(f); const daten = { kanaele: fd.getAll("kanal") };
    for (const [k, v] of fd.entries()) { if (!k.includes(".")) continue; const [typ, feld] = k.split("."); if (v !== "") (daten[typ] ||= {})[feld] = v; }
    const meldung = f.querySelector("[data-meldung]"); meldung.hidden = true;
    try { await api(`/integrationen/${f.dataset.ziel}`, { methode: "PUT", daten }); melden("Gespeichert", "ok"); danach?.(); }
    catch (e) { meldung.className = "fehler"; meldung.textContent = e.message; meldung.hidden = false; }
  }));
}
async function integrationenAnsicht(ziel) {
  const [st, integ, betrieb] = await Promise.all([api("/integrationen/status"), api("/integrationen"), api("/betrieb")]);
  const basis = (betrieb.stammdaten || {}).basis_url || location.origin;
  const kategorien = [["kunde", "Kundenkommunikation und Zahlung"], ["finanzen", "Buchhaltung und Steuerkanzlei"], ["betrieb", "Fachsoftware und Hinweise"]];
  const karte = (i) => {
    const [text, klasse] = ZUSTAND_TEXT[i.zustand] || ZUSTAND_TEXT.unbekannt; const o = i.outbox;
    return `<div class="karte integration" data-ziel="${esc(i.ziel)}" data-typ="${esc(i.typ)}"><div class="zeile" style="justify-content:space-between;align-items:flex-start"><h3 style="margin:0">${esc(i.bezeichnung)}</h3>${tag(text, klasse)}</div>
      <p class="unterzeile">${esc(i.text)}</p>
      ${o ? `<p><small>Outbox: ${o.zugestellt} zugestellt · ${o.ausstehend} ausstehend · ${o.tot} Dead Letter${o.letzter_fehler ? `<br><span class="fehler">Letzter Fehler: ${esc(o.letzter_fehler)}</span>` : ""}</small></p>` : ""}
      ${i.reihenfolge ? `<p><small>Reihenfolge im Ziel „${esc(i.ziel)}“: ${i.reihenfolge}.</small></p>` : ""}
      <div class="aktionen"><button class="leise" data-pruefen="${esc(i.ziel)}:${esc(i.typ)}" ${i.konfiguriert ? "" : "disabled"}>Verbindung prüfen</button><button class="leise" data-bearbeiten="${esc(i.ziel)}">Einrichten</button>${i.doku.startsWith("http") ? `<a href="${esc(i.doku)}" target="_blank" rel="noopener"><small>Anleitung ↗</small></a>` : `<small>${esc(i.doku)}</small>`}</div>
      <div class="pruefung" hidden></div></div>`;
  };
  ziel.innerHTML = `<h1>Integrationen</h1><p class="unterzeile">${st.aktiv} Anbindungen aktiv${st.gestoert ? `, <strong class="fehler">${st.gestoert} gestört</strong>` : ""}. Zugangsdaten werden verschlüsselt gespeichert; „Verbindung prüfen“ ruft den Dienst mit den gespeicherten Daten auf. Kundenkanäle: ${st.kunde_kanaele.length ? esc(st.kunde_kanaele.join(" → ")) : "nur CSV (keine Kundenkommunikation!)"}.</p>
    <div class="karte banner"><div><strong>Webhook-Adressen für eingehende Ereignisse</strong><br><small>WhatsApp (Meta App → Webhooks, Feld messages): <code>${esc(basis)}${esc(st.webhooks.whatsapp)}</code> · Stripe (Ereignis checkout.session.completed): <code>${esc(basis)}${esc(st.webhooks.stripe)}</code></small></div></div>
    ${kategorien.map(([k, titel]) => `<h2 style="margin:18px 0 10px">${esc(titel)}</h2><div class="raster">${st.integrationen.filter((i) => i.kategorie === k).map(karte).join("")}</div>`).join("")}
    <h2 style="margin:18px 0 10px">Systemdienste</h2><div class="raster">${st.dienste.map((d) => { const [text, klasse] = ZUSTAND_TEXT[d.zustand] || ZUSTAND_TEXT.unbekannt; return `<div class="karte"><div class="zeile" style="justify-content:space-between"><h3 style="margin:0">${esc(d.bezeichnung)}</h3>${tag(text, klasse)}</div><p class="unterzeile">${esc(d.text)} <small>(${esc(d.doku)})</small></p></div>`; }).join("")}</div>
    <div id="int-formular"></div>`;
  ziel.querySelectorAll("[data-pruefen]").forEach((b) => b.addEventListener("click", async () => {
    const [z, typ] = b.dataset.pruefen.split(":"); const box = b.closest(".karte").querySelector(".pruefung"); b.disabled = true; box.hidden = false; box.innerHTML = "<small>Wird geprüft …</small>";
    try { const r = await api(`/integrationen/${z}/${typ}/pruefen`, { methode: "POST", daten: {} }); box.innerHTML = `<div class="${r.ok ? "hinweis" : "fehler"}">${esc(r.meldung)}</div>`; }
    catch (e) { box.innerHTML = `<div class="fehler">${esc(e.message)}</div>`; }
    b.disabled = false;
  }));
  ziel.querySelectorAll("[data-bearbeiten]").forEach((b) => b.addEventListener("click", () => {
    const z = b.dataset.bearbeiten; const a = ANBINDUNGEN[z]; if (!a) return;
    const box = $("#int-formular", ziel);
    box.innerHTML = `<div class="karte" style="margin-top:16px"><h2>${esc(a.titel)}</h2><p class="unterzeile">${esc(a.hinweis)}</p>${anbindungFormular(z, a, integ.ziele[z] || {})}</div>`;
    anbindungBinden(box, () => integrationenAnsicht(ziel)); box.scrollIntoView({ behavior: "smooth" });
  }));
}
