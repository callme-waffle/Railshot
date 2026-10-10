import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdir, readFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { join } from 'node:path';
import test from 'node:test';
import { chromium } from 'playwright';

async function fixture(t) {
  const state = { applications: [
    { id: 'app-ready', app: 'my-app', status: 'ready', environment_target_id: 'aws-shared' },
    { id: 'app-stopped', app: 'paused-app', status: 'stopped', environment_target_id: 'gcp-shared' },
    { id: 'app-building', app: 'building-app', status: 'queued', environment_target_id: 'openstack-shared' },
  ], plans: [], writes: [], gets: 0, outcome: 'succeeded', planMode: 'valid', operations: new Map(), errors: [] };
  const send = (response, status, data, location) => {
    response.writeHead(status, { 'content-type': 'application/json', ...(location ? { location } : {}) });
    response.end(JSON.stringify(data));
  };
  const server = createServer(async (request, response) => {
    const path = new URL(request.url, 'http://localhost').pathname;
    const chunks = []; for await (const chunk of request) chunks.push(chunk);
    const input = chunks.length ? JSON.parse(Buffer.concat(chunks)) : null;
    if (path === '/api/v1/sessions') return send(response, 200, { expires_at: '2099-01-01T00:00:00Z' });
    if (path === '/api/v1/preferences') return send(response, 200, { view: 'history', environment: 'cloud', provider: 'aws' });
    if (path === '/api/v1/applications') { state.gets++; return send(response, 200, { items: state.applications }); }
    if (path === '/api/v1/deployments') return send(response, 200, { items: [
      { id: 'deploy-1', application_id: 'app-building', app: 'building-app', target_id: 'openstack-shared', status: 'running' },
      { id: 'deploy-old', app: 'legacy-app', target_id: 'aws-shared', status: 'succeeded' },
    ] });
    if (path === '/api/v1/deployments/deploy-1') return send(response, 200, { id: 'deploy-1', application_id: 'app-building', app: 'building-app', target_id: 'openstack-shared', status: 'running', steps: [] });
    if (/\/deployments\/deploy-1\/(events|logs|metrics)$/.test(path)) return send(response, 200, { deployment_id: 'deploy-1', events: [], lines: [], metrics: {} });
    if (['/api/v1/connections', '/api/v1/targets', '/api/v1/profiles', '/api/v1/options'].includes(path)) return send(response, 200, { items: [] });
    const plan = /^\/api\/v1\/applications\/([^/]+)\/plans$/.exec(path);
    if (plan) {
      state.plans.push({ id: plan[1], input });
      const result = { id: '11111111-1111-4111-8111-111111111111', application_id: state.planMode === 'foreign' ? 'foreign-app' : plan[1],
        action: input.action, plan_hash: 'a'.repeat(64), expires_at: new Date(Date.now() + (state.planMode === 'expired' ? -1000 : 600000)).toISOString(),
        resources: state.resources || [{ kind: 'Deployment', namespace: 'app-private', name: 'workload' }, { kind: 'PersistentVolumeClaim', namespace: 'app-private', name: 'database' }],
        retained: [{ kind: 'Node', name: 'shared-node' }, { kind: 'LoadBalancer', name: 'shared-lb' }] };
      state.planDocument = result;
      if (state.planMode === 'async') {
        return send(response, 202, { id: result.id, application_id: result.application_id, action: result.action, status: 'planning', created_at: new Date().toISOString() }, `/api/v1/applications/${plan[1]}/plans/${result.id}`);
      }
      return send(response, 200, result);
    }
    if (/^\/api\/v1\/applications\/[^/]+\/plans\/[^/]+$/.test(path)) {
      state.planReads = (state.planReads || 0) + 1;
      if (state.planReadError) return send(response, 503, { error: { message: '계획 조회 연결 실패' } });
      const doc = state.planDocument;
      const accepted = [...state.operations.values()].find(row => row.plan_id === doc.id && row.application_id === doc.application_id);
      return send(response, 200, { ...doc, ...(accepted ? { operation_id: accepted.id } : {}), status: state.planStatus || (state.planMode === 'async' ? 'planning' : 'ready'), created_at: new Date().toISOString(),
        ...(state.planStatus === 'failed' ? { error: { code: 'APPLICATION_PLAN_INTERRUPTED', message: '서버 재시작으로 계획 확인이 중단됐습니다.' } } : {}) });
    }
    const mutation = /^\/api\/v1\/applications\/([^/]+)\/operations$/.exec(path);
    if (mutation) {
      if (state.outcome === 'rejected') return send(response, 409, { error: { code: 'APPLICATION_PLAN_STALE', message: '계획 만료', outcome_unknown: false } });
      state.writes.push({ application_id: mutation[1], input, key: request.headers['idempotency-key'] });
      const id = `operation-${state.writes.length}`;
      const operation = { id, application_id: mutation[1], action: input.action, plan_id: input.plan_id, status: 'queued', stage: 'accepted', steps: [], residuals: [] };
      state.operations.set(id, operation);
      if (state.outcome === 'lost') { response.writeHead(202, { 'content-type': 'application/json' }); return response.end('{'); }
      return send(response, 202, operation, `/api/v1/operations/${id}`);
    }
    if (path.startsWith('/api/v1/operations/')) {
      const operation = state.operations.get(path.split('/').at(-1));
      if (!operation) return send(response, 404, { error: 'unknown operation' });
      const app = state.applications.find((app) => app.id === operation.application_id);
      if (state.outcome === 'succeeded') app.status = { stop: 'stopped', start: 'ready', delete: 'deleted' }[operation.action];
      return send(response, 200, { ...operation, status: state.outcome, stage: state.outcome === 'succeeded' ? 'verified' : 'cleanup',
        steps: [{ name: '배포 중단 확인', status: 'succeeded' }],
        residuals: ['unknown', 'blocked'].includes(state.outcome) ? [{ kind: 'PersistentVolumeClaim', namespace: 'app-private', name: 'database' }] : [] });
    }
    const files = { '/': ['index.html', 'text/html'], '/app.js': ['app.js', 'text/javascript'], '/styles.css': ['styles.css', 'text/css'],
      '/src/api.js': ['src/api.js', 'text/javascript'], '/src/openstack-installer.js': ['src/openstack-installer.js', 'text/javascript'],
      '/src/insights.js': ['src/insights.js', 'text/javascript'],
      '/src/insights-view.js': ['src/insights-view.js', 'text/javascript'],
      '/src/deployment-history.js': ['src/deployment-history.js', 'text/javascript'],
      '/src/recovery.js': ['src/recovery.js', 'text/javascript'],
      '/src/lifecycle.js': ['src/lifecycle.js', 'text/javascript'],
      '/contracts/application.mjs': ['../../contracts/application.mjs', 'text/javascript'] };
    if (!files[path]) return send(response, 404, { error: 'Fixture path unavailable' });
    response.writeHead(200, { 'content-type': files[path][1] });
    response.end(await readFile(new URL('../../apps/dashboard/' + files[path][0], import.meta.url)));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined });
  const context = await browser.newContext({ viewport: { width: 1280, height: 960 }, serviceWorkers: 'block' });
  const page = await context.newPage(); page.setDefaultTimeout(8000);
  page.on('pageerror', (error) => state.errors.push(error.message));
  await context.route('**/*', (route) => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
  t.after(async () => { await browser.close(); await new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); }); });
  await page.goto(origin);
  await page.waitForFunction(() => document.querySelector('#applications-list li')?.textContent.includes('my-app'));
  return { state, page, origin };
}
const appAction = (page, name, action) => page.locator('#applications-list').getByRole('button', { name: new RegExp(`^${name} ${action}`) });

