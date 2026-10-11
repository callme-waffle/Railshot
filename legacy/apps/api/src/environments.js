import { APP_NAME, TENANT_NAME } from './contract.js';
import { createHash, randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import { lstat, mkdir, open, readFile, realpath, rename } from 'node:fs/promises';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = fileURLToPath(new URL('../../../', import.meta.url));
const PROVISION = join(REPO, 'infrastructure/providers/terraform_tools/provision.py');
const BUDGET = join(REPO, 'infrastructure/providers/terraform_tools/budget.py');
const CLUSTER = join(REPO, 'infrastructure/ansible/cluster.py');
const STACK = join(REPO, 'deployment/scripts/environment.py');
const ANSIBLE = join(REPO, 'infrastructure/ansible/run.py');
const ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const SHA = /^[a-f0-9]{64}$/;
const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const canonical = (value) => Array.isArray(value) ? value.map(canonical) : object(value)
  ? Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])])) : value;
const hash = (value) => createHash('sha256').update(JSON.stringify(canonical(value))).digest('hex');
const validId = (value) => typeof value === 'string' && ID.test(value);
const exact = (value, required, optional = []) => object(value) && required.every((key) => key in value)
  && Object.keys(value).every((key) => [...required, ...optional].includes(key));

export class EnvironmentError extends Error {
  constructor(code, status = 400, outcomeUnknown = false) {
    super(code); this.code = code; this.status = status; this.outcomeUnknown = outcomeUnknown;
  }
}

export async function privateJson(path, { maxBytes = 1024 * 1024 } = {}) {
  try {
    const info = await lstat(path);
    if (!isAbsolute(path) || !info.isFile() || info.uid !== process.getuid() || (info.mode & 0o077)
        || info.size > maxBytes) throw new Error();
    return JSON.parse(await readFile(path, 'utf8'));
  } catch { throw new EnvironmentError('ENVIRONMENT_CONFIGURATION_INVALID', 503); }
}

export async function privateDirectory(path) {
  if (!isAbsolute(path)) throw new EnvironmentError('ENVIRONMENT_CONFIGURATION_INVALID', 503);
  await mkdir(path, { recursive: true, mode: 0o700 });
  const info = await lstat(path);
  if (!info.isDirectory() || info.uid !== process.getuid() || (info.mode & 0o077)
      || await realpath(path) !== resolve(path)) throw new EnvironmentError('ENVIRONMENT_CONFIGURATION_INVALID', 503);
}

export async function savePrivate(path, value) {
  const temporary = `${path}.${randomUUID()}.tmp`;
  const file = await open(temporary, 'wx', 0o600);
  try { await file.writeFile(JSON.stringify(value)); await file.sync(); } finally { await file.close(); }
  await rename(temporary, path);
  const directory = await open(dirname(path), 'r');
  try { await directory.sync(); } finally { await directory.close(); }
}

