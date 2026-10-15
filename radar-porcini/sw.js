/* Radar Porcini: lavoro senza rete. La pagina e i dati si prendono prima dalla rete (sempre i più recenti) e, senza
   campo, dalla copia salvata; le tessere della mappa topografica restano salvate dopo la prima volta. */
var VERSIONE = "rp-03f3555692", DATI = "rp-dati", TESSERE = "rp-tessere", FONT = "rp-font";
var GUSCIO = ["./", "./index.html", "./manifest.webmanifest", "./icona-192.png", "./icona-512.png", "./icona-maskable-512.png"];

self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(VERSIONE).then(function (c) { return c.addAll(GUSCIO); }).then(function () { return self.skipWaiting(); }));
});
self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (ks) {
    return Promise.all(ks.filter(function (k) { return k !== VERSIONE && k !== DATI && k !== TESSERE && k !== FONT; }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

function primaRete(req, nome) {
  return fetch(req).then(function (r) {
    if (r && r.ok) { var copia = r.clone(); caches.open(nome).then(function (c) { c.put(req, copia); }); }
    return r;
  }).catch(function (err) {
    return caches.match(req, { ignoreSearch: true }).then(function (m) { if (m) return m; throw err; });
  });
}
function primaCache(req, nome) {
  return caches.match(req).then(function (m) {
    if (m) return m;
    return fetch(req).then(function (r) {
      if (r && (r.ok || r.type === "opaque")) { var copia = r.clone(); caches.open(nome).then(function (c) { c.put(req, copia); }); }
      return r;
    });
  });
}
function tessera(req) {
  return caches.open(TESSERE).then(function (c) {
    return c.match(req).then(function (m) {
      if (m) return m;
      return fetch(req).then(function (r) {
        if (r && (r.ok || r.type === "opaque")) {
          c.put(req, r.clone()).then(function () {
            return c.keys().then(function (k) { if (k.length > 2000) return Promise.all(k.slice(0, k.length - 2000).map(function (x) { return c.delete(x); })); });
          });
        }
        return r;
      });
    });
  });
}
self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;
  var u = new URL(req.url);
  if (/\.tile\.opentopomap\.org$/.test(u.hostname)) { e.respondWith(tessera(req)); return; }
  if (u.hostname === "fonts.googleapis.com" || u.hostname === "fonts.gstatic.com") { e.respondWith(primaCache(req, FONT)); return; }
  if (u.origin !== self.location.origin) return;
  if (/\.json$/.test(u.pathname)) { e.respondWith(primaRete(req, DATI)); return; }
  e.respondWith(primaRete(req, VERSIONE));
});
