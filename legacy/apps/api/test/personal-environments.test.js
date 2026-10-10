import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { setTimeout as pause } from 'node:timers/promises';
import { createAppServer } from '../src/server.js';
import { apiAccessConfig } from '../src/access.js';
import { DatabaseSync } from 'node:sqlite';
import { createPersonalEnvironments } from '../src/personal-environments.js';

async function fixture(t) {
  const directory = await mkdtemp(join(tmpdir(), 'personal-api-'));
  const calls = { deploy: [], delete: [], gateway: [], verify: [], execute: [], runtime: [], revoke: [] }, targets = {}, runs = new Map();
  const appAdapter = { targets,
    describe: (id, app) => ({ id: 'app-' + createHash('sha256').update(id + app).digest('hex').slice(0, 24), app,
      target_id: 'app-' + createHash('sha256').update(id + app).digest('hex').slice(0, 24), environment_target_id: id, provider: 'openstack' }),
    register: async (app) => ({ ...app, status: 'ready' }),
    deployPublished: async () => ({ cd: { deployed: true, revision: 'a'.repeat(40), state: 'deployed' }, public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://app.example.test' } }),
    planLifecycle: async (app, { id }) => ({ public: { id, application_id: app.id, action: 'delete', plan_hash: 'a'.repeat(64), expires_at: new Date(Date.now() + 600000).toISOString(), resources: [], retained: [] }, private: {} }),
    verifyLifecyclePlan: async () => {},
    applyLifecycle: async (app, plan, options) => { calls.delete.push({ app, options }); return { status: 'succeeded', residuals: [], steps: [] }; },
  };
  const personalAdapter = { config: { public_url: 'https://api.example.test', installer_url: 'https://api.example.test/install.sh', artifact_url: 'https://api.example.test/client.tgz', artifact_sha256: 'b'.repeat(64) },
    readiness: async () => ({ scope: 'personal', ready: true, blockers: [] }),
    execute: async (target, input) => { calls.execute.push({ target_id: target.id, input }); return {ok:true,result:[{id:'server-one',name:'customer VM',status:'ACTIVE'}]}; },
    prepare: async () => false,
    prepareRuntime: async (target) => { calls.runtime.push(target.id); targets[target.id] = { provider: 'openstack', automaticDelivery: true }; return { status: 'succeeded', blockers: [], binding_sha256: 'a'.repeat(64), verified_at: new Date().toISOString() }; },
    reconcileRuntime: async () => ({ status: 'succeeded', blockers: [], binding_sha256: 'a'.repeat(64), verified_at: new Date().toISOString() }),
    verifyRuntime: async () => ({ status: 'succeeded', blockers: [], binding_sha256: 'a'.repeat(64), verified_at: new Date().toISOString() }),
    deploymentReady: (target) => Boolean(targets[target.id]),
    removeRuntime: async (target) => { calls.revoke.push(target.id); return { status: 'succeeded', residuals: [], revocation_verified: true }; },
    register: async (target) => { calls.gateway.push(target.id); return { status: 'succeeded', tunnel: { server_public_key: 'B'.repeat(43) + '=', endpoint: 'wg.example.test:51820', address: '10.80.0.2/32', allowed_ips: '10.80.0.1/32' } }; },
    verify: async (target) => { calls.verify.push(target.id); return { status: 'succeeded', reachable: true, openstack_verified: true }; },
    cleanup: async () => ({ status: 'succeeded', residuals: [] }),
    remove: async () => ({ status: 'succeeded' }),
  };
  const service = { targetId: 'shared', allowTarget() {}, deploy: async (input) => {
    const id = runs.size + 1; runs.set(id, input); calls.deploy.push(input); return { run_id: id, source_commit: 'a'.repeat(40) };
  }, status: async (id) => { const input = runs.get(Number(id)); return { run_id: Number(id), state: 'published', status: 'completed', conclusion: 'success',
    publication: { run_id: Number(id), app: input.app, target_id: input.target_id, source_commit: 'a'.repeat(40), artifact_id: 1, producer_attempt: 1 } }; } };
  let server, base;
  async function start() {
    server = createAppServer({ stateDirectory: directory, service, applicationAdapter: appAdapter, personalAdapter, pollInterval: 1,
      access: apiAccessConfig({ RAILSHOT_PUBLIC_DEMO: '1', RAILSHOT_ALLOWED_HOSTS: '127.0.0.1', RAILSHOT_ALLOWED_ORIGINS: 'http://127.0.0.1' }) });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve)); base = `http://127.0.0.1:${server.address().port}`;
  }
  async function stop() { await new Promise((resolve) => server.close(resolve)); await (await server.productReady).close(); }
  await start(); t.after(async () => { await stop(); await rm(directory, { recursive: true, force: true }); });
  const client = () => { const cookies = new Map(); return { cookies, async request(path, body, options = {}) {
    const response = await fetch(base + path, { method: body === undefined ? 'GET' : 'POST', ...options,
      headers: { Cookie: [...cookies].map(([k, v]) => `${k}=${v}`).join('; '), 'X-Railshot-Request': 'dashboard',
        ...(body !== undefined && { 'Content-Type': 'application/json' }), ...options.headers }, ...(body !== undefined && { body: JSON.stringify(body) }) });
    for (const set of response.headers.getSetCookie()) { const [name, value] = set.split(';')[0].split('='); cookies.set(name, value); }
    return { status: response.status, headers: response.headers, body: await response.json() };
  } }; };
  async function target(client, project = 'project-one') {
    const owner = await client.request('/api/v1/owners', {});
    const created = await client.request('/api/v1/targets', { label: '내 OpenStack', provider: 'openstack' }); assert.equal(created.status, 201);
    const enrollment = await client.request(`/api/v1/targets/${created.body.id}/enrollments`, {}); assert.equal(enrollment.status, 201);
    const token = /RAILSHOT_ENROLLMENT_TOKEN='([^']+)'/.exec(enrollment.body.install_command)[1];
    const claim = await client.request(`/api/v1/enrollments/${enrollment.body.id}/claims`, { public_key: 'A'.repeat(43) + '=', client_version: '1.0', project_id: project, runtime: {ssh_host_key:'ssh-ed25519 '+'A'.repeat(68)} }, { headers: { Authorization: `Bearer ${token}` } });
    return { owner, created, enrollment, token, claim, id: created.body.id };
  }
  const heartbeat = (client, row) => client.request(`/api/v1/targets/${row.id}/heartbeats`, { generation: row.claim.body.generation, client_version: '1.0', checks: { tunnel: true, openstack: true, runtime: true } }, { headers: { Authorization: `Bearer ${row.claim.body.client_token}` } });
  async function until(client, path, predicate) { let last; for (let i = 0; i < 100; i++) { const r = await client.request(path); last = r.body; if (predicate(r.body)) return r; await pause(10); } assert.fail('operation did not reach expected state: ' + JSON.stringify(last)); }
  const evidence = { resource_id: 'vm-one', private_ipv4: '10.0.0.2', management_network: 'private', placement: 'nova', architecture: 'amd64', initialization: 'preconfigured', ssh_user: 'railshot-runtime', ssh_port: 2223, ssh_host_key: 'ssh-ed25519 ' + 'A'.repeat(68) };
  async function runtime(client, row) {
    await heartbeat(client, row);
    const response = await client.request(`/api/v1/targets/${row.id}/runtimes`, { generation: row.claim.body.generation, evidence }, { headers: { Authorization: `Bearer ${row.claim.body.client_token}` } });
    assert.equal(response.status, 202);
    await until(client, `/api/v1/targets/${row.id}`, (r) => r.runtime_preparation.status === 'succeeded');
  }
  return { directory, calls, client, target, heartbeat, until, runtime, evidence, personalAdapter, appAdapter, product: () => server.productReady, get base() { return base; }, restart: async () => { await stop(); await start(); } };
}

