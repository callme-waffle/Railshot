import test from 'node:test';
import { DatabaseSync } from 'node:sqlite';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, realpath, rm, stat, chmod, symlink, writeFile, readdir, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as pause } from 'node:timers/promises';
import { createProductService } from '../src/product.js';
import { createProductStore } from '../src/product-store.js';
import { createAppServer } from '../src/server.js';
import { createApplicationAdapter } from '../src/applications.js';
import { EnvironmentError } from '../src/environments.js';
import { SubmissionError } from '../src/github.js';
import { fetchPublicGithubSource } from '../src/public-github.js';

function diskState(directory) {
  const db = new DatabaseSync(join(directory, 'dashboard.sqlite3'), { readOnly: true });
  try { return { operations: Object.fromEntries(db.prepare('SELECT id, record FROM operations').all().map((row) => [row.id, JSON.parse(row.record)])) }; } finally { db.close(); }
}

const files = [{ path: 'app.js', content: Buffer.from('hello') }];
const input = { app: 'demo-app', target_id: 'demo', source_type: 'folder', files };
const publication = { run_id: 123, target_id: 'demo', app: 'demo-app', source_commit: 'a'.repeat(40), artifact_id: 456, producer_attempt: 1 };
const deployed = { cd: { state: 'deployed', deployed: true, revision: 'b'.repeat(40) }, public_http: { state: 'succeeded', verified_at: '2026-10-02T12:00:00Z', url: 'https://demo.example.test' } };
async function fixture(t, overrides = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-product-'));
  let dispatches = 0, reads = 0, cdCalls = 0;
  const service = { targetId: 'demo',
    deploy: async () => { dispatches++; return { run_id: '123', app: 'demo-app', source_commit: publication.source_commit, target_id: 'demo', state: 'queued' }; },
    status: async () => { reads++; return { run_id: 123, state: 'published', status: 'completed', conclusion: 'success', publication }; },
    ...overrides.service };
  const deployPublished = overrides.deployPublished || (async () => { cdCalls++; return deployed; });
  const product = await createProductService({ service, directory, deployPublished, pollInterval: 5, ...overrides, service });
  t.after(async () => { await product.close(); await rm(directory, { recursive: true, force: true }); });
  return { product, service, directory, dispatches: () => dispatches, reads: () => reads, cdCalls: () => cdCalls };
}
async function settle(get, predicate = (record) => !['queued', 'running'].includes(record.status)) {
  const deadline = performance.now() + 10000;
  let record;
  // Durable writes and HTTP polling take longer on shared CI runners than local fixtures.
  do { record = await get(); if (predicate(record)) return record; await pause(5); } while (performance.now() < deadline);
  assert.fail(`Operation did not settle: status=${record?.status}, stage=${record?.stage}`);
}

test('documentation-only uploads fail before reserving an operation or invoking CI', async (t) => {
  const f = await fixture(t);
  const docs = ['readme.md', 'LICENSE.md', '.editorconfig', 'contributing.md', 'mentioned.md', 'CODE_OF_CONDUCT.md']
    .map((path) => ({ path, content: Buffer.from('documentation') }));
  for (const source_type of ['github', 'zip', 'folder']) {
    const source = source_type === 'github' ? { repository_url: 'https://github.com/xxczaki/awesome-calculators' } : { files: docs };
    await assert.rejects(f.product.createDeployment({ ...input, files: undefined, source_type, ...source }, `docs-${source_type}`,
      async () => ({ files: docs })), (error) => error.status === 422 && /실제 웹 앱/.test(error.message));
  }
  assert.equal(f.dispatches(), 0);
  const created = await f.product.createDeployment(input, 'valid-after-docs');
  assert.ok(created.id); // rejected documentation did not occupy the executor
});

test('source lookup failure is a traceable 422, not a missing deployment API, and reserves nothing', async (t) => {
  const f = await fixture(t), logs = [];
  t.mock.method(console, 'error', (line) => logs.push(JSON.parse(line)));
  const sourceLoader = (url) => fetchPublicGithubSource(url, async () => Response.json({ message: 'Not Found' }, { status: 404 }));
  const server = createAppServer({ product: f.product, service: f.service, sourceLoader });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); }));
  const base = `http://127.0.0.1:${server.address().port}`;
  const form = new FormData();
  for (const [key, value] of Object.entries({ app: 'demo-app', target_id: 'demo', repository_url: 'https://github.com/example/missing' })) form.set(key, value);
  const response = await fetch(`${base}/api/v1/deployments`, { method: 'POST', headers: { 'Idempotency-Key': 'missing-source' }, body: form });
  const body = await response.json();
  assert.equal(response.status, 422);
  assert.equal(body.error.code, 'SOURCE_NOT_FOUND');
  assert.match(body.error.message, /ZIP·폴더/);
  assert.equal(body.error.request_id, response.headers.get('x-request-id'));
  assert.equal(body.error.outcome_unknown, false);
  assert.deepEqual(logs[0], { event: 'api.request_failed', request_id: body.error.request_id, status: 422, code: 'SOURCE_NOT_FOUND' });
  assert.deepEqual(diskState(f.directory).operations, {});
  assert.equal(f.dispatches(), 0);
  const absent = await fetch(`${base}/api/v1/no-such-route`);
  assert.equal(absent.status, 404);
  assert.equal((await absent.json()).error.code, 'NOT_FOUND');
});

test('snapshot and intent are durable before one dispatch; replay returns the same completed deployment', async (t) => {
  let f;
  f = await fixture(t, { service: { deploy: async () => {
    const state = diskState(f.directory), operation = Object.values(state.operations)[0];
    assert.equal(operation.status, 'running');
    const saved = JSON.parse(await readFile(join(f.directory, `${operation.id}.source.json`)));
    assert.equal(Buffer.from(saved[0].content, 'base64').toString(), 'hello');
    assert.equal((await stat(join(f.directory, `${operation.id}.source.json`))).mode & 0o777, 0o600);
    return { run_id: 123, source_commit: publication.source_commit };
  } } });
  const created = await f.product.createDeployment(input, 'same');
  const complete = await settle(() => f.product.getDeployment(created.id));
  assert.equal(complete.status, 'succeeded');
  assert.equal(complete.url, deployed.public_http.url);
  assert.equal(complete.ci.run_id, '123');
  assert.match(complete.source_digest, /^[a-f0-9]{64}$/);
  assert.equal(complete.observation.deployment_id, complete.id);
  assert.equal(complete.observation.metrics.pods.state, 'not_configured');
  assert.equal(complete.ci.publication_artifact_id, '456');
  assert.equal(f.cdCalls(), 1);
  assert.equal((await f.product.createDeployment(input, 'same')).id, created.id);
  assert.equal(f.cdCalls(), 1);
  await assert.rejects(f.product.createDeployment({ ...input, files: [{ ...files[0], content: Buffer.from('changed') }] }, 'same'), { code: 'IDEMPOTENCY_CONFLICT' });
  assert.equal((await f.product.getBuild('123')).id, '123');
  const before = f.reads();
  await assert.rejects(f.product.getBuild('999'), { status: 404 });
  await assert.rejects(f.product.getBuild('__proto__'), { status: 404 });
  assert.equal(f.reads(), before);
});

test('concurrent identical keys dispatch once; another deployment waits in the FIFO', async (t) => {
  let release;
  const wait = new Promise((resolve) => { release = resolve; });
  let calls = 0;
  const f = await fixture(t, { service: {
    deploy: async () => { const run_id = String(123 + calls++); await wait; return { run_id, source_commit: publication.source_commit }; },
    status: async (id) => ({ run_id: id, state: 'published', publication: { ...publication, run_id: id } }),
  } });
  const [first, second] = await Promise.all([f.product.createDeployment(input, 'same'), f.product.createDeployment(input, 'same')]);
  assert.equal(first.id, second.id);
  let next;
  try {
    next = await f.product.createDeployment(input, 'other');
    assert.equal(next.status, 'queued'); assert.ok(next.queue.sequence > first.queue.sequence);
    assert.equal(next.queue.started_at, undefined);
  } finally { release(); }
  assert.equal((await settle(() => f.product.getDeployment(first.id))).status, 'succeeded');
  assert.equal((await settle(() => f.product.getDeployment(next.id))).status, 'succeeded');
  assert.equal(calls, 2);
});

test('CD progress persists revision and HTTP stage before completion while observer failure stays separate', async (t) => {
  let finish;
  const waiting = new Promise((resolve) => { finish = resolve; });
  const images = { web: `ghcr.io/example/demo@sha256:${'c'.repeat(64)}` };
  const f = await fixture(t, {
    service: { status: async () => ({ run_id: 123, state: 'published', publication: { ...publication, images } }) },
    deployPublished: async ({ onProgress }) => {
      await onProgress({ cd: deployed.cd, public_http: { state: 'unverified', verified_at: null, url: null } });
      await waiting; return deployed;
    },
    observeMetrics: async (record) => ({ deployment_id: record.id, metrics: { pods: { state: 'unavailable', value: null } } }),
  });
  const first = await f.product.createDeployment(input, 'progress');
  try {
    const progressing = await settle(() => f.product.getDeployment(first.id), (record) => record.stage === 'http');
    assert.equal(progressing.status, 'running'); assert.equal(progressing.cd.revision, deployed.cd.revision);
    assert.deepEqual(progressing.ci.images, images); assert.equal(progressing.url, null);
    assert.equal(progressing.observation.metrics.pods.state, 'unavailable');
    const disk = diskState(f.directory);
    assert.equal(disk.operations[first.id].stage, 'http');
  } finally { finish(); }
  assert.equal((await settle(() => f.product.getDeployment(first.id))).status, 'succeeded');
});

test('release drain finishes the active deployment but prevents the next queued writer until resumed', async (t) => {
  let release, entered; let calls = 0;
  const waiting = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  const f = await fixture(t, { service: {
    deploy: async () => { const run_id = String(123 + calls++); entered(); await waiting; return { run_id, source_commit: publication.source_commit }; },
    status: async id => ({ run_id: id, state: 'published', publication: { ...publication, run_id: id } }),
  } });
  const first = await f.product.createDeployment(input, 'drain-first');
  await started;
  const second = await f.product.createDeployment(input, 'drain-second');
  try {
    assert.equal(f.product.pauseForRelease(), false);
    release();
    assert.equal((await settle(() => f.product.getDeployment(first.id))).status, 'succeeded');
    await pause(25);
    assert.equal(calls, 1);
    assert.equal((await f.product.getDeployment(second.id)).status, 'queued');
    assert.equal(f.product.pauseForRelease(), true);
  } finally { release(); f.product.resumeAfterRelease(); }
  assert.equal((await settle(() => f.product.getDeployment(second.id))).status, 'succeeded');
  assert.equal(calls, 2);
});

test('GitHub URL replay keeps the first pinned source without fetching current HEAD', async (t) => {
  const f = await fixture(t);
  let loads = 0;
  const request = { app: input.app, target_id: input.target_id, source_type: 'github', repository_url: 'https://github.com/example/demo' };
  const loader = async () => { loads++; return { files, source: { type: 'github', repository: request.repository_url, sha: 'c'.repeat(40) } }; };
  const first = await f.product.createDeployment(request, 'github-key', loader);
  await settle(() => f.product.getDeployment(first.id));
  await f.product.createDeployment(request, 'github-key', async () => { throw new Error('Must not refetch'); });
  assert.equal(loads, 1);
});

test('lost dispatch response survives restart and fences the same app without replay', async (t) => {
  let dispatches = 0;
  const f = await fixture(t, { service: { deploy: async () => { dispatches++; throw new Error('private upstream secret'); } } });
  const first = await f.product.createDeployment(input, 'uncertain');
  const failed = await settle(() => f.product.getDeployment(first.id));
  assert.equal(failed.status, 'unknown');
  assert.equal(failed.error.outcome_unknown, true);
  assert.ok(failed.error.request_id);
  assert.ok(!JSON.stringify(failed).includes('private upstream secret'));
  await f.product.close();
  const restarted = await createProductService({ service: f.service, directory: f.directory, deployPublished: async () => { assert.fail('No CD replay'); } });
  try {
    assert.equal((await restarted.createDeployment(input, 'uncertain')).id, first.id);
    await assert.rejects(restarted.createDeployment(input, 'new-key'), { code: 'APPLICATION_RECONCILE_REQUIRED' });
    assert.equal(dispatches, 1);
  } finally { await restarted.close(); }
});

test('unfinished durable intent is recovered as unknown; malformed or insecure storage fails closed', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-recovery-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const store = await createProductStore(directory);
  await store.transaction((state) => { state.operations.pending = { id: 'pending', kind: 'deployments', status: 'queued' }; });
  await assert.rejects(createProductStore(directory), /already open/);
  await store.close();
  const restored = await createProductStore(directory);
  assert.equal(restored.read().operations.pending.status, 'unknown');
  await restored.close();
  await chmod(directory, 0o755);
  await assert.rejects(createProductStore(directory), /private owned/);
  await chmod(directory, 0o700);
  const linked = `${directory}-link`;
  await symlink(directory, linked);
  try { await assert.rejects(createProductStore(linked), /private owned/); } finally { await rm(linked); }
});

test('concurrent store close callers both wait for the owner lock release before restart', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-close-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const store = await createProductStore(directory);
  const session = store.dashboard.session();
  store.dashboard.preferences(session.id, { view: 'history' });
  const closing = store.close();
  try {
    await store.close();
    await assert.rejects(stat(join(directory, 'owner.json')), { code: 'ENOENT' });
    const restarted = await createProductStore(directory);
    try { assert.equal(restarted.dashboard.preferences(session.id).view, 'history'); }
    finally { await restarted.close(); }
  } finally { await closing; }
});

test('snapshot quota bounds admission before any dispatch', async (t) => {
  const f = await fixture(t, { maxSourceBytes: 1 });
  await assert.rejects(f.product.createDeployment(input, 'large'), { code: 'CAPACITY_EXCEEDED' });
  assert.equal(f.dispatches(), 0);
  assert.equal((await readdir(f.directory)).filter((name) => name.endsWith('.source.json')).length, 0);
  await assert.rejects(f.product.createDeployment({ ...input, target_id: 'foreign' }, 'bad-target'), { status: 422 });
  assert.equal(f.dispatches(), 0);
});

test('CI publication alone and mismatched publication never report application success', async (t) => {
  const f = await fixture(t, { deployPublished: async () => ({ cd: { state: 'running', revision: null, deployed: false }, public_http: { state: 'not_run', url: 'https://unverified.invalid' } }) });
  const first = await f.product.createDeployment(input, 'not-verified');
  const record = await settle(() => f.product.getDeployment(first.id));
  assert.equal(record.status, 'unknown'); assert.equal(record.url, null);
  const g = await fixture(t, { service: { status: async () => ({ state: 'published', publication: { ...publication, app: 'foreign' } }) } });
  const other = await g.product.createDeployment(input, 'mismatch');
  const mismatch = await settle(() => g.product.getDeployment(other.id));
  assert.equal(mismatch.status, 'blocked'); assert.equal(mismatch.error.code, 'CI_BINDING_MISMATCH');
  assert.equal(g.cdCalls(), 0);
});

