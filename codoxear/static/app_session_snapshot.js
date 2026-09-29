/* Session snapshot restore row: element, projection, feed, and click handling.
 *
 * The row sits in the sidebar directly above the session list and is visible
 * only while the server-side session snapshot holds entries that are not
 * currently running. One click restores every restorable entry; the outcome
 * arrives as a single toast, and the list itself is the success evidence.
 *
 * Feed (documented reconciliation rule): the row's count is
 * `entries.filter((entry) => !entry.live).length` from `GET
 * /api/sessions/snapshot`. The feed has two triggers, both evaluated inside
 * the catalog subscription so an idle app never gains traffic:
 *
 * 1. Liveness key. The count usually changes when the set of live sessions
 *    changes (a broker dies, a restore launches, a session is deleted) — every
 *    one of which changes the session-list catalog — so a catalog generation
 *    whose liveness key (session ids plus their `lost` flag) differs from the
 *    last fetched one refetches immediately, plus once after each restore.
 * 2. Cadence safety net. The server's `live` annotation can also change
 *    without the key moving: a lost session's orphaned agent process still
 *    counts as live until it dies, and a Codex/Claude Code resume that binds
 *    its log late is annotated live only afterwards. So a catalog generation
 *    also refetches when more than `SNAPSHOT_REFETCH_INTERVAL_MS` has passed
 *    since the last answered fetch (a server answer, including a 404/5xx that
 *    commits the empty state; transport loss does not count, and a row that
 *    has never been answered is treated as stale).
 *
 * Busy/idle churn within the same session set and inside the cadence window
 * never triggers a fetch, so the feed coalesces with the existing session-list
 * poll instead of adding a loop.
 */

const SNAPSHOT_REFETCH_INTERVAL_MS = 60_000;

function requireFunction(value, name) {
  if (typeof value !== "function") throw new TypeError(`session snapshot dependency missing: ${name}`);
  return value;
}

function requireNode(value, name) {
  if (!value || typeof value !== "object" || !value.parentNode || typeof value.parentNode.insertBefore !== "function") {
    throw new TypeError(`session snapshot dependency missing: ${name}`);
  }
  return value;
}

function requireSessionCatalog(value) {
  if (!value || typeof value.get !== "function" || typeof value.subscribe !== "function") {
    throw new TypeError("session snapshot dependency missing: sessionCatalog");
  }
  return value;
}

function restorableEntries(entries) {
  return Array.isArray(entries) ? entries.filter((entry) => entry && typeof entry === "object" && !entry.live) : [];
}

function restoreLabel(count) {
  return `Restore ${count} saved session${count === 1 ? "" : "s"}`;
}

function counted(response, field) {
  const value = Number(response && response[field]);
  return Number.isFinite(value) && value >= 0 ? value : 0;
}

// One toast line per restore: "Restored 3 sessions", "Restored 2 · skipped 1
// (running)", with failures appended as " · failed 2: <reason> ; <reason>" —
// every failed entry's reason, in result order, joined with " ; ".
function restoreSummaryText(response) {
  const results = Array.isArray(response && response.results) ? response.results : [];
  const restored = counted(response, "restored");
  const skipped = counted(response, "skipped");
  const failed = counted(response, "failed");
  if (!skipped && !failed) return `Restored ${restored} session${restored === 1 ? "" : "s"}`;
  const parts = [`Restored ${restored}`];
  if (skipped) parts.push(`skipped ${skipped} (running)`);
  if (failed) {
    const reasons = results
      .filter((result) => result && result.outcome === "failed" && result.error)
      .map((result) => String(result.error).trim())
      .filter((reason) => reason);
    parts.push(reasons.length ? `failed ${failed}: ${reasons.join(" ; ")}` : `failed ${failed}`);
  }
  return parts.join(" · ");
}

function livenessKey(sessions) {
  return (Array.isArray(sessions) ? sessions : [])
    .map((session) => `${session && session.session_id}:${session && session.lost ? 1 : 0}`)
    .sort()
    .join("|");
}

