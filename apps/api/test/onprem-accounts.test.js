import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { DatabaseSync } from 'node:sqlite';
import { createHash } from 'node:crypto';
import { createAccountData } from '../src/accounts.js';
import { createPersonalEnvironments } from '../src/personal-environments.js';
import { createAppServer } from '../src/server.js';
import { apiAccessConfig } from '../src/access.js';

async function setup(t) {
  const db = new DatabaseSync(':memory:');
  db.exec(await readFile(new URL('../src/dashboard-schema.sql', import.meta.url), 'utf8'));
  t.after(() => db.close());
  const mail = [], accounts = createAccountData(db, async (message) => { mail.push(message); });
  const state = { applications: {}, operations: {}, plans: {}, personal: { targets: {}, enrollments: {} } };
  const store = { read: () => state, transaction: async (mutation) => mutation(state) };
  const adapter = { config: { public_url: 'https://api.example.test', artifact_url: 'https://releases.example.test/pinned.tgz', artifact_sha256: 'a'.repeat(64) },
    connectionReadiness: async () => ({ ready: true, blockers: [] }),
    readiness: async () => ({ ready: false, blockers: [{ code: 'RUNTIME_OPERATOR_NOT_CONFIGURED' }] }),
    register: async () => ({ status: 'succeeded', tunnel: { address: '10.80.0.2/32', allowed_ips: '10.80.0.1/32', endpoint: 'wg.example.test:51820', server_public_key: 'B'.repeat(43) + '=' } }),
    prepare: async () => false,
    runtimeAccess: () => ({ public_key: 'ssh-ed25519 ' + 'B'.repeat(68) }),
    verify: async () => ({ status: 'succeeded', reachable: true, server_list_verified: false, openstack_verified: false }),
    vaultPublicKey: () => ({ vault_public_key: 'PEM', vault_key_id: 'a'.repeat(64) }),
    validateVault: async (input) => input.key_id === 'a'.repeat(64),
  };
  const personal = createPersonalEnvironments({ store, adapter, launch: () => {}, publicApplication: () => {} });
  const product = { dashboard: { accounts, owner: () => null, session: () => null }, personal };
  const server = createAppServer({ product, access: apiAccessConfig({ RAILSHOT_API_TOKEN: 'x'.repeat(32), RAILSHOT_ALLOWED_HOSTS: '127.0.0.1' }) });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { await new Promise((resolve) => server.close(resolve)); });
  const base = `http://127.0.0.1:${server.address().port}`;
  const client = () => {
    let cookie = '', csrf = '';
    return { async request(path, body, options = {}) {
      const response = await fetch(base + path, { method: options.method || (body === undefined ? 'GET' : 'POST'),
        headers: { Cookie: cookie, 'X-Railshot-Request': 'dashboard', ...(csrf ? { 'X-Railshot-CSRF': csrf } : {}),
          ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
        ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });
      const set = response.headers.get('set-cookie'); if (set) cookie = set.split(';')[0];
      const content = response.headers.get('content-type') || '';
      const data = content.includes('json') ? await response.json() : await response.text();
      if (data?.csrf_token) csrf = data.csrf_token;
      return { status: response.status, body: data, headers: response.headers };
    }, setCsrf(value) { csrf = value; } };
  };
  return { db, accounts, mail, personal, state, client, adapter };
}

async function registerAndLogin(f, client, email) {
  assert.equal((await client.request('/api/v1/accounts', { email, password: 'correct horse battery staple' })).status, 202);
  assert.equal((await client.request('/api/v1/accounts/verifications', { token: f.mail.at(-1).token })).status, 200);
  const login = await client.request('/api/v1/logins', { email, password: 'correct horse battery staple' });
  assert.equal(login.status, 201); return login.body;
}

test('account sessions isolate new targets and require CSRF for mutations', async (t) => {
  const f = await setup(t), a = f.client(), b = f.client();
  const owner = await registerAndLogin(f, a, 'a@example.test');
  assert.equal(f.db.prepare('SELECT password_hash FROM accounts WHERE id = ?').get(owner.account_id).password_hash.includes('correct horse'), false);
  await registerAndLogin(f, b, 'b@example.test');
  const created = await a.request('/api/v1/targets', { label: 'site A' }); assert.equal(created.status, 201);
  assert.equal(f.state.personal.targets[created.body.id].account_id, owner.account_id);
  assert.equal((await b.request(`/api/v1/targets/${created.body.id}`)).status, 404);
  assert.deepEqual((await b.request('/api/v1/targets?scope=owned&provider=openstack')).body.items, []);
  assert.equal((await b.request(`/api/v1/targets/${created.body.id}/enrollments`, {})).status, 404);
  a.setCsrf('bad');
  assert.equal((await a.request('/api/v1/targets', { label: 'blocked' })).status, 403);
  assert.equal((await a.request('/api/v1/logins', undefined, { method: 'DELETE' })).status, 403);
});