test('a failed lifecycle read retains execution state and retries reads without another mutation', { timeout: 45000 }, async t => {
  const { state, page } = await fixture(t);
  state.outcome = 'running';
  await page.evaluate(() => {
    const original = window.setTimeout.bind(window);
    window.setTimeout = (fn, delay, ...args) => original(fn, delay === 15000 ? 200 : delay, ...args);
  });
  await appAction(page, 'my-app', '중지').click();
  await page.getByRole('button', { name: '앱 중지', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#lifecycle-operation-state').textContent.startsWith('실행 중'));
  let failed = false;
  await page.route('**/api/v1/operations/*', route => {
    if (failed) return route.fallback();
    failed = true;
    return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { message: 'temporary read failure' } }) });
  });
  await page.locator('#lifecycle-operation-refresh').click();
  await page.waitForFunction(() => document.querySelector('#lifecycle-operation-message').textContent.includes('마지막 확인 상태'));
  assert.match(await page.locator('#lifecycle-operation-state').innerText(), /^실행 중/);
  const saved = await page.evaluate(() => JSON.parse(sessionStorage.getItem('railshot.application-operation')));
  assert.equal(saved.status, 'running');
  state.outcome = 'succeeded';
  await page.waitForFunction(() => document.querySelector('#lifecycle-operation-state').textContent.startsWith('완료'));
  assert.equal(state.writes.length, 1); assert.deepEqual(state.errors, []);
});

