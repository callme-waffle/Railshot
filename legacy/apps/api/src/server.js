import { monitorEventLoopDelay } from 'node:perf_hooks';
import { createServer } from 'node:http';
import { createTrafficObserver } from './traffic.js';
import { createInsightsService } from './insights.js';
import { lstatSync, readFileSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { homedir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { dirname, isAbsolute, join } from 'node:path';
import { createDeploymentService, ServiceError } from './github.js';
import { fetchPublicGithubSource } from './public-github.js';
import { createProductService } from './product.js';
import { createJevClassifier } from './classifier.js';
import { apiAccessConfig, allowsHost, allowsOrigin, allowsToken } from './access.js';
import { createEnvironmentAdapter } from './environments.js';
import { cookieToken, sessionCookie, SESSION_COOKIE, ownerCookie, ownerToken, OWNER_COOKIE } from './sessions.js';
import { json, apiError, accepted } from './http/response.js';
import { jsonInput, pagination, page, requestKey } from './http/request.js';
import { uploadedSource, sourceArchive } from './http/source.js';
import { createOpenStackRoutes, isRegistrationRoute, isTokenClaimRoute } from './http/openstack.js';
import { isDashboardRoute, serveDashboard } from './http/dashboard.js';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'dashboard');
const assets = new Map([
  ['/', ['index.html', 'text/html; charset=utf-8']],
  ['/app.js', ['app.js', 'text/javascript; charset=utf-8']],
  ['/src/api.js', ['src/api.js', 'text/javascript; charset=utf-8']],
  ['/src/openstack-installer.js', ['src/openstack-installer.js', 'text/javascript; charset=utf-8']],
  ['/src/deployment-history.js', ['src/deployment-history.js', 'text/javascript; charset=utf-8']],
  ['/src/insights.js', ['src/insights.js', 'text/javascript; charset=utf-8']],
  ['/src/insights-view.js', ['src/insights-view.js', 'text/javascript; charset=utf-8']],
  ['/src/recovery.js', ['src/recovery.js', 'text/javascript; charset=utf-8']],
  ['/src/lifecycle.js', ['src/lifecycle.js', 'text/javascript; charset=utf-8']],
  ['/contracts/application.mjs', ['../../contracts/application.mjs', 'text/javascript; charset=utf-8']],
  ['/styles.css', ['styles.css', 'text/css; charset=utf-8']],
]);

export function readGithubToken(env = process.env) {
  if (env.GITHUB_TOKEN !== undefined && env.GITHUB_TOKEN_FILE !== undefined) {
    throw new Error('Set only one of GITHUB_TOKEN and GITHUB_TOKEN_FILE');
  }
  if (env.GITHUB_TOKEN_FILE === undefined) return env.GITHUB_TOKEN;
  try {
    const path = env.GITHUB_TOKEN_FILE;
    if (typeof path !== 'string' || !isAbsolute(path)) throw new Error();
    const info = lstatSync(path);
    if (!info.isFile() || info.isSymbolicLink() || info.uid !== process.getuid() || info.nlink !== 1
        || (info.mode & 0o777) !== 0o600 || info.size < 1 || info.size > 4097) throw new Error();
    const raw = readFileSync(path, 'utf8');
    const token = raw.endsWith('\n') ? raw.slice(0, -1) : raw;
    if (!/^[\x21-\x7e]{1,4096}$/.test(token)) throw new Error();
    return token;
  } catch {
    throw new Error('GITHUB_TOKEN_FILE must be a private operator-owned 0600 regular file');
  }
}

function configuredDeploymentService(env = process.env) {
  const token = readGithubToken(env);
  return token && env.RAILSHOT_TARGET_ID ? createDeploymentService({ token,
    owner: env.GITHUB_OWNER, repo: env.GITHUB_REPO, ref: env.GITHUB_REF, tenant: env.RAILSHOT_TENANT || env.JASMIN_TENANT,
    workflow: env.GITHUB_WORKFLOW, targetId: env.RAILSHOT_TARGET_ID, targetIds: env.RAILSHOT_TARGET_IDS?.split(',') }) : null;
}
export function createAppServer({ sourceLoader = fetchPublicGithubSource, access = apiAccessConfig(),
  service = configuredDeploymentService(),
  stateDirectory = process.env.RAILSHOT_STATE_DIR || join(homedir(), '.local', 'state', 'railshot'),
  deployPublished, environmentAdapter, applicationAdapter, personalAdapter, observeMetrics, observeLogs, observeTraffic, classifyFailure, product, pollInterval,
  maxConcurrentDeployments = Number(process.env.RAILSHOT_MAX_CONCURRENT_DEPLOYMENTS ?? 3),
  target = { provider: process.env.RAILSHOT_TARGET_PROVIDER }, providerTargets, releaseLeaseMs = 120_000,
} = {}) {
  // Keep the dedicated API credential in the adapter closure. Child CI/CD tools
  // must never inherit it through their default process environment.
  const classifierKey = process.env.TYPESAFE_API_KEY;
  delete process.env.TYPESAFE_API_KEY;
  const classifier = classifyFailure === undefined ? createJevClassifier({ apiKey: classifierKey }) : classifyFailure;
  const openstack = createOpenStackRoutes();
  // Explicit adapter instances keep tests offline; production adapters consume only operator files.
  const productReady = Promise.resolve().then(async () => {
    if (product) return product;
    let cd = deployPublished;
    if (!cd && service && process.env.RAILSHOT_CD_CONFIG) {
      const { createCdAdapter } = await import('./cd.js');
      cd = await createCdAdapter({ configPath: process.env.RAILSHOT_CD_CONFIG, loadPublished: service.publishedFiles });
    }
    const environment = environmentAdapter || (process.env.RAILSHOT_PROFILES_FILE ? await createEnvironmentAdapter({ profilesFile: process.env.RAILSHOT_PROFILES_FILE, stateDir: join(stateDirectory, 'environments'), loadPublished: service?.publishedFiles }) : undefined);
    const { createApplicationAdapter } = await import('./applications.js');
    let applications = applicationAdapter || (process.env.RAILSHOT_APPLICATIONS_FILE ? await createApplicationAdapter({ configPath: process.env.RAILSHOT_APPLICATIONS_FILE, ciIdentity: service?.identity, loadPublished: service?.publishedFiles }) : undefined);
    const { createPersonalAdapter } = await import('./personal-adapter.js');
    const personal = personalAdapter || (process.env.RAILSHOT_PERSONAL_CONFIG ? await createPersonalAdapter({ configPath: process.env.RAILSHOT_PERSONAL_CONFIG, stateDirectory: join(stateDirectory, 'personal'), service, base: applications }) : undefined);
    if (personal?.application) applications = personal.application;
    const { createMetricsObserver } = await import('./metrics.js');
    const observer = observeMetrics || createMetricsObserver({
      configPath: process.env.RAILSHOT_OBSERVER_PRODUCT_FILE || process.env.RAILSHOT_OBSERVER_CONFIG,
    });
    const selections = providerTargets ?? (process.env.RAILSHOT_PROVIDER_TARGETS === undefined ? undefined : JSON.parse(process.env.RAILSHOT_PROVIDER_TARGETS));
    const { createAppLogsObserver } = await import('./logs.js');
    const logs = observeLogs || createAppLogsObserver({ configPath: process.env.RAILSHOT_CD_CONFIG });
    return createProductService({ observeMetrics: observer, observeLogs: logs, classifyFailure: classifier, service, target, providerTargets: selections, directory: stateDirectory, deployPublished: cd, environmentAdapter: environment, applicationAdapter: applications, personalAdapter: personal, pollInterval, maxConcurrentDeployments });
  });
  // Hold initialization errors until a request can receive a safe 503; never leak private config paths.
  let productInitialized = false;
  productReady.then(value => { productInitialized = Boolean(value); }, () => {});
  const traffic = observeTraffic || createTrafficObserver({ configPath: process.env.RAILSHOT_OBSERVER_PRODUCT_FILE || process.env.RAILSHOT_OBSERVER_CONFIG });
  let activeRequests = 0, release = null, draining = null, releaseTimer, productClosing, shuttingDown = false;
  const closeProduct = () => productClosing ||= productReady.then((value) => value?.close?.());
  const server = createServer(async (request, response) => {
    const requestId = randomUUID();
    let counted = false;
    let versioned = request.url.startsWith('/api/v1');
    response.setHeader('X-Request-ID', requestId);
    try {
      if (!allowsHost(request.headers.host, access) || !allowsOrigin(request.headers.origin, access)) throw new ServiceError('요청의 Host 또는 Origin이 허용되지 않습니다.', 403);
      let url;
      try { url = new URL(request.url, 'http://localhost'); } catch { throw new ServiceError('요청 경로가 잘못되었습니다.', 400); }
      versioned = url.pathname.startsWith('/api/v1');
      if (request.method === 'GET' && ['/healthz', '/readyz'].includes(url.pathname)) {
        // Liveness remains local; readiness also requires usable durable state and operator config.
        const configured = !shuttingDown && productInitialized
          && Boolean(product || service?.targetId || environmentAdapter || process.env.RAILSHOT_PROFILES_FILE);
        json(response, url.pathname === '/readyz' && !configured ? 503 : 200, { ok: true, configured, ...(!access.remote && { target_id: service?.targetId || null }) }); return;
      }
      if (url.pathname === '/internal/deployments/resume' && request.method === 'POST') {
        versioned = true;
        if (!access.token || !allowsToken(request.headers.authorization, access.token))
          throw new ServiceError('API authentication required', 401);
        if (shuttingDown || release || draining) throw new ServiceError('Platform update in progress', 503);
        const input = await jsonInput(request);
        if (!input || Object.keys(input).sort().join(',') !== 'operation_id,run_id,source_commit'
            || !/^[a-f0-9-]{36}$/.test(input.operation_id || '')
            || !/^[a-f0-9]{40}$/.test(input.source_commit || '') || !/^[1-9][0-9]*$/.test(input.run_id || ''))
          throw new ServiceError('Exact published deployment identity required', 422);
        activeRequests++; counted = true;
        json(response, 202, await (await productReady).resumePublishedOperation(input)); return;
      }
      if (url.pathname === '/internal/deployments/replay-source' && request.method === 'POST') {
        versioned = true;
        if (!access.token || !allowsToken(request.headers.authorization, access.token))
          throw new ServiceError('API authentication required', 401);
        if (shuttingDown || release || draining) throw new ServiceError('Platform update in progress', 503);
        const input = await jsonInput(request);
        if (!input || !['environment_target_id,operation_id', 'environment_target_id,operation_id,packaging'].includes(Object.keys(input).sort().join(','))
            || !/^[a-f0-9-]{36}$/.test(input.operation_id || '')
            || !/^[A-Za-z0-9._-]{1,128}$/.test(input.environment_target_id || ''))
          throw new ServiceError('Invalid source replay identity', 422);
        activeRequests++; counted = true;
        json(response, 202, await (await productReady).replaySubmittedSource(input.operation_id, input.environment_target_id, input.packaging)); return;
      }
      // Kept outside the public gateway's /api/ route. Always require the operator
      // token, including public-demo mode. The hook has no database/cloud mounts.
      if (url.pathname === '/internal/releases/prepare' && request.method === 'POST') {
        versioned = true;
        if (!access.token || !allowsToken(request.headers.authorization, access.token))
          throw new ServiceError('API authentication required', 401);
        const input = await jsonInput(request);
        if (!input || Object.keys(input).length !== 1 || !/^[a-f0-9-]{36}$/.test(input.release_id || ''))
          throw new ServiceError('Invalid release identity', 400);
        const products = await productReady;
        const desired = request.headers['x-railshot-desired-template'];
        if (!release && /^[a-f0-9]{64}$/.test(desired || '') && desired === process.env.RAILSHOT_POD_TEMPLATE_ID) {
          json(response, 200, { status: 'current', release_id: input.release_id }); return;
        }
        if (!products?.pauseForRelease || shuttingDown) throw new ServiceError('Release preparation unavailable', 503);
        if ((release || draining) && (release || draining) !== input.release_id) throw new ServiceError('Another release is prepared', 409);
        if (!release) {
          // Fence the next queued writer before waiting for the current one.
          // Refresh a bounded lease so an abandoned preparation resumes service.
          draining = input.release_id;
          clearTimeout(releaseTimer);
          releaseTimer = setTimeout(() => {
            release = null; draining = null;
            products.resumeAfterRelease();
          }, releaseLeaseMs);
          releaseTimer.unref();
          const idle = products.pauseForRelease();
          if (activeRequests || !idle) {
            json(response, 202, { status: 'busy', waiting_for: !idle ? 'active_worker' : 'active_request' }, { 'Retry-After': '2' }); return;
          }
          release = input.release_id;
        }
        json(response, 200, { status: 'prepared', release_id: release, lease_ms: releaseLeaseMs }); return;
      }
      if (url.pathname.startsWith('/api/')) {
        if (release || shuttingDown || draining && !['GET', 'HEAD'].includes(request.method)) {
          // The request has not been read or executed. This exact envelope allows
          // a browser to retry the same request safely during the short handover.
          json(response, 503, { error: { code: 'PLATFORM_UPDATING', message: '서버 업데이트를 마치고 요청을 이어서 처리합니다.',
            outcome_unknown: false, retryable: true } }, { 'Retry-After': '1' }); return;
        }
        activeRequests++; counted = true;
        if (request.headers['sec-fetch-site'] === 'cross-site') throw new ServiceError('다른 사이트에서 보낸 요청은 허용되지 않습니다.', 403);
        const clientRoute = /^\/api\/v1\/(enrollments\/[A-Za-z0-9._-]+\/claims|targets\/[A-Za-z0-9._-]+\/(heartbeats|receipts|runtimes))$/.test(url.pathname);
        const prerequisiteRoute = url.pathname === '/api/v1/readiness';
        if (isTokenClaimRoute(url.pathname)) {
          if (!access.token) throw new ServiceError('등록 요청에는 운영자 API 토큰 설정이 필요합니다.', 503);
          let products;
          try { products = await productReady; } catch { throw new ServiceError('제품 저장소 또는 서버 설정을 확인할 수 없습니다.', 503); }
          await openstack.claimToken(request, response, url, products);
          return;
        }
        if (!clientRoute && !prerequisiteRoute && !access.publicDemo && !allowsToken(request.headers.authorization, access.token)) {
          response.setHeader('www-authenticate', 'Bearer');
          throw new ServiceError('API authentication required', 401);
        }
        const registrationRoute = isRegistrationRoute(url.pathname);
        if (registrationRoute) {
          if (!access.token) throw new ServiceError('등록 요청에는 운영자 API 토큰 설정이 필요합니다.', 503);
          if (!allowsToken(request.headers.authorization, access.token)) {
            response.setHeader('www-authenticate', 'Bearer');
            throw new ServiceError('API authentication required', 401);
          }
        }
        if (versioned && await openstack.serveInstaller(request, response, url)) return;
        let products;
        try { products = await productReady; } catch { throw new ServiceError('제품 저장소 또는 서버 설정을 확인할 수 없습니다.', 503); }
        if (prerequisiteRoute) {
          if (request.method !== 'GET') { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = 'GET'; throw error; }
          if (url.searchParams.getAll('scope').length !== 1 || url.searchParams.get('scope') !== 'personal'
              || [...url.searchParams].length !== 1) throw new ServiceError('조회 조건이 잘못되었습니다.', 422);
          json(response, 200, await products.personal.readiness()); return;
        }
        // Public visitors are anonymous cookie sessions. Existing localhost maintenance clients
        // without a cookie retain their private maintenance channel and legacy contracts.
        const dashboardRoute = isDashboardRoute(url.pathname);
        const scoped = !clientRoute && (access.remote || access.publicDemo || dashboardRoute || registrationRoute || (request.headers.cookie || '').includes(`${SESSION_COOKIE}=`) || (request.headers.cookie || '').includes(`${OWNER_COOKIE}=`) || /^\/api\/v1\/(owners|recoveries)$/.test(url.pathname));
        const session = scoped ? products.dashboard.session(cookieToken(request.headers.cookie)) : null;
        const owner = clientRoute ? null : products.dashboard?.owner?.(ownerToken(request.headers.cookie));
        const sessionId = owner?.session_id ?? session?.id ?? null;
        if (session?.token) response.setHeader('Set-Cookie', sessionCookie(session.token, access.remote));
        response.setHeader('Vary', 'Cookie');
        if (versioned) {
          const method = (allowed) => { if (!allowed.includes(request.method)) { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = allowed.join(', '); throw error; } };
          const claimRoute = /^\/api\/v1\/enrollments\/([A-Za-z0-9._-]+)\/claims$/.exec(url.pathname);
          const personalRoute = /^\/api\/v1\/targets(?:\/([A-Za-z0-9._-]+))?(?:\/(enrollments|heartbeats|receipts|runtimes|applications|instances|plans|operations|reconciliations))?$/.exec(url.pathname);
          const ownerRoute = /^\/api\/v1\/(owners|recoveries)$/.exec(url.pathname);
          const personalHandled = claimRoute || ownerRoute || personalRoute && (personalRoute[2] || personalRoute[1] || request.method === 'POST' || url.searchParams.has('scope') || url.searchParams.has('provider'));
          if (personalHandled) {
            if (request.method !== 'GET' && !clientRoute && request.headers['x-railshot-request'] !== 'dashboard') throw new ServiceError('개인 환경 변경 요청 헤더가 필요합니다.', 403);
            if ([...url.searchParams].length && !(personalRoute && !personalRoute[1] && request.method === 'GET')) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            if (ownerRoute) {
              method(ownerRoute[1] === 'owners' ? ['GET', 'POST'] : ['POST']);
              if (request.method === 'GET') { json(response, 200, { id: owner?.id || null, recovery_configured: Boolean(owner) }); return; }
              const input = await jsonInput(request);
              if (ownerRoute[1] === 'owners' && Object.keys(input).length) throw new ServiceError('알 수 없는 소유권 입력입니다.', 422);
              if (ownerRoute[1] === 'owners' && owner) { json(response, 200, { id: owner.id, recovery_configured: true }); return; }
              const result = ownerRoute[1] === 'owners' ? products.dashboard.createOwner() : products.dashboard.recoverOwner(input);
              response.setHeader('Set-Cookie', ownerCookie(result.token, access.remote));
              json(response, ownerRoute[1] === 'owners' ? 201 : 200, { id: result.id, recovery_key: result.recovery_key, recovery_key_once: true, recovery_configured: true }); return;
            }
            if (claimRoute) { method(['POST']); const result = await products.personal.claim(claimRoute[1], await jsonInput(request), request.headers.authorization); json(response, 201, result, { Location: `/api/v1/targets/${result.target_id}` }); return; }
            const [, id, child] = personalRoute;
            if (child === 'runtimes') {
              method(['GET', 'POST']);
              if (request.method === 'GET') json(response, 200, products.personal.runtimeStatus(id, request.headers.authorization));
              else json(response, 202, await products.personal.prepareRuntime(id, await jsonInput(request), request.headers.authorization));
              return;
            }
            if (child === 'heartbeats' || child === 'receipts') { method(['POST']); json(response, 200, await products.personal[child === 'heartbeats' ? 'heartbeat' : 'receipt'](id, await jsonInput(request), request.headers.authorization)); return; }
            const ownerId = owner?.session_id || null;
            if (!id) {
              method(['GET', 'POST']);
              if (request.method === 'POST') { const result = await products.personal.create(await jsonInput(request), ownerId); json(response, 201, result, { Location: `/api/v1/targets/${result.id}` }); return; }
              const params = new URLSearchParams(url.searchParams);
              for (const key of ['scope', 'provider']) if (params.has(key)) { if (params.getAll(key).length !== 1 || params.get(key) !== (key === 'scope' ? 'owned' : 'openstack')) throw new ServiceError('조회 조건이 잘못되었습니다.', 422); params.delete(key); }
              json(response, 200, page(products.personal.list(ownerId), params)); return;
            }
            if (!child) { method(['GET']); json(response, 200, products.personal.get(id, ownerId)); return; }
            method(['applications', 'instances'].includes(child) ? ['GET'] : ['POST']);
            if (child === 'instances') { json(response, 200, { items: await products.personal.instances(id, ownerId), next_marker: null }); return; }
            if (child === 'applications') { json(response, 200, { items: products.personal.applications(id, ownerId), next_marker: null }); return; }
            if (child === 'enrollments') { const result = await products.personal.enrollment(id, await jsonInput(request), ownerId); json(response, 201, result, { Location: `/api/v1/targets/${id}` }); return; }
            if (child === 'reconciliations') {
              const result = await products.personal.reconcile(id, await jsonInput(request), ownerId);
              const location = result.id ? `/api/v1/operations/${result.id}` : `/api/v1/targets/${result.target_id}`;
              json(response, 202, result, { Location: location, 'Retry-After': '2' }); return;
            }
            if (child === 'plans') { const result = await products.personal.plan(id, await jsonInput(request), ownerId); json(response, 201, result, { Location: `/api/v1/plans/${result.id}` }); return; }
            accepted(response, 'operations', await products.personal.remove(id, await jsonInput(request), requestKey(request), ownerId), requestId, 'delete'); return;
          }
          if (registrationRoute) {
            await openstack.serveRegistration(request, response, url, products, sessionId);
            return;
          }
          if (dashboardRoute) {
            await serveDashboard(request, response, url, products, session, requestId, sessionId);
            return;
          }
          if (url.pathname === '/api/v1/options') {
            if (request.method !== 'GET') { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = 'GET'; throw error; }
            json(response, 200, page(products?.deploymentOptions?.() || [], url.searchParams)); return;
          }
          if (url.pathname === '/api/v1/applications/resolve') {
            if (request.method !== 'GET') { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = 'GET'; throw error; }
            const keys = ['environment', 'provider', 'app'];
            const optional = ['target_id', 'targetId', 'registerNew'];
            if ([...url.searchParams.keys()].some((key) => ![...keys, ...optional].includes(key))
                || keys.some((key) => url.searchParams.getAll(key).length !== 1)
                || optional.some((key) => url.searchParams.getAll(key).length > 1)
                || url.searchParams.has('target_id') && url.searchParams.has('targetId')
                || url.searchParams.has('registerNew') && url.searchParams.get('registerNew') !== 'false')
              throw new ServiceError('환경·공급자·앱 이름을 하나씩 입력하세요.', 422);
            const selection = Object.fromEntries(keys.map((key) => [key, url.searchParams.get(key)]));
            // The deployed dashboard serializes its selection object, including
            // targetId=null for cloud providers. Keep its review/start flow compatible.
            const targetId = url.searchParams.get('target_id') ?? url.searchParams.get('targetId');
            if (targetId !== null && !(selection.environment === 'cloud' && targetId === 'null')) selection.target_id = targetId;
            json(response, 200, products.resolveApplication(selection, sessionId)); return;
          }
          const updateRoute = /^\/api\/v1\/applications\/([A-Za-z0-9._-]+)\/updates$/.exec(url.pathname);
          if (updateRoute) {
            if (request.method !== 'POST') { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = 'POST'; throw error; }
            if ([...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            // Authorize before reading an upload or resolving a remote repository.
            products.getApplication(updateRoute[1], sessionId);
            const key = requestKey(request);
            const record = await products.createUpdate(updateRoute[1], await uploadedSource(request, true, false, true), key, sourceLoader, sessionId);
            accepted(response, 'deployments', record, requestId); return;
          }
          const startRoute = /^\/api\/v1\/deployments\/([A-Za-z0-9._-]+)\/start$/.exec(url.pathname);
          if (startRoute) {
            if (request.method !== 'POST') { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = 'POST'; throw error; }
            if ([...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            const input = await jsonInput(request);
            if (Object.keys(input).some((key) => key !== 'rebuild') || input.rebuild !== undefined && typeof input.rebuild !== 'boolean') throw new ServiceError('rebuild는 true 또는 false로 입력하세요.', 422);
            accepted(response, 'deployments', await products.startUpdate(startRoute[1], input, sessionId), requestId); return;
          }
          const sourceRoute = /^\/api\/v1\/deployments\/([A-Za-z0-9._-]+)\/source$/.exec(url.pathname);
          if (sourceRoute) {
            if (request.method !== 'GET') { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = 'GET'; throw error; }
            if ([...url.searchParams.keys()].some((key) => key !== 'variant') || url.searchParams.getAll('variant').length > 1) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            const variant = url.searchParams.get('variant') || 'submitted';
            if (!['submitted', 'deployed', 'failed'].includes(variant)) throw new ServiceError('소스 종류를 확인하세요.', 422);
            const bytes = await sourceArchive(await products.sourceFiles(sourceRoute[1], variant, sessionId));
            response.writeHead(200, { 'content-type': 'application/zip', 'content-length': bytes.length,
              'content-disposition': `attachment; filename="railshot-${sourceRoute[1]}-${variant}.zip"`,
              'cache-control': 'no-store', 'x-content-type-options': 'nosniff' }); response.end(bytes); return;
          }
          const insightRoute = /^\/api\/v1\/deployments\/([A-Za-z0-9._-]+)\/(insights|evidence)$/.exec(url.pathname);
          if (insightRoute) {
            method(['GET']);
            const [, id, kind] = insightRoute, key = kind === 'insights' ? 'minutes' : 'area';
            if ([...url.searchParams.keys()].some(name => name !== key) || url.searchParams.getAll(key).length > 1)
              throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            const value = url.searchParams.get(key) ?? (key === 'minutes' ? '15' : 'deploy');
            if (!(key === 'minutes' ? ['15', '60'] : ['build', 'deploy', 'runtime']).includes(value))
              throw new ServiceError('조회 범위가 잘못되었습니다.', 422);
            const insights = createInsightsService(products, traffic);
            json(response, 200, key === 'minutes' ? await insights.overview(id, sessionId, Number(value)) : await insights.evidence(id, sessionId, value));
            return;
          }
          const diagnosticRoute = /^\/api\/v1\/deployments\/([A-Za-z0-9._-]+)\/(diagnostics|classifications)$/.exec(url.pathname);
          if (diagnosticRoute) {
            const allowed = diagnosticRoute[2] === 'diagnostics' ? 'GET' : 'POST';
            if (request.method !== allowed) { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = allowed; throw error; }
            if ([...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            if (allowed === 'POST' && Object.keys(await jsonInput(request)).length) throw new ServiceError('분류 요청은 빈 객체만 받습니다.', 422);
            const data = allowed === 'GET' ? await products.getDeploymentDiagnostics(diagnosticRoute[1], sessionId)
              : await products.classifyDeployment(diagnosticRoute[1], sessionId);
            json(response, data.state === 'running' && allowed === 'POST' ? 202 : 200, data,
              allowed === 'POST' ? { Location: `/api/v1/deployments/${diagnosticRoute[1]}/diagnostics`, ...(data.state === 'running' ? { 'Retry-After': '2' } : {}) } : {});
            return;
          }
          const eventRoute = /^\/api\/v1\/deployments\/([A-Za-z0-9._-]+)\/events$/.exec(url.pathname);
          if (eventRoute) {
            if (request.method !== 'GET') { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = 'GET'; throw error; }
            if ([...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            if (!products) throw new ServiceError('제품 실행 기능이 설정되지 않았습니다.', 503);
            json(response, 200, await products.getDeploymentEvents(eventRoute[1], sessionId)); return;
          }
          const observationRoute = /^\/api\/v1\/targets\/([A-Za-z0-9._-]+)\/observations$/.exec(url.pathname);
          if (observationRoute) {
            if (request.method !== 'GET') { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = 'GET'; throw error; }
            if ([...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            json(response, 200, await products.getTargetObservation(observationRoute[1], sessionId)); return;
          }
          const logRoute = /^\/api\/v1\/deployments\/([A-Za-z0-9._-]+)\/logs$/.exec(url.pathname);
          if (logRoute) {
            if (request.method !== 'GET') { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = 'GET'; throw error; }
            if ([...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            if (!products) throw new ServiceError('제품 실행 기능이 설정되지 않았습니다.', 503);
            json(response, 200, await products.getDeploymentLogs(logRoute[1], sessionId)); return;
          }
          const lifecyclePlan = /^\/api\/v1\/applications\/([A-Za-z0-9._-]+)\/plans\/([A-Za-z0-9._-]+)$/.exec(url.pathname);
          if (lifecyclePlan) {
            if (request.method !== 'GET') { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = 'GET'; throw error; }
            if ([...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            if (!products) throw new ServiceError('제품 실행 기능이 설정되지 않았습니다.', 503);
            const plan = products.getApplicationPlan(lifecyclePlan[1], lifecyclePlan[2], sessionId);
            if (plan.status === 'planning') response.setHeader('Retry-After', '2');
            json(response, 200, plan); return;
          }
          const lifecycle = /^\/api\/v1\/applications\/([A-Za-z0-9._-]+)\/(plans|operations)$/.exec(url.pathname);
          const operation = /^\/api\/v1\/operations\/([A-Za-z0-9._-]+)$/.exec(url.pathname);
          if (lifecycle || operation) {
            const allowed = lifecycle ? 'POST' : 'GET';
            if (request.method !== allowed) { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = allowed; throw error; }
            if ([...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            if (!products) throw new ServiceError('제품 실행 기능이 설정되지 않았습니다.', 503);
            if (operation) { json(response, 200, products.getOperation(operation[1], sessionId)); return; }
            if (lifecycle[2] === 'plans') {
              const asynchronous = request.headers.prefer?.split(',').some((value) => value.trim().toLowerCase() === 'respond-async') === true;
              const plan = await products.createApplicationPlan(lifecycle[1], await jsonInput(request), sessionId, { asynchronous });
              if (asynchronous) {
                response.setHeader('Preference-Applied', 'respond-async'); response.setHeader('Retry-After', '2');
                response.setHeader('Location', `/api/v1/applications/${lifecycle[1]}/plans/${plan.id}`);
              }
              json(response, asynchronous ? 202 : 201, plan); return;
            }
            const key = requestKey(request);
            accepted(response, 'operations', await products.createApplicationOperation(lifecycle[1], await jsonInput(request), key, sessionId), requestId); return;
          }
          const actionRoute = /^\/api\/v1\/deployments\/([A-Za-z0-9._-]+)\/actions$/.exec(url.pathname);
          if (actionRoute) {
            if (request.method !== 'POST') { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = 'POST'; throw error; }
            if ([...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            if (!products) throw new ServiceError('제품 실행 기능이 설정되지 않았습니다.', 503);
            const input = await jsonInput(request);
            if (!input || Array.isArray(input) || typeof input !== 'object'
                || Object.keys(input).length !== 1 || input.action !== 'resume') throw new ServiceError('action=resume만 입력하세요.', 422);
            accepted(response, 'deployments', await products.resumeDeployment(actionRoute[1], sessionId), requestId, 'resume'); return;
          }
          const routes = /^(?:\/api\/v1\/(targets|applications|builds|deployments|profiles|plans|environments))(?:\/([A-Za-z0-9._-]+))?$/.exec(url.pathname);
          if (!routes) throw new ServiceError('API 경로를 찾을 수 없습니다.', 404);
          const [, kind, id] = routes;
          const methods = id ? ['builds', 'deployments', 'plans', 'environments', 'applications'].includes(kind) ? ['GET'] : [] : ['targets', 'profiles', 'applications'].includes(kind) ? ['GET'] : ['GET', 'POST'];
          if (!methods.length) throw new ServiceError('API 경로를 찾을 수 없습니다.', 404);
          if (!methods.includes(request.method)) { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = methods.join(', '); throw error; }
          if (kind === 'applications') {
            if (id && [...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            json(response, 200, id ? products.getApplication(id, sessionId) : page(products.applications(sessionId), url.searchParams)); return;
          }
          if (['targets', 'profiles'].includes(kind)) {
            json(response, 200, page(products ? await products[kind](sessionId) : [], url.searchParams)); return;
          }
          if (!id && request.method === 'GET') {
            json(response, 200, kind === 'plans' ? page(products.list(kind, sessionId), url.searchParams)
              : products.list(kind, sessionId, pagination(url.searchParams))); return;
          }
          const recordView = kind === 'deployments' && id && request.method === 'GET'
            && [...url.searchParams].length === 1 && url.searchParams.get('view') === 'record';
          if ([...url.searchParams].length && !recordView) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
          if (!products) throw new ServiceError('제품 실행 기능이 설정되지 않았습니다.', 503);
          if (request.method === 'GET') {
            const getter = { builds: 'getBuild', deployments: 'getDeployment', plans: 'getPlan', environments: 'getEnvironment' }[kind];
            json(response, 200, await products[getter](id, sessionId, ...(recordView ? [{ view: 'record' }] : []))); return;
          }
          if (kind === 'builds') {
            const result = await products.createBuild(await uploadedSource(request, true), sourceLoader, sessionId);
            accepted(response, kind, { id: String(result.run_id), status: 'queued' }, requestId); return;
          }
          if (kind === 'deployments') {
            const key = requestKey(request);
            accepted(response, kind, await products.createDeployment(await uploadedSource(request, true, true), key, sourceLoader, sessionId), requestId); return;
          }
          if (kind === 'plans') {
            const plan = await products.createPlan(await jsonInput(request), sessionId);
            json(response, 201, plan, { Location: `/api/v1/plans/${plan.id}` }); return;
          }
          const key = requestKey(request);
          accepted(response, kind, await products.createEnvironment(await jsonInput(request), key, sessionId), requestId); return;
        }
        if (!service) throw new ServiceError('CI 실행 기능이 설정되지 않았습니다.', 503);
        if (request.method === 'POST' && url.pathname === '/api/deploy') {
          if ((request.headers['x-railshot-request'] ?? request.headers['x-jasmin-request']) !== 'deploy') throw new ServiceError('요청 헤더가 필요합니다.', 403);
          const input = await uploadedSource(request);
          input.target_id ??= service.targetId;
          const result = products ? await products.createBuild(input, sourceLoader, sessionId) : await service.deploy(input.files ? input : { ...input, ...await sourceLoader(input.repository_url) });
          json(response, 202, result); return;
        }
        const match = request.method === 'GET' && /^\/api\/runs\/(\d+)$/.exec(url.pathname);
        if (match) { json(response, 200, products ? await products.legacyStatus(match[1], sessionId) : await service.status(match[1])); return; }
        throw new ServiceError('API 경로를 찾을 수 없습니다.', 404);
      }
      const asset = request.method === 'GET' && assets.get(url.pathname);
      if (!asset) { response.writeHead(404).end('Not found'); return; }
      const content = await readFile(join(root, asset[0]));
      response.writeHead(200, { 'content-type': asset[1], 'content-length': content.length, 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' }).end(content);
    } catch (error) { apiError(response, error, requestId, versioned); }
    finally { if (counted) activeRequests--; }
  });
  server.on('close', () => { clearTimeout(releaseTimer); closeProduct().catch(() => {}); });
  server.shutdown = async () => {
    shuttingDown = true;
    await new Promise((resolve) => server.close(resolve));
    await closeProduct();
  };
  server.productReady = productReady;
  return server;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const port = Number(process.env.PORT || 4173), access = apiAccessConfig();
  const server = createAppServer({ access });
  server.listen(port, access.bindHost, () => { console.log(`RAILSHOT API listening on ${access.bindHost}:${port}`); });
  const delay = monitorEventLoopDelay({ resolution: 20 });
  delay.enable();
  let cpu = process.cpuUsage(), observedAt = performance.now();
  const runtimeObservation = (reason) => {
    const now = performance.now(), usage = process.cpuUsage(cpu);
    console.log(JSON.stringify({ event: 'api.runtime_observation', reason, observed_at: new Date().toISOString(),
      interval_ms: Math.round(now - observedAt), cpu_ms: Math.round((usage.user + usage.system) / 1000),
      event_loop_max_ms: Math.round(delay.max / 1e6), event_loop_p99_ms: Math.round(delay.percentile(99) / 1e6),
      rss_bytes: process.memoryUsage().rss }));
    cpu = process.cpuUsage(); observedAt = now; delay.reset();
  };
  const runtimeTimer = setInterval(() => runtimeObservation('interval'), 30_000);
  runtimeTimer.unref();
  let stopping = false;
  const stop = (signal) => {
    if (stopping) return;
    stopping = true;
    runtimeObservation(signal); clearInterval(runtimeTimer); delay.disable();
    server.shutdown().then(() => process.exit(0), () => process.exit(1));
  };
  process.on('SIGTERM', () => stop('SIGTERM'));
  process.on('SIGINT', () => stop('SIGINT'));
}