// Fixed scripts and argv only. Bound the process group so a timeout cannot leave Terraform running unseen.
export function runEnvironmentCommand(python, args, { timeout = 120000, mutation = false } = {}) {
  return new Promise((resolveCommand, reject) => {
    const child = spawn(python, args, { cwd: REPO, detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '', stderr = '', interrupted = false;
    const stop = () => { interrupted = true; try { process.kill(-child.pid, 'SIGKILL'); } catch {} };
    const timer = setTimeout(stop, timeout);
    const collect = (which, chunk) => {
      if (which === 'stdout') stdout += chunk; else stderr += chunk;
      if (Buffer.byteLength(stdout) + Buffer.byteLength(stderr) > 1024 * 1024) stop();
    };
    child.stdout.on('data', (chunk) => collect('stdout', chunk));
    child.stderr.on('data', (chunk) => collect('stderr', chunk));
    child.on('error', () => { clearTimeout(timer); reject(new EnvironmentError('EXECUTOR_UNAVAILABLE', 503)); });
    child.on('close', (code) => {
      clearTimeout(timer);
      if (interrupted) { reject(new EnvironmentError('EXECUTOR_OUTCOME_UNKNOWN', 503, mutation)); return; }
      let result;
      try { result = JSON.parse(stdout || stderr); } catch {
        reject(new EnvironmentError('EXECUTOR_RESPONSE_INVALID', 502, mutation)); return;
      }
      if (code !== 0) {
        const error = result?.error;
        const unknown = error?.outcome_unknown === true || error?.outcome === 'UNKNOWN'
          || (mutation && !object(error))
          || (mutation && ['possible', 'unknown', 'completed'].includes(error?.side_effect));
        reject(new EnvironmentError(/^[A-Z][A-Z0-9_]{1,95}$/.test(error?.code) ? error.code : 'EXECUTOR_FAILED',
          error?.outcome === 'BLOCKED' || result.status === 'blocked' ? 409 : 502, unknown)); return;
      }
      resolveCommand(result);
    });
  });
}

/** Operator profiles authorize fixed targets or creation templates; HTTP never supplies paths, SSH or provider variables. */
export async function createEnvironmentAdapter({ profilesFile, stateDir, python = 'python3', runner = runEnvironmentCommand,
  now = () => Date.now(), loadPublished } = {}) {
  if (!profilesFile) return {
    profiles: () => [],
    plan: async () => { throw new EnvironmentError('PROFILES_NOT_CONFIGURED', 503); },
    verifyPlan: async () => { throw new EnvironmentError('PROFILES_NOT_CONFIGURED', 503); },
    execute: async () => { throw new EnvironmentError('PROFILES_NOT_CONFIGURED', 503); },
  };
  await privateDirectory(stateDir);
  if (resolve(stateDir).startsWith(resolve(REPO) + '/')) throw new EnvironmentError('PRIVATE_STATE_OUTSIDE_REPOSITORY_REQUIRED', 503);

  async function configuration() {
    const config = await privateJson(profilesFile);
    if (!exact(config, ['version', 'policy_revision', 'profiles']) || config.version !== 1
        || typeof config.policy_revision !== 'string' || !ID.test(config.policy_revision)
        || !Array.isArray(config.profiles) || config.profiles.length > 100)
      throw new EnvironmentError('ENVIRONMENT_CONFIGURATION_INVALID', 503);
    const ids = new Set(), targets = new Set();
    for (const profile of config.profiles) {
      if (!exact(profile, ['id', 'label', 'provider', 'site', 'purposes', 'target_file', 'state_root', 'ssh', 'allow_apply'], ['timeout_seconds', 'database', 'deployment_file', 'enroll_ssh', 'create_per_request', 'registration_max_age_seconds', 'budget_refresh'])
          || !validId(profile.id) || ids.has(profile.id) || typeof profile.label !== 'string' || !profile.label.trim()
          || typeof profile.site !== 'string' || !profile.site || !Array.isArray(profile.purposes)
          || profile.purposes.some((purpose) => !['runtime', 'database'].includes(purpose)) || typeof profile.allow_apply !== 'boolean'
          || typeof profile.target_file !== 'string' || !isAbsolute(profile.target_file)
          || typeof profile.state_root !== 'string' || !isAbsolute(profile.state_root)
          || !exact(profile.ssh, ['user', 'identity_file', 'known_hosts_file'])
          || !/^[a-z_][a-z0-9_-]{0,31}$/.test(profile.ssh.user)
          || ![profile.ssh.identity_file, profile.ssh.known_hosts_file].every((path) => typeof path === 'string' && /^\/[A-Za-z0-9_./-]+$/.test(path) && !path.split('/').includes('..'))
          || !Number.isInteger(profile.timeout_seconds ?? 1200) || (profile.timeout_seconds ?? 1200) < 30 || (profile.timeout_seconds ?? 1200) > 1800)
        throw new EnvironmentError('ENVIRONMENT_CONFIGURATION_INVALID', 503);
      const target = await privateJson(profile.target_file);
      if (target.provider_kind !== profile.provider || typeof target.target_id !== 'string' || !/^[a-z][a-z0-9-]{2,39}$/.test(target.target_id)
          || targets.has(target.target_id)) throw new EnvironmentError('ENVIRONMENT_CONFIGURATION_INVALID', 503);
      ids.add(profile.id); targets.add(target.target_id);
      profile.target = target;
      if (profile.enroll_ssh !== undefined && typeof profile.enroll_ssh !== 'boolean')
        throw new EnvironmentError('ENVIRONMENT_CONFIGURATION_INVALID', 503);
      if (profile.create_per_request !== undefined && typeof profile.create_per_request !== 'boolean')
        throw new EnvironmentError('ENVIRONMENT_CONFIGURATION_INVALID', 503);
      if (profile.budget_refresh !== undefined && (profile.provider !== 'aws' || !exact(profile.budget_refresh, [], ['retained_storage_hours'])
          || (profile.budget_refresh.retained_storage_hours !== undefined && profile.budget_refresh.retained_storage_hours !== 24)))
        throw new EnvironmentError('ENVIRONMENT_CONFIGURATION_INVALID', 503);
      if (profile.deployment_file !== undefined) profile.deployment = await privateJson(profile.deployment_file);
      if (profile.registration_max_age_seconds !== undefined && (!Number.isInteger(profile.registration_max_age_seconds)
          || profile.registration_max_age_seconds < 1800 || profile.registration_max_age_seconds > 604800
          || !profile.create_per_request || !object(profile.deployment?.registration)))
        throw new EnvironmentError('ENVIRONMENT_CONFIGURATION_INVALID', 503);
      if (profile.database !== undefined) {
        if (!exact(profile.database, ['nodes']) || !Array.isArray(profile.database.nodes)
            || profile.database.nodes.length < 3 || profile.database.nodes.length > 16)
          throw new EnvironmentError('ENVIRONMENT_CONFIGURATION_INVALID', 503);
        for (const node of profile.database.nodes) {
          if (!exact(node, ['target_file', 'roles']) || !Array.isArray(node.roles) || !node.roles.length
              || new Set(node.roles).size !== node.roles.length || node.roles.some((role) => !['database', 'dcs', 'proxy'].includes(role))
              || node.roles.includes('database') && node.roles.includes('proxy'))
            throw new EnvironmentError('ENVIRONMENT_CONFIGURATION_INVALID', 503);
          node.target = await privateJson(node.target_file);
          if (node.target.provider_kind !== profile.provider || !/^[a-z][a-z0-9-]{2,39}$/.test(node.target.target_id)
              || node.target.profile?.kind !== 'database_cluster' || node.target.variables?.purpose !== 'database'
              || targets.has(node.target.target_id)) throw new EnvironmentError('ENVIRONMENT_CONFIGURATION_INVALID', 503);
          targets.add(node.target.target_id);
        }
      }
    }
    return config;
  }

  function blockersFor(profile) {
    const blockers = [];
    if (!['aws', 'gcp'].includes(profile.provider)) blockers.push('PROVIDER_RUNTIME_UNSUPPORTED');
    if (!profile.purposes.includes('runtime')) blockers.push('PROFILE_PURPOSE_UNSUPPORTED');
    if (!profile.allow_apply) blockers.push('PROFILE_EXECUTION_DISABLED');
    return blockers;
  }
  // Reading current profiles also makes operator policy revocation effective before dispatch.
  const initial = await configuration();
  let listed = initial;
  return {
    profiles: () => listed.profiles.map((profile) => ({
      ...Object.fromEntries(['id', 'label', 'provider', 'site', 'purposes'].map((key) => [key, profile[key]])),
      target_id: profile.target.target_id,
      create_per_request: profile.create_per_request === true,
      application_name: profile.create_per_request ? null : profile.deployment?.cd?.targets?.[profile.target.target_id]?.app || null,
      deployment_supported: Boolean(profile.deployment),
      database: profile.database ? { mode: 'patroni', required: Boolean(profile.deployment?.cd?.targets?.[profile.target.target_id]?.target?.database), database_nodes: profile.database.nodes.filter((node) => node.roles.includes('database')).length, dcs_voters: profile.database.nodes.filter((node) => node.roles.includes('dcs')).length, proxy_nodes: profile.database.nodes.filter((node) => node.roles.includes('proxy')).length } : null,
      supported: blockersFor(profile).length === 0, blockers: blockersFor(profile),
    })),

    async deployPublished(id, args) {
      if (!validId(id) || !loadPublished) throw new EnvironmentError('CD_ADAPTER_NOT_CONFIGURED', 503);
      const { createCdAdapter } = await import('./cd.js');
      const deploy = createCdAdapter({ configPath: join(stateDir, id, 'cd.json'), loadPublished, python });
      return deploy(args);
    },
    async observeLogs(id, record) {
      if (!validId(id)) throw new EnvironmentError('INVALID_ENVIRONMENT_ID', 422);
      const { createAppLogsObserver } = await import('./logs.js');
      return createAppLogsObserver({ configPath: join(stateDir, id, 'cd.json'), python })(record);
    },
    async plan(input, { id }) {
      if (!validId(id) || !exact(input, ['name', 'runtime', 'database']) || typeof input.name !== 'string'
          || !APP_NAME.test(input.name)
          || !exact(input.runtime, ['profile_id', 'node_count']) || typeof input.runtime.profile_id !== 'string'
          || !Number.isInteger(input.runtime.node_count) || input.runtime.node_count < 1 || input.runtime.node_count > 64
          || !exact(input.database, ['mode'], ['placements']) || !['none', 'standalone', 'patroni'].includes(input.database.mode)
          || ('placements' in input.database && (!Array.isArray(input.database.placements) || input.database.placements.length > 16))
          || input.database.placements?.some((placement) => !exact(placement, ['profile_id', 'database_nodes', 'dcs_voters', 'proxy_nodes'])
            || typeof placement.profile_id !== 'string' || !ID.test(placement.profile_id)
            || ['database_nodes', 'dcs_voters', 'proxy_nodes'].some((key) => !Number.isInteger(placement[key]) || placement[key] < 0 || placement[key] > 31))
          || (input.database.mode === 'none' && input.database.placements?.length))
        throw new EnvironmentError('INVALID_PLAN_REQUEST');
      listed = await configuration();
      const profile = listed.profiles.find((row) => row.id === input.runtime.profile_id);
      if (!profile) throw new EnvironmentError('PROFILE_NOT_REGISTERED', 404);
      const blockers = blockersFor(profile);
      if (profile.deployment) {
        const registered = profile.deployment.cd?.targets?.[profile.target.target_id];
        if (!registered || !APP_NAME.test(registered.app) || (!profile.create_per_request && registered.app !== input.name) || registered.target?.id !== profile.target.target_id
            || typeof registered.tenant !== 'string' || !TENANT_NAME.test(registered.tenant))
          blockers.push('DEPLOYMENT_PROFILE_MISMATCH');
        if (Boolean(registered?.target?.database) !== (input.database.mode === 'patroni'))
          blockers.push('DATABASE_BINDING_PROFILE_MISMATCH');
      }
      if (input.runtime.node_count !== 1) blockers.push('SINGLE_NODE_ONLY');
      const selected = [{ profile, target: profile.target, roles: ['runtime'] }];
      if (input.database.mode === 'standalone') blockers.push('STANDALONE_DATABASE_UNSUPPORTED');
      if (input.database.mode === 'patroni') {
        if (!input.database.placements?.length) blockers.push('DATABASE_PLACEMENT_REQUIRED');
        const placements = new Set();
        for (const placement of input.database.placements || []) {
          const databaseProfile = listed.profiles.find((row) => row.id === placement.profile_id);
          if (!databaseProfile?.database || !databaseProfile.purposes.includes('database') || placements.has(placement.profile_id)) {
            blockers.push('DATABASE_PROFILE_UNSUPPORTED'); continue;
          }
          placements.add(placement.profile_id);
          if (!databaseProfile.allow_apply) blockers.push('PROFILE_EXECUTION_DISABLED');
          for (const [count, role] of [['database_nodes', 'database'], ['dcs_voters', 'dcs'], ['proxy_nodes', 'proxy']])
            if (placement[count] !== databaseProfile.database.nodes.filter((node) => node.roles.includes(role)).length)
              blockers.push('DATABASE_TOPOLOGY_MISMATCH');
          for (const node of databaseProfile.database.nodes) selected.push({ profile: databaseProfile, target: node.target, roles: node.roles });
        }
        const count = (role) => selected.filter((node) => node.roles.includes(role)).length;
        if (count('database') < 2 || count('dcs') < 3 || count('dcs') % 2 !== 1 || count('proxy') < 1)
          blockers.push('DATABASE_TOPOLOGY_UNSUPPORTED');
      }
      // Keep selected[*].profile unchanged for live policy verification; only execution targets are derived.
      const executionProfile = structuredClone(profile);
      if (profile.create_per_request) {
        // GCP VM names allow 25 characters; leave room for the largest database suffix (-dbNN).
        const prefixLength = selected.some((node) => node.profile.provider === 'gcp')
          ? Math.min(11, 25 - 9 - 3 - String(selected.length - 1).length) : 24;
        const runtimeId = `${profile.target.target_id.slice(0, prefixLength).replace(/-+$/, '')}-${createHash('sha256').update(id).digest('hex').slice(0, 8)}`;
        for (const [index, node] of selected.entries()) {
          const targetId = index === 0 ? runtimeId : `${runtimeId}-db${index}`;
          node.target = { ...structuredClone(node.target), target_id: targetId,
            variables: { ...structuredClone(node.target.variables), target_id: targetId, name: targetId } };
        }
        executionProfile.target = selected[0].target;
        if (executionProfile.deployment) {
          const targets = executionProfile.deployment.cd?.targets;
          const registered = targets?.[profile.target.target_id];
          const segments = typeof registered?.target?.path === 'string' ? registered.target.path.split('/') : null;
          if (!segments || segments.length < 3 || segments.some((segment) => !segment || segment === '.' || segment === '..')
              || segments.at(-2) !== registered.app || segments.at(-1) !== profile.target.target_id) {
            blockers.push('DEPLOYMENT_PROFILE_MISMATCH');
          } else {
            registered.app = input.name;
            Object.assign(registered.target, { id: runtimeId, namespace: `app-${input.name}`,
              project: `railshot-${runtimeId}`, path: [...segments.slice(0, -2), input.name, runtimeId].join('/') });
            if (registered.target.image_pull_secret) registered.target.image_pull_secret.namespace = registered.target.namespace;
            delete targets[profile.target.target_id];
            targets[runtimeId] = registered;
          }
        }
      }
      try {
        for (const selectedProfile of new Set(selected.map((node) => node.profile))) for (const [key, mask] of [['identity_file', 0o077], ['known_hosts_file', 0o022]]) {
          if (key === 'known_hosts_file' && selectedProfile.enroll_ssh) continue;
          const info = await lstat(selectedProfile.ssh[key]);
          if (!info.isFile() || !info.size || info.uid !== process.getuid() || (info.mode & mask)) throw new Error();
        }
      } catch { blockers.push('SSH_REFERENCES_UNAVAILABLE'); }
      const home = join(stateDir, id);
      await mkdir(home, { mode: 0o700 }); // A plan ID and its input snapshot are never overwritten.
      let cost = null;
      if (selected.some((node) => node.profile.budget_refresh !== undefined)) {
        const policy = profile.budget_refresh;
        if (!policy || selected.some((node) => node.profile.provider !== 'aws'
            || hash(node.profile.budget_refresh ?? null) !== hash(policy))) blockers.push('BUDGET_REFRESH_POLICY_MISMATCH');
        if (!blockers.length) {
          try {
            const inputFile = join(home, 'budget-input.json'), outputFile = join(home, 'budget-output.json');
            await savePrivate(inputFile, { version: 1, targets: selected.map((node) => node.target), ...policy });
            const summary = await runner(python, [BUDGET, '--input', inputFile, '--evidence-dir', join(home, 'budget-evidence'), '--output', outputFile], { timeout: 120000 });
            const refreshed = await privateJson(outputFile);
            const money = (value) => typeof value === 'string' && /^\d+(?:\.\d+)?$/.test(value) && Number.isFinite(Number(value));
            if (!exact(refreshed, ['version', 'targets', 'summary', 'evidence']) || refreshed.version !== 1
                || !Array.isArray(refreshed.targets) || refreshed.targets.length !== selected.length || !Array.isArray(refreshed.evidence)
                || !exact(summary, ['currency', 'incremental_estimate', 'projected_total', 'limit', 'within_budget']) || summary.currency !== 'USD'
                || !['incremental_estimate', 'projected_total', 'limit'].every((key) => money(summary[key]))
                || typeof summary.within_budget !== 'boolean' || hash(summary) !== hash(refreshed.summary))
              throw new EnvironmentError('BUDGET_REFRESH_INVALID', 502);
            for (const [index, target] of refreshed.targets.entries()) {
              const original = selected[index].target;
              const quote = target?.budget;
              if (!object(target) || !exact(quote, ['ledger_path', 'scope', 'currency', 'incremental_cost', 'limit', 'unreported_cost', 'quoted_at', 'expires_at'])
                  || hash({ ...target, budget: original.budget }) !== hash(original)
                  || hash(quote) !== hash({ ...original.budget, ...Object.fromEntries(['scope', 'incremental_cost', 'quoted_at', 'expires_at'].map((key) => [key, quote[key]])) })
                  || !money(quote.incremental_cost) || !Number.isFinite(Date.parse(quote.quoted_at)) || Date.parse(quote.quoted_at) > now()
                  || !Number.isFinite(Date.parse(quote.expires_at)) || Date.parse(quote.expires_at) <= now())
                throw new EnvironmentError('BUDGET_REFRESH_INVALID', 502);
            }
            for (const [index, target] of refreshed.targets.entries()) selected[index].target = target;
            executionProfile.target = selected[0].target;
            cost = summary;
            if (!cost.within_budget) blockers.push('INFRA_BUDGET_BLOCKED');
          } catch (error) { blockers.push(error instanceof EnvironmentError ? error.code : 'BUDGET_REFRESH_FAILED'); }
        }
      }
      const nodes = [];
      for (const [index, selectedNode] of selected.entries()) {
        const targetFile = join(home, index === 0 ? 'target.json' : `target-${index}.json`);
        await savePrivate(targetFile, selectedNode.target);
        let review = null;
        if (!blockers.length) {
          try {
            review = await runner(python, [PROVISION, 'plan', '--target', targetFile, '--state-root', selectedNode.profile.state_root]);
            if (!SHA.test(review.plan_sha256) || review.target_id !== selectedNode.target.target_id || !Array.isArray(review.changes))
              throw new EnvironmentError('PROVIDER_PLAN_INVALID', 502);
            if (review.destructive || review.maintenance_required
                || review.changes.some((change) => !Array.isArray(change.actions) || change.actions.some((action) => !['create', 'read'].includes(action))))
              blockers.push('EXISTING_RESOURCE_CHANGE_REQUIRES_OPERATOR');
            if (!review.changes.some((change) => change.actions.includes('create'))) blockers.push('NO_NEW_RESOURCES');
          } catch (error) { blockers.push(error instanceof EnvironmentError ? error.code : 'PROVIDER_PLAN_FAILED'); }
        }
        nodes.push({ ...selectedNode, target_file: targetFile, plan_sha256: review?.plan_sha256 ?? null });
      }
      const readyAt = now();
      let expiresAt = readyAt + 15 * 60 * 1000;
      for (const node of nodes) if (node.target.budget?.expires_at !== undefined) {
        const quoteExpiry = Date.parse(node.target.budget.expires_at);
        if (!Number.isFinite(quoteExpiry) || quoteExpiry <= readyAt) blockers.push('BUDGET_QUOTE_EXPIRED');
        if (Number.isFinite(quoteExpiry)) expiresAt = Math.min(expiresAt, quoteExpiry);
      }
      if (profile.registration_max_age_seconds !== undefined)
        executionProfile.deployment.registration.expires_at = new Date(readyAt + profile.registration_max_age_seconds * 1000).toISOString();
      const planDigest = nodes.length === 1 ? nodes[0].plan_sha256 : hash(nodes.map((node) => [node.target.target_id, node.plan_sha256]));
      const snapshot = { profile: executionProfile, policy_revision: listed.policy_revision, nodes, input_sha256: hash(input) };
      return {
        public: { id, ...structuredClone(input), runtime_target_id: executionProfile.target.target_id, policy_revision: listed.policy_revision,
          expires_at: new Date(expiresAt).toISOString(), executable: blockers.length === 0,
          blockers: [...new Set(blockers)], cost, steps: ['resources', 'guest', 'runtime', ...(input.database.mode === 'none' ? [] : ['database', 'binding']), ...(profile.deployment ? ['registration'] : [])], plan_sha256: planDigest },
        private: { ...snapshot, snapshot_sha256: hash(snapshot) },
      };
    },

    async verifyPlan(record) {
      const { public: plan, private: snapshot } = record;
      if (!plan?.executable || !SHA.test(plan.plan_sha256)) throw new EnvironmentError('PLAN_NOT_EXECUTABLE', 409);
      if (!Number.isFinite(Date.parse(plan.expires_at)) || Date.parse(plan.expires_at) <= now()) throw new EnvironmentError('PLAN_EXPIRED', 409);
      const { snapshot_sha256: checksum, ...original } = snapshot;
      if (checksum !== hash(original) || snapshot.input_sha256 !== hash({ name: plan.name, runtime: plan.runtime, database: plan.database })
          || snapshot.profile.target.target_id !== snapshot.nodes[0].target.target_id
          || (plan.runtime_target_id !== undefined && plan.runtime_target_id !== snapshot.profile.target.target_id))
        throw new EnvironmentError('PLAN_SNAPSHOT_CHANGED', 409);
      const current = await configuration();
      for (const node of snapshot.nodes) {
        const profile = current.profiles.find((row) => row.id === node.profile.id);
        if (current.policy_revision !== plan.policy_revision || !profile || hash(profile) !== hash(node.profile))
          throw new EnvironmentError('PROFILE_POLICY_CHANGED', 409);
        if (hash(await privateJson(node.target_file)) !== hash(node.target)) throw new EnvironmentError('PLAN_SNAPSHOT_CHANGED', 409);
        const manifest = await privateJson(join(profile.state_root, node.target.target_id, 'plan-manifest.json'));
        if (manifest.plan_sha256 !== node.plan_sha256) throw new EnvironmentError('SAVED_PLAN_REPLACED', 409);
        if (manifest.apply_attempted) throw new EnvironmentError('PLAN_ALREADY_EXECUTED', 409);
      }
      const planDigest = snapshot.nodes.length === 1 ? snapshot.nodes[0].plan_sha256
        : hash(snapshot.nodes.map((node) => [node.target.target_id, node.plan_sha256]));
      if (planDigest !== plan.plan_sha256) throw new EnvironmentError('PLAN_SNAPSHOT_CHANGED', 409);
    },

    async execute(record, { id, onProgress = async () => {} }) {
      if (!validId(id)) throw new EnvironmentError('INVALID_ENVIRONMENT_ID');
      await this.verifyPlan(record);
      const { public: plan, private: { profile, nodes } } = record;
      const home = join(stateDir, id);
      await mkdir(home, { mode: 0o700 });
      const state = { status: 'running', stage: 'resources', resources: { status: 'running' },
        guest: { status: 'queued' }, runtime: { status: 'queued' }, database: { status: 'skipped' },
        runtime_target_id: null, deployment_supported: false, blockers: ['DEPLOYMENT_TARGET_NOT_REGISTERED'], error: null };
      const progress = async () => onProgress(structuredClone(state));
      await progress();
      try {
        const registry = { version: 1, targets: {} }, descriptors = [];
        for (const node of nodes) {
          const applied = await runner(python, [PROVISION, 'apply', '--target', node.target_file, '--state-root', node.profile.state_root,
            '--plan-sha256', node.plan_sha256], { timeout: 30 * 60 * 1000, mutation: true });
          const descriptor = applied.node_descriptor;
          if (applied.apply_status !== 'completed' || applied.plan_sha256 !== node.plan_sha256
              || !object(descriptor) || descriptor.target_id !== node.target.target_id || descriptor.provider_kind !== node.profile.provider)
            throw new EnvironmentError('PROVIDER_RESULT_INVALID', 502, true);
          descriptors.push(descriptor);
          const descriptorFile = join(home, `${descriptor.target_id}.json`);
          await savePrivate(descriptorFile, descriptor);
          let ssh = node.profile.ssh;
          if (node.profile.enroll_ssh) {
            const knownHosts = join(home, `${descriptor.target_id}.known_hosts`);
            const enrolled = await runner(python, [join(REPO, 'infrastructure/providers/terraform_tools/access.py'), '--descriptor', descriptorFile, '--output', knownHosts],
              { timeout: 10 * 60 * 1000, mutation: true });
            if (enrolled.status !== 'succeeded' || enrolled.target_id !== descriptor.target_id)
              throw new EnvironmentError('SSH_ENROLLMENT_UNVERIFIED', 502, true);
            ssh = { ...ssh, known_hosts_file: knownHosts };
          }
          registry.targets[descriptor.target_id] = { descriptor_file: descriptorFile, ssh,
            timeout_seconds: node.profile.timeout_seconds ?? 1800, purpose: node.roles.includes('runtime') ? 'runtime' : 'database' };
          await savePrivate(join(home, 'targets.json'), registry);
        }
        state.resources = { status: 'succeeded', nodes: descriptors.map((node) => ({ target_id: node.target_id, provider: node.provider_kind })) };
        state.stage = 'guest'; state.guest = { status: 'running' }; await progress();
        const descriptor = descriptors[0], registered = registry.targets[descriptor.target_id];
        const descriptorFile = registered.descriptor_file;
        const nativeArgs = (operation) => [ANSIBLE, '--node-descriptor', registered.descriptor_file,
          '--request-id', `${id}.${operation}`, '--operation', operation, '--ssh-user', registered.ssh.user,
          '--identity-file', registered.ssh.identity_file, '--known-hosts-file', registered.ssh.known_hosts_file,
          '--timeout-seconds', String(registered.timeout_seconds), '--state-dir', join(home, 'ansible')];
        const validated = await runner(python, [...nativeArgs('guest.check'), '--validate-only']);
        if (validated.status !== 'validated' || validated.target_id !== descriptor.target_id)
          throw new EnvironmentError('REGISTERED_TARGET_INVALID', 502, true);
        state.runtime_target_id = descriptor.target_id;
        for (const [stage, operation] of [['guest', 'guest.check'], ['runtime', 'runtime.install']]) {
          state.stage = stage; state[stage] = { status: 'running' }; await progress();
          if (hash(await privateJson(descriptorFile)) !== hash(descriptor)
              || hash(await privateJson(join(home, 'targets.json'))) !== hash(registry))
            throw new EnvironmentError('REGISTERED_TARGET_CHANGED', 409);
          const result = await runner(python, nativeArgs(operation), { timeout: (registered.timeout_seconds + 120) * 1000,
            mutation: operation === 'runtime.install' });
          if (result.status !== 'succeeded' || result.request_id !== `${id}.${operation}` || result.target_id !== descriptor.target_id
              || result.operation !== operation || result[`${stage}_ready`] !== true)
            throw new EnvironmentError('READINESS_RESULT_INVALID', 502, operation === 'runtime.install');
          state[stage] = { status: 'succeeded', ansible_job_id: result.request_id }; await progress();
        }
        let bindingFile;
        if (plan.database.mode === 'patroni') {
          state.stage = 'database'; state.database = { status: 'running' }; await progress();
          const registryFile = join(home, 'database-targets.json');
          await savePrivate(registryFile, { version: 1, targets: Object.fromEntries(Object.entries(registry.targets).filter(([, value]) => value.purpose === 'database')) });
          const specFile = join(home, 'cluster-spec.json');
          await savePrivate(specFile, { version: 1, request_id: `${id}.database`, cluster_name: plan.name, app_name: plan.name,
            nodes: nodes.filter((node) => !node.roles.includes('runtime')).map((node) => ({ target_id: node.target.target_id, roles: node.roles })),
            client_cidrs: [`${descriptor.addresses.private}/32`], timeout_seconds: 1800 });
          const result = await runner(python, [CLUSTER, '--registry-file', registryFile, '--spec-file', specFile, '--state-dir', join(home, 'database')],
            { timeout: 35 * 60 * 1000, mutation: true });
          if (result.status !== 'succeeded' || result.database_ready !== true || result.request_id !== `${id}.database`
              || typeof result.binding_file !== 'string' || !result.binding_file.startsWith(home + '/')
              || !SHA.test(result.binding_sha256) || createHash('sha256').update(await readFile(result.binding_file)).digest('hex') !== result.binding_sha256)
            throw new EnvironmentError('DATABASE_BINDING_UNVERIFIED', 502, true);
          bindingFile = result.binding_file;
          state.database = { status: 'succeeded', ansible_job_id: result.request_id }; await progress();
        }
        if (profile.deployment) {
          state.stage = 'binding'; state.binding = { status: 'running' }; await progress();
          const configFile = join(home, 'deployment.json'); await savePrivate(configFile, profile.deployment);
          const result = await runner(python, [STACK, 'register', '--registry', join(home, 'targets.json'), '--target-id', descriptor.target_id,
            '--config', configFile, '--state-dir', home, ...(bindingFile ? ['--binding', bindingFile] : [])],
            { timeout: 10 * 60 * 1000, mutation: true });
          if (result.status !== 'succeeded' || result.target_id !== descriptor.target_id)
            throw new EnvironmentError('DEPLOYMENT_REGISTRATION_UNVERIFIED', 502, true);
          state.binding = { status: 'succeeded' }; state.deployment_supported = true; state.blockers = [];
        }
        state.status = 'succeeded'; return state;
      } catch (error) {
        state.status = error.outcomeUnknown ? 'unknown' : error.status === 409 || error.status === 503 ? 'blocked' : 'failed';
        state[state.stage] = { status: state.status };
        state.error = { code: error instanceof EnvironmentError ? error.code : 'ENVIRONMENT_OUTCOME_UNKNOWN',
          message: '환경 단계의 결과를 확인하고 서버 실행 기록을 점검하세요.', request_id: randomUUID(),
          retryable: false, outcome_unknown: error.outcomeUnknown || !(error instanceof EnvironmentError) };
        if (state.error.outcome_unknown) { state.status = 'unknown'; state[state.stage].status = 'unknown'; }
        return state;
      }
    },
  };
}