test('environment plans are private, consumed once and share deployment admission', async (t) => {
  const adapter = {
    profiles: () => [{ id: 'aws', supported: true }],
    plan: async (_, { id }) => ({ public: { id, executable: true }, private: { secret_path: '/private/key' } }),
    verifyPlan: async () => {}, execute: async () => ({ status: 'succeeded', stage: 'runtime', deployment_supported: false, runtime_target_id: 'runtime-demo' }),
  };
  const f = await fixture(t, { environmentAdapter: adapter });
  const plan = await f.product.createPlan({});
  assert.ok(!JSON.stringify(f.product.getPlan(plan.id)).includes('/private/key'));
  const environment = await f.product.createEnvironment({ plan_id: plan.id }, 'env-key');
  await settle(() => f.product.getEnvironment(environment.id));
  assert.equal((await f.product.createEnvironment({ plan_id: plan.id }, 'env-key')).id, environment.id);
  await assert.rejects(f.product.createEnvironment({ plan_id: plan.id }, 'other-key'), { code: 'CONFLICT' });
  assert.throws(() => f.product.getPlan('__proto__'), { status: 404 });
});

async function httpFixture(t, options = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-http-'));
  const server = createAppServer({ stateDirectory: directory,
    service: { targetId: 'demo', deploy: async () => ({ run_id: '123', source_commit: publication.source_commit }),
      status: async () => ({ run_id: 123, state: 'published', status: 'completed', conclusion: 'success', publication }) },
    deployPublished: async () => deployed, pollInterval: 5, ...options });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { await new Promise((resolve) => server.close(resolve)); await (await server.productReady)?.close(); await rm(directory, { recursive: true, force: true }); });
  return { server, base: `http://127.0.0.1:${server.address().port}` };
}
function form() {
  const value = new FormData(); value.set('app', 'demo-app'); value.set('target_id', 'demo'); value.set('source_type', 'folder');
  value.append('files', new Blob(['hello']), 'app.js'); value.set('paths', '["app.js"]'); return value;
}
test('record view skips slow metrics and CI refresh without changing default detail reads', async (t) => {
  let metricReads = 0, ciReads = 0, release, entered;
  const gate = new Promise(resolve => { release = resolve; });
  const observing = new Promise(resolve => { entered = resolve; });
  t.after(() => release());
  const f = await fixture(t, {
    service: { status: async () => { ciReads++; return { run_id: 123, state: 'failed', status: 'completed', conclusion: 'failure' }; } },
    observeMetrics: async () => { metricReads++; entered(); await gate; return { state: 'ready' }; },
  });
  const created = await f.product.createDeployment(input, 'record-view');
  const record = await settle(() => f.product.getDeployment(created.id, null, { view: 'record' }));
  assert.equal(record.status, 'failed');
  const now = Date.now(); t.mock.method(Date, 'now', () => now + 31000);
  const server = createAppServer({ product: f.product, service: f.service });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }));
  const url = `http://127.0.0.1:${server.address().port}/api/v1/deployments/${created.id}`;
  const before = ciReads;
  const fast = await fetch(url + '?view=record', { signal: AbortSignal.timeout(1000) });
  const body = await fast.json();
  assert.equal(fast.status, 200); assert.equal(body.id, created.id);
  assert.equal('observation' in body, false); assert.equal('session_id' in body, false);
  assert.equal(ciReads, before); assert.equal(metricReads, 0);
  for (const query of ['view=full', 'view=record&view=record', 'view=record&token=x']) {
    assert.equal((await fetch(url + '?' + query)).status, 422);
  }
  assert.equal((await fetch(url.replace('/deployments/', '/builds/') + '?view=record')).status, 422);
  let finished = false;
  const full = fetch(url).then(response => response.json()).then(value => { finished = true; return value; });
  await observing; assert.equal(finished, false); assert.equal(ciReads, before + 1);
  assert.equal((await fetch(url + '?view=record', { signal: AbortSignal.timeout(1000) })).status, 200);
  assert.equal(metricReads, 1); assert.equal(finished, false);
  release(); assert.deepEqual((await full).observation, { state: 'ready' });
});

test('API reads registrar updates and never falls back to a legacy observer after handoff', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-observer-binding-'));
  const legacy = join(directory, 'observer.json'), live = join(directory, 'product.json');
  const previous = Object.fromEntries(['RAILSHOT_OBSERVER_CONFIG', 'RAILSHOT_OBSERVER_PRODUCT_FILE'].map((key) => [key, process.env[key]]));
  t.after(async () => {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
    await rm(directory, { recursive: true, force: true });
  });
  const collector = { id: 'shared-observer', lifecycle: 'shared', expires_at: new Date(Date.now() + 3600000).toISOString() };
  await writeFile(legacy, JSON.stringify({ version: 1, targets: [], collector: { ...collector, id: 'legacy-observer' } }), { mode: 0o600 });
  process.env.RAILSHOT_OBSERVER_CONFIG = legacy;
  process.env.RAILSHOT_OBSERVER_PRODUCT_FILE = live;
  const { base, server } = await httpFixture(t);
  const product = await server.productReady;
  const operation = await product.createDeployment(input, 'observer-binding');
  await settle(() => product.getDeployment(operation.id));
  const read = async () => (await (await fetch(`${base}/api/v1/deployments/${operation.id}`)).json()).observation;
  assert.equal((await read()).metrics.pods.state, 'unavailable', 'missing live state must not restore legacy values');
  await writeFile(live, JSON.stringify({ version: 1, targets: [], collector }), { mode: 0o600 });
  assert.equal((await read()).collector.id, collector.id, 'the same API process reads registrar output');
  await rm(live);
  assert.equal((await read()).metrics.pods.state, 'unavailable');
  delete process.env.RAILSHOT_OBSERVER_PRODUCT_FILE;
  const oldServer = await httpFixture(t);
  const oldProduct = await oldServer.server.productReady;
  const oldOperation = await oldProduct.createDeployment(input, 'legacy-observer');
  await settle(() => oldProduct.getDeployment(oldOperation.id));
  assert.equal((await oldProduct.getDeployment(oldOperation.id)).observation.collector.id, 'legacy-observer', 'existing static configuration remains supported');
});

test('deployment polling preserves normalized CI steps while running and after publication', async (t) => {
  const runningSteps = [
    { key: 'loop', status: 'in_progress', conclusion: null, observed_attempt: 2 },
    { key: 'release', status: 'queued', conclusion: null, observed_attempt: null },
  ];
  let upstream = { run_id: 123, state: 'running', status: 'in_progress', conclusion: null, steps: runningSteps };
  const { base } = await httpFixture(t, { service: {
    targetId: 'demo', deploy: async () => ({ run_id: '123', source_commit: publication.source_commit }),
    status: async () => structuredClone(upstream),
  } });
  const accepted = await fetch(`${base}/api/v1/deployments`, { method: 'POST', body: form(), headers: { 'Idempotency-Key': 'steps-progress' } });
  assert.equal(accepted.status, 202);
  const get = async () => (await fetch(`${base}${accepted.headers.get('location')}`)).json();
  const running = await settle(get, (record) => record.ci?.steps?.[0]?.status === 'in_progress');
  assert.equal(running.status, 'running'); assert.equal(running.stage, 'ci');
  assert.equal(running.ci.run_id, '123'); assert.equal(running.cd.state, 'not_started');
  assert.deepEqual(running.ci.steps, runningSteps);

  const releasingSteps = [
    { key: 'loop', status: 'completed', conclusion: 'success', observed_attempt: 2 },
    { key: 'release', status: 'in_progress', conclusion: null, observed_attempt: 2 },
  ];
  upstream = { ...upstream, steps: releasingSteps };
  const releasing = await settle(get, (record) => record.ci?.steps?.[1]?.status === 'in_progress');
  assert.equal(releasing.status, 'running'); assert.equal(releasing.stage, 'ci');
  assert.deepEqual(releasing.ci.steps, releasingSteps);

  const completedSteps = releasingSteps.map((step) => ({ ...step, status: 'completed', conclusion: 'success' }));
  upstream = { ...upstream, state: 'published', status: 'completed', conclusion: 'success', steps: completedSteps, publication };
  const complete = await settle(get);
  assert.equal(complete.status, 'succeeded'); assert.equal(complete.stage, 'complete');
  assert.deepEqual(complete.ci.steps, completedSteps);
  assert.equal(complete.ci.producer_attempt, 1); // Observation attempt and verified artifact producer remain distinct.
  assert.deepEqual((await (await fetch(`${base}/api/v1/builds/123`)).json()).steps, completedSteps);
  assert.deepEqual((await (await fetch(`${base}/api/runs/123`)).json()).steps, completedSteps);
});

test('v1 HTTP contract has accepted/error headers, strict fields, preserved legacy and no foreign reads', async (t) => {
  const { base } = await httpFixture(t);
  const targets = await (await fetch(`${base}/api/v1/targets`)).json();
  assert.equal(targets.items[0].capabilities.application_deployment, true);
  const post = await fetch(`${base}/api/v1/deployments`, { method: 'POST', body: form(), headers: { 'Idempotency-Key': 'http1', 'X-Request-ID': 'untrusted' } });
  assert.equal(post.status, 202);
  const accepted = await post.json();
  assert.deepEqual(Object.keys(accepted).sort(), ['action', 'request_id', 'resource_id', 'status']);
  assert.equal(post.headers.get('x-request-id'), accepted.request_id);
  assert.notEqual(accepted.request_id, 'untrusted');
  assert.equal(post.headers.get('retry-after'), '2'); assert.equal(post.headers.get('cache-control'), 'no-store');
  const location = post.headers.get('location');
  assert.equal(location, `/api/v1/deployments/${accepted.resource_id}`);
  await settle(async () => (await fetch(`${base}${location}`)).json());
  const replay = await fetch(`${base}/api/v1/deployments`, { method: 'POST', body: form(), headers: { 'Idempotency-Key': 'http1' } });
  assert.equal(replay.status, 200); assert.equal((await replay.json()).id, accepted.resource_id);
  assert.notEqual(replay.headers.get('x-request-id'), accepted.request_id);
  const legacy = await fetch(`${base}/api/runs/123`); assert.equal(legacy.status, 200); assert.equal((await legacy.json()).run_id, 123);
  assert.equal((await fetch(`${base}/api/v1/builds/999`)).status, 404);
  assert.equal((await fetch(`${base}/api/runs/999`)).status, 404);
  for (const mutate of [(value) => value.append('app', 'other'), (value) => value.set('provider_token', 'secret'), (value) => value.set('target_id', 'foreign')]) {
    const value = form(); mutate(value);
    const response = await fetch(`${base}/api/v1/deployments`, { method: 'POST', body: value, headers: { 'Idempotency-Key': 'different' } });
    assert.equal(response.status, 422);
    const body = await response.json(); assert.equal(body.error.code, 'INVALID_INPUT'); assert.equal(body.error.request_id, response.headers.get('x-request-id'));
    assert.ok(!JSON.stringify(body).includes('secret'));
  }
  const method = await fetch(`${base}/api/v1/profiles`, { method: 'POST' }); assert.equal(method.status, 405); assert.equal(method.headers.get('allow'), 'GET');
  assert.equal((await fetch(`${base}/api/v1/targets?limit=20&limit=2`)).status, 422);
  assert.equal((await fetch(`${base}/api/v1/targets?unexpected=1`)).status, 422);
  assert.equal((await fetch(`${base}/api/v1/deployments`, { method: 'POST', body: form() })).status, 422);
});

test('unconfigured server persists dashboard state while listing empty execution capabilities', async (t) => {
  const { base, server } = await httpFixture(t, { service: null, deployPublished: undefined });
  for (const path of ['targets', 'profiles']) assert.deepEqual(await (await fetch(`${base}/api/v1/${path}`)).json(), { items: [], next_marker: null });
  assert.ok((await server.productReady).dashboard);
  assert.equal((await (await fetch(`${base}/healthz`)).json()).configured, false, 'dashboard storage alone does not configure an executor');
  const response = await fetch(`${base}/api/v1/sessions`, { method: 'POST' });
  assert.equal(response.status, 201);
  assert.match(response.headers.get('set-cookie'), /railshot_session=/);
});

test('health stays live but not ready when private product state fails initialization', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-unready-'));
  await chmod(directory, 0o750);
  let externalCalls = 0;
  const server = createAppServer({ stateDirectory: directory,
    service: { targetId: 'demo', deploy: async () => { externalCalls++; }, status: async () => { externalCalls++; } } });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { await new Promise((resolve) => server.close(resolve)); await rm(directory, { recursive: true, force: true }); });
  const base = `http://127.0.0.1:${server.address().port}`;
  const response = await fetch(`${base}/healthz`);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true, configured: false, target_id: 'demo' });
  const api = await fetch(`${base}/api/v1/targets`);
  assert.equal(api.status, 503);
  assert.ok(!(await api.text()).includes(directory));
  assert.equal(externalCalls, 0);
});


test('intent write failure prevents external effects and fails subsequent writes closed', async (t) => {
  const f = await fixture(t);
  const db = new DatabaseSync(join(f.directory, 'dashboard.sqlite3'));
  db.exec("CREATE TRIGGER simulate_disk_failure BEFORE INSERT ON operations BEGIN SELECT RAISE(ABORT, 'write failed'); END");
  db.close();
  await assert.rejects(f.product.createDeployment(input, 'write-failure'));
  await assert.rejects(f.product.createDeployment(input, 'write-failure-2'));
  assert.equal(f.dispatches(), 0);
  assert.equal((await readdir(f.directory)).filter((name) => name.endsWith('.source.json')).length, 1);
});


