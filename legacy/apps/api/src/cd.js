import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { lstatSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { setTimeout } from 'node:timers/promises';
import { APP_NAME, TARGET_ID, TENANT_NAME } from './contract.js';

const bridge = fileURLToPath(new URL('../../../gitops/bridge.py', import.meta.url));
const unknown = () => ({ cd: { state: 'unknown', revision: null, deployed: false },
  public_http: { state: 'not_run', verified_at: null, url: null },
  error: { code: 'CD_RECONCILE_REQUIRED', retryable: false, outcome_unknown: true } });

// Configuration and artifact loader are supplied by the backend, never request JSON.
export function createCdAdapter({ configPath, loadPublished, python = 'python3', timeoutMs = 600_000 }) {
  if (!configPath?.startsWith('/') || typeof loadPublished !== 'function' ||
      !Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 1_800_000) {
    throw new Error('Registered CD configuration and trusted artifact loader are required.');
  }
  const info = lstatSync(configPath);
  if (!info.isFile() || info.uid !== process.getuid() || (info.mode & 0o077) || info.size > 1_000_000) {
    throw new Error('CD configuration must be a private operator-owned file.');
  }
  const configBytes = readFileSync(configPath);
  const configDigest = createHash('sha256').update(configBytes).digest('hex');
  const config = JSON.parse(configBytes.toString('utf8'));
  if (config.version !== 1 || !config.targets || Array.isArray(config.targets) || typeof config.targets !== 'object') {
    throw new Error('CD configuration requires registered targets.');
  }
  const targets = Object.fromEntries(Object.entries(config.targets).map(([id, registered]) => {
    if (!registered || typeof registered.app !== 'string' || typeof registered.tenant !== 'string' ||
        !TARGET_ID.test(id) || !APP_NAME.test(registered.app) || !TENANT_NAME.test(registered.tenant) ||
        registered.target?.id !== id) throw new Error('CD application registration is invalid.');
    return [id, Object.freeze({ applicationName: registered.app, tenant: registered.tenant,
      deploymentScope: 'registered_application' })];
  }));
  const invoke = (request, signal, remainingMs) => new Promise((resolve) => {
    if (signal?.aborted) { resolve(unknown()); return; }
    const child = spawn(python, [bridge, '--config', configPath], { detached: true, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = ''; let invalid = false;
    const terminate = () => {
      invalid = true;
      // Kill the entire group immediately: a surviving Git child must not outlive the bridge's lock.
      if (child.pid) { try { process.kill(-child.pid, 'SIGKILL'); } catch { /* already exited */ } }
    };
    const timer = globalThis.setTimeout(terminate, Math.min(config.targets[request.target_id]?.edge ? 600_000 : 120_000, Math.max(1, remainingMs)));
    signal?.addEventListener('abort', terminate, { once: true });
    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString();
      if (stdout.length > 65_536) terminate();
    });
    child.stderr.resume(); // Native diagnostics may contain operator details; never publish them.
    child.stdin.on('error', () => { invalid = true; });
    child.on('error', () => { invalid = true; });
    child.on('close', (code) => {
      clearTimeout(timer); signal?.removeEventListener('abort', terminate);
      try {
        const result = JSON.parse(stdout);
        if (invalid || code !== 0 || !result.cd || typeof result.cd.deployed !== 'boolean' ||
            !['blocked', 'unknown', 'progressing', 'failed', 'deployed'].includes(result.cd.state) ||
            !result.public_http || !['not_run', 'unverified', 'succeeded'].includes(result.public_http.state)) {
          resolve(unknown()); return;
        }
        resolve(result);
      } catch { resolve(unknown()); }
    });
    child.stdin.end(JSON.stringify(request));
  });
  async function deployPublished({ deploymentId, app, targetId, sourceCommit, publication, signal, onProgress, observeOnly = false }) {
    if (publication?.app !== app || publication?.target_id !== targetId || publication?.source_commit !== sourceCommit) {
      throw new Error('Published deployment binding differs.');
    }
    if (targets[targetId]?.applicationName !== app || targets[targetId]?.tenant !== publication.tenant) {
      throw new Error('Registered deployment application differs.');
    }
    const files = await loadPublished(publication);
    const request = { action: observeOnly ? 'observe' : 'apply', deployment_id: deploymentId, target_id: targetId,
      config_sha256: configDigest, publication,
      files: Object.fromEntries(files.map(({ path, content }) => [path, content.toString('base64')])) };
    const deadline = Date.now() + timeoutMs;
    let result = await invoke(request, signal, deadline - Date.now());
    await onProgress?.(result);
    if (observeOnly) return result;
    while (!signal?.aborted && Date.now() < deadline &&
           (result.cd.state === 'progressing' || (result.cd.deployed && result.public_http.state === 'unverified'))) {
      try { await setTimeout(Math.min(2000, Math.max(1, deadline - Date.now())), undefined, { signal }); }
      catch { return unknown(); }
      result = await invoke({ ...request, action: 'observe' }, signal, deadline - Date.now());
      await onProgress?.(result);
    }
    return result;
  }
  deployPublished.targets = Object.freeze(targets);
  return deployPublished;
}
