"""Behavioral coverage for the session snapshot restore row (app_session_snapshot.js).

The controller is executed inside Node's vm against a small fake DOM and a
scripted API. Assertions cover the rendered row (count, singular/plural,
visibility), the catalog-driven feed (fetch when the live session set changes,
plus the slow-cadence safety net evaluated on catalog generations only),
one-click restore (single POST, re-entry guard, disabled while in
flight, list refresh, snapshot refetch), toast composition per outcome mix, and
hint-mode discovery of the row as an activatable control.
"""
from __future__ import annotations

import json
import os
import subprocess
import textwrap

from frontend_module_loader import module_path


APP_SESSION_CATALOG_JS = module_path("app_session_catalog.js")
APP_SESSION_SNAPSHOT_JS = module_path("app_session_snapshot.js")
APP_HINT_MODE_JS = module_path("app_hint_mode.js")


FAKE_DOM = """
class Node {
  constructor(tag, attrs = {}) {
    this.tagName = String(tag).toUpperCase();
    this.attrs = {};
    this.children = [];
    this.parentNode = null;
    this.style = { display: "block", visibility: "visible" };
    this.disabled = false;
    this.hidden = false;
    this.className = "";
    this.textContent = "";
    this.innerHTML = "";
    for (const [name, value] of Object.entries(attrs)) {
      if (name === "class") this.className = String(value);
      else if (name === "text") this.textContent = String(value);
      else if (name === "html") this.innerHTML = String(value);
      else this.setAttribute(name, value);
    }
  }
  get offsetParent() {
    let node = this;
    while (node) {
      if (node.hidden || node.style.display === "none") return null;
      node = node.parentNode;
    }
    return {};
  }
  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  append(...children) { children.forEach((child) => this.appendChild(child)); }
  insertBefore(child, reference) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    const index = this.children.indexOf(reference);
    if (index < 0) this.children.push(child);
    else this.children.splice(index, 0, child);
    return child;
  }
  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) this.children.splice(index, 1);
    child.parentNode = null;
    return child;
  }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return Object.hasOwn(this.attrs, name) ? this.attrs[name] : null; }
  hasAttribute(name) { return Object.hasOwn(this.attrs, name); }
  getBoundingClientRect() { return { left: 0, top: 0, right: 200, bottom: 32, width: 200, height: 32 }; }
  contains(target) { return target === this || this.children.some((child) => child.contains(target)); }
  click() {
    if (typeof this.onclick === "function") this.onclick({ preventDefault() {}, stopPropagation() {} });
  }
  querySelectorAll(selector) { return query(this, selector); }
}
function descendants(root) {
  const nodes = [];
  for (const child of root.children) nodes.push(child, ...descendants(child));
  return nodes;
}
function matches(node, selector) {
  if (selector === "[data-hint]") return node.hasAttribute("data-hint");
  if (selector === "#sessions .session[data-session-id]") return false;
  if (selector.startsWith(".chat a")) return false;
  return ["BUTTON", "INPUT", "TEXTAREA", "SELECT", "A"].includes(node.tagName)
    && (selector === node.tagName.toLowerCase() || (selector === "a[href]" && node.hasAttribute("href")));
}
function query(root, selector) {
  if (selector.includes(",")) return descendants(root).filter((node) => selector.split(",").some((part) => matches(node, part.trim())));
  return descendants(root).filter((node) => matches(node, selector));
}
const el = (tag, attrs = {}, children = []) => {
  const node = new Node(tag, attrs);
  children.forEach((child) => node.appendChild(child));
  return node;
};
const eventBindings = { on: (target, type, handler) => { target[`on${type}`] = handler; } };
const flush = () => new Promise((resolve) => setImmediate(resolve));
"""


