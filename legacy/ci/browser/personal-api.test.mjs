import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as pause } from 'node:timers/promises';
import test from 'node:test';
import { chromium } from 'playwright';
import { createServer as createViteServer } from 'vite';
import { createAppServer } from '../../apps/api/src/server.js';

// Real dashboard, HTTP routes, owner cookies and SQLite. Only customer OpenStack
// effects are replaced so the test cannot reach or modify external infrastructure.
test('personal target resolution and owner recovery cross two isolated browser contexts through the real API', { timeout: 60000 }, async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-personal-browser-api-'));
  const deploymentReady = new Set();
  const personalAdapter = {
    config: { public_url: 'https://api.example.test', installer_url: 'https://api.example.test/install.sh', artifact_url: 'https://api.example.test/client.tgz', artifact_sha256: 'b'.repeat(64) },
    readiness: async () => ({ scope: 'personal', ready: true, verification_scope: 'configuration_only', blockers: [] }),
    register: async () => ({ status: 'succeeded', tunnel: { server_public_key: 'B'.repeat(43) + '=', endpoint: 'gateway.example.test:51820', address: '10.80.0.2/32', allowed_ips: '10.80.0.1/32' } }),
    prepare: async () => false,
    verify: async () => ({ status: 'succeeded', reachable: true, openstack_verified: true }),
    prepareRuntime: async (target) => {
      deploymentReady.add(target.id);
      return { status: 'succeeded', blockers: [], binding_sha256: 'a'.repeat(64), verified_at: new Date().toISOString() };
    },
    verifyRuntime: async (target) => ({ status: deploymentReady.has(target.id) ? 'succeeded' : 'blocked', blockers: deploymentReady.has(target.id) ? [] : ['RUNTIME_RESOURCE_UNVERIFIED'], binding_sha256: 'a'.repeat(64), verified_at: new Date().toISOString() }),
    deploymentReady: (target) => deploymentReady.has(target.id),
  };
  const server = createAppServer({ stateDirectory: directory, service: null, personalAdapter, pollInterval: 1 });
  await server.productReady;
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const apiOrigin = `http://127.0.0.1:${server.address().port}`;
  const previousTarget = process.env.RAILSHOT_DEV_API_TARGET;
  process.env.RAILSHOT_DEV_API_TARGET = apiOrigin;
  let vite;
  try {
    vite = await createViteServer({ root: fileURLToPath(new URL('../../apps/dashboard/', import.meta.url)),
      server: { host: '127.0.0.1', port: 0 }, logLevel: 'error' });
    await vite.listen();
  } finally {
    if (previousTarget === undefined) delete process.env.RAILSHOT_DEV_API_TARGET;
    else process.env.RAILSHOT_DEV_API_TARGET = previousTarget;
  }
  const origin = `http://127.0.0.1:${vite.httpServer.address().port}`;
  const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined });
  t.after(async () => {
    await browser.close();
    await vite.close();
    await new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); });
    await (await server.productReady).close();
    await rm(directory, { recursive: true, force: true });
  });

  const contextA = await browser.newContext({ serviceWorkers: 'block', viewport: { width: 1280, height: 960 } });
  const contextB = await browser.newContext({ serviceWorkers: 'block', viewport: { width: 1280, height: 960 } });
  const pageA = await contextA.newPage(), pageB = await contextB.newPage(), errors = [];
  for (const page of [pageA, pageB]) { page.setDefaultTimeout(10000); page.on('pageerror', (error) => errors.push(error.message)); }
  const headers = { 'X-Railshot-Request': 'dashboard' };
  const post = (context, path, data, extra = {}) => context.request.post(origin + path, { data, headers: { ...headers, ...extra } });

  await pageA.goto(origin);
  await pageA.getByRole('button', { name: '개인 배포환경 관리' }).click();
  await pageA.getByLabel('환경 이름').fill('실제 API OpenStack');
  await pageA.getByRole('button', { name: '등록 시작' }).click();
  await pageA.getByText('복구 키를 안전한 곳에 저장하세요').waitFor();
  const recoveryKey = await pageA.locator('#environment-recovery-key-value').innerText();
  assert.match(recoveryKey, /^[A-Za-z0-9_-]{32,256}$/);

  let response = await contextA.request.get(origin + '/api/v1/targets?scope=owned&provider=openstack', { headers });
  assert.equal(response.status(), 200, await response.text());
  const owned = await response.json();
  assert.equal(owned.items.length, 1);
  const targetId = owned.items[0].id;

  response = await post(contextA, `/api/v1/targets/${targetId}/enrollments`, {});
  assert.equal(response.status(), 201, await response.text());
  const enrollment = await response.json();
  const enrollmentToken = /RAILSHOT_ENROLLMENT_TOKEN='([^']+)'/.exec(enrollment.install_command)?.[1];
  assert.ok(enrollmentToken);
  response = await post(contextA, `/api/v1/enrollments/${enrollment.id}/claims`, {
    public_key: 'A'.repeat(43) + '=', client_version: '1.0.0', project_id: 'project-browser-api',
    runtime: { ssh_host_key: 'ssh-ed25519 ' + 'A'.repeat(68) },
  }, { Authorization: `Bearer ${enrollmentToken}` });
  assert.equal(response.status(), 201, await response.text());
  const claim = await response.json();
  response = await post(contextA, `/api/v1/targets/${targetId}/heartbeats`, {
    generation: claim.generation, client_version: '1.0.0', checks: { tunnel: true, openstack: true, runtime: true },
  }, { Authorization: `Bearer ${claim.client_token}` });
  assert.equal(response.status(), 200, await response.text());
  response = await post(contextA, `/api/v1/targets/${targetId}/runtimes`, {
    generation: claim.generation,
    evidence: { resource_id: 'vm-browser-api', private_ipv4: '10.0.0.2', management_network: 'private', placement: 'nova', architecture: 'amd64', initialization: 'preconfigured', ssh_user: 'railshot-runtime', ssh_port: 2223, ssh_host_key: 'ssh-ed25519 ' + 'A'.repeat(68) },
  }, { Authorization: `Bearer ${claim.client_token}` });
  assert.equal(response.status(), 202, await response.text());
  let target;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    response = await contextA.request.get(origin + `/api/v1/targets/${targetId}`, { headers });
    target = await response.json();
    if (target.runtime_preparation?.status === 'succeeded') break;
    await pause(10);
  }
  assert.equal(target.runtime_preparation.status, 'succeeded');
  assert.equal(target.deployable, true);

  const selectionPage = await contextA.newPage();
  selectionPage.setDefaultTimeout(10000);
  let releasePreferences, signalPreferences;
  const preferencesRequested = new Promise((resolve) => { signalPreferences = resolve; });
  const preferencesRelease = new Promise((resolve) => { releasePreferences = resolve; });
  await selectionPage.route('**/api/v1/preferences', async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    signalPreferences(); await preferencesRelease;
    await route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ view: 'deploy', environment: 'cloud', provider: 'aws' }) });
  });
  await selectionPage.goto(origin); await preferencesRequested;
  await selectionPage.getByRole('radio', { name: /온프레미스/ }).check();
  releasePreferences();
  await selectionPage.waitForFunction((id) => document.querySelector('#session-note').textContent.includes('까지')
    && [...document.querySelector('#provider').options].some((option) => option.value === id), targetId);
  assert.equal(await selectionPage.locator('[name="environment"]:checked').inputValue(), 'onprem');
  await selectionPage.locator('#provider').selectOption(targetId);
  assert.equal(await selectionPage.locator('#provider').inputValue(), targetId);
  const resolvedRequest = selectionPage.waitForRequest((request) => new URL(request.url()).pathname === '/api/v1/applications/resolve');
  await selectionPage.locator('#repository-url').fill('https://github.com/example/preferences-race');
  await selectionPage.locator('#deploy-form button[type="submit"]').click();
  const resolvedUrl = new URL((await resolvedRequest).url());
  assert.equal(resolvedUrl.searchParams.get('environment'), 'onprem');
  assert.equal(resolvedUrl.searchParams.get('provider'), 'openstack');
  assert.equal(resolvedUrl.searchParams.get('target_id'), targetId);
  await selectionPage.locator('#review-panel').waitFor({ state: 'visible' });
  await selectionPage.close();

  const resolveQuery = new URLSearchParams({ environment: 'onprem', provider: 'openstack', app: 'actual-app', target_id: targetId });
  response = await contextA.request.get(origin + `/api/v1/applications/resolve?${resolveQuery}`);
  assert.equal(response.status(), 200, await response.text());
  const resolution = await response.json();
  assert.deepEqual(resolution, { app: 'actual-app', environment_target_id: targetId, application: null });
  assert.equal((await contextB.request.get(origin + `/api/v1/applications/resolve?${resolveQuery}`)).status(), 404);
  const dashboardCompatible = new URLSearchParams({ environment: 'onprem', provider: 'openstack', app: 'actual-app', targetId, registerNew: 'false' });
  response = await contextA.request.get(origin + `/api/v1/applications/resolve?${dashboardCompatible}`);
  assert.equal(response.status(), 200, await response.text());
  assert.deepEqual(await response.json(), resolution);
  assert.equal((await contextB.request.get(origin + `/api/v1/applications/resolve?${dashboardCompatible}`)).status(), 404);

  await pageA.getByRole('button', { name: '목록 새로고침' }).click();
  await pageA.getByRole('button', { name: '실제 API OpenStack' }).waitFor();
  await pageB.goto(origin);
  await pageB.getByRole('button', { name: '개인 배포환경 관리' }).click();
  await pageB.getByText('복구 키로 관리권 복구').click();
  await pageB.locator('#environment-recovery-input').fill(recoveryKey);
  await pageB.getByRole('button', { name: '관리권 복구' }).click();
  await pageB.getByText('이 브라우저에서 관리권을 복구했습니다.').waitFor();
  await pageB.getByRole('button', { name: '실제 API OpenStack' }).waitFor();
  assert.equal((await contextA.request.get(origin + `/api/v1/targets/${targetId}`, { headers })).status(), 404);
  assert.equal((await contextB.request.get(origin + `/api/v1/targets/${targetId}`, { headers })).status(), 200);
  assert.equal((await post(contextA, '/api/v1/recoveries', { recovery_key: recoveryKey })).status(), 401);
  await pageA.getByRole('button', { name: '목록 새로고침' }).click();
  await pageA.getByText('아직 등록한 OpenStack 환경이 없습니다.').waitFor();
  assert.equal(await pageA.locator('#environment-owned-items button').count(), 0);
  assert.deepEqual(errors, []);
});
