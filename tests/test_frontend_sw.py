import json
import subprocess
from pathlib import Path

from frontend_module_loader import module_path


STATIC = Path(__file__).resolve().parents[1] / "codoxear/static"


def test_registration_is_unversioned_memoized_and_retries_after_failure() -> None:
    source = module_path("app_sw.js").read_text()
    script = "const SOURCE = " + json.dumps(source) + r""";
      const vm = require('node:vm'), assert = require('node:assert/strict');
      const calls = [], registration = {};
      let fail = true;
      const ctx = { window: {}, navigator: { serviceWorker: { async register(url, options) {
        calls.push([url, options]);
        if (fail) throw new Error('registration unavailable');
        return registration;
      } } } };
      vm.createContext(ctx);
      vm.runInContext(SOURCE, ctx);
      (async () => {
        const ensure = ctx.window.CodoxearSW.ensureAppServiceWorker;
        const resolve = path => '/codoxear' + path;
        const first = ensure(resolve), concurrent = ensure(resolve);
        assert.equal(first, concurrent);
        await assert.rejects(first, /registration unavailable/);
        fail = false;
        assert.equal(await ensure(resolve), registration);
        assert.equal(await ensure(resolve), registration);
        assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
          ['/codoxear/service-worker.js', { scope: '/codoxear/' }], ['/codoxear/service-worker.js', { scope: '/codoxear/' }],
        ]);
        assert.equal(await ensure(resolve, {}), null);
      })().catch(error => { console.error(error); process.exit(1); });
    """
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_boot_registers_without_push_support_or_waiting_before_auth() -> None:
    """Link actual ESM boot and SW helper; auth uses its unchanged GET seam."""
    script = "const BOOT = " + json.dumps((STATIC / "app.js").read_text()) + ";\n"
    script += "const SW = " + json.dumps((STATIC / "app_sw.js").read_text()) + ";\n"
    script += r"""
      const vm = require('node:vm'), assert = require('node:assert/strict');
      const calls = [];
      const ctx = vm.createContext({
        window: {}, document: {}, console,
        navigator: { serviceWorker: { register(url, options) {
          calls.push(['register', url, options]);
          return new Promise(() => {}); // Boot must not await registration.
        } } },
        calls,
      });
      const app = new vm.SourceTextModule(`
        function createApplicationController() {
          return {
            api: async path => { calls.push(['auth', path]); return { ok: true }; },
            renderApp: () => calls.push(['render']), renderLogin() { throw new Error('unexpected login'); },
          };
        }
        const resolveAppUrl = path => path;
        export { createApplicationController, resolveAppUrl };
      `, { context: ctx });
      const sw = new vm.SourceTextModule(SW, { context: ctx });
      const boot = new vm.SourceTextModule(BOOT, { context: ctx });
      (async () => {
        await boot.link(specifier => specifier === './app_sw.js' ? sw : app);
        await boot.evaluate();
        for (let i = 0; i < 5; i++) await Promise.resolve();
        assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
          ['auth', '/api/me'], ['register', '/service-worker.js', { scope: '/' }], ['render'],
        ]);
      })().catch(error => { console.error(error); process.exit(1); });
    """
    subprocess.run(["node", "--experimental-vm-modules", "-e", script], check=True, capture_output=True, text=True)
