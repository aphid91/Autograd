// The service worker: what makes the app work offline once installed.
//
// Every file of the site is cached on install -- the app, types.json, the
// vendored Tweakpane, the icons and every bundled save -- and served from
// that cache first, so nothing needs the network once it has loaded once.
//
// build.sh fills in the two placeholders: the file list, and VERSION, a
// hash of every file's contents. A deploy that changes anything changes
// this file's bytes, so the browser installs the new worker, which caches
// the new set under a new name and deletes the old cache. It takes over at
// once (skipWaiting / claim); the page already open keeps the code it
// loaded, and the next launch runs the new one. Served unbuilt (no
// placeholders filled in), it caches nothing and stays out of the way.

const VERSION = "__VERSION__";
const FILES = __FILES__;
const CACHE = `gates-${VERSION}`;
const BUILT = !VERSION.startsWith("__");

self.addEventListener("install", (e) => {
  self.skipWaiting();
  if (!BUILT) return;
  // cache: "reload" so a stale HTTP cache can never seed the new version.
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(FILES.map(f => new Request(f, { cache: "reload" })))));
});

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k.startsWith("gates-") && k !== CACHE) await caches.delete(k);
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (!BUILT || req.method !== "GET" || new URL(req.url).origin !== self.location.origin) return;
  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    // A launch from the home screen asks for "./"; the cache has index.html.
    const hit = await cache.match(req, { ignoreSearch: true })
      || (req.mode === "navigate" ? await cache.match("index.html") : undefined);
    return hit || fetch(req);
  })());
});
