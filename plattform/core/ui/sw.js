/* Service Worker der Büro-Oberfläche (PWA für „Mein Einsatz“): App-Hülle offline, API immer aus dem Netz.
   Hülle wird bei jeder neuen Version (Cache-Name) ersetzt; API-Antworten werden nie zwischengespeichert. */
const VERSION = "shk-ui-v11";
const HUELLE = ["./", "./index.html", "./app.css", "./app.js", "./lib/leaflet.js", "./lib/leaflet.css", "./lib/uplot.min.js", "./lib/uplot.min.css", "./manifest.webmanifest", "./icon.svg"];
self.addEventListener("install", (ev) => { ev.waitUntil(caches.open(VERSION).then((c) => c.addAll(HUELLE)).then(() => self.skipWaiting())); });
self.addEventListener("activate", (ev) => { ev.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== VERSION).map((k) => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener("fetch", (ev) => {
  const url = new URL(ev.request.url);
  if (ev.request.method !== "GET" || url.origin !== location.origin || !url.pathname.startsWith(new URL("./", location.href).pathname)) return;  // API und Fremdes: Netz
  ev.respondWith(fetch(ev.request).then((r) => { const kopie = r.clone(); caches.open(VERSION).then((c) => c.put(ev.request, kopie)); return r; })
    .catch(() => caches.match(ev.request).then((r) => r || caches.match("./index.html"))));
});
