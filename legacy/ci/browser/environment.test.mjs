import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdir, readFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { join } from 'node:path';
import test from 'node:test';
import { chromium } from 'playwright';

test('original cloud card binds a DB plan, blocks invalid plans, permits independent review during unknown, and replays a lost response after expiry', { timeout: 60000 }, async (t) => {
  const plans = [], deployments = [], environments = [], errors = [];
  const attempts = [], acceptedKeys = new Map(), outcomes = new Map();
  const profiles = [
    { id: 'ha-profile', label: 'AWS with PostgreSQL', provider: 'aws', supported: true, deployment_supported: true,
      target_id: 'ha-runtime', create_per_request: true, application_name: null, database: { mode: 'patroni', required: true, database_nodes: 2, dcs_voters: 3, proxy_nodes: 1 } },
    { id: 'plain-profile', label: 'AWS runtime', provider: 'aws', supported: true, deployment_supported: true,
      target_id: 'plain-runtime', application_name: 'plain-app', database: null },
    { id: 'optional-profile', label: 'Optional database environment', provider: 'aws', supported: true, deployment_supported: false,
      target_id: 'optional-runtime', application_name: null, database: { mode: 'patroni', required: false, database_nodes: 2, dcs_voters: 3, proxy_nodes: 1 } },
  ];
  let planMode = 'valid', deploymentMode = 'valid', activeProfiles = [profiles[0]];
  const respond = (response, status, body, location) => {
    response.writeHead(status, { 'content-type': 'application/json', ...(location ? { location } : {}) });
    response.end(JSON.stringify(body));
  };
  const server = createServer(async (request, response) => {
    const path = new URL(request.url, 'http://localhost').pathname;
    const body = [];
    for await (const chunk of request) body.push(chunk);
    const bytes = Buffer.concat(body);
    if (path === '/api/v1/sessions') return respond(response, 200, { expires_at: '2099-01-01T00:00:00Z' });
    if (path === '/api/v1/preferences') return respond(response, 200, { view: 'deploy', environment: 'cloud', provider: '' });
    if (path === '/api/v1/connections') return respond(response, 200, { items: [] });
    if (path === '/api/v1/deployments' && request.method === 'GET') return respond(response, 200, { items: deployments.map(({ input }, index) => ({
      id: `execution-${index + 1}`, app: input.app, status: outcomes.get(`execution-${index + 1}`), target_id: input.target_id,
    })).reverse() });
    if (path === '/api/v1/targets') return respond(response, 200, { items: [{ id: 'ready-runtime', label: 'Existing runtime',
      capabilities: { ci_submission: true, application_deployment: true } }] });
    if (path === '/api/v1/profiles') return respond(response, 200, { items: activeProfiles });
    if (path === '/api/v1/options') return respond(response, 200, { items: [{ id: 'cloud-aws', environment: 'cloud', provider: 'aws', label: 'AWS', available: true }] });
    if (path === '/api/v1/plans') {
      const input = JSON.parse(bytes); plans.push(input);
      const result = { id: `plan-${plans.length}`, ...input, executable: planMode !== 'budget', blockers: planMode === 'budget' ? ['BUDGET_EXCEEDED'] : [],
        cost: { currency: 'USD', incremental_estimate: '4.00', projected_total: planMode === 'budget' ? '31.36' : '29.36', limit: '30.00' },
        expires_at: new Date(Date.now() + (planMode === 'expired' ? -1000 : 900000)).toISOString() };
      if (profiles.find((profile) => profile.id === input.runtime.profile_id)?.create_per_request) result.runtime_target_id = `ha-runtime-${String(plans.length).padStart(8, '0')}`;
      if (planMode === 'wrong-name') result.name = 'another-app';
      return respond(response, 200, result);
    }
    if (path === '/api/v1/deployments' || path === '/api/v1/builds') {
      const form = await new Request('http://localhost', { method: 'POST', body: bytes, headers: request.headers }).formData();
      const input = Object.fromEntries(form.entries()), key = request.headers['idempotency-key'];
      attempts.push({ key, path, input });
      if (acceptedKeys.has(key)) {
        const existing = acceptedKeys.get(key);
        assert.deepEqual(input, existing.input, 'replay must retain the accepted source and plan');
        return respond(response, 202, { id: existing.id, status: 'accepted' }, `${path}/${existing.id}`);
      }
      deployments.push({ path, input });
      const id = `execution-${deployments.length}`;
      acceptedKeys.set(key, { id, input });
      outcomes.set(id, deploymentMode === 'unknown' ? 'unknown' : 'succeeded');
      if (deploymentMode === 'lost-response') {
        response.writeHead(202, { 'content-type': 'application/json' });
        return response.end('{'); // The server accepted the request; the response was truncated.
      }
      return respond(response, 202, { id, status: 'accepted' }, `${path}/${id}`);
    }
    if (/^\/api\/v1\/(deployments|builds)\/execution-\d+$/.test(path)) {
      const input = deployments[Number(path.split('-').at(-1)) - 1].input;
      return respond(response, 200, { id: path.split('/').at(-1), target_id: input.target_id, app: input.app,
        status: path.includes('/builds/') ? 'published' : outcomes.get(path.split('/').at(-1)), steps: [] });
    }
    if (path === '/api/v1/environments') {
      environments.push(JSON.parse(bytes));
      return respond(response, 202, { id: 'environment-1', status: 'accepted' }, '/api/v1/environments/environment-1');
    }
    if (path === '/api/v1/environments/environment-1') return respond(response, 200, { id: 'environment-1',
      status: 'succeeded', runtime_target_id: 'ha-runtime', deployment_supported: true });
    const files = { '/': ['index.html', 'text/html'], '/app.js': ['app.js', 'text/javascript'], '/styles.css': ['styles.css', 'text/css'],
      '/src/api.js': ['src/api.js', 'text/javascript'], '/src/openstack-installer.js': ['src/openstack-installer.js', 'text/javascript'],
      '/src/insights.js': ['src/insights.js', 'text/javascript'],
      '/src/insights-view.js': ['src/insights-view.js', 'text/javascript'],
      '/src/deployment-history.js': ['src/deployment-history.js', 'text/javascript'],
      '/src/recovery.js': ['src/recovery.js', 'text/javascript'],
      '/src/lifecycle.js': ['src/lifecycle.js', 'text/javascript'],
      '/contracts/application.mjs': ['../../contracts/application.mjs', 'text/javascript'] };
    if (!files[path]) return respond(response, 404, { error: 'fixture path unavailable' });
    response.writeHead(200, { 'content-type': files[path][1] });
    response.end(await readFile(new URL('../../apps/dashboard/' + files[path][0], import.meta.url)));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined });
  t.after(async () => {
    await browser.close();
    await new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); });
  });
  const page = await browser.newPage(); page.setDefaultTimeout(10000);
  await page.addInitScript(() => {
    const schedule = window.setTimeout.bind(window);
    window.railshotTestTimeouts = [];
    window.setTimeout = (callback, delay, ...args) => {
      window.railshotTestTimeouts.push(delay);
      return schedule(callback, delay, ...args);
    };
  });
  const origin = `http://127.0.0.1:${server.address().port}`;
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route('**/*', (route) => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
  await page.goto(origin);
  await page.waitForFunction(() => !document.querySelector('#deployment-database-field').hidden);
  assert.equal(await page.locator('#target, #operation, #app-name, #environment-panel').count(), 0);
  await page.locator('#repository-url').fill('https://github.com/example/my-new-app');
  assert.equal(await page.locator('#deployment-database').inputValue(), 'patroni');
  assert.equal(await page.locator('#deployment-database option[value="none"]').isDisabled(), true);
  assert.match(await page.locator('#deployment-database-note').innerText(), /PostgreSQL 2대.*DCS.*3대.*프록시 1대/);
  const review = () => page.locator('#deploy-form button[type="submit"]').click();
  await review();
  await page.waitForFunction(() => !document.querySelector('#review-panel').hidden);
  assert.deepEqual(plans[0], { name: 'my-new-app', runtime: { profile_id: 'ha-profile', node_count: 1 },
    database: { mode: 'patroni', placements: [{ profile_id: 'ha-profile', database_nodes: 2, dcs_voters: 3, proxy_nodes: 1 }] } });
  assert.equal(deployments.length, 0, 'review must not create resources or submit source');
  assert.equal(await page.locator('#review-app').innerText(), 'my-new-app');
  assert.match(await page.locator('#review-note').innerText(), /추가 비용 예상 4.00 USD.*29.36.*30.00/);
  if (process.env.CI_OUTPUT_DIR) {
    await mkdir(process.env.CI_OUTPUT_DIR, { recursive: true });
    await page.screenshot({ path: join(process.env.CI_OUTPUT_DIR, 'app-db-review.png'), fullPage: true });
  }
  await page.locator('#deploy-button').click();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료');
  assert.deepEqual(deployments[0], { path: '/api/v1/deployments', input: { app: 'my-new-app', target_id: 'ha-runtime-00000001',
    plan_id: 'plan-1', repository_url: 'https://github.com/example/my-new-app' } });
  assert.equal(deployments.length, 1, 'one approved plan must produce one final deployment request');
  assert.equal(environments.length, 0, 'combined deployment delegates environment execution to the product API');
  const timeouts = await page.evaluate(() => window.railshotTestTimeouts);
  assert.ok(timeouts.includes(600000), 'environment planning gets ten minutes');
  assert.ok(timeouts.includes(120000), 'deployment submission retains its two-minute deadline');
  assert.ok(timeouts.includes(15000), 'GET requests retain their fifteen-second deadline');

  activeProfiles = [profiles[1]];
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#connection-status').textContent.includes('새 실행 환경'));
  await page.locator('#repository-url').fill('https://github.com/example/my-new-app');
  assert.equal(await page.locator('#deployment-database-field').isVisible(), false);
  await review();
  await page.waitForFunction(() => document.querySelector('#form-error').textContent.includes('plain-app 앱 전용'));
  assert.equal(await page.locator('#deploy-button').isDisabled(), true);
  assert.equal(plans.length, 1, 'a fixed profile cannot replace the source app with its registered app');
  assert.equal(deployments.length, 1);
  await page.locator('#repository-url').fill('https://github.com/example/plain-app');
  await review();
  await page.waitForFunction(() => !document.querySelector('#review-panel').hidden);
  assert.equal(await page.locator('#review-app').innerText(), 'plain-app');
  assert.deepEqual(plans[1].database, { mode: 'none' });
  await page.locator('#deploy-button').click();
  await page.waitForFunction(() => document.querySelector('#run-meta').textContent.includes('execution-2') && document.querySelector('#run-state').textContent === '앱 배포 완료');
  assert.equal(deployments[1].input.plan_id, 'plan-2');
  assert.equal(deployments[1].input.app, 'plain-app');
  assert.equal(deployments[1].input.target_id, 'plain-runtime', 'legacy plans without runtime_target_id use the fixed profile target');

  activeProfiles = [profiles[0]];
  await page.reload();
  await page.waitForFunction(() => !document.querySelector('#deployment-database-field').hidden);
  await page.locator('#repository-url').fill('https://github.com/example/my-new-app');
  planMode = 'budget';
  await review();
  await page.waitForFunction(() => document.querySelector('#form-error').textContent.includes('BUDGET_EXCEEDED'));
  assert.match(await page.locator('#form-error').innerText(), /31.36.*30.00/);
  assert.equal(await page.locator('#deploy-button').isDisabled(), true);
  planMode = 'wrong-name';
  await review();
  await page.waitForFunction(() => document.querySelector('#form-error').textContent.includes('일치하지 않습니다'));
  assert.equal(await page.locator('#deploy-button').isDisabled(), true);
  planMode = 'expired';
  await review();
  await page.waitForFunction(() => document.querySelector('#form-error').textContent.includes('만료'));
  assert.equal(await page.locator('#deploy-button').isDisabled(), true);
  assert.equal(deployments.length, 2);

  planMode = 'valid'; deploymentMode = 'unknown';
  await review();
  await page.waitForFunction(() => !document.querySelector('#review-panel').hidden);
  await page.locator('#deploy-button').click();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '실행 결과 확인 필요');
  const plansBeforeUnknownReview = plans.length;
  await page.locator('#repository-url').fill('https://github.com/example/independent-app');
  await review();
  await page.waitForFunction(() => !document.querySelector('#review-panel').hidden
    && document.querySelector('#review-app').textContent === 'independent-app');
  assert.equal(await page.locator('#form-error').isVisible(), false);
  assert.equal(plans.length, plansBeforeUnknownReview + 1, 'unknown execution must not block reviewing another app');
  assert.equal(await page.locator('#run-state').innerText(), '실행 결과 확인 필요');
  assert.equal(deployments.length, 3, 'review is not an execution and does not replay the unknown app');

  // Resolve the previous execution before exercising an independent lost-response scenario.
  outcomes.set('execution-3', 'succeeded'); deploymentMode = 'lost-response';
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료'
    && !document.querySelector('#deployment-database-field').hidden);
  await page.locator('#repository-url').fill('https://github.com/example/retry-app');
  await review();
  await page.waitForFunction(() => !document.querySelector('#review-panel').hidden);
  await page.locator('#deploy-button').click();
  await page.waitForFunction(() => !document.querySelector('#request-error').hidden);
  assert.equal(deployments.length, 4, 'the first request was accepted before its response was lost');
  assert.equal(await page.locator('#deploy-button').isDisabled(), false);
  const plansBeforeRetry = plans.length;
  await page.evaluate(() => { const now = Date.now; Date.now = () => now() + 3600000; });
  await page.locator('#deploy-button').click();
  await page.waitForFunction(() => document.querySelector('#run-meta').textContent.includes('execution-4')
    && document.querySelector('#run-state').textContent === '앱 배포 완료');
  assert.equal(plans.length, plansBeforeRetry, 'expired-plan replay must not create a replacement plan');
  assert.equal(deployments.length, 4, 'same-key replay must recover the existing execution');
  assert.deepEqual(attempts.at(-1), attempts.at(-2), 'retry must preserve key, source and plan after expiry');
  assert.deepEqual(errors, []);
});
