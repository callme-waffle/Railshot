import { readFile, readdir, lstat, mkdir, writeFile, rename } from 'node:fs/promises';
import { createHash, randomUUID } from 'node:crypto';
import { homedir } from 'node:os';
import { basename, join, relative, resolve, sep } from 'node:path';
import yazl from 'yazl';
import { APP_NAME, APP_NAME_MESSAGE, sourceAppName } from './contract.js';
import { readApiToken } from './access.js';
import { cookieToken, SESSION_COOKIE } from './sessions.js';
import { archiveLimits } from './archive.js';

const skipped = new Set(['.git', 'node_modules', '.DS_Store', '__MACOSX']);
const defaultUrl = process.env.RAILSHOT_API_URL || process.env.JASMIN_API_URL || 'http://127.0.0.1:4173';

function authorization() {
  const token = readApiToken();
  return token ? { authorization: `Bearer ${token}` } : {};
}

async function sessionFetch(url, options = {}) {
  // Persist per-origin cookies so a later CLI process can observe its own deployment.
  const directory = process.env.RAILSHOT_CLIENT_SESSION_DIR || join(homedir(), '.local/state/railshot-client');
  const path = join(directory, createHash('sha256').update(url.origin).digest('hex') + '.cookie');
  const check = async (file, isDirectory = false) => {
    const info = await lstat(file);
    if ((isDirectory ? !info.isDirectory() : !info.isFile()) || info.uid !== process.getuid() || (info.mode & 0o077)) throw new Error('CLI 세션 파일은 소유자만 접근할 수 있어야 합니다.');
  };
  let cookie;
  try {
    await check(directory, true); await check(path);
    cookie = (await readFile(path, 'utf8')).trim();
    if (!cookieToken(cookie)) throw new Error('CLI 세션 파일이 잘못되었습니다.');
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  const response = await fetch(url, { ...options, headers: { ...options.headers, ...(cookie && { cookie }) }, redirect: 'error' });
  const token = cookieToken(response.headers.get('set-cookie'));
  if (token) {
    await mkdir(directory, { recursive: true, mode: 0o700 }); await check(directory, true);
    const temporary = `${path}.${randomUUID()}`;
    await writeFile(temporary, `${SESSION_COOKIE}=${token}`, { mode: 0o600, flag: 'wx' });
    await rename(temporary, path);
  }
  return response;
}

export function inferredAppName(source) {
  const raw = /^https?:\/\//i.test(source)
    ? new URL(source).pathname.split('/')[2]?.replace(/\.git$/i, '')
    : basename(resolve(source));
  return sourceAppName(raw);
}

async function sendDeploy(form, baseUrl) {
  const response = await sessionFetch(new URL('/api/deploy', baseUrl), {
    method: 'POST', headers: { 'x-railshot-request': 'deploy', 'x-jasmin-request': 'deploy', ...authorization() }, body: form, redirect: 'error',
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `API ${response.status}`);
  return result;
}

async function zipFolder(folder) {
  const zip = new yazl.ZipFile();
  let count = 0;
  let sourceBytes = 0;
  async function visit(directory) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      if (skipped.has(entry.name)) continue;
      const absolute = join(directory, entry.name);
      const path = relative(folder, absolute).split(sep).join('/');
      if (entry.isSymbolicLink()) throw new Error(`심볼릭 링크는 업로드할 수 없습니다: ${path}`);
      if (entry.isDirectory()) { await visit(absolute); continue; }
      if (!entry.isFile()) continue;
      if (/^\.env(?:\.|$)/i.test(entry.name) || /\.(?:pem|key|p12|pfx)$/i.test(entry.name)) {
        throw new Error(`비밀키로 보이는 파일을 제거하세요: ${path}`);
      }
      sourceBytes += (await lstat(absolute)).size;
      if (sourceBytes > archiveLimits.maxBytes) throw new Error('파일 총 크기는 100 MB 이하여야 합니다.');
      zip.addFile(absolute, path);
      count++;
      if (count > archiveLimits.maxFiles) throw new Error('파일은 최대 2,000개까지 업로드할 수 있습니다.');
    }
  }
  await visit(folder);
  if (!count) throw new Error('폴더에 업로드할 파일이 없습니다.');
  zip.end();
  return new Promise((done, fail) => {
    const chunks = [];
    let size = 0;
    zip.outputStream.on('data', (chunk) => {
      size += chunk.length;
      if (size > archiveLimits.maxBytes) zip.outputStream.destroy(new Error('ZIP이 100 MB를 초과했습니다.'));
      else chunks.push(chunk);
    });
    zip.outputStream.once('error', fail);
    zip.outputStream.once('end', () => done(Buffer.concat(chunks)));
  });
}

export async function archiveFromPath(input) {
  const path = resolve(input);
  const stat = await lstat(path);
  if (stat.isSymbolicLink()) throw new Error('심볼릭 링크는 업로드할 수 없습니다.');
  if (stat.isDirectory()) return { bytes: await zipFolder(path), name: `${basename(path)}.zip` };
  if (stat.isFile() && path.toLowerCase().endsWith('.zip')) {
    if (stat.size > archiveLimits.maxBytes) throw new Error('ZIP 파일은 100 MB 이하이어야 합니다.');
    return { bytes: await readFile(path), name: basename(path) };
  }
  throw new Error('폴더 또는 ZIP 파일 경로를 지정하세요.');
}

export async function deployPath({ app, path, targetId, baseUrl = defaultUrl }) {
  const { bytes, name } = await archiveFromPath(path);
  const form = new FormData();
  form.set('app', app);
  if (targetId) form.set('target_id', targetId);
  form.set('archive', new Blob([bytes], { type: 'application/zip' }), name);
  return sendDeploy(form, baseUrl);
}

export async function deployRepository({ app, repositoryUrl, targetId, baseUrl = defaultUrl }) {
  const form = new FormData();
  form.set('app', app);
  if (targetId) form.set('target_id', targetId);
  form.set('repository_url', repositoryUrl);
  return sendDeploy(form, baseUrl);
}

export async function deploySource({ source, app = inferredAppName(source), targetId, baseUrl = defaultUrl }) {
  if (/^https?:\/\//i.test(source)) return deployRepository({ app, repositoryUrl: source, targetId, baseUrl });
  if (/^[a-z][a-z\d+.-]*:\/\//i.test(source)) throw new Error('공개 GitHub HTTPS URL 또는 로컬 경로만 사용할 수 있습니다.');
  return deployPath({ app, path: source, targetId, baseUrl });
}

export async function getRun(runId, baseUrl = defaultUrl) {
  if (!/^\d+$/.test(String(runId))) throw new Error('run_id는 숫자여야 합니다.');
  const response = await sessionFetch(new URL(`/api/runs/${runId}`, baseUrl), { headers: authorization() });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `API ${response.status}`);
  return result;
}
