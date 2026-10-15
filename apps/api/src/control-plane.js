import { randomUUID } from 'node:crypto';
import { unlink } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { EnvironmentError, privateJson, privateDirectory, savePrivate, runEnvironmentCommand } from './environments.js';
import { createApplicationAdapter } from './applications.js';
import { createMetricsObserver } from './metrics.js';

const CLI = fileURLToPath(new URL('../../../infrastructure/providers/environments/cli.py', import.meta.url));
const fail = (code, status = 409) => new EnvironmentError(code, status);
const methods = ['register', 'deployPublished', 'observePublished', 'observeLogs', 'registrationStarted',
  'planPendingDeletion', 'planLifecycle', 'verifyLifecyclePlan', 'applyLifecycle', 'reconcileLifecycle', 'resumeLifecycle'];

/** Thin private-file bridge. Native credentials and paths never originate in HTTP input. */
export async function createControlPlane({ configPath, stateDirectory, base, service,
  python = 'python3', runner = runEnvironmentCommand, applicationFactory = createApplicationAdapter }) {
  await privateDirectory(stateDirectory);
  const environments = new Map(), bindings = new Map(), jobs = new Map(), mutationFences = new Set(), admissions = new Set();
  const observers = new Map();
  const pendingRestores = [];
  let restoreGuard;
  async function call(owner, action, payload = {}) {
    if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(owner || '')) throw fail('AUTHENTICATION_REQUIRED', 401);
    const request = join(stateDirectory, `${randomUUID()}.json`);
    await savePrivate(request, { ...payload, owner_id: owner, action });
    try { return await runner(python, [CLI, '--config', configPath, '--request', request],
      { timeout: ['apply', 'operate', 'delete'].includes(action) ? 7_200_000 : 600_000,
        mutation: ['apply', 'operate', 'delete'].includes(action) }); }
    finally { await unlink(request).catch(() => {}); }
  }
  async function remember(row, owner) {
    environments.set(row.id, { ...row, owner });
    const nativeReady = ['infrastructure', 'cluster', 'storage', 'public_access', ...(row.spec.database.mode === 'standalone' ? ['database'] : [])].every((key) => row.readiness?.[key] === true);
    if (!nativeReady || ['queued', 'running', 'unknown', 'deleted', 'stopped'].includes(row.status)) { bindings.delete(row.id); return row; }
    try {
      const receipt = await call(owner, 'binding', { environment_id: row.id });
      const binding = await applicationFactory({ configPath: receipt.application_config_path,
        ciIdentity: service?.identity, loadPublished: service?.publishedFiles, python, runner });
      if (Object.keys(binding.targets).length !== 1 || binding.targets[row.id]?.provider !== row.spec.provider
          || binding.targets[row.id]?.automaticDelivery !== true) throw fail('APPLICATION_BINDING_INVALID');
      row = await call(owner, 'confirm_binding', { environment_id: row.id, binding_hash: receipt.binding_hash });
      environments.set(row.id, { ...row, owner });
      bindings.set(row.id, binding);
      if (receipt.observer_product_file) observers.set(row.id, createMetricsObserver({ configPath: receipt.observer_product_file }));
    } catch {
      bindings.delete(row.id);
      row = { ...row, deployable: false, blockers: [...(row.blockers || []), 'APPLICATION_BINDING_UNAVAILABLE'] };
      environments.set(row.id, { ...row, owner });
    }
    return row;
  }
  function writable(id, owner, app) {
    if (!id?.startsWith('env-')) return;
    const row = environments.get(id);
    if (!row || row.owner !== owner) throw fail('ENVIRONMENT_NOT_FOUND', 404);
    if (mutationFences.has(id)) throw fail('ENVIRONMENT_BUSY');
    if (!row.deployable || !bindings.has(id)) throw fail('ENVIRONMENT_NOT_READY');
    if (app && row.spec.scope === 'dedicated' && row.spec.application_name !== app)
      throw fail('DEDICATED_ENVIRONMENT_APPLICATION_MISMATCH', 403);
  }
  const application = {
    get targets() { return Object.assign({}, base?.targets || {}, ...[...bindings.values()].map((value) => value.targets)); },
    describe(id, app) {
      const row = environments.get(id);
      if (row?.spec.scope === 'dedicated' && row.spec.application_name !== app) throw fail('DEDICATED_ENVIRONMENT_APPLICATION_MISMATCH', 403);
      const selected = bindings.get(id) || (!id.startsWith('env-') && base);
      if (!selected) throw fail('ENVIRONMENT_NOT_READY');
      return selected.describe(id, app);
    },
  };
  for (const method of methods) application[method] = (app, ...args) => {
    const id = app.environment_target_id;
    const row = environments.get(id);
    if (row?.spec.scope === 'dedicated' && row.spec.application_name !== app.app) throw fail('DEDICATED_ENVIRONMENT_APPLICATION_MISMATCH', 403);
    const selected = bindings.get(id) || (!id.startsWith('env-') && base);
    if (!selected?.[method]) throw fail('APPLICATION_BINDING_UNAVAILABLE');
    return selected[method](app, ...args);
  };
  const control = {
    application, writable,
    async setRestoreGuard(guard) {
      restoreGuard = guard;
      for (const item of pendingRestores.splice(0)) {
        try { await control.mutate(item.owner, item.environment_id, item.action, item.idempotency_key, item.operation); }
        catch { /* A fresh product guard must succeed; leave the durable intent queued. */ }
      }
    },
    observeMetrics(record, fallback) {
      const observer = observers.get(record.environment_target_id || record.target_id);
      return (observer || fallback)(record);
    },
    owns: (id, owner) => environments.get(id)?.owner === owner,
    isEnvironment: (id) => typeof id === 'string' && id.startsWith('env-'),
    targets: (owner) => [...environments.values()].filter((row) => row.owner === owner && row.status !== 'deleted').map((row) => ({
      id: row.id, label: row.spec.name, provider: row.spec.provider, scope: 'owned', environment: row.spec.provider === 'openstack' ? 'onprem' : 'cloud',
      status: row.status, deployable: row.deployable, available: row.deployable, deployment_scope: 'environment',
    })),
    accounts: (owner) => call(owner, 'accounts'),
    connection: (owner, account_id) => call(owner, 'connection', { account_id }),
    catalog: (owner, account_id) => call(owner, 'catalog', { account_id }),
    async list(owner) {
      const result = await call(owner, 'list');
      return { items: await Promise.all(result.items.map((row) => remember(row, owner))) };
    },
    async get(owner, id) { return remember(await call(owner, 'get', { environment_id: id }), owner); },
    async plan(owner, spec, key) { return remember(await call(owner, 'plan', { spec, idempotency_key: key }), owner); },
    async mutate(owner, id, action, key, operation) {
      if (admissions.has(id)) throw fail('ENVIRONMENT_BUSY');
      admissions.add(id);
      mutationFences.add(id);
      let accepted;
      let restoreAuthorization;
      try {
        if (action === 'operate' && operation?.action === 'restore') {
          if (!restoreGuard) throw fail('RESTORE_PRODUCT_GUARD_UNAVAILABLE');
          restoreGuard(id, owner);
          restoreAuthorization = { restore_authorized: true, active_applications: 0 };
        }
        accepted = await call(owner, 'admit', { environment_id: id, operation_action: action,
          idempotency_key: key, ...(operation ? { operation } : {}), ...(restoreAuthorization ? { restore_authorization: restoreAuthorization } : {}) });
      await remember(accepted, owner);
      if (accepted.status !== 'queued' || jobs.has(id)) { if (!jobs.has(id)) mutationFences.delete(id); return accepted; }
      bindings.delete(id);
      const task = call(owner, action, { environment_id: id, idempotency_key: key, ...(operation ? { operation } : {}),
        ...(restoreAuthorization ? { restore_authorization: restoreAuthorization } : {}) })
        .then((row) => remember(row, owner)).catch(() => { environments.delete(id); bindings.delete(id); })
        .finally(() => { jobs.delete(id); if (!admissions.has(id)) mutationFences.delete(id); });
      jobs.set(id, task);
      return accepted;
      } finally { admissions.delete(id); if (!jobs.has(id)) mutationFences.delete(id); }
    },
    async reconcile(owner, id) { return remember(await call(owner, 'reconcile', { environment_id: id }), owner); },
    async close() { await Promise.allSettled(jobs.values()); },
  };
  // Restore registered applications after API restarts; each owner is scoped in the Python registry.
  const config = await privateJson(configPath);
  for (const owner of new Set(config.accounts.map((account) => account.owner_id))) {
    await control.list(owner);
    // Only durable intents with no native dispatch marker are resumable automatically.
    const pending = await call(owner, 'pending');
    for (const operation of pending.items) {
      if (operation.operation?.action === 'restore') pendingRestores.push({ ...operation, owner });
      else await control.mutate(owner, operation.environment_id, operation.action, operation.idempotency_key, operation.operation);
    }
  }
  return control;
}
