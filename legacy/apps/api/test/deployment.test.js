import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import yazl from 'yazl';
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { inspectArchive, documentationOnly, validateFiles } from '../src/archive.js';
import { createDeploymentService } from '../src/github.js';
import { createAppServer } from '../src/server.js';
import { archiveFromPath, deploySource, inferredAppName } from '../src/client.js';
import { fetchPublicGithubSource } from '../src/public-github.js';
import { readPublished } from '../src/published.js';
import { readSourceSnapshot, sourceSnapshotLimit } from '../src/source-snapshot.js';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { readFile } from 'node:fs/promises';

test('documentation detection preserves executable and unknown source formats', () => {
  for (const path of ['index.html', 'package.json', 'Dockerfile', 'main.go', 'app.py', 'web/main.rs', '.railshot/railshot.yaml']) {
    assert.equal(documentationOnly([{ path: 'readme.md' }, { path }]), false, path);
  }
  assert.equal(documentationOnly([{ path: 'readme.md' }, { path: '.editorconfig' }]), true);
});

async function zipOf(files, options = {}) {
  const zip = new yazl.ZipFile();
  for (const [name, content] of Object.entries(files)) zip.addBuffer(Buffer.from(content), name, options[name]);
  zip.end();
  const chunks = [];
  for await (const chunk of zip.outputStream) chunks.push(chunk);
  return Buffer.concat(chunks);
}

test('ZIP 검사 후 앱을 Git 트리에 등록하고 Actions 실행 ID를 반환한다', async () => {
  const archive = await zipOf({ 'my-app/package.json': '{"name":"test"}', 'my-app/server.js': 'hello' });
  const calls = [];
  const fakeFetch = async (url, options = {}) => {
    const path = new URL(url).pathname;
    calls.push({ path, method: options.method || 'GET', body: options.body && JSON.parse(options.body) });
    let data;
    let status = 200;
    if (path.endsWith('/git/ref/heads/main')) data = { object: { sha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' } };
    else if (path.endsWith('/git/commits/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')) data = { tree: { sha: 'base' } };
    else if (path.endsWith('/git/trees/base')) data = { tree: [] };
    else if (path.endsWith('/git/blobs')) data = { sha: `blob-${calls.length}` };
    else if (path.endsWith('/git/trees')) data = { sha: 'tree' };
    else if (path.endsWith('/git/commits')) data = { sha: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' };
    else if (path.endsWith('/git/refs/heads/main')) data = {};
    else if (path.endsWith('/dispatches')) data = { workflow_run_id: 123, html_url: 'https://github.com/example/run/123' };
    else throw new Error(`Unexpected path: ${path}`);
    return new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } });
  };
  const service = createDeploymentService({ token: 'test', owner: 'org', repo: 'apps', targetId: 'aws-demo', targetIds: ['aws-demo', 'stack-aws-1002'] }, fakeFetch);
  const result = await service.deploy({ app: 'my-app', files: await inspectArchive(archive) });
  assert.equal(result.run_id, 123);
  assert.deepEqual(result.changes, { added: 2, updated: 0, deleted: 0, unchanged: 0 });
  const trees = calls.filter((call) => call.path.endsWith('/git/trees') && call.method === 'POST').map((call) => call.body);
  assert.deepEqual(trees[0].tree.map((item) => item.path), ['package.json', 'server.js']);
  assert.equal(trees[1].tree[0].path, 'apps/demo/my-app');
  const dispatch = calls.find((call) => call.path.endsWith('/dispatches')).body;
  assert.deepEqual(dispatch.inputs, { tenant: 'demo', app: 'my-app', source_commit: 'b'.repeat(40), target_id: 'aws-demo' });
  assert.equal(result.source_commit, 'b'.repeat(40));
  assert.equal(result.target_id, 'aws-demo');
  const secondary = await service.deploy({ app: 'my-app', target_id: 'stack-aws-1002', files: await inspectArchive(archive) });
  assert.equal(secondary.target_id, 'stack-aws-1002');
  assert.equal(calls.at(-1).body.inputs.target_id, 'stack-aws-1002');
  const beforeRegistration = calls.length;
  await assert.rejects(service.deploy({ app: 'my-app', target_id: 'request-aws-new', files: await inspectArchive(archive) }), { status: 400 });
  assert.equal(calls.length, beforeRegistration);
  service.allowTarget('request-aws-new');
  const dynamic = await service.deploy({ app: 'my-app', target_id: 'request-aws-new', files: await inspectArchive(archive) });
  assert.equal(dynamic.target_id, 'request-aws-new');
  assert.equal(calls.at(-1).body.inputs.target_id, 'request-aws-new');
});

test('internal target registration validates IDs and exposes a read-only target snapshot', async () => {
  const service = createDeploymentService({ token: 'test', targetId: 'aws-demo' }, async () => assert.fail('Registration must not call GitHub'));
  const before = service.targetIds;
  for (const invalid of [undefined, null, 123, '', '../target', 'UPPER', 'a'.repeat(64)]) assert.throws(() => service.allowTarget(invalid), { status: 400 });
  assert.throws(() => before.push('untrusted-target'), TypeError);
  assert.deepEqual(service.targetIds, ['aws-demo']);
  service.allowTarget('request-aws-new'); service.allowTarget('request-aws-new');
  assert.deepEqual(service.targetIds, ['aws-demo', 'request-aws-new']);
  assert.deepEqual(before, ['aws-demo']);
  await assert.rejects(service.status('123', 'untrusted-target'), { status: 400 });
});