test('JSON singleton keys including nested escaped duplicates are rejected before adapter calls', async (t) => {
  let calls = 0;
  const { base } = await httpFixture(t, { environmentAdapter: { profiles: () => [], plan: async () => { calls++; throw new Error('Must not execute'); } } });
  for (const raw of ['{"name":"a","name":"b"}', '{"runtime":{"node_count":1,"node_count":2}}', '{"plan_id":"a","plan_\\u0069d":"b"}']) {
    const response = await fetch(`${base}/api/v1/plans`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: raw });
    assert.equal(response.status, 422);
    assert.equal((await response.json()).error.code, 'INVALID_INPUT');
  }
  const malformed = await fetch(`${base}/api/v1/plans`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{"name":' });
  assert.equal(malformed.status, 400);
  assert.equal(calls, 0);
});

test('running intent becomes unknown after restart without dispatch or CD replay', async (t) => {
  const f = await fixture(t, { service: { status: async () => ({ state: 'running', status: 'in_progress', conclusion: null }) } });
  const first = await f.product.createDeployment(input, 'interrupted');
  await settle(() => f.product.getDeployment(first.id), (value) => value.ci.run_id === '123');
  await f.product.close();
  const restarted = await createProductService({ service: f.service, directory: f.directory, deployPublished: async () => assert.fail('No replay') });
  try {
    assert.equal((await restarted.getDeployment(first.id)).status, 'unknown');
    assert.equal((await restarted.createDeployment(input, 'interrupted')).id, first.id);
    assert.equal(f.dispatches(), 1);
    assert.equal(f.cdCalls(), 0);
  } finally { await restarted.close(); }
});

test('build POST preserves string run IDs and plan/environment HTTP use 201 then 202', async (t) => {
  const { base } = await httpFixture(t, { environmentAdapter: {
    profiles: () => [{ id: 'registered' }],
    plan: async (value, { id }) => ({ public: { id, ...value, executable: true }, private: { token: 'private-never-publish' } }),
    verifyPlan: async () => {}, execute: async () => ({ status: 'succeeded', stage: 'runtime', deployment_supported: false }),
  } });
  const build = await fetch(`${base}/api/v1/builds`, { method: 'POST', body: form() });
  assert.equal(build.status, 202);
  assert.equal((await build.json()).resource_id, '123');
  assert.equal(build.headers.get('location'), '/api/v1/builds/123');
  const observed = await (await fetch(`${base}/api/v1/builds/123`)).json();
  assert.equal(observed.id, '123'); assert.equal(observed.status, 'published'); assert.equal(observed.url, null);
  let planResponse;
  for (let attempt = 0; attempt < 30; attempt++) {
    planResponse = await fetch(`${base}/api/v1/plans`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"name":"runtime-demo"}' });
    if (planResponse.status !== 409) break;
    await pause(5);
  }
  assert.equal(planResponse.status, 201);
  const plan = await planResponse.json();
  assert.equal(planResponse.headers.get('location'), `/api/v1/plans/${plan.id}`);
  assert.ok(!JSON.stringify(plan).includes('private-never-publish'));
  const create = await fetch(`${base}/api/v1/environments`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': 'environment' }, body: JSON.stringify({ plan_id: plan.id }) });
  assert.equal(create.status, 202);
  const complete = await settle(async () => (await fetch(`${base}${create.headers.get('location')}`)).json());
  assert.equal(complete.status, 'succeeded');
  assert.equal(complete.deployment_supported, false);
});

test('one deployment consumes its app-bound plan before CI, and replay never repeats the DB stack', async (t) => {
  const sequence = [];
  const environmentAdapter = {
    plan: async (value, { id }) => ({ public: { ...value, id, executable: true }, private: { profile: { target: { target_id: input.target_id }, deployment: {} } } }),
    verifyPlan: async () => sequence.push('verify'),
    execute: async (plan, { onProgress }) => { sequence.push('environment'); await onProgress({ status: 'running', stage: 'database' }); return { status: 'succeeded', database: { status: 'succeeded' }, deployment_supported: true, runtime_target_id: input.target_id }; },
    deployPublished: async () => { sequence.push('cd'); return deployed; },
  };
  const f = await fixture(t, { environmentAdapter, service: { deploy: async () => { sequence.push('ci'); return { run_id: 123, source_commit: publication.source_commit }; } } });
  const plan = await f.product.createPlan({ name: input.app, runtime: {}, database: { mode: 'patroni' } });
  await assert.rejects(f.product.createDeployment({ ...input, app: 'wrong-app', plan_id: plan.id }, 'wrong'), { code: 'INVALID_INPUT' });
  const request = { ...input, plan_id: plan.id };
  const created = await f.product.createDeployment(request, 'combined');
  const result = await settle(() => f.product.getDeployment(created.id));
  assert.equal(result.status, 'succeeded'); assert.equal(result.environment.database.status, 'succeeded');
  assert.deepEqual(sequence, ['verify', 'verify', 'environment', 'ci', 'cd']);
  await f.product.createDeployment(request, 'combined');
  assert.deepEqual(sequence, ['verify', 'verify', 'environment', 'ci', 'cd']);
  await assert.rejects(f.product.createDeployment(request, 'different'), { code: 'CONFLICT' });
});

test('failed database environment never dispatches CI or CD', async (t) => {
  const f = await fixture(t, { environmentAdapter: {
    plan: async (value, { id }) => ({ public: { ...value, id }, private: { profile: { target: { target_id: input.target_id }, deployment: {} } } }),
    verifyPlan: async () => {}, deployPublished: async () => assert.fail('must not deploy'),
    execute: async () => ({ status: 'unknown', database: { status: 'unknown' }, deployment_supported: false, error: { code: 'DATABASE_OUTCOME_UNKNOWN', outcome_unknown: true } }),
  } });
  const plan = await f.product.createPlan({ name: input.app });
  const created = await f.product.createDeployment({ ...input, plan_id: plan.id }, 'db-failed');
  const result = await settle(() => f.product.getDeployment(created.id));
  assert.equal(result.status, 'unknown'); assert.equal(f.dispatches(), 0); assert.equal(f.cdCalls(), 0);
});

for (const origin of ['environments', 'deployments']) {
  test(`successful ${origin} registration reuses the app-bound environment through restart`, async (t) => {
    const request = { ...input, app: 'another-app', target_id: 'request-aws-123' };
    let executions = 0, verifications = 0, dispatches = 0, registrations = 0, registrationComplete = false, unknownCd = false;
    const cdEnvironments = [];
    const environmentAdapter = {
      plan: async (value, { id }) => ({ public: { ...value, id, runtime_target_id: request.target_id },
        private: { profile: { provider: 'aws', create_per_request: true, target: { target_id: request.target_id }, deployment: {} } } }),
      verifyPlan: async () => { verifications++; },
      execute: async () => {
        executions++; registrationComplete = true;
        return { status: 'succeeded', deployment_supported: true, runtime_target_id: request.target_id, database: { status: 'succeeded' } };
      },
      deployPublished: async (id) => {
        cdEnvironments.push(id);
        return unknownCd ? { cd: { state: 'unknown', deployed: false }, public_http: { state: 'not_run' }, error: { outcome_unknown: true } } : deployed;
      },
    };
    function ciService() {
      const targetIds = ['demo'];
      return { targetId: 'demo', targetIds,
        allowTarget: (id) => { assert.equal(registrationComplete, true); registrations++; if (!targetIds.includes(id)) targetIds.push(id); },
        deploy: async ({ app, target_id }) => {
          assert.ok(targetIds.includes(target_id)); assert.equal(app, request.app); dispatches++;
          return { run_id: 122 + dispatches, source_commit: publication.source_commit };
        },
        status: async (id, target_id) => {
          assert.ok(targetIds.includes(target_id));
          return { state: 'published', publication: { ...publication, run_id: Number(id), app: request.app, target_id } };
        },
      };
    }
    const f = await fixture(t, { deployPublished: undefined, environmentAdapter, service: ciService() });
    const owner = f.product.dashboard.session(null).id, stranger = f.product.dashboard.session(null).id;
    assert.equal(f.product.targets().some(({ id }) => id === request.target_id), false);
    await assert.rejects(f.product.createDeployment(request, 'unregistered'), { code: 'INVALID_INPUT' });
    const plan = await f.product.createPlan({ name: request.app }, owner);
    const first = origin === 'environments'
      ? await f.product.createEnvironment({ plan_id: plan.id }, 'environment', owner)
      : await f.product.createDeployment({ ...request, plan_id: plan.id }, 'initial', undefined, owner);
    const ready = await settle(() => origin === 'environments' ? f.product.getEnvironment(first.id) : f.product.getDeployment(first.id));
    assert.equal(ready.status, 'succeeded');
    assert.ok(f.product.targets(owner).some(({ id }) => id === request.target_id));
    assert.ok(!f.product.targets(stranger).some(({ id }) => id === request.target_id));
    await assert.rejects(f.product.createDeployment(request, 'foreign', undefined, stranger), { code: 'INVALID_INPUT' });
    await assert.rejects(f.product.createBuild(request, undefined, stranger), { code: 'INVALID_INPUT' });
    const environmentId = origin === 'environments' ? first.id : first.environment_id;
    const registered = f.product.targets().find(({ id }) => id === request.target_id);
    assert.equal(registered.application_name, request.app);
    assert.equal(registered.provider, 'aws'); assert.equal(registered.environment_id, environmentId);
    const observation = await f.product.getTargetObservation(request.target_id, owner);
    assert.equal(observation.environment_id, environmentId); assert.equal(observation.app, request.app);
    assert.equal(observation.metrics.node_up.state, 'not_configured');
    await assert.rejects(f.product.getTargetObservation(request.target_id, stranger), { status: 404 });
    assert.deepEqual(registered.capabilities, { ci_submission: true, application_deployment: true, database_configuration: true });
    await assert.rejects(f.product.createDeployment({ ...request, app: 'wrong-app' }, 'wrong-app'), { code: 'INVALID_INPUT' });
    await assert.rejects(f.product.createBuild({ ...request, app: 'wrong-app' }), { code: 'INVALID_INPUT' });
    await assert.rejects(f.product.createDeployment({ ...request, target_id: 'unknown-target' }, 'unknown'), { code: 'INVALID_INPUT' });
    const reused = await f.product.createDeployment(request, 'reuse');
    assert.equal(reused.environment_id, environmentId);
    assert.equal(Object.hasOwn(reused, 'environment'), false); assert.equal(Object.hasOwn(reused, 'plan_id'), false);
    const reusedComplete = await settle(() => f.product.getDeployment(reused.id));
    assert.equal(reusedComplete.status, 'succeeded');
    assert.equal((await f.product.createDeployment(request, 'reuse')).id, reused.id);
    assert.equal(executions, 1); assert.equal(verifications, origin === 'deployments' ? 2 : 1); assert.equal(registrations, 1);
    await f.product.close();

    const restartedService = ciService();
    const restarted = await createProductService({ service: restartedService, directory: f.directory, environmentAdapter, pollInterval: 5 });
    try {
      assert.deepEqual(restartedService.targetIds, ['demo', request.target_id]);
      assert.ok(restarted.targets(owner).some(({ id }) => id === request.target_id));
      assert.ok(!restarted.targets(stranger).some(({ id }) => id === request.target_id));
      await assert.rejects(restarted.getTargetObservation(request.target_id, stranger), { status: 404 });
      assert.equal((await restarted.getTargetObservation(request.target_id, owner)).environment_id, environmentId);
      await assert.rejects(restarted.createDeployment(request, 'foreign-restart', undefined, stranger), { code: 'INVALID_INPUT' });
      assert.equal(restarted.targets().find(({ id }) => id === request.target_id).capabilities.database_configuration, true);
      assert.equal((await restarted.getBuild(reusedComplete.ci.run_id)).status, 'published');
      assert.equal((await restarted.legacyStatus(reusedComplete.ci.run_id)).state, 'published');
      const afterRestart = await restarted.createDeployment(request, 'after-restart');
      assert.equal(afterRestart.environment_id, environmentId);
      assert.equal((await settle(() => restarted.getDeployment(afterRestart.id))).status, 'succeeded');
      assert.equal(executions, 1); assert.equal(verifications, origin === 'deployments' ? 2 : 1);
      assert.ok(cdEnvironments.length >= 2 && cdEnvironments.every((id) => id === environmentId));
      unknownCd = true;
      const uncertain = await restarted.createDeployment(request, 'uncertain');
      assert.equal((await settle(() => restarted.getDeployment(uncertain.id))).status, 'unknown');
      const dispatchesBeforeRetry = dispatches;
      assert.equal((await restarted.createDeployment(request, 'uncertain')).id, uncertain.id);
      await assert.rejects(restarted.createDeployment(request, 'new-after-unknown'), { code: 'APPLICATION_RECONCILE_REQUIRED' });
      assert.equal(dispatches, dispatchesBeforeRetry); assert.equal(executions, 1);
    } finally { await restarted.close(); }
  });
}

test('dynamic target admission requires the exact private and public plan binding before provisioning', async (t) => {
  for (const mismatch of ['private-target', 'public-target', 'not-per-request', 'invalid-id']) {
    const targetId = mismatch === 'invalid-id' ? '../invalid' : 'request-aws-new';
    const f = await fixture(t, { deployPublished: undefined, service: { allowTarget: () => assert.fail('No registration') }, environmentAdapter: {
      plan: async (value, { id }) => ({ public: { ...value, id, runtime_target_id: mismatch === 'public-target' ? 'different-target' : targetId },
        private: { profile: { create_per_request: mismatch !== 'not-per-request',
          target: { target_id: mismatch === 'private-target' ? 'different-target' : targetId }, deployment: {} } } }),
      verifyPlan: async () => assert.fail('Invalid binding must fail before verification'),
      execute: async () => assert.fail('No provisioning'), deployPublished: async () => assert.fail('No deployment'),
    } });
    const plan = await f.product.createPlan({ name: input.app });
    await assert.rejects(f.product.createDeployment({ ...input, target_id: targetId, plan_id: plan.id }, 'invalid'), { code: 'INVALID_INPUT' });
    assert.equal(f.dispatches(), 0);
  }
});

test('incomplete or mismatched environment registration never admits the dynamic CI target', async (t) => {
  for (const patch of [{ status: 'unknown' }, { status: 'failed' }, { deployment_supported: false }, { runtime_target_id: 'wrong-target' }]) {
    const targetId = 'request-aws-new';
    const f = await fixture(t, { deployPublished: undefined, service: { allowTarget: () => assert.fail('No registration') }, environmentAdapter: {
      plan: async (value, { id }) => ({ public: { ...value, id, runtime_target_id: targetId },
        private: { profile: { create_per_request: true, target: { target_id: targetId }, deployment: {} } } }),
      verifyPlan: async () => {},
      execute: async () => ({ status: 'succeeded', deployment_supported: true, runtime_target_id: targetId, ...patch }),
      deployPublished: async () => assert.fail('No deployment'),
    } });
    const plan = await f.product.createPlan({ name: input.app });
    const created = await f.product.createDeployment({ ...input, target_id: targetId, plan_id: plan.id }, 'incomplete');
    const completed = await settle(() => f.product.getDeployment(created.id));
    assert.notEqual(completed.status, 'succeeded'); assert.equal(f.dispatches(), 0);
    assert.equal(f.product.targets().some(({ id }) => id === targetId || id === 'wrong-target'), false);
  }
});

test('UI environment selection preserves the source app identity and rejects missing or invalid names before execution', async (t) => {
  const contract = JSON.parse(await readFile(new URL('../../../docs/api/product.openapi.json', import.meta.url)));
  for (const route of Object.keys(contract.paths)) {
    assert.match(route, /^\/api\/v1(?:\/(?:[a-z]+|\{[a-z]+\}))+$/, `Nonconforming product route: ${route}`);
  }
  const submitted = [], deliveries = [];
  const deployPublished = async (value) => { deliveries.push(value); return deployed; };
  deployPublished.targets = { demo: { applicationName: 'calculator' } };
  const { base } = await httpFixture(t, { target: { provider: 'aws' }, deployPublished,
    service: { targetId: 'demo', deploy: async (value) => { submitted.push(value); return { run_id: '123', source_commit: publication.source_commit }; },
      status: async () => ({ run_id: 123, state: 'published', publication: { ...publication, app: 'calculator' } }) } });
  const options = await (await fetch(`${base}/api/v1/options`)).json();
  assert.deepEqual(options.items.map(({ provider, available }) => [provider, available]), [['aws', true], ['gcp', false], ['openstack', false], ['proxmox', false]]);
  const selection = () => { const value = form(); value.delete('app'); value.delete('target_id'); value.set('environment', 'cloud'); value.set('provider', 'aws'); value.set('source_name', 'Calculator'); return value; };
  for (const [mutate, status] of [
    [(value) => { value.set('environment', 'onprem'); value.set('provider', 'openstack'); }, 409],
    [(value) => { value.set('environment', 'onprem'); value.set('provider', 'proxmox'); }, 409],
    [(value) => value.set('provider', 'gcp'), 409],
    [(value) => { value.set('environment', 'onprem'); value.set('provider', 'gcp'); }, 422],
    [(value) => value.set('target_id', 'foreign'), 422],
    [(value) => value.set('app', 'foreign-app'), 422],
    [(value) => value.set('plan_id', 'foreign-plan'), 422],
    [(value) => value.append('provider', 'proxmox'), 422],
    [(value) => value.set('expected_target_id', 'foreign'), 409],
    [(value) => value.set('expected_target_id', '../foreign'), 422],
    [(value) => value.set('source_name', 'x'.repeat(256)), 422],
    [(value) => value.set('source_name', 'different-source'), 422],
    [(value) => value.delete('source_name'), 422],
    ...['', ' ', '한글', 'ab', '123calculator'].map((name) => [(value) => value.set('source_name', name), 422]),
  ]) {
    const body = selection(); mutate(body);
    const response = await fetch(`${base}/api/v1/deployments`, { method: 'POST', body, headers: { 'Idempotency-Key': 'selection' } });
    assert.equal(response.status, status, await response.text());
  }
  assert.equal(submitted.length, 0);
  assert.equal(deliveries.length, 0);
  const post = () => fetch(`${base}/api/v1/deployments`, { method: 'POST', body: selection(), headers: { 'Idempotency-Key': 'selection' } });
  const accepted = await post(); assert.equal(accepted.status, 202);
  const completed = await settle(async () => (await fetch(`${base}${accepted.headers.get('location')}`)).json());
  assert.equal(completed.status, 'succeeded'); assert.equal(completed.app, 'calculator'); assert.equal(completed.target_id, 'demo');
  assert.equal((await post()).status, 200); assert.equal(submitted.length, 1);
  assert.equal(submitted[0].app, 'calculator'); assert.equal(submitted[0].target_id, 'demo');
  assert.equal(deliveries.length, 1); assert.equal(deliveries[0].app, 'calculator');
  const unregistered = await fixture(t);
  assert.ok(unregistered.product.deploymentOptions().every((option) => !option.available), 'unknown provider never becomes AWS');
  const onprem = await fixture(t, { target: { provider: 'openstack' } });
  assert.deepEqual(onprem.product.deploymentOptions().filter((option) => option.available).map((option) => option.provider), ['openstack']);
});

test('HTTP provider selection rejects other source apps before fetching or dispatching and preserves registered app updates', async (t) => {
  const previous = process.env.RAILSHOT_PROVIDER_TARGETS;
  process.env.RAILSHOT_PROVIDER_TARGETS = JSON.stringify({ gcp: 'stack-gcp', openstack: 'stack-openstack' });
  t.after(() => { if (previous === undefined) delete process.env.RAILSHOT_PROVIDER_TARGETS; else process.env.RAILSHOT_PROVIDER_TARGETS = previous; });
  const submissions = [], deliveries = [], fetched = [], runs = new Map();
  const deployPublished = async ({ app, targetId }) => {
    deliveries.push({ app, targetId });
    return { ...deployed, public_http: { ...deployed.public_http, url: `https://${targetId}.example.test` } };
  };
  deployPublished.targets = { demo: { applicationName: 'demo-app' }, 'stack-gcp': { applicationName: 'gcp-app' }, 'stack-openstack': { applicationName: 'openstack-app' } };
  const { base } = await httpFixture(t, { target: { provider: 'aws' }, deployPublished,
    sourceLoader: async (url) => { fetched.push(url); return { files }; },
    service: { targetId: 'demo', targetIds: ['demo', 'stack-gcp', 'stack-openstack'],
      deploy: async (value) => {
        submissions.push({ app: value.app, targetId: value.target_id });
        const runId = String(123 + runs.size);
        runs.set(runId, { ...publication, run_id: runId, app: value.app, target_id: value.target_id });
        return { run_id: runId, source_commit: publication.source_commit };
      },
      status: async (runId, targetId) => {
        assert.equal(runs.get(runId).target_id, targetId);
        return { state: 'published', publication: runs.get(runId) };
      } } });
  const options = await (await fetch(`${base}/api/v1/options`)).json();
  assert.deepEqual(options.items.map(({ provider, available }) => [provider, available]), [['aws', true], ['gcp', true], ['openstack', true], ['proxmox', false]]);
  const targets = await (await fetch(`${base}/api/v1/targets`)).json();
  assert.deepEqual(targets.items.map(({ id, provider }) => [id, provider]), [['demo', 'aws'], ['stack-gcp', 'gcp'], ['stack-openstack', 'openstack']]);
  for (const [provider, environment, id, app] of [['aws', 'cloud', 'demo', 'demo-app'], ['gcp', 'cloud', 'stack-gcp', 'gcp-app'], ['openstack', 'onprem', 'stack-openstack', 'openstack-app']]) {
    const before = [submissions.length, deliveries.length];
    for (const sourceType of ['folder', 'github']) {
      const body = form(); body.delete('app'); body.delete('target_id');
      body.set('provider', provider); body.set('environment', environment);
      if (sourceType === 'github') {
        body.delete('files'); body.delete('paths'); body.set('source_type', 'github');
        body.set('repository_url', 'https://github.com/example/different-source');
      } else body.set('source_name', 'different-source');
      const rejected = await fetch(`${base}/api/v1/deployments`, { method: 'POST', body, headers: { 'Idempotency-Key': `wrong-${provider}-${sourceType}` } });
      assert.equal(rejected.status, 422, await rejected.text());
      assert.deepEqual([submissions.length, deliveries.length], before);
      assert.equal(fetched.length, 0);
    }
    const post = () => {
      const body = form(); body.delete('app'); body.delete('target_id');
      body.set('provider', provider); body.set('environment', environment); body.set('source_name', app.toUpperCase());
      body.set('expected_target_id', id);
      return fetch(`${base}/api/v1/deployments`, { method: 'POST', body, headers: { 'Idempotency-Key': `multi-${provider}` } });
    };
    const accepted = await post(); assert.equal(accepted.status, 202, await accepted.text());
    const completed = await settle(async () => (await fetch(`${base}${accepted.headers.get('location')}`)).json());
    assert.equal(completed.status, 'succeeded'); assert.equal(completed.target_id, id); assert.equal(completed.app, app);
    assert.deepEqual(completed.deployment_selection, { environment, provider });
    assert.equal(completed.url, `https://${id}.example.test`);
    const replay = await post(); assert.equal(replay.status, 200); assert.equal((await replay.json()).id, completed.id);
  }
  assert.deepEqual(submissions, [{ app: 'demo-app', targetId: 'demo' }, { app: 'gcp-app', targetId: 'stack-gcp' }, { app: 'openstack-app', targetId: 'stack-openstack' }]);
  assert.deepEqual(deliveries, submissions);
});

test('provider metadata never admits an additional target without both CI permission and a CD registration', async (t) => {
  for (const [name, permitted, registered] of [['missing-ci', false, true], ['missing-cd', true, false], ['legacy-adapter', true, null]]) {
    await t.test(name, async (t) => {
      const deployPublished = async () => assert.fail('Unavailable selection must not invoke CD');
      if (registered !== null) deployPublished.targets = { demo: { applicationName: 'demo-app' },
        ...(registered ? { 'stack-openstack': { applicationName: 'openstack-app' } } : {}) };
      const f = await fixture(t, { target: { provider: 'aws' }, providerTargets: { openstack: 'stack-openstack' }, deployPublished,
        service: { targetIds: permitted ? ['demo', 'stack-openstack'] : ['demo'] } });
      assert.equal(f.product.deploymentOptions().find(({ provider }) => provider === 'aws').available, true);
      assert.equal(f.product.deploymentOptions().find(({ provider }) => provider === 'openstack').available, false);
      await assert.rejects(f.product.createDeployment({ source_type: 'folder', files,
        deployment_selection: { environment: 'onprem', provider: 'openstack' } }, 'unavailable'), { code: 'CAPABILITY_UNAVAILABLE' });
      assert.equal(f.dispatches(), 0);
    });
  }
  const deployPublished = async () => deployed;
  deployPublished.targets = { demo: { applicationName: 'demo-app' } };
  const f = await fixture(t, { target: { provider: 'aws' }, deployPublished, service: { targetIds: [] } });
  assert.ok(f.product.deploymentOptions().every(({ available }) => !available), 'the legacy default also requires CI admission');
});

test('provider target configuration rejects ambiguous identities and cannot replace the existing default', async () => {
  for (const providerTargets of [null, [], 'openstack', { openstack: '../foreign' }, { unknown: 'stack-openstack' },
    { aws: 'replacement' }, { openstack: 'demo' }, { openstack: 'stack-openstack', proxmox: 'stack-openstack' }]) {
    await assert.rejects(createProductService({ service: { targetId: 'demo' }, target: { provider: 'aws' }, providerTargets }), { code: 'INVALID_INPUT' });
  }
});

test('HTTP deployment accepts a dynamic app-bound plan alongside existing environment options and replays once', async (t) => {
  const app = 'fresh-app', targetId = 'request-aws-new', targetIds = ['demo'];
  const sequence = [];
  const deployPublished = async () => assert.fail('Dynamic plan uses its environment CD binding');
  deployPublished.targets = { demo: { applicationName: 'demo-app' } };
  const environmentAdapter = {
    plan: async (value, { id }) => ({ public: { ...value, id, runtime_target_id: targetId, executable: true },
      private: { profile: { create_per_request: true, target: { target_id: targetId }, deployment: {} } } }),
    verifyPlan: async () => sequence.push('verify'),
    execute: async () => { sequence.push('environment'); return { status: 'succeeded', deployment_supported: true,
      runtime_target_id: targetId, database: { status: 'succeeded' } }; },
    deployPublished: async () => { sequence.push('cd'); return deployed; },
  };
  const { base } = await httpFixture(t, { target: { provider: 'aws' }, deployPublished, environmentAdapter,
    service: { targetId: 'demo', targetIds, allowTarget: (id) => targetIds.push(id),
      deploy: async (value) => { assert.equal(value.app, app); assert.equal(value.target_id, targetId);
        assert.ok(targetIds.includes(targetId)); sequence.push('ci'); return { run_id: 123, source_commit: publication.source_commit }; },
      status: async () => ({ state: 'published', publication: { ...publication, app, target_id: targetId } }) } });
  const options = await (await fetch(`${base}/api/v1/options`)).json();
  assert.equal(options.items.find(({ provider }) => provider === 'aws').available, true);
  const planned = await fetch(`${base}/api/v1/plans`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: app }) });
  assert.equal(planned.status, 201);
  const plan = await planned.json();
  const source = () => { const value = form(); value.set('app', plan.name); value.set('target_id', plan.runtime_target_id); value.set('plan_id', plan.id); return value; };
  assert.equal((await fetch(`${base}/api/v1/builds`, { method: 'POST', body: source() })).status, 422);
  assert.deepEqual(sequence, []);
  const post = () => fetch(`${base}/api/v1/deployments`, { method: 'POST', body: source(), headers: { 'Idempotency-Key': 'dynamic-plan' } });
  const accepted = await post(); assert.equal(accepted.status, 202);
  const complete = await settle(async () => (await fetch(`${base}${accepted.headers.get('location')}`)).json());
  assert.equal(complete.status, 'succeeded'); assert.equal(complete.app, app); assert.equal(complete.target_id, targetId);
  assert.equal(complete.plan_id, plan.id); assert.equal(complete.environment.database.status, 'succeeded');
  assert.deepEqual(sequence, ['verify', 'verify', 'environment', 'ci', 'cd']);
  const replay = await post(); assert.equal(replay.status, 200); assert.equal((await replay.json()).id, complete.id);
  assert.deepEqual(sequence, ['verify', 'verify', 'environment', 'ci', 'cd']);
});

