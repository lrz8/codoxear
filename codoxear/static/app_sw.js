let registrationPromise = null;

function ensureAppServiceWorker(navigatorLike = typeof navigator === "undefined" ? undefined : navigator) {
  if (!navigatorLike || !navigatorLike.serviceWorker) return Promise.resolve(null);
  if (!registrationPromise) {
    registrationPromise = Promise.resolve().then(() => navigatorLike.serviceWorker.register("/service-worker.js", { scope: "/" })).catch((error) => {
      registrationPromise = null;
      throw error;
    });
  }
  return registrationPromise;
}

export { ensureAppServiceWorker };
