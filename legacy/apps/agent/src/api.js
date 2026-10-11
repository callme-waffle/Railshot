import { readFileSync } from 'node:fs';
import { mkdir, lstat, readFile, rename, unlink, writeFile } from 'node:fs/promises';
import { createHash, randomUUID } from 'node:crypto';
import { homedir } from 'node:os';
import { isAbsolute, join } from 'node:path';

const sessionCookie = 'railshot_session';
const sessionToken = /^[A-Za-z0-9_-]{43}$/;

async function privatePath(path, directory = false) {
  const info = await lstat(path);
  if ((directory ? !info.isDirectory() : !info.isFile()) || info.uid !== process.getuid() || (info.mode & 0o077)) {
    throw new Error('MCP 세션 저장소는 현재 사용자만 접근할 수 있어야 합니다.');
  }
}

async function savedSession(path, directory) {
  try {
    await privatePath(directory, true);
    await privatePath(path);
    const token = (await readFile(path, 'utf8')).trim();
    if (!sessionToken.test(token)) throw new Error('MCP 세션 파일이 잘못되었습니다.');
    return token;
  } catch (error) {
    if (error.code === 'ENOENT') return null;
    throw error;
  }
}

async function storeSession(path, directory, token) {
  await mkdir(directory, { recursive: true, mode: 0o700 });
  await privatePath(directory, true);
  const temporary = `${path}.${randomUUID()}`;
  await writeFile(temporary, token, { mode: 0o600, flag: 'wx' });
  try { await rename(temporary, path); }
  catch (error) {
    await unlink(temporary).catch(() => {});
    throw error;
  }
}

function apiToken(env) {
  if (env.RAILSHOT_API_TOKEN && env.RAILSHOT_API_TOKEN_FILE) throw new Error('API 토큰은 값 또는 파일 중 하나만 설정하세요.');
  if (!env.RAILSHOT_API_TOKEN_FILE) return env.RAILSHOT_API_TOKEN;
  try { return readFileSync(env.RAILSHOT_API_TOKEN_FILE, 'utf8').trim(); }
  catch { throw new Error('API 토큰 파일을 읽을 수 없습니다.'); }
}

export function createApiClient({ baseUrl = process.env.RAILSHOT_API_URL || 'http://127.0.0.1:4173', env = process.env, fetchImpl = fetch, session = null } = {}) {
  const base = new URL(baseUrl);
  if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password || base.search || base.hash || base.pathname !== '/') {
    throw new Error('RAILSHOT_API_URL은 API 원점 주소여야 합니다.');
  }
  if (session !== null && (!sessionToken.test(session) || typeof session !== 'string')) throw new Error('원격 MCP 세션이 잘못되었습니다.');
  const directory = session === null ? (env.RAILSHOT_AGENT_SESSION_DIR || join(homedir(), '.local', 'state', 'railshot-agent')) : null;
  if (directory !== null && !isAbsolute(directory)) throw new Error('RAILSHOT_AGENT_SESSION_DIR은 절대 경로여야 합니다.');
  const sessionPath = directory && join(directory, `${createHash('sha256').update(base.origin).digest('hex')}.cookie`);
  let pending = Promise.resolve();
  async function send(path, { method = 'GET', body, key } = {}) {
    const token = apiToken(env);
    const cookie = session ?? await savedSession(sessionPath, directory);
    const headers = { ...(token ? { authorization: `Bearer ${token}` } : {}), ...(cookie ? { cookie: `${sessionCookie}=${cookie}` } : {}),
      ...(key ? { 'Idempotency-Key': key } : {}) };
    const response = await fetchImpl(new URL(path, base), {
      method, headers, body, redirect: 'error', signal: AbortSignal.timeout(30000),
    });
    const received = response.headers.get('set-cookie')?.match(/^railshot_session=([^;,\s]+)/)?.[1];
    if (received) {
      if (!sessionToken.test(received)) throw new Error('API 세션 쿠키가 잘못되었습니다.');
      if (session !== null && received !== session) {
        const error = new Error('웹 세션이 만료되었습니다. AI 연결을 다시 승인하세요.');
        error.code = 'SESSION_EXPIRED';
        if (method !== 'GET') error.outcomeUnknown = true;
        throw error;
      }
      try { if (session === null) await storeSession(sessionPath, directory, received); }
      catch (error) {
        if (method !== 'GET') error.outcomeUnknown = true;
        throw error;
      }
    }
    let value;
    try { value = await response.json(); } catch { throw new Error(`API 응답을 읽을 수 없습니다 (${response.status}).`); }
    if (!response.ok) {
      const detail = value?.error;
      const error = new Error(typeof detail?.message === 'string' ? detail.message : `API 요청 실패 (${response.status}).`);
      error.code = detail?.code || 'UPSTREAM_FAILURE';
      error.status = response.status;
      error.outcomeUnknown = detail?.outcome_unknown === true;
      throw error;
    }
    return value;
  }
  function request(path, options) {
    // Concurrent first calls must not create different remote sessions.
    const result = pending.then(() => send(path, options));
    pending = result.catch(() => {});
    return result;
  }
  return {
    options: () => request('/api/v1/options'),
    targets: () => request('/api/v1/targets'),
    deployment: (id) => request(`/api/v1/deployments/${encodeURIComponent(id)}`),
    appOverview: (id, minutes) => request(`/api/v1/deployments/${encodeURIComponent(id)}/insights?minutes=${minutes}`),
    deploymentEvidence: (id, area) => request(`/api/v1/deployments/${encodeURIComponent(id)}/evidence?area=${area}`),
    deploymentEvents: (id) => request(`/api/v1/deployments/${encodeURIComponent(id)}/events`),
    deploymentDiagnostics: (id) => request(`/api/v1/deployments/${encodeURIComponent(id)}/diagnostics`),
    build: (id) => request(`/api/v1/builds/${encodeURIComponent(id)}`),
    deployArchive: async ({ app, target_id, idempotency_key, bytes, name }) => {
      const form = new FormData();
      form.set('app', app);
      form.set('target_id', target_id);
      form.set('archive', new Blob([bytes], { type: 'application/zip' }), name);
      try {
        return await request('/api/v1/deployments', { method: 'POST', body: form, key: idempotency_key });
      } catch (error) {
        // A lost response cannot establish whether the upload was accepted. Never auto-retry it.
        if (error.status === undefined) error.outcomeUnknown = true;
        throw error;
      }
    },
    deploy: ({ repository_url, app, target_id, idempotency_key }) => {
      const form = new FormData();
      form.set('repository_url', repository_url);
      form.set('app', app);
      form.set('target_id', target_id);
      return request('/api/v1/deployments', { method: 'POST', body: form, key: idempotency_key });
    },
  };
}
