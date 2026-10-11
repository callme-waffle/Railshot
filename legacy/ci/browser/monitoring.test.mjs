import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdtemp, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { chromium } from 'playwright';
import { createAppServer } from '../../apps/api/src/server.js';
import { createProductStore } from '../../apps/api/src/product-store.js';

test('monitor follows the bound Actions run and keeps long console output scrollable', { timeout: 45000 }, async (t) => {
  const directory = await realpath(await mkdtemp(join(tmpdir(), 'railshot-monitor-')));
  let server, browser, product;
  t.after(async () => {
    await browser?.close();
    if (server?.listening) await new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); });
    await product?.close();
    await rm(directory, { recursive: true, force: true });
  });
  const store = await createProductStore(directory);
  const session = store.dashboard.session();
  const sourceCommit = 'a'.repeat(40);
  await store.transaction((state) => {
    state.operations['deploy-monitor'] = { id: 'deploy-monitor', kind: 'deployments', session_id: session.id,
      app: 'sample-app', target_id: 'demo-aws', source_commit: sourceCommit, status: 'running', stage: 'ci',
      created_at: new Date().toISOString(), ci: { run_id: '4242', state: 'queued', steps: [] } };
    state.bindings['4242'] = { operation_id: 'deploy-monitor', app: 'sample-app', target_id: 'demo-aws', source_commit: sourceCommit };
  });
  await store.close();
  let finished = false;
  const service = { targetId: 'demo-aws', targetIds: ['demo-aws'], status: async () => ({
    run_id: 4242, status: finished ? 'completed' : 'in_progress', conclusion: finished ? 'success' : null,
    state: finished ? 'published' : 'running', source_commit: sourceCommit, target_id: 'demo-aws',
    actions_url: 'https://github.com/Jasmin-Softbank/railshot-apps/actions/runs/4242',
    steps: [
      { key: 'loop', status: 'completed', conclusion: 'success', tasks: [] },
      { key: 'release', status: finished ? 'completed' : 'in_progress', conclusion: finished ? 'success' : null,
        tasks: [{ number: 3, name: 'Verify bundle and publish tested images', status: finished ? 'completed' : 'in_progress', conclusion: finished ? 'success' : null }] },
    ],
    ...(finished ? { publication: { run_id: 4242, app: 'sample-app', target_id: 'demo-aws', source_commit: sourceCommit } } : {}),
  }) };
  server = createAppServer({ stateDirectory: directory, service });
  product = await server.productReady;
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.addCookies([{ name: 'railshot_session', value: session.token, url: origin, httpOnly: true, sameSite: 'Strict' }]);
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  await page.goto(origin);
  await page.locator('[data-view="history"]').click();
  await page.getByRole('button', { name: /sample-app.*상세/ }).click();
  assert.equal(await page.locator('.dh-log-layout').count(), 0);
  await page.getByRole('button', { name: '작업 로그', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#monitor-ci-status')?.textContent.includes('Actions #4242 · 확인'));
  assert.match(await page.locator('#monitor-steps').innerText(), /검증 이미지 게시\s+진행 중/);
  assert.match(await page.locator('#monitor-steps').innerText(), /Verify bundle and publish tested images/);
  assert.equal(await page.locator('#monitor-actions-link').getAttribute('href'), 'https://github.com/Jasmin-Softbank/railshot-apps/actions/runs/4242');

  finished = true;
  await page.locator('#refresh-run').evaluate((button) => button.click());
  await page.waitForFunction(() => document.querySelector('#monitor-steps')?.textContent.includes('게시 확인'));
  assert.match(await page.locator('#monitor-steps').innerText(), /GitOps 반영\s+대기/);
  assert.match(await page.locator('#monitor-steps').innerText(), /URL 및 앱 상태 확인\s+대기/);

  const dimensions = await page.locator('#console-output').evaluate((node) => {
    node.textContent = '긴 작업 로그\n'.repeat(400);
    node.scrollTop = node.scrollHeight;
    return { height: node.getBoundingClientRect().height, clientHeight: node.clientHeight,
      scrollHeight: node.scrollHeight, scrollTop: node.scrollTop };
  });
  assert.ok(dimensions.height <= 480, `console grew to ${dimensions.height}px`);
  assert.ok(dimensions.scrollHeight > dimensions.clientHeight && dimensions.scrollTop > 0);
});

