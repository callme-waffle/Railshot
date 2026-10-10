import yazl from 'yazl';
import { ServiceError } from '../github.js';
import { archiveLimits, inspectArchive, validateFiles } from '../archive.js';
import { APP_NAME, APP_NAME_MESSAGE, TARGET_ID } from '../contract.js';
import { readLimited } from './request.js';

function normalizedRepository(value) {
  let url;
  try { url = new URL(value); } catch { throw new ServiceError('공개 GitHub 저장소 URL이 필요합니다.', 400); }
  const match = /^\/([A-Za-z0-9-]+)\/([A-Za-z0-9._-]+?)(?:\.git)?\/?$/.exec(url.pathname);
  if (url.protocol !== 'https:' || url.hostname !== 'github.com' || url.port || url.username || url.password || url.search || url.hash || !match || match[1].startsWith('-') || match[2].startsWith('.') || match[2].endsWith('.')) throw new ServiceError('공개 GitHub 저장소 기본 URL만 사용할 수 있습니다.', 400);
  return `https://github.com/${match[1].toLowerCase()}/${match[2].toLowerCase()}`;
}
// Parse without fetching GitHub: an idempotency replay must retain its first source snapshot.
export async function uploadedSource(request, strict = false, allowSelection = false, sourceOnly = false) {
  const contentType = request.headers['content-type'] || '';
  if (!/^multipart\/form-data\s*;/i.test(contentType)) throw new ServiceError('multipart/form-data 요청이 필요합니다.', 415);
  const body = await readLimited(request, archiveLimits.maxBytes + 1024 * 1024);
  let form;
  try { form = await new Request('http://localhost/', { method: 'POST', headers: { 'content-type': contentType }, body }).formData(); }
  catch { throw new ServiceError('multipart 요청 형식이 잘못되었습니다.', 400); }
  const fail = (message) => { throw new ServiceError(message, strict ? 422 : 400); };
  const allowed = new Set(['app', 'target_id', 'plan_id', 'source_type', 'repository_url', 'archive', 'files', 'paths']);
  if (sourceOnly) for (const name of ['app', 'target_id', 'plan_id']) allowed.delete(name);
  if (allowSelection) for (const name of ['environment', 'provider', 'source_name', 'expected_target_id']) allowed.add(name);
  for (const key of form.keys()) {
    if (!allowed.has(key)) fail('알 수 없는 입력 필드입니다.');
    if (key !== 'files' && form.getAll(key).length !== 1) fail('단일 입력 필드를 중복해서 보낼 수 없습니다.');
  }
  const selecting = allowSelection && (form.has('environment') || form.has('provider'));
  const app = form.has('app') ? form.get('app') : undefined;
  if (!selecting && !sourceOnly && (typeof app !== 'string' || !APP_NAME.test(app))) fail(APP_NAME_MESSAGE);
  const target_id = form.has('target_id') ? form.get('target_id') : undefined;
  if (target_id !== undefined && (typeof target_id !== 'string' || !target_id)) fail('대상 ID가 잘못되었습니다.');
  if (strict && !selecting && !sourceOnly && !target_id) fail('대상 ID가 필요합니다.');
  const plan_id = form.has('plan_id') ? form.get('plan_id') : undefined;
  if (plan_id !== undefined && (!allowSelection || typeof plan_id !== 'string' || !/^[A-Za-z0-9._-]{1,128}$/.test(plan_id))) fail('환경 계획 ID가 잘못되었습니다.');
  let selected = {};
  if (selecting) {
    const environment = form.get('environment'), provider = form.get('provider');
    if (form.has('app') || form.has('plan_id')) fail('환경 선택과 직접 대상·계획 지정을 함께 사용할 수 없습니다.');
    if (!(environment === 'cloud' && ['aws', 'gcp'].includes(provider) || environment === 'onprem' && ['openstack', 'proxmox'].includes(provider))) fail('배포 환경과 인프라 종류를 확인하세요.');
    const source_name = form.has('source_name') ? form.get('source_name') : undefined;
    if (source_name !== undefined && (typeof source_name !== 'string' || !source_name.length || source_name.length > 255 || /[\x00-\x1f]/.test(source_name))) fail('소스 이름을 확인하세요.');
    const expected = form.has('expected_target_id') ? form.get('expected_target_id') : undefined;
    if (expected !== undefined && (typeof expected !== 'string' || !TARGET_ID.test(expected))) fail('검토한 배포 대상 ID가 잘못되었습니다.');
    if (target_id !== undefined && !(environment === 'onprem' && provider === 'openstack')) fail('개인 환경은 OpenStack에서만 선택하세요.');
    selected = { target_id: undefined, deployment_selection: { environment, provider, ...(target_id !== undefined ? { target_id } : {}) }, source_name, ...(expected !== undefined ? { expected_target_id: expected } : {}) };
  } else if (form.has('source_name') || form.has('expected_target_id')) fail('소스 이름과 검토 대상은 환경 선택과 함께 입력하세요.');
  const uploads = form.getAll('files');
  const supplied = [form.has('repository_url') && 'github', uploads.length > 0 && 'folder', form.has('archive') && 'zip'].filter(Boolean);
  if (supplied.length !== 1) fail('배포 소스 하나만 입력하세요.');
  const source_type = supplied[0];
  if (form.has('source_type') && form.get('source_type') !== source_type) fail('소스 형식과 입력값이 일치하지 않습니다.');
  if (source_type !== 'folder' && form.has('paths')) fail('폴더 소스에만 paths를 사용할 수 있습니다.');
  if (source_type === 'github') {
    if (typeof form.get('repository_url') !== 'string') fail('공개 GitHub 저장소 URL이 필요합니다.');
    let repository_url;
    try { repository_url = normalizedRepository(form.get('repository_url')); } catch { fail('공개 GitHub 저장소 기본 URL이 필요합니다.'); }
    return { ...(sourceOnly ? {} : { app, target_id }), ...(plan_id ? { plan_id } : {}), ...selected, source_type, repository_url };
  }
  try {
    if (source_type === 'folder') {
      const paths = JSON.parse(form.get('paths'));
      if (!Array.isArray(paths) || paths.length !== uploads.length || uploads.length > archiveLimits.maxFiles) fail('폴더 파일 목록이 잘못되었습니다.');
      const files = await Promise.all(uploads.map(async (file, index) => {
        if (!file || typeof file.arrayBuffer !== 'function') fail('폴더 파일이 잘못되었습니다.');
        return { path: paths[index], content: Buffer.from(await file.arrayBuffer()) };
      }));
      return { ...(sourceOnly ? {} : { app, target_id }), ...(plan_id ? { plan_id } : {}), ...selected, source_type, files: validateFiles(files) };
    }
    const file = form.get('archive');
    if (!file || typeof file.arrayBuffer !== 'function' || !file.name?.toLowerCase().endsWith('.zip')) fail('ZIP 파일이 필요합니다.');
    return { ...(sourceOnly ? {} : { app, target_id }), ...(plan_id ? { plan_id } : {}), ...selected, ...(selecting && !selected.source_name ? { source_name: file.name } : {}), source_type, files: await inspectArchive(Buffer.from(await file.arrayBuffer())) };
  } catch { fail('소스 파일 목록·경로·크기를 확인하세요. 비밀 파일은 보낼 수 없습니다.'); }
}
export async function sourceArchive(files) {
  const zip = new yazl.ZipFile();
  for (const file of validateFiles(files)) zip.addBuffer(file.content, file.path);
  zip.end();
  const chunks = []; let size = 0;
  for await (const chunk of zip.outputStream) {
    size += chunk.length;
    if (size > archiveLimits.maxBytes + 1024 * 1024) throw new ServiceError('소스 다운로드 크기가 허용 범위를 초과했습니다.', 413);
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}
