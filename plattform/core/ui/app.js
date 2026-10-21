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
const ANSICHTEN = { cockpit, anruf, freigaben, vorgaenge, plantafel, kunden, wartung, ontologie, rechnungen, protokoll, einstellungen,
  einsatz: einsatzAnsicht, belege: belegeAnsicht, zeiten: zeitenAnsicht, betrieb: betriebAnsicht };
async function zeigen() {
  if (!speicher("mandant")) return anmelden();
  $("#mandant-name").textContent = speicher("mandantName") || "";
  try {
    const wir = await ich();
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
async function freigabenZaehlen() {
  try { const n = (await api("/freigaben")).length; $("#zaehler-freigaben").innerHTML = n ? tag(n, "offen", n) : ""; } catch { /* Anmeldung läuft */ }
  try { if (ICH?.rechte.includes("finanzen")) { const n = (await api("/belege?status=erfasst")).length; $("#zaehler-belege").innerHTML = n ? tag(n, "offen", n) : ""; } } catch { /* Anmeldung läuft */ }
}
async function dateiLaden(pfad, name) {
  const r = await fetch(pfad, { headers: authKopf() });
  if (!r.ok) return alert(`HTTP ${r.status}`);
  const url = URL.createObjectURL(await r.blob()); const a = document.createElement("a"); a.href = url; a.download = name; a.target = "_blank"; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}
async function bildUrl(anhangId) {  // Anhänge liegen hinter der Authentifizierung, daher als Blob-URL laden
  const r = await fetch(`/anhaenge/${anhangId}/inhalt`, { headers: authKopf() });
  return r.ok ? URL.createObjectURL(await r.blob()) : "";
}
let ICH = null;  // Prinzipal der Sitzung (Name, Rollen, Rechte), nach jedem Anmelden neu geladen
async function ich() { return ICH || (ICH = await api("/ich", { mitMandant: false })); }
function detail(html) { $("#detail-inhalt").innerHTML = html; const d = $("#detail"); if (!d.open) d.showModal(); return $("#detail-inhalt"); }

// --- Cockpit ---------------------------------------------------------------------------------
async function cockpit(ziel) {
  const [k, z, notfaelle] = await Promise.all([api("/kennzahlen"), api("/twin/betrieb"), api("/servicefaelle?dringlichkeit=notdienst")]);
  const hoch = await api("/servicefaelle?dringlichkeit=hoch");
  const dringend = [...notfaelle, ...hoch].filter((f) => ["eingegangen", "qualifiziert"].includes(f.status));
  const kachel = (wert, bez) => `<div class="karte kachel"><div class="wert">${esc(wert ?? "–")}</div><div class="bez">${esc(bez)}</div></div>`;
  ziel.innerHTML = `
    <h1>Cockpit</h1><p class="unterzeile">Digital Twin des Betriebs · letzte ${k.zeitraum_tage} Tage</p>
    <div class="kacheln">
      ${kachel(k.offene_vorgaenge, "offene Vorgänge")}
      ${kachel(k.offene_freigaben, "offene Freigaben")}
      ${kachel(k.anrufe_erfasst, "Anrufe erfasst")}
      ${kachel(z.notdienst_offen, "Notdienst offen")}
      ${kachel(k.median_stunden_bis_einplanung === null ? "–" : `${k.median_stunden_bis_einplanung} h`, "Median Eingang → Einplanung")}
      ${kachel(k.automatisierungsquote === null ? "–" : `${Math.round(k.automatisierungsquote * 100)} %`, "automatisch freigegeben")}
      ${kachel(z.wartungspotenzial.length, "Wartungen fällig")}
      ${kachel(k.outbox_tot, "Zustellfehler (Dead Letter)")}
    </div>
    <div class="raster">
      <div class="karte"><h2>Dringend und noch nicht eingeplant</h2>${dringend.length ? `<table><tbody>${dringend.map((f) =>
        `<tr class="klickbar" data-fall="${esc(f.id)}"><td>${tag(f.dringlichkeit)}</td><td>${esc(f.anliegen)}</td><td>${esc(STATUS_TEXT[f.status])}</td></tr>`).join("")}</tbody></table>` : "<p>Nichts offen.</p>"}</div>
      <div class="karte"><h2>Auslastung nächste 5 Tage</h2>${z.auslastung.map((a) => `
        <div style="margin-bottom:10px"><div class="zeile" style="justify-content:space-between"><span>${esc(a.name)}</span><span>${a.gebucht_h} / ${a.kapazitaet_h} h</span></div>
        <div class="balken"><span style="width:${Math.min(100, Math.round((a.quote || 0) * 100))}%"></span></div></div>`).join("")}</div>
    </div>
    <div class="raster">
      <div class="karte"><h2>Vorgänge je Status</h2><table><tbody>${STATUS.map((s) => `<tr><td>${STATUS_TEXT[s]}</td><td style="text-align:right">${z.vorgaenge_je_status[s] || 0}</td></tr>`).join("")}</tbody></table></div>
      <div class="karte"><h2>Letzte Ereignisse</h2><ul class="verlauf">${z.letzte_ereignisse.slice(0, 8).map((e) => `<li>${tag(e.akteur, "neutral")} <code>${esc(e.typ)}</code> · ${zeit(e.zeit)}</li>`).join("")}</ul></div>
    </div>`;
  ziel.querySelectorAll("[data-fall]").forEach((z) => z.addEventListener("click", () => fallDetail(z.dataset.fall)));
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
      <td><div class="aktionen"><button class="gefahr" data-entscheidung="${esc(f.id)}" data-ja="0"${sperre(f)}>Ablehnen</button><button data-entscheidung="${esc(f.id)}" data-ja="1"${sperre(f)}>Freigeben</button></div></td></tr>`).join("")
      || `<tr><td colspan="5">Keine offenen Freigaben.</td></tr>`}
    </tbody></table></div></div>
    <h2 style="margin-top:24px">Zuletzt freigegeben</h2>
    <div class="karte"><table><tbody>${erledigt.slice(-8).reverse().map((f) => `<tr><td>${tag(f.risikoklasse)}</td><td>${esc(AKTION_TEXT[f.aktion] || f.aktion)}</td><td>${esc(f.entschieden_von)}</td><td>${esc(f.begruendung || "")}</td></tr>`).join("") || "<tr><td>–</td></tr>"}</tbody></table></div>`;
  ziel.querySelectorAll("[data-entscheidung]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try { await api(`/freigaben/${b.dataset.entscheidung}/entscheidung`, { methode: "POST", daten: { freigeben: b.dataset.ja === "1", wer: "buero" } }); zeigen(); }
    catch (e) { b.disabled = false; alert(e.message); }
  }));
  ziel.querySelectorAll("a[data-fall]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); fallDetail(a.dataset.fall); }));
}

// --- Vorgänge (Kanban) -----------------------------------------------------------------------
async function vorgaenge(ziel) {
  const faelle = await api("/servicefaelle?limit=500");
  ziel.innerHTML = `<h1>Vorgänge</h1><p class="unterzeile">Statusautomat des Servicefalls; jeder Übergang wird im Ereignisprotokoll festgehalten.</p>
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
    <h3>Termine</h3>${d.termine.length ? `<ul class="verlauf">${d.termine.map((t) => `<li>${zeit(t.beginn)} ${tag(t.status)}</li>`).join("")}</ul>` : "<p>Noch kein Termin.</p>"}
    <div class="aktionen" style="margin:12px 0">${(UEBERGAENGE[f.status] || []).map((z) => `<button class="${z === "storniert" ? "gefahr" : "leise"}" data-ziel="${z}">${STATUS_TEXT[z]}</button>`).join("")}
      ${["eingegangen", "qualifiziert"].includes(f.status) ? `<button id="termin-suchen">Termin vorschlagen</button>` : ""}</div>
    <div id="terminwahl"></div>
    <h3>Ereignisse</h3><ul class="verlauf">${d.ereignisse.map((e) => `<li>${tag(e.akteur, "neutral")} <code>${esc(e.typ)}</code> · ${zeit(e.zeit)}${e.payload?.grund ? ` · ${esc(e.payload.grund)}` : ""}</li>`).join("")}</ul>`);
  fallErgaenzungen(wurzel, id);
  wurzel.querySelectorAll("[data-ziel]").forEach((b) => b.addEventListener("click", async () => {
    try { await api(`/servicefaelle/${id}/status`, { methode: "POST", daten: { ziel: b.dataset.ziel, wer: "buero" } }); await fallDetail(id); zeigen(); }
    catch (e) { alert(e.message); }
  }));
  $("#termin-suchen", wurzel)?.addEventListener("click", async () => {
    const q = f.benoetigte_qualifikation ? `&qualifikation=${encodeURIComponent(f.benoetigte_qualifikation)}` : "";
    const fenster = await api(`/disposition/zeitfenster?dauer_minuten=${f.dauer_minuten}&anzahl=6${q}`);
    $("#terminwahl", wurzel).innerHTML = fenster.length ? `<div class="wahl">${fenster.map((z, i) => `<button class="leise" data-i="${i}">${esc(WOCHENTAG.format(new Date(z.beginn)))} ${esc(UHRZEIT.format(new Date(z.beginn)))} · ${esc(z.ressource_name.split(" (")[0])}</button>`).join("")}</div>` : "<p>Kein passendes Zeitfenster.</p>";
    $("#terminwahl", wurzel).querySelectorAll("[data-i]").forEach((b) => b.addEventListener("click", async () => {
      const z = fenster[Number(b.dataset.i)];
      try { await api(`/servicefaelle/${id}/termine`, { methode: "POST", daten: { ressource_id: z.ressource_id, beginn: z.beginn } }); await fallDetail(id); zeigen(); }
      catch (e) { alert(e.message); }
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
      <div class="pt-zelle pt-kopf">Monteur</div>${tage.map((d) => `<div class="pt-zelle pt-kopf">${esc(WOCHENTAG.format(d))}</div>`).join("")}
      ${p.ressourcen.map((r) => `<div class="pt-zelle"><strong>${esc(r.name.split(" (")[0])}</strong><br><small>${esc(r.qualifikationen.join(", "))}</small><br><small>${esc(r.arbeitsbeginn)}–${esc(r.arbeitsende)}</small></div>
        ${tage.map((d) => `<div class="pt-zelle">${r.termine.filter((t) => gleicherTag(t.beginn, d)).map((t) => `
          <div class="einsatz ${esc(t.status)} ${t.dringlichkeit === "notdienst" ? "notdienst" : ""}" data-fall="${esc(t.servicefall_id)}">
          <strong>${esc(UHRZEIT.format(new Date(t.beginn)))}–${esc(UHRZEIT.format(new Date(t.ende)))}</strong> ${esc(t.kunde)}<br>${esc(t.anliegen)}</div>`).join("")}</div>`).join("")}`).join("")}
    </div></div>`;
  ziel.querySelectorAll("[data-fall]").forEach((k) => k.addEventListener("click", () => fallDetail(k.dataset.fall)));
  if (wir.rechte.includes("verwalten")) await monteureVerwalten(ziel);
}

// Inhaber: Anmeldename je Monteur, damit die Monteur-Rolle nur eigene Einsätze sieht
async function monteureVerwalten(ziel) {
  const liste = await api("/ressourcen");
  const bereich = document.createElement("div");
  bereich.innerHTML = `<h2 style="margin-top:24px">Monteure</h2>
    <div class="karte"><div class="tabelle-rahmen"><table><thead><tr><th>Monteur</th><th>Qualifikationen</th><th>Anmeldename (Keycloak)</th><th></th></tr></thead><tbody>
    ${liste.map((r) => `<tr><td>${esc(r.name)}${r.aktiv ? "" : ` ${tag("inaktiv", "neutral")}`}</td><td>${esc(r.qualifikationen.join(", "))}</td>
      <td><input data-benutzer="${esc(r.id)}" value="${esc(r.benutzer || "")}" placeholder="z. B. m.arndt" autocomplete="off"></td>
      <td><button class="leise" data-speichern="${esc(r.id)}">Speichern</button></td></tr>`).join("")}</tbody></table></div></div>`;
  ziel.append(bereich);
  bereich.querySelectorAll("[data-speichern]").forEach((b) => b.addEventListener("click", async () => {
    const id = b.dataset.speichern, wert = bereich.querySelector(`[data-benutzer="${id}"]`).value.trim();
    b.disabled = true;
    try { await api(`/ressourcen/${id}`, { methode: "PATCH", daten: { benutzer: wert || null } }); b.textContent = "Gespeichert"; }
    catch (e) { alert(e.message); } finally { b.disabled = false; }
  }));
}

// --- Kunden und Anlagen ----------------------------------------------------------------------
async function kunden(ziel) {
  ziel.innerHTML = `<h1>Kunden &amp; Anlagen</h1><p class="unterzeile">Kanonisches Datenmodell: Kunde → Objekt → Anlage; Dubletten über die Rufnummer.</p>
    <div class="karte" style="margin-bottom:16px"><div class="zeile"><label>Suche <input id="k-suche" placeholder="Name, Ort oder E-Mail"></label></div></div>
    <div class="karte"><div class="tabelle-rahmen"><table><thead><tr><th>Name</th><th>Ort</th><th>Telefon</th><th>Vertrag</th></tr></thead><tbody id="k-liste"></tbody></table></div></div>`;
  const laden = async () => {
    const q = $("#k-suche").value.trim();
    const liste = await api(`/kunden?limit=200${q ? `&q=${encodeURIComponent(q)}` : ""}`);
    $("#k-liste").innerHTML = liste.map((k) => `<tr class="klickbar" data-kunde="${esc(k.id)}"><td>${esc(k.name)}</td><td>${esc(k.ort || "")}</td><td>${esc(k.telefon || "")}</td><td>${k.vertragsstatus === "wartungsvertrag" ? tag("Wartungsvertrag", "A") : ""}</td></tr>`).join("") || `<tr><td colspan="4">Keine Treffer.</td></tr>`;
    $("#k-liste").querySelectorAll("[data-kunde]").forEach((z) => z.addEventListener("click", () => kundeDetail(z.dataset.kunde)));
  };
  let warte; $("#k-suche").addEventListener("input", () => { clearTimeout(warte); warte = setTimeout(laden, 250); });
  await laden();
}
async function kundeDetail(id) {
  const d = await api(`/kunden/${id}`);
  const anlagen = d.objekte.flatMap((o) => o.anlagen);
  const zwillinge = await Promise.all(anlagen.map((a) => api(`/twin/anlagen/${a.id}`)));
  const wurzel = detail(`<h2>${esc(d.kunde.name)}</h2>
    <p>${esc(d.kunde.strasse || "")}, ${esc(d.kunde.plz || "")} ${esc(d.kunde.ort || "")} · ${esc(d.kunde.telefon || "")}</p>
    <h3>Anlagen (Digital Twin)</h3>
    <table><thead><tr><th>Anlage</th><th>Baujahr</th><th>Letzte Wartung</th><th>Status</th><th>Qualifikation</th></tr></thead><tbody>
    ${zwillinge.map((z) => `<tr><td>${esc(z.anlage.bezeichnung)}${z.anlage.hersteller ? ` · ${esc(z.anlage.hersteller)}` : ""}<br><small><code>shk:${esc(z.anlage.ontologieklasse)}</code></small></td>
      <td>${esc(z.anlage.baujahr || "–")}</td><td>${esc(z.wartung.letzte || "nie")}</td><td>${tag(z.wartung.status)}</td><td>${esc(z.wartung.benoetigte_qualifikation || "–")}</td></tr>`).join("") || `<tr><td colspan="5">Keine Anlagen erfasst.</td></tr>`}
    </tbody></table>
    <h3 style="margin-top:16px">Vorgänge</h3><table><tbody>${d.servicefaelle.map((f) => `<tr class="klickbar" data-fall="${esc(f.id)}"><td>${esc(f.anliegen)}</td><td>${tag(f.dringlichkeit)}</td><td>${esc(STATUS_TEXT[f.status])}</td><td>${zeit(f.erstellt)}</td></tr>`).join("") || "<tr><td>Keine.</td></tr>"}</tbody></table>`);
  wurzel.querySelectorAll("[data-fall]").forEach((z) => z.addEventListener("click", () => fallDetail(z.dataset.fall)));
  kundeDokumente(wurzel, id);
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
    <textarea id="o-abfrage" rows="8">${esc(Object.values(ABFRAGEN)[0])}</textarea><div class="aktionen" style="margin-top:8px"><button id="o-start">Abfrage ausführen</button></div></div>
    <div class="karte" id="o-ergebnis"></div>`;
  const ausfuehren = async () => {
    try {
      const r = await api("/ontologie/sparql", { methode: "POST", daten: { abfrage: $("#o-abfrage").value } });
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
    try { await api(`/rechnungen/${b.dataset.storno}/storno`, { methode: "POST", daten: { grund } }); alert("Storno liegt in der Freigabe-Inbox (Klasse C)."); zeigen(); }
    catch (e) { alert(e.message); }
  }));
  $("#r-belegpaket").addEventListener("click", async () => {
    const z = $("#r-zeitraum").value; if (!z) return alert("Monat wählen");
    try { await api("/kanzlei/belegpaket", { methode: "POST", daten: { zeitraum: z } }); alert("Belegpaket liegt in der Freigabe-Inbox (Klasse C)."); }
    catch (e) { alert(e.message); }
  });
}

// --- Einstellungen (Inhaber): Stammdaten und Anbindungen -------------------------------------
const FELDER_STAMM = [["inhaber", "Inhaber/in"], ["strasse", "Straße"], ["plz", "PLZ"], ["ort", "Ort"], ["telefon", "Telefon"], ["email", "E-Mail"],
  ["ust_id", "USt-IdNr."], ["steuernummer", "Steuernummer"], ["handwerksrolle", "Handwerksrolle"], ["iban", "IBAN"], ["bic", "BIC"], ["bank", "Bank"],
  ["zahlungsziel_tage", "Zahlungsziel (Tage)"], ["rechnungsnummer_praefix", "Rechnungsnummer-Präfix"], ["rechnungshinweis", "Hinweis auf der Rechnung"],
  ["kontenrahmen", "Kontenrahmen (SKR03/SKR04)"], ["datev_beraternummer", "DATEV Beraternummer"], ["datev_mandantennummer", "DATEV Mandantennummer"], ["kanzlei_email", "E-Mail der Kanzlei"],
  ["stundensatz_kosten", "Stundensatz intern (€/h, Nachkalkulation)"], ["stundensatz_verrechnung", "Stundensatz Verrechnung (€/h)"], ["materialaufschlag_prozent", "Materialaufschlag (%)"]];
const ANBINDUNGEN = {
  fachsoftware: { titel: "Fachsoftware", hinweis: "Termine und Aufträge. Bis die Schreib-API der Fachsoftware geklärt ist: Webhook (z. B. n8n) oder CSV-Export.",
    typen: { webhook: [["url", "Webhook-URL"], ["signatur_schluessel", "Signaturschlüssel (HMAC)", true], ["api_key", "Bearer-Token (optional)", true]], csv: [] } },
  endkunde: { titel: "Kundenkommunikation", hinweis: "Terminbestätigung, Nachrichten, Rechnungen. Reihenfolge: E-Mail, dann SMS (nur mit Einwilligung).",
    typen: { email: [["absender", "Absenderadresse"], ["host", "SMTP-Server"], ["port", "Port (587 STARTTLS / 465 SMTPS)"], ["benutzer", "SMTP-Benutzer"], ["passwort", "SMTP-Passwort", true], ["antwort_an", "Antwort an (optional)"]],
      sms: [["api_key", "seven.io API-Key", true], ["absender", "Absenderkennung (max. 11 Zeichen)"]], csv: [] } },
  finanz: { titel: "Buchhaltung", hinweis: "Rechnungen und Stornos als Entwurf in die Buchhaltungssoftware.",
    typen: { lexware: [["api_key", "Lexware Office API-Key", true]], sevdesk: [["api_token", "sevDesk API-Token", true]], csv: [] } },
  kanzlei: { titel: "Steuerkanzlei", hinweis: "Monatliches Belegpaket (PDF, XRechnung, DATEV-Buchungsstapel) per E-Mail oder als Datei.",
    typen: { email: [["kanzlei_email", "E-Mail der Kanzlei"], ["absender", "Absenderadresse"], ["host", "SMTP-Server"], ["port", "Port"], ["benutzer", "SMTP-Benutzer"], ["passwort", "SMTP-Passwort", true]],
      belegpaket: [["verzeichnis", "Ablageverzeichnis auf dem Server"]] } },
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
    for (const [k, v] of fd.entries()) if (k !== "kleinunternehmer" && v !== "") daten[k] = k === "zahlungsziel_tage" ? Number(v) : v;
    daten.kleinunternehmer = fd.has("kleinunternehmer");
    try { await api("/betrieb/stammdaten", { methode: "PUT", daten }); alert("Gespeichert"); } catch (e) { alert(e.message); }
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
  ziel.innerHTML = `<h1>Mein Einsatz</h1><p class="unterzeile">${esc(DATUM.format(new Date()))}${h.ressource ? ` · ${esc(h.ressource.name.split(" (")[0])}` : ""}${h.bereitschaft ? ` · Bereitschaft: ${esc(h.bereitschaft.name)} ${esc(h.bereitschaft.telefon)}` : ""}</p>
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
        <br><a target="_blank" rel="noopener" href="https://www.openstreetmap.org/search?query=${encodeURIComponent(`${t.kunde.strasse || ""} ${t.kunde.plz || ""} ${t.kunde.ort || ""}`)}">Karte</a></p>
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
  const starten = async (art, fall) => { try { await api("/zeiten/start", { methode: "POST", daten: { art, servicefall_id: fall, ressource_id: rid } }); zeigen(); } catch (e) { alert(e.message); } };
  ziel.querySelectorAll("[data-start]").forEach((b) => b.addEventListener("click", () => starten(b.dataset.start)));
  ziel.querySelectorAll("[data-arbeit]").forEach((b) => b.addEventListener("click", () => starten("arbeit", b.dataset.arbeit)));
  $("#e-stop")?.addEventListener("click", async () => { try { await api(`/zeiten/${laufend.id}/stop`, { methode: "POST" }); zeigen(); } catch (e) { alert(e.message); } });
  ziel.querySelectorAll("[data-fall]").forEach((b) => b.addEventListener("click", () => fallDetail(b.dataset.fall)));
  ziel.querySelectorAll("[data-rapport]").forEach((b) => b.addEventListener("click", () => rapportFormular(b.dataset.rapport, b.dataset.termin, rid)));
  $("#e-beleg-senden")?.addEventListener("click", async () => {
    const datei = $("#e-beleg").files[0]; if (!datei) return alert("Bitte Datei oder Foto wählen");
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
  const summe = (art) => zeiten.filter((z) => z.art === art).reduce((a, z) => a + z.minuten, 0);
  const wurzel = detail(`<h2>Rapport · ${esc(d.servicefall.anliegen)}</h2>
    <label>Ausgeführte Arbeiten <textarea id="rp-taetigkeiten" style="font-family:inherit" placeholder="Was wurde gemacht, welche Ursache, welche Teile?"></textarea></label>
    <div class="zeile"><label>Arbeitszeit (min) <input id="rp-arbeit" type="number" min="0" value="${summe("arbeit")}"></label>
      <label>Fahrzeit (min) <input id="rp-fahrt" type="number" min="0" value="${summe("fahrt")}"></label>
      <label>Ergebnis <select id="rp-ergebnis">${Object.entries(ERGEBNIS_TEXT).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label></div>
    <label>Material (eine Zeile je Position: Menge; Bezeichnung) <textarea id="rp-material" style="font-family:inherit" placeholder="1; Umwälzpumpe Grundfos UPM3&#10;2; Dichtung 3/4&quot;"></textarea></label>
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
      const material = $("#rp-material", wurzel).value.split("\n").map((z) => z.trim()).filter(Boolean).map((z) => { const [menge, ...rest] = z.split(";"); return rest.length ? { menge: menge.trim(), bezeichnung: rest.join(";").trim() } : { menge: "1", bezeichnung: z }; });
      const messwerte = {}; for (const [id, k] of [["rp-abgas", "abgasverlust_prozent"], ["rp-co", "co_ppm"], ["rp-druck", "druck_bar"]]) { const v = $(`#${id}`, wurzel).value.trim(); if (v) messwerte[k] = v; }
      const r = await api(`/servicefaelle/${fallId}/rapport`, { methode: "POST", daten: {
        taetigkeiten: $("#rp-taetigkeiten", wurzel).value, arbeitszeit_minuten: Number($("#rp-arbeit", wurzel).value || 0), fahrzeit_minuten: Number($("#rp-fahrt", wurzel).value || 0),
        ergebnis: $("#rp-ergebnis", wurzel).value, material, hinweise_kunde: $("#rp-hinweise", wurzel).value || null, messwerte, termin_id: terminId || null,
        ressource_id: ressourceId || null, unterschrift: u.wert(), unterzeichnet_von: $("#rp-name", wurzel).value || null } });
      $("#rp-status", wurzel).innerHTML = `Rapport <strong>${esc(r.nummer)}</strong> gespeichert${fotos.length ? `, ${fotos.length} Foto(s) abgelegt` : ""}. <button class="leise" id="rp-pdf">PDF</button>`;
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
    ${wir.rechte.includes("finanzen") ? `<h3 style="margin-top:16px">Nachkalkulation</h3><div id="kalk">Wird geladen …</div>` : ""}`;
  wurzel.append(teil);
  teil.querySelectorAll("[data-rpdf]").forEach((b) => b.addEventListener("click", () => dateiLaden(`/rapporte/${b.dataset.rpdf}/pdf`, `${b.dataset.nr}.pdf`)));
  teil.querySelectorAll("[data-bild]").forEach(async (img) => { img.src = await bildUrl(img.dataset.bild); });
  teil.querySelector("[data-rapport-neu]")?.addEventListener("click", () => rapportFormular(id, null, null));
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
const BELEGSTATUS_TEXT = { erfasst: "Zu prüfen", geprueft: "Geprüft", uebergeben: "An Kanzlei übergeben", bezahlt: "Bezahlt" };
async function belegeAnsicht(ziel) {
  const [wir, liste] = await Promise.all([ich(), api("/belege")]);
  const darf = wir.rechte.includes("bearbeiten");
  const offen = liste.filter((b) => b.status === "erfasst").length;
  ziel.innerHTML = `<h1>Belege</h1><p class="unterzeile">Eingangsrechnungen und Lieferscheine: XRechnung und ZUGFeRD werden strukturiert gelesen, PDF und Fotos per Texterkennung. Geprüfte Belege gehen mit dem nächsten Belegpaket an die Kanzlei; die Kontierung bleibt dort.</p>
    <div class="kacheln"><div class="karte kachel"><div class="wert">${offen}</div><div class="bez">zu prüfen</div></div>
      <div class="karte kachel"><div class="wert">${liste.filter((b) => b.status === "geprueft").length}</div><div class="bez">bereit für Belegpaket</div></div>
      <div class="karte kachel"><div class="wert">${esc(EURO.format(liste.filter((b) => b.status !== "bezahlt" && b.status !== "uebergeben").reduce((a, b) => a + Number(b.brutto), 0)))}</div><div class="bez">offen (brutto)</div></div></div>
    <div class="karte" style="margin-bottom:16px"><h2>Beleg hochladen</h2><div class="zeile">
      <label>Datei (XRechnung-XML, ZUGFeRD/PDF, Foto) <input id="b-datei" type="file" accept="image/*,application/pdf,.xml,text/xml"></label>
      <label>Kategorie <select id="b-kategorie">${Object.entries(KATEGORIE_TEXT).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label>
      <button id="b-hochladen">Erkennen und erfassen</button></div>
      <details><summary>Ohne Datei von Hand erfassen</summary><div class="zeile" style="margin-top:8px"><label>Lieferant <input id="b-lieferant"></label><label>Nummer <input id="b-nummer"></label><label>Datum <input id="b-datum" type="date"></label><label>Brutto <input id="b-brutto" inputmode="decimal"></label><label>USt <select id="b-ust"><option>19</option><option>7</option><option>0</option></select></label><button id="b-manuell" class="leise">Erfassen</button></div></details>
      <p id="b-status"></p></div>
    <div class="karte"><div class="tabelle-rahmen"><table><thead><tr><th>Datum</th><th>Lieferant</th><th>Nummer</th><th style="text-align:right">Netto</th><th style="text-align:right">Brutto</th><th>Kategorie</th><th>Erkennung</th><th>Status</th></tr></thead><tbody>
      ${liste.map((b) => `<tr class="klickbar" data-beleg="${esc(b.id)}"><td>${esc(DATUM.format(new Date(b.datum)))}</td><td>${esc(b.lieferant)}</td><td>${esc(b.nummer || "–")}</td><td style="text-align:right">${esc(EURO.format(b.netto))}</td><td style="text-align:right">${esc(EURO.format(b.brutto))}</td>
        <td>${esc(KATEGORIE_TEXT[b.kategorie])}</td><td>${tag(b.quelle, "neutral")} ${b.erkennung.konfidenz != null && b.quelle !== "manuell" ? tag(`${Math.round(b.erkennung.konfidenz * 100)} %`, b.erkennung.konfidenz >= 0.75 ? "A" : "B") : ""}</td>
        <td>${tag(BELEGSTATUS_TEXT[b.status], b.status === "erfasst" ? "offen" : b.status === "bezahlt" ? "neutral" : "A")}</td></tr>`).join("") || `<tr><td colspan="8">Noch keine Belege.</td></tr>`}</tbody></table></div></div>`;
  const senden = async (fd) => {
    $("#b-status").textContent = "Wird verarbeitet …";
    try { const b = await api("/belege", { methode: "POST", daten: fd }); $("#b-status").textContent = `Erfasst: ${b.lieferant}, ${EURO.format(b.brutto)}.`; zeigen(); belegDetail(b.id); }
    catch (e) { $("#b-status").innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  };
  $("#b-hochladen").addEventListener("click", () => {
    const datei = $("#b-datei").files[0]; if (!datei) return alert("Datei wählen");
    const fd = new FormData(); fd.append("datei", datei); fd.append("kategorie", $("#b-kategorie").value); senden(fd);
  });
  $("#b-manuell").addEventListener("click", () => {
    const fd = new FormData(); for (const [id, feld] of [["b-lieferant", "lieferant"], ["b-nummer", "nummer"], ["b-datum", "datum"], ["b-brutto", "brutto"], ["b-ust", "ust_satz"]]) { const v = $(`#${id}`).value.trim(); if (v) fd.append(feld, v); }
    fd.append("kategorie", $("#b-kategorie").value); senden(fd);
  });
  ziel.querySelectorAll("[data-beleg]").forEach((z) => z.addEventListener("click", () => belegDetail(z.dataset.beleg, darf)));
}
async function belegDetail(id, darf = true) {
  const [b, faelle] = await Promise.all([api(`/belege/${id}`), api("/servicefaelle?limit=300")]);
  const fest = b.status === "uebergeben" || b.status === "bezahlt";
  const feld = (name, bez, typ = "text", wert = b[name]) => `<label>${bez} <input data-feld="${name}" type="${typ}" value="${esc(wert ?? "")}" ${fest ? "disabled" : ""}></label>`;
  const wurzel = detail(`<h2>${esc(b.lieferant)} ${b.nummer ? `· ${esc(b.nummer)}` : ""}</h2>
    <p>${tag(BELEGSTATUS_TEXT[b.status], b.status === "erfasst" ? "offen" : "A")} ${tag(b.quelle, "neutral")}${b.erkennung.hinweise?.length ? `<br><small>Hinweise der Erkennung: ${esc(b.erkennung.hinweise.join("; "))}</small>` : ""}${b.erkennung.kommission ? `<br><small>Kommission laut Beleg: ${esc(b.erkennung.kommission)}</small>` : ""}</p>
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
    <div class="aktionen">${b.anhang_id ? `<button class="leise" id="b-datei-laden">Original</button>` : ""}${darf && !fest ? `<button class="gefahr" id="b-loeschen">Löschen</button><button class="leise" id="b-speichern">Speichern</button><button id="b-pruefen">Geprüft, ins Belegpaket</button>` : ""}${darf && b.status === "uebergeben" ? `<button class="leise" id="b-bezahlt">Als bezahlt markieren</button>` : ""}</div></div></div>`);
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
  $("#b-bezahlt", wurzel)?.addEventListener("click", async () => { try { await api(`/belege/${id}`, { methode: "PATCH", daten: { status: "bezahlt" } }); $("#detail").close(); zeigen(); } catch (e) { alert(e.message); } });
  $("#b-loeschen", wurzel)?.addEventListener("click", async () => { if (!confirm("Beleg und Datei löschen?")) return; try { await api(`/belege/${id}`, { methode: "DELETE" }); $("#detail").close(); zeigen(); } catch (e) { alert(e.message); } });
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
      <label>Beginn <input id="z-beginn" type="datetime-local" value="${esc(lokalIso())}"></label><label>Ende <input id="z-ende" type="datetime-local" value="${esc(lokalIso())}"></label>
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
  ziel.querySelectorAll("[data-bl]").forEach((b) => b.addEventListener("click", async () => { try { await api(`/bereitschaft/${b.dataset.bl}`, { methode: "DELETE" }); zeigen(); } catch (e) { alert(e.message); } }));
  $("#fr-anlegen")?.addEventListener("click", async () => {
    const [typ, id] = $("#fr-bezug").value.split(":");
    try { await api("/fristen", { methode: "POST", daten: { art: $("#fr-art").value, bezeichnung: $("#fr-bez").value, faellig: $("#fr-faellig").value, intervall_monate: Number($("#fr-intervall").value) || null, bezug_typ: typ, bezug_id: id || null } }); zeigen(); }
    catch (e) { $("#fr-status").innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
  ziel.querySelectorAll("[data-fe]").forEach((b) => b.addEventListener("click", async () => { try { const r = await api(`/fristen/${b.dataset.fe}/erledigt`, { methode: "POST", daten: {} }); if (r.folgefrist) alert(`Folgefrist angelegt: fällig ${DATUM.format(new Date(r.folgefrist.faellig))}`); zeigen(); } catch (e) { alert(e.message); } }));
  ziel.querySelectorAll("[data-fl]").forEach((b) => b.addEventListener("click", async () => { try { await api(`/fristen/${b.dataset.fl}`, { methode: "DELETE" }); zeigen(); } catch (e) { alert(e.message); } }));
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
    const datei = $("#kd-datei", teil).files[0]; if (!datei) return alert("Datei wählen");
    const fd = new FormData(); fd.append("datei", datei); fd.append("bezug_typ", "kunde"); fd.append("bezug_id", id); fd.append("art", datei.type.startsWith("image/") ? "foto" : "dokument"); if ($("#kd-notiz", teil).value) fd.append("notiz", $("#kd-notiz", teil).value);
    try { await api("/anhaenge", { methode: "POST", daten: fd }); kundeDetail(id); } catch (e) { $("#kd-status", teil).innerHTML = `<span class="fehler">${esc(e.message)}</span>`; }
  });
}
