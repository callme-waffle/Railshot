import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const helper = fileURLToPath(new URL('../../../gitops/logs.py', import.meta.url));
const id = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const sha = /^[a-f0-9]{40}$/;
const name = /^[a-z0-9][a-z0-9.-]{0,252}$/;

// The product layer must authorize ownership and reject a superseded shared-app record first.
// Both the record and the private config path come from the backend, never HTTP input.
export function createAppLogsObserver({ configPath, python = 'python3', timeoutMs = 45_000 } = {}) {
  if (configPath && (!configPath.startsWith('/') || !Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 120_000)) {
    throw new Error('Registered log configuration required.');
  }
  return async (record) => {
    const base = { deployment_id: record.id, app: record.app, target_id: record.target_id,
      checked_at: new Date().toISOString(), entries: [] };
    const unavailable = (reason, state = 'unavailable') => ({ ...base, state, reason });
    if (record.kind !== 'deployments' || record.cd?.deployed !== true || record.cd?.state !== 'deployed') {
      return unavailable('deployment_not_ready', 'not_deployed');
    }
    if (!configPath) return unavailable('runtime_logs_not_configured', 'not_configured');
    if (![record.id, record.app, record.target_id].every((v) => typeof v === 'string' && id.test(v)) ||
        !sha.test(record.cd.revision || '') || !sha.test(record.source_commit || '') ||
        !/^[1-9][0-9]{0,19}$/.test(String(record.ci?.run_id || ''))) return unavailable('deployment_not_ready');
    return new Promise((resolve) => {
      const child = spawn(python, [helper, '--config', configPath], { detached: true, stdio: ['pipe', 'pipe', 'pipe'] });
      let stdout = ''; let invalid = false;
      const terminate = () => {
        invalid = true;
        if (child.pid) { try { process.kill(-child.pid, 'SIGKILL'); } catch { /* already exited */ } }
      };
      const timer = setTimeout(terminate, timeoutMs);
      child.stdout.on('data', (chunk) => { stdout += chunk.toString(); if (Buffer.byteLength(stdout) > 262144) terminate(); });
      child.stderr.resume();
      child.stdin.on('error', () => { invalid = true; });
      child.on('error', () => { invalid = true; });
      child.on('close', (code) => {
        clearTimeout(timer);
        try {
          const value = JSON.parse(stdout);
          if (invalid || code !== 0 || !['ready', 'unavailable'].includes(value.state) ||
              !Number.isFinite(Date.parse(value.checked_at)) || !Array.isArray(value.entries) || value.entries.length > 3 ||
              !value.entries.every((entry) => name.test(entry.pod) && name.test(entry.container) && typeof entry.text === 'string') ||
              value.entries.reduce((size, entry) => size + Buffer.byteLength(entry.text), 0) > 32768 ||
              ![null, 'runtime_logs_unavailable', 'runtime_logs_not_ready', 'deployment_not_current'].includes(value.reason) ||
              (value.state !== 'ready' && value.entries.length)) return resolve(unavailable('runtime_logs_unavailable'));
          const state = value.reason === 'deployment_not_current' ? 'superseded' :
            value.state === 'ready' && !value.entries.some((entry) => entry.text.trim()) ? 'no_data' : value.state;
          resolve({ ...base, state, checked_at: value.checked_at, reason: value.reason,
            entries: value.entries.map(({ pod, container, text }) => ({ pod, container, text })) });
        } catch { resolve(unavailable('runtime_logs_unavailable')); }
      });
      child.stdin.end(JSON.stringify({ deployment_id: record.id, app: record.app, target_id: record.target_id,
        source_commit: record.source_commit, run_id: String(record.ci.run_id), revision: record.cd.revision }));
    });
  };
}
