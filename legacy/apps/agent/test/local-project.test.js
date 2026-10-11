import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { mkdtemp, mkdir, writeFile, rm, symlink, open } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import yazl from 'yazl';
import { connectTools } from '../src/mcp-client.js';
import { localTools } from '../src/local-tools.js';
import { createToolRunner } from '../src/tools.js';
import { createApiClient } from '../src/api.js';
import { inspectArchive, archiveLimits } from '../../api/src/archive.js';
import { uploadedSource } from '../../api/src/http/source.js';

const input = { app: 'demo-app', target_id: 'demo-aws', idempotency_key: 'local-demo-once' };
const entry = fileURLToPath(new URL('../src/mcp-local.js', import.meta.url));
async function workspace(t) {
  const root = await mkdtemp(join(tmpdir(), 'railshot-local-mcp-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  return root;
}
async function zip(entries) {
  const archive = new yazl.ZipFile();
  for (const [path, text] of entries) archive.addBuffer(Buffer.from(text), path);
  archive.end();
  const chunks = [];
  for await (const chunk of archive.outputStream) chunks.push(chunk);
  return Buffer.concat(chunks);
}

test('local MCP uploads via the existing multipart contract, preserves the request key and reuses its session for observation', async t => {
  const root = await workspace(t), project = join(root, 'app'), sessions = join(root, 'session');
  await mkdir(project); await mkdir(join(project, 'node_modules')); await mkdir(join(project, '.git'));
  await writeFile(join(project, 'server.js'), 'private source stays out of model output');
  await writeFile(join(project, 'node_modules', 'unused.js'), 'excluded dependency');
  await writeFile(join(project, '.git', 'config'), 'excluded metadata');
  const received = [], cookie = `railshot_session=${'x'.repeat(43)}`;
  const api = createServer(async (req, res) => {
    if (req.url === '/api/v1/deployments') {
      try {
        received.push({ key: req.headers['idempotency-key'], source: await uploadedSource(req, true, true) });
        res.writeHead(202, { 'content-type': 'application/json', 'set-cookie': `${cookie}; Path=/; HttpOnly` })
          .end(JSON.stringify({ resource_id: 'dep-local', status: 'accepted' }));
      } catch (error) { res.writeHead(422).end(JSON.stringify({ error: { message: error.message } })); }
    } else if (req.url === '/api/v1/deployments/dep-local/insights?minutes=15') {
      assert.equal(req.headers.cookie, cookie);
      res.end(JSON.stringify({ deployment_id: 'dep-local', traffic: { state: 'unsupported', series: [] } }));
    } else res.writeHead(404).end('{}');
  });
  api.listen(0, '127.0.0.1'); await once(api, 'listening');
  t.after(() => { api.closeAllConnections(); api.close(); });
  const env = { ...process.env, RAILSHOT_API_URL: `http://127.0.0.1:${api.address().port}`,
    RAILSHOT_AGENT_SESSION_DIR: sessions, RAILSHOT_API_TOKEN: undefined, RAILSHOT_API_TOKEN_FILE: undefined };
  const client = await connectTools({ path: entry, env }); t.after(() => client.close());
  const tools = await client.list();
  assert.equal(tools.find(tool => tool.name === 'deploy_local_project').annotations.readOnlyHint, false);
  const result = await client.call('deploy_local_project', { ...input, path: project });
  assert.equal(result.resource_id, 'dep-local');
  assert.equal(result.upload.files, 1);
  assert.match(result.upload.sha256, /^[0-9a-f]{64}$/);
  assert.ok(!JSON.stringify(result).includes('private source'));
  assert.equal(received[0].key, input.idempotency_key);
  assert.equal(received[0].source.app, input.app);
  assert.equal(received[0].source.target_id, input.target_id);
  assert.deepEqual(received[0].source.files.map(file => file.path), ['server.js']);
  assert.equal(received[0].source.files[0].content.toString(), 'private source stays out of model output');
  await client.call('deploy_local_project', { ...input, path: project });
  assert.equal(received[1].key, received[0].key, 'caller retry retains the server idempotency key');
  client.close();
  const restarted = await connectTools({ path: entry, env }); t.after(() => restarted.close());
  assert.equal((await restarted.call('get_app_overview', { deployment_id: 'dep-local' })).deployment_id, 'dep-local');
  const remoteCatalog = await connectTools({ env }); t.after(() => remoteCatalog.close());
  assert.ok(!(await remoteCatalog.list()).some(tool => tool.name === 'deploy_local_project'));
  await assert.rejects(remoteCatalog.call('deploy_local_project', { ...input, path: project }));
  assert.equal(received.length, 2, 'remote catalog never reaches local filesystem upload');
});

test('ZIP uploads remove excluded entries before transmission; unsafe local inputs never call the API', async t => {
  const root = await workspace(t), archive = join(root, 'source.zip');
  let calls = 0;
  const run = createToolRunner({ deployArchive: async ({ bytes }) => {
    calls++;
    assert.deepEqual((await inspectArchive(bytes)).map(file => file.path), ['server.js']);
    // Inspect the physical ZIP as well: ignored entries must not travel over the network.
    assert.ok(!bytes.includes(Buffer.from('.git/config')));
    assert.ok(!bytes.includes(Buffer.from('node_modules/ignored.js')));
    return { status: 'accepted' };
  } }, localTools);
  await writeFile(archive, await zip([['server.js', 'ok'], ['.git/config', 'secret'], ['node_modules/ignored.js', 'skip']]));
  await run('deploy_local_project', { ...input, path: archive });
  await writeFile(archive, await zip([['server.js', 'ok'], ['id_rsa', 'private key']]));
  await assert.rejects(run('deploy_local_project', { ...input, path: archive }), /비밀키/);
  const project = join(root, 'app'); await mkdir(project);
  await writeFile(join(project, '.env'), 'SECRET=do-not-upload');
  await assert.rejects(run('deploy_local_project', { ...input, path: project }), /비밀키/);
  await rm(join(project, '.env')); await symlink(archive, join(project, 'external.zip'));
  await assert.rejects(run('deploy_local_project', { ...input, path: project }), /심볼릭 링크/);
  await assert.rejects(run('deploy_local_project', { ...input, path: '../app' }), /절대 경로/);
  const file = await open(archive, 'w'); await file.truncate(archiveLimits.maxBytes + 1); await file.close();
  await assert.rejects(run('deploy_local_project', { ...input, path: archive }), /100 MB/);
  assert.equal(calls, 1);
});

test('a lost upload response is unknown and does not trigger an automatic second deployment', async t => {
  const root = await workspace(t);
  let calls = 0;
  const api = createApiClient({ baseUrl: 'https://example.invalid', env: { RAILSHOT_AGENT_SESSION_DIR: root },
    fetchImpl: async () => { calls++; throw new Error('connection lost'); } });
  await assert.rejects(api.deployArchive({ ...input, name: 'source.zip', bytes: Buffer.from('zip') }),
    error => error.outcomeUnknown === true);
  assert.equal(calls, 1);
});
