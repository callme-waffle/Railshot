import { join } from 'node:path';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { readFile, lstat, writeFile, unlink } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { request as httpRequest } from 'node:http';
import { privateJson, privateDirectory, savePrivate, runEnvironmentCommand } from './environments.js';
import { createApplicationAdapter } from './applications.js';

const run = promisify(execFile);
const gatewayScript = fileURLToPath(new URL('../../../deployment/scripts/personal_wireguard.py', import.meta.url));
const runtimeScript = fileURLToPath(new URL('../../../deployment/scripts/personal_runtime.py', import.meta.url));
const MAX_RESPONSE = 1024 * 1024;

function sshTransport(executable, args, { input, timeout = 60000 }) {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, args, { detached: true, stdio: ['pipe', 'pipe', 'pipe'] });
    let output = '', invalid = false;
    const stop = () => { invalid = true; if (child.pid) { try { process.kill(-child.pid, 'SIGKILL'); } catch {} } };
    const timer = setTimeout(stop, timeout);
    child.stdout.on('data', (chunk) => { output += chunk.toString(); if (Buffer.byteLength(output) > MAX_RESPONSE) stop(); });
    child.stderr.resume(); // Provider stderr can contain local auth details. Never expose it.
    child.stdin.on('error', () => { invalid = true; });
    child.on('error', () => { invalid = true; });
    child.on('close', (code) => { clearTimeout(timer); if (invalid || ![0, 1].includes(code)) reject(new Error('OpenStack transport outcome unknown')); else resolve({ stdout: output, code }); });
    child.stdin.end(input);
  });
}