test('session app controls stop and resume through fresh plans; native dialog cancels and restores focus', { timeout: 45000 }, async (t) => {
  const { state, page } = await fixture(t);
  assert.equal(await appAction(page, 'my-app', '재개').isDisabled(), true);
  assert.equal(await appAction(page, 'paused-app', '중지').isDisabled(), true);
  await page.evaluate(() => {
    const schedule = window.setTimeout.bind(window);
    window.planTimeouts = [];
    window.setTimeout = (callback, delay, ...args) => {
      window.planTimeouts.push(delay);
      return schedule(callback, delay, ...args);
    };
  });
  await appAction(page, 'my-app', '중지').click();
  await page.waitForFunction(() => !document.querySelector('#lifecycle-confirm').disabled);
  const timeouts = await page.evaluate(() => window.planTimeouts);
  assert.ok(timeouts.includes(15000), 'async application planning only waits for admission and short status reads');
  assert.ok(!timeouts.includes(600000), 'the browser no longer holds one ten-minute plan response');
  assert.ok(!timeouts.includes(120000), 'a plan must not abort at the normal POST deadline');
  assert.match(await page.locator('#lifecycle-description').innerText(), /데이터와 스토리지는 보존/);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#lifecycle-dialog').isVisible(), false);
  await page.waitForFunction(() => document.activeElement.getAttribute('aria-label') === 'my-app 중지');
  assert.equal(state.writes.length, 0);
  const gets = state.gets;
  await appAction(page, 'my-app', '중지').click();
  await page.getByRole('button', { name: '앱 중지', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#lifecycle-operation-state').textContent.startsWith('완료'));
  assert.ok(state.gets >= gets + 3, 'read fresh list before preview, submission and after completion');
  assert.deepEqual(state.writes[0].input, { action: 'stop', plan_id: '11111111-1111-4111-8111-111111111111', plan_hash: 'a'.repeat(64), confirmation: 'my-app' });
  assert.match(state.writes[0].key, /^[a-f0-9-]{36}$/);
  await appAction(page, 'my-app', '재개').click();
  await page.getByRole('button', { name: '앱 재개', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#lifecycle-operation-title').textContent.includes('재개') && document.querySelector('#lifecycle-operation-state').textContent.startsWith('완료'));
  assert.equal(state.writes.length, 2); assert.equal(state.writes[1].input.action, 'start');
  assert.notEqual(state.writes[0].key, state.writes[1].key);
  assert.deepEqual(state.errors, []);
});

test('running deployment trash requires a second permanent-delete click and shows shared resources retained', { timeout: 45000 }, async (t) => {
  const { state, page } = await fixture(t);
  const history = page.locator('#history-list');
  for (const summary of await history.locator('.dh-card-extra > summary').all()) await summary.click();
  assert.match(await history.innerText(), /앱 관리 ID.*자동 삭제를 지원하지/);
  await history.getByRole('button', { name: /^building-app 삭제/ }).click();
  await page.waitForFunction(() => !document.querySelector('#lifecycle-confirm').disabled);
  assert.match(await page.locator('#lifecycle-description').innerText(), /데이터도 영구 삭제/);
  assert.match(await page.locator('#lifecycle-description').innerText(), /진행 중인 배포.*중단/);
  assert.match(await page.locator('#lifecycle-retained').innerText(), /shared-node/);
  assert.match(await page.locator('#lifecycle-retained').innerText(), /shared-lb/);
  assert.equal(await page.locator('#lifecycle-dialog input').count(), 0);
  assert.equal(await page.locator('#lifecycle-dialog button:visible').count(), 2);
  assert.equal(state.writes.length, 0, 'trash click only prepares a plan');
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement.id), 'lifecycle-confirm');
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement.id), 'lifecycle-cancel', 'native dialog traps keyboard focus');
  const output = process.env.CI_OUTPUT_DIR;
  if (output) { await mkdir(output, { recursive: true }); await page.screenshot({ path: join(output, 'application-delete-desktop.png'), fullPage: true }); }
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  if (output) await page.screenshot({ path: join(output, 'application-delete-mobile.png'), fullPage: true });
  await page.getByRole('button', { name: '영구 삭제', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#lifecycle-operation-state').textContent.startsWith('완료'));
  assert.equal(state.writes.length, 1);
  assert.equal(state.writes[0].application_id, 'app-building');
  assert.equal(state.writes[0].input.confirmation, 'building-app');
  assert.equal(state.writes[0].input.delete_data, true);
  assert.equal(await appAction(page, 'building-app', '삭제').count(), 0);
  await page.reload(); await page.waitForFunction(() => document.querySelector('#applications-message').textContent.includes('2개'));
  assert.equal(state.writes.length, 1, 'reload only observes the accepted operation');
  assert.deepEqual(state.errors, []);
});

test('expired and foreign plans cannot execute; lost replies recover from the saved plan after reload without another write', { timeout: 45000 }, async (t) => {
  const { state, page } = await fixture(t);
  for (const planMode of ['expired', 'foreign']) {
    state.planMode = planMode;
    await appAction(page, 'my-app', '삭제').click();
    await page.waitForFunction(() => !document.querySelector('#lifecycle-error').hidden);
    assert.equal(await page.locator('#lifecycle-confirm').isDisabled(), true);
    await page.getByRole('button', { name: '취소', exact: true }).click();
  }
  assert.equal(state.writes.length, 0);
  state.planMode = 'valid'; state.outcome = 'unknown';
  await appAction(page, 'my-app', '삭제').click();
  await page.getByRole('button', { name: '영구 삭제', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#lifecycle-operation-state').textContent.includes('결과 확인 필요'));
  assert.match(await page.locator('#lifecycle-residual-list').innerText(), /app-private\/database/);
  assert.equal(await appAction(page, 'my-app', '삭제').isDisabled(), true);
  await page.locator('#lifecycle-operation-refresh').click();
  assert.equal(state.writes.length, 1);
  state.outcome = 'lost';
  await appAction(page, 'paused-app', '삭제').click();
  await page.getByRole('button', { name: '영구 삭제', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#lifecycle-operation-message').textContent.includes('상태 조회 실패'));
  assert.equal(await appAction(page, 'paused-app', '삭제').isDisabled(), true);
  assert.equal(await page.locator('#lifecycle-operation-refresh').isEnabled(), true);
  state.outcome = 'succeeded';
  await page.reload(); await page.waitForFunction(() => document.querySelector('#lifecycle-operation-state').textContent.startsWith('완료'));
  assert.equal(await appAction(page, 'paused-app', '삭제').count(), 0);
  assert.equal(state.writes.length, 2);
  assert.deepEqual(state.errors, []);
});

test('a changed app blocks submission and an in-progress management operation disables further actions', { timeout: 45000 }, async (t) => {
  const { state, page } = await fixture(t);
  await appAction(page, 'my-app', '삭제').click();
  await page.waitForFunction(() => !document.querySelector('#lifecycle-confirm').disabled);
  state.applications[0].status = 'unknown';
  await page.getByRole('button', { name: '영구 삭제', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#lifecycle-error').textContent.includes('앱 상태가 바뀌었'));
  assert.equal(state.writes.length, 0);
  await page.getByRole('button', { name: '취소', exact: true }).click();
  state.applications[0].status = 'ready'; state.outcome = 'running';
  await page.locator('#applications-refresh').click();
  await appAction(page, 'my-app', '삭제').click();
  await page.getByRole('button', { name: '영구 삭제', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#lifecycle-operation-state').textContent.startsWith('실행 중'));
  assert.equal(await appAction(page, 'my-app', '삭제').isDisabled(), true);
  assert.equal(await appAction(page, 'paused-app', '재개').isDisabled(), true);
  state.outcome = 'blocked';
  await page.locator('#lifecycle-operation-refresh').click();
  await page.waitForFunction(() => document.querySelector('#lifecycle-operation-state').textContent.startsWith('실행 차단'));
  assert.match(await page.locator('#lifecycle-operation-message').innerText(), /완료로 확인되지/);
  assert.equal(await appAction(page, 'my-app', '삭제').isDisabled(), true);
  assert.equal(state.writes.length, 1);
  assert.deepEqual(state.errors, []);
});

test('failed first deployment is not running and only deletion is enabled', { timeout: 45000 }, async (t) => {
  const { state, page } = await fixture(t);
  Object.assign(state.applications[0], { current_deployment_state: 'not_deployed', current_deployment: null,
    latest_deployment: { id: 'failed-first', status: 'failed' } });
  await page.getByRole('button', { name: '앱 목록 새로고침' }).click();
  await page.waitForFunction(() => document.querySelector('#applications-list').textContent.includes('배포 실패'));
  assert.equal(await appAction(page, 'my-app', '중지').isDisabled(), true);
  assert.equal(await appAction(page, 'my-app', '재개').isDisabled(), true);
  assert.equal(await appAction(page, 'my-app', '삭제').isEnabled(), true);
  assert.equal(state.writes.length, 0);
});

test('uncertain published delivery offers a fresh delete plan without stop or start controls', { timeout: 45000 }, async (t) => {
  const { state, page } = await fixture(t);
  Object.assign(state.applications[0], { current_deployment_state: 'unverified', current_deployment: null,
    latest_deployment: { id: 'missing-cd', status: 'blocked', error: { code: 'DEPLOYMENT_NOT_FOUND', outcome_unknown: true } } });
  await page.getByRole('button', { name: '앱 목록 새로고침' }).click();
  await page.waitForFunction(() => document.querySelector('#applications-list').textContent.includes('배포 결과 확인 필요'));
  assert.equal(await appAction(page, 'my-app', '중지').isDisabled(), true);
  assert.equal(await appAction(page, 'my-app', '재개').isDisabled(), true);
  await appAction(page, 'my-app', '삭제').click();
  await page.waitForFunction(() => !document.querySelector('#lifecycle-confirm').disabled);
  assert.equal(state.writes.length, 0);
  assert.match(await page.locator('#lifecycle-retained').innerText(), /shared-node/);
  await page.getByRole('button', { name: '영구 삭제', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#lifecycle-operation-state').textContent.startsWith('완료'));
  assert.equal(await appAction(page, 'my-app', '삭제').count(), 0);
  assert.equal(state.writes.length, 1); assert.deepEqual(state.errors, []);
});

test('definitive admission rejection allows a fresh plan without a permanent unknown lock', { timeout: 45000 }, async (t) => {
  const { state, page } = await fixture(t);
  state.outcome = 'rejected';
  await appAction(page, 'my-app', '삭제').click();
  await page.getByRole('button', { name: '영구 삭제', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#lifecycle-error').textContent.includes('접수되지 않았습니다'));
  await page.getByRole('button', { name: '취소', exact: true }).click();
  assert.equal(await appAction(page, 'my-app', '삭제').isEnabled(), true);
  assert.equal(state.writes.length, 0);
  state.outcome = 'succeeded';
  await appAction(page, 'my-app', '삭제').click();
  await page.getByRole('button', { name: '영구 삭제', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#lifecycle-operation-state').textContent.startsWith('완료'));
  assert.equal(await appAction(page, 'my-app', '삭제').count(), 0);
});

test('slow delete plan shows progress and survives closing and reloading without another POST', { timeout: 45000 }, async (t) => {
  const { state, page, origin } = await fixture(t); state.planMode = 'async';
  await appAction(page, 'my-app', '삭제').click();
  await page.waitForFunction(() => document.querySelector('#lifecycle-description').textContent.includes('서버에서'));
  assert.equal(await page.locator('#lifecycle-confirm').isDisabled(), true);
  assert.match(await page.locator('#lifecycle-resources').innerText(), /확인 후 표시/);
  await page.waitForFunction(() => !document.querySelector('#lifecycle-progress').hidden);
  assert.match(await page.locator('#lifecycle-progress').innerText(), /초 경과/);
  if (process.env.RAILSHOT_SCREENSHOT_DIR) {
    await mkdir(process.env.RAILSHOT_SCREENSHOT_DIR, { recursive: true });
    await page.screenshot({ path: join(process.env.RAILSHOT_SCREENSHOT_DIR, 'delete-plan-pending.png'), fullPage: true });
  }
  await page.getByRole('button', { name: '취소', exact: true }).click();
  await page.goto(origin); await page.waitForFunction(() => document.querySelector('#applications-list li')?.textContent.includes('my-app'));
  await appAction(page, 'my-app', '삭제').click();
  await page.waitForFunction(() => document.querySelector('#lifecycle-description').textContent.includes('서버에서'));
  assert.equal(state.plans.length, 1); assert.equal(state.writes.length, 0);
  state.planStatus = 'ready';
  await page.waitForFunction(() => !document.querySelector('#lifecycle-confirm').disabled);
  assert.match(await page.locator('#lifecycle-retained').innerText(), /shared-lb/);
  await page.getByRole('button', { name: '영구 삭제', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#lifecycle-operation-state').textContent.startsWith('완료'));
  assert.equal(state.writes.length, 1);
});

test('failed plan shows a reason and allows a fresh preview while read failure retries the same plan', { timeout: 45000 }, async (t) => {
  const { state, page } = await fixture(t); state.planMode = 'async'; state.planReadError = true;
  await appAction(page, 'my-app', '삭제').click();
  await page.waitForFunction(() => !document.querySelector('#lifecycle-retry').hidden);
  assert.match(await page.locator('#lifecycle-error').innerText(), /연결 실패/);
  assert.equal(await page.locator('#lifecycle-confirm').isDisabled(), true);
  state.planReadError = false; state.planStatus = 'failed';
  await page.getByRole('button', { name: '계획 다시 확인' }).click();
  await page.waitForFunction(() => document.querySelector('#lifecycle-error').textContent.includes('APPLICATION_PLAN_INTERRUPTED'));
  assert.equal(state.plans.length, 1); assert.equal(state.writes.length, 0);
  if (process.env.RAILSHOT_SCREENSHOT_DIR) await page.screenshot({ path: join(process.env.RAILSHOT_SCREENSHOT_DIR, 'delete-plan-retry.png'), fullPage: true });
  state.planStatus = 'ready';
  await page.getByRole('button', { name: '계획 다시 확인' }).click();
  await page.waitForFunction(() => !document.querySelector('#lifecycle-confirm').disabled);
  assert.equal(state.plans.length, 2); assert.equal(state.writes.length, 0);
});

test('cancel during inventory refresh never sends a hidden plan request', { timeout: 45000 }, async (t) => {
  const { state, page } = await fixture(t); let finish;
  await page.route('**/api/v1/applications?*', async (route) => {
    await new Promise((resolve) => { finish = resolve; }); await route.continue();
  });
  await appAction(page, 'my-app', '삭제').click();
  await page.getByRole('button', { name: '취소', exact: true }).click();
  while (!finish) await new Promise((resolve) => setTimeout(resolve, 5));
  finish(); await page.waitForFunction(() => document.querySelector('#applications-list li')?.textContent.includes('my-app'));
  assert.equal(state.plans.length, 0); assert.equal(state.writes.length, 0);
});

test('proxy HTML failure is readable and retryable without treating an uncertain delete as rejected', { timeout: 45000 }, async (t) => {
  const { state, page } = await fixture(t); page.setDefaultTimeout(20000); let unavailable = true;
  await page.route('**/api/v1/applications/app-ready/plans', (route) => unavailable
    ? route.fulfill({ status: 502, contentType: 'text/html', body: '<html>Bad Gateway</html>' }) : route.fallback());
  await appAction(page, 'my-app', '삭제').click();
  await page.waitForFunction(() => !document.querySelector('#lifecycle-retry').hidden);
  assert.match(await page.locator('#lifecycle-error').innerText(), /서버 응답.*HTTP 502/);
  assert.equal(await page.locator('#lifecycle-confirm').isDisabled(), true);
  assert.equal(state.writes.length, 0); unavailable = false;
  await page.getByRole('button', { name: '계획 다시 확인' }).click();
  await page.waitForFunction(() => !document.querySelector('#lifecycle-confirm').disabled);
  await page.route('**/api/v1/applications/app-ready/operations', (route) =>
    route.fulfill({ status: 502, contentType: 'text/html', body: '<html>Bad Gateway</html>' }));
  await page.getByRole('button', { name: '영구 삭제', exact: true }).click();
  await page.waitForFunction(() => !document.querySelector('#lifecycle-dialog').open);
  assert.match(await page.locator('#lifecycle-operation-message').innerText(), /HTTP 502|접수된 실행/);
  assert.equal(await appAction(page, 'my-app', '삭제').isDisabled(), true);
  assert.equal(state.writes.length, 0);
});

 test('brief API replacement is transparent to plan creation and keyed deletion', { timeout: 30000 }, async (t) => {
  const { state, page } = await fixture(t); let plans = 0; const attempts = [];
  await page.route('**/api/v1/applications/app-ready/plans', (route) => ++plans === 1
    ? route.fulfill({ status: 502, contentType: 'text/html', body: '<html>Bad Gateway</html>' }) : route.fallback());
  await appAction(page, 'my-app', '삭제').click();
  await page.waitForFunction(() => !document.querySelector('#lifecycle-confirm').disabled);
  assert.equal(plans, 2); assert.equal(await page.locator('#lifecycle-retry').isHidden(), true);
  await page.route('**/api/v1/applications/app-ready/operations', (route) => {
    attempts.push({ body: route.request().postData(), key: route.request().headers()['idempotency-key'] });
    return attempts.length === 1
      ? route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: {
        code: 'PLATFORM_UPDATING', outcome_unknown: false, retryable: true,
      } }) }) : route.fallback();
  });
  await page.getByRole('button', { name: '영구 삭제', exact: true }).click();
  await page.waitForFunction(() => !document.querySelector('#lifecycle-dialog').open);
  assert.equal(attempts.length, 2); assert.ok(attempts[0].key);
  assert.deepEqual(attempts[0], attempts[1]); assert.equal(state.writes.length, 1);
});

 test('long native inventories keep confirmation and cancel visible on desktop and mobile', async (t) => {
  const { state, page } = await fixture(t);
  state.resources = Array.from({ length: 40 }, (_, i) => ({ kind: 'Service', namespace: 'app-private', name: `owned-resource-${i}` }));
  for (const viewport of [{ width: 1280, height: 960 }, { width: 390, height: 844 }, { width: 844, height: 390 }]) {
    await page.setViewportSize(viewport);
    await appAction(page, 'my-app', '삭제').click();
    await page.waitForFunction(() => !document.querySelector('#lifecycle-confirm').disabled);
    const layout = await page.evaluate(() => {
      const button = document.querySelector('#lifecycle-confirm').getBoundingClientRect();
      const content = document.querySelector('.lifecycle-plan-content');
      return { buttonTop: button.top, buttonBottom: button.bottom, viewportHeight: innerHeight,
        contentHeight: content.clientHeight, scrollHeight: content.scrollHeight };
    });
    assert.ok(layout.buttonTop >= 0 && layout.buttonBottom <= layout.viewportHeight, JSON.stringify(layout));
    assert.ok(layout.contentHeight > 0 && layout.scrollHeight > layout.contentHeight);
    await page.getByRole('button', { name: '취소', exact: true }).click();
    assert.equal(state.writes.length, 0);
  }
});

 test('measured handover delay stays within the plan request deadline without surfacing an error', { timeout: 30000 }, async (t) => {
  const { page } = await fixture(t); page.setDefaultTimeout(20000); let attempts = 0;
  await page.route('**/api/v1/applications/app-ready/plans', async (route) => {
    if (++attempts > 4) return route.fallback();
    await new Promise((resolve) => setTimeout(resolve, 2000));
    return route.fulfill({ status: 502, contentType: 'text/html', body: '<html>Bad Gateway</html>' });
  });
  await appAction(page, 'my-app', '삭제').click();
  await page.waitForFunction(() => !document.querySelector('#lifecycle-confirm').disabled);
  assert.equal(attempts, 5); assert.equal(await page.locator('#lifecycle-retry').isHidden(), true);
  await page.getByRole('button', { name: '취소', exact: true }).click();
});

test('read-only lifecycle polling does not disable another app and confirmed failure can be retried', async (t) => {
  const { state, page } = await fixture(t);
  state.outcome = 'failed';
  await appAction(page, 'my-app', '중지').click();
  await page.getByRole('button', { name: '앱 중지', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#lifecycle-operation-state').textContent.startsWith('실행 실패'));
  assert.equal(await appAction(page, 'my-app', '중지').isEnabled(), true);
  let release; const hold = new Promise(resolve => { release = resolve; });
  await page.route('**/api/v1/operations/*', async route => { await hold; await route.fallback(); });
  await page.locator('#lifecycle-operation-refresh').click();
  assert.equal(await appAction(page, 'paused-app', '재개').isEnabled(), true);
  release();
  assert.equal(state.writes.length, 1);
});
