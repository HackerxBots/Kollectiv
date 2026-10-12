/*
 * Service worker: makes the dashboard installable and its shell work offline.
 *
 * The rules that matter, in order of importance:
 *
 * 1. The API is never cached. Kollektiv has no telemetry and the dashboard is
 *    the operator's data: a response from /projects or /health on the API you
 *    pointed it at must always come from the network, never from a cache.
 *    Only the app's own shell (this directory) is stored.
 * 2. Navigations are network-first, so a newer deploy is picked up immediately,
 *    with the cached shell as the offline fallback.
 * 3. Shell assets are cache-first, because they are versioned by CACHE below:
 *    bump VERSION to publish new files.
 *
 * The worker is registered by assets/app.js from the page next to it, so it
 * works the same when the dashboard is served by Cloudflare Pages at /, by
 * GitHub Pages under a path, or by the API itself at /ui/.
 */

const VERSION = "v1";
const CACHE = `kollektiv-shell-${VERSION}`;

/* Everything the app needs to render without a network. */
const SHELL = [
  "./",
  "./index.html",
  "./assets/styles.css",
  "./assets/app.js",
  "./assets/favicon.svg",
  "./assets/manifest.webmanifest",
];

/* Absolute paths of the shell, resolved against the worker's own scope. */
const SCOPE = new URL(self.registration.scope);
const SHELL_PATHS = new Set(SHELL.map((path) => new URL(path, SCOPE).pathname));

/** Fill the cache with the shell. */
async function precache() {
  const cache = await caches.open(CACHE);
  await cache.addAll(SHELL);
}

/** Drop caches from previous versions. */
async function prune() {
  const names = await caches.keys();
  await Promise.all(names.filter((name) => name !== CACHE).map((name) => caches.delete(name)));
}

/** Cache-first for shell assets, with a network write-through on success. */
async function shellResponse(request) {
  const cache = await caches.open(CACHE);
  const hit = await cache.match(request, { ignoreSearch: false });
  if (hit) return hit;
  try {
    const response = await fetch(request);
    if (response && response.ok && response.type === "basic") await cache.put(request, response.clone());
    return response;
  } catch (error) {
    console.warn("Shell asset unavailable offline:", request.url, error);
    return Response.error();
  }
}

/** Network-first for pages, falling back to the cached shell when offline. */
async function pageResponse(request) {
  try {
    return await fetch(request);
  } catch (error) {
    console.warn("Serving the offline shell:", error);
    const cache = await caches.open(CACHE);
    return (await cache.match("./index.html")) || (await cache.match("./")) || Response.error();
  }
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      try {
        await precache();
      } catch (error) {
        console.warn("Could not pre-cache the shell:", error);
      }
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      try {
        await prune();
      } catch (error) {
        console.warn("Could not prune old caches:", error);
      }
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  let url;
  try {
    url = new URL(request.url);
  } catch (error) {
    return; // Not a URL we understand; let the browser handle it.
  }

  // Another origin — the API, an object-storage URL, anything. Never cached.
  if (url.origin !== SCOPE.origin) return;

  // This app's own shell files: safe to serve from the cache.
  if (SHELL_PATHS.has(url.pathname)) {
    event.respondWith(shellResponse(request));
    return;
  }

  // A page inside the app's scope: fresh when online, cached shell offline.
  const insideScope = url.pathname.startsWith(SCOPE.pathname);
  if (request.mode === "navigate" && insideScope) {
    event.respondWith(pageResponse(request));
  }

  // Everything else on this origin — the API when the dashboard is served at
  // /ui/, webhooks, file downloads — goes straight to the network.
});
