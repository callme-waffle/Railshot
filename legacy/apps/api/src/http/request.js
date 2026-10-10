import { ServiceError } from '../github.js';
import { idempotencyKey } from '../product.js';

export async function readLimited(request, limit) {
  const chunks = []; let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > limit) throw new ServiceError('요청 크기가 허용 범위를 초과했습니다.', 413);
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}
export async function jsonInput(request) {
  if (!/^application\/json(?:\s*;|$)/i.test(request.headers['content-type'] || '')) throw new ServiceError('application/json 요청이 필요합니다.', 415);
  let value, raw;
  try { raw = (await readLimited(request, 64 * 1024)).toString('utf8'); value = JSON.parse(raw); }
  catch (error) { if (error.status) throw error; throw new ServiceError('JSON 형식이 잘못되었습니다.', 400); }
  // JSON.parse validates grammar; this small token pass rejects duplicate decoded object keys at every depth.
  const tokens = raw.match(/"(?:\\[\s\S]|[^"\\])*"|[{}\[\]:]/g) || [], stack = [];
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];
    if (token === '{' || token === '[') stack.push(token === '{' ? new Set() : null);
    else if (token === '}' || token === ']') stack.pop();
    else if (token.startsWith('"') && tokens[i + 1] === ':') {
      const key = JSON.parse(token), keys = stack.at(-1);
      if (keys.has(key)) throw new ServiceError('JSON 필드를 중복해서 보낼 수 없습니다.', 422);
      keys.add(key);
    }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ServiceError('JSON 객체가 필요합니다.', 422);
  return value;
}
export function pagination(parameters) {
  for (const key of parameters.keys()) if (!['limit', 'marker'].includes(key) || parameters.getAll(key).length !== 1) throw new ServiceError('조회 조건이 잘못되었습니다.', 422);
  const rawLimit = parameters.get('limit') ?? '20';
  if (!/^[1-9]\d?$|^100$/.test(rawLimit)) throw new ServiceError('limit는 1–100이어야 합니다.', 422);
  const marker = parameters.get('marker');
  if (marker !== null && (!/^[A-Za-z0-9._-]{1,128}$/.test(marker))) throw new ServiceError('marker가 잘못되었습니다.', 422);
  return { limit: Number(rawLimit), marker };
}
export function page(items, parameters) {
  const { limit, marker } = pagination(parameters);
  const start = marker === null ? 0 : items.findIndex((item) => item.id === marker) + 1;
  if (marker !== null && start === 0) throw new ServiceError('marker가 잘못되었습니다.', 422);
  const visible = items.slice(start, start + limit);
  return { items: visible, next_marker: start + visible.length < items.length ? visible.at(-1).id : null };
}
export function requestKey(request) {
  const count = request.rawHeaders.filter((value, index) => index % 2 === 0 && value.toLowerCase() === 'idempotency-key').length;
  if (count !== 1) throw new ServiceError('Idempotency-Key 하나만 입력하세요.', 422);
  return idempotencyKey(request.headers['idempotency-key']);
}