test('current browser owns registration immediately and can query instances as soon as the client is ready without recovery', async (t) => {
  const f = await fixture(t), owner = f.client(), other = f.client();
  assert.equal((await owner.request('/api/v1/owners', {})).status, 201);
  const ownerCookie = owner.cookies.get('railshot_owner');
  await other.request('/api/v1/owners', {});
  const created = await owner.request('/api/v1/targets', { label: '즉시 관리 환경', provider: 'openstack' });
  assert.equal(created.status, 201);
  const id = created.body.id, path = `/api/v1/targets/${id}`;
  assert.equal(created.body.status, 'pending');
  assert.equal((await owner.request(path)).status, 200);
  assert.deepEqual((await owner.request('/api/v1/targets?scope=owned&provider=openstack')).body.items.map((row) => row.id), [id]);
  assert.equal((await other.request(path)).status, 404);
  assert.deepEqual((await other.request('/api/v1/targets?scope=owned&provider=openstack')).body.items, []);
  assert.equal((await other.request(`${path}/enrollments`, {})).status, 404);
  assert.equal((await owner.request(`${path}/instances`)).status, 409, 'ownership precedes control connectivity');
  const enrollment = await owner.request(`${path}/enrollments`, {});
  assert.equal(enrollment.status, 201);
  const token = /RAILSHOT_ENROLLMENT_TOKEN='([^']+)'/.exec(enrollment.body.install_command)[1];
  const claim = await owner.request(`/api/v1/enrollments/${enrollment.body.id}/claims`, {
    public_key: 'A'.repeat(43) + '=', client_version: '1', project_id: 'project-one', runtime: { ssh_host_key: 'ssh-ed25519 ' + 'A'.repeat(68) },
  }, { headers: { Authorization: `Bearer ${token}` } });
  assert.equal(claim.status, 201);
  const connected = await f.heartbeat(owner, { id, claim });
  assert.equal(connected.body.connection_status, 'ready');
  assert.equal(connected.body.status, 'preparing');
  assert.equal(connected.body.deployable, false);
  assert.equal((await other.request(`${path}/instances`)).status, 404);
  assert.equal(f.calls.execute.length, 0, 'unready or foreign requests never reach the customer executor');
  const instances = await owner.request(`${path}/instances`);
  assert.equal(instances.status, 200);
  assert.equal(instances.body.items[0].id, 'server-one');
  assert.deepEqual(f.calls.execute, [{ target_id: id, input: { argv: ['server', 'list'] } }]);
  assert.equal(owner.cookies.get('railshot_owner'), ownerCookie, 'no recovery or owner credential rotation is needed');
});

test('durable owner recovery rotates both capabilities and survives ordinary session expiry/restart', async (t) => {
  const f = await fixture(t), a = f.client(), b = f.client(), row = await f.target(a);
  assert.equal(row.claim.status, 201);
  assert.equal((await b.request(`/api/v1/targets/${row.id}`)).status, 404);
  assert.equal((await b.request('/api/v1/targets?scope=owned&provider=openstack')).body.items.length, 0);
  const previousCookie = new Map(a.cookies);
  const db = new DatabaseSync(join(f.directory, 'dashboard.sqlite3'));
  db.prepare('UPDATE sessions SET expires_at = 0 WHERE expires_at < ?').run(8640000000000000); db.close();
  await f.restart();
  assert.equal((await a.request(`/api/v1/targets/${row.id}`)).status, 200);
  const recovery = await b.request('/api/v1/recoveries', { recovery_key: row.owner.body.recovery_key }); assert.equal(recovery.status, 200);
  assert.notEqual(recovery.body.recovery_key, row.owner.body.recovery_key);
  assert.equal((await a.request(`/api/v1/targets/${row.id}`)).status, 404, 'old owner cookie is revoked');
  assert.equal((await b.request(`/api/v1/targets/${row.id}`)).status, 200);
  assert.equal((await a.request('/api/v1/recoveries', { recovery_key: row.owner.body.recovery_key })).status, 401);
  const bytes = await readFile(join(f.directory, 'dashboard.sqlite3')); assert.ok(!bytes.includes(Buffer.from(row.owner.body.recovery_key)));
  assert.ok(previousCookie.has('railshot_owner'));
});

test('enrollment is single-use and rotates stale credentials without requiring preassigned runtime profiles', async (t) => {
  const f = await fixture(t), a = f.client(); await a.request('/api/v1/owners', {});
  const created = await a.request('/api/v1/targets', { label: '환경' }), id = created.body.id;
  const first = await a.request(`/api/v1/targets/${id}/enrollments`, {}), second = await a.request(`/api/v1/targets/${id}/enrollments`, {});
  const claim = (enrollment, project = 'project-one') => a.request(`/api/v1/enrollments/${enrollment.body.id}/claims`, { public_key: 'A'.repeat(43) + '=', client_version: '1', project_id: project, runtime: {ssh_host_key:'ssh-ed25519 '+'A'.repeat(68)} }, { headers: { Authorization: `Bearer ${/RAILSHOT_ENROLLMENT_TOKEN='([^']+)'/.exec(enrollment.body.install_command)[1]}` } });
  assert.equal((await claim(first)).status, 401);
  assert.equal(f.calls.gateway.length, 0);
  assert.equal((await claim(second)).status, 201); assert.equal((await claim(second)).status, 401);
  assert.equal((await a.request(`/api/v1/targets/${id}/enrollments`, {})).status, 409);
  assert.match(second.body.install_command, /sudo --preserve-env=RAILSHOT_ENROLLMENT_TOKEN bash/);
  assert.match(second.body.install_command, /--personal-registration/);
});

test('public personal readiness blocks enrollment before installer side effects when operator prerequisites are missing', async (t) => {
  const f = await fixture(t), a = f.client();
  f.personalAdapter.readiness = async () => ({ scope: 'personal', ready: false,
    blockers: [{ code: 'RUNTIME_OPERATOR_NOT_CONFIGURED', message: '개인 실행환경 운영 설정이 없습니다.' }] });
  const readiness = await a.request('/api/v1/readiness?scope=personal');
  assert.equal(readiness.status, 200); assert.equal(readiness.body.ready, false);
  assert.equal(readiness.body.blockers[0].code, 'RUNTIME_OPERATOR_NOT_CONFIGURED');
  await a.request('/api/v1/owners', {});
  const created = await a.request('/api/v1/targets', { label: '준비 전 환경' });
  const enrollment = await a.request(`/api/v1/targets/${created.body.id}/enrollments`, {});
  assert.equal(enrollment.status, 409); assert.equal(enrollment.body.error.code, 'RUNTIME_OPERATOR_NOT_CONFIGURED');
  assert.equal(f.calls.gateway.length, 0);
});

