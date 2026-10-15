import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createControlPlane } from '../src/control-plane.js';
import { privateJson, savePrivate, EnvironmentError } from '../src/environments.js';
import { createAppServer } from '../src/server.js';

const spec = { version: 1, name: 'demo-env', provider: 'aws', account_id: 'aws-test', region: 'ap-northeast-2',
  scope: 'dedicated', application_name: 'demo-app', runtime: { kind: 'kubeadm', control_planes: 1, workers: 2 }, database: { mode: 'none' } };

test('bridge loads real application factory only after native evidence and confines dedicated app ownership', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'control-bridge-'));
  const configPath = join(directory, 'config.json');
  await savePrivate(configPath, { accounts: [{ owner_id: 'account:alice' }] });
  let factoryCalls = 0;
  const row = { id: 'env-owned', spec, status: 'blocked', deployable: false,
    readiness: { infrastructure: true, cluster: true, storage: true, deployment_binding: false }, blockers: [], operations: [] };
  const runner = async (_, args) => {
    const request = await privateJson(args.at(-1));
    if (request.owner_id !== 'account:alice') throw new EnvironmentError('ENVIRONMENT_NOT_FOUND', 404);
    if (request.action === 'list') return { items: [structuredClone(row)] };
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
