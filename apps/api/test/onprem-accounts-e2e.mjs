// Explicit live acceptance tool; it is not included in node --test discovery.
// Only private file paths are accepted on argv. Passwords stay in this process.
import { createHash, randomBytes, randomUUID } from 'node:crypto';
import { constants } from 'node:fs';
import { lstat, mkdir, open, rename, rm } from 'node:fs/promises';
import { isAbsolute, join } from 'node:path';
import { pathToFileURL } from 'node:url';

class AcceptanceError extends Error {
  constructor(stage, code = 'ACCEPTANCE_FAILED') { super(code); this.stage = stage; this.code = code; }
}
const ensure = (condition, stage, code) => { if (!condition) throw new AcceptanceError(stage, code); };
const sha256 = (text) => createHash('sha256').update(text).digest('hex');
const privateFailure = (stage) => new AcceptanceError(stage, 'PRIVATE_FILE_INVALID');

async function privateJson(path, stage, uid = process.getuid()) {
  let file;
  try {
    ensure(typeof path === 'string' && isAbsolute(path), stage);
    const info = await lstat(path);
    const valid = (row) => row.isFile() && row.uid === uid && row.nlink === 1
      && (row.mode & 0o7777) === 0o600 && row.size > 0 && row.size <= 131072;
    if (!valid(info)) throw privateFailure(stage);
    file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    const opened = await file.stat();
    if (!valid(opened) || opened.dev !== info.dev || opened.ino !== info.ino) throw privateFailure(stage);
    const bytes = Buffer.alloc(131073);
    let length = 0, count;
    while (length < bytes.length && (count = (await file.read(bytes, length, bytes.length - length, null)).bytesRead) !== 0) length += count;
    if (length > 131072) throw privateFailure(stage);
    return JSON.parse(bytes.subarray(0, length).toString('utf8'));
  } catch (error) {
    if (error.code === 'ENOENT') throw error;
    throw privateFailure(stage);
  } finally { await file?.close(); }
}

async function privateDirectory(path, stage, create = false) {
  ensure(typeof path === 'string' && isAbsolute(path), stage, 'PRIVATE_DIRECTORY_INVALID');
  if (create) {
    try { await mkdir(path, { mode: 0o700 }); }
    catch (error) { if (error.code !== 'EEXIST') throw new AcceptanceError(stage, 'PRIVATE_DIRECTORY_INVALID'); }
  }
  const info = await lstat(path);
  ensure(info.isDirectory() && info.uid === process.getuid() && (info.mode & 0o7777) === 0o700, stage, 'PRIVATE_DIRECTORY_INVALID');
}

async function privateWrite(path, contents) {
  const file = await open(path, 'wx', 0o600);
  try { await file.writeFile(contents); await file.sync(); }
  finally { await file.close(); }
}

async function privateReplace(path, contents) {
  const info = await lstat(path);
  ensure(info.isFile() && info.uid === process.getuid() && info.nlink === 1 && (info.mode & 0o7777) === 0o600, 'private_output', 'PRIVATE_FILE_INVALID');
  const temporary = `${path}.${randomUUID()}.tmp`;
  try { await privateWrite(temporary, contents); await rename(temporary, path); }
  finally { await rm(temporary, { force: true }); }
}

async function configuration(path) {
  const config = await privateJson(path, 'configuration');
  const required = ['api_url', 'mail_directory', 'output_directory', 'label'];
  const allowed = [...required, 'mail_uid', 'test_allow_http'];
  ensure(config && typeof config === 'object' && !Array.isArray(config) && required.every((key) => Object.hasOwn(config, key))
    && Object.keys(config).every((key) => allowed.includes(key)), 'configuration', 'INVALID_CONFIGURATION');
  let url;
  try { url = new URL(config.api_url); } catch { throw new AcceptanceError('configuration', 'INVALID_CONFIGURATION'); }
  ensure((url.protocol === 'https:' || url.protocol === 'http:' && config.test_allow_http === true)
    && !url.username && !url.password && !url.search && !url.hash && url.pathname === '/', 'configuration', 'INVALID_CONFIGURATION');
  ensure(typeof config.label === 'string' && config.label.length > 0 && config.label.length <= 80 && !/[\x00-\x1f]/.test(config.label), 'configuration', 'INVALID_CONFIGURATION');
  ensure(isAbsolute(config.mail_directory) && isAbsolute(config.output_directory), 'configuration', 'INVALID_CONFIGURATION');
  ensure(config.mail_uid === undefined || Number.isSafeInteger(config.mail_uid) && config.mail_uid >= 0, 'configuration', 'INVALID_CONFIGURATION');
  return { ...config, api_url: url.origin, mail_uid: config.mail_uid ?? process.getuid() };
}

