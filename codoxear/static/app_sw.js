let registrationPromise = null;

function ensureAppServiceWorker(resolveAppUrl, navigatorLike = typeof navigator === "undefined" ? undefined : navigator) {
  if (!navigatorLike || !navigatorLike.serviceWorker) return Promise.resolve(null);
  if (!registrationPromise) {
    registrationPromise = Promise.resolve().then(() => navigatorLike.serviceWorker.register(resolveAppUrl("/service-worker.js"), { scope: resolveAppUrl("/") })).catch((error) => {
      registrationPromise = null;
      throw error;
    });
  }
  return registrationPromise;
}

export { ensureAppServiceWorker };