test('CI diagnostics preserve blocked and unknown outcomes without replaying execution', async (t) => {
  for (const outcome of ['BLOCKED', 'UNKNOWN']) {
    const diagnostics = { state: 'ready', reason: 'NO_TESTS', code: outcome === 'UNKNOWN' ? 'SDK_OUTCOME_UNKNOWN' : 'GATE_CONFIG_INVALID',
      outcome, phase: 'Q.discovery', message: '테스트를 찾지 못했습니다.', guidance: '수정 결과를 확인하세요.' };
    const steps = [{ key: 'loop', status: 'completed', conclusion: 'failure', tasks: [{ number: 2, name: 'Baseline gates, then SDK repair only when eligible', conclusion: 'failure' }] }];
    const f = await fixture(t, { service: { status: async () => ({ run_id: 123, state: 'failed', diagnostics, steps, message: 'CI 실패' }) } });
    const accepted = await f.product.createDeployment(input, 'diagnostic');
    const record = await settle(() => f.product.getDeployment(accepted.id));
    assert.equal(record.status, outcome === 'UNKNOWN' ? 'unknown' : 'blocked');
    assert.equal(record.error.outcome_unknown, outcome === 'UNKNOWN');
    assert.equal(record.error.message, '테스트를 찾지 못했습니다. 수정 결과를 확인하세요.');
    assert.deepEqual(record.ci.diagnostics, diagnostics); assert.deepEqual(record.ci.steps, steps);
    assert.equal(f.dispatches(), 1); assert.equal(f.cdCalls(), 0);
    await f.product.getDeployment(accepted.id);
    assert.equal(f.dispatches(), 1);
    if (outcome === 'UNKNOWN') await assert.rejects(f.product.createDeployment(input, 'another'), { code: 'APPLICATION_RECONCILE_REQUIRED' });
  }
});

