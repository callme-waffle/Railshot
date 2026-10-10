import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as pause } from 'node:timers/promises';
import { createDeploymentService } from '../src/github.js';
import { createAppServer } from '../src/server.js';
import { apiAccessConfig } from '../src/access.js';
import { summarizeAgentEvents } from '../src/agent-events.js';

const sourceCommit = 'a'.repeat(40);
const binding = { source_commit: sourceCommit, app: 'demo-app', target_id: 'demo' };
function fixture() {
  const now = new Date().toISOString();
  const envelope = { version: 1, run_id: '123', run_attempt: 1, source_commit: sourceCommit, app: 'demo-app', tenant: 'demo',
    target_id: 'demo', updated_at: now, status: 'running', truncated: false,
    items: [{ sequence: 1, occurred_at: now, event_name: 'agent.heartbeat', native_run_id: 'native:123', attempt_id: 'native:123:1',
      role: 'fixer', provider: 'codex', elapsed_ms: 12000, process_running: true, snapshot_state: 'current',
      sdk_activity_since_previous: false, last_sdk_event_age_ms: 11000,
      progress: { elapsed_ms: 1000, sdk_event_count: 4, last_sdk_event_at_ms: 1000, item_counts: { reasoning: 1, commandExecution: 1 },
        last_item: { kind: 'reasoning', status: 'inProgress' }, token_usage: { input_tokens: 100, output_tokens: 2 } } }] };
  const check = { id: 500, name: 'Railshot agent events', external_id: 'railshot-events:123:1', head_sha: sourceCommit,
    details_url: 'https://github.com/org/apps/runs/500', app: { slug: 'github-actions', id: 15368 },
    status: 'in_progress', conclusion: null, output: { text: JSON.stringify(envelope) } };
  const run = { id: 123, path: '.github/workflows/railshot-deploy.yml', head_sha: sourceCommit, head_branch: 'main',
    event: 'workflow_dispatch', repository: { full_name: 'org/apps' }, html_url: 'https://github.com/org/apps/actions/runs/123', run_attempt: 1, status: 'in_progress' };
  const f = { envelope, check, checks: [check], run, calls: [], onRun: null, rawBody: null };
  f.service = createDeploymentService({ token: 'server-secret-canary', owner: 'org', repo: 'apps', targetId: 'demo' }, async (url, options) => {
    f.calls.push(url); assert.equal(options.redirect, 'error'); assert.equal(options.headers.authorization, 'Bearer server-secret-canary');
    const path = new URL(url);
    if (path.pathname.endsWith('/actions/runs/123')) { f.onRun?.(); return Response.json(f.run); }
    assert.equal(path.pathname, `/repos/org/apps/commits/${sourceCommit}/check-runs`);
    assert.equal(path.searchParams.get('check_name'), 'Railshot agent events');
    assert.equal(path.searchParams.get('filter'), 'all'); assert.equal(path.searchParams.get('app_id'), '15368');
    assert.equal(path.searchParams.get('per_page'), '20');
    const start = (Number(path.searchParams.get('page')) - 1) * 20;
    return f.rawBody ? new Response(f.rawBody) : Response.json({ total_count: f.checks.length, check_runs: f.checks.slice(start, start + 20) });
  });
  f.save = () => { f.check.output.text = JSON.stringify(f.envelope); };
  f.read = (identity = binding) => f.service.events('123', identity);
  return f;
}

