import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdir, mkdtemp, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { chromium } from 'playwright';
import { createServer as createViteServer } from 'vite';
import { createAppServer } from '../../apps/api/src/server.js';
import { createProductStore } from '../../apps/api/src/product-store.js';

// Real HTTP/session/store/pagination, simulated CI and observer. No cloud resources or paid calls.
test('session history pages and live environment states remain truthful across navigation and failures', { timeout: 60000 }, async (t) => {
  const directory = await realpath(await mkdtemp(join(tmpdir(), 'railshot-live-ui-')));
  const store = await createProductStore(directory), owner = store.dashboard.session(), stranger = store.dashboard.session();
  store.dashboard.preferences(owner.id, { view: 'history' });
  const record = (id, sessionId, extra = {}) => ({ id, kind: 'deployments', session_id: sessionId, app: `app-${id}`, target_id: 'demo-aws',
    status: 'succeeded', created_at: new Date(Date.now() - (100 - Number(id.replace(/\D/g, ''))) * 1000).toISOString(), ...extra });
  await store.transaction((state) => {
    for (let n = 1; n <= 23; n++) state.operations[`deployment-${n}`] = record(`deployment-${n}`, owner.id,
      n === 22 ? { status: 'failed', error: { message: '테스트 실패' } } : {});
    state.operations.foreign = record('foreign', stranger.id, { app: 'PRIVATE-OTHER-SESSION' });
    for (let n = 1; n <= 12; n++) {
      const id = `build-${n}`, run = String(1000 + n);
      state.operations[id] = record(id, owner.id, { kind: 'builds', ci: { run_id: run } });
      state.bindings[run] = { operation_id: id, app: `app-${id}`, target_id: 'demo-aws' };
    }
  });
  await store.close();
  let broken = false, recovered = false, healthFailed = false;
  const observeMetrics = async (row) => {
    const state = broken ? 'unavailable' : row.target_id === 'demo-gcp' && !recovered ? 'stale' : 'ready';
    const observed_at = new Date(Date.now() - (state === 'stale' ? 120000 : 0)).toISOString();
    return { target_id: row.target_id, app: row.app, deployment_id: row.id ?? null, checked_at: new Date().toISOString(), stale_after_seconds: 90,
      runtime: { status: state, observed_at }, metrics: Object.fromEntries(Object.entries({ runtime_healthz: healthFailed && row.target_id === 'demo-openstack' ? 0 : 1, node_up: 1, cpu_percent: 0, memory_percent: 32.5, disk_percent: 20,
        pods: 2, http: row.target_id === 'demo-openstack' && !recovered ? 0 : 1, network_receive_bytes_per_second: null, network_transmit_bytes_per_second: null })
        .map(([name, value]) => [name, { state: value === null ? 'no_data' : state, value: state === 'ready' ? value : null, observed_at, scope: 'target_node' }])) };
  };
  const service = { targetId: 'demo-aws', targetIds: ['demo-aws', 'demo-gcp', 'demo-openstack'],
    deploy: async () => ({ run_id: 9999, source_commit: 'a'.repeat(40) }),
    status: async (id) => ({ run_id: Number(id), state: 'failed', status: 'completed', conclusion: 'failure',
      steps: [{ key: 'loop', status: 'completed', conclusion: 'failure', tasks: [{ number: 1, name: 'Fixture task', status: 'completed', conclusion: 'failure' }] }] }) };
  const cd = async () => ({});
  cd.targets = Object.fromEntries(service.targetIds.map((id) => [id, { applicationName: `${id}-app` }]));
  const server = createAppServer({ stateDirectory: directory, service, observeMetrics, deployPublished: cd, pollInterval: 1,
    providerTargets: { aws: 'demo-aws', gcp: 'demo-gcp', openstack: 'demo-openstack' }, target: { provider: 'aws' } });
  await server.productReady; server.listen(0, '127.0.0.1'); await once(server, 'listening');
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
  t.after(async () => { await browser.close(); await vite.close(); await new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); });
    await (await server.productReady).close(); await rm(directory, { recursive: true, force: true }); });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1050 } });
  await context.addCookies([{ name: 'railshot_session', value: owner.token, url: origin, httpOnly: true, sameSite: 'Strict' }]);
  const page = await context.newPage(), errors = [], queries = [];
  page.setDefaultTimeout(10000); page.on('pageerror', (error) => errors.push(error.message));
  page.on('request', (request) => { if (/\/api\/v1\/(deployments|builds)\?/.test(request.url())) queries.push(new URL(request.url())); });
  await page.goto(origin);
  const rows = page.locator('#history-list > li');
  await page.waitForFunction(() => document.querySelector('#history-list').getAttribute('aria-busy') === 'false');
  assert.equal(await rows.count(), 10); assert.match(await rows.first().innerText(), /app-deployment-23/);
  assert.match(await rows.nth(1).innerText(), /실행 실패/);
  await rows.nth(1).getByRole('button', { name: 'app-deployment-22 실행 상세·작업 로그' }).click();
  await page.locator('#deployment-history-detail h2').waitFor();
  assert.equal(await page.locator('#deployment-history-detail h2').innerText(), 'app-deployment-22');
  assert.equal(await page.locator('.dh-log-layout').count(), 0);
  await page.locator('.dh-issue-trigger').click();
  assert.match(await page.locator('.dh-stage-detail').innerText(), /테스트 실패/);
  assert.equal(await page.locator('.dh-recovery').count(), 0, 'no invented recovery questions or submission capability');
  assert.equal(await page.locator('#deployment-history-detail a').count(), 0, 'no unverified service links');
  await page.getByRole('button', { name: '‹ 배포 내역', exact: true }).click();
  await page.getByLabel('앱 이름 검색', { exact: true }).fill('deployment-22');
  assert.equal(await rows.count(), 1);
  await page.getByLabel('상태', { exact: true }).selectOption('running');
  assert.equal(await rows.count(), 0); assert.equal(await page.locator('#history-empty').isVisible(), true);
  await page.getByLabel('상태', { exact: true }).selectOption('');
  await page.getByLabel('앱 이름 검색', { exact: true }).fill('');
  await page.locator('#history-next').click();
  await page.waitForFunction(() => document.querySelector('#history-page').textContent.startsWith('2페이지'));
  const secondPage = await rows.allTextContents(); assert.match(secondPage[0], /app-deployment-13/);
  await page.reload(); await page.waitForFunction(() => document.querySelector('#history-list').getAttribute('aria-busy') === 'false');
  assert.deepEqual(await rows.allTextContents(), secondPage);
  // A new request admitted in this session must not shift the active marker page.
  const result = await context.request.post(origin + '/api/v1/deployments', { headers: { 'Idempotency-Key': 'new-arrival' },
    multipart: { app: 'demo-aws-app', target_id: 'demo-aws', files: { name: 'app.js', mimeType: 'text/javascript', buffer: Buffer.from('console.log(1)') }, paths: '["app.js"]' } });
  assert.equal(result.status(), 202, await result.text());
  await page.locator('#history-next').click(); await page.waitForFunction(() => document.querySelector('#history-page').textContent.startsWith('3페이지'));
  assert.equal(await rows.count(), 3); assert.match(await rows.last().innerText(), /app-deployment-1/);
  assert.equal(await page.locator('#history-next').isDisabled(), true);
  await page.locator('#history-prev').click(); await page.waitForFunction(() => document.querySelector('#history-page').textContent.startsWith('2페이지'));
  assert.deepEqual(await rows.allTextContents(), secondPage);
  await page.locator('#history-refresh').click(); await page.waitForFunction(() => document.querySelector('#history-list').textContent.includes('demo-aws-app'));
  assert.match(await page.locator('#history-page').innerText(), /1페이지/);
  await page.locator('#history-kind').selectOption('builds');
  await page.waitForFunction(() => document.querySelector('#history-list').textContent.includes('app-build-12'));
  assert.equal(await rows.count(), 10); assert.match(await page.locator('#history-page').innerText(), /1페이지/);
  assert.match(await rows.first().innerText(), /이미지 게시 완료/);
  assert.doesNotMatch(await rows.first().innerText(), /앱 배포 완료/);
  await rows.first().getByRole('button').click();
  await page.waitForFunction(() => document.querySelector('#run-steps').textContent.includes('Fixture task'));
  assert.ok(queries.every((url) => url.searchParams.get('limit') === '10'));
  assert.ok(queries.some((url) => url.searchParams.has('marker')));
  const emptyContext = await browser.newContext(), empty = await emptyContext.newPage();
  await empty.goto(origin); await empty.locator('[data-view="history"]').click();
  await empty.waitForFunction(() => document.querySelector('#history-list').getAttribute('aria-busy') === 'false');
  assert.equal(await empty.locator('#history-list > li').count(), 0);
  assert.doesNotMatch(await page.locator('#history-list').textContent(), /PRIVATE-OTHER/);
  assert.equal((await emptyContext.request.get(origin + '/api/v1/deployments/deployment-23')).status(), 404);
  await page.locator('[data-view="monitor"]').click();
  await page.waitForFunction(() => document.querySelector('#environment-message').textContent.includes('마지막 조회'));
  assert.equal(await page.locator('#environment-list tr').count(), 3);
  assert.equal(await page.locator('#environment-list [data-state="ready"]').count(), 2);
  assert.equal(await page.locator('#environment-list [data-state="stale"]').count(), 1);
  assert.equal(await page.locator('#environment-list [data-state="failed"]').count(), 0, 'app HTTP failure cannot disconnect the runtime');
  assert.match(await page.locator('#environment-list').innerText(), /0.0%/, 'observed zero remains a real zero');
  await page.locator('#monitor-provider').selectOption('gcp');
  await page.waitForFunction(() => document.querySelectorAll('#environment-list tr').length === 1);
  assert.match(await page.locator('#environment-list').innerText(), /오래된 값/);
  broken = true; await page.locator('#monitor-refresh').click();
  await page.waitForFunction(() => document.querySelector('#environment-list').textContent.includes('수집 연결 실패'));
  assert.doesNotMatch(await page.locator('#environment-list').innerText(), /0.0%/);
  broken = false; recovered = true; await page.locator('#monitor-refresh').click();
  await page.waitForFunction(() => document.querySelector('#environment-list [data-state="ready"]'));
  await page.locator('#monitor-provider').selectOption('');
  await page.waitForFunction(() => document.querySelectorAll('#environment-list tr').length === 3);
  delete cd.targets['demo-openstack'];
  await page.locator('#monitor-refresh').click();
  await page.waitForFunction(() => document.querySelector('#environment-list').textContent.includes('앱 미지정'));
  assert.equal(await page.locator('#environment-list [data-state="ready"]').count(), 3, 'deleting the app binding preserves runtime connectivity');
  healthFailed = true; await page.locator('#monitor-refresh').click();
  await page.waitForFunction(() => document.querySelector('#environment-list [data-state="failed"]'));
  assert.equal(await page.locator('#environment-list [data-state="ready"]').count(), 2);
  healthFailed = false; await page.locator('#monitor-refresh').click();
  await page.waitForFunction(() => document.querySelectorAll('#environment-list [data-state="ready"]').length === 3);
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
  await page.locator('[data-view="deploy"]').click();
  await page.getByRole('radio', { name: /온프레미스/ }).check();
  await page.waitForFunction(() => document.querySelector('#provider option[value="__new_openstack__"]'));
  assert.equal(await page.locator('#provider option[value="openstack"]').count(), 0, 'static OpenStack targets are not personal deployment choices');
  await page.locator('#provider').selectOption('__new_openstack__');
  assert.match(await page.locator('#connection-status').innerText(), /등록한 뒤 배포/, 'an unregistered OpenStack target cannot start deployment');
  await page.locator('[data-view="monitor"]').click();
  await page.waitForFunction(() => document.querySelector('#environment-message').textContent.includes('마지막 조회'));
  await page.locator('#environment-detail summary').first().click();
  assert.match(await page.locator('#environment-detail').innerText(), /네트워크 수신.*데이터 없음/s);
  if (process.env.CI_OUTPUT_DIR) {
    await mkdir(process.env.CI_OUTPUT_DIR, { recursive: true });
    await page.screenshot({ path: join(process.env.CI_OUTPUT_DIR, 'live-environments-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: join(process.env.CI_OUTPUT_DIR, 'live-environments-mobile.png'), fullPage: true });
    await page.locator('[data-view="history"]').click(); await page.locator('#history-kind').selectOption('deployments');
    await page.waitForFunction(() => document.querySelector('#history-list').textContent.includes('demo-aws-app'));
    await page.screenshot({ path: join(process.env.CI_OUTPUT_DIR, 'session-history-mobile.png'), fullPage: true });
  }
  await page.locator('[data-view="history"]').click();
  await page.locator('#history-kind').selectOption('deployments');
  await page.route('**/api/v1/deployments?*', (route) => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { message: '조회 연결 실패' } }) }));
  await page.locator('#history-refresh').click();
  await page.waitForFunction(() => document.querySelector('#history-summary').textContent.includes('확인하지 못했습니다'));
  assert.equal(await rows.count(), 0); assert.match(await page.locator('#history-detail').innerText(), /조회 연결 실패/);
  await page.unroute('**/api/v1/deployments?*'); await page.locator('#history-refresh').click();
  await page.waitForFunction(() => document.querySelector('#history-list').textContent.includes('demo-aws-app'));
  await page.clock.install();
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
  await page.locator('[data-view="monitor"]').click();
  await page.waitForFunction(() => document.querySelector('#environment-message').textContent.includes('마지막 조회'));
  // Exercise the actual request timeout/abort path, not an HTTP error response.
  for (const path of ['**/api/v1/targets?*', '**/api/v1/targets/demo-aws/observations']) {
    const held = [];
    await page.route(path, (route) => { held.push(route); });
    const requested = page.waitForRequest(path);
    await page.locator('#monitor-refresh').click(); await requested;
    await page.clock.fastForward(15001);
    await page.waitForFunction(() => document.querySelector('#environment-message').textContent.includes('환경 조회 실패'));
    assert.match(await page.locator('#environment-message').innerText(), /30초 후/);
    assert.equal(await page.locator('#environment-list [data-state="missing"]').count(), 3);
    await page.unroute(path);
    await Promise.all(held.map((route) => route.continue().catch(() => {})));
    await page.clock.fastForward(30001);
    await page.waitForFunction(() => document.querySelector('#environment-message').textContent.includes('마지막 조회'));
    assert.doesNotMatch(await page.locator('#environment-list').innerText(), /조회 실패/);
  }
  const held = []; let targetRequests = 0;
  await page.route('**/api/v1/targets?*', (route) => { targetRequests++; held.push(route); });
  const requested = page.waitForRequest('**/api/v1/targets?*');
  await page.locator('#monitor-refresh').click(); await requested;
  await page.locator('[data-view="history"]').click();
  await page.clock.fastForward(45001);
  assert.equal(targetRequests, 1, 'leaving the view cancels observation polling without retry');
  await page.unroute('**/api/v1/targets?*');
  await Promise.all(held.map((route) => route.continue().catch(() => {})));
  assert.deepEqual(errors, []);
});
