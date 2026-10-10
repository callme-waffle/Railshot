import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import yazl from 'yazl';
import { readDiagnosticArchive, readDiagnosticCase, sha256, verifyDiagnosticSource } from '../src/diagnostics.js';
import { classificationInput, createJevClassifier, validateClassification } from '../src/classifier.js';
import { createDeploymentService } from '../src/github.js';
import { appendEvent, ingestCiEvents, publicTelemetry } from '../src/telemetry.js';

const root = fileURLToPath(new URL('../../..', import.meta.url));
function fixture() {
  const dir = mkdtempSync(join(tmpdir(), 'railshot-diagnostic-'));
  try {
    execFileSync(process.env.PYTHON || 'python3', ['-c', `
import json,os,sys,subprocess,time
from pathlib import Path
sys.path.insert(0,sys.argv[1]+'/ci/scripts')
from diagnostics import Diagnostics
from gate.bundle import source_digest
from observability import OperationError
p=Path(sys.argv[2]);w=p/'ws';r=p/'run';w.mkdir();r.mkdir()
(w/'app.ts').write_text('const x: number = "no";\\n')
os.environ.update(SOURCE_COMMIT='a'*40,GITHUB_SHA='a'*40,GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='1',APP='demo-app',TENANT='demo',TARGET_ID='demo')
d=Diagnostics(w,r,'native123','native123:0',['L0','L1','L2','L3'],'packaging',source_digest(w));d.capture();d.layer='L2'
d.process(['docker','buildx','build'],subprocess.CompletedProcess([],1,'','error TS2322 app.ts:1:3'),time.monotonic())
d.finish(dict(failure=dict(layer='L2',**{'class':'F2'},fingerprint='v2:L2:F2:abc',excerpt='error TS2322 app.ts:1:3'),layers=[dict(layer='L0',outcome='PASS'),dict(layer='L1',outcome='PASS'),dict(layer='L2',outcome='FAIL')],source_sha256=source_digest(w),release_eligible=False,status='FAIL',error=OperationError('GATE_CHECK_FAILED',component='gate',phase='L2',outcome='FAIL').as_dict()))
`, root, dir], { stdio: 'pipe' });
    const files = new Map(['case.json', 'process-1.log'].map((n) => [n, readFileSync(join(dir, 'run/diagnostics', n))]));
    const source = readFileSync(join(dir, 'run/diagnostic-source.json'));
    const binding = JSON.parse(files.get('case.json')).binding;
    const artifact = { id: 99, sha256: 'b'.repeat(64) };
    return { files, source, binding, artifact, read() { return readDiagnosticCase(this.files, binding, artifact); } };
  } finally { rmSync(dir, { recursive: true, force: true }); }
}
async function zip(files) {
  const value = new yazl.ZipFile();
  for (const [name, content] of files) value.addBuffer(content, name);
  value.end(); const chunks = []; for await (const c of value.outputStream) chunks.push(c); return Buffer.concat(chunks);
}
function response(input) {
  const request = JSON.parse(input.body);
  const answers = Object.fromEntries(Object.entries(request.questions).map(([name, q]) => {
    const selected = name === 'category' ? 'source' : 'failure';
    return [name, { type: 'choice', choice: selected, confidence: 1, probabilities: Object.fromEntries(Object.keys(q.criteria).map((key) => [key, key === selected ? 1 : 0])) }];
  }));
  return { model: 'jev-1.13.0', answers, usage: { input_tokens: 42, output_tokens: 17 } };
}

test('Python producer -> bounded ZIP -> Node reader binds exact source, logs, references and profile', async () => {
  const f = fixture(); f.files = await readDiagnosticArchive(await zip(f.files)); const value = f.read();
  assert.equal(value.failure.code, 'TS2322'); assert.equal(value.failure.locations[0].path, 'app.ts');
  assert.equal(value.checks.find((c) => c.check_id === 'Q').outcome, 'NOT_RUN');
  assert.equal(verifyDiagnosticSource(f.source, value)[0].path, 'app.ts');
  assert.throws(() => verifyDiagnosticSource(Buffer.concat([f.source, Buffer.from(' ')]), value));
  assert.throws(() => readDiagnosticCase(f.files, { ...f.binding, tenant: 'other' }, f.artifact));
  f.files.set('process-1.log', Buffer.from('forged log'));
  assert.throws(() => f.read());
});

test('forged source locations, policy, attempts, private data and archive bombs are rejected', async () => {
  const f = fixture(), original = f.files.get('case.json');
  for (const mutate of [v => { v.failure.locations[0].line = 999; }, v => { v.failure.locations[0].path = '../app.ts'; },
    v => { v.policy.gate_order.push('Q'); }, v => { v.binding.producer_attempt = 2; }, v => { v.raw_secret = 'private'; }]) {
    const v = JSON.parse(original); mutate(v); f.files.set('case.json', Buffer.from(JSON.stringify(v))); assert.throws(() => f.read());
  }
  await assert.rejects(readDiagnosticArchive(await zip(new Map([['process-1.log', Buffer.alloc(90000)]]))));
  await assert.rejects(readDiagnosticArchive(await zip(new Map([['untrusted.txt', Buffer.from('x')]]))));
});

