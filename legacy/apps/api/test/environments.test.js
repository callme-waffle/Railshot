import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { lstat, mkdir, mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createEnvironmentAdapter, EnvironmentError, runEnvironmentCommand } from '../src/environments.js';

const DIGEST = 'a'.repeat(64);
const input = { name: 'demo-runtime', runtime: { profile_id: 'aws-small', node_count: 1 }, database: { mode: 'none' } };
const descriptor = { schema_version: 'v1', provider_kind: 'aws', execution_driver: 'terraform',
  target_id: 'demo-runtime', architecture: 'x86_64', resource_id: 'arn:aws:ec2:ap-northeast-2:123456789012:instance/i-12345678',
  location: { region: 'ap-northeast-2' }, addresses: { private: '10.0.1.10' }, transport_ref: 'ssm:ap-northeast-2:i-12345678' };

async function fixture(t, { replaceRunner, modify, now } = {}) {
  const home = await realpath(await mkdtemp(join(tmpdir(), 'railshot-environments-')));
  t.after(() => rm(home, { recursive: true, force: true }));
  const config = { version: 1, policy_revision: 'revision1', profiles: [{ id: 'aws-small', label: 'AWS demo',
    provider: 'aws', site: 'ap-northeast-2', purposes: ['runtime'], allow_apply: true,
    target_file: join(home, 'target.json'), state_root: join(home, 'terraform'),
    ssh: { user: 'ubuntu', identity_file: join(home, 'identity'), known_hosts_file: join(home, 'knownhosts') } }] };
  const profile = config.profiles[0];
  await writeFile(profile.target_file, JSON.stringify({ schema_version: 'v1', target_id: 'demo-runtime',
    provider_kind: 'aws', variables: { target_id: 'demo-runtime' }, budget: { ledger_path: 'private' } }), { mode: 0o600 });
  for (const path of [profile.ssh.identity_file, profile.ssh.known_hosts_file]) await writeFile(path, 'private', { mode: 0o600 });
  await modify?.(config, home);
  const profilesFile = join(home, 'profiles.json');
  await writeFile(profilesFile, JSON.stringify(config), { mode: 0o600 });
  const calls = [];
  async function fakeRunner(python, args, options) {
    calls.push({ args, options });
    if (replaceRunner) return replaceRunner(python, args, options, defaultRunner);
    return defaultRunner(python, args, options);
  }
  async function defaultRunner(python, args, options) {
    if (args[1] === 'plan') {
      const target = JSON.parse(await readFile(args[args.indexOf('--target') + 1], 'utf8'));
      await mkdir(join(profile.state_root, target.target_id), { recursive: true, mode: 0o700 });
      await writeFile(join(profile.state_root, target.target_id, 'plan-manifest.json'), JSON.stringify({ plan_sha256: DIGEST, apply_attempted: false }), { mode: 0o600 });
      return { plan_sha256: DIGEST, target_id: target.target_id, destructive: false,
        maintenance_required: false, changes: [{ address: 'aws_instance.node', actions: ['create'] }] };
    }
    if (args[1] === 'apply') return { apply_status: 'completed', plan_sha256: DIGEST, node_descriptor: descriptor };
    if (args.includes('--validate-only')) return runEnvironmentCommand(python, args, options); // Real checked-in descriptor/schema adapter; no SSH.
    const operation = args[args.indexOf('--operation') + 1];
    return { status: 'succeeded', target_id: 'demo-runtime', request_id: args[args.indexOf('--request-id') + 1],
      operation, guest_ready: true, runtime_ready: operation === 'runtime.install' };
  }
  const adapter = await createEnvironmentAdapter({ profilesFile, stateDir: join(home, 'environments'), runner: fakeRunner, now });
  return { adapter, home, profilesFile, config, calls };
}