function client(config, initial = {}) {
  const cookies = new Map();
  for (const part of (initial.cookie || '').split(';').filter(Boolean)) {
    const match = /^(railshot_account|railshot_session)=([A-Za-z0-9_-]{43}|)$/.exec(part.trim());
    ensure(match, 'account_state', 'INVALID_CONFIGURATION');
    cookies.set(match[1], match[2]);
  }
  let csrf = initial.csrf_token || '';
  const cookie = () => [...cookies].map(([name, value]) => `${name}=${value}`).join('; ');
  return {
    get privateState() { return { cookie: cookie(), csrf_token: csrf }; },
    async request(path, body, { method = body === undefined ? 'GET' : 'POST', headers = {}, csrfToken = csrf } = {}) {
      let response;
      try {
        response = await fetch(config.api_url + path, { method, redirect: 'error', signal: AbortSignal.timeout(15000),
          headers: { Cookie: cookie(), 'X-Railshot-Request': 'dashboard', ...(csrfToken ? { 'X-Railshot-CSRF': csrfToken } : {}),
            ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...headers },
          ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });
      } catch { throw new AcceptanceError('http', 'REQUEST_FAILED'); }
      const setCookies = response.headers.getSetCookie?.() || (response.headers.get('set-cookie') || '').split(/,(?=\s*railshot_)/).filter(Boolean);
      for (const setCookie of setCookies) {
        const match = /^(railshot_account|railshot_session)=([A-Za-z0-9_-]{43}|);/.exec(setCookie.trim());
        ensure(match, 'login_cookie', 'INVALID_RESPONSE');
        cookies.set(match[1], match[2]);
      }
      const text = await response.text();
      ensure(text.length <= 131072, 'http', 'INVALID_RESPONSE');
      let data = text;
      if (response.headers.get('content-type')?.includes('json')) {
        try { data = JSON.parse(text); } catch { throw new AcceptanceError('http', 'INVALID_RESPONSE'); }
      }
      if (data?.csrf_token) csrf = data.csrf_token;
      return { status: response.status, data };
    },
  };
}

