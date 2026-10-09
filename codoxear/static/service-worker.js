const HTML_CACHE = "codoxear-html-v1";
const STATIC_CACHE = "codoxear-static-v1";
const API_CACHE = "codoxear-api-v1";
const SW_META_CACHE = "codoxear-sw-meta-v1";
const APP_CACHES = new Set([HTML_CACHE, STATIC_CACHE, API_CACHE, SW_META_CACHE]);

self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  const claim = clients.claim();
  const prune = caches.keys().then((names) => Promise.all(
    names.filter((name) => name.startsWith("codoxear-") && !APP_CACHES.has(name)).map((name) => caches.delete(name))
  ));
  event.waitUntil(Promise.all([claim, prune, warmActivationCaches().catch(warnWarmupFailure)]));
});

function warnWarmupFailure(error) {
  console.warn("Codoxear offline cache warm-up failed", error);
}

async function warmUrl(url, cacheName) {
  const cache = await caches.open(cacheName);
  return fetchAndCache(new Request(url, { credentials: "same-origin" }), cache, {
    pruneVersions: cacheName === STATIC_CACHE,
  });
}

async function warmShell() {
  const shellUrl = new URL("./", self.registration.scope);
  const response = await warmUrl(shellUrl.href, HTML_CACHE);
  if (response.status !== 200) return;
  const html = (await response.text()).replace(/&amp;/g, "&");
  const assets = new Set();
  const attributes = /\b(?:src|href)\s*=\s*(["'])([^"'<>]*[?&]v=[^"'<>]*)\1/gi;
  for (const match of html.matchAll(attributes)) {
    const url = new URL(match[2], self.registration.scope);
    if (url.origin === self.location.origin && url.searchParams.has("v")) assets.add(url.href);
  }
  // The theme link is built by document.write, not a literal HTML attribute.
  // All shell assets share the server's memoized asset version.
  const firstAsset = assets.values().next().value;
  if (firstAsset) {
    const version = new URL(firstAsset).searchParams.get("v");
    for (const family of ["paper", "clay", "slate"]) {
      const url = new URL(`themes/${family}.css`, self.registration.scope);
      url.searchParams.set("v", version);
      assets.add(url.href);
    }
  }
  await Promise.all([...assets].map((url) => warmUrl(url, STATIC_CACHE).catch(warnWarmupFailure)));
}

async function warmActivationCaches() {
  await Promise.all([
    warmShell().catch(warnWarmupFailure),
    ...["api/me", "api/sessions"].map((path) =>
      warmUrl(new URL(path, self.registration.scope).href, API_CACHE).catch(warnWarmupFailure)
    ),
  ]);
}

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

function recordNavigation(request, offline) {
  const markerKey = new URL("sw-meta/last-navigation", self.registration.scope);
  const marker = JSON.stringify({ ts: Date.now(), offline, url: request.url });
  return caches.open(SW_META_CACHE).then((cache) => cache.put(
    new Request(markerKey),
    new Response(marker, { headers: { "content-type": "application/json" } })
  ));
}

function startNavigationMarker(event, request) {
  let finish;
  event.waitUntil(new Promise((resolve) => {
    finish = (offline) => {
      try {
        resolve(recordNavigation(request, offline).catch(() => {}));
      } catch (_error) {
        resolve();
      }
    };
  }));
  return finish;
}

async function navigationNetworkFirst(request, finishMarker) {
  let offline = false;
  try {
    const cache = await caches.open(HTML_CACHE);
    try {
      return await fetchAndCache(request, cache);
    } catch (error) {
      offline = true;
      const cached = await cache.match(request, { ignoreSearch: true });
      if (cached) return cached;
      throw error;
    }
  } finally {
    // Diagnostics must never alter navigation delivery, including a failed fallback.
    finishMarker(offline);
  }
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
    const finishMarker = startNavigationMarker(event, request);
    event.respondWith(navigationNetworkFirst(request, finishMarker));
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