test('registered profile → saved plan → descriptor snapshot → native validation → guest/runtime receipts', async (t) => {
  const { adapter, home, calls } = await fixture(t);
  assert.deepEqual(Object.keys(adapter.profiles()[0]).sort(), ['application_name', 'blockers', 'create_per_request', 'database', 'deployment_supported', 'id', 'label', 'provider', 'purposes', 'site', 'supported', 'target_id']);
  assert.equal(adapter.profiles()[0].create_per_request, false);
  const plan = await adapter.plan(input, { id: 'plan1' });
  assert.equal(plan.public.executable, true);
  assert.equal(plan.public.plan_sha256, DIGEST);
  assert.equal(plan.public.runtime_target_id, 'demo-runtime');
  assert.equal(JSON.stringify(plan.public).includes(home), false);
  const progress = [];
  const result = await adapter.execute(plan, { id: 'environment1', onProgress: async (patch) => progress.push(patch) });
  assert.equal(result.status, 'succeeded');
  assert.equal(result.resources.status, 'succeeded');
  assert.equal(result.guest.status, 'succeeded');
  assert.equal(result.runtime.status, 'succeeded');
  assert.equal(result.runtime_target_id, descriptor.target_id);
  assert.equal(result.deployment_supported, false);
  assert.deepEqual(result.blockers, ['DEPLOYMENT_TARGET_NOT_REGISTERED']);
  assert.equal(result.database.status, 'skipped');
  assert.equal(progress[0].resources.status, 'running');
  assert.equal(JSON.stringify(result).includes(home), false);
  assert.deepEqual(calls.filter((call) => call.args[1] === 'apply')[0].args.slice(-2), ['--plan-sha256', DIGEST]);
  const registry = JSON.parse(await readFile(join(home, 'environments/environment1/targets.json'), 'utf8'));
  assert.equal(registry.targets['demo-runtime'].purpose, 'runtime');
  await assert.rejects(adapter.execute(plan, { id: 'environment1' }), { code: 'EEXIST' });
  assert.equal(calls.filter((call) => call.args[1] === 'apply').length, 1);
});

async function creationTemplate(t, { provider = 'aws', modify, now, replaceRunner } = {}) {
  return fixture(t, { now, replaceRunner, modify: async (config, home) => {
    const profile = config.profiles[0];
    profile.provider = provider;
    profile.create_per_request = true;
    profile.purposes.push('database');
    profile.database = { nodes: [] };
    profile.deployment_file = join(home, 'deployment.json');
    const targetId = 'approved-runtime-template-long-name';
    const target = { schema_version: 'v1', provider_kind: provider, target_id: targetId,
      variables: { target_id: targetId, name: targetId, security_group_ids: ['sg-approved'], instance_type: 't3.small', ingress_ports: [22, 443] },
      cloud_credentials_file: '/operator/approved-credentials', budget: { ledger_path: '/operator/budget', max_hourly_usd: 0.1 } };
    await writeFile(profile.target_file, JSON.stringify(target));
    const deployment = { version: 1, registration: { expires_at: '2030-01-01T00:00:00Z' }, cd: { targets: { [targetId]: {
      app: 'template-app', tenant: 'demo', target: { id: targetId, namespace: 'app-template-app', project: 'railshot-template',
        path: `gitops/applications/template-app/${targetId}`, node_port: 30080,
        image_pull_secret: { namespace: 'app-template-app', name: 'ghcr-pull' },
        database: { runtime_secret: 'runtime-db', migration_secret: 'migration-db', ca_secret: 'database-ca' } },
    } } } };
    await writeFile(profile.deployment_file, JSON.stringify(deployment), { mode: 0o600 });
    for (const [index, roles] of [['database', 'dcs'], ['database', 'dcs'], ['proxy', 'dcs']].entries()) {
      const file = join(home, `db${index}.json`);
      await writeFile(file, JSON.stringify({ ...target, target_id: `db-${index}`, profile: { kind: 'database_cluster' },
        variables: { ...target.variables, target_id: `db-${index}`, name: `db-${index}`, purpose: 'database' } }), { mode: 0o600 });
      profile.database.nodes.push({ target_file: file, roles });
    }
    await modify?.(profile, deployment, config, home);
    await writeFile(profile.deployment_file, JSON.stringify(deployment));
  } });
}

const freshInput = { ...input, name: 'my-new-app', database: { mode: 'patroni',
  placements: [{ profile_id: 'aws-small', database_nodes: 2, dcs_voters: 3, proxy_nodes: 1 }] } };

