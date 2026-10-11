import test from 'node:test';
import assert from 'node:assert/strict';
import { request } from 'node:http';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { apiAccessConfig, readApiToken } from '../src/access.js';
import { createAppServer } from '../src/server.js';
import { deployRepository, getRun } from '../src/client.js';

const token = 'test-only-operator-token-0123456789abcdef';
const remoteEnv = {
  RAILSHOT_BIND_HOST: '0.0.0.0',
  RAILSHOT_ALLOWED_HOSTS: '127.0.0.1,railshot-api,console.example.test',
  RAILSHOT_ALLOWED_ORIGINS: 'https://console.example.test',
  RAILSHOT_API_TOKEN: token,
};

function http(server, path, headers = {}, method = 'GET') {
  return new Promise((resolve, reject) => {
    const req = request({ hostname: '127.0.0.1', port: server.address().port, path, method, headers }, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { body += chunk; });
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body }));
    });
    req.on('error', reject);
    req.end();
  });
}

async function serving(access, check) {
  const calls = [];
  const service = {
    targetId: 'private-target',
    status: async (id) => { calls.push(['status', id]); return { run_id: Number(id), state: 'queued' }; },
    deploy: async (input) => { calls.push(['deploy', input.app]); return { run_id: 123, state: 'queued' }; },
  };
  // Isolate access checks from worker polling/storage; product.test.js verifies real run binding.
  const product = {
    dashboard: { session: () => ({ id: 'access-test', expires_at: '2099-01-01T00:00:00Z' }) },
    createBuild: async (input, loadSource) => service.deploy(input.files ? input : { ...input, ...await loadSource(input.repository_url) }),
    legacyStatus: service.status,
  };
  const server = createAppServer({ access, service, product,
    sourceLoader: async () => ({ files: [{ path: 'index.js', content: Buffer.from('example') }] }) });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try { await check(server, calls); }
  finally { await new Promise((resolve) => server.close(resolve)); }
}

test('container access fails closed without explicit hosts and an operator token', () => {
  assert.equal(apiAccessConfig({}).bindHost, '127.0.0.1');
  assert.throws(() => apiAccessConfig({ RAILSHOT_BIND_HOST: '0.0.0.0' }), /requires/);
  assert.throws(() => apiAccessConfig({ RAILSHOT_ALLOWED_HOSTS: 'console.example.test' }), /requires/);
  assert.throws(() => apiAccessConfig({ ...remoteEnv, RAILSHOT_ALLOWED_HOSTS: '*' }), /exact hostnames/);
  assert.throws(() => apiAccessConfig({ ...remoteEnv, RAILSHOT_ALLOWED_ORIGINS: 'http://console.example.test' }), /HTTPS/);
  assert.throws(() => apiAccessConfig({ ...remoteEnv, RAILSHOT_ALLOWED_ORIGINS: 'https://console.example.test/path' }), /HTTPS/);
  assert.throws(() => apiAccessConfig({ ...remoteEnv, RAILSHOT_API_TOKEN: 'short' }), /32-4096/);
});

test('HTTP enforces exact Host, Origin and Bearer before accessing deployment service', async () => {
  await serving(apiAccessConfig(remoteEnv), async (server, calls) => {
    const auth = { authorization: `Bearer ${token}` };
    assert.equal((await http(server, '/api/runs/123', { host: 'attacker.test', 'x-forwarded-host': 'console.example.test', ...auth })).status, 403);
    assert.equal((await http(server, '/api/runs/123', { host: 'console.example.test', origin: 'https://attacker.test', ...auth })).status, 403);
    const unauthorized = await http(server, '/api/runs/123', { host: 'railshot-api', 'x-jasmin-request': 'deploy' });
    assert.equal(unauthorized.status, 401);
    assert.equal(unauthorized.headers['www-authenticate'], 'Bearer');
    assert.equal((await http(server, '/api/runs/123', { host: 'railshot-api', authorization: `Bearer ${token}wrong` })).status, 401);
    assert.equal((await http(server, '/api/deploy', { host: 'railshot-api', 'x-jasmin-request': 'deploy' }, 'POST')).status, 401);
    assert.equal((await http(server, 'http://[invalid', { host: 'railshot-api', ...auth })).status, 400);
    assert.equal(calls.length, 0);
    const health = await http(server, '/healthz', { host: 'railshot-api' });
    assert.equal(health.status, 200);
    assert.deepEqual(JSON.parse(health.body), { ok: true, configured: true });
    const accepted = await http(server, '/api/runs/123', { host: 'console.example.test', origin: 'https://console.example.test', ...auth });
    assert.equal(accepted.status, 200);
    assert.equal(accepted.headers['access-control-allow-origin'], undefined);
    assert.equal((await http(server, '/api/deploy', { host: 'railshot-api', ...auth }, 'POST')).status, 403);
    assert.equal((await http(server, '/api/deploy', { host: 'railshot-api', ...auth,
      'x-railshot-request': 'invalid', 'x-jasmin-request': 'deploy' }, 'POST')).status, 403);
    assert.deepEqual(calls, [['status', '123']]);
  });
});