test('app logs enforce session ownership, deployed state and latest shared-app ownership', async (t) => {
  let logReads = 0, run = 122;
  const f = await fixture(t, { observeLogs: async (record) => { logReads++; return { deployment_id: record.id, target_id: record.target_id,
    app: record.app, state: 'ready', checked_at: new Date().toISOString(), entries: [{ pod: 'demo-pod', container: 'app', text: 'GET /health 200' }] }; },
    service: { deploy: async () => ({ run_id: ++run, source_commit: publication.source_commit }),
      status: async (id) => ({ run_id: id, state: 'published', publication: { ...publication, run_id: id } }) } });
  const owner = f.product.dashboard.session().id, stranger = f.product.dashboard.session().id;
  const first = await f.product.createDeployment(input, 'first', undefined, owner);
  assert.equal((await f.product.getDeploymentLogs(first.id, owner)).state, 'not_deployed');
  await settle(() => f.product.getDeployment(first.id, owner));
  assert.equal((await f.product.getDeploymentLogs(first.id, owner)).state, 'ready');
  await assert.rejects(f.product.getDeploymentLogs(first.id, stranger), { status: 404 });
  assert.equal(logReads, 1);
  const second = await f.product.createDeployment(input, 'second', undefined, stranger);
  await settle(() => f.product.getDeployment(second.id, stranger));
  assert.equal((await f.product.getDeploymentLogs(first.id, owner)).state, 'superseded');
  assert.equal(logReads, 1, 'superseded session never reads the new deployment logs');
  assert.equal((await f.product.getDeploymentLogs(second.id, stranger)).state, 'ready');
});

test('deployment logs route rejects mutation and caller-provided query controls', async (t) => {
  const { base } = await httpFixture(t);
  const accepted = await fetch(`${base}/api/v1/deployments`, { method: 'POST', body: form(), headers: { 'Idempotency-Key': 'log-route' } });
  const path = accepted.headers.get('location');
  await settle(async () => (await fetch(base + path)).json());
  const response = await fetch(base + path + '/logs');
  assert.equal(response.status, 200);
  assert.equal((await response.json()).state, 'not_configured');
  assert.equal((await fetch(base + path + '/logs?pod=foreign')).status, 422);
  assert.equal((await fetch(base + path + '/logs', { method: 'POST' })).status, 405);
  assert.equal((await fetch(base + '/api/v1/deployments/foreign/logs')).status, 404);
});

test('app logs discard an in-flight read when another session admits CD for the same app', async (t) => {
  let releaseLogs, releaseCd, beganLogs, beganCd, calls = 0, run = 122;
  const waitingLogs = new Promise((resolve) => { releaseLogs = resolve; });
  const waitingCd = new Promise((resolve) => { releaseCd = resolve; });
  const enteredLogs = new Promise((resolve) => { beganLogs = resolve; });
  const enteredCd = new Promise((resolve) => { beganCd = resolve; });
  const f = await fixture(t, { observeLogs: async (record) => {
    beganLogs(); await waitingLogs;
    return { deployment_id: record.id, state: 'ready', entries: [{ pod: 'shared-pod', container: 'app', text: 'new session output' }] };
  }, deployPublished: async () => {
    if (++calls === 2) { beganCd(); await waitingCd; }
    return deployed; // Reusing the same image/revision must still revoke the old session's log read.
  }, service: { deploy: async () => ({ run_id: ++run, source_commit: publication.source_commit }),
    status: async (id) => ({ run_id: id, state: 'published', publication: { ...publication, run_id: id } }) } });
  const owner = f.product.dashboard.session().id, other = f.product.dashboard.session().id;
  try {
    const first = await f.product.createDeployment(input, 'first-race', undefined, owner);
    await settle(() => f.product.getDeployment(first.id, owner));
    const logs = f.product.getDeploymentLogs(first.id, owner);
    await enteredLogs;
    const second = await f.product.createDeployment(input, 'second-race', undefined, other);
    await enteredCd;
    releaseLogs();
    const result = await logs;
    assert.equal(result.state, 'superseded'); assert.deepEqual(result.entries, []);
    assert.equal((await f.product.getDeploymentLogs(first.id, owner)).state, 'superseded');
    releaseCd();
    await settle(() => f.product.getDeployment(second.id, other));
  } finally { releaseLogs(); releaseCd(); }
});

test('agent events are not fabricated before dispatch and require the persisted run binding after restart', async (t) => {
  let release, reads = 0;
  const waiting = new Promise((resolve) => { release = resolve; });
  const f = await fixture(t, { service: {
    deploy: async () => { await waiting; return { run_id: 123, source_commit: publication.source_commit }; },
    events: async () => { reads++; return { state: 'live', items: [] }; },
  } });
  const owner = f.product.dashboard.session().id;
  const created = await f.product.createDeployment(input, 'event-binding', undefined, owner);
  try {
    const pending = await f.product.getDeploymentEvents(created.id, owner);
    assert.equal(pending.state, 'not_started'); assert.equal(pending.reason, 'not_dispatched');
    assert.deepEqual(pending.items, []); assert.equal(reads, 0);
  } finally { release(); }
  await settle(() => f.product.getDeployment(created.id, owner));
  await f.product.close();
  const store = await createProductStore(f.directory);
  await store.transaction((state) => { state.bindings['123'].source_commit = 'd'.repeat(40); });
  await store.close();
  const authorizedReads = reads;
  const restarted = await createProductService({ service: f.service, directory: f.directory });
  try {
    const rejected = await restarted.getDeploymentEvents(created.id, owner);
    assert.equal(rejected.state, 'unavailable'); assert.equal(rejected.reason, 'binding_mismatch');
    assert.equal(reads, authorizedReads);
    await assert.rejects(restarted.getDeploymentEvents(created.id, 'foreign'), { status: 404 });
  } finally { await restarted.close(); }
});

test('HTTP target observations authorize IDs before collecting and need no deployment history', async (t) => {
  const calls = [];
  const deployPublished = async () => assert.fail('read-only observation dispatched CD');
  deployPublished.targets = { demo: { applicationName: 'demo-app' }, 'stack-gcp': { applicationName: 'gcp-app' }, 'stack-openstack': { applicationName: 'openstack-app' } };
  const { base } = await httpFixture(t, { target: { provider: 'aws' }, providerTargets: { gcp: 'stack-gcp', openstack: 'stack-openstack' }, deployPublished,
    service: { targetId: 'demo', targetIds: ['demo', 'stack-gcp', 'stack-openstack'], deploy: async () => assert.fail('read-only observation dispatched CI') },
    observeMetrics: async (record) => {
      calls.push(record);
      return { deployment_id: null, target_id: record.target_id, app: record.app, checked_at: new Date().toISOString(), stale_after_seconds: 90,
        metrics: {
          node_up: { state: 'collection_failed', value: null, observed_at: '2026-10-03T00:00:00.000Z', scope: 'target_node' },
          runtime_healthz: { state: record.target_id === 'stack-openstack' ? 'stale' : 'ready', value: record.target_id === 'stack-gcp' ? 0 : record.target_id === 'stack-openstack' ? null : 1, observed_at: '2026-10-03T00:00:00.000Z', scope: 'target_runtime' },
        } };
    } });
  for (const [id, app] of [['demo', 'demo-app'], ['stack-gcp', 'gcp-app'], ['stack-openstack', 'openstack-app']]) {
    const response = await fetch(`${base}/api/v1/targets/${id}/observations`), body = await response.json();
    assert.equal(response.status, 200); assert.equal(response.headers.get('cache-control'), 'no-store'); assert.ok(response.headers.get('x-request-id'));
    assert.equal(body.target_id, id); assert.equal(body.app, app); assert.equal(body.environment_id, null); assert.equal(body.deployment_id, null);
    assert.deepEqual(body.runtime, { status: id === 'demo' ? 'healthy' : id === 'stack-gcp' ? 'unhealthy' : 'unknown',
      observation_state: body.metrics.runtime_healthz.state, observed_at: body.metrics.runtime_healthz.observed_at });
  }
  for (const id of ['unknown', '__proto__', 'BAD-ID']) assert.equal((await fetch(`${base}/api/v1/targets/${id}/observations`)).status, 404);
  assert.equal((await fetch(`${base}/api/v1/targets/demo/observations?app=another-app`)).status, 422);
  const method = await fetch(`${base}/api/v1/targets/demo/observations`, { method: 'POST' });
  assert.equal(method.status, 405); assert.equal(method.headers.get('allow'), 'GET'); assert.equal(calls.length, 3);
});

async function applicationFixture(t, { registrationStatus = 'succeeded', publicationChange = {}, openstackIngress, unknownGraceMs = 60000 } = {}) {
  const home = await realpath(await mkdtemp(join(tmpdir(), 'railshot-product-apps-')));
  t.after(() => rm(home, { recursive: true, force: true }));
  const configPath = join(home, 'environments.json');
  await writeFile(configPath, JSON.stringify({ version: 1, state_dir: join(home, 'registrations'), environments: {
    'runtime-aws': { provider: 'aws', tenant: 'team', source_repository: 'example/apps', ingress: { edge_config_file: '/private/aws-edge.json', dns_config_file: '/private/dns.json' } },
    'runtime-gcp': { provider: 'gcp', tenant: 'team', source_repository: 'example/apps', ingress: { edge_config_file: '/private/gcp-edge.json', dns_config_file: '/private/dns.json' } },
    'runtime-openstack': { provider: 'openstack', tenant: 'team', source_repository: 'example/apps', ingress: openstackIngress },
  } }), { mode: 0o600 });
  const registrations = [], submissions = [], deliveries = [], runs = new Map(), allowed = [];
  const adapter = await createApplicationAdapter({ configPath, ciIdentity: { tenant: 'team', sourceRepository: 'example/apps' }, loadPublished: async () => assert.fail('CD stub consumes the bound publication'),
    runner: async (_python, args) => {
      assert.ok(args[0].endsWith('/applications.py'));
      const request = JSON.parse(await readFile(args[args.indexOf('--request') + 1], 'utf8'));
      registrations.push(request);
      return { status: registrationStatus, application_id: request.application_id, target_id: request.application_id,
        environment_id: request.environment_id, app: request.app, namespace: `app-${request.app}`,
        node_port: 31000 + registrations.length, hostname: `${request.app}.example.test`,
        ...(registrationStatus === 'unknown' ? { error: { code: 'APPLICATION_RECONCILE_REQUIRED' } } : {}) };
    } });
  adapter.deployPublished = async (application, args) => {
    deliveries.push({ application, args });
    assert.equal(application.app, args.app); assert.equal(application.target_id, args.targetId);
    assert.equal(args.publication.app, args.app); assert.equal(args.publication.target_id, args.targetId);
    assert.equal(args.publication.source_commit, args.sourceCommit);
    return { ...deployed, public_http: { ...deployed.public_http, site_url: `https://${args.app}.example.test/` } };
  };
  adapter.observePublished = undefined; // This fixture stubs CD without creating a native CD journal.
  const target = { id: 'runtime-aws', provider: 'aws' };
  const providerTargets = { gcp: 'runtime-gcp', openstack: 'runtime-openstack' };
  const legacy = Object.assign(async () => assert.fail('A new application must use its own CD binding'), { targets: {} });
  const service = { targetId: 'runtime-aws', targetIds: [],
    allowTarget: (id) => { if (!allowed.includes(id)) allowed.push(id); },
    deploy: async (value) => {
      assert.ok(allowed.includes(value.target_id), 'registration precedes CI admission');
      submissions.push(value);
      const runId = String(1000 + submissions.length);
      const source = String(submissions.length % 10).repeat(40);
      runs.set(runId, { ...publication, tenant: 'team', run_id: runId, app: value.app, target_id: value.target_id, source_commit: source,
        images: { web: `ghcr.io/example/apps@sha256:${'c'.repeat(64)}` }, ...publicationChange });
      return { run_id: runId, source_commit: source };
    }, status: async (id, targetId) => {
      assert.equal(submissions[Number(id) - 1001].target_id, targetId);
      return { state: 'published', publication: runs.get(id) };
    } };
  const options = { service, target, providerTargets, applicationAdapter: adapter, deployPublished: legacy, pollInterval: 5, unknownGraceMs };
  const f = await fixture(t, options);
  return { ...f, options: { ...options, service: f.service, directory: f.directory }, adapter,
    registrations, submissions, deliveries, allowed, runs };
}
const applicationSource = (name, provider = 'aws') => ({ source_name: name, source_type: 'folder',
  files: [{ path: 'app.js', content: Buffer.from(`user source for ${name}`) }],
  deployment_selection: { environment: provider === 'openstack' ? 'onprem' : 'cloud', provider } });

test('unknown published delivery fences its environment while uncertain CI still fences the shared app source', async (t) => {
  for (const phase of ['cd', 'ci']) await t.test(phase, async (t) => {
    const f = await applicationFixture(t, { unknownGraceMs: 1 }), owner = f.product.dashboard.session().id;
    const deliver = f.adapter.deployPublished, submit = f.service.deploy;
    if (phase === 'cd') f.adapter.deployPublished = async (app, ...args) => {
      if (app.environment_target_id === 'runtime-gcp') throw new EnvironmentError('APPLICATION_ROUTE_RECONCILE_REQUIRED', 502, true);
      return deliver(app, ...args);
    };
    else f.service.deploy = async () => { throw new Error('unknown CI dispatch'); };
    const blocked = await f.product.createDeployment(applicationSource('same-app', 'gcp'), 'gcp-unknown', undefined, owner);
    const prior = await settle(() => f.product.getDeployment(blocked.id, owner));
    assert.equal(prior.status, 'unknown'); assert.equal(prior.stage, phase);
    await assert.rejects(f.product.createDeployment(applicationSource('same-app', 'gcp'), 'same-env', undefined, owner), { code: 'APPLICATION_RECONCILE_REQUIRED' });
    f.service.deploy = submit;
    if (phase === 'ci') {
      await assert.rejects(f.product.createDeployment(applicationSource('same-app', 'aws'), 'aws-after-ci', undefined, owner), { code: 'APPLICATION_RECONCILE_REQUIRED' });
    } else {
      const accepted = await f.product.createDeployment(applicationSource('same-app', 'aws'), 'aws-after-cd', undefined, owner);
      const result = await settle(() => f.product.getDeployment(accepted.id, owner));
      assert.equal(result.status, 'succeeded');
      assert.notEqual(result.application_id, prior.application_id);
      assert.equal(f.product.resolveApplication({ environment: 'cloud', provider: 'aws', app: 'same-app' }, owner).application.id, result.application_id);
      f.service.sourceFiles = async () => applicationSource('same-app').files;
      const preview = await f.product.createUpdate(result.application_id, { source_type: 'folder', files: applicationSource('same-app').files }, 'aws-update', undefined, owner);
      assert.equal(preview.no_changes, true);
      assert.equal((await f.product.startUpdate(preview.id, {}, owner)).status, 'unchanged');
      assert.equal((await f.product.getDeployment(blocked.id, owner)).status, 'unknown');
    }
  });
});