test('creation templates derive isolated runtime, DB and GitOps targets while retaining original policy snapshots', async (t) => {
  for (const provider of ['aws', 'gcp']) {
    const { adapter, config, calls } = await creationTemplate(t, { provider });
    const originalProfile = config.profiles[0];
    const originalTarget = await readFile(originalProfile.target_file, 'utf8');
    const originalDeployment = await readFile(originalProfile.deployment_file, 'utf8');
    assert.equal(adapter.profiles()[0].create_per_request, true);
    assert.equal(adapter.profiles()[0].application_name, null);
    const plan = await adapter.plan(freshInput, { id: 'fresh-first' });
    const second = await adapter.plan({ ...freshInput, name: 'another-app' }, { id: 'fresh-second' });
    assert.equal(plan.public.executable, true);
    assert.equal(second.public.executable, true);
    const prefixLength = provider === 'gcp' ? 11 : 24;
    const id = `${JSON.parse(originalTarget).target_id.slice(0, prefixLength).replace(/-+$/, '')}-${createHash('sha256').update('fresh-first').digest('hex').slice(0, 8)}`;
    assert.equal(plan.public.runtime_target_id, id);
    assert.notEqual(second.public.runtime_target_id, id);
    assert.equal(plan.private.profile.target.target_id, id);
    assert.equal(plan.private.nodes[0].profile.target.target_id, JSON.parse(originalTarget).target_id);
    for (const [index, node] of plan.private.nodes.entries()) {
      const targetId = index ? `${id}-db${index}` : id;
      assert.equal(node.target.target_id, targetId);
      assert.equal(node.target.variables.target_id, targetId);
      assert.equal(node.target.variables.name, targetId);
      assert.ok(targetId.length <= (provider === 'gcp' ? 25 : 40));
      const original = index ? node.profile.database.nodes[index - 1].target : node.profile.target;
      assert.deepEqual(node.target, { ...original, target_id: targetId, variables: { ...original.variables, target_id: targetId, name: targetId } });
    }
    const entry = plan.private.profile.deployment.cd.targets[id];
    assert.equal(entry.app, freshInput.name);
    assert.equal(entry.target.id, id);
    assert.equal(entry.target.namespace, `app-${freshInput.name}`);
    assert.equal(entry.target.project, `railshot-${id}`);
    assert.equal(entry.target.path, `gitops/applications/${freshInput.name}/${id}`);
    assert.equal(entry.target.image_pull_secret.namespace, entry.target.namespace);
    assert.equal(entry.target.image_pull_secret.name, 'ghcr-pull');
    assert.equal(entry.target.node_port, 30080);
    assert.equal(plan.private.profile.deployment.registration.expires_at, '2030-01-01T00:00:00Z');
    assert.deepEqual(Object.keys(plan.private.profile.deployment.cd.targets), [id]);
    assert.equal(await readFile(originalProfile.target_file, 'utf8'), originalTarget);
    assert.equal(await readFile(originalProfile.deployment_file, 'utf8'), originalDeployment);
    await adapter.verifyPlan(plan); await adapter.verifyPlan(second);
    assert.equal(calls.length, 8);
  }
});

test('creation templates reject invalid apps, incompatible GitOps paths and missing required DB before cloud planning', async (t) => {
  const { adapter, calls } = await creationTemplate(t);
  for (const name of ['ABadName', '../escape', 'aa', 'app-', 'a'.repeat(31)])
    await assert.rejects(adapter.plan({ ...freshInput, name }, { id: 'invalid-app' }), { code: 'INVALID_PLAN_REQUEST' });
  const noDatabase = await adapter.plan({ ...freshInput, database: { mode: 'none' } }, { id: 'missing-db' });
  assert.ok(noDatabase.public.blockers.includes('DATABASE_BINDING_PROFILE_MISMATCH'));
  assert.equal(calls.length, 0);
  for (const path of ['gitops/applications/wrong-app/approved-runtime-template-long-name', 'gitops/applications/template-app/wrong-id', '../template-app/approved-runtime-template-long-name']) {
    const broken = await creationTemplate(t, { modify: (profile, deployment) => { Object.values(deployment.cd.targets)[0].target.path = path; } });
    const plan = await broken.adapter.plan(freshInput, { id: 'invalid-path' });
    assert.ok(plan.public.blockers.includes('DEPLOYMENT_PROFILE_MISMATCH'));
    assert.equal(broken.calls.length, 0);
  }
});

