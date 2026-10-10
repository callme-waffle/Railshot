import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { mkdtemp, readdir, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createAgent } from '../src/agent.js';
import { connectTools } from '../src/mcp-client.js';
import { createAgentServer } from '../src/server.js';
import { createModel } from '../src/model.js';

const secret = 'railshot-agent-test-secret-32-characters';
const repository = 'https://github.com/example/demo';
const args = { repository_url: repository, app: 'demo-app', target_id: 'demo', idempotency_key: 'same-intent' };

test('agent queries MCP read tool and requires an exact approval before deployment', async () => {
  const calls = [];
  const client = {
    list: async () => [
      { name: 'list_targets', description: 'targets', inputSchema: { type: 'object', properties: {} } },
      { name: 'deploy_repository', description: 'deploy', inputSchema: { type: 'object', properties: {} } },
    ],
    call: async (name, input) => { calls.push([name, input]); return name === 'list_targets' ? { items: [{ id: 'demo' }] } : { resource_id: 'dep-1', status: 'accepted' }; },
    close: () => {},
  };
  let step = 0;
  const model = async () => {
    step++;
    if (step === 1) return [{ type: 'function_call', name: 'list_targets', arguments: '{}', call_id: 'one' }];
    return [{ type: 'function_call', name: 'deploy_repository', arguments: JSON.stringify(args), call_id: 'two' }];
  };
  const agent = createAgent({ secret, model, connect: async () => client });
  const proposed = await agent.message('demo 앱을 배포해 줘');
  assert.equal(proposed.status, 'approval_required');
  assert.deepEqual(calls.map(([name]) => name), ['list_targets']);
  assert.deepEqual({ ...proposed.action.arguments, idempotency_key: args.idempotency_key }, args);
  assert.match(proposed.action.arguments.idempotency_key, /^[0-9a-f-]{36}$/);
  const accepted = await agent.approve(proposed.approval_token);
  assert.equal(accepted.result.resource_id, 'dep-1');
  assert.deepEqual(calls.map(([name]) => name), ['list_targets', 'deploy_repository']);
  await assert.rejects(agent.approve(`${proposed.approval_token}x`), { status: 422 });
});

test('MCP stdio persists the session from deployment acceptance across process restarts', async (t) => {
  const received = [];
  const cookie = `railshot_session=${'s'.repeat(43)}`;
  const sessionDirectory = await mkdtemp(join(tmpdir(), 'railshot-agent-session-'));
  t.after(() => rm(sessionDirectory, { recursive: true, force: true }));
  const api = createServer(async (request, response) => {
    received.push({ method: request.method, url: request.url, authorization: request.headers.authorization,
      cookie: request.headers.cookie, key: request.headers['idempotency-key'] });
    if (!request.headers.cookie) response.setHeader('set-cookie', `${cookie}; Path=/; HttpOnly; SameSite=Strict`);
    if (request.url === '/api/v1/targets') response.end(JSON.stringify({ items: [{ id: 'demo' }], next_marker: null }));
    else if (request.url === '/api/v1/deployments') {
      for await (const _ of request) { /* consume form */ }
      response.writeHead(202, { 'content-type': 'application/json' })
        .end(JSON.stringify({ resource_id: 'dep-1', status: 'accepted' }));
    } else if (request.url === '/api/v1/deployments/dep-1') {
      response.writeHead(request.headers.cookie === cookie ? 200 : 404, { 'content-type': 'application/json' })
        .end(JSON.stringify({ id: 'dep-1', status: 'succeeded', url: 'https://demo.example' }));
    } else response.writeHead(404).end('{}');
  });
  api.listen(0, '127.0.0.1'); await once(api, 'listening');
  t.after(() => api.close());
  const env = { ...process.env, RAILSHOT_API_URL: `http://127.0.0.1:${api.address().port}`,
    RAILSHOT_API_TOKEN: 'a'.repeat(32), RAILSHOT_API_TOKEN_FILE: undefined,
    RAILSHOT_AGENT_SESSION_DIR: sessionDirectory };
  const client = await connectTools({ env });
  t.after(() => client.close());
  const listed = await client.list();
  assert.match(listed.find((tool) => tool.name === 'deploy_repository').description, /로컬 폴더/);
  assert.ok(listed.some((tool) => tool.name === 'get_deployment_progress'));
  const submitted = await client.call('deploy_repository', args);
  assert.equal(submitted.status, 'accepted');
  client.close();
  const restarted = await connectTools({ env });
  t.after(() => restarted.close());
  assert.equal((await restarted.call('get_deployment', { deployment_id: 'dep-1' })).url, 'https://demo.example');
  assert.deepEqual((await restarted.call('list_targets', {})).items, [{ id: 'demo' }]);
  assert.deepEqual(received.map((item) => item.url), ['/api/v1/deployments', '/api/v1/deployments/dep-1', '/api/v1/targets']);
  assert.equal(received[0].cookie, undefined);
  assert.equal(received[1].cookie, cookie);
  assert.equal(received[2].cookie, cookie);
  assert.equal(received[0].authorization, `Bearer ${'a'.repeat(32)}`);
  assert.equal(received[0].key, 'same-intent');
  const files = await readdir(sessionDirectory);
  assert.equal(files.length, 1);
  assert.equal((await stat(join(sessionDirectory, files[0]))).mode & 0o777, 0o600);
});

