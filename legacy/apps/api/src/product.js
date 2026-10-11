import { agentActivity } from './agent-activity.js';
import { createHash, randomUUID } from 'node:crypto';
import { createPersonalEnvironments } from './personal-environments.js';
import { createProductStore } from './product-store.js';
import { APP_NAME, TARGET_ID, sourceAppName } from './contract.js';
import { validateFiles, documentationOnly, documentationOnlyMessage } from './archive.js';
import { createMetricsObserver } from './metrics.js';
import { emptyAgentEvents, summarizeAgentEvents } from './agent-events.js';
import { EnvironmentError } from './environments.js';
import { SubmissionError } from './github.js';
import { createDeploymentDiagnostics } from './deployment-diagnostics.js';
import { appendEvent, observeOperation, ingestCiEvents, publicTelemetry } from './telemetry.js';
import { exact, lifecycleActions, lifecycleId, lifecycleHash, lifecycleResources, lifecycleSteps } from './application-lifecycle.js';

export class ProductError extends Error {
  constructor(status, code, message, { outcomeUnknown = false, retryable = false, admission } = {}) {
    super(message); Object.assign(this, { status, code, outcomeUnknown, retryable, admission });
  }
}
const invalid = (message) => new ProductError(422, 'INVALID_INPUT', message);
const unavailable = () => new ProductError(409, 'CAPABILITY_UNAVAILABLE', '이 서버에 해당 실행 기능이 설정되지 않았습니다.');
const active = (record) => ['queued', 'running', 'unknown'].includes(record.status);
const occupiesSlot = (record) => active(record) && !(record.status === 'unknown' && record.queue?.released_at)
  && !(record.status === 'running' && (record.ci?.observation?.next_retry_at || record.cd?.observation?.next_retry_at));
const digest = (value) => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const deliveryMessages = {
  GCP_ROUTE_PREPARATION_FAILED: 'GCP 로드밸런서 경로 준비에 실패했습니다. 앱 적용 전에 환경 설정과 실행 권한을 확인해야 합니다.',
  GCP_ROUTE_APPLY_TIMEOUT: 'GCP 로드밸런서 구성 시간이 초과됐습니다. 일부 자원이 생성됐을 수 있으며 현재 상태 확인 후 게시된 이미지로 이어갈 수 있습니다.',
  GCP_ROUTE_APPLY_INCOMPLETE: 'GCP 로드밸런서 구성이 끝나지 않았습니다. 방화벽·경로·인증서 적용 결과를 확인해야 합니다.',
  GCP_ROUTE_RECONCILE_REQUIRED: '이전 GCP 로드밸런서 구성이 일부만 완료돼 배포가 멈췄습니다. 남은 자원을 복구한 뒤 게시된 이미지로 이어갈 수 있습니다.',
  GCP_CERTIFICATE_DNS_NOT_CONFIGURED: 'GCP 인증용 DNS 설정이 연결되지 않았습니다. 환경의 DNS 설정을 확인해야 합니다.',
  GCP_CERTIFICATE_DNS_FAILED: 'GCP 인증용 DNS 등록 또는 공개 전파 확인에 실패해 인증서 생성을 진행하지 않았습니다. DNS 설정을 복구한 뒤 게시된 이미지로 이어갈 수 있습니다.',
};
const operationError = (code = 'UPSTREAM_FAILURE', unknown = true) => ({ code, request_id: randomUUID(), message: deliveryMessages[code] || (unknown ? '외부 실행 결과를 확인할 수 없습니다. 자동으로 재실행하지 않습니다.' : '실행이 완료되지 않았습니다.'), retryable: false, outcome_unknown: unknown });
function ciObservation(build, previous = {}) {
  const now = new Date().toISOString();
  return { ...previous, run_id: build.id, state: build.status, steps: build.steps ?? [],
    workflow: build.workflow,
    observation: { checked_at: now, last_success_at: now, error: null, next_retry_at: null },
    message: build.message || null, diagnostics: build.diagnostics || null, diagnostics_checked_at: new Date().toISOString(),
    images: build.publication?.images || {}, publication_artifact_id: build.publication?.artifact_id ? String(build.publication.artifact_id) : null,
    producer_attempt: build.publication?.producer_attempt || null };
}
function ciFailure(build) {
  const diagnostic = build.diagnostics?.state === 'ready' ? build.diagnostics : null;
  const unknown = diagnostic?.outcome === 'UNKNOWN';
  return { status: unknown ? 'unknown' : diagnostic?.outcome === 'BLOCKED' ? 'blocked' : 'failed',
    error: { ...operationError(diagnostic?.code || (build.status === 'publication_unverified' ? 'PUBLICATION_UNVERIFIED' : 'CI_FAILED'), unknown),
      message: [diagnostic?.message || build.message || 'CI 실행이 완료되지 않았습니다.', diagnostic?.guidance].filter(Boolean).join(' ') } };
}
export function idempotencyKey(value) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9._-]{1,128}$/.test(value)) throw invalid('유효한 Idempotency-Key가 필요합니다.');
  return value;
}
function publicRecord(record) {
  const { fingerprint, key, source, source_bytes, legacy, session_id, publication, refreshed_plan, diagnostic_evidence, classifications, telemetry, ...visible } = record;
  const timeline = publicTelemetry(record);
  const activity = agentActivity(record, timeline);
  return { ...structuredClone(visible), ...(telemetry ? { telemetry: timeline } : {}),
    agent_activity_summary: activity ? { id: activity.id, state: activity.state, attempt: activity.attempt, updated_at: activity.updated_at } : null };
}
function checkFree(state, sessionId = null, except = null) {
  const blocker = Object.values(state.operations).find((record) => record.id !== except && occupiesSlot(record));
  if (!blocker) return;
  // ponytail: one shared Git branch and runtime writer; per-app admission needs isolated writers first.
  const owned = Boolean(sessionId && blocker.session_id === sessionId);
  const detail = owned
    ? ` ${blocker.app || '환경'} · ${blocker.stage || '접수'} · 마지막 갱신 ${blocker.updated_at || blocker.created_at || '확인 불가'}.` : '';
  const admission = { scope: 'workspace', accepted: false,
    reason: blocker.status === 'unknown' ? 'reconciliation_required' : 'execution_in_progress',
    ...(owned ? { blocking_operation: { id: blocker.id, kind: blocker.kind, app: blocker.app,
      status: blocker.status, stage: blocker.stage, updated_at: blocker.updated_at || blocker.created_at } } : {}) };
  throw new ProductError(409, 'EXECUTOR_BUSY', `공유 배포 작업의 실행 또는 결과 확인이 끝나지 않았습니다.${detail} 이번 요청은 실행 대기열에 추가되지 않았습니다.`,
    { retryable: blocker.status !== 'unknown', admission });
}