test('wrong generation and failed peer mutation cannot reuse enrollment token', async (t) => {
  const f = await setup(t), a = f.client();
  await registerAndLogin(f, a, 'peer@example.test');
  const id = (await a.request('/api/v1/targets', { label: 'site' })).body.id;
  const claim = (row) => a.request(`/api/v1/enrollments/${row.body.id}/claims`, { public_key: 'A'.repeat(43) + '=', client_version: '1.0', runtime: { ssh_host_key: 'ssh-ed25519 ' + 'A'.repeat(68) } },
    { headers: { Authorization: `Bearer ${row.body.token}` } });
  const outdated = await a.request(`/api/v1/targets/${id}/enrollments`, {});
  f.state.personal.targets[id].generation += 1;
  assert.equal((await claim(outdated)).status, 409);
  const next = await a.request(`/api/v1/targets/${id}/enrollments`, {});
  f.adapter.register = async () => { throw new Error('peer result unknown'); };
  assert.equal((await claim(next)).status, 503);
  assert.equal(f.state.personal.targets[id].status, 'attention');
  assert.equal((await claim(next)).status, 401);
  assert.notEqual(f.state.personal.targets[id].connection_status, 'ready');
});

test('30-minute token expires, rotates, and cannot be replayed; vault binds project after claim', async (t) => {
  const f = await setup(t), a = f.client();
  const account = await registerAndLogin(f, a, 'owner@example.test');
  const id = (await a.request('/api/v1/targets', { label: 'site' })).body.id;
  const readiness = await a.request('/api/v1/readiness?scope=personal&phase=connection');
  assert.equal(readiness.body.ready, true);
  assert.equal((await a.request('/api/v1/readiness?scope=personal')).body.ready, false);
  const first = await a.request(`/api/v1/targets/${id}/enrollments`, {});
  assert.equal(first.status, 201);
  assert.equal(Date.parse(first.body.expires_at) - Date.now() > 29 * 60000, true);
  assert.equal(first.body.script_url, 'https://api.example.test/api/v1/installers/personal');
  assert.equal(first.body.script_url.includes(first.body.token), false);
  const script = await a.request('/api/v1/installers/personal');
  assert.equal(script.status, 200);
  assert.equal(createHash('sha256').update(script.body).digest('hex'), first.body.script_sha256);
  assert.equal(script.body.includes(first.body.token), false);
  const second = await a.request(`/api/v1/targets/${id}/enrollments`, {});
  const claim = (row) => a.request(`/api/v1/enrollments/${row.body.id}/claims`, { public_key: 'A'.repeat(43) + '=', client_version: '1.0', runtime: { ssh_host_key: 'ssh-ed25519 ' + 'A'.repeat(68) } },
    { headers: { Authorization: `Bearer ${row.body.token}` } });
  assert.equal((await claim(first)).status, 401);
  f.state.personal.enrollments[second.body.id].expires_at = new Date(Date.now() - 1).toISOString();
  assert.equal((await claim(second)).status, 401);
  const third = await a.request(`/api/v1/targets/${id}/enrollments`, {});
  const connected = await claim(third);
  assert.equal(connected.status, 201);
  assert.equal(connected.body.vault_key_id, 'a'.repeat(64));
  assert.ok(connected.body.runtime_access);
  assert.equal((await claim(third)).status, 401);
  const vault = await a.request(`/api/v1/targets/${id}/vaults`, { generation: connected.body.generation, project_id: 'project-one', key_id: 'a'.repeat(64), wrapped_dek: 'QUJDRA==' },
    { headers: { Authorization: `Bearer ${connected.body.client_token}` } });
  assert.equal(vault.status, 201);
  assert.equal(f.state.personal.targets[id].account_id, account.account_id);
  assert.equal(f.state.personal.targets[id].vault.project_id, 'project-one');
  const heartbeat = () => a.request(`/api/v1/targets/${id}/heartbeats`, { generation: connected.body.generation, client_version: '1.0', checks: { tunnel: true, openstack: false } },
    { headers: { Authorization: `Bearer ${connected.body.client_token}` } });
  assert.equal((await heartbeat()).body.connection_status, 'connecting');
  f.adapter.verify = async () => ({ status: 'succeeded', reachable: true, server_list_verified: true, openstack_verified: true });
  const ready = await heartbeat();
  assert.equal(ready.body.connection_status, 'ready');
  assert.equal(ready.body.deployable, false);
  assert.equal((await a.request(`/api/v1/targets/${id}/vaults`, { generation: connected.body.generation, project_id: 'project-one', key_id: 'a'.repeat(64), wrapped_dek: 'QUJDRA==' },
    { headers: { Authorization: `Bearer ${connected.body.client_token}` } })).status, 201);
  assert.equal((await a.request(`/api/v1/targets/${id}/vaults`, { generation: connected.body.generation, project_id: 'project-two', key_id: 'a'.repeat(64), wrapped_dek: 'QUJDRA==' },
    { headers: { Authorization: `Bearer ${connected.body.client_token}` } })).status, 409);
});

test('password reset invalidates login sessions and is single-use', async (t) => {
  const f = await setup(t), a = f.client();
  await registerAndLogin(f, a, 'reset@example.test');
  assert.equal((await a.request('/api/v1/accounts/resets', { email: 'reset@example.test' })).status, 202);
  const token = f.mail.at(-1).token;
  assert.equal((await a.request('/api/v1/accounts/password', { token, password: 'a different long password' })).status, 200);
  assert.equal((await a.request('/api/v1/accounts/password', { token, password: 'a different long password' })).status, 401);
  assert.equal((await a.request('/api/v1/logins')).status, 401);
  assert.equal((await a.request('/api/v1/logins', { email: 'reset@example.test', password: 'a different long password' })).status, 201);
});
