import test from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { randomUUID } from 'node:crypto';
import { setTimeout as pause } from 'node:timers/promises';
import { createAppServer } from '../src/server.js';
import { apiAccessConfig } from '../src/access.js';

async function fixture(t, product, releaseLeaseMs = 1000) {
  const token = 'release-test-token-'.repeat(3);
  const server = createAppServer({ product, access: apiAccessConfig({ RAILSHOT_API_TOKEN: token }), releaseLeaseMs });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => server.shutdown());
  const origin = `http://127.0.0.1:${server.address().port}`;
  const headers = { authorization: `Bearer ${token}`, 'content-type': 'application/json' };
  return { server, get: (path) => fetch(origin + path, { headers }),
    post: (path) => fetch(origin + path, { method: 'POST', headers, body: '{}' }),
    prepare: (id = randomUUID(), auth = true) => fetch(origin + '/internal/releases/prepare', {
      method: 'POST', headers: auth ? headers : { 'content-type': 'application/json' }, body: JSON.stringify({ release_id: id }),
    }) };
}

test('release waits for in-flight requests, fences admissions atomically and recovers an abandoned lease', async (t) => {
  let finish, started;
  const entered = new Promise((resolve) => { started = resolve; });
  let paused = false, pauses = 0, resumed = 0, closed = 0;
  const f = await fixture(t, {
    async getDeploymentLogs() { started(); return new Promise((resolve) => { finish = () => resolve({ logs: [] }); }); },
    pauseForRelease() { paused = true; pauses++; return true; },
    resumeAfterRelease() { paused = false; resumed++; },
    close() { closed++; },
  }, 100);
  assert.equal((await f.prepare(undefined, false)).status, 401);
  assert.equal(pauses, 0);
  const pending = f.get('/api/v1/deployments/one/logs'); await entered;
  const id = randomUUID();
  assert.equal((await f.prepare(id)).status, 202); assert.equal(pauses, 1);
  assert.equal((await f.post('/api/v1/deployments')).status, 503);
  finish(); assert.equal((await pending).status, 200);
  assert.equal((await f.prepare(id)).status, 200); assert.equal(paused, true);
  assert.equal((await f.prepare()).status, 409);
  const rejected = await f.get('/api/v1/options');
  assert.equal(rejected.status, 503);
  assert.deepEqual((await rejected.json()).error, { code: 'PLATFORM_UPDATING',
    message: '서버 업데이트를 마치고 요청을 이어서 처리합니다.', outcome_unknown: false, retryable: true });
  await pause(130);
  assert.equal(resumed, 1); assert.equal(paused, false);
  assert.equal((await f.get('/api/v1/options')).status, 200);
  await f.server.shutdown(); assert.equal(closed, 1);
});

test('release does not stop active native work; shutdown waits for durable store closure', async (t) => {
  let busy = true, closeDone = false;
  const f = await fixture(t, {
    pauseForRelease: () => !busy, resumeAfterRelease() {},
    async close() { await pause(15); closeDone = true; },
  });
  const id = randomUUID();
  const waiting = await f.prepare(id);
  assert.equal(waiting.status, 202);
  assert.equal((await waiting.json()).waiting_for, 'active_worker');
  assert.equal((await f.get('/api/v1/options')).status, 200);
  busy = false;
  assert.equal((await f.prepare(id)).status, 200);
  await f.server.shutdown(); assert.equal(closeDone, true);
});

test('abandoned draining releases the fence even when work never became idle', async (t) => {
  let resumed = 0;
  const f = await fixture(t, { pauseForRelease: () => false, resumeAfterRelease() { resumed++; }, close() {} }, 40);
  assert.equal((await f.prepare()).status, 202);
  assert.equal((await f.post('/api/v1/deployments')).status, 503);
  await pause(70);
  assert.equal(resumed, 1);
  assert.notEqual((await f.post('/api/v1/deployments')).status, 503);
});

test('pre-sync waits out an earlier release lease and reports only safe states', async () => {
  const { prepareRelease } = await import('../src/prepare-release.js');
  let calls = 0; const progress = [];
  await prepareRelease({ token: 'private-test-token', templateId: 'a'.repeat(64), pollMs: 1, timeoutMs: 100,
    onProgress: (row) => progress.push(row), request: async (_url, request) => {
      const { release_id } = JSON.parse(request.body);
      calls++;
      if (calls === 1) return Response.json({ error: { message: 'do not log private content' } }, { status: 409 });
      if (calls === 2) return Response.json({ status: 'busy', waiting_for: 'active_worker' }, { status: 202 });
      return Response.json({ status: 'prepared', release_id, lease_ms: 120000 });
    } });
  assert.deepEqual(progress.map(row => row.reason), ['previous_release_lease', 'active_worker']);
  assert.ok(!JSON.stringify(progress).includes('private'));
});

