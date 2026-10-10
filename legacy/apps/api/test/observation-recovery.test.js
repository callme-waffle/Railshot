import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as pause } from 'node:timers/promises';
import { createProductService } from '../src/product.js';
import { SubmissionError, createDeploymentService } from '../src/github.js';
import { createHash } from 'node:crypto';

const files = [{ path: 'app.js', content: Buffer.from('hello') }];
const input = { app: 'demo-app', target_id: 'demo', source_type: 'folder', files };
const publication = { run_id: 123, target_id: 'demo', app: 'demo-app', source_commit: 'a'.repeat(40), artifact_id: 456, producer_attempt: 1 };
const published = { run_id: 123, state: 'published', status: 'completed', conclusion: 'success', publication };
const delivered = { cd: { state: 'deployed', deployed: true, revision: 'b'.repeat(40) }, public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://demo.example.test' } };
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
async function until(read, condition) {
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) { const row = await read(); if (condition(row)) return row; await pause(5); }
  assert.fail('Observation did not settle');
}
async function fixture(t, overrides = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-observation-'));
  let dispatches = 0, reads = 0, cdCalls = 0;
  const service = { targetId: 'demo', deploy: async () => { dispatches++; return { run_id: 123, source_commit: publication.source_commit }; },
    status: async () => { reads++; return published; }, ...overrides };
  const options = { directory, pollInterval: 5, service, deployPublished: async () => { cdCalls++; return delivered; } };
  const f = { options, directory, service, counts: () => ({ dispatches, reads, cdCalls }) };
  f.product = await createProductService(options);
  t.after(async () => { await f.product.close(); await rm(directory, { recursive: true, force: true }); });
  return f;
}

test('temporary CI read failure preserves the last observation and recovers without redispatch', async t => {
  let reads = 0, recovered = false;
  const f = await fixture(t, { status: async () => {
    if (++reads === 1) return { state: 'running', status: 'in_progress', conclusion: null };
    if (!recovered) throw new TypeError('private transport details');
    return published;
  } });
  const accepted = await f.product.createDeployment(input, 'read-recovery');
  const stale = await until(() => f.product.getDeployment(accepted.id), row => row.ci.observation?.error);
  assert.equal(stale.status, 'running'); assert.equal(stale.stage, 'ci'); assert.equal(stale.error, null);
  assert.equal(stale.ci.state, 'running'); assert.ok(stale.ci.observation.last_success_at);
  assert.equal(stale.ci.observation.error.code, 'CI_OBSERVATION_UNAVAILABLE');
  assert.ok(stale.ci.observation.next_retry_at); assert.ok(!JSON.stringify(stale).includes('private transport'));
  recovered = true;
  const done = await until(() => f.product.getDeployment(accepted.id), row => row.status === 'succeeded');
  assert.equal(done.ci.observation.error, null); assert.equal(done.ci.workflow.conclusion, 'success');
  assert.equal(f.counts().dispatches, 1); assert.equal(f.counts().cdCalls, 1);
});

test('restart during unavailable observation reattaches the saved run and completes once', async t => {
  const f = await fixture(t, { status: async () => { throw new TypeError('network unavailable'); } });
  const first = await f.product.createDeployment(input, 'restart-read');
  await until(() => f.product.getDeployment(first.id), row => row.ci.observation?.error);
  await f.product.close(); f.service.status = async () => published;
  f.product = await createProductService(f.options);
  const done = await until(() => f.product.getDeployment(first.id), row => row.status === 'succeeded');
  assert.equal(done.ci.run_id, '123'); assert.equal(f.counts().dispatches, 1); assert.equal(f.counts().cdCalls, 1);
});

test('permanent CI read rejection is actionable and never proceeds to CD', async t => {
  const f = await fixture(t, { status: async () => { throw Object.assign(new Error('private'), { retryable: false, upstreamStatus: 401 }); } });
  const accepted = await f.product.createDeployment(input, 'read-rejected');
  const result = await until(() => f.product.getDeployment(accepted.id), row => row.status === 'blocked');
  assert.equal(result.error.code, 'CI_OBSERVATION_REJECTED'); assert.equal(result.error.outcome_unknown, true);
  assert.equal(result.ci.observation.error.upstream_status, 401); assert.equal(result.ci.observation.next_retry_at, null);
  assert.equal(f.counts().cdCalls, 0);
});

