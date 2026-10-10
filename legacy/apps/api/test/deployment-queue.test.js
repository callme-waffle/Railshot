import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as pause } from 'node:timers/promises';
import { createProductService } from '../src/product.js';
import { EnvironmentError } from '../src/environments.js';

const file = (content) => ({ path: 'app.js', content: Buffer.from(content) });
const source = (app, provider = 'aws') => ({ source_type: 'folder', source_name: app,
  files: [file(`source for ${app}`)], deployment_selection: { environment: 'cloud', provider } });
const successful = (args) => ({ cd: { state: 'deployed', deployed: true, revision: args.sourceCommit },
  public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: `https://${args.app}.example.test/` } });

async function until(read, predicate = (record) => !['queued', 'running'].includes(record.status)) {
  const deadline = performance.now() + 10000;
  let value;
  do {
    value = await read();
    if (predicate(value)) return value;
    await pause(5);
  } while (performance.now() < deadline);
  assert.fail(`Queue did not settle: ${JSON.stringify(value)}`);
}

async function fixture(t, { unknownGraceMs = 40, environmentAdapter, maxConcurrentDeployments } = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-queue-'));
  const submissions = [], deliveries = [], publications = new Map(), releases = [];
  const service = { targetId: 'runtime-aws', targetIds: ['runtime-aws', 'runtime-gcp'],
    allowTarget(id) { if (!this.targetIds.includes(id)) this.targetIds.push(id); },
    async deploy(input) {
      submissions.push(input);
      const runId = String(1000 + submissions.length), sha = String(submissions.length % 10).repeat(40);
      publications.set(runId, { run_id: runId, app: input.app, target_id: input.target_id, source_commit: sha,
        artifact_id: 9000 + submissions.length, producer_attempt: 1,
        images: { web: `ghcr.io/example/apps@sha256:${'c'.repeat(64)}` } });
      return { run_id: runId, source_commit: sha };
    },
    async status(id) { return { state: 'published', publication: publications.get(id) }; },
    async sourceFiles(publication) { return submissions[Number(publication.run_id) - 1001].files; },
  };
  const adapter = { targets: { 'runtime-aws': { provider: 'aws' }, 'runtime-gcp': { provider: 'gcp' } },
    describe(environment, app) { return { id: `${environment}-${app}`, app, target_id: `${environment}-${app}`,
      environment_target_id: environment, provider: environment === 'runtime-aws' ? 'aws' : 'gcp' }; },
    async register() { return { status: 'ready' }; },
    async deployPublished(_application, args) { deliveries.push(args); return successful(args); },
  };
  const options = { directory, service, applicationAdapter: adapter, environmentAdapter,
    target: { id: 'runtime-aws', provider: 'aws' }, providerTargets: { gcp: 'runtime-gcp' },
    pollInterval: 5, unknownGraceMs, maxConcurrentDeployments, observeMetrics: async () => ({ metrics: {} }) };
  const f = { directory, service, adapter, options, submissions, deliveries,
    gate() { let release; const wait = new Promise((resolve) => { release = resolve; }); releases.push(release); return { wait, release }; } };
  f.product = await createProductService(options);
  f.owner = f.product.dashboard.session().id;
  f.read = (id) => f.product.getDeployment(id, f.owner);
  f.create = (app, key = app) => f.product.createDeployment(source(app), key, undefined, f.owner);
  t.after(async () => {
    for (const release of releases) release();
    await f.product.close();
    await rm(directory, { recursive: true, force: true });
  });
  return f;
}

test('one-slot configuration: concurrent different apps persist FIFO while identical keys produce only one dispatch', async (t) => {
  const f = await fixture(t, { maxConcurrentDeployments: 1 }), hold = f.gate();
  const deliver = f.adapter.deployPublished;
  let inFlight = 0, maximum = 0;
  f.adapter.deployPublished = async (...args) => {
    maximum = Math.max(maximum, ++inFlight);
    try { if (args[1].app === 'alpha') await hold.wait; return await deliver(...args); }
    finally { inFlight--; }
  };
  const [alpha, duplicate, beta, gamma] = await Promise.all([
    f.create('alpha', 'same-key'), f.create('alpha', 'same-key'), f.create('beta'), f.create('gamma'),
  ]);
  assert.equal(alpha.id, duplicate.id);
  assert.deepEqual([alpha, beta, gamma].map((row) => row.queue.sequence), [1, 2, 3]);
  await until(() => f.read(alpha.id), (row) => row.stage === 'cd');
  assert.equal((await f.read(beta.id)).status, 'queued');
  assert.equal((await f.read(gamma.id)).status, 'queued');
  assert.deepEqual(f.submissions.map((row) => row.app), ['alpha']);
  await assert.rejects(f.product.createBuild({ app: 'legacy', target_id: 'runtime-aws', source_type: 'folder', files: [file('legacy')] }), { code: 'EXECUTOR_BUSY' });
  hold.release();
  for (const row of [alpha, beta, gamma]) assert.equal((await until(() => f.read(row.id))).status, 'succeeded');
  assert.equal(maximum, 1);
  assert.deepEqual(f.submissions.map((row) => row.app), ['alpha', 'beta', 'gamma']);
  assert.deepEqual(f.deliveries.map((row) => row.app), ['alpha', 'beta', 'gamma']);
  assert.equal((await f.create('alpha', 'same-key')).id, alpha.id);
  assert.equal(f.submissions.length, 3);
});

