import test from 'node:test';
import assert from 'node:assert/strict';
import { createToolRunner, tools } from '../src/tools.js';

const deployment = { id: 'dep-1', app: 'demo-app', target_id: 'demo', source_commit: 'a'.repeat(40),
  status: 'failed', stage: 'ci', error: { message: 'CI 검사가 완료되지 않았습니다.' },
  ci: { run_id: 42, status: 'failed', steps: [{ name: 'build', status: 'failed' }] } };
const diagnostic = { deployment_id: 'dep-1', state: 'ready',
  binding: { app: deployment.app, target_id: deployment.target_id,
    source_commit: deployment.source_commit, run_id: '42' },
  failure: { excerpt: 'required railshot.yaml missing' },
  logs: [{ process_id: 'process-1', text: 'exactly one railshot.yaml is required' }] };

test('MCP progress exposes only diagnostics bound to the selected deployment', async () => {
  const api = { deployment: async () => deployment,
    deploymentEvents: async () => ({ deployment_id: 'dep-1', status: 'completed', progress: { poll_after_ms: 5000 } }),
    deploymentDiagnostics: async () => diagnostic };
  const call = createToolRunner(api);
  const result = await call('get_deployment_progress', { deployment_id: 'dep-1' });
  assert.equal(result.status, 'failed');
  assert.equal(result.error.message, deployment.error.message);
  assert.equal(result.diagnostics.failure.excerpt, diagnostic.failure.excerpt);
  assert.equal(result.diagnostics.logs[0].text, diagnostic.logs[0].text);
  assert.equal(result.poll_after_ms, null);

  api.deploymentDiagnostics = async () => ({ ...diagnostic, binding: { ...diagnostic.binding, run_id: '43' } });
  const stale = await call('get_deployment_progress', { deployment_id: 'dep-1' });
  assert.equal(stale.diagnostics.state, 'unavailable');
  assert.deepEqual(stale.diagnostics.logs, []);
  assert.equal(stale.diagnostics.failure, null);
});

test('MCP progress can be repeated during a running build and rejects folder input', async () => {
  let status = 'running';
  const api = { deployment: async () => ({ ...deployment, status, error: null }),
    deploymentEvents: async () => ({ deployment_id: 'dep-1', status: 'running', progress: { poll_after_ms: 7000 } }),
    deploymentDiagnostics: async () => { throw new Error('진행 중에는 호출하지 않아야 합니다.'); } };
  const call = createToolRunner(api);
  assert.equal((await call('get_deployment_progress', { deployment_id: 'dep-1' })).poll_after_ms, 7000);
  status = 'succeeded';
  assert.equal((await call('get_deployment_progress', { deployment_id: 'dep-1' })).poll_after_ms, null);
  assert.match(tools.deploy_repository.description, /로컬 폴더/);
  await assert.rejects(call('deploy_repository', { repository_url: '/Users/me/app', app: 'demo-app',
    target_id: 'demo', idempotency_key: 'request-1' }));
});

test('MCP progress tracks AI repair revisions and does not replay an older Actions attempt', async () => {
  let attempt = 1, revision = 22, activityId = 'dep-1:1:run-1', observationState = 'current';
  const api = { deployment: async () => ({ ...deployment, status: 'running', error: null,
    ci: { ...deployment.ci, producer_attempt: attempt } }),
  deploymentEvents: async () => ({ deployment_id: 'dep-1', run_attempt: attempt, status: 'running', state: 'current',
    progress: { poll_after_ms: 15000 }, agent_activity: { id: activityId, revision, state: 'verifying',
      summary: '배포 설정 검사를 복구 중입니다.', current_action: '공식 검사 진행 중', attempt: 1,
      changes: [{ path: 'Dockerfile', summary: '시작 명령 변경', status: 'applied' }],
      verification: [{ key: 'image.build', state: 'succeeded' }], previous_attempts: [],
      observation: { state: observationState } } }),
  deploymentDiagnostics: async () => { throw new Error('진행 중 진단 호출'); } };
  const call = createToolRunner(api);
  const first = await call('get_deployment_progress', { deployment_id: 'dep-1' });
  assert.equal(first.run_attempt, 1);
  assert.equal(first.deployment_updated, true);
  assert.equal(first.agent_activity_updated, true);
  assert.equal(first.agent_activity.changes[0].status, 'applied');
  assert.equal(first.agent_activity.verification[0].key, 'image.build');
  assert.equal(first.activity_cursor.revision, 22);
  const unchanged = await call('get_deployment_progress', { deployment_id: 'dep-1', since: first.activity_cursor });
  assert.equal(unchanged.agent_activity_updated, false);
  assert.equal(unchanged.deployment_updated, false);
  observationState = 'stale';
  const delayed = await call('get_deployment_progress', { deployment_id: 'dep-1', since: first.activity_cursor });
  assert.equal(delayed.agent_activity_updated, true);
  assert.equal(delayed.agent_activity.observation.state, 'stale');
  observationState = 'current';
  revision = 23;
  const updated = await call('get_deployment_progress', { deployment_id: 'dep-1', since: first.activity_cursor });
  assert.equal(updated.agent_activity_updated, true);
  revision = 21;
  const oldRevision = await call('get_deployment_progress', { deployment_id: 'dep-1', since: updated.activity_cursor });
  assert.equal(oldRevision.events_state, 'stale_response');
  assert.equal(oldRevision.agent_activity, null);
  assert.deepEqual(oldRevision.activity_cursor, updated.activity_cursor);

  attempt = 2; activityId = 'dep-1:2:run-2'; revision = 1;
  const retry = await call('get_deployment_progress', { deployment_id: 'dep-1', since: updated.activity_cursor });
  assert.equal(retry.run_attempt_changed, true);
  assert.equal(retry.agent_activity_updated, true);
  assert.equal(retry.activity_cursor.run_attempt, 2);

  api.deploymentEvents = async () => ({ deployment_id: 'dep-1', run_attempt: 1, status: 'running',
    agent_activity: { id: 'dep-1:1:run-1', revision: 99, state: 'verifying' } });
  const stale = await call('get_deployment_progress', { deployment_id: 'dep-1', since: retry.activity_cursor });
  assert.equal(stale.events_state, 'stale_response');
  assert.equal(stale.agent_activity, null);
  assert.equal(stale.agent_activity_updated, false);
});
