import json

from frontend_module_loader import module_path
from test_frontend_message_flow_controller import run_flow
from test_frontend_queue_module_source import harness_script, run_node_json


def test_offline_confirmed_send_preserves_composer_without_network_or_optimistic_rows() -> None:
    composer_source = module_path("app_composer.js").read_text()
    result = run_flow(
        f"vm.runInContext({json.dumps(composer_source)}, ctx);\n" + r"""
        const navigator = { onLine: false };
        const requests = [];
        const test = harness({
          navigatorValue: () => navigator,
          api: async (url, options) => { requests.push([url, options]); return {}; },
          hasPendingUploads: () => false,
        });
        function node() {
          return { style: {}, value: '', textContent: '', disabled: false, scrollHeight: 32,
            classList: { toggle() {} }, addEventListener() {}, removeEventListener() {},
            setAttribute() {}, removeAttribute() {}, focus() {}, blur() {},
          };
        }
        const form = node(), textarea = node();
        ctx.window.CodoxearComposer.createComposerController({
          form, textarea, msgPh: node(), sendBtn: node(), sendChoice: node(), sendChoiceBackdrop: node(),
          sendChoiceNowBtn: node(), sendChoiceLaterBtn: node(), sendChoiceCancelBtn: node(),
          sessionCatalog: test.options.sessionCatalog, sessionState: test.options.sessionState,
          sessionLaunchFailed: () => false, getStagedAttachments: () => [], isModalOpen: () => false,
          api: async () => { throw new Error('unexpected composer request'); }, setToast: noop,
          setPollFastUntilMs: noop, kickPoll: noop,
          sendText: (...args) => test.controller.sendText(...args), enqueueComposerText: async () => false,
          prepareModalOpen: noop, afterModalVisibilityChanged: noop, restoreModalFocus: noop,
          storageGetItem: () => null, storageSetItem: noop, storageRemoveItem: noop,
          requestFrame: fn => fn(), getComputedStyle: () => ({ minHeight: '32px' }),
          activeElement: () => null, isHTMLElement: () => false,
        });
        (async () => {
          const draft = '  keep my draft\nexactly  ';
          textarea.value = draft;
          await form.onsubmit({ preventDefault() {} });
          const refused = await test.controller.sendText(draft, { sid: 'another-session' });
          const offline = { refused, value: textarea.value, requests: requests.length,
            events: test.state.events.slice(), sending: Boolean(test.options.sessionState.get('sending')), toasts: test.state.errors.slice() };
          navigator.onLine = true;
          await form.onsubmit({ preventDefault() {} });
          process.stdout.write(JSON.stringify({ offline, online: { value: textarea.value, requests: requests.length } }));
        })().catch(error => { console.error(error); process.exit(1); });
        """
    )
    assert result["offline"] == {
        "refused": False,
        "value": "  keep my draft\nexactly  ",
        "requests": 0,
        "events": [],
        "sending": False,
        "toasts": ["Offline — reconnect to send."] * 2,
    }
    assert result["online"] == {"value": "", "requests": 1}


def test_offline_queue_button_preserves_composer_and_can_enqueue_after_reconnect() -> None:
    result = run_node_json(harness_script(r"""
        const h = globalThis.__harness;
        h.select('sid');
        h.setComposerText('  queue this\nexactly  ');
        h.setOffline(true);
        const refused = await h.controller.enqueueComposerText('different sid text', { sid: 'another' });
        h.dom.queueBtn.onclick({ preventDefault() {}, stopPropagation() {} });
        await Promise.resolve();
        await Promise.resolve();
        const offline = { refused, text: h.getComposerText(), toasts: h.toasts.slice(),
          apiCalls: h.calls.filter(call => call[0] === 'api'), disabled: h.dom.queueBtn.disabled };
        h.setOffline(false);
        h.setApiResponses([{ queued: true, queue_len: 1 }]);
        h.dom.queueBtn.onclick({ preventDefault() {}, stopPropagation() {} });
        // The actual button continuation clears the composer only after success.
        for (let i = 0; i < 10; i++) await Promise.resolve();
        globalThis.__result = { offline, online: { text: h.getComposerText(), apiCalls: h.calls.filter(call => call[0] === 'api') } };
    """))
    assert result["offline"] == {
        "refused": False,
        "text": "  queue this\nexactly  ",
        "toasts": ["Offline — reconnect to queue."] * 2,
        "apiCalls": [],
        "disabled": False,
    }
    assert result["online"] == {
        "text": "",
        "apiCalls": [["api", "/api/sessions/sid/enqueue", {"text": "  queue this\nexactly  "}]],
    }