test('installation test transport follows operator configuration and cannot be selected by browser input', async (t) => {
  const f = await fixture(t), client = f.client();
  await client.request('/api/v1/owners', {});
  assert.equal((await client.request('/api/v1/targets', { label: 'test', test_allow_http: true })).status, 422);
  const target = await client.request('/api/v1/targets', { label: '시험 환경' });
  const path = `/api/v1/targets/${target.body.id}/enrollments`;
  assert.equal((await client.request(path, { test_allow_http: true })).status, 422);
  assert.equal((await client.request(path, { public_url: 'http://browser.example.test' })).status, 422);
  const standard = await client.request(path, {});
  assert.equal(standard.status, 201);
  assert.match(standard.body.install_command, /--proto '=https'/);
  assert.ok(!standard.body.install_command.includes('--test-allow-http'));
  // Only the trusted adapter can supply this normalized operator setting.
  Object.assign(f.personalAdapter.config, { test_allow_http: true, public_url: 'http://api.example.test',
    installer_url: 'http://release.example.test/install.sh', artifact_url: 'http://release.example.test/client.tgz' });
  const testMode = await client.request(path, {});
  assert.equal(testMode.status, 201);
  assert.match(testMode.body.install_command, /--proto '=http,https'/);
  assert.match(testMode.body.install_command, /--api-url 'http:\/\/api.example.test'/);
  assert.match(testMode.body.install_command, /--artifact-sha256 '[a-f0-9]{64}' --test-allow-http;/);
  assert.match(testMode.body.install_command, /--max-redirs 0/);
  assert.ok(!testMode.body.install_command.includes('--insecure'));
});

test('client assertion alone cannot report deployment readiness and foreign heartbeat cannot mutate', async (t) => {
  const f = await fixture(t), a = f.client(), row = await f.target(a);
  assert.equal((await a.request(`/api/v1/targets/${row.id}`)).body.deployable, false);
  f.personalAdapter.verify = async () => ({ status: 'succeeded', reachable: false });
  assert.equal((await f.heartbeat(a, row)).body.status, 'connecting');
  f.personalAdapter.verify = async () => ({ status: 'succeeded', reachable: true });
  assert.equal((await f.heartbeat(a, row)).body.status, 'connecting', 'gateway alone is not OpenStack control verification');
  f.personalAdapter.verify = async () => ({ status: 'succeeded', reachable: true, openstack_verified: true });
  assert.equal((await f.heartbeat(a, row)).body.status, 'preparing');
  await f.runtime(a, row);
  assert.equal((await f.heartbeat(a, row)).body.status, 'ready');
  const changed = { ...row, claim: { body: { ...row.claim.body } } }; changed.claim.body.client_token = 'Z'.repeat(43);
  assert.equal((await f.heartbeat(a, changed)).status, 401);
});

test('chosen personal target routes deployment and deletes only its RailShot app after explicit data consent', async (t) => {
  const f = await fixture(t), a = f.client(), b = f.client(), row = await f.target(a); await f.runtime(a, row);
  const form = () => { const value = new FormData(); value.set('environment', 'onprem'); value.set('provider', 'openstack'); value.set('target_id', row.id); value.set('source_name', 'hello-app'); value.append('files', new Blob(['console.log(1)']), 'app.js'); value.set('paths', '["app.js"]'); return value; };
  const deployment = await a.request('/api/v1/deployments', undefined, { method: 'POST', body: form(), headers: { 'Idempotency-Key': 'deploy-one' } });
  assert.equal(deployment.status, 202); await f.until(a, `/api/v1/deployments/${deployment.body.resource_id}`, (r) => r.status === 'succeeded');
  const listed = await a.request(`/api/v1/targets/${row.id}/applications`); assert.equal(listed.body.items.length, 1);
  assert.equal(f.calls.deploy[0].target_id, listed.body.items[0].target_id);
  assert.equal((await b.request(`/api/v1/targets/${row.id}/applications`)).status, 404);
  assert.equal((await a.request(`/api/v1/targets/${row.id}/plans`, { action: 'delete', delete_data: false })).status, 422);
  const plan = await a.request(`/api/v1/targets/${row.id}/plans`, { action: 'delete', delete_data: true }); assert.equal(plan.status, 201); assert.deepEqual(plan.body.blockers, []);
  const input = { action: 'delete', delete_data: true, plan_id: plan.body.id, plan_hash: plan.body.plan_hash, confirmation: row.created.body.label };
  const accepted = await a.request(`/api/v1/targets/${row.id}/operations`, input, { headers: { 'Idempotency-Key': 'remove-one' } }); assert.equal(accepted.status, 202);
  assert.equal((await a.request('/api/v1/deployments', undefined, { method: 'POST', body: form(), headers: { 'Idempotency-Key': 'deploy-after-delete' } })).status, 409);
  const operation = await f.until(a, `/api/v1/operations/${accepted.body.id}`, (r) => r.stage === 'client');
  assert.deepEqual(f.calls.revoke, [row.id]);
  assert.equal(f.calls.delete.length, 1); assert.equal(f.calls.delete[0].options.deleteData, true);
  const hb = await f.heartbeat(a, row); assert.deepEqual(hb.body.command.applications, []);
  const receipt = { operation_id: operation.body.id, attempt_id: hb.body.command.attempt_id, generation: row.claim.body.generation, status: 'running', steps: [], residuals: [], client_removed: false };
  assert.equal((await a.request(`/api/v1/targets/${row.id}/receipts`, receipt, { headers: { Authorization: `Bearer ${row.claim.body.client_token}` } })).status, 200);
  assert.equal((await a.request(`/api/v1/targets/${row.id}/receipts`, { ...receipt, status: 'succeeded', client_removed: true }, { headers: { Authorization: `Bearer ${row.claim.body.client_token}` } })).body.status, 'succeeded');
  assert.equal((await a.request(`/api/v1/targets/${row.id}`)).body.status, 'deleted');
  assert.equal((await f.heartbeat(a, row)).status, 401);
  assert.equal((await a.request(`/api/v1/targets/${row.id}/operations`, input, { headers: { 'Idempotency-Key': 'remove-one' } })).body.id, accepted.body.id);
  assert.equal(f.calls.delete.length, 1);
  await f.restart(); assert.equal((await a.request(`/api/v1/targets/${row.id}`)).body.status, 'deleted');
});

test('interrupted client removal remains attention after restart and never repeats removal command', async (t) => {
  const f = await fixture(t), a = f.client(), row = await f.target(a); await f.heartbeat(a, row);
  const plan = await a.request(`/api/v1/targets/${row.id}/plans`, { action: 'delete', delete_data: true });
  const operation = await a.request(`/api/v1/targets/${row.id}/operations`, { action: 'delete', delete_data: true, plan_id: plan.body.id, plan_hash: plan.body.plan_hash, confirmation: row.created.body.label }, { headers: { 'Idempotency-Key': 'remove' } });
  await f.until(a, `/api/v1/operations/${operation.body.id}`, (r) => r.stage === 'client');
  await f.restart();
  assert.equal((await a.request(`/api/v1/targets/${row.id}`)).body.status, 'attention');
  assert.equal((await a.request(`/api/v1/operations/${operation.body.id}`)).body.status, 'unknown');
  assert.equal((await f.heartbeat(a, row)).body.command, null);
});

