import test from 'node:test';
import assert from 'node:assert/strict';
import { request } from '../../dashboard/src/api.js';

test('keyed lifecycle retry preserves exact request; unknown unkeyed writes are not repeated', async (t) => {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (_path, options) => {
    calls.push(options);
    return calls.length === 1 ? new Response('<html>Bad Gateway</html>', { status: 502 })
      : Response.json({ id: 'original-operation', status: 'unknown' });
  });
  const options = { method: 'POST', headers: { 'Idempotency-Key': 'same-deletion-request' }, body: '{"plan_id":"same"}' };
  assert.equal((await request('/api/v1/applications/owned/operations', options)).data.id, 'original-operation');
  assert.equal(calls.length, 2); assert.equal(calls[0].body, calls[1].body);
  assert.deepEqual(calls[0].headers, calls[1].headers);
  calls.length = 0;
  await assert.rejects(request('/api/v1/applications/owned/operations', { method: 'POST', body: '{}' }), { code: 'RESPONSE_UNREADABLE' });
  assert.equal(calls.length, 1);
});

test('cancellation interrupts handover retry and application conflicts are never retried', async (t) => {
  let calls = 0;
  t.mock.method(globalThis, 'fetch', async () => { calls++; return new Response('unavailable', { status: 502 }); });
  const controller = new AbortController();
  const pending = request('/api/v1/options', {}, controller);
  setTimeout(() => controller.abort(), 20);
  await assert.rejects(pending, { name: 'AbortError' }); assert.equal(calls, 1);
  t.mock.restoreAll(); calls = 0;
  t.mock.method(globalThis, 'fetch', async () => { calls++; return Response.json({ error: { code: 'APPLICATION_PLAN_STALE' } }, { status: 409 }); });
  await assert.rejects(request('/api/v1/applications/owned/plans', { method: 'POST', headers: { Prefer: 'respond-async' } }), { code: 'APPLICATION_PLAN_STALE' });
  assert.equal(calls, 1);
});
