import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdir, mkdtemp, readFile, writeFile, rm, realpath } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { chromium } from 'playwright';
import { createAppServer } from '../../apps/api/src/server.js';
import { apiAccessConfig } from '../../apps/api/src/access.js';
import { archiveFromPath } from '../../apps/api/src/client.js';
import { inspectArchive } from '../../apps/api/src/archive.js';

async function start(t, options) {
  const stateDirectory = await realpath(await mkdtemp(join(tmpdir(), 'railshot-browser-')));
  const server = createAppServer({ stateDirectory, pollInterval: 10, ...options });
  await server.productReady;
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const origin = `http://127.0.0.1:${server.address().port}`;
  if (options.access?.publicDemo) options.access.allowedOrigins.add(origin);
  const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined });
  const context = await browser.newContext({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage(); page.setDefaultTimeout(10000);
  const errors = [], requests = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await context.route('**/*', async (route) => {
    const request = route.request(), url = new URL(request.url());
    requests.push({ method: request.method(), path: url.pathname, authorization: request.headers().authorization });
    if (url.origin !== origin) { errors.push(`Unexpected remote request ${url.origin}`); await route.abort(); }
    else await route.continue();
  });
  await context.routeWebSocket('**/*', (socket) => { errors.push('Unexpected WebSocket'); socket.close(); });
  t.after(async () => {
    await browser.close();
    await new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); });
    await (await server.productReady)?.close?.();
    await rm(stateDirectory, { recursive: true, force: true });
  });
  return { page, origin, errors, requests, stateDirectory };
}

test('dashboard loads without credentials, offers no login and blocks unconfigured execution', { timeout: 45000 }, async (t) => {
  const { page, origin, errors, requests } = await start(t, { service: null });
  await page.goto(origin);
  await page.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
  assert.equal(await page.locator('#api-token').count(), 0);
  assert.equal(await page.locator('#deploy-view input[type="password"]:not(#openstack-enrollment-key)').count(), 0);
  await page.locator('#deploy-form button[type="submit"]').click();
  assert.match(await page.locator('#form-error').innerText(), /소스를 선택/);
  await page.locator('#repository-url').fill('https://example.invalid/app');
  await page.locator('#deploy-form button[type="submit"]').click();
  assert.match(await page.locator('#form-error').innerText(), /GitHub 저장소 URL/);
  await page.locator('#repository-url').fill('https://github.com/example/demo');
  await page.locator('#deploy-form button[type="submit"]').click();
  assert.match(await page.locator('#form-error').innerText(), /앱 배포 설정이 아직 준비되지/);
  assert.equal(await page.locator('#deploy-button').isDisabled(), true);
  await page.locator('[data-view="history"]').click();
  assert.equal(await page.locator('#history-view').isVisible(), true);
  await page.locator('[data-view="monitor"]').click();
  assert.equal(await page.locator('#monitor-view').isVisible(), true);
  await page.locator('[data-console="app"]').click();
  assert.match(await page.locator('#console-output').innerText(), /실행을 시작/);
  assert.equal(requests.some((request) => request.method === 'POST' && request.path !== '/api/v1/sessions'), false);
  assert.deepEqual(errors, []);
});