test('unavailable CI observation yields the writer to another app and later finishes the original run', async t => {
  const f = await fixture(t), status = f.options.service.status;
  let unavailable = true;
  f.options.service.status = async (...args) => {
    if (String(args[0]) === '1001' && unavailable) throw new TypeError('temporary GitHub read failure');
    return status(...args);
  };
  const alpha = await f.create('alpha');
  await until(() => f.read(alpha.id), row => row.ci.observation?.error);
  const beta = await f.create('beta');
  assert.equal((await until(() => f.read(beta.id))).status, 'succeeded');
  const waiting = await f.read(alpha.id);
  assert.equal(waiting.status, 'running'); assert.equal(waiting.ci.run_id, '1001');
  unavailable = false;
  assert.equal((await until(() => f.read(alpha.id))).status, 'succeeded');
  assert.deepEqual(f.submissions.map(row => row.app), ['alpha', 'beta']);
  assert.equal(f.deliveries.length, 2);
});

test('expired published delivery frees the global slot but preserves the affected environment fence', async (t) => {
  const grace = 100, f = await fixture(t, { unknownGraceMs: grace, maxConcurrentDeployments: 1 });
  const deliver = f.adapter.deployPublished;
  f.adapter.deployPublished = async (...args) => args[1].app === 'alpha' && args[0].environment_target_id === 'runtime-aws'
    ? { cd: { state: 'unknown', deployed: false }, public_http: { state: 'not_run' }, error: { outcome_unknown: true } }
    : deliver(...args);
  const alpha = await f.create('alpha');
  const uncertain = await until(() => f.read(alpha.id));
  assert.equal(uncertain.status, 'unknown');
  const beta = await f.create('beta');
  await assert.rejects(
    f.product.createDeployment(source('alpha', 'aws'), 'new-aws', undefined, f.owner),
    { code: 'APPLICATION_RECONCILE_REQUIRED' });
  const separate = await f.product.createDeployment(source('alpha', 'gcp'), 'new-gcp', undefined, f.owner);
  assert.notEqual(separate.application_id, alpha.application_id);
  assert.equal((await until(() => f.read(separate.id))).status, 'succeeded');
  assert.equal((await until(() => f.read(beta.id))).status, 'succeeded');
  const released = await f.read(alpha.id);
  assert.equal(released.status, 'unknown'); assert.equal(released.error.outcome_unknown, true);
  assert.equal(released.queue.release_reason, 'unknown_timeout');
  assert.ok(Date.parse(released.queue.released_at) - Date.parse(released.unknown_since) >= grace);
  assert.equal(released.ci.run_id, uncertain.ci.run_id);
  assert.equal((await f.create('alpha')).id, alpha.id);
  await assert.rejects(f.create('alpha', 'retry-after-expiry'), { code: 'APPLICATION_RECONCILE_REQUIRED' });
  f.adapter.planLifecycle = async () => assert.fail('uncertain app must not reach native lifecycle planning');
  f.adapter.verifyLifecyclePlan = async () => {};
  f.adapter.applyLifecycle = async () => assert.fail('uncertain app must not reach native lifecycle execution');
  for (const action of ['stop', 'delete']) await assert.rejects(
    f.product.createApplicationPlan(alpha.application_id, { action }, f.owner), { code: 'APPLICATION_RECONCILE_REQUIRED' });
  assert.deepEqual(f.submissions.map((row) => row.app), ['alpha', 'beta', 'alpha']);
});