test('personal mutation requires request header, shared targets cannot be adopted and no runtime is fabricated', async (t) => {
  const f = await fixture(t), a = f.client();
  assert.equal((await a.request('/api/v1/owners', {}, { headers: { 'X-Railshot-Request': '' } })).status, 403);
  await a.request('/api/v1/owners', {});
  assert.equal((await a.request('/api/v1/targets/shared')).status, 404);
  assert.equal((await a.request('/api/v1/targets/shared/enrollments', {})).status, 404);
  assert.equal((await a.request('/api/v1/targets', { label: 'x', target_id: 'shared' })).status, 422);
  assert.equal((await a.request('/api/v1/targets?scope=owned&scope=owned')).status, 422);
  assert.equal((await a.request('/api/v1/targets?scope=owned&provider=aws')).status, 422);
});

test('unconfigured installation stays blocked while an unclaimed registration can be safely removed', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'personal-unconfigured-'));
  const server = createAppServer({ stateDirectory: directory, service: null });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { await new Promise((resolve) => server.close(resolve)); await (await server.productReady).close(); await rm(directory, { recursive: true, force: true }); });
  const base = `http://127.0.0.1:${server.address().port}`, cookies = new Map();
  const request = async (path, body, key) => {
    const response = await fetch(base + path, { method: body === undefined ? 'GET' : 'POST',
      headers: { Cookie: [...cookies].map(([k,v]) => `${k}=${v}`).join('; '), 'X-Railshot-Request': 'dashboard', 'Content-Type': 'application/json', ...(key && { 'Idempotency-Key': key }) },
      ...(body !== undefined && { body: JSON.stringify(body) }) });
    for (const item of response.headers.getSetCookie()) { const [name, value] = item.split(';')[0].split('='); cookies.set(name, value); }
    return { status: response.status, body: await response.json() };
  };
  await request('/api/v1/owners', {});
  const created = await request('/api/v1/targets', { label: '미등록 환경' }), id = created.body.id;
  assert.equal((await request(`/api/v1/targets/${id}/enrollments`, {})).status, 409);
  const plan = await request(`/api/v1/targets/${id}/plans`, { action: 'delete', delete_data: true });
  const accepted = await request(`/api/v1/targets/${id}/operations`, { action: 'delete', delete_data: true, plan_id: plan.body.id, plan_hash: plan.body.plan_hash, confirmation: '미등록 환경' }, 'unclaimed-delete');
  assert.equal(accepted.status, 202);
  for (let i=0;i<30;i++) { const result = await request(`/api/v1/operations/${accepted.body.id}`); if (result.body.status === 'succeeded') break; await pause(10); }
  assert.equal((await request(`/api/v1/targets/${id}`)).body.status, 'deleted');
});

test('host measurements retain their own freshness and incomplete client removal cannot be declared complete', async (t) => {
  const f = await fixture(t), a = f.client(), row = await f.target(a);
  const body = { generation: row.claim.body.generation, client_version: '1', checks: { tunnel: true, openstack: true, runtime: true }, metrics: { cpu_percent: 21, memory_percent: 37, disk_percent: null } };
  const auth = { headers: { Authorization: `Bearer ${row.claim.body.client_token}` } };
  assert.equal((await a.request(`/api/v1/targets/${row.id}/heartbeats`, body, auth)).status, 200);
  const observation = await a.request(`/api/v1/targets/${row.id}/observations`);
  assert.equal(observation.body.metrics.cpu_percent.scope, 'client_host'); assert.equal(observation.body.metrics.cpu_percent.value, 21);
  assert.equal(observation.body.metrics.disk_percent.state, 'not_configured');
  assert.equal((await a.request(`/api/v1/targets/${row.id}/heartbeats`, { ...body, metrics: { cpu_percent: 101 } }, auth)).status, 422);
  const plan = await a.request(`/api/v1/targets/${row.id}/plans`, { action: 'delete', delete_data: true });
  const operation = await a.request(`/api/v1/targets/${row.id}/operations`, { action: 'delete', delete_data: true, plan_id: plan.body.id, plan_hash: plan.body.plan_hash, confirmation: row.created.body.label }, { headers: { 'Idempotency-Key': 'partial' } });
  await f.until(a, `/api/v1/operations/${operation.body.id}`, (r) => r.stage === 'client');
  await a.request(`/api/v1/targets/${row.id}/receipts`, {operation_id:operation.body.id,attempt_id:(await f.heartbeat(a,row)).body.command.attempt_id,generation:row.claim.body.generation,status:'unknown',steps:[],residuals:[{kind:'Client',name:row.id}],client_removed:false},auth);
  assert.equal((await a.request(`/api/v1/targets/${row.id}`)).body.status, 'attention');
  assert.equal((await f.heartbeat(a, row)).body.command, null, 'uncertain client cleanup is never replayed');
});

test('runtime preparation reports client progress, rejects foreign credentials, and only verified binding enables deployments', async (t) => {
  const f = await fixture(t), a = f.client(), row = await f.target(a);
  const path = `/api/v1/targets/${row.id}/runtimes`, auth = { headers: { Authorization: `Bearer ${row.claim.body.client_token}` } };
  assert.equal((await a.request(path)).status, 401, 'owner cookie cannot impersonate runtime client');
  assert.equal((await a.request(path, { generation: 99, evidence: f.evidence }, auth)).status, 401);
  assert.equal((await a.request(path, { generation: 1, progress: { stage: 'client_installation', status: 'succeeded' } }, auth)).status, 422);
  assert.equal((await a.request(path, { generation: 1, progress: { stage: 'client_installation', status: 'running' } }, auth)).status, 202);
  assert.equal((await a.request(path, undefined, auth)).body.runtime_preparation.stage, 'client_installation');
  await f.runtime(a, row);
  const ready = (await a.request(`/api/v1/targets/${row.id}`)).body;
  assert.equal(ready.deployable, true);
  assert.equal(ready.runtime_preparation.client_reported_ready, true);
  await a.request(path, { generation: 1, evidence: f.evidence }, auth);
  assert.equal(f.calls.runtime.length, 1, 'completed binding replays without provisioning');
  assert.equal((await a.request(path, { generation: 1, evidence: { ...f.evidence, resource_id: 'different-vm' } }, auth)).status, 409);
  f.personalAdapter.verifyRuntime = async () => ({ status: 'blocked', stage: 'permission_verification', blockers: ['RUNTIME_DEPLOYMENT_PERMISSION_MISMATCH'] });
  const failed = await f.heartbeat(a, row);
  assert.equal(failed.body.connection_status, 'ready');
  assert.equal(failed.body.deployable, false);
  assert.equal(failed.body.runtime_preparation.stage, 'permission_verification');
});

test('known blocked runtime can retry the same binding but unknown cannot replay after restart', async (t) => {
  const f = await fixture(t), a = f.client(), row = await f.target(a);
  await f.heartbeat(a, row);
  const path = `/api/v1/targets/${row.id}/runtimes`, auth = { headers: { Authorization: `Bearer ${row.claim.body.client_token}` } };
  let calls = 0;
  f.personalAdapter.prepareRuntime = async () => { calls++; return { status: calls === 1 ? 'blocked' : 'unknown', blockers: ['RUNTIME_OPERATOR_NOT_CONFIGURED'] }; };
  await a.request(path, { generation: 1, evidence: f.evidence }, auth);
  await f.until(a, `/api/v1/targets/${row.id}`, (target) => target.runtime_preparation.status === 'blocked');
  await a.request(path, { generation: 1, evidence: f.evidence }, auth);
  await f.until(a, `/api/v1/targets/${row.id}`, (target) => target.runtime_preparation.status === 'unknown');
  await f.restart();
  await f.heartbeat(a, row);
  await a.request(path, { generation: 1, evidence: f.evidence }, auth);
  assert.equal(calls, 2);
  const plan = await a.request(`/api/v1/targets/${row.id}/plans`, { action: 'delete', delete_data: true });
  assert.ok(plan.body.blockers.includes('RUNTIME_RECONCILIATION_REQUIRED'));
});

