import assert from 'node:assert/strict';
import test from 'node:test';
import { once } from 'node:events';
import { createServer } from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { chromium } from 'playwright';
import { validateQuestion, validateAnswer } from '../../apps/dashboard/src/recovery.js';
import { filterHistory, relatedRecords, deploymentStages, hasDeploymentIssue } from '../../apps/dashboard/src/deployment-history.js';

const question = () => ({ id: 'q-1', deployment_id: 'deployment-1', revision: 'r-1', summary: '연결 실패', prompt: '방법 선택',
  expires_at: new Date(Date.now() + 60000).toISOString(), evidence: [{ label: '로그', text: 'connection refused' }],
  options: [{ id: 'retry', label: '다시 시도', fields: [
    { id: 'mode', label: '방식', type: 'single_select', required: true, choices: [{ id: 'step', label: '현재 단계' }] },
    { id: 'checks', label: '확인', type: 'multi_select', required: true, choices: [{ id: 'network', label: '네트워크' }] },
    { id: 'notes', label: '사항', type: 'text_list', required: false },
    { id: 'text', label: '설명', type: 'text', required: true },
  ] }, { id: 'stop', label: '중단', fields: [] }] });
const answer = () => ({ question_id: 'q-1', deployment_id: 'deployment-1', revision: 'r-1', option_id: 'retry',
  values: { mode: 'step', checks: ['network'], notes: ['one', 'two'], text: '확인' } });

test('recovery contract rejects stale, cross-deployment and unselected/unsupported input', () => {
  assert.equal(validateQuestion(question(), 'deployment-1').id, 'q-1');
  assert.deepEqual(validateAnswer(question(), answer()), answer());
  assert.throws(() => validateQuestion(question(), 'other'));
  assert.throws(() => validateAnswer(question(), { ...answer(), revision: 'old' }));
  assert.throws(() => validateAnswer(question(), { ...answer(), deployment_id: 'other' }));
  assert.throws(() => validateAnswer({ ...question(), expires_at: '2000-01-01' }, answer()));
  for (const values of [{ ...answer().values, mode: 'arbitrary' }, { ...answer().values, checks: [] },
    { ...answer().values, extra: 'shell' }, { ...answer().values, notes: [''] }, { ...answer().values, text: '' }]) {
    assert.throws(() => validateAnswer(question(), { ...answer(), values }));
  }
  assert.throws(() => validateAnswer(question(), { ...answer(), option_id: 'stop' }));
  const invalid = question(); invalid.options[0].fields[0].type = 'shell'; assert.throws(() => validateQuestion(invalid, 'deployment-1'));
});

test('history does not conflate same-named apps or infer successful unobserved stages', () => {
  const records = [{ id: '1', app: 'Shop', target_id: 'aws', kind: 'deployments', status: 'failed', created_at: new Date().toISOString() },
    { id: '2', app: 'Shop', target_id: 'gcp', kind: 'deployments', status: 'running' }];
  assert.deepEqual(relatedRecords(records[0], records).map((item) => item.id), ['1']);
  assert.equal(filterHistory(records, { search: 'SHOP', status: 'failed', days: '1' }).length, 1);
  assert.equal(filterHistory(records, { search: 'unknown' }).length, 0);
  assert.equal(deploymentStages({ status: 'succeeded' })[0].state, 'not_started');
  assert.equal(deploymentStages({ status: 'failed', stage: 'ci' })[0].state, 'failed');
  assert.equal(hasDeploymentIssue({ status: 'succeeded', error: { message: 'old error' } }), false);
  assert.equal(hasDeploymentIssue({ status: 'running' }), false);
  assert.equal(hasDeploymentIssue({ status: 'failed' }), true);
});

