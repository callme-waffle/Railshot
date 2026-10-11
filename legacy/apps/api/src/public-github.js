import { archiveLimits, inspectArchive } from './archive.js';
import { ServiceError } from './github.js';

const headers = { accept: 'application/vnd.github+json', 'user-agent': 'railshot-api' };

function parseRepositoryUrl(input) {
  let url;
  try { url = new URL(input); } catch { throw new ServiceError('GitHub 저장소 URL을 입력하세요.', 400); }
  if (url.protocol !== 'https:' || url.hostname !== 'github.com' || url.port || url.username || url.password || url.search || url.hash) {
    throw new ServiceError('https://github.com/소유자/저장소 형식의 공개 저장소 URL만 사용할 수 있습니다.', 400);
  }
  const match = /^\/([A-Za-z0-9-]+)\/([A-Za-z0-9._-]+?)(?:\.git)?\/?$/.exec(url.pathname);
  if (!match || match[1].startsWith('-') || match[2].startsWith('.') || match[2].endsWith('.')) {
    throw new ServiceError('저장소의 기본 URL만 입력하세요. 브랜치·하위 폴더 URL은 지원하지 않습니다.', 400);
  }
  return { owner: match[1], repo: match[2], repository: `https://github.com/${match[1]}/${match[2]}` };
}

async function githubJson(fetchImpl, path) {
  const response = await fetchImpl(`https://api.github.com${path}`, { headers, redirect: 'manual', signal: AbortSignal.timeout(30_000) });
  if (response.status === 404) throw new ServiceError('GitHub에서 공개 소스를 찾을 수 없습니다. 저장소 주소와 기본 브랜치를 확인하거나 ZIP·폴더로 업로드하세요. 비공개 저장소 URL은 지원하지 않습니다.', 422, 'SOURCE_NOT_FOUND');
  if (response.status === 403 || response.status === 429) throw new ServiceError('GitHub의 공개 API 요청 한도에 도달했습니다. 잠시 후 다시 시도하세요.', 503);
  if (!response.ok) throw new ServiceError(`GitHub 공개 API 요청 실패 (${response.status}).`, 502);
  return response.json();
}

async function limitedBytes(response) {
  if (!response.ok) throw new ServiceError(`GitHub 소스 다운로드 실패 (${response.status}).`, 502);
  const declared = Number(response.headers.get('content-length'));
  if (declared > archiveLimits.maxBytes) throw new ServiceError('GitHub 소스 ZIP은 100 MB 이하여야 합니다.', 413);
  const chunks = [];
  let size = 0;
  for await (const chunk of response.body) {
    size += chunk.length;
    if (size > archiveLimits.maxBytes) throw new ServiceError('GitHub 소스 ZIP은 100 MB 이하여야 합니다.', 413);
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

export async function fetchPublicGithubSource(input, fetchImpl = fetch) {
  const { owner, repo, repository } = parseRepositoryUrl(input);
  const path = `/repos/${owner}/${repo}`;
  // No server token is sent to the source repository: only anonymously readable repos qualify.
  const metadata = await githubJson(fetchImpl, path);
  if (metadata.private !== false || metadata.visibility && metadata.visibility !== 'public') {
    throw new ServiceError('공개 저장소만 배포 소스로 사용할 수 있습니다.', 400);
  }
  if (!metadata.default_branch) throw new ServiceError('저장소의 기본 브랜치를 확인할 수 없습니다.', 400);
  const commit = await githubJson(fetchImpl, `${path}/commits/${encodeURIComponent(metadata.default_branch)}`);
  if (!/^[0-9a-f]{40}$/i.test(commit.sha || '')) throw new ServiceError('저장소의 커밋 SHA를 확인할 수 없습니다.', 502);
  const archive = await fetchImpl(`https://api.github.com${path}/zipball/${commit.sha}`, { headers, redirect: 'manual', signal: AbortSignal.timeout(30_000) });
  if (![301, 302, 303, 307, 308].includes(archive.status)) throw new ServiceError(`GitHub 소스 다운로드 요청 실패 (${archive.status}).`, 502);
  let downloadUrl;
  try { downloadUrl = new URL(archive.headers.get('location')); } catch { throw new ServiceError('GitHub 소스 다운로드 주소가 잘못되었습니다.', 502); }
  if (downloadUrl.protocol !== 'https:' || downloadUrl.hostname !== 'codeload.github.com' || downloadUrl.username || downloadUrl.password || downloadUrl.port) {
    throw new ServiceError('GitHub 소스 다운로드 주소가 허용되지 않습니다.', 502);
  }
  const bytes = await limitedBytes(await fetchImpl(downloadUrl, { redirect: 'manual', headers: { 'user-agent': headers['user-agent'] }, signal: AbortSignal.timeout(30_000) }));
  let files;
  try { files = await inspectArchive(bytes, { stripRoot: true }); }
  catch (error) { throw new ServiceError(`GitHub 소스 검사 실패: ${error.message}`, 422); }
  return { files, source: { type: 'github', repository, sha: commit.sha } };
}