def run_node_json(script: str) -> dict:
    result = subprocess.run(
        ["node", "-e", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "TZ": "UTC"},
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def harness(body: str, *, extra_modules: tuple = ()) -> dict:
    modules = [APP_SESSION_CATALOG_JS, APP_SESSION_SNAPSHOT_JS, *extra_modules]
    loads = "\n".join(f"vm.runInContext({json.dumps(path.read_text(encoding='utf-8'))}, ctx);" for path in modules)
    script = textwrap.dedent(
        f"""
        const vm = require("vm");
        const ctx = {{ window: {{}}, console }};
        vm.createContext(ctx);
        {loads}
        {FAKE_DOM}
        const sidebar = el("div", {{ class: "sidebar" }});
        const sessionsWrap = el("div", {{ class: "sessions", id: "sessions" }});
        sidebar.append(el("header"), sessionsWrap, el("footer"));
        const sessionCatalog = ctx.window.CodoxearSessionCatalog.createSessionCatalog({{ consoleError: () => {{}} }});
        const calls = [];
        const toasts = [];
        const errors = [];
        let refreshes = 0;
        let disposed = false;
        let snapshot = {{ saved_at: null, entries: [] }};
        let restoreResponse = {{ results: [], restored: 0, skipped: 0, failed: 0 }};
        let restoreGate = null;
        let getFailure = null;
        let clock = 1_000_000;
        const now = () => clock;
        const api = async (path, options = {{}}) => {{
          const method = options.method || "GET";
          calls.push({{ path, method, body: options.body === undefined ? null : options.body }});
          if (method === "GET" && path === "/api/sessions/snapshot") {{
            if (getFailure) throw getFailure;
            return JSON.parse(JSON.stringify(snapshot));
          }}
          if (method === "POST" && path === "/api/sessions/snapshot/restore") {{
            if (restoreGate) await restoreGate;
            if (restoreResponse instanceof Error) throw restoreResponse;
            return JSON.parse(JSON.stringify(restoreResponse));
          }}
          throw new Error(`unexpected api call ${{method}} ${{path}}`);
        }};
        const controller = ctx.window.CodoxearSessionSnapshot.createSessionSnapshotController({{
          el,
          iconSvg: (name) => `<svg class="icon" data-icon="${{name}}"></svg>`,
          api,
          setToast: (text) => toasts.push(text),
          handleAppAuthLoss: () => {{ errors.push("auth-loss"); }},
          refreshSessions: async () => {{ refreshes += 1; }},
          isAppDisposed: () => disposed,
          sessionCatalog,
          sessionsWrap,
          eventBindings,
          now,
          consoleError: (message) => errors.push(String(message)),
        }});
        const {{ host, button, label }} = controller.elements;
        const snap = () => ({{
          hidden: host.hidden,
          text: label.textContent,
          ariaLabel: button.getAttribute("aria-label"),
          disabled: button.disabled,
          busy: button.getAttribute("aria-busy"),
        }});
        const gets = () => calls.filter((call) => call.method === "GET").length;
        const posts = () => calls.filter((call) => call.method === "POST").length;
        (async () => {{
          {body}
        }})().catch((error) => {{ console.error(error); process.exit(1); }});
        """
    )
    return run_node_json(script)


def test_row_mounts_before_the_session_list_and_projects_the_snapshot_count() -> None:
    result = harness(
        """
        const mounted = {
          order: sidebar.children.map((node) => node.className),
          buttonTag: button.tagName,
          buttonType: button.getAttribute("type"),
          icon: button.innerHTML.includes('data-icon="refresh"'),
          initial: snap(),
        };
        snapshot = { saved_at: 1, entries: [
          { session_id: "a", live: false }, { session_id: "b", live: true }, { session_id: "c", live: false }, { session_id: "d", live: false },
        ] };
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "b" }] });
        await flush();
        const plural = snap();
        snapshot = { saved_at: 2, entries: [{ session_id: "a", live: false }, { session_id: "b", live: true }] };
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "b" }, { session_id: "e" }] });
        await flush();
        const singular = snap();
        snapshot = { saved_at: 3, entries: [{ session_id: "a", live: true }, { session_id: "b", live: true }] };
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "a" }, { session_id: "b" }] });
        await flush();
        const allLive = snap();
        snapshot = { saved_at: null, entries: [] };
        sessionCatalog.applySnapshot({ latestSessions: [] });
        await flush();
        const empty = snap();
        process.stdout.write(JSON.stringify({ mounted, plural, singular, allLive, empty, gets: gets(), count: controller.restorableCount() }));
        """
    )
    assert result == {
        "mounted": {
            "order": ["", "sessionRestoreLead", "sessions", ""],
            "buttonTag": "BUTTON",
            "buttonType": "button",
            "icon": True,
            "initial": {"hidden": True, "text": "Restore 0 saved sessions", "ariaLabel": "Restore 0 saved sessions", "disabled": False, "busy": "false"},
        },
        "plural": {"hidden": False, "text": "Restore 3 saved sessions", "ariaLabel": "Restore 3 saved sessions", "disabled": False, "busy": "false"},
        "singular": {"hidden": False, "text": "Restore 1 saved session", "ariaLabel": "Restore 1 saved session", "disabled": False, "busy": "false"},
        "allLive": {"hidden": True, "text": "Restore 0 saved sessions", "ariaLabel": "Restore 0 saved sessions", "disabled": False, "busy": "false"},
        "empty": {"hidden": True, "text": "Restore 0 saved sessions", "ariaLabel": "Restore 0 saved sessions", "disabled": False, "busy": "false"},
        "gets": 4,
        "count": 0,
    }


def test_feed_refetches_only_when_the_live_session_set_changes() -> None:
    """Busy/idle churn republishes the catalog every poll; the snapshot GET must not follow it."""
    result = harness(
        """
        snapshot = { saved_at: 1, entries: [{ session_id: "gone", live: false }] };
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "a", busy: false }] });
        await flush();
        const afterFirst = gets();
        // Same session set, different runtime fields: no fetch.
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "a", busy: true, updated_ts: 5 }] });
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "a", busy: false, updated_ts: 6 }] });
        sessionCatalog.patchSession("a", { queue_len: 3 });
        await flush();
        const afterChurn = gets();
        // A session becoming lost is a liveness change: fetch.
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "a", lost: true }] });
        await flush();
        const afterLost = gets();
        // Session removed: fetch again.
        sessionCatalog.applySnapshot({ latestSessions: [] });
        await flush();
        const afterRemoval = gets();
        const visible = !host.hidden;
        // Two liveness changes while a GET is in flight coalesce into one follow-up fetch.
        let releaseGet;
        const gate = new Promise((resolve) => { releaseGet = resolve; });
        controller.dispose();
        let gated = true;
        const slowSnapshot = { saved_at: 2, entries: [{ session_id: "gone", live: false }, { session_id: "gone2", live: false }] };
        // Replace the GET path with a gated variant for this window only.
        const controller2 = ctx.window.CodoxearSessionSnapshot.createSessionSnapshotController({
          el, iconSvg: () => "", setToast: () => {}, handleAppAuthLoss: () => {}, refreshSessions: async () => {},
          isAppDisposed: () => false, sessionCatalog, sessionsWrap, eventBindings, now, consoleError: () => {},
          api: async (path) => { calls.push({ path, method: "GET", gated: true }); if (gated) await gate; return JSON.parse(JSON.stringify(slowSnapshot)); },
        });
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "p" }] });
        await flush();
        const gatedGetsBefore = calls.filter((call) => call.gated).length;
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "p" }, { session_id: "q" }] });
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "q" }] });
        await flush();
        gated = false;
        releaseGet();
        await flush();
        await flush();
        const gatedGetsAfter = calls.filter((call) => call.gated).length;
        const coalesced = { extraFetches: gatedGetsAfter - gatedGetsBefore, text: controller2.elements.label.textContent };
        process.stdout.write(JSON.stringify({ afterFirst, afterChurn, afterLost, afterRemoval, visible, coalesced }));
        """
    )
    assert result == {
        "afterFirst": 1, "afterChurn": 1, "afterLost": 2, "afterRemoval": 3, "visible": True,
        "coalesced": {"extraFetches": 1, "text": "Restore 2 saved sessions"},
    }


def test_feed_cadence_refetches_an_unchanged_live_set_only_after_the_window_on_a_catalog_generation() -> None:
    """The server's `live` annotation can move without the liveness key (an orphaned
    agent process dies later; a late log bind), so a catalog generation older than
    the window since the last answered fetch refetches. Time alone never fetches."""
    result = harness(
        """
        const interval = ctx.window.CodoxearSessionSnapshot.SNAPSHOT_REFETCH_INTERVAL_MS;
        const sessions = [{ session_id: "a", lost: true }];
        snapshot = { saved_at: 1, entries: [{ session_id: "a", live: true }] };
        sessionCatalog.applySnapshot({ latestSessions: sessions });
        await flush();
        const afterFirst = { gets: gets(), hidden: host.hidden };
        // The orphan dies: the server now annotates the entry restorable, but the key is unchanged.
        snapshot = { saved_at: 1, entries: [{ session_id: "a", live: false }] };
        // Time passes with no catalog generation: nothing fetches.
        clock += interval * 3;
        await flush();
        const afterSilentWait = { gets: gets(), hidden: host.hidden };
        // Reset the clock reference by forcing a key-triggered fetch, then probe the window edges.
        sessionCatalog.applySnapshot({ latestSessions: [...sessions, { session_id: "b" }] });
        await flush();
        sessionCatalog.applySnapshot({ latestSessions: sessions });
        await flush();
        const baseline = gets();
        clock += interval - 1;
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "a", lost: true, busy: true }] });
        await flush();
        const beforeWindow = gets() - baseline;
        clock += 1;
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "a", lost: true, busy: false }] });
        await flush();
        const atWindow = gets() - baseline;
        clock += 1;
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "a", lost: true, busy: true }] });
        await flush();
        const pastWindow = { gets: gets() - baseline, hidden: host.hidden, text: label.textContent };
        // The answered fetch restarts the window: the next generation is inside it again.
        clock += interval;
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "a", lost: true, busy: false }] });
        await flush();
        const insideNewWindow = gets() - baseline;
        // Transport loss does not count as an answer: the following generation retries.
        clock += 1;
        getFailure = new TypeError("Failed to fetch");
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "a", lost: true, busy: true }] });
        await flush();
        const lostTransport = gets() - baseline;
        getFailure = null;
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "a", lost: true, busy: false }] });
        await flush();
        const retriedAfterLoss = gets() - baseline;
        // An answered fetch closes the window even when that answer is 404: no per-poll storm.
        clock += interval + 1;
        getFailure = Object.assign(new Error("not found"), { status: 404 });
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "a", lost: true, busy: true }] });
        await flush();
        const serverAnswered = gets() - baseline;
        getFailure = null;
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "a", lost: true, busy: false }] });
        await flush();
        const afterServerAnswer = gets() - baseline;
        process.stdout.write(JSON.stringify({
          afterFirst, afterSilentWait, beforeWindow, atWindow, pastWindow, insideNewWindow,
          lostTransport, retriedAfterLoss, serverAnswered, afterServerAnswer,
        }));
        """
    )
    assert result == {
        "afterFirst": {"gets": 1, "hidden": True},
        "afterSilentWait": {"gets": 1, "hidden": True},
        "beforeWindow": 0,
        "atWindow": 0,
        "pastWindow": {"gets": 1, "hidden": False, "text": "Restore 1 saved session"},
        "insideNewWindow": 1,
        "lostTransport": 2,
        "retriedAfterLoss": 3,
        "serverAnswered": 4,
        "afterServerAnswer": 4,
    }


def test_click_restores_once_guards_reentry_and_drains_the_row() -> None:
    result = harness(
        """
        snapshot = { saved_at: 1, entries: [{ session_id: "a", live: false }, { session_id: "b", live: false }, { session_id: "c", live: true }] };
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "c" }] });
        await flush();
        const before = snap();
        let release;
        restoreGate = new Promise((resolve) => { release = resolve; });
        restoreResponse = {
          results: [
            { session_id: "a", outcome: "launched", new_session_id: "a2" },
            { session_id: "b", outcome: "launched", new_session_id: "b2" },
            { session_id: "c", outcome: "skipped_live" },
          ],
          restored: 2, skipped: 1, failed: 0,
        };
        button.click();
        await flush();
        const inFlight = snap();
        button.click();
        button.click();
        await flush();
        const postsWhileInFlight = posts();
        // The restore consumed both entries server-side; the next GET shows only the live one.
        snapshot = { saved_at: 2, entries: [{ session_id: "a2", live: true }, { session_id: "b2", live: true }, { session_id: "c", live: true }] };
        release();
        await flush();
        await flush();
        await flush();
        const after = snap();
        process.stdout.write(JSON.stringify({
          before, inFlight, postsWhileInFlight, after,
          posts: posts(), gets: gets(), refreshes, toasts,
          postBody: calls.find((call) => call.method === "POST").body,
        }));
        """
    )
    assert result == {
        "before": {"hidden": False, "text": "Restore 2 saved sessions", "ariaLabel": "Restore 2 saved sessions", "disabled": False, "busy": "false"},
        "inFlight": {"hidden": False, "text": "Restore 2 saved sessions", "ariaLabel": "Restore 2 saved sessions", "disabled": True, "busy": "true"},
        "postsWhileInFlight": 1,
        "after": {"hidden": True, "text": "Restore 0 saved sessions", "ariaLabel": "Restore 0 saved sessions", "disabled": False, "busy": "false"},
        "posts": 1,
        "gets": 2,
        "refreshes": 1,
        "toasts": ["Restored 2 · skipped 1 (running)"],
        "postBody": None,
    }


def test_restore_http_error_toasts_and_keeps_the_row() -> None:
    result = harness(
        """
        snapshot = { saved_at: 1, entries: [{ session_id: "a", live: false }] };
        sessionCatalog.applySnapshot({ latestSessions: [] });
        await flush();
        restoreResponse = Object.assign(new Error("snapshot missing"), { status: 409 });
        button.click();
        await flush();
        await flush();
        await flush();
        const afterError = snap();
        restoreResponse = Object.assign(new Error("unauthorized"), { status: 401 });
        button.click();
        await flush();
        await flush();
        process.stdout.write(JSON.stringify({ afterError, toasts, errors, posts: posts(), refreshes }));
        """
    )
    assert result == {
        "afterError": {"hidden": False, "text": "Restore 1 saved session", "ariaLabel": "Restore 1 saved session", "disabled": False, "busy": "false"},
        "toasts": ["restore error: snapshot missing"],
        "errors": ["auth-loss"],
        "posts": 2,
        "refreshes": 2,
    }


def test_snapshot_fetch_failures_distinguish_server_answers_from_transport_loss() -> None:
    result = harness(
        """
        snapshot = { saved_at: 1, entries: [{ session_id: "a", live: false }] };
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "x" }] });
        await flush();
        const shown = snap();
        getFailure = new TypeError("Failed to fetch");
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "x" }, { session_id: "y" }] });
        await flush();
        const afterTransportLoss = snap();
        getFailure = Object.assign(new Error("not found"), { status: 404 });
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "y" }] });
        await flush();
        const afterServerAnswer = snap();
        getFailure = Object.assign(new Error("unauthorized"), { status: 401 });
        sessionCatalog.applySnapshot({ latestSessions: [] });
        await flush();
        process.stdout.write(JSON.stringify({ shown: !shown.hidden, afterTransportLoss: !afterTransportLoss.hidden, afterServerAnswer: !afterServerAnswer.hidden, errors, toasts }));
        """
    )
    assert result == {
        "shown": True,
        "afterTransportLoss": True,
        "afterServerAnswer": False,
        "errors": ["session snapshot fetch failed", "session snapshot fetch failed", "auth-loss"],
        "toasts": [],
    }


def test_dispose_unsubscribes_and_removes_the_row() -> None:
    result = harness(
        """
        snapshot = { saved_at: 1, entries: [{ session_id: "a", live: false }] };
        sessionCatalog.applySnapshot({ latestSessions: [] });
        await flush();
        controller.dispose();
        sessionCatalog.applySnapshot({ latestSessions: [{ session_id: "z" }] });
        await flush();
        process.stdout.write(JSON.stringify({ gets: gets(), mounted: sidebar.children.map((node) => node.className) }));
        """
    )
    assert result == {"gets": 1, "mounted": ["", "sessions", ""]}


def test_restore_summary_text_per_outcome_mix() -> None:
    script = textwrap.dedent(
        f"""
        const vm = require("vm");
        const ctx = {{ window: {{}}, console }};
        vm.createContext(ctx);
        vm.runInContext({json.dumps(APP_SESSION_SNAPSHOT_JS.read_text(encoding="utf-8"))}, ctx);
        const summary = ctx.window.CodoxearSessionSnapshot.restoreSummaryText;
        process.stdout.write(JSON.stringify([
          summary({{ results: [], restored: 3, skipped: 0, failed: 0 }}),
          summary({{ results: [], restored: 1, skipped: 0, failed: 0 }}),
          summary({{ results: [], restored: 0, skipped: 0, failed: 0 }}),
          summary({{ results: [], restored: 2, skipped: 1, failed: 0 }}),
          summary({{ results: [
            {{ session_id: "a", outcome: "launched" }},
            {{ session_id: "b", outcome: "failed", error: "tmux unavailable" }},
            {{ session_id: "c", outcome: "failed", error: "log missing" }},
          ], restored: 1, skipped: 0, failed: 2 }}),
          summary({{ results: [
            {{ session_id: "b", outcome: "skipped_live" }},
            {{ session_id: "c", outcome: "failed", error: "  log missing " }},
          ], restored: 2, skipped: 1, failed: 1 }}),
          summary({{ results: [{{ session_id: "c", outcome: "failed" }}], restored: 0, skipped: 0, failed: 1 }}),
          summary({{}}),
        ]));
        """
    )
    assert run_node_json(script) == [
        "Restored 3 sessions",
        "Restored 1 session",
        "Restored 0 sessions",
        "Restored 2 · skipped 1 (running)",
        "Restored 1 · failed 2: tmux unavailable",
        "Restored 2 · skipped 1 (running) · failed 1: log missing",
        "Restored 0 · failed 1",
        "Restored 0 sessions",
    ]


def test_hint_mode_discovers_the_visible_row_and_skips_the_hidden_one() -> None:
    result = harness(
        """
        const body = el("body");
        body.appendChild(sidebar);
        const documentTarget = {
          body,
          activeElement: null,
          defaultView: { innerHeight: 1000, innerWidth: 1000, getComputedStyle: (node) => node.style },
          createElement: (tag) => new Node(tag),
          querySelectorAll(selector) { return query(body, selector); },
        };
        const hints = ctx.window.CodoxearHintMode.createHintModeController({
          documentTarget,
          isTextEntryElement: () => false,
          isMobile: () => false,
          modalIsolationTargets: [],
          isModalTargetOpen: () => false,
          addAppEvent: () => {},
          shellHints: [],
        });
        const labelsFor = () => Array.from(hints.collectTargets().entries()).filter(([, node]) => node === button).map(([label]) => label);
        const whileHidden = labelsFor();
        snapshot = { saved_at: 1, entries: [{ session_id: "a", live: false }] };
        sessionCatalog.applySnapshot({ latestSessions: [] });
        await flush();
        const whileVisible = labelsFor();
        restoreGate = new Promise(() => {});
        button.click();
        await flush();
        const whileRestoring = labelsFor();
        process.stdout.write(JSON.stringify({ whileHidden, whileVisible, whileRestoring }));
        """,
        extra_modules=(APP_HINT_MODE_JS,),
    )
    assert result == {"whileHidden": [], "whileVisible": ["r"], "whileRestoring": []}