function createSessionSnapshotController(options = {}) {
  if (!options || typeof options !== "object") throw new TypeError("session snapshot dependency missing: options");
  const el = requireFunction(options.el, "el");
  const iconSvg = requireFunction(options.iconSvg, "iconSvg");
  const api = requireFunction(options.api, "api");
  const setToast = requireFunction(options.setToast, "setToast");
  const handleAppAuthLoss = requireFunction(options.handleAppAuthLoss, "handleAppAuthLoss");
  const refreshSessions = requireFunction(options.refreshSessions, "refreshSessions");
  const isAppDisposed = requireFunction(options.isAppDisposed, "isAppDisposed");
  const sessionCatalog = requireSessionCatalog(options.sessionCatalog);
  const sessionsWrap = requireNode(options.sessionsWrap, "sessionsWrap");
  const now = requireFunction(options.now, "now");
  const eventBindings = options.eventBindings;
  if (!eventBindings || typeof eventBindings.on !== "function") {
    throw new TypeError("session snapshot dependency missing: eventBindings");
  }
  const consoleError = typeof options.consoleError === "function" ? options.consoleError : () => {};

  // The sessions controller owns every child of `sessionsWrap` (it clears the
  // container on each render and reads its child count for the empty hint),
  // so the row lives in its own lead host inserted just before the list.
  const label = el("span", { class: "sessionRestoreLabel", text: "" });
  const button = el("button", {
    class: "sessionRestore",
    id: "sessionRestoreBtn",
    type: "button",
    html: iconSvg("refresh"),
  }, [label]);
  const host = el("div", { class: "sessionRestoreLead", id: "sessionRestoreLead" }, [button]);
  host.hidden = true;
  sessionsWrap.parentNode.insertBefore(host, sessionsWrap);

  // Store: the only inputs the projection reads.
  const state = { entries: [], restoring: false };
  let fetchedKey = null;
  // Wall-clock ms of the last fetch the server answered; null until then.
  let answeredAt = null;
  let fetchInFlight = null;
  let fetchQueued = false;

  function render() {
    const count = restorableEntries(state.entries).length;
    const text = restoreLabel(count);
    host.hidden = count === 0;
    label.textContent = text;
    button.setAttribute("aria-label", text);
    button.disabled = state.restoring;
    button.setAttribute("aria-busy", state.restoring ? "true" : "false");
  }

  function commit(patch) {
    Object.assign(state, patch);
    render();
  }

  async function fetchSnapshotOnce() {
    let data;
    try {
      data = await api("/api/sessions/snapshot");
    } catch (error) {
      if (error && error.status === 401) {
        handleAppAuthLoss();
        return;
      }
      // A server answer without a snapshot (404/5xx) means no restorable
      // entries are known; a transport failure keeps the last known state.
      if (error && typeof error.status === "number") {
        answeredAt = now();
        commit({ entries: [] });
      }
      consoleError("session snapshot fetch failed", error);
      return;
    }
    if (isAppDisposed()) return;
    answeredAt = now();
    commit({ entries: Array.isArray(data && data.entries) ? data.entries.slice() : [] });
  }

  function refresh() {
    if (fetchInFlight) {
      fetchQueued = true;
      return fetchInFlight;
    }
    fetchInFlight = (async () => {
      try {
        do {
          fetchQueued = false;
          await fetchSnapshotOnce();
        } while (fetchQueued && !isAppDisposed());
      } finally {
        fetchInFlight = null;
      }
    })();
    return fetchInFlight;
  }

  function cadenceStale() {
    return answeredAt === null || now() - answeredAt > SNAPSHOT_REFETCH_INTERVAL_MS;
  }

  function onCatalogGeneration(sessions) {
    const key = livenessKey(sessions);
    if (key === fetchedKey && !cadenceStale()) return;
    fetchedKey = key;
    void refresh();
  }

  async function restoreAll() {
    if (state.restoring) return null;
    commit({ restoring: true });
    let response = null;
    try {
      response = await api("/api/sessions/snapshot/restore", { method: "POST" });
      if (isAppDisposed()) return response;
      setToast(restoreSummaryText(response));
    } catch (error) {
      if (error && error.status === 401) {
        handleAppAuthLoss();
        return null;
      }
      if (isAppDisposed()) return null;
      setToast(`restore error: ${error && error.message ? error.message : "unknown error"}`);
    } finally {
      if (!isAppDisposed()) {
        try {
          await refreshSessions();
        } catch (error) {
          consoleError("refreshSessions failed after restore", error);
        }
        // Restore consumes entries server-side; refetch even when the session
        // list is unchanged (every entry failed) so the row reflects it.
        fetchedKey = livenessKey(sessionCatalog.get("latestSessions"));
        await refresh();
        commit({ restoring: false });
      }
    }
    return response;
  }

  const unsubscribe = sessionCatalog.subscribe("latestSessions", onCatalogGeneration);
  render();

  eventBindings.on(button, "click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    void restoreAll();
  });

  function dispose() {
    unsubscribe();
    if (host.parentNode && typeof host.parentNode.removeChild === "function") host.parentNode.removeChild(host);
  }

  return Object.freeze({
    elements: Object.freeze({ host, button, label }),
    refresh,
    restoreAll,
    restorableCount: () => restorableEntries(state.entries).length,
    dispose,
  });
}

export {
  createSessionSnapshotController,
  restoreSummaryText,
  restoreLabel,
  restorableEntries,
  livenessKey,
  SNAPSHOT_REFETCH_INTERVAL_MS,
};
