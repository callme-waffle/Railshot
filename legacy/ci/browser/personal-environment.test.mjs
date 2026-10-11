import assert from 'node:assert/strict';
import { once } from 'node:events';
import { readFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import test from 'node:test';
import { chromium } from 'playwright';

test('personal OpenStack management uses HTTP fixture states, preserves deploy source, and requires data deletion consent', { timeout: 60000 }, async (t) => {
  const calls = [], targets = [{ id: 'ready-target', label: '연구실 OpenStack', provider: 'openstack', status: 'ready', connection_status: 'ready', deployable: true,
    runtime_preparation: { status: 'succeeded', stage: 'complete', blockers: [], verified_at: '2099-01-01T00:00:00Z' },
    project_id: 'project-a', application_count: 1, last_seen_at: '2026-10-03T01:00:00Z', client_version: '1.0.0', registration_stage: 'ready' },
  { id: 'retry-target', label: '대기 중 OpenStack', provider: 'openstack', status: 'pending', deployable: false,
    connection_status: 'connecting', runtime_preparation: { status: 'not_started', stage: 'client', blockers: [], verified_at: null }, application_count: 0, registration_stage: 'enrollment' },
  { id: 'blocked-target', label: '준비 확인 OpenStack', provider: 'openstack', status: 'preparing', connection_status: 'ready', deployable: false,
    runtime_preparation: { status: 'blocked', stage: 'configuration', blockers: ['RUNTIME_OPERATOR_NOT_CONFIGURED', 'RUNTIME_FUTURE_CODE'], verified_at: null, client_reported_ready: true },
    project_id: 'project-b', application_count: 0, last_seen_at: '2099-01-01T00:00:00Z', client_version: '1.0.0', registration_stage: 'runtime' },
  { id: 'legacy-target', label: '기존 등록 OpenStack', provider: 'openstack', status: 'preparing', connection_status: 'ready', deployable: false,
    runtime_preparation: { status: 'not_started', stage: 'client', blockers: [], verified_at: null },
    project_id: 'project-legacy', application_count: 0, last_seen_at: '2099-01-01T00:00:00Z', client_version: '0.9.0', registration_stage: 'complete' },
  { id: 'completed-target', label: '삭제 완료 예정 OpenStack', provider: 'openstack', status: 'deleting', connection_status: 'ready', deployable: false,
    runtime_preparation: { status: 'revoked', stage: 'complete', blockers: [], verified_at: null }, deletion_operation_id: 'operation-complete',
    project_id: 'project-complete', application_count: 0, last_seen_at: '2099-01-01T00:00:00Z', client_version: '1.0.0', registration_stage: 'complete' }];
  let retryEnrollmentAttempts = 0;
  let failNextRecoveredList = false;
  let failNextCreatedList = false;
  let failNextDeletedList = false;
  let ownerConfigured = false;
  let personalReady = false;
  let deletionReconciled = false;
  let deletionResumed = false;
  let runtimeReconciliationPolls = 0;
  let ownerToken = null, recoveryKey = 'recover-this-once', ownerRotation = 0;
  let releaseDeletePlan;
  const deletePlanGate = new Promise((resolve) => { releaseDeletePlan = resolve; });
  const respond = (response, status, body, location, headers = {}) => {
    response.writeHead(status, { 'content-type': 'application/json', ...(location ? { location } : {}), ...headers }); response.end(JSON.stringify(body));
  };
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, 'http://localhost'), path = url.pathname;
    const chunks = []; for await (const chunk of request) chunks.push(chunk); const bytes = Buffer.concat(chunks);
    const json = () => bytes.length ? JSON.parse(bytes.toString('utf8')) : {};
    const cookies = Object.fromEntries((request.headers.cookie || '').split(';').map((item) => item.trim().split('=')).filter((item) => item.length === 2));
    const ownsTargets = Boolean(ownerToken && cookies.railshot_owner === ownerToken);
    if (path === '/api/v1/sessions') return respond(response, 200, { expires_at: '2099-01-01T00:00:00Z' });
    if (path === '/api/v1/readiness' && url.searchParams.get('scope') === 'personal') return respond(response, 200,
      personalReady ? { scope: 'personal', ready: true, verification_scope: 'configuration_only', blockers: [] }
        : { scope: 'personal', ready: false, verification_scope: 'configuration_only',
          blockers: [{ code: 'RUNTIME_OPERATOR_NOT_CONFIGURED', message: '개인 실행환경 운영 설정이 없습니다.' }] });
    if (path === '/api/v1/preferences') return respond(response, 200, { view: 'deploy', environment: 'cloud', provider: '' });
    if (path === '/api/v1/profiles') return respond(response, 200, { items: [] });
    if (path === '/api/v1/options') return respond(response, 200, { items: [{ environment: 'cloud', provider: 'aws', label: 'AWS', available: true }] });
    if (path === '/api/v1/connections') return respond(response, 200, { items: [] });
    if (path === '/api/v1/applications') return respond(response, 200, { items: [] });
    if (path === '/api/v1/applications/resolve') {
      assert.deepEqual([...url.searchParams.keys()].sort(), ['app', 'environment', 'provider', 'target_id']);
      assert.equal(url.searchParams.get('environment'), 'onprem'); assert.equal(url.searchParams.get('provider'), 'openstack');
      return respond(response, 200, { app: url.searchParams.get('app'), environment_target_id: url.searchParams.get('target_id'), application: null });
    }
    if (path === '/api/v1/deployments') {
      if (request.method === 'GET') return respond(response, 200, { items: [] });
      const form = await new Request('http://localhost', { method: 'POST', body: bytes, headers: request.headers }).formData();
      calls.push({ kind: 'deployment', environment: form.get('environment'), provider: form.get('provider'), target: form.get('target_id') });
      return respond(response, 202, { id: 'deployment-1', status: 'accepted' }, '/api/v1/deployments/deployment-1');
    }
    if (path === '/api/v1/deployments/deployment-1') return respond(response, 200, { id: 'deployment-1', status: 'succeeded', target_id: 'ready-target', steps: [] });
    if (path === '/api/v1/owners') {
      assert.equal(request.headers['x-railshot-request'], 'dashboard');
      if (request.method === 'POST') {
        if (ownerConfigured && ownsTargets) return respond(response, 200, { id: 'owner-1', recovery_configured: true });
        ownerConfigured = true; ownerToken = 'A'.repeat(43);
        return respond(response, 201, { id: 'owner-1', recovery_key: recoveryKey, recovery_key_once: true }, null,
          { 'set-cookie': `railshot_owner=${ownerToken}; Path=/; HttpOnly; SameSite=Strict` });
      }
      return respond(response, 200, { id: ownsTargets ? 'owner-1' : null, recovery_configured: ownsTargets });
    }
    if (path === '/api/v1/recoveries') {
      assert.equal(request.headers['x-railshot-request'], 'dashboard');
      const supplied = json().recovery_key;
      assert.ok(supplied === recoveryKey || supplied === 'recover-list-failure');
      ownerConfigured = true;
      if (supplied === 'recover-list-failure') failNextRecoveredList = true;
      ownerRotation += 1; ownerToken = String.fromCharCode(65 + ownerRotation).repeat(43); recoveryKey = `rotated-recovery-key-${ownerRotation}`;
      return respond(response, 200, { id: 'owner-1', recovery_key: recoveryKey }, null,
        { 'set-cookie': `railshot_owner=${ownerToken}; Path=/; HttpOnly; SameSite=Strict` });
    }
    if (path === '/api/v1/targets' && request.method === 'GET') {
      assert.equal(request.headers['x-railshot-request'], 'dashboard');
      if (!ownsTargets) return respond(response, 200, { items: [] });
      if (url.searchParams.get('scope') === 'owned' && (failNextRecoveredList || failNextCreatedList || failNextDeletedList)) {
        failNextRecoveredList = false; failNextCreatedList = false; failNextDeletedList = false;
        return respond(response, 503, { error: { message: '개인 환경 목록 서버가 응답하지 않습니다.' } });
      }
      return respond(response, 200, { items: url.searchParams.get('scope') === 'owned' ? targets : [] });
    }
    if (path === '/api/v1/targets' && request.method === 'POST') {
      assert.equal(request.headers['x-railshot-request'], 'dashboard');
      if (!ownsTargets) return respond(response, 404, { error: { message: 'missing owner' } });
      const target = { id: 'pending-target', label: json().label, provider: 'openstack', status: 'pending', connection_status: 'connecting', deployable: false,
        runtime_preparation: { status: 'not_started', stage: 'client', blockers: [], verified_at: null }, application_count: 0 };
      targets.push(target); failNextCreatedList = true; calls.push({ kind: 'create-target', body: json() }); return respond(response, 201, target);
    }
    const targetMatch = path.match(/^\/api\/v1\/targets\/([^/]+)(?:\/(enrollments|observations|applications|plans|operations|reconciliations))?$/);
    if (targetMatch) {
      assert.equal(request.headers['x-railshot-request'], 'dashboard');
      if (!ownsTargets) return respond(response, 404, { error: { message: 'missing target' } });
      const [, id, child] = targetMatch, target = targets.find((item) => item.id === id);
      if (!target) return respond(response, 404, { error: { message: 'missing target' } });
      if (!child) {
        if (id === 'blocked-target' && ['queued', 'running'].includes(target.runtime_preparation.status)) {
          runtimeReconciliationPolls += 1;
          if (runtimeReconciliationPolls === 1) target.runtime_preparation = { ...target.runtime_preparation, status: 'running' };
          else Object.assign(target, { status: 'ready', deployable: true,
            runtime_preparation: { status: 'succeeded', stage: 'complete', blockers: [], verified_at: '2099-01-01T00:00:00Z', client_reported_ready: true } });
        }
        return respond(response, 200, target);
      }
      if (child === 'enrollments') {
        if (id === 'retry-target') {
          retryEnrollmentAttempts += 1;
          if (retryEnrollmentAttempts === 1) return respond(response, 409, { error: { code: 'CAPABILITY_UNAVAILABLE', message: '개인 환경 설치 서버가 구성되지 않았습니다.' } });
        }
        return respond(response, 201, { id: `enrollment-${id}`, target_id: id, expires_at: '2099-01-01T00:00:00Z', install_command: `curl -fsSL https://example.test/${id}/install | sh` });
      }
      if (child === 'observations') return respond(response, 200, { target_id: id, stale_after_seconds: 90, metrics: { cpu_percent: { state: 'ready', value: 10, observed_at: '2026-10-03T01:00:00Z' }, memory_percent: { state: 'ready', value: 20, observed_at: '2026-10-03T01:00:00Z' }, disk_percent: { state: 'ready', value: 30, observed_at: '2026-10-03T01:00:00Z' } } });
      if (child === 'applications') return respond(response, 200, { items: [{ id: 'app-1', name: 'railshot-app', status: 'running', deployed_at: '2026-10-03T01:00:00Z' }] });
      if (child === 'plans') { calls.push({ kind: 'delete-plan', body: json() }); await deletePlanGate; return respond(response, 201, { id: 'delete-plan', target_id: id, action: 'delete', plan_hash: 'signed-plan', expires_at: '2099-01-01T00:00:00Z', resources: ['railshot-app', '앱 전용 데이터'], retained: ['고객 별도 서비스', '기반 VM', '공유 데이터'], blockers: [] }); }
      if (child === 'operations') {
        if (json().action === 'resume') {
          calls.push({ kind: 'resume-operation', body: json() }); deletionResumed = true; target.status = 'deleting';
          return respond(response, 202, { id: 'operation-1', kind: 'target-lifecycle', target_id: id, status: 'running', stage: 'client' }, '/api/v1/operations/operation-1');
        }
        if (id === 'pending-target') {
          calls.push({ kind: 'pending-delete-operation', body: json() }); Object.assign(target, { status: 'deleting', deletion_operation_id: 'operation-pending' });
          return respond(response, 202, { id: 'operation-pending', status: 'running' }, '/api/v1/operations/operation-pending');
        }
        calls.push({ kind: 'delete-operation', body: json() }); Object.assign(target, { status: 'attention', deletion_operation_id: 'operation-1' });
        return respond(response, 202, { id: 'operation-1', status: 'running' }, '/api/v1/operations/operation-1');
      }
      if (child === 'reconciliations') {
        if (json().scope === 'runtime') {
          calls.push({ kind: 'runtime-reconciliation', body: json() });
          target.runtime_preparation = { ...target.runtime_preparation, status: 'queued', stage: 'reconciliation', blockers: [] };
          return respond(response, 202, { target_id: id, generation: 1, status: 'preparing', connection_status: 'ready', deployable: false,
            runtime_preparation: target.runtime_preparation });
        }
        calls.push({ kind: 'delete-reconciliation', body: json() }); deletionReconciled = true;
        return respond(response, 202, { id: 'operation-1', kind: 'target-lifecycle', target_id: id, status: 'unknown', stage: 'reconciliation', residuals: [{ kind: 'RemovalVerification', name: id }],
          reconciliation: { id: 'reconciliation-1', status: 'pending', expires_at: '2099-01-01T00:00:00Z', resumable: false, blockers: [] } }, '/api/v1/operations/operation-1');
      }
    }
    if (path === '/api/v1/operations/operation-1') {
      if (!ownsTargets) return respond(response, 404, { error: { message: 'missing operation' } });
      if (deletionResumed) {
        const target = targets.find((item) => item.id === 'ready-target'); target.status = 'deleted'; failNextDeletedList = true;
        return respond(response, 200, { id: 'operation-1', kind: 'target-lifecycle', target_id: 'ready-target', status: 'succeeded', stage: 'complete', steps: [], residuals: [] });
      }
      return respond(response, 200, { id: 'operation-1', kind: 'target-lifecycle', target_id: 'ready-target', status: 'unknown', stage: 'reconciliation',
        error: { code: 'REMOVAL_UNVERIFIED', message: '남은 서비스·클라이언트를 확인해야 합니다. 자동 재실행하지 않습니다.' },
        steps: [{ name: 'client', status: 'unknown' }, { name: 'gateway', status: 'unknown' }], residuals: [{ kind: 'RemovalVerification', name: 'ready-target' }],
        ...(deletionReconciled ? { reconciliation: { id: 'reconciliation-1', status: 'ready', expires_at: '2099-01-01T00:00:00Z', resumable: true, blockers: [] } } : {}) });
    }
    if (path === '/api/v1/operations/operation-complete') {
      if (!ownsTargets) return respond(response, 404, { error: { message: 'missing operation' } });
      const target = targets.find((item) => item.id === 'completed-target'); target.status = 'deleted';
      return respond(response, 200, { id: 'operation-complete', kind: 'target-lifecycle', target_id: 'completed-target', status: 'succeeded', stage: 'complete', steps: [], residuals: [] });
    }
    if (path === '/api/v1/operations/operation-pending') {
      if (!ownsTargets) return respond(response, 404, { error: { message: 'missing operation' } });
      const target = targets.find((item) => item.id === 'pending-target'); target.status = 'deleted';
      return respond(response, 200, { id: 'operation-pending', kind: 'target-lifecycle', target_id: 'pending-target', status: 'succeeded', stage: 'complete', steps: [], residuals: [] });
    }
    const files = { '/': ['index.html', 'text/html'], '/app.js': ['app.js', 'text/javascript'], '/styles.css': ['styles.css', 'text/css'],
      '/contracts/application.mjs': ['../../contracts/application.mjs', 'text/javascript'],
      '/src/api.js': ['src/api.js', 'text/javascript'], '/src/lifecycle.js': ['src/lifecycle.js', 'text/javascript'],
      '/src/insights.js': ['src/insights.js', 'text/javascript'],
      '/src/insights-view.js': ['src/insights-view.js', 'text/javascript'],
      '/src/deployment-history.js': ['src/deployment-history.js', 'text/javascript'], '/src/recovery.js': ['src/recovery.js', 'text/javascript'] };
    if (!files[path]) return respond(response, 404, { error: { message: 'fixture path unavailable' } });
    response.writeHead(200, { 'content-type': files[path][1] }); response.end(await readFile(new URL('../../apps/dashboard/' + files[path][0], import.meta.url)));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined });
  t.after(async () => { await browser.close(); await new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); }); });
  const page = await browser.newPage(); page.setDefaultTimeout(10000); const origin = `http://127.0.0.1:${server.address().port}`;
  const errors = []; page.on('pageerror', (error) => errors.push(error.message)); await page.goto(origin);

  await page.getByRole('button', { name: '개인 배포환경 관리' }).click();
  await page.getByLabel('환경 이름').fill('새 OpenStack');
  await page.getByRole('button', { name: '등록 시작' }).click();
  await page.getByText('환경 등록 실패: 개인 실행환경 운영 설정이 없습니다.').waitFor();
  assert.equal(ownerConfigured, false, 'readiness 실패는 소유권 생성보다 먼저 차단해야 합니다.');
  assert.equal(calls.some((call) => call.kind === 'create-target'), false);
  assert.equal(await page.locator('#environment-recovery-key').isHidden(), true);
  personalReady = true;
  await page.getByRole('button', { name: '등록 시작' }).click();
  await page.getByText('복구 키를 안전한 곳에 저장하세요').waitFor();
  assert.equal(await page.locator('#environment-recovery-key-value').innerText(), 'recover-this-once');
  await page.getByRole('heading', { name: '관리 호스트 설치' }).waitFor();
  assert.match(await page.locator('#environment-install-command').innerText(), /curl -fsSL/);
  assert.deepEqual(calls.find((call) => call.kind === 'create-target').body, { label: '새 OpenStack', provider: 'openstack' });
  assert.match(await page.locator('#environment-register-message').innerText(), /현재 브라우저에 관리 권한이 연결되었습니다/);
  assert.match(await page.locator('#environment-owner-note').innerText(), /현재 브라우저에 개인 환경 관리 권한이 연결되어 있습니다/);
  assert.match(await page.locator('#environment-owned-message').innerText(), /목록 조회 실패/);
  await page.getByRole('button', { name: '새 OpenStack' }).waitFor();
  await page.getByText('복구 키로 관리권 복구').click();
  await page.locator('#environment-recovery-input').fill('recover-this-once');
  await page.getByRole('button', { name: '관리권 복구' }).click();
  await page.getByText('이 브라우저에서 관리권을 복구했습니다.').waitFor();
  assert.equal(await page.locator('#environment-recovery-key-value').innerText(), 'rotated-recovery-key-1');
  assert.match(await page.locator('#environment-recover-message').innerText(), /등록한 환경 6개를 불러왔습니다/);
  assert.equal(await page.locator('#environment-owned-list-panel').evaluate((node) => node.classList.contains('recovery-highlight')), true);
  assert.equal(await page.locator('#environment-owned-items button.active').innerText(), '연구실 OpenStack');
  assert.equal(await page.locator('#environment-install-command').isHidden(), true, '복구 전 설치 명령은 새 소유권에 남지 않아야 합니다.');
  assert.equal(await page.locator('#environment-copy-command').isDisabled(), true);

  await page.getByRole('button', { name: '준비 확인 OpenStack' }).click();
  assert.match(await page.getByRole('button', { name: '준비 확인 OpenStack' }).locator('..').innerText(), /전체 서버 배포 준비 중/);
  await page.getByText('진행 차단 · 중앙 배포 설정 확인').waitFor();
  await page.getByText('중앙 서버에 OpenStack 배포 준비 설정이 없습니다. 운영자 설정이 필요합니다.').waitFor();
  await page.getByText('확인 필요 (RUNTIME_FUTURE_CODE)').waitFor();
  await page.getByRole('button', { name: '준비 다시 확인' }).click();
  assert.deepEqual(calls.find((call) => call.kind === 'runtime-reconciliation').body, { scope: 'runtime' });
  await page.getByText('완료 · 준비 완료').waitFor();

  await page.getByRole('button', { name: '기존 등록 OpenStack' }).click();
  assert.match(await page.locator('#environment-detail-facts').innerText(), /등록 단계\s+OpenStack 등록 완료/);
  assert.doesNotMatch(await page.locator('#environment-detail-facts').innerText(), /등록·배포 준비 완료/);

  await page.getByRole('button', { name: '대기 중 OpenStack' }).click();
  await page.getByText('시작 전 · 서버 배포 준비 시작 전').waitFor();
  await page.getByText('등록 대기 환경입니다. 설치 명령을 다시 발급할 수 있습니다.').waitFor();
  assert.equal(await page.locator('#environment-copy-command').isDisabled(), true);
  await page.getByRole('button', { name: '설치 명령 재발급' }).click();
  await page.getByText('개인 환경 설치 서버가 구성되지 않았습니다.').waitFor();
  assert.equal(await page.locator('#environment-enrollment').isHidden(), false);
  assert.equal(await page.locator('#environment-install-command').isHidden(), true);
  assert.equal(await page.locator('#environment-copy-command').isDisabled(), true);
  if (process.env.PERSONAL_UI_SCREENSHOT) await page.screenshot({ path: process.env.PERSONAL_UI_SCREENSHOT, fullPage: true });
  await page.getByRole('button', { name: '설치 명령 재발급' }).click();
  await page.locator('#environment-install-command').getByText(/retry-target/).waitFor();
  assert.equal(await page.locator('#environment-copy-command').isDisabled(), false);
  await page.getByRole('button', { name: '설치 명령 복사' }).click();

  await page.locator('#environment-recovery-input').fill('recover-list-failure');
  await page.getByRole('button', { name: '관리권 복구' }).click();
  await page.getByText('관리권은 복구했으나 환경 목록을 불러오지 못했습니다. 목록 새로고침으로 다시 확인하세요.').waitFor();
  assert.match(await page.locator('#environment-owned-message').innerText(), /목록 조회 실패/);
  assert.doesNotMatch(await page.locator('#environment-recover-message').innerText(), /환경 0개를 불러왔습니다/);
  await page.getByRole('button', { name: '목록 새로고침' }).click();
  await page.getByRole('button', { name: '연구실 OpenStack' }).waitFor();

  await page.getByRole('button', { name: '새 배포' }).click();
  await page.locator('#repository-url').fill('https://github.com/example/my-app');
  await page.locator('[name="environment"][value="onprem"]').check();
  await page.locator('#provider').selectOption('__new_openstack__');
  await page.getByText('개인 배포환경 관리로 이동').click();
  await page.getByRole('button', { name: '새 배포' }).click();
  assert.equal(await page.locator('#repository-url').inputValue(), 'https://github.com/example/my-app');
  await page.locator('#provider').selectOption('ready-target');
  await page.getByRole('button', { name: /선택 내용 확인/ }).click();
  await page.getByRole('button', { name: '배포 시작' }).click();
  assert.deepEqual(calls.find((call) => call.kind === 'deployment'), { kind: 'deployment', environment: 'onprem', provider: 'openstack', target: 'ready-target' });

  await page.getByRole('button', { name: '개인 배포환경 관리' }).click();
  await page.getByRole('button', { name: '연구실 OpenStack' }).click();
  await page.getByRole('button', { name: '환경 삭제' }).click();
  await page.getByRole('dialog').getByText('최신 삭제 계획을 확인하고 있습니다.').waitFor();
  assert.equal(await page.locator('#environment-delete-confirmation').isDisabled(), true);
  releaseDeletePlan();
  await page.getByRole('dialog').getByText('앱 전용 데이터', { exact: true }).waitFor();
  assert.deepEqual(calls.find((call) => call.kind === 'delete-plan').body, { action: 'delete', delete_data: true });
  await page.locator('#environment-delete-confirmation').fill('연구실 OpenStack');
  await page.locator('#environment-delete-data-consent').check();
  await page.getByRole('button', { name: '삭제 시작' }).click();
  assert.deepEqual(calls.find((call) => call.kind === 'delete-operation').body, { action: 'delete', plan_id: 'delete-plan', plan_hash: 'signed-plan', confirmation: '연구실 OpenStack', delete_data: true });
  await page.getByText('작업 operation-1 · 결과 확인 필요 · 남은 항목 확인 필요').waitFor();
  await page.getByText('남은 서비스·클라이언트를 확인해야 합니다. 자동 재실행하지 않습니다.').waitFor();
  await page.getByText('관리 클라이언트 제거 · 상태 확인 필요').waitFor();
  await page.getByText('보안 연결 제거 · 상태 확인 필요').waitFor();
  await page.getByText('클라이언트·보안 연결 제거 확인 · ready-target').waitFor();
  assert.equal(await page.getByRole('button', { name: '삭제 상태 확인' }).isVisible(), true);

  await page.getByRole('button', { name: '삭제 재개 가능 여부 확인' }).click();
  assert.deepEqual(calls.find((call) => call.kind === 'delete-reconciliation').body, { operation_id: 'operation-1' });
  await page.getByRole('button', { name: '삭제 재개 가능 여부 확인 중' }).waitFor();
  assert.equal(await page.getByRole('button', { name: '삭제 재개 가능 여부 확인 중' }).isDisabled(), true);
  await page.getByRole('button', { name: '환경 삭제 재개' }).waitFor();
  await page.getByRole('button', { name: '환경 삭제 재개' }).click();
  await page.locator('#environment-delete-confirmation').fill('연구실 OpenStack');
  await page.locator('#environment-delete-data-consent').check();
  await page.getByRole('button', { name: '삭제 재개 실행' }).click();
  assert.deepEqual(calls.find((call) => call.kind === 'resume-operation').body, { action: 'resume', operation_id: 'operation-1', reconciliation_id: 'reconciliation-1', confirmation: '연구실 OpenStack', delete_data: true });
  await page.getByText('환경 삭제를 완료했습니다. 등록 목록과 배포 대상에서 제거했습니다.').waitFor();
  assert.equal(await page.getByRole('button', { name: '연구실 OpenStack' }).count(), 0);
  assert.equal(await page.locator('#provider option[value="ready-target"]').count(), 0);
  assert.match(await page.locator('#environment-owned-message').innerText(), /목록 조회 실패/,
    '삭제 완료 직후 목록 재조회가 실패해도 확인된 삭제 대상을 로컬 목록에서 제거해야 합니다.');
  assert.match(await page.locator('#environment-register-message').innerText(), /새 OpenStack 환경을 만들었습니다/,
    '다른 환경을 삭제할 때 등록 성공 문구를 지우지 않아야 합니다.');

  await page.getByRole('button', { name: '삭제 완료 예정 OpenStack' }).click();
  await page.getByText('환경 삭제를 완료했습니다. 등록 목록과 배포 대상에서 제거했습니다.').waitFor();
  assert.equal(await page.getByRole('button', { name: '삭제 완료 예정 OpenStack' }).count(), 0);
  assert.equal(await page.locator('#provider option[value="completed-target"]').count(), 0);

  await page.getByRole('button', { name: '새 OpenStack' }).click();
  await page.getByRole('button', { name: '설치 명령 재발급' }).click();
  await page.locator('#environment-install-command').getByText(/pending-target/).waitFor();
  assert.match(await page.locator('#environment-register-message').innerText(), /새 OpenStack 환경을 만들었습니다/);
  await page.getByRole('button', { name: '환경 삭제' }).click();
  await page.getByRole('dialog').getByText('앱 전용 데이터', { exact: true }).waitFor();
  await page.locator('#environment-delete-confirmation').fill('새 OpenStack');
  await page.locator('#environment-delete-data-consent').check();
  await page.getByRole('button', { name: '삭제 시작' }).click();
  await page.getByText('환경 삭제를 완료했습니다. 등록 목록과 배포 대상에서 제거했습니다.').waitFor();
  assert.equal(await page.locator('#environment-enrollment').isHidden(), true);
  assert.equal(await page.locator('#environment-install-command').innerText(), '');
  assert.equal(await page.locator('#environment-register-message').innerText(), '');
  assert.equal(await page.locator('#provider option[value="pending-target"]').count(), 0);

  const currentRecoveryKey = await page.locator('#environment-recovery-key-value').innerText();
  const recoveredPage = await browser.newPage();
  await recoveredPage.goto(origin);
  await recoveredPage.getByRole('button', { name: '개인 배포환경 관리' }).click();
  await recoveredPage.getByText('복구 키로 관리권 복구').click();
  await recoveredPage.locator('#environment-recovery-input').fill(currentRecoveryKey);
  await recoveredPage.getByRole('button', { name: '관리권 복구' }).click();
  await recoveredPage.getByText('이 브라우저에서 관리권을 복구했습니다.').waitFor();
  await page.getByRole('button', { name: '목록 새로고침' }).click();
  await page.getByText('아직 등록한 OpenStack 환경이 없습니다.').waitFor();
  assert.equal(await page.locator('#environment-owned-items button').count(), 0, '복구 후 이전 브라우저의 관리 권한이 폐기되어야 합니다.');
  await recoveredPage.close();
  assert.deepEqual(errors, []);
});