async function mailbox(config, email) {
  const path = join(config.mail_directory, `${sha256(email)}-verify.json`), deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    try {
      const message = await privateJson(path, 'mail_verification', config.mail_uid);
      ensure(message.to === email && message.purpose === 'verify' && /^[A-Za-z0-9_-]{43}$/.test(message.token), 'mail_verification', 'INVALID_RESPONSE');
      return message;
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new AcceptanceError('mail_verification', 'MAIL_TIMEOUT');
}

async function account(config, checks) {
  const connection = client(config), email = `railshot-e2e-${randomUUID()}@example.test`;
  const password = randomBytes(36).toString('base64url');
  ensure((await connection.request('/api/v1/accounts', { email, password })).status === 202, 'account_registration');
  ensure((await connection.request('/api/v1/logins', { email, password })).status === 401, 'unverified_login');
  const mail = await mailbox(config, email);
  ensure((await connection.request('/api/v1/accounts/verifications', { token: mail.token })).status === 200, 'account_verification');
  ensure((await connection.request('/api/v1/accounts/verifications', { token: mail.token })).status === 401, 'verification_replay');
  const login = await connection.request('/api/v1/logins', { email, password });
  ensure(login.status === 201 && typeof login.data.account_id === 'string' && /^[A-Za-z0-9_-]{43}$/.test(login.data.csrf_token), 'account_login');
  const current = await connection.request('/api/v1/logins');
  ensure(current.status === 200 && current.data.account_id === login.data.account_id, 'current_account');
  checks.push('email_verification', 'verification_single_use', 'account_login');
  return { connection, account_id: login.data.account_id, email };
}

async function issueEnrollment(config, connection, targetId, previous, checks) {
  const start = Date.now(), enrollment = await connection.request(`/api/v1/targets/${targetId}/enrollments`, {}), end = Date.now();
  ensure(enrollment.status === 201 && /^[A-Za-z0-9_-]{43}$/.test(enrollment.data.token), 'enrollment_rotation');
  const expires = Date.parse(enrollment.data.expires_at);
  ensure(expires >= start + 1800000 - 1000 && expires <= end + 1800000 + 1000, 'enrollment_30_minutes');
  const revoked = await connection.request(`/api/v1/enrollments/${previous.id}/claims`,
    { public_key: 'A'.repeat(43) + '=', client_version: 'e2e-revocation-probe', runtime: { ssh_host_key: 'ssh-ed25519 ' + 'A'.repeat(68) } },
    { headers: { Authorization: `Bearer ${previous.token}` } });
  ensure(revoked.status === 401, 'revoked_enrollment');
  const scriptUrl = new URL(enrollment.data.script_url);
  ensure(scriptUrl.origin === config.api_url && scriptUrl.pathname === '/api/v1/installers/personal' && !scriptUrl.search
    && !scriptUrl.username && !scriptUrl.password && !scriptUrl.hash
    && !enrollment.data.script_url.includes(enrollment.data.token), 'credential_free_script_url');
  const script = await connection.request(scriptUrl.pathname);
  ensure(script.status === 200 && typeof script.data === 'string' && sha256(script.data) === enrollment.data.script_sha256
    && !script.data.includes(enrollment.data.token) && !script.data.includes(previous.token), 'pinned_script');
  checks.push('enrollment_rotation', 'enrollment_30_minutes', 'revoked_enrollment', 'credential_free_script_url', 'pinned_script');
  return { enrollment: enrollment.data, script: script.data };
}

export async function runAccountAcceptance(mode, configPath, statePath) {
  const config = await configuration(configPath), checks = [];
  await privateDirectory(config.output_directory, 'output_directory', true);
  if (mode === 'refresh') {
    ensure(!statePath, 'arguments', 'INVALID_ARGUMENTS');
    const path = join(config.output_directory, 'account-state.json');
    const state = await privateJson(path, 'account_state');
    ensure(state.version === 1 && state.api_url === config.api_url && /^personal-[0-9a-f-]{36}$/.test(state.target_id), 'account_state', 'INVALID_CONFIGURATION');
    const connection = client(config, state.primary);
    const issued = await issueEnrollment(config, connection, state.target_id, state.enrollment, checks);
    state.enrollment = issued.enrollment;
    state.primary = { ...state.primary, ...connection.privateState };
    state.refreshed_at = new Date().toISOString();
    await privateReplace(path, JSON.stringify(state, null, 2) + '\n');
    await privateReplace(join(config.output_directory, 'personal-install.sh'), issued.script);
    await privateWrite(join(config.output_directory, `enrollment-report-${Date.now()}.json`), JSON.stringify({ status: 'succeeded',
      checked_at: state.refreshed_at, target_id: state.target_id, enrollment_id: issued.enrollment.id,
      enrollment_expires_at: issued.enrollment.expires_at, checks }, null, 2) + '\n');
    return { status: 'succeeded', stage: 'enrollment', checks: checks.length };
  }
  if (mode === 'check') {
    const state = await privateJson(statePath || join(config.output_directory, 'account-state.json'), 'account_state');
    ensure(state.version === 1 && state.api_url === config.api_url && /^personal-[0-9a-f-]{36}$/.test(state.target_id), 'account_state', 'INVALID_CONFIGURATION');
    const primary = client(config, state.primary), secondary = client(config, state.secondary);
    const target = await primary.request(`/api/v1/targets/${state.target_id}`);
    ensure(target.status === 200 && target.data.connection_status === 'ready' && target.data.deployable === false, 'connected_target');
    const list = await primary.request('/api/v1/targets?scope=owned&provider=openstack');
    ensure(list.status === 200 && list.data.items.some((item) => item.id === state.target_id && item.connection_status === 'ready' && item.deployable === false), 'connected_target_list');
    ensure((await secondary.request(`/api/v1/targets/${state.target_id}`)).status === 404, 'connected_target_isolation');
    const instances = await primary.request(`/api/v1/targets/${state.target_id}/instances`);
    ensure(instances.status === 200 && Array.isArray(instances.data.items), 'openstack_server_list');
    ensure((await secondary.request(`/api/v1/targets/${state.target_id}/instances`)).status === 404, 'openstack_command_isolation');
    const replay = await primary.request(`/api/v1/enrollments/${state.enrollment.id}/claims`,
      { public_key: 'A'.repeat(43) + '=', client_version: 'e2e-replay-probe', runtime: { ssh_host_key: 'ssh-ed25519 ' + 'A'.repeat(68) } },
      { headers: { Authorization: `Bearer ${state.enrollment.token}` } });
    ensure(replay.status === 401, 'claimed_enrollment_replay');
    const report = { status: 'succeeded', checked_at: new Date().toISOString(), target_id: state.target_id,
      account_id: state.primary.account_id, connection_status: target.data.connection_status, deployable: target.data.deployable,
      server_count: instances.data.items.length,
      checks: ['connected_target', 'connected_target_list', 'connected_target_isolation', 'openstack_server_list', 'openstack_command_isolation', 'claimed_enrollment_replay'] };
    await privateWrite(join(config.output_directory, `connection-report-${Date.now()}.json`), JSON.stringify(report, null, 2) + '\n');
    return { status: 'succeeded', stage: 'connection', checks: report.checks.length };
  }
  ensure(mode === 'setup', 'arguments', 'INVALID_ARGUMENTS');
  const ready = await client(config).request('/api/v1/readiness?scope=personal&phase=connection');
  ensure(ready.status === 200 && ready.data.ready === true, 'connection_readiness');
  checks.push('connection_readiness');
  const primary = await account(config, checks), secondary = await account(config, checks);
  ensure(primary.account_id !== secondary.account_id, 'distinct_accounts');
  const a = primary.connection, b = secondary.connection;
  ensure((await a.request('/api/v1/targets', { label: config.label, provider: 'openstack' }, { csrfToken: 'invalid-csrf-fixture' })).status === 403, 'csrf_required');
  const created = await a.request('/api/v1/targets', { label: config.label, provider: 'openstack' });
  ensure(created.status === 201 && /^personal-[0-9a-f-]{36}$/.test(created.data.id), 'target_creation');
  const targetId = created.data.id;
  const list = await a.request('/api/v1/targets?scope=owned&provider=openstack');
  ensure(list.status === 200 && list.data.items.some((item) => item.id === targetId), 'owned_target_list');
  ensure((await b.request(`/api/v1/targets/${targetId}`)).status === 404, 'target_isolation');
  const otherList = await b.request('/api/v1/targets?scope=owned&provider=openstack');
  ensure(otherList.status === 200 && otherList.data.items.length === 0, 'target_list_isolation');
  ensure((await b.request(`/api/v1/targets/${targetId}/enrollments`, {})).status === 404, 'enrollment_isolation');
  checks.push('csrf_required', 'target_creation', 'owned_target_list', 'target_isolation', 'target_list_isolation', 'enrollment_isolation');
  const previous = await a.request(`/api/v1/targets/${targetId}/enrollments`, {});
  ensure(previous.status === 201, 'enrollment_creation');
  const issued = await issueEnrollment(config, a, targetId, previous.data, checks);
  const state = { version: 1, created_at: new Date().toISOString(), api_url: config.api_url, target_id: targetId,
    primary: { account_id: primary.account_id, email: primary.email, ...a.privateState },
    secondary: { account_id: secondary.account_id, email: secondary.email, ...b.privateState }, enrollment: issued.enrollment };
  const report = { status: 'succeeded', checked_at: state.created_at, account_id: primary.account_id, target_id: targetId,
    enrollment_id: issued.enrollment.id, enrollment_expires_at: issued.enrollment.expires_at, checks };
  await privateWrite(join(config.output_directory, 'account-state.json'), JSON.stringify(state, null, 2) + '\n');
  await privateWrite(join(config.output_directory, 'personal-install.sh'), issued.script);
  await privateWrite(join(config.output_directory, 'account-report.json'), JSON.stringify(report, null, 2) + '\n');
  return { status: 'succeeded', stage: 'accounts', checks: checks.length };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    ensure(process.argv.length >= 4 && process.argv.length <= 5, 'arguments', 'INVALID_ARGUMENTS');
    process.stdout.write(JSON.stringify(await runAccountAcceptance(process.argv[2], process.argv[3], process.argv[4])) + '\n');
  } catch (error) {
    process.stderr.write(JSON.stringify({ status: 'failed', stage: error instanceof AcceptanceError ? error.stage : 'tool',
      code: error instanceof AcceptanceError ? error.code : 'ACCEPTANCE_FAILED' }) + '\n');
    process.exitCode = 1;
  }
}