test('derived plans bind private and public targets and reject current template policy changes', async (t) => {
  const { adapter, calls, config, profilesFile } = await creationTemplate(t);
  const plan = await adapter.plan(freshInput, { id: 'policy-plan' });
  await adapter.verifyPlan(plan);
  await assert.rejects(adapter.verifyPlan({ ...plan, public: { ...plan.public, runtime_target_id: 'another-target' } }), { code: 'PLAN_SNAPSHOT_CHANGED' });
  const changed = structuredClone(plan); changed.private.profile.deployment.cd.targets[plan.public.runtime_target_id].app = 'another-app';
  await assert.rejects(adapter.verifyPlan(changed), { code: 'PLAN_SNAPSHOT_CHANGED' });
  config.profiles[0].create_per_request = false;
  await writeFile(profilesFile, JSON.stringify(config));
  await assert.rejects(adapter.execute(plan, { id: 'policy-execution' }), { code: 'PROFILE_POLICY_CHANGED' });
  assert.ok(calls.every((call) => call.args[1] === 'plan'));
});

test('per-request registration expiry is frozen once in the private plan and rechecked against original policy', async (t) => {
  let time = Date.parse('2026-10-02T10:00:00Z');
  const { adapter, config, profilesFile } = await creationTemplate(t, { now: () => time,
    modify: (profile, deployment) => { profile.registration_max_age_seconds = 3600; deployment.registration.expires_at = null; } });
  const plan = await adapter.plan(freshInput, { id: 'expiry-plan' });
  const expiry = '2026-10-02T11:00:00.000Z';
  assert.equal(plan.private.profile.deployment.registration.expires_at, expiry);
  assert.equal(plan.private.nodes[0].profile.deployment.registration.expires_at, null);
  assert.equal(JSON.stringify(plan.public).includes(expiry), false);
  time += 60000;
  await adapter.verifyPlan(plan); await adapter.verifyPlan(plan);
  assert.equal(plan.private.profile.deployment.registration.expires_at, expiry);
  config.profiles[0].registration_max_age_seconds = 7200;
  await writeFile(profilesFile, JSON.stringify(config));
  await assert.rejects(adapter.verifyPlan(plan), { code: 'PROFILE_POLICY_CHANGED' });
});

test('creation and registration lifetime policies require explicit booleans and bounded operator-approved ages', async (t) => {
  await assert.rejects(fixture(t, { modify: (config) => { config.profiles[0].create_per_request = 'true'; } }), { code: 'ENVIRONMENT_CONFIGURATION_INVALID' });
  for (const age of [1799, 604801, 3600.5, '3600'])
    await assert.rejects(creationTemplate(t, { modify: (profile) => { profile.registration_max_age_seconds = age; } }), { code: 'ENVIRONMENT_CONFIGURATION_INVALID' });
  await assert.rejects(creationTemplate(t, { modify: (profile) => { profile.registration_max_age_seconds = 3600; profile.create_per_request = false; } }), { code: 'ENVIRONMENT_CONFIGURATION_INVALID' });
  await assert.rejects(fixture(t, { modify: (config) => { config.profiles[0].create_per_request = true; config.profiles[0].registration_max_age_seconds = 3600; } }), { code: 'ENVIRONMENT_CONFIGURATION_INVALID' });
});

async function refreshedBudget(t, { editOutput, editConfig, onPlan, horizon = 24 } = {}) {
  let time = Date.parse('2026-10-02T10:00:00Z');
  const data = await creationTemplate(t, { now: () => time,
    modify: async (profile, deployment, config, home) => {
      profile.budget_refresh = horizon === null ? {} : { retained_storage_hours: horizon };
      profile.registration_max_age_seconds = 3600;
      for (const file of [profile.target_file, ...profile.database.nodes.map((node) => node.target_file)]) {
        const target = JSON.parse(await readFile(file, 'utf8'));
        target.budget = { ledger_path: join(home, 'budget.db'), scope: 'old-scope', currency: 'USD',
          incremental_cost: '1', limit: '30', unreported_cost: '2', quoted_at: '2026-10-01T00:00:00Z', expires_at: '2026-10-01T02:00:00Z' };
        await writeFile(file, JSON.stringify(target));
      }
      await editConfig?.(profile, config, home);
    },
    replaceRunner: async (python, args, options, run) => {
      if (!args[0].endsWith('/budget.py')) { if (args[1] === 'plan') { time += 120000; await onPlan?.(args); } return run(python, args, options); }
      assert.deepEqual(options, { timeout: 120000 });
      const inputFile = args[args.indexOf('--input') + 1];
      assert.equal((await lstat(inputFile)).mode & 0o777, 0o600);
      const request = JSON.parse(await readFile(inputFile, 'utf8'));
      assert.equal(request.version, 1); assert.equal(request.targets.length, 4);
      assert.equal(request.retained_storage_hours, horizon === null ? undefined : horizon);
      const result = { version: 1, targets: request.targets.map((target) => ({ ...target, budget: { ...target.budget,
        scope: 'aws:approved:2026-10', incremental_cost: '1.25', quoted_at: new Date(time).toISOString(), expires_at: '2026-10-02T10:20:00Z' } })),
      summary: { currency: 'USD', incremental_estimate: '5.00', projected_total: '12.00', limit: '30', within_budget: true }, evidence: [] };
      await editOutput?.(result);
      await writeFile(args[args.indexOf('--output') + 1], JSON.stringify(result), { mode: 0o600 });
      return result.summary;
    } });
  return { ...data, setTime: (value) => { time = Date.parse(value); } };
}