test('restart retains waiting source snapshots, never redispatches the started job, and starts each waiting job once', async (t) => {
  const f = await fixture(t, { maxConcurrentDeployments: 1 }), hold = f.gate();
  const deliver = f.adapter.deployPublished;
  f.adapter.deployPublished = async (...args) => {
    if (args[1].app === 'alpha') {
      await hold.wait;
      throw new EnvironmentError('INTERRUPTED_CD', 502, true);
    }
    return deliver(...args);
  };
  const alpha = await f.create('alpha');
  await until(() => f.read(alpha.id), (row) => row.stage === 'cd');
  const request = { source_type: 'github', source_name: 'beta', repository_url: 'https://github.com/example/beta',
    deployment_selection: { environment: 'cloud', provider: 'aws' } };
  const expected = Buffer.from([0, 10, 65, 255]);
  let downloads = 0;
  const beta = await f.product.createDeployment(request, 'pinned-beta', async () => {
    downloads++;
    return { files: [{ path: 'assets/binary.dat', content: Buffer.from(expected) }],
      source: { type: 'github', repository: request.repository_url, sha: 'd'.repeat(40) } };
  }, f.owner);
  const gamma = await f.create('gamma');
  assert.equal((await f.read(beta.id)).queue.started_at, undefined);
  const closing = f.product.close();
  hold.release();
  await closing;
  f.product = await createProductService(f.options);
  assert.equal((await f.read(alpha.id)).status, 'unknown');
  assert.equal((await until(() => f.read(beta.id))).status, 'succeeded');
  assert.equal((await until(() => f.read(gamma.id))).status, 'succeeded');
  assert.equal((await f.product.createDeployment(request, 'pinned-beta', () => assert.fail('must not fetch new repository HEAD'), f.owner)).id, beta.id);
  assert.equal(downloads, 1);
  assert.deepEqual(f.submissions.map((row) => row.app), ['alpha', 'beta', 'gamma']);
  assert.deepEqual(f.submissions[1].files, [{ path: 'assets/binary.dat', content: expected }]);
  assert.equal(f.submissions[1].source.sha, 'd'.repeat(40));
});

test('an unknown environment with a live worker cannot expire its slot or run queued work', async (t) => {
  let started, release;
  const entered = new Promise((resolve) => { started = resolve; });
  const wait = new Promise((resolve) => { release = resolve; });
  const environmentAdapter = {
    plan: async (_, { id }) => ({ public: { id }, private: { profile: { target: { target_id: 'runtime-aws' } } } }),
    verifyPlan: async () => {},
    execute: async (_plan, { onProgress }) => {
      await onProgress({ status: 'unknown', error: { code: 'OBSERVATION_PENDING', outcome_unknown: true } });
      started(); await wait;
      return { status: 'unknown', error: { code: 'OBSERVATION_PENDING', outcome_unknown: true } };
    },
  };
  const grace = 25, f = await fixture(t, { environmentAdapter, unknownGraceMs: grace });
  const plan = await f.product.createPlan({}, f.owner);
  const operation = await f.product.createEnvironment({ plan_id: plan.id }, 'environment', f.owner);
  await entered;
  try {
    const beta = await f.product.createDeployment(source('beta', 'gcp'), 'beta', undefined, f.owner);
    await pause(grace * 3);
    const current = f.product.getEnvironment(operation.id, f.owner);
    assert.equal(current.status, 'unknown'); assert.equal(current.queue?.released_at, undefined);
    assert.equal((await f.read(beta.id)).status, 'queued'); assert.equal(f.submissions.length, 0);
    release();
    assert.equal((await until(() => f.read(beta.id))).status, 'succeeded');
    assert.equal(f.product.getEnvironment(operation.id, f.owner).queue.release_reason, 'unknown_timeout');
    await assert.rejects(f.create('gamma'), { code: 'APPLICATION_RECONCILE_REQUIRED' });
    assert.deepEqual(f.submissions.map((row) => row.app), ['beta']);
  } finally { release(); }
});

