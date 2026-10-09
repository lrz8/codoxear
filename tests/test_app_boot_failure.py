"""Execute the ESM boot against login, worker readiness, and DOM seams."""
import json
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "codoxear/static"


def test_post_login_refresh_orders_readiness_auth_and_render_and_reports_failures() -> None:
    program = "const BOOT = " + json.dumps((STATIC / "app.js").read_text()) + ";\n"
    program += r"""
      const vm = require('node:vm'), assert = require('node:assert/strict');
      class Element {
        constructor(tag) { this.tag = tag; this.attributes = {}; this.children = []; this.style = {}; }
        setAttribute(name, value) { this.attributes[name] = value; }
        appendChild(child) { this.children.push(child); }
        replaceChildren(...children) { this.children = children; }
      }
      async function scenario(mode) {
        const calls = [], errors = [], root = new Element('main');
        root.appendChild(new Element('form'));
        let callback, resolveReady, rejectReady, timeout;
        const ready = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
        const navigator = mode === 'absent' ? {} : { serviceWorker: { ready } };
        const ctx = vm.createContext({
          navigator, calls,
          window: { setTimeout(fn) { timeout = fn; return 1; }, clearTimeout() {} },
          document: { getElementById: () => root, body: root, createElement: tag => new Element(tag) },
          console: { error: (...args) => errors.push(args), warn() {} },
          saveCallback: fn => { callback = fn; },
          authError: mode === 'auth-failure', renderError: mode === 'render-failure',
        });
        const application = new vm.SourceTextModule(`
          function createApplicationController() {
            let count = 0;
            return {
              api: async path => {
                calls.push(['auth', path]);
                if (++count === 1) throw {status: 401};
                if (authError) throw new Error('auth refresh failed');
                return {ok: true};
              },
              renderApp: () => { calls.push(['render']); if (renderError) throw new ReferenceError('missing render dependency'); },
              renderLogin: fn => { calls.push(['login']); saveCallback(fn); },
            };
          }
          const resolveAppUrl = path => path;
          export {createApplicationController, resolveAppUrl};
        `, {context: ctx});
        const sw = new vm.SourceTextModule('export const ensureAppServiceWorker = async () => null;', {context: ctx});
        const boot = new vm.SourceTextModule(BOOT, {context: ctx});
        await boot.link(specifier => specifier === './app_sw.js' ? sw : application);
        await boot.evaluate();
        await new Promise(setImmediate);
        assert.equal(typeof callback, 'function');
        const login = callback();
        if (mode !== 'absent') {
          assert.deepEqual(JSON.parse(JSON.stringify(calls)), [['auth', '/api/me'], ['login']], 'auth refresh must await readiness');
          if (mode === 'ready-failure') rejectReady(new Error('worker unavailable'));
          else if (mode === 'timeout') timeout();
          else resolveReady({active: true});
        }
        await login;
        const expected = [['auth', '/api/me'], ['login'], ['auth', '/api/me']];
        if (mode !== 'auth-failure') expected.push(['render']);
        assert.deepEqual(JSON.parse(JSON.stringify(calls)), expected);
        if (mode === 'auth-failure' || mode === 'render-failure') {
          const message = mode === 'auth-failure' ? 'auth refresh failed' : 'missing render dependency';
          assert.equal(errors.length, 1);
          assert.equal(errors[0][0], 'application bootstrap failed after login');
          assert.equal(errors[0][1].message, message);
          assert.equal(root.children.length, 1);
          const panel = root.children[0];
          assert.equal(panel.attributes['data-codoxear-boot-error'], 'true');
          assert.equal(panel.attributes.role, 'alert');
          assert.equal(panel.textContent, `Codoxear failed to start\n\n${message}\n\nReload to retry.`);
        } else assert.equal(errors.length, 0);
      }
      (async () => {
        for (const mode of ['ready', 'ready-failure', 'absent', 'timeout', 'auth-failure', 'render-failure']) await scenario(mode);
      })().catch(e => { console.error(e); process.exit(1); });
    """
    subprocess.run(["node", "--experimental-vm-modules", "-e", program], check=True, capture_output=True, text=True)