test('one budget refresh precedes all derived target plans, preserves policy, and caps a ready plan at quote expiry', async (t) => {
  const { adapter, calls, home, config, profilesFile, setTime } = await refreshedBudget(t);
  const plan = await adapter.plan(freshInput, { id: 'fresh-budget' });
  assert.equal(plan.public.executable, true);
  assert.deepEqual(plan.public.cost, { currency: 'USD', incremental_estimate: '5.00', projected_total: '12.00', limit: '30', within_budget: true });
  assert.equal(plan.public.expires_at, '2026-10-02T10:20:00.000Z');
  assert.equal(plan.private.profile.deployment.registration.expires_at, '2026-10-02T11:08:00.000Z', 'registration lifetime starts after all four native plans finish');
  assert.ok(calls[0].args[0].endsWith('/budget.py'));
  assert.equal(calls.filter((call) => call.args[0].endsWith('/budget.py')).length, 1);
  assert.equal(calls.filter((call) => call.args[1] === 'plan').length, 4);
  assert.equal(JSON.stringify(plan.public).includes(home), false);
  for (const node of plan.private.nodes) {
    assert.equal(node.target.budget.incremental_cost, '1.25');
    assert.equal(node.target.budget.limit, '30');
    assert.equal(node.target.budget.unreported_cost, '2');
    assert.equal(node.profile.target.budget.scope, 'old-scope');
    assert.equal(JSON.parse(await readFile(node.target_file, 'utf8')).budget.scope, 'aws:approved:2026-10');
  }
  assert.equal(plan.private.profile.target.budget.scope, 'aws:approved:2026-10');
  await adapter.verifyPlan(plan); await adapter.verifyPlan(plan);
  assert.equal(calls.length, 5, 'rechecking a saved plan never recollects or refreshes expiry');
  setTime('2026-10-02T10:20:00Z');
  await assert.rejects(adapter.verifyPlan(plan), { code: 'PLAN_EXPIRED' });
  setTime('2026-10-02T10:09:00Z');
  config.profiles[0].budget_refresh = {};
  await writeFile(profilesFile, JSON.stringify(config));
  await assert.rejects(adapter.verifyPlan(plan), { code: 'PROFILE_POLICY_CHANGED' });
});

test('budget overage, collection failure, expired quote, and changed identity or policy stop before Terraform', async (t) => {
  const cases = [
    [(result) => { result.summary.within_budget = false; result.summary.projected_total = '31'; }, 'INFRA_BUDGET_BLOCKED'],
    [() => { throw new EnvironmentError('AWS_BUDGET_REFRESH_FAILED', 409); }, 'AWS_BUDGET_REFRESH_FAILED'],
    [(result) => { result.targets[0].variables.instance_type = 't3.large'; }, 'BUDGET_REFRESH_INVALID'],
    [(result) => { result.targets[0].budget.limit = '999'; }, 'BUDGET_REFRESH_INVALID'],
    [(result) => { result.targets[0].budget.unreported_cost = '0'; }, 'BUDGET_REFRESH_INVALID'],
    [(result) => { result.targets[0].budget.ledger_path = '/another/ledger'; }, 'BUDGET_REFRESH_INVALID'],
    [(result) => { result.targets[0].budget.expires_at = '2026-10-02T09:00:00Z'; }, 'BUDGET_REFRESH_INVALID'],
    [(result) => { result.targets.reverse(); }, 'BUDGET_REFRESH_INVALID'],
    [(result) => { result.targets.pop(); }, 'BUDGET_REFRESH_INVALID'],
    [(result) => { result.summary.incremental_estimate = 'NaN'; }, 'BUDGET_REFRESH_INVALID'],
  ];
  for (const [editOutput, code] of cases) {
    const { adapter, calls } = await refreshedBudget(t, { editOutput });
    const plan = await adapter.plan(freshInput, { id: 'bad-budget' });
    assert.equal(plan.public.executable, false);
    assert.ok(plan.public.blockers.includes(code), JSON.stringify(plan.public.blockers));
    assert.equal(calls.length, 1);
    await assert.rejects(adapter.verifyPlan(plan), { code: 'PLAN_NOT_EXECUTABLE' });
  }
});

