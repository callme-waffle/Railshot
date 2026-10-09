import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { DatabaseSync } from 'node:sqlite';
import { createAccountData } from '../src/accounts.js';
import { createPersonalEnvironments } from '../src/personal-environments.js';
import { createAppServer } from '../src/server.js';
import { apiAccessConfig } from '../src/access.js';
import { runAccountAcceptance } from './onprem-accounts-e2e.mjs';

test('live account harness preserves private enrollment artifacts and verifies read-only command isolation', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-account-acceptance-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const mailDirectory = join(directory, 'mail'), outputDirectory = join(directory, 'output');
  await mkdir(mailDirectory, { mode: 0o700 });
  const db = new DatabaseSync(':memory:');
  db.exec(await readFile(new URL('../src/dashboard-schema.sql', import.meta.url), 'utf8'));
  t.after(() => db.close());
  const accounts = createAccountData(db, async (message) => {
    const filename = createHash('sha256').update(message.to).digest('hex') + '-verify.json';
    await writeFile(join(mailDirectory, filename), JSON.stringify({ ...message, received_at: new Date().toISOString() }), { mode: 0o600 });
  });
  const state = { applications: {}, operations: {}, plans: {}, personal: { targets: {}, enrollments: {} } };
  const store = { read: () => state, transaction: async (mutation) => mutation(state) };
  let peerMutations = 0, commands = 0, commandResult = [];
  const adapter = { config: { public_url: '', artifact_url: 'https://releases.example.test/pinned.tgz', artifact_sha256: 'a'.repeat(64) },
    connectionReadiness: async () => ({ ready: true, blockers: [] }),
    register: async () => { peerMutations++; throw new Error('A revoked token must not register a peer'); },
    execute: async () => { commands++; return { ok: true, result: commandResult }; },
  };
  const personal = createPersonalEnvironments({ store, adapter, launch: () => {}, publicApplication: () => {} });
  const product = { dashboard: { accounts, owner: () => null,
    session: (token) => ({ id: 'anonymous-test-session', ...(token ? {} : { token: 'Z'.repeat(43) }) }) }, personal };
  const server = createAppServer({ product, access: apiAccessConfig({ RAILSHOT_BIND_HOST: '0.0.0.0',
    RAILSHOT_API_TOKEN: 'x'.repeat(32), RAILSHOT_ALLOWED_HOSTS: '127.0.0.1' }) });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const base = `http://127.0.0.1:${server.address().port}`;
  adapter.config.public_url = base;
  // This fixture explicitly permits isolated HTTP in the installer generator.
  adapter.config.test_allow_http = true;
  const configPath = join(directory, 'configuration.json');
  await writeFile(configPath, JSON.stringify({ api_url: base, mail_directory: mailDirectory, output_directory: outputDirectory,
    label: 'acceptance test site', test_allow_http: true }), { mode: 0o600 });
  const tool = fileURLToPath(new URL('./onprem-accounts-e2e.mjs', import.meta.url));
  const child = spawn(process.execPath, [tool, 'setup', configPath], { stdio: ['ignore', 'pipe', 'pipe'] });
  let stdout = '', stderr = '';
  child.stdout.on('data', (data) => { stdout += data; }); child.stderr.on('data', (data) => { stderr += data; });
  const code = await new Promise((resolve, reject) => { child.on('error', reject); child.on('exit', resolve); });
  assert.equal(code, 0, stderr);
  assert.deepEqual(JSON.parse(stdout), { status: 'succeeded', stage: 'accounts', checks: 18 });
  assert.equal(stderr, ''); assert.equal(peerMutations, 0);
  const privateStatePath = join(outputDirectory, 'account-state.json');
  const saved = JSON.parse(await readFile(privateStatePath, 'utf8'));
  assert.match(saved.primary.cookie, /railshot_account=[A-Za-z0-9_-]{43}/);
  assert.match(saved.primary.cookie, /railshot_session=Z{43}/);
  assert.equal(Object.hasOwn(saved.primary, 'password'), false);
  assert.match(saved.enrollment.token, /^[A-Za-z0-9_-]{43}$/);
  for (const path of ['account-state.json', 'account-report.json', 'personal-install.sh']) {
    assert.equal((await stat(join(outputDirectory, path))).mode & 0o777, 0o600);
  }
  assert.deepEqual(await runAccountAcceptance('refresh', configPath), { status: 'succeeded', stage: 'enrollment', checks: 5 });
  const refreshed = JSON.parse(await readFile(privateStatePath, 'utf8'));
  assert.notEqual(refreshed.enrollment.id, saved.enrollment.id);
  assert.equal((await stat(privateStatePath)).mode & 0o777, 0o600);
  assert.equal(state.personal.enrollments[saved.enrollment.id].revoked, true);
  await assert.rejects(runAccountAcceptance('check', configPath), (error) => error.stage === 'connected_target');
  Object.assign(state.personal.targets[saved.target_id], { connection_status: 'ready', last_seen_at: new Date().toISOString(), status: 'connecting' });
  state.personal.enrollments[refreshed.enrollment.id].used = true;
  const checked = await runAccountAcceptance('check', configPath);
  assert.deepEqual(checked, { status: 'succeeded', stage: 'connection', checks: 6, connection_ready: true, server_count: 0 });
  const reportPath = (await readdir(outputDirectory)).find((name) => name.startsWith('connection-report-'));
  const report = JSON.parse(await readFile(join(outputDirectory, reportPath), 'utf8'));
  assert.deepEqual(report.servers, []);
  assert.equal(report.connection_ready, true);
  assert.equal(commands, 1); assert.equal(peerMutations, 0);
  const privateText = 'sensitive-runtime-string-fixture';
  commandResult = [{ ID: '123e4567-e89b-42d3-a456-426614174000', Status: 'ACTIVE', Name: privateText, Networks: privateText },
    { ID: privateText, Status: privateText }];
  assert.equal((await runAccountAcceptance('check', configPath)).server_count, 2);
  const reports = (await readdir(outputDirectory)).filter((name) => name.startsWith('connection-report-'));
  for (const filename of reports) assert.equal((await readFile(join(outputDirectory, filename), 'utf8')).includes(privateText), false);
  assert.equal(commands, 2); assert.equal(peerMutations, 0);
});