test('failed runtime revocation never issues the client removal command', async (t) => {
  const f = await fixture(t), a = f.client(), row = await f.target(a);
  await f.runtime(a, row);
  f.personalAdapter.removeRuntime = async () => ({ status: 'unknown', residuals: [{ kind: 'ServiceAccount' }] });
  const plan = await a.request(`/api/v1/targets/${row.id}/plans`, { action: 'delete', delete_data: true });
  const operation = await a.request(`/api/v1/targets/${row.id}/operations`, { action: 'delete', delete_data: true,
    plan_id: plan.body.id, plan_hash: plan.body.plan_hash, confirmation: row.created.body.label }, { headers: { 'Idempotency-Key': 'revoke-failure' } });
  await f.until(a, `/api/v1/operations/${operation.body.id}`, (r) => r.status === 'unknown');
  const heartbeat = await f.heartbeat(a, row);
  assert.equal(heartbeat.body.status, 'attention');
  assert.equal(heartbeat.body.command, null);
});


async function deletionFixture(t) {
  const f=await fixture(t), owner=f.client(), other=f.client(), row=await f.target(owner);
  await other.request('/api/v1/owners',{}); await f.heartbeat(owner,row);
  const path=`/api/v1/targets/${row.id}`, plan=await owner.request(path+'/plans',{action:'delete',delete_data:true});
  const op=await owner.request(path+'/operations',{action:'delete',delete_data:true,plan_id:plan.body.id,plan_hash:plan.body.plan_hash,confirmation:row.created.body.label},{headers:{'Idempotency-Key':'delete-once'}});
  const operation=(await f.until(owner,`/api/v1/operations/${op.body.id}`,(r)=>r.stage==='client')).body;
  const auth={headers:{Authorization:`Bearer ${row.claim.body.client_token}`}};
  const receipt={operation_id:operation.id,attempt_id:operation.active_attempt_id,generation:row.claim.body.generation,status:'unknown',steps:[],residuals:['client'],client_removed:false};
  return {...f,owner,other,row,path,operation,auth,receipt};
}

test('owner explicitly reconciles intact client and resumes same deletion with a fresh attempt; stale responses cannot alter it',async(t)=>{
  const f=await deletionFixture(t), {owner,other,path,row,operation,auth,receipt}=f;
  assert.equal((await owner.request(path+'/receipts',receipt,auth)).body.status,'unknown');
  assert.equal((await other.request(path+'/reconciliations',{operation_id:operation.id})).status,404);
  assert.equal((await owner.request(path+'/reconciliations',{operation_id:operation.id},{headers:{'X-Railshot-Request':''}})).status,403);
  const check=await owner.request(path+'/reconciliations',{operation_id:operation.id});assert.equal(check.status,202);
  assert.equal(check.headers.get('location'), `/api/v1/operations/${operation.id}`);
  assert.equal(check.body.reconciliation.status,'pending');
  const probe=(await f.heartbeat(owner,row)).body.command;assert.equal(probe.kind,'environment.inspect');
  assert.equal(probe.previous_attempt_id,operation.active_attempt_id);
  const resume={action:'resume',operation_id:operation.id,reconciliation_id:probe.reconciliation_id,confirmation:row.created.body.label,delete_data:true};
  const headers={headers:{'Idempotency-Key':'resume-one'}};
  assert.equal((await owner.request(path+'/operations',resume,headers)).status,409,'no browser assertion replaces inspection');
  const inspected={operation_id:operation.id,generation:1,status:'inspected',steps:[],residuals:[],client_removed:false,reconciliation_id:probe.reconciliation_id,
    inspection:{state:'intact',preflight_ok:true,helper_active:false}};
  assert.equal((await owner.request(path+'/receipts',inspected)).status,401);
  assert.equal((await owner.request(path+'/receipts',{...inspected,generation:2},auth)).status,401);
  assert.equal((await owner.request(path+'/receipts',inspected,auth)).status,200);
  assert.equal((await owner.request(path+'/receipts',inspected,auth)).status,200,'identical probe replay is harmless');
  assert.equal((await other.request(path+'/operations',resume,headers)).status,404);
  const resumed=await owner.request(path+'/operations',resume,headers);assert.equal(resumed.status,202);assert.equal(resumed.body.id,operation.id);
  assert.notEqual(resumed.body.active_attempt_id,operation.active_attempt_id);assert.equal(resumed.body.attempts.length,2);
  const command=(await f.heartbeat(owner,row)).body.command;assert.equal(command.reconciliation_id,probe.reconciliation_id);
  assert.equal(command.attempt_id,resumed.body.active_attempt_id);
  assert.equal((await owner.request(path+'/operations',resume,headers)).body.active_attempt_id,command.attempt_id);
  assert.equal((await owner.request(path+'/operations',{...resume,confirmation:'other'},headers)).status,409);
  assert.equal((await owner.request(path+'/receipts',receipt,auth)).status,409,'old failed attempt cannot poison new attempt');
  assert.equal((await owner.request(path+'/receipts',{...receipt,status:'succeeded',client_removed:true,residuals:[]},auth)).status,409);
  const final={...receipt,attempt_id:command.attempt_id,status:'succeeded',client_removed:true,residuals:[]};
  assert.equal((await owner.request(path+'/receipts',final,auth)).body.status,'succeeded');
  assert.equal((await owner.request(path+'/receipts',final,auth)).body.status,'succeeded','lost final acknowledgement can be retried');
  assert.equal((await owner.request(path+'/receipts',{...final,steps:[{name:'changed'}]},auth)).status,401);
  assert.equal((await f.heartbeat(owner,row)).status,401);
  assert.equal((await owner.request('/api/v1/targets?scope=owned&provider=openstack')).body.items.length,0);
  assert.equal((await owner.request(path)).body.status,'deleted');assert.equal((await other.request(path)).status,404);
  await f.restart();assert.equal((await owner.request(path+'/receipts',final,auth)).body.status,'succeeded');
});

