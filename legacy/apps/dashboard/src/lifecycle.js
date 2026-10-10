import { request } from './api.js';

export const applicationStates = { ready: '실행 중', stopped: '중지됨', deleted: '삭제됨', unknown: '확인 필요',
  stopping: '중지 중', starting: '재개 중', deleting: '삭제 중', queued: '배포 대기', registering: '배포 준비 중' };

export function applicationLabel(app) {
  if (app.status !== 'ready') return applicationStates[app.status] || '상태 확인 필요';
  if (app.current_deployment_state === 'unverified') return '배포 결과 확인 필요';
  if (app.current_deployment) return '배포 확인됨';
  if (['queued', 'running'].includes(app.latest_deployment?.status)) return '배포 중';
  if (['failed', 'blocked', 'cancelled'].includes(app.latest_deployment?.status)) return '배포 실패';
  return '미배포';
}

export function createLifecycleController({ getApplications, getCurrent, getApplicationDetail, isSubmitting, isResuming,
  clearApplicationDetail, isApplicationsLoading, applicationSite, hasObservationError, isHistoryHidden,
  loadApplications, renderApplications, element, formatTime }) {
  // Lifecycle actions bind only to the latest session-owned application inventory.
  const lifecycleDialog = document.querySelector('#lifecycle-dialog');
  const lifecycleNames = { stop: '중지', start: '재개', delete: '삭제' };
  let lifecycleDraft, lifecycleBusy = false, lifecycleTimer, lifecycleReadBusy = false;
  let lifecycleOperation = null;
  try { lifecycleOperation = JSON.parse(sessionStorage.getItem('railshot.application-operation') || 'null'); } catch { /* No writes are replayed on reload. */ }
  function storeLifecycleOperation() {
    try { sessionStorage.setItem('railshot.application-operation', JSON.stringify(lifecycleOperation)); } catch { /* The server remains the authority. */ }
  }
  function applicationBusy(id, ownSubmission = false) {
    return (!ownSubmission && isSubmitting()) || Boolean(isResuming()) || lifecycleBusy
      || ['queued', 'running'].includes(lifecycleOperation?.status)
      || Boolean(lifecycleOperation && lifecycleOperation.application_id === id
        && (!['succeeded', 'failed', 'blocked'].includes(lifecycleOperation.status) || lifecycleOperation.error?.outcome_unknown || lifecycleOperation.residuals?.length));
  }
  function updateBlocked(app) {
    return applicationBusy(app?.id) ? '진행 중이거나 결과를 확인해야 하는 앱 관리 작업이 있습니다.'
      : app?.current_deployment_state === 'unverified' ? '현재 적용 결과가 불확실하여 운영자 확인 후 업데이트할 수 있습니다.'
      : app?.status !== 'ready' ? '앱이 실행 가능한 준비 상태일 때 업데이트할 수 있습니다.'
      : !app.current_deployment ? '업데이트 기준으로 사용할 성공한 배포가 없습니다.' : '';
  }
  function applicationAllowed(app, action) {
    return !applicationBusy(app.id)
      && (action === 'delete' || !['not_deployed', 'unverified'].includes(app.current_deployment_state))
      && (action === 'start' ? app.status === 'stopped' : action === 'stop' ? app.status === 'ready' : ['ready', 'stopped', 'queued', 'running', 'registering'].includes(app.status));
  }
  function applicationButtons(app) {
    const actions = element('div', '', 'history-actions');
    for (const action of ['stop', 'start', 'delete']) {
      const button = element('button', action === 'delete' ? '🗑 삭제' : lifecycleNames[action], action === 'delete' ? 'text-button danger-button' : 'text-button');
      button.type = 'button'; button.disabled = !applicationAllowed(app, action);
      button.setAttribute('aria-label', `${app.app} ${lifecycleNames[action]}${action === 'delete' ? ' · 데이터도 영구 삭제' : ''}`);
      button.addEventListener('click', () => reviewApplication(app.id, action)); actions.append(button);
    }
    return actions;
  }
  function renderApplicationActions() {
    const app = getApplications().find((row) => row.id === getCurrent()?.application_id);
    for (const id of ['run-application-actions', 'monitor-application-actions']) {
      const holder = document.getElementById(id); holder.replaceChildren();
      if (app) holder.append(element('strong', `${app.app} · ${applicationLabel(app)}`), applicationButtons(app));
      else if (getCurrent()?.kind === 'deployments') holder.append(element('span', '이 배포의 앱 관리 ID를 최신 목록에서 확인하지 못했습니다. 자동 삭제를 지원하지 않습니다.', 'field-note'));
    }
    const detail = getApplications().find((row) => row.id === getApplicationDetail()?.id);
    if (!isApplicationsLoading() && getApplicationDetail() && !detail) clearApplicationDetail();
    document.querySelector('#detail-application-site').replaceChildren(...(detail ? [applicationSite(detail)] : []));
    document.querySelector('#detail-application-actions').replaceChildren(...(detail ? [applicationButtons(detail)] : []));
    document.querySelector('#application-update').disabled = !detail || Boolean(updateBlocked({ ...getApplicationDetail(), ...detail }));
    if (getCurrent()) document.querySelector('#resume-run').disabled = hasObservationError() || applicationBusy(getCurrent().application_id);
  }
  function lifecycleResources(selector, rows, empty) {
    document.querySelector(selector).replaceChildren(...(rows?.length ? rows.map((row) => element('li', `${row.kind === 'CIBinding' ? 'CI 배포 연결' : row.kind} · ${row.namespace ? row.namespace + '/' : ''}${row.name}`)) : [element('li', empty)]));
  }
  function resourceList(rows) { return Array.isArray(rows) && rows.every((row) => row && typeof row.kind === 'string' && typeof row.name === 'string' && (row.namespace == null || typeof row.namespace === 'string')); }
  function lifecycleError(message) { const error = document.querySelector('#lifecycle-error'); error.textContent = message; error.hidden = !message; }
  function lifecycleConfirmState() {
    const draft = lifecycleDraft;
    const valid = draft?.plan && Date.parse(draft.plan.expires_at) > Date.now();
    document.querySelector('#lifecycle-confirm').disabled = lifecycleBusy || !valid;
    if (draft?.plan && !valid) { lifecycleError('계획이 만료됐습니다. 계획 다시 확인으로 새 계획을 확인하세요.'); document.querySelector('#lifecycle-retry').hidden = false; }
  }
  function rememberPlan(draft, id) {
    draft.pending = id;
    try { sessionStorage.setItem('railshot.application-plan', JSON.stringify({ application_id: draft.id, action: draft.action, id })); } catch { /* The server retains the plan. */ }
  }
  function clearPlan(draft) {
    draft.pending = null;
    try { sessionStorage.removeItem('railshot.application-plan'); } catch { /* A stale cached plan remains subject to server checks. */ }
  }
  function showPlan(draft, plan) {
    if (plan.application_id !== draft.id || plan.action !== draft.action || !/^[a-f0-9-]{36}$/.test(plan.id || ''))
      throw new Error('앱과 일치하는 실행 계획을 확인하지 못했습니다.');
    rememberPlan(draft, plan.id);
    if (plan.status === 'failed') {
      clearPlan(draft);
      throw new Error(`${plan.error?.message || '실행 계획을 확인하지 못했습니다.'}${plan.error?.code ? ' (' + plan.error.code + ')' : ''}`);
    }
    if (plan.status === 'planning') {
      draft.startedAt = Date.parse(plan.created_at) || draft.startedAt;
      document.querySelector('#lifecycle-description').textContent = '서버에서 앱 전용 자원과 공용 자원을 확인하고 있습니다. 아직 앱 변경은 실행되지 않았습니다.';
      draft.pollTimer = setTimeout(() => readPlan(draft), 2000);
      return;
    }
    if ((plan.status && plan.status !== 'ready') || !/^[a-f0-9]{64}$/.test(plan.plan_hash || '')
        || !Number.isFinite(Date.parse(plan.expires_at)) || !resourceList(plan.resources) || !resourceList(plan.retained))
      throw new Error('앱과 일치하는 실행 계획을 확인하지 못했습니다.');
    clearPlan(draft); draft.plan = plan; draft.loading = false;
    document.querySelector('#lifecycle-progress').hidden = true;
    document.querySelector('#lifecycle-title').textContent = `${draft.app.app} ${lifecycleNames[draft.action]}`;
    document.querySelector('#lifecycle-description').textContent = draft.action === 'delete'
      ? '데이터도 영구 삭제됩니다. 진행 중인 배포는 중단을 확인한 뒤 정리합니다. 앱 전용 Pod·네트워크·스토리지와 데이터를 영구 삭제합니다. 삭제한 데이터는 복구할 수 없습니다. 공용 노드와 공용 로드밸런서는 보존합니다.'
      : draft.action === 'stop' ? '앱 실행을 중지합니다. 데이터와 스토리지는 보존하며, 다시 사용하려면 재개를 선택하세요.' : '보존된 앱 설정과 데이터로 실행을 재개합니다.';
    document.querySelector('#lifecycle-expiry').textContent = `계획 유효 기한: ${formatTime(plan.expires_at)}`;
    lifecycleResources('#lifecycle-resources', plan.resources, '변경할 리소스 없음');
    lifecycleResources('#lifecycle-retained', plan.retained, '서버가 별도로 표시한 보존 리소스 없음');
    lifecycleDialog.setAttribute('aria-busy', 'false'); lifecycleConfirmState();
  }
  function planFailure(draft, cause) {
    if (lifecycleDraft !== draft || !lifecycleDialog.open) return;
    draft.loading = false; clearTimeout(draft.pollTimer);
    document.querySelector('#lifecycle-progress').hidden = true;
    document.querySelector('#lifecycle-description').textContent = '계획 확인을 완료하지 못했습니다. 앱 변경은 실행되지 않았습니다.';
    lifecycleError(cause.name === 'AbortError' ? '서버 응답을 기다리는 시간이 초과됐습니다. 계획 다시 확인을 눌러 상태를 조회하세요.' : cause.message);
    document.querySelector('#lifecycle-retry').hidden = false;
    lifecycleDialog.setAttribute('aria-busy', 'false'); lifecycleConfirmState();
  }
  async function readPlan(draft) {
    if (lifecycleDraft !== draft || !lifecycleDialog.open) return;
    try {
      if (Date.now() - draft.startedAt > 660000) throw new Error('계획 확인이 예상보다 오래 걸립니다. 계획 다시 확인으로 서버 상태를 조회하세요.');
      const { data } = await request(`/api/v1/applications/${encodeURIComponent(draft.id)}/plans/${encodeURIComponent(draft.pending)}`, {}, draft.controller);
      if (lifecycleDraft !== draft || !lifecycleDialog.open) return;
      if (data.id !== draft.pending) throw new Error('계획 조회 결과가 일치하지 않습니다.');
      showPlan(draft, data);
    } catch (cause) {
      if (lifecycleDraft !== draft || !lifecycleDialog.open) return;
      if (cause.status === 404) clearPlan(draft);
      planFailure(draft, cause);
    }
  }
  async function preparePlan(draft) {
    draft.controller?.abort(); clearTimeout(draft.pollTimer);
    draft.controller = new AbortController(); draft.loading = true; draft.startedAt = Date.now();
    draft.plan = null; lifecycleError(''); lifecycleConfirmState();
    document.querySelector('#lifecycle-retry').hidden = true;
    document.querySelector('#lifecycle-description').textContent = '최신 앱 상태를 확인하고 있습니다. 아직 앱 변경은 실행되지 않았습니다.';
    document.querySelector('#lifecycle-expiry').textContent = '';
    lifecycleResources('#lifecycle-resources', [], '계획 확인 후 표시됩니다.');
    lifecycleResources('#lifecycle-retained', [], '계획 확인 후 표시됩니다.');
    lifecycleDialog.setAttribute('aria-busy', 'true');
    try {
      if (!await loadApplications()) throw new Error('최신 앱 목록을 확인한 뒤 다시 시도하세요.');
      if (lifecycleDraft !== draft || !lifecycleDialog.open) return;
      const app = getApplications().find((row) => row.id === draft.id);
      if (!app || !applicationAllowed(app, draft.action)) throw new Error('현재 앱 상태에서는 이 작업을 실행할 수 없습니다. 진행 중인 작업이나 배포 상태를 확인하세요.');
      draft.app = app;
      if (draft.pending) { await readPlan(draft); return; }
      const { data, status, location } = await request(`/api/v1/applications/${encodeURIComponent(draft.id)}/plans`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', Prefer: 'respond-async' }, body: JSON.stringify({ action: draft.action }) }, draft.controller);
      if (lifecycleDraft !== draft || !lifecycleDialog.open) return;
      if (status === 202 && location !== `/api/v1/applications/${draft.id}/plans/${data.id}`) throw new Error('계획 접수 결과를 확인하지 못했습니다.');
      showPlan(draft, data);
    } catch (cause) { planFailure(draft, cause); }
  }
  async function reviewApplication(id, action) {
    if (lifecycleBusy || lifecycleDialog.open) return;
    const opener = document.activeElement;
    const draft = { id, action, opener, openerLabel: opener?.getAttribute('aria-label') };
    try {
      const saved = JSON.parse(sessionStorage.getItem('railshot.application-plan') || 'null');
      if (saved?.application_id === id && saved.action === action && /^[a-f0-9-]{36}$/.test(saved.id || '')) draft.pending = saved.id;
    } catch { /* Start a fresh read-only plan when no saved reference exists. */ }
    lifecycleDraft = draft;
    document.querySelector('#lifecycle-title').textContent = `앱 ${lifecycleNames[action]} 계획`;
    document.querySelector('#lifecycle-confirm').textContent = action === 'delete' ? '영구 삭제' : `앱 ${lifecycleNames[action]}`;
    document.querySelector('#lifecycle-confirm').classList.toggle('danger-button', action === 'delete');
    lifecycleDialog.showModal();
    draft.clock = setInterval(() => {
      if (lifecycleDraft !== draft) return;
      const progress = document.querySelector('#lifecycle-progress');
      progress.hidden = !draft.loading;
      if (draft.loading) progress.textContent = `계획 확인 중 · ${Math.floor((Date.now() - draft.startedAt) / 1000)}초 경과. 최대 10분 걸릴 수 있습니다. 창을 닫아도 삭제는 실행되지 않습니다.`;
      lifecycleConfirmState();
    }, 1000);
    await preparePlan(draft);
  }
  function renderLifecycleOperation() {
    const operation = lifecycleOperation;
    document.querySelector('#lifecycle-operation').hidden = !operation;
    if (!operation) { renderApplications(); return; }
    const title = `${operation.app || '앱'} ${lifecycleNames[operation.action] || '관리'}`;
    document.querySelector('#lifecycle-operation-title').textContent = title;
    document.querySelector('#lifecycle-operation-state').textContent = `${({ queued: '접수됨', running: '실행 중', succeeded: '완료', blocked: '실행 차단', failed: '실행 실패', unknown: '결과 확인 필요' })[operation.status] || '결과 확인 필요'}${operation.stage ? ' · ' + operation.stage : ''}`;
    document.querySelector('#lifecycle-operation-message').textContent = operation.readError || operation.error?.message ||
      (operation.status === 'succeeded' ? '서버가 실행 완료를 확인했습니다.' : ['queued', 'running'].includes(operation.status)
        ? '15초마다 상태를 확인합니다. 페이지를 닫아도 서버 작업은 계속됩니다.' : '완료로 확인되지 않았습니다. 자동으로 다시 실행하지 않습니다. 남은 자원과 서버의 실행 상태를 확인하세요.');
    document.querySelector('#lifecycle-operation-steps').replaceChildren(...(Array.isArray(operation.steps) ? operation.steps : []).map((step) => element('li', `${step.name}: ${step.status}`)));
    document.querySelector('#lifecycle-residuals').hidden = !operation.residuals?.length;
    lifecycleResources('#lifecycle-residual-list', operation.residuals, '');
    document.querySelector('#lifecycle-operation-refresh').disabled = lifecycleReadBusy || !(operation.id || operation.plan_id);
    document.querySelector('#lifecycle-operation').setAttribute('aria-busy', String(lifecycleReadBusy || ['queued', 'running'].includes(operation.status)));
    renderApplications();
  }
  async function refreshLifecycleOperation() {
    clearTimeout(lifecycleTimer);
    const previous = lifecycleOperation;
    if (!(previous?.id || previous?.plan_id) || lifecycleReadBusy) return;
    lifecycleReadBusy = true; renderLifecycleOperation();
    try {
      let operationId = previous.id;
      if (!operationId) {
        const { data: plan } = await request(`/api/v1/applications/${encodeURIComponent(previous.application_id)}/plans/${encodeURIComponent(previous.plan_id)}`);
        if (lifecycleOperation !== previous) return;
        if (plan.id !== previous.plan_id || plan.application_id !== previous.application_id || plan.action !== previous.action)
          throw new Error('접수 확인의 앱과 계획이 일치하지 않습니다.');
        if (!/^[A-Za-z0-9._-]+$/.test(plan.operation_id || '')) throw new Error('서버에서 접수된 실행을 아직 찾지 못했습니다. 변경 요청을 다시 보내지 않고 확인합니다.');
        operationId = plan.operation_id;
      }
      const { data } = await request(`/api/v1/operations/${encodeURIComponent(operationId)}`);
      if (lifecycleOperation !== previous) return;
      if (data.id !== operationId || (previous.plan_id && data.plan_id !== previous.plan_id) || data.application_id !== previous.application_id || data.action !== previous.action
          || !['queued', 'running', 'succeeded', 'blocked', 'failed', 'unknown'].includes(data.status)
          || !resourceList(data.residuals || [])) throw new Error('실행 결과의 앱 정보가 일치하지 않습니다.');
      lifecycleOperation = { ...previous, ...data, readError: null }; storeLifecycleOperation();
      if (['queued', 'running'].includes(data.status)) lifecycleTimer = setTimeout(refreshLifecycleOperation, 15000);
      else await loadApplications();
    } catch (cause) {
      if (lifecycleOperation === previous) {
        lifecycleOperation = { ...previous, readError: `상태 조회 실패: ${cause.message} 마지막 확인 상태를 유지하며 15초 뒤 다시 조회합니다.` };
        storeLifecycleOperation(); lifecycleTimer = setTimeout(refreshLifecycleOperation, 15000);
      }
    } finally { lifecycleReadBusy = false; renderLifecycleOperation(); }
  }
  document.querySelector('#lifecycle-form').addEventListener('submit', async (event) => {
    event.preventDefault(); lifecycleConfirmState();
    const draft = lifecycleDraft;
    if (document.querySelector('#lifecycle-confirm').disabled || !draft?.plan) return;
    lifecycleBusy = true; lifecycleConfirmState(); renderApplications(); lifecycleDialog.setAttribute('aria-busy', 'true');
    document.querySelector('#lifecycle-cancel').disabled = true;
    let submitted = false;
    const previousOperation = lifecycleOperation;
    try {
      if (!await loadApplications()) throw new Error('최신 앱 상태를 확인하지 못했습니다. 요청을 보내지 않았습니다.');
      const app = getApplications().find((row) => row.id === draft.id);
      if (!app || app.app !== draft.app.app || app.status !== draft.app.status || Date.parse(draft.plan.expires_at) <= Date.now()) throw new Error('앱 상태가 바뀌었거나 계획이 만료됐습니다. 취소 후 새 계획을 확인하세요.');
      lifecycleOperation = { application_id: app.id, app: app.app, action: draft.action, status: 'unknown', id: null, plan_id: draft.plan.id };
      storeLifecycleOperation(); submitted = true;
      const { data, location, status } = await request(`/api/v1/applications/${encodeURIComponent(app.id)}/operations`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID() },
        body: JSON.stringify({ action: draft.action, plan_id: draft.plan.id, plan_hash: draft.plan.plan_hash,
          confirmation: app.app, ...(draft.action === 'delete' ? { delete_data: true } : {}) }) });
      if (![200, 202].includes(status) || !/^[A-Za-z0-9._-]+$/.test(data.id || '') || location !== `/api/v1/operations/${data.id}`
          || data.application_id !== app.id || data.action !== draft.action || !['queued', 'running', 'succeeded', 'blocked', 'failed', 'unknown'].includes(data.status)) throw new Error('접수 결과를 확인하지 못했습니다.');
      lifecycleOperation = { ...lifecycleOperation, ...data, application_id: app.id, app: app.app, action: draft.action };
      storeLifecycleOperation(); clearPlan(draft); lifecycleDialog.close(); refreshLifecycleOperation();
    } catch (cause) {
      if (submitted && cause.status >= 400 && cause.status < 500 && cause.outcomeUnknown === false) {
        lifecycleOperation = previousOperation; storeLifecycleOperation();
        lifecycleError(`요청이 접수되지 않았습니다: ${cause.message} 취소 후 새 계획을 확인하세요.`);
      } else if (submitted) {
        lifecycleOperation.readError = `요청 결과를 확인하지 못했습니다: ${cause.message} 변경 요청은 다시 보내지 않고 저장된 계획으로 접수 상태를 확인합니다.`;
        storeLifecycleOperation(); lifecycleDialog.close(); refreshLifecycleOperation();
      } else lifecycleError(cause.message);
    } finally {
      lifecycleBusy = false; document.querySelector('#lifecycle-cancel').disabled = false;
      lifecycleDialog.setAttribute('aria-busy', 'false'); lifecycleConfirmState(); renderLifecycleOperation();
    }
  });
  document.querySelector('#lifecycle-retry').addEventListener('click', () => {
    const draft = lifecycleDraft; if (!draft || draft.loading || lifecycleBusy) return;
    if (draft.plan && Date.parse(draft.plan.expires_at) <= Date.now()) clearPlan(draft);
    preparePlan(draft);
  });
  document.querySelector('#lifecycle-cancel').addEventListener('click', () => lifecycleDialog.close());
  lifecycleDialog.addEventListener('cancel', (event) => { if (lifecycleBusy) event.preventDefault(); });
  lifecycleDialog.addEventListener('keydown', (event) => {
    if (event.key !== 'Tab') return;
    const controls = [...lifecycleDialog.querySelectorAll('button:not(:disabled)')].filter((button) => !button.hidden);
    const first = controls[0], last = controls.at(-1);
    if (!first) { event.preventDefault(); return; }
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  lifecycleDialog.addEventListener('close', () => {
    const draft = lifecycleDraft; lifecycleDraft = null;
    draft?.controller?.abort(); clearTimeout(draft?.pollTimer); clearInterval(draft?.clock);
    document.querySelector('#lifecycle-progress').hidden = true;
    const opener = draft?.opener?.isConnected ? draft.opener : [...document.querySelectorAll('button[aria-label]')].find((button) => button.getAttribute('aria-label') === draft?.openerLabel && !button.disabled && button.getClientRects().length);
    (opener || (!isHistoryHidden() ? document.querySelector('#applications-refresh') : document.querySelector('#lifecycle-operation-refresh'))).focus();
  });
  document.querySelector('#lifecycle-operation-refresh').addEventListener('click', refreshLifecycleOperation);
  window.addEventListener('pagehide', () => { clearTimeout(lifecycleTimer); lifecycleDraft?.controller?.abort(); clearTimeout(lifecycleDraft?.pollTimer); clearInterval(lifecycleDraft?.clock); });

  return { applicationBusy, updateBlocked, applicationButtons, renderApplicationActions,
    renderLifecycleOperation, refreshLifecycleOperation, get operation() { return lifecycleOperation; } };
}
