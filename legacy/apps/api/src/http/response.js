import { ServiceError } from '../github.js';
import { ProductError } from '../product.js';
import { EnvironmentError } from '../environments.js';
import { DashboardError } from '../sessions.js';

export function json(response, code, data, headers = {}) {
  response.writeHead(code, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', 'x-content-type-options': 'nosniff', ...headers });
  response.end(JSON.stringify(data));
}
export function apiError(response, error, requestId, versioned) {
  const status = error instanceof EnvironmentError && error.status === 400 ? 422 : Number.isInteger(error.status) && error.status >= 400 && error.status < 600 ? error.status : 500;
  const codes = { 400: 'INVALID_INPUT', 401: 'UNAUTHENTICATED', 403: 'FORBIDDEN', 404: 'NOT_FOUND', 405: 'METHOD_NOT_ALLOWED', 409: 'CONFLICT', 413: 'PAYLOAD_TOO_LARGE', 415: 'UNSUPPORTED_MEDIA_TYPE', 422: 'INVALID_INPUT', 502: 'UPSTREAM_FAILURE', 503: 'UPSTREAM_UNAVAILABLE' };
  const message = error instanceof ServiceError || error instanceof ProductError || error instanceof DashboardError ? error.message : '요청을 처리하지 못했습니다.';
  const headers = { 'X-Request-ID': requestId, ...(error.allow ? { Allow: error.allow } : {}), ...(error.retryable ? { 'Retry-After': '2' } : {}) };
  const code = error instanceof EnvironmentError && error.status === 400 ? 'INVALID_INPUT'
    : (error instanceof ServiceError || error instanceof ProductError || error instanceof EnvironmentError || error instanceof DashboardError) && error.code || codes[status] || 'INTERNAL_ERROR';
  // Correlate the safe error envelope without persisting source URLs, credentials or request bodies.
  console.error(JSON.stringify({ event: 'api.request_failed', request_id: requestId, status, code }));
  json(response, status, versioned ? { error: { code, message,
    request_id: requestId, retryable: Boolean(error.retryable), outcome_unknown: Boolean(error.outcomeUnknown),
    ...(error instanceof ProductError && error.admission ? { admission: error.admission } : {}) } } : { error: message }, headers);
}
export function accepted(response, kind, record, requestId, action = 'create') {
  const terminal = !['queued', 'running'].includes(record.status);
  json(response, terminal ? 200 : 202, terminal || kind === 'operations' ? record : { resource_id: record.id, action, status: 'accepted', request_id: requestId },
    { Location: `/api/v1/${kind}/${record.id}`, 'X-Request-ID': requestId, ...(!terminal ? { 'Retry-After': '2' } : {}) });
}
