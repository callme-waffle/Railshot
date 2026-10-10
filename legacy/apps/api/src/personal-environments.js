import { createHash, randomBytes, randomUUID } from 'node:crypto';
import { DashboardError } from './sessions.js';
import { isIP } from 'node:net';

const hash = (value) => createHash('sha256').update(typeof value === 'string' ? value : JSON.stringify(value)).digest('hex');
const now = () => new Date().toISOString();
const fail = (message, status = 422, code = 'INVALID_INPUT') => { throw new DashboardError(message, status, code); };
const absent = () => fail('개인 환경을 찾을 수 없습니다.', 404, 'NOT_FOUND');
const exact = (value, required, optional = []) => value && typeof value === 'object' && !Array.isArray(value)
  && required.every((key) => Object.hasOwn(value, key)) && Object.keys(value).every((key) => [...required, ...optional].includes(key));
const tokenHash = (authorization) => /^Bearer [A-Za-z0-9_-]{43}$/.test(authorization || '') ? hash(authorization.slice(7)) : null;
const shell = (value) => `'${String(value).replaceAll("'", "'\\''")}'`;
const uuid = (value) => typeof value === 'string' && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(value);
const visibleOperation = ({ session_id, fingerprint, ...value }) => structuredClone(value);
const metricNames = ['cpu_percent', 'memory_percent', 'disk_percent', 'network_receive_bytes_per_second', 'network_transmit_bytes_per_second'];
const held = (target) => ['deleting', 'attention', 'deleted'].includes(target.status);
const resources = (apps) => apps.map((app) => ({ kind: 'RailShotApplicationAndPrivateData', name: app.app, application_id: app.id }));
const retained = [{ kind: 'CustomerServices', name: 'RailShot 외부 서비스' },
  { kind: 'Infrastructure', name: '기반 VM·공유 네트워크·클러스터·공유 스토리지' },
  { kind: 'OpenStackIdentityMetadata', name: '공유 railshot 프로젝트·OpenStack 전용 계정·역할 기록' },
  { kind: 'AuditRecord', name: '작업 및 삭제 기록' }];