test('omitted storage horizon is forwarded unchanged and a quote expiring during native plans cannot execute', async (t) => {
  const { adapter } = await refreshedBudget(t, { horizon: null,
    editOutput: (result) => { for (const target of result.targets) target.budget.expires_at = '2026-10-02T10:05:00Z'; } });
  const plan = await adapter.plan(freshInput, { id: 'quote-ended' });
  assert.equal(plan.public.executable, false);
  assert.ok(plan.public.blockers.includes('BUDGET_QUOTE_EXPIRED'));
  assert.equal(plan.public.expires_at, '2026-10-02T10:05:00.000Z');
  await assert.rejects(adapter.execute(plan, { id: 'expired-budget-run' }), { code: 'PLAN_NOT_EXECUTABLE' });
});

test('budget refresh requires an AWS policy with the same settings across every selected profile', async (t) => {
  for (const policy of [null, true, { retained_storage_hours: 23 }, { retained_storage_hours: 25 }, { retained_storage_hours: '24' }, { unreported_cost: 0 }])
    await assert.rejects(fixture(t, { modify: (config) => { config.profiles[0].budget_refresh = policy; } }), { code: 'ENVIRONMENT_CONFIGURATION_INVALID' });
  await assert.rejects(creationTemplate(t, { provider: 'gcp', modify: (profile) => { profile.budget_refresh = {}; } }), { code: 'ENVIRONMENT_CONFIGURATION_INVALID' });
  for (const databasePolicy of [undefined, {}]) {
    const { adapter, calls } = await refreshedBudget(t, { editConfig: async (profile, config, home) => {
      const other = structuredClone(profile);
      other.id = 'other-profile'; other.target_file = join(home, 'other-target.json');
      if (databasePolicy === undefined) delete other.budget_refresh; else other.budget_refresh = databasePolicy;
      delete other.deployment_file; delete other.registration_max_age_seconds;
      const target = JSON.parse(await readFile(profile.target_file, 'utf8'));
      target.target_id = 'other-runtime'; target.variables.target_id = 'other-runtime';
      await writeFile(other.target_file, JSON.stringify(target), { mode: 0o600 });
      delete profile.database;
      config.profiles.push(other);
    } });
    const plan = await adapter.plan({ ...freshInput, database: { mode: 'patroni', placements: [{ ...freshInput.database.placements[0], profile_id: 'other-profile' }] } }, { id: 'mixed-budget' });
    assert.ok(plan.public.blockers.includes('BUDGET_REFRESH_POLICY_MISMATCH'));
    assert.equal(calls.length, 0);
  }
});

test('unsupported database/multinode requests are saved as blocked plans without cloud calls', async (t) => {
  const { adapter, calls } = await fixture(t);
  const plan = await adapter.plan({ ...input, runtime: { ...input.runtime, node_count: 2 }, database: { mode: 'patroni' } }, { id: 'plan2' });
  assert.equal(plan.public.executable, false);
  assert.deepEqual(plan.public.blockers, ['SINGLE_NODE_ONLY', 'DATABASE_PLACEMENT_REQUIRED', 'DATABASE_TOPOLOGY_UNSUPPORTED']);
  assert.equal(calls.length, 0);
  await assert.rejects(adapter.verifyPlan(plan), { code: 'PLAN_NOT_EXECUTABLE' });
});