test('a queued environment plan is revalidated before any provisioning or CI dispatch', async (t) => {
  let stale = false, checks = 0, executions = 0;
  const environmentAdapter = {
    plan: async (_, { id }) => ({ public: { id, name: 'planned', executable: true },
      private: { profile: { target: { target_id: 'runtime-aws' }, deployment: {} } } }),
    verifyPlan: async () => { checks++; if (stale) throw new EnvironmentError('PLAN_EXPIRED', 409, false); },
    execute: async () => { executions++; assert.fail('stale plan must not provision'); },
    deployPublished: async () => assert.fail('stale plan must not deploy'),
  };
  const f = await fixture(t, { environmentAdapter }), hold = f.gate();
  const deliver = f.adapter.deployPublished;
  f.adapter.deployPublished = async (...args) => { if (args[1].app === 'alpha') await hold.wait; return deliver(...args); };
  const plan = await f.product.createPlan({}, f.owner);
  const alpha = await f.create('alpha');
  await until(() => f.read(alpha.id), (row) => row.stage === 'cd');
  const planned = await f.product.createDeployment({ app: 'planned', target_id: 'runtime-aws', plan_id: plan.id,
    source_type: 'folder', files: [file('planned source')] }, 'planned', undefined, f.owner);
  assert.equal(planned.status, 'queued'); assert.equal(checks, 1);
  stale = true; hold.release();
  const result = await until(() => f.read(planned.id));
  assert.equal(result.status, 'blocked'); assert.equal(result.error.code, 'PLAN_EXPIRED');
  assert.equal(result.error.outcome_unknown, false); assert.equal(checks, 2); assert.equal(executions, 0);
  assert.deepEqual(f.submissions.map((row) => row.app), ['alpha']);
});

test('queued updates recheck the deployed base so a later preview cannot overwrite its successor', async (t) => {
  const f = await fixture(t), base = await f.create('alpha');
  const initial = await until(() => f.read(base.id));
  const first = await f.product.createUpdate(initial.application_id, { source_type: 'folder', files: [file('version two')] }, 'update-two', undefined, f.owner);
  const second = await f.product.createUpdate(initial.application_id, { source_type: 'folder', files: [file('version three')] }, 'update-three', undefined, f.owner);
  const hold = f.gate(), deliver = f.adapter.deployPublished;
  f.adapter.deployPublished = async (...args) => { if (args[1].app === 'beta') await hold.wait; return deliver(...args); };
  const beta = await f.create('beta');
  await until(() => f.read(beta.id), (row) => row.stage === 'cd');
  const acceptedFirst = await f.product.startUpdate(first.id, {}, f.owner);
  const acceptedSecond = await f.product.startUpdate(second.id, {}, f.owner);
  assert.equal(acceptedFirst.status, 'queued'); assert.equal(acceptedSecond.status, 'queued');
  assert.equal((await f.product.startUpdate(first.id, {}, f.owner)).queue.sequence, acceptedFirst.queue.sequence);
  hold.release();
  assert.equal((await until(() => f.read(first.id))).status, 'succeeded');
  const refused = await until(() => f.read(second.id));
  assert.equal(refused.status, 'blocked'); assert.equal(refused.error.code, 'UPDATE_BASE_CHANGED');
  assert.equal(refused.error.outcome_unknown, false);
  assert.deepEqual(f.submissions.map((row) => [row.app, row.files[0].content.toString()]),
    [['alpha', 'source for alpha'], ['beta', 'source for beta'], ['alpha', 'version two']]);
});

test('a released unknown app does not prevent confirmed deletion of another app with an active deployment', async (t) => {
  const f = await fixture(t, { maxConcurrentDeployments: 1 }), hold = f.gate();
  const calls = { pendingPlan: 0, cancel: 0, apply: 0 };
  f.adapter.deployPublished = async (_application, args) => {
    if (args.app === 'alpha') return { cd: { state: 'unknown', deployed: false }, public_http: { state: 'not_run' }, error: { outcome_unknown: true } };
    await hold.wait; return successful(args);
  };
  const nativePlan = (application, { id, action }) => ({ public: { id, application_id: application.id, action,
    plan_hash: 'e'.repeat(64), resources: [{ kind: 'Deployment', name: application.app }], retained: [],
    expires_at: new Date(Date.now() + 60000).toISOString() }, private: {} });
  f.adapter.planPendingDeletion = async (application, { id, deploymentId }) => {
    calls.pendingPlan++;
    const plan = nativePlan(application, { id, action: 'delete' });
    plan.private = { deferred: true, deployment_id: deploymentId };
    return plan;
  };
  f.adapter.planLifecycle = async (application, args) => nativePlan(application, args);
  f.adapter.verifyLifecyclePlan = async () => {};
  f.adapter.applyLifecycle = async (application, plan) => {
    calls.apply++;
    return { application_id: application.id, action: plan.public.action, status: 'succeeded', steps: [], residuals: [] };
  };
  f.service.cancel = async (runId, binding) => {
    calls.cancel++; assert.equal(binding.app, 'beta'); assert.equal(runId, '1002');
    return { status: 'completed' };
  };
  const alpha = await f.create('alpha');
  await until(() => f.read(alpha.id), (row) => row.status === 'unknown' && row.queue?.released_at);
  const beta = await f.create('beta');
  await until(() => f.read(beta.id), (row) => row.stage === 'cd');
  const plan = await f.product.createApplicationPlan(beta.application_id, { action: 'delete' }, f.owner);
  assert.equal(calls.pendingPlan, 1);
  const deletion = await f.product.createApplicationOperation(beta.application_id, { action: 'delete', plan_id: plan.id,
    plan_hash: plan.plan_hash, confirmation: 'beta', delete_data: true }, 'delete-beta', f.owner);
  hold.release();
  const result = await until(() => f.product.getOperation(deletion.id, f.owner));
  assert.equal(result.status, 'succeeded'); assert.equal(calls.cancel, 1); assert.equal(calls.apply, 1);
  assert.equal((await f.read(beta.id)).stage, 'cancelled');
  assert.equal(f.product.getApplication(beta.application_id, f.owner).status, 'deleted');
  assert.equal((await f.read(alpha.id)).status, 'unknown');
});

