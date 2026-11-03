// Card Desk service worker: lets the app open and search with no signal.
// - App files: network first (so updates arrive), cached copy when offline.
// - Firebase bundle and icons: cache first.
// - Catalog shards (?v=build): cache first; one copy per file (old builds dropped);
//   offline falls back to any cached build of the same file.
// - manifest.json: network first, cached copy when offline.
const APP = "cd-app-v4", CAT = "cd-catalog";
const SHELL = ["./", "index.html", "vendor/firebase.js", "app.webmanifest", "icon.svg", "icon-192.png", "icon-512.png", "fonts/bricolage-500.woff2", "fonts/bricolage-700.woff2", "fonts/bricolage-800.woff2", "fonts/plex-400.woff2", "fonts/plex-500.woff2", "fonts/plex-600.woff2", "fonts/plex-700.woff2"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(APP).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== APP && k !== CAT).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

async function networkFirst(req, cacheName, key) {
  const c = await caches.open(cacheName);
  try {
    const res = await fetch(req);
    if (res.ok) c.put(key || req, res.clone());
    return res;
  } catch (e) {
    const hit = await c.match(key || req, { ignoreSearch: true });
    if (hit) return hit;
    throw e;
  }
}
async function cacheFirst(req, cacheName) {
  const c = await caches.open(cacheName);
  const hit = await c.match(req);
  if (hit) return hit;
  try {
    const res = await fetch(req);
    if (res.ok) {
      const path = new URL(req.url).pathname;
      for (const k of await c.keys()) if (new URL(k.url).pathname === path && k.url !== req.url) c.delete(k);
      c.put(req, res.clone());
    }
    return res;
  } catch (e) {
    const old = await c.match(req, { ignoreSearch: true });
    if (old) return old;
    throw e;
  }
}

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return; // Firebase traffic goes straight through
  const p = url.pathname;
  if (p.endsWith("/catalog/manifest.json")) return e.respondWith(networkFirst(req, CAT, new Request(url.origin + p)));
  if (p.includes("/catalog/")) return e.respondWith(cacheFirst(req, CAT));
  if (p.includes("/vendor/") || /\/icon[^/]*$/.test(p)) return e.respondWith(cacheFirst(req, APP));
  if (req.mode === "navigate") return e.respondWith(networkFirst(req, APP, new Request(new URL("index.html", self.registration.scope))));
  e.respondWith(networkFirst(req, APP));
});