test('client paths/credentials, unknown profiles and extra intent fields never reach the executor', async (t) => {
  const { adapter, calls } = await fixture(t);
  for (const bad of [
    { ...input, ssh: { identity_file: '/tmp/secret' } },
    { ...input, runtime: { ...input.runtime, target_file: '/tmp/target' } },
    { ...input, database: { mode: 'none', password: 'secret' } },
  ]) await assert.rejects(adapter.plan(bad, { id: 'bad' }), { code: 'INVALID_PLAN_REQUEST' });
  await assert.rejects(adapter.plan({ ...input, runtime: { profile_id: 'unknown', node_count: 1 } }, { id: 'bad' }), { code: 'PROFILE_NOT_REGISTERED' });
  assert.equal(calls.length, 0);
});

test('policy revocation, expired plans and changed snapshots reject before apply', async (t) => {
  const { adapter, profilesFile, config, calls } = await fixture(t);
  const plan = await adapter.plan({ database: input.database, runtime: input.runtime, name: input.name }, { id: 'plan3' });
  await adapter.verifyPlan(plan); // Input object key order is irrelevant.
  await assert.rejects(adapter.verifyPlan({ ...plan, public: { ...plan.public, expires_at: '2000-01-01' } }), { code: 'PLAN_EXPIRED' });
  await assert.rejects(adapter.verifyPlan({ ...plan, public: { ...plan.public, runtime: { ...input.runtime, node_count: 2 } } }), { code: 'PLAN_SNAPSHOT_CHANGED' });
  config.profiles[0].allow_apply = false;
  await writeFile(profilesFile, JSON.stringify(config));
  await assert.rejects(adapter.execute(plan, { id: 'environment3' }), { code: 'PROFILE_POLICY_CHANGED' });
  assert.equal(calls.some((call) => call.args[1] === 'apply'), false);
});

test('a superseding native plan or previously attempted apply is rejected before admission', async (t) => {
  const { adapter, config } = await fixture(t);
  const plan = await adapter.plan(input, { id: 'replaced' });
  const manifest = join(config.profiles[0].state_root, 'demo-runtime/plan-manifest.json');
  await writeFile(manifest, JSON.stringify({ plan_sha256: 'b'.repeat(64), apply_attempted: false }));
  await assert.rejects(adapter.verifyPlan(plan), { code: 'SAVED_PLAN_REPLACED' });
  await writeFile(manifest, JSON.stringify({ plan_sha256: DIGEST, apply_attempted: true }));
  await assert.rejects(adapter.verifyPlan(plan), { code: 'PLAN_ALREADY_EXECUTED' });
});

test('public creation cannot authorize destruction or modification of existing resources', async (t) => {
  const { adapter } = await fixture(t, { replaceRunner: async () => ({ plan_sha256: DIGEST, target_id: 'demo-runtime',
    changes: [{ address: 'aws_instance.node', actions: ['update'] }], destructive: false, maintenance_required: false }) });
  const plan = await adapter.plan(input, { id: 'plan4' });
  assert.equal(plan.public.executable, false);
  assert.ok(plan.public.blockers.includes('EXISTING_RESOURCE_CHANGE_REQUIRES_OPERATOR'));
});

test('uncertain apply is retained as unknown and runtime dispatch never starts', async (t) => {
  const { adapter, calls } = await fixture(t, { replaceRunner: async (python, args, options, run) => {
    if (args[1] === 'apply') throw new EnvironmentError('INFRA_EXECUTION_FAILED', 502, true);
    return run(python, args, options);
  } });
  const plan = await adapter.plan(input, { id: 'plan5' });
  const result = await adapter.execute(plan, { id: 'environment5' });
  assert.equal(result.status, 'unknown');
  assert.equal(result.error.outcome_unknown, true);
  assert.equal(result.error.retryable, false);
  assert.equal(calls.length, 2);
});

test('runtime readiness requires the exact environment request and target identity', async (t) => {
  const { adapter } = await fixture(t, { replaceRunner: async (python, args, options, run) => {
    const result = await run(python, args, options);
    if (args.includes('runtime.install')) result.request_id = 'another-environment';
    return result;
  } });
  const result = await adapter.execute(await adapter.plan(input, { id: 'plan6' }), { id: 'environment6' });
  assert.equal(result.resources.status, 'succeeded');
  assert.equal(result.guest.status, 'succeeded');
  assert.equal(result.runtime.status, 'unknown');
  assert.equal(result.status, 'unknown');
  assert.equal(result.error.code, 'READINESS_RESULT_INVALID');
});