test('verified app URL is visible in inventory and detail, survives a failed update and disappears after stop or uncertain rollout', { timeout: 45000 }, async (t) => {
  const { page, origin, errors } = await start(t, { service: null });
  const deployed = { id: 'deployed-v1', status: 'succeeded', cd: { deployed: true, revision: 'a'.repeat(40) },
    public_http: { state: 'succeeded', verified_at: '2026-10-03T06:00:00Z', url: 'https://calculator.example/health', site_url: 'https://calculator.example/' } };
  const app = { id: 'calculator-id', app: 'calculator', target_id: 'aws-runtime', status: 'ready',
    current_deployment_state: 'verified', current_deployment: deployed, latest_deployment: { id: 'failed-v2', status: 'failed' } };
  const json = (route, data) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) });
  await page.route('**/api/v1/applications?*', (route) => json(route, { items: [app] }));
  await page.route('**/api/v1/applications/calculator-id', (route) => json(route, app));
  await page.goto(origin);
  await page.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
  await page.locator('[data-view="history"]').click();
  const link = page.locator('#applications-list').getByRole('link', { name: 'calculator 배포한 앱 열기' });
  await link.waitFor();
  assert.equal(await link.getAttribute('href'), 'https://calculator.example/');
  assert.equal(await link.getAttribute('target'), '_blank');
  assert.match(await link.getAttribute('rel'), /noopener/);
  assert.match(await page.locator('#applications-list').innerText(), /https:\/\/calculator.example\//);
  await page.getByRole('button', { name: 'calculator 앱 상세·업데이트' }).click();
  await page.locator('#detail-application-site a').waitFor();
  assert.equal(await page.locator('#detail-application-site a').getAttribute('href'), 'https://calculator.example/');
  if (process.env.CI_OUTPUT_DIR) {
    await mkdir(process.env.CI_OUTPUT_DIR, { recursive: true });
    await page.screenshot({ path: join(process.env.CI_OUTPUT_DIR, 'deployed-app-links-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: join(process.env.CI_OUTPUT_DIR, 'deployed-app-links-mobile.png'), fullPage: true });
  }
  for (const patch of [{ status: 'stopped' }, { status: 'deleted' }, { status: 'ready', current_deployment_state: 'unverified' },
    { current_deployment_state: 'not_deployed' }, { current_deployment_state: 'verified', current_deployment: { ...deployed, public_http: { ...deployed.public_http, site_url: 'javascript:alert(1)' } } }]) {
    Object.assign(app, patch);
    await page.locator('#applications-refresh').click();
    await page.waitForFunction(() => document.querySelector('#applications-list').getAttribute('aria-busy') === 'false');
    assert.equal(await page.locator('#applications-list a, #detail-application-site a').count(), 0);
    if (app.status === 'deleted') assert.equal(await page.locator('#application-detail').isVisible(), false);
  }
  assert.deepEqual(errors, []);
});

test('execution links require the latest owned active service and disappear on stop, delete, replacement or inventory failure', { timeout: 45000 }, async (t) => {
  const { page, origin, errors } = await start(t, { service: null });
  const deployment = { id: 'deployed-v1', application_id: 'calculator-id', app: 'calculator', target_id: 'aws-runtime', status: 'succeeded',
    cd: { deployed: true, revision: 'a'.repeat(40) },
    public_http: { state: 'succeeded', verified_at: '2026-10-03T06:00:00Z', site_url: 'https://calculator.example/' } };
  const app = { id: 'calculator-id', app: 'calculator', status: 'ready', current_deployment_state: 'verified', current_deployment: deployment };
  let failInventory = false;
  const json = (route, data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
  await page.route('**/api/v1/applications?*', (route) => failInventory
    ? json(route, { error: { message: 'Unavailable' } }, 503) : json(route, { items: [app] }));
  await page.route('**/api/v1/deployments?*', (route) => json(route, { items: [deployment] }));
  await page.route('**/api/v1/deployments/deployed-v1', (route) => json(route, deployment));
  await page.goto(origin);
  const links = ['#application-link', '#monitor-application-link'];
  await page.waitForFunction(() => ['#application-link', '#monitor-application-link']
    .every(selector => document.querySelector(selector).getAttribute('href') === 'https://calculator.example/'));
  for (const patch of [{ status: 'stopped' }, { status: 'deleted' },
    { status: 'ready', current_deployment: { ...deployment, id: 'deployed-v2' } },
    { current_deployment: deployment, current_deployment_state: 'unverified' }]) {
    Object.assign(app, patch);
    await page.locator('[data-view="history"]').click();
    await page.locator('#applications-refresh').click();
    await page.waitForFunction(() => document.querySelector('#applications-list').getAttribute('aria-busy') === 'false');
    for (const selector of links) assert.equal(await page.locator(selector).getAttribute('href'), null);
    assert.match(await page.locator('#history-list').textContent(), /앱 배포 완료/, 'historical success stays visible');
  }
  app.current_deployment_state = 'verified';
  await page.locator('#applications-refresh').click();
  await page.waitForFunction(() => document.querySelector('#application-link').hasAttribute('href'));
  failInventory = true;
  await page.locator('#applications-refresh').click();
  await page.waitForFunction(() => document.querySelector('#applications-message').textContent.includes('조회 실패'));
  for (const selector of links) assert.equal(await page.locator(selector).getAttribute('href'), null);
  // Restoring a previously successful run must still consult current inventory on reload.
  failInventory = false; app.status = 'deleted';
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료');
  for (const selector of links) assert.equal(await page.locator(selector).getAttribute('href'), null);
  assert.deepEqual(errors, []);
});

test('application detail keeps update and lifecycle controls while active deployments beyond the first app page remain manageable', { timeout: 45000 }, async (t) => {
  const { page, origin, errors } = await start(t, { service: null });
  const baseline = { id: 'stable-deployment', app: 'stable-app', target_id: 'shared-target', status: 'succeeded' };
  const stable = { id: 'stable-application', app: 'stable-app', target_id: 'shared-target', status: 'ready',
    current_deployment_state: 'verified', current_deployment: baseline, latest_deployment: baseline };
  const building = { id: 'building-application', app: 'building-app', target_id: 'shared-target', status: 'queued' };
  const deployment = { id: 'active-deployment', application_id: building.id, app: building.app,
    target_id: building.target_id, status: 'running', stage: 'ci', steps: [] };
  const writes = [];
  const previews = [];
  const operation = { id: 'manage-stable', application_id: stable.id, action: 'stop', status: 'running', steps: [], residuals: [] };
  const json = (route, data, status = 200, headers = {}) => route.fulfill({ status, contentType: 'application/json', headers, body: JSON.stringify(data) });
  await page.route('**/api/v1/applications?*', (route) => json(route, new URL(route.request().url()).searchParams.has('marker')
    ? { items: [building], next_marker: null } : { items: [stable], next_marker: 'next-page' }));
  await page.route('**/api/v1/applications/stable-application', (route) => json(route, stable));
  await page.route('**/api/v1/applications/stable-application/updates', (route) => {
    previews.push(route.request().headers()['idempotency-key']);
    return json(route, { id: 'stable-preview', application_id: stable.id, app: stable.app, target_id: stable.target_id,
      status: 'preview', base_deployment_id: baseline.id, expires_at: '2099-01-01T00:00:00Z', baseline_kind: 'deployed',
      changes: { added: [], modified: ['app.js'], deleted: [], unchanged: 0 }, no_changes: false },
    200, { location: '/api/v1/deployments/stable-preview' });
  });
  await page.route('**/api/v1/deployments?*', (route) => json(route, { items: [deployment], next_marker: null }));
  await page.route('**/api/v1/deployments/active-deployment', (route) => json(route, deployment));
  await page.route('**/api/v1/applications/*/plans', (route) => {
    const id = new URL(route.request().url()).pathname.split('/').at(-2);
    return json(route, { id: '11111111-1111-4111-8111-111111111111', application_id: id,
      action: route.request().postDataJSON().action, plan_hash: 'a'.repeat(64), expires_at: '2099-01-01T00:00:00Z',
      resources: [{ kind: 'Namespace', name: id }], retained: [{ kind: 'Node', name: 'shared-node' }] });
  });
  await page.route('**/api/v1/applications/stable-application/operations', (route) => {
    writes.push(route.request().postDataJSON()); stable.status = 'stopping';
    return json(route, operation, 202, { location: '/api/v1/operations/manage-stable' });
  });
  await page.route('**/api/v1/operations/manage-stable', (route) => json(route, operation));
  await page.goto(origin); await page.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
  await page.locator('[data-view="history"]').click();
  await page.waitForFunction(() => document.querySelector('#applications-list').getAttribute('aria-busy') === 'false');
  assert.equal(await page.locator('#applications-list > li').count(), 1);
  const trash = page.locator('#history-list').getByRole('button', { name: /^building-app 삭제/ });
  await page.locator('#history-list .dh-card').filter({ hasText: 'building-app' }).locator('summary').click();
  await trash.click();
  await page.waitForFunction(() => !document.querySelector('#lifecycle-confirm').disabled);
  assert.match(await page.locator('#lifecycle-description').innerText(), /데이터도 영구 삭제.*진행 중인 배포/);
  assert.match(await page.locator('#lifecycle-retained').innerText(), /shared-node/);
  await page.getByRole('button', { name: '취소', exact: true }).click();
  assert.equal(writes.length, 0, 'opening or cancelling delete never executes');
  await page.locator('#applications-more').click();
  assert.equal(await page.locator('#applications-list > li').count(), 2);
  await page.getByRole('button', { name: 'stable-app 앱 상세·업데이트' }).click();
  await page.waitForFunction(() => !document.querySelector('#application-update').disabled);
  assert.equal(await page.locator('#detail-application-actions').getByRole('button').count(), 3);
  assert.ok(await page.locator('#application-versions').getByRole('button', { name: /최종 소스 다운로드/ }).count());
  await page.locator('#application-update').click();
  assert.equal(await page.locator('#update-context').isVisible(), true);
  assert.equal(await page.locator('#target-section').isVisible(), false);
  await page.locator('#repository-url').fill('https://github.com/example/stable-update');
  await page.locator('#deploy-form button[type="submit"]').click();
  await page.waitForFunction(() => !document.querySelector('#update-review').hidden);
  assert.equal(previews.length, 1, 'another app running must not block this app update preview');
  assert.equal(await page.locator('#update-identity').innerText(), 'stable-app');
  await page.locator('[data-view="history"]').click();
  await page.waitForFunction(() => document.querySelector('#applications-list').getAttribute('aria-busy') === 'false');
  await page.locator('#detail-application-actions').getByRole('button', { name: 'stable-app 중지', exact: true }).click();
  await page.getByRole('button', { name: '앱 중지', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#lifecycle-operation-state').textContent.startsWith('실행 중'));
  assert.equal(writes.length, 1); assert.equal(writes[0].action, 'stop');
  assert.equal(await page.locator('#application-update').isDisabled(), true);
  await page.locator('#history-list .dh-card').filter({ hasText: 'building-app' }).locator('summary').click();
  assert.equal(await page.locator('#history-list').getByRole('button', { name: /^building-app 삭제/ }).isDisabled(), true);
  assert.deepEqual(errors, []);
});

test('rejected admission stays unsubmitted while a lost response preserves the same deployment key', { timeout: 45000 }, async (t) => {
  const { page, origin, errors } = await start(t, { service: null });
  await page.route('**/api/v1/applications/resolve?*', (route) => route.fulfill({ contentType: 'application/json',
    body: JSON.stringify({ app: 'todomvc', environment_target_id: 'runtime-aws', application: null }) }));
  await page.route('**/api/v1/options', (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: [
    { id: 'cloud-aws', environment: 'cloud', provider: 'aws', available: true, label: '클라우드 · AWS' },
  ] }) }));
  let mode = 'busy'; const keys = [];
  await page.route('**/api/v1/deployments', (route) => {
    keys.push(route.request().headers()['idempotency-key']);
    if (mode === 'network') return route.abort('connectionreset');
    const error = mode === 'busy'
      ? { code: 'EXECUTOR_BUSY', message: '이번 요청은 실행 대기열에 추가되지 않았습니다.', outcome_unknown: false,
        admission: { scope: 'workspace', accepted: false, reason: 'reconciliation_required' } }
      : { code: 'INVALID_INPUT', message: '요청 조건을 확인하세요.', outcome_unknown: false };
    return route.fulfill({ status: mode === 'busy' ? 409 : 422, contentType: 'application/json', body: JSON.stringify({ error }) });
  });
  await page.goto(origin);
  await page.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
  await page.locator('#repository-url').fill('https://github.com/tastejs/todomvc');
  await page.locator('#deploy-form button[type="submit"]').click();
  await page.locator('#deploy-button').click();
  await page.waitForFunction(() => !document.querySelector('#request-error').hidden);
  assert.match(await page.locator('#request-error').innerText(), /운영자가 기존 작업의 결과를 확인/);
  assert.doesNotMatch(await page.locator('#request-error').innerText(), /서버에서 이미 처리/);
  assert.equal(await page.locator('#run-panel').isVisible(), false);
  if (process.env.CI_OUTPUT_DIR) {
    await mkdir(process.env.CI_OUTPUT_DIR, { recursive: true });
    await page.screenshot({ path: join(process.env.CI_OUTPUT_DIR, 'admission-rejected.png'), fullPage: true });
  }
  mode = 'network'; await page.locator('#deploy-button').click();
  await page.waitForFunction(() => document.querySelector('#request-error').textContent.includes('서버에서 이미 처리'));
  mode = 'invalid'; await page.locator('#deploy-button').click();
  await page.waitForFunction(() => document.querySelector('#request-error').textContent.includes('요청 조건을 확인'));
  assert.doesNotMatch(await page.locator('#request-error').innerText(), /서버에서 이미 처리/);
  assert.equal(keys.length, 3); assert.equal(new Set(keys).size, 1);
  assert.equal(await page.locator('#run-panel').isVisible(), false); assert.deepEqual(errors, []);
});

test('application updates keep app and environment fixed across all source formats, review diffs, and start the frozen preview', { timeout: 90000 }, async (t) => {
  const { page, origin, errors, stateDirectory } = await start(t, { service: null });
  const baseline = { id: 'deployed-v1', app: 'stable-app', target_id: 'same-target', status: 'succeeded', source_commit: 'a'.repeat(40) };
  const application = { id: 'application-1', app: 'stable-app', target_id: 'same-target', environment_target_id: 'aws-environment', status: 'ready', current_deployment_state: 'verified',
    current_deployment: baseline, latest_deployment: { ...baseline, id: 'failed-v2', status: 'failed' } };
  const previews = [], uploads = [], starts = []; let failPreview = true, rejectStart = true;
  const json = (route, data, status = 200, headers = {}) => route.fulfill({ status, contentType: 'application/json', headers, body: JSON.stringify(data) });
  await page.route('**/api/v1/applications?*', (route) => json(route, { items: [application], next_marker: null }));
  await page.route('**/api/v1/applications/application-1', (route) => json(route, application));
  await page.route('**/api/v1/applications/application-1/updates', async (route) => {
    const request = route.request();
    const form = await new Request('http://fixture', { method: 'POST', headers: { 'content-type': request.headers()['content-type'] }, body: request.postDataBuffer() }).formData();
    uploads.push({ key: request.headers()['idempotency-key'], keys: [...form.keys()], repository: form.get('repository_url'), paths: form.get('paths') });
    if (failPreview) { failPreview = false; return json(route, { error: { message: '미리보기 응답을 확인하지 못했습니다.' } }, 503); }
    const preview = { id: `preview-${previews.length + 1}`, application_id: application.id, app: application.app, target_id: application.target_id,
      status: 'preview', base_deployment_id: baseline.id, expires_at: '2099-01-01T00:00:00Z', baseline_kind: previews.length ? 'deployed' : 'submitted',
      changes: { added: previews.length ? [] : ['<img src=x onerror=alert(1)>.js'], modified: previews.length ? [] : ['index.js'], deleted: previews.length ? [] : ['old.js'], unchanged: 2 },
      no_changes: previews.length > 0, source_origin: form.has('repository_url') ? { repository: form.get('repository_url'), sha: 'b'.repeat(40) } : null };
    previews.push(preview); return json(route, preview, 200, { location: `/api/v1/deployments/${preview.id}` });
  });
  await page.route('**/api/v1/deployments/preview-*/start', async (route) => {
    const id = new URL(route.request().url()).pathname.split('/').at(-2), body = route.request().postDataJSON();
    starts.push({ id, body });
    if (rejectStart) { rejectStart = false; return json(route, { error: { message: '다른 실행을 확인한 뒤 다시 시작하세요.' } }, 409); }
    const preview = previews.find((row) => row.id === id);
    preview.status = preview.no_changes && !body.rebuild ? 'unchanged' : 'succeeded';
    return json(route, preview.status === 'unchanged' ? preview : { resource_id: id, status: 'accepted' }, preview.status === 'unchanged' ? 200 : 202,
      { location: `/api/v1/deployments/${id}` });
  });
  await page.route('**/api/v1/deployments/preview-*', (route) => json(route, previews.find((row) => row.id === new URL(route.request().url()).pathname.split('/').at(-1))));
  await page.route('**/api/v1/deployments/deployed-v1/source?variant=deployed', (route) => json(route, { error: { message: '이전 배포의 최종 소스가 보관되어 있지 않습니다.' } }, 404));
  await page.goto(origin); await page.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
  const open = async () => {
    await page.locator('[data-view="history"]').click();
    await page.getByRole('button', { name: 'stable-app 앱 상세·업데이트' }).click();
    await page.locator('#application-update').click();
    assert.equal(await page.locator('#target-section').isVisible(), false);
  };
  await page.locator('[data-view="history"]').click();
  await page.waitForFunction(() => document.querySelector('#applications-list').getAttribute('aria-busy') === 'false');
  assert.match(await page.locator('#applications-list').innerText(), /현재 서비스: deployed-v1/);
  assert.match(await page.locator('#applications-list').innerText(), /환경 aws-environment/);
  assert.doesNotMatch(await page.locator('#applications-list').innerText(), /환경 same-target/);
  assert.match(await page.locator('#applications-list').innerText(), /최근 시도: 실행 실패 · failed-v2/);
  await page.getByRole('button', { name: 'stable-app 앱 상세·업데이트' }).click();
  await page.waitForFunction(() => document.querySelector('#application-detail-message').textContent.includes('앱 ID application-1'));
  assert.match(await page.locator('#application-detail-message').innerText(), /환경 aws-environment · 앱 ID application-1/);
  await page.getByRole('button', { name: 'stable-app deployed-v1 최종 소스 다운로드' }).click();
  await page.waitForFunction(() => document.querySelector('#application-versions').textContent.includes('최종 소스가 보관되어 있지'));
  await page.locator('#application-update').click();
  await page.locator('#repository-url').fill('https://github.com/example/different-repository');
  const review = () => page.locator('#deploy-form button[type="submit"]').click();
  await review(); await page.waitForFunction(() => !document.querySelector('#form-error').hidden);
  await review(); await page.waitForFunction(() => !document.querySelector('#update-review').hidden);
  assert.equal(uploads[0].key, uploads[1].key, 'preview failure retries the same request key');
  assert.equal(await page.locator('#update-identity').innerText(), 'stable-app');
  assert.equal(await page.locator('#deploy-form').isVisible(), false, 'review replaces the source form');
  assert.equal(await page.locator('#run-panel').isVisible(), false, 'previous execution cannot look like this update result');
  assert.equal(await page.locator('#update-source-details').getAttribute('open'), null);
  await page.locator('#update-source-details summary').click();
  await page.waitForFunction(() => document.querySelector('#update-baseline').innerText.length > 0);
  assert.match(await page.locator('#update-baseline').innerText(), /AI 수정 후 최종 소스는 보관되어 있지/);
  for (const label of ['추가 1', '수정 1', '삭제 1']) assert.ok((await page.locator('#update-diff-summary').innerText()).includes(label));
  assert.match(await page.locator('#update-origin').innerText(), /bbbbbbbb/);
  assert.equal(await page.locator('#update-changes img').count(), 0, 'file paths render as text');
  assert.match(await page.locator('#update-impact').innerText(), /파일 1개는 새 버전에서 제외/);
  await page.locator('#update-source-details summary').click();
  await page.waitForFunction(() => getComputedStyle(document.querySelector('#update-source-details'), '::details-content').opacity === '0');
  if (process.env.CI_OUTPUT_DIR) {
    await mkdir(process.env.CI_OUTPUT_DIR, { recursive: true });
    await page.screenshot({ path: join(process.env.CI_OUTPUT_DIR, 'application-update-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: join(process.env.CI_OUTPUT_DIR, 'application-update-mobile.png'), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1000 });
  }
  await page.locator('#deploy-button').click();
  await page.waitForFunction(() => !document.querySelector('#request-error').hidden);
  assert.equal(await page.locator('#review-panel').isVisible(), true, 'rejected start stays in review');
  await page.locator('#deploy-button').click();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료');
  assert.equal(starts[0].id, starts[1].id);
  assert.equal(uploads.length, 2, 'start retry never reuploads or creates another preview');
  const files = join(stateDirectory, 'renamed-folder'); await mkdir(files); await writeFile(join(files, 'index.js'), 'frozen source');
  const zip = await archiveFromPath(files);
  await open(); await page.locator('#archive').setInputFiles({ name: 'renamed-archive.zip', mimeType: 'application/zip', buffer: zip.bytes });
  await review(); await page.waitForFunction(() => !document.querySelector('#update-review').hidden);
  assert.equal(await page.locator('#update-identity').innerText(), 'stable-app');
  assert.equal(await page.locator('#update-rebuild').isChecked(), false);
  assert.equal(await page.locator('#deploy-button').innerText(), '변경 없음으로 완료');
  await page.locator('#deploy-button').click(); await page.waitForFunction(() => document.querySelector('#run-state').textContent.includes('실행 생략'));
  assert.deepEqual(starts.at(-1).body, { rebuild: false });
  await open(); await page.locator('#folder').setInputFiles(files);
  await review(); await page.waitForFunction(() => !document.querySelector('#update-review').hidden);
  assert.equal(await page.locator('#update-identity').innerText(), 'stable-app');
  await page.locator('#update-rebuild').check(); await page.locator('#deploy-button').click();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료');
  assert.deepEqual(starts.at(-1).body, { rebuild: true });
  assert.ok(uploads.every((upload) => upload.keys.every((key) => ['repository_url', 'archive', 'files', 'paths'].includes(key))));
  assert.equal(uploads.at(-1).paths, '["index.js"]');
  assert.equal(previews.length, 3);
  await open(); await page.locator('#repository-url').fill('https://github.com/example/expiry-retry');
  await review(); await page.waitForFunction(() => !document.querySelector('#update-review').hidden);
  const expiredKey = uploads.at(-1).key, startCount = starts.length;
  await page.evaluate(() => { window.originalNow = Date.now; Date.now = () => 4102444800000; });
  await page.locator('#deploy-button').click();
  assert.match(await page.locator('#request-error').innerText(), /만료/);
  assert.equal(starts.length, startCount, 'locally expired preview never dispatches');
  await page.evaluate(() => { Date.now = window.originalNow; delete window.originalNow; });
  await page.locator('#renew-update').click(); await page.waitForFunction(() => !document.querySelector('#deploy-button').disabled);
  assert.notEqual(uploads.at(-1).key, expiredKey, 'same-source re-review after expiry creates a fresh preview key');
  assert.deepEqual(errors, []);
});

test('update review ignores stale source responses and uncertain service state blocks a new update', { timeout: 45000 }, async (t) => {
  const { page, origin, errors } = await start(t, { service: null });
  const application = { id: 'app-guard', app: 'guard-app', target_id: 'guard-target', status: 'ready', current_deployment_state: 'verified',
    current_deployment: { id: 'old', app: 'guard-app', target_id: 'guard-target', status: 'succeeded' } };
  const json = (route, data) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
  await page.route('**/api/v1/applications?*', (route) => json(route, new URL(route.request().url()).searchParams.has('marker')
    ? { items: [{ ...application, id: 'app-next', app: 'next-app' }], next_marker: null }
    : { items: [application], next_marker: 'app-guard' }));
  await page.route('**/api/v1/applications/app-guard', (route) => json(route, application));
  let delayed;
  await page.route('**/api/v1/applications/app-guard/updates', (route) => { delayed = route; });
  await page.goto(origin); await page.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
  await page.locator('[data-view="history"]').click(); await page.locator('#applications-more').click();
  await page.getByRole('button', { name: 'next-app 앱 상세·업데이트' }).waitFor();
  assert.equal(await page.locator('#applications-list > li').count(), 2);
  await page.getByRole('button', { name: 'guard-app 앱 상세·업데이트' }).click();
  await page.locator('#application-update').click(); await page.locator('#repository-url').fill('https://github.com/example/first');
  const pending = page.waitForRequest('**/api/v1/applications/app-guard/updates');
  await page.locator('#deploy-form button[type="submit"]').click(); await pending;
  await page.locator('#repository-url').fill('https://github.com/example/second');
  await delayed.fulfill({ contentType: 'application/json', headers: { location: '/api/v1/deployments/stale-preview' }, body: JSON.stringify({
    id: 'stale-preview', app: application.app, target_id: application.target_id, status: 'preview', base_deployment_id: 'old',
    expires_at: '2099-01-01T00:00:00Z', baseline_kind: 'deployed', no_changes: true, changes: { added: [], modified: [], deleted: [], unchanged: 1 } }) });
  await page.waitForFunction(() => !document.querySelector('#deploy-form button[type="submit"]').disabled);
  assert.equal(await page.locator('#review-panel').isVisible(), false);
  await page.locator('#cancel-update').click();
  application.current_deployment_state = 'unverified';
  await page.locator('[data-view="history"]').click(); await page.getByRole('button', { name: 'guard-app 앱 상세·업데이트' }).click();
  await page.waitForFunction(() => document.querySelector('#application-detail-message').textContent.includes('운영자 확인'));
  assert.equal(await page.locator('#application-update').isDisabled(), true);
  assert.deepEqual(errors, []);
});

test('browser update crosses real preview/start/source HTTP routes and reuses the registered application', { timeout: 60000 }, async (t) => {
  const submissions = [], publications = new Map(); let registrations = 0, version = 1;
  const service = { targetId: 'runtime-aws', targetIds: [], allowTarget() {},
    async deploy(input) {
      submissions.push(input); const run = String(submissions.length), sha = String(submissions.length).repeat(40);
      publications.set(run, { run_id: run, app: input.app, tenant: 'demo', target_id: input.target_id,
        source_commit: sha, artifact_id: 900 + submissions.length, producer_attempt: 1 });
      return { run_id: run, source_commit: sha };
    },
    status: async (id) => ({ state: 'published', publication: publications.get(id) }),
    sourceFiles: async (publication) => submissions[Number(publication.run_id) - 1].files };
  const applicationAdapter = { targets: { 'runtime-aws': { provider: 'aws', automaticDelivery: true } },
    describe: (environment, app) => ({ id: `app-${app}`, app, target_id: `app-${app}`, environment_target_id: environment, provider: 'aws' }),
    register: async () => { registrations++; return { status: 'ready' }; },
    deployPublished: async (_application, args) => ({ cd: { state: 'deployed', deployed: true, revision: args.sourceCommit },
      public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://example.test' } }) };
  const { page, origin, errors } = await start(t, { service, applicationAdapter, target: { id: 'runtime-aws', provider: 'aws' },
    sourceLoader: async (repository) => ({ source: { type: 'github', repository, sha: 'b'.repeat(40) },
      files: [{ path: 'app.js', content: Buffer.from(`version ${version}`) }] }) });
  await page.goto(origin); await page.waitForFunction(() => document.querySelector('#connection-status').textContent.includes('URL 확인'));
  await page.locator('#repository-url').fill('https://github.com/example/stable-repo');
  await page.locator('#deploy-form button[type="submit"]').click(); await page.locator('#deploy-button').click();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료');
  version = 2;
  await page.locator('[data-view="history"]').click(); await page.getByRole('button', { name: 'stable-repo 앱 상세·업데이트' }).click();
  await page.locator('#application-update').click(); await page.locator('#repository-url').fill('https://github.com/example/renamed-repo');
  await page.locator('#deploy-form button[type="submit"]').click();
  await page.waitForFunction(() => !document.querySelector('#update-review').hidden);
  assert.equal(await page.locator('#review-app').innerText(), 'stable-repo');
  assert.match(await page.locator('#update-diff-summary').innerText(), /수정 1/);
  await page.locator('#update-source-details summary').click();
  await page.waitForFunction(() => document.querySelector('#update-baseline').innerText.length > 0);
  assert.match(await page.locator('#update-baseline').innerText(), /검증된 최종 소스/);
  assert.equal(submissions.length, 1, 'preview does not dispatch');
  version = 3;
  await page.locator('#deploy-button').click();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료' && document.querySelector('#run-binding').textContent.includes('CI run: 2'));
  assert.equal(submissions.length, 2); assert.equal(registrations, 1);
  assert.equal(submissions[1].app, submissions[0].app); assert.equal(submissions[1].target_id, submissions[0].target_id);
  assert.equal(submissions[1].files[0].content.toString(), 'version 2', 'start uses server-frozen source without refetching GitHub');
  await page.locator('[data-view="history"]').click(); await page.getByRole('button', { name: 'stable-repo 앱 상세·업데이트' }).click();
  const downloadEvent = page.waitForEvent('download');
  await page.locator('#application-versions .application-version').first().getByRole('button', { name: /최종 소스 다운로드/ }).click();
  const download = await downloadEvent;
  assert.equal(await download.failure(), null);
  const files = await inspectArchive(await readFile(await download.path()));
  assert.equal(files.find((file) => file.path === 'app.js').content.toString(), 'version 2');
  assert.deepEqual(errors, []);
});

async function queuedBrowser(t, { unknownFirst = false, provider = 'aws' } = {}) {
  const targetId = `runtime-${provider}`;
  const submissions = [], deliveries = [], publications = new Map();
  let release, active = 0, maximumActive = 0;
  const held = new Promise((resolve) => { release = resolve; });
  // Release before start() closes its product worker, including on assertion failure.
  t.after(() => release());
  const service = { targetId, targetIds: [], allowTarget() {},
    async deploy(input) {
      submissions.push(input);
      const run = String(submissions.length), sha = String(submissions.length).repeat(40);
      publications.set(run, { run_id: run, app: input.app, tenant: 'demo', target_id: input.target_id,
        source_commit: sha, artifact_id: 900 + submissions.length, producer_attempt: 1 });
      return { run_id: run, source_commit: sha };
    },
    status: async (id) => ({ state: 'published', publication: publications.get(id) }),
    sourceFiles: async (publication) => submissions[Number(publication.run_id) - 1].files };
  const applicationAdapter = { targets: { [targetId]: { provider, automaticDelivery: true } },
    describe: (environment, app) => ({ id: `app-${app}`, app, target_id: `app-${app}`, environment_target_id: environment, provider }),
    register: async () => ({ status: 'ready' }),
    async deployPublished(_application, args) {
      maximumActive = Math.max(maximumActive, ++active);
      deliveries.push(args.app);
      try {
        if (args.app === 'alpha-queue') {
          await held;
          if (unknownFirst) return { cd: { state: 'unknown', deployed: false }, public_http: { state: 'not_run' },
            error: { code: 'LOCAL_FIXTURE_UNCERTAIN', outcome_unknown: true } };
        }
        return { cd: { state: 'deployed', deployed: true, revision: args.sourceCommit },
          public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: `https://${args.app}.example.test/` } };
      } finally { active--; }
    } };
  const fixture = await start(t, { service, applicationAdapter, target: { id: targetId, provider },
    maxConcurrentDeployments: 1,
    sourceLoader: async (repository) => ({ source: { type: 'github', repository, sha: 'a'.repeat(40) },
      files: [{ path: 'app.js', content: Buffer.from(repository) }] }) });
  const { page, origin } = fixture;
  await page.goto(origin);
  await page.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
  await page.locator('#cloud-provider').selectOption(provider);
  await page.waitForFunction(() => document.querySelector('#connection-status').textContent.includes('URL 확인'));
  async function submit() {
    await page.locator('#deploy-form button[type="submit"]').click();
    await page.waitForFunction(() => !document.querySelector('#review-panel').hidden && !document.querySelector('#deploy-button').disabled);
    const response = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/v1/deployments'
      && response.request().method() === 'POST');
    await page.locator('#deploy-button').click();
    const accepted = await response;
    assert.equal(accepted.status(), 202, 'an active deployment must not reject another app upload');
    const data = await accepted.json(), id = data.resource_id || data.id;
    assert.equal(accepted.headers().location, `/api/v1/deployments/${id}`);
    return id;
  }
  async function read(id) {
    const response = await page.request.get(`${origin}/api/v1/deployments/${id}`);
    assert.equal(response.status(), 200);
    return response.json();
  }
  async function until(id, predicate, timeout = 10000) {
    const deadline = performance.now() + timeout;
    do {
      const value = await read(id);
      if (predicate(value)) return value;
      await new Promise((resolve) => setTimeout(resolve, 100));
    } while (performance.now() < deadline);
    assert.fail(`Deployment ${id} did not reach the expected queue state`);
  }
  return { ...fixture, submissions, deliveries, release, submit, read, until, maximumActive: () => maximumActive };
}

test('new cloud apps keep selection review separate from deployment start and show the verified link', { timeout: 45000 }, async (t) => {
  for (const provider of ['aws', 'gcp']) await t.test(provider, async (t) => {
    const f = await queuedBrowser(t, { provider }), { page } = f;
    const app = `new-${provider}-app`;
    await page.locator('#repository-url').fill(`https://github.com/example/${app}`);
    await page.locator('#deploy-form button[type="submit"]').click();
    await page.locator('#review-panel').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#review-app').innerText(), app);
    assert.equal(f.submissions.length, 0, 'review must not start CI');
    await page.locator('#deploy-button').click();
    await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료');
    await page.locator('#application-link').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#application-link').getAttribute('href'), `https://${app}.example.test/`);
    assert.equal(f.submissions.length, 1);
    assert.equal(f.deliveries.length, 1);
    assert.deepEqual(f.errors, []);
  });
});

test('a second session resolves an app name collision through the optional name without changing the source or owner', { timeout: 45000 }, async (t) => {
  const f = await queuedBrowser(t);
  await f.page.locator('#repository-url').fill('https://github.com/example/beta-queue');
  const original = await f.submit();
  await f.until(original, (row) => row.status === 'succeeded');
  const context = await f.page.context().browser().newContext();
  const page = await context.newPage(); page.setDefaultTimeout(10000);
  await page.goto(f.origin);
  await page.waitForFunction(() => document.querySelector('#connection-status').textContent.includes('URL 확인'));
  await page.locator('#repository-url').fill('https://github.com/example/beta-queue');
  await page.locator('#deploy-form button[type="submit"]').click();
  await page.waitForFunction(() => !document.querySelector('#form-error').hidden);
  assert.match(await page.locator('#form-error').innerText(), /다른 세션.*다른 이름/);
  assert.equal(await page.locator('#review-panel').isVisible(), false);
  assert.equal(f.submissions.length, 1, 'name collision cannot dispatch or overwrite the original app');
  await page.getByLabel('앱 이름 (선택)').fill('second-calculator');
  assert.equal(await page.locator('#review-panel').isVisible(), false, 'renaming requires a fresh review and request key');
  await page.locator('#deploy-form button[type="submit"]').click();
  await page.locator('#review-panel').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#review-app').innerText(), 'second-calculator');
  const response = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/v1/deployments' && r.request().method() === 'POST');
  await page.locator('#deploy-button').click();
  assert.equal((await response).status(), 202);
  await page.waitForFunction(() => document.querySelector('#run-meta').textContent.includes('second-calculator'));
  const deadline = Date.now() + 10000;
  while (f.submissions.length < 2 && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(f.submissions[1].app, 'second-calculator');
  assert.equal(f.submissions[1].files[0].content.toString(), 'https://github.com/example/beta-queue');
  const inventory = await (await page.request.get(f.origin + '/api/v1/applications')).json();
  assert.deepEqual(inventory.items.map((app) => app.app), ['second-calculator']);
  assert.equal((await f.read(original)).app, 'beta-queue');
  assert.deepEqual(f.errors, []);
  await context.close();
});

test('new deployment of the same owned GitHub or ZIP app becomes an update and retains the service identity', { timeout: 45000 }, async (t) => {
  const f = await queuedBrowser(t), { page, origin, stateDirectory } = f;
  await page.locator('#repository-url').fill('https://github.com/example/beta-queue');
  const original = await f.submit();
  const baseline = await f.until(original, (row) => row.status === 'succeeded');
  // The exact server lookup must not depend on the currently loaded inventory page.
  await page.route('**/api/v1/applications?*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '{"items":[]}' }));
  await page.locator('[data-view="deploy"]').click();
  await page.locator('#deploy-form button[type="submit"]').click();
  await page.waitForFunction(() => !document.querySelector('#update-review').hidden);
  assert.equal(await page.locator('#deploy-title').innerText(), '앱 업데이트하기');
  assert.equal(await page.locator('#repository-url').inputValue(), 'https://github.com/example/beta-queue');
  assert.match(await page.locator('#update-diff-summary').innerText(), /변경된 파일이 없습니다/);
  assert.equal(await page.locator('#deploy-button').innerText(), '변경 없음으로 완료');
  await page.locator('#deploy-button').click();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent.includes('변경 없음'));
  assert.equal(f.submissions.length, 1, 'identical deployed source does not rebuild');

  await page.locator('[data-view="deploy"]').click();
  const directory = join(stateDirectory, 'updated-source'); await mkdir(directory);
  await writeFile(join(directory, 'app.js'), 'updated ZIP source');
  const zip = await archiveFromPath(directory);
  await page.locator('#archive').setInputFiles({ name: 'beta-queue.zip', mimeType: 'application/zip', buffer: zip.bytes });
  const previewResponse = page.waitForResponse((r) => new URL(r.url()).pathname === `/api/v1/applications/${baseline.application_id}/updates` && r.request().method() === 'POST');
  await page.locator('#deploy-form button[type="submit"]').click();
  const preview = await (await previewResponse).json();
  assert.equal(preview.application_id, baseline.application_id);
  assert.equal(preview.base_deployment_id, original);
  assert.deepEqual(preview.changes.modified, ['app.js']);
  assert.equal(preview.no_changes, false);
  await page.waitForFunction(() => !document.querySelector('#update-review').hidden);
  assert.equal(await page.locator('#deploy-button').innerText(), '업데이트 시작');
  await page.locator('#deploy-button').click();
  const updated = await f.until(preview.id, (row) => row.status === 'succeeded');
  assert.equal(updated.application_id, baseline.application_id);
  assert.equal(updated.target_id, baseline.target_id);
  assert.equal(updated.public_http.url, baseline.public_http.url);
  assert.equal(f.submissions.length, 2);
  assert.equal(f.submissions[1].files[0].content.toString(), 'updated ZIP source');
  for (const query of ['app=beta-queue', 'environment=cloud&provider=aws&app=beta-queue&app=other', 'environment=cloud&provider=aws&app=beta-queue&extra=1'])
    assert.equal((await page.request.get(`${origin}/api/v1/applications/resolve?${query}`)).status(), 422);
  assert.deepEqual(f.errors, []);
});

test('browser accepts overlapping GitHub ZIP and folder uploads and the real HTTP queue dispatches FIFO once', { timeout: 45000 }, async (t) => {
  const f = await queuedBrowser(t), { page, stateDirectory } = f;
  await page.locator('#repository-url').fill('https://github.com/example/alpha-queue');
  const alpha = await f.submit();
  await f.until(alpha, (row) => row.stage === 'cd');
  assert.equal(f.submissions.length, 1);

  const zipDirectory = join(stateDirectory, 'zip-source'); await mkdir(zipDirectory);
  await writeFile(join(zipDirectory, 'app.js'), 'source from queued ZIP');
  const zip = await archiveFromPath(zipDirectory);
  await page.locator('#archive').setInputFiles({ name: 'beta-queue.zip', mimeType: 'application/zip', buffer: zip.bytes });
  const beta = await f.submit();
  await page.waitForFunction(() => document.querySelector('#run-message').textContent.includes('대기열에 접수'));
  assert.equal(await page.locator('#run-state').innerText(), '실행 대기 중');
  assert.match(await page.locator('#run-message').innerText(), /앞선 작업이 끝나면 자동으로 실행/);

  const folder = join(stateDirectory, 'gamma-queue'); await mkdir(folder);
  await writeFile(join(folder, 'app.js'), 'source from queued folder');
  await page.locator('#folder').setInputFiles(folder);
  const gamma = await f.submit();
  await page.waitForFunction(() => document.querySelector('#run-message').textContent.includes('대기열에 접수'));
  const accepted = await Promise.all([alpha, beta, gamma].map(f.read));
  assert.deepEqual(accepted.map((row) => row.queue.sequence), [1, 2, 3]);
  assert.ok(accepted.every((row) => Number.isFinite(Date.parse(row.queue.enqueued_at))));
  assert.deepEqual(accepted.map((row) => row.status), ['running', 'queued', 'queued']);
  assert.deepEqual(f.submissions.map((row) => row.app), ['alpha-queue']);
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#run-message').textContent.includes('대기열에 접수'));
  assert.deepEqual(f.submissions.map((row) => row.app), ['alpha-queue'], 'reload does not dispatch waiting sources');

  f.release();
  for (const id of [alpha, beta, gamma]) await f.until(id, (row) => row.status === 'succeeded');
  assert.deepEqual(f.submissions.map((row) => row.app), ['alpha-queue', 'beta-queue', 'gamma-queue']);
  assert.deepEqual(f.deliveries, ['alpha-queue', 'beta-queue', 'gamma-queue']);
  assert.equal(f.maximumActive(), 1);
  assert.equal(f.submissions[1].files[0].content.toString(), 'source from queued ZIP');
  assert.equal(f.submissions[2].files[0].content.toString(), 'source from queued folder');
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료');
  assert.equal(f.requests.filter((request) => request.method === 'POST' && request.path === '/api/v1/deployments').length, 3);
  assert.deepEqual(f.errors, []);
});

test('browser shows unknown slot release after the real default 60 seconds without replay or false success', { timeout: 100000 }, async (t) => {
  const f = await queuedBrowser(t, { unknownFirst: true }), { page } = f;
  await page.locator('#repository-url').fill('https://github.com/example/alpha-queue');
  const alpha = await f.submit();
  await f.until(alpha, (row) => row.stage === 'cd');
  f.release();
  const unknown = await f.until(alpha, (row) => row.status === 'unknown');
  assert.equal(unknown.queue.released_at, undefined);
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '실행 결과 확인 필요');
  assert.doesNotMatch(await page.locator('#run-message').innerText(), /다른 앱의 실행을 허용/);

  await page.locator('#repository-url').fill('https://github.com/example/beta-queue');
  const beta = await f.submit();
  await page.waitForFunction(() => document.querySelector('#run-message').textContent.includes('대기열에 접수'));
  assert.equal((await f.read(beta)).status, 'queued');
  assert.deepEqual(f.submissions.map((row) => row.app), ['alpha-queue']);
  // Keep the real API's default clock and grace period; do not synthesize released_at in a browser route.
  const completed = await f.until(beta, (row) => row.status === 'succeeded', 75000);
  const released = await f.read(alpha);
  assert.equal(released.status, 'unknown');
  assert.equal(released.error.outcome_unknown, true);
  assert.equal(released.queue.release_reason, 'unknown_timeout');
  assert.ok(Date.parse(released.queue.released_at) - Date.parse(released.unknown_since) >= 60000);
  assert.ok(Date.parse(completed.queue.started_at) >= Date.parse(released.queue.released_at));
  assert.equal(released.ci.run_id, unknown.ci.run_id);
  assert.deepEqual(f.submissions.map((row) => row.app), ['alpha-queue', 'beta-queue']);

  await page.locator('[data-view="history"]').click();
  await page.locator('#history-refresh').click();
  await page.getByRole('button', { name: 'alpha-queue 실행 상세·작업 로그', exact: true }).click();
  await page.locator('.dh-issue-trigger').click();
  await page.getByRole('button', { name: '로그 전체 보기', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#monitor-message').textContent.includes('다른 앱의 실행을 허용'));
  assert.equal(await page.locator('#monitor-state').innerText(), '실행 결과 확인 필요');
  assert.match(await page.locator('#monitor-message').innerText(), /자동으로 재실행하지 않습니다/);
  assert.equal(await page.locator('#application-link').isVisible(), false);
  assert.equal(f.requests.filter((request) => request.method === 'POST' && request.path === '/api/v1/deployments').length, 2);
  assert.deepEqual(f.errors, []);
});

test('original dashboard cards submit three source types through backend selection and restore the saved record', { timeout: 90000 }, async (t) => {
  const submitted = [], cd = [];
  const commit = 'a'.repeat(40);
  const service = {
    targetId: 'demo-aws',
    deploy: async (input) => {
      submitted.push(input);
      return { run_id: submitted.length, app: input.app, target_id: 'demo-aws', source_commit: commit,
        state: 'queued', actions_url: `https://github.com/example/apps/actions/runs/${submitted.length}` };
    },
    status: async (id) => ({ run_id: Number(id), target_id: 'demo-aws', state: 'published', status: 'completed', conclusion: 'success',
      source_commit: commit, actions_url: `https://github.com/example/apps/actions/runs/${id}`, url: 'https://example.invalid/unverified',
      steps: [{ key: 'loop', status: 'completed', conclusion: 'success' }, { key: 'release', status: 'completed', conclusion: 'success' }],
      publication: { run_id: String(id), target_id: 'demo-aws', app: submitted[Number(id) - 1].app, source_commit: commit, artifact_id: 22, producer_attempt: 1 } }),
  };
  const { page, origin, errors, requests, stateDirectory } = await start(t, { service, target: { provider: 'aws' },
    access: apiAccessConfig({ RAILSHOT_PUBLIC_DEMO: '1', RAILSHOT_ALLOWED_HOSTS: '127.0.0.1', RAILSHOT_ALLOWED_ORIGINS: 'http://127.0.0.1' }),
    sourceLoader: async () => ({ files: [{ path: 'index.js', content: Buffer.from('source from github') }] }),
    deployPublished: async (request) => { cd.push(request); return { cd: { state: 'deployed', revision: 'b'.repeat(40), deployed: true },
      public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://demo.railshot.io' } }; },
  });
  await page.goto(origin);
  await page.waitForFunction(() => document.querySelector('#connection-status').textContent.includes('URL 확인'));
  assert.equal(await page.locator('#target, #operation, #app-name, #environment-panel').count(), 0, 'backend internals do not replace the original UI');
  await page.getByRole('radio', { name: /온프레미스/ }).check();
  await page.waitForFunction(() => document.querySelector('#provider option[value="__new_openstack__"]'));
  await page.locator('#provider').selectOption('__new_openstack__');
  assert.match(await page.locator('#connection-status').innerText(), /등록한 뒤 배포/);
  await page.getByRole('radio', { name: /클라우드/ }).check();
  assert.equal(await page.locator('#provider-field').isVisible(), false);
  const review = () => page.locator('#deploy-form button[type="submit"]').click();
  const run = () => page.locator('#deploy-button').click();
  await page.locator('#repository-url').fill('https://github.com/example/browser-demo.git/');
  await review();
  await page.locator('#review-panel').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#review-app').innerText(), 'browser-demo');
  await page.getByRole('radio', { name: /온프레미스/ }).check();
  assert.equal(await page.locator('#review-panel').isVisible(), false, 'changing environment invalidates the reviewed request');
  await page.locator('#provider').selectOption('__new_openstack__');
  await review();
  assert.match(await page.locator('#form-error').innerText(), /새 OpenStack 환경을 등록한 뒤 배포/);
  assert.equal(submitted.length, 0);
  await page.getByRole('radio', { name: /클라우드/ }).check();
  const output = process.env.CI_OUTPUT_DIR;
  if (output) {
    await mkdir(output, { recursive: true });
    await page.screenshot({ path: join(output, 'original-cards-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: join(output, 'original-cards-mobile.png'), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1000 });
  }
  await review(); await run();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료', undefined, { timeout: 30000 });
  assert.equal(submitted.length, 1);
  assert.equal(submitted[0].app, 'browser-demo');
  assert.equal(await page.locator('#application-link').isVisible(), false, 'legacy success without an owned current application has no live service link');
  assert.equal(await page.locator('#actions-link').getAttribute('href'), 'https://github.com/example/apps/actions/runs/1');
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료', undefined, { timeout: 30000 });
  assert.match(await page.locator('#history-list').innerText(), /browser-demo/);
  assert.equal(submitted.length, 1, 'reload observes without submitting');
  const files = join(stateDirectory, 'fixture'); await mkdir(files);
  await writeFile(join(files, 'index.js'), 'source from zip');
  const zip = await archiveFromPath(files);
  await page.locator('#archive').setInputFiles({ name: 'archive-app.zip', mimeType: 'application/zip', buffer: zip.bytes });
  await review();
  await page.locator('#review-panel').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#review-app').innerText(), 'archive-app');
  await run();
  await page.waitForFunction(() => document.querySelector('#run-meta').textContent.includes('archive-app') && document.querySelector('#run-state').textContent === '앱 배포 완료', undefined, { timeout: 30000 });
  assert.equal(submitted[1].files[0].content.toString(), 'source from zip');
  await writeFile(join(files, 'index.js'), 'source from folder');
  await page.locator('#folder').setInputFiles(files);
  await review();
  await page.locator('#review-panel').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#review-app').innerText(), 'fixture');
  await run();
  await page.waitForFunction(() => document.querySelector('#run-meta').textContent.includes('fixture') && document.querySelector('#run-state').textContent === '앱 배포 완료', undefined, { timeout: 30000 });
  assert.equal(await page.locator('#run-state').innerText(), '앱 배포 완료');
  assert.equal(submitted.length, 3);
  assert.equal(submitted[2].files[0].content.toString(), 'source from folder');
  assert.equal(cd.length, 3);
  assert.equal(await page.locator('#application-link').isVisible(), false, 'legacy success without an owned current application has no live service link');
  assert.equal(requests.some((request) => request.authorization), false, 'no browser credentials');
  assert.equal(requests.some((request) => ['/api/deploy'].includes(request.path)), false, 'dashboard uses product resources');
  const other = await page.context().browser().newContext();
  try {
    const stranger = await other.newPage(); await stranger.goto(origin);
    await stranger.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
    assert.equal(await stranger.locator('#history-list li').count(), 0, 'another browser cannot restore these deployments');
  } finally { await other.close(); }
  assert.deepEqual(errors, []);
  if (process.env.RAILSHOT_BROWSER_SCREENSHOT) {
    const output = process.env.RAILSHOT_BROWSER_SCREENSHOT;
    await page.screenshot({ path: output, fullPage: true });
  }
});

for (const interruption of ['unknown', 'blocked']) test(`dashboard resumes the same published deployment after ${interruption} without another upload or CI dispatch`, { timeout: 60000 }, async (t) => {
  let registrations = 0, submissions = 0, deliveries = 0;
  const commit = 'a'.repeat(40), image = `ghcr.io/example/calculator@sha256:${'b'.repeat(64)}`;
  const describe = (environment_target_id, app) => ({ id: 'app-calculator', target_id: 'app-calculator', environment_target_id, app, provider: 'aws' });
  const applicationAdapter = {
    targets: { 'runtime-aws': { provider: 'aws', automaticDelivery: true } }, describe,
    register: async (application) => { registrations++; return { ...application, status: 'ready' }; },
    deployPublished: async (_application, args) => {
      deliveries++;
      assert.equal(args.publication.images.web, image);
      if (deliveries === 1) return { cd: { state: interruption, deployed: false }, public_http: { state: 'not_run' },
        error: { code: 'APPLICATION_ROUTE_RECONCILE_REQUIRED', outcome_unknown: interruption === 'unknown' } };
      return { cd: { state: 'deployed', revision: 'c'.repeat(40), deployed: true },
        public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://calculator.example.test/' } };
    },
  };
  const service = { targetId: 'runtime-aws', targetIds: [], allowTarget: () => {},
    deploy: async () => { submissions++; return { run_id: 123, source_commit: commit }; },
    status: async () => ({ state: 'published', publication: { run_id: '123', app: 'calculator', target_id: 'app-calculator',
      source_commit: commit, artifact_id: 456, producer_attempt: 1, images: { web: image } } }),
  };
  const { page, origin, errors, requests } = await start(t, { service, target: { id: 'runtime-aws', provider: 'aws' },
    applicationAdapter, sourceLoader: async () => ({ files: [{ path: 'app.js', content: Buffer.from('user calculator source') }] }) });
  await page.goto(origin);
  await page.waitForFunction(() => document.querySelector('#connection-status').textContent.includes('URL 확인'));
  await page.locator('#repository-url').fill('https://github.com/example/calculator');
  await page.locator('#deploy-form button[type="submit"]').click();
  await page.locator('#deploy-button').click();
  await page.waitForFunction(() => !document.querySelector('#resume-run').hidden, undefined, { timeout: 30000 });
  const identity = await page.locator('#run-meta').innerText();
  await page.getByRole('button', { name: '게시된 이미지로 배포 이어가기' }).click();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료', undefined, { timeout: 30000 });
  assert.equal(await page.locator('#run-meta').innerText(), identity);
  assert.equal(await page.locator('#resume-run').isVisible(), false);
  assert.equal(await page.locator('#application-link').getAttribute('href'), 'https://calculator.example.test/');
  assert.deepEqual([registrations, submissions, deliveries], [1, 1, 2]);
  assert.equal(requests.filter((row) => row.method === 'POST' && row.path.endsWith('/actions')).length, 1);
  assert.deepEqual(errors, []);
});

test('anonymous browser sessions persist their selected view separately', { timeout: 45000 }, async (t) => {
  const { page, origin, errors } = await start(t, { service: null });
  await page.goto(origin);
  await page.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
  const cookie = (await page.context().cookies()).find((row) => row.name === 'railshot_session');
  assert.ok(cookie.httpOnly); assert.equal(cookie.sameSite, 'Strict');
  const savedView = page.waitForResponse((res) => res.url().endsWith('/api/v1/preferences') && res.request().method() === 'PUT');
  await page.locator('[data-view="history"]').click(); await savedView;
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
  assert.equal(await page.locator('#history-view').isVisible(), true);
  const other = await page.context().browser().newContext();
  try {
    const stranger = await other.newPage(); await stranger.goto(origin);
    await stranger.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
    assert.equal(await stranger.locator('#deploy-view').isVisible(), true);
  } finally { await other.close(); }
  if (process.env.CI_OUTPUT_DIR) {
    await mkdir(process.env.CI_OUTPUT_DIR, { recursive: true });
    await page.screenshot({ path: join(process.env.CI_OUTPUT_DIR, 'sessions-history-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: join(process.env.CI_OUTPUT_DIR, 'sessions-history-mobile.png'), fullPage: true });
  }
  assert.deepEqual(errors, []);
});

test('each cloud provider keeps the assigned CI and CD target through reload', { timeout: 90000 }, async (t) => {
  for (const provider of ['aws', 'gcp']) await t.test(provider, async (t) => {
    const targetId = `assigned-${provider}`, app = `${provider}-app`, commit = 'a'.repeat(40);
    const submissions = [], deliveries = [];
    const publication = { run_id: 1, target_id: targetId, app, tenant: 'demo', source_commit: commit, artifact_id: 2, producer_attempt: 1 };
    const service = { targetId,
      deploy: async (input) => { submissions.push(input); return { run_id: 1, source_commit: commit }; },
      status: async () => ({ state: 'published', status: 'completed', conclusion: 'success', source_commit: commit, publication }),
    };
    const deployPublished = Object.assign(async (input) => {
      deliveries.push(input);
      return { cd: { state: 'deployed', deployed: true, revision: 'b'.repeat(40) },
        public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: `https://${provider}.example.test/health` } };
    }, { targets: { [targetId]: { applicationName: app, tenant: 'demo' } } });
    const { page, origin, errors } = await start(t, { service, target: { provider }, deployPublished,
      sourceLoader: async () => ({ files: [{ path: 'index.js', content: Buffer.from('provider fixture') }] }),
    });
    await page.goto(origin);
    await page.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
    const saved = page.waitForResponse((res) => res.url().endsWith('/api/v1/preferences') && res.request().method() === 'PUT');
    await page.locator('#cloud-provider').selectOption(provider);
    await saved; await page.reload();
    await page.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
    assert.equal(await page.locator('#cloud-provider').inputValue(), provider);
    await page.locator('#repository-url').fill('https://github.com/example/provider-fixture');
    await page.locator('#deploy-form button[type="submit"]').click();
    await page.locator('#review-panel').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#review-app').innerText(), 'provider-fixture');
    await page.locator('#deploy-button').click();
    await page.waitForFunction(() => document.querySelector('#request-error').textContent.includes('전용입니다'));
    assert.equal(submissions.length, 0); assert.equal(deliveries.length, 0);
    await page.locator('#repository-url').fill(`https://github.com/example/${app}`);
    await page.locator('#deploy-form button[type="submit"]').click();
    await page.locator('#review-panel').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#review-app').innerText(), app);
    await page.locator('#deploy-button').click();
    await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료');
    assert.equal(submissions.length, 1); assert.equal(deliveries.length, 1);
    assert.equal(submissions[0].target_id, targetId); assert.equal(submissions[0].app, app);
    assert.equal(deliveries[0].targetId, targetId); assert.equal(deliveries[0].publication.target_id, targetId);
    assert.equal(await page.locator('#application-link').isVisible(), false, 'historical provider success does not establish a current application');
    assert.deepEqual(errors, []);
  });
});

test('provider selection made before saved preferences arrive stays bound through submission', { timeout: 90000 }, async (t) => {
  for (const [provider, sourceType] of [['gcp', 'github'], ['gcp', 'zip'], ['gcp', 'folder']]) await t.test(`${provider}/${sourceType}`, async (t) => {
    const targetId = `assigned-${provider}`, app = 'provider-race', commit = 'a'.repeat(40);
    const submissions = [], deliveries = [];
    const publication = { run_id: 1, target_id: targetId, app, tenant: 'demo', source_commit: commit, artifact_id: 2, producer_attempt: 1 };
    const service = { targetId: 'assigned-aws', targetIds: ['assigned-aws', 'assigned-gcp'],
      deploy: async (input) => { submissions.push(input); return { run_id: 1, source_commit: commit }; },
      status: async () => ({ state: 'published', status: 'completed', conclusion: 'success', source_commit: commit, publication }),
    };
    const deployPublished = Object.assign(async (input) => {
      deliveries.push(input);
      return { cd: { state: 'deployed', deployed: true, revision: 'b'.repeat(40) },
        public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: `https://${provider}.example.test/health` } };
    }, { targets: Object.fromEntries(service.targetIds.map((id) => [id, { applicationName: app, tenant: 'demo' }])) });
    const { page, origin, errors, stateDirectory } = await start(t, { service, target: { provider: 'aws' },
      providerTargets: { gcp: 'assigned-gcp' }, deployPublished,
      sourceLoader: async () => ({ files: [{ path: 'index.js', content: Buffer.from('provider fixture') }] }),
    });
    let releasePreferences, preferencesRequested;
    const waiting = new Promise((resolve) => { preferencesRequested = resolve; });
    const release = new Promise((resolve) => { releasePreferences = resolve; });
    await page.route('**/api/v1/preferences', async (route) => {
      if (route.request().method() !== 'GET') { await route.continue(); return; }
      preferencesRequested(); await release;
      await route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ view: 'deploy', environment: 'cloud', provider: 'aws' }) });
    });
    await page.goto(origin); await waiting;
    await page.locator('#cloud-provider').selectOption(provider);
    releasePreferences();
    await page.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
    assert.equal(await page.locator('#cloud-provider').inputValue(), provider);
    assert.equal(await page.locator('[name="environment"]:checked').inputValue(), 'cloud');
    if (sourceType === 'github') await page.locator('#repository-url').fill(`https://github.com/example/${app}`);
    else {
      const source = join(stateDirectory, app);
      await mkdir(source); await writeFile(join(source, 'index.js'), 'provider fixture');
      if (sourceType === 'folder') await page.locator('#folder').setInputFiles(source);
      else {
        const zip = await archiveFromPath(source);
        await page.locator('#archive').setInputFiles({ name: `${app}.zip`, mimeType: 'application/zip', buffer: zip.bytes });
      }
    }
    await page.locator('#deploy-form button[type="submit"]').click();
    await page.locator('#review-panel').waitFor({ state: 'visible' });
    assert.match(await page.locator('#review-target').innerText(), provider === 'gcp' ? /Google Cloud/ : /OpenStack/);
    if (provider === 'gcp' && sourceType === 'github') {
      // Restore/autofill can change a native value without emitting a change event.
      await page.locator('#cloud-provider').evaluate((select) => { select.value = 'aws'; });
      await page.locator('#deploy-button').click();
      assert.match(await page.locator('#form-error').innerText(), /검토한 배포 환경과 현재 선택이 다릅니다/);
      assert.equal(submissions.length, 0); assert.equal(deliveries.length, 0);
      await page.locator('#cloud-provider').selectOption('gcp');
      await page.locator('#deploy-form button[type="submit"]').click();
      await page.locator('#review-panel').waitFor({ state: 'visible' });
    }
    await page.locator('#deploy-button').click();
    await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료');
    assert.equal(submissions.length, 1); assert.equal(deliveries.length, 1);
    assert.equal(submissions[0].target_id, targetId); assert.equal(deliveries[0].targetId, targetId);
    assert.deepEqual(errors, []);
  });
});

test('deployment monitor binds metrics, restores progress, and distinguishes stale, collection and HTTP failure', { timeout: 45000 }, async (t) => {
  let state = 'ready', age = 0, http = 1, broken = false;
  const record = { id: 'monitor-demo', application_id: 'monitor-app', app: 'demo-app', target_id: 'demo-aws', status: 'running', stage: 'cd',
    actions_url: 'https://github.com/example/apps/actions/runs/123',
    source_commit: 'a'.repeat(40), source_digest: 'b'.repeat(64), ci: { run_id: '123', state: 'published', images: { app: `ghcr.io/example/app@sha256:${'c'.repeat(64)}` }, steps: [{ key: 'release', status: 'completed', conclusion: 'success' }] },
    cd: { state: 'progressing', revision: 'd'.repeat(40), deployed: false }, public_http: { state: 'not_run', verified_at: null, url: null } };
  const { page, origin, errors, requests } = await start(t, { product: {
    dashboard: { session: () => ({ id: 'monitor-test', expires_at: '2099-01-01T00:00:00Z' }), preferences: () => ({ view: 'deploy', environment: 'cloud', provider: '' }), connections: () => [] },
    list: () => ({ items: [record], next_marker: null, total: 1 }),
    targets: () => [], profiles: () => [],
    getDeploymentLogs: () => ({ deployment_id: record.id, app: record.app, target_id: record.target_id, state: 'ready',
      checked_at: new Date().toISOString(), entries: [{ pod: 'demo-app-123', container: 'app', text: 'GET /health 200\n<img src=x onerror=alert(1)>' }] }),
    getDeployment: () => {
      if (broken) throw new Error('private backend details');
      return { ...record, observation: { deployment_id: record.id, app: record.app, target_id: record.target_id, checked_at: new Date().toISOString(), stale_after_seconds: 90,
        collector: { id: 'acceptance-observer', role: 'shared_observer', lifecycle: 'acceptance', expires_at: new Date(Date.now() + 3600000).toISOString() },
        metrics: Object.fromEntries(Object.entries({ pods: 2, cpu_percent: 12.5, memory_percent: 30, http }).map(([name, value]) => [name, { state, value: state === 'ready' ? value : null, observed_at: new Date(Date.now() - age).toISOString() }])) } };
    },
  } });
  await page.route('**/api/v1/applications?*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: 'monitor-app', app: record.app, status: 'ready', current_deployment_state: 'verified', current_deployment: record }] }) }));
  const monitor = async () => { await page.locator('[data-view="monitor"]').click(); };
  const refresh = async () => {
    await page.locator('[data-view="deploy"]').click();
    await page.locator('#stop-polling').click();
    assert.match(await page.locator('#observation-status').textContent(), /조회 중지 · 마지막 조회/);
    await page.locator('#refresh-run').click();
    await monitor();
  };
  await page.goto(origin); await page.waitForFunction(() => document.querySelector('#metric-pods').textContent === '2개');
  await monitor();
  assert.match(await page.locator('#collector-note').innerText(), /공유 관측 서버 · 임시 인수용 · 만료/);
  assert.match(await page.locator('#run-binding').textContent(), /CI run: 123/);
  assert.match(await page.locator('#run-binding').textContent(), /sha256:cccc/);
  assert.equal(await page.locator('#monitor-application-link').isVisible(), false);
  const output = process.env.CI_OUTPUT_DIR;
  if (output) { await mkdir(output, { recursive: true }); await page.screenshot({ path: join(output, 'monitor-running.png'), fullPage: true }); }
  record.status = 'succeeded'; record.stage = 'complete'; record.cd.deployed = true; record.cd.state = 'deployed';
  record.public_http = { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://app.example.test/health', site_url: 'https://app.example.test/' };
  await page.reload(); await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료', undefined, { timeout: 30000 }); await monitor();
  assert.equal(await page.locator('#monitor-application-link').getAttribute('href'), 'https://app.example.test/');
  assert.equal(await page.locator('#monitor-actions-link').getAttribute('href'), 'https://github.com/example/apps/actions/runs/123');
  await page.locator('[data-console="app"]').click();
  await page.waitForFunction(() => document.querySelector('#console-output').textContent.includes('GET /health 200'));
  assert.equal(await page.locator('#console-output img').count(), 0, 'app logs are text, never HTML');
  assert.ok(requests.some((request) => request.path === '/api/v1/deployments/monitor-demo/logs'));
  await page.locator('[data-console="work"]').click();
  if (output) await page.screenshot({ path: join(output, 'monitor-success.png'), fullPage: true });
  age = 100000; await refresh(); await page.waitForFunction(() => document.querySelector('#metric-pods').textContent === '오래된 값');
  age = 0; state = 'collection_failed'; await refresh(); await page.waitForFunction(() => document.querySelector('#metric-pods').textContent === '수집 실패');
  state = 'ready'; http = 0; await refresh(); await page.waitForFunction(() => document.querySelector('#metric-http').textContent === '검사 실패');
  assert.equal(await page.locator('#metric-pods').innerText(), '2개');
  broken = true; await refresh(); await page.waitForFunction(() => document.querySelector('#metric-pods').textContent === '수집 연결 실패');
  assert.match(await page.locator('#monitor-state').innerText(), /상태 조회 실패/);
  assert.match(await page.locator('#observation-status').innerText(), /재시도 중/);
  await page.locator('[data-view="deploy"]').click();
  await page.locator('#stop-polling').click();
  assert.match(await page.locator('#observation-status').textContent(), /조회 중지/);
  await monitor();
  if (output) await page.screenshot({ path: join(output, 'monitor-unavailable.png'), fullPage: true });
  broken = false; record.status = 'failed'; record.stage = 'ci'; record.error = { message: '테스트를 찾지 못했습니다. 실행 가능한 테스트를 추가하세요.' }; record.public_http.state = 'not_run';
  record.ci.diagnostics = { state: 'ready', reason: 'NO_TESTS', phase: 'Q.discovery', agent_attempts: 1, changed_file_count: 3 };
  await page.reload(); await page.waitForFunction(() => document.querySelector('#run-state').textContent === '실행 실패'); await monitor();
  assert.equal(await page.locator('#monitor-application-link').isVisible(), false);
  assert.match(await page.locator('#monitor-message').innerText(), /테스트를 찾지 못했습니다/);
  assert.match(await page.locator('#console-output').innerText(), /NO_TESTS/);
  assert.equal(await page.locator('#monitor-actions-link').isVisible(), true);
  if (output) await page.screenshot({ path: join(output, 'monitor-failed.png'), fullPage: true });
  assert.equal(requests.some((request) => request.method === 'POST' && request.path !== '/api/v1/sessions'), false, 'resume and observation never redeploy');
  assert.deepEqual(errors, []);
});

test('work log reads bound agent events over HTTP and marks stale or failed observations without redeploying', { timeout: 45000 }, async (t) => {
  const record = { id: 'events-demo', app: 'demo-app', target_id: 'demo-aws', status: 'running',
    source_commit: 'a'.repeat(40), ci: { run_id: '123', state: 'running', steps: [] } };
  let mode = 'live', attempt = 1;
  const { page, origin, errors, requests } = await start(t, { product: {
    dashboard: { session: () => ({ id: 'events-test', expires_at: '2099-01-01T00:00:00Z' }), preferences: () => ({ view: 'monitor', environment: 'cloud', provider: '' }), connections: () => [] },
    list: () => ({ items: [record], next_marker: null, total: 1 }), targets: () => [], profiles: () => [], getDeployment: () => record,
    getDeploymentEvents: () => {
      if (mode === 'error') throw new Error('private event transport details');
      return { deployment_id: record.id, app: record.app, target_id: record.target_id, source_commit: record.source_commit,
        run_id: record.ci.run_id, run_attempt: attempt, state: mode === 'empty' ? 'not_started' : 'live',
        checked_at: new Date().toISOString(), updated_at: new Date(Date.now() - (mode === 'stale' ? 120000 : 0)).toISOString(),
        truncated: true, items: mode === 'empty' ? [] : [{ sequence: 1, event_name: 'agent.heartbeat', role: 'fixer',
          progress: { sdk_event_count: 7, elapsed_ms: 1200, last_sdk_event_at_ms: Date.now(), item_counts: { commandExecution: 1 } } }], next_marker: null };
    },
  } });
  const output = page.locator('#console-output');
  const refresh = async () => { await page.locator('[data-console="work"]').click(); };
  await page.goto(origin);
  await page.waitForFunction(() => document.querySelector('#console-output').textContent.includes('agent.heartbeat'));
  assert.match(await output.innerText(), /관측 중/);
  assert.match(await output.innerText(), /최근 이벤트만/);
  assert.match(await output.innerText(), /sdk_event_count/);
  mode = 'stale'; await refresh();
  await page.waitForFunction(() => document.querySelector('#console-output').textContent.includes('갱신이 지연'));
  mode = 'error'; await refresh();
  await page.waitForFunction(() => document.querySelector('#console-output').textContent.includes('이벤트 조회 실패'));
  assert.match(await output.innerText(), /agent.heartbeat/);
  assert.doesNotMatch(await output.innerText(), /private event/);
  mode = 'empty'; attempt = 2; await refresh();
  await page.waitForFunction(() => document.querySelector('#console-output').textContent.includes('아직 CI 진행 이벤트'));
  assert.doesNotMatch(await output.innerText(), /agent.heartbeat/, 'an earlier attempt is never reused for a new attempt');
  assert.ok(requests.some((request) => request.path === '/api/v1/deployments/events-demo/events'));
  assert.equal(requests.some((request) => request.authorization), false);
  assert.equal(requests.some((request) => request.method === 'POST' && request.path !== '/api/v1/sessions'), false);
  assert.deepEqual(errors, []);
});

test('removed connections view falls back to deployment without loading saved connections', { timeout: 45000 }, async (t) => {
  const { page, origin, errors, requests } = await start(t, { service: null });
  await page.route('**/api/v1/preferences', (route) => route.request().method() === 'GET'
    ? route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ view: 'connections', environment: 'cloud', provider: 'aws' }) })
    : route.continue());
  const fallback = page.waitForResponse((response) => response.url().endsWith('/api/v1/preferences') && response.request().method() === 'PUT');
  await page.goto(origin);
  assert.equal((await fallback).request().postDataJSON().view, 'deploy');
  await page.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
  assert.equal(await page.locator('#deploy-view').isVisible(), true);
  assert.equal(await page.locator('[data-view="connections"], #connections-view, #connection-form').count(), 0);
  assert.equal(requests.some((request) => request.path.startsWith('/api/v1/connections')), false);
  assert.equal((await page.request.get(`${origin}/src/connections.js`)).status(), 404);
  assert.match(await page.locator('#session-note').textContent(), /까지 유지/);
  const saved = page.waitForResponse((response) => response.url().endsWith('/api/v1/preferences') && response.request().method() === 'PUT');
  await page.getByRole('radio', { name: /온프레미스/ }).check();
  assert.equal((await saved).status(), 200);
  assert.deepEqual(errors, []);
});

test('update review expires visibly and a lost start response resumes the same preview without claiming service success', { timeout: 45000 }, async (t) => {
  const { page, origin, errors } = await start(t, { service: null });
  const baseline = { id: 'stable-v1', app: 'stable-app', status: 'succeeded', cd: { deployed: true, revision: 'a'.repeat(40) },
    public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://stable.example.test/' } };
  const application = { id: 'app-timing', app: 'stable-app', provider: 'aws', target_id: 'same-target', environment_target_id: 'k3s-aws', status: 'ready',
    current_deployment_state: 'verified', current_deployment: baseline, latest_deployment: { id: 'failed-v2', status: 'failed' } };
  let preview, startCount = 0, previewCount = 0, pendingStart;
  const json = (route, data, headers = {}) => route.fulfill({ contentType: 'application/json', headers, body: JSON.stringify(data) });
  await page.route('**/api/v1/applications?*', route => json(route, { items: [application], next_marker: null }));
  await page.route('**/api/v1/applications/app-timing', route => json(route, application));
  await page.route('**/api/v1/applications/app-timing/updates', route => {
    previewCount++;
    preview = { id: `timing-${previewCount}`, application_id: application.id, app: application.app, target_id: application.target_id,
      status: 'preview', expires_at: new Date(Date.now() + 600000).toISOString(), base_deployment_id: baseline.id, baseline_kind: 'deployed',
      no_changes: false, changes: { added: [], modified: ['app.js'], deleted: [], unchanged: 10 } };
    return json(route, preview, { location: `/api/v1/deployments/${preview.id}` });
  });
  await page.route('**/api/v1/deployments/timing-*/start', async route => {
    startCount++;
    if (startCount === 1) { pendingStart = route; return; }
    application.current_deployment_state = 'unverified';
    preview = { ...preview, status: 'unknown', stage: 'cd', ci: { state: 'published' }, cd: { state: 'unknown' } };
    return json(route, preview, { location: `/api/v1/deployments/${preview.id}` });
  });
  await page.route('**/api/v1/deployments/timing-*', route => json(route, preview));
  await page.goto(origin); await page.locator('[data-view="history"]').click();
  await page.getByRole('button', { name: 'stable-app 앱 상세·업데이트' }).click();
  await page.locator('#application-update').click();
  assert.equal(await page.locator('#update-site').getAttribute('href'), 'https://stable.example.test/');
  assert.match(await page.locator('#update-latest-note').innerText(), /마지막으로 검증된 배포/);
  await page.locator('#repository-url').fill('https://github.com/example/update');
  await page.locator('#deploy-form button[type="submit"]').click();
  await page.locator('#review-panel').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#update-changes details').count(), 1, 'empty change groups are omitted');
  assert.equal(await page.locator('#update-changes details').getAttribute('open'), null);
  await page.evaluate(() => { window.savedNow = Date.now; Date.now = () => 4102444800000; document.dispatchEvent(new Event('visibilitychange')); });
  assert.equal(await page.locator('#deploy-button').isDisabled(), true);
  assert.match(await page.locator('#review-note').innerText(), /만료/);
  assert.equal(startCount, 0);
  await page.evaluate(() => { Date.now = window.savedNow; delete window.savedNow; });
  await page.locator('#renew-update').click();
  await page.waitForFunction(() => !document.querySelector('#deploy-button').disabled);
  assert.equal(previewCount, 2);
  await page.locator('#deploy-button').click();
  await page.waitForFunction(() => document.querySelector('#deploy-button').disabled);
  assert.equal(await page.locator('#edit-selection').isDisabled(), true);
  assert.equal(await page.locator('#cancel-update').isDisabled(), true);
  const deadline = Date.now() + 10000;
  while (!pendingStart && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(pendingStart); await pendingStart.abort();
  await page.waitForFunction(() => !document.querySelector('#request-error').hidden);
  assert.equal(await page.locator('#deploy-button').innerText(), '실행 상태 다시 확인');
  assert.equal(await page.locator('#edit-selection').isDisabled(), true);
  await page.locator('#deploy-button').click();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '실행 결과 확인 필요');
  await page.waitForFunction(() => document.querySelector('#update-site').hidden);
  assert.equal(startCount, 2);
  assert.equal(previewCount, 2, 'retry reads/starts the same preview rather than uploading again');
  assert.equal(await page.locator('#deploy-form').isVisible(), false);
  assert.equal(await page.locator('#review-panel').isVisible(), false);
  assert.equal(await page.locator('#update-again').isVisible(), false);
  assert.match(await page.locator('#update-service-state').innerText(), /확인 필요/);
  assert.equal(await page.locator('#run-details').getAttribute('open'), null);
  assert.deepEqual(errors, []);
});