test('client uses Railshot configuration first and retains legacy URL/header compatibility', async () => {
  const keys = ['RAILSHOT_API_URL', 'JASMIN_API_URL'];
  const previous = Object.fromEntries(keys.map((key) => [key, process.env[key]]));
  const fetchBefore = globalThis.fetch;
  const observed = [];
  try {
    globalThis.fetch = async (url, options) => {
      observed.push({ url: String(url), headers: options.headers });
      return Response.json({ run_id: 123 });
    };
    process.env.JASMIN_API_URL = 'https://legacy.example';
    delete process.env.RAILSHOT_API_URL;
    const legacy = await import('../src/client.js?legacy-configuration-test');
    await legacy.deployRepository({ app: 'demo-app', repositoryUrl: 'https://github.com/example/demo' });
    process.env.RAILSHOT_API_URL = 'https://railshot.example';
    const canonical = await import('../src/client.js?canonical-configuration-test');
    await canonical.deployRepository({ app: 'demo-app', repositoryUrl: 'https://github.com/example/demo' });
    assert.deepEqual(observed.map((call) => call.url), ['https://legacy.example/api/deploy', 'https://railshot.example/api/deploy']);
    for (const call of observed) {
      assert.equal(call.headers['x-railshot-request'], 'deploy');
      assert.equal(call.headers['x-jasmin-request'], 'deploy');
    }
  } finally {
    globalThis.fetch = fetchBefore;
    for (const key of keys) {
      if (previous[key] === undefined) delete process.env[key]; else process.env[key] = previous[key];
    }
  }
});

test('container API rejects browser origins unless explicitly enabled', async () => {
  const env = { ...remoteEnv };
  delete env.RAILSHOT_ALLOWED_ORIGINS;
  await serving(apiAccessConfig(env), async (server) => {
    assert.equal((await http(server, '/healthz', { origin: 'http://localhost:4181' })).status, 403);
    assert.equal((await http(server, '/healthz')).status, 200);
  });
});

test('CLI and stdio MCP client authenticate from the mounted token file for deploy and status', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-token-'));
  const path = join(directory, 'api-token');
  const previousToken = process.env.RAILSHOT_API_TOKEN;
  const previousFile = process.env.RAILSHOT_API_TOKEN_FILE;
  try {
    await writeFile(path, `${token}\n`, { mode: 0o600 });
    assert.throws(() => readApiToken({ RAILSHOT_API_TOKEN: token, RAILSHOT_API_TOKEN_FILE: path }), /only one/);
    assert.throws(() => readApiToken({ RAILSHOT_API_TOKEN_FILE: join(directory, 'missing') }), /cannot be read/);
    delete process.env.RAILSHOT_API_TOKEN;
    process.env.RAILSHOT_API_TOKEN_FILE = path;
    const env = { ...remoteEnv, RAILSHOT_API_TOKEN_FILE: path };
    delete env.RAILSHOT_API_TOKEN;
    await serving(apiAccessConfig(env), async (server, calls) => {
      const baseUrl = `http://127.0.0.1:${server.address().port}`;
      assert.equal((await deployRepository({ app: 'demo-app', repositoryUrl: 'https://github.com/example/demo', baseUrl })).run_id, 123);
      assert.equal((await getRun(123, baseUrl)).state, 'queued');
      assert.deepEqual(calls, [['deploy', 'demo-app'], ['status', '123']]);
      await writeFile(path, 'different-valid-token-0123456789abcdef');
      await assert.rejects(getRun(123, baseUrl), /authentication required/);
      assert.equal(calls.length, 2);
    });
  } finally {
    if (previousToken === undefined) delete process.env.RAILSHOT_API_TOKEN; else process.env.RAILSHOT_API_TOKEN = previousToken;
    if (previousFile === undefined) delete process.env.RAILSHOT_API_TOKEN_FILE; else process.env.RAILSHOT_API_TOKEN_FILE = previousFile;
    await rm(directory, { recursive: true, force: true });
  }
});