test('classifier has typed candidates and verified references; probabilities cannot authorize repair', async () => {
  const input = classificationInput(fixture().read()), value = response(input);
  const result = validateClassification(value, input, 'request-1');
  assert.equal(result.category.choice, 'source'); assert.equal(result.evidence_refs[0].path, 'case.json');
  assert.equal(result.grants_write_authority, false); assert.equal(result.is_hypothesis, true);
  for (const mutate of [v => { v.model = 'unknown'; }, v => { v.answers.category.probabilities.source = NaN; },
    v => { v.answers.category.probabilities.platform = 1; }, v => { v.answers.evidence.choice = 'invented'; },
    v => { v.answers.category.choice = 'dependency'; }, v => { v.answers.category.probabilities.other = 0; }]) {
    const v = structuredClone(value); mutate(v); assert.throws(() => validateClassification(v, input));
  }
  let calls = 0;
  const classify = createJevClassifier({ apiKey: 'private-key', fetchImpl: async (url, options) => {
    calls++; assert.equal(url, 'https://api.typesafe.ai/v1/systemone'); assert.equal(options.redirect, 'error');
    assert.equal(options.headers.authorization, 'Bearer private-key'); return Response.json(value);
  } });
  assert.equal((await classify(input)).state, 'succeeded'); assert.equal(calls, 1);
  calls = 0;
  const failed = createJevClassifier({ apiKey: 'private-key', fetchImpl: async () => { calls++; throw new Error('private-key'); } });
  const failure = await failed(input); assert.equal(calls, 1); assert.equal(failure.code, 'CLASSIFICATION_OUTCOME_UNKNOWN');
  assert.equal(JSON.stringify(failure).includes('private-key'), false);
});

test('artifact credentials are stripped on redirect; current workflow and attempt checked before and after', async () => {
  const f = fixture(), bytes = await zip(f.files), run = { id: 123, path: '.github/workflows/railshot-deploy.yml', head_sha: 'a'.repeat(40),
    head_branch: 'main', event: 'workflow_dispatch', repository: { full_name: 'org/apps' },
    html_url: 'https://github.com/org/apps/actions/runs/123', run_attempt: 1, status: 'completed' };
  let runs = 0, changeAttempt = false, redirect = 'https://test.blob.core.windows.net/diagnostics?sig=private';
  const service = createDeploymentService({ token: 'private-gh', owner: 'org', repo: 'apps', targetId: 'demo' }, async (url, options) => {
    const parsed = new URL(url);
    if (parsed.hostname === 'test.blob.core.windows.net') { assert.equal(options.headers, undefined); return new Response(bytes); }
    assert.equal(options.headers.authorization, 'Bearer private-gh');
    if (parsed.pathname.endsWith('/runs/123')) { runs++; return Response.json({ ...run, run_attempt: changeAttempt && runs % 2 === 0 ? 2 : 1 }); }
    if (parsed.pathname.endsWith('/runs/123/artifacts')) return Response.json({ total_count: 1, artifacts: [{ id: 99, name: 'diagnostics-1', expired: false,
      size_in_bytes: bytes.length, digest: `sha256:${sha256(bytes)}`, workflow_run: { id: 123, head_sha: run.head_sha } }] });
    assert.equal(options.redirect, 'manual'); return new Response(null, { status: 302, headers: { location: redirect } });
  });
  assert.equal((await service.diagnostics('123', f.binding)).state, 'ready');
  changeAttempt = true; await assert.rejects(service.diagnostics('123', f.binding));
  changeAttempt = false; redirect = 'https://attacker.invalid/'; await assert.rejects(service.diagnostics('123', f.binding));
});

test('central timeline deduplicates out-of-order CI rows without deciding deployment state', () => {
  const record = { id: 'dep', app: 'demo-app', target_id: 'demo', status: 'failed', ci: { run_id: 123 } };
  appendEvent(record, 'deployment.observed', 'ci', 'FAIL');
  const envelope = { run_id: '123', run_attempt: 1, checked_at: new Date().toISOString(), state: 'complete', items: [
    { sequence: 2, native_run_id: 'run', event_name: 'gate.layer.completed', phase: 'L2', outcome: 'FAIL', occurred_at: '2026-10-01T00:00:00Z' },
    { sequence: 1, native_run_id: 'run', event_name: 'gate.layer.started', phase: 'L2', outcome: 'RUNNING', occurred_at: '2026-10-01T00:00:00Z' }] };
  ingestCiEvents(record, envelope); ingestCiEvents(record, envelope);
  assert.equal(publicTelemetry(record).items.length, 3); assert.equal(record.status, 'failed');
  assert.equal(publicTelemetry(record).items[0].producer_key, undefined);
});

