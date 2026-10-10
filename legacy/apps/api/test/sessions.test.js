import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile, stat, mkdir, readdir } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createHash, createDecipheriv } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { setTimeout as pause } from 'node:timers/promises';
import { createAppServer } from '../src/server.js';
import { apiAccessConfig } from '../src/access.js';
import { createProductStore } from '../src/product-store.js';

const source = () => {
  const form = new FormData(); form.set('app', 'demo-app'); form.set('target_id', 'demo');
  form.append('files', new Blob(['console.log("hello")']), 'app.js'); form.set('paths', '["app.js"]'); return form;
};
async function fixture(t, remote = false, publicDemo = true) {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-session-'));
  let runs = 0, server, origin;
  const sourceCommit = 'a'.repeat(40);
  const service = { targetId: 'demo', deploy: async () => ({ run_id: ++runs, source_commit: sourceCommit }),
    status: async (id) => ({ run_id: Number(id), state: 'published', status: 'completed', conclusion: 'success',
      publication: { run_id: Number(id), target_id: 'demo', app: 'demo-app', source_commit: sourceCommit, artifact_id: 5, producer_attempt: 1 } }) };
  async function start() {
    server = createAppServer({ stateDirectory: directory, service, pollInterval: 1,
      access: apiAccessConfig({ RAILSHOT_PUBLIC_DEMO: publicDemo ? '1' : '0', RAILSHOT_BIND_HOST: remote ? '0.0.0.0' : '127.0.0.1',
        RAILSHOT_ALLOWED_HOSTS: '127.0.0.1', ...(publicDemo ? { RAILSHOT_ALLOWED_ORIGINS: 'http://127.0.0.1' } : { RAILSHOT_API_TOKEN: 'internal-proxy-token-test-0123456789' }) }),
      environmentAdapter: { profiles: () => [], plan: async (_, { id }) => ({ public: { id, executable: true }, private: {} }), verifyPlan: async () => {}, execute: async () => assert.fail('foreign plan executed') },
      deployPublished: async () => ({ cd: { deployed: true, state: 'succeeded', revision: 'b'.repeat(40) }, public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://demo.example.test' } }) });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve)); origin = `http://127.0.0.1:${server.address().port}`;
  }
  async function stop() { await new Promise((resolve) => server.close(resolve)); await (await server.productReady).close(); }
  await start();
  t.after(async () => { await stop(); await rm(directory, { recursive: true, force: true }); });
  const client = () => {
    let cookie;
    return { get cookie() { return cookie; }, async request(path, options = {}) {
      const response = await fetch(origin + path, { ...options, headers: { ...(!publicDemo && { Authorization: 'Bearer internal-proxy-token-test-0123456789' }), ...(cookie ? { Cookie: cookie } : {}), ...options.headers } });
      const set = response.headers.get('set-cookie'); if (set) cookie = set.split(';')[0];
      const body = response.status === 204 ? null : await response.json(); return { response, body };
    } };
  };
  return { directory, client, get origin() { return origin; }, runs: () => runs, restart: async () => { await stop(); await start(); } };
}
const json = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
async function completed(client, id) {
  for (let count = 0; count < 100; count++) {
    const { body } = await client.request(`/api/v1/deployments/${id}`);
    if (body.status === 'succeeded') return body;
    await pause(10);
  }
  assert.fail('deployment did not complete');
}

test('anonymous sessions isolate histories, IDs, legacy reads, plans and idempotency; survive restart', async (t) => {
  const f = await fixture(t), a = f.client(), b = f.client();
  const session = await a.request('/api/v1/sessions', { method: 'POST' });
  assert.equal(session.response.status, 201); assert.match(session.response.headers.get('set-cookie'), /HttpOnly; SameSite=Strict; Max-Age=2147483647/);
  assert.deepEqual(Object.keys(session.body), ['expires_at']);
  await b.request('/api/v1/sessions', { method: 'POST' }); assert.notEqual(a.cookie, b.cookie);
  const plan = await a.request('/api/v1/plans', json('POST', {}));
  assert.equal((await b.request(`/api/v1/plans/${plan.body.id}`)).response.status, 404);
  assert.deepEqual((await b.request('/api/v1/plans')).body.items, []);
  assert.equal((await b.request('/api/v1/environments', { ...json('POST', { plan_id: plan.body.id }), headers: { 'Content-Type': 'application/json', 'Idempotency-Key': 'env' } })).response.status, 404);
  const first = await a.request('/api/v1/deployments', { method: 'POST', headers: { 'Idempotency-Key': 'same' }, body: source() });
  assert.equal(first.response.status, 202);
  const id = first.body.resource_id, complete = await completed(a, id);
  assert.equal('session_id' in complete, false);
  for (const path of [`/api/v1/deployments/${id}`, `/api/v1/deployments/${id}?view=record`, `/api/v1/builds/${complete.ci.run_id}`, `/api/runs/${complete.ci.run_id}`]) {
    assert.equal((await b.request(path)).response.status, 404);
    assert.equal((await f.client().request(path)).response.status, 404, 'omitting a cookie never grants operator access remotely');
  }
  assert.equal((await a.request('/api/v1/deployments')).body.items.length, 1);
  assert.equal((await b.request('/api/v1/deployments')).body.items.length, 0);
  const second = await b.request('/api/v1/deployments', { method: 'POST', headers: { 'Idempotency-Key': 'same' }, body: source() });
  assert.notEqual(second.body.resource_id, id); await completed(b, second.body.resource_id);
  assert.equal(f.runs(), 2);
  await a.request('/api/v1/preferences', json('PUT', { view: 'history', environment: 'onprem', provider: 'openstack' }));
  assert.equal((await b.request('/api/v1/preferences')).body.view, 'deploy');
  await f.restart();
  assert.equal((await a.request('/api/v1/preferences')).body.view, 'history');
  assert.equal((await a.request('/api/v1/deployments')).body.items[0].id, id);
  const replay = await a.request('/api/v1/deployments', { method: 'POST', headers: { 'Idempotency-Key': 'same' }, body: source() });
  assert.equal(replay.body.id, id); assert.equal(f.runs(), 2);
  assert.equal((await a.request('/api/v1/preferences', { ...json('PUT', { view: 'deploy' }), headers: { 'Content-Type': 'application/json', 'Sec-Fetch-Site': 'cross-site' } })).response.status, 403);
});

test('internal proxy bearer never bypasses remote anonymous session ownership', async (t) => {
  const f = await fixture(t, true, false), a = f.client();
  const accepted = await a.request('/api/v1/deployments', { method: 'POST', headers: { 'Idempotency-Key': 'proxy' }, body: source() });
  assert.equal(accepted.response.status, 202);
  await completed(a, accepted.body.resource_id);
  const b = f.client();
  assert.deepEqual((await b.request('/api/v1/deployments')).body.items, []);
  assert.equal((await b.request(`/api/v1/deployments/${accepted.body.resource_id}`)).response.status, 404);
});

test('connection secrets are encrypted, write-only and scoped; edits preserve or explicitly clear them', async (t) => {
  const f = await fixture(t, true), a = f.client(), b = f.client();
  const session = await a.request('/api/v1/sessions', { method: 'POST' }); assert.match(session.response.headers.get('set-cookie'), /; Secure/);
  await b.request('/api/v1/sessions', { method: 'POST' });
  const input = { label: 'Private OpenStack', console_url: 'https://openstack.example/dashboard/', username: 'demo-admin', password: 'unique-private-password-123' };
  const saved = await a.request('/api/v1/connections', json('POST', input));
  assert.equal(saved.response.status, 201); assert.equal(saved.body.has_password, true);
  assert.equal(JSON.stringify(saved.body).includes(input.password), false);
  assert.deepEqual((await b.request('/api/v1/connections')).body.items, []);
  assert.equal((await b.request(`/api/v1/connections/${saved.body.id}`)).response.status, 404);
  assert.equal((await a.request(saved.response.headers.get('location'))).body.has_password, true);
  assert.equal((await b.request(`/api/v1/connections/${saved.body.id}`, json('PUT', input))).response.status, 404);
  assert.equal((await b.request(`/api/v1/connections/${saved.body.id}`, { method: 'DELETE' })).response.status, 404);
  const db = new DatabaseSync(join(f.directory, 'dashboard.sqlite3'));
  const row = db.prepare('SELECT * FROM connections').get(), ciphertext = Buffer.from(row.password_encrypted);
  assert.equal(ciphertext.includes(Buffer.from(input.password)), false);
  const decipher = createDecipheriv('aes-256-gcm', await readFile(join(f.directory, 'connections.key')), ciphertext.subarray(0, 12));
  decipher.setAAD(Buffer.from(`${row.session_id}:${row.id}`)); decipher.setAuthTag(ciphertext.subarray(12, 28));
  assert.equal(Buffer.concat([decipher.update(ciphertext.subarray(28)), decipher.final()]).toString(), input.password);
  assert.equal(row.session_id, createHash('sha256').update(a.cookie.split('=')[1]).digest('hex'));
  assert.equal((await stat(join(f.directory, 'dashboard.sqlite3'))).mode & 0o777, 0o600);
  assert.equal((await stat(join(f.directory, 'connections.key'))).mode & 0o777, 0o600);
  db.close(); await f.restart();
  assert.equal((await a.request('/api/v1/connections')).body.items[0].has_password, true);
  const { password, ...withoutPassword } = input;
  assert.equal((await a.request(`/api/v1/connections/${saved.body.id}`, json('PUT', withoutPassword))).body.has_password, true);
  assert.equal((await a.request(`/api/v1/connections/${saved.body.id}`, json('PUT', { ...withoutPassword, password: null }))).body.has_password, false);
  assert.equal((await a.request('/api/v1/connections', json('POST', { ...input, console_url: 'javascript:alert(1)' }))).response.status, 422);
  assert.equal((await a.request('/api/v1/connections', json('POST', { ...input, console_url: 'https://id:password@openstack.example/' }))).response.status, 422);
  assert.equal((await a.request(`/api/v1/connections/${saved.body.id}`, { method: 'DELETE' })).response.status, 204);
  assert.deepEqual((await a.request('/api/v1/connections')).body.items, []);
});

test('CLI persists an origin-bound private session across processes', async (t) => {
  const f = await fixture(t), folder = join(f.directory, 'source'), cookies = join(f.directory, 'client');
  await mkdir(folder); await writeFile(join(folder, 'app.js'), 'hello');
  const module = new URL('../src/client.js', import.meta.url).href;
  const env = { ...process.env, RAILSHOT_CLIENT_SESSION_DIR: cookies };
  const run = (script) => promisify(execFile)(process.execPath, ['--input-type=module', '-e', script], { env });
  const deployed = await run(`import {deployPath} from ${JSON.stringify(module)}; console.log(JSON.stringify(await deployPath(${JSON.stringify({ app: 'demo-app', path: folder, baseUrl: f.origin })})));`);
  const id = JSON.parse(deployed.stdout).run_id;
  const status = await run(`import {getRun} from ${JSON.stringify(module)}; console.log(JSON.stringify(await getRun(${id}, ${JSON.stringify(f.origin)})));`);
  assert.equal(JSON.parse(status.stdout).run_id, id);
  const [file] = await readdir(cookies);
  assert.equal((await stat(join(cookies, file))).mode & 0o777, 0o600);
  assert.equal((await stat(cookies)).mode & 0o777, 0o700);
  assert.equal((await f.client().request(`/api/runs/${id}`)).response.status, 404);
});

test('legacy state migrates once without assigning old jobs; expired or forged cookies cannot recover data', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-migrate-')); t.after(() => rm(directory, { recursive: true, force: true }));
  const legacy = { version: 1, operations: { old: { id: 'old', kind: 'deployments', status: 'queued', session_id: 'forged' } }, plans: {}, bindings: {}, keys: {} };
  await writeFile(join(directory, 'state.json'), JSON.stringify(legacy), { mode: 0o600 });
  let store = await createProductStore(directory);
  assert.equal(store.read().operations.old.status, 'unknown'); assert.equal(store.read().operations.old.session_id, undefined);
  assert.equal(await readFile(join(directory, 'state.json'), 'utf8'), JSON.stringify(legacy));
  const session = store.dashboard.session(null); store.dashboard.preferences(session.id, { view: 'history' });
  await store.close();
  const db = new DatabaseSync(join(directory, 'dashboard.sqlite3')); db.prepare('UPDATE sessions SET expires_at = 0').run(); db.close();
  store = await createProductStore(directory);
  try {
    const expired = store.dashboard.session(session.token); assert.notEqual(expired.id, session.id);
    assert.equal(store.dashboard.preferences(expired.id).view, 'deploy');
    const forged = store.dashboard.session('A'.repeat(43)); assert.notEqual(forged.token, 'A'.repeat(43));
    assert.equal(store.read().operations.old.status, 'unknown');
  } finally { await store.close(); }
});

test('SQLite history keysets retain deterministic pages across ties, inserts and restart without disclosing owners', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-history-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  let store = await createProductStore(directory);
  const a = store.dashboard.session(null), b = store.dashboard.session(null);
  const add = (state, id, sessionId, kind = 'deployments', created_at = '2026-10-03T00:00:00.000Z') => {
    state.operations[id] = { id, session_id: sessionId, kind, status: 'failed', created_at, ci: { run_id: `10${id.slice(-1)}` } };
  };
  await store.transaction((state) => {
    for (const id of ['d3', 'd1', 'd5', 'd2', 'd4']) add(state, id, a.id);
    add(state, 'foreign', b.id); add(state, 'legacy', null);
    for (const id of ['b2', 'b1', 'b3']) add(state, id, a.id, 'builds');
  });
  const page = (kind, marker = null, sessionId = a.id) => store.operationPage(kind, sessionId, { limit: 2, marker });
  try {
    const first = page('deployments');
    assert.deepEqual(first.records.map((row) => row.id), ['d5', 'd4']); assert.equal(first.total, 5); assert.equal(first.hasMore, true);
    await store.transaction((state) => add(state, 'new', a.id, 'deployments', '2026-10-03T01:00:00.000Z'));
    const second = page('deployments', 'd4');
    assert.deepEqual(second.records.map((row) => row.id), ['d3', 'd2']); assert.equal(second.total, 6);
    await store.close(); store = await createProductStore(directory);
    assert.deepEqual(page('deployments').records.map((row) => row.id), ['new', 'd5']);
    assert.deepEqual(page('deployments', 'd4').records.map((row) => row.id), ['d3', 'd2']);
    const last = page('deployments', 'd2'); assert.deepEqual(last.records.map((row) => row.id), ['d1']); assert.equal(last.hasMore, false);
    assert.deepEqual(page('deployments', 'd1').records, []);
    for (const marker of ['foreign', 'legacy', 'b1', '__proto__']) assert.throws(() => page('deployments', marker), { status: 422 });
    assert.throws(() => page('deployments', 'd4', b.id), { status: 422 });
    assert.deepEqual(page('deployments', null, b.id).records.map((row) => row.id), ['foreign']);
    const builds = page('builds'); assert.deepEqual(builds.records.map((row) => row.id), ['b3', 'b2']);
    assert.deepEqual(page('builds', '102').records.map((row) => row.id), ['b1']);
    assert.throws(() => page('builds', 'b2'), { status: 422 }, 'build markers are public run IDs, not internal operation IDs');
  } finally { await store.close(); }
});

test('HTTP histories page persisted summaries, reject invalid bounds and keep new admissions off following pages', async (t) => {
  const f = await fixture(t), a = f.client(), b = f.client();
  const admitted = [];
  const deploy = async (client, key) => {
    const { body } = await client.request('/api/v1/deployments', { method: 'POST', headers: { 'Idempotency-Key': key }, body: source() });
    await completed(client, body.resource_id); return body.resource_id;
  };
  for (let index = 0; index < 5; index++) admitted.push(await deploy(a, `page-${index}`));
  const foreign = await deploy(b, 'foreign');
  const first = await a.request('/api/v1/deployments?limit=2');
  assert.equal(first.response.status, 200); assert.equal(first.response.headers.get('cache-control'), 'no-store');
  assert.ok(first.response.headers.get('x-request-id')); assert.equal(first.body.total, 5); assert.equal(first.body.items.length, 2);
  assert.equal(JSON.stringify(first.body).includes('session_id'), false);
  const newer = await deploy(a, 'newer'), seen = first.body.items.map((row) => row.id);
  let marker = first.body.next_marker;
  while (marker) {
    const result = await a.request(`/api/v1/deployments?limit=2&marker=${marker}`);
    assert.equal(result.body.total, 6); seen.push(...result.body.items.map((row) => row.id)); marker = result.body.next_marker;
  }
  assert.equal(new Set(seen).size, 5); assert.deepEqual([...seen].sort(), [...admitted].sort()); assert.ok(!seen.includes(newer));
  await f.restart();
  assert.equal((await a.request('/api/v1/deployments?limit=1')).body.items[0].id, newer);
  for (const query of ['limit=0', 'limit=101', 'limit=-1', 'limit=2.5', 'limit=2&limit=3', 'marker=', 'marker=x&marker=y', `marker=${foreign}`, 'status=failed', 'marker=not-found']) {
    const rejected = await a.request(`/api/v1/deployments?${query}`);
    assert.equal(rejected.response.status, 422, query); assert.equal(rejected.body.error.code, 'INVALID_INPUT');
  }
  assert.equal((await b.request(`/api/v1/deployments?marker=${first.body.next_marker}`)).response.status, 422);
  assert.equal((await f.client().request('/api/v1/deployments')).body.total, 0);
  assert.deepEqual((await a.request('/api/v1/builds?limit=1')).body, { items: [], next_marker: null, total: 0 });
  const builds = [];
  for (let index = 0; index < 3; index++) {
    const accepted = await a.request('/api/v1/builds', { method: 'POST', body: source() });
    assert.equal(accepted.response.status, 202); builds.push(accepted.body.resource_id);
    for (let count = 0; count < 100; count++) {
      const list = (await a.request('/api/v1/builds')).body;
      if (list.items.every((row) => !['queued', 'running'].includes(row.status))) break;
      await pause(10);
    }
  }
  const buildPage = (await a.request('/api/v1/builds?limit=2')).body;
  assert.equal(buildPage.total, 3); assert.equal(buildPage.items.length, 2);
  const buildLast = (await a.request(`/api/v1/builds?limit=2&marker=${buildPage.next_marker}`)).body;
  assert.equal(buildLast.items.length, 1); assert.equal(buildLast.next_marker, null);
  assert.deepEqual([...buildPage.items, ...buildLast.items].map((row) => row.id).sort(), builds.sort());
  assert.equal((await a.request(`/api/v1/builds/${buildLast.items[0].id}`)).response.status, 200);
  assert.equal((await b.request('/api/v1/builds')).body.total, 0);

});