test('current SDK metadata is content-free, cached briefly and independent of CI success', async () => {
  const f = fixture(), first = await f.read();
  assert.equal(first.state, 'live'); assert.equal(first.run_attempt, 1); assert.equal(first.stale, false);
  assert.equal(first.next_marker, null);
  assert.equal(first.items[0].progress.sdk_event_count, 4); assert.equal(first.items[0].sdk_activity_since_previous, false);
  first.items[0].role = 'mutated-client';
  const second = await f.read();
  assert.equal(second.items[0].role, 'fixer'); assert.equal(f.calls.filter((url) => url.includes('check-runs')).length, 1);
  f.run.run_attempt = 2; f.run.status = 'completed'; f.run.conclusion = 'failure';
  f.check.external_id = 'railshot-events:123:2'; f.check.status = 'completed'; f.check.conclusion = 'neutral';
  f.envelope.run_attempt = 2; f.envelope.status = 'completed'; f.save();
  const rerun = await f.read();
  assert.equal(rerun.run_attempt, 2); assert.equal(rerun.state, 'complete');
  assert.equal('conclusion' in rerun, false, 'observer completion must not claim successful CI');
  assert.equal(f.calls.filter((url) => url.includes('check-runs')).length, 2);
  assert.equal(JSON.stringify(rerun).includes('server-secret-canary'), false);
});

test('current attempt cannot fall back to an old producer and a concurrent rerun discards its snapshot', async () => {
  const old = fixture(); old.run.run_attempt = 2;
  assert.equal((await old.read()).reason, 'not_available');
  const f = fixture(); let calls = 0;
  f.onRun = () => { if (++calls === 2) f.run.run_attempt = 2; };
  const result = await f.read(); assert.equal(result.state, 'unavailable'); assert.equal(result.reason, 'attempt_changed');
  assert.deepEqual(result.items, []);
});

test('the requested workflow details URL is also accepted', async () => {
  const f = fixture(); f.check.details_url = f.run.html_url;
  assert.equal((await f.read()).state, 'live');
});

test('complete paginated listings reject duplicate external IDs instead of selecting a convenient producer', async () => {
  const f = fixture();
  f.checks = Array.from({ length: 20 }, (_, i) => ({ id: 1000 + i, external_id: `railshot-events:${1000 + i}:1` })).concat(f.check);
  assert.equal((await f.read()).state, 'live');
  assert.equal(f.calls.filter((url) => url.includes('check-runs')).length, 2);
  const duplicate = fixture(); duplicate.checks.push({ ...duplicate.check, id: 501 });
  assert.equal((await duplicate.read()).reason, 'producer_mismatch');
  const tooMany = fixture(); tooMany.checks = Array.from({ length: 101 }, (_, id) => ({ id }));
  assert.equal((await tooMany.read()).reason, 'producer_mismatch');
});

test('malformed, oversized, foreign and raw SDK payloads never leave the API', async (t) => {
  const mutations = {
    'foreign workflow': (f) => { f.run.path = '.github/workflows/evil.yml'; },
    'foreign ref': (f) => { f.run.head_branch = 'evil'; },
    'foreign source': (f) => { f.run.head_sha = 'b'.repeat(40); },
    'foreign repository': (f) => { f.run.repository.full_name = 'evil/apps'; },
    'wrong producer': (f) => { f.check.app.slug = 'other'; },
    'wrong app id': (f) => { f.check.app.id = 1; },
    'wrong details URL': (f) => { f.check.details_url += '/attempts/1'; },
    'another check URL': (f) => { f.check.details_url = 'https://github.com/org/apps/runs/501'; },
    'another repository check URL': (f) => { f.check.details_url = 'https://github.com/other/apps/runs/500'; },
    'wrong check head': (f) => { f.check.head_sha = 'b'.repeat(40); },
    'wrong check name': (f) => { f.check.name = 'other'; },
    'wrong app binding': (f) => { f.envelope.app = 'other-app'; f.save(); },
    'wrong tenant binding': (f) => { f.envelope.tenant = 'other'; f.save(); },
    'wrong target binding': (f) => { f.envelope.target_id = 'other'; f.save(); },
    'wrong attempt envelope': (f) => { f.envelope.run_attempt = 2; f.save(); },
    'raw text': (f) => { f.envelope.items[0].text = 'raw-secret-canary'; f.save(); },
    'raw command': (f) => { f.envelope.items[0].progress.last_item.command = 'raw-secret-canary'; f.save(); },
    'raw token map': (f) => { f.envelope.items[0].progress.token_usage.api_key = 'raw-secret-canary'; f.save(); },
    'raw envelope': (f) => { f.envelope.reasoning = 'raw-secret-canary'; f.save(); },
    'unknown event': (f) => { f.envelope.items[0].event_name = 'raw-secret-canary'; f.save(); },
    'too many rows': (f) => { f.envelope.items = Array(61).fill(f.envelope.items[0]); f.save(); },
    'duplicate sequence': (f) => { f.envelope.items.push(f.envelope.items[0]); f.save(); },
    'mixed native run': (f) => { f.envelope.items.push({ ...f.envelope.items[0], sequence: 2, native_run_id: 'other' }); f.save(); },
    'foreign native attempt': (f) => { f.envelope.items[0].attempt_id = 'other:1'; f.save(); },
    'future timestamp': (f) => { f.envelope.updated_at = '2999-01-01T00:00:00Z'; f.save(); },
    'oversized text': (f) => { f.check.output.text = 'x'.repeat(60000); },
    'oversized transport': (f) => { f.rawBody = 'x'.repeat(2 * 1024 * 1024 + 1); },
    'invalid JSON': (f) => { f.check.output.text = 'raw-secret-canary'; },
  };
  for (const [name, mutate] of Object.entries(mutations)) await t.test(name, async () => {
    const f = fixture(); mutate(f); const result = await f.read();
    assert.equal(result.state, 'unavailable'); assert.deepEqual(result.items, []);
    assert.equal(JSON.stringify(result).includes('raw-secret-canary'), false);
  });
});

