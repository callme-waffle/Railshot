import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { setTimeout as pause } from 'node:timers/promises';
import { createProductService } from '../src/product.js';

const file = (path, content) => ({ path, content: Buffer.from(content) });
const upload = (files) => ({ source_type: 'folder', files });
const original = [file('app.js', 'original'), file('remove.txt', 'old')];
async function settled(product, id, owner) {
  for (let count = 0; count < 1000; count++) {
    const value = await product.getDeployment(id, owner);
    if (!['queued', 'running'].includes(value.status)) return value;
    await pause(5);
  }
  assert.fail('deployment did not settle');
}
async function fixture(t, { legacy = false } = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-updates-'));
  const submissions = [], publications = new Map();
  let registrations = 0, sourceReads = 0;
  const service = { targetId: 'runtime-aws', targetIds: [], allowTarget() {},
    async deploy(input) {
      submissions.push(input);
      if (service.failDispatch) throw new Error('private uncertainty');
      const run = String(100 + submissions.length), sha = String(submissions.length % 10).repeat(40);
      publications.set(run, { run_id: run, app: input.app, tenant: 'demo', target_id: input.target_id,
        source_commit: sha, artifact_id: 900 + submissions.length, producer_attempt: 1 });
      return { run_id: run, source_commit: sha };
    },
    async status(id) {
      return service.failCi ? { state: 'failed' } : { state: 'published', publication: publications.get(id) };
    },
    async sourceFiles(publication) {
      sourceReads++;
      if (service.sourceError) throw service.sourceError;
      return service.deployedSource || submissions[Number(publication.run_id) - 101].files;
    } };
  const adapter = { targets: { 'runtime-aws': { provider: 'aws', automaticDelivery: true } },
    describe(environment, app) { return { id: `app-${app}`, app, target_id: `app-${app}`, environment_target_id: environment, provider: 'aws' }; },
    async register() { registrations++; return { status: 'ready' }; },
    async deployPublished(_application, args) {
      if (adapter.uncertainCd) return { cd: { state: 'unknown', deployed: false }, public_http: { state: 'not_run' }, error: { outcome_unknown: true } };
      return { cd: { state: 'deployed', deployed: true, revision: args.sourceCommit },
        public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://demo.example.test' } };
    } };
  const options = { directory, service, applicationAdapter: adapter, target: { id: 'runtime-aws', provider: 'aws' }, pollInterval: 1,
    observeMetrics: async () => ({ metrics: {} }) };
  const f = { options, service, adapter, submissions, directory, registrations: () => registrations, sourceReads: () => sourceReads };
  f.product = await createProductService(options);
  t.after(async () => { await f.product.close(); await rm(directory, { recursive: true, force: true }); });
  f.owner = legacy ? null : f.product.dashboard.session().id; f.other = f.product.dashboard.session().id;
  const accepted = await f.product.createDeployment({ ...upload(original), source_name: 'demo-app', deployment_selection: { environment: 'cloud', provider: 'aws' } }, 'initial', undefined, f.owner);
  f.base = await settled(f.product, accepted.id, f.owner); f.app = f.base.application_id;
  assert.equal(f.base.status, 'succeeded');
  return f;
}

test('same-name resolution uses the exact environment and owner without dispatching or exposing another session', async (t) => {
  const f = await fixture(t);
  const selection = { environment: 'cloud', provider: 'aws', app: 'demo-app' };
  const resolved = f.product.resolveApplication(selection, f.owner);
  assert.equal(resolved.application.id, f.app);
  assert.equal(resolved.application.current_deployment.id, f.base.id);
  assert.equal(resolved.environment_target_id, 'runtime-aws');
  assert.ok(!('session_id' in resolved.application));
  for (const owner of [f.other, null]) assert.throws(() => f.product.resolveApplication(selection, owner), { code: 'APPLICATION_OWNERSHIP_CONFLICT' });
  assert.equal(f.product.resolveApplication({ ...selection, app: 'another-app' }, f.owner).application, null);
  assert.throws(() => f.product.resolveApplication({ ...selection, provider: 'gcp' }, f.owner), { code: 'CAPABILITY_UNAVAILABLE' });
  assert.throws(() => f.product.resolveApplication({ ...selection, environment: 'onprem' }, f.owner), { status: 422 });
  assert.throws(() => f.product.resolveApplication({ ...selection, app: '../invalid' }, f.owner), { status: 422 });
  assert.equal(f.submissions.length, 1); assert.equal(f.sourceReads(), 0);
});

test('preview uses verified deployed source, persists exact GitHub snapshot, and starts once after restart', async (t) => {
  const f = await fixture(t);
  f.service.deployedSource = [file('app.js', 'agent-fixed'), file('remove.txt', 'old'), file('agent-test.js', 'assert app')];
  const next = [file('app.js', 'agent-fixed'), file('added.txt', 'new')];
  let loads = 0;
  const input = { source_type: 'github', repository_url: 'https://github.com/example/demo' };
  const loader = async () => { loads++; return { files: next, source: { type: 'github', repository: input.repository_url, sha: 'c'.repeat(40) } }; };
  const preview = await f.product.createUpdate(f.app, input, 'preview', loader, f.owner);
  assert.equal(preview.status, 'preview'); assert.equal(preview.stage, 'review');
  assert.equal(preview.baseline_kind, 'deployed'); assert.equal(preview.base_deployment_id, f.base.id);
  assert.deepEqual(preview.changes, { added: ['added.txt'], modified: [], deleted: ['agent-test.js', 'remove.txt'], unchanged: 1 });
  assert.equal(preview.source_origin.sha, 'c'.repeat(40)); assert.equal(f.submissions.length, 1);
  assert.equal((await f.product.createUpdate(f.app, input, 'preview', () => assert.fail('no replay download'), f.owner)).id, preview.id);
  await assert.rejects(f.product.createUpdate(f.app, upload(next), 'preview', undefined, f.owner), { code: 'IDEMPOTENCY_CONFLICT' });
  await f.product.close(); f.product = await createProductService(f.options);
  assert.equal((await f.product.getDeployment(preview.id, f.owner)).status, 'preview');
  const [one, two] = await Promise.all([f.product.startUpdate(preview.id, {}, f.owner), f.product.startUpdate(preview.id, {}, f.owner)]);
  assert.equal(one.id, two.id);
  assert.equal((await settled(f.product, preview.id, f.owner)).status, 'succeeded');
  assert.equal(loads, 1); assert.equal(f.submissions.length, 2); assert.equal(f.registrations(), 1);
  assert.deepEqual(f.submissions[1].files, next);
  assert.equal((await f.product.startUpdate(preview.id, { rebuild: true }, f.owner)).id, preview.id);
  assert.equal(f.submissions.length, 2);
  const app = f.product.getApplication(f.app, f.owner);
  assert.equal(app.current_deployment.id, preview.id); assert.equal(app.latest_deployment.id, preview.id);
  assert.equal(app.current_deployment_state, 'verified'); assert.ok(!('publication' in app.current_deployment));
});

test('unchanged preview skips dispatch; explicit rebuild uses a new preview and ready registration', async (t) => {
  const f = await fixture(t);
  const first = await f.product.createUpdate(f.app, upload(original), 'same', undefined, f.owner);
  assert.equal(first.no_changes, true);
  assert.equal((await f.product.startUpdate(first.id, { rebuild: false }, f.owner)).status, 'unchanged');
  assert.equal(f.submissions.length, 1);
  const rebuild = await f.product.createUpdate(f.app, upload(original), 'rebuild', undefined, f.owner);
  await f.product.startUpdate(rebuild.id, { rebuild: true }, f.owner);
  assert.equal((await settled(f.product, rebuild.id, f.owner)).status, 'succeeded');
  assert.equal(f.submissions.length, 2); assert.equal(f.registrations(), 1);
});

test('stale baseline and expired previews never dispatch', async (t) => {
  const f = await fixture(t);
  const first = await f.product.createUpdate(f.app, upload([file('app.js', 'v2')]), 'one', undefined, f.owner);
  const stale = await f.product.createUpdate(f.app, upload([file('app.js', 'v3')]), 'two', undefined, f.owner);
  await f.product.startUpdate(first.id, {}, f.owner); await settled(f.product, first.id, f.owner);
  await assert.rejects(f.product.startUpdate(stale.id, {}, f.owner), { code: 'UPDATE_BASE_CHANGED' });
  const expired = await f.product.createUpdate(f.app, upload(original), 'expired', undefined, f.owner);
  const now = Date.now;
  try { Date.now = () => Date.parse(expired.expires_at) + 1; await assert.rejects(f.product.startUpdate(expired.id, {}, f.owner), { code: 'UPDATE_EXPIRED' }); }
  finally { Date.now = now; }
  assert.equal(f.submissions.length, 2);
});

test('only explicit legacy absence falls back; failed source verification cannot create deletion preview', async (t) => {
  const f = await fixture(t);
  f.service.sourceError = Object.assign(new Error('legacy'), { code: 'SOURCE_NOT_AVAILABLE' });
  const legacy = await f.product.createUpdate(f.app, upload(original), 'legacy', undefined, f.owner);
  assert.equal(legacy.baseline_kind, 'submitted'); assert.equal(legacy.no_changes, false); assert.equal(legacy.source_comparison_only, true);
  await assert.rejects(f.product.sourceFiles(f.base.id, 'deployed', f.owner), { code: 'SOURCE_NOT_AVAILABLE' });
  f.service.sourceError = Object.assign(new Error('transport or invalid artifact'), { code: 'SOURCE_VERIFICATION_FAILED' });
  await assert.rejects(f.product.createUpdate(f.app, upload([file('app.js', 'partial')]), 'bad-source', undefined, f.owner), { code: 'SOURCE_VERIFICATION_FAILED' });
  f.service.sourceError = null; f.service.deployedSource = {};
  await assert.rejects(f.product.createUpdate(f.app, upload(original), 'malformed', undefined, f.owner), { code: 'SOURCE_INVALID' });
  assert.equal(f.product.list('deployments', f.owner, { limit: 100, marker: null }).total, 2);
  assert.equal(f.submissions.length, 1);
});

test('ownership and fixed application identity are checked before source reads or mutation', async (t) => {
  const f = await fixture(t), before = f.sourceReads();
  await assert.rejects(f.product.createUpdate(f.app, upload(original), 'foreign', undefined, f.other), { status: 404 });
  for (const key of ['app', 'target_id', 'environment_target_id', 'deployment_selection', 'plan_id'])
    await assert.rejects(f.product.createUpdate(f.app, { ...upload(original), [key]: 'other' }, key, undefined, f.owner), { status: 422 });
  assert.equal(f.sourceReads(), before);
  const preview = await f.product.createUpdate(f.app, upload([file('app.js', 'v2')]), 'own', undefined, f.owner);
  await assert.rejects(f.product.startUpdate(preview.id, {}, f.other), { status: 404 });
  await assert.rejects(f.product.sourceFiles(preview.id, 'submitted', f.other), { status: 404 });
  assert.deepEqual(await f.product.sourceFiles(preview.id, 'submitted', f.owner), [file('app.js', 'v2')]);
  assert.deepEqual(await f.product.sourceFiles(f.base.id, 'deployed', f.owner), original);
  assert.equal(f.submissions.length, 1);
});

test('maintenance reads never authorize updates to a browser-owned application or preview', async (t) => {
  const f = await fixture(t), before = f.sourceReads();
  assert.equal(f.product.getApplication(f.app).id, f.app);
  await assert.rejects(f.product.createUpdate(f.app, upload(original), 'maintenance', undefined), { status: 404 });
  assert.equal(f.sourceReads(), before);
  assert.equal(f.product.list('deployments', f.owner, { limit: 100, marker: null }).total, 1);
  const preview = await f.product.createUpdate(f.app, upload([file('app.js', 'v2')]), 'own-preview', undefined, f.owner);
  await assert.rejects(f.product.startUpdate(preview.id), { status: 404 });
  assert.equal((await f.product.getDeployment(preview.id, f.owner)).status, 'preview');
  assert.equal(f.submissions.length, 1);
  await f.product.startUpdate(preview.id, {}, f.owner);
  assert.equal((await settled(f.product, preview.id, f.owner)).status, 'succeeded');
  await assert.rejects(f.product.startUpdate(preview.id), { status: 404 });
  assert.equal(f.product.getApplication(f.app, f.owner).current_deployment.id, preview.id);
  assert.equal(f.submissions.length, 2);
});

test('legacy null-owned applications retain same-owner update and version tracking', async (t) => {
  const f = await fixture(t, { legacy: true });
  const preview = await f.product.createUpdate(f.app, upload([file('app.js', 'v2')]), 'legacy-preview', undefined);
  await f.product.startUpdate(preview.id);
  assert.equal((await settled(f.product, preview.id)).status, 'succeeded');
  const application = f.product.getApplication(f.app);
  assert.equal(application.current_deployment.id, preview.id);
  assert.equal(application.latest_deployment.id, preview.id);
  assert.equal(application.current_deployment_state, 'verified');
});

test('failed latest deployment preserves last verified success; unknown dispatch blocks another start without replay', async (t) => {
  const f = await fixture(t);
  f.service.failCi = true;
  const failed = await f.product.createUpdate(f.app, upload([file('app.js', 'bad')]), 'fail-ci', undefined, f.owner);
  await f.product.startUpdate(failed.id, {}, f.owner); await settled(f.product, failed.id, f.owner);
  let app = f.product.getApplication(f.app, f.owner);
  assert.equal(app.current_deployment.id, f.base.id); assert.equal(app.latest_deployment.status, 'failed'); assert.equal(app.current_deployment_state, 'verified');
  f.service.failCi = false;
  const uncertain = await f.product.createUpdate(f.app, upload([file('app.js', 'uncertain')]), 'unknown', undefined, f.owner);
  const waiting = await f.product.createUpdate(f.app, upload(original), 'waiting', undefined, f.owner);
  f.service.failDispatch = true;
  await f.product.startUpdate(uncertain.id, {}, f.owner); await settled(f.product, uncertain.id, f.owner);
  await f.product.close(); f.product = await createProductService(f.options);
  assert.equal((await f.product.startUpdate(uncertain.id, {}, f.owner)).status, 'unknown');
  await assert.rejects(f.product.startUpdate(waiting.id, {}, f.owner), (error) => {
    assert.equal(error.code, 'APPLICATION_RECONCILE_REQUIRED'); assert.equal(error.retryable, false);
    assert.doesNotMatch(error.message, /demo-app|마지막 갱신/); return true;
  });
  const queued = await f.product.createDeployment({ ...upload(original), source_name: 'different-app',
    deployment_selection: { environment: 'cloud', provider: 'aws' } }, 'other-owner', undefined, f.other);
  assert.equal(queued.status, 'queued');
  assert.equal(queued.queue.started_at, undefined);
  assert.doesNotMatch(JSON.stringify(queued), new RegExp(uncertain.id));
  assert.equal(f.submissions.length, 3);
});

test('uncertain CD makes the previous success explicitly unverified and prevents further updates', async (t) => {
  const f = await fixture(t);
  const preview = await f.product.createUpdate(f.app, upload([file('app.js', 'v2')]), 'cd', undefined, f.owner);
  f.adapter.uncertainCd = true;
  await f.product.startUpdate(preview.id, {}, f.owner); await settled(f.product, preview.id, f.owner);
  const app = f.product.getApplication(f.app, f.owner);
  assert.equal(app.current_deployment.id, f.base.id); assert.equal(app.current_deployment_state, 'unverified');
  await assert.rejects(f.product.createUpdate(f.app, upload(original), 'blocked', undefined, f.owner), { code: 'APPLICATION_RECONCILE_REQUIRED' });
});

test('tampered persisted preview is refused before dispatch', async (t) => {
  const f = await fixture(t);
  const preview = await f.product.createUpdate(f.app, upload([file('app.js', 'v2')]), 'tamper', undefined, f.owner);
  await writeFile(join(f.directory, `${preview.id}.source.json`), JSON.stringify([{ path: 'app.js', content: Buffer.from('changed after review').toString('base64') }]));
  await assert.rejects(f.product.startUpdate(preview.id, {}, f.owner), { code: 'SOURCE_NOT_AVAILABLE' });
  assert.equal(f.submissions.length, 1);
});

test('a submitted-only legacy comparison still runs CI and missing source support never claims deployed bytes', async (t) => {
  const f = await fixture(t);
  delete f.service.sourceFiles;
  const preview = await f.product.createUpdate(f.app, upload(original), 'legacy-run', undefined, f.owner);
  assert.equal(preview.baseline_kind, 'submitted'); assert.equal(preview.no_changes, false);
  await assert.rejects(f.product.sourceFiles(f.base.id, 'deployed', f.owner), { code: 'SOURCE_NOT_AVAILABLE' });
  await f.product.startUpdate(preview.id, {}, f.owner);
  assert.equal((await settled(f.product, preview.id, f.owner)).status, 'succeeded');
  assert.equal(f.submissions.length, 2);
});
