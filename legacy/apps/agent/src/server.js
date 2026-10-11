#!/usr/bin/env node
import { createServer } from 'node:http';
import { timingSafeEqual } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { createAgent, AgentError } from './agent.js';

function authorized(header, secret) {
  const actual = Buffer.from(header || '');
  const expected = Buffer.from(`Bearer ${secret}`);
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}

function send(response, status, value) {
  response.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' });
  response.end(JSON.stringify(value));
}

async function body(request) {
  if (!/^application\/json(?:\s*;|$)/i.test(request.headers['content-type'] || '')) throw new AgentError(415, 'application/json 본문이 필요합니다.');
  const chunks = []; let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 32 * 1024) throw new AgentError(413, '요청 본문이 너무 큽니다.');
    chunks.push(chunk);
  }
  try {
    const value = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error();
    return value;
  } catch { throw new AgentError(400, 'JSON 객체가 필요합니다.'); }
}

export function createAgentServer({ secret = process.env.RAILSHOT_AGENT_TOKEN, agent = createAgent({ secret }) } = {}) {
  if (!secret || secret.length < 32) throw new Error('RAILSHOT_AGENT_TOKEN은 32자 이상이어야 합니다.');
  let busy = false;
  return createServer(async (request, response) => {
    try {
      const host = request.headers.host || '';
      if (!/^\[?::1\]?(?::\d+)?$|^(localhost|127\.0\.0\.1)(:\d+)?$/i.test(host) || request.headers.origin) throw new AgentError(403, '허용되지 않은 요청 원점입니다.');
      if (request.method === 'GET' && request.url === '/healthz') { send(response, 200, { ok: true }); return; }
      if (!authorized(request.headers.authorization, secret)) throw new AgentError(401, '인증이 필요합니다.');
      if (request.method !== 'POST' || !['/v1/messages', '/v1/approvals'].includes(request.url)) throw new AgentError(404, '경로를 찾을 수 없습니다.');
      if (busy) throw new AgentError(409, '다른 에이전트 요청이 처리 중입니다.');
      const input = await body(request);
      busy = true;
      try {
        if (request.url === '/v1/messages') {
          if (Object.keys(input).join(',') !== 'message') throw new AgentError(422, 'message만 입력하세요.');
          send(response, 200, await agent.message(input.message));
        } else {
          if (Object.keys(input).join(',') !== 'approval_token') throw new AgentError(422, 'approval_token만 입력하세요.');
          send(response, 200, await agent.approve(input.approval_token));
        }
      } finally { busy = false; }
    } catch (error) {
      const status = error instanceof AgentError ? error.status : 502;
      send(response, status, { error: { code: status === 401 ? 'UNAUTHENTICATED' : status === 422 ? 'INVALID_INPUT' : status === 502 ? 'UPSTREAM_FAILURE' : 'AGENT_ERROR',
        message: error instanceof AgentError ? error.message : '에이전트 요청을 처리하지 못했습니다.' } });
    }
  });
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const server = createAgentServer();
  const port = Number(process.env.RAILSHOT_AGENT_PORT || 4184);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('RAILSHOT_AGENT_PORT가 잘못되었습니다.');
  server.listen(port, '127.0.0.1', () => console.error(`RailShot agent listening on 127.0.0.1:${port}`));
}