test('stale observations, zero SDK invocation, truncation and no data stay explicit', async () => {
  const f = fixture(); f.envelope.updated_at = new Date(Date.now() - 120000).toISOString();
  f.envelope.items = [{ sequence: 50, occurred_at: f.envelope.updated_at, event_name: 'loop.completed', native_run_id: 'native:123',
    phase: 'loop', outcome: 'PASS', sdk_invocations: 0 }]; f.envelope.truncated = true; f.save();
  const stale = await f.read(); assert.equal(stale.stale, true); assert.equal(stale.truncated, true); assert.equal(stale.items[0].sdk_invocations, 0);
  const empty = fixture(); empty.envelope.items = []; empty.save(); assert.equal((await empty.read()).state, 'no_data');
});

test('real events HTTP route binds ownership before GitHub reads, rejects controls and preserves a safe failure', async (t) => {
  const f = fixture(), directory = await mkdtemp(join(tmpdir(), 'railshot-events-http-'));
  const service = { ...f.service, deploy: async () => ({ run_id: 123, source_commit: sourceCommit }),
    status: async () => ({ run_id: 123, state: 'published', status: 'completed', conclusion: 'success',
      publication: { run_id: 123, ...binding, artifact_id: 9, producer_attempt: 1 } }) };
  const server = createAppServer({ service, stateDirectory: directory, pollInterval: 1,
    access: apiAccessConfig({ RAILSHOT_PUBLIC_DEMO: '1', RAILSHOT_BIND_HOST: '0.0.0.0',
      RAILSHOT_ALLOWED_HOSTS: '127.0.0.1', RAILSHOT_ALLOWED_ORIGINS: 'http://127.0.0.1' }),
    deployPublished: async () => ({ cd: { state: 'deployed', deployed: true }, public_http: { state: 'succeeded', url: 'https://example.test' } }) });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { await new Promise((resolve) => server.close(resolve)); await (await server.productReady).close(); await rm(directory, { recursive: true, force: true }); });
  const origin = `http://127.0.0.1:${server.address().port}`;
  const session = async () => (await fetch(origin + '/api/v1/sessions', { method: 'POST' })).headers.get('set-cookie').split(';')[0];
  const owner = await session(), foreign = await session();
  const form = new FormData(); form.set('app', binding.app); form.set('target_id', binding.target_id);
  form.set('paths', '["app.js"]'); form.append('files', new Blob(['hello']), 'app.js');
  const accepted = await fetch(origin + '/api/v1/deployments', { method: 'POST', headers: { Cookie: owner, 'Idempotency-Key': 'events' }, body: form });
  assert.equal(accepted.status, 202);
  const id = (await accepted.json()).resource_id, path = `/api/v1/deployments/${id}/events`;
  for (let n = 0; n < 200; n++) {
    const state = await (await fetch(origin + `/api/v1/deployments/${id}`, { headers: { Cookie: owner } })).json();
    if (state.status === 'succeeded') break;
    await pause(10);
  }
  await pause(30); // Allow the authorized terminal background collection to settle.
  const authorizedReads = f.calls.length;
  for (const cookie of [foreign, null]) {
    assert.equal((await fetch(origin + path, { headers: cookie ? { Cookie: cookie } : {} })).status, 404);
    assert.equal(f.calls.length, authorizedReads);
  }
  assert.equal((await fetch(origin + path + '?run_id=999', { headers: { Cookie: owner } })).status, 422);
  assert.equal((await fetch(origin + path, { method: 'POST', headers: { Cookie: owner } })).status, 405);
  const response = await fetch(origin + path, { headers: { Cookie: owner } }), body = await response.json();
  assert.equal(response.status, 200); assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.equal(body.deployment_id, id); assert.equal(body.state, 'live'); assert.equal(body.run_id, '123');
  assert.equal(body.items[0].progress.sdk_event_count, 4);
  assert.equal(body.agent_activity.stage, 'build');
  assert.equal(body.agent_activity.state, 'analyzing');
  const detail = await (await fetch(origin + `/api/v1/deployments/${id}`, { headers: { Cookie: owner } })).json();
  assert.equal(detail.agent_activity_summary.id, body.agent_activity.id);
  assert.equal(body.progress.poll_after_ms, 15000);
  assert.equal(body.progress.latest.attempt_id, 'native:123:1');
  assert.equal(body.progress.sdk_invocations, null, 'heartbeat is not an SDK call receipt');
  assert.equal(body.progress.agent_budget, null, 'older producers do not declare a budget');
  const stored = body.timeline.items.find((event) => event.event_name === 'agent.heartbeat');
  assert.equal(stored.attributes.process_running, true);
  assert.deepEqual(stored.attributes.progress.token_usage, { input_tokens: 100, output_tokens: 2 });
  const count = f.calls.length;
  assert.equal((await fetch(origin + path, { headers: { Cookie: foreign } })).status, 404);
  assert.equal(f.calls.length, count, 'another session cannot reach a warmed event cache');
  f.run.run_attempt = 2; f.check.external_id = 'railshot-events:123:2'; f.envelope.run_attempt = 2;
  f.envelope.items[0].command = 'raw-secret-canary'; f.save();
  const rejected = await (await fetch(origin + path, { headers: { Cookie: owner } })).json();
  assert.equal(rejected.state, 'unavailable'); assert.deepEqual(rejected.items, []);
  assert.equal(JSON.stringify(rejected).includes('raw-secret-canary'), false);
});