test('history preview: stage navigation, conditional inputs, one submission, keyboard and mobile layout', { timeout: 60000 }, async (t) => {
  const root = new URL('../../apps/dashboard/', import.meta.url);
  const server = createServer(async (req, res) => {
    try {
      const path = new URL(req.url, 'http://localhost').pathname;
      if (!/^\/(preview\/history\.(html|js)|src\/(deployment-history|recovery|insights|insights-view)\.js|styles\.css)$/.test(path)) { res.writeHead(404).end(); return; }
      const data = await readFile(new URL(path.slice(1), root));
      res.writeHead(200, { 'Content-Type': path.endsWith('.js') ? 'text/javascript' : path.endsWith('.css') ? 'text/css' : 'text/html' }).end(data);
    } catch { res.writeHead(500).end(); }
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined });
  t.after(async () => { await browser.close(); await new Promise((resolve) => server.close(resolve)); });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } }), errors = [], writes = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('request', (req) => { if (req.method() !== 'GET') writes.push(req.url()); });
  const origin = `http://127.0.0.1:${server.address().port}`;
  await page.goto(`${origin}/preview/history.html`);
  assert.equal(await page.locator('.dh-card').count(), 6);
  const screenshots = process.env.RAILSHOT_HISTORY_SCREENSHOTS;
  if (screenshots) { await mkdir(screenshots, { recursive: true }); await page.screenshot({ path: `${screenshots}/history-list.png`, fullPage: true }); }
  await page.getByRole('button', { name: /auth-service/ }).click();
  await page.getByRole('heading', { name: 'auth-service', exact: true }).waitFor();
  assert.equal(await page.locator('.dh-log-layout').count(), 0, 'pipeline waits for a failed history row selection');
  assert.equal(await page.locator('.dh-row-arrow').count(), 1);
  await page.locator('.dh-issue-row td').first().click();
  assert.match(await page.locator('.dh-stage-detail').innerText(), /DATABASE_URL/);
  assert.equal(await page.locator('.dh-history-pane').getAttribute('aria-hidden'), 'true');
  await page.getByRole('button', { name: /빌드.*성공/ }).click();
  await page.getByText('에이전트 활동 기록', { exact: true }).click();
  assert.match(await page.locator('.dh-agent').innerText(), /agent.completed/);
  await page.getByRole('button', { name: /배포.*실패/ }).click();
  const submit = page.getByRole('button', { name: '선택 내용 확인' }); assert.equal(await submit.isDisabled(), true);
  await page.getByRole('radio', { name: /필수 배포 입력을 보완/ }).check();
  await page.getByLabel('연결 문자열', { exact: true }).fill('temporary-private-input');
  await page.getByRole('radio', { name: '실패한 단계부터' }).check();
  await page.getByRole('checkbox', { name: '네트워크 연결' }).check();
  await page.getByRole('checkbox', { name: '상태 확인 경로' }).check();
  await page.getByRole('textbox', { name: '전달할 사항 항목', exact: true }).fill('첫 번째 전달 사항');
  await page.getByRole('button', { name: '＋ 항목 추가' }).click();
  await page.getByRole('textbox', { name: '전달할 사항 항목', exact: true }).nth(1).fill('두 번째 전달 사항');
  await page.getByRole('button', { name: '전달할 사항 항목 삭제', exact: true }).nth(1).click();
  if (screenshots) await page.screenshot({ path: `${screenshots}/history-recovery-desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  if (screenshots) await page.screenshot({ path: `${screenshots}/history-recovery-mobile.png`, fullPage: true });
  await submit.click(); await page.getByText('입력 형식을 확인했습니다.', { exact: false }).waitFor();
  assert.equal(await submit.isDisabled(), true);
  assert.equal(await page.getByLabel('연결 문자열', { exact: true }).count(), 0);
  assert.equal(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }).includes('temporary-private-input')), false);
  await page.getByRole('button', { name: '상태 다시 확인' }).click();
  await page.getByRole('radio', { name: /작업을 중단/ }).check();
  assert.equal(await page.getByLabel('연결 문자열', { exact: true }).count(), 0);
  await page.getByLabel('전달할 내용', { exact: true }).fill('직접 확인하겠습니다.');
  await submit.click(); await page.getByText('입력 형식을 확인했습니다.', { exact: false }).waitFor();
  await page.getByRole('button', { name: '‹ 배포 내역' }).click();
  await page.getByLabel('앱 이름 검색').fill('shop');
  assert.equal(await page.locator('.dh-card').count(), 1);
  await page.locator('.dh-card-open').focus(); await page.keyboard.press('Enter');
  await page.getByRole('heading', { name: 'shop-api', exact: true }).waitFor();
  assert.equal(await page.locator('.dh-table-wrap tbody tr').count(), 3);
  assert.equal(await page.locator('.dh-log-layout').count(), 0, 'successful deployment hides the pipeline');
  assert.equal(await page.locator('.dh-issue-trigger').count(), 3, 'successful and running agent histories remain accessible');
  assert.doesNotMatch(await page.locator('#preview-detail').innerText(), /해결 방법을 조회하지 못했습니다/);
  assert.equal(await page.locator('.dh-current').count(), 1);
  await page.setViewportSize({ width: 1440, height: 1050 });
  if (screenshots) await page.screenshot({ path: `${screenshots}/history-overview.png`, fullPage: true });
  const failedTrigger = page.locator('.dh-issue-trigger[data-deployment-id="demo-shop-failed"]');
  await failedTrigger.focus(); await page.keyboard.press('Enter');
  await page.locator('.dh-stage-detail').waitFor();
  assert.match(await page.locator('.dh-issue-heading').innerText(), /demo-shop-failed/);
  assert.equal(await page.locator('.dh-detail-viewport').evaluate((element) => element.classList.contains('dh-instant')), true);
  await page.getByRole('button', { name: '‹ 배포내역으로 돌아가기' }).click();
  assert.equal(await failedTrigger.evaluate((element) => document.activeElement === element), true);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await failedTrigger.click();
  assert.equal(await page.locator('.dh-issue-pane').evaluate((element) => getComputedStyle(element).transform), 'none');
  await page.getByRole('button', { name: '‹ 배포내역으로 돌아가기' }).click();
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  // A stage change must not unlock a question whose response was already accepted.
  await page.evaluate(async (q) => {
    const { createHistoryDetail } = await import('/src/deployment-history.js');
    const host = document.createElement('section'); host.id = 'submission-probe'; document.querySelector('#history-view').replaceChildren(host);
    window.submissionCalls = 0;
    const record = { id: q.deployment_id, app: 'submission-probe', kind: 'deployments', status: 'failed', stage: 'cd', ci: { state: 'published' }, cd: { state: 'failed' } };
    window.probe = createHistoryDetail({ host, request: async (path) => ({ data: path.endsWith('/diagnostics') ? {} : record }),
      getRecords: () => [record], getApplications: () => [], serviceUrl: () => null, onLogs: () => {}, onMonitor: () => {},
      recovery: { load: async () => q, submit: async (answer) => { window.submissionCalls++; return { status: 'accepted', question_id: answer.question_id, revision: answer.revision }; } } });
    await window.probe.open(record);
  }, { ...question(), stage: 'deploy' });
  await page.locator('#submission-probe .dh-issue-trigger').click();
  await page.getByRole('radio', { name: '중단', exact: true }).check();
  await page.getByRole('button', { name: '선택한 방법 제출' }).click();
  await page.getByText('응답이 접수되었습니다.', { exact: false }).waitFor();
  await page.getByRole('button', { name: /빌드.*성공/ }).click();
  await page.getByRole('button', { name: /배포.*실패/ }).click();
  assert.equal(await page.getByRole('button', { name: '선택한 방법 제출' }).isDisabled(), true);
  await page.getByRole('button', { name: '상태 다시 확인' }).click();
  await page.getByRole('radio', { name: '중단', exact: true }).waitFor();
  assert.equal(await page.getByRole('radio', { name: '중단', exact: true }).isDisabled(), true);
  assert.equal(await page.evaluate(() => window.submissionCalls), 1);
  assert.deepEqual(writes, []); assert.deepEqual(errors, []);
});

test('agent card opens on successful history, polls independently and preserves questions', { timeout: 60000 }, async (t) => {
  const root = new URL('../../apps/dashboard/', import.meta.url);
  const server = createServer(async (req, res) => {
    const path = new URL(req.url, 'http://localhost').pathname;
    try {
      if (path === '/') { res.setHeader('Content-Type', 'text/html'); res.end('<html lang="ko"><head><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/styles.css"></head><body><main class="content"><section id="history-view"><div id="detail"></div></section></main></body></html>'); return; }
      if (!['/src/recovery.js', '/src/deployment-history.js', '/src/insights.js', '/src/insights-view.js', '/styles.css'].includes(path)) { res.writeHead(404).end(); return; }
      res.setHeader('Content-Type', path.endsWith('.js') ? 'text/javascript' : 'text/css');
      res.end(await readFile(new URL(path.slice(1), root)));
    } catch { res.writeHead(500).end(); }
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined });
  t.after(async () => { await browser.close(); await new Promise(resolve => server.close(resolve)); });
  const page = await browser.newPage({ viewport: { width: 1000, height: 900 } });
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  await page.goto(`http://127.0.0.1:${server.address().port}/`);
  await page.evaluate(async () => {
    const { createHistoryDetail } = await import('/src/deployment-history.js');
    const record = { id: 'deployment-1', app: 'clock', kind: 'deployments', status: 'succeeded', stage: 'complete', ci: { state: 'published' },
      cd: { state: 'deployed' }, created_at: new Date().toISOString(), agent_activity_summary: { id: 'repair-1', state: 'verifying', attempt: 1 } };
    window.activity = { id: 'repair-1', revision: 1, stage: 'build', state: 'verifying', attempt: 1, summary: '시작 검사 실패를 처리합니다.',
      current_action: '수정 후 실행 검사 중', started_at: '2026-10-04T03:00:00Z', updated_at: '2026-10-04T03:00:42Z',
      observation: { state: 'current' }, changes: [{ path: 'Dockerfile', summary: '<img src=x onerror=alert(1)>', status: 'applied' }],
      verification: [{ key: 'image.build', label: '이미지 빌드', state: 'succeeded' }], previous_attempts: [] };
    window.eventReads = 0; window.questionReads = 0; window.detailReads = []; window.unavailable = false;
    const question = { id: 'q-1', deployment_id: record.id, revision: 'r1', stage: 'build', summary: '확인 필요', prompt: '추가 설명',
      expires_at: new Date(Date.now() + 60000).toISOString(), evidence: [{ label: '확인', text: '확인' }],
      options: [{ id: 'provide', label: '설명 제공', fields: [{ id: 'notes', label: '설명', type: 'text', required: true }] }] };
    window.historyDetail = createHistoryDetail({ host: document.querySelector('#detail'), getRecords: () => [record], getApplications: () => [],
      serviceUrl: () => null, onLogs: () => {}, onMonitor: () => {}, recovery: { load: async () => { window.questionReads++; return question; } },
      request: async path => {
        window.detailReads.push(path);
        if (path.endsWith('/events')) {
          window.eventReads++;
          if (window.unavailable) throw new Error('offline');
          return { data: { deployment_id: record.id, agent_activity: structuredClone(window.activity),
            status: window.activity.state === 'succeeded' ? 'completed' : 'running', progress: { poll_after_ms: 5000 } } };
        }
        return { data: record };
      } });
    await window.historyDetail.open(record);
  });
  assert.deepEqual(await page.evaluate(() => ({ events: window.eventReads, questions: window.questionReads, reads: window.detailReads })),
    { events: 0, questions: 0, reads: ['/api/v1/deployments/deployment-1?view=record'] },
    'overview renders after only the durable record read, without waiting for remote events or recovery');
  assert.equal(await page.getByRole('heading', { name: '접속정보', exact: true }).isVisible(), true);
  await page.getByRole('button', { name: /처리 내역 보기/ }).click();
  await page.getByRole('heading', { name: 'AI 자동 복구', exact: true }).waitFor();
  assert.equal(await page.evaluate(() => window.eventReads), 1, 'events load when processing details are opened');
  assert.equal(await page.evaluate(() => window.questionReads), 1);
  assert.match(await page.locator('.dh-agent-card').innerText(), /재검증 중/);
  assert.equal(await page.locator('.dh-agent-card img').count(), 0, 'model description is text, never HTML');
  await page.getByText('변경 내용 보기', { exact: true }).click();
  await page.getByRole('radio', { name: '설명 제공', exact: true }).check();
  await page.getByRole('textbox', { name: '설명', exact: true }).fill('작성 중인 사용자 입력');
  await page.evaluate(() => { window.unavailable = true; });
  await page.getByText('최신 상태를 조회하지 못했습니다.', { exact: false }).waitFor({ timeout: 12000 });
  assert.equal(await page.getByRole('textbox', { name: '설명', exact: true }).inputValue(), '작성 중인 사용자 입력');
  await page.evaluate(() => { window.unavailable = false; window.activity.state = 'succeeded'; window.activity.revision = 2;
    window.activity.current_action = '필수 검사를 통과했습니다.'; window.activity.finished_at = '2026-10-04T03:00:58Z'; });
  await page.locator('.dh-agent-card .dh-status').getByText('복구 완료', { exact: true }).waitFor({ timeout: 12000 });
  assert.equal(await page.locator('.dh-agent-card details').first().getAttribute('open'), '');
  assert.equal(await page.getByRole('textbox', { name: '설명', exact: true }).inputValue(), '작성 중인 사용자 입력');
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  if (process.env.RAILSHOT_HISTORY_SCREENSHOTS) {
    await mkdir(process.env.RAILSHOT_HISTORY_SCREENSHOTS, { recursive: true });
    await page.screenshot({ path: `${process.env.RAILSHOT_HISTORY_SCREENSHOTS}/agent-recovery-mobile.png`, fullPage: true });
  }
  const count = await page.evaluate(() => window.eventReads);
  await page.getByRole('button', { name: '‹ 배포내역으로 돌아가기' }).click();
  // A restored successful record still exposes the activity, rather than hiding its history.
  await page.getByRole('button', { name: /처리 내역 보기/ }).click();
  await page.getByRole('heading', { name: 'AI 자동 복구', exact: true }).waitFor();
  assert.ok(await page.evaluate(() => window.eventReads) >= count);
  assert.deepEqual(errors, []);
});