test('Patroni request plans every new VM, installs only runtime on app VM, binds DB without exposing credentials', async (t) => {
  const { home, config, profilesFile } = await fixture(t);
  const profile = config.profiles[0];
  profile.purposes.push('database'); profile.database = { nodes: [] };
  profile.deployment_file = join(home, 'deployment.json');
  await writeFile(profile.deployment_file, JSON.stringify({ version: 1, cd: { targets: { 'demo-runtime': { app: input.name, tenant: 'demo', target: { id: 'demo-runtime', database: { runtime_secret: 'runtime-db', migration_secret: 'migration-db', ca_secret: 'database-ca' } } } } } }), { mode: 0o600 });
  for (const [index, roles] of [['database', 'dcs'], ['database', 'dcs'], ['proxy', 'dcs']].entries()) {
    const file = join(home, `db${index}.json`);
    await writeFile(file, JSON.stringify({ schema_version: 'v1', target_id: `db-${index}`, provider_kind: 'aws',
      profile: { kind: 'database_cluster' }, variables: { target_id: `db-${index}`, purpose: 'database' } }), { mode: 0o600 });
    profile.database.nodes.push({ target_file: file, roles });
  }
  await writeFile(profilesFile, JSON.stringify(config));
  const calls = [];
  const runner = async (python, args) => {
    calls.push(args);
    if (['plan', 'apply'].includes(args[1])) {
      const target = JSON.parse(await readFile(args[args.indexOf('--target') + 1], 'utf8'));
      const sha = target.target_id === 'demo-runtime' ? DIGEST : target.target_id.slice(-1).repeat(64);
      if (args[1] === 'plan') {
        const dir = join(profile.state_root, target.target_id); await mkdir(dir, { recursive: true });
        await writeFile(join(dir, 'plan-manifest.json'), JSON.stringify({ plan_sha256: sha, apply_attempted: false }), { mode: 0o600 });
        return { target_id: target.target_id, plan_sha256: sha, changes: [{ actions: ['create'] }] };
      }
      return { apply_status: 'completed', plan_sha256: sha, node_descriptor: { ...descriptor, target_id: target.target_id } };
    }
    if (args[0].endsWith('/cluster.py')) {
      const spec = JSON.parse(await readFile(args[args.indexOf('--spec-file') + 1], 'utf8'));
      assert.equal(spec.nodes.length, 3);
      assert.deepEqual(spec.client_cidrs, ['10.0.1.10/32']);
      const file = join(home, 'environments', 'combined', 'binding.json');
      const raw = JSON.stringify({ password: 'never-in-status' });
      await writeFile(file, raw, { mode: 0o600 });
      const { createHash } = await import('node:crypto');
      return { status: 'succeeded', database_ready: true, request_id: spec.request_id,
        binding_file: file, binding_sha256: createHash('sha256').update(raw).digest('hex') };
    }
    if (args[0].endsWith('/environment.py')) return { status: 'succeeded', target_id: 'demo-runtime' };
    if (args.includes('--validate-only')) return { status: 'validated', target_id: 'demo-runtime' };
    const operation = args[args.indexOf('--operation') + 1];
    return { status: 'succeeded', target_id: 'demo-runtime', request_id: args[args.indexOf('--request-id') + 1], operation, guest_ready: true, runtime_ready: true };
  };
  const adapter = await createEnvironmentAdapter({ profilesFile, stateDir: join(home, 'environments'), runner });
  const noDatabase = await adapter.plan(input, { id: 'missing-database' });
  assert.equal(noDatabase.public.executable, false);
  assert.deepEqual(noDatabase.public.blockers, ['DATABASE_BINDING_PROFILE_MISMATCH']);
  assert.equal(calls.length, 0);
  const plan = await adapter.plan({ ...input, database: { mode: 'patroni', placements: [{ profile_id: profile.id, database_nodes: 2, dcs_voters: 3, proxy_nodes: 1 }] } }, { id: 'all-nodes' });
  assert.equal(plan.public.executable, true); assert.equal(calls.length, 4);
  assert.ok(plan.public.steps.includes('database'));
  const result = await adapter.execute(plan, { id: 'combined' });
  assert.equal(result.status, 'succeeded'); assert.equal(result.database.status, 'succeeded');
  assert.equal(result.deployment_supported, true);
  assert.equal(calls.filter((args) => args.includes('runtime.install')).length, 1);
  assert.ok(!JSON.stringify(result).includes('never-in-status'));
  assert.ok(!JSON.stringify(result).includes(home));
});
