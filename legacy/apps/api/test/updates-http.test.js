import test from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import yazl from 'yazl';
import { createAppServer } from '../src/server.js';
import { ProductError } from '../src/product.js';
import { inspectArchive } from '../src/archive.js';

async function archive() {
  const zip = new yazl.ZipFile(); zip.addBuffer(Buffer.from('updated'), 'index.js'); zip.end();
  const chunks = []; for await (const chunk of zip.outputStream) chunks.push(chunk);
  return Buffer.concat(chunks);
}

test('update HTTP routes accept all source formats, pin the app, and expose bounded source downloads', async (t) => {
  const calls = [], id = 'c8898221-8e56-4c04-8660-0b29058342cf';
  const product = {
    getApplication: (app, session) => {
      assert.equal(session, null);
      if (app !== 'app-example') throw new ProductError(404, 'NOT_FOUND', '앱 등록을 찾을 수 없습니다.');
      return { id: app };
    },
    createUpdate: async (app, input, key, loader, session) => {
      calls.push({ app, input, key, session }); return { id, kind: 'deployments', status: 'preview' };
    },
    startUpdate: async (operation, input, session) => {
      calls.push({ operation, input, session }); return { id: operation, status: input.rebuild ? 'queued' : 'unchanged' };
    },
    sourceFiles: async (operation, variant, session) => {
      assert.equal(operation, id); assert.equal(session, null);
      if (variant === 'deployed') throw new ProductError(409, 'SOURCE_NOT_AVAILABLE', '이 실행에는 최종 소스가 보관되지 않았습니다.');
      return [{ path: 'index.js', content: Buffer.from('original') }];
    },
  };
  const server = createAppServer({ product, service: null });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); }));
  const base = `http://127.0.0.1:${server.address().port}`;
  const preview = '/api/v1/applications/app-example/updates';
  for (const format of ['zip', 'folder', 'github']) {
    const form = new FormData(); form.set('source_type', format);
    if (format === 'zip') form.set('archive', new Blob([await archive()]), 'different-app-name.zip');
    if (format === 'folder') { form.append('files', new Blob(['updated']), 'index.js'); form.set('paths', '["index.js"]'); }
    if (format === 'github') form.set('repository_url', 'https://github.com/example/source');
    const response = await fetch(base + preview, { method: 'POST', body: form, headers: { 'Idempotency-Key': format } });
    assert.equal(response.status, 200, await response.text());
    assert.equal(response.headers.get('location'), `/api/v1/deployments/${id}`);
    const call = calls.at(-1); assert.equal(call.app, 'app-example'); assert.equal(call.input.app, undefined);
    assert.equal(call.input.target_id, undefined); assert.equal(call.input.source_type, format);
    if (format !== 'github') assert.equal(call.input.files[0].content.toString(), 'updated');
  }
  for (const field of ['app', 'target_id', 'provider', 'plan_id', 'environment']) {
    const form = new FormData(); form.set('repository_url', 'https://github.com/example/source'); form.set(field, 'foreign');
    const response = await fetch(base + preview, { method: 'POST', body: form, headers: { 'Idempotency-Key': field } });
    assert.equal(response.status, 422);
  }
  assert.equal(calls.length, 3);
  assert.equal((await fetch(base + preview.replace('app-example', 'foreign'), { method: 'POST' })).status, 404);
  const start = `${base}/api/v1/deployments/${id}/start`;
  for (const body of [{ rebuild: 'true' }, { app: 'other' }, { rebuild: false, files: [] }]) {
    assert.equal((await fetch(start, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).status, 422);
  }
  for (const rebuild of [false, true]) {
    const response = await fetch(start, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rebuild }) });
    assert.equal(response.status, rebuild ? 202 : 200);
    assert.equal(response.headers.get('location'), `/api/v1/deployments/${id}`);
  }
  const response = await fetch(`${base}/api/v1/deployments/${id}/source?variant=submitted`);
  assert.equal(response.status, 200); assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.match(response.headers.get('content-disposition'), /submitted\.zip/);
  const files = await inspectArchive(Buffer.from(await response.arrayBuffer()));
  assert.equal(files[0].content.toString(), 'original');
  assert.equal((await fetch(`${base}/api/v1/deployments/${id}/source?variant=deployed`)).status, 409);
  for (const query of ['variant=other', 'variant=submitted&variant=deployed', 'token=x']) {
    assert.equal((await fetch(`${base}/api/v1/deployments/${id}/source?${query}`)).status, 422);
  }
  assert.equal((await fetch(base + preview)).status, 405);
});
