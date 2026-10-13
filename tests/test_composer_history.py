"""Behavioral VM coverage for shell-style sent-message recall in the composer."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import textwrap

from frontend_module_loader import module_path


ROOT = Path(__file__).resolve().parents[1]
COMPOSER_SOURCE = module_path("app_composer.js").read_text(encoding="utf-8")
HISTORY_SOURCE = module_path("app_composer_history.js").read_text(encoding="utf-8")
SESSION_CATALOG_SOURCE = module_path("app_session_catalog.js").read_text(encoding="utf-8")
SESSION_STATE_SOURCE = module_path("app_session_state.js").read_text(encoding="utf-8")


def _run_node(script: str) -> dict:
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_composer_sent_history_recall_obeys_picker_draft_and_reset_boundaries() -> None:
    script = textwrap.dedent(
        f"""
        const vm = require("vm");
        function Event(type, options = {{}}) {{ this.type = type; Object.assign(this, options); }}
        const ctx = {{ window: {{}}, console, Event }};
        vm.createContext(ctx);
        for (const source of {json.dumps([SESSION_STATE_SOURCE, SESSION_CATALOG_SOURCE, HISTORY_SOURCE, COMPOSER_SOURCE])}) {{
          vm.runInContext(source, ctx);
        }}

        function node(extra = {{}}) {{
          const listeners = new Map();
          const attributes = new Map();
          return {{
            value: "", textContent: "", title: "", disabled: false, scrollHeight: 32,
            selectionStart: 0, selectionEnd: 0, style: {{}}, dataset: {{}}, children: [],
            classList: {{ toggle() {{}}, add() {{}}, remove() {{}} }},
            addEventListener(type, handler) {{
              if (!listeners.has(type)) listeners.set(type, new Set());
              listeners.get(type).add(handler);
            }},
            removeEventListener(type, handler) {{ listeners.get(type)?.delete(handler); }},
            emit(type, event = {{}}) {{ for (const handler of listeners.get(type) || []) handler(event); }},
            dispatchEvent(event) {{ this.emit(event.type, event); return true; }},
            setAttribute(name, value) {{ attributes.set(name, String(value)); }},
            getAttribute(name) {{ return attributes.get(name); }},
            removeAttribute(name) {{ attributes.delete(name); }},
            appendChild(child) {{ this.children.push(child); return child; }},
            set innerHTML(value) {{ this.children = []; }}, get innerHTML() {{ return ""; }},
            focus() {{}}, blur() {{}},
            ...extra,
          }};
        }}
        const noop = () => {{}};
        const flush = async () => {{ for (let index = 0; index < 12; index += 1) await Promise.resolve(); }};
        const key = (name) => ({{ key: name, isComposing: false, prevented: false, preventDefault() {{ this.prevented = true; }}, stopPropagation: noop }});

        function harness() {{
          const textarea = node();
          const form = node({{ requestSubmit: noop }});
          const sessionState = ctx.window.CodoxearSessionState.createSessionState({{ consoleError: noop }});
          sessionState.set("selected", "sid");
          const sessionCatalog = ctx.window.CodoxearSessionCatalog.createSessionCatalog({{ consoleError: noop }});
          sessionCatalog.set("latestSessions", [{{ session_id: "sid", agent_backend: "pi" }}, {{ session_id: "sid-2", agent_backend: "pi" }}]);
          const requests = [];
          const drafts = [];
          const api = async (path) => {{
            requests.push(path);
            if (path.includes("cursor=older")) return {{ entries: [{{ id: "old", text: "oldest" }}], next_cursor: null }};
            return {{ entries: [{{ id: "new", text: "newest" }}, {{ id: "recent", text: "recent" }}], next_cursor: "older" }};
          }};
          const history = ctx.window.CodoxearComposerHistory.createComposerHistoryController({{ textarea, sessionState, api }});
          const sent = [];
          const controller = ctx.window.CodoxearComposer.createComposerController({{
            form, textarea, msgPh: node(), sendBtn: node(), sendChoice: node(), sendChoiceBackdrop: node(),
            sendChoiceNowBtn: node(), sendChoiceLaterBtn: node(), sendChoiceCancelBtn: node(),
            sessionCatalog, sessionLaunchFailed: () => false, sessionState, getStagedAttachments: () => [],
            isModalOpen: () => false, api, setToast: noop, setPollFastUntilMs: noop, kickPoll: noop,
            sendText: async (text) => {{ sent.push(text); return true; }}, enqueueComposerText: async () => true,
            prepareModalOpen: noop, afterModalVisibilityChanged: noop, restoreModalFocus: noop,
            storageGetItem: () => null, storageSetItem: (_key, value) => drafts.push(value), storageRemoveItem: noop,
            requestFrame: (callback) => callback(), getComputedStyle: () => ({{ minHeight: "32px" }}),
            activeElement: () => null, isHTMLElement: () => false, historyController: history,
          }});
          return {{ textarea, form, sessionState, requests, drafts, history, sent, controller }};
        }}

        async function recall(run) {{
          const event = key("ArrowUp");
          run.textarea.emit("keydown", event);
          await flush();
          return event;
        }}

        (async () => {{
          const walk = harness();
          const first = await recall(walk);
          const firstValue = walk.textarea.value;
          const second = await recall(walk);
          const secondValue = walk.textarea.value;
          const third = await recall(walk);
          const thirdValue = walk.textarea.value;
          const oldest = await recall(walk);
          const walkResult = {{
            values: [first.prevented ? firstValue : null, second.prevented ? secondValue : null, third.prevented ? thirdValue : null],
            current: walk.textarea.value,
            requests: walk.requests,
            draftWrites: walk.drafts,
            oldestDidNotWrite: walk.drafts.length === 3,
          }};

          const character = harness();
          await recall(character);
          character.textarea.emit("keydown", key("x"));
          const arrowDown = harness();
          await recall(arrowDown);
          arrowDown.textarea.emit("keydown", key("ArrowDown"));
          const pointer = harness();
          await recall(pointer);
          pointer.textarea.emit("pointerdown");
          const selection = harness();
          await recall(selection);
          selection.textarea.emit("select");
          const modifier = harness();
          await recall(modifier);
          modifier.textarea.emit("keydown", key("Shift"));

          const switchRun = harness();
          await recall(switchRun);
          switchRun.sessionState.set("selected", "sid-2");

          const sendRun = harness();
          await recall(sendRun);
          await sendRun.form.onsubmit({{ preventDefault: noop }});

          const nonEmpty = harness();
          nonEmpty.textarea.value = "existing draft";
          const nonEmptyEvent = key("ArrowUp");
          nonEmpty.textarea.emit("keydown", nonEmptyEvent);
          const composing = harness();
          const composingEvent = key("ArrowUp");
          composingEvent.isComposing = true;
          composing.textarea.emit("keydown", composingEvent);

          let pickerHistoryCalls = 0;
          const pickerTextarea = node();
          const pickerForm = node({{ requestSubmit: noop }});
          const pickerState = ctx.window.CodoxearSessionState.createSessionState({{ consoleError: noop }});
          pickerState.set("selected", "picker");
          const pickerCatalog = ctx.window.CodoxearSessionCatalog.createSessionCatalog({{ consoleError: noop }});
          pickerCatalog.set("latestSessions", [{{ session_id: "picker", agent_backend: "pi", slash_commands: [{{ name: "status", description: "show status" }}] }}]);
          const picker = node({{ id: "modelPicker" }});
          const previousDocument = ctx.document;
          ctx.document = {{ createElement: () => node(), createTextNode: (text) => ({{ textContent: text }}), activeElement: null }};
          const pickerComposer = ctx.window.CodoxearComposer.createComposerController({{
            form: pickerForm, textarea: pickerTextarea, msgPh: node(), modelPicker: picker, sendBtn: node(), sendChoice: node(), sendChoiceBackdrop: node(),
            sendChoiceNowBtn: node(), sendChoiceLaterBtn: node(), sendChoiceCancelBtn: node(),
            sessionCatalog: pickerCatalog, sessionLaunchFailed: () => false, sessionState: pickerState, getStagedAttachments: () => [],
            isModalOpen: () => false, api: async () => ({{}}), setToast: noop, setPollFastUntilMs: noop, kickPoll: noop,
            sendText: async () => true, enqueueComposerText: async () => true,
            prepareModalOpen: noop, afterModalVisibilityChanged: noop, restoreModalFocus: noop,
            storageGetItem: () => null, storageSetItem: noop, storageRemoveItem: noop,
            requestFrame: (callback) => callback(), getComputedStyle: () => ({{ minHeight: "32px" }}), activeElement: () => null, isHTMLElement: () => false,
            historyController: {{ handleKeydown: () => {{ pickerHistoryCalls += 1; return false; }}, reset: noop }},
          }});
          pickerTextarea.value = "/";
          pickerTextarea.emit("input");
          const pickerEvent = key("ArrowUp");
          pickerTextarea.emit("keydown", pickerEvent);
          ctx.document = previousDocument;

          process.stdout.write(JSON.stringify({{
            walk: walkResult,
            exits: {{ character: !character.history.isActive(), arrowDown: !arrowDown.history.isActive(), pointer: !pointer.history.isActive(), cursorMove: !selection.history.isActive(), modifier: modifier.history.isActive() }},
            switchReset: !switchRun.history.isActive(),
            sendReset: {{ inactive: !sendRun.history.isActive(), sent: sendRun.sent, value: sendRun.textarea.value }},
            nonEmpty: {{ untouched: nonEmpty.textarea.value, prevented: nonEmptyEvent.prevented, requests: nonEmpty.requests.length }},
            composing: {{ prevented: composingEvent.prevented, requests: composing.requests.length }},
            picker: {{ display: picker.style.display, prevented: pickerEvent.prevented, historyCalls: pickerHistoryCalls }}, 
          }}));
        }})().catch((error) => {{ console.error(error); process.exit(1); }});
        """
    )

    assert _run_node(script) == {
        "walk": {
            "values": ["newest", "recent", "oldest"],
            "current": "oldest",
            "requests": [
                "/api/sessions/sid/sent-history?limit=5",
                "/api/sessions/sid/sent-history?cursor=older&limit=5",
            ],
            "draftWrites": ["newest", "recent", "oldest"],
            "oldestDidNotWrite": True,
        },
        "exits": {"character": True, "arrowDown": True, "pointer": True, "cursorMove": True, "modifier": True},
        "switchReset": True,
        "sendReset": {"inactive": True, "sent": ["newest"], "value": ""},
        "nonEmpty": {"untouched": "existing draft", "prevented": False, "requests": 0},
        "composing": {"prevented": False, "requests": 0},
        "picker": {"display": "block", "prevented": True, "historyCalls": 0},
    }
