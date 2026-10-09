"""Execute the real worker against an in-memory Cache Storage/browser sandbox."""
import json
import subprocess
from pathlib import Path


WORKER = Path(__file__).resolve().parents[1] / "codoxear/static/service-worker.js"
HARNESS = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const origin = 'https://codoxear.example';
const listeners = {}, stores = new Map(), fetches = [], notifications = [], clientCalls = [];
let network = async () => new Response('online');
let skips = 0, puts = 0;
const warnings = [];
const keyUrl = request => typeof request === 'string' ? new URL(request, origin).href : request.url;
const caches = {
  async open(name) {
    if (!stores.has(name)) stores.set(name, new Map());
    const store = stores.get(name);
    return {
      async match(request, options = {}) {
        const url = keyUrl(request);
        for (const [key, response] of store) {
          if (key === url || (options.ignoreSearch && key.split('?')[0] === url.split('?')[0])) return response.clone();
        }
      },
      async put(request, response) { puts++; store.set(keyUrl(request), response.clone()); },
      async keys() { return [...store.keys()].map(url => new Request(url)); },
      async delete(request) { return store.delete(keyUrl(request)); },
    };
  },
  async keys() { return [...stores.keys()]; },
  async delete(name) { return stores.delete(name); },
};
const clients = {
  async claim() { clientCalls.push('claim'); },
  async matchAll() { return []; },
  async openWindow(url) { clientCalls.push(url); },
};
const self = {
  location: { origin },
  registration: { scope: origin + '/', async showNotification(title, options) { notifications.push({ title, options }); } },
  async skipWaiting() { skips++; },
  addEventListener(type, listener) { listeners[type] = listener; },
};
const ctx = { self, clients, caches, URL, Request, console: { warn: (...args) => warnings.push(args) }, fetch: async request => { fetches.push(request.url); return network(request); } };
vm.createContext(ctx);
vm.runInContext(WORKER_SOURCE, ctx);
function dispatch(type, extra = {}) {
  const waits = [];
  let response;
  listeners[type]({ ...extra, waitUntil(promise) { waits.push(promise); }, respondWith(promise) { response = promise; } });
  return { response, waits, async finish() { await Promise.all(waits); } };
}
function request(path, options = {}) {
  return { url: new URL(path, origin).href, method: options.method || 'GET', mode: options.mode || 'cors' };
}
async function get(path, options = {}) {
  const event = dispatch('fetch', { request: request(path, options) });
  assert.ok(event.response, 'expected interception');
  const response = await event.response;
  await event.finish();
  return response;
}
async function cached(name, path) { return (await caches.open(name)).match(request(path)); }
function offline() { network = async () => { throw new TypeError('offline'); }; }
"""


def run_worker(body: str) -> None:
    program = "const WORKER_SOURCE = " + json.dumps(WORKER.read_text()) + ";\n" + HARNESS
    program += "\n(async () => {\n" + body + "\n})().catch(e => { console.error(e); process.exit(1); });"
    subprocess.run(["node", "-e", program], check=True, text=True, capture_output=True)


def test_navigation_network_first_and_ignore_search_offline_fallback() -> None:
    run_worker("""
      assert.equal(await (await get('/?entry=one', { mode: 'navigate' })).text(), 'online');
      assert.equal(await (await cached('codoxear-html-v1', '/?entry=one')).text(), 'online');
      network = async () => new Response('fresh shell');
      assert.equal(await (await get('/?entry=one', { mode: 'navigate' })).text(), 'fresh shell');
      offline();
      assert.equal(await (await get('/?entry=two', { mode: 'navigate' })).text(), 'fresh shell');
      await assert.rejects(get('/uncached', { mode: 'navigate' }), /offline/);
    """)


def test_api_200_snapshots_survive_offline_with_exact_query_keys() -> None:
    run_worker("""
      for (const path of ['/api/me', '/api/sessions', '/api/sessions/s1/messages/tail?limit=60', '/api/sessions/s1/messages/history?before=c1']) {
        network = async () => new Response(JSON.stringify({ path }));
        await get(path);
        offline();
        assert.deepEqual(await (await get(path)).json(), { path });
      }
      await assert.rejects(get('/api/sessions/s1/messages/tail?limit=10'), /offline/);
      network = async () => new Response('new sessions');
      assert.equal(await (await get('/api/sessions')).text(), 'new sessions');
    """)


def test_sse_post_and_cross_origin_requests_are_pure_passthrough() -> None:
    run_worker("""
      for (const req of [
        request('/api/sessions/s1/live?after=c1'),
        request('/api/sessions/s1/send', { method: 'POST' }),
        request('/static/app.css', { method: 'HEAD' }),
        request('https://other.example/static/app.css?v=1'),
      ]) {
        const event = dispatch('fetch', { request: req });
        assert.equal(event.response, undefined);
        assert.equal(event.waits.length, 0);
      }
      assert.equal(fetches.length, 0);
      assert.equal(puts, 0);
      // JSON polling is not the excluded SSE stream.
      await get('/api/sessions/s1/messages/live?after=c1');
      assert.ok(await cached('codoxear-api-v1', '/api/sessions/s1/messages/live?after=c1'));
    """)


def test_non_200_responses_never_replace_or_create_cached_entries() -> None:
    run_worker("""
      for (const [path, options, name] of [
        ['/', { mode: 'navigate' }, 'codoxear-html-v1'],
        ['/api/me', {}, 'codoxear-api-v1'],
        ['/static/icon.png', {}, 'codoxear-static-v1'],
      ]) {
        network = async () => new Response('known-good');
        await get(path, options);
        for (const status of [304, 401, 500]) {
          network = async () => new Response(status === 304 ? null : 'error', { status });
          await get(path, options);
          assert.equal(await (await cached(name, path)).text(), 'known-good');
        }
      }
      network = async () => new Response(null, { status: 304 });
      assert.equal((await get('/static/app.js?v=bad')).status, 304);
      assert.equal(await cached('codoxear-static-v1', '/static/app.js?v=bad'), undefined);
      network = async () => new Response('unauthorized', { status: 401 });
      await get('/api/uncached');
      offline();
      await assert.rejects(get('/api/uncached'), /offline/);
    """)


def test_versioned_static_cache_first_and_prunes_other_versions_of_same_path() -> None:
    run_worker("""
      await get('/static/app.js?v=old');
      await get('/static/other.js?v=old');
      const before = fetches.length;
      offline();
      assert.equal(await (await get('/static/app.js?v=old')).text(), 'online');
      assert.equal(fetches.length, before);
      network = async () => new Response('new');
      await get('/static/app.js?v=new');
      assert.equal(await cached('codoxear-static-v1', '/static/app.js?v=old'), undefined);
      assert.ok(await cached('codoxear-static-v1', '/static/other.js?v=old'));
      assert.equal(await (await cached('codoxear-static-v1', '/static/app.js?v=new')).text(), 'new');
      network = async () => new Response('broken', { status: 500 });
      await get('/static/app.js?v=failed');
      assert.ok(await cached('codoxear-static-v1', '/static/app.js?v=new'));
      assert.equal(await cached('codoxear-static-v1', '/static/app.js?v=failed'), undefined);
    """)


def test_unversioned_static_serves_cache_before_background_revalidation_finishes() -> None:
    run_worker("""
      await get('/static/monaco/editor.worker.js');
      let resolveFetch;
      network = () => new Promise(resolve => { resolveFetch = resolve; });
      const event = dispatch('fetch', { request: request('/static/monaco/editor.worker.js') });
      assert.equal(event.waits.length, 1);
      assert.equal(await (await event.response).text(), 'online');
      resolveFetch(new Response('updated worker'));
      await event.finish();
      assert.equal(await (await cached('codoxear-static-v1', '/static/monaco/editor.worker.js')).text(), 'updated worker');
      offline();
      assert.equal(await (await get('/static/monaco/editor.worker.js')).text(), 'updated worker');
      await assert.rejects(get('/uncached-icon.png'), /offline/);
    """)


def test_install_activate_and_push_notification_behavior() -> None:
    run_worker("""
      await caches.open('codoxear-static-old');
      await caches.open('other-app');
      await caches.open('codoxear-api-v1');
      await dispatch('install').finish();
      offline();
      await dispatch('activate').finish();
      assert.equal(skips, 1);
      assert.deepEqual(clientCalls, ['claim']);
      assert.equal((await caches.keys()).includes('codoxear-static-old'), false);
      assert.ok((await caches.keys()).includes('other-app'));
      assert.ok(warnings.length > 0);
      await dispatch('push', { data: { json: () => ({ session_display_name: 'Repo', notification_text: 'Done', session_id: 'a b' }) } }).finish();
      assert.equal(notifications[0].title, 'Repo');
      assert.equal(notifications[0].options.body, 'Done');
      assert.equal(notifications[0].options.data.url, origin + '/#session=a%20b');
      await dispatch('push', { data: { json() { throw new Error('malformed'); } } }).finish();
      assert.equal(notifications[1].title, 'Codoxear');
      assert.equal(notifications[1].options.body, 'New final response');
      let closed = false;
      await dispatch('notificationclick', { notification: { data: { url: origin + '/#session=s1' }, close() { closed = true; } } }).finish();
      assert.equal(closed, true);
      assert.equal(clientCalls[1], origin + '/#session=s1');
    """)


def test_prefixed_scope_routes_api_and_sse_without_capturing_other_app_paths() -> None:
    run_worker("""
      self.registration.scope = origin + '/codoxear/';
      await get('/codoxear/api/sessions');
      offline();
      assert.equal(await (await get('/codoxear/api/sessions')).text(), 'online');
      assert.ok(await cached('codoxear-api-v1', '/codoxear/api/sessions'));
      assert.equal(await cached('codoxear-static-v1', '/codoxear/api/sessions'), undefined);
      const before = fetches.length;
      for (const path of ['/codoxear/api/sessions/s1/live?after=c1', '/api/sessions', '/elsewhere/app.js?v=1']) {
        const event = dispatch('fetch', { request: request(path) });
        assert.equal(event.response, undefined);
        assert.equal(event.waits.length, 0);
      }
      assert.equal(fetches.length, before);
      network = async () => new Response('prefixed shell');
      await get('/codoxear/?entry=1', { mode: 'navigate' });
      offline();
      assert.equal(await (await get('/codoxear/?entry=2', { mode: 'navigate' })).text(), 'prefixed shell');
    """)


def test_activation_warms_shell_assets_and_authenticated_apis_before_any_reload() -> None:
    families = [path.stem for path in (WORKER.parent / 'themes').glob('*.css')]
    run_worker('const themeFamilies = ' + json.dumps(families) + ';' + r"""
      for (const scope of [origin + '/', origin + '/codoxear/']) {
        stores.clear(); fetches.length = 0;
        self.registration.scope = scope;
        const shell = `<link href="app.css?v=release">
          <script src='dist/app.bundle.js?v=release'></script>
          <link href="manifest.webmanifest?v=release">
          <link href="favicon.svg?size=32&amp;v=release">
          <link href="apple-touch-icon.png?v=release">
          <link href="app.css?v=release">
          <link href="https://other.example/foreign.css?v=release">
          <script src="unversioned.js"></script>
          <script>document.write('<link href="themes/' + family + '.css?v=release">')</script>`;
        const expectedAssets = ['app.css?v=release', 'dist/app.bundle.js?v=release',
          'manifest.webmanifest?v=release', 'favicon.svg?size=32&v=release',
          'apple-touch-icon.png?v=release', ...themeFamilies.map(f => `themes/${f}.css?v=release`)];
        network = async req => {
          assert.equal(req.credentials, 'same-origin');
          return new Response(req.url === scope ? shell : req.url);
        };
        const event = dispatch('activate');
        assert.equal(clientCalls.at(-1), 'claim', 'claim starts immediately');
        await event.finish();
        assert.equal(await (await cached('codoxear-html-v1', scope)).text(), shell);
        for (const path of expectedAssets) {
          const url = new URL(path, scope).href;
          assert.equal(await (await cached('codoxear-static-v1', url)).text(), url);
          assert.equal(fetches.filter(u => u === url).length, 1, 'deduplicate assets');
        }
        for (const path of ['api/me', 'api/sessions']) {
          const url = new URL(path, scope).href;
          assert.equal((await cached('codoxear-api-v1', url)).status, 200);
        }
        assert.equal(fetches.length, 3 + expectedAssets.length);
        offline();
        assert.equal(await (await get(scope + '?entry=offline', {mode: 'navigate'})).text(), shell);
        assert.equal((await get(new URL('api/sessions', scope).href)).status, 200);
      }
    """)


def test_offline_activation_claims_and_prunes_without_rejection() -> None:
    run_worker("""
      await caches.open('codoxear-html-old');
      await caches.open('another-app');
      offline();
      const event = dispatch('activate');
      assert.deepEqual(clientCalls, ['claim']);
      await event.finish();
      assert.equal((await caches.keys()).includes('codoxear-html-old'), false);
      assert.ok((await caches.keys()).includes('another-app'));
      assert.equal(puts, 0);
      assert.equal(warnings.length, 3);
    """)


def test_activation_warmup_skips_non_200_and_isolates_asset_failures() -> None:
    run_worker("""
      const shell = '<link href="app.css?v=new"><script src="broken.js?v=new"></script>';
      network = async req => {
        if (req.url.endsWith('/')) return new Response(shell);
        if (req.url.includes('broken.js')) throw new TypeError('asset unavailable');
        if (req.url.endsWith('/api/me')) return new Response('unauthenticated', {status: 401});
        if (req.url.endsWith('/api/sessions')) return new Response(null, {status: 304});
        return new Response('asset');
      };
      await dispatch('activate').finish();
      assert.equal(await cached('codoxear-api-v1', '/api/me'), undefined);
      assert.equal(await cached('codoxear-api-v1', '/api/sessions'), undefined);
      assert.equal(await cached('codoxear-static-v1', '/broken.js?v=new'), undefined);
      assert.ok(await cached('codoxear-static-v1', '/app.css?v=new'));
      assert.ok(await cached('codoxear-static-v1', '/themes/clay.css?v=new'));
      assert.equal(warnings.length, 1);
    """)