test('failed deployment shows exact checks, escaped evidence and a persisted classification hypothesis', { timeout: 45000 }, async (t) => {
  const directory = await realpath(await mkdtemp(join(tmpdir(), 'railshot-diagnostic-browser-')));
  let server, browser, product;
  t.after(async () => { await browser?.close(); if (server?.listening) await new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); }); await product?.close(); await rm(directory, { recursive: true, force: true }); });
  const store = await createProductStore(directory), session = store.dashboard.session(), source = 'a'.repeat(40);
  await store.transaction((state) => {
    state.operations['diagnostic-demo'] = { id: 'diagnostic-demo', kind: 'deployments', session_id: session.id,
      app: 'diagnostic-demo', target_id: 'demo-aws', source_commit: source, status: 'failed', stage: 'ci', created_at: new Date().toISOString(),
      ci: { run_id: '123', state: 'failed', steps: [] }, error: { code: 'CI_FAILED', message: '빌드 실패' } };
    state.bindings['123'] = { operation_id: 'diagnostic-demo', app: 'diagnostic-demo', target_id: 'demo-aws', source_commit: source };
  }); await store.close();
  let calls = 0;
  const diagnostic = { state: 'ready', checked_at: new Date().toISOString(), case_id: 'b'.repeat(64), case_sha256: 'c'.repeat(64), artifact_id: 2,
    binding: { run_id: 123, producer_attempt: 1, source_commit: source, app: 'diagnostic-demo', target_id: 'demo-aws', tenant: 'demo' },
    source: { tested_sha256: 'd'.repeat(64), after_sha256: 'd'.repeat(64), snapshot: { path: 'snapshot.json' } },
    failure: { layer: 'L2', code: 'TS2322', excerpt: 'app.ts:1:3 error TS2322\n<script>throw Error("untrusted")</script>' }, logs: [], error: null,
    checks: [{ check_id: 'L0', outcome: 'PASS', required: true }, { check_id: 'L1', outcome: 'PASS', required: true },
      { check_id: 'L2', outcome: 'FAIL', required: true }, { check_id: 'L3', outcome: 'NOT_RUN', required: true },
      { check_id: 'Q', outcome: 'NOT_RUN', required: false }, { check_id: 'L4', outcome: 'NOT_RUN', required: false }],
    missing_evidence: [], verification: { release_eligible: false, gate_outcome: 'FAIL' } };
  server = createAppServer({ stateDirectory: directory, service: { targetId: 'demo-aws', diagnostics: async () => structuredClone(diagnostic) },
    classifyFailure: async () => { calls++; return { state: 'succeeded', category: { choice: 'source' }, evidence_refs: [],
      is_hypothesis: true, grants_write_authority: false, action: '참조된 소스와 오류를 확인하세요.' }; } });
  product = await server.productReady; server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.addCookies([{ name: 'railshot_session', value: session.token, url: origin, httpOnly: true, sameSite: 'Strict' }]);
  const page = await context.newPage(), errors = []; page.on('pageerror', error => errors.push(error.message));
  await page.goto(origin); await page.locator('[data-view="history"]').click();
  await page.getByRole('button', { name: /diagnostic-demo.*상세/ }).click();
  await page.locator('.dh-issue-trigger').click();
  await page.locator('.dh-stage-detail').waitFor();
  assert.match(await page.locator('.dh-code').innerText(), /TS2322/);
  assert.equal(await page.locator('.dh-code script').count(), 0);
  await page.getByRole('button', { name: '로그 전체 보기', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#diagnostic-state')?.textContent.includes('연결된 진단'));
  assert.equal(calls, 0); assert.equal(await page.locator('#diagnostic-excerpt script').count(), 0);
  assert.match(await page.locator('#diagnostic-checks').innerText(), /L3 · 실행하지 않음/);
  assert.match(await page.locator('#diagnostic-checks').innerText(), /Q · 실행하지 않음 · 선택 검사/);
  assert.equal(await page.locator('#diagnostic-source').getAttribute('href'), '/api/v1/deployments/diagnostic-demo/source?variant=failed');
  await page.locator('#classify-failure').click(); await page.locator('[data-console="work"]').click();
  await page.waitForFunction(() => document.querySelector('#diagnostic-classification')?.textContent.includes('Jev 원인 추정'));
  assert.equal(calls, 1); assert.match(await page.locator('#diagnostic-classification').innerText(), /복구 완료를 뜻하지 않습니다/);
  assert.deepEqual(errors, []);
  if (process.env.RAILSHOT_SCREENSHOT_DIR) await page.screenshot({ path: join(process.env.RAILSHOT_SCREENSHOT_DIR, 'diagnostics.png'), fullPage: true });
});