test('source materialization does not hold unrelated CI progress in the persistence queue', async t => {
  const ci = deferred(), source = deferred(), entered = deferred();
  const f = await fixture(t, { status: async () => { await ci.promise; return published; } });
  const first = await f.product.createDeployment(input, 'first');
  await until(() => f.product.getDeployment(first.id), row => row.ci.run_id);
  const pending = f.product.createDeployment({ app: 'other-app', target_id: 'demo', source_type: 'github', repository_url: 'https://github.com/example/fixture' }, 'second', async () => {
    entered.resolve(); await source.promise; throw new Error('fixture stops before dispatch');
  }).catch(error => error.message);
  try {
    await entered.promise; ci.resolve();
    const done = await until(() => f.product.getDeployment(first.id), row => row.status === 'succeeded');
    assert.equal(done.ci.state, 'published'); assert.equal(f.counts().cdCalls, 1);
  } finally { ci.resolve(); source.resolve(); await pending; }
});

test('lost dispatch response is found by the durable request source, without a second dispatch', async t => {
  let sends = 0, lookups = 0, prepared;
  const f = await fixture(t, {
    deploy: async ({ operation_id, onPrepared }) => {
      sends++; prepared = { operation_id, source_commit: publication.source_commit, app: input.app, target_id: input.target_id };
      await onPrepared({ source_commit: publication.source_commit });
      throw new SubmissionError('ci_dispatch', new TypeError('lost response'));
    },
    findDeployment: async binding => { lookups++; assert.deepEqual(binding, prepared); return lookups === 1 ? null : { run_id: 123, source_commit: publication.source_commit }; },
  });
  const first = await f.product.createDeployment(input, 'lost-ack');
  const done = await until(() => f.product.getDeployment(first.id), row => row.status === 'succeeded');
  assert.ok(done.dispatch.prepared_at); assert.equal(done.ci.run_id, '123'); assert.equal(sends, 1);
  assert.equal(lookups, 2); assert.equal(f.counts().cdCalls, 1);
  assert.equal((await f.product.createDeployment(input, 'lost-ack')).id, first.id);
  assert.equal(sends, 1);
});

test('restart while locating a lost dispatch continues lookup without resubmission', async t => {
  let sends = 0;
  const f = await fixture(t, {
    deploy: async ({ onPrepared }) => { sends++; await onPrepared({ source_commit: publication.source_commit }); throw new SubmissionError('ci_dispatch', new TypeError('lost')); },
    findDeployment: async () => null,
  });
  const accepted = await f.product.createDeployment(input, 'restart-dispatch');
  await until(() => f.product.getDeployment(accepted.id), row => row.ci.observation?.error?.code === 'CI_DISPATCH_PENDING');
  await f.product.close(); f.service.findDeployment = async () => ({ run_id: 123, source_commit: publication.source_commit });
  f.product = await createProductService(f.options);
  await until(() => f.product.getDeployment(accepted.id), row => row.status === 'succeeded');
  assert.equal(sends, 1); assert.equal(f.counts().cdCalls, 1);
});