test('partial inspection, active removal and blocked preflight never authorize automatic retries',async(t)=>{
  const f=await deletionFixture(t), {owner,path,row,operation,auth,receipt}=f;
  assert.equal((await owner.request(path+'/receipts',{...receipt,status:'blocked',mutation_started:false,error_code:'REMOVAL_PREFLIGHT_FAILED'},auth)).body.status,'blocked');
  assert.equal((await owner.request(path+'/receipts',{...receipt,status:'blocked',mutation_started:false,error_code:'REMOVAL_PREFLIGHT_FAILED'},auth)).body.status,'blocked','same terminal failure receipt is acknowledged without resending deletion');
  assert.equal((await f.heartbeat(owner,row)).body.command,null);
  for(const inspection of [{state:'partial',preflight_ok:true,helper_active:false},{state:'intact',preflight_ok:true,helper_active:true},{state:'intact',preflight_ok:false,helper_active:false}]) {
    const check=(await owner.request(path+'/reconciliations',{operation_id:operation.id})).body.reconciliation;
    await owner.request(path+'/receipts',{operation_id:operation.id,generation:1,status:'inspected',steps:[],residuals:['client'],client_removed:false,reconciliation_id:check.id,inspection},auth);
    const op=(await owner.request(`/api/v1/operations/${operation.id}`)).body;
    assert.equal(op.reconciliation.status,'blocked');assert.equal(op.reconciliation.resumable,false);
    assert.equal((await owner.request(path+'/operations',{action:'resume',operation_id:operation.id,reconciliation_id:check.id,confirmation:row.created.body.label,delete_data:true},{headers:{'Idempotency-Key':check.id}})).status,409);
  }
});

test('interrupted attempt can be inspected after restart but is never resent without explicit confirmation',async(t)=>{
  const f=await deletionFixture(t),{owner,path,row,operation,auth}=f;
  await f.restart();assert.equal((await f.heartbeat(owner,row)).body.command,null);
  const check=(await owner.request(path+'/reconciliations',{operation_id:operation.id})).body.reconciliation;
  await owner.request(path+'/receipts',{operation_id:operation.id,generation:1,status:'inspected',steps:[],residuals:[],client_removed:false,reconciliation_id:check.id,inspection:{state:'intact',preflight_ok:true,helper_active:false}},auth);
  await f.restart();assert.equal((await f.heartbeat(owner,row)).body.command,null);
  const resumed=await owner.request(path+'/operations',{action:'resume',operation_id:operation.id,reconciliation_id:check.id,confirmation:row.created.body.label,delete_data:true},{headers:{'Idempotency-Key':'restart-resume'}});
  assert.equal(resumed.status,202);assert.notEqual(resumed.body.active_attempt_id,operation.active_attempt_id);
});

test('verified client removal survives gateway failure and owner resumes only gateway cleanup',async(t)=>{
  const f=await deletionFixture(t),{owner,path,row,operation,auth,receipt}=f;let removes=0;
  f.personalAdapter.remove=async()=>({status:++removes===1?'unknown':'succeeded'});
  const final={...receipt,status:'succeeded',client_removed:true,residuals:[]};
  assert.equal((await owner.request(path+'/receipts',final,auth)).body.status,'unknown');
  assert.equal((await owner.request(path+'/receipts',final,auth)).body.status,'unknown');assert.equal(removes,1);
  await f.restart();
  const check=(await owner.request(path+'/reconciliations',{operation_id:operation.id})).body.reconciliation;
  assert.equal(check.resumable,true);assert.equal((await f.heartbeat(owner,row)).body.command,null);
  await owner.request(path+'/operations',{action:'resume',operation_id:operation.id,reconciliation_id:check.id,confirmation:row.created.body.label,delete_data:true},{headers:{'Idempotency-Key':'gateway-only'}});
  await f.until(owner,`/api/v1/operations/${operation.id}`,(r)=>r.status==='succeeded');assert.equal(removes,2);
  assert.equal((await owner.request(path+'/receipts',final,auth)).body.status,'succeeded');
});


test('legacy unknown deletion without attempt metadata resumes through inspection, never by rewriting persisted status',async()=>{
  const id='personal-11111111-1111-4111-8111-111111111111', operationId='22222222-2222-4222-8222-222222222222', planId='33333333-3333-4333-8333-333333333333';
  const owner='fixture-owner', token='A'.repeat(43), auth='Bearer '+token;
  let state={applications:{},keys:{},personal:{targets:{[id]:{id,label:'legacy',generation:1,session_id:owner,status:'attention',client_hash:createHash('sha256').update(token).digest('hex'),deletion_operation_id:operationId,
    command:{id:operationId,operation_id:operationId,kind:'environment.delete',applications:[],delete_data:true,generation:1}}},enrollments:{}},
    operations:{[operationId]:{id:operationId,target_id:id,session_id:owner,kind:'target-lifecycle',action:'delete',delete_data:true,plan_id:planId,status:'unknown',stage:'reconciliation',steps:[{name:'client',status:'unknown'},{name:'gateway',status:'unknown'}],error:{code:'REMOVAL_UNVERIFIED'}}},
    plans:{[planId]:{kind:'target-lifecycle',session_id:owner,public:{target_id:id},children:[]}}};
  const store={read:()=>structuredClone(state),transaction:async(fn)=>{const next=structuredClone(state);const result=await fn(next);state=next;return result;}};
  const personal=createPersonalEnvironments({store,adapter:{},launch:()=>assert.fail('inspection/resume must not rerun application workers'),publicApplication:()=>{}});
  const check=(await personal.reconcile(id,{operation_id:operationId},owner)).reconciliation;
  assert.equal(check.status,'pending');assert.equal(state.personal.targets[id].command.previous_attempt_id,undefined);
  await personal.receipt(id,{operation_id:operationId,generation:1,status:'inspected',steps:[],residuals:[],client_removed:false,reconciliation_id:check.id,inspection:{state:'intact',preflight_ok:true,helper_active:false}},auth);
  const input={action:'resume',operation_id:operationId,reconciliation_id:check.id,confirmation:'legacy',delete_data:true};
  const ready=structuredClone(state);
  state.operations[operationId].reconciliation.expires_at='2000-01-01T00:00:00Z';
  await assert.rejects(()=>personal.remove(id,input,'new-attempt',owner),/최신 삭제 재확인/);
  state=structuredClone(ready);state.applications.foreign={id:'foreign',environment_target_id:id,status:'ready',session_id:'other'};
  await assert.rejects(()=>personal.remove(id,input,'new-attempt',owner),/최신 삭제 재확인/);
  state=structuredClone(ready);
  const result=await personal.remove(id,input,'new-attempt',owner);
  assert.equal(result.id,operationId);assert.equal(result.attempts[0].id,'legacy');assert.equal(result.attempts[0].status,'unknown');
  assert.match(result.active_attempt_id,/^[a-f0-9-]{36}$/);
  await assert.rejects(()=>personal.receipt(id,{operation_id:operationId,generation:1,status:'unknown',steps:[],residuals:['client'],client_removed:false},auth),/삭제 시도/);
});


test('a late proven success for the same attempt can close uncertainty before any resumed attempt exists',async(t)=>{
  const f=await deletionFixture(t),{owner,path,row,operation,auth,receipt}=f;
  await owner.request(path+'/receipts',receipt,auth);
  const check=await owner.request(path+'/reconciliations',{operation_id:operation.id});assert.equal(check.body.reconciliation.status,'pending');
  const final={...receipt,status:'succeeded',client_removed:true,residuals:[]};
  assert.equal((await owner.request(path+'/receipts',final,auth)).body.status,'succeeded');
  assert.equal((await owner.request(path)).body.status,'deleted');
  assert.equal((await owner.request(path+'/operations',{action:'resume',operation_id:operation.id,reconciliation_id:check.body.reconciliation.id,confirmation:row.created.body.label,delete_data:true},{headers:{'Idempotency-Key':'late-resume'}})).status,409);
});