export async function createProductService({ service, directory, target, providerTargets, deployPublished, environmentAdapter, applicationAdapter, personalAdapter, classifyFailure, observeMetrics = createMetricsObserver(), observeLogs, pollInterval = 5000, unknownGraceMs = 60_000, maxConcurrentDeployments = 3, maxOperations = 100, maxSourceBytes = 512 * 1024 * 1024 }) {
  const targetId = target?.id || service?.targetId;
  if (targetId && !TARGET_ID.test(targetId)) throw invalid('등록된 대상 ID가 잘못되었습니다.');
  const selections = new Map(target?.provider && targetId ? [[target.provider, targetId]] : []);
  if (providerTargets !== undefined) {
    if (!providerTargets || Array.isArray(providerTargets) || typeof providerTargets !== 'object') throw invalid('공급자별 대상 설정이 잘못되었습니다.');
    for (const [provider, id] of Object.entries(providerTargets)) {
      if (!['aws', 'gcp', 'openstack', 'proxmox'].includes(provider) || typeof id !== 'string' || !TARGET_ID.test(id)
          || selections.has(provider) && selections.get(provider) !== id
          || [...selections].some(([other, value]) => other !== provider && value === id)) throw invalid('공급자별 대상 설정이 잘못되었습니다.');
      selections.set(provider, id);
    }
  }
  if (!Number.isSafeInteger(unknownGraceMs) || unknownGraceMs < 1) throw invalid('unknown 대기 시간은 양의 정수여야 합니다.');
  if (!Number.isSafeInteger(maxConcurrentDeployments) || maxConcurrentDeployments < 1 || maxConcurrentDeployments > 64)
    throw invalid('배포 동시 실행 한도는 1..64 정수여야 합니다.');
  const store = await createProductStore(directory);
  const abort = new AbortController();
  const diagnostics = createDeploymentDiagnostics({ store, find, service, classifier: classifyFailure, signal: abort.signal });
  const telemetryTasks = new Map(), telemetryNext = new Map();
  function refreshCiJournal(record) {
    if (abort.signal.aborted || !service?.events || !record.ci?.run_id || !record.source_commit || telemetryTasks.has(record.id)) return;
    if (Date.now() < (telemetryNext.get(record.id) || 0) && !['published', 'failed', 'publication_unverified'].includes(record.ci.state)) return;
    telemetryNext.set(record.id, Date.now() + 15000);
    const runId = String(record.ci.run_id);
    const task = (async () => {
      const envelope = await service.events(runId, { source_commit: record.source_commit, app: record.app, target_id: record.target_id });
      await store.transaction((state) => {
        const row = state.operations[record.id], bound = state.bindings[runId];
        if (bound?.operation_id === record.id && row.source_commit === record.source_commit && String(row.ci?.run_id) === runId
            && row.session_id === record.session_id && bound.app === row.app && bound.target_id === row.target_id
            && bound.source_commit === row.source_commit) ingestCiEvents(row, envelope);
      });
    })().catch(() => {}).finally(() => telemetryTasks.delete(record.id));
    telemetryTasks.set(record.id, task);
  }
  function checkCapacity(state, sourceBytes = 0) {
    if (Object.keys(state.operations).length >= maxOperations || store.snapshotBytes() + sourceBytes > maxSourceBytes) throw new ProductError(409, 'CAPACITY_EXCEEDED', 'workspace 보관 한도에 도달했습니다. 운영자가 저장소를 확인해야 합니다.');
  }
  const workers = new Set();
  const deploymentWorkers = new Map();
  // CI observation is concurrent. Each shared writer remains single-file: source
  // commits use one branch, while native registration/CD share Git/edge state.
  function serialWriter() {
    let tail = Promise.resolve();
    return (fn) => {
      const next = tail.then(() => { if (abort.signal.aborted) throw new Error('Worker stopped'); return fn(); });
      tail = next.catch(() => {});
      return next;
    };
  }
  const writeSource = serialWriter(), writeInfrastructure = serialWriter();
  const sharedTargets = new Set(service?.targetIds || (service?.targetId ? [service.targetId] : []));
  const scopeKey = (kind, key, sessionId) => `${sessionId ? sessionId + ':' : ''}${kind}:${key}`;
  const owns = (value, sessionId) => value && (store.dashboard.isOwnerSession(value.session_id)
    ? Boolean(sessionId && value.session_id === sessionId) : !sessionId || value.session_id === sessionId);
  function applicationFor(state, id, sessionId, exactOwner = false) {
    const application = Object.hasOwn(state.applications, id) ? state.applications[id] : null;
    // Maintenance reads may span sessions; mutations must preserve the exact app owner.
    if (!owns(application, sessionId) || exactOwner && application.session_id !== sessionId)
      throw new ProductError(404, 'NOT_FOUND', '앱 등록을 찾을 수 없습니다.');
    return application;
  }
  function lifecycleAvailable(state, application, action, cancelling = null) {
    checkUncertainResource(state, application, cancelling, action === 'delete');
    personal.writable(state, application.environment_target_id, application.session_id);
    if (!['planLifecycle', 'verifyLifecyclePlan', 'applyLifecycle'].every((name) => typeof applicationAdapter?.[name] === 'function')) throw unavailable();
    const versions = applicationVersions(state, application);
    if (action !== 'delete' && versions.latest && !versions.current)
      throw new ProductError(409, 'APPLICATION_NOT_DEPLOYED', '성공한 배포가 없어 중지하거나 재개할 앱이 없습니다. 실패한 앱은 삭제할 수 있습니다.');
    // The normal native plan must prove no registration intent/binding exists before a local tombstone.
    if (action === 'delete' && !cancelling && application.status === 'queued') return;
    if (action === 'delete' && cancelling && ['queued', 'registering'].includes(application.status)
        && typeof applicationAdapter.planPendingDeletion === 'function') return;
    if (!(action === 'start' ? application.status === 'stopped' : ['ready', 'stopped'].includes(application.status))
        || action === 'stop' && application.status !== 'ready') throw new ProductError(409, 'APPLICATION_STATE_CONFLICT', '앱의 현재 상태에서는 이 작업을 실행할 수 없습니다.');
  }
  function applicationSnapshot(state, application, deferred = false) {
    const deployments = Object.values(state.operations).filter((row) => row.kind === 'deployments' && row.application_id === application.id);
    const latest = deployments.at(-1);
    return digest({ application: deferred ? Object.fromEntries(['id', 'app', 'target_id', 'environment_target_id', 'provider', 'session_id', 'created_at'].map((key) => [key, application[key]])) : application,
      deployment: latest ? { id: latest.id, ...(deferred ? {} : { source_commit: latest.source_commit || null }) } : null });
  }
  function deletionCandidate(state, applicationId, action) {
    const pending = Object.values(state.operations).filter(occupiesSlot);
    return action === 'delete' && pending.length === 1 && pending[0].kind === 'deployments'
      && pending[0].application_id === applicationId && ['queued', 'running'].includes(pending[0].status) ? pending[0].id : null;
  }
  const deletionRequested = (id) => Boolean(store.read().operations[id]?.deletion_requested);
  async function quiesceDeployment(deploymentId, operationId) {
    await update(operationId, { stage: 'quiescing' });
    const worker = deploymentWorkers.get(deploymentId);
    if (worker) {
      let timer;
      try { await Promise.race([worker, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Deployment quiescence unknown')), 1_800_000); timer.unref(); })]); }
      finally { clearTimeout(timer); }
    }
    const state = store.read(), deployment = state.operations[deploymentId];
    if (!deployment?.deletion_requested || deployment.status === 'unknown') throw new Error('Deployment outcome unknown');
    if (deployment.ci?.run_id) {
      const binding = state.bindings[String(deployment.ci.run_id)];
      if (binding?.operation_id !== deploymentId || binding.app !== deployment.app || binding.target_id !== deployment.target_id
          || binding.source_commit !== deployment.source_commit || typeof service?.cancel !== 'function') throw new Error('CI cancellation binding unknown');
      await update(operationId, { stage: 'cancelling_ci' });
      const result = await service.cancel(deployment.ci.run_id, binding, { signal: abort.signal });
      if (result?.status !== 'completed') throw new Error('CI not quiescent');
    } else if (deployment.stage === 'ci' && deployment.status !== 'queued') throw new Error('CI dispatch outcome unknown');
    await update(deploymentId, { status: 'blocked', stage: 'cancelled', error: operationError('DELETION_REQUESTED', false) });
  }
  function providerSelection(provider) {
    const id = selections.get(provider);
    const environment = id && applicationAdapter?.targets?.[id];
    if (environment) return { id, cdTarget: null, available: Boolean(service && environment.provider === provider && environment.automaticDelivery !== false) };
    const cdTarget = id && deployPublished?.targets && Object.hasOwn(deployPublished.targets, id) ? deployPublished.targets[id] : null;
    // Older single-target adapters have no registry; they authorize only the existing default.
    const available = Boolean(service && id && sharedTargets.has(id) && deployPublished
      && (cdTarget || !deployPublished.targets && id === targetId));
    return { id, cdTarget, available };
  }
  function deploymentOptions() {
    return [['cloud', 'aws', '클라우드 · AWS'], ['cloud', 'gcp', '클라우드 · Google Cloud'], ['onprem', 'openstack', '온프레미스 · OpenStack'], ['onprem', 'proxmox', '온프레미스 · Proxmox']].map(([environment, provider, label]) => {
      const { cdTarget, available } = providerSelection(provider);
      return { id: `${environment}-${provider}`, environment, provider, label, available,
        message: available ? `소스 검사부터 앱 배포와 URL 확인까지 진행합니다.${cdTarget?.applicationName ? ` 등록된 앱 ${cdTarget.applicationName}의 소스를 갱신합니다.` : ''}`
          : `${label}의 앱 배포 설정이 아직 준비되지 않았습니다.` };
    });
  }
  function resolveSelection(input, sessionId) {
    if (!input.deployment_selection) return input;
    if (input.app !== undefined || input.target_id !== undefined || input.plan_id !== undefined) throw invalid('환경 선택과 직접 대상·계획 지정을 함께 사용할 수 없습니다.');
    const { environment, provider, target_id: personalId } = input.deployment_selection;
    if (personalId !== undefined) {
      if (environment !== 'onprem' || provider !== 'openstack') throw invalid('개인 대상은 OpenStack 환경에서만 선택할 수 있습니다.');
      personal.get(personalId, sessionId);
      personal.writable(store.read(), personalId, sessionId);
      if (input.expected_target_id !== undefined && input.expected_target_id !== personalId)
        throw new ProductError(409, 'DEPLOYMENT_TARGET_CHANGED', '검토한 배포 대상이 현재 환경과 다릅니다. 선택 내용을 다시 확인하세요.');
      let app;
      try { app = sourceAppName(input.source_name ?? input.repository_url?.split('/').filter(Boolean).at(-1)?.replace(/\.git$/i, '')); } catch { throw invalid('소스 이름을 확인하세요.'); }
      return { ...input, app, target_id: personalId };
    }
    const option = deploymentOptions().find((item) => item.environment === environment && item.provider === provider);
    if (!option) throw invalid('배포 환경과 인프라 종류를 확인하세요.');
    if (!option.available) throw new ProductError(409, 'CAPABILITY_UNAVAILABLE', option.message);
    const { id } = providerSelection(provider);
    if (input.expected_target_id !== undefined && input.expected_target_id !== id)
      throw new ProductError(409, 'DEPLOYMENT_TARGET_CHANGED', '검토한 배포 대상이 현재 환경과 다릅니다. 선택 내용을 다시 확인하세요.');
    const name = input.source_name ?? input.repository_url?.split('/').filter(Boolean).at(-1)?.replace(/\.git$/i, '');
    let app;
    try { app = sourceAppName(name); } catch (error) { throw invalid(`소스 이름을 확인하세요. ${error.message}`); }
    return { ...input, app, target_id: id };
  }
  async function update(id, patch) {
    let newFailure = false;
    await store.transaction((state) => {
      const record = state.operations[id], now = new Date().toISOString();
      const before = structuredClone(record);
      newFailure = ['failed', 'blocked', 'unknown'].includes(patch.status)
        && !['failed', 'blocked', 'unknown'].includes(before.status);
      if (patch.status === 'unknown' && record.status !== 'unknown') record.unknown_since = now;
      if (patch.status === 'running' && record.queue) { delete record.queue.released_at; delete record.queue.release_reason; }
      Object.assign(record, patch, { updated_at: now });
      try { observeOperation(record, patch, before); }
      catch { record.telemetry = { ...record.telemetry, state: 'unavailable', reason: 'projection_failed' }; }
    });
    const row = store.read().operations[id];
    if (patch.ci) refreshCiJournal(row);
    if (newFailure && row.kind === 'deployments' && row.ci?.state === 'failed') diagnostics.schedule(id);
  }
  function launch(fn, deploymentId = null) {
    const worker = Promise.resolve().then(fn).catch(() => { console.error('RAILSHOT worker could not persist its final state; inspect private workspace state.'); }).finally(() => {
      workers.delete(worker);
      if (deploymentId && deploymentWorkers.get(deploymentId) === worker) deploymentWorkers.delete(deploymentId);
    });
    workers.add(worker);
    if (deploymentId) deploymentWorkers.set(deploymentId, worker);
    return worker;
  }
  function checkUncertainResource(state, candidate, except = null, deleting = false) {
    const target = candidate.environment_target_id || candidate.target_id;
    // CI writes a shared app source path. Once publication is bound, CD affects
    // the registered environment only; another environment has a distinct app ID.
    const separateDelivery = (row) => row.application_id && row.ci?.state === 'published'
      && ['cd', 'http'].includes(row.stage) && row.environment_target_id && candidate.environment_target_id
      && row.environment_target_id !== candidate.environment_target_id;
    // A stopped observation is not a live writer. Deletion can inventory the exact
    // registered app without a successful CD journal; the native plan/apply still
    // checks ownership, active syncs, provider state and resource identities.
    const inspectForDeletion = (row) => deleting && row.kind === 'deployments' && row.status === 'blocked'
      && row.ci?.state === 'published' && ['cd', 'http'].includes(row.stage)
      && row.application_id === candidate.id && row.target_id === candidate.target_id
      && row.environment_target_id === candidate.environment_target_id && row.session_id === candidate.session_id
      && !deploymentWorkers.has(row.id);
    const blocker = Object.values(state.operations).find((row) => row.id !== except && (row.status === 'unknown' || row.status === 'blocked' && row.error?.outcome_unknown)
      && !inspectForDeletion(row)
      && (row.app && row.app === candidate.app && !separateDelivery(row)
        || (row.kind === 'environments' || row.stage === 'environment')
          && (!target || (row.environment_target_id || row.target_id || state.plans[row.plan_id]?.private?.profile?.target?.target_id) === target)));
    if (blocker) throw new ProductError(409, 'APPLICATION_RECONCILE_REQUIRED', '이 앱 또는 환경의 이전 실행 결과를 먼저 확인해야 합니다. 다른 앱은 대기열에 접수할 수 있습니다.');
  }
  function enqueue(state, record) {
    record.queue = { sequence: 1 + Math.max(0, ...Object.values(state.operations).map((row) => row.queue?.sequence || 0)),
      enqueued_at: new Date().toISOString() };
    record.status = 'queued';
  }
  function interruptedCI(state, row) {
    const due = row.status === 'running' && row.ci?.observation?.next_retry_at && Date.parse(row.ci.observation.next_retry_at) <= Date.now();
    if (!['deployments', 'builds'].includes(row.kind) || !(due || row.status === 'unknown'
        && ['INTERRUPTED', 'CD_OUTCOME_UNKNOWN', 'CI_DISPATCH_UNCONFIRMED', 'SOURCE_REGISTRATION_FAILED'].includes(row.error?.code)) || row.stage !== 'ci' || row.deletion_requested
        || row.kind === 'deployments' && (row.cd?.state !== 'not_started' || row.cd?.deployed || row.cd?.revision)) return false;
    const runId = String(row.ci?.run_id), binding = state.bindings[runId];
    if (!/^[a-f0-9]{40}$/.test(row.source_commit || '')) return false;
    if (row.ci?.run_id) {
      if (!/^\d+$/.test(runId) || !binding || binding.operation_id !== row.id || binding.app !== row.app
          || binding.target_id !== row.target_id || binding.source_commit !== row.source_commit) return false;
    } else if (row.dispatch?.state === 'source_prepared') {
      if (!/^[a-f0-9]{40}$/.test(row.dispatch.source_parent || '') || typeof service.resumePrepared !== 'function') return false;
    } else if (!Number.isFinite(Date.parse(row.dispatch?.prepared_at)) || typeof service.findDeployment !== 'function') return false;
    if (row.application_id) {
      const app = state.applications[row.application_id];
      if (!app || app.status !== 'ready' || app.deletion_requested || app.session_id !== row.session_id
          || app.app !== row.app || app.target_id !== row.target_id
          || app.environment_target_id !== row.environment_target_id) return false;
    }
    return !Object.values(state.operations).some((other) => other.id !== row.id
      && other.application_id === row.application_id && other.app === row.app && other.target_id === row.target_id
      && other.created_at > row.created_at && !(other.status === 'queued' && !other.queue?.started_at));
  }
  const cdRecoveries = new Set(Object.values(store.read().operations).filter(row =>
    (row.status === 'unknown' || row.status === 'blocked' && row.error?.code === 'INTERRUPTED')
      && ['cd', 'http'].includes(row.stage)).map(row => row.id));
  function recoveringCD(state, row) {
    if (!cdRecoveries.has(row.id) || !['unknown', 'running', 'blocked'].includes(row.status) || !['cd', 'http'].includes(row.stage)
        || typeof applicationAdapter?.observePublished !== 'function' || !row.publication
        || row.ci?.state !== 'published' || Date.parse(row.cd?.observation?.next_retry_at) > Date.now()) return false;
    return interruptedCI(state, { ...row, status: 'unknown', stage: 'ci', error: { code: 'INTERRUPTED' }, cd: { state: 'not_started' } });
  }
  let pumping;
  const waiting = (row) => row.kind === 'deployments' && row.status === 'queued'
    && row.queue?.enqueued_at && !row.queue.started_at;
  function pump() {
    if (pumping || releasePaused || abort.signal.aborted) return pumping;
    // Read-only CI retries retain an admission slot, even between polls. Otherwise
    // a queue of 100 requests would dispatch 100 builds despite the configured limit.
    const admitted = (row) => row.status === 'running'
      || row.status === 'unknown' && !row.queue?.released_at;
    const snapshot = store.read(), rows = Object.values(snapshot.operations);
    if (!rows.some(row => waiting(row) || interruptedCI(snapshot, row) || recoveringCD(snapshot, row)
        || row.status === 'unknown' && !row.queue?.released_at)) return;
    const visited = new Set();
    pumping = (async () => {
      while (!abort.signal.aborted && !releasePaused && deploymentWorkers.size < maxConcurrentDeployments) {
        // Legacy builds, lifecycle changes and provisioning retain exclusive admission.
        if (workers.size > deploymentWorkers.size) break;
        const record = await store.transaction((state) => {
          if (abort.signal.aborted || releasePaused) return null;
          const now = Date.now(), iso = new Date(now).toISOString();
          const rows = Object.values(state.operations);
          for (const row of rows) {
            if (row.status !== 'unknown' || row.queue?.released_at || deploymentWorkers.has(row.id)) continue;
            if (!Number.isFinite(Date.parse(row.unknown_since))) row.unknown_since =
              Number.isFinite(Date.parse(row.updated_at || row.created_at)) ? row.updated_at || row.created_at : iso;
            if (now - Date.parse(row.unknown_since) >= unknownGraceMs)
              row.queue = { ...row.queue, released_at: iso, release_reason: 'unknown_timeout' };
          }
          if (rows.some(row => !['deployments', 'builds'].includes(row.kind) && occupiesSlot(row))) return null;
          const recovery = rows.find(row => !visited.has(row.id) && !deploymentWorkers.has(row.id)
            && (interruptedCI(state, row) || recoveringCD(state, row)));
          if (recovery) {
            const recoveringDelivery = ['cd', 'http'].includes(recovery.stage);
            Object.assign(recovery, { status: 'running', ...(recoveringDelivery ? {} : { error: null }), updated_at: iso });
            const phase = recoveringDelivery ? recovery.cd : recovery.ci;
            if (phase.observation) phase.observation.next_retry_at = null;
            if (recovery.queue) { delete recovery.queue.released_at; delete recovery.queue.release_reason; }
            return { ...structuredClone(recovery), recoveringCI: !recoveringDelivery, recoveringDelivery };
          }
          if (rows.filter(admitted).length >= maxConcurrentDeployments) return null;
          const next = rows.filter(row => waiting(row) && !rows.some(other => other.id !== row.id
            && other.app === row.app && (other.status === 'running' || deploymentWorkers.has(other.id))))
            .sort((a, b) => a.queue.sequence - b.queue.sequence)[0];
          if (!next) return null;
          try {
            personal.writable(state, next.environment_target_id || next.target_id, next.session_id);
            checkUncertainResource(state, next, next.id);
          }
          catch (error) {
            Object.assign(next, { status: 'blocked', error: operationError(error.code, false), updated_at: iso });
            return { skipped: true };
          }
          Object.assign(next, { status: 'running', updated_at: iso });
          next.queue.started_at = iso;
          return structuredClone(next);
        });
        if (!record || abort.signal.aborted) break;
        visited.add(record.id);
        if (!record.skipped) launch(() => record.recoveringDelivery ? observeDelivery(record) : record.recoveringCI
          ? record.ci?.run_id ? observe(record, String(record.ci.run_id)) : recoverDispatch(record)
          : runDeployment(record), record.id);
      }
    })().catch(() => { console.error('RAILSHOT deployment queue could not persist state; inspect private workspace state.'); })
      .finally(() => { pumping = null; });
    return pumping;
  }
  function find(kind, id, sessionId = null) {
    const operation = store.read().operations[id];
    if (!owns(operation, sessionId) || operation.kind !== kind) throw new ProductError(404, 'NOT_FOUND', '자원을 찾을 수 없습니다.');
    return operation;
  }
  function validateInput(input) {
    if (!service || !targetId) throw unavailable();
    if (typeof input.app !== 'string' || !APP_NAME.test(input.app)) throw invalid('앱 이름이 잘못되었습니다.');
    if (typeof input.target_id !== 'string' || !TARGET_ID.test(input.target_id)) throw invalid('등록된 배포 대상 ID가 잘못되었습니다.');
  }
  function registeredEnvironment(state, id, sessionId = null) {
    if (typeof environmentAdapter?.deployPublished !== 'function' || typeof id !== 'string' || !TARGET_ID.test(id)) return null;
    for (const operation of Object.values(state.operations).reverse()) {
      if (!owns(operation, sessionId)) continue;
      const standalone = operation.kind === 'environments';
      const environment = standalone ? operation : operation.kind === 'deployments' ? operation.environment : null;
      const environmentId = standalone ? operation.id : operation.environment_id;
      const plan = Object.hasOwn(state.plans, operation.plan_id || '') ? state.plans[operation.plan_id] : null;
      if (environment?.status !== 'succeeded' || environment.deployment_supported !== true
          || environment.runtime_target_id !== id || !environmentId || plan?.environment_id !== environmentId
          || plan.private?.profile?.target?.target_id !== id || !plan.private.profile.deployment
          || !APP_NAME.test(plan.public?.name || '') || (!standalone && operation.app !== plan.public.name)) continue;
      return { environment_id: environmentId, applicationName: plan.public.name, provider: plan.private.profile.provider ?? null,
        database_configuration: environment.database?.status === 'succeeded' };
    }
    return null;
  }
  // Restore only successful registrations; interrupted intents still occupy checkFree admission.
  const restored = store.read();
  for (const app of Object.values(restored.applications)) if (app.status === 'ready') service?.allowTarget?.(app.target_id);
  for (const operation of Object.values(restored.operations)) {
    const id = operation.kind === 'environments' ? operation.runtime_target_id : operation.environment?.runtime_target_id;
    if (registeredEnvironment(restored, id)) service?.allowTarget?.(id);
  }
  // A process restart can interrupt a task still waiting for the shared writer.
  // Only absence of the native request/intent permits replay of the same source.
  for (const operation of Object.values(restored.operations)) {
    const application = restored.applications[operation.application_id];
    if (operation.kind !== 'deployments' || operation.status !== 'unknown' || operation.stage !== 'registration'
        || !application || !['unknown', 'queued'].includes(application.status)
        || operation.deletion_requested || application.deletion_requested || application.lifecycle_operation_id
        || operation.ci?.run_id || operation.source_commit || operation.dispatch
        || !operation.queue?.enqueued_at || typeof applicationAdapter?.registrationStarted !== 'function') continue;
    try {
      if (await applicationAdapter.registrationStarted(application)) continue;
      await store.transaction(state => {
        const row = state.operations[operation.id];
        state.applications[application.id].status = 'queued';
        Object.assign(row, { status: 'queued', error: null, updated_at: new Date().toISOString() });
        delete row.queue.started_at; delete row.queue.released_at; delete row.queue.release_reason;
      });
    } catch { /* A failed observation never grants permission to repeat a write. */ }
  }
  function inputFingerprint(input) {
    return digest({ app: input.app, target_id: input.target_id, type: input.source_type, ...(input.environment_target_id ? { environment_target_id: input.environment_target_id } : {}), ...(input.plan_id ? { plan_id: input.plan_id } : {}),
      ...(input.deployment_selection ? { selection: input.deployment_selection } : {}),
      ...(input.repository_url ? { repository_url: input.repository_url } : {
        files: input.files.map(({ path, content }) => [path, createHash('sha256').update(content).digest('hex')]).sort(([a], [b]) => a.localeCompare(b)),
      }) });
  }
  async function reserve(kind, input, key, materialize, sessionId = null) {
    function authorizeTarget(state) {
      personal.writable(state, input.environment_target_id || input.target_id, sessionId);
      const linked = Object.values(state.applications).find((app) => app.target_id === input.target_id);
      if (linked) personal.writable(state, linked.environment_target_id, sessionId);
    }
    authorizeTarget(store.read());
    validateInput(input);
    const fingerprint = inputFingerprint(input);
    const existingId = key && store.read().keys[scopeKey(kind, key, sessionId)];
    if (existingId) {
      const existing = store.read().operations[existingId];
      if (existing.fingerprint !== fingerprint) throw new ProductError(409, 'IDEMPOTENCY_CONFLICT', '같은 키로 다른 입력을 보낼 수 없습니다.');
      return { record: existing, replay: true };
    }
    function admission(state) {
      authorizeTarget(state);
      const selectedCdTarget = deployPublished?.targets?.[input.target_id];
      const application = kind === 'deployments' && input.environment_target_id
        ? applicationAdapter.describe(input.environment_target_id, input.app) : null;
      const previousApplication = application && state.applications[application.id];
      if (application && applicationAdapter.targets[application.environment_target_id]?.automaticDelivery === false) throw unavailable();
      if (previousApplication && previousApplication.session_id !== sessionId) throw new ProductError(409, 'APPLICATION_OWNERSHIP_CONFLICT', '같은 환경의 이 앱 이름은 다른 세션에 등록되어 있습니다. 다른 이름을 사용하세요.');
      if (previousApplication && ['stopped', 'deleted'].includes(previousApplication.status)) throw new ProductError(409, 'APPLICATION_STATE_CONFLICT', '중지한 앱은 명시적으로 시작해야 하며 삭제한 앱은 다시 배포할 수 없습니다.');
      if (previousApplication && !['queued', 'registering', 'ready'].includes(previousApplication.status)) throw new ProductError(409, 'APPLICATION_RECONCILE_REQUIRED', '이 앱 등록 결과를 운영자가 확인해야 합니다. 자동 재등록하지 않습니다.');
      const selectedCdAvailable = Boolean(deployPublished && (!deployPublished.targets || selectedCdTarget));
      const savedEnvironment = registeredEnvironment(state, input.target_id, sessionId);
      const registered = !selectedCdAvailable && !input.plan_id && kind === 'deployments' ? savedEnvironment : null;
      const admittedTarget = sharedTargets.has(input.target_id) || Boolean(savedEnvironment) || Boolean(application);
      const plan = input.plan_id && Object.hasOwn(state.plans, input.plan_id) ? state.plans[input.plan_id] : null;
      if (input.plan_id) {
        if (kind !== 'deployments' || !owns(plan, sessionId) || ['application-lifecycle', 'target-lifecycle'].includes(plan.kind) || !environmentAdapter?.deployPublished) throw invalid('실행 가능한 환경 계획이 필요합니다.');
        if (plan.environment_id) throw new ProductError(409, 'CONFLICT', '이미 실행에 사용된 계획입니다.');
        if (plan.public.name !== input.app || plan.private?.profile?.target?.target_id !== input.target_id
            || !plan.private.profile.deployment) throw invalid('계획의 앱·배포 대상과 일치해야 합니다.');
        if (!admittedTarget && (plan.private.profile.create_per_request !== true || plan.public.runtime_target_id !== input.target_id
            || typeof service.allowTarget !== 'function')) throw invalid('요청별 실행 대상이 승인된 계획이어야 합니다.');
      }
      if (!admittedTarget && !plan && !savedEnvironment) throw invalid('등록된 배포 대상만 사용할 수 있습니다.');
      if (kind === 'deployments' && !input.plan_id && !selectedCdAvailable && !registered && !application) throw unavailable();
      if (kind === 'builds') checkFree(state, sessionId);
      checkUncertainResource(state, input);
      checkCapacity(state);
      const applicationName = selectedCdTarget?.applicationName || savedEnvironment?.applicationName;
      if (!input.plan_id && applicationName && (kind === 'deployments' || savedEnvironment) && input.app !== applicationName) throw invalid(`등록된 배포 앱 이름과 일치하지 않습니다. 이 대상은 ${applicationName} 전용입니다. ${input.app} 배포에는 새 앱용 환경 또는 같은 이름의 앱 등록이 필요합니다.`);
      return { application, previousApplication, plan, registered };
    }
    const admitted = admission(store.read());
    if (admitted.plan) await environmentAdapter.verifyPlan(admitted.plan);
    // Network reads must not hold the shared persistence queue. Recheck ownership,
    // capacity and idempotency below after the source arrives.
    const source = input.files ? input : { ...input, ...await materialize(input.repository_url) };
    const files = validateFiles(source.files);
    if (documentationOnly(files)) throw invalid(documentationOnlyMessage);
    return store.transaction(async (state) => {
      validateInput(input);
      authorizeTarget(state);
      const existingId = key && state.keys[scopeKey(kind, key, sessionId)];
      if (existingId) {
        const existing = state.operations[existingId];
        if (existing.fingerprint !== fingerprint) throw new ProductError(409, 'IDEMPOTENCY_CONFLICT', '같은 키로 다른 입력을 보낼 수 없습니다.');
        return { record: existing, replay: true };
      }
      const { application, previousApplication, plan, registered } = admission(state);
      const sourceBytes = files.reduce((sum, file) => sum + Buffer.byteLength(file.path) + Math.ceil(file.content.length / 3) * 4 + 128, 0);
      checkCapacity(state, sourceBytes);
      const id = randomUUID(), now = new Date().toISOString();
      await store.snapshot(id, files);
      if (application && !previousApplication) state.applications[application.id] = { ...application, session_id: sessionId, status: 'queued', created_at: now };
      const record = { id, kind, session_id: sessionId, app: input.app, target_id: input.target_id,
        ...(input.deployment_selection ? { deployment_selection: { ...input.deployment_selection } } : {}),
        ...(application ? { application_id: application.id, environment_target_id: application.environment_target_id } : {}),
        ...(plan ? { plan_id: input.plan_id, environment_id: `${id}.environment`, environment: { status: 'queued' } } : registered ? { environment_id: registered.environment_id } : {}), status: 'queued', stage: application ? 'registration' : plan ? 'environment' : 'ci',
        ci: { run_id: null, state: 'queued', steps: [], publication_artifact_id: null, producer_attempt: null },
        cd: { state: 'not_started', revision: null, deployed: false },
        public_http: { state: 'not_run', verified_at: null, url: null }, url: null,
        actions_url: null, error: null, created_at: now, updated_at: now, fingerprint, key,
        source: source.source || null, source_bytes: sourceBytes, source_digest: digest(files.map(({ path, content }) => [path, createHash('sha256').update(content).digest('hex')]).sort()),
      };
      if (kind === 'deployments') enqueue(state, record);
      appendEvent(record, 'deployment.accepted', 'dispatch', 'NOT_RUN');
      state.operations[id] = record;
      if (plan) plan.environment_id = record.environment_id;
      if (key) state.keys[scopeKey(kind, key, sessionId)] = id;
      return { record, plan, input: { app: input.app, target_id: input.target_id, files, source: source.source } };
    });
  }
  async function bindRun(record, result) {
    const runId = String(result.run_id);
    if (!/^\d+$/.test(runId) || typeof result.run_id === 'number' && !Number.isSafeInteger(result.run_id)) throw new Error('Invalid upstream run id');
    await store.transaction((state) => {
      if (state.bindings[runId]) throw new Error('Duplicate upstream run id');
      const saved = state.operations[record.id];
      if (saved.source_commit && saved.source_commit !== result.source_commit) throw new Error('Submitted source changed');
      state.bindings[runId] = { operation_id: record.id, app: record.app, target_id: record.target_id, source_commit: result.source_commit || null, run_attempt: 1 };
      Object.assign(saved, { legacy: result, actions_url: result.actions_url || null, error: null,
        ...(saved.dispatch ? { dispatch: { ...saved.dispatch, state: 'accepted' } } : {}),
        source_commit: result.source_commit || null, ci: { ...saved.ci, run_id: runId } });
    });
    return runId;
  }
  async function submit(record, input) {
    if (deletionRequested(record.id)) return null;
    await update(record.id, { status: 'running', stage: 'ci', dispatch: { version: 2, state: 'preparing' } });
    try {
      if (deletionRequested(record.id)) return null;
      if (!(service.targetIds || [targetId]).includes(record.target_id)) {
        const application = record.application_id && store.read().applications[record.application_id];
        const registered = registeredEnvironment(store.read(), record.target_id);
        const admitted = application?.status === 'ready' && application.app === record.app && application.target_id === record.target_id
          || registered?.applicationName === record.app;
        if (!admitted || typeof service.allowTarget !== 'function') throw unavailable();
        service.allowTarget(record.target_id);
      }
      const result = await writeSource(() => service.deploy({ ...input, operation_id: record.id,
        onSourcePrepared: async ({ source_commit, source_parent }) => {
          if (!/^[a-f0-9]{40}$/.test(source_commit || '') || !/^[a-f0-9]{40}$/.test(source_parent || '')) throw new Error('Invalid prepared source');
          await update(record.id, { source_commit, dispatch: { version: 2, state: 'source_prepared', source_parent, source_prepared_at: new Date().toISOString() } });
          if (deletionRequested(record.id)) throw new Error('Submission cancelled before dispatch');
        },
        onPrepared: async ({ source_commit }) => {
          if (!/^[a-f0-9]{40}$/.test(source_commit || '')) throw new Error('Invalid prepared source');
          // Graceful shutdown waits for this already-started submission to bind its run.
          // Aborting here loses a successfully published source before CI is requested.
          if (deletionRequested(record.id)) throw new Error('Submission cancelled before dispatch');
          await update(record.id, { source_commit, dispatch: { state: 'requesting', prepared_at: new Date().toISOString() } });
        } }));
      await bindRun(record, result);
      return result;
    } catch (error) {
      const known = error instanceof SubmissionError;
      const unknown = !known || error.outcomeUnknown;
      const failure = { ...operationError(known ? error.code : 'UPSTREAM_FAILURE', unknown),
        ...(known ? { message: error.message, phase: error.phase, upstream_status: error.upstream_status, reason: error.reason } : {}) };
      await update(record.id, { status: unknown ? 'unknown' : 'failed', error: failure });
      console.error(JSON.stringify({ event: 'api.deployment_failed', operation_id: record.id, request_id: failure.request_id,
        code: failure.code, phase: failure.phase || 'ci_submission', upstream_status: failure.upstream_status || null, outcome_unknown: unknown,
        cause_type: error.cause_type || null, cause_code: error.cause_code || null }));
      throw new ProductError(502, failure.code, failure.message, { outcomeUnknown: unknown });
    }
  }
  async function observationRetry(record, error) {
    const now = Date.now();
    const ci = store.read().operations[record.id].ci;
    const failures = (ci.observation?.consecutive_failures || 0) + 1;
    const delay = Math.max(Math.min(30_000, pollInterval * 2 ** Math.min(failures, 8)),
      Number.isFinite(error.retryAt) ? Math.max(0, error.retryAt - now) : 0);
    const diagnostic = { code: error.code || 'CI_OBSERVATION_UNAVAILABLE',
      message: error.message, retryable: true, ...(error.upstream_status ? { upstream_status: error.upstream_status } : {}) };
    await update(record.id, { ci: { ...ci, observation: { ...ci.observation, checked_at: new Date(now).toISOString(),
      consecutive_failures: failures, last_success_at: ci.observation?.last_success_at || null, error: diagnostic, next_retry_at: new Date(now + delay).toISOString() } } });
  }
  async function recoverDispatch(record) {
    try {
      while (!abort.signal.aborted && !deletionRequested(record.id)) {
        let result;
        try {
          const saved = store.read().operations[record.id];
          const binding = { operation_id: record.id, source_commit: saved.source_commit, app: record.app, target_id: record.target_id };
          if (saved.dispatch?.state === 'source_prepared') {
            service.allowTarget?.(record.target_id);
            result = await writeSource(() => service.resumePrepared({ ...binding, source_parent: saved.dispatch.source_parent,
              onPrepared: async ({ source_commit }) => {
                if (source_commit !== saved.source_commit) throw new Error('Source binding changed');
                if (deletionRequested(record.id)) throw new Error('Submission cancelled before dispatch');
                await update(record.id, { dispatch: { ...saved.dispatch, state: 'requesting', prepared_at: new Date().toISOString() } });
              } }));
          } else result = await service.findDeployment(binding);
        }
        catch (error) {
          if (error instanceof SubmissionError && error.outcomeUnknown) {
            await observationRetry(record, { code: 'CI_DISPATCH_LOOKUP_UNAVAILABLE', message: '소스 반영 또는 CI 접수 결과를 다시 확인합니다.' });
            return;
          }
          if (error.retryable === false) throw error;
          await observationRetry(record, { code: 'CI_DISPATCH_LOOKUP_UNAVAILABLE', message: 'GitHub 실행 목록 조회를 재시도하고 있습니다.' });
          return;
        }
        if (deletionRequested(record.id)) return;
        if (result) {
          await bindRun(record, result);
          if (!abort.signal.aborted) return await observe(record, String(result.run_id));
          return;
        }
        if (abort.signal.aborted) return;
        if (Date.now() - Date.parse(record.dispatch.prepared_at) > 300_000)
          throw new ProductError(409, 'CI_DISPATCH_NOT_IDENTIFIED', '5분 동안 접수한 CI 실행을 찾지 못했습니다. 요청 ID와 소스 커밋으로 GitHub 실행을 확인해야 합니다. 실행을 다시 보내지는 않았습니다.');
        await observationRetry(record, { code: 'CI_DISPATCH_PENDING', message: '실행 접수 응답이 유실되어 같은 소스 커밋의 CI 실행을 찾고 있습니다.' });
        return;
      }
    } catch (error) {
      if (!abort.signal.aborted) await update(record.id, { status: 'blocked', error: {
        ...operationError(error.code || 'CI_DISPATCH_AMBIGUOUS', true), message: error instanceof ProductError ? error.message : '접수 기록과 CI 실행을 연결하지 못했습니다. 요청 ID와 소스 커밋으로 운영 확인이 필요합니다.' } });
    }
  }
  async function readBuild(runId, sessionId = null) {
    const state = store.read(), binding = /^\d+$/.test(runId) && Object.hasOwn(state.bindings, runId) ? state.bindings[runId] : null;
    if (!binding || !owns(state.operations[binding.operation_id], sessionId)) throw new ProductError(404, 'NOT_FOUND', '이 workspace에서 접수한 빌드를 찾을 수 없습니다.');
    let observed;
    try { observed = await service.status(runId, binding.target_id); }
    catch (error) { throw Object.assign(new ProductError(502, error.retryable === false ? 'CI_OBSERVATION_REJECTED' : 'CI_OBSERVATION_UNAVAILABLE',
      error.retryable === false ? 'CI 조회 권한 또는 실행 정보를 확인해야 합니다.' : 'GitHub 상태 조회에 실패했습니다. 마지막 확인 상태를 유지하고 재조회합니다.',
      { retryable: error.retryable !== false, outcomeUnknown: error.retryable === false }), { upstream_status: error.upstreamStatus || null, retryAt: error.retryAt }); }
    if (observed.publication && (String(observed.publication.run_id) !== runId || observed.publication.target_id !== binding.target_id || observed.publication.app !== binding.app || binding.source_commit && observed.publication.source_commit !== binding.source_commit)) {
      throw new ProductError(502, 'CI_BINDING_MISMATCH', '게시 결과와 접수 기록이 일치하지 않습니다.');
    }
    const { run_id, state: status, status: workflowStatus, conclusion, ...rest } = observed;
    return { ...rest, id: runId, app: binding.app, target_id: binding.target_id, source_commit: binding.source_commit,
      status, workflow: { status: workflowStatus, conclusion }, url: null };
  }
  async function observeDelivery(record) {
    const now = new Date().toISOString();
    try {
      const result = await writeInfrastructure(() => applicationAdapter.observePublished(store.read().applications[record.application_id], {
        deploymentId: record.id, app: record.app, targetId: record.target_id, sourceCommit: record.source_commit,
        publication: record.publication, signal: abort.signal }));
      if (abort.signal.aborted || deletionRequested(record.id)) return;
      const succeeded = result.cd?.deployed === true && result.cd.revision && result.public_http?.state === 'succeeded'
        && result.public_http.verified_at && /^https?:\/\//.test(result.public_http.url || '');
      const stopped = ['blocked', 'failed'].includes(result.cd?.state);
      const unknown = result.cd?.state === 'unknown';
      const cd = unknown ? store.read().operations[record.id].cd : result.cd;
      const missing = result.error?.code === 'DEPLOYMENT_NOT_FOUND';
      const originalError = missing && store.read().operations[record.id].error;
      if (missing && originalError?.code === 'INTERRUPTED' && !record.cd?.deployed && !record.cd?.revision) {
        // A platform restart can interrupt route preparation before CD records a
        // revision. Resume the same publication through the idempotent adapter.
        // Known route failures still require reconciliation instead of replay.
        await update(record.id, { error: null, resumed_at: now, resume_count: (record.resume_count || 0) + 1 });
        await observe(record, String(record.ci.run_id), { resume: true });
        return;
      }
      const error = unknown ? { code: result.error?.code || 'CD_OBSERVATION_UNAVAILABLE', message: '클러스터 결과 조회를 재시도하고 있습니다.', retryable: true }
        : missing ? { code: 'DEPLOYMENT_NOT_FOUND', message: '클러스터 적용 기록이 아직 없습니다. 앞선 경로 구성 오류를 확인하세요.', retryable: false } : null;
      const observation = { checked_at: now, last_success_at: unknown ? cd.observation?.last_success_at || null : now,
        error, next_retry_at: succeeded || stopped ? null : new Date(Date.now() + Math.max(pollInterval, 5000)).toISOString() };
      await update(record.id, { status: succeeded ? 'succeeded' : stopped ? 'blocked' : 'running',
        stage: succeeded ? 'complete' : cd.deployed ? 'http' : 'cd', cd: { ...cd, observation },
        ...(unknown ? {} : { public_http: result.public_http }),
        url: succeeded ? result.public_http.site_url || result.public_http.url : null,
        error: stopped ? originalError || { ...operationError(result.error?.code || 'CD_OBSERVATION_REJECTED', true), message: '기존 배포 기록으로 클러스터 결과를 확인하지 못했습니다. 이 앱의 배포 기록을 확인해야 합니다.' }
          : unknown ? store.read().operations[record.id].error : null });
      if (succeeded || stopped) cdRecoveries.delete(record.id);
    } catch (error) {
      if (abort.signal.aborted) return;
      const cd = store.read().operations[record.id].cd;
      const stopped = error instanceof EnvironmentError || error.retryable === false;
      await update(record.id, { status: stopped ? 'blocked' : 'running', cd: { ...cd, observation: {
        checked_at: now, last_success_at: cd.observation?.last_success_at || null,
        error: { code: stopped ? error.code || 'CD_OBSERVATION_REJECTED' : 'CD_OBSERVATION_UNAVAILABLE',
          message: stopped ? '기존 CD 기록 또는 조회 권한을 확인해야 합니다.' : '클러스터 조회를 재시도하고 있습니다.', retryable: !stopped },
        next_retry_at: stopped ? null : new Date(Date.now() + 30_000).toISOString() } },
        error: stopped ? { ...operationError(error.code || 'CD_OBSERVATION_REJECTED', true), message: '기존 CD 기록 또는 조회 권한을 확인해야 합니다. CI나 배포 변경을 다시 실행하지 않았습니다.' } : null });
      if (stopped) cdRecoveries.delete(record.id);
    }
  }
  async function observe(record, runId, { resume = false } = {}) {
    try {
      for (;;) {
        if (abort.signal.aborted || deletionRequested(record.id)) return;
        let build;
        try { build = await readBuild(runId, record.session_id); }
        catch (error) {
          if (!error.retryable || abort.signal.aborted) throw error;
          await observationRetry(record, error);
          return;
        }
        if (abort.signal.aborted || deletionRequested(record.id)) return;
        if (resume) {
          const published = build.publication;
          const imageEntries = (images) => Object.entries(images || {}).sort(([a], [b]) => a.localeCompare(b));
          if (build.status !== 'published' || build.source_commit !== record.source_commit
              || String(published?.run_id) !== runId || published?.source_commit !== record.source_commit
              || published?.app !== record.app || published?.target_id !== record.target_id
              || String(published?.artifact_id) !== record.ci.publication_artifact_id
              || published?.producer_attempt !== record.ci.producer_attempt
              || digest(imageEntries(published?.images)) !== digest(imageEntries(record.ci.images))) {
            throw new EnvironmentError('RESUME_PUBLICATION_CHANGED', 409, true);
          }
        }
        const ci = ciObservation(build, store.read().operations[record.id].ci);
        if (!['published', 'failed', 'publication_unverified'].includes(build.status))
          ci.observation.next_retry_at = new Date(Date.now() + pollInterval * Math.max(1, Object.values(store.read().operations)
            .filter(row => row.status === 'running' && row.stage === 'ci').length)).toISOString();
        await update(record.id, { ci, ...(build.publication ? { publication: build.publication } : {}) });
        if (build.status === 'published') {
          if (record.kind === 'builds') { await update(record.id, { status: 'succeeded', stage: 'ci' }); return; }
          if (!resume || !record.cd?.deployed) await update(record.id, { stage: 'cd', cd: { state: 'running', revision: null, deployed: false } });
          if (deletionRequested(record.id)) return;
          personal.writable(store.read(), record.environment_target_id || record.target_id, record.session_id);
          const deploy = record.application_id ? (args) => applicationAdapter.deployPublished(store.read().applications[record.application_id], args)
            : record.environment_id ? (args) => environmentAdapter.deployPublished(record.environment_id, args) : deployPublished;
          const result = await writeInfrastructure(() => deploy({ deploymentId: record.id, app: record.app, targetId: record.target_id,
            sourceCommit: build.source_commit, publication: build.publication, signal: abort.signal,
            onProgress: (progress) => update(record.id, { stage: progress.cd?.deployed ? 'http' : 'cd', cd: progress.cd, public_http: progress.public_http }) }));
          if (deletionRequested(record.id)) {
            if (!(result.cd?.deployed === true || ['blocked', 'failed'].includes(result.cd?.state) && !result.error?.outcome_unknown))
              await update(record.id, { status: 'unknown', error: operationError('CD_OUTCOME_UNKNOWN', true) });
            return;
          }
          const succeeded = result.cd?.deployed === true && typeof result.cd.revision === 'string' && result.cd.revision.length > 0 && result.public_http?.state === 'succeeded' && result.public_http.verified_at && /^https?:\/\//.test(result.public_http.url || '');
          const status = succeeded ? 'succeeded' : resume || result.error?.outcome_unknown ? 'unknown' : ['blocked', 'failed'].includes(result.cd?.state) ? result.cd.state : 'unknown';
          await update(record.id, { status, stage: succeeded ? 'complete' : result.cd?.deployed ? 'http' : 'cd', cd: result.cd, public_http: result.public_http,
            url: succeeded ? (result.public_http.site_url || result.public_http.url) : null, error: succeeded ? null : operationError(result.error?.code || 'CD_UNVERIFIED', status === 'unknown') });
          if (status === 'unknown' && record.application_id) cdRecoveries.add(record.id);
          return;
        }
        if (['failed', 'publication_unverified'].includes(build.status)) {
          await update(record.id, ciFailure(build)); return;
        }
        return;
      }
    } catch (error) {
      const known = error instanceof EnvironmentError || error instanceof ProductError;
      const unknown = resume || !known || error.outcomeUnknown;
      const status = error instanceof ProductError ? 'blocked' : unknown ? 'unknown' : 'blocked', saved = store.read().operations[record.id], cd = saved?.cd;
      if (!abort.signal.aborted) await update(record.id, { status, ...(cd?.state === 'running' ? { cd: { ...cd, state: status } } : {}),
        ...(saved.stage === 'ci' && error.code === 'CI_OBSERVATION_REJECTED' ? { ci: { ...saved.ci, observation: {
          ...saved.ci.observation, checked_at: new Date().toISOString(), last_success_at: saved.ci.observation?.last_success_at || null,
          error: { code: error.code, message: error.message, retryable: false, ...(error.upstream_status ? { upstream_status: error.upstream_status } : {}) }, next_retry_at: null } } } : {}),
        error: { ...operationError(known ? error.code : store.read().operations[record.id]?.stage === 'ci' ? 'CI_OBSERVATION_FAILED' : 'CD_OUTCOME_UNKNOWN', unknown),
          ...(error instanceof ProductError ? { message: error.message } : {}) } });
      if (status === 'unknown' && record.application_id && ['cd', 'http'].includes(saved.stage)) cdRecoveries.add(record.id);
    }
  }
  // Updates retain the existing deployment identity, worker and durable source snapshot.
  const sourceDigest = (files) => digest(files.map(({ path, content }) => [path, createHash('sha256').update(content).digest('hex')]).sort());
  const sourceUnavailable = () => new ProductError(409, 'SOURCE_NOT_AVAILABLE', '이 배포의 검증된 소스를 제공할 수 없습니다.');
  function successfulDeployment(record) {
    return record.status === 'succeeded' && record.cd?.deployed === true && record.cd?.revision
      && record.public_http?.state === 'succeeded' && record.public_http.verified_at && /^https?:\/\//.test(record.public_http.url || '');
  }
  function applicationVersions(state, application) {
    const rows = Object.values(state.operations).filter((row) => row.kind === 'deployments'
      && row.application_id === application.id && row.app === application.app && row.target_id === application.target_id
      && row.session_id === application.session_id).reverse();
    const current = rows.find(successfulDeployment) || null;
    const newer = current ? rows.slice(0, rows.indexOf(current)) : rows;
    const uncertain = newer.some((row) => row.cd?.state === 'running' || row.cd?.state === 'unknown'
      || row.cd?.deployed === true || (row.status === 'unknown' || row.error?.outcome_unknown) && ['cd', 'http'].includes(row.stage));
    return { current, latest: rows[0] || null, state: uncertain ? 'unverified' : current ? 'verified' : 'not_deployed' };
  }
  function publicApplication(state, application) {
    const versions = applicationVersions(state, application);
    return { ...publicRecord(application), current_deployment: versions.current ? publicRecord(versions.current) : null,
      latest_deployment: versions.latest ? publicRecord(versions.latest) : null, current_deployment_state: versions.state };
  }
  function readyApplication(state, id, sessionId) {
    const application = applicationFor(state, id, sessionId);
    personal.writable(state, application.environment_target_id, sessionId);
    if (application.status !== 'ready' || applicationVersions(state, application).state === 'unverified')
      throw new ProductError(409, 'APPLICATION_RECONCILE_REQUIRED', '앱 등록 또는 현재 배포 버전을 먼저 확인해야 합니다.');
    if (!service || typeof applicationAdapter?.deployPublished !== 'function') throw unavailable();
    return application;
  }
  async function submittedFiles(record) {
    let files;
    try { files = await store.readSnapshot(record.id); }
    catch { throw sourceUnavailable(); }
    if (sourceDigest(files) !== record.source_digest) throw sourceUnavailable();
    return files;
  }
  async function deployedFiles(record) {
    if (!successfulDeployment(record) || typeof service?.sourceFiles !== 'function') throw sourceUnavailable();
    const publication = record.publication || (await readBuild(String(record.ci.run_id), record.session_id)).publication;
    if (!publication || String(publication.run_id) !== String(record.ci.run_id) || publication.app !== record.app
        || publication.target_id !== record.target_id || publication.source_commit !== record.source_commit)
      throw new ProductError(502, 'SOURCE_VERIFICATION_FAILED', '배포와 소스 게시 기록의 연결을 확인할 수 없습니다.');
    const files = await service.sourceFiles(publication);
    if (files === null) throw sourceUnavailable();
    try { return validateFiles(files); }
    catch { throw new ProductError(502, 'SOURCE_INVALID', '검증된 배포 소스의 파일 목록이 잘못되었습니다.'); }
  }
  async function createUpdate(applicationId, input, key, materialize, sessionId = null) {
    key = idempotencyKey(key);
    return store.transaction(async (state) => {
      const application = applicationFor(state, applicationId, sessionId, true);
      if (!input || typeof input !== 'object' || Array.isArray(input)
          || Object.keys(input).some((name) => !['source_type', 'files', 'repository_url', 'source_name'].includes(name))
          || !['folder', 'zip', 'github'].includes(input.source_type)
          || (input.source_type === 'github' ? typeof input.repository_url !== 'string' || input.files !== undefined
            : !Array.isArray(input.files) || input.repository_url !== undefined)) throw invalid('업데이트에는 새 소스만 입력하세요. 앱과 배포 대상은 변경할 수 없습니다.');
      const fingerprint = inputFingerprint({ ...input, app: application.app, target_id: application.target_id });
      const scopedKey = scopeKey(`updates:${applicationId}`, key, sessionId), existingId = state.keys[scopedKey];
      if (existingId) {
        const existing = state.operations[existingId];
        if (existing.fingerprint !== fingerprint) throw new ProductError(409, 'IDEMPOTENCY_CONFLICT', '같은 키로 다른 입력을 보낼 수 없습니다.');
        return publicRecord(existing);
      }
      checkUncertainResource(state, application);
      readyApplication(state, applicationId, sessionId);
      const baseline = applicationVersions(state, application).current;
      if (!baseline) throw new ProductError(409, 'BASE_DEPLOYMENT_REQUIRED', '업데이트 기준으로 사용할 성공한 배포가 없습니다.');
      checkCapacity(state);
      let before, baselineKind = 'deployed';
      try { before = await deployedFiles(baseline); }
      catch (error) {
        if (error.code !== 'SOURCE_NOT_AVAILABLE') throw error;
        before = await submittedFiles(baseline); baselineKind = 'submitted';
      }
      const source = input.source_type === 'github' ? await materialize(input.repository_url) : input;
      const files = validateFiles(source.files);
      if (documentationOnly(files)) throw invalid(documentationOnlyMessage);
      const origin = input.source_type === 'github' ? source.source : null;
      if (origin && (origin.type !== 'github' || !/^https:\/\/github\.com\/[A-Za-z0-9-]+\/[A-Za-z0-9._-]+$/.test(origin.repository)
          || !/^[a-f0-9]{40}$/.test(origin.sha)) || input.source_type === 'github' && !origin) throw invalid('GitHub 소스의 저장소와 고정 SHA를 확인할 수 없습니다.');
      const previous = new Map(before.map((file) => [file.path, file.content]));
      const incoming = new Set(files.map((file) => file.path));
      const changes = { added: [], modified: [], deleted: [...previous.keys()].filter((path) => !incoming.has(path)).sort(), unchanged: 0 };
      for (const file of files) {
        const old = previous.get(file.path);
        if (old === undefined) changes.added.push(file.path);
        else if (!old.equals(file.content)) changes.modified.push(file.path);
        else changes.unchanged++;
      }
      changes.added.sort(); changes.modified.sort();
      const sourceBytes = files.reduce((sum, file) => sum + Buffer.byteLength(file.path) + Math.ceil(file.content.length / 3) * 4 + 128, 0);
      checkCapacity(state, sourceBytes);
      const id = randomUUID(), now = new Date().toISOString();
      await store.snapshot(id, files);
      const record = { id, kind: 'deployments', application_id: application.id, app: application.app, target_id: application.target_id,
        environment_target_id: application.environment_target_id, session_id: sessionId, status: 'preview', stage: 'review',
        base_deployment_id: baseline.id, base_revision: baseline.cd.revision, baseline_kind: baselineKind,
        expires_at: new Date(Date.now() + 30 * 60 * 1000).toISOString(), changes,
        no_changes: baselineKind === 'deployed' && !changes.added.length && !changes.modified.length && !changes.deleted.length,
        source_comparison_only: baselineKind === 'submitted',
        source_origin: origin ? { type: 'github', repository: origin.repository, sha: origin.sha } : null,
        source: origin, source_bytes: sourceBytes, source_digest: sourceDigest(files), fingerprint, key,
        ci: { run_id: null, state: 'not_started', steps: [], publication_artifact_id: null, producer_attempt: null },
        cd: { state: 'not_started', revision: null, deployed: false }, public_http: { state: 'not_run', verified_at: null, url: null },
        url: null, actions_url: null, error: null, created_at: now, updated_at: now };
      state.operations[id] = record; state.keys[scopedKey] = id;
      return publicRecord(record);
    });
  }
  async function startUpdate(id, options = {}, sessionId = null) {
    if (!options || typeof options !== 'object' || Array.isArray(options) || Object.keys(options).some((key) => key !== 'rebuild')
        || options.rebuild !== undefined && typeof options.rebuild !== 'boolean') throw invalid('rebuild는 참 또는 거짓이어야 합니다.');
    const accepted = await store.transaction(async (state) => {
      const record = state.operations[id];
      const application = record?.application_id && state.applications[record.application_id];
      if (!record || record.session_id !== sessionId || record.kind !== 'deployments' || !record.base_deployment_id
          || !application || application.session_id !== sessionId) throw new ProductError(404, 'NOT_FOUND', '업데이트를 찾을 수 없습니다.');
      if (record.status !== 'preview') return { record, replay: true };
      if (Date.parse(record.expires_at) <= Date.now()) throw new ProductError(409, 'UPDATE_EXPIRED', '미리보기가 만료되었습니다. 새 소스를 다시 확인하세요.');
      readyApplication(state, record.application_id, sessionId);
      const current = applicationVersions(state, application).current;
      if (current?.id !== record.base_deployment_id || current.cd.revision !== record.base_revision)
        throw new ProductError(409, 'UPDATE_BASE_CHANGED', '기준 배포가 변경되었습니다. 미리보기를 다시 만드세요.');
      checkUncertainResource(state, record, id);
      const files = await submittedFiles(record);
      Object.assign(record, { status: record.no_changes && !options.rebuild ? 'unchanged' : 'queued',
        stage: record.no_changes && !options.rebuild ? 'complete' : 'ci', rebuild: options.rebuild === true, updated_at: new Date().toISOString() });
      if (record.status === 'queued') enqueue(state, record);
      return { record, replay: record.status === 'unchanged', input: { app: record.app, target_id: record.target_id, files, source: record.source } };
    });
    if (!accepted.replay) void pump();
    return publicRecord(accepted.record);
  }
  const personal = createPersonalEnvironments({ store, adapter: personalAdapter, applicationAdapter, launch, publicApplication });
  await personal.restore();
  async function runDeployment(record) {
    if (abort.signal.aborted) return;
    try {
      if (deletionRequested(record.id)) return;
      const state = store.read(), plan = record.plan_id ? state.plans[record.plan_id] : null;
      const input = { app: record.app, target_id: record.target_id, files: await submittedFiles(record), source: record.source };
      if (record.base_deployment_id) {
        const application = readyApplication(state, record.application_id, record.session_id);
        const current = applicationVersions(state, application).current;
        if (current?.id !== record.base_deployment_id || current.cd.revision !== record.base_revision)
          throw new ProductError(409, 'UPDATE_BASE_CHANGED', '기준 배포가 변경되었습니다. 미리보기를 다시 만드세요.');
      }
      if (plan) await environmentAdapter.verifyPlan(plan);
      if (record.application_id && store.read().applications[record.application_id]?.status !== 'ready') {
        const appId = record.application_id;
        const application = store.read().applications[appId];
        if (application?.status !== 'queued') throw new ProductError(409, 'APPLICATION_RECONCILE_REQUIRED', '앱 상태가 변경되어 대기 중인 배포를 실행하지 않았습니다.');
        await update(record.id, { status: 'running', stage: 'registration' });
        await store.transaction((state) => { if (!state.operations[record.id].deletion_requested) state.applications[appId].status = 'registering'; });
        try {
          if (deletionRequested(record.id)) return;
          const registered = await writeInfrastructure(() => applicationAdapter.register(application));
          await store.transaction((state) => { Object.assign(state.applications[appId], registered,
            state.operations[record.id].deletion_requested ? { status: 'deleting' } : {}); });
        } catch (error) {
          const unknown = error.outcomeUnknown !== false;
          // This read-only preflight precedes the native registration intent; only a new explicit request may retry it.
          const unstarted = error.code === 'APPLICATION_AWS_ROUTE_PREFLIGHT_FAILED' && error.outcomeUnknown === false;
          await store.transaction((state) => { state.applications[appId].status = unstarted ? 'queued' : unknown ? 'unknown' : 'blocked'; });
          await update(record.id, { status: unknown ? 'unknown' : 'blocked', error: operationError(error.code || 'APPLICATION_REGISTRATION_FAILED', unknown) });
          return;
        }
        if (deletionRequested(record.id)) return;
      }
      if (plan) {
        await update(record.id, { status: 'running', stage: 'environment' });
        const environment = await writeInfrastructure(() => environmentAdapter.execute(plan, { id: record.environment_id,
          onProgress: (value) => update(record.id, { environment: value }) }));
        await update(record.id, { environment });
        if (environment.status !== 'succeeded' || environment.deployment_supported !== true || environment.runtime_target_id !== record.target_id) {
          await update(record.id, { status: environment.status === 'succeeded' ? 'blocked' : environment.status,
            error: environment.error || operationError('DEPLOYMENT_TARGET_NOT_REGISTERED', false) }); return;
        }
      }
      const result = await submit(record, input);
      if (result && !deletionRequested(record.id)) await observe(record, String(result.run_id));
    } catch (error) {
      if (store.read().operations[record.id]?.error) return; // Keep precise CI submission diagnostics.
      const known = error instanceof ProductError && !error.outcomeUnknown || error.outcomeUnknown === false;
      await update(record.id, { status: known ? 'blocked' : 'unknown', error: operationError(known ? error.code || 'DEPLOYMENT_PRECHECK_FAILED' : 'STACK_OUTCOME_UNKNOWN', !known) });
    }
  }
  async function generateApplicationPlan(record, asynchronous = true) {
    const { id, action, application_id: applicationId } = record.public;
    const sessionId = record.session_id;
    const cancelling = record.cancelling_deployment_id;
    try {
      const state = store.read(), application = applicationFor(state, applicationId, sessionId, true);
      if (record.application_snapshot !== applicationSnapshot(state, application, Boolean(cancelling)))
        throw new ProductError(409, 'APPLICATION_PLAN_STALE', '계획 확인 중 앱 상태가 바뀌었습니다. 새 계획을 확인하세요.');
      lifecycleAvailable(state, application, action, cancelling);
      // Cloud inspection must not hold the store's writer queue for up to ten minutes.
      const plan = cancelling ? await applicationAdapter.planPendingDeletion(application, { id, deploymentId: cancelling })
        : await applicationAdapter.planLifecycle(application, { id, action });
      if (plan.public?.id !== id || plan.public.application_id !== applicationId || plan.public.action !== action)
        throw new EnvironmentError('APPLICATION_LIFECYCLE_RECEIPT_INVALID', 502);
      if (cancelling && (plan.private?.deferred !== true || plan.private.deployment_id !== cancelling))
        throw new EnvironmentError('APPLICATION_LIFECYCLE_RECEIPT_INVALID', 502);
      return await store.transaction((state) => {
        const current = applicationFor(state, applicationId, sessionId, true);
        if (record.application_snapshot !== applicationSnapshot(state, current, Boolean(cancelling)))
          throw new ProductError(409, 'APPLICATION_PLAN_STALE', '계획 확인 중 앱 상태가 바뀌었습니다. 새 계획을 확인하세요.');
        if (cancelling) plan.public.resources = [...plan.public.resources, { kind: 'DeploymentOperation', name: cancelling }];
        plan.public = { ...plan.public, status: 'ready', created_at: record.public.created_at, updated_at: new Date().toISOString() };
        state.plans[id] = { ...record, ...plan };
        return structuredClone(plan.public);
      });
    } catch (cause) {
      await store.transaction((state) => {
        const code = cause instanceof ProductError || cause instanceof EnvironmentError ? cause.code : 'APPLICATION_PLAN_UNAVAILABLE';
        Object.assign(state.plans[id].public, { status: 'failed', updated_at: new Date().toISOString(),
          error: { code, outcome_unknown: false, message: cause instanceof ProductError ? cause.message
            : '실행 계획을 확인하지 못했습니다. 앱 변경은 실행되지 않았습니다. 다시 확인하거나 오류 코드를 운영자에게 전달하세요.' } });
      });
      if (!asynchronous) throw cause;
    }
  }
  // Recover only read-only plans. Applying an operation still requires its original
  // confirmation and idempotency key; uncertain mutations are never replayed here.
  for (const record of Object.values(store.read().plans)) {
    if (record.kind === 'application-lifecycle' && record.public?.status === 'planning')
      launch(() => generateApplicationPlan(record));
  }
  let releasePaused = false;
  const queueTimer = setInterval(pump, Math.min(pollInterval, 1000));
  queueTimer.unref();
  void pump();
  return {
    pauseForRelease() {
      releasePaused = true;
      return !workers.size && !pumping;
    },
    resumeAfterRelease() { releasePaused = false; void pump(); },
    dashboard: store.dashboard, personal,
    executeOpenStack: (targetId, request, ownerSessionId) => personal.execute(targetId, request, ownerSessionId),
    registrations: store.registrations,
    createUpdate, startUpdate,
    resolveApplication({ environment, provider, app, target_id }, sessionId = null) {
      if (typeof app !== 'string' || !APP_NAME.test(app)) throw invalid('앱 이름을 확인하세요.');
      if (target_id !== undefined && (typeof target_id !== 'string' || !/^[A-Za-z0-9._-]{1,128}$/.test(target_id)))
        throw invalid('개인 환경 대상을 확인하세요.');
      const selected = resolveSelection({ deployment_selection: { environment, provider,
        ...(target_id !== undefined ? { target_id } : {}) }, source_name: app }, sessionId);
      const state = store.read();
      const identity = applicationAdapter?.targets?.[selected.target_id]
        ? applicationAdapter.describe(selected.target_id, selected.app) : null;
      const application = identity && state.applications[identity.id];
      if (application && application.session_id !== sessionId)
        throw new ProductError(409, 'APPLICATION_OWNERSHIP_CONFLICT', '같은 환경의 이 앱 이름은 다른 세션에 등록되어 있습니다. 다른 이름을 사용하세요.');
      return { app: selected.app, environment_target_id: selected.target_id,
        application: application ? publicApplication(state, application) : null };
    },
    async sourceFiles(id, variant, sessionId = null) {
      const record = find('deployments', id, sessionId);
      if (variant === 'failed') return diagnostics.source(id, sessionId);
      if (!['submitted', 'deployed'].includes(variant)) throw invalid('지원하지 않는 소스 종류입니다.');
      return variant === 'submitted' ? submittedFiles(record) : deployedFiles(record);
    },
    applications(sessionId = null) {
      const state = store.read();
      return Object.values(state.applications)
        .filter((row) => owns(row, sessionId) && row.status !== 'deleted')
        .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || '') || b.id.localeCompare(a.id))
        .map((row) => publicApplication(state, row));
    },
    getApplication(id, sessionId = null) {
      const state = store.read();
      return publicApplication(state, applicationFor(state, id, sessionId));
    },
    async createApplicationPlan(applicationId, input, sessionId = null, { asynchronous = false } = {}) {
      if (!exact(input, ['action']) || !lifecycleActions.includes(input.action)) throw invalid('action은 stop, start, delete 중 하나여야 합니다.');
      const pending = await store.transaction((state) => {
        const application = applicationFor(state, applicationId, sessionId, true);
        const cancelling = deletionCandidate(state, applicationId, input.action);
        checkFree(state, sessionId, cancelling); lifecycleAvailable(state, application, input.action, cancelling);
        const existing = Object.values(state.plans).find((plan) => plan.kind === 'application-lifecycle'
          && plan.session_id === sessionId && plan.public.application_id === applicationId && plan.public.status === 'planning');
        if (existing) {
          if (asynchronous && existing.public.action === input.action) return { record: existing, replay: true };
          throw new ProductError(409, 'APPLICATION_PLAN_IN_PROGRESS', '이미 앱의 실행 계획을 확인하고 있습니다. 기존 계획 상태를 다시 조회하세요.');
        }
        if (Object.keys(state.plans).length >= maxOperations) throw new ProductError(409, 'CAPACITY_EXCEEDED', '계획 보관 한도에 도달했습니다.');
        if (cancelling && typeof applicationAdapter.planPendingDeletion !== 'function') throw unavailable();
        const id = randomUUID(), now = new Date().toISOString();
        const record = { kind: 'application-lifecycle', session_id: sessionId,
          application_snapshot: applicationSnapshot(state, application, Boolean(cancelling)),
          cancelling_deployment_id: cancelling,
          public: { id, application_id: applicationId, action: input.action, status: 'planning',
            created_at: now, updated_at: now, resources: [], retained: [] } };
        state.plans[id] = record;
        return { record, application: structuredClone(application), replay: false };
      });
      if (asynchronous) {
        if (!pending.replay) launch(() => generateApplicationPlan(pending.record)); // Durable read-only work survives a process restart.
        return structuredClone(pending.record.public);
      }
      return generateApplicationPlan(pending.record, false);
    },
    getApplicationPlan(applicationId, planId, sessionId = null) {
      const state = store.read();
      applicationFor(state, applicationId, sessionId, true);
      const plan = Object.hasOwn(state.plans, planId) ? state.plans[planId] : null;
      if (!plan || plan.session_id !== sessionId || plan.kind !== 'application-lifecycle' || plan.public.application_id !== applicationId)
        throw new ProductError(404, 'NOT_FOUND', '앱 계획을 찾을 수 없습니다.');
      return structuredClone({ ...plan.public, ...(plan.operation_id ? { operation_id: plan.operation_id } : {}) });
    },
    async createApplicationOperation(applicationId, input, key, sessionId = null) {
      idempotencyKey(key);
      if (!exact(input, ['action', 'plan_id', 'plan_hash', 'confirmation'], ['delete_data'])
          || !lifecycleActions.includes(input.action) || !lifecycleId.test(input.plan_id || '')
          || !lifecycleHash.test(input.plan_hash || '') || typeof input.confirmation !== 'string'
          || (input.action === 'delete' ? input.delete_data !== true : input.delete_data !== undefined && input.delete_data !== false))
        throw invalid('유효한 작업·계획·확인이 필요하며 삭제에는 delete_data=true를 입력해야 합니다.');
      const accepted = await store.transaction(async (state) => {
        const application = applicationFor(state, applicationId, sessionId, true);
        const fingerprint = digest({ application_id: applicationId, action: input.action, plan_id: input.plan_id,
          plan_hash: input.plan_hash, confirmation: input.confirmation, delete_data: input.delete_data === true });
        const existingId = state.keys[scopeKey('application-lifecycle', key, sessionId)];
        if (existingId) {
          const record = state.operations[existingId];
          if (record.fingerprint !== fingerprint) throw new ProductError(409, 'IDEMPOTENCY_CONFLICT', '같은 키로 다른 작업을 보낼 수 없습니다.');
          return { record, replay: true };
        }
        if (input.confirmation !== application.app) throw invalid('현재 앱 이름을 정확히 입력해야 합니다.');
        const plan = Object.hasOwn(state.plans, input.plan_id) ? state.plans[input.plan_id] : null;
        if (!plan || plan.session_id !== sessionId || plan.kind !== 'application-lifecycle' || plan.public.application_id !== applicationId)
          throw new ProductError(404, 'NOT_FOUND', '앱 계획을 찾을 수 없습니다.');
        if ((plan.public.status && plan.public.status !== 'ready') || plan.operation_id || plan.public.action !== input.action || plan.public.plan_hash !== input.plan_hash
            || !Number.isFinite(Date.parse(plan.public.expires_at)) || Date.parse(plan.public.expires_at) <= Date.now()
            || plan.application_snapshot !== applicationSnapshot(state, application, Boolean(plan.private?.deferred))) throw new ProductError(409, 'APPLICATION_PLAN_STALE', '계획이 만료되었거나 앱 상태가 바뀌었습니다. 새 계획을 확인하세요.');
        const cancelling = deletionCandidate(state, applicationId, input.action);
        if (cancelling && plan.cancelling_deployment_id !== cancelling) throw new ProductError(409, 'APPLICATION_PLAN_STALE', '배포 상태가 바뀌어 새 삭제 계획을 확인해야 합니다.');
        checkFree(state, sessionId, cancelling); checkCapacity(state); lifecycleAvailable(state, application, input.action, cancelling);
        await applicationAdapter.verifyLifecyclePlan(application, plan);
        const quiescing = cancelling || (plan.private?.deferred ? plan.cancelling_deployment_id : null);
        if (quiescing && (state.operations[quiescing]?.kind !== 'deployments' || state.operations[quiescing].application_id !== applicationId))
          throw new ProductError(409, 'APPLICATION_PLAN_STALE', '삭제할 앱의 배포 기록이 달라졌습니다.');
        const id = randomUUID(), now = new Date().toISOString();
        const record = { id, kind: 'application-lifecycle', session_id: sessionId, application_id: applicationId,
          app: application.app, target_id: application.target_id, action: input.action, plan_id: input.plan_id,
          plan_hash: input.plan_hash, delete_data: input.delete_data === true, status: 'queued', stage: input.action,
          steps: [], residuals: [], retained: plan.public.retained, error: null, fingerprint, key,
          created_at: now, updated_at: now };
        if (quiescing) { record.cancelling_deployment_id = quiescing; state.operations[quiescing].deletion_requested = id; }
        state.operations[id] = record; state.keys[scopeKey('application-lifecycle', key, sessionId)] = id; plan.operation_id = id;
        Object.assign(application, { status: { stop: 'stopping', start: 'starting', delete: 'deleting' }[input.action],
          lifecycle_operation_id: id, updated_at: now });
        return { record, plan, application: structuredClone(application) };
      });
      if (!accepted.replay) launch(async () => {
        const { record, plan, application } = accepted;
        let result, failureCode;
        try {
          await update(record.id, { status: 'running' });
          let approved = plan;
          if (record.cancelling_deployment_id) {
            await quiesceDeployment(record.cancelling_deployment_id, record.id);
            const refreshed = await applicationAdapter.planLifecycle(application, { id: randomUUID(), action: record.action });
            const allowed = new Set(plan.public.resources.filter((row) => row.kind !== 'DeploymentOperation').map(digest));
            if (refreshed.public.application_id !== applicationId || refreshed.public.action !== 'delete') throw new Error('Deletion binding changed');
            if (!plan.private?.deferred && refreshed.public.resources.some((row) => !allowed.has(digest(row)))) {
              const changed = new Error('Deletion scope changed'); changed.code = 'APPLICATION_PLAN_CHANGED'; throw changed;
            }
            approved = refreshed;
            await store.transaction((state) => { state.operations[record.id].effective_plan_id = refreshed.public.id;
              state.operations[record.id].effective_plan_hash = refreshed.public.plan_hash;
              state.operations[record.id].retained = lifecycleResources(refreshed.public.retained);
              state.operations[record.id].refreshed_plan = refreshed; });
          }
          await update(record.id, { stage: record.action });
          const observed = await applicationAdapter.applyLifecycle(application, approved, { id: record.id, deleteData: record.delete_data });
          if (observed.application_id !== applicationId || observed.action !== record.action || !['succeeded', 'blocked', 'unknown'].includes(observed.status)) throw new Error('Invalid lifecycle result');
          result = { status: observed.status, steps: lifecycleSteps(observed.steps), residuals: lifecycleResources(observed.residuals) };
          if (result.status === 'succeeded' && result.residuals.length) throw new Error('Lifecycle residuals remain');
          if (result.status !== 'succeeded' && !result.residuals.length) result.residuals = lifecycleResources(approved.public.resources);
        } catch (error) {
          failureCode = error.code === 'APPLICATION_PLAN_CHANGED' ? error.code : null;
          result = { status: error.code === 'APPLICATION_PLAN_CHANGED' ? 'blocked' : 'unknown', steps: [], residuals: plan.public.resources };
        }
        await store.transaction((state) => {
          Object.assign(state.operations[record.id], result, { stage: result.status === 'succeeded' ? 'complete' : result.steps.at(-1)?.name || record.action,
            error: result.status === 'succeeded' ? null : operationError(failureCode || 'APPLICATION_LIFECYCLE_UNVERIFIED', result.status === 'unknown'), updated_at: new Date().toISOString() });
          Object.assign(state.applications[applicationId], { status: result.status === 'succeeded'
            ? { stop: 'stopped', start: 'ready', delete: 'deleted' }[record.action] : 'unknown', updated_at: new Date().toISOString() });
        });
      });
      return publicRecord(accepted.record);
    },
    getOperation(id, sessionId = null) {
      const record = store.read().operations[id];
      if (!owns(record, sessionId) || !['application-lifecycle', 'target-lifecycle'].includes(record.kind)) throw new ProductError(404, 'NOT_FOUND', '작업을 찾을 수 없습니다.');
      return publicRecord(record);
    },
    list(kind, sessionId, pagination) {
      if (kind === 'plans') return Object.values(store.read().plans).filter((row) => owns(row, sessionId) && !['application-lifecycle', 'target-lifecycle'].includes(row.kind)).reverse().map((row) => structuredClone(row.public));
      const { records, hasMore, total } = store.operationPage(kind, sessionId, pagination);
      const items = records.map((row) => ({ id: kind === 'builds' ? String(row.ci.run_id) : row.id, kind, status: row.status,
        ...Object.fromEntries(['app', 'application_id', 'environment_target_id', 'target_id', 'stage', 'created_at', 'updated_at'].filter((key) => row[key] !== undefined).map((key) => [key, row[key]])) }));
      return { items, next_marker: hasMore ? items.at(-1).id : null, total };
    },
    deploymentOptions,
    targets(sessionId = null) {
      const state = store.read();
      const ids = new Set(sharedTargets);
      for (const id of Object.keys(applicationAdapter?.targets || {})) ids.add(id);
      for (const operation of Object.values(state.operations)) {
        const id = operation.kind === 'environments' ? operation.runtime_target_id : operation.environment?.runtime_target_id;
        if (registeredEnvironment(state, id, sessionId)) ids.add(id);
      }
      return [...ids].filter((id) => !state.personal.targets[id]).map((id) => {
        const staticTarget = deployPublished?.targets?.[id];
        const staticAvailable = Boolean(deployPublished && (!deployPublished.targets || staticTarget));
        const registered = staticAvailable ? staticTarget : registeredEnvironment(state, id, sessionId);
        const applicationEnvironment = applicationAdapter?.targets?.[id];
        const available = Boolean(applicationEnvironment && applicationEnvironment.automaticDelivery !== false) || staticAvailable || Boolean(registered);
        return { id, label: id === targetId ? target?.label || id : id,
          provider: applicationEnvironment?.provider || [...selections].find(([, selected]) => selected === id)?.[0] || registered?.provider || null, environment: 'registered',
          environment_id: registered?.environment_id || null,
          ...(applicationEnvironment ? { deployment_scope: 'environment' } : registered?.applicationName ? { application_name: registered.applicationName, deployment_scope: 'registered_application' } : {}),
          capabilities: { ci_submission: Boolean(service), application_deployment: available,
            database_configuration: !staticAvailable && registered?.database_configuration === true },
          runtime: { status: 'unknown', observed_at: null }, blockers: available ? [] : ['CD_ADAPTER_NOT_CONFIGURED'] };
      }).concat(personal.list(sessionId));
    },
    async getTargetObservation(id, sessionId = null) {
      const target = TARGET_ID.test(id) && this.targets(sessionId).find((item) => item.id === id);
      if (!target) throw new ProductError(404, 'NOT_FOUND', '등록된 대상을 찾을 수 없습니다.');
      let observation = await observeMetrics({ target_id: id, app: target.application_name ?? null });
      if (target.scope === 'owned') observation = personal.observation(id, sessionId, observation);
      const health = observation.metrics.runtime_healthz ?? { state: 'not_configured', observed_at: null };
      return { ...observation, environment_id: target.environment_id ?? null,
        runtime: { status: health.state === 'ready' ? (health.value === 1 ? 'healthy' : 'unhealthy') : 'unknown',
          observation_state: health.state, observed_at: health.observed_at } };
    },
    async createBuild(input, materialize, sessionId = null) {
      const reserved = await reserve('builds', input, null, materialize, sessionId);
      const result = await submit(reserved.record, reserved.input);
      launch(() => observe(reserved.record, String(result.run_id)));
      return result;
    },
    getBuild: readBuild,
    async legacyStatus(id, sessionId = null) {
      const state = store.read();
      const binding = Object.hasOwn(state.bindings, id) ? state.bindings[id] : null;
      if (!binding || !owns(state.operations[binding.operation_id], sessionId)) {
        throw new ProductError(404, 'NOT_FOUND', '접수한 실행을 찾을 수 없습니다.');
      }
      return service.status(id, binding.target_id);
    },
    async createDeployment(input, key, materialize, sessionId = null) {
      input = resolveSelection(input, sessionId);
      personal.writable(store.read(), input.target_id, sessionId);
      if (!input.plan_id && applicationAdapter?.targets?.[input.target_id]) {
        const application = applicationAdapter.describe(input.target_id, input.app);
        input = { ...input, environment_target_id: input.target_id, target_id: application.target_id };
      }
      const reserved = await reserve('deployments', input, idempotencyKey(key), materialize, sessionId);
      if (!reserved.replay) void pump();
      return publicRecord(reserved.record);
    },
    // Private operator route only: reuse verified stored bytes and preserve the original owner.
    async replaySubmittedSource(id, environmentTargetId, packaging = {}) {
      const record = find('deployments', id);
      const sameTarget = environmentTargetId === record.environment_target_id;
      const notSent = record.status === 'failed' && record.stage === 'ci' && !record.ci?.run_id
        && !record.error?.outcome_unknown && record.dispatch?.state !== 'requesting'
        && ['SOURCE_REGISTRATION_FAILED', 'SOURCE_CHECKPOINT_FAILED', 'CI_DISPATCH_NOT_SENT'].includes(record.error?.code);
      if (record.deletion_requested || !(sameTarget ? notSent : successfulDeployment(record))
          || !applicationAdapter?.targets?.[environmentTargetId])
        throw new ProductError(409, 'SOURCE_REPLAY_REJECTED', '미접수로 확인된 요청 또는 성공한 배포의 저장 소스만 사용할 수 있습니다.');
      if (!packaging || typeof packaging !== 'object' || Array.isArray(packaging)
          || Object.entries(packaging).some(([path, content]) => !['Dockerfile', '.dockerignore', '.railshot/railshot.yaml'].includes(path)
            || typeof content !== 'string' || Buffer.byteLength(content) > 65536))
        throw invalid('복구 시에는 Dockerfile, .dockerignore, 배포 설정만 추가하거나 수정할 수 있습니다.');
      const files = await submittedFiles(record);
      for (const [path, content] of Object.entries(packaging)) {
        const existing = files.find(file => file.path === path);
        if (existing) existing.content = Buffer.from(content); else files.push({ path, content: Buffer.from(content) });
      }
      return this.createDeployment({ app: record.app, target_id: environmentTargetId, source_type: 'folder', files },
        `source-replay.${record.id}.${digest([environmentTargetId, sourceDigest(files)]).slice(0, 32)}`, undefined, record.session_id);
    },
    async resumePublishedOperation({ operation_id: id, source_commit: source, run_id: run }) {
      const record = store.read().operations[id];
      if (!record?.session_id || record.source_commit !== source || record.ci?.run_id !== run)
        throw new ProductError(409, 'RESUME_BINDING_MISMATCH', '재개 요청의 소스와 CI 실행이 원래 배포와 일치하지 않습니다.');
      return this.resumeDeployment(id, record.session_id, 'operator');
    },
    async resumeDeployment(id, sessionId = null, initiatedBy = 'owner') {
      const record = await store.transaction((state) => {
        const operation = Object.hasOwn(state.operations, id) ? state.operations[id] : null;
        const application = operation?.application_id && state.applications[operation.application_id];
        if (application) personal.writable(state, application.environment_target_id, sessionId);
        // Unlike the legacy maintenance reads, resume always requires an exact cookie-session owner.
        if (!sessionId || operation?.kind !== 'deployments' || operation.session_id !== sessionId
            || !application || application.session_id !== sessionId) {
          throw new ProductError(404, 'NOT_FOUND', '이 세션에서 재개할 배포를 찾을 수 없습니다.');
        }
        const ci = operation.ci;
        const lifecycleId = application.lifecycle_operation_id;
        const lifecycle = lifecycleId && Object.hasOwn(state.operations, lifecycleId) ? state.operations[lifecycleId] : null;
        const completedLifecycle = !lifecycleId || lifecycle?.id === lifecycleId
          && lifecycle.kind === 'application-lifecycle' && lifecycle.application_id === application.id
          && lifecycle.session_id === sessionId && lifecycle.target_id === operation.target_id
          && lifecycle.app === application.app && lifecycle.status === 'succeeded' && ['stop', 'start'].includes(lifecycle.action);
        if (!['unknown', 'blocked', 'failed'].includes(operation.status) || !['cd', 'http'].includes(operation.stage)
            || application.status !== 'ready' || application.deletion_requested || operation.deletion_requested || !completedLifecycle
            || ci?.state !== 'published'
            || typeof applicationAdapter?.deployPublished !== 'function'
            || !/^\d+$/.test(ci.run_id) || !/^[a-f0-9]{40}$/.test(operation.source_commit || '')
            || typeof ci.publication_artifact_id !== 'string' || !/^[1-9]\d*$/.test(ci.publication_artifact_id)
            || !Number.isSafeInteger(ci.producer_attempt) || ci.producer_attempt < 1
            || !ci.images || typeof ci.images !== 'object' || Array.isArray(ci.images) || !Object.keys(ci.images).length
            || Object.values(ci.images).some((image) => typeof image !== 'string' || !/^ghcr\.io\/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$/.test(image))) {
          throw new ProductError(409, 'DEPLOYMENT_NOT_RESUMABLE', '이미지 게시 후 적용 또는 확인 단계에서 멈춘 앱 배포만 재개할 수 있습니다.');
        }
        const binding = state.bindings[ci.run_id];
        const expected = applicationAdapter.describe(operation.environment_target_id, operation.app);
        if (!binding || binding.operation_id !== id || binding.app !== operation.app
            || binding.target_id !== operation.target_id || binding.source_commit !== operation.source_commit
            || application.id !== operation.application_id || application.target_id !== operation.target_id
            || application.app !== operation.app || application.environment_target_id !== operation.environment_target_id
            || expected.id !== application.id || expected.target_id !== application.target_id
            || expected.provider !== application.provider) {
          throw new ProductError(409, 'RESUME_BINDING_MISMATCH', '원래 앱·환경·CI 실행과 연결이 일치하지 않습니다.');
        }
        // Match fresh app admission; writeInfrastructure still serializes shared writes.
        const conflicts = Object.fromEntries(Object.entries(state.operations).filter(([, row]) =>
          !row.application_id || row.application_id === operation.application_id));
        checkFree({ operations: conflicts }, sessionId, id);
        checkUncertainResource(state, operation, id);
        if (operation.queue) { delete operation.queue.released_at; delete operation.queue.release_reason; }
        Object.assign(operation, { status: 'running', error: null, resumed_at: new Date().toISOString(),
          resume_count: (operation.resume_count || 0) + 1, resume_initiated_by: initiatedBy, updated_at: new Date().toISOString() });
        return operation;
      });
      launch(() => observe(record, String(record.ci.run_id), { resume: true }), record.id);
      return publicRecord(record);
    },
    async getDeployment(id, sessionId = null, { view = 'full' } = {}) {
      let record = publicRecord(find('deployments', id, sessionId));
      // History needs the durable record, not a synchronous GitHub/metrics refresh.
      if (view === 'record') return record;
      // Older completed records can acquire diagnostics without replaying CI or CD.
      if (record.stage === 'ci' && ['failed', 'blocked'].includes(record.status) && record.ci?.run_id && record.ci.diagnostics?.state !== 'ready'
          && (!record.ci.diagnostics_checked_at || Date.now() - Date.parse(record.ci.diagnostics_checked_at) > 30000)) {
        try {
          const build = await readBuild(record.ci.run_id, sessionId);
          if (['failed', 'publication_unverified'].includes(build.status)) {
            await update(id, { ci: ciObservation(build, record.ci), ...ciFailure(build) });
            record = publicRecord(find('deployments', id, sessionId));
          }
        } catch { /* Keep the recorded failure when read-only diagnostics are unavailable. */ }
      }
      return { ...record, observation: await observeMetrics(record) };
    },
    getDeploymentDiagnostics: diagnostics.get,
    classifyDeployment: diagnostics.classify,
    async getDeploymentEvents(id, sessionId = null) {
      const record = find('deployments', id, sessionId);
      const identity = { runId: record.ci?.run_id ? String(record.ci.run_id) : null,
        source_commit: record.source_commit, app: record.app, target_id: record.target_id };
      const response = (envelope, row) => {
        const timeline = publicTelemetry(row);
        return { ...envelope, deployment_id: id, timeline, agent_activity: agentActivity(row, timeline, envelope), progress: summarizeAgentEvents(envelope, timeline) };
      };
      const empty = (state, reason) => response(emptyAgentEvents(identity, state, reason), record);
      if (!identity.runId) return empty('not_started', 'not_dispatched');
      const bound = () => {
        const current = find('deployments', id, sessionId), state = store.read();
        const binding = Object.hasOwn(state.bindings, identity.runId) ? state.bindings[identity.runId] : null;
        return binding?.operation_id === id && binding.app === identity.app && binding.target_id === identity.target_id
          && binding.source_commit === identity.source_commit && current.source_commit === identity.source_commit
          && String(current.ci?.run_id) === identity.runId && current.app === identity.app && current.target_id === identity.target_id;
      };
      if (!bound()) return empty('unavailable', 'binding_mismatch');
      if (typeof service?.events !== 'function') return empty('unavailable', 'not_configured');
      try {
        const observed = await service.events(identity.runId, { source_commit: identity.source_commit, app: identity.app, target_id: identity.target_id });
        if (!bound()) return empty('unavailable', 'binding_mismatch');
        await store.transaction((state) => ingestCiEvents(state.operations[id], observed));
        return bound() ? response(observed, find('deployments', id, sessionId)) : empty('unavailable', 'binding_mismatch');
      } catch { return empty('unavailable', 'upstream_unavailable'); }
    },
    async getDeploymentLogs(id, sessionId = null) {
      const record = publicRecord(find('deployments', id, sessionId));
      const empty = (state) => ({ deployment_id: id, target_id: record.target_id, app: record.app,
        state, checked_at: new Date().toISOString(), entries: [] });
      if (!record.cd?.deployed) return empty('not_deployed');
      // A shared target can be reused even when its image/revision does not change.
      // Only the most recently admitted CD operation may expose its runtime logs.
      const current = () => Object.values(store.read().operations).reverse().find((row) => row.kind === 'deployments'
        && row.target_id === record.target_id && row.app === record.app && row.cd?.state !== 'not_started')?.id === id;
      if (!current()) return empty('superseded');
      const observer = record.application_id ? (value) => applicationAdapter?.observeLogs(store.read().applications[record.application_id], value)
        : record.environment_id ? environmentAdapter?.observeLogs : observeLogs;
      if (!observer) return empty('not_configured');
      try {
        const logs = await (record.environment_id ? observer(record.environment_id, record) : observer(record));
        return current() ? logs : empty('superseded');
      }
      catch { return empty('unavailable'); }
    },
    profiles() { return environmentAdapter?.profiles() || []; },
    async createPlan(input, sessionId = null) {
      if (!environmentAdapter) throw unavailable();
      return store.transaction(async (state) => {
        checkFree(state, sessionId);
        if (Object.keys(state.plans).length >= maxOperations) throw new ProductError(409, 'CAPACITY_EXCEEDED', '계획 보관 한도에 도달했습니다.');
        const id = randomUUID(), plan = await environmentAdapter.plan(input, { id });
        plan.session_id = sessionId;
        state.plans[id] = plan;
        return plan.public;
      });
    },
    getPlan(id, sessionId = null) {
      const plans = store.read().plans;
      const plan = Object.hasOwn(plans, id) ? plans[id] : null;
      if (!owns(plan, sessionId) || plan.kind === 'application-lifecycle') {
        throw new ProductError(404, 'NOT_FOUND', '계획을 찾을 수 없습니다.');
      }
      return plan.public;
    },
    async createEnvironment(input, key, sessionId = null) {
      idempotencyKey(key);
      if (!input || Object.keys(input).length !== 1 || typeof input.plan_id !== 'string') throw invalid('plan_id만 입력하세요.');
      if (!environmentAdapter) throw unavailable();
      const accepted = await store.transaction(async (state) => {
        const fingerprint = digest(input), existingId = state.keys[scopeKey('environments', key, sessionId)];
        if (existingId) {
          const record = state.operations[existingId];
          if (record.fingerprint !== fingerprint) throw new ProductError(409, 'IDEMPOTENCY_CONFLICT', '같은 키로 다른 계획을 실행할 수 없습니다.');
          return { record, replay: true };
        }
        const plan = Object.hasOwn(state.plans, input.plan_id) ? state.plans[input.plan_id] : null;
        if (!owns(plan, sessionId) || ['application-lifecycle', 'target-lifecycle'].includes(plan.kind)) throw new ProductError(404, 'NOT_FOUND', '계획을 찾을 수 없습니다.');
        if (plan.environment_id) throw new ProductError(409, 'CONFLICT', '이미 실행에 사용된 계획입니다.');
        checkFree(state, sessionId);
        checkUncertainResource(state, { target_id: plan.private?.profile?.target?.target_id });
        checkCapacity(state);
        await environmentAdapter.verifyPlan(plan);
        const id = randomUUID();
        const record = { id, kind: 'environments', session_id: sessionId, plan_id: input.plan_id, status: 'queued', stage: 'provision', error: null, fingerprint, created_at: new Date().toISOString() };
        state.operations[id] = record; state.keys[scopeKey('environments', key, sessionId)] = id; plan.environment_id = id;
        return { record, plan };
      });
      if (!accepted.replay) launch(async () => {
        try {
          await update(accepted.record.id, { status: 'running' });
          const patch = await environmentAdapter.execute(accepted.plan, { id: accepted.record.id, onProgress: (value) => update(accepted.record.id, value) });
          await update(accepted.record.id, patch);
        } catch (error) { await update(accepted.record.id, { status: error.outcomeUnknown === false ? [409, 503].includes(error.status) ? 'blocked' : 'failed' : 'unknown', error: operationError('ENVIRONMENT_FAILED', error.outcomeUnknown !== false) }); }
      });
      return publicRecord(accepted.record);
    },
    getEnvironment(id, sessionId = null) {
      return publicRecord(find('environments', id, sessionId));
    },
    async close() {
      abort.abort();
      clearInterval(queueTimer);
      await pumping;
      await Promise.allSettled(workers);
      await diagnostics.close();
      await Promise.allSettled(telemetryTasks.values());
      await store.close();
    },
  };
}
