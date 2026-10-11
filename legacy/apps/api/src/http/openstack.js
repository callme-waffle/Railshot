import { ServiceError } from '../github.js';
import { buildOpenStackInstaller } from '../openstack/installer.js';
import { json } from './response.js';
import { jsonInput, page } from './request.js';

const installerPaths = new Set([
  '/api/v1/installers/openstack',
  '/api/v1/installers/openstack/scripts',
  '/api/v1/installers/openstack/bundles',
  '/api/v1/installers/openstack/client',
]);

export const isRegistrationRoute = (path) => /^\/api\/v1\/registrations(?:\/|$)/.test(path);
export const isTokenClaimRoute = (path) => path === '/api/v1/registrations/claim';

export function createOpenStackRoutes() {
  let installerReady;

  return {
    async serveTokenizedInstaller(request, response, url, products) {
      if (request.method !== 'GET') {
        const error = new ServiceError('지원하지 않는 메서드입니다.', 405);
        error.allow = 'GET';
        throw error;
      }
      if (url.searchParams.size !== 1 || url.searchParams.getAll('token').length !== 1
          || !products.registrations?.activeToken(url.searchParams.get('token'))) {
        throw new ServiceError('유효하지 않거나 만료된 연계 토큰입니다.', 401);
      }
      const packageData = await (installerReady ??= buildOpenStackInstaller());
      response.writeHead(200, { 'content-type': 'text/x-shellscript; charset=utf-8',
        'content-disposition': 'attachment; filename="install.sh"', 'cache-control': 'no-store',
        'referrer-policy': 'no-referrer', 'x-content-type-options': 'nosniff' });
      response.end(packageData.script);
    },

    async serveInstaller(request, response, url) {
      if (!installerPaths.has(url.pathname)) return false;
      if (request.method !== 'GET') {
        const error = new ServiceError('지원하지 않는 메서드입니다.', 405);
        error.allow = 'GET';
        throw error;
      }
      if ([...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);

      const packageData = await (installerReady ??= buildOpenStackInstaller());
      if (url.pathname.endsWith('/bundles')) {
        response.writeHead(200, { 'content-type': 'application/zip', 'content-length': packageData.archive.length,
          'content-disposition': 'attachment; filename="railshot-openstack-installer.zip"',
          'cache-control': 'no-store', 'x-content-type-options': 'nosniff' });
        response.end(packageData.archive);
      } else if (url.pathname.endsWith('/client')) {
        response.writeHead(200, { 'content-type': 'text/x-python; charset=utf-8',
          'content-disposition': 'attachment; filename="claim_token.py"', 'cache-control': 'no-store',
          'x-content-type-options': 'nosniff' });
        response.end(packageData.tokenClient);
      } else if (url.pathname.endsWith('/scripts')) {
        response.writeHead(200, { 'content-type': 'text/x-shellscript; charset=utf-8',
          'content-disposition': 'attachment; filename="install.sh"', 'cache-control': 'no-store',
          'x-content-type-options': 'nosniff' });
        response.end(packageData.script);
      } else {
        json(response, 200, { install_sh: packageData.script, bundle_sha256: packageData.sha256,
          script_url: '/api/v1/installers/openstack/scripts', bundle_url: '/api/v1/installers/openstack/bundles',
          token_client_url: '/api/v1/installers/openstack/client' });
      }
      return true;
    },

    async claimToken(request, response, url, products) {
      if (request.method !== 'POST') {
        const error = new ServiceError('지원하지 않는 메서드입니다.', 405);
        error.allow = 'POST';
        throw error;
      }
      if ([...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
      if (!products.registrations) throw new ServiceError('등록 저장소를 사용할 수 없습니다.', 503);
      const input = await jsonInput(request);
      if (Object.keys(input).length !== 1 || typeof input.linkage_token !== 'string') {
        throw new ServiceError('일회성 연계 토큰이 필요합니다.', 422);
      }
      const claimed = products.registrations.claim(input.linkage_token);
      if (!claimed) throw new ServiceError('유효하지 않거나 만료된 연계 토큰입니다.', 401);
      json(response, 200, claimed);
    },

    async serveRegistration(request, response, url, products, sessionId) {
      if (!products.registrations) throw new ServiceError('등록 저장소를 사용할 수 없습니다.', 503);
      if ([...url.searchParams].length && !(url.pathname === '/api/v1/registrations' && request.method === 'GET')) {
        throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
      }
      const route = /^\/api\/v1\/registrations(?:\/([a-f0-9-]{36})(?:\/(tokens))?)?$/.exec(url.pathname);
      if (!route) throw new ServiceError('API 경로를 찾을 수 없습니다.', 404);
      const [, id, child] = route;
      const methods = child ? ['POST'] : id ? ['GET'] : ['GET', 'POST'];
      if (!methods.includes(request.method)) {
        const error = new ServiceError('지원하지 않는 메서드입니다.', 405);
        error.allow = methods.join(', ');
        throw error;
      }
      if (child) {
        const input = await jsonInput(request);
        if (Object.keys(input).length !== 0) throw new ServiceError('재발급 요청에는 빈 JSON 객체만 입력하세요.', 422);
        json(response, 201, products.registrations.issue(sessionId, id));
      } else if (id) {
        json(response, 200, products.registrations.get(sessionId, id));
      } else if (request.method === 'GET') {
        json(response, 200, page(products.registrations.list(sessionId), url.searchParams));
      } else {
        const record = products.registrations.create(sessionId, await jsonInput(request));
        json(response, 201, record, { Location: `/api/v1/registrations/${record.id}` });
      }
    },
  };
}