function selectedPersonalSource(targetId, expectedTargetId) {
  const form = new FormData();
  form.set('environment', 'onprem'); form.set('provider', 'openstack');
  form.set('target_id', targetId); form.set('source_name', 'merged-personal-app');
  if (expectedTargetId !== undefined) form.set('expected_target_id', expectedTargetId);
  form.append('files', new Blob(['console.log(1)']), 'app.js'); form.set('paths', '["app.js"]');
  return form;
}

test('modular upload preserves personal ownership and the reviewed target precondition', async (t) => {
  const f = await fixture(t), owner = f.client(), stranger = f.client(), row = await f.target(owner);
  await f.runtime(owner, row);
  const query = new URLSearchParams({ environment: 'onprem', provider: 'openstack', app: 'merged-personal-app', target_id: row.id });
  const resolved = await owner.request(`/api/v1/applications/resolve?${query}`);
  assert.equal(resolved.status, 200);
  assert.equal(resolved.body.environment_target_id, row.id);
  assert.equal((await stranger.request(`/api/v1/applications/resolve?${query}`)).status, 404);
  query.delete('target_id'); query.set('targetId', row.id); query.set('registerNew', 'false');
  assert.equal((await owner.request(`/api/v1/applications/resolve?${query}`)).body.environment_target_id, row.id);
  assert.equal((await stranger.request(`/api/v1/applications/resolve?${query}`)).status, 404);
  for (const extra of ['&target_id=other', '&targetId=other', '&registerNew=false', '&unexpected=1'])
    assert.equal((await owner.request(`/api/v1/applications/resolve?${query}${extra}`)).status, 422);
  query.set('registerNew', 'true');
  assert.equal((await owner.request(`/api/v1/applications/resolve?${query}`)).status, 422);
  const submit = (client, expected, key) => client.request('/api/v1/deployments', undefined,
    { method: 'POST', body: selectedPersonalSource(row.id, expected), headers: { 'Idempotency-Key': key } });
  const stale = await submit(owner, 'different-target', 'review-stale');
  assert.equal(stale.status, 409); assert.equal(stale.body.error.code, 'DEPLOYMENT_TARGET_CHANGED');
  assert.equal((await submit(stranger, row.id, 'foreign-review')).status, 404);
  assert.equal(f.calls.deploy.length, 0);
  const accepted = await submit(owner, row.id, 'review-correct');
  assert.equal(accepted.status, 202);
  await f.until(owner, `/api/v1/deployments/${accepted.body.resource_id}`, (value) => value.status === 'succeeded');
  assert.equal(f.calls.deploy.length, 1);
});

test('application resolution binds snake_case personal target selection to its current owner', async (t) => {
  const f = await fixture(t), owner = f.client(), stranger = f.client(), row = await f.target(owner);
  await f.runtime(owner, row);
  const query = new URLSearchParams({ environment: 'onprem', provider: 'openstack', app: 'resolved-app', target_id: row.id });
  const resolved = await owner.request(`/api/v1/applications/resolve?${query}`);
  assert.equal(resolved.status, 200);
  assert.deepEqual(resolved.body, { app: 'resolved-app', environment_target_id: row.id, application: null });
  assert.equal((await stranger.request(`/api/v1/applications/resolve?${query}`)).status, 404);
  const camelCase = new URLSearchParams({ environment: 'onprem', provider: 'openstack', app: 'resolved-app', targetId: row.id });
  const compatible = await owner.request(`/api/v1/applications/resolve?${camelCase}`);
  assert.equal(compatible.status, 200);
  assert.deepEqual(compatible.body, resolved.body);
  assert.equal((await stranger.request(`/api/v1/applications/resolve?${camelCase}`)).status, 404);
});

test('owner runtime reconciliation points to the readable target resource', async (t) => {
  const f = await fixture(t), owner = f.client(), row = await f.target(owner);
  await f.runtime(owner, row);
  const reconciled = await owner.request(`/api/v1/targets/${row.id}/reconciliations`, { scope: 'runtime' });
  assert.equal(reconciled.status, 202);
  assert.equal(reconciled.body.target_id, row.id);
  assert.equal(reconciled.headers.get('location'), `/api/v1/targets/${row.id}`);
  assert.equal(reconciled.headers.get('retry-after'), '2');
});

test('queued personal deployment rechecks connectivity before registration or CI dispatch', async (t) => {
  const f = await fixture(t), owner = f.client(), row = await f.target(owner);
  await f.runtime(owner, row);
  const product = await f.product();
  assert.equal(product.pauseForRelease(), true);
  const accepted = await owner.request('/api/v1/deployments', undefined,
    { method: 'POST', body: selectedPersonalSource(row.id, row.id), headers: { 'Idempotency-Key': 'queued-personal' } });
  assert.equal(accepted.status, 202);
  assert.equal((await owner.request(`/api/v1/deployments/${accepted.body.resource_id}`)).body.status, 'queued');
  f.personalAdapter.verify = async () => ({ status: 'succeeded', reachable: false });
  await f.heartbeat(owner, row);
  product.resumeAfterRelease();
  const stopped = await f.until(owner, `/api/v1/deployments/${accepted.body.resource_id}`, (value) => value.status === 'blocked');
  assert.equal(stopped.body.error.code, 'TARGET_UNAVAILABLE');
  assert.equal(stopped.body.error.outcome_unknown, false);
  assert.equal(f.calls.deploy.length, 0);
  const apps = await owner.request(`/api/v1/targets/${row.id}/applications`);
  assert.equal(apps.body.items[0].status, 'queued', 'native registration never starts');
});

test('owner dashboard preferences and upstream registration tokens coexist across restart and recovery', async (t) => {
  const f = await fixture(t), oldBrowser = f.client(), recoveredBrowser = f.client(), row = await f.target(oldBrowser);
  assert.equal((await oldBrowser.request('/api/v1/preferences', { view: 'personal', environment: 'onprem', provider: 'openstack' }, { method: 'PUT' })).status, 200);
  const product = await f.product();
  const identity = product.dashboard.owner(oldBrowser.cookies.get('railshot_owner'));
  const registration = product.registrations.create(identity.session_id, { provider: 'openstack' });
  await f.restart();
  const restarted = await f.product();
  assert.equal(restarted.registrations.get(identity.session_id, registration.id).status, 'pending');
  assert.equal(restarted.registrations.activeToken(registration.linkage_token), true);
  const recovery = await recoveredBrowser.request('/api/v1/recoveries', { recovery_key: row.owner.body.recovery_key });
  assert.equal(recovery.status, 200);
  assert.equal((await recoveredBrowser.request(`/api/v1/targets/${row.id}`)).status, 200);
  assert.deepEqual((await recoveredBrowser.request('/api/v1/preferences')).body,
    { view: 'personal', environment: 'onprem', provider: 'openstack' });
  assert.equal((await oldBrowser.request('/api/v1/preferences')).body.view, 'deploy');
  assert.equal(restarted.registrations.claim(registration.linkage_token).registration_id, registration.id);
  assert.equal(restarted.registrations.claim(registration.linkage_token), null);
});


