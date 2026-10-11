import { createHash, randomUUID } from 'node:crypto';
import { join, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import { lstat, readdir } from 'node:fs/promises';
import { APP_NAME, TARGET_ID, TENANT_NAME } from './contract.js';
import { EnvironmentError, privateJson, privateDirectory, savePrivate, runEnvironmentCommand } from './environments.js';
import { createCdAdapter } from './cd.js';
import { createAppLogsObserver } from './logs.js';
import { lifecycleActions, lifecycleId, lifecycleHash, lifecycleResources, lifecycleSteps } from './application-lifecycle.js';

const REGISTER = fileURLToPath(new URL('../../../deployment/scripts/applications.py', import.meta.url));
const FINALIZE = fileURLToPath(new URL('../../../deployment/scripts/application_routes.py', import.meta.url));
const LIFECYCLE = fileURLToPath(new URL('../../../deployment/scripts/application_lifecycle.py', import.meta.url));
const hash = (value) => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const fail = (code, status = 409, unknown = false) => new EnvironmentError(code, status, unknown);

/** An operator registers existing environments; uploads allocate only application bindings. */
export async function createApplicationAdapter({ configPath, ciIdentity, loadPublished, python = 'python3', runner = runEnvironmentCommand }) {
  const config = await privateJson(configPath);
  if (config.version !== 1 || !isAbsolute(config.state_dir || '') || !config.environments
      || Array.isArray(config.environments) || typeof config.environments !== 'object'
      || typeof loadPublished !== 'function') throw fail('APPLICATION_CONFIGURATION_INVALID', 503);
  await privateDirectory(config.state_dir);
  const fingerprint = hash(config);
  const targets = Object.fromEntries(Object.entries(config.environments).map(([id, value]) => {
    if (!TARGET_ID.test(id) || !['aws', 'gcp', 'openstack'].includes(value.provider)
        || !TENANT_NAME.test(value.tenant || '')) throw fail('APPLICATION_CONFIGURATION_INVALID', 503);
    if (!ciIdentity || value.tenant !== ciIdentity.tenant || value.source_repository !== ciIdentity.sourceRepository)
      throw fail('APPLICATION_CI_CONFIGURATION_MISMATCH', 503);
    const routeFiles = ['edge_config_file', 'dns_config_file',
      ...(value.provider === 'openstack' ? ['tunnel_config_file'] : [])];
    const automaticDelivery = routeFiles.every((key) => isAbsolute(value.ingress?.[key] || ''));
    return [id, Object.freeze({ provider: value.provider, tenant: value.tenant, deploymentScope: 'environment', automaticDelivery })];
  }));
  function describe(environmentId, app) {
    const environment = Object.hasOwn(targets, environmentId) ? targets[environmentId] : null;
    if (!environment || typeof app !== 'string' || !APP_NAME.test(app)) throw fail('APPLICATION_INPUT_INVALID', 422);
    const id = 'app-' + hash([environmentId, environment.tenant, app]).slice(0, 24);
    return { id, app, target_id: id, environment_target_id: environmentId, provider: environment.provider };
  }
  async function current(application) {
    if (hash(await privateJson(configPath)) !== fingerprint) throw fail('APPLICATION_POLICY_CHANGED');
    const expected = describe(application.environment_target_id, application.app);
    if (expected.id !== application.id || expected.target_id !== application.target_id) throw fail('APPLICATION_BINDING_MISMATCH');
    return join(config.state_dir, expected.id);
  }
  function resultStatus(result, application) {
    if (result?.application_id !== application.id || result.environment_id !== application.environment_target_id
        || result.target_id !== application.target_id || result.app !== application.app) throw fail('APPLICATION_BINDING_MISMATCH', 502, true);
    return result;
  }
  async function lifecycleRequest(application, payload, mutation) {
    const home = await current(application);
    if (!lifecycleId.test(payload.operation_id || '') || !lifecycleActions.includes(payload.action)) throw fail('APPLICATION_INPUT_INVALID', 422);
    const run = join(home, 'lifecycle', payload.operation_id);
    await privateDirectory(run);
    const request = join(run, payload.phase + (['reconcile', 'resume'].includes(payload.phase) ? '-' + randomUUID() : '') + '-request.json');
    // An uncertain executor invocation is never repeated, even with identical bytes.
    try { await lstat(request); throw fail('APPLICATION_OPERATION_RECONCILE_REQUIRED', 409, mutation); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    await savePrivate(request, { version: 1, application_id: application.id, environment_id: application.environment_target_id,
      app: application.app, ...payload });
    const result = await runner(python, [LIFECYCLE, '--config', configPath, '--request', request], { mutation, timeout: mutation ? 1_800_000 : 600_000 });
    await savePrivate(join(run, payload.phase + '-receipt.json'), result);
    return result;
  }
  async function verifyLifecyclePlan(application, plan) {
    await current(application);
    if (plan.private?.configuration_sha256 !== fingerprint || plan.public?.application_id !== application.id
        || !lifecycleId.test(plan.public?.id || '') || !lifecycleHash.test(plan.public?.plan_hash || '')
        || plan.private.native_plan_id !== undefined && !lifecycleId.test(plan.private.native_plan_id)
        || !lifecycleActions.includes(plan.public?.action) || Date.parse(plan.public.expires_at) <= Date.now()
        || !Number.isFinite(Date.parse(plan.public.expires_at))) throw fail('APPLICATION_PLAN_STALE');
    if (plan.private.deferred && (plan.public.action !== 'delete' || !lifecycleId.test(plan.private.deployment_id || '')
        || plan.public.plan_hash !== hash({ application_id: application.id, environment_id: application.environment_target_id,
          app: application.app, configuration_sha256: fingerprint, deployment_id: plan.private.deployment_id,
          expires_at: plan.public.expires_at, scope: 'owned-application' }))) throw fail('APPLICATION_PLAN_STALE');
  }
  return {
    targets: Object.freeze(targets), describe,
    async planPendingDeletion(application, { id, deploymentId }) {
      await current(application);
      if (!lifecycleId.test(id || '') || !lifecycleId.test(deploymentId || '')) throw fail('APPLICATION_INPUT_INVALID', 422);
      const expires_at = new Date(Date.now() + 600_000).toISOString();
      const plan_hash = hash({ application_id: application.id, environment_id: application.environment_target_id,
        app: application.app, configuration_sha256: fingerprint, deployment_id: deploymentId, expires_at, scope: 'owned-application' });
      return { public: { id, application_id: application.id, action: 'delete', plan_hash, expires_at,
        resources: [{ kind: 'ApplicationNamespace', name: application.id }, { kind: 'ApplicationRoutes', name: application.id }],
        retained: [{ kind: 'SharedRuntime', name: application.environment_target_id },
          { kind: 'AuditRecord', name: application.id }, { kind: 'BuildArtifacts', name: application.id }] },
      private: { configuration_sha256: fingerprint, deferred: true, deployment_id: deploymentId } };
    },
    async planLifecycle(application, { id, action }) {
      // Each read-only calculation gets a fresh native identity, including restart recovery.
      const nativeId = randomUUID();
      const result = await lifecycleRequest(application, { phase: 'plan', action, operation_id: nativeId }, false);
      const expires = Date.parse(result.expires_at);
      if (result.status !== 'planned') throw fail(/^[A-Z][A-Z0-9_]{1,95}$/.test(result.error?.code) ? result.error.code : 'APPLICATION_PLAN_BLOCKED');
      if (result.plan_id !== nativeId || !lifecycleHash.test(result.plan_hash || '') || !Number.isFinite(expires)
          || expires <= Date.now() || expires > Date.now() + 600_000) throw fail('APPLICATION_LIFECYCLE_RECEIPT_INVALID', 502);
      return { public: { id, application_id: application.id, action, plan_hash: result.plan_hash,
        resources: lifecycleResources(result.resources), retained: lifecycleResources(result.retained), expires_at: result.expires_at },
      private: { configuration_sha256: fingerprint, native_plan_id: nativeId } };
    },
    verifyLifecyclePlan,
    async reconcileLifecycle(application, plan, { id }) {
      await current(application);
      if (plan.private?.configuration_sha256 !== fingerprint || plan.public?.application_id !== application.id
          || plan.public.action !== 'delete' || plan.private.deferred) throw fail('APPLICATION_PLAN_STALE');
      const result = await lifecycleRequest(application, { phase: 'reconcile', action: 'delete', operation_id: id,
        plan_id: plan.private.native_plan_id || plan.public.id, plan_hash: plan.public.plan_hash, delete_data: true }, false);
      if (result.application_id !== application.id || result.action !== 'delete'
          || !['succeeded', 'blocked', 'unknown'].includes(result.status)) throw fail('APPLICATION_LIFECYCLE_RECEIPT_INVALID', 502);
      return { status: result.status, resumable: result.resumable === true, resume_mode: result.resume_mode,
        steps: lifecycleSteps(result.steps), residuals: lifecycleResources(result.residuals) };
    },
    async resumeLifecycle(application, plan, { id, deleteData }) {
      await current(application);
      if (deleteData !== true || plan.private?.configuration_sha256 !== fingerprint || plan.public?.application_id !== application.id
          || plan.public.action !== 'delete' || plan.private.deferred) throw fail('APPLICATION_PLAN_STALE');
      const result = await lifecycleRequest(application, { phase: 'resume', action: 'delete', operation_id: id,
        plan_id: plan.private.native_plan_id || plan.public.id, plan_hash: plan.public.plan_hash, delete_data: true }, true);
      if (result.application_id !== application.id || result.action !== 'delete'
          || !['succeeded', 'blocked', 'unknown'].includes(result.status)) throw fail('APPLICATION_LIFECYCLE_RECEIPT_INVALID', 502, true);
      return { status: result.status, steps: lifecycleSteps(result.steps), residuals: lifecycleResources(result.residuals) };
    },
    async applyLifecycle(application, plan, { id, deleteData }) {
      await verifyLifecyclePlan(application, plan);
      if (plan.private.deferred) throw fail('APPLICATION_PLAN_STALE');
      const { action, plan_hash: planHash } = plan.public;
      const planId = plan.private.native_plan_id || plan.public.id;
      if (action === 'delete' && deleteData !== true || action !== 'delete' && deleteData !== false) throw fail('APPLICATION_INPUT_INVALID', 422);
      const result = await lifecycleRequest(application, { phase: 'apply', action, operation_id: id,
        plan_id: planId, plan_hash: planHash, delete_data: deleteData }, true);
      if (result.application_id !== application.id || result.action !== action
          || !['succeeded', 'blocked', 'unknown'].includes(result.status)) throw fail('APPLICATION_LIFECYCLE_RECEIPT_INVALID', 502, true);
      return { status: result.status, application_id: application.id, action,
        steps: lifecycleSteps(result.steps), residuals: lifecycleResources(result.residuals) };
    },
    async registrationStarted(application) {
      const home = await current(application);
      try { await lstat(home); }
      catch (error) { if (error.code === 'ENOENT') return false; throw error; }
      await privateDirectory(home);
      // register persists request.json before invoking any native command. Any
      // file, including an interrupted temporary write, keeps recovery fenced.
      return (await readdir(home)).length !== 0;
    },
    async register(application) {
      const home = await current(application);
      await privateDirectory(home);
      const request = join(home, 'request.json');
      await savePrivate(request, { environment_id: application.environment_target_id, app: application.app, application_id: application.id });
      const result = resultStatus(await runner(python, [REGISTER, '--config', configPath, '--request', request],
        { mutation: true, timeout: 600_000 }), application);
      if (result.status !== 'succeeded') throw fail(result.error?.code || 'APPLICATION_REGISTRATION_FAILED', 502, result.status === 'unknown');
      return { ...application, status: 'ready', namespace: result.namespace, node_port: result.node_port,
        hostname: result.hostname, registered_at: new Date().toISOString() };
    },
    async deployPublished(application, args) {
      const home = await current(application);
      if (!/^[a-f0-9-]{36}$/.test(args.deploymentId) || args.app !== application.app || args.targetId !== application.target_id
          || args.publication?.app !== args.app || args.publication?.target_id !== args.targetId
          || args.publication?.tenant !== targets[application.environment_target_id].tenant
          || args.publication?.source_commit !== args.sourceCommit) throw fail('APPLICATION_PUBLICATION_MISMATCH');
      const run = join(home, 'deployments', args.deploymentId);
      await privateDirectory(run);
      const files = await loadPublished(args.publication);
      const request = join(run, 'publication.json');
      const payload = { deployment_id: args.deploymentId, application_id: application.id,
        environment_id: application.environment_target_id, publication: args.publication,
        files: Object.fromEntries(files.map(({ path, content }) => [path, content.toString('base64')])) };
      let exists;
      try { exists = await lstat(request); } catch (error) { if (error.code !== 'ENOENT') throw error; }
      if (exists) {
        if (hash(await privateJson(request, { maxBytes: 14_000_000 })) !== hash(payload)) throw fail('APPLICATION_PUBLICATION_MISMATCH');
      } else await savePrivate(request, payload);
      const result = resultStatus(await runner(python, [FINALIZE, '--config', configPath, '--request', request],
        { mutation: true, timeout: 1_800_000 }), application);
      if (result.status !== 'succeeded') throw fail(result.error?.code || 'APPLICATION_PUBLIC_ROUTE_UNVERIFIED', 502, result.status === 'unknown');
      const deploy = createCdAdapter({ configPath: join(run, 'cd.json'), loadPublished, python });
      return deploy(args);
    },
    async observePublished(application, args) {
      const home = await current(application);
      if (!/^[a-f0-9-]{36}$/.test(args.deploymentId) || args.app !== application.app || args.targetId !== application.target_id)
        throw fail('APPLICATION_PUBLICATION_MISMATCH');
      const configPath = join(home, 'deployments', args.deploymentId, 'cd.json');
      // Recovery reads an existing CD journal; it never runs route finalization or apply.
      try { await privateJson(configPath); }
      catch (error) { if (error.code === 'ENOENT') throw fail('CD_RECORD_MISSING', 409, true); throw error; }
      return createCdAdapter({ configPath, loadPublished, python })({ ...args, observeOnly: true });
    },
    async observeLogs(application, record) {
      const home = await current(application);
      if (!/^[a-f0-9-]{36}$/.test(record.id)) throw fail('APPLICATION_BINDING_MISMATCH');
      return createAppLogsObserver({ configPath: join(home, 'deployments', record.id, 'cd.json'), python })(record);
    },
  };
}