test('declared optional agent budget is bounded and preserved without fabricating SDK usage', async () => {
  const f = fixture();
  const started = { sequence: 1, occurred_at: f.envelope.updated_at, event_name: 'loop.started', native_run_id: 'native:123',
    phase: 'loop', outcome: 'RUNNING', sdk_invocations: 0, agent_budget: { enabled: true, max_invocations: 4 } };
  f.envelope.items = [started]; f.save();
  const observed = await f.read();
  assert.equal(observed.state, 'live');
  const summary = summarizeAgentEvents(observed);
  assert.deepEqual(summary.agent_budget, started.agent_budget);
  assert.equal(summary.sdk_invocations, 0);
  for (const budget of [{ enabled: true, max_invocations: 17 }, { enabled: false, max_invocations: 4 },
    { enabled: true, max_invocations: '2' }, { enabled: true, max_invocations: 2, command: 'raw-secret-canary' }]) {
    const bad = fixture(); bad.envelope.items = [{ ...started, agent_budget: budget }]; bad.save();
    assert.equal((await bad.read()).reason, 'invalid_payload');
  }
  for (const limit of [0, 1, 2, 3, 10, 16]) {
    const older = fixture();
    older.envelope.items = [{ ...started, agent_budget: { enabled: limit > 0, max_invocations: limit } }]; older.save();
    assert.equal((await older.read()).state, 'live', 'older and disabled budgets remain compatible');
  }
  const completed = fixture();
  completed.envelope.items = [{ ...started, event_name: 'loop.completed', outcome: 'PASS', sdk_invocations: 4 }]; completed.save();
  const final = await completed.read();
  assert.equal(final.state, 'live');
  assert.equal(summarizeAgentEvents(final).sdk_invocations, 4);
});

