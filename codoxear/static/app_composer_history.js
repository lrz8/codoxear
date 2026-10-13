function requireFunction(value, name) {
  if (typeof value !== "function") throw new TypeError(`composer history dependency missing: ${name}`);
  return value;
}

function requireTextarea(value) {
  if (!value || typeof value !== "object" || typeof value.addEventListener !== "function" || typeof value.dispatchEvent !== "function") {
    throw new TypeError("composer history dependency missing: textarea");
  }
  return value;
}

function createComposerHistoryController(options = {}) {
  if (!options || typeof options !== "object") throw new TypeError("composer history dependency missing: options");
  const textarea = requireTextarea(options.textarea);
  const sessionState = options.sessionState;
  if (!sessionState || typeof sessionState.get !== "function" || typeof sessionState.subscribe !== "function") {
    throw new TypeError("composer history dependency missing: sessionState");
  }
  const api = requireFunction(options.api, "api");
  const cleanups = [];
  const PAGE_LIMIT = 5;
  let active = false;
  let entries = [];
  let nextCursor = null;
  let fetched = false;
  let entryIndex = -1;
  let generation = 0;
  let advanceQueue = Promise.resolve();

  function reset() {
    active = false;
    entries = [];
    nextCursor = null;
    fetched = false;
    entryIndex = -1;
    generation += 1;
  }

  function listen(type, listener) {
    textarea.addEventListener(type, listener);
    cleanups.push(() => textarea.removeEventListener(type, listener));
  }

  function currentSessionId() {
    const value = sessionState.get("selected");
    return value ? String(value) : null;
  }

  function dispatchInput() {
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  }

  async function fetchNextPage(sessionId, expectedGeneration) {
    const cursor = nextCursor;
    const query = cursor == null
      ? `limit=${PAGE_LIMIT}`
      : `cursor=${encodeURIComponent(cursor)}&limit=${PAGE_LIMIT}`;
    const response = await api(`/api/sessions/${encodeURIComponent(sessionId)}/sent-history?${query}`);
    if (generation !== expectedGeneration || !active || currentSessionId() !== sessionId) return false;
    const rows = Array.isArray(response && response.entries) ? response.entries : [];
    entries.push(...rows.filter((entry) => entry && typeof entry.text === "string"));
    nextCursor = response && typeof response.next_cursor === "string" && response.next_cursor ? response.next_cursor : null;
    fetched = true;
    return rows.length > 0;
  }

  async function advance(sessionId, expectedGeneration) {
    const targetIndex = entryIndex + 1;
    while (entries.length <= targetIndex && (!fetched || nextCursor)) {
      const beforeCount = entries.length;
      const receivedRows = await fetchNextPage(sessionId, expectedGeneration);
      if (generation !== expectedGeneration || !active || currentSessionId() !== sessionId) return;
      if (!receivedRows || entries.length === beforeCount) break;
    }
    if (generation !== expectedGeneration || !active || currentSessionId() !== sessionId) return;
    const entry = entries[targetIndex];
    if (!entry) return;
    entryIndex = targetIndex;
    textarea.value = entry.text;
    dispatchInput();
  }

  function isModifierOnly(event) {
    return ["Shift", "Control", "Alt", "Meta"].includes(event.key);
  }

  function handleKeydown(event) {
    if (!event || event.isComposing) return false;
    if (event.key !== "ArrowUp") {
      if (active && !isModifierOnly(event)) reset();
      return false;
    }
    if (!active && String(textarea.value || "") !== "") return false;
    const sessionId = currentSessionId();
    if (!sessionId) return false;
    event.preventDefault();
    if (!active) {
      active = true;
      entries = [];
      nextCursor = null;
      fetched = false;
      entryIndex = -1;
    }
    const expectedGeneration = generation;
    advanceQueue = advanceQueue.then(() => advance(sessionId, expectedGeneration)).catch(() => {
      if (generation === expectedGeneration) reset();
    });
    return true;
  }

  listen("pointerdown", () => {
    if (active) reset();
  });
  listen("select", () => {
    if (active) reset();
  });
  const unsubscribeSelected = sessionState.subscribe("selected", reset);

  return Object.freeze({
    handleKeydown,
    reset,
    isActive: () => active,
    dispose() {
      unsubscribeSelected();
      while (cleanups.length) cleanups.pop()();
      reset();
    },
  });
}

export { createComposerHistoryController };
