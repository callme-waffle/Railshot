import { ServiceError } from '../github.js';
import { json } from './response.js';
import { jsonInput, page, requestKey } from './request.js';

export const isControlPlaneRoute = (path) => /^\/api\/v1\/(tenancies|clusters)(?:\/|$)/.test(path);
export async function serveControlPlane(request, response, url, control, owner, assertRemovable) {
  if (!control) throw new ServiceError('공통 환경 제어 설정이 준비되지 않았습니다.', 503);
  if (!owner) throw new ServiceError('환경 소유자 세션이 필요합니다.', 401);
  const route = /^\/api\/v1\/(tenancies|clusters)(?:\/([A-Za-z0-9._:-]{1,128}))?(?:\/(checks|catalogs|operations))?$/.exec(url.pathname);
  if (!route) throw new ServiceError('API 경로를 찾을 수 없습니다.', 404);
  const [, kind, id, child] = route;
  const list = !id && request.method === 'GET';
  if (!list && [...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
  const allowed = kind === 'tenancies' ? ['GET'] : child ? ['POST'] : id ? ['GET', 'DELETE'] : ['GET', 'POST'];
  if (!allowed.includes(request.method)) {
    const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = allowed.join(', '); throw error;
  }
  if (kind === 'tenancies') {
    if (!id && !child) { json(response, 200, page((await control.accounts(owner)).items, url.searchParams)); return; }
    if (id && ['checks', 'catalogs'].includes(child)) { json(response, 200, await control[child === 'checks' ? 'connection' : 'catalog'](owner, id)); return; }
  } else {
    if (child && child !== 'operations') throw new ServiceError('API 경로를 찾을 수 없습니다.', 404);
    if (!id && request.method === 'GET') { json(response, 200, page((await control.list(owner)).items, url.searchParams)); return; }
    if (!id && request.method === 'POST') {
      const row = await control.plan(owner, await jsonInput(request), requestKey(request));
      json(response, 201, row, { Location: `/api/v1/clusters/${row.id}` }); return;
    }
    if (id && !child && request.method === 'GET') { json(response, 200, await control.get(owner, id)); return; }
    if (id) {
      const body = request.method === 'DELETE' ? { action: 'delete' } : await jsonInput(request);
      if (!body || Array.isArray(body) || typeof body !== 'object'
          || !['apply', 'reconcile', 'delete', 'start', 'stop', 'backup', 'restore', 'verify'].includes(body.action)
          || Object.keys(body).some((key) => !['action', 'backup_id'].includes(key))
          || body.backup_id !== undefined && (body.action !== 'restore' || typeof body.backup_id !== 'string' || !/^[A-Za-z0-9._-]{1,128}$/.test(body.backup_id)))
        throw new ServiceError('운영 요청을 확인하세요.', 422);
      if (body.action === 'reconcile') { json(response, 200, await control.reconcile(owner, id)); return; }
      if (body.action === 'delete') assertRemovable(id, owner);
      const action = ['apply', 'delete'].includes(body.action) ? body.action : 'operate';
      const row = await control.mutate(owner, id, action, requestKey(request), action === 'operate' ? body : undefined);
      const terminal = !['queued', 'running'].includes(row.status);
      json(response, terminal ? 200 : 202, terminal ? row : { resource_id: id, action: body.action, status: 'accepted', request_id: response.getHeader('X-Request-ID') },
        { Location: `/api/v1/clusters/${id}`, ...(!terminal ? { 'Retry-After': '2' } : {}) }); return;
    }
  }
  throw new ServiceError('API 경로를 찾을 수 없습니다.', 404);
}
