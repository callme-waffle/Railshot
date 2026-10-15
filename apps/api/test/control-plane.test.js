import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createControlPlane } from '../src/control-plane.js';
import { privateJson, savePrivate, EnvironmentError } from '../src/environments.js';
import { createAppServer } from '../src/server.js';
import { createProductStore } from '../src/product-store.js';
import { DatabaseSync } from 'node:sqlite';
import { createHash } from 'node:crypto';

const spec = { version: 1, name: 'demo-env', provider: 'aws', account_id: 'aws-test', region: 'ap-northeast-2',
  scope: 'dedicated', application_name: 'demo-app', runtime: { kind: 'kubeadm', control_planes: 1, workers: 2 }, database: { mode: 'none' } };

test('bridge loads real application factory only after native evidence and confines dedicated app ownership', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'control-bridge-'));
  const configPath = join(directory, 'config.json');
  await savePrivate(configPath, { accounts: [{ owner_id: 'account:alice' }] });
  let factoryCalls = 0;
  const row = { id: 'env-owned', spec, status: 'blocked', deployable: false,
    readiness: { infrastructure: true, cluster: true, storage: true, public_access: true, deployment_binding: false }, blockers: [], operations: [] };
  const runner = async (_, args) => {
    const request = await privateJson(args.at(-1));
    if (request.owner_id !== 'account:alice') throw new EnvironmentError('ENVIRONMENT_NOT_FOUND', 404);
    if (request.action === 'list') return { items: [structuredClone(row)] };
    if (request.action === 'pending') return { items: [] };
    if (request.action === 'get') return structuredClone(row);
    if (request.action === 'binding') return { application_config_path: '/private/owned-app.json', binding_hash: 'approved' };
    if (request.action === 'confirm_binding') {
      assert.equal(request.binding_hash, 'approved'); return { ...row, status: 'ready', deployable: true };
    }
    throw new Error('Unexpected private operation');
  };
  try {
    const control = await createControlPlane({ configPath, stateDirectory: join(directory, 'requests'), runner,
      applicationFactory: async ({ configPath }) => {
        factoryCalls++; assert.equal(configPath, '/private/owned-app.json');
        return { targets: { 'env-owned': { provider: 'aws', automaticDelivery: true } }, describe: (id, app) => ({ id, app }) };
      } });
    assert.equal(factoryCalls, 1);
    assert.equal(control.targets('account:alice')[0].deployable, true);
    assert.deepEqual(control.targets('account:bob'), []);
    assert.throws(() => control.writable('env-owned', 'account:bob'), /ENVIRONMENT_NOT_FOUND/);
    assert.throws(() => control.application.describe('env-owned', 'another-app'), /DEDICATED_ENVIRONMENT_APPLICATION_MISMATCH/);
    assert.deepEqual(control.application.describe('env-owned', 'demo-app'), { id: 'env-owned', app: 'demo-app' });
    row.readiness.cluster = false;
    await control.get('account:alice', 'env-owned');
    assert.throws(() => control.writable('env-owned', 'account:alice'), /ENVIRONMENT_NOT_READY/);
    assert.equal(factoryCalls, 1);
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test('resource HTTP routes preserve pagination, accepted headers, method validation and authenticated ownership', async () => {
  const calls = [];
  const controlPlane = {
    accounts: async (owner) => { calls.push(['accounts', owner]); return { items: [{ id: 'aws-test', provider: 'aws' }] }; },
    list: async () => ({ items: [] }),
    plan: async (owner, body, key) => { calls.push(['plan', owner, key]); return { id: 'env-owned', spec: body, status: 'planned' }; },
    mutate: async (owner, id, action, key) => { calls.push(['mutate', owner, id, action, key]); return { id, status: 'queued' }; },
    get: async (_, id) => ({ id, status: 'queued' }),
  };
  const product = { dashboard: { session: () => ({ id: 'session-alice' }), accounts: { session: () => null } }, controlPlane,
    assertEnvironmentRemovable: () => { throw new EnvironmentError('ENVIRONMENT_IN_USE', 409); } };
  const server = createAppServer({ product, service: null, access: { remote: false, publicDemo: true, allowedHosts: new Set(['127.0.0.1']), allowedOrigins: new Set() } });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    let response = await fetch(base + '/api/v1/tenancies');
    assert.equal(response.status, 200); assert.deepEqual((await response.json()).items, [{ id: 'aws-test', provider: 'aws' }]);
    assert.deepEqual(calls[0], ['accounts', 'session-alice']);
    response = await fetch(base + '/api/v1/clusters', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': 'plan-1' }, body: JSON.stringify(spec) });
    assert.equal(response.status, 201); assert.equal(response.headers.get('location'), '/api/v1/clusters/env-owned');
    response = await fetch(base + '/api/v1/clusters/env-owned/operations', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': 'apply-1' }, body: JSON.stringify({ action: 'apply' }) });
    assert.equal(response.status, 202); assert.equal(response.headers.get('retry-after'), '2');
    const accepted = await response.json(); assert.equal(accepted.status, 'accepted'); assert.equal(accepted.request_id, response.headers.get('x-request-id'));
    response = await fetch(base + '/api/v1/clusters/env-owned', { method: 'DELETE', headers: { 'Idempotency-Key': 'delete-1' } });
    assert.equal(response.status, 409); assert.equal((await response.json()).error.code, 'ENVIRONMENT_IN_USE');
    const count = calls.length;
    response = await fetch(base + '/api/v1/clusters/env-owned/operations', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': 'unsafe' }, body: JSON.stringify({ action: 'apply', command: 'sh' }) });
    assert.equal(response.status, 422); assert.equal(calls.length, count);
    response = await fetch(base + '/api/v1/tenancies', { method: 'POST' });
    assert.equal(response.status, 405); assert.equal(response.headers.get('allow'), 'GET');
  } finally { await new Promise((resolve) => server.close(resolve)); }
});

test('account history preserves legacy sessions and keeps foreign accounts out of maintenance pagination', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'control-history-'));
  const store = await createProductStore(directory);
  try {
    const legacy = store.dashboard.session(null).id;
    const db = new DatabaseSync(join(directory, 'dashboard.sqlite3'));
    for (const name of ['alice', 'bob']) {
      db.prepare('INSERT INTO accounts VALUES (?, ?, ?, ?, ?)').run(name, name + '@example.test', 'unused', Date.now(), Date.now());
      db.prepare('INSERT INTO account_sessions VALUES (?, ?, ?, ?, ?)').run(createHash('sha256').update(name).digest('hex'), name, 'csrf', Date.now() + 60000, Date.now());
      assert.equal(store.dashboard.accounts.session(name).id, name);
      assert.ok(db.prepare('SELECT id FROM sessions WHERE id=?').get('account:' + name));
    }
    assert.equal(store.dashboard.accounts.session('invalid'), null);
    db.close();
    await store.transaction((state) => {
      for (const [id, owner, created] of [['legacy', legacy, '2026-01-01'], ['owned', 'account:alice', '2026-01-03'], ['foreign', 'account:bob', '2026-01-02']]) {
        state.operations[id] = { id, kind: 'deployments', session_id: owner, status: 'succeeded', created_at: created };
      }
    });
    const first = store.operationPage('deployments', [legacy, 'account:alice'], { limit: 1, marker: null });
    assert.deepEqual(first.records.map((row) => row.id), ['owned']); assert.equal(first.total, 2); assert.equal(first.hasMore, true);
    const second = store.operationPage('deployments', [legacy, 'account:alice'], { limit: 1, marker: 'owned' });
    assert.deepEqual(second.records.map((row) => row.id), ['legacy']);
    assert.deepEqual(store.operationPage('deployments', null, { limit: 10, marker: null }).records.map((row) => row.id), ['legacy']);
    assert.throws(() => store.operationPage('deployments', [legacy, 'account:alice'], { limit: 1, marker: 'foreign' }));
  } finally { await store.close(); await rm(directory, { recursive: true, force: true }); }
});

