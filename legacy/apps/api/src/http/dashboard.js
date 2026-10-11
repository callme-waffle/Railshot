import { ServiceError } from '../github.js';
import { DashboardError } from '../sessions.js';
import { json } from './response.js';
import { jsonInput, page } from './request.js';

export const isDashboardRoute = (path) => /^\/api\/v1\/(sessions|preferences|connections)(?:\/|$)/.test(path);

export async function serveDashboard(request, response, url, products, session, requestId, sessionId = session.id) {
  const route = /^\/api\/v1\/(sessions|preferences|connections)(?:\/([a-f0-9-]{36}))?$/.exec(url.pathname);
  if (!route) throw new ServiceError('API 경로를 찾을 수 없습니다.', 404);
  const [, kind, id] = route;
  if (id && kind !== 'connections') throw new ServiceError('API 경로를 찾을 수 없습니다.', 404);

  const methods = kind === 'preferences' ? ['GET', 'PUT'] : id ? ['GET', 'PUT', 'DELETE'] : ['GET', 'POST'];
  if (!methods.includes(request.method)) {
    const error = new ServiceError('지원하지 않는 메서드입니다.', 405);
    error.allow = methods.join(', ');
    throw error;
  }
  if ([...url.searchParams].length && !(kind === 'connections' && !id && request.method === 'GET')) {
    throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
  }

  if (kind === 'sessions') {
    json(response, request.method === 'POST' && session.token ? 201 : 200,
      { expires_at: session.expires_at }, { 'X-Request-ID': requestId });
    return;
  }
  if (kind === 'preferences') {
    json(response, 200, products.dashboard.preferences(sessionId,
      request.method === 'PUT' ? await jsonInput(request) : undefined));
    return;
  }
  if (!id && request.method === 'GET') {
    json(response, 200, page(products.dashboard.connections(sessionId), url.searchParams));
    return;
  }
  if (id && request.method === 'GET') {
    const connection = products.dashboard.connections(sessionId).find((row) => row.id === id);
    if (!connection) throw new DashboardError('이 세션에서 자원을 찾을 수 없습니다.', 404, 'NOT_FOUND');
    json(response, 200, connection);
    return;
  }
  if ((!id && request.method === 'POST') || (id && request.method === 'PUT')) {
    const connection = products.dashboard.saveConnection(sessionId, id, await jsonInput(request));
    json(response, id ? 200 : 201, connection,
      id ? {} : { Location: `/api/v1/connections/${connection.id}` });
    return;
  }
  if (id && request.method === 'DELETE') {
    products.dashboard.deleteConnection(sessionId, id);
    response.writeHead(204, { 'cache-control': 'no-store' });
    response.end();
    return;
  }
  throw new ServiceError('지원하지 않는 메서드입니다.', 405);
}