test('a stopped SDK observation preserves its metadata without inventing success or ongoing work', () => {
  const record = { id: 'dep', app: 'demo-app', target_id: 'demo', status: 'running', ci: { run_id: 123 } };
  ingestCiEvents(record, { run_id: '123', run_attempt: 1, checked_at: new Date().toISOString(), state: 'live', items: [
    { sequence: 1, native_run_id: 'run', event_name: 'agent.observation', occurred_at: new Date().toISOString(),
      process_running: false, snapshot_state: 'unavailable', last_sdk_event_age_ms: null }] });
  const event = publicTelemetry(record).items[0];
  assert.equal(event.outcome, 'UNKNOWN');
  assert.equal(event.attributes.process_running, false);
  assert.equal(event.attributes.last_sdk_event_age_ms, null);
  assert.equal(record.status, 'running');
});

test('classification is durable and single-call; GET and another session cannot spend a call', async (t) => {
  const { createProductStore } = await import('../src/product-store.js');
  const { createProductService } = await import('../src/product.js');
  const { createAppServer } = await import('../src/server.js');
  const { once } = await import('node:events');
  const directory = mkdtempSync(join(tmpdir(), 'railshot-classification-'));
  let product, server;
  t.after(async () => { if (server?.listening) await new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); }); await product?.close(); rmSync(directory, { recursive: true, force: true }); });
  const store = await createProductStore(directory), session = store.dashboard.session(), other = store.dashboard.session();
  const diagnostic = fixture().read();
  await store.transaction((state) => {
    state.operations.dep = { id: 'dep', kind: 'deployments', session_id: session.id, app: 'demo-app', target_id: 'demo',
      source_commit: 'a'.repeat(40), status: 'failed', stage: 'ci', created_at: new Date().toISOString(), ci: { run_id: '123', state: 'failed' } };
    state.bindings['123'] = { operation_id: 'dep', app: 'demo-app', target_id: 'demo', source_commit: 'a'.repeat(40) };
  });
  await store.close();
  let calls = 0, artifactReads = 0, release;
  const wait = new Promise((resolve) => { release = resolve; });
  const service = { targetId: 'demo', diagnostics: async () => { artifactReads++; return structuredClone(diagnostic); },
    status: async () => ({ run_id: 123, state: 'failed', status: 'completed', conclusion: 'failure', source_commit: 'a'.repeat(40) }) };
  const classifyFailure = async (input) => { calls++; await wait; return { state: 'succeeded', ...validateClassification(response(input), input, 'req-1') }; };
  product = await createProductService({ directory, service, classifyFailure });
  server = createAppServer({ product }); server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const url = `http://127.0.0.1:${server.address().port}/api/v1/deployments/dep`;
  const headers = { cookie: `railshot_session=${session.token}`, 'content-type': 'application/json' };
  const denied = await fetch(url + '/classifications', { method: 'POST', headers: { ...headers, cookie: `railshot_session=${other.token}` }, body: '{}' });
  assert.equal(denied.status, 404); assert.equal(calls, 0); assert.equal(artifactReads, 0);
  for (let i = 0; i < 3; i++) assert.equal((await fetch(url + '/diagnostics', { headers })).status, 200);
  for (let i = 0; i < 3; i++) assert.equal((await fetch(url, { headers })).status, 200);
  assert.equal(calls, 0); assert.equal(artifactReads, 1);
  const requests = await Promise.all(Array.from({ length: 6 }, () => fetch(url + '/classifications', { method: 'POST', headers, body: '{}' })));
  for (const r of requests) { assert.equal(r.status, 202); assert.equal(r.headers.get('location'), '/api/v1/deployments/dep/diagnostics'); }
  assert.equal(calls, 1); release();
  await new Promise((r) => setTimeout(r, 30));
  assert.equal((await (await fetch(url + '/diagnostics', { headers })).json()).classification.state, 'succeeded');
  await new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); }); await product.close();
  product = await createProductService({ directory, service, classifyFailure });
  assert.equal((await product.classifyDeployment('dep', session.id)).state, 'succeeded'); assert.equal(calls, 1);
  assert.equal((await product.getDeployment('dep', session.id)).status, 'failed');
});

test('restart marks a persisted in-flight classification unknown without replaying it', async (t) => {
  const { createProductStore } = await import('../src/product-store.js');
  const directory = mkdtempSync(join(tmpdir(), 'railshot-interrupted-classification-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  let store = await createProductStore(directory);
  await store.transaction((state) => { state.operations.dep = { id: 'dep', kind: 'deployments', status: 'failed',
    classification: { state: 'running', input_sha256: 'abc' }, classifications: { abc: { state: 'running', input_sha256: 'abc' } } }; });
  await store.close(); store = await createProductStore(directory);
  assert.equal(store.read().operations.dep.classification.code, 'CLASSIFICATION_INTERRUPTED');
  assert.equal(store.read().operations.dep.classification.outcome_unknown, true); await store.close();
});