test('empty application registry accepts two user apps on one existing environment and reuses each binding after restart', async (t) => {
  const f = await applicationFixture(t), owner = f.product.dashboard.session().id;
  assert.deepEqual(f.product.applications(owner), []);
  assert.ok(f.product.targets(owner).every((row) => row.deployment_scope === 'environment' && !row.application_name));
  const completed = [];
  for (const app of ['calculator', 'notes']) {
    const accepted = await f.product.createDeployment(applicationSource(app), app, undefined, owner);
    const result = await settle(() => f.product.getDeployment(accepted.id, owner));
    assert.equal(result.status, 'succeeded'); assert.equal(result.app, app);
    assert.equal(result.environment_target_id, 'runtime-aws'); assert.equal(result.target_id, result.application_id);
    assert.equal(result.url, `https://${app}.example.test/`); completed.push(result);
  }
  assert.notEqual(completed[0].target_id, completed[1].target_id);
  assert.equal(f.product.applications(owner).length, 2);
  assert.deepEqual(f.submissions.map((row) => [row.app, row.target_id, row.files[0].content.toString()]),
    completed.map((row) => [row.app, row.target_id, `user source for ${row.app}`]));
  assert.deepEqual(f.deliveries.map(({ args }) => [args.app, args.targetId]), completed.map((row) => [row.app, row.target_id]));
  await f.product.close(); f.allowed.length = 0;
  const restarted = await createProductService(f.options);
  try {
    assert.deepEqual(new Set(f.allowed), new Set(completed.map((row) => row.target_id)));
    assert.equal((await restarted.createDeployment(applicationSource('calculator'), 'calculator', undefined, owner)).id, completed[0].id);
    assert.equal(f.submissions.length, 2);
    const accepted = await restarted.createDeployment(applicationSource('calculator'), 'calculator-v2', undefined, owner);
    const again = await settle(() => restarted.getDeployment(accepted.id, owner));
    assert.equal(again.status, 'succeeded'); assert.equal(again.application_id, completed[0].application_id);
    assert.equal(again.target_id, completed[0].target_id); assert.equal(restarted.applications(owner).length, 2);
    assert.equal(new Set(f.registrations.map((row) => row.application_id)).size, 2);
  } finally { await restarted.close(); }
});

test('application names are session-owned while separate providers receive separate app targets', async (t) => {
  const f = await applicationFixture(t), owner = f.product.dashboard.session().id, other = f.product.dashboard.session().id;
  const first = await f.product.createDeployment(applicationSource('calculator'), 'first', undefined, owner);
  await settle(() => f.product.getDeployment(first.id, owner));
  await assert.rejects(f.product.createDeployment(applicationSource('calculator'), 'foreign', undefined, other),
    { status: 409, code: 'APPLICATION_OWNERSHIP_CONFLICT' });
  assert.deepEqual(f.product.applications(other), []);
  assert.throws(() => f.product.getApplication(first.application_id, other), { status: 404 });
  assert.equal(f.registrations.length, 1); assert.equal(f.submissions.length, 1);
  for (const provider of ['gcp']) {
    const accepted = await f.product.createDeployment(applicationSource('calculator', provider), provider, undefined, owner);
    assert.equal((await settle(() => f.product.getDeployment(accepted.id, owner))).status, 'succeeded');
  }
  await assert.rejects(f.product.createDeployment(applicationSource('calculator', 'openstack'), 'not-connected', undefined, owner),
    { status: 409, code: 'CAPABILITY_UNAVAILABLE' });
  assert.equal(new Set(f.submissions.map((row) => row.target_id)).size, 2);
  assert.deepEqual(f.registrations.map((row) => row.environment_id), ['runtime-aws', 'runtime-gcp']);
});

test('configured OpenStack accepts the uploaded app and binds its publication to that environment', async (t) => {
  const f = await applicationFixture(t, { openstackIngress: { edge_config_file: '/private/openstack.json',
    dns_config_file: '/private/dns.json', tunnel_config_file: '/private/tunnel.json' } });
  const owner = f.product.dashboard.session().id;
  assert.equal(f.product.deploymentOptions().find((row) => row.provider === 'openstack').available, true);
  const accepted = await f.product.createDeployment(applicationSource('calculator', 'openstack'), 'onprem', undefined, owner);
  const result = await settle(() => f.product.getDeployment(accepted.id, owner));
  assert.equal(result.status, 'succeeded');
  assert.equal(result.environment_target_id, 'runtime-openstack');
  assert.equal(result.target_id, result.application_id);
  assert.deepEqual(f.registrations.map((row) => row.environment_id), ['runtime-openstack']);
  assert.equal(f.deliveries[0].args.targetId, result.application_id);
});

test('unknown application registration survives restart without CI or CD dispatch', async (t) => {
  const f = await applicationFixture(t, { registrationStatus: 'unknown' }), owner = f.product.dashboard.session().id;
  const accepted = await f.product.createDeployment(applicationSource('calculator'), 'uncertain-app', undefined, owner);
  const outcome = await settle(() => f.product.getDeployment(accepted.id, owner));
  assert.equal(outcome.status, 'unknown'); assert.equal(outcome.error.code, 'APPLICATION_RECONCILE_REQUIRED');
  assert.equal(f.product.getApplication(accepted.application_id, owner).status, 'unknown');
  assert.equal(f.submissions.length, 0); assert.equal(f.deliveries.length, 0); assert.deepEqual(f.allowed, []);
  await f.product.close();
  const restarted = await createProductService(f.options);
  try {
    assert.equal((await restarted.createDeployment(applicationSource('calculator'), 'uncertain-app', undefined, owner)).id, accepted.id);
    await assert.rejects(restarted.createDeployment(applicationSource('calculator'), 'retry', undefined, owner),
      { status: 409, code: 'APPLICATION_RECONCILE_REQUIRED' });
    assert.equal(f.registrations.length, 1); assert.equal(f.submissions.length, 0); assert.deepEqual(f.allowed, []);
  } finally { await restarted.close(); }
});

test('new app publication cannot substitute source app target or run before CD', async (t) => {
  for (const field of ['source_commit', 'app', 'target_id', 'run_id']) {
    await t.test(field, async (t) => {
      const f = await applicationFixture(t, { publicationChange: { [field]: field === 'run_id' ? '999' : 'foreign' } });
      const accepted = await f.product.createDeployment(applicationSource('calculator'), field);
      const result = await settle(() => f.product.getDeployment(accepted.id));
      assert.equal(result.status, 'blocked'); assert.equal(result.error.code, 'CI_BINDING_MISMATCH'); assert.equal(result.url, null);
      assert.equal(f.submissions.length, 1); assert.equal(f.deliveries.length, 0);
    });
  }
  const f = await applicationFixture(t);
  await assert.rejects(f.product.createDeployment({ ...applicationSource('calculator'), app: 'replacement' }, 'forged'), { status: 422 });
  await assert.rejects(f.product.createDeployment({ app: 'calculator', target_id: 'app-unregistered', source_type: 'folder', files }, 'target'), { status: 422 });
  assert.equal(f.registrations.length, 0); assert.equal(f.submissions.length, 0);
});

test('HTTP applications are read-only, session-owned and paginated after automatic app registration', async (t) => {
  const f = await applicationFixture(t);
  const { base } = await httpFixture(t, { product: f.product });
  const session = async () => {
    const response = await fetch(`${base}/api/v1/sessions`, { method: 'POST' });
    assert.equal(response.status, 201);
    return { cookie: response.headers.get('set-cookie').split(';')[0] };
  };
  const owner = await session(), stranger = await session();
  const list = (query = '', headers = owner) => fetch(`${base}/api/v1/applications${query}`, { headers });
  const empty = await list();
  assert.equal(empty.status, 200); assert.equal(empty.headers.get('cache-control'), 'no-store');
  assert.ok(empty.headers.get('x-request-id'));
  assert.deepEqual(await empty.json(), { items: [], next_marker: null });
  const applicationIds = [];
  for (const app of ['calculator', 'notes']) {
    const source = form(); source.delete('app'); source.delete('target_id');
    source.set('source_name', app); source.set('environment', 'cloud'); source.set('provider', 'aws');
    const response = await fetch(`${base}/api/v1/deployments`, {
      method: 'POST', headers: { ...owner, 'Idempotency-Key': `http-app-${app}` }, body: source,
    });
    assert.equal(response.status, 202);
    const complete = await settle(async () => (await fetch(`${base}${response.headers.get('location')}`, { headers: owner })).json());
    assert.equal(complete.status, 'succeeded'); assert.equal(complete.app, app);
    applicationIds.push(complete.application_id);
  }
  const first = await (await list('?limit=1')).json();
  assert.equal(first.items.length, 1); assert.equal(first.next_marker, first.items[0].id);
  const second = await (await list(`?limit=1&marker=${first.next_marker}`)).json();
  assert.equal(second.items.length, 1); assert.equal(second.next_marker, null);
  assert.deepEqual(new Set([...first.items, ...second.items].map((row) => row.id)), new Set(applicationIds));
  assert.deepEqual(await (await list(`?marker=${second.items[0].id}`)).json(), { items: [], next_marker: null });
  const detail = await fetch(`${base}/api/v1/applications/${first.items[0].id}`, { headers: owner });
  assert.equal(detail.status, 200); assert.equal(detail.headers.get('cache-control'), 'no-store');
  assert.deepEqual(await detail.json(), first.items[0]);
  assert.equal(first.items[0].status, 'ready'); assert.equal(first.items[0].environment_target_id, 'runtime-aws');
  assert.equal(Object.hasOwn(first.items[0], 'session_id'), false);
  assert.deepEqual(await (await list('', stranger)).json(), { items: [], next_marker: null });
  for (const id of [first.items[0].id, 'app-missing', '__proto__']) {
    const response = await fetch(`${base}/api/v1/applications/${id}`, { headers: stranger });
    assert.equal(response.status, 404); assert.equal((await response.json()).error.code, 'NOT_FOUND');
  }
  assert.equal((await list(`?marker=${first.items[0].id}`, stranger)).status, 422);
  for (const query of ['?limit=0', '?limit=101', '?limit=1&limit=2', '?marker=', '?marker=missing', '?unexpected=1'])
    assert.equal((await list(query)).status, 422);
  assert.equal((await fetch(`${base}/api/v1/applications/${first.items[0].id}?limit=1`, { headers: owner })).status, 422);
  for (const path of ['', `/${first.items[0].id}`]) for (const method of ['POST', 'PUT', 'DELETE', 'HEAD', 'OPTIONS']) {
    const response = await fetch(`${base}/api/v1/applications${path}`, { method, headers: owner });
    assert.equal(response.status, 405); assert.equal(response.headers.get('allow'), 'GET');
  }
  assert.equal(f.registrations.length, 2); assert.equal(f.submissions.length, 2); assert.equal(f.deliveries.length, 2);
});

async function interruptedApplication(t, { http = false } = {}) {
  const f = await applicationFixture(t);
  const owner = f.product.dashboard.session().id;
  const deliver = f.adapter.deployPublished;
  f.adapter.deployPublished = async (...args) => {
    await deliver(...args);
    if (http) {
      const progress = { cd: deployed.cd, public_http: { state: 'unverified', url: null, verified_at: null } };
      await args[1].onProgress(progress);
      return { ...progress, error: { code: 'HTTP_UNVERIFIED', outcome_unknown: true } };
    }
    throw new EnvironmentError('APPLICATION_ROUTE_RECONCILE_REQUIRED', 502, true);
  };
  const created = await f.product.createDeployment(applicationSource('calculator'), 'resume-source', undefined, owner);
  const original = await settle(() => f.product.getDeployment(created.id, owner));
  assert.equal(original.status, 'unknown'); assert.equal(original.stage, http ? 'http' : 'cd');
  return { ...f, owner, created, original, deliver };
}

test('explicit CD resume survives restart, preserves the deployment and publishes no new CI or registration', async (t) => {
  const f = await interruptedApplication(t, { http: true });
  const source = await readFile(join(f.directory, `${f.created.id}.source.json`));
  await f.product.close();
  const product = await createProductService(f.options);
  let finish, calls = 0;
  const waiting = new Promise((resolve) => { finish = resolve; });
  f.adapter.deployPublished = async (...args) => {
    calls++;
    const state = diskState(f.directory).operations[f.created.id];
    assert.equal(state.status, 'running'); assert.equal(state.resume_count, 1);
    assert.deepEqual(state.cd, f.original.cd, 'HTTP resume retains the last verified CD revision');
    assert.equal(args[1].deploymentId, f.created.id);
    await waiting;
    return f.deliver(...args);
  };
  try {
    const responses = await Promise.allSettled([
      product.resumeDeployment(f.created.id, f.owner), product.resumeDeployment(f.created.id, f.owner),
    ]);
    assert.equal(responses.filter(({ status }) => status === 'fulfilled').length, 1, responses.map(r => r.reason?.code).join(', '));
    assert.equal(responses.find(({ status }) => status === 'rejected').reason.code, 'DEPLOYMENT_NOT_RESUMABLE');
    await settle(() => product.getDeployment(f.created.id, f.owner), () => calls === 1);
    const running = await product.getDeployment(f.created.id, f.owner);
    assert.equal(running.status, 'running'); assert.equal(running.stage, 'http');
    finish();
    const result = await settle(() => product.getDeployment(f.created.id, f.owner));
    assert.equal(result.status, 'succeeded'); assert.equal(result.id, f.created.id);
    assert.equal(result.ci.run_id, f.original.ci.run_id); assert.equal(result.source_commit, f.original.source_commit);
    assert.equal(result.resume_count, 1); assert.ok(Date.parse(result.resumed_at));
    assert.equal(f.registrations.length, 1); assert.equal(f.submissions.length, 1); assert.equal(f.deliveries.length, 2);
    assert.deepEqual(await readFile(join(f.directory, `${f.created.id}.source.json`)), source);
    await assert.rejects(product.resumeDeployment(f.created.id, f.owner), { code: 'DEPLOYMENT_NOT_RESUMABLE' });
  } finally { finish(); await product.close(); }
});