test('unchanged source receives a request commit before dispatch and lookup rejects ambiguous identities', async () => {
  const operation_id = '11111111-1111-4111-8111-111111111111', source_commit = 'b'.repeat(40);
  const blob = createHash('sha1').update(`blob ${files[0].content.length}\0`).update(files[0].content).digest('hex');
  let commit, prepared = false, dispatches = 0, mode = 'match';
  const service = createDeploymentService({ token: 'test', owner: 'org', repo: 'apps', targetId: 'demo' }, async (url, options = {}) => {
    const path = new URL(url).pathname;
    if (path.endsWith('/git/ref/heads/main')) return Response.json({ object: { sha: 'a'.repeat(40) } });
    if (path.endsWith(`/git/commits/${'a'.repeat(40)}`)) return Response.json({ tree: { sha: 'base' } });
    if (path.endsWith('/git/trees/base')) return Response.json({ tree: [{ path: 'apps', type: 'tree', sha: 'apps' }] });
    if (path.endsWith('/git/trees/apps')) return Response.json({ tree: [{ path: 'demo', type: 'tree', sha: 'tenant' }] });
    if (path.endsWith('/git/trees/tenant')) return Response.json({ tree: [{ path: 'demo-app', type: 'tree', sha: 'app' }] });
    if (path.endsWith('/git/trees/app')) return Response.json({ tree: [{ path: 'app.js', type: 'blob', mode: '100644', sha: blob }] });
    if (path.endsWith('/git/commits')) { commit = JSON.parse(options.body); assert.equal(commit.tree, 'base'); return Response.json({ sha: source_commit }); }
    if (path.endsWith('/git/refs/heads/main')) return Response.json({});
    if (path.endsWith('/dispatches')) { assert.ok(prepared); dispatches++; throw new TypeError('lost acknowledgement'); }
    if (path.endsWith(`/git/commits/${source_commit}`)) return Response.json({ message: commit.message });
    if (path.endsWith('/runs')) {
      const run = { id: 123, head_sha: source_commit, head_branch: 'main', event: 'workflow_dispatch', path: '.github/workflows/railshot-deploy.yml',
        run_attempt: mode === 'rerun' ? 2 : 1, display_title: `railshot:demo/demo-app:${mode === 'foreign' ? 'other' : 'demo'}:${source_commit}` };
      return Response.json({ total_count: mode === 'duplicate' ? 2 : 1, workflow_runs: mode === 'duplicate' ? [run, { ...run, id: 124 }] : [run] });
    }
    assert.fail(`Unexpected GitHub path ${path}`);
  });
  await assert.rejects(service.deploy({ ...input, operation_id, onPrepared: async value => { assert.equal(value.source_commit, source_commit); prepared = true; } }), { code: 'CI_DISPATCH_UNCONFIRMED' });
  await assert.rejects(service.deploy({ ...input, operation_id,
    onPrepared: async () => { throw new Error('private checkpoint failure'); } }),
  error => error.code === 'SOURCE_CHECKPOINT_FAILED' && error.phase === 'source_checkpoint'
    && !error.message.includes('GitHub') && error.outcomeUnknown === false);
  assert.equal(dispatches, 1, 'a failed local checkpoint must not dispatch CI');
  assert.match(commit.message, new RegExp(`Railshot-Request: ${operation_id}`));
  const binding = { operation_id, source_commit, app: input.app, target_id: input.target_id };
  assert.equal((await service.findDeployment(binding)).run_id, 123);
  mode = 'foreign'; assert.equal(await service.findDeployment(binding), null);
  for (mode of ['duplicate', 'rerun']) await assert.rejects(service.findDeployment(binding), { code: 'CI_DISPATCH_AMBIGUOUS' });
  assert.equal(dispatches, 1);
});

test('artifact transport failure remains retriable while explicit dispatch rejection is definite', async () => {
  const service = createDeploymentService({ token: 'test', targetId: 'demo' }, async url => {
    const path = new URL(url).pathname;
    if (path.endsWith('/runs/123')) return Response.json({ path: '.github/workflows/railshot-deploy.yml', run_attempt: 1, status: 'completed', conclusion: 'success', head_sha: publication.source_commit });
    if (path.endsWith('/jobs')) return Response.json({ total_count: 2, jobs: ['loop', 'release'].map(name => ({ name, status: 'completed', conclusion: 'success' })) });
    if (path.endsWith('/artifacts')) return Response.json({ message: 'unavailable' }, { status: 503 });
    assert.fail(path);
  });
  await assert.rejects(service.status('123'), error => error.retryable === true && error.upstreamStatus === 503);
  for (const status of [401, 403, 404, 422]) {
    const error = new SubmissionError('ci_dispatch', { upstreamStatus: status });
    assert.equal(error.code, 'CI_DISPATCH_REJECTED'); assert.equal(error.outcomeUnknown, false);
  }
});


test('GitHub rate limit backs off across runs without spending another request', async () => {
  let calls = 0;
  const service = createDeploymentService({ token: 'test', targetId: 'demo' }, async () => {
    calls++;
    return Response.json({}, { status: 429, headers: { 'retry-after': '120' } });
  });
  const started = Date.now();
  await assert.rejects(service.status('123'), error => error.retryable && error.retryAt >= started + 120_000);
  await assert.rejects(service.status('456'), error => error.upstreamStatus === 429 && error.retryable);
  assert.equal(calls, 1);
});

test('GitHub successful response near exhaustion preserves account reserve', async () => {
  let calls = 0;
  const reset = Math.floor(Date.now() / 1000) + 300;
  const service = createDeploymentService({ token: 'test', targetId: 'demo' }, async () => {
    calls++;
    return Response.json({ path: '.github/workflows/railshot-deploy.yml', run_attempt: 1 },
      { headers: { 'x-ratelimit-remaining': '100', 'x-ratelimit-reset': String(reset) } });
  });
  await assert.rejects(service.status('123'), error => error.retryAt === reset * 1000 && error.retryable);
  await assert.rejects(service.status('456'), error => error.retryAt === reset * 1000);
  assert.equal(calls, 1);
});