test('restart completes the original GCP deployment only when native registration never started', async t => {
  for (const started of [false, true]) await t.test(`native request exists: ${started}`, async t => {
    const f = await fixture(t);
    let attempts = 0;
    f.adapter.register = async () => { attempts++; throw new Error('Worker stopped before acquiring writer'); };
    const first = await f.product.createDeployment(source('memos', 'gcp'), 'original-upload', undefined, f.owner);
    const stopped = await until(() => f.read(first.id));
    assert.equal(stopped.status, 'unknown');
    assert.equal(stopped.stage, 'registration');
    assert.equal(f.submissions.length, 0);
    await f.product.close();
    f.adapter.registrationStarted = async application => {
      assert.equal(application.environment_target_id, 'runtime-gcp');
      assert.equal(application.app, 'memos');
      return started;
    };
    f.adapter.register = async () => { attempts++; return { status: 'ready' }; };
    f.product = await createProductService(f.options);
    if (started) {
      const blocked = await f.read(first.id);
      assert.equal(blocked.status, 'unknown');
      assert.equal(attempts, 1); assert.equal(f.submissions.length, 0);
    } else {
      const completed = await until(() => f.read(first.id));
      assert.equal(completed.status, 'succeeded');
      assert.equal(completed.id, first.id); assert.equal(completed.application_id, first.application_id);
      assert.equal(attempts, 2); assert.equal(f.submissions.length, 1); assert.equal(f.deliveries.length, 1);
      assert.equal(f.submissions[0].files[0].content.toString(), 'source for memos');
    }
  });
});

test('operator source replay is idempotent, preserves ownership and requires a known unsent request or successful cross-provider source', async t => {
  const { SubmissionError } = await import('../src/github.js');
  const f = await fixture(t), deploy = f.service.deploy;
  f.service.deploy = async () => { throw new SubmissionError('source_ref', { upstreamStatus: 422 }); };
  const original = await f.create('terminal');
  await until(() => f.read(original.id));
  f.service.deploy = deploy;
  await assert.rejects(f.product.replaySubmittedSource(original.id, 'runtime-aws', { 'app.js': 'changed' }), { code: 'INVALID_INPUT' });
  const packaging = { Dockerfile: 'FROM python:3.13-slim' };
  const replay = await f.product.replaySubmittedSource(original.id, 'runtime-aws', packaging);
  assert.notEqual(replay.id, original.id);
  assert.equal((await f.product.replaySubmittedSource(original.id, 'runtime-aws', packaging)).id, replay.id);
  assert.equal((await until(() => f.read(replay.id))).status, 'succeeded');
  assert.equal((await f.read(original.id)).status, 'failed');
  await assert.rejects(f.product.getDeployment(replay.id, 'another-session'), { code: 'NOT_FOUND' });
  const gcp = await f.product.replaySubmittedSource(replay.id, 'runtime-gcp');
  assert.equal((await until(() => f.read(gcp.id))).status, 'succeeded');
  assert.deepEqual(f.submissions[0].files, f.submissions[1].files);
  assert.equal(f.submissions[0].files.find(file => file.path === 'app.js').content.toString(), 'source for terminal');
  await assert.rejects(f.product.replaySubmittedSource(replay.id, 'runtime-aws'), { code: 'SOURCE_REPLAY_REJECTED' });
  await assert.rejects(f.product.replaySubmittedSource(original.id, 'runtime-gcp'), { code: 'SOURCE_REPLAY_REJECTED' });
});