test('published CD failure can resume after cluster permissions are repaired without another CI run', async (t) => {
  const f = await applicationFixture(t);
  const owner = f.product.dashboard.session().id;
  const deliver = f.adapter.deployPublished;
  f.adapter.deployPublished = async (...args) => {
    await deliver(...args);
    return { cd: { state: 'failed', deployed: false, revision: 'd'.repeat(40),
      evidence: { sync: 'Unknown', health: 'Healthy' } },
    public_http: { state: 'not_run', url: null, verified_at: null } };
  };
  const accepted = await f.product.createDeployment(applicationSource('calculator'), 'permission-repair', undefined, owner);
  const failed = await settle(() => f.product.getDeployment(accepted.id, owner));
  assert.equal(failed.status, 'failed'); assert.equal(failed.stage, 'cd');
  assert.equal(failed.ci.state, 'published'); assert.equal(failed.url, null);
  await assert.rejects(f.product.resumeDeployment(accepted.id, f.product.dashboard.session().id), { status: 404 });
  f.adapter.deployPublished = deliver;
  await f.product.resumeDeployment(accepted.id, owner);
  const done = await settle(() => f.product.getDeployment(accepted.id, owner));
  assert.equal(done.status, 'succeeded'); assert.equal(done.id, accepted.id);
  assert.equal(done.ci.run_id, failed.ci.run_id); assert.deepEqual(done.ci.images, failed.ci.images);
  assert.equal(done.resume_count, 1);
  assert.equal(f.registrations.length, 1); assert.equal(f.submissions.length, 1); assert.equal(f.deliveries.length, 2);
});

test('published app resume is not blocked by an unrelated app awaiting registration reconciliation', async (t) => {
  const f = await interruptedApplication(t);
  await f.product.close();
  const store = await createProductStore(f.directory);
  await store.transaction(state => {
    state.operations.other = { id: 'other', kind: 'deployments', app: 'other-app',
      application_id: 'app-other', environment_target_id: 'runtime-aws', status: 'unknown', stage: 'registration' };
  });
  await store.close();
  f.adapter.deployPublished = f.deliver;
  const product = await createProductService(f.options);
  try {
    await product.resumeDeployment(f.created.id, f.owner);
    const done = await settle(() => product.getDeployment(f.created.id, f.owner));
    assert.equal(done.status, 'succeeded'); assert.equal(done.ci.run_id, f.original.ci.run_id);
    assert.equal(f.submissions.length, 1); assert.equal(f.registrations.length, 1);
    assert.equal(diskState(f.directory).operations.other.status, 'unknown');
  } finally { await product.close(); }
});

test('resume checks exact session ownership before any CI or CD observation', async (t) => {
  const f = await interruptedApplication(t);
  f.service.status = async () => assert.fail('Unauthorized resume must not read CI');
  for (const session of [null, '', f.product.dashboard.session().id]) {
    await assert.rejects(f.product.resumeDeployment(f.created.id, session), { status: 404, code: 'NOT_FOUND' });
  }
  assert.equal(f.registrations.length, 1); assert.equal(f.submissions.length, 1); assert.equal(f.deliveries.length, 1);
});

test('resume refuses changed publication identity before overwriting original CI evidence or calling CD', async (t) => {
  const changes = {
    artifact: (value) => ({ ...value, artifact_id: value.artifact_id + 1 }),
    attempt: (value) => ({ ...value, producer_attempt: value.producer_attempt + 1 }),
    image: (value) => ({ ...value, images: { web: `ghcr.io/example/apps@sha256:${'d'.repeat(64)}` } }),
    source: (value) => ({ ...value, source_commit: 'f'.repeat(40) }),
    app: (value) => ({ ...value, app: 'other-app' }),
    target: (value) => ({ ...value, target_id: 'other-target' }),
    run: (value) => ({ ...value, run_id: '99999' }),
    unpublished: () => null,
  };
  for (const [name, change] of Object.entries(changes)) await t.test(name, async (t) => {
    const f = await interruptedApplication(t);
    const runId = f.original.ci.run_id;
    f.runs.set(runId, change(f.runs.get(runId)));
    if (name === 'unpublished') f.service.status = async () => ({ state: 'running' });
    await f.product.resumeDeployment(f.created.id, f.owner);
    const result = await settle(() => f.product.getDeployment(f.created.id, f.owner));
    assert.equal(result.status, ['source', 'app', 'target', 'run'].includes(name) ? 'blocked' : 'unknown'); assert.equal(result.error.outcome_unknown, true);
    assert.deepEqual(result.ci, f.original.ci);
    assert.equal(f.submissions.length, 1); assert.equal(f.registrations.length, 1); assert.equal(f.deliveries.length, 1);
  });
});

test('resume honors native unknown results without clearing journals or automatically retrying', async (t) => {
  const f = await interruptedApplication(t);
  await f.product.resumeDeployment(f.created.id, f.owner);
  const result = await settle(() => f.product.getDeployment(f.created.id, f.owner));
  assert.equal(result.status, 'unknown'); assert.equal(result.error.code, 'APPLICATION_ROUTE_RECONCILE_REQUIRED');
  assert.equal(f.deliveries.length, 2); assert.equal(f.submissions.length, 1); assert.equal(f.registrations.length, 1);
  await pause(30); assert.equal(f.deliveries.length, 2);
});

test('resume cannot downgrade an earlier unknown when a later native preflight returns blocked', async (t) => {
  const f = await interruptedApplication(t);
  f.adapter.deployPublished = async (...args) => {
    await f.deliver(...args);
    return { cd: { state: 'blocked', revision: null, deployed: false }, public_http: { state: 'not_run', url: null, verified_at: null },
      error: { code: 'CD_PREPARATION_FAILED', outcome_unknown: false } };
  };
  await f.product.resumeDeployment(f.created.id, f.owner);
  const result = await settle(() => f.product.getDeployment(f.created.id, f.owner));
  assert.equal(result.status, 'unknown'); assert.equal(result.error.outcome_unknown, true);
  assert.equal(result.error.code, 'CD_PREPARATION_FAILED'); assert.equal(result.cd.state, 'blocked');
  assert.equal(f.deliveries.length, 2); assert.equal(f.submissions.length, 1); assert.equal(f.registrations.length, 1);
});

test('resume rejects altered registration or run bindings, lifecycle actions and another active operation', async (t) => {
  const changes = {
    application_session: (state, record, other) => { state.applications[record.application_id].session_id = other; },
    environment: (_state, record) => { record.environment_target_id = 'runtime-gcp'; },
    target: (_state, record) => { record.target_id = 'different-target'; },
    binding: (state, record) => { state.bindings[record.ci.run_id].source_commit = 'e'.repeat(40); },
    registration: (state, record) => { state.applications[record.application_id].status = 'unknown'; },
    deletion: (state, record) => { state.applications[record.application_id].deletion_requested = true; },
    lifecycle: (state, record) => { state.applications[record.application_id].lifecycle_operation_id = 'pending'; },
    ci_stage: (_state, record) => { record.stage = 'ci'; },
    unpublished: (_state, record) => { record.ci.state = 'running'; },
    cancelled: (_state, record) => { record.status = 'cancelled'; },
    busy: (state) => { state.operations.other = { id: 'other', kind: 'deployments', status: 'unknown' }; },
    same_application_busy: (state, record) => { state.operations.other = { id: 'other', kind: 'deployments',
      application_id: record.application_id, app: record.app, status: 'running', stage: 'cd' }; },
  };
  for (const [name, change] of Object.entries(changes)) await t.test(name, async (t) => {
    const f = await interruptedApplication(t);
    await f.product.close();
    const store = await createProductStore(f.directory);
    const other = store.dashboard.session().id;
    await store.transaction((state) => change(state, state.operations[f.created.id], other));
    await store.close();
    const product = await createProductService(f.options);
    f.service.status = async () => assert.fail('Invalid resume must not read CI');
    try {
      await assert.rejects(product.resumeDeployment(f.created.id, f.owner), (error) => {
        if (name === 'busy') {
          assert.equal(error.retryable, false);
          assert.deepEqual(error.admission, { scope: 'workspace', accepted: false, reason: 'reconciliation_required' });
        }
        return [404, 409].includes(error.status);
      });
      assert.equal(f.submissions.length, 1); assert.equal(f.registrations.length, 1); assert.equal(f.deliveries.length, 1);
    } finally { await product.close(); }
  });
});