test('summary retains only the current attempt budget and distinguishes heartbeat from final usage', () => {
  const f = fixture(), timeline = { items: [{ correlation: { github_run_id: '123', github_run_attempt: 1 },
    attributes: { agent_budget: { enabled: true, max_invocations: 4 } } }] };
  const summary = summarizeAgentEvents(f.envelope, timeline);
  assert.equal(summary.agent_budget.max_invocations, 4);
  assert.equal(summary.sdk_invocations, null);
  assert.equal(summary.latest.progress.sdk_event_count, 4);
  assert.equal(summarizeAgentEvents({ ...f.envelope, run_attempt: 2 }, timeline).agent_budget, null);
  assert.equal(summarizeAgentEvents({ ...f.envelope, run_id: '456' }, timeline).agent_budget, null);
  const final = { ...f.envelope.items[0], event_name: 'loop.completed', sdk_invocations: 2 };
  assert.equal(summarizeAgentEvents({ ...f.envelope, items: [...f.envelope.items, final] }, timeline).sdk_invocations, 2);
  const unavailable = summarizeAgentEvents({ items: [], run_id: '123', run_attempt: null }, timeline);
  assert.equal(unavailable.latest, null);
  assert.equal(unavailable.agent_budget, null);
  assert.equal(unavailable.sdk_invocations, null);
});

test('deterministic gate progress is admitted with no agent call and rejects forged counters', async () => {
  const f = fixture(), now = f.envelope.updated_at;
  f.envelope.items = [{ sequence: 1, occurred_at: now, event_name: 'gate.layer.completed', native_run_id: 'native:123',
    attempt_id: 'native:123:0', phase: 'L2', outcome: 'FAIL', completed_steps: 3, total_steps: 4, duration_s: 1.25 }]; f.save();
  const result = await f.read(); assert.equal(result.items[0].event_name, 'gate.layer.completed');
  const bad = fixture(); bad.envelope.items = [{ ...f.envelope.items[0], completed_steps: 7 }]; bad.save();
  assert.equal((await bad.read()).state, 'unavailable');
});

test('host repair receipts expose bounded redacted descriptions, never arbitrary model fields', async () => {
  const f = fixture();
  f.envelope.items = [{ sequence: 1, occurred_at: f.envelope.updated_at, event_name: 'agent.repair', native_run_id: 'native:123', attempt_id: 'native:123:1',
    repair: { state: 'verifying', role: 'fixer', failure_layer: 'L2', omitted_changes: 0,
      changes: [{ path: 'Dockerfile', summary: 'token=private-example', status: 'applied' }] } }];
  f.save();
  const data = await f.read(); assert.equal(data.state, 'live');
  assert.equal(data.items[0].repair.changes[0].summary, '[REDACTED]');
  for (const mutate of [r => r.changes[0].path = '../secret', r => r.raw_prompt = 'secret', r => r.changes[0].status = 'proposed',
    r => r.changes[0].summary = 'x'.repeat(301), r => r.state = 'deployed']) {
    const bad = fixture(); bad.envelope.items = structuredClone(f.envelope.items); mutate(bad.envelope.items[0].repair); bad.save();
    assert.equal((await bad.read()).state, 'unavailable');
  }
});
