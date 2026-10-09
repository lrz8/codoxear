const HTML_CACHE = "codoxear-html-v1";
const STATIC_CACHE = "codoxear-static-v1";
const API_CACHE = "codoxear-api-v1";
const APP_CACHES = new Set([HTML_CACHE, STATIC_CACHE, API_CACHE]);

self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter((name) => name.startsWith("codoxear-") && !APP_CACHES.has(name)).map((name) => caches.delete(name)));
    await clients.claim();
  })());
});

async function fetchAndCache(request, cache, { pruneVersions = false } = {}) {
  const response = await fetch(request);
  if (response.status === 200) {
    await cache.put(request, response.clone());
    if (pruneVersions) {
      const url = new URL(request.url);
      const keys = await cache.keys();
      await Promise.all(keys.filter((key) => {
        const oldUrl = new URL(key.url);
        return oldUrl.pathname === url.pathname && oldUrl.searchParams.has("v") && oldUrl.searchParams.get("v") !== url.searchParams.get("v");
      }).map((key) => cache.delete(key)));
    }
  }
  return response;
}

async function networkFirst(request, cacheName, matchOptions) {
  const cache = await caches.open(cacheName);
  try {
    return await fetchAndCache(request, cache);
  } catch (error) {
    const cached = await cache.match(request, matchOptions);
    if (cached) return cached;
    throw error;
  }
}

async function versionedStatic(request) {
  const cache = await caches.open(STATIC_CACHE);
  const cached = await cache.match(request);
  return cached || fetchAndCache(request, cache, { pruneVersions: true });
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  const scopePath = new URL(self.registration.scope).pathname;
  if (request.method !== "GET" || url.origin !== self.location.origin || !url.pathname.startsWith(scopePath)) return;
  const pathname = `/${url.pathname.slice(scopePath.length)}`;
  // This is the SSE stream, not the JSON /messages/live polling route.
  if (/^\/api\/sessions\/[^/]+\/live$/.test(pathname)) return;
  if (request.mode === "navigate") {
    event.respondWith(networkFirst(request, HTML_CACHE, { ignoreSearch: true }));
  } else if (pathname.startsWith("/api/")) {
    event.respondWith(networkFirst(request, API_CACHE));
  } else if (url.searchParams.has("v")) {
    event.respondWith(versionedStatic(request));
  } else {
    const cachePromise = caches.open(STATIC_CACHE);
    const revalidation = cachePromise.then((cache) => fetchAndCache(request, cache));
    // Offline revalidation may fail while the cached response remains usable.
    event.waitUntil(revalidation.catch(() => {}));
    event.respondWith(cachePromise.then(async (cache) => (await cache.match(request)) || revalidation));
  }
});

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_error) {
    payload = {};
  }
  const sessionName = typeof payload.session_display_name === "string" && payload.session_display_name ? payload.session_display_name : "Codoxear";
  const body = typeof payload.notification_text === "string" && payload.notification_text ? payload.notification_text : "New final response";
  const sessionId = typeof payload.session_id === "string" ? payload.session_id : "";
  const targetUrl = new URL("./", self.registration.scope);
  if (sessionId) {
    targetUrl.hash = `session=${encodeURIComponent(sessionId)}`;
  }
  event.waitUntil(
    self.registration.showNotification(sessionName, {
      body,
      tag: sessionId || "codoxear-final-response",
      data: { url: targetUrl.toString() },
      renotify: true,
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const rawUrl = event.notification && event.notification.data ? event.notification.data.url : "";
  const target = typeof rawUrl === "string" && rawUrl ? rawUrl : new URL("./", self.registration.scope).toString();
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then(async (windows) => {
      for (const client of windows) {
        if ("focus" in client) {
          if ("navigate" in client) {
            const navigated = await client.navigate(target);
            return (navigated || client).focus();
          }
          return client.focus();
        }
      }
      return clients.openWindow(target);
    })
  );
});
