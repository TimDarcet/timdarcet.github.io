// Service worker: offline app shell + runtime tile cache. Bump CACHE to invalidate on deploy.
const CACHE = "paris-v10";
const CACHE_PREFIX = "paris-";
const SHELL = [
  "./",
  "./index.html",
  "./paris.js",
  "./plaques.js",
  "./sucettes.js",
  "./match.js",
  "./sync.js",
  "./icon.svg",
  "./manifest.webmanifest",
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
];

self.addEventListener("install", e => {
  // {cache:"reload"} bypasses the browser HTTP cache, so a deploy never bakes a stale copy into the new cache
  e.waitUntil(caches.open(CACHE).then(c =>
    Promise.all(SHELL.map(u => fetch(u, { cache: "reload" }).then(r => r.ok && c.put(u, r)).catch(() => {})))
  ).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(ks => Promise.all(ks.filter(k => k.startsWith(CACHE_PREFIX) && k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  const isTile = /basemaps\.cartocdn\.com/.test(url.host) || /fonts\.(googleapis|gstatic)\.com/.test(url.host);

  if (isTile) {
    // stale-while-revalidate for map tiles & fonts (cache grows as you explore; works offline after)
    e.respondWith(
      caches.open(CACHE).then(async c => {
        const hit = await c.match(req);
        const net = fetch(req).then(res => { if (res.ok) c.put(req, res.clone()); return res; }).catch(() => hit);
        return hit || net;
      })
    );
  } else {
    // network-first for the app shell so code/data updates land immediately; cache is the offline fallback.
    // (cache-first used to strand users on stale JS after a deploy — a bumped CACHE alone didn't help.)
    e.respondWith(
      fetch(req).then(res => {
        if (res.ok && url.origin === location.origin){ const cp = res.clone(); caches.open(CACHE).then(c => c.put(req, cp)); }
        return res;
      }).catch(() => caches.match(req))
    );
  }
});