test('재배포는 변경된 blob만 올리고 삭제된 파일은 앱 트리에서 제외한다', async () => {
  const sha = (value) => createHash('sha1').update(`blob ${Buffer.byteLength(value)}\0${value}`).digest('hex');
  const calls = [];
  const oldFiles = [
    { path: 'same.js', mode: '100644', type: 'blob', sha: sha('same') },
    { path: 'changed.js', mode: '100644', type: 'blob', sha: sha('old') },
    { path: 'removed.js', mode: '100644', type: 'blob', sha: sha('removed') },
  ];
  const fakeFetch = async (url, options = {}) => {
    const path = new URL(url).pathname;
    const method = options.method || 'GET';
    calls.push({ path, method, body: options.body && JSON.parse(options.body) });
    let data;
    if (path.endsWith('/git/ref/heads/main') && method === 'GET') data = { object: { sha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' } };
    else if (path.endsWith('/git/commits/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')) data = { tree: { sha: 'base' } };
    else if (path.endsWith('/git/trees/base')) data = { tree: [{ path: 'apps', type: 'tree', sha: 'apps-tree' }] };
    else if (path.endsWith('/git/trees/apps-tree')) data = { tree: [{ path: 'demo', type: 'tree', sha: 'tenant-tree' }] };
    else if (path.endsWith('/git/trees/tenant-tree')) data = { tree: [{ path: 'my-app', type: 'tree', sha: 'app-tree' }] };
    else if (path.endsWith('/git/trees/app-tree')) data = { tree: oldFiles, truncated: false };
    else if (path.endsWith('/git/blobs')) data = { sha: 'new-blob' };
    else if (path.endsWith('/git/trees') && method === 'POST') data = { sha: 'new-tree' };
    else if (path.endsWith('/git/commits') && method === 'POST') data = { sha: 'cccccccccccccccccccccccccccccccccccccccc' };
    else if (path.endsWith('/git/refs/heads/main') && method === 'PATCH') data = {};
    else if (path.endsWith('/dispatches')) data = { workflow_run_id: 456 };
    else throw new Error(`Unexpected path: ${path}`);
    return Response.json(data);
  };
  const service = createDeploymentService({ token: 'test', owner: 'org', repo: 'apps', targetId: 'aws-demo' }, fakeFetch);
  const result = await service.deploy({ app: 'my-app', files: [
    { path: 'same.js', content: Buffer.from('same') },
    { path: 'changed.js', content: Buffer.from('new') },
    { path: 'added.js', content: Buffer.from('added') },
  ] });
  assert.deepEqual(result.changes, { added: 1, updated: 1, deleted: 1, unchanged: 1 });
  assert.equal(calls.filter((call) => call.path.endsWith('/git/blobs')).length, 2);
  const appTree = calls.find((call) => call.path.endsWith('/git/trees') && call.method === 'POST').body;
  assert.deepEqual(appTree.tree.map((item) => item.path), ['same.js', 'changed.js', 'added.js']);
  assert.equal(appTree.tree[0].sha, oldFiles[0].sha);
  assert.equal(calls.at(-1).path.endsWith('/dispatches'), true);
});

test('소스가 같아도 커밋 없이 Actions를 다시 실행한다', async () => {
  const content = Buffer.from('same');
  const sha = createHash('sha1').update(`blob ${content.length}\0`).update(content).digest('hex');
  const calls = [];
  const fakeFetch = async (url, options = {}) => {
    const path = new URL(url).pathname;
    calls.push({ path, method: options.method || 'GET' });
    let data;
    if (path.endsWith('/git/ref/heads/main')) data = { object: { sha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' } };
    else if (path.endsWith('/git/commits/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')) data = { tree: { sha: 'base' } };
    else if (path.endsWith('/git/trees/base')) data = { tree: [{ path: 'apps', type: 'tree', sha: 'apps-tree' }] };
    else if (path.endsWith('/git/trees/apps-tree')) data = { tree: [{ path: 'demo', type: 'tree', sha: 'tenant-tree' }] };
    else if (path.endsWith('/git/trees/tenant-tree')) data = { tree: [{ path: 'my-app', type: 'tree', sha: 'app-tree' }] };
    else if (path.endsWith('/git/trees/app-tree')) data = { tree: [{ path: 'same.js', mode: '100644', type: 'blob', sha }], truncated: false };
    else if (path.endsWith('/dispatches')) data = { workflow_run_id: 789 };
    else throw new Error(`Unexpected path: ${path}`);
    return Response.json(data);
  };
  const service = createDeploymentService({ token: 'test', owner: 'org', repo: 'apps', targetId: 'aws-demo' }, fakeFetch);
  const result = await service.deploy({ app: 'my-app', files: [{ path: 'same.js', content }] });
  assert.equal(result.source_commit, 'a'.repeat(40));
  assert.deepEqual(result.changes, { added: 0, updated: 0, deleted: 0, unchanged: 1 });
  assert.deepEqual(calls.filter((call) => call.method !== 'GET').map((call) => call.path.split('/').at(-1)), ['dispatches']);
});

test('ZIP 경로 이동과 비밀키 파일을 거부한다', async () => {
  await assert.rejects(inspectArchive(await zipOf({ 'app/.env': 'secret' })), /비밀키/);
  const unsafe = await zipOf({ 'aaa/outside': 'bad' });
  const text = unsafe.toString('latin1').replaceAll('aaa/outside', '../.outside');
  await assert.rejects(inspectArchive(Buffer.from(text, 'latin1')), /invalid relative path|안전하지 않은/);
});

test('source credentials are rejected before any GitHub call without echoing their values', async () => {
  let calls = 0;
  const service = createDeploymentService({ token: 'test', targetId: 'aws-demo' }, async () => { calls++; throw new Error('Unexpected GitHub request'); });
  const secrets = ['ghp_' + 'a'.repeat(36), 'github_pat_' + 'a'.repeat(40), 'sk-' + 'a'.repeat(32),
    'apikey_' + 'a'.repeat(24), 'AKIA' + 'A'.repeat(16), 'AIza' + 'a'.repeat(35),
    'xoxb-' + '1'.repeat(24), '-----BEGIN ' + 'PRIVATE KEY-----'];
  for (const secret of secrets) {
    const content = Buffer.from(`export const credential = "${secret}";`);
    const files = [{ path: 'index.html', content: Buffer.from('safe') }, { path: 'config.js', content }];
    const rejected = (error) => /비밀키/.test(error.message) && !error.message.includes(secret);
    assert.throws(() => validateFiles(files), rejected);
    await assert.rejects(inspectArchive(await zipOf({ 'config.js': content })), rejected);
    await assert.rejects(service.deploy({ app: 'safe-app', files }), rejected);
  }
  assert.equal(calls, 0);
  for (const path of ['production.env', 'config/production.env.local', '.aws/credentials', '.kube/config', '.codex/auth.json', '.npmrc']) {
    assert.throws(() => validateFiles([{ path, content: Buffer.from('private') }]), /비밀키/);
    await assert.rejects(inspectArchive(await zipOf({ [path]: 'private' })), /비밀키/);
  }
  const safe = [{ path: 'env.js', content: Buffer.from('https://example.org/news/with-sk-telecom-and-arm-sovereign-ai') }];
  assert.deepEqual(validateFiles(safe), safe, 'A public article slug is not an API key');
});

test('ignored dependency symlinks are skipped while application symlinks remain rejected', async () => {
  const link = 'repo/examples/react/node_modules/.bin/JSONStream';
  const files = { 'repo/package.json': '{}', 'repo/index.html': 'app', [link]: '../JSONStream/bin.js' };
  const bytes = await zipOf(files, { [link]: { mode: 0o120777 } });
  assert.deepEqual((await inspectArchive(bytes, { stripRoot: true })).map((file) => file.path), ['package.json', 'index.html']);
  await assert.rejects(inspectArchive(await zipOf({ 'repo/app-link': '/etc/passwd' },
    { 'repo/app-link': { mode: 0o120777 } })), /안전하지 않은 ZIP 경로/);
});

test('공개 GitHub 저장소의 기본 브랜치를 SHA로 고정하고 공통 파일 목록으로 변환한다', async () => {
  const sha = 'a'.repeat(40);
  let zip = await zipOf({ 'sample-a1b2c3/requirements.txt': 'flask', 'sample-a1b2c3/app.py': 'print(1)' });
  const calls = [];
  const fakeFetch = async (url, options) => {
    calls.push({ url: String(url), options });
    if (url === 'https://api.github.com/repos/example/sample') return Response.json({ private: false, visibility: 'public', default_branch: 'main' });
    if (url === 'https://api.github.com/repos/example/sample/commits/main') return Response.json({ sha });
    if (url === `https://api.github.com/repos/example/sample/zipball/${sha}`) {
      return new Response(null, { status: 302, headers: { location: `https://codeload.github.com/example/sample/legacy.zip/${sha}` } });
    }
    if (url.href === `https://codeload.github.com/example/sample/legacy.zip/${sha}`) return new Response(zip);
    throw new Error(`Unexpected URL: ${url}`);
  };
  const result = await fetchPublicGithubSource('https://github.com/example/sample.git', fakeFetch);
  assert.deepEqual(result.files.map((file) => file.path), ['requirements.txt', 'app.py']);
  assert.deepEqual(result.source, { type: 'github', repository: 'https://github.com/example/sample', sha });
  assert.ok(calls.every((call) => !call.options.headers.authorization));
  assert.ok(calls.every((call) => call.options.signal instanceof AbortSignal), 'Every source read has a deadline');
  zip = await zipOf({ 'sample-a1b2c3/.env': 'private' });
  await assert.rejects(fetchPublicGithubSource('https://github.com/example/sample', fakeFetch),
    (error) => error.status === 422 && /GitHub 소스 검사 실패:.*비밀키/.test(error.message));
});

test('GitHub 입력은 공개 저장소 기본 URL로만 제한한다', async () => {
  await assert.rejects(fetchPublicGithubSource('http://github.com/example/sample'), /공개 저장소 URL/);
  await assert.rejects(fetchPublicGithubSource('https://github.com/example/sample/tree/main'), /기본 URL/);
  await assert.rejects(fetchPublicGithubSource('https://github.com/example/sample?token=x'), /공개 저장소 URL/);
  await assert.rejects(fetchPublicGithubSource('https://github.com/example/sample', async () => Response.json({ private: true, default_branch: 'main' })), /공개 저장소만/);
});

test('소스 이름에서 앱 이름을 만들고 잘못된 이름은 거부한다', () => {
  assert.equal(inferredAppName('/tmp/My App.zip'), 'my-app');
  assert.equal(inferredAppName('https://github.com/example/Web.App.git'), 'web-app');
  assert.throws(() => inferredAppName('/tmp/앱.zip'), /앱 이름/);
});

test('HTTP 업로드, GitHub URL과 상태 조회는 동일한 서비스를 사용한다', async () => {
  const observed = [];
  const server = createAppServer({ sourceLoader: async (url) => ({
    files: [{ path: 'app.py', content: Buffer.from('print(1)') }],
    source: { type: 'github', repository: url, sha: 'b'.repeat(40) },
  }), service: {}, product: {
    createBuild: async (input, loadSource) => {
      observed.push(input.files ? input : { ...input, ...await loadSource(input.repository_url) });
      return { run_id: 456, app: input.app, tenant: 'demo' };
    },
    legacyStatus: async (id) => ({ run_id: Number(id), status: 'queued', steps: [] }),
  } });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const base = `http://127.0.0.1:${server.address().port}`;
    const index = await fetch(base);
    assert.equal(index.status, 200);
    assert.match(await index.text(), /id="deploy-form"/);
    const script = await fetch(`${base}/app.js`);
    assert.equal(script.status, 200);
    assert.match(await script.text(), /#review-panel/);
    const form = new FormData();
    form.set('app', 'my-app');
    form.set('archive', new Blob([await zipOf({ 'index.js': 'test' })]), 'app.zip');
    const create = await fetch(`${base}/api/deploy`, { method: 'POST', headers: { 'x-railshot-request': 'deploy' }, body: form });
    assert.equal(create.status, 202);
    assert.equal((await create.json()).run_id, 456);
    assert.equal(observed[0].app, 'my-app');
    assert.deepEqual(observed[0].files.map((file) => file.path), ['index.js']);
    const folder = new FormData();
    folder.set('app', 'folder-app');
    folder.append('files', new Blob(['hello']), 'index.js');
    folder.set('paths', JSON.stringify(['src/index.js']));
    const folderCreate = await fetch(`${base}/api/deploy`, { method: 'POST', headers: { 'x-jasmin-request': 'deploy' }, body: folder });
    assert.equal(folderCreate.status, 202);
    assert.deepEqual(observed[1].files.map((file) => file.path), ['src/index.js']);
    folder.set('paths', JSON.stringify(['../outside.js']));
    const unsafeFolder = await fetch(`${base}/api/deploy`, { method: 'POST', headers: { 'x-jasmin-request': 'deploy' }, body: folder });
    assert.equal(unsafeFolder.status, 400);
    assert.equal(observed.length, 2);
    const github = new FormData();
    github.set('app', 'github-app');
    github.set('repository_url', 'https://github.com/example/sample');
    const githubCreate = await fetch(`${base}/api/deploy`, { method: 'POST', headers: { 'x-jasmin-request': 'deploy' }, body: github });
    assert.equal(githubCreate.status, 202);
    assert.equal(observed[2].files[0].path, 'app.py');
    assert.equal(observed[2].source.sha, 'b'.repeat(40));
    assert.equal((await deploySource({ source: 'https://github.com/example/sample', baseUrl: base })).app, 'sample');
    assert.equal(observed[3].app, 'sample');
    const localFolder = await mkdtemp(join(tmpdir(), 'jasmin-local-'));
    try {
      await writeFile(join(localFolder, 'index.js'), 'console.log(1)');
      assert.equal((await deploySource({ source: localFolder, baseUrl: base })).app, inferredAppName(localFolder));
      assert.deepEqual(observed[4].files.map((file) => file.path), ['index.js']);
    } finally { await rm(localFolder, { recursive: true, force: true }); }
    github.set('archive', new Blob([await zipOf({ 'index.js': 'test' })]), 'app.zip');
    const multiple = await fetch(`${base}/api/deploy`, { method: 'POST', headers: { 'x-jasmin-request': 'deploy' }, body: github });
    assert.equal(multiple.status, 400);
    assert.equal(observed.length, 5);
    const status = await fetch(`${base}/api/runs/456`);
    assert.equal((await status.json()).status, 'queued');
    const blocked = await fetch(`${base}/api/deploy`, { method: 'POST', body: form });
    assert.equal(blocked.status, 403);
  } finally { server.close(); }
});

function publishedFiles({ specName = 'railshot.yaml', attempt = 1, sourceCommit = 'a'.repeat(40), targetId = 'aws-demo', app = 'my-app', gateResult, sourceSha256 = 'c'.repeat(64), layers = ['L0', 'L1', 'Q', 'L2', 'L4', 'L3'] } = {}) {
  const hash = (value) => createHash('sha256').update(value).digest('hex');
  const verdict = { ok: true, release_eligible: true, status: 'PASS', source_sha256: sourceSha256,
    layers: layers.map((layer) => ({ layer, ok: true, errors: [] })),
    images: { web: 'local/web:gate' }, image_ids: { web: 'sha256:' + 'd'.repeat(64) } };
  if (gateResult) Object.assign(verdict.layers.find((row) => row.layer === gateResult.layer), gateResult);
  const files = { [specName]: 'app: my-app\n', 'verdict.json': JSON.stringify(verdict),
    'images.json': JSON.stringify({ web: 'ghcr.io/org/demo-my-app-web@sha256:' + 'e'.repeat(64) }) };
  files['manifest.json'] = JSON.stringify({ version: 1, trust: 'trusted-ci-artifact-not-a-signature',
    source_sha256: verdict.source_sha256, images: { web: { local_ref: verdict.images.web, id: verdict.image_ids.web } },
    files: { [specName]: hash(files[specName]), 'verdict.json': hash(files['verdict.json']), 'images.tar': 'f'.repeat(64) } });
  files['handoff.json'] = JSON.stringify({ version: 2, status: 'published', run_id: 789, producer_attempt: attempt,
    source_commit: sourceCommit, target_id: targetId, tenant: 'demo', app, bundle_artifact_id: 100,
    registry: { visibility: 'public', verification: 'anonymous_manifest_read',
      images_sha256: hash(files['images.json']), image_pull_secret: null },
    files: Object.fromEntries(Object.entries(files).map(([name, value]) => [name, hash(value)])) });
  return files;
}

test('canonical and historical publications retain exact filename/hash bindings; ambiguity is rejected', async () => {
  const identity = { runId: 789, attempt: 1, headSha: 'a'.repeat(40), targetId: 'aws-demo', tenant: 'demo' };
  const entries = (files) => Object.entries(files).map(([path, content]) => ({ path, content: Buffer.from(content) }));
  for (const specName of ['railshot.yaml', 'jasmin.yaml']) {
    const files = publishedFiles({ specName });
    assert.equal(readPublished(entries(files), identity).status, 'published');
    const { service } = await publicationService({ files });
    assert.equal((await service.status('789')).state, 'published');
    const other = specName === 'railshot.yaml' ? 'jasmin.yaml' : 'railshot.yaml';
    assert.throws(() => readPublished(entries({ ...files, [other]: files[specName] }), identity));
    const renamed = { ...files, [other]: files[specName] }; delete renamed[specName];
    assert.throws(() => readPublished(entries(renamed), identity));
  }
});

test('publication accepts complete minimal and historical gates but rejects partial or failed image/runtime verification', () => {
  const identity = { runId: 789, attempt: 1, headSha: 'a'.repeat(40), targetId: 'aws-demo', tenant: 'demo' };
  const read = (layers, gateResult) => readPublished(Object.entries(publishedFiles({ layers, gateResult }))
    .map(([path, content]) => ({ path, content: Buffer.from(content) })), identity);
  for (const layers of [['L0', 'L1', 'L2', 'L3'], ['L0', 'L1', 'L2', 'L4', 'L3'], ['L0', 'L1', 'Q', 'L2', 'L4', 'L3']]) {
    assert.equal(read(layers).status, 'published');
    for (const layer of ['L0', 'L1', 'L2', 'L3']) assert.throws(() => read(layers, { layer, ok: false }), /gate 단계/);
  }
  for (const layers of [['L0', 'L1', 'L2'], ['L0', 'L1', 'L3'], ['L0', 'L1', 'L3', 'L2']])
    assert.throws(() => read(layers), /gate 단계/);
});

test('publication accepts quality advisories but rejects runtime, isolation and unknown failures', () => {
  const identity = { runId: 789, attempt: 1, headSha: 'a'.repeat(40), targetId: 'aws-demo', tenant: 'demo' };
  const read = (gateResult) => readPublished(Object.entries(publishedFiles({ gateResult }))
    .map(([path, content]) => ({ path, content: Buffer.from(content) })), identity);
  const q = { layer: 'Q', ok: false, advisory: true, outcome: 'BLOCKED', blocked: 'NO_TESTS',
    error: { code: 'GATE_CONFIG_INVALID', phase: 'Q.discovery', outcome: 'BLOCKED' } };
  assert.equal(read(q).status, 'published');
  assert.equal(read({ ...q, outcome: 'FAIL', error: { code: 'GATE_CHECK_FAILED', phase: 'Q.unit', outcome: 'FAIL' } }).status, 'published');
  for (const layer of ['L0', 'L1', 'L2', 'L4', 'L3']) assert.throws(() => read({ ...q, layer }), /gate 단계/);
  for (const [phase, code, outcome] of [
    ['Q.snapshot', 'GATE_CONFIG_INVALID', 'BLOCKED'], ['network', 'GATE_ENVIRONMENT_UNAVAILABLE', 'BLOCKED'],
    ['Q.cleanup', 'GATE_EXECUTION_FAILED', 'UNKNOWN'], ['Q.evidence-write', 'OBSERVATION_WRITE_FAILED', 'UNKNOWN'],
  ]) assert.throws(() => read({ ...q, outcome, error: { phase, code, outcome } }), /gate 단계/);
  assert.throws(() => read({ ...q, advisory: false }), /gate 단계/);
});

test('source ZIP root detection supports either specification name', async () => {
  for (const spec of ['.railshot/railshot.yaml', '.jasmin/jasmin.yaml']) {
    const files = await inspectArchive(await zipOf({ [`project/${spec}`]: 'app: demo\n', 'project/server.py': 'pass\n' }));
    assert.deepEqual(files.map((file) => file.path).sort(), [spec, 'server.py']);
  }
});

async function publicationService({ attempt = 1, producer = attempt, files = publishedFiles({ attempt: producer }),
  expired = false, duplicate = false, release = 'success', headSha = 'a'.repeat(40), artifacts = true, jobRows } = {}) {
  const zip = await zipOf(files);
  const calls = [];
  const fetchImpl = async (url, options = {}) => {
    calls.push(String(url));
    const value = new URL(url); const path = value.pathname;
    if (path.endsWith('/actions/runs/789')) return Response.json({ run_attempt: attempt, head_sha: headSha,
      status: 'completed', conclusion: 'success', path: '.github/workflows/railshot-deploy.yml',
      html_url: 'https://github.com/org/apps/actions/runs/789' });
    const jobs = /\/attempts\/(\d+)\/jobs$/.exec(path);
    if (jobs) return Response.json({ jobs: Number(jobs[1]) === producer ? (jobRows || [
      { name: 'loop', status: 'completed', conclusion: 'success' },
      { name: 'release', status: 'completed', conclusion: release },
    ]) : [] });
    if (path.endsWith('/runs/789/artifacts')) {
      assert.equal(value.searchParams.get('name'), `published-${producer}`);
      const item = { id: 200, name: `published-${producer}`, expired, size_in_bytes: zip.length,
        workflow_run: { id: 789, head_sha: headSha }, archive_download_url: 'https://untrusted.invalid/do-not-follow' };
      return Response.json({ artifacts: artifacts ? (duplicate ? [item, { ...item, id: 201 }] : [item]) : [] });
    }
    if (path.endsWith('/actions/artifacts/200/zip')) {
      assert.equal(options.headers.authorization, 'Bearer test');
      return new Response(zip);
    }
    throw new Error(`Unexpected URL: ${url}`);
  };
  return { service: createDeploymentService({ token: 'test', owner: 'org', repo: 'apps', targetId: 'aws-demo' }, fetchImpl), calls, fetchImpl };
}

test('실제 producer artifact ID와 해시를 확인한 경우에만 이미지 게시로 표시한다', async () => {
  const { service, calls } = await publicationService();
  const result = await service.status('789');
  assert.equal(result.state, 'published');
  assert.equal(result.url, null);
  assert.equal(result.publication.artifact_id, 200);
  assert.equal(result.publication.producer_attempt, 1);
  assert.equal(result.publication.version, 2);
  const trustedFiles = await service.publishedFiles(result.publication);
  assert.equal(trustedFiles.length, 5);
  await assert.rejects(service.publishedFiles({ ...result.publication, artifact_id: 201 }), /게시 참조/);
  assert.equal(result.publication.registry.verification, 'anonymous_manifest_read');
  assert.equal(result.publication.registry.image_pull_secret, null);
  assert.equal(result.target_id, 'aws-demo');
  assert.deepEqual(result.steps.map((step) => step.key), ['loop', 'release']);
  assert.ok(calls.some((url) => url.endsWith('/artifacts/200/zip')));
  assert.ok(calls.every((url) => url.startsWith('https://api.github.com/')));
});

test('private registry 검증과 pull Secret 참조가 있는 v2 인계를 읽는다', async () => {
  const files = publishedFiles();
  const handoff = JSON.parse(files['handoff.json']);
  handoff.registry = { ...handoff.registry, visibility: 'private', verification: 'authenticated_manifest_read',
    image_pull_secret: { namespace: 'demo', name: 'ghcr-pull' } };
  files['handoff.json'] = JSON.stringify(handoff);
  const { service } = await publicationService({ files });
  const result = await service.status('789');
  assert.equal(result.state, 'published');
  assert.equal(result.url, null);
  assert.deepEqual(result.publication.registry, handoff.registry);
});

test('구버전·registry 계약 불일치·추가 credential 필드는 게시 확인을 차단한다', async () => {
  const mutations = [
    (h) => { h.version = 1; },
    (h) => { h.token = 'must-not-return'; },
    (h) => { delete h.registry; },
    (h) => { h.registry = null; },
    (h) => { h.registry = []; },
    (h) => { h.registry.visibility = 'internal'; },
    (h) => { h.registry.verification = 'anonymous_manifest_read'; },
    (h) => { h.registry.images_sha256 = '0'.repeat(64); },
    (h) => { delete h.registry.images_sha256; },
    (h) => { h.registry.auth = 'must-not-return'; },
    (h) => { h.registry.image_pull_secret = null; },
    (h) => { h.registry.image_pull_secret = []; },
    (h) => { delete h.registry.image_pull_secret.name; },
    (h) => { h.registry.image_pull_secret.token = 'must-not-return'; },
    (h) => { h.registry.visibility = 'public'; },
    (h) => { h.registry.visibility = 'public'; h.registry.verification = 'anonymous_manifest_read'; },
    ...['', 'Bad', '-bad', 'bad-', 'a'.repeat(64), 'bad\n', 123].flatMap((value) =>
      ['namespace', 'name'].map((key) => (h) => { h.registry.image_pull_secret[key] = value; })),
  ];
  for (const [index, mutate] of mutations.entries()) {
    const files = publishedFiles();
    const handoff = JSON.parse(files['handoff.json']);
    handoff.registry = { ...handoff.registry, visibility: 'private', verification: 'authenticated_manifest_read',
      image_pull_secret: { namespace: 'demo', name: 'ghcr-pull' } };
    mutate(handoff);
    files['handoff.json'] = JSON.stringify(handoff);
    const { service } = await publicationService({ files });
    const result = await service.status('789');
    assert.equal(result.state, 'publication_unverified', `mutation ${index}`);
    assert.equal(result.publication, null);
    assert.equal(result.url, null);
    assert.ok(!JSON.stringify(result).includes('must-not-return'));
  }
});

test('실패 job 재시도는 현재 attempt 별칭 대신 실제 이전 release producer를 읽는다', async () => {
  const { service, calls } = await publicationService({ attempt: 2, producer: 1 });
  const result = await service.status('789');
  assert.equal(result.state, 'published');
  assert.equal(result.publication.artifact_name, 'published-1');
  assert.ok(calls.some((url) => url.includes('/attempts/2/jobs')));
  assert.ok(calls.some((url) => url.includes('/attempts/1/jobs')));
});

test('재시도에 복제된 loop job의 조회 attempt를 artifact 생산 attempt로 표시하지 않는다', async () => {
  const { service, calls } = await publicationService({ attempt: 2, jobRows: [
    { id: 110657805259, name: 'release', run_attempt: 2, status: 'completed', conclusion: 'success',
      started_at: '2026-10-02T01:03:50Z', completed_at: '2026-10-02T01:04:19Z' },
    // GitHub assigned a new ID/attempt but retained the original attempt 1 execution times.
    { id: 110657806684, name: 'loop', run_attempt: 2, status: 'completed', conclusion: 'success',
      started_at: '2026-10-02T00:51:21Z', completed_at: '2026-10-02T00:52:43Z' },
  ] });
  const result = await service.status('789');
  assert.equal(result.state, 'published');
  assert.deepEqual(result.steps.map(({ key, observed_attempt }) => ({ key, observed_attempt })),
    [{ key: 'loop', observed_attempt: 2 }, { key: 'release', observed_attempt: 2 }]);
  assert.ok(result.steps.every((step) => !Object.hasOwn(step, 'producer_attempt')));
  assert.equal(result.publication.producer_attempt, 2);
  assert.equal(result.publication.artifact_name, 'published-2');
  assert.equal(result.publication.bundle_artifact_id, 100);
  assert.ok(calls.every((url) => !url.includes('/attempts/1/jobs')));
});

test('최신 release가 실패하거나 건너뛰면 옛 성공 artifact를 사용하지 않는다', async () => {
  for (const release of ['failure', 'skipped']) {
    const { service, calls } = await publicationService({ attempt: 2, release });
    const result = await service.status('789');
    assert.equal(result.state, 'failed');
    assert.equal(result.conclusion, 'failure');
    assert.equal(result.publication, null);
    assert.ok(calls.every((url) => !url.includes('/artifacts')));
  }
});

async function diagnosticService({ artifact = {}, evidence = {}, files, duplicate = false, body, attempt = 1 } = {}) {
  const data = { passed: false, result: 'blocked: NO_TESTS', status: 'BLOCKED', agent_attempts: 1, repair_scope: 'packaging',
    error: { code: 'GATE_CONFIG_INVALID', phase: 'Q.discovery', outcome: 'BLOCKED', summary: 'private-secret-value' },
    attempts: [{ attempt: 0, agent_invoked: false, written: null },
      { attempt: 1, agent_invoked: true, written: ['Dockerfile', '.dockerignore', '.railshot/railshot.yaml', '.env', '../private-secret-value'] }],
    ...evidence };
  const zip = await zipOf(files || { 'evidence.json': JSON.stringify(data) }), calls = [];
  const fetchImpl = async (url) => {
    calls.push(String(url));
    const value = new URL(url), path = value.pathname;
    if (path.endsWith('/actions/runs/789')) return Response.json({ run_attempt: attempt, head_sha: 'a'.repeat(40),
      status: 'completed', conclusion: 'failure', path: '.github/workflows/railshot-deploy.yml',
      html_url: 'https://github.com/org/apps/actions/runs/789' });
    if (path.endsWith(`/attempts/${attempt}/jobs`)) return Response.json({ total_count: 2, jobs: [
      { name: 'loop', status: 'completed', conclusion: 'failure', steps: [
        { number: 1, name: 'Validate inputs', status: 'completed', conclusion: 'success' },
        { number: 2, name: 'Baseline gates, then SDK repair only when eligible', status: 'completed', conclusion: 'failure' },
        { number: 3, name: 'private-secret-value', status: 'private-secret-value', conclusion: 'private-secret-value' }, null,
      ] }, { name: 'release', status: 'completed', conclusion: 'skipped' },
    ] });
    if (path.endsWith('/runs/789/artifacts')) {
      assert.equal(value.searchParams.get('name'), `loop-${attempt}`);
      const row = { id: 201, name: `loop-${attempt}`, expired: false, size_in_bytes: zip.length,
        workflow_run: { id: 789, head_sha: 'a'.repeat(40) }, archive_download_url: 'https://untrusted.invalid/private-secret-value', ...artifact };
      return Response.json({ total_count: duplicate ? 2 : 1, artifacts: duplicate ? [row, { ...row, id: 202 }] : [row] });
    }
    if (path.endsWith('/actions/artifacts/201/zip')) return new Response(body || zip);
    throw new Error('private-secret-value');
  };
  return { service: createDeploymentService({ token: 'test', owner: 'org', repo: 'apps', targetId: 'aws-demo' }, fetchImpl), calls };
}

test('failed CI exposes current-attempt NO_TESTS diagnostics and safe job tasks without raw artifact text', async () => {
  const { service, calls } = await diagnosticService();
  const result = await service.status('789');
  assert.equal(result.state, 'failed'); assert.equal(result.publication, null);
  assert.deepEqual(result.diagnostics, { state: 'ready', run_attempt: 1, artifact_id: 201, reason: 'NO_TESTS',
    code: 'GATE_CONFIG_INVALID', phase: 'Q.discovery', outcome: 'BLOCKED', agent_attempts: 1,
    changed_files: ['Dockerfile', '.dockerignore', '.railshot/railshot.yaml'], changed_file_count: 3, repair_scope: 'packaging',
    message: '실행할 테스트를 찾지 못해 품질 검사가 중단되었습니다.',
    guidance: '원본 프로젝트에 실행 가능한 테스트와 테스트 명령을 추가한 뒤 다시 배포하세요. 자동 패키징 수정은 테스트를 만들거나 품질 검사를 생략하지 않습니다.' });
  assert.deepEqual(result.steps[0].tasks, [
    { number: 1, name: 'Validate inputs', status: 'completed', conclusion: 'success' },
    { number: 2, name: 'Baseline gates, then SDK repair only when eligible', status: 'completed', conclusion: 'failure' },
    { number: 3, name: '단계 3', status: 'unknown', conclusion: null },
  ]);
  assert.ok(!JSON.stringify(result).includes('private-secret-value'));
  assert.ok(calls.every((url) => url.startsWith('https://api.github.com/')));
  const source = await diagnosticService({ evidence: { repair_scope: 'source', attempts: [
    { attempt: 0, agent_invoked: false, written: null },
    { attempt: 1, agent_invoked: true, written: ['Dockerfile', 'tests/private-secret-value.test.js', 'src/private-secret-value.js',
      'tests/private-secret-value.test.js', '../private-secret-value', '.env', '/private-secret-value'] },
  ] } });
  const repaired = (await source.service.status('789')).diagnostics;
  assert.equal(repaired.repair_scope, 'source'); assert.equal(repaired.changed_file_count, 3);
  assert.deepEqual(repaired.changed_files, ['Dockerfile']);
  assert.match(repaired.guidance, /소스·테스트 자동 수정을 시도/);
  assert.ok(!JSON.stringify(repaired).includes('private-secret-value'));
  assert.ok(!repaired.guidance.includes('테스트를 만들거나'));
});

test('stale, mismatched, oversized and malicious CI diagnostics fail closed without hiding the failed run', async () => {
  for (const options of [
    { attempt: 2, artifact: { name: 'loop-1' } },
    { artifact: { expired: true } }, { duplicate: true },
    { artifact: { workflow_run: { id: 788, head_sha: 'a'.repeat(40) } } },
    { artifact: { workflow_run: { id: 789, head_sha: 'b'.repeat(40) } } },
    { artifact: { size_in_bytes: 3 * 1024 * 1024 } }, { body: Buffer.alloc(2 * 1024 * 1024 + 1) },
    { files: { 'evidence.json': '{private-secret-value' } },
    { files: { 'other/evidence.json': '{}' } }, { evidence: { passed: true } },
    { evidence: { agent_attempts: 9 } }, { evidence: { status: 'FAIL' } },
  ]) {
    const { service, calls } = await diagnosticService(options);
    const result = await service.status('789');
    assert.equal(result.state, 'failed'); assert.equal(result.publication, null);
    assert.equal(result.diagnostics.state, 'unavailable', JSON.stringify(options));
    assert.ok(!JSON.stringify(result).includes('private-secret-value'));
    if (options.attempt === 2) assert.ok(calls.every((url) => !url.includes('loop-1') && !url.includes('/artifacts/201/zip')));
  }
  const { service } = await diagnosticService({ evidence: { result: 'private-secret-value', repair_scope: 'private-secret-value',
    error: { code: 'private-secret-value', phase: '/private-secret-value', outcome: 'BLOCKED' } } });
  const result = await service.status('789');
  assert.equal(result.diagnostics.state, 'ready');
  assert.equal(result.diagnostics.reason, 'CI_FAILED'); assert.equal(result.diagnostics.code, 'CI_FAILED');
  assert.equal(result.diagnostics.phase, null);
  assert.equal(result.diagnostics.repair_scope, null);
  assert.ok(!JSON.stringify(result).includes('private-secret-value'));
});

test('artifact 만료·중복·누락과 source/target/attempt/파일 변조는 게시 확인을 차단한다', async () => {
  const tampered = publishedFiles(); tampered['images.json'] = '{}';
  for (const options of [
    { expired: true }, { duplicate: true }, { artifacts: false }, { files: tampered },
    { files: publishedFiles({ sourceCommit: 'b'.repeat(40) }) },
    { files: publishedFiles({ targetId: 'onprem-demo' }) },
    { files: publishedFiles({ attempt: 2 }) },
    { files: { ...publishedFiles(), 'handoff.json': '{"private-secret-value": INVALID}' } },
    { files: { ...publishedFiles(), 'private-secret-value.pem': 'private-secret-value' } },
  ]) {
    const { service } = await publicationService(options);
    const result = await service.status('789');
    assert.equal(result.state, 'publication_unverified', JSON.stringify(options));
    assert.equal(result.publication, null);
    assert.equal(result.url, null);
    assert.ok(!JSON.stringify(result).includes('private-secret-value'));
    assert.equal(result.message, '게시 산출물의 식별자·무결성·계약을 확인하지 못했습니다. GitHub Actions 기록을 확인하세요.');
  }
});

test('CI와 다른 앱 이름과 임의 target은 소스 등록 전에 거부한다', async () => {
  let calls = 0;
  const service = createDeploymentService({ token: 'test', targetId: 'aws-demo' }, async () => { calls++; throw new Error('must not call'); });
  for (const app of [undefined, null, 'a', 'ab', '-app', '1app', 'app-', 'a'.repeat(31)]) {
    await assert.rejects(service.deploy({ app, files: [{ path: 'app.js', content: Buffer.from('x') }] }), /앱 이름/);
  }
  await assert.rejects(service.deploy({ app: 'my-app', target_id: 'other', files: [{ path: 'app.js', content: Buffer.from('x') }] }), /대상/);
  assert.equal(calls, 0);
  assert.throws(() => createDeploymentService({ token: 'test', tenant: 'demo-tenant', targetId: 'aws-demo' }), /TENANT/);
  assert.throws(() => createDeploymentService({ token: 'test' }), /TARGET_ID/);
});

test('CLI의 폴더 입력은 API가 받는 ZIP으로 만들어진다', async () => {
  const folder = await mkdtemp(join(tmpdir(), 'jasmin-app-'));
  try {
    await writeFile(join(folder, 'package.json'), '{"name":"my-app"}');
    const archive = await archiveFromPath(folder);
    const files = await inspectArchive(archive.bytes);
    assert.deepEqual(files.map((file) => file.path), ['package.json']);
  } finally { await rm(folder, { recursive: true, force: true }); }
});

// Loopback HTTP and publication code are real; GitHub and registry access stay offline.
test('HTTP 업로드부터 Python 게시 인계를 거쳐 HTTP 상태 조회까지 연결한다', async () => {
  const root = await mkdtemp(join(tmpdir(), 'railshot-publication-'));
  const calls = [];
  let publicationFetch;
  const fetchImpl = async (url, options = {}) => {
    const value = new URL(url);
    assert.equal(value.origin, 'https://api.github.com');
    const path = value.pathname;
    assert.ok(path.startsWith('/repos/org/apps/'));
    if (publicationFetch) return publicationFetch(url, options);
    const method = options.method || 'GET';
    const body = options.body && JSON.parse(options.body);
    calls.push({ path, method, body });
    if (path.endsWith('/git/ref/heads/main') && method === 'GET') return Response.json({ object: { sha: 'a'.repeat(40) } });
    if (path.endsWith(`/git/commits/${'a'.repeat(40)}`)) return Response.json({ tree: { sha: 'base' } });
    if (path.endsWith('/git/trees/base')) return Response.json({ tree: [] });
    if (path.endsWith('/git/blobs') && method === 'POST') return Response.json({ sha: `blob-${calls.length}` });
    if (path.endsWith('/git/trees') && method === 'POST') return Response.json({ sha: 'tree' });
    if (path.endsWith('/git/commits') && method === 'POST') return Response.json({ sha: 'b'.repeat(40) });
    if (path.endsWith('/git/refs/heads/main') && method === 'PATCH') return Response.json({});
    if (path.endsWith('/dispatches') && method === 'POST') return Response.json({ workflow_run_id: 789 });
    throw new Error(`Unexpected GitHub request: ${method} ${path}`);
  };
  const service = createDeploymentService({ token: 'test', owner: 'org', repo: 'apps', targetId: 'aws-demo' }, fetchImpl);
  const server = createAppServer({ service, stateDirectory: join(root, 'product'), sourceLoader: async () => { throw new Error('ZIP upload must not fetch a source'); } });
  try {
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    const base = `http://127.0.0.1:${server.address().port}`;
    const source = { 'package.json': '{"name":"my-app"}', 'index.js': 'console.log("hello")' };
    const form = new FormData();
    form.set('app', 'my-app');
    form.set('target_id', 'aws-demo');
    form.set('archive', new Blob([await zipOf(source)]), 'app.zip');
    const response = await fetch(`${base}/api/deploy`, { method: 'POST', headers: { 'x-jasmin-request': 'deploy' }, body: form });
    assert.equal(response.status, 202);
    const submitted = await response.json();
    assert.equal(submitted.state, 'queued');
    assert.equal(submitted.run_id, 789);
    assert.equal(submitted.source_commit, 'b'.repeat(40));
    assert.equal(submitted.target_id, 'aws-demo');
    assert.deepEqual(calls.filter((call) => call.path.endsWith('/git/blobs')).map((call) =>
      Buffer.from(call.body.content, 'base64').toString()), Object.values(source));
    assert.deepEqual(calls.find((call) => call.path.endsWith('/dispatches')).body,
      { ref: 'main', inputs: { tenant: submitted.tenant, app: submitted.app,
        source_commit: submitted.source_commit, target_id: submitted.target_id } });
    const bundle = join(root, 'bundle'); await mkdir(bundle);
    const files = publishedFiles({ sourceCommit: submitted.source_commit, targetId: submitted.target_id, app: submitted.app });
    for (const name of ['railshot.yaml', 'verdict.json', 'manifest.json']) await writeFile(join(bundle, name), files[name]);
    await writeFile(join(root, 'images.json'), files['images.json']);
    const script = fileURLToPath(new URL('../../../ci/scripts/publication.py', import.meta.url));
    // test/ is under apps/api, so the repository is three parents above that directory.
    execFileSync('python3', ['-c', `
import hashlib, os, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(sys.argv[1]).parent))
import publication
def verified_registry(images_bytes, env):
    return {'visibility': 'public', 'verification': 'anonymous_manifest_read',
            'images_sha256': hashlib.sha256(images_bytes).hexdigest(), 'image_pull_secret': None}
with patch.object(publication, 'verify_registry', side_effect=verified_registry) as verify:
    publication.prepare(*sys.argv[2:], os.environ)
    verify.assert_called_once()
`, script, bundle, join(root, 'images.json'), join(root, 'published')], {
      env: { ...process.env, SOURCE_COMMIT: submitted.source_commit, GITHUB_SHA: submitted.source_commit,
        TARGET_ID: submitted.target_id, TENANT: submitted.tenant, APP: submitted.app,
        GITHUB_RUN_ID: String(submitted.run_id), GITHUB_RUN_ATTEMPT: '1', BUNDLE_ARTIFACT_ID: '100',
        REGISTRY_PREFIX: 'ghcr.io/org', REGISTRY_VISIBILITY: 'public', PYTHONDONTWRITEBYTECODE: '1' },
    });
    const published = Object.fromEntries(await Promise.all(Object.keys(files).map(async (name) => [name, await readFile(join(root, 'published', name))])));
    ({ fetchImpl: publicationFetch } = await publicationService({ files: published, headSha: submitted.source_commit }));
    const status = await fetch(`${base}/api/runs/${submitted.run_id}`);
    assert.equal(status.status, 200);
    const result = await status.json();
    assert.equal(result.state, 'published');
    for (const field of ['run_id', 'source_commit', 'target_id', 'tenant', 'app']) {
      assert.equal(result[field], submitted[field], field);
      assert.equal(result.publication[field], submitted[field], `publication.${field}`);
    }
    assert.equal(result.publication.artifact_id, 200);
    assert.equal(result.publication.producer_attempt, 1);
    assert.equal(result.url, null); // Image publication does not claim a deployed application URL.
  } finally {
    await new Promise((resolve) => server.close(resolve));
    await (await server.productReady)?.close();
    await rm(root, { recursive: true, force: true });
  }
});


test('허용된 복수 target도 publication은 접수 target과 정확히 일치해야 한다', async () => {
  const { fetchImpl } = await publicationService({ files: publishedFiles({ targetId: 'stack-aws-1002' }) });
  const service = createDeploymentService({ token: 'test', owner: 'org', repo: 'apps', targetId: 'aws-demo',
    targetIds: ['aws-demo', 'stack-aws-1002'] }, fetchImpl);
  const accepted = await service.status('789', 'stack-aws-1002');
  assert.equal(accepted.state, 'published');
  assert.equal(accepted.publication.target_id, 'stack-aws-1002');
  assert.ok((await service.publishedFiles(accepted.publication)).length);
  assert.equal((await service.status('789', 'aws-demo')).state, 'publication_unverified');
  await assert.rejects(service.status('789', 'unregistered'), /대상/);
});

function snapshotFixture() {
  const contents = [{ path: '.railshot', type: 'd', mode: 0o755, size: 0, content: null },
    ...Object.entries({ '.railshot/railshot.yaml': 'app: my-app\n', 'Dockerfile': 'FROM node:22\n', 'generated.js': 'console.log("final AI repair")\n' })
      .map(([path, text]) => ({ path, type: 'f', mode: path === 'generated.js' ? 0o755 : 0o644,
        size: Buffer.byteLength(text), content: Buffer.from(text).toString('base64') }))];
  const digest = createHash('sha256').update('railshot-source-v1\0');
  for (const entry of contents) {
    const header = Buffer.from(JSON.stringify([entry.path, entry.type, entry.mode, entry.size]));
    const size = Buffer.alloc(8); size.writeBigUInt64BE(BigInt(header.length)); digest.update(size).update(header);
    if (entry.type === 'f') digest.update(Buffer.from(entry.content, 'base64'));
  }
  return { version: 1, source_commit: 'a'.repeat(40), app: 'my-app', tenant: 'demo', target_id: 'aws-demo', run_id: 789,
    producer_attempt: 1, source_sha256: digest.digest('hex'), entries: contents };
}

async function sourceService() {
  const snapshot = snapshotFixture(), files = publishedFiles({ sourceSha256: snapshot.source_sha256 });
  const publicationZip = await zipOf(files), calls = [];
  const publication = { ...JSON.parse(files['handoff.json']), images: JSON.parse(files['images.json']), artifact_id: 200, artifact_name: 'published-1' };
  const f = { snapshot, publication, calls, missing: false, duplicate: false, artifact: {}, redirect: null, zip: null, responseSize: null };
  const service = createDeploymentService({ token: 'private-server-token', owner: 'org', repo: 'apps', targetId: 'aws-demo' }, async (url, options) => {
    calls.push({ url: String(url), authorization: options?.headers?.authorization });
    const parsed = new URL(url);
    if (parsed.hostname === 'productionresultssa1.blob.core.windows.net') {
      assert.equal(options.headers, undefined); assert.equal(options.redirect, 'error');
      return new Response(await zipOf({ 'snapshot.json': JSON.stringify(f.snapshot) }));
    }
    assert.equal(parsed.hostname, 'api.github.com');
    if (parsed.pathname.endsWith('/actions/artifacts/200/zip')) return new Response(publicationZip);
    if (parsed.pathname.endsWith('/actions/artifacts/201/zip')) {
      if (f.redirect) return new Response(null, { status: 302, headers: { location: f.redirect } });
      return new Response(f.zip || await zipOf({ 'snapshot.json': JSON.stringify(f.snapshot) }),
        { headers: f.responseSize === null ? {} : { 'content-length': String(f.responseSize) } });
    }
    assert.ok(parsed.pathname.endsWith('/runs/789/artifacts'));
    const source = parsed.searchParams.get('name') === 'source-1';
    assert.equal(parsed.searchParams.get('name'), source ? 'source-1' : 'published-1');
    const artifact = { id: source ? 201 : 200, name: source ? 'source-1' : 'published-1', expired: false, size_in_bytes: 200,
      workflow_run: { id: 789, head_sha: 'a'.repeat(40) }, archive_download_url: 'https://hostile.invalid/must-not-follow',
      ...(source ? f.artifact : {}) };
    const artifacts = source && f.missing ? [] : source && f.duplicate ? [artifact, { ...artifact, id: 202 }] : [artifact];
    return Response.json({ total_count: artifacts.length, artifacts });
  });
  f.read = () => service.sourceFiles(publication);
  return f;
}

test('final gate-tested source is verified through the publication and exact source artifact before returning buffers', async () => {
  const f = await sourceService(), files = await f.read();
  assert.deepEqual(files.map((file) => file.path), ['.railshot/railshot.yaml', 'Dockerfile', 'generated.js']);
  assert.equal(files.at(-1).content.toString(), 'console.log("final AI repair")\n');
  assert.ok(files.every((file) => Buffer.isBuffer(file.content) && Object.keys(file).join(',') === 'path,content'));
  assert.match(f.calls[0].url, /name=published-1/);
  assert.ok(f.calls.every((call) => !call.url.includes('hostile.invalid')));
  f.redirect = 'https://productionresultssa1.blob.core.windows.net/actions-results/source.zip?sig=private-signed-query';
  assert.equal((await f.read()).length, 3);
  assert.equal(f.calls.at(-1).authorization, undefined, 'GitHub token must never reach object storage');
});

test('only an absent source artifact permits the explicit legacy fallback', async () => {
  const f = await sourceService(); f.missing = true;
  await assert.rejects(f.read(), { code: 'SOURCE_NOT_AVAILABLE', status: 404 });
  for (const change of [
    (f) => { f.artifact.expired = true; }, (f) => { f.duplicate = true; },
    (f) => { f.artifact.workflow_run = { id: 790, head_sha: 'a'.repeat(40) }; },
    (f) => { f.artifact.workflow_run = { id: 789, head_sha: 'b'.repeat(40) }; },
    (f) => { f.artifact.name = 'source-2'; }, (f) => { f.artifact.size_in_bytes = sourceSnapshotLimit + 1; },
    (f) => { f.responseSize = sourceSnapshotLimit + 1; },
    (f) => { f.redirect = 'https://hostile.invalid/private-server-token'; },
    (f) => { f.redirect = 'http://productionresultssa1.blob.core.windows.net/source.zip'; },
    (f) => { f.snapshot.producer_attempt = 2; }, (f) => { f.snapshot.run_id = 790; },
    (f) => { f.snapshot.source_commit = 'b'.repeat(40); }, (f) => { f.snapshot.app = 'other-app'; },
    (f) => { f.snapshot.tenant = 'other'; }, (f) => { f.snapshot.target_id = 'other'; },
    (f) => { f.snapshot.entries.at(-1).content = Buffer.from('tampered source').toString('base64'); },
    (f) => { f.snapshot.entries.at(-1).mode = 0o644; },
    (f) => { f.publication.bundle_artifact_id = 999; },
  ]) {
    const f = await sourceService(); change(f);
    await assert.rejects(f.read(), (error) => error.code === 'SOURCE_VERIFICATION_FAILED' && error.status === 502
      && !error.message.includes('private-server-token'));
  }
});

test('snapshot rejects private paths, links, malformed trees, duplicate files and noncanonical content', async () => {
  for (const mutate of [
    (v) => { v.entries.at(-1).path = '../private'; }, (v) => { v.entries.at(-1).path = '.env'; },
    (v) => { v.entries.at(-1).path = '.ssh/id_rsa'; }, (v) => { v.entries.at(-1).path = 'node_modules/index.js'; },
    (v) => { v.entries.at(-1).type = 'l'; }, (v) => { v.entries.at(-1).mode = 0o4755; },
    (v) => { v.entries.at(-1).path = 'missing/file.js'; }, (v) => { v.entries.push(v.entries.at(-1)); },
    (v) => { v.entries.reverse(); }, (v) => { v.entries.at(-1).content += '\n'; },
    (v) => { v.entries.at(-1).path = 'auth.txt'; const text = '-----BEGIN PRIVATE KEY-----'; v.entries.at(-1).content = Buffer.from(text).toString('base64'); v.entries.at(-1).size = text.length; },
    (v) => { v.entries = Array.from({ length: 2001 }, (_, i) => ({ path: `file${i}`, type: 'f', mode: 0o644, size: 0, content: '' })); },
  ]) {
    const value = snapshotFixture(); mutate(value);
    assert.throws(() => readSourceSnapshot(Buffer.from(JSON.stringify(value)), value, value.source_sha256));
  }
  const wrongEntry = await sourceService(); wrongEntry.zip = await zipOf({ 'foreign.json': '{}' });
  await assert.rejects(wrongEntry.read(), { code: 'SOURCE_VERIFICATION_FAILED' });
  const extraEntry = await sourceService(); extraEntry.zip = await zipOf({ 'snapshot.json': JSON.stringify(extraEntry.snapshot), 'secret.txt': 'private-canary' });
  await assert.rejects(extraEntry.read(), { code: 'SOURCE_VERIFICATION_FAILED' });
});

test('snapshot digest interoperates with Python gate hashing for Unicode names, directory order and executable permissions', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'railshot-source-digest-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const script = `import base64,json,os,stat,sys\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nfrom gate.bundle import source_digest\np=Path(sys.argv[2]);(p/'a').mkdir();(p/'a'/'한글😀.txt').write_bytes(b'final source');(p/'a.txt').write_bytes(b'sibling');(p/'empty').mkdir();(p/'a.txt').chmod(0o755)\nentries=[]\ndef visit(d):\n for q in sorted(d.iterdir(),key=lambda p:p.name):\n  s=q.lstat();f=q.is_file();entries.append(dict(path=q.relative_to(p).as_posix(),type='f' if f else 'd',mode=stat.S_IMODE(s.st_mode),size=s.st_size if f else 0,content=base64.b64encode(q.read_bytes()).decode() if f else None))\n  if not f:visit(q)\nvisit(p)\nprint(json.dumps(dict(source_sha256=source_digest(p),entries=entries)))`;
  const scripts = fileURLToPath(new URL('../../../ci/scripts', import.meta.url));
  const generated = JSON.parse(execFileSync('python3', ['-c', script, scripts, root], { encoding: 'utf8' }));
  const value = { ...snapshotFixture(), ...generated };
  const read = readSourceSnapshot(Buffer.from(JSON.stringify(value)), value, value.source_sha256);
  assert.deepEqual(read.map((file) => file.path), ['a/한글😀.txt', 'a.txt']);
});

test('large source trees are bounded, complete, and never dispatch after a tree failure', async () => {
  const files = Array.from({ length: 231 }, (_, i) => ({ path: `src/group-${i % 7}/file-${i}.js`, content: Buffer.from(`file ${i}`) }));
  for (const failure of [null, 'tree', 'dispatch']) {
    const trees = new Map(); let sourceWrites = 0, commits = 0, dispatches = 0, publishedTree;
    const service = createDeploymentService({ token: 'secret', targetId: 'aws-demo' }, async (url, options = {}) => {
      const path = new URL(url).pathname, body = options.body && JSON.parse(options.body);
      if (path.endsWith('/git/ref/heads/main')) return Response.json({ object: { sha: 'a'.repeat(40) } });
      if (path.endsWith('/git/commits/' + 'a'.repeat(40))) return Response.json({ tree: { sha: 'base' } });
      if (path.endsWith('/git/trees/base')) return Response.json({ tree: [] });
      if (path.endsWith('/git/blobs')) return Response.json({ sha: createHash('sha1').update(body.content).digest('hex') });
      if (path.endsWith('/git/trees')) {
        if (body.tree[0].type === 'tree') { publishedTree = trees.get(body.tree[0].sha); return Response.json({ sha: 'root' }); }
        sourceWrites++;
        if (body.tree.length > 100 || failure === 'tree' && sourceWrites === 2) return Response.json({ message: 'private upstream detail' }, { status: 504 });
        const entries = new Map(body.base_tree ? trees.get(body.base_tree) : []);
        for (const entry of body.tree) entries.set(entry.path, entry.sha);
        const sha = `tree-${sourceWrites}`; trees.set(sha, entries); return Response.json({ sha });
      }
      if (path.endsWith('/git/commits')) { commits++; return Response.json({ sha: 'b'.repeat(40) }); }
      if (path.endsWith('/git/refs/heads/main')) return Response.json({});
      if (path.endsWith('/dispatches')) {
        dispatches++;
        if (failure === 'dispatch') throw new DOMException('private upstream detail', 'TimeoutError');
        return Response.json({ workflow_run_id: 987 });
      }
      assert.fail(path);
    });
    if (failure) {
      await assert.rejects(service.deploy({ app: 'large-app', files }), (error) => {
        assert.equal(error.phase, failure === 'tree' ? 'source_tree' : 'ci_dispatch');
        assert.equal(error.upstream_status, failure === 'tree' ? 504 : null);
        assert.equal(error.outcomeUnknown, failure === 'dispatch');
        assert.ok(!error.message.includes('private upstream detail'));
        return true;
      });
      assert.equal(dispatches, failure === 'tree' ? 0 : 1);
      assert.equal(commits, failure === 'tree' ? 0 : 1);
    } else {
      assert.equal((await service.deploy({ app: 'large-app', files })).run_id, 987);
      assert.deepEqual([...publishedTree.keys()].sort(), files.map((f) => f.path).sort());
      assert.equal(dispatches, 1);
      assert.equal(commits, 1);
    }
  }
});