export function createPersonalEnvironments({ store, adapter, applicationAdapter, launch, publicApplication, maxTargets = 20 }) {
  const runtimeWorkers = new Set();
  function owned(state, id, owner) {
    const target = state.personal.targets[id];
    if (!owner || !target || target.session_id !== owner) absent();
    return target;
  }
  function apps(state, target) { return Object.values(state.applications).filter((app) => app.environment_target_id === target.id && app.status !== 'deleted'); }
  function publicTarget(target, state = store.read()) {
    const stale = !target.last_seen_at || Date.now() - Date.parse(target.last_seen_at) > 90000;
    const connection = stale && target.connection_status === 'ready' ? 'offline' : target.connection_status || 'connecting';
    const runtime = target.runtime_preparation || { status: 'not_started', stage: 'client', blockers: [], verified_at: null };
    const prepared = target.runtime_prepared === true && runtime.status === 'succeeded' && Date.now() - Date.parse(runtime.verified_at) <= 180000;
    const status = held(target) ? target.status : ['pending', 'installing'].includes(target.status) ? target.status
      : connection === 'offline' ? 'offline' : connection !== 'ready' ? 'connecting' : prepared ? 'ready' : 'preparing';
    return { id: target.id, label: target.label, provider: 'openstack', scope: 'owned', environment: 'onprem', status,
      connection_status: connection, runtime_preparation: { ...runtime, client_reported_ready: Boolean(target.runtime_evidence) },
      deployable: status === 'ready' && prepared, generation: target.generation,
      last_seen_at: target.last_seen_at || null, client_version: target.client_version || null, project_id: target.project_id || null,
      application_count: apps(state, target).length, registration_stage: target.registration_stage,
      checks: target.checks || null, blockers: target.blockers || [], created_at: target.created_at,
      deletion_operation_id: target.deletion_operation_id || null,
      capabilities: { openstack_control: !held(target) && connection === 'ready', ci_submission: status === 'ready' && prepared, application_deployment: status === 'ready' && prepared },
    };
  }
  function authenticated(state, id, authorization, generation) {
    const target = state.personal.targets[id];
    if (!target || !target.client_hash || target.client_hash !== tokenHash(authorization) || target.status === 'deleted'
        || generation !== undefined && generation !== target.generation) fail('클라이언트 인증에 실패했습니다.', 401, 'UNAUTHENTICATED');
    return target;
  }
  function writable(state, id, owner, requireReady = true) {
    const target = state.personal.targets[id];
    if (!target) return;
    owned(state, id, owner);
    if (held(target) || requireReady && !publicTarget(target, state).deployable) fail('현재 환경은 배포·앱 변경을 받을 수 없습니다.', 409, 'TARGET_UNAVAILABLE');
  }
  function snapshot(state, target) {
    return hash({ generation: target.generation, applications: apps(state, target), status: target.status,
      pending: Object.values(state.operations).filter((op) => ['queued', 'running', 'unknown'].includes(op.status) && (op.environment_target_id === target.id || op.target_id === target.id)) });
  }
  const runtimeView = (target) => {
    const visible = publicTarget(target);
    return { target_id: target.id, generation: target.generation, status: visible.status, connection_status: visible.connection_status,
      deployable: visible.deployable, runtime_preparation: visible.runtime_preparation };
  };
  function runtimeResult(result) {
    const allowed = ['succeeded', 'blocked', 'unknown'];
    if (!allowed.includes(result?.status)) throw new Error('Runtime result invalid');
    return { status: result.status, stage: result.status === 'succeeded' ? 'complete' : /^[a-z_]{1,48}$/.test(result.stage || '') ? result.stage : 'verification',
      cluster_verified: result.cluster_verified === true, resumable: result.resumable === true,
      blockers: (result.blockers || []).filter((code) => /^[A-Z][A-Z0-9_]{0,95}$/.test(code)),
      verified_at: result.status === 'succeeded' ? result.verified_at : null };
  }
  async function runRuntime(id, generation, fingerprint, reconcileOnly = false) {
    if (runtimeWorkers.has(id)) return;
    runtimeWorkers.add(id);
    try {
      const target = await store.transaction((state) => {
        const row = state.personal.targets[id];
        if (held(row) || row.generation !== generation || row.runtime_fingerprint !== fingerprint) return null;
        row.runtime_preparation = { status: 'running', stage: reconcileOnly ? 'reconciliation' : 'registration', blockers: [], verified_at: null };
        return structuredClone(row);
      });
      if (!target) return;
      let result = await (reconcileOnly ? adapter.reconcileRuntime(target, target.runtime_evidence) : adapter.prepareRuntime(target, target.runtime_evidence));
      if (result.status === 'succeeded') result = await adapter.verifyRuntime(target, target.runtime_evidence);
      if (result.status === 'succeeded' && adapter.deploymentReady?.(target) !== true) result = { status: 'blocked', blockers: ['RUNTIME_APPLICATION_BINDING_INCOMPLETE'] };
      const visible = runtimeResult(result);
      await store.transaction((state) => {
        const row = state.personal.targets[id];
        if (row.generation !== generation || row.runtime_fingerprint !== fingerprint || held(row)) return;
        row.runtime_preparation = visible;
        row.runtime_prepared = result.status === 'succeeded' && adapter.deploymentReady?.(row) === true;
        // Keep known ownership when a readback fails; removal still needs it.
        row.runtime_binding = result.binding_sha256 || row.runtime_binding || null;
        row.status = 'connecting';
      });
    } catch {
      await store.transaction((state) => {
        const row = state.personal.targets[id];
        if (held(row) || row.generation !== generation) return;
        row.runtime_prepared = false;
        row.runtime_preparation = { status: 'unknown', stage: 'reconciliation', blockers: ['RUNTIME_PREPARATION_UNVERIFIED'], verified_at: null };
      });
    } finally { runtimeWorkers.delete(id); }
  }
  async function reconcileRuntime(id, owner, authorization, generation) {
    const accepted = await store.transaction((state) => {
      const target = owner ? owned(state, id, owner) : authenticated(state, id, authorization, generation);
      if (held(target) || !target.runtime_evidence || !target.runtime_fingerprint || !adapter?.reconcileRuntime)
        fail('접수된 실행환경 증거가 있어야 준비 상태를 다시 확인할 수 있습니다.', 409, 'RUNTIME_EVIDENCE_REQUIRED');
      if (runtimeWorkers.has(id) || ['queued', 'running'].includes(target.runtime_preparation?.status)) return { replay: true, target: structuredClone(target) };
      target.runtime_prepared = false;
      target.runtime_preparation = { status: 'queued', stage: 'reconciliation', blockers: [], verified_at: null };
      return { target: structuredClone(target) };
    });
    if (!accepted.replay) launch(() => runRuntime(id, accepted.target.generation, accepted.target.runtime_fingerprint, true));
    return runtimeView(accepted.target);
  }
  function clientPhaseComplete(state, target, operation) {
    const plan = state.plans[operation.plan_id];
    return operation.kind === 'target-lifecycle' && operation.action === 'delete' && operation.delete_data === true
      && operation.session_id === target.session_id && target.deletion_operation_id === operation.id
      && plan?.kind === 'target-lifecycle' && plan.public.target_id === target.id && plan.session_id === target.session_id
      && Array.isArray(plan.children) && plan.children.every((child) => state.applications[child.application_id]?.status === 'deleted'
        && operation.steps.some((step) => step.name === `application:${child.application_id}` && step.status === 'succeeded'))
      && apps(state, target).length === 0
      && (!target.runtime_binding || operation.steps.some((step) => step.name === 'runtime:revoke' && step.status === 'succeeded'))
      && (operation.client_command_issued === true || target.command?.kind === 'environment.delete' && target.command.operation_id === operation.id);
  }
  function initialDeletion(state, target, operation) {
    const plan = state.plans[operation.plan_id];
    return !operation.client_command_issued && target.command?.kind !== 'environment.delete'
      && !operation.steps.some((step) => step.name === 'client') && operation.kind === 'target-lifecycle' && operation.action === 'delete'
      && operation.delete_data === true && operation.session_id === target.session_id && target.deletion_operation_id === operation.id
      && plan?.kind === 'target-lifecycle' && plan.session_id === target.session_id && plan.public.target_id === target.id
      && Array.isArray(plan.children) && apps(state, target).every((app) => plan.children.some((child) => child.application_id === app.id));
  }
  async function reconcileDeletionServices(id, operationId, checkId) {
    const state = store.read(), target = state.personal.targets[id], operation = state.operations[operationId], plan = state.plans[operation.plan_id];
    const children = [], completed = [], blockers = [];
    let runtimeRevoked = false, runtimeResume = false;
    try {
      if (plan.children.some((entry) => state.applications[entry.application_id]?.status !== 'deleted')) await adapter.restoreApplications?.(target);
      for (const entry of plan.children) {
        const app = state.applications[entry.application_id];
        if (app?.status === 'deleted' && operation.steps.some((step) => step.name === `application:${app.id}` && step.status === 'succeeded')) continue;
        if (!app || app.session_id !== target.session_id || !applicationAdapter?.reconcileLifecycle) throw new Error('Application reconciliation unavailable');
        const result = await applicationAdapter.reconcileLifecycle(app, entry.plan, { id: entry.operation_id });
        if (result.status === 'succeeded' && !(result.residuals || []).length) { completed.push(app.id); continue; }
        if (!result.resumable) throw new Error('Application outcome remains unknown');
        if (result.resume_mode === 'fresh') {
          children.push({ application_id: app.id, operation_id: randomUUID(), plan: await applicationAdapter.planLifecycle(app, { id: randomUUID(), action: 'delete' }) });
        } else if (result.resume_mode === 'existing') children.push({ ...entry, resume: true });
        else throw new Error('Application reconciliation mode invalid');
      }
      if (!children.length && target.runtime_binding && !operation.steps.some((step) => step.name === 'runtime:revoke' && step.status === 'succeeded')) {
        if (!adapter.inspectRuntimeRemoval) throw new Error('Runtime reconciliation unavailable');
        const result = await adapter.inspectRuntimeRemoval(target);
        runtimeRevoked = result.status === 'succeeded' && result.revocation_verified === true && !result.residuals?.length;
        runtimeResume = !runtimeRevoked && result.resumable === true;
        if (!runtimeRevoked && !runtimeResume) throw new Error('Runtime outcome remains unknown');
      }
    } catch { blockers.push('REMOVAL_RESIDUALS_UNVERIFIED'); }
    await store.transaction((current) => {
      const row = current.personal.targets[id], op = current.operations[operationId], check = op.reconciliation;
      if (row.status !== 'attention' || check?.id !== checkId || check.status !== 'pending') return;
      for (const appId of completed) {
        current.applications[appId].status = 'deleted';
        op.steps.push({ name: `application:${appId}`, status: 'succeeded' });
      }
      if (runtimeRevoked) {
        row.runtime_prepared = false; row.runtime_preparation = { status: 'revoked', stage: 'complete', blockers: [], verified_at: null };
        op.steps.push({ name: 'runtime:revoke', status: 'succeeded' });
      }
      Object.assign(check, { status: blockers.length ? 'blocked' : 'ready', resumable: blockers.length === 0,
        blockers, expires_at: new Date(Date.now() + 300000).toISOString() });
      current.plans[op.plan_id].recovery = { reconciliation_id: checkId, children, runtime_resume: runtimeResume };
      row.blockers = blockers; op.updated_at = now();
    });
  }
  function updateAttempt(operation, status, stage, errorCode) {
    const attempt = operation.attempts?.find((entry) => entry.id === operation.active_attempt_id);
    if (attempt) Object.assign(attempt, { status, stage, updated_at: now(), ...(errorCode ? { error_code: errorCode } : {}) });
  }
  function issueClientCommand(target, operation, reconciliationId) {
    const attempt = randomUUID();
    operation.attempts ||= [];
    operation.attempts.push({ id: attempt, status: 'running', stage: 'client', created_at: now(), updated_at: now() });
    Object.assign(operation, { active_attempt_id: attempt, client_command_issued: true, status: 'running', stage: 'client', error: null,
      residuals: [{ kind: 'RailShotClient', name: target.id }], updated_at: now() });
    target.status = 'deleting'; target.blockers = [];
    target.command = { id: operation.id, operation_id: operation.id, attempt_id: attempt, kind: 'environment.delete', applications: [],
      delete_data: true, generation: target.generation, ...(reconciliationId ? { reconciliation_id: reconciliationId } : {}) };
  }
  async function finishGateway(id, operationId, attemptId) {
    const current = store.read(), target = current.personal.targets[id], operation = current.operations[operationId];
    let removed = false;
    try { const result = await adapter.remove(target); removed = result.status === 'succeeded'; } catch { /* retain verified client completion */ }
    return store.transaction((state) => {
      const row = state.personal.targets[id], op = state.operations[operationId];
      if (op.active_attempt_id !== attemptId || op.stage !== 'gateway') return { target_id: id, operation_id: operationId, status: op.status };
      Object.assign(op, { status: removed ? 'succeeded' : 'unknown', stage: removed ? 'complete' : 'reconciliation', updated_at: now(),
        residuals: removed ? [] : [{ kind: 'GatewayRegistration', name: id }],
        error: removed ? null : { code: 'GATEWAY_REMOVAL_UNVERIFIED', message: '클라이언트 제거는 확인됐지만 서버 연결 등록 회수를 다시 확인해야 합니다.', retryable: false, outcome_unknown: true } });
      op.steps.push({ name: 'gateway', status: removed ? 'succeeded' : 'unknown' });
      updateAttempt(op, op.status, op.stage, removed ? undefined : 'GATEWAY_REMOVAL_UNVERIFIED');
      row.status = removed ? 'deleted' : 'attention';
      row.blockers = removed ? [] : ['GATEWAY_REMOVAL_UNVERIFIED'];
      delete row.command;
      if (removed) {
        row.receipt_client_hash = row.client_hash; row.receipt_expires_at = new Date(Date.now() + 900000).toISOString();
        delete row.client_hash;
      }
      return { target_id: id, operation_id: operationId, status: op.status };
    });
  }
  async function resumeDeletion(id, input, key, owner) {
    if (!exact(input, ['action', 'operation_id', 'reconciliation_id', 'confirmation', 'delete_data']) || input.delete_data !== true
        || !uuid(input.operation_id) || !uuid(input.reconciliation_id)) fail('재확인 결과와 명시적인 데이터 삭제 확인이 필요합니다.');
    const accepted = await store.transaction((state) => {
      const target = owned(state, id, owner), op = state.operations[input.operation_id];
      if (!op || op.session_id !== owner || op.target_id !== id || op.kind !== 'target-lifecycle') absent();
      const requestKey = hash(key), fingerprint = hash(input), prior = target.resume_requests?.[requestKey];
      if (prior) {
        if (prior.fingerprint !== fingerprint) fail('같은 키로 다른 재개를 요청할 수 없습니다.', 409, 'IDEMPOTENCY_CONFLICT');
        return { record: structuredClone(op), replay: true };
      }
      const check = op.reconciliation;
      const initial = check?.phase === 'services' && initialDeletion(state, target, op);
      if (target.status !== 'attention' || !['unknown', 'blocked'].includes(op.status) || !(clientPhaseComplete(state, target, op) || initial)
          || check?.id !== input.reconciliation_id || !check.resumable || check.status !== 'ready'
          || Date.parse(check.expires_at) <= Date.now() || input.confirmation !== target.label)
        fail('최신 삭제 재확인 결과를 먼저 확인하세요.', 409, 'REMOVAL_RECONCILIATION_REQUIRED');
      if (Object.values(state.operations).some((other) => other.id !== op.id && ['queued', 'running'].includes(other.status)))
        fail('다른 실행을 먼저 확인하세요.', 409, 'EXECUTOR_BUSY');
      target.resume_requests ||= {}; target.resume_requests[requestKey] = { fingerprint, operation_id: op.id };
      if (!op.attempts?.length) op.attempts = [{ id: 'legacy', status: op.status, stage: op.stage, error_code: op.error?.code || 'REMOVAL_UNVERIFIED', updated_at: op.updated_at }];
      check.resumable = false; check.status = 'used';
      if (initial) {
        // The refreshed child plans retain exact application identities and the
        // original data-deletion consent. Completed children are never repeated.
        const plan = state.plans[op.plan_id];
        if (plan.recovery?.reconciliation_id !== check.id) fail('삭제 재개 근거가 일치하지 않습니다.', 409, 'REMOVAL_RECONCILIATION_REQUIRED');
        plan.children = plan.children.map((entry) => plan.recovery.children.find((child) => child.application_id === entry.application_id) || entry);
        op.runtime_resume = plan.recovery.runtime_resume === true;
        Object.assign(op, { status: 'running', stage: 'services', error: null, updated_at: now() }); target.status = 'deleting';
        return { record: structuredClone(op), initial: true, plan: structuredClone(plan) };
      } else if (op.client_removed_verified) {
        op.active_attempt_id = randomUUID();
        op.attempts.push({ id: op.active_attempt_id, status: 'running', stage: 'gateway', created_at: now(), updated_at: now() });
        Object.assign(op, { status: 'running', stage: 'gateway', error: null, updated_at: now() }); target.status = 'deleting';
      } else issueClientCommand(target, op, check.id);
      return { record: structuredClone(op), gateway: op.client_removed_verified === true };
    });
    if (!accepted.replay && accepted.gateway) launch(() => finishGateway(id, accepted.record.id, accepted.record.active_attempt_id));
    if (!accepted.replay && accepted.initial) launch(() => runDeletion(accepted.record, accepted.plan));
    return visibleOperation(accepted.record);
  }
  async function runDeletion(record, plan) {
    try {
      await store.transaction((state) => { Object.assign(state.operations[record.id], { status: 'running', stage: 'services', updated_at: now() }); });
      for (const entry of plan.children) {
        const app = store.read().applications[entry.application_id];
        if (app.status === 'deleted' && store.read().operations[record.id].steps.some((step) => step.name === `application:${app.id}` && step.status === 'succeeded')) continue;
        if (!entry.resume) await applicationAdapter.verifyLifecyclePlan(app, entry.plan);
        const result = await (entry.resume ? applicationAdapter.resumeLifecycle(app, entry.plan, { id: entry.operation_id, deleteData: true })
          : applicationAdapter.applyLifecycle(app, entry.plan, { id: entry.operation_id, deleteData: true }));
        if (result.status !== 'succeeded' || (result.residuals || []).length) throw new Error('Application removal unverified');
        await store.transaction((state) => {
          state.applications[app.id].status = 'deleted';
          state.operations[record.id].steps.push({ name: `application:${app.id}`, status: 'succeeded' });
          state.operations[record.id].updated_at = now();
        });
      }
      const target = store.read().personal.targets[record.target_id];
      if (target.runtime_binding && !store.read().operations[record.id].steps.some((step) => step.name === 'runtime:revoke' && step.status === 'succeeded')) {
        await store.transaction((state) => { state.operations[record.id].stage = 'runtime'; });
        const revoked = await (record.runtime_resume ? adapter.resumeRuntimeRemoval(target) : adapter.removeRuntime(target));
        if (revoked.status !== 'succeeded' || revoked.revocation_verified !== true || revoked.residuals.length) throw new Error('Runtime revocation unverified');
        await store.transaction((state) => {
          state.personal.targets[target.id].runtime_prepared = false;
          state.personal.targets[target.id].runtime_preparation = { status: 'revoked', stage: 'complete', blockers: [], verified_at: null };
          state.operations[record.id].steps.push({ name: 'runtime:revoke', status: 'succeeded' });
        });
      }
      if (!target.client_hash) {
        // An unclaimed registration has no installed client and no gateway peer.
        if (target.public_key) throw new Error('Enrollment outcome requires reconciliation');
        await store.transaction((state) => {
          state.personal.targets[target.id].status = 'deleted';
          Object.assign(state.operations[record.id], { status: 'succeeded', stage: 'complete', residuals: [], updated_at: now() });
        });
      } else {
        await store.transaction((state) => {
          issueClientCommand(state.personal.targets[target.id], state.operations[record.id]);
        });
      }
    } catch {
      await store.transaction((state) => {
        state.personal.targets[record.target_id].status = 'attention';
        state.operations[record.id].failed_stage = state.operations[record.id].stage;
        Object.assign(state.operations[record.id], { status: 'unknown', stage: 'reconciliation', updated_at: now(),
          error: { code: 'REMOVAL_UNVERIFIED', message: '남은 서비스·클라이언트를 확인해야 합니다. 자동 재실행하지 않습니다.', retryable: false, outcome_unknown: true } });
      });
    }
  }
  return {
    writable,
    async readiness() {
      if (!adapter?.readiness) return { scope: 'personal', ready: false,
        blockers: [{ code: 'PERSONAL_GATEWAY_NOT_CONFIGURED', message: '개인 환경 등록 서버가 설정되지 않았습니다.' }] };
      return adapter.readiness();
    },
    async restore() {
      for (const target of Object.values(store.read().personal.targets)) {
        if (!target.client_hash || ['deleted', 'attention', 'deleting'].includes(target.status) || !adapter) continue;
        try {
          await adapter.register(target);
          await adapter.prepare(target);
          await store.transaction((state) => {
            const row = state.personal.targets[target.id];
            row.runtime_prepared = false; row.connection_status = 'connecting';
            if (['queued', 'running'].includes(row.runtime_preparation?.status)) row.runtime_preparation = {
              status: 'unknown', stage: 'reconciliation', blockers: ['RUNTIME_PREPARATION_INTERRUPTED'], verified_at: null };
          });
        } catch { await store.transaction((state) => { Object.assign(state.personal.targets[target.id], { status: 'attention', blockers: ['RUNTIME_RECONCILIATION_REQUIRED'] }); }); }
      }
    },
    list(owner) { const state = store.read(); return Object.values(state.personal.targets).filter((t) => owner && t.session_id === owner && t.status !== 'deleted').map((t) => publicTarget(t, state)); },
    get(id, owner) { const state = store.read(); return publicTarget(owned(state, id, owner), state); },
    async execute(id, input, owner) {
      const target = owned(store.read(), id, owner);
      if (!input || !/^[A-Za-z0-9_-]{1,64}$/.test(input.job_id || '')) fail('OpenStack 실행에는 지속되는 job_id가 필요합니다.');
      if (!publicTarget(target).capabilities.openstack_control || !adapter?.execute) fail('OpenStack 제어 연결이 준비되지 않았습니다.', 409, 'TARGET_UNAVAILABLE');
      return adapter.execute(target, input);
    },
    async instances(id, owner) {
      const target = owned(store.read(), id, owner);
      if (!publicTarget(target).capabilities.openstack_control || !adapter?.execute) fail('OpenStack 제어 연결이 준비되지 않았습니다.', 409, 'TARGET_UNAVAILABLE');
      const result = await adapter.execute(target, { argv: ['server', 'list'] });
      if (!result.ok || !Array.isArray(result.result)) fail('OpenStack 조회를 확인하지 못했습니다.', 502, 'UPSTREAM_FAILURE');
      return result.result;
    },
    applications(id, owner) { const state = store.read(), target = owned(state, id, owner); return apps(state, target).filter((app) => app.session_id === owner).map((app) => publicApplication(state, app)); },
    observation(id, owner, observation) {
      const state = store.read(), target = owned(state, id, owner);
      const stale = !target.metrics_seen_at || Date.now() - Date.parse(target.metrics_seen_at) > 90000;
      for (const name of metricNames) observation.metrics[name] = {
        state: target.metrics?.[name] == null ? 'not_configured' : stale ? 'stale' : 'ready',
        value: target.metrics?.[name] ?? null, observed_at: target.metrics?.[name] == null ? null : target.metrics_seen_at,
        scope: 'client_host',
      };
      return { ...observation, measured_scope: 'client_host', measured_target: id };
    },
    async create(input, owner) {
      if (!owner) fail('복구키를 발급받아 개인 환경 소유권을 먼저 설정하세요.', 409, 'OWNER_REQUIRED');
      if (!exact(input, ['label'], ['provider']) || input.provider !== undefined && input.provider !== 'openstack'
          || typeof input.label !== 'string' || !input.label.trim() || input.label.length > 80 || /[\x00-\x1f]/.test(input.label)) fail('환경 이름을 확인하세요.');
      return store.transaction((state) => {
        if (Object.values(state.personal.targets).filter((t) => t.session_id === owner && t.status !== 'deleted').length >= maxTargets) fail('개인 환경 등록 한도를 초과했습니다.', 409, 'CAPACITY_EXCEEDED');
        const id = 'personal-' + randomUUID();
        const target = { id, session_id: owner, label: input.label.trim(), status: 'pending', generation: 0,
          registration_stage: 'enrollment', blockers: adapter ? [] : ['PERSONAL_GATEWAY_NOT_CONFIGURED'], created_at: now() };
        state.personal.targets[id] = target;
        return publicTarget(target, state);
      });
    },
    async enrollment(id, input, owner) {
      if (!exact(input, [])) fail('등록 자격 요청에는 추가 필드를 넣을 수 없습니다.');
      const prerequisites = adapter?.readiness ? await adapter.readiness() : { ready: false, blockers: [{ code: 'PERSONAL_GATEWAY_NOT_CONFIGURED' }] };
      if (!prerequisites.ready) fail(prerequisites.blockers[0]?.message || '개인 환경 설치 선행 조건을 확인하세요.', 409,
        prerequisites.blockers[0]?.code || 'CAPABILITY_UNAVAILABLE');
      return store.transaction((state) => {
        const target = owned(state, id, owner);
        if (target.client_hash || held(target) || target.public_key) fail('이미 등록했거나 확인이 필요한 환경입니다.', 409, 'TARGET_UNAVAILABLE');
        if (!adapter) fail('개인 환경 설치 서버가 구성되지 않았습니다.', 409, 'CAPABILITY_UNAVAILABLE');
        for (const item of Object.values(state.personal.enrollments)) if (item.target_id === id) item.revoked = true;
        if (Object.keys(state.personal.enrollments).length >= 10000) fail('등록 자격 보관 한도를 초과했습니다.', 409, 'CAPACITY_EXCEEDED');
        const enrollmentId = randomUUID(), token = randomBytes(32).toString('base64url'), expires_at = new Date(Date.now() + 900000).toISOString();
        target.generation += 1;
        state.personal.enrollments[enrollmentId] = { id: enrollmentId, target_id: id, token_hash: hash(token), generation: target.generation, expires_at, used: false };
        const config = adapter.config;
        const testHttp = config.test_allow_http === true;
        const command = `install_file=$(mktemp) && curl --disable --proto '${testHttp ? '=http,https' : '=https'}' --max-redirs 0 --fail --silent --show-error ${shell(config.installer_url)} -o "$install_file" && RAILSHOT_ENROLLMENT_TOKEN=${shell(token)} sudo --preserve-env=RAILSHOT_ENROLLMENT_TOKEN bash "$install_file" --personal-registration --api-url ${shell(config.public_url)} --enrollment-id ${shell(enrollmentId)} --artifact-url ${shell(config.artifact_url)} --artifact-sha256 ${shell(config.artifact_sha256)}${testHttp ? ' --test-allow-http' : ''}; install_status=$?; rm -f "$install_file"; (exit "$install_status")`;
        return { id: enrollmentId, target_id: id, expires_at, install_command: command };
      });
    },
    async claim(id, input, authorization) {
      if (!exact(input, ['public_key', 'client_version', 'project_id', 'runtime'], ['capabilities']) || !/^[A-Za-z0-9+/]{43}=$/.test(input.public_key || '')
          || typeof input.client_version !== 'string' || !/^[A-Za-z0-9._-]{1,64}$/.test(input.client_version)
          || typeof input.project_id !== 'string' || !/^[A-Za-z0-9._-]{1,128}$/.test(input.project_id)
          || input.runtime !== undefined && (!exact(input.runtime, ['ssh_host_key'], ['profile_id', 'resource_id'])
            || input.runtime.ssh_host_key !== undefined && !/^ssh-ed25519 [A-Za-z0-9+/]{68}={0,2}$/.test(input.runtime.ssh_host_key)
            || input.runtime.profile_id !== undefined && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(input.runtime.profile_id)
            || input.runtime.resource_id !== undefined && !/^[A-Za-z0-9._-]{1,128}$/.test(input.runtime.resource_id))) fail('클라이언트 등록 입력을 확인하세요.');
      const target = await store.transaction((state) => {
        const enrollment = state.personal.enrollments[id];
        if (!enrollment || enrollment.token_hash !== tokenHash(authorization) || enrollment.used || enrollment.revoked || Date.parse(enrollment.expires_at) <= Date.now()) fail('등록 자격이 만료되었거나 유효하지 않습니다.', 401, 'UNAUTHENTICATED');
        const target = state.personal.targets[enrollment.target_id];
        if (!target || held(target) || target.generation !== enrollment.generation || target.client_hash) fail('환경 등록 상태가 변경되었습니다.', 409, 'TARGET_UNAVAILABLE');
        enrollment.used = true;
        Object.assign(target, { status: 'installing', public_key: input.public_key, project_id: input.project_id, client_version: input.client_version, registration_stage: 'gateway', runtime: input.runtime || null });
        return structuredClone(target);
      });
      try {
        const registered = await adapter.register(target);
        if (registered.status !== 'succeeded' || !registered.tunnel) throw new Error('Gateway unverified');
        target.tunnel = registered.tunnel;
        const runtimePrepared = await adapter.prepare(target), token = randomBytes(32).toString('base64url');
        await store.transaction((state) => {
          Object.assign(state.personal.targets[target.id], { client_hash: hash(token), tunnel: registered.tunnel, status: 'connecting', runtime_prepared: runtimePrepared,
            registration_stage: 'openstack', blockers: ['OPENSTACK_CONTROL_PENDING'] });
        });
        return { target_id: target.id, generation: target.generation, client_token: token, tunnel: registered.tunnel,
          heartbeat_url: `/api/v1/targets/${target.id}/heartbeats`, ...(adapter.runtimeAccess?.(target) ? { runtime_access: adapter.runtimeAccess(target) } : {}) };
      } catch {
        await store.transaction((state) => { Object.assign(state.personal.targets[target.id], { status: 'attention', blockers: ['ENROLLMENT_RECONCILIATION_REQUIRED'] }); });
        fail('클라이언트 등록 결과를 확인해야 합니다. 등록 자격은 재사용할 수 없습니다.', 503, 'ENROLLMENT_UNVERIFIED');
      }
    },
    async heartbeat(id, input, authorization) {
      if (!exact(input, ['generation', 'client_version', 'checks'], ['capabilities', 'project_id', 'metrics'])
          || !Number.isSafeInteger(input.generation) || !/^[A-Za-z0-9._-]{1,64}$/.test(input.client_version || '')
          || !exact(input.checks, ['tunnel', 'openstack'], ['runtime']) || Object.values(input.checks).some((v) => typeof v !== 'boolean')
          || input.capabilities !== undefined && (!Array.isArray(input.capabilities) || input.capabilities.length > 20 || input.capabilities.some((v) => typeof v !== 'string' || !/^[a-z.]{1,64}$/.test(v)))) fail('상태 보고 입력을 확인하세요.');
      if (input.metrics !== undefined && (!exact(input.metrics, [], metricNames) || Object.entries(input.metrics).some(([name, value]) => value !== null && (!Number.isFinite(value) || value < 0 || name.endsWith('_percent') && value > 100)))) fail('메트릭 값 범위를 확인하세요.');
      const current = authenticated(store.read(), id, authorization, input.generation);
      let verified = false;
      let runtimeVerification;
      if (!held(current) && input.checks.tunnel && adapter) {
        try { const result = await adapter.verify(current); verified = result.status === 'succeeded' && result.reachable === true && result.openstack_verified === true; } catch { /* never infer connectivity from the client alone */ }
      }
      if (verified && !held(current) && current.runtime_evidence && current.runtime_binding
          && ['succeeded', 'blocked'].includes(current.runtime_preparation?.status) && adapter?.verifyRuntime) {
        try { runtimeVerification = await adapter.verifyRuntime(current, current.runtime_evidence); }
        catch { runtimeVerification = { status: 'blocked', blockers: ['RUNTIME_READBACK_FAILED'] }; }
      }
      return store.transaction((state) => {
        const target = authenticated(state, id, authorization, input.generation);
        if (input.project_id !== undefined && input.project_id !== target.project_id) fail('프로젝트가 등록과 다릅니다.', 409, 'PROJECT_MISMATCH');
        Object.assign(target, { last_seen_at: now(), client_version: input.client_version, checks: input.checks, ...(input.metrics !== undefined ? { metrics: input.metrics, metrics_seen_at: now() } : {}) });
        if (!held(target)) {
          target.connection_status = verified && input.checks.openstack ? 'ready' : 'connecting';
          if (runtimeVerification) {
            target.runtime_prepared = runtimeVerification.status === 'succeeded' && adapter.deploymentReady?.(target) === true;
            target.runtime_preparation = runtimeResult(runtimeVerification);
          }
          target.status = target.connection_status === 'ready' && target.runtime_prepared ? 'ready' : 'connecting';
          target.registration_stage = target.status === 'ready' ? 'complete' : target.connection_status === 'ready' ? 'runtime' : 'checks';
          target.blockers = target.connection_status === 'ready' ? target.runtime_preparation?.blockers || [] : ['OPENSTACK_CONTROL_PENDING'];
        }
        const operation = target.command && state.operations[target.command.operation_id || target.command.id];
        return { ...runtimeView(target),
          command: target.command?.kind === 'environment.inspect'
            ? operation?.reconciliation?.status === 'pending' && Date.parse(operation.reconciliation.expires_at) > Date.now() ? target.command : null
            : operation?.status === 'running' && operation.stage === 'client' ? target.command : null };
      });
    },
    runtimeStatus(id, authorization) { return runtimeView(authenticated(store.read(), id, authorization)); },
    async prepareRuntime(id, input, authorization) {
      if (exact(input, ['generation', 'action']) && Number.isSafeInteger(input.generation) && input.action === 'reconcile')
        return reconcileRuntime(id, null, authorization, input.generation);
      if (exact(input, ['generation', 'progress']) && Number.isSafeInteger(input.generation)) {
        const progress = input.progress;
        if (!exact(progress, ['stage', 'status'], ['blockers']) || !['client_selection', 'client_installation', 'client_verification'].includes(progress.stage)
            || !['running', 'blocked'].includes(progress.status) || progress.blockers !== undefined && (!Array.isArray(progress.blockers)
              || progress.blockers.length > 10 || progress.blockers.some((code) => !/^[A-Z][A-Z0-9_]{0,95}$/.test(code)))) fail('고객 실행환경 진행 상태를 확인하세요.');
        return store.transaction((state) => {
          const target = authenticated(state, id, authorization, input.generation);
          if (held(target) || target.runtime_fingerprint) fail('서버 실행환경 준비가 이미 접수되었거나 확인 중입니다.', 409, 'TARGET_UNAVAILABLE');
          target.runtime_prepared = false;
          target.runtime_preparation = { status: progress.status, stage: progress.stage, blockers: progress.blockers || [], verified_at: null };
          return runtimeView(target);
        });
      }
      const fields = ['resource_id', 'private_ipv4', 'management_network', 'placement', 'architecture', 'initialization', 'ssh_user', 'ssh_port', 'ssh_host_key'];
      if (!exact(input, ['generation', 'evidence'], ['action']) || input.action !== undefined && input.action !== 'resume'
          || !Number.isSafeInteger(input.generation) || !exact(input.evidence, fields)
          || !['resource_id', 'management_network', 'placement'].every((key) => /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(input.evidence[key] || ''))
          || isIP(input.evidence.private_ipv4) !== 4 || input.evidence.architecture !== 'amd64'
          || !['cloud-init', 'preconfigured'].includes(input.evidence.initialization) || input.evidence.ssh_user !== 'railshot-runtime' || input.evidence.ssh_port !== 2223
          || !/^ssh-ed25519 [A-Za-z0-9+/]{68}={0,2}$/.test(input.evidence.ssh_host_key || '')) fail('실행환경 준비 증거를 확인하세요.');
      const fingerprint = hash({ generation: input.generation, evidence: input.evidence });
      const accepted = await store.transaction((state) => {
        const target = authenticated(state, id, authorization, input.generation);
        if (held(target)) fail('삭제 또는 확인 중인 환경입니다.', 409, 'TARGET_UNAVAILABLE');
        if (!publicTarget(target).capabilities.openstack_control) fail('OpenStack 제어 연결을 먼저 확인하세요.', 409, 'TARGET_UNAVAILABLE');
        if (target.runtime_fingerprint) {
          if (target.runtime_fingerprint !== fingerprint) fail('이미 준비 요청에 연결된 실행환경입니다.', 409, 'RUNTIME_BINDING_CONFLICT');
          if (input.action === 'resume' && target.runtime_preparation?.resumable !== true) fail('읽기 전용 재확인으로 재개 가능 여부를 먼저 확인하세요.', 409, 'RUNTIME_RECONCILIATION_REQUIRED');
          if (target.runtime_preparation?.status !== 'blocked') return { replay: true, target: structuredClone(target) };
        }
        target.runtime_fingerprint = fingerprint; target.runtime_evidence = input.evidence; target.runtime_prepared = false;
        target.runtime_preparation = { status: 'queued', stage: 'registration', blockers: [], verified_at: null };
        return { replay: false, target: structuredClone(target) };
      });
      if (!accepted.replay) launch(() => runRuntime(id, input.generation, fingerprint));
      return runtimeView(accepted.target);
    },
    async plan(id, input, owner) {
      if (!exact(input, ['action', 'delete_data']) || input.action !== 'delete' || input.delete_data !== true) fail('서비스와 앱 전용 데이터 삭제에 명시적으로 동의해야 합니다.');
      return store.transaction(async (state) => {
        const target = owned(state, id, owner), applications = apps(state, target);
        if (['deleting', 'deleted'].includes(target.status)) fail('삭제가 접수되었거나 끝난 환경입니다.', 409, 'TARGET_UNAVAILABLE');
        const blockers = [], children = [];
        if (target.status === 'attention') blockers.push('RECONCILIATION_REQUIRED');
        if (applications.some((app) => app.session_id !== owner)) blockers.push('FOREIGN_APPLICATIONS');
        if (Object.values(state.operations).some((op) => ['queued', 'running', 'unknown'].includes(op.status))) blockers.push('EXECUTION_IN_PROGRESS');
        if (target.client_hash && publicTarget(target, state).connection_status !== 'ready') blockers.push('CLIENT_OFFLINE');
        if (['queued', 'running'].includes(target.runtime_preparation?.status)) blockers.push('RUNTIME_PREPARATION_IN_PROGRESS');
        if (target.runtime_preparation?.status === 'unknown') blockers.push('RUNTIME_RECONCILIATION_REQUIRED');
        if (!blockers.length) for (const app of applications) {
          if (!['ready', 'stopped'].includes(app.status) || !applicationAdapter?.planLifecycle) { blockers.push('APPLICATION_RECONCILIATION_REQUIRED'); break; }
          const child = await applicationAdapter.planLifecycle(app, { id: randomUUID(), action: 'delete' });
          children.push({ application_id: app.id, operation_id: randomUUID(), plan: child });
        }
        if (Object.keys(state.plans).length >= 1000) fail('삭제 계획 보관 한도를 초과했습니다.', 409, 'CAPACITY_EXCEEDED');
        const planId = randomUUID(), expires_at = new Date(Date.now() + 600000).toISOString(), targetSnapshot = snapshot(state, target);
        const plan = { id: planId, target_id: id, action: 'delete', delete_data: true, expires_at,
          resources: [...resources(applications), { kind: 'RailShotClient', name: target.label }, { kind: 'Registration', name: id }], retained, blockers,
          executable: blockers.length === 0, plan_hash: hash({ targetSnapshot, children, expires_at }) };
        state.plans[planId] = { kind: 'target-lifecycle', session_id: owner, public: plan, children, target_snapshot: targetSnapshot };
        return structuredClone(plan);
      });
    },
    async reconcile(id, input, owner) {
      if (exact(input, ['scope']) && input.scope === 'runtime') return reconcileRuntime(id, owner);
      if (!exact(input, ['operation_id']) || !uuid(input.operation_id)) fail('기존 삭제 작업을 지정하세요.');
      const accepted = await store.transaction((state) => {
        const target = owned(state, id, owner), operation = state.operations[input.operation_id];
        if (!operation || operation.session_id !== owner || operation.target_id !== id || operation.kind !== 'target-lifecycle') absent();
        const initial = initialDeletion(state, target, operation);
        if (target.status !== 'attention' || !['unknown', 'blocked'].includes(operation.status) || !(clientPhaseComplete(state, target, operation) || initial))
          fail('서비스 및 실행환경 권한 제거부터 확인해야 합니다. 자동 재실행할 수 없습니다.', 409, 'REMOVAL_RECONCILIATION_REQUIRED');
        if (operation.reconciliation?.status === 'pending' && Date.parse(operation.reconciliation.expires_at) > Date.now()) return { operation: visibleOperation(operation) };
        const check = { id: randomUUID(), status: operation.client_removed_verified ? 'ready' : 'pending', resumable: operation.client_removed_verified === true,
          expires_at: new Date(Date.now() + 300000).toISOString(), blockers: [] };
        updateAttempt(operation, operation.status, operation.stage, operation.error?.code);
        if (initial) {
          check.phase = 'services'; operation.reconciliation = check; operation.updated_at = now();
          return { operation: visibleOperation(operation), initial: true, checkId: check.id };
        }
        operation.client_command_issued = true;
        operation.reconciliation = check; operation.updated_at = now();
        if (!operation.client_removed_verified) target.command = { id: check.id, reconciliation_id: check.id, operation_id: operation.id,
          kind: 'environment.inspect', generation: target.generation, ...(operation.active_attempt_id ? { previous_attempt_id: operation.active_attempt_id } : {}) };
        return { operation: visibleOperation(operation) };
      });
      if (accepted.initial) launch(() => reconcileDeletionServices(id, input.operation_id, accepted.checkId));
      return accepted.operation;
    },
    async remove(id, input, key, owner) {
      if (input?.action === 'resume') return resumeDeletion(id, input, key, owner);
      if (!exact(input, ['action', 'plan_id', 'plan_hash', 'confirmation', 'delete_data']) || input.action !== 'delete' || input.delete_data !== true) fail('삭제 계획과 명시적인 데이터 삭제 확인이 필요합니다.');
      const accepted = await store.transaction(async (state) => {
        const target = owned(state, id, owner), fingerprint = hash({ target_id: id, ...input }), scopedKey = `${owner}:target-lifecycle:${key}`;
        const prior = state.keys[scopedKey] && state.operations[state.keys[scopedKey]];
        if (prior) { if (prior.fingerprint !== fingerprint) fail('같은 키로 다른 삭제를 요청할 수 없습니다.', 409, 'IDEMPOTENCY_CONFLICT'); return { record: prior, replay: true }; }
        const plan = state.plans[input.plan_id];
        if (!plan || plan.kind !== 'target-lifecycle' || plan.session_id !== owner || plan.public.target_id !== id) absent();
        if (plan.operation_id || input.confirmation !== target.label || input.plan_hash !== plan.public.plan_hash || Date.parse(plan.public.expires_at) <= Date.now()
            || plan.target_snapshot !== snapshot(state, target) || plan.public.blockers.length) fail('환경 삭제 계획을 다시 확인하세요.', 409, 'TARGET_PLAN_STALE');
        if (Object.values(state.operations).some((op) => ['queued', 'running', 'unknown'].includes(op.status))) fail('진행 중인 실행을 먼저 확인하세요.', 409, 'EXECUTOR_BUSY');
        for (const child of plan.children) await applicationAdapter.verifyLifecyclePlan(state.applications[child.application_id], child.plan);
        const operationId = randomUUID();
        const record = { id: operationId, kind: 'target-lifecycle', session_id: owner, target_id: id, action: 'delete', delete_data: true,
          plan_id: input.plan_id, status: 'queued', stage: 'services', steps: [], residuals: plan.public.resources, retained,
          fingerprint, created_at: now(), updated_at: now() };
        state.operations[operationId] = record; state.keys[scopedKey] = operationId; plan.operation_id = operationId;
        Object.assign(target, { status: 'deleting', deletion_operation_id: operationId });
        for (const enrollment of Object.values(state.personal.enrollments)) if (enrollment.target_id === id) enrollment.revoked = true;
        return { record: structuredClone(record), plan };
      });
      if (!accepted.replay) launch(() => runDeletion(accepted.record, accepted.plan));
      const { session_id, fingerprint, ...visible } = accepted.record; return visible;
    },
    async receipt(id, input, authorization) {
      if (!exact(input, ['operation_id', 'generation', 'status', 'steps', 'residuals', 'client_removed'],
          ['attempt_id', 'reconciliation_id', 'inspection', 'error_code', 'mutation_started'])
          || !['running', 'succeeded', 'failed', 'unknown', 'blocked', 'inspected'].includes(input.status)
          || !uuid(input.operation_id) || input.attempt_id !== undefined && !uuid(input.attempt_id)
          || !Array.isArray(input.steps) || input.steps.length > 30 || !Array.isArray(input.residuals) || input.residuals.length > 100
          || typeof input.client_removed !== 'boolean' || input.error_code !== undefined && !/^[A-Z][A-Z0-9_]{0,95}$/.test(input.error_code)
          || input.mutation_started !== undefined && typeof input.mutation_started !== 'boolean') fail('제거 확인 형식을 확인하세요.');
      const fingerprint = hash(input);
      const accepted = await store.transaction((state) => {
        const old = state.personal.targets[id];
        if (old?.status === 'deleted' && old.receipt_client_hash === tokenHash(authorization) && old.generation === input.generation
            && Date.parse(old.receipt_expires_at) > Date.now() && old.final_receipt_hash === fingerprint)
          return { response: { target_id: id, operation_id: input.operation_id, status: 'succeeded' } };
        const target = authenticated(state, id, authorization, input.generation), operation = state.operations[input.operation_id];
        if (!operation || operation.target_id !== id || operation.kind !== 'target-lifecycle' || target.deletion_operation_id !== input.operation_id) absent();
        if (input.status === 'inspected') {
          const check = operation.reconciliation, proof = input.inspection;
          if (!uuid(input.reconciliation_id) || check?.id !== input.reconciliation_id || Date.parse(check.expires_at) <= Date.now()
              || !['pending', 'ready', 'blocked'].includes(check.status) || !exact(proof, ['state', 'preflight_ok', 'helper_active'])
              || !['intact', 'partial', 'unknown'].includes(proof.state) || typeof proof.preflight_ok !== 'boolean' || typeof proof.helper_active !== 'boolean'
              || input.client_removed || input.attempt_id !== undefined) fail('재확인 응답이 현재 요청과 다릅니다.', 409, 'REMOVAL_RECEIPT_STALE');
          if (target.inspection_receipts?.[check.id]) {
            if (target.inspection_receipts[check.id] !== fingerprint) fail('재확인 응답이 변경됐습니다.', 409, 'REMOVAL_RECEIPT_STALE');
            return { response: { target_id: id, operation_id: operation.id, status: operation.status } };
          }
          const intact = proof.state === 'intact' && proof.preflight_ok && !proof.helper_active && clientPhaseComplete(state, target, operation);
          Object.assign(check, { status: intact ? 'ready' : 'blocked', resumable: intact,
            blockers: intact ? [] : [proof.helper_active ? 'REMOVAL_STILL_RUNNING' : 'CLIENT_INSTALLATION_NOT_INTACT'] });
          target.inspection_receipts ||= {}; target.inspection_receipts[check.id] = fingerprint;
          operation.updated_at = now(); target.blockers = check.blockers; delete target.command;
          return { response: { target_id: id, operation_id: operation.id, status: operation.status } };
        }
        if (input.inspection !== undefined || input.reconciliation_id !== undefined
            || (operation.active_attempt_id || undefined) !== input.attempt_id) fail('삭제 시도와 응답이 다릅니다.', 409, 'REMOVAL_RECEIPT_STALE');
        if (target.last_removal_receipt_hash === fingerprint || target.final_receipt_hash === fingerprint && operation.client_removed_verified)
          return { response: { target_id: id, operation_id: operation.id, status: operation.status } };
        const lateSuccess = operation.stage === 'reconciliation' && ['unknown', 'blocked'].includes(operation.status)
          && input.status === 'succeeded' && input.client_removed && input.residuals.length === 0;
        if ((!lateSuccess && operation.stage !== 'client') || !['running', 'unknown', 'blocked'].includes(operation.status) || !clientPhaseComplete(state, target, operation))
          fail('현재 삭제 단계의 응답이 아닙니다.', 409, 'REMOVAL_RECEIPT_STALE');
        if (input.status === 'running') {
          operation.updated_at = now();
          return { response: { target_id: id, operation_id: operation.id, status: operation.status } };
        }
        const succeeded = input.status === 'succeeded' && input.client_removed && input.residuals.length === 0;
        operation.steps.push({ name: 'client', status: succeeded ? 'succeeded' : input.status === 'blocked' && input.mutation_started === false ? 'blocked' : 'unknown' });
        if (succeeded) {
          target.final_receipt_hash = fingerprint; operation.client_removed_verified = true;
          if (operation.reconciliation) { operation.reconciliation.resumable = false; operation.reconciliation.status = 'used'; }
          Object.assign(operation, { status: 'running', stage: 'gateway', updated_at: now() }); delete target.command;
          updateAttempt(operation, 'running', 'gateway');
          return { gateway: true, attempt: operation.active_attempt_id };
        }
        target.last_removal_receipt_hash = fingerprint;
        const blocked = input.status === 'blocked' && input.mutation_started === false;
        Object.assign(operation, { status: blocked ? 'blocked' : 'unknown', stage: 'reconciliation', updated_at: now(),
          residuals: [{ kind: 'RailShotClient', name: id }],
          error: { code: blocked ? 'REMOVAL_PREFLIGHT_FAILED' : 'REMOVAL_UNVERIFIED',
            message: blocked ? '클라이언트 제거 전 검사가 실패했습니다. 상태 재확인 후 다시 요청하세요.' : '클라이언트 제거 결과를 재확인해야 합니다. 자동 재실행하지 않습니다.',
            retryable: false, outcome_unknown: !blocked } });
        updateAttempt(operation, operation.status, operation.stage, operation.error.code);
        target.status = 'attention'; target.blockers = [operation.error.code];
        return { response: { target_id: id, operation_id: operation.id, status: operation.status } };
      });
      return accepted.response || finishGateway(id, input.operation_id, accepted.attempt);
    },
  };
}