test('HTTP deployment actions accepts exact resume input and returns the same resource with session isolation', async (t) => {
  const f = await applicationFixture(t);
  const { base } = await httpFixture(t, { product: f.product });
  const response = await fetch(`${base}/api/v1/sessions`, { method: 'POST' });
  const cookie = response.headers.get('set-cookie').split(';')[0];
  const source = new FormData();
  source.set('environment', 'cloud'); source.set('provider', 'aws'); source.set('source_type', 'folder'); source.set('source_name', 'calculator');
  source.append('files', new Blob(['hello']), 'app.js'); source.set('paths', '["app.js"]');
  const deliver = f.adapter.deployPublished;
  f.adapter.deployPublished = async (...args) => { await deliver(...args); throw new EnvironmentError('APPLICATION_ROUTE_RECONCILE_REQUIRED', 502, true); };
  const created = await fetch(`${base}/api/v1/deployments`, { method: 'POST', headers: { cookie, 'Idempotency-Key': 'resume-http' }, body: source });
  assert.equal(created.status, 202);
  const id = (await created.json()).resource_id;
  const original = await settle(async () => (await fetch(`${base}/api/v1/deployments/${id}`, { headers: { cookie } })).json());
  const history = await (await fetch(`${base}/api/v1/deployments`, { headers: { cookie } })).json();
  assert.equal(history.items[0].application_id, original.application_id);
  assert.equal(history.items[0].environment_target_id, original.environment_target_id);
  assert.notEqual(original.environment_target_id, original.target_id);
  source.set('source_name', 'calculator');
  for (const sameOwner of [true, false]) {
    const blocked = await fetch(`${base}/api/v1/deployments`, { method: 'POST',
      headers: { ...(sameOwner ? { cookie } : {}), 'Idempotency-Key': 'blocked-new-app' }, body: source });
    assert.equal(blocked.status, 409); assert.equal(blocked.headers.get('retry-after'), null);
    const { error } = await blocked.json();
    assert.equal(error.code, sameOwner ? 'APPLICATION_RECONCILE_REQUIRED' : 'APPLICATION_OWNERSHIP_CONFLICT');
    assert.equal(error.outcome_unknown, false);
    assert.doesNotMatch(JSON.stringify(error), new RegExp(`${id}|session_id`));
  }
  assert.equal(f.registrations.length, 1); assert.equal(f.submissions.length, 1);
  const url = `${base}/api/v1/deployments/${id}/actions`;
  const headers = { cookie, 'content-type': 'application/json' };
  for (const method of ['GET', 'PUT', 'DELETE']) {
    const res = await fetch(url, { method, headers });
    assert.equal(res.status, 405); assert.equal(res.headers.get('allow'), 'POST');
  }
  for (const body of [{}, { action: 'retry' }, { action: 'resume', app: 'foreign' }, null, []]) {
    assert.equal((await fetch(url, { method: 'POST', headers, body: JSON.stringify(body) })).status, 422);
  }
  assert.equal((await fetch(url, { method: 'POST', headers, body: '{"action":"resume","action":"resume"}' })).status, 422);
  assert.equal((await fetch(`${url}?target_id=foreign`, { method: 'POST', headers, body: '{"action":"resume"}' })).status, 422);
  assert.equal((await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{"action":"resume"}' })).status, 404);
  assert.equal(f.deliveries.length, 1);
  f.adapter.deployPublished = deliver;
  const resumed = await fetch(url, { method: 'POST', headers, body: '{"action":"resume"}' });
  assert.equal(resumed.status, 202); assert.equal(resumed.headers.get('location'), `/api/v1/deployments/${id}`);
  assert.equal(resumed.headers.get('retry-after'), '2'); assert.equal(resumed.headers.get('cache-control'), 'no-store');
  const accepted = await resumed.json();
  assert.deepEqual(accepted, { resource_id: id, action: 'resume', status: 'accepted', request_id: resumed.headers.get('x-request-id') });
  const result = await settle(async () => (await fetch(`${base}/api/v1/deployments/${id}`, { headers: { cookie } })).json());
  assert.equal(result.status, 'succeeded'); assert.equal(result.resume_count, 1);
  assert.equal(f.registrations.length, 1); assert.equal(f.submissions.length, 1); assert.equal(f.deliveries.length, 2);
});

test('known AWS route preflight leaves registration unstarted and only a new explicit upload may retry it', async (t) => {
  const f = await applicationFixture(t), owner = f.product.dashboard.session().id;
  const register = f.adapter.register;
  let calls = 0;
  f.adapter.register = async (application) => {
    calls++;
    if (calls === 1) throw new EnvironmentError('APPLICATION_AWS_ROUTE_PREFLIGHT_FAILED', 409, false);
    return register(application);
  };
  const first = await f.product.createDeployment(applicationSource('calculator'), 'preflight-failure', undefined, owner);
  const blocked = await settle(() => f.product.getDeployment(first.id, owner));
  assert.equal(blocked.status, 'blocked'); assert.equal(blocked.stage, 'registration');
  assert.equal(blocked.error.code, 'APPLICATION_AWS_ROUTE_PREFLIGHT_FAILED'); assert.equal(blocked.error.outcome_unknown, false);
  assert.equal(f.product.getApplication(first.application_id, owner).status, 'queued');
  assert.equal(calls, 1); assert.equal(f.submissions.length, 0); assert.equal(f.deliveries.length, 0);
  assert.equal((await f.product.createDeployment(applicationSource('calculator'), 'preflight-failure', undefined, owner)).id, first.id);
  assert.equal(calls, 1); assert.equal(f.submissions.length, 0);
  const second = await f.product.createDeployment(applicationSource('calculator'), 'corrected-preflight', undefined, owner);
  assert.notEqual(second.id, first.id); assert.equal(second.application_id, first.application_id);
  const completed = await settle(() => f.product.getDeployment(second.id, owner));
  assert.equal(completed.status, 'succeeded'); assert.equal(f.product.getApplication(first.application_id, owner).status, 'ready');
  assert.equal(calls, 2); assert.equal(f.submissions.length, 1); assert.equal(f.deliveries.length, 1);
  assert.equal((await f.product.getDeployment(first.id, owner)).status, 'blocked', 'the original failed operation remains evidence');
});

test('other blocked or uncertain registration failures never gain the preflight retry exception', async (t) => {
  for (const [code, unknown, status] of [
    ['APPLICATION_AWS_ROUTE_PREFLIGHT_FAILED', true, 'unknown'],
    ['APPLICATION_NAMESPACE_CONFLICT', false, 'blocked'],
  ]) await t.test(`${code}:${status}`, async (t) => {
    const f = await applicationFixture(t), owner = f.product.dashboard.session().id;
    let calls = 0;
    f.adapter.register = async () => { calls++; throw new EnvironmentError(code, 409, unknown); };
    const first = await f.product.createDeployment(applicationSource('calculator'), 'registration-failure', undefined, owner);
    assert.equal((await settle(() => f.product.getDeployment(first.id, owner))).status, status);
    assert.equal(f.product.getApplication(first.application_id, owner).status, status);
    await assert.rejects(f.product.createDeployment(applicationSource('calculator'), 'retry', undefined, owner),
      { code: 'APPLICATION_RECONCILE_REQUIRED' });
    assert.equal(calls, 1); assert.equal(f.submissions.length, 0); assert.equal(f.deliveries.length, 0);
  });
});

test('application submission preserves safe phase diagnostics and only blocks admission for uncertain dispatch', async (t) => {
  const logs = [];
  t.mock.method(console, 'error', (line) => logs.push(JSON.parse(line)));
  for (const phase of ['source_tree', 'ci_dispatch']) {
    const f = await applicationFixture(t), owner = f.product.dashboard.session().id;
    let calls = 0;
    f.service.deploy = async () => { calls++; throw new SubmissionError(phase, Object.assign(new Error('private upstream secret'), { upstreamStatus: 504 })); };
    const request = applicationSource('Memos');
    const first = await f.product.createDeployment(request, 'submission-diagnostic', null, owner);
    const result = await settle(() => f.product.getDeployment(first.id, owner));
    const unknown = phase === 'ci_dispatch';
    assert.equal(result.status, unknown ? 'unknown' : 'failed');
    assert.equal(result.error.code, unknown ? 'CI_DISPATCH_UNCONFIRMED' : 'SOURCE_REGISTRATION_FAILED');
    assert.equal(result.error.phase, phase);
    assert.equal(result.error.upstream_status, 504);
    assert.equal(result.error.outcome_unknown, unknown);
    assert.match(result.error.message, /GitHub HTTP 504/);
    assert.ok(!JSON.stringify(result).includes('private upstream secret'));
    assert.ok(logs.some((row) => row.operation_id === first.id && row.request_id === result.error.request_id && row.phase === phase));
    assert.equal((await f.product.createDeployment(request, 'submission-diagnostic', null, owner)).id, first.id);
    assert.equal(calls, 1);
    const next = await f.product.createDeployment(applicationSource('Other'), 'another-request', null, owner);
    if (unknown) {
      assert.equal(next.status, 'queued');
      assert.equal(next.queue.started_at, undefined);
      await assert.rejects(f.product.createDeployment(request, 'same-app-again', null, owner), { code: 'APPLICATION_RECONCILE_REQUIRED' });
    } else assert.equal((await settle(() => f.product.getDeployment(next.id, owner))).status, 'failed');
  }
});

test('restart reattaches interrupted CI without resubmitting and delivers the same app once', async (t) => {
  const f = await applicationFixture(t), owner = f.product.dashboard.session().id;
  const published = f.service.status;
  f.service.status = async () => ({ state: 'queued' });
  const accepted = await f.product.createDeployment(applicationSource('restart-app'), 'restart-request', undefined, owner);
  await settle(() => f.product.getDeployment(accepted.id, owner), (r) => r.stage === 'ci' && r.ci?.run_id);
  await f.product.close();
  // CI finishes outside the API process while the API is down.
  f.service.status = published;
  const restarted = await createProductService(f.options);
  try {
    const result = await settle(() => restarted.getDeployment(accepted.id, owner), (r) => r.status === 'succeeded');
    assert.equal(result.error, null);
    assert.equal(result.ci.run_id, '1001');
    assert.equal(result.application_id, accepted.application_id);
    assert.equal((await restarted.createDeployment(applicationSource('restart-app'), 'restart-request', undefined, owner)).id, accepted.id);
    assert.equal(f.submissions.length, 1);
    assert.equal(f.registrations.length, 1);
    assert.equal(f.deliveries.length, 1);
    assert.equal(f.deliveries[0].args.deploymentId, accepted.id);
  } finally { await restarted.close(); }
  const again = await createProductService(f.options);
  try { await pause(30); assert.equal(f.deliveries.length, 1); } finally { await again.close(); }
});

test('restart never replays uncertain CD, unbound CI, changed ownership or deleted requests', async (t) => {
  const cases = {
    cd_started: (s, r) => { r.stage = 'cd'; r.cd.state = 'running'; },
    missing_binding: (s, r) => { delete s.bindings[r.ci.run_id]; },
    changed_source: (s, r) => { s.bindings[r.ci.run_id].source_commit = 'f'.repeat(40); },
    changed_owner: (s, r) => { s.applications[r.application_id].session_id = null; },
    deletion: (s, r) => { r.deletion_requested = true; },
    newer_request: (s, r) => { s.operations.newer = { ...r, id: 'newer', status: 'failed', created_at: '2099-01-01T00:00:00Z' }; },
  };
  for (const [name, mutate] of Object.entries(cases)) await t.test(name, async (t) => {
    const f = await applicationFixture(t), owner = f.product.dashboard.session().id;
    const published = f.service.status;
    f.service.status = async () => ({ state: 'queued' });
    const accepted = await f.product.createDeployment(applicationSource('restart-app'), name, undefined, owner);
    await settle(() => f.product.getDeployment(accepted.id, owner), (r) => r.stage === 'ci' && r.ci?.run_id);
    await f.product.close();
    const store = await createProductStore(f.directory);
    await store.transaction((s) => mutate(s, s.operations[accepted.id]));
    await store.close();
    let reads = 0;
    f.service.status = async (...args) => { reads++; return published(...args); };
    const restarted = await createProductService(f.options);
    try {
      await pause(40);
      assert.equal((await restarted.getDeployment(accepted.id, owner)).status, 'unknown');
      assert.equal(reads, 0); assert.equal(f.submissions.length, 1); assert.equal(f.deliveries.length, 0);
    } finally { await restarted.close(); }
  });
});

test('restart reobserves published customer CD through the read-only adapter, without applying again', async t => {
  const f = await applicationFixture(t), owner = f.product.dashboard.session().id;
  let applies = 0, observations = 0;
  f.adapter.deployPublished = async () => { applies++; throw new EnvironmentError('CD_RECONCILE_REQUIRED', 502, true); };
  const accepted = await f.product.createDeployment(applicationSource('recover-cd'), 'read-cd', undefined, owner);
  const interrupted = await settle(() => f.product.getDeployment(accepted.id, owner));
  assert.equal(interrupted.status, 'unknown'); assert.equal(interrupted.stage, 'cd');
  await f.product.close();
  f.adapter.observePublished = async (application, args) => {
    observations++; assert.equal(application.id, accepted.application_id); assert.equal(args.deploymentId, accepted.id);
    assert.equal(args.publication.source_commit, interrupted.source_commit); return deployed;
  };
  const restarted = await createProductService(f.options);
  try {
    const done = await settle(() => restarted.getDeployment(accepted.id, owner), row => row.status === 'succeeded');
    assert.ok(done.cd.observation.last_success_at); assert.equal(done.cd.observation.error, null);
    assert.equal(applies, 1); assert.equal(observations, 1); assert.equal(f.submissions.length, 1);
  } finally { await restarted.close(); }
});

test('platform restart resumes an unapplied published image without another upload or CI dispatch', async t => {
  for (const status of ['unknown', 'blocked']) await t.test(status, async t => {
    const f = await applicationFixture(t), owner = f.product.dashboard.session().id;
    const deliver = f.adapter.deployPublished;
    f.adapter.deployPublished = async () => { throw new EnvironmentError('INTERRUPTED', 502, true); };
    const accepted = await f.product.createDeployment(applicationSource('restart-cd'), 'restart-cd', undefined, owner);
    const first = await settle(() => f.product.getDeployment(accepted.id, owner));
    assert.equal(first.error.code, 'INTERRUPTED');
    await f.product.close();
    const store = await createProductStore(f.directory);
    await store.transaction(s => { s.operations[accepted.id].status = status; });
    await store.close();
    f.adapter.observePublished = async () => ({ cd: { state: 'blocked', deployed: false, revision: null },
      public_http: { state: 'not_run' }, error: { code: 'DEPLOYMENT_NOT_FOUND' } });
    f.adapter.deployPublished = deliver;
    const product = await createProductService(f.options);
    try {
      const done = await settle(() => product.getDeployment(accepted.id, owner), row => row.status === 'succeeded');
      assert.equal(done.id, first.id); assert.equal(done.ci.run_id, first.ci.run_id);
      assert.deepEqual(done.ci.images, first.ci.images);
      assert.equal(done.resume_count, 1); assert.equal(done.error, null);
      assert.equal(done.url, 'https://restart-cd.example.test/');
      assert.equal(f.submissions.length, 1); assert.equal(f.registrations.length, 1); assert.equal(f.deliveries.length, 1);
    } finally { await product.close(); }
  });
});

test('missing CD observation preserves the route failure and the owner can resume its published image', async t => {
  const f = await applicationFixture(t), owner = f.product.dashboard.session().id;
  const deliver = f.adapter.deployPublished;
  let applies = 0;
  f.adapter.deployPublished = async () => { applies++; throw new EnvironmentError('GCP_ROUTE_APPLY_TIMEOUT', 502, true); };
  const accepted = await f.product.createDeployment(applicationSource('gcp-recover'), 'gcp-recover', undefined, owner);
  const first = await settle(() => f.product.getDeployment(accepted.id, owner));
  assert.equal(first.error.code, 'GCP_ROUTE_APPLY_TIMEOUT');
  await f.product.close();
  f.adapter.observePublished = async () => ({ cd: { state: 'blocked', deployed: false, revision: null },
    public_http: { state: 'not_run', url: null, verified_at: null }, error: { code: 'DEPLOYMENT_NOT_FOUND' } });
  const product = await createProductService(f.options);
  try {
    const blocked = await settle(() => product.getDeployment(accepted.id, owner), row => row.status === 'blocked');
    assert.equal(blocked.error.code, 'GCP_ROUTE_APPLY_TIMEOUT');
    assert.equal(blocked.error.request_id, first.error.request_id);
    assert.match(blocked.error.message, /GCP 로드밸런서/);
    assert.equal(blocked.cd.observation.error.code, 'DEPLOYMENT_NOT_FOUND');
    assert.equal(applies, 1);
    f.adapter.deployPublished = deliver;
    await product.resumeDeployment(accepted.id, owner);
    const done = await settle(() => product.getDeployment(accepted.id, owner), row => row.status === 'succeeded');
    assert.equal(done.ci.run_id, first.ci.run_id);
    assert.deepEqual(done.ci.images, first.ci.images);
    assert.equal(f.submissions.length, 1); assert.equal(f.registrations.length, 1);
  } finally { await product.close(); }
});

for (const limit of [3, 16]) test(`${limit === 3 ? 'default' : 'explicit'} ${limit}-slot queue retains admission between polls and starts waiting work FIFO`, async (t) => {
  const submitted = new Map(), completed = new Set();
  let sourceWriters = 0, peakSourceWriters = 0, cdWriters = 0, peakCdWriters = 0;
  const f = await fixture(t, { ...(limit === 3 ? {} : { maxConcurrentDeployments: limit }), service: {
    deploy: async ({ app, target_id }) => {
      peakSourceWriters = Math.max(peakSourceWriters, ++sourceWriters);
      await pause(2);
      const run_id = String(1000 + submitted.size), source_commit = Number(run_id).toString(16).padStart(40, '0');
      submitted.set(run_id, { app, target_id, run_id, source_commit });
      sourceWriters--;
      return submitted.get(run_id);
    },
    status: async (id) => {
      const row = submitted.get(String(id));
      return { ...row, state: completed.has(row.app) ? 'published' : 'running',
        ...(completed.has(row.app) ? { publication: { ...row, artifact_id: Number(id) + 100, producer_attempt: 1 } } : {}) };
    },
  }, deployPublished: async ({ app, publication: result }) => {
    assert.equal(result.app, app);
    peakCdWriters = Math.max(peakCdWriters, ++cdWriters);
    await pause(4); cdWriters--;
    return deployed;
  } });
  const rows = await Promise.all(Array.from({ length: limit + 2 }, (_, i) =>
    f.product.createDeployment({ ...input, app: `parallel-${i}` }, `parallel-${i}`)));
  await settle(() => f.product.getDeployment(rows[limit - 1].id), row => Boolean(row.ci.run_id));
  await pause(60); // Several polling rounds must not manufacture more admission slots.
  assert.equal(submitted.size, limit);
  assert.equal((await f.product.getDeployment(rows[limit].id)).status, 'queued');
  assert.equal((await f.product.getDeployment(rows[limit + 1].id)).queue.started_at, undefined);
  const same = await f.product.createDeployment({ ...input, app: 'parallel-0' }, 'same-app-later');
  completed.add('parallel-1');
  await settle(() => f.product.getDeployment(rows[limit].id), row => Boolean(row.ci.run_id));
  assert.equal(submitted.size, limit + 1);
  assert.equal((await f.product.getDeployment(same.id)).status, 'queued');
  for (let i = 0; i < limit + 2; i++) completed.add(`parallel-${i}`);
  await Promise.all([...rows, same].map(row => settle(() => f.product.getDeployment(row.id))));
  assert.equal(submitted.size, limit + 3);
  assert.equal(peakSourceWriters, 1);
  assert.equal(peakCdWriters, 1);
  assert.equal(new Set((await Promise.all([...rows, same].map(row => f.product.getDeployment(row.id)))).map(row => row.ci.run_id)).size, limit + 3);
});

test('concurrency configuration is bounded and rejects invalid limits before opening the store', async (t) => {
  for (const maxConcurrentDeployments of [0, -1, 65, 1.5, NaN, '16']) {
    await assert.rejects(fixture(t, { maxConcurrentDeployments }), { code: 'INVALID_INPUT' });
  }
});

test('operator resumes only the exact published operation without new CI', async t => {
  const f = await interruptedApplication(t);
  const identity = { operation_id: f.created.id, source_commit: f.original.source_commit, run_id: f.original.ci.run_id };
  await assert.rejects(f.product.resumePublishedOperation({ ...identity, source_commit: 'f'.repeat(40) }), { code: 'RESUME_BINDING_MISMATCH' });
  await assert.rejects(f.product.resumePublishedOperation({ ...identity, run_id: '999999' }), { code: 'RESUME_BINDING_MISMATCH' });
  f.adapter.deployPublished = f.deliver;
  await f.product.resumePublishedOperation(identity);
  const result = await settle(() => f.product.getDeployment(f.created.id, f.owner));
  assert.equal(result.status, 'succeeded');
  assert.equal(f.submissions.length, 1);
  assert.equal(diskState(f.directory).operations[f.created.id].resume_initiated_by, 'operator');
});