test('restart after a lost source acknowledgement resumes the same checkpoint and dispatches once', async t => {
  const checkpointed = deferred(), release = deferred();
  let uploads = 0, dispatches = 0;
  const f = await fixture(t, {
    deploy: async ({ onSourcePrepared, onPrepared }) => {
      uploads++;
      await onSourcePrepared({ source_commit: publication.source_commit, source_parent: 'b'.repeat(40) });
      checkpointed.resolve(); await release.promise;
      throw new SubmissionError('source_ref', new TypeError('lost acknowledgement during shutdown'));
    },
    findDeployment: async () => null,
    resumePrepared: async ({ source_commit, source_parent, onPrepared }) => {
      assert.equal(source_commit, publication.source_commit); assert.equal(source_parent, 'b'.repeat(40));
      await onPrepared({ source_commit }); dispatches++;
      return { run_id: 123, source_commit };
    },
  });
  const accepted = await f.product.createDeployment(input, 'source-shutdown');
  await checkpointed.promise;
  const closing = f.product.close(); release.resolve(); await closing;
  f.product = await createProductService(f.options);
  const done = await until(() => f.product.getDeployment(accepted.id), row => row.status === 'succeeded');
  assert.equal(done.source_commit, publication.source_commit);
  assert.equal(uploads, 1); assert.equal(dispatches, 1); assert.equal(f.counts().cdCalls, 1);
});

test('source-ref acknowledgement loss recovers a saved source instead of failing permanently', async t => {
  let uploads = 0, resumes = 0;
  const f = await fixture(t, {
    deploy: async ({ onSourcePrepared }) => {
      uploads++; await onSourcePrepared({ source_commit: publication.source_commit, source_parent: 'b'.repeat(40) });
      throw new SubmissionError('source_ref', new TypeError('private transport detail'));
    },
    findDeployment: async () => null,
    resumePrepared: async ({ source_commit, onPrepared }) => {
      resumes++; await onPrepared({ source_commit }); return { run_id: 123, source_commit };
    },
  });
  const accepted = await f.product.createDeployment(input, 'source-lost-ack');
  const done = await until(() => f.product.getDeployment(accepted.id), row => row.status === 'succeeded');
  assert.equal(uploads, 1); assert.equal(resumes, 1);
  assert.ok(!JSON.stringify(done).includes('private transport detail'));
});

function preparedService(mode, checkpoint = async () => {}) {
  const binding = { operation_id: '11111111-1111-4111-8111-111111111111', source_commit: 'a'.repeat(40), source_parent: 'b'.repeat(40), app: 'demo-app', target_id: 'demo', onPrepared: checkpoint };
  const counts = { patches: 0, dispatches: 0 };
  const service = createDeploymentService({ token: 'fixture', owner: 'org', repo: 'apps', targetId: 'demo' }, async (url, options = {}) => {
    const path = new URL(url).pathname;
    if (path.endsWith('/git/commits/' + binding.source_commit)) return Response.json({ message: `feat: add demo/demo-app\n\nRailshot-Request: ${binding.operation_id}\nRailshot-Target: demo`, parents: [{ sha: mode === 'wrong-parent' ? 'c'.repeat(40) : binding.source_parent }] });
    if (path.endsWith('/runs')) return Response.json({ total_count: mode === 'existing' ? 1 : 0, workflow_runs: mode === 'existing' ? [{ id: 123, head_sha: binding.source_commit, head_branch: 'main', event: 'workflow_dispatch', path: '.github/workflows/railshot-deploy.yml', run_attempt: 1, display_title: `railshot:demo/demo-app:demo:${binding.source_commit}` }] : [] });
    if (path.endsWith('/git/ref/heads/main')) return Response.json({ object: { sha: mode === 'pending' ? binding.source_parent : mode === 'ahead' || mode === 'conflict' ? 'c'.repeat(40) : binding.source_commit } });
    if (path.includes('/compare/')) return Response.json({ status: mode === 'ahead' ? 'ahead' : 'diverged' });
    if (path.endsWith('/git/refs/heads/main')) { counts.patches++; assert.equal(JSON.parse(options.body).force, false); return Response.json({}); }
    if (path.endsWith('/dispatches')) { counts.dispatches++; return Response.json({ workflow_run_id: 123 }); }
    assert.fail(path);
  });
  return { service, binding, counts };
}