test('the API entrypoint handles SIGTERM and exits after graceful closure', { timeout: 8000 }, async (t) => {
  const { spawn } = await import('node:child_process');
  const { fileURLToPath } = await import('node:url');
  const { mkdtemp, rm } = await import('node:fs/promises');
  const { tmpdir } = await import('node:os');
  const directory = await mkdtemp(`${tmpdir()}/railshot-signal-`);
  t.after(() => rm(directory, { recursive: true, force: true }));
  const child = spawn(process.execPath, [fileURLToPath(new URL('../src/server.js', import.meta.url))], {
    env: { PATH: process.env.PATH, HOME: process.env.HOME, PORT: '0', RAILSHOT_STATE_DIR: directory }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  t.after(() => { if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL'); });
  const exited = once(child, 'exit');
  await new Promise((resolve, reject) => {
    child.stdout.on('data', (data) => { if (String(data).includes('RAILSHOT API listening')) resolve(); });
    child.once('error', reject);
    child.once('exit', () => reject(new Error('API exited before listening')));
  });
  child.kill('SIGTERM');
  assert.deepEqual(await exited, [0, null]);
});

test('an already deployed template never pauses service for a dashboard-only sync', async (t) => {
  const previous = process.env.RAILSHOT_POD_TEMPLATE_ID;
  process.env.RAILSHOT_POD_TEMPLATE_ID = 'a'.repeat(64);
  t.after(() => { if (previous === undefined) delete process.env.RAILSHOT_POD_TEMPLATE_ID; else process.env.RAILSHOT_POD_TEMPLATE_ID = previous; });
  const f = await fixture(t, { pauseForRelease() { assert.fail('current template must not pause'); }, close() {} });
  const response = await fetch(`http://127.0.0.1:${f.server.address().port}/internal/releases/prepare`, {
    method: 'POST', headers: { authorization: `Bearer ${'release-test-token-'.repeat(3)}`, 'content-type': 'application/json',
      'x-railshot-desired-template': 'a'.repeat(64) }, body: JSON.stringify({ release_id: randomUUID() }),
  });
  assert.equal(response.status, 200); assert.equal((await response.json()).status, 'current');
  assert.equal((await f.get('/api/v1/options')).status, 200);
  assert.equal((await f.get('/readyz')).status, 200);
});

test('pre-sync waits for active work and refuses malformed or rejected preparation', async () => {
  const { prepareRelease } = await import('../src/prepare-release.js');
  const options = { token: 'private-test-token', templateId: 'a'.repeat(64), pollMs: 1, timeoutMs: 100 };
  const calls = [];
  await prepareRelease({ ...options, request: async (_url, request) => {
    calls.push(request);
    const { release_id } = JSON.parse(request.body);
    return calls.length === 1 ? Response.json({ status: 'busy' }, { status: 202 })
      : Response.json({ status: 'prepared', release_id, lease_ms: 120000 });
  } });
  assert.equal(calls.length, 2); assert.equal(calls[0].body, calls[1].body);
  assert.equal(calls[0].headers['x-railshot-desired-template'], options.templateId);
  for (const response of [() => new Response('Not found', { status: 404 }),
    () => Response.json({ status: 'prepared', release_id: 'wrong', lease_ms: 120000 }),
    () => Response.json({ status: 'busy' }, { status: 401 })]) {
    await assert.rejects(prepareRelease({ ...options, request: async () => response() }));
  }
  await assert.rejects(prepareRelease({ ...options, timeoutMs: 5,
    request: async () => Response.json({ status: 'busy' }, { status: 202 }) }), /API_HANDOVER_BUSY/);
});

test('saved-source operator replay requires the private API token and exact input', async t => {
  const calls = [], f = await fixture(t, {
    replaySubmittedSource: async (...args) => { calls.push(args); return { id: 'replay' }; }, close() {},
  });
  const origin = `http://127.0.0.1:${f.server.address().port}`;
  const body = JSON.stringify({ operation_id: randomUUID(), environment_target_id: 'k3s-gcp' });
  const path = '/internal/deployments/replay-source';
  assert.equal((await fetch(origin + path, { method: 'POST', headers: { 'content-type': 'application/json' }, body })).status, 401);
  assert.equal((await f.post(path)).status, 422);
  const response = await fetch(origin + path, { method: 'POST', headers: { 'content-type': 'application/json', authorization: `Bearer ${'release-test-token-'.repeat(3)}` }, body });
  assert.equal(response.status, 202); assert.equal(calls.length, 1);
});

test('liveness responds during initialization while readiness is unavailable', async t => {
  let initialized;
  const pending = new Promise(resolve => { initialized = resolve; });
  const f = await fixture(t, pending);
  try {
    const health = await Promise.race([f.get('/healthz'), pause(1000).then(() => { throw new Error('Liveness blocked on initialization'); })]);
    assert.equal(health.status, 200);
    assert.equal((await health.json()).configured, false);
    assert.equal((await f.get('/readyz')).status, 503);
  } finally { initialized({ close() {} }); }
  await f.server.productReady;
  assert.equal((await f.get('/readyz')).status, 200);
});

test('operator CD resume requires token and exact publication identity', async t => {
  const calls = [], f = await fixture(t, {
    resumePublishedOperation: async input => { calls.push(input); return { id: input.operation_id }; }, close() {},
  });
  const origin = `http://127.0.0.1:${f.server.address().port}`;
  const path = '/internal/deployments/resume';
  const input = { operation_id: randomUUID(), source_commit: 'a'.repeat(40), run_id: '123' };
  const send = (body, auth = true) => fetch(origin + path, { method: 'POST', headers: {
    'content-type': 'application/json', ...(auth ? { authorization: `Bearer ${'release-test-token-'.repeat(3)}` } : {}),
  }, body: JSON.stringify(body) });
  assert.equal((await send(input, false)).status, 401);
  assert.equal((await send({ ...input, session_id: 'spoof' })).status, 422);
  assert.equal((await send({ operation_id: input.operation_id })).status, 422);
  assert.equal(calls.length, 0);
  assert.equal((await send(input)).status, 202);
  assert.deepEqual(calls, [input]);
});