test('HTTP service rejects unauthenticated calls and exposes approval flow', async (t) => {
  const agent = { message: async () => ({ status: 'answered', answer: '조회 결과', events: [] }), approve: async () => ({ status: 'submitted' }) };
  const server = createAgentServer({ secret, agent });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => server.close());
  const url = `http://127.0.0.1:${server.address().port}/v1/messages`;
  const body = JSON.stringify({ message: '상태 알려줘' });
  assert.equal((await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body })).status, 401);
  const response = await fetch(url, { method: 'POST', headers: { authorization: `Bearer ${secret}`, 'content-type': 'application/json' }, body });
  assert.equal(response.status, 200);
  assert.equal((await response.json()).answer, '조회 결과');
});

test('expired approval cannot execute and uncertain outcome is returned without retry', async () => {
  let now = 1000, calls = 0;
  const client = { list: async () => [{ name: 'deploy_repository', description: 'deploy', inputSchema: { type: 'object', properties: {} } }],
    call: async () => { calls++; const error = new Error('실행 결과를 확인할 수 없습니다.'); error.code = 'UPSTREAM_FAILURE'; error.outcomeUnknown = true; throw error; },
    close: () => {} };
  const agent = createAgent({ secret, now: () => now, connect: async () => client,
    model: async () => [{ type: 'function_call', name: 'deploy_repository', arguments: JSON.stringify(args), call_id: 'one' }] });
  const proposed = await agent.message('배포해 줘');
  assert.equal(calls, 0);
  const result = await agent.approve(proposed.approval_token);
  assert.equal(result.status, 'unknown');
  assert.equal(result.error.outcome_unknown, true);
  assert.equal(calls, 1);
  now += 10 * 60 * 1000 + 1;
  await assert.rejects(agent.approve(proposed.approval_token), { status: 422 });
  assert.equal(calls, 1);
});

test('model request uses Responses function tools without storing response state', async () => {
  let sent;
  const model = createModel({ apiKey: 'model-secret', model: 'configured-model', fetchImpl: async (_url, request) => {
    sent = { headers: request.headers, body: JSON.parse(request.body) };
    return { ok: true, json: async () => ({ status: 'completed', output: [{ type: 'message', content: [{ type: 'output_text', text: '완료' }] }] }) };
  } });
  const output = await model({ input: [{ role: 'user', content: '상태' }], tools: [], instructions: '규칙' });
  assert.equal(output[0].type, 'message');
  assert.equal(sent.body.store, false);
  assert.equal(sent.body.model, 'configured-model');
  assert.equal(sent.headers.authorization, 'Bearer model-secret');
});
