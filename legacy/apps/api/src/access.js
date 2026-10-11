import { readFileSync } from 'node:fs';
import { timingSafeEqual } from 'node:crypto';

const localHosts = new Set(['localhost', '127.0.0.1']);
const hostname = /^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*$/i;

export function readApiToken(env = process.env) {
  if (env.RAILSHOT_API_TOKEN !== undefined && env.RAILSHOT_API_TOKEN_FILE !== undefined) {
    throw new Error('Set only one of RAILSHOT_API_TOKEN and RAILSHOT_API_TOKEN_FILE');
  }
  let token = env.RAILSHOT_API_TOKEN;
  if (env.RAILSHOT_API_TOKEN_FILE !== undefined) {
    try { token = readFileSync(env.RAILSHOT_API_TOKEN_FILE, 'utf8').trim(); }
    catch { throw new Error('RAILSHOT_API_TOKEN_FILE cannot be read'); }
  }
  if (token !== undefined && !/^[\x21-\x7e]{32,4096}$/.test(token)) {
    throw new Error('API token must contain 32-4096 non-whitespace ASCII characters');
  }
  return token;
}

export function apiAccessConfig(env = process.env) {
  const bindHost = env.RAILSHOT_BIND_HOST || '127.0.0.1';
  if (!['127.0.0.1', '0.0.0.0'].includes(bindHost)) throw new Error('RAILSHOT_BIND_HOST must be 127.0.0.1 or 0.0.0.0');
  const allowedHosts = new Set((env.RAILSHOT_ALLOWED_HOSTS ?? 'localhost,127.0.0.1').split(',').map((host) => host.trim().toLowerCase()));
  if ([...allowedHosts].some((host) => !hostname.test(host))) throw new Error('RAILSHOT_ALLOWED_HOSTS requires exact hostnames without ports');
  const remote = bindHost !== '127.0.0.1' || [...allowedHosts].some((host) => !localHosts.has(host));
  if (env.RAILSHOT_PUBLIC_DEMO !== undefined && !['0', '1'].includes(env.RAILSHOT_PUBLIC_DEMO)) throw new Error('RAILSHOT_PUBLIC_DEMO must be 0 or 1');
  const publicDemo = env.RAILSHOT_PUBLIC_DEMO === '1';
  const token = readApiToken(env);
  if (remote && (!env.RAILSHOT_ALLOWED_HOSTS || (!publicDemo && !token))) {
    throw new Error('Nonlocal API access requires RAILSHOT_ALLOWED_HOSTS and an API token');
  }
  // Preserve local development origins. Container mode accepts no browser Origin unless explicitly configured.
  let allowedOrigins = remote ? new Set() : null;
  if (env.RAILSHOT_ALLOWED_ORIGINS !== undefined) {
    allowedOrigins = new Set(env.RAILSHOT_ALLOWED_ORIGINS.split(',').map((value) => value.trim()).filter(Boolean));
    for (const origin of allowedOrigins) {
      let url;
      try { url = new URL(origin); } catch { throw new Error('Invalid RAILSHOT_ALLOWED_ORIGINS'); }
      if (url.origin !== origin || (url.protocol !== 'https:' && !(url.protocol === 'http:' && localHosts.has(url.hostname)))) {
        throw new Error('Allowed origins must be exact HTTPS origins, or HTTP localhost origins');
      }
    }
  }
  if (publicDemo && (!env.RAILSHOT_ALLOWED_HOSTS || !allowedOrigins?.size)) {
    throw new Error('Public demo requires explicit RAILSHOT_ALLOWED_HOSTS and RAILSHOT_ALLOWED_ORIGINS');
  }
  return { bindHost, allowedHosts, allowedOrigins, token, remote, publicDemo };
}

export function allowsHost(host, access) {
  const match = /^([a-z0-9.-]+)(?::([0-9]{1,5}))?$/i.exec(host || '');
  return Boolean(match && (!match[2] || Number(match[2]) <= 65535) && access.allowedHosts.has(match[1].toLowerCase()));
}

export function allowsOrigin(origin, access) {
  if (origin === undefined) return true;
  return access.allowedOrigins === null
    ? /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(origin)
    : access.allowedOrigins.has(origin);
}

export function allowsToken(authorization, token) {
  if (!token) return true;
  const supplied = Buffer.from(authorization || '');
  const expected = Buffer.from(`Bearer ${token}`);
  return supplied.length === expected.length && timingSafeEqual(supplied, expected);
}