test('logged in owners still read legacy app records with their original session identity', async () => {
  const reads = [];
  const product = { dashboard: { session: () => ({ id: 'legacy-alice' }), accounts: { session: () => ({ id: 'alice', csrf_token: 'csrf' }) } },
    isAccountResource: (id) => id === 'owned-app',
    applications: (owner) => [{ id: owner === 'account:alice' ? 'owned-app' : 'legacy-app', owner }],
    getApplication: (id, owner) => { reads.push([id, owner]); return { id }; } };
  const server = createAppServer({ product, service: null, access: { remote: false, publicDemo: true, allowedHosts: new Set(['127.0.0.1']), allowedOrigins: new Set() } });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const rows = await (await fetch(base + '/api/v1/applications')).json();
    assert.deepEqual(rows.items.map((row) => row.id), ['legacy-app', 'owned-app']);
    assert.equal((await fetch(base + '/api/v1/applications/legacy-app')).status, 200);
    assert.equal((await fetch(base + '/api/v1/applications/owned-app')).status, 200);
    assert.deepEqual(reads, [['legacy-app', 'legacy-alice'], ['owned-app', 'account:alice']]);
  } finally { await new Promise((resolve) => server.close(resolve)); }
});

test('concurrent admission cannot release an environment mutation fence', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'control-admission-'));
  const configPath = join(directory, 'config.json');
  await savePrivate(configPath, { accounts: [{ owner_id: 'account:alice' }] });
  const row = { id: 'env-owned', spec, status: 'planned', deployable: false, readiness: {}, blockers: [], operations: [] };
  let release;
  const admitted = new Promise((resolve) => { release = resolve; });
  const runner = async (_, args) => {
    const request = await privateJson(args.at(-1));
    if (request.action === 'list') return { items: [row] };
    if (request.action === 'pending') return { items: [] };
    if (request.action === 'admit') { await admitted; throw new EnvironmentError('INTENT_REJECTED', 409); }
    throw new Error('unexpected');
  };
  const control = await createControlPlane({ configPath, stateDirectory: join(directory, 'requests'), runner });
  try {
    const first = control.mutate('account:alice', row.id, 'apply', 'first');
    await assert.rejects(control.mutate('account:alice', row.id, 'delete', 'second'), /ENVIRONMENT_BUSY/);
    assert.throws(() => control.writable(row.id, 'account:alice'), /ENVIRONMENT_BUSY/);
    release(); await assert.rejects(first, /INTENT_REJECTED/);
    assert.throws(() => control.writable(row.id, 'account:alice'), /ENVIRONMENT_NOT_READY/);
  } finally { release(); await control.close(); await rm(directory, { recursive: true, force: true }); }
});