for (const mode of ['pending', 'published', 'existing']) test(`prepared source reconciliation: ${mode}`, async () => {
  let checkpoints = 0;
  const f = preparedService(mode, async () => { checkpoints++; });
  const result = await f.service.resumePrepared(f.binding);
  assert.equal(result.run_id, 123);
  assert.equal(f.counts.patches, mode === 'pending' ? 1 : 0);
  assert.equal(f.counts.dispatches, mode === 'existing' ? 0 : 1);
  assert.equal(checkpoints, mode === 'existing' ? 0 : 1);
});
for (const mode of ['conflict', 'ahead', 'wrong-parent']) test(`prepared source refuses ${mode} without dispatch`, async () => {
  const f = preparedService(mode);
  await assert.rejects(f.service.resumePrepared(f.binding), { code: mode === 'wrong-parent' ? 'CI_BINDING_MISMATCH' : 'SOURCE_REF_CONFLICT' });
  assert.deepEqual(f.counts, { patches: 0, dispatches: 0 });
});
test('local source checkpoint failure is not labeled as a GitHub communication error', async () => {
  const f = preparedService('published', async () => { throw new Error('Submission interrupted before dispatch'); });
  await assert.rejects(f.service.resumePrepared(f.binding), error => error.code === 'SOURCE_CHECKPOINT_FAILED' && error.reason === 'local_checkpoint_failure' && !error.message.includes('GitHub 통신'));
  assert.equal(f.counts.dispatches, 0);
});

test('restart only requeues protocol-2 source work that could not have dispatched', async t => {
  const { createProductStore } = await import('../src/product-store.js');
  const directory = await mkdtemp(join(tmpdir(), 'railshot-source-checkpoint-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  let store = await createProductStore(directory);
  await store.transaction(state => {
    for (const [id, extra] of Object.entries({ fresh: {}, legacy: { dispatch: { state: 'preparing' } }, deleting: { deletion_requested: true }, requesting: { dispatch: { version: 2, state: 'requesting' }, source_commit: 'a'.repeat(40) } })) {
      state.operations[id] = { id, kind: 'deployments', status: 'running', stage: 'ci', created_at: new Date().toISOString(),
        dispatch: { version: 2, state: 'preparing' }, ci: { run_id: null },
        queue: { sequence: 1, enqueued_at: new Date().toISOString(), started_at: new Date().toISOString() }, ...extra };
    }
  });
  await store.close(); store = await createProductStore(directory);
  try {
    const rows = store.read().operations;
    assert.equal(rows.fresh.status, 'queued'); assert.equal(rows.fresh.queue.started_at, undefined);
    assert.equal(rows.legacy.status, 'failed'); assert.equal(rows.deleting.status, 'failed');
    assert.equal(rows.requesting.status, 'unknown'); assert.equal(rows.requesting.dispatch.state, 'requesting');
  } finally { await store.close(); }
});

test('graceful shutdown finishes an admitted source submission and restart observes its run without redispatch', async t => {
  const entered = deferred(), uploaded = deferred();
  let sends = 0;
  const f = await fixture(t, {
    deploy: async ({ onPrepared }) => {
      sends++; entered.resolve(); await uploaded.promise;
      await onPrepared({ source_commit: publication.source_commit });
      return { run_id: 123, source_commit: publication.source_commit };
    },
  });
  const accepted = await f.product.createDeployment(input, 'shutdown-source');
  await entered.promise;
  const closing = f.product.close();
  uploaded.resolve(); await closing;
  f.product = await createProductService(f.options);
  const done = await until(() => f.product.getDeployment(accepted.id), row => row.status === 'succeeded');
  assert.equal(done.source_commit, publication.source_commit);
  assert.equal(done.ci.run_id, '123'); assert.equal(sends, 1);
});

test('local source checkpoint errors are never reported as GitHub communication errors', () => {
  const error = new SubmissionError('source_checkpoint', new Error('private storage path'));
  assert.equal(error.code, 'SOURCE_CHECKPOINT_FAILED');
  assert.equal(error.reason, 'local_checkpoint_failure');
  assert.equal(error.outcomeUnknown, false);
  assert.equal(error.upstream_status, null);
  assert.doesNotMatch(error.message, /GitHub|private/);
});

test('lost acknowledgement during recovered dispatch only looks up the run afterwards', async t => {
  let resumes = 0, lookups = 0;
  const f = await fixture(t, {
    deploy: async ({ onSourcePrepared }) => {
      await onSourcePrepared({ source_commit: publication.source_commit, source_parent: 'b'.repeat(40) });
      throw new SubmissionError('source_ref', new TypeError('lost'));
    },
    resumePrepared: async ({ source_commit, onPrepared }) => {
      resumes++; await onPrepared({ source_commit });
      throw new SubmissionError('ci_dispatch', new TypeError('lost dispatch response'));
    },
    findDeployment: async () => { lookups++; return { run_id: 123, source_commit: publication.source_commit }; },
  });
  const accepted = await f.product.createDeployment(input, 'recovered-dispatch-ack');
  await until(() => f.product.getDeployment(accepted.id), row => row.status === 'succeeded');
  assert.equal(resumes, 1); assert.equal(lookups, 1); assert.equal(f.counts().cdCalls, 1);
});
