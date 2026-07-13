// Service worker: offline app shell + runtime tile cache. Bump CACHE to invalidate on deploy.
const CACHE = "paris-v1";
const SHELL = [
  "./",
  "./index.html",
  "./paris.js",
  "./icon.svg",
  "./manifest.webmanifest",
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
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
    // cache-first for the app shell, fall back to network then cache the result
    e.respondWith(
      caches.match(req).then(hit => hit || fetch(req).then(res => {
        if (res.ok && url.origin === location.origin) caches.open(CACHE).then(c => c.put(req, res.clone()));
        return res;
      }).catch(() => hit))
    );
  }
});