test('owner runtime reconciliation recovers a completed unknown binding without preparing again', async (t) => {
  const f = await fixture(t), owner = f.client(), foreign = f.client(), row = await f.target(owner);
  const path = `/api/v1/targets/${row.id}`, auth = { headers: { Authorization: `Bearer ${row.claim.body.client_token}` } };
  await f.heartbeat(owner, row);
  const prepare = f.personalAdapter.prepareRuntime;
  f.personalAdapter.prepareRuntime = async (target) => { await prepare(target); return { status: 'unknown', blockers: ['RUNTIME_PREPARATION_UNVERIFIED'] }; };
  await owner.request(path + '/runtimes', { generation: 1, evidence: f.evidence }, auth);
  await f.until(owner, path, (r) => r.runtime_preparation.status === 'unknown');
  await f.restart();
  let inspected = 0;
  f.personalAdapter.reconcileRuntime = async () => { inspected++; return f.personalAdapter.verifyRuntime(); };
  await foreign.request('/api/v1/owners', {});
  assert.equal((await foreign.request(path + '/reconciliations', { scope: 'runtime' })).status, 404);
  const accepted = await owner.request(path + '/reconciliations', { scope: 'runtime' });
  assert.equal(accepted.status, 202); assert.equal(accepted.body.target_id, row.id);
  await f.until(owner, path, (r) => r.runtime_preparation.status === 'succeeded');
  assert.equal(inspected, 1); assert.equal(f.calls.runtime.length, 1);
});

test('client resumes preparation only after read-only reconciliation proves it safe', async (t) => {
  const f = await fixture(t), owner = f.client(), row = await f.target(owner);
  const path = `/api/v1/targets/${row.id}`, auth = { headers: { Authorization: `Bearer ${row.claim.body.client_token}` } };
  await f.heartbeat(owner, row);
  const prepare = f.personalAdapter.prepareRuntime;
  f.personalAdapter.prepareRuntime = async () => ({ status: 'unknown', blockers: ['RUNTIME_PREPARATION_UNVERIFIED'] });
  await owner.request(path + '/runtimes', { generation: 1, evidence: f.evidence }, auth);
  await f.until(owner, path, (r) => r.runtime_preparation.status === 'unknown');
  assert.equal((await owner.request(path + '/runtimes', { generation: 1, action: 'resume', evidence: f.evidence }, auth)).status, 409);
  f.personalAdapter.reconcileRuntime = async () => ({ status: 'blocked', stage: 'registration', blockers: [], resumable: true });
  await owner.request(path + '/runtimes', { generation: 1, action: 'reconcile' }, auth);
  await f.until(owner, path, (r) => r.runtime_preparation.resumable === true);
  f.personalAdapter.prepareRuntime = prepare;
  assert.equal((await owner.request(path + '/runtimes', { generation: 1, action: 'resume', evidence: f.evidence }, auth)).status, 202);
  await f.until(owner, path, (r) => r.runtime_preparation.status === 'succeeded');
  assert.equal(f.calls.runtime.length, 1);
});

test('initial runtime deletion failure reconciles and resumes before issuing client removal', async (t) => {
  const f = await fixture(t), owner = f.client(), row = await f.target(owner); await f.runtime(owner, row);
  const path = `/api/v1/targets/${row.id}`;
  let removed = 0, resumed = 0, inspected = 0;
  f.personalAdapter.removeRuntime = async () => { removed++; return { status: 'unknown', residuals: [{ kind: 'Role' }] }; };
  f.personalAdapter.inspectRuntimeRemoval = async () => { inspected++; return { status: 'blocked', resumable: true }; };
  f.personalAdapter.resumeRuntimeRemoval = async () => { resumed++; return { status: 'succeeded', residuals: [], revocation_verified: true }; };
  const plan = (await owner.request(path + '/plans', { action: 'delete', delete_data: true })).body;
  const operation = (await owner.request(path + '/operations', { action: 'delete', delete_data: true, plan_id: plan.id, plan_hash: plan.plan_hash, confirmation: row.created.body.label }, { headers: { 'Idempotency-Key': 'partial-runtime' } })).body;
  await f.until(owner, `/api/v1/operations/${operation.id}`, (r) => r.status === 'unknown');
  await f.restart();
  assert.equal((await f.heartbeat(owner, row)).body.command, null);
  assert.equal((await owner.request(path + '/reconciliations', { operation_id: operation.id })).status, 202);
  const checked = (await f.until(owner, `/api/v1/operations/${operation.id}`, (r) => r.reconciliation?.status === 'ready')).body;
  assert.equal(inspected, 1); assert.equal(resumed, 0); assert.equal(checked.reconciliation.phase, 'services');
  assert.equal('children' in checked.reconciliation, false);
  const input = { action: 'resume', operation_id: operation.id, reconciliation_id: checked.reconciliation.id, confirmation: row.created.body.label, delete_data: true };
  assert.equal((await owner.request(path + '/operations', input, { headers: { 'Idempotency-Key': 'resume-runtime' } })).status, 202);
  await f.until(owner, `/api/v1/operations/${operation.id}`, (r) => r.stage === 'client');
  assert.equal(removed, 1); assert.equal(resumed, 1);
});

test('partial application deletion is read back then resumed while unresolved ownership stays blocked', async (t) => {
  const f = await fixture(t), owner = f.client(), row = await f.target(owner); await f.runtime(owner, row);
  const deployment = await owner.request('/api/v1/deployments', undefined, { method: 'POST', body: selectedPersonalSource(row.id, row.id), headers: { 'Idempotency-Key': 'app-to-remove' } });
  await f.until(owner, `/api/v1/deployments/${deployment.body.resource_id}`, (r) => r.status === 'succeeded');
  const path = `/api/v1/targets/${row.id}`;
  f.appAdapter.applyLifecycle = async () => ({ status: 'unknown', residuals: [{ kind: 'Namespace' }] });
  let resumable = false, resumed = 0;
  f.appAdapter.reconcileLifecycle = async () => ({ status: 'blocked', resumable, resume_mode: 'existing', residuals: [{ kind: 'Namespace' }] });
  f.appAdapter.resumeLifecycle = async () => { resumed++; return { status: 'succeeded', residuals: [], steps: [] }; };
  const plan = (await owner.request(path + '/plans', { action: 'delete', delete_data: true })).body;
  const operation = (await owner.request(path + '/operations', { action: 'delete', delete_data: true, plan_id: plan.id, plan_hash: plan.plan_hash, confirmation: row.created.body.label }, { headers: { 'Idempotency-Key': 'partial-app' } })).body;
  await f.until(owner, `/api/v1/operations/${operation.id}`, (r) => r.status === 'unknown');
  await owner.request(path + '/reconciliations', { operation_id: operation.id });
  await f.until(owner, `/api/v1/operations/${operation.id}`, (r) => r.reconciliation?.status === 'blocked');
  assert.equal(resumed, 0); assert.equal(f.calls.revoke.length, 0);
  resumable = true;
  await owner.request(path + '/reconciliations', { operation_id: operation.id });
  const checked = (await f.until(owner, `/api/v1/operations/${operation.id}`, (r) => r.reconciliation?.status === 'ready')).body;
  await owner.request(path + '/operations', { action: 'resume', operation_id: operation.id, reconciliation_id: checked.reconciliation.id, confirmation: row.created.body.label, delete_data: true }, { headers: { 'Idempotency-Key': 'resume-app' } });
  await f.until(owner, `/api/v1/operations/${operation.id}`, (r) => r.stage === 'client');
  assert.equal(resumed, 1); assert.equal(f.calls.revoke.length, 1);
});