/** Private server adapter. Browser requests cannot choose executables, hosts, paths or credentials. */
export async function createPersonalAdapter({ configPath, stateDirectory, base, service, python = 'python3', runner = runEnvironmentCommand, transport = sshTransport, env = process.env, applicationFactory = createApplicationAdapter }) {
  const config = await privateJson(configPath);
  if (config.version !== 1 || !config.gateway?.config_path?.startsWith('/')) throw new Error('Invalid personal configuration');
  const testHttp = env.RAILSHOT_PERSONAL_TEST_ALLOW_HTTP;
  if (testHttp !== undefined && !['0', '1'].includes(testHttp)) throw new Error('RAILSHOT_PERSONAL_TEST_ALLOW_HTTP must be 0 or 1');
  // Test transport is an operator process setting, never a browser input or a URL-derived default.
  config.test_allow_http = testHttp === '1';
  for (const key of ['public_url', 'installer_url', 'artifact_url']) {
    const url = new URL(config[key]);
    if (!(url.protocol === 'https:' || config.test_allow_http && url.protocol === 'http:')
        || !url.hostname || url.username || url.password || url.hash || url.search || /\s/.test(config[key])) throw new Error('Personal endpoints require HTTPS (HTTP is test-only opt-in) and credential-free URLs');
  }
  if (!/^[a-f0-9]{64}$/.test(config.artifact_sha256 || '')) throw new Error('Pinned personal artifact required');
  if (config.gateway.command && !config.gateway.command.startsWith('/')) throw new Error('Absolute privileged gateway wrapper required');
  if (config.gateway.socket_path !== undefined || config.gateway.token_file !== undefined) {
    if (config.gateway.command || !config.gateway.socket_path?.startsWith('/') || !config.gateway.token_file?.startsWith('/')) throw new Error('Invalid gateway socket configuration');
  }
  if (config.runtime && (Object.keys(config.runtime).length !== 1 || !config.runtime.config_path?.startsWith('/'))) throw new Error('Invalid runtime operator configuration');
  const accessKeys = new Map();
  const dynamicApplications = new Map();
  const application = {
    get targets() { return Object.assign({}, base?.targets || {}, ...[...dynamicApplications.values()].map((entry) => entry.targets)); },
    describe(id, app) {
      const selected = dynamicApplications.get(id) || base;
      if (!selected) throw new Error('Application environment is not registered');
      return selected.describe(id, app);
    },
  };
  for (const method of ['register', 'deployPublished', 'observePublished', 'observeLogs', 'planPendingDeletion', 'planLifecycle', 'verifyLifecyclePlan', 'applyLifecycle', 'reconcileLifecycle', 'resumeLifecycle']) {
    application[method] = (app, ...args) => {
      const selected = dynamicApplications.get(app.environment_target_id) || base;
      if (!selected?.[method]) throw new Error('Application environment is not registered');
      return selected[method](app, ...args);
    };
  }
  const blockerMessage = {
    RUNTIME_OPERATOR_NOT_CONFIGURED: '개인 실행환경 운영 설정이 없습니다.',
    APPLICATION_CI_NOT_CONFIGURED: '공통 빌드·배포 연결이 설정되지 않았습니다.',
    APPLICATION_CI_CONFIGURATION_MISMATCH: '공통 빌드 설정과 개인 실행환경 템플릿이 일치하지 않습니다.',
    RUNTIME_ROUTE_PROFILE_INVALID: '개인 환경 공개 경로 프로필을 확인해야 합니다.',
    RUNTIME_EDGE_TEMPLATE_INVALID: 'OpenStack 공개 경로 기본 설정을 확인해야 합니다.',
    RUNTIME_EDGE_REGISTRATION_UNSUPPORTED: 'OpenStack 실행환경별 공개 경로 등록 기능을 사용할 수 없습니다.',
    RUNTIME_TUNNEL_TEMPLATE_INVALID: '터널 기본 설정을 확인해야 합니다.',
    RUNTIME_DNS_CONFIGURATION_INVALID: '공개 DNS 운영 설정을 확인해야 합니다.',
    PERSONAL_GATEWAY_UNAVAILABLE: '개인 환경 연결 게이트웨이의 준비 상태를 확인해야 합니다.',
  };
  async function readiness() {
    const codes = [];
    if (config.gateway.socket_path) {
      try { if ((await gateway('health')).status !== 'ready') codes.push('PERSONAL_GATEWAY_UNAVAILABLE'); }
      catch { codes.push('PERSONAL_GATEWAY_UNAVAILABLE'); }
    }
    if (!config.runtime) codes.push('RUNTIME_OPERATOR_NOT_CONFIGURED');
    if (!service?.identity || typeof service.publishedFiles !== 'function') codes.push('APPLICATION_CI_NOT_CONFIGURED');
    if (config.runtime) {
      try {
        const result = await runner(python, [runtimeScript, '--config', config.runtime.config_path, '--check'], { mutation: false, timeout: 30000 });
        if (result?.ready !== true) codes.push(...(result?.blockers || []).map((row) => row?.code));
        const runtimeConfig = await privateJson(config.runtime.config_path);
        const template = await privateJson(runtimeConfig.application_template);
        const profile = template.environments?.[runtimeConfig.template_environment];
        if (service?.identity && (profile?.tenant !== service.identity.tenant
            || profile?.source_repository !== service.identity.sourceRepository)) codes.push('APPLICATION_CI_CONFIGURATION_MISMATCH');
      } catch { codes.push('RUNTIME_OPERATOR_NOT_CONFIGURED'); }
    }
    const unique = [...new Set(codes.filter((code) => /^[A-Z][A-Z0-9_]{0,95}$/.test(code || '')))];
    return { scope: 'personal', ready: unique.length === 0, verification_scope: 'configuration_only',
      message: '운영 파일과 로컬 비밀값 참조를 검사했습니다. 외부 자격의 유효성이나 실제 클라우드 연결은 등록 과정에서 별도로 검증합니다.',
      blockers: unique.map((code) => ({ code, message: blockerMessage[code] || '개인 환경 운영 설정을 확인해야 합니다.' })) };
  }
  async function gateway(action, target) {
    if (config.gateway.socket_path) {
      const tokenPath = config.gateway.token_file, info = await lstat(tokenPath);
      if (!info.isFile() || info.size > 513 || info.uid !== process.getuid() || info.mode & 0o077) throw new Error('Private gateway token required');
      const token = (await readFile(tokenPath, 'utf8')).trim();
      if (!/^[A-Za-z0-9._~-]{32,512}$/.test(token)) throw new Error('Invalid gateway token');
      const body = action === 'health' ? '' : JSON.stringify({ action, target_id: target.id, generation: target.generation, public_key: target.public_key });
      return new Promise((resolve, reject) => {
        const request = httpRequest({ socketPath: config.gateway.socket_path, path: action === 'health' ? '/healthz' : '/requests', method: action === 'health' ? 'GET' : 'POST',
          headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) } }, (response) => {
          let raw = '';
          response.on('data', (chunk) => { raw += chunk.toString(); if (Buffer.byteLength(raw) > 16384) request.destroy(new Error('Gateway response too large')); });
          response.on('error', reject);
          response.on('end', () => {
            try { if (response.statusCode !== 200) throw new Error('Gateway request failed'); resolve(JSON.parse(raw)); }
            catch { reject(new Error('Gateway response invalid')); }
          });
        });
        request.setTimeout(60000, () => request.destroy(new Error('Gateway request timed out')));
        request.on('error', reject); request.end(body);
      });
    }
    const folder = join(stateDirectory, 'gateway'); await privateDirectory(folder);
    const request = join(folder, `${target.id}-${target.generation}-${action}.json`);
    await savePrivate(request, { action, target_id: target.id, generation: target.generation, public_key: target.public_key });
    const executable = config.gateway.command ? '/usr/bin/sudo' : python;
    const args = config.gateway.command ? ['-n', config.gateway.command, '--request', request] : [gatewayScript, '--config', config.gateway.config_path, '--request', request];
    return runner(executable, args, { mutation: action !== 'verify', timeout: 60000 });
  }
  async function execute(target, { job_id = randomUUID(), argv, delete_data, public_key } = {}) {
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(job_id) || !Array.isArray(argv) || !argv.length || argv.length > 64
        || argv.some((part) => typeof part !== 'string' || !part.length || part.length > 4096 || /[\x00-\x1f]/.test(part))
        || delete_data !== undefined && delete_data !== true || public_key !== undefined && typeof public_key !== 'string') throw new Error('Invalid OpenStack operation');
    const address = target.tunnel?.address?.split('/')[0], source = target.tunnel?.allowed_ips?.split('/')[0];
    if (!/^\d{1,3}(?:\.\d{1,3}){3}$/.test(address || '') || !/^\d{1,3}(?:\.\d{1,3}){3}$/.test(source || '')) throw new Error('Registered gateway route required');
    const gatewayState = await gateway('verify', target);
    if (gatewayState.status !== 'succeeded' || gatewayState.reachable !== true) throw new Error('OpenStack gateway is not connected');
    const folder = join(stateDirectory, target.id), request = { version: 1, job_id, action: 'openstack.execute', params: {
      argv, ...(delete_data !== undefined ? { delete_data } : {}), ...(public_key !== undefined ? { public_key } : {}),
    } };
    const encoded = JSON.stringify(request);
    if (Buffer.byteLength(encoded) > 16384) throw new Error('OpenStack operation too large');
    const args = ['-F', '/dev/null', '-T', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
      '-o', 'StrictHostKeyChecking=yes', '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'ConnectTimeout=10',
      '-o', `UserKnownHostsFile=${join(folder, 'known_hosts')}`, '-i', join(folder, 'runtime_ed25519'),
      '-b', source, '-p', '2222', `railshot-openstack@${address}`, 'railshot-openstack-v1'];
    const response = await transport('ssh', args, { input: encoded, timeout: 120000 });
    const raw = typeof response === 'string' ? response : response.stdout;
    const code = typeof response === 'string' ? 0 : response.code;
    if (![0, 1].includes(code) || typeof raw !== 'string') throw new Error('OpenStack transport failed');
    if (Buffer.byteLength(raw) > MAX_RESPONSE) throw new Error('OpenStack response too large');
    let result;
    try { result = JSON.parse(raw); } catch { throw new Error('OpenStack response invalid'); }
    if (result?.version !== 1 || result.job_id !== job_id || result.action !== request.action || typeof result.ok !== 'boolean' || result.ok !== (code === 0)
        || result.ok && result.error !== null || !result.ok && (!result.error || typeof result.error.code !== 'string')) throw new Error('OpenStack response binding invalid');
    return result;
  }
  async function runtime(action, target, evidence) {
    const blocked = (code) => { dynamicApplications.delete(target.id); return { status: 'blocked', stage: 'operator_configuration', blockers: [code] }; };
    if (!config.runtime) return blocked('RUNTIME_OPERATOR_NOT_CONFIGURED');
    if (!service?.identity || typeof service.publishedFiles !== 'function') return blocked('APPLICATION_CI_NOT_CONFIGURED');
    const folder = join(stateDirectory, target.id); await privateDirectory(folder);
    const observed = await execute(target, { argv: ['server', 'show', evidence.resource_id] });
    if (!observed.ok || !observed.result || Array.isArray(observed.result)) return blocked('RUNTIME_RESOURCE_UNVERIFIED');
    const normalized = Object.fromEntries(Object.entries(observed.result).map(([key, value]) => [key.toLowerCase().replaceAll(' ', '_'), value]));
    if (normalized.id !== evidence.resource_id || (normalized.project_id ?? normalized.tenant_id) !== target.project_id || normalized.status !== 'ACTIVE') return blocked('RUNTIME_RESOURCE_MISMATCH');
    const ports = await execute(target, { argv: ['port', 'list'] });
    if (!ports.ok || !Array.isArray(ports.result)) return blocked('RUNTIME_NETWORK_BINDING_UNVERIFIED');
    const normalize = (row) => Object.fromEntries(Object.entries(row).map(([key, value]) => [key.toLowerCase().replaceAll(' ', '_'), value]));
    const fixedAddress = (value) => Array.isArray(value) && value.some((item) => typeof item === 'string'
      ? item === evidence.private_ipv4 : item?.ip_address === evidence.private_ipv4 || item?.address === evidence.private_ipv4);
    const rows = [];
    // The CLI list is a summary: it omits device_id and security groups. Read
    // each project-bound detail instead of treating summary columns as evidence.
    const identifiers = [...new Set(ports.result.map(normalize).filter((row) => fixedAddress(row.fixed_ips ?? row.fixed_ip_addresses)).map((row) => row.id))];
    if (!identifiers.length || identifiers.length > 100 || identifiers.some((id) => !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(id || ''))) return blocked('RUNTIME_NETWORK_BINDING_UNVERIFIED');
    for (const id of identifiers) {
      const detail = await execute(target, { argv: ['port', 'show', id] });
      if (!detail.ok || !detail.result || Array.isArray(detail.result)) return blocked('RUNTIME_NETWORK_BINDING_UNVERIFIED');
      const row = normalize(detail.result);
      if (row.id !== id || (row.project_id ?? row.tenant_id) !== target.project_id) return blocked('RUNTIME_NETWORK_BINDING_UNVERIFIED');
      rows.push(row);
    }
    const selectedPorts = rows.filter((row) => row.device_id === evidence.resource_id && fixedAddress(row.fixed_ips));
    if (selectedPorts.length !== 1 || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(selectedPorts[0].id || '')) return blocked('RUNTIME_NETWORK_BINDING_UNVERIFIED');
    const groupValues = selectedPorts[0].security_group_ids ?? selectedPorts[0].security_groups;
    if (!Array.isArray(groupValues)) return blocked('RUNTIME_NETWORK_BINDING_UNVERIFIED');
    const groups = groupValues.map((item) => typeof item === 'string' ? item : item?.id).filter(Boolean);
    if (groups.length !== 1 || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(groups[0])) return blocked('RUNTIME_NETWORK_BINDING_UNVERIFIED');
    const address = target.tunnel.address.split('/')[0], knownHosts = join(folder, 'runtime_known_hosts');
    // Existing Ansible transport pins HostKeyAlias to the selected VM's private IP.
    const known = `${evidence.private_ipv4} ${evidence.ssh_host_key}\n`;
    try { if (await readFile(knownHosts, 'utf8') !== known) return blocked('RUNTIME_HOST_IDENTITY_CHANGED'); }
    catch (error) { if (error.code !== 'ENOENT') throw error; await writeFile(knownHosts, known, { mode: 0o600, flag: 'wx' }); }
    const { ssh_host_key, ...nativeEvidence } = evidence;
    const request = join(folder, `runtime-${action}-request.json`);
    await savePrivate(request, { action, target_id: target.id, generation: target.generation, project_id: target.project_id,
      profile_id: target.runtime?.profile_id || null, expected_resource_id: target.runtime?.resource_id || null,
      address, identity_file: join(folder, 'runtime_ed25519'), known_hosts_file: knownHosts,
      evidence: { ...nativeEvidence, provider_binding: { port_id: selectedPorts[0].id,
        security_group_id: groups[0] }, server: observed.result } });
    const result = await runner(python, [runtimeScript, '--config', config.runtime.config_path, '--request', request], { mutation: !['verify', 'reconcile', 'inspect-delete'].includes(action), timeout: ['verify', 'reconcile', 'inspect-delete'].includes(action) ? 180_000 : 1_800_000 });
    if (result.target_id !== target.id || result.generation !== target.generation || !['succeeded', 'blocked', 'unknown'].includes(result.status)) throw new Error('Runtime response binding invalid');
    const blockers = (result.blockers || []).map((item) => typeof item === 'string' ? item : item?.code).filter((item) => /^[A-Z][A-Z0-9_]{0,95}$/.test(item || ''));
    if (result.status !== 'succeeded') { dynamicApplications.delete(target.id); return { status: result.status,
      stage: /^[a-z_]{1,48}$/.test(result.stage || '') ? result.stage : 'verification', cluster_verified: result.cluster_verified === true,
      resumable: ['reconcile', 'inspect-delete'].includes(action) && result.resumable === true,
      blockers: blockers.length ? blockers : ['RUNTIME_VERIFICATION_FAILED'] }; }
    if (['delete', 'inspect-delete', 'resume-delete'].includes(action)) {
      if (result.revocation_verified !== true || !Array.isArray(result.residuals) || result.residuals.length) throw new Error('Runtime revocation unverified');
      dynamicApplications.delete(target.id);
      return { status: 'succeeded', residuals: [], revocation_verified: true };
    }
    const runtimeConfig = await privateJson(config.runtime.config_path);
    if (result.application_config_path !== join(runtimeConfig.state_dir, target.id, 'applications.json')
        || !/^[a-f0-9]{64}$/.test(result.binding_sha256 || '') || !Number.isFinite(Date.parse(result.verified_at))
        || Math.abs(Date.now() - Date.parse(result.verified_at)) > 180000) throw new Error('Runtime verification receipt invalid');
    const selected = await applicationFactory({ configPath: result.application_config_path, ciIdentity: service.identity, loadPublished: service.publishedFiles, python, runner });
    if (Object.keys(selected.targets).length !== 1 || selected.targets[target.id]?.automaticDelivery !== true) return blocked('RUNTIME_APPLICATION_BINDING_INCOMPLETE');
    dynamicApplications.set(target.id, selected);
    return { status: 'succeeded', stage: 'complete', cluster_verified: true, blockers: [], binding_sha256: result.binding_sha256, verified_at: result.verified_at };
  }
  return {
    config, application, execute,
    readiness,
    prepareRuntime: (target, evidence) => runtime('prepare', target, evidence),
    verifyRuntime: (target, evidence) => runtime('verify', target, evidence),
    reconcileRuntime: (target, evidence) => runtime('reconcile', target, evidence),
    removeRuntime: (target) => runtime('delete', target, target.runtime_evidence),
    inspectRuntimeRemoval: (target) => runtime('inspect-delete', target, target.runtime_evidence),
    resumeRuntimeRemoval: (target) => runtime('resume-delete', target, target.runtime_evidence),
    deploymentReady: (target) => dynamicApplications.has(target.id),
    async restoreApplications(target) {
      if (dynamicApplications.has(target.id)) return;
      if (!target.runtime_binding || !config.runtime || !service?.identity) throw new Error('Registered runtime binding required');
      const common = await privateJson(config.runtime.config_path);
      const selected = await applicationFactory({ configPath: join(common.state_dir, target.id, 'applications.json'),
        ciIdentity: service.identity, loadPublished: service.publishedFiles, python, runner });
      if (Object.keys(selected.targets).length !== 1 || selected.targets[target.id]?.automaticDelivery !== true) throw new Error('Application binding invalid');
      dynamicApplications.set(target.id, selected);
    },
    runtimeAccess: (target) => accessKeys.get(target.id),
    async prepare(target) {
      const folder = join(stateDirectory, target.id); await privateDirectory(folder);
      if (!/^ssh-ed25519 [A-Za-z0-9+/]{68}={0,2}$/.test(target.runtime?.ssh_host_key || '')) throw new Error('Customer management host key missing');
      const address = target.tunnel?.address?.split('/')[0];
      if (!/^\d{1,3}(?:\.\d{1,3}){3}$/.test(address || '')) throw new Error('Gateway address invalid');
      const identity = join(folder, 'runtime_ed25519'), knownHosts = join(folder, 'known_hosts');
      try { const info = await lstat(identity); if (!info.isFile() || info.size > 513 || info.uid !== process.getuid() || info.mode & 0o077) throw new Error('Private management identity invalid'); }
      catch (error) {
        if (error.code !== 'ENOENT') throw error;
        await run('ssh-keygen', ['-q', '-t', 'ed25519', '-N', '', '-C', `railshot:${target.id}`, '-f', identity]);
      }
      const known = `[${address}]:2222 ${target.runtime.ssh_host_key}\n`;
      try { if (await readFile(knownHosts, 'utf8') !== known) throw new Error('Management host identity changed'); }
      catch (error) { if (error.code !== 'ENOENT') throw error; await writeFile(knownHosts, known, { mode: 0o600, flag: 'wx' }); }
      accessKeys.set(target.id, { public_key: (await readFile(identity + '.pub', 'utf8')).trim() });
      return false; // Host access alone is not proof of a prepared Kubernetes deployment environment.
    },
    register: (target) => gateway('register', target),
    async verify(target) {
      const result = await gateway('verify', target);
      if (result.status !== 'succeeded' || !result.reachable) return { ...result, openstack_verified: false };
      try {
        const response = await execute(target, { argv: ['server', 'list'] });
        return { ...result, openstack_verified: response.ok && Array.isArray(response.result) };
      } catch { return { ...result, openstack_verified: false }; }
    },
    remove: async (target) => {
      const result = await gateway('remove', target);
      if (result.status === 'succeeded') {
        for (const file of ['runtime_ed25519', 'runtime_ed25519.pub', 'known_hosts']) {
          await unlink(join(stateDirectory, target.id, file)).catch((error) => { if (error.code !== 'ENOENT') throw error; });
        }
        accessKeys.delete(target.id);
      }
      return result;
    },
  };
}
