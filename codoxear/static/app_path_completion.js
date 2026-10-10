function hasPathPrefix(token, start) {
  return token.startsWith("./") || token.startsWith("../") || token.startsWith("~/")
    || (start > 0 && token.startsWith("/"));
}

function tokenEnd(value, caret) {
  let end = caret;
  while (end < value.length && !/[ \t\n\r]/.test(value[end])) end += 1;
  return end;
}

function pathTokenAt(value, caret) {
  let start = caret;
  while (start > 0 && !/[ \t\n\r]/.test(value[start - 1])) start -= 1;
  const end = tokenEnd(value, caret);
  const token = value.slice(start, end);
  return hasPathPrefix(token, start) ? { start, end, token } : null;
}

// This runtime owns the anchored completion session and network state only.
// The composer remains the sole writer of the shared picker DOM.
function createPathCompletionRuntime({ api, onChange, setTimeout: timerSet = (...args) => setTimeout(...args), clearTimeout: timerClear = (id) => clearTimeout(id), AbortController: AbortCtor = globalThis.AbortController, debounceMs = 120 }) {
  let anchor = null;
  let sessionId = null;
  let end = 0;
  let token = "";
  let entries = [];
  let dir = "";
  let loading = false;
  let truncated = false;
  let timer = null;
  let abort = null;
  let seq = 0;

  function snapshot() {
    return { open: anchor !== null, start: anchor, end, token, sessionId, entries: entries.slice(), dir, loading, truncated };
  }

  function cancelRequest() {
    seq += 1;
    if (timer !== null) timerClear(timer);
    timer = null;
    if (abort) abort.abort();
    abort = null;
  }

  function close() {
    cancelRequest();
    anchor = null;
    sessionId = null;
    end = 0;
    token = "";
    entries = [];
    dir = "";
    loading = false;
    truncated = false;
  }

  function schedule() {
    cancelRequest();
    // Keep the previous listing visible while the replacement fetch is in
    // flight: completions stay valid (they replace the whole token), and
    // blanking the menu on every keystroke would flash "No matches".
    loading = true;
    const requestSeq = seq;
    const requestSession = sessionId;
    const requestToken = token;
    timer = timerSet(async () => {
      timer = null;
      const controller = typeof AbortCtor === "function" ? new AbortCtor() : null;
      abort = controller;
      let result = null;
      try {
        result = await api(`/api/sessions/${encodeURIComponent(requestSession)}/file/complete?path=${encodeURIComponent(requestToken)}&limit=50`, { signal: controller ? controller.signal : undefined });
      } catch (_) {
        // Unavailable directories and network failures are an empty listing.
      }
      if (requestSeq !== seq || sessionId !== requestSession || anchor === null) return;
      abort = null;
      entries = result && result.ok === true && Array.isArray(result.entries) ? result.entries : [];
      dir = result && result.ok === true ? String(result.dir || "") : "";
      truncated = Boolean(result && result.ok === true && result.truncated);
      loading = false;
      onChange(snapshot());
    }, debounceMs);
  }

  function sync(value, caret, selectedSessionId) {
    if (anchor !== null) {
      const active = value.slice(anchor, caret);
      // A whitespace at the caret boundary ends the token: interior spaces
      // stay legal (anchored span across accepted names containing spaces),
      // but a trailing space can only come from the user finishing the path.
      if (!selectedSessionId || selectedSessionId !== sessionId || caret < anchor
          || !hasPathPrefix(active, anchor) || /[\n\r]/.test(active)
          || /\s$/.test(active)) {
        close();
        return null;
      }
      end = tokenEnd(value, caret);
      if (active !== token) {
        token = active;
        schedule();
      }
    } else {
      const span = selectedSessionId ? pathTokenAt(value, caret) : null;
      if (!span) return null;
      const active = value.slice(span.start, caret);
      if (!hasPathPrefix(active, span.start)) return null;
      anchor = span.start;
      sessionId = selectedSessionId;
      end = span.end;
      token = active;
      schedule();
    }
    return snapshot();
  }

  return Object.freeze({ sync, snapshot, close });
}

export { pathTokenAt, createPathCompletionRuntime };
